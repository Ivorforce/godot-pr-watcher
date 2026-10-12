# godot-pr-watcher

Get notified about new [Godot](https://github.com/godotengine/godot) PRs that touch paths you pick,
or lines you wrote or approved. Nothing is posted to the Godot repo.

Anyone can subscribe: you don't need to be a maintainer or have any role in Godot. This is an
unofficial community tool, not run by the Godot project.

## Usage

[Open a "Watch pull requests" issue](../../issues/new/choose) to subscribe.

The default rules will let you know when somebody changes code you wrote or reviewed,
which should be useful for most users. But you can also edit the rules to your needs.

- First, a bot will reply to let you know your watcher is in effect.
- Then you'll get updates about new PRs in digests, as new comments.
- Edit the issue to change rules, close it to stop.

Rules are public. Max 3 open subscriptions per person (no limit for collaborators on this repo).

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
  authors: [someone]
  labels: [topic:gdscript]
  milestones: ["3.7"]
  branches: [3.x]
ignore:                    # files, or PRs, to leave out; same keys except the line counts
  paths: ["**/tests/"]
  labels: [documentation]
include: [drafts, own, other_branches, involved]  # all off by default
digest: daily              # hourly, daily (default) or weekly
```

`other_branches` means PRs to branches other than `master` (e.g. cherry-picks); you get those
anyway when they match through `branches` or `milestones`. `involved` keeps PRs where you're
already a requested reviewer, assignee, commenter or reviewer, or @-mentioned in the description;
GitHub notifies you about those already, so digests leave them out by default.

You hear about each PR once, and only about PRs that became ready for review after you subscribed.
The preview points out path patterns that match no file in the repository, and labels no recent PR had.

Labels and milestones are often added hours after a PR opens. One that triggers a match works
whenever it arrives. One under `ignore:` only helps if it's there before your digest goes out,
which the daily default usually allows.

`teams` uses Godot's [CODEOWNERS](https://github.com/godotengine/godot/blob/master/.github/CODEOWNERS)
file, so you can follow a team's areas without being on the team. Team members can use it instead
of team review requests: one digest line per PR rather than every later comment and push. The
preview lists the valid team names if you misspell one.

`my_lines` blames the changed lines at the PR's merge-base and honours Godot's
`.git-blame-ignore-revs`. It isn't affected by `ignore: paths`. PRs that only add code have
nothing to blame. If `my_lines` is too noisy, e.g. because you approve large PRs you only partly
review, try `most_lines` (fewer, more relevant matches) or `my_written_lines`. Roughly:

| Rule | Notifications you end up reviewing | PRs you review that you hear about |
|---|---|---|
| `my_lines: [20%, 40]` (default) | ~1 in 4 | ~45% |
| `my_lines: 1` | ~1 in 6 | ~53% |
| `most_lines: true` | ~1 in 3 | ~33% |
| `my_written_lines: [20%, 40]` | ~1 in 3 | ~18% |
| *For comparison: CODEOWNERS team requests* | *~1 in 10* | *~70%* |

Measured on ~1000 past Godot PRs that changed existing code. Interest is more than reviewing, so
compare rows rather than reading the numbers literally. Team requests also subscribe you to every
later comment and push.

## How it works

`.github/workflows/watch.yml` runs hourly. It indexes PRs from the last 30 days (changed files, plus
blame through GitHub's GraphQL API, no clone), matches them against open `watch` issues and posts
due digests. `preview.yml` answers new and edited issues; run it by hand to refresh one preview, or all of them
after changing the watcher. State lives on the `data` branch, which
also serves the Pages site: the feeds, and a redirect page so Godot PRs don't get "mentioned this"
backlinks. A week after notifying you, it checks whether you took part in the PR, to see whether
this is useful.

Code is in `watcher/`. `match.py` (rules) and `index.py` (blame) are the interesting parts.

## Running your own

> [!NOTE]
> If you want to watch Godot, subscribe by opening an issue.
> You only need to copy this repo if you want to set it up to watch another repository.

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

Based on an analysis of ~1000 merged Godot PRs: blame and personal history predict who reviews a
PR better than CODEOWNERS teams. Written with help from an AI assistant. The tool itself does not
involve any AI as it runs.
