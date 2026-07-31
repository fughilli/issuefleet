"""Integration tests against real git (temp repos + a local bare 'origin')
and real tmux — the two host tools this container does have."""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

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

    def test_create_worktree_recovers_from_deleted_dir(self):
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        shutil.rmtree(self.wt)
        self.git.create_worktree(self.repo, "agent/fug-1-x", "main", self.wt)
        self.assertTrue((self.wt / "README.md").is_file())

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
        self.git.delete_remote_branch(self.repo, "agent/fug-1-x", url=str(other))
        in_other = subprocess.run(["git", "ls-remote", "--heads", str(other)],
                                  capture_output=True, text=True).stdout
        self.assertNotIn("agent/fug-1-x", in_other)

    def test_remote_url(self):
        self.assertEqual(self.git.remote_url(self.repo), str(self.origin))

    def test_clone_bootstraps_missing_checkout(self):
        dest = Path(self.tmp.name) / "deep" / "nested" / "clone"  # parents created
        self.git.clone(str(self.origin), dest)
        self.assertTrue(self.git.is_repo(dest))
        self.assertTrue((dest / "README.md").is_file())
        self.assertEqual(self.git.remote_url(dest), str(self.origin))

    def _project(self, repo, git_url=None, local_checkout=None):
        from issuefleet.config import ClaimRule, ProjectConfig

        return ProjectConfig(
            name="p", linear_project="P", repo=Path(repo),
            claim=ClaimRule("agent", ""), git_url=git_url,
            local_checkout=Path(local_checkout) if local_checkout else None,
        )

    def test_ensure_checkout_symlinks_local_checkout(self):
        from issuefleet.gitops import ensure_checkout

        link = Path(self.tmp.name) / "root" / "repos" / "p"
        action = ensure_checkout(self.git, self._project(link, local_checkout=self.repo))
        self.assertIn("symlinked", action)
        self.assertTrue(link.is_symlink())
        self.assertTrue(self.git.is_repo(link))
        # Idempotent: an existing (symlinked) repo is a no-op.
        self.assertIsNone(ensure_checkout(self.git, self._project(link, local_checkout=self.repo)))

    def test_ensure_checkout_falls_back_to_clone(self):
        from issuefleet.gitops import ensure_checkout

        dest = Path(self.tmp.name) / "root" / "repos" / "q"
        action = ensure_checkout(
            self.git,
            self._project(dest, git_url=str(self.origin),
                          local_checkout=Path(self.tmp.name) / "nope"),
        )
        self.assertIn("cloned", action)
        self.assertIn("not found", action)  # says why the checkout wasn't used
        self.assertTrue(self.git.is_repo(dest))
        self.assertFalse(dest.is_symlink())

    def test_ensure_checkout_dead_ends_raise(self):
        from issuefleet.gitops import ensure_checkout

        with self.assertRaisesRegex(GitError, "neither"):
            ensure_checkout(self.git, self._project(Path(self.tmp.name) / "missing"))
        broken = Path(self.tmp.name) / "broken"
        broken.symlink_to(Path(self.tmp.name) / "gone")
        with self.assertRaisesRegex(GitError, "missing target"):
            ensure_checkout(self.git, self._project(broken, git_url=str(self.origin)))


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
