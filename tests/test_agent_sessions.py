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

    def test_prompt_routed_to_worker_inbox(self):
        self.add_issue()
        self.rec.enqueue_session(created())
        self.rec.tick()
        self.rec.enqueue_session(prompted())
        self.rec.tick()
        replies = [m for m in self.mailbox().pending_inbox() if m.kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertIn("update the docs", replies[0].payload["text"])

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
