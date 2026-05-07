"""Phase 9g tests — proxy reputation warm-up + weighted selection.

Two layers of coverage:

* Pure :class:`backend.app.proxy_reputation.ProxyReputation` unit
  tests: the warm-up window logic, the success/failure counter
  arithmetic, the post-warmup weight formula, and the
  stride-scheduled :meth:`pick` selection.
* :class:`backend.app.proxy_pool.ProxyPool` integration tests that
  exercise the wiring point — the byte-for-byte default-off path,
  the weighted ``acquire`` when the knob is on, and the
  ``mark_failed`` / ``mark_success`` reputation feed.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from app import proxy_pool, proxy_reputation
from app.config import settings


class _StubSettings:
    """Minimal stand-in for :class:`backend.app.config.Settings`."""

    def __init__(
        self,
        *,
        enable_proxy_reputation: int = 0,
        proxy_warmup_window_days: int = 7,
    ) -> None:
        self.enable_proxy_reputation = enable_proxy_reputation
        self.proxy_warmup_window_days = proxy_warmup_window_days


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop the pool + reputation singletons around every test."""
    monkeypatch.setattr(settings, "proxy_pool_urls", "", raising=False)
    monkeypatch.setattr(settings, "enable_proxy_rotator", 0, raising=False)
    monkeypatch.setattr(settings, "enable_geo_aware_proxy", 0, raising=False)
    monkeypatch.setattr(settings, "enable_proxy_reputation", 0, raising=False)
    monkeypatch.setattr(settings, "proxy_warmup_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_pool.reset_pool_for_tests()
    proxy_reputation.reset_for_tests()
    yield
    proxy_pool.reset_pool_for_tests()
    proxy_reputation.reset_for_tests()


# ---------------------------------------------------------------------------
# from_settings + default-off contract
# ---------------------------------------------------------------------------


def test_from_settings_defaults_to_disabled() -> None:
    rep = proxy_reputation.ProxyReputation.from_settings(_StubSettings())
    assert rep.is_enabled is False
    # Default warmup window is 7 days expressed in seconds.
    assert rep.warmup_window_sec == pytest.approx(7 * 86_400)


def test_from_settings_reads_enabled_and_warmup_days() -> None:
    rep = proxy_reputation.ProxyReputation.from_settings(
        _StubSettings(enable_proxy_reputation=1, proxy_warmup_window_days=3)
    )
    assert rep.is_enabled is True
    assert rep.warmup_window_sec == pytest.approx(3 * 86_400)


def test_from_settings_clamps_negative_warmup_days_to_zero() -> None:
    """Defensive: a negative knob value still parses to a non-negative window."""

    rep = proxy_reputation.ProxyReputation.from_settings(
        _StubSettings(enable_proxy_reputation=1, proxy_warmup_window_days=-5)
    )
    assert rep.warmup_window_sec == 0.0


# ---------------------------------------------------------------------------
# record_success / record_failure
# ---------------------------------------------------------------------------


def test_record_success_increments_counter() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    rep.record_success("http://a:1", now=100.0)
    rep.record_success("http://a:1", now=101.0)
    assert rep._states["http://a:1"].successes == 2
    assert rep._states["http://a:1"].failures == 0


def test_record_failure_increments_counter() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    rep.record_failure("http://a:1", now=100.0)
    assert rep._states["http://a:1"].failures == 1
    assert rep._states["http://a:1"].successes == 0


def test_record_with_none_url_is_a_noop() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    rep.record_success(None, now=100.0)
    rep.record_failure(None, now=100.0)
    assert rep._states == {}


# ---------------------------------------------------------------------------
# weight() math
# ---------------------------------------------------------------------------


def test_weight_unknown_url_defaults_to_one() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    # ``weight()`` must be non-mutating — a query for an unknown URL
    # returns 1.0 without seeding a state row.
    assert rep.weight("http://nobody:1") == 1.0
    assert "http://nobody:1" not in rep._states


def test_weight_during_warmup_is_one_regardless_of_failures() -> None:
    rep = proxy_reputation.ProxyReputation(
        enabled=True,
        warmup_window_sec=3600.0,  # 1h warmup
    )
    rep.record_failure("http://a:1", now=100.0)
    rep.record_failure("http://a:1", now=101.0)
    # Still inside the warm-up window (1 second elapsed << 3600s),
    # so the all-failure URL is still 1.0-weight.
    assert rep.weight("http://a:1", now=102.0) == 1.0


def test_weight_after_warmup_uses_success_rate() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=10.0)
    rep.record_success("http://a:1", now=100.0)  # first_seen=100
    rep.record_failure("http://a:1", now=100.0)
    rep.record_success("http://a:1", now=100.0)
    rep.record_success("http://a:1", now=100.0)
    # 3 success / 1 failure = 0.75 ratio. now=120 is past first_seen+10
    # so the warm-up branch has lapsed.
    assert rep.weight("http://a:1", now=120.0) == pytest.approx(0.75)


