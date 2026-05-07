"""Phase 9e — quarterly catalog auto-refresh from upstream maigret.

Closes residual risk **M4** (~5%/year drift in the bundled
:mod:`backend.app.data.sites` catalog) by running once a quarter from a
GitHub Actions cron, downloading the latest tagged
``soxoj/maigret/maigret/resources/data.json``, regenerating Reckora's
``backend/app/data/sites.json`` via the existing
:mod:`backend.scripts.build_sites` pipeline, and emitting a structured
markdown diff report so a human reviewer can decide whether to land the
quarterly catalog refresh as a normal PR.

The script is intentionally split into pure functions so the diff
classifier (the only piece with non-trivial logic) is unit-testable
without any network or filesystem dependency.

Diff classifier: each (old_entry, new_entry) pair maps to one of

* :data:`DELTA_ADDED`           — site exists upstream but not in Reckora
* :data:`DELTA_REMOVED`         — site exists in Reckora but not upstream
* :data:`DELTA_URL_CHANGED`     — ``url`` / ``urlMain`` template changed
* :data:`DELTA_IDENTITY_TYPE_CHANGED`
                                — ``tags`` set changed in a way that
                                  shifts the inferred identity type (e.g.
                                  ``social`` → ``forum``); Reckora's
                                  per-site ``identityTypes`` override may
                                  need re-evaluation.
* :data:`DELTA_ENGINE_HINT_CHANGED`
                                — ``engine`` field changed (a Maigret
                                  shared engine like ``XenForo`` /
                                  ``phpBB`` was swapped or added/removed)

Reckora-specific fields (``prewarm``, ``ignore403``, ``requestHeadOnly``,
``identityTypes`` overrides — see :func:`merge_into_candidate`) are
preserved in the regenerated candidate so a quarterly catalog refresh
does not undo Phase 5h / Phase 6 precision wins.

Usage::

    # Refresh the catalog, write a candidate sites.json, and dump the
    # markdown diff to docs/catalog-refresh-<tag>.md:
    python -m backend.scripts.refresh_catalog \
        --upstream-data data.json \
        --current backend/app/data/sites.json \
        --candidate /tmp/sites.candidate.json \
        --report docs/catalog-refresh-2026Q3.md

The GitHub Actions workflow at
``.github/workflows/catalog-regen.yml`` (cron ``0 5 1 */3 *``) wires
this together with :mod:`backend.scripts.build_sites` so the quarterly
PR opens with the report inlined.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Delta categories — exposed for tests and the markdown renderer.
DELTA_ADDED = "added"
DELTA_REMOVED = "removed"
DELTA_URL_CHANGED = "url_changed"
DELTA_IDENTITY_TYPE_CHANGED = "identityType_changed"
DELTA_ENGINE_HINT_CHANGED = "engine_hint_changed"

DELTA_KINDS: tuple[str, ...] = (
    DELTA_ADDED,
    DELTA_REMOVED,
    DELTA_URL_CHANGED,
    DELTA_IDENTITY_TYPE_CHANGED,
    DELTA_ENGINE_HINT_CHANGED,
)

# Reckora-specific fields that must NOT be discarded when regenerating the
# catalog from upstream — these are operator-tuned overrides that closed
# Phase 5h / Phase 6 precision gaps and the upstream catalog does not
# carry them.
RECKORA_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {
        "prewarm",
        "ignore403",
        "requestHeadOnly",
        "identityTypes",
    }
)

# A small allow-list of "identity type" tags. Maigret's ``tags`` field is
# a free-form list and most entries carry several tags; the identity type
# inference picks the most-specific tag in this priority order. This is
# how a new XenForo board going from ``social`` to ``forum`` upstream
# surfaces as DELTA_IDENTITY_TYPE_CHANGED.
_IDENTITY_TAG_PRIORITY: tuple[str, ...] = (
    "dating",
    "gaming",
    "porn",
    "forum",
    "music",
    "video",
    "art",
    "shopping",
    "news",
    "tech",
    "blog",
    "social",
)


def infer_identity_type(entry: Mapping[str, Any] | None) -> str | None:
    """Pick the most-specific identity tag from ``entry["tags"]``.

    Returns ``None`` if the entry has no ``tags`` field or no tag matches
    the priority list. The result is the canonical "identity type" used
    by Reckora's SPA badge — and stable across catalog refreshes unless
    upstream genuinely re-tags a site.
    """

    if not entry:
        return None
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return None
    tag_set = {t for t in tags if isinstance(t, str)}
    for candidate in _IDENTITY_TAG_PRIORITY:
        if candidate in tag_set:
            return candidate
    return None


def _url_pair(entry: Mapping[str, Any] | None) -> tuple[str, str]:
    if not entry:
        return ("", "")
    return (
        str(entry.get("url", "")),
        str(entry.get("urlMain", "")),
    )


def _engine_hint(entry: Mapping[str, Any] | None) -> str:
    if not entry:
        return ""
    return str(entry.get("engine", ""))


def classify_delta(
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any] | None,
) -> str | None:
    """Classify a single (old, new) pair into one of :data:`DELTA_KINDS`.

    Returns ``None`` when the entries are byte-equivalent on the fields
    the classifier cares about. The classifier is order-sensitive: when
    multiple categories apply (e.g. an engine swap that also rewrites
    the URL template), the first match in the order
    ``url_changed → identityType_changed → engine_hint_changed`` wins
    so the most-disruptive category surfaces in the diff report.
    """

    if old is None and new is None:
        return None
    if old is None and new is not None:
        return DELTA_ADDED
    if old is not None and new is None:
        return DELTA_REMOVED
    assert old is not None and new is not None  # for mypy

    if _url_pair(old) != _url_pair(new):
        return DELTA_URL_CHANGED
    if infer_identity_type(old) != infer_identity_type(new):
        return DELTA_IDENTITY_TYPE_CHANGED
    if _engine_hint(old) != _engine_hint(new):
        return DELTA_ENGINE_HINT_CHANGED
    return None


@dataclass(frozen=True)
class CatalogDiff:
    """Aggregated diff between two catalog snapshots.

    The members are ordered lists keyed by site name so the markdown
    renderer can emit deterministic output (sort-on-render is enough —
    catalog regen needs reproducible reports for PR review).
    """

    added: list[str]
    removed: list[str]
    url_changed: list[str]
    identity_type_changed: list[str]
    engine_hint_changed: list[str]

    @property
    def total_changes(self) -> int:
        return (
            len(self.added)
            + len(self.removed)
            + len(self.url_changed)
            + len(self.identity_type_changed)
            + len(self.engine_hint_changed)
        )

    def by_kind(self) -> dict[str, list[str]]:
        return {
            DELTA_ADDED: list(self.added),
            DELTA_REMOVED: list(self.removed),
            DELTA_URL_CHANGED: list(self.url_changed),
            DELTA_IDENTITY_TYPE_CHANGED: list(self.identity_type_changed),
            DELTA_ENGINE_HINT_CHANGED: list(self.engine_hint_changed),
        }


def diff_catalogs(
    old_sites: Mapping[str, Mapping[str, Any]],
    new_sites: Mapping[str, Mapping[str, Any]],
) -> CatalogDiff:
    """Diff two ``sites`` dicts and return a :class:`CatalogDiff`.

    Both inputs are the inner ``sites`` dict (not the wrapping
    ``{source, license, count, sites}`` envelope). The function is
    insensitive to dict iteration order — the returned lists are
    sorted alphabetically to keep the markdown report stable across
    Python interpreter versions.
    """

    added: list[str] = []
    removed: list[str] = []
    url_changed: list[str] = []
    identity_type_changed: list[str] = []
    engine_hint_changed: list[str] = []

    all_names: set[str] = set(old_sites) | set(new_sites)
    for name in sorted(all_names):
        kind = classify_delta(old_sites.get(name), new_sites.get(name))
        if kind == DELTA_ADDED:
            added.append(name)
        elif kind == DELTA_REMOVED:
            removed.append(name)
        elif kind == DELTA_URL_CHANGED:
            url_changed.append(name)
        elif kind == DELTA_IDENTITY_TYPE_CHANGED:
            identity_type_changed.append(name)
        elif kind == DELTA_ENGINE_HINT_CHANGED:
            engine_hint_changed.append(name)
    return CatalogDiff(
        added=added,
        removed=removed,
        url_changed=url_changed,
        identity_type_changed=identity_type_changed,
        engine_hint_changed=engine_hint_changed,
    )


def merge_into_candidate(
    upstream_sites: Mapping[str, Mapping[str, Any]],
    current_sites: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a candidate ``sites`` dict that replaces upstream-controlled
    fields but keeps Reckora-specific overrides (``prewarm``,
    ``ignore403``, ``requestHeadOnly``, ``identityTypes``).

    The result is a brand-new dict — neither input is mutated.
    """

    candidate: dict[str, dict[str, Any]] = {}
    for name, entry in upstream_sites.items():
        merged: dict[str, Any] = dict(entry)
        existing = current_sites.get(name)
        if existing is not None:
            for field in RECKORA_OVERRIDE_FIELDS:
                if field in existing:
                    merged[field] = existing[field]
        candidate[name] = merged
    return candidate


