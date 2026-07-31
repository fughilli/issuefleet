import tempfile
import unittest
from pathlib import Path

from issuefleet import config
from issuefleet.config import ConfigError
from issuefleet.model import Issue


def make_issue(**kw):
    base = dict(
        id="i1",
        key="FUG-1",
        title="t",
        description="",
        url="",
        priority=0,
        state_name="Todo",
        state_type="unstarted",
    )
    base.update(kw)
    return Issue(**base)


MINIMAL = {
    "projects": [
        {
            "name": "splanc",
            "linear_project": "Splanc",
            "repo": "~/Projects/splanc",
            "claim": {"strategy": "label", "value": "agent"},
        }
    ]
}


class ConfigTest(unittest.TestCase):
    def test_minimal_parses_with_defaults(self):
        cfg = config.parse(MINIMAL)
        self.assertEqual(cfg.poll_interval_s, 60)
        self.assertEqual(cfg.max_workers, 4)
        self.assertEqual(cfg.max_auto_turns, 40)
        p = cfg.project("splanc")
        self.assertEqual(p.base_ref, "main")
        self.assertEqual(p.claim.strategy, "label")
        self.assertNotIn("~", str(p.repo))  # expanded

    def test_load_from_toml_file(self):
        toml = (
            '[daemon]\npoll_interval_s = 30\n'
            '[[projects]]\nname = "x"\nlinear_project = "X"\nrepo = "/tmp/x"\n'
            'claim = { strategy = "state", value = "Ready for agent" }\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
        cfg = config.load(f.name)
        self.assertEqual(cfg.poll_interval_s, 30)
        self.assertEqual(cfg.projects[0].claim.strategy, "state")
        Path(f.name).unlink()

    def test_missing_projects_rejected(self):
        with self.assertRaisesRegex(ConfigError, "projects"):
            config.parse({})

    def test_missing_required_project_key(self):
        with self.assertRaisesRegex(ConfigError, "repo"):
            config.parse({"projects": [{"name": "a", "linear_project": "A"}]})

    def test_bad_claim_strategy(self):
        bad = {
            "projects": [
                {
                    "name": "a",
                    "linear_project": "A",
                    "repo": "/tmp/a",
                    "claim": {"strategy": "vibes", "value": "x"},
                }
            ]
        }
        with self.assertRaisesRegex(ConfigError, "strategy"):
            config.parse(bad)

    def test_secret_in_config_rejected(self):
        data = dict(MINIMAL)
        data["credentials"] = {"linear_api_key": "lin_api_123"}
        with self.assertRaisesRegex(ConfigError, "chmod-600"):
            config.parse(data)

    def test_duplicate_project_names_rejected(self):
        data = {"projects": [MINIMAL["projects"][0], dict(MINIMAL["projects"][0])]}
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            config.parse(data)

    def test_paths_expand_env_vars(self):
        # ${ISSUEFLEET_ROOT}/... in config paths makes one config.toml work
        # on a laptop and in the homelab container (which sets the var).
        import os

        os.environ["ISSUEFLEET_ROOT"] = "/data/fleet"
        try:
            data = {
                "daemon": {
                    "state_dir": "${ISSUEFLEET_ROOT}/state",
                    "worktree_root": "${ISSUEFLEET_ROOT}/worktrees",
                },
                "projects": [
                    {
                        "name": "x",
                        "linear_project": "X",
                        "repo": "${ISSUEFLEET_ROOT}/repos/x",
                        "claim": {"strategy": "agent"},
                    }
                ],
            }
            cfg = config.parse(data)
            self.assertEqual(str(cfg.state_dir), "/data/fleet/state")
            self.assertEqual(str(cfg.worktree_root), "/data/fleet/worktrees")
            self.assertEqual(str(cfg.projects[0].repo), "/data/fleet/repos/x")
        finally:
            del os.environ["ISSUEFLEET_ROOT"]

    def test_issuefleet_root_defaults_when_unset(self):
        # A shared config's ${ISSUEFLEET_ROOT} paths must work on a laptop
        # (no env var) without leaving a literal "${ISSUEFLEET_ROOT}" dir.
        import os
        from pathlib import Path

        saved = os.environ.pop("ISSUEFLEET_ROOT", None)
        try:
            data = {
                "daemon": {"worktree_root": "${ISSUEFLEET_ROOT}/worktrees"},
                "projects": MINIMAL["projects"],
            }
            cfg = config.parse(data)
            expected = Path("~/.issuefleet/worktrees").expanduser()
            self.assertEqual(cfg.worktree_root, expected)
        finally:
            if saved is not None:
                os.environ["ISSUEFLEET_ROOT"] = saved

    def test_issuefleet_projects_expands_and_defaults(self):
        # Same contract as ISSUEFLEET_ROOT, for checkouts the daemon doesn't
        # own: the compose stack exports it, a laptop falls back to ~/Projects.
        import os
        from pathlib import Path

        saved = os.environ.pop("ISSUEFLEET_PROJECTS", None)
        try:
            data = {"projects": [dict(MINIMAL["projects"][0],
                                      repo="${ISSUEFLEET_PROJECTS}/led_mapper")]}
            self.assertEqual(config.parse(data).projects[0].repo,
                             Path("~/Projects/led_mapper").expanduser())
            os.environ["ISSUEFLEET_PROJECTS"] = "/Users/x/code"
            self.assertEqual(str(config.parse(data).projects[0].repo),
                             "/Users/x/code/led_mapper")
        finally:
            os.environ.pop("ISSUEFLEET_PROJECTS", None)
            if saved is not None:
                os.environ["ISSUEFLEET_PROJECTS"] = saved

    def test_claude_config_var_defaults(self):
        import os
        from pathlib import Path

        saved = os.environ.pop("ISSUEFLEET_CLAUDE_CONFIG", None)
        try:
            data = {"agent": {"container_config_dir": "${ISSUEFLEET_CLAUDE_CONFIG}"},
                    "projects": MINIMAL["projects"]}
            cfg = config.parse(data)
            self.assertEqual(cfg.container_config_dir,
                             Path("~/.config/claude-container/config").expanduser())
            os.environ["ISSUEFLEET_CLAUDE_CONFIG"] = "/live/creds"
            self.assertEqual(str(config.parse(data).container_config_dir), "/live/creds")
        finally:
            os.environ.pop("ISSUEFLEET_CLAUDE_CONFIG", None)
            if saved is not None:
                os.environ["ISSUEFLEET_CLAUDE_CONFIG"] = saved

    def test_git_url_optional(self):
        self.assertIsNone(config.parse(MINIMAL).projects[0].git_url)
        data = {"projects": [dict(MINIMAL["projects"][0], git_url="git@github.com:a/b.git")]}
        self.assertEqual(config.parse(data).projects[0].git_url, "git@github.com:a/b.git")

    def test_local_checkout_is_rejected(self):
        # Removed feature: fail loudly rather than silently cloning instead.
        data = {"projects": [dict(MINIMAL["projects"][0], local_checkout="~/Projects/x")]}
        with self.assertRaisesRegex(config.ConfigError, "local_checkout is no longer supported"):
            config.parse(data)

    def test_launcher_args_default_and_override(self):
        self.assertEqual(config.parse(MINIMAL).launcher_args, ["--skills-ignore-new"])
        data = dict(MINIMAL)
        data["agent"] = {"launcher_args": []}
        self.assertEqual(config.parse(data).launcher_args, [])

    def test_claim_rules(self):
        label = config.ClaimRule("label", "agent")
        self.assertTrue(label.matches(make_issue(labels=["agent", "bug"])))
        self.assertFalse(label.matches(make_issue(labels=["bug"])))
        assignee = config.ClaimRule("assignee", "user-bot")
        self.assertTrue(assignee.matches(make_issue(assignee_id="user-bot")))
        self.assertFalse(assignee.matches(make_issue(assignee_id=None)))
        state = config.ClaimRule("state", "Ready for agent")
        self.assertTrue(state.matches(make_issue(state_name="Ready for agent")))
        self.assertFalse(state.matches(make_issue(state_name="Todo")))
        # "agent": nothing is ever poll-eligible; sessions claim directly.
        agent = config.ClaimRule("agent", "")
        self.assertFalse(agent.matches(make_issue(labels=["agent"], assignee_id="x")))

    def test_agent_strategy_needs_no_value(self):
        data = {
            "projects": [
                {
                    "name": "a",
                    "linear_project": "A",
                    "repo": "/tmp/a",
                    "claim": {"strategy": "agent"},
                }
            ]
        }
        cfg = config.parse(data)
        self.assertEqual(cfg.projects[0].claim.strategy, "agent")
        # ...but the others still require one.
        data["projects"][0]["claim"] = {"strategy": "label"}
        with self.assertRaisesRegex(config.ConfigError, "claim.value"):
            config.parse(data)


if __name__ == "__main__":
    unittest.main()
