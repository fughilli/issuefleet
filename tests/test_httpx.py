"""Response metadata remains available without changing JSON-only clients."""

import json
import unittest
import urllib.error
from unittest import mock

from issuefleet.httpx import ApiError, urllib_transport, urllib_transport_with_headers


class Response:
    headers = {"X-Next-Page": "2", "Content-Type": "application/json"}

    def read(self):
        return b'[{"id": 42}]'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class EmptyResponse(Response):
    def read(self):
        return b""


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


    def test_an_empty_body_decodes_to_an_empty_object(self):
        with mock.patch("urllib.request.urlopen", return_value=EmptyResponse()):
            self.assertEqual(urllib_transport("DELETE", "https://forge.example/x", {}, None), {})

    def test_an_error_status_does_not_put_the_token_in_the_exception(self):
        err = urllib.error.HTTPError("https://forge.example/api", 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ApiError) as caught:
                urllib_transport_with_headers(
                    "GET", "https://forge.example/api", {"PRIVATE-TOKEN": "test-token"}, None
                )
        self.assertNotIn("test-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
