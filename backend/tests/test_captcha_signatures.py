"""Phase 9d tests: vendor-specific CAPTCHA classifier.

Exercises the regex bank in :mod:`app.captcha_signatures` (every vendor
matches its canonical embed snippet, false-positive footer mentions
do *not* match, and more-specific vendors win over the
``generic_form_captcha`` fallback), the rolling per-vendor counter
(thread-safe ``record_vendor`` + ``vendor_counts`` + the
``reset_counts_for_tests`` helper), the integration with
:func:`app.checker.check_site` (default-off path is byte-for-byte
identical to pre-9d, knob-on path stamps a structured
``reason="captcha_<vendor>"`` on the BLOCKED result and bumps the
admin counter), and the new ``CheckResult.reason`` field plumbing
through ``to_dict`` / ``check_result_from_dict``.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app import captcha_signatures
from app import checker as checker_mod
from app.checker import CheckResult, Status, check_result_from_dict, check_site
from app.sites import Site

# ---------------------------------------------------------------------------
# Test fixtures + helpers.
# ---------------------------------------------------------------------------


def _site(**overrides: object) -> Site:
    base: dict[str, object] = {
        "name": "Example",
        "url": "https://example.com/{username}",
        "url_main": "https://example.com/",
        "check_type": "status_code",
    }
    base.update(overrides)
    return Site(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_vendor_counts() -> Iterator[None]:
    """Drop per-vendor counts around every test so order doesn't leak."""
    captcha_signatures.reset_counts_for_tests()
    yield
    captcha_signatures.reset_counts_for_tests()


@pytest.fixture(autouse=True)
def _disable_phase_5h_floors_for_legacy_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Disable Phase 5h precision floors so a synthetic 200-OK body
    with no presence/absence hints stays CLAIMED rather than getting
    demoted to UNKNOWN.

    Mirrors the autouse fixture in :mod:`tests.test_checker` — without
    this, the default-off path test would observe a CLAIMED → UNKNOWN
    demotion and miss the actual byte-for-byte regression that this
    suite is trying to guard.
    """
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


@pytest.fixture
def _classifier_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force ``enable_captcha_classifier`` to ``1`` for the test body.

    Patches :func:`app.checker._admin_int` rather than the SQLite
    table so the test runs without bootstrapping the admin DB. Also
    re-applies the Phase 5h floor disabler so the autouse fixture's
    monkey-patch isn't overwritten.
    """
    real_admin_int = checker_mod._admin_int

    def _patched(key: str) -> int:
        if key == "enable_captcha_classifier":
            return 1
        if key in {
            "min_claimed_confidence",
            "min_claimed_ml_confidence",
            "require_verify_for_claim",
            "enable_link_liveness",
        }:
            return 0
        return real_admin_int(key)

    monkeypatch.setattr(checker_mod, "_admin_int", _patched)
    yield


# ---------------------------------------------------------------------------
# Pure-module tests — no checker, no httpx.
# ---------------------------------------------------------------------------


def test_classify_empty_body_returns_none() -> None:
    assert captcha_signatures.classify("") is None
    assert captcha_signatures.classify("   ") is None


def test_classify_innocuous_body_returns_none() -> None:
    """A site that merely mentions the word "captcha" in a footer link
    must NOT be classified as a captcha vendor — that's the
    classic false-positive guard."""
    body = (
        "<html><body><h1>Welcome alice</h1>"
        '<footer><a href="/help/captcha-faq">Why am I asked for a captcha?</a>'
        "</footer></body></html>"
    )
    assert captcha_signatures.classify(body) is None


