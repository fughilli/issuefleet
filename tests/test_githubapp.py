"""GitHub App auth: JWT construction (with a real openssl sign+verify
roundtrip), installation-token caching, and forge integration — offline."""

import base64
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from issuefleet import config, creds
from issuefleet.github import GithubForge
from issuefleet.githubapp import (
    AppTokenProvider,
    GithubAppError,
    b64url,
    build_jwt,
    build_manifest,
    convert_manifest_code,
    manifest_form_html,
    openssl_sign,
    run_manifest_flow,
)

MINIMAL = {
    "projects": [
        {
            "name": "x",
            "linear_project": "X",
            "repo": "/tmp/x",
            "claim": {"strategy": "label", "value": "agent"},
        }
    ]
}


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class JwtTest(unittest.TestCase):
    def test_structure_and_claims(self):
        jwt = build_jwt("12345", Path("/nonexistent"), now=1_000_000,
                       signer=lambda key, data: b"SIG")
        header_b64, payload_b64, sig_b64 = jwt.split(".")
        self.assertEqual(json.loads(unb64url(header_b64)), {"alg": "RS256", "typ": "JWT"})
        payload = json.loads(unb64url(payload_b64))
        self.assertEqual(payload["iss"], "12345")
        self.assertEqual(payload["iat"], 1_000_000 - 60)  # clock-skew grace
        self.assertLessEqual(payload["exp"] - payload["iat"], 601)  # <= 10 min cap
        self.assertEqual(unb64url(sig_b64), b"SIG")

    def test_signer_receives_signing_input(self):
        seen = {}

        def signer(key, data):
            seen["data"] = data
            return b"s"

        jwt = build_jwt("1", Path("/k"), now=5, signer=signer)
        header_b64, payload_b64, _ = jwt.split(".")
        self.assertEqual(seen["data"], f"{header_b64}.{payload_b64}".encode())

    @unittest.skipIf(shutil.which("openssl") is None, "openssl not available")
    def test_real_openssl_sign_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "app.pem"
            pub = Path(tmp) / "app.pub"
            subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                           check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
                           check=True, capture_output=True)
            data = b"header.payload"
            sig = openssl_sign(key, data)
            sig_file = Path(tmp) / "sig"
            sig_file.write_bytes(sig)
            data_file = Path(tmp) / "data"
            data_file.write_bytes(data)
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", str(pub),
                 "-signature", str(sig_file), str(data_file)],
                capture_output=True, text=True,
            )
            self.assertIn("Verified OK", proc.stdout)

    def test_openssl_failure_is_actionable(self):
        with self.assertRaisesRegex(GithubAppError, "openssl signing"):
            openssl_sign(Path("/definitely/not/a/key.pem"), b"x")

    def test_b64url_no_padding(self):
        self.assertNotIn("=", b64url(b"\x00\x01\x02\x03"))


class FakeAppTransport:
    def __init__(self):
        self.calls = []
        self.token_serial = 0

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "headers": headers})
        if url.endswith("/app/installations"):
            return [
                {"id": 111, "account": {"login": "Fughilli"}},
                {"id": 222, "account": {"login": "other-org"}},
            ]
        if "/access_tokens" in url:
            self.token_serial += 1
            return {"token": f"ghs_tok{self.token_serial}"}
        if url.endswith("/app"):
            return {"slug": "issuefleet"}
        raise AssertionError(url)


class AppTokenProviderTest(unittest.TestCase):
    def make(self, clock_value=None, installation_id=None):
        self.transport = FakeAppTransport()
        self.now = [1_000_000.0 if clock_value is None else clock_value]
        return AppTokenProvider(
            "12345",
            Path("/k.pem"),
            installation_id=installation_id,
            transport=self.transport,
            clock=lambda: self.now[0],
            signer=lambda key, data: b"SIG",
        )

    def test_owner_mapping_case_insensitive(self):
        p = self.make()
        self.assertEqual(p.installation_for_owner("fughilli"), 111)
        self.assertEqual(p.installation_for_owner("OTHER-ORG"), 222)
        with self.assertRaisesRegex(GithubAppError, "not installed"):
            p.installation_for_owner("stranger")

    def test_token_cached_until_margin_then_refreshed(self):
        p = self.make()
        t1 = p.token_for_owner("fughilli")
        t2 = p.token_for_owner("fughilli")
        self.assertEqual(t1, t2)  # cached
        self.now[0] += 3600 - 60  # inside the 5-min refresh margin
        t3 = p.token_for_owner("fughilli")
        self.assertNotEqual(t1, t3)  # refreshed

    def test_tokens_are_per_installation(self):
        p = self.make()
        self.assertNotEqual(p.token_for_owner("fughilli"), p.token_for_owner("other-org"))

    def test_pinned_installation_skips_discovery(self):
        p = self.make(installation_id=999)
        p.token_for_owner("anyone")
        self.assertFalse(any(c["url"].endswith("/app/installations") for c in self.transport.calls))

    def test_jwt_used_for_app_endpoints(self):
        p = self.make()
        p.token_for_owner("fughilli")
        auth = self.transport.calls[0]["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Bearer eyJ"))  # a JWT, not a token


