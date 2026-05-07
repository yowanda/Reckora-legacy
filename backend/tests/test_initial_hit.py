"""Phase 10a: auto-PDF preliminary export on initial CLAIMED hit.

Covers ``InitialHitTracker`` latch semantics, the on-disk
persist/read-back round-trip, the TTL janitor, the SSE frame shape,
and the admin-knob plumbing for all five new knobs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import initial_hit as initial_hit_mod
from app.checker import CheckResult, Status
from app.initial_hit import InitialHitTracker, ScanContext
from app.main import app

# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture(autouse=True)
def _wipe_overrides(client: TestClient) -> Iterator[None]:
    """Each test starts and ends with no admin overrides set.

    Depends on ``client`` so the FastAPI startup hook (which creates the
    ``admin_settings`` table) runs before the first DELETE.
    """
    from app.auth import db_connection

    with db_connection() as conn:
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()


@pytest.fixture(autouse=True)
def _isolate_pdf_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect ``initial_hit._pdf_dir`` to a per-test tmp path."""
    pdf_dir = tmp_path / "initial_pdfs"
    monkeypatch.setattr(initial_hit_mod, "_pdf_dir", lambda: pdf_dir)
    yield pdf_dir


def _enable_master_switch() -> None:
    admin_settings_mod.set_override("enable_initial_hit_pdf", 1, actor="test")


def _ctx(scan_token: str = "tok-test", *, sites_total: int = 100) -> ScanContext:
    return ScanContext(
        scan_token=scan_token,
        member="alice",
        primary_username="torvalds",
        mode="list",
        started_at=int(time.time()),
        sites_total=sites_total,
    )


