"""``issuefleet takeover <KEY>``: grab a worker's branch for a live, interactive
local session, then hand it straight back.

The operator runs ``bazel run //tools:takeover -- FUG-555`` (or ``issuefleet
takeover FUG-555``) and is dropped into an interactive claude-container session
on that worker's exact branch, *resuming its exact Claude conversation*. When
they exit, the branch returns to the worker pool and the headless worker picks
up where the interactive session left off.

Why it drives the daemon rather than mutating the fleet itself: releasing and
adopting a branch were deliberately routed through the reconcile loop's single
writer thread (FUG-113) — the web thread only enqueues; the tick thread does all
git/registry/tmux work, so nothing races it. This tool is a *third* process, so
it delegates those two transitions to the running daemon over the same dashboard
control channel the Release/Adopt buttons use (POST ``/worker/<KEY>/release`` and
``/worker/<KEY>/adopt``), and only does the interactive middle itself. That
middle is safe to run locally: a ``released`` worker is inert to the daemon (it's
skipped in ``_service``), so the local worktree this tool builds on the freed
branch can't collide with the tick thread.

The context the worker had rides along for free. Release remembers the worker's
Claude session id and turn count; the interactive session resumes that same
session (``claude --resume <uuid>`` against the same container config dir), so
the operator sees the full conversation. When the branch is adopted back, the
worker resumes *the same session again* — now including the operator's turns — so
it knows what transpired, and it gets the adopt flow's "an operator worked on
this locally; check ``git log``/``git status``" mailbox note on top.

Only committed work returns: the branch ref is shared, so commits made in the
interactive session survive without a push (adopt keeps a local branch that's
ahead of ``origin``). Uncommitted changes do not — the worktree is rebuilt on
adopt — so the tool reminds the operator to commit before they exit.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from issuefleet.config import Config
from issuefleet.gitops import Gitops
from issuefleet.model import (
    PHASE_ACTIVE,
    PHASE_CRASHED,
    PHASE_RELEASED,
    WorkerRecord,
)
from issuefleet.registry import Registry
from issuefleet.runner import TmuxRunner

log = logging.getLogger("issuefleet.takeover")

# How long to wait for the daemon's tick thread to carry out a release/adopt we
# enqueued. A tick fires immediately (the dashboard POST wakes the loop), so this
# is a generous ceiling on the git/tmux work, not the poll interval.
POLL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 1.0


class TakeoverError(Exception):
    """Anything that stops a takeover before/around the interactive session."""


class DaemonControl:
    """POST release/adopt to the running daemon's dashboard — the single-writer
    control channel FUG-113 built for exactly these transitions. Success is any
    2xx (the endpoints answer 303 and urllib follows the redirect to the worker
    page); a 404 means no such worker."""

    def __init__(self, base_url: str, opener=None):
        self.base_url = base_url.rstrip("/")
        # Injectable so tests exercise the request shape with no socket.
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))

    def reachable(self) -> bool:
        try:
            with self._open(urllib.request.Request(f"{self.base_url}/healthz")) as r:
                return 200 <= getattr(r, "status", 200) < 300
        except OSError:
            return False

    def _post(self, path: str) -> None:
        req = urllib.request.Request(f"{self.base_url}{path}", data=b"", method="POST")
        try:
            with self._open(req):
                pass
        except urllib.error.HTTPError as e:
            detail = "no such worker (already gone?)" if e.code == 404 else f"HTTP {e.code}"
            raise TakeoverError(f"daemon rejected POST {path}: {detail}") from e
        except OSError as e:
            raise TakeoverError(
                f"could not reach the daemon at {self.base_url}: {e}"
            ) from e

    def release(self, key: str) -> None:
        self._post(f"/worker/{urllib.parse.quote(key)}/release")

    def adopt(self, key: str) -> None:
        self._post(f"/worker/{urllib.parse.quote(key)}/adopt")


def dashboard_url(cfg: Config) -> str:
    """Where the takeover CLI reaches the daemon. ``ISSUEFLEET_DASHBOARD_URL``
    wins outright (a remote/tailnet daemon); otherwise the dashboard's own bind,
    with a wildcard bind dialed back to loopback for the client's sake."""
    override = os.environ.get("ISSUEFLEET_DASHBOARD_URL")
    if override:
        return override.rstrip("/")
    dcfg = cfg.dashboard
    if not dcfg.enabled:
        raise TakeoverError(
            "[dashboard] is disabled, but takeover drives release/adopt through it; "
            "enable it ([dashboard] enabled = true) or set ISSUEFLEET_DASHBOARD_URL"
        )
    host = os.environ.get("ISSUEFLEET_DASHBOARD_BIND", dcfg.bind)
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{dcfg.port}"


