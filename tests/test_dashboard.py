"""Dashboard: snapshot assembly, transcript parsing, and end-to-end HTTP
against a real server on an ephemeral port (offline: loopback only, no
daemon, no credentials)."""

import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from issuefleet.dashboard import (
    DashboardServer,
    FleetView,
    parse_transcript,
    turn_files,
    worker_snapshot,
)
from issuefleet.model import WorkerRecord
from issuefleet.registry import Registry
from issuefleet.runner import TmuxRunner


def make_record(worktree, key="FUG-1", issue_id="i1", **kw):
    base = dict(
        issue_id=issue_id,
        issue_key=key,
        issue_title="Fix the thing",
        issue_url="https://linear.app/x/issue/" + key,
        project="splanc",
        repo="/repos/splanc",
        branch=f"agent/{key.lower()}-fix",
        worktree=str(worktree),
        base_ref="main",
        session_uuid="00000000-0000-0000-0000-000000000001",
        tmux_session=f"issuefleet-splanc-{key}",
    )
    base.update(kw)
    return WorkerRecord(**base)


def provision_worktree(root: Path, key="FUG-1", turns=None, state=None):
    """Lay down a fake worktree's .agent dir (state.json, logs, mailbox)."""
    agent = root / ".agent"
    (agent / "logs").mkdir(parents=True)
    (agent / "mailbox").mkdir(parents=True)
    st = {"phase": "running", "turns_taken": 3, "auto_turns": 1, "max_auto_turns": 50}
    if state:
        st.update(state)
    (agent / "state.json").write_text(json.dumps(st))
    for n, lines in (turns or {}).items():
        (agent / "logs" / f"turn-{n:04d}.jsonl").write_text("\n".join(lines))
    return root


