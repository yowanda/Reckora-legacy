"""Phase 5d: admin-managed runtime overrides on graph + extractor knobs.

The overrides live in the ``admin_settings`` SQLite table (see
``auth.init_db``). Each row maps a whitelisted ``key`` to an integer
``value`` that takes precedence over the ``Settings`` field of the same
name from ``config.py``. Members can read the effective values via
``GET /api/settings``; admins can list, set, and reset overrides via
``GET/PATCH /api/admin/settings``.

Why a tiny bespoke table instead of a generic key/value store?

  * Every overrideable knob is an integer in a known range, so we get
    away with a four-column schema and no JSON parsing on the hot path.
  * The allowlist is enforced at the storage layer too (``set_override``
    rejects unknown keys), so a SQL injection or stray API call can't
    smuggle a value into a runtime field that doesn't validate it.
  * Reads on the per-site classifier hot path hit ``effective_int`` many
    times per scan, so the table contents are cached with a short TTL
    and invalidated eagerly on mutation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .auth import db_connection
from .config import settings

# ---------------------------------------------------------------------------
# Knob registry — defined first so cache helpers can reference ALLOWED_KEYS.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnobSpec:
    """Range + description for an overrideable integer setting.

    ``minimum`` / ``maximum`` are inclusive. ``description`` is shown to
    admins in the UI; ``settings_attr`` is the matching field on
    ``config.Settings`` (always identical to ``key`` today, but kept
    explicit so future renames stay safe).
    """

    key: str
    settings_attr: str
    minimum: int
    maximum: int
    description: str


# Keep this list focused on knobs that genuinely benefit from runtime
# tuning by an operator (graph fanout + extractor confidence floor).
# Adding more knobs is a one-liner here — but resist the urge to expose
# every Settings field, since the admin UI loses signal as the surface
# grows.
KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec(
        key="graph_max_depth",
        settings_attr="graph_max_depth",
        minimum=0,
        maximum=1,
        description=(
            "Maximum recursion depth for /api/search/graph. "
            "0 collapses to a regular search (graph view shows just the "
            "primary scan); 1 enables one level of secondary scans on "
            "extracted identifiers."
        ),
    ),
    KnobSpec(
        key="graph_max_secondary",
        settings_attr="graph_max_secondary",
        minimum=1,
        maximum=20,
        description=(
            "Cap on extracted secondary usernames per primary CLAIMED "
            "hit. Higher values widen the graph at the cost of extra "
            "site probes."
        ),
    ),
    KnobSpec(
        key="graph_max_seed_hits",
        settings_attr="graph_max_seed_hits",
        minimum=1,
        maximum=20,
        description=(
            "Cap on the number of primary CLAIMED hits whose extracted "
            "identifiers are eligible to seed sub-scans."
        ),
    ),
    KnobSpec(
        key="extract_min_confidence",
        settings_attr="extract_min_confidence",
        minimum=0,
        maximum=100,
        description=(
            "Minimum CLAIMED confidence (0-100) below which the "
            "socid-extractor pass is skipped. Lower values surface more "
            "leads but also more noise."
        ),
    ),
    KnobSpec(
        key="proxy_enabled",
        settings_attr="proxy_enabled_default",
        minimum=0,
        maximum=1,
        description=(
            "Phase 5e: when 1, all outbound site probes are routed "
            "through ``RECKORA_PROXY_URL`` (Tor SOCKS5 / HTTP CONNECT / "
            "etc.). When 0, the proxy is bypassed even if a URL is "
            "configured. Setting this to 1 with no URL configured is a "
            "no-op — the runtime falls back to direct egress."
        ),
    ),
    KnobSpec(
        key="min_claimed_confidence",
        settings_attr="min_claimed_confidence",
        minimum=0,
        maximum=100,
        description=(
            "Phase 5h: smallest rule-based confidence (0-100) a CLAIMED "
            "result must reach to stay CLAIMED. Anything below is "
            "demoted to UNKNOWN with a ``claimed-below-confidence-floor`` "
            "signal. 0 disables the floor; 60 is the tuned default."
        ),
    ),
    KnobSpec(
        key="min_claimed_ml_confidence",
        settings_attr="min_claimed_ml_confidence",
        minimum=0,
        maximum=100,
        description=(
            "Phase 5h: smallest ONNX-reranker confidence (0-100) a "
            "CLAIMED result must reach to stay CLAIMED. Only applied "
            "when the ML reranker actually produced a score; CLAIMED "
            "rows without a score (non-username scans, ML disabled) "
            "are unaffected. 0 disables this floor."
        ),
    ),
    KnobSpec(
        key="require_verify_for_claim",
        settings_attr="require_verify_for_claim",
        minimum=0,
        maximum=1,
        description=(
            "Phase 5h: when 1, the Phase-2 verify pass must have "
            "explicitly agreed for a hit to stay CLAIMED. Hits where "
            "verify was skipped (HEAD-only sites) or disagreed are "
            "demoted to UNKNOWN. Use this for zero-FP operator runs."
        ),
    ),
    KnobSpec(
        key="enable_link_liveness",
        settings_attr="enable_link_liveness",
        minimum=0,
        maximum=1,
        description=(
            "Phase 5h Layer D: when 1, every CLAIMED hit gets one "
            "extra HEAD/GET on the human-facing ``url`` (post-"
            "substitution). Hits that 404 / 410, redirect to the site "
            "homepage, redirect to a login wall, or look like a soft-"
            "404 are demoted to UNKNOWN. Disabling skips the extra "
            "probe — useful when running behind a heavily rate-"
            "limited egress."
        ),
    ),
    KnobSpec(
        key="enable_image_phash",
        settings_attr="enable_image_phash",
        minimum=0,
        maximum=1,
        description=(
            "Phase 7a: when 1, /api/search/graph fetches the avatar "
            "URL on every CLAIMED hit (when the extractor pass found "
            "one) and computes a 64-bit perceptual hash. Pairs of "
            "hits whose hashes differ by at most "
            "``image_phash_max_distance`` bits get a ``phash_edge`` "
            "stitched between them — strong evidence the same person "
            "owns both accounts. Default off because the extra "
            "HTTP-per-claim is not free."
        ),
    ),
    KnobSpec(
        key="image_phash_max_distance",
        settings_attr="image_phash_max_distance",
        minimum=0,
        maximum=32,
        description=(
            "Phase 7a: maximum Hamming distance (0-32) on the 64-bit "
            "dHash for two avatars to be considered the same person. "
            "0 means byte-identical reuploads only; 6 (the default) "
            "tolerates mild re-encoding / resizing; values past ~12 "
            "start surfacing same-shape-different-person false "
            "positives. Only consulted when ``enable_image_phash`` "
            "is on."
        ),
    ),
    KnobSpec(
        key="enable_email_enrichment",
        settings_attr="enable_email_enrichment",
        minimum=0,
        maximum=1,
        description=(
            "Phase 7b: when 1, ``POST /api/enrich/email`` runs an "
            "RDAP lookup on the email's domain and an HIBP breach "
            "lookup on the address (if a key is configured). The "
            "SPA surfaces the result as a side panel after every "
            "email-mode scan. Default off — both upstreams have "
            "their own quota and casual list-mode operators don't "
            "need the extra panel. The HIBP API key itself lives "
            "in env (``RECKORA_ENRICHMENT_HIBP_API_KEY``) and is "
            "**never** returned by the public settings route."
        ),
    ),
    KnobSpec(
        key="enrichment_rate_per_min",
        settings_attr="enrichment_rate_per_min",
        minimum=1,
        maximum=60,
        description=(
            "Phase 7b: per-actor sliding-window rate limit "
            "(requests / minute) on ``/api/enrich/email``. The "
            "default of 5 lets an operator batch a handful of "
            "emails in a row without tripping HIBP's own 1-rps "
            "soft limit. Lower values further protect upstream "
            "quotas; values past ~20 start risking HIBP / RDAP "
            "rate-limiting the whole instance."
        ),
    ),
)

KNOBS_BY_KEY: dict[str, KnobSpec] = {k.key: k for k in KNOBS}
ALLOWED_KEYS: frozenset[str] = frozenset(KNOBS_BY_KEY)


class UnknownKnobError(KeyError):
    """Raised when an admin payload references a non-allowlisted key."""


class KnobOutOfRangeError(ValueError):
    """Raised when an admin payload sets a knob outside its valid range."""


# Aliases preserved for callers/tests that imported the old names.
UnknownKnob = UnknownKnobError
KnobOutOfRange = KnobOutOfRangeError


def _spec(key: str) -> KnobSpec:
    spec = KNOBS_BY_KEY.get(key)
    if spec is None:
        raise UnknownKnobError(key)
    return spec


def _default(spec: KnobSpec) -> int:
    return int(getattr(settings, spec.settings_attr))


# ---------------------------------------------------------------------------
# In-process cache (TTL + eager invalidate on mutation).
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache: dict[str, int] = {}
_cache_loaded_at: float = 0.0


def _query_all_overrides() -> dict[str, int]:
    placeholders = ",".join("?" * len(ALLOWED_KEYS))
    with db_connection() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM admin_settings WHERE key IN ({placeholders})",
            tuple(ALLOWED_KEYS),
        ).fetchall()
    return {row["key"]: int(row["value"]) for row in rows}


def _ensure_cache_fresh() -> None:
    global _cache_loaded_at
    now = time.monotonic()
    if now - _cache_loaded_at < _CACHE_TTL_SECONDS:
        return
    with _cache_lock:
        if now - _cache_loaded_at < _CACHE_TTL_SECONDS:
            return
        rows = _query_all_overrides()
        _cache.clear()
        _cache.update(rows)
        _cache_loaded_at = time.monotonic()


def _invalidate_cache() -> None:
    global _cache_loaded_at
    _cache_loaded_at = 0.0


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def get_overrides() -> dict[str, int]:
    """Return a snapshot of all currently-set admin overrides."""
    _ensure_cache_fresh()
    return dict(_cache)


def effective_int(key: str) -> int:
    """Return the override (if any) for ``key``, else the env-var default.

    Falls back to the ``Settings`` value untouched if the key is not
    overrideable; raising here would force callers in the hot path to
    catch ``UnknownKnob``, which is friction we don't want for what is
    essentially a config read.
    """
    spec = KNOBS_BY_KEY.get(key)
    if spec is None:
        return int(getattr(settings, key))
    _ensure_cache_fresh()
    if spec.key in _cache:
        return _cache[spec.key]
    return _default(spec)


def set_override(key: str, value: int, *, actor: str) -> int:
    """Upsert a knob override and return the stored value.

    Raises ``UnknownKnob`` if ``key`` is not in the allowlist or
    ``KnobOutOfRange`` if ``value`` is outside the spec's range.
    """
    spec = _spec(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise KnobOutOfRangeError(f"{key}: value must be an integer")
    if value < spec.minimum or value > spec.maximum:
        raise KnobOutOfRangeError(
            f"{key}: {value} is outside allowed range [{spec.minimum}, {spec.maximum}]"
        )
    now = int(time.time())
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO admin_settings (key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value = excluded.value, "
            "  updated_by = excluded.updated_by, "
            "  updated_at = excluded.updated_at",
            (key, value, actor, now),
        )
    _invalidate_cache()
    return value


def delete_override(key: str) -> bool:
    """Drop an override row. Returns True iff a row was deleted.

    Unknown keys are treated as "not present" rather than raising — the
    intent ("this knob should be the default") is satisfied either way.
    """
    if key not in ALLOWED_KEYS:
        return False
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM admin_settings WHERE key = ?", (key,))
    _invalidate_cache()
    return cur.rowcount > 0


def describe() -> list[dict[str, Any]]:
    """Per-knob descriptor with default/override/effective + range."""
    overrides = get_overrides()
    out: list[dict[str, Any]] = []
    for spec in KNOBS:
        default = _default(spec)
        override = overrides.get(spec.key)
        out.append(
            {
                "key": spec.key,
                "description": spec.description,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "default": default,
                "override": override,
                "effective": override if override is not None else default,
            }
        )
    return out


def effective_map() -> dict[str, int]:
    """Public-safe view: just ``{key: effective_int}`` for every knob."""
    overrides = get_overrides()
    return {spec.key: overrides.get(spec.key, _default(spec)) for spec in KNOBS}
