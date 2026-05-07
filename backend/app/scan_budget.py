"""Phase 9f — per-scan hard budget + ``SKIPPED`` bucket.

Closes residual risk **M5** (scan tail-latency runaway) from the Phase
8 deploy debrief: a long-tail of probes that take 30+ seconds (TLS
handshake retries, deeply paginated 503s, slow Tor exits) used to
hold the entire SSE stream open until every coroutine settled. Phase
8b's per-host token bucket and Phase 9a's anti-bot pipeline both
*amplify* the tail because they wait longer before giving up on a
hostile site, not less. Phase 9f pulls in a triple-budget model so
the operator can bound a worst-case scan to a known wall-clock and
the per-probe latency is capped independently of the network stack's
own timeout.

Three knobs — all default-off so the pre-9f scan behaviour is
preserved byte-for-byte:

* :attr:`backend.app.config.Settings.enable_scan_budget` — master
  switch. When 0, :func:`stream_checks` does not even instantiate a
  :class:`ScanBudget`, no per-probe wrapping happens, and the scan
  walk is identical to the pre-9f path.
* :attr:`backend.app.config.Settings.scan_max_total_sec` — hard
  ceiling on total wall-clock for the scan in seconds. Default 300.
  Once exceeded, every remaining site (sitting in the asyncio queue
  but not yet acquired by a worker) is bulk-marked :data:`Status.SKIPPED`
  with ``reason="scan_total_killed"`` and the SSE stream closes.
* :attr:`backend.app.config.Settings.scan_per_site_timeout_sec` —
  per-probe wall-clock cap on a single site's :func:`check_site` call.
  Default 8s. This is *separate* from the ``httpx`` connect/read
  timeouts because those don't include the time spent inside the
  Phase 5h verify probes / Phase 9a Playwright fallback /
  Phase 2.2 cffi rescue. When a probe exceeds this budget the site
  is recorded as :data:`Status.SKIPPED` with ``reason="probe_timeout"``.
* :attr:`backend.app.config.Settings.scan_max_concurrent_skips` —
  after this many *consecutive* probe-timeouts (without an intervening
  successful probe to reset the counter) the budget tripper concludes
  the network is wedged and bulk-skips every remaining site with
  ``reason="budget_exceeded"``. Default 50.

The :class:`ScanBudget` instance is per-scan and per-stream — it is
not shared across scans and has no state outside the lifetime of one
:func:`stream_checks` call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

# Reason strings stamped onto the :class:`backend.app.checker.CheckResult`
# ``reason`` field (Phase 9d wired the field; Phase 9f is the second
# major consumer after the CAPTCHA classifier).
REASON_SCAN_TOTAL_KILLED = "scan_total_killed"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_PROBE_TIMEOUT = "probe_timeout"

REASONS: frozenset[str] = frozenset(
    {
        REASON_SCAN_TOTAL_KILLED,
        REASON_BUDGET_EXCEEDED,
        REASON_PROBE_TIMEOUT,
    }
)


@dataclass
class ScanBudget:
    """Tracks one scan's elapsed time + sequential probe-timeout count.

    The instance is mutated by every worker via :meth:`record_skip` and
    :meth:`record_success`, so the counters are guarded by an internal
    :class:`threading.Lock`. The lock is cheap (one mutex acquire per
    probe end, never held across an ``await``) and keeps the
    consecutive-skip counter race-free under the asyncio fan-out.

    Construction is from :meth:`from_settings` which reads the four
    Phase 9f admin knobs off ``settings``. Direct instantiation is
    supported for tests.
    """

    enabled: bool = False
    max_total_sec: int = 300
    per_site_timeout_sec: int = 8
    max_concurrent_skips: int = 50
    started_at: float = field(default_factory=time.monotonic)
    _consecutive_skips: int = 0
    _total_skips: int = 0
    _total_probes: int = 0
    _lock: Lock = field(default_factory=Lock)
    _bulk_skip_armed: bool = False

    @classmethod
    def from_settings(cls, settings_obj: object) -> ScanBudget:
        """Build a :class:`ScanBudget` from the Phase 9f admin knobs.

        ``settings_obj`` is anything quacking like
        :class:`backend.app.config.Settings` — only the four knob
        attributes are read so unit tests can pass a stub.
        """

        return cls(
            enabled=bool(int(getattr(settings_obj, "enable_scan_budget", 0))),
            max_total_sec=int(getattr(settings_obj, "scan_max_total_sec", 300)),
            per_site_timeout_sec=int(getattr(settings_obj, "scan_per_site_timeout_sec", 8)),
            max_concurrent_skips=int(getattr(settings_obj, "scan_max_concurrent_skips", 50)),
        )

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def per_site_timeout(self) -> float | None:
        """Per-probe budget in seconds, or ``None`` when disabled.

        Returned as a ``float | None`` so the caller can pass it
        directly to :func:`asyncio.wait_for` — that helper treats
        ``None`` as "no timeout" which is the correct disabled-path
        behaviour.
        """

        if not self.enabled:
            return None
        return float(self.per_site_timeout_sec)

    @property
    def consecutive_skips(self) -> int:
        with self._lock:
            return self._consecutive_skips

    @property
    def total_skips(self) -> int:
        with self._lock:
            return self._total_skips

    @property
    def total_probes(self) -> int:
        with self._lock:
            return self._total_probes

    def elapsed_sec(self, now: float | None = None) -> float:
        """Wall-clock elapsed since :attr:`started_at`."""

        return (now if now is not None else time.monotonic()) - self.started_at

    def is_total_exceeded(self, now: float | None = None) -> bool:
        """``True`` when the total wall-clock budget is exhausted."""

        if not self.enabled:
            return False
        return self.elapsed_sec(now) >= float(self.max_total_sec)

    def should_bulk_skip(self) -> bool:
        """``True`` once the consecutive-skip threshold has been hit.

        After the threshold latches the budget is *armed*: subsequent
        :meth:`record_success` calls do NOT reset it (the signal is
        intended as a one-way trip — once we've decided the network is
        wedged, the rest of the scan is skipped). This matches the
        ROADMAP's "bulk-marked SKIPPED" intent — the operator re-runs
        the SKIPPED rows in a second scan rather than letting the
        first one limp through.
        """

        if not self.enabled:
            return False
        with self._lock:
            return self._bulk_skip_armed

    def record_skip(self) -> None:
        """Tick the consecutive-skip counter; arm bulk-skip when hit."""

        if not self.enabled:
            return
        with self._lock:
            self._consecutive_skips += 1
            self._total_skips += 1
            self._total_probes += 1
            if self._consecutive_skips >= self.max_concurrent_skips:
                self._bulk_skip_armed = True

    def record_success(self) -> None:
        """Reset the consecutive-skip counter (a successful probe means
        the network isn't wedged after all). Does NOT disarm the bulk-
        skip latch — see :meth:`should_bulk_skip`.
        """

        if not self.enabled:
            return
        with self._lock:
            self._consecutive_skips = 0
            self._total_probes += 1

    def force_bulk_skip(self) -> None:
        """Latch the bulk-skip arm without ticking the counter — used
        when the *total* wall-clock budget is exceeded so every still-
        queued worker emits a SKIPPED result rather than firing a probe.
        """

        if not self.enabled:
            return
        with self._lock:
            self._bulk_skip_armed = True

    def stats(self) -> dict[str, int | bool | float]:
        """Return a serializable snapshot of the current counters.

        Used by the SSE stream's terminal frame and by tests; never
        called on the hot path so the lock acquire is fine.
        """

        with self._lock:
            return {
                "enabled": self.enabled,
                "elapsed_sec": round(self.elapsed_sec(), 3),
                "max_total_sec": self.max_total_sec,
                "per_site_timeout_sec": self.per_site_timeout_sec,
                "max_concurrent_skips": self.max_concurrent_skips,
                "consecutive_skips": self._consecutive_skips,
                "total_skips": self._total_skips,
                "total_probes": self._total_probes,
                "bulk_skip_armed": self._bulk_skip_armed,
            }
