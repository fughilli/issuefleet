# Build: a generic Linear → GitHub work-queue orchestrator

You are building a new, standalone tool from scratch. Read this whole brief
before you start, then post a plan.

---

## 1. Mission

Build a **generic, reusable service that drains a work queue from Linear into a
GitHub repository using a fleet of autonomous coding agents.**

The operator labels (or assigns) Linear issues. The service claims them, gives
each one an isolated agent with its own git worktree and branch, lets that agent
discuss its work in the issue's comment thread, opens a pull request when the
agent believes the issue is satisfied, feeds PR review back to the agent, and
tears the whole thing down when the PR merges.

It runs continuously in the background, unattended, and survives its own restarts.

**This is not a one-repo script.** It must work for any (Linear project → GitHub
repo) pair, several of them at once, configured declaratively. The specific
motivating case — a Linear project called "Splanc" feeding `fughilli/splanc` —
is one row in a config file, not an assumption baked into the code.

---

## 2. Where to build it

A new repository of its own. Nothing about this belongs inside the repos it
operates on: those repos should need *zero* modifications to be driven by it —
no committed entrypoint script, no vendored agent CLI. (An early sketch put the
worker entrypoint inside the target repo so worktrees would inherit it. That was
a mistake; see §5.4 for how to avoid it.)

Pick a name and confirm it with the operator before scaffolding — suggestions:
`workqueue`, `issuefleet`, `linear-fleet`.

Language: **Python 3.11+, standard library only** for the core. `tomllib` is
available; `urllib.request` is enough for both APIs. Do not add a dependency
without asking — a zero-install daemon is a real feature here. Dev-only
dependencies for tests/linting are fine.

---

## 3. The one architectural decision that is already made

**All credentials live host-side, in the orchestrator process. Agent containers
get none.**

Agents run with permission prompts disabled. Handing four such sandboxes a
Linear API key and a GitHub token is a bad trade for a marginally tighter
feedback loop. Instead:

- Each worker's only channel to the outside world is a **filesystem mailbox**: a
  directory of small JSON files inside its own worktree.
- The agent writes `status` / `question` / `ready` messages into an outbox.
- The orchestrator polls those and performs the credentialed act — posting a
  Linear comment, pushing the branch, opening or updating a PR.
- Inbound Linear comments and PR review comments are written into the agent's
  inbox and injected into its next turn.
- Agents *can* commit freely: a linked worktree's objects land in the shared
  `.git` of the main checkout, which is bind-mounted into the container. Nothing
  leaves the machine until the orchestrator pushes it.

Do not redesign this without arguing the case first. Everything else below is
open to your judgement.

---

## 4. Product requirements

### 4.1 Claiming work

- Poll Linear on an interval (default 60s) for issues in the configured
  project(s) that are open (state type not `completed`/`canceled`).
- **Claim rule is configurable**, with at least these strategies:
  - `label` — issue carries a given label (default; opt-in and easily revoked)
  - `assignee` — issue is assigned to a given Linear user (a bot account)
  - `state` — issue is in a given workflow state (e.g. "Ready for agent")
- Cap concurrency (default 4 workers). When the fleet is full, extra eligible
  issues just wait; order the queue by Linear priority then age.
- Claiming should be observable from Linear alone: move the issue to a
  configured state (e.g. "In Progress") and post a comment saying which branch
  and worktree it got, and how to watch it.
- **Un-claiming must work.** If the label is removed, the assignee changes, or
  the issue is closed while a worker holds it, wind that worker down cleanly.

### 4.2 Worker isolation

One issue ⇒ one branch ⇒ one git worktree ⇒ one container. Worktrees live
outside the target repo (configurable root, e.g. `~/worktrees/<repo>/<ISSUE>`).
Branch naming is configurable (default `agent/<issue-id>-<slug>`).

### 4.3 Conversation

The agent must be able to:

- post a **status** update (relayed as a Linear comment) — required at minimum
  when it first forms a plan, and on meaningful progress;
- **ask** a blocking question, then idle until a human replies on the issue;
- receive **replies** — new Linear comments, and PR review comments once a PR
  exists — as injected context on its next turn;
- signal **ready**, handing over a PR title and body.

Messages the orchestrator itself posts must never be re-ingested as inbound
messages (filter on the API user's identity *and* an HTML-comment marker in the
body — belt and braces, because the identity check breaks if someone reuses the
key).

### 4.4 Pull requests

