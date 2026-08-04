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
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from issuefleet.config import Config
from issuefleet.model import WorkerRecord

log = logging.getLogger("issuefleet.runner")

# script(1) has two incompatible command-line interfaces, and picking the wrong
# one fails in the worst possible way: script exits instantly on the unknown
# flag, so the log it was supposed to create never exists — destroying the very
# diagnostic that would explain the failure.
_BSD_SCRIPT_PLATFORMS = ("darwin", "freebsd", "openbsd", "netbsd", "dragonfly")


def _script_wrapper(cmd: list[str], log_path: Path) -> str:
    """A shell string that runs ``cmd`` under script(1), teeing to ``log_path``.

    util-linux (Linux, the container deploy target):
        script -q -e -f -c '<cmd>' <file>
    BSD (macOS and the *BSDs, the local-dev target):
        script -q -e -t 0 <file> <cmd...>

    macOS rejects ``-c`` outright (``script: illegal option -- c``), which is
    why the form has to be chosen per platform rather than assuming either one.

    **Both need an explicit flush flag, and neither flushes by default.** BSD's
    ``-t`` is a flush interval defaulting to THIRTY SECONDS; util-linux buffers
    until the child exits unless given ``-f``. Either way a long-running worker's
    output sits in script's buffer: the pane log reads empty for the entire time
    the agent is working, `issuefleet logs` shows nothing, and a killed session
    loses the buffer outright. It only *looked* fine because a launcher that dies
    immediately flushes on exit — which is the one case the original comment
    was written against.
    """
    if sys.platform.startswith(_BSD_SCRIPT_PLATFORMS):
        return f"exec script -q -e -t 0 {shlex.quote(str(log_path))} {shlex.join(cmd)}"
    return (
        f"exec script -q -e -f -c {shlex.quote(shlex.join(cmd))} {shlex.quote(str(log_path))}"
    )


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

    def env_path(self, rec: WorkerRecord) -> Path:
        return self.log_dir / f"{rec.tmux_session}.env"

    def _write_env_file(self, rec: WorkerRecord, config: Config) -> Path | None:
        """Materialize [agent.env] for one worker, or None if it's empty.

        The launcher forwards a variable to the container BY NAME (overlay.json
        "env"), so the value has to be in the launcher's own environment — and
        tmux does not carry the caller's environment into a detached session
        (an existing tmux server's environment wins), so it must be injected
        into the session command itself.

        A 0600 file rather than `env VAR=value ...` in the command: the command
        is visible in `ps` and echoed into the worker log on failure, and a
        Tailscale auth key has no business in either. The session shell sources
        this file and deletes it in the same breath (see `start`), so it exists
        for milliseconds; `stop` sweeps it up if the session died first."""
        if not config.worker_env:
            return None
        lines, missing = [], []
        for name, src in sorted(config.worker_env.items()):
            value = src.resolve()
            if value is None:
                missing.append(f"{name} (from {src.describe()})")
                continue
            lines.append(f"{name}={shlex.quote(value)}")
        if missing:
            # Not fatal: the container's overlay decides what to do without it
            # (led_mapper's skips the tailnet join and says so).
            log.warning(
                "worker %s: no value for %s — the container will start without it",
                rec.tmux_session, ", ".join(missing),
            )
        if not lines:
            return None
        path = self.env_path(rec)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create restricted from the start; never widen an existing file.
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def start(self, rec: WorkerRecord, config: Config) -> None:
        if self.alive(rec):
            return  # idempotent: adopt the live session
        self.log_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.command(rec, config)
        log_path = self.log_path(rec)
        # Run the launcher under script(1) rather than pipe-pane: script
        # gives it a real pty (docker run -it needs one) AND flushes all
        # output to the log file, so even a launcher that dies in <1s is
        # captured. pipe-pane raced this and lost — the session vanished
        # before it could attach, leaving an empty log and no diagnosis.
        wrapped = _script_wrapper(cmd, log_path)
        env_path = self._write_env_file(rec, config)
        if env_path is not None:
            # `set -a` exports what the file defines, and the rm runs before
            # exec so the secret is off disk as soon as it is in the process.
            quoted = shlex.quote(str(env_path))
            wrapped = f"set -a; . {quoted}; set +a; rm -f {quoted}; {wrapped}"
        _tmux(["new-session", "-d", "-s", rec.tmux_session, "sh", "-c", wrapped])
        time.sleep(1.0)
        if not self.alive(rec):
            tail = ""
            try:
                tail = log_path.read_text()[-800:].strip()
            except OSError:
                pass
            # An empty log is itself a clue and used to be an unexplained one:
            # if the wrapper never got as far as opening it, the failure is in
            # the wrapper, not the launcher. Print the shell line we actually
            # ran so that case is diagnosable without reading this source.
            log.error(
                "worker session %s died within 1s of launch. Captured output:\n%s\n"
                "Shell line that was run:\n  %s\n"
                "Reproduce the launcher directly with:\n  %s",
                rec.tmux_session,
                tail or "(log empty — script(1) may have failed before opening it)",
                wrapped, shlex.join(cmd),
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
        # Backstop: the session shell removes this itself the moment it has
        # sourced it, so this only fires when the session never got that far.
        self.env_path(rec).unlink(missing_ok=True)
