"""Phase 8d: public share links for scan-history items.

A scan owner can mint a URL-safe random token that grants anonymous
read-only access to a single scan via ``GET /share/{token}``. The
public endpoint emits CDN-friendly cache headers so the response can
be served from a Cloudflare / Vercel / Bunny edge node with the API
backend free to focus on running new scans.

Threat model
------------
The token is the only secret. Anyone who has the token gets the same
view that an authenticated owner sees on
``GET /api/history/{scan_id}`` — scan summary plus per-site results.
The owner's username is included (so report consumers know who ran
the scan) but no per-result private metadata is added. Member
passwords, session cookies, and unrelated scans stay invisible.

Tokens are 32 URL-safe characters (192 bits of entropy) so brute-
forcing requires ~2^96 guesses on average; the CDN-side rate limit
plus the backend's ``slowapi`` 429 ceiling make that infeasible.

Lifecycle
---------
1. Owner calls ``POST /api/history/{scan_id}/share`` (auth-gated).
   Server validates ownership, checks the per-scan max link cap, and
   inserts a row into ``share_links`` with a fresh token. Returns
   ``{token, url, expires_at}``.
2. Anyone with the URL hits ``GET /share/{token}``. Server looks up
   the token, returns the scan + results, and emits
   ``Cache-Control: public, s-maxage=<knob>, stale-while-revalidate=
   <knob/2>`` plus an ``ETag`` derived from
   ``hash(token | finished_at)`` for revalidation.
3. Owner can list active links via
   ``GET /api/history/{scan_id}/shares`` and revoke a specific token
   via ``DELETE /api/history/{scan_id}/share/{token}``.

When the parent scan is deleted via ``DELETE /api/history/{scan_id}``
all its share links are cascade-removed; the public endpoint then
returns 404 and a stale CDN entry expires within the cache window.

Storage
-------
Rows live in ``share_links`` (created in :func:`app.auth.init_db`).
Schema is intentionally narrow — no view counts, no last-accessed
timestamp — because tracking those would invalidate the CDN cache on
every hit and the whole point of this batch is to keep edge cache
hits cheap.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .auth import db_connection

# 24 random bytes of base64url ⇒ 32 chars; collision probability is
# 2^-96 over the whole table so we never need a uniqueness retry loop.
_TOKEN_BYTES = 24


@dataclass(frozen=True, slots=True)
class ShareLink:
    token: str
    scan_id: int
    member: str
    created_at: int
    expires_at: int  # 0 means never expires

    def is_expired(self, *, now: int | None = None) -> bool:
        if self.expires_at == 0:
            return False
        ts = now if now is not None else int(time.time())
        return ts >= self.expires_at

    def to_dict(self, *, include_token: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "scan_id": self.scan_id,
            "member": self.member,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if include_token:
            d["token"] = self.token
            d["url"] = f"/share/{self.token}"
        return d


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def etag_for(link: ShareLink, finished_at: int) -> str:
    """Stable ETag for a (token, scan finish) tuple.

    Edge proxies revalidate by sending ``If-None-Match`` on every
    refresh; we hash the token together with the parent scan's
    ``finished_at`` so a re-run of the same scan id (rare but
    possible if an admin reuses scan ids in bespoke setups) busts the
    cache without forcing a token rotation.
    """
    raw = f"{link.token}:{finished_at}".encode()
    return '"' + hashlib.sha256(raw).hexdigest()[:16] + '"'


def mint(
    *,
    scan_id: int,
    member: str,
    ttl_sec: int = 0,
    max_per_scan: int = 5,
) -> ShareLink | None:
    """Create a new share link for ``scan_id`` owned by ``member``.

    Returns ``None`` when the per-scan cap is already reached (caller
    should surface that as a 429 / 409). ``ttl_sec=0`` means the link
    never expires until explicitly revoked.
    """
    now = int(time.time())
    expires_at = 0 if ttl_sec <= 0 else now + ttl_sec

    with db_connection() as conn:
        # Enforce the per-scan cap atomically.
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM share_links "
            "WHERE scan_id = ? AND member = ? COLLATE NOCASE "
            "AND (expires_at = 0 OR expires_at > ?)",
            (scan_id, member, now),
        ).fetchone()
        if active is not None and int(active["n"]) >= max_per_scan:
            return None

        token = _new_token()
        conn.execute(
            "INSERT INTO share_links "
            "(token, scan_id, member, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, scan_id, member, now, expires_at),
        )
    return ShareLink(
        token=token,
        scan_id=scan_id,
        member=member,
        created_at=now,
        expires_at=expires_at,
    )


def lookup(token: str, *, now: int | None = None) -> ShareLink | None:
    """Return the share link for ``token`` if it exists and is not
    expired. Returns ``None`` for unknown or expired tokens."""
    if not token:
        return None
    ts = now if now is not None else int(time.time())
    with db_connection() as conn:
        row = conn.execute(
            "SELECT token, scan_id, member, created_at, expires_at "
            "FROM share_links WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    link = ShareLink(
        token=str(row["token"]),
        scan_id=int(row["scan_id"]),
        member=str(row["member"]),
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
    )
    if link.is_expired(now=ts):
        return None
    return link


def list_for_scan(*, scan_id: int, member: str) -> list[ShareLink]:
    """List active (non-expired) share links for a scan owned by ``member``."""
    now = int(time.time())
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT token, scan_id, member, created_at, expires_at "
            "FROM share_links "
            "WHERE scan_id = ? AND member = ? COLLATE NOCASE "
            "AND (expires_at = 0 OR expires_at > ?) "
            "ORDER BY created_at DESC",
            (scan_id, member, now),
        ).fetchall()
    return [
        ShareLink(
            token=str(r["token"]),
            scan_id=int(r["scan_id"]),
            member=str(r["member"]),
            created_at=int(r["created_at"]),
            expires_at=int(r["expires_at"]),
        )
        for r in rows
    ]


def revoke(*, token: str, member: str) -> bool:
    """Delete the share link if it belongs to ``member``. Returns True
    when a row was removed, False otherwise (unknown token or
    cross-member attempt).

    The owner check is enforced server-side so a member can't revoke
    another member's links by guessing their tokens — the only
    network-visible signal of "member X owns token Y" remains the
    list endpoint.
    """
    if not token:
        return False
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM share_links WHERE token = ? AND member = ? COLLATE NOCASE",
            (token, member),
        )
        return cur.rowcount > 0


def revoke_all_for_scan(*, scan_id: int) -> int:
    """Cascade-delete every share link tied to a scan.

    Called from ``DELETE /api/history/{scan_id}`` so removing a scan
    also invalidates any outstanding public links — the public
    endpoint will then return 404 and a stale CDN entry will expire
    within ``share_link_cache_max_age_sec``.
    """
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM share_links WHERE scan_id = ?",
            (scan_id,),
        )
        return cur.rowcount


def purge_expired(*, now: int | None = None) -> int:
    """Drop expired rows. Cheap to call from the public endpoint as a
    best-effort GC; we don't run a scheduled job because the table
    stays small (one row per share link, bounded by
    ``share_link_max_per_scan`` x scan count)."""
    ts = now if now is not None else int(time.time())
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM share_links WHERE expires_at != 0 AND expires_at <= ?",
            (ts,),
        )
        return cur.rowcount
