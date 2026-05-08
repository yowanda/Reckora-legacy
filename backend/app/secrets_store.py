"""Phase 10g: runtime secret store backed by a ``secrets.json`` file on disk.

Some Phase 10+ capabilities (the AI investigation summary in 10g, future
provider integrations) require an API key that the operator can rotate
*without* a redeploy. We deliberately do **not** keep the key alongside
the SQLite ``admin_settings`` table: a leaked DB dump would otherwise
expose the model API key, which is high-value (an LLM API key with no
spend cap can ring up thousands of dollars in usage). The ``Settings``
env-var path is also a poor fit because we need the SPA to be able to
update the value at runtime.

The store is intentionally small:

  * One JSON object on disk at ``<data>/secrets.json`` (``<data>`` is the
    directory holding the SQLite DB), permissioned to ``0o600`` after
    every write.
  * A short-lived in-process cache keyed by mtime so repeated reads on
    the hot path don't hit the filesystem.
  * An allowlist of permitted keys. The store ignores requests for any
    key not on the list so a typo-d PUT can't leave a stale "secret"
    sitting on disk.

Threat model: anyone with read access to the data directory already has
read access to the SQLite DB containing member password hashes, so this
module does not provide additional confidentiality at rest. What it does
provide is a clear separation between *configuration* (knobs, env vars,
admin DB) and *credentials* (API keys), so an operator can audit the
narrow surface area where credentials are persisted.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Final

from .config import settings

# Allowlist of secret keys that the store will accept. Adding a new key
# requires a code change here so an attacker who finds a stray PUT
# endpoint can't smuggle arbitrary values onto disk.
ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Phase 10g — AI investigation summary provider API key. Read by
        # ``app.ai_summary.summarize_scan`` and surfaced to the SPA via
        # ``GET /api/admin/secrets/status`` (presence-only; the value is
        # never echoed back).
        "ai_summary_api_key",
    }
)


def _path() -> Path:
    """Filesystem location of ``secrets.json``.

    Lives next to the SQLite DB so a single bind-mount on ``/data``
    captures both. Created lazily on the first write.
    """
    return Path(settings.db_path).parent / "secrets.json"


_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_mtime: float = -1.0


def _load_unlocked() -> None:
    """Refresh ``_cache`` from disk if the file has been modified.

    Caller must hold ``_lock``.
    """
    global _cache_mtime
    path = _path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        if _cache_mtime != 0.0:
            _cache.clear()
            _cache_mtime = 0.0
        return
    if stat.st_mtime == _cache_mtime:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # Treat unreadable as empty — fail-open to "no secret configured"
        # so a permissions glitch doesn't surface as a 500 in the API.
        _cache.clear()
        _cache_mtime = stat.st_mtime
        return
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        # Corrupt file is the operator's problem; keep the cache empty
        # so ``get_secret`` returns ``None`` until they fix it.
        _cache.clear()
        _cache_mtime = stat.st_mtime
        return
    _cache.clear()
    if isinstance(decoded, dict):
        for key, value in decoded.items():
            if key in ALLOWED_KEYS and isinstance(value, str) and value:
                _cache[key] = value
    _cache_mtime = stat.st_mtime


def _save_unlocked() -> None:
    """Persist ``_cache`` to disk atomically.

    Caller must hold ``_lock``.
    """
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(_cache, ensure_ascii=False, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    # Best-effort chmod — the at-rest threat model already assumes
    # filesystem-level access, so a chmod failure (e.g. exotic FS)
    # isn't worth aborting the write for.
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def get_secret(key: str) -> str | None:
    """Return the secret for ``key`` or ``None`` if unset.

    Unknown keys (not in :data:`ALLOWED_KEYS`) always return ``None`` —
    the store doesn't disambiguate "unknown" from "unset" so a misspelled
    key looks the same as a missing one to the caller.
    """
    if key not in ALLOWED_KEYS:
        return None
    with _lock:
        _load_unlocked()
        return _cache.get(key)


def has_secret(key: str) -> bool:
    """Cheap presence check used by the admin "status" endpoint.

    Never returns the value, so an admin can see *whether* a key is
    configured without the response leaking the value to the audit log.
    """
    return get_secret(key) is not None


def set_secret(key: str, value: str) -> None:
    """Persist ``value`` for ``key``.

    Raises :class:`KeyError` for keys not on the allowlist and
    :class:`ValueError` for empty values. The caller is expected to have
    already validated the value (length, character set, etc.) — this
    layer just persists.
    """
    if key not in ALLOWED_KEYS:
        raise KeyError(f"unknown secret key: {key}")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key}: value must be a non-empty string")
    with _lock:
        _load_unlocked()
        _cache[key] = value
        _save_unlocked()


def delete_secret(key: str) -> bool:
    """Drop ``key`` from the store. Returns ``True`` iff a value was removed."""
    if key not in ALLOWED_KEYS:
        return False
    with _lock:
        _load_unlocked()
        if key not in _cache:
            return False
        del _cache[key]
        _save_unlocked()
        return True


def list_status() -> dict[str, bool]:
    """Per-key ``{key: configured}`` map — values are never returned."""
    with _lock:
        _load_unlocked()
        return {key: key in _cache for key in sorted(ALLOWED_KEYS)}


def _reset_cache_for_tests() -> None:
    """Drop the in-process cache so tests with monkeypatched paths see disk."""
    global _cache_mtime
    with _lock:
        _cache.clear()
        _cache_mtime = -1.0
