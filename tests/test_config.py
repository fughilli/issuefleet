import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(cfg.max_auto_turns, 50)
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

    def test_linear_auth_client_credentials_accepted(self):
        data = dict(MINIMAL)
        data["credentials"] = {"linear_auth": "client_credentials"}
        self.assertEqual(config.parse(data).linear_auth, "client_credentials")

    def test_linear_auth_invalid_rejected(self):
        data = dict(MINIMAL)
        data["credentials"] = {"linear_auth": "bogus"}
        with self.assertRaisesRegex(ConfigError, "linear_auth must be"):
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

    def test_worker_env_sources(self):
        self.assertEqual(config.parse(MINIMAL).worker_env, {})
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "ts.key"
            key.write_text("tskey-auth-EXAMPLE-fixture-1\n")
            data = dict(MINIMAL)
            data["agent"] = {
                "env": {
                    "TS_AUTHKEY": {"file": str(key)},
                    "FROM_ENV": {"env": "IF_TEST_VAR"},
                    "HITL_SERVERS": {"value": "hitl-rig"},
                }
            }
            cfg = config.parse(data)
            # Trailing newline stripped — a shell would otherwise export it.
            self.assertEqual(cfg.worker_env["TS_AUTHKEY"].resolve(), "tskey-auth-EXAMPLE-fixture-1")
            self.assertEqual(cfg.worker_env["HITL_SERVERS"].resolve(), "hitl-rig")
            with mock.patch.dict(os.environ, {"IF_TEST_VAR": "from-env"}):
                self.assertEqual(cfg.worker_env["FROM_ENV"].resolve(), "from-env")
            # A source that isn't there resolves to None rather than raising:
            # the worker still launches, just without the variable.
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(cfg.worker_env["FROM_ENV"].resolve())
            key.unlink()
            self.assertIsNone(cfg.worker_env["TS_AUTHKEY"].resolve())

    def test_worker_env_rejects_bad_specs(self):
        def parse_env(env):
            return config.parse(dict(MINIMAL, agent={"env": env}))

        with self.assertRaisesRegex(config.ConfigError, "exactly one of"):
            parse_env({"TS_AUTHKEY": "~/ts.key"})  # bare string is ambiguous
        with self.assertRaisesRegex(config.ConfigError, "exactly one of"):
            parse_env({"TS_AUTHKEY": {"file": "a", "env": "B"}})
        with self.assertRaisesRegex(config.ConfigError, "unknown source"):
            parse_env({"TS_AUTHKEY": {"secret": "a"}})
        with self.assertRaisesRegex(config.ConfigError, "not a valid variable name"):
            parse_env({"1BAD": {"value": "x"}})
        # The whole point of the file/env indirection: a pasted key is refused.
        with self.assertRaisesRegex(config.ConfigError, "looks like a secret"):
            parse_env({"TS_AUTHKEY": {"value": "tskey-auth-EXAMPLE-not-a-real-key"}})

    def test_docker_platform_auto_default(self):
        cfg = config.parse(MINIMAL)
        self.assertEqual(cfg.docker_platform, "auto")
        data = dict(MINIMAL)
        data["agent"] = {"docker_platform": "linux/amd64"}
        self.assertEqual(config.parse(data).docker_platform, "linux/amd64")
        data["agent"] = {"docker_platform": ""}
        self.assertIsNone(config.parse(data).resolved_docker_platform())

    def test_docker_platform_auto_keys_on_docker_host_arch(self):
        """The docker server's arch decides, not this process's — a daemon
        containerized as emulated amd64 on an arm64 host must still pin."""
        cfg = config.parse(MINIMAL)
        with mock.patch.object(config, "docker_host_arch", return_value="arm64"):
            self.assertEqual(cfg.resolved_docker_platform(), "linux/amd64")
        with mock.patch.object(config, "docker_host_arch", return_value="amd64"):
            self.assertIsNone(cfg.resolved_docker_platform())
        cfg.docker_platform = "linux/arm64"
        self.assertEqual(cfg.resolved_docker_platform(), "linux/arm64")

    def test_docker_platform_rejects_garbage(self):
        for bad in (True, 1, "Auto", "linux amd64", "LINUX/AMD64"):
            data = dict(MINIMAL)
            data["agent"] = {"docker_platform": bad}
            with self.assertRaisesRegex(config.ConfigError, "docker_platform"):
                config.parse(data)
        data = dict(MINIMAL)
        data["agent"] = {"docker_platform": "linux/arm64/v8"}
        self.assertEqual(config.parse(data).docker_platform, "linux/arm64/v8")

    def test_docker_host_arch_caches_success_but_not_fallback(self):
        """A failed probe must retry next call; a successful one is cached."""
        probe_ok = mock.Mock(returncode=0, stdout="arm64\n")
        probe_bad = mock.Mock(returncode=1, stdout="")
        config._docker_host_arch_cache = None
        try:
            with mock.patch.object(config.subprocess, "run", return_value=probe_bad) as r:
                config.docker_host_arch()
                config.docker_host_arch()
                self.assertEqual(r.call_count, 2)
            with mock.patch.object(config.subprocess, "run", return_value=probe_ok) as r:
                self.assertEqual(config.docker_host_arch(), "arm64")
                self.assertEqual(config.docker_host_arch(), "arm64")
                self.assertEqual(r.call_count, 1)
        finally:
            config._docker_host_arch_cache = None

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


