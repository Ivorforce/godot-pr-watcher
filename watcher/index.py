"""Index recent PRs of the target repository: changed files and blame of the lines they modify.

Blame uses GitHub's GraphQL `blame`, at the PR's merge-base, on the old-side lines the PR
modifies or deletes (pure additions have no blame). It honours the target's
`.git-blame-ignore-revs`, so mass-reformat commits don't take credit for lines.
"""
import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta, timezone

from .config import Config
from .gh import GitHub, GitHubError, RateLimited
from .state import State

log = logging.getLogger(__name__)

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BLAME_FILES_PER_QUERY = 5
OBJECTS_PER_QUERY = 25
MAX_FAILURES = 3
MAX_BLAME_FILES = 50  # per PR, the files with the most modified lines; caps cost of mass-edit PRs


class OutOfTime(Exception):
    pass
WORKERS = 8  # PRs analysed concurrently; each is a handful of sequential API calls


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def old_lines_from_patch(patch: str) -> list[int]:
    """Old-side line numbers of removed ('-') lines in a unified diff patch."""
    lines, old = [], None
    for pl in patch.split("\n"):
        m = HUNK.match(pl)
        if m:
            old = int(m.group(1))
        elif old is None or pl.startswith("+") or pl.startswith("\\"):
            continue
        elif pl.startswith("-"):
            lines.append(old)
            old += 1
        else:
            old += 1
    return lines


