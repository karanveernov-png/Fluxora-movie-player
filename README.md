# Aura Player v2

A production-grade media player with VLC-inspired playback strategy:
**Direct Play → Remux → Transcode → HLS**, built with React + FastAPI + FFmpeg.

## Quick start

### macOS / Linux
```bash
chmod +x setup.sh
./setup.sh
```

### Windows
```
setup.bat
```

The script installs all dependencies and creates `.env` files with working defaults.
No manual editing is required to get started.

### Run it

Open two terminals:

```bash
# Terminal 1 — backend
cd backend
./.venv/bin/uvicorn app.main:app --reload --port 8000    # macOS/Linux
.venv\Scripts\uvicorn app.main:app --reload --port 8000  # Windows

# Terminal 2 — frontend
npm run dev
```

Then open **http://localhost:5173**.

API documentation (interactive): **http://localhost:8000/api/docs**

---

## What this is

Drop in a local video file or paste a streaming URL (HLS, DASH, MP4, MKV, AVI…).
The backend probes it with FFprobe, decides the fastest safe way to play it
(direct play if the browser can handle it natively, otherwise remux or transcode
with FFmpeg), and the frontend plays whatever comes back — with resume-on-reload,
offline handling, quality/audio/subtitle switching, and a technical info panel.

See **MIGRATION.md** for the full architecture writeup, the playback-strategy
decision table, security notes, and a list of every file that changed and why.

## Requirements

- **Node.js** 18+ and npm
- **Python** 3.10+
- **FFmpeg + FFprobe** (optional — only needed for MKV/HEVC/DTS conversion;
  direct-play formats like MP4/H.264 and WebM/VP9 work without it)

## Project layout

```
aura-player-pro/
├── setup.sh / setup.bat      ← run this first
├── MIGRATION.md              ← full architecture + decision docs
├── src/                      ← React frontend
│   ├── App.tsx
│   ├── api/mediaApi.ts       ← backend client
│   ├── hooks/                ← useVideoPlayer, useResume, usePlayStrategy
│   ├── core/                 ← HLSManager, PlaybackManager
│   └── components/MoviePlayer/
└── backend/                  ← FastAPI backend
    ├── app/
    │   ├── main.py
    │   ├── media/            ← probe, compatibility, remux, hls_gen, streaming
    │   ├── core/              ← config, url_validator (SSRF guard)
    │   └── api/routes/media.py
    └── tests/test_backend.py ← 40+ tests, run with `pytest tests/ -v`
```

## Configuration

Both `.env` files are created automatically by the setup script with working
defaults. To customize (e.g. point to a specific FFmpeg install), edit:

- `backend/.env` — FFmpeg paths, timeouts, CORS origins, cache TTLs
- `.env` (project root) — `VITE_API_URL` for the frontend

See `backend/.env.example` for every available option with comments.
