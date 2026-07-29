"""Worker provisioning: build the ``.agent/`` runtime inside a fresh worktree.

The target repo stays untouched — everything lives under ``.agent/`` (ignored
via the per-worktree info/exclude, handled by the Git port) and the runtime is
a staged copy of this very package, so it is version-matched to the
orchestrator by construction (brief §5.4, option 1).
"""

from __future__ import annotations

import shutil
import stat
import uuid
from pathlib import Path

import issuefleet
from issuefleet.agent_runtime.turns import TurnState
from issuefleet.mailbox import Mailbox
from issuefleet.prompts import render_brief

_ENTRY_TEMPLATE = """\
#!/usr/bin/env python3
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from issuefleet.agent_runtime.{module} import main

sys.exit(main())
"""


def stage_runtime(bin_dir: Path) -> None:
    """Copy the issuefleet package (py sources only) into <agent>/bin and
    write the agentctl/turnloop entry scripts."""
    pkg_src = Path(issuefleet.__file__).resolve().parent
    pkg_dst = bin_dir / "issuefleet"
    if pkg_dst.exists():
        shutil.rmtree(pkg_dst)
    shutil.copytree(
        pkg_src,
        pkg_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "BUILD.bazel"),
    )
    for module in ("agentctl", "turnloop"):
        entry = bin_dir / module
        entry.write_text(_ENTRY_TEMPLATE.format(module=module))
        entry.chmod(entry.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def provision(worktree: Path, issue, branch: str, base_ref: str, config) -> str:
    """Create/refresh the .agent dir. Idempotent: an existing state.json is
    preserved (re-adoption after an orchestrator restart must not reset the
    turn counters or the session), everything else is (re)staged.

    Returns the worker's Claude session UUID.
    """
    agent_dir = Path(worktree) / ".agent"
    bin_dir = agent_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    Mailbox(agent_dir / "mailbox").ensure()
    (agent_dir / "logs").mkdir(exist_ok=True)
    (agent_dir / "brief.md").write_text(render_brief(issue, branch, base_ref))
    stage_runtime(bin_dir)

    state_path = agent_dir / "state.json"
    if state_path.exists():
        return TurnState.load(agent_dir).session_uuid
    state = TurnState(
        session_uuid=str(uuid.uuid4()),
        max_auto_turns=config.max_auto_turns,
        claude_args=list(config.claude_args),
    )
    state.save(agent_dir)
    return state.session_uuid
