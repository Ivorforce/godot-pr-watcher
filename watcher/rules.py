"""Parse and validate subscription rules from an issue body.

The rules are the first ```yaml (or plain ```) fenced block in the body:

    match:    {paths, teams, my_lines, my_written_lines, most_lines,     any one triggers
               lines_by, written_lines_by, authors, labels, milestones, branches, all}
    ignore:   {paths, teams, authors, labels, milestones, branches, all} files / PRs to leave out

`all:` is one entry or a list of entries with the same keys as its section; all keys of an entry
must hold (e.g. an author *and* a team). In `ignore: all:`, paths and teams ask whether the PR
touches any such file, so the entry leaves out whole PRs.
    include:  [drafts, own, other_branches, involved]                    all off by default
    digest:   hourly | daily | weekly

The issue body is untrusted input: it is only ever parsed with `yaml.safe_load` and type-checked here.
"""
import re
from dataclasses import dataclass, field

import pathspec
import yaml

from .codeowners import team_slug
from .config import DIGEST_PERIODS
from .render import code_span

FENCE = re.compile(r"^```[ \t]*(?:ya?ml)?[ \t]*\n(.*?)^```", re.S | re.M)
MAX_BODY = 20_000
# Patterns become regexes; these limits keep matching fast (see check_pattern).
MAX_PATTERNS = 50
MAX_PATTERN_LEN = 200
MAX_WILDCARD_RUNS = 4
MAX_ALL = 10           # entries per `all:`
MAX_PEOPLE = 10        # logins per `lines_by:`
PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")

TOP_KEYS = ("match", "ignore", "include", "digest")
LIST_KEYS = {
    "paths": "a list of path patterns",
    "teams": "a list of team names from CODEOWNERS, e.g. `core`",
    "authors": "a list of GitHub logins",
    "labels": "a list of label names",
    "milestones": "a list of milestone names",
    "branches": "a list of branch names",
}
LINE_KEYS = ("my_lines", "my_written_lines")  # written or approved / written only
BY_KEYS = ("lines_by", "written_lines_by")     # the same for other people, per login
MATCH_KEYS = (*LIST_KEYS, *LINE_KEYS, "most_lines", *BY_KEYS, "all")
IGNORE_KEYS = (*LIST_KEYS, "all")
INCLUDE = ("drafts", "own", "other_branches", "involved")
MY_LINES = "a percentage like `20%`, a line count like `40`, or both: `[20%, 40]`"


def anchored(pattern: str) -> str:
    """Plain patterns start at the repository root (`editor/` is the top-level editor folder,
    not every `editor/` folder); patterns starting with `*` or `/` keep gitignore semantics."""
    return pattern if pattern.startswith(("/", "*")) else "/" + pattern


@dataclass
class Rules:
    # match
    paths: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    my_lines_share: float | None = None   # `20%`
    my_lines_count: int | None = None     # `40`
    my_written_lines_share: float | None = None
    my_written_lines_count: int | None = None
    most_lines: bool = False              # more lines (written or approved) than anyone but the author
    lines_by: dict[str, tuple[float | None, int | None]] = field(default_factory=dict)  # login -> (share, count)
    written_lines_by: dict[str, tuple[float | None, int | None]] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    # ignore
    ignore_paths: list[str] = field(default_factory=list)
    ignore_authors: list[str] = field(default_factory=list)
    ignore_labels: list[str] = field(default_factory=list)
    ignore_milestones: list[str] = field(default_factory=list)
    ignore_branches: list[str] = field(default_factory=list)
    ignore_teams: list[str] = field(default_factory=list)
    # all: (entries are Rules that use only the match fields)
    match_all: list["Rules"] = field(default_factory=list)
    ignore_all: list["Rules"] = field(default_factory=list)
    # include
    drafts: bool = False
    own: bool = False
    other_branches: bool = False
    involved: bool = False                # PRs you're already requested on, assigned to or took part in
    digest: str | None = None

    def __post_init__(self):
        # One spec per pattern, so a match can say which pattern caused it.
        self.path_specs = [(p, pathspec.GitIgnoreSpec.from_lines([anchored(p)])) for p in self.paths]
        self.ignore_spec = pathspec.GitIgnoreSpec.from_lines([anchored(p) for p in self.ignore_paths])

    @property
    def my_lines(self) -> bool:
        return self.my_lines_share is not None or self.my_lines_count is not None

    @property
    def uses_paths(self) -> bool:
        return bool(self.paths or any(e.paths for e in self.match_all + self.ignore_all))

    @property
    def uses_teams(self) -> bool:
        return bool(self.teams or self.ignore_teams or any(e.teams for e in self.match_all + self.ignore_all))

    @property
    def my_written_lines(self) -> bool:
        return self.my_written_lines_share is not None or self.my_written_lines_count is not None


