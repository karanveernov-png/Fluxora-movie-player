"""
schemas/media.py — Pydantic request/response models for the media API.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, field_validator


class SourceType(str, Enum):
    hls        = "hls"
    mp4        = "mp4"
    dash       = "dash"
    local      = "local"
    remuxed    = "remuxed"
    transcoded = "transcoded"
    hls_generated = "hls_generated"


class PlaybackStrategy(str, Enum):
    direct        = "direct"
    remux         = "remux"
    transcode     = "transcode"
    hls_transcode = "hls_transcode"
    unsupported   = "unsupported"


# ── URL validation shared ─────────────────────────────────────────────────────

class URLRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        if len(v) > 2048:
            raise ValueError("url exceeds 2048 character limit")
        return v


# ── Probe ─────────────────────────────────────────────────────────────────────

class ProbeRequest(URLRequest):
    force: bool = False   # bypass cache


class StreamInfoOut(BaseModel):
    index: int
    codec_type: str
    codec_name: str
    codec_long_name: str
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    bit_rate: Optional[int] = None
    pix_fmt: Optional[str] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    channel_layout: Optional[str] = None
    language: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None
    profile: Optional[str] = None


class ProbeResponse(BaseModel):
    url: str
    container: str
    duration: float
    bit_rate: int
    size: int
    video_codec: str
    audio_codec: str
    width: int
    height: int
    fps: float
    video_streams: list[StreamInfoOut]
    audio_streams: list[StreamInfoOut]
    subtitle_streams: list[StreamInfoOut]
    probe_error: Optional[str] = None


# ── Compatibility ─────────────────────────────────────────────────────────────

class CompatibilityRequest(URLRequest):
    pass


class CompatibilityResponse(BaseModel):
    url: str
    strategy: PlaybackStrategy
    reason: str
    target_container: str = ""
    target_video_codec: str = ""
    target_audio_codec: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    needs_processing: bool = False
    processing_url: Optional[str] = None


# ── Play decision ─────────────────────────────────────────────────────────────

class PlayRequest(URLRequest):
    """
    Full pipeline: probe → compat check → decide strategy → return play URL.
    This is the main endpoint the frontend calls for remote URLs.
    """
    hint_type: Optional[SourceType] = None


class PlayResponse(BaseModel):
    original_url: str
    play_url: str
    source_type: SourceType
    strategy: PlaybackStrategy
    reason: str
    probe: Optional[ProbeResponse] = None
    job_id: Optional[str] = None


# ── Auth-header proxy ─────────────────────────────────────────────────────────

class ProxySessionRequest(BaseModel):
    """Register auth headers for a URL. Returns a proxy token."""
    url: str
    headers: dict[str, str] = {}

    @field_validator("url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        if len(v) > 2048:
            raise ValueError("url exceeds 2048 character limit")
        return v

    @field_validator("headers")
    @classmethod
    def limit_headers(cls, v: dict) -> dict:
        if len(v) > 20:
            raise ValueError("too many headers (max 20)")
        # Reject dangerous header names
        blocked = {"host", "content-length", "transfer-encoding", "connection"}
        for k in v:
            if k.lower() in blocked:
                raise ValueError(f"header '{k}' cannot be overridden")
        return v


class ProxySessionResponse(BaseModel):
    token: str
    proxy_url: str    # load this as HLS source in the player


# ── RTMP/RTSP relay ───────────────────────────────────────────────────────────

class RtmpRelayRequest(BaseModel):
    rtmp_url: str

    @field_validator("rtmp_url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        return v.strip()


class RtmpRelayResponse(BaseModel):
    key: str
    play_url: str     # HLS playlist URL served by /api/media/hls/{key}/playlist.m3u8
    message: str = ""


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsState(BaseModel):
    allow_private_ips: bool


class SettingsUpdateRequest(BaseModel):
    allow_private_ips: Optional[bool] = None


# ── Validate source (keep backward compat) ────────────────────────────────────

class SourceRequest(URLRequest):
    type: Optional[SourceType] = None


class SourceResponse(BaseModel):
    valid: bool
    type: Optional[SourceType] = None
    reachable: Optional[bool] = None
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    message: Optional[str] = None


# ── Metadata (keep backward compat) ──────────────────────────────────────────

class MetadataRequest(URLRequest):
    type: Optional[SourceType] = None


class MetadataResponse(BaseModel):
    url: str
    type: Optional[SourceType] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None
    accept_ranges: Optional[bool] = None
    server: Optional[str] = None
    reachable: bool = False
    message: Optional[str] = None


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    ffprobe_available: bool
    ffmpeg_available: bool
    version: str = "2.0.0"
