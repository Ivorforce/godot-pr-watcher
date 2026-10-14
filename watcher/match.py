import signal
from contextlib import contextmanager

import pathspec

from .codeowners import CodeOwners
from .render import code_span
from .rules import Rules


@contextmanager
def time_limit(seconds: float):
    """Raise TimeoutError if the block runs longer than `seconds` (main thread only).
    Backstop against slow subscriber patterns: regex matching is interruptible by signals."""
    def expired(*_):
        raise TimeoutError
    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _file_hit(what: str, files: list[str]) -> str:
    more = f" +{len(files) - 1} more" if len(files) > 1 else ""
    return f"{what} → {code_span(files[0])}{more}"


def checks(r: Rules, rec: dict, login: str, files: list[str], owners: CodeOwners | None) -> dict[str, str | None]:
    """Each trigger that `r` sets, mapped to its reason text if it holds for the PR, else None.
    `files` are the PR's files that count (after excludes and ignored files)."""
    out = {}
    labels = set(rec.get("labels", []))
    author = rec.get("author")
    if r.path_specs:
        out["paths"] = next((_file_hit(code_span(pattern, 40), hits) for pattern, spec in r.path_specs
                             if (hits := [f for f in files if spec.match_file(f)])), None)
    if r.teams:
        out["teams"] = next((_file_hit(f"team {code_span(team, 40)}", hits) for team in r.teams if owners
                             and (hits := [f for f in files if team in owners.teams_of(f)])), None)
    if r.my_lines or r.my_written_lines or r.most_lines or r.lines_by or r.written_lines_by:
        total = rec.get("old_lines") or 0
        direct, transitive = rec.get("direct", {}), rec.get("transitive", {})
        mine, approved = direct.get(login, 0), transitive.get(login, 0)
        parts = " + ".join([f"your lines {mine}"] * bool(mine) + [f"approved {approved}"] * bool(approved))

        def reaches(count, share, at_least):
            return bool(total and count) and ((share is not None and count / total >= share)
                                              or (at_least is not None and count >= at_least))

        def by(person, written):
            """Reason text if `person`'s lines reach their threshold in `lines_by` / `written_lines_by`."""
            share, at_least = (r.written_lines_by if written else r.lines_by)[person]
            wrote, appr = direct.get(person, 0), 0 if written else transitive.get(person, 0)
            if not reaches(min(wrote + appr, total), share, at_least):
                return None
            counts = " + ".join([f"lines {wrote}"] * bool(wrote) + [f"approved {appr}"] * bool(appr))
            return f"by {person}: {counts} of {total}"

        def most(person):
            return min(direct.get(person, 0) + transitive.get(person, 0), total)

        if r.my_lines:
            out["my_lines"] = f"{parts} of {total}" if reaches(
                min(mine + approved, total), r.my_lines_share, r.my_lines_count) else None
        if r.my_written_lines:
            out["my_written_lines"] = f"your lines {mine} of {total}" if reaches(
                mine, r.my_written_lines_share, r.my_written_lines_count) else None
        for key, written in (("lines_by", False), ("written_lines_by", True)):
            if getattr(r, key):
                out[key] = next((text for person in getattr(r, key) if (text := by(person, written))), None)
        if r.most_lines:
            count = most(login)
            out["most_lines"] = f"most lines: {parts} of {total}" if total and count and all(
                most(p) <= count for p in {*direct, *transitive}
                if p not in (login, author) and not p.endswith("[bot]")) else None
    if r.authors:
        out["authors"] = f"author {author}" if author in r.authors else None
    if r.labels:
        out["labels"] = next((f"label {code_span(label, 40)}" for label in r.labels if label in labels), None)
    if r.milestones:
        m = rec.get("milestone")
        out["milestones"] = f"milestone {code_span(m, 40)}" if m and m in r.milestones else None
    if r.branches:
        b = rec.get("base_ref")
        out["branches"] = f"branch {code_span(b, 40)}" if b and b in r.branches else None
    return out


# Within each group, only the first trigger that holds is shown.
LINE_TRIGGERS = ("my_lines", "my_written_lines", "most_lines")
BY_TRIGGERS = ("lines_by", "written_lines_by")
BRANCH_TRIGGERS = ("branches", "milestones")


