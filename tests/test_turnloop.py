"""Turn-loop plumbing: live stream-json summarization and run_claude wiring,
exercised against a stub `claude` on PATH."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from issuefleet.agent_runtime import turnloop, turns


class SummarizeEventTest(unittest.TestCase):
    def s(self, obj):
        return turnloop.summarize_event(json.dumps(obj))

    def test_init_event(self):
        out = self.s({"type": "system", "subtype": "init", "session_id": "abcd1234ef", "model": "m"})
        self.assertIn("abcd1234", out)
        self.assertIn("model=m", out)

    def test_assistant_text_and_tool_use(self):
        out = self.s(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I will read  the\nconfig first."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                    ]
                },
            }
        )
        self.assertIn("I will read the config first.", out)
        self.assertIn("→ Bash ls -la", out)

    def test_long_text_truncated(self):
        out = self.s(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 500}]}}
        )
        self.assertLess(len(out), 250)

    def test_result_event_ok_and_error(self):
        ok = self.s({"type": "result", "duration_ms": 12000, "total_cost_usd": 0.5})
        self.assertIn("✓ turn complete", ok)
        self.assertIn("12s", ok)
        self.assertIn("$0.50", ok)
        err = self.s({"type": "result", "is_error": True})
        self.assertIn("✗ turn errored", err)

    def test_tool_results_are_silenced(self):
        self.assertIsNone(self.s({"type": "user", "message": {"content": []}}))

    def test_non_json_passes_through_for_diagnosis(self):
        self.assertEqual(
            turnloop.summarize_event("Error: cannot connect to Anthropic API\n"),
            "Error: cannot connect to Anthropic API",
        )
        self.assertIsNone(turnloop.summarize_event("   \n"))


class RunClaudeTest(unittest.TestCase):
    """run_claude with a stub `claude` script: argv recording, prompt via
    stdin, per-line log capture, exit-code passthrough."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "ws"
        self.agent_dir = self.workspace / ".agent"
        self.agent_dir.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        self.tmp.cleanup()

    def stub_claude(self, exit_code=0):
        stub = self.bin / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf \'%s\\n\' "$@" > "{self.agent_dir}/argv.txt"\n'
            f'cat > "{self.agent_dir}/prompt.txt"\n'
            'echo \'{"type":"system","subtype":"init","session_id":"s1","model":"m"}\'\n'
            'echo \'{"type":"result","duration_ms":1000}\'\n'
            f"exit {exit_code}\n"
        )
        stub.chmod(0o755)

    def state(self, turns_taken=0):
        return turns.TurnState(session_uuid="u-1", turns_taken=turns_taken)

    def test_first_turn_streams_to_jsonl_log(self):
        self.stub_claude()
        rc = turnloop.run_claude("do the thing", self.state(), self.agent_dir)
        self.assertEqual(rc, 0)
        argv = (self.agent_dir / "argv.txt").read_text().split("\n")
        self.assertIn("--session-id", argv)
        self.assertIn("stream-json", argv)
        self.assertNotIn("--resume", argv)
        self.assertEqual((self.agent_dir / "prompt.txt").read_text(), "do the thing")
        log = (self.agent_dir / "logs" / "turn-0001.jsonl").read_text().strip().split("\n")
        self.assertEqual(len(log), 2)
        self.assertIn('"init"', log[0])

    def test_later_turns_resume_the_session(self):
        self.stub_claude()
        rc = turnloop.run_claude("continue", self.state(turns_taken=3), self.agent_dir)
        self.assertEqual(rc, 0)
        argv = (self.agent_dir / "argv.txt").read_text().split("\n")
        self.assertIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertTrue((self.agent_dir / "logs" / "turn-0004.jsonl").is_file())

    def test_claude_failure_code_passes_through(self):
        self.stub_claude(exit_code=3)
        self.assertEqual(turnloop.run_claude("x", self.state(), self.agent_dir), 3)