def render_markdown_report(
    diff: CatalogDiff,
    *,
    old_count: int,
    new_count: int,
    upstream_tag: str | None = None,
    sample_limit: int = 20,
) -> str:
    """Render a stable markdown diff suitable for a PR body."""

    lines: list[str] = []
    title_tag = upstream_tag or "latest"
    lines.append(f"# Catalog refresh — soxoj/maigret `{title_tag}`")
    lines.append("")
    lines.append(f"- Old catalog count: **{old_count}** sites")
    lines.append(f"- New catalog count: **{new_count}** sites")
    lines.append(f"- Total deltas: **{diff.total_changes}**")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("| --- | ---: |")
    by_kind = diff.by_kind()
    for kind in DELTA_KINDS:
        lines.append(f"| `{kind}` | {len(by_kind[kind])} |")
    lines.append("")

    for kind in DELTA_KINDS:
        names = by_kind[kind]
        if not names:
            continue
        lines.append(f"## `{kind}` ({len(names)})")
        lines.append("")
        sample = names[:sample_limit]
        for name in sample:
            lines.append(f"- `{name}`")
        if len(names) > sample_limit:
            lines.append(f"- … and {len(names) - sample_limit} more")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_sites(path: Path) -> dict[str, dict[str, Any]]:
    """Read either the wrapping envelope shape or a raw ``sites`` dict."""

    raw: Any = json.loads(path.read_text())
    if isinstance(raw, Mapping) and "sites" in raw and isinstance(raw["sites"], Mapping):
        return dict(raw["sites"])
    if isinstance(raw, Mapping):
        return dict(raw)
    raise ValueError(f"unexpected catalog shape in {path!s}: top-level is {type(raw).__name__}")


