import unittest

from issuefleet.sigbot import SigbotClient, SignalError, SignalMessage


class FakeServiceClient:
    """Stands in for sigbot_client.ServiceClient."""

    def __init__(self):
        self.sent: list[tuple[str, bool]] = []
        self.log: list[dict] = []
        self.reacted: list[tuple] = []
        self.raise_on: dict[str, Exception] = {}  # method name -> exception

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def service(self):
        self._maybe_raise("service")
        return {"name": "fleet", "label": "fleet", "group_name": "Fleet Ops"}

    def send(self, text, prefix=True):
        self._maybe_raise("send")
        self.sent.append((text, prefix))

    def react(self, message_id, emoji):
        self._maybe_raise("react")
        self.reacted.append((message_id, emoji))

    def messages(self, after_id=None, limit=50):
        self._maybe_raise("messages")
        out = self.log
        if after_id is not None:
            idx = next((i for i, m in enumerate(out) if m["id"] == after_id), None)
            out = out[idx + 1 :] if idx is not None else out
        return out[-limit:]


class SigbotApiErrorLike(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


class SignalMessageParseTest(unittest.TestCase):
    def test_reads_id_text_author_from_common_keys(self):
        m = SignalMessage.from_api({"id": 7, "body": "hi", "sender": "kevin"})
        self.assertEqual((m.id, m.text, m.author), (7, "hi", "kevin"))

    def test_prefers_text_over_body_and_defaults_author(self):
        m = SignalMessage.from_api({"id": 1, "text": "t", "body": "b"})
        self.assertEqual(m.text, "t")
        self.assertEqual(m.author, "unknown")

    def test_keeps_raw(self):
        d = {"id": 1, "text": "t", "extra": 9}
        self.assertEqual(SignalMessage.from_api(d).raw, d)


class SigbotClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServiceClient()
        self.client = SigbotClient("http://h:8100", "sb_x", service_client=self.fake)

    def test_service_passthrough(self):
        self.assertEqual(self.client.service()["group_name"], "Fleet Ops")

    def test_send_passes_prefix(self):
        self.client.send("hello")
        self.client.send("raw", prefix=False)
        self.assertEqual(self.fake.sent, [("hello", True), ("raw", False)])

    def test_messages_normalized_and_paged(self):
        self.fake.log = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c"}]
        msgs = self.client.messages()
        self.assertEqual([m.id for m in msgs], [1, 2, 3])
        self.assertIsInstance(msgs[0], SignalMessage)
        newer = self.client.messages(after_id=2)
        self.assertEqual([m.id for m in newer], [3])

    def test_messages_handles_empty(self):
        self.assertEqual(self.client.messages(), [])

    def test_api_error_normalized_with_status(self):
        self.fake.raise_on["send"] = SigbotApiErrorLike(401, "revoked key")
        with self.assertRaises(SignalError) as cm:
            self.client.send("x")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("revoked key", cm.exception.message)

    def test_generic_error_normalized_without_status(self):
        self.fake.raise_on["service"] = RuntimeError("boom")
        with self.assertRaises(SignalError) as cm:
            self.client.service()
        self.assertIsNone(cm.exception.status)
        self.assertIn("boom", cm.exception.message)

    def test_missing_package_gives_actionable_error(self):
        # No service_client injected and sigbot_client is not installed in the
        # test env, so the lazy import fails with a helpful SignalError.
        bare = SigbotClient("http://h:8100", "sb_x")
        with self.assertRaises(SignalError) as cm:
            bare.service()
        self.assertIn("sigbot-client", cm.exception.message)


class ReactionTest(unittest.TestCase):
    """react() is a courtesy: it must never raise, because a failed
    acknowledgement must not cost a message or fail a tick."""

    def _client(self, svc):
        return SigbotClient("http://x", "k", service_client=svc)

    def test_react_passes_through(self):
        svc = FakeServiceClient()
        assert self._client(svc).react(7, "\N{EYES}") is True
        self.assertEqual(svc.reacted, [(7, "\N{EYES}")])

    def test_old_client_without_react_returns_false(self):
        class Old:
            def service(self):
                return {}

        self.assertIs(self._client(Old()).react(7, "\N{EYES}"), False)

    def test_404_from_an_old_service_returns_false(self):
        svc = FakeServiceClient()
        svc.raise_on["react"] = SigbotApiErrorLike(404, "not found")
        self.assertIs(self._client(svc).react(7, "\N{EYES}"), False)

    def test_any_other_failure_also_returns_false(self):
        svc = FakeServiceClient()
        svc.raise_on["react"] = RuntimeError("connection reset")
        self.assertIs(self._client(svc).react(7, "\N{EYES}"), False)

    def test_the_unsupported_warning_is_logged_once(self):
        class Old:
            def service(self):
                return {}

        c = self._client(Old())
        with self.assertLogs("issuefleet.sigbot", level="INFO") as cm:
            c.react(1, "x")
            c.react(2, "x")
            c.react(3, "x")
        self.assertEqual(len([r for r in cm.records if "unavailable" in r.getMessage()]), 1)


if __name__ == "__main__":
    unittest.main()
