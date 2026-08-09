"""Worker provisioning: inheriting launcher-local state from the parent
checkout (claude-container skill approval etc.) into fresh worktrees."""

import tempfile
import unittest
from pathlib import Path

from issuefleet.worker import ensure_container_overlay, inherit_repo_files

DEFAULTS = [".claude", ".claude-container-overlay"]


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


class EnsureContainerOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_default_python_overlay(self):
        """The file must carry real Dockerfile line continuations — a non-raw
        Python literal silently eats backslash-newlines."""
        self.assertTrue(ensure_container_overlay(self.wt))
        text = (self.wt / ".claude-container-overlay").read_text()
        self.assertIn("python3", text)
        self.assertIn("apk", text)
        self.assertIn("\\\n", text)
        self.assertFalse(ensure_container_overlay(self.wt))

    def test_leaves_directory_overlay_alone(self):
        overlay = self.wt / ".claude-container-overlay"
        overlay.mkdir()
        (overlay / "overlay.json").write_text("{}")
        self.assertFalse(ensure_container_overlay(self.wt))
        self.assertTrue(overlay.is_dir())


if __name__ == "__main__":
    unittest.main()
