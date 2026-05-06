"""Build curated sites.json from upstream maigret data.json.

Filters: keep only sites with a username URL pattern, drop disabled, drop
shared-engine entries, and cap by alexaRank for sane defaults.

Usage:
    python scripts/build_sites.py <path/to/maigret/data.json> [out.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
    "request_head_only",
    "errors",
    "errorMsg",
}

MAX_RANK = 200_000


def main(src: str, dst: str = "backend/data/sites.json") -> None:
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    sites_in = raw.get("sites", {})

    out: dict[str, dict] = {}
    for name, site in sites_in.items():
        if site.get("disabled"):
            continue
        if site.get("engine"):
            continue
        url = site.get("url") or ""
        if "{username}" not in url:
            continue
        rank = site.get("alexaRank") or 10**9
        if rank > MAX_RANK:
            continue
        cleaned = {k: site[k] for k in ALLOWED_KEYS if k in site}
        cleaned.setdefault("checkType", "status_code")
        out[name] = cleaned

    out_sorted = dict(
        sorted(out.items(), key=lambda kv: (kv[1].get("alexaRank") or 10**9, kv[0]))
    )

    payload = {
        "source": "https://github.com/soxoj/maigret (MIT)",
        "license": "MIT",
        "count": len(out_sorted),
        "sites": out_sorted,
    }
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(out_sorted)} sites -> {dst}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "backend/data/sites.json"
    main(src, dst)
