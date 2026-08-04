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
    # "in" (someone else posted it) | "out" (the service posted it). The only
    # reliable own-message signal: sigbot records no sender/sender_name at all on
    # its own sends, so an author-name comparison can never identify them.
    # Defaults to "in" so a service that omits the field behaves as before rather
    # than going deaf.
    direction: str = "in"
    raw: dict = field(default_factory=dict)

    @property
    def outbound(self) -> bool:
        return self.direction.lower() == "out"

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
            direction=first(("direction",), default="in"),
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

    def react(self, message_id: Any, emoji: str) -> bool:
        """Put an emoji on a message. Best-effort: returns False (never raises)
        when the service or client is too old to support reactions."""


class SigbotClient:
    """Wraps ``sigbot_client.ServiceClient``, normalizing its errors to
    ``SignalError`` and its message dicts to ``SignalMessage``. The underlying
    client is built lazily on first use (so importing this module needs no
    package), or injected outright for tests."""

    def __init__(self, base_url: str, api_key: str, *, service_client: object | None = None):
        self._base_url = base_url
        self._api_key = api_key
        self._client = service_client
        self._warned_no_reactions = False

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

    def react(self, message_id: Any, emoji: str) -> bool:
        """Put an emoji on a message; True if it landed.

        Deliberately best-effort and never raising. A reaction is a courtesy —
        it must not be able to fail a tick or lose a message — and the capability
        is split across two moving parts the operator upgrades independently:
        sigbot-client >= 0.3.0 for the method, and a sigbot service new enough to
        serve the route. Either being behind degrades to silence, not an error.

        Reacting again replaces the previous emoji rather than adding a second,
        which is Signal's own semantics — so ✅ after 👀 needs no explicit clear.
        """
        svc = self._svc() if self._client is not None else self._try_svc()
        if svc is None or not hasattr(svc, "react"):
            self._note_no_reactions("sigbot-client is older than 0.3.0")
            return False
        try:
            svc.react(message_id, emoji)
            return True
        except Exception as e:
            status = getattr(e, "status", None)
            if status == 404:
                self._note_no_reactions("this sigbot service has no reactions route")
            else:
                log.debug("reaction on %s failed: %s", message_id, e)
            return False

    def _try_svc(self):
        try:
            return self._svc()
        except SignalError:
            return None

    def _note_no_reactions(self, why: str) -> None:
        # Once per process: a per-message warning would be pure noise on a
        # deployment that simply hasn't upgraded yet.
        if not self._warned_no_reactions:
            log.info("fleet manager: reactions unavailable (%s); continuing without them", why)
            self._warned_no_reactions = True
