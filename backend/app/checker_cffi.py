"""Phase 2.2 rescue path — curl-cffi probe for BLOCKED sites.

httpx (and any vanilla async HTTP client built on Python's ``ssl`` module)
shows a clean OpenSSL TLS fingerprint. Cloudflare, Akamai, and most modern
WAFs key on JA3/JA4 + h2-priority + a small grab-bag of Chrome-shaped
quirks; that's why our httpx probe routinely lands on a 403 challenge or
the WAF-body interstitial even when a real browser visits the same URL
fine.

curl-cffi exposes libcurl-impersonate, which speaks the on-the-wire
Chrome handshake (cipher list, ALPN, h2 SETTINGS frame, X.509 negotiation
order, etc). Probing a Reckora-classified-BLOCKED site through curl-cffi
gives us a second opinion from an identity that the WAF is much less
likely to flag.

Design choices:

* We **only** call this on sites the httpx pass already labelled
  BLOCKED. A blanket switch to curl-cffi would slow the scan ~3x for the
  ~95 % of sites that are perfectly happy talking to vanilla httpx, and
  it would also defeat the cookie jar prewarm (curl-cffi has its own
  jar). Re-checking only blocked sites keeps the cost bounded.
* If curl-cffi *also* returns BLOCKED, we keep the original BLOCKED
  result — no false-positive escalation.
* If curl-cffi gets through to a real classification (CLAIMED / FREE /
  UNKNOWN), we replace the result and tag a ``cffi-rescue`` signal so
  the SPA can show the user that the high-trust path was used.
* The session is shared across one scan via ``CffiRescue`` so we pay
  the libcurl init cost once.
"""
from __future__ import annotations

import contextlib
import time

from .checker import (
    _BLOCK_FINGERPRINTS,
    CheckResult,
    Status,
    _classify_response,
    _confidence,
    _is_blocked_body,
    _matches_regex,
)
from .config import settings
from .sites import Site


class CffiRescue:
    """Lazy holder for a per-scan curl-cffi AsyncSession.

    Importing curl-cffi pulls in a ~30 MB native dep with libcurl-impersonate
    binaries. We keep it behind a lazy attribute so test environments and
    callers that opt out (``settings.enable_curl_cffi=False``) don't pay
    the import cost.
    """

    def __init__(self) -> None:
        self._session = None
        self._session_unavailable = False

    async def __aenter__(self) -> CffiRescue:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    def _get_session(self):
        """Lazy import + init. Returns None if curl-cffi is unavailable."""
        if self._session is not None:
            return self._session
        if self._session_unavailable:
            return None
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            self._session_unavailable = True
            return None
        try:
            self._session = AsyncSession(
                impersonate=settings.cffi_impersonate,
                timeout=settings.cffi_timeout,
            )
        except Exception:
            self._session_unavailable = True
            return None
        return self._session

    async def rescue(self, site: Site, username: str) -> CheckResult | None:
        """Re-probe ``site`` with a Chrome-impersonated TLS handshake.

        Returns ``None`` if curl-cffi is unavailable or the rescue itself
        errors out — callers should fall back to the original BLOCKED
        result. Returns a fresh CheckResult on success.
        """
        if not settings.enable_curl_cffi:
            return None
        url = site.url.replace("{username}", username)
        if not _matches_regex(username, site.regex_check):
            return None
        session = self._get_session()
        if session is None:
            return None

        started = time.perf_counter()
        method = "HEAD" if (site.head_only and site.check_type == "status_code") else "GET"
        headers = dict(site.headers)
        try:
            if method == "HEAD":
                resp = await session.head(url, allow_redirects=True, headers=headers)
            else:
                resp = await session.get(url, allow_redirects=True, headers=headers)
        except Exception as e:
            return CheckResult(
                site=site.name,
                status=Status.ERROR,
                url=url,
                url_main=site.url_main,
                tags=list(site.tags),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=f"cffi:{type(e).__name__}",
                signals=["cffi-rescue-error"],
            )

        body = "" if method == "HEAD" else (resp.text or "")
        # Phase 2.2: re-check the body against the same anti-bot
        # fingerprints as the httpx pass. If curl-cffi *also* hit the WAF
        # interstitial we shouldn't mask that.
        if resp.status_code in (429, 451) or (body and _is_blocked_body(body)):
            sigs = ["cffi-rescue-still-blocked", f"status-{resp.status_code}"]
            if body and _is_blocked_body(body):
                sigs.append("body-fingerprint-block")
            return CheckResult(
                site=site.name,
                status=Status.BLOCKED,
                url=url,
                url_main=site.url_main,
                tags=list(site.tags),
                http_status=resp.status_code,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                confidence=_confidence(site, Status.BLOCKED, resp.status_code, sigs),
                signals=sigs,
            )

        # Build a synthetic httpx-shaped response for the existing
        # classifier. We only need ``status_code`` and ``url`` (final
        # redirected URL) — both are available on curl-cffi responses.
        class _FakeResp:
            def __init__(self, status_code: int, url: str) -> None:
                self.status_code = status_code
                self.url = url

        fake = _FakeResp(resp.status_code, str(resp.url))
        status, signals = _classify_response(fake, body, site)  # type: ignore[arg-type]
        signals = ["cffi-rescue", *signals]
        confidence = _confidence(site, status, resp.status_code, signals)
        # Phase 3: rescued CLAIMED hits should still produce identity
        # extracts; the body we just got from curl-cffi is the highest-trust
        # body we'll have for these sites.
        extracted: dict[str, str] | None = None
        if (
            status is Status.CLAIMED
            and confidence >= settings.extract_min_confidence
            and body
        ):
            from .extractor import extract_identifiers

            try:
                extracted = extract_identifiers(body, url)
            except Exception:  # pragma: no cover
                extracted = {}
        return CheckResult(
            site=site.name,
            status=status,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            http_status=resp.status_code,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            confidence=confidence,
            signals=signals,
            extracted=extracted,
        )


# Re-export for tests.
__all__ = ["_BLOCK_FINGERPRINTS", "CffiRescue"]
