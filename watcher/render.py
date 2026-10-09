"""Comment text. Everything posted is templated, and nothing in it may ping or reference the target:

- PR links go through the Pages redirect (`Config.pr_link`), so target PRs get no
  "mentioned this" backlink from this repository.
- Text taken from the target (titles) has `@` and `#` defused, so it can't mention anyone or
  reference an issue; author names are written without `@`.
"""
import re

from .config import Config

ZWSP = "​"
PREVIEW_MARKER = "<!-- pr-watcher:preview -->"
DIGEST_MARKER = "<!-- pr-watcher:digest -->"
STATE_NOTE = {"merged": " (merged)", "closed": " (closed)"}


def defuse(text: str) -> str:
    """Make untrusted text unable to mention, reference or link (GitHub autolinks `GH-123` too)."""
    text = " ".join(text.split())  # no newlines: keep it on its line
    for ch in "@#":
        text = text.replace(ch, ch + ZWSP)
    text = re.sub(r"(?i)\bgh-(?=\d)", lambda m: m.group(0)[:2] + ZWSP + "-", text)
    return text.replace("://", ":" + ZWSP + "//")


def code_span(text: str, limit: int = 80) -> str:
    """Untrusted text as inline code. Mentions and references aren't parsed inside code; backticks
    and newlines are removed so the span can't be closed early."""
    text = " ".join(text.replace("`", "'").split())
    return f"`{text[:limit - 1] + '…' if len(text) > limit else text}`"


def pr_line(cfg: Config, rec: dict, reasons: list[str]) -> str:
    title = defuse(rec.get("title", ""))
    if len(title) > 100:
        title = title[:99] + "…"
    pr = f"[PR {rec['n']}]({cfg.pr_link(rec['n'])}) {title}"
    if rec.get("state") in STATE_NOTE:  # merged or closed: struck through, so it stands out
        pr = f"~~{pr.replace('~', '~' + ZWSP)}~~{STATE_NOTE[rec['state']]}"
    draft = " · draft" if rec.get("draft") else ""
    return f"- {pr} — by {defuse(rec.get('author', '?'))} · {' · '.join(reasons)}{draft}"


def digest(cfg: Config, items: list[tuple[dict, list[str]]], more: int, feed_url: str) -> str:
    n = len(items) + max(more, 0)
    lines = [DIGEST_MARKER, f"**{n} new PR{'s' if n != 1 else ''} match{'es' if n == 1 else ''} your rules**", ""]
    lines += [pr_line(cfg, rec, reasons) for rec, reasons in items]
    if more > 0:
        lines.append(f"- … and {more} more (your [feed]({feed_url}) lists recent matches)")
    lines += ["", f"<sub>Edit this issue's description to change your rules; close it to stop. "
                  f"[Feed]({feed_url})</sub>"]
    return "\n".join(lines)


def preview(cfg: Config, errors: list[str], items: list[tuple[dict, list[str]]],
            per_trigger: dict[str, int], notes: list[str], weeks: float) -> str:
    lines = [PREVIEW_MARKER]
    if errors:
        lines += ["**These rules aren't active yet:**", ""]
        lines += [f"- {defuse(e)}" for e in errors]
        return "\n".join(lines)
    n = len(items)
    rate = n / weeks if weeks else 0
    if n:
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(per_trigger.items()))
        lines += [f"**Rules look good.** Over the last {cfg.index_window_days} days they would have matched "
                  f"**{n} PR{'s' if n != 1 else ''}** (about {rate:.1f} per week; {breakdown}).", ""]
    else:
        lines += [f"**Rules are valid, but matched nothing in the last {cfg.index_window_days} days.** "
                  "Check your paths.", ""]
    lines += [f"- {defuse(note)}" for note in notes] + ([""] if notes else [])
    if items:
        lines += ["<details><summary>Matches</summary>", ""]
        lines += [pr_line(cfg, rec, reasons) for rec, reasons in items[:50]]
        if n > 50:
            lines.append(f"- … and {n - 50} more")
        lines += ["", "</details>", ""]
    lines.append("<sub>You'll get digests for matching PRs opened after you created this issue. "
                 "Edit the description to change your rules; this preview updates.</sub>")
    return "\n".join(lines)
