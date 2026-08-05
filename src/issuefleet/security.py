"""Security gate: scan what a `ready` would push for leaked credentials.

A worker's containers run with permission prompts disabled and hold no
credentials of their own — but nothing stops an agent from *committing* a
secret it generated, pasted, or scraped from the environment into its branch.
The one outbox action that carries such content into the repository is
`ready` (it force-pushes the branch and opens the PR). This module gates the
diff that a `ready` would push, host-side in the orchestrator, and lets the
gate reject with a rationale that the reconciler routes back into the worker's
inbox so the agent can fix it and submit again.

The design mirrors ``advisor.py``: a ``SecurityGate`` Protocol with a
deterministic default (``RegexSecretScanner``), an optional LLM backend
(``ClaudeSecurityGate``) that only *adds* findings, and a ``build_gate``
factory that degrades gracefully. Two invariants matter for a security seam:

- **Redact.** A finding never carries the matched secret verbatim — the
  rejection note and the archived outbox receipt are themselves durable
  artifacts, so echoing the key back would relocate the leak, not stop it.
  ``Finding.masked`` keeps only enough to identify the hit.
- **The deterministic scanner is authoritative.** The optional model can find
  *more*, but a model outage never *clears* a hit the regex scanner made and
  never wedges a submission — an API failure degrades to the regex result.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from issuefleet.httpx import ApiError, urllib_transport

log = logging.getLogger("issuefleet.security")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"

# A diff larger than this is scanned only up to the cap (added lines are cheap,
# but a vendored blob or a lockfile churn shouldn't make the gate chew MBs).
# Truncation is surfaced in the verdict so a real leak past the cap isn't
# silently cleared.
MAX_DIFF_BYTES = 4_000_000

GATE_MODES = ("block", "warn", "off")
DEEP_SCAN_KINDS = ("off", "claude")


@dataclass
class Finding:
    rule: str  # which detector fired (human-readable)
    path: str  # file the added line belongs to ("" if unknown)
    line: int  # 1-based line number within the new file, 0 if not tracked
    masked: str  # a REDACTED excerpt — never the raw secret

    def describe(self) -> str:
        where = self.path or "(unknown file)"
        loc = f"{where}:{self.line}" if self.line else where
        return f"- {self.rule} in {loc}: {self.masked}"


@dataclass
class Verdict:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    truncated: bool = False  # diff exceeded MAX_DIFF_BYTES; tail unscanned

    def render(self) -> str:
        """The rationale delivered to the worker on a block. Lists what fired,
        redacted, and tells the agent how to resolve or escalate."""
        lines = [
            "Your `ready` was rejected by the security gate: the diff you're "
            "trying to submit appears to contain leaked credentials. Nothing "
            "was pushed.",
            "",
            "Findings (values redacted):",
            *(f.describe() for f in self.findings),
            "",
            "Remove the secret from the diff — delete it from the file AND "
            "rewrite the history so it isn't in any commit on this branch "
            "(e.g. `git rebase`/amend, or move it to an ignored file and "
            "`git rm --cached`) — then commit and run `agentctl ready` again. "
            "If this is a false positive (e.g. a test fixture or documentation "
            "example, not a live credential), explain via `agentctl ask` and a "
            "human will decide.",
        ]
        if self.truncated:
            lines.insert(
                -1,
                "(Note: the diff was large and only the first "
                f"{MAX_DIFF_BYTES // 1_000_000}MB was scanned.)",
            )
        return "\n".join(lines)


class SecurityGate(Protocol):
    def scan(self, diff: str) -> Verdict:
        """Inspect a unified diff and return a verdict. ``ok=False`` means a
        credential looks present in the added lines."""
        ...


# --------------------------------------------------------------------- redaction


def _mask(secret: str) -> str:
    """Show enough to identify the hit without reproducing the credential:
    a short prefix, the length, and a hash-free ellipsis. Never the middle."""
    s = secret.strip()
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-2:]} ({len(s)} chars)"


# --------------------------------------------------------------------- patterns

# High-confidence credential shapes. Deliberately narrow: each pattern targets
# a *known* token format so a match is almost certainly a real secret, keeping
# false positives (which block a legitimate PR) rare. Aligned with the
# credential prefixes config.py already refuses to accept inline.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(
        r"(?i)aws.{0,20}(secret|access).{0,20}[=:]\s*['\"]?([A-Za-z0-9/+]{40})['\"]?"
    )),
    ("GitHub token", re.compile(r"\bgh[posru]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Linear API key", re.compile(r"\blin_(api|oauth)_[A-Za-z0-9]{32,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe secret key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}\b")),
    ("Tailscale auth key", re.compile(r"\btskey-[A-Za-z0-9-]{10,}\b")),
    ("Google OAuth client secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # A generic assignment to a secret-shaped name with a long opaque value.
    # Kept last and stricter (>=16 char value, no spaces) to bound false hits.
    ("hardcoded secret assignment", re.compile(
        r"(?i)\b(pass(word|wd)?|secret|token|api[-_]?key|access[-_]?key|"
        r"private[-_]?key|client[-_]?secret)\b\s*[=:]\s*['\"]([^'\"\s]{16,})['\"]"
    )),
)

# Files whose mere presence in a diff is a leak, regardless of contents.
_SENSITIVE_FILE = re.compile(
    r"(^|/)("
    r"\.env(\.[^/]+)?"
    r"|id_rsa|id_dsa|id_ecdsa|id_ed25519"
    r"|.*\.(pem|pfx|p12|key|keystore|jks)"
    r"|credentials\.json|service[-_]account.*\.json"
    r"|\.npmrc|\.pypirc|\.netrc"
    r")$",
    re.IGNORECASE,
)
# ...but a public key or a lockfile named *.key false-positives; exempt these.
_SENSITIVE_FILE_EXEMPT = re.compile(r"\.pub$|/known_hosts$", re.IGNORECASE)


def _iter_added(diff: str):
    """Yield (path, new_line_no, text) for every added line in a unified diff.
    Also yields a synthetic ('<file>', 0, '') marker line-count is irrelevant
    for; callers key off ``path`` for file-level rules. Tracks the current file
    from ``+++ b/<path>`` headers and the new-side line number from @@ hunks."""
    path = ""
    new_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            # "+++ b/foo" -> "foo"; "/dev/null" for deletions.
            path = p[2:] if p.startswith(("a/", "b/")) else p
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_no = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            yield path, new_no, raw[1:]
            new_no += 1
        elif not raw.startswith("-"):
            # context line advances the new-side counter; "-" (removed) does not.
            new_no += 1


def _added_files(diff: str) -> list[str]:
    """Paths introduced (or modified) on the new side of the diff."""
    files = []
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            p = p[2:] if p.startswith(("a/", "b/")) else p
            if p and p != "/dev/null":
                files.append(p)
    return files


class RegexSecretScanner:
    """Deterministic, stdlib-only credential scanner over a unified diff.
    Scans only ADDED lines (what the branch introduces) plus the set of files
    the diff touches, so an unchanged secret already on the base isn't blamed
    on this PR."""

    def scan(self, diff: str) -> Verdict:
        truncated = False
        if len(diff) > MAX_DIFF_BYTES:
            diff = diff[:MAX_DIFF_BYTES]
            truncated = True

        findings: list[Finding] = []
        seen: set[tuple[str, str, int]] = set()

        for path in _added_files(diff):
            if _SENSITIVE_FILE.search(path) and not _SENSITIVE_FILE_EXEMPT.search(path):
                key = ("sensitive file", path, 0)
                if key not in seen:
                    seen.add(key)
                    findings.append(Finding(
                        rule="sensitive file added",
                        path=path,
                        line=0,
                        masked=f"file {path!r} looks like it holds credentials",
                    ))

        for path, line_no, text in _iter_added(diff):
            for rule, pat in _SECRET_PATTERNS:
                m = pat.search(text)
                if not m:
                    continue
                # Prefer the most specific capturing group as the secret.
                secret = next((g for g in reversed(m.groups()) if g), m.group(0))
                key = (rule, path, line_no)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    rule=rule, path=path, line=line_no, masked=_mask(secret),
                ))

        return Verdict(ok=not findings, findings=findings, truncated=truncated)


class NullGate:
    """Always passes. Used when the gate is turned off."""

    def scan(self, diff: str) -> Verdict:  # noqa: D401 - trivial
        return Verdict(ok=True)


_LLM_SYSTEM = (
    "You are a security reviewer for an autonomous coding agent. You are given "
    "the ADDED lines of a pull-request diff. Decide whether they introduce a "
    "leaked live credential: an API key, token, password, private key, or "
    "similar secret that should never be committed. Report ONLY genuine "
    "secrets — ignore obvious placeholders ('YOUR_KEY_HERE', 'xxx', "
    "'example'), test fixtures clearly marked as fake, and public keys. For "
    "each real finding give the file, a short rule label, and a REDACTED "
    "excerpt (never the full secret). If there are none, return an empty list."
)

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "path": {"type": "string"},
                    "masked": {"type": "string"},
                },
                "required": ["rule", "path", "masked"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


class ClaudeSecurityGate:
    """LLM-backed deep scan layered *on top of* the deterministic scanner. It
    can surface secrets the regexes miss (novel formats, obfuscation), but it
    is additive only: any regex finding stands, and any API failure degrades to
    the deterministic verdict rather than clearing a hit or wedging a `ready`."""

    def __init__(
        self,
        api_key: str,
        base: SecurityGate | None = None,
        model: str = DEFAULT_MODEL,
        transport=urllib_transport,
    ):
        self._api_key = api_key
        self._base = base or RegexSecretScanner()
        self._model = model
        self._transport = transport

    def scan(self, diff: str) -> Verdict:
        base = self._base.scan(diff)
        added = "\n".join(
            f"{p}: {t}" for p, _, t in _iter_added(
                diff[:MAX_DIFF_BYTES] if len(diff) > MAX_DIFF_BYTES else diff
            )
        )
        if not added.strip():
            return base
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "thinking": {"type": "adaptive"},
            "output_config": {"format": {"type": "json_schema", "schema": _LLM_SCHEMA}},
            "system": _LLM_SYSTEM,
            "messages": [{"role": "user", "content": f"Added diff lines:\n{added}"}],
        }
        try:
            resp = self._transport(
                "POST",
                API_URL,
                {
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                body,
            )
        except ApiError:
            log.exception("security deep-scan API call failed; using regex result only")
            return base
        extra = self._parse(resp)
        # Merge: keep every deterministic finding, append model-only ones.
        merged = list(base.findings)
        for f in extra:
            if not any(e.path == f.path and e.rule == f.rule for e in merged):
                merged.append(f)
        return Verdict(ok=not merged, findings=merged, truncated=base.truncated)

    @staticmethod
    def _parse(resp: dict) -> list[Finding]:
        text = ""
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                break
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("security deep-scan returned non-JSON; ignoring its findings")
            return []
        out = []
        for item in data.get("findings", []) or []:
            out.append(Finding(
                rule=f"deep-scan: {item.get('rule', 'secret')}",
                path=str(item.get("path", "")),
                line=0,
                masked=str(item.get("masked", "(redacted)")),
            ))
        return out


def build_gate(mode: str, deep_scan: str = "off", api_key: str | None = None) -> SecurityGate:
    """Pick the gate backend from config. ``mode='off'`` disables scanning
    entirely; otherwise the deterministic scanner runs, optionally wrapped by
    the Claude deep scan when ``deep_scan='claude'`` and a key resolves. A
    requested deep scan with no key degrades (with a warning) to the
    deterministic scanner rather than failing startup."""
    if mode == "off":
        return NullGate()
    if deep_scan == "claude":
        if not api_key:
            log.warning(
                "security deep_scan='claude' but no Anthropic API key resolved; "
                "using the deterministic scanner alone"
            )
            return RegexSecretScanner()
        return ClaudeSecurityGate(api_key)
    return RegexSecretScanner()
