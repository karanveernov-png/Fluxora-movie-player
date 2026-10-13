# Aura Player v2 — Migration Guide

## What changed and why

This document explains every change made to the existing project,
the reasoning behind each decision, and exactly how to apply them.

---

## Architecture summary

```
v1                              v2
──────────────────────────────  ──────────────────────────────────────────
Frontend only (no backend       Full media pipeline:
pipeline):                        probe → compat → direct/remux/transcode
  - HLS direct only             
  - MP4 direct only             DASH support (lazy-loaded dash.js)
  - No codec detection          FFprobe-powered codec detection
  - No remux/transcode          FFmpeg remux + HLS transcode
  - SSRF-vulnerable backend     SSRF-safe URL validator
  - No resume                   Per-URL resume (localStorage)
  - No autoplay-policy          Autoplay-policy handling + unmute UX
  - No offline handling         Offline/online event handler
  - No info panel data          Real codec/container info from FFprobe
```

---

## Step 1 — Backend: install new dependencies

```bash
cd backend
pip install -r requirements.txt
```

New packages added:
- `pydantic-settings` — for `.env` file support
- `aiofiles` — for async file streaming (Range Requests)

`ffmpeg` and `ffprobe` must be installed on the system:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
apt-get install ffmpeg

# Windows
# Download from https://ffmpeg.org/download.html
# Add to PATH
```

The player works without FFmpeg — it falls back to direct play.
FFmpeg is only needed for remux/transcode of incompatible formats.

---

## Step 2 — Backend: copy new files

```
backend/
  app/
    core/
      config.py          ← REPLACE (adds pydantic-settings, more tunables)
      logging.py         ← NEW
      url_validator.py   ← NEW (SSRF protection)
    media/
      probe.py           ← NEW (FFprobe service + TTL cache)
      compatibility.py   ← NEW (playback strategy engine)
      remux.py           ← NEW (FFmpeg remux service)
      hls_gen.py         ← NEW (FFmpeg HLS transcode service)
      streaming.py       ← NEW (Range Request streaming)
    schemas/
      media.py           ← REPLACE (expanded schemas)
    api/
      routes/
        media.py         ← REPLACE (was player.py — new pipeline endpoints)
    services/
      source.py          ← REPLACE (adds SSRF guard to existing validation)
    main.py              ← REPLACE (registers new routes, lifespan)
  tests/
    test_backend.py      ← NEW
  pytest.ini             ← NEW
  .env.example           ← NEW
