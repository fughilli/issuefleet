"""Worker sessions: detached host tmux running claude-container.

Why tmux rather than a hand-rolled ``docker run`` (brief §5.1 asked for the
choice to be written down): the launcher requires a pty on stdin
(``docker run --rm -it``), which tmux provides for free; the operator gets
``tmux attach`` to watch or take over any worker live; output is captured
with ``pipe-pane -o`` so the pty stays intact; and — decisively — the
launcher's first-class handling of linked-worktree ``.git`` mounts would
otherwise have to be replicated by hand and kept in sync with every launcher
release. One worker = one tmux session = one container.

Session names are deterministic (``issuefleet-<project>-<KEY>``), so
adoption after an orchestrator restart is a ``tmux has-session`` away — we
never need to guess container names (which embed a pid).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from pathlib import Path

from issuefleet.config import Config
from issuefleet.model import WorkerRecord

log = logging.getLogger("issuefleet.runner")


class RunnerError(Exception):
    pass


def _tmux(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise RunnerError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


class TmuxRunner:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def command(self, rec: WorkerRecord, config: Config) -> list[str]:
        """The host command a worker session runs. The launcher word-splits
        its command argument, so the in-container part must be plain
        space-separated words — ``/workspace/.agent/bin/turnloop run`` is."""
        cmd = [config.claude_container, "-w", rec.worktree]
        if config.container_config_dir is not None:
            cmd += ["-c", str(config.container_config_dir)]
        # Launcher flags must precede the command: the launcher treats the
        # first non-option argument as the start of the in-container command.
        cmd += list(config.launcher_args)
        cmd += ["/workspace/.agent/bin/turnloop", "run"]
        return cmd

    def log_path(self, rec: WorkerRecord) -> Path:
        return self.log_dir / f"{rec.tmux_session}.log"

    def start(self, rec: WorkerRecord, config: Config) -> None:
        if self.alive(rec):
            return  # idempotent: adopt the live session
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # A brief exec prelude so pipe-pane attaches before the command can
        # die: a launcher that exits instantly used to vanish before its
        # output was ever captured (observed live — the only evidence was
        # "can't find pane"). exec keeps the pty; no pipes involved.
        wrapped = f"sleep 0.3; exec {shlex.join(self.command(rec, config))}"
        _tmux(["new-session", "-d", "-s", rec.tmux_session, "sh", "-c", wrapped])
        # Capture output without stealing the pty.
        proc = _tmux(
            [
                "pipe-pane",
                "-o",
                "-t",
                f"={rec.tmux_session}",
                f"cat >> {shlex.quote(str(self.log_path(rec)))}",
            ],
            check=False,
        )
        if proc.returncode != 0:
            # Not fatal (the worker runs fine unlogged; turn logs still land
            # in the worktree) but silently missing pane logs cost real
            # debugging time — say so.
            log.warning("pipe-pane for %s failed (%s); pane log %s will be empty",
                        rec.tmux_session, proc.stderr.strip(), self.log_path(rec))
        time.sleep(1.0)
        if not self.alive(rec):
            log.error(
                "worker session %s died within 1s of launch — the launcher itself is "
                "failing. Its dying words are in %s; reproduce interactively with:\n"
                "  %s",
                rec.tmux_session, self.log_path(rec),
                " ".join(self.command(rec, config)),
            )

    def alive(self, rec: WorkerRecord) -> bool:
        return _tmux(["has-session", "-t", f"={rec.tmux_session}"], check=False).returncode == 0

    def stop(self, rec: WorkerRecord) -> None:
        # Killing the session drops the pty; `docker run --rm -it` exits and
        # cleans up the container. The shutdown mailbox message was written
        # first (teardown order), so a between-turns loop exits on its own;
        # a mid-turn agent is cut off — its commits and mailbox survive and
        # were archived before this call.
        _tmux(["kill-session", "-t", f"={rec.tmux_session}"], check=False)
