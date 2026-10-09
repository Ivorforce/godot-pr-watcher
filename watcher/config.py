import os
from dataclasses import dataclass
from pathlib import Path

import pathspec
import yaml

DIGEST_PERIODS = {"hourly": 1, "daily": 24, "weekly": 24 * 7}  # hours


@dataclass
class Config:
    target: str
    label: str
    exclude_paths: list[str]
    index_window_days: int
    default_digest: str
    site_url: str
    repo: str  # the repository running the watcher (subscriptions live here)

    @property
    def exclude_spec(self) -> pathspec.PathSpec:
        return pathspec.GitIgnoreSpec.from_lines(self.exclude_paths)

    def pr_link(self, number: int) -> str:
        """Link through the Pages redirect, so the target PR gets no cross-reference backlink."""
        return f"{self.site_url}/pr/?n={number}"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, _, name = repo.partition("/")
    site_url = raw.get("site_url") or (f"https://{owner.lower()}.github.io/{name}" if repo else "")
    digest = raw.get("default_digest", "daily")
    if digest not in DIGEST_PERIODS:
        raise ValueError(f"default_digest must be one of {sorted(DIGEST_PERIODS)}")
    return Config(
        target=raw["target"],
        label=raw.get("label", "watch"),
        exclude_paths=list(raw.get("exclude_paths") or []),
        index_window_days=int(raw.get("index_window_days", 30)),
        default_digest=digest,
        site_url=site_url.rstrip("/"),
        repo=repo,
    )
