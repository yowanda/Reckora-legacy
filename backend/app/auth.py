"""Member authentication: SQLite-backed users, signed-cookie sessions, password hashing.

No third-party crypto dependencies — uses stdlib `hashlib.pbkdf2_hmac` for passwords
and `hmac` for signing session cookies. Sessions are stateless: the cookie payload
itself carries `{username, role, exp}` and is verified via HMAC-SHA256.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from .config import settings

PBKDF2_ITERATIONS = 200_000
PBKDF2_DKLEN = 32
PBKDF2_HASH = "sha256"
SALT_BYTES = 16
SESSION_COOKIE = "reckora_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
USERNAME_RE = (
    # 3-32 chars, letters/digits/_-. and must start with a letter or digit
    "^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$"
)
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
ROLES = frozenset({"admin", "member"})


@dataclass(frozen=True)
class Member:
    username: str
    role: str
    created_at: int


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        PBKDF2_HASH, password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=PBKDF2_DKLEN
    )
    return f"pbkdf2_{PBKDF2_HASH}${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, dk_b64 = stored.split("$", 3)
        if not algo.startswith("pbkdf2_"):
            return False
        algo_name = algo[len("pbkdf2_"):]
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        algo_name, password.encode("utf-8"), salt, iters, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


_DB_LOCK = RLock()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        conn = _connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()


def init_db() -> None:
    with db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                username      TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL CHECK(role IN ('admin','member')),
                created_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_secret (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                secret  TEXT NOT NULL
            );

            -- Phase 4: admin-managed live overrides on top of the bundled
            -- ``sites.json``. ``operation`` is one of upsert/delete/disable;
            -- ``patch_json`` carries the raw site definition (or partial
            -- patch) merged onto the base catalog at runtime.
            CREATE TABLE IF NOT EXISTS site_overrides (
                name        TEXT PRIMARY KEY COLLATE NOCASE,
                operation   TEXT NOT NULL CHECK(operation IN ('upsert','delete','disable')),
                patch_json  TEXT NOT NULL,
                created_by  TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );

            -- Phase 4: per-member scan history. ``results_json`` is a
            -- compact JSON array of {site, status, confidence, ml_confidence,
            -- url} tuples; the full result payload is intentionally NOT
            -- kept (storage cost is bounded at ~3 KB per scan).
            CREATE TABLE IF NOT EXISTS scans (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                member       TEXT NOT NULL COLLATE NOCASE,
                username     TEXT NOT NULL,
                mode         TEXT NOT NULL CHECK(mode IN ('list','graph')),
                started_at   INTEGER NOT NULL,
                finished_at  INTEGER NOT NULL,
                site_count   INTEGER NOT NULL,
                claimed      INTEGER NOT NULL,
                free         INTEGER NOT NULL,
                blocked      INTEGER NOT NULL,
                unknown      INTEGER NOT NULL,
                error_count  INTEGER NOT NULL,
                results_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scans_member_username
                ON scans(member, username, finished_at DESC);
            """
        )


def _ensure_session_secret() -> bytes:
    """Return the persistent HMAC secret used to sign session cookies."""
    env_secret = os.getenv("RECKORA_SESSION_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    with db_connection() as conn:
        row = conn.execute("SELECT secret FROM session_secret WHERE id = 1").fetchone()
        if row is not None:
            return row["secret"].encode("utf-8")
        new_secret = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO session_secret (id, secret) VALUES (1, ?)",
            (new_secret,),
        )
        return new_secret.encode("utf-8")


def seed_admin_from_env() -> None:
    """Create the seed admin from env if it doesn't already exist."""
    username = os.getenv("RECKORA_ADMIN_USERNAME", "").strip()
    password = os.getenv("RECKORA_ADMIN_PASSWORD", "").strip()
    if not username or not password:
        return
    with db_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM members WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO members (username, password_hash, role, created_at) "
            "VALUES (?, ?, 'admin', ?)",
            (username, hash_password(password), int(time.time())),
        )


def list_members() -> list[Member]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT username, role, created_at FROM members ORDER BY created_at ASC"
        ).fetchall()
    return [Member(username=r["username"], role=r["role"], created_at=r["created_at"]) for r in rows]


def get_member(username: str) -> Member | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT username, role, created_at FROM members WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return Member(username=row["username"], role=row["role"], created_at=row["created_at"])


def get_member_with_hash(username: str) -> tuple[Member, str] | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT username, role, created_at, password_hash FROM members "
            "WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if row is None:
        return None
    member = Member(username=row["username"], role=row["role"], created_at=row["created_at"])
    return member, row["password_hash"]


def create_member(username: str, password: str, role: str) -> Member:
    if role not in ROLES:
        raise ValueError(f"invalid role: {role!r}")
    member = Member(username=username, role=role, created_at=int(time.time()))
    with db_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO members (username, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, member.created_at),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError("username already exists") from e
    return member


def delete_member(username: str) -> bool:
    with db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM members WHERE username = ? COLLATE NOCASE", (username,)
        )
        return cur.rowcount > 0


def update_member(
    username: str,
    *,
    role: str | None = None,
    password: str | None = None,
) -> Member | None:
    if role is not None and role not in ROLES:
        raise ValueError(f"invalid role: {role!r}")
    sets: list[str] = []
    args: list[object] = []
    if role is not None:
        sets.append("role = ?")
        args.append(role)
    if password is not None:
        sets.append("password_hash = ?")
        args.append(hash_password(password))
    if not sets:
        return get_member(username)
    args.append(username)
    with db_connection() as conn:
        cur = conn.execute(
            f"UPDATE members SET {', '.join(sets)} "
            f"WHERE username = ? COLLATE NOCASE",
            args,
        )
        if cur.rowcount == 0:
            return None
    return get_member(username)


def count_admins() -> int:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM members WHERE role = 'admin'"
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# session signing
# ---------------------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_session(member: Member, *, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = {
        "u": member.username,
        "r": member.role,
        "e": int(time.time()) + ttl,
    }
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_ensure_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def verify_session(token: str) -> Member | None:
    if not token or "." not in token:
        return None
    body, sig_b64 = token.rsplit(".", 1)
    expected = hmac.new(_ensure_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64u_decode(sig_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("e", 0)) < int(time.time()):
        return None
    username = str(payload.get("u", ""))
    role = str(payload.get("r", ""))
    if not username or role not in ROLES:
        return None
    fresh = get_member(username)
    if fresh is None:
        return None
    # Always trust the DB role; ignore stale role baked into cookie.
    return fresh


# ---------------------------------------------------------------------------
# input validators
# ---------------------------------------------------------------------------


def is_valid_username(value: str) -> bool:
    import re

    return bool(re.match(USERNAME_RE, value))


def is_valid_password(value: str) -> bool:
    return MIN_PASSWORD_LEN <= len(value) <= MAX_PASSWORD_LEN
