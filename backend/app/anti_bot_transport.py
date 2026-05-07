"""Phase 9a: anti-bot probe transport — curl-cffi browser-impersonating
``httpx.AsyncBaseTransport`` + JS-challenge detection.

Phase 8b's :class:`app.transport.RotatingProxyTransport` spreads load
across egress IPs, but every request still presents the OpenSSL TLS
fingerprint that comes with vanilla Python. WAFs that fingerprint the
TLS handshake (cipher suite ordering + extension list = JA3/JA4)
flag those requests regardless of how good the proxy pool is — that's
the residual risk **C2** from the Phase 8 deploy debrief, paired with
**C1** (Cloudflare / Akamai / Datadome JS-challenge HTML returned in
place of the real page body).

This module wraps ``curl-cffi``'s ``AsyncSession.request`` — which
speaks the on-the-wire Chrome / Safari / Firefox handshake via
libcurl-impersonate — into an :class:`httpx.AsyncBaseTransport` so the
rest of the codebase keeps using its existing ``httpx.AsyncClient``
abstractions. The transport is intentionally a peer of
``RotatingProxyTransport``: :func:`app.transport.build_anti_bot_client`
stacks a ``RotatingProxyTransport`` *outside* this transport so the
proxy rotation lives at the request boundary and the impersonation
lives at the dispatch boundary, in that order.

JS-challenge detection
----------------------
After every dispatch, :func:`looks_like_js_challenge` runs a fast
substring scan against the response body to detect the canonical
Cloudflare / Akamai / Datadome challenge interstitials. The caller
can use that signal to gate a Playwright fallback (see
:mod:`app.playwright_fallback`).

Backward compatibility
----------------------
When ``enable_anti_bot_pipeline=0`` (the default), this module is
inert — :func:`is_enabled` is ``False`` and
:func:`app.transport.build_async_client` keeps returning the Phase 8b
``RotatingProxyTransport``-only stack. HF Space's single-container
deployment sees no behavioural change.

Per-site override
-----------------
A site whose admin override carries an ``antiBotProfile`` field gets
that impersonation profile instead of the global default. Useful for
e.g. a WAF that fingerprints Safari handshakes specifically while the
rest of the catalog stays on Chrome.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, cast

import httpx

from .config import settings

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Whitelist of curl-cffi impersonation profiles we expose as admin
# knob values + ``site_overrides.antiBotProfile``. Pinning the list
# keeps a typo from silently bypassing the WAF dance — curl-cffi's
# accepted ``impersonate`` literals change between minor releases, but
# these four are stable across the 0.7+ line we depend on.
SUPPORTED_IMPERSONATIONS: frozenset[str] = frozenset(
    {
        "chrome120",
        "chrome131",
        "safari17",
        "firefox120",
    }
)


# Compiled regex bank for JS-challenge interstitials. Keep these
# anchored on tag fragments rather than full ``<head>...</head>``
# blocks so the matcher works on partial response bodies (curl-cffi
# can hand us up to ``content`` bytes before we explicitly stream).
_CHALLENGE_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    # Cloudflare "Just a moment..." (managed challenge + I'm Under Attack mode).
    re.compile(rb"<title[^>]*>\s*Just a moment\.\.\.\s*</title>", re.IGNORECASE),
    # Cloudflare classic "Attention Required! | Cloudflare".
    re.compile(rb"<title[^>]*>\s*Attention Required!\s*\|\s*Cloudflare\s*</title>", re.IGNORECASE),
    # Cloudflare Turnstile inline widget bootstrap (also fires from a
    # Cloudflare-fronted JS challenge page).
    re.compile(rb"challenges\.cloudflare\.com/turnstile", re.IGNORECASE),
    # Datadome challenge meta tag + dd-* boot script.
    re.compile(rb'<meta[^>]+name=["\']dd[^"\']*["\']', re.IGNORECASE),
    re.compile(rb"datadome\.co/captcha", re.IGNORECASE),
    # Akamai Bot Manager interstitial.
    re.compile(rb"<title[^>]*>Pardon Our Interruption</title>", re.IGNORECASE),
    re.compile(rb"_abck=", re.IGNORECASE),
    # PerimeterX human-verification page.
    re.compile(rb"PerimeterX,\s*Inc\.", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Knob accessors
# ---------------------------------------------------------------------------


def _enabled_knob() -> int:
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_anti_bot_pipeline")
    except Exception:
        return int(settings.enable_anti_bot_pipeline)


def is_enabled() -> bool:
    """``True`` when the anti-bot transport is active."""
    return _enabled_knob() == 1


def default_impersonation() -> str:
    """Return the configured default impersonation profile.

    Falls back to ``"chrome120"`` if the env value isn't in
    :data:`SUPPORTED_IMPERSONATIONS` so a typo doesn't silently
    disable the pipeline.
    """
    raw = (settings.anti_bot_default_impersonation or "").strip()
    if raw in SUPPORTED_IMPERSONATIONS:
        return raw
    return "chrome120"


def is_chromium_installed() -> bool:
    """``True`` iff a Playwright chromium runtime is reachable.

    Mirrored on ``/api/health`` so an operator can verify the
    Playwright fallback is actually wired up without firing a probe.
    Importing :mod:`app.playwright_fallback` lazily here keeps this
    module dep-free when the fallback is disabled.
    """
    try:
        from . import playwright_fallback
    except ImportError:
        return False
    return playwright_fallback.chromium_available()


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health.anti_bot``."""
    return {
        "enabled": is_enabled(),
        "default_impersonation": default_impersonation(),
        "playwright_fallback": _playwright_fallback_enabled(),
        "playwright_chrome_installed": is_chromium_installed(),
    }


