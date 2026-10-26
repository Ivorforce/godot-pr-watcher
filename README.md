# godot-pr-watcher

Get notified about new [Godot](https://github.com/godotengine/godot) PRs that touch paths you pick,
or lines you wrote or approved. Nothing is posted to the Godot repo.

Anyone can subscribe: you don't need to be a maintainer or have any role in Godot. This is an
unofficial community tool, not run by the Godot project.

## Usage

[Open a "Watch pull requests" issue](../../issues/new/choose) to subscribe.

The default rules will let you know when somebody changes code you wrote or approved,
which should be useful for most users. But you can also edit the rules to your needs.

- First, a bot will reply to let you know your watcher is in effect.
- Then you'll get updates about new PRs in digests, as new comments. PRs that are later merged
  or closed are struck through.
- Edit the issue to change rules, close it to stop.

Since watchers are issues, rules are public. Max 3 issues per person.

## Rules

```yaml
match:                     # notify if any of these match
  paths:                   # from the repo root; "**/" matches anywhere
    - modules/gdscript/
    - "**/*.glsl"          # quote patterns that start with *
  teams: [core]            # files a team owns in Godot's CODEOWNERS
  my_lines: [20%, 40]      # ≥20% or ≥40 of the changed lines are yours or from PRs you approved
  my_written_lines: 20%    # same, but only lines you wrote
  most_lines: true         # you wrote or approved more of the changed lines than anyone else
  lines_by:                # like my_lines, for someone else's lines
    someone: [20%, 40]
  written_lines_by: {someone: 20%}  # like my_written_lines
  authors: [someone]
  labels: [topic:gdscript]
  milestones: ["3.7"]
  branches: [3.x]
  all:                     # entries whose keys must all match
    - authors: [someone]
      teams: [gdscript]    # someone's PRs, but only those touching GDScript
ignore:                    # files, or PRs, to leave out; same keys except the line counts
  paths: ["**/tests/"]
  labels: [documentation]
  all:
    - authors: [someone]
      paths: ["**/tests/"] # someone's PRs that touch tests
include: [drafts, own, other_branches, involved]  # all off by default
digest: daily              # hourly, daily (default) or weekly
```

A PR matches if anything under `match:` matches and nothing under `ignore:` does. Lists mean
"any of these".

- Under `ignore:`, `paths` and `teams` leave out single files; inside `all:` they leave out the whole PR.
- `other_branches`: PRs to branches other than `master` (e.g. cherry-picks). Those matching
  `branches` or `milestones` come through anyway.
- `involved`: PRs where you're already a requested reviewer, assignee, commenter or reviewer, or
  @-mentioned in the description. GitHub notifies you about those already.
- You hear about each PR once, and only about PRs that became ready for review after you subscribed.
- Labels and milestones are often added hours after a PR opens. Under `match:` they work whenever
  they arrive; under `ignore:` only if they're there before your digest goes out, which the daily
  default usually allows.

### Picking a line rule

If `my_lines` is too noisy, e.g. because you approve large PRs you only partly review, try
`most_lines` (fewer, more relevant matches) or `my_written_lines`. Measured on ~1000 past Godot PRs
that changed existing code:

| Rule | Notifications you end up reviewing | PRs you review that you hear about |
|---|---|---|
| `my_lines: [20%, 40]` (default) | ~1 in 4 | ~45% |
| `my_lines: 1` | ~1 in 6 | ~53% |
| `most_lines: true` | ~1 in 3 | ~33% |
| `my_written_lines: [20%, 40]` | ~1 in 3 | ~18% |
| *For comparison: CODEOWNERS team requests* | *~1 in 10* | *~70%* |

Interest is more than reviewing, so compare rows rather than reading the numbers literally. Team
requests also send you every later comment and push; `teams` gives you one digest line per PR.

## How it works

`.github/workflows/watch.yml` runs hourly. It indexes PRs from the last 30 days (changed files, plus
blame through GitHub's GraphQL API, no clone), matches them against open `watch` issues and posts
due digests. `preview.yml` answers new and edited issues; run it by hand to refresh one preview, or all of them
after changing the watcher. State lives on the `data` branch, which
also serves the Pages site: the feeds, and a redirect page so Godot PRs don't get "mentioned this"
backlinks. A week after notifying you, it checks whether you took part in the PR, and counts
this per rule to tune the defaults.

Line rules blame the changed lines at the PR's merge-base, honour Godot's
`.git-blame-ignore-revs` and count all files, even those under `ignore: paths`. PRs that only add
code have nothing to blame.

Code is in `watcher/`. `match.py` (rules) and `index.py` (blame) are the interesting parts.

## Running your own

Only needed to watch a repository other than Godot.

1. Copy this repo (e.g. "Use this template"). Don't fork: forks start with scheduled workflows disabled.
2. Set `target` in `config.yml`. If you change `label`, also change it in
   `.github/ISSUE_TEMPLATE/watch.yml` and `.github/workflows/preview.yml`.
3. Run the `watch` workflow by hand with `budget` 600. That creates the `data` branch and the label,
   and indexes the last 30 days (about 5 minutes for Godot).
4. Settings → Pages: deploy from the `data` branch, `/docs` folder.

GitHub disables scheduled workflows after 60 days without repo activity. While anyone is
subscribed, each run resets that timer; otherwise re-enable `watch` in the Actions tab.

Locally:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m watcher run --data ./data          # index only
.venv/bin/python -m watcher preview --data ./data --rules my-rules.yml --login <you>  # try rules
.venv/bin/python -m unittest discover tests
```

## Background

Written with help from an AI assistant. The tool itself does not involve any AI as it runs.
