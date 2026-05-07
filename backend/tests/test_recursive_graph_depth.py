"""Phase 10d — recursive graph depth >1 with stricter conf floor.

These tests ride on top of ``app.main`` (same TestClient bootstrap as
``test_api.py`` / ``test_graph_phash.py``) but stub out
``stream_checks`` so we can hand the graph endpoint a deterministic set
of CLAIMED hits at every depth. End-to-end probing is covered
separately in ``test_api.py`` smoke tests.

Coverage map (the four contract points from the Phase 10d spec):

1. **Admin knobs**: ``graph_max_depth`` range bumped to 0-3,
   ``graph_secondary_min_confidence`` (50-100, default 80) and
   ``graph_max_total_subscans`` (50-500, default 200) round-trip via
   ``set_override`` / ``effective_map`` / ``describe`` and reject
   out-of-range writes.
2. **Default-off invariance**: depth=1 SSE output is byte-for-byte
   identical to the pre-Phase-10d shape (no new fields on existing
   events for the depth-1 payload, ``capped_subscans=0`` on the
   ``done`` payload).
3. **Deep recursion**: with the knob bumped, depth=2 spawns secondary
   scans of secondary children (extract=True so the next generation
   has fields to walk), and depth=3 spawns tertiary scans on top of
   that. The ``edge`` / ``extracted`` / ``secondary_start`` events at
   each depth carry an explicit ``depth`` field.
4. **Safety caps**: the ``graph_max_total_subscans`` cap surfaces
   SKIPPED rows with ``reason='graph_subscan_cap'`` for every
   candidate beyond the cap; the ``graph_secondary_min_confidence``
   floor blocks low-confidence CLAIMED children from spawning the
   next generation.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Same env bootstrap as test_api.py + test_graph_phash.py.
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


def _claimed(
    site: str,
    *,
    extracted: dict[str, str] | None = None,
    confidence: int = 90,
) -> CheckResult:
    """Build a CLAIMED CheckResult with optional extractor output."""
    return CheckResult(
        site=site,
        status=Status.CLAIMED,
        url=f"https://{site}.example/profile",
        url_main=f"https://{site}.example/",
        tags=[],
        http_status=200,
        confidence=confidence,
        signals=[],
        extracted=extracted or {},
    )


def _stub_stream_checks(
    results_by_username: dict[str, list[CheckResult]],
):
    """Replacement for ``stream_checks`` that yields pre-canned results
    keyed on the username. Honours the same call signature so the
    graph code can pass any kwargs (``client=``, ``extract=``, etc.)
    without raising. ``identity_type`` defaults to None to keep the
    graph code's call shape intact."""

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


@pytest.fixture(autouse=True)
def _reset_phase_10d_overrides(client: TestClient) -> Iterator[None]:
    """Force every test to start from default knob values so test
    order doesn't matter. Depending on ``client`` ensures the
    SQLite admin DB is bootstrapped before we delete rows."""
    for k in (
        "graph_max_depth",
        "graph_max_secondary",
        "graph_max_seed_hits",
        "graph_secondary_min_confidence",
        "graph_max_total_subscans",
        "enable_image_phash",
    ):
        admin_settings_mod.delete_override(k)
    admin_settings_mod._invalidate_cache()
    yield
    for k in (
        "graph_max_depth",
        "graph_max_secondary",
        "graph_max_seed_hits",
        "graph_secondary_min_confidence",
        "graph_max_total_subscans",
        "enable_image_phash",
    ):
        admin_settings_mod.delete_override(k)
    admin_settings_mod._invalidate_cache()


# ---------------------------------------------------------------------------
# Knob-level tests — admin surface area only, no graph traffic.
# ---------------------------------------------------------------------------


