@echo off
REM setup.bat — One-command setup for Aura Player v2 (Windows)
REM Usage: setup.bat

echo ===================================================
echo   Aura Player v2 - Setup
echo ===================================================

where ffmpeg >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [OK] ffmpeg found
) else (
    echo [WARN] ffmpeg not found on PATH.
    echo    Direct-play formats ^(MP4/H.264, WebM/VP9^) still work.
    echo    Download ffmpeg from https://ffmpeg.org/download.html for MKV/HEVC support.
)

echo.
echo -- Backend --
cd backend

if not exist ".venv" (
    echo Creating Python virtual environment...
    python -m venv .venv
)

echo Installing Python dependencies...
.venv\Scripts\pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt

if not exist ".env" (
    copy .env.example .env >nul
    echo [OK] Created backend\.env from example
) else (
    echo [OK] backend\.env already exists, leaving it untouched
)

cd ..

echo.
echo -- Frontend --

if not exist ".env" (
    echo VITE_API_URL=http://localhost:8000/api> .env
    echo [OK] Created .env
) else (
    echo [OK] .env already exists, leaving it untouched
)

echo Installing Node dependencies...
call npm install --silent

echo.
echo ===================================================
echo   Setup complete!
echo ===================================================
echo.
echo To run the app, open two terminals:
echo.
echo   Terminal 1 (backend):
echo     cd backend ^&^& .venv\Scripts\uvicorn app.main:app --reload --port 8000
echo.
echo   Terminal 2 (frontend):
echo     npm run dev
echo.
echo Then open http://localhost:5173
echo API docs available at http://localhost:8000/api/docs
echo.
pause
