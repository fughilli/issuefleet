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

# Discord's REST base, and the User-Agent bot requests are expected to send.
# urllib's default ("Python-urllib/3.x") is a shape Discord's edge is known to
# turn away, so bot calls name themselves explicitly.
DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/fughilli/issuefleet, 0.1)"


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


class _DiscordPublisher:
    """Shared plumbing for the two ways to reach a Discord channel.

    Both chunk the summary to Discord's 2000-character ceiling and POST the
    pieces in order, stopping at the first failure; they differ only in the URL
    they post to, what authenticates the call, and what the message can carry.

    Discord renders standard Markdown (headings, bold, lists, fenced code) but
    NOT tables or Mermaid — those arrive as their raw source, which is still
    legible and copy-pasteable.
    """

    name = "discord"

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict:
        raise NotImplementedError

    def _payload(self, piece: str) -> dict:
        return {"content": piece}

    def publish(self, text: str) -> None:
        pieces = chunk(text)
        if not pieces:
            log.info("roadmap: nothing to publish to Discord (empty summary)")
            return
        for i, piece in enumerate(pieces):
            try:
                # A webhook replies 204 No Content and the bot endpoint replies
                # 200 with the created message; neither body is inspected (the
                # transport hands back {} for an empty one).
                self._transport(
                    "POST", self._endpoint(), self._headers(), self._payload(piece)
                )
            except ApiError as e:
                raise PublishError(
                    f"Discord POST ({self.name}) failed on message "
                    f"{i + 1}/{len(pieces)}: {e}"
                ) from e


class DiscordWebhookPublisher(_DiscordPublisher):
    """Posts to a Discord channel via an incoming webhook.

    A webhook needs no bot account or gateway connection: one HTTPS POST of
    ``{"content": ...}`` to the webhook URL drops a message in the channel. The
    URL itself carries a secret token, so it is resolved env-then-file like
    every other credential and never sits in the config file.

    Because the message is not attributed to any account, a webhook post can
    override its own display name per message (``username``).
    """

    name = "discord-webhook"

    def __init__(self, webhook_url: str, *, username: str | None = None, transport=urllib_transport):
        self._url = webhook_url
        self._username = username
        self._transport = transport

    def _endpoint(self) -> str:
        return self._url

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _payload(self, piece: str) -> dict:
        payload = {"content": piece}
        if self._username:
            payload["username"] = self._username
        return payload


class DiscordBotPublisher(_DiscordPublisher):
    """Posts to a Discord channel as a bot account.

    Where a webhook is an anonymous drop-box for one channel, this authenticates
    as the application's bot user (``Authorization: Bot <token>``) and posts to
    ``/channels/{id}/messages``. That is the surface to use when the update
    should come *from* an identifiable member of the server — the bot's avatar,
    name, and role are its own, it can be given access per channel, and the same
    token can later reach any channel it can see (the roadmap bot posts to one).

    No gateway/websocket connection is involved: posting a message is a plain
    REST call. The bot must be in the server (invited via OAuth2 with the ``bot``
    scope) and hold **View Channel** + **Send Messages** on the target channel,
    or Discord answers 403.

    ``username`` has no analogue here — a bot always posts under its own account
    name, which is changed in the Developer Portal, not per message.
    """

    name = "discord-bot"

    def __init__(self, token: str, channel_id: str, *, transport=urllib_transport):
        self._token = token
        self._channel_id = str(channel_id)
        self._transport = transport

    def _endpoint(self) -> str:
        return f"{DISCORD_API}/channels/{self._channel_id}/messages"

    def _headers(self) -> dict:
        # The "Bot " prefix is required: without it Discord reads the token as a
        # (long-dead) user token and answers 401.
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        }
