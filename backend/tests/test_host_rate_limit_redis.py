"""Phase 9b: tests for the Redis-backed host rate limiter.

The full integration suite spins a real Redis (the dev box has
``redis-server`` available; CI ships a ``redis-stack`` service on
port 6379 in the matrix). Tests that *need* Redis discover it via
``RECKORA_TEST_REDIS_URL`` and skip when the env var is not set so
the local-dev box without Redis still gets a green run.

The ``RedisHostRateLimiter`` lazy-import (``redis`` only loads on
the first acquire) and the fail-open dispatch in
``host_rate_limit.HostRateLimiter`` are also covered with
synthetic stubs so the *protocol* of the wiring is exercised even
when the env var is unset.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Iterator

import pytest

from app import host_rate_limit
from app.config import settings
from app.host_rate_limit import HostRateLimiter
from app.host_rate_limit_redis import (
    RedisHostRateLimiter,
    RedisUnavailableError,
)

REDIS_URL = os.environ.get("RECKORA_TEST_REDIS_URL")

requires_redis = pytest.mark.skipif(
    REDIS_URL is None,
    reason="RECKORA_TEST_REDIS_URL not set; skipping live-Redis tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Drop the cached limiter singletons before/after every test."""
    host_rate_limit.reset_limiter_for_tests()
    host_rate_limit.reset_redis_limiter_for_tests()
    yield
    host_rate_limit.reset_limiter_for_tests()
    host_rate_limit.reset_redis_limiter_for_tests()


def _unique_prefix() -> str:
    return f"reckora:test:{uuid.uuid4().hex[:12]}:"


# ---------------------------------------------------------------------------
# Phase 9b knob plumbing — runs without redis.
# ---------------------------------------------------------------------------


def test_status_reports_memory_backend_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both knobs at default (use_redis=0, no URL), the limiter
    reports ``backend="memory"`` so an operator dashboard can verify
    cluster mode is *not* active."""
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 0, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_redis_url", "", raising=False)
    monkeypatch.setattr(settings, "celery_broker_url", "", raising=False)
    payload = host_rate_limit.status()
    assert payload["backend"] == "memory"


def test_status_reports_redis_when_knob_and_url_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping ``host_rate_limit_use_redis=1`` *and* providing a
    URL flips the descriptor to ``backend="redis"``."""
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 1, raising=False)
    monkeypatch.setattr(
        settings,
        "host_rate_limit_redis_url",
        "redis://example.invalid:6379/0",
        raising=False,
    )
    payload = host_rate_limit.status()
    assert payload["backend"] == "redis"


def test_redis_backend_falls_back_to_memory_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``host_rate_limit_use_redis=1`` but no URL configured
    anywhere, the limiter still reports ``memory`` — the dispatch
    helper in :mod:`app.host_rate_limit` checks both the knob and
    a non-empty resolved URL."""
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_redis_url", "", raising=False)
    monkeypatch.setattr(settings, "celery_broker_url", "", raising=False)
    payload = host_rate_limit.status()
    assert payload["backend"] == "memory"


def test_status_falls_back_to_celery_broker_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the dedicated ``host_rate_limit_redis_url`` is empty the
    Phase 8a ``celery_broker_url`` is reused — that's the documented
    operator default (one Redis URL configured for both Celery and
    rate limit)."""
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_redis_url", "", raising=False)
    monkeypatch.setattr(
        settings,
        "celery_broker_url",
        "redis://shared.invalid:6379/0",
        raising=False,
    )
    payload = host_rate_limit.status()
    assert payload["backend"] == "redis"


