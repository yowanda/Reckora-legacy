"""Unit tests for the per-site checker."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app.checker import Status, check_site, is_valid_username
from app.sites import Site


@pytest.fixture(autouse=True)
def _disable_phase_5h_floors_for_legacy_tests(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Phase 5h Layer A+B+C — disable the precision floor for legacy tests.

    The pre-5h tests below assert specific CLAIMED outcomes on synthetic
    fixtures whose confidence happens to fall below the production
    floor (default 60). Rather than rewrite each fixture's
    confidence-shaping signals, we monkey-patch the floor lookup to a
    no-op for the entire module. New 5h-specific tests opt back in by
    monkey-patching to a non-zero value inside the test body.
    """
    from app import checker as checker_mod

    real_admin_int = checker_mod._admin_int

    def _disabled_admin_int(key: str) -> int:
        if key in {
            "min_claimed_confidence",
            "min_claimed_ml_confidence",
            "require_verify_for_claim",
            "enable_link_liveness",
        }:
            return 0
        return real_admin_int(key)

    monkeypatch.setattr(checker_mod, "_admin_int", _disabled_admin_int)
    yield


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


# ---------- Phase 5a: identity-type plumbing ----------


async def test_check_site_skipped_when_identity_type_unsupported() -> None:
    """A username-only site probed with ``identity_type=email`` short-circuits."""
    site = _site()  # default identity_types=("username",)
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice@example.com", identity_type="email")
    assert result.status is Status.SKIPPED
    assert "identity-mismatch" in result.signals


async def test_check_site_runs_when_identity_type_matches() -> None:
    """An email-annotated site accepts ``identity_type=email`` and probes the URL."""
    site = _site(identity_types=("email",))
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, text="hi")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice@example.com", identity_type="email")
    # The default {username} substitution receives the raw email value
    # (no per-site transform registered for ``Example``).
    assert seen["url"].endswith("/alice@example.com")
    assert result.status is Status.CLAIMED


async def test_check_site_regex_check_skipped_for_non_username_identity() -> None:
    """``regex_check`` is tuned for usernames — non-username scans bypass it."""
    site = _site(
        identity_types=("email",),
        regex_check=r"^[a-z]+$",  # would reject the email's '@' otherwise
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice@example.com", identity_type="email")
    # If the regex were applied, we'd see SKIPPED with regex-mismatch.
    assert result.status is Status.CLAIMED


async def test_check_site_gravatar_email_md5_transform() -> None:
    """The curated Gravatar transform hashes the email before substitution."""
    import hashlib

    site = _site(
        name="Gravatar",
        url="https://gravatar.com/{username}",
        identity_types=("email",),
    )
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    expected_hash = hashlib.md5(b"alice@example.com").hexdigest()
    async with httpx.AsyncClient(transport=transport) as client:
        await check_site(client, site, "alice@example.com", identity_type="email")
    assert captured["url"].endswith(f"/{expected_hash}")


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
        await check_site(
            client, site, "alice", prewarm_cache=cache, prewarm_lock=lock, verify=False
        )
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


async def test_cffi_rescue_replaces_blocked_when_chrome_passes() -> None:
    """When httpx hits a WAF body, the curl-cffi rescue (mocked) should
    re-classify the result if Chrome impersonation gets through."""
    import asyncio

    from app.checker import check_site
    from app.checker_cffi import CffiRescue

    httpx_seen: list[str] = []

    def httpx_handler(req: httpx.Request) -> httpx.Response:
        httpx_seen.append(str(req.url))
        # Return a Cloudflare interstitial body — should trigger BLOCKED.
        return httpx.Response(
            200,
            text='<html><title>Just a moment...</title><div class="cf-browser-verification"></div></html>',
        )

    transport = httpx.MockTransport(httpx_handler)

    class MockResp:
        def __init__(self, status_code: int, text: str, url: str) -> None:
            self.status_code = status_code
            self.text = text
            self.url = url

    class MockSession:
        async def get(self, url, allow_redirects=True, headers=None):
            return MockResp(200, "user profile here for alice", url)

        async def head(self, url, allow_redirects=True, headers=None):
            return MockResp(200, "", url)

        async def close(self) -> None:
            return None

    rescue = CffiRescue()
    rescue._session = MockSession()  # bypass libcurl init

    site = _site(check_type="message", presense_strs=("user profile",))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(
            client,
            site,
            "alice",
            prewarm_cache=set(),
            prewarm_lock=asyncio.Lock(),
            verify=False,
            cffi_rescue=rescue,
        )
    # The httpx pass landed BLOCKED (CF body), but the cffi rescue should
    # have promoted the result back to CLAIMED.
    assert result.status is Status.CLAIMED, result.signals
    assert "cffi-rescue" in result.signals


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


# ---------------------------------------------------------------------------
# Phase 5h Layer A+B+C — claimed precision floor.
#
# These tests opt **out** of the legacy ``_disable_phase_5h_floors_for_legacy_tests``
# autouse fixture by monkey-patching ``_admin_int`` to a real value. Each
# test asserts a specific demote signal so a regression that drops one
# layer is caught immediately.
# ---------------------------------------------------------------------------


from ._phase_5h import _force_admin_int  # noqa: E402


async def test_5h_a_demotes_claimed_below_confidence_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Plain status_code claim → confidence 80 (see ``_confidence``).
    # A 90 floor demotes it, exercising Layer A.
    _force_admin_int(monkeypatch, {"min_claimed_confidence": 90})
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)
    assert result.status is Status.UNKNOWN
    assert any(s.startswith("claimed-below-confidence-floor:") for s in result.signals), (
        result.signals
    )


