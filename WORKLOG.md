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

1. Smoke test (operator's Mac, 2026-07-29): **happy path verified live
   through PR creation** — claim → provision → container → first turn →
   plan status relayed as Linear comment → commit → `agentctl ready` →
   branch pushed → PR opened → link posted back to the issue (SMOKE_TEST
   steps 1–4 essentially done; driven by manual `once` ticks, so relays
   only happened on tick — expected, not a bug). Still unverified live:
   PR review feedback forwarding + re-submission, merge → teardown/archive
   (step 5), the robustness spot-checks (step 6), and the long-running
   `run` daemon itself. The skill-approval wedge was
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
2. NEW 2026-07-29: webhooks + bot identities shipped (offline-tested only):
   - `[webhooks]` listener (loopback + tunnel) wakes the reconcile loop on
     verified GitHub/Linear events — no more waiting out the poll interval.
     Operator setup: tunnel (Cloudflare/Tailscale), repo webhook w/ secret,
     Linear webhook w/ signing secret; see README "Webhooks".
   - GitHub identity 2026-07-30: **GitHub App auth** (preferred over the
     machine-user PAT, which remains the fallback). RS256 app JWTs signed
     via the openssl CLI (stdlib can't do RSA; real sign→verify roundtrip
     in tests), installation tokens cached per owner with 5-min refresh
     margin, forge accepts a callable token source. Operator setup in
     README "Bot identities". Unproven live like the rest of this batch —
     first `doctor` run with the app configured probes /app +
     /app/installations and will surface any mismatch.
     Also: `issuefleet github-app-setup` (manifest flow) — **live-verified
     2026-07-30**: created https://github.com/apps/issuefleet (app id
     4440229, webhook-less variant), private key written. Remaining for
     app auth: operator installs the app on target repos, sets
     github_app_id in config, `doctor` (first live probe of /app +
     /app/installations + JWT signing). Because the app was created
     without a webhook, NO github webhook secret exists yet — when adding
     the webhook later in the app's settings, also set a secret there and
     write it to [webhooks] github_secret_file, or the endpoint stays
     disabled. The classic PAT in credentials/ (gitignored) remains the
     token-mode fallback until then.
   - Linear agents platform: `issuefleet linear-oauth` (actor=app install,
     no seat), Bearer auth auto-detected via lin_oauth_ prefix,
     delegation/@-mention claims via AgentSessionEvent webhooks (10s ack
     from the webhook thread), status/question/ready → thought/elicitation/
     response activities, prompted → worker inbox. **Everything
     agent-platform is unproven live** — implemented from Linear docs
     (linear.app/developers/agents + /agent-interaction); verify the
     payload shapes on first real install, especially AgentSessionEvent
     field names and agentActivityCreate input.
3. `nix flake lock` on the Mac (no nix in this container); commit flake.lock.
4. Finish `docs/SMOKE_TEST.md` (review-forward, merge teardown, robustness
   checks, `run` daemon); then the webhook/agent live test; then Splanc.

## Open questions / blockers

- Tool name confirmation (see deviations). Note: the operator's checkout
  dir is `~/Projects/linear_dispatch`, which may be the preferred name.
- Homelab compose stack (deploy/docker/, added 2026-07-30) is authored but
  entirely unrun: riskiest seams are claude-container-inside-a-container
  (root USER_UID → root-owned worktree files) and the tmux-in-daemon-
  container caveat (daemon restart kills worker sessions; crash-restart
  path recovers). Also live-green as of 2026-07-30: GitHub App doctor chain AND the
  linear-oauth actor=app install (doctor authenticates as the 'issuefleet'
  app user, Bearer). Still untested live: the webhook path end-to-end
  (tunnel -> signature verify -> AgentSessionEvent -> claim/activities).
- `deploy/*.plist|.service` contain operator-specific paths to edit.

## Don't retry (dead ends)

- Bazel 7.x with rules_python 2.x — py_binary bootstrap breaks; stay on
  8.x + `bootstrap_impl=script`.
- Per-worktree `info/exclude` — git ignores it entirely; common-dir only.
- Committing worker entrypoints into target repos — ruled out by the brief.
