"""
services/source.py — Legacy source validation and HTTP metadata extraction.

These functions back the v1 /player/ endpoints kept for backward compatibility.
SSRF protection is now applied via url_validator before any HTTP request.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.url_validator import validate_url, URLValidationError, ssrf_safe_client
from app.schemas.media import (
    SourceRequest, SourceResponse,
    MetadataRequest, MetadataResponse,
    SourceType,
)

log = get_logger(__name__)

_HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
_MP4_CONTENT_TYPES = {
    "video/mp4", "video/quicktime", "video/x-m4v",
    "video/webm", "video/ogg", "video/x-msvideo",
}


def _detect_from_url(url: str) -> SourceType:
    lower = url.lower().split("?")[0]
    if ".m3u8" in lower or "/hls/" in lower or "/live/" in lower:
        return SourceType.hls
    if ".mpd" in lower:
        return SourceType.dash
    return SourceType.mp4


def _detect_from_ct(ct: str) -> SourceType | None:
    ct_lower = ct.lower().split(";")[0].strip()
    if ct_lower in _HLS_CONTENT_TYPES:
        return SourceType.hls
    if ct_lower in _MP4_CONTENT_TYPES:
        return SourceType.mp4
    if "dash" in ct_lower or "mpd" in ct_lower:
        return SourceType.dash
    return None


def _client() -> httpx.AsyncClient:
    """
    SSRF-safe HTTP client for source validation and metadata probing.

    Uses ssrf_safe_client() which attaches a post-redirect event hook so that
    a server responding with a 301/302 to a private or metadata address is
    rejected before the second connection is made.  validate_url() guards the
    initial hostname; this guard covers redirect targets.
    """
    return ssrf_safe_client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.probe_user_agent},
    )


async def validate_source(req: SourceRequest) -> SourceResponse:
    url = req.url.strip()

    # SSRF guard
    try:
        validate_url(url)
    except URLValidationError as e:
        return SourceResponse(valid=False, message=str(e))

    detected_type = req.type or _detect_from_url(url)

    async with _client() as client:
        try:
            resp = await client.head(url)
            if resp.status_code == 405:
                resp = await client.get(url, headers={"Range": "bytes=0-0"})

            content_type = resp.headers.get("content-type", "")
            ct_type = _detect_from_ct(content_type) or detected_type
            ok = resp.status_code in (200, 206)
            return SourceResponse(
                valid=ok, type=ct_type, reachable=ok,
                status_code=resp.status_code,
                content_type=content_type or None,
                message=None if ok else f"Server returned HTTP {resp.status_code}",
            )
        except httpx.ConnectError:
            return SourceResponse(valid=False, reachable=False, type=detected_type,
                                  message="Connection refused.")
        except httpx.TimeoutException:
            return SourceResponse(valid=False, reachable=False, type=detected_type,
                                  message=f"Request timed out after {settings.http_timeout}s.")
        except httpx.RequestError:
            return SourceResponse(valid=False, reachable=False, type=detected_type,
                                  message="Network or connection error.")


async def get_metadata(req: MetadataRequest) -> MetadataResponse:
    url = req.url.strip()

    try:
        validate_url(url)
    except URLValidationError as e:
        return MetadataResponse(url=url, reachable=False, message=str(e))

    detected_type = req.type or _detect_from_url(url)

    async with _client() as client:
        try:
            resp = await client.head(url)
            if resp.status_code == 405:
                resp = await client.get(url, headers={"Range": "bytes=0-0"})

            h = resp.headers
            content_type = h.get("content-type", "")
            ct_type = _detect_from_ct(content_type) or detected_type
            raw_length = h.get("content-length")
            content_length = int(raw_length) if raw_length and raw_length.isdigit() else None
            accept_ranges = h.get("accept-ranges", "").lower() == "bytes"
            server = h.get("server") or h.get("x-served-by") or None
            ok = resp.status_code in (200, 206)

            return MetadataResponse(
                url=url, type=ct_type,
                content_type=content_type or None,
                content_length=content_length,
                accept_ranges=accept_ranges,
                server=server, reachable=ok,
                message=None if ok else f"HTTP {resp.status_code}",
            )
        except (httpx.RequestError, httpx.TimeoutException):
            return MetadataResponse(url=url, type=detected_type, reachable=False,
                                    message="Network or connection error.")