def interactive_command(
    rec: WorkerRecord, cfg: Config, session_uuid: str, resume: bool
) -> list[str]:
    """The host command for the operator's interactive session: the same
    launcher, config dir, and sibling git mounts a worker gets, but running a
    plain interactive ``claude`` (resuming the worker's session) instead of the
    headless turnloop."""
    cmd = [cfg.claude_container, "-w", rec.worktree]
    if cfg.container_config_dir is not None:
        cmd += ["-c", str(cfg.container_config_dir)]
    cmd += list(cfg.launcher_args)
    cmd += TmuxRunner._sibling_mount_args(rec, cfg)
    inner = ["claude"]
    if resume and session_uuid:
        # Same session id + same config dir => the worker's conversation resumes.
        inner += ["--resume", session_uuid]
    return cmd + inner


def _find(registry: Registry, key: str) -> WorkerRecord | None:
    return next(
        (w for w in registry.all() if w.issue_key.lower() == key.lower()), None
    )


def _wait_for_phase(
    registry: Registry, key: str, phase: str, sleep, timeout_s: float, interval_s: float
) -> WorkerRecord | None:
    """Re-read registry.json (the daemon rewrites it) until the worker reaches
    ``phase`` or we give up. Returns the record on success, else None."""
    tries = max(1, int(timeout_s / interval_s))
    for _ in range(tries):
        registry.reload()
        rec = _find(registry, key)
        if rec is not None and rec.phase == phase:
            return rec
        sleep(interval_s)
    return None


def _run_foreground(cmd: list[str]) -> int:
    # Inherits the operator's stdio/TTY: this IS the interactive session.
    return subprocess.call(cmd)


def run(
    cfg: Config,
    key: str,
    *,
    git: Gitops | None = None,
    control: DaemonControl | None = None,
    launch=None,
    sleep=time.sleep,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> int:
    """Release ``key``'s branch from the fleet, drop the operator into an
    interactive session on it, then adopt it back on exit. Collaborators are
    injectable so the whole flow is testable with no daemon, container, or git."""
    git = git or Gitops()
    launch = launch or _run_foreground
    registry = Registry(cfg.state_dir)
    rec = _find(registry, key)
    if rec is None:
        known = ", ".join(w.issue_key for w in registry.all()) or "none"
        raise TakeoverError(f"no worker for {key!r}; known: {known}")
    key = rec.issue_key  # canonical casing for the URL and prints

    control = control or DaemonControl(dashboard_url(cfg))
    if not control.reachable():
        raise TakeoverError(
            f"the issuefleet daemon isn't reachable at {control.base_url}. Takeover "
            "drives release/adopt through the running daemon; start it (`issuefleet "
            "run`) with the dashboard enabled, or point ISSUEFLEET_DASHBOARD_URL at it."
        )

    # 1. Release — unless it's already released (recover an interrupted takeover).
    if rec.phase == PHASE_RELEASED:
        print(f"{key} is already released; taking it over as-is.")
    elif rec.phase in (PHASE_ACTIVE, PHASE_CRASHED):
        print(f"Releasing {key} (branch {rec.branch}) from the fleet…")
        control.release(key)
        released = _wait_for_phase(
            registry, key, PHASE_RELEASED, sleep, timeout_s, interval_s
        )
        if released is None:
            raise TakeoverError(
                f"{key} didn't reach 'released' in {int(timeout_s)}s — check the daemon "
                "log; the branch was not taken over."
            )
        rec = released
    else:
        raise TakeoverError(f"{key} is in phase {rec.phase!r}; nothing to take over")

    worktree = Path(rec.worktree)
    resume = rec.released_turns > 0

    # 2. Rebuild a local worktree on the freed branch and launch the session.
    #    Everything past the release goes through the finally, so however it
    #    ends the branch is handed back rather than stranded in 'released'.
    rc = 0
    try:
        print(f"Building a local worktree at {worktree} on {rec.branch}…")
        git.create_worktree(Path(rec.repo), rec.branch, rec.base_ref, worktree)
        cmd = interactive_command(rec, cfg, rec.session_uuid, resume)
        print(
            "\nDropping into an interactive session"
            + (" (resuming the worker's Claude conversation)" if resume else "")
            + f".\n  branch:  {rec.branch}\n  worktree: {worktree}\n"
            "COMMIT anything you want to keep before you exit — only committed work "
            "returns to the fleet; the worktree is rebuilt on adopt.\n"
        )
        rc = launch(cmd)
    finally:
        # 3. Hand the branch back — always, even on Ctrl-C, so it never strands
        #    in 'released'. The daemon rebuilds the worktree on adopt, so remove
        #    ours first (a leftover worktree would block adopt's re-add).
        print(f"\nAdopting {rec.branch} back into the fleet…")
        try:
            git.remove_worktree(Path(rec.repo), worktree, rec.branch)
        except Exception:
            log.exception("removing the interactive worktree failed; adopt may stall")
        control.adopt(key)
        adopted = _wait_for_phase(
            registry, key, PHASE_ACTIVE, sleep, timeout_s, interval_s
        )
        if adopted is None:
            print(
                f"{key}: adopt queued — the worker will resume shortly "
                "(watch it on the dashboard or with `issuefleet status`)."
            )
        else:
            print(f"{key} is back with the fleet; the worker is resuming its session.")
    return rc
