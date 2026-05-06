"""Site definitions loader."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from .config import settings

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


@dataclass(frozen=True, slots=True)
class SiteCatalog:
    sites: tuple[Site, ...] = field(default_factory=tuple)

    def by_name(self, name: str) -> Site | None:
        for s in self.sites:
            if s.name == name:
                return s
        return None

    def filtered(self, tags: Iterable[str] | None = None, limit: int | None = None) -> list[Site]:
        wanted = {t.lower() for t in tags or ()}
        out: list[Site] = []
        for s in self.sites:
            if wanted and not ({t.lower() for t in s.tags} & wanted):
                continue
            out.append(s)
            if limit and len(out) >= limit:
                break
        return out

    def all_tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for s in self.sites:
            for t in s.tags:
                counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _coerce_site(name: str, raw: dict) -> Site | None:
    url = raw.get("url")
    if not url or "{username}" not in url:
        return None
    headers_raw = raw.get("headers")
    if isinstance(headers_raw, dict):
        headers = tuple(headers_raw.items())
    elif isinstance(headers_raw, list):
        # Some engine merges produce a list of (k, v) pairs; tolerate that.
        headers = tuple((str(h[0]), str(h[1])) for h in headers_raw if isinstance(h, (list, tuple)) and len(h) == 2)
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
    )


@lru_cache(maxsize=1)
def load_catalog(path: Path | None = None) -> SiteCatalog:
    p = Path(path) if path else settings.sites_path
    payload = json.loads(p.read_text(encoding="utf-8"))
    raw_sites = payload.get("sites") or payload
    sites: list[Site] = []
    for name, raw in raw_sites.items():
        s = _coerce_site(name, raw)
        if s:
            sites.append(s)
    sites.sort(key=lambda s: (s.alexa_rank or 10**9, s.name))
    return SiteCatalog(tuple(sites))
