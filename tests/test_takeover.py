"""Offline coverage for `issuefleet takeover`: the release → interactive
session → adopt-back flow, driven against fakes for the daemon control channel,
git, and the interactive launcher — no container, no network, no real daemon."""

import tempfile
import unittest
from pathlib import Path

from fakes import FakeGit

from issuefleet import config, takeover
from issuefleet.model import PHASE_ACTIVE, PHASE_CRASHED, PHASE_RELEASED, WorkerRecord
from issuefleet.registry import Registry


class FakeControl:
    """Stands in for the running daemon: release/adopt flip the on-disk
    registry phase the way the real tick thread would, so the tool's
    wait-for-phase polling sees the transition."""

    def __init__(self, state_dir, *, reachable=True, released_turns=3):
        self.state_dir = Path(state_dir)
        self.base_url = "http://fake-daemon"
        self._reachable = reachable
        self.released_turns = released_turns
        self.calls: list[tuple[str, str]] = []

    def reachable(self) -> bool:
        return self._reachable

    def release(self, key: str) -> None:
        self.calls.append(("release", key))
        self._set(key, PHASE_RELEASED, self.released_turns)

    def adopt(self, key: str) -> None:
        self.calls.append(("adopt", key))
        self._set(key, PHASE_ACTIVE, 0)

    def _set(self, key: str, phase: str, turns: int) -> None:
        reg = Registry(self.state_dir)
        rec = next(w for w in reg.all() if w.issue_key.lower() == key.lower())
        rec.phase = phase
        if phase == PHASE_RELEASED:
            rec.released_turns = turns
        reg.save()


