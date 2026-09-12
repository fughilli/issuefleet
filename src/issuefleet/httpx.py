"""Tiny stdlib HTTP JSON transport shared by the Linear and GitHub clients.

Clients take a ``transport`` callable so tests can assert exact requests
offline; the default is urllib.

Requests name themselves: urllib's default (``Python-urllib/3.x``) is a bot
signature that a WAF in front of a self-hosted forge rejects outright.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
import urllib.error
import urllib.request

from issuefleet import __version__

TIMEOUT_S = 30
USER_AGENT = f"issuefleet/{__version__} (+https://github.com/fughilli/issuefleet)"


class ApiError(Exception):
    def __init__(self, status: int, url: str, detail: str):
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {detail[:300]}")


@dataclass
class JsonResponse:
    """Decoded JSON and response headers keyed by lowercase field name."""

    data: dict | list
    headers: dict[str, str]


def urllib_transport(method: str, url: str, headers: dict, payload: dict | None) -> dict | list:
    """JSON-only transport contract retained for existing clients and fakes."""
    return urllib_transport_with_headers(method, url, headers, payload).data


def urllib_transport_with_headers(
    method: str, url: str, headers: dict, payload: dict | None
) -> JsonResponse:
    """JSON plus response headers, for clients that need pagination metadata."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode()
            response_headers = {
                key.lower(): value for key, value in getattr(resp, "headers", {}).items()
            }
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, url, e.read().decode(errors="replace"))
    except urllib.error.URLError as e:
        raise ApiError(0, url, str(e.reason))
    return JsonResponse(json.loads(body) if body.strip() else {}, response_headers)