- On `ready`: verify the branch actually has commits on top of the base ref,
  push it, then open a PR — or update the existing one if the agent is
  re-submitting after review. Use force-with-lease so a post-review rebase
  updates the PR without clobbering a concurrent push.
- Post the PR link back to the Linear issue.
- Poll open PRs. Merged ⇒ wind the worker down, move the issue to the configured
  done state, delete the branch (local, and remote if configured). Closed without
  merge ⇒ tell the agent so and let it respond, rather than silently dying.
- Review comments (issue comments, inline review comments, and review bodies)
  get forwarded into the inbox with enough context (file path, reviewer) to act
  on.

### 4.5 Teardown

Winding down means: signal the agent, archive its mailbox and transcripts
somewhere durable outside the worktree, stop the container, kill the session,
remove the worktree, prune, and drop the registry entry. The transcript must
outlive the branch — post-mortems are the whole reason to keep it.

### 4.6 Robustness

- **Restart-safe.** The daemon can be killed and restarted at any time and
  re-adopts the running fleet from a durable registry. Stopping the daemon must
  not stop the agents. Idempotent worktree/branch creation (adopt what exists
  rather than failing or clobbering).
- **One sick worker must not stall the fleet.** Per-worker exception isolation
  in the reconcile loop.
- **Relay is retryable.** A failed Linear/GitHub call leaves the outbox message
  pending for the next tick; it does not drop the message or double-post. Think
  about at-least-once delivery and make the dedupe explicit.
- **Crash handling.** Detect a dead worker session, restart it a bounded number
  of times, then report on the issue and leave the worktree intact for
  inspection rather than looping forever.
- **Runaway brake.** Bound how many self-driven turns an agent takes without
  human contact (default ~40); on exhaustion, post a status and idle rather than
  grinding. Consider also a wall-clock or token budget.

### 4.7 Operator surface

A single CLI. At minimum:

| Command | Does |
| --- | --- |
| `doctor` | verify tooling, credentials, API reachability, config; print exactly which issues *would* be claimed. Must be safe and side-effect-free. |
| `run` | the daemon |
| `once` | a single reconcile tick (cron/launchd-friendly) |
| `status` | fleet state: phase, turns, branch, PR, pending messages, liveness |
| `attach <ISSUE>` | how to watch a given worker live |
| `stop <ISSUE>` | wind one worker down by hand |
| `logs <ISSUE>` | tail that worker's output |

`doctor` is the highest-value command in the tool — an operator's first run
should tell them precisely what is missing. Invest in it.

Also provide a **dry-run mode**: reconcile and log every action it *would* take
without mutating Linear, GitHub, or the filesystem.

---

## 5. Environment facts (verified on the operator's machine — don't rediscover these)

Host: macOS (darwin 25.2), fish shell, Python 3.11.12 at `/opt/homebrew/bin/python3`
(`tomllib` present), `tmux` and `docker` on PATH, **`gh` is NOT installed**, SSH
keys present at `~/.ssh/id_ed25519` (git pushes over SSH work today).

### 5.1 `claude-container` (the agent runner)

Launcher at `~/.local/bin/claude-container`, v1.6.12; source repo at
`~/Projects/claude-container` (read its README — it is thorough).

- `claude-container -w <workspace> [-c <config-dir>] <command...>` — the first
  non-option argument and everything after it is the command run inside the
  container. **The launcher expands it unquoted (`$COMMAND`)**, so it word-splits:
  pass a space-separated command with no shell metacharacters and no arguments
  containing spaces. Anything complex must go through a script file.
- The workspace is always mounted at **`/workspace`**.
- **Linked git worktrees are supported first-class**: the launcher detects the
  `gitdir:` pointer file and additionally bind-mounts the main repo's `.git` at
  the path the pointer expects, so git works normally inside. One container per
  worktree is the documented, intended usage.
- Note the operator's own checkout may *itself* already be a linked worktree
  (`/Users/kevin/Projects/led_mapper_clanker` → common dir in
  `/Users/kevin/Projects/led_mapper/.git`). Nested worktree creation from a
  linked worktree works fine, but don't assume `<repo>/.git` is a directory.
- **Named services**: each container publishes one mux on an *ephemeral* host
  port, with a host-side router giving stable per-instance names. This is
  specifically designed so N concurrent worktree containers don't collide on
  ports. Declared in `.claude-container-overlay/overlay.json` in the target repo.
