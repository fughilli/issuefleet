# WORKLOG

_Last updated: 2026-07-29 by an agent session. Read together with `git log`._

## Goal

**issuefleet** (this repo): a generic daemon draining Linear issues into
GitHub PRs via containerized agent workers, per `AGENT_BUILD_PROMPT.md`.
Credentials host-side only; filesystem mailbox as the agents' sole channel.

## State of play

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
- Name `issuefleet` self-selected from the brief's suggestion list (session
  ran unattended); trivial rename if the operator objects.
- Dry-run is implemented as `Reconciler.plan()` (API reads, zero writes)
  rather than no-op client wrappers — simpler and honestly side-effect-free.

## Next up

1. Resume the smoke test on the operator's Mac. Dispatch works live
   (claiming, provisioning, tmux runner, container launch). Observability
   fix 2026-07-29: turns now stream (`stream-json` summarized live into the
   tmux pane; raw per-turn `.jsonl` kept) — the earlier buffered-`json`
   design showed nothing until a turn finished and read as a hang. The
   in-flight worker predates this: recycle it (`issuefleet stop <KEY>` then
   `once`) and watch `issuefleet logs <KEY> -f`. NOTE: `claude -p
   --output-format stream-json --verbose` is assumed per the brief §5.2;
   if the container's claude CLI rejects that combo, check
   `.agent/logs/turn-0001.jsonl` + pane for the CLI's error text. The skill-approval wedge was
   root-caused: the launcher keyed skill choices per workspace *path* in
   its user config dir, so every worktree re-prompted — the operator fixed
   claude-container itself (choices now keyed on the resolved main working
   tree via skill_identity_dir(), shared by all worktrees; new
   --skills-ignore-new launch flag). issuefleet side (2026-07-29): runner
   passes `[agent] launcher_args` (default ["--skills-ignore-new"]) before
   the in-container command; doctor probes `--help` to confirm the
   installed launcher knows each flag. Requires launcher > 1.6.12.
   (`copy_from_repo` stays, but only for untracked workspace state like
   .claude/settings.local.json — it never held skill approval.)
2. `nix flake lock` on the Mac (no nix in this container); commit flake.lock.
3. Finish `docs/SMOKE_TEST.md`; record results here. Then Splanc.

## Open questions / blockers

- Tool name confirmation (see deviations).
- `deploy/*.plist|.service` contain operator-specific paths to edit.

## Don't retry (dead ends)

- Bazel 7.x with rules_python 2.x — py_binary bootstrap breaks; stay on
  8.x + `bootstrap_impl=script`.
- Per-worktree `info/exclude` — git ignores it entirely; common-dir only.
- Committing worker entrypoints into target repos — ruled out by the brief.
