# WORKLOG

_Last updated: 2026-07-31 by an agent session. Read together with `git log`._

_Follow-up / backlog work is now tracked on the Linear issue board, not here.
FUG-14 converted the former "Next up" / "Open questions" backlog into tickets
(FUG-16…FUG-23); future follow-ups go straight to the board. This log keeps
the project narrative, the verification record, and the dead-ends reference
only._

## Goal

**issuefleet** (this repo): a generic daemon draining Linear issues into
GitHub PRs via containerized agent workers, per `AGENT_BUILD_PROMPT.md`.
Credentials host-side only; filesystem mailbox as the agents' sole channel.

## State of play

**MILESTONE 2026-07-31: full lifecycle verified live end-to-end** on the
operator's Mac via the Linear agent platform — new issue delegated to the
bot → webhook session claim → worker → PR (as issuefleet[bot]) → merge →
clean teardown. Both echo loops fixed and confirmed gone. New claim
strategy `agent` (7166658+) disables label/poll triggering per the
operator's preference: delegation/@-mention is the only claim gesture.
2026-07-31 late: `agentctl idle` verb + two-noop-turn auto-idle backstop
(cb5e216) after FUG-13 forensics — a finished agent woken by a courtesy
comment had no way to decline further turns. Agent-side change: recycle
live workers. 2026-07-31: bazel targets for the homelab stack
(//deploy/docker:image|up|down — docker-CLI wrappers, deliberately not
rules_oci: the apt layer isn't hermetically expressible without
rules_distroless) + GitHub Actions CI (test with in-tree bazel caches;
multi-arch image pushed to ghcr.io/fughilli/issuefleet on main).

The full system is built and committed — core, agent runtime, real
Linear/GitHub clients, gitops, tmux runner, doctor/CLI, docs, deploy units.
`bazelisk test //tests:all` = 8 targets / ~80 tests green. See `git log`
for the per-layer details; README.md documents the architecture.

**Verified by running here (Linux container, no docker/nix/credentials):**
- everything offline: mailbox, turn decisions, reconcile lifecycle (claim →
  relay → ready → PR → review → merge → teardown, un-claim, crash-restart,
  retry-after-outage, dedupe, capacity), client request construction;
- real-git integration (worktrees, exclude, force-with-lease push against a
  local bare origin) and real-tmux start/alive/stop with a stub container;
- `bin/issuefleet doctor` / `status` / `--help` live (doctor correctly
  flags this container's missing docker/claude-container/credentials).

**Unproven (needs the operator's Mac):** anything touching the real
claude-container launcher, live Linear/GitHub APIs, and the full
end-to-end flow — `docs/SMOKE_TEST.md` is the step-by-step procedure.

**Deviations from the brief, deliberate:**
- §5.4 exclusion path: git does not read `.git/worktrees/<name>/info/exclude`
  (verified on git 2.43) — using `$GIT_COMMON_DIR/info/exclude` instead.
  Flagged in README "Known edges".
- Dry-run is implemented as `Reconciler.plan()` (API reads, zero writes)
  rather than no-op client wrappers — simpler and honestly side-effect-free.

## Recent additions

- **FUG-13 — bot authors Linear issues** (branch `agent/fug-13-…`): new
  `agentctl file-issue` outbox verb → `Reconciler._handle_file_issue` →
  `LinearTracker.create_issue` (`issueCreate`). New tickets inherit the
  delegated issue's team & project by default (`--team`/`--project`/
  `--no-project`/`--priority`/`--label` to steer); the new key/url is sent
  back to the worker as an `info` notice so it can summarize. Deduped by a
  marker embedded in the new issue's description (`find_issue_by_marker`,
  best-effort — degrades to at-least-once if the backend rejects the content
  filter). Offline-tested (mailbox/clients/reconcile/fakes).

## Don't retry (dead ends)

- Bazel 7.x with rules_python 2.x — py_binary bootstrap breaks; stay on
  8.x + `bootstrap_impl=script`.
- Per-worktree `info/exclude` — git ignores it entirely; common-dir only.
- Committing worker entrypoints into target repos — ruled out by the brief.