def update_index(cfg: Config, gh: GitHub, st: State, deadline: float):
    """Refresh PR metadata since the last run, then analyse PRs whose head changed until `deadline`."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=cfg.index_window_days)
    cursor = parse_time(st.meta["cursor"]) if "cursor" in st.meta else window_start
    stop = max(cursor, window_start)
    newest = cursor

    seen = 0
    for pr in gh.paginate(f"repos/{cfg.target}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}):
        updated = parse_time(pr["updated_at"])
        if updated < stop:
            break
        newest = max(newest, updated)
        if parse_time(pr["created_at"]) < window_start:
            continue
        seen += 1
        rec = st.index.setdefault(pr["number"], {"n": pr["number"]})
        draft = bool(pr.get("draft"))
        # When the PR became reviewable: creation, or the run that first saw it out of draft.
        if not draft and "ready_at" not in rec:
            rec["ready_at"] = pr["created_at"] if "draft" not in rec else now.isoformat()
        rec.update({
            "title": pr["title"],
            "author": (pr["user"] or {}).get("login", "").lower(),
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "state": "merged" if pr.get("merged_at") else pr["state"],
            "draft": draft,
            "labels": sorted(label["name"] for label in pr["labels"]),
            "base_sha": pr["base"]["sha"],
            "default_branch": pr["base"]["ref"] == pr["base"]["repo"]["default_branch"],
            "base_ref": pr["base"]["ref"],
            "milestone": (pr.get("milestone") or {}).get("title"),
            "head_sha": pr["head"]["sha"],
        })
    st.meta["cursor"] = newest.isoformat()

    for n in [n for n, r in st.index.items() if parse_time(r["created_at"]) < window_start]:
        del st.index[n]

    pending = sorted((r for r in st.index.values() if r.get("analyzed_sha") != r["head_sha"]),
                     key=lambda r: r["created_at"], reverse=True)

    stop = Event()

    def work(rec) -> bool:
        if stop.is_set() or time.monotonic() > deadline:
            return False
        try:
            analyze(cfg, gh, st, rec, deadline)
            return True
        except OutOfTime:
            return False
        except RateLimited as e:
            if not stop.is_set():
                log.warning("rate limited, stopping analysis for this run: %s", e)
            stop.set()
            return False
        except GitHubError as e:
            rec["failures"] = rec.get("failures", 0) + 1
            if rec["failures"] >= MAX_FAILURES:
                # Give up on blame for this head; path rules still apply if the file list was fetched.
                rec.update({"analyzed_sha": rec["head_sha"], "failures": 0, "files": rec.get("files", []),
                            "old_lines": 0, "direct": {}, "transitive": {}})
            log.warning("PR #%d: analysis failed (%s), attempt %d", rec["n"], e, rec["failures"] or MAX_FAILURES)
            return False

    with ThreadPoolExecutor(WORKERS) as pool:
        done = sum(pool.map(work, pending))
    log.info("index: %d PRs updated, %d analysed, %d still pending, %d indexed",
             seen, done, len(pending) - done, len(st.index))


def analyze(cfg: Config, gh: GitHub, st: State, rec: dict, deadline: float):
    exclude = cfg.exclude_spec
    files, ranges, skipped = [], {}, 0
    for f in gh.paginate(f"repos/{cfg.target}/pulls/{rec['n']}/files"):
        old_path = f.get("previous_filename") or f["filename"]
        files.append(f["filename"])
        if f.get("previous_filename"):
            files.append(f["previous_filename"])
        if exclude.match_file(f["filename"]) or f["status"] == "added":
            continue
        if f.get("patch") is None:
            skipped += f["changes"] > 0  # too large for the API to return a patch
            continue
        lines = old_lines_from_patch(f["patch"])
        if lines:
            ranges[old_path] = lines

    rec["files"] = sorted(set(files))

    base = rec["base_sha"]
    if ranges:  # pure additions have nothing to blame
        try:
            cmp = gh.rest(f"repos/{cfg.target}/compare/{rec['base_sha']}...{rec['head_sha']}", {"per_page": 1})
            base = cmp["merge_base_commit"]["sha"]
        except GitHubError as e:
            log.warning("PR #%d: no merge-base (%s), blaming the base branch head", rec["n"], e)

    if len(ranges) > MAX_BLAME_FILES:
        keep = sorted(ranges, key=lambda p: -len(ranges[p]))[:MAX_BLAME_FILES]
        skipped += len(ranges) - len(keep)
        ranges = {p: ranges[p] for p in keep}

    by_sha = Counter()
    paths = sorted(ranges)
    for i in range(0, len(paths), BLAME_FILES_PER_QUERY):
        if time.monotonic() > deadline:
            raise OutOfTime
        chunk = paths[i:i + BLAME_FILES_PER_QUERY]
        blamed = blame_files(cfg, gh, base, chunk)
        for path in chunk:
            line_sha = blamed.get(path)
            if line_sha is None:
                skipped += 1
                continue
            for ln in ranges[path]:
                if ln in line_sha:
                    by_sha[line_sha[ln]] += 1
    resolve_commits(cfg, gh, st, list(by_sha))
    prs = {st.commits[s][1] for s in by_sha if st.commits.get(s, [None, None])[1]}
    resolve_prs(cfg, gh, st, prs)
    if any(s not in st.commits for s in by_sha) or any(str(n) not in st.prs for n in prs):
        raise GitHubError(0, "some commits or PRs could not be resolved")

    direct, transitive = Counter(), Counter()
    for sha, count in by_sha.items():
        login, pr = st.commits.get(sha, [None, None])
        if login:
            direct[login] += count
        for approver in (st.prs.get(str(pr)) or {}).get("approvers", []) if pr else []:
            transitive[approver] += count

    rec.update({
        "failures": 0,
        "old_lines": sum(len(v) for v in ranges.values()),
        "direct": dict(direct),
        "transitive": dict(transitive),
        "skipped_files": skipped,
        "merge_base": base,
        "analyzed_sha": rec["head_sha"],
    })


def blame_files(cfg: Config, gh: GitHub, commit: str, paths: list[str]) -> dict[str, dict[int, str]]:
    """line -> commit per path. Big files can make GitHub time out a batched query, so a failed
    batch is retried file by file; files that still fail are left out (counted as skipped)."""
    try:
        return _blame_query(cfg, gh, commit, paths, attempts=1)
    except RateLimited:
        raise
    except GitHubError:
        if len(paths) == 1:
            return {}
    out = {}
    for p in paths:
        try:
            out.update(_blame_query(cfg, gh, commit, [p], attempts=2))
        except RateLimited:
            raise
        except GitHubError as e:
            log.warning("blame of %s at %s failed: %s", p, commit[:10], e)
    return out


def _blame_query(cfg: Config, gh: GitHub, commit: str, paths: list[str], attempts: int):
    owner, name = cfg.target.split("/")
    fields = "\n".join(
        f"f{i}: blame(path: {json.dumps(p)}) {{ ranges {{ startingLine endingLine commit {{ oid }} }} }}"
        for i, p in enumerate(paths))
    query = (f'query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ '
             f'object(oid: {json.dumps(commit)}) {{ ... on Commit {{ {fields} }} }} }} }}')
    obj = (gh.graphql(query, attempts=attempts).get("repository") or {}).get("object") or {}
    out = {}
    for i, p in enumerate(paths):
        if not obj.get(f"f{i}"):
            continue
        out[p] = {ln: r["commit"]["oid"] for r in obj[f"f{i}"]["ranges"]
                  for ln in range(r["startingLine"], r["endingLine"] + 1)}
    return out


def resolve_commits(cfg: Config, gh: GitHub, st: State, shas: list[str]):
    """Cache commit -> [author login, PR that introduced it]."""
    owner, name = cfg.target.split("/")
    todo = [s for s in shas if s not in st.commits]
    for i in range(0, len(todo), OBJECTS_PER_QUERY):
        chunk = todo[i:i + OBJECTS_PER_QUERY]
        fields = "\n".join(
            f'c{j}: object(oid: "{s}") {{ ... on Commit {{ author {{ user {{ login }} }} '
            f'associatedPullRequests(first: 1) {{ nodes {{ number }} }} }} }}' for j, s in enumerate(chunk))
        repo = gh.graphql(f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}")
        repo = repo.get("repository") or {}
        for j, s in enumerate(chunk):
            c = repo.get(f"c{j}")
            if c is None:
                continue  # failed in this query (null), not "no author": retry next time
            user = ((c.get("author") or {}).get("user") or {}).get("login")
            prs = ((c.get("associatedPullRequests") or {}).get("nodes") or [])
            st.commits[s] = [user.lower() if user else None, prs[0]["number"] if prs else None]


def resolve_prs(cfg: Config, gh: GitHub, st: State, numbers: set[int]):
    """Cache PR -> author and approvers (approvers carry transitive blame)."""
    owner, name = cfg.target.split("/")
    todo = sorted(n for n in numbers if str(n) not in st.prs)
    for i in range(0, len(todo), OBJECTS_PER_QUERY):
        chunk = todo[i:i + OBJECTS_PER_QUERY]
        fields = "\n".join(
            f"p{n}: pullRequest(number: {n}) {{ author {{ login }} "
            f"reviews(states: APPROVED, first: 50) {{ nodes {{ author {{ login }} }} }} }}" for n in chunk)
        repo = gh.graphql(f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}")
        repo = repo.get("repository") or {}
        for n in chunk:
            p = repo.get(f"p{n}")
            if p is None:
                continue  # failed in this query: retry next time
            author = ((p.get("author") or {}).get("login") or "").lower()
            approvers = {((r.get("author") or {}).get("login") or "").lower()
                         for r in (p.get("reviews") or {}).get("nodes") or []}
            st.prs[str(n)] = {"author": author, "approvers": sorted(a for a in approvers if a and a != author)}