ASSISTANT_LINE = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "Looking at the code now"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
    ]},
})
TOOL_RESULT_LINE = json.dumps({
    "type": "user",
    "message": {"content": [
        {"type": "tool_result", "content": "total 0\nfile.py", "is_error": False},
    ]},
})
RESULT_LINE = json.dumps({
    "type": "result", "is_error": False, "duration_ms": 4200, "total_cost_usd": 0.12,
})
INIT_LINE = json.dumps({
    "type": "system", "subtype": "init", "model": "claude-opus", "session_id": "abcdef1234",
})


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_reads_state_and_mailbox(self):
        wt = provision_worktree(self.root / "wt", turns={1: [INIT_LINE], 2: [ASSISTANT_LINE]})
        rec = make_record(wt, pr_number=7, pr_url="https://gh/pr/7", restarts=2)
        # A stub runner that never shells out to tmux.
        runner = TmuxRunner(log_dir=self.root / "logs")
        runner.alive = lambda r: True
        snap = worker_snapshot(rec, runner)
        self.assertEqual(snap["issue_key"], "FUG-1")
        self.assertEqual(snap["turn_phase"], "running")
        self.assertEqual(snap["turns_taken"], 3)
        self.assertTrue(snap["alive"])
        self.assertEqual(snap["pr_number"], 7)
        self.assertEqual(snap["restarts"], 2)
        self.assertIsNotNone(snap["last_activity_s"])

    def test_snapshot_tolerates_missing_state(self):
        wt = (self.root / "bare")
        (wt / ".agent" / "logs").mkdir(parents=True)
        rec = make_record(wt)
        runner = TmuxRunner(log_dir=self.root / "logs")
        runner.alive = lambda r: False
        snap = worker_snapshot(rec, runner)
        self.assertIsNone(snap["turn_phase"])
        self.assertIsNone(snap["last_activity_s"])
        self.assertFalse(snap["alive"])

    def test_turn_files_sorted(self):
        wt = provision_worktree(self.root / "wt", turns={2: ["x"], 1: ["y"], 10: ["z"]})
        self.assertEqual(turn_files(wt / ".agent"), [1, 2, 10])


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "turn.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_all_event_kinds(self):
        self.p.write_text("\n".join([INIT_LINE, ASSISTANT_LINE, TOOL_RESULT_LINE, RESULT_LINE]))
        events = parse_transcript(self.p)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["system", "text", "tool_use", "tool_result", "result"])
        self.assertEqual(events[1]["text"], "Looking at the code now")
        self.assertEqual(events[2]["name"], "Bash")
        self.assertIn("file.py", events[3]["text"])
        self.assertAlmostEqual(events[4]["cost"], 0.12)

    def test_partial_final_line_tolerated(self):
        # A live turn: the last line is half-written JSON.
        self.p.write_text(ASSISTANT_LINE + "\n" + '{"type":"assist')
        events = parse_transcript(self.p)
        # The good line parsed; the broken one became a raw event, not a crash.
        self.assertEqual(events[0]["kind"], "text")
        self.assertEqual(events[-1]["kind"], "raw")


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        wt = provision_worktree(
            self.state_dir / "worktrees" / "FUG-1",
            turns={1: [INIT_LINE, ASSISTANT_LINE, RESULT_LINE]},
        )
        reg = Registry(self.state_dir)
        reg.add(make_record(wt, pr_number=7, pr_url="https://gh/pr/7"))
        # Pane log the dashboard tails.
        (self.state_dir / "logs").mkdir(exist_ok=True)
        (self.state_dir / "logs" / "issuefleet-splanc-FUG-1.log").write_text("pane output here")

        self.stopped: list[str] = []
        self.view = FleetView(self.state_dir, stop_cb=self.stopped.append)
        # Never shell to tmux in tests.
        self.view.runner.alive = lambda r: True
        self.server = DashboardServer(bind="127.0.0.1", port=0, view=self.view).start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode(), resp.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), None

    def test_healthz(self):
        code, body, _ = self.get("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body, "ok")

    def test_index_lists_worker(self):
        code, body, _ = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("FUG-1", body)
        self.assertIn("Fix the thing", body)

    def test_api_workers_json(self):
        code, body, _ = self.get("/api/workers")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data[0]["issue_key"], "FUG-1")
        self.assertEqual(data[0]["pr_number"], 7)

    def test_worker_detail_and_transcript(self):
        code, body, _ = self.get("/worker/FUG-1")
        self.assertEqual(code, 200)
        self.assertIn("Stop worker", body)
        self.assertIn("turn 1", body)
        self.assertIn("pane output here", body)

        code, body, _ = self.get("/worker/FUG-1/turn/1")
        self.assertEqual(code, 200)
        self.assertIn("Looking at the code now", body)
        self.assertIn("Bash", body)

    def test_raw_turn(self):
        code, body, _ = self.get("/worker/FUG-1/raw/1")
        self.assertEqual(code, 200)
        self.assertIn('"type": "assistant"', body)

    def test_unknown_worker_404(self):
        code, _, _ = self.get("/worker/FUG-999")
        self.assertEqual(code, 404)

    def test_missing_turn_404(self):
        code, _, _ = self.get("/worker/FUG-1/turn/99")
        self.assertEqual(code, 404)

    def test_stop_is_post_only(self):
        # A GET to the stop path is not the stop action (and must not fire it).
        code, _, _ = self.get("/worker/FUG-1/stop")
        self.assertEqual(code, 404)
        self.assertEqual(self.stopped, [])

    def test_stop_post_enqueues(self):
        code, _, final = self.get("/worker/FUG-1/stop", method="POST")
        # 303 -> urllib follows to the index.
        self.assertEqual(code, 200)
        self.assertEqual(self.stopped, ["FUG-1"])

    def test_stop_unknown_worker_does_not_enqueue(self):
        code, _, _ = self.get("/worker/FUG-999/stop", method="POST")
        self.assertEqual(code, 404)
        self.assertEqual(self.stopped, [])

    def test_html_is_escaped(self):
        # Inject a worker whose title carries markup; it must be escaped.
        wt = provision_worktree(self.state_dir / "worktrees" / "FUG-2", turns={})
        reg = Registry(self.state_dir)
        reg.add(make_record(wt, key="FUG-2", issue_id="i2",
                            issue_title="<script>alert(1)</script>"))
        code, body, _ = self.get("/")
        self.assertEqual(code, 200)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class ProjectsPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        (self.state_dir / "logs").mkdir(exist_ok=True)
        self.config_path = self.state_dir / "config.toml"
        self.config_path.write_text(
            "[[projects]]\n"
            'name = "splanc"\n'
            'linear_project = "Splanc"\n'
            'repo = "/repos/splanc"\n'
            'claim = { strategy = "agent" }\n'
        )
        self.enqueued: list[dict] = []
        self.results: list[dict] = []
        self.view = FleetView(
            self.state_dir,
            config_path=self.config_path,
            allow_add_project=True,
            add_project_cb=self.enqueued.append,
            project_results_cb=lambda: self.results,
        )
        self.server = DashboardServer(bind="127.0.0.1", port=0, view=self.view).start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read().decode(), resp.geturl()

    def post_form(self, path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode(), resp.geturl()

    def test_index_links_to_projects(self):
        _, body, _ = self.get("/")
        self.assertIn("/projects", body)

    def test_projects_page_lists_configured(self):
        code, body, _ = self.get("/projects")
        self.assertEqual(code, 200)
        self.assertIn("splanc", body)
        self.assertIn("Add a project", body)
        # This page must NOT auto-refresh (it holds a form).
        self.assertNotIn("http-equiv='refresh'", body)

    def test_add_valid_enqueues(self):
        code, body, final = self.post_form("/projects/add", {
            "name": "led-mapper",
            "linear_project": "LED Mapper",
            "repo": "/repos/led_mapper",
            "git_url": "https://github.com/o/led_mapper",
            "base_ref": "main",
            "claim_strategy": "state",
            "claim_value": "Ready for agent",
            "max_workers": "2",
        })
        self.assertEqual(code, 200)  # 303 -> followed to /projects
        self.assertIn("/projects", final)
        self.assertEqual(len(self.enqueued), 1)
        spec = self.enqueued[0]
        self.assertEqual(spec["name"], "led-mapper")
        self.assertEqual(spec["claim"], {"strategy": "state", "value": "Ready for agent"})
        self.assertEqual(spec["git_url"], "https://github.com/o/led_mapper")
        self.assertEqual(spec["max_workers"], "2")

    def test_add_agent_claim_omits_value(self):
        self.post_form("/projects/add", {
            "name": "foo", "linear_project": "Foo", "repo": "/repos/foo",
            "claim_strategy": "agent", "claim_value": "",
        })
        self.assertEqual(self.enqueued[0]["claim"], {"strategy": "agent"})

    def test_add_duplicate_name_rejected(self):
        code, _, final = self.post_form("/projects/add", {
            "name": "splanc", "linear_project": "Splanc", "repo": "/repos/splanc",
            "claim_strategy": "agent",
        })
        self.assertIn("error=", final)
        self.assertEqual(self.enqueued, [])

    def test_add_missing_field_rejected(self):
        _, _, final = self.post_form("/projects/add", {
            "name": "x", "linear_project": "", "repo": "/repos/x",
            "claim_strategy": "agent",
        })
        self.assertIn("error=", final)
        self.assertEqual(self.enqueued, [])

    def test_results_rendered(self):
        self.results.append({"name": "led-mapper", "ok": True,
                             "detail": "cloned from https://x", "ts": 0})
        self.results.append({"name": "bad", "ok": False, "detail": "boom", "ts": 0})
        _, body, _ = self.get("/projects")
        self.assertIn("led-mapper", body)
        self.assertIn("cloned from", body)
        self.assertIn("boom", body)


class ProjectsDisabledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        (self.state_dir / "logs").mkdir(exist_ok=True)
        self.enqueued: list[dict] = []
        # No add_project_cb / allow_add_project -> add surface is off.
        self.view = FleetView(self.state_dir)
        self.server = DashboardServer(bind="127.0.0.1", port=0, view=self.view).start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_page_renders_without_config(self):
        with urllib.request.urlopen(self.base + "/projects", timeout=5) as resp:
            body = resp.read().decode()
        self.assertIn("Disabled", body)
        self.assertNotIn("<form method='post' action='/projects/add'", body)

    def test_add_when_disabled_returns_error(self):
        # add_project on a view with no callback rejects rather than raising.
        self.assertIsNotNone(self.view.add_project({"name": "x"}))


if __name__ == "__main__":
    unittest.main()