```

The old `backend/app/api/routes/player.py` can be deleted —
its endpoints are preserved in `media.py` as legacy `/api/player/*` routes.

---

## Step 3 — Frontend: copy new files

```
src/
  types.ts               ← REPLACE (extended with probe/compat/strategy types)
  App.tsx                ← REPLACE (uses usePlayStrategy, health check, resume)
  App.css                ← REPLACE (processing overlay, strategy pill, info panel)
  api/
    mediaApi.ts          ← NEW (typed fetch client for backend)
  hooks/
    useVideoPlayer.ts    ← REPLACE (DASH, offline, autoplay-policy, CPU fix)
    useResume.ts         ← NEW (localStorage resume per URL)
    usePlayStrategy.ts   ← NEW (resolve URL through backend pipeline)
  components/
    MediaInfoPanel.tsx   ← NEW (rich codec/strategy info panel)
    MoviePlayer/
      MoviePlayer.tsx    ← REPLACE (wires resume, unmute banner, offline banner)
      MoviePlayer.css    ← REPLACE (new banners + accent color updated)
```

Files NOT changed (keep exactly as-is from v1):
- `src/core/HLSManager.ts`      — already excellent, no changes needed
- `src/core/PlaybackManager.ts` — already excellent, no changes needed
- `src/components/MoviePlayer/PlayerControls.tsx`
- `src/components/MoviePlayer/ProgressBar.tsx`
- `src/components/MoviePlayer/LoadingOverlay.tsx`
- `src/components/MoviePlayer/ErrorOverlay.tsx`
- `src/components/MoviePlayer/QualityMenu.tsx`
- `src/components/MoviePlayer/SpeedMenu.tsx`

---

## Step 4 — Environment variable

Create `backend/.env` from the example:

```bash
cp backend/.env.example backend/.env
```

If FFmpeg is not on your PATH, set the absolute paths:

```
AURA_FFMPEG_PATH=/usr/local/bin/ffmpeg
AURA_FFPROBE_PATH=/usr/local/bin/ffprobe
```

---

## Step 5 — Frontend environment variable

Create `src/.env` or add to your Vite config:

```
VITE_API_URL=http://localhost:8000/api
```

---

## Step 6 — Run

```bash
# Terminal 1 — backend
cd backend
uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
npm run dev
```

Visit `http://localhost:5173/api/docs` for the interactive API explorer.

---

## Step 7 — Run tests

```bash
cd backend
pip install pytest pytest-asyncio --break-system-packages
pytest tests/ -v
```

Expected output:
```
tests/test_backend.py::TestURLValidator::test_valid_https PASSED
tests/test_backend.py::TestURLValidator::test_localhost_blocked PASSED
tests/test_backend.py::TestCompatibility::test_mp4_h264_aac_is_direct PASSED
tests/test_backend.py::TestCompatibility::test_mkv_h264_dts_is_remux_audio_transcode PASSED
tests/test_backend.py::TestCompatibility::test_large_file_hevc_is_hls_transcode PASSED
tests/test_backend.py::TestStreaming::test_range_request_206 PASSED
... (all 40+ tests)
```

---

## Playback strategy decision guide

| Format | Strategy | Notes |
|---|---|---|
| MP4 H.264/AAC | DIRECT | Universal browser support |
| MP4 H.264/MP3 | DIRECT | Supported everywhere |
| WebM VP9/Opus | DIRECT | Chrome, Firefox, Safari 14+ |
| WebM AV1/Opus | DIRECT | Chrome 70+, Firefox 67+ |
| MKV H.264/AAC | REMUX | Fast — copy streams into MP4 |
| MKV H.264/DTS | REMUX | Copy video, transcode audio to AAC |
| MKV H.264/AC3 | REMUX | Copy video, transcode audio to AAC |
| AVI H.264/AAC | REMUX | Container swap only |
| MKV HEVC/AAC | TRANSCODE | Full video re-encode to H.264 |
| MKV HEVC/DTS | TRANSCODE | Full video + audio re-encode |
| Large HEVC (>500MB) | HLS_TRANSCODE | Segmented for streaming |
| Long video (>30min) | HLS_TRANSCODE | Progressive loading |
| HLS .m3u8 | DIRECT | Handled by hls.js |
| DASH .mpd | DIRECT | Handled by dash.js (lazy) |

---

## New API endpoints

| Method | Path | Description |
|---|---|---|
| GET | /api/health | FFprobe/FFmpeg availability check |
| POST | /api/media/probe | FFprobe a URL → codec/container data |
| POST | /api/media/compatibility | Browser compat check → strategy |
| POST | /api/media/play | Full pipeline → play URL |
| GET | /api/media/stream/{job_id} | Serve remuxed file (Range Requests) |
| GET | /api/media/hls/{key}/{file} | Serve HLS segments + playlist |
| POST | /api/player/validate-source | Legacy v1 endpoint (kept) |
| POST | /api/player/metadata | Legacy v1 endpoint (kept) |

---

## Security notes

### SSRF protection
All URLs are validated through `url_validator.py` before any HTTP request.
Private IP ranges (RFC 1918), loopback, and cloud metadata endpoints (169.254.169.254) are blocked.

### FFmpeg safety
All FFmpeg/FFprobe invocations use `subprocess.exec` with a list of arguments.
User-supplied URLs are always passed as a single argv element, never interpolated into a shell string.

### Path traversal
The `/api/media/hls/{key}/{filename}` endpoint sanitises the filename with `Path(filename).name`,
ensuring it cannot escape the HLS output directory.

### Job ID validation
`/api/media/stream/{job_id}` rejects any job_id that is not alphanumeric
(our job IDs are SHA hex strings).

---

## Performance notes

- **FFprobe results are cached** in memory for 1 hour (configurable).
  The same URL will not trigger a second FFprobe invocation.
- **Remux jobs are deduplicated**: concurrent requests for the same URL
  share a single FFmpeg process.
- **The progress timer in useVideoPlayer is paused when video is paused**,
  saving unnecessary CPU cycles on the 250ms interval.
- **dash.js and hls.js are lazy-loaded** — they are only downloaded when
  a DASH or HLS source is actually selected, not on every page load.
- **Direct play never touches the backend pipeline** — local files and
  already-determined HLS sources bypass the probe/compat API entirely.
