"""Phase 5e: outbound proxy gate (httpx + curl-cffi).

The OSINT operator wants two things at runtime:

  1. A *configured* proxy URL (Tor SOCKS5, an HTTP forward proxy, an
     egress gateway). This is process-static — set via
     ``RECKORA_PROXY_URL`` and read once on import.
  2. A *toggle* to flip the proxy on or off without a restart. This is
     dynamic — backed by the ``proxy_enabled`` admin knob (see
     ``app.admin_settings``) and re-read per-request through the
     short-TTL cache so any admin change takes effect within seconds.

Why split them? Because the URL itself is sensitive infrastructure
(SOCKS5 endpoint, possibly with credentials) that you don't want
sitting in a SQLite admin table where it could leak via the SPA's
``GET /api/admin/settings`` payload. The integer toggle is fine to
ship to the SPA; the URL stays env-only.

This module is intentionally tiny: a couple of pure functions over
``settings`` + ``admin_settings``, plus a status descriptor for the
``GET /api/health`` payload and the SPA's proxy badge.
"""

from __future__ import annotations

from typing import Any

from .config import settings


def _enabled_knob() -> int:
    """Read the ``proxy_enabled`` admin knob through the cache.

    Imported lazily to avoid a module-load cycle with ``auth`` (which
    ``admin_settings`` depends on) — this file is imported from
    ``main`` early during app startup.
    """
    from . import admin_settings as admin_settings_mod

    return admin_settings_mod.effective_int("proxy_enabled")


def proxy_url_or_none() -> str | None:
    """Return the active proxy URL if both URL is configured *and* the
    runtime toggle is on; otherwise ``None``.

    Callers that want to pass the result straight to httpx should use
    this — passing ``None`` to ``httpx.AsyncClient(proxy=...)`` is
    equivalent to direct egress.
    """
    url = (settings.proxy_url or "").strip()
    if not url:
        return None
    if _enabled_knob() != 1:
        return None
    return url


def status() -> dict[str, Any]:
    """Compact status descriptor for ``/api/health`` and the SPA badge.

    We deliberately do *not* return the proxy URL itself — only its
    scheme and whether it's currently routing traffic. The scheme is
    enough for the SPA to label the badge ("via tor", "via http
    proxy") without exposing the actual endpoint.
    """
    raw = (settings.proxy_url or "").strip()
    configured = bool(raw)
    enabled_knob = _enabled_knob() == 1
    active = configured and enabled_knob
    scheme = ""
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
    return {
        "configured": configured,
        "enabled": enabled_knob,
        "active": active,
        "scheme": scheme,
    }
