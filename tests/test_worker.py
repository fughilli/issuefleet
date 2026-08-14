"""Worker provisioning: inheriting launcher-local state from the parent
checkout (claude-container skill approval etc.) into fresh worktrees."""

import tempfile
import unittest
from pathlib import Path

from issuefleet import config
from issuefleet.agent_runtime.turns import PHASE_RUNNING, TurnState
from issuefleet.model import Issue
from issuefleet.worker import inherit_repo_files, provision

DEFAULTS = [".claude", ".claude-container-overlay"]


def _issue():
    return Issue(id="i1", key="FUG-1", title="Fix it", description="do the thing",
                 url="https://x/FUG-1", priority=0, state_name="Todo", state_type="unstarted")


def _cfg(root):
    return config.parse({
        "daemon": {"state_dir": str(root / "s"), "worktree_root": str(root / "w")},
        "projects": [{"name": "p", "linear_project": "P", "repo": str(root / "r"),
                      "claim": {"strategy": "agent"}}],
    })


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt"
        self.wt.mkdir()
        self.cfg = _cfg(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_claim_seeds_a_new_session(self):
        uuid = provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg)
        st = TurnState.load(self.wt / ".agent")
        self.assertEqual(st.session_uuid, uuid)
        self.assertEqual(st.turns_taken, 0)  # first turn will use --session-id

    def test_adopt_seeds_prior_session_for_resume(self):
        # The release->adopt path rebuilds a torn-down worktree carrying the
        # released worker's own session id and turn count, so the loop resumes
        # (--resume) rather than colliding on its own session id.
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg,
                  session_uuid="keep-me", turns_taken=7, phase=PHASE_RUNNING)
        st = TurnState.load(self.wt / ".agent")
        self.assertEqual(st.session_uuid, "keep-me")
        self.assertEqual(st.turns_taken, 7)
        self.assertEqual(st.phase, PHASE_RUNNING)

    def test_siblings_add_the_cross_project_section_to_the_brief(self):
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg,
                  siblings=[{"name": "embedded", "repo": "o/embedded"}])
        brief = (self.wt / ".agent" / "brief.md").read_text()
        self.assertIn("Contributing to other fleet projects", brief)
        self.assertIn("**embedded** (o/embedded)", brief)

    def test_no_siblings_omits_the_cross_project_section(self):
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg)
        brief = (self.wt / ".agent" / "brief.md").read_text()
        self.assertNotIn("Contributing to other fleet projects", brief)

    def test_precreates_empty_siblings_dir(self):
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg)
        siblings = self.wt / "siblings"
        self.assertTrue(siblings.is_dir())
        self.assertEqual(list(siblings.iterdir()), [])  # empty until a checkout

    def test_existing_state_is_preserved(self):
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg, session_uuid="first")
        # A re-provision (restart adoption) must not reset the session or seed.
        provision(self.wt, _issue(), "agent/fug-1-x", "main", self.cfg,
                  session_uuid="second", turns_taken=99)
        st = TurnState.load(self.wt / ".agent")
        self.assertEqual(st.session_uuid, "first")
        self.assertEqual(st.turns_taken, 0)


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


if __name__ == "__main__":
    unittest.main()
