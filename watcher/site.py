"""The GitHub Pages site on the data branch (`docs/`): PR redirect page and per-subscriber feeds."""
import json
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config
from .state import State

FEED_ITEMS = 200

REDIRECT = """<!doctype html>
<meta charset="utf-8">
<title>Redirecting…</title>
<script>
const n = new URLSearchParams(location.search).get("n");
if (/^[0-9]+$/.test(n)) location.replace({base} + n);
</script>
<p>Redirecting to the pull request… (needs JavaScript)</p>
"""

INDEX = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR watcher</title>
<p>Notifications for pull requests in <a href="https://github.com/{target}">{target}</a>.
<a href="https://github.com/{repo}">Subscribe by opening an issue</a>.</p>
"""


def build_site(cfg: Config, st: State, root: Path):
    docs = root / "docs"
    (docs / "pr").mkdir(parents=True, exist_ok=True)
    (docs / "feeds").mkdir(exist_ok=True)
    (docs / ".nojekyll").write_text("")
    (docs / "index.html").write_text(INDEX.format(target=escape(cfg.target), repo=escape(cfg.repo)))
    (docs / "pr" / "index.html").write_text(
        REDIRECT.replace("{base}", json.dumps(f"https://github.com/{cfg.target}/pull/")))

    # Feeds exist only for current subscribers; logins are [a-z0-9-], so safe as file names.
    by_login = defaultdict(list)
    for r in st.notified:
        by_login[r["login"]].append(r)
    subscribers = {s["login"] for s in st.subs.values()}
    for old in (docs / "feeds").glob("*.xml"):
        if old.stem not in subscribers:
            old.unlink()
    for login in subscribers:
        rows = by_login.get(login, [])
        (docs / "feeds" / f"{login}.xml").write_text(feed(cfg, login, rows[-FEED_ITEMS:][::-1], st))


def feed(cfg: Config, login: str, rows: list[dict], st: State) -> str:
    entries = []
    for r in rows:
        rec = st.index.get(r["pr"], {})
        url = f"https://github.com/{cfg.target}/pull/{r['pr']}"
        title = f"PR {r['pr']}: {rec.get('title', '')}"
        entries.append(
            f"<entry><id>{escape(url)}#{escape(r['at'])}</id><title>{escape(title)}</title>"
            f'<link href="{escape(url)}"/><updated>{escape(r["at"])}</updated>'
            f"<summary>{escape(' · '.join(x.replace('`', '') for x in r['reasons']))}</summary></entry>")
    updated = rows[0]["at"] if rows else "1970-01-01T00:00:00+00:00"
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
            f"<id>{escape(cfg.site_url)}/feeds/{escape(login)}.xml</id>"
            f"<title>PR watcher: {escape(login)}</title><updated>{escape(updated)}</updated>"
            + "".join(entries) + "</feed>\n")
