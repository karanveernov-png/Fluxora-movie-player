"""
media/compatibility.py — Playback compatibility engine.

Implements the core decision pipeline:
  Native Direct Play → Remux → Transcode → Failure

Browser codec support tables are based on:
  • MDN "Media type and format guide"
  • Can I Use data for H.264, HEVC, VP9, AV1
  • Testing against Chrome 120+, Firefox 121+, Safari 17+

Design principle:
  NEVER transcode when the browser can decode natively.
  NEVER remux when native playback works.
  Be conservative about what we claim the browser supports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.media.probe import MediaInfo, StreamInfo


class PlaybackStrategy(str, Enum):
    DIRECT = "direct"           # browser plays original source unchanged
    REMUX = "remux"             # container swap, no re-encoding (fast)
    TRANSCODE = "transcode"     # full video re-encoding (slow, broad support)
    HLS_TRANSCODE = "hls_transcode"  # segment + transcode into HLS
    UNSUPPORTED = "unsupported" # cannot play even after processing


@dataclass(frozen=True)
class CompatibilityResult:
    strategy: PlaybackStrategy
    reason: str                 # human-readable explanation
    # Populated only for REMUX / TRANSCODE / HLS_TRANSCODE
    target_container: str = ""  # "mp4", "hls"
    target_video_codec: str = ""  # "copy", "libx264", "libx265"
    target_audio_codec: str = ""  # "copy", "aac", "libopus"
    # For the info panel
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    needs_processing: bool = False


# ── Browser codec support tables ──────────────────────────────────────────────
# Conservative: only include codecs that work WITHOUT special flags across
# the majority of Chrome + Firefox + Safari installs.

# Video codecs that all major browsers support natively (no DRM required)
_BROWSER_VIDEO_CODECS = {
    "h264",      # baseline/main/high – universal
    "vp8",       # WebM – Chrome, Firefox, Safari 16.4+
    "vp9",       # WebM – Chrome, Firefox, Safari 14+
    "av1",       # Chrome 70+, Firefox 67+, Safari 17+
    "theora",    # Ogg – Chrome, Firefox (rare in practice)
}

# Codecs that Safari supports but Chrome/Firefox may not (HEVC needs HW)
# We do NOT include hevc/h265 as universally supported
_SAFARI_ONLY_VIDEO = {
    "hevc",
    "h265",
}

# Audio codecs that all major browsers support
_BROWSER_AUDIO_CODECS = {
    "aac",
    "mp3",
    "vorbis",
    "opus",
    "flac",      # Chrome 56+, Firefox 51+, Safari 11+
    "pcm_s16le", "pcm_s24le", "pcm_f32le",  # WAV variants
}

# Audio codecs that need transcoding (common in MKV/AVI/MKV releases)
_TRANSCODE_AUDIO = {
    "dts", "dca",
    "ac3", "eac3",      # Dolby Digital — Safari only, not Chrome/Firefox
    "truehd",
    "mlp",
    "wmav1", "wmav2",
}

# Containers directly playable by the <video> element.
# NOTE: "matroska,webm" (ffprobe's identifier for MKV files) is intentionally
# excluded.  MKV is not supported by Chrome/Firefox without plugins; only true
# WebM files (reported as "webm" by ffprobe) are browser-native.  MKV content
# with compatible codecs is handled by the REMUX branch.
_BROWSER_CONTAINERS = {
    "mov,mp4,m4a,3gp,3g2,mj2",  # ffprobe name for MP4/MOV family
    "webm",                       # pure WebM (VP8/VP9/AV1 + Vorbis/Opus)
    "ogg",
    "mpegts",                     # some browsers play TS directly
}

# Containers that need remuxing but whose streams may be kept
_REMUX_CONTAINERS = {
    "matroska,webm",  # MKV with H.264/AAC → MP4 remux (no re-encode)
    "avi",
    "asf",
    "flv",
    "mpeg",
    "rm",
    "rmvb",
    "3gp",
}


def check_compatibility(info: MediaInfo) -> CompatibilityResult:
    """
    Given probe metadata, return the optimal playback strategy.

    Hierarchy:
      1. DIRECT   — container + all codecs natively supported
      2. REMUX    — codec supported, only container needs changing
      3. TRANSCODE — video codec unsupported, re-encode to H.264
      4. UNSUPPORTED — media is corrupt or truly unplayable
    """
    v = info.video_codec.lower() if info.video_codec else ""
    a = info.audio_codec.lower() if info.audio_codec else ""
    c = info.container.lower() if info.container else ""

    base = dict(video_codec=v, audio_codec=a, container=c)

    # ── No probe data (ffprobe unavailable / network stream / HLS) ──────────
    if not v and not a:
        return CompatibilityResult(
            strategy=PlaybackStrategy.DIRECT,
            reason="No codec data available — attempting direct play.",
            **base,
        )

    video_ok = _video_supported(v)
    audio_ok  = _audio_supported(a)
    container_ok = _container_supported(c)

    # ── 1. DIRECT ─────────────────────────────────────────────────────────────
    if video_ok and audio_ok and container_ok:
        return CompatibilityResult(
            strategy=PlaybackStrategy.DIRECT,
            reason=f"Browser natively supports {v}/{a} in {_friendly_container(c)}.",
            **base,
        )

    # ── 2. REMUX ──────────────────────────────────────────────────────────────
    # Video codec IS browser-compatible but container is not.
    # Remux to MP4 without re-encoding — fast, lossless.
    if video_ok and audio_ok and not container_ok:
        return CompatibilityResult(
            strategy=PlaybackStrategy.REMUX,
            reason=(
                f"{_friendly_container(c)} container is not browser-playable, "
                f"but {v}/{a} codecs are compatible. Remuxing to MP4 (no re-encode)."
            ),
            target_container="mp4",
            target_video_codec="copy",
            target_audio_codec="copy",
            needs_processing=True,
            **base,
        )

    # ── 2b. REMUX with audio transcode ────────────────────────────────────────
    # Video codec OK but audio needs conversion (e.g. DTS → AAC)
    if video_ok and not audio_ok:
        return CompatibilityResult(
            strategy=PlaybackStrategy.REMUX,
            reason=(
                f"{a.upper()} audio is not browser-compatible. "
                f"Remuxing {v} video (no re-encode) and transcoding audio to AAC."
            ),
            target_container="mp4",
            target_video_codec="copy",
            target_audio_codec="aac",
            needs_processing=True,
            **base,
        )

    # ── 3. TRANSCODE ──────────────────────────────────────────────────────────
    # Video codec is unsupported (HEVC, AV1 on older browsers, etc.)
    # Full video transcode. Use HLS for large files / live streams.
    large_file = info.size > 500 * 1024 * 1024  # > 500 MB
    long_video  = info.duration > 1800           # > 30 minutes

    if large_file or long_video:
        return CompatibilityResult(
            strategy=PlaybackStrategy.HLS_TRANSCODE,
            reason=(
                f"{v.upper()} video is not browser-compatible. "
                f"Transcoding to H.264/AAC delivered as HLS "
                f"({'large file' if large_file else 'long video'})."
            ),
            target_container="hls",
            target_video_codec="libx264",
            target_audio_codec="aac" if not audio_ok else "copy",
            needs_processing=True,
            **base,
        )

    return CompatibilityResult(
        strategy=PlaybackStrategy.TRANSCODE,
        reason=(
            f"{v.upper()} video is not browser-compatible. "
            f"Transcoding to H.264/AAC MP4."
        ),
        target_container="mp4",
        target_video_codec="libx264",
        target_audio_codec="aac" if not audio_ok else "copy",
        needs_processing=True,
        **base,
    )


def check_stream_compatibility(
    video_stream: StreamInfo | None,
    audio_stream: StreamInfo | None,
) -> CompatibilityResult:
    """Convenience wrapper for individual stream objects."""
    dummy = MediaInfo(
        url="",
        container="",
        duration=0,
        bit_rate=0,
        size=0,
        video_codec=video_stream.codec_name if video_stream else "",
        audio_codec=audio_stream.codec_name if audio_stream else "",
    )
    return check_compatibility(dummy)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _video_supported(codec: str) -> bool:
    return codec in _BROWSER_VIDEO_CODECS


def _audio_supported(codec: str) -> bool:
    if not codec:
        return True  # no audio track — fine
    return codec in _BROWSER_AUDIO_CODECS and codec not in _TRANSCODE_AUDIO


def _container_supported(container: str) -> bool:
    # Use exact equality.  Substring matching caused "webm" to match inside
    # "matroska,webm", making ffprobe's MKV container identifier pass as
    # browser-native even though MKV is not universally playable in Chrome/Firefox.
    # ffprobe container names are fixed, well-known strings, so exact match is safe.
    return container in _BROWSER_CONTAINERS


def _friendly_container(raw: str) -> str:
    mapping = {
        "mov,mp4,m4a,3gp,3g2,mj2": "MP4",
        "matroska,webm": "MKV/WebM",
        "webm": "WebM",
        "ogg": "OGG",
        "avi": "AVI",
        "asf": "ASF/WMV",
        "flv": "FLV",
    }
    for k, v in mapping.items():
        if k in raw or raw in k:
            return v
    return raw.upper() or "Unknown"
