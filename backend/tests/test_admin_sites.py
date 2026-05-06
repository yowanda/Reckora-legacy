"""Phase 4: live DB editor tests — site_overrides table + merge logic."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sites import reload_catalog


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
def _isolate_overrides(client: TestClient, admin_token: str) -> Iterator[None]:
    """Wipe overrides before & after each test so they can't leak."""
    from app.auth import db_connection
    with db_connection() as conn:
        conn.execute("DELETE FROM site_overrides")
    reload_catalog()
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM site_overrides")
    reload_catalog()


def test_admin_list_sites_requires_admin(client: TestClient) -> None:
    r = client.get("/api/admin/sites")
    assert r.status_code == 401


def test_admin_list_sites_returns_provenance(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/admin/sites", headers=_hdr(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 100  # bundled sites.json has thousands
    assert body["custom_count"] == 0
    assert body["overridden_count"] == 0
    sample = body["sites"][0]
    assert sample["source"] == "base"
    assert "raw" in sample
    assert "url" in sample["raw"]


def test_admin_upsert_creates_custom_site(client: TestClient, admin_token: str) -> None:
    r = client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={
            "name": "MySpaceTest",
            "operation": "upsert",
            "patch": {
                "url": "https://example.test/{username}",
                "urlMain": "https://example.test/",
                "checkType": "status_code",
                "tags": ["custom"],
                "presenseStrs": ["welcome"],
            },
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["site"]["name"] == "MySpaceTest"
    assert body["site"]["source"] == "custom"
    assert body["site"]["raw"]["url"].startswith("https://example.test/")

    # confirm it shows up in the merged list
    r2 = client.get("/api/admin/sites/MySpaceTest", headers=_hdr(admin_token))
    assert r2.status_code == 200
    assert r2.json()["source"] == "custom"


def test_admin_upsert_rejects_new_site_without_username_placeholder(
    client: TestClient, admin_token: str
) -> None:
    r = client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={
            "name": "BadNew",
            "operation": "upsert",
            "patch": {"url": "https://no-placeholder.example/", "checkType": "status_code"},
        },
    )
    assert r.status_code == 400
    assert "placeholder" in r.json()["detail"]


def test_admin_disable_existing_site(client: TestClient, admin_token: str) -> None:
    # Find an arbitrary base site
    r = client.get("/api/admin/sites", headers=_hdr(admin_token))
    sample_name = r.json()["sites"][0]["name"]

    r2 = client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={"name": sample_name, "operation": "disable", "patch": {}},
    )
    assert r2.status_code == 201
    assert r2.json()["site"]["disabled"] is True

    r3 = client.get(f"/api/admin/sites/{sample_name}", headers=_hdr(admin_token))
    assert r3.json()["disabled"] is True
    assert r3.json()["source"] == "override"


def test_admin_delete_removes_site_from_runtime(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/admin/sites", headers=_hdr(admin_token))
    body = r.json()
    sample_name = body["sites"][0]["name"]
    base_total = body["total"]

    r2 = client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={"name": sample_name, "operation": "delete", "patch": {}},
    )
    assert r2.status_code == 201
    assert r2.json()["catalog_size"] == base_total - 1

    r3 = client.get(f"/api/admin/sites/{sample_name}", headers=_hdr(admin_token))
    assert r3.status_code == 404


def test_admin_remove_override_reverts_site(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/admin/sites", headers=_hdr(admin_token))
    sample_name = r.json()["sites"][0]["name"]

    # disable it
    client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={"name": sample_name, "operation": "disable", "patch": {}},
    )
    # then remove the override
    r2 = client.delete(f"/api/admin/sites/{sample_name}", headers=_hdr(admin_token))
    assert r2.status_code == 200

    r3 = client.get(f"/api/admin/sites/{sample_name}", headers=_hdr(admin_token))
    assert r3.json()["disabled"] is False
    assert r3.json()["source"] == "base"


def test_admin_reload_endpoint(client: TestClient, admin_token: str) -> None:
    r = client.post("/api/admin/sites/reload", headers=_hdr(admin_token))
    assert r.status_code == 200
    assert r.json()["sites"] > 100


def test_admin_upsert_invalid_operation_rejected(
    client: TestClient, admin_token: str
) -> None:
    r = client.post(
        "/api/admin/sites",
        headers=_hdr(admin_token),
        json={"name": "X", "operation": "blow-up", "patch": {}},
    )
    assert r.status_code == 422  # pydantic validator rejects
