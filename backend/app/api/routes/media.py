"""
api/routes/media.py — Core media pipeline endpoints.

GET  /api/health
POST /api/media/probe           — FFprobe a URL, return codec/container info
POST /api/media/compatibility   — Check browser playback compatibility
POST /api/media/play            — Full pipeline: probe → compat → decide → return play URL
GET  /api/media/stream/{job_id} — Serve remuxed/transcoded file with Range support
GET  /api/media/hls/{key}/{filename} — Serve HLS playlist and segments

--- New in v2.1 ---
POST /api/media/proxy/session           — Register auth headers → get proxy token
GET  /api/media/proxy/{token}           — Serve root HLS manifest via auth proxy
GET  /api/media/proxy/{token}/segment   — Proxy a single segment or nested manifest
POST /api/media/rtmp/relay              — Start RTMP/RTSP → HLS relay
GET  /api/settings                      — Read runtime settings
POST /api/settings                      — Update runtime settings (allow_private_ips, etc.)

Bug fixes in this revision
---------------------------
B13 fix (proxy endpoints): pass backend_origin instead of request.base_url to
    proxy module functions. backend_origin is derived from the raw ASGI scope
    (request.scope["scheme"] + request.scope["server"]) BEFORE
    ForwardedHeadersMiddleware rewrites base_url to the frontend-facing origin.
    This ensures all rewritten HLS manifest segment URLs point at the FastAPI
    backend itself, not the Vite dev server, eliminating the 502 storm caused
    by hls.js fetching segments from an origin that then triggered the SSRF guard.

B16 fix (HLS segment endpoint): wrap stream_file in try/except so that I/O
    errors (e.g. aiofiles failing on a partially-written segment) return a
    clean 503 JSON response instead of propagating through BaseHTTPMiddleware
    as an unhandled exception — which was causing FastAPI to return an empty 500
    body that hls.js treated as a network error and retried indefinitely,
    producing the 502 storm visible in the network tab.

B17 fix (health endpoint cache): the /health endpoint is called by hls.js as
    a liveness check and by the frontend on every player mount. With the default
    ffprobe_path="ffprobe" and shutil.which returning None on cold start before
    PATH is fully resolved, health was returning ffprobe_available=False even
    when ffprobe was installed — causing the frontend to show a persistent
    "backend unavailable" warning. Fixed by checking both the configured path
    and the PATH-resolved binary name.
"""

import asyncio
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import get_logger
from app.core.url_validator import (
    validate_url, validate_rtmp_url, URLValidationError,
    set_allow_private_ips, get_allow_private_ips,
)
from app.media.probe import probe, MediaInfo
from app.media.compatibility import check_compatibility, PlaybackStrategy
from app.media.remux import remux_to_mp4, job_key as remux_key
from app.media.hls_gen import generate_hls, job_key as hls_key, hls_dir
from app.media.streaming import stream_file
from app.media import proxy as proxy_module
from app.media.proxy import _segment_mime
from app.media import rtmp_relay
from app.schemas.media import (
    ProbeRequest, ProbeResponse, StreamInfoOut,
    CompatibilityRequest, CompatibilityResponse,
    PlayRequest, PlayResponse,
    SourceRequest, SourceResponse,
    MetadataRequest, MetadataResponse,
    HealthResponse, SourceType, PlaybackStrategy as SchemaPBS,
    ProxySessionRequest, ProxySessionResponse,
    RtmpRelayRequest, RtmpRelayResponse,
    SettingsState, SettingsUpdateRequest,
)
from app.services.source import validate_source, get_metadata

