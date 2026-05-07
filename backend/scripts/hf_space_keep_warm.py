"""Phase 9h — HF Space keep-warm cron.

Closes residual risk **M7** (cold-start latency on the HF Space free tier:
the container hibernates after ~5 min of inactivity, so the next visitor
pays the 30-45 s Docker boot before the FastAPI worker accepts a TCP
connection) by pinging the live demo's ``/api/health`` endpoint on a
30-minute schedule from a GitHub Actions cron - keeping the container
warm without paying for HF Spaces' "Always On" tier.

The script is intentionally pure stdlib (``urllib.request``, no
``httpx``, no ``requests``) so the GitHub Actions runner doesn't need to
install Reckora's runtime dependencies — a fresh ``ubuntu-latest`` +
``actions/setup-python@v5`` image can run it directly.

The retry strategy is straightforward: hit the URL, on failure sleep for
``backoff^attempt`` seconds (capped at ``max_backoff_sec``), and try
again up to ``max_attempts`` times. ``max_attempts=3`` plus
``initial_backoff=1.5`` exponential gives roughly ``1.5 + 2.25 = 3.75 s``
of total backoff which fits comfortably inside the GitHub Actions
30-second job timeout we set in the workflow file.

This script does NOT count as Reckora runtime — it is invoked exclusively
by the cron workflow. The unit tests stub ``urllib.request.urlopen`` so
the test suite never makes a real network call.

Usage::

    # Default URL (the live HF Space demo):
    python -m scripts.hf_space_keep_warm

    # Override URL (smoke-test against staging or local FastAPI):
    python -m scripts.hf_space_keep_warm --url http://localhost:8000/api/health

    # Tune retry budget:
    python -m scripts.hf_space_keep_warm --max-attempts 5 --timeout 20
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Default endpoint hit by the cron workflow. The live HF Space at
# ``yesmyyt-cacadoxx.hf.space`` exposes ``/api/health`` as a cheap GET
# that returns the runtime knob snapshot — which means a successful
# 200-OK ping also doubles as a smoke-test that the FastAPI app booted.
DEFAULT_URL = "https://yesmyyt-cacadoxx.hf.space/api/health"

# Conservative retry budget — 3 attempts with 1.5x exponential backoff
# is enough to ride out a ~5 s cold-start without making the GH Actions
# runner job feel sluggish. The 12 s ``timeout`` matches the upper bound
# of the cold-start observed in the Phase 8 deploy debrief.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_SEC = 12.0
DEFAULT_INITIAL_BACKOFF_SEC = 1.5
DEFAULT_MAX_BACKOFF_SEC = 8.0
DEFAULT_USER_AGENT = "reckora-keep-warm/1.0 (+https://github.com/yowanda/Reckora)"


@dataclass(frozen=True)
class PingResult:
    """Outcome of a single ``ping_url`` call.

    ``ok=True`` means the endpoint returned an HTTP status in the
    ``200..399`` band on at least one attempt. ``status`` and
    ``elapsed_sec`` reflect the *successful* attempt; on permanent
    failure they describe the LAST attempt instead.
    """

    ok: bool
    status: int | None
    attempts: int
    elapsed_sec: float
    error: str | None


def _open_request(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    opener: Callable[[urllib.request.Request, float], object] | None = None,
) -> int:
    """Issue a single GET against ``url`` and return the HTTP status.

    The default ``opener`` is ``urllib.request.urlopen`` — tests stub
    this argument so they can simulate timeouts and HTTP error responses
    without a real network call.
    """

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": user_agent})
    # ``urlopen`` accepts the timeout as a kwarg; wrap it in a closure so
    # the call signature matches the injected ``opener`` used by the
    # unit tests.
    if opener is not None:
        open_fn: Callable[[urllib.request.Request, float], object] = opener
    else:

        def open_fn(r: urllib.request.Request, t: float) -> object:
            return urllib.request.urlopen(r, timeout=t)

    response = open_fn(req, timeout)
    # ``http.client.HTTPResponse`` exposes ``status`` (int); the tests
    # use a duck-typed stub with the same attribute so the call here
    # is shape-stable across the real ``urlopen`` and the test fakes.
    status = int(getattr(response, "status", 0))
    close = getattr(response, "close", None)
    if callable(close):
        close()
    return status


def _backoff_seconds(
    attempt: int,
    *,
    initial: float,
    cap: float,
) -> float:
    """Exponential backoff capped at ``cap`` seconds.

    ``attempt`` is 1-indexed: the first retry waits ``initial`` seconds,
    the second waits ``initial * 1.5``, and so on, capped at ``cap``.
    """

    if attempt < 1:
        return 0.0
    delay = initial * (1.5 ** (attempt - 1))
    return min(delay, cap)


def ping_url(
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SEC,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
    opener: Callable[[urllib.request.Request, float], object] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> PingResult:
    """Hit ``url`` with retries and return a structured :class:`PingResult`.

    A response with status in ``200..399`` counts as success. A 4xx /
    5xx is treated as a *transient* failure that is worth retrying — HF
    Spaces returns 503 during the boot window and a 4xx after the
    container hibernates but before the FastAPI worker registers, so
    retrying on any non-2xx is safer than treating 4xx as a permanent
    fault. The unit tests pin this behaviour.

    ``sleep`` and ``clock`` are injected so tests can advance "time"
    without actually sleeping. Production calls leave them ``None``
    and they default to ``time.sleep`` and ``time.monotonic``.
    """

    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    sleep_fn = sleep if sleep is not None else time.sleep
    clock_fn = clock if clock is not None else time.monotonic

    started = clock_fn()
    last_status: int | None = None
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            status = _open_request(
                url,
                timeout=timeout,
                user_agent=user_agent,
                opener=opener,
            )
        except urllib.error.HTTPError as exc:
            # ``HTTPError`` is itself a non-2xx response — capture the
            # status and treat it as a transient failure (HF Spaces 503
            # during boot, 4xx during the brief unregistered window).
            last_status = int(exc.code)
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            last_status = None
            last_error = f"URLError: {exc.reason!r}"
        except OSError as exc:
            # Covers socket-level errors that don't surface as
            # ``URLError`` (e.g. broken pipe mid-handshake on a flaky
            # runner) — same retry treatment.
            last_status = None
            last_error = f"OSError: {exc!r}"
        else:
            if 200 <= status < 400:
                return PingResult(
                    ok=True,
                    status=status,
                    attempts=attempt,
                    elapsed_sec=max(0.0, clock_fn() - started),
                    error=None,
                )
            last_status = status
            last_error = f"non-2xx status {status}"

        if attempt < max_attempts:
            sleep_fn(_backoff_seconds(attempt, initial=initial_backoff, cap=max_backoff))

    return PingResult(
        ok=False,
        status=last_status,
        attempts=max_attempts,
        elapsed_sec=max(0.0, clock_fn() - started),
        error=last_error,
    )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hf_space_keep_warm",
        description=(
            "Phase 9h — ping the live HF Space ``/api/health`` endpoint to "
            "keep the free-tier container warm. Invoked by the GitHub "
            "Actions cron workflow at ``.github/workflows/hf-keep-warm.yml`` "
            "every 30 minutes; also exposed via ``workflow_dispatch`` so a "
            "maintainer can fire an ad-hoc smoke run from the GitHub UI."
        ),
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Health endpoint to ping (default: {DEFAULT_URL}).",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Retry budget on transient failure (default: {DEFAULT_MAX_ATTEMPTS}).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Per-attempt socket timeout in seconds (default: {DEFAULT_TIMEOUT_SEC}).",
    )
    p.add_argument(
        "--initial-backoff",
        type=float,
        default=DEFAULT_INITIAL_BACKOFF_SEC,
        help=(
            "Backoff between retries in seconds, exponentially scaled by 1.5 per "
            f"attempt (default: {DEFAULT_INITIAL_BACKOFF_SEC})."
        ),
    )
    p.add_argument(
        "--max-backoff",
        type=float,
        default=DEFAULT_MAX_BACKOFF_SEC,
        help=f"Cap on the per-retry backoff in seconds (default: {DEFAULT_MAX_BACKOFF_SEC}).",
    )
    p.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Override the User-Agent header (default: identifies the cron job).",
    )
    return p


def _emit_log_line(result: PingResult, *, url: str) -> str:
    """Render a single-line JSON status report for the GH Actions log."""

    payload = {
        "ok": result.ok,
        "url": url,
        "status": result.status,
        "attempts": result.attempts,
        "elapsed_sec": round(result.elapsed_sec, 3),
        "error": result.error,
    }
    return json.dumps(payload, sort_keys=True)


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint. Returns 0 on success, 1 on permanent failure."""

    args = _build_argparser().parse_args(list(argv) if argv is not None else None)
    result = ping_url(
        args.url,
        max_attempts=args.max_attempts,
        timeout=args.timeout,
        initial_backoff=args.initial_backoff,
        max_backoff=args.max_backoff,
        user_agent=args.user_agent,
    )
    line = _emit_log_line(result, url=args.url)
    # Always emit a single structured line to stdout so the GH Actions
    # job log captures the outcome regardless of exit code; the workflow
    # surfaces this as the job step output.
    print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
