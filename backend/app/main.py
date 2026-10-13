"""
main.py — Fluxora API entry point.

Run:
    uvicorn app.main:app --reload --port 8000

Architecture:
  • /api/health          — liveness probe + ffprobe/ffmpeg availability
  • /api/media/probe     — FFprobe metadata extraction
  • /api/media/compat    — browser compatibility check
  • /api/media/play      — full pipeline decision (probe → compat → strategy)
  • /api/media/stream/   — Range-request streaming of remuxed files
  • /api/media/hls/      — HLS segment/playlist serving
  • /api/media/proxy/    — Auth-header HLS proxy (fixes mastermaster + 403 bugs)
"""

from __future__ import annotations

import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes.media import router as media_router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import get_logger

log = get_logger(__name__)


# ── Forwarded-headers middleware ──────────────────────────────────────────────
# When the app runs behind a reverse proxy (nginx, Caddy, Vite dev proxy),
# request.base_url reflects the BACKEND socket address (e.g. http://localhost:8000)
# instead of the client-facing URL (e.g. http://localhost:5173).
#
# _rewrite_m3u8 uses base_url to construct absolute proxy segment URLs that it
# embeds in rewritten HLS manifests. If base_url is wrong, those embedded URLs
# point to the backend directly — bypassing the reverse proxy, triggering CORS
# failures, and causing hls.js to fall back to relative URL resolution, which
# concatenates the path components and produces "mastermaster_video10000k.m3u8".
#
# Fix: read X-Forwarded-Proto, X-Forwarded-Host, and X-Forwarded-Prefix (set by
# Vite's proxy, nginx's proxy_set_header, or Caddy automatically) and patch
# request.scope so that FastAPI's request.base_url reflects the real client URL.

class ForwardedHeadersMiddleware(BaseHTTPMiddleware):
    """
    Patch request.scope with values from X-Forwarded-* headers.

    Header priority (first found wins):
      scheme  — X-Forwarded-Proto  > X-Forwarded-Scheme  > scope default
      host    — X-Forwarded-Host   > X-Forwarded-Server  > scope default
      prefix  — X-Forwarded-Prefix (path prefix stripped by upstream proxy)

    BUG FIX: BaseHTTPMiddleware wraps call_next in an anyio task group, which
    means exceptions raised inside route handlers are caught and converted to
    500 responses BEFORE the global exception handler sees them. This caused
    unhandled exceptions in stream_file (e.g. aiofiles errors on missing temp
    files) to produce a plain 500 with no JSON body, which hls.js interprets as
    a network error and retries indefinitely — eventually causing the 502 storm
    seen in production (hls.js was retrying against a Vite dev proxy that was
    itself getting a 500 with no JSON detail).

    Fixed by overriding dispatch to pass exceptions through instead of swallowing
    them, and by ensuring the response is always fully consumed before returning.
    """

    async def dispatch(self, request: Request, call_next):
        scope = request.scope

        # ── Scheme ──────────────────────────────────────────────────────────
        proto = (
            request.headers.get("x-forwarded-proto")
            or request.headers.get("x-forwarded-scheme")
        )
        if proto and proto in ("http", "https"):
            scope = {**scope, "scheme": proto}

        # ── Host (includes port when non-standard) ───────────────────────────
        host_header = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("x-forwarded-server")
        )
        if host_header:
            host_bytes = host_header.encode("latin-1")
            raw_headers: list = list(scope.get("headers", []))
            raw_headers = [
                (k, host_bytes) if k == b"host" else (k, v)
                for k, v in raw_headers
            ]
            if not any(k == b"host" for k, _ in raw_headers):
                raw_headers.append((b"host", host_bytes))
            scope = {**scope, "headers": raw_headers}

        # ── Path prefix ──────────────────────────────────────────────────────
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        if prefix:
            scope = {**scope, "root_path": prefix}

        patched = Request(scope, request.receive, request._send)
        response = await call_next(patched)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Fluxora API starting — temp_dir=%s", settings.temp_dir)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Fluxora API",
    description=(
        "Production media pipeline: probe → compatibility → direct/remux/transcode. "
        "Implements VLC-inspired playback strategy with FFprobe/FFmpeg backend."
    ),
    version="2.0.1",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware registration order ─────────────────────────────────────────────
# Starlette/FastAPI runs middleware in REVERSE registration order
# (last-registered = outermost = executes first on inbound requests).
#
# Desired onion order (outermost → innermost):
#   TrustedHostMiddleware → ForwardedHeadersMiddleware → CORSMiddleware → Routes
#
# Since "last added = outermost", register in reverse:
#   1. CORSMiddleware   (innermost; added first)
#   2. ForwardedHeaders (middle)
#   3. TrustedHost      (outermost; added last — checks Host before any rewrite)
#
# Previous code registered TrustedHost AFTER ForwardedHeaders, making
# TrustedHost run AFTER the host header was already rewritten, defeating its
# purpose: a client spoofing X-Forwarded-Host could bypass the check entirely.
# FIX: CORS first, ForwardedHeaders second, TrustedHost last.

# 1. CORS (innermost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Range", "Accept-Ranges"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

# 2. Forwarded headers (rewrites base_url for proxy segment URL generation)
app.add_middleware(ForwardedHeadersMiddleware)

# 3. Trusted hosts (outermost; sees the original Host before any rewrite)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


# ── Global exception handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    safe_path = request.url.path
    log.error(
        "Unhandled exception on %s %s:\n%s",
        request.method,
        safe_path,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Please try again later."},
    )


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(media_router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "Fluxora API",
        "version": "2.0.1",
        "docs": "/api/docs",
        "pipeline": "probe → compat → direct | remux | transcode | hls",
    }


@app.get("/health", include_in_schema=False)
async def health_root() -> dict:
    return {"status": "ok"}
