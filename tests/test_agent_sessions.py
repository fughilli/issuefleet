"""Linear agent-session lifecycle in the reconcile loop, plus Bearer auth
and the OAuth flow helpers — all offline."""

import tempfile
import unittest
from pathlib import Path

from fakes import FakeForge, FakeGit, FakeRunner, FakeTracker, make_issue

from issuefleet import config, oauth
from issuefleet.linear import LinearClient
from issuefleet.mailbox import Mailbox
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry
from issuefleet.webhooks import SessionEvent


def created(n=1, session="sess-1"):
    return SessionEvent(
        action="created", session_id=session, issue_id=f"issue-{n}", issue_key=f"FUG-{n}", body="ctx"
    )


def prompted(n=1, session="sess-1", text="also update the docs", **kw):
    return SessionEvent(
        action="prompted", session_id=session, issue_id=f"issue-{n}", issue_key=f"FUG-{n}",
        body=text, **kw
    )


class AgentSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.parse(
            {
                "daemon": {
                    "state_dir": str(root / "state"),
                    "worktree_root": str(root / "worktrees"),
                    "max_workers": 1,
                },
                "projects": [
                    {
                        "name": "splanc",
                        "linear_project": "proj-splanc",
                        "repo": str(root / "repo"),
                        "claim": {"strategy": "label", "value": "agent"},
                    }
                ],
            }
        )
        self.registry = Registry(self.cfg.state_dir)
        self.tracker = FakeTracker()
        self.forge = FakeForge()
        self.git = FakeGit(root)
        self.runner = FakeRunner()
        self.rec = Reconciler(
            self.cfg, self.registry, self.tracker, {"splanc": self.forge}, self.git, self.runner
        )

    def tearDown(self):
        self.tmp.cleanup()

    def add_issue(self, n=1, **kw):
        # No claim label: session claims must work regardless of the rule.
        kw.setdefault("labels", [])
        kw.setdefault("project_id", "proj-splanc")
        return self.tracker.add_issue(make_issue(n, **kw))

    def mailbox(self, n=1):
        return Mailbox(Path(self.registry.get(f"issue-{n}").worktree) / ".agent" / "mailbox")

    def test_delegation_claims_despite_claim_rule(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        w = self.registry.get("issue-1")
        self.assertIsNotNone(w)
        self.assertEqual(w.claim_origin, "session")
        self.assertEqual(w.agent_session_id, "sess-1")
        # Claim announcement went to the session, not the issue thread.
        kinds = [c["type"] for _, c in self.tracker.activities]
        self.assertIn("thought", kinds)
        self.assertFalse(any("Claimed" in b for _, b in self.tracker.posted))
        # No label to remove -> the label rule must NOT unclaim it.
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-1"))

    def test_closure_still_unclaims_session_worker(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.tracker.issues["issue-1"].state_type = "canceled"
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))
        # The session got a closing activity so its UI doesn't hang.
        finals = [c for _, c in self.tracker.activities if "wound down" in c.get("body", "").lower()]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["type"], "error")  # unmerged wind-down

    def test_merge_teardown_closes_session_with_response(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.mailbox().put_outbox("ready", {"title": "T", "body": "B"})
        self.rec.tick()
        self.forge.merge(self.registry.get("issue-1").pr_number)
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))
        finals = [c for _, c in self.tracker.activities
                  if c["type"] == "response" and "wound down" in c["body"].lower()]
        self.assertEqual(len(finals), 1)

    def test_prompt_routed_to_worker_inbox(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.rec.enqueue_session(prompted())
        self.rec.tick()
        replies = [m for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertIn("update the docs", replies[0].payload["text"])

    def test_prompt_is_acknowledged_with_eyes(self):
        # 👀: routing a genuine user prompt to the worker emits an immediate
        # "seen it" thought into the session, before any worker turn runs.
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.rec.enqueue_session(prompted())
        self.rec.tick()
        eyes = [c for _, c in self.tracker.activities
                if c["type"] == "thought" and c["body"].startswith("👀")]
        self.assertEqual(len(eyes), 1)

    def test_polled_comment_acknowledged_once_per_batch(self):
        # 👀 also covers the comment-poll path, once per ingest batch (not once
        # per comment), and lands in the session when one is bound.
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.tracker.human_comment("issue-1", "first thought")
        self.tracker.human_comment("issue-1", "and another")
        self.rec.tick()
        eyes = [c for _, c in self.tracker.activities
                if c["type"] == "thought" and c["body"].startswith("👀")]
        self.assertEqual(len(eyes), 1)

    def test_ack_outbox_relays_to_session_only(self):
        # ⚙️/✅ acks render in the session; in comment mode they're dropped so
        # they never spam the issue thread. Either way the message is archived.
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.mailbox().put_outbox("ack", {"text": "⚙️ On it."})
        self.rec.tick()
        self.assertIn(("sess-1", {"type": "thought", "body": "⚙️ On it."}),
                      self.tracker.activities)
        self.assertEqual(self.tracker.posted, [])
        self.assertEqual(self.mailbox().pending_outbox(), [])

        # Unbind the session: the next ack is dropped, not posted.
        self.registry.get("issue-1").agent_session_id = None
        self.registry.save()
        self.mailbox().put_outbox("ack", {"text": "✅ Done for now."})
        self.rec.tick()
        self.assertEqual(self.tracker.posted, [])
        self.assertEqual(self.mailbox().pending_outbox(), [])

    def test_echoed_agent_activities_do_not_wake_the_worker(self):
        # Live-observed loop (2026-07-30): our own thought/response activity
        # emissions come back as `prompted` webhooks; routing them to the
        # inbox re-wakes the agent forever. Only genuine user prompts pass.
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        for evt in (
            prompted(activity_type="thought"),  # our status relay, echoed
            prompted(activity_type="response"),
            prompted(text="anything", actor_type="application"),
        ):
            self.rec.enqueue_session(evt)
        self.rec.tick()
        self.assertEqual(self.mailbox().pending_inbox(), [])
        # A genuine user prompt still gets through...
        self.rec.enqueue_session(prompted(activity_type="prompt", text="use approach B"))
        # ...as does one where Linear omits the fields (can't be recovered
        # if dropped; the turn loop's ready-restore bounds any residual echo).
        self.rec.enqueue_session(prompted(text="bare prompt"))
        self.rec.tick()
        texts = [m.payload["text"] for m in self.mailbox().pending_inbox()]
        self.assertEqual(texts, ["use approach B", "bare prompt"])

    def test_relays_become_activities_not_comments(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.mailbox().put_outbox("status", {"text": "formed a plan"})
        self.mailbox().put_outbox("question", {"text": "which schema?"})
        self.rec.tick()
        contents = [c for _, c in self.tracker.activities]
        self.assertIn({"type": "thought", "body": "formed a plan"}, contents)
        self.assertIn({"type": "elicitation", "body": "which schema?"}, contents)
        self.assertEqual(self.tracker.posted, [])  # nothing on the thread
        self.assertEqual(self.mailbox().pending_outbox(), [])  # acked

    def test_ready_emits_response_with_pr_link(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.mailbox().put_outbox("ready", {"title": "Fix", "body": "done"})
        self.rec.tick()
        responses = [c for _, c in self.tracker.activities if c["type"] == "response"]
        self.assertEqual(len(responses), 1)
        self.assertIn("Pull request ready", responses[0]["body"])
        self.assertEqual(len(self.forge.opened), 1)

    def test_session_for_unconfigured_project_rejected_with_error_activity(self):
        self.add_issue(project_id="proj-other")
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))
        errors = [c for _, c in self.tracker.activities if c["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.rec.pending_session_claims, {})  # not retried forever

    def test_session_claim_waits_when_fleet_full(self):
        self.tracker.add_issue(make_issue(2, project_id="proj-splanc"))  # labeled: rule-claimed
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-2"))  # occupies the 1 slot
        self.add_issue(1)
        self.rec.enqueue_session(created(1))
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))
        self.assertIn("issue-1", self.rec.pending_session_claims)  # still queued
        self.tracker.issues["issue-2"].labels = []  # un-claim the rule worker
        self.rec.tick()
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-1"))

    def test_agent_strategy_ignores_labels_but_claims_via_sessions(self):
        self.cfg.projects[0].claim = config.ClaimRule("agent", "")
        # Labels mean nothing under the agent strategy...
        self.tracker.add_issue(make_issue(1, labels=["agent"], project_id="proj-splanc"))
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))
        # ...but delegation still claims.
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-1"))
        self.assertEqual(self.registry.get("issue-1").claim_origin, "session")

    def test_agent_strategy_poll_claims_delegated_issues(self):
        # Webhooks down (dead tunnel): delegation is pollable — the fleet
        # must not go deaf. Linear stores delegation in `delegate`, NOT
        # `assignee` (found live: doctor showed '0 assigned' for a
        # UI-delegated issue).
        self.cfg.projects[0].claim = config.ClaimRule("agent", "")
        self.tracker.add_issue(
            make_issue(1, delegate_id=self.tracker.viewer_id, project_id="proj-splanc")
        )
        self.rec.tick()  # no session event ever arrives
        w = self.registry.get("issue-1")
        self.assertIsNotNone(w)
        self.assertEqual(w.claim_origin, "poll")
        # Revoking the delegation un-claims.
        self.tracker.issues["issue-1"].delegate_id = None
        self.rec.tick()
        self.assertIsNone(self.registry.get("issue-1"))

    def test_agent_strategy_also_accepts_plain_assignment(self):
        self.cfg.projects[0].claim = config.ClaimRule("agent", "")
        self.tracker.add_issue(
            make_issue(2, assignee_id=self.tracker.viewer_id, project_id="proj-splanc")
        )
        self.rec.tick()
        self.assertIsNotNone(self.registry.get("issue-2"))

    def test_poll_claimed_worker_binds_session_by_polling(self):
        # The dead-tunnel case (FUG-28): the `created` webhook never arrives,
        # so the worker is poll-claimed with no session id and would drive the
        # issue over comments while the session view hangs. Discovery via
        # polling must bind the session and switch relays to activities.
        self.tracker.app_identity = True
        self.tracker.add_issue(make_issue(1, project_id="proj-splanc"))  # labeled -> poll-claimed
        self.tracker.sessions["issue-1"] = "sess-poll"
        self.rec.tick()  # claims via poll (no session event)
        w = self.registry.get("issue-1")
        self.assertEqual(w.claim_origin, "poll")
        self.rec.tick()  # servicing discovers and binds the session
        w = self.registry.get("issue-1")
        self.assertEqual(w.agent_session_id, "sess-poll")
        # A catch-up thought went into the discovered session...
        self.assertTrue(any(sid == "sess-poll" and c["type"] == "thought"
                            for sid, c in self.tracker.activities))
        # ...and subsequent status relays now stream to the session, not comments.
        self.mailbox().put_outbox("status", {"text": "made progress"})
        self.rec.tick()
        self.assertIn(("sess-poll", {"type": "thought", "body": "made progress"}),
                      self.tracker.activities)

    def test_session_discovery_skipped_for_personal_key(self):
        # A personal-key tracker owns no sessions: discovery must never fire,
        # so a poll-claimed worker doesn't even count an attempt.
        self.tracker.add_issue(make_issue(1, project_id="proj-splanc"))
        self.rec.tick()  # poll-claim; app_identity False
        self.rec.tick()
        w = self.registry.get("issue-1")
        self.assertIsNone(w.agent_session_id)
        self.assertEqual(w.session_lookup_attempts, 0)

    def test_session_discovery_bounded_when_no_session_exists(self):
        # App identity but the issue genuinely has no session (odd race / a
        # manual assignment): probing must stop after the cap, not hit Linear
        # every tick forever.
        from issuefleet.reconcile import _SESSION_LOOKUP_MAX

        self.tracker.app_identity = True
        self.tracker.add_issue(make_issue(1, project_id="proj-splanc"))  # no sessions[] entry
        self.rec.tick()  # poll-claim
        for _ in range(_SESSION_LOOKUP_MAX + 3):
            self.rec.tick()
        w = self.registry.get("issue-1")
        self.assertIsNone(w.agent_session_id)
        self.assertEqual(w.session_lookup_attempts, _SESSION_LOOKUP_MAX)

    def test_session_attaches_to_already_claimed_worker(self):
        self.tracker.add_issue(make_issue(1, project_id="proj-splanc"))  # labeled
        self.rec.tick()
        self.assertEqual(self.registry.get("issue-1").agent_session_id, None)
        self.rec.enqueue_session(created())
        self.rec.tick()
        w = self.registry.get("issue-1")
        self.assertEqual(w.agent_session_id, "sess-1")
        self.assertEqual(w.claim_origin, "poll")  # origin (and unclaim rules) unchanged


