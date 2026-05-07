"""Phase 10a: auto-PDF preliminary export on initial CLAIMED target hit.

The :class:`InitialHitTracker` is wired into each SSE search stream
(``_search_stream``, ``_graph_search_stream``, ``_permute_search_stream``)
and is fed every :class:`~app.checker.CheckResult` produced by the
underlying ``stream_checks`` iterator. The first time a result satisfies
all of:

* ``status == Status.CLAIMED``
* ``confidence >= initial_hit_min_confidence`` (admin knob, default 80)
* ``verified is True`` when ``initial_hit_require_verify`` is on
  (admin knob, default 1)
* the result belongs to the *primary* target of the scan — i.e. not a
  Phase 5d graph-secondary spawn or a Phase 5c permutation variant

…the tracker latches (it can only fire once per scan), renders a
preliminary PDF from the in-flight scan state via
:func:`app.exports.to_pdf`, persists it under
``<data>/initial_pdfs/{scan_token}.pdf`` with a sidecar JSON
metadata file, and returns an ``initial_pdf_ready`` SSE event payload
the caller can yield to the SPA.

A 24h TTL janitor (configurable via ``initial_hit_pdf_ttl_sec``) sweeps
the directory periodically so stale preliminary PDFs don't accumulate.

The whole feature is gated behind a single master switch knob
(``enable_initial_hit_pdf``, default off). When the switch is off the
tracker is a no-op — :meth:`InitialHitTracker.consider` always returns
``None`` without touching the disk or the latch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import admin_settings as admin_settings_mod
from . import exports as exports_mod
from .checker import CheckResult, Status
from .config import settings
from .history import ScanMode, ScanSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------


def _pdf_dir() -> Path:
    """Directory where preliminary PDFs are persisted.

    Lives next to the SQLite DB so a single bind-mount on ``/data``
    captures both. Created lazily on the first write.
    """
    return Path(settings.db_path).parent / "initial_pdfs"


def _pdf_path(scan_token: str) -> Path:
    return _pdf_dir() / f"{scan_token}.pdf"


def _meta_path(scan_token: str) -> Path:
    return _pdf_dir() / f"{scan_token}.json"


# ---------------------------------------------------------------------------
# Scan context + tracker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanContext:
    """Per-stream metadata the tracker needs to render a partial PDF."""

    scan_token: str
    member: str
    primary_username: str
    mode: ScanMode
    started_at: int
    sites_total: int


def new_scan_token() -> str:
    """Generate an opaque, URL-safe token for the preliminary PDF.

    Used as the path component in ``/api/history/{scan_token}/initial_pdf``
    and as the file basename on disk. Independent of the eventual
    ``history.scans`` integer id (which is only allocated when the scan
    completes).
    """
    return uuid.uuid4().hex


@dataclass(slots=True)
class InitialHitTracker:
    """Latch the first high-confidence primary CLAIMED hit and emit a PDF.

    A single instance is created per stream invocation. Feed every
    primary-pass :class:`CheckResult` through :meth:`consider`; the
    return value is either ``None`` (no latch) or a dict ready to be
    serialised as an ``initial_pdf_ready`` SSE event by the caller.
    """

    context: ScanContext
    _fired: bool = field(default=False, init=False)

    @staticmethod
    def is_enabled() -> bool:
        """Master switch: ``enable_initial_hit_pdf`` admin knob."""
        try:
            return bool(admin_settings_mod.effective_int("enable_initial_hit_pdf"))
        except Exception:
            return bool(getattr(settings, "enable_initial_hit_pdf", 0))

    @property
    def fired(self) -> bool:
        return self._fired

    async def consider(
        self,
        result: CheckResult,
        *,
        is_primary: bool,
        results_so_far: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Evaluate ``result`` and persist a partial PDF on first match.

        Returns the SSE payload dict on the latch fire, ``None``
        otherwise. The tracker can only fire once: subsequent calls
        always return ``None``.
        """
        if self._fired:
            return None
        if not self.is_enabled():
            return None
        if not self._should_latch(result, is_primary=is_primary):
            return None
        self._fired = True
        try:
            return await self._emit_initial_pdf(result, results_so_far)
        except Exception:  # pragma: no cover — keep the SSE stream alive
            logger.exception(
                "initial_hit: failed to emit preliminary PDF for scan_token=%s",
                self.context.scan_token,
            )
            # Leave the latch fired so we don't retry on every probe.
            return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _knob(key: str) -> int:
        try:
            return admin_settings_mod.effective_int(key)
        except Exception:
            spec = admin_settings_mod.KNOBS_BY_KEY.get(key)
            if spec is None:
                return 0
            return int(getattr(settings, spec.settings_attr))

    def _should_latch(self, result: CheckResult, *, is_primary: bool) -> bool:
        if not is_primary:
            return False
        if result.status != Status.CLAIMED:
            return False
        min_confidence = self._knob("initial_hit_min_confidence")
        if int(result.confidence or 0) < min_confidence:
            return False
        return not (self._knob("initial_hit_require_verify") and result.verified is not True)

    async def _emit_initial_pdf(
        self,
        result: CheckResult,
        results_so_far: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Snapshot the current results list and optionally strip
        # extractor + ML extras (admin knob).
        include_extras = bool(self._knob("initial_hit_include_extras"))
        snapshot = [_clone_result(r, include_extras=include_extras) for r in results_so_far]

        partial_summary = _partial_summary(self.context, snapshot)
        pdf_bytes = await asyncio.to_thread(exports_mod.to_pdf, partial_summary, snapshot)

        token = self.context.scan_token
        pdf_path = _pdf_path(token)
        meta_path = _meta_path(token)
        pdf_dir = pdf_path.parent
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pdf_bytes)

        now = int(time.time())
        meta = {
            "scan_token": token,
            "member": self.context.member,
            "username": self.context.primary_username,
            "mode": self.context.mode,
            "hit_site": result.site,
            "hit_url": result.url,
            "hit_confidence": int(result.confidence or 0),
            "hit_ml_confidence": (
                int(result.ml_confidence) if result.ml_confidence is not None else None
            ),
            "hit_verified": bool(result.verified),
            "results_count": len(snapshot),
            "started_at": self.context.started_at,
            "fired_at": now,
            "ttl_sec": self._knob("initial_hit_pdf_ttl_sec"),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        return {
            "type": "initial_pdf_ready",
            "scan_id": token,
            "scan_token": token,
            "url": f"/api/history/{token}/initial_pdf",
            "hit_site": result.site,
            "hit_url": result.url,
            "hit_confidence": meta["hit_confidence"],
            "hit_verified": meta["hit_verified"],
            "results_count": len(snapshot),
        }


# ---------------------------------------------------------------------------
# Persisted PDF lookups (used by the REST endpoint)
# ---------------------------------------------------------------------------


def read_initial_pdf(scan_token: str) -> bytes | None:
    """Return the persisted preliminary PDF bytes, or ``None``.

    The TTL is *not* enforced here — the janitor task is responsible
    for sweeping stale files. We only check existence so a token that
    has already been purged returns ``None`` (→ HTTP 404 at the route
    layer).
    """
    path = _pdf_path(scan_token)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_initial_pdf_meta(scan_token: str) -> dict[str, Any] | None:
    """Return the sidecar metadata (member, hit info), or ``None``."""
    path = _meta_path(scan_token)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# TTL janitor
# ---------------------------------------------------------------------------


# Cap how many files a single sweep removes so a directory full of
# stale PDFs doesn't block the event loop. The janitor task runs every
# hour by default, which is more than enough headroom for a sane
# steady-state.
_JANITOR_BATCH_LIMIT = 100


def purge_expired(now: int | None = None, *, batch_limit: int = _JANITOR_BATCH_LIMIT) -> int:
    """Delete preliminary PDFs older than the configured TTL.

    Returns the number of (pdf, sidecar) pairs purged. Safe to call
    when the directory is missing — returns 0 in that case.
    """
    pdf_dir = _pdf_dir()
    if not pdf_dir.exists():
        return 0
    if now is None:
        now = int(time.time())
    try:
        ttl = int(admin_settings_mod.effective_int("initial_hit_pdf_ttl_sec"))
    except Exception:
        ttl = int(getattr(settings, "initial_hit_pdf_ttl_sec", 86400))
    cutoff = now - max(ttl, 0)
    removed = 0
    for entry in pdf_dir.iterdir():
        if removed >= batch_limit:
            break
        if entry.suffix != ".pdf":
            continue
        try:
            mtime = int(entry.stat().st_mtime)
        except OSError:
            continue
        if mtime > cutoff:
            continue
        try:
            entry.unlink()
        except OSError:
            continue
        sidecar = entry.with_suffix(".json")
        if sidecar.exists():
            with contextlib.suppress(OSError):
                sidecar.unlink()
        removed += 1
    return removed


JanitorIntervalSec = Literal[60, 300, 3600]


async def janitor_loop(
    *,
    interval_sec: int = 3600,
    sleep: Any = asyncio.sleep,
) -> None:
    """Long-running task that calls :func:`purge_expired` every hour.

    Spawned from FastAPI's startup hook. The ``sleep`` parameter is
    injected so tests can drive the loop deterministically.
    """
    while True:
        try:
            purge_expired()
        except Exception:  # pragma: no cover — never let the task die
            logger.exception("initial_hit: janitor sweep failed")
        await sleep(interval_sec)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "extracted",
        "ml_confidence",
        "signals",
        "reason",
    }
)


