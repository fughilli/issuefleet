"""Doctor and dry-run plan, offline via injected fakes."""

import io
import tempfile
import unittest
from pathlib import Path

from fakes import FakeForge, FakeGit, FakeRunner, FakeTracker, make_issue

from issuefleet import config
from issuefleet.doctor import run_doctor
from issuefleet.mailbox import Mailbox
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry


class FakeDoctorTracker(FakeTracker):
    """FakeTracker + the doctor-only surface (viewer details, open_issues,
    team states)."""

    def viewer(self):
        return {"id": self.viewer_id, "name": "fleet-bot", "email": "bot@example.com"}

    def open_issues(self, project):
        return [i for i in self.issues.values() if i.open]

    def _states_for_issue(self, issue_id):
        return {"todo": "s0", "in progress": "s1", "done": "s2"}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "daemon": {
                    "state_dir": str(root / "state"),
                    "worktree_root": str(root / "worktrees"),
                    "max_workers": 1,
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
        self.tracker = FakeDoctorTracker()
        self.forge = FakeForge()
        self.git = FakeGit(root)
        self.runner = FakeRunner()
        self.rec = Reconciler(
            self.cfg, self.registry, self.tracker, {"splanc": self.forge}, self.git, self.runner
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_reports_claims_and_queue_without_mutating(self):
        self.tracker.add_issue(make_issue(1))
        self.tracker.add_issue(make_issue(2))
        lines = self.rec.plan()
        text = "\n".join(lines)
        self.assertIn("FUG-1: would claim", text)
        self.assertIn("FUG-2: eligible but waiting", text)
        # Nothing actually happened.
        self.assertEqual(self.registry.all(), [])
        self.assertEqual(self.tracker.posted, [])
        self.assertEqual(self.runner.started, [])
        self.assertFalse((self.cfg.worktree_root / "splanc").exists())

    def test_plan_reports_pending_relays_and_unclaims(self):
        self.tracker.add_issue(make_issue(1))
        self.rec.tick()  # real claim
        Mailbox(Path(self.registry.get("issue-1").worktree) / ".agent" / "mailbox").put_outbox(
            "status", {"text": "hi"}
        )
        lines = self.rec.plan()
        self.assertTrue(any("would relay status" in l for l in lines))
        self.tracker.issues["issue-1"].labels = []
        lines = self.rec.plan()
        self.assertTrue(any("would un-claim" in l for l in lines))
        self.assertIsNotNone(self.registry.get("issue-1"))  # still not mutated

    def test_plan_empty_fleet_says_nothing_to_do(self):
        self.assertEqual(self.rec.plan(), ["nothing to do"])


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, body: str) -> Path:
        p = self.root / "config.toml"
        p.write_text(body)
        return p

    def test_bad_config_fails_with_the_precise_error(self):
        p = self.write_config("[[projects]]\nname='x'\n")
        out = io.StringIO()
        code = run_doctor(p, stream=out)
        self.assertEqual(code, 1)
        self.assertIn("linear_project", out.getvalue())

    def test_healthy_ish_run_reports_would_claim(self):
        import os

        os.environ["LINEAR_API_KEY"] = "k"
        os.environ["GITHUB_TOKEN"] = "t"
        try:
            p = self.write_config(
                f"""
[daemon]
state_dir = "{self.root}/state"
worktree_root = "{self.root}/wt"
[[projects]]
name = "splanc"
linear_project = "Splanc"
repo = "{self.root}/repo"
claim = {{ strategy = "label", value = "agent" }}
state_in_progress = "In Progress"
state_done = "Done"
"""
            )
            tracker = FakeDoctorTracker()
            tracker.add_issue(make_issue(1))
            tracker.add_issue(make_issue(2, labels=[]))  # open but not eligible
            git = FakeGit(self.root)
            git.repo_urls = {}
            # FakeGit lacks doctor helpers; add them here.
            git.is_repo = lambda repo: True
            git.remote_url = lambda repo: "git@github.com:fughilli/splanc.git"
            out = io.StringIO()
            code = run_doctor(
                p,
                tracker=tracker,
                forges={"splanc": FakeForge()},
                git=git,
                runner=FakeRunner(),
                stream=out,
            )
            text = out.getvalue()
            self.assertIn("authenticated as fleet-bot", text)
            self.assertIn("2 open issue(s), 1 eligible", text)
            self.assertIn("workflow state 'In Progress'", text)
            self.assertIn("Would claim now:", text)
            self.assertIn("FUG-1", text)
        finally:
            del os.environ["LINEAR_API_KEY"]
            del os.environ["GITHUB_TOKEN"]

    def test_missing_credentials_is_actionable(self):
        import os

        saved = {k: os.environ.pop(k, None) for k in ("LINEAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")}
        try:
            p = self.write_config(
                f"""
[credentials]
linear_api_key_file = "{self.root}/nope.key"
github_token_file = "{self.root}/nope2.key"
[[projects]]
name = "x"
linear_project = "X"
repo = "{self.root}/repo"
claim = {{ strategy = "label", value = "agent" }}
"""
            )
            out = io.StringIO()
            code = run_doctor(p, git=FakeGit(self.root), stream=out)
            self.assertEqual(code, 1)
            self.assertIn("linear.app/settings/api", out.getvalue())
            self.assertIn("fine-grained PAT", out.getvalue())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