class FleetManagerConfigTest(unittest.TestCase):
    def test_defaults_disabled(self):
        fm = config.parse(MINIMAL).fleet_manager
        self.assertFalse(fm.enabled)
        self.assertEqual(fm.advisor, "conservative")
        self.assertEqual(fm.poll_interval_s, 60)
        self.assertEqual(fm.report_interval_s, 3600)
        self.assertTrue(fm.assign_goals)

    def test_disabled_section_skips_validation(self):
        # A half-filled but disabled section must never block startup.
        data = dict(MINIMAL, fleet_manager={"enabled": False, "base_url": ""})
        self.assertFalse(config.parse(data).fleet_manager.enabled)

    def _enabled(self, **over):
        base = {
            "enabled": True,
            "base_url": "http://sig:8100",
            "board_project": "Fleet",
            "board_team": "FUG",
        }
        base.update(over)
        return dict(MINIMAL, fleet_manager=base)

    def test_enabled_parses(self):
        fm = config.parse(self._enabled(report_interval_s=0, assign_goals=False)).fleet_manager
        self.assertTrue(fm.enabled)
        self.assertEqual(fm.base_url, "http://sig:8100")
        self.assertEqual(fm.board_project, "Fleet")
        self.assertEqual(fm.board_team, "FUG")
        self.assertEqual(fm.report_interval_s, 0)
        self.assertFalse(fm.assign_goals)

    def test_enabled_requires_board_and_url(self):
        for missing in ("base_url", "board_project", "board_team"):
            with self.assertRaisesRegex(ConfigError, missing):
                config.parse(self._enabled(**{missing: ""}))

    def test_bad_advisor_rejected(self):
        with self.assertRaisesRegex(ConfigError, "advisor"):
            config.parse(self._enabled(advisor="magic"))

    def test_poll_and_report_bounds(self):
        with self.assertRaisesRegex(ConfigError, "poll_interval_s"):
            config.parse(self._enabled(poll_interval_s=1))
        with self.assertRaisesRegex(ConfigError, "report_interval_s"):
            config.parse(self._enabled(report_interval_s=-1))

    def test_api_key_never_from_config(self):
        with self.assertRaisesRegex(ConfigError, "secrets"):
            config.parse(self._enabled(api_key="sb_secret_value"))

    def test_api_key_file_expands(self):
        fm = config.parse(self._enabled(api_key_file="~/x/sigbot.key")).fleet_manager
        self.assertNotIn("~", str(fm.api_key_file))


class DashboardConfigTest(unittest.TestCase):
    def test_bind_defaults_to_all_interfaces(self):
        # FUG-68: the dashboard binds 0.0.0.0 by default so it's reachable from
        # other machines on the tailnet.
        cfg = config.parse(MINIMAL)
        self.assertEqual(cfg.dashboard.bind, "0.0.0.0")
        self.assertTrue(cfg.dashboard.enabled)
        self.assertTrue(cfg.dashboard.allow_add_project)

    def test_bind_and_add_project_overridable(self):
        data = dict(MINIMAL)
        data["dashboard"] = {"bind": "127.0.0.1", "allow_add_project": False}
        cfg = config.parse(data)
        self.assertEqual(cfg.dashboard.bind, "127.0.0.1")
        self.assertFalse(cfg.dashboard.allow_add_project)


