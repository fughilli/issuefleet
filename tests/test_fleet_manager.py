import tempfile
import unittest
from pathlib import Path

from issuefleet import config as config_mod
from issuefleet.advisor import ConservativeAdvisor, Triage
from issuefleet.fleet_manager import FleetManager
from issuefleet.mailbox import Mailbox
from issuefleet.model import WorkerRecord
from issuefleet.registry import Registry
from fakes import FakeSignal, FakeTracker, make_issue


CONFIG = {
    "projects": [
        {"name": "p", "linear_project": "P", "repo": "/tmp/p", "claim": {"strategy": "agent"}}
    ],
    "fleet_manager": {
        "enabled": True,
        "base_url": "http://sig:8100",
        "board_project": "Fleet",
        "board_team": "FUG",
        "report_interval_s": 0,  # disable reports unless a test wants them
    },
}


class YesAdvisor:
    def triage(self, q):
        return Triage(answerable=True, answer="Use Redis.", reason="ticket says so")


class FleetManagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = config_mod.parse(CONFIG)
        self.cfg.state_dir = self.tmp / "state"
        self.tracker = FakeTracker()
        self.tracker.app_identity = True
        self.signal = FakeSignal()
        self.registry = Registry(self.cfg.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _fm(self, advisor=None, clock=None, agent_key=None):
        return FleetManager(
            self.cfg,
            self.tracker,
            self.signal,
            advisor or ConservativeAdvisor(),
            self.registry,
            clock=clock or (lambda: 0.0),
            agent_key=agent_key,
        )

    def _worker(self, key="FUG-9", title="add caching", pr_number=None):
        wt = self.tmp / "wt" / key
        (wt / ".agent" / "mailbox").mkdir(parents=True, exist_ok=True)
        rec = WorkerRecord(
            issue_id=f"issue-{key}",
            issue_key=key,
            issue_title=title,
            issue_url=f"https://linear.app/x/{key}",
            project="p",
            repo="/tmp/p",
            branch=f"agent/{key}",
            worktree=str(wt),
            base_ref="main",
            session_uuid="s",
            tmux_session=f"tmux-{key}",
            pr_number=pr_number,
        )
        self.registry.add(rec)
        self.tracker.add_issue(
            make_issue(key=key, id=f"issue-{key}", title=title, description="Use Redis.")
        )
        return rec

    def _ask(self, rec, text="Which database should I use?"):
        return Mailbox(Path(rec.worktree) / ".agent" / "mailbox").put_outbox(
            "question", {"text": text}
        )

    def _inbox(self, rec):
        return Mailbox(Path(rec.worktree) / ".agent" / "mailbox").pending_inbox()

    # -- signal baseline ---------------------------------------------------

    def test_first_run_sets_baseline_without_processing(self):
        self.signal.user_says("Build a dashboard", id="m0")
        self._fm().tick()
        self.assertEqual(self.tracker.created, [])  # history not replayed as goals
        # cursor now at the newest message
        fm = self._fm()
        self.assertEqual(fm.state["signal_cursor"], "m0")

    # -- goals -------------------------------------------------------------

    def test_new_message_becomes_a_goal_and_is_assigned(self):
        fm = self._fm()
        self.signal.user_says("prior", id="m0")
        fm.tick()  # baseline
        self.signal.user_says("Build a metrics dashboard\nwith p95 latency", id="m1")
        fm.tick()
        self.assertEqual(len(self.tracker.created), 1)
        created = self.tracker.created[0]
        self.assertEqual(created["title"], "Build a metrics dashboard")
        self.assertEqual(created["team"], "FUG")
        self.assertEqual(created["project_id"], "Fleet")
        # assigned to the fleet identity so it auto-claims
        self.assertEqual([a[1] for a in self.tracker.assigned], [self.tracker.viewer_id])
        self.assertTrue(any("Filed" in s for s in self.signal.sent))

    def test_goal_prefix_forces_goal_even_with_pending(self):
        rec = self._worker()
        fm = self._fm()
        fm.state["pending"].append(
            {"msg_id": "x", "issue_id": rec.issue_id, "issue_key": rec.issue_key, "question": "?"}
        )
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base-msg", id="base")
        self.signal.user_says("goal: ship it faster", id="g1")
        fm.tick()
        self.assertEqual(len(self.tracker.created), 1)
        self.assertEqual(self.tracker.created[0]["title"], "ship it faster")

    # -- acknowledgement reactions -----------------------------------------

    def test_a_handled_message_gets_seen_then_done(self):
        fm = self._fm()
        self.signal.user_says("base", id="base")
        fm.tick()
        self.signal.reacted.clear()
        self.signal.user_says("build a dashboard", id="u1")
        fm.tick()
        self.assertEqual(self.signal.reacted,
                         [("u1", "\N{EYES}"), ("u1", "\N{WHITE HEAVY CHECK MARK}")])

    def test_the_agent_path_also_acknowledges(self):
        fm = self._fm(agent_key="sk-test")
        self._ask_agent(fm, "what's up?", lambda **kw: "all quiet")
        self.assertEqual(self.signal.reacted,
                         [("q1", "\N{EYES}"), ("q1", "\N{WHITE HEAVY CHECK MARK}")])

    def test_our_own_posts_are_never_reacted_to(self):
        fm = self._fm()
        self.signal.user_says("base", id="base")
        fm.tick()
        self.signal.user_says("goal: ship it", id="u1")
        fm.tick()
        self.signal.reacted.clear()
        fm.tick()  # the "Filed ..." confirmation is now in the log
        self.assertEqual(self.signal.reacted, [])

    def test_reactions_being_unsupported_does_not_break_the_tick(self):
        # An un-upgraded sigbot must degrade to silence, not fail a message.
        self.signal.reactions_unsupported = True
        fm = self._fm()
        self.signal.user_says("base", id="base")
        fm.tick()
        self.signal.user_says("build a dashboard", id="u1")
        fm.tick()
        self.assertEqual(len(self.tracker.created), 1)
        self.assertEqual(self.signal.reacted, [])

    # -- own-message loopback ----------------------------------------------

    def test_the_managers_own_posts_are_never_reprocessed(self):
        # The live bug: sigbot stores no sender on outgoing rows, so the
        # author-name filter never matched and the manager answered itself —
        # filing its own "Filed FUG-49" confirmation as a new goal, recursively.
        fm = self._fm()
        self.signal.user_says("base", id="base")
        fm.tick()  # baseline
        self.signal.user_says("build a dashboard", id="u1")
        fm.tick()
        self.assertEqual(len(self.tracker.created), 1)
        filed = self.signal.sent[-1]
        self.assertIn("Filed", filed)
        # That confirmation is now in the group log. Another tick must ignore it.
        self.tracker.created.clear()
        fm.tick()
        self.assertEqual(self.tracker.created, [])

    def test_an_outbound_message_is_skipped_even_with_a_matching_author(self):
        from issuefleet.sigbot import SignalMessage

        fm = self._fm()
        self.signal.user_says("base", id="base")
        fm.tick()
        self.signal.log.append(
            SignalMessage(id="x1", text="goal: not a real goal",
                          author="kevin", direction="out"))
        fm.tick()
        self.assertEqual(self.tracker.created, [])

    def test_direction_defaults_to_inbound_when_absent(self):
        # An older sigbot omits the field; the manager must not go deaf.
        from issuefleet.sigbot import SignalMessage

        m = SignalMessage.from_api({"id": 1, "text": "hi", "sender": "kevin"})
        self.assertFalse(m.outbound)
        m_out = SignalMessage.from_api({"id": 2, "text": "hi", "direction": "out"})
        self.assertTrue(m_out.outbound)
        self.assertEqual(m_out.author, "unknown")  # no sender on outgoing rows

    # -- question baseline -------------------------------------------------

    def _archive_question(self, rec, text):
        """A question the reconciler already drained — the shape that was being
        replayed as a fresh escalation."""
        mb = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
        msg = mb.put_outbox("question", {"text": text})
        mb.archive_outbox(msg)
        return msg

    def test_first_run_baselines_archived_questions_instead_of_escalating(self):
        # The regression: a fresh state_dir replayed every archived `agentctl
        # ask` as a live escalation, including ones long since resolved.
        rec = self._worker()
        self._archive_question(rec, "a stale question from hours ago")
        fm = self._fm()
        fm.tick()
        self.assertEqual(fm.state["pending"], [])
        self.assertFalse(any("stale question" in s for s in self.signal.sent))
        self.assertTrue(fm.state["questions_baselined"])

    def test_a_pending_question_still_escalates_on_the_first_tick(self):
        # Not yet drained by anyone => genuinely unanswered, not history.
        rec = self._worker()
        self._ask(rec, "I am actually blocked right now")
        fm = self._fm()
        fm.tick()
        self.assertEqual(len(fm.state["pending"]), 1)
        self.assertIn("I am actually blocked right now", self.signal.sent[-1])

    def test_a_question_after_the_baseline_still_escalates(self):
        rec = self._worker()
        self._archive_question(rec, "stale")
        fm = self._fm()
        fm.tick()  # baseline
        self.signal.sent.clear()
        self._ask(rec, "a genuinely new question")
        fm.tick()
        self.assertEqual(len(fm.state["pending"]), 1)
        self.assertIn("a genuinely new question", self.signal.sent[-1])

    def test_an_existing_state_file_is_not_re_baselined(self):
        # A daemon that already escalated must not swallow those questions on
        # the next start just because the flag is new.
        rec = self._worker()
        fm = self._fm()
        fm.state["seen_questions"] = ["some-old-id"]
        fm.state.pop("questions_baselined", None)
        fm._save_state()
        self._ask(rec, "still waiting on you")
        fresh = self._fm()
        self.assertTrue(fresh.state["questions_baselined"])
        fresh.tick()
        self.assertEqual(len(fresh.state["pending"]), 1)

    # -- agentic inbound path ---------------------------------------------

    def _ask_agent(self, fm, text, fake_run_agent):
        """Drive one inbound message through the agent path."""
        from unittest import mock

        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base-msg", id="base")
        self.signal.user_says(text, id="q1")
        with mock.patch("issuefleet.fleet_manager.run_agent", fake_run_agent):
            fm.tick()

    def test_a_question_is_answered_not_filed_as_a_ticket(self):
        # The regression: "What's going on in the Splanc project?" used to fall
        # through the dispatch table into _file_goal.
        seen = {}

        def fake_run_agent(**kw):
            seen.update(kw)
            return "Two agents are running on Splanc; FUG-43 has PR #26 open."

        fm = self._fm(agent_key="sk-test")
        self._ask_agent(fm, "What's going on in the Splanc project?", fake_run_agent)
        self.assertEqual(self.tracker.created, [])  # nothing filed
        self.assertIn("FUG-43 has PR #26 open.", self.signal.sent[-1])
        self.assertIn("Splanc", seen["user_message"])
        self.assertIn("list_workers", [t.name for t in seen["tools"]])

    def test_agent_failure_falls_back_to_scripted_dispatch(self):
        from issuefleet.agent import AgentError

        def boom(**kw):
            raise AgentError("no key / API down")

        fm = self._fm(agent_key="sk-test")
        self._ask_agent(fm, "goal: ship it faster", boom)
        # The scripted path still recorded the goal rather than dropping it.
        self.assertEqual(len(self.tracker.created), 1)
        self.assertEqual(self.tracker.created[0]["title"], "ship it faster")

    def test_without_a_key_the_scripted_dispatch_runs(self):
        def never(**kw):
            raise AssertionError("agent must not run without a key")

        fm = self._fm()  # no agent_key
        self._ask_agent(fm, "goal: ship it faster", never)
        self.assertEqual(len(self.tracker.created), 1)

    def test_file_goal_tool_files_and_reports_the_key(self):
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        out = tools["file_goal"].run({"text": "make HITL tests faster"})
        self.assertEqual(len(self.tracker.created), 1)
        self.assertIn("Filed", out)

    def test_reply_to_worker_tool_delivers_and_clears_pending(self):
        rec = self._worker(key="FUG-9")
        fm = self._fm(agent_key="sk-test")
        fm.state["pending"].append(
            {"msg_id": "x", "issue_id": rec.issue_id, "issue_key": "FUG-9", "question": "?"}
        )

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        out = tools["reply_to_worker"].run({"issue_key": "FUG-9", "text": "use Redis"})
        self.assertIn("Delivered", out)
        self.assertEqual(fm.state["pending"], [])
        inbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox").pending_inbox()
        self.assertEqual([m.payload["text"] for m in inbox], ["use Redis"])

    def test_reply_to_worker_tool_reports_an_unknown_key(self):
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        self.assertIn("no active worker", tools["reply_to_worker"].run(
            {"issue_key": "FUG-404", "text": "hi"}
        ))

    def test_list_workers_tool_flags_who_is_awaiting_the_human(self):
        self._worker(key="FUG-9")
        fm = self._fm(agent_key="sk-test")
        fm.state["pending"].append(
            {"msg_id": "x", "issue_id": "i", "issue_key": "FUG-9", "question": "?"}
        )

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        out = tools["list_workers"].run({})
        self.assertIn("FUG-9", out)
        self.assertIn("awaiting_human=yes", out)

    def test_list_open_issues_accepts_the_config_name_not_just_the_linear_one(self):
        # The live bug: list_workers reports the config name ("p"), the tracker
        # keys on the Linear name ("P"), and the agent fed one into the other.
        self.tracker.add_issue(
            make_issue(key="FUG-9", id="issue-FUG-9", title="add caching", project_id="P"))
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        for spelling in ("p", "P", "  p  "):
            self.assertIn("add caching", tools["list_open_issues"].run({"project": spelling}),
                          f"failed for {spelling!r}")

    def test_list_open_issues_defaults_to_the_goals_board(self):
        self.tracker.add_issue(
            make_issue(key="FUG-2", id="issue-FUG-2", title="a goal", project_id="Fleet"))
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        self.assertIn("a goal", tools["list_open_issues"].run({}))

    def test_the_project_argument_is_enumerated_for_the_model(self):
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        enum = tools["list_open_issues"].input_schema["properties"]["project"]["enum"]
        self.assertIn("Fleet", enum)   # the goals board
        self.assertIn("P", enum)       # Linear name
        self.assertIn("p", enum)       # config name

    def test_get_issue_tool_searches_worker_projects_not_just_the_board(self):
        # Worker issues live in a configured project ("P"), not the goals board.
        self.tracker.add_issue(
            make_issue(key="FUG-9", id="issue-FUG-9", title="add caching",
                       description="Use Redis.", project_id="P")
        )
        fm = self._fm(agent_key="sk-test")

        class M:
            id = "m1"
            author = "kevin"

        tools = {t.name: t for t in fm._agent_tools(M())}
        out = tools["get_issue"].run({"issue_key": "FUG-9"})
        self.assertIn("add caching", out)
        self.assertIn("Use Redis.", out)  # the description
        self.assertIn("No open issue", tools["get_issue"].run({"issue_key": "FUG-404"}))

    def test_goal_filing_is_deduped_by_marker(self):
        fm = self._fm()

        class M:
            id = "dup"

        fm._file_goal(M(), "do the thing")
        fm._file_goal(M(), "do the thing")  # marker already present
        self.assertEqual(len(self.tracker.created), 1)

    def test_own_messages_are_ignored(self):
        fm = self._fm()
        self.signal.user_says("prior", id="m0")
        fm.tick()  # baseline
        self.signal.send("📥 Filed FUG-1: something")  # bot's own send, authored "fleet"
        fm.tick()
        self.assertEqual(self.tracker.created, [])  # our own message wasn't taken as a goal

    # -- blocked-worker triage --------------------------------------------

    def test_unanswerable_question_escalates_to_signal(self):
        rec = self._worker()
        self._ask(rec, "Should we drop backwards compatibility?")
        fm = self._fm()  # conservative advisor → escalate
        fm.tick()
        self.assertTrue(any("is blocked and needs you" in s for s in self.signal.sent))
        self.assertEqual(len(fm.state["pending"]), 1)
        self.assertEqual(fm.state["pending"][0]["issue_key"], "FUG-9")

    def test_question_is_only_escalated_once(self):
        rec = self._worker()
        self._ask(rec)
        fm = self._fm()
        fm.tick()
        fm.tick()
        blocked = [s for s in self.signal.sent if "is blocked" in s]
        self.assertEqual(len(blocked), 1)

    def test_answerable_question_is_auto_answered(self):
        rec = self._worker()
        self._ask(rec, "Which database should I use?")
        fm = self._fm(advisor=YesAdvisor())
        fm.tick()
        replies = self._inbox(rec)
        self.assertEqual(len(replies), 1)
        self.assertIn("Use Redis.", replies[0].payload["text"])
        self.assertEqual(fm.state["pending"], [])  # not escalated
        self.assertTrue(any("Auto-answered" in s for s in self.signal.sent))

    # -- human answer routing ---------------------------------------------

    def test_human_reply_routes_to_the_blocked_worker(self):
        rec = self._worker()
        self._ask(rec, "Which DB?")
        fm = self._fm()
        fm.tick()  # escalate; also sets state
        # baseline the cursor at the escalation message, then the human replies
        fm.state["signal_cursor"] = self.signal.log[-1].id
        self.signal.user_says("use postgres", author="kevin")
        fm.tick()
        replies = self._inbox(rec)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].payload["text"], "use postgres")
        self.assertEqual(replies[0].payload["author"], "kevin")
        self.assertEqual(fm.state["pending"], [])
        self.assertTrue(any("Relayed your answer to FUG-9" in s for s in self.signal.sent))

    def test_reply_prefixed_with_issue_key_routes_directly(self):
        rec = self._worker(key="FUG-42")
        fm = self._fm()
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base", id="base")
        self.signal.user_says("FUG-42: use the shared cache", author="kevin")
        fm.tick()
        replies = self._inbox(rec)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].payload["text"], "use the shared cache")

    def test_reply_for_unknown_worker_warns(self):
        fm = self._fm()
        fm.state["pending"].append(
            {"msg_id": "x", "issue_id": "gone", "issue_key": "FUG-99", "question": "?"}
        )
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base", id="base")
        self.signal.user_says("here is the answer", author="kevin")
        fm.tick()
        self.assertTrue(any("no active worker" in s for s in self.signal.sent))
        self.assertEqual(fm.state["pending"], [])

    # -- reports -----------------------------------------------------------

    def test_report_sent_on_interval(self):
        self.cfg.fleet_manager.report_interval_s = 60
        self._worker(key="FUG-7", title="a task", pr_number=101)
        t = {"now": 0.0}
        fm = self._fm(clock=lambda: t["now"])
        fm.tick()  # first tick reports (last_report=0)
        reports = [s for s in self.signal.sent if "Fleet status" in s]
        self.assertEqual(len(reports), 1)
        self.assertIn("FUG-7", reports[0])
        self.assertIn("PR #101", reports[0])
        # not again before the interval elapses
        t["now"] = 30.0
        fm.tick()
        self.assertEqual(len([s for s in self.signal.sent if "Fleet status" in s]), 1)
        # again after the interval
        t["now"] = 61.0
        fm.tick()
        self.assertEqual(len([s for s in self.signal.sent if "Fleet status" in s]), 2)

    # -- persistence -------------------------------------------------------

    def test_state_persists_across_restart(self):
        rec = self._worker()
        self._ask(rec)
        self._fm().tick()  # escalates, saves state
        fm2 = self._fm()  # fresh instance loads fleet_manager.json
        self.assertEqual(len(fm2.state["pending"]), 1)
        # the same question isn't re-escalated after restart
        fm2.tick()
        self.assertEqual(len([s for s in self.signal.sent if "is blocked" in s]), 1)

    # -- robustness fixes --------------------------------------------------

    def test_transient_tracker_error_retries_question(self):
        rec = self._worker()
        self._ask(rec, "Should we drop v1 support?")
        self.tracker.fail_get_issue.add(rec.issue_id)
        fm = self._fm()
        fm.tick()  # get_issue raises → caught → question NOT marked seen/escalated
        self.assertEqual(fm.state["pending"], [])
        self.assertEqual([s for s in self.signal.sent if "is blocked" in s], [])
        self.assertEqual(fm.state["seen_questions"], [])
        self.tracker.fail_get_issue.discard(rec.issue_id)
        fm.tick()  # now it escalates
        self.assertEqual(len(fm.state["pending"]), 1)

    def test_bare_reply_with_multiple_pending_prompts_for_key(self):
        r1 = self._worker(key="FUG-1")
        r2 = self._worker(key="FUG-2")
        fm = self._fm()
        fm.state["pending"] = [
            {"msg_id": "a", "issue_id": r1.issue_id, "issue_key": "FUG-1", "question": "q1"},
            {"msg_id": "b", "issue_id": r2.issue_id, "issue_key": "FUG-2", "question": "q2"},
        ]
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base", id="base")
        self.signal.user_says("use postgres")  # bare + ambiguous
        fm.tick()
        self.assertTrue(any("Multiple workers are waiting" in s for s in self.signal.sent))
        self.assertEqual(self._inbox(r1), [])
        self.assertEqual(self._inbox(r2), [])

    def test_goal_filing_failure_notifies_user(self):
        fm = self._fm()
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base", id="base")
        self.signal.user_says("Build a thing")
        self.tracker.fail_next_create = 1
        fm.tick()
        self.assertEqual(self.tracker.created, [])
        self.assertTrue(any("Couldn't record that goal" in s for s in self.signal.sent))

    def test_ingest_drains_multiple_pages(self):
        import unittest.mock as mock

        import issuefleet.fleet_manager as fmmod

        fm = self._fm()
        fm.state["signal_cursor"] = "base"
        self.signal.user_says("base", id="base")
        self.signal.user_says("goal: one", id="g1")
        self.signal.user_says("goal: two", id="g2")
        self.signal.user_says("goal: three", id="g3")
        with mock.patch.object(fmmod, "_PAGE", 2):  # force >1 page
            fm.tick()
        self.assertEqual(len(self.tracker.created), 3)  # all pages drained
        # cursor fully advanced (past the interleaved bot confirmations too)
        self.assertEqual(fm.state["signal_cursor"], self.signal.log[-1].id)

    def test_report_timestamp_only_advances_on_successful_send(self):
        self.cfg.fleet_manager.report_interval_s = 60
        fm = self._fm(clock=lambda: 100.0)
        self.signal.send_fail = 1  # the report send fails
        fm.tick()
        self.assertIsNone(fm.state["last_report"])  # not advanced
        fm.tick()  # retried, succeeds
        self.assertEqual(fm.state["last_report"], 100.0)
        self.assertTrue(any("Fleet status" in s for s in self.signal.sent))

    def test_board_summary_reads_top_level_board(self):
        self.tracker.add_issue(
            make_issue(key="FUG-200", id="g200", title="ship dashboards", project_id="Fleet")
        )
        fm = self._fm()
        self.assertIn("FUG-200", fm._board_summary())


if __name__ == "__main__":
    unittest.main()
