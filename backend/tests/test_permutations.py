"""Phase 5c: deterministic username permutation generator + HTTP route."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import permutations as permutations_mod
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


# ---------------------------------------------------------------------------
# Module: generate_permutations.
# ---------------------------------------------------------------------------


def test_generate_excludes_the_base_username() -> None:
    out = permutations_mod.generate_permutations("torvalds", max_count=20)
    assert "torvalds" not in out
    # Cap is honoured.
    assert len(out) <= 20


def test_generate_dedupes_across_strategies() -> None:
    out = permutations_mod.generate_permutations("torvalds", max_count=40)
    assert len(out) == len(set(out)), "duplicates leaked into the output"


def test_generate_invalid_base_raises() -> None:
    with pytest.raises(ValueError):
        permutations_mod.generate_permutations("has space", max_count=10)
    with pytest.raises(ValueError):
        permutations_mod.generate_permutations("", max_count=10)


def test_generate_unknown_strategy_raises() -> None:
    with pytest.raises(permutations_mod.UnknownStrategyError):
        permutations_mod.generate_permutations(
            "torvalds",
            max_count=10,
            strategies=["bogus"],
        )


def test_generate_strategy_filter_isolates_to_one_family() -> None:
    suffixes = permutations_mod.generate_permutations(
        "torvalds",
        max_count=40,
        strategies=["suffix"],
    )
    # Every variant should *start* with the base when only suffixes are
    # generated.
    assert all(v.startswith("torvalds") for v in suffixes)
    # And include at least one of the canonical suffixes.
    assert "torvalds_real" in suffixes


def test_generate_strategy_filter_prefix() -> None:
    out = permutations_mod.generate_permutations(
        "torvalds",
        max_count=40,
        strategies=["prefix"],
    )
    # Every variant ends in the base when only prefixes are generated.
    assert all(v.endswith("torvalds") for v in out)
    assert "the_torvalds" in out
    assert "real_torvalds" in out


def test_generate_separator_handles_camel_case() -> None:
    out = permutations_mod.generate_permutations(
        "JohnDoe",
        max_count=10,
        strategies=["separator"],
    )
    assert "john_doe" in out
    assert "john.doe" in out
    assert "john-doe" in out


def test_generate_leetspeak_swaps() -> None:
    out = permutations_mod.generate_permutations(
        "google",
        max_count=20,
        strategies=["leetspeak"],
    )
    # 'o' → '0' and 'g' → '9' should both appear as single-char swaps.
    assert "g00gle" in out
    assert "9oo9le" in out


def test_generate_max_count_zero_returns_empty() -> None:
    assert permutations_mod.generate_permutations("torvalds", max_count=0) == []


def test_generate_dedupes_strategies_argument() -> None:
    """Repeating ``"suffix"`` shouldn't double the output."""
    once = permutations_mod.generate_permutations(
        "torvalds",
        max_count=40,
        strategies=["suffix"],
    )
    twice = permutations_mod.generate_permutations(
        "torvalds",
        max_count=40,
        strategies=["suffix", "suffix"],
    )
    assert once == twice


def test_generate_drops_invalid_variants() -> None:
    # A 60-char base + 9-char suffix would blow through the 64-char cap on
    # ``is_valid_username`` — the variant must be silently dropped.
    base = "a" * 60
    out = permutations_mod.generate_permutations(
        base,
        max_count=20,
        strategies=["suffix"],
    )
    for v in out:
        assert len(v) <= 64, f"{v!r} exceeds the 64-char cap"


# ---------------------------------------------------------------------------
# HTTP: GET /api/permute/strategies.
# ---------------------------------------------------------------------------


def test_strategies_endpoint_lists_all_four(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.get("/api/permute/strategies", headers=_hdr(admin_token))
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()["strategies"]}
    assert keys == permutations_mod.STRATEGY_KEYS


def test_strategies_endpoint_requires_auth(client: TestClient) -> None:
    r = client.get("/api/permute/strategies")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# HTTP: POST /api/search/permute.
#
# We don't stand up a real catalog scan against the live network here —
# instead we monkeypatch ``stream_checks`` to yield a single canned
# CLAIMED result per handle. That gives us deterministic SSE output and
# confirms the route's wiring (start → candidate_start → result →
# candidate_done → done) without flakiness.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_stream_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``stream_checks`` so each handle emits exactly one
    CLAIMED result against a fake site, regardless of the real catalog."""
    from app import main as main_mod
    from app.checker import Status

    class FakeResult:
        def __init__(self, username: str) -> None:
            self.status = Status.CLAIMED
            self._u = username

        def to_dict(self) -> dict[str, object]:
            return {
                "site": "fakesite",
                "url": f"https://fake.example/{self._u}",
                "username": self._u,
                "status": "claimed",
                "confidence": 90,
                "duration_ms": 1,
            }

    async def fake_stream(sites, username, *, client, extract=True):  # type: ignore[no-untyped-def]
        yield FakeResult(username)

    monkeypatch.setattr(main_mod, "stream_checks", fake_stream)


def test_permute_route_runs_base_plus_candidates(
    client: TestClient,
    admin_token: str,
    stub_stream_checks: None,
) -> None:
    r = client.post(
        "/api/search/permute",
        headers=_hdr(admin_token),
        json={
            "username": "torvalds",
            "tags": [],
            "limit": 1,
            "max_permutations": 3,
            "strategies": ["suffix"],
        },
    )
    assert r.status_code == 200
    body = r.text
    # The start frame announces the candidate set.
    assert "event: start" in body
    assert '"candidate_count": 4' in body or '"candidate_count":4' in body
    # First candidate_start is for the base.
    assert '"username": "torvalds"' in body
    assert '"is_base": true' in body or '"is_base":true' in body
    # End frame reports the run count.
    assert "event: done" in body
    assert '"candidates_run": 4' in body or '"candidates_run":4' in body


def test_permute_route_zero_candidates_runs_only_base(
    client: TestClient,
    admin_token: str,
    stub_stream_checks: None,
) -> None:
    r = client.post(
        "/api/search/permute",
        headers=_hdr(admin_token),
        json={
            "username": "torvalds",
            "tags": [],
            "limit": 1,
            "max_permutations": 0,
        },
    )
    assert r.status_code == 200
    body = r.text
    assert '"candidate_count": 1' in body or '"candidate_count":1' in body
    assert '"candidates_run": 1' in body or '"candidates_run":1' in body


def test_permute_route_rejects_unknown_strategy(
    client: TestClient,
    admin_token: str,
) -> None:
    r = client.post(
        "/api/search/permute",
        headers=_hdr(admin_token),
        json={
            "username": "torvalds",
            "max_permutations": 5,
            "strategies": ["bogus"],
        },
    )
    assert r.status_code == 422  # pydantic validator caught it pre-stream


def test_permute_route_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/search/permute",
        json={"username": "torvalds", "max_permutations": 0},
    )
    assert r.status_code == 401