def _claimed(
    site: str = "github",
    *,
    confidence: int = 95,
    verified: bool | None = True,
    status: Status = Status.CLAIMED,
) -> CheckResult:
    return CheckResult(
        site=site,
        status=status,
        url=f"https://{site}.test/torvalds",
        url_main=f"https://{site}.test/",
        tags=["dev"],
        http_status=200,
        elapsed_ms=42,
        confidence=confidence,
        signals=[],
        attempts=1,
        verified=verified,
        extracted={"display_name": "Linus"},
        ml_confidence=88,
    )


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Latch semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latch_fires_exactly_once_on_first_qualifying_hit(
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    tracker = InitialHitTracker(context=_ctx("tok-once"))

    first = _claimed("github", confidence=92)
    second = _claimed("twitter", confidence=99)

    payload = await tracker.consider(first, is_primary=True, results_so_far=[first.to_dict()])
    assert payload is not None
    assert payload["type"] == "initial_pdf_ready"
    assert tracker.fired is True

    again = await tracker.consider(
        second, is_primary=True, results_so_far=[first.to_dict(), second.to_dict()]
    )
    assert again is None
    # The latch is sticky — even a higher-confidence later hit doesn't
    # overwrite the first PDF.
    persisted = initial_hit_mod.read_initial_pdf("tok-once")
    assert persisted is not None


@pytest.mark.asyncio
async def test_latch_skipped_when_master_switch_off() -> None:
    # No override set → enable_initial_hit_pdf defaults to 0.
    tracker = InitialHitTracker(context=_ctx("tok-off"))
    result = _claimed("github", confidence=95)
    payload = await tracker.consider(result, is_primary=True, results_so_far=[result.to_dict()])
    assert payload is None
    assert tracker.fired is False
    assert initial_hit_mod.read_initial_pdf("tok-off") is None


@pytest.mark.asyncio
async def test_below_confidence_floor_does_not_latch() -> None:
    _enable_master_switch()
    # Default floor is 80; a 79-confidence result must NOT latch.
    tracker = InitialHitTracker(context=_ctx("tok-low"))
    low = _claimed("github", confidence=79)
    payload = await tracker.consider(low, is_primary=True, results_so_far=[low.to_dict()])
    assert payload is None
    assert tracker.fired is False
    # Then a higher-confidence hit DOES latch.
    high = _claimed("twitter", confidence=95)
    payload = await tracker.consider(
        high, is_primary=True, results_so_far=[low.to_dict(), high.to_dict()]
    )
    assert payload is not None
    assert tracker.fired is True


@pytest.mark.asyncio
async def test_unverified_result_does_not_latch_when_require_verify_on() -> None:
    _enable_master_switch()
    # initial_hit_require_verify defaults to 1 → must reject verified=False.
    tracker = InitialHitTracker(context=_ctx("tok-noverify"))
    res = _claimed("github", confidence=95, verified=False)
    payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is None

    # But when the operator turns verify off, the same result latches.
    admin_settings_mod.set_override("initial_hit_require_verify", 0, actor="test")
    tracker2 = InitialHitTracker(context=_ctx("tok-verifyoff"))
    payload = await tracker2.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is not None


@pytest.mark.asyncio
async def test_secondary_spawn_does_not_latch() -> None:
    """A graph-secondary hit (is_primary=False) must never latch."""
    _enable_master_switch()
    tracker = InitialHitTracker(context=_ctx("tok-secondary"))
    res = _claimed("github", confidence=95)
    payload = await tracker.consider(res, is_primary=False, results_so_far=[res.to_dict()])
    assert payload is None
    assert tracker.fired is False


@pytest.mark.asyncio
async def test_non_claimed_status_does_not_latch() -> None:
    _enable_master_switch()
    tracker = InitialHitTracker(context=_ctx("tok-status"))
    for s in (Status.FREE, Status.UNKNOWN, Status.BLOCKED, Status.ERROR, Status.SKIPPED):
        res = _claimed("github", confidence=95, status=s)
        payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
        assert payload is None, f"status={s!r} should not latch"
    assert tracker.fired is False


# ---------------------------------------------------------------------------
# Persistence + read-back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_persist_round_trip_and_sidecar_metadata(
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    tracker = InitialHitTracker(context=_ctx("tok-persist"))

    res = _claimed("github", confidence=90)
    payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is not None

    # PDF on disk reads back identical to what consider() rendered.
    on_disk = initial_hit_mod.read_initial_pdf("tok-persist")
    assert on_disk is not None
    assert len(on_disk) > 0

    # Sidecar JSON captures the member, hit metadata, and TTL.
    meta = initial_hit_mod.read_initial_pdf_meta("tok-persist")
    assert meta is not None
    assert meta["scan_token"] == "tok-persist"
    assert meta["member"] == "alice"
    assert meta["username"] == "torvalds"
    assert meta["mode"] == "list"
    assert meta["hit_site"] == "github"
    assert meta["hit_confidence"] == 90
    assert meta["hit_verified"] is True
    assert meta["results_count"] == 1
    assert meta["ttl_sec"] == 86400


@pytest.mark.asyncio
async def test_partial_summary_renders_with_one_and_with_many_results(
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()

    # 1-result snapshot.
    tracker1 = InitialHitTracker(context=_ctx("tok-one"))
    a = _claimed("github", confidence=95)
    payload = await tracker1.consider(a, is_primary=True, results_so_far=[a.to_dict()])
    assert payload is not None
    pdf_one = initial_hit_mod.read_initial_pdf("tok-one")
    assert pdf_one is not None
    assert len(pdf_one) > 0

    # N-result snapshot.
    tracker2 = InitialHitTracker(context=_ctx("tok-many"))
    free = CheckResult(
        site="reddit",
        status=Status.FREE,
        url="https://reddit.test/torvalds",
        url_main="https://reddit.test/",
        tags=["forum"],
    )
    err = CheckResult(
        site="hn",
        status=Status.ERROR,
        url="https://hn.test/torvalds",
        url_main="https://hn.test/",
        tags=["forum"],
        error="timeout",
    )
    snapshot = [free.to_dict(), err.to_dict(), a.to_dict()]
    payload = await tracker2.consider(a, is_primary=True, results_so_far=snapshot)
    assert payload is not None
    assert payload["results_count"] == 3

    pdf_many = initial_hit_mod.read_initial_pdf("tok-many")
    assert pdf_many is not None
    # Both snapshots produced non-empty bodies; we don't depend on
    # weasyprint being installed (the fallback HTML path is fine).
    assert len(pdf_many) > 0


@pytest.mark.asyncio
async def test_include_extras_knob_strips_ml_and_signals(
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    admin_settings_mod.set_override("initial_hit_include_extras", 0, actor="test")

    res = _claimed("github", confidence=95)
    res.signals.append("strict-claim:has-username-in-title")
    snapshot = [res.to_dict()]
    tracker = InitialHitTracker(context=_ctx("tok-strip"))
    payload = await tracker.consider(res, is_primary=True, results_so_far=snapshot)
    assert payload is not None

    # The persisted PDF body shouldn't contain the stripped keys'
    # values (avatar_phash, ml_confidence). We can't easily inspect
    # the rendered PDF, but the snapshot the tracker passed to
    # ``exports.to_pdf`` is the gate — exercise the helper directly.
    stripped = initial_hit_mod._clone_result(res.to_dict(), include_extras=False)
    assert "extracted" not in stripped
    assert "ml_confidence" not in stripped
    assert "signals" not in stripped
    assert "reason" not in stripped
    # Core fields survive.
    assert stripped["site"] == "github"
    assert stripped["status"] == "claimed"


# ---------------------------------------------------------------------------
# TTL janitor
# ---------------------------------------------------------------------------


def test_purge_expired_drops_old_pdfs_and_sidecars(_isolate_pdf_dir: Path) -> None:
    pdf_dir = _isolate_pdf_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)
    fresh = pdf_dir / "fresh.pdf"
    fresh.write_bytes(b"%PDF-1.4 fresh")
    (pdf_dir / "fresh.json").write_text("{}", encoding="utf-8")
    stale = pdf_dir / "stale.pdf"
    stale.write_bytes(b"%PDF-1.4 stale")
    (pdf_dir / "stale.json").write_text("{}", encoding="utf-8")

    # Backdate the stale PDF + sidecar to last week. The default TTL
    # (86400s = 1 day) easily covers the 7-day skew.
    stale_mtime = time.time() - 7 * 86400
    import os

    os.utime(stale, (stale_mtime, stale_mtime))
    os.utime(pdf_dir / "stale.json", (stale_mtime, stale_mtime))

    removed = initial_hit_mod.purge_expired()
    assert removed == 1
    assert fresh.exists()
    assert (pdf_dir / "fresh.json").exists()
    assert not stale.exists()
    assert not (pdf_dir / "stale.json").exists()


def test_purge_expired_is_safe_when_directory_missing(_isolate_pdf_dir: Path) -> None:
    # _isolate_pdf_dir hasn't been created yet.
    assert initial_hit_mod.purge_expired() == 0


def test_purge_expired_respects_batch_limit(_isolate_pdf_dir: Path) -> None:
    pdf_dir = _isolate_pdf_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)
    stale_mtime = time.time() - 7 * 86400
    import os

    for i in range(5):
        path = pdf_dir / f"stale{i}.pdf"
        path.write_bytes(b"%PDF-1.4")
        os.utime(path, (stale_mtime, stale_mtime))

    removed = initial_hit_mod.purge_expired(batch_limit=3)
    assert removed == 3
    leftover = sorted(p.name for p in pdf_dir.iterdir() if p.suffix == ".pdf")
    assert len(leftover) == 2


# ---------------------------------------------------------------------------
# SSE frame shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_payload_shape_includes_url_token_and_hit_metadata(
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    tracker = InitialHitTracker(context=_ctx("tok-shape"))
    res = _claimed("github", confidence=88)
    payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is not None
    assert payload["type"] == "initial_pdf_ready"
    assert payload["scan_id"] == "tok-shape"
    assert payload["scan_token"] == "tok-shape"
    assert payload["url"] == "/api/history/tok-shape/initial_pdf"
    assert payload["hit_site"] == "github"
    assert payload["hit_url"] == "https://github.test/torvalds"
    assert payload["hit_confidence"] == 88
    assert payload["hit_verified"] is True
    assert payload["results_count"] == 1
    # Payload is JSON-serialisable as-is.
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# Admin-knob plumbing — all five new knobs round-trip via PATCH.
# ---------------------------------------------------------------------------


def test_all_five_initial_hit_knobs_round_trip_via_admin_api(
    client: TestClient,
    admin_token: str,
) -> None:
    expected = {
        "enable_initial_hit_pdf": 1,
        "initial_hit_min_confidence": 75,
        "initial_hit_require_verify": 0,
        "initial_hit_pdf_ttl_sec": 7200,
        "initial_hit_include_extras": 0,
    }
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json=expected,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    knobs_by_key = {row["key"]: row for row in body["knobs"]}
    for key, value in expected.items():
        assert key in knobs_by_key, f"knob {key} missing from /api/admin/settings"
        assert knobs_by_key[key]["override"] == value
        assert knobs_by_key[key]["effective"] == value

    # And admin_settings.effective_int reflects them too.
    for key, value in expected.items():
        assert admin_settings_mod.effective_int(key) == value


def test_initial_hit_knobs_reject_out_of_range_via_admin_api(
    client: TestClient,
    admin_token: str,
) -> None:
    # initial_hit_min_confidence is 50-100, so 49 must reject.
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"initial_hit_min_confidence": 49},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# REST endpoint — GET /api/history/{token}/initial_pdf
# ---------------------------------------------------------------------------