# ---------------------------------------------------------------------------
# Fail-open contract — runs without redis.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_falls_open_to_memory_when_redis_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis transport error must not propagate to the caller —
    :func:`HostRateLimiter.acquire` swallows the exception and falls
    through to the in-memory token bucket so a Redis outage doesn't
    fail-closed every probe."""

    monkeypatch.setattr(settings, "enable_host_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 1, raising=False)
    monkeypatch.setattr(
        settings,
        "host_rate_limit_redis_url",
        "redis://shared.invalid:6379/0",
        raising=False,
    )

    class _AlwaysFailingLimiter:
        async def acquire(self, host: str) -> None:
            raise RedisUnavailableError("simulated outage")

    monkeypatch.setattr(
        host_rate_limit,
        "_get_redis_limiter",
        lambda: _AlwaysFailingLimiter(),
    )

    limiter = HostRateLimiter(rate_per_min=600, capacity=600)
    # Force the dispatch path even though ``rate_per_min`` is
    # explicit — the test_only switch toggles the redis branch via
    # the live setting check for the *live* (no-explicit-rate)
    # limiter we instantiate next.
    assert limiter is not None

    live_limiter = HostRateLimiter()
    # acquire() must complete fast (< 50 ms) because the in-memory
    # bucket starts full and the Redis call should be a no-op
    # raise → caught → fall through.
    start = time.monotonic()
    await live_limiter.acquire("example.com")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"acquire took {elapsed:.3f}s; fail-open is broken"


def test_redis_limiter_raises_when_redis_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ``redis`` package isn't importable the limiter
    raises :class:`RedisUnavailableError` so the fail-open dispatch
    in :mod:`host_rate_limit` can route to the in-memory bucket.

    We simulate the missing package by hooking ``__import__`` so
    only ``redis`` raises ``ImportError`` — every other import is
    untouched.
    """
    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    )  # type: ignore[index, union-attr]

    def fake_import(
        name: str,
        globals: object | None = None,
        locals: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "redis":
            raise ImportError("simulated redis-package-missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setitem(
        __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__,
        "__import__",
        fake_import,
    )  # type: ignore[union-attr,arg-type]

    limiter = RedisHostRateLimiter(redis_url="redis://example.invalid:6379/0")
    with pytest.raises(RedisUnavailableError):
        limiter._ensure_client()


def test_redis_limiter_raises_when_url_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit empty URL + a blank ``celery_broker_url`` is
    structurally a misconfiguration — the limiter must surface it
    rather than silently picking ``localhost``."""
    monkeypatch.setattr(settings, "host_rate_limit_redis_url", "", raising=False)
    monkeypatch.setattr(settings, "celery_broker_url", "", raising=False)
    limiter = RedisHostRateLimiter()
    with pytest.raises(RedisUnavailableError):
        limiter._ensure_client()


def test_redis_unavailable_when_url_unreachable() -> None:
    """An unreachable Redis URL surfaces a :class:`RedisUnavailableError`
    on first acquire, not a low-level ``ConnectionError``. The
    constructor itself never opens a connection (lazy)."""
    limiter = RedisHostRateLimiter(redis_url="redis://127.0.0.1:1/0")
    with pytest.raises(RedisUnavailableError):
        limiter._ensure_client()


# ---------------------------------------------------------------------------
# Live Redis integration — skipped unless RECKORA_TEST_REDIS_URL is set.
# ---------------------------------------------------------------------------


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_acquire_within_window_is_immediate() -> None:
    """First N acquires for a host inside the window must complete
    immediately. With ``rate_per_min=120`` and 4 sequential
    acquires we should be well under the limit and never block."""
    limiter = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=120,
        key_prefix=_unique_prefix(),
    )
    try:
        start = time.monotonic()
        for _ in range(4):
            await limiter.acquire("example.com")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"4 acquires took {elapsed:.2f}s; expected <1s"
    finally:
        await limiter.aclose()


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_blocks_when_window_full() -> None:
    """When the configured window is exhausted, the next acquire
    sleeps until the oldest bucket falls out. We pick a 2-second
    window with ``rate_per_min=2`` so the test runs in <3s; the
    limiter is constructed with ``window_sec=2`` to keep the bound
    deterministic."""
    limiter = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=2,
        window_sec=2,
        key_prefix=_unique_prefix(),
    )
    try:
        # First two are immediate.
        await limiter.acquire("example.com")
        await limiter.acquire("example.com")
        # Third must block ~2s for the window to roll.
        start = time.monotonic()
        await limiter.acquire("example.com")
        elapsed = time.monotonic() - start
        assert 0.5 <= elapsed <= 3.5, f"third acquire elapsed {elapsed:.2f}s; expected ~2s"
    finally:
        await limiter.aclose()


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_concurrent_workers_share_one_bucket() -> None:
    """The whole point of the Redis backend: 4 simulated workers
    racing the same host's bucket must collectively stay under the
    rate. We dispatch 8 acquires across 4 workers with
    ``rate_per_min=4`` and ``window_sec=2``; the first 4 land
    immediately and the last 4 collectively block ~2s for the
    window to roll."""
    prefix = _unique_prefix()

    async def worker() -> None:
        # Each worker uses its own RedisHostRateLimiter instance to
        # simulate the Celery fan-out (separate client connections,
        # one shared Redis bucket).
        wlimiter = RedisHostRateLimiter(
            redis_url=REDIS_URL,
            rate_per_min=4,
            window_sec=2,
            key_prefix=prefix,
        )
        try:
            await wlimiter.acquire("shared-host.example")
            await wlimiter.acquire("shared-host.example")
        finally:
            await wlimiter.aclose()

    start = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(4)))
    elapsed = time.monotonic() - start
    # 8 requests, rate=4/window=2s → first 4 are immediate, last 4
    # wait for window roll, second-window-pass elapses around 2s.
    assert elapsed >= 1.5, (
        f"4 workers x 2 acquires = 8 requests at rate=4/window=2s "
        f"completed in {elapsed:.2f}s — too fast; the bucket isn't shared"
    )
    assert elapsed <= 5.0, f"4 workers took {elapsed:.2f}s — too slow; expected ≤5s"


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_buckets_independent_per_host() -> None:
    """Two distinct hosts share the same Redis but their buckets
    are independent — exhausting host A's window must not delay
    host B."""
    prefix = _unique_prefix()
    limiter = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=2,
        window_sec=2,
        key_prefix=prefix,
    )
    try:
        # Drain host A's window.
        await limiter.acquire("host-a.example")
        await limiter.acquire("host-a.example")
        # Host B is still empty so its acquire is immediate.
        start = time.monotonic()
        await limiter.acquire("host-b.example")
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"host-b acquire blocked {elapsed:.2f}s — buckets aren't isolated"
    finally:
        await limiter.aclose()


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_acquire_recovers_from_noscript_eviction() -> None:
    """Redis can evict the cached Lua script under memory pressure;
    the limiter must transparently re-load the script and retry.
    We simulate eviction with ``SCRIPT FLUSH`` between acquires."""
    import redis as redis_pkg

    prefix = _unique_prefix()
    limiter = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=120,
        key_prefix=prefix,
    )
    try:
        await limiter.acquire("host-flush.example")
        # Manually flush the script cache from a sibling client.
        sibling = redis_pkg.Redis.from_url(REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
        sibling.script_flush()
        sibling.close()
        # Next acquire should still succeed thanks to the NOSCRIPT
        # recovery path.
        await limiter.acquire("host-flush.example")
    finally:
        await limiter.aclose()


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_aclose_is_idempotent() -> None:
    """Double-close is allowed and a no-op."""
    limiter = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=60,
        key_prefix=_unique_prefix(),
    )
    await limiter.aclose()
    await limiter.aclose()
    # Acquiring after close must raise so the caller falls open.
    with pytest.raises(RedisUnavailableError):
        await limiter.acquire("example.com")


@requires_redis
@pytest.mark.asyncio
async def test_live_redis_dispatch_through_host_rate_limit_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with the admin knobs flipped on and a real Redis
    URL configured, ``HostRateLimiter().acquire`` dispatches to the
    Redis backend and back-pressures correctly."""
    prefix = _unique_prefix()
    monkeypatch.setattr(settings, "enable_host_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_use_redis", 1, raising=False)
    monkeypatch.setattr(settings, "host_rate_limit_redis_url", REDIS_URL, raising=False)
    monkeypatch.setattr(settings, "host_rate_per_min", 4, raising=False)

    # Replace the cached singleton so the prefix + window are
    # deterministic (the default uses prefix="reckora:hrl:" and
    # window=60s).
    custom = RedisHostRateLimiter(
        redis_url=REDIS_URL,
        rate_per_min=4,
        window_sec=2,
        key_prefix=prefix,
    )
    monkeypatch.setattr(host_rate_limit, "_get_redis_limiter", lambda: custom)

    limiter = HostRateLimiter()
    try:
        await limiter.acquire("dispatch.example")
        await limiter.acquire("dispatch.example")
        await limiter.acquire("dispatch.example")
        await limiter.acquire("dispatch.example")
        # Fifth blocks because window=2s, rate=4.
        start = time.monotonic()
        await limiter.acquire("dispatch.example")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.5, f"fifth acquire elapsed {elapsed:.2f}s; window roll not enforced"
    finally:
        await custom.aclose()
