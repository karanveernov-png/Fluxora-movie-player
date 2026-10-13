"""
config.py — Centralised application configuration.

All tuneable values live here. Override via environment variables prefixed AURA_
or a .env file in the backend directory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = [
        "http://localhost:5173",  # Vite dev
        "http://localhost:4173",  # Vite preview
        "http://localhost:3000",
    ]

    # ── Trusted hosts ─────────────────────────────────────────────────────────
    # Host header allow-list, checked before routing. "*" (default) accepts any
    # Host header, matching this app's historical behaviour for local dev. In
    # production set AURA_TRUSTED_HOSTS to a comma-separated list of the real
    # domain(s) this API is served on (e.g. "api.example.com") to reject
    # requests with a forged/unexpected Host header.
    trusted_hosts: list[str] = ["*"]

    # ── HTTP probe ────────────────────────────────────────────────────────────
    http_timeout: float = 10.0
    probe_user_agent: str = "AuraPlayer/2.0 (media probe; not archiving)"
    max_url_length: int = 2048
    max_redirects: int = 5

    # ── SSRF protection ───────────────────────────────────────────────────────
    # Block requests to private/loopback ranges.
    block_private_ips: bool = True

    # ── FFprobe ───────────────────────────────────────────────────────────────
    ffprobe_path: str = "ffprobe"          # set to absolute path if not in PATH
    ffprobe_timeout: float = 15.0          # seconds per invocation
    ffprobe_cache_ttl: int = 3600          # seconds to cache probe results

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_max_jobs: int = 4               # concurrent transcode/remux jobs
    ffmpeg_job_timeout: float = 300.0      # max seconds for a single job
    transcode_crf: int = 23               # H.264 quality (18=near-lossless, 28=fast)
    transcode_preset: str = "veryfast"     # x264 preset
    hls_segment_duration: int = 4          # HLS segment target duration (seconds)
    hls_segment_count: int = 10            # number of segments to pre-generate

    # ── Temp / cache ──────────────────────────────────────────────────────────
    temp_dir: Path = Path(tempfile.gettempdir()) / "aura_player"
    cache_dir: Path = Path(tempfile.gettempdir()) / "aura_cache"
    # Soft storage budget: actual usage can temporarily exceed this by up to
    # ffmpeg_max_jobs × largest_single_output because the check runs before
    # each job, not during it. See media/cleanup.py for full explanation.
    max_temp_size_mb: int = 2048           # soft temp storage budget (MB)
    remux_cache_ttl: int = 600             # seconds before evicting remuxed files

    # ── Streaming ─────────────────────────────────────────────────────────────
    stream_chunk_size: int = 512 * 1024    # bytes per streaming chunk (512 KB)
    stream_buffer_size: int = 8 * 1024 * 1024  # read buffer for range requests

    # ── Rate limiting ─────────────────────────────────────────────────────────
    # Expensive endpoints (probe, play, proxy session creation) — per client IP.
    # Format: "N/period" e.g. "20/minute", "5/second", "100/hour".
    # HLS segment serving is intentionally NOT rate-limited here (high-frequency
    # legitimate traffic from the player).
    rate_limit_probe: str = "30/minute"      # ffprobe invocations
    rate_limit_play: str = "30/minute"       # full pipeline decisions
    rate_limit_proxy_session: str = "20/minute"  # proxy session creation
    rate_limit_remux: str = "10/minute"      # remux/transcode submissions
    rate_limit_rtmp: str = "10/minute"       # RTMP relay start
    rate_limit_enabled: bool = True

    model_config = ConfigDict(env_prefix="AURA_", env_file=".env")


settings = Settings()

# Ensure work directories exist at import time
settings.temp_dir.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
