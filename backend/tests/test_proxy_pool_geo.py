"""Phase 9c tests: geo-aware proxy selection.

Exercises the region-tagged ``RECKORA_PROXY_POOL_URLS`` parser, the
three-pass ``ProxyPool.acquire(preferred_region=...)`` dispatch, the
:func:`app.proxy_pool.preferred_region_for_site` 30s in-process cache,
the ``RotatingProxyTransport._select_proxy`` integration through the
``X-Reckora-Site-Name`` request header, and the
``/api/health.proxy_pool.geo_distribution`` surface.

The tests are deliberately self-contained — they patch
``settings.proxy_pool_urls`` + the admin-knob effective values + the
site-overrides DB so a CI without a populated SQLite still runs them
cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import proxy_pool, transport
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop the pool singleton + the region cache around every test."""
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 0, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_pool.reset_pool_for_tests()
    yield
    proxy_pool.reset_pool_for_tests()


# ---------------------------------------------------------------------------
# Region tag parser.
# ---------------------------------------------------------------------------


def test_parse_pool_entry_bare_url_gets_default_region() -> None:
    url, region = proxy_pool._parse_pool_entry("socks5://a.example:1080")
    assert url == "socks5://a.example:1080"
    assert region == "default"


def test_parse_pool_entry_extracts_leading_region_tag() -> None:
    url, region = proxy_pool._parse_pool_entry("us-east:socks5://a.example:1080")
    assert url == "socks5://a.example:1080"
    assert region == "us-east"


def test_parse_pool_entry_treats_unknown_scheme_prefix_as_region() -> None:
    # ``ftp`` is not in our scheme allowlist — anything other than
    # http/https/socks4/socks5/socks5h is treated as a region tag so
    # an operator typo doesn't silently swallow the URL.
    url, region = proxy_pool._parse_pool_entry("ftp:socks5://b.example:1080")
    assert url == "socks5://b.example:1080"
    assert region == "ftp"


def test_parse_pool_entry_normalises_region_case() -> None:
    url, region = proxy_pool._parse_pool_entry("US-EAST:socks5://a.example:1080")
    assert url == "socks5://a.example:1080"
    assert region == "us-east"


def test_parse_pool_urls_mixes_tagged_and_bare() -> None:
    raw = "us-east:socks5://a.example:1080,ru:socks5://b.example:1080,socks5://c.example:1080"
    urls, regions = proxy_pool._parse_pool_urls(raw)
    assert urls == (
        "socks5://a.example:1080",
        "socks5://b.example:1080",
        "socks5://c.example:1080",
    )
    assert regions == {
        "socks5://a.example:1080": "us-east",
        "socks5://b.example:1080": "ru",
        "socks5://c.example:1080": "default",
    }


# ---------------------------------------------------------------------------
# Three-pass acquire.
# ---------------------------------------------------------------------------


def test_acquire_without_preferred_region_collapses_to_round_robin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,ru:socks5://b.example",
        raising=False,
    )
    proxy_pool.reset_pool_for_tests()
    pool = proxy_pool.get_pool()
    # No preferred region => Phase 8b single-pass round-robin.
    seen = [pool.acquire() for _ in range(4)]
    assert seen == [
        "socks5://a.example",
        "socks5://b.example",
        "socks5://a.example",
        "socks5://b.example",
    ]


def test_acquire_prefers_region_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,ru:socks5://b.example",
        raising=False,
    )
    proxy_pool.reset_pool_for_tests()
    pool = proxy_pool.get_pool()
    # ``ru`` is requested 4 times in a row — the region-match pass
    # picks the same single ``ru`` URL every call (round-robin within
    # the region).
    seen = [pool.acquire(preferred_region="ru") for _ in range(4)]
    assert seen == ["socks5://b.example"] * 4


def test_acquire_falls_back_to_default_then_any_when_region_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,socks5://b.example",
        raising=False,
    )
    proxy_pool.reset_pool_for_tests()
    pool = proxy_pool.get_pool()
    # Pool has only "us-east" + "default" — a request for "ru" misses
    # pass 1 (no ``ru`` proxy) and lands on the ``default``-region
    # URL via pass 2.
    assert pool.acquire(preferred_region="ru") == "socks5://b.example"


def test_acquire_falls_back_to_any_when_no_default_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,eu:socks5://b.example",
        raising=False,
    )
    proxy_pool.reset_pool_for_tests()
    pool = proxy_pool.get_pool()
    # Pool has neither "ru" nor "default" — pass 3 (any healthy URL)
    # must still surface a proxy so the probe stays fail-open.
    picked = pool.acquire(preferred_region="ru")
    assert picked in ("socks5://a.example", "socks5://b.example")


def test_acquire_skips_cooldown_url_within_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "ru:socks5://a.example,ru:socks5://b.example",
        raising=False,
    )
    proxy_pool.reset_pool_for_tests()
    pool = proxy_pool.get_pool()
    pool.mark_failed("socks5://a.example")
    # ``a.example`` is in cooldown so the region-match pass skips it
    # and lands on ``b.example``.
    seen = {pool.acquire(preferred_region="ru") for _ in range(4)}
    assert seen == {"socks5://b.example"}


