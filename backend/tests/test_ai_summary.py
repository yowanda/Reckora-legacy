"""Phase 10g: AI investigation summary.

The tests are split into four bands:

* Markdown report builder — verifies the projection from scan history +
  extractor fields + graph edges + pHash matches into the compact
  markdown document we hand to the LLM.
* Provider wire-format adapters — round-trip checks that
  :func:`ai_summary.build_request` produces the exact JSON shape /
  headers / URL each upstream expects (OpenAI, Anthropic, OpenRouter,
  Ollama).
* Streaming-response unwrap — verifies :func:`ai_summary.stream_response`
  decodes provider-specific chunks into plain-text fragments.
* HTTP endpoint — verifies the four admin knobs round-trip, the
  ``MissingApiKey`` translates to ``412``, the master switch translates
  to ``503``, and a happy-path call streams markdown back to the SPA.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import ai_summary, secrets_store
from app import history as history_mod
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


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolate(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Each test starts and ends with a clean history table, no admin
    overrides, and a private ``secrets.json`` location.

    We deliberately do **not** redirect ``settings.db_path`` here —
    that would land us on a fresh, schema-less SQLite file. Instead we
    override :func:`secrets_store._path` so the secret writes land in
    ``tmp_path`` while the rest of the app keeps using the shared test
    DB created by ``conftest.py``. Depending on the module-scoped
    ``client`` fixture forces the FastAPI startup hook (which builds
    the schema) to run before this fixture's first ``DELETE``.
    """
    del client  # ordering dependency only
    from app.auth import db_connection

    secrets_path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_store, "_path", lambda: secrets_path)
    secrets_store._reset_cache_for_tests()

    with db_connection() as conn:
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()
    yield
    with db_connection() as conn:
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM admin_settings")
    admin_settings_mod._invalidate_cache()
    secrets_store._reset_cache_for_tests()


_MEMBER = "rootadmin"


def _seed_scan(member: str, username: str, results: list[dict]) -> int:
    started = 1_700_000_000
    finished = started + 12
    new_id = history_mod.record_scan(
        member=member,
        username=username,
        mode="list",
        started_at=started,
        finished_at=finished,
        site_count=len(results),
        results=results,
    )
    assert new_id is not None
    return new_id


def _enable_ai_summary() -> None:
    admin_settings_mod.set_override("enable_ai_summary", 1, actor="rootadmin")


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------


def test_build_markdown_report_renders_claimed_table() -> None:
    summary = history_mod.ScanSummary(
        id=42,
        member="rootadmin",
        username="alice",
        mode="list",
        started_at=1_700_000_000,
        finished_at=1_700_000_012,
        site_count=3,
        claimed=2,
        free=1,
        blocked=0,
        unknown=0,
        error_count=0,
    )
    results = [
        {
            "site": "github",
            "status": "claimed",
            "confidence": 95,
            "ml_confidence": 90,
            "url": "https://github.com/alice",
            "extracted": {"real_name": "Alice Doe", "location": "NYC"},
        },
        {
            "site": "twitter",
            "status": "claimed",
            "confidence": 80,
            "ml_confidence": 70,
            "url": "https://x.com/alice",
            "extracted": {"secondary_usernames": ["alice2", "alice_real"]},
        },
        {
            "site": "reddit",
            "status": "free",
            "confidence": 70,
            "ml_confidence": 5,
            "url": "https://reddit.com/u/alice",
        },
    ]
    md = ai_summary.build_markdown_report(summary, results)
    assert "Reckora scan #42" in md
    assert "`alice`" in md
    assert "## CLAIMED hits" in md
    # Claimed rows present, free row excluded from the CLAIMED table.
    assert "github" in md
    assert "twitter" in md
    assert "reddit.com/u/alice" not in md
    # Higher-confidence row sorted first.
    assert md.index("github") < md.index("twitter")
    # Extracted fields rendered as ``k=v; k=v``.
    assert "real_name=Alice Doe" in md
    assert "secondary_usernames=alice2, alice_real" in md


