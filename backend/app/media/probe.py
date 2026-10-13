"""
media/probe.py — FFprobe wrapper.

Responsibilities:
  • Run ffprobe safely (no shell=True, arguments are a list)
  • Parse JSON output into a structured MediaInfo model
  • Cache results in memory (TTL-based) to avoid redundant invocations
  • Distinguish between "ffprobe not installed" and "media unreadable"

Design decisions:
  • Never shell-interpolate user-supplied URLs — always pass as argv element
  • Timeout every ffprobe invocation (FFPROBE_TIMEOUT setting)
  • Cache key = SHA-256(url) so long URLs don't create filesystem issues
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str          # "video" | "audio" | "subtitle"
    codec_name: str          # "h264", "aac", "hevc", "dts", …
    codec_long_name: str
    # Video-specific
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bit_rate: int | None = None
    pix_fmt: str | None = None
    # Audio-specific
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    # Common
    language: str | None = None
    title: str | None = None
    duration: float | None = None
    profile: str | None = None


@dataclass
class MediaInfo:
    url: str
    container: str               # e.g. "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2"
    duration: float              # seconds (0 if unknown/live)
    bit_rate: int                # overall bitrate bps (0 if unknown)
    size: int                    # file size bytes (0 if unknown/streaming)
    video_streams: list[StreamInfo] = field(default_factory=list)
    audio_streams: list[StreamInfo] = field(default_factory=list)
    subtitle_streams: list[StreamInfo] = field(default_factory=list)
    # Convenience (first video/audio stream)
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    probe_error: str | None = None   # set when ffprobe failed


# ── In-memory TTL cache ───────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    info: MediaInfo
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_MAX_CACHE_ENTRIES = 512   # B5 fix: cap to prevent unbounded memory growth


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _get_cached(url: str) -> MediaInfo | None:
    entry = _cache.get(_cache_key(url))
    if entry and time.monotonic() < entry.expires_at:
        log.debug("probe cache hit for %s", url[:80])
        return entry.info
    return None


def _set_cached(url: str, info: MediaInfo) -> None:
    # B5 fix: evict oldest entry when cache is at capacity
    if len(_cache) >= _MAX_CACHE_ENTRIES:
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]
    _cache[_cache_key(url)] = _CacheEntry(
        info=info,
        expires_at=time.monotonic() + settings.ffprobe_cache_ttl,
    )


def invalidate_cache(url: str) -> None:
    _cache.pop(_cache_key(url), None)


# ── FFprobe invocation ─────────────────────────────────────────────────────────

_FFPROBE_CMD = [
    "{ffprobe}",
    "-v", "quiet",
    "-print_format", "json",
    "-show_format",
    "-show_streams",
    "{url}",
]


async def probe(url: str, *, force: bool = False) -> MediaInfo:
    """
    Run ffprobe against `url` and return a MediaInfo.

    Uses cached result if available and `force=False`.
    Never raises — returns a MediaInfo with probe_error set on failure.
    """
    if not force:
        cached = _get_cached(url)
        if cached:
            return cached

    cmd = [
        settings.ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        url,          # ← url is an argv element, never interpolated into a shell string
    ]

    # Log only scheme+host+path, not query string (may contain signed tokens)
    from urllib.parse import urlparse as _urlparse
    _p = _urlparse(url)
    _safe_url = f"{_p.scheme}://{_p.netloc}{_p.path}"[:120]
    log.info("ffprobe: %s", _safe_url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.ffprobe_timeout,
        )
    except FileNotFoundError:
        log.warning("ffprobe not found at path: %s", settings.ffprobe_path)
        return MediaInfo(url=url, container="", duration=0, bit_rate=0, size=0,
                         probe_error="ffprobe is not installed or not in PATH")
    except asyncio.TimeoutError:
        log.warning("ffprobe timed out for %s", url[:80])
        return MediaInfo(url=url, container="", duration=0, bit_rate=0, size=0,
                         probe_error=f"ffprobe timed out after {settings.ffprobe_timeout}s")
    except Exception as exc:
        log.error("ffprobe unexpected error: %s", type(exc).__name__)
        return MediaInfo(url=url, container="", duration=0, bit_rate=0, size=0,
                         probe_error="Media probe encountered an unexpected error.")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        log.warning("ffprobe non-zero exit for %s: %s", url[:80], err[:500])
        # Return a generic probe_error — do not echo raw ffprobe stderr to the
        # browser as it can contain internal filesystem paths (e.g. the temp
        # dir, full ffprobe argv including the URL).
        return MediaInfo(url=url, container="", duration=0, bit_rate=0, size=0,
                         probe_error="Media probe failed — the URL may be unreachable or unsupported.")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return MediaInfo(url=url, container="", duration=0, bit_rate=0, size=0,
                         probe_error="Media probe returned unexpected output.")

    info = _parse_ffprobe(url, data)
    _set_cached(url, info)
    return info


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_ffprobe(url: str, data: dict[str, Any]) -> MediaInfo:
    fmt = data.get("format", {})
    streams_raw = data.get("streams", [])

    container   = fmt.get("format_name", "")
    duration    = _safe_float(fmt.get("duration"))
    bit_rate    = _safe_int(fmt.get("bit_rate"))
    size        = _safe_int(fmt.get("size"))

    video_streams: list[StreamInfo] = []
    audio_streams: list[StreamInfo] = []
    subtitle_streams: list[StreamInfo] = []

    for s in streams_raw:
        si = _parse_stream(s)
        if si.codec_type == "video":
            video_streams.append(si)
        elif si.codec_type == "audio":
            audio_streams.append(si)
        elif si.codec_type == "subtitle":
            subtitle_streams.append(si)

    v = video_streams[0] if video_streams else None
    a = audio_streams[0] if audio_streams else None

    return MediaInfo(
        url=url,
        container=container,
        duration=duration,
        bit_rate=bit_rate,
        size=size,
        video_streams=video_streams,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
        video_codec=v.codec_name if v else "",
        audio_codec=a.codec_name if a else "",
        width=v.width or 0 if v else 0,
        height=v.height or 0 if v else 0,
        fps=v.fps or 0.0 if v else 0.0,
        raw=data,
    )


def _parse_stream(s: dict[str, Any]) -> StreamInfo:
    tags = s.get("tags", {})

    # Frame rate: "30000/1001" → 29.97
    fps: float | None = None
    r_frame_rate = s.get("r_frame_rate", "")
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        if int(den) > 0:
            fps = int(num) / int(den)

    return StreamInfo(
        index=s.get("index", 0),
        codec_type=s.get("codec_type", ""),
        codec_name=s.get("codec_name", ""),
        codec_long_name=s.get("codec_long_name", ""),
        width=s.get("width"),
        height=s.get("height"),
        fps=fps,
        bit_rate=_safe_int(s.get("bit_rate")),
        pix_fmt=s.get("pix_fmt"),
        sample_rate=_safe_int(s.get("sample_rate")),
        channels=s.get("channels"),
        channel_layout=s.get("channel_layout"),
        language=tags.get("language"),
        title=tags.get("title"),
        duration=_safe_float(s.get("duration")),
        profile=s.get("profile"),
    )


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
