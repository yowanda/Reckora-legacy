"""Endpoint tests for Phase 7d — ``/api/admin/ml/{status,retrain}``."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import ml as ml_mod
from app import ml_retrain as retrain_mod
from app.main import app


def _safe_clear_overrides() -> None:
    with contextlib.suppress(Exception):
        from app.auth import db_connection

        with db_connection() as conn:
            conn.execute("DELETE FROM admin_settings")
    with contextlib.suppress(Exception):
        admin_settings_mod._invalidate_cache()


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
    return cast(str, r.json()["token"])


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    retrain_mod.reset_status()
    ml_mod.reset_recent_features_for_tests()
    _safe_clear_overrides()
    yield
    retrain_mod.reset_status()
    ml_mod.reset_recent_features_for_tests()
    _safe_clear_overrides()


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


class TestAuth:
    def test_status_requires_admin(self, client: TestClient) -> None:
        # No token at all -> 401.
        assert client.get("/api/admin/ml/status").status_code == 401

    def test_retrain_requires_admin(self, client: TestClient) -> None:
        assert client.post("/api/admin/ml/retrain").status_code == 401


class TestStatus:
    def test_returns_drift_envelope(self, client: TestClient, admin_token: str) -> None:
        r = client.get("/api/admin/ml/status", headers=_hdr(admin_token))
        assert r.status_code == 200, r.text
        body = r.json()
        # Required keys.
        assert "model_age_days" in body
        assert "last_retrain" in body
        drift = body["drift"]
        assert "any_alarm" in drift
        assert "threshold" in drift
        assert "alarm_features" in drift
        assert "features" in drift
        # No retrain has happened yet -> last_retrain is None.
        assert body["last_retrain"] is None

    def test_threshold_pulled_from_admin_knob(self, client: TestClient, admin_token: str) -> None:
        admin_settings_mod.set_override("ml_drift_ks_threshold_pct", 25, actor="rootadmin")
        r = client.get("/api/admin/ml/status", headers=_hdr(admin_token))
        assert r.status_code == 200, r.text
        # 25% -> 0.25 in the runtime gate.
        assert r.json()["drift"]["threshold"] == pytest.approx(0.25)


class TestRetrain:
    def test_endpoint_persists_status(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub the trainer so the test doesn't actually subprocess
        # scripts/train_ml.py; we just want to verify the wiring.
        monkeypatch.setattr(
            retrain_mod,
            "_default_trainer",
            lambda: (True, "stubbed retrain ok\n", None),
        )
        r = client.post("/api/admin/ml/retrain", headers=_hdr(admin_token))
        assert r.status_code == 200, r.text
        body = r.json()
        status = body["status"]
        assert status["ok"] is True
        assert status["triggered_by"] == "rootadmin"
        assert "stubbed retrain ok" in status["log"]
        # Status visible to a follow-up GET /api/admin/ml/status.
        r2 = client.get("/api/admin/ml/status", headers=_hdr(admin_token))
        assert r2.status_code == 200
        last = r2.json()["last_retrain"]
        assert last is not None
        assert last["ok"] is True
        assert last["triggered_by"] == "rootadmin"

    def test_failed_retrain_surfaces_error(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            retrain_mod,
            "_default_trainer",
            lambda: (False, "boom\n", "trainer exited 1"),
        )
        r = client.post("/api/admin/ml/retrain", headers=_hdr(admin_token))
        assert r.status_code == 200, r.text
        status = r.json()["status"]
        assert status["ok"] is False
        assert status["error"] == "trainer exited 1"
