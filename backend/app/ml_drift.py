"""Phase 7d — ML reranker drift detector.

Pure-Python statistical helpers for comparing two feature
distributions (the *reference* set the model was trained on vs. the
*current* set it's seeing in production) and surfacing a per-feature
drift score plus a simple "alarm" flag.

Why pure Python? The runtime already imports ``onnxruntime``,
``numpy`` and ``sklearn`` for the ML reranker itself, but the drift
detector runs from the admin panel - possibly on a slimmed-down image
without scipy. Two-sample Kolmogorov-Smirnov is a small enough
algorithm to implement directly (sort + linear scan), and the
asymptotic p-value approximation is one closed-form expression.

Public API
----------
* :class:`FeatureSummary` — descriptive stats (count, mean, std,
  percentiles) for a single feature.
* :class:`FeatureDrift` — per-feature drift verdict (KS statistic,
  p-value, alarm boolean).
* :class:`DriftReport` — aggregate of per-feature drifts plus a
  rolled-up "any feature alarmed" flag.
* :func:`summarise` — turn a flat list of floats into a
  :class:`FeatureSummary`.
* :func:`ks_two_sample` — KS statistic + asymptotic p-value for two
  samples.
* :func:`compute_drift` — top-level entry point: takes two
  ``{feature_name: list[float]}`` mappings and returns a
  :class:`DriftReport`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Default thresholds. ``ks_alarm_threshold`` is the KS statistic above
# which a feature is flagged. 0.1 is a reasonable starting point for
# the small (~10k) row counts the synthetic training set produces;
# admins can tune it via the ``ml_drift_ks_threshold`` knob.
DEFAULT_KS_THRESHOLD: float = 0.1
# Minimum sample size to run a test on. Below this the KS statistic
# is too noisy to be useful and we silently skip the feature.
MIN_SAMPLE_SIZE: int = 30


@dataclass(frozen=True)
class FeatureSummary:
    """Five-number summary plus mean / std for a single feature."""

    count: int
    mean: float
    std: float
    minimum: float
    p50: float
    p90: float
    maximum: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "mean": self.mean,
            "std": self.std,
            "min": self.minimum,
            "p50": self.p50,
            "p90": self.p90,
            "max": self.maximum,
        }


@dataclass(frozen=True)
class FeatureDrift:
    """Per-feature KS verdict.

    ``alarm`` is ``True`` when the KS statistic exceeds the configured
    threshold *and* both samples meet :data:`MIN_SAMPLE_SIZE`.
    ``skipped`` is ``True`` when either sample was too small to run
    the test reliably (no alarm raised in that case).
    """

    feature: str
    ks_statistic: float
    p_value: float
    alarm: bool
    skipped: bool
    reference: FeatureSummary | None
    current: FeatureSummary | None

    def to_dict(self) -> dict[str, float | bool | str | dict[str, float | int] | None]:
        return {
            "feature": self.feature,
            "ks_statistic": self.ks_statistic,
            "p_value": self.p_value,
            "alarm": self.alarm,
            "skipped": self.skipped,
            "reference": self.reference.to_dict() if self.reference else None,
            "current": self.current.to_dict() if self.current else None,
        }


@dataclass(frozen=True)
class DriftReport:
    """Aggregate drift report across every shared feature."""

    features: tuple[FeatureDrift, ...] = field(default_factory=tuple)
    threshold: float = DEFAULT_KS_THRESHOLD

    @property
    def any_alarm(self) -> bool:
        return any(f.alarm for f in self.features)

    @property
    def alarm_features(self) -> tuple[str, ...]:
        return tuple(f.feature for f in self.features if f.alarm)

    def to_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "any_alarm": self.any_alarm,
            "alarm_features": list(self.alarm_features),
            "features": [f.to_dict() for f in self.features],
        }


# ---------------------------------------------------------------------------
# Pure stats helpers.
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile (NumPy's default 'linear' style).

    ``sorted_values`` MUST already be sorted. ``q`` is in 0..1.
    """

    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[low]
    frac = pos - low
    return sorted_values[low] + frac * (sorted_values[high] - sorted_values[low])


def summarise(values: list[float]) -> FeatureSummary:
    """Compute count / mean / std / min / p50 / p90 / max for ``values``.

    Empty input produces a zero-filled summary; callers should check
    ``count`` before using the rest.
    """

    n = len(values)
    if n == 0:
        return FeatureSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(values) / n
    if n == 1:
        std = 0.0
    else:
        # Population variance is fine for descriptive purposes.
        var = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(var)
    sorted_vals = sorted(values)
    return FeatureSummary(
        count=n,
        mean=mean,
        std=std,
        minimum=sorted_vals[0],
        p50=_percentile(sorted_vals, 0.5),
        p90=_percentile(sorted_vals, 0.9),
        maximum=sorted_vals[-1],
    )


