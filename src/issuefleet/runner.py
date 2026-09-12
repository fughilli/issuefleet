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
identify containers at teardown by their exact workspace mount and the
launcher's workspace-derived name, rather than guessing its embedded pid.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from issuefleet.agent_runtime.turns import TurnState
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


def worker_state(rec: WorkerRecord) -> TurnState | WorkerRecord:
    """Runtime settings survive configuration edits and removed worktrees."""
    agent_dir = Path(rec.worktree) / ".agent"
    if (agent_dir / "state.json").exists():
        return TurnState.load(agent_dir)
    return rec


def worker_runtime(rec: WorkerRecord) -> str:
    """An existing worker keeps its persisted runtime across config changes."""
    return worker_state(rec).runtime


def worker_codex_home(rec: WorkerRecord, config: Config) -> Path:
    state = worker_state(rec)
    return Path(state.runtime_home or config.codex_home).resolve()


def container_exec(argv: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Preserve argv through launchers that expand an unquoted `$COMMAND`.

    Shell quoting cannot survive that expansion: quotes become literal bytes.
    The fixed Python bootstrap and encoded, nonsecret payload contain no shell
    whitespace or glob characters. Python is already required by the turnloop.
    No shell evaluates the decoded values, and exec preserves signal delivery.
    """
    payload = base64.b64encode(
        json.dumps({"argv": argv, "env": env or {}}).encode()
    ).decode()
    bootstrap = (
        "s=__import__('json').loads(__import__('base64').b64decode(__import__('sys').argv.__getitem__(1)));"
        "__import__('os').environ.update(s.get('env'));"
        "__import__('os').execvp(s.get('argv').__getitem__(0),s.get('argv'))"
    )
    return ["python3", "-c", bootstrap, payload]


def _tmux(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise RunnerError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def _docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError("cannot verify worker container state: Docker is unavailable") from exc
    if check and proc.returncode:
        raise RunnerError(f"docker {args[0]} failed while stopping worker: {proc.stderr.strip()}")
    return proc


def _workspace_container_ids() -> set[str]:
    # This filter selects the launcher's mount DESTINATION, not an approximate
    # name prefix. Source and full launcher identity are checked after inspect.
    return set(_docker(["ps", "--all", "--quiet", "--no-trunc",
                        "--filter", "volume=/workspace"]).stdout.split())


def _worker_containers(rec: WorkerRecord) -> dict[str, bool]:
    """Verified container IDs and running state for exactly this worktree.

    claude-container 1.7 names a container cc-<basename>-<sha256(path)[:12]>-<pid>.
    Require that identity AND its exact /workspace bind mount before stopping
    anything. An operator container on the same worktree with an unknown name
    is an error, never an invitation to kill it. Inspect only metadata, not
    environment variables or other configuration that can contain credentials.
    """
    paths = {str(Path(rec.worktree).absolute()), str(Path(rec.worktree).resolve())}
    prefixes = set()
    for path in paths:
        basename = re.sub(r"[^a-zA-Z0-9_.-]", "-", Path(path).name).rstrip("-")
        digest = hashlib.sha256(path.encode()).hexdigest()[:12]
        prefixes.add(f"cc-{basename}-{digest}-")
    target = Path(rec.worktree).resolve()
    found = {}
    template = ('{"Id":{{json .Id}},"Name":{{json .Name}},'
                '"Running":{{json .State.Running}},"Mounts":{{json .Mounts}}}')
    for cid in sorted(_workspace_container_ids()):
        proc = _docker(["inspect", "--type", "container", "--format", template, cid], check=False)
        if proc.returncode:
            # --rm containers may disappear between ps and inspect. A fresh,
            # successful listing proves this is removal, not a Docker outage.
            if cid not in _workspace_container_ids():
                continue
            raise RunnerError(f"cannot inspect container {cid} while stopping {rec.tmux_session}")
        try:
            metadata = json.loads(proc.stdout)
            matches = any(
                mount.get("Type") == "bind" and mount.get("Destination") == "/workspace"
                and Path(mount["Source"]).resolve() == target
                for mount in metadata["Mounts"]
            )
            if not matches:
                continue
            name = metadata["Name"].lstrip("/")
            identity_ok = any(name.startswith(prefix) and name[len(prefix):].isdigit()
                              for prefix in prefixes)
            if not identity_ok or metadata["Id"] != cid:
                raise RunnerError(f"unrecognized container {name!r} mounts {rec.worktree}; "
                                  "refusing to stop it or remove the worktree")
            if not isinstance(metadata["Running"], bool):
                raise ValueError("missing container running state")
            found[cid] = metadata["Running"]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RunnerError(f"invalid Docker metadata while stopping {rec.tmux_session}") from exc
    return found


class TmuxRunner:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def command(self, rec: WorkerRecord, config: Config) -> list[str]:
        """The host command a worker session runs, using its persisted runtime."""
        return self.launcher_command(
            rec, config, ["/workspace/.agent/bin/turnloop", "run"]
        )

    @staticmethod
    def launcher_command(rec: WorkerRecord, config: Config, inner: list[str]) -> list[str]:
        """The shared container boundary for headless and interactive workers."""
        cmd = [config.claude_container, "-w", rec.worktree]
        if config.container_config_dir is not None:
            cmd += ["-c", str(config.container_config_dir)]
        # Launcher flags must precede the command: the launcher treats the
        # first non-option argument as the start of the in-container command.
        cmd += list(config.launcher_args)
        cmd += TmuxRunner._sibling_mount_args(rec, config)
        runtime = worker_runtime(rec)
        if runtime == "codex":
            codex_home = worker_codex_home(rec, config)
            # Never silently mount ~/.codex: it can contain unrelated sessions,
            # plugins and credentials. Only the configured worker home crosses
            # this boundary; provider keys for the manager stay on the host.
            cmd += ["--mount", str(codex_home)]
            inner = container_exec(inner, {"CODEX_HOME": str(codex_home)})
        elif runtime != "claude":
            raise RunnerError(f"unsupported persisted worker runtime {runtime!r}")
        elif any(any(c.isspace() or c in "*?[" for c in word) for word in inner):
            inner = container_exec(inner)
        if config.container_image:
            # No secret values here; env is needed for foreground takeovers as
            # well as tmux, whose server environment may predate this config.
            cmd = ["env", f"CLAUDE_IMAGE={config.container_image}", *cmd]
        return cmd + inner

    @staticmethod
    def _sibling_mount_args(rec: WorkerRecord, config: Config) -> list[str]:
        """Same-path `--mount` flags for every sibling project's git-common-dir,
        so `agentctl upstream-checkout` can open a linked worktree of a sibling
        inside `/workspace/siblings/<name>` and have its absolute `.git` pointer
        resolve in-container (the launcher only mounts the `-w` worktree's own
        repo). We mount the sibling clone's `.git` at its identical host path —
        which is also what makes its shared Bazel cache under `<.git>/bazel-cache`
        reachable and warm. All siblings are mounted up front (the container
        starts once, before any checkout), so which one a worker actually uses is
        decided later with no relaunch.

        Empty unless enabled and there is more than one project: a single-project
        or opt-out fleet emits nothing and runs on any launcher. A sibling whose
        clone isn't on disk yet is skipped (it can't be checked out anyway)."""
        if not config.mount_sibling_git:
            return []
        args: list[str] = []
        for p in config.projects:
            if p.name == rec.project:
                continue
            gitdir = Path(p.repo) / ".git"
            if gitdir.is_dir():
                args += ["--mount", str(gitdir)]
        return args

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
        # Losing tmux or its Docker client can leave the old container alive.
        # A replacement must not share its checkout and conversation with an
        # orphan. Discovery also fails closed on unknown owners/Docker errors.
        if _worker_containers(rec):
            self.stop(rec)
        if worker_runtime(rec) == "codex" and not worker_codex_home(rec, config).is_dir():
            raise RunnerError(
                f"Codex home {worker_codex_home(rec, config)} is missing; authenticate the dedicated "
                "worker home first (see docs/CODEX_RUNTIME.md)"
            )
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
        # A dead tmux pane does NOT prove its Docker container has stopped.
        # Stop the verified container while the one-shot launcher is attached,
        # then close tmux and verify again before callers archive/delete files.
        containers = _worker_containers(rec)
        if self.alive(rec) and not containers:
            # The launcher may still be building an image or submitting docker
            # create. Killing its terminal here could leave that request alive
            # and allow a worker to start after the caller removes its worktree.
            raise RunnerError(f"{rec.tmux_session}: launcher is live but no worker container "
                              "can be verified; retry after container startup completes")
        for cid, running in containers.items():
            if running:
                stopped = _docker(["stop", "--time", "10", cid], check=False)
                if stopped.returncode and cid in _workspace_container_ids():
                    raise RunnerError(f"could not stop worker container {cid}; worktree is preserved")
            # A created-but-not-started container is not yet a stopped worker:
            # an in-flight docker run can start it after the client disappears.
            removed = _docker(["rm", "--force", cid], check=False)
            if removed.returncode and cid in _workspace_container_ids():
                raise RunnerError(f"could not remove worker container {cid}; worktree is preserved")
        if _worker_containers(rec):
            raise RunnerError(f"{rec.tmux_session}: worker container is still running or present")
        _tmux(["kill-session", "-t", f"={rec.tmux_session}"], check=False)
        if self.alive(rec) or _worker_containers(rec):
            raise RunnerError(f"{rec.tmux_session}: could not confirm worker termination")
        # Backstop: the session shell removes this itself the moment it has
        # sourced it, so this only fires when the session never got that far.
        self.env_path(rec).unlink(missing_ok=True)
