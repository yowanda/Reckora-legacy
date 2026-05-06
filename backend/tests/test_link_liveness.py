"""Unit tests for Phase 5h Layer D — post-claim link-liveness probe."""

from __future__ import annotations

import httpx
import pytest

from app.checker import CheckResult, Status, _link_liveness_probe
from app.sites import Site

from ._phase_5h import _force_admin_int

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENABLE_LIVENESS_KNOBS: dict[str, int] = {
    "enable_link_liveness": 1,
    "min_claimed_confidence": 0,
    "min_claimed_ml_confidence": 0,
    "require_verify_for_claim": 0,
}


def _site(**overrides: object) -> Site:
    base: dict[str, object] = {
        "name": "Example",
        "url": "https://example.com/{username}",
        "url_main": "https://example.com/",
        "check_type": "status_code",
    }
    base.update(overrides)
    return Site(**base)  # type: ignore[arg-type]


def _claimed(url: str = "https://example.com/alice") -> CheckResult:
    return CheckResult(
        site="Example",
        status=Status.CLAIMED,
        url=url,
        url_main="https://example.com/",
        tags=[],
        http_status=200,
        confidence=80,
        signals=[],
    )


# ---------------------------------------------------------------------------
# Layer D test cases
# ---------------------------------------------------------------------------


async def test_5h_d_404_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.UNKNOWN
    assert "link-dead-after-claim:404" in result.signals


async def test_5h_d_410_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = httpx.MockTransport(lambda req: httpx.Response(410))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.UNKNOWN
    assert "link-dead-after-claim:410" in result.signals


def _redirect_transport(target: str) -> httpx.MockTransport:
    """Return a transport that 302-redirects to *target*, then serves 200."""

    def _handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).rstrip("/") == target.rstrip("/"):
            return httpx.Response(200, text="landed")
        return httpx.Response(302, headers={"Location": target})

    return httpx.MockTransport(_handler)


async def test_5h_d_login_redirect_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = _redirect_transport("https://example.com/login")
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.UNKNOWN
    assert any(s.startswith("link-redirects-login:") for s in result.signals), result.signals


async def test_5h_d_homepage_redirect_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = _redirect_transport("https://example.com/")
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.UNKNOWN
    assert "link-redirects-home:root" in result.signals


async def test_5h_d_softness_query_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = _redirect_transport("https://example.com/?missing=true")
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.UNKNOWN
    assert "link-redirects-home:soft-404-flag" in result.signals


async def test_5h_d_network_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)

    def _raise(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    transport = httpx.MockTransport(_raise)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.CLAIMED
    assert any(s.startswith("link-liveness-error:") for s in result.signals), result.signals


async def test_5h_d_blocked_response_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = httpx.MockTransport(lambda req: httpx.Response(429))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.CLAIMED
    assert any(s.startswith("link-liveness-blocked:") for s in result.signals), result.signals


async def test_5h_d_link_verified_live_when_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>profile page</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.CLAIMED
    assert "link-verified-live" in result.signals


async def test_5h_d_disabled_knob_skips_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_admin_int(monkeypatch, {"enable_link_liveness": 0})
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(client, _claimed(), _site())
    assert result.status is Status.CLAIMED
    assert not any(s.startswith("link-") for s in result.signals)


# ---------------------------------------------------------------------------
# Layer E — dual-URL check (url_probe != url)
# ---------------------------------------------------------------------------


async def test_5h_e_dual_url_demotes_when_display_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe URL passes liveness but display URL returns 404 → demote."""
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    probe_url = "https://api.example.com/users/{username}"
    display_url = "https://example.com/{username}"
    site = _site(url=display_url, url_probe=probe_url)

    def _handler(req: httpx.Request) -> httpx.Response:
        if "api.example.com" in str(req.url):
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    # primary.url = probe URL (simulates Phase 6b where the checker uses
    # url_probe for the initial detection; primary.url differs from the
    # display URL derived from site.url).
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(
            client,
            _claimed("https://api.example.com/users/alice"),
            site,
            username="alice",
        )
    assert result.status is Status.UNKNOWN
    assert any("display-url-dead" in s for s in result.signals), result.signals


async def test_5h_e_dual_url_passes_when_both_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both probe URL and display URL return 200 → stays CLAIMED."""
    _force_admin_int(monkeypatch, _ENABLE_LIVENESS_KNOBS)
    probe_url = "https://api.example.com/users/{username}"
    display_url = "https://example.com/{username}"
    site = _site(url=display_url, url_probe=probe_url)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="profile"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _link_liveness_probe(
            client,
            _claimed("https://api.example.com/users/alice"),
            site,
            username="alice",
        )
    assert result.status is Status.CLAIMED
    assert "link-verified-live" in result.signals
    assert not any("display-url-dead" in s for s in result.signals)
