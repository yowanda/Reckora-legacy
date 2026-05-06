"""Phase 5b: scan-history export formatters + ``GET /api/history/<id>/export.<fmt>``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import exports as exports_mod
from app.history import ScanSummary
from app.main import app


def _summary(**overrides: object) -> ScanSummary:
    """Build a ScanSummary fixture with sensible defaults."""
    base: dict[str, object] = {
        "id": 7,
        "member": "alice",
        "username": "torvalds",
        "mode": "list",
        "started_at": 1_700_000_000,
        "finished_at": 1_700_000_042,
        "site_count": 3,
        "claimed": 2,
        "free": 0,
        "blocked": 0,
        "unknown": 1,
        "error_count": 0,
    }
    base.update(overrides)
    return ScanSummary(**base)  # type: ignore[arg-type]


def _results() -> list[dict[str, object]]:
    return [
        {
            "site": "GitHub",
            "status": "claimed",
            "confidence": 95,
            "ml_confidence": 92,
            "url": "https://github.com/torvalds",
            "parent_username": None,
        },
        {
            "site": "Twitter",
            "status": "claimed",
            "confidence": 80,
            "ml_confidence": None,
            "url": "https://twitter.com/torvalds",
            "parent_username": None,
        },
        {
            "site": "Mastodon",
            "status": "unknown",
            "confidence": 30,
            "ml_confidence": None,
            "url": "https://mastodon.social/@torvalds",
            "parent_username": None,
        },
    ]


# ---------------------------------------------------------------------------
# Pure renderer unit tests.
# ---------------------------------------------------------------------------


def test_csv_has_header_and_one_row_per_result() -> None:
    out = exports_mod.to_csv(_summary(), _results())
    lines = out.strip().splitlines()
    assert lines[0] == "site,status,confidence,ml_confidence,url,parent_username"
    # 1 header + 3 result rows
    assert len(lines) == 4


def test_csv_quotes_values_with_special_chars() -> None:
    rows = [
        {
            "site": "Foo, Inc.",
            "status": "claimed",
            "confidence": 90,
            "ml_confidence": None,
            "url": 'https://foo.example/path?q="x"',
            "parent_username": None,
        }
    ]
    out = exports_mod.to_csv(_summary(), rows)
    # Comma in site name forces quoting; embedded double quote is doubled.
    assert '"Foo, Inc."' in out
    assert '""x""' in out


def test_json_round_trips_summary_and_results() -> None:
    import json

    out = exports_mod.to_json(_summary(), _results())
    payload = json.loads(out)
    assert payload["scan"]["id"] == 7
    assert payload["scan"]["username"] == "torvalds"
    assert len(payload["results"]) == 3


def test_markdown_lists_only_claimed_hits() -> None:
    out = exports_mod.to_markdown(_summary(), _results())
    assert "Reckora scan #7" in out
    assert "torvalds" in out
    # CLAIMED hits show up as table rows; unknown rows are summarised
    # only in the count line.
    assert "GitHub" in out
    assert "Twitter" in out
    assert "Mastodon" not in out


def test_markdown_handles_zero_claimed() -> None:
    rows = [r for r in _results() if r["status"] != "claimed"]
    summary = _summary(claimed=0, unknown=1, site_count=1)
    out = exports_mod.to_markdown(summary, rows)
    assert "No CLAIMED hits" in out


def test_markdown_escapes_pipes_in_username() -> None:
    summary = _summary(username="bad|user")
    out = exports_mod.to_markdown(summary, _results())
    # The username appears in the H1, which is not a table cell, so
    # escaping isn't required there. But sanity check it's there.
    assert "bad|user" in out


def test_html_is_self_contained_and_quotes_userdata() -> None:
    rows = [
        {
            "site": "<script>",
            "status": "claimed",
            "confidence": 50,
            "ml_confidence": None,
            "url": "javascript:alert(1)",
            "parent_username": None,
        }
    ]
    summary = _summary(username="<a&b>")
    out = exports_mod.to_html(summary, rows)
    assert out.startswith("<!DOCTYPE html>")
    # Escaped: site name and username never appear unescaped.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<a&b>" not in out
    assert "&lt;a&amp;b&gt;" in out
    # Inline style block, no external assets.
    assert "<style>" in out
    assert "stylesheet" not in out.lower()


def test_html_includes_every_result_row_not_just_claimed() -> None:
    out = exports_mod.to_html(_summary(), _results())
    assert "Mastodon" in out  # unknown rows are also surfaced for printing
    assert "UNKNOWN" in out


def test_filename_is_sanitised() -> None:
    summary = _summary(username="bad/path?name")
    name = exports_mod.filename_for(summary, "csv")
    assert name == "reckora-7-bad_path_name.csv"


def test_render_returns_bytes_and_correct_media_type() -> None:
    body, media, name = exports_mod.render("csv", _summary(), _results())
    assert isinstance(body, bytes)
    assert media.startswith("text/csv")
    assert name.endswith(".csv")


def test_render_unknown_format_raises() -> None:
    with pytest.raises(ValueError):
        # type: ignore[arg-type]
        exports_mod.render("xml", _summary(), _results())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HTTP route — auth gating, format whitelist, content-disposition.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post(
        "/api/auth/login",
        json={"username": "rootadmin", "password": "rootpass1"},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def recorded_scan_id(client: TestClient, admin_token: str) -> int:
    """Insert a small scan via record_scan() so the export endpoint
    has something to render. Returning a fresh id per call (function
    scope) lets tests ignore the rolling history window."""
    from app import history as history_mod

    new_id = history_mod.record_scan(
        member="rootadmin",
        username="exporttest",
        mode="list",
        started_at=1_700_000_000,
        finished_at=1_700_000_005,
        site_count=2,
        results=[
            {
                "site": "Alpha",
                "status": "claimed",
                "confidence": 90,
                "ml_confidence": 88,
                "url": "https://alpha.example/exporttest",
                "parent_username": None,
            },
            {
                "site": "Beta",
                "status": "free",
                "confidence": 80,
                "ml_confidence": None,
                "url": "https://beta.example/exporttest",
                "parent_username": None,
            },
        ],
    )
    assert isinstance(new_id, int)
    return new_id


@pytest.mark.parametrize(
    "fmt,expected_ct,expected_ext",
    [
        ("csv", "text/csv", "csv"),
        ("json", "application/json", "json"),
        ("markdown", "text/markdown", "md"),
        ("html", "text/html", "html"),
    ],
)
def test_export_route_returns_attachment(
    client: TestClient,
    admin_token: str,
    recorded_scan_id: int,
    fmt: str,
    expected_ct: str,
    expected_ext: str,
) -> None:
    r = client.get(
        f"/api/history/{recorded_scan_id}/export.{fmt}",
        headers=_hdr(admin_token),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(expected_ct)
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert f".{expected_ext}" in cd
    # The recorded site name is in every format.
    body = r.content.decode("utf-8")
    assert "Alpha" in body


def test_export_route_rejects_unknown_format(
    client: TestClient,
    admin_token: str,
    recorded_scan_id: int,
) -> None:
    r = client.get(
        f"/api/history/{recorded_scan_id}/export.xml",
        headers=_hdr(admin_token),
    )
    assert r.status_code == 400
    assert "unsupported export format" in r.json()["detail"]


def test_export_route_404_for_missing_scan(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/history/9999999/export.csv", headers=_hdr(admin_token))
    assert r.status_code == 404


def test_export_route_requires_auth(client: TestClient, recorded_scan_id: int) -> None:
    r = client.get(f"/api/history/{recorded_scan_id}/export.csv")
    assert r.status_code == 401


def test_export_route_respects_member_ownership(client: TestClient, recorded_scan_id: int) -> None:
    """A second member cannot read another member's scan."""
    # Create a second member (admin can do this).
    admin = client.post(
        "/api/auth/login",
        json={"username": "rootadmin", "password": "rootpass1"},
    )
    assert admin.status_code == 200
    admin_token = admin.json()["token"]
    create = client.post(
        "/api/admin/members",
        headers=_hdr(admin_token),
        json={
            "username": "exportother",
            "password": "exportotherpass",
            "role": "member",
        },
    )
    assert create.status_code in (200, 201, 409)
    other = client.post(
        "/api/auth/login",
        json={"username": "exportother", "password": "exportotherpass"},
    )
    assert other.status_code == 200
    other_token = other.json()["token"]
    r = client.get(
        f"/api/history/{recorded_scan_id}/export.csv",
        headers=_hdr(other_token),
    )
    assert r.status_code == 404