# ---------------------------------------------------------------------------
# Region cache lookup.
# ---------------------------------------------------------------------------


def test_preferred_region_for_site_returns_none_for_blank() -> None:
    assert proxy_pool.preferred_region_for_site("") is None
    assert proxy_pool.preferred_region_for_site(None) is None


def test_preferred_region_for_site_pulls_from_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import site_overrides as site_overrides_mod

    fake = site_overrides_mod.SiteOverride(
        name="Yandex",
        operation="upsert",
        patch={"preferredRegion": "ru"},
        created_by="test",
        created_at=0,
        updated_at=0,
    )
    monkeypatch.setattr(site_overrides_mod, "list_overrides", lambda: [fake])
    proxy_pool.reset_pool_for_tests()
    # First call seeds the cache.
    assert proxy_pool.preferred_region_for_site("Yandex") == "ru"
    # Cached on second call — even when ``list_overrides`` would
    # raise, the cached value still wins until TTL expires.
    monkeypatch.setattr(
        site_overrides_mod,
        "list_overrides",
        lambda: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    assert proxy_pool.preferred_region_for_site("Yandex") == "ru"


def test_preferred_region_for_site_case_insensitive_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import site_overrides as site_overrides_mod

    fake = site_overrides_mod.SiteOverride(
        name="VK",
        operation="upsert",
        patch={"preferredRegion": "RU"},
        created_by="test",
        created_at=0,
        updated_at=0,
    )
    monkeypatch.setattr(site_overrides_mod, "list_overrides", lambda: [fake])
    proxy_pool.reset_pool_for_tests()
    assert proxy_pool.preferred_region_for_site("vk") == "ru"


# ---------------------------------------------------------------------------
# Status descriptor.
# ---------------------------------------------------------------------------


def test_status_reports_geo_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a,us-east:socks5://b,ru:socks5://c,socks5://d",
        raising=False,
    )
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 1, raising=False)
    proxy_pool.reset_pool_for_tests()
    desc: dict[str, Any] = proxy_pool.status()
    assert desc["geo_aware_enabled"] is True
    assert desc["geo_aware_active"] is True
    assert desc["geo_distribution"] == {"us-east": 2, "ru": 1, "default": 1}


def test_status_geo_aware_active_false_when_rotator_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "ru:socks5://c",
        raising=False,
    )
    # Rotator off => geo-aware can't be active either, even if its
    # own knob is on.
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 1, raising=False)
    proxy_pool.reset_pool_for_tests()
    desc = proxy_pool.status()
    assert desc["geo_aware_enabled"] is True
    assert desc["geo_aware_active"] is False


# ---------------------------------------------------------------------------
# RotatingProxyTransport integration.
# ---------------------------------------------------------------------------


def test_select_proxy_respects_x_reckora_site_name_when_geo_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,ru:socks5://b.example",
        raising=False,
    )
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 1, raising=False)
    proxy_pool.reset_pool_for_tests()

    monkeypatch.setattr(
        proxy_pool,
        "preferred_region_for_site",
        lambda name: "ru" if name == "VK" else None,
    )
    rt = transport.RotatingProxyTransport()
    request = httpx.Request(
        "GET",
        "https://vk.com/",
        headers={"X-Reckora-Site-Name": "VK"},
    )
    assert rt._select_proxy(request) == "socks5://b.example"


def test_select_proxy_ignores_site_header_when_geo_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "proxy_pool_urls",
        "us-east:socks5://a.example,ru:socks5://b.example",
        raising=False,
    )
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 0, raising=False)
    proxy_pool.reset_pool_for_tests()

    rt = transport.RotatingProxyTransport()
    # Round-robin: first request gets a.example regardless of the
    # site header — geo-aware knob is off.
    request = httpx.Request(
        "GET",
        "https://vk.com/",
        headers={"X-Reckora-Site-Name": "VK"},
    )
    assert rt._select_proxy(request) == "socks5://a.example"


@pytest.mark.asyncio
async def test_handle_async_request_strips_internal_site_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal X-Reckora-Site-Name header must never leak over the wire.

    ``RotatingProxyTransport`` strips it after consumption so a
    downstream plain-httpx transport doesn't forward it to the target
    site. ``AntiBotTransport`` does the same on its own egress.
    """
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 0, raising=False)
    proxy_pool.reset_pool_for_tests()

    captured: dict[str, Any] = {}

    class _CaptureTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, request=request)

    rt = transport.RotatingProxyTransport(direct_transport=_CaptureTransport())
    request = httpx.Request(
        "GET",
        "https://example.test/path",
        headers={
            "User-Agent": "Reckora",
            "X-Reckora-Site-Name": "ExampleNetwork",
        },
    )
    resp = await rt.handle_async_request(request)
    assert resp.status_code == 200
    # Header is gone from the outgoing request.
    headers_lower = {k.lower() for k in captured["headers"]}
    assert "x-reckora-site-name" not in headers_lower
    # User-Agent passes through untouched.
    assert captured["headers"].get("user-agent") == "Reckora"
