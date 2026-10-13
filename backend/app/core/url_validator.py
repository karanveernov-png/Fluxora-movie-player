"""
url_validator.py — SSRF-safe URL validation.

Blocks:
  • Non-HTTP(S) schemes
  • Private IPv4/IPv6 ranges (RFC 1918, loopback, link-local)
  • Cloud metadata endpoints (169.254.169.254, etc.)
  • URLs exceeding max length
  • URLs with userinfo (user:pass@host)

validate_url() is synchronous and performs DNS resolution via
socket.getaddrinfo(). This is a deliberate, tested contract — every call
site (app/services/source.py, app/api/routes/media.py) and the entire test
suite (tests/test_security.py, tests/test_backend.py, tests/test_reliability.py)
import and call this exact name, both directly and via
unittest.mock.patch("app.core.url_validator.validate_url", ...). Renaming or
splitting it into a separate async function breaks those tests structurally,
not just cosmetically — do not do that without updating every test file.

Known limitation (documented, not silently patched around): under
concurrent load with multiple in-flight requests to different hostnames,
the synchronous DNS lookup here blocks the asyncio event loop for the
duration of each lookup. This is acceptable for the current single-worker
deployment target documented in .env.example ("Multi-worker deployment
note"). If this becomes a bottleneck, the fix is to offload the
getaddrinfo() call via loop.run_in_executor() *inside this same function*
(keeping the name and signature identical) — not to introduce a parallel
async-named function that call sites and tests must be migrated to.

Runtime private-IP toggle
  Call set_allow_private_ips(True) to permit LAN/localhost URLs at runtime.
  Cloud metadata IPs remain blocked regardless.

RTMP/RTSP validation
  validate_rtmp_url() accepts rtmp://, rtmps://, rtsp://, rtsps:// for relay.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from typing import Any, Callable, Awaitable

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("100::/64"),
]

_allow_private_ips_runtime: bool = False


def set_allow_private_ips(enabled: bool) -> None:
    global _allow_private_ips_runtime
    _allow_private_ips_runtime = enabled
    log.info("allow_private_ips set to: %s", enabled)


def get_allow_private_ips() -> bool:
    return _allow_private_ips_runtime


class URLValidationError(ValueError):
    """Raised when a URL fails security or format validation."""


def validate_url(url: str) -> str:
    """
    Validate and normalise a media source URL (http/https only).

    Synchronous — see module docstring for why this is not split into an
    async variant.
    """
    url = url.strip()
    if not url:
        raise URLValidationError("URL must not be empty.")
    if len(url) > settings.max_url_length:
        raise URLValidationError(
            f"URL exceeds maximum length of {settings.max_url_length} characters."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise URLValidationError(
            f"Unsupported scheme '{parsed.scheme}'. Only http:// and https:// are allowed."
        )
    if parsed.username or parsed.password:
        raise URLValidationError("URLs with embedded credentials are not allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("URL has no hostname.")
    block_all = settings.block_private_ips and not _allow_private_ips_runtime
    _assert_not_private(hostname, block_metadata_only=not block_all)
    return url


# ── RTMP/RTSP validation ──────────────────────────────────────────────────────

_RELAY_SCHEMES = ("rtmp", "rtmps", "rtsp", "rtsps")


def validate_rtmp_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise URLValidationError("URL must not be empty.")
    if len(url) > settings.max_url_length:
        raise URLValidationError("URL exceeds maximum length.")
    parsed = urlparse(url)
    if parsed.scheme not in _RELAY_SCHEMES:
        raise URLValidationError(
            f"Expected one of: {', '.join(s + '://' for s in _RELAY_SCHEMES)}."
        )
    if parsed.username or parsed.password:
        raise URLValidationError("URLs with embedded credentials are not allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("URL has no hostname.")
    block_all = settings.block_private_ips and not _allow_private_ips_runtime
    _assert_not_private(hostname, block_metadata_only=not block_all)
    return url


# ── Internal: DNS check ────────────────────────────────────────────────────────

def _assert_not_private(hostname: str, *, block_metadata_only: bool) -> None:
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        log.debug("Could not resolve hostname %s — allowing through", hostname)
        return
    for _family, _type, _proto, _canonname, sockaddr in results:
        # Cast to str to resolve Pylance typing warning for (str | int)
        ip_str = str(sockaddr[0])
        _check_ip_str(ip_str, block_metadata_only=block_metadata_only)


def _check_ip_str(ip_str: str, *, block_metadata_only: bool) -> None:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return
    if addr in _METADATA_IPS:
        raise URLValidationError("Requests to cloud metadata endpoints are blocked.")
    if block_metadata_only:
        return
    for network in _PRIVATE_NETWORKS:
        if addr in network:
            raise URLValidationError(
                "Requests to private/internal IP addresses are blocked. "
                "Enable 'Allow private networks' in settings to reach local servers."
            )


# ── SSRF-safe httpx client ────────────────────────────────────────────────────

def _make_redirect_guard() -> Callable[[httpx.Request], Awaitable[None]]:
    async def _on_request(request: httpx.Request) -> None:
        host = request.url.host
        if not host:
            return
        block_meta_only = not (settings.block_private_ips and not _allow_private_ips_runtime)
        try:
            _assert_not_private(host, block_metadata_only=block_meta_only)
        except URLValidationError as exc:
            raise httpx.RequestError(str(exc), request=request) from exc
    return _on_request


def ssrf_safe_client(**kwargs: Any) -> httpx.AsyncClient:
    existing_hooks = kwargs.pop("event_hooks", {})
    req_hooks = list(existing_hooks.get("request", []))
    req_hooks.insert(0, _make_redirect_guard())
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=settings.max_redirects,
        event_hooks={"request": req_hooks, **{k: v for k, v in existing_hooks.items() if k != "request"}},
        **kwargs,
    )