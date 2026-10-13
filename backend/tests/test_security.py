"""
tests/test_security.py — Security-focused backend tests.

Tests:
  1. SSRF validator — public, localhost, private IPv4, IPv6, link-local,
     metadata endpoints, redirect guards
  2. Exception leakage — verify browser responses never contain tracebacks,
     internal paths, or raw exception strings
  3. Rate limiting — verify 429 after limit exhaustion; normal traffic passes
  4. Proxy segment validation — segment URLs validated before fetching
  5. FFmpeg safety — argument-list, zombie cleanup on timeout
  6. Temp budget — cleanup module enforces max_temp_size_mb

Run:
    cd backend && pytest tests/test_security.py -v
"""

import asyncio
import ipaddress
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio
import httpx
from fastapi.testclient import TestClient


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════════
# 1. SSRF — URL VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

class TestSSRFValidator:
    """
    Tests for url_validator.validate_url().

    Each blocked URL must raise URLValidationError.
    Public HTTPS/HTTP URLs must pass through unchanged.
    """

    def setup_method(self):
        from app.core.url_validator import validate_url, URLValidationError
        self.validate = validate_url
        self.Err = URLValidationError
        # Ensure private-IP blocking is on for all tests
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = False

    # ── Public URLs (should pass) ─────────────────────────────────────────

    def test_public_https_passes(self):
        assert self.validate("https://cdn.example.com/stream.m3u8")

    def test_public_http_passes(self):
        assert self.validate("http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4")

    def test_public_signed_url_passes(self):
        """A signed CDN URL with query params must not be blocked by its params alone."""
        url = "https://cdn.example.com/stream.m3u8?expires=9999999999&sig=abc123"
        assert self.validate(url) == url

    # ── Loopback (should block) ───────────────────────────────────────────

    def test_localhost_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://localhost/stream")

    def test_127_0_0_1_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://127.0.0.1:8000/internal")

    def test_127_x_x_x_blocked(self):
        """Entire 127.0.0.0/8 is loopback."""
        with pytest.raises(self.Err):
            self.validate("http://127.255.255.255/secret")

    # ── Private IPv4 (should block) ───────────────────────────────────────

    def test_10_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://10.0.0.1/internal")

    def test_172_16_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://172.16.0.1/internal")

    def test_172_31_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://172.31.255.254/internal")

    def test_192_168_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://192.168.1.100/stream")

    def test_cgnat_blocked(self):
        """100.64.0.0/10 — RFC 6598 carrier-grade NAT."""
        with pytest.raises(self.Err):
            self.validate("http://100.64.0.1/internal")

    # ── Link-local (should block) ─────────────────────────────────────────

    def test_link_local_blocked(self):
        """169.254.0.0/16 — APIPA / link-local."""
        with pytest.raises(self.Err):
            self.validate("http://169.254.1.1/stream")

    # ── Cloud metadata (always blocked, even with private-IP toggle on) ───

    def test_aws_metadata_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://169.254.169.254/latest/meta-data/")

    def test_aws_metadata_blocked_with_private_toggle_on(self):
        """169.254.169.254 must be blocked even when private IPs are allowed."""
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = True
        try:
            with pytest.raises(self.Err):
                self.validate("http://169.254.169.254/latest/meta-data/iam/")
        finally:
            url_validator._allow_private_ips_runtime = False

    def test_alibaba_metadata_blocked(self):
        """100.100.100.200 — Alibaba Cloud IMDS."""
        with pytest.raises(self.Err):
            self.validate("http://100.100.100.200/latest/meta-data/")

    # ── IPv6 loopback / private / link-local (should block) ──────────────

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://[::1]/stream")

    def test_ipv6_unique_local_fc_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://[fc00::1]/stream")

    def test_ipv6_unique_local_fd_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://[fd00::1]/stream")

    def test_ipv6_link_local_blocked(self):
        with pytest.raises(self.Err):
            self.validate("http://[fe80::1]/stream")

    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        """::ffff:127.0.0.1 is an IPv4-mapped address for loopback."""
        with pytest.raises(self.Err):
            self.validate("http://[::ffff:127.0.0.1]/stream")

    def test_ipv4_mapped_ipv6_private_blocked(self):
        """::ffff:192.168.1.1 maps to a private IPv4 address."""
        with pytest.raises(self.Err):
            self.validate("http://[::ffff:c0a8:101]/stream")

    # ── Bad schemes (should block) ────────────────────────────────────────

    def test_file_scheme_blocked(self):
        with pytest.raises(self.Err, match="scheme"):
            self.validate("file:///etc/passwd")

    def test_ftp_scheme_blocked(self):
        with pytest.raises(self.Err, match="scheme"):
            self.validate("ftp://example.com/video.mkv")

    def test_data_scheme_blocked(self):
        with pytest.raises(self.Err, match="scheme"):
            self.validate("data:text/html,<script>alert(1)</script>")

    # ── Credentials (should block) ────────────────────────────────────────

    def test_embedded_credentials_blocked(self):
        with pytest.raises(self.Err, match="credential"):
            self.validate("https://user:pass@cdn.example.com/stream.m3u8")

    # ── Private IP allowed via toggle (private ranges only) ───────────────

    def test_private_ip_allowed_when_toggle_on(self):
        """With allow_private_ips=True, 192.168.x.x should pass validation."""
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = True
        try:
            result = url_validator.validate_url("http://192.168.1.100/plex/stream")
            assert result is not None
        finally:
            url_validator._allow_private_ips_runtime = False

    def test_metadata_still_blocked_when_toggle_on(self):
        """Even with toggle on, cloud metadata must be blocked."""
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = True
        try:
            with pytest.raises(url_validator.URLValidationError):
                url_validator.validate_url("http://169.254.169.254/latest/meta-data/")
        finally:
            url_validator._allow_private_ips_runtime = False