def test_weight_after_warmup_with_no_data_defaults_to_one() -> None:
    """A URL registered (e.g. via ``pick``) but never recorded keeps a
    1.0 weight even after warmup so it is not perpetually starved."""

    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=10.0)
    # pick() seeds the state row at first_seen=100
    rep.pick(["http://a:1"], now=100.0)
    # Past warmup (now > 110) but no record_success / record_failure
    # was ever called.
    assert rep.weight("http://a:1", now=120.0) == 1.0


def test_weight_after_warmup_all_failures_drops_to_zero() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=1.0)
    rep.record_failure("http://a:1", now=0.0)
    rep.record_failure("http://a:1", now=0.0)
    assert rep.weight("http://a:1", now=10.0) == 0.0


# ---------------------------------------------------------------------------
# pick() — selection
# ---------------------------------------------------------------------------


def test_pick_returns_none_for_empty_candidates() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    assert rep.pick([], now=0.0) is None


def test_pick_equal_weights_round_robins_in_cursor_order() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=0.0)
    # No data on any URL => weight=1.0 for every URL => stride=1 each
    # => the picks fall in stable insertion order over 3 calls.
    seen = [rep.pick(["a", "b", "c"], now=0.0) for _ in range(6)]
    assert seen == ["a", "b", "c", "a", "b", "c"]


def test_pick_higher_weight_url_is_picked_more_often() -> None:
    """1.0-weight URL has stride=1; 0.5-weight URL has stride=2; over 6
    picks the 1.0-weight URL is picked twice as often.
    """

    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=1.0)
    # ``a`` is 4/4 success past warmup => weight=1.0
    for _ in range(4):
        rep.record_success("a", now=0.0)
    # ``b`` is 1/2 past warmup => weight=0.5
    rep.record_success("b", now=0.0)
    rep.record_failure("b", now=0.0)
    seen = [rep.pick(["a", "b"], now=10.0) for _ in range(6)]
    # Stride scheduling result is deterministic: a=stride1, b=stride2
    # => sequence of virt_time picks is a a b a a b (repeating).
    assert seen.count("a") == 4
    assert seen.count("b") == 2


def test_pick_skips_url_whose_weight_is_below_epsilon() -> None:
    """A URL with weight=0 (post-warmup, all failures) is excluded from
    the candidate set so the higher-weight URL keeps serving.
    """

    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=1.0)
    # ``dead`` has 0/3 success => weight=0 past warmup.
    for _ in range(3):
        rep.record_failure("dead", now=0.0)
    # ``alive`` has 1/1 success => weight=1.
    rep.record_success("alive", now=0.0)
    seen = {rep.pick(["dead", "alive"], now=10.0) for _ in range(5)}
    assert seen == {"alive"}


def test_pick_returns_none_when_every_candidate_is_below_epsilon() -> None:
    """If literally every candidate is dead, ``pick`` returns ``None``
    so the caller falls back to direct egress (matches the Phase 8b
    fail-open contract).
    """

    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=1.0)
    rep.record_failure("a", now=0.0)
    rep.record_failure("b", now=0.0)
    assert rep.pick(["a", "b"], now=10.0) is None


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def test_snapshot_returns_per_url_rows() -> None:
    rep = proxy_reputation.ProxyReputation(enabled=True, warmup_window_sec=10.0)
    rep.record_success("http://a:1", now=0.0)
    rep.record_failure("http://a:1", now=0.0)
    rep.record_success("socks5://b:2", now=0.0)
    rows = {r["url_scheme"]: r for r in rep.snapshot(now=20.0)}
    assert rows["http"]["successes"] == 1
    assert rows["http"]["failures"] == 1
    assert rows["http"]["weight"] == pytest.approx(0.5)
    assert rows["http"]["in_warmup"] is False
    assert rows["socks5"]["successes"] == 1
    assert rows["socks5"]["failures"] == 0


# ---------------------------------------------------------------------------
# Module-level singleton + is_enabled gate
# ---------------------------------------------------------------------------


def test_get_reputation_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    proxy_reputation.reset_for_tests()
    a = proxy_reputation.get_reputation()
    b = proxy_reputation.get_reputation()
    assert a is b


def test_is_enabled_reads_admin_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enable_proxy_reputation", 0, raising=False)
    assert proxy_reputation.is_enabled() is False
    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    assert proxy_reputation.is_enabled() is True


