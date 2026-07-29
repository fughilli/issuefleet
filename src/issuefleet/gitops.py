"""Real git operations (subprocess). Implements the Git port.

Worktrees are created idempotently — an existing worktree on the right
branch is adopted, matching the restart-safety rule.

Exclusion of ``.agent/``: the brief suggested the per-worktree
``.git/worktrees/<name>/info/exclude``, but git (verified on 2.43) does not
read that file — only ``$GIT_COMMON_DIR/info/exclude`` is consulted, even
from linked worktrees. So we append to the common one: still uncommitted
local state, invisible to the repo's content and collaborators, and never
the repo's ``.gitignore``. The pattern is shared by all worktrees of that
repo, which is what every issuefleet worker wants anyway. Resolved via
``git rev-parse --git-common-dir`` from inside the worktree, which also
handles the nested-worktree case where the main checkout is itself linked.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("issuefleet.git")


class GitError(Exception):
    pass


def _git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


class Gitops:
    def create_worktree(self, repo: Path, branch: str, base_ref: str, path: Path) -> None:
        path = Path(path)
        if (path / ".git").exists():
            head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
            if head == branch:
                return  # adopt
            raise GitError(f"worktree {path} exists but is on {head!r}, expected {branch!r}")
        # A registered-but-deleted worktree blocks re-adding; prune first.
        _git(["worktree", "prune"], cwd=repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._branch_exists(repo, branch):
            _git(["worktree", "add", str(path), branch], cwd=repo)
        else:
            _git(["worktree", "add", "-b", branch, str(path), base_ref], cwd=repo)

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            capture_output=True,
        )
        return proc.returncode == 0

    def add_worktree_exclude(self, repo: Path, path: Path, pattern: str) -> None:
        common = Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path))
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if pattern not in existing.splitlines():
            with open(exclude, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(pattern + "\n")

    def has_commits_ahead(self, worktree: Path, base_ref: str) -> bool:
        for ref in (f"origin/{base_ref}", base_ref):
            try:
                return int(_git(["rev-list", "--count", f"{ref}..HEAD"], cwd=worktree)) > 0
            except GitError:
                continue
        raise GitError(f"cannot resolve base ref {base_ref!r} (or origin/{base_ref}) in {worktree}")

    def push(self, worktree: Path, branch: str) -> None:
        # force-with-lease: a post-review rebase updates the PR without
        # clobbering a concurrent push (brief §4.4). SSH remote, no token.
        _git(["push", "--force-with-lease", "origin", f"{branch}:{branch}"], cwd=worktree)

    def remove_worktree(self, repo: Path, path: Path, branch: str) -> None:
        if Path(path).exists():
            _git(["worktree", "remove", "--force", str(path)], cwd=repo)
        _git(["worktree", "prune"], cwd=repo)

    def delete_remote_branch(self, repo: Path, branch: str) -> None:
        """Called only after a merge: drop the remote branch, and the local
        one with it (best-effort each — the remote may already be gone if
        GitHub auto-deleted it)."""
        try:
            _git(["push", "origin", "--delete", branch], cwd=repo)
        except GitError as e:
            log.warning("remote branch %s: %s", branch, e)
        try:
            _git(["branch", "-D", branch], cwd=repo)
        except GitError as e:
            log.warning("local branch %s: %s", branch, e)

    # -- doctor helpers ----------------------------------------------------

    def remote_url(self, repo: Path) -> str:
        return _git(["remote", "get-url", "origin"], cwd=repo)

    def is_repo(self, repo: Path) -> bool:
        try:
            _git(["rev-parse", "--git-dir"], cwd=repo)
            return True
        except (GitError, FileNotFoundError, NotADirectoryError):
            return False
