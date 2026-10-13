"""
media/cleanup.py — Temp directory budget enforcement.

Prunes the oldest remux and HLS output files when total temp usage exceeds
settings.max_temp_size_mb.  Called opportunistically before starting a new
FFmpeg job.

Design and honest limitations
------------------------------
• Operates only within settings.temp_dir — never touches other paths.
• Sorts by mtime (oldest first) so recently-generated content survives.
• Skips files younger than _MIN_AGE_SECONDS (may still be actively written).
• Never raises — failures are logged and ignored so they don't block playback.
• Filesystem I/O (glob + stat + unlink) is run in a thread-pool executor so
  it does not block the asyncio event loop.

NOT a hard real-time cap
------------------------
enforce_budget() is a best-effort pre-job check: it measures current disk
usage, prunes, then returns.  While the new job runs, other concurrent jobs
(up to ffmpeg_max_jobs) may also start and pass their own pre-job checks
before any of them write significant data.  In the worst case actual usage
can exceed the budget by approximately ffmpeg_max_jobs × single_output_size.
This is acceptable for a single-worker deployment with a generous budget
(default: 2 GB).  A stricter reservation system would require tracking
in-progress job sizes, which adds complexity not warranted at this scale.

Serving vs. deletion
---------------------
On Linux, unlink() removes the directory entry but the inode (and data) remain
accessible to any open file descriptor.  Clients actively streaming a segment
that is unlinked by cleanup will continue receiving data correctly.  New
clients requesting the same segment after the unlink will receive a 404.
This is the intended behaviour: old segments are evicted; new ones are served.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_MIN_AGE_SECONDS = 60  # don't evict files younger than this


# ── Synchronous helpers (run in executor) ─────────────────────────────────────

def _collect_files(temp_dir: Path) -> list[tuple[float, int, Path]]:
    """
    Return (mtime, size, path) for every prunable file under temp_dir,
    sorted oldest-first.  Safe to call when temp_dir does not exist.
    """
    if not temp_dir.exists():
        return []

    entries: list[tuple[float, int, Path]] = []
    patterns = ["remux_*.mp4", "hls_*/seg*.ts", "hls_*/playlist.m3u8"]
    for pat in patterns:
        try:
            for p in temp_dir.glob(pat):
                try:
                    st = p.stat()
                    entries.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
        except OSError:
            pass

    entries.sort(key=lambda e: e[0])   # oldest first
    return entries


def _measure_total(temp_dir: Path) -> int:
    """Total bytes used by remux and HLS outputs.  0 if dir missing."""
    if not temp_dir.exists():
        return 0

    total = 0
    for pat in ["remux_*.mp4", "hls_*"]:
        try:
            for p in temp_dir.glob(pat):
                try:
                    if p.is_dir():
                        total += sum(
                            f.stat().st_size
                            for f in p.rglob("*")
                            if f.is_file()
                        )
                    else:
                        total += p.stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
    return total


def _prune_sync(temp_dir: Path, budget_bytes: int) -> None:
    """
    Synchronous prune loop.  Called via run_in_executor so it does not
    block the event loop.
    """
    current = _measure_total(temp_dir)
    if current <= budget_bytes:
        return

    log.info(
        "temp dir over budget (%.0f MB / %d MB), pruning ...",
        current / 1_048_576,
        budget_bytes // (1024 * 1024),
    )

    now = time.time()
    for mtime, size, path in _collect_files(temp_dir):
        if current <= budget_bytes:
            break
        if now - mtime < _MIN_AGE_SECONDS:
            continue   # too recent — may still be written or served
        try:
            path.unlink()
            current -= size
            log.debug("pruned %s (%.1f MB)", path.name, size / 1_048_576)
        except OSError as exc:
            log.warning("failed to prune %s: %s", path.name, type(exc).__name__)


# ── Public API ────────────────────────────────────────────────────────────────

def enforce_budget() -> None:
    """
    Synchronous budget check, suitable for direct calls before job start.

    Runs filesystem I/O in the calling thread.  When called from async
    context (all current callers), use enforce_budget_async() instead to
    avoid event-loop blocking on large temp directories.

    Kept for backward compatibility with the test suite and any callers
    that have not yet been converted.
    """
    budget_bytes = settings.max_temp_size_mb * 1024 * 1024
    try:
        _prune_sync(settings.temp_dir, budget_bytes)
    except Exception as exc:
        log.warning("enforce_budget error: %s", type(exc).__name__)


async def enforce_budget_async() -> None:
    """
    Async budget enforcement that runs filesystem I/O off the event loop.

    Preferred for production call sites (remux.py, hls_gen.py).
    Falls back to enforce_budget() if no running loop is found.
    """
    budget_bytes = settings.max_temp_size_mb * 1024 * 1024
    temp_dir = settings.temp_dir
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _prune_sync, temp_dir, budget_bytes)
    except RuntimeError:
        # No running event loop — call synchronously
        enforce_budget()
    except Exception as exc:
        log.warning("enforce_budget_async error: %s", type(exc).__name__)