async def test_5h_a_keeps_claimed_above_confidence_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two-signal claim (presence + absence agree on a message site)
    # → confidence 92+. Floor of 80 keeps it CLAIMED.
    _force_admin_int(monkeypatch, {"min_claimed_confidence": 80})
    site = _site(
        check_type="message",
        presense_strs=("user-profile",),
        absence_strs=("user not found",),
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>user-profile here, definitely real</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice", verify=False)
    assert result.status is Status.CLAIMED, result.signals
    assert not any(s.startswith("claimed-below-confidence-floor:") for s in result.signals)


async def test_5h_a_floor_zero_disables_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Floor of 0 must be a no-op even for low-confidence claims.
    _force_admin_int(monkeypatch, {"min_claimed_confidence": 0})
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)
    assert result.status is Status.CLAIMED


async def test_5h_b_demotes_claimed_below_ml_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_admin_int(
        monkeypatch,
        {"min_claimed_confidence": 0, "min_claimed_ml_confidence": 80},
    )
    # Inject a low ML score directly via a patched scorer so we don't
    # depend on the ONNX model's training data.
    from app import ml as ml_mod

    monkeypatch.setattr(ml_mod, "score", lambda site, **_: 30, raising=False)

    site = _site(
        check_type="message",
        presense_strs=("user-profile",),
        absence_strs=("user not found",),
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>user-profile here</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice", verify=False)
    assert result.status is Status.UNKNOWN
    assert any(s.startswith("claimed-below-ml-floor:") for s in result.signals), result.signals


async def test_5h_b_no_demotion_when_ml_score_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ML floor is a no-op when the reranker didn't produce a score.

    Non-username scans (identity_type=email/phone/etc.) and ML-disabled
    deployments leave ``ml_confidence`` at None — those paths must
    flow through unchanged.
    """
    _force_admin_int(
        monkeypatch,
        {"min_claimed_confidence": 0, "min_claimed_ml_confidence": 80},
    )
    from app import ml as ml_mod

    monkeypatch.setattr(ml_mod, "score", lambda *a, **k: None, raising=False)

    site = _site(
        check_type="message",
        presense_strs=("user-profile",),
        absence_strs=("user not found",),
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>user-profile here</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice", verify=False)
    assert result.status is Status.CLAIMED, result.signals
    assert result.ml_confidence is None


async def test_5h_c_demotes_unverified_when_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_admin_int(
        monkeypatch,
        {
            "min_claimed_confidence": 0,
            "min_claimed_ml_confidence": 0,
            "require_verify_for_claim": 1,
        },
    )
    # verify=False → primary.verified stays None. Strict mode demotes.
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)
    assert result.status is Status.UNKNOWN
    assert "claimed-not-verified" in result.signals
    assert result.verified is None


async def test_5h_c_off_keeps_unverified_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_admin_int(
        monkeypatch,
        {
            "min_claimed_confidence": 0,
            "min_claimed_ml_confidence": 0,
            "require_verify_for_claim": 0,
        },
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)
    assert result.status is Status.CLAIMED
    assert "claimed-not-verified" not in result.signals
