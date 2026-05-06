"""Phase 5e: outbound proxy gate (env-configured URL + admin toggle)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import proxy as proxy_mod
from app.config import settings
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


@pytest.fixture
def reset_proxy_state(client: TestClient) -> Iterator[None]:
    """Snapshot + restore the proxy configuration around each test.

    The toggle lives in admin_settings (DB), the URL lives in
    ``settings.proxy_url`` (process-static), and the cache for the
    toggle is keyed by ``effective_int``. We restore all three after
    the test.
    """
    saved_url = settings.proxy_url
    saved_override = admin_settings_mod.get_overrides().get("proxy_enabled")
    try:
        admin_settings_mod.delete_override("proxy_enabled")
        yield
    finally:
        settings.proxy_url = saved_url  # type: ignore[misc]
        if saved_override is not None:
            admin_settings_mod.set_override(
                "proxy_enabled", saved_override, actor="test-fixture"
            )
        else:
            admin_settings_mod.delete_override("proxy_enabled")


# ---------------------------------------------------------------------------
# proxy_url_or_none()
# ---------------------------------------------------------------------------


def test_proxy_url_none_when_url_blank(reset_proxy_state: None) -> None:
    settings.proxy_url = ""  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 1, actor="test")
    assert proxy_mod.proxy_url_or_none() is None


def test_proxy_url_none_when_toggle_off(reset_proxy_state: None) -> None:
    settings.proxy_url = "socks5://127.0.0.1:9050"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 0, actor="test")
    assert proxy_mod.proxy_url_or_none() is None


def test_proxy_url_returned_when_both_set(reset_proxy_state: None) -> None:
    settings.proxy_url = "socks5://127.0.0.1:9050"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 1, actor="test")
    assert proxy_mod.proxy_url_or_none() == "socks5://127.0.0.1:9050"


# ---------------------------------------------------------------------------
# status() — reported via /api/health and /api/settings.
# ---------------------------------------------------------------------------


def test_status_reports_scheme_without_leaking_url(
    reset_proxy_state: None,
) -> None:
    settings.proxy_url = "socks5h://user:pw@10.0.0.5:1080"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 1, actor="test")
    s = proxy_mod.status()
    assert s == {
        "configured": True,
        "enabled": True,
        "active": True,
        "scheme": "socks5h",
    }
    # The URL itself must not appear anywhere in the descriptor.
    assert "10.0.0.5" not in repr(s)
    assert "user:pw" not in repr(s)


def test_status_active_false_when_toggle_off(reset_proxy_state: None) -> None:
    settings.proxy_url = "http://corp-proxy.internal:3128"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 0, actor="test")
    s = proxy_mod.status()
    assert s["configured"] is True
    assert s["enabled"] is False
    assert s["active"] is False
    assert s["scheme"] == "http"


def test_status_blank_when_unset(reset_proxy_state: None) -> None:
    settings.proxy_url = ""  # type: ignore[misc]
    s = proxy_mod.status()
    assert s == {
        "configured": False,
        "enabled": False,
        "active": False,
        "scheme": "",
    }


# ---------------------------------------------------------------------------
# /api/health and /api/settings reflect proxy state.
# ---------------------------------------------------------------------------


def test_health_carries_proxy_descriptor(
    client: TestClient,
    reset_proxy_state: None,
) -> None:
    settings.proxy_url = "socks5://tor:9050"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 1, actor="test")
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "proxy" in body
    assert body["proxy"]["scheme"] == "socks5"
    assert body["proxy"]["active"] is True
    # URL never leaks here either.
    assert "tor:9050" not in r.text


def test_settings_carries_proxy_descriptor(
    client: TestClient,
    admin_token: str,
    reset_proxy_state: None,
) -> None:
    settings.proxy_url = "http://corp:3128"  # type: ignore[misc]
    admin_settings_mod.set_override("proxy_enabled", 1, actor="test")
    r = client.get("/api/settings", headers=_hdr(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["proxy"]["scheme"] == "http"
    assert body["proxy"]["active"] is True


# ---------------------------------------------------------------------------
# proxy_enabled is now a knob — admin can flip it via PATCH.
# ---------------------------------------------------------------------------


def test_admin_can_flip_proxy_enabled_via_http(
    client: TestClient,
    admin_token: str,
    reset_proxy_state: None,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"proxy_enabled": 1},
    )
    assert r.status_code == 200
    knobs = {k["key"]: k for k in r.json()["knobs"]}
    assert knobs["proxy_enabled"]["override"] == 1
    assert knobs["proxy_enabled"]["effective"] == 1


def test_admin_proxy_enabled_range_clamped(
    client: TestClient,
    admin_token: str,
    reset_proxy_state: None,
) -> None:
    """proxy_enabled is a 0/1 toggle; anything else is rejected."""
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"proxy_enabled": 2},
    )
    assert r.status_code == 400