@pytest.mark.parametrize(
    "vendor, body",
    [
        (
            "recaptcha_v2",
            '<iframe title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=ABC"></iframe>',
        ),
        (
            "recaptcha_v3",
            '<script src="https://www.google.com/recaptcha/api.js?render=6Lc-INVISIBLEKEY"></script>',
        ),
        (
            "hcaptcha",
            '<script src="https://hcaptcha.com/1/api.js" async defer></script>',
        ),
        (
            "hcaptcha",
            '<script src="https://js.hcaptcha.com/1/api.js" async defer></script>',
        ),
        (
            "turnstile",
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" defer></script>',
        ),
        (
            "datadome",
            "<script>window._dd_ = {hsh:'ABC'};</script>",
        ),
        (
            "aws_waf_captcha",
            '<script src="https://abc123.awswaf.com/captcha.js"></script>',
        ),
        (
            "funcaptcha",
            '<input type="hidden" name="fc-token" value="..."/>',
        ),
        (
            "funcaptcha",
            '<script src="https://client-api.arkoselabs.com/v2/abc.js"></script>',
        ),
        (
            "geetest",
            '<script>initGeetest({gt: "abc"}, function(){});</script>',
        ),
        (
            "perimeterx",
            "<script>window._pxAppId = 'PX-ABC';</script>",
        ),
        (
            "generic_form_captcha",
            '<form action="/internal/captcha-check" method="post"><input name="answer"/></form>',
        ),
    ],
)
def test_classify_recognises_known_vendor_signatures(vendor: str, body: str) -> None:
    assert captcha_signatures.classify(body) == vendor


def test_specific_vendor_wins_over_generic_form_captcha() -> None:
    """A page that ships both a hCaptcha widget AND a ``<form
    action="/captcha">`` (some sites do — the form is the no-JS
    fallback) must be classified as ``hcaptcha``, not as
    ``generic_form_captcha``. The signature order in
    :data:`_SIGNATURES` enforces this."""
    body = (
        '<form action="/captcha" method="post">…</form>'
        '<script src="https://hcaptcha.com/1/api.js" async defer></script>'
    )
    assert captcha_signatures.classify(body) == "hcaptcha"


def test_known_vendors_returns_expected_set() -> None:
    vendors = captcha_signatures.known_vendors()
    assert "recaptcha_v2" in vendors
    assert "recaptcha_v3" in vendors
    assert "hcaptcha" in vendors
    assert "turnstile" in vendors
    assert "datadome" in vendors
    assert "aws_waf_captcha" in vendors
    assert "funcaptcha" in vendors
    assert "geetest" in vendors
    assert "perimeterx" in vendors
    assert "generic_form_captcha" in vendors
    # Generic catch-all must be LAST so a vendor-specific signature
    # is always preferred (covered by
    # ``test_specific_vendor_wins_over_generic_form_captcha`` above).
    assert vendors[-1] == "generic_form_captcha"


def test_record_vendor_and_vendor_counts_round_trip() -> None:
    captcha_signatures.record_vendor("recaptcha_v2")
    captcha_signatures.record_vendor("recaptcha_v2")
    captcha_signatures.record_vendor("turnstile")
    counts = captcha_signatures.vendor_counts()
    assert counts == {"recaptcha_v2": 2, "turnstile": 1}


def test_reset_counts_for_tests_clears_counter() -> None:
    captcha_signatures.record_vendor("hcaptcha")
    assert captcha_signatures.vendor_counts() == {"hcaptcha": 1}
    captcha_signatures.reset_counts_for_tests()
    assert captcha_signatures.vendor_counts() == {}


def test_vendor_counts_returns_a_copy() -> None:
    """The dict returned to callers must be a snapshot — mutating it
    must not leak back into the live counter (otherwise the admin
    JSON serialiser could clear the counter by accident)."""
    captcha_signatures.record_vendor("turnstile")
    snapshot = captcha_signatures.vendor_counts()
    snapshot["turnstile"] = 999
    snapshot["bogus"] = 1
    assert captcha_signatures.vendor_counts() == {"turnstile": 1}


# ---------------------------------------------------------------------------
# CheckResult dataclass plumbing.
# ---------------------------------------------------------------------------


def test_check_result_reason_defaults_to_none() -> None:
    """The new field must default to ``None`` so all pre-9d call
    sites that build a :class:`CheckResult` without ``reason=``
    continue to work without modification."""
    result = CheckResult(
        site="x",
        status=Status.CLAIMED,
        url="https://x/",
        url_main="https://x/",
        tags=[],
    )
    assert result.reason is None
    assert result.to_dict()["reason"] is None


