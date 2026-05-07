"""Phase 9e tests — quarterly catalog auto-refresh.

The diff classifier and merge logic are pure functions, so the tests in
this module avoid any network I/O — they exercise
:func:`backend.scripts.refresh_catalog.classify_delta`,
:func:`backend.scripts.refresh_catalog.diff_catalogs`,
:func:`backend.scripts.refresh_catalog.merge_into_candidate`,
:func:`backend.scripts.refresh_catalog.infer_identity_type`,
:func:`backend.scripts.refresh_catalog.render_markdown_report`, and the
``main()`` CLI against a tmp directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ``backend/scripts`` is a sibling of ``backend/tests`` and not exposed as
# a package — add the parent directory to ``sys.path`` so the test can
# import the regen module directly without polluting ``app/``.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from scripts import refresh_catalog as rc  # noqa: E402  # sys.path tweak above


def _site(
    *,
    url: str = "https://example.com/{username}",
    url_main: str = "https://example.com/",
    tags: list[str] | None = None,
    engine: str | None = None,
    extras: dict | None = None,
) -> dict:
    entry: dict = {"url": url, "urlMain": url_main, "checkType": "status_code"}
    if tags is not None:
        entry["tags"] = tags
    if engine is not None:
        entry["engine"] = engine
    if extras:
        entry.update(extras)
    return entry


# ---------------------------------------------------------------------------
# infer_identity_type
# ---------------------------------------------------------------------------


def test_infer_identity_type_picks_most_specific_tag() -> None:
    assert rc.infer_identity_type({"tags": ["social", "forum", "tech"]}) == "forum"


def test_infer_identity_type_returns_none_when_no_tags_match_priority() -> None:
    assert rc.infer_identity_type({"tags": ["uncategorized", "misc"]}) is None


def test_infer_identity_type_handles_missing_or_malformed_tags() -> None:
    assert rc.infer_identity_type({}) is None
    assert rc.infer_identity_type({"tags": "social"}) is None  # not a list
    assert rc.infer_identity_type(None) is None


# ---------------------------------------------------------------------------
# classify_delta
# ---------------------------------------------------------------------------


def test_classify_delta_added_when_old_is_none() -> None:
    assert rc.classify_delta(None, _site()) == rc.DELTA_ADDED


def test_classify_delta_removed_when_new_is_none() -> None:
    assert rc.classify_delta(_site(), None) == rc.DELTA_REMOVED


def test_classify_delta_returns_none_when_entries_match_on_tracked_fields() -> None:
    a = _site(tags=["social"], engine="XenForo")
    b = _site(tags=["social"], engine="XenForo")
    assert rc.classify_delta(a, b) is None


def test_classify_delta_url_changed_wins_over_other_kinds() -> None:
    old = _site(url="https://example.com/{username}", tags=["social"], engine="XenForo")
    # Both URL changed AND engine changed AND identity tag flipped — url wins.
    new = _site(
        url="https://example.com/u/{username}",
        tags=["forum"],
        engine="phpBB",
    )
    assert rc.classify_delta(old, new) == rc.DELTA_URL_CHANGED


def test_classify_delta_url_main_change_also_counts_as_url_changed() -> None:
    old = _site(url_main="https://www.example.com/")
    new = _site(url_main="https://example.com/")
    assert rc.classify_delta(old, new) == rc.DELTA_URL_CHANGED


def test_classify_delta_identity_type_changed_when_tags_flip_priority() -> None:
    old = _site(tags=["social", "blog"])
    new = _site(tags=["forum", "blog"])
    assert rc.classify_delta(old, new) == rc.DELTA_IDENTITY_TYPE_CHANGED


def test_classify_delta_engine_hint_changed_when_only_engine_differs() -> None:
    old = _site(tags=["social"], engine="XenForo")
    new = _site(tags=["social"], engine="phpBB")
    assert rc.classify_delta(old, new) == rc.DELTA_ENGINE_HINT_CHANGED


def test_classify_delta_engine_added_or_removed_counts_as_engine_hint_changed() -> None:
    old = _site(tags=["social"])
    new = _site(tags=["social"], engine="XenForo")
    assert rc.classify_delta(old, new) == rc.DELTA_ENGINE_HINT_CHANGED
    assert rc.classify_delta(new, old) == rc.DELTA_ENGINE_HINT_CHANGED


# ---------------------------------------------------------------------------
# diff_catalogs
# ---------------------------------------------------------------------------


def test_diff_catalogs_aggregates_all_kinds_in_alphabetical_order() -> None:
    old = {
        "Alpha": _site(),
        "Beta": _site(url="https://beta.example.com/{username}"),
        "Gone": _site(),
        "Same": _site(tags=["social"]),
    }
    new = {
        "Alpha": _site(tags=["social"], engine="XenForo"),
        "Beta": _site(url="https://beta.example.com/u/{username}"),
        "FreshOne": _site(),
        "Same": _site(tags=["social"]),
    }
    diff = rc.diff_catalogs(old, new)
    assert diff.added == ["FreshOne"]
    assert diff.removed == ["Gone"]
    assert diff.url_changed == ["Beta"]
    # Alpha: URL didn't change but tags went from [] → [social] (None → social
    # in the priority infer) so the first matching kind in
    # url → identity → engine is identity_type_changed. The engine field also
    # changed but identity wins by ordering.
    assert diff.identity_type_changed == ["Alpha"]
    assert diff.engine_hint_changed == []


def test_diff_catalogs_total_changes_matches_sum_of_per_kind_lengths() -> None:
    old = {"OnlyOld": _site(), "Both": _site()}
    new = {"OnlyNew": _site(), "Both": _site(url="https://changed.example.com/{username}")}
    diff = rc.diff_catalogs(old, new)
    expected = (
        len(diff.added)
        + len(diff.removed)
        + len(diff.url_changed)
        + len(diff.identity_type_changed)
        + len(diff.engine_hint_changed)
    )
    assert diff.total_changes == expected
    assert diff.total_changes == 3  # added + removed + url_changed


def test_diff_catalogs_by_kind_returns_full_mapping_with_empty_lists() -> None:
    diff = rc.diff_catalogs({}, {})
    by_kind = diff.by_kind()
    assert set(by_kind.keys()) == set(rc.DELTA_KINDS)
    for v in by_kind.values():
        assert v == []


# ---------------------------------------------------------------------------
# merge_into_candidate
# ---------------------------------------------------------------------------


def test_merge_into_candidate_preserves_reckora_specific_overrides() -> None:
    upstream = {
        "BoardX": _site(
            url="https://boardx.example.com/{username}",
            engine="XenForo",
            tags=["forum"],
        ),
    }
    current = {
        "BoardX": _site(
            url="https://old-boardx.example.com/{username}",
            engine="phpBB",
            tags=["social"],
            extras={
                "prewarm": True,
                "ignore403": True,
                "requestHeadOnly": True,
                "identityTypes": ["forum_handle"],
            },
        ),
    }
    candidate = rc.merge_into_candidate(upstream, current)
    entry = candidate["BoardX"]
    # Upstream-controlled fields overwrite.
    assert entry["url"] == "https://boardx.example.com/{username}"
    assert entry["engine"] == "XenForo"
    assert entry["tags"] == ["forum"]
    # Reckora-specific fields preserved.
    assert entry["prewarm"] is True
    assert entry["ignore403"] is True
    assert entry["requestHeadOnly"] is True
    assert entry["identityTypes"] == ["forum_handle"]


def test_merge_into_candidate_does_not_introduce_overrides_for_brand_new_sites() -> None:
    upstream = {"FreshSite": _site()}
    current = {"OldSite": _site(extras={"prewarm": True})}
    candidate = rc.merge_into_candidate(upstream, current)
    assert "OldSite" not in candidate
    assert "prewarm" not in candidate["FreshSite"]


def test_merge_into_candidate_returns_a_fresh_dict() -> None:
    upstream = {"S": _site()}
    current: dict = {}
    a = rc.merge_into_candidate(upstream, current)
    b = rc.merge_into_candidate(upstream, current)
    assert a == b
    assert a is not b
    a["S"]["url"] = "mutated"
    assert b["S"]["url"] != "mutated"


# ---------------------------------------------------------------------------
# render_markdown_report
# ---------------------------------------------------------------------------


def test_render_markdown_report_emits_summary_table_and_section_per_kind() -> None:
    diff = rc.CatalogDiff(
        added=["A1", "A2"],
        removed=["R1"],
        url_changed=[],
        identity_type_changed=["I1"],
        engine_hint_changed=[],
    )
    md = rc.render_markdown_report(
        diff,
        old_count=1000,
        new_count=1001,
        upstream_tag="v0.5.4",
    )
    assert "# Catalog refresh — soxoj/maigret `v0.5.4`" in md
    assert "Old catalog count: **1000**" in md
    assert "Total deltas: **4**" in md
    # Summary table.
    assert "| `added` | 2 |" in md
    assert "| `removed` | 1 |" in md
    assert "| `url_changed` | 0 |" in md
    # Per-kind sections only when non-empty.
    assert "## `added` (2)" in md
    assert "- `A1`" in md
    assert "## `url_changed`" not in md  # no section for empty kind


def test_render_markdown_report_truncates_large_lists_with_more_marker() -> None:
    diff = rc.CatalogDiff(
        added=[f"S{i:03d}" for i in range(50)],
        removed=[],
        url_changed=[],
        identity_type_changed=[],
        engine_hint_changed=[],
    )
    md = rc.render_markdown_report(diff, old_count=0, new_count=50, sample_limit=20)
    assert "- `S000`" in md
    assert "- `S019`" in md
    assert "- `S020`" not in md
    assert "and 30 more" in md


# ---------------------------------------------------------------------------
# main() CLI integration
# ---------------------------------------------------------------------------


def test_main_writes_candidate_and_report(tmp_path: Path) -> None:
    upstream = {
        "sites": {
            "Fresh": _site(url="https://fresh.example.com/{username}"),
            "Same": _site(tags=["social"]),
        }
    }
    current = {
        "source": "test",
        "license": "MIT",
        "count": 2,
        "sites": {
            "Same": _site(
                tags=["social"],
                extras={"prewarm": True},
            ),
            "Gone": _site(),
        },
    }
    upstream_path = tmp_path / "upstream.json"
    current_path = tmp_path / "current.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "report.md"
    upstream_path.write_text(json.dumps(upstream))
    current_path.write_text(json.dumps(current))

    rc_returned = rc.main(
        [
            "--upstream-data",
            str(upstream_path),
            "--current",
            str(current_path),
            "--candidate",
            str(candidate_path),
            "--report",
            str(report_path),
            "--upstream-tag",
            "v0.5.5",
        ]
    )
    assert rc_returned == 0
    candidate_payload = json.loads(candidate_path.read_text())
    assert candidate_payload["count"] == 2
    assert "Fresh" in candidate_payload["sites"]
    assert "Gone" not in candidate_payload["sites"]
    # Reckora override on Same was preserved.
    assert candidate_payload["sites"]["Same"]["prewarm"] is True

    md = report_path.read_text()
    assert "soxoj/maigret `v0.5.5`" in md
    assert "Total deltas:" in md


def test_main_reads_raw_sites_dict_when_envelope_missing(tmp_path: Path) -> None:
    upstream_path = tmp_path / "upstream.json"
    current_path = tmp_path / "current.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "report.md"
    upstream_path.write_text(json.dumps({"Solo": _site()}))
    current_path.write_text(json.dumps({"Solo": _site()}))
    assert (
        rc.main(
            [
                "--upstream-data",
                str(upstream_path),
                "--current",
                str(current_path),
                "--candidate",
                str(candidate_path),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )


def test_read_sites_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="unexpected catalog shape"):
        rc._read_sites(bad)


# ---------------------------------------------------------------------------
# Constants exposed for downstream tooling
# ---------------------------------------------------------------------------


def test_delta_kinds_tuple_covers_all_named_constants() -> None:
    assert rc.DELTA_KINDS == (
        rc.DELTA_ADDED,
        rc.DELTA_REMOVED,
        rc.DELTA_URL_CHANGED,
        rc.DELTA_IDENTITY_TYPE_CHANGED,
        rc.DELTA_ENGINE_HINT_CHANGED,
    )


def test_reckora_override_fields_match_module_contract() -> None:
    assert (
        frozenset({"prewarm", "ignore403", "requestHeadOnly", "identityTypes"})
        == rc.RECKORA_OVERRIDE_FIELDS
    )