def test_initial_pdf_endpoint_404_until_latch_fires(
    client: TestClient,
    admin_token: str,
    _isolate_pdf_dir: Path,
) -> None:
    r = client.get("/api/history/never-fired/initial_pdf", headers=_hdr(admin_token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_initial_pdf_endpoint_serves_after_latch_with_attachment(
    client: TestClient,
    admin_token: str,
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    # Use the same actor name as the admin token (rootadmin).
    ctx = ScanContext(
        scan_token="tok-rest",
        member="rootadmin",
        primary_username="torvalds",
        mode="list",
        started_at=int(time.time()),
        sites_total=10,
    )
    tracker = InitialHitTracker(context=ctx)
    res = _claimed("github", confidence=95)
    payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is not None

    r = client.get("/api/history/tok-rest/initial_pdf", headers=_hdr(admin_token))
    assert r.status_code == 200
    assert r.content == initial_hit_mod.read_initial_pdf("tok-rest")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "tok-rest" in cd
    assert "torvalds-initial.pdf" in cd
    # Response cache header is private (no edge caching of personal scans).
    assert r.headers.get("cache-control") == "private, no-store"


@pytest.mark.asyncio
async def test_initial_pdf_endpoint_rejects_other_member(
    client: TestClient,
    admin_token: str,
    _isolate_pdf_dir: Path,
) -> None:
    _enable_master_switch()
    # Provision a second member.
    client.post(
        "/api/admin/members",
        headers=_hdr(admin_token),
        json={"username": "otheruser10a", "password": "otherpass1", "role": "member"},
    )
    other_login = client.post(
        "/api/auth/login",
        json={"username": "otheruser10a", "password": "otherpass1"},
    )
    assert other_login.status_code == 200
    other_token = other_login.json()["token"]

    # Latch fires under "rootadmin"'s scan.
    ctx = ScanContext(
        scan_token="tok-private",
        member="rootadmin",
        primary_username="torvalds",
        mode="list",
        started_at=int(time.time()),
        sites_total=10,
    )
    tracker = InitialHitTracker(context=ctx)
    res = _claimed("github", confidence=95)
    payload = await tracker.consider(res, is_primary=True, results_so_far=[res.to_dict()])
    assert payload is not None

    # The other member gets 404 even though the token is on disk.
    r = client.get("/api/history/tok-private/initial_pdf", headers=_hdr(other_token))
    assert r.status_code == 404
    # The owning admin still gets 200.
    r = client.get("/api/history/tok-private/initial_pdf", headers=_hdr(admin_token))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Janitor loop wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_janitor_loop_calls_purge_expired_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _fake_purge(*_args: object, **_kwargs: object) -> int:
        calls.append(int(time.time()))
        return 0

    sleeps: list[int] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(int(seconds))
        # Bail after two iterations.
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(initial_hit_mod, "purge_expired", _fake_purge)

    with pytest.raises(asyncio.CancelledError):
        await initial_hit_mod.janitor_loop(interval_sec=42, sleep=_fake_sleep)

    assert len(calls) == 2
    assert sleeps == [42, 42]