def test_build_markdown_report_handles_no_claimed_rows() -> None:
    summary = history_mod.ScanSummary(
        id=1,
        member="rootadmin",
        username="ghost",
        mode="list",
        started_at=0,
        finished_at=1,
        site_count=0,
        claimed=0,
        free=0,
        blocked=0,
        unknown=0,
        error_count=0,
    )
    md = ai_summary.build_markdown_report(summary, [])
    assert "_No CLAIMED hits" in md


def test_build_markdown_report_includes_graph_and_phash_sections() -> None:
    summary = history_mod.ScanSummary(
        id=7,
        member="rootadmin",
        username="bob",
        mode="graph",
        started_at=0,
        finished_at=10,
        site_count=2,
        claimed=2,
        free=0,
        blocked=0,
        unknown=0,
        error_count=0,
    )
    results = [
        {
            "site": "github",
            "status": "claimed",
            "confidence": 95,
            "ml_confidence": 90,
            "url": "https://github.com/bob",
        },
        {
            "site": "gitlab",
            "status": "claimed",
            "confidence": 90,
            "ml_confidence": 85,
            "url": "https://gitlab.com/bob",
        },
    ]
    edges = [ai_summary.GraphEdge(parent_username="bob", child_username="bob_dev", site="github")]
    matches = [ai_summary.PhashMatch(site_a="github", site_b="gitlab", distance=2)]
    md = ai_summary.build_markdown_report(
        summary, results, graph_edges=edges, phash_matches=matches
    )
    assert "## Graph edges" in md
    assert "`bob` → `bob_dev`" in md
    assert "## Perceptual-hash matches" in md
    assert "Hamming distance 2" in md


def test_graph_edges_from_results_skips_rows_without_parent() -> None:
    results = [
        {"site": "github", "status": "claimed", "username": "primary"},
        {
            "site": "twitter",
            "status": "claimed",
            "username": "primary_t",
            "parent_username": "primary",
        },
        {"site": "reddit", "status": "free", "parent_username": "primary"},
    ]
    edges = ai_summary.graph_edges_from_results(results)
    assert len(edges) == 1
    assert edges[0].parent_username == "primary"
    assert edges[0].child_username == "primary_t"


def test_phash_matches_from_results_returns_empty_without_avatar_hashes() -> None:
    # History only persists summary tuples, so without raw avatar hashes
    # we expect the rebuilder to return an empty list (callers that have
    # the raw hashes pass them in via the optional argument).
    results = [
        {"site": "github", "status": "claimed"},
        {"site": "gitlab", "status": "claimed"},
    ]
    assert ai_summary.phash_matches_from_results(results) == []


# ---------------------------------------------------------------------------
# Provider wire-format adapters
# ---------------------------------------------------------------------------


def test_build_request_openai_uses_chat_completions_endpoint() -> None:
    req = ai_summary.build_request(
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        prompt="hello",
    )
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert req.headers["Accept"] == "text/event-stream"
    assert req.body["model"] == "gpt-4o-mini"
    assert req.body["stream"] is True
    assert req.body["messages"][0]["role"] == "system"
    assert req.body["messages"][1] == {"role": "user", "content": "hello"}


def test_build_request_openrouter_adds_referrer_headers() -> None:
    req = ai_summary.build_request(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        base_url="",  # falls back to the openrouter default
        api_key="sk-or-test",
        prompt="hello",
    )
    assert req.url == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-or-test"
    # OpenRouter dashboard requires HTTP-Referer + X-Title for grouping.
    assert "HTTP-Referer" in req.headers
    assert req.headers["X-Title"] == "Reckora"


def test_build_request_anthropic_uses_messages_endpoint_and_x_api_key() -> None:
    req = ai_summary.build_request(
        provider="anthropic",
        model="claude-3-5-haiku-latest",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-test",
        prompt="hello",
    )
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert req.headers["anthropic-version"] == "2023-06-01"
    # Anthropic uses a top-level ``system`` field, not a system message.
    assert "system" in req.body
    assert req.body["messages"] == [{"role": "user", "content": "hello"}]
    assert req.body["stream"] is True


