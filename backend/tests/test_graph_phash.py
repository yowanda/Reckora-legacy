"""Phase 7a: ``phash_edge`` SSE-emission tests for /api/search/graph.

These ride on top of ``app.main`` (the same TestClient bootstrap as
``test_api.py``) but stub out ``stream_checks`` so we can hand the
graph endpoint a deterministic set of CLAIMED hits with prebaked
``avatar_phash`` fingerprints. End-to-end avatar fetching + dHash
computation is covered separately in ``test_imagephash.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Same env bootstrap as test_api.py; importing app builds the
# Settings singleton with these values so admin auth works.
TEST_DB = Path("/tmp/reckora-test.db")
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["RECKORA_DB_PATH"] = str(TEST_DB)
os.environ["RECKORA_ADMIN_USERNAME"] = "rootadmin"
os.environ["RECKORA_ADMIN_PASSWORD"] = "rootpass1"
os.environ["RECKORA_LOGIN_RATE_LIMIT"] = "1000/minute"
os.environ["RECKORA_SEARCH_RATE_LIMIT"] = "1000/minute"

from app import admin_settings as admin_settings_mod  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.checker import CheckResult, Status  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    r = client.post(
        "/api/auth/login",
        json={"username": "rootadmin", "password": "rootpass1"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _parse_sse(stream: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for chunk in stream.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        ev = "message"
        data_lines: list[str] = []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        events.append({"event": ev, "data": "\n".join(data_lines)})
    return events


def _claimed(site: str, *, phash: str | None) -> CheckResult:
    extracted: dict[str, str] = {}
    if phash is not None:
        extracted["avatar_phash"] = phash
    return CheckResult(
        site=site,
        status=Status.CLAIMED,
        url=f"https://{site}.example/alice",
        url_main=f"https://{site}.example/",
        tags=[],
        http_status=200,
        confidence=90,
        signals=[],
        extracted=extracted,
    )


def _stub_stream_checks(
    results_by_username: dict[str, list[CheckResult]],
):
    """Build a replacement for ``stream_checks`` that yields the
    pre-canned results for the username supplied to it. Honours the
    same call signature so the graph code can keep its kwargs."""

    async def _stub(
        sites,
        username: str,
        *,
        client=None,
        verify: bool = True,
        extract: bool = True,
        identity_type=None,
    ) -> AsyncIterator[CheckResult]:
        for r in results_by_username.get(username, []):
            yield r

    return _stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_phash_edge_emitted_when_two_hashes_within_threshold(
    client: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two CLAIMED hits with identical avatar phashes must yield one
    ``phash_edge`` event keyed on both site names."""
    main_mod.admin_settings_mod.set_override("enable_image_phash", 1, actor="test-7a")
    main_mod.admin_settings_mod.set_override("image_phash_max_distance", 6, actor="test-7a")
    monkeypatch.setattr(
        main_mod,
        "stream_checks",
        _stub_stream_checks(
            {
                "alice": [
                    _claimed("github", phash="0123456789abcdef"),
                    _claimed("twitter", phash="0123456789abcdef"),
                ],
            }
        ),
    )

    try:
        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 0, "limit": 2},
            headers=_hdr(admin_token),
        )
    finally:
        admin_settings_mod.delete_override("enable_image_phash")
        admin_settings_mod.delete_override("image_phash_max_distance")

    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    phash_events = [json.loads(e["data"]) for e in events if e["event"] == "phash_edge"]
    assert len(phash_events) == 1
    edge = phash_events[0]
    assert {edge["site_a"], edge["site_b"]} == {"github", "twitter"}
    assert edge["hamming"] == 0
    assert edge["max_distance"] == 6
    done = json.loads(events[-1]["data"])
    assert done["phash_edges"] == 1


def test_phash_edge_skipped_when_distance_exceeds_threshold(
    client: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the only candidate pair is past ``image_phash_max_distance``
    (here all 64 bits flipped), no ``phash_edge`` event is emitted."""
    main_mod.admin_settings_mod.set_override("enable_image_phash", 1, actor="test-7a")
    main_mod.admin_settings_mod.set_override("image_phash_max_distance", 6, actor="test-7a")
    monkeypatch.setattr(
        main_mod,
        "stream_checks",
        _stub_stream_checks(
            {
                "alice": [
                    _claimed("github", phash="0000000000000000"),
                    _claimed("twitter", phash="ffffffffffffffff"),
                ],
            }
        ),
    )

    try:
        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 0, "limit": 2},
            headers=_hdr(admin_token),
        )
    finally:
        admin_settings_mod.delete_override("enable_image_phash")
        admin_settings_mod.delete_override("image_phash_max_distance")

    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert "phash_edge" not in [e["event"] for e in events]
    done = json.loads(events[-1]["data"])
    assert done["phash_edges"] == 0


def test_phash_edge_disabled_when_admin_knob_off(
    client: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``enable_image_phash`` left at its default (0), the graph
    endpoint never runs the pairwise compare even if the input has
    matching phashes. ``phash_edges`` in the ``done`` payload is 0."""
    monkeypatch.setattr(
        main_mod,
        "stream_checks",
        _stub_stream_checks(
            {
                "alice": [
                    _claimed("github", phash="0123456789abcdef"),
                    _claimed("twitter", phash="0123456789abcdef"),
                ],
            }
        ),
    )

    r = client.post(
        "/api/search/graph",
        json={"username": "alice", "depth": 0, "limit": 2},
        headers=_hdr(admin_token),
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert "phash_edge" not in [e["event"] for e in events]
    done = json.loads(events[-1]["data"])
    assert done["phash_edges"] == 0


def test_phash_edge_skips_results_without_avatar_phash(
    client: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLAIMED row whose extractor pass missed an avatar (no
    ``avatar_phash`` field) is silently skipped — no spurious edges
    against rows that *do* have a hash."""
    main_mod.admin_settings_mod.set_override("enable_image_phash", 1, actor="test-7a")
    monkeypatch.setattr(
        main_mod,
        "stream_checks",
        _stub_stream_checks(
            {
                "alice": [
                    _claimed("github", phash="0123456789abcdef"),
                    _claimed("twitter", phash=None),  # extractor missed it
                ],
            }
        ),
    )

    try:
        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 0, "limit": 2},
            headers=_hdr(admin_token),
        )
    finally:
        admin_settings_mod.delete_override("enable_image_phash")

    events = _parse_sse(r.text)
    assert "phash_edge" not in [e["event"] for e in events]
    done = json.loads(events[-1]["data"])
    assert done["phash_edges"] == 0
