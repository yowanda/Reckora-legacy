"""Tests for Phase 7d — ML drift detector helpers."""

from __future__ import annotations

import math
import random

import pytest

from app.ml_drift import (
    MIN_SAMPLE_SIZE,
    DriftReport,
    FeatureSummary,
    compute_drift,
    ks_two_sample,
    summarise,
)


class TestSummarise:
    def test_empty(self) -> None:
        s = summarise([])
        assert s == FeatureSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def test_single(self) -> None:
        s = summarise([3.0])
        assert s.count == 1
        assert s.mean == 3.0
        assert s.std == 0.0
        assert s.minimum == s.p50 == s.p90 == s.maximum == 3.0

    def test_basic(self) -> None:
        s = summarise([1.0, 2.0, 3.0, 4.0, 5.0])
        assert s.count == 5
        assert math.isclose(s.mean, 3.0)
        assert math.isclose(s.std, math.sqrt(2.0))
        assert s.minimum == 1.0
        assert s.maximum == 5.0
        # Percentile interpolation (linear) for 5 sorted values:
        # p50 == values[2] == 3.0
        # p90 of (5-1) = 3.6 -> idx 3 + 0.6*(5-4) -> 4.6
        assert math.isclose(s.p50, 3.0)
        assert math.isclose(s.p90, 4.6)


class TestKsTwoSample:
    def test_identical_distribution_yields_zero(self) -> None:
        a = [float(i) for i in range(100)]
        b = list(a)
        d, p = ks_two_sample(a, b)
        assert d == 0.0
        assert p == 1.0

    def test_disjoint_distributions_yield_one(self) -> None:
        a = [0.0] * 50
        b = [1.0] * 50
        d, p = ks_two_sample(a, b)
        assert d == pytest.approx(1.0, abs=1e-9)
        assert p < 0.001

    def test_empty_input_returns_no_drift(self) -> None:
        assert ks_two_sample([], [1.0, 2.0]) == (0.0, 1.0)
        assert ks_two_sample([1.0], []) == (0.0, 1.0)

    def test_partial_overlap(self) -> None:
        # Generate two roughly-similar distributions; KS should be small.
        rnd = random.Random(0)
        a = [rnd.gauss(0, 1) for _ in range(500)]
        b = [rnd.gauss(0, 1) for _ in range(500)]
        d, p = ks_two_sample(a, b)
        assert 0.0 < d < 0.2  # noisy but bounded
        assert 0.0 <= p <= 1.0

    def test_shifted_mean_detected(self) -> None:
        # A clean shift should produce a large statistic.
        rnd = random.Random(42)
        a = [rnd.gauss(0, 1) for _ in range(500)]
        b = [rnd.gauss(2, 1) for _ in range(500)]
        d, p = ks_two_sample(a, b)
        assert d > 0.5
        assert p < 1e-6

    def test_p_value_bounded(self) -> None:
        rnd = random.Random(7)
        a = [rnd.random() for _ in range(200)]
        b = [rnd.random() for _ in range(200)]
        d, p = ks_two_sample(a, b)
        assert 0.0 <= d <= 1.0
        assert 0.0 <= p <= 1.0


class TestComputeDrift:
    def _ramp(self, n: int, *, shift: float = 0.0) -> list[float]:
        return [i / n + shift for i in range(n)]

    def test_no_overlap_returns_no_features(self) -> None:
        report = compute_drift({"a": [1.0, 2.0]}, {"b": [3.0, 4.0]})
        assert report.features == ()
        assert report.any_alarm is False
        assert report.alarm_features == ()

    def test_skips_undersized_features(self) -> None:
        report = compute_drift(
            {"a": [1.0, 2.0]},
            {"a": [1.0, 2.0]},
        )
        assert len(report.features) == 1
        f = report.features[0]
        assert f.skipped is True
        assert f.alarm is False
        assert f.feature == "a"

    def test_alarms_on_large_drift(self) -> None:
        ref = self._ramp(MIN_SAMPLE_SIZE * 2)
        cur = self._ramp(MIN_SAMPLE_SIZE * 2, shift=10.0)
        report = compute_drift({"x": ref}, {"x": cur}, threshold=0.1)
        assert report.any_alarm is True
        assert "x" in report.alarm_features

    def test_no_alarm_on_identical(self) -> None:
        ref = self._ramp(MIN_SAMPLE_SIZE * 2)
        report = compute_drift({"x": ref}, {"x": list(ref)}, threshold=0.1)
        assert report.any_alarm is False
        assert report.alarm_features == ()
        # The feature is reported (not skipped) but with alarm=False.
        f = report.features[0]
        assert f.skipped is False
        assert f.alarm is False
        assert f.ks_statistic == 0.0

    def test_threshold_controls_alarm_state(self) -> None:
        ref = self._ramp(MIN_SAMPLE_SIZE * 2)
        cur = [v + 0.01 for v in ref]
        # Tiny shift, ks ~= 1/N. Strict threshold should alarm; loose
        # threshold should not.
        strict = compute_drift({"x": ref}, {"x": cur}, threshold=0.001)
        loose = compute_drift({"x": ref}, {"x": cur}, threshold=0.5)
        assert strict.any_alarm is True
        assert loose.any_alarm is False

    def test_to_dict_round_trips(self) -> None:
        ref = self._ramp(MIN_SAMPLE_SIZE * 2)
        cur = self._ramp(MIN_SAMPLE_SIZE * 2, shift=10.0)
        report = compute_drift({"x": ref}, {"x": cur}, threshold=0.1)
        out = report.to_dict()
        assert out["any_alarm"] is True
        assert out["threshold"] == 0.1
        assert out["alarm_features"] == ["x"]
        assert isinstance(out["features"], list)
        feat = out["features"][0]
        assert feat["feature"] == "x"
        assert feat["alarm"] is True
        assert feat["skipped"] is False
        assert feat["reference"]["count"] == MIN_SAMPLE_SIZE * 2
        assert feat["current"]["count"] == MIN_SAMPLE_SIZE * 2


class TestDriftReportProps:
    def test_default_no_features_means_no_alarm(self) -> None:
        report = DriftReport()
        assert report.any_alarm is False
        assert report.alarm_features == ()
