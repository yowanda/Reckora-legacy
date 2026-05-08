"""Phase 10f: ``reckora`` CLI tests.

Covers argparse parsing, exit codes, JSON-out shape, tag filtering,
identity-type validation + selection, the SIGINT cancel token, the
``--list-tags`` / ``--list-identity-types`` admin commands, and the
report writer dispatch.

The async ``scan()`` API is exercised end-to-end with an
``httpx.MockTransport``-style monkeypatch so the test file never makes
a real network request — same contract every other Reckora test holds
to keep CI deterministic.
"""

from __future__ import annotations

import io
import json
import signal
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from app.checker import CheckResult, Status
from app.cli import (
    CliInputError,
    build_scan_summary,
    ensure_admin_db,
    load_default_catalog,
    render_report,
    select_sites,
)
from app.cli.__main__ import (
    EXIT_ARGV,
    EXIT_INTERRUPTED,
    EXIT_NO_HITS,
    EXIT_NO_SITES,
    EXIT_OK,
    _CancelToken,
    build_parser,
    main,
    parse_tags,
)
from app.exports import ALLOWED_FORMATS
from app.identity import ALL_IDENTITY_TYPES
from app.sites import Site, SiteCatalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _site(
    name: str = "Foo",
    *,
    url: str = "https://foo.test/{username}",
    tags: tuple[str, ...] = ("dev",),
    identity_types: tuple[str, ...] = ("username",),
    tentative: bool = False,
) -> Site:
    return Site(
        name=name,
        url=url,
        url_main=url.split("{username}")[0] or url,
        check_type="status_code",
        regex_check=None,
        absence_strs=(),
        presense_strs=(),
        headers=(),
        alexa_rank=None,
        tags=tags,
        error_strs=(),
        head_only=False,
        ignore_403=False,
        engine=None,
        prewarm=False,
        disabled=False,
        source="base",
        identity_types=identity_types,  # type: ignore[arg-type]
        url_probe=None,
        tentative=tentative,
        tentative_reason=None,
    )


def _claimed(site: str = "Foo", username: str = "torvalds", confidence: int = 95) -> CheckResult:
    return CheckResult(
        site=site,
        status=Status.CLAIMED,
        url=f"https://{site.lower()}.test/{username}",
        url_main=f"https://{site.lower()}.test/",
        tags=["dev"],
        http_status=200,
        elapsed_ms=42,
        confidence=confidence,
        signals=[],
        attempts=1,
        verified=True,
        extracted={"display_name": username},
        ml_confidence=88,
    )


def _free(site: str = "Bar", username: str = "torvalds") -> CheckResult:
    return CheckResult(
        site=site,
        status=Status.FREE,
        url=f"https://{site.lower()}.test/{username}",
        url_main=f"https://{site.lower()}.test/",
        tags=["social"],
        http_status=404,
        elapsed_ms=42,
        confidence=0,
        signals=[],
        attempts=1,
        verified=None,
    )


@pytest.fixture(autouse=True)
def _isolated_admin_db(tmp_path: Path) -> Iterator[Path]:
    """Each test gets its own scratch admin DB so the CLI bootstrap
    can run without colliding with the shared conftest test DB.
    """
    target = tmp_path / "cli-admin.db"
    yield ensure_admin_db(target)


# ---------------------------------------------------------------------------
# argparse parser
# ---------------------------------------------------------------------------


def test_parser_defaults_for_minimal_argv() -> None:
    args = build_parser().parse_args(["torvalds"])
    assert args.identifier == "torvalds"
    assert args.identity_type == "username"
    assert args.tags == ""
    assert args.limit is None
    assert args.include_tentative is False
    assert args.verify is True
    assert args.extract is True
    assert args.admin_db is None
    assert args.print_json is False
    assert args.quiet is False


def test_parser_accepts_every_export_format_flag(tmp_path: Path) -> None:
    out = tmp_path / "out"
    args = build_parser().parse_args(
        [
            "torvalds",
            "--csv",
            str(out / "a.csv"),
            "--json",
            str(out / "a.json"),
            "--md",
            str(out / "a.md"),
            "--html",
            str(out / "a.html"),
            "--pdf",
            str(out / "a.pdf"),
            "--txt",
            str(out / "a.txt"),
            "--xmind",
            str(out / "a.xmind"),
            "--d3",
            str(out / "a.html2"),
        ]
    )
    assert args.csv == out / "a.csv"
    assert args.json == out / "a.json"
    assert args.md == out / "a.md"
    assert args.html == out / "a.html"
    assert args.pdf == out / "a.pdf"
    assert args.txt == out / "a.txt"
    assert args.xmind == out / "a.xmind"
    assert args.d3 == out / "a.html2"


