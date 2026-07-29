import tempfile
import unittest
from pathlib import Path

from issuefleet.model import WorkerRecord
from issuefleet.registry import Registry


def make_record(issue_id="i1", key="FUG-1"):
    return WorkerRecord(
        issue_id=issue_id,
        issue_key=key,
        issue_title="Fix the thing",
        issue_url="https://linear.app/x/issue/" + key,
        project="splanc",
        repo="/repos/splanc",
        branch=f"agent/{key.lower()}-fix-the-thing",
        worktree=f"/worktrees/splanc/{key}",
        base_ref="main",
        session_uuid="00000000-0000-0000-0000-000000000001",
        tmux_session=f"issuefleet-{key}",
    )


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_across_restart(self):
        reg = Registry(self.state_dir)
        reg.add(make_record())
        reg.add(make_record(issue_id="i2", key="FUG-2"))

        # A fresh instance (daemon restart) re-adopts the fleet.
        reg2 = Registry(self.state_dir)
        self.assertEqual(len(reg2.all()), 2)
        rec = reg2.get("i1")
        self.assertEqual(rec.branch, "agent/fug-1-fix-the-thing")
        self.assertEqual(rec.phase, "active")

    def test_remove_persists(self):
        reg = Registry(self.state_dir)
        reg.add(make_record())
        reg.remove("i1")
        self.assertEqual(Registry(self.state_dir).all(), [])

    def test_empty_state_dir_is_fine(self):
        self.assertEqual(Registry(self.state_dir / "does-not-exist-yet").all(), [])

    def test_corrupt_registry_fails_loudly(self):
        (self.state_dir / "registry.json").write_text("{oops")
        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            Registry(self.state_dir)

    def test_unknown_fields_tolerated(self):
        # Forward compat: an older daemon must not crash on a newer registry.
        reg = Registry(self.state_dir)
        reg.add(make_record())
        import json

        data = json.loads(reg.path.read_text())
        data["workers"][0]["some_future_field"] = 42
        reg.path.write_text(json.dumps(data))
        self.assertEqual(Registry(self.state_dir).get("i1").issue_key, "FUG-1")

    def test_archive_dir_outside_worktree(self):
        reg = Registry(self.state_dir)
        rec = make_record()
        d = reg.archive_dir_for(rec)
        self.assertTrue(str(d).startswith(str(self.state_dir)))
        self.assertIn("FUG-1", str(d))


if __name__ == "__main__":
    unittest.main()
