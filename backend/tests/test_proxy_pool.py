"""Phase 8b tests: multi-proxy egress pool with health cooldown.

Exercises ``app.proxy_pool`` in isolation — env parsing, round-robin
selection, mark_failed cooldown, fail-open when every URL is in
penalty box, and the admin-knob gate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app import proxy_pool
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_pool_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the singleton + reset the env list around every test."""
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_pool.reset_pool_for_tests()
    yield
    proxy_pool.reset_pool_for_tests()


# ---------------------------------------------------------------------------
# env parsing
# ---------------------------------------------------------------------------


def test_parse_pool_urls_handles_empty_env() -> None:
    # Phase 9c: ``_parse_pool_urls`` now returns ``(urls, regions)``.
    # An empty env => empty urls + empty regions map.
    assert proxy_pool._parse_pool_urls("") == ((), {})


def test_parse_pool_urls_strips_whitespace_and_empty_parts() -> None:
    raw = "  socks5://a:1, , http://b:2 ,,socks5h://c:3,"
    urls, regions = proxy_pool._parse_pool_urls(raw)
    assert urls == (
        "socks5://a:1",
        "http://b:2",
        "socks5h://c:3",
    )
    # Bare URLs (no leading region tag) all get the ``"default"``
    # region so the Phase 8b round-robin behaviour is preserved when
    # operators upgrade without changing their env.
    assert regions == {
        "socks5://a:1": "default",
        "http://b:2": "default",
        "socks5h://c:3": "default",
    }


# ---------------------------------------------------------------------------
# acquire / round-robin
# ---------------------------------------------------------------------------


def test_acquire_returns_none_when_pool_empty() -> None:
    pool = proxy_pool.ProxyPool(urls=())
    assert pool.acquire() is None


def test_acquire_round_robin_cycles_through_urls() -> None:
    pool = proxy_pool.ProxyPool(urls=("a", "b", "c"))
    seen = [pool.acquire() for _ in range(7)]
    assert seen == ["a", "b", "c", "a", "b", "c", "a"]


# ---------------------------------------------------------------------------
# health cooldown
# ---------------------------------------------------------------------------


def test_mark_failed_skips_url_until_cooldown_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 30, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    pool.mark_failed("a", now=0.0)
    # b is healthy, a is in cooldown until t=30. Acquire at t=5 should
    # skip a even though it would be next in the round-robin.
    assert pool.acquire(now=5.0) == "b"
    assert pool.acquire(now=5.0) == "b"
    # After cooldown expires both URLs are eligible again.
    assert pool.acquire(now=31.0) == "a"
    assert pool.acquire(now=31.0) == "b"


def test_mark_failed_with_none_is_a_noop() -> None:
    pool = proxy_pool.ProxyPool(urls=("a",))
    # Should not raise / not modify state.
    pool.mark_failed(None)
    assert pool.acquire() == "a"


def test_pool_returns_none_when_every_url_is_in_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open contract: when every URL is unhealthy the pool returns
    ``None`` (direct egress) rather than refusing to serve.
    """
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    pool.mark_failed("a", now=0.0)
    pool.mark_failed("b", now=0.0)
    assert pool.acquire(now=5.0) is None


def test_mark_success_resets_consecutive_failure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 1, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a",))
    pool.mark_failed("a", now=0.0)
    pool.mark_failed("a", now=0.5)
    assert pool._state["a"].consecutive_failures == 2
    pool.mark_success("a")
    assert pool._state["a"].consecutive_failures == 0


# ---------------------------------------------------------------------------
# is_enabled gate / get_pool singleton
# ---------------------------------------------------------------------------


def test_is_enabled_false_when_pool_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    proxy_pool.reset_pool_for_tests()
    assert proxy_pool.is_enabled() is False


def test_is_enabled_false_when_knob_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "proxy_pool_urls", "http://a:1,http://b:2", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    proxy_pool.reset_pool_for_tests()
    assert proxy_pool.is_enabled() is False


def test_is_enabled_true_when_pool_set_and_knob_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_pool_urls", "http://a:1,http://b:2", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 1, raising=False)
    proxy_pool.reset_pool_for_tests()
    assert proxy_pool.is_enabled() is True


def test_get_pool_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "proxy_pool_urls", "http://a:1", raising=False)
    proxy_pool.reset_pool_for_tests()
    a = proxy_pool.get_pool()
    b = proxy_pool.get_pool()
    assert a is b


# ---------------------------------------------------------------------------
# health() snapshot
# ---------------------------------------------------------------------------


def test_health_snapshot_reports_cooldown_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    pool = proxy_pool.ProxyPool(urls=("socks5://a:1", "http://b:2"))
    pool.mark_failed("socks5://a:1")
    snap = pool.health()
    schemes = {row["url_scheme"]: row for row in snap}
    assert schemes["socks5"]["in_cooldown"] is True
    assert schemes["http"]["in_cooldown"] is False
    assert schemes["socks5"]["consecutive_failures"] == 1