def test_check_result_to_dict_round_trips_reason() -> None:
    payload = CheckResult(
        site="x",
        status=Status.BLOCKED,
        url="https://x/",
        url_main="https://x/",
        tags=[],
        reason="captcha_turnstile",
    ).to_dict()
    assert payload["reason"] == "captcha_turnstile"
    rebuilt = check_result_from_dict(payload)
    assert rebuilt.reason == "captcha_turnstile"


# ---------------------------------------------------------------------------
# Integration tests — end-to-end through ``check_site``.
# ---------------------------------------------------------------------------


async def test_classifier_off_is_byte_for_byte_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 9d default-off contract: with the knob at 0, a Turnstile
    body that wouldn't have triggered ``_is_blocked_body`` (Turnstile
    isn't in ``_BLOCK_FINGERPRINTS``) must still classify as CLAIMED
    on the 200 — i.e. exactly the pre-9d behaviour."""
    real_admin_int = checker_mod._admin_int

    def _disabled(key: str) -> int:
        if key == "enable_captcha_classifier":
            return 0
        return real_admin_int(key)

    monkeypatch.setattr(checker_mod, "_admin_int", _disabled)

    turnstile_body = (
        "<html><body>"
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" defer></script>'
        "</body></html>"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=turnstile_body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)

    # Default-off → status is CLAIMED (status_code-only classifier on
    # 200), no reason stamped, vendor counter untouched.
    assert result.status is Status.CLAIMED
    assert result.reason is None
    assert captcha_signatures.vendor_counts() == {}


async def test_classifier_on_blocks_turnstile_and_stamps_reason(
    _classifier_on: None,
) -> None:
    """Knob-on path: a Turnstile body short-circuits the status-only
    classifier and BLOCKED is set with a vendor-specific reason."""
    body = (
        "<html><body>"
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" defer></script>'
        "</body></html>"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)

    assert result.status is Status.BLOCKED
    assert result.reason == "captcha_turnstile"
    assert "body-fingerprint-block" in result.signals
    assert captcha_signatures.vendor_counts() == {"turnstile": 1}


async def test_classifier_on_overrides_generic_block_with_specific_vendor(
    _classifier_on: None,
) -> None:
    """When BOTH ``_is_blocked_body`` and a vendor signature fire (a
    Cloudflare interstitial that happens to ship the hCaptcha widget),
    the vendor-specific reason wins — the SPA still gets the precise
    label, not the generic fingerprint."""
    body = (
        "<html><head><title>Just a moment...</title></head>"
        '<body><div class="cf-browser-verification">checking your browser</div>'
        '<script src="https://hcaptcha.com/1/api.js" async defer></script>'
        "</body></html>"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)

    assert result.status is Status.BLOCKED
    assert result.reason == "captcha_hcaptcha"
    # The generic fingerprint signal is still present so the existing
    # downstream code paths (Phase 2.2 cffi rescue, scan stats) keep
    # working — we only enrich the result, never strip a signal.
    assert "body-fingerprint-block" in result.signals
    assert captcha_signatures.vendor_counts() == {"hcaptcha": 1}


async def test_classifier_on_does_not_set_reason_when_no_vendor_matches(
    _classifier_on: None,
) -> None:
    """A 429 with no body still surfaces as BLOCKED via ``status-429``
    but ``reason`` stays ``None`` because no vendor signature fired."""
    transport = httpx.MockTransport(lambda _req: httpx.Response(429, text=""))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)

    assert result.status is Status.BLOCKED
    assert "status-429" in result.signals
    assert result.reason is None
    assert captcha_signatures.vendor_counts() == {}


async def test_classifier_on_innocuous_body_keeps_pre_9d_classification(
    _classifier_on: None,
) -> None:
    """Even with the knob ON, a body that mentions "captcha" in a
    footer link must NOT be reclassified as BLOCKED — the regex
    bank only matches structural embed snippets, not free text."""
    body = (
        "<html><body><h1>Profile of alice</h1>"
        '<footer><a href="/help/captcha-faq">What is a captcha?</a></footer>'
        "</body></html>"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice", verify=False)

    assert result.status is Status.CLAIMED
    assert result.reason is None
    assert captcha_signatures.vendor_counts() == {}
