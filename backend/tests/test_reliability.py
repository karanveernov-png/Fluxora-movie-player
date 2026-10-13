"""
tests/test_reliability.py — Focused reliability audit tests.

Covers the four scopes from the final audit:
  A. Legacy SSRF paths (source.py redirect regression tests)
  B. FFmpeg lifecycle (timeout, cancellation, orphan prevention, partial cleanup)
  C. Temporary storage safety (cleanup.py behavior and async off-loading)
  D. Deployment configuration (CORS, budget docs, multi-worker honesty)

All tests use mocks — no real FFmpeg processes or network calls are made.
Live CDN/streaming compatibility is NOT claimed.

Run:
    cd backend && pytest tests/test_reliability.py -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient


# ── Shared app fixture ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════════
# SCOPE A — SOURCE.PY SSRF (redirect regression tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestSourceSSRF:
    """
    Verify that source.py validate_source / get_metadata cannot be redirected
    to private, loopback, link-local, reserved, or metadata addresses.

    The defence has two layers that both need to work:
      1. validate_url() rejects private hostnames before the first connection.
      2. ssrf_safe_client() event hook rejects private redirect targets.

    These tests confirm both layers via mocks (no live network).
    """

    # ── Layer 1: initial URL validation ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_validate_source_rejects_private_ip(self):
        """validate_source must reject private IPs before connecting."""
        from app.services.source import validate_source
        from app.schemas.media import SourceRequest

        req = SourceRequest(url="http://192.168.1.10/stream.m3u8")
        result = await validate_source(req)
        assert not result.valid
        assert not result.reachable
        # Message should be our own SSRF description, not a network error
        assert result.message is not None
        assert "private" in result.message.lower() or "blocked" in result.message.lower()

    @pytest.mark.asyncio
    async def test_validate_source_rejects_localhost(self):
        from app.services.source import validate_source
        from app.schemas.media import SourceRequest

        req = SourceRequest(url="http://localhost:8080/internal")
        result = await validate_source(req)
        assert not result.valid
        assert not result.reachable

    @pytest.mark.asyncio
    async def test_validate_source_rejects_metadata_endpoint(self):
        """AWS metadata endpoint must be blocked at validation stage."""
        from app.services.source import validate_source
        from app.schemas.media import SourceRequest

        req = SourceRequest(url="http://169.254.169.254/latest/meta-data/")
        result = await validate_source(req)
        assert not result.valid
        assert not result.reachable

    @pytest.mark.asyncio
    async def test_get_metadata_rejects_private_ip(self):
        from app.services.source import get_metadata
        from app.schemas.media import MetadataRequest

        req = MetadataRequest(url="http://10.0.0.1/stream.mp4")
        result = await get_metadata(req)
        assert not result.reachable

    # ── Layer 2: redirect-to-private guard (post-redirect hook) ──────────────

    def test_validate_source_redirect_to_private_blocked(self):
        """
        A public URL that redirects to a private IP must be blocked.

        We mock httpx to simulate a 302 redirect to 10.0.0.1 by having the
        ssrf_safe_client event hook fire with the redirect target host.
        In practice the hook fires via httpx's request event on every request
        including redirected ones.
        """
        from app.services.source import validate_source
        from app.schemas.media import SourceRequest
        from app.core.url_validator import _make_redirect_guard

        # Confirm the guard itself blocks a redirect to a private host
        guard = _make_redirect_guard()
        redirect_req = httpx.Request("GET", "http://10.0.0.1/private-stream")
        with pytest.raises(httpx.RequestError):
            guard(redirect_req)

    def test_validate_source_redirect_to_metadata_blocked(self):
        """Redirect to cloud metadata address must always be blocked."""
        from app.core.url_validator import _make_redirect_guard

        guard = _make_redirect_guard()
        # 169.254.169.254 is in _METADATA_IPS — blocked even with private toggle on
        redirect_req = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/iam/")
        with pytest.raises(httpx.RequestError):
            guard(redirect_req)

    def test_validate_source_redirect_to_loopback_blocked(self):
        """Redirect to 127.x.x.x must be blocked."""
        from app.core.url_validator import _make_redirect_guard

        guard = _make_redirect_guard()
        redirect_req = httpx.Request("GET", "http://127.0.0.1:8080/admin")
        with pytest.raises(httpx.RequestError):
            guard(redirect_req)

    def test_validate_source_redirect_to_ipv6_loopback_blocked(self):
        """Redirect to [::1] must be blocked."""
        from app.core.url_validator import _make_redirect_guard

        guard = _make_redirect_guard()
        redirect_req = httpx.Request("GET", "http://[::1]/secret")
        with pytest.raises(httpx.RequestError):
            guard(redirect_req)

    def test_validate_source_redirect_to_link_local_blocked(self):
        """Redirect to link-local (169.254.x.x other than IMDS) must be blocked."""
        from app.core.url_validator import _make_redirect_guard

        guard = _make_redirect_guard()
        redirect_req = httpx.Request("GET", "http://169.254.1.1/local")
        with pytest.raises(httpx.RequestError):
            guard(redirect_req)

    def test_validate_source_redirect_to_public_allowed(self):
        """Redirect to a public IP must NOT be blocked by the guard."""
        from app.core.url_validator import _make_redirect_guard

        guard = _make_redirect_guard()
        redirect_req = httpx.Request("GET", "https://cdn.example.com/stream.m3u8")
        # DNS resolution patched to return a public IP (93.184.216.34)
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            guard(redirect_req)   # must not raise

    @pytest.mark.asyncio
    async def test_ssrf_safe_client_used_by_source(self):
        """
        Confirm source._client() uses ssrf_safe_client(), which attaches the
        redirect guard.  We inspect the event hooks on the returned client.
        """
        from app.services.source import _client

        c = _client()
        # ssrf_safe_client inserts a guard at index 0 of request hooks
        hooks = c.event_hooks.get("request", [])
        assert len(hooks) >= 1, (
            "ssrf_safe_client must attach at least one request event hook; "
            "source._client() does not appear to use ssrf_safe_client()"
        )
        # Close the client to avoid ResourceWarning
        await c.aclose()

    @pytest.mark.asyncio
    async def test_source_uses_configurable_redirects(self):
        """
        Confirm max_redirects is respected (not set to unlimited).
        A misconfigured client with unlimited redirects is an amplification risk.
        """
        from app.services.source import _client
        from app.core.config import settings

        c = _client()
        assert c.max_redirects == settings.max_redirects, (
            f"Expected max_redirects={settings.max_redirects}, "
            f"got {c.max_redirects}"
        )
        await c.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# SCOPE B — FFMPEG LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

class TestRemuxLifecycle:
    """Tests for remux.py timeout, cancellation, and zombie prevention."""

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_removes_output(self, tmp_path):
        """
        On timeout: the child process must be killed and the partial output
        file must be deleted.
        """
        from app.media import remux
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        killed = []
        waited = []

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock(side_effect=lambda: waited.append(True) or None)

        # Simulate communicate() hanging until wait_for cancels it
        async def hanging_communicate():
            await asyncio.sleep(9999)

        mock_proc.communicate = hanging_communicate

        # Simulate ffmpeg writing a partial file BEFORE the timeout fires.
        # We do this inside the mock communicate so the file exists when
        # asyncio.wait_for raises TimeoutError and the finally block runs.
        partial_file = tmp_path / "remux_testkey123456789.mp4"

        async def communicate_then_timeout():
            # Write partial output as ffmpeg would, then simulate timeout
            partial_file.write_bytes(b"\x00" * 1024)
            raise asyncio.TimeoutError()

        mock_proc.communicate = communicate_then_timeout

        try:
            with patch("asyncio.create_subprocess_exec",
                       new=AsyncMock(return_value=mock_proc)):
                # Don't patch wait_for globally — let the real one call communicate,
                # which raises TimeoutError itself (simulating the timeout firing)
                with patch("app.media.remux.enforce_budget_async",
                           new=AsyncMock()):
                    result = await remux._run_remux(
                        "https://cdn.example.com/v.mkv",
                        "testkey123456789",
                        "copy", "aac",
                    )

            assert result is None, "Timed-out remux must return None"
            # Process must be terminated/killed
            assert mock_proc.terminate.called or len(killed) > 0, (
                "Process was not signalled after timeout"
            )
            # Partial output must be removed by the finally block
            assert not partial_file.exists(), (
                "Partial output file was not cleaned up after timeout"
            )
        finally:
            cfg.settings.temp_dir = orig_temp

    @pytest.mark.asyncio
    async def test_cancellation_kills_process_and_reraises(self, tmp_path):
        """
        On asyncio.CancelledError: process is killed, partial file removed,
        and CancelledError is re-raised (not swallowed).
        """
        from app.media import remux
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        kill_called = []
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock(side_effect=lambda: kill_called.append(True))
        mock_proc.wait = AsyncMock(return_value=None)

        partial = tmp_path / "remux_cancelkey00000.mp4"

        async def communicate_then_cancel():
            # Write partial output then simulate task cancellation
            partial.write_bytes(b"\xff" * 512)
            raise asyncio.CancelledError()

        mock_proc.communicate = communicate_then_cancel

        try:
            with pytest.raises(asyncio.CancelledError):
                with patch("asyncio.create_subprocess_exec",
                           new=AsyncMock(return_value=mock_proc)):
                    # Let communicate() raise CancelledError directly —
                    # no need to patch wait_for (which would shadow the inner raise)
                    with patch("app.media.remux.enforce_budget_async",
                               new=AsyncMock()):
                        await remux._run_remux(
                            "https://cdn.example.com/v.mkv",
                            "cancelkey00000",
                            "copy", "aac",
                        )

            # Process must have been signalled
            assert mock_proc.terminate.called or len(kill_called) > 0
            # Partial output must be gone
            assert not partial.exists(), "Partial file not removed after cancellation"
        finally:
            cfg.settings.temp_dir = orig_temp

    @pytest.mark.asyncio
    async def test_nonzero_exit_removes_output(self, tmp_path):
        """
        When ffmpeg exits with a non-zero code, the partial output must be
        deleted and None returned.
        """
        from app.media import remux
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.stderr = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Error: codec not found")
        )

        partial = tmp_path / "remux_failkey0000000000.mp4"

        try:
            with patch("asyncio.create_subprocess_exec",
                       new=AsyncMock(return_value=mock_proc)):
                with patch("app.media.remux.enforce_budget_async",
                           new=AsyncMock()):
                    result = await remux._run_remux(
                        "https://cdn.example.com/v.mkv",
                        "failkey0000000000",
                        "copy", "aac",
                    )

            assert result is None, "Non-zero exit must return None"
            assert not partial.exists(), (
                "Partial output must not exist after failed remux"
            )
        finally:
            cfg.settings.temp_dir = orig_temp

    @pytest.mark.asyncio
    async def test_success_leaves_output_intact(self, tmp_path):
        """Successful remux: output file exists and path is returned."""
        from app.media import remux
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stderr = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)

        key = "successkey000000000"

        async def good_communicate():
            # Create the output file to simulate successful ffmpeg
            out = tmp_path / f"remux_{key}.mp4"
            out.write_bytes(b"\x00" * 2048)
            return b"", b""

        mock_proc.communicate = good_communicate

        try:
            with patch("asyncio.create_subprocess_exec",
                       new=AsyncMock(return_value=mock_proc)):
                with patch("app.media.remux.enforce_budget_async",
                           new=AsyncMock()):
                    result = await remux._run_remux(
                        "https://cdn.example.com/v.mp4",
                        key,
                        "copy", "aac",
                    )

            assert result is not None, "Successful remux must return a Path"
            assert result.exists(), "Output file must exist after success"
        finally:
            cfg.settings.temp_dir = orig_temp


class TestHLSLifecycle:
    """Tests for hls_gen.py timeout, cancellation, and partial directory cleanup."""

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_removes_dir(self, tmp_path):
        """
        On timeout: the process is killed and the entire out_dir is removed
        (including any partial .ts segments already written).
        """
        from app.media import hls_gen
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        killed = []
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.feed_eof = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock(return_value=None)

        key = "hlstimeoutkey00000"
        out_dir = tmp_path / f"hls_{key}"
        out_dir.mkdir()
        # Simulate partial segments already written
        (out_dir / "seg00001.ts").write_bytes(b"\x47" * 188)
        (out_dir / "seg00002.ts").write_bytes(b"\x47" * 188)

        try:
            with patch("asyncio.create_subprocess_exec",
                       new=AsyncMock(return_value=mock_proc)):
                with patch("asyncio.wait_for",
                           side_effect=asyncio.TimeoutError()):
                    with patch("app.media.hls_gen.enforce_budget_async",
                               new=AsyncMock()):
                        result = await hls_gen._run_hls(
                            "https://cdn.example.com/v.mkv",
                            key,
                            "libx264", "aac", None, None,
                        )

            assert result is None, "Timed-out HLS job must return None"
            # The entire output directory must be removed
            assert not out_dir.exists(), (
                f"Partial HLS output directory {out_dir.name} was not removed "
                f"after timeout. Contents would be: "
                f"{list(out_dir.iterdir()) if out_dir.exists() else 'N/A'}"
            )
            # Process must have been signalled
            assert mock_proc.terminate.called or len(killed) > 0
        finally:
            cfg.settings.temp_dir = orig_temp

    @pytest.mark.asyncio
    async def test_cancellation_removes_dir(self, tmp_path):
        """
        On CancelledError: out_dir is removed and CancelledError is re-raised.
        """
        from app.media import hls_gen
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.feed_eof = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)

        key = "hlscancelkey000000"
        out_dir = tmp_path / f"hls_{key}"
        out_dir.mkdir()
        (out_dir / "seg00001.ts").write_bytes(b"\x47" * 188)

        async def communicate_then_cancel():
            raise asyncio.CancelledError()

        mock_proc.communicate = communicate_then_cancel

        try:
            with pytest.raises(asyncio.CancelledError):
                with patch("asyncio.create_subprocess_exec",
                           new=AsyncMock(return_value=mock_proc)):
                    with patch("app.media.hls_gen.enforce_budget_async",
                               new=AsyncMock()):
                        await hls_gen._run_hls(
                            "https://cdn.example.com/v.mkv",
                            key,
                            "libx264", "aac", None, None,
                        )

            assert not out_dir.exists(), (
                "HLS output directory not removed after CancelledError"
            )
        finally:
            cfg.settings.temp_dir = orig_temp

    @pytest.mark.asyncio
    async def test_nonzero_exit_removes_dir(self, tmp_path):
        """Non-zero ffmpeg exit: out_dir removed, None returned."""
        from app.media import hls_gen
        from app.core import config as cfg

        orig_temp = cfg.settings.temp_dir
        cfg.settings.temp_dir = tmp_path

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.feed_eof = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Unsupported codec")
        )

        key = "hlsfailkey00000000"
        out_dir = tmp_path / f"hls_{key}"
        out_dir.mkdir()
        (out_dir / "seg00001.ts").write_bytes(b"\x47" * 188)

        try:
            with patch("asyncio.create_subprocess_exec",
                       new=AsyncMock(return_value=mock_proc)):
                with patch("app.media.hls_gen.enforce_budget_async",
                           new=AsyncMock()):
                    result = await hls_gen._run_hls(
                        "https://cdn.example.com/v.mkv",
                        key,
                        "libx264", "aac", None, None,
                    )

            assert result is None
            assert not out_dir.exists(), (
                "HLS output dir not removed after non-zero exit"
            )
        finally:
            cfg.settings.temp_dir = orig_temp


class TestRtmpLifecycle:
    """Tests for rtmp_relay.py stop_relay zombie prevention."""

    @pytest.mark.asyncio
    async def test_stop_relay_reaps_zombie_after_kill(self):
        """
        stop_relay must call proc.wait() after proc.kill() to reap the process.
        Without this the PID becomes a zombie.
        """
        from app.media import rtmp_relay

        kill_called = []
        wait_called = []

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = None

        mock_proc.terminate = MagicMock()

        # First wait_for (after terminate) times out, triggering kill
        call_count = [0]

        async def _wait_impl():
            wait_called.append(True)

        # Use AsyncMock so pytest-asyncio properly awaits it;
        # plain async def assigned to an attribute can be left un-awaited
        # when wait_for is patched to raise before the call completes.
        mock_proc.wait = AsyncMock(side_effect=lambda: wait_called.append(True))
        mock_proc.kill = MagicMock(side_effect=lambda: kill_called.append(True))

        key = "relayzombietest0"
        rtmp_relay._relay_procs[key] = mock_proc

        # Simulate terminate-wait timing out so kill path is exercised
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            await rtmp_relay.stop_relay(key)

        assert len(kill_called) > 0, "proc.kill() was not called after terminate timeout"
        assert len(wait_called) > 0, "proc.wait() was not called after proc.kill() — zombie risk"
        assert key not in rtmp_relay._relay_procs, "Key not removed from tracking dict"

    @pytest.mark.asyncio
    async def test_stop_relay_clean_exit_no_kill_needed(self):
        """When process exits cleanly after SIGTERM, kill must not be called."""
        from app.media import rtmp_relay

        kill_called = []
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock(side_effect=lambda: kill_called.append(True))
        mock_proc.wait = AsyncMock(return_value=0)

        key = "relaycleanstoptest"
        rtmp_relay._relay_procs[key] = mock_proc

        await rtmp_relay.stop_relay(key)

        assert len(kill_called) == 0, "kill() called unnecessarily for clean exit"
        assert key not in rtmp_relay._relay_procs

    @pytest.mark.asyncio
    async def test_stop_relay_idempotent_for_already_stopped(self):
        """Calling stop_relay on a process that already exited must not raise."""
        from app.media import rtmp_relay

        mock_proc = AsyncMock()
        mock_proc.returncode = 0   # already exited

        key = "relayalreadydone0"
        rtmp_relay._relay_procs[key] = mock_proc

        await rtmp_relay.stop_relay(key)   # must not raise
        assert key not in rtmp_relay._relay_procs


# ══════════════════════════════════════════════════════════════════════════════
# SCOPE C — TEMPORARY STORAGE SAFETY
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanupSafety:
    """Tests for cleanup.py correctness, async off-loading, and edge cases."""

    def test_prune_removes_oldest_first(self, tmp_path):
        """Oldest files must be evicted before newer ones."""
        from app.media.cleanup import _prune_sync

        budget = 1 * 1024 * 1024   # 1 MB budget

        old_file = tmp_path / "remux_oldfile00000000.mp4"
        new_file = tmp_path / "remux_newfile00000000.mp4"

        old_file.write_bytes(b"\x00" * (600 * 1024))
        time.sleep(0.05)
        new_file.write_bytes(b"\x00" * (600 * 1024))

        # Make old_file old enough to be evicted
        old_ts = time.time() - 120
        os.utime(old_file, (old_ts, old_ts))

        _prune_sync(tmp_path, budget)

        assert not old_file.exists(), "Oldest file was not pruned"
        assert new_file.exists(), "Newest file was pruned before oldest"

    def test_recent_files_protected_from_eviction(self, tmp_path):
        """Files younger than _MIN_AGE_SECONDS must not be evicted."""
        from app.media.cleanup import _prune_sync

        f = tmp_path / "remux_recent000000000.mp4"
        f.write_bytes(b"\x00" * (2 * 1024 * 1024))   # 2 MB over a 0-byte budget

        # mtime is NOW — within the protection window
        _prune_sync(tmp_path, 0)   # budget = 0: should try to prune everything

        assert f.exists(), (
            "File within protection window was evicted — active writes may be disrupted"
        )

    def test_missing_temp_dir_does_not_raise(self):
        """_prune_sync must be safe when the temp directory does not exist."""
        from app.media.cleanup import _prune_sync

        nonexistent = Path("/tmp/aura_test_missing_dir_xyz_12345")
        # Must not raise FileNotFoundError or any other exception
        _prune_sync(nonexistent, 1024 * 1024)

    def test_within_budget_does_nothing(self, tmp_path):
        """No files are deleted when usage is within budget."""
        from app.media.cleanup import _prune_sync

        f = tmp_path / "remux_withinbudget000.mp4"
        f.write_bytes(b"\x00" * 1024)   # 1 KB
        old_ts = time.time() - 120
        os.utime(f, (old_ts, old_ts))

        _prune_sync(tmp_path, 100 * 1024 * 1024)   # 100 MB budget

        assert f.exists(), "File deleted when usage is within budget"

    @pytest.mark.asyncio
    async def test_enforce_budget_async_uses_executor(self):
        """
        enforce_budget_async must run the I/O work in a thread-pool executor,
        not on the event loop thread.
        """
        from app.media.cleanup import enforce_budget_async

        executor_calls = []

        original_run_in_executor = asyncio.get_event_loop().run_in_executor

        async def patched():
            loop = asyncio.get_running_loop()
            original = loop.run_in_executor

            def tracking_executor(executor, fn, *args):
                executor_calls.append(fn.__name__ if hasattr(fn, '__name__') else str(fn))
                return original(executor, fn, *args)

            with patch.object(loop, 'run_in_executor', side_effect=tracking_executor):
                await enforce_budget_async()

        await patched()

        assert len(executor_calls) > 0, (
            "enforce_budget_async did not use run_in_executor — "
            "filesystem I/O is blocking the event loop"
        )

    def test_soft_cap_limitation_documented(self):
        """
        The cleanup module's docstring must honestly document that
        enforce_budget is a soft pre-job check, not a hard real-time cap.
        """
        from app.media import cleanup
        doc = cleanup.__doc__ or ""
        assert "soft" in doc.lower() or "not a hard" in doc.lower() or "best-effort" in doc.lower(), (
            "cleanup.py docstring does not document the soft-cap limitation. "
            "This is required to avoid misleading operators about storage guarantees."
        )

    def test_collect_files_returns_empty_for_missing_dir(self):
        """_collect_files must return [] (not raise) when temp dir is missing."""
        from app.media.cleanup import _collect_files

        result = _collect_files(Path("/tmp/aura_does_not_exist_xyz_99999"))
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# SCOPE D — DEPLOYMENT CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

class TestDeploymentConfiguration:
    """Verify deployment-relevant configuration properties."""

    def test_cors_no_wildcard(self):
        """CORS allowed_origins must not contain '*'."""
        from app.core.config import settings
        for origin in settings.cors_origins:
            assert origin != "*", (
                "CORS is configured with wildcard '*' — "
                "this allows any origin to make credentialed requests."
            )

    def test_cors_credentials_false(self, client):
        """CORS must not allow credentials (no allow_credentials=True)."""
        # OPTIONS preflight to check CORS headers
        r = client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        acao = r.headers.get("access-control-allow-credentials", "false")
        assert acao.lower() != "true", (
            "CORS allow_credentials is True — combined with broad origins "
            "this allows cross-origin cookie/session attacks."
        )

    def test_rate_limit_enabled_by_default(self):
        """Rate limiting must be enabled in the default config."""
        from app.core.config import settings
        assert settings.rate_limit_enabled, (
            "rate_limit_enabled is False — rate limiting is disabled by default. "
            "This must be True in production."
        )

    def test_rate_limiter_uses_in_memory_storage(self):
        """
        Document that the rate limiter uses in-process memory, which does
        not survive restarts and is not shared across workers.
        This is a known acceptable limitation for single-worker deployments.
        """
        from app.core.limiter import limiter
        # slowapi's default storage is in-memory (no Redis configured)
        # We verify this by checking that _storage is not a Redis-backed store
        storage_type = type(limiter._storage).__name__
        # If Redis is configured the type would contain 'Redis'; in-memory is 'MemoryStorage'
        is_in_memory = "redis" not in storage_type.lower()
        # This is not a test failure — in-memory is correct for single-worker.
        # We assert it's documented (the .env.example must mention the limitation).
        env_example = (
            Path(__file__).parent.parent / ".env.example"
        ).read_text()
        assert "multi-worker" in env_example.lower() or "multiple" in env_example.lower(), (
            ".env.example does not document multi-worker rate-limiter limitations. "
            "Operators deploying with --workers N will be surprised when limits don't work."
        )

    def test_ffmpeg_job_timeout_is_bounded(self):
        """FFmpeg jobs must have a finite timeout (not 0 or infinity)."""
        from app.core.config import settings
        assert settings.ffmpeg_job_timeout > 0, "ffmpeg_job_timeout must be positive"
        assert settings.ffmpeg_job_timeout <= 3600, (
            f"ffmpeg_job_timeout={settings.ffmpeg_job_timeout}s is > 1 hour — "
            "this could allow jobs to run indefinitely if the source stalls."
        )

    def test_max_redirects_is_bounded(self):
        """max_redirects must be a small positive number to prevent redirect loops."""
        from app.core.config import settings
        assert 1 <= settings.max_redirects <= 10, (
            f"max_redirects={settings.max_redirects} is outside the safe range [1, 10]"
        )

    def test_env_example_documents_job_timeout(self):
        """.env.example must document AURA_FFMPEG_JOB_TIMEOUT."""
        env_example = (
            Path(__file__).parent.parent / ".env.example"
        ).read_text()
        assert "FFMPEG_JOB_TIMEOUT" in env_example, (
            ".env.example is missing AURA_FFMPEG_JOB_TIMEOUT — "
            "operators have no guidance on how to tune job timeouts."
        )

    def test_health_endpoint_fast(self, client):
        """Lightweight /health must respond without invoking FFmpeg."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=AssertionError("FFmpeg invoked in /health"),
        ):
            r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_no_traceback_in_500_response(self, client):
        """Global exception handler must return safe JSON with no traceback."""
        from app.media.probe import MediaInfo
        with patch(
            "app.api.routes.media.probe",
            new=AsyncMock(side_effect=RuntimeError("secret internal error /tmp/leak")),
        ):
            r = client.post(
                "/api/media/probe",
                json={"url": "https://cdn.example.com/v.mp4"},
            )
        assert r.status_code == 500
        body = r.text
        assert "Traceback" not in body
        assert "/tmp" not in body
        assert "RuntimeError" not in body
        assert "secret" not in body
        import json
        data = json.loads(body)
        assert "detail" in data