def _site_count(sites: Mapping[str, Mapping[str, Any]]) -> int:
    return len(sites)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="refresh_catalog",
        description=(
            "Phase 9e — diff Reckora's bundled catalog against the upstream "
            "soxoj/maigret data.json snapshot, regenerate a candidate "
            "sites.json (preserving Reckora-specific overrides), and emit a "
            "PR-ready markdown report."
        ),
    )
    p.add_argument(
        "--upstream-data",
        required=True,
        type=Path,
        help="Path to a downloaded soxoj/maigret data.json (use scripts/build_sites.py to convert to Reckora's format first).",
    )
    p.add_argument(
        "--current",
        required=True,
        type=Path,
        help="Path to the currently bundled backend/app/data/sites.json.",
    )
    p.add_argument(
        "--candidate",
        required=True,
        type=Path,
        help="Where to write the regenerated candidate sites.json (with Reckora overrides preserved).",
    )
    p.add_argument(
        "--report",
        required=True,
        type=Path,
        help="Where to write the markdown diff report.",
    )
    p.add_argument(
        "--upstream-tag",
        default=None,
        help="Optional upstream tag string for the report header (e.g. v0.5.4).",
    )
    return p


def _emit_candidate(
    upstream: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    candidate_path: Path,
) -> int:
    candidate_sites = merge_into_candidate(upstream, current)
    payload = {
        "source": "https://github.com/soxoj/maigret (MIT)",
        "license": "MIT",
        "count": len(candidate_sites),
        "sites": candidate_sites,
    }
    candidate_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return len(candidate_sites)


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_argparser().parse_args(list(argv) if argv is not None else None)

    upstream = _read_sites(args.upstream_data)
    current = _read_sites(args.current)

    diff = diff_catalogs(current, upstream)
    new_count = _emit_candidate(upstream, current, args.candidate)
    report = render_markdown_report(
        diff,
        old_count=_site_count(current),
        new_count=new_count,
        upstream_tag=args.upstream_tag,
    )
    args.report.write_text(report)
    print(
        f"refresh_catalog: {diff.total_changes} delta(s); "
        f"old={_site_count(current)} new={new_count}; "
        f"candidate={args.candidate}; report={args.report}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
