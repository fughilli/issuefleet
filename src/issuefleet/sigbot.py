"""Signal bridge for the fleet manager, over the sigbot service.

The fleet manager's chat interface is a Signal group fronted by a *sigbot
service* (https://github.com/fughilli/sigbot): one Signal group, one persona,
one API key. We talk to it through the official ``sigbot_client.ServiceClient``
— a zero-dependency urllib client that is the source of truth for the sigbot
wire protocol — wrapped here behind a small protocol so the fleet-manager loop
is testable offline with an injected fake and never imports the package at test
time.

Why wrap rather than hand-roll (as we did for Linear/GitHub over ``httpx.py``):
sigbot already ships a stdlib-only client, so wrapping it keeps us correct
against the live service instead of re-deriving its endpoints by guesswork. The
import is lazy, so the offline test suite stays dependency-free — the tests
depend on ``//src/issuefleet``, the library, which carries no pip deps at all.
The wheel hangs off ``//src/issuefleet:cli`` (the Bazel entrypoint) via the
requirements lock, so only the bare ``bin/issuefleet`` wrapper needs it
installed by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("issuefleet.sigbot")

# Message-dict keys the sigbot API *might* use. The published client docs only
# guarantee ``["id"]``; we read the text/author defensively from the first key
# present so a service-side field rename can't silently blank every message.
_TEXT_KEYS = ("text", "body", "message", "content")
_AUTHOR_KEYS = ("author", "sender", "source", "from", "name")


class SignalError(Exception):
    """A sigbot API failure, normalized from ``sigbot_client.SigbotApiError``
    (or any transport error). Carries the HTTP status and message so callers
    can special-case, e.g. a 401 on a revoked key."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        self.message = message
        super().__init__(f"sigbot HTTP {status}: {message}" if status else message)


@dataclass
class SignalMessage:
    """One message from the group log, normalized. ``raw`` keeps the original
    dict so nothing is lost when the service carries fields we don't model."""

    id: Any
    text: str
    author: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "SignalMessage":
        def first(keys: tuple[str, ...], default: str = "") -> str:
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v:
                    return v
            return default

        return cls(
            id=d.get("id"),
            text=first(_TEXT_KEYS),
            author=first(_AUTHOR_KEYS, default="unknown"),
            raw=d,
        )


class SignalClient(Protocol):
    """The slice of the sigbot ServiceClient the fleet manager depends on."""

    def service(self) -> dict:
        """Group/persona metadata: {'name', 'label', 'group_name'}."""

    def send(self, text: str, *, prefix: bool = True) -> None:
        """Post into the group as the bot. ``prefix=False`` suppresses the
        ``[label]`` prefix for this one message."""

    def messages(self, after_id: Any | None = None, limit: int = 50) -> list[SignalMessage]:
        """The group's message log, oldest-first. ``after_id`` pages
        incrementally past a previously seen id."""


class SigbotClient:
    """Wraps ``sigbot_client.ServiceClient``, normalizing its errors to
    ``SignalError`` and its message dicts to ``SignalMessage``. The underlying
    client is built lazily on first use (so importing this module needs no
    package), or injected outright for tests."""

    def __init__(self, base_url: str, api_key: str, *, service_client: object | None = None):
        self._base_url = base_url
        self._api_key = api_key
        self._client = service_client

    def _svc(self):
        if self._client is None:
            try:
                from sigbot_client import ServiceClient  # type: ignore
            except ImportError as e:
                raise SignalError(
                    None,
                    "sigbot-client is not importable in this interpreter; run via "
                    "`bazel run //:issuefleet` (the requirements lock provides it) "
                    "or `pip install sigbot-client` (see the fleet-manager setup docs)",
                ) from e
            self._client = ServiceClient(self._base_url, api_key=self._api_key)
        return self._client

    def _wrap(self, fn, what: str):
        try:
            return fn()
        except SignalError:
            raise
        except Exception as e:  # sigbot_client.SigbotApiError or any transport error
            status = getattr(e, "status", None)
            message = getattr(e, "message", None) or str(e)
            raise SignalError(status, f"{what}: {message}") from e

    def service(self) -> dict:
        return self._wrap(lambda: self._svc().service(), "service()")

    def send(self, text: str, *, prefix: bool = True) -> None:
        self._wrap(lambda: self._svc().send(text, prefix=prefix), "send()")

    def messages(self, after_id: Any | None = None, limit: int = 50) -> list[SignalMessage]:
        raw = self._wrap(
            lambda: self._svc().messages(after_id=after_id, limit=limit), "messages()"
        )
        return [SignalMessage.from_api(m) for m in (raw or [])]
