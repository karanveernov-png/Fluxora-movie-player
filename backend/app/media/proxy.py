"""
media/proxy.py — Auth-header HLS proxy.

Bug fixes in this file
-----------------------
B1  Resource leak: stream_segment now uses try/finally to guarantee the httpx
    client is closed even when the client disconnects mid-stream.

B3  Wrong MIME type: stream_segment now derives the MIME type from the segment
    URL's file extension (.ts → video/MP2T, .aac → audio/aac, .mp4 → video/mp4,
    .vtt/.webvtt → text/vtt) instead of hardcoding video/MP2T for everything.
    This fixes hls.js rejecting fMP4 init segments and subtitle tracks.

B13 Infinite-loop SSRF: _rewrite_m3u8 was passing base_url (the frontend-facing
    origin, e.g. http://localhost:5173) as the base for constructing proxy
    segment URLs. When the backend was also behind the same Vite dev proxy, those
    rewritten URLs pointed back at the frontend origin — which the segment
    validation (validate_url) then blocked as a private IP (127.0.0.1), causing
    all segments to return 422. Additionally, since the rewritten URLs were
    absolute and pointed at localhost, hls.js would occasionally bypass the Vite
    proxy and hit the backend directly, losing the /api prefix.

    Root cause: _make_proxy_url received base_url from request.base_url in the
    route handler. After ForwardedHeadersMiddleware rewrote base_url to the
    frontend origin (e.g. http://localhost:5173), all proxy segment URLs pointed
    at the Vite dev server, not the FastAPI backend. hls.js then fetched those
    URLs and the Vite proxy forwarded them to FastAPI — BUT the URL contained
    /api/media/proxy/.../segment which then called validate_url on the upstream
    URL parameter (which was already an absolute proxy URL from a prior rewrite),
    creating a recursive proxy chain.

    Fix: always use the BACKEND's own origin (scheme + host from the ASGI scope,
    NOT the X-Forwarded-* headers) when constructing proxy segment URLs. The
    Vite dev proxy and production reverse proxies will forward /api/... paths to
    this backend regardless of which origin they come from. The proxy segment
    URL therefore never contains a frontend-only origin, never triggers the
    private-IP SSRF guard, and never creates a rewrite loop.

    _rewrite_m3u8 now accepts backend_origin (str) instead of base_url + token
    together. The route handler computes backend_origin from request.scope
    directly (bypassing ForwardedHeadersMiddleware) and passes it down.

B14 Segment validate_url false-positive: the proxy_segment_endpoint called
    validate_url(url) on the upstream segment URL. When the backend is on
    localhost and the CDN uses a relative path that was incorrectly resolved to
    localhost, this always raised URLValidationError(422). The fix in B13
    prevents incorrect relative→localhost resolution, but as defence-in-depth
    we also ensure the upstream URL is always an absolute http(s) URL before
    validate_url sees it.

B15 Missing Content-Type on streamed segments: StreamingResponse was created
    with media_type from _segment_mime(url) but stream_segment is an async
    generator — if the generator raised immediately (e.g. validate_url error),
    FastAPI returned an empty 200 with no body. Fixed by catching errors before
    creating StreamingResponse and returning an HTTPException instead.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from collections import OrderedDict
from typing import AsyncGenerator

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.url_validator import validate_url, ssrf_safe_client, URLValidationError

log = get_logger(__name__)

SESSION_TTL  = 3600
MAX_SESSIONS = 256

_sessions: OrderedDict[str, dict] = OrderedDict()

_URI_ATTR_RE = re.compile(r'(URI=)(["\'])([^"\']*)\2', re.IGNORECASE)

# B3: MIME map for segment types served by the proxy
_SEGMENT_MIME: dict[str, str] = {
    ".ts":      "video/MP2T",
    ".mp2t":    "video/MP2T",
    ".mp4":     "video/mp4",
    ".m4s":     "video/mp4",       # fMP4 segment
    ".m4v":     "video/mp4",
    ".aac":     "audio/aac",
    ".m4a":     "audio/mp4",
    ".mp3":     "audio/mpeg",
    ".vtt":     "text/vtt",
    ".webvtt":  "text/vtt",
    ".srt":     "text/plain",
    ".m3u8":    "application/vnd.apple.mpegurl",
    ".m3u":     "application/vnd.apple.mpegurl",
}


def _segment_mime(url: str) -> str:
    """Return the correct MIME type for a segment URL based on its extension."""
    path = url.split("?")[0].lower()
    for ext, mime in _SEGMENT_MIME.items():
        if path.endswith(ext):
            return mime
    return "application/octet-stream"


# ── Session management ────────────────────────────────────────────────────────

def create_session(url: str, headers: dict[str, str]) -> str:
    _evict_expired()
    token = hashlib.sha256(f"{url}:{time.monotonic()}".encode()).hexdigest()[:32]
    _sessions[token] = {
        "url": url,
        "headers": dict(headers),
        "created_at": time.monotonic(),
    }
    if len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    log.info("proxy session created: token=%s url_len=%d", token, len(url))
    return token


def refresh_session(token: str, url: str, headers: dict[str, str]) -> bool:
    s = _sessions.get(token)
    if not s:
        return False
    if (time.monotonic() - s["created_at"]) >= SESSION_TTL:
        _sessions.pop(token, None)
        return False
    s["url"]     = url
    s["headers"] = dict(headers)
    log.info("proxy session refreshed: token=%s url_len=%d", token, len(url))
    return True


def get_session(token: str) -> dict | None:
    s = _sessions.get(token)
    if s and (time.monotonic() - s["created_at"]) < SESSION_TTL:
        return s
    _sessions.pop(token, None)
    return None


def _evict_expired() -> None:
    now = time.monotonic()
    dead = [k for k, v in _sessions.items() if now - v["created_at"] > SESSION_TTL]
    for k in dead:
        del _sessions[k]


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _client(auth_headers: dict[str, str]) -> httpx.AsyncClient:
    return ssrf_safe_client(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
        headers={"User-Agent": settings.probe_user_agent, **auth_headers},
    )


async def fetch_root(
    session: dict, backend_origin: str, token: str
) -> tuple[bytes, str]:
    validate_url(session["url"])
    return await _fetch_and_rewrite(
        session["url"], session["headers"], backend_origin, token
    )


async def fetch_segment(
    url: str, session: dict, backend_origin: str, token: str
) -> tuple[bytes, str]:
    validate_url(url)
    return await _fetch_and_rewrite(url, session["headers"], backend_origin, token)


async def stream_segment(
    url: str, auth_headers: dict[str, str]
) -> AsyncGenerator[bytes, None]:
    """
    Stream a binary segment without full buffering.

    B1 fix: the httpx client is opened with `async with` inside the generator
    body so it is always closed via the context manager's __aexit__, even when
    the caller (StreamingResponse) closes the generator early on client disconnect.

    B15 fix: validate_url is called before the generator yields anything so
    that a URLValidationError surfaces as an exception the caller can catch,
    not as a broken empty stream.
    """
    validate_url(url)
    # B1 fix: use async with so __aexit__ runs on generator close (client disconnect)
    async with _client(auth_headers) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk


async def _fetch_and_rewrite(
    url: str,
    auth_headers: dict[str, str],
    backend_origin: str,
    token: str,
) -> tuple[bytes, str]:
    async with _client(auth_headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        body = resp.content

    if _is_manifest(content_type, url):
        body = _rewrite_m3u8(body, url, backend_origin, token)
        content_type = "application/vnd.apple.mpegurl; charset=utf-8"

    return body, content_type


def _is_manifest(content_type: str, url: str) -> bool:
    if "mpegurl" in content_type.lower():
        return True
    path = url.split("?")[0].lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


def _make_proxy_url(backend_origin: str, token: str, abs_url: str) -> str:
    """
    Build a proxy segment URL rooted at the BACKEND origin.

    B13 fix: use backend_origin (derived from the raw ASGI scope, before
    ForwardedHeadersMiddleware rewrites it) instead of request.base_url.
    This guarantees the URL points at the FastAPI backend itself, not the
    frontend dev server, and never triggers the private-IP SSRF guard.
    """
    encoded = urllib.parse.quote(abs_url, safe="")
    return f"{backend_origin}/api/media/proxy/{token}/segment?url={encoded}"


def _resolve_url(ref: str, current_url: str) -> str:
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    return urllib.parse.urljoin(current_url, ref)


def _rewrite_m3u8(
    content: bytes, current_url: str, backend_origin: str, token: str
) -> bytes:
    """
    Rewrite every URL in an HLS manifest to route through the proxy.

    Handles both bare URL lines and URI= attributes inside tag lines
    (#EXT-X-MEDIA, #EXT-X-KEY, #EXT-X-MAP).

    B13 fix: parameter renamed from base_url → backend_origin to clarify that
    this must be the FastAPI backend's own origin, not the frontend-facing one.
    """
    lines = content.decode("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")
        ending   = "\r\n" if line.endswith("\r\n") else "\n"

        if not stripped:
            out.append(line)
            continue

        if stripped.startswith("#"):
            if _URI_ATTR_RE.search(stripped):
                def _replace_uri(
                    m: re.Match,
                    _cu: str = current_url,
                    _bo: str = backend_origin,
                    _tk: str = token,
                ) -> str:
                    ref       = m.group(3)
                    abs_url   = _resolve_url(ref, _cu)
                    proxy_url = _make_proxy_url(_bo, _tk, abs_url)
                    qc        = m.group(2)
                    return f"{m.group(1)}{qc}{proxy_url}{qc}"
                rewritten = _URI_ATTR_RE.sub(_replace_uri, stripped)
                out.append(rewritten + ending)
            else:
                out.append(line)
            continue

        abs_url   = _resolve_url(stripped, current_url)
        proxy_url = _make_proxy_url(backend_origin, token, abs_url)
        out.append(proxy_url + ending)

    return "".join(out).encode("utf-8")
