"""Phase 9d: CAPTCHA classifier + structured ``UNKNOWN`` labelling.

Inspects probe response bodies for vendor-specific CAPTCHA / anti-bot
challenge signatures and surfaces a more specific ``reason`` than the
generic ``body-fingerprint-block`` signal that
:func:`app.checker._is_blocked_body` emits today.

Detection is implemented as a small registry of pre-compiled regex
patterns — fast, no DOM parser dependency, and easy to extend. Each
pattern is compiled exactly once at import time so the hot path is
``O(N_vendors)`` per probe, where ``N_vendors`` is currently ~10.

Vendor counter
--------------

The per-vendor count is an in-process :class:`collections.Counter`
wrapped in a :class:`threading.Lock` so :func:`record_vendor` can be
called from any worker thread without a TOCTTOU race against the read
in ``/api/admin/ml/status``. The counter is intentionally never
persisted to disk — operators only need a "this week" snapshot, not
historical analytics; a future Phase 9e/9f revision can promote it to a
Redis stream once the rest of the platform demands it.

Default-off contract
--------------------

Phase 9d ships behind the ``enable_captcha_classifier`` admin knob
(default ``0``). When the knob is off, :func:`app.checker.check_site`
never calls into this module so the runtime is byte-for-byte equivalent
to the pre-9d behaviour.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Signature:
    """One vendor + its body-matching regex.

    ``pattern`` is searched against the raw response body (HTML / text)
    via :func:`re.Pattern.search`. The first signature that matches in
    iteration order wins, so more-specific vendors are listed before
    the generic fallbacks.
    """

    vendor: str
    pattern: re.Pattern[str]


# Curated bank of vendor signatures. Order matters: the first match
# wins, so list more-specific vendors before generic fallbacks like
# ``generic_form_captcha`` (which would otherwise eat a hCaptcha hit).
#
# Patterns intentionally anchor on stable, vendor-controlled tokens
# (script ``src`` URLs, JS API names) rather than on free-text
# wording — the latter is far easier to false-positive on a footer
# link or a forum post that mentions "captcha" in passing.
_SIGNATURES: tuple[_Signature, ...] = (
    # reCAPTCHA v2 — anchor iframe src is the canonical embed.
    _Signature(
        vendor="recaptcha_v2",
        pattern=re.compile(
            r'<iframe[^>]*\bsrc=["\']https://www\.google\.com/recaptcha/api2/anchor',
            re.IGNORECASE,
        ),
    ),
    # reCAPTCHA v3 — invisible variant; only the api.js?render= form
    # is unambiguous (v2 sites also load api.js without ?render=).
    _Signature(
        vendor="recaptcha_v3",
        pattern=re.compile(
            r'<script[^>]*\bsrc=["\']https://www\.google\.com/recaptcha/api\.js\?[^"\']*\brender=',
            re.IGNORECASE,
        ),
    ),
    # hCaptcha — js.hcaptcha.com or hcaptcha.com/1/api.js are both valid.
    _Signature(
        vendor="hcaptcha",
        pattern=re.compile(
            r'<script[^>]*\bsrc=["\']https://(?:js\.)?hcaptcha\.com/1/api\.js',
            re.IGNORECASE,
        ),
    ),
    # Cloudflare Turnstile — challenges.cloudflare.com/turnstile script.
    _Signature(
        vendor="turnstile",
        pattern=re.compile(
            r'<script[^>]*\bsrc=["\']https://challenges\.cloudflare\.com/turnstile',
            re.IGNORECASE,
        ),
    ),
    # Datadome — anchor on the global JS namespace and the captcha host.
    _Signature(
        vendor="datadome",
        pattern=re.compile(
            r"(?:window\._dd_|geo\.captcha-delivery\.com|datadome\.co/captcha|dd_cookie_test_)",
            re.IGNORECASE,
        ),
    ),
    # AWS WAF Captcha — anchor on the captcha JS bundle host.
    _Signature(
        vendor="aws_waf_captcha",
        pattern=re.compile(
            r"(?:awswaf\.com/captcha|aws-waf-token|aws-waf-captcha\.js)",
            re.IGNORECASE,
        ),
    ),
    # Arkose / FunCaptcha — fc-token field or the funcaptcha.com host.
    _Signature(
        vendor="funcaptcha",
        pattern=re.compile(
            r'(?:funcaptcha\.com/|client-api\.arkoselabs\.com|name=["\']?fc-token)',
            re.IGNORECASE,
        ),
    ),
    # Geetest — gt_captcha JS or geetest.com host.
    _Signature(
        vendor="geetest",
        pattern=re.compile(
            r"(?:static\.geetest\.com|api\.geetest\.com|gt_captcha|initGeetest)",
            re.IGNORECASE,
        ),
    ),
    # PerimeterX — _pxAppId / px-captcha JS bundle.
    _Signature(
        vendor="perimeterx",
        pattern=re.compile(
            r"(?:_pxAppId|px-captcha|client\.perimeterx\.net)",
            re.IGNORECASE,
        ),
    ),
    # Generic ``<form action="...captcha...">`` — the catch-all for
    # custom captcha endpoints (Apache mod_security, bespoke WAFs).
    # Listed LAST so a vendor-specific match always wins.
    _Signature(
        vendor="generic_form_captcha",
        pattern=re.compile(
            r'<form[^>]+\baction=["\'][^"\']*\bcaptcha\b',
            re.IGNORECASE,
        ),
    ),
)


# In-process per-vendor counter. The lock guards both reads and writes
# because :class:`collections.Counter` is *not* thread-safe across
# concurrent ``+=`` operations — without the lock two probes that hit
# the same vendor at the same instant could race and lose an increment.
_VENDOR_COUNTS: Counter[str] = Counter()
_COUNT_LOCK = threading.Lock()


def classify(body: str) -> str | None:
    """Return the first matching vendor name, or ``None`` if none match.

    Iteration order is :data:`_SIGNATURES`, which lists vendor-specific
    patterns before generic fallbacks. An empty body short-circuits to
    ``None`` so the hot path doesn't even compile the regex tape on
    every HEAD probe.
    """

    if not body:
        return None
    for sig in _SIGNATURES:
        if sig.pattern.search(body):
            return sig.vendor
    return None


def record_vendor(vendor: str) -> None:
    """Bump the in-process counter for ``vendor`` by one.

    Callers (the checker hot path) should only invoke this after a
    successful :func:`classify` — passing an arbitrary string still
    works (the Counter accepts any hashable key) but pollutes the
    admin status surface with non-vendor names.
    """

    with _COUNT_LOCK:
        _VENDOR_COUNTS[vendor] += 1


def vendor_counts() -> dict[str, int]:
    """Snapshot of the current per-vendor counts.

    The dict is a copy taken under the lock so callers (the admin JSON
    serialiser) can iterate it freely without observing a torn state
    while a concurrent probe bumps a counter.
    """

    with _COUNT_LOCK:
        return dict(_VENDOR_COUNTS)


def reset_counts_for_tests() -> None:
    """Clear the in-process counter. Test-only.

    Production code never calls this — operators rely on the rolling
    counter for the lifetime of the worker process. Tests use it as a
    pytest fixture teardown so ordering between tests doesn't leak
    counter state.
    """

    with _COUNT_LOCK:
        _VENDOR_COUNTS.clear()


def known_vendors() -> tuple[str, ...]:
    """Return every vendor name this module can emit, in match order.

    Useful for parameterising tests and for the admin SPA which can
    pre-render a zero entry per known vendor before the first probe
    reports a hit.
    """

    return tuple(sig.vendor for sig in _SIGNATURES)
