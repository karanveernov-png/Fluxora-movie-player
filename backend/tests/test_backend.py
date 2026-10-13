"""
tests/test_backend.py — Aura Player v2 backend test suite.

Run:
    pip install pytest pytest-asyncio httpx --break-system-packages
    pytest tests/ -v

Test matrix covers:
  ✓ URL validator — safe URLs, SSRF, private IPs, schemes
  ✓ Compatibility engine — all strategy branches
  ✓ Probe parser — happy path + error cases
  ✓ Range-request streaming — 200, 206, 416
  ✓ API endpoints — health, probe, compat, play
  ✓ Security — path traversal in HLS segment endpoint
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

# ── Shared fixture: test app client ───────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


# ════════════════════════════════════════════════════════════
# 1. URL VALIDATOR
# ════════════════════════════════════════════════════════════

class TestURLValidator:
    def setup_method(self):
        from app.core.url_validator import validate_url, URLValidationError
        self.validate = validate_url
        self.Err = URLValidationError

    def test_valid_https(self):
        assert self.validate("https://example.com/video.mp4") == "https://example.com/video.mp4"

    def test_valid_http(self):
        assert self.validate("http://cdn.example.com/stream.m3u8") == "http://cdn.example.com/stream.m3u8"

    def test_strips_whitespace(self):
        assert self.validate("  https://example.com/v.mp4  ") == "https://example.com/v.mp4"

    def test_empty_raises(self):
        with pytest.raises(self.Err, match="empty"):
            self.validate("")

    def test_too_long_raises(self):
        with pytest.raises(self.Err, match="length"):
            self.validate("https://x.com/" + "a" * 2100)

    def test_ftp_scheme_raises(self):
        with pytest.raises(self.Err, match="scheme"):
            self.validate("ftp://example.com/video.mkv")

    def test_file_scheme_raises(self):
        with pytest.raises(self.Err, match="scheme"):
            self.validate("file:///etc/passwd")

    def test_userinfo_raises(self):
        with pytest.raises(self.Err, match="credential"):
            self.validate("https://user:pass@example.com/video.mp4")

    def test_localhost_blocked(self):
        """Loopback addresses must be blocked (SSRF)."""
        with pytest.raises(self.Err):
            self.validate("http://localhost/internal")

    def test_127_0_0_1_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://127.0.0.1:8080/secret")

    def test_no_hostname_raises(self):
        with pytest.raises(self.Err):
            self.validate("https:///no-host")


# ════════════════════════════════════════════════════════════
# 2. COMPATIBILITY ENGINE
# ════════════════════════════════════════════════════════════

class TestCompatibility:
    def setup_method(self):
        from app.media.compatibility import check_compatibility, PlaybackStrategy
        from app.media.probe import MediaInfo
        self.check = check_compatibility
        self.PS = PlaybackStrategy
        self.MI = MediaInfo

    def _make(self, container, video_codec, audio_codec, size=0, duration=0):
        return self.MI(
            url="http://x.com/v",
            container=container,
            duration=duration,
            bit_rate=0,
            size=size,
            video_codec=video_codec,
            audio_codec=audio_codec,
        )

    # ── Direct play cases ──────────────────────────────────────────────────

    def test_mp4_h264_aac_is_direct(self):
        r = self.check(self._make("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac"))
        assert r.strategy == self.PS.DIRECT

    def test_webm_vp9_opus_is_direct(self):
        r = self.check(self._make("webm", "vp9", "opus"))
        assert r.strategy == self.PS.DIRECT

    def test_mp4_av1_aac_is_direct(self):
        r = self.check(self._make("mov,mp4,m4a,3gp,3g2,mj2", "av1", "aac"))
        assert r.strategy == self.PS.DIRECT

    def test_mp4_h264_mp3_is_direct(self):
        r = self.check(self._make("mov,mp4,m4a,3gp,3g2,mj2", "h264", "mp3"))
        assert r.strategy == self.PS.DIRECT

    # ── Remux cases ────────────────────────────────────────────────────────

    def test_mkv_h264_aac_is_remux(self):
        """MKV container + compatible codecs → remux only."""
        r = self.check(self._make("matroska,webm", "h264", "aac"))
        assert r.strategy == self.PS.REMUX
        assert r.target_video_codec == "copy"
        assert r.target_audio_codec == "copy"

    def test_avi_h264_aac_is_remux(self):
        r = self.check(self._make("avi", "h264", "aac"))
        assert r.strategy == self.PS.REMUX

    def test_mkv_h264_dts_is_remux_audio_transcode(self):
        """MKV with DTS audio → remux video, transcode audio."""
        r = self.check(self._make("matroska,webm", "h264", "dts"))
        assert r.strategy == self.PS.REMUX
        assert r.target_video_codec == "copy"
        assert r.target_audio_codec == "aac"

    def test_mkv_h264_ac3_is_remux_audio_transcode(self):
        r = self.check(self._make("matroska,webm", "h264", "ac3"))
        assert r.strategy == self.PS.REMUX
        assert r.target_audio_codec == "aac"

    # ── Transcode cases ────────────────────────────────────────────────────

    def test_mkv_hevc_aac_is_transcode(self):
        """HEVC video → full transcode."""
        r = self.check(self._make("matroska,webm", "hevc", "aac"))
        assert r.strategy in (self.PS.TRANSCODE, self.PS.HLS_TRANSCODE)
        assert r.target_video_codec == "libx264"

    def test_large_file_hevc_is_hls_transcode(self):
        """Large HEVC file → HLS transcode (stream instead of download)."""
        r = self.check(self._make("matroska,webm", "hevc", "aac",
                                   size=600 * 1024 * 1024))
        assert r.strategy == self.PS.HLS_TRANSCODE

    def test_long_video_h264_is_hls_transcode(self):
        """Long H.264 that somehow ends up here → HLS."""
        # This would only happen if container is unsupported
        r = self.check(self._make("avi", "hevc", "aac", duration=3600))
        assert r.strategy == self.PS.HLS_TRANSCODE

    # ── Regression: MKV must never be DIRECT ──────────────────────────────

    def test_mkv_h264_aac_is_not_direct(self):
        """MKV (matroska,webm ffprobe id) with browser-compatible codecs must
        be REMUX, never DIRECT.  MKV is not playable in Chrome/Firefox without
        a plugin; only pure WebM containers ('webm') are browser-native."""
        r = self.check(self._make("matroska,webm", "h264", "aac"))
        assert r.strategy != self.PS.DIRECT, \
            "MKV/H264/AAC must not be DIRECT — it requires a remux to MP4"
        assert r.strategy == self.PS.REMUX

    def test_webm_vp9_opus_stays_direct(self):
        """Pure WebM (codec 'webm', not 'matroska,webm') with VP9/Opus is
        natively browser-playable and must stay DIRECT."""
        r = self.check(self._make("webm", "vp9", "opus"))
        assert r.strategy == self.PS.DIRECT

    # ── No codec data ──────────────────────────────────────────────────────

    def test_no_codec_data_is_direct(self):
        """When ffprobe has no data (e.g. HLS stream), attempt direct."""
        r = self.check(self._make("", "", ""))
        assert r.strategy == self.PS.DIRECT


# ════════════════════════════════════════════════════════════
# 3. PROBE PARSER
# ════════════════════════════════════════════════════════════

class TestProbeParser:
    def _sample_ffprobe_output(self) -> dict:
        return {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "120.5",
                "bit_rate": "2000000",
                "size": "30000000",
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "codec_long_name": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10",
                    "width": 1920, "height": 1080,
                    "r_frame_rate": "30/1",
                    "bit_rate": "1800000",
                    "pix_fmt": "yuv420p",
                    "profile": "High",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "codec_long_name": "AAC (Advanced Audio Coding)",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "tags": {"language": "eng"},
                },
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "codec_long_name": "SubRip subtitle",
                    "tags": {"language": "eng", "title": "English"},
                },
            ],
        }

    def test_parse_full(self):
        from app.media.probe import _parse_ffprobe
        info = _parse_ffprobe("http://x.com/v.mp4", self._sample_ffprobe_output())
        assert info.container == "mov,mp4,m4a,3gp,3g2,mj2"
        assert info.duration == 120.5
        assert info.bit_rate == 2_000_000
        assert info.size == 30_000_000
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert info.width == 1920
        assert info.height == 1080
        assert info.fps == pytest.approx(30.0)
        assert len(info.video_streams) == 1
        assert len(info.audio_streams) == 1
        assert len(info.subtitle_streams) == 1
        assert info.audio_streams[0].language == "eng"

    def test_parse_empty_streams(self):
        from app.media.probe import _parse_ffprobe
        info = _parse_ffprobe("http://x.com/v.mp4", {"format": {}, "streams": []})
        assert info.video_codec == ""
        assert info.audio_codec == ""
        assert info.duration == 0

    def test_fps_fraction(self):
        from app.media.probe import _parse_stream
        s = _parse_stream({"index":0,"codec_type":"video","codec_name":"h264",
                            "codec_long_name":"","r_frame_rate":"30000/1001"})
        assert abs(s.fps - 29.97) < 0.01

    def test_fps_zero_denominator(self):
        from app.media.probe import _parse_stream
        s = _parse_stream({"index":0,"codec_type":"video","codec_name":"h264",
                            "codec_long_name":"","r_frame_rate":"0/0"})
        assert s.fps is None


# ════════════════════════════════════════════════════════════
# 4. RANGE-REQUEST STREAMING
# ════════════════════════════════════════════════════════════

class TestStreaming:
    @pytest.fixture
    def tmp_mp4(self, tmp_path):
        """Create a fake 1 MB binary file simulating an MP4."""
        f = tmp_path / "test.mp4"
        f.write_bytes(b"\x00" * 1024 * 1024)  # 1 MB zeros
        return f

    @pytest.mark.asyncio
    async def test_full_file_200(self, tmp_mp4):
        from app.media.streaming import stream_file
        from starlette.testclient import TestClient
        from fastapi import FastAPI, Request
        from fastapi.responses import Response

        app = FastAPI()

        @app.get("/stream")
        async def s(request: Request):
            return await stream_file(tmp_mp4, request)

        with TestClient(app) as c:
            r = c.get("/stream")
            assert r.status_code == 200
            assert r.headers["accept-ranges"] == "bytes"
            assert len(r.content) == 1024 * 1024

    @pytest.mark.asyncio
    async def test_range_request_206(self, tmp_mp4):
        from app.media.streaming import stream_file
        from starlette.testclient import TestClient
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.get("/stream")
        async def s(request: Request):
            return await stream_file(tmp_mp4, request)

        with TestClient(app) as c:
            r = c.get("/stream", headers={"Range": "bytes=0-1023"})
            assert r.status_code == 206
            assert r.headers["content-range"] == f"bytes 0-1023/{1024*1024}"
            assert len(r.content) == 1024

    @pytest.mark.asyncio
    async def test_range_beyond_file_416(self, tmp_mp4):
        from app.media.streaming import stream_file
        from starlette.testclient import TestClient
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.get("/stream")
        async def s(request: Request):
            return await stream_file(tmp_mp4, request)

        with TestClient(app) as c:
            r = c.get("/stream", headers={"Range": "bytes=999999999-9999999999"})
            assert r.status_code == 416

    @pytest.mark.asyncio
    async def test_suffix_range_206(self, tmp_mp4):
        """bytes=-N (suffix range, RFC 7233 §2.1) must return the LAST N bytes."""
        from app.media.streaming import stream_file
        from starlette.testclient import TestClient
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.get("/stream")
        async def s(request: Request):
            return await stream_file(tmp_mp4, request)

        file_size = 1024 * 1024  # 1 MB
        suffix = 1000
        with TestClient(app) as c:
            r = c.get("/stream", headers={"Range": f"bytes=-{suffix}"})
            assert r.status_code == 206, f"Expected 206, got {r.status_code}"
            expected_start = file_size - suffix
            assert r.headers["content-range"] == f"bytes {expected_start}-{file_size-1}/{file_size}", \
                f"Wrong Content-Range: {r.headers.get('content-range')}"
            assert len(r.content) == suffix, f"Expected {suffix} bytes, got {len(r.content)}"

    @pytest.mark.asyncio
    async def test_nonexistent_file_404(self, tmp_path):
        from app.media.streaming import stream_file
        from starlette.testclient import TestClient
        from fastapi import FastAPI, Request

        app = FastAPI()
        fake = tmp_path / "nonexistent.mp4"

        @app.get("/stream")
        async def s(request: Request):
            return await stream_file(fake, request)

        with TestClient(app) as c:
            r = c.get("/stream")
            assert r.status_code == 404


# ════════════════════════════════════════════════════════════
# 5. API ENDPOINTS
# ════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "ffprobe_available" in body
        assert "ffmpeg_available" in body
        assert body["version"] == "2.0.0"


class TestProbeEndpoint:
    def test_probe_rejects_empty_url(self, client):
        r = client.post("/api/media/probe", json={"url": ""})
        assert r.status_code == 422

    def test_probe_rejects_non_http(self, client):
        r = client.post("/api/media/probe", json={"url": "ftp://example.com/v.mp4"})
        assert r.status_code == 422

    def test_probe_rejects_private_ip(self, client):
        r = client.post("/api/media/probe", json={"url": "http://192.168.1.1/v.mp4"})
        assert r.status_code == 422

    @patch("app.api.routes.media.probe")
    def test_probe_returns_info(self, mock_probe, client):
        from app.media.probe import MediaInfo
        mock_probe.return_value = MediaInfo(
            url="https://cdn.example.com/v.mp4",
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration=120.0, bit_rate=2_000_000, size=30_000_000,
            video_codec="h264", audio_codec="aac",
            width=1920, height=1080, fps=30.0,
        )
        r = client.post("/api/media/probe", json={"url": "https://cdn.example.com/v.mp4"})
        assert r.status_code == 200
        body = r.json()
        assert body["video_codec"] == "h264"
        assert body["width"] == 1920
        assert body["fps"] == 30.0


class TestCompatibilityEndpoint:
    @patch("app.api.routes.media.probe")
    def test_h264_mp4_returns_direct(self, mock_probe, client):
        from app.media.probe import MediaInfo
        mock_probe.return_value = MediaInfo(
            url="https://cdn.example.com/v.mp4",
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration=60, bit_rate=1_000_000, size=8_000_000,
            video_codec="h264", audio_codec="aac", width=1280, height=720, fps=30,
        )
        r = client.post("/api/media/compatibility",
                        json={"url": "https://cdn.example.com/v.mp4"})
        assert r.status_code == 200
        assert r.json()["strategy"] == "direct"

    @patch("app.api.routes.media.probe")
    def test_hevc_mkv_returns_transcode(self, mock_probe, client):
        from app.media.probe import MediaInfo
        mock_probe.return_value = MediaInfo(
            url="https://cdn.example.com/v.mkv",
            container="matroska,webm",
            duration=300, bit_rate=8_000_000, size=100_000_000,
            video_codec="hevc", audio_codec="aac", width=3840, height=2160, fps=24,
        )
        r = client.post("/api/media/compatibility",
                        json={"url": "https://cdn.example.com/v.mkv"})
        assert r.status_code == 200
        assert r.json()["strategy"] in ("transcode", "hls_transcode")
        assert r.json()["needs_processing"] is True


class TestPlayEndpoint:
    @patch("app.api.routes.media.probe")
    def test_direct_play_returns_original_url(self, mock_probe, client):
        from app.media.probe import MediaInfo
        url = "https://cdn.example.com/v.mp4"
        mock_probe.return_value = MediaInfo(
            url=url, container="mov,mp4,m4a,3gp,3g2,mj2",
            duration=60, bit_rate=1_000_000, size=8_000_000,
            video_codec="h264", audio_codec="aac", width=1280, height=720, fps=30,
        )
        r = client.post("/api/media/play", json={"url": url})
        assert r.status_code == 200
        body = r.json()
        assert body["play_url"] == url
        assert body["strategy"] == "direct"


class TestHLSSegmentSecurity:
    def test_path_traversal_rejected(self, client):
        """../../../etc/passwd must not be served."""
        r = client.get("/api/media/hls/abc123/../../../etc/passwd")
        # Should be 404 (sanitised) or 400, not 200
        assert r.status_code in (400, 404, 422)

    def test_invalid_key_rejected(self, client):
        r = client.get("/api/media/hls/../../evil/playlist.m3u8")
        assert r.status_code in (400, 404, 422)


# ════════════════════════════════════════════════════════════
# 6. MEDIA FORMAT MATRIX
# (Documents expected strategy per format combination)
# ════════════════════════════════════════════════════════════

MATRIX = [
    # (container,              video,   audio,   expected_strategy)
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264",  "aac",    "direct"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264",  "mp3",    "direct"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "vp9",   "opus",   "direct"),
    ("webm",                     "vp9",   "opus",   "direct"),
    ("webm",                     "av1",   "opus",   "direct"),
    ("matroska,webm",            "h264",  "aac",    "remux"),    # MKV → MP4 remux
    ("matroska,webm",            "h264",  "dts",    "remux"),    # + audio transcode
    ("matroska,webm",            "h264",  "ac3",    "remux"),    # + audio transcode
    ("avi",                      "h264",  "aac",    "remux"),
    ("matroska,webm",            "hevc",  "aac",    "transcode"),
    ("matroska,webm",            "hevc",  "dts",    "transcode"),
    ("avi",                      "hevc",  "aac",    "transcode"),
    ("asf",                      "wmv3",  "wmav2",  "transcode"),
]

@pytest.mark.parametrize("container,video,audio,expected", MATRIX)
def test_format_matrix(container, video, audio, expected):
    from app.media.compatibility import check_compatibility, PlaybackStrategy
    from app.media.probe import MediaInfo

    info = MediaInfo(
        url="http://x.com/v", container=container,
        duration=60, bit_rate=2_000_000, size=50_000_000,
        video_codec=video, audio_codec=audio,
    )
    result = check_compatibility(info)
    # HLS_TRANSCODE is also acceptable where TRANSCODE is expected
    strat = result.strategy.value
    if expected == "transcode":
        assert strat in ("transcode", "hls_transcode"), \
            f"{container}/{video}/{audio}: got {strat}, expected transcode"
    else:
        assert strat == expected, \
            f"{container}/{video}/{audio}: got {strat}, expected {expected}"
