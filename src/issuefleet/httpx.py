"""Tiny stdlib HTTP JSON transport shared by the Linear and GitHub clients.

Clients take a ``transport`` callable so tests can assert exact requests
offline; the default is urllib.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT_S = 30


class ApiError(Exception):
    def __init__(self, status: int, url: str, detail: str):
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {detail[:300]}")


def urllib_transport(method: str, url: str, headers: dict, payload: dict | None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, url, e.read().decode(errors="replace"))
    except urllib.error.URLError as e:
        raise ApiError(0, url, str(e.reason))
    return json.loads(body) if body.strip() else {}
