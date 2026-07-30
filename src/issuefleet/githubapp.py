"""GitHub App authentication — PRs open as ``yourapp[bot]``.

Flow: an RS256-signed JWT (app identity) mints short-lived installation
access tokens (1h), which the forge uses as Bearer tokens. Python's stdlib
cannot sign RS256, so the JWT signature shells out to ``openssl dgst
-sha256 -sign`` (present on macOS and Linux; GitHub App keys are RSA, for
which openssl emits exactly the raw RSASSA-PKCS1-v1_5 signature JWTs need).
This keeps the zero-dependency rule intact.

Tokens are cached per installation and refreshed with a safety margin; the
provider maps repo owners to installations, so one app covers projects
across multiple orgs/users if it's installed on each.
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path

from issuefleet.httpx import urllib_transport

API_ROOT = "https://api.github.com"
JWT_TTL_S = 9 * 60  # GitHub caps app JWTs at 10 minutes
TOKEN_REFRESH_MARGIN_S = 5 * 60


class GithubAppError(Exception):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def openssl_sign(key_path: Path, data: bytes) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=data,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise GithubAppError(
            f"openssl signing with {key_path} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def build_jwt(app_id: str, key_path: Path, now: int | None = None, signer=openssl_sign) -> str:
    now = int(time.time()) if now is None else now
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(
        json.dumps(
            {"iat": now - 60, "exp": now + JWT_TTL_S, "iss": str(app_id)}  # 60s clock-skew grace
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    return f"{header}.{payload}." + b64url(signer(key_path, signing_input))


class AppTokenProvider:
    """Mints and caches installation tokens; thread-safe (the webhook thread
    and the tick may both trigger forge calls)."""

    def __init__(
        self,
        app_id: str,
        key_path: Path,
        installation_id: int | None = None,  # None = discover by repo owner
        transport=urllib_transport,
        clock=time.time,
        signer=openssl_sign,
    ):
        self.app_id = str(app_id)
        self.key_path = Path(key_path)
        self.pinned_installation = installation_id
        self.transport = transport
        self.clock = clock
        self.signer = signer
        self._lock = threading.Lock()
        self._installations: dict[str, int] | None = None  # owner(lower) -> id
        self._tokens: dict[int, tuple[str, float]] = {}  # id -> (token, expires_at)

    def _jwt_call(self, method: str, path: str, payload: dict | None = None):
        jwt = build_jwt(self.app_id, self.key_path, now=int(self.clock()), signer=self.signer)
        return self.transport(
            method,
            f"{API_ROOT}{path}",
            {
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            payload,
        )

    def installations(self) -> dict[str, int]:
        if self._installations is None:
            found = {}
            for inst in self._jwt_call("GET", "/app/installations"):
                account = (inst.get("account") or {}).get("login", "")
                found[account.lower()] = inst["id"]
            if not found:
                raise GithubAppError(
                    f"GitHub App {self.app_id} has no installations — install it on the "
                    "target repos/orgs first (GitHub → the app → Install)"
                )
            self._installations = found
        return self._installations

    def installation_for_owner(self, owner: str) -> int:
        if self.pinned_installation is not None:
            return self.pinned_installation
        inst = self.installations().get(owner.lower())
        if inst is None:
            raise GithubAppError(
                f"GitHub App {self.app_id} is not installed for {owner!r}; "
                f"installed on: {sorted(self.installations())}"
            )
        return inst

    def token_for_owner(self, owner: str) -> str:
        inst_id = self.installation_for_owner(owner)
        with self._lock:
            cached = self._tokens.get(inst_id)
            if cached and cached[1] - self.clock() > TOKEN_REFRESH_MARGIN_S:
                return cached[0]
            resp = self._jwt_call("POST", f"/app/installations/{inst_id}/access_tokens", {})
            token = resp.get("token")
            if not token:
                raise GithubAppError(f"no token in access_tokens response: {resp}")
            # expires_at is ISO8601; keep it simple and conservative: GitHub
            # installation tokens live 1h.
            self._tokens[inst_id] = (token, self.clock() + 3600)
            return token

    def app_slug(self) -> str:
        """The app's slug (doctor display); bot user is <slug>[bot]."""
        return self._jwt_call("GET", "/app").get("slug", "?")
