"""Persistent state, stored as JSON files on the `data` branch.

    state/meta.json            index cursor
    state/index.jsonl          one analysed PR per line (PRs created within the index window)
    state/commits.json         commit sha -> [author login, associated PR number]
    state/prs.json             PR number -> {"author", "approvers"} (for transitive blame)
    state/subscriptions.json   issue number -> digest bookkeeping and queued matches
    state/notified.jsonl       one line per delivered notification (+ later engagement)

Caches and the notification log are pruned on save, so state stays a few MB at most.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_COMMITS = 50_000      # cache sizes; when exceeded the cache is dropped and refilled on demand
MAX_PRS = 20_000
KEEP_NOTIFIED = timedelta(days=90)  # older notifications are dropped; evaluated ones are kept as meta stats


class State:
    def __init__(self, root: Path):
        self.dir = root / "state"
        self.meta = self._load("meta.json", {})
        self.index = {int(r["n"]): r for r in self._load_lines("index.jsonl")}
        self.commits = self._load("commits.json", {})
        self.prs = self._load("prs.json", {})
        self.subs = self._load("subscriptions.json", {})
        self.notified = self._load_lines("notified.jsonl")

    def _load(self, name, default):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def _load_lines(self, name):
        p = self.dir / name
        return [json.loads(line) for line in p.read_text().splitlines() if line] if p.exists() else []

    def engagement_stats(self) -> tuple[int, int]:
        """(engaged, evaluated) over all notifications, including pruned ones."""
        done = [r for r in self.notified if r["engaged"] is not None]
        return (self.meta.get("engaged", 0) + sum(r["engaged"] for r in done),
                self.meta.get("evaluated", 0) + len(done))

    def prune(self):
        if len(self.commits) > MAX_COMMITS:
            self.commits = {}
        if len(self.prs) > MAX_PRS:
            self.prs = {}
        cutoff = datetime.now(timezone.utc) - KEEP_NOTIFIED
        keep = []
        for r in self.notified:
            if datetime.fromisoformat(r["at"]) >= cutoff:
                keep.append(r)
            elif r["engaged"] is not None:
                self.meta["evaluated"] = self.meta.get("evaluated", 0) + 1
                self.meta["engaged"] = self.meta.get("engaged", 0) + r["engaged"]
        self.notified = keep

    def save(self):
        self.prune()
        self.dir.mkdir(parents=True, exist_ok=True)
        # Sorted keys and one record per line: stable output that's easy to inspect.
        for name, obj in [("meta.json", self.meta), ("commits.json", self.commits),
                          ("prs.json", self.prs), ("subscriptions.json", self.subs)]:
            (self.dir / name).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
        self._save_lines("index.jsonl", [self.index[n] for n in sorted(self.index)])
        self._save_lines("notified.jsonl", self.notified)

    def _save_lines(self, name, rows):
        (self.dir / name).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
