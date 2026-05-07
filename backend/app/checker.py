"""Per-site username checker.

Heavily inspired by maigret's check logic, but rewritten to be small,
async-first, and self-contained. We support three check types from the
upstream site database:

- ``status_code``: 2xx → claimed, 404 → free, anything else → unknown.
- ``message``: parse HTML body for ``presenseStrs`` (claimed) /
  ``absenceStrs`` (free). Either set may be present; both are honored.
- ``response_url``: if the final URL after redirects matches the templated
  URL, the username is claimed.

Phase 1 additions on top of the maigret-equivalent classifier:

- ``BLOCKED`` status — captures Cloudflare/Akamai/captcha pages, 429s,
  and 451s. Without this, message-classified sites would either
  false-positive (claimed) or false-negative (free) when the WAF replied
  instead of the application. Maigret rolls these into ``ERROR``.
- ``confidence`` 0-100 - surfaced on every result. Two-signal hits
  (presence AND absence agree) score highest; single-signal classifies
  score lower; status-only classifies score middle. This is what
  downstream verification (Phase 2) and the SPA's confidence-sorted view
  rely on.
- ``HEAD`` probe for ``status_code`` sites that opt in via
  ``request_head_only`` — the body isn't needed to classify, so we save
  ~all of its bandwidth.
- Smart retry with exponential backoff for transient transport errors
  (timeout, server-errors 502/503/504, ``RemoteProtocolError``). Without
  retries, bursty packet loss inflates the ``ERROR`` rate and erases
  recall on long scans.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from .config import settings
from .identity import (
    DEFAULT_IDENTITY_TYPE,
    IdentityType,
    is_valid_username,
    transform_identifier,
)
from .sites import Site

__all__ = [
    "CheckResult",
    "Status",
    "check_result_from_dict",
    "check_site",
    "is_valid_username",
    "stream_checks",
    "stream_checks_via_celery",
]


def check_result_from_dict(payload: dict[str, Any]) -> CheckResult:
    """Reverse of ``CheckResult.to_dict``.

    Used by the Phase 8a Celery collector to rebuild ``CheckResult``
    objects from JSON returned by ``check_site_task`` so that
    downstream callers (the SSE stream pipeline, history recorder, ML
    recorder) receive the same dataclass shape regardless of whether
    the in-process or distributed path produced the result.
    """

    status_raw = payload.get("status", Status.ERROR.value)
    try:
        status = Status(status_raw)
    except ValueError:
        status = Status.ERROR
    return CheckResult(
        site=str(payload.get("site", "")),
        status=status,
        url=str(payload.get("url", "")),
        url_main=str(payload.get("url_main", "")),
        tags=list(payload.get("tags") or ()),
        http_status=payload.get("http_status"),
        elapsed_ms=payload.get("elapsed_ms"),
        error=payload.get("error"),
        confidence=int(payload.get("confidence") or 0),
        signals=list(payload.get("signals") or ()),
        attempts=int(payload.get("attempts") or 1),
        verified=payload.get("verified"),
        extracted=payload.get("extracted"),
        ml_confidence=payload.get("ml_confidence"),
        reason=payload.get("reason"),
    )


def _extract_min_confidence() -> int:
    """Defer the import to break the cycle between checker → admin_settings → auth."""
    from . import admin_settings as admin_settings_mod

    return admin_settings_mod.effective_int("extract_min_confidence")


def _admin_int(key: str) -> int:
    """Return the admin-tunable knob value for ``key`` (Phase 5h helper).

    Falls back to the ``Settings`` env-var default when the admin DB
    isn't initialised yet (typical for unit tests that hit the checker
    directly without a TestClient bootstrapping the schema). The
    fallback path keeps Phase-5h floors active even pre-DB-init,
    using whatever default the operator set via ``RECKORA_*`` env vars.
    """
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int(key)
    except Exception:
        spec = admin_settings_mod.KNOBS_BY_KEY.get(key)
        if spec is None:
            return int(getattr(settings, key))
        return int(getattr(settings, spec.settings_attr))


# ---------------------------------------------------------------------------
# Phase 5h Layer D — post-claim link-liveness probe.
#
# The display URL the SPA renders is sometimes different from the URL the
# checker probes (``site.url`` vs ``site.url_probe``). Even when probes
# pass, the display URL may be 404, redirect to a login wall, or redirect
# to the site homepage with ``?missing=true``. These patterns are stable
# across hundreds of sites in the catalog so a small set of substrings
# captures the bulk of "claimed but link is dead" cases without per-site
# tuning.
# ---------------------------------------------------------------------------

# Substrings that indicate the redirected URL is not a real profile page.
# Matched case-insensitively against the final URL's path component only;
# real profile URLs embed the username so these tokens shouldn't appear
# on a healthy CLAIMED page.
_LINK_LIVENESS_DEAD_PATH_TOKENS: tuple[str, ...] = (
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/auth/",
    "/oauth/",
    "/sso/",
    "/users/sign_in",
    "/account/login",
    "/account/signin",
    "/404",
    "/not-found",
    "/notfound",
    "/page-not-found",
    "/page_not_found",
    "/error",
    "/missing",
    "/deactivated",
    "/deleted",
    "/suspended",
)
# Substrings indicating a homepage-with-flag soft-404 (``?missing=true``,
# ``?error=usernotfound``, etc.). Matched case-insensitively against the
# final URL's query string.
_LINK_LIVENESS_DEAD_QUERY_TOKENS: tuple[str, ...] = (
    "missing=true",
    "missing=1",
    "deleted=true",
    "deleted=1",
    "redirect=login",
    "error=usernotfound",
    "notfound=1",
)


def _final_url_after_redirects(resp: httpx.Response) -> str:
    """Return the final URL of a (possibly redirected) httpx response."""
    return str(resp.url)


def _link_liveness_demote_reason(resp: httpx.Response, site: Site) -> str | None:
    """Inspect a final response and return a demote reason or ``None``.

    Demote signals (any one is sufficient):

    * ``link-dead-after-claim``    — final status 404 / 410.
    * ``link-redirects-home``      — final URL host matches ``url_main``
      and the path is empty / ``/`` / ``/index`` / ``/home``, or the
      query string contains a known soft-404 token.
    * ``link-redirects-login``     — final URL path matches a known
      login / signup / not-found / deactivated token.
    """
    if resp.status_code in (404, 410):
        return f"link-dead-after-claim:{resp.status_code}"

    final_url = httpx.URL(_final_url_after_redirects(resp))
    final_path = (final_url.path or "/").lower()
    final_query = (final_url.query or b"").decode("ascii", errors="ignore").lower()

    for token in _LINK_LIVENESS_DEAD_PATH_TOKENS:
        if token in final_path:
            return f"link-redirects-login:{token}"

    if any(token in final_query for token in _LINK_LIVENESS_DEAD_QUERY_TOKENS):
        return "link-redirects-home:soft-404-flag"

    # Homepage redirect detection: if the final host matches the site's
    # canonical host *and* the final path is just the root, the user
    # redirect collapsed to the homepage (a strong "user gone" signal).
    try:
        url_main = httpx.URL(site.url_main)
    except httpx.InvalidURL:
        return None
    if final_url.host == url_main.host and final_path in (
        "",
        "/",
        "/index",
        "/home",
        "/index.html",
    ):
        return "link-redirects-home:root"

    return None


async def _link_liveness_probe(
    client: httpx.AsyncClient,
    primary: CheckResult,
    site: Site,
    *,
    username: str = "",
) -> CheckResult:
    """Phase 5h Layer D+E — HEAD/GET the display URL and demote dead links.

    The probe is a no-op when ``enable_link_liveness`` is disabled, when
    the row isn't CLAIMED, or when ``primary.url`` is empty. On any
    network error or anti-bot block (429 / 451 / blocked body) the
    probe is *fail-open*: it adds a ``link-liveness-error`` /
    ``link-liveness-blocked`` signal but doesn't demote — we'd rather
    keep a possibly-real claim than demote it because Cloudflare didn't
    let us check.

    A successful liveness check appends ``link-verified-live`` so the
    SPA can render a green badge.

    **Layer E (dual-URL check):** when ``site.url_probe`` is set and
    differs from ``site.url``, the display URL (derived from
    ``site.url``) may differ from what was probed. After the primary
    liveness check passes, Layer E probes the display URL independently
    and demotes with ``display-url-dead`` if it fails liveness even
    though the probe URL was fine.
    """
    if _admin_int("enable_link_liveness") != 1:
        return primary
    if primary.status is not Status.CLAIMED:
        return primary
    if not primary.url:
        return primary

    timeout = settings.link_liveness_timeout
    headers = {"User-Agent": _ALT_USER_AGENT}

    resp: httpx.Response | None = None
    try:
        resp = await client.head(
            primary.url,
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        )
        # Some sites reject HEAD with 405 / 501 — retry with GET.
        if resp.status_code in (405, 501):
            resp = await client.get(
                primary.url,
                headers=headers,
                follow_redirects=True,
                timeout=timeout,
            )
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        primary.signals = [*primary.signals, f"link-liveness-error:{type(e).__name__}"]
        return primary

    # Anti-bot: fail open. We don't trust the result either way; better
    # to keep the claim than demote because of a WAF.
    body_for_block_check = ""
    if resp.request.method == "GET":
        try:
            body_for_block_check = resp.text or ""
        except Exception:
            body_for_block_check = ""
    if resp.status_code in (429, 451) or (
        body_for_block_check and _is_blocked_body(body_for_block_check)
    ):
        primary.signals = [*primary.signals, f"link-liveness-blocked:{resp.status_code}"]
        return primary

    reason = _link_liveness_demote_reason(resp, site)
    if reason is not None:
        primary.signals = [*primary.signals, reason]
        primary.status = Status.UNKNOWN
        return primary

    primary.signals = [*primary.signals, "link-verified-live"]

    # Phase 5h Layer E — dual-URL check.
    #
    # When ``site.url_probe`` is set and differs from ``site.url``, the
    # display URL may differ from the URL that was probed for liveness
    # above.  Probe the display URL independently and demote with
    # ``display-url-dead`` if it fails liveness even though the probe
    # URL was fine.  This catches sites where the API endpoint is
    # healthy but the human-facing page is a 404 / login wall / homepage
    # redirect.
    if site.url_probe and site.url_probe != site.url and username:
        display_url = site.url.replace("{username}", username)
        if display_url != primary.url:
            try:
                dresp = await client.head(
                    display_url,
                    headers=headers,
                    follow_redirects=True,
                    timeout=timeout,
                )
                if dresp.status_code in (405, 501):
                    dresp = await client.get(
                        display_url,
                        headers=headers,
                        follow_redirects=True,
                        timeout=timeout,
                    )
            except (httpx.HTTPError, httpx.InvalidURL):
                return primary

            dbody = ""
            if dresp.request.method == "GET":
                try:
                    dbody = dresp.text or ""
                except Exception:
                    dbody = ""
            if dresp.status_code in (429, 451) or (dbody and _is_blocked_body(dbody)):
                return primary

            dreason = _link_liveness_demote_reason(dresp, site)
            if dreason is not None:
                primary.signals = [*primary.signals, f"display-url-dead:{dreason}"]
                primary.status = Status.UNKNOWN
                return primary

    return primary


def _apply_claimed_precision_floor(primary: CheckResult) -> CheckResult:
    """Phase 5h Layer A+B+C — demote borderline CLAIMED rows to UNKNOWN.

    Three independent floors, each runtime-tunable via the admin-settings
    table (``min_claimed_confidence`` / ``min_claimed_ml_confidence`` /
    ``require_verify_for_claim``) so an operator can dial precision up
    or down without a redeploy:

    * **Layer A — confidence floor.** A rule-based confidence below
      ``min_claimed_confidence`` (default ``60``) is treated as
      borderline; the row is demoted with signal
      ``claimed-below-confidence-floor:<n>``.
    * **Layer B — ML floor.** When the Phase-4 ONNX reranker actually
      produced a score (i.e. ``ml_confidence is not None``), a value
      below ``min_claimed_ml_confidence`` (default ``50``, on the
      0-100 scale) demotes with ``claimed-below-ml-floor:<n>``. Rows
      without an ML score are unaffected — non-username scans and
      ML-disabled deployments still flow through unchanged.
    * **Layer C — verify-required.** When ``require_verify_for_claim``
      is ``1``, only rows where the Phase-2 verify pass explicitly
      agreed (``primary.verified is True``) stay CLAIMED. Rows where
      verify was skipped (``verified is None``) or disagreed
      (``verified is False``) demote with ``claimed-not-verified``.
      Verify-disagreement already demotes via ``_verify_check`` —
      this layer additionally catches the *skipped* case so an
      operator can opt into a stricter "rule + verify both agreed"
      mode without changing per-site verify behaviour.

    The function is a no-op for non-CLAIMED rows, so it's safe to call
    unconditionally at the end of every probe path. It mutates and
    returns ``primary`` so the caller can treat it as a pipeline stage.
    """
    if primary.status is not Status.CLAIMED:
        return primary

    floor = _admin_int("min_claimed_confidence")
    if floor > 0 and primary.confidence < floor:
        primary.signals = [
            *primary.signals,
            f"claimed-below-confidence-floor:{primary.confidence}",
        ]
        primary.status = Status.UNKNOWN
        return primary

    ml_floor = _admin_int("min_claimed_ml_confidence")
    if ml_floor > 0 and primary.ml_confidence is not None and primary.ml_confidence < ml_floor:
        primary.signals = [
            *primary.signals,
            f"claimed-below-ml-floor:{primary.ml_confidence}",
        ]
        primary.status = Status.UNKNOWN
        return primary

    if _admin_int("require_verify_for_claim") == 1 and primary.verified is not True:
        primary.signals = [*primary.signals, "claimed-not-verified"]
        primary.status = Status.UNKNOWN

    return primary


class Status(StrEnum):
    CLAIMED = "claimed"
    FREE = "free"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(slots=True)
class CheckResult:
    site: str
    status: Status
    url: str
    url_main: str
    tags: list[str]
    http_status: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    confidence: int = 0
    signals: list[str] = field(default_factory=list)
    attempts: int = 1
    # Phase 2: 2-stage verification.
    # ``None`` = verify wasn't attempted (low-priority result, or verify
    # disabled). ``True`` = a second probe with a different UA/Referer
    # agreed with the primary classification. ``False`` = the second
    # probe disagreed; primary classification is downgraded to UNKNOWN.
    verified: bool | None = None
    # Phase 3: structured identity fields pulled from the page body for
    # CLAIMED hits with sufficient confidence. ``None`` = extraction
    # didn't run (status wasn't CLAIMED, body unavailable, or confidence
    # too low). ``{}`` = ran but the page yielded no recognised fields.
    extracted: dict[str, str] | None = None
    # Phase 4: supplemental ML confidence (probability that the result
    # is CLAIMED, mapped to 0-100). ``None`` means the model wasn't
    # consulted (disabled in config, model file missing, SKIPPED result,
    # or onnxruntime failure). The rule-based ``confidence`` remains
    # authoritative; ``ml_confidence`` is shown side-by-side in the SPA
    # and a diverging value adds a ``ml-disagree`` signal.
    ml_confidence: int | None = None
    # Phase 9d: structured anti-bot reason code, surfaced when the
    # CAPTCHA classifier (``app.captcha_signatures``) recognises a
    # vendor-specific challenge in the response body. ``None`` means
    # either the classifier was disabled (default) or no signature
    # matched; otherwise the value is ``"captcha_<vendor>"`` where
    # ``<vendor>`` is one of the names returned by
    # :func:`app.captcha_signatures.known_vendors` (e.g. ``recaptcha_v2``
    # / ``hcaptcha`` / ``turnstile``). The SPA renders a vendor-aware
    # badge from this string, and ``/api/admin/ml/status`` reports a
    # rolling per-vendor counter for the same vocabulary so an operator
    # can see e.g. ``"47 sites returned cloudflare turnstile this week"``
    # without scrolling through individual scan results.
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "status": self.status.value,
            "url": self.url,
            "url_main": self.url_main,
            "tags": self.tags,
            "http_status": self.http_status,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "confidence": self.confidence,
            "signals": self.signals,
            "attempts": self.attempts,
            "verified": self.verified,
            "extracted": self.extracted,
            "ml_confidence": self.ml_confidence,
            "reason": self.reason,
        }


def _matches_regex(username: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    try:
        return re.match(pattern, username) is not None
    except re.error:
        return True


# Anti-bot fingerprints that survive across operators (Cloudflare, Akamai,
# Sucuri, generic captcha). Lower-cased substring match against the body.
_BLOCK_FINGERPRINTS: tuple[str, ...] = (
    # Cloudflare
    "just a moment",
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-please-wait",
    "checking your browser",
    "challenge-platform",
    # Akamai
    "akamaized.net",
    "reference&#32;#",
    # Sucuri / Imperva / generic WAF
    "sucuri website firewall",
    "incapsula incident",
    "imperva",
    # Captcha widgets
    "g-recaptcha",
    "h-captcha",
    "data-sitekey=",
    "/recaptcha/api.js",
)


def _is_blocked_body(body: str) -> bool:
    """True iff the response body looks like an anti-bot interstitial."""
    if not body:
        return False
    low = body.lower()
    return any(fp in low for fp in _BLOCK_FINGERPRINTS)


def _classify_message(body: str, site: Site) -> tuple[Status, list[str]]:
    """Return (status, signals) — signals are the human-readable evidence
    strings used for confidence scoring."""
    signals: list[str] = []
    presense_hit = any(s in body for s in site.presense_strs) if site.presense_strs else None
    absence_hit = any(s in body for s in site.absence_strs) if site.absence_strs else None

    if presense_hit:
        signals.append("presence-match")
    if absence_hit:
        signals.append("absence-match")

    if presense_hit and not absence_hit:
        return Status.CLAIMED, signals
    if absence_hit and not presense_hit:
        return Status.FREE, signals
    if site.absence_strs and not absence_hit and not site.presense_strs:
        signals.append("absence-miss")
        return Status.CLAIMED, signals
    if site.presense_strs and not presense_hit and not site.absence_strs:
        signals.append("presence-miss")
        return Status.FREE, signals
    return Status.UNKNOWN, signals


def _classify_response(resp: httpx.Response, body: str, site: Site) -> tuple[Status, list[str]]:
    # Phase 6b: engine-aware probe — override generic classifier when the
    # site declares an engine that has a registered probe function.
    if site.engine:
        from .engine_probes import ENGINE_PROBES

        probe = ENGINE_PROBES.get(site.engine)
        if probe is not None:
            result = probe(resp, body, site)
            if result is not None:
                return result

    if site.error_strs and any(s in body for s in site.error_strs):
        return Status.ERROR, ["error-string-match"]
    if site.check_type == "status_code":
        if 200 <= resp.status_code < 300:
            return Status.CLAIMED, [f"status-{resp.status_code}"]
        if resp.status_code in (404, 410):
            return Status.FREE, [f"status-{resp.status_code}"]
        return Status.UNKNOWN, [f"status-{resp.status_code}"]
    if site.check_type == "response_url":
        target = site.url.replace("{username}", "USERNAME").rstrip("/")
        final = str(resp.url).rstrip("/")
        if "USERNAME" in target:
            prefix = target.split("USERNAME", 1)[0]
            if final.startswith(prefix.rstrip("/")):
                return Status.CLAIMED, ["response-url-prefix-match"]
            return Status.FREE, ["response-url-redirected"]
        return (
            (Status.CLAIMED, ["response-url-match"])
            if final.startswith(target)
            else (Status.FREE, ["response-url-mismatch"])
        )
    # message
    return _classify_message(body, site)


def _confidence(site: Site, status: Status, http_status: int | None, signals: list[str]) -> int:
    """Map (site type, status, signals) to a 0-100 confidence score.

    The SPA sorts results by ``status, -confidence`` so the user sees
    high-confidence claims first within each group. Phase-2 will re-probe
    any CLAIMED with ``confidence < 70`` using a different UA + referrer.
    """
    presence = "presence-match" in signals
    absence = "absence-match" in signals
    if status == Status.CLAIMED:
        # Both signals agreed → very high confidence.
        if presence and not absence:
            base = 92 if site.check_type == "message" else 88
        elif site.check_type == "message":
            base = 70
        elif site.check_type == "status_code":
            # Pure 2xx with no body parse — middle confidence; tied to the
            # site's reputation (rank).
            base = 80
        elif site.check_type == "response_url":
            base = 85
        else:
            base = 60
    elif status == Status.FREE:
        if absence and not presence:
            base = 90 if site.check_type == "message" else 85
        elif site.check_type == "message":
            base = 65
        elif http_status in (404, 410):
            base = 88
        elif site.check_type == "response_url":
            base = 82
        else:
            base = 55
    elif status == Status.BLOCKED:
        # The site is up; we just couldn't see past its WAF. Treat as
        # "we know nothing useful" but worth showing.
        base = 40
    elif status == Status.UNKNOWN:
        base = 25
    elif status == Status.ERROR:
        base = 0
    else:  # SKIPPED
        base = 100  # the skip itself is 100% confident; just not informative

    # Ranked sites are slightly more trustworthy than long-tail unranked.
    if site.alexa_rank:
        if site.alexa_rank <= 1_000:
            base = min(100, base + 4)
        elif site.alexa_rank <= 10_000:
            base = min(100, base + 2)
    return base


# Status codes that are usually transient and worth a single retry. 5xx is
# server-side flakes; 408 is a client-timeout signal from the LB.
_RETRYABLE_STATUS = frozenset({408, 502, 503, 504})


async def _do_probe(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    if method == "HEAD":
        return await client.head(url, headers=headers, follow_redirects=True)
    return await client.get(url, headers=headers, follow_redirects=True)


# Phase 2: alternate identity used by the verify probe. Different family
# (Firefox, not Chrome), different referer, different language. Picking a
# distinct identity lets us catch sites that key on the first request's
# fingerprint to decide whether to serve real content.
_ALT_USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) "
    "Gecko/20100101 Firefox/122.0 Reckora-Verify/0.2"
)
_ALT_REFERERS: tuple[str, ...] = (
    "https://duckduckgo.com/",
    "https://www.bing.com/search",
    "https://search.brave.com/",
)


def _origin(url: str) -> str:
    """Return ``scheme://host`` portion of a URL for cookie-jar bookkeeping."""
    try:
        u = httpx.URL(url)
        port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
        return f"{u.scheme}://{u.host}{port}"
    except (httpx.InvalidURL, ValueError, TypeError):
        return ""


