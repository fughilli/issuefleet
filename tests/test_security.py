"""Unit tests for the credential-scanning security gate: each detector family,
redaction, the file-level rule, the size cap, and the build_gate factory /
Claude deep-scan merge."""

import unittest

from issuefleet.security import (
    MAX_DIFF_BYTES,
    ClaudeSecurityGate,
    Finding,
    NullGate,
    RegexSecretScanner,
    Verdict,
    build_gate,
)


def _diff(path: str, *added_lines: str) -> str:
    """A minimal unified diff (one hunk of added lines) for `path`."""
    body = "".join(f"+{ln}\n" for ln in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{body}"
    )


class RegexScannerTest(unittest.TestCase):
    def setUp(self):
        self.s = RegexSecretScanner()

    def _flag(self, line: str, path: str = "app/config.py") -> Verdict:
        return self.s.scan(_diff(path, line))

    # -- each credential family fires -------------------------------------

    def test_detects_aws_access_key(self):
        v = self._flag("aws_key = 'AKIAIOSFODNN7EXAMPLE'")
        self.assertFalse(v.ok)
        self.assertTrue(any("AWS" in f.rule for f in v.findings))

    def test_detects_github_token(self):
        v = self._flag("token = 'ghp_" + "a" * 36 + "'")
        self.assertFalse(v.ok)

    def test_detects_github_fine_grained_pat(self):
        v = self._flag("t = 'github_pat_" + "A1b2" * 20 + "'")
        self.assertFalse(v.ok)

    def test_detects_anthropic_key(self):
        v = self._flag("os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-" + "x" * 30 + "'")
        self.assertFalse(v.ok)

    def test_detects_linear_key(self):
        v = self._flag("lin = 'lin_api_" + "9" * 40 + "'")
        self.assertFalse(v.ok)

    def test_detects_slack_token(self):
        v = self._flag("hook = 'xoxb-123456789012-abcdefghijkl'")
        self.assertFalse(v.ok)

    def test_detects_google_api_key(self):
        v = self._flag("g = 'AIza" + "B" * 35 + "'")
        self.assertFalse(v.ok)

    def test_detects_tailscale_key(self):
        v = self._flag("TS_AUTHKEY=tskey-auth-abcdefghij-1234567890")
        self.assertFalse(v.ok)

    def test_detects_private_key_block(self):
        v = self._flag("-----BEGIN OPENSSH PRIVATE KEY-----")
        self.assertFalse(v.ok)

    def test_detects_generic_secret_assignment(self):
        v = self._flag('password = "s3cr3t-hunter2-longenough-value"')
        self.assertFalse(v.ok)

    # -- file-level rule ---------------------------------------------------

    def test_flags_added_env_file(self):
        v = self.s.scan(_diff("config/.env", "NOT_A_SECRET=hello"))
        self.assertFalse(v.ok)
        self.assertTrue(any("sensitive file" in f.rule for f in v.findings))

    def test_flags_private_key_file(self):
        v = self.s.scan(_diff("deploy/id_ed25519", "x"))
        self.assertFalse(v.ok)

    def test_public_key_file_is_exempt(self):
        v = self.s.scan(_diff("deploy/id_ed25519.pub", "ssh-ed25519 AAAA..."))
        self.assertTrue(v.ok)

    # -- clean diffs pass --------------------------------------------------

    def test_clean_diff_passes(self):
        v = self.s.scan(_diff("app/main.py", "def hello():", "    return 42"))
        self.assertTrue(v.ok)
        self.assertEqual(v.findings, [])

    def test_empty_diff_passes(self):
        self.assertTrue(self.s.scan("").ok)

    def test_only_scans_added_lines(self):
        # A secret on a REMOVED line (leading '-') is being deleted, not added.
        diff = (
            "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,1 +0,0 @@\n"
            "-token = 'ghp_" + "a" * 36 + "'\n"
        )
        self.assertTrue(self.s.scan(diff).ok)

    def test_placeholder_not_flagged(self):
        # Short/among-spaces values don't trip the generic assignment rule.
        v = self._flag('password = "changeme"')
        self.assertTrue(v.ok)

    # -- redaction & reporting --------------------------------------------

    def test_finding_is_redacted(self):
        secret = "ghp_" + "a" * 36
        v = self._flag(f"token = '{secret}'")
        rendered = v.render()
        self.assertNotIn(secret, rendered)
        for f in v.findings:
            self.assertNotIn(secret, f.masked)

    def test_render_mentions_path_and_recovery(self):
        v = self._flag("aws = 'AKIAIOSFODNN7EXAMPLE'", path="src/leak.py")
        r = v.render()
        self.assertIn("src/leak.py", r)
        self.assertIn("agentctl ready", r)  # tells the agent how to recover
        self.assertIn("agentctl ask", r)  # ...and how to escalate a false positive

    def test_line_number_tracked(self):
        diff = (
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
            "@@ -0,0 +5,2 @@\n"
            "+clean = 1\n"
            "+key = 'AKIAIOSFODNN7EXAMPLE'\n"
        )
        v = self.s.scan(diff)
        self.assertFalse(v.ok)
        self.assertEqual(v.findings[0].line, 6)

    # -- size cap ----------------------------------------------------------

    def test_oversize_diff_is_truncated_and_flagged(self):
        filler = _diff("big.txt", *["x" * 100 for _ in range(50)])
        huge = filler + "y" * (MAX_DIFF_BYTES + 10)
        v = self.s.scan(huge)
        self.assertTrue(v.truncated)
        self.assertIn("was scanned", v.render())

    def test_secret_before_cap_still_found(self):
        diff = _diff("f.py", "aws = 'AKIAIOSFODNN7EXAMPLE'") + "z" * (MAX_DIFF_BYTES + 1)
        v = self.s.scan(diff)
        self.assertFalse(v.ok)