class ReadyWakeRestoreTest(unittest.TestCase):
    """A ready-idling agent woken by a message that needs no response must
    return to ready after ONE turn — not fall into running phase and grind
    continuation turns until the budget (live-observed loop, 2026-07-30)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "ws"
        self.agent_dir = self.workspace / ".agent"
        self.agent_dir.mkdir(parents=True)
        (self.agent_dir / "brief.md").write_text("# brief")
        from issuefleet.mailbox import Mailbox

        self.mb = Mailbox(self.agent_dir / "mailbox").ensure()
        st = turns.TurnState(session_uuid="u-1", phase=turns.PHASE_READY, turns_taken=5)
        st.save(self.agent_dir)
        self.bin = root / "bin"
        self.bin.mkdir()
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        self.tmp.cleanup()

    def stub_claude(self, extra=""):
        stub = self.bin / "claude"
        stub.write_text(f"#!/bin/sh\ncat > /dev/null\n{extra}\nexit 0\n")
        stub.chmod(0o755)

    def test_silent_wake_returns_to_ready(self):
        self.mb.put_inbox("pr_feedback", {"reviewer": "alice", "text": "LGTM!"})
        self.stub_claude()  # agent emits nothing
        code = turnloop.step(self.agent_dir)
        self.assertEqual(code, 0)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_READY)
        # Next decision idles instead of granting a continuation turn.
        self.assertEqual(turnloop.step(self.agent_dir), turns.EXIT_READY)

    def test_noop_continuation_turns_auto_idle(self):
        # The FUG-13 grind: agent says "nothing left to do", emits nothing,
        # commits nothing — and used to get continuation turns until the
        # budget. Two no-op turns now park it in idle — but only AFTER a
        # first submission (ever_ready).
        st = turns.TurnState.load(self.agent_dir)
        st.phase = turns.PHASE_RUNNING
        st.turns_taken = 3
        st.ever_ready = True
        st.save(self.agent_dir)
        self.stub_claude()  # does nothing at all
        self.assertEqual(turnloop.step(self.agent_dir), 0)  # no-op 1
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_RUNNING)
        self.assertEqual(turnloop.step(self.agent_dir), 0)  # no-op 2 -> idle
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_IDLE)
        self.assertEqual(turnloop.step(self.agent_dir), turns.EXIT_READY)  # parked

    def test_pre_submission_exploration_is_never_parked(self):
        # Live misfire: a worker exploring the codebase (quiet turns, no
        # commits yet, nothing submitted) was parked at turn 3. Before
        # ever_ready, the auto-turn budget is the only brake.
        st = turns.TurnState.load(self.agent_dir)
        st.phase = turns.PHASE_RUNNING
        st.turns_taken = 1
        st.save(self.agent_dir)  # ever_ready defaults False
        self.stub_claude()
        for _ in range(4):  # way past MAX_NOOP_TURNS
            self.assertEqual(turnloop.step(self.agent_dir), 0)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_RUNNING)

    def test_failed_turns_are_never_parked_as_idle(self):
        # Live incident: every turn failed instantly (root-refused claude);
        # failures counted as no-ops and the worker parked into an
        # innocent-looking idle, masking the outage.
        st = turns.TurnState.load(self.agent_dir)
        st.phase = turns.PHASE_RUNNING
        st.turns_taken = 1
        st.ever_ready = True  # even in the parkable regime
        st.save(self.agent_dir)
        stub = self.bin / "claude"
        stub.write_text("#!/bin/sh\ncat > /dev/null\nexit 1\n")
        stub.chmod(0o755)
        for _ in range(4):
            self.assertEqual(turnloop.step(self.agent_dir), turns.EXIT_ERROR)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_RUNNING)

    def test_wake_emits_gear_ack_and_settling_emits_check(self):
        # 👀 (orchestrator, elsewhere) → ⚙️ when the agent starts a turn on the
        # wake → ✅ when it settles. A silent wake from ready still brackets
        # with ⚙️/✅ so the sender never sees a dangling gear.
        self.mb.put_inbox("reply", {"author": "kevin", "text": "one more tweak"})
        self.stub_claude()  # agent emits nothing, so the wake returns to ready
        self.assertEqual(turnloop.step(self.agent_dir), 0)
        acks = [m.payload for m in self.mb.pending_outbox() if m.kind == "ack"]
        self.assertEqual(len(acks), 2)
        self.assertTrue(acks[0]["text"].startswith("⚙️"))
        self.assertEqual(acks[0]["activity"], "thought")  # working → stays active
        self.assertTrue(acks[1]["text"].startswith("✅"))
        # ✅ settles the Linear session to `complete`, not a lingering "Working…"
        # thought that would eventually false-error on timeout (FUG-98).
        self.assertEqual(acks[1]["activity"], "response")
        # The cycle is closed: working_acked cleared, ready to fire again next time.
        self.assertFalse(turns.TurnState.load(self.agent_dir).working_acked)

    def test_continuation_turns_do_not_re_ack(self):
        # ⚙️ fires once per work cycle, not on every self-driven turn.
        st = turns.TurnState.load(self.agent_dir)
        st.phase = turns.PHASE_RUNNING
        st.working_acked = True  # mid-cycle already
        st.turns_taken = 2
        st.save(self.agent_dir)
        self.stub_claude()
        self.assertEqual(turnloop.step(self.agent_dir), 0)
        self.assertEqual([m for m in self.mb.pending_outbox() if m.kind == "ack"], [])

    def test_wake_from_idle_restores_idle(self):
        st = turns.TurnState.load(self.agent_dir)
        st.phase = turns.PHASE_IDLE
        st.save(self.agent_dir)
        self.mb.put_inbox("reply", {"author": "kevin", "text": "please pick this up"})
        self.stub_claude()
        self.assertEqual(turnloop.step(self.agent_dir), 0)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_IDLE)

    def test_responsive_wake_keeps_working(self):
        self.mb.put_inbox("pr_feedback", {"reviewer": "bob", "text": "rename this please"})
        # The agent posts a status during the turn (simulated by the stub
        # dropping a validly-named message into the outbox). Seq 2: the wake's
        # ⚙️ acknowledgment already took seq 1 before the turn ran.
        outbox_file = self.mb.outbox / "000002-status-aaaaaaaaaaaa.json"
        self.stub_claude(
            extra=f"printf '%s' '{json.dumps({'seq': 2, 'kind': 'status', 'id': 'aaaaaaaaaaaa', 'ts': 't', 'payload': {'text': 'on it'}})}' > {outbox_file}"
        )
        code = turnloop.step(self.agent_dir)
        self.assertEqual(code, 0)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_RUNNING)
        # The ⚙️ ack was emitted, and the responsive turn left it awaiting ✅.
        acks = [m.payload["text"] for m in self.mb.pending_outbox() if m.kind == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0].startswith("⚙️"))
        self.assertTrue(turns.TurnState.load(self.agent_dir).working_acked)


class PreflightGitTest(unittest.TestCase):
    """FUG-116: a worker whose git-common-dir mount was lost on a restart must
    fail the preflight and exit, so the orchestrator relaunches it rather than
    letting it wedge on 'not a git repository'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "ws"
        self.agent_dir = self.workspace / ".agent"
        self.agent_dir.mkdir(parents=True)
        # Keep retries fast and non-blocking for the broken-path assertions.
        self._orig_tries = turnloop.GIT_PREFLIGHT_TRIES
        self._orig_sleep = turnloop.GIT_PREFLIGHT_SLEEP_S
        turnloop.GIT_PREFLIGHT_TRIES = 2
        turnloop.GIT_PREFLIGHT_SLEEP_S = 0

    def tearDown(self):
        turnloop.GIT_PREFLIGHT_TRIES = self._orig_tries
        turnloop.GIT_PREFLIGHT_SLEEP_S = self._orig_sleep
        self.tmp.cleanup()

    def _make_healthy_repo(self):
        import subprocess as sp

        sp.run(["git", "init", "-q", str(self.workspace)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sp.run(["git", "-C", str(self.workspace), "config", k, v], check=True)
        (self.workspace / "f").write_text("x")
        sp.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        sp.run(["git", "-C", str(self.workspace), "commit", "-qm", "init"], check=True)

    def test_healthy_worktree_passes(self):
        self._make_healthy_repo()
        self.assertTrue(turnloop.preflight_git(self.workspace))

    def test_unmounted_gitdir_fails(self):
        # The exact restart symptom: .git points at a host path that isn't
        # here, so every git command fails.
        (self.workspace / ".git").write_text("gitdir: /nonexistent/repos/x/.git/worktrees/y\n")
        self.assertFalse(turnloop.preflight_git(self.workspace))

    def test_run_exits_error_on_broken_git(self):
        (self.workspace / ".git").write_text("gitdir: /nonexistent/repos/x/.git/worktrees/y\n")
        self.assertEqual(turnloop.run(self.agent_dir), turns.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
