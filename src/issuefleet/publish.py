"""Publish surfaces for the roadmap bot.

The roadmap bot writes one summary per run and hands it to one or more *publish
surfaces* — the places stakeholders read it. Discord is the first (and, for
now, only) surface built out, but the seam is deliberately small so Slack, a
Linear document, or a plain webhook can be added the same way: implement
``Publisher`` and nothing else changes.

Like the Linear/GitHub clients (and unlike sigbot, which ships its own client),
each surface here is hand-rolled over the shared stdlib ``httpx`` transport —
the daemon core stays dependency-free, and tests inject a fake transport to
assert the exact request offline.
"""

from __future__ import annotations

import logging
from typing import Protocol

from issuefleet.httpx import ApiError, urllib_transport

log = logging.getLogger("issuefleet.publish")

# Discord rejects a message body over 2000 characters outright, so a summary
# longer than that is split across several messages (see ``chunk``). A little
# headroom under the hard limit leaves room for the "(1/3)" continuation
# markers a future change might add without re-tuning the math.
DISCORD_LIMIT = 2000


class PublishError(Exception):
    """A surface failed to publish. The bot logs it and moves on to the next
    surface rather than losing the whole run to one dead webhook."""


class Publisher(Protocol):
    """One place the roadmap summary is posted. ``name`` is for logs and the
    doctor; ``publish`` posts the (Markdown) text, raising PublishError on
    failure."""

    name: str

    def publish(self, text: str) -> None: ...


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split ``text`` into pieces no longer than ``limit`` characters, breaking
    on line boundaries so a paragraph or table row is never cut mid-line.

    A single line longer than the limit (a very wide table, say) is hard-split
    as a last resort — better a broken line than a rejected message. Blank
    output collapses to one empty-safe chunk so a caller never has to special-case
    "nothing to send".
    """
    if not text.strip():
        return []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A line that can't fit on its own is split hard, in limit-sized bites.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class DiscordWebhookPublisher:
    """Posts to a Discord channel via an incoming webhook.

    A webhook needs no bot token or gateway connection: one HTTPS POST of
    ``{"content": ...}`` to the webhook URL drops a message in the channel. The
    URL itself carries a secret token, so it is resolved env-then-file like
    every other credential and never sits in the config file.

    Discord renders standard Markdown (headings, bold, lists, fenced code) but
    NOT tables or Mermaid — those arrive as their raw source, which is still
    legible and copy-pasteable. Long summaries are chunked to Discord's
    2000-character ceiling and posted in order.
    """

    name = "discord"

    def __init__(self, webhook_url: str, *, username: str | None = None, transport=urllib_transport):
        self._url = webhook_url
        self._username = username
        self._transport = transport

    def publish(self, text: str) -> None:
        pieces = chunk(text)
        if not pieces:
            log.info("roadmap: nothing to publish to Discord (empty summary)")
            return
        for i, piece in enumerate(pieces):
            payload: dict = {"content": piece}
            if self._username:
                payload["username"] = self._username
            try:
                # Discord replies 204 No Content on success; the transport
                # returns {} for an empty body, which we don't inspect.
                self._transport("POST", self._url, {"Content-Type": "application/json"}, payload)
            except ApiError as e:
                raise PublishError(
                    f"Discord webhook POST failed on message {i + 1}/{len(pieces)}: {e}"
                ) from e