def check_pattern(p: str) -> str | None:
    """Reject patterns that could make matching slow (catastrophic regex backtracking)."""
    if p.startswith("!"):
        return "starts with `!`; use `ignore: paths:` instead"
    if len(p) > MAX_PATTERN_LEN:
        return f"is longer than {MAX_PATTERN_LEN} characters"
    if len(re.findall(r"\*+", p)) > MAX_WILDCARD_RUNS:
        return f"has more than {MAX_WILDCARD_RUNS} wildcards"
    if any(len(run) > 2 for run in re.findall(r"\*+", p)) or any(
            "**" in seg and seg != "**" for seg in p.split("/")):
        return "uses `**` other than as a whole path segment, like `a/**/b`"
    return None


def parse_my_lines(value) -> tuple[float | None, int | None] | None:
    """`20%` (or 0.2) is a share of the PR's changed lines; a whole number ≥ 1 is a line count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and (m := PERCENT.match(value)):
        value = float(m.group(1)) / 100
        return (value, None) if 0 < value <= 1 else None
    if isinstance(value, int) and value >= 1:
        return None, value
    if isinstance(value, float) and 0 < value < 1:
        return value, None
    return None


def parse_threshold(value) -> tuple[float | None, int | None] | None:
    """One `parse_my_lines` value, or a list with a share and a count (either one triggers)."""
    parsed = [parse_my_lines(x) for x in (value if isinstance(value, list) else [value])]
    if not parsed or None in parsed:
        return None
    shares = [share for share, _ in parsed if share is not None]
    counts = [count for _, count in parsed if count is not None]
    if len(shares) > 1 or len(counts) > 1:
        return None
    return (shares[0] if shares else None), (counts[0] if counts else None)


def parse_list(section: str, key: str, value, errors: list[str]) -> list[str] | None:
    if isinstance(value, str):
        value = [value]
    if not (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)):
        errors.append(f"`{section}: {key}:` must be {LIST_KEYS[key]}.")
        return None
    if key == "paths":
        if len(value) > MAX_PATTERNS:
            errors.append(f"`{section}: paths:` has more than {MAX_PATTERNS} patterns.")
            return None
        bad = [(v, why) for v in value if (why := check_pattern(v))]
        errors += [f"Pattern {code_span(v)} {why}." for v, why in bad]
        return None if bad else value
    if key == "authors":
        return [v.strip().lstrip("@").lower() for v in value]
    if key == "teams":
        return [team_slug(v) for v in value]
    return value


def parse_section(name: str, value, allowed: tuple[str, ...], errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append(f"`{name}:` must contain `key: value` lines, e.g. `{name}:` then `  paths: [core/]`.")
        return {}
    out = {}
    for key, v in value.items():
        if key not in allowed:
            errors.append(f"Unknown key {code_span(str(key))} in `{name}:`. "
                          f"Known keys: {', '.join(f'`{k}`' for k in allowed)}.")
        elif v is None:
            continue
        elif key == "most_lines":
            if isinstance(v, bool):
                out["most_lines"] = v
            else:
                errors.append(f"`{name}: most_lines:` must be `true` or `false`.")
        elif key in LINE_KEYS:
            if (threshold := parse_threshold(v)) is not None:
                out[f"{key}_share"], out[f"{key}_count"] = threshold
            else:
                errors.append(f"`{name}: {key}:` must be {MY_LINES}.")
        elif key in BY_KEYS:
            if not (isinstance(v, dict) and v and all(isinstance(login, str) and login.strip() for login in v)):
                errors.append(f"`{name}: {key}:` must be GitHub logins with a threshold each, "
                              f"e.g. `{key}: {{someone: [20%, 40]}}`.")
            elif len(v) > MAX_PEOPLE:
                errors.append(f"`{name}: {key}:` has more than {MAX_PEOPLE} logins.")
            else:
                people = {}
                for login, threshold in v.items():
                    if (parsed := parse_threshold(threshold)) is None:
                        errors.append(f"`{name}: {key}: {login}:` must be {MY_LINES}.")
                    people[login.strip().lstrip("@").lower()] = parsed
                out[key] = people
        elif key == "all":
            entries = v if isinstance(v, list) else [v]
            if not entries or not all(isinstance(e, dict) and e for e in entries):
                errors.append(f"`{name}: all:` must be entries with keys that must all match, e.g. "
                              f"`- {{authors: [someone], teams: [core]}}`.")
            elif len(entries) > MAX_ALL:
                errors.append(f"`{name}: all:` has more than {MAX_ALL} entries.")
            else:
                inner = tuple(k for k in allowed if k != "all")
                parsed = [parse_section(f"{name}: all", e, inner, errors) for e in entries]
                if not errors and not all(has_trigger(p) for p in parsed):
                    errors.append(f"Each entry in `{name}: all:` needs at least one key with a value.")
                out["all"] = [Rules(**p) for p in parsed] if not errors else []
        elif (parsed := parse_list(name, key, v, errors)) is not None:
            out[key] = parsed
    return out


def has_trigger(kw: dict) -> bool:
    return any(kw.get(k) for k in (*LIST_KEYS, *BY_KEYS, "most_lines", "all", "match_all")) or any(
        kw.get(f"{k}_{part}") is not None for k in LINE_KEYS for part in ("share", "count"))


def parse_rules(body: str | None) -> tuple[Rules | None, list[str]]:
    """Return (rules, errors). `rules` is None when there are errors."""
    body = (body or "").replace("\r\n", "\n")
    if len(body) > MAX_BODY:
        return None, [f"The issue description is longer than {MAX_BODY} characters."]
    m = FENCE.search(body)
    if not m:
        return None, ["No rules found. They must be in a YAML code block (the issue form adds one); "
                      "check that its ``` lines are still there."]
    try:
        raw = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        if "alias" in str(e):
            return None, ['Patterns starting with `*` need quotes, e.g. `- "**/*.glsl"`.']
        return None, [f"The rules are not valid YAML: {code_span(str(e).splitlines()[0], 200)}"]
    if not isinstance(raw, dict):
        return None, ["The rules must be `key: value` lines, starting with `match:`."]

    errors, kw = [], {}
    for key, value in raw.items():
        if key not in TOP_KEYS:
            hint = " Put it under `match:`." if key in MATCH_KEYS else ""
            errors.append(f"Unknown key {code_span(str(key))}.{hint} "
                          f"Known keys: {', '.join(f'`{k}`' for k in TOP_KEYS)}.")
        elif value is None:
            continue
        elif key == "match":
            section = parse_section("match", value, MATCH_KEYS, errors)
            if "all" in section:
                section["match_all"] = section.pop("all")
            kw.update(section)
        elif key == "ignore":
            kw.update({f"ignore_{k}": v for k, v in parse_section("ignore", value, IGNORE_KEYS, errors).items()})
        elif key == "include":
            items = value if isinstance(value, list) else [value]
            bad = [x for x in items if x not in INCLUDE]
            if bad:
                errors.append(f"`include:` takes {', '.join(f'`{k}`' for k in INCLUDE)}; "
                              f"got {', '.join(code_span(str(x), 40) for x in bad)}.")
            kw.update({k: True for k in INCLUDE if k in items})
        elif key == "digest":
            if value in DIGEST_PERIODS:
                kw["digest"] = value
            else:
                errors.append(f"`digest:` must be one of {', '.join(DIGEST_PERIODS)}.")
    if not errors and not has_trigger({k: v for k, v in kw.items() if not k.startswith("ignore_")}):
        errors.append("Nothing to match: add at least one key under `match:`, e.g. `paths: [core/]`.")
    if errors:
        return None, errors
    return Rules(**kw), []
