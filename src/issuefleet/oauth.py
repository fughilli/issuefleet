"""Linear OAuth (agents platform) — the one-time `issuefleet linear-oauth`
installation flow.

The app is installed with ``actor=app``: Linear creates a dedicated app user
in the workspace (no seat consumed) that can be @-mentioned and delegated
issues (``app:mentionable`` / ``app:assignable``). The resulting access
token authenticates as that app user (Bearer). Workspace admin permissions
are required to complete the install.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
AGENT_SCOPES = ["read", "write", "app:mentionable", "app:assignable"]


class OAuthError(Exception):
    pass


def build_authorize_url(client_id: str, redirect_uri: str, scopes: list[str] | None = None) -> str:
    return (
        AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": ",".join(scopes or AGENT_SCOPES),
                "actor": "app",  # install as an agent app user, not as the admin
            }
        )
    )


def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, post_form=_post_form
) -> str:
    resp = post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    token = resp.get("access_token")
    if not token:
        raise OAuthError(f"token exchange failed: {resp}")
    return token


def fetch_app_token(
    client_id: str, client_secret: str, scopes: list[str] | None = None, post_form=_post_form
) -> tuple[str, int]:
    """Mint an app-actor token via the client_credentials grant. Unlike the
    authorization_code flow this needs no browser and no admin present: the
    app authenticates as itself. Linear app tokens last ~30 days and come
    with NO refresh token — the documented pattern is to request a fresh one
    when the current one nears expiry or a call returns 401. Returns
    (access_token, expires_in_seconds)."""
    resp = post_form(
        TOKEN_URL,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": ",".join(scopes or AGENT_SCOPES),
        },
    )
    token = resp.get("access_token")
    if not token:
        raise OAuthError(f"client_credentials token request failed: {resp}")
    return token, int(resp.get("expires_in") or 0)


def wait_for_code(port: int, timeout_s: int = 300) -> str:
    """One-shot localhost listener for the OAuth redirect."""
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if "code" in params:
                captured["code"] = params["code"][0]
                self.wfile.write(b"issuefleet: authorized. You can close this tab.\n")
            else:
                captured["error"] = params.get("error", ["no code in redirect"])[0]
                self.wfile.write(b"issuefleet: authorization failed; see terminal.\n")

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout_s
    try:
        while not captured:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in captured:
        raise OAuthError(f"authorization failed: {captured.get('error')}")
    return captured["code"]
