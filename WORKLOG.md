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

- **FUG-32 — outer deploy loop** (branch `agent/fug-32-…`):
  `deploy/docker/watch.sh` (+ `//deploy/docker:watch`,
  `deploy/issuefleet-watch.service`) supervises the compose stack and keeps
  it on the newest build of `main` with no human in the loop. IMAGE mode
  (default): `docker compose pull issuefleet`, and if `:latest` resolves to a
  new image id, recreate the daemon onto exactly the image CI published to
  ghcr.io — no source tree, no build. SOURCE mode (fallback): fast-forward
  `origin/main` and `up -d --build`. Only the `issuefleet` service is
  recreated per update; tailscale stays up and sibling workers survive, so a
  deploy never interrupts an in-flight worker. Poll interval / mode / branch
  via `ISSUEFLEET_WATCH_{INTERVAL,MODE,BRANCH}`; clean SIGTERM exit leaves
  the stack running.

- **Fresh base + reachable branches for workers** (branch
  `worker-prefetch-branches`): the daemon clones a repo once and never
  fetched, so every worker branched from the local `main` frozen at clone
  time, and the container (no forge credential of its own) couldn't reach any
  branch pushed since. Now `_claim_one` runs `Gitops.fetch` (prune,
  `+refs/heads/*:refs/remotes/origin/*`, with the forge token via one-shot
  http.extraheader) before cutting the worktree — best-effort, a fetch blip
  still claims from local refs. `create_worktree` now cuts new branches from
  `origin/<base_ref>` (via `_base`, falling back to the bare ref when there's
  no origin/<ref> — offline/local-only), so workers start from latest main;
  existing-branch adoption is unchanged. Because a linked worktree shares the
  clone's object store, the refreshed `origin/*` refs are checkout-able inside
  the container with no network — the worker brief now points at
  `git switch --detach origin/<name>` for reproducing on another branch, and
  says to return to the work branch before committing (the daemon still
  assumes HEAD stays on the assigned branch across restarts). Offline-tested:
  real-git origin-base + fallback, prefetch-with-token, fetch-failure
  tolerance.

- **FUG-13 — bot authors Linear issues** (branch `agent/fug-13-…`): new
  `agentctl file-issue` outbox verb → `Reconciler._handle_file_issue` →
  `LinearTracker.create_issue` (`issueCreate`). New tickets inherit the
  delegated issue's team & project by default (`--team`/`--project`/
  `--no-project`/`--priority`/`--label` to steer); the new key/url is sent
  back to the worker as an `info` notice so it can summarize. Deduped by a
  marker embedded in the new issue's description (`find_issue_by_marker`,
  best-effort — degrades to at-least-once if the backend rejects the content
  filter). Offline-tested (mailbox/clients/reconcile/fakes).

- **Linear `client_credentials` app tokens** (branch
  `linear-client-credentials`): the daemon can now mint its OWN Linear
  app-actor token instead of relying on the authorization-code token
  `linear-oauth` wrote to `linear.key` — that token expires in ~24h with no
  refresh token stored, so the daemon went dark daily (observed: HTTP 401
  "not authenticated" crash-loop). New `oauth.fetch_app_token`
  (client_credentials grant, 30-day token), `linear.AppTokenProvider`
  (caches + refetches near expiry / on forced refresh), `LinearClient`
  gained a `token_provider` path that sends Bearer and, on a 401, refetches
  once and retries (Linear's prescribed 401 handling; static keys still
  propagate their 401 unchanged). `linear.client_from_config` +
  `creds.linear_uses_app_token` / `resolve_linear_oauth_client` centralize
  the choice; wired through `cli.build_stack` and `doctor`. Enable with
  `[credentials] linear_auth = "client_credentials"` (client id/secret must
  be set; `linear-oauth` install still needed once to create the app user).
  Offline-tested (clients/creds/provider/401-retry). **Unproven on the
  operator's Mac:** that a client_credentials app token actually carries the
  agent (mentionable/assignable/session) identity — Linear's docs don't say;
  verify with `bin/issuefleet doctor` (viewer identity) after switching.

## Don't retry (dead ends)

- Bazel 7.x with rules_python 2.x — py_binary bootstrap breaks; stay on
  8.x + `bootstrap_impl=script`.
- Per-worktree `info/exclude` — git ignores it entirely; common-dir only.
- Committing worker entrypoints into target repos — ruled out by the brief.
