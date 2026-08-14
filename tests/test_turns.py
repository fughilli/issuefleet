"""Turn-decision control flow, driven with a temp directory as a fake
workspace — no container, no network, no credentials (brief §6)."""

import tempfile
import unittest
from pathlib import Path

from issuefleet.agent_runtime import agentctl, turns
from issuefleet.mailbox import Mailbox

BRIEF = "# Issue FUG-1: Fix the thing\n\nDo the work.\n"


class TurnsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.agent_dir = self.workspace / ".agent"
        self.agent_dir.mkdir()
        (self.agent_dir / "brief.md").write_text(BRIEF)
        self.state = turns.TurnState(
            session_uuid="00000000-0000-0000-0000-000000000001", max_auto_turns=3
        )
        self.state.save(self.agent_dir)
        self.mb = Mailbox(self.agent_dir / "mailbox").ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def reload(self):
        return turns.TurnState.load(self.agent_dir)

    def decide(self):
        return turns.decide(self.agent_dir, self.mb, self.reload())

    def decide_and_commit(self):
        state = self.reload()
        d = turns.decide(self.agent_dir, self.mb, state)
        turns.commit(d, self.agent_dir, self.mb, state)
        return d

    # -- the brief's mandated cases ---------------------------------------

    def test_first_turn_emits_full_brief_without_resume(self):
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertFalse(d.resume)
        self.assertIn("Fix the thing", d.prompt)

    def test_reply_resumes_a_paused_agent(self):
        state = self.reload()
        state.phase = turns.PHASE_WAITING
        state.save(self.agent_dir)
        self.assertEqual(self.decide().exit_code, turns.EXIT_IDLE)

        self.mb.put_inbox("reply", {"author": "alice", "text": "use approach B"})
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertTrue(d.resume)
        self.assertIn("use approach B", d.prompt)
        self.assertIn("alice", d.prompt)

    def test_asking_a_question_idles(self):
        self.decide_and_commit()  # first turn
        # During the turn the agent runs `agentctl ask`.
        self.mb.put_outbox("question", {"text": "which schema?"})
        state = self.reload()
        state.phase = turns.PHASE_WAITING
        state.save(self.agent_dir)
        self.assertEqual(self.decide().exit_code, turns.EXIT_IDLE)

    def test_auto_turn_budget_trips_and_reports_once(self):
        self.decide_and_commit()  # first turn (resets budget clock)
        for _ in range(3):  # max_auto_turns=3 self-driven continuations
            d = self.decide_and_commit()
            self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        d = self.decide_and_commit()
        self.assertEqual(d.exit_code, turns.EXIT_BUDGET)
        statuses = [m for m in self.mb.pending_outbox() if m.kind == "status"]
        self.assertEqual(len(statuses), 1)
        self.assertIn("budget", statuses[0].payload["text"].lower())
        # Second trip does not spam another status.
        d = self.decide_and_commit()
        self.assertEqual(d.exit_code, turns.EXIT_BUDGET)
        self.assertEqual(len([m for m in self.mb.pending_outbox() if m.kind == "status"]), 1)

    def test_reply_resets_the_budget_clock(self):
        self.decide_and_commit()
        for _ in range(3):
            self.decide_and_commit()
        self.assertEqual(self.decide().exit_code, turns.EXIT_BUDGET)
        self.mb.put_inbox("reply", {"author": "alice", "text": "keep going"})
        d = self.decide_and_commit()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertEqual(self.reload().auto_turns, 0)

    def test_shutdown_exits(self):
        self.mb.put_inbox("shutdown", {"reason": "PR merged"})
        d = self.decide_and_commit()
        self.assertEqual(d.exit_code, turns.EXIT_SHUTDOWN)
        # The stop message is consumed so a restarted loop doesn't re-exit
        # on stale state after a legitimate re-claim.
        self.assertEqual(self.mb.pending_inbox(), [])

    def test_unclaimed_exits(self):
        self.mb.put_inbox("unclaimed", {"reason": "label removed"})
        self.assertEqual(self.decide().exit_code, turns.EXIT_SHUTDOWN)

    # -- supporting behavior ----------------------------------------------

    def test_ready_phase_idles_until_feedback(self):
        state = self.reload()
        state.phase = turns.PHASE_READY
        state.save(self.agent_dir)
        self.assertEqual(self.decide().exit_code, turns.EXIT_READY)
        self.mb.put_inbox(
            "pr_feedback",
            {"reviewer": "bob", "kind": "review_comment", "path": "src/x.py", "text": "rename this"},
        )
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("src/x.py", d.prompt)
        self.assertIn("bob", d.prompt)

    def test_pr_closed_wakes_the_agent(self):
        state = self.reload()
        state.phase = turns.PHASE_READY
        state.save(self.agent_dir)
        self.mb.put_inbox("pr_closed", {"text": "closed without merge"})
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("closed without merging", d.prompt)

    def test_merge_conflict_wakes_the_agent(self):
        state = self.reload()
        state.phase = turns.PHASE_READY  # sitting on a submitted PR
        state.save(self.agent_dir)
        self.mb.put_inbox("merge_conflict", {"text": "rebase onto origin/main"})
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("rebase needed", d.prompt)

    def test_ci_status_wakes_the_agent(self):
        state = self.reload()
        state.phase = turns.PHASE_READY  # sitting on a submitted PR
        state.save(self.agent_dir)
        self.mb.put_inbox(
            "ci_status",
            {"state": "failure", "text": "CI failed on PR #7. Failing checks:\n  - lint"},
        )
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("CI failure on your PR", d.prompt)
        self.assertIn("lint", d.prompt)

    def test_upstream_ready_wakes_a_worker_idling_on_its_request(self):
        # After `upstream-checkout` the worker idles in WAITING; the
        # orchestrator's reply must wake it with the checkout path.
        state = self.reload()
        state.phase = turns.PHASE_WAITING
        state.save(self.agent_dir)
        self.assertEqual(self.decide().exit_code, turns.EXIT_IDLE)
        self.mb.put_inbox(
            "upstream_ready",
            {"ok": True, "project": "embedded", "path": "upstream/embedded",
             "text": "clone ready at upstream/embedded"},
        )
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("Upstream checkout ready (embedded)", d.prompt)
        self.assertIn("upstream/embedded", d.prompt)

    def test_upstream_merged_wakes_the_agent_to_repoint(self):
        state = self.reload()
        state.phase = turns.PHASE_READY  # sitting on its own submitted PR
        state.save(self.agent_dir)
        self.mb.put_inbox(
            "upstream_merged",
            {"project": "embedded", "merge_sha": "deadbeef",
             "text": "upstream merged; pin to deadbeef"},
        )
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("Upstream PR merged (embedded)", d.prompt)
        self.assertIn("deadbeef", d.prompt)

    def test_upstream_failure_reply_still_wakes(self):
        # An error reply (ok=False) must unblock the worker too, not strand it.
        state = self.reload()
        state.phase = turns.PHASE_WAITING
        state.save(self.agent_dir)
        self.mb.put_inbox(
            "upstream_pr_opened",
            {"ok": False, "project": "embedded", "text": "no commits to push"},
        )
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("Upstream PR request FAILED (embedded)", d.prompt)

    def test_info_alone_does_not_wake(self):
        state = self.reload()
        state.phase = turns.PHASE_READY
        state.save(self.agent_dir)
        self.mb.put_inbox("info", {"text": "PR opened at https://x"})
        self.assertEqual(self.decide().exit_code, turns.EXIT_READY)
        # ...but rides along once a waking message arrives.
        self.mb.put_inbox("reply", {"author": "alice", "text": "also update docs"})
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertIn("PR opened at", d.prompt)

    def test_idle_phase_idles_and_wakes_like_ready(self):
        state = self.reload()
        state.phase = turns.PHASE_IDLE
        state.save(self.agent_dir)
        self.assertEqual(self.decide().exit_code, turns.EXIT_READY)
        self.mb.put_inbox("reply", {"author": "alice", "text": "one more thing"})
        d = self.decide()
        self.assertEqual(d.exit_code, turns.EXIT_CONTINUE)
        self.assertEqual(d.wake_from_phase, turns.PHASE_IDLE)

    def test_commit_consumes_injected_messages(self):
        self.mb.put_inbox("reply", {"author": "alice", "text": "hi"})
        self.decide_and_commit()
        self.assertEqual(self.mb.pending_inbox(), [])

    def test_state_roundtrip_tolerates_unknown_fields(self):
        import json

        p = self.agent_dir / "state.json"
        data = json.loads(p.read_text())
        data["future_field"] = "x"
        p.write_text(json.dumps(data))
        self.assertEqual(self.reload().max_auto_turns, 3)


class AgentctlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        agent = self.workspace / ".agent"
        agent.mkdir()
        (agent / "brief.md").write_text(BRIEF)
        turns.TurnState(session_uuid="u1").save(agent)
        self.agent_dir = agent
        self.mb = Mailbox(agent / "mailbox").ensure()
        import os

        os.environ["ISSUEFLEET_AGENT_DIR"] = str(agent)

    def tearDown(self):
        import os

        del os.environ["ISSUEFLEET_AGENT_DIR"]
        self.tmp.cleanup()

    def test_status(self):
        agentctl.main(["status", "made", "a", "plan"])
        [m] = self.mb.pending_outbox()
        self.assertEqual((m.kind, m.payload["text"]), ("status", "made a plan"))

    def test_ask_sets_waiting_phase(self):
        agentctl.main(["ask", "which db?"])
        [m] = self.mb.pending_outbox()
        self.assertEqual(m.kind, "question")
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_WAITING)

    def test_idle_sets_idle_phase(self):
        agentctl.main(["idle"])
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_IDLE)
        self.assertEqual(self.mb.pending_outbox(), [])  # no message required

    def test_ready_sets_ready_phase(self):
        agentctl.main(["ready", "--title", "Fix the thing", "--body", "Does the work."])
        [m] = self.mb.pending_outbox()
        self.assertEqual(m.kind, "ready")
        self.assertEqual(m.payload["title"], "Fix the thing")
        self.assertFalse(m.payload["new_pr"])
        st = turns.TurnState.load(self.agent_dir)
        self.assertEqual(st.phase, turns.PHASE_READY)
        self.assertTrue(st.ever_ready)  # gates the no-op auto-idle backstop

    def test_upstream_checkout_queues_and_idles(self):
        agentctl.main(["upstream-checkout", "--project", "embedded", "--branch", "b"])
        [m] = self.mb.pending_outbox()
        self.assertEqual(m.kind, "upstream_checkout")
        self.assertEqual(m.payload, {"project": "embedded", "branch": "b"})
        # Idles like `ask` until the orchestrator makes the checkout.
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_WAITING)

    def test_upstream_checkout_branch_optional(self):
        agentctl.main(["upstream-checkout", "--project", "embedded"])
        [m] = self.mb.pending_outbox()
        self.assertEqual(m.payload, {"project": "embedded"})  # no branch key

    def test_upstream_pr_queues_and_idles(self):
        agentctl.main(
            ["upstream-pr", "--project", "embedded", "--title", "Add C3", "--body", "why"]
        )
        [m] = self.mb.pending_outbox()
        self.assertEqual(m.kind, "upstream_pr")
        self.assertEqual(
            m.payload, {"project": "embedded", "title": "Add C3", "body": "why"}
        )
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_WAITING)

    def test_find_agent_dir_walks_up(self):
        import os

        del os.environ["ISSUEFLEET_AGENT_DIR"]
        sub = self.workspace / "src" / "deep"
        sub.mkdir(parents=True)
        found = agentctl.find_agent_dir(start=sub)
        self.assertEqual(found, self.agent_dir)
        os.environ["ISSUEFLEET_AGENT_DIR"] = str(self.agent_dir)


if __name__ == "__main__":
    unittest.main()
