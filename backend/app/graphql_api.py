"""Phase 8c: GraphQL gateway alongside the REST/SSE surface.

A focused subset of the REST API is exposed through a Strawberry-served
schema mounted at ``/api/graphql``. The goal is *alternate transport*,
not feature parity — operators who want to drive Reckora from a
third-party dashboard (Hasura, Apollo Client, a custom CLI) get a
typed query interface without forcing them to glue together SSE
streams. The bundled SPA continues to talk to the REST endpoints; this
module is opt-in via the ``enable_graphql_gateway`` admin knob and
adds zero overhead to the default deployment.

What the schema covers
----------------------
Queries (read-only, every resolver gates on the same session cookie /
Bearer token used by REST):

* ``health`` — same payload as ``GET /api/health``.
* ``me`` — current authenticated member.
* ``sites(limit, tags)`` — site catalog descriptors (no probe).
* ``permuteStrategies`` — Phase 6 permutation strategy registry.
* ``history(username, limit)`` — caller's recent scans.
* ``historyItem(scanId)`` — single scan with full result rows.
* ``adminSettings`` — admin-allowlist knob registry + values
  (admin-only).

Mutation:

* ``search(input)`` — synchronous version of ``POST /api/search``.
  Drains the SSE stream into a single response so a GraphQL client
  doesn't need to process server-sent events. Cache + history
  recording continue to work because the underlying ``stream_checks``
  pipeline is unchanged.

What is intentionally **not** exposed
-------------------------------------
* SSE streaming. ``search`` is collected into a list — clients that
  need progress events should use the REST endpoint.
* Graph search, enrichment, reverse-image, ML retrain. These are
  niche OSINT operations that already have stable REST shapes; adding
  them to GraphQL is gated on operator demand.
* Admin mutations (member CRUD, settings PATCH, ML retrain). The
  GraphQL surface is read-mostly; mutating endpoints stay on REST so
  there is one canonical write path.

Auth
----
Strawberry's ``context_getter`` injects the FastAPI ``Request`` into
every resolver. Each resolver re-runs ``main.current_member`` (and
``current_admin`` for admin queries) so auth behaviour is byte-for-
byte identical to the REST path — same cookie, same Bearer token,
same 401 / 403 semantics rendered as GraphQL error extensions.

Knob
----
The route is mounted unconditionally (so the schema is reachable for
introspection by automated GraphQL tooling), but every resolver
checks ``effective_int("enable_graphql_gateway")`` first and raises a
GraphQL error with extension code ``DISABLED`` when the knob is off.
This avoids the fragility of remounting routers in response to admin
edits while still keeping the gateway off-by-default.
"""

from __future__ import annotations

from typing import Any

import strawberry
from fastapi import HTTPException, Request
from strawberry.fastapi import GraphQLRouter

from . import admin_settings as admin_settings_mod
from . import history as history_mod
from . import host_rate_limit as host_rate_limit_mod
from . import permutations as permutations_mod
from . import proxy as proxy_mod
from . import proxy_pool as proxy_pool_mod
from .config import settings
from .identity import ALL_IDENTITY_TYPES, DEFAULT_IDENTITY_TYPE
from .sites import load_catalog

# ---------------------------------------------------------------------------
# Gate helpers — every resolver runs both gates before doing real work.
# ---------------------------------------------------------------------------


class GraphQLDisabledError(Exception):
    """Raised when ``enable_graphql_gateway`` is 0.

    Strawberry surfaces the exception's message in the GraphQL response
    under ``errors[].message``; callers can also inspect the
    ``extensions.code`` field which we set to ``DISABLED`` so a SPA
    can distinguish "feature off" from "auth required" without
    string-matching the message.
    """


def _ensure_enabled() -> None:
    if admin_settings_mod.effective_int("enable_graphql_gateway") != 1:
        raise GraphQLDisabledError(
            "GraphQL gateway is disabled (set enable_graphql_gateway=1 to enable)"
        )


async def _auth_member(request: Request) -> Any:
    """Re-run the REST auth dependency for a GraphQL resolver.

    Imported lazily to dodge the circular import: ``main`` imports
    this module via ``mount_graphql``, and importing ``main`` from
    here at module load would loop. The lazy import inside the
    resolver is cheap because ``main`` is already loaded by the time
    any resolver runs.
    """
    from . import main as main_mod

    return await main_mod.current_member(
        request,
        reckora_session=request.cookies.get(main_mod.SESSION_COOKIE),
    )


async def _auth_admin(request: Request) -> Any:
    member = await _auth_member(request)
    if member.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return member


# ---------------------------------------------------------------------------
# GraphQL types — keep these flat so queries are easy to write by hand.
# ---------------------------------------------------------------------------


@strawberry.type
class HealthInfo:
    status: str
    version: str
    sites: int
    proxy_active: bool
    proxy_pool_active: bool
    proxy_pool_configured: int
    host_rate_limit_enabled: bool
    host_rate_per_min: int


