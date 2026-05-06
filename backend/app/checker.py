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
from .sites import Site


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
        }


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def is_valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


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
    prewarm_cache: set[str] | None = None,
    prewarm_lock: asyncio.Lock | None = None,
    verify: bool = True,
    cffi_rescue: Any = None,
    extract: bool = True,
) -> CheckResult:
    url = site.url.replace("{username}", username)
    if not _matches_regex(username, site.regex_check):
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
    if body and _is_blocked_body(body):
        blocked_signals.append("body-fingerprint-block")
    if blocked_signals:
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
        )
        # Phase 2.2: rescue with curl-cffi (Chrome TLS impersonation).
        # We only call rescue on real BLOCKED, never on the 403-ignored
        # case (those sites *expect* 403 for missing accounts).
        if (
            cffi_rescue is not None
            and "status-403-ignored" not in blocked_signals
        ):
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
        and primary.confidence >= settings.extract_min_confidence
        and body
    ):
        from .extractor import extract_identifiers

        try:
            primary.extracted = extract_identifiers(body, primary.url)
        except Exception:  # pragma: no cover - extractor must never break a scan
            primary.extracted = {}
    # Phase 4: ML confidence. Skipped when the rule-based classifier
    # opted out (SKIPPED) or when extract+verify already concluded the
    # answer doesn't matter. We score every other status because the
    # SPA needs a comparable second-opinion number even on UNKNOWN /
    # BLOCKED rows.
    if primary.status is not Status.SKIPPED and settings.enable_ml_confidence:
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
            if (
                primary.status is Status.CLAIMED
                and rule >= 70
                and ml <= rule - disagree_threshold
            ):
                primary.signals = [*primary.signals, f"ml-disagree:claimed-{ml}"]
            elif (
                primary.status is Status.FREE
                and rule >= 70
                and ml >= rule  # ml predicts CLAIMED while rules say FREE
                and (ml - (100 - rule)) >= disagree_threshold
            ):
                primary.signals = [*primary.signals, f"ml-disagree:free-{ml}"]
    return primary


async def stream_checks(
    sites: list[Site],
    username: str,
    *,
    client: httpx.AsyncClient | None = None,
    verify: bool = True,
    extract: bool = True,
) -> AsyncIterator[CheckResult]:
    """Run checks concurrently and yield each result as it arrives.

    ``verify`` toggles the Phase 2 second-stage probe for borderline
    CLAIMED results (default on; ``False`` for tests / latency-sensitive
    callers that want raw first-pass classification).

    ``extract`` toggles the Phase 3 identity-extraction step on confident
    CLAIMED hits (default on; ``False`` keeps ``CheckResult.extracted``
    as ``None`` and skips the regex pass entirely).
    """
    owned_client = client is None
    if owned_client:
        # http2=True saves ~1 RTT per host on warm pools; many CMS-hosted
        # sites support it. The h2 pkg is pulled in via httpx[http2].
        client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            http2=True,
            limits=httpx.Limits(
                max_connections=settings.total_concurrency,
                max_keepalive_connections=settings.total_concurrency,
            ),
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
                    url=site.url.replace("{username}", username),
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
