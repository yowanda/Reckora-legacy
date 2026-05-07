"""Phase 8b: per-host token-bucket rate limiter.

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
* **Token bucket**, not sliding window. A bucket lets a quiet host
  absorb a 60-request burst at the start of a scan if it had been
  idle for a minute, which is exactly what we want — it preserves
  scan throughput while still enforcing a steady-state rate. A
  sliding window would drag every burst out to the slow steady rate.

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
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

from .config import settings


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
        """Block until one token is available for ``host``.

        Hosts are normalised to lowercase so ``Reddit.com`` and
        ``reddit.com`` share a bucket. An empty / falsy host (e.g. a
        ``file://`` URL passed in by accident) is treated as a no-op.
        """
        if not host:
            return
        normalised = host.lower()
        # Fast-path: if the limiter is globally disabled, do nothing.
        if self._explicit_rate is None and not _is_enabled():
            return
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


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health``."""
    return {
        "enabled": _enabled_knob() == 1,
        "rate_per_min": _live_rate_per_min(),
    }
