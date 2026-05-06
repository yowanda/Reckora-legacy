"""Build curated sites.json from upstream maigret data.json.

Resolves engine-templated sites (Maigret keeps a small set of shared "engines"
— XenForo, phpBB, Discourse, vBulletin, uCoz, Wordpress/Author, Flarum, op.gg,
plus the ``engine404*``/``engineRedirect`` family — and most forum/CMS
entries inherit their detection rules from one of those engines). Maigret
merges those at runtime; we do it at build time so the runtime checker stays
zero-dependency on engine logic.

Filtering rules:

- Drop ``disabled: true`` entries.
- Drop entries whose URL template still lacks ``{username}`` after engine
  resolution and ``{urlMain}`` / ``{urlSubpath}`` substitution (these are
  meta-engine entries or shared bases that aren't directly probable).
- Keep all remaining entries regardless of Alexa rank — the runtime API
  caps each scan via ``SearchRequest.limit`` (default 250, sorted by rank).
  The unranked tail just isn't reached on a default scan.

Usage:
    python scripts/build_sites.py <path/to/maigret/data.json> [out.json]
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ALLOWED_KEYS = {
    "url",
    "urlMain",
    "urlProbe",
    "regexCheck",
    "checkType",
    "absenceStrs",
    "presenseStrs",
    "headers",
    "alexaRank",
    "tags",
    "usernameClaimed",
    "usernameUnclaimed",
    "requestHeadOnly",
    "errors",
    "errorMsg",
    "engine",
    "ignore403",
    "prewarm",
    "identityTypes",
}

# Phase 6a: map maigret ``type`` values to Reckora identity-type names.
# Sites whose ``type`` matches a key here get an ``identityTypes`` annotation
# so the runtime skips them on identity-type-mismatched scans (e.g. a username
# scan won't probe a GAIA-only site).
_MAIGRET_TYPE_TO_IDENTITY: dict[str, str] = {
    "gaia_id": "gaia",
    "yandex_public_id": "yandex",
    "vk_id": "username",
    "steam_id": "username",
    "ok_id": "username",
    "yelp_userid": "username",
    "wikimapia_uid": "username",
    "uidme_uguid": "username",
}

# Phase 2: engines whose sites typically gate behind a session cookie set
# on the home page. Probing the username URL cold often returns 403/302/CF
# challenge; visiting urlMain first primes the cookie jar so the probe
# succeeds. Set conservatively — over-flagging adds latency.
PREWARM_ENGINES = {
    "XenForo",
    "vBulletin",
    "uCoz",
    "Discourse",
}


def _merge_engine(site: dict[str, Any], engine_site: dict[str, Any]) -> dict[str, Any]:
    """Merge engine.site defaults into site dict.

    - dict fields (e.g. ``errors``) — engine entries first, site keys override.
    - list fields (e.g. ``presenseStrs``, ``absenceStrs``) — engine first, then
      site-specific entries appended (additive: matches Maigret's behaviour).
    - scalar fields (``url``, ``checkType``, ``regexCheck``, etc.) — site wins
      if it has its own value, otherwise engine default applies.
    """
    merged: dict[str, Any] = {}
    for k, v in engine_site.items():
        merged[k] = copy.deepcopy(v)
    for k, v in site.items():
        if k in merged and isinstance(v, list) and isinstance(merged[k], list):
            merged[k] = merged[k] + v
        elif k in merged and isinstance(v, dict) and isinstance(merged[k], dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def _substitute(template: str, vars_: dict[str, str]) -> str:
    """Best-effort {placeholder} substitution that leaves {username} alone."""
    out = template
    for k, v in vars_.items():
        out = out.replace("{" + k + "}", v)
    return out


def _resolve_site(
    name: str, site: dict[str, Any], engines: dict[str, Any]
) -> dict[str, Any] | None:
    if site.get("disabled"):
        return None
    engine_name = site.get("engine")
    if engine_name:
        engine = engines.get(engine_name)
        if not engine:
            return None
        engine_site = engine.get("site") or {}
        site = _merge_engine(site, engine_site)
    url_template = site.get("url") or ""
    if not url_template:
        return None
    url_vars = {
        "urlMain": (site.get("urlMain") or "").rstrip("/"),
        "urlSubpath": site.get("urlSubpath") or "",
    }
    url = _substitute(url_template, url_vars)
    if "{username}" not in url:
        return None
    site["url"] = url
    cleaned = {k: site[k] for k in ALLOWED_KEYS if k in site}
    cleaned.setdefault("checkType", "status_code")
    if engine_name in PREWARM_ENGINES and not cleaned.get("prewarm"):
        cleaned["prewarm"] = True
    # Phase 6a: auto-import identityTypes from maigret ``type`` field.
    maigret_type = site.get("type")
    if maigret_type and maigret_type in _MAIGRET_TYPE_TO_IDENTITY:
        mapped = _MAIGRET_TYPE_TO_IDENTITY[maigret_type]
        if mapped != "username":
            cleaned["identityTypes"] = [mapped]
    return cleaned


def main(src: str, dst: str = "backend/data/sites.json") -> None:
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    sites_in = raw.get("sites", {})
    engines = raw.get("engines", {})

    out: dict[str, dict] = {}
    skipped_no_engine = 0
    skipped_no_username = 0
    skipped_disabled = 0
    for name, site in sites_in.items():
        if site.get("disabled"):
            skipped_disabled += 1
            continue
        resolved = _resolve_site(name, site, engines)
        if resolved is None:
            if site.get("engine") and site["engine"] not in engines:
                skipped_no_engine += 1
            else:
                skipped_no_username += 1
            continue
        out[name] = resolved

    out_sorted = dict(
        sorted(
            out.items(),
            key=lambda kv: (kv[1].get("alexaRank") or 10**9, kv[0]),
        )
    )

    payload = {
        "source": "https://github.com/soxoj/maigret (MIT)",
        "license": "MIT",
        "count": len(out_sorted),
        "sites": out_sorted,
    }
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote {len(out_sorted)} sites -> {dst} "
        f"(skipped: {skipped_disabled} disabled, "
        f"{skipped_no_username} no-username-placeholder, "
        f"{skipped_no_engine} unknown-engine)"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "backend/data/sites.json"
    main(src, dst)
