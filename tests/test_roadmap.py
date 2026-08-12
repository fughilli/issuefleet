import tempfile
import unittest
from pathlib import Path

from issuefleet import config as config_mod
from issuefleet.config import ConfigError
from issuefleet.httpx import ApiError
from issuefleet.roadmap import RoadmapBot
from fakes import FakePublisher, FakeTracker, make_issue


BOARD = "Roadmap Board"

CONFIG = {
    "projects": [
        {"name": "p", "linear_project": "P", "repo": "/tmp/p", "claim": {"strategy": "agent"}}
    ],
    "roadmap": {
        "enabled": True,
        "projects": [BOARD],
        "interval_s": 86400,
        "discord": {"enabled": True, "channel_id": "1234"},
    },
}


class RoadmapTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = config_mod.parse(CONFIG)
        self.cfg.state_dir = self.tmp / "state"
        self.tracker = FakeTracker()
        self.pub = FakePublisher("discord")
        self.now = [1000.0]

    def tearDown(self):
        self._tmp.cleanup()

    def _issue(self, key, title, state_name="In Progress", priority=2, description="Please fix it."):
        self.tracker.add_issue(
            make_issue(
                key=key, id=f"issue-{key}", title=title, description=description,
                state_name=state_name, project_id=BOARD, priority=priority,
            )
        )

    def _bot(self, *, agent_key=None, transport=None, publishers=None):
        kwargs = {"agent_key": agent_key, "clock": lambda: self.now[0]}
        if transport is not None:
            kwargs["transport"] = transport
        if publishers is None:
            publishers = [self.pub]
        return RoadmapBot(self.cfg, self.tracker, publishers, **kwargs)


class ModelTransport:
    """Fake Anthropic Messages transport. Records the request; returns text."""

    def __init__(self, text="MODEL SUMMARY", fail=False):
        self.text = text
        self.fail = fail
        self.requests = []

    def __call__(self, method, url, headers, payload):
        self.requests.append(payload)
        if self.fail:
            raise ApiError(500, url, "model outage")
        return {"content": [{"type": "text", "text": self.text}]}


class SummaryTest(RoadmapTestBase):
    def test_deterministic_fallback_lists_issues(self):
        self._issue("FUG-1", "Add caching")
        self._issue("FUG-2", "Fix login", state_name="Todo", priority=1)
        text = self._bot(publishers=[self.pub]).render()
        self.assertIn("Roadmap update", text)
        self.assertIn("FUG-1", text)
        self.assertIn("FUG-2", text)
        self.assertIn("Add caching", text)
        # grouped by state
        self.assertIn("In Progress", text)
        self.assertIn("Todo", text)

    def test_no_open_work_renders_empty(self):
        self.assertEqual(self._bot(publishers=[self.pub]).render(), "")

    def test_urgent_sorts_before_lower_priority_in_a_state(self):
        self._issue("FUG-1", "low one", priority=4)
        self._issue("FUG-2", "urgent one", priority=1)
        text = self._bot(publishers=[self.pub]).render()
        self.assertLess(text.index("FUG-2"), text.index("FUG-1"))

    def test_model_path_uses_system_prompt_and_returns_its_text(self):
        self._issue("FUG-1", "Add caching")
        mt = ModelTransport(text="Crisp update.")
        text = self._bot(agent_key="sk-test", transport=mt, publishers=[self.pub]).render()
        self.assertEqual(text, "Crisp update.")
        req = mt.requests[0]
        self.assertEqual(req["system"], self.cfg.roadmap.system_prompt)
        self.assertEqual(req["model"], self.cfg.roadmap.model)
        self.assertIn("FUG-1", req["messages"][0]["content"])

    def test_model_failure_falls_back_to_deterministic(self):
        self._issue("FUG-1", "Add caching")
        mt = ModelTransport(fail=True)
        text = self._bot(agent_key="sk-test", transport=mt, publishers=[self.pub]).render()
        self.assertIn("Roadmap update", text)
        self.assertIn("FUG-1", text)

    def test_empty_model_text_falls_back(self):
        self._issue("FUG-1", "Add caching")
        mt = ModelTransport(text="")
        text = self._bot(agent_key="sk-test", transport=mt, publishers=[self.pub]).render()
        self.assertIn("FUG-1", text)

    def test_unreadable_project_is_skipped_not_fatal(self):
        # A project whose read raises is logged and skipped; others still report.
        self._issue("FUG-1", "Add caching")

        class Boom(FakeTracker):
            def open_issues_in_project(self, ref):
                if ref == "Broken":
                    raise ConnectionError("outage")
                return super().open_issues_in_project(ref)

        boom = Boom()
        boom.add_issue(make_issue(key="FUG-1", id="issue-FUG-1", title="Add caching",
                                  state_name="In Progress", project_id=BOARD))
        self.cfg.roadmap.projects = ["Broken", BOARD]
        bot = RoadmapBot(self.cfg, boom, [self.pub], clock=lambda: self.now[0])
        text = bot.render()
        self.assertIn("FUG-1", text)


