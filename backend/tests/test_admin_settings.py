"""Phase 5d: admin_settings runtime-knob storage + HTTP endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
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


@pytest.fixture(scope="module")
def member_token(client: TestClient, admin_token: str) -> str:
    # Create a non-admin member once per module.
    client.post(
        "/api/admin/members",
        headers=_hdr(admin_token),
        json={"username": "member5d", "password": "memberpass1", "role": "member"},
    )
    r = client.post(
        "/api/auth/login",
        json={"username": "member5d", "password": "memberpass1"},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _wipe_overrides() -> Iterator[None]:
    """Each test starts and ends with no admin overrides set."""
    from app.auth import db_connection

    with db_connection() as conn:
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()


# ---------------------------------------------------------------------------
# Module-level (storage layer).
# ---------------------------------------------------------------------------


def test_effective_int_falls_back_to_settings_default(client: TestClient) -> None:
    # Default ``graph_max_secondary`` from ``Settings`` is 5.
    assert admin_settings_mod.effective_int("graph_max_secondary") == 5


def test_set_override_round_trip(client: TestClient) -> None:
    admin_settings_mod.set_override("graph_max_secondary", 12, actor="rootadmin")
    assert admin_settings_mod.effective_int("graph_max_secondary") == 12
    overrides = admin_settings_mod.get_overrides()
    assert overrides == {"graph_max_secondary": 12}


def test_set_override_rejects_unknown_keys(client: TestClient) -> None:
    with pytest.raises(admin_settings_mod.UnknownKnob):
        admin_settings_mod.set_override("not_a_real_knob", 1, actor="rootadmin")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("graph_max_depth", -1),
        ("graph_max_depth", 5),
        ("graph_max_secondary", 0),
        ("graph_max_secondary", 99),
        ("extract_min_confidence", 200),
        ("extract_min_confidence", -10),
    ],
)
def test_set_override_rejects_out_of_range(client: TestClient, key: str, value: int) -> None:
    with pytest.raises(admin_settings_mod.KnobOutOfRange):
        admin_settings_mod.set_override(key, value, actor="rootadmin")


def test_delete_override_removes_row(client: TestClient) -> None:
    admin_settings_mod.set_override("extract_min_confidence", 30, actor="rootadmin")
    assert admin_settings_mod.delete_override("extract_min_confidence") is True
    assert admin_settings_mod.effective_int("extract_min_confidence") == 70  # default


def test_delete_unknown_key_returns_false(client: TestClient) -> None:
    assert admin_settings_mod.delete_override("not_a_real_knob") is False


@pytest.mark.parametrize(
    "key,initial,override,bad",
    [
        ("min_claimed_confidence", 60, 75, 200),
        ("min_claimed_ml_confidence", 50, 65, -5),
        ("require_verify_for_claim", 0, 1, 2),
    ],
)
def test_phase_5h_floor_knobs_round_trip(
    client: TestClient, key: str, initial: int, override: int, bad: int
) -> None:
    """Phase 5h Layer A+B+C — every claimed-precision-floor knob must
    expose the standard set/get/reset/range behaviour so an operator
    can tune precision live without a redeploy."""
    # Fresh defaults.
    assert admin_settings_mod.effective_int(key) == initial
    # Set + read-back.
    admin_settings_mod.set_override(key, override, actor="rootadmin")
    assert admin_settings_mod.effective_int(key) == override
    # Out-of-range upserts are rejected.
    with pytest.raises(admin_settings_mod.KnobOutOfRange):
        admin_settings_mod.set_override(key, bad, actor="rootadmin")
    # Reset returns to default.
    assert admin_settings_mod.delete_override(key) is True
    assert admin_settings_mod.effective_int(key) == initial


def test_describe_includes_default_and_override(client: TestClient) -> None:
    admin_settings_mod.set_override("graph_max_seed_hits", 12, actor="rootadmin")
    out = {row["key"]: row for row in admin_settings_mod.describe()}
    seed = out["graph_max_seed_hits"]
    assert seed["default"] == 8
    assert seed["override"] == 12
    assert seed["effective"] == 12
    secondary = out["graph_max_secondary"]
    assert secondary["override"] is None
    assert secondary["effective"] == secondary["default"]


# ---------------------------------------------------------------------------
# HTTP layer.
# ---------------------------------------------------------------------------


def test_public_settings_returns_effective_map(
    client: TestClient,
    member_token: str,
) -> None:
    r = client.get("/api/settings", headers=_hdr(member_token))
    assert r.status_code == 200
    data = r.json()["settings"]
    # All allowlisted knobs are present (4 graph/extract + 3 Phase 5h floors).
    assert set(data) == set(admin_settings_mod.ALLOWED_KEYS)
    # Defaults match Settings.
    assert data["graph_max_depth"] == 1
    assert data["graph_max_secondary"] == 5
    assert data["graph_max_seed_hits"] == 8
    assert data["extract_min_confidence"] == 70
    # Phase 5h Layer A+B+C — claimed precision floor defaults.
    assert data["min_claimed_confidence"] == 60
    assert data["min_claimed_ml_confidence"] == 50
    assert data["require_verify_for_claim"] == 0
    # Phase 5h Layer D — link-liveness probe enabled by default.
    assert data["enable_link_liveness"] == 1


def test_public_settings_requires_auth(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_admin_get_settings_describes_all_knobs(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.get("/api/admin/settings", headers=_hdr(admin_token))
    assert r.status_code == 200
    knobs = {k["key"]: k for k in r.json()["knobs"]}
    assert set(knobs) == set(admin_settings_mod.ALLOWED_KEYS)
    depth = knobs["graph_max_depth"]
    assert depth["minimum"] == 0
    # Phase 10d bumped the maximum from 1 to 3 to unlock depth=2/3.
    assert depth["maximum"] == 3
    assert depth["override"] is None


def test_admin_get_settings_forbidden_for_member(
    client: TestClient,
    member_token: str,
) -> None:
    r = client.get("/api/admin/settings", headers=_hdr(member_token))
    assert r.status_code == 403


def test_admin_patch_sets_and_resets_knobs(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"graph_max_secondary": 10, "extract_min_confidence": 50},
    )
    assert r.status_code == 200
    knobs = {k["key"]: k for k in r.json()["knobs"]}
    assert knobs["graph_max_secondary"]["effective"] == 10
    assert knobs["extract_min_confidence"]["effective"] == 50

    # Reset one back to default.
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"reset": ["extract_min_confidence"]},
    )
    assert r.status_code == 200
    knobs = {k["key"]: k for k in r.json()["knobs"]}
    assert knobs["extract_min_confidence"]["override"] is None
    assert knobs["extract_min_confidence"]["effective"] == 70
    # The other knob is untouched by reset.
    assert knobs["graph_max_secondary"]["effective"] == 10


def test_admin_patch_rejects_out_of_range(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"graph_max_depth": 9},
    )
    assert r.status_code == 400
    assert "outside allowed range" in r.json()["detail"]


def test_admin_patch_rejects_unknown_extra_field(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"made_up_knob": 7},
    )
    assert r.status_code == 422  # pydantic strict (extra=forbid)


def test_admin_patch_model_exposes_every_registered_knob(
    client: TestClient,
    admin_token: str,
) -> None:
    """The PATCH body model must mirror ``admin_settings.KNOBS`` so a knob
    added to the registry is auto-flippable. Catches the regression where
    Phase 7-9 knobs landed in ``KNOBS`` but the request schema still only
    enumerated the original Phase 5d fields, silently 422-ing every
    PATCH against a newer knob."""
    from app.main import AdminSettingsPatch

    declared = set(AdminSettingsPatch.model_fields) - {"reset"}
    assert declared == set(admin_settings_mod.ALLOWED_KEYS), (
        "AdminSettingsPatch fields must match admin_settings.KNOBS exactly"
    )


@pytest.mark.parametrize("knob", sorted(admin_settings_mod.ALLOWED_KEYS))
def test_admin_patch_accepts_every_registered_knob_in_range(
    client: TestClient,
    admin_token: str,
    knob: str,
) -> None:
    """Round-trip every knob through PATCH at its current default value
    so a future addition to ``KNOBS`` that is NOT mirrored on the model
    fails this parametrized check loudly instead of just silently
    blocking PATCH on the live deployment."""
    spec = admin_settings_mod.KNOBS_BY_KEY[knob]
    # Pick a value safely inside the valid range — the current effective
    # value (== Settings default when no override is set) is always
    # in-range by construction.
    target = admin_settings_mod.effective_int(knob)
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={knob: target},
    )
    assert r.status_code == 200, f"PATCH {knob}={target} -> {r.status_code} {r.text}"
    knobs = {k["key"]: k for k in r.json()["knobs"]}
    assert knobs[knob]["effective"] == target
    # Also exercise the reset path so we leave no override behind for
    # the next test in the parametrized run.
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"reset": [knob]},
    )
    assert r.status_code == 200
    assert r.json()["knobs"][0]["key"] in admin_settings_mod.ALLOWED_KEYS
    # Range rejected: max+1 must always be out-of-range.
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={knob: spec.maximum + 1},
    )
    assert r.status_code == 400
    assert "outside allowed range" in r.json()["detail"]


def test_admin_patch_forbidden_for_member(
    client: TestClient,
    member_token: str,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(member_token),
        json={"graph_max_depth": 0},
    )
    assert r.status_code == 403


def test_admin_patch_reset_unknown_key(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"reset": ["not_a_real_knob"]},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Behaviour: effective values flow into the runtime.
# ---------------------------------------------------------------------------


def test_graph_endpoint_advertises_admin_capped_depth(
    client: TestClient,
    admin_token: str,
) -> None:
    """Setting graph_max_depth=0 collapses the graph endpoint's advertised
    depth even when the request asks for ``depth=1`` — the SSE ``start``
    event must reflect the cap so the SPA can disable secondary fetches."""
    admin_settings_mod.set_override("graph_max_depth", 0, actor="rootadmin")
    r = client.post(
        "/api/search/graph",
        headers=_hdr(admin_token),
        json={"username": "torvalds", "depth": 1, "limit": 1},
    )
    assert r.status_code == 200
    body = r.text
    # The first event line for ``start`` carries the effective depth.
    assert '"depth": 0' in body or '"depth":0' in body
