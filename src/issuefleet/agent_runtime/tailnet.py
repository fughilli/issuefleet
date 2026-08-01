"""Bring the worker onto the tailnet, from inside the container (FUG-40).

Agents doing local development against shared resources (a HITL rig, a device
on a lab LAN) need to reach those hosts over the operator's tailnet. The daemon
opts a worker in by staging ``.agent/tailscale/{authkey,params.json}`` at claim
time (see ``worker.stage_tailscale``); this module, called once at the top of
``turnloop.run``, reads that material and brings Tailscale up.

Design choices (see the FUG-40 thread):

* **Userspace networking, no privileges.** ``tailscaled --tun=userspace-networking``
  needs neither a ``/dev/net/tun`` device nor ``NET_ADMIN``, so nothing has to
  change about how ``claude-container`` launches the worker. It exposes a local
  SOCKS5 + HTTP proxy instead of a system-wide route.
* **No control-plane hijack.** We do *not* export a global ``ALL_PROXY`` — that
  would push the worker's own claude/git traffic through the tailnet. The proxy
  endpoint is written to ``.agent/tailscale/env`` and surfaced in the brief so
  the agent opts in per command (``ALL_PROXY=socks5://127.0.0.1:1055 ssh …``).
* **Best-effort, never fatal.** Every failure path returns a ``Status`` and
  logs to ``.agent/tailscale/bringup.log``; a worker whose tailnet fails to come
  up still runs its turns normally.

Everything is stdlib-only, like the rest of ``agent_runtime`` (it is staged into
the worker as a copy of this package, version-matched to the orchestrator).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Status:
    ok: bool
    reason: str = ""
    proxy: str | None = None  # socks5://127.0.0.1:<port> when ok
    hostname: str | None = None
    ip: str | None = None  # tailnet IPv4 when known


def _log(ts_dir: Path, msg: str) -> None:
    try:
        with open(ts_dir / "bringup.log", "a") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def _proxy_url(port: int) -> str:
    return f"socks5://127.0.0.1:{port}"


def _status_running(tailscale: str, sock: Path) -> tuple[bool, str | None]:
    """(backend is Running, tailnet IPv4 or None), via `tailscale status --json`."""
    try:
        proc = subprocess.run(
            [tailscale, "--socket", str(sock), "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if proc.returncode != 0:
        return False, None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, None
    running = data.get("BackendState") == "Running"
    ips = (data.get("Self") or {}).get("TailscaleIPs") or []
    ipv4 = next((ip for ip in ips if ":" not in ip), None)
    return running, ipv4


def _write_env(ts_dir: Path, proxy: str) -> None:
    """A shell snippet the agent can `source` (or read) to opt a command into
    the tailnet. Only outbound, proxy-aware tools are affected."""
    (ts_dir / "env").write_text(
        "# Source this to route a command over the tailnet (FUG-40). Example:\n"
        "#   source .agent/tailscale/env && ssh user@rig-host\n"
        f"export ALL_PROXY={proxy}\n"
        f"export HTTP_PROXY={proxy}\n"
        f"export HTTPS_PROXY={proxy}\n"
        f"export http_proxy={proxy}\n"
        f"export https_proxy={proxy}\n"
    )


def ensure_up(agent_dir: Path) -> Status | None:
    """Bring the worker onto the tailnet if the daemon opted it in. Returns
    None when tailnet was not requested (no ``.agent/tailscale/`` material) —
    the overwhelmingly common case — so callers can stay quiet."""
    ts_dir = Path(agent_dir) / "tailscale"
    params_path = ts_dir / "params.json"
    authkey_path = ts_dir / "authkey"
    if not params_path.exists() or not authkey_path.exists():
        return None

    try:
        params = json.loads(params_path.read_text())
        authkey = authkey_path.read_text().strip()
    except (OSError, json.JSONDecodeError) as e:
        _log(ts_dir, f"unreadable tailscale material: {e}")
        return Status(ok=False, reason="unreadable tailscale material")
    if not authkey:
        return Status(ok=False, reason="empty auth key")

    port = int(params.get("proxy_port", 1055))
    hostname = params.get("hostname") or None
    proxy = _proxy_url(port)

    tailscaled = shutil.which("tailscaled")
    tailscale = shutil.which("tailscale")
    if not tailscaled or not tailscale:
        reason = (
            "tailscale binary not found in the worker image — install tailscale "
            "(tailscaled + tailscale) in the container image to enable tailnet"
        )
        _log(ts_dir, reason)
        return Status(ok=False, reason=reason)

    sock = ts_dir / "tailscaled.sock"
    state = ts_dir / "tailscaled.state"

    # Idempotent: if a prior bring-up already left the backend Running, just
    # re-publish the proxy env and return (turnloop may restart within one
    # container life; tailscaled, detached below, outlives it).
    already, ip = _status_running(tailscale, sock)
    if not already:
        _log(ts_dir, f"starting tailscaled (userspace) on proxy {proxy}")
        try:
            with open(ts_dir / "tailscaled.log", "a") as logf:
                subprocess.Popen(
                    [
                        tailscaled,
                        "--tun=userspace-networking",
                        f"--socks5-server=127.0.0.1:{port}",
                        f"--outbound-http-proxy-listen=127.0.0.1:{port}",
                        f"--socket={sock}",
                        f"--state={state}",
                        "--statedir", str(ts_dir),
                    ],
                    stdout=logf, stderr=subprocess.STDOUT,
                    start_new_session=True,  # survive turnloop restarts
                )
        except OSError as e:
            _log(ts_dir, f"failed to start tailscaled: {e}")
            return Status(ok=False, reason=f"tailscaled failed to start: {e}")
        # Wait for the control socket before `up`.
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.2)

    up_cmd = [
        tailscale, "--socket", str(sock), "up",
        f"--authkey={authkey}",
        "--accept-routes",  # reach subnet routes a rig advertises (e.g. a lab LAN)
    ]
    if hostname:
        up_cmd.append(f"--hostname={hostname}")
    tags = params.get("tags") or []
    if tags:
        up_cmd.append("--advertise-tags=" + ",".join(tags))
    up_cmd += list(params.get("up_args", []))

    try:
        proc = subprocess.run(up_cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as e:
        _log(ts_dir, f"`tailscale up` failed to run: {e}")
        return Status(ok=False, reason=f"tailscale up failed: {e}")
    if proc.returncode != 0:
        # Never log the command (it carries the auth key); log only stderr.
        _log(ts_dir, f"`tailscale up` exited {proc.returncode}: {proc.stderr.strip()}")
        return Status(ok=False, reason="tailscale up rejected the auth key or config")

    running, ip = _status_running(tailscale, sock)
    _write_env(ts_dir, proxy)
    _log(ts_dir, f"tailnet up: hostname={hostname} ip={ip} proxy={proxy}")
    return Status(ok=True, proxy=proxy, hostname=hostname, ip=ip)
