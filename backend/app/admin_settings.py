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
    KnobSpec(
        key="enable_reverse_image",
        settings_attr="enable_reverse_image",
        minimum=0,
        maximum=1,
        description=(
            "Phase 7c: when 1, render a row of reverse-image "
            "search chips (Yandex / Google Lens / TinEye) under "
            "every CLAIMED hit whose extractor pass produced an "
            "``avatar_url``. Each chip deep-links into the "
            "upstream's public 'search-by-URL' landing page in a "
            "new tab — no API key, no upstream HTTP call from the "
            "backend; the operator's browser hits the upstream "
            "directly. Default on because there's no quota cost."
        ),
    ),
    KnobSpec(
        key="ml_drift_ks_threshold_pct",
        settings_attr="ml_drift_ks_threshold_pct",
        minimum=1,
        maximum=100,
        description=(
            "Phase 7d: threshold (in percent, 1-100) on the "
            "two-sample Kolmogorov-Smirnov statistic above which a "
            "feature is flagged as drifted in the admin ML "
            "panel. The runtime divides by 100 before applying "
            "the gate. 10 (== 0.10) is reasonable for the "
            "default synthetic training set; noisier production "
            "streams should crank the threshold up to avoid "
            "false alarms."
        ),
    ),
    KnobSpec(
        key="ml_drift_window_size",
        settings_attr="ml_drift_window_size",
        minimum=64,
        maximum=65536,
        description=(
            "Phase 7d: maximum number of feature vectors kept in "
            "the in-memory ring buffer the drift detector samples "
            "from. Older observations are evicted when this cap "
            "is exceeded. 2048 covers ~10 scans of a 200-site "
            "catalog without leaking memory."
        ),
    ),
    KnobSpec(
        key="enable_proxy_rotator",
        settings_attr="enable_proxy_rotator",
        minimum=0,
        maximum=1,
        description=(
            "Phase 8b: when 1, every outbound request is routed "
            "via one of the URLs in ``RECKORA_PROXY_POOL_URLS`` "
            "(comma-separated env). Round-robin selection with a "
            "per-URL health cooldown skips any proxy that has "
            "recently raised a transport error. When 0, the pool "
            "is dormant and the runtime falls back to the Phase 5e "
            "single-proxy gate (``proxy_enabled``). Setting this "
            "to 1 with no pool URLs configured is a no-op."
        ),
    ),
    KnobSpec(
        key="enable_host_rate_limit",
        settings_attr="enable_host_rate_limit",
        minimum=0,
        maximum=1,
        description=(
            "Phase 8b: when 1, every outbound request waits for a "
            "token from a per-host bucket before dispatch. The "
            "bucket refill rate comes from "
            "``host_rate_per_min``. Use this to keep large scans "
            "below per-host public rate limits even on a single "
            "egress IP."
        ),
    ),
    KnobSpec(
        key="host_rate_per_min",
        settings_attr="host_rate_per_min",
        minimum=1,
        maximum=600,
        description=(
            "Phase 8b: steady-state requests / minute / host the "
            "outbound token bucket grants when "
            "``enable_host_rate_limit`` is 1. The bucket capacity "
            "matches this value so a 1-minute idle period absorbs "
            "the equivalent burst. 60 (== 1 rps / host) is a safe "
            "default below the documented limits of all the major "
            "platforms in the catalog."
        ),
    ),
    KnobSpec(
        key="proxy_health_cooldown_sec",
        settings_attr="proxy_health_cooldown_sec",
        minimum=1,
        maximum=600,
        description=(
            "Phase 8b: how long (seconds) the proxy rotator skips "
            "a URL after a transport error against it. The pool "
            "is fail-open — when every URL is in cooldown it "
            "returns ``None`` and the request goes direct, since "
            "refusing every request is worse than briefly leaking "
            "the API process's IP."
        ),
    ),
    KnobSpec(
        key="enable_graphql_gateway",
        settings_attr="enable_graphql_gateway",
        minimum=0,
        maximum=1,
        description=(
            "Phase 8c: when 1, ``/api/graphql`` exposes a "
            "Strawberry-served schema that mirrors the REST surface "
            "(health, sites, current member, scan history, search "
            "mutation, permute strategies, admin settings reads). "
            "When 0, the route returns 503 so casual operators "
            "don't see a second transport unless they ask for it. "
            "Auth piggybacks on the same session cookie / Bearer "
            "token used by REST."
        ),
    ),
    KnobSpec(
        key="enable_share_links",
        settings_attr="enable_share_links",
        minimum=0,
        maximum=1,
        description=(
            "Phase 8d: when 1, scan owners can mint anonymous public "
            "share links via ``POST /api/history/{id}/share`` and "
            "the unauthenticated ``GET /share/{token}`` returns the "
            "scan summary + results with CDN-friendly cache headers. "
            "When 0, both endpoints return 503 so a deployment that "
            "hasn't reviewed the privacy implications doesn't "
            "accidentally expose member-scoped scan data."
        ),
    ),
    KnobSpec(
        key="share_link_default_ttl_sec",
        settings_attr="share_link_default_ttl_sec",
        minimum=0,
        maximum=31_536_000,  # 1 year cap
        description=(
            "Phase 8d: default TTL applied to a share link when the "
            "owner doesn't pass ``ttl_sec`` on the mint request. "
            "0 = never expire until explicitly revoked. Anything > "
            "0 also caps the owner-specified TTL so admin policy "
            "wins over a misbehaving SPA."
        ),
    ),
    KnobSpec(
        key="share_link_cache_max_age_sec",
        settings_attr="share_link_cache_max_age_sec",
        minimum=1,
        maximum=86_400,
        description=(
            "Phase 8d: edge ``s-maxage`` (seconds) on the public "
            "``/share/{token}`` response. 60s lets a revoke "
            "propagate within a minute on Cloudflare-class CDNs but "
            "still soaks up viral traffic. Bump higher to maximise "
            "edge-hit rate when revoke latency is acceptable."
        ),
    ),
    KnobSpec(
        key="share_link_max_per_scan",
        settings_attr="share_link_max_per_scan",
        minimum=1,
        maximum=50,
        description=(
            "Phase 8d: maximum number of active (non-expired) share "
            "links per scan. Mint requests past the cap return 409 "
            "so a scan can't accumulate dozens of dangling tokens; "
            "owners can revoke an existing one to free a slot."
        ),
    ),
    KnobSpec(
        key="enable_anti_bot_pipeline",
        settings_attr="enable_anti_bot_pipeline",
        minimum=0,
        maximum=1,
        description=(
            "Phase 9a: when 1, every outbound site probe is "
            "dispatched via the curl-cffi-backed anti-bot transport "
            "so the TLS handshake bytes (cipher suite ordering + "
            "extension list = JA3/JA4) match a real Chrome / Safari "
            "/ Firefox client. Stacks underneath the Phase 8b "
            "rotating proxy transport so a deployment with both "
            "knobs on gets browser-shaped fingerprints across every "
            "egress IP in the pool. The default impersonation "
            "profile is set via ``RECKORA_ANTI_BOT_DEFAULT_IMPERSONATION``; "
            "per-site overrides via ``site_overrides.antiBotProfile``."
        ),
    ),
    KnobSpec(
        key="anti_bot_playwright_fallback",
        settings_attr="anti_bot_playwright_fallback",
        minimum=0,
        maximum=1,
        description=(
            "Phase 9a: when 1, sites that still return a JS-challenge "
            "HTML after the curl-cffi impersonation are re-fetched "
            "through a headless chromium so the challenge JavaScript "
            "actually runs. Off by default because chromium has a "
            "~200 MB image footprint and a warm-up cost; only flip "
            "on when running a custom Docker image with playwright "
            "pre-installed. Bounded by "
            "``anti_bot_playwright_max_per_scan`` so a 2,500-site "
            "scan can't fan out 2,500 chromium processes."
        ),
    ),
    KnobSpec(
        key="anti_bot_playwright_max_per_scan",
        settings_attr="anti_bot_playwright_max_per_scan",
        minimum=0,
        maximum=200,
        description=(
            "Phase 9a: hard ceiling on chromium spawns per scan when "
            "``anti_bot_playwright_fallback`` is on. ``0`` disables "
            "the fallback entirely without needing to flip the "
            "boolean knob. 5 is the tuned default — enough to "
            "rescue a few high-value Cloudflare-fronted sites per "
            "scan without paying the chromium cost on every probe."
        ),
    ),
    KnobSpec(
        key="anti_bot_playwright_timeout_sec",
        settings_attr="anti_bot_playwright_timeout_sec",
        minimum=1,
        maximum=60,
        description=(
            "Phase 9a: per-fetch budget (seconds) for the Playwright "
            "fallback. Includes the chromium navigation + the "
            "post-challenge wait for ``networkidle``. 10s is enough "
            "for the typical Cloudflare ``Just a moment...`` "
            "interstitial to settle while keeping the worst-case "
            "scan tail-latency bounded."
        ),
    ),
    KnobSpec(
        key="host_rate_limit_use_redis",
        settings_attr="host_rate_limit_use_redis",
        minimum=0,
        maximum=1,
        description=(
            "Phase 9b: when 1, the per-host token bucket is replaced "
            "by a Redis-backed sliding-window counter so a Celery "
            "fan-out (Phase 8a) keeps a single bucket per host across "
            "all workers. The connection string comes from "
            "``RECKORA_HOST_RATE_LIMIT_REDIS_URL``; if blank the "
            "Phase 8a ``RECKORA_CELERY_BROKER_URL`` is reused. A Redis "
            "transport error falls *open* to the in-memory bucket so "
            "an outage stays fail-open rather than fail-closed. Off "
            "by default — the in-memory bucket is the right choice "
            "for HF Space's single-container deployment."
        ),
    ),
    KnobSpec(
        key="enable_geo_aware_proxy",
        settings_attr="enable_geo_aware_proxy",
        minimum=0,
        maximum=1,
        description=(
            "Phase 9c: when 1, the Phase 8b proxy pool selects an "
            "egress URL whose region tag matches the target site's "
            "``preferredRegion`` site override (falling back to the "
            "``default`` region, then to any healthy URL). Pool URLs "
            "are tagged via the ``region:scheme://host:port`` syntax in "
            "``RECKORA_PROXY_POOL_URLS`` (bare URLs continue to work "
            'and are tagged ``region="default"``). Off by default — '
            "the round-robin behaviour from 8b is preserved unless an "
            "operator explicitly opts in. Useful for catalogs with "
            "geo-locked sites (Yandex, VK, Naver, Sina Weibo, …) where "
            "an EU exit IP gets blocked but a regional proxy gets "
            "through."
        ),
    ),
    KnobSpec(
        key="enable_captcha_classifier",
        settings_attr="enable_captcha_classifier",
        minimum=0,
        maximum=1,
        description=(
            "Phase 9d: when 1, the per-site checker consults the "
            "vendor-specific CAPTCHA classifier (``app.captcha_signatures``) "
            "against every probe body that could fall into the BLOCKED "
            'branch and surfaces a structured ``reason="captcha_<vendor>"`` '
            "string on the result so the SPA can render a vendor-aware "
            "badge (reCAPTCHA v2 / v3, hCaptcha, Cloudflare Turnstile, "
            "Datadome, AWS WAF Captcha, FunCaptcha, Geetest, PerimeterX, "
            'generic ``<form action="/captcha">``). Per-vendor counts '
            "are exposed via ``GET /api/admin/ml/status.captcha_vendors_seen`` "
            "so an operator can see which CAPTCHA fleets are pushing "
            "the most BLOCKED rows this week. Off by default — when "
            "off, the classifier is never imported on the hot path "
            "and ``CheckResult.reason`` stays ``None``."
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
