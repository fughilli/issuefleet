"""Doctor and dry-run plan, offline via injected fakes."""

import io
import os
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
        """Mock the arch probe so doctor never touches the host's docker."""
        from unittest import mock

        arch = mock.patch("issuefleet.config.docker_host_arch", return_value="amd64")
        arch.start()
        self.addCleanup(arch.stop)
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


class LauncherFlagCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "projects": [
                    {
                        "name": "x",
                        "linear_project": "X",
                        "repo": str(self.root),
                        "claim": {"strategy": "label", "value": "agent"},
                    }
                ]
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def stub_launcher(self, help_text: str) -> str:
        stub = self.root / "cc-stub"
        stub.write_text(f"#!/bin/sh\necho '{help_text}'\n")
        stub.chmod(0o755)
        return str(stub)

    def test_supported_flag_ok(self):
        from issuefleet.doctor import _check_launcher_flags

        self.cfg.claude_container = self.stub_launcher(
            "usage: claude-container [--skills-ignore-new] ..."
        )
        [check] = _check_launcher_flags(self.cfg)
        self.assertEqual(check.status, "ok")

    def test_unknown_flag_fails_with_upgrade_hint(self):
        from issuefleet.doctor import _check_launcher_flags

        self.cfg.claude_container = self.stub_launcher("usage: claude-container [-w DIR] ...")
        [check] = _check_launcher_flags(self.cfg)
        self.assertEqual(check.status, "fail")
        self.assertIn("launcher_args", check.detail)

    def test_no_flags_configured_is_silent(self):
        from issuefleet.doctor import _check_launcher_flags

        self.cfg.launcher_args = []
        self.assertEqual(_check_launcher_flags(self.cfg), [])

    def test_missing_launcher_defers_to_tools_check(self):
        from issuefleet.doctor import _check_launcher_flags

        self.cfg.claude_container = "definitely-not-on-path-xyz"
        self.assertEqual(_check_launcher_flags(self.cfg), [])


class CliParserTest(unittest.TestCase):
    """Global flags must parse in EITHER position — a trailing -v after the
    subcommand bootlooped the container (usage error -> restart loop)."""

    def parse(self, argv):
        from issuefleet.cli import build_parser

        return build_parser().parse_args(argv)

    def test_verbose_both_positions(self):
        self.assertTrue(getattr(self.parse(["run", "-v"]), "verbose", False))
        self.assertTrue(getattr(self.parse(["-v", "run"]), "verbose", False))
        self.assertFalse(getattr(self.parse(["run"]), "verbose", False))

    def test_config_both_positions_no_default_clobber(self):
        # SUPPRESS semantics: a pre-subcommand value must survive the
        # subparser (argparse parents pitfall).
        a = self.parse(["--config", "/x.toml", "status"])
        self.assertEqual(getattr(a, "config"), "/x.toml")
        a = self.parse(["status", "--config", "/y.toml"])
        self.assertEqual(getattr(a, "config"), "/y.toml")
        self.assertFalse(hasattr(self.parse(["status"]), "config"))

    def test_subcommand_own_flags_still_work(self):
        a = self.parse(["once", "--dry-run", "-v"])
        self.assertTrue(a.dry_run)
        self.assertTrue(getattr(a, "verbose", False))
        a = self.parse(["logs", "FUG-1", "-f"])
        self.assertTrue(a.follow)

    def test_fleet_subcommand_parses(self):
        self.assertEqual(self.parse(["fleet"]).cmd, "fleet")


class FleetManagerCheckTest(unittest.TestCase):
    def _cfg(self, **fm):
        base = {"enabled": True, "base_url": "http://s:8100",
                "board_project": "Fleet", "board_team": "FUG"}
        base.update(fm)
        return config.parse({"projects": [{"name": "x", "linear_project": "X",
                                           "repo": "/tmp/x", "claim": {"strategy": "agent"}}],
                             "fleet_manager": base})

    def test_disabled_is_ok(self):
        from issuefleet.doctor import _check_fleet_manager

        cfg = config.parse({"projects": [{"name": "x", "linear_project": "X",
                                          "repo": "/tmp/x", "claim": {"strategy": "agent"}}]})
        checks = _check_fleet_manager(cfg)
        self.assertEqual([c.status for c in checks], ["ok"])

    def test_enabled_flags_missing_sigbot_key(self):
        from unittest import mock

        from issuefleet.doctor import _check_fleet_manager

        with mock.patch.dict(os.environ, {}, clear=True):
            checks = _check_fleet_manager(self._cfg())
        key_check = [c for c in checks if c.label == "sigbot key"][0]
        self.assertEqual(key_check.status, "fail")

    def test_claude_advisor_without_key_warns(self):
        from unittest import mock

        from issuefleet.doctor import _check_fleet_manager

        with mock.patch.dict(os.environ, {"ISSUEFLEET_SIGBOT_API_KEY": "sb_x"}, clear=True):
            checks = _check_fleet_manager(self._cfg(advisor="claude"))
        adv = [c for c in checks if c.label == "advisor key"][0]
        self.assertEqual(adv.status, "warn")


