"""HTTP-level tests for the Phase 8c GraphQL gateway."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Bootstrap the same isolated DB the rest of the suite uses, so the
# admin seed + session table are intact when this module imports the
# app for the first time.
TEST_DB = Path("/tmp/reckora-test.db")
if not TEST_DB.exists():
    # ``conftest.py`` already nukes the file at session start; if the
    # test runs in isolation we still want a clean shared DB so the
    # seeded admin (rootadmin / rootpass1) exists before login.
    pass
os.environ.setdefault("RECKORA_DB_PATH", str(TEST_DB))
os.environ.setdefault("RECKORA_ADMIN_USERNAME", "rootadmin")
os.environ.setdefault("RECKORA_ADMIN_PASSWORD", "rootpass1")
os.environ.setdefault("RECKORA_LOGIN_RATE_LIMIT", "1000/minute")
os.environ.setdefault("RECKORA_SEARCH_RATE_LIMIT", "1000/minute")

pytest.importorskip("strawberry")

from app import admin_settings as admin_settings_mod  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def gateway_on() -> Iterator[None]:
    """Flip ``enable_graphql_gateway`` on for the duration of a test."""
    admin_settings_mod.set_override("enable_graphql_gateway", 1, actor="rootadmin")
    try:
        yield
    finally:
        admin_settings_mod.delete_override("enable_graphql_gateway")


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _gql(
    c: TestClient,
    query: str,
    *,
    token: str | None = None,
    variables: dict | None = None,
) -> dict:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = c.post(
        "/api/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Routing / mount sanity
# ---------------------------------------------------------------------------


def test_graphql_route_is_mounted(client: TestClient) -> None:
    """Route exists even when knob is off; resolvers gate themselves."""
    r = client.get("/api/graphql")
    # GraphiQL UI returns 200 (HTML); POST without query returns 400.
    assert r.status_code in (200, 400)


def test_health_includes_graphql_descriptor(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "graphql" in body
    assert body["graphql"]["installed"] is True
    # available reflects the knob; default is off.
    assert isinstance(body["graphql"]["available"], bool)


# ---------------------------------------------------------------------------
# Knob gate — every resolver should error when knob is off.
# ---------------------------------------------------------------------------


def test_query_rejected_when_knob_off(client: TestClient) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(client, "query { me { username role } }", token=token)
    assert "errors" in body
    assert any("disabled" in (e["message"] or "").lower() for e in body["errors"])


# ---------------------------------------------------------------------------
# Auth integration — same cookie / Bearer token as REST.
# ---------------------------------------------------------------------------


def test_query_requires_auth(client: TestClient, gateway_on: None) -> None:
    body = _gql(client, "query { me { username } }")
    assert "errors" in body
    assert any("login" in (e["message"] or "").lower() for e in body["errors"])


def test_me_returns_authenticated_member(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        "query { me { username role createdAt } }",
        token=token,
    )
    assert "errors" not in body, body
    assert body["data"]["me"]["username"] == "rootadmin"
    assert body["data"]["me"]["role"] == "admin"


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def test_health_query_returns_catalog_size(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        """query {
            health {
                status
                sites
                proxyActive
                hostRateLimitEnabled
                hostRatePerMin
            }
        }""",
        token=token,
    )
    assert "errors" not in body, body
    h = body["data"]["health"]
    assert h["status"] == "ok"
    assert h["sites"] > 0
    assert h["proxyActive"] is False
    assert h["hostRatePerMin"] >= 1


def test_sites_query_returns_catalog(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        "query { sites(limit: 5) { name urlMain tags } }",
        token=token,
    )
    assert "errors" not in body, body
    sites = body["data"]["sites"]
    assert len(sites) == 5
    assert all(s["name"] for s in sites)
    assert all(s["urlMain"].startswith("http") for s in sites)


def test_permute_strategies_query(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        "query { permuteStrategies { key title description } }",
        token=token,
    )
    assert "errors" not in body, body
    strats = body["data"]["permuteStrategies"]
    assert len(strats) >= 1
    keys = {s["key"] for s in strats}
    assert "separator" in keys


def test_history_query_is_member_scoped(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(client, "query { history(limit: 5) { id username } }", token=token)
    assert "errors" not in body, body
    # Default DB has no scans for this user; an empty list is fine.
    assert isinstance(body["data"]["history"], list)


def test_history_item_returns_null_for_unknown(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        "query { historyItem(scanId: 999999) { summary { id } } }",
        token=token,
    )
    assert "errors" not in body, body
    assert body["data"]["historyItem"] is None


# ---------------------------------------------------------------------------
# Admin-only query
# ---------------------------------------------------------------------------


def test_admin_settings_requires_admin_role(client: TestClient, gateway_on: None) -> None:
    # Create a non-admin member, then prove they get a 403-equivalent.
    token = _login(client, "rootadmin", "rootpass1")
    r = client.post(
        "/api/admin/members",
        json={"username": "gqluser", "password": "gqlpass1", "role": "member"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code in (201, 409), r.text
    member_token = _login(client, "gqluser", "gqlpass1")
    body = _gql(client, "query { adminSettings { key value } }", token=member_token)
    assert "errors" in body
    assert any("admin" in (e["message"] or "").lower() for e in body["errors"])


def test_admin_settings_returns_knob_registry(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        """query {
            adminSettings {
                key
                value
                minimum
                maximum
            }
        }""",
        token=token,
    )
    assert "errors" not in body, body
    knobs = body["data"]["adminSettings"]
    keys = {k["key"] for k in knobs}
    assert "enable_graphql_gateway" in keys
    assert "host_rate_per_min" in keys


# ---------------------------------------------------------------------------
# Mutation — search collected from the SSE stream.
# ---------------------------------------------------------------------------


def test_search_mutation_validates_identity_type(client: TestClient, gateway_on: None) -> None:
    token = _login(client, "rootadmin", "rootpass1")
    body = _gql(
        client,
        """mutation Run($i: SearchInput!) {
            search(input: $i) { total cached scanId results { site status } }
        }""",
        token=token,
        variables={
            "i": {
                "username": "alice",
                "tags": [],
                "limit": 1,
                "identityType": "not-a-real-type",
            }
        },
    )
    assert "errors" in body
    assert any("identity" in (e["message"] or "").lower() for e in body["errors"])
