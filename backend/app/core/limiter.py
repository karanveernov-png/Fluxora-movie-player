"""
core/limiter.py — Shared slowapi rate limiter singleton.

Imported by both app.main (to attach to app.state) and app.api.routes.media
(to decorate endpoints).  Using a single module-level instance ensures the
route registrations and the app state point to the same object.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    enabled=settings.rate_limit_enabled,
    default_limits=[],
)
