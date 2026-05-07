"""Phase 9b: Redis-backed distributed host rate limiter.

Closes residual risk **M1** from the Phase 8 deploy debrief — the
Phase 8b ``host_rate_limit.HostRateLimiter`` is process-local, so a
Phase 8a Celery fan-out gives every worker its *own* token bucket
and the effective rate against a host scales linearly with worker
count.

Design choices
--------------

* **Sliding window via per-second buckets**, not a token bucket.
  Sliding window is the natural fit for "no more than R requests
  per minute" because it doesn't need a refill thread — each
  acquire just sums the last ``window_sec`` seconds of buckets.
  We trade burst absorption (Phase 8b's token bucket lets a quiet
  host take 60 requests at once if it had been idle) for a
  cleaner steady-state under fan-out, which is exactly the right
  trade-off when the limiter's job is "be a good citizen across N
  workers".

* **One atomic Lua script** for ``incr-bucket-and-decide``. The
  script reads the per-second buckets that fall in the window,
  sums them, decides "allow / wait", increments the current
  bucket on allow, and sets ``EXPIRE`` so dead buckets garbage-
  collect themselves. Everything is wrapped in a single
  ``EVAL`` round-trip so two workers cannot both observe ``count
  < limit`` and then both increment past it — the classic
  ``check-then-set`` race.

* **Synchronous redis-py client + ``run_in_executor``** rather
  than ``redis.asyncio``. The Lua script is the only round-trip
  on the hot path and ``redis-py`` >= 5.x has a robust sync
  client; using the sync client + an executor keeps the API
  surface tidy (no async-context issues) while still letting an
  ``asyncio`` caller await the result. The executor cost (~5 µs)
  is dominated by the Redis round-trip itself (~150 µs on a
  loopback redis-stack).

* **Fail-open on Redis error**. If ``EVAL`` raises (connection
  error, timed-out script, OOM, anything), we swallow the
  exception and let the caller's in-memory bucket take over. A
  Redis outage briefly making us "too fast" is much better than
  fail-closed (every probe stuck waiting forever). The catching
  layer lives in :mod:`app.host_rate_limit` so the public
  ``acquire(host)`` coroutine has the same signature regardless
  of backend.

The module is **opt-in** via ``host_rate_limit_use_redis=1`` (admin
knob) plus a connection string in ``RECKORA_HOST_RATE_LIMIT_REDIS_URL``
(falling back to the Phase 8a Celery broker URL when blank). With
both knobs at 0 / "" the module is never imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from typing import Any, cast

from .config import settings


class RedisUnavailableError(RuntimeError):
    """Raised when Redis cannot be reached or the redis package is missing.

    The :func:`app.host_rate_limit.acquire` coroutine catches this
    and falls back to the in-memory bucket.
    """


def _is_noscript_class(exc: BaseException) -> bool:
    """``True`` iff ``exc`` is an instance of ``redis.exceptions.NoScriptError``.

    Imported lazily so the rest of the module remains usable when
    ``redis`` is not installed (the ImportError surfaces from
    :meth:`RedisHostRateLimiter._ensure_client` instead).
    """
    try:
        from redis.exceptions import NoScriptError
    except ImportError:
        return False
    return isinstance(exc, NoScriptError)


# Lua script: atomic per-host sliding-window admission decision.
#
# KEYS[1] = redis hash key for the host's per-second buckets.
# ARGV[1] = current second (epoch int).
# ARGV[2] = window_sec (int, e.g. 60).
# ARGV[3] = limit (int, e.g. 60).
#
# Returns:
#   {1, 0}     allowed; 0 = "no wait, dispatch immediately"
#   {0, ms}    rejected; ms = milliseconds to wait before the
#              caller should re-try the script.
#
# Implementation notes:
# * Sums every per-second bucket whose key (a stringified second
#   value) falls in ``[now - window + 1, now]``.
# * On allow, ``HINCRBY KEYS[1] now 1`` records the new request
#   and ``EXPIRE KEYS[1] window+1`` keeps the hash alive past the
#   end of the window so a slow client can still see its own
#   recent traffic. ``+1`` of slack so a clock-rollover-on-the-
#   second doesn't drop the freshest bucket.
# * On reject, the script does *not* mutate the hash, so
#   re-trying after the recommended wait is safe.
_LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

local oldest = now - window + 1
local count = 0
local oldest_in_window = nil

local entries = redis.call('HGETALL', key)
for i = 1, #entries, 2 do
  local sec = tonumber(entries[i])
  if sec ~= nil and sec >= oldest and sec <= now then
    count = count + tonumber(entries[i + 1])
    if oldest_in_window == nil or sec < oldest_in_window then
      oldest_in_window = sec
    end
  end
end

if count < limit then
  redis.call('HINCRBY', key, tostring(now), 1)
  redis.call('EXPIRE', key, window + 1)
  return {1, 0}
end

-- Caller should retry after the *oldest in-window* bucket
-- falls out of the window. ``now - oldest + 1 == window`` is
-- the steady state, so the wait is at most ~window seconds and
-- usually a small fraction of it.
local wait_sec = (oldest_in_window or oldest) + window - now
if wait_sec < 0 then
  wait_sec = 0
end
return {0, wait_sec * 1000}
"""