class TestPhase10dKnobs:
    def test_graph_max_depth_range_extended_to_three(self, client: TestClient) -> None:
        # Pre-Phase-10d the maximum was 1; Phase 10d bumps it to 3 to
        # unlock depth=2 and depth=3.
        admin_settings_mod.set_override("graph_max_depth", 3, actor="rootadmin")
        assert admin_settings_mod.effective_int("graph_max_depth") == 3
        with pytest.raises(admin_settings_mod.KnobOutOfRangeError):
            admin_settings_mod.set_override("graph_max_depth", 4, actor="rootadmin")
        with pytest.raises(admin_settings_mod.KnobOutOfRangeError):
            admin_settings_mod.set_override("graph_max_depth", -1, actor="rootadmin")

    def test_describe_lists_phase_10d_knobs(self, client: TestClient) -> None:
        keys = {row["key"] for row in admin_settings_mod.describe()}
        assert "graph_secondary_min_confidence" in keys
        assert "graph_max_total_subscans" in keys

    def test_effective_map_returns_phase_10d_defaults(self, client: TestClient) -> None:
        m = admin_settings_mod.effective_map()
        assert m["graph_secondary_min_confidence"] == 80
        assert m["graph_max_total_subscans"] == 200

    def test_secondary_min_confidence_round_trip_and_range(self, client: TestClient) -> None:
        admin_settings_mod.set_override("graph_secondary_min_confidence", 90, actor="rootadmin")
        assert admin_settings_mod.effective_int("graph_secondary_min_confidence") == 90
        for bad in (49, 101):
            with pytest.raises(admin_settings_mod.KnobOutOfRangeError):
                admin_settings_mod.set_override(
                    "graph_secondary_min_confidence", bad, actor="rootadmin"
                )

    def test_max_total_subscans_round_trip_and_range(self, client: TestClient) -> None:
        admin_settings_mod.set_override("graph_max_total_subscans", 100, actor="rootadmin")
        assert admin_settings_mod.effective_int("graph_max_total_subscans") == 100
        for bad in (49, 501):
            with pytest.raises(admin_settings_mod.KnobOutOfRangeError):
                admin_settings_mod.set_override("graph_max_total_subscans", bad, actor="rootadmin")

    def test_request_depth_range_accepts_three_rejects_four(
        self, client: TestClient, admin_token: str
    ) -> None:
        # 422 means pydantic V2 validation rejected it pre-handler;
        # confirms the GraphSearchRequest schema bumped from le=2 to
        # le=3 in Phase 10d.
        ok = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 3, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert ok.status_code == 200, ok.text
        bad = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 4, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Default-off invariance — depth=1 must keep its existing payload shape.
# ---------------------------------------------------------------------------