class LinearAuthTest(unittest.TestCase):
    def test_auto_mode_infers_from_prefix(self):
        self.assertEqual(LinearClient("lin_api_abc").auth_header(), "lin_api_abc")
        self.assertEqual(LinearClient("lin_oauth_abc").auth_header(), "Bearer lin_oauth_abc")

    def test_forced_modes(self):
        self.assertEqual(LinearClient("tok", auth="oauth").auth_header(), "Bearer tok")
        self.assertEqual(LinearClient("lin_oauth_x", auth="api_key").auth_header(), "lin_oauth_x")


class OAuthFlowTest(unittest.TestCase):
    def test_authorize_url_is_agent_install(self):
        url = oauth.build_authorize_url("client-1", "http://localhost:9779/callback")
        self.assertIn("actor=app", url)
        self.assertIn("response_type=code", url)
        self.assertIn("app%3Amentionable", url)
        self.assertIn("app%3Aassignable", url)

    def test_exchange_code_posts_form_and_returns_token(self):
        calls = []

        def fake_post(url, fields):
            calls.append((url, fields))
            return {"access_token": "lin_oauth_tok"}

        tok = oauth.exchange_code("cid", "csec", "the-code", "http://localhost:9779/callback",
                                  post_form=fake_post)
        self.assertEqual(tok, "lin_oauth_tok")
        url, fields = calls[0]
        self.assertEqual(url, oauth.TOKEN_URL)
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code"], "the-code")

    def test_exchange_failure_raises(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.exchange_code("c", "s", "x", "r", post_form=lambda u, f: {"error": "nope"})


if __name__ == "__main__":
    unittest.main()
