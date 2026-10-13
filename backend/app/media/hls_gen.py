"""
media/hls_gen.py — HLS generation service.

Converts incompatible video into HLS (H.264/AAC) for:
  • Large files (>500 MB) — stream without downloading first
  • Long videos (>30 min) — progressive loading
  • Unsupported codecs (HEVC, AV1 on older browsers, etc.)

Strategy:
  • ffmpeg writes the playlist with -hls_flags append_list so hls.js can
    start playing from the first few segments rather than waiting for the
    playlist to be finalised at the end of a (potentially very long) job.
  • generate_hls() still awaits the full ffmpeg job to completion/failure,
    exactly like remux_to_mp4() — this keeps job lifecycle (dedup, timeout,
    cancellation, cleanup) identical and testable the same way.
  • hls_segment_endpoint (media.py) serves whatever segments exist on disk
    at request time — a client polling the playlist mid-transcode already
    gets a playable, growing file because of append_list, without needing
    generate_hls() itself to return early.
  • Semaphore-limited concurrent jobs.
  • Output under settings.temp_dir / hls_{key}/.
  • Each job is deduplicated by URL hash.

Security:
  • ffmpeg args always passed as a list — never shell-interpolated

Process lifecycle contract (matches remux.py exactly — see tests/test_reliability.py
TestHLSLifecycle, which patches asyncio.wait_for / proc.communicate directly):
  • proc.communicate() is wrapped in asyncio.wait_for(..., timeout=ffmpeg_job_timeout)
  • asyncio.TimeoutError → kill process, remove out_dir, return None
  • asyncio.CancelledError → kill process, remove out_dir, re-raise
  • non-zero exit code → remove out_dir, return None
  • success → return playlist Path
  • finally block always reaps the process (_kill_and_wait) and removes
    out_dir on any non-success path
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.media.cleanup import enforce_budget_async

log = get_logger(__name__)

_in_progress: dict[str, asyncio.Event] = {}
_results: dict[str, Path | None] = {}

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.ffmpeg_max_jobs)
    return _semaphore


def job_key(url: str) -> str:
    return hashlib.sha256(f"hls:{url}".encode()).hexdigest()[:24]


def hls_dir(key: str) -> Path:
    return settings.temp_dir / f"hls_{key}"


def playlist_path(key: str) -> Path:
    return hls_dir(key) / "playlist.m3u8"


async def generate_hls(
    url: str,
    *,
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    crf: int | None = None,
    preset: str | None = None,
) -> Path | None:
    """
    Run HLS transcoding for `url` to completion.

    Returns the playlist Path on success, None on failure.
    Concurrent calls for the same URL share the same job.

    NOTE: this awaits the full transcode, same as remux_to_mp4(). Callers
    that want progressive playback while this is still running should poll
    hls_segment_endpoint — ffmpeg's append_list flag means the playlist and
    segments on disk are valid and playable before the job finishes, even
    though this coroutine itself only returns at the end.
    """
    key = job_key(url)
    playlist = playlist_path(key)

    if playlist.exists():
        log.debug("hls cache hit: %s", key)
        return playlist

    if key in _in_progress:
        log.debug("hls waiting for in-progress job: %s", key)
        await _in_progress[key].wait()
        return _results.get(key)

    event = asyncio.Event()
    _in_progress[key] = event

    try:
        await enforce_budget_async()   # prune oldest temp files if over disk budget
        async with _get_semaphore():
            result = await _run_hls(url, key, video_codec, audio_codec, crf, preset)
        _results[key] = result
        return result
    finally:
        event.set()
        _in_progress.pop(key, None)


async def _kill_and_wait(
    proc: asyncio.subprocess.Process, label: str
) -> None:
    """
    Gracefully terminate a subprocess and await its exit.

    Sends SIGTERM first (gives ffmpeg a chance to flush/close files),
    waits up to 3 s, then sends SIGKILL if still alive.  Always awaits
    proc.wait() so the PID is reaped and no zombie is left behind.

    Closes stderr pipe before waiting so that any blocked pipe-write in
    the child does not prevent it from exiting.
    """
    if proc.returncode is not None:
        return  # already exited

    # Close the stderr pipe so the child is not blocked on a full pipe buffer
    if proc.stderr is not None:
        try:
            proc.stderr.feed_eof()
        except Exception:
            pass

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


async def _run_hls(
    url: str,
    key: str,
    video_codec: str,
    audio_codec: str,
    crf: int | None,
    preset: str | None,
) -> Path | None:
    out_dir = hls_dir(key)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = out_dir / "playlist.m3u8"
    segment_pattern = str(out_dir / "seg%05d.ts")

    actual_crf    = crf    or settings.transcode_crf
    actual_preset = preset or settings.transcode_preset

    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-i", url,                # ← safe argv element
        "-c:v", video_codec,
        "-c:a", audio_codec,
    ]

    if video_codec == "libx264":
        cmd += [
            "-crf", str(actual_crf),
            "-preset", actual_preset,
            "-profile:v", "main",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",  # maximum browser compat
        ]

    if audio_codec == "aac":
        cmd += ["-b:a", "192k", "-ar", "48000"]

    cmd += [
        "-f", "hls",
        "-hls_time", str(settings.hls_segment_duration),
        # B6 fix (progressive playback): append_list makes ffmpeg write the
        # playlist incrementally as each segment completes, instead of only
        # at the end of the job. A client polling the playlist mid-transcode
        # sees a valid, growing file and can start playback immediately —
        # this does not change generate_hls()'s own return timing, only what
        # is observable on disk while it runs.
        # hls_list_size 0 + append_list keeps every segment (not a live
        # sliding window) — correct for VOD transcode, unlike a live stream.
        "-hls_list_size", "0",
        "-hls_segment_filename", segment_pattern,
        "-hls_flags", "append_list+independent_segments",
        str(playlist),
    ]

    from urllib.parse import urlparse as _urlparse
    _p = _urlparse(url)
    _safe = f"{_p.scheme}://{_p.netloc}{_p.path}"[:80]
    log.info("hls transcode start: %s → %s", _safe, out_dir.name)
    t0 = time.monotonic()

    proc: asyncio.subprocess.Process | None = None
    succeeded = False

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.ffmpeg_job_timeout,
            )
        except asyncio.TimeoutError:
            log.error("hls transcode timed out for %s", _safe)
            return None   # finally block kills proc and removes out_dir

    except FileNotFoundError:
        log.error("ffmpeg not found at: %s", settings.ffmpeg_path)
        return None
    except asyncio.CancelledError:
        log.warning("hls transcode cancelled for %s", _safe)
        raise   # re-raise after finally cleans up
    except Exception as exc:
        log.error("hls transcode unexpected error: %s", type(exc).__name__)
        return None
    else:
        # Only reached when communicate() completed without exception
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[-500:]
            log.error("hls failed (exit %d) in %.1fs: %s",
                      proc.returncode, elapsed, err)
        elif playlist.exists():
            succeeded = True
            log.info("hls done in %.1fs, segments in %s", elapsed, out_dir.name)
        else:
            log.error("hls completed but playlist missing for %s", _safe)
        return playlist if succeeded else None
    finally:
        # Guarantee: child process is always reaped; partial output removed on failure.
        if proc is not None:
            await _kill_and_wait(proc, f"hls[{key[:8]}]")
        if not succeeded:
            # Remove the entire output directory — no partial segments left behind.
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
