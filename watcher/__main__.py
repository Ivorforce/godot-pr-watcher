"""godot-pr-watcher: notify subscribers about pull requests that match their rules.

    python -m watcher run [--budget S] [--dry-run]          index, queue matches, send due digests
    python -m watcher preview --issue N [--dry-run]         reply to a subscription issue
    python -m watcher preview --rules FILE --login LOGIN    try rules locally against the index

State is read from and written to `--data` (a checkout of the `data` branch).
Needs GITHUB_TOKEN; locally, `GITHUB_TOKEN=$(gh auth token)` works.
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import notify
from .config import load_config
from .gh import GitHub
from .index import update_index
from .rules import parse_rules
from .site import build_site
from .state import State


def main():
    ap = argparse.ArgumentParser(prog="watcher")
    ap.add_argument("command", choices=["run", "preview"])
    ap.add_argument("--config", type=Path, default=Path("config.yml"))
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--budget", type=float, default=40, help="seconds to spend analysing PRs")
    ap.add_argument("--dry-run", action="store_true", help="print comments instead of posting them")
    ap.add_argument("--issue", type=int)
    ap.add_argument("--rules", type=Path, help="preview: rules file instead of an issue")
    ap.add_argument("--login", help="preview: subscriber login for --rules")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    gh = GitHub(os.environ.get("GITHUB_TOKEN", ""))  # not needed for a local --rules preview
    st = State(args.data)

    if args.command == "run":
        start = time.monotonic()
        failed = False

        def stage(fn, *a):
            # A failing stage is logged and the rest still run, so state (e.g. digests already
            # posted) is always saved.
            nonlocal failed
            try:
                return fn(*a)
            except Exception:
                logging.exception("%s failed", fn.__name__)
                failed = True

        stage(update_index, cfg, gh, st, start + args.budget)
        if cfg.repo:
            stage(notify.ensure_label, cfg, gh, st)
            active = stage(notify.load_subscriptions, cfg, gh, st)
            if active is not None:
                stage(notify.queue_matches, cfg, st, active)
                stage(notify.send_digests, cfg, gh, st, active, args.dry_run)
            stage(notify.evaluate, cfg, gh, st)
        else:
            logging.warning("GITHUB_REPOSITORY not set: only indexing, no subscriptions")
        st.save()
        build_site(cfg, st, args.data)
        logging.info("run: %.1fs", time.monotonic() - start)
        if failed:
            sys.exit(1)
    elif args.issue is not None:
        notify.preview_issue(cfg, gh, st, args.issue, args.dry_run)
    elif args.rules and args.login:
        text = args.rules.read_text()
        rules, errors = parse_rules(text if "```" in text else f"```yaml\n{text}\n```")
        if errors:
            sys.exit("\n".join(errors))
        items, per_trigger, notes = notify.preview_matches(cfg, st, rules, args.login.lower())
        for rec, reasons in items:
            print(f"#{rec['n']} {rec['title']} — {' · '.join(reasons)}")
        print(f"\n{len(items)} matches ({dict(per_trigger)})", *notes, sep="\n")
    else:
        ap.error("preview needs --issue, or --rules and --login")


if __name__ == "__main__":
    main()