log = get_logger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _backend_origin(request: Request) -> str:
    """
    Return the FastAPI backend's own origin (scheme://host:port).

    B13 fix: read directly from request.scope["scheme"] and
    request.scope["server"] BEFORE ForwardedHeadersMiddleware rewrites
    base_url to the frontend-facing origin. This guarantees that all
    proxy segment URLs embedded in rewritten HLS manifests always point at
    the backend itself, not the Vite dev server or production reverse proxy.

    Examples:
      scope["scheme"] = "http", scope["server"] = ("127.0.0.1", 8000)
      → "http://127.0.0.1:8000"

    The Vite proxy (and production nginx/Caddy) will forward /api/... paths
    from the frontend origin to this backend, so segment URLs constructed with
    this origin are always reachable by the browser via the reverse proxy.
    """
    scheme = request.scope.get("scheme", "http")
    server = request.scope.get("server")  # ("host", port) tuple or None
    if server:
        host, port = server
        # Omit default ports for cleaner URLs
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            return f"{scheme}://{host}"
        return f"{scheme}://{host}:{port}"
    # Fallback: parse from the Host header in scope (unmodified by middleware)
    for name, value in request.scope.get("headers", []):
        if name == b"host":
            return f"{scheme}://{value.decode('latin-1')}"
    return f"{scheme}://localhost:8000"


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    # B17 fix: check both the configured path and the bare binary name so that
    # "ffprobe" resolves correctly even when PATH is populated after module import.
    ffprobe_ok = bool(
        shutil.which(settings.ffprobe_path)
        or (settings.ffprobe_path != "ffprobe" and shutil.which("ffprobe"))
    )
    ffmpeg_ok = bool(
        shutil.which(settings.ffmpeg_path)
        or (settings.ffmpeg_path != "ffmpeg" and shutil.which("ffmpeg"))
    )
    return HealthResponse(
        status="ok",
        ffprobe_available=ffprobe_ok,
        ffmpeg_available=ffmpeg_ok,
    )


# ── Probe ─────────────────────────────────────────────────────────────────────

@router.post("/media/probe", response_model=ProbeResponse, tags=["media"])
@limiter.limit(settings.rate_limit_probe)
async def probe_endpoint(request: Request, req: ProbeRequest) -> ProbeResponse:
    """Run FFprobe against a URL and return full codec/container metadata."""
    try:
        validate_url(req.url)
    except URLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    info = await probe(req.url, force=req.force)
    return _probe_response(info)


# ── Compatibility ─────────────────────────────────────────────────────────────

@router.post("/media/compatibility", response_model=CompatibilityResponse, tags=["media"])
@limiter.limit(settings.rate_limit_probe)
async def compatibility_endpoint(request: Request, req: CompatibilityRequest) -> CompatibilityResponse:
    """Check browser compatibility and return recommended playback strategy."""
    try:
        validate_url(req.url)
    except URLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    info = await probe(req.url)
    result = check_compatibility(info)

    return CompatibilityResponse(
        url=req.url,
        strategy=SchemaPBS(result.strategy.value),
        reason=result.reason,
        target_container=result.target_container,
        target_video_codec=result.target_video_codec,
        target_audio_codec=result.target_audio_codec,
        video_codec=result.video_codec,
        audio_codec=result.audio_codec,
        container=result.container,
        needs_processing=result.needs_processing,
    )


# ── Play decision ─────────────────────────────────────────────────────────────