def test_parser_rejects_unknown_identity_type() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["foo", "--identity-type", "not-a-type"])


def test_parser_rejects_non_positive_limit() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["foo", "--limit", "0"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["foo", "--limit", "-3"])


def test_parser_no_verify_no_extract_set_false() -> None:
    args = build_parser().parse_args(["foo", "--no-verify", "--no-extract"])
    assert args.verify is False
    assert args.extract is False


def test_parse_tags_strips_and_drops_empty() -> None:
    assert parse_tags("") == ()
    assert parse_tags("photo") == ("photo",)
    assert parse_tags("photo, dating ,, social") == ("photo", "dating", "social")


# ---------------------------------------------------------------------------
# Catalog projection helpers
# ---------------------------------------------------------------------------


def test_select_sites_filters_by_tag_and_identity_type() -> None:
    cat = SiteCatalog(
        (
            _site("Photo", tags=("photo",)),
            _site("Email", url="https://e.test/{username}", identity_types=("email",)),
            _site("Dev", tags=("dev",)),
        )
    )
    by_tag = select_sites(cat, tags=["photo"])
    assert [s.name for s in by_tag] == ["Photo"]

    by_identity = select_sites(cat, identity_type="email")  # type: ignore[arg-type]
    assert [s.name for s in by_identity] == ["Email"]


def test_select_sites_skips_tentative_unless_opted_in() -> None:
    cat = SiteCatalog(
        (
            _site("Solid"),
            _site("Shaky", tentative=True, tags=("dev", "tentative")),
        )
    )
    assert [s.name for s in select_sites(cat)] == ["Solid"]
    opted_in = select_sites(cat, include_tentative=True)
    assert {s.name for s in opted_in} == {"Solid", "Shaky"}


# ---------------------------------------------------------------------------
# build_scan_summary + render_report
# ---------------------------------------------------------------------------


def test_build_scan_summary_counts_each_status() -> None:
    results = [_claimed(), _claimed("Bar"), _free("Baz")]
    summary = build_scan_summary(
        results,
        username="torvalds",
        started_at=100,
        finished_at=142,
        site_count=3,
    )
    assert summary.username == "torvalds"
    assert summary.mode == "list"
    assert summary.site_count == 3
    assert summary.claimed == 2
    assert summary.free == 1
    assert summary.unknown == 0
    assert summary.error_count == 0
    assert summary.finished_at - summary.started_at == 42


def test_render_report_round_trips_json_and_csv() -> None:
    results = [_claimed(), _free()]
    summary = build_scan_summary(
        results,
        username="torvalds",
        started_at=100,
        finished_at=200,
        site_count=2,
    )
    json_body, json_media, json_filename = render_report("json", summary, results)
    payload = json.loads(json_body)
    assert payload["scan"]["username"] == "torvalds"
    assert {row["site"] for row in payload["results"]} == {"Foo", "Bar"}
    assert json_media.startswith("application/json")
    assert json_filename.endswith(".json")

    csv_body, csv_media, _csv_filename = render_report("csv", summary, results)
    csv_text = csv_body.decode("utf-8")
    assert csv_text.splitlines()[0] == "site,status,confidence,ml_confidence,url,parent_username"
    assert "Foo,claimed" in csv_text
    assert csv_media.startswith("text/csv")


def test_render_report_supports_every_documented_format() -> None:
    results = [_claimed()]
    summary = build_scan_summary(
        results,
        username="torvalds",
        started_at=1,
        finished_at=2,
        site_count=1,
    )
    for fmt in sorted(ALLOWED_FORMATS):
        body, media, filename = render_report(fmt, summary, results)  # type: ignore[arg-type]
        assert isinstance(body, bytes) and body, fmt
        assert media, fmt
        assert filename, fmt


# ---------------------------------------------------------------------------
# Async scan() — driven via a stubbed ``stream_checks``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_validates_identifier_against_identity_type() -> None:
    cat = SiteCatalog((_site(),))
    from app.cli import scan

    with pytest.raises(CliInputError):
        async for _ in scan("not-an-email", identity_type="email", catalog=cat):
            pass


@pytest.mark.asyncio
async def test_scan_yields_results_from_stubbed_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = SiteCatalog((_site("Foo"), _site("Bar", url="https://bar.test/{username}")))

    async def _fake_stream(
        sites: list[Site],
        username: str,
        **_kwargs: object,
    ) -> AsyncIterator[CheckResult]:
        for s in sites:
            yield _claimed(site=s.name, username=username)

    from app import cli as cli_pkg

    monkeypatch.setattr(cli_pkg, "stream_checks", _fake_stream)

    seen: list[CheckResult] = []
    async for r in cli_pkg.scan("torvalds", catalog=cat):
        seen.append(r)
    assert [r.site for r in seen] == ["Foo", "Bar"]
    assert all(r.status is Status.CLAIMED for r in seen)


