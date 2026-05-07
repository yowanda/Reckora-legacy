"""Phase 9g — proxy reputation warm-up + weighted selection.

Closes residual risk **M6** (round-robin pool ignores per-proxy reliability;
MEDIUM) from the Phase 8 deploy debrief: the Phase 8b ``ProxyPool`` rotates
URLs strictly round-robin and the Phase 9c geo-aware extension only adds a
region preference on top. Once a proxy starts intermittently failing the
short cooldown (``proxy_health_cooldown_sec``) keeps recycling it back into
the rotation at the same share as the healthy ones, so the same flaky
proxy keeps tripping the operator's host rate limits and burning probe
budget. Phase 9g adds a *reputation* signal — the running success ratio
per URL — and biases the pool selector toward proxies with a higher
ratio. New proxies get a configurable *warm-up window* (default 7 days)
during which they are treated as 1.0-weight regardless of failures so
they can collect enough samples to be ranked fairly.

Two independent admin knobs, both default-off so the Phase 8b round-robin
pool walk stays byte-for-byte identical when the operator hasn't opted in:

* :attr:`backend.app.config.Settings.enable_proxy_reputation` — master
  switch. ``0`` keeps :func:`backend.app.proxy_pool.ProxyPool.acquire`
  on the existing round-robin / three-pass region path.
* :attr:`backend.app.config.Settings.proxy_warmup_window_days` — number
  of days a freshly-seen URL stays at full warm-up weight before its
  recorded success ratio takes over. Default ``7``. Range ``0-365``.

Selection algorithm
-------------------
The reputation tracker uses **stride scheduling** (also known as
deficit-round-robin's continuous-weight cousin):

* Each URL has a ``virt_time`` cursor that starts at ``0``.
* Every :meth:`pick` returns the URL with the smallest ``virt_time`` and
  then increments it by ``stride = 1 / weight`` so a higher-weight URL
  has a smaller stride and therefore catches up to the next pick sooner.
* When weights are equal (e.g. every URL is in warm-up) every stride is
  the same and the picks fall in stable insertion order — i.e. the
  scheduler collapses to the cursor-order round-robin Phase 8b already
  ships, which keeps tests + integration cleanly deterministic.
* A URL whose post-warm-up weight drops below
  :data:`_WEIGHT_EPSILON` (a 100% failure rate) is excluded from the
  candidate set; the caller falls through to the next region tier or
  to direct egress. The Phase 8b cooldown latch is layered separately
  in :class:`backend.app.proxy_pool.ProxyPool` and still excludes URLs
  in the penalty box regardless of reputation, so a flaky proxy is
  skipped *for two reasons* when both signals fire.

The tracker is a singleton and uses :func:`time.monotonic` throughout so
restarts reset the warm-up clock cleanly (matching the lifetime of the
in-memory state itself). The class is thread-safe — every state
mutation acquires :class:`threading.Lock` and the lock is never held
across an ``await`` so the asyncio fan-out is safe to call into the
tracker from worker coroutines.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import settings

# Default warm-up window in days. Mirrors the
# ``proxy_warmup_window_days`` admin-knob default in
# :mod:`backend.app.config`.
_DEFAULT_WARMUP_DAYS: int = 7

# A URL whose effective weight drops below this floor is treated as
# "excluded" by :meth:`ProxyReputation.pick` — including it in the
# stride scheduler would either divide-by-zero on stride computation
# or perpetually starve the higher-weight URLs while it served out a
# pathological 0.001-weight rotation. The Phase 8b cooldown latch is
# the orthogonal mechanism that re-admits a healthy URL once a
# successful probe lands; reputation is a longer-horizon signal.
_WEIGHT_EPSILON: float = 1e-3

# Seconds in a day, factored out so unit tests can patch this constant
# instead of having to drive ``time.monotonic`` forward by 7 * 86400
# seconds to reach the post-warmup branch.
_SECONDS_PER_DAY: int = 86_400


@dataclass(slots=True)
class _RepState:
    """Per-URL reputation record stored alongside the pool entry.

    Lives in :class:`ProxyReputation._states`; instances are mutable
    under :attr:`ProxyReputation._lock`.
    """

    url: str
    successes: int = 0
    failures: int = 0
    first_seen: float = 0.0
    virt_time: float = 0.0


@dataclass
class ProxyReputation:
    """Tracks per-URL reputation + serves weighted picks.

    Construction is from :meth:`from_settings` which reads the two
    Phase 9g admin knobs off ``settings``. Direct instantiation is
    supported for unit tests that pass an explicit ``warmup_window_sec``.
    """

    enabled: bool = False
    warmup_window_sec: float = float(_DEFAULT_WARMUP_DAYS * _SECONDS_PER_DAY)
    _states: dict[str, _RepState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_settings(cls, settings_obj: object) -> ProxyReputation:
        """Build a :class:`ProxyReputation` from the Phase 9g admin knobs.

        ``settings_obj`` is anything quacking like
        :class:`backend.app.config.Settings` — only the two knob
        attributes are read so unit tests can pass a stub.
        """

        days = int(getattr(settings_obj, "proxy_warmup_window_days", _DEFAULT_WARMUP_DAYS))
        return cls(
            enabled=bool(int(getattr(settings_obj, "enable_proxy_reputation", 0))),
            warmup_window_sec=float(max(0, days) * _SECONDS_PER_DAY),
        )

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def _ensure_locked(self, url: str, *, now: float) -> _RepState:
        """Return / create the per-URL record. Caller holds ``self._lock``."""

        st = self._states.get(url)
        if st is None:
            st = _RepState(url=url, first_seen=now)
            self._states[url] = st
        return st

    def record_success(self, url: str | None, *, now: float | None = None) -> None:
        """Tick the success counter for ``url``.

        ``url=None`` (direct egress) is a no-op; the tracker only
        records URLs that came out of the pool.
        """

        if url is None:
            return
        ts = now if now is not None else time.monotonic()
        with self._lock:
            st = self._ensure_locked(url, now=ts)
            st.successes += 1

    def record_failure(self, url: str | None, *, now: float | None = None) -> None:
        """Tick the failure counter for ``url``.

        Same ``None``-is-noop contract as :meth:`record_success`.
        """

        if url is None:
            return
        ts = now if now is not None else time.monotonic()
        with self._lock:
            st = self._ensure_locked(url, now=ts)
            st.failures += 1

    # ------------------------------------------------------------------
    # Weight / pick
    # ------------------------------------------------------------------

    def _weight_locked(self, st: _RepState, *, now: float) -> float:
        """Compute weight while holding ``self._lock``."""

        if (now - st.first_seen) < self.warmup_window_sec:
            # Warm-up: full weight regardless of recorded outcome so a
            # fresh URL gets a fair share of the rotation while it
            # builds up enough samples to be ranked.
            return 1.0
        total = st.successes + st.failures
        if total == 0:
            # Post warm-up but no probes recorded — give the URL the
            # benefit of the doubt rather than starving it forever.
            return 1.0
        return st.successes / float(total)

    def weight(self, url: str, *, now: float | None = None) -> float:
        """Return the current weight for ``url`` in ``[0.0, 1.0]``.

        URLs with no recorded state yield ``1.0`` (they are still in
        warm-up by definition — :meth:`weight` is non-mutating so it
        does *not* register the URL with a ``first_seen`` timestamp;
        only :meth:`pick`, :meth:`record_success`, :meth:`record_failure`
        do that).
        """

        ts = now if now is not None else time.monotonic()
        with self._lock:
            st = self._states.get(url)
            if st is None:
                return 1.0
            return self._weight_locked(st, now=ts)

    def pick(
        self,
        candidates: list[str] | tuple[str, ...],
        *,
        now: float | None = None,
    ) -> str | None:
        """Return the next URL via stride-scheduled weighted selection.

        ``candidates`` is the list of URLs already filtered for
        cooldown / region eligibility by the caller. The chosen URL's
        ``virt_time`` is advanced by ``1 / weight`` so the scheduler
        is fair: a 1.0-weight URL advances by 1 each pick while a
        0.5-weight URL advances by 2, so it is picked half as often.

        Returns ``None`` when the candidate list is empty or every
        candidate's weight has decayed below :data:`_WEIGHT_EPSILON`
        — in which case the caller falls back through the
        :class:`backend.app.proxy_pool.ProxyPool` region tiers or
        direct egress, matching the Phase 8b fail-open contract.
        """

        if not candidates:
            return None
        ts = now if now is not None else time.monotonic()
        with self._lock:
            best: _RepState | None = None
            best_weight: float = 0.0
            for url in candidates:
                st = self._ensure_locked(url, now=ts)
                w = self._weight_locked(st, now=ts)
                if w < _WEIGHT_EPSILON:
                    continue
                if best is None or st.virt_time < best.virt_time:
                    best = st
                    best_weight = w
            if best is None:
                return None
            best.virt_time += 1.0 / max(best_weight, _WEIGHT_EPSILON)
            return best.url

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def snapshot(self, *, now: float | None = None) -> list[dict[str, object]]:
        """Return a JSON-friendly list of per-URL reputation rows.

        Surfaced via the admin-only ``/api/health.proxy_pool.reputation``
        path so an operator can see the success ratio per URL without
        tailing logs.
        """

        ts = now if now is not None else time.monotonic()
        with self._lock:
            rows: list[dict[str, object]] = []
            for st in self._states.values():
                rows.append(
                    {
                        "url_scheme": st.url.split("://", 1)[0] if "://" in st.url else "",
                        "successes": st.successes,
                        "failures": st.failures,
                        "weight": round(self._weight_locked(st, now=ts), 4),
                        "in_warmup": (ts - st.first_seen) < self.warmup_window_sec,
                    }
                )
            return rows


# ---------------------------------------------------------------------------
# Module-level singleton + helpers.
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_reputation: ProxyReputation | None = None


def get_reputation() -> ProxyReputation:
    """Return the lazy module-level reputation singleton."""

    global _reputation
    if _reputation is None:
        with _singleton_lock:
            if _reputation is None:
                _reputation = ProxyReputation.from_settings(settings)
    return _reputation


def reset_for_tests() -> None:
    """Drop the singleton so tests can rebuild it after env edits.

    Production code never calls this; it exists purely so unit tests
    that patch ``settings.enable_proxy_reputation`` /
    ``settings.proxy_warmup_window_days`` get a fresh tracker on every
    test instead of one another's leftover state.
    """

    global _reputation
    with _singleton_lock:
        _reputation = None


def _enabled_knob() -> int:
    """Read the ``enable_proxy_reputation`` admin knob through the cache."""

    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_proxy_reputation")
    except Exception:
        return int(settings.enable_proxy_reputation)


def is_enabled() -> bool:
    """``True`` when the reputation-aware selector is on.

    Mirror of :func:`backend.app.proxy_pool.is_enabled` shape — the
    caller in :class:`backend.app.proxy_pool.ProxyPool.acquire` uses
    this to gate the weighted pick path so the Phase 8b round-robin
    behaviour stays byte-for-byte identical when the knob is off.
    """

    return _enabled_knob() == 1
