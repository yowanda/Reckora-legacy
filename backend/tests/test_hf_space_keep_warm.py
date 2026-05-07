"""Phase 9h tests — HF Space keep-warm cron ping script.

The keep-warm script is intentionally pure stdlib (``urllib.request``)
so the GitHub Actions runner doesn't need to install Reckora's runtime
dependencies. The tests in this module avoid any real network I/O —
they inject a stub ``opener`` into
:func:`backend.scripts.hf_space_keep_warm.ping_url` so the retry / status
classification / backoff contracts can be exercised deterministically.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from io import StringIO
from pathlib import Path

import pytest

# ``backend/scripts`` is a sibling of ``backend/tests`` and not exposed
# as a package — add the parent directory to ``sys.path`` so the test
# can import the keep-warm module directly without polluting ``app/``.
# This mirrors the pattern used by ``test_catalog_refresh.py``.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from scripts import hf_space_keep_warm as kw  # noqa: E402  # sys.path tweak above


class _StubResponse:
    """Duck-typed stand-in for ``http.client.HTTPResponse``.

    The real ``urlopen`` returns an object with a ``.status`` attribute
    and a ``.close()`` method; that's the entire surface the script
    touches, so the stub only needs those two members.
    """

    def __init__(self, status: int) -> None:
        self.status = status
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_opener(
    plan: list[int | type[BaseException] | BaseException],
) -> Callable[[urllib.request.Request, float], object]:
    """Build a stub ``opener`` that walks ``plan`` once per call.

    Each entry can be:

    * an ``int`` — return a :class:`_StubResponse` with that HTTP status,
    * a ``BaseException`` instance — raise it,
    * an exception *class* — instantiate with empty args and raise it.

    ``IndexError`` on an unexpected extra call surfaces as a real test
    failure rather than silently looping past the plan.
    """

    iterator = iter(plan)

    def opener(_request: urllib.request.Request, _timeout: float) -> object:
        try:
            entry = next(iterator)
        except StopIteration as exc:
            raise AssertionError("opener called more times than the plan allows") from exc
        if isinstance(entry, int):
            return _StubResponse(entry)
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, type) and issubclass(entry, BaseException):
            raise entry()
        raise AssertionError(f"unsupported plan entry: {entry!r}")

    return opener


# ---------------------------------------------------------------------------
# _backoff_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt,initial,cap,expected",
    [
        (0, 1.5, 8.0, 0.0),
        (1, 1.5, 8.0, 1.5),
        (2, 1.5, 8.0, 2.25),
        (3, 1.5, 8.0, 3.375),
        (10, 1.5, 8.0, 8.0),
        (-1, 1.5, 8.0, 0.0),
    ],
)
def test_backoff_seconds_exponential_with_cap(
    attempt: int,
    initial: float,
    cap: float,
    expected: float,
) -> None:
    actual = kw._backoff_seconds(attempt, initial=initial, cap=cap)
    assert actual == pytest.approx(expected)


# ---------------------------------------------------------------------------
# ping_url — success path
# ---------------------------------------------------------------------------


def test_ping_url_returns_ok_on_first_2xx() -> None:
    sleeps: list[float] = []
    result = kw.ping_url(
        "https://example.test/api/health",
        opener=_make_opener([200]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.status == 200
    assert result.attempts == 1
    assert result.error is None
    # No retries fired so no sleep call was made.
    assert sleeps == []


@pytest.mark.parametrize("status", [200, 204, 301, 302, 304, 399])
def test_ping_url_treats_2xx_and_3xx_as_success(status: int) -> None:
    result = kw.ping_url(
        "https://example.test/api/health",
        opener=_make_opener([status]),
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.status == status
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# ping_url — retry path
# ---------------------------------------------------------------------------


def test_ping_url_retries_on_503_and_succeeds_on_subsequent_attempt() -> None:
    sleeps: list[float] = []
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([503, 200]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.status == 200
    assert result.attempts == 2
    assert result.error is None
    # One backoff sleep between the failed attempt and the retry.
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.5)


def test_ping_url_retries_on_httperror_with_status() -> None:
    sleeps: list[float] = []
    fake_http_error = urllib.error.HTTPError(
        url="https://example.test/api/health",
        code=503,
        msg="Service Unavailable",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([fake_http_error, 200]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.attempts == 2
    assert len(sleeps) == 1


def test_ping_url_retries_on_urlerror() -> None:
    sleeps: list[float] = []
    fake_url_error = urllib.error.URLError("connection reset")
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([fake_url_error, 200]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.attempts == 2
    assert len(sleeps) == 1


def test_ping_url_retries_on_oserror() -> None:
    sleeps: list[float] = []
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([OSError("broken pipe"), 200]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is True
    assert result.attempts == 2


# ---------------------------------------------------------------------------
# ping_url — permanent failure path
# ---------------------------------------------------------------------------


def test_ping_url_returns_failure_after_exhausting_retries() -> None:
    sleeps: list[float] = []
    fake_url_error = urllib.error.URLError("name resolution failed")
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([fake_url_error, fake_url_error, fake_url_error]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is False
    assert result.status is None
    assert result.attempts == 3
    assert result.error is not None
    assert "name resolution failed" in result.error
    # Two backoff sleeps between three attempts (no sleep after the
    # final attempt, because giving up immediately is faster).
    assert len(sleeps) == 2


def test_ping_url_records_last_error_on_mixed_failure_modes() -> None:
    fake_http_error = urllib.error.HTTPError(
        url="https://example.test/api/health",
        code=502,
        msg="Bad Gateway",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener(
            [
                urllib.error.URLError("transient"),
                OSError("flaky socket"),
                fake_http_error,
            ]
        ),
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    assert result.ok is False
    # The final attempt was the HTTPError so the captured status reflects it.
    assert result.status == 502
    assert result.attempts == 3
    assert result.error is not None
    assert "502" in result.error


def test_ping_url_treats_4xx_as_transient_and_retries() -> None:
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=3,
        opener=_make_opener([404, 404, 404]),
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    assert result.ok is False
    assert result.status == 404
    assert result.attempts == 3


# ---------------------------------------------------------------------------
# ping_url — argument validation
# ---------------------------------------------------------------------------


def test_ping_url_rejects_zero_or_negative_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        kw.ping_url(
            "https://example.test/api/health",
            max_attempts=0,
            opener=_make_opener([200]),
            sleep=lambda _: None,
            clock=lambda: 0.0,
        )


def test_ping_url_max_attempts_one_does_not_retry() -> None:
    sleeps: list[float] = []
    result = kw.ping_url(
        "https://example.test/api/health",
        max_attempts=1,
        opener=_make_opener([503]),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result.ok is False
    assert result.status == 503
    assert result.attempts == 1
    # Single attempt — no backoff window even on failure.
    assert sleeps == []


# ---------------------------------------------------------------------------
# ping_url — elapsed time accounting
# ---------------------------------------------------------------------------


def test_ping_url_elapsed_sec_is_clock_delta() -> None:
    times = iter([100.0, 102.5])

    def fake_clock() -> float:
        return next(times)

    result = kw.ping_url(
        "https://example.test/api/health",
        opener=_make_opener([200]),
        sleep=lambda _: None,
        clock=fake_clock,
    )
    assert result.elapsed_sec == pytest.approx(2.5)


def test_ping_url_elapsed_sec_clamped_to_non_negative() -> None:
    # An adversarial monotonic-ish clock that goes backwards still
    # produces a non-negative elapsed_sec — the script never reports a
    # negative duration in the GH Actions log.
    times = iter([100.0, 50.0])

    def regressive_clock() -> float:
        return next(times)

    result = kw.ping_url(
        "https://example.test/api/health",
        opener=_make_opener([200]),
        sleep=lambda _: None,
        clock=regressive_clock,
    )
    assert result.elapsed_sec == 0.0


# ---------------------------------------------------------------------------
# _emit_log_line
# ---------------------------------------------------------------------------


def test_emit_log_line_is_single_line_json_with_stable_keys() -> None:
    result = kw.PingResult(
        ok=True,
        status=200,
        attempts=1,
        elapsed_sec=0.4218,
        error=None,
    )
    line = kw._emit_log_line(result, url="https://example.test/api/health")
    # Exactly one line, valid JSON, and the keys are sorted so log
    # diffs across runs stay stable.
    assert "\n" not in line
    payload = json.loads(line)
    assert payload == {
        "attempts": 1,
        "elapsed_sec": 0.422,
        "error": None,
        "ok": True,
        "status": 200,
        "url": "https://example.test/api/health",
    }


def test_emit_log_line_rounds_elapsed_to_three_decimals() -> None:
    result = kw.PingResult(ok=False, status=503, attempts=3, elapsed_sec=4.123456789, error="x")
    payload = json.loads(kw._emit_log_line(result, url="u"))
    assert payload["elapsed_sec"] == 4.123


# ---------------------------------------------------------------------------
# _build_argparser / main()
# ---------------------------------------------------------------------------


def test_build_argparser_defaults_match_module_constants() -> None:
    args = kw._build_argparser().parse_args([])
    assert args.url == kw.DEFAULT_URL
    assert args.max_attempts == kw.DEFAULT_MAX_ATTEMPTS
    assert args.timeout == kw.DEFAULT_TIMEOUT_SEC
    assert args.initial_backoff == kw.DEFAULT_INITIAL_BACKOFF_SEC
    assert args.max_backoff == kw.DEFAULT_MAX_BACKOFF_SEC
    assert args.user_agent == kw.DEFAULT_USER_AGENT


def test_build_argparser_accepts_explicit_overrides() -> None:
    args = kw._build_argparser().parse_args(
        [
            "--url",
            "http://localhost:8000/api/health",
            "--max-attempts",
            "7",
            "--timeout",
            "30",
            "--initial-backoff",
            "0.5",
            "--max-backoff",
            "20",
            "--user-agent",
            "smoke-runner/2.0",
        ]
    )
    assert args.url == "http://localhost:8000/api/health"
    assert args.max_attempts == 7
    assert args.timeout == 30.0
    assert args.initial_backoff == 0.5
    assert args.max_backoff == 20.0
    assert args.user_agent == "smoke-runner/2.0"


def test_main_returns_zero_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_ping(
        _url: str,
        *,
        max_attempts: int = 3,  # noqa: ARG001
        timeout: float = 12.0,  # noqa: ARG001
        initial_backoff: float = 1.5,  # noqa: ARG001
        max_backoff: float = 8.0,  # noqa: ARG001
        user_agent: str = "x",  # noqa: ARG001
    ) -> kw.PingResult:
        return kw.PingResult(ok=True, status=200, attempts=1, elapsed_sec=0.05, error=None)

    monkeypatch.setattr(kw, "ping_url", fake_ping)
    rc = kw.main(["--url", "https://example.test/api/health"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True
    assert payload["status"] == 200


def test_main_returns_one_on_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_ping(
        _url: str,
        *,
        max_attempts: int = 3,  # noqa: ARG001
        timeout: float = 12.0,  # noqa: ARG001
        initial_backoff: float = 1.5,  # noqa: ARG001
        max_backoff: float = 8.0,  # noqa: ARG001
        user_agent: str = "x",  # noqa: ARG001
    ) -> kw.PingResult:
        return kw.PingResult(
            ok=False, status=503, attempts=3, elapsed_sec=4.0, error="non-2xx status 503"
        )

    monkeypatch.setattr(kw, "ping_url", fake_ping)
    rc = kw.main(["--url", "https://example.test/api/health"])
    assert rc == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is False
    assert payload["status"] == 503
    assert payload["attempts"] == 3
    assert payload["error"] == "non-2xx status 503"


def test_main_emits_exactly_one_log_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_ping(
        _url: str,
        **_kwargs: object,
    ) -> kw.PingResult:
        return kw.PingResult(ok=True, status=200, attempts=1, elapsed_sec=0.1, error=None)

    monkeypatch.setattr(kw, "ping_url", fake_ping)
    kw.main([])
    captured = capsys.readouterr()
    # Exactly one non-empty line on stdout.
    lines = [ln for ln in captured.out.split("\n") if ln.strip()]
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# _open_request — exercise the production opener path
# ---------------------------------------------------------------------------


def test_open_request_passes_user_agent_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_opener(req: urllib.request.Request, timeout: float) -> object:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["user_agent"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _StubResponse(200)

    status = kw._open_request(
        "https://example.test/api/health",
        timeout=7.5,
        user_agent="reckora-keep-warm-test/9.9",
        opener=fake_opener,
    )
    assert status == 200
    assert seen == {
        "url": "https://example.test/api/health",
        "method": "GET",
        "user_agent": "reckora-keep-warm-test/9.9",
        "timeout": 7.5,
    }


def test_open_request_default_opener_uses_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(req: object, timeout: float | None = None) -> object:  # noqa: ARG001
        captured["called"] = True
        return _StubResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status = kw._open_request(
        "https://example.test/api/health",
        timeout=4.0,
        user_agent="x",
        opener=None,
    )
    assert status == 204
    assert captured == {"called": True}


def test_open_request_closes_response_with_close_method() -> None:
    stub = _StubResponse(200)

    def fake_opener(_req: urllib.request.Request, _timeout: float) -> object:
        return stub

    kw._open_request(
        "https://example.test/api/health", timeout=1.0, user_agent="x", opener=fake_opener
    )
    assert stub.closed is True


def test_open_request_handles_response_without_close_attribute() -> None:
    class _NoClose:
        status = 200

    def fake_opener(_req: urllib.request.Request, _timeout: float) -> object:
        return _NoClose()

    # Should not raise when the response stub has no ``close`` callable.
    status = kw._open_request(
        "https://example.test/api/health",
        timeout=1.0,
        user_agent="x",
        opener=fake_opener,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Module-level constants exposed for downstream tooling
# ---------------------------------------------------------------------------


def test_default_url_points_at_live_hf_space() -> None:
    assert kw.DEFAULT_URL.startswith("https://yesmyyt-cacadoxx.hf.space")
    assert kw.DEFAULT_URL.endswith("/api/health")


def test_default_user_agent_identifies_the_cron_job() -> None:
    assert "reckora-keep-warm" in kw.DEFAULT_USER_AGENT


def test_module_exposes_ping_result_and_ping_url_for_smoke_imports() -> None:
    # Smoke check that the public surface required by the workflow is
    # importable at module level.
    assert callable(kw.ping_url)
    assert callable(kw.main)
    assert hasattr(kw, "PingResult")
    assert hasattr(kw, "DEFAULT_URL")


# Silence the ``StringIO`` unused import that some IDEs flag — keep it
# for ergonomic ``capsys`` assertions in case a future test wants to
# bypass capsys and capture stdout manually.
_ = StringIO