def _clone_result(r: dict[str, Any], *, include_extras: bool) -> dict[str, Any]:
    """Shallow-copy a result row, optionally stripping extractor + ML extras."""
    if include_extras:
        return dict(r)
    return {k: v for k, v in r.items() if k not in _EXTRA_KEYS}


def _partial_summary(ctx: ScanContext, results: list[dict[str, Any]]) -> ScanSummary:
    """Build a synthetic :class:`ScanSummary` over the in-flight state.

    The history table only allocates a real ``id`` when the scan
    completes; the partial PDF needs *something* with the right shape
    for :func:`exports.to_pdf`, so we use ``id=0`` as a sentinel and
    set ``finished_at`` to the current wall clock.
    """
    counts = {"claimed": 0, "free": 0, "blocked": 0, "unknown": 0, "error": 0}
    for r in results:
        s = r.get("status")
        if isinstance(s, str) and s in counts:
            counts[s] += 1
    return ScanSummary(
        id=0,
        member=ctx.member,
        username=ctx.primary_username,
        mode=ctx.mode,
        started_at=ctx.started_at,
        finished_at=int(time.time()),
        site_count=ctx.sites_total,
        claimed=counts["claimed"],
        free=counts["free"],
        blocked=counts["blocked"],
        unknown=counts["unknown"],
        error_count=counts["error"],
    )
