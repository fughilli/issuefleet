"""Parse a git remote URL into (host, slug).

Shared by the forge implementations and the forge factory: GitHub only ever
needs the ``owner/name`` slug (it always talks to github.com), but GitLab is
routinely self-hosted, so the host has to survive parsing to reach the right
API base and build the right push URL. The slug is the whole path after the
host, so it carries GitLab's nested groups (``group/subgroup/project``) too.

The returned host is what the HTTPS API and push URL are built from, so an
explicit port on an ``http(s)://`` remote is kept (the API/clone honor it) but a
port on an ``ssh://`` remote is dropped (the API and HTTPS clone go over the
default port, not the SSH one).
"""

from __future__ import annotations

import re
import urllib.parse

# scp-like short form: git@host:group/project.git (no scheme, no "//").
_SCP_RE = re.compile(r"^(?P<user>[^@/]+@)?(?P<host>[^:/]+):(?P<slug>.+)$")


def _clean_slug(path: str) -> str:
    return path.strip("/").removesuffix(".git")


def parse_remote(remote_url: str) -> tuple[str, str]:
    """(host, slug) from an SSH or HTTPS remote URL. ``slug`` is the full path
    after the host (``owner/name`` on GitHub, ``group/…/project`` on GitLab).
    Raises ValueError when neither form matches."""
    remote_url = remote_url.strip()
    if "://" in remote_url:
        parsed = urllib.parse.urlsplit(remote_url)
        if parsed.hostname and parsed.path.strip("/"):
            host = parsed.hostname
            # Keep an explicit port only for the HTTP(S) schemes, where the API
            # and push URL are built on it; an ssh:// port is irrelevant to both.
            if parsed.scheme in ("http", "https") and parsed.port:
                host = f"{host}:{parsed.port}"
            slug = _clean_slug(parsed.path)
            if slug:
                return host, slug
    else:
        m = _SCP_RE.match(remote_url)
        if m:
            return m.group("host"), _clean_slug(m.group("slug"))
    raise ValueError(f"cannot parse host/slug from remote url {remote_url!r}")