- **Permission prompts** are disabled by the launcher writing
  `{"permissions":{"defaultMode":"bypassPermissions"}}` into the config dir's
  `settings.json`. Headless turns depend on this — if it's ever missing, the
  first tool call needing approval hangs forever. `doctor` should check for it.
- The launcher always uses `docker run --rm -it`, so **it needs a pty on stdin**.
  You cannot simply background it. Two workable options:
  - run each worker inside a detached host **tmux** session (`tmux new-session -d`),
    which also gives the operator `tmux attach` to watch or take over a worker
    live — capture output with `tmux pipe-pane -o` rather than piping stdout, so
    the pty stays intact;
  - or bypass the launcher and call `docker run` yourself, replicating its mounts
    (`-v <ws>:/workspace`, `-v <config>:/claude`, `-e CLAUDE_CONFIG_DIR=/claude`,
    `-e USER_UID/USER_GID`, plus the worktree `.git` mount). More control, more
    to keep in sync with the launcher.
  Choose deliberately and write down why.
- Container names are `cc-<workspace-basename>-<sha256-12-of-path>-<pid>` — the
  pid makes them **unpredictable**, so to find a worker's container, list
  `docker ps -q` and match the `/workspace` mount source via `docker inspect`.
- The config dir (`~/.config/claude-container/config`, holding Claude
  credentials) is **shared** by all containers by default. That is the launcher's
  expected model for one-container-per-worktree; per-worker copies risk OAuth
  refresh-token races. Default to sharing; make it configurable.
- `.claude-container-overlay/` in the target repo defines a per-project image
  layer (Dockerfile fragment) and port/service config. The target repo in
  question installs Nix, pre-commit, sudo, and `git config --system safe.directory`.
  Your workers inherit all of that for free — don't duplicate it.

### 5.2 Claude Code headless mode (the agent itself)

- `claude -p` reads the prompt from **stdin** when no prompt argument is given —
  use this; it sidesteps all argv quoting and length limits.
- `--session-id <uuid>` sets the session id on the first turn; `--resume <uuid>`
  continues it. Pin a UUID per worker at creation and you get a single coherent
  conversation across many turns, surviving orchestrator restarts.
- `--output-format json` (or `stream-json`) gives a parseable result envelope.
  `stream-json` + `--include-partial-messages` if you want live progress.
- Other flags worth knowing: `--append-system-prompt`, `--permission-mode`,
  `--allowedTools`, `--add-dir`, `--mcp-config` / `--strict-mcp-config`,
  `--agents`, `--max-turns`, `--fork-session`.
- A "turn loop" driving repeated `claude -p --resume` calls works well: keep the
  *decision* of what the next turn should be (or whether to idle, or exit) in one
  testable place, and make the shell loop a dumb consumer of its exit code.

### 5.3 APIs

**Linear** — GraphQL at `https://api.linear.app/graphql`. A *personal API key*
(https://linear.app/settings/api) goes in the `Authorization` header **raw, with
no `Bearer` prefix**. Operations you'll need: `viewer`, `project.issues` (filter
`state: {type: {nin: ["completed","canceled"]}}`, paginate), `issue.comments`,
`commentCreate`, `team.states`, `issueUpdate(stateId:)`. The workspace in
question is `fughilli`; the motivating project is `Splanc`
(`3dabd3ad-7ff0-4b90-a10f-2c5413ed6240`), team prefix `FUG`, six open issues,
currently no `agent` label — the operator will create one. **Don't hardcode any
of that.**

Note: an MCP Linear server exists and is OAuth-authenticated in interactive
sessions, but it is unsuitable here — a headless daemon needs a key it owns.

**GitHub** — REST v3 over `urllib` with a fine-grained PAT (*Contents: RW*,
*Pull requests: RW*). Resolve the token from env (`GITHUB_TOKEN`/`GH_TOKEN`), a
file under `~/.config/<tool>/`, and — if present — `gh auth token`, in that
order. Pushing should use the existing SSH remote, not the token. Parse
`owner/name` from `git remote get-url origin` handling both SSH and HTTPS forms.
Endpoints: `GET/POST /repos/{slug}/pulls`, `PATCH /repos/{slug}/pulls/{n}`,
`GET .../pulls/{n}/comments`, `.../issues/{n}/comments`, `.../pulls/{n}/reviews`.

Secrets never go in the config file — env var or a `chmod 600` file only.

### 5.4 Making it repo-agnostic

The in-container worker needs an entrypoint and a small CLI for its mailbox
verbs. It must **not** be committed into every target repo. Options, in rough
order of preference:

1. Stage the runtime into the worktree at worker-creation time (e.g.
   `<worktree>/.agent/bin/`), alongside the mailbox — already gitignored, and
   trivially version-matched to the orchestrator.
2. Mount the tool into the container as a second volume (needs bypassing or
   extending the launcher, which only mounts workspace + config).
3. Bake it into an image layer.

Whichever you pick: the target repo must stay untouched, and you must add
`.agent/` to git's exclusion **without editing the repo's `.gitignore`** — write
to `.git/worktrees/<name>/info/exclude`, which is per-worktree and invisible to
the repo. (Confirm the path resolves correctly for the nested-worktree case.)

---

## 6. Testing (not optional)

The failure modes here are all in the seams, and the expensive ones only show up
after a container has been running for twenty minutes. So:

- **The turn-decision logic must be unit-testable with no container, no network,
  and no credentials.** Drive it with a temp directory as a fake workspace and
  assert the exit-code control flow: first turn emits the full brief; a reply in
  the inbox resumes a paused agent; asking a question idles; the auto-turn budget
  trips; shutdown exits. This was already prototyped and it catches real bugs
  cheaply.
- **Fake Linear and GitHub clients** behind the same interface as the real ones,
  so the whole reconcile loop can be tested offline: claim → status relay →
  ready → PR → merge → teardown, plus un-claim, crash-restart, and
  retry-after-API-failure.
- At least one **end-to-end smoke test** an operator can run by hand against a
  throwaway repo and a scratch Linear project, documented step by step.
- Concurrency sanity: four workers, four worktrees, no port/name/lock collisions.

---

## 7. Documentation

A README that a competent operator can follow cold:

- the architecture diagram and the credential-boundary rationale (§3) — lead with
  this, it's the design's load-bearing idea;
- setup: creating the two credentials, the Linear label, the config file;
- the config schema, every key explained, with a worked multi-project example;
- the worker lifecycle as a state table;
- how to watch, steer, and stop a worker;
- how to run the daemon persistently on macOS (launchd plist) and Linux (systemd
  unit) — ship both;
- **known edges, honestly**: agents branch off the base ref at claim time and
  aren't auto-rebased, so overlapping issues produce conflicting PRs; merge
  detection lags by the poll interval; and running N agents continuously has real
  cost, with the turn budget as the only brake.

---

## 8. Explicit non-goals

- Don't build a web UI or dashboard. Linear *is* the UI; the CLI covers the rest.
- Don't auto-merge PRs. A human merges; the service reacts to the merge.
- Don't invent a general plugin system for arbitrary trackers. Keep the tracker
  and forge behind narrow interfaces so GitLab/Jira *could* be added, and stop
  there.
- Don't touch the target repositories' contents, history, or config.

---

## 9. How to proceed

1. Read this brief, look at `claude-container`'s README and launcher script
   (`~/Projects/claude-container`, `~/.local/bin/claude-container`) to confirm
   §5.1 for yourself, and check the Linear/GitHub API shapes you'll depend on.
2. **Post a plan before writing code**: your module layout, the mailbox contract,
   the config schema, and — specifically — how you're solving §5.4
   (repo-agnostic runtime) and §5.2 (pty/backgrounding). Flag anything in this
   brief you think is wrong; it was written from one afternoon's investigation,
   not from operating experience.
3. Build the offline-testable core first (mailbox contract + turn decisions +
   reconcile loop against fake clients). Get those tests green before anything
   touches Docker.
4. Then the real clients, then the container runner, then `doctor`, then the
   daemon.
5. Prove it end-to-end on a throwaway repo and a scratch Linear project before
   pointing it at anything real.
6. Report honestly at the end: what you verified by running it, what you only
   unit-tested, and what remains unproven.

### Prior art

An afternoon's throwaway prototype of roughly this design exists at
`/private/tmp/claude-501/-Users-kevin-Projects-led-mapper-clanker/303c1b60-c1e0-425b-82fe-7f457e183ba2/scratchpad/prior_art/`
(host path; ephemeral — copy it out if you want it, and ask the operator to mount
it if you're containerized). It contains a working mailbox contract, a turn loop
whose control flow was tested and passing, a Linear/GitHub client pair, and a
worker prompt template.

It is a **sketch, not a specification**: it is hardcoded to one repo, commits its
worker entrypoint into the target repo (§5.4 says don't), has no tests beyond the
turn-loop script, and never ran against a live container. Mine it for the
contracts and the API details; don't inherit its structure.
