"""Integration tests against real git (temp repos + a local bare 'origin')
and real tmux — the two host tools this container does have."""

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from issuefleet import config
from issuefleet.config import Config, ProjectConfig, ClaimRule
from issuefleet.gitops import GitError, Gitops
from issuefleet.model import WorkerRecord
from issuefleet.runner import TmuxRunner


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class GitopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.worktrees = root / "worktrees"
        run(["git", "init", "--bare", "-b", "main", str(self.origin)])
        run(["git", "clone", str(self.origin), str(self.repo)])
        run(["git", "config", "user.email", "t@t"], cwd=self.repo)
        run(["git", "config", "user.name", "t"], cwd=self.repo)
        (self.repo / "README.md").write_text("hello\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "init"], cwd=self.repo)
        run(["git", "push", "origin", "main"], cwd=self.repo)
        self.git = Gitops()
        self.wt = self.worktrees / "FUG-1"

    def tearDown(self):
        self.tmp.cleanup()

    def commit_in_worktree(self, msg="work"):
        (self.wt / "change.txt").write_text(msg)
        run(["git", "add", "."], cwd=self.wt)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg], cwd=self.wt)

    def test_create_worktree_and_adopt(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.assertTrue((self.wt / "README.md").is_file())
        # Idempotent adoption.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        # Wrong branch is an error, not a clobber.
        with self.assertRaisesRegex(GitError, "expected"):
            self.git.create_worktree(self.repo, "agent/other", "main", self.wt)

    def test_new_worktree_starts_from_origin_base_not_stale_local(self):
        # Advance origin/main past the daemon clone's local main via a second
        # clone, then fetch: the clone's local `main` stays pinned (it's
        # checked out), only origin/main moves.
        other = Path(self.tmp.name) / "other"
        run(["git", "clone", str(self.origin), str(other)])
        (other / "NEW.txt").write_text("fresh\n")
        run(["git", "add", "."], cwd=other)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "advance"],
            cwd=other)
        run(["git", "push", "origin", "main"], cwd=other)
        self.git.fetch(self.repo, url=str(self.origin))

        def sha(ref, cwd):
            return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                                  capture_output=True, text=True).stdout.strip()

        self.assertNotEqual(sha("main", self.repo), sha("origin/main", self.repo))
        # A fresh worker branch is cut from origin/main (the fetched base),
        # not the stale local main.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.assertTrue((self.wt / "NEW.txt").is_file())
        self.assertEqual(sha("HEAD", self.wt), sha("origin/main", self.repo))

    def test_adopt_branch_that_exists_only_on_origin(self):
        # A branch pushed from an interactive session elsewhere: it exists as
        # origin/<branch> in the daemon clone but has no local ref. Adoption
        # must check it out (tracking origin), not cut a fresh one from base —
        # which would silently discard that work.
        other = Path(self.tmp.name) / "other"
        run(["git", "clone", str(self.origin), str(other)])
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "checkout", "-b", "my-feature"], cwd=other)
        (other / "FEATURE.txt").write_text("external work\n")
        run(["git", "add", "."], cwd=other)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "feature"],
            cwd=other)
        run(["git", "push", "origin", "my-feature"], cwd=other)
        self.git.fetch(self.repo, url=str(self.origin))

        self.git.create_worktree(self.repo, "my-feature", "main", self.wt)
        # The operator's commit is present — the branch was adopted, not re-cut.
        self.assertTrue((self.wt / "FEATURE.txt").is_file())
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.wt,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "my-feature")

    def test_worktree_base_falls_back_to_local_ref_without_origin(self):
        # A local-only base (no origin/<ref>) still works — bootstrap/offline.
        run(["git", "branch", "local-only", "main"], cwd=self.repo)
        self.git.create_worktree(self.repo, "agent/fug-2-y", "local-only", self.wt)
        self.assertTrue((self.wt / "README.md").is_file())

    def test_adopt_to_remote_resets_onto_a_rebased_origin(self):
        # Release/adopt robustness (FUG-113): the operator holds a branch,
        # rebases it onto a newer mainline, force-pushes, and adopts it back.
        # origin/<branch> now has rewritten history no fast-forward can follow;
        # adopt_to_remote must reset the worktree onto the operator's branch,
        # not leave it stranded on the pre-release tip.
        def sha(ref, cwd):
            return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                                  capture_output=True, text=True).stdout.strip()

        # The branch as origin last saw it (the "released" state).
        self.git.create_worktree(self.repo, "agent/f", "main", self.wt)
        self.commit_in_worktree("branch work")
        run(["git", "push", "origin", "agent/f"], cwd=self.wt)
        old_tip = sha("HEAD", self.wt)

        # The operator, in their own clone: advance main, rebase the branch onto
        # it, force-push.
        other = Path(self.tmp.name) / "other"
        run(["git", "clone", str(self.origin), str(other)])
        run(["git", "config", "user.email", "o@o"], cwd=other)
        run(["git", "config", "user.name", "o"], cwd=other)
        (other / "MAIN.txt").write_text("newer mainline\n")
        run(["git", "add", "."], cwd=other)
        run(["git", "commit", "-m", "advance main"], cwd=other)
        run(["git", "push", "origin", "main"], cwd=other)
        run(["git", "checkout", "-B", "agent/f", "origin/agent/f"], cwd=other)
        run(["git", "rebase", "origin/main"], cwd=other)
        run(["git", "push", "--force", "origin", "agent/f"], cwd=other)

        # Daemon: fetch, then adopt the branch it still holds locally at old_tip.
        self.git.fetch(self.repo, url=str(self.origin))
        self.assertNotEqual(old_tip, sha("origin/agent/f", self.repo))  # rewritten
        status = self.git.adopt_to_remote(self.wt, "agent/f")
        self.assertEqual(status, "reset-to-remote")
        # The worktree is now on the operator's rebased branch, carrying the new
        # mainline; the pre-release tip survives in the reflog.
        self.assertEqual(sha("HEAD", self.wt), sha("origin/agent/f", self.repo))
        self.assertTrue((self.wt / "MAIN.txt").is_file())
        self.assertEqual(sha("agent/f@{1}", self.wt), old_tip)

    def test_adopt_to_remote_fast_forwards_when_operator_only_appended(self):
        def sha(ref, cwd):
            return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                                  capture_output=True, text=True).stdout.strip()

        self.git.create_worktree(self.repo, "agent/g", "main", self.wt)
        run(["git", "push", "origin", "agent/g"], cwd=self.wt)
        other = Path(self.tmp.name) / "other"
        run(["git", "clone", str(self.origin), str(other)])
        run(["git", "config", "user.email", "o@o"], cwd=other)
        run(["git", "config", "user.name", "o"], cwd=other)
        run(["git", "checkout", "-B", "agent/g", "origin/agent/g"], cwd=other)
        (other / "MORE.txt").write_text("appended\n")
        run(["git", "add", "."], cwd=other)
        run(["git", "commit", "-m", "more"], cwd=other)
        run(["git", "push", "origin", "agent/g"], cwd=other)

        self.git.fetch(self.repo, url=str(self.origin))
        self.assertEqual(self.git.adopt_to_remote(self.wt, "agent/g"), "fast-forwarded")
        self.assertEqual(sha("HEAD", self.wt), sha("origin/agent/g", self.repo))

    def test_create_worktree_recovers_from_deleted_dir(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        shutil.rmtree(self.wt)
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.assertTrue((self.wt / "README.md").is_file())

    def test_repair_worktree_relinks_a_stale_pointer(self):
        # FUG-116: after a crash the worktree's .git pointer can go stale. A
        # bogus pointer breaks every git command; repair re-links it from the
        # admin gitdir and git works again.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        (self.wt / ".git").write_text("gitdir: /nonexistent/bogus\n")
        broken = subprocess.run(
            ["git", "status"], cwd=self.wt, capture_output=True, text=True
        )
        self.assertNotEqual(broken.returncode, 0)
        self.git.repair_worktree(self.repo, self.wt)
        fixed = subprocess.run(
            ["git", "status"], cwd=self.wt, capture_output=True, text=True
        )
        self.assertEqual(fixed.returncode, 0, fixed.stderr)

    def test_repair_worktree_is_a_noop_on_a_healthy_worktree(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        before = (self.wt / ".git").read_text()
        self.git.repair_worktree(self.repo, self.wt)  # must not raise or churn
        self.assertEqual((self.wt / ".git").read_text(), before)
        ok = subprocess.run(["git", "status"], cwd=self.wt, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0)

    def test_exclude_is_per_worktree_not_repo(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.git.add_worktree_exclude(self.repo, self.wt, ".agent/")
        self.git.add_worktree_exclude(self.repo, self.wt, ".agent/")  # no dup
        (self.wt / ".agent").mkdir()
        (self.wt / ".agent" / "junk.txt").write_text("x")
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.wt, capture_output=True, text=True
        ).stdout
        self.assertNotIn(".agent", out)  # invisible to the repo
        self.assertFalse((self.repo / ".gitignore").exists())  # repo untouched
        # Git only reads $GIT_COMMON_DIR/info/exclude (the per-worktree one
        # the brief suggested is ignored — verified on git 2.43), so that is
        # where the pattern must land, exactly once.
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertEqual(exclude.read_text().count(".agent/"), 1)

    def test_nested_worktree_exclude_resolves(self):
        # The operator's checkout may itself be a linked worktree (§5.1).
        linked_main = Path(self.tmp.name) / "linked_main"
        run(["git", "worktree", "add", "-b", "side", str(linked_main), "main"], cwd=self.repo)
        nested = Path(self.tmp.name) / "nested"
        self.git.create_worktree(linked_main, "agent/fug-9-y", "side", nested)
        self.git.add_worktree_exclude(linked_main, nested, ".agent/")
        (nested / ".agent").mkdir()
        (nested / ".agent" / "x").write_text("x")
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=nested, capture_output=True, text=True
        ).stdout
        self.assertNotIn(".agent", out)

    def test_has_commits_ahead(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.assertFalse(self.git.has_commits_ahead(self.wt, "main"))
        self.commit_in_worktree()
        self.assertTrue(self.git.has_commits_ahead(self.wt, "main"))

    def test_has_commits_ahead_resolves_non_origin_remote(self):
        # An adopted operator clone may not name its remote `origin`. Reshape
        # the clone so the base resolves ONLY via a differently-named remote:
        # detach the primary so local `main` can be dropped, delete it, then
        # rename origin -> upstream. Neither `origin/main` nor a local `main`
        # exists now — only `upstream/main` does.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        run(["git", "checkout", "--detach"], cwd=self.repo)
        run(["git", "branch", "-D", "main"], cwd=self.repo)
        run(["git", "remote", "rename", "origin", "upstream"], cwd=self.repo)
        self.assertFalse(self.git.has_commits_ahead(self.wt, "main"))
        self.commit_in_worktree()
        self.assertTrue(self.git.has_commits_ahead(self.wt, "main"))

    def test_has_commits_ahead_unresolvable_base_reports_candidates(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        with self.assertRaisesRegex(GitError, "cannot resolve base ref 'nope'"):
            self.git.has_commits_ahead(self.wt, "nope")

    def head_of(self, cwd):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True
        ).stdout.strip()

    def advance_origin(self, branch, msg="remote work"):
        """Push a commit to `branch` on origin from an unrelated clone — what
        an operator pushing to an agent's branch looks like from the daemon."""
        other = Path(self.tmp.name) / f"other-{len(msg)}-{branch.replace('/', '_')}"
        run(["git", "clone", "-b", branch, str(self.origin), str(other)])
        (other / "remote_work.txt").write_text(msg)
        run(["git", "add", "."], cwd=other)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg], cwd=other)
        run(["git", "push", "origin", branch], cwd=other)

    def test_sync_to_remote_fast_forwards_a_behind_branch(self):
        # The live regression: someone pushes to the agent's branch while the
        # worker is stopped. Resuming stale and then push --force erases it.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.git.push(self.wt, "agent/fug-1-x")
        self.advance_origin("agent/fug-1-x")
        self.git.fetch(self.repo)
        self.assertEqual(self.git.sync_to_remote(self.wt, "agent/fug-1-x"), "fast-forwarded")
        self.assertTrue((self.wt / "remote_work.txt").is_file())

    def test_sync_to_remote_keeps_unpushed_worker_commits(self):
        # Worker ahead of origin: nothing to pull, and nothing to reset — its
        # unpushed commits are the unrecoverable side.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.git.push(self.wt, "agent/fug-1-x")
        self.git.fetch(self.repo)
        self.commit_in_worktree("unpushed")
        head = self.head_of(self.wt)
        self.assertEqual(self.git.sync_to_remote(self.wt, "agent/fug-1-x"), "up-to-date")
        self.assertEqual(self.head_of(self.wt), head)

    def test_sync_to_remote_leaves_a_diverged_branch_untouched(self):
        # Both sides advanced. Fast-forward is impossible and any automatic
        # resolution would destroy one side, so the branch must not move.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.git.push(self.wt, "agent/fug-1-x")
        self.advance_origin("agent/fug-1-x")
        self.git.fetch(self.repo)
        self.commit_in_worktree("local divergence")
        head = self.head_of(self.wt)
        self.assertEqual(self.git.sync_to_remote(self.wt, "agent/fug-1-x"), "diverged")
        self.assertEqual(self.head_of(self.wt), head)

    def test_sync_to_remote_without_a_remote_branch(self):
        # First run: nothing pushed yet, so there is no origin/<branch> to
        # compare against and the fresh worktree is already correct.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.assertEqual(self.git.sync_to_remote(self.wt, "agent/fug-1-x"), "no-remote")

    def test_push_and_delete_remote_branch(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.git.push(self.wt, "agent/fug-1-x")
        refs = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.origin)], capture_output=True, text=True
        ).stdout
        self.assertIn("agent/fug-1-x", refs)
        # Rebase + force-with-lease re-push (the re-submission path).
        run(["git", "commit", "--amend", "-m", "amended"], cwd=self.wt)
        self.git.push(self.wt, "agent/fug-1-x")
        self.git.remove_worktree(self.repo, self.wt, "agent/fug-1-x")
        self.assertFalse(self.wt.exists())
        self.git.delete_remote_branch(self.repo, "agent/fug-1-x")
        refs = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.origin)], capture_output=True, text=True
        ).stdout
        self.assertNotIn("agent/fug-1-x", refs)

    def test_remove_worktree_survives_corrupt_worktree(self):
        # A dir that exists but isn't a valid worktree (a prior stop rm'd it
        # and something recreated the path) must not raise — teardown has to
        # complete.
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        # Corrupt it: remove the .git pointer so git no longer recognizes it.
        (self.wt / ".git").unlink()
        self.git.remove_worktree(self.repo, self.wt, "agent/fug-1-x")  # no raise
        self.assertFalse(self.wt.exists())

    def test_push_to_explicit_url_ignores_origin(self):
        # The daemon pushes to the forge's URL with a scoped token, not to
        # whatever `origin` points at (which may be an SSH remote using the
        # operator's key). Simulated with a second bare repo as the "url".
        other = Path(self.tmp.name) / "other.git"
        run(["git", "init", "--bare", "-b", "main", str(other)])
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.commit_in_worktree()
        self.git.push(self.wt, "agent/fug-1-x", url=str(other), auth_header="basic zzz")
        in_other = subprocess.run(["git", "ls-remote", "--heads", str(other)],
                                  capture_output=True, text=True).stdout
        in_origin = subprocess.run(["git", "ls-remote", "--heads", str(self.origin)],
                                   capture_output=True, text=True).stdout
        self.assertIn("agent/fug-1-x", in_other)
        self.assertNotIn("agent/fug-1-x", in_origin)

        # History rewrite then re-push must update the URL remote — the live
        # bug was a force that silently no-op'd against a URL, freezing the
        # PR at the first commit. Reset to a divergent commit and re-push.
        first_sha = subprocess.run(["git", "ls-remote", str(other), "agent/fug-1-x"],
                                   capture_output=True, text=True).stdout.split()[0]
        run(["git", "reset", "--hard", "HEAD~1"], cwd=self.wt)
        (self.wt / "rewritten.txt").write_text("clean single commit")
        run(["git", "add", "."], cwd=self.wt)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "rewrite"],
            cwd=self.wt)
        self.git.push(self.wt, "agent/fug-1-x", url=str(other), auth_header="basic zzz")
        new_sha = subprocess.run(["git", "ls-remote", str(other), "agent/fug-1-x"],
                                 capture_output=True, text=True).stdout.split()[0]
        self.assertNotEqual(new_sha, first_sha)  # the rewrite actually landed

        self.git.delete_remote_branch(self.repo, "agent/fug-1-x", url=str(other))
        in_other = subprocess.run(["git", "ls-remote", "--heads", str(other)],
                                  capture_output=True, text=True).stdout
        self.assertNotIn("agent/fug-1-x", in_other)

    def test_sibling_worktree_nested_in_a_worktree_shares_the_sibling_common_dir(self):
        # FUG-115: a sibling project's change is staged in a LINKED WORKTREE of
        # the sibling repo, opened at siblings/<name> inside the worker's own
        # worktree. It shares the sibling repo's git-common-dir (so its Bazel
        # cache is warm), and the worker container reaches it because the runner
        # same-path-mounts that .git. Here `self.repo` plays the sibling.
        worker_wt = self.worktrees / "FUG-1"
        self.git.create_worktree(self.repo, "agent/fug-1-main", "main", worker_wt)
        sib = worker_wt / "siblings" / "embedded"  # nested under the worker worktree
        self.git.create_worktree(self.repo, "agent/fug-1-embedded", "main", sib)
        self.assertTrue((sib / "README.md").is_file())
        # Its common-dir is the sibling repo's .git — exactly the dir the runner
        # mounts same-path so this resolves in-container.
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=sib, capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(common), (self.repo / ".git"))
        # Commit + push-to-explicit-URL works from it, like the primary worktree.
        (sib / "patch.txt").write_text("upstream change")
        run(["git", "add", "."], cwd=sib)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "up"], cwd=sib)
        self.assertTrue(self.git.has_commits_ahead(sib, "main"))
        other = Path(self.tmp.name) / "embedded.git"
        run(["git", "init", "--bare", "-b", "main", str(other)])
        self.git.push(sib, "agent/fug-1-embedded", url=str(other), auth_header="basic zzz")
        self.assertIn(
            "agent/fug-1-embedded",
            subprocess.run(["git", "ls-remote", "--heads", str(other)],
                           capture_output=True, text=True).stdout,
        )
        # Teardown deregisters it from the sibling repo (no stale worktree).
        self.git.remove_worktree(self.repo, sib, "agent/fug-1-embedded")
        listing = subprocess.run(["git", "worktree", "list"], cwd=self.repo,
                                 capture_output=True, text=True).stdout
        self.assertNotIn("embedded", listing)

    def test_remote_url(self):
        self.assertEqual(self.git.remote_url(self.repo), str(self.origin))

    def test_clone_bootstraps_missing_checkout(self):
        dest = Path(self.tmp.name) / "deep" / "nested" / "clone"  # parents created
        self.git.clone(str(self.origin), dest)
        self.assertTrue(self.git.is_repo(dest))
        self.assertTrue((dest / "README.md").is_file())
        self.assertEqual(self.git.remote_url(dest), str(self.origin))

    def _project(self, repo, git_url=None):
        from issuefleet.config import ClaimRule, ProjectConfig

        return ProjectConfig(
            name="p", linear_project="P", repo=Path(repo),
            claim=ClaimRule("agent", ""), git_url=git_url,
        )

    def test_ensure_checkout_clones_into_repo(self):
        from issuefleet.gitops import ensure_checkout

        dest = Path(self.tmp.name) / "root" / "repos" / "q"  # parents created
        action = ensure_checkout(self.git, self._project(dest, git_url=str(self.origin)))
        self.assertIn("cloned", action)
        self.assertTrue(self.git.is_repo(dest))
        self.assertFalse(dest.is_symlink())  # a real clone the daemon owns
        # Idempotent: an existing repo is a no-op, not a re-clone.
        self.assertIsNone(
            ensure_checkout(self.git, self._project(dest, git_url=str(self.origin)))
        )

    def test_ensure_checkout_dead_ends_raise(self):
        from issuefleet.gitops import ensure_checkout

        with self.assertRaisesRegex(GitError, "no git_url"):
            ensure_checkout(self.git, self._project(Path(self.tmp.name) / "missing"))
        broken = Path(self.tmp.name) / "broken"
        broken.symlink_to(Path(self.tmp.name) / "gone")
        with self.assertRaisesRegex(GitError, "missing target"):
            ensure_checkout(self.git, self._project(broken, git_url=str(self.origin)))


