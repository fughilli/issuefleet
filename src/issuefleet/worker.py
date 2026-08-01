"""Worker provisioning: build the ``.agent/`` runtime inside a fresh worktree.

The target repo stays untouched — everything lives under ``.agent/`` (ignored
via the per-worktree info/exclude, handled by the Git port) and the runtime is
a staged copy of this very package, so it is version-matched to the
orchestrator by construction (brief §5.4, option 1).
"""

from __future__ import annotations

import json
import shutil
import stat
import uuid
from pathlib import Path

import issuefleet
from issuefleet import creds
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


def inherit_repo_files(repo: Path, worktree: Path, rel_paths: list[str]) -> list[str]:
    """Copy launcher-local workspace state (e.g. claude-container's skill
    approval) from the parent checkout into a fresh worktree, so headless
    launches don't stop at interactive confirmation prompts the operator
    already answered once in the main checkout.

    Copy-if-missing per file: whatever the checkout already provides
    (tracked files) always wins, only absent files are filled in. Returns
    the relative paths that exist in the parent repo, with a trailing slash
    for directories, so the caller can git-exclude them in the worktree —
    an agent's `git add .` must never sweep this state into a commit.
    """
    inherited: list[str] = []
    for rel in rel_paths:
        src = Path(repo) / rel
        dst = Path(worktree) / rel
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if not f.is_file():
                    continue
                target = dst / f.relative_to(src)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
            inherited.append(rel.rstrip("/") + "/")
        elif src.is_file():
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            inherited.append(rel)
    return inherited


def stage_tailscale(worktree: Path, issue, project, config) -> bool:
    """Deliver the worker's tailnet auth key + bring-up params into
    ``.agent/tailscale/`` (FUG-40), when tailscale is enabled for this project
    and a key resolves. Returns True if tailnet material was staged.

    A no-op that also *clears* any stale material otherwise, so toggling the
    feature off (or dropping a project's opt-in) between claims never leaves a
    live key sitting in a worktree. ``.agent/`` is git-excluded and owned by the
    daemon, so the key never rides into a commit and the container reads it at
    the same absolute path it is written (the worktree is bind-mounted)."""
    ts_dir = Path(worktree) / ".agent" / "tailscale"
    if not config.tailscale_enabled_for(project):
        shutil.rmtree(ts_dir, ignore_errors=True)
        return False
    authkey = creds.resolve_tailscale_authkey(config)
    if not authkey:
        shutil.rmtree(ts_dir, ignore_errors=True)
        return False
    ts_dir.mkdir(parents=True, exist_ok=True)
    params = {
        "hostname": config.tailscale.hostname_template.format(key=issue.key.lower()),
        "tags": list(config.tailscale.tags),
        "up_args": list(config.tailscale.up_args),
        "proxy_port": config.tailscale.proxy_port,
    }
    (ts_dir / "params.json").write_text(json.dumps(params, indent=2))
    keyfile = ts_dir / "authkey"
    # Write restricted from the start: create at 0600 rather than write-then-chmod
    # so the key is never briefly world-readable.
    keyfile.touch(mode=0o600, exist_ok=True)
    keyfile.chmod(0o600)
    keyfile.write_text(authkey)

    # Teach the agent how to use the tailnet, in its first-turn context. Safe
    # to append: provision() rewrites brief.md fresh on every (re)provision, so
    # this never accumulates.
    brief = Path(worktree) / ".agent" / "brief.md"
    if brief.exists():
        with open(brief, "a") as f:
            f.write(_TAILNET_BRIEF.format(port=config.tailscale.proxy_port))
    return True


_TAILNET_BRIEF = """\

## Tailnet access (shared rigs)

This worker is on the operator's tailnet, so you can reach shared resources (a
HITL rig, a device on a lab LAN). This does **not** override the "no network
credentials" rule above — it only routes to tailnet peers, and only when you
opt a command in. Your normal traffic is unaffected:

    source .agent/tailscale/env && ssh user@rig-host        # per-shell opt-in
    ALL_PROXY=socks5://127.0.0.1:{port} curl http://rig-host/  # per-command

`.agent/tailscale/env` exports ALL_PROXY/HTTP(S)_PROXY pointing at the local
Tailscale proxy. Run `tailscale status` to list reachable peers. If the tailnet
isn't up, see `.agent/tailscale/bringup.log`.
"""


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
