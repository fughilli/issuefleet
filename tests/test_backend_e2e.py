"""Local end-to-end manager/provider and Linear worker-profile matrix.

Real config, manager HTTP, tool dispatch, git worktrees/remotes, staging,
turnloop/agentctl subprocesses, mailboxes, registry and lifecycle. The external
tracker, forge, Signal, process scheduler and model executables are stand-ins.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from fakes import FakeForge, FakeRunner, FakeSignal, FakeTracker, make_issue

from issuefleet import config
from issuefleet.advisor import ConservativeAdvisor
from issuefleet.agent_runtime.turns import TurnState
from issuefleet.fleet_manager import FleetManager
from issuefleet.gitops import Gitops
from issuefleet.mailbox import Mailbox
from issuefleet.model import IssueLabel
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry


# Executed by the real staged turnloop; calls the real staged agentctl. It
# deliberately checks the CLI session protocol before making a local commit.
_RUNTIME = r'''
import json
import pathlib
import subprocess
import sys
import time

root = pathlib.Path.cwd()
agent = root / ".agent"
state = json.loads((agent / "state.json").read_text())
args = sys.argv[1:]
prompt = sys.stdin.read()
runtime = pathlib.Path(sys.argv[0]).name
session = "thread-" + state["session_uuid"]
if runtime == "codex":
    assert args[0] == "exec" and "--json" in args and args[-1] == "-", args
    if state["turns_taken"]:
        assert args[args.index("resume") + 1] == session, args
    else:
        assert "resume" not in args, args
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
    deadline = time.monotonic() + 5
    while json.loads((agent / "state.json").read_text()).get("runtime_session_id") != session:
        assert time.monotonic() < deadline, "thread ID not persisted before tool execution"
        time.sleep(0.01)
else:
    flag = "--resume" if state["turns_taken"] else "--session-id"
    assert args[args.index(flag) + 1] == state["session_uuid"], args
    print(json.dumps({"type": "system", "subtype": "init", "session_id": state["session_uuid"]}), flush=True)
with (agent / "runtime-calls.jsonl").open("a") as log:
    log.write(json.dumps({"runtime": runtime, "args": args, "prompt": prompt}) + "\n")
ctl = [sys.executable, str(agent / "bin" / "agentctl")]
if state["turns_taken"] == 0:
    assert "Please fix it" in prompt, prompt
    subprocess.run([*ctl, "ask", "Which color should the output use?"], check=True)
elif state["turns_taken"] == 1:
    assert "Use green" in prompt, prompt
    (root / "result.txt").write_text("green\n")
    subprocess.run(["git", "add", "result.txt"], check=True)
    subprocess.run(["git", "-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "Implement green output"], check=True)
    subprocess.run([*ctl, "ready", "--title", "Implement green output", "--body", "Verified output is green."], check=True)
else:
    assert "adopted back" in prompt, prompt
    subprocess.run([*ctl, "idle"], check=True)
if runtime == "codex":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
else:
    print(json.dumps({"type": "result", "is_error": False}), flush=True)
'''


def git(*args, cwd=None):
    proc = subprocess.run(["git", *map(str, args)], cwd=cwd, capture_output=True,
                          text=True, timeout=30)
    if proc.returncode:
        raise AssertionError(f"git failed: {args!r}\n{proc.stderr}")
    return proc.stdout.strip()


class ProjectTracker(FakeTracker):
    def eligible_issues(self, project):
        return [issue for issue in super().eligible_issues(project)
                if issue.project_id == project.linear_project]


class LocalForge(FakeForge):
    def __init__(self, remote):
        super().__init__()
        self.remote = remote

    def push_spec(self):
        return str(self.remote), None


@contextmanager
def model_server(provider):
    """Expose each provider's real HTTP shape with scripted tool decisions."""
    requests = []
    reads = [("read_workers", "list_workers", {}), ("read_pending", "pending_escalations", {})]
    writes = [(f"reply_{n}", "reply_to_worker", {"issue_key": f"FUG-{n}", "text": "Use green."})
              for n in (1, 2)]
    if provider == "openai":
        replies = [{
            "status": "completed", "output": [
                {"type": "reasoning", "id": f"rs_{i}", "summary": [], "encrypted_content": "opaque"},
                *[{"type": "function_call", "id": "fc_" + cid, "call_id": cid,
                   "name": name, "arguments": json.dumps(arguments), "status": "completed"}
                  for cid, name, arguments in calls],
            ]} for i, calls in enumerate((reads, writes))]
        replies.append({"status": "completed", "output": [{
            "type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
            "content": [{"type": "output_text", "text": "Both workers have the answer."}],
        }]})
    else:
        replies = [{"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": cid, "name": name, "input": arguments}
            for cid, name, arguments in calls]} for calls in (reads, writes)]
        replies.append({"stop_reason": "end_turn", "content": [
            {"type": "text", "text": "Both workers have the answer."}]})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, dict(self.headers),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            payload = json.dumps(replies.pop(0) if replies else {"error": "unexpected call"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("issuefleet.agent.API_URL", url + "/v1/messages"), \
                 patch("issuefleet.agent.OPENAI_API_URL", url + "/v1/responses"):
                yield requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


class BackendEndToEndTest(unittest.TestCase):
    def test_manager_provider_and_linear_worker_profile_matrix(self):
        for provider in ("anthropic", "openai"):
            with self.subTest(manager=provider), tempfile.TemporaryDirectory() as temp:
                self.run_lifecycle(Path(temp), provider)

    def run_lifecycle(self, root, provider):
        binary_dir = root / "executables"
        binary_dir.mkdir()
        for runtime in ("claude", "codex"):
            executable = binary_dir / runtime
            executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(_RUNTIME))
            executable.chmod(0o755)

        projects, forges = [], {}
        for n, runtime in enumerate(("claude", "codex"), 1):
            remote, repo = root / f"{runtime}.git", root / runtime
            git("init", "--bare", "--initial-branch=main", remote)
            git("clone", remote, repo)
            (repo / "README.md").write_text("Local fleet fixture.\n")
            git("add", "README.md", cwd=repo)
            git("-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-m", "Initial", cwd=repo)
            git("push", "origin", "main", cwd=repo)
            project = {"name": runtime, "linear_project": runtime, "repo": str(repo),
                       "claim": {"strategy": "label", "value": "agent"}}
            projects.append(project)
            forges[runtime] = LocalForge(remote)
        cfg = config.parse({
            "daemon": {"state_dir": str(root / "state"), "worktree_root": str(root / "worktrees"), "max_workers": 2},
            "agent": {
                "runtime": "claude", "model": "claude-test",
                "args": ["--allowedTools", "Bash"],
                "profile_label_group_id": "group-worker-profile",
                "profiles": [{
                    "name": "codex-astra", "label_id": "label-codex-astra",
                    "runtime": "codex", "model": "gpt-6-astra",
                    "reasoning_effort": "high",
                }],
            },
            "projects": projects,
            "fleet_manager": {"enabled": True, "provider": provider,
                              "base_url": "http://local.invalid", "board_project": "Fleet", "board_team": "FUG",
                              "report_interval_s": 0},
        })
        registry, tracker, runner = Registry(cfg.state_dir), ProjectTracker(), FakeRunner()
        signal = FakeSignal()
        manager = FleetManager(cfg, tracker, signal, ConservativeAdvisor(), registry, agent_key="local-key")
        signal.user_says("baseline", id="baseline")
        manager.tick()
        for n, runtime in enumerate(("claude", "codex"), 1):
            labels = [] if runtime == "claude" else [IssueLabel(
                "label-codex-astra", "Codex Astra", "group-worker-profile", "Worker profile"
            )]
            tracker.add_issue(make_issue(n, project_id=runtime, label_details=labels))
        reconciler = Reconciler(cfg, registry, tracker, forges, Gitops(), runner)
        reconciler.tick()
        self.assertEqual(len(registry.all()), 2)
        records = [registry.get(f"issue-{n}") for n in (1, 2)]
        self.assertEqual([rec.runtime for rec in records], ["claude", "codex"])
        self.assertEqual([rec.runtime_profile for rec in records], [None, "codex-astra"])

        def step(rec, expected=0):
            env = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
                   "ISSUEFLEET_AGENT_DIR": str(Path(rec.worktree) / ".agent")}
            proc = subprocess.run([sys.executable, str(Path(rec.worktree) / ".agent/bin/turnloop"), "step"],
                                  cwd=rec.worktree, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
            return TurnState.load(Path(rec.worktree) / ".agent")

        sessions = {}
        for rec in records:
            state = step(rec)
            self.assertEqual((state.phase, state.turns_taken), ("waiting", 1))
            sessions[rec.issue_id] = (state.session_uuid, state.runtime_session_id)
            # Waiting really idles; a second step must not invoke a model.
            step(rec, expected=10)
        reconciler.tick()
        manager.tick()
        self.assertEqual({p["issue_key"] for p in manager.state["pending"]}, {"FUG-1", "FUG-2"})
        self.assertEqual(len([body for _, body in tracker.posted if "Which color" in body]), 2)

        signal.user_says("Tell both workers to use green.", id="answer")
        with model_server(provider) as requests:
            manager.tick()
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0][0], "/v1/responses" if provider == "openai" else "/v1/messages")
        self.assertEqual(requests[0][2]["model"], "gpt-6-astra" if provider == "openai" else "claude-opus-5")
        self.assertIn("FUG-1", json.dumps(requests[1][2]))
        self.assertIn("FUG-2", json.dumps(requests[1][2]))
        self.assertEqual(manager.state["pending"], [])
        self.assertIn("Both workers have the answer.", signal.sent)
        self.assertEqual(tracker.created, [])  # answering must not file new goals
        for rec in records:
            mailbox = Mailbox(Path(rec.worktree) / ".agent/mailbox")
            self.assertEqual([m.payload["text"] for m in mailbox.pending_inbox() if m.kind == "reply"], ["Use green."])
            state = step(rec)
            self.assertEqual((state.phase, state.turns_taken), ("ready", 2))
            self.assertEqual((state.session_uuid, state.runtime_session_id), sessions[rec.issue_id])
            calls = [json.loads(line) for line in (Path(rec.worktree) / ".agent/runtime-calls.jsonl").read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["args"][calls[0]["args"].index("--model") + 1],
                             "gpt-6-astra" if rec.runtime == "codex" else "claude-test")
            if rec.runtime == "codex":
                self.assertNotIn("--allowedTools", calls[0]["args"])
                self.assertIn('model_reasoning_effort="high"', calls[0]["args"])
            self.assertEqual(git("show", "HEAD:result.txt", cwd=rec.worktree), "green")
            self.assertEqual(git("status", "--porcelain", cwd=rec.worktree), "")
        reconciler.tick()
        records = [registry.get(f"issue-{n}") for n in (1, 2)]
        for rec in records:
            self.assertIsNotNone(rec.pr_number)
            self.assertEqual(git("show", f"{rec.branch}:result.txt", cwd=forges[rec.project].remote), "green")
        self.assertEqual(len([body for _, body in tracker.posted if "Pull request ready" in body]), 2)

        # A daemon restart must retain each backend and conversation identity.
        registry = Registry(cfg.state_dir)
        reconciler = Reconciler(cfg, registry, tracker, forges, Gitops(), runner)
        reconciler.tick()
        self.assertEqual(len(runner.started), 2)
        codex = registry.get("issue-2")
        reconciler.enqueue_release(codex.issue_key)
        reconciler.tick()
        codex = registry.get("issue-2")
        self.assertEqual(codex.phase, "released")
        self.assertFalse(Path(codex.worktree).exists())
        archive = registry.archive_dir_for(codex)
        self.assertEqual(TurnState.load(archive).runtime_session_id, sessions[codex.issue_id][1])
        self.assertTrue((archive / "logs/turn-0002.jsonl").is_file())
        # Existing workers keep their runtime even if the selected profile changes.
        cfg.worker_profiles[0].runtime = config.WorkerRuntimeConfig(
            "claude", "different-model"
        )
        reconciler.enqueue_adopt(codex.issue_key)
        reconciler.tick()
        codex = registry.get("issue-2")
        restored = TurnState.load(Path(codex.worktree) / ".agent")
        self.assertEqual((restored.runtime, restored.model, restored.runtime_session_id),
                         ("codex", "gpt-6-astra", sessions[codex.issue_id][1]))
        self.assertEqual((restored.turns_taken, restored.reasoning_effort), (2, "high"))
        self.assertEqual(step(codex).phase, "idle")
        self.assertEqual(TurnState.load(Path(codex.worktree) / ".agent").runtime_session_id, sessions[codex.issue_id][1])
        reconciler.tick()

        # Merge signals complete the issue and remove the actual git worktree;
        # the runtime transcript remains in the registry archive.
        records = list(registry.all())
        for rec in records:
            forges[rec.project].merge(rec.pr_number)
        reconciler.tick()
        self.assertEqual(registry.all(), [])
        for rec in records:
            self.assertFalse(Path(rec.worktree).exists())
            self.assertTrue((registry.archive_dir_for(rec) / "state.json").is_file())
            self.assertIn((rec.issue_id, "Done"), tracker.state_changes)
            self.assertNotIn(rec.worktree, git("worktree", "list", "--porcelain", cwd=rec.repo))


if __name__ == "__main__":
    unittest.main()
