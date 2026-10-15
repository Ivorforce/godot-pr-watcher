"""Subscriptions (issues), digest delivery, previews, and engagement evaluation."""
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import codeowners, render
from .codeowners import CodeOwners
from .config import DIGEST_PERIODS, Config
from .gh import GitHub, GitHubError, RateLimited
from .index import parse_time
from .match import diagnose, match, time_limit
from .rules import parse_rules
from .state import State

log = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS = 3       # open subscription issues per person; extras are ignored
UNLIMITED = {"OWNER", "MEMBER", "COLLABORATOR"}  # people with access to this repo have no limit
MAX_DIGEST_ITEMS = 50       # more are only counted in the comment (they're still in the feed)
SHOWN = ("title", "author", "draft", "state")  # PR fields kept per posted digest line, to re-render it
MATCH_SECONDS = 5           # per subscription and run; guards against slow patterns
EVALUATE_AFTER = timedelta(days=7)
EVALUATE_PER_RUN = 100


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def feed_url(cfg: Config, login: str) -> str:
    return f"{cfg.site_url}/feeds/{login}.xml"


def too_many_error(login_count: int) -> str:
    return (f"You have {login_count} open subscriptions; only your oldest {MAX_SUBSCRIPTIONS} are active. "
            "Close one, or merge its paths into another.")


def ensure_label(cfg: Config, gh: GitHub, st: State):
    """The issue form only applies labels that exist."""
    if st.meta.get("label") == cfg.label:
        return
    try:
        gh.rest(f"repos/{cfg.repo}/labels/{cfg.label}")
    except GitHubError as e:
        if e.status != 404:
            raise
        gh.rest(f"repos/{cfg.repo}/labels", method="POST",
                body={"name": cfg.label, "color": "0e8a16", "description": "PR watcher subscription"})
    st.meta["label"] = cfg.label


def load_subscriptions(cfg: Config, gh: GitHub, st: State) -> dict[str, dict]:
    """Sync st.subs with the open, labelled issues. Returns issue -> {issue, login, rules, errors}."""
    active, per_login = {}, defaultdict(list)
    issues = [i for i in gh.paginate(f"repos/{cfg.repo}/issues", {"labels": cfg.label, "state": "open"})
              if "pull_request" not in i]
    for issue in sorted(issues, key=lambda i: i["number"]):
        login = issue["user"]["login"].lower()
        per_login[login].append(issue)
        rules, errors = parse_rules(issue.get("body"))
        if len(per_login[login]) > MAX_SUBSCRIPTIONS and issue.get("author_association") not in UNLIMITED:
            rules, errors = None, [too_many_error(len(per_login[login]))]
        key = str(issue["number"])
        sub = st.subs.setdefault(key, {"login": login, "created_at": issue["created_at"], "queue": {}})
        sub["login"] = login
        active[key] = {"issue": issue["number"], "login": login, "rules": rules, "errors": errors}
    for key in [k for k in st.subs if k not in active]:
        del st.subs[key]
    return active


def uses_teams(active: dict[str, dict]) -> bool:
    return any(a["rules"] and a["rules"].uses_teams for a in active.values())


def queue_matches(cfg: Config, st: State, active: dict[str, dict], owners: CodeOwners | None = None):
    """Queue newly matching PRs for each subscription. Every PR is notified at most once per issue."""
    exclude = cfg.exclude_spec
    sent = {(str(r["issue"]), r["pr"]) for r in st.notified}
    for key, a in active.items():
        if a["rules"] is None:
            continue
        sub = st.subs[key]
        since = parse_time(sub["created_at"])
        # Only PRs that became reviewable after subscribing (or were opened after, for drafts: true).
        start_key = "created_at" if a["rules"].drafts else "ready_at"
        try:
            with time_limit(MATCH_SECONDS):
                for n, rec in st.index.items():
                    if (rec.get("analyzed_sha") != rec.get("head_sha") or rec.get("state") != "open"
                            or parse_time(rec.get(start_key) or rec["created_at"]) < since
                            or str(n) in sub["queue"] or (key, n) in sent):
                        continue
                    reasons = match(a["rules"], rec, a["login"], exclude, owners)
                    if reasons:
                        sub["queue"][str(n)] = [text for _, text in reasons]
        except TimeoutError:
            log.warning("issue %s: matching took over %ds, skipped this run", key, MATCH_SECONDS)


