"""
media/remux.py — FFmpeg remux service.

Remuxing = change container format WITHOUT re-encoding video.
This is orders of magnitude faster than transcoding (real-time or faster).

Examples:
  MKV (H.264/AAC) → MP4 (H.264/AAC)  [copy both streams]
  MKV (H.264/DTS) → MP4 (H.264/AAC)  [copy video, transcode audio only]

Output files are cached on disk until TTL expires or disk budget is exceeded.

Security:
  • ffmpeg is invoked via subprocess args list (never shell=True)
  • Output paths are under settings.temp_dir only
  • Input URLs are passed as a single argv element
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.media.cleanup import enforce_budget_async

log = get_logger(__name__)


async def _kill_and_wait(proc: asyncio.subprocess.Process, label: str) -> None:
    """
    Terminate a subprocess and await its exit.

    Sends SIGTERM first (gives ffmpeg a chance to flush/close files),
    waits up to 3 s, then sends SIGKILL if still alive.  Always awaits
    proc.wait() so the PID is reaped and no zombie is left behind.

    Safe to call even if the process has already exited.
    """
    if proc.returncode is not None:
        return  # already exited
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("%s: SIGTERM timeout, sending SIGKILL", label)
            proc.kill()
            await proc.wait()
    except Exception as exc:
        log.warning("%s: error during kill: %s", label, type(exc).__name__)
        try:
            await proc.wait()
        except Exception:
            pass


# ── Job deduplication ─────────────────────────────────────────────────────────
# Prevents two simultaneous requests from launching duplicate ffmpeg jobs
# for the same source URL + target configuration.
_in_progress: dict[str, asyncio.Event] = {}
_results: dict[str, Path | None] = {}
_result_time: dict[str, float] = {}

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.ffmpeg_max_jobs)
    return _semaphore


def job_key(url: str, audio_codec: str) -> str:
    return hashlib.sha256(f"remux:{url}:{audio_codec}".encode()).hexdigest()[:24]


def _cached_path(key: str) -> Path | None:
    """Return the cached output path if it still exists and is fresh."""
    result = _results.get(key)
    t = _result_time.get(key, 0)
    if result and result.exists() and (time.monotonic() - t) < settings.remux_cache_ttl:
        return result
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

async def remux_to_mp4(
    url: str,
    *,
    video_codec: str = "copy",
    audio_codec: str = "copy",
) -> Path | None:
    """
    Remux `url` to an MP4 file.

    Returns the output Path on success, None on failure.
    Result is cached; concurrent calls for the same URL share the same job.
    """
    key = job_key(url, audio_codec)

    # Cache hit
    if cached := _cached_path(key):
        log.debug("remux cache hit: %s", key)
        return cached

    # Dedup: if another coroutine is already remuxing this, wait for it
    if key in _in_progress:
        log.debug("remux waiting for in-progress job: %s", key)
        await _in_progress[key].wait()
        return _results.get(key)

    event = asyncio.Event()
    _in_progress[key] = event

    try:
        await enforce_budget_async()   # prune oldest temp files if over disk budget
        async with _get_semaphore():
            result = await _run_remux(url, key, video_codec, audio_codec)
        _results[key] = result
        _result_time[key] = time.monotonic()
        return result
    finally:
        event.set()
        _in_progress.pop(key, None)


async def _run_remux(
    url: str,
    key: str,
    video_codec: str,
    audio_codec: str,
) -> Path | None:
    out_path = settings.temp_dir / f"remux_{key}.mp4"

    if out_path.exists():
        return out_path

    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-i", url,            # ← safe: argv element, not shell string
        "-c:v", video_codec,
        "-c:a", audio_codec,
    ]

    if audio_codec == "aac":
        cmd += ["-b:a", "192k"]

    cmd += [
        "-movflags", "+faststart",  # MP4 moov atom at start for streaming
        "-f", "mp4",
        "-y",                       # overwrite if exists
        str(out_path),
    ]

    from urllib.parse import urlparse as _urlparse
    _p = _urlparse(url)
    _safe = f"{_p.scheme}://{_p.netloc}{_p.path}"[:80]
    log.info("remux start: %s → %s", _safe, out_path.name)
    t0 = time.monotonic()

    proc: asyncio.subprocess.Process | None = None
    stderr_bytes: bytes = b""
    succeeded = False

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.ffmpeg_job_timeout,
            )
        except asyncio.TimeoutError:
            log.error("remux timed out for %s", _safe)
            # proc.kill() + proc.wait() happen in the finally block
            return None
        # CancelledError propagates out; finally block handles cleanup
    except FileNotFoundError:
        log.error("ffmpeg not found at: %s", settings.ffmpeg_path)
        return None
    except asyncio.CancelledError:
        log.warning("remux cancelled for %s", _safe)
        raise   # re-raise after finally cleans up
    except Exception as exc:
        log.error("remux unexpected error: %s", type(exc).__name__)
        return None
    else:
        # Reached only when communicate() returned normally (no exception)
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = stderr_bytes.decode(errors="replace").strip()[-500:]
            log.error("remux failed (exit %d) in %.1fs: %s", proc.returncode, elapsed, err)
        else:
            succeeded = True
            log.info("remux done in %.1fs → %s (%.1f MB)",
                     elapsed, out_path.name, out_path.stat().st_size / 1_048_576)
        return out_path if succeeded else None
    finally:
        # Guarantee: child process is always reaped, partial file is removed on failure.
        if proc is not None:
            await _kill_and_wait(proc, f"remux[{key[:8]}]")
        if not succeeded:
            out_path.unlink(missing_ok=True)
