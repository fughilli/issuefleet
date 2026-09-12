"""CLI-specific commands and event handling for persistent worker sessions.

The turn loop owns process execution and lifecycle; these adapters describe
the two wire protocols. Neither runtime needs a Python SDK in the container.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from issuefleet.agent_runtime.turns import TurnState


def _flag(arg: str) -> str:
    """Include CLI's attached short values, e.g. -C/tmp and -mMODEL."""
    if arg.startswith("-") and not arg.startswith("--") and len(arg) >= 2:
        return arg[:2]
    return arg.split("=", 1)[0]


def validate_runtime_args(
    runtime: str, args: list[str], model: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Reject flags that defeat managed conversation identity or metadata."""
    if runtime not in ("claude", "codex"):
        raise ValueError(f"unsupported worker runtime: {runtime!r}")
    forbidden = {"--", "--help", "-h", "--version", "-V"}
    if runtime == "codex":
        forbidden |= {"--ephemeral", "--last", "--all", "--worktree", "--cd", "-C",
                      "--json", "resume", "fork", "review"}
        # This one persisted argument set also drives interactive takeover.
        # Exec-only switches would make `codex resume` fail at parse time.
        forbidden |= {"--color", "--ignore-rules", "--ignore-user-config",
                      "--output-last-message", "-o", "--output-schema",
                      "--skip-git-repo-check", "--thread-source"}
    else:
        forbidden |= {"--session-id", "--resume", "-r", "--continue", "-c",
                      "--fork-session", "--no-session-persistence", "--output-format",
                      "--input-format", "--print", "-p"}
    for arg in args:
        flag = _flag(arg)
        if flag in forbidden:
            raise ValueError(f"{runtime} runtime_args cannot contain {arg!r}")
        config = arg
        if arg.startswith("--config="):
            config = arg[len("--config="):]
        elif runtime == "codex" and arg.startswith("-c") and arg != "-c":
            config = arg[2:].lstrip("=")
        key = config.split("=", 1)[0].strip()
        if model and (flag in ("--model", "-m") or key == "model"):
            raise ValueError("worker model conflicts with a model override in runtime arguments")
        if reasoning_effort and (flag == "--effort" or key == "model_reasoning_effort"):
            raise ValueError("worker reasoning_effort conflicts with runtime arguments")


def command(state: TurnState) -> list[str]:
    extras = list(state.runtime_args)
    if state.runtime == "claude":
        extras += list(state.claude_args)
    validate_runtime_args(state.runtime, extras, state.model, state.reasoning_effort)
    if state.runtime == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        argv += ["--session-id" if state.turns_taken == 0 else "--resume", state.session_uuid]
        argv += list(state.claude_args) + list(state.runtime_args)
        if state.model:
            argv += ["--model", state.model]
        if state.reasoning_effort:
            argv += ["--effort", state.reasoning_effort]
        return argv
    argv = ["codex", "exec", "--json", *state.runtime_args]
    # Worker containers supply the isolation boundary. Codex's default
    # read-only sandbox cannot commit into the linked host git admin dir.
    # An explicit sandbox remains configurable through runtime arguments.
    if not any(_flag(arg) in {
        "--dangerously-bypass-approvals-and-sandbox", "--sandbox", "-s", "--approve-for-me",
    } for arg in state.runtime_args):
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    if state.model:
        argv += ["--model", state.model]
    if state.reasoning_effort:
        # JSON strings are valid TOML strings; quote values without shell
        # interpolation because subprocess passes each argument literally.
        argv += ["-c", f"model_reasoning_effort={json.dumps(state.reasoning_effort)}"]
    if state.runtime_session_id:
        if state.runtime_session_id.startswith("-"):
            raise ValueError("invalid Codex runtime_session_id")
        argv += ["resume", state.runtime_session_id]
    # Explicit stdin works for both initial exec and exec resume.
    return [*argv, "-"]


def parse_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _compact(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def summarize_codex(event: dict) -> str | None:
    kind = event.get("type")
    if kind == "thread.started":
        return f"· codex session {str(event.get('thread_id', ''))[:8]}"
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        return ("✓ turn complete "
                f"tokens in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')}")
    if kind in ("turn.failed", "error"):
        error = event.get("error") or event
        message = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
        return f"✗ turn errored: {message}"
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return None
    item_kind = item.get("type")
    if kind == "item.completed" and item_kind == "agent_message":
        return _compact(str(item.get("text", ""))) or None
    if kind == "item.started" and item_kind == "command_execution":
        return "→ command " + _compact(str(item.get("command", "")), 120)
    if kind == "item.completed" and item_kind == "command_execution":
        return f"· command exited {item.get('exit_code', '?')}"
    if kind == "item.completed" and item_kind == "file_change":
        paths = [str(change.get("path", "?")) for change in item.get("changes", [])]
        return "→ files " + _compact(", ".join(paths), 160)
    if kind == "item.started" and item_kind == "mcp_tool_call":
        return f"→ {item.get('server', '?')}/{item.get('tool', '?')}"
    if kind == "item.completed" and item_kind == "error":
        return f"· {item.get('message', 'runtime item error')}"
    return None


@dataclass
class CodexTurn:
    """Track protocol completion independently of the subprocess exit code."""

    session_id: str | None
    completed: bool = False
    failed: bool = False

    def observe(self, event: dict, agent_dir: Path) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
            if not isinstance(session_id, str) or not session_id or session_id.startswith("-"):
                self.failed = True
                return
            if self.session_id and session_id != self.session_id:
                self.failed = True
                return
            # Persist on the event, before the child can finish or crash.
            # agentctl can already have changed phase: never save the stale
            # state object supplied to run_runtime at the start of the turn.
            current = TurnState.load(agent_dir)
            if current.runtime_session_id and current.runtime_session_id != session_id:
                self.failed = True
                return
            current.runtime_session_id = session_id
            current.save(agent_dir)
            self.session_id = session_id
        elif kind == "turn.completed":
            self.completed = True
        elif kind in ("turn.failed", "error"):
            self.failed = True

    @property
    def succeeded(self) -> bool:
        return bool(self.session_id and self.completed and not self.failed)