@pytest.mark.asyncio
async def test_scan_returns_immediately_when_no_sites_match() -> None:
    cat = SiteCatalog((_site("Foo", tags=("dev",)),))
    from app.cli import scan

    seen: list[CheckResult] = []
    async for r in scan("torvalds", tags=["does-not-exist"], catalog=cat):
        seen.append(r)
    assert seen == []


# ---------------------------------------------------------------------------
# main() integration — uses the same stub to keep tests offline
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[CheckResult]]:
    """Replace ``app.cli.stream_checks`` with a deterministic stub.

    Yields a mutable list; tests mutate it before invoking ``main``.
    """
    queue: list[CheckResult] = []

    async def _fake_stream(
        sites: list[Site],
        username: str,
        **_kwargs: object,
    ) -> AsyncIterator[CheckResult]:
        for r in queue:
            yield r

    cat = SiteCatalog((_site("Foo"), _site("Bar", url="https://bar.test/{username}")))

    from app import cli as cli_pkg

    monkeypatch.setattr(cli_pkg, "stream_checks", _fake_stream)
    monkeypatch.setattr(cli_pkg, "load_default_catalog", lambda: cat)

    from app.cli import __main__ as main_mod

    monkeypatch.setattr(main_mod, "load_default_catalog", lambda: cat)
    yield queue


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_main_exits_with_zero_when_at_least_one_claimed(
    stubbed_stream: list[CheckResult],
) -> None:
    stubbed_stream.extend([_claimed("Foo"), _free("Bar")])
    code, _stdout, stderr = _run_main(["torvalds", "--quiet"])
    assert code == EXIT_OK
    assert "torvalds" not in stderr  # quiet suppresses progress


def test_main_exit_3_when_no_claimed_hits(stubbed_stream: list[CheckResult]) -> None:
    stubbed_stream.extend([_free("Foo"), _free("Bar")])
    code, _stdout, _stderr = _run_main(["torvalds", "--quiet"])
    assert code == EXIT_NO_HITS


def test_main_exit_4_when_no_sites_match_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = SiteCatalog((_site("Foo", tags=("dev",)),))

    from app import cli as cli_pkg
    from app.cli import __main__ as main_mod

    monkeypatch.setattr(cli_pkg, "load_default_catalog", lambda: cat)
    monkeypatch.setattr(main_mod, "load_default_catalog", lambda: cat)
    code, _stdout, stderr = _run_main(["torvalds", "--tags", "does-not-exist", "--quiet"])
    assert code == EXIT_NO_SITES
    assert "no sites matched" in stderr


def test_main_writes_every_requested_report(
    stubbed_stream: list[CheckResult],
    tmp_path: Path,
) -> None:
    stubbed_stream.append(_claimed("Foo"))
    csv_path = tmp_path / "a.csv"
    json_path = tmp_path / "a.json"
    md_path = tmp_path / "a.md"
    code, _stdout, _stderr = _run_main(
        [
            "torvalds",
            "--quiet",
            "--csv",
            str(csv_path),
            "--json",
            str(json_path),
            "--md",
            str(md_path),
        ]
    )
    assert code == EXIT_OK
    assert csv_path.exists() and csv_path.read_text(encoding="utf-8").startswith("site,status,")
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["scan"]["username"] == "torvalds"
    assert payload["results"][0]["site"] == "Foo"
    assert md_path.exists()
    assert "torvalds" in md_path.read_text(encoding="utf-8")


def test_main_print_json_emits_parseable_payload_to_stdout(
    stubbed_stream: list[CheckResult],
) -> None:
    stubbed_stream.append(_claimed("Foo"))
    code, stdout, _stderr = _run_main(["torvalds", "--quiet", "--print-json"])
    assert code == EXIT_OK
    payload = json.loads(stdout)
    assert payload["scan"]["claimed"] == 1
    assert payload["results"][0]["status"] == "claimed"


def test_main_rejects_invalid_identifier_for_identity_type(
    stubbed_stream: list[CheckResult],
) -> None:
    code, _stdout, stderr = _run_main(["not-an-email", "--identity-type", "email", "--quiet"])
    assert code == EXIT_ARGV
    assert "valid email" in stderr


def test_main_list_identity_types_prints_every_known_type() -> None:
    code, stdout, _stderr = _run_main(["--list-identity-types"])
    assert code == EXIT_OK
    for t in ALL_IDENTITY_TYPES:
        assert t in stdout


