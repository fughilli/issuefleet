"""Fetch images referenced in issue, comment and PR text into a worker's worktree.

Workers have no network or credentials, so the orchestrator downloads images
host-side into ``<worktree>/.agent/attachments/`` and the prompt points at the
local files. Best-effort: anything that fails, is oversized, or isn't an image
is skipped and the original link stays in the text."""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from issuefleet.httpx import TIMEOUT_S, USER_AGENT

log = logging.getLogger("issuefleet.attachments")

MAX_BYTES = 12 * 1024 * 1024

_ATTACH_DIR = Path(".agent") / "attachments"

_CT_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".tif", ".ico"}

_LINEAR_HOST = "uploads.linear.app"
_GH_IMG_HOST_SUFFIX = "user-images.githubusercontent.com"


def _md_image_refs(text: str) -> list[str]:
    """Destinations from markdown image embeds ``![alt](url "title")`` and HTML
    ``<img src=...>`` — deliberate image references, fetched regardless of host.
    May be absolute URLs or site-relative paths (the caller decides what to do).
    Hand-parsed rather than regex'd so nested parens in a URL don't truncate it.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while True:
        j = text.find("![", i)
        if j == -1:
            break
        close = text.find("]", j + 2)
        if close == -1 or close + 1 >= n or text[close + 1] != "(":
            i = j + 2
            continue
        url = _scan_paren_url(text, close + 2)
        if url is not None:
            out.append(url)
        i = close + 2
    low = text.lower()
    k = 0
    while True:
        t = low.find("<img", k)
        if t == -1:
            break
        end = low.find(">", t)
        seg = text[t : (end if end != -1 else n)]
        url = _attr_value(seg, "src")
        if url:
            out.append(url)
        k = (end + 1) if end != -1 else n
    return out


def _scan_paren_url(text: str, start: int) -> str | None:
    """Read the URL out of a markdown link destination that begins at ``start``
    (just past the opening ``(``). Handles a leading ``<``-wrapped URL and an
    optional ``"title"``; stops at the matching ``)``."""
    i = start
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i < n and text[i] == "<":
        end = text.find(">", i + 1)
        return text[i + 1 : end].strip() if end != -1 else None
    j = i
    while j < n and not text[j].isspace() and text[j] != ")":
        j += 1
    url = text[i:j].strip()
    return url or None


def _attr_value(tag: str, attr: str) -> str | None:
    low = tag.lower()
    a = low.find(attr + "=")
    if a == -1:
        return None
    v = tag[a + len(attr) + 1 :]
    if not v:
        return None
    q = v[0]
    if q in "\"'":
        end = v.find(q, 1)
        return v[1:end] if end != -1 else None
    end = 0
    while end < len(v) and not v[end].isspace() and v[end] != ">":
        end += 1
    return v[:end] or None


def is_attachment_url(url: str) -> bool:
    """True for a bare URL that clearly points at hosted attachment media, so
    it's worth fetching even without markdown-image syntax around it."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host == _LINEAR_HOST:
        return True
    if host.endswith(_GH_IMG_HOST_SUFFIX):
        return True
    if host == "github.com" and path.startswith("/user-attachments/"):
        return True
    if "/uploads/" in path:
        return True
    return _has_image_ext(url)


def _has_image_ext(url: str) -> bool:
    try:
        return Path(urlsplit(url).path).suffix.lower() in _IMG_EXTS
    except ValueError:
        return False


def extract_image_refs(text: str) -> list[str]:
    """Image references worth downloading, order-preserving and deduped.
    Embeds are always taken; bare links only for known attachment hosts or image
    extensions. Absolute URLs and site-relative paths (GitLab's ``/uploads/…``)
    are kept; everything else is dropped."""
    if not text:
        return []
    refs = list(_md_image_refs(text))
    for tok in _bare_urls(text):
        if is_attachment_url(tok):
            refs.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        r = r.strip()
        if not r or r in seen:
            continue
        low = r.lower()
        if not (low.startswith(("http://", "https://")) or r.startswith("/")):
            continue
        seen.add(r)
        out.append(r)
    return out


def _bare_urls(text: str) -> list[str]:
    """Whitespace-delimited http(s) tokens, with trailing markdown/prose
    punctuation trimmed. Deliberately simple: the caller filters to attachment
    URLs, so over-matching prose links is harmless."""
    out: list[str] = []
    for raw in text.replace("<", " ").replace(">", " ").split():
        low = raw.lower()
        pos = low.find("http://")
        if pos == -1:
            pos = low.find("https://")
        if pos == -1:
            continue
        tok = raw[pos:].rstrip(").,'\"];!")
        tok = tok.lstrip("(")
        if tok:
            out.append(tok)
    return out


