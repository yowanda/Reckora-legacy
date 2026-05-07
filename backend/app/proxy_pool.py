"""Phase 8b: multi-proxy egress pool with health-aware selection.

Phase 5e's single ``proxy.proxy_url_or_none()`` is a great fit when the
operator has exactly one egress IP — the typical "OSINT through Tor"
setup. It falls over once the catalog crosses ~1k sites and the same
exit IP starts tripping per-IP rate limits on popular hosts (Reddit,
GitHub, Cloudflare-fronted forums) within a single scan.

Phase 8b adds a *pool* of proxy URLs the runtime can rotate through, so
no single egress IP carries the whole scan's load. The pool is
configured via the env var ``RECKORA_PROXY_POOL_URLS`` (comma-separated
URLs — same schemes httpx accepts: ``socks5://``, ``socks5h://``,
``http://``, ``https://``). The env stays env-only because each URL
may carry credentials; the runtime toggle lives in the admin
``enable_proxy_rotator`` knob so an operator can flip the pool on and
off without restarting.

Selection strategy
------------------
Round-robin with a short *health cooldown*: when the caller marks a
URL as failed, the pool skips it for ``proxy_health_cooldown_sec``
seconds before considering it again. If every URL is in cooldown the
pool returns ``None`` (direct egress) — this is the fail-open contract
for a degraded proxy fleet, since refusing every request would be
worse than briefly leaking the API process's IP. Operators who need
fail-closed semantics should run a circuit breaker upstream of
Reckora.

The pool intentionally has no "least recently used" or weighted
strategy: round-robin is dead-simple, the cooldown handles the worst
failure mode (a single proxy is consistently broken), and anything
fancier means a knob the operator has to tune. We can always lift this
to a strategy enum later.

Backward compatibility
----------------------
When ``enable_proxy_rotator=0`` (the default), the pool is dormant and
all callers fall back to ``proxy.proxy_url_or_none()`` which keeps the
Phase 5e single-proxy semantics intact. Existing deployments — and the
HF Space single container — see no behavioural change until an
operator explicitly turns the knob on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import cycle

from . import proxy_reputation
from .config import settings

# ---------------------------------------------------------------------------
# Phase 9c: region tag parsing.
#
# The Phase 8b pool URL list is a flat ``","``-separated string of httpx-
# compatible URLs. Phase 9c extends the parser to optionally accept a
# leading ``"<region>:"`` tag on each entry — e.g.
# ``"us-east:socks5://a.example,ru:socks5://b.example,socks5://c.example"``
# parses to three entries with regions ``"us-east"``, ``"ru"``, and
# ``"default"`` (the bare URL keeps the legacy single-region semantics so
# operators upgrading from 8b see no behaviour change). The parser tells
# a region tag apart from a URL scheme by enforcing a small allowlist of
# accepted httpx schemes — anything else before the first ``":"`` is
# treated as a region label.
# ---------------------------------------------------------------------------

_DEFAULT_REGION: str = "default"
_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


@dataclass(slots=True)
class _ProxyState:
    """Per-URL health record stored alongside the pool entry."""

    url: str
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    region: str = _DEFAULT_REGION


@dataclass(slots=True)
class ProxyPool:
    """Round-robin pool of proxy URLs with a health-cooldown guard.

    Single instance is exposed as a module-level singleton via
    :func:`get_pool`. Construction parses the env list once; subsequent
    behaviour reads the live admin knob through
    :func:`_enabled_knob` so the toggle is hot-swappable.

    The class is intentionally thread-safe (``acquire`` and
    ``mark_failed`` take the same lock) so it can sit behind both the
    asyncio event loop and Celery's worker thread pool without the
    caller having to think about it.
    """

    urls: tuple[str, ...]
    regions: dict[str, str] = field(default_factory=dict)
    _state: dict[str, _ProxyState] = field(default_factory=dict)
    _cursor: Iterator[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Stable iteration order; ``cycle`` over ``self.urls`` keeps
        # round-robin deterministic across calls.
        self._state = {
            u: _ProxyState(url=u, region=self.regions.get(u, _DEFAULT_REGION)) for u in self.urls
        }
        self._cursor = cycle(self.urls) if self.urls else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        *,
        now: float | None = None,
        preferred_region: str | None = None,
    ) -> str | None:
        """Return the next eligible proxy URL, or ``None`` for direct.

        ``None`` is returned when the pool is empty *or* every URL is
        currently in cooldown (fail-open, see module docstring).

        Phase 9c: when ``preferred_region`` is set the pool walks the
        cursor in three passes:

        1. **Region match** — first healthy URL whose region equals
           ``preferred_region`` (case-insensitive).
        2. **Default region** — first healthy URL tagged
           ``"default"`` (i.e. URLs without a region prefix in the
           env, which is the Phase 8b behaviour).
        3. **Any healthy URL** — last-resort sweep so a single-region
           pool still works.

        Each pass advances the same round-robin cursor so two passes
        never re-pick the same URL within one call. When
        ``preferred_region=None`` the method collapses to the Phase 8b
        single-pass round-robin (preserves bit-for-bit behaviour when
        ``enable_geo_aware_proxy=0``).

        Phase 9g: when the ``enable_proxy_reputation`` knob is on the
        round-robin pick is replaced with a stride-scheduled weighted
        pick (see :mod:`backend.app.proxy_reputation`). The weighted
        path is layered *inside* the same three-pass region tier, so
        a region preference still wins over reputation when both are
        on. The default-off knob branch (``enable_proxy_reputation=0``)
        falls through to the Phase 8b / 9c code below byte-for-byte.
        """
        if not self.urls or self._cursor is None:
            return None
        ts = now if now is not None else time.monotonic()
        # Phase 9g — reputation-aware pick. Gated by the live admin
        # knob so the pre-9g code path stays byte-for-byte identical
        # when the operator hasn't opted in.
        if proxy_reputation.is_enabled():
            return self._acquire_weighted(now=ts, preferred_region=preferred_region)
        # Phase 8b single-pass fast path — preferred region not
        # supplied, so don't pay the three-pass cost.
        if preferred_region is None:
            with self._lock:
                for _ in range(len(self.urls)):
                    url = next(self._cursor)
                    state = self._state[url]
                    if state.cooldown_until <= ts:
                        return url
            return None
        target = preferred_region.strip().lower()
        with self._lock:
            # Pass 1: region match.
            for _ in range(len(self.urls)):
                url = next(self._cursor)
                state = self._state[url]
                if state.cooldown_until <= ts and state.region.lower() == target:
                    return url
            # Pass 2: default region (only if the operator hasn't
            # already asked for ``"default"`` — that pass is identical
            # to pass 1).
            if target != _DEFAULT_REGION:
                for _ in range(len(self.urls)):
                    url = next(self._cursor)
                    state = self._state[url]
                    if state.cooldown_until <= ts and state.region.lower() == _DEFAULT_REGION:
                        return url
            # Pass 3: any healthy URL (fail-open last resort).
            for _ in range(len(self.urls)):
                url = next(self._cursor)
                state = self._state[url]
                if state.cooldown_until <= ts:
                    return url
        return None

    def _acquire_weighted(
        self,
        *,
        now: float,
        preferred_region: str | None,
    ) -> str | None:
        """Phase 9g: stride-scheduled weighted pick across region tiers.

        Mirrors :meth:`acquire`'s three-pass region structure exactly
        but replaces the cursor advance with a delegated
        :meth:`backend.app.proxy_reputation.ProxyReputation.pick` so
        higher-reputation URLs are picked more often within each tier.
        Tier fallback semantics are preserved — a region miss still
        falls through to the default tier and then to any healthy URL.
        """

        target = preferred_region.strip().lower() if preferred_region else None
        # Build the per-tier candidate list under the pool lock so the
        # cooldown read is consistent with whatever ``mark_failed``
        # write is racing with us. Lock is released before delegating
        # to the reputation tracker so its lock is not nested under
        # ours.
        region_match: list[str] = []
        default_tier: list[str] = []
        any_healthy: list[str] = []
        with self._lock:
            for url in self.urls:
                state = self._state[url]
                if state.cooldown_until > now:
                    continue
                any_healthy.append(url)
                region = state.region.lower()
                if target is not None and region == target:
                    region_match.append(url)
                if region == _DEFAULT_REGION:
                    default_tier.append(url)
        rep = proxy_reputation.get_reputation()
        # Pass 1: region match (skipped when no preferred region).
        if target is not None:
            chosen = rep.pick(region_match, now=now)
            if chosen is not None:
                return chosen
            # Pass 2: default region (only when target itself is not
            # already ``"default"`` — that case collapses pass 1 into 2
            # and we'd just retry the same candidate set).
            if target != _DEFAULT_REGION:
                chosen = rep.pick(default_tier, now=now)
                if chosen is not None:
                    return chosen
        # Pass 3: any healthy URL (fail-open). Also the single-pass
        # path when ``preferred_region`` is None — matches the Phase
        # 8b "no region preference" contract.
        return rep.pick(any_healthy, now=now)

    def mark_failed(self, url: str | None, *, now: float | None = None) -> None:
        """Record a failure against ``url`` and put it in cooldown.

        ``url=None`` (direct egress) is a no-op; the pool only tracks
        URLs it could have selected. The cooldown duration comes from
        the live ``proxy_health_cooldown_sec`` setting so an admin can
        widen the dead-window without restarting.

        Phase 9g: when ``enable_proxy_reputation`` is on the failure
        also ticks the URL's reputation counter so the next
        :meth:`acquire` weighted pick reflects the new ratio. The knob
        check is on the live admin gate so the pre-9g hot path stays
        free of the extra counter mutation when the operator has not
        opted in.
        """
        if url is None:
            return
        ts = now if now is not None else time.monotonic()
        cooldown = max(1.0, float(settings.proxy_health_cooldown_sec))
        with self._lock:
            state = self._state.get(url)
            if state is None:
                return
            state.consecutive_failures += 1
            state.cooldown_until = ts + cooldown
        if proxy_reputation.is_enabled():
            proxy_reputation.get_reputation().record_failure(url, now=ts)

    def mark_success(self, url: str | None) -> None:
        """Reset the failure counter for ``url``.

        Cooldown is *not* shortened on success — once a URL is in the
        penalty box it serves out the full ``proxy_health_cooldown_sec``
        timer regardless of who else succeeded in the meantime.

        Phase 9g: when ``enable_proxy_reputation`` is on the success
        also ticks the URL's reputation counter (same gating contract
        as :meth:`mark_failed`).
        """
        if url is None:
            return
        with self._lock:
            state = self._state.get(url)
            if state is not None:
                state.consecutive_failures = 0
        if proxy_reputation.is_enabled():
            proxy_reputation.get_reputation().record_success(url)

    def health(self) -> list[dict[str, object]]:
        """Return a JSON-friendly snapshot of every URL's health.

        Used by ``/api/health`` so an operator can spot a wedged proxy
        without tailing logs. Cooldown is reported as the number of
        seconds remaining (``0`` for healthy URLs).
        """
        ts = time.monotonic()
        out: list[dict[str, object]] = []
        with self._lock:
            for url in self.urls:
                state = self._state[url]
                remaining = max(0.0, state.cooldown_until - ts)
                out.append(
                    {
                        "url_scheme": url.split("://", 1)[0] if "://" in url else "",
                        "in_cooldown": remaining > 0,
                        "cooldown_remaining_sec": round(remaining, 2),
                        "consecutive_failures": state.consecutive_failures,
                        "region": state.region,
                    }
                )
        return out

    def geo_distribution(self) -> dict[str, int]:
        """Return ``{region: count}`` over the configured URLs.

        Surfaced on ``/api/health.proxy_pool.geo_distribution`` so a
        dashboard can quickly verify the pool covers the regions the
        operator expects (no proxies tagged ``"ru"`` means a Yandex
        probe is going to fall back to the default region or direct
        egress, which is useful to see at a glance).
        """
        out: dict[str, int] = {}
        with self._lock:
            for state in self._state.values():
                out[state.region] = out.get(state.region, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Module-level singleton + helpers.
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: ProxyPool | None = None

# Phase 9c: cached site-name → preferred-region map. Built lazily
# from ``site_overrides.list_overrides()`` and refreshed every
# ``_REGION_CACHE_TTL_SEC`` seconds so a hot-path scan doesn't hit
# SQLite per request. Mutations to site overrides invalidate the
# cache via :func:`reset_pool_for_tests` (tests) and via the
# ``reload_catalog`` hook (production — see
# ``main.admin_upsert_site_override``).
_REGION_CACHE_TTL_SEC: float = 30.0
_region_cache_lock = threading.Lock()
_region_cache: dict[str, str] = {}
_region_cache_built_at: float = 0.0


def _parse_pool_entry(entry: str) -> tuple[str, str]:
    """Return ``(url, region)`` for one ``RECKORA_PROXY_POOL_URLS`` entry.

    Accepts both ``"us-east:socks5://a.example"`` (region-tagged) and
    ``"socks5://a.example"`` (bare URL → ``"default"`` region). The
    region/scheme split is decided by checking the first segment
    against :data:`_URL_SCHEMES` — anything else before the first
    ``":"`` is a region tag.
    """
    head, sep, tail = entry.partition(":")
    if not sep:
        return entry, _DEFAULT_REGION
    if head.lower() in _URL_SCHEMES:
        return entry, _DEFAULT_REGION
    return tail.lstrip(), head.strip().lower() or _DEFAULT_REGION


def _parse_pool_urls(raw: str) -> tuple[tuple[str, ...], dict[str, str]]:
    """Split a ``RECKORA_PROXY_POOL_URLS`` value into URLs + regions.

    Accepts comma-separated URLs with optional whitespace; empty parts
    and lone ``,`` are skipped so an operator can leave a trailing
    comma in their env file without breaking the pool. Each entry is
    parsed via :func:`_parse_pool_entry` so a leading ``region:``
    prefix is recognised. The returned ``regions`` dict maps
    ``url -> region`` for the URLs in the tuple.
    """
    parts = [p.strip() for p in (raw or "").split(",")]
    urls: list[str] = []
    regions: dict[str, str] = {}
    for part in parts:
        if not part:
            continue
        url, region = _parse_pool_entry(part)
        if not url:
            continue
        urls.append(url)
        regions[url] = region
    return tuple(urls), regions


def _build_pool() -> ProxyPool:
    urls, regions = _parse_pool_urls(settings.proxy_pool_urls)
    return ProxyPool(urls=urls, regions=regions)


def get_pool() -> ProxyPool:
    """Return the lazy module-level pool singleton."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def reset_pool_for_tests() -> None:
    """Drop the singleton so tests can rebuild it after env edits.

    Production code never calls this; it exists purely so unit tests
    that patch ``settings.proxy_pool_urls`` get a fresh pool on every
    test instead of one another's leftover state.
    """
    global _pool, _region_cache, _region_cache_built_at
    with _pool_lock:
        _pool = None
    with _region_cache_lock:
        _region_cache = {}
        _region_cache_built_at = 0.0


