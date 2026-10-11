import signal
from contextlib import contextmanager

import pathspec

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


def match(rules: Rules, rec: dict, login: str, exclude: pathspec.PathSpec) -> list[tuple[str, str]]:
    """Why an analysed PR matches a subscriber's rules, as (trigger, text) pairs; empty if it doesn't."""
    labels = set(rec.get("labels", []))
    author = rec.get("author")
    if author == login and not rules.own:
        return []
    if rec.get("draft") and not rules.drafts:
        return []
    if (author in rules.ignore_authors or labels & set(rules.ignore_labels)
            or rec.get("milestone") in rules.ignore_milestones or rec.get("base_ref") in rules.ignore_branches):
        return []

    reasons = []
    if rules.path_specs:
        files = [f for f in rec.get("files", [])
                 if not rules.ignore_spec.match_file(f) and not exclude.match_file(f)]
        for pattern, spec in rules.path_specs:
            hits = [f for f in files if spec.match_file(f)]
            if hits:
                more = f" +{len(hits) - 1} more" if len(hits) > 1 else ""
                reasons.append(("paths", f"{code_span(pattern, 40)} → {code_span(hits[0])}{more}"))
                break
    if (rules.my_lines or rules.my_written_lines or rules.most_lines) and rec.get("old_lines"):
        total = rec["old_lines"]
        direct, transitive = rec.get("direct", {}), rec.get("transitive", {})
        mine, approved = direct.get(login, 0), transitive.get(login, 0)
        parts = [f"your lines {mine}"] * bool(mine) + [f"approved {approved}"] * bool(approved)

        def reaches(count, share, at_least):
            return count and ((share is not None and count / total >= share)
                              or (at_least is not None and count >= at_least))

        def most(person):
            return min(direct.get(person, 0) + transitive.get(person, 0), total)

        if reaches(min(mine + approved, total), rules.my_lines_share, rules.my_lines_count):
            reasons.append(("my_lines", f"{' + '.join(parts)} of {total}"))
        elif reaches(mine, rules.my_written_lines_share, rules.my_written_lines_count):
            reasons.append(("my_written_lines", f"your lines {mine} of {total}"))
        elif rules.most_lines and (count := most(login)) and all(
                most(p) <= count for p in {*direct, *transitive}
                if p not in (login, author) and not p.endswith("[bot]")):
            reasons.append(("most_lines", f"most lines: {' + '.join(parts)} of {total}"))
    if author in rules.authors:
        reasons.append(("authors", f"author {author}"))
    if hit := next((label for label in rules.labels if label in labels), None):
        reasons.append(("labels", f"label {code_span(hit, 40)}"))
    if rec.get("milestone") and rec["milestone"] in rules.milestones:
        reasons.append(("milestones", f"milestone {code_span(rec['milestone'], 40)}"))
    if rec.get("base_ref") and rec["base_ref"] in rules.branches:
        reasons.append(("branches", f"branch {code_span(rec['base_ref'], 40)}"))
    # PRs to other branches than the default (e.g. cherry-picks) only count when asked for:
    # with `include: [other_branches]`, or through a `branches` or `milestones` trigger.
    if not rec.get("default_branch", True) and not rules.other_branches:
        reasons = [r for r in reasons if r[0] in ("branches", "milestones")]
    return reasons


def diagnose(rules: Rules, recs: list[dict], login: str, repo_files: list[str] | None = None) -> list[str]:
    """Hints for tuning rules, from the indexed PRs (ignoring filters) and the repository's files.
    Paths are only checked against `repo_files`: a path no recent PR touched is still fine."""
    notes = []
    for pattern, spec in rules.path_specs if repo_files is not None else ():
        if not any(spec.match_file(f) for f in repo_files):
            notes.append(f"{code_span(pattern, 40)} matches no file in the repository. Paths start at the "
                         "repository root; use `**/` to match anywhere.")
    seen_labels = {label for r in recs for label in r.get("labels", [])}
    for label in sorted(set(rules.labels) | set(rules.ignore_labels)):
        if label not in seen_labels:
            notes.append(f"No PR had the label {code_span(label, 40)}. Check the spelling.")
    seen_milestones = {r.get("milestone") for r in recs}
    for milestone in rules.milestones:
        if milestone not in seen_milestones:
            notes.append(f"No PR had the milestone {code_span(milestone, 40)}. Check the spelling.")
    seen_branches = {r.get("base_ref") for r in recs}
    for branch in rules.branches:
        if branch not in seen_branches:
            notes.append(f"No PR targeted the branch {code_span(branch, 40)}.")
    seen_authors = {r.get("author") for r in recs}
    for author in rules.authors:
        if author not in seen_authors:
            notes.append(f"No PRs by {code_span(author, 40)}.")
    return notes
