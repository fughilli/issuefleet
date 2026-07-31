"""Integration tests for the tools/bazel Bazelisk wrapper, against real git
(temp repos + worktrees) and a stub BAZEL_REAL that records its argv — the
same real-tool style as test_gitops_runner."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def _find_wrapper() -> Path:
    """tools/bazel, whether we run from the source tree or a Bazel runfiles
    tree (where it is present as this test's data dep)."""
    for base in Path(__file__).resolve().parents:
        cand = base / "tools" / "bazel"
        if cand.is_file():
            return cand
    raise unittest.SkipTest("tools/bazel wrapper not found in runfiles")


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class BazelWrapperTest(unittest.TestCase):
    def setUp(self):
        self.wrapper = _find_wrapper()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        run(["git", "init", "-b", "main", str(self.repo)])
        run(["git", "config", "user.email", "t@t"], cwd=self.repo)
        run(["git", "config", "user.name", "t"], cwd=self.repo)
        (self.repo / "README.md").write_text("hi\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "init"], cwd=self.repo)
        # A stub "bazel" that just records the argv it was exec'd with.
        self.argv_out = self.root / "argv.txt"
        self.stub = self.root / "bazel-stub"
        self.stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_OUT"\n')
        self.stub.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, cwd, extra_env=None):
        env = {**os.environ, "BAZEL_REAL": str(self.stub), "ARGV_OUT": str(self.argv_out)}
        if extra_env:
            env.update(extra_env)
        subprocess.run(
            ["bash", str(self.wrapper), "build", "//foo"],
            cwd=cwd, env=env, check=True, capture_output=True, text=True,
        )
        return self.argv_out.read_text().splitlines()

    def _common_dir(self, cwd) -> Path:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out)

    def test_injects_shared_cache_from_git_common_dir(self):
        argv = self._run(self.repo)
        root = self._common_dir(self.repo) / "bazel-cache"
        rc = root / "shared.bazelrc"
        self.assertEqual(argv[0], f"--bazelrc={rc}")
        self.assertEqual(argv[1:], ["build", "//foo"])
        body = rc.read_text()
        self.assertIn(f"--disk_cache={root}/disk", body)
        self.assertIn(f"--repository_cache={root}/repo", body)

    def test_worktrees_of_a_repo_share_one_root(self):
        # The whole point: a linked worktree resolves to the SAME cache root as
        # the main checkout, so its first build is warm.
        wt = self.root / "wt"
        run(["git", "worktree", "add", "-b", "side", str(wt), "main"], cwd=self.repo)
        main_argv = self._run(self.repo)
        wt_argv = self._run(wt)
        self.assertEqual(main_argv[0], wt_argv[0])
        self.assertIn(str(self.repo / ".git" / "bazel-cache"), wt_argv[0])

    def test_env_override_wins(self):
        shared = self.root / "mounted-cache"
        argv = self._run(self.repo, extra_env={"BAZEL_SHARED_CACHE_DIR": str(shared)})
        rc = shared / "shared.bazelrc"
        self.assertEqual(argv[0], f"--bazelrc={rc}")
        self.assertIn(f"--disk_cache={shared}/disk", rc.read_text())

    def test_falls_back_outside_a_git_repo(self):
        nongit = self.root / "plain"
        nongit.mkdir()
        argv = self._run(nongit)
        # No --bazelrc injected: Bazel is exec'd with the original args only,
        # so the workspace .bazelrc's per-tree caches apply.
        self.assertEqual(argv, ["build", "//foo"])


if __name__ == "__main__":
    unittest.main()
