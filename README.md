# godot-pr-watcher

Get notified about new [Godot](https://github.com/godotengine/godot) PRs that touch paths you pick,
or lines you wrote or approved. Nothing is posted to the Godot repo.

## Usage

[Open a "Watch pull requests" issue](../../issues/new/choose), put in your rules, submit.

- A bot comment shows what the rules would have matched in the last 30 days, or what's wrong with
  them. It updates when you edit the issue.
- Matching PRs then arrive as digest comments on your issue, so you get normal GitHub
  notifications. Older digests get collapsed. Each digest links an Atom feed too.
- Edit the issue to change rules, close it to stop.

Rules are public. Max 3 open subscriptions per person. Bugs or questions: open a blank issue.

## Rules

```yaml
match:                     # notify if any of these match
  paths:                   # from the repo root; "**/" matches anywhere
    - modules/gdscript/
    - "**/*.glsl"          # quote patterns that start with *
  my_lines: [20%, 40]      # ≥20% or ≥40 of the changed lines are yours or from PRs you approved
  authors: [someone]
  labels: [topic:gdscript]
  milestones: ["3.7"]
  branches: [3.x]
ignore:                    # files, or PRs, to leave out; same keys except my_lines
  paths: ["**/tests/"]
  labels: [documentation]
include: [drafts, own, other_branches]  # all off by default
digest: daily              # hourly, daily (default) or weekly
```

`other_branches` means PRs to branches other than `master` (e.g. cherry-picks); you get those
anyway when they match through `branches` or `milestones`.

You hear about each PR once, and only about PRs that became ready for review after you subscribed.
The preview tells you about patterns or labels that matched nothing.

Labels and milestones are often added hours after a PR opens. One that triggers a match works
whenever it arrives. One under `ignore:` only helps if it's there before your digest goes out,
which the daily default usually allows.

`my_lines` blames the changed lines at the PR's merge-base and honours Godot's
`.git-blame-ignore-revs`. It isn't affected by `ignore: paths`. PRs that only add code have
nothing to blame.

## How it works

`.github/workflows/watch.yml` runs hourly. It indexes PRs from the last 30 days (changed files, plus
blame through GitHub's GraphQL API, no clone), matches them against open `watch` issues and posts
due digests. `preview.yml` answers new and edited issues. State lives on the `data` branch, which
also serves the Pages site: the feeds, and a redirect page so Godot PRs don't get "mentioned this"
backlinks. A week after notifying you, it checks whether you took part in the PR, to see whether
this is useful.

Code is in `watcher/`. `match.py` (rules) and `index.py` (blame) are the interesting parts.

## Running your own

1. Copy this repo (e.g. "Use this template"). Don't fork: forks start with scheduled workflows disabled.
2. Set `target` in `config.yml`. If you change `label`, also change it in
   `.github/ISSUE_TEMPLATE/watch.yml` and `.github/workflows/preview.yml`.
3. Run the `watch` workflow by hand. That creates the `data` branch and the label. The 30-day
   backfill then takes a few hours of hourly runs (API rate limit).
4. Settings → Pages: deploy from the `data` branch, `/docs` folder.

GitHub disables scheduled workflows after 60 days without repo activity. If that happens,
re-enable `watch` in the Actions tab.

Locally:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m watcher run --data ./data          # index only
.venv/bin/python -m watcher preview --data ./data --rules my-rules.yml --login <you>  # try rules
.venv/bin/python -m unittest
```

## Background

Based on an analysis of ~1000 merged Godot PRs: blame and personal history predict who reviews a
PR better than CODEOWNERS teams. Written with help from an AI assistant; the bot's comments are
fixed templates, not generated text.
