"""Phase 8b + Phase 9b: per-host rate limiter.

Pairs with :mod:`app.proxy_pool` to make a Reckora scan that fans out
to thousands of sites a *good citizen* on the public internet — even
when the operator has the proxy rotator turned off. The two pillars:

* The proxy pool spreads load across multiple egress IPs so no single
  IP hits a global rate limit.
* This module spreads load across time on a *per-host* basis so even a
  single egress IP doesn't pound the same site faster than its public
  rate limit.

Design choices
--------------
* **Token bucket** (Phase 8b in-memory backend), not sliding window.
  A bucket lets a quiet host absorb a 60-request burst at the start
  of a scan if it had been idle for a minute, which is exactly what
  we want — it preserves scan throughput while still enforcing a
  steady-state rate. A sliding window would drag every burst out to
  the slow steady rate.

* **Capacity == refill rate**. We pick the simplest defensible
  capacity: one full minute of traffic. Operators tune the rate
  through the ``host_rate_per_min`` admin knob; the bucket capacity
  follows automatically.

* **Pure Python**. The acquire path is a tight loop over ``time.monotonic``
  + ``asyncio.sleep`` so it adds <1µs of overhead per call when the
  bucket has tokens available. No background refill task — refill is
  computed on demand from the elapsed time since the last
  observation.

* **Disabled-mode fast path**. When ``enable_host_rate_limit=0`` the
  ``acquire`` coroutine is a no-op. The check is a single int read
  through the admin-settings cache so the gate cost is negligible
  even on a hot per-site code path.

* **Redis backend** (Phase 9b — opt-in). When
  ``host_rate_limit_use_redis=1`` the module dispatches to
  :mod:`app.host_rate_limit_redis`, a sliding-window counter that
  uses one atomic Lua script for the ``INCR + EXPIRE + check``
  round-trip so N Celery workers (Phase 8a) share *one* bucket per
  host. A Redis transport error falls *open* to the in-memory
  bucket so an outage stays fail-open rather than fail-closed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from .host_rate_limit_redis import RedisHostRateLimiter


@dataclass(slots=True)
class _Bucket:
    """Per-host token-bucket state.

    ``tokens`` is the float number of tokens currently available;
    ``last_refill`` is the monotonic timestamp at which we last
    advanced the bucket. Refill is lazy — we add
    ``(now - last_refill) * refill_rate_per_sec`` tokens on every
    acquire, capped at ``capacity``.
    """

    capacity: float
    refill_rate_per_sec: float
    tokens: float = 0.0
    last_refill: float = 0.0


class HostRateLimiter:
    """Per-host token-bucket limiter.

    The class is intentionally async-first; ``acquire`` is the only
    public coroutine and returns when a token has been spent. A
    ``threading.Lock`` guards the bucket dict so it's safe to share
    one limiter across the API process and a Celery worker thread (the
    Phase 8a fan-out path).

    Construction parameters default to the live ``settings`` values so
    the typical caller just instantiates ``HostRateLimiter()``. Tests
    pass explicit ``rate_per_min`` / ``capacity`` so the bucket math
    is deterministic.
    """

    def __init__(
        self,
        *,
        rate_per_min: int | None = None,
        capacity: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._explicit_rate = rate_per_min
        self._explicit_capacity = capacity

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, host: str) -> None:
        """Block until one slot is available for ``host``.

        Hosts are normalised to lowercase so ``Reddit.com`` and
        ``reddit.com`` share a bucket. An empty / falsy host (e.g. a
        ``file://`` URL passed in by accident) is treated as a no-op.

        When ``host_rate_limit_use_redis=1`` (Phase 9b), the request
        is dispatched to the Redis-backed sliding-window limiter so
        N Celery workers share a single bucket per host. A Redis
        transport error falls *open* to the in-memory token bucket
        below — being briefly too-fast on rate is much better than
        fail-closing every probe during a Redis outage.
        """
        if not host:
            return
        normalised = host.lower()
        # Fast-path: if the limiter is globally disabled, do nothing.
        if self._explicit_rate is None and not _is_enabled():
            return

        # Phase 9b: Redis-backed sliding window (opt-in).
        if self._explicit_rate is None and _is_redis_backend_enabled():
            try:
                redis_limiter = _get_redis_limiter()
                await redis_limiter.acquire(normalised)
                return
            except Exception:
                # Fall open to the in-memory bucket below.
                pass

        rate = self._current_rate_per_sec()
        capacity = self._current_capacity()
        while True:
            sleep_for = self._take_token(normalised, rate=rate, capacity=capacity)
            if sleep_for <= 0:
                return
            await asyncio.sleep(sleep_for)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Return a JSON-friendly view of every bucket's current state.

        Used by tests + (eventually) the admin diagnostics endpoint.
        """
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for host, b in self._buckets.items():
                out[host] = {
                    "tokens": round(b.tokens, 4),
                    "capacity": b.capacity,
                    "refill_rate_per_sec": b.refill_rate_per_sec,
                }
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _current_rate_per_sec(self) -> float:
        rpm = self._explicit_rate if self._explicit_rate is not None else _live_rate_per_min()
        return max(rpm, 1) / 60.0

    def _current_capacity(self) -> float:
        if self._explicit_capacity is not None:
            return max(self._explicit_capacity, 1.0)
        rpm = self._explicit_rate if self._explicit_rate is not None else _live_rate_per_min()
        return float(max(rpm, 1))

    def _take_token(self, host: str, *, rate: float, capacity: float) -> float:
        """Try to spend one token. Returns 0 on success, else sleep duration.

        Holding the lock for both the refill and the consume keeps two
        concurrent acquires from over-spending the bucket; the actual
        ``await asyncio.sleep`` happens outside the lock so the lock
        never gates the event loop for more than a few microseconds.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = _Bucket(
                    capacity=capacity,
                    refill_rate_per_sec=rate,
                    tokens=capacity,
                    last_refill=now,
                )
                self._buckets[host] = bucket
            # Update refill rate / capacity in case the admin knob has
            # changed since the bucket was created. New rate takes
            # effect on the next refill tick — we do not retroactively
            # rescale the existing token count.
            bucket.refill_rate_per_sec = rate
            bucket.capacity = capacity
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            # Need to wait for one full token. ``rate`` is tokens/sec,
            # so the wait is ``(1 - tokens) / rate``.
            deficit = 1.0 - bucket.tokens
            return deficit / rate if rate > 0 else 1.0


# ---------------------------------------------------------------------------
# Module-level singleton + admin-knob helpers.
# ---------------------------------------------------------------------------

_limiter_lock = threading.Lock()
_limiter: HostRateLimiter | None = None


def get_limiter() -> HostRateLimiter:
    """Return the lazy module-level limiter singleton."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = HostRateLimiter()
    return _limiter


def reset_limiter_for_tests() -> None:
    """Drop the singleton so tests can rebuild it without leftover state."""
    global _limiter
    with _limiter_lock:
        _limiter = None


def _enabled_knob() -> int:
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_host_rate_limit")
    except Exception:
        return int(settings.enable_host_rate_limit)


def _live_rate_per_min() -> int:
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("host_rate_per_min")
    except Exception:
        return int(settings.host_rate_per_min)


def _is_enabled() -> bool:
    return _enabled_knob() == 1


def is_enabled() -> bool:
    """Public alias. ``True`` when the per-host limiter is on."""
    return _is_enabled()


# ---------------------------------------------------------------------------
# Phase 9b: Redis backend dispatch.
# ---------------------------------------------------------------------------

_redis_limiter_lock = threading.Lock()
_redis_limiter: RedisHostRateLimiter | None = None


def _is_redis_backend_enabled() -> bool:
    """``True`` iff the admin knob *and* a redis URL are both set.

    Wraps :func:`app.host_rate_limit_redis.is_configured` so the
    dispatch in ``acquire`` does not pay the import cost on the
    default in-memory path.
    """
    try:
        from . import host_rate_limit_redis as redis_mod
    except Exception:
        return False
    try:
        return bool(redis_mod.is_configured())
    except Exception:
        return False


def _get_redis_limiter() -> RedisHostRateLimiter:
    """Return a process-singleton Redis limiter.

    The constructor never opens the connection — the first
    ``acquire`` triggers the lazy ``redis.Redis.from_url`` round-trip
    via :meth:`RedisHostRateLimiter._ensure_client`. Caching here
    means we don't pay the constructor cost on every probe.
    """
    global _redis_limiter
    if _redis_limiter is not None:
        return _redis_limiter
    with _redis_limiter_lock:
        if _redis_limiter is None:
            from .host_rate_limit_redis import RedisHostRateLimiter

            _redis_limiter = RedisHostRateLimiter()
    return _redis_limiter


def reset_redis_limiter_for_tests() -> None:
    """Drop the cached Redis limiter so tests can rebuild it."""
    global _redis_limiter
    with _redis_limiter_lock:
        _redis_limiter = None


def active_backend() -> str:
    """``"redis"`` when 9b is configured, else ``"memory"``.

    Used by :func:`status` to surface the active backend on
    ``/api/health.host_rate_limit.backend`` so an operator dashboard
    can verify cluster mode is live without firing a probe.
    """
    return "redis" if _is_redis_backend_enabled() else "memory"


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health``."""
    return {
        "enabled": _enabled_knob() == 1,
        "rate_per_min": _live_rate_per_min(),
        "backend": active_backend(),
    }