def _enabled_knob() -> int:
    """Read the ``enable_proxy_rotator`` admin knob through the cache."""
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_proxy_rotator")
    except Exception:
        return int(settings.enable_proxy_rotator)


def is_enabled() -> bool:
    """``True`` when the rotator is on **and** at least one URL is
    configured. Callers use this to gate transport selection."""
    return bool(get_pool().urls) and _enabled_knob() == 1


def _geo_aware_knob() -> int:
    """Phase 9c: read the ``enable_geo_aware_proxy`` knob."""
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_geo_aware_proxy")
    except Exception:
        return int(settings.enable_geo_aware_proxy)


def is_geo_aware_enabled() -> bool:
    """``True`` when geo-aware selection is on **and** the rotator is
    active. Callers use this to gate the ``preferred_region`` lookup.
    """
    return is_enabled() and _geo_aware_knob() == 1


def preferred_region_for_site(site_name: str | None) -> str | None:
    """Look up the ``preferredRegion`` site override for ``site_name``.

    The lookup walks ``site_overrides.list_overrides()`` once per
    :data:`_REGION_CACHE_TTL_SEC` (30s default) and caches the
    site-name → region map in-process. Returns ``None`` when the
    knob is off, ``site_name`` is empty, or the site has no override.

    Phase 9c never hits the SQLite override table on the request hot
    path; the cache costs one DB scan every 30s no matter how many
    sites are scanned.
    """
    if not site_name:
        return None
    global _region_cache, _region_cache_built_at
    now = time.monotonic()
    with _region_cache_lock:
        if now - _region_cache_built_at > _REGION_CACHE_TTL_SEC:
            try:
                from . import site_overrides as site_overrides_mod

                fresh: dict[str, str] = {}
                for o in site_overrides_mod.list_overrides():
                    region = o.patch.get("preferredRegion")
                    if isinstance(region, str) and region.strip():
                        fresh[o.name.lower()] = region.strip().lower()
                _region_cache = fresh
                _region_cache_built_at = now
            except Exception:
                # Fall through to the previous cache snapshot — being
                # briefly stale is better than failing every probe
                # because the override table is locked.
                _region_cache_built_at = now
        return _region_cache.get(site_name.lower())


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health`` and the SPA."""
    pool = get_pool()
    return {
        "configured": len(pool.urls),
        "enabled": _enabled_knob() == 1,
        "active": is_enabled(),
        "members": pool.health(),
        "geo_aware_enabled": _geo_aware_knob() == 1,
        "geo_aware_active": is_geo_aware_enabled(),
        "geo_distribution": pool.geo_distribution(),
    }