async def _prewarm(
    client: httpx.AsyncClient,
    site: Site,
    headers: dict[str, str],
    cache: set[str],
    cache_lock: asyncio.Lock,
) -> bool:
    """Hit ``site.url_main`` once per host to seed cookies in the shared
    jar. Returns True iff a prewarm was actually performed (callers may use
    this to skip the body-fetch on the username probe if nothing changed).
    """
    origin = _origin(site.url_main)
    if not origin:
        return False
    async with cache_lock:
        if origin in cache:
            return False
        cache.add(origin)
    try:
        # GET (not HEAD) — many anti-bot middlewares only emit cookies on
        # full requests. Short timeout: prewarm shouldn't block the probe
        # for long, and a stale prewarm just degrades to the un-warmed
        # behaviour we had before.
        await client.get(
            site.url_main,
            headers=headers,
            follow_redirects=True,
            timeout=min(settings.request_timeout, 5.0),
        )
        return True
    except (httpx.HTTPError, httpx.InvalidURL):
        return False


def _should_verify(site: Site, primary: CheckResult) -> bool:
    """Decide whether a borderline CLAIMED is worth re-probing.

    Verification is the most expensive thing in the pipeline (it doubles
    the request count for the affected site), so we want to spend it only
    on results that are genuinely ambiguous. Heuristics:

    - Pure status_code CLAIMED with no body signals — these are the most
      common false-positive source (200-on-everything sites, soft-404).
    - message-classified CLAIMED with only ``presence-miss`` (no real
      presence string matched, just the absence of a "not-found" marker).
    - Anything with confidence < 75.
    """
    if primary.status is not Status.CLAIMED:
        return False
    if primary.confidence < 75:
        return True
    if site.check_type == "status_code":
        # 2xx-only classifies are inherently weak even at high score.
        return True
    return "presence-miss" in primary.signals


