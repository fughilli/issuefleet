"""Whole-loop reconcile tests against the in-memory fakes: claim → status
relay → ready → PR → feedback → merge → teardown, plus un-claim,
crash-restart, retry-after-API-failure, isolation, and capacity."""

import tempfile
import unittest
from pathlib import Path

from fakes import FakeForge, FakeGit, FakeRunner, FakeTracker, make_issue

from issuefleet import MARKER_PREFIX, config
from issuefleet.mailbox import Mailbox
from issuefleet.model import PHASE_ACTIVE, PHASE_CRASHED, PHASE_RELEASED
from issuefleet.reconcile import Reconciler, slugify
from issuefleet.registry import Registry


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "daemon": {
                    "state_dir": str(root / "state"),
                    "worktree_root": str(root / "worktrees"),
                    "max_workers": 2,
                },
                "projects": [
                    {
                        "name": "splanc",
                        "linear_project": "Splanc",
                        "repo": str(root / "repo"),
                        "claim": {"strategy": "label", "value": "agent"},
                    }
                ],
            }
        )
        self.registry = Registry(self.cfg.state_dir)
        self.tracker = FakeTracker()
        self.forge = FakeForge()
        self.git = FakeGit(root)
        self.runner = FakeRunner()
        self.rec = Reconciler(
            self.cfg, self.registry, self.tracker, {"splanc": self.forge}, self.git, self.runner
        )

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def worker(self, n=1):
        return self.registry.get(f"issue-{n}")

    def mailbox(self, n=1):
        return Mailbox(Path(self.worker(n).worktree) / ".agent" / "mailbox")

    def claim_one(self, n=1, **kw):
        self.tracker.add_issue(make_issue(n, **kw))
        self.rec.tick()
        return self.worker(n)

    # -- claiming ----------------------------------------------------------

    def test_claim_provisions_registers_starts_and_announces(self):
        w = self.claim_one()
        self.assertIsNotNone(w)
        self.assertEqual(w.branch, "agent/fug-1-" + slugify("Fix thing 1"))
        wt = Path(w.worktree)
        self.assertTrue((wt / ".agent" / "brief.md").is_file())
        self.assertTrue((wt / ".agent" / "bin" / "agentctl").is_file())
        self.assertTrue((wt / ".agent" / "bin" / "issuefleet" / "mailbox.py").is_file())
        self.assertIn((str(wt), ".agent/"), self.git.excludes)
        self.assertEqual(self.runner.started, [w.tmux_session])
        self.assertEqual(self.tracker.state_changes, [("issue-1", "In Progress")])
        # Claim comment mentions branch and how to watch, and carries a marker.
        [(iid, body)] = self.tracker.posted
        self.assertIn(w.branch, body)
        self.assertIn("tmux attach", body)
        self.assertIn(MARKER_PREFIX + "claim-issue-1", body)

    def test_claim_prefetches_origin_with_forge_token(self):
        w = self.claim_one()
        # Exactly one fetch, on the project repo, carrying the forge's auth so
        # a private repo's refs land in the shared clone for the container.
        self.assertEqual(len(self.git.fetched), 1)
        repo, url, auth = self.git.fetched[0]
        self.assertEqual(repo, str(self.cfg.projects[0].repo))
        self.assertEqual((url, auth), ("https://github.example/o/r.git", "basic fake-token"))
        # Refs are refreshed BEFORE the worktree is cut off them.
        self.assertIsNotNone(w)

    def test_claim_tolerates_fetch_failure(self):
        self.git.fail_next_fetch = 1
        w = self.claim_one()  # must still claim from whatever refs are local
        self.assertIsNotNone(w)
        self.assertEqual(self.runner.started, [w.tmux_session])

    def test_claim_inherits_launcher_state_from_parent_repo(self):
        # The claude-container skill-approval prompt wedges headless
        # launches; the approval state from the main checkout must land in
        # the worktree and be git-excluded there.
        repo = self.cfg.projects[0].repo
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "skills-approval.json").write_text('{"ok": 1}')
        w = self.claim_one()
        self.assertEqual(
            (Path(w.worktree) / ".claude" / "skills-approval.json").read_text(), '{"ok": 1}'
        )
        self.assertIn((w.worktree, ".claude/"), self.git.excludes)

    def test_claim_is_idempotent_across_ticks(self):
        self.claim_one()
        self.rec.tick()
        self.rec.tick()
        self.assertEqual(len(self.registry.all()), 1)
        self.assertEqual(len([b for _, b in self.tracker.posted if "Claimed" in b]), 1)

    def test_concurrency_cap_and_priority_order(self):
        # max_workers=2; urgent issue 3 must beat older no-priority issues.
        self.tracker.add_issue(make_issue(1))
        self.tracker.add_issue(make_issue(2))
        self.tracker.add_issue(make_issue(3, priority=1))
        self.rec.tick()
        claimed = {w.issue_key for w in self.registry.all()}
        self.assertEqual(claimed, {"FUG-3", "FUG-1"})
        # Capacity frees up -> the waiting issue gets claimed.
        self.tracker.issues["issue-3"].labels = []  # un-claim FUG-3
        self.rec.tick()
        self.rec.tick()
        claimed = {w.issue_key for w in self.registry.all()}
        self.assertEqual(claimed, {"FUG-1", "FUG-2"})

    def test_restart_adopts_fleet_without_reclaiming(self):
        w = self.claim_one()
        # Fresh registry + reconciler = daemon restart.
        reg2 = Registry(self.cfg.state_dir)
        rec2 = Reconciler(
            self.cfg, reg2, self.tracker, {"splanc": self.forge}, self.git, self.runner
        )
        rec2.tick()
        self.assertEqual(len(reg2.all()), 1)
        self.assertEqual(reg2.get("issue-1").session_uuid, w.session_uuid)
        self.assertEqual(len([b for _, b in self.tracker.posted if "Claimed" in b]), 1)

    # -- outbox relay ------------------------------------------------------

    def test_status_relayed_once_with_marker(self):
        self.claim_one()
        self.mailbox().put_outbox("status", {"text": "formed a plan"})
        self.rec.tick()
        self.rec.tick()  # must not double-post
        plans = [b for _, b in self.tracker.posted if "formed a plan" in b]
        self.assertEqual(len(plans), 1)
        self.assertIn(MARKER_PREFIX, plans[0])
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_relay_failure_leaves_message_pending_then_retries(self):
        self.claim_one()
        self.mailbox().put_outbox("status", {"text": "progress"})
        self.tracker.fail_next_post = 1
        self.rec.tick()
        self.assertEqual(len(self.mailbox().pending_outbox()), 1)  # not dropped
        self.rec.tick()
        self.assertEqual(self.mailbox().pending_outbox(), [])  # delivered
        self.assertEqual(len([b for _, b in self.tracker.posted if "progress" in b]), 1)

    def test_relay_crash_after_post_does_not_double_post(self):
        # Simulate: post succeeded, archive never happened (process died).
        self.claim_one()
        m = self.mailbox().put_outbox("status", {"text": "half-delivered"})
        from issuefleet import marker

        self.tracker.post_comment("issue-1", f"🤖 half-delivered\n\n{marker(m.id)}")
        posted_before = len(self.tracker.posted)
        self.rec.tick()
        self.assertEqual(len(self.tracker.posted), posted_before)  # deduped
        self.assertEqual(self.mailbox().pending_outbox(), [])  # but acked

    # -- ready / PR --------------------------------------------------------

    def test_ready_pushes_opens_pr_and_links_back(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "Fix thing 1", "body": "Did the work."})
        self.rec.tick()
        w = self.worker()
        self.assertEqual(self.git.pushed, [w.branch])
        # Pushed with the forge's scoped token over HTTPS, never SSH/origin.
        self.assertEqual(self.git.push_specs, [("https://github.example/o/r.git", "basic fake-token")])
        [opened] = self.forge.opened
        self.assertEqual(opened["head"], w.branch)
        self.assertIn("Closes-Linear: FUG-1", opened["body"])
        self.assertEqual(w.pr_number, opened["number"])
        self.assertTrue(any("Pull request ready" in b for _, b in self.tracker.posted))

    def test_ready_fetches_base_before_gate(self):
        self.claim_one()
        self.git.fetched.clear()  # ignore the claim-time prefetch
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        # The base is refreshed on the ready path with the forge's token, so a
        # clone that never fetched the base can still resolve the gate rather
        # than wedge the outbox forever.
        self.assertEqual(len(self.git.fetched), 1)
        self.assertEqual(self.git.fetched[0][1], "https://github.example/o/r.git")

    def test_ready_tolerates_fetch_failure(self):
        self.claim_one()
        self.git.fail_next_fetch = 1
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()  # fetch fails, but the ready still proceeds off local refs
        self.assertEqual(self.git.pushed, [self.worker().branch])

    def test_ready_without_commits_is_rejected_and_wakes_agent(self):
        self.claim_one()
        self.git.ahead = False
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.forge.opened, [])
        self.assertEqual(self.git.pushed, [])
        kinds = [(m.kind, m.payload.get("text", "")) for m in self.mailbox().pending_inbox()]
        self.assertTrue(any(k == "reply" and "no commits" in t for k, t in kinds))

    # -- security gate -----------------------------------------------------

    def _leaky_diff(self):
        return (
            "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -0,0 +1 @@\n"
            "+token = 'ghp_" + "a" * 36 + "'\n"
        )

    def test_security_gate_blocks_leaky_ready_and_wakes_agent(self):
        from issuefleet.security import RegexSecretScanner

        self.rec.gate = RegexSecretScanner()  # cfg.security.mode defaults to "block"
        self.claim_one()
        self.git.diff_text = self._leaky_diff()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        # Nothing pushed, no PR, and the agent is woken with a redacted reason.
        self.assertEqual(self.git.pushed, [])
        self.assertEqual(self.forge.opened, [])
        kinds = [(m.kind, m.payload.get("text", "")) for m in self.mailbox().pending_inbox()]
        self.assertTrue(any(k == "reply" and "security gate" in t for k, t in kinds))
        # The raw secret never reaches the mailbox.
        self.assertFalse(any("ghp_" + "a" * 36 in t for _, t in kinds))
        # The outbox message is archived with a rejection receipt (not retried).
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_security_gate_passes_clean_ready(self):
        from issuefleet.security import RegexSecretScanner

        self.rec.gate = RegexSecretScanner()
        self.claim_one()
        self.git.diff_text = (
            "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -0,0 +1 @@\n+x = 1\n"
        )
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.git.pushed, [self.worker().branch])

    def test_security_warn_mode_submits_but_notifies(self):
        from issuefleet.security import RegexSecretScanner

        self.rec.gate = RegexSecretScanner()
        self.cfg.security.mode = "warn"
        self.claim_one()
        self.git.diff_text = self._leaky_diff()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.git.pushed, [self.worker().branch])  # still pushed
        texts = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertTrue(any("warn-only" in t for t in texts))

    def test_security_off_never_scans(self):
        # Default reconciler gate is NullGate; a leaky diff sails through.
        self.claim_one()
        self.git.diff_text = self._leaky_diff()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.git.pushed, [self.worker().branch])

    def test_security_scan_error_fails_closed(self):
        from issuefleet.security import RegexSecretScanner

        self.rec.gate = RegexSecretScanner()
        self.claim_one()
        self.git.fail_next_diff = 1
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.git.pushed, [])  # not pushed when the scan can't run
        texts = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertTrue(any("could not be submitted" in t for t in texts))

    # -- file-issue (bot authoring Linear issues) --------------------------

    def test_file_issue_creates_and_reports_back(self):
        self.claim_one()
        self.tracker.team_labels["team-1"] = {"backlog": "l1"}
        self.mailbox().put_outbox(
            "file_issue",
            {"title": "First WORKLOG item", "description": "do the thing",
             "priority": 2, "labels": ["backlog"]},
        )
        self.rec.tick()
        self.assertEqual(len(self.tracker.created), 1)
        c = self.tracker.created[0]
        self.assertEqual(c["title"], "First WORKLOG item")
        self.assertEqual(c["team"], "team-1")  # inherited from the delegated issue
        self.assertIn(MARKER_PREFIX, c["description"])  # dedupe marker embedded
        # The worker is told the new key/url via an info notice.
        info = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertTrue(any("FUG-101" in m.payload["text"] for m in info))
        self.assertEqual(self.mailbox().pending_outbox(), [])  # acked

    def test_file_issue_reports_unknown_labels(self):
        self.claim_one()
        self.mailbox().put_outbox(
            "file_issue", {"title": "T", "labels": ["ghost"]}
        )
        self.rec.tick()
        info = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertTrue(any("labels not found" in m.payload["text"] for m in info))

    def test_file_issue_deduped_when_marker_already_present(self):
        # A create landed but the ack never happened (crash mid-relay); the
        # retry must find the existing issue by marker, not file a duplicate.
        self.claim_one()
        m = self.mailbox().put_outbox("file_issue", {"title": "T", "description": "d"})
        from issuefleet import marker
        from fakes import make_issue

        self.tracker.add_issue(
            make_issue(50, labels=[], description=f"d\n\n{marker(m.id)}",
                       url="https://linear.app/x/issue/FUG-50")
        )
        self.rec.tick()
        self.assertEqual(self.tracker.created, [])  # not filed again
        info = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertTrue(any("deduped" in i.payload["text"] for i in info))
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_file_issue_failure_leaves_message_pending_then_retries(self):
        self.claim_one()
        self.mailbox().put_outbox("file_issue", {"title": "T"})
        self.tracker.fail_next_create = 1
        self.rec.tick()
        self.assertEqual(len(self.mailbox().pending_outbox()), 1)  # not dropped
        self.assertEqual(self.tracker.created, [])
        self.rec.tick()
        self.assertEqual(len(self.tracker.created), 1)  # filed on retry
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_ready_new_pr_closes_old_and_opens_fresh(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "v1", "body": "wrong premise"})
        self.rec.tick()
        old = self.worker().pr_number
        self.mailbox().put_outbox(
            "ready", {"title": "v2", "body": "correct", "new_pr": True}
        )
        self.rec.tick()
        # Old PR closed, a genuinely new PR opened (different number).
        self.assertIn(old, self.forge.closed)
        self.assertEqual(len(self.forge.opened), 2)
        self.assertNotEqual(self.worker().pr_number, old)
        self.assertEqual(self.forge.get_pr(old).state, "closed")

    def test_resubmission_updates_existing_pr(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "v1", "body": "b1"})
        self.rec.tick()
        self.mailbox().put_outbox("ready", {"title": "v2", "body": "b2"})
        self.rec.tick()
        self.assertEqual(len(self.forge.opened), 1)
        [updated] = self.forge.updated
        self.assertEqual(updated["title"], "v2")
        self.assertEqual(self.git.pushed.count(self.worker().branch), 2)

    def test_review_feedback_forwarded_once_with_context(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        n = self.worker().pr_number
        self.forge.add_feedback(n, "rename this", kind="review_comment", reviewer="bob", path="src/x.py")
        self.rec.tick()
        self.rec.tick()  # no duplicates
        fb = [m for m in self.mailbox().pending_inbox() if m.kind == "pr_feedback"]
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0].payload["reviewer"], "bob")
        self.assertEqual(fb[0].payload["path"], "src/x.py")

    def test_merge_tears_down_completely(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        w = self.worker()
        archive = self.registry.archive_dir_for(w)
        self.forge.merge(w.pr_number)
        self.rec.tick()
        self.assertIsNone(self.worker())  # registry entry dropped
        self.assertEqual(self.runner.stopped, [w.tmux_session])
        self.assertEqual(self.git.removed, [w.worktree])
        self.assertEqual(self.git.deleted_remote, [w.branch])
        self.assertIn(("issue-1", "Done"), self.tracker.state_changes)
        # Transcript/mailbox archived durably outside the worktree.
        self.assertTrue((archive / "brief.md").is_file())
        self.assertTrue((archive / "mailbox").is_dir())

    # -- dashboard stop request --------------------------------------------

    def test_dashboard_stop_request_winds_down(self):
        w = self.claim_one()
        # The web thread only enqueues; the tick thread drains and winds down.
        # (A poll-eligible issue whose claim rule still matches is re-claimed
        # on the same tick, exactly like the CLI `stop` — so exercise the drain
        # directly to assert the wind-down itself.)
        self.rec.enqueue_stop("fug-1")  # case-insensitive on the issue key
        self.assertIsNotNone(self.worker())  # queued only; nothing yet
        self.rec._drain_stop_requests()
        self.assertIsNone(self.worker())  # registry entry dropped
        self.assertEqual(self.runner.stopped, [w.tmux_session])
        self.assertEqual(self.git.removed, [w.worktree])
        self.assertEqual(self.git.deleted_remote, [])  # not done: branch kept

    def test_dashboard_stop_of_unknown_worker_is_a_noop(self):
        self.claim_one()
        self.rec.enqueue_stop("FUG-404")
        self.rec._drain_stop_requests()  # must not raise
        self.assertIsNotNone(self.worker())

    # -- dashboard reset request -------------------------------------------

    def _crash_worker(self, n=1):
        """Drive a claimed worker to phase=crashed by killing its session and
        ticking past max_restarts, exactly as the daemon does."""
        w = self.claim_one(n)
        for _ in range(self.cfg.max_restarts + 1):
            self.runner.dead.add(w.tmux_session)
            self.rec.tick()
        self.assertEqual(self.worker(n).phase, PHASE_CRASHED)
        return self.worker(n)

    def test_reset_clears_crashed_phase_and_zeroes_restarts(self):
        w = self._crash_worker()
        self.assertGreater(w.restarts, 0)
        self.rec.enqueue_reset("fug-1")  # case-insensitive
        self.assertEqual(self.worker().phase, PHASE_CRASHED)  # queued only
        self.rec._drain_reset_requests()
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)
        self.assertEqual(self.worker().restarts, 0)
        # The reset persists across a reload — it went through the registry
        # save, not just the in-memory object (the whole point vs hand-editing).
        self.registry.reload()
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)
        self.assertEqual(self.worker().restarts, 0)
        # It leaves a note for the agent so a revived worker knows why.
        info = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertTrue(any("reset" in m.payload.get("text", "").lower() for m in info))

    def test_reset_revives_a_crashed_worker_on_the_next_tick(self):
        w = self._crash_worker()
        # Crashed workers are not serviced: a tick alone won't restart it.
        self.runner.started.clear()
        self.rec.tick()
        self.assertEqual(self.runner.started, [])
        # After a reset the very next tick restarts the dead session (the
        # two-edit trap encoded: clearing phase is what actually revives it).
        self.rec.enqueue_reset("FUG-1")
        self.rec.tick()
        self.assertIn(w.tmux_session, self.runner.started)
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)

    def test_dashboard_reset_of_unknown_worker_is_a_noop(self):
        self.claim_one()
        self.rec.enqueue_reset("FUG-404")
        self.rec._drain_reset_requests()  # must not raise
        self.assertIsNotNone(self.worker())

    # -- release / adopt (FUG-113) -----------------------------------------

    def test_release_stops_removes_worktree_and_holds_the_claim(self):
        w = self.claim_one()
        self.rec.enqueue_release("fug-1")  # case-insensitive
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)  # queued only
        self.rec._drain_release_requests()
        rec = self.worker()
        self.assertIsNotNone(rec)  # registry entry KEPT (unlike stop)
        self.assertEqual(rec.phase, PHASE_RELEASED)
        self.assertIsNotNone(rec.released_at)
        self.assertEqual(self.runner.stopped, [w.tmux_session])
        self.assertEqual(self.git.removed, [w.worktree])
        self.assertEqual(self.git.deleted_remote, [])  # branch kept
        # It survives a reload (went through the registry save).
        self.registry.reload()
        self.assertEqual(self.worker().phase, PHASE_RELEASED)
        # The operator is told on the issue.
        self.assertTrue(any("Released" in b for _, b in self.tracker.posted))

    def test_released_worker_is_not_serviced_or_restarted(self):
        w = self.claim_one()
        self.rec.enqueue_release("FUG-1")
        self.rec._drain_release_requests()
        self.runner.started.clear()
        # Even with a dead session, a released worker is never restarted.
        self.runner.dead.add(w.tmux_session)
        self.rec.tick()
        self.assertEqual(self.runner.started, [])
        self.assertEqual(self.worker().phase, PHASE_RELEASED)

    def test_released_worker_holds_claim_and_is_not_reclaimed(self):
        self.claim_one()  # issue still carries the label
        self.rec.enqueue_release("FUG-1")
        self.rec._drain_release_requests()
        self.rec.tick()  # a full tick with the eligible issue present
        # No second worker, and the released one stays released (not re-claimed).
        self.assertEqual(len(self.registry.all()), 1)
        self.assertEqual(self.worker().phase, PHASE_RELEASED)

    def test_released_worker_is_unclaimed_when_issue_closes(self):
        self.claim_one()
        self.rec.enqueue_release("FUG-1")
        self.rec._drain_release_requests()
        self.tracker.issues["issue-1"].state_type = "completed"  # closed
        self.rec.tick()
        self.assertIsNone(self.worker())  # stale released record dropped

    def _release(self, n=1):
        w = self.claim_one(n)
        self.rec.enqueue_release(w.issue_key)
        self.rec._drain_release_requests()
        return self.worker(n)

    def test_adopt_rebuilds_worktree_and_resumes_same_session(self):
        w = self.claim_one()
        session_uuid = w.session_uuid
        self.rec.enqueue_release("FUG-1")
        self.rec._drain_release_requests()
        self.runner.started.clear()
        self.git.fetched.clear()
        self.rec.enqueue_adopt("fug-1")
        self.rec._drain_release_requests()
        rec = self.worker()
        self.assertEqual(rec.phase, PHASE_ACTIVE)
        self.assertIsNone(rec.released_at)
        self.assertIn(rec.tmux_session, self.runner.started)  # container restarted
        self.assertTrue(self.git.fetched)  # fetched before ff
        self.assertIn(rec.worktree, self.git.synced)  # fast-forwarded onto origin
        # The worktree is rebuilt and the SAME Claude session resumes: turns>0
        # so the loop uses --resume, and the session id is unchanged.
        import json as _json

        state = _json.loads(
            (Path(rec.worktree) / ".agent" / "state.json").read_text()
        )
        self.assertEqual(state["session_uuid"], session_uuid)
        self.assertGreater(state["turns_taken"], 0)
        # A re-orientation note is left for the resumed agent.
        info = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertTrue(any("adopted back" in m.payload.get("text", "").lower() for m in info))

    def test_adopt_resets_onto_the_operators_rebased_branch(self):
        # The operator released, rebased onto a newer mainline, force-pushed,
        # and adopts back: adopt_to_remote reports reset-to-remote and the agent
        # is told (with the reflog recovery path), not silently left on stale.
        self.claim_one()
        self.rec.enqueue_release("FUG-1")
        self.rec._drain_release_requests()
        self.git.adopt_status = "reset-to-remote"
        self.rec.enqueue_adopt("FUG-1")
        self.rec._drain_release_requests()
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)
        info = " ".join(
            m.payload.get("text", "")
            for m in self.mailbox().pending_inbox() if m.kind == "info"
        )
        self.assertIn("reflog", info)
        self.assertIn("rebased", info.lower())

    def test_adopt_of_released_worker_whose_issue_closed_drops_it(self):
        self._release()
        self.tracker.issues["issue-1"].state_type = "canceled"
        self.rec.enqueue_adopt("FUG-1")
        self.rec._drain_release_requests()
        self.assertIsNone(self.worker())  # not resurrected

    def test_adopt_of_unknown_or_active_worker_is_a_noop(self):
        self.claim_one()  # active, not released
        self.rec.enqueue_adopt("FUG-1")  # active -> ignored
        self.rec.enqueue_adopt("FUG-404")  # unknown -> ignored
        self.rec._drain_release_requests()  # must not raise
        self.assertEqual(self.worker().phase, PHASE_ACTIVE)

    def test_adopt_branch_claims_an_existing_external_branch(self):
        self.tracker.add_issue(make_issue(2, labels=[]))  # unlabeled: made outside issuefleet
        self.rec.enqueue_adopt_branch(
            {"project": "splanc", "issue_key": "FUG-2", "branch": "my-feature"}
        )
        self.rec._drain_release_requests()
        rec = self.worker(2)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.branch, "my-feature")  # not the templated name
        self.assertEqual(rec.claim_origin, "adopt")
        self.assertIn(rec.tmux_session, self.runner.started)
        self.assertIn(("issue-2", "In Progress"), self.tracker.state_changes)
        results = self.rec.adopt_results()
        self.assertTrue(results and results[-1]["ok"])

    def test_adopted_branch_is_not_unclaimed_by_the_label_rule(self):
        self.tracker.add_issue(make_issue(2, labels=[]))  # no 'agent' label
        self.rec.enqueue_adopt_branch(
            {"project": "splanc", "issue_key": "FUG-2", "branch": "my-feature"}
        )
        self.rec._drain_release_requests()
        self.rec.tick()  # the poll rule must NOT reclaim/unclaim it
        self.assertIsNotNone(self.worker(2))
        self.assertEqual(self.worker(2).branch, "my-feature")

    def test_adopt_branch_rejects_unknown_issue_and_double_claim(self):
        # Unknown issue.
        self.rec.enqueue_adopt_branch(
            {"project": "splanc", "issue_key": "FUG-404", "branch": "b"}
        )
        self.rec._drain_release_requests()
        self.assertFalse(self.rec.adopt_results()[-1]["ok"])
        # Already claimed.
        self.claim_one(3)
        self.rec.enqueue_adopt_branch(
            {"project": "splanc", "issue_key": "FUG-3", "branch": "b"}
        )
        self.rec._drain_release_requests()
        self.assertFalse(self.rec.adopt_results()[-1]["ok"])

    def test_pr_closed_without_merge_notifies_agent_once(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.forge.close(self.worker().pr_number)
        self.rec.tick()
        self.rec.tick()
        closed = [m for m in self.mailbox().pending_inbox() if m.kind == "pr_closed"]
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(self.worker())  # not silently dead

    # -- merge conflicts ---------------------------------------------------

    def _open_pr(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        return self.worker().pr_number

    def test_merge_conflict_fetches_base_and_wakes_agent_once(self):
        n = self._open_pr()
        self.git.fetched.clear()  # ignore the claim-time prefetch
        self.forge.set_mergeable(n, False)  # base advanced under the PR -> dirty
        self.rec.tick()
        self.rec.tick()  # dirty persists; must not nag again
        conflicts = [m for m in self.mailbox().pending_inbox() if m.kind == "merge_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].payload["pr_number"], n)
        self.assertEqual(conflicts[0].payload["base_ref"], "main")
        self.assertIn("origin/main", conflicts[0].payload["text"])
        # The credentialed fetch was done host-side (the container can't).
        self.assertEqual(len(self.git.fetched), 1)
        self.assertEqual(self.git.fetched[0][1], "https://github.example/o/r.git")
        self.assertTrue(self.worker().conflict_notified)

    def test_merge_conflict_rearms_after_pr_reads_mergeable(self):
        n = self._open_pr()
        self.forge.set_mergeable(n, False)
        self.rec.tick()
        self.forge.set_mergeable(n, True)  # agent rebased; PR clean again
        self.rec.tick()
        self.assertFalse(self.worker().conflict_notified)
        self.forge.set_mergeable(n, False)  # a fresh conflict later on
        self.rec.tick()
        conflicts = [m for m in self.mailbox().pending_inbox() if m.kind == "merge_conflict"]
        self.assertEqual(len(conflicts), 2)  # notified again

    def test_mergeable_unknown_does_not_notify(self):
        n = self._open_pr()
        self.forge.set_mergeable(n, None)  # GitHub hasn't computed it yet
        self.rec.tick()
        conflicts = [m for m in self.mailbox().pending_inbox() if m.kind == "merge_conflict"]
        self.assertEqual(conflicts, [])
        self.assertFalse(self.worker().conflict_notified)

    def test_merge_conflict_not_notified_when_fetch_fails(self):
        n = self._open_pr()
        self.forge.set_mergeable(n, False)
        self.git.fail_next_fetch = 1
        self.rec.tick()  # fetch fails: stay un-notified, retry next tick
        self.assertFalse(self.worker().conflict_notified)
        self.assertEqual([m for m in self.mailbox().pending_inbox() if m.kind == "merge_conflict"], [])
        self.rec.tick()  # retry succeeds
        self.assertTrue(self.worker().conflict_notified)

    # -- CI results --------------------------------------------------------

    def _ci_msgs(self):
        return [m for m in self.mailbox().pending_inbox() if m.kind == "ci_status"]

    def test_ci_success_notifies_agent_once(self):
        n = self._open_pr()
        self.forge.set_ci(n, "success", total=3)
        self.rec.tick()
        self.rec.tick()  # same result must not re-notify
        msgs = self._ci_msgs()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].payload["state"], "success")
        self.assertEqual(msgs[0].payload["pr_number"], n)
        self.assertIn("3 checks green", msgs[0].payload["text"])

    def test_ci_failure_lists_failing_checks(self):
        n = self._open_pr()
        self.forge.set_ci(n, "failure", failing=[("lint", "http://ci/lint")])
        self.rec.tick()
        msgs = self._ci_msgs()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].payload["state"], "failure")
        self.assertEqual(msgs[0].payload["failing"], [{"name": "lint", "url": "http://ci/lint"}])
        self.assertIn("lint", msgs[0].payload["text"])
        self.assertIn("http://ci/lint", msgs[0].payload["text"])

    def test_ci_pending_does_not_notify(self):
        n = self._open_pr()
        self.forge.set_ci(n, "pending", settled=False, total=2)
        self.rec.tick()
        self.assertEqual(self._ci_msgs(), [])

    def test_ci_no_checks_does_not_notify(self):
        self._open_pr()
        self.rec.tick()  # FakeForge default is state "none"
        self.assertEqual(self._ci_msgs(), [])

    def test_ci_rerun_flip_failure_to_success_renotifies(self):
        n = self._open_pr()
        self.forge.set_ci(n, "failure", failing=[("test", None)])
        self.rec.tick()
        self.forge.set_ci(n, "success", total=1)  # same SHA, re-run went green
        self.rec.tick()
        msgs = self._ci_msgs()
        self.assertEqual([m.payload["state"] for m in msgs], ["failure", "success"])

    def test_ci_new_commit_renotifies(self):
        n = self._open_pr()
        self.forge.set_ci(n, "failure", failing=[("test", None)])
        self.rec.tick()
        self.forge.bump_head_sha(n, "sha-after-push")  # agent pushed a fix
        self.forge.set_ci(n, "success", total=1)
        self.rec.tick()
        msgs = self._ci_msgs()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1].payload["sha"], "sha-after-push")

    def test_ci_fetch_failure_is_soft_and_retries(self):
        n = self._open_pr()
        self.forge.set_ci(n, "failure", failing=[("test", None)])
        self.forge.fail_next_ci = 1
        self.rec.tick()  # checks call fails: no message, worker survives
        self.assertEqual(self._ci_msgs(), [])
        self.assertIsNotNone(self.worker())
        self.rec.tick()  # retry succeeds
        self.assertEqual(len(self._ci_msgs()), 1)

    # -- un-claim ----------------------------------------------------------

    def test_label_removed_unclaims_cleanly(self):
        self.claim_one()
        w = self.worker()
        self.tracker.issues["issue-1"].labels = []
        self.rec.tick()
        self.assertIsNone(self.worker())
        self.assertEqual(self.runner.stopped, [w.tmux_session])
        self.assertEqual(self.git.removed, [w.worktree])
        self.assertEqual(self.git.deleted_remote, [])  # not done: branch kept
        self.assertNotIn(("issue-1", "Done"), self.tracker.state_changes)

    def test_issue_closed_unclaims(self):
        self.claim_one()
        self.tracker.issues["issue-1"].state_type = "canceled"
        self.rec.tick()
        self.assertIsNone(self.worker())

    # -- crash handling ----------------------------------------------------

    def test_dead_session_restarted_bounded_then_reported(self):
        self.claim_one()
        w = self.worker()
        for expected_restarts in (1, 2, 3):
            self.runner.dead.add(w.tmux_session)
            self.runner.started.clear()
            self.rec.tick()
            self.assertEqual(self.worker().restarts, expected_restarts)
            self.assertEqual(self.runner.started, [w.tmux_session])  # restarted
        # Fourth death exceeds max_restarts=3: report and keep the worktree.
        self.runner.dead.add(w.tmux_session)
        self.runner.started.clear()
        self.rec.tick()
        self.assertEqual(self.worker().phase, "crashed")
        self.assertEqual(self.runner.started, [])  # no restart loop
        self.assertTrue(any("giving up" in b for _, b in self.tracker.posted))
        self.assertEqual(self.git.removed, [])  # worktree kept for inspection
        # Crashed worker stays claimed but frees its concurrency slot.
        self.tracker.add_issue(make_issue(2))
        self.tracker.add_issue(make_issue(3))
        self.rec.tick()
        self.assertEqual(len(self.registry.all()), 3)

    def test_restart_fast_forwards_the_branch_before_the_agent_resumes(self):
        # A worker stopped while someone pushed to its branch must not resume
        # on stale code: push() is a plain --force, so its next push would
        # erase whatever landed while it was down.
        self.claim_one()
        w = self.worker()
        self.git.synced.clear()
        self.git.sync_status = "fast-forwarded"
        self.runner.dead.add(w.tmux_session)
        self.rec.tick()
        self.assertEqual(self.git.synced, [w.worktree])
        info = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertTrue(any("fast-forwarded to origin" in t for t in info), info)

    def test_restart_warns_on_divergence_without_touching_the_branch(self):
        self.claim_one()
        w = self.worker()
        self.git.sync_status = "diverged"
        self.runner.dead.add(w.tmux_session)
        self.runner.started.clear()
        self.rec.tick()
        info = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertTrue(any("BOTH advanced" in t for t in info), info)
        # The warning is advisory: the worker still comes back up.
        self.assertEqual(self.runner.started, [w.tmux_session])

    def test_up_to_date_branch_adds_no_mailbox_noise(self):
        self.claim_one()
        w = self.worker()
        self.git.sync_status = "up-to-date"
        self.runner.dead.add(w.tmux_session)
        self.rec.tick()
        info = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertFalse(any("origin" in t for t in info), info)

    def test_branch_sync_failure_never_blocks_a_restart(self):
        # Best-effort: a fetch/merge failure leaves the old behaviour (resume
        # where it left off) rather than stranding the worker.
        self.claim_one()
        w = self.worker()
        self.git.fail_next_sync = 1
        self.runner.dead.add(w.tmux_session)
        self.runner.started.clear()
        self.rec.tick()
        self.assertEqual(self.runner.started, [w.tmux_session])

    def test_restart_note_written_only_once_a_session_is_live(self):
        # A worker that can never start (e.g. the macOS script(1) bug) must not
        # accrue one identical unread restart note per tick — that buries real
        # replies. No live session => no note, but the counter still climbs.
        self.claim_one()
        w = self.worker()
        for _ in range(2):
            self.runner.dead.add(w.tmux_session)
            self.runner.fail_start.add(w.tmux_session)
            self.rec.tick()
        notes = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()]
        self.assertFalse(any("restarted after a crash" in t for t in notes), notes)
        self.assertEqual(self.worker().restarts, 2)  # attempts still counted

    def test_repeated_live_restarts_leave_a_single_restart_note(self):
        # When the worker does come back but is restarted again before it reads
        # (the note is never consumed in these tests), coalescing keeps exactly
        # one copy rather than a per-restart pile.
        self.claim_one()
        w = self.worker()
        for _ in range(3):
            self.runner.dead.add(w.tmux_session)
            self.rec.tick()
        notes = [m.payload.get("text", "") for m in self.mailbox().pending_inbox()
                 if "restarted after a crash" in m.payload.get("text", "")]
        self.assertEqual(len(notes), 1, notes)

    # -- inbound comments --------------------------------------------------

    def test_human_comment_ingested_own_posts_filtered(self):
        self.claim_one()
        self.mailbox().put_outbox("status", {"text": "plan"})
        self.rec.tick()  # posts the status (bot comment now on the issue)
        self.tracker.human_comment("issue-1", "looks good, also handle nulls")
        self.rec.tick()
        replies = [m for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].payload["author"], "alice")
        self.rec.tick()  # cursor advanced: no re-ingestion
        replies = [m for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(len(replies), 1)

    def test_comment_mode_acknowledges_with_a_deduped_eyes_comment(self):
        # No agent session (personal-key / poll claim): the 👀 acknowledgment
        # falls back to a comment, posted once and deduped by the source
        # comment id so a crash-retry can't double-post it.
        self.claim_one()
        self.tracker.human_comment("issue-1", "please also add a test")
        self.rec.tick()
        eyes = [b for _, b in self.tracker.posted if b.startswith("👀")]
        self.assertEqual(len(eyes), 1)
        # A retry of the same ingest doesn't post a second 👀 (marker dedupe).
        self.worker().comment_cursor = None  # force re-ingest of the same comment
        self.registry.save()
        self.rec.tick()
        eyes = [b for _, b in self.tracker.posted if b.startswith("👀")]
        self.assertEqual(len(eyes), 1)

    def test_operator_reply_from_api_key_account_is_ingested(self):
        # With a personal (non-bot) Linear key, the operator's replies come
        # from the same user as the orchestrator's posts. Only the marker
        # may filter — an identity check would silently eat the reply.
        from issuefleet.model import Comment

        self.claim_one()
        self.mailbox().put_outbox("status", {"text": "plan"})
        self.rec.tick()  # bot posts (marker-stamped) under the viewer id
        self.tracker.comments["issue-1"].append(
            Comment(
                id="op1",
                author_id=self.tracker.viewer_id,  # operator == API user
                author_name="kevin",
                body="looks good, ship it",
                created_at="2026-07-29T23:00:00+00:00",
            )
        )
        self.rec.tick()
        replies = [m for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].payload["text"], "looks good, ship it")
        # ...while the orchestrator's own marker-stamped posts stayed out.
        self.assertEqual(replies[0].payload["author"], "kevin")

    def test_app_identity_filters_viewer_comments(self):
        # Authenticated AS THE APP, viewer-authored comments are the app's
        # own — including Linear's unmarked mirrors of session activities
        # (the live echo loop of 2026-07-30). Identity filtering applies.
        from issuefleet.model import Comment

        self.tracker.app_identity = True
        self.claim_one()
        self.tracker.comments["issue-1"].append(
            Comment(
                id="mirror1",
                author_id=self.tracker.viewer_id,
                author_name="issuefleet",
                body="Pull request ready: https://github.example/pr/1",  # no marker!
                created_at="2026-07-30T16:16:00+00:00",
            )
        )
        self.tracker.human_comment("issue-1", "real human reply")
        self.rec.tick()
        replies = [m.payload["text"] for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(replies, ["real human reply"])

    def test_resubmission_does_not_repeat_pr_info(self):
        self.claim_one()
        self.mailbox().put_outbox("ready", {"title": "v1", "body": "b"})
        self.rec.tick()
        self.mailbox().put_outbox("ready", {"title": "v2", "body": "b"})
        self.rec.tick()
        infos = [m for m in self.mailbox().pending_inbox() if m.kind == "info"]
        self.assertEqual(len(infos), 1)  # told once, not per re-submission

    def test_persistent_poll_failure_collapses_to_one_traceback(self):
        self.claim_one()
        self.tracker.fail_next_post = 0
        original = self.tracker.eligible_issues
        self.tracker.eligible_issues = lambda p: (_ for _ in ()).throw(
            ConnectionError("DNS down")
        )
        try:
            with self.assertLogs("issuefleet", level="ERROR") as cm:
                self.rec.tick()
                self.rec.tick()
                self.rec.tick()
            with_tb = [r for r in cm.records if r.exc_info]
            without_tb = [r for r in cm.records if not r.exc_info]
            self.assertEqual(len(with_tb), 1)  # one full traceback
            self.assertEqual(len(without_tb), 2)  # then one-liners
        finally:
            self.tracker.eligible_issues = original

    def test_reload_drops_worker_stopped_by_another_process(self):
        import shutil
        from issuefleet.registry import Registry

        self.claim_one()
        w = self.worker()
        self.tracker.issues["issue-1"].labels = []  # un-eligible: no re-claim
        # Simulate an external `stop`: remove from the on-disk registry and
        # delete the worktree, WITHOUT touching this reconciler's memory.
        Registry(self.cfg.state_dir).remove("issue-1")
        shutil.rmtree(w.worktree)
        self.rec.tick()  # stale in-memory entry must not crash the tick
        self.assertIsNone(self.worker())

    def test_service_drops_worker_whose_worktree_vanished(self):
        # A worktree removed underfoot (still in the on-disk registry) is
        # unserviceable and gets dropped rather than crashing every tick.
        import shutil

        self.claim_one()
        self.tracker.issues["issue-1"].labels = []  # un-eligible: no re-claim
        shutil.rmtree(self.worker().worktree)
        self.rec.tick()
        self.assertIsNone(self.worker())

    def test_still_delegated_worker_is_reclaimed_after_external_stop(self):
        # The production case: stop a still-eligible worker and the next
        # tick re-claims it fresh (new worktree), not a zombie.
        import shutil
        from issuefleet.registry import Registry

        self.claim_one()
        old_wt = self.worker().worktree
        Registry(self.cfg.state_dir).remove("issue-1")
        shutil.rmtree(old_wt)  # issue-1 stays labeled/eligible
        self.rec.tick()
        w = self.worker()
        self.assertIsNotNone(w)  # re-claimed
        self.assertTrue((Path(w.worktree) / ".agent").is_dir())  # fresh worktree

    # -- isolation ---------------------------------------------------------

    def test_sick_worker_does_not_stall_the_fleet(self):
        self.tracker.add_issue(make_issue(1))
        self.tracker.add_issue(make_issue(2))
        self.rec.tick()
        self.tracker.fail_get_issue.add("issue-1")
        self.mailbox(2).put_outbox("status", {"text": "worker two fine"})
        self.rec.tick()  # worker 1 raises; worker 2 must still be serviced
        self.assertTrue(any("worker two fine" in b for _, b in self.tracker.posted))
        self.assertIsNotNone(self.worker(1))  # not torn down by the failure


class AddProjectTest(unittest.TestCase):
    """Dashboard-driven add-project: the tick thread clones, wires the forge,
    grows cfg.projects, and persists — with the config file written last."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            "[daemon]\n"
            f'state_dir = "{self.root / "state"}"\n'
            f'worktree_root = "{self.root / "worktrees"}"\n\n'
            "[[projects]]\n"
            'name = "splanc"\n'
            'linear_project = "Splanc"\n'
            f'repo = "{self.root / "repo"}"\n'
            'claim = { strategy = "agent" }\n'
        )
        self.cfg = config.load(self.config_path)
        self.registry = Registry(self.cfg.state_dir)
        self.tracker = FakeTracker()
        self.git = FakeGit(self.root)
        self.runner = FakeRunner()
        self.rec = Reconciler(
            self.cfg, self.registry, self.tracker, {"splanc": FakeForge()},
            self.git, self.runner, token_source=lambda owner: "faketoken",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def valid_spec(self, **kw):
        spec = {
            "name": "led-mapper",
            "linear_project": "LED Mapper",
            "repo": str(self.root / "led_mapper"),
            "git_url": "https://github.com/o/led_mapper",
            "claim": {"strategy": "state", "value": "Ready for agent"},
        }
        spec.update(kw)
        return spec

    def test_add_clones_wires_and_persists(self):
        self.rec.enqueue_add_project(self.valid_spec())
        self.rec.tick()
        # Live in this process...
        self.assertIn("led-mapper", [p.name for p in self.cfg.projects])
        self.assertIn("led-mapper", self.rec.forges)
        self.assertEqual(len(self.git.cloned), 1)
        # ...and persisted, so a fresh load sees it.
        reloaded = config.load(self.config_path)
        self.assertIn("led-mapper", [p.name for p in reloaded.projects])
        results = self.rec.project_results()
        self.assertTrue(results[-1]["ok"])

    def test_added_project_is_polled_same_tick(self):
        # An issue matching the new project's claim is claimed the tick it lands.
        issue = make_issue(5, labels=[], state_name="Ready for agent")
        issue.project_id = "LED Mapper"
        self.tracker.add_issue(issue)
        self.rec.enqueue_add_project(self.valid_spec())
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-5"))

    def test_duplicate_name_rejected(self):
        self.rec.enqueue_add_project(self.valid_spec(name="splanc"))
        self.rec.tick()
        self.assertEqual([p.name for p in self.cfg.projects], ["splanc"])
        self.assertEqual(self.git.cloned, [])
        self.assertFalse(self.rec.project_results()[-1]["ok"])

    def test_clone_failure_does_not_persist(self):
        # No git_url and the repo doesn't exist -> ensure_checkout dead-ends.
        spec = self.valid_spec()
        del spec["git_url"]
        self.rec.enqueue_add_project(spec)
        self.rec.tick()
        self.assertNotIn("led-mapper", [p.name for p in self.cfg.projects])
        reloaded = config.load(self.config_path)
        self.assertNotIn("led-mapper", [p.name for p in reloaded.projects])
        self.assertFalse(self.rec.project_results()[-1]["ok"])

    def test_no_token_source_reports_error(self):
        rec = Reconciler(
            self.cfg, self.registry, self.tracker, {"splanc": FakeForge()},
            self.git, self.runner,  # token_source=None
        )
        rec.enqueue_add_project(self.valid_spec())
        rec.tick()
        self.assertNotIn("led-mapper", [p.name for p in self.cfg.projects])
        self.assertFalse(rec.project_results()[-1]["ok"])

    def test_invalid_spec_reports_error(self):
        self.rec.enqueue_add_project({"name": "x", "linear_project": "", "repo": "/r"})
        self.rec.tick()
        self.assertFalse(self.rec.project_results()[-1]["ok"])


class CrossProjectTest(unittest.TestCase):
    """FUG-115: a worker on one project staging a change in a sibling project —
    checkout, PR, and being notified when the upstream PR lands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "daemon": {
                    "state_dir": str(root / "state"),
                    "worktree_root": str(root / "worktrees"),
                    "max_workers": 2,
                },
                "projects": [
                    {"name": "splanc", "linear_project": "Splanc",
                     "repo": str(root / "splanc"), "git_url": "https://github.com/o/splanc.git",
                     "claim": {"strategy": "label", "value": "agent"}},
                    {"name": "embedded", "linear_project": "Embedded",
                     "repo": str(root / "embedded"), "git_url": "https://github.com/o/embedded.git",
                     # A distinct claim label so the test issue (labelled "agent")
                     # is claimed only by splanc — embedded is purely the sibling
                     # this worker contributes to.
                     "claim": {"strategy": "label", "value": "embedded-work"}},
                ],
            }
        )
        self.registry = Registry(self.cfg.state_dir)
        self.tracker = FakeTracker()
        self.splanc_forge = FakeForge()
        self.embedded_forge = FakeForge()
        self.git = FakeGit(root)
        self.runner = FakeRunner()
        self.rec = Reconciler(
            self.cfg, self.registry, self.tracker,
            {"splanc": self.splanc_forge, "embedded": self.embedded_forge},
            self.git, self.runner,
        )
        # Claim a splanc issue: this worker will contribute upstream to embedded.
        self.tracker.add_issue(make_issue(1, project_id="Splanc"))
        self.rec.tick()
        self.worker = self.registry.get("issue-1")

    def tearDown(self):
        self.tmp.cleanup()

    def mailbox(self):
        return Mailbox(Path(self.worker.worktree) / ".agent" / "mailbox")

    def inbox_kinds(self, kind):
        return [m for m in self.mailbox().pending_inbox() if m.kind == kind]

    def request(self, kind, payload):
        self.mailbox().put_outbox(kind, payload)
        self.rec.tick()

    def checkout(self, project="embedded"):
        self.request("upstream_checkout", {"project": project})

    # -- the brief advertises the mechanism -------------------------------

    def test_brief_lists_sibling_projects(self):
        brief = (Path(self.worker.worktree) / ".agent" / "brief.md").read_text()
        self.assertIn("Contributing to other fleet projects", brief)
        self.assertIn("embedded", brief)
        self.assertNotIn("**splanc**", brief)  # not its own project

    def test_claim_precreates_and_excludes_siblings_dir(self):
        self.assertTrue((Path(self.worker.worktree) / "siblings").is_dir())
        self.assertIn((str(Path(self.worker.worktree)), "siblings/"), self.git.excludes)

    # -- checkout ---------------------------------------------------------

    def test_upstream_checkout_opens_a_worktree_of_the_sibling(self):
        self.checkout()
        # A linked worktree of the sibling repo was opened at siblings/embedded.
        wt = next(w for w in self.git.worktrees if w["path"].endswith("siblings/embedded"))
        self.assertEqual(wt["repo"], str(self.cfg.project("embedded").repo))
        # A link is recorded on the worker, and the agent is woken with the path.
        self.worker = self.registry.get("issue-1")
        [link] = self.worker.upstream_links
        self.assertEqual(link["project"], "embedded")
        self.assertEqual(link["path"], "siblings/embedded")
        self.assertEqual(link["base_sha"], self.git.head_sha)
        self.assertIsNone(link["pr_number"])
        [ready] = self.inbox_kinds("upstream_ready")
        self.assertTrue(ready.payload["ok"])
        self.assertIn("siblings/embedded", ready.payload["text"])
        # The request was archived, not left to retry.
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_unknown_project_is_reported_not_wedged(self):
        self.request("upstream_checkout", {"project": "nope"})
        [ready] = self.inbox_kinds("upstream_ready")
        self.assertFalse(ready.payload["ok"])
        self.assertEqual(self.registry.get("issue-1").upstream_links, [])
        self.assertEqual(self.mailbox().pending_outbox(), [])  # archived, no retry

    def test_own_project_is_rejected(self):
        self.request("upstream_checkout", {"project": "splanc"})
        [ready] = self.inbox_kinds("upstream_ready")
        self.assertFalse(ready.payload["ok"])
        self.assertIn("your own project", ready.payload["text"])

    def test_checkout_failure_is_reported(self):
        self.git.fail_next_worktree = 1
        self.checkout()
        [ready] = self.inbox_kinds("upstream_ready")
        self.assertFalse(ready.payload["ok"])
        self.assertEqual(self.registry.get("issue-1").upstream_links, [])

    # -- staging the PR ---------------------------------------------------

    def test_upstream_pr_pushes_to_the_sibling_and_reports_the_pin(self):
        self.checkout()
        self.git.head_sha = "pushedsha1234"
        self.request("upstream_pr", {"project": "embedded", "title": "Add C3", "body": "why"})
        # A PR was opened on the sibling forge, not on splanc, from the link's branch.
        link = self.registry.get("issue-1").upstream_links[0]
        self.assertEqual(self.git.pushed[-1], link["branch"])
        self.assertEqual(len(self.embedded_forge.opened), 1)
        self.assertEqual(self.splanc_forge.opened, [])
        opened = self.embedded_forge.opened[0]
        self.assertIn("Add C3", opened["title"])
        # The link now carries the PR + pushed head SHA (the CI-testable pin).
        self.assertEqual(link["pr_number"], opened["number"])
        self.assertEqual(link["head_sha"], "pushedsha1234")
        [opened_msg] = self.inbox_kinds("upstream_pr_opened")
        self.assertTrue(opened_msg.payload["ok"])
        self.assertEqual(opened_msg.payload["head_sha"], "pushedsha1234")
        self.assertIn("pushedsha1234"[:12], opened_msg.payload["text"])  # shown short

    def test_upstream_pr_without_checkout_is_rejected(self):
        self.request("upstream_pr", {"project": "embedded", "title": "t", "body": "b"})
        [msg] = self.inbox_kinds("upstream_pr_opened")
        self.assertFalse(msg.payload["ok"])
        self.assertIn("upstream-checkout", msg.payload["text"])
        self.assertEqual(self.embedded_forge.opened, [])

    def test_upstream_pr_with_no_commits_is_rejected(self):
        self.checkout()
        self.git.ahead = False
        self.request("upstream_pr", {"project": "embedded", "title": "t", "body": "b"})
        [msg] = self.inbox_kinds("upstream_pr_opened")
        self.assertFalse(msg.payload["ok"])
        self.assertIn("no commits", msg.payload["text"])
        self.assertEqual(self.embedded_forge.opened, [])

    def test_resubmitting_updates_the_same_pr(self):
        self.checkout()
        self.request("upstream_pr", {"project": "embedded", "title": "v1", "body": "b"})
        num = self.registry.get("issue-1").upstream_links[0]["pr_number"]
        self.request("upstream_pr", {"project": "embedded", "title": "v2", "body": "b"})
        self.assertEqual(len(self.embedded_forge.opened), 1)  # not opened twice
        self.assertEqual(self.embedded_forge.updated[-1]["title"], "v2")
        self.assertEqual(self.registry.get("issue-1").upstream_links[0]["pr_number"], num)

    # -- notification when the upstream PR lands --------------------------

    def _stage_pr(self):
        self.checkout()
        self.request("upstream_pr", {"project": "embedded", "title": "t", "body": "b"})
        return self.registry.get("issue-1").upstream_links[0]["pr_number"]

    def test_upstream_merge_notifies_with_mainline_sha_once(self):
        num = self._stage_pr()
        self.embedded_forge.merge(num, merge_sha="mainlinesha99")
        self.rec.tick()
        [merged] = self.inbox_kinds("upstream_merged")
        self.assertEqual(merged.payload["merge_sha"], "mainlinesha99")
        self.assertIn("mainlinesha99"[:12], merged.payload["text"])  # shown short
        link = self.registry.get("issue-1").upstream_links[0]
        self.assertTrue(link["merged"])
        # A second tick does not re-notify.
        self.rec.tick()
        self.assertEqual(len(self.inbox_kinds("upstream_merged")), 1)

    def test_upstream_close_unmerged_notifies_once(self):
        num = self._stage_pr()
        self.embedded_forge.close(num)  # closed, not merged
        self.rec.tick()
        [closed] = self.inbox_kinds("upstream_pr_closed")
        self.assertIn("closed", closed.payload["text"].lower())
        self.rec.tick()
        self.assertEqual(len(self.inbox_kinds("upstream_pr_closed")), 1)

    # -- teardown ---------------------------------------------------------

    def test_teardown_deregisters_sibling_worktrees(self):
        self.checkout()
        sib_path = str(Path(self.worker.worktree) / "siblings" / "embedded")
        # Close the driving issue: the worker winds down this tick.
        self.tracker.issues["issue-1"].state_type = "completed"
        self.rec.tick()
        self.assertIn(sib_path, self.git.removed)  # sibling worktree deregistered
        self.assertIn(self.worker.worktree, self.git.removed)  # and the main one
        self.assertIsNone(self.registry.get("issue-1"))


if __name__ == "__main__":
    unittest.main()
