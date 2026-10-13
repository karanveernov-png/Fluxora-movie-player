"""
media/rtmp_relay.py — RTMP / RTSP → HLS live relay via FFmpeg.

Starts an ffmpeg process that pulls an RTMP or RTSP stream and writes
live HLS segments under the same directory layout as hls_gen.py.
The existing /api/media/hls/{key}/{filename} endpoint serves the output
without modification.

Live stream behaviour
---------------------
• hls_flags delete_segments  — disk stays bounded; old .ts files auto-removed
• hls_list_size 6            — ~24 s of buffer at 4 s segments
• append_list                — playlist grows until FFmpeg exits (seekable on record)
• Video/audio copy           — no transcode; minimal CPU usage

One relay per URL
-----------------
If start_relay() is called again for the same URL while the process is
still running, the existing key is returned immediately (no duplicate).

Cleanup
-------
Call stop_relay(key) to send SIGTERM to the ffmpeg process.
Processes that exit on their own (stream ended, connection refused) are
detected via proc.returncode is not None.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.media.hls_gen import hls_dir

log = get_logger(__name__)

# Map relay key → running asyncio subprocess
_relay_procs: dict[str, asyncio.subprocess.Process] = {}


# ── Key / path helpers ────────────────────────────────────────────────────────

def relay_key(rtmp_url: str) -> str:
    """Stable 24-char key for an RTMP/RTSP URL (reuses hls_gen's dir layout)."""
    return hashlib.sha256(f"rtmp_relay:{rtmp_url}".encode()).hexdigest()[:24]


def relay_playlist(key: str) -> Path:
    return hls_dir(key) / "playlist.m3u8"


def is_running(key: str) -> bool:
    proc = _relay_procs.get(key)
    return proc is not None and proc.returncode is None


# ── Start / stop ──────────────────────────────────────────────────────────────

async def start_relay(rtmp_url: str) -> str:
    """
    Launch an ffmpeg RTMP/RTSP→HLS relay. Returns the job key immediately.
    If an identical relay is already running, returns the existing key.
    """
    key = relay_key(rtmp_url)

    if is_running(key):
        log.info("rtmp relay already running: key=%s", key)
        return key

    out_dir = hls_dir(key)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = out_dir / "playlist.m3u8"

    cmd = [
        settings.ffmpeg_path,
        "-loglevel", "warning",
        "-i", rtmp_url,
        # Copy streams — no transcode overhead for live relay
        "-c:v", "copy",
        "-c:a", "copy",
        # HLS muxer
        "-f", "hls",
        "-hls_time", str(settings.hls_segment_duration),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(out_dir / "seg%05d.ts"),
        str(playlist),
    ]

    # Don't log full RTMP URL — it may contain stream keys in the path
    log.info("starting relay: key=%s → %s", key, playlist)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _relay_procs[key] = proc

    # Drain stderr in background so the pipe never fills and blocks ffmpeg
    asyncio.create_task(_drain_stderr(key, proc))
    return key


async def wait_for_playlist(key: str, timeout: float = 20.0) -> bool:
    """
    Poll until the HLS playlist exists and has content.
    Returns True when ready, False on timeout or if ffmpeg exits early.
    """
    playlist = relay_playlist(key)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if playlist.exists() and playlist.stat().st_size > 0:
            return True
        proc = _relay_procs.get(key)
        if proc is not None and proc.returncode is not None:
            log.error("relay ffmpeg exited before playlist was ready: rc=%s", proc.returncode)
            return False
        await asyncio.sleep(0.4)
    log.warning("relay playlist not ready within %.1fs: key=%s", timeout, key)
    return False


async def stop_relay(key: str) -> None:
    """
    Terminate the ffmpeg relay process for the given key.

    Sends SIGTERM, waits up to 5 s, then sends SIGKILL.  Always awaits
    proc.wait() so the PID is reaped and no zombie is left behind.
    """
    proc = _relay_procs.pop(key, None)
    if proc is None or proc.returncode is not None:
        return
    log.info("stopping relay: key=%s", key)

    # Close stderr pipe so the drain task (and the child) are not blocked
    if proc.stderr is not None:
        try:
            proc.stderr.feed_eof()
        except Exception:
            pass

    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("relay did not exit cleanly; killing: key=%s", key)
        proc.kill()
        # Always reap — without this await the PID becomes a zombie
        try:
            await proc.wait()
        except Exception:
            pass


# ── Internal ──────────────────────────────────────────────────────────────────

async def _drain_stderr(key: str, proc: asyncio.subprocess.Process) -> None:
    if not proc.stderr:
        return
    async for raw_line in proc.stderr:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if line:
            log.debug("relay[%s] ffmpeg: %s", key, line)
    rc = await proc.wait()
    log.info("relay[%s] ffmpeg exited: rc=%s", key, rc)
    # Remove from tracking once confirmed dead
    _relay_procs.pop(key, None)
