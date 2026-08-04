import unittest

from issuefleet.httpx import ApiError
from issuefleet.signal_bridge import (
    SignalClient,
    SignalError,
    UrllibSignalClient,
    connect,
)


class RecordingTransport:
    """Captures each (method, url, headers, payload) and returns queued
    responses so tests can assert exact requests offline."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])
        self.raise_next = None  # an ApiError to raise on the next call

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if self.raise_next is not None:
            err, self.raise_next = self.raise_next, None
            raise err
        return self._responses.pop(0) if self._responses else {}


class TestUrllibSignalClient(unittest.TestCase):
    def setUp(self):
        self.t = RecordingTransport()
        self.c = UrllibSignalClient("http://host:8100/", "sb_key", transport=self.t)

    def test_conforms_to_protocol(self):
        self.assertIsInstance(self.c, SignalClient)

    def test_service_get_with_bearer(self):
        self.t._responses = [{"name": "fleet", "label": "F", "group_name": "g"}]
        out = self.c.service()
        self.assertEqual(out["group_name"], "g")
        method, url, headers, payload = self.t.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://host:8100/service")  # trailing slash trimmed
        self.assertEqual(headers["Authorization"], "Bearer sb_key")
        self.assertIsNone(payload)

    def test_send_posts_text_and_prefix(self):
        self.c.send("hello", prefix=False)
        method, url, _headers, payload = self.t.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://host:8100/messages")
        self.assertEqual(payload, {"text": "hello", "prefix": False})

    def test_send_defaults_prefix_true(self):
        self.c.send("hi")
        self.assertEqual(self.t.calls[0][3], {"text": "hi", "prefix": True})

    def test_messages_limit_and_after_id(self):
        self.t._responses = [[{"id": 5}, {"id": 6}]]
        out = self.c.messages(limit=10, after_id=4)
        self.assertEqual([m["id"] for m in out], [5, 6])
        method, url, _h, payload = self.t.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://host:8100/messages?limit=10&after_id=4")
        self.assertIsNone(payload)

    def test_messages_no_after_id_omits_param(self):
        self.t._responses = [[]]
        self.c.messages(limit=50)
        self.assertEqual(self.t.calls[0][1], "http://host:8100/messages?limit=50")

    def test_messages_accepts_enveloped_response(self):
        self.t._responses = [{"messages": [{"id": 1}]}]
        out = self.c.messages()
        self.assertEqual(out, [{"id": 1}])

    def test_api_error_becomes_signal_error(self):
        self.t.raise_next = ApiError(401, "http://host:8100/service", "revoked key")
        with self.assertRaises(SignalError) as ctx:
            self.c.service()
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("401", str(ctx.exception))


class TestConnect(unittest.TestCase):
    def test_falls_back_to_urllib_when_package_absent(self):
        # sigbot_client isn't installed in the test env, so connect() must not
        # raise — it logs and returns the stdlib client.
        client = connect("http://host:8100", "sb_key")
        self.assertIsInstance(client, UrllibSignalClient)

    def test_prefer_package_false_forces_urllib(self):
        client = connect("http://host:8100", "sb_key", prefer_package=False)
        self.assertIsInstance(client, UrllibSignalClient)


if __name__ == "__main__":
    unittest.main()