class Launcher:
    def __init__(self, rc=0, boom=False):
        self.rc, self.boom = rc, boom
        self.cmd: list[str] | None = None
        self.calls = 0

    def __call__(self, cmd):
        self.calls += 1
        self.cmd = cmd
        if self.boom:
            raise KeyboardInterrupt
        return self.rc


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "daemon": {
                    "state_dir": str(self.root / "state"),
                    "worktree_root": str(self.root / "worktrees"),
                },
                "projects": [
                    {
                        "name": "splanc",
                        "linear_project": "Splanc",
                        "repo": str(self.root / "repo"),
                        "claim": {"strategy": "label", "value": "agent"},
                    }
                ],
            }
        )
        self.registry = Registry(self.cfg.state_dir)
        self.git = FakeGit(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def add_worker(self, *, phase=PHASE_ACTIVE, key="FUG-5", turns=0):
        rec = WorkerRecord(
            issue_id="issue-5",
            issue_key=key,
            issue_title="Do the thing",
            issue_url="https://linear.app/x/issue/" + key,
            project="splanc",
            repo=str(self.root / "repo"),
            branch=f"agent/{key.lower()}-do-the-thing",
            worktree=str(self.root / "worktrees" / key),
            base_ref="main",
            session_uuid="sess-uuid-1",
            tmux_session=f"issuefleet-{key}",
            phase=phase,
            released_turns=turns,
        )
        self.registry.add(rec)
        return rec

    def run_takeover(self, key="FUG-5", *, control=None, launch=None):
        control = control or FakeControl(self.cfg.state_dir)
        launch = launch or Launcher()
        rc = takeover.run(
            self.cfg, key, git=self.git, control=control, launch=launch,
            sleep=lambda _s: None, timeout_s=3, interval_s=1,
        )
        return rc, control, launch

    # -- happy path --------------------------------------------------------

    def test_release_interactive_adopt_roundtrip(self):
        self.add_worker(phase=PHASE_ACTIVE)
        rc, control, launch = self.run_takeover()

        self.assertEqual(rc, 0)
        # Both transitions were driven through the daemon, in order.
        self.assertEqual(control.calls, [("release", "FUG-5"), ("adopt", "FUG-5")])
        # A local worktree was built on the freed branch and torn down after.
        wt = str(self.root / "worktrees" / "FUG-5")
        self.assertEqual(self.git.worktrees[0]["path"], wt)
        self.assertEqual(self.git.worktrees[0]["branch"], "agent/fug-5-do-the-thing")
        self.assertIn(wt, self.git.removed)
        # The worker is back with the fleet.
        self.registry.reload()
        self.assertEqual(self.registry.get("issue-5").phase, PHASE_ACTIVE)

    def test_interactive_command_resumes_when_turns_taken(self):
        self.add_worker(phase=PHASE_ACTIVE)
        # released_turns=3 (default) => the session is resumed.
        _, _, launch = self.run_takeover()
        self.assertIn("claude", launch.cmd)
        self.assertIn("--resume", launch.cmd)
        self.assertIn("sess-uuid-1", launch.cmd)
        # The worker's launcher + worktree flags are carried through.
        self.assertEqual(launch.cmd[0], self.cfg.claude_container)
        self.assertIn("-w", launch.cmd)

    def test_no_resume_when_worker_never_took_a_turn(self):
        self.add_worker(phase=PHASE_ACTIVE)
        control = FakeControl(self.cfg.state_dir, released_turns=0)
        _, _, launch = self.run_takeover(control=control)
        self.assertIn("claude", launch.cmd)
        self.assertNotIn("--resume", launch.cmd)

    # -- robustness --------------------------------------------------------

    def test_adopts_back_even_when_the_session_is_interrupted(self):
        self.add_worker(phase=PHASE_ACTIVE)
        launch = Launcher(boom=True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_takeover(launch=launch)
        # The finally block still handed the branch back.
        self.registry.reload()
        self.assertEqual(self.registry.get("issue-5").phase, PHASE_ACTIVE)

    def test_already_released_skips_the_release_step(self):
        self.add_worker(phase=PHASE_RELEASED, turns=2)
        rc, control, launch = self.run_takeover()
        self.assertEqual(rc, 0)
        # No second release — straight to the interactive session, then adopt.
        self.assertEqual(control.calls, [("adopt", "FUG-5")])
        self.assertEqual(launch.calls, 1)

    def test_crashed_worker_can_be_taken_over(self):
        self.add_worker(phase=PHASE_CRASHED)
        rc, control, _ = self.run_takeover()
        self.assertEqual(rc, 0)
        self.assertEqual(control.calls[0], ("release", "FUG-5"))

    def test_unknown_worker_is_a_clean_error(self):
        with self.assertRaises(takeover.TakeoverError) as cm:
            self.run_takeover(key="FUG-999")
        self.assertIn("no worker", str(cm.exception))

    def test_unreachable_daemon_aborts_before_touching_git(self):
        self.add_worker(phase=PHASE_ACTIVE)
        control = FakeControl(self.cfg.state_dir, reachable=False)
        with self.assertRaises(takeover.TakeoverError) as cm:
            self.run_takeover(control=control)
        self.assertIn("isn't reachable", str(cm.exception))
        self.assertEqual(self.git.worktrees, [])
        self.assertEqual(control.calls, [])

    def test_release_that_never_lands_times_out(self):
        self.add_worker(phase=PHASE_ACTIVE)

        class StuckControl(FakeControl):
            def release(self, key):  # enqueued, but the daemon never acts
                self.calls.append(("release", key))

        control = StuckControl(self.cfg.state_dir)
        with self.assertRaises(takeover.TakeoverError) as cm:
            self.run_takeover(control=control)
        self.assertIn("released", str(cm.exception))
        # Never advanced to the interactive session or adopt.
        self.assertEqual(self.git.worktrees, [])
        self.assertEqual(control.calls, [("release", "FUG-5")])


class DashboardUrlTest(unittest.TestCase):
    def _cfg(self, **dash):
        return config.parse(
            {
                "daemon": {"state_dir": "/tmp/x", "worktree_root": "/tmp/y"},
                "dashboard": dash or {},
                "projects": [
                    {
                        "name": "splanc",
                        "linear_project": "Splanc",
                        "repo": "/tmp/repo",
                        "claim": {"strategy": "label", "value": "agent"},
                    }
                ],
            }
        )

    def test_wildcard_bind_dials_back_to_loopback(self):
        cfg = self._cfg(bind="0.0.0.0", port=8788)
        self.assertEqual(takeover.dashboard_url(cfg), "http://127.0.0.1:8788")

    def test_explicit_bind_is_kept(self):
        cfg = self._cfg(bind="100.64.0.3", port=9000)
        self.assertEqual(takeover.dashboard_url(cfg), "http://100.64.0.3:9000")

    def test_disabled_dashboard_is_a_clear_error(self):
        cfg = self._cfg(enabled=False)
        with self.assertRaises(takeover.TakeoverError):
            takeover.dashboard_url(cfg)

    def test_env_override_wins(self):
        import os

        cfg = self._cfg(enabled=False)  # override works even when it's off
        os.environ["ISSUEFLEET_DASHBOARD_URL"] = "http://box.tailnet:1234/"
        try:
            self.assertEqual(takeover.dashboard_url(cfg), "http://box.tailnet:1234")
        finally:
            del os.environ["ISSUEFLEET_DASHBOARD_URL"]


class DaemonControlTest(unittest.TestCase):
    """The HTTP shape of the control channel, exercised with a fake opener."""

    def _control(self):
        calls = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req):
            calls.append((req.get_method(), req.full_url))
            return Resp()

        return takeover.DaemonControl("http://d:8788/", opener=opener), calls

    def test_release_and_adopt_post_to_the_worker_endpoints(self):
        control, calls = self._control()
        control.release("FUG-5")
        control.adopt("FUG-5")
        self.assertEqual(
            calls,
            [
                ("POST", "http://d:8788/worker/FUG-5/release"),
                ("POST", "http://d:8788/worker/FUG-5/adopt"),
            ],
        )

    def test_reachable_hits_healthz(self):
        control, calls = self._control()
        self.assertTrue(control.reachable())
        self.assertEqual(calls, [("GET", "http://d:8788/healthz")])

    def test_404_becomes_a_takeover_error(self):
        import urllib.error

        def opener(req):
            raise urllib.error.HTTPError(req.full_url, 404, "no", {}, None)

        control = takeover.DaemonControl("http://d:8788", opener=opener)
        with self.assertRaises(takeover.TakeoverError) as cm:
            control.release("FUG-9")
        self.assertIn("no such worker", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
