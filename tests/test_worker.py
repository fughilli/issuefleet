"""Worker provisioning: inheriting launcher-local state from the parent
checkout (claude-container skill approval etc.) into fresh worktrees, and
staging the opt-in tailnet material (FUG-40)."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from issuefleet import config as config_mod
from issuefleet.worker import inherit_repo_files, stage_tailscale

DEFAULTS = [".claude", ".claude-container-overlay"]


def _cfg(enabled=True, per_project=None):
    data = {
        "projects": [{
            "name": "splanc", "linear_project": "Splanc", "repo": "/tmp/splanc",
            "claim": {"strategy": "agent"},
        }],
        "agent": {"tailscale": {
            "enabled": enabled, "tags": ["tag:issuefleet-worker"],
            "authkey_env": "TEST_TS_KEY",
        }},
    }
    if per_project is not None:
        data["projects"][0]["tailscale"] = per_project
    return config_mod.parse(data)


class _Issue:
    key = "FUG-40"


class InheritRepoFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.wt = root / "wt"
        self.repo.mkdir()
        self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_nested_state_and_reports_dir_pattern(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "skills-approval.json").write_text('{"approved": true}')
        (self.repo / ".claude" / "sub").mkdir()
        (self.repo / ".claude" / "sub" / "x.json").write_text("{}")
        inherited = inherit_repo_files(self.repo, self.wt, DEFAULTS)
        self.assertEqual(inherited, [".claude/"])  # overlay absent -> skipped
        self.assertEqual(
            (self.wt / ".claude" / "skills-approval.json").read_text(), '{"approved": true}'
        )
        self.assertTrue((self.wt / ".claude" / "sub" / "x.json").is_file())

    def test_never_overwrites_what_the_checkout_provides(self):
        # A tracked overlay file already exists in the worktree; the parent's
        # (possibly stale) copy must not clobber it.
        (self.repo / ".claude-container-overlay").mkdir()
        (self.repo / ".claude-container-overlay" / "overlay.json").write_text('{"from": "repo"}')
        (self.wt / ".claude-container-overlay").mkdir()
        (self.wt / ".claude-container-overlay" / "overlay.json").write_text('{"from": "checkout"}')
        inherited = inherit_repo_files(self.repo, self.wt, DEFAULTS)
        self.assertEqual(inherited, [".claude-container-overlay/"])
        self.assertEqual(
            (self.wt / ".claude-container-overlay" / "overlay.json").read_text(),
            '{"from": "checkout"}',
        )

    def test_missing_sources_are_skipped_quietly(self):
        self.assertEqual(inherit_repo_files(self.repo, self.wt, DEFAULTS), [])

    def test_single_file_path(self):
        (self.repo / "approval.json").write_text("ok")
        inherited = inherit_repo_files(self.repo, self.wt, ["approval.json"])
        self.assertEqual(inherited, ["approval.json"])
        self.assertEqual((self.wt / "approval.json").read_text(), "ok")

    def test_idempotent_across_reprovisioning(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "a.json").write_text("1")
        inherit_repo_files(self.repo, self.wt, DEFAULTS)
        (self.wt / ".claude" / "a.json").write_text("agent-modified")
        inherit_repo_files(self.repo, self.wt, DEFAULTS)  # re-adopt after restart
        self.assertEqual((self.wt / ".claude" / "a.json").read_text(), "agent-modified")


class StageTailscaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        (self.wt / ".agent").mkdir()
        (self.wt / ".agent" / "brief.md").write_text("# brief\n")
        self.ts = self.wt / ".agent" / "tailscale"

    def tearDown(self):
        self.tmp.cleanup()

    def test_stages_key_and_params_when_enabled(self):
        with mock.patch.dict(os.environ, {"TEST_TS_KEY": "tskey-abc"}):
            cfg = _cfg(enabled=True)
            staged = stage_tailscale(self.wt, _Issue(), cfg.projects[0], cfg)
        self.assertTrue(staged)
        self.assertEqual((self.ts / "authkey").read_text(), "tskey-abc")
        params = json.loads((self.ts / "params.json").read_text())
        self.assertEqual(params["hostname"], "issuefleet-fug-40")
        self.assertEqual(params["tags"], ["tag:issuefleet-worker"])
        # Key file is not group/world readable.
        mode = (self.ts / "authkey").stat().st_mode
        self.assertFalse(mode & (stat.S_IRGRP | stat.S_IROTH))
        # The brief gains tailnet usage guidance.
        self.assertIn("Tailnet access", (self.wt / ".agent" / "brief.md").read_text())

    def test_noop_and_clears_when_disabled(self):
        # Pre-existing (stale) material from a prior enabled claim.
        self.ts.mkdir()
        (self.ts / "authkey").write_text("stale")
        with mock.patch.dict(os.environ, {"TEST_TS_KEY": "tskey-abc"}):
            cfg = _cfg(enabled=False)
            staged = stage_tailscale(self.wt, _Issue(), cfg.projects[0], cfg)
        self.assertFalse(staged)
        self.assertFalse(self.ts.exists())  # cleared, no live key left behind

    def test_noop_when_enabled_but_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = _cfg(enabled=True)
            staged = stage_tailscale(self.wt, _Issue(), cfg.projects[0], cfg)
        self.assertFalse(staged)
        self.assertFalse(self.ts.exists())

    def test_per_project_opt_out_overrides_fleet_on(self):
        with mock.patch.dict(os.environ, {"TEST_TS_KEY": "tskey-abc"}):
            cfg = _cfg(enabled=True, per_project=False)
            staged = stage_tailscale(self.wt, _Issue(), cfg.projects[0], cfg)
        self.assertFalse(staged)


if __name__ == "__main__":
    unittest.main()