@strawberry.type
class MemberInfo:
    username: str
    role: str
    created_at: int


@strawberry.type
class SiteInfo:
    name: str
    url_main: str
    tags: list[str]
    alexa_rank: int | None


@strawberry.type
class PermuteStrategy:
    key: str
    title: str
    description: str


@strawberry.type
class CheckResultInfo:
    site: str
    status: str
    url: str
    url_main: str
    tags: list[str]
    http_status: int | None
    elapsed_ms: int | None
    error: str | None
    confidence: int
    signals: list[str]
    attempts: int
    verified: bool | None
    ml_confidence: int | None


@strawberry.type
class SearchResponse:
    total: int
    cached: bool
    scan_id: int | None
    results: list[CheckResultInfo]


@strawberry.type
class HistoryEntry:
    id: int
    member: str
    username: str
    mode: str
    started_at: int
    finished_at: int
    duration_ms: int
    site_count: int
    claimed: int
    free: int
    blocked: int
    unknown: int
    error_count: int


@strawberry.type
class HistoryItem:
    summary: HistoryEntry
    results: list[CheckResultInfo]


@strawberry.type
class AdminKnobInfo:
    key: str
    value: int
    minimum: int
    maximum: int
    description: str


# ---------------------------------------------------------------------------
# Input types
# ---------------------------------------------------------------------------


@strawberry.input
class SearchInput:
    username: str
    tags: list[str] = strawberry.field(default_factory=list)
    limit: int = 200
    identity_type: str = DEFAULT_IDENTITY_TYPE


# ---------------------------------------------------------------------------
# Query / Mutation roots
# ---------------------------------------------------------------------------


def _result_to_info(payload: dict[str, Any]) -> CheckResultInfo:
    return CheckResultInfo(
        site=payload.get("site", ""),
        status=payload.get("status", ""),
        url=payload.get("url", ""),
        url_main=payload.get("url_main", ""),
        tags=list(payload.get("tags") or []),
        http_status=payload.get("http_status"),
        elapsed_ms=payload.get("elapsed_ms"),
        error=payload.get("error"),
        confidence=int(payload.get("confidence") or 0),
        signals=list(payload.get("signals") or []),
        attempts=int(payload.get("attempts") or 1),
        verified=payload.get("verified"),
        ml_confidence=payload.get("ml_confidence"),
    )


@strawberry.type
class Query:
    @strawberry.field
    async def health(self, info: strawberry.Info[dict[str, Any]]) -> HealthInfo:
        _ensure_enabled()
        request: Request = info.context["request"]
        await _auth_member(request)
        catalog = load_catalog()
        return HealthInfo(
            status="ok",
            version=__import__("app").__version__,
            sites=len(catalog.sites),
            proxy_active=bool(proxy_mod.proxy_url_or_none()),
            proxy_pool_active=proxy_pool_mod.is_enabled(),
            proxy_pool_configured=len(proxy_pool_mod.get_pool().urls),
            host_rate_limit_enabled=host_rate_limit_mod.is_enabled(),
            host_rate_per_min=admin_settings_mod.effective_int("host_rate_per_min"),
        )

    @strawberry.field
    async def me(self, info: strawberry.Info[dict[str, Any]]) -> MemberInfo:
        _ensure_enabled()
        request: Request = info.context["request"]
        member = await _auth_member(request)
        return MemberInfo(
            username=member.username,
            role=member.role,
            created_at=member.created_at,
        )

    @strawberry.field
    async def sites(
        self,
        info: strawberry.Info[dict[str, Any]],
        limit: int | None = None,
        tags: list[str] | None = None,
    ) -> list[SiteInfo]:
        _ensure_enabled()
        request: Request = info.context["request"]
        await _auth_member(request)
        catalog = load_catalog()
        sites = catalog.filtered(tags=tags or [], limit=limit or len(catalog.sites))
        return [
            SiteInfo(
                name=s.name,
                url_main=s.url_main,
                tags=list(s.tags),
                alexa_rank=s.alexa_rank,
            )
            for s in sites
        ]

    @strawberry.field
    async def permute_strategies(
        self, info: strawberry.Info[dict[str, Any]]
    ) -> list[PermuteStrategy]:
        _ensure_enabled()
        request: Request = info.context["request"]
        await _auth_member(request)
        return [
            PermuteStrategy(key=s["key"], title=s["title"], description=s["description"])
            for s in permutations_mod.describe_strategies()
        ]

    @strawberry.field
    async def history(
        self,
        info: strawberry.Info[dict[str, Any]],
        username: str | None = None,
        limit: int = 50,
    ) -> list[HistoryEntry]:
        _ensure_enabled()
        request: Request = info.context["request"]
        member = await _auth_member(request)
        if not settings.enable_scan_history:
            return []
        bounded = max(1, min(limit, 200))
        rows = history_mod.list_scans(member=member.username, username=username, limit=bounded)
        return [_history_entry_from_summary(s.to_dict()) for s in rows]

    @strawberry.field
    async def history_item(
        self, info: strawberry.Info[dict[str, Any]], scan_id: int
    ) -> HistoryItem | None:
        _ensure_enabled()
        request: Request = info.context["request"]
        member = await _auth_member(request)
        if not settings.enable_scan_history:
            return None
        out = history_mod.get_scan(scan_id, member.username)
        if out is None:
            return None
        summary, results = out
        return HistoryItem(
            summary=_history_entry_from_summary(summary.to_dict()),
            results=[_result_to_info(r) for r in results],
        )

    @strawberry.field
    async def admin_settings(self, info: strawberry.Info[dict[str, Any]]) -> list[AdminKnobInfo]:
        _ensure_enabled()
        request: Request = info.context["request"]
        await _auth_admin(request)
        effective = admin_settings_mod.effective_map()
        return [
            AdminKnobInfo(
                key=k.key,
                value=int(effective.get(k.key, 0)),
                minimum=k.minimum,
                maximum=k.maximum,
                description=k.description,
            )
            for k in admin_settings_mod.KNOBS
        ]