def match(rules: Rules, rec: dict, login: str, exclude: pathspec.PathSpec,
          owners: CodeOwners | None = None) -> list[tuple[str, str]]:
    """Why an analysed PR matches a subscriber's rules, as (trigger, text) pairs; empty if it doesn't.
    Without `owners` (the target's CODEOWNERS), `teams` rules match and ignore nothing."""
    labels = set(rec.get("labels", []))
    author = rec.get("author")
    if author == login and not rules.own:
        return []
    if rec.get("draft") and not rules.drafts:
        return []
    if (author in rules.ignore_authors or labels & set(rules.ignore_labels)
            or rec.get("milestone") in rules.ignore_milestones or rec.get("base_ref") in rules.ignore_branches):
        return []
    changed = [f for f in rec.get("files", []) if not exclude.match_file(f)]
    if any(all(checks(entry, rec, login, changed, owners).values()) for entry in rules.ignore_all):
        return []

    ignored_teams = set(rules.ignore_teams) if owners else set()
    files = [f for f in changed if not rules.ignore_spec.match_file(f)
             and not (ignored_teams and owners.teams_of(f) & ignored_teams)]
    held = {k: text for k, text in checks(rules, rec, login, files, owners).items() if text}
    shown = {next((k for k in group if k in held), None) for group in (LINE_TRIGGERS, BY_TRIGGERS)}
    reasons = [(k, text) for k, text in held.items() if k not in LINE_TRIGGERS + BY_TRIGGERS or k in shown]
    for entry in rules.match_all:
        c = checks(entry, rec, login, files, owners)
        if all(c.values()):
            # Kind "all", or a branch trigger's kind if the entry names the branch or milestone.
            kind = next((k for k in BRANCH_TRIGGERS if k in c), "all")
            reasons.append((kind, " + ".join(c.values())))
    # PRs to other branches than the default (e.g. cherry-picks) only count when asked for:
    # with `include: [other_branches]`, or through a `branches` or `milestones` trigger.
    if not rec.get("default_branch", True) and not rules.other_branches:
        reasons = [r for r in reasons if r[0] in BRANCH_TRIGGERS]
    return reasons


def diagnose(rules: Rules, recs: list[dict], login: str, repo_files: list[str] | None = None,
             owners: CodeOwners | None = None) -> list[str]:
    """Hints for tuning rules, from the indexed PRs (ignoring filters) and the repository's files.
    Paths are only checked against `repo_files`: a path no recent PR touched is still fine."""
    notes = []
    parts = [rules, *rules.match_all, *rules.ignore_all]

    def names(attr, *extra):
        return list(dict.fromkeys([x for r in parts for x in getattr(r, attr)] + [x for e in extra for x in e]))

    specs = {pattern: spec for r in parts for pattern, spec in r.path_specs}
    for pattern, spec in specs.items() if repo_files is not None else ():
        if not any(spec.match_file(f) for f in repo_files):
            notes.append(f"{code_span(pattern, 40)} matches no file in the repository. Paths start at the "
                         "repository root; use `**/` to match anywhere.")
    teams = names("teams", rules.ignore_teams)
    if teams and owners is None:
        notes.append("Couldn't read the repository's CODEOWNERS file, so `teams` matches nothing for now.")
    elif unknown := [t for t in teams if t not in owners.teams]:
        notes.append(f"No team {', '.join(code_span(t, 40) for t in unknown)} in CODEOWNERS. "
                     f"Teams there: {', '.join(code_span(t, 40) for t in sorted(owners.teams))}.")
    seen_labels = {label for r in recs for label in r.get("labels", [])}
    for label in sorted(set(names("labels", rules.ignore_labels))):
        if label not in seen_labels:
            notes.append(f"No PR had the label {code_span(label, 40)}. Check the spelling.")
    seen_milestones = {r.get("milestone") for r in recs}
    for milestone in names("milestones"):
        if milestone not in seen_milestones:
            notes.append(f"No PR had the milestone {code_span(milestone, 40)}. Check the spelling.")
    seen_branches = {r.get("base_ref") for r in recs}
    for branch in names("branches"):
        if branch not in seen_branches:
            notes.append(f"No PR targeted the branch {code_span(branch, 40)}.")
    seen_people = {p for r in recs for p in (*r.get("direct", {}), *r.get("transitive", {}))}
    for person in dict.fromkeys(names("lines_by") + names("written_lines_by")):
        if person not in seen_people:
            notes.append(f"No recent PR changed lines by {code_span(person, 40)}. Check the spelling.")
    seen_authors = {r.get("author") for r in recs}
    for author in names("authors"):
        if author not in seen_authors:
            notes.append(f"No PRs by {code_span(author, 40)}.")
    return notes