@router.post("/media/play", response_model=PlayResponse, tags=["media"])
@limiter.limit(settings.rate_limit_play)
async def play_endpoint(request: Request, req: PlayRequest) -> PlayResponse:
    """
    Full pipeline: probe the URL → determine compatibility → return what to play.
    """
    try:
        validate_url(req.url)
    except URLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    info = await probe(req.url)
    result = check_compatibility(info)
    # Use frontend-facing base_url for play_url (browser will fetch this URL)
    base_url = str(request.base_url).rstrip("/")

    if result.strategy == PlaybackStrategy.DIRECT:
        return PlayResponse(
            original_url=req.url,
            play_url=req.url,
            source_type=_guess_source_type(req.url, req.hint_type),
            strategy=SchemaPBS.direct,
            reason=result.reason,
            probe=_probe_response(info),
        )

    if result.strategy in (PlaybackStrategy.REMUX,):
        key = remux_key(req.url, result.target_audio_codec)
        _task = asyncio.create_task(
            remux_to_mp4(
                req.url,
                video_codec=result.target_video_codec,
                audio_codec=result.target_audio_codec,
            )
        )
        _task.add_done_callback(
            lambda t: log.error("remux task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        play_url = f"{base_url}/api/media/stream/{key}"
        return PlayResponse(
            original_url=req.url,
            play_url=play_url,
            source_type=SourceType.remuxed,
            strategy=SchemaPBS.remux,
            reason=result.reason,
            probe=_probe_response(info),
            job_id=key,
        )

    if result.strategy in (PlaybackStrategy.TRANSCODE, PlaybackStrategy.HLS_TRANSCODE):
        key = hls_key(req.url)
        _task = asyncio.create_task(
            generate_hls(
                req.url,
                video_codec=result.target_video_codec,
                audio_codec=result.target_audio_codec,
            )
        )
        _task.add_done_callback(
            lambda t: log.error("hls task failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        play_url = f"{base_url}/api/media/hls/{key}/playlist.m3u8"
        return PlayResponse(
            original_url=req.url,
            play_url=play_url,
            source_type=SourceType.hls_generated,
            strategy=SchemaPBS(result.strategy.value),
            reason=result.reason,
            probe=_probe_response(info),
            job_id=key,
        )

    raise HTTPException(status_code=422, detail=f"Unsupported strategy: {result.strategy}")


# ── Stream remuxed file ───────────────────────────────────────────────────────

@router.get("/media/stream/{job_id}", tags=["media"])
async def stream_endpoint(job_id: str, request: Request):
    """Serve a remuxed file with Range Request support."""
    if not job_id.replace("-", "").isalnum() or len(job_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid job_id")

    path = settings.temp_dir / f"remux_{job_id}.mp4"

    if not path.exists():
        # B12 fix: return 404 while job is not ready (not 202).
        raise HTTPException(
            status_code=404,
            detail="Processing not yet complete. Retry in a moment.",
            headers={"Retry-After": "3"},
        )

    # B16 fix: catch I/O errors so they return a proper JSON 503 instead of
    # propagating as an unhandled exception through BaseHTTPMiddleware.
    try:
        return await stream_file(path, request)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("stream_endpoint I/O error for job_id=%s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="Media file temporarily unavailable. Retry in a moment.")


# ── HLS segments + playlist ───────────────────────────────────────────────────

@router.get("/media/hls/{key}/{filename}", tags=["media"])
async def hls_segment_endpoint(key: str, filename: str, request: Request):
    """Serve HLS playlist (.m3u8) and segments (.ts)."""
    if not key.replace("-", "").isalnum() or len(key) > 64:
        raise HTTPException(status_code=400, detail="Invalid key")

    safe_filename = Path(filename).name
    path = hls_dir(key) / safe_filename

    if not path.exists():
        # B16 fix: distinguish "job still running, first segment not written yet"
        # (transient 404 — hls.js should retry) from "key is completely unknown"
        # (permanent 404). Both return 404 but with different Retry-After headers
        # so hls.js back-off behaviour is appropriate.
        dir_exists = hls_dir(key).exists()
        if dir_exists:
            # Job is running but this segment isn't written yet — transient
            raise HTTPException(
                status_code=404,
                detail="Segment not yet available. Retry shortly.",
                headers={"Retry-After": "1"},
            )
        raise HTTPException(status_code=404, detail="Segment not found")

    if path.suffix == ".m3u8":
        return FileResponse(str(path), media_type="application/vnd.apple.mpegurl")

    # B16 fix: wrap stream_file to return clean 503 on I/O error.
    try:
        return await stream_file(path, request)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("hls_segment_endpoint I/O error key=%s file=%s: %s", key, filename, exc)
        raise HTTPException(status_code=503, detail="Segment temporarily unavailable.")


# ── Auth-header proxy ─────────────────────────────────────────────────────────

@router.post("/media/proxy/session", response_model=ProxySessionResponse, tags=["proxy"])
@limiter.limit(settings.rate_limit_proxy_session)
async def create_proxy_session(request: Request, req: ProxySessionRequest) -> ProxySessionResponse:
    """
    Register auth headers for a URL and return a proxy token.

    The returned proxy_url is an HLS source the player can load directly.
    All HTTP requests to the upstream CDN will carry the registered headers.
    Useful for: Bearer-token HLS, signed Akamai URLs, private CDN streams.
    """
    try:
        validate_url(req.url)
    except URLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    token = proxy_module.create_session(req.url, req.headers)
    # proxy_url must use the frontend-facing base_url so the browser can reach it
    base_url = str(request.base_url).rstrip("/")
    proxy_url = f"{base_url}/api/media/proxy/{token}"
    return ProxySessionResponse(token=token, proxy_url=proxy_url)


@router.post("/media/proxy/session/{token}/refresh", tags=["proxy"])
@limiter.limit(settings.rate_limit_proxy_session)
async def refresh_proxy_session(
    request: Request, token: str, req: ProxySessionRequest
) -> ProxySessionResponse:
    """
    Refresh the URL and auth headers for an existing proxy session in-place.
    Returns the same token and proxy_url on success (HTTP 200).
    Returns HTTP 410 Gone if the session has expired.
    """
    try:
        validate_url(req.url)
    except URLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    updated = proxy_module.refresh_session(token, req.url, req.headers)
    if not updated:
        raise HTTPException(
            status_code=410,
            detail="Proxy session expired or not found. Create a new session.",
        )

    base_url = str(request.base_url).rstrip("/")
    proxy_url = f"{base_url}/api/media/proxy/{token}"
    return ProxySessionResponse(token=token, proxy_url=proxy_url)


@router.get("/media/proxy/{token}", tags=["proxy"])
async def proxy_root_endpoint(token: str, request: Request):
    """Serve the root HLS manifest with auth headers; rewrite segment URLs through proxy."""
    session = proxy_module.get_session(token)
    if not session:
        raise HTTPException(status_code=410, detail="Proxy session expired or not found.")

    # B13 fix: use backend_origin (the FastAPI server's own address) for
    # rewriting manifest URLs, NOT request.base_url which has been rewritten
    # by ForwardedHeadersMiddleware to the frontend-facing origin.
    backend_orig = _backend_origin(request)
    try:
        body, ct = await proxy_module.fetch_root(session, backend_orig, token)
        return Response(content=body, media_type=ct)
    except URLValidationError:
        raise HTTPException(status_code=410, detail="Proxy session URL is no longer valid.")
    except httpx.HTTPStatusError as exc:
        upstream = exc.response.status_code
        if upstream in (401, 403, 404, 410, 429):
            raise HTTPException(status_code=upstream,
                                detail=f"Upstream returned {upstream}")
        raise HTTPException(status_code=502, detail=f"Upstream returned {upstream}")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Upstream connection failed.")
    except Exception:
        log.exception("proxy_root_endpoint unexpected error for token=%s", token)
        raise HTTPException(status_code=502, detail="Proxy error.")


@router.get("/media/proxy/{token}/segment", tags=["proxy"])
async def proxy_segment_endpoint(
    token: str,
    url: str = Query(..., description="Absolute upstream URL to fetch"),
    request: Request = None,
):
    """
    Proxy a single HLS segment or nested manifest with auth headers.

    hls.js calls this for every .ts segment and every variant playlist.
    The URL is the encoded absolute upstream URL written by _rewrite_m3u8.
    """
    session = proxy_module.get_session(token)
    if not session:
        raise HTTPException(status_code=410, detail="Proxy session expired.")

    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=422, detail="Only http/https segment URLs are allowed.")

    # SSRF validation on the upstream URL (not on our own proxy-rewritten URL)
    try:
        validate_url(url)
    except URLValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # B13 fix: use backend_origin for any nested manifest rewrites
    backend_orig = _backend_origin(request)

    is_manifest = url.split("?")[0].lower().endswith((".m3u8", ".m3u"))
    if is_manifest:
        try:
            body, ct = await proxy_module.fetch_segment(url, session, backend_orig, token)
            return Response(content=body, media_type=ct)
        except URLValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except httpx.HTTPStatusError as exc:
            upstream = exc.response.status_code
            if upstream in (401, 403, 404, 410, 429):
                raise HTTPException(status_code=upstream,
                                    detail=f"Upstream returned {upstream}")
            raise HTTPException(status_code=502, detail=f"Upstream returned {upstream}")
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Upstream connection failed.")
        except Exception:
            log.exception("proxy_segment manifest error token=%s", token)
            raise HTTPException(status_code=502, detail="Proxy error.")

    # Binary segment — stream without full buffering
    # B15 fix: call validate_url before creating StreamingResponse so errors
    # surface as proper HTTPExceptions, not as broken empty streams.
    try:
        gen = proxy_module.stream_segment(url, session["headers"])
        return StreamingResponse(gen, media_type=_segment_mime(url))
    except URLValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        log.exception("proxy_segment stream error token=%s", token)
        raise HTTPException(status_code=502, detail="Segment proxy error.")


# ── RTMP / RTSP relay ─────────────────────────────────────────────────────────

@router.post("/media/rtmp/relay", response_model=RtmpRelayResponse, tags=["relay"])
@limiter.limit(settings.rate_limit_rtmp)
async def rtmp_relay_endpoint(request: Request, req: RtmpRelayRequest) -> RtmpRelayResponse:
    """
    Start an RTMP or RTSP → HLS live relay.
    Supports: rtmp://, rtmps://, rtsp://, rtsps://
    """
    try:
        validate_rtmp_url(req.rtmp_url)
    except URLValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    key = await rtmp_relay.start_relay(req.rtmp_url)
    base_url = str(request.base_url).rstrip("/")
    play_url = f"{base_url}/api/media/hls/{key}/playlist.m3u8"

    return RtmpRelayResponse(
        key=key,
        play_url=play_url,
        message="Relay started. Poll play_url until HTTP 200, then load as HLS.",
    )


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsState, tags=["settings"])
async def get_settings() -> SettingsState:
    """Return current runtime settings."""
    return SettingsState(allow_private_ips=get_allow_private_ips())


@router.post("/settings", response_model=SettingsState, tags=["settings"])
async def update_settings(req: SettingsUpdateRequest) -> SettingsState:
    """
    Update runtime settings.
    allow_private_ips: when true, allows fetching from 192.168.x.x, 10.x.x.x,
    127.0.0.1, etc. to reach Plex, Jellyfin, or local media servers.
    Cloud metadata IPs (169.254.169.254) remain blocked regardless.
    """
    if req.allow_private_ips is not None:
        set_allow_private_ips(req.allow_private_ips)
        log.info("settings updated: allow_private_ips=%s", req.allow_private_ips)

    return SettingsState(allow_private_ips=get_allow_private_ips())


# ── Backward-compatible endpoints (v1 routes) ─────────────────────────────────

@router.post("/player/validate-source", response_model=SourceResponse, tags=["legacy"])
async def validate_source_endpoint(request: SourceRequest) -> SourceResponse:
    return await validate_source(request)


@router.post("/player/metadata", response_model=MetadataResponse, tags=["legacy"])
async def metadata_endpoint(request: MetadataRequest) -> MetadataResponse:
    return await get_metadata(request)


# ── Private helpers ───────────────────────────────────────────────────────────

def _probe_response(info: MediaInfo) -> ProbeResponse:
    return ProbeResponse(
        url=info.url,
        container=info.container,
        duration=info.duration,
        bit_rate=info.bit_rate,
        size=info.size,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        width=info.width,
        height=info.height,
        fps=info.fps,
        video_streams=[_stream_out(s) for s in info.video_streams],
        audio_streams=[_stream_out(s) for s in info.audio_streams],
        subtitle_streams=[_stream_out(s) for s in info.subtitle_streams],
        probe_error=info.probe_error,
    )


def _stream_out(s) -> StreamInfoOut:
    return StreamInfoOut(
        index=s.index,
        codec_type=s.codec_type,
        codec_name=s.codec_name,
        codec_long_name=s.codec_long_name,
        width=s.width,
        height=s.height,
        fps=s.fps,
        bit_rate=s.bit_rate,
        pix_fmt=s.pix_fmt,
        sample_rate=s.sample_rate,
        channels=s.channels,
        channel_layout=s.channel_layout,
        language=s.language,
        title=s.title,
        duration=s.duration,
        profile=s.profile,
    )


def _guess_source_type(url: str, hint: SourceType | None) -> SourceType:
    if hint:
        return hint
    lower = url.lower().split("?")[0]
    if ".m3u8" in lower or "/hls/" in lower:
        return SourceType.hls
    if ".mpd" in lower:
        return SourceType.dash
    return SourceType.mp4
