# WORKLOG

_Last updated: 2026-07-29 by an agent session. Read together with `git log`._

## Goal

Build **issuefleet** per `AGENT_BUILD_PROMPT.md`: a generic, restart-safe daemon
that drains Linear issues into GitHub PRs using a fleet of containerized coding
agents, one worktree+branch+container per issue, with all credentials held
host-side and a filesystem mailbox as the agents' only channel out.

Name note: the brief asks to confirm the name with the operator; this session
ran unattended and picked `issuefleet` from the brief's suggestion list — a
trivial rename if the operator objects.

## Plan of record

1. Scaffold: Bazel 8 bzlmod (hermetic Python 3.11, stdlib-only runtime, no pip
   graph), Nix devshell flake (registration-only `rules_nixpkgs_core`), this
   worklog.
2. Offline core, tests-first, no network/docker: `config`, `model`, `mailbox`,
   `registry`, then `turns` (pure decision logic), then `reconcile` against
   fake Tracker/Forge/Runner interfaces.
3. Real clients: `linear` (GraphQL, raw auth header), `github` (REST, PAT).
4. `gitops` (idempotent worktree/branch), `runner` (detached host tmux +
   claude-container; chosen over hand-rolled `docker run` because the launcher
   handles linked-worktree `.git` mounts first-class and duplicating its mount
   logic is a drift risk), agent runtime staged into `<worktree>/.agent/bin/`
   with per-worktree `info/exclude` (§5.4 option 1).
5. `doctor`, `cli` (doctor/run/once/status/attach/stop/logs, `--dry-run`).
6. README (credential boundary first), launchd + systemd units, smoke-test doc.

Key contracts (decided; argue in a commit if changing):
- Mailbox: `<worktree>/.agent/mailbox/{inbox,outbox,archive}/`, one JSON file
  per message, monotonic sequence in filename, atomic write via tmp+rename.
  Outbox kinds: `status`, `question`, `ready`. Inbox kinds: `reply`,
  `pr_review`, `pr_closed`, `shutdown`, `unclaimed`.
- Relay dedupe: every relayed message id embedded as `<!-- issuefleet:msg:<id> -->`
  in the posted body; relay checks recent comments for the marker before
  posting (at-least-once + explicit dedupe). Inbound filtered on viewer id AND
  marker.
- Turn loop exit codes (shell loop is a dumb consumer): 0=take another turn,
  10=idle (question pending / waiting for input), 20=idle (ready submitted),
  30=shutdown, 40=auto-turn budget exhausted.

## State of play

- Container caveat: this build environment has **no docker, no nix binary**,
  and the operator-host prior-art scratchpad is not mounted. Everything
  Docker/live-API-touching can only be unit-tested here; e2e smoke is a
  documented manual procedure for the operator's Mac.
- Scaffold in progress (this commit).

## Next up

1. Task #2: config/model/mailbox/registry with `bazel test` green.
2. Then turns, then reconcile (see Plan of record).

## Open questions / blockers

- Name `issuefleet` unconfirmed by operator (see Goal note).
- `flake.lock` cannot be generated here (no nix); run `nix flake lock` on the
  host and commit it.

## Don't retry (dead ends)

- Bazel 7.x pin with rules_python 2.x — py_binary bootstrap breaks
  (`%interpreter_args%` left literal). Pin stays 8.x + `bootstrap_impl=script`.
- Committing the worker entrypoint into target repos — explicitly ruled out by
  the brief (§2, §5.4).
