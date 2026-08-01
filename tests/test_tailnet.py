"""In-container tailnet bring-up (FUG-40). No real tailscaled: the happy path
uses stub `tailscale`/`tailscaled` scripts on PATH that record their argv, so we
assert the userspace flags and the auth-key handoff without touching a network."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from issuefleet.agent_runtime import tailnet


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class EnsureUpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self.tmp.name) / ".agent"
        self.ts = self.agent / "tailscale"
        self.ts.mkdir(parents=True)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _path_with_stubs(self):
        # Prepend the stub dir so our fake tailscale/tailscaled win, but keep the
        # real PATH so the stubs' own `touch`/`sh` builtins still resolve.
        return str(self.bin) + os.pathsep + os.environ.get("PATH", "")

    def _stage(self, **params):
        base = {"hostname": "issuefleet-fug-40", "tags": ["tag:issuefleet-worker"],
                "up_args": [], "proxy_port": 1055}
        base.update(params)
        (self.ts / "params.json").write_text(json.dumps(base))
        (self.ts / "authkey").write_text("tskey-secret")

    def test_no_material_is_silent_noop(self):
        # Fresh agent dir with no tailscale/ material at all.
        empty = Path(self.tmp.name) / "empty" / ".agent"
        empty.mkdir(parents=True)
        self.assertIsNone(tailnet.ensure_up(empty))

    def test_binary_missing_reports_reason(self):
        self._stage()
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=False):
            with mock.patch("shutil.which", return_value=None):
                status = tailnet.ensure_up(self.agent)
        self.assertFalse(status.ok)
        self.assertIn("not found", status.reason)

    def test_happy_path_userspace_flags_and_env(self):
        self._stage()
        arglog = self.ts / "argv.log"
        # tailscaled: record argv, create the control socket, stay "running".
        _write_stub(self.bin / "tailscaled",
                    f'echo "tailscaled $@" >> {arglog}\n'
                    f'touch {self.ts}/tailscaled.sock\n')
        # tailscale: `status --json` is Running only once tailscaled has created
        # its socket (so the first check reports down and we exercise the start
        # path); `up` records argv, including the auth key.
        _write_stub(self.bin / "tailscale",
                    f'echo "tailscale $@" >> {arglog}\n'
                    'case "$*" in\n'
                    f'  *status*) if [ -e {self.ts}/tailscaled.sock ]; then '
                    'echo \'{"BackendState":"Running",'
                    '"Self":{"TailscaleIPs":["100.64.0.7","fd7a::7"]}}\'; '
                    'else exit 1; fi ;;\n'
                    'esac\n')
        with mock.patch.dict(os.environ, {"PATH": self._path_with_stubs()}, clear=False):
            status = tailnet.ensure_up(self.agent)

        self.assertTrue(status.ok, status.reason)
        self.assertEqual(status.proxy, "socks5://127.0.0.1:1055")
        self.assertEqual(status.ip, "100.64.0.7")
        log = arglog.read_text()
        self.assertIn("--tun=userspace-networking", log)
        self.assertIn("--socks5-server=127.0.0.1:1055", log)
        self.assertIn("--authkey=tskey-secret", log)
        self.assertIn("--hostname=issuefleet-fug-40", log)
        self.assertIn("--advertise-tags=tag:issuefleet-worker", log)
        # The opt-in proxy env snippet is written for the agent.
        env = (self.ts / "env").read_text()
        self.assertIn("ALL_PROXY=socks5://127.0.0.1:1055", env)
        # The auth key is never written into the human-readable bring-up log.
        self.assertNotIn("tskey-secret", (self.ts / "bringup.log").read_text())

    def test_up_failure_is_not_fatal(self):
        _write_stub(self.bin / "tailscaled",
                    f'touch {self.ts}/tailscaled.sock\n')
        # status -> not Running; up -> exit 1.
        _write_stub(self.bin / "tailscale",
                    'case "$*" in *up*) exit 1 ;; esac\n')
        self._stage()
        with mock.patch.dict(os.environ, {"PATH": self._path_with_stubs()}, clear=False):
            status = tailnet.ensure_up(self.agent)
        self.assertFalse(status.ok)
        self.assertIn("auth key or config", status.reason)


if __name__ == "__main__":
    unittest.main()
