"""Phase 10f: ``reckora-cli`` console-script — public Python API.

The CLI exists so a maigret-style one-liner scan is available without
spinning up the FastAPI server. Two thin layers cooperate:

* :func:`scan` — async generator that yields :class:`CheckResult` rows
  as they arrive. Reuses ``app.checker`` / ``app.sites`` / ``app.identity``
  so the wrapper is genuinely thin (zero re-implementation of probe logic).
* :func:`build_scan_summary` — builds a :class:`ScanSummary` from the
  finished result list so ``app.exports`` can render the same XMind /
  HTML / PDF / TXT / D3 / CSV / JSON / Markdown the SPA gets.

CLI mode deliberately *skips* SQLite for non-essential subsystems
(history, share-links, admin-overrides). To still exercise the
runtime knobs that proxy_pool / host_rate_limit / anti-bot read off
``admin_settings``, we initialise the schema in a tempfile DB at
import time so every ``effective_int`` falls through to the
``Settings`` env-var default. An operator who wants real overrides
passes ``--admin-db /path/to/reckora.db`` to the front-end and the
same env-var driven config is honoured with the live overrides
mirrored in.

Backwards compatibility: importing this module never touches the
network. Every entry point is opt-in by call site.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from ..checker import CheckResult, Status, stream_checks
from ..exports import ALLOWED_FORMATS, ExportFormat, render
from ..history import ScanSummary
from ..identity import (
    ALL_IDENTITY_TYPES,
    DEFAULT_IDENTITY_TYPE,
    IDENTITY_TYPE_LABELS,
    IdentityType,
    is_valid_identifier,
    normalise_identifier,
)
from ..sites import Site, SiteCatalog, load_catalog

__all__ = [
    "ALLOWED_FORMATS",
    "ALL_IDENTITY_TYPES",
    "DEFAULT_IDENTITY_TYPE",
    "IDENTITY_TYPE_LABELS",
    "CliInputError",
    "IdentityType",
    "build_scan_summary",
    "ensure_admin_db",
    "load_default_catalog",
    "render_report",
    "scan",
    "select_sites",
]


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class CliInputError(ValueError):
    """Raised for argv parse failures the front-end maps to exit code 2."""


# ---------------------------------------------------------------------------
# Admin-DB bootstrap
# ---------------------------------------------------------------------------


def ensure_admin_db(admin_db: Path | str | None = None) -> Path:
    """Resolve which SQLite file the CLI should attach to.

    When ``admin_db`` is ``None`` we hand the runtime an *empty* but
    schema-valid database in a tempfile so subsystems that read
    ``admin_settings`` (proxy_pool, host_rate_limit, anti_bot) fall
    through cleanly to the env-var default for every knob without
    raising ``OperationalError`` on the missing tables. With an
    explicit ``admin_db`` the operator gets parity with the host's
    overrides.

    Returns the absolute path that ``settings.db_path`` was set to so
    the caller can log it. Safe to call multiple times — a second call
    with the same target is a no-op.
    """
    from ..config import settings

    if admin_db is not None:
        target = Path(admin_db).expanduser().resolve()
    else:
        # ``mkstemp`` so the file exists with 0600 perms before sqlite
        # opens it; ``delete=False`` keeps it alive across the scan.
        fd, name = tempfile.mkstemp(prefix="reckora-cli-", suffix=".db")
        os.close(fd)
        target = Path(name)

    target.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path = target

    # Force a fresh ``sqlite3.Connection`` so the next ``init_db`` /
    # query opens *this* file, not whatever the previous test or
    # process pinned. We import lazily because ``app.auth`` pulls in
    # signing-secret bootstrap and we want CLI startup to stay quiet.
    from .. import auth as auth_mod

    auth_mod.init_db()

    # Drop every cached override so the next ``effective_int`` re-reads
    # against the new file.
    from .. import admin_settings as admin_settings_mod

    admin_settings_mod._invalidate_cache()
    return target


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------


def load_default_catalog() -> SiteCatalog:
    """Return the bundled ``sites.json`` catalog (admin overrides applied
    when an admin DB is attached)."""
    return load_catalog()


def select_sites(
    catalog: SiteCatalog,
    *,
    identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    tags: Iterable[str] = (),
    limit: int | None = None,
    include_tentative: bool = False,
) -> list[Site]:
    """Project the catalog down to the probe-eligible subset.

    Pass ``include_tentative=True`` to opt the Phase 10b precision-trim
    sites in. The default mirrors the live HF Space deployment which
    keeps ``enable_tentative_sites=0`` until an operator flips it.
    """
    return catalog.filtered(
        tags=list(tags) if tags else None,
        limit=limit,
        identity_type=identity_type,
        include_tentative=include_tentative,
    )


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------


async def scan(
    username: str,
    *,
    identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    tags: Iterable[str] = (),
    limit: int | None = None,
    include_tentative: bool = False,
    catalog: SiteCatalog | None = None,
    verify: bool = True,
    extract: bool = True,
) -> AsyncIterator[CheckResult]:
    """Scan ``username`` across the catalog and yield results as they arrive.

    Mirrors the parameter shape of ``checker.stream_checks`` plus the
    catalog-projection knobs (``tags`` / ``limit`` / ``include_tentative``)
    that are otherwise applied by the FastAPI ``/api/search`` endpoint.

    The username is *normalised + validated* before fan-out — invalid
    identifiers raise :class:`CliInputError` rather than spamming the
    catalog with malformed URLs.
    """
    normalised = normalise_identifier(username, identity_type)
    if not normalised:
        raise CliInputError("identifier is empty after normalisation")
    if not is_valid_identifier(normalised, identity_type):
        label = IDENTITY_TYPE_LABELS.get(identity_type, identity_type)
        raise CliInputError(f"{normalised!r} is not a valid {label}")

    cat = catalog if catalog is not None else load_default_catalog()
    sites = select_sites(
        cat,
        identity_type=identity_type,
        tags=tags,
        limit=limit,
        include_tentative=include_tentative,
    )
    if not sites:
        return

    async for result in stream_checks(
        sites,
        normalised,
        identity_type=identity_type,
        verify=verify,
        extract=extract,
    ):
        yield result


# ---------------------------------------------------------------------------
# Summary + report rendering
# ---------------------------------------------------------------------------


def build_scan_summary(
    results: list[CheckResult],
    *,
    username: str,
    started_at: int,
    finished_at: int,
    site_count: int,
    member: str = "cli",
    scan_id: int = 0,
) -> ScanSummary:
    """Build a :class:`ScanSummary` from a finished list of CheckResults.

    The CLI is single-user / single-process so ``member`` and ``scan_id``
    are stable placeholders rather than DB-issued ids. The status
    counts mirror :func:`app.history._counts` so any export renderer
    sees the same shape it would for a server-side scan.
    """
    counts: dict[str, int] = {"claimed": 0, "free": 0, "blocked": 0, "unknown": 0, "error": 0}
    for r in results:
        key = r.status.value
        if key in counts:
            counts[key] += 1
    return ScanSummary(
        id=scan_id,
        member=member,
        username=username,
        mode="list",
        started_at=started_at,
        finished_at=finished_at,
        site_count=site_count,
        claimed=counts["claimed"],
        free=counts["free"],
        blocked=counts["blocked"],
        unknown=counts["unknown"],
        error_count=counts["error"],
    )


def _result_dict(r: CheckResult) -> dict[str, Any]:
    """Project a CheckResult to the dict shape every export expects."""
    payload = r.to_dict()
    # The export renderers also consume ``ml_confidence`` and
    # ``parent_username`` (Phase 5d). The base ``CheckResult`` carries
    # the former; the latter is only set on graph-secondary scans which
    # the CLI doesn't emit, so default to None.
    payload.setdefault("ml_confidence", None)
    payload.setdefault("parent_username", None)
    return payload


def render_report(
    fmt: ExportFormat,
    summary: ScanSummary,
    results: list[CheckResult],
) -> tuple[bytes, str, str]:
    """Render ``results`` to ``(body_bytes, media_type, filename)``.

    Thin wrapper around ``app.exports.render`` that handles the
    CheckResult → dict projection so the CLI never has to know about
    the in-memory result shape.
    """
    payload = [_result_dict(r) for r in results]
    return render(fmt, summary, payload)


def claimed_results(results: list[CheckResult]) -> list[CheckResult]:
    """Return the CLAIMED subset — handy for callers that want to make
    "did the scan find anything?" decisions without recomputing."""
    return [r for r in results if r.status is Status.CLAIMED]