class ForgeCallableTokenTest(unittest.TestCase):
    def test_forge_calls_token_source_per_request(self):
        tokens = iter(["tok-a", "tok-b"])
        seen = []

        def transport(method, url, headers, payload):
            seen.append(headers["Authorization"])
            return {"number": 1, "html_url": "u", "state": "open", "merged_at": None,
                    "head": {"ref": "b"}, "base": {"ref": "main"}}

        forge = GithubForge(lambda: next(tokens), "o/r", transport=transport)
        forge.get_pr(1)
        forge.get_pr(1)
        self.assertEqual(seen, ["Bearer tok-a", "Bearer tok-b"])


class ManifestFlowTest(unittest.TestCase):
    def test_manifest_contents(self):
        m = build_manifest("issuefleet", "http://localhost:9780/callback",
                           "https://tunnel.example/webhook/github")
        # issues:read is load-bearing: the issue_comment event AND the
        # PR-feedback poll both require it (GitHub rejected the manifest
        # without it: "Default events are not supported by permissions").
        self.assertEqual(
            m["default_permissions"],
            {"contents": "write", "pull_requests": "write", "issues": "read"},
        )
        self.assertIn("pull_request_review_comment", m["default_events"])
        self.assertFalse(m["public"])
        self.assertEqual(
            m["hook_attributes"], {"url": "https://tunnel.example/webhook/github", "active": True}
        )

    def test_manifest_without_webhook_omits_hook_and_events(self):
        # GitHub rejects events without an active hook ("Hook url cannot be
        # blank"), so a webhook-less app must carry neither.
        m = build_manifest("x", "r", None)
        self.assertNotIn("hook_attributes", m)
        self.assertNotIn("default_events", m)

    def test_form_html_escapes_and_targets(self):
        m = build_manifest('issue"fleet', "http://localhost:9780/callback", None)
        html = manifest_form_html(m, "https://github.com/settings/apps/new")
        self.assertIn('action="https://github.com/settings/apps/new"', html)
        self.assertIn("&quot;", html)
        self.assertNotIn('value="{"', html)  # quotes inside the JSON are escaped

    def test_convert_code(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url))
            return {"id": 99, "slug": "issuefleet", "pem": "PEMPEM", "webhook_secret": "whs"}

        app = convert_manifest_code("c0de", transport=transport)
        self.assertEqual(app["id"], 99)
        self.assertEqual(calls[0], ("POST", "https://api.github.com/app-manifests/c0de/conversions"))
        with self.assertRaisesRegex(GithubAppError, "no private key"):
            convert_manifest_code("x", transport=lambda *a: {"message": "Not Found"})

    def test_flow_serves_form_then_captures_code(self):
        import threading
        import urllib.request

        result = {}

        def run():
            result["code"] = run_manifest_flow(9788, "<html>FORM</html>", timeout_s=10)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.2)
        with urllib.request.urlopen("http://127.0.0.1:9788/", timeout=5) as resp:
            self.assertIn(b"FORM", resp.read())
        with urllib.request.urlopen("http://127.0.0.1:9788/callback?code=abc123", timeout=5) as resp:
            self.assertIn(b"created", resp.read())
        t.join(timeout=5)
        self.assertEqual(result.get("code"), "abc123")


class PushSpecTest(unittest.TestCase):
    def test_push_spec_is_https_with_basic_header(self):
        import base64

        forge = GithubForge("ghs_tok1", "fughilli/issuefleet", transport=lambda *a: {})
        url, header = forge.push_spec()
        self.assertEqual(url, "https://github.com/fughilli/issuefleet.git")
        scheme, b64 = header.split(" ")
        self.assertEqual(scheme, "basic")
        self.assertEqual(base64.b64decode(b64).decode(), "x-access-token:ghs_tok1")

    def test_push_spec_mints_fresh_callable_tokens(self):
        tokens = iter(["t1", "t2"])
        forge = GithubForge(lambda: next(tokens), "o/r", transport=lambda *a: {})
        self.assertNotEqual(forge.push_spec()[1], forge.push_spec()[1])


class GithubAuthModeTest(unittest.TestCase):
    def test_auto_prefers_app_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.parse(MINIMAL)
            self.assertEqual(creds.github_auth_mode(cfg), "token")  # nothing configured
            cfg.github_app_id = "123"
            self.assertEqual(creds.github_auth_mode(cfg), "token")  # key file missing
            key = Path(tmp) / "app.pem"
            key.write_text("fake")
            cfg.github_app_key_file = key
            self.assertEqual(creds.github_auth_mode(cfg), "app")
            cfg.github_auth = "token"  # explicit override wins
            self.assertEqual(creds.github_auth_mode(cfg), "token")

    def test_config_parses_app_fields(self):
        data = dict(MINIMAL)
        data["credentials"] = {
            "github_auth": "app",
            "github_app_id": 4242,
            "github_app_installation_id": 777,
        }
        cfg = config.parse(data)
        self.assertEqual(cfg.github_auth, "app")
        self.assertEqual(cfg.github_app_id, "4242")  # normalized to str
        self.assertEqual(cfg.github_app_installation_id, 777)
        data["credentials"] = {"github_auth": "vibes"}
        with self.assertRaises(config.ConfigError):
            config.parse(data)


if __name__ == "__main__":
    unittest.main()
