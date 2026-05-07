"""Phase 8b tests: ``RotatingProxyTransport`` integration.

Verifies the transport selects a proxy from the pool on every request,
calls the rate limiter, and marks the proxy as failed on transport
errors. Uses ``httpx.MockTransport`` mounted as the per-proxy backend
so the tests don't depend on a real network or proxy.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import host_rate_limit, proxy_pool, transport
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "proxy_url", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "enable_host_rate_limit", 0, raising=False)
    monkeypatch.setattr(settings, "host_rate_per_min", 60, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_pool.reset_pool_for_tests()
    host_rate_limit.reset_limiter_for_tests()
    yield
    proxy_pool.reset_pool_for_tests()
    host_rate_limit.reset_limiter_for_tests()


def _ok(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _boom(_req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated proxy down")


# ---------------------------------------------------------------------------
# proxy selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_uses_pool_proxy_when_rotator_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the rotator is on, the pool's ``acquire`` must drive the
    transport. We verify by counting selections, not by actually
    routing through the proxy (that would require a live proxy)."""
    monkeypatch.setattr(settings, "proxy_pool_urls", "http://a:1,http://b:2", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    proxy_pool.reset_pool_for_tests()

    # Replace the per-proxy backend transport with a mock so the test
    # never opens a real socket.
    cached: dict[str | None, httpx.MockTransport] = {}
    seen: list[str | None] = []

    rt = transport.RotatingProxyTransport()

    def _mock_for(proxy_url: str | None) -> httpx.MockTransport:
        seen.append(proxy_url)
        if proxy_url not in cached:
            cached[proxy_url] = httpx.MockTransport(_ok)
        return cached[proxy_url]

    monkeypatch.setattr(rt, "_transport_for", _mock_for)

    async with httpx.AsyncClient(transport=rt) as client:
        for _ in range(4):
            resp = await client.get("https://example.test/")
            assert resp.status_code == 200

    # Round-robin across the two pool URLs.
    assert seen == ["http://a:1", "http://b:2", "http://a:1", "http://b:2"]


@pytest.mark.asyncio
async def test_request_falls_back_to_phase5e_proxy_when_rotator_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_url", "socks5://tor:9050", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    # Phase 5e proxy_enabled knob defaults to ``proxy_enabled_default`` (0).
    # Force the runtime gate on so the proxy URL is actually returned.
    from app import admin_settings as admin_settings_mod

    monkeypatch.setitem(admin_settings_mod._cache, "proxy_enabled", 1)
    admin_settings_mod._cache_loaded_at = 1e18  # type: ignore[assignment]

    rt = transport.RotatingProxyTransport()
    seen: list[str | None] = []

    def _mock_for(proxy_url: str | None) -> httpx.MockTransport:
        seen.append(proxy_url)
        return httpx.MockTransport(_ok)

    monkeypatch.setattr(rt, "_transport_for", _mock_for)
    async with httpx.AsyncClient(transport=rt) as client:
        await client.get("https://example.test/")

    assert seen == ["socks5://tor:9050"]


@pytest.mark.asyncio
async def test_request_uses_direct_when_no_proxy_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt = transport.RotatingProxyTransport()
    seen: list[str | None] = []

    def _mock_for(proxy_url: str | None) -> httpx.MockTransport:
        seen.append(proxy_url)
        return httpx.MockTransport(_ok)

    monkeypatch.setattr(rt, "_transport_for", _mock_for)
    async with httpx.AsyncClient(transport=rt) as client:
        await client.get("https://example.test/")

    assert seen == [None]


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_marks_proxy_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_pool_urls", "http://a:1,http://b:2", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_pool.reset_pool_for_tests()

    rt = transport.RotatingProxyTransport()

    def _mock_for(proxy_url: str | None) -> httpx.MockTransport:
        # Only proxy ``a`` is broken.
        return httpx.MockTransport(_boom if proxy_url == "http://a:1" else _ok)

    monkeypatch.setattr(rt, "_transport_for", _mock_for)
    async with httpx.AsyncClient(transport=rt) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://example.test/")

    # Pool should have marked ``a`` as failed; next acquire skips it.
    pool = proxy_pool.get_pool()
    assert pool._state["http://a:1"].consecutive_failures == 1


# ---------------------------------------------------------------------------
# rate limiter integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_acquires_token_when_host_rate_limit_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_host_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_per_min", 60, raising=False)
    host_rate_limit.reset_limiter_for_tests()

    acquired: list[str] = []

    async def _spy_acquire(self: Any, host: str) -> None:  # noqa: ARG001
        acquired.append(host)

    monkeypatch.setattr(host_rate_limit.HostRateLimiter, "acquire", _spy_acquire)

    rt = transport.RotatingProxyTransport()

    def _mock_for(_proxy_url: str | None) -> httpx.MockTransport:
        return httpx.MockTransport(_ok)

    monkeypatch.setattr(rt, "_transport_for", _mock_for)
    async with httpx.AsyncClient(transport=rt) as client:
        await client.get("https://reddit.test/")
        await client.get("https://twitter.test/")

    assert acquired == ["reddit.test", "twitter.test"]


# ---------------------------------------------------------------------------
# build_async_client wires the right pieces
# ---------------------------------------------------------------------------


def test_build_async_client_returns_client_with_rotating_transport() -> None:
    client = transport.build_async_client()
    try:
        assert isinstance(client._transport, transport.RotatingProxyTransport)
    finally:
        # Synchronous close is safe before any request was issued.
        pass