class TestDepth1RemainsUnchanged:
    def test_depth_1_done_payload_includes_phase_10d_counters_at_zero(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At depth=1 with default knobs the new ``capped_subscans``
        counter is always 0 (the cap is 200 sub-scans, depth=1
        worst-case is 8*5=40). This test pins the expectation so any
        future regression that flips ``capped_subscans`` non-zero at
        depth=1 fails loudly."""
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    # Single primary CLAIMED with one extractor field
                    # → spawns one depth-1 sub-scan.
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [],  # secondary scan returns nothing
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 1, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        assert done["capped_subscans"] == 0
        assert done["max_depth_reached"] == 1
        assert done["subscans_run"] == 1
        # ``edges`` is the structural-edge count; one secondary
        # spawned → one edge.
        assert done["edges"] == 1
        assert done["secondary_count"] == 1

    def test_depth_1_does_not_spawn_tertiary_when_knob_at_one(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ``graph_max_depth=1`` (default), even if the user
        passes ``depth=3`` in the request body the runtime clamps it
        down to 1 — no tertiary sub-scans, no depth-2 results."""
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [
                        _claimed(
                            "twitter",
                            extracted={"link_keybase": "https://keybase.io/carol"},
                        ),
                    ],
                    "carol": [],  # would only fire at depth>=3
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 3, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        # Default knob caps at 1, so the runtime should not have
        # walked into bob's extractor → no edge for bob → carol.
        assert done["max_depth_reached"] == 1
        assert done["subscans_run"] == 1
        # Only edge: alice → bob; nothing about bob → carol.
        edge_events = [e for e in events if e["event"] == "edge"]
        assert len(edge_events) == 1
        assert json.loads(edge_events[0]["data"])["child_username"] == "bob"


# ---------------------------------------------------------------------------
# Deep recursion — depth=2 + depth=3.
# ---------------------------------------------------------------------------


class TestDeepRecursion:
    def test_depth_2_spawns_secondary_subscan(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ``graph_max_depth=2`` the runtime walks alice → bob,
        extracts bob's profile fields, and spawns a secondary scan on
        the tertiary handle (carol). The depth-2 scan emits its own
        ``extracted`` / ``edge`` / ``secondary_start`` events with
        ``depth=2``."""
        admin_settings_mod.set_override("graph_max_depth", 2, actor="rootadmin")
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [
                        _claimed(
                            "twitter",
                            extracted={"link_keybase": "https://keybase.io/carol"},
                            confidence=90,
                        ),
                    ],
                    "carol": [_claimed("keybase")],
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 2, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        assert done["max_depth_reached"] == 2
        assert done["subscans_run"] == 2  # bob + carol
        # Two edges: alice → bob (depth 1), bob → carol (depth 2).
        edge_events = [json.loads(e["data"]) for e in events if e["event"] == "edge"]
        assert len(edge_events) == 2
        depths_by_child = {e["child_username"]: e["depth"] for e in edge_events}
        assert depths_by_child == {"bob": 1, "carol": 2}
        # Two ``secondary_start`` events, one per generation.
        sec_starts = [json.loads(e["data"]) for e in events if e["event"] == "secondary_start"]
        assert sorted((s["child_username"], s["depth"]) for s in sec_starts) == [
            ("bob", 1),
            ("carol", 2),
        ]

    def test_depth_3_spawns_three_generations(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At depth=3 the runtime walks four levels: alice → bob →
        carol → dave. Generations 1-2 carry extracted fields so the
        next generation has something to walk; generation 3 (the
        tertiary sub-scan) skips extraction since there is no fourth
        layer."""
        admin_settings_mod.set_override("graph_max_depth", 3, actor="rootadmin")
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [
                        _claimed(
                            "twitter",
                            extracted={"link_keybase": "https://keybase.io/carol"},
                            confidence=90,
                        ),
                    ],
                    "carol": [
                        _claimed(
                            "keybase",
                            extracted={"link_github": "https://github.com/dave"},
                            confidence=90,
                        ),
                    ],
                    "dave": [_claimed("github")],
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 3, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        assert done["max_depth_reached"] == 3
        assert done["subscans_run"] == 3  # bob, carol, dave
        edge_events = [json.loads(e["data"]) for e in events if e["event"] == "edge"]
        depths_by_child = {e["child_username"]: e["depth"] for e in edge_events}
        assert depths_by_child == {"bob": 1, "carol": 2, "dave": 3}


# ---------------------------------------------------------------------------
# Safety caps.
# ---------------------------------------------------------------------------


class TestSafetyCaps:
    def test_max_total_subscans_cap_hits_at_global_cap(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the global cap pinned at its 50-knob minimum and 60
        eligible candidates (3 primaries * 20 extracted handles each),
        the first 50 candidates enqueue normally and the remaining 10
        emit synthetic SKIPPED rows with ``reason='graph_subscan_cap'``.

        Note: the cap check fires at candidate-enqueue time, *before*
        any sub-scan actually runs, so ``subscans_run`` reflects the
        truly-spawned count and the SKIPPED rows are emitted purely
        from the candidate-enumeration loop."""
        admin_settings_mod.set_override("graph_max_depth", 1, actor="rootadmin")
        # 50 is the spec minimum for ``graph_max_total_subscans``; 20
        # is the spec maximum for ``graph_max_secondary`` and
        # ``graph_max_seed_hits``. With 3 primaries (each carrying
        # 20 extractor handles) we generate 60 candidates → 10 over
        # the cap.
        admin_settings_mod.set_override("graph_max_total_subscans", 50, actor="rootadmin")
        admin_settings_mod.set_override("graph_max_secondary", 20, actor="rootadmin")
        admin_settings_mod.set_override("graph_max_seed_hits", 20, actor="rootadmin")
        # Three primary CLAIMED hits, each with 20 extracted children
        # → 60 candidates total. We use unique child handles per
        # primary so dedup doesn't kick in.
        # Build extracted handles using real Twitter URLs because
        # ``find_secondary_usernames`` only walks values that match a
        # registered social-URL pattern. The handle text must also
        # clear ``_looks_like_username`` (3-20 chars, alphanumeric,
        # no skip-list collision).
        primaries = [
            _claimed(
                f"site{p}",
                extracted={
                    f"link_handle_{p}_{c}": (f"https://twitter.com/handle{p}user{c}")
                    for c in range(20)
                },
            )
            for p in range(3)
        ]
        # Spawned sub-scans return nothing — keeps the test fast and
        # ensures the cap kicks at candidate-enqueue time, not on the
        # secondary stream.
        results = {"alice": primaries}
        for p in range(3):
            for c in range(20):
                results[f"handle{p}user{c}"] = []
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(results),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 1, "limit": 5},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        # Cap = 50 → first 50 candidates spawn, the next 10 become
        # SKIPPED graph_subscan_cap rows.
        assert done["capped_subscans"] == 10
        assert done["subscans_run"] == 50
        skipped_events = [
            json.loads(e["data"])
            for e in events
            if e["event"] == "result"
            and json.loads(e["data"]).get("status") == "skipped"
            and json.loads(e["data"]).get("reason") == "graph_subscan_cap"
        ]
        assert len(skipped_events) == 10
        for s in skipped_events:
            assert s["site"] == "graph_subscan_cap"
            assert s["depth"] == 1

    def test_secondary_conf_floor_blocks_next_generation_spawn(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At depth=2 a CLAIMED secondary-scan hit whose confidence
        falls below ``graph_secondary_min_confidence`` does not spawn
        a tertiary scan even though it has extractor fields.

        Note: the Phase 5h ``min_claimed_confidence`` default is 60;
        the Phase 10d default for the secondary floor is 80. We
        exercise the gap by giving bob a CLAIMED hit at confidence=70
        — above the rule-based floor (so it stays CLAIMED) but below
        the secondary-spawn floor (so carol is never spawned)."""
        admin_settings_mod.set_override("graph_max_depth", 2, actor="rootadmin")
        # Default secondary_min_confidence=80; bob comes in at 70.
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [
                        _claimed(
                            "twitter",
                            extracted={"link_keybase": "https://keybase.io/carol"},
                            confidence=70,
                        ),
                    ],
                    "carol": [_claimed("keybase")],
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 2, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        # Only one sub-scan ran (bob), carol was never spawned because
        # bob's confidence=70 < floor=80.
        assert done["subscans_run"] == 1
        edge_events = [json.loads(e["data"]) for e in events if e["event"] == "edge"]
        assert len(edge_events) == 1
        assert edge_events[0]["child_username"] == "bob"

    def test_lowering_secondary_floor_unblocks_next_generation(
        self,
        client: TestClient,
        admin_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lowering ``graph_secondary_min_confidence`` to 60 lets the
        same confidence=70 secondary spawn a tertiary scan."""
        admin_settings_mod.set_override("graph_max_depth", 2, actor="rootadmin")
        admin_settings_mod.set_override("graph_secondary_min_confidence", 60, actor="rootadmin")
        monkeypatch.setattr(
            main_mod,
            "stream_checks",
            _stub_stream_checks(
                {
                    "alice": [
                        _claimed(
                            "github",
                            extracted={"link_twitter": "https://twitter.com/bob"},
                        ),
                    ],
                    "bob": [
                        _claimed(
                            "twitter",
                            extracted={"link_keybase": "https://keybase.io/carol"},
                            confidence=70,
                        ),
                    ],
                    "carol": [_claimed("keybase")],
                }
            ),
        )

        r = client.post(
            "/api/search/graph",
            json={"username": "alice", "depth": 2, "limit": 1},
            headers=_hdr(admin_token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = json.loads(events[-1]["data"])
        assert done["subscans_run"] == 2  # bob + carol
        edge_events = [json.loads(e["data"]) for e in events if e["event"] == "edge"]
        assert {e["child_username"] for e in edge_events} == {"bob", "carol"}
