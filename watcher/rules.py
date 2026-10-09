"""Parse and validate subscription rules from an issue body.

The rules are the first ```yaml (or plain ```) fenced block in the body:

    match:    {paths, my_lines, authors, labels, milestones, branches}   any one triggers
    ignore:   {paths, authors, labels, milestones, branches}             files / PRs to leave out
    include:  [drafts, own, other_branches]                              all off by default
    digest:   hourly | daily | weekly

The issue body is untrusted input: it is only ever parsed with `yaml.safe_load` and type-checked here.
"""
import re
from dataclasses import dataclass, field

import pathspec
import yaml

from .config import DIGEST_PERIODS
from .render import code_span

FENCE = re.compile(r"^```[ \t]*(?:ya?ml)?[ \t]*\n(.*?)^```", re.S | re.M)
MAX_BODY = 20_000
# Patterns become regexes; these limits keep matching fast (see check_pattern).
MAX_PATTERNS = 50
MAX_PATTERN_LEN = 200
MAX_WILDCARD_RUNS = 4
PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")

TOP_KEYS = ("match", "ignore", "include", "digest")
LIST_KEYS = {
    "paths": "a list of path patterns",
    "authors": "a list of GitHub logins",
    "labels": "a list of label names",
    "milestones": "a list of milestone names",
    "branches": "a list of branch names",
}
MATCH_KEYS = (*LIST_KEYS, "my_lines")
INCLUDE = ("drafts", "own", "other_branches")
MY_LINES = "a percentage like `20%`, a line count like `40`, or both: `[20%, 40]`"


def anchored(pattern: str) -> str:
    """Plain patterns start at the repository root (`editor/` is the top-level editor folder,
    not every `editor/` folder); patterns starting with `*` or `/` keep gitignore semantics."""
    return pattern if pattern.startswith(("/", "*")) else "/" + pattern


@dataclass
class Rules:
    # match
    paths: list[str] = field(default_factory=list)
    my_lines_share: float | None = None   # `20%`
    my_lines_count: int | None = None     # `40`
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
    # include
    drafts: bool = False
    own: bool = False
    other_branches: bool = False
    digest: str | None = None

    def __post_init__(self):
        # One spec per pattern, so a match can say which pattern caused it.
        self.path_specs = [(p, pathspec.GitIgnoreSpec.from_lines([anchored(p)])) for p in self.paths]
        self.ignore_spec = pathspec.GitIgnoreSpec.from_lines([anchored(p) for p in self.ignore_paths])

    @property
    def my_lines(self) -> bool:
        return self.my_lines_share is not None or self.my_lines_count is not None


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
        elif key == "my_lines":
            # One value, or a list with a share and a count (either one triggers).
            parsed = [parse_my_lines(x) for x in (v if isinstance(v, list) else [v])]
            ok = bool(parsed) and None not in parsed
            shares = [share for share, _ in parsed if share is not None] if ok else []
            counts = [count for _, count in parsed if count is not None] if ok else []
            if ok and len(shares) <= 1 and len(counts) <= 1:
                out["my_lines_share"] = shares[0] if shares else None
                out["my_lines_count"] = counts[0] if counts else None
            else:
                errors.append(f"`match: my_lines:` must be {MY_LINES}.")
        elif (parsed := parse_list(name, key, v, errors)) is not None:
            out[key] = parsed
    return out


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
            kw.update(parse_section("match", value, MATCH_KEYS, errors))
        elif key == "ignore":
            kw.update({f"ignore_{k}": v for k, v in parse_section("ignore", value, tuple(LIST_KEYS), errors).items()})
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
    has_trigger = any(kw.get(k) for k in LIST_KEYS) or kw.get("my_lines_share") is not None \
        or kw.get("my_lines_count") is not None
    if not errors and not has_trigger:
        errors.append("Nothing to match: add at least one key under `match:`, e.g. `paths: [core/]`.")
    if errors:
        return None, errors
    return Rules(**kw), []