class RedisHostRateLimiter:
    """Distributed sliding-window limiter backed by Redis.

    Construct with an explicit ``redis_url`` + ``rate_per_min``, or
    leave them as ``None`` and the live :data:`config.settings`
    values are read at acquire time so an admin can tune the rate
    on the fly without recreating the limiter.

    The Redis client is opened lazily on first acquire so an
    inactive limiter never holds a connection. ``aclose`` releases
    the client; calling ``aclose`` on a never-used limiter is a
    no-op.
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        rate_per_min: int | None = None,
        window_sec: int = 60,
        key_prefix: str = "reckora:hrl:",
    ) -> None:
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        self._explicit_url = redis_url
        self._explicit_rate = rate_per_min
        self._window_sec = int(window_sec)
        self._key_prefix = key_prefix
        self._client: Any | None = None
        self._script_sha: str | None = None
        self._client_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, host: str) -> None:
        """Block until one slot is available for ``host`` in Redis.

        Raises :class:`RedisUnavailableError` on any Redis transport
        error so the caller can fall back to the in-memory bucket.
        """
        if not host:
            return
        if self._closed:
            raise RedisUnavailableError("limiter is closed")

        normalised = host.lower()
        # Re-read the rate per call so an admin can tune it live.
        rate = self._effective_rate_per_min()
        loop = asyncio.get_running_loop()
        # Bound the retry tail so a very narrow rate (e.g. 1/min)
        # still terminates instead of looping forever in a fast
        # test. ``rate`` of 1/min puts the worst-case tail at
        # ``window_sec`` seconds, so two windows is a generous
        # ceiling that still surfaces a misconfiguration loudly.
        deadline = time.monotonic() + 2 * self._window_sec
        while True:
            try:
                allowed, wait_ms = await loop.run_in_executor(
                    None,
                    self._eval_script,
                    normalised,
                    rate,
                )
            except RedisUnavailableError:
                raise
            except Exception as exc:  # pragma: no cover — paranoia
                raise RedisUnavailableError(str(exc)) from exc

            if allowed:
                return
            wait_for = max(0.0, wait_ms / 1000.0)
            if wait_for == 0:
                # Defensive — Lua said "wait 0ms" but did not allow
                # us. Sleep a small fraction of the bucket width to
                # avoid a tight loop.
                wait_for = 1.0 / max(rate, 1) * 60.0 / 4.0
            if time.monotonic() + wait_for > deadline:
                raise RedisUnavailableError("host rate limit acquire exceeded retry budget")
            await asyncio.sleep(wait_for)

    async def aclose(self) -> None:
        """Release the underlying Redis client. Idempotent."""
        if self._closed:
            return
        self._closed = True
        client = self._client
        self._client = None
        self._script_sha = None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, client.close)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _effective_rate_per_min(self) -> int:
        if self._explicit_rate is not None:
            return max(int(self._explicit_rate), 1)
        from . import admin_settings as admin_settings_mod

        try:
            return max(admin_settings_mod.effective_int("host_rate_per_min"), 1)
        except Exception:
            return max(int(settings.host_rate_per_min), 1)

    def _redis_url(self) -> str:
        if self._explicit_url is not None:
            return self._explicit_url
        url = settings.host_rate_limit_redis_url
        if url:
            return url
        return settings.celery_broker_url

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                import redis
            except ImportError as exc:
                raise RedisUnavailableError(
                    "redis package is not installed — install the "
                    "``[scale]`` extra (`uv pip install -e .[scale]`)"
                ) from exc
            url = self._redis_url()
            if not url:
                raise RedisUnavailableError(
                    "no Redis URL configured "
                    "(set RECKORA_HOST_RATE_LIMIT_REDIS_URL or "
                    "RECKORA_CELERY_BROKER_URL)"
                )
            try:
                # ``decode_responses`` so HGETALL returns strings —
                # the Lua side does ``tonumber`` regardless but a
                # str payload is easier to log.
                client = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_timeout=2.0,
                    socket_connect_timeout=2.0,
                )
                # ``script_load`` is a sync call here (sync ``redis``
                # client); cast through ``Any`` because redis-py
                # types it as a ``Awaitable[Any] | Any`` union for
                # the shared sync/async surface.
                sha = cast(Any, client.script_load(_LUA_SLIDING_WINDOW))
                self._script_sha = str(sha)
            except Exception as exc:
                raise RedisUnavailableError(f"could not connect to Redis at {url}: {exc}") from exc
            self._client = client
            return client

    def _eval_script(self, host: str, rate_per_min: int) -> tuple[bool, int]:
        client = self._ensure_client()
        key = self._key_prefix + host
        now_sec = math.floor(time.time())
        try:
            result = client.evalsha(
                self._script_sha,
                1,
                key,
                now_sec,
                self._window_sec,
                rate_per_min,
            )
        except Exception as exc:
            # NOSCRIPT recovery: re-load the script and try once
            # more. Other errors propagate as RedisUnavailableError.
            #
            # We detect NOSCRIPT three ways: the redis-py
            # ``NoScriptError`` class (preferred), the legacy
            # uppercase prefix ``NOSCRIPT`` (server reply), and the
            # human-readable ``No matching script`` substring
            # (some redis-py versions surface only the de-prefixed
            # text).
            err_text = str(exc)
            is_noscript = (
                "NOSCRIPT" in err_text
                or "No matching script" in err_text
                or _is_noscript_class(exc)
            )
            if is_noscript:
                try:
                    self._script_sha = str(cast(Any, client.script_load(_LUA_SLIDING_WINDOW)))
                    result = client.evalsha(
                        self._script_sha,
                        1,
                        key,
                        now_sec,
                        self._window_sec,
                        rate_per_min,
                    )
                except Exception as inner_exc:
                    raise RedisUnavailableError(
                        f"NOSCRIPT recovery failed: {inner_exc}"
                    ) from inner_exc
            else:
                raise RedisUnavailableError(err_text) from exc
        # ``EVAL`` returns a list of two ints in Lua — redis-py
        # surfaces those as Python ints when ``decode_responses=True``
        # is set (the bytes path is unreachable here).
        allowed = bool(result[0])
        wait_ms = int(result[1])
        return allowed, wait_ms


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """``True`` iff the admin knob *and* a redis URL are both set."""
    return _enabled_knob() == 1 and _resolved_redis_url() != ""


def _enabled_knob() -> int:
    from . import admin_settings as admin_settings_mod

    try:
        return admin_settings_mod.effective_int("host_rate_limit_use_redis")
    except Exception:
        return int(settings.host_rate_limit_use_redis)


def _resolved_redis_url() -> str:
    return settings.host_rate_limit_redis_url or settings.celery_broker_url


def status() -> dict[str, object]:
    """Compact descriptor for ``/api/health.host_rate_limit.redis``."""
    return {
        "configured": is_configured(),
        "redis_url_set": _resolved_redis_url() != "",
    }
