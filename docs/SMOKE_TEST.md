# End-to-end smoke test (manual, ~30 minutes)

Run this on the orchestrator host (macOS with docker, tmux,
claude-container) against a **throwaway** GitHub repo and a **scratch**
Linear project before pointing issuefleet at anything real. It exercises the
seams no unit test can: the real launcher, a real container, real APIs.

## 0. Fixtures

1. GitHub: create a throwaway repo (e.g. `you/issuefleet-smoke`) with a
   README on `main`; clone it via SSH to `~/tmp/issuefleet-smoke`.
2. Linear: create a scratch project (e.g. "Fleet Smoke") in a team you can
   litter, and a label `agent-smoke`. Add one issue:
   *"Add a CONTRIBUTING.md that says contributions welcome"* — small enough
   for one or two turns.
3. Credentials per the README (env vars are fine for a smoke run).
4. Config `~/tmp/smoke.toml`:

   ```toml
   [daemon]
   state_dir = "~/tmp/issuefleet-smoke-state"
   worktree_root = "~/tmp/issuefleet-smoke-worktrees"
   max_workers = 2
   [agent]
   max_auto_turns = 10
   [[projects]]
   name = "smoke"
   linear_project = "Fleet Smoke"
   repo = "~/tmp/issuefleet-smoke"
   claim = { strategy = "label", value = "agent-smoke" }
   state_in_progress = "In Progress"
   state_done = "Done"
   ```

## 1. Doctor, then dry-run

```sh
bin/issuefleet --config ~/tmp/smoke.toml doctor
```

Expect: all ✓ (fix anything ✗ — that is the point of doctor), and the issue
**not** listed under "Would claim now" (no label yet). Label the issue
`agent-smoke`, re-run doctor: it appears. Then:

```sh
bin/issuefleet --config ~/tmp/smoke.toml once --dry-run
```

Expect a `would claim` line naming branch and worktree, and **no** container
started, no Linear comment, no worktree created.

## 2. Claim and first turn

```sh
bin/issuefleet --config ~/tmp/smoke.toml once
bin/issuefleet --config ~/tmp/smoke.toml status
```

Expect: issue moves to *In Progress* with a claim comment naming branch,
worktree, and the tmux session; `status` shows the worker alive;
`tmux attach -t issuefleet-smoke-<KEY>` shows the container booting and the
first turn running. Within a few minutes the agent should post a plan
comment (`status` relay) on the issue — that requires another `once` tick
(or leave `run` going in a second terminal for the rest of the test).

## 3. Conversation

Comment on the issue: *"Also mention that PRs need tests."* Expect the next
turn to acknowledge it (watch via `logs -f`). If the agent asks a question,
answer it in the thread and confirm it resumes.

## 4. PR

When the agent declares ready, expect: branch pushed, PR opened with the
title/body the agent chose plus a `Closes-Linear` line, PR link posted to
the issue. Leave a review comment on the PR (inline on a file, ideally);
expect it forwarded into the agent's next turn and a re-push
(force-with-lease) updating the same PR.

## 5. Merge and teardown

Merge the PR. Within a poll interval expect: issue → *Done*, worker comment
"wound down", tmux session gone, worktree removed, remote branch deleted,
and the transcript + mailbox archived under
`~/tmp/issuefleet-smoke-state/archive/…` — verify the archive contains
`brief.md`, `mailbox/`, and `logs/turn-*.json`.

## 6. Robustness spot-checks (pick at least two)

- **Un-claim:** claim a second issue, then remove its label mid-work →
  worker winds down, branch kept, no Done transition.
- **Daemon restart:** kill `run` mid-fleet, restart it → `status` identical,
  no duplicate claim comment, agents never noticed.
- **Crash restart:** `tmux kill-session` on a worker → next tick restarts it
  (same session UUID, conversation intact); do it 4× total → crash report
  comment, worktree kept.
- **Relay retry:** disconnect the network, let the agent post a status, tick
  (fails), reconnect, tick → exactly one comment appears.
- **Concurrency:** three labeled issues with `max_workers = 2` → two claimed,
  one reported as waiting; merging one PR pulls the third in.

## 7. Cleanup

Unload/stop the daemon, `tmux kill-server` if anything lingers, delete the
scratch repo/project/label and `~/tmp/issuefleet-smoke*`.

## Record what you saw

Update `WORKLOG.md`: which steps passed, exact failures for anything that
didn't. Steps 2–5 are the ones that have **never run against real
infrastructure** in this repo's history until someone completes this
document once — treat a first clean pass as a milestone worth a commit.