def _playwright_fallback_enabled() -> int:
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("anti_bot_playwright_fallback")
    except Exception:
        return int(settings.anti_bot_playwright_fallback)


# ---------------------------------------------------------------------------
# JS-challenge detection
# ---------------------------------------------------------------------------


def looks_like_js_challenge(body: bytes | str | None) -> bool:
    """Return ``True`` when ``body`` looks like a WAF challenge page.

    Operates on at most the first 64 KiB of the body so a 50 MiB
    download doesn't penalise the matcher; challenge interstitials
    are tiny and always include the canonical fragments in the head.
    """
    if not body:
        return False
    if isinstance(body, str):
        sample = body.encode("utf-8", errors="replace")[:65536]
    else:
        sample = bytes(body[:65536])
    return any(pat.search(sample) for pat in _CHALLENGE_PATTERNS)


# ---------------------------------------------------------------------------
# Per-site override resolution
# ---------------------------------------------------------------------------


def resolve_impersonation(site_name: str | None) -> str:
    """Pick the impersonation profile for ``site_name``.

    Order of precedence:

    1. ``site_overrides.<name>.patch.antiBotProfile`` when present and
       in :data:`SUPPORTED_IMPERSONATIONS`.
    2. :func:`default_impersonation`.
    """
    if site_name:
        try:
            from . import site_overrides as site_overrides_mod
        except ImportError:
            site_overrides_mod = None  # type: ignore[assignment]
        if site_overrides_mod is not None:
            override = site_overrides_mod.get_override(site_name)
            if override is not None:
                profile = override.patch.get("antiBotProfile")
                if isinstance(profile, str) and profile in SUPPORTED_IMPERSONATIONS:
                    return profile
    return default_impersonation()


# ---------------------------------------------------------------------------
# AntiBotTransport
# ---------------------------------------------------------------------------


