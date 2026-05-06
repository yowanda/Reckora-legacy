"""HTTP-level smoke tests for the public + auth-gated endpoints."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Point the DB and admin seed at a clean location BEFORE importing the app, so
# auth.init_db() / seed_admin_from_env() pick up these values on FastAPI startup.
TEST_DB = Path("/tmp/cacadoxx-test.db")
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["CACADOXX_DB_PATH"] = str(TEST_DB)
os.environ["CACADOXX_ADMIN_USERNAME"] = "rootadmin"
os.environ["CACADOXX_ADMIN_PASSWORD"] = "rootpass1"
os.environ["CACADOXX_LOGIN_RATE_LIMIT"] = "1000/minute"
os.environ["CACADOXX_SEARCH_RATE_LIMIT"] = "1000/minute"

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, username: str, password: str) -> str:
    """Return the bearer token after a successful login.

    We use the token instead of cookies because TestClient runs over plain HTTP and
    the cookie is set with `Secure`, which browsers (and httpx) do not send over HTTP.
    """
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return _login(client, "rootadmin", "rootpass1")


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sites"] > 0


def test_sites_requires_auth(client: TestClient) -> None:
    r = client.get("/api/sites")
    assert r.status_code == 401


def test_search_requires_auth(client: TestClient) -> None:
    r = client.post("/api/search", json={"username": "abc"})
    assert r.status_code == 401


def test_login_bad_password(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "rootadmin", "password": "x"})
    assert r.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "nope", "password": "rootpass1"})
    assert r.status_code == 401


def test_login_returns_token_and_me_works(client: TestClient, admin_token: str) -> None:
    r = client.get("/api/auth/me", headers=_hdr(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["member"]["username"] == "rootadmin"
    assert body["member"]["role"] == "admin"


def test_logged_in_can_search(client: TestClient, admin_token: str) -> None:
    r = client.post(
        "/api/search",
        json={"username": "alice", "limit": 1},
        headers=_hdr(admin_token),
    )
    assert r.status_code == 200


def test_search_validates_username(client: TestClient, admin_token: str) -> None:
    r = client.post(
        "/api/search",
        json={"username": "bad name"},
        headers=_hdr(admin_token),
    )
    assert r.status_code == 422


def test_admin_member_crud(client: TestClient, admin_token: str) -> None:
    headers = _hdr(admin_token)

    # Create
    r = client.post(
        "/api/admin/members",
        json={"username": "bob", "password": "bobpass1", "role": "member"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["member"]["username"] == "bob"

    # Conflict on duplicate
    r = client.post(
        "/api/admin/members",
        json={"username": "bob", "password": "bobpass2", "role": "member"},
        headers=headers,
    )
    assert r.status_code == 409

    # List shows it
    r = client.get("/api/admin/members", headers=headers)
    assert r.status_code == 200
    names = [m["username"] for m in r.json()["members"]]
    assert "bob" in names

    # Reset password
    r = client.patch(
        "/api/admin/members/bob",
        json={"password": "bobpass3"},
        headers=headers,
    )
    assert r.status_code == 200

    # Old password fails, new one works
    r = client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "bob", "password": "bobpass3"})
    assert r.status_code == 200

    # Delete
    r = client.delete("/api/admin/members/bob", headers=headers)
    assert r.status_code == 200


def test_member_cannot_access_admin_endpoints(client: TestClient, admin_token: str) -> None:
    admin_h = _hdr(admin_token)
    # admin creates carol
    r = client.post(
        "/api/admin/members",
        json={"username": "carol", "password": "carolpass", "role": "member"},
        headers=admin_h,
    )
    assert r.status_code == 201, r.text

    # carol logs in
    carol_token = _login(client, "carol", "carolpass")
    carol_h = _hdr(carol_token)

    # carol cannot list members
    r = client.get("/api/admin/members", headers=carol_h)
    assert r.status_code == 403

    # carol cannot create members
    r = client.post(
        "/api/admin/members",
        json={"username": "evil", "password": "evilpass1", "role": "admin"},
        headers=carol_h,
    )
    assert r.status_code == 403

    # carol cannot reset her own password (gated by admin role — by design)
    r = client.patch(
        "/api/admin/members/carol",
        json={"password": "newpw1"},
        headers=carol_h,
    )
    assert r.status_code == 403


def test_cannot_delete_self_admin(client: TestClient, admin_token: str) -> None:
    r = client.delete("/api/admin/members/rootadmin", headers=_hdr(admin_token))
    assert r.status_code == 400


def test_logout_clears_cookie(client: TestClient, admin_token: str) -> None:
    # Logout currently only clears the cookie; bearer-token sessions remain valid until exp.
    r = client.post("/api/auth/logout", headers=_hdr(admin_token))
    assert r.status_code == 200
    # the cookie should be set with an empty value
    assert "cacadoxx_session" in r.headers.get("set-cookie", "")
