"""Subscriptions (issues), digest delivery, previews, and engagement evaluation."""
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import render
from .config import DIGEST_PERIODS, Config
from .gh import GitHub, GitHubError
from .index import parse_time
from .match import diagnose, match, time_limit
from .rules import parse_rules
from .state import State

log = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS = 3       # open subscription issues per person; extras are ignored
UNLIMITED = {"OWNER", "MEMBER", "COLLABORATOR"}  # people with access to this repo have no limit
MAX_DIGEST_ITEMS = 50       # more are only counted in the comment (they're still in the feed)
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


def queue_matches(cfg: Config, st: State, active: dict[str, dict]):
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
                    reasons = match(a["rules"], rec, a["login"], exclude)
                    if reasons:
                        sub["queue"][str(n)] = [text for _, text in reasons]
        except TimeoutError:
            log.warning("issue %s: matching took over %ds, skipped this run", key, MATCH_SECONDS)


def send_digests(cfg: Config, gh: GitHub, st: State, active: dict[str, dict], dry_run: bool):
    """Post due digests. A digest is recorded as sent *before* posting, so a failure later in the
    run (or a failed post) can never make it go out twice; at worst one digest is lost."""
    now = now_utc()
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
                        reasons = match(a["rules"], rec, a["login"], cfg.exclude_spec)
                        if reasons:
                            items.append((rec, [text for _, text in reasons]))
        except TimeoutError:
            log.warning("issue %s: re-checking the queue took over %ds, skipped this run", key, MATCH_SECONDS)
            continue
        sub["queue"] = {}
        sub["last_digest_at"] = now.isoformat()
        for rec, reasons in items:
            st.notified.append({"issue": int(key), "login": a["login"], "pr": rec["n"],
                                "at": now.isoformat(), "reasons": reasons, "engaged": None})
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
        if sub.get("last_comment_node"):
            minimize(gh, sub["last_comment_node"])
        sub["last_comment_node"] = comment["node_id"]
        log.info("digest: issue %s, %d PRs", key, len(items))


def minimize(gh: GitHub, node_id: str):
    try:
        gh.graphql('mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) '
                   '{ minimizedComment { isMinimized } } }', {"id": node_id})
    except GitHubError as e:
        log.warning("could not minimize comment %s: %s", node_id, e)


def preview_matches(cfg: Config, st: State, rules, login: str):
    """(matches as (rec, reason texts), matches per trigger, tuning notes).
    Raises TimeoutError if the rules are too slow to evaluate."""
    exclude = cfg.exclude_spec
    recs = [rec for rec in st.index.values() if rec.get("analyzed_sha") == rec.get("head_sha")]
    with time_limit(MATCH_SECONDS * 2):
        matched = [(rec, why) for rec in recs if (why := match(rules, rec, login, exclude))]
        notes = diagnose(rules, recs, login)
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
            items, per_trigger, notes = preview_matches(cfg, st, rules, login)
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


def evaluate(cfg: Config, gh: GitHub, st: State):
    """Record whether notified people later took part in the PR (comment, review, ...)."""
    owner, name = cfg.target.split("/")
    cutoff = now_utc() - EVALUATE_AFTER
    todo = [r for r in st.notified if r["engaged"] is None and parse_time(r["at"]) < cutoff][:EVALUATE_PER_RUN]
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