class ScriptWrapperTest(unittest.TestCase):
    """Pure-string tests for the script(1) dialect choice — deliberately NOT in
    TmuxRunnerTest, which is skipped wherever tmux is absent (including the
    bazel sandbox). The macOS `illegal option -- c` bug shipped precisely
    because the only coverage of this code path was inside that skipped class.
    """

    def test_script_wrapper_matches_the_platform_dialect(self):
        from issuefleet import runner as runner_mod

        cmd = ["launcher", "-w", "/some path/wt", "run"]
        log_path = Path("/logs/w.log")
        real = sys.platform
        try:
            runner_mod.sys.platform = "darwin"
            bsd = runner_mod._script_wrapper(cmd, log_path)
            runner_mod.sys.platform = "linux"
            gnu = runner_mod._script_wrapper(cmd, log_path)
        finally:
            runner_mod.sys.platform = real
        # BSD: file first, command as trailing argv, never -c.
        self.assertNotIn(" -c ", bsd)
        self.assertIn("script -q -e -t 0 /logs/w.log launcher", bsd)
        # -t 0 is load-bearing: BSD script's default flush interval is 30s, so
        # without it a live worker's pane log reads empty while it works.
        self.assertIn("-t 0", bsd)
        # util-linux: -c with the whole command as ONE quoted argument.
        self.assertIn("-c ", gnu)
        # ...and -f, for the same reason BSD needs -t 0: util-linux buffers
        # until the child exits, so without it a live worker's pane log is
        # empty the whole time it is working. CI caught this one.
        self.assertIn(" -f ", gnu)
        self.assertTrue(gnu.rstrip().endswith("/logs/w.log"))
        # Either way the space in the worktree path survives quoting.
        for form in (bsd, gnu):
            self.assertIn("some path", form)


