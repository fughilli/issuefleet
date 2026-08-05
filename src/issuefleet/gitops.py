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
import os
import shutil
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


def _auth_args(auth_header: str | None) -> list[str]:
    # http.extraheader keeps the token out of remote URLs (which git echoes
    # into error messages) — same technique actions/checkout uses.
    return ["-c", f"http.extraheader=AUTHORIZATION: {auth_header}"] if auth_header else []


def ensure_checkout(
    git: "Gitops", project, clone_url: str | None = None, auth_header: str | None = None
) -> str | None:
    """Bootstrap a project's main checkout at daemon startup (never doctor —
    it must stay side-effect-free). Returns a description of what was done,
    or None if the repo was already in place. Raises GitError on dead ends.

    `repo` is always a clone the daemon owns: existing repo wins, otherwise
    clone from git_url. Pointing it at a checkout elsewhere on the machine
    is deliberately not supported — the path has to resolve identically for
    the daemon and for the worker containers it launches, which a checkout
    outside the mounted tree does not."""
    repo = Path(project.repo)
    if git.is_repo(repo):
        return None
    if repo.is_symlink() and not repo.exists():
        raise GitError(
            f"{repo} is a symlink to a missing target "
            f"({os.readlink(repo)}); remove or fix it"
        )
    if project.git_url:
        # Prefer the caller-supplied HTTPS URL + token (scoped app auth, no
        # SSH key needed); fall back to the configured remote as-is.
        url = clone_url or project.git_url
        git.clone(url, repo, auth_header=auth_header if clone_url else None)
        return f"cloned from {url}"
    raise GitError(
        f"{repo} does not exist and the project has no git_url to clone from"
    )


