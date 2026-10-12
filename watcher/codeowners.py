"""Team ownership from the target repository's CODEOWNERS file, for `teams` rules.

Only the file is needed (it's public); team membership and visibility never matter, since the
subscriber names the team. As in GitHub, the last matching rule decides a path's owners.
"""
import base64
import logging

import pathspec

from .config import Config
from .gh import GitHub, GitHubError

log = logging.getLogger(__name__)

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def team_slug(name: str) -> str:
    """`@godotengine/core`, `godotengine/core` and `core` all mean the team `core`."""
    return name.strip().lstrip("@").rpartition("/")[2].lower()


class CodeOwners:
    def __init__(self, text: str):
        self.rules = []
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2:
                teams = frozenset(team_slug(o) for o in parts[1:] if o.startswith("@") and "/" in o)
                self.rules.append((pathspec.GitIgnoreSpec.from_lines([parts[0]]), teams))
        self.teams = frozenset().union(*(t for _, t in self.rules))
        self._cache: dict[str, frozenset[str]] = {}

    def teams_of(self, path: str) -> frozenset[str]:
        if path not in self._cache:
            self._cache[path] = next((t for spec, t in reversed(self.rules) if spec.match_file(path)), frozenset())
        return self._cache[path]


def load(cfg: Config, gh: GitHub) -> CodeOwners | None:
    """The target's CODEOWNERS, or None if it has none or it can't be read right now."""
    for path in LOCATIONS:
        try:
            res = gh.rest(f"repos/{cfg.target}/contents/{path}")
        except GitHubError as e:
            if e.status == 404:
                continue
            log.warning("could not read %s: %s", path, e)
            return None
        return CodeOwners(base64.b64decode(res["content"]).decode(errors="replace"))
    return None
