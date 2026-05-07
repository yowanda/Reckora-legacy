"""HTTP-level + module-level tests for the Phase 8d public share links."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import history as history_mod
from app import share_links as share_links_mod
from app.main import app


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
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolate_state(client: TestClient) -> Iterator[None]:
    """Wipe scans + share_links + admin overrides between tests.

    Depends on ``client`` so the FastAPI startup hook (``init_db``) has
    already run by the time we touch the tables.
    """
    from app.auth import db_connection

    with db_connection() as conn:
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM share_links")
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM share_links")
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()


def _seed_scan(*, member: str = "rootadmin", username: str = "alice") -> int:
    started = 1_700_000_000
    finished = started + 12
    new_id = history_mod.record_scan(
        member=member,
        username=username,
        mode="list",
        started_at=started,
        finished_at=finished,
        site_count=2,
        results=[
            {
                "site": "GitHub",
                "status": "claimed",
                "url": "https://github.com/alice",
                "url_main": "https://github.com",
                "tags": ["dev"],
                "confidence": 90,
                "ml_confidence": 85,
            },
            {
                "site": "Reddit",
                "status": "free",
                "url": "https://reddit.com/u/alice",
                "url_main": "https://reddit.com",
                "tags": [],
                "confidence": 10,
                "ml_confidence": 5,
            },
        ],
    )
    assert new_id is not None
    return new_id


@pytest.fixture()
def shares_on() -> Iterator[None]:
    admin_settings_mod.set_override("enable_share_links", 1, actor="rootadmin")
    yield
    admin_settings_mod.delete_override("enable_share_links")


# ---------------------------------------------------------------------------
# Module-level (storage) tests.
# ---------------------------------------------------------------------------


def test_mint_creates_token_and_lookup_returns_link() -> None:
    scan_id = _seed_scan()
    link = share_links_mod.mint(scan_id=scan_id, member="rootadmin")
    assert link is not None
    assert link.scan_id == scan_id
    assert len(link.token) >= 32
    found = share_links_mod.lookup(link.token)
    assert found is not None
    assert found.token == link.token


def test_mint_caps_per_scan() -> None:
    scan_id = _seed_scan()
    for _ in range(3):
        assert share_links_mod.mint(scan_id=scan_id, member="rootadmin", max_per_scan=3) is not None
    # 4th attempt should be refused.
    assert share_links_mod.mint(scan_id=scan_id, member="rootadmin", max_per_scan=3) is None


def test_lookup_returns_none_for_expired_token() -> None:
    scan_id = _seed_scan()
    link = share_links_mod.mint(scan_id=scan_id, member="rootadmin", ttl_sec=10)
    assert link is not None
    # Force the lookup to use a clock past the expiry.
    found = share_links_mod.lookup(link.token, now=link.expires_at + 1)
    assert found is None


def test_lookup_returns_none_for_unknown_token() -> None:
    assert share_links_mod.lookup("not-a-real-token") is None


def test_revoke_only_deletes_owners_token() -> None:
    scan_id = _seed_scan(member="rootadmin")
    link = share_links_mod.mint(scan_id=scan_id, member="rootadmin")
    assert link is not None
    # Cross-member revoke is a no-op.
    assert share_links_mod.revoke(token=link.token, member="someone-else") is False
    assert share_links_mod.lookup(link.token) is not None
    assert share_links_mod.revoke(token=link.token, member="rootadmin") is True
    assert share_links_mod.lookup(link.token) is None


def test_revoke_all_for_scan_cleans_cascade() -> None:
    scan_id = _seed_scan()
    for _ in range(3):
        share_links_mod.mint(scan_id=scan_id, member="rootadmin")
    removed = share_links_mod.revoke_all_for_scan(scan_id=scan_id)
    assert removed == 3


def test_purge_expired_cleans_old_rows() -> None:
    scan_id = _seed_scan()
    short = share_links_mod.mint(scan_id=scan_id, member="rootadmin", ttl_sec=5)
    forever = share_links_mod.mint(scan_id=scan_id, member="rootadmin")
    assert short is not None and forever is not None
    purged = share_links_mod.purge_expired(now=short.expires_at + 1)
    assert purged == 1
    assert share_links_mod.lookup(forever.token) is not None


def test_etag_changes_when_finished_at_changes() -> None:
    scan_id = _seed_scan()
    link = share_links_mod.mint(scan_id=scan_id, member="rootadmin")
    assert link is not None
    a = share_links_mod.etag_for(link, finished_at=1_700_000_012)
    b = share_links_mod.etag_for(link, finished_at=1_700_000_999)
    assert a != b
    assert a == share_links_mod.etag_for(link, finished_at=1_700_000_012)


# ---------------------------------------------------------------------------
# HTTP gate: knob off ⇒ all routes refuse.
# ---------------------------------------------------------------------------


def test_routes_503_when_knob_off(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan()
    h = _hdr(admin_token)
    assert client.post(f"/api/history/{scan_id}/share", json={}, headers=h).status_code == 503
    assert client.get(f"/api/history/{scan_id}/shares", headers=h).status_code == 503
    assert client.delete(f"/api/history/{scan_id}/share/foo", headers=h).status_code == 503
    # Public endpoint also gates on the knob.
    r = client.get("/share/some-token")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# HTTP: end-to-end mint → list → public read → revoke.
# ---------------------------------------------------------------------------


def test_mint_list_public_revoke_round_trip(
    client: TestClient, admin_token: str, shares_on: None
) -> None:
    scan_id = _seed_scan()
    h = _hdr(admin_token)

    r = client.post(f"/api/history/{scan_id}/share", json={}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "token" in body["share"]
    token = body["share"]["token"]

    r = client.get(f"/api/history/{scan_id}/shares", headers=h)
    assert r.status_code == 200
    shares = r.json()["shares"]
    assert len(shares) == 1
    assert shares[0]["token"] == token

    # Public read: no auth header.
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    payload = r.json()
    assert payload["scan"]["id"] == scan_id
    assert isinstance(payload["results"], list)
    assert "etag" in r.headers
    assert "s-maxage" in r.headers["cache-control"]
    assert r.headers.get("vary") == "accept-encoding"

    # ETag revalidation returns 304 with the same headers.
    r2 = client.get(f"/share/{token}", headers={"if-none-match": r.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers.get("etag") == r.headers["etag"]
    assert "s-maxage" in r2.headers["cache-control"]

    # Revoke and confirm 404.
    r = client.delete(f"/api/history/{scan_id}/share/{token}", headers=h)
    assert r.status_code == 200
    r = client.get(f"/share/{token}")
    assert r.status_code == 404


def test_public_read_has_no_member_identity(
    client: TestClient, admin_token: str, shares_on: None
) -> None:
    scan_id = _seed_scan()
    r = client.post(f"/api/history/{scan_id}/share", json={}, headers=_hdr(admin_token))
    token = r.json()["share"]["token"]
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    body = r.json()
    # Owner is on the scan summary (it's part of the existing public
    # shape) but no auth metadata leaks.
    assert "share" in body
    assert "scan" in body
    assert "results" in body
    assert "session" not in body
    assert "password" not in body


def test_unknown_token_returns_404(client: TestClient, shares_on: None) -> None:
    r = client.get("/share/definitely-not-a-real-token-1234567890")
    assert r.status_code == 404


def test_other_member_cannot_mint(client: TestClient, admin_token: str, shares_on: None) -> None:
    scan_id = _seed_scan(member="rootadmin")
    # Create a non-admin member and try to mint against rootadmin's scan.
    r = client.post(
        "/api/admin/members",
        json={"username": "shareuser", "password": "sharepass1", "role": "member"},
        headers=_hdr(admin_token),
    )
    assert r.status_code in (201, 409)
    r = client.post(
        "/api/auth/login",
        json={"username": "shareuser", "password": "sharepass1"},
    )
    member_token = r.json()["token"]
    r = client.post(
        f"/api/history/{scan_id}/share",
        json={},
        headers=_hdr(member_token),
    )
    assert r.status_code == 404  # cross-member returns 404 (privacy)


def test_history_delete_cascades_share_links(
    client: TestClient, admin_token: str, shares_on: None
) -> None:
    scan_id = _seed_scan()
    h = _hdr(admin_token)
    r = client.post(f"/api/history/{scan_id}/share", json={}, headers=h)
    token = r.json()["share"]["token"]

    r = client.delete(f"/api/history/{scan_id}", headers=h)
    assert r.status_code == 200
    # Public link should now 404.
    r = client.get(f"/share/{token}")
    assert r.status_code == 404


def test_health_includes_share_links_descriptor(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "share_links" in body
    assert "enabled" in body["share_links"]
    assert "cache_max_age_sec" in body["share_links"]
    assert isinstance(body["share_links"]["enabled"], bool)
    assert body["share_links"]["cache_max_age_sec"] >= 1
