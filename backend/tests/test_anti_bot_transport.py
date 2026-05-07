"""Phase 9a tests: anti-bot transport (curl-cffi impersonation).

Covers:

* JS-challenge HTML detection regex bank.
* Per-site override resolution (default profile + override + fallback
  on a typo).
* ``AntiBotTransport`` request flow: header sanitisation,
  request -> curl-cffi session dispatch, response rebuild, error
  translation, session caching by impersonation profile.
* ``build_anti_bot_client`` factory composes ``RotatingProxyTransport``
  *outside* ``AntiBotTransport`` so the proxy hop stays on vanilla httpx
  and the direct-egress hop runs the impersonation.
* ``/api/health`` payload exposes ``anti_bot.{enabled,
  default_impersonation, playwright_fallback,
  playwright_chrome_installed}``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import anti_bot_transport, proxy_pool, transport
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hold the singleton + config knobs at known values per test."""
    monkeypatch.setattr(settings, "enable_anti_bot_pipeline", 0, raising=False)
    monkeypatch.setattr(settings, "anti_bot_default_impersonation", "chrome120", raising=False)
    monkeypatch.setattr(settings, "anti_bot_playwright_fallback", 0, raising=False)
    monkeypatch.setattr(settings, "anti_bot_playwright_max_per_scan", 5, raising=False)
    monkeypatch.setattr(settings, "anti_bot_playwright_timeout_sec", 10, raising=False)
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    proxy_pool.reset_pool_for_tests()
    yield
    proxy_pool.reset_pool_for_tests()


# ---------------------------------------------------------------------------
# Knob plumbing
# ---------------------------------------------------------------------------


def test_is_enabled_reflects_admin_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    assert anti_bot_transport.is_enabled() is False
    monkeypatch.setattr(settings, "enable_anti_bot_pipeline", 1, raising=False)
    assert anti_bot_transport.is_enabled() is True


def test_default_impersonation_falls_back_on_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anti_bot_default_impersonation", "edge_lol", raising=False)
    assert anti_bot_transport.default_impersonation() == "chrome120"


def test_default_impersonation_accepts_supported_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anti_bot_default_impersonation", "safari17", raising=False)
    assert anti_bot_transport.default_impersonation() == "safari17"


def test_status_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = anti_bot_transport.status()
    assert set(payload) == {
        "enabled",
        "default_impersonation",
        "playwright_fallback",
        "playwright_chrome_installed",
    }
    assert payload["enabled"] is False
    assert payload["default_impersonation"] == "chrome120"
    # playwright_fallback should be 0 (knob value, not bool) — keep
    # the contract loose so the SPA can render either shape.
    assert payload["playwright_fallback"] in (0, 1, False, True)
    # playwright_chrome_installed is a bool sourced from a probe.
    assert isinstance(payload["playwright_chrome_installed"], bool)


# ---------------------------------------------------------------------------
# JS-challenge detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"<html><head><title>Just a moment...</title></head>",
        b"<title>Attention Required! | Cloudflare</title>",
        b'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
        b"<title>Pardon Our Interruption</title>",
        b"<script>var _abck=12345</script>",
        b'<meta name="dd-track-id" content="abc">',
        b'<a href="https://datadome.co/captcha/challenge">solve</a>',
        b"PerimeterX, Inc. is here to help",
    ],
)
def test_looks_like_js_challenge_detects_known_interstitials(body: bytes) -> None:
    assert anti_bot_transport.looks_like_js_challenge(body) is True


def test_looks_like_js_challenge_str_input() -> None:
    body = "<title>Just a moment...</title>"
    assert anti_bot_transport.looks_like_js_challenge(body) is True


def test_looks_like_js_challenge_negatives() -> None:
    assert anti_bot_transport.looks_like_js_challenge(None) is False
    assert anti_bot_transport.looks_like_js_challenge(b"") is False
    assert anti_bot_transport.looks_like_js_challenge(b"<html><body>hi</body></html>") is False


def test_looks_like_js_challenge_only_inspects_first_64kib() -> None:
    # 80 KiB of innocent prefix followed by a Cloudflare title — should
    # NOT match because the matcher is bounded to 64 KiB.
    body = b"a" * (80 * 1024) + b"<title>Just a moment...</title>"
    assert anti_bot_transport.looks_like_js_challenge(body) is False


# ---------------------------------------------------------------------------
# Per-site override resolution
# ---------------------------------------------------------------------------


def test_resolve_impersonation_returns_default_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import site_overrides as site_overrides_mod

    monkeypatch.setattr(site_overrides_mod, "get_override", lambda _name: None)
    assert anti_bot_transport.resolve_impersonation("UnknownSite") == "chrome120"