class PublishTest(RoadmapTestBase):
    def test_publish_now_pushes_to_surface(self):
        self._issue("FUG-1", "Add caching")
        self.assertTrue(self._bot(publishers=[self.pub]).publish_now())
        self.assertEqual(len(self.pub.published), 1)
        self.assertIn("FUG-1", self.pub.published[0])

    def test_publish_now_false_when_no_work(self):
        self.assertFalse(self._bot(publishers=[self.pub]).publish_now())
        self.assertEqual(self.pub.published, [])

    def test_one_dead_surface_does_not_sink_the_others(self):
        self._issue("FUG-1", "Add caching")
        good, dead = FakePublisher("good"), FakePublisher("dead", fail=True)
        self.assertTrue(self._bot(publishers=[dead, good]).publish_now())
        self.assertEqual(len(good.published), 1)

    def test_all_surfaces_failing_returns_false(self):
        self._issue("FUG-1", "Add caching")
        dead = FakePublisher("dead", fail=True)
        self.assertFalse(self._bot(publishers=[dead]).publish_now())


class TickTest(RoadmapTestBase):
    def test_first_tick_publishes_then_holds_until_interval(self):
        self._issue("FUG-1", "Add caching")
        bot = self._bot(publishers=[self.pub])
        bot.tick()
        self.assertEqual(len(self.pub.published), 1)
        self.assertEqual(bot.state["last_report"], 1000.0)
        # within the interval: no second publish
        self.now[0] += 100
        bot.tick()
        self.assertEqual(len(self.pub.published), 1)
        # past the interval: publishes again
        self.now[0] += 90000
        bot.tick()
        self.assertEqual(len(self.pub.published), 2)

    def test_interval_zero_never_auto_publishes(self):
        self._issue("FUG-1", "Add caching")
        self.cfg.roadmap.interval_s = 0
        bot = self._bot(publishers=[self.pub])
        bot.tick()
        self.assertEqual(self.pub.published, [])

    def test_timestamp_not_advanced_when_publish_fails(self):
        self._issue("FUG-1", "Add caching")
        bot = self._bot(publishers=[FakePublisher("dead", fail=True)])
        bot.tick()
        self.assertIsNone(bot.state["last_report"])

    def test_state_persists_across_restart(self):
        self._issue("FUG-1", "Add caching")
        self._bot(publishers=[self.pub]).tick()
        # a fresh bot reads the persisted timestamp and holds
        bot2 = self._bot(publishers=[self.pub])
        self.assertEqual(bot2.state["last_report"], 1000.0)
        bot2.tick()
        self.assertEqual(len(self.pub.published), 1)


