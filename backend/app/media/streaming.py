"""
media/streaming.py — HTTP Range Request streaming for local/remuxed files.

Implements:
  • Proper 206 Partial Content responses
  • Accept-Ranges: bytes header
  • Content-Range header construction
  • Chunked async file reading (never loads full file into RAM)
  • Correct MIME type mapping

Used by:
  • /api/media/stream/{job_id} — stream a remuxed/transcoded file
  • /api/media/hls/{key}/{filename} — serve HLS segments and playlists
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import AsyncIterator

import aiofiles
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, Response

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_MIME_MAP = {
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".ogg":  "video/ogg",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts":   "video/mp2t",
    ".mkv":  "video/x-matroska",
}


def _mime_for(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


async def stream_file(path: Path, request: Request) -> Response:
    """
    Stream a file with Range Request support.

    Returns 206 Partial Content for range requests, 200 for full requests.
    Never reads the entire file into memory.
    """
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = path.stat().st_size
    mime = _mime_for(path)

    range_header = request.headers.get("range")

    if not range_header:
        # Full file — still streamed in chunks
        return StreamingResponse(
            _file_chunks(path, 0, file_size - 1),
            status_code=200,
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
            },
        )

    # Parse "bytes=start-end" (RFC 7233).
    # Three legal forms:
    #   bytes=500-999   → bytes 500 to 999 inclusive
    #   bytes=500-      → bytes 500 to EOF
    #   bytes=-500      → last 500 bytes (suffix-range)
    try:
        range_spec = range_header.replace("bytes=", "").strip()
        start_str, end_str = range_spec.split("-", 1)
        if not start_str and end_str:
            # Suffix range: last N bytes
            suffix_len = int(end_str)
            start = max(0, file_size - suffix_len)
            end   = file_size - 1
        else:
            start = int(start_str) if start_str else 0
            end   = int(end_str)   if end_str   else file_size - 1
    except (ValueError, AttributeError):
        raise HTTPException(status_code=416, detail="Invalid Range header")

    # Clamp to file bounds
    start = max(0, start)
    end   = min(end, file_size - 1)

    if start > end or start >= file_size:
        raise HTTPException(
            status_code=416,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    content_length = end - start + 1
    log.debug("range request: bytes=%d-%d (%d bytes) of %d", start, end, content_length, file_size)

    return StreamingResponse(
        _file_chunks(path, start, end),
        status_code=206,
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Cache-Control": "no-cache",
        },
    )


async def _file_chunks(
    path: Path,
    start: int,
    end: int,
) -> AsyncIterator[bytes]:
    """Async generator that yields file chunks between byte offsets [start, end]."""
    remaining = end - start + 1
    chunk_size = settings.stream_chunk_size

    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            chunk = await f.read(read_size)
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)
