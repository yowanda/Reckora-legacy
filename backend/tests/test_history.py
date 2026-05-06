"""Phase 4: scan history + diff tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import history as history_mod
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
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolate_history() -> Iterator[None]:
    from app.auth import db_connection

    with db_connection() as conn:
        conn.execute("DELETE FROM scans")
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM scans")


_MEMBER = "rootadmin"


def _seed_scan(member: str, username: str, results: list[dict]) -> int:
    started = 1_700_000_000
    finished = started + 12
    new_id = history_mod.record_scan(
        member=member,
        username=username,
        mode="list",
        started_at=started,
        finished_at=finished,
        site_count=len(results),
        results=results,
    )
    assert new_id is not None
    return new_id


def test_history_list_starts_empty(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/history", headers=_hdr(admin_token))
    assert r.status_code == 200
    assert r.json() == {"scans": []}


def test_history_records_basic_scan_and_lists_it(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        _MEMBER,
        "alice",
        [
            {
                "site": "github",
                "status": "claimed",
                "confidence": 95,
                "ml_confidence": 90,
                "url": "https://github.com/alice",
            },
            {
                "site": "twitter",
                "status": "free",
                "confidence": 70,
                "ml_confidence": 10,
                "url": "https://x.com/alice",
            },
        ],
    )
    r = client.get("/api/history", headers=_hdr(admin_token))
    body = r.json()
    assert len(body["scans"]) == 1
    s = body["scans"][0]
    assert s["id"] == scan_id
    assert s["username"] == "alice"
    assert s["claimed"] == 1
    assert s["free"] == 1


def test_history_get_returns_full_results(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        _MEMBER,
        "bob",
        [
            {
                "site": "github",
                "status": "claimed",
                "confidence": 95,
                "ml_confidence": 90,
                "url": "https://github.com/bob",
            }
        ],
    )
    r = client.get(f"/api/history/{scan_id}", headers=_hdr(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["scan"]["username"] == "bob"
    assert len(body["results"]) == 1
    assert body["results"][0]["site"] == "github"
    assert body["results"][0]["status"] == "claimed"


def test_history_diff_detects_new_claim(client: TestClient, admin_token: str) -> None:
    a = _seed_scan(
        _MEMBER,
        "carol",
        [
            {"site": "github", "status": "free", "confidence": 70, "ml_confidence": 5, "url": ""},
            {
                "site": "reddit",
                "status": "claimed",
                "confidence": 90,
                "ml_confidence": 80,
                "url": "https://reddit.com/u/carol",
            },
        ],
    )
    b = _seed_scan(
        _MEMBER,
        "carol",
        [
            {
                "site": "github",
                "status": "claimed",
                "confidence": 95,
                "ml_confidence": 88,
                "url": "https://github.com/carol",
            },
            {
                "site": "reddit",
                "status": "claimed",
                "confidence": 92,
                "ml_confidence": 85,
                "url": "https://reddit.com/u/carol",
            },
        ],
    )
    r = client.get(f"/api/history/diff/{a}/{b}", headers=_hdr(admin_token))
    assert r.status_code == 200
    diff = r.json()
    sites_newly = [d["site"] for d in diff["newly_claimed"]]
    assert "github" in sites_newly
    assert diff["no_longer_claimed"] == []
    assert diff["unchanged_count"] >= 1  # reddit unchanged


def test_history_diff_detects_lost_claim(client: TestClient, admin_token: str) -> None:
    a = _seed_scan(
        _MEMBER,
        "dave",
        [{"site": "github", "status": "claimed", "confidence": 95, "ml_confidence": 90, "url": ""}],
    )
    b = _seed_scan(
        _MEMBER,
        "dave",
        [{"site": "github", "status": "free", "confidence": 80, "ml_confidence": 5, "url": ""}],
    )
    r = client.get(f"/api/history/diff/{a}/{b}", headers=_hdr(admin_token))
    diff = r.json()
    assert [d["site"] for d in diff["no_longer_claimed"]] == ["github"]
    assert diff["newly_claimed"] == []


def test_history_diff_rejects_same_id(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        _MEMBER,
        "erin",
        [{"site": "github", "status": "claimed", "confidence": 95, "ml_confidence": 90, "url": ""}],
    )
    r = client.get(f"/api/history/diff/{scan_id}/{scan_id}", headers=_hdr(admin_token))
    assert r.status_code == 400


def test_history_pruning_keeps_only_max(
    client: TestClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.history.settings.scan_history_max_per_user", 3)
    for i in range(5):
        history_mod.record_scan(
            member=_MEMBER,
            username="frank",
            mode="list",
            started_at=1_700_000_000 + i,
            finished_at=1_700_000_001 + i,
            site_count=1,
            results=[
                {
                    "site": "github",
                    "status": "claimed",
                    "confidence": 90,
                    "ml_confidence": 80,
                    "url": "",
                }
            ],
        )
    r = client.get("/api/history?username=frank", headers=_hdr(admin_token))
    body = r.json()
    assert len(body["scans"]) == 3


def test_history_delete(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        _MEMBER,
        "grace",
        [{"site": "github", "status": "claimed", "confidence": 90, "ml_confidence": 80, "url": ""}],
    )
    r = client.delete(f"/api/history/{scan_id}", headers=_hdr(admin_token))
    assert r.status_code == 200
    r2 = client.get(f"/api/history/{scan_id}", headers=_hdr(admin_token))
    assert r2.status_code == 404


def test_history_get_other_users_scan_returns_404(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        "someoneelse",
        "alice",
        [{"site": "github", "status": "claimed", "confidence": 90, "ml_confidence": 80, "url": ""}],
    )
    r = client.get(f"/api/history/{scan_id}", headers=_hdr(admin_token))
    assert r.status_code == 404