async def _verify_check(
    client: httpx.AsyncClient,
    site: Site,
    primary: CheckResult,
) -> CheckResult:
    """Re-probe ``site`` for ``username`` with a rotated identity and amend
    ``primary`` in place.

    On agreement, confidence is bumped and ``verified=True``.
    On disagreement, status is demoted to UNKNOWN and confidence halved.
    On verify-time error, ``verified`` stays None and a signal is added so
    the SPA can surface "verify skipped" rather than masking the result.
    """
    url = primary.url
    referer_idx = abs(hash(url)) % len(_ALT_REFERERS)
    headers = {
        "User-Agent": _ALT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-GB,en;q=0.7",
        "Referer": _ALT_REFERERS[referer_idx],
    }
    headers.update(dict(site.headers))
    method = "HEAD" if (site.head_only and site.check_type == "status_code") else "GET"
    try:
        resp = await _do_probe(client, method, url, headers)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        primary.signals = [*primary.signals, f"verify-err:{type(e).__name__}"]
        return primary

    body = "" if method == "HEAD" else (resp.text or "")
    # Verify-time block detection.
    if resp.status_code in (429, 451) or _is_blocked_body(body):
        primary.signals = [*primary.signals, "verify-blocked"]
        primary.confidence = max(0, primary.confidence - 10)
        return primary

    second_status, second_signals = _classify_response(resp, body, site)
    if second_status is Status.CLAIMED:
        primary.verified = True
        primary.signals = [*primary.signals, "verify-agree"]
        primary.confidence = min(100, primary.confidence + 6)
    else:
        primary.verified = False
        primary.signals = [
            *primary.signals,
            f"verify-disagree:{second_status.value}",
            *(f"v:{s}" for s in second_signals),
        ]
        primary.confidence = max(0, primary.confidence // 2)
        primary.status = Status.UNKNOWN
    return primary


async def check_site(
    client: httpx.AsyncClient,
    site: Site,
    username: str,
    *,
    identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    prewarm_cache: set[str] | None = None,
    prewarm_lock: asyncio.Lock | None = None,
    verify: bool = True,
    cffi_rescue: Any = None,
    extract: bool = True,
) -> CheckResult:
    # Phase 5a: ``username`` is now a generic *identifier* — it may be
    # an email / phone / GAIA / Yandex / Skype id depending on
    # ``identity_type``. The variable name is kept for backward
    # compatibility with older callers and the SSE payload schema.
    encoded = transform_identifier(site.name, identity_type, username)
    url = site.url.replace("{username}", encoded)
    if identity_type not in site.identity_types:
        return CheckResult(
            site=site.name,
            status=Status.SKIPPED,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            error=f"site does not accept {identity_type}",
            confidence=100,
            signals=["identity-mismatch"],
        )
    # ``regex_check`` is tuned for username probes (e.g. ``^[^\.]+$``);
    # bypassing it for non-username identity types avoids spurious
    # SKIPPED rows on email/phone scans where the identifier intentionally
    # contains characters the site's username regex would reject.
    if identity_type == DEFAULT_IDENTITY_TYPE and not _matches_regex(username, site.regex_check):
        return CheckResult(
            site=site.name,
            status=Status.SKIPPED,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            error="username does not match site regex",
            confidence=100,
            signals=["regex-mismatch"],
        )

    started = time.perf_counter()
    headers = {
        "User-Agent": settings.user_agent,
        # httpx negotiates these automatically when the corresponding extras
        # are installed (h2, brotli). Setting them explicitly lets us be
        # robust if a site honours only an Accept-Encoding it sees.
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Phase 9a / 9c: internal-only routing header. Consumed by
        # ``app.transport.RotatingProxyTransport._select_proxy`` (geo-
        # aware proxy selection) and
        # ``app.anti_bot_transport.AntiBotTransport._resolve_profile``
        # (per-site impersonation override) so they can look up the
        # site's overrides without re-deriving the site name from the
        # request URL. ``AntiBotTransport.handle_async_request``
        # strips the header before dispatching downstream so it is
        # never forwarded over the wire.
        "X-Reckora-Site-Name": site.name,
    }
    headers.update(dict(site.headers))

    # Phase 2: per-host cookie prewarm. Only hits each origin once per
    # scan; subsequent checks share the cookie jar via the AsyncClient.
    if site.prewarm and prewarm_cache is not None and prewarm_lock is not None:
        await _prewarm(client, site, headers, prewarm_cache, prewarm_lock)

    method = "HEAD" if (site.head_only and site.check_type == "status_code") else "GET"

    resp: httpx.Response | None = None
    attempts = 0
    for attempt in range(settings.checker_max_attempts):
        attempts = attempt + 1
        try:
            resp = await _do_probe(client, method, url, headers)
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            if attempt + 1 < settings.checker_max_attempts:
                await asyncio.sleep(settings.checker_retry_base_delay * (2**attempt))
                continue
            return CheckResult(
                site=site.name,
                status=Status.ERROR,
                url=url,
                url_main=site.url_main,
                tags=list(site.tags),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=type(e).__name__,
                attempts=attempts,
            )
        except httpx.NetworkError as e:
            return CheckResult(
                site=site.name,
                status=Status.ERROR,
                url=url,
                url_main=site.url_main,
                tags=list(site.tags),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=type(e).__name__,
                attempts=attempts,
            )
        except httpx.HTTPError as e:  # pragma: no cover - safety net
            return CheckResult(
                site=site.name,
                status=Status.ERROR,
                url=url,
                url_main=site.url_main,
                tags=list(site.tags),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=type(e).__name__,
                attempts=attempts,
            )

        if resp.status_code in _RETRYABLE_STATUS and attempt + 1 < settings.checker_max_attempts:
            await asyncio.sleep(settings.checker_retry_base_delay * (2**attempt))
            continue
        break

    assert resp is not None  # mypy: loop above either returned or assigned

    # Body fetch — for HEAD probes we have no body, which is fine for
    # status_code-only classification.
    body = ""
    if method != "HEAD":
        try:
            body = resp.text
        except Exception:  # pragma: no cover - decode safety
            body = ""

    # Anti-bot detection lives ABOVE the per-checkType classifier so it
    # short-circuits status-only classifies (which would otherwise label a
    # Cloudflare interstitial as CLAIMED on the 200).
    blocked_signals: list[str] = []
    if resp.status_code == 429:
        blocked_signals.append("status-429")
    elif resp.status_code == 451:
        blocked_signals.append("status-451")
    elif resp.status_code == 403 and site.ignore_403:
        blocked_signals.append("status-403-ignored")
    # Phase 9d: vendor-specific CAPTCHA classifier. Only consulted when
    # the ``enable_captcha_classifier`` admin knob is on; otherwise the
    # generic ``_is_blocked_body`` fingerprint scan is the only body
    # check. When the classifier is enabled and matches, the BLOCKED
    # branch fires even if ``_is_blocked_body`` would not have — that's
    # how new vendors (Datadome / Cloudflare Turnstile / FunCaptcha)
    # not present in ``_BLOCK_FINGERPRINTS`` get a structured reason.
    captcha_vendor: str | None = None
    if body:
        if _is_blocked_body(body):
            blocked_signals.append("body-fingerprint-block")
        if _admin_int("enable_captcha_classifier"):
            from . import captcha_signatures as _captcha

            captcha_vendor = _captcha.classify(body)
            if captcha_vendor is not None and "body-fingerprint-block" not in blocked_signals:
                blocked_signals.append("body-fingerprint-block")
    if blocked_signals:
        blocked_reason: str | None = None
        if captcha_vendor is not None:
            from . import captcha_signatures as _captcha

            # Vendor-specific reason ALWAYS wins over the generic
            # ``body-fingerprint-block`` signal when both fire — the
            # SPA renders a vendor-aware badge from this string.
            blocked_reason = f"captcha_{captcha_vendor}"
            _captcha.record_vendor(captcha_vendor)
        blocked_result = CheckResult(
            site=site.name,
            status=Status.BLOCKED,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            http_status=resp.status_code,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            confidence=_confidence(site, Status.BLOCKED, resp.status_code, blocked_signals),
            signals=blocked_signals,
            attempts=attempts,
            reason=blocked_reason,
        )
        # Phase 2.2: rescue with curl-cffi (Chrome TLS impersonation).
        # We only call rescue on real BLOCKED, never on the 403-ignored
        # case (those sites *expect* 403 for missing accounts).
        if cffi_rescue is not None and "status-403-ignored" not in blocked_signals:
            rescued = await cffi_rescue.rescue(site, username)
            if rescued is not None and rescued.status is not Status.BLOCKED:
                # Bring across the original elapsed ms so latency is the
                # combined cost of both probes (the user sees real e2e time).
                rescued.elapsed_ms = int((time.perf_counter() - started) * 1000)
                rescued.attempts = attempts
                return rescued
        return blocked_result

    status, signals = _classify_response(resp, body, site)
    # Treat upstream ratelimit / blocks as unknown rather than free if we
    # somehow missed them above.
    if resp.status_code in (401, 403, 429, 451) and status == Status.FREE:
        status = Status.UNKNOWN
        signals = [*signals, f"status-{resp.status_code}-block-overrides-free"]
    if site.ignore_403 and resp.status_code == 403 and status == Status.FREE:
        status = Status.UNKNOWN
        signals = [*signals, "site-ignores-403"]

    primary = CheckResult(
        site=site.name,
        status=status,
        url=url,
        url_main=site.url_main,
        tags=list(site.tags),
        http_status=resp.status_code,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        confidence=_confidence(site, status, resp.status_code, signals),
        signals=signals,
        attempts=attempts,
    )
    if verify and _should_verify(site, primary):
        primary = await _verify_check(client, site, primary)
        # Verify time included so the caller sees the true latency.
        primary.elapsed_ms = int((time.perf_counter() - started) * 1000)
    # Phase 3: extract identity fields from the body of confident CLAIMED
    # hits. The cost is regex-only (no extra HTTP), so the only reason to
    # gate on confidence is to avoid surfacing identifiers from probable
    # false positives.
    if (
        extract
        and primary.status is Status.CLAIMED
        and primary.confidence >= _extract_min_confidence()
        and body
    ):
        from .extractor import extract_identifiers

        try:
            primary.extracted = extract_identifiers(body, primary.url)
        except Exception:  # pragma: no cover - extractor must never break a scan
            primary.extracted = {}
        # Phase 7a: opt-in avatar perceptual-hash capture. Only runs
        # when the extractor pulled an ``avatar_url`` out of the page
        # body — the open web doesn't have a stable convention for
        # profile-image URLs, so when the extractor missed one we'd
        # be guessing. Stored on the same ``extracted`` dict so it
        # rides the existing serialization path through to
        # ``CheckResult.to_dict`` and ``record_scan``.
        if _admin_int("enable_image_phash") and primary.extracted:
            avatar_url = primary.extracted.get("avatar_url")
            if avatar_url:
                from .imagephash import fetch_phash

                phash = await fetch_phash(
                    client,
                    avatar_url,
                    max_bytes=settings.image_phash_max_bytes,
                    timeout=settings.image_phash_timeout,
                )
                if phash:
                    primary.extracted["avatar_phash"] = phash
    # Phase 4: ML confidence. Skipped when the rule-based classifier
    # opted out (SKIPPED) or when extract+verify already concluded the
    # answer doesn't matter. We score every other status because the
    # SPA needs a comparable second-opinion number even on UNKNOWN /
    # BLOCKED rows.
    #
    # Phase 5a: the ML model is trained on username probes (regex-based
    # features assume a username-shaped identifier). Score only username
    # scans — non-username identity types leave ``ml_confidence`` at None
    # so the SPA renders no second-opinion badge for those rows.
    score_ml = (
        primary.status is not Status.SKIPPED
        and settings.enable_ml_confidence
        and identity_type == DEFAULT_IDENTITY_TYPE
    )
    if score_ml:
        from . import ml as _ml

        try:
            primary.ml_confidence = _ml.score(
                site,
                http_status=primary.http_status,
                body=body,
                elapsed_ms=primary.elapsed_ms,
            )
        except Exception:  # pragma: no cover - ml must never break a scan
            primary.ml_confidence = None
        if primary.ml_confidence is not None:
            ml = primary.ml_confidence
            rule = primary.confidence
            disagree_threshold = settings.ml_disagree_threshold
            if primary.status is Status.CLAIMED and rule >= 70 and ml <= rule - disagree_threshold:
                primary.signals = [*primary.signals, f"ml-disagree:claimed-{ml}"]
            elif (
                primary.status is Status.FREE
                and rule >= 70
                and ml >= rule  # ml predicts CLAIMED while rules say FREE
                and (ml - (100 - rule)) >= disagree_threshold
            ):
                primary.signals = [*primary.signals, f"ml-disagree:free-{ml}"]
    # Phase 5h Layer A+B+C — claimed precision floor. Applied last so it
    # sees the verify-adjusted confidence and the ML reranker's score;
    # demotes borderline hits to UNKNOWN before they leave the checker.
    primary = _apply_claimed_precision_floor(primary)
    # Phase 5h Layer D — link-liveness probe. Only runs when the row is
    # still CLAIMED after the precision floor (the floor's a cheap
    # short-circuit).
    primary = await _link_liveness_probe(client, primary, site, username=encoded)
    return primary


async def stream_checks(
    sites: list[Site],
    username: str,
    *,
    identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    client: httpx.AsyncClient | None = None,
    verify: bool = True,
    extract: bool = True,
) -> AsyncIterator[CheckResult]:
    """Run checks concurrently and yield each result as it arrives.

    ``identity_type`` (Phase 5a) selects which kind of identifier
    ``username`` is — defaults to ``"username"`` for backwards compat
    with Phase 1-4 callers. Per-site probe behaviour is the same; the
    checker just routes the value through ``transform_identifier``
    before substitution and skips the username-shaped ``regex_check``
    on non-username scans.

    ``verify`` toggles the Phase 2 second-stage probe for borderline
    CLAIMED results (default on; ``False`` for tests / latency-sensitive
    callers that want raw first-pass classification).

    ``extract`` toggles the Phase 3 identity-extraction step on confident
    CLAIMED hits (default on; ``False`` keeps ``CheckResult.extracted``
    as ``None`` and skips the regex pass entirely).

    Phase 8a: when ``settings.use_celery`` is set the implementation
    transparently dispatches to ``stream_checks_via_celery`` so the
    SSE pipeline does not need to know which fan-out it got. Default
    (``use_celery=0``) keeps the in-process asyncio path below.
    """
    if settings.use_celery:
        async for r in stream_checks_via_celery(
            sites,
            username,
            identity_type=identity_type,
            verify=verify,
            extract=extract,
        ):
            yield r
        return
    owned_client = client is None
    if owned_client:
        # http2=True saves ~1 RTT per host on warm pools; many CMS-hosted
        # sites support it. The h2 pkg is pulled in via httpx[http2].
        # Phase 8b: routes through the proxy rotator + per-host bucket
        # when their knobs are on; falls back to direct egress
        # otherwise, so the no-feature-flag default is unchanged.
        from . import transport as transport_mod

        client = transport_mod.build_async_client(
            follow_redirects=False,
        )
    assert client is not None

    sem = asyncio.Semaphore(settings.total_concurrency)
    queue: asyncio.Queue[CheckResult | None] = asyncio.Queue()
    # Phase 2: per-host prewarm bookkeeping. One set per scan so cookies
    # don't bleed between scans (each scan gets a fresh AsyncClient when
    # owned_client is True; the cache lives only as long as the iterator).
    prewarm_cache: set[str] = set()
    prewarm_lock = asyncio.Lock()

    # Phase 2.2: lazy curl-cffi session for BLOCKED-site rescue. Imported
    # inline to avoid forcing the libcurl-impersonate native dep on
    # callers that disable the rescue path.
    cffi_rescue: Any = None
    if settings.enable_curl_cffi:
        from .checker_cffi import CffiRescue

        cffi_rescue = CffiRescue()

    async def worker(site: Site) -> None:
        async with sem:
            try:
                result = await check_site(
                    client,
                    site,
                    username,
                    identity_type=identity_type,
                    prewarm_cache=prewarm_cache,
                    prewarm_lock=prewarm_lock,
                    verify=verify,
                    cffi_rescue=cffi_rescue,
                    extract=extract,
                )
            except Exception as e:
                result = CheckResult(
                    site=site.name,
                    status=Status.ERROR,
                    url=site.url.replace(
                        "{username}",
                        transform_identifier(site.name, identity_type, username),
                    ),
                    url_main=site.url_main,
                    tags=list(site.tags),
                    error=type(e).__name__,
                )
            await queue.put(result)

    tasks = [asyncio.create_task(worker(s)) for s in sites]

    async def waiter() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    waiter_task = asyncio.create_task(waiter())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        waiter_task.cancel()
        for t in tasks:
            t.cancel()
        if cffi_rescue is not None:
            await cffi_rescue.aclose()
        if owned_client:
            await client.aclose()


async def stream_checks_via_celery(
    sites: list[Site],
    username: str,
    *,
    identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    verify: bool = True,
    extract: bool = True,
) -> AsyncIterator[CheckResult]:
    """Phase 8a: distributed fan-out via Celery + Redis.

    Submits one ``check_site_task`` per site, then polls each
    ``AsyncResult`` from the API process and yields completed results
    as they arrive. ``settings.celery_poll_interval`` controls the poll
    cadence; ``settings.celery_task_timeout`` is honoured worker-side
    via the per-task soft / hard timeouts on ``celery_app``.

    On task FAILURE / timeout the collector synthesises an ERROR
    ``CheckResult`` so the SSE stream stays consistent with the
    in-process path; the caller cannot tell which fan-out produced a
    given row beyond the (intentionally unobservable) latency
    distribution.
    """

    if not sites:
        return

    # Local import — celery is only required when use_celery=1.
    from .tasks import check_site_task

    async_results = [
        check_site_task.delay(
            s.name,
            username,
            identity_type,
            verify=verify,
            extract=extract,
        )
        for s in sites
    ]
    pending: dict[str, Any] = {s.name: ar for s, ar in zip(sites, async_results, strict=True)}
    site_by_name = {s.name: s for s in sites}
    poll = max(0.005, float(settings.celery_poll_interval))

    while pending:
        done_now: list[str] = []
        for name, ar in pending.items():
            if ar.ready():
                done_now.append(name)
        if not done_now:
            await asyncio.sleep(poll)
            continue
        for name in done_now:
            ar = pending.pop(name)
            site = site_by_name[name]
            try:
                payload = ar.get(timeout=settings.celery_task_timeout, propagate=True)
                result = check_result_from_dict(payload)
            except Exception as e:
                result = CheckResult(
                    site=site.name,
                    status=Status.ERROR,
                    url=site.url.replace(
                        "{username}",
                        transform_identifier(site.name, identity_type, username),
                    ),
                    url_main=site.url_main,
                    tags=list(site.tags),
                    error=type(e).__name__,
                )
            yield result