def test_resolve_impersonation_uses_site_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import site_overrides as site_overrides_mod

    fake = site_overrides_mod.SiteOverride(
        name="WAFedSite",
        operation="upsert",
        patch={"antiBotProfile": "safari17"},
        created_by="root",
        created_at=0,
        updated_at=0,
    )
    monkeypatch.setattr(site_overrides_mod, "get_override", lambda _name: fake)
    assert anti_bot_transport.resolve_impersonation("WAFedSite") == "safari17"


def test_resolve_impersonation_rejects_unsupported_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo on the override field falls back to the default."""
    from app import site_overrides as site_overrides_mod

    fake = site_overrides_mod.SiteOverride(
        name="WAFedSite",
        operation="upsert",
        patch={"antiBotProfile": "lynx18"},
        created_by="root",
        created_at=0,
        updated_at=0,
    )
    monkeypatch.setattr(site_overrides_mod, "get_override", lambda _name: fake)
    assert anti_bot_transport.resolve_impersonation("WAFedSite") == "chrome120"


def test_resolve_impersonation_no_site_name() -> None:
    assert anti_bot_transport.resolve_impersonation(None) == "chrome120"


# ---------------------------------------------------------------------------
# AntiBotTransport request flow
# ---------------------------------------------------------------------------


class _FakeCurlResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


@pytest.mark.asyncio
async def test_handle_async_request_dispatches_via_curl_cffi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport must call curl-cffi's session.request once with
    the correct impersonation, URL, and method, and rebuild the
    httpx.Response from the curl-cffi response shape.
    """
    captured_kwargs: dict[str, Any] = {}

    fake_resp = _FakeCurlResponse(
        status_code=200,
        headers={"content-type": "text/html"},
        content=b"<html>ok</html>",
    )

    fake_session = MagicMock()
    fake_session.request = AsyncMock(return_value=fake_resp)
    fake_session.close = AsyncMock(return_value=None)

    def _capture_request(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return fake_resp

    fake_session.request.side_effect = _capture_request

    rt = anti_bot_transport.AntiBotTransport(default_profile="chrome131")
    monkeypatch.setattr(rt, "_get_session", lambda _profile: fake_session)

    request = httpx.Request(
        "GET",
        "https://example.test/path?u=alice",
        headers={"User-Agent": "Reckora", "X-Reckora-Site-Name": "ExampleNetwork"},
    )
    monkeypatch.setattr(
        anti_bot_transport,
        "resolve_impersonation",
        lambda _name: "firefox120",
    )

    response = await rt.handle_async_request(request)

    fake_session.request.assert_awaited_once()
    assert captured_kwargs["method"] == "GET"
    assert captured_kwargs["url"] == "https://example.test/path?u=alice"
    # Per-site override won; firefox120 selected via the resolver.
    assert captured_kwargs["impersonate"] == "firefox120"
    # X-Reckora-Site-Name is internal-only — never forwarded over the wire.
    assert "X-Reckora-Site-Name" not in captured_kwargs["headers"]
    assert "x-reckora-site-name" not in {k.lower() for k in captured_kwargs["headers"]}
    # User-Agent passes through untouched.
    assert captured_kwargs["headers"].get("User-Agent") == "Reckora"

    assert response.status_code == 200
    assert response.content == b"<html>ok</html>"
    assert response.headers.get("content-type") == "text/html"
    assert response.request is request


@pytest.mark.asyncio
async def test_handle_async_request_uses_default_profile_without_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, Any] = {}
    fake_resp = _FakeCurlResponse()
    fake_session = MagicMock()

    async def _request(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return fake_resp

    fake_session.request = _request
    fake_session.close = AsyncMock(return_value=None)

    rt = anti_bot_transport.AntiBotTransport(default_profile="chrome131")
    monkeypatch.setattr(rt, "_get_session", lambda _profile: fake_session)

    request = httpx.Request("GET", "https://example.test/")
    await rt.handle_async_request(request)
    assert captured_kwargs["impersonate"] == "chrome131"


@pytest.mark.asyncio
async def test_handle_async_request_strips_disallowed_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, Any] = {}
    fake_resp = _FakeCurlResponse()
    fake_session = MagicMock()

    async def _request(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return fake_resp

    fake_session.request = _request
    fake_session.close = AsyncMock(return_value=None)

    rt = anti_bot_transport.AntiBotTransport()
    monkeypatch.setattr(rt, "_get_session", lambda _profile: fake_session)

    # httpx auto-injects Host + Content-Length; our transport must
    # drop them before forwarding to curl-cffi or libcurl will trip.
    request = httpx.Request(
        "POST",
        "https://example.test/",
        headers={"Host": "example.test", "Content-Length": "0", "X-Foo": "bar"},
        content=b"",
    )
    await rt.handle_async_request(request)
    forwarded = {k.lower() for k in captured_kwargs["headers"]}
    assert "host" not in forwarded
    assert "content-length" not in forwarded
    assert "x-foo" in forwarded


@pytest.mark.asyncio
async def test_handle_async_request_translates_curl_errors() -> None:
    fake_session = MagicMock()
    fake_session.request = AsyncMock(side_effect=RuntimeError("libcurl boom"))
    fake_session.close = AsyncMock(return_value=None)

    rt = anti_bot_transport.AntiBotTransport()
    rt._sessions["chrome120"] = fake_session

    request = httpx.Request("GET", "https://example.test/")
    with pytest.raises(httpx.ConnectError) as excinfo:
        await rt.handle_async_request(request)
    assert "libcurl boom" in str(excinfo.value)


@pytest.mark.asyncio
async def test_handle_async_request_raises_when_curl_unavailable() -> None:
    rt = anti_bot_transport.AntiBotTransport()
    rt._session_unavailable = True  # simulate curl-cffi import failure

    request = httpx.Request("GET", "https://example.test/")
    with pytest.raises(httpx.ConnectError) as excinfo:
        await rt.handle_async_request(request)
    assert "curl-cffi unavailable" in str(excinfo.value)


@pytest.mark.asyncio
async def test_session_cached_per_profile() -> None:
    rt = anti_bot_transport.AntiBotTransport(default_profile="chrome120")
    sentinel_a = MagicMock()
    sentinel_a.close = AsyncMock(return_value=None)
    sentinel_b = MagicMock()
    sentinel_b.close = AsyncMock(return_value=None)
    rt._sessions["chrome120"] = sentinel_a
    rt._sessions["safari17"] = sentinel_b

    assert rt._get_session("chrome120") is sentinel_a
    assert rt._get_session("safari17") is sentinel_b
    # Cache survives multiple lookups.
    assert rt._get_session("chrome120") is sentinel_a


# ---------------------------------------------------------------------------
# Transport stacking — Phase 8b proxy rotator over Phase 9a anti-bot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_anti_bot_client_stacks_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_anti_bot_client must place RotatingProxyTransport on the
    outside (so proxy rotation gates dispatch) and AntiBotTransport on
    the inside as the direct-egress hop."""
    client = transport.build_anti_bot_client(impersonation="chrome131")
    try:
        outer = client._transport
        assert isinstance(outer, transport.RotatingProxyTransport)
        # Direct egress is the AntiBotTransport.
        direct = outer._direct_transport
        assert isinstance(direct, anti_bot_transport.AntiBotTransport)
        assert direct._default_profile == "chrome131"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_build_async_client_uses_anti_bot_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_anti_bot_pipeline", 1, raising=False)
    client = transport.build_async_client()
    try:
        outer = client._transport
        assert isinstance(outer, transport.RotatingProxyTransport)
        assert isinstance(outer._direct_transport, anti_bot_transport.AntiBotTransport)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_build_async_client_skips_anti_bot_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off contract: HF Space single-container deployment must
    NOT see the anti-bot transport unless the knob is flipped on."""
    client = transport.build_async_client()
    try:
        outer = client._transport
        assert isinstance(outer, transport.RotatingProxyTransport)
        assert outer._direct_transport is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rotating_transport_uses_anti_bot_for_direct_egress() -> None:
    """When ``direct_transport=AntiBotTransport`` is wired, a request
    that resolves to ``proxy_url=None`` must dispatch through the
    anti-bot transport, not a freshly-built httpx.AsyncHTTPTransport."""
    seen: list[str] = []

    class _Marker(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append("anti-bot")
            return httpx.Response(200, content=b"ok", request=request)

    rt = transport.RotatingProxyTransport(direct_transport=_Marker())
    async with httpx.AsyncClient(transport=rt) as client:
        resp = await client.get("https://example.test/")
        assert resp.status_code == 200
    assert seen == ["anti-bot"]


# ---------------------------------------------------------------------------
# /api/health — anti_bot descriptor
# ---------------------------------------------------------------------------


def test_health_endpoint_exposes_anti_bot_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/health`` must include the ``anti_bot`` descriptor with the
    four required fields so a dashboard can verify the impersonation
    state without firing a probe."""
    from fastapi.testclient import TestClient

    from app import admin_settings as admin_settings_mod
    from app.main import app

    monkeypatch.setattr(settings, "enable_anti_bot_pipeline", 1, raising=False)
    monkeypatch.setattr(settings, "anti_bot_default_impersonation", "safari17", raising=False)

    # ``with TestClient(app) as c`` fires the FastAPI startup hook
    # (which in turn runs ``init_db``), so the admin_settings table
    # exists by the time ``/api/health`` reads the proxy knob.
    with TestClient(app) as client:
        admin_settings_mod._invalidate_cache()
        response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert "anti_bot" in body
    descriptor = body["anti_bot"]
    assert set(descriptor) == {
        "enabled",
        "default_impersonation",
        "playwright_fallback",
        "playwright_chrome_installed",
    }
    assert descriptor["enabled"] is True
    assert descriptor["default_impersonation"] == "safari17"
    assert isinstance(descriptor["playwright_chrome_installed"], bool)