class DockerPlatformCheckTest(unittest.TestCase):
    def _cfg(self, platform_value):
        return config.parse({"projects": [{"name": "x", "linear_project": "X",
                                           "repo": "/tmp/x", "claim": {"strategy": "agent"}}],
                             "agent": {"docker_platform": platform_value}})

    def _check(self, platform_value, arch):
        from unittest import mock

        from issuefleet.doctor import _check_docker_platform

        with mock.patch("issuefleet.config.docker_host_arch", return_value=arch):
            [c] = _check_docker_platform(self._cfg(platform_value))
        return c

    def test_auto_pins_on_arm64_docker_host(self):
        c = self._check("auto", "arm64")
        self.assertEqual(c.status, "ok")
        self.assertIn("linux/amd64", c.detail)

    def test_amd64_host_needs_no_pin(self):
        self.assertEqual(self._check("auto", "amd64").status, "ok")
        self.assertEqual(self._check("", "x86_64").status, "ok")

    def test_explicit_disable_on_arm64_warns_not_fails(self):
        """An explicit "" is the documented opt-out: exit-0, but say so."""
        c = self._check("", "arm64")
        self.assertEqual(c.status, "warn")
        self.assertIn("amd64-only", c.detail)


class WorkerRuntimeCheckTest(unittest.TestCase):
    def test_root_euid_fails_with_guidance(self):
        from unittest import mock

        from issuefleet.doctor import _check_worker_runtime

        cfg = config.parse({"projects": [{"name": "x", "linear_project": "X",
                                          "repo": "/tmp/x",
                                          "claim": {"strategy": "agent"}}]})
        with mock.patch("os.geteuid", return_value=0):
            checks = _check_worker_runtime(cfg)
        root_check = [c for c in checks if "root" in c.label][0]
        self.assertEqual(root_check.status, "fail")
        self.assertIn("bypassPermissions", root_check.detail)
        with mock.patch("os.geteuid", return_value=501):
            checks = _check_worker_runtime(cfg)
        self.assertEqual([c.status for c in checks if "uid 501" in c.label], ["ok"])


class WebhookBindTest(unittest.TestCase):
    def test_env_overrides_config_bind(self):
        import os

        from issuefleet.cli import _webhook_bind
        from issuefleet.config import WebhookConfig

        w = WebhookConfig()
        self.assertEqual(_webhook_bind(w), "127.0.0.1")
        os.environ["ISSUEFLEET_WEBHOOK_BIND"] = "0.0.0.0"
        try:
            self.assertEqual(_webhook_bind(w), "0.0.0.0")
        finally:
            del os.environ["ISSUEFLEET_WEBHOOK_BIND"]


class DoctorTest(unittest.TestCase):
    def setUp(self):
        """Mock the arch probe so doctor never touches the host's docker."""
        from unittest import mock

        arch = mock.patch("issuefleet.config.docker_host_arch", return_value="amd64")
        arch.start()
        self.addCleanup(arch.stop)
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

    def test_missing_repo_with_git_url_warns_and_checks_api(self):
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
name = "x"
linear_project = "X"
repo = "{self.root}/not-cloned-yet"
git_url = "git@github.com:fughilli/somerepo.git"
claim = {{ strategy = "agent" }}
"""
            )
            tracker = FakeDoctorTracker()
            git = FakeGit(self.root)
            git.is_repo = lambda repo: False
            out = io.StringIO()
            run_doctor(p, tracker=tracker, forges={"x": FakeForge()}, git=git,
                       runner=FakeRunner(), stream=out)
            text = out.getvalue()
            self.assertIn("will be cloned from git@github.com:fughilli/somerepo.git", text)
            self.assertIn("-> fughilli/somerepo", text)  # slug derived from git_url
            self.assertNotIn("not a git repository", text)
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