class ConfigTest(unittest.TestCase):
    def _cfg(self, roadmap):
        base = {
            "projects": [
                {"name": "p", "linear_project": "P", "repo": "/tmp/p",
                 "claim": {"strategy": "agent"}}
            ],
            "roadmap": roadmap,
        }
        return config_mod.parse(base)

    def test_disabled_by_default(self):
        cfg = self._cfg({})
        self.assertFalse(cfg.roadmap.enabled)

    def test_bare_string_project_is_accepted(self):
        cfg = self._cfg({"enabled": True, "projects": "Splanc",
                         "discord": {"enabled": True, "channel_id": "1"}})
        self.assertEqual(cfg.roadmap.projects, ["Splanc"])

    def test_enabled_without_projects_is_error(self):
        with self.assertRaises(ConfigError):
            self._cfg({"enabled": True, "discord": {"enabled": True, "channel_id": "1"}})

    def test_enabled_without_surface_is_error(self):
        with self.assertRaises(ConfigError):
            self._cfg({"enabled": True, "projects": ["Splanc"]})

    def test_default_system_prompt_is_chat_shaped(self):
        cfg = self._cfg({"enabled": True, "projects": ["Splanc"],
                         "discord": {"enabled": True, "channel_id": "1"}})
        prompt = cfg.roadmap.system_prompt
        self.assertIn("workstream summarization agent", prompt)
        # Chat clients render neither, so the default must not ask for them.
        self.assertIn("Do NOT emit Markdown tables, Mermaid", prompt)
        # A friendly lede up top, with the per-workstream detail left factual.
        self.assertIn("chipper, informal tone", prompt)

    def test_custom_system_prompt_and_model(self):
        cfg = self._cfg({
            "enabled": True, "projects": ["Splanc"],
            "discord": {"enabled": True, "channel_id": "1"},
            "system_prompt": "Be terse.", "model": "claude-x",
        })
        self.assertEqual(cfg.roadmap.system_prompt, "Be terse.")
        self.assertEqual(cfg.roadmap.model, "claude-x")


class DiscordSurfaceConfigTest(ConfigTest):
    def test_bot_is_the_default_mode(self):
        cfg = self._cfg({"enabled": True, "projects": ["Splanc"],
                         "discord": {"enabled": True, "channel_id": "1234"}})
        self.assertEqual(cfg.roadmap.discord.mode, "bot")
        self.assertEqual(cfg.roadmap.discord.channel_id, "1234")

    def test_bot_mode_without_channel_id_is_error(self):
        with self.assertRaises(ConfigError):
            self._cfg({"enabled": True, "projects": ["Splanc"], "discord": {"enabled": True}})

    def test_webhook_mode_needs_no_channel_id(self):
        cfg = self._cfg({"enabled": True, "projects": ["Splanc"],
                         "discord": {"enabled": True, "mode": "webhook"}})
        self.assertEqual(cfg.roadmap.discord.mode, "webhook")

    def test_unknown_mode_is_error(self):
        with self.assertRaises(ConfigError):
            self._cfg({"enabled": True, "projects": ["Splanc"],
                       "discord": {"enabled": True, "mode": "gateway", "channel_id": "1"}})

    def test_numeric_channel_id_is_normalized_to_a_string(self):
        # TOML gives an unquoted snowflake back as an int; the endpoint wants text.
        cfg = self._cfg({"enabled": True, "projects": ["Splanc"],
                         "discord": {"enabled": True, "channel_id": 1234567890123456789}})
        self.assertEqual(cfg.roadmap.discord.channel_id, "1234567890123456789")

    def test_token_in_the_config_file_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._cfg({"enabled": True, "projects": ["Splanc"],
                       "discord": {"enabled": True, "channel_id": "1", "bot_token": "abc"}})

    def test_secret_locations_are_overridable(self):
        cfg = self._cfg({"enabled": True, "projects": ["Splanc"], "discord": {
            "enabled": True, "channel_id": "1",
            "bot_token_env": "MY_TOKEN", "bot_token_file": "/tmp/bot.token",
        }})
        self.assertEqual(cfg.roadmap.discord.bot_token_env, "MY_TOKEN")
        self.assertEqual(str(cfg.roadmap.discord.bot_token_file), "/tmp/bot.token")


if __name__ == "__main__":
    unittest.main()
