"""Phase 7b: enrichment module + ``POST /api/enrich/email`` endpoint tests.

Two halves:

1. Pure-module unit tests against ``app.enrichment`` — RDAP / HIBP
   parsers, the rate limiter, and the HTTP fetchers (driven through
   ``httpx.MockTransport`` so we never touch the public internet).
2. End-to-end endpoint tests via ``fastapi.testclient.TestClient``,
   which exercise auth / admin-knob gating / rate limiting / error
   shape. The ``/api/enrich/email`` handler instantiates its own
   ``httpx.AsyncClient`` so we monkeypatch ``app.enrichment.enrich_email``
   directly to avoid the real network in those tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import enrichment as enrichment_mod
from app.enrichment import (
    Breach,
    EnrichmentResult,
    RateLimiter,
    WhoisRecord,
    email_domain,
    enrich_email,
    fetch_breaches,
    fetch_whois,
    parse_hibp_breaches,
    parse_rdap_domain,
)
from app.main import app

# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


class TestEmailDomain:
    def test_returns_lowered_domain(self) -> None:
        assert email_domain("Alice@Example.COM") == "example.com"

    def test_returns_none_when_at_missing(self) -> None:
        assert email_domain("nope") is None

    def test_returns_none_for_empty_local_or_domain(self) -> None:
        assert email_domain("@example.com") is None
        assert email_domain("alice@") is None

    def test_handles_multiple_at_signs_via_rfind(self) -> None:
        # The address-like-but-not-quoted "a@b@example.com" has its
        # right-most ``@`` as the domain anchor.
        assert email_domain("a@b@example.com") == "example.com"


# ---------------------------------------------------------------------------
# RDAP / HIBP parsers.
# ---------------------------------------------------------------------------


class TestParseRdapDomain:
    def test_picks_registrar_via_vcard_fn(self) -> None:
        record = parse_rdap_domain(
            {
                "ldhName": "example.com",
                "events": [
                    {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
                    {"eventAction": "expiration", "eventDate": "2030-08-13T04:00:00Z"},
                    {"eventAction": "last changed", "eventDate": "2024-08-14T07:01:31Z"},
                ],
                "entities": [
                    {
                        "roles": ["registrar"],
                        "vcardArray": [
                            "vcard",
                            [
                                ["version", {}, "text", "4.0"],
                                ["fn", {}, "text", "Reserved by ICANN"],
                            ],
                        ],
                    }
                ],
                "nameservers": [
                    {"ldhName": "a.iana-servers.net"},
                    {"ldhName": "b.iana-servers.net"},
                ],
                "status": ["active", "client transfer prohibited"],
            }
        )
        assert record == WhoisRecord(
            domain="example.com",
            registrar="Reserved by ICANN",
            created="1995-08-14T04:00:00Z",
            expires="2030-08-13T04:00:00Z",
            updated="2024-08-14T07:01:31Z",
            nameservers=("a.iana-servers.net", "b.iana-servers.net"),
            status=("active", "client transfer prohibited"),
        )

    def test_falls_back_to_handle_when_no_fn(self) -> None:
        record = parse_rdap_domain(
            {
                "ldhName": "example.com",
                "entities": [
                    {"roles": ["registrar"], "handle": "REG-007"},
                ],
            }
        )
        assert record.registrar == "REG-007"
        assert record.created is None and record.expires is None

    def test_tolerates_missing_blocks(self) -> None:
        record = parse_rdap_domain({"ldhName": "x.test"})
        assert record == WhoisRecord(
            domain="x.test",
            registrar=None,
            created=None,
            expires=None,
            updated=None,
            nameservers=(),
            status=(),
        )

    def test_tolerates_garbage_shapes(self) -> None:
        record = parse_rdap_domain(
            {
                "ldhName": 7,  # wrong type — domain stays empty
                "events": "nope",  # wrong type — silently skipped
                "entities": [{"roles": "registrar"}],  # roles must be list
                "nameservers": [None, {"ldhName": "ns.x.test"}],
                "status": ["ok", 42],
            }
        )
        assert record.domain == ""
        assert record.registrar is None
        assert record.nameservers == ("ns.x.test",)
        assert record.status == ("ok",)


class TestParseHibpBreaches:
    def test_distils_full_breach_object(self) -> None:
        out = parse_hibp_breaches(
            [
                {
                    "Name": "Adobe",
                    "Title": "Adobe",
                    "Domain": "adobe.com",
                    "BreachDate": "2013-10-04",
                    "AddedDate": "2013-12-04T00:00:00Z",
                    "PwnCount": 152_445_165,
                    "DataClasses": ["Email addresses", "Password hints", "Passwords"],
                    "IsVerified": True,
                    "IsFabricated": False,
                    "IsSensitive": False,
                }
            ]
        )
        assert out == [
            Breach(
                name="Adobe",
                title="Adobe",
                domain="adobe.com",
                breach_date="2013-10-04",
                added_date="2013-12-04T00:00:00Z",
                pwn_count=152_445_165,
                data_classes=(
                    "Email addresses",
                    "Password hints",
                    "Passwords",
                ),
                is_verified=True,
                is_fabricated=False,
                is_sensitive=False,
            )
        ]

    def test_skips_entries_missing_name(self) -> None:
        out = parse_hibp_breaches([{"name": "ok", "title": "OK"}, {"foo": 1}])
        assert [b.name for b in out] == ["ok"]

    def test_returns_empty_list_for_non_list_payload(self) -> None:
        assert parse_hibp_breaches({"oops": True}) == []
        assert parse_hibp_breaches(None) == []
        assert parse_hibp_breaches("nope") == []


# ---------------------------------------------------------------------------
# Rate limiter.
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_up_to_limit_then_denies(self) -> None:
        limiter = RateLimiter(window_seconds=60.0)
        for i in range(5):
            assert limiter.allow("alice", limit=5, now=100.0 + i * 0.1) is True
        assert limiter.allow("alice", limit=5, now=100.5) is False

    def test_window_eviction_releases_budget(self) -> None:
        limiter = RateLimiter(window_seconds=60.0)
        for i in range(3):
            limiter.allow("alice", limit=3, now=100.0 + i)
        # 30 seconds later — still inside the window, all 3 hits stand.
        assert limiter.allow("alice", limit=3, now=130.0) is False
        # 65 seconds past the first hit — the first slot drops out and
        # we should have budget for one more request.
        assert limiter.allow("alice", limit=3, now=165.5) is True

    def test_per_actor_buckets_independent(self) -> None:
        limiter = RateLimiter()
        for _ in range(2):
            assert limiter.allow("alice", limit=2) is True
        # Bob's bucket is fresh.
        assert limiter.allow("bob", limit=2) is True

    def test_zero_or_negative_limit_always_denies(self) -> None:
        limiter = RateLimiter()
        assert limiter.allow("alice", limit=0) is False
        assert limiter.allow("alice", limit=-3) is False

    def test_reset_one_actor_only(self) -> None:
        limiter = RateLimiter()
        for _ in range(2):
            limiter.allow("alice", limit=2)
            limiter.allow("bob", limit=2)
        limiter.reset("alice")
        assert limiter.allow("alice", limit=2) is True
        assert limiter.allow("bob", limit=2) is False


# ---------------------------------------------------------------------------
# HTTP fetchers — driven through MockTransport.
# ---------------------------------------------------------------------------


def _mock_client(handler: httpx.RequestHandler | Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetchWhois:
    @pytest.mark.asyncio
    async def test_returns_record_on_200(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "ldhName": "example.com",
                    "events": [
                        {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"}
                    ],
                },
            )

        async with _mock_client(handler) as client:
            record = await fetch_whois(
                client,
                "example.com",
                url_template="https://rdap.test/domain/{domain}",
                timeout=2.0,
            )
        assert record is not None
        assert record.domain == "example.com"
        assert record.created == "1995-08-14T04:00:00Z"
        assert seen["url"] == "https://rdap.test/domain/example.com"

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={})

        async with _mock_client(handler) as client:
            record = await fetch_whois(
                client,
                "missing.test",
                url_template="https://rdap.test/domain/{domain}",
                timeout=2.0,
            )
        assert record is None

    @pytest.mark.asyncio
    async def test_returns_none_on_transport_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        async with _mock_client(handler) as client:
            record = await fetch_whois(
                client,
                "example.com",
                url_template="https://rdap.test/domain/{domain}",
                timeout=2.0,
            )
        assert record is None

    @pytest.mark.asyncio
    async def test_returns_none_when_template_missing_placeholder(self) -> None:
        async with _mock_client(lambda _r: httpx.Response(200, json={})) as client:
            record = await fetch_whois(
                client,
                "example.com",
                url_template="https://rdap.test/static",
                timeout=2.0,
            )
        assert record is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_json_body(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json", headers={"content-type": "text/html"})

        async with _mock_client(handler) as client:
            record = await fetch_whois(
                client,
                "example.com",
                url_template="https://rdap.test/domain/{domain}",
                timeout=2.0,
            )
        assert record is None


class TestFetchBreaches:
    @pytest.mark.asyncio
    async def test_returns_list_on_200(self) -> None:
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(request.headers))
            return httpx.Response(
                200,
                json=[
                    {"Name": "Adobe", "Title": "Adobe", "DataClasses": ["Emails"]},
                ],
            )

        async with _mock_client(handler) as client:
            out = await fetch_breaches(
                client,
                "alice@example.com",
                url_template="https://hibp.test/api/v3/breachedaccount/{email}",
                api_key="key123",
                timeout=2.0,
            )
        assert out is not None and [b.name for b in out] == ["Adobe"]
        assert seen_headers.get("hibp-api-key") == "key123"

    @pytest.mark.asyncio
    async def test_404_returns_empty_list(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with _mock_client(handler) as client:
            out = await fetch_breaches(
                client,
                "clean@example.com",
                url_template="https://hibp.test/api/v3/breachedaccount/{email}",
                api_key="key123",
                timeout=2.0,
            )
        assert out == []

    @pytest.mark.asyncio
    async def test_other_status_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "bad key"})

        async with _mock_client(handler) as client:
            out = await fetch_breaches(
                client,
                "alice@example.com",
                url_template="https://hibp.test/api/v3/breachedaccount/{email}",
                api_key="bad",
                timeout=2.0,
            )
        assert out is None

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_none(self) -> None:
        async with _mock_client(lambda _r: httpx.Response(500)) as client:
            out = await fetch_breaches(
                client,
                "alice@example.com",
                url_template="https://hibp.test/api/v3/breachedaccount/{email}",
                api_key="",
                timeout=2.0,
            )
        assert out is None

    @pytest.mark.asyncio
    async def test_transport_error_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        async with _mock_client(handler) as client:
            out = await fetch_breaches(
                client,
                "alice@example.com",
                url_template="https://hibp.test/api/v3/breachedaccount/{email}",
                api_key="key",
                timeout=0.1,
            )
        assert out is None


class TestEnrichEmail:
    @pytest.mark.asyncio
    async def test_runs_both_lookups_in_parallel(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rdap" in url:
                return httpx.Response(
                    200,
                    json={
                        "ldhName": "example.com",
                        "entities": [
                            {
                                "roles": ["registrar"],
                                "vcardArray": [
                                    "vcard",
                                    [["fn", {}, "text", "ExampleReg LLC"]],
                                ],
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json=[{"Name": "Adobe", "Title": "Adobe", "DataClasses": []}],
            )

        async with _mock_client(handler) as client:
            result = await enrich_email(
                client,
                "alice@example.com",
                whois_url_template="https://rdap.test/domain/{domain}",
                hibp_url_template="https://hibp.test/breachedaccount/{email}",
                hibp_api_key="key",
                timeout=2.0,
            )
        assert result.email == "alice@example.com"
        assert result.domain == "example.com"
        assert result.whois is not None and result.whois.registrar == "ExampleReg LLC"
        assert result.breaches is not None and [b.name for b in result.breaches] == ["Adobe"]
        assert result.breaches_disabled is False

    @pytest.mark.asyncio
    async def test_disables_hibp_when_no_api_key(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            # WHOIS is the only call we expect to see.
            return httpx.Response(200, json={"ldhName": "example.com"})

        async with _mock_client(handler) as client:
            result = await enrich_email(
                client,
                "alice@example.com",
                whois_url_template="https://rdap.test/domain/{domain}",
                hibp_url_template="https://hibp.test/breachedaccount/{email}",
                hibp_api_key="",
                timeout=2.0,
            )
        assert result.breaches is None
        assert result.breaches_disabled is True
        assert result.whois is not None and result.whois.domain == "example.com"


# ---------------------------------------------------------------------------
# Endpoint integration tests (FastAPI TestClient).
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
def _reset_state() -> Iterator[None]:
    """Wipe the in-memory rate-limit bucket between every test.

    The DB-side admin-settings reset only fires when the schema
    actually exists (it doesn't for the pure-module unit tests
    above which never touch the TestClient).
    """
    enrichment_mod.GLOBAL_LIMITER.reset()
    _safe_clear_overrides()
    yield
    enrichment_mod.GLOBAL_LIMITER.reset()
    _safe_clear_overrides()


def _safe_clear_overrides() -> None:
    try:
        from app.auth import db_connection

        with db_connection() as conn:
            conn.execute("DELETE FROM admin_settings")
    except Exception:
        return
    admin_settings_mod._invalidate_cache()


def _enable_enrichment() -> None:
    admin_settings_mod.set_override("enable_email_enrichment", 1, actor="rootadmin")


def _stub_enrich(
    monkeypatch: pytest.MonkeyPatch,
    *,
    whois: WhoisRecord | None,
    breaches: list[Breach] | None,
    breaches_disabled: bool = False,
) -> list[str]:
    """Replace ``enrichment.enrich_email`` with a stub that records the
    emails it was called with and returns canned data. Returns the
    list (mutated by each call) so tests can assert call counts."""
    seen: list[str] = []

    async def stub(
        _client: httpx.AsyncClient,
        email: str,
        **_kwargs: Any,
    ) -> EnrichmentResult:
        seen.append(email)
        return EnrichmentResult(
            email=email,
            domain=email.rsplit("@", 1)[1],
            whois=whois,
            breaches=breaches,
            breaches_disabled=breaches_disabled,
        )

    monkeypatch.setattr(enrichment_mod, "enrich_email", stub)
    return seen


def test_endpoint_disabled_when_knob_off(client: TestClient, admin_token: str) -> None:
    r = client.post(
        "/api/enrich/email",
        headers=_hdr(admin_token),
        json={"email": "alice@example.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"disabled": True, "rate_limited": False, "result": None}


def test_endpoint_requires_auth(client: TestClient) -> None:
    r = client.post("/api/enrich/email", json={"email": "alice@example.com"})
    assert r.status_code == 401


def test_endpoint_rejects_invalid_email(
    client: TestClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_enrichment()
    _stub_enrich(monkeypatch, whois=None, breaches=None)
    r = client.post(
        "/api/enrich/email",
        headers=_hdr(admin_token),
        json={"email": "not-an-email"},
    )
    assert r.status_code == 400


def test_endpoint_returns_result_when_enabled(
    client: TestClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_enrichment()
    seen = _stub_enrich(
        monkeypatch,
        whois=WhoisRecord(
            domain="example.com",
            registrar="ExampleReg",
            created=None,
            expires=None,
            updated=None,
            nameservers=(),
            status=(),
        ),
        breaches=[
            Breach(
                name="Adobe",
                title="Adobe",
                domain=None,
                breach_date=None,
                added_date=None,
                pwn_count=None,
                data_classes=(),
                is_verified=True,
                is_fabricated=False,
                is_sensitive=False,
            )
        ],
    )
    r = client.post(
        "/api/enrich/email",
        headers=_hdr(admin_token),
        json={"email": "Alice@Example.com"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["disabled"] is False
    result = payload["result"]
    assert result["email"] == "alice@example.com"
    assert result["domain"] == "example.com"
    assert result["whois"]["registrar"] == "ExampleReg"
    assert [b["name"] for b in result["breaches"]] == ["Adobe"]
    # Stub was called with the *normalised* (lowercased) email.
    assert seen == ["alice@example.com"]


def test_endpoint_rate_limits_per_member(
    client: TestClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_enrichment()
    admin_settings_mod.set_override("enrichment_rate_per_min", 2, actor="rootadmin")
    _stub_enrich(monkeypatch, whois=None, breaches=None)
    body = {"email": "alice@example.com"}
    headers = _hdr(admin_token)
    assert client.post("/api/enrich/email", headers=headers, json=body).status_code == 200
    assert client.post("/api/enrich/email", headers=headers, json=body).status_code == 200
    third = client.post("/api/enrich/email", headers=headers, json=body)
    assert third.status_code == 429
    assert "rate limit" in third.json()["detail"].lower()