class _AuthStrippingRedirect(urllib.request.HTTPRedirectHandler):
    """Drop credential headers when a redirect crosses to another host. Linear
    and GitHub attachment URLs redirect to signed storage URLs on a different
    host; urllib otherwise re-sends ``Authorization`` verbatim, leaking the
    workspace/forge token to that third party. Same protection requests gives."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            for h in list(new.headers):
                if h.lower() in ("authorization", "private-token"):
                    del new.headers[h]
        return new


_OPENER = urllib.request.build_opener(_AuthStrippingRedirect)


def _default_fetch(url: str, headers: dict[str, str]) -> tuple[str, bytes]:
    """Binary GET returning ``(content_type, data)``. Credential headers are
    dropped on a cross-host redirect, and one byte past the cap is read so an
    oversized body is caught without trusting Content-Length."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8", **headers}
    )
    with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
        ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read(MAX_BYTES + 1)
    return ct, data


_GENERIC_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
)


def _ext_for(url: str, content_type: str) -> str | None:
    """The extension to save under, or None if not an image. A concrete non-image
    type is rejected whatever the URL suffix, because GitLab's web route serves its
    sign-in HTML at a ``.png`` URL. The suffix is trusted only for image/* and
    octet-stream, which the uploads API serves."""
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return _CT_EXT.get(ct) or _url_img_suffix(url) or ".img"
    if ct in _GENERIC_TYPES:
        return _url_img_suffix(url)
    return None


def _url_img_suffix(url: str) -> str | None:
    try:
        suffix = Path(urlsplit(url).path).suffix.lower()
    except ValueError:
        return None
    if suffix in _IMG_EXTS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def _default_resolve(ref: str) -> str | None:
    """The fetch URL for a raw reference: an absolute ``http(s)`` URL as-is, and
    nothing else (a site-relative path needs a host-aware resolver to become
    fetchable). Callers with forge context pass their own."""
    return ref if ref.lower().startswith(("http://", "https://")) else None


_GL_UPLOADS_RE = re.compile(r"/uploads/(?P<secret>[^/]+)/(?P<file>[^/?#]+)")


def gitlab_resolver(host: str, api_root: str, project_id: str):
    """A ``resolve(ref)`` that rewrites GitLab upload references, relative or the
    absolute web URL, to ``<api_root>/projects/<id>/uploads/<secret>/<file>``; the
    web route ignores ``PRIVATE-TOKEN`` and serves a sign-in page. Other absolute
    URLs pass through; anything else is dropped."""

    def resolve(ref: str) -> str | None:
        low = ref.lower()
        if low.startswith(("http://", "https://")):
            parts = urlsplit(ref)
            if (parts.hostname or "").lower() == (host or "").lower():
                m = _GL_UPLOADS_RE.search(parts.path)
                if m:
                    return f"{api_root}/projects/{project_id}/uploads/{m['secret']}/{m['file']}"
            return ref
        if ref.startswith("/"):
            m = _GL_UPLOADS_RE.search(ref)
            if m:
                return f"{api_root}/projects/{project_id}/uploads/{m['secret']}/{m['file']}"
        return None

    return resolve


def download_images(
    text: str,
    worktree: str | Path,
    auth_for_url=None,
    fetch=None,
    resolve=None,
) -> list[str]:
    """Download every image referenced in ``text`` into ``.agent/attachments/`` and
    return worktree-relative paths, order-preserving and deduped. ``resolve(ref)``
    maps a reference to the URL to fetch, or None to skip; ``auth_for_url(url)``
    returns request headers. Per-image failures are logged and skipped."""
    refs = extract_image_refs(text)
    if not refs:
        return []
    resolve = resolve or _default_resolve
    fetch = fetch or _default_fetch
    dest_dir = Path(worktree) / _ATTACH_DIR
    saved: list[str] = []
    seen_urls: set[str] = set()
    for ref in refs:
        url = resolve(ref)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        rel = _download_one(url, dest_dir, Path(worktree), auth_for_url, fetch)
        if rel and rel not in saved:
            saved.append(rel)
    return saved


def _download_one(url, dest_dir: Path, worktree: Path, auth_for_url, fetch) -> str | None:
    stem = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    for existing in dest_dir.glob(f"{stem}.*"):
        return existing.relative_to(worktree).as_posix()
    try:
        headers = (auth_for_url(url) if auth_for_url else None) or {}
        content_type, data = fetch(url, headers)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("attachment: could not fetch %s (%s)", url, e)
        return None
    if len(data) > MAX_BYTES:
        log.warning("attachment: %s exceeds %d bytes; skipping", url, MAX_BYTES)
        return None
    if not data:
        log.warning("attachment: %s returned no data; skipping", url)
        return None
    ext = _ext_for(url, content_type)
    if ext is None:
        log.info("attachment: %s is not an image (%s); leaving the link", url, content_type or "?")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{stem}{ext}"
    tmp = dest_dir / f".{stem}{ext}.tmp"
    tmp.write_bytes(data)
    tmp.replace(path)
    return path.relative_to(worktree).as_posix()


def render_image_block(images: list[str]) -> str:
    """The prompt snippet that tells the worker where downloaded images live and
    to actually open them. Empty string when there are none, so callers can
    unconditionally append it."""
    if not images:
        return ""
    lines = "\n".join(f"- {p}" for p in images)
    return (
        "Attached image(s) were posted with this text and downloaded into your "
        "worktree. Open each with the Read tool to view it:\n" + lines
    )