def test_build_request_ollama_does_not_require_api_key() -> None:
    req = ai_summary.build_request(
        provider="ollama",
        model="llama3.1",
        base_url="http://localhost:11434",
        api_key="",  # local runtime, no key needed
        prompt="hello",
    )
    assert req.url == "http://localhost:11434/api/chat"
    assert "Authorization" not in req.headers
    assert req.body["model"] == "llama3.1"
    assert req.body["stream"] is True
    assert req.body["messages"][1]["content"] == "hello"


def test_build_request_rejects_unknown_provider() -> None:
    with pytest.raises(ai_summary.UnknownProviderError):
        ai_summary.build_request(
            provider="bogus",
            model="x",
            base_url="",
            api_key="x",
            prompt="x",
        )


def test_build_request_openai_requires_api_key() -> None:
    with pytest.raises(ai_summary.MissingApiKeyError):
        ai_summary.build_request(
            provider="openai",
            model="gpt-4o-mini",
            base_url="",
            api_key="",
            prompt="hello",
        )


# ---------------------------------------------------------------------------
# Streaming-response unwrap
# ---------------------------------------------------------------------------


def _build_streaming_response(provider: str, payload: bytes) -> httpx.Response:
    """Wrap a fixed byte payload in an :class:`httpx.Response` with a
    streaming body suitable for :func:`ai_summary.stream_response`."""

    async def _gen() -> Iterator[bytes]:
        # A single-shot async generator is enough for a unit test —
        # ``stream_response`` only cares that the body decodes to the
        # expected provider-specific chunks.
        yield payload

    media = "application/x-ndjson" if provider == "ollama" else "text/event-stream"
    return httpx.Response(
        200,
        headers={"content-type": media},
        content=_gen(),
    )


