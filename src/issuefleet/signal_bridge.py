"""Signal chat bridge for the fleet manager, over the sigbot service API.

The fleet manager's chat channel is a single `sigbot
<https://github.com/fughilli/sigbot>`_ *service* — one Signal group with its
own persona, scoped by an API key minted in the sigbot dashboard. This module
gives the manager a small, injectable client with exactly the surface it needs:

    client.service()                     -> {'name','label','group_name', ...}
    client.send("deploy done ✅")        -> post into the group as the bot
    client.messages(limit=50)            -> the group's recent message log
    client.messages(after_id="...")      -> only messages newer than an id

Two implementations, chosen by :func:`connect`:

* :class:`SigbotServiceClient` — the *official* ``sigbot_client.ServiceClient``,
  lazily imported. This is the supported production path (it owns the real wire
  protocol) and what the issue asks for ("integrated with sigbot-client"). It is
  a runtime import, never a Bazel/pip dependency of this package, so the
  stdlib-only daemon image and the hermetic test build are unaffected.

* :class:`UrllibSignalClient` — a stdlib fallback over
  :func:`issuefleet.httpx.urllib_transport` (same reason ``httpx.py`` exists),
  used when the package isn't installed. Its endpoint/auth shape mirrors the
  sigbot API but is a documented best-effort; prefer the official client in
  production.

The fleet manager depends only on the three-method surface (the
:class:`SignalClient` protocol), so tests inject a fake and neither
implementation is exercised offline. Errors surface as :class:`SignalError`
(``.status`` / ``.message``), mirroring the package's ``SigbotApiError``.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from issuefleet.httpx import ApiError, urllib_transport

log = logging.getLogger("issuefleet.signal")


class SignalError(Exception):
    """A sigbot API failure. Mirrors sigbot_client.SigbotApiError so callers
    can treat either uniformly (``.status`` is the HTTP status, 0 for a
    transport/connection failure; ``.message`` is a short detail)."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"sigbot API error {status}: {message}")


@runtime_checkable
class SignalClient(Protocol):
    """The surface the fleet manager needs — satisfied by both the official
    ``sigbot_client.ServiceClient`` and :class:`UrllibSignalClient`."""

    def service(self) -> dict: ...

    def send(self, text: str, prefix: bool = True) -> object: ...

    def messages(self, limit: int = 50, after_id: object | None = None) -> list[dict]: ...


class SigbotServiceClient:
    """Thin adapter around the official ``sigbot_client.ServiceClient``.

    The import is deferred to construction so importing this module never
    requires the package; a missing package raises a clear, actionable error
    rather than an ImportError at daemon start."""

    def __init__(self, base_url: str, api_key: str):
        try:
            from sigbot_client import ServiceClient  # type: ignore
        except ImportError as e:  # pragma: no cover - exercised only live
            raise SignalError(
                0,
                "sigbot_client is not installed; `pip install sigbot-client` in the "
                "daemon environment (or leave it out to use the stdlib fallback)",
            ) from e
        self._client = ServiceClient(base_url, api_key=api_key)

    def service(self) -> dict:
        return self._client.service()

    def send(self, text: str, prefix: bool = True) -> object:
        return self._client.send(text, prefix=prefix)

    def messages(self, limit: int = 50, after_id: object | None = None) -> list[dict]:
        return self._client.messages(limit=limit, after_id=after_id)


class UrllibSignalClient:
    """Stdlib fallback speaking the sigbot service API over urllib.

    Endpoint and auth shape mirror the sigbot service (base_url + Bearer
    api_key; ``/service`` and ``/messages``). This is a best-effort so the
    daemon works without the package; the official client is the wire-correct
    production path. The transport is injectable so tests pin exact requests.
    """

    def __init__(self, base_url: str, api_key: str, transport=urllib_transport):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            return self.transport(method, f"{self.base_url}{path}", self._headers(), payload)
        except ApiError as e:
            raise SignalError(e.status, str(e)) from e

    def service(self) -> dict:
        return self._call("GET", "/service")

    def send(self, text: str, prefix: bool = True) -> dict:
        return self._call("POST", "/messages", {"text": text, "prefix": prefix})

    def messages(self, limit: int = 50, after_id: object | None = None) -> list[dict]:
        query = f"?limit={int(limit)}"
        if after_id is not None:
            query += f"&after_id={after_id}"
        data = self._call("GET", f"/messages{query}", None)
        # Accept either a bare list or a {"messages": [...]} envelope.
        if isinstance(data, dict):
            return list(data.get("messages", []))
        return list(data)


def connect(base_url: str, api_key: str, *, prefer_package: bool = True) -> SignalClient:
    """Build a SignalClient. Prefer the official ``sigbot_client`` package (the
    supported wire-correct path); fall back to the stdlib client when it isn't
    installed, logging once so the operator knows which is in use."""
    if prefer_package:
        try:
            import sigbot_client  # type: ignore # noqa: F401
        except ImportError:
            log.warning(
                "sigbot_client not installed; using the stdlib fallback client. "
                "Install sigbot-client in the daemon environment for the supported path."
            )
        else:
            return SigbotServiceClient(base_url, api_key)
    return UrllibSignalClient(base_url, api_key)
