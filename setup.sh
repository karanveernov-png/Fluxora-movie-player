#!/usr/bin/env bash
# setup.sh — One-command setup for Aura Player v2
# Usage: chmod +x setup.sh && ./setup.sh

set -e

echo "═══════════════════════════════════════════════════"
echo "  Aura Player v2 — Setup"
echo "═══════════════════════════════════════════════════"

# ── Check for ffmpeg/ffprobe ─────────────────────────────────────────────────
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  echo "✓ ffmpeg and ffprobe found"
else
  echo "⚠ ffmpeg/ffprobe not found on PATH."
  echo "  The player will still work for direct-play formats (MP4/H.264, WebM/VP9)."
  echo "  Install ffmpeg for MKV/HEVC/DTS remux+transcode support:"
  echo "    macOS:  brew install ffmpeg"
  echo "    Ubuntu: sudo apt-get install ffmpeg"
fi

# ── Backend setup ─────────────────────────────────────────────────────────────
echo ""
echo "── Backend ──"
cd backend

if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✓ Created backend/.env from example"
else
  echo "✓ backend/.env already exists, leaving it untouched"
fi

cd ..

# ── Frontend setup ────────────────────────────────────────────────────────────
echo ""
echo "── Frontend ──"

if [ ! -f ".env" ]; then
  echo "VITE_API_URL=http://localhost:8000/api" > .env
  echo "✓ Created .env"
else
  echo "✓ .env already exists, leaving it untouched"
fi

echo "Installing Node dependencies..."
npm install --silent

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════════════"
echo ""
echo "To run the app, open two terminals:"
echo ""
echo "  Terminal 1 (backend):"
echo "    cd backend && ./.venv/bin/uvicorn app.main:app --reload --port 8000"
echo ""
echo "  Terminal 2 (frontend):"
echo "    npm run dev"
echo ""
echo "Then open http://localhost:5173"
echo "API docs available at http://localhost:8000/api/docs"
echo ""
