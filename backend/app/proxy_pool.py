"""Phase 8b: multi-proxy egress pool with health-aware selection.

Phase 5e's single ``proxy.proxy_url_or_none()`` is a great fit when the
operator has exactly one egress IP — the typical "OSINT through Tor"
setup. It falls over once the catalog crosses ~1k sites and the same
exit IP starts tripping per-IP rate limits on popular hosts (Reddit,
GitHub, Cloudflare-fronted forums) within a single scan.

Phase 8b adds a *pool* of proxy URLs the runtime can rotate through, so
no single egress IP carries the whole scan's load. The pool is
configured via the env var ``RECKORA_PROXY_POOL_URLS`` (comma-separated
URLs — same schemes httpx accepts: ``socks5://``, ``socks5h://``,
``http://``, ``https://``). The env stays env-only because each URL
may carry credentials; the runtime toggle lives in the admin
``enable_proxy_rotator`` knob so an operator can flip the pool on and
off without restarting.

Selection strategy
------------------
Round-robin with a short *health cooldown*: when the caller marks a
URL as failed, the pool skips it for ``proxy_health_cooldown_sec``
seconds before considering it again. If every URL is in cooldown the
pool returns ``None`` (direct egress) — this is the fail-open contract
for a degraded proxy fleet, since refusing every request would be
worse than briefly leaking the API process's IP. Operators who need
fail-closed semantics should run a circuit breaker upstream of
Reckora.

The pool intentionally has no "least recently used" or weighted
strategy: round-robin is dead-simple, the cooldown handles the worst
failure mode (a single proxy is consistently broken), and anything
fancier means a knob the operator has to tune. We can always lift this
to a strategy enum later.

Backward compatibility
----------------------
When ``enable_proxy_rotator=0`` (the default), the pool is dormant and
all callers fall back to ``proxy.proxy_url_or_none()`` which keeps the
Phase 5e single-proxy semantics intact. Existing deployments — and the
HF Space single container — see no behavioural change until an
operator explicitly turns the knob on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import cycle

from .config import settings


@dataclass(slots=True)
class _ProxyState:
    """Per-URL health record stored alongside the pool entry."""

    url: str
    cooldown_until: float = 0.0
    consecutive_failures: int = 0


@dataclass(slots=True)
class ProxyPool:
    """Round-robin pool of proxy URLs with a health-cooldown guard.

    Single instance is exposed as a module-level singleton via
    :func:`get_pool`. Construction parses the env list once; subsequent
    behaviour reads the live admin knob through
    :func:`_enabled_knob` so the toggle is hot-swappable.

    The class is intentionally thread-safe (``acquire`` and
    ``mark_failed`` take the same lock) so it can sit behind both the
    asyncio event loop and Celery's worker thread pool without the
    caller having to think about it.
    """

    urls: tuple[str, ...]
    _state: dict[str, _ProxyState] = field(default_factory=dict)
    _cursor: Iterator[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Stable iteration order; ``cycle`` over ``self.urls`` keeps
        # round-robin deterministic across calls.
        self._state = {u: _ProxyState(url=u) for u in self.urls}
        self._cursor = cycle(self.urls) if self.urls else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, *, now: float | None = None) -> str | None:
        """Return the next eligible proxy URL, or ``None`` for direct.

        ``None`` is returned when the pool is empty *or* every URL is
        currently in cooldown (fail-open, see module docstring).
        """
        if not self.urls or self._cursor is None:
            return None
        ts = now if now is not None else time.monotonic()
        with self._lock:
            for _ in range(len(self.urls)):
                url = next(self._cursor)
                state = self._state[url]
                if state.cooldown_until <= ts:
                    return url
        return None

    def mark_failed(self, url: str | None, *, now: float | None = None) -> None:
        """Record a failure against ``url`` and put it in cooldown.

        ``url=None`` (direct egress) is a no-op; the pool only tracks
        URLs it could have selected. The cooldown duration comes from
        the live ``proxy_health_cooldown_sec`` setting so an admin can
        widen the dead-window without restarting.
        """
        if url is None:
            return
        ts = now if now is not None else time.monotonic()
        cooldown = max(1.0, float(settings.proxy_health_cooldown_sec))
        with self._lock:
            state = self._state.get(url)
            if state is None:
                return
            state.consecutive_failures += 1
            state.cooldown_until = ts + cooldown

    def mark_success(self, url: str | None) -> None:
        """Reset the failure counter for ``url``.

        Cooldown is *not* shortened on success — once a URL is in the
        penalty box it serves out the full ``proxy_health_cooldown_sec``
        timer regardless of who else succeeded in the meantime.
        """
        if url is None:
            return
        with self._lock:
            state = self._state.get(url)
            if state is not None:
                state.consecutive_failures = 0

    def health(self) -> list[dict[str, object]]:
        """Return a JSON-friendly snapshot of every URL's health.

        Used by ``/api/health`` so an operator can spot a wedged proxy
        without tailing logs. Cooldown is reported as the number of
        seconds remaining (``0`` for healthy URLs).
        """
        ts = time.monotonic()
        out: list[dict[str, object]] = []
        with self._lock:
            for url in self.urls:
                state = self._state[url]
                remaining = max(0.0, state.cooldown_until - ts)
                out.append(
                    {
                        "url_scheme": url.split("://", 1)[0] if "://" in url else "",
                        "in_cooldown": remaining > 0,
                        "cooldown_remaining_sec": round(remaining, 2),
                        "consecutive_failures": state.consecutive_failures,
                    }
                )
        return out


# ---------------------------------------------------------------------------
# Module-level singleton + helpers.
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: ProxyPool | None = None


def _parse_pool_urls(raw: str) -> tuple[str, ...]:
    """Split a ``RECKORA_PROXY_POOL_URLS`` value into a tuple.

    Accepts comma-separated URLs with optional whitespace; empty parts
    and lone ``,`` are skipped so an operator can leave a trailing
    comma in their env file without breaking the pool.
    """
    parts = [p.strip() for p in (raw or "").split(",")]
    return tuple(p for p in parts if p)


def _build_pool() -> ProxyPool:
    return ProxyPool(urls=_parse_pool_urls(settings.proxy_pool_urls))


def get_pool() -> ProxyPool:
    """Return the lazy module-level pool singleton."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def reset_pool_for_tests() -> None:
    """Drop the singleton so tests can rebuild it after env edits.

    Production code never calls this; it exists purely so unit tests
    that patch ``settings.proxy_pool_urls`` get a fresh pool on every
    test instead of one another's leftover state.
    """
    global _pool
    with _pool_lock:
        _pool = None


def _enabled_knob() -> int:
    """Read the ``enable_proxy_rotator`` admin knob through the cache."""
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("enable_proxy_rotator")
    except Exception:
        return int(settings.enable_proxy_rotator)


def is_enabled() -> bool:
    """``True`` when the rotator is on **and** at least one URL is
    configured. Callers use this to gate transport selection."""
    return bool(get_pool().urls) and _enabled_knob() == 1


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health`` and the SPA."""
    pool = get_pool()
    return {
        "configured": len(pool.urls),
        "enabled": _enabled_knob() == 1,
        "active": is_enabled(),
        "members": pool.health(),
    }
