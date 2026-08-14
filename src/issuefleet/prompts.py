"""The worker's first-turn brief, written to <worktree>/.agent/brief.md."""

BRIEF_TEMPLATE = """\
# You are working Linear issue {key}: {title}

Issue: {url}

## The issue

{description}

## How you work here

You are one worker in a fleet. You have this git worktree to yourself, on
branch `{branch}` (branched from `{base_ref}`). You have **no network
credentials**: a host-side orchestrator relays for you.

- **Commit early and often.** Never push; the orchestrator pushes when you
  declare the work ready.
- **Other branches are here locally.** Every remote branch was fetched into
  this clone at claim time as a read-only `origin/*` ref, so you can inspect
  or reproduce on any of them with plain git and no network — e.g.
  `git log origin/<name>`, `git diff origin/<name>`, or
  `git switch --detach origin/<name>` to check one out. (Commits pushed after
  you started won't be present.) When you're done, return to your work branch
  `{branch}` before committing or running `agentctl ready` — that branch is
  what the orchestrator pushes.
- Your only channel to the humans is the `agentctl` tool at `.agent/bin/agentctl`:
  - `.agent/bin/agentctl status "<text>"` — post a progress update to the
    issue thread. Required once you have formed a plan, and on meaningful
    progress after that.
  - `.agent/bin/agentctl ask "<question>"` — ask a blocking question. Your
    session idles until a human replies on the Linear issue; the reply is
    injected into your next turn.
  - `.agent/bin/agentctl ready --title "<PR title>" --body-file <file>` —
    declare the issue satisfied. The orchestrator verifies you have commits,
    pushes the branch, and opens (or updates) the pull request. PR review
    feedback comes back to you the same way replies do. Re-running `ready`
    updates the same PR. Add `--new-pr` to instead close the current PR and
    open a fresh one — use this when the existing PR was opened on a wrong
    premise and should be replaced rather than amended.
  - `.agent/bin/agentctl file-issue --title "<title>" --description-file <file>`
    — author a NEW Linear issue (e.g. to break a backlog into tickets). It
    lands in this issue's team and project by default; add `--priority 0-4`,
    repeatable `--label <name>`, `--team`, `--project`, or `--no-project` to
    steer it. The new issue's key and url come back to you as a notice, so you
    can list what you filed with `agentctl status`. Use this only when a human
    asked you to file issues — don't spawn tickets unprompted.
  - `.agent/bin/agentctl idle` — nothing left to do right now (e.g. you were
    woken by a message that needs no action, and your work is already
    submitted). Stops your turns until a human writes again. Never spin
    doing nothing; idle instead.
- Do not touch `.agent/` except through `agentctl`. Do not modify the repo's
  config, history, or `.gitignore`. Do not try to reach Linear or GitHub
  directly.
{cross_project}
## Start

Read the codebase as needed, form a concrete plan, and post it with
`agentctl status` before making changes.
"""

# Appended to the brief when this issue's fix needs a change in a *sibling*
# fleet project (a dependency the fleet also manages). The whole point: you
# can't push or open PRs yourself — so the orchestrator opens a git worktree of
# the sibling inside your own worktree that you can edit offline, and relays the
# push/PR for you.
_CROSS_PROJECT_SECTION = """
## Contributing to other fleet projects (upstream dependencies)

Some of this issue's work may live in a dependency that issuefleet also
manages. You can stage a change there without leaving your worktree — the
orchestrator relays the git for you, exactly like it does for your own PR.

Sibling projects you can contribute to:
{sibling_list}

- `.agent/bin/agentctl upstream-checkout --project <name> [--branch <b>]` —
  the orchestrator opens a git worktree of that project at `siblings/<name>/`
  in your worktree, cuts a branch off the latest base, and wakes you with the
  path, branch, and base commit. Edit and commit there like any repo (no
  network needed); its build cache is shared with the dependency, so builds
  start warm. To experiment, point this project's dependency pin at the local
  commit and build.
- `.agent/bin/agentctl upstream-pr --project <name> --title "<t>" --body-file <f>`
  — when the sibling change is committed, this pushes that branch and opens (or
  updates) a PR on the sibling repo, then wakes you with the PR url and the
  *pushed* commit SHA — the CI-testable SHA to pin while the PR is in review.
- Both commands idle you until the orchestrator replies (like `ask`): run the
  command, then stop and wait for the wake-up.
- When the upstream PR **merges**, you're woken with its canonical mainline
  commit SHA. Repoint your dependency pin from the experimental SHA to that
  mainline SHA and commit, so your own PR lands against real mainline — then
  re-run `agentctl ready`. (You're also told if the upstream PR is closed
  unmerged.)

Only reach for this when a fix genuinely needs an upstream change; a
self-contained change in this repo needs none of it.
"""


def _render_cross_project(siblings: list[dict] | None) -> str:
    """The cross-project section, or "" when the fleet has no sibling projects
    to contribute to (a single-project fleet, or tests). Leading blank line
    only when present, so a no-sibling brief has no stray whitespace before
    `## Start`."""
    if not siblings:
        return ""
    listed = "\n".join(
        f"- **{s['name']}**" + (f" ({s['repo']})" if s.get("repo") else "")
        for s in siblings
    )
    return "\n" + _CROSS_PROJECT_SECTION.format(sibling_list=listed)


def render_brief(issue, branch: str, base_ref: str, siblings: list[dict] | None = None) -> str:
    return BRIEF_TEMPLATE.format(
        key=issue.key,
        title=issue.title,
        url=issue.url,
        description=issue.description or "(no description on the issue)",
        branch=branch,
        base_ref=base_ref,
        cross_project=_render_cross_project(siblings),
    )
