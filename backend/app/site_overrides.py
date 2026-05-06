"""Phase 4: admin-managed live overrides on the site catalog.

Stored in the ``site_overrides`` SQLite table (created in
``auth.init_db``). The runtime catalog is rebuilt by ``sites.load_catalog``
on every cache miss; mutations call ``sites.reload_catalog()`` so the
next probe sees the new state without a server restart.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from .auth import db_connection

Operation = Literal["upsert", "delete", "disable"]
ALLOWED_OPS: frozenset[str] = frozenset(("upsert", "delete", "disable"))


@dataclass(frozen=True, slots=True)
class SiteOverride:
    name: str
    operation: Operation
    patch: dict[str, Any]
    created_by: str
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "operation": self.operation,
            "patch": self.patch,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_override(row: Any) -> SiteOverride:
    try:
        patch = json.loads(row["patch_json"]) if row["patch_json"] else {}
    except json.JSONDecodeError:
        patch = {}
    return SiteOverride(
        name=row["name"],
        operation=row["operation"],
        patch=patch,
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_overrides() -> list[SiteOverride]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT name, operation, patch_json, created_by, created_at, updated_at "
            "FROM site_overrides ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_override(r) for r in rows]


def get_override(name: str) -> SiteOverride | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT name, operation, patch_json, created_by, created_at, updated_at "
            "FROM site_overrides WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_override(row)


def upsert_override(
    *,
    name: str,
    operation: Operation,
    patch: dict[str, Any],
    actor: str,
) -> SiteOverride:
    if operation not in ALLOWED_OPS:
        raise ValueError(f"unknown operation: {operation}")
    if not name or not isinstance(name, str):
        raise ValueError("site name is required")
    patch_json = json.dumps(patch, ensure_ascii=False)
    now = int(time.time())
    with db_connection() as conn:
        existing = conn.execute(
            "SELECT created_by, created_at FROM site_overrides WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO site_overrides "
                "(name, operation, patch_json, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, operation, patch_json, actor, now, now),
            )
        else:
            conn.execute(
                "UPDATE site_overrides "
                "SET operation = ?, patch_json = ?, updated_at = ? "
                "WHERE name = ? COLLATE NOCASE",
                (operation, patch_json, now, name),
            )
    return SiteOverride(
        name=name,
        operation=operation,
        patch=patch,
        created_by=existing["created_by"] if existing else actor,
        created_at=existing["created_at"] if existing else now,
        updated_at=now,
    )


def remove_override(name: str) -> bool:
    """Drop an override entry. Returns True iff a row was deleted.

    Note: this *removes the override* (so the site reverts to its
    bundled definition or, for ``custom`` sites, disappears from the
    runtime catalog). To delete a *site* from the runtime, use
    ``upsert_override(operation='delete')`` instead.
    """
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM site_overrides WHERE name = ? COLLATE NOCASE",
            (name,),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Schema validation for incoming admin payloads.
# ---------------------------------------------------------------------------

# Whitelisted keys we accept on the patch payload — anything else is
# silently dropped to prevent admins from injecting unsupported fields
# that would later break the runtime checker.
_ALLOWED_PATCH_KEYS: frozenset[str] = frozenset(
    {
        "url",
        "urlMain",
        "checkType",
        "regexCheck",
        "absenceStrs",
        "presenseStrs",
        "headers",
        "alexaRank",
        "tags",
        "errors",
        "requestHeadOnly",
        "ignore403",
        "engine",
        "prewarm",
        "disabled",
    }
)


def sanitize_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown keys and coerce list-shaped fields to plain lists."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k not in _ALLOWED_PATCH_KEYS:
            continue
        if k in ("absenceStrs", "presenseStrs", "tags") and v is not None:
            if not isinstance(v, list):
                raise ValueError(f"{k} must be a list of strings")
            out[k] = [str(x) for x in v]
        elif k == "headers" and v is not None:
            if not isinstance(v, dict):
                raise ValueError("headers must be an object")
            out[k] = {str(hk): str(hv) for hk, hv in v.items()}
        elif k == "errors" and v is not None:
            if not isinstance(v, dict):
                raise ValueError("errors must be an object")
            out[k] = {str(hk): str(hv) for hk, hv in v.items()}
        else:
            out[k] = v
    return out