class ParseProjectTest(unittest.TestCase):
    def test_defaults_and_agent_claim(self):
        p = config.parse_project(
            {"name": "foo", "linear_project": "Foo", "repo": "~/Projects/foo"}, "where"
        )
        self.assertEqual(p.name, "foo")
        self.assertEqual(p.claim.strategy, "label")  # the default claim
        self.assertEqual(p.base_ref, "main")
        self.assertNotIn("~", str(p.repo))

    def test_missing_required_key(self):
        with self.assertRaisesRegex(ConfigError, "linear_project"):
            config.parse_project({"name": "foo", "repo": "~/x"}, "where")

    def test_bad_claim_strategy(self):
        with self.assertRaisesRegex(ConfigError, "claim.strategy"):
            config.parse_project(
                {"name": "f", "linear_project": "F", "repo": "~/x",
                 "claim": {"strategy": "bogus"}}, "where",
            )

    def test_nonagent_claim_needs_value(self):
        with self.assertRaisesRegex(ConfigError, "claim.value"):
            config.parse_project(
                {"name": "f", "linear_project": "F", "repo": "~/x",
                 "claim": {"strategy": "label"}}, "where",
            )

    def test_max_workers_validated(self):
        with self.assertRaisesRegex(ConfigError, "max_workers"):
            config.parse_project(
                {"name": "f", "linear_project": "F", "repo": "~/x", "max_workers": 0}, "where",
            )
        # A string (as it arrives from a web form) is coerced.
        p = config.parse_project(
            {"name": "f", "linear_project": "F", "repo": "~/x", "max_workers": "3"}, "where",
        )
        self.assertEqual(p.max_workers, 3)

    def test_secret_rejected(self):
        with self.assertRaisesRegex(ConfigError, "secrets"):
            config.parse_project(
                {"name": "f", "linear_project": "F", "repo": "~/x",
                 "github_token": "ghp_xxx"}, "where",
            )


class AppendProjectTest(unittest.TestCase):
    """append_project persists to the daemon-owned drop-in, NOT config.toml, and
    load() merges the drop-in back into the fleet on every start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.toml"
        self.original = (
            "# my comment\n"
            "[daemon]\n"
            f'state_dir = "{self.root / "state"}"\n\n'
            "[dashboard]\n"
            "enabled = true\n\n"
            "[[projects]]\n"
            'name = "splanc"\n'
            'linear_project = "Splanc"\n'
            'repo = "/repos/splanc"\n'
            'claim = { strategy = "agent" }\n'
        )
        self.path.write_text(self.original)
        self.drop_in = config.load(self.path).added_projects_path()

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trips_and_leaves_config_untouched(self):
        p = config.parse_project(
            {"name": "led-mapper", "linear_project": "LED Mapper",
             "repo": "/repos/led_mapper", "git_url": "https://github.com/o/led_mapper",
             "claim": {"strategy": "state", "value": "Ready for agent"}, "max_workers": 2},
            "where",
        )
        config.append_project(self.drop_in, p)
        # config.toml is never touched — it can sit on a read-only mount.
        self.assertEqual(self.path.read_text(), self.original)
        self.assertNotIn("led-mapper", self.path.read_text())
        cfg = config.load(self.path)
        names = [pr.name for pr in cfg.projects]
        self.assertEqual(names, ["splanc", "led-mapper"])
        added = cfg.project("led-mapper")
        self.assertEqual(added.claim.strategy, "state")
        self.assertEqual(added.claim.value, "Ready for agent")
        self.assertEqual(added.git_url, "https://github.com/o/led_mapper")
        self.assertEqual(added.max_workers, 2)

    def test_string_escaping(self):
        # A value with a quote must survive the append/reload round-trip.
        p = config.parse_project(
            {"name": "q", "linear_project": 'A "Quoted" Board', "repo": "/r",
             "claim": {"strategy": "label", "value": "needs agent"}},
            "where",
        )
        config.append_project(self.drop_in, p)
        cfg = config.load(self.path)
        self.assertEqual(cfg.project("q").linear_project, 'A "Quoted" Board')

    def test_config_wins_on_name_conflict(self):
        # A drop-in entry whose name also lives in config.toml is skipped; the
        # config.toml definition (repo /repos/splanc) is the one that survives.
        p = config.parse_project(
            {"name": "splanc", "linear_project": "Splanc", "repo": "/somewhere/else",
             "claim": {"strategy": "agent"}},
            "where",
        )
        config.append_project(self.drop_in, p)
        cfg = config.load(self.path)
        self.assertEqual([pr.name for pr in cfg.projects], ["splanc"])
        self.assertEqual(str(cfg.project("splanc").repo), "/repos/splanc")

    def test_malformed_drop_in_is_skipped_not_fatal(self):
        self.drop_in.parent.mkdir(parents=True, exist_ok=True)
        self.drop_in.write_text("this is not valid toml =\n")
        cfg = config.load(self.path)  # must not raise
        self.assertEqual([pr.name for pr in cfg.projects], ["splanc"])

    def test_append_is_atomic_no_temp_left_behind(self):
        p = config.parse_project(
            {"name": "led-mapper", "linear_project": "LED Mapper", "repo": "/r",
             "claim": {"strategy": "agent"}},
            "where",
        )
        config.append_project(self.drop_in, p)
        leftovers = list(self.drop_in.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
