"""Phase 8b tests: per-host token-bucket rate limiter.

Token bucket math is verified deterministically by patching
``time.monotonic`` and ``asyncio.sleep`` so the tests don't depend on
wall-clock timing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app import host_rate_limit
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_limiter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "enable_host_rate_limit", 0, raising=False)
    monkeypatch.setattr(settings, "host_rate_per_min", 60, raising=False)
    host_rate_limit.reset_limiter_for_tests()
    yield
    host_rate_limit.reset_limiter_for_tests()


# ---------------------------------------------------------------------------
# disabled-mode fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enable_host_rate_limit", 0, raising=False)
    sleeps: list[float] = []

    async def _spy_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter()
    for _ in range(100):
        await limiter.acquire("example.com")
    assert sleeps == []


@pytest.mark.asyncio
async def test_acquire_is_noop_for_empty_host() -> None:
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=60)
    # Should return immediately even when explicitly enabled.
    await limiter.acquire("")


# ---------------------------------------------------------------------------
# token bucket math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_starts_full_then_drains_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity == rate by default. A fresh bucket should grant exactly
    ``capacity`` immediate acquires before the next one blocks."""
    fake_time = [0.0]
    monkeypatch.setattr(host_rate_limit.time, "monotonic", lambda: fake_time[0])
    sleeps: list[float] = []

    async def _spy_sleep(d: float) -> None:
        sleeps.append(d)
        # Simulate the wait-clock advance so the next iteration of the
        # acquire loop sees a refilled bucket.
        fake_time[0] += d

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=2.0)
    # Two immediate acquires, the third must sleep until next refill.
    await limiter.acquire("h")
    await limiter.acquire("h")
    assert sleeps == []
    await limiter.acquire("h")
    assert len(sleeps) == 1
    # rate = 60/min == 1/sec, so one full token takes ~1.0s.
    assert sleeps[0] == pytest.approx(1.0, abs=1e-3)


@pytest.mark.asyncio
async def test_buckets_are_independent_per_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [0.0]
    monkeypatch.setattr(host_rate_limit.time, "monotonic", lambda: fake_time[0])
    sleeps: list[tuple[str, float]] = []

    async def _spy_sleep(d: float) -> None:
        sleeps.append(("sleep", d))
        fake_time[0] += d

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=1.0)
    # Drain host A's bucket.
    await limiter.acquire("alpha.com")
    # host B should still have its own full bucket — no sleep.
    await limiter.acquire("beta.com")
    assert sleeps == []


@pytest.mark.asyncio
async def test_host_normalised_to_lowercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [0.0]
    monkeypatch.setattr(host_rate_limit.time, "monotonic", lambda: fake_time[0])
    sleeps: list[float] = []

    async def _spy_sleep(d: float) -> None:
        sleeps.append(d)
        fake_time[0] += d

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=1.0)
    # Same host with different cases must share a bucket.
    await limiter.acquire("Reddit.com")
    await limiter.acquire("reddit.com")
    assert sleeps and sleeps[0] == pytest.approx(1.0, abs=1e-3)


@pytest.mark.asyncio
async def test_bucket_refills_after_idle_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bucket that's been quiet for ``capacity / rate`` seconds must
    refill to capacity (not unbounded)."""
    fake_time = [0.0]
    monkeypatch.setattr(host_rate_limit.time, "monotonic", lambda: fake_time[0])
    sleeps: list[float] = []

    async def _spy_sleep(d: float) -> None:
        sleeps.append(d)
        fake_time[0] += d

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=2.0)
    # Drain.
    await limiter.acquire("h")
    await limiter.acquire("h")
    # Sleep 60s — enough for >>2 tokens to accrue, but capped at 2.
    fake_time[0] = 60.0
    sleeps.clear()
    await limiter.acquire("h")
    await limiter.acquire("h")
    assert sleeps == []  # both immediate
    # Third request now blocks because capacity is 2, not 60.
    await limiter.acquire("h")
    assert len(sleeps) == 1


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_reports_per_host_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [0.0]
    monkeypatch.setattr(host_rate_limit.time, "monotonic", lambda: fake_time[0])

    async def _spy_sleep(_d: float) -> None:  # pragma: no cover
        pytest.fail("should not sleep")

    monkeypatch.setattr(host_rate_limit.asyncio, "sleep", _spy_sleep)
    limiter = host_rate_limit.HostRateLimiter(rate_per_min=60, capacity=10.0)
    await limiter.acquire("a.test")
    snap = limiter.snapshot()
    assert "a.test" in snap
    state: dict[str, Any] = snap["a.test"]
    assert state["capacity"] == 10.0
    assert state["refill_rate_per_sec"] == pytest.approx(1.0, abs=1e-6)
    assert state["tokens"] == pytest.approx(9.0, abs=1e-3)
