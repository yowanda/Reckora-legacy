"""Unit tests for the per-site checker."""
from __future__ import annotations

import httpx
import pytest

from app.checker import Status, check_site, is_valid_username
from app.sites import Site


def _site(**overrides) -> Site:
    base = {
        "name": "Example",
        "url": "https://example.com/{username}",
        "url_main": "https://example.com/",
        "check_type": "status_code",
    }
    base.update(overrides)
    return Site(**base)


@pytest.mark.parametrize(
    "username,expected",
    [
        ("alice", True),
        ("alice_42", True),
        ("a.b-c", True),
        ("", False),
        ("a" * 65, False),
        ("bad name", False),
        ("../etc/passwd", False),
    ],
)
def test_is_valid_username(username: str, expected: bool) -> None:
    assert is_valid_username(username) is expected


async def test_status_code_claimed() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.CLAIMED
    assert result.http_status == 200


async def test_status_code_free_on_404() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.FREE


async def test_message_check_uses_presense_strs() -> None:
    site = _site(check_type="message", presense_strs=("user-profile",))
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>user-profile here</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice")
    assert result.status is Status.CLAIMED


async def test_message_check_uses_absence_strs() -> None:
    site = _site(check_type="message", absence_strs=("not found",))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="page says: not found"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice")
    assert result.status is Status.FREE


async def test_skipped_when_regex_fails() -> None:
    site = _site(regex_check=r"^[a-z]+$")
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "Alice123")
    assert result.status is Status.SKIPPED


async def test_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectTimeout("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.ERROR
    assert result.error == "ConnectTimeout"


# ---------- Phase 1 regression coverage ----------


async def test_confidence_reflects_signal_strength() -> None:
    """Two-signal classifies score higher than single-signal."""
    site_two_signal = _site(
        check_type="message",
        presense_strs=("user-profile",),
        absence_strs=("not found",),
    )
    site_one_signal = _site(check_type="message", presense_strs=("user-profile",))
    body = "<html>user-profile here</html>"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        r2 = await check_site(client, site_two_signal, "alice")
        r1 = await check_site(client, site_one_signal, "alice")
    assert r2.status is Status.CLAIMED
    assert r1.status is Status.CLAIMED
    # Both classify the same body as CLAIMED, but two-signal should score higher.
    assert r2.confidence >= r1.confidence
    assert r2.confidence >= 80
    assert "presence-match" in r2.signals


async def test_blocked_status_for_cloudflare_body() -> None:
    """A Cloudflare interstitial must NOT be classified as CLAIMED."""
    body = (
        "<html><head><title>Just a moment...</title></head>"
        '<body><div class="cf-browser-verification">checking your browser</div></body></html>'
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.BLOCKED
    assert "body-fingerprint-block" in result.signals
    # status_code-based classifier would have said CLAIMED on 200 — Phase 1
    # short-circuits BEFORE that classifier runs.
    assert result.http_status == 200


async def test_blocked_status_for_429_and_451() -> None:
    """Rate limit + legal block both surface as BLOCKED."""
    transport_429 = httpx.MockTransport(lambda req: httpx.Response(429, text=""))
    transport_451 = httpx.MockTransport(lambda req: httpx.Response(451, text=""))
    async with httpx.AsyncClient(transport=transport_429) as client:
        r1 = await check_site(client, _site(), "alice")
    async with httpx.AsyncClient(transport=transport_451) as client:
        r2 = await check_site(client, _site(), "alice")
    assert r1.status is Status.BLOCKED
    assert r2.status is Status.BLOCKED
    assert "status-429" in r1.signals
    assert "status-451" in r2.signals


async def test_head_only_uses_head_method() -> None:
    """When the site opts in to HEAD, the request method must be HEAD."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.method)
        return httpx.Response(200, text="")

    site = _site(head_only=True)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # verify=False keeps the test focussed on the primary probe.
        # Phase 2's verify pass would otherwise add a second HEAD.
        result = await check_site(client, site, "alice", verify=False)
    assert seen == ["HEAD"]
    assert result.status is Status.CLAIMED


async def test_smart_retry_recovers_from_503() -> None:
    """A 503 should be retried; a subsequent 200 should classify as CLAIMED."""
    state: dict[str, int] = {"attempts": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["attempts"] += 1
        if state["attempts"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)
    assert state["attempts"] == 2
    assert result.attempts == 2
    assert result.status is Status.CLAIMED


async def test_ignore_403_keeps_claim_open() -> None:
    """Sites that mark ignore_403 must NOT classify a 403 as FREE."""
    transport = httpx.MockTransport(lambda req: httpx.Response(403, text=""))
    site = _site(ignore_403=True)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice")
    # Phase 1 escalates ignore_403 to BLOCKED so the SPA can show the user
    # this slot needs manual confirmation.
    assert result.status is Status.BLOCKED
    assert "status-403-ignored" in result.signals


# ---------------------------------------------------------------------------
# Phase 2 regression tests
# ---------------------------------------------------------------------------


async def test_prewarm_sends_url_main_first() -> None:
    """Sites flagged ``prewarm`` must hit ``url_main`` before the probe."""
    import asyncio
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cache: set[str] = set()
    lock = asyncio.Lock()
    site = _site(prewarm=True)
    async with httpx.AsyncClient(transport=transport) as client:
        await check_site(
            client,
            site,
            "alice",
            prewarm_cache=cache,
            prewarm_lock=lock,
            verify=False,
        )
    assert seen[0] == "https://example.com/"
    assert seen[1] == "https://example.com/alice"


async def test_prewarm_runs_only_once_per_host() -> None:
    """Repeated checks against the same host must NOT re-prewarm."""
    import asyncio
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cache: set[str] = set()
    lock = asyncio.Lock()
    site = _site(prewarm=True)
    async with httpx.AsyncClient(transport=transport) as client:
        await check_site(client, site, "alice", prewarm_cache=cache, prewarm_lock=lock, verify=False)
        await check_site(client, site, "bob", prewarm_cache=cache, prewarm_lock=lock, verify=False)
    # Only ONE prewarm hit (first call), then 2 probe hits
    prewarm_hits = [u for u in seen if u == "https://example.com/"]
    assert len(prewarm_hits) == 1
    assert "https://example.com/alice" in seen
    assert "https://example.com/bob" in seen


async def test_verify_pass_promotes_agreement() -> None:
    """When primary CLAIMED and verify also CLAIMED, ``verified=True``."""
    import asyncio
    seen_uas: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_uas.append(req.headers.get("user-agent", ""))
        return httpx.Response(200, text="welcome")

    transport = httpx.MockTransport(handler)
    site = _site()  # status_code → triggers verify
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(
            client,
            site,
            "alice",
            prewarm_cache=set(),
            prewarm_lock=asyncio.Lock(),
            verify=True,
        )
    # Two probes — primary + verify — with DIFFERENT UAs.
    assert len(seen_uas) == 2
    assert seen_uas[0] != seen_uas[1]
    assert "Firefox" in seen_uas[1]
    assert result.status is Status.CLAIMED
    assert result.verified is True
    assert "verify-agree" in result.signals
    assert result.confidence > 80  # bumped


async def test_verify_pass_demotes_disagreement() -> None:
    """When primary CLAIMED but verify FREE, status falls to UNKNOWN."""
    import asyncio
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, text="welcome")
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    site = _site()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(
            client,
            site,
            "alice",
            prewarm_cache=set(),
            prewarm_lock=asyncio.Lock(),
            verify=True,
        )
    assert result.status is Status.UNKNOWN
    assert result.verified is False
    assert any(s.startswith("verify-disagree") for s in result.signals)
