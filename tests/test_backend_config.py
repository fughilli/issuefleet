"""Configuration boundaries for independently selected manager and workers."""

import copy
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from issuefleet import config, creds
from issuefleet.agent_runtime.turns import TurnState
from issuefleet.dashboard import _events_from, render_transcript
from issuefleet.model import Issue, WorkerRecord
from issuefleet.worker import provision


BASE = {"projects": [{"name": "app", "linear_project": "App", "repo": "/tmp/app"}]}


class BackendConfigTest(unittest.TestCase):
    def test_defaults_preserve_claude(self):
        cfg = config.parse(BASE)
        self.assertEqual(cfg.fleet_manager.provider, "anthropic")
        self.assertEqual(cfg.runtime_for("app").runtime, "claude")

    def test_load_accepts_legacy_ignored_docker_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                f'[daemon]\nstate_dir = "{tmp}"\n'
                '[agent]\ndocker_platform = ""\n'
                '[[projects]]\nname = "app"\nlinear_project = "App"\nrepo = "/tmp/app"\n'
            )
            cfg = config.load(path)
        self.assertEqual(cfg.runtime_for("app"), config.WorkerRuntimeConfig())
        self.assertFalse(cfg.fleet_manager.enabled)
        self.assertEqual(cfg.fleet_manager.provider, "anthropic")

    def test_manager_and_workers_are_independent(self):
        for provider in ("openai", "anthropic"):
            for runtime in ("codex", "claude"):
                with self.subTest(provider=provider, runtime=runtime):
                    data = copy.deepcopy(BASE)
                    data["fleet_manager"] = {"provider": provider}
                    data["agent"] = {"runtime": runtime}
                    cfg = config.parse(data)
                    self.assertEqual(cfg.fleet_manager.provider, provider)
                    self.assertEqual(cfg.runtime_for("app").runtime, runtime)

    def test_runtime_switch_does_not_inherit_other_runtime_model_or_flags(self):
        data = copy.deepcopy(BASE)
        data["agent"] = {"runtime": "claude", "model": "claude-opus-5",
                         "reasoning_effort": "high", "args": ["--verbose"]}
        data["projects"][0]["agent"] = {"runtime": "codex"}
        selected = config.parse(data).runtime_for("app")
        self.assertEqual(selected, config.WorkerRuntimeConfig(runtime="codex"))

    def test_same_runtime_inherits_and_overrides(self):
        data = copy.deepcopy(BASE)
        data["agent"] = {"runtime": "codex", "model": "gpt-6-astra", "reasoning_effort": "high"}
        data["projects"][0]["agent"] = {"reasoning_effort": "medium"}
        selected = config.parse(data).runtime_for("app")
        self.assertEqual(selected.model, "gpt-6-astra")
        self.assertEqual(selected.reasoning_effort, "medium")

    def test_project_overrides_survive_drop_in_serialization(self):
        data = copy.deepcopy(BASE)
        data["projects"][0]["agent"] = {
            "runtime": "codex", "model": "gpt-6-astra", "args": ["--enable", "some_feature"]
        }
        cfg = config.parse(data)
        restored = config.parse(tomllib.loads(config.project_to_toml(cfg.projects[0])))
        self.assertEqual(restored.runtime_for("app"), cfg.runtime_for("app"))

    def test_invalid_runtime_configuration_fails_before_claim(self):
        for options in (
            {"runtime": "typo"}, {"runtime": "codex", "reasoning_effort": "none"},
            {"args": "--verbose"}, {"args": [12]}, {"model": ""},
            {"runtime": "codex", "args": ["--ephemeral"]},
            {"runtime": "codex", "model": "gpt-6-astra", "args": ["--model", "other"]},
            {"runtime": "claude", "args": ["--resume", "wrong-thread"]},
            {"provider": "codex"}, {"runtme": "codex"},
        ):
            with self.subTest(options=options), self.assertRaises(config.ConfigError):
                config.parse({**BASE, "agent": options})
        data = copy.deepcopy(BASE)
        data["projects"][0]["agent"] = {"provider": "openai"}
        with self.assertRaisesRegex(config.ConfigError, "unknown runtime"):
            config.parse(data)

    def test_invalid_manager_settings_fail_early(self):
        for options in (
            {"provider": "codex"}, {"model": False}, {"reasoning_effort": "none"},
            {"max_turns": 0}, {"max_turns": True}, {"max_output_tokens": "1000"},
            {"model_provider": "openai"},
        ):
            with self.subTest(options=options), self.assertRaises(config.ConfigError):
                config.parse({**BASE, "fleet_manager": options})

    def test_manager_key_is_separate_from_codex_auth(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            cfg = config.parse({**BASE, "fleet_manager": {"provider": "openai"}})
            cfg.openai_api_key_env = "TEST_MANAGER_KEY"
            cfg.openai_api_key_file = Path(tmp) / "openai.key"
            self.assertIsNone(creds.resolve_manager_key(cfg))
            cfg.openai_api_key_file.write_text("file-key\n")
            self.assertEqual(creds.resolve_manager_key(cfg), "file-key")
            os.environ["TEST_MANAGER_KEY"] = "env-key"
            self.assertEqual(creds.resolve_manager_key(cfg), "env-key")
        with self.assertRaisesRegex(config.ConfigError, "secrets"):
            config.parse({**BASE, "credentials": {"api_key": "secret"}})

    def test_provision_snapshots_project_runtime_and_preserves_existing_worker(self):
        issue = Issue("i", "TEST-1", "Title", "Body", "", 0, "Todo", "unstarted")
        data = copy.deepcopy(BASE)
        data["agent"] = {"claude_args": ["--verbose"]}
        data["projects"][0]["agent"] = {"runtime": "codex", "model": "gpt-6-astra"}
        cfg = config.parse(data)
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            provision(worktree, issue, "agent/test", "main", cfg, project_name="app")
            state = TurnState.load(worktree / ".agent")
            self.assertEqual(state.runtime, "codex")
            self.assertEqual(state.model, "gpt-6-astra")
            self.assertEqual(state.claude_args, [])
            state.runtime_session_id = "persistent-thread"
            state.save(worktree / ".agent")
            cfg.projects[0].agent = {"runtime": "claude"}
            provision(worktree, issue, "agent/test", "main", cfg, project_name="app")
            state = TurnState.load(worktree / ".agent")
            self.assertEqual(state.runtime, "codex")
            self.assertEqual(state.runtime_session_id, "persistent-thread")

    def test_manager_builder_selects_openai_key_and_rejects_missing_key(self):
        from types import SimpleNamespace
        from issuefleet.cli import build_fleet_manager

        cfg = config.parse({**BASE, "fleet_manager": {"provider": "openai"}})
        cfg.fleet_manager.enabled = True
        reconciler = SimpleNamespace(tracker=object(), registry=object())
        with mock.patch("issuefleet.creds.resolve_sigbot_key", return_value=("signal-key", "test")), \
             mock.patch("issuefleet.creds.resolve_anthropic_key", return_value="anthropic-key"), \
             mock.patch("issuefleet.creds.resolve_openai_key", return_value="openai-key") as key, \
             mock.patch("issuefleet.sigbot.SigbotClient"), \
             mock.patch("issuefleet.fleet_manager.FleetManager") as manager:
            build_fleet_manager(cfg, reconciler)
            self.assertEqual(manager.call_args.kwargs["agent_key"], "openai-key")
            key.return_value = None
            with self.assertRaisesRegex(creds.CredentialError, "OpenAI fleet manager needs"):
                build_fleet_manager(cfg, reconciler)

    def test_old_registry_records_default_to_claude(self):
        rec = WorkerRecord.from_dict(dict(
            issue_id="i", issue_key="T-1", issue_title="T", issue_url="", project="app",
            repo="/tmp/app", branch="agent/t", worktree="/tmp/worktree", base_ref="main",
            session_uuid="claude-session", tmux_session="fleet-t",
        ))
        self.assertEqual(rec.runtime, "claude")
        self.assertIsNone(rec.runtime_session_id)

    def test_codex_transcript_renders_text_tools_and_failure(self):
        events = []
        for raw in (
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "<hello>"}},
            {"type": "item.started", "item": {"type": "command_execution", "command": "git status"}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1,
                                                "aggregated_output": "failed"}},
            {"type": "turn.failed", "error": {"message": "provider unavailable"}},
        ):
            events.extend(_events_from(raw))
        html = render_transcript("T-1", 1, events)
        self.assertIn("&lt;hello&gt;", html)
        self.assertIn("git status", html)
        self.assertIn("provider unavailable", html)
        self.assertIn("turn errored", html)


if __name__ == "__main__":
    unittest.main()