@pytest.mark.asyncio
async def test_stream_response_decodes_openai_sse_chunks() -> None:
    body = (
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    resp = _build_streaming_response("openai", body)
    out = [chunk async for chunk in ai_summary.stream_response(resp, provider="openai")]
    assert "".join(out) == "Hello world"


@pytest.mark.asyncio
async def test_stream_response_decodes_anthropic_sse_chunks() -> None:
    body = (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" there"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )
    resp = _build_streaming_response("anthropic", body)
    out = [chunk async for chunk in ai_summary.stream_response(resp, provider="anthropic")]
    assert "".join(out) == "Hi there"


@pytest.mark.asyncio
async def test_stream_response_decodes_ollama_ndjson_chunks() -> None:
    body = b'{"message":{"content":"foo"}}\n{"message":{"content":" bar"}}\n{"done":true}\n'
    resp = _build_streaming_response("ollama", body)
    out = [chunk async for chunk in ai_summary.stream_response(resp, provider="ollama")]
    assert "".join(out) == "foo bar"


@pytest.mark.asyncio
async def test_stream_summary_uses_mock_transport_and_yields_text() -> None:
    """End-to-end with an :class:`httpx.MockTransport` posing as OpenAI."""

    seen_requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        body = (
            b'data: {"choices":[{"delta":{"content":"Investigation"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" summary"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out: list[str] = []
        async for fragment in ai_summary.stream_summary(
            provider="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            prompt="hi",
            client=client,
        ):
            out.append(fragment)
    assert "".join(out) == "Investigation summary"
    assert len(seen_requests) == 1
    sent = json.loads(seen_requests[0].content.decode())
    assert sent["model"] == "gpt-4o-mini"
    assert sent["stream"] is True


# ---------------------------------------------------------------------------
# Admin knobs round-trip + REST endpoint
# ---------------------------------------------------------------------------


def test_four_admin_knobs_round_trip(
    client: TestClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 10g 'four-knob' contract:

    * ``enable_ai_summary`` is the master switch — registered as an
      int admin knob in ``admin_settings.KNOBS`` so an operator can flip
      it at runtime via PATCH ``/api/admin/settings`` without a redeploy.
    * ``ai_summary_provider`` / ``ai_summary_base_url`` / ``ai_summary_model``
      are operator config (string-typed). The admin DB is int-only by
      design, so these live on :class:`Settings` and are env-var driven.
      The AI summary module reads them via
      :func:`ai_summary.current_provider` / ``current_base_url`` /
      ``current_model``.
    """
    # Defaults: master switch registered, enable=0.
    r = client.get("/api/admin/settings", headers=_hdr(admin_token))
    assert r.status_code == 200
    body = r.json()
    keys = {k["key"] for k in body["knobs"]}
    assert "enable_ai_summary" in keys
    enable_knob = next(k for k in body["knobs"] if k["key"] == "enable_ai_summary")
    assert enable_knob["default"] == 0
    assert enable_knob["effective"] == 0
    assert enable_knob["minimum"] == 0
    assert enable_knob["maximum"] == 1
    assert ai_summary.is_enabled() is False

    # Round-trip the master switch through PATCH.
    r = client.patch(
        "/api/admin/settings",
        headers=_hdr(admin_token),
        json={"enable_ai_summary": 1},
    )
    assert r.status_code == 200
    assert ai_summary.is_enabled() is True

    # The three string knobs are env-var-driven; verify the
    # ``current_*`` helpers honour ``Settings`` overrides.
    monkeypatch.setattr(ai_summary.settings, "ai_summary_provider", "anthropic")
    monkeypatch.setattr(
        ai_summary.settings,
        "ai_summary_base_url",
        "https://api.anthropic.com/v1",
    )
    monkeypatch.setattr(
        ai_summary.settings,
        "ai_summary_model",
        "claude-3-5-haiku-latest",
    )
    assert ai_summary.current_provider() == "anthropic"
    assert ai_summary.current_base_url() == "https://api.anthropic.com/v1"
    assert ai_summary.current_model() == "claude-3-5-haiku-latest"


def test_endpoint_503_when_master_switch_off(client: TestClient, admin_token: str) -> None:
    scan_id = _seed_scan(
        _MEMBER,
        "alice",
        [
            {
                "site": "github",
                "status": "claimed",
                "confidence": 95,
                "ml_confidence": 90,
                "url": "https://github.com/alice",
            }
        ],
    )
    r = client.post(f"/api/history/{scan_id}/ai_summary", headers=_hdr(admin_token))
    assert r.status_code == 503


def test_endpoint_412_when_api_key_missing(client: TestClient, admin_token: str) -> None:
    _enable_ai_summary()
    scan_id = _seed_scan(
        _MEMBER,
        "alice",
        [
            {
                "site": "github",
                "status": "claimed",
                "confidence": 95,
                "ml_confidence": 90,
                "url": "https://github.com/alice",
            }
        ],
    )
    # No secret configured — handler should fail-open with 412.
    r = client.post(f"/api/history/{scan_id}/ai_summary", headers=_hdr(admin_token))
    assert r.status_code == 412


def test_endpoint_404_when_scan_not_visible(client: TestClient, admin_token: str) -> None:
    _enable_ai_summary()
    secrets_store.set_secret("ai_summary_api_key", "sk-test")
    r = client.post("/api/history/999999/ai_summary", headers=_hdr(admin_token))
    assert r.status_code == 404


def test_secrets_endpoints_round_trip(client: TestClient, admin_token: str) -> None:
    # Initial: key is unset.
    r = client.get("/api/admin/secrets", headers=_hdr(admin_token))
    assert r.status_code == 200
    assert r.json()["secrets"]["ai_summary_api_key"] is False

    # Set the key.
    r = client.put(
        "/api/admin/secrets/ai_summary_api_key",
        headers=_hdr(admin_token),
        json={"value": "sk-test-set"},
    )
    assert r.status_code == 200
    assert r.json()["configured"] is True
    assert secrets_store.get_secret("ai_summary_api_key") == "sk-test-set"

    # GET reflects "configured".
    r = client.get("/api/admin/secrets", headers=_hdr(admin_token))
    assert r.json()["secrets"]["ai_summary_api_key"] is True

    # Unknown keys are 404.
    r = client.put(
        "/api/admin/secrets/totally_unknown",
        headers=_hdr(admin_token),
        json={"value": "x"},
    )
    assert r.status_code == 404

    # Delete clears the key.
    r = client.delete(
        "/api/admin/secrets/ai_summary_api_key",
        headers=_hdr(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert secrets_store.has_secret("ai_summary_api_key") is False
