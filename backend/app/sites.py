"""Site definitions loader.

Phase 4: the runtime catalog is now the merge of the bundled
``data/sites.json`` (immutable, derived from maigret at build time)
and the SQLite-backed ``site_overrides`` table populated by admins
through the live DB editor.

Override semantics:
- ``operation = upsert`` carries a JSON ``patch`` that's merged on top
  of the base entry; if the site name doesn't exist in the base, the
  patch is the full definition.
- ``operation = delete`` removes the site from the runtime catalog
  (without touching the bundled JSON).
- ``operation = disable`` keeps the site visible but flags it as
  disabled (we drop it from probe sets but list it in /api/admin/sites).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import settings
from .identity import (
    ALL_IDENTITY_TYPES,
    DEFAULT_IDENTITY_TYPE,
    KNOWN_IDENTITY_OVERRIDES,
    IdentityType,
)

CheckType = Literal["status_code", "message", "response_url"]


@dataclass(frozen=True, slots=True)
class Site:
    name: str
    url: str
    url_main: str
    check_type: CheckType
    regex_check: str | None = None
    absence_strs: tuple[str, ...] = ()
    presense_strs: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    alexa_rank: int | None = None
    tags: tuple[str, ...] = ()
    error_strs: tuple[str, ...] = ()
    # Phase 1: HEAD-only probe — for 'status_code' sites that only need the
    # response code. Saves ~30% bandwidth on the cheap-classify subset.
    head_only: bool = False
    # Phase 1: ignore 403 — engines like XenForo emit 403 for valid users
    # behind anti-scrape; treat 403 as inconclusive instead of FREE.
    ignore_403: bool = False
    # Phase 1: engine name (origin engine after build-time merge). Kept
    # purely for telemetry / debugging — runtime classification doesn't
    # branch on it.
    engine: str | None = None
    # Phase 2: pre-warm cookies before probing. Sites flagged ``prewarm``
    # get a GET to ``url_main`` first so any session/anti-bot cookies are
    # in the shared cookie jar before the username probe goes out. Set
    # automatically by ``build_sites.py`` for engines known to need it
    # (XenForo, vBulletin, uCoz, plus the high-WAF subset).
    prewarm: bool = False
    # Phase 4: admin-managed disable flag. ``True`` = the runtime hides
    # the site from probes; the admin DB editor still lists it so it can
    # be re-enabled. Distinct from "delete" which drops the site
    # entirely from the merged catalog.
    disabled: bool = False
    # Phase 4: provenance — ``base`` = bundled sites.json, ``override``
    # = upsert patch, ``custom`` = added entirely via admin editor.
    source: Literal["base", "override", "custom"] = "base"
    # Phase 5a: which identity types this site accepts. Defaults to
    # ``("username",)`` for backwards compat with the username-only
    # Phase 1-4 catalog. Annotations come from the curated
    # ``identity.KNOWN_IDENTITY_OVERRIDES`` map (applied at load time
    # when the raw entry doesn't carry an explicit ``identityTypes``)
    # or from per-site override patches via the admin DB editor.
    identity_types: tuple[IdentityType, ...] = (DEFAULT_IDENTITY_TYPE,)
    # Phase 5h Layer E: alternate probe URL. Some sites have a separate
    # API endpoint for checking username existence (``urlProbe``) while
    # the human-facing display URL (``url``) is different. When set and
    # != ``url``, Layer E probes the display URL independently after the
    # main liveness check passes on the probe URL.
    url_probe: str | None = None


@dataclass(frozen=True, slots=True)
class SiteCatalog:
    sites: tuple[Site, ...] = field(default_factory=tuple)

    def by_name(self, name: str) -> Site | None:
        for s in self.sites:
            if s.name == name:
                return s
        return None

    def filtered(
        self,
        tags: Iterable[str] | None = None,
        limit: int | None = None,
        identity_type: IdentityType = DEFAULT_IDENTITY_TYPE,
    ) -> list[Site]:
        wanted = {t.lower() for t in tags or ()}
        out: list[Site] = []
        for s in self.sites:
            if s.disabled:
                continue
            if identity_type not in s.identity_types:
                continue
            if wanted and not ({t.lower() for t in s.tags} & wanted):
                continue
            out.append(s)
            if limit and len(out) >= limit:
                break
        return out

    def all_tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for s in self.sites:
            if s.disabled:
                continue
            for t in s.tags:
                counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _coerce_site(name: str, raw: dict, *, source: str = "base") -> Site | None:
    url = raw.get("url")
    if not url or "{username}" not in url:
        return None
    headers_raw = raw.get("headers")
    if isinstance(headers_raw, dict):
        headers = tuple(headers_raw.items())
    elif isinstance(headers_raw, list):
        # Some engine merges produce a list of (k, v) pairs; tolerate that.
        headers = tuple(
            (str(h[0]), str(h[1]))
            for h in headers_raw
            if isinstance(h, list | tuple) and len(h) == 2
        )
    else:
        headers = ()
    errors_raw = raw.get("errors")
    if isinstance(errors_raw, dict):
        # Maigret stores errors as {"keyword": "human-readable"} — we only
        # care about the keyword for substring matching.
        error_strs = tuple(errors_raw.keys())
    elif isinstance(errors_raw, list):
        error_strs = tuple(str(e) for e in errors_raw)
    else:
        error_strs = ()
    raw_identity = raw.get("identityTypes")
    if isinstance(raw_identity, list) and raw_identity:
        # Filter out unknown values defensively so a typo in an admin
        # patch doesn't crash the loader; fall back to the default if
        # nothing recognised survives.
        identity_types: tuple[IdentityType, ...] = tuple(
            t for t in raw_identity if t in ALL_IDENTITY_TYPES
        )
        if not identity_types:
            identity_types = (DEFAULT_IDENTITY_TYPE,)
    else:
        # Curated overrides apply only when the raw entry doesn't
        # already carry an ``identityTypes`` field — admin patches win.
        identity_types = KNOWN_IDENTITY_OVERRIDES.get(name, (DEFAULT_IDENTITY_TYPE,))

    return Site(
        name=name,
        url=url,
        url_main=raw.get("urlMain") or url,
        check_type=raw.get("checkType", "status_code"),
        regex_check=raw.get("regexCheck"),
        absence_strs=tuple(raw.get("absenceStrs") or ()),
        presense_strs=tuple(raw.get("presenseStrs") or ()),
        headers=headers,
        alexa_rank=raw.get("alexaRank"),
        tags=tuple(raw.get("tags") or ()),
        error_strs=error_strs,
        head_only=bool(raw.get("requestHeadOnly")),
        ignore_403=bool(raw.get("ignore403")),
        engine=raw.get("engine"),
        prewarm=bool(raw.get("prewarm")),
        disabled=bool(raw.get("disabled")),
        source=source,  # type: ignore[arg-type]
        identity_types=identity_types,
        url_probe=raw.get("urlProbe"),
    )


def site_to_raw(site: Site) -> dict:
    """Inverse of ``_coerce_site`` — for serialising a Site back to the
    JSON shape used in sites.json + the overrides table."""
    raw: dict = {
        "url": site.url,
        "urlMain": site.url_main,
        "checkType": site.check_type,
    }
    if site.regex_check:
        raw["regexCheck"] = site.regex_check
    if site.absence_strs:
        raw["absenceStrs"] = list(site.absence_strs)
    if site.presense_strs:
        raw["presenseStrs"] = list(site.presense_strs)
    if site.headers:
        raw["headers"] = dict(site.headers)
    if site.alexa_rank is not None:
        raw["alexaRank"] = site.alexa_rank
    if site.tags:
        raw["tags"] = list(site.tags)
    if site.error_strs:
        raw["errors"] = dict.fromkeys(site.error_strs, "")
    if site.head_only:
        raw["requestHeadOnly"] = True
    if site.ignore_403:
        raw["ignore403"] = True
    if site.engine:
        raw["engine"] = site.engine
    if site.prewarm:
        raw["prewarm"] = True
    if site.disabled:
        raw["disabled"] = True
    if site.identity_types and site.identity_types != (DEFAULT_IDENTITY_TYPE,):
        raw["identityTypes"] = list(site.identity_types)
    if site.url_probe:
        raw["urlProbe"] = site.url_probe
    return raw


def _merge_raw(base: dict, patch: dict) -> dict:
    """Shallow-merge ``patch`` over ``base`` for site definitions.

    Keys present in ``patch`` replace the corresponding base keys; this
    is intentionally NOT deep-merged so admins can fully replace e.g.
    ``presenseStrs`` rather than only append to it.
    """
    merged = dict(base)
    for k, v in patch.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------

# In-memory cache of the merged catalog. Cleared by ``reload_catalog`` on
# every admin override mutation so the next request sees the new sites.
_CACHED_CATALOG: SiteCatalog | None = None
_CACHED_BASE_PATH: Path | None = None


def _load_base(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_sites = payload.get("sites") or payload
    return dict(raw_sites)


def _apply_overrides(base: dict[str, dict]) -> dict[str, tuple[str, dict]]:
    """Return ``{site_name: (source, merged_raw_dict)}`` after applying
    overrides from the SQLite ``site_overrides`` table on top of the
    bundled base.

    ``source`` is one of ``base`` (no override), ``override`` (base entry
    patched), or ``custom`` (admin-added entry that doesn't exist in the
    bundled JSON).
    """
    if not settings.enable_site_overrides:
        return {name: ("base", raw) for name, raw in base.items()}

    # Lazy import to avoid circular auth/sites dependency during startup.
    from .auth import db_connection

    out: dict[str, tuple[str, dict]] = {name: ("base", raw) for name, raw in base.items()}
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT name, operation, patch_json FROM site_overrides ORDER BY updated_at"
            ).fetchall()
    except Exception:
        # Table missing during early startup or test bootstrap — no-op.
        return out

    for row in rows:
        name = row["name"]
        op = row["operation"]
        patch_json = row["patch_json"] or "{}"
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError:
            continue
        if op == "delete":
            out.pop(name, None)
            continue
        if op == "disable":
            base_raw = out.get(name, ("base", {}))[1]
            out[name] = ("override", {**base_raw, "disabled": True})
            continue
        # upsert (or any unknown op falls back to upsert)
        if name in out:
            base_raw = out[name][1]
            out[name] = ("override", _merge_raw(base_raw, patch))
        else:
            # Brand-new site added entirely via the editor.
            out[name] = ("custom", patch)
    return out


def load_catalog(path: Path | None = None) -> SiteCatalog:
    """Load the merged site catalog (bundled JSON + admin overrides)."""
    global _CACHED_CATALOG, _CACHED_BASE_PATH
    p = Path(path) if path else settings.sites_path
    if _CACHED_CATALOG is not None and p == _CACHED_BASE_PATH:
        return _CACHED_CATALOG

    base = _load_base(p)
    merged = _apply_overrides(base)
    sites: list[Site] = []
    for name, (source, raw) in merged.items():
        s = _coerce_site(name, raw, source=source)
        if s:
            sites.append(s)
    sites.sort(key=lambda s: (s.alexa_rank or 10**9, s.name))
    catalog = SiteCatalog(tuple(sites))
    _CACHED_CATALOG = catalog
    _CACHED_BASE_PATH = p
    return catalog


def reload_catalog() -> SiteCatalog:
    """Drop the cached catalog and re-merge. Called by the admin DB
    editor after every mutation so the next probe sees the new state."""
    global _CACHED_CATALOG, _CACHED_BASE_PATH
    _CACHED_CATALOG = None
    _CACHED_BASE_PATH = None
    return load_catalog()