def _history_entry_from_summary(d: dict[str, Any]) -> HistoryEntry:
    return HistoryEntry(
        id=int(d["id"]),
        member=str(d["member"]),
        username=str(d["username"]),
        mode=str(d["mode"]),
        started_at=int(d["started_at"]),
        finished_at=int(d["finished_at"]),
        duration_ms=int(d.get("duration_ms") or 0),
        site_count=int(d["site_count"]),
        claimed=int(d.get("claimed") or 0),
        free=int(d.get("free") or 0),
        blocked=int(d.get("blocked") or 0),
        unknown=int(d.get("unknown") or 0),
        error_count=int(d.get("error_count") or 0),
    )


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def search(
        self,
        info: strawberry.Info[dict[str, Any]],
        input: SearchInput,
    ) -> SearchResponse:
        """Synchronous GraphQL counterpart to ``POST /api/search``.

        Reuses :func:`app.checker.stream_checks` to drive the same
        per-site classifier pipeline the SSE endpoint uses, and
        records to scan history exactly the same way. The only
        difference is the wire format — clients get one JSON
        response instead of an SSE stream.
        """
        _ensure_enabled()
        request: Request = info.context["request"]
        member = await _auth_member(request)

        # Validate identity_type up front so we surface a clean
        # GraphQL error instead of a checker-level traceback.
        if input.identity_type not in ALL_IDENTITY_TYPES:
            raise ValueError(
                f"unknown identity_type {input.identity_type!r} "
                f"(allowed: {sorted(ALL_IDENTITY_TYPES)})"
            )

        from . import main as main_mod
        from .checker import stream_checks

        catalog = load_catalog()
        sites = catalog.filtered(
            tags=input.tags,
            limit=max(1, min(input.limit, 3000)),
            identity_type=input.identity_type,
        )
        if not sites:
            return SearchResponse(total=0, cached=False, scan_id=None, results=[])

        started_at = history_mod.now_unix()
        collected: list[dict[str, Any]] = []
        async with main_mod._new_async_client() as client:
            async for result in stream_checks(
                sites,
                input.username,
                identity_type=input.identity_type,
                client=client,
            ):
                collected.append(result.to_dict())

        scan_id = history_mod.record_scan(
            member=member.username,
            username=input.username,
            mode="list",
            started_at=started_at,
            finished_at=history_mod.now_unix(),
            site_count=len(sites),
            results=collected,
        )
        return SearchResponse(
            total=len(collected),
            cached=False,
            scan_id=scan_id,
            results=[_result_to_info(r) for r in collected],
        )


# ---------------------------------------------------------------------------
# Router factory — called once from main.py at app construction time.
# ---------------------------------------------------------------------------


def _context_getter(request: Request) -> dict[str, Any]:
    return {"request": request}


def build_router() -> GraphQLRouter[dict[str, Any], None]:
    """Construct the Strawberry router. Kept in a factory so tests can
    rebuild the schema after monkeypatching settings without leaking
    state across modules.
    """
    schema = strawberry.Schema(query=Query, mutation=Mutation)
    return GraphQLRouter(schema, context_getter=_context_getter)


def is_available() -> bool:
    """Report whether the gateway is currently serving requests.

    The router itself is always mounted; this function is the single
    source of truth for "is the GraphQL surface active right now". It
    is consumed by the public ``GET /api/health`` payload so an
    operator dashboard can detect when the knob is on without trying
    a query.
    """
    try:
        return admin_settings_mod.effective_int("enable_graphql_gateway") == 1
    except Exception:
        return int(settings.enable_graphql_gateway) == 1