def test_main_list_tags_prints_catalog_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = SiteCatalog(
        (
            _site("Foo", tags=("photo",)),
            _site("Bar", url="https://bar.test/{username}", tags=("photo", "dev")),
        )
    )
    from app import cli as cli_pkg
    from app.cli import __main__ as main_mod

    monkeypatch.setattr(cli_pkg, "load_default_catalog", lambda: cat)
    monkeypatch.setattr(main_mod, "load_default_catalog", lambda: cat)
    code, stdout, _stderr = _run_main(["--list-tags"])
    assert code == EXIT_OK
    # photo (2) appears before dev (1) by site count.
    photo_index = stdout.index("photo")
    dev_index = stdout.index("dev")
    assert photo_index < dev_index


def test_main_argparse_failure_returns_exit_2(stubbed_stream: list[CheckResult]) -> None:
    code, _stdout, _stderr = _run_main(["torvalds", "--limit", "not-a-number"])
    assert code == EXIT_ARGV


def test_main_missing_identifier_when_no_meta_command_returns_exit_2() -> None:
    code, _stdout, _stderr = _run_main([])
    assert code == EXIT_ARGV


# ---------------------------------------------------------------------------
# SIGINT graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigint_cancel_token_short_circuits_loop() -> None:
    cancel = _CancelToken()
    saw: list[str] = []

    async def _stream() -> AsyncIterator[CheckResult]:
        for s in ("Foo", "Bar", "Baz"):
            saw.append(s)
            yield _claimed(s)

    # Mimic the CLI loop: after the first item set the flag and break.
    async for _r in _stream():
        if cancel.triggered:
            break
        cancel.triggered = True
    assert saw == ["Foo", "Bar"]
    # Sanity: the token only has one slot.
    assert _CancelToken.__slots__ == ("triggered",)


def test_main_handles_sigint_during_scan(
    stubbed_stream: list[CheckResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch ``stream_checks`` so the second yield raises a synthesised
    SIGINT inside the asyncio loop. The CLI must surface exit code 130.
    """

    async def _interrupting_stream(
        _sites: list[Site],
        _username: str,
        **_kwargs: object,
    ) -> AsyncIterator[CheckResult]:
        yield _claimed("Foo")
        # Set the cancel flag the same way SIGINT would. Reach into the
        # CLI's main module to flip the only live token instance.
        from app.cli import __main__ as main_mod

        # The currently-running ``main`` left a token registered as the
        # signal handler; emulate it firing.
        main_mod._signal_token.triggered = True  # type: ignore[attr-defined]
        yield _claimed("Bar")

    from app import cli as cli_pkg
    from app.cli import __main__ as main_mod

    # Track the active token so the stub above can flip it.
    original_run_scan = main_mod._run_scan

    async def _instrumented_run_scan(args, *, cancel, stdout, stderr):  # type: ignore[no-untyped-def]
        main_mod._signal_token = cancel  # type: ignore[attr-defined]
        return await original_run_scan(args, cancel=cancel, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(main_mod, "_run_scan", _instrumented_run_scan)
    monkeypatch.setattr(cli_pkg, "stream_checks", _interrupting_stream)

    code, _stdout, stderr = _run_main(["torvalds", "--quiet"])
    assert code == EXIT_INTERRUPTED
    assert "interrupted" in stderr


def test_main_sigint_handler_is_restored_after_run(
    stubbed_stream: list[CheckResult],
) -> None:
    sentinel_called: list[int] = []

    def _sentinel(_signum: int, _frame: object) -> None:
        sentinel_called.append(1)

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _sentinel)
    try:
        stubbed_stream.append(_claimed("Foo"))
        code, _stdout, _stderr = _run_main(["torvalds", "--quiet"])
        assert code == EXIT_OK
        assert signal.getsignal(signal.SIGINT) is _sentinel
    finally:
        signal.signal(signal.SIGINT, previous)


# ---------------------------------------------------------------------------
# ensure_admin_db
# ---------------------------------------------------------------------------


def test_ensure_admin_db_creates_schema_at_target_path(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "reckora.db"
    resolved = ensure_admin_db(target)
    assert resolved == target.resolve()
    assert target.exists()


def test_ensure_admin_db_with_no_path_creates_a_tempfile() -> None:
    resolved = ensure_admin_db(None)
    assert resolved.exists()
    assert resolved.suffix == ".db"


def test_load_default_catalog_returns_real_bundled_catalog() -> None:
    cat = load_default_catalog()
    # Sanity — the bundled catalog has thousands of sites; even a small
    # tag projection should leave plenty of probe-eligible entries.
    assert len(cat.sites) > 100
