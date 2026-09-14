"""Turn-loop plumbing: live stream-json summarization and run_claude wiring,
exercised against a stub `claude` on PATH."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from issuefleet.agent_runtime import runtimes, turnloop, turns
from issuefleet.mailbox import Mailbox


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

    def test_model_effort_and_legacy_arguments(self):
        self.stub_claude()
        state = self.state()
        state.model = "claude-opus-5"
        state.reasoning_effort = "high"
        state.claude_args = ["--dangerously-skip-permissions"]
        state.runtime_args = ["--max-turns", "5"]
        self.assertEqual(turnloop.run_claude("x", state, self.agent_dir), 0)
        argv = (self.agent_dir / "argv.txt").read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--max-turns", argv)


class RunCodexTest(unittest.TestCase):
    """Exercise actual subprocess pipes and a fresh turnloop process each turn.

    The executable fixture implements Codex's JSONL protocol, including
    failures with exit zero and an event/state handshake before completion.
    """

    THREAD = "d5f8448e-bc53-4d50-b97d-f4d8e4737343"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.agent_dir = root / "workspace" / ".agent"
        self.agent_dir.mkdir(parents=True)
        (self.agent_dir / "brief.md").write_text("Implement the requested feature.")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.env = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p),
            "ISSUEFLEET_AGENT_DIR": str(self.agent_dir),
        })
        self.env.start()
        turns.TurnState(
            runtime="codex", model="gpt-6-astra", reasoning_effort="high",
            session_uuid="fleet-identity-is-not-codex-thread-id",
            claude_args=["--must-never-reach-codex"],
            runtime_args=["--dangerously-bypass-approvals-and-sandbox"],
        ).save(self.agent_dir)
        self.mb = Mailbox(self.agent_dir / "mailbox").ensure()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def stub(self, body):
        executable = self.bin / "codex"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "from issuefleet.agent_runtime.turns import TurnState\n"
            "agent = Path(os.environ['ISSUEFLEET_AGENT_DIR'])\n"
            "(agent / 'argv.json').write_text(json.dumps(sys.argv[1:]))\n"
            "(agent / 'prompt.txt').write_text(sys.stdin.read())\n"
            "def emit(event):\n"
            "    print(json.dumps(event), flush=True)\n"
            + body + "\n"
        )
        executable.chmod(0o755)

    def events(self, *events, exit_code=0):
        self.stub("\n".join(f"emit({event!r})" for event in events) + f"\nsys.exit({exit_code})")

    def start(self):
        return {"type": "thread.started", "thread_id": self.THREAD}

    def complete(self):
        return {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}

    def run_runtime(self, prompt="original request"):
        return turnloop.run_runtime(prompt, turns.TurnState.load(self.agent_dir), self.agent_dir)

    def subprocess_step(self):
        return subprocess.run(
            [sys.executable, "-m", "issuefleet.agent_runtime.turnloop", "step"],
            cwd=self.agent_dir.parent, text=True, capture_output=True, timeout=10,
        )

    def test_first_and_resumed_turn_end_to_end_with_agentctl(self):
        subprocess.run(["git", "init", "-q", str(self.agent_dir.parent)], check=True)
        self.events(self.start(), {"type": "item.completed", "item": {
            "type": "agent_message", "text": "Working on the feature."}}, self.complete())
        first = self.subprocess_step()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Working on the feature.", first.stdout)
        first_argv = json.loads((self.agent_dir / "argv.json").read_text())
        self.assertEqual(first_argv[:2], ["exec", "--json"])
        self.assertNotIn("resume", first_argv)
        self.assertNotIn("--must-never-reach-codex", first_argv)
        self.assertEqual(first_argv[-1], "-")
        self.assertIn('model_reasoning_effort="high"', first_argv)
        self.assertEqual(first_argv[first_argv.index("--model") + 1], "gpt-6-astra")
        self.assertEqual((self.agent_dir / "prompt.txt").read_text(),
                         "Implement the requested feature.")
        self.assertEqual(turns.TurnState.load(self.agent_dir).runtime_session_id, self.THREAD)
        self.assertFalse((self.agent_dir / "pending-codex-turn.json").exists())

        self.mb.put_inbox("reply", {"author": "alice", "text": "Submit the feature now."})
        self.stub(
            f"emit({self.start()!r})\n"
            "subprocess.run([sys.executable, '-m', 'issuefleet.agent_runtime.agentctl',\n"
            "    'ready', '--title', 'Implement feature', '--body', 'Verified it.'], check=True)\n"
            f"emit({self.complete()!r})"
        )
        resumed = self.subprocess_step()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        argv = json.loads((self.agent_dir / "argv.json").read_text())
        self.assertEqual(argv[-3:], ["resume", self.THREAD, "-"])
        self.assertNotIn("--last", argv)
        self.assertIn("Submit the feature now.", (self.agent_dir / "prompt.txt").read_text())
        state = turns.TurnState.load(self.agent_dir)
        self.assertEqual(state.turns_taken, 2)
        self.assertEqual(state.phase, turns.PHASE_READY)
        self.assertTrue(state.ever_ready)
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_READY)
        ready = [m for m in self.mb.pending_outbox() if m.kind == "ready"]
        self.assertEqual(ready[0].payload["title"], "Implement feature")
        self.assertEqual(len(list((self.agent_dir / "logs").glob("*.jsonl"))), 2)

    def test_thread_id_is_saved_before_exit_without_clobbering_agentctl(self):
        # The child refuses to finish until it sees the session in state.
        # Writing it only after wait() would deadlock/fail this handshake.
        self.stub(
            "subprocess.run([sys.executable, '-m', 'issuefleet.agent_runtime.agentctl',\n"
            "    'ask', 'Which database?'], check=True)\n"
            f"emit({self.start()!r})\n"
            "deadline = time.monotonic() + 3\n"
            "while time.monotonic() < deadline:\n"
            "    state = TurnState.load(agent)\n"
            f"    if state.runtime_session_id == {self.THREAD!r}:\n"
            "        assert state.phase == 'waiting', state.phase\n"
            "        (agent / 'persisted-before-exit').touch()\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "else:\n"
            "    sys.exit(9)\n"
            f"emit({self.complete()!r})"
        )
        self.assertEqual(self.run_runtime(), 0)
        self.assertTrue((self.agent_dir / "persisted-before-exit").exists())
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_WAITING)

    def test_failed_first_turn_resumes_id_even_with_zero_completed_turns(self):
        self.events(self.start(), exit_code=7)
        self.assertEqual(self.run_runtime(), 7)
        self.assertEqual(turns.TurnState.load(self.agent_dir).turns_taken, 0)
        self.events(self.start(), self.complete())
        self.assertEqual(self.run_runtime(), 0)
        argv = json.loads((self.agent_dir / "argv.json").read_text())
        self.assertEqual(argv[-3:], ["resume", self.THREAD, "-"])

    def test_failed_turn_retries_original_prompt_and_new_messages_once(self):
        self.mb.put_inbox("reply", {"author": "alice", "text": "Keep this requirement."})
        self.events(self.start(), {"type": "turn.failed", "error": {"message": "quota"}})
        first = self.subprocess_step()
        self.assertEqual(first.returncode, turns.EXIT_ERROR, first.stdout + first.stderr)
        self.assertTrue((self.agent_dir / "pending-codex-turn.json").exists())
        self.mb.put_inbox("reply", {"author": "alice", "text": "And this new requirement."})
        self.events(self.start(), exit_code=3)
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
        self.events(self.start(), self.complete())
        self.assertEqual(self.subprocess_step().returncode, 0)
        prompt = (self.agent_dir / "prompt.txt").read_text()
        self.assertIn("Implement the requested feature.", prompt)
        self.assertEqual(prompt.count("Keep this requirement."), 1)
        self.assertEqual(prompt.count("And this new requirement."), 1)
        self.assertFalse((self.agent_dir / "pending-codex-turn.json").exists())

    def test_crash_before_thread_id_retries_original_prompt_without_resume(self):
        self.mb.put_inbox("reply", {"author": "alice", "text": "Initial requirement."})
        self.events(exit_code=2)
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
        self.events(self.start(), self.complete())
        self.assertEqual(self.subprocess_step().returncode, 0)
        argv = json.loads((self.agent_dir / "argv.json").read_text())
        self.assertNotIn("resume", argv)
        prompt = (self.agent_dir / "prompt.txt").read_text()
        self.assertIn("Implement the requested feature.", prompt)
        self.assertIn("Initial requirement.", prompt)

    def test_killed_turnloop_resumes_captured_thread_and_retains_crash_log(self):
        self.stub(
            f"emit({self.start()!r})\n"
            "deadline = time.monotonic() + 3\n"
            "while time.monotonic() < deadline:\n"
            f"    if TurnState.load(agent).runtime_session_id == {self.THREAD!r}:\n"
            "        (agent / 'safe-to-kill').touch()\n"
            "        time.sleep(30)\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "else:\n"
            "    sys.exit(9)"
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "issuefleet.agent_runtime.turnloop", "step"],
            cwd=self.agent_dir.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not (self.agent_dir / "safe-to-kill").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("turnloop did not persist the session before interruption")
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        interrupted = turns.TurnState.load(self.agent_dir)
        self.assertEqual(interrupted.runtime_session_id, self.THREAD)
        self.assertEqual(interrupted.turns_taken, 0)
        crash_log = self.agent_dir / "logs" / "turn-0001.jsonl"
        original_log = crash_log.read_text()
        self.events(self.start(), self.complete())
        resumed = self.subprocess_step()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        argv = json.loads((self.agent_dir / "argv.json").read_text())
        self.assertEqual(argv[-3:], ["resume", self.THREAD, "-"])
        self.assertEqual((self.agent_dir / "prompt.txt").read_text(),
                         "Implement the requested feature.")
        self.assertEqual(crash_log.read_text(), original_log)
        self.assertTrue((self.agent_dir / "logs" / "turn-0001-retry-01.jsonl").is_file())

    def test_failure_event_with_exit_zero_is_failure_and_logs_remain_raw(self):
        failed = {"type": "turn.failed", "error": {"message": "request rejected"}}
        self.events(self.start(), failed)
        self.assertNotEqual(self.run_runtime(), 0)
        log = (self.agent_dir / "logs" / "turn-0001.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line) for line in log], [self.start(), failed])

    def test_error_event_cannot_be_hidden_by_later_completion(self):
        self.events(self.start(), {"type": "error", "message": "connection lost"}, self.complete())
        self.assertNotEqual(self.run_runtime(), 0)

    def test_runtime_failures_exhaust_bounded_retries_without_false_idle(self):
        self.events(self.start(), {"type": "turn.failed", "error": {"message": "unavailable"}})
        with patch.object(turnloop, "preflight_git", return_value=True), \
                patch.object(turnloop.time, "sleep"), \
                patch.object(turnloop, "MAX_RUNTIME_RETRIES", 2):
            self.assertEqual(turnloop.run(self.agent_dir), turns.EXIT_ERROR)
        state = turns.TurnState.load(self.agent_dir)
        self.assertEqual(state.turns_taken, 3)
        self.assertEqual(state.phase, turns.PHASE_RUNNING)
        self.assertEqual(state.runtime_session_id, self.THREAD)

    def test_ask_then_failed_turn_stays_error_after_restart_until_reply(self):
        self.stub(
            f"emit({self.start()!r})\n"
            "subprocess.run([sys.executable, '-m', 'issuefleet.agent_runtime.agentctl',\n"
            "    'ask', 'Which database?'], check=True)\n"
            "emit({'type': 'turn.failed', 'error': {'message': 'disconnected'}})"
        )
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
        self.assertEqual(turns.TurnState.load(self.agent_dir).phase, turns.PHASE_WAITING)
        # A new turnloop process must not mistake the stored waiting phase
        # for healthy idle and thereby disable all subsequent crash handling.
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
        self.assertTrue((self.agent_dir / "pending-codex-turn.json").exists())
        self.mb.put_inbox("reply", {"author": "alice", "text": "Use PostgreSQL."})
        self.events(self.start(), self.complete())
        self.assertEqual(self.subprocess_step().returncode, 0)
        self.assertIn("Use PostgreSQL.", (self.agent_dir / "prompt.txt").read_text())
        self.assertIn("Implement the requested feature.", (self.agent_dir / "prompt.txt").read_text())
        self.assertFalse((self.agent_dir / "pending-codex-turn.json").exists())

    def test_unfinished_turn_cannot_hide_in_ready_idle_or_budget(self):
        self.events(self.start(), {"type": "turn.failed", "error": {"message": "failed"}})
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
        for phase in (turns.PHASE_READY, turns.PHASE_IDLE, turns.PHASE_RUNNING):
            with self.subTest(phase=phase):
                state = turns.TurnState.load(self.agent_dir)
                state.phase = phase
                state.auto_turns = state.max_auto_turns
                state.save(self.agent_dir)
                self.assertEqual(self.subprocess_step().returncode, turns.EXIT_ERROR)
                self.assertEqual(turns.TurnState.load(self.agent_dir).phase, phase)
        self.mb.put_inbox("shutdown", {"reason": "released"})
        self.assertEqual(self.subprocess_step().returncode, turns.EXIT_SHUTDOWN)

    def test_exit_zero_requires_completed_event_and_persisted_id(self):
        for events in ((self.start(),), (self.complete(),), ()):
            with self.subTest(events=events):
                state = turns.TurnState.load(self.agent_dir)
                state.runtime_session_id = None
                state.save(self.agent_dir)
                self.events(*events)
                self.assertNotEqual(self.run_runtime(), 0)

    def test_wrong_thread_does_not_replace_persisted_identity(self):
        state = turns.TurnState.load(self.agent_dir)
        state.runtime_session_id = "expected-id"
        state.save(self.agent_dir)
        self.events(self.start(), self.complete())
        self.assertNotEqual(self.run_runtime(), 0)
        self.assertEqual(turns.TurnState.load(self.agent_dir).runtime_session_id, "expected-id")

    def test_non_object_json_and_stderr_are_diagnostic_not_crashes(self):
        self.stub(f"print('null', flush=True)\nprint('warning', file=sys.stderr, flush=True)\n"
                  f"emit({self.start()!r})\nemit({self.complete()!r})")
        self.assertEqual(self.run_runtime(), 0)
        self.assertIn("warning", (self.agent_dir / "logs" / "turn-0001.jsonl").read_text())

    def test_no_ephemeral_or_implicit_resume_arguments(self):
        for arg in ("--ephemeral", "--last", "--worktree", "--cd=/elsewhere", "-C/tmp", "--", "fork"):
            with self.subTest(arg=arg):
                state = turns.TurnState(runtime="codex", runtime_args=[arg])
                with self.assertRaises(ValueError):
                    runtimes.command(state)

    def test_explicit_model_and_effort_conflicts_are_rejected(self):
        for runtime, args in (("claude", ["--model", "other"]),
                              ("codex", ["-mother"]),
                              ("codex", ["-c", "model=other"]),
                              ("codex", ["-c", "model_reasoning_effort=low"])):
            with self.subTest(runtime=runtime, args=args):
                with self.assertRaises(ValueError):
                    runtimes.command(turns.TurnState(
                        runtime=runtime, model="chosen", reasoning_effort="high", runtime_args=args))

    def test_attached_short_sandbox_flag_is_respected(self):
        argv = runtimes.command(turns.TurnState(runtime="codex", runtime_args=["-sworkspace-write"]))
        self.assertIn("-sworkspace-write", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_exec_only_flags_are_rejected_from_shared_runtime_arguments(self):
        for args in (["--skip-git-repo-check"], ["--output-schema", "schema.json"],
                     ["--output-last-message=result.txt"], ["-o", "result.txt"],
                     ["-oresult.txt"], ["--ignore-user-config"], ["--ignore-rules"],
                     ["--thread-source", "worker"], ["--color", "never"]):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    runtimes.command(turns.TurnState(runtime="codex", runtime_args=args))


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