class NullGateTest(unittest.TestCase):
    def test_null_gate_always_passes(self):
        v = NullGate().scan(_diff("f", "token = 'ghp_" + "a" * 36 + "'"))
        self.assertTrue(v.ok)


class BuildGateTest(unittest.TestCase):
    def test_off_builds_null_gate(self):
        self.assertIsInstance(build_gate("off"), NullGate)

    def test_block_builds_regex_scanner(self):
        self.assertIsInstance(build_gate("block"), RegexSecretScanner)

    def test_deep_scan_without_key_degrades_to_regex(self):
        self.assertIsInstance(build_gate("block", "claude", None), RegexSecretScanner)

    def test_deep_scan_with_key_builds_claude_gate(self):
        self.assertIsInstance(build_gate("block", "claude", "sk-ant-xxx"), ClaudeSecurityGate)


class ClaudeGateTest(unittest.TestCase):
    def _transport(self, findings):
        import json as _json

        def t(method, url, headers, body):
            return {"content": [{"type": "text", "text": _json.dumps({"findings": findings})}]}

        return t

    def test_regex_findings_survive_even_if_llm_finds_nothing(self):
        gate = ClaudeSecurityGate("k", transport=self._transport([]))
        v = gate.scan(_diff("f.py", "aws = 'AKIAIOSFODNN7EXAMPLE'"))
        self.assertFalse(v.ok)  # deterministic hit is authoritative

    def test_llm_adds_findings(self):
        extra = [{"rule": "obfuscated key", "path": "f.py", "masked": "ab…yz (30 chars)"}]
        gate = ClaudeSecurityGate("k", transport=self._transport(extra))
        v = gate.scan(_diff("f.py", "some = 'value'"))
        self.assertFalse(v.ok)
        self.assertTrue(any("deep-scan" in f.rule for f in v.findings))

    def test_api_error_degrades_to_regex_result(self):
        from issuefleet.httpx import ApiError

        def boom(*a, **k):
            raise ApiError(500, "https://api.anthropic.com", "down")

        gate = ClaudeSecurityGate("k", transport=boom)
        # Clean diff + API down => still passes (degrades, never wedges).
        self.assertTrue(gate.scan(_diff("f.py", "x = 1")).ok)
        # A real secret + API down => still caught by the regex base.
        self.assertFalse(gate.scan(_diff("f.py", "k = 'AKIAIOSFODNN7EXAMPLE'")).ok)


if __name__ == "__main__":
    unittest.main()