MENTION = re.compile(r"(?<![\w`])@([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)", re.I)


def involvement(cfg: Config, gh: GitHub, numbers: list[int]) -> dict[int, set[str]]:
    """PR -> people GitHub already notifies about it: requested reviewers, assignees, participants
    (author, commenters, reviewers) and people @-mentioned in the description. PRs that couldn't be
    looked up are missing, so nobody is skipped for them."""
    owner, name = cfg.target.split("/")
    out = {}
    for i in range(0, len(numbers), 25):
        chunk = numbers[i:i + 25]
        fields = " ".join(
            f"p{n}: pullRequest(number: {n}) {{ body participants(first: 100) {{ nodes {{ login }} }} "
            f"assignees(first: 20) {{ nodes {{ login }} }} "
            f"reviewRequests(first: 50) {{ nodes {{ requestedReviewer {{ ... on User {{ login }} }} }} }} }}"
            for n in chunk)
        try:
            repo = gh.graphql(f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
        except GitHubError as e:
            log.warning("could not look up who is involved in %d PRs: %s", len(chunk), e)
            continue
        for n in chunk:
            pr = (repo.get("repository") or {}).get(f"p{n}")
            if pr is None:
                continue
            people = {p["login"] for key in ("participants", "assignees")
                      for p in (pr.get(key) or {}).get("nodes") or [] if p}
            people |= {(r.get("requestedReviewer") or {}).get("login")
                       for r in (pr.get("reviewRequests") or {}).get("nodes") or [] if r}
            people |= set(MENTION.findall(pr.get("body") or ""))
            out[n] = {p.lower() for p in people if p}
    return out


def send_digests(cfg: Config, gh: GitHub, st: State, active: dict[str, dict], dry_run: bool,
                 owners: CodeOwners | None = None):
    """Post due digests. A digest is recorded as sent *before* posting, so a failure later in the
    run (or a failed post) can never make it go out twice; at worst one digest is lost.
    PRs the subscriber is already involved in are recorded as skipped, unless `include: involved`."""
    now = now_utc()
    due = {}
    for key, a in active.items():
        sub = st.subs[key]
        if not sub["queue"] or a["rules"] is None:
            continue
        period = timedelta(hours=DIGEST_PERIODS[a["rules"].digest or cfg.default_digest])
        last = parse_time(sub.get("last_digest_at") or sub["created_at"])
        if now - last < period - timedelta(minutes=10):
            continue
        # Re-check queued PRs: labels, draft state or the rules may have changed since.
        items = []
        try:
            with time_limit(MATCH_SECONDS):
                for n in sorted(sub["queue"], key=int):
                    rec = st.index.get(int(n))
                    if rec and rec.get("state") != "closed":
                        reasons = match(a["rules"], rec, a["login"], cfg.exclude_spec, owners)
                        if reasons:
                            items.append((rec, reasons))
        except TimeoutError:
            log.warning("issue %s: re-checking the queue took over %ds, skipped this run", key, MATCH_SECONDS)
            continue
        due[key] = items

    check = sorted({rec["n"] for key, items in due.items() if not active[key]["rules"].involved
                    for rec, _ in items})
    involved = involvement(cfg, gh, check) if check else {}

    for key, items in due.items():
        a, sub = active[key], st.subs[key]
        sub["queue"] = {}
        sub["last_digest_at"] = now.isoformat()
        skip = set() if a["rules"].involved else {
            rec["n"] for rec, _ in items
            if rec.get("author") != a["login"] and a["login"] in involved.get(rec["n"], ())}
        rows = {}
        for rec, reasons in items:
            rows[rec["n"]] = {"issue": int(key), "login": a["login"], "pr": rec["n"],
                            "at": now.isoformat(), "reasons": [text for _, text in reasons],
                            "kinds": sorted({kind for kind, _ in reasons}), "engaged": None,
                            **({"skipped": "involved"} if rec["n"] in skip else {})}
            st.notified.append(rows[rec["n"]])
        if skip:
            log.info("issue %s: skipped %d PRs the subscriber is already involved in", key, len(skip))
        items = [(rec, [text for _, text in reasons]) for rec, reasons in items if rec["n"] not in skip]
        if not items:
            continue
        body = render.digest(cfg, items[:MAX_DIGEST_ITEMS], len(items) - MAX_DIGEST_ITEMS,
                             feed_url(cfg, a["login"]))
        if dry_run:
            print(f"--- digest for issue {key} ({a['login']}) ---\n{body}\n")
            continue
        try:
            comment = gh.rest(f"repos/{cfg.repo}/issues/{key}/comments", method="POST", body={"body": body})
        except GitHubError as e:
            log.warning("digest for issue %s (%d PRs) could not be posted: %s", key, len(items), e)
            continue
        for rec, _ in items:
            rows[rec["n"]]["digest"] = {"comment": comment["id"], **{k: rec.get(k) for k in SHOWN}}
        log.info("digest: issue %s, %d PRs", key, len(items))


def update_digests(cfg: Config, gh: GitHub, st: State, dry_run: bool):
    """Strike through PRs in posted digests once they're merged or closed (and undo it if one is
    reopened). Everything else stays as posted, titles included, so a digest still reads like the
    email it sent. Works while the PRs are in the index."""
    by_comment = defaultdict(list)
    for r in st.notified:
        if "digest" in r:
            by_comment[r["digest"]["comment"]].append(r)
    for comment, rows in by_comment.items():
        rows.sort(key=lambda r: r["pr"])
        state = {r["pr"]: st.index[r["pr"]].get("state") for r in rows[:MAX_DIGEST_ITEMS]
                 if r["pr"] in st.index and st.index[r["pr"]].get("state") != r["digest"]["state"]}
        if not state:
            continue
        items = [({**r["digest"], "n": r["pr"], "state": state.get(r["pr"], r["digest"]["state"])}, r["reasons"])
                 for r in rows]
        body = render.digest(cfg, items[:MAX_DIGEST_ITEMS], len(items) - MAX_DIGEST_ITEMS,
                             feed_url(cfg, rows[0]["login"]))
        if dry_run:
            print(f"--- updated digest {comment} on issue {rows[0]['issue']} ---\n{body}\n")
            continue
        try:
            gh.rest(f"repos/{cfg.repo}/issues/comments/{comment}", method="PATCH", body={"body": body})
        except RateLimited:
            raise
        except GitHubError as e:
            log.warning("digest %s on issue %s could not be updated: %s", comment, rows[0]["issue"], e)
            if e.status == 404:  # comment or issue deleted
                for r in rows:
                    del r["digest"]
            continue
        for r in rows:
            r["digest"]["state"] = state.get(r["pr"], r["digest"]["state"])
        log.info("digest %s on issue %s: %d PRs changed state", comment, rows[0]["issue"], len(state))


def keep_alive(cfg: Config, gh: GitHub, active: dict[str, dict]):
    """GitHub disables scheduled workflows after 60 days without repository activity. Re-enabling
    resets that timer; only done while someone is subscribed, so an unused watcher still stops."""
    if not active:
        return
    try:
        gh.rest(f"repos/{cfg.repo}/actions/workflows/watch.yml/enable", method="PUT")
    except GitHubError as e:
        log.warning("could not reset the schedule's inactivity timer: %s", e)


_repo_files: dict[str, list[str] | None] = {}


def repo_files(cfg: Config, gh: GitHub) -> list[str] | None:
    """All file paths on the target's default branch, or None if they can't be listed in one call.
    Fetched once per process."""
    if cfg.target not in _repo_files:
        _repo_files[cfg.target] = _list_repo_files(cfg, gh)
    return _repo_files[cfg.target]


def _list_repo_files(cfg: Config, gh: GitHub) -> list[str] | None:
    try:
        branch = gh.rest(f"repos/{cfg.target}")["default_branch"]
        tree = gh.rest(f"repos/{cfg.target}/git/trees/{branch}", {"recursive": "1"})
    except GitHubError as e:
        log.warning("could not list the repository's files: %s", e)
        return None
    return None if tree.get("truncated") else [t["path"] for t in tree["tree"] if t["type"] == "blob"]


def preview_matches(cfg: Config, st: State, rules, login: str, files: list[str] | None = None,
                    owners: CodeOwners | None = None):
    """(matches as (rec, reason texts), matches per trigger, tuning notes). `files` (the repository's
    paths) enables the check for path patterns that match nothing.
    Raises TimeoutError if the rules are too slow to evaluate."""
    exclude = cfg.exclude_spec
    recs = [rec for rec in st.index.values() if rec.get("analyzed_sha") == rec.get("head_sha")]
    with time_limit(MATCH_SECONDS * 2):
        matched = [(rec, why) for rec in recs if (why := match(rules, rec, login, exclude, owners))]
        notes = diagnose(rules, recs, login, files, owners)
    per_trigger = Counter(kind for _, why in matched for kind in {k for k, _ in why})
    items = sorted(((rec, [text for _, text in why]) for rec, why in matched), key=lambda x: -x[0]["n"])
    return items, per_trigger, notes


def preview_issue(cfg: Config, gh: GitHub, st: State, number: int, dry_run: bool):
    """Reply to a new or edited subscription issue with validation errors or a dry-run preview."""
    issue = gh.rest(f"repos/{cfg.repo}/issues/{number}")
    if issue["state"] != "open" or cfg.label not in {label["name"] for label in issue["labels"]}:
        log.info("issue %d is not an open subscription, skipping preview", number)
        return
    login = issue["user"]["login"].lower()
    rules, errors = parse_rules(issue.get("body"))
    mine = sorted(i["number"] for i in gh.paginate(
        f"repos/{cfg.repo}/issues", {"labels": cfg.label, "state": "open", "creator": login})
        if "pull_request" not in i)
    if number in mine[MAX_SUBSCRIPTIONS:] and issue.get("author_association") not in UNLIMITED:
        rules, errors = None, [too_many_error(len(mine))]
    items, per_trigger, notes = [], Counter(), []
    if rules:
        try:
            files = repo_files(cfg, gh) if rules.uses_paths else None
            owners = codeowners.load(cfg, gh) if rules.uses_teams else None
            items, per_trigger, notes = preview_matches(cfg, st, rules, login, files, owners)
        except TimeoutError:
            rules, errors = None, ["Your path patterns take too long to evaluate. Simplify them."]
    body = render.preview(cfg, errors, items, per_trigger, notes, cfg.index_window_days / 7)
    if dry_run:
        print(body)
        return
    for c in gh.paginate(f"repos/{cfg.repo}/issues/{number}/comments"):
        if c["user"]["login"] == "github-actions[bot]" and c["body"].startswith(render.PREVIEW_MARKER):
            gh.rest(f"repos/{cfg.repo}/issues/comments/{c['id']}", method="PATCH", body={"body": body})
            return
    gh.rest(f"repos/{cfg.repo}/issues/{number}/comments", method="POST", body={"body": body})


def preview_all(cfg: Config, gh: GitHub, st: State, dry_run: bool):
    """Refresh the preview on every open subscription, e.g. after the watcher itself changed."""
    issues = [i["number"] for i in gh.paginate(f"repos/{cfg.repo}/issues", {"labels": cfg.label, "state": "open"})
              if "pull_request" not in i]
    failed = 0
    for number in sorted(issues):
        try:
            preview_issue(cfg, gh, st, number, dry_run)
        except GitHubError as e:
            log.warning("preview for issue %d failed: %s", number, e)
            failed += 1
    log.info("refreshed %d previews (%d failed)", len(issues) - failed, failed)
    if failed:
        raise GitHubError(0, f"{failed} previews failed")


def evaluate(cfg: Config, gh: GitHub, st: State):
    """Record whether notified people later took part in the PR (comment, review, ...)."""
    owner, name = cfg.target.split("/")
    cutoff = now_utc() - EVALUATE_AFTER
    todo = [r for r in st.notified if r["engaged"] is None and not r.get("skipped")
            and parse_time(r["at"]) < cutoff][:EVALUATE_PER_RUN]
    numbers = sorted({r["pr"] for r in todo})
    participants = {}
    for i in range(0, len(numbers), 25):
        chunk = numbers[i:i + 25]
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ participants(first: 100) {{ nodes {{ login }} }} }}"
                          for n in chunk)
        repo = gh.graphql(f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
        for n in chunk:
            pr = (repo.get("repository") or {}).get(f"p{n}")
            if pr is not None:
                participants[n] = {p["login"].lower() for p in (pr.get("participants") or {}).get("nodes") or []}
    for r in todo:
        if r["pr"] in participants:
            r["engaged"] = r["login"] in participants[r["pr"]]
    stats = st.engagement_stats()
    if stats[1]:
        log.info("evaluate: %d/%d evaluated notifications engaged (%.0f%%)", *stats, 100 * stats[0] / stats[1])
    for kind, (engaged, evaluated) in sorted(st.engagement_by_kind().items()):
        log.info("evaluate: %s: %d/%d engaged (%.0f%%)", kind, engaged, evaluated, 100 * engaged / evaluated)