class SiblingMountTest(unittest.TestCase):
    """FUG-115: the worker command same-path-mounts sibling git dirs. Pure
    argv construction — no tmux — so it runs in the sandbox too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("splanc", "embedded"):  # need a .git dir for the mount
            (self.root / name / ".git").mkdir(parents=True)
        self.runner = TmuxRunner(self.root / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, mount=True):
        return Config(
            projects=[
                ProjectConfig(name="splanc", linear_project="S",
                              repo=self.root / "splanc", claim=ClaimRule("label", "agent")),
                ProjectConfig(name="embedded", linear_project="E",
                              repo=self.root / "embedded", claim=ClaimRule("label", "agent")),
            ],
            mount_sibling_git=mount,
        )

    def _rec(self):
        return WorkerRecord(
            issue_id="i", issue_key="FUG-1", issue_title="t", issue_url="u",
            project="splanc", repo=str(self.root / "splanc"), branch="b",
            worktree=str(self.root / "wt"), base_ref="main",
            session_uuid="s", tmux_session="ts",
        )

    def test_mounts_sibling_git_dirs_same_path_before_the_command(self):
        cmd = self.runner.command(self._rec(), self._cfg())
        self.assertIn("--mount", cmd)
        i = cmd.index("--mount")
        self.assertEqual(cmd[i + 1], str(self.root / "embedded" / ".git"))  # the sibling
        # Never the worker's own repo (the launcher already mounts that).
        self.assertNotIn(str(self.root / "splanc" / ".git"), cmd)
        # Flags precede the in-container command.
        self.assertLess(i, cmd.index("/workspace/.agent/bin/turnloop"))

    def test_disabled_emits_no_mounts(self):
        self.assertNotIn("--mount", self.runner.command(self._rec(), self._cfg(mount=False)))

    def test_single_project_emits_no_mounts(self):
        cfg = self._cfg()
        cfg.projects = [cfg.projects[0]]  # only the worker's own project
        self.assertNotIn("--mount", self.runner.command(self._rec(), cfg))

    def test_absent_sibling_clone_is_skipped(self):
        import shutil as _sh

        _sh.rmtree(self.root / "embedded" / ".git")  # sibling not cloned yet
        self.assertNotIn("--mount", self.runner.command(self._rec(), self._cfg()))


@unittest.skipIf(shutil.which("tmux") is None, "tmux not available")
class TmuxRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # A stub "claude-container" that just sleeps, so start/alive/stop are
        # exercised through real tmux without docker.
        self.stub = root / "cc-stub"
        self.stub.write_text("#!/bin/sh\nsleep 300\n")
        self.stub.chmod(0o755)
        self.cfg = Config(
            projects=[
                ProjectConfig(
                    name="p", linear_project="P", repo=root, claim=ClaimRule("label", "agent")
                )
            ],
            claude_container=str(self.stub),
        )
        self.rec = WorkerRecord(
            issue_id="i1", issue_key="FUG-1", issue_title="t", issue_url="u",
            project="p", repo=str(root), branch="b", worktree=str(root),
            base_ref="main", session_uuid="s",
            tmux_session=f"issuefleet-test-{id(self) % 100000}",
        )
        self.runner = TmuxRunner(log_dir=root / "logs")

    def tearDown(self):
        self.runner.stop(self.rec)
        self.tmp.cleanup()

    def test_the_launcher_output_actually_reaches_the_pane_log(self):
        """The regression that mattered: on macOS the util-linux `script -c`
        form fails with `illegal option -- c`, script exits instantly, and the
        log is never created — so the worker dies with no diagnostic at all.
        Assert against real tmux + real script(1) on whatever platform we're on.
        """
        marker = "LAUNCHER_RAN_OK"
        self.stub.write_text(f"#!/bin/sh\necho {marker}\nsleep 300\n")
        self.stub.chmod(0o755)
        self.runner.start(self.rec, self.cfg)
        log_path = self.runner.log_path(self.rec)
        for _ in range(40):  # script flushes through a pty; give it a moment
            if log_path.exists() and marker in log_path.read_text(errors="replace"):
                break
            time.sleep(0.1)
        self.assertTrue(log_path.exists(), "script(1) never created the pane log")
        self.assertIn(marker, log_path.read_text(errors="replace"))
        self.assertTrue(self.runner.alive(self.rec))

    def test_command_shape(self):
        cmd = self.runner.command(self.rec, self.cfg)
        self.assertEqual(cmd[:3], [str(self.stub), "-w", self.rec.worktree])
        self.assertEqual(cmd[-2:], ["/workspace/.agent/bin/turnloop", "run"])
        # Launcher flags must precede the in-container command (the first
        # non-option argument starts the command).
        self.assertLess(cmd.index("--skills-ignore-new"), cmd.index("/workspace/.agent/bin/turnloop"))
        for word in cmd:  # launcher word-splits: no spaces allowed anywhere
            self.assertNotIn(" ", word)

    def test_launcher_args_configurable(self):
        self.cfg.launcher_args = []
        cmd = self.runner.command(self.rec, self.cfg)
        self.assertNotIn("--skills-ignore-new", cmd)

    def test_worker_env_reaches_the_launcher_without_touching_argv(self):
        key = Path(self.tmp.name) / "ts.key"
        key.write_text("tskey-auth-EXAMPLE-fixture-2\n")
        self.cfg.worker_env = {"TS_AUTHKEY": config.EnvSource(file=key)}

        env_file = self.runner._write_env_file(self.rec, self.cfg)
        self.assertIsNotNone(env_file)
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        # A shell sourcing it under `set -a` must export exactly the value.
        out = subprocess.run(
            ["sh", "-c", f"set -a; . {env_file}; set +a; printenv TS_AUTHKEY"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.stdout.strip(), "tskey-auth-EXAMPLE-fixture-2")
        # The launcher command itself must stay clean: it is logged verbatim
        # when a worker dies, and visible in `ps` for the container's lifetime.
        self.assertNotIn("tskey-auth-EXAMPLE-fixture-2", " ".join(self.runner.command(self.rec, self.cfg)))

    def test_worker_env_file_is_removed_once_sourced(self):
        key = Path(self.tmp.name) / "ts.key"
        key.write_text("tskey-auth-EXAMPLE-fixture-2\n")
        self.cfg.worker_env = {"TS_AUTHKEY": config.EnvSource(file=key)}
        self.runner.start(self.rec, self.cfg)
        time.sleep(0.3)
        self.assertFalse(
            self.runner.env_path(self.rec).exists(),
            "the session shell should delete the env file immediately after sourcing it",
        )
        self.runner.stop(self.rec)

    def test_missing_worker_env_source_is_not_fatal(self):
        self.cfg.worker_env = {
            "TS_AUTHKEY": config.EnvSource(file=Path(self.tmp.name) / "absent.key")
        }
        # No value to write -> no env file, and start() proceeds regardless
        # (deliberately not asserting alive(): BSD script(1) makes that a
        # container-only assertion, as test_start_alive_stop_idempotent shows).
        self.assertIsNone(self.runner._write_env_file(self.rec, self.cfg))
        self.runner.start(self.rec, self.cfg)
        self.assertFalse(self.runner.env_path(self.rec).exists())
        self.runner.stop(self.rec)

    def test_start_alive_stop_idempotent(self):
        self.assertFalse(self.runner.alive(self.rec))
        self.runner.start(self.rec, self.cfg)
        time.sleep(0.2)
        self.assertTrue(self.runner.alive(self.rec))
        self.runner.start(self.rec, self.cfg)  # adopt, not error
        self.runner.stop(self.rec)
        time.sleep(0.2)
        self.assertFalse(self.runner.alive(self.rec))
        self.runner.stop(self.rec)  # double-stop is fine


if __name__ == "__main__":
    unittest.main()