class AntiBotTransport(httpx.AsyncBaseTransport):
    """``httpx.AsyncBaseTransport`` backed by ``curl-cffi`` impersonation.

    Each ``handle_async_request`` opens a per-impersonation
    :class:`curl_cffi.requests.AsyncSession` (cached) and dispatches
    the request through it. Response bytes + headers + status code are
    rebuilt into an :class:`httpx.Response` so the caller is unaware
    that the actual TLS handshake spoke Chrome / Safari / Firefox.

    The transport intentionally does **not** take a ``proxy`` argument.
    Proxy rotation lives in :class:`app.transport.RotatingProxyTransport`
    which is stacked *outside* this transport via
    :func:`app.transport.build_anti_bot_client`. That keeps the two
    concerns orthogonal — the pool decides the egress IP, this
    transport decides the TLS fingerprint.

    Failure modes
    -------------
    * ``curl-cffi`` not installed → :class:`RuntimeError` raised
      eagerly so the operator sees a clear remediation message
      instead of a misleading network failure.
    * ``curl-cffi`` raises during dispatch → re-raised as
      :class:`httpx.ConnectError` so callers don't have to special-
      case curl-cffi's exception hierarchy.
    """

    def __init__(
        self,
        *,
        default_profile: str | None = None,
        timeout: float | None = None,
        site_resolver: Any | None = None,
    ) -> None:
        self._default_profile = default_profile or default_impersonation()
        self._timeout = timeout if timeout is not None else float(settings.cffi_timeout)
        # Allow tests to inject a custom (host -> profile) resolver
        # without going through the site_overrides DB.
        self._site_resolver = site_resolver
        self._sessions: dict[str, Any] = {}
        self._session_unavailable = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        profile = self._profile_for(request)
        session = self._get_session(profile)
        if session is None:
            raise httpx.ConnectError(
                "curl-cffi unavailable: install the [scale] extra or unset "
                "RECKORA_ENABLE_ANTI_BOT_PIPELINE",
                request=request,
            )

        body = await request.aread() if request.stream is not None else request.content
        headers = self._headers_to_curl(request.headers)
        try:
            resp = await session.request(
                method=request.method,
                url=str(request.url),
                headers=headers,
                data=body if body else None,
                timeout=self._timeout,
                allow_redirects=False,
                impersonate=profile,
            )
        except Exception as exc:
            raise httpx.ConnectError(
                f"anti-bot dispatch failed via curl-cffi/{profile}: {exc}",
                request=request,
            ) from exc

        return self._to_httpx_response(resp, request)

    async def aclose(self) -> None:
        for session in self._sessions.values():
            with contextlib.suppress(Exception):
                await session.close()
        self._sessions.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _profile_for(self, request: httpx.Request) -> str:
        """Resolve the impersonation profile to use for ``request``.

        The resolver is called with the request host; production wires
        this to :func:`resolve_impersonation` keyed off the matched
        site name (set by the checker via the
        ``X-Reckora-Site-Name`` request header). When no resolver is
        installed, the default profile is used.
        """
        site_name = request.headers.get("x-reckora-site-name")
        if site_name and self._site_resolver is not None:
            return self._site_resolver(site_name) or self._default_profile
        if site_name:
            try:
                return resolve_impersonation(site_name) or self._default_profile
            except Exception:
                return self._default_profile
        return self._default_profile

    def _get_session(self, profile: str) -> Any | None:
        if profile in self._sessions:
            return self._sessions[profile]
        if self._session_unavailable:
            return None
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            self._session_unavailable = True
            return None
        try:
            # curl-cffi annotates ``impersonate`` as a ``Literal`` of all
            # currently supported browser tokens. The set churns between
            # minor releases (chrome131, chrome133a, safari260, ...), so
            # we cast through ``Any`` here to keep the call safe against
            # version drift while keeping our own
            # :data:`SUPPORTED_IMPERSONATIONS` allowlist as the source of
            # truth for what the runtime accepts.
            self._sessions[profile] = AsyncSession(
                impersonate=cast(Any, profile),
                timeout=self._timeout,
            )
        except Exception:
            self._session_unavailable = True
            return None
        return self._sessions[profile]

    @staticmethod
    def _headers_to_curl(headers: httpx.Headers) -> dict[str, str]:
        """Convert an :class:`httpx.Headers` instance into a curl-cffi
        compatible ``dict[str, str]``.

        ``Host`` is dropped because curl-cffi reconstructs it from the
        URL; including it explicitly tends to produce duplicate entries
        on some libcurl versions. ``Content-Length`` is also dropped —
        curl-cffi recomputes it from the body so a stale length sent
        by httpx (e.g. after the request was rewritten) doesn't trip
        the libcurl ``CURLE_PARTIAL_FILE`` failure mode.

        We iterate ``headers.raw`` so the original header *casing* is
        preserved on the wire — WAFs that fingerprint header casing
        (alongside JA3 / JA4) treat ``User-Agent`` and ``user-agent``
        as different signals, and the whole point of this transport is
        to look exactly like the impersonated browser.
        """
        out: dict[str, str] = {}
        for raw_key, raw_val in headers.raw:
            try:
                k = raw_key.decode("latin-1")
                v = raw_val.decode("latin-1")
            except Exception:
                continue
            kl = k.lower()
            if kl in ("host", "content-length"):
                continue
            # Internal routing-only header; never forwarded over the wire.
            if kl == "x-reckora-site-name":
                continue
            out[k] = v
        return out

    @staticmethod
    def _to_httpx_response(resp: Any, request: httpx.Request) -> httpx.Response:
        """Rebuild an :class:`httpx.Response` from a curl-cffi ``Response``.

        curl-cffi's ``Response`` exposes ``status_code``, ``headers``
        (multi-dict), and ``content`` (bytes). We forward the lot to
        ``httpx.Response`` and bind the original request so the
        downstream client treats it identically to a vanilla httpx
        dispatch.
        """
        # curl-cffi's headers object is dict-ish but not always strictly
        # a Mapping[str, str]; coerce defensively so ``httpx.Headers``
        # construction never blows up on a None / bytes value.
        headers_iter: list[tuple[str, str]] = []
        raw_headers = getattr(resp, "headers", None)
        if raw_headers is not None:
            try:
                items = raw_headers.multi_items()
            except AttributeError:
                items = list(raw_headers.items())
            for k, v in items:
                if v is None:
                    continue
                headers_iter.append((str(k), str(v)))
        content = getattr(resp, "content", b"") or b""
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        return httpx.Response(
            status_code=int(getattr(resp, "status_code", 200) or 200),
            headers=headers_iter,
            content=bytes(content),
            request=request,
        )
