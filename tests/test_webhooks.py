"""Webhook listener: signature verification and end-to-end HTTP behavior
against a real server on an ephemeral port (offline: loopback only)."""

import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from issuefleet import webhooks
from issuefleet.webhooks import (
    SessionEvent,
    WebhookServer,
    linear_timestamp_fresh,
    parse_session_event,
    verify_github_signature,
    verify_linear_signature,
)


def gh_sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def lin_sig(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SignatureTest(unittest.TestCase):
    def test_github_signature(self):
        body = b'{"action":"submitted"}'
        self.assertTrue(verify_github_signature("s3cret", body, gh_sig("s3cret", body)))
        self.assertFalse(verify_github_signature("s3cret", body, gh_sig("wrong", body)))
        self.assertFalse(verify_github_signature("s3cret", body, "sha256=deadbeef"))
        self.assertFalse(verify_github_signature("s3cret", body, None))
        self.assertFalse(verify_github_signature("s3cret", body, "nosha_prefix"))

    def test_linear_signature(self):
        body = b'{"type":"Comment"}'
        self.assertTrue(verify_linear_signature("s3cret", body, lin_sig("s3cret", body)))
        self.assertFalse(verify_linear_signature("s3cret", body, lin_sig("wrong", body)))
        self.assertFalse(verify_linear_signature("s3cret", body, None))

    def test_linear_timestamp_replay_guard(self):
        now = int(time.time() * 1000)
        self.assertTrue(linear_timestamp_fresh({"webhookTimestamp": now - 5_000}, now_ms=now))
        self.assertFalse(linear_timestamp_fresh({"webhookTimestamp": now - 120_000}, now_ms=now))
        self.assertFalse(linear_timestamp_fresh({}, now_ms=now))


class ParseSessionEventTest(unittest.TestCase):
    def test_created(self):
        evt = parse_session_event(
            {
                "type": "AgentSessionEvent",
                "action": "created",
                "agentSession": {"id": "sess-1", "issue": {"id": "i1", "identifier": "FUG-1"}},
                "promptContext": "Issue FUG-1: fix it",
            }
        )
        self.assertEqual(
            (evt.action, evt.session_id, evt.issue_id, evt.issue_key),
            ("created", "sess-1", "i1", "FUG-1"),
        )
        self.assertIn("fix it", evt.body)

    def test_prompted(self):
        evt = parse_session_event(
            {
                "type": "AgentSessionEvent",
                "action": "prompted",
                "actor": {"id": "u1", "type": "user"},
                "agentSession": {"id": "sess-1", "issue": {"id": "i1", "identifier": "FUG-1"}},
                "agentActivity": {"body": "please also update the docs",
                                  "content": {"type": "prompt"}},
            }
        )
        self.assertEqual(evt.action, "prompted")
        self.assertEqual(evt.body, "please also update the docs")
        self.assertEqual(evt.activity_type, "prompt")
        self.assertEqual(evt.actor_type, "user")

    def test_prompted_echo_fields_extracted(self):
        # An echo of our own activity: type from content, body may live there.
        evt = parse_session_event(
            {
                "type": "AgentSessionEvent",
                "action": "prompted",
                "actor": {"type": "application"},
                "agentSession": {"id": "sess-1", "issue": {"id": "i1", "identifier": "FUG-1"}},
                "agentActivity": {"content": {"type": "thought", "body": "formed a plan"}},
            }
        )
        self.assertEqual(evt.activity_type, "thought")
        self.assertEqual(evt.actor_type, "application")
        self.assertEqual(evt.body, "formed a plan")

    def test_non_session_and_malformed(self):
        self.assertIsNone(parse_session_event({"type": "Comment", "action": "create"}))
        self.assertIsNone(
            parse_session_event({"type": "AgentSessionEvent", "action": "created"})
        )  # no session id


class WebhookServerTest(unittest.TestCase):
    def setUp(self):
        self.woken = threading.Event()
        self.sessions: list[SessionEvent] = []
        self.server = WebhookServer(
            bind="127.0.0.1",
            port=0,  # ephemeral
            wake=self.woken.set,
            on_session=self.sessions.append,
            github_secret="gh-secret",
            linear_secret="lin-secret",
        ).start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()

    def post(self, path, body: bytes, headers: dict) -> int:
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_github_valid_wakes(self):
        body = b'{"action":"submitted"}'
        code = self.post(
            "/webhook/github",
            body,
            {"X-Hub-Signature-256": gh_sig("gh-secret", body), "X-GitHub-Event": "pull_request_review"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(self.woken.wait(2))

    def test_github_bad_signature_rejected(self):
        body = b"{}"
        code = self.post("/webhook/github", body, {"X-Hub-Signature-256": gh_sig("wrong", body)})
        self.assertEqual(code, 401)
        self.assertFalse(self.woken.is_set())

    def test_linear_session_event_queued_and_wakes(self):
        payload = {
            "type": "AgentSessionEvent",
            "action": "created",
            "webhookTimestamp": int(time.time() * 1000),
            "agentSession": {"id": "sess-9", "issue": {"id": "i9", "identifier": "FUG-9"}},
            "promptContext": "ctx",
        }
        body = json.dumps(payload).encode()
        code = self.post("/webhook/linear", body, {"Linear-Signature": lin_sig("lin-secret", body)})
        self.assertEqual(code, 200)
        self.assertTrue(self.woken.wait(2))
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sessions[0].session_id, "sess-9")

    def test_linear_stale_timestamp_rejected(self):
        payload = {"type": "Comment", "action": "create", "webhookTimestamp": 1000}
        body = json.dumps(payload).encode()
        code = self.post("/webhook/linear", body, {"Linear-Signature": lin_sig("lin-secret", body)})
        self.assertEqual(code, 401)
        self.assertFalse(self.woken.is_set())

    def test_plain_linear_event_wakes_without_session(self):
        payload = {
            "type": "Comment",
            "action": "create",
            "webhookTimestamp": int(time.time() * 1000),
        }
        body = json.dumps(payload).encode()
        self.assertEqual(
            self.post("/webhook/linear", body, {"Linear-Signature": lin_sig("lin-secret", body)}),
            200,
        )
        self.assertTrue(self.woken.wait(2))
        self.assertEqual(self.sessions, [])

    def test_unknown_path_404(self):
        self.assertEqual(self.post("/nope", b"", {}), 404)

    def test_health_probe(self):
        with urllib.request.urlopen(self.base + "/webhook/github", timeout=5) as resp:
            # GET is the tunnel health probe
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
