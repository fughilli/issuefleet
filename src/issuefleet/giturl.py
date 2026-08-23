"""Parse a git remote URL into (host, slug).

Shared by the forge implementations and the forge factory: GitHub only ever
needs the ``owner/name`` slug (it always talks to github.com), but GitLab is
routinely self-hosted, so the host has to survive parsing to reach the right
API base and build the right push URL. The slug is the whole path after the
host, so it carries GitLab's nested groups (``group/subgroup/project``) too.
"""

from __future__ import annotations

import re

# scp-like (git@host:group/project.git) and ssh:// forms.
_SSH_RE = re.compile(r"^(?:ssh://)?git@(?P<host>[^:/]+)[:/](?P<slug>.+?)(?:\.git)?/?$")
# https:// / http:// forms, optionally with a user@ (e.g. oauth2@) prefix.
_HTTPS_RE = re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<slug>.+?)(?:\.git)?/?$")


def parse_remote(remote_url: str) -> tuple[str, str]:
    """(host, slug) from an SSH or HTTPS remote URL. ``slug`` is the full path
    after the host (``owner/name`` on GitHub, ``group/…/project`` on GitLab).
    Raises ValueError when neither form matches."""
    remote_url = remote_url.strip()
    for rx in (_SSH_RE, _HTTPS_RE):
        m = rx.match(remote_url)
        if m:
            return m.group("host"), m.group("slug")
    raise ValueError(f"cannot parse host/slug from remote url {remote_url!r}")
