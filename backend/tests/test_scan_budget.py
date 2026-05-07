"""Phase 9f tests — per-scan hard budget + ``SKIPPED`` bucket.

Two layers of coverage:

* Pure :class:`backend.app.scan_budget.ScanBudget` unit tests (no
  asyncio, no I/O): the consecutive-skip latching, the
  ``record_success`` reset path, the ``elapsed_sec`` clock, the
  default-off no-op contract, and the :meth:`stats` snapshot shape.
* End-to-end ``stream_checks`` integration tests that exercise the
  worker wrapping in :mod:`backend.app.checker` against a stub
  ``check_site`` so we don't need real HTTP traffic.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any
from unittest.mock import patch

import pytest

from app import scan_budget as budget_mod
from app.checker import CheckResult, Site, Status, stream_checks


class _StubSettings:
    """Minimal stand-in for :class:`backend.app.config.Settings`."""

    def __init__(
        self,
        *,
        enable_scan_budget: int = 0,
        scan_max_total_sec: int = 300,
        scan_per_site_timeout_sec: int = 8,
        scan_max_concurrent_skips: int = 50,
    ) -> None:
        self.enable_scan_budget = enable_scan_budget
        self.scan_max_total_sec = scan_max_total_sec
        self.scan_per_site_timeout_sec = scan_per_site_timeout_sec
        self.scan_max_concurrent_skips = scan_max_concurrent_skips


def _site(name: str = "Acme") -> Site:
    return Site(
        name=name,
        url=f"https://{name.lower()}.example.com/{{username}}",
        url_main=f"https://{name.lower()}.example.com/",
        check_type="status_code",
    )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_reasons_frozenset_lists_three_distinct_values() -> None:
    assert {
        budget_mod.REASON_SCAN_TOTAL_KILLED,
        budget_mod.REASON_BUDGET_EXCEEDED,
        budget_mod.REASON_PROBE_TIMEOUT,
    } == budget_mod.REASONS
    assert budget_mod.REASON_SCAN_TOTAL_KILLED == "scan_total_killed"
    assert budget_mod.REASON_BUDGET_EXCEEDED == "budget_exceeded"
    assert budget_mod.REASON_PROBE_TIMEOUT == "probe_timeout"


# ---------------------------------------------------------------------------
# from_settings + default-off contract
# ---------------------------------------------------------------------------


def test_from_settings_defaults_to_disabled_with_safe_caps() -> None:
    b = budget_mod.ScanBudget.from_settings(_StubSettings())
    assert b.is_enabled is False
    assert b.max_total_sec == 300
    assert b.per_site_timeout_sec == 8
    assert b.max_concurrent_skips == 50
    # Disabled budget is a no-op for every gating method.
    assert b.is_total_exceeded() is False
    assert b.should_bulk_skip() is False
    assert b.per_site_timeout is None
    b.record_skip()
    b.record_success()
    b.force_bulk_skip()
    assert b.consecutive_skips == 0
    assert b.total_skips == 0
    assert b.total_probes == 0


def test_from_settings_reads_all_four_knobs_when_enabled() -> None:
    b = budget_mod.ScanBudget.from_settings(
        _StubSettings(
            enable_scan_budget=1,
            scan_max_total_sec=120,
            scan_per_site_timeout_sec=4,
            scan_max_concurrent_skips=10,
        )
    )
    assert b.is_enabled is True
    assert b.max_total_sec == 120
    assert b.per_site_timeout_sec == 4
    assert b.max_concurrent_skips == 10
    assert b.per_site_timeout == 4.0


# ---------------------------------------------------------------------------
# record_skip / record_success / should_bulk_skip
# ---------------------------------------------------------------------------


def test_record_skip_ticks_consecutive_and_total_counters() -> None:
    b = budget_mod.ScanBudget(enabled=True, max_concurrent_skips=3)
    b.record_skip()
    b.record_skip()
    assert b.consecutive_skips == 2
    assert b.total_skips == 2
    assert b.total_probes == 2
    assert b.should_bulk_skip() is False


def test_record_success_resets_consecutive_but_keeps_total() -> None:
    b = budget_mod.ScanBudget(enabled=True, max_concurrent_skips=3)
    b.record_skip()
    b.record_skip()
    b.record_success()
    assert b.consecutive_skips == 0
    assert b.total_skips == 2  # cumulative counter survives
    assert b.total_probes == 3  # success also ticks total_probes


def test_should_bulk_skip_latches_at_threshold_and_stays_armed() -> None:
    b = budget_mod.ScanBudget(enabled=True, max_concurrent_skips=3)
    b.record_skip()
    b.record_skip()
    assert b.should_bulk_skip() is False
    b.record_skip()  # third skip — latches
    assert b.should_bulk_skip() is True
    # A successful probe DOES NOT disarm — the bulk-skip is one-way.
    b.record_success()
    assert b.should_bulk_skip() is True


def test_force_bulk_skip_arms_without_ticking_counters() -> None:
    b = budget_mod.ScanBudget(enabled=True, max_concurrent_skips=50)
    b.force_bulk_skip()
    assert b.should_bulk_skip() is True
    assert b.consecutive_skips == 0
    assert b.total_skips == 0


# ---------------------------------------------------------------------------
# is_total_exceeded — clock arithmetic
# ---------------------------------------------------------------------------


def test_is_total_exceeded_uses_monotonic_clock() -> None:
    b = budget_mod.ScanBudget(enabled=True, max_total_sec=5, started_at=100.0)
    assert b.is_total_exceeded(now=104.0) is False
    assert b.is_total_exceeded(now=104.999) is False
    assert b.is_total_exceeded(now=105.0) is True
    assert b.is_total_exceeded(now=200.0) is True


def test_elapsed_sec_returns_floating_seconds_since_start() -> None:
    b = budget_mod.ScanBudget(enabled=True, started_at=100.0)
    assert b.elapsed_sec(now=102.5) == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# stats snapshot
# ---------------------------------------------------------------------------


def test_stats_snapshot_includes_all_documented_keys() -> None:
    b = budget_mod.ScanBudget(
        enabled=True,
        max_total_sec=120,
        per_site_timeout_sec=4,
        max_concurrent_skips=10,
    )
    b.record_skip()
    b.record_skip()
    snap = b.stats()
    assert snap["enabled"] is True
    assert snap["max_total_sec"] == 120
    assert snap["per_site_timeout_sec"] == 4
    assert snap["max_concurrent_skips"] == 10
    assert snap["consecutive_skips"] == 2
    assert snap["total_skips"] == 2
    assert snap["total_probes"] == 2
    assert snap["bulk_skip_armed"] is False
    assert isinstance(snap["elapsed_sec"], float)


# ---------------------------------------------------------------------------
# stream_checks integration — default-off path is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_checks_default_off_does_not_emit_skipped_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = [_site(f"Site{i}") for i in range(3)]

    async def _fake_check_site(*args: Any, **kwargs: Any) -> CheckResult:
        # Pull the ``site`` positional out of args — the real check_site
        # signature is (client, site, username, *, ...).
        site = args[1]
        return CheckResult(
            site=site.name,
            status=Status.UNKNOWN,
            url=site.url.replace("{username}", "alice"),
            url_main=site.url_main,
            tags=list(site.tags),
        )

    monkeypatch.setattr("app.checker.check_site", _fake_check_site)
    # Force the default-off path explicitly.
    monkeypatch.setattr("app.checker.settings.enable_scan_budget", 0, raising=False)
    monkeypatch.setattr("app.checker.settings.use_celery", 0, raising=False)

    results: list[CheckResult] = []
    async for r in stream_checks(sites, "alice", verify=False, extract=False):
        results.append(r)

    assert len(results) == 3
    for r in results:
        assert r.status is Status.UNKNOWN
        assert r.reason is None


# ---------------------------------------------------------------------------
# stream_checks integration — per-probe timeout emits SKIPPED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_checks_with_budget_emits_skipped_on_per_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = [_site("Slow")]

    async def _slow_check_site(*args: Any, **kwargs: Any) -> CheckResult:
        await asyncio.sleep(5.0)  # well past the 1-sec per-site cap below
        raise AssertionError("should never reach here")

    monkeypatch.setattr("app.checker.check_site", _slow_check_site)
    monkeypatch.setattr("app.checker.settings.use_celery", 0, raising=False)
    monkeypatch.setattr("app.checker.settings.enable_scan_budget", 1, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_total_sec", 60, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_per_site_timeout_sec", 1, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_concurrent_skips", 50, raising=False)

    results: list[CheckResult] = []
    async for r in stream_checks(sites, "alice", verify=False, extract=False):
        results.append(r)

    assert len(results) == 1
    assert results[0].status is Status.SKIPPED
    assert results[0].reason == budget_mod.REASON_PROBE_TIMEOUT


# ---------------------------------------------------------------------------
# stream_checks integration — bulk-skip latches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_checks_with_low_threshold_bulk_skips_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five sites + a per-probe timeout that always fires + a
    threshold of 2 → after two probe-timeouts the budget latches and
    the remaining three emit ``reason="budget_exceeded"`` without
    waiting their full per-probe budget.
    """

    sites = [_site(f"Slow{i}") for i in range(5)]

    async def _always_slow(*args: Any, **kwargs: Any) -> CheckResult:
        await asyncio.sleep(5.0)
        raise AssertionError("should never reach here")

    monkeypatch.setattr("app.checker.check_site", _always_slow)
    monkeypatch.setattr("app.checker.settings.use_celery", 0, raising=False)
    monkeypatch.setattr("app.checker.settings.enable_scan_budget", 1, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_total_sec", 60, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_per_site_timeout_sec", 1, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_concurrent_skips", 2, raising=False)
    # Force fan-out to be sequential so the consecutive-skip counter
    # latches deterministically — otherwise five workers race against
    # each other and some may emit before the latch arms.
    monkeypatch.setattr("app.checker.settings.total_concurrency", 1, raising=False)

    results: list[CheckResult] = []
    async for r in stream_checks(sites, "alice", verify=False, extract=False):
        results.append(r)

    assert len(results) == 5
    statuses = [r.status for r in results]
    reasons = [r.reason for r in results]
    assert all(s is Status.SKIPPED for s in statuses)
    # First two skipped via probe_timeout (real wait-for triggers); the
    # remaining three skipped via the bulk-skip latch.
    assert reasons[:2] == [
        budget_mod.REASON_PROBE_TIMEOUT,
        budget_mod.REASON_PROBE_TIMEOUT,
    ]
    assert all(r == budget_mod.REASON_BUDGET_EXCEEDED for r in reasons[2:])


# ---------------------------------------------------------------------------
# stream_checks integration — total-budget kill path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_checks_total_budget_zero_skips_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run with ``scan_max_total_sec=1`` and a fast ``check_site`` that
    sleeps 1.5 seconds so the elapsed budget is already exceeded by
    the time the second worker enters its async-with-sem block. The
    second worker should emit ``reason="scan_total_killed"`` rather
    than calling ``check_site``.
    """

    sites = [_site(f"Site{i}") for i in range(3)]

    async def _one_sec_check(*args: Any, **kwargs: Any) -> CheckResult:
        site = args[1]
        await asyncio.sleep(1.5)
        return CheckResult(
            site=site.name,
            status=Status.UNKNOWN,
            url=site.url.replace("{username}", "alice"),
            url_main=site.url_main,
            tags=list(site.tags),
        )

    monkeypatch.setattr("app.checker.check_site", _one_sec_check)
    monkeypatch.setattr("app.checker.settings.use_celery", 0, raising=False)
    monkeypatch.setattr("app.checker.settings.enable_scan_budget", 1, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_total_sec", 1, raising=False)
    # Per-site timeout > total budget so the limit that fires is the
    # total wall-clock cap, not the per-probe one.
    monkeypatch.setattr("app.checker.settings.scan_per_site_timeout_sec", 60, raising=False)
    monkeypatch.setattr("app.checker.settings.scan_max_concurrent_skips", 50, raising=False)
    monkeypatch.setattr("app.checker.settings.total_concurrency", 1, raising=False)

    results: list[CheckResult] = []
    async for r in stream_checks(sites, "alice", verify=False, extract=False):
        results.append(r)

    assert len(results) == 3
    # First worker completes successfully (budget not yet exceeded
    # when it enters), then the elapsed budget exceeds 1s and the
    # other two are bulk-skipped via REASON_SCAN_TOTAL_KILLED.
    assert results[0].status is Status.UNKNOWN
    for r in results[1:]:
        assert r.status is Status.SKIPPED
        assert r.reason in {
            budget_mod.REASON_SCAN_TOTAL_KILLED,
            budget_mod.REASON_BUDGET_EXCEEDED,
        }


# ---------------------------------------------------------------------------
# CheckResult.reason round-trip on SKIPPED rows
# ---------------------------------------------------------------------------


def test_check_result_reason_round_trips_through_to_dict() -> None:
    site = _site("Acme")
    r = CheckResult(
        site=site.name,
        status=Status.SKIPPED,
        url=site.url.replace("{username}", "alice"),
        url_main=site.url_main,
        tags=list(site.tags),
        reason=budget_mod.REASON_BUDGET_EXCEEDED,
    )
    d = r.to_dict()
    assert d["status"] == "skipped"
    assert d["reason"] == budget_mod.REASON_BUDGET_EXCEEDED


# ---------------------------------------------------------------------------
# Module-level config knob shape — verify Settings exposes the four knobs
# ---------------------------------------------------------------------------


def test_settings_exposes_all_four_phase_9f_knobs() -> None:
    # Import inside the test so the assertion describes the
    # post-import state of the Settings module.
    config = importlib.import_module("app.config")
    s = config.settings
    assert s.enable_scan_budget == 0
    assert s.scan_max_total_sec == 300
    assert s.scan_per_site_timeout_sec == 8
    assert s.scan_max_concurrent_skips == 50


def test_admin_knobs_expose_all_four_phase_9f_keys() -> None:
    admin_settings = importlib.import_module("app.admin_settings")
    keys = admin_settings.ALLOWED_KEYS
    for k in (
        "enable_scan_budget",
        "scan_max_total_sec",
        "scan_per_site_timeout_sec",
        "scan_max_concurrent_skips",
    ):
        assert k in keys


# ---------------------------------------------------------------------------
# unused import guard (pytest happy path)
# ---------------------------------------------------------------------------


# ``patch`` is imported above so that future tests can stub the timer
# clock if needed; reference it once to keep ruff happy without
# silencing the F401 lint.
_ = patch