class Gitops:
    def clone(self, url: str, path: Path, auth_header: str | None = None) -> None:
        """Bootstrap a missing main checkout (daemon startup, not doctor)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git([*_auth_args(auth_header), "clone", url, str(path)])

    def fetch(self, repo: Path, url: str | None = None, auth_header: str | None = None) -> None:
        """Refresh the clone's remote-tracking refs (``origin/*``) before a
        worker is cut. A linked worktree shares this clone's object store, so
        this is also what lets the worker CONTAINER check out any branch —
        e.g. ``git switch origin/some-branch`` to reproduce a report there —
        with no network access or credential of its own: the daemon pulls the
        refs here, once, with its scoped forge token. Fetches from the forge
        URL (token via one-shot http.extraheader, never persisted) into the
        origin/* namespace, and prunes deleted branches so a stale
        remote-tracking ref can't shadow a real one."""
        _git(
            [
                *_auth_args(auth_header),
                "fetch",
                "--prune",
                url or "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            cwd=repo,
        )

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
            _git(["worktree", "add", "-b", branch, str(path), self._base(repo, base_ref)], cwd=repo)

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return self._ref_exists(repo, f"refs/heads/{branch}")

    def _ref_exists(self, repo: Path, ref: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo,
            capture_output=True,
        )
        return proc.returncode == 0

    def _base(self, repo: Path, base_ref: str) -> str:
        """Cut new worker branches from the freshly-fetched remote-tracking
        ref (``origin/<base_ref>``) so they start from the latest base, not
        the daemon's local base branch — which git won't fast-forward while
        it's checked out in the primary worktree, so it stays pinned at clone
        time. Falls back to the bare ref when there's no ``origin/<base_ref>``
        (offline bootstrap, or a local-only base)."""
        remote = f"origin/{base_ref}"
        return remote if self._ref_exists(repo, f"refs/remotes/{remote}") else base_ref

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

    def _remotes(self, repo: Path) -> list[str]:
        # Tolerant: a repo whose remotes can't be listed still gets the
        # bare-ref fallback in has_commits_ahead rather than a hard failure.
        try:
            return _git(["remote"], cwd=repo).split()
        except GitError:
            return []

    def _base_candidates(self, worktree: Path, base_ref: str) -> list[str]:
        """Where the base ref might resolve, most-preferred first: the base on
        each configured remote (preferring ``origin`` — the daemon's own clones
        use it, but an adopted operator clone may name its remote something
        else), then the bare local ref (a local-only base, or an offline
        bootstrap with no remote-tracking ref yet)."""
        remotes = self._remotes(worktree)
        ordered = (["origin"] if "origin" in remotes else []) + [
            r for r in remotes if r != "origin"
        ]
        return [*(f"{r}/{base_ref}" for r in ordered), base_ref]

    def has_commits_ahead(self, worktree: Path, base_ref: str) -> bool:
        """Does HEAD carry commits the base doesn't?"""
        candidates = self._base_candidates(worktree, base_ref)
        for ref in candidates:
            try:
                return int(_git(["rev-list", "--count", f"{ref}..HEAD"], cwd=worktree)) > 0
            except GitError:
                continue
        raise GitError(
            f"cannot resolve base ref {base_ref!r} in {worktree} (tried: {', '.join(candidates)})"
        )

    def diff(self, worktree: Path, base_ref: str) -> str:
        """The unified diff a `ready` would contribute: HEAD against its
        merge-base with the resolved base (``base...HEAD``, three-dot), so only
        this branch's own additions show — commits the base gained in the
        meantime don't pollute the scan. Resolves the base the same way as
        ``has_commits_ahead``."""
        candidates = self._base_candidates(worktree, base_ref)
        for ref in candidates:
            try:
                return _git(["diff", "--unified=0", f"{ref}...HEAD"], cwd=worktree)
            except GitError:
                continue
        raise GitError(
            f"cannot resolve base ref {base_ref!r} in {worktree} (tried: {', '.join(candidates)})"
        )

    def _is_ancestor(self, worktree: Path, maybe_ancestor: str, descendant: str) -> bool:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
            cwd=worktree,
            capture_output=True,
        )
        return proc.returncode == 0

    def sync_to_remote(self, worktree: Path, branch: str) -> str:
        """Fast-forward an adopted worker branch onto its remote tip, so a
        restarted agent never resumes on stale code. Returns ``"no-remote"``
        (nothing pushed yet, or the ref isn't fetched), ``"up-to-date"``,
        ``"fast-forwarded"``, or ``"diverged"``.

        Fast-forward ONLY, deliberately. A diverged branch is left exactly as
        it is: the worker's unpushed commits are the expensive side, and since
        ``push()`` force-pushes, any automatic resolution here would silently
        destroy whichever side it didn't pick. Reconciling is the agent's job,
        in-session, prompted by the note the caller mails it.

        Reads only the remote-tracking ref, never the network — the caller
        fetches first (with the forge's scoped token) so this works in a
        worktree that has no credential of its own.
        """
        remote = f"origin/{branch}"
        if not self._ref_exists(worktree, f"refs/remotes/{remote}"):
            return "no-remote"
        # Remote tip already contained in HEAD covers both "equal" and "the
        # worker is ahead of what was last pushed" — nothing to pull either way.
        if self._is_ancestor(worktree, remote, "HEAD"):
            return "up-to-date"
        if self._is_ancestor(worktree, "HEAD", remote):
            _git(["merge", "--ff-only", remote], cwd=worktree)
            return "fast-forwarded"
        return "diverged"

    def push(
        self, worktree: Path, branch: str, url: str | None = None, auth_header: str | None = None
    ) -> None:
        # Plain --force. The brief wanted --force-with-lease, but that needs
        # a remote-tracking ref to lease against, which does NOT exist when
        # pushing to an explicit URL (the app-token remote) — so after an
        # agent rewrote its branch history the force-push silently failed to
        # update the PR (observed live: PR frozen at the first commit).
        # A plain force is correct here: agent/* branches are the bot's,
        # pushed only by this single daemon, so there is no concurrent
        # pusher to protect against; humans review via comments, not by
        # pushing to the agent's branch.
        # Pushed to the forge's HTTPS URL with its scoped token — never the
        # operator's SSH key (which carries their full push rights).
        _git(
            [*_auth_args(auth_header), "push", "--force", url or "origin", f"{branch}:{branch}"],
            cwd=worktree,
        )

    def remove_worktree(self, repo: Path, path: Path, branch: str) -> None:
        # Best-effort: teardown must complete even if the worktree is
        # already partly gone (a prior stop rm'd the dir; git then reports
        # "not a working tree"). Fall back to rm + prune rather than raise.
        path = Path(path)
        if path.exists():
            try:
                _git(["worktree", "remove", "--force", str(path)], cwd=repo)
            except GitError as e:
                log.warning("worktree remove %s: %s; removing the dir and pruning", path, e)
                shutil.rmtree(path, ignore_errors=True)
        try:
            _git(["worktree", "prune"], cwd=repo)
        except GitError as e:
            log.warning("worktree prune in %s: %s", repo, e)

    def delete_remote_branch(
        self, repo: Path, branch: str, url: str | None = None, auth_header: str | None = None
    ) -> None:
        """Called only after a merge: drop the remote branch, and the local
        one with it (best-effort each — the remote may already be gone if
        GitHub auto-deleted it)."""
        try:
            _git(
                [*_auth_args(auth_header), "push", url or "origin", "--delete", branch],
                cwd=repo,
            )
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
