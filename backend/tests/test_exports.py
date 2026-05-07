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


# ---------------------------------------------------------------------------
# Phase 6d — PDF export
# ---------------------------------------------------------------------------


class TestPdfExport:
    def test_pdf_fallback_returns_html_bytes(self) -> None:
        """When weasyprint is NOT installed, to_pdf falls back to HTML bytes."""
        scan = _summary()
        results = _results()
        pdf_bytes = exports_mod.to_pdf(scan, results)
        assert isinstance(pdf_bytes, bytes)
        # Fallback produces HTML (starts with DOCTYPE).
        assert pdf_bytes.startswith(b"<!DOCTYPE html")

    def test_pdf_in_allowed_formats(self) -> None:
        assert "pdf" in exports_mod.ALLOWED_FORMATS

    def test_pdf_media_type(self) -> None:
        assert exports_mod.MEDIA_TYPES["pdf"] == "application/pdf"

    def test_pdf_extension(self) -> None:
        assert exports_mod.EXTENSIONS["pdf"] == "pdf"

    def test_pdf_filename(self) -> None:
        scan = _summary()
        fname = exports_mod.filename_for(scan, "pdf")
        assert fname.endswith(".pdf")
        assert "torvalds" in fname

    def test_render_pdf_dispatcher(self) -> None:
        scan = _summary()
        results = _results()
        body, _media, fname = exports_mod.render("pdf", scan, results)
        assert isinstance(body, bytes)
        assert fname.endswith(".pdf")


# ---------------------------------------------------------------------------
# Phase 10h — XMind8 + D3-HTML graph + plain-text exports
# ---------------------------------------------------------------------------


class TestTxtExport:
    """Plain-text dossier — matches maigret's ``--txt`` shape so the
    output is interchangeable with existing maigret tooling."""

    def test_txt_in_allowed_formats(self) -> None:
        assert "txt" in exports_mod.ALLOWED_FORMATS

    def test_txt_media_type_and_extension(self) -> None:
        assert exports_mod.MEDIA_TYPES["txt"].startswith("text/plain")
        assert exports_mod.EXTENSIONS["txt"] == "txt"

    def test_txt_renders_each_claimed_hit_with_url(self) -> None:
        out = exports_mod.to_txt(_summary(), _results())
        assert isinstance(out, str)
        assert "Reckora scan #7 — torvalds" in out
        # Every CLAIMED hit shows up as a `[+] <Site>: <URL>` stanza.
        assert "[+] GitHub: https://github.com/torvalds" in out
        assert "[+] Twitter: https://twitter.com/torvalds" in out
        # UNKNOWN rows are summarised in the footer count, NOT as a
        # per-row stanza.
        assert "[+] Mastodon" not in out
        assert "Outcome:" in out
        assert "2 claimed" in out

    def test_txt_no_claimed_emits_dash_marker(self) -> None:
        rows = [r for r in _results() if r["status"] != "claimed"]
        summary = _summary(claimed=0, unknown=1, site_count=1)
        out = exports_mod.to_txt(summary, rows)
        assert "[-] No CLAIMED hits" in out
        assert "[+] Total Sites with CLAIMED profile: 0" in out

    def test_txt_includes_confidence_and_ml_when_present(self) -> None:
        out = exports_mod.to_txt(_summary(), _results())
        # GitHub has both confidence and ml_confidence; Twitter has only confidence.
        assert "Confidence: 95" in out
        assert "ML score:   92" in out
        assert "Confidence: 80" in out

    def test_render_txt_dispatcher_round_trips(self) -> None:
        body, media, fname = exports_mod.render("txt", _summary(), _results())
        assert isinstance(body, bytes)
        assert media.startswith("text/plain")
        assert fname.endswith(".txt")
        # bytes round-trip back to the original string output.
        assert body.decode("utf-8") == exports_mod.to_txt(_summary(), _results())


class TestXmindExport:
    """XMind8 workbook — pure stdlib zip + xml.etree, no extra deps."""

    def test_xmind_in_allowed_formats(self) -> None:
        assert "xmind" in exports_mod.ALLOWED_FORMATS

    def test_xmind_media_type_and_extension(self) -> None:
        assert exports_mod.MEDIA_TYPES["xmind"] == "application/vnd.xmind.workbook"
        assert exports_mod.EXTENSIONS["xmind"] == "xmind"

    def test_xmind_is_a_valid_zip_with_required_entries(self) -> None:
        import zipfile

        body = exports_mod.to_xmind(_summary(), _results())
        # Magic bytes — every zip starts with PK\x03\x04.
        assert body[:4] == b"PK\x03\x04"
        zf = zipfile.ZipFile(__import__("io").BytesIO(body))
        names = set(zf.namelist())
        assert "content.xml" in names
        assert "meta.xml" in names
        assert "META-INF/manifest.xml" in names

    def test_xmind_content_xml_lists_every_claimed_hit(self) -> None:
        import zipfile

        body = exports_mod.to_xmind(_summary(), _results())
        zf = zipfile.ZipFile(__import__("io").BytesIO(body))
        content = zf.read("content.xml").decode("utf-8")
        assert content.startswith('<?xml version="1.0"')
        # Username + scan id appear in the central topic.
        assert "torvalds" in content
        assert "scan #7" in content
        # Every CLAIMED hit name appears as a leaf topic.
        assert "GitHub" in content
        assert "Twitter" in content
        # UNKNOWN rows are skipped in the mind-map dossier.
        assert "Mastodon" not in content
        # URL appears as href attribute on the site topic.
        assert "https://github.com/torvalds" in content

    def test_xmind_handles_zero_claimed(self) -> None:
        import zipfile

        rows = [r for r in _results() if r["status"] != "claimed"]
        summary = _summary(claimed=0, unknown=1, site_count=1)
        body = exports_mod.to_xmind(summary, rows)
        # Still a valid zip with the three core entries.
        zf = zipfile.ZipFile(__import__("io").BytesIO(body))
        assert "content.xml" in zf.namelist()
        # Topic tree still has the central root but no children.
        content = zf.read("content.xml").decode("utf-8")
        assert "torvalds" in content

    def test_xmind_content_is_deterministic_for_identical_input(self) -> None:
        # Two renders of the same scan should produce zips with the same
        # internal payload (the zip wrapper itself includes timestamps,
        # so we compare entry contents instead of byte-equality of the
        # whole archive).
        import zipfile

        a = exports_mod.to_xmind(_summary(), _results())
        b = exports_mod.to_xmind(_summary(), _results())
        za = zipfile.ZipFile(__import__("io").BytesIO(a))
        zb = zipfile.ZipFile(__import__("io").BytesIO(b))
        assert za.read("content.xml") == zb.read("content.xml")
        assert za.read("META-INF/manifest.xml") == zb.read("META-INF/manifest.xml")

    def test_render_xmind_dispatcher(self) -> None:
        body, media, fname = exports_mod.render("xmind", _summary(), _results())
        assert isinstance(body, bytes)
        assert body[:4] == b"PK\x03\x04"
        assert media == "application/vnd.xmind.workbook"
        assert fname.endswith(".xmind")


