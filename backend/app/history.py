"""Phase 4: per-member scan history with diff computation.

Each completed ``/api/search`` and ``/api/search/graph`` stream is
recorded in the ``scans`` table (created in ``auth.init_db``). To keep
storage bounded we persist a *summary* of each result rather than the
full payload — just enough to render a list view and compute diffs.

Schema layout:
    id, member, username, mode, started_at, finished_at,
    site_count, claimed, free, blocked, unknown, error_count,
    results_json

``results_json`` is a JSON array of ``[site, status, confidence,
ml_confidence, url, parent_username]`` tuples. We keep the rolling
window per member capped at ``settings.scan_history_max_per_user``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from .auth import db_connection
from .config import settings

ScanMode = Literal["list", "graph"]

# How many keys we serialise per result. Keep this stable — adding new
# entries breaks the diff algorithm against historical scans.
_SUMMARY_KEYS: tuple[str, ...] = (
    "site",
    "status",
    "confidence",
    "ml_confidence",
    "url",
    "parent_username",
)


@dataclass(frozen=True, slots=True)
class ScanSummary:
    id: int
    member: str
    username: str
    mode: ScanMode
    started_at: int
    finished_at: int
    site_count: int
    claimed: int
    free: int
    blocked: int
    unknown: int
    error_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "member": self.member,
            "username": self.username,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": (self.finished_at - self.started_at) * 1000,
            "site_count": self.site_count,
            "claimed": self.claimed,
            "free": self.free,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "error_count": self.error_count,
        }


def _summarise(results: list[dict[str, Any]]) -> list[list[Any]]:
    """Project full result dicts to compact tuples for storage."""
    out: list[list[Any]] = []
    for r in results:
        row: list[Any] = []
        for key in _SUMMARY_KEYS:
            row.append(r.get(key))
        out.append(row)
    return out


def _hydrate(rows: list[list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        # Tolerate older entries with fewer columns by zip-shortest.
        d: dict[str, Any] = {}
        for k, v in zip(_SUMMARY_KEYS, row, strict=False):
            d[k] = v
        out.append(d)
    return out


def _counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"claimed": 0, "free": 0, "blocked": 0, "unknown": 0, "error": 0}
    for r in results:
        s = r.get("status")
        if s in counts:
            counts[s] += 1
    return counts


def record_scan(
    *,
    member: str,
    username: str,
    mode: ScanMode,
    started_at: int,
    finished_at: int,
    site_count: int,
    results: list[dict[str, Any]],
) -> int | None:
    """Persist a finished scan. Returns the new row id, or ``None`` when
    history is disabled."""
    if not settings.enable_scan_history:
        return None
    counts = _counts(results)
    summary = _summarise(results)
    with db_connection() as conn:
        cur = conn.execute(
            "INSERT INTO scans "
            "(member, username, mode, started_at, finished_at, site_count, "
            " claimed, free, blocked, unknown, error_count, results_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member,
                username,
                mode,
                started_at,
                finished_at,
                site_count,
                counts["claimed"],
                counts["free"],
                counts["blocked"],
                counts["unknown"],
                counts["error"],
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        new_id = cur.lastrowid

        # Trim the rolling window per (member, username) to keep storage
        # bounded.
        conn.execute(
            "DELETE FROM scans WHERE id IN ("
            "  SELECT id FROM scans "
            "  WHERE member = ? COLLATE NOCASE AND username = ? "
            "  ORDER BY finished_at DESC "
            "  LIMIT -1 OFFSET ?"
            ")",
            (member, username, settings.scan_history_max_per_user),
        )
    return new_id


def list_scans(
    *,
    member: str,
    username: str | None = None,
    limit: int = 50,
) -> list[ScanSummary]:
    sql = (
        "SELECT id, member, username, mode, started_at, finished_at, site_count, "
        " claimed, free, blocked, unknown, error_count "
        "FROM scans WHERE member = ? COLLATE NOCASE "
    )
    params: list[Any] = [member]
    if username:
        sql += "AND username = ? "
        params.append(username)
    sql += "ORDER BY finished_at DESC LIMIT ?"
    params.append(limit)
    with db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        ScanSummary(
            id=r["id"],
            member=r["member"],
            username=r["username"],
            mode=r["mode"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            site_count=r["site_count"],
            claimed=r["claimed"],
            free=r["free"],
            blocked=r["blocked"],
            unknown=r["unknown"],
            error_count=r["error_count"],
        )
        for r in rows
    ]


def get_scan(scan_id: int, member: str) -> tuple[ScanSummary, list[dict[str, Any]]] | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id, member, username, mode, started_at, finished_at, site_count, "
            " claimed, free, blocked, unknown, error_count, results_json "
            "FROM scans WHERE id = ? AND member = ? COLLATE NOCASE",
            (scan_id, member),
        ).fetchone()
    if row is None:
        return None
    summary = ScanSummary(
        id=row["id"],
        member=row["member"],
        username=row["username"],
        mode=row["mode"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        site_count=row["site_count"],
        claimed=row["claimed"],
        free=row["free"],
        blocked=row["blocked"],
        unknown=row["unknown"],
        error_count=row["error_count"],
    )
    try:
        rows = json.loads(row["results_json"])
    except json.JSONDecodeError:
        rows = []
    return summary, _hydrate(rows)


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


def diff_scans(
    *,
    member: str,
    from_id: int,
    to_id: int,
) -> dict[str, Any] | None:
    """Compare two scans by id. Both must belong to ``member``.

    The diff is computed per-site:
      - ``newly_claimed``  : site went UNKNOWN/FREE/ERROR → CLAIMED
      - ``no_longer_claimed``: site went CLAIMED → UNKNOWN/FREE/ERROR
      - ``status_changed``  : any other status change
      - ``confidence_shift``: same status but |Δconfidence| ≥ 15
      - ``unchanged_count`` : sites with identical (status, ±15 confidence)

    Returns ``None`` if either scan is missing.
    """
    a = get_scan(from_id, member)
    b = get_scan(to_id, member)
    if a is None or b is None:
        return None
    a_summary, a_results = a
    b_summary, b_results = b
    a_index = {r["site"]: r for r in a_results}
    b_index = {r["site"]: r for r in b_results}
    sites = sorted(set(a_index) | set(b_index))

    newly_claimed: list[dict[str, Any]] = []
    no_longer_claimed: list[dict[str, Any]] = []
    status_changed: list[dict[str, Any]] = []
    confidence_shift: list[dict[str, Any]] = []
    unchanged = 0

    for site in sites:
        a_row = a_index.get(site)
        b_row = b_index.get(site)
        if a_row is None and b_row is not None:
            if b_row.get("status") == "claimed":
                newly_claimed.append({"site": site, **b_row})
            else:
                status_changed.append(
                    {"site": site, "from": None, "to": b_row.get("status")}
                )
            continue
        if b_row is None and a_row is not None:
            if a_row.get("status") == "claimed":
                no_longer_claimed.append({"site": site, **a_row})
            else:
                status_changed.append(
                    {"site": site, "from": a_row.get("status"), "to": None}
                )
            continue
        # Both present.
        assert a_row is not None and b_row is not None
        a_status = a_row.get("status")
        b_status = b_row.get("status")
        if a_status != b_status:
            if b_status == "claimed":
                newly_claimed.append({"site": site, **b_row})
            elif a_status == "claimed":
                no_longer_claimed.append({"site": site, **a_row, "new_status": b_status})
            else:
                status_changed.append(
                    {"site": site, "from": a_status, "to": b_status}
                )
            continue
        # Same status — check confidence drift.
        ac = a_row.get("confidence") or 0
        bc = b_row.get("confidence") or 0
        if abs(ac - bc) >= 15:
            confidence_shift.append(
                {
                    "site": site,
                    "status": b_status,
                    "from_confidence": ac,
                    "to_confidence": bc,
                }
            )
        else:
            unchanged += 1

    return {
        "from": a_summary.to_dict(),
        "to": b_summary.to_dict(),
        "newly_claimed": newly_claimed,
        "no_longer_claimed": no_longer_claimed,
        "status_changed": status_changed,
        "confidence_shift": confidence_shift,
        "unchanged_count": unchanged,
    }


def latest_scan_for(*, member: str, username: str) -> ScanSummary | None:
    rows = list_scans(member=member, username=username, limit=1)
    return rows[0] if rows else None


def delete_scan(scan_id: int, member: str) -> bool:
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM scans WHERE id = ? AND member = ? COLLATE NOCASE",
            (scan_id, member),
        )
    return cur.rowcount > 0


def now_unix() -> int:
    return int(time.time())
