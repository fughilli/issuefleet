"""Response metadata remains available without changing JSON-only clients."""

import json
import unittest
from unittest import mock

from issuefleet.httpx import urllib_transport, urllib_transport_with_headers


class Response:
    headers = {"X-Next-Page": "2", "Content-Type": "application/json"}

    def read(self):
        return b'[{"id": 42}]'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TransportResponseTest(unittest.TestCase):
    def test_json_only_transport_still_returns_decoded_body(self):
        with mock.patch("urllib.request.urlopen", return_value=Response()):
            result = urllib_transport("GET", "https://forge.example/api", {}, None)
        self.assertEqual(result, [{"id": 42}])

    def test_header_transport_preserves_body_and_normalizes_header_names(self):
        with mock.patch("urllib.request.urlopen", return_value=Response()) as opened:
            result = urllib_transport_with_headers(
                "POST", "https://forge.example/api", {"PRIVATE-TOKEN": "test-token"}, {"body": "reply"}
            )
        self.assertEqual(result.data, [{"id": 42}])
        self.assertEqual(result.headers["x-next-page"], "2")
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"body": "reply"})
        self.assertNotIn("test-token", repr(result))


if __name__ == "__main__":
    unittest.main()
