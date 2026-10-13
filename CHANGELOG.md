# Changelog — October 2026 stabilization & rebrand pass

Everything below was found and fixed by actually building, installing, and
running the project — not just reading the code. Each claim is reproducible;
see "How to verify" at the bottom.

## Backend — critical fixes (app could not start or serve requests before this)

### 1. Missing dependency: `slowapi`
`app/main.py` and `app/core/limiter.py` import `slowapi` for rate limiting,
but it was never listed in `backend/requirements.txt`. A fresh install via
`setup.sh` installed everything *except* the one package the app needs to
boot, and the server crashed immediately on startup with
`ModuleNotFoundError: No module named 'slowapi'`.

**Fix:** added `slowapi==0.1.10` to `requirements.txt`.

### 2. Every rate-limited API route failed to load
`app/api/routes/media.py` had `from __future__ import annotations` at the
top, which turns every type hint in the file into a string to be resolved
later. The `@limiter.limit(...)` decorator on each route wraps the endpoint
function in a new function *defined inside slowapi's own module*. When
FastAPI/Pydantic tried to resolve the stringified hints (e.g. `ProbeRequest`),
they resolved them using that wrapper's module globals — slowapi's — where
`ProbeRequest` doesn't exist, raising `NameError: name 'ProbeRequest' is not
defined`. This broke `router` import entirely, which means **probe, play,
compatibility, proxy session, and RTMP relay — the entire API surface — never
loaded**, 404-ing on every request. The exact same pattern was duplicated in
`tests/test_security.py`'s isolated test app, breaking test collection too.

**Fix:** removed `from __future__ import annotations` from both files. All
type hints in both files already resolve fine natively under Python 3.10+
without it (confirmed — no `list[X]`/`X | Y` forward-ref issues remained).

**Verified:** 149/149 backend tests pass; the live server boots and serves
all 21 routes.

### 3. Live end-to-end verification (beyond the test suite)
With the app actually running, I exercised every point in the playback
pipeline against real media, not mocks:
- Direct probe/play of a remote MP4 — confirmed correct strategy decision.
- Full **remux** pipeline (MKV → MP4) — confirmed the output file is valid,
  playable MP4 (verified with `ffprobe`).
- Full **HLS transcode** pipeline (HEVC → H.264/AAC HLS) — confirmed a real
  `.m3u8` playlist and valid `.ts` segments were generated and play back.
- **Live RTMP relay** — stood up a real RTMP ingest server, pushed a
  continuous live test feed into it (simulating OBS/a camera), pointed the
  backend's relay at it, and confirmed it produced a genuinely live, growing
  HLS playlist with new valid segments appearing over time. This is the
  feature behind your "live streaming" requirement, and it works correctly.

### 4. Hardening: `TrustedHostMiddleware` was imported but never used
`main.py` imported `TrustedHostMiddleware` and never called
`app.add_middleware(...)` with it — dead code, and a missed layer of
production hardening (Host-header allow-listing). Wired it up properly:
- Added `trusted_hosts: list[str] = ["*"]` to `Settings` (default stays fully
  permissive for local dev — no behavior change out of the box).
- Added it to the middleware stack in the correct order — **after**
  `ForwardedHeadersMiddleware` is added, which (because Starlette executes
  middleware in reverse-registration order) makes it run **before**
  `ForwardedHeadersMiddleware`, so it checks the real Host header rather than
  one a client could overwrite via a forged `X-Forwarded-Host`.
- Documented `AURA_TRUSTED_HOSTS` in `.env.example` for production use.

### 5. Minor cleanup
Removed 4 unused imports (`Any` in `schemas/media.py`, `playlist_path` in
`routes/media.py`, `URLValidationError` in `media/proxy.py`) and an unused
local variable in `media/probe.py`, found via `pyflakes`. Zero functional
change — just a clean lint pass.

## Frontend — brand identity fix

The entire color system (`App.css` **and** `MoviePlayer.css`, two separate
token definitions) was built on `#E50914` — **Netflix's exact trademark
red**. For a product you want to look official and original, shipping a
competitor's exact brand color as your own primary accent is a real problem,
not a style nitpick.

**Fix — new "Signal Blue" identity** (`#4C7CF2`), grounded in what the
product actually is (a broadcast/technical streaming tool, not a consumer
entertainment app):
- Replaced the accent token in both `App.css` and `MoviePlayer.css` and every
  derived usage (hover states, active states, glows, toggles).
- Checked contrast: accent-on-background now measures 5.16:1 (passes WCAG AA
  for body text; the old red measured lower).
- Replaced the generic ▶-in-a-box logo mark (used by nearly every video app)
  with an original signal/equalizer-bars glyph that ties to the product's
  actual subject matter — streaming signal.
- Updated the browser tab favicon and `theme-color` to match.
- Fixed a color mismatch in `index.html`'s pre-render background (`#020008`,
  a dark purple-black that didn't match the app's actual `#0a0a0a` background)
  that would have caused a visible flash when the React app mounted.
- Tokenized one stray hardcoded error-text color (`#ff6b6b` → `var(--red-soft)`).
- Removed two generic/templated UI patterns: the all-caps hero eyebrow label
  (now sentence case) and a two-tone (bold/light) headline treatment where the
  second line was styled differently from the first for no functional reason.

No layout, component structure, or interaction logic was touched — this was
a deliberate, scoped identity fix on top of a frontend that already compiles
cleanly and was well engineered going in.

## How to verify

```bash
# Backend
cd backend
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest tests/ -v      # → 149 passed
./.venv/bin/uvicorn app.main:app --port 8000

# Frontend
cd ..
npm install
npx tsc --noEmit    # → no errors
npm run build        # → clean build
npm run dev
```
