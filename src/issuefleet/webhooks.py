"""Webhook listener: push-based wake-ups for the reconcile loop, plus the
entry point for Linear agent sessions.

Design rule: webhooks are an *accelerator*, never the source of truth. A
valid event does no work in the request thread beyond (a) waking the daemon
so the next tick runs now instead of at the poll interval, and (b) queueing
parsed agent-session events for the reconciler. Deliveries can be lost or
replayed; the polling reconcile loop remains the safety net, and all the
existing idempotence/dedupe still applies.

Endpoints (bind to localhost and put a tunnel — Cloudflare Tunnel,
Tailscale Funnel — in front; never expose the port directly):
    POST /webhook/github   X-Hub-Signature-256: sha256=<hex hmac-sha256(body)>
    POST /webhook/linear   Linear-Signature: <hex hmac-sha256(body)>,
                           webhookTimestamp (ms) must be fresh (replay guard)

Responses are immediate (Linear requires 200 within 5s); the Linear agent
platform's 10-second first-activity rule is met by the on_session callback,
which must itself be non-blocking.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("issuefleet.webhooks")

LINEAR_TIMESTAMP_SKEW_MS = 60_000


@dataclass
class SessionEvent:
    """A parsed Linear AgentSessionEvent."""

    action: str  # "created" | "prompted"
    session_id: str
    issue_id: str | None
    issue_key: str | None
    body: str | None  # prompt text (prompted) or promptContext (created)
    activity_type: str | None = None  # e.g. "prompt" (user) vs "thought" (agent echo)
    actor_type: str | None = None  # webhook actor: "user" vs app/integration


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256=") :], expected)


def verify_linear_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def linear_timestamp_fresh(payload: dict, now_ms: int | None = None) -> bool:
    ts = payload.get("webhookTimestamp")
    if not isinstance(ts, (int, float)):
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return abs(now_ms - ts) <= LINEAR_TIMESTAMP_SKEW_MS


def parse_session_event(payload: dict) -> SessionEvent | None:
    if payload.get("type") != "AgentSessionEvent":
        return None
    session = payload.get("agentSession") or {}
    issue = session.get("issue") or {}
    action = payload.get("action", "")
    activity = payload.get("agentActivity") or {}
    content = activity.get("content") or {}
    if action == "prompted":
        body = activity.get("body") or content.get("body")
    else:
        body = payload.get("promptContext")
    if not session.get("id") or action not in ("created", "prompted"):
        return None
    actor = payload.get("actor") or {}
    return SessionEvent(
        action=action,
        session_id=session["id"],
        issue_id=issue.get("id"),
        issue_key=issue.get("identifier"),
        body=body,
        activity_type=content.get("type") or activity.get("type"),
        actor_type=(actor.get("type") or "").lower() or None,
    )


class WebhookServer:
    """Threaded listener. `wake` is called after every verified event;
    `on_session` receives parsed SessionEvents and must return quickly
    (queue + hand off — the HTTP response is held until it returns)."""

    def __init__(
        self,
        bind: str,
        port: int,
        wake,
        on_session=None,
        github_secret: str | None = None,
        linear_secret: str | None = None,
    ):
        self.wake = wake
        self.on_session = on_session
        self.github_secret = github_secret
        self.linear_secret = linear_secret
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # route through our logger
                log.debug("http: " + fmt, *args)

            def _respond(self, code: int, text: str = "") -> None:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(text.encode())

            def do_GET(self):
                # Health probe for tunnel setup.
                self._respond(200, "issuefleet webhook listener\n")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if self.path == "/webhook/github":
                    self._github(body)
                elif self.path == "/webhook/linear":
                    self._linear(body)
                else:
                    self._respond(404)

            def _github(self, body: bytes) -> None:
                if outer.github_secret is None:
                    return self._respond(403, "github webhook not configured")
                if not verify_github_signature(
                    outer.github_secret, body, self.headers.get("X-Hub-Signature-256")
                ):
                    return self._respond(401, "bad signature")
                event = self.headers.get("X-GitHub-Event", "?")
                log.info("github webhook: %s -> waking reconcile loop", event)
                outer.wake()
                self._respond(200, "ok")

            def _linear(self, body: bytes) -> None:
                if outer.linear_secret is None:
                    return self._respond(403, "linear webhook not configured")
                if not verify_linear_signature(
                    outer.linear_secret, body, self.headers.get("Linear-Signature")
                ):
                    return self._respond(401, "bad signature")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    return self._respond(400, "bad json")
                if not linear_timestamp_fresh(payload):
                    return self._respond(401, "stale timestamp")
                evt = parse_session_event(payload)
                if evt is not None and outer.on_session is not None:
                    log.info("linear agent session %s (%s)", evt.action, evt.issue_key or "?")
                    try:
                        outer.on_session(evt)
                    except Exception:
                        log.exception("on_session handler failed; event dropped from webhook "
                                      "path (polling remains the safety net)")
                else:
                    log.info(
                        "linear webhook: %s %s -> waking reconcile loop",
                        payload.get("type", "?"),
                        payload.get("action", ""),
                    )
                outer.wake()
                self._respond(200, "ok")

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "WebhookServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="issuefleet-webhooks", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
