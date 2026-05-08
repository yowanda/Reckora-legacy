"""Phase 10f: ``reckora`` console-script — argparse front-end.

Usage examples (matches the maigret one-liner shape so an analyst can
swap tools without re-learning the surface):

    reckora torvalds --json out.json
    reckora torvalds --html out.html --pdf out.pdf
    reckora torvalds --tags photo,dating --md out.md
    reckora foo@example.com --identity-type email --csv out.csv
    reckora torvalds --xmind torvalds.xmind --d3 graph.html --txt out.txt
    reckora --list-tags
    reckora --list-identity-types

Exit codes:

* ``0`` — at least one CLAIMED hit
* ``2`` — argv parse failure (also raised by argparse on its own)
* ``3`` — scan completed but no CLAIMED hits
* ``4`` — scan failed: every probed site errored or no sites matched
* ``130`` — user pressed Ctrl-C (SIGINT)

The CLI is intentionally **batch-mode**: results are written to disk
as soon as the scan completes. Streaming progress goes to stderr so
``reckora ... --print-json > out.json`` stays parseable.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO

from ..checker import CheckResult, Status
from ..exports import ExportFormat
from ..identity import (
    IDENTITY_TYPE_LABELS,
    is_valid_identifier,
    normalise_identifier,
)
from ..sites import SiteCatalog
from . import (
    ALL_IDENTITY_TYPES,
    ALLOWED_FORMATS,
    DEFAULT_IDENTITY_TYPE,
    CliInputError,
    build_scan_summary,
    ensure_admin_db,
    load_default_catalog,
    render_report,
    scan,
    select_sites,
)

# Exit-code constants — kept as module-level so tests can assert
# against the same names rather than scattered magic numbers.
EXIT_OK = 0
EXIT_ARGV = 2
EXIT_NO_HITS = 3
EXIT_NO_SITES = 4
EXIT_INTERRUPTED = 130

# argparse → ``--<fmt>`` flag suffix mapping. ``markdown`` is the long
# name in ``ExportFormat`` but operators expect ``--md`` so we keep
# both names visible on the CLI surface.
_REPORT_FLAGS: tuple[tuple[str, ExportFormat, str], ...] = (
    ("--csv", "csv", "Write CSV report to PATH."),
    ("--json", "json", "Write JSON report to PATH."),
    ("--md", "markdown", "Write Markdown report to PATH."),
    ("--html", "html", "Write HTML report to PATH."),
    ("--pdf", "pdf", "Write PDF report to PATH (falls back to HTML if weasyprint unavailable)."),
    ("--txt", "txt", "Write plain-text dossier to PATH."),
    ("--xmind", "xmind", "Write XMind8 workbook to PATH."),
    ("--d3", "d3", "Write self-contained D3 force-directed graph HTML to PATH."),
)


# ---------------------------------------------------------------------------
# argparse construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. Exposed so tests can drive it directly."""
    parser = argparse.ArgumentParser(
        prog="reckora",
        description=(
            "Reckora username footprint scanner — command-line front-end. "
            "Probes ~2,500 sites for the supplied identifier and writes "
            "one or more reports."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "identifier",
        nargs="?",
        help="Identifier to scan (username, email, phone, ...).",
    )
    parser.add_argument(
        "-i",
        "--identity-type",
        choices=list(ALL_IDENTITY_TYPES),
        default=DEFAULT_IDENTITY_TYPE,
        help="Type of identifier (default: username).",
    )
    parser.add_argument(
        "-t",
        "--tags",
        default="",
        help="Comma-separated tag filter (e.g. 'photo,dating'). Empty = no filter.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Cap on the number of probed sites (default: probe every match).",
    )
    parser.add_argument(
        "--include-tentative",
        action="store_true",
        help="Include Phase 10b tentative sites in the probe set (default: off).",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        default=True,
        help="Skip the Phase 2 second-stage verify probe.",
    )
    parser.add_argument(
        "--no-extract",
        dest="extract",
        action="store_false",
        default=True,
        help="Skip the Phase 3 identity extractor on CLAIMED hits.",
    )
    parser.add_argument(
        "--admin-db",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Attach to a host's admin SQLite DB so runtime knob "
            "overrides (proxy pool, anti-bot, host rate limit, ...) "
            "take effect. Default: empty in-memory tempfile, every "
            "knob falls back to its env-var default."
        ),
    )
    for flag, _fmt, helptext in _REPORT_FLAGS:
        parser.add_argument(
            flag,
            type=Path,
            default=None,
            metavar="PATH",
            help=helptext,
        )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Echo the JSON report to stdout in addition to (or instead of) writing files.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-site progress output on stderr.",
    )
    parser.add_argument(
        "--list-tags",
        action="store_true",
        help="Print every tag in the catalog (sorted by site count) and exit.",
    )
    parser.add_argument(
        "--list-identity-types",
        action="store_true",
        help="Print every supported identity type and exit.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"reckora {_package_version()}",
    )
    return parser


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover — argparse formats the message
        raise argparse.ArgumentTypeError(f"expected int, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _package_version() -> str:
    """Best-effort introspection of the installed package version."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("reckora-backend")
    except PackageNotFoundError:  # pragma: no cover — fallback for source checkouts
        return "0.0.0+local"


# ---------------------------------------------------------------------------
# Helper: parse the ``--tags`` CSV blob into the canonical tuple shape
# ---------------------------------------------------------------------------


def parse_tags(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(t.strip() for t in raw.split(",") if t.strip())


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit(stream: IO[str], message: str, *, quiet: bool) -> None:
    if quiet:
        return
    stream.write(message)
    stream.flush()


def _format_status(r: CheckResult) -> str:
    label = r.status.value.upper()
    return f"[{label:<7}]"


def _progress_line(r: CheckResult, *, total: int, done: int) -> str:
    bits = [
        f"{done:>4}/{total:<4}",
        _format_status(r),
        r.site,
    ]
    if r.status is Status.CLAIMED:
        bits.append(f"-> {r.url}")
        if r.confidence:
            bits.append(f"(conf {r.confidence})")
    elif r.status is Status.ERROR and r.error:
        bits.append(f"({r.error})")
    return "  ".join(bits) + "\n"


# ---------------------------------------------------------------------------
# Catalog meta commands
# ---------------------------------------------------------------------------


def _print_tags(stream: IO[str], catalog: SiteCatalog) -> None:
    rows = catalog.all_tags()
    if not rows:
        stream.write("(catalog has no tagged sites)\n")
        return
    width = max(len(tag) for tag, _ in rows)
    for tag, count in rows:
        stream.write(f"{tag.ljust(width)}  {count}\n")


def _print_identity_types(stream: IO[str]) -> None:
    width = max(len(t) for t in ALL_IDENTITY_TYPES)
    for t in ALL_IDENTITY_TYPES:
        label = IDENTITY_TYPE_LABELS.get(t, t)
        stream.write(f"{t.ljust(width)}  {label}\n")


# ---------------------------------------------------------------------------
# Report emission
# ---------------------------------------------------------------------------


def _collect_report_paths(args: argparse.Namespace) -> list[tuple[ExportFormat, Path]]:
    out: list[tuple[ExportFormat, Path]] = []
    seen: set[ExportFormat] = set()
    for flag, fmt, _help in _REPORT_FLAGS:
        attr = flag.lstrip("-").replace("-", "_")
        path = getattr(args, attr, None)
        if path is None:
            continue
        if fmt in seen:
            raise CliInputError(f"--{attr} specified more than once")
        if fmt not in ALLOWED_FORMATS:  # pragma: no cover — guarded by _REPORT_FLAGS
            raise CliInputError(f"unknown export format: {fmt}")
        seen.add(fmt)
        out.append((fmt, path))
    return out


def _write_reports(
    targets: Iterable[tuple[ExportFormat, Path]],
    summary: object,
    results: list[CheckResult],
    *,
    log: IO[str],
    quiet: bool,
) -> None:
    for fmt, path in targets:
        body, _media, _filename = render_report(fmt, summary, results)  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        _emit(log, f"wrote {fmt} -> {path} ({len(body)} bytes)\n", quiet=quiet)


# ---------------------------------------------------------------------------
# SIGINT handling
# ---------------------------------------------------------------------------


class _CancelToken:
    """Mutable flag the SIGINT handler flips so the scan loop can break."""

    __slots__ = ("triggered",)

    def __init__(self) -> None:
        self.triggered = False


# ---------------------------------------------------------------------------
# Async driver
# ---------------------------------------------------------------------------


async def _run_scan(
    args: argparse.Namespace,
    *,
    cancel: _CancelToken,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    # Validate the identifier *before* consulting the catalog so the
    # operator gets a precise "not a valid <type>" message rather than
    # a generic "no sites matched" — the same shape ``scan()`` would
    # have raised if it ever began iterating.
    normalised = normalise_identifier(args.identifier, args.identity_type)
    if not normalised:
        raise CliInputError("identifier is empty after normalisation")
    if not is_valid_identifier(normalised, args.identity_type):
        label = IDENTITY_TYPE_LABELS.get(args.identity_type, args.identity_type)
        raise CliInputError(f"{normalised!r} is not a valid {label}")

    catalog = load_default_catalog()
    tags = parse_tags(args.tags)
    sites = select_sites(
        catalog,
        identity_type=args.identity_type,
        tags=tags,
        limit=args.limit,
        include_tentative=args.include_tentative,
    )
    if not sites:
        stderr.write("no sites matched the given filters; nothing to do\n")
        return EXIT_NO_SITES

    _emit(
        stderr,
        f"probing {len(sites)} sites for {args.identifier!r} ({args.identity_type})\n",
        quiet=args.quiet,
    )

    started_at = int(time.time())
    results: list[CheckResult] = []
    done = 0
    async for r in scan(
        args.identifier,
        identity_type=args.identity_type,
        tags=tags,
        limit=args.limit,
        include_tentative=args.include_tentative,
        catalog=catalog,
        verify=args.verify,
        extract=args.extract,
    ):
        if cancel.triggered:
            break
        results.append(r)
        done += 1
        _emit(stderr, _progress_line(r, total=len(sites), done=done), quiet=args.quiet)
    finished_at = int(time.time())

    if cancel.triggered:
        stderr.write("scan interrupted; partial results discarded\n")
        return EXIT_INTERRUPTED

    summary = build_scan_summary(
        results,
        username=args.identifier,
        started_at=started_at,
        finished_at=finished_at,
        site_count=len(sites),
    )

    targets = _collect_report_paths(args)
    _write_reports(targets, summary, results, log=stderr, quiet=args.quiet)

    if args.print_json:
        body, _media, _filename = render_report("json", summary, results)
        stdout.write(body.decode("utf-8"))

    claimed = [r for r in results if r.status is Status.CLAIMED]
    if not args.quiet:
        stderr.write(
            f"done — {summary.claimed} claimed · {summary.free} free · "
            f"{summary.blocked} blocked · {summary.unknown} unknown · "
            f"{summary.error_count} errors "
            f"({finished_at - started_at}s)\n"
        )
    if not claimed:
        return EXIT_NO_HITS
    return EXIT_OK


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Programmatic entry point for the ``reckora`` console-script.

    Tests call this directly with a fabricated ``argv`` and captured
    streams. The real script wires it via the ``[project.scripts]``
    table in ``pyproject.toml`` and pipes through ``sys.exit``.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits with 2 on bad argv; surface the same code.
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ARGV

    if args.list_identity_types:
        _print_identity_types(out)
        return EXIT_OK

    ensure_admin_db(args.admin_db)

    if args.list_tags:
        _print_tags(out, load_default_catalog())
        return EXIT_OK

    if not args.identifier:
        err.write("error: identifier is required (or pass --list-tags / --list-identity-types)\n")
        return EXIT_ARGV

    cancel = _CancelToken()

    def _handle_sigint(_signum: int, _frame: object) -> None:
        cancel.triggered = True

    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except ValueError:  # pragma: no cover — running outside the main thread
        previous = None

    try:
        return asyncio.run(_run_scan(args, cancel=cancel, stdout=out, stderr=err))
    except CliInputError as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ARGV
    except KeyboardInterrupt:  # pragma: no cover — handled by cancel token
        err.write("scan interrupted; partial results discarded\n")
        return EXIT_INTERRUPTED
    except Exception as exc:
        err.write(f"error: {type(exc).__name__}: {exc}\n")
        # ``--print-json`` callers expect *something* on stdout even on
        # failure so a downstream pipeline doesn't hang waiting for
        # bytes; emit a minimal envelope they can still ``json.loads``.
        if getattr(args, "print_json", False):
            out.write(json.dumps({"error": str(exc)}) + "\n")
        return EXIT_NO_SITES
    finally:
        if previous is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":  # pragma: no cover — direct ``python -m`` usage
    raise SystemExit(main())
