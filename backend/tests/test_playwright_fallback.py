"""Phase 9a tests: Playwright fallback budget + availability probe.

We stay scrupulously dep-free here — playwright is NOT installed in
the default backend dev deps (it ships a ~200 MB chromium asset). The
tests exercise:

* ``BudgetTracker`` atomic semantics + zero-limit short-circuit.
* ``chromium_available`` returning ``False`` when playwright isn't
  importable — i.e. in CI.
* ``PlaywrightFallback.fetch`` returning :data:`BUDGET_EXHAUSTED` when
  the budget is drained, *without* raising.
* ``PlaywrightFallback.fetch`` raising ``PlaywrightUnavailableError``
  when playwright cannot be imported (lazy-import contract).
* ``PlaywrightFallback.aclose`` is idempotent and cleans up state
  even when the browser was never started.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app import playwright_fallback
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "anti_bot_playwright_fallback", 0, raising=False)
    monkeypatch.setattr(settings, "anti_bot_playwright_max_per_scan", 5, raising=False)
    monkeypatch.setattr(settings, "anti_bot_playwright_timeout_sec", 10, raising=False)
    yield


# ---------------------------------------------------------------------------
# BudgetTracker
# ---------------------------------------------------------------------------


def test_budget_tracker_starts_with_full_capacity() -> None:
    bt = playwright_fallback.BudgetTracker(limit=3)
    assert bt.remaining() == 3
    assert bt.used == 0


def test_budget_tracker_consumes_one_slot_at_a_time() -> None:
    bt = playwright_fallback.BudgetTracker(limit=2)
    assert bt.try_consume() is True
    assert bt.used == 1
    assert bt.try_consume() is True
    assert bt.used == 2
    # Exhausted.
    assert bt.try_consume() is False
    assert bt.used == 2  # didn't go over


def test_budget_tracker_zero_limit_is_immediately_exhausted() -> None:
    bt = playwright_fallback.BudgetTracker(limit=0)
    assert bt.remaining() == 0
    assert bt.try_consume() is False


def test_budget_tracker_concurrent_consume_never_overspends() -> None:
    """Spin 50 threads at a 10-token bucket and assert exactly 10 win.

    Guards the lock around _used so a multi-worker Celery fan-out
    can't race past the budget.
    """
    import threading

    bt = playwright_fallback.BudgetTracker(limit=10)
    wins: list[bool] = []
    barrier = threading.Barrier(50)
    lock = threading.Lock()

    def _attempt() -> None:
        barrier.wait()
        result = bt.try_consume()
        with lock:
            wins.append(result)

    threads = [threading.Thread(target=_attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for w in wins if w) == 10
    assert bt.used == 10


# ---------------------------------------------------------------------------
# chromium_available probe
# ---------------------------------------------------------------------------


def test_chromium_available_false_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In default dev environments playwright isn't installed; the
    probe must report False without raising."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert playwright_fallback.chromium_available() is False


# ---------------------------------------------------------------------------
# PlaywrightFallback.fetch — budget + lazy-import contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_budget_exhausted_when_drained() -> None:
    bt = playwright_fallback.BudgetTracker(limit=1)
    pf = playwright_fallback.PlaywrightFallback(budget=bt)

    # Drain the budget by hand so the next fetch hits the
    # short-circuit *before* attempting a chromium spawn.
    assert bt.try_consume() is True

    result = await pf.fetch("https://example.test/")
    assert result is playwright_fallback.BUDGET_EXHAUSTED
    assert result.used_fallback is False
    assert result.status == 0
    await pf.aclose()


@pytest.mark.asyncio
async def test_fetch_raises_unavailable_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When playwright isn't importable, fetch raises a clean
    ``PlaywrightUnavailableError`` so the checker can record a
    ``playwright-unavailable`` signal."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright.async_api":
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    pf = playwright_fallback.PlaywrightFallback(budget=playwright_fallback.BudgetTracker(limit=5))
    with pytest.raises(playwright_fallback.PlaywrightUnavailableError):
        await pf.fetch("https://example.test/")
    await pf.aclose()


@pytest.mark.asyncio
async def test_fetch_after_close_raises() -> None:
    pf = playwright_fallback.PlaywrightFallback(budget=playwright_fallback.BudgetTracker(limit=5))
    await pf.aclose()
    with pytest.raises(playwright_fallback.PlaywrightUnavailableError):
        await pf.fetch("https://example.test/")


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    pf = playwright_fallback.PlaywrightFallback(budget=playwright_fallback.BudgetTracker(limit=5))
    await pf.aclose()
    # No exception on a second close call.
    await pf.aclose()


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_default_budget_uses_admin_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anti_bot_playwright_max_per_scan", 7, raising=False)
    pf = playwright_fallback.PlaywrightFallback()
    try:
        assert pf.budget.limit == 7
    finally:
        asyncio.run(pf.aclose())


def test_default_timeout_uses_admin_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anti_bot_playwright_timeout_sec", 23, raising=False)
    pf = playwright_fallback.PlaywrightFallback()
    try:
        assert pf._timeout_sec == 23
    finally:
        asyncio.run(pf.aclose())