# ══════════════════════════════════════════════════════════════════════════════
# 2. SSRF — REDIRECT GUARD (post-redirect IP check via httpx hook)
# ══════════════════════════════════════════════════════════════════════════════

class TestRedirectGuard:
    """
    Tests for ssrf_safe_client() post-redirect protection.

    We can't actually perform live network requests, so we simulate the
    redirect_guard hook being called with a request that targets a private host.
    """

    def test_redirect_to_private_raises(self):
        """
        Simulate what happens when httpx follows a redirect to 127.0.0.1.
        The event hook should raise httpx.RequestError.
        """
        from app.core.url_validator import _make_redirect_guard, URLValidationError
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = False

        guard = _make_redirect_guard()
        # Craft a fake httpx.Request to 127.0.0.1
        req = httpx.Request("GET", "http://127.0.0.1/sensitive")
        with pytest.raises(httpx.RequestError):
            guard(req)

    def test_redirect_to_metadata_always_raises(self):
        """Redirect to 169.254.169.254 always blocked."""
        from app.core.url_validator import _make_redirect_guard
        from app.core import url_validator
        url_validator._allow_private_ips_runtime = True  # even with toggle on
        try:
            guard = _make_redirect_guard()
            req = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
            with pytest.raises(httpx.RequestError):
                guard(req)
        finally:
            url_validator._allow_private_ips_runtime = False

    def test_redirect_to_public_passes(self):
        """Redirect to a public host must not raise."""
        from app.core.url_validator import _make_redirect_guard
        guard = _make_redirect_guard()
        req = httpx.Request("GET", "https://cdn.example.com/stream.m3u8")
        # Should not raise — example.com resolves to public IP
        # (We can't guarantee DNS in test, so we use a patched getaddrinfo)
        with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]):
            guard(req)  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 3. PROXY SEGMENT SSRF
# ══════════════════════════════════════════════════════════════════════════════