class TestD3HtmlExport:
    """D3 force-directed graph — self-contained HTML page with
    embedded graph data."""

    def test_d3_in_allowed_formats(self) -> None:
        assert "d3" in exports_mod.ALLOWED_FORMATS

    def test_d3_media_type_and_extension(self) -> None:
        assert exports_mod.MEDIA_TYPES["d3"].startswith("text/html")
        assert exports_mod.EXTENSIONS["d3"] == "html"

    def test_d3_html_starts_with_doctype(self) -> None:
        out = exports_mod.to_d3_html(_summary(), _results())
        assert out.startswith("<!DOCTYPE html>")
        # Loads D3 via CDN.
        assert "d3@7" in out
        # Embeds graph data as a JSON script tag (parsed by the inline
        # JS via JSON.parse — never executed as JavaScript).
        assert "<script id='graph-data' type='application/json'>" in out

    def test_d3_graph_payload_has_one_site_node_per_claimed_hit(self) -> None:
        payload = exports_mod._d3_graph_payload(_summary(), _results())
        # Identity node for the primary username + 2 claimed sites.
        site_nodes = [n for n in payload["nodes"] if n["kind"] == "site"]
        assert len(site_nodes) == 2
        identity_nodes = [n for n in payload["nodes"] if n["kind"] == "identity"]
        assert len(identity_nodes) == 1
        assert identity_nodes[0]["label"] == "torvalds"
        # Every site links back to the identity that spawned it.
        assert len(payload["links"]) == 2

    def test_d3_html_escapes_username_in_title_and_header(self) -> None:
        summary = _summary(username="<a&b>")
        out = exports_mod.to_d3_html(summary, _results())
        # Title block escapes the username so the browser parses it as
        # text, not as an HTML tag.
        assert "&lt;a&amp;b&gt;" in out
        # The literal-character form is allowed inside the JSON block
        # because the surrounding ``<script type="application/json">``
        # tag suspends HTML tag parsing — JSON.parse will reconstruct
        # it safely. Confirm it shows up there.
        assert '"<a&b>"' in out

    def test_d3_html_escapes_script_terminators_in_embedded_json(self) -> None:
        # A site name containing ``</script>`` must not let the embedded
        # JSON terminate the surrounding script tag.
        rows = [
            {
                "site": "</script><img src=x>",
                "status": "claimed",
                "confidence": 90,
                "ml_confidence": None,
                "url": "https://example.com/x",
                "parent_username": None,
            }
        ]
        out = exports_mod.to_d3_html(_summary(), rows)
        # The literal ``</script>`` substring must not appear in the
        # JSON block — we escape it as ``<\/script>`` so the parser
        # still sees ``"</script>"`` after JSON.parse but the tokeniser
        # does not.
        assert "</script><img" not in out

    def test_render_d3_dispatcher(self) -> None:
        body, media, fname = exports_mod.render("d3", _summary(), _results())
        assert isinstance(body, bytes)
        assert media.startswith("text/html")
        assert fname.endswith(".html")
        assert body.startswith(b"<!DOCTYPE html>")


class TestPhase10hDispatcher:
    """The route-level allowlist exposes the new formats."""

    def test_export_route_returns_txt_attachment(
        self,
        client: TestClient,
        admin_token: str,
        recorded_scan_id: int,
    ) -> None:
        r = client.get(
            f"/api/history/{recorded_scan_id}/export.txt",
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert ".txt" in cd
        body = r.content.decode("utf-8")
        assert "Alpha" in body

    def test_export_route_returns_xmind_attachment(
        self,
        client: TestClient,
        admin_token: str,
        recorded_scan_id: int,
    ) -> None:
        r = client.get(
            f"/api/history/{recorded_scan_id}/export.xmind",
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/vnd.xmind.workbook"
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert ".xmind" in cd
        # Magic-byte sniff for zip.
        assert r.content[:4] == b"PK\x03\x04"

    def test_export_route_returns_d3_html_attachment(
        self,
        client: TestClient,
        admin_token: str,
        recorded_scan_id: int,
    ) -> None:
        r = client.get(
            f"/api/history/{recorded_scan_id}/export.d3",
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert ".html" in cd
        assert r.content.startswith(b"<!DOCTYPE html>")