# ---------------------------------------------------------------------------
# ProxyPool integration — default-off byte-for-byte
# ---------------------------------------------------------------------------


def test_pool_acquire_default_off_is_byte_for_byte_round_robin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enable_proxy_reputation=0`` MUST keep the Phase 8b round-robin
    behaviour byte-for-byte — this is the locked-in default-off
    contract from the Phase 9g handoff.
    """

    monkeypatch.setattr(settings, "enable_proxy_reputation", 0, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a", "b", "c"))
    # Same expectation as the Phase 8b
    # ``test_acquire_round_robin_cycles_through_urls`` test.
    seen = [pool.acquire() for _ in range(7)]
    assert seen == ["a", "b", "c", "a", "b", "c", "a"]


def test_pool_acquire_weighted_path_active_when_knob_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With reputation on the higher-weight URL is picked more often."""

    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    monkeypatch.setattr(settings, "proxy_warmup_window_days", 0, raising=False)
    proxy_reputation.reset_for_tests()
    rep = proxy_reputation.get_reputation()
    # Stack the deck: ``a`` has 100% success, ``b`` is ~10% success.
    for _ in range(10):
        rep.record_success("a", now=0.0)
    rep.record_success("b", now=0.0)
    for _ in range(9):
        rep.record_failure("b", now=0.0)
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    seen = [pool.acquire(now=10.0) for _ in range(10)]
    # ``a`` should dominate the rotation by a wide margin.
    assert seen.count("a") > seen.count("b")
    assert seen.count("a") >= 8


def test_pool_mark_failed_feeds_reputation_when_knob_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    pool.mark_failed("a", now=0.0)
    assert proxy_reputation.get_reputation()._states["a"].failures == 1


def test_pool_mark_success_feeds_reputation_when_knob_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    pool.mark_success("a")
    assert proxy_reputation.get_reputation()._states["a"].successes == 1


def test_pool_mark_failed_does_not_touch_reputation_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off contract: mark_failed must not allocate / mutate the
    reputation tracker so the pre-9g hot path stays free of the extra
    cost.
    """

    monkeypatch.setattr(settings, "enable_proxy_reputation", 0, raising=False)
    proxy_reputation.reset_for_tests()
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    pool.mark_failed("a", now=0.0)
    # Singleton may not even have been built; force-build it and
    # verify no rows were recorded.
    rep = proxy_reputation.get_reputation()
    assert rep._states == {}


def test_pool_acquire_weighted_falls_back_through_region_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 9c three-pass region structure is preserved on the
    weighted path: a ``ru`` request misses pass 1, falls through to
    the default tier in pass 2.
    """

    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    monkeypatch.setattr(settings, "proxy_warmup_window_days", 0, raising=False)
    proxy_reputation.reset_for_tests()
    pool = proxy_pool.ProxyPool(
        urls=("us-east-only", "default-only"),
        regions={"us-east-only": "us-east", "default-only": "default"},
    )
    # No ``ru`` in the pool — pass 1 misses, pass 2 picks the
    # default-tagged URL.
    assert pool.acquire(preferred_region="ru", now=10.0) == "default-only"


def test_pool_acquire_weighted_skips_cooldown_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cooldown still excludes a URL on the weighted path so the Phase
    8b health latch composes correctly with Phase 9g reputation.
    """

    monkeypatch.setattr(settings, "enable_proxy_reputation", 1, raising=False)
    monkeypatch.setattr(settings, "proxy_warmup_window_days", 0, raising=False)
    monkeypatch.setattr(settings, "proxy_health_cooldown_sec", 60, raising=False)
    proxy_reputation.reset_for_tests()
    pool = proxy_pool.ProxyPool(urls=("a", "b"))
    # Mark ``a`` failed -> 60s cooldown. mark_failed itself feeds the
    # reputation tracker so ``a`` also has a 0/1 ratio, but the
    # cooldown is what matters here.
    pool.mark_failed("a", now=0.0)
    seen = {pool.acquire(now=5.0) for _ in range(4)}
    assert seen == {"b"}


# ---------------------------------------------------------------------------
# Module-level config knob shape
# ---------------------------------------------------------------------------


def test_settings_exposes_phase_9g_knobs() -> None:
    config = importlib.import_module("app.config")
    s = config.settings
    assert s.enable_proxy_reputation == 0
    assert s.proxy_warmup_window_days == 7


def test_admin_knobs_expose_phase_9g_keys() -> None:
    admin_settings = importlib.import_module("app.admin_settings")
    keys = admin_settings.ALLOWED_KEYS
    for k in ("enable_proxy_reputation", "proxy_warmup_window_days"):
        assert k in keys