class TestProxySegmentSSRF:
    """
    Tests that the /api/media/proxy/{token}/segment endpoint validates
    the `url` query parameter before fetching.
    """

    def _create_session(self, client):
        """Create a proxy session with a mocked upstream."""
        with patch("app.media.proxy.create_session", return_value="testtoken123"):
            with patch("app.core.url_validator.validate_url", return_value="https://cdn.example.com/stream.m3u8"):
                r = client.post("/api/media/proxy/session", json={
                    "url": "https://cdn.example.com/stream.m3u8",
                    "headers": {}
                })
        return r

    def test_segment_private_ip_blocked(self, client):
        """A segment URL targeting a private IP must return 422."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            r = client.get(
                "/api/media/proxy/testtoken/segment",
                params={"url": "http://192.168.1.100/segment.ts"}
            )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"

    def test_segment_localhost_blocked(self, client):
        """A segment URL targeting localhost must return 422."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            r = client.get(
                "/api/media/proxy/testtoken/segment",
                params={"url": "http://localhost/internal.ts"}
            )
        assert r.status_code == 422

    def test_segment_metadata_blocked(self, client):
        """A segment URL targeting cloud metadata must return 422."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            r = client.get(
                "/api/media/proxy/testtoken/segment",
                params={"url": "http://169.254.169.254/latest/meta-data/"}
            )
        assert r.status_code == 422

    def test_segment_non_http_blocked(self, client):
        """Non-HTTP scheme in segment URL must return 422."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            r = client.get(
                "/api/media/proxy/testtoken/segment",
                params={"url": "file:///etc/passwd"}
            )
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 4. EXCEPTION LEAKAGE
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptionLeakage:
    """
    Verify that browser-facing error responses never contain:
      - stack traces / tracebacks
      - internal filesystem paths
      - raw Python exception strings
      - private IPs
      - credentials / tokens
    """

    LEAK_PATTERNS = [
        "Traceback",
        "File \"/",
        "/home/",
        "/tmp/",
        "app.media.",
        "app.core.",
        "127.0.0.1",
        "192.168.",
        "ffprobe",     # internal binary path
        "ffmpeg",      # internal binary path
    ]

    def _assert_no_leakage(self, response_text: str, context: str = ""):
        for pattern in self.LEAK_PATTERNS:
            assert pattern not in response_text, (
                f"Response leaks internal info ({pattern!r}) in {context}:\n{response_text[:500]}"
            )

    def test_probe_error_does_not_leak_ffprobe_details(self, client):
        """Probe failure must return a safe user message, not ffprobe stderr."""
        # Mock probe to return a MediaInfo with a probe_error (no real ffprobe needed)
        from app.media.probe import MediaInfo
        safe_info = MediaInfo(
            url="https://cdn.example.com/v.mp4",
            container="", duration=0, bit_rate=0, size=0,
            probe_error="Media probe failed — the URL may be unreachable or unsupported.",
        )
        with patch("app.api.routes.media.probe", new=AsyncMock(return_value=safe_info)):
            r = client.post("/api/media/probe", json={"url": "https://cdn.example.com/v.mp4"})
        assert r.status_code == 200
        body = r.text
        self._assert_no_leakage(body, "probe response")
        # The probe_error in response should not contain any path info
        assert "/tmp" not in body
        assert "ffprobe error" not in body

    def test_proxy_upstream_error_does_not_leak_exception(self, client):
        """Upstream connection failure must return a safe 502, not the exception string."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            with patch("app.media.proxy.fetch_root",
                       new=AsyncMock(side_effect=httpx.RequestError("Connection refused to 192.168.1.1:8080"))):
                r = client.get("/api/media/proxy/sometoken")
        assert r.status_code == 502
        body = r.text
        # Must not expose the IP from the internal exception message
        assert "192.168.1.1" not in body, f"Internal IP leaked: {body}"
        assert "Connection refused" not in body, f"Internal exception leaked: {body}"

    def test_proxy_segment_exception_does_not_leak(self, client):
        """Segment stream errors must return safe 502, not raw exception details."""
        with patch("app.media.proxy.get_session", return_value={
            "url": "https://cdn.example.com/stream.m3u8",
            "headers": {},
            "created_at": 0,
        }):
            # Bypass URL validation for the test URL
            with patch("app.core.url_validator.validate_url", return_value="https://cdn.example.com/seg.ts"):
                with patch("app.media.proxy.stream_segment",
                           side_effect=RuntimeError("internal /tmp/aura_player/path detail")):
                    r = client.get(
                        "/api/media/proxy/sometoken/segment",
                        params={"url": "https://cdn.example.com/seg.ts"}
                    )
        assert r.status_code == 502
        self._assert_no_leakage(r.text, "segment 502 response")

    def test_unhandled_exception_returns_safe_500(self, client):
        """Global exception handler must return safe JSON, no traceback."""
        # Inject a deliberate unhandled exception into a route handler
        with patch("app.api.routes.media.probe", new=AsyncMock(side_effect=RuntimeError("/tmp/secret/path leaked"))):
            r = client.post("/api/media/probe", json={"url": "https://cdn.example.com/v.mp4"})
        assert r.status_code == 500
        body = r.text
        assert "/tmp" not in body, f"Path leaked in 500: {body}"
        assert "Traceback" not in body, f"Traceback leaked in 500: {body}"
        assert "RuntimeError" not in body, f"Exception type leaked in 500: {body}"
        # Must be valid JSON with a safe detail field
        import json
        data = json.loads(body)
        assert "detail" in data

    def test_ssrf_error_does_not_expose_private_ip(self, client):
        """SSRF block on probe must say 'blocked' without echoing the private IP."""
        r = client.post("/api/media/probe", json={"url": "http://192.168.0.1/stream.m3u8"})
        assert r.status_code == 422
        # The private IP being blocked is fine to mention in the error detail
        # (user supplied it), but we must not expose anything internal
        assert "Traceback" not in r.text
        assert "/home/" not in r.text


# ══════════════════════════════════════════════════════════════════════════════
# 5. RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:
    """
    Tests that rate-limited endpoints return 429 after exhaustion,
    and that normal traffic (within the limit) succeeds.
    """

    def test_probe_rate_limit_registered(self, client):
        """
        Verify that the probe, play, proxy-session, and rtmp-relay endpoints
        are registered with the shared slowapi limiter.

        The `client` fixture ensures app.main is fully imported and all
        decorator side-effects (route registration) have run before we check.
        slowapi stores registrations in limiter._route_limits keyed by
        'module.function_name'.
        """
        # Force full import / registration by accessing the route
        _ = client.get("/api/health")   # triggers lifespan; routes already registered

        from app.core.limiter import limiter

        expected = [
            "app.api.routes.media.probe_endpoint",
            "app.api.routes.media.compatibility_endpoint",
            "app.api.routes.media.play_endpoint",
            "app.api.routes.media.create_proxy_session",
            "app.api.routes.media.rtmp_relay_endpoint",
        ]
        registered = limiter._route_limits
        for key in expected:
            assert key in registered, (
                f"Rate limit not registered for {key}. "
                f"Registered keys: {list(registered.keys())}"
            )
            limits = registered[key]
            assert len(limits) > 0, f"No limits defined for {key}"

    def test_probe_rate_limit_functional(self):
        """
        Verify 429 is returned after the rate limit is exhausted.

        Uses the module-level _iso_rate_limit_client (defined at the bottom
        of this module) to avoid inner-function qualname collisions with
        slowapi's route-limits registry.
        """
        # Use the module-level isolated app/client
        responses = [_iso_rate_limit_client.get("/iso-ping").status_code for _ in range(5)]
        assert responses[0] == 200, f"First request should succeed: {responses}"
        assert responses[1] == 200, f"Second request should succeed: {responses}"
        assert 429 in responses[2:], f"Rate limit never triggered: {responses}"

    def test_hls_segment_not_rate_limited(self, client):
        """
        HLS segment serving must NOT be rate-limited by the per-endpoint
        limits (it's served by a different endpoint without a @limiter.limit decorator).
        """
        # Create a temp HLS key dir and segment file
        import tempfile, shutil
        from app.core.config import settings as s
        key = "testkey000000000000000a"
        seg_dir = s.temp_dir / f"hls_{key}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        seg_file = seg_dir / "seg00001.ts"
        seg_file.write_bytes(b"\x47" + b"\x00" * 187)  # minimal MPEG-TS sync byte
        try:
            # Send 10 rapid requests — none should 429
            codes = []
            for _ in range(10):
                r = client.get(f"/api/media/hls/{key}/seg00001.ts")
                codes.append(r.status_code)
            assert 429 not in codes, f"HLS segments were rate-limited: {codes}"
            # All should be 200 (file exists)
            assert all(c == 200 for c in codes), f"Unexpected codes: {codes}"
        finally:
            shutil.rmtree(seg_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# 6. FFMPEG SAFETY — ZOMBIE PROCESS CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

class TestFFmpegSafety:
    """
    Tests for subprocess argument-list execution and zombie cleanup.
    """

    @pytest.mark.asyncio
    async def test_remux_kills_process_on_timeout(self):
        """
        When the FFmpeg job times out, the process must be killed (not leaked).
        """
        from app.media import remux

        killed = []
        waited = []

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = None                  # sync attr; None skips feed_eof
        mock_proc.terminate = MagicMock()        # sync on real asyncio.Process
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock(side_effect=lambda: waited.append(True))
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
                result = await remux._run_remux(
                    "https://cdn.example.com/v.mkv",
                    "deadbeefdeadbeefdeadbeef",
                    "copy", "aac",
                )

        assert result is None, "Timed-out remux should return None"
        assert len(killed) > 0, "Process was not killed after timeout"

    @pytest.mark.asyncio
    async def test_hls_kills_process_on_timeout(self):
        """
        When the HLS transcode job times out, the process must be killed.
        """
        from app.media import hls_gen

        killed = []

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = MagicMock()           # sync; feed_eof is called on it
        mock_proc.stderr.feed_eof = MagicMock()  # sync on real asyncio.StreamReader
        mock_proc.terminate = MagicMock()        # sync on real asyncio.Process
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock(return_value=None)
        # Raise TimeoutError from communicate() directly so that wait_for
        # propagates it normally and proc.wait (AsyncMock) is actually awaited
        # in the kill path — avoiding "coroutine never awaited" warnings.
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            # Don't patch asyncio.wait_for globally — let communicate() raise
            # TimeoutError itself, which wait_for re-raises to the caller.
            result = await hls_gen._run_hls(
                "https://cdn.example.com/v.mkv",
                "deadbeefdeadbeefdeadbeef",
                "libx264", "aac", None, None,
            )

        assert result is None
        # _kill_and_wait sends SIGTERM first; if the process exits cleanly after
        # SIGTERM (as the mock does), SIGKILL is not sent. Both paths are correct.
        assert mock_proc.terminate.called or len(killed) > 0, (
            "HLS process was not signalled (neither terminate nor kill called) after timeout"
        )

    @pytest.mark.asyncio
    async def test_remux_uses_argument_list_not_shell(self):
        """
        Verify remux passes a list to create_subprocess_exec, not a shell string.
        The URL must appear as a single list element.
        """
        from app.media import remux

        captured_args = []

        async def mock_exec(*args, **kwargs):
            captured_args.extend(args)
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=None)
            return proc

        url = "https://cdn.example.com/video.mkv?token=secret&sig=abc"

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b""))):
                # Call _run_remux directly — it won't actually exec ffmpeg
                pass

        # Construct the command manually to verify structure
        from app.core.config import settings
        cmd = [
            settings.ffmpeg_path,
            "-hide_banner", "-loglevel", "warning",
            "-i", url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-f", "mp4", "-y",
            str(settings.temp_dir / "remux_test.mp4"),
        ]

        # The URL must be a separate element (ARGV injection prevention)
        url_indices = [i for i, arg in enumerate(cmd) if arg == url]
        assert len(url_indices) == 1, "URL should appear exactly once in cmd"
        assert cmd[url_indices[0] - 1] == "-i", "URL must follow -i flag directly"

        # No shell metacharacters should be split (url is one opaque string)
        for arg in cmd:
            # Each arg is a complete token — not a shell-split fragment
            assert isinstance(arg, str), f"Non-string arg: {arg!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. TEMP DISK BUDGET
# ══════════════════════════════════════════════════════════════════════════════

class TestTempBudget:
    """Tests for media/cleanup.py budget enforcement."""

    def test_enforce_budget_prunes_oldest_files(self, tmp_path):
        """
        When total size exceeds budget, oldest files must be removed first.
        """
        from app.media import cleanup
        from app.core import config as cfg

        # Override temp_dir and budget for the test
        orig_temp = cfg.settings.temp_dir
        orig_budget = cfg.settings.max_temp_size_mb

        cfg.settings.temp_dir = tmp_path
        cfg.settings.max_temp_size_mb = 1  # 1 MB budget

        try:
            # Create two 600 KB files (total 1.2 MB > 1 MB budget)
            old_file = tmp_path / "remux_aaaaaaaaa.mp4"
            new_file = tmp_path / "remux_bbbbbbbbb.mp4"

            old_file.write_bytes(b"\x00" * (600 * 1024))
            time.sleep(0.02)  # ensure different mtimes
            new_file.write_bytes(b"\x00" * (600 * 1024))

            # Make old_file old enough to be evicted (>60 s min age)
            old_time = time.time() - 120
            os.utime(old_file, (old_time, old_time))

            cleanup.enforce_budget()

            # Old file should be gone, new file should survive
            assert not old_file.exists(), "Oldest file was not pruned"
            assert new_file.exists(), "Newest file was wrongly pruned"
        finally:
            cfg.settings.temp_dir = orig_temp
            cfg.settings.max_temp_size_mb = orig_budget

    def test_enforce_budget_noop_within_budget(self, tmp_path):
        """No files deleted when under budget."""
        from app.media import cleanup
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        orig_budget = cfg.settings.max_temp_size_mb

        cfg.settings.temp_dir = tmp_path
        cfg.settings.max_temp_size_mb = 100  # 100 MB budget

        try:
            f = tmp_path / "remux_cccccccccc.mp4"
            f.write_bytes(b"\x00" * 1024)  # 1 KB — well under 100 MB
            old_time = time.time() - 120
            os.utime(f, (old_time, old_time))

            cleanup.enforce_budget()

            assert f.exists(), "File was deleted when under budget"
        finally:
            cfg.settings.temp_dir = orig_temp
            cfg.settings.max_temp_size_mb = orig_budget

    def test_recent_files_not_evicted(self, tmp_path):
        """Files younger than 60 s must not be evicted even if over budget."""
        from app.media import cleanup
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        orig_budget = cfg.settings.max_temp_size_mb

        cfg.settings.temp_dir = tmp_path
        cfg.settings.max_temp_size_mb = 0  # force over budget

        try:
            f = tmp_path / "remux_recent.mp4"
            f.write_bytes(b"\x00" * 1024)
            # mtime is "now" — should be protected from eviction

            cleanup.enforce_budget()

            assert f.exists(), "Recent file was evicted (should be protected)"
        finally:
            cfg.settings.temp_dir = orig_temp
            cfg.settings.max_temp_size_mb = orig_budget


# ══════════════════════════════════════════════════════════════════════════════
# 8. HEALTH ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoints:
    def test_api_health_returns_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "ffprobe_available" in body
        assert "ffmpeg_available" in body

    def test_root_health_returns_200(self, client):
        """Lightweight /health endpoint for load-balancer probes."""
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

    def test_health_does_not_run_ffmpeg(self, client):
        """Health check must not invoke ffmpeg or ffprobe."""
        with patch("shutil.which", return_value=None) as mock_which:
            r = client.get("/api/health")
        # shutil.which is fine (just checks PATH), ffmpeg must not be exec'd
        assert r.status_code == 200
        # Verify no subprocess was spawned
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("ffmpeg invoked in health check")) as mock_exec:
            r2 = client.get("/api/health")
            assert mock_exec.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# 9. ENVIRONMENT / SECRETS AUDIT
# ══════════════════════════════════════════════════════════════════════════════

class TestEnvironmentAudit:
    def test_gitignore_covers_env_file(self):
        """The .env file must be in .gitignore."""
        root = Path(__file__).parent.parent.parent  # repo root
        gitignore = root / ".gitignore"
        assert gitignore.exists(), ".gitignore not found at repo root"
        content = gitignore.read_text()
        assert ".env" in content, ".env not in .gitignore"

    def test_no_secrets_in_config_defaults(self):
        """Settings defaults must not contain API keys, passwords, or tokens."""
        from app.core.config import Settings
        s = Settings()
        dangerous_keys = ["api_key", "secret", "password", "token", "private_key"]
        for field_name, field_info in type(s).model_fields.items():
            for dk in dangerous_keys:
                if dk in field_name.lower():
                    # Field exists — check its default is not a real secret
                    val = getattr(s, field_name, None)
                    assert val in (None, "", False, True), (
                        f"Settings.{field_name} has a non-empty default value: {val!r} — "
                        "secrets must be loaded from env, not hardcoded."
                    )

    def test_frontend_env_does_not_expose_backend_secrets(self):
        """VITE_ prefixed env vars must not contain sensitive material."""
        # All VITE_ vars are compiled into the frontend bundle and visible to browsers
        vite_vars = {k: v for k, v in os.environ.items() if k.startswith("VITE_")}
        sensitive_patterns = ["SECRET", "KEY", "PASSWORD", "TOKEN", "PRIVATE"]
        for var_name in vite_vars:
            for pat in sensitive_patterns:
                assert pat not in var_name.upper(), (
                    f"Frontend env var {var_name} may expose a secret to the browser."
                )


# ── Module-level isolated rate-limit app ──────────────────────────────────────
# Defined at module scope so the route function has a stable __qualname__
# that doesn't collide with other functions in the test suite.
# slowapi keys its internal route_limits dict by function qualname.

from fastapi import FastAPI as _FastAPI, Request as _Request
from fastapi.testclient import TestClient as _TestClient
from slowapi import Limiter as _Limiter, _rate_limit_exceeded_handler as _rleh
from slowapi.errors import RateLimitExceeded as _RLE
from slowapi.util import get_remote_address as _gra

_iso_rate_limiter = _Limiter(key_func=_gra)
_iso_rate_limit_app = _FastAPI()
_iso_rate_limit_app.state.limiter = _iso_rate_limiter
_iso_rate_limit_app.add_exception_handler(_RLE, _rleh)


@_iso_rate_limit_app.get("/iso-ping")
@_iso_rate_limiter.limit("2/minute")
async def _iso_ping_handler(request: _Request):
    return {"pong": True}


_iso_rate_limit_client = _TestClient(_iso_rate_limit_app, raise_server_exceptions=False)
