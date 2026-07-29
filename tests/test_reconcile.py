"""Whole-loop reconcile tests against the in-memory fakes: claim → status
relay → ready → PR → feedback → merge → teardown, plus un-claim,
crash-restart, retry-after-API-failure, isolation, and capacity."""

import tempfile
import unittest
from pathlib import Path

from fakes import FakeForge, FakeGit, FakeRunner, FakeTracker

from issuefleet import MARKER_PREFIX, config
from issuefleet.mailbox import Mailbox
from issuefleet.model import Issue
from issuefleet.reconcile import Reconciler, slugify
from issuefleet.registry import Registry


def make_issue(n=1, **kw):
    base = dict(
        id=f"issue-{n}",
        key=f"FUG-{n}",
        title=f"Fix thing {n}",
        description="Please fix it.",
        url=f"https://linear.app/x/issue/FUG-{n}",
        priority=0,
        state_name="Todo",
        state_type="unstarted",
        labels=["agent"],
        created_at=f"2026-07-{n:02d}T00:00:00+00:00",
    )
    base.update(kw)
    return Issue(**base)


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
        [opened] = self.forge.opened
        self.assertEqual(opened["head"], w.branch)
        self.assertIn("Closes-Linear: FUG-1", opened["body"])
        self.assertEqual(w.pr_number, opened["number"])
        self.assertTrue(any("Pull request ready" in b for _, b in self.tracker.posted))

    def test_ready_without_commits_is_rejected_and_wakes_agent(self):
        self.claim_one()
        self.git.ahead = False
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.assertEqual(self.forge.opened, [])
        self.assertEqual(self.git.pushed, [])
        kinds = [(m.kind, m.payload.get("text", "")) for m in self.mailbox().pending_inbox()]
        self.assertTrue(any(k == "reply" and "no commits" in t for k, t in kinds))

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


if __name__ == "__main__":
    unittest.main()