def ks_two_sample(a: list[float], b: list[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov statistic + asymptotic p-value.

    Returns ``(0.0, 1.0)`` when either sample is empty so callers can
    treat that as "no evidence of drift".

    The p-value uses the standard asymptotic formula
    ``p = 2 · Σ_{k=1..∞} (-1)^{k-1} · exp(-2k²λ²)`` where
    ``λ = (sqrt(n) + 0.12 + 0.11 / sqrt(n)) · D`` with effective
    ``n = m * n / (m + n)``. We sum the first ``KS_PVAL_TERMS`` terms
    of the alternating series — the contribution of subsequent terms
    is well below 1e-9 even for ``D ≈ 0.05``, more than enough
    precision for an alarm.
    """

    if not a or not b:
        return 0.0, 1.0
    sa = sorted(a)
    sb = sorted(b)
    na = len(sa)
    nb = len(sb)
    # Sweep over the union of unique sample values; at each x compare
    # the empirical CDFs F_a(x) and F_b(x) after consuming all samples
    # ≤ x from each side. This handles ties correctly — both CDFs
    # jump simultaneously to the same plateau when a tied value is
    # exhausted on both sides.
    i = j = 0
    fa = fb = 0.0
    d = 0.0
    while i < na and j < nb:
        if sa[i] < sb[j]:
            x = sa[i]
        elif sb[j] < sa[i]:
            x = sb[j]
        else:
            x = sa[i]
        while i < na and sa[i] == x:
            i += 1
        while j < nb and sb[j] == x:
            j += 1
        fa = i / na
        fb = j / nb
        gap = abs(fa - fb)
        if gap > d:
            d = gap
    # Tail: at this point either sa or sb is exhausted; the surviving
    # CDF stays at its terminal value 1.0 while the other catches up.
    if i < na:
        fb = 1.0
        while i < na:
            x = sa[i]
            while i < na and sa[i] == x:
                i += 1
            fa = i / na
            gap = abs(fa - fb)
            if gap > d:
                d = gap
    if j < nb:
        fa = 1.0
        while j < nb:
            x = sb[j]
            while j < nb and sb[j] == x:
                j += 1
            fb = j / nb
            gap = abs(fa - fb)
            if gap > d:
                d = gap
    # Asymptotic p-value (Stephens 1970).
    n_eff = (na * nb) / (na + nb)
    sqrt_n = math.sqrt(n_eff)
    lam = (sqrt_n + 0.12 + 0.11 / sqrt_n) * d
    if lam < 1e-12:
        p = 1.0
    else:
        p = 0.0
        for k in range(1, 101):
            term = 2.0 * ((-1) ** (k - 1)) * math.exp(-2.0 * (k * lam) ** 2)
            p += term
            if abs(term) < 1e-12:
                break
        p = max(0.0, min(1.0, p))
    return d, p


def compute_drift(
    reference: dict[str, list[float]],
    current: dict[str, list[float]],
    *,
    threshold: float = DEFAULT_KS_THRESHOLD,
    min_samples: int = MIN_SAMPLE_SIZE,
) -> DriftReport:
    """Compare two feature mappings and return a :class:`DriftReport`.

    Only features present in *both* mappings are scored. Features with
    fewer than ``min_samples`` rows on either side are flagged
    ``skipped=True`` (no alarm).
    """

    drifts: list[FeatureDrift] = []
    for name in sorted(set(reference) & set(current)):
        ref_vals = reference[name]
        cur_vals = current[name]
        ref_summary = summarise(ref_vals)
        cur_summary = summarise(cur_vals)
        if len(ref_vals) < min_samples or len(cur_vals) < min_samples:
            drifts.append(
                FeatureDrift(
                    feature=name,
                    ks_statistic=0.0,
                    p_value=1.0,
                    alarm=False,
                    skipped=True,
                    reference=ref_summary,
                    current=cur_summary,
                )
            )
            continue
        d, p = ks_two_sample(ref_vals, cur_vals)
        drifts.append(
            FeatureDrift(
                feature=name,
                ks_statistic=d,
                p_value=p,
                alarm=d >= threshold,
                skipped=False,
                reference=ref_summary,
                current=cur_summary,
            )
        )
    return DriftReport(features=tuple(drifts), threshold=threshold)
