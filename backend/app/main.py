"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import __version__
from . import admin_settings as admin_settings_mod
from . import auth as auth_mod
from . import exports as exports_mod
from . import history as history_mod
from . import initial_hit as initial_hit_mod
from . import permutations as permutations_mod
from . import proxy as proxy_mod
from . import site_overrides as overrides_mod
from . import transport as transport_mod
from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    Member,
)
from .cache import TTLCache
from .checker import is_valid_username, stream_checks
from .config import settings
from .identity import (
    ALL_IDENTITY_TYPES,
    DEFAULT_IDENTITY_TYPE,
    IdentityType,
    is_valid_identifier,
    normalise_identifier,
)
from .sites import SiteCatalog, load_catalog, reload_catalog, site_to_raw

logger = logging.getLogger("reckora")

limiter = Limiter(key_func=get_remote_address, default_limits=[])
cache = TTLCache(max_entries=settings.cache_max_entries, ttl_seconds=settings.cache_ttl_seconds)

app = FastAPI(
    title="Reckora",
    version=__version__,
    description="Username footprint scanner — modern OSINT lookup.",
    default_response_class=ORJSONResponse,
)

app.state.limiter = limiter

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")] or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# Vite content-hashes filenames in /assets/, so we can pin them for a year.
# HTML must always be revalidated so a fresh deploy's new asset hashes reach
# every browser tab on the next reload, not after the cache TTL expires.
_IMMUTABLE_ASSETS = "public, max-age=31536000, immutable"
_NO_CACHE_HTML = "no-cache, no-store, must-revalidate"
_FAVICON_CACHE = "public, max-age=3600"


@app.middleware("http")
async def cache_control(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    # Don't clobber headers a route handler already set (e.g. the SSE search
    # stream pins `Cache-Control: no-cache, no-transform` itself).
    if "cache-control" in response.headers:
        return response
    path = request.url.path
    # /api/* responses manage their own caching; leave them alone.
    if path.startswith("/api/"):
        return response
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = _IMMUTABLE_ASSETS
    elif path == "/favicon.svg":
        response.headers["Cache-Control"] = _FAVICON_CACHE
    else:
        response.headers["Cache-Control"] = _NO_CACHE_HTML
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
async def _startup() -> None:
    auth_mod.init_db()
    auth_mod.seed_admin_from_env()
    # Force HMAC secret to be created/loaded eagerly so first login is fast.
    auth_mod._ensure_session_secret()
    # Phase 10a: hourly TTL sweep over /data/initial_pdfs. The task is
    # a no-op when the directory is empty so it's cheap to leave on
    # even with the master switch off. Stash the handle on the app
    # state so it isn't garbage collected mid-flight (RUF006).
    app.state.initial_pdf_janitor = asyncio.create_task(initial_hit_mod.janitor_loop())


# Phase 8c: mount the GraphQL gateway alongside REST. Always-on mount
# keeps the schema reachable for tooling (introspection); each
# resolver gates on ``enable_graphql_gateway`` so the runtime knob
# decides whether queries actually run. The import is wrapped so the
# default deployment, which doesn't ship the [gql] extra, still
# starts up cleanly — `/api/graphql` returns 503 in that case.
try:
    from . import graphql_api as _graphql_api_mod

    app.include_router(_graphql_api_mod.build_router(), prefix="/api/graphql")
except ImportError:  # pragma: no cover - exercised only when [gql] is absent

    @app.api_route("/api/graphql", methods=["GET", "POST"])
    async def _graphql_unavailable() -> ORJSONResponse:
        return ORJSONResponse(
            status_code=503,
            content={
                "detail": (
                    "GraphQL gateway not installed; reinstall the backend with the [gql] extra"
                )
            },
        )


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(_: Request, exc: RateLimitExceeded) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=429,
        content={"detail": "rate limit exceeded, slow down"},
    )


def get_catalog() -> SiteCatalog:
    return load_catalog()


class SearchRequest(BaseModel):
    # Phase 5a: ``username`` is now a generic identifier (username /
    # email / phone / GAIA / Yandex / Skype). The field name is kept
    # for backwards compat with the pre-5a SSE schema; the
    # authoritative shape is governed by ``identity_type``.
    username: str = Field(..., min_length=1, max_length=320)
    tags: list[str] = Field(default_factory=list)
    # Phase 5g: cap raised from 600 → 3,000 so a single request can probe
    # the entire catalogue (currently 2,506 sites; Phase 6a regen will push
    # this to ~3,000+). The default stays at 200 so casual users do not
    # accidentally trigger a multi-minute scan; the SPA exposes a picker
    # for higher tiers (250 / 500 / 1000 / all-sites) with progress
    # streamed via SSE.
    limit: int = Field(default=200, ge=1, le=3000)
    identity_type: IdentityType = Field(default=DEFAULT_IDENTITY_TYPE)

    @field_validator("identity_type")
    @classmethod
    def _validate_identity_type(cls, v: str) -> str:
        if v not in ALL_IDENTITY_TYPES:
            raise ValueError(f"unknown identity_type {v!r} (allowed: {sorted(ALL_IDENTITY_TYPES)})")
        return v

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        # Per-type validation happens in ``model_validator`` once both
        # ``username`` and ``identity_type`` have been parsed. Here we
        # only enforce the universal upper bound (320 = the max RFC
        # 5321 email length); per-type bounds are tighter.
        v = v.strip()
        if not v:
            raise ValueError("identifier must not be empty")
        return v

    @model_validator(mode="after")
    def _normalise_and_validate_per_identity(self) -> SearchRequest:
        identity_type = self.identity_type
        normalised = normalise_identifier(self.username, identity_type)
        if not is_valid_identifier(normalised, identity_type):
            raise ValueError(
                f"invalid {identity_type} (value did not match the {identity_type} format)"
            )
        # Re-assign the normalised value so the rest of the request
        # pipeline (cache key, history record, SSE payload) sees the
        # canonical form.
        object.__setattr__(self, "username", normalised)
        return self


# ---------------------------------------------------------------------------
# auth dependencies
# ---------------------------------------------------------------------------


async def current_member(
    request: Request,
    reckora_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Member:
    token = reckora_session
    if token is None:
        # fallback to Authorization: Bearer <token> for non-browser clients (curl/CI)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    member = auth_mod.verify_session(token or "")
    if member is None:
        raise HTTPException(status_code=401, detail="login required")
    return member


async def current_admin(member: Member = Depends(current_member)) -> Member:
    if member.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return member


# ---------------------------------------------------------------------------
# auth endpoints
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class MemberOut(BaseModel):
    username: str
    role: str
    created_at: int

    @classmethod
    def of(cls, m: Member) -> MemberOut:
        return cls(username=m.username, role=m.role, created_at=m.created_at)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )


@app.post("/api/auth/login")
@limiter.limit(settings.login_rate_limit)
async def login(request: Request, body: LoginRequest, response: Response) -> dict[str, Any]:
    record = auth_mod.get_member_with_hash(body.username.strip())
    if record is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    member, password_hash = record
    if not auth_mod.verify_password(body.password, password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = auth_mod.issue_session(member)
    _set_session_cookie(response, token)
    return {"member": MemberOut.of(member).model_dump(), "token": token}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(member: Member = Depends(current_member)) -> dict[str, Any]:
    return {"member": MemberOut.of(member).model_dump()}


# ---------------------------------------------------------------------------
# admin: member management
# ---------------------------------------------------------------------------


class MemberCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(default="member")

    @field_validator("username")
    @classmethod
    def _val_username(cls, v: str) -> str:
        v = v.strip()
        if not auth_mod.is_valid_username(v):
            raise ValueError(
                "username must be 3-32 chars, start with letter/digit, "
                "allowed chars: letters digits _ . -",
            )
        return v

    @field_validator("role")
    @classmethod
    def _val_role(cls, v: str) -> str:
        if v not in auth_mod.ROLES:
            raise ValueError(f"role must be one of {sorted(auth_mod.ROLES)}")
        return v


class MemberPatch(BaseModel):
    role: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)

    @field_validator("role")
    @classmethod
    def _val_role(cls, v: str | None) -> str | None:
        if v is not None and v not in auth_mod.ROLES:
            raise ValueError(f"role must be one of {sorted(auth_mod.ROLES)}")
        return v


@app.get("/api/admin/members")
async def admin_list_members(_: Member = Depends(current_admin)) -> dict[str, Any]:
    return {"members": [MemberOut.of(m).model_dump() for m in auth_mod.list_members()]}


@app.post("/api/admin/members", status_code=201)
async def admin_create_member(
    body: MemberCreate,
    _: Member = Depends(current_admin),
) -> dict[str, Any]:
    try:
        m = auth_mod.create_member(body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"member": MemberOut.of(m).model_dump()}


@app.delete("/api/admin/members/{username}")
async def admin_delete_member(
    username: str,
    admin: Member = Depends(current_admin),
) -> dict[str, Any]:
    if username.lower() == admin.username.lower():
        raise HTTPException(status_code=400, detail="cannot delete the currently signed-in admin")
    target = auth_mod.get_member(username)
    if target is None:
        raise HTTPException(status_code=404, detail="member not found")
    if target.role == "admin" and auth_mod.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    if not auth_mod.delete_member(username):
        raise HTTPException(status_code=404, detail="member not found")
    return {"ok": True}


@app.patch("/api/admin/members/{username}")
async def admin_patch_member(
    username: str,
    body: MemberPatch,
    admin: Member = Depends(current_admin),
) -> dict[str, Any]:
    target = auth_mod.get_member(username)
    if target is None:
        raise HTTPException(status_code=404, detail="member not found")
    if (
        body.role is not None
        and target.role == "admin"
        and body.role != "admin"
        and auth_mod.count_admins() <= 1
    ):
        raise HTTPException(status_code=400, detail="cannot demote the last admin")
    try:
        updated = auth_mod.update_member(username, role=body.role, password=body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="member not found")
    return {"member": MemberOut.of(updated).model_dump()}


# ---------------------------------------------------------------------------
# public/health + auth-gated catalog & search
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from . import host_rate_limit as host_rate_limit_mod
    from . import proxy_pool as proxy_pool_mod

    payload: dict[str, Any] = {
        "status": "ok",
        "version": __version__,
        "sites": len(get_catalog().sites),
        # Phase 5e: surface proxy state without leaking the URL itself
        # so an external uptime probe can flag a dropped Tor circuit.
        "proxy": proxy_mod.status(),
        # Phase 8b: descriptors for the multi-proxy pool + per-host
        # token bucket. Pool members are reported by scheme only — the
        # full URL stays env-only so a leaked health probe never
        # exposes proxy credentials.
        "proxy_pool": proxy_pool_mod.status(),
        "host_rate_limit": host_rate_limit_mod.status(),
    }
    # Phase 8c: surface gateway availability so a dashboard can spot
    # the alternate transport without trying a query. The `available`
    # bit is False when either the [gql] extra is not installed *or*
    # the admin knob is off.
    try:
        from . import graphql_api as graphql_api_mod

        payload["graphql"] = {
            "available": graphql_api_mod.is_available(),
            "installed": True,
        }
    except ImportError:
        payload["graphql"] = {"available": False, "installed": False}
    # Phase 8d: surface share-link state so a dashboard can detect
    # whether public shares are currently mintable, plus the
    # configured edge-cache window. The cache window is intentionally
    # public — it leaks no secrets and lets a CDN debugger confirm
    # the response will indeed be cached for the expected duration.
    payload["share_links"] = {
        "enabled": admin_settings_mod.effective_int("enable_share_links") == 1,
        "cache_max_age_sec": admin_settings_mod.effective_int("share_link_cache_max_age_sec"),
    }
    # Phase 9a: surface anti-bot pipeline state so an operator can
    # confirm the impersonation profile + Playwright fallback wiring
    # without having to fire a probe at a known-WAF site.
    from . import anti_bot_transport as anti_bot_mod

    payload["anti_bot"] = anti_bot_mod.status()
    return payload


@app.get("/api/sites")
async def list_sites(
    catalog: SiteCatalog = Depends(get_catalog),
    _: Member = Depends(current_member),
) -> dict[str, Any]:
    return {
        "count": len(catalog.sites),
        "tags": [{"name": n, "count": c} for n, c in catalog.all_tags()],
        "sites": [
            {
                "name": s.name,
                "url_main": s.url_main,
                "tags": list(s.tags),
                "alexa_rank": s.alexa_rank,
            }
            for s in catalog.sites
        ],
    }


def _sse_event(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _new_async_client() -> httpx.AsyncClient:
    """Build the shared httpx client used by all three search streams.

    Goes through :func:`app.transport.build_async_client` so both the
    Phase 5e single-proxy gate *and* the Phase 8b rate-limit-aware
    multi-proxy rotator are uniformly applied. The transport
    transparently falls back to direct egress when neither feature is
    on, so behaviour for the HF Space single-container deployment is
    unchanged.
    """
    return transport_mod.build_async_client()


async def _search_stream(
    req: SearchRequest,
    catalog: SiteCatalog,
    *,
    member: Member | None = None,
) -> AsyncIterator[bytes]:
    identity_type = req.identity_type
    sites = catalog.filtered(tags=req.tags, limit=req.limit, identity_type=identity_type)
    yield _sse_event(
        "start",
        {
            "username": req.username,
            "total": len(sites),
            "identity_type": identity_type,
        },
    )

    if not sites:
        yield _sse_event("done", {"results": 0})
        return

    cache_key = json.dumps(
        {
            "u": req.username,
            "t": sorted(req.tags),
            "l": req.limit,
            "i": identity_type,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        for r in cached:
            yield _sse_event("result", r)
            await asyncio.sleep(0)  # cooperative yield
        yield _sse_event("done", {"results": len(cached), "cached": True})
        return

    started_at = history_mod.now_unix()
    collected: list[dict[str, Any]] = []
    tracker = initial_hit_mod.InitialHitTracker(
        context=initial_hit_mod.ScanContext(
            scan_token=initial_hit_mod.new_scan_token(),
            member=member.username if member is not None else "",
            primary_username=req.username,
            mode="list",
            started_at=started_at,
            sites_total=len(sites),
        ),
    )
    # Phase 1: HTTP/2 + connection pool. Phase 5e: optional proxy.
    async with _new_async_client() as client:
        async for result in stream_checks(
            sites,
            req.username,
            identity_type=identity_type,
            client=client,
        ):
            payload = result.to_dict()
            collected.append(payload)
            yield _sse_event("result", payload)
            if member is not None:
                fired = await tracker.consider(
                    result,
                    is_primary=True,
                    results_so_far=collected,
                )
                if fired is not None:
                    yield _sse_event("initial_pdf_ready", fired)

    cache.set(cache_key, collected)
    scan_id: int | None = None
    if member is not None:
        scan_id = history_mod.record_scan(
            member=member.username,
            username=req.username,
            mode="list",
            started_at=started_at,
            finished_at=history_mod.now_unix(),
            site_count=len(sites),
            results=collected,
        )
    yield _sse_event(
        "done",
        {"results": len(collected), "cached": False, "scan_id": scan_id},
    )


@app.post("/api/search")
@limiter.limit(settings.search_rate_limit)
async def search(
    request: Request,
    body: SearchRequest,
    catalog: SiteCatalog = Depends(get_catalog),
    member: Member = Depends(current_member),
) -> StreamingResponse:
    if not catalog.sites:
        raise HTTPException(503, "site catalog unavailable")
    return StreamingResponse(
        _search_stream(body, catalog, member=member),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Phase 3: graph search — recursive identity discovery + react-flow payload.
# ---------------------------------------------------------------------------


class GraphSearchRequest(SearchRequest):
    """Same as ``SearchRequest`` plus per-call recursion knobs.

    ``depth=0`` collapses to a regular search; ``depth>=1`` runs sub-scans
    on secondary usernames extracted from the primary CLAIMED hits.

    Phase 5a: graph search is username-only — the extractor pulls
    ``username``-shaped child handles out of profile pages, and feeding
    those into an email/phone scan would be category-confused. Non-
    username ``identity_type`` values are rejected at the validator layer.
    """

    # Phase 10d: depth field range bumped from 0-2 to 0-3. Operators
    # who want depth>1 must *also* lift the ``graph_max_depth`` admin
    # knob (default 1) — otherwise the runtime clamps the per-call
    # ``req.depth`` down to the knob value, so existing deployments
    # see no behaviour change unless they opt in.
    depth: int = Field(default=1, ge=0, le=3)
    max_secondary: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _reject_non_username_identity(self) -> GraphSearchRequest:
        if self.identity_type != DEFAULT_IDENTITY_TYPE:
            raise ValueError(
                "graph search supports identity_type='username' only "
                "(got identity_type=" + str(self.identity_type) + ")"
            )
        return self


async def _graph_search_stream(
    req: GraphSearchRequest,
    catalog: SiteCatalog,
    *,
    member: Member | None = None,
) -> AsyncIterator[bytes]:
    # Snapshot the admin overrides once per request so the whole stream
    # observes a consistent set of knobs even if an admin saves new
    # values mid-scan.
    knobs = admin_settings_mod.effective_map()
    effective_max_depth = knobs["graph_max_depth"]
    effective_max_secondary = knobs["graph_max_secondary"]
    effective_max_seed_hits = knobs["graph_max_seed_hits"]
    # Phase 10d knobs — deep-recursion safety net. ``effective_map`` is
    # guaranteed to return every registered knob, but we use ``.get``
    # with the Settings default so unit tests that monkey-patch the
    # admin map don't have to know about every new key.
    secondary_conf_floor = knobs.get(
        "graph_secondary_min_confidence",
        settings.graph_secondary_min_confidence,
    )
    max_total_subscans = knobs.get(
        "graph_max_total_subscans",
        settings.graph_max_total_subscans,
    )

    sites = catalog.filtered(tags=req.tags, limit=req.limit)
    yield _sse_event(
        "start",
        {
            "username": req.username,
            "total": len(sites),
            "depth": min(req.depth, effective_max_depth),
        },
    )
    if not sites:
        yield _sse_event("done", {"results": 0, "edges": 0})
        return

    max_secondary = min(
        req.max_secondary or effective_max_secondary,
        effective_max_secondary,
    )
    effective_depth = min(req.depth, effective_max_depth)

    started_at = history_mod.now_unix()
    primary_results: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    discovered: dict[str, str] = {}  # child_username -> source description
    # Phase 10d: total sub-scan counter + capped counter. The capped
    # counter is surfaced on the terminal ``done`` payload so the SPA
    # can render a "+N candidates skipped (graph_subscan_cap)" badge.
    subscan_count = 0
    capped_subscans = 0
    tracker = initial_hit_mod.InitialHitTracker(
        context=initial_hit_mod.ScanContext(
            scan_token=initial_hit_mod.new_scan_token(),
            member=member.username if member is not None else "",
            primary_username=req.username,
            mode="graph",
            started_at=started_at,
            sites_total=len(sites),
        ),
    )

    async with _new_async_client() as client:
        # ---- depth 0: primary scan ------------------------------------
        # ``current_seeds`` accumulates the parents that are eligible
        # to spawn the *next* generation. We re-use the same list
        # across generations 1..effective_depth so the deep-recursion
        # path is just a tight outer loop on top of the depth-1 logic
        # that pre-dates Phase 10d.
        current_seeds: list[tuple[str, dict[str, Any]]] = []
        async for result in stream_checks(sites, req.username, client=client):
            payload = result.to_dict()
            payload["parent_username"] = None
            primary_results.append(payload)
            yield _sse_event("result", payload)
            if member is not None:
                fired = await tracker.consider(
                    result,
                    is_primary=True,
                    results_so_far=primary_results,
                )
                if fired is not None:
                    yield _sse_event("initial_pdf_ready", fired)
            if (
                effective_depth >= 1
                and result.status.value == "claimed"
                and result.extracted
                and len(current_seeds) < effective_max_seed_hits
            ):
                # Primary CLAIMED hits don't have to clear the
                # ``graph_secondary_min_confidence`` floor — that gate
                # only applies to spawning *deeper* generations. The
                # primary already cleared the Phase 5h
                # ``min_claimed_confidence`` floor by virtue of being
                # CLAIMED at this point.
                current_seeds.append((req.username, payload))

        # ---- depth 1..N: BFS over generations -------------------------
        # Each iteration of the outer loop spawns one generation of
        # sub-scans. We stop early when there are no eligible parents
        # left (e.g. nothing extracted, or every child fell below the
        # confidence floor).
        if effective_depth >= 1 and current_seeds:
            from .extractor import find_secondary_usernames

            for current_depth in range(1, effective_depth + 1):
                if not current_seeds:
                    break
                # Children at the deepest layer never spawn another
                # generation, so we skip the (expensive) extraction
                # pass to keep payload size bounded — matches the
                # pre-Phase-10d depth-1 behaviour exactly.
                child_extract = current_depth < effective_depth

                sub_targets: list[tuple[str, str, dict[str, Any]]] = []
                for parent_username, parent_payload in current_seeds:
                    extracted = parent_payload.get("extracted") or {}
                    yield _sse_event(
                        "extracted",
                        {
                            "parent_username": parent_username,
                            "site": parent_payload["site"],
                            "url": parent_payload["url"],
                            "fields": extracted,
                            "depth": current_depth,
                        },
                    )
                    children_for_parent = 0
                    for child_username, evidence in find_secondary_usernames(
                        extracted, parent_username
                    ):
                        if child_username in discovered:
                            continue
                        if not is_valid_username(child_username):
                            continue
                        if children_for_parent >= max_secondary:
                            break
                        # Phase 10d: enforce the global subscan cap.
                        # Once we've spawned ``max_total_subscans``
                        # we stop accepting *new* candidates and emit
                        # a synthetic SKIPPED row so the SPA can show
                        # the operator that the cap kicked in.
                        if subscan_count + len(sub_targets) >= max_total_subscans:
                            skipped_payload = {
                                "site": "graph_subscan_cap",
                                "status": "skipped",
                                "url": "",
                                "url_main": "",
                                "tags": [],
                                "reason": "graph_subscan_cap",
                                "parent_username": child_username,
                                "discovered_via": parent_username,
                                "depth": current_depth,
                            }
                            primary_results.append(skipped_payload)
                            yield _sse_event("result", skipped_payload)
                            capped_subscans += 1
                            continue
                        discovered[child_username] = evidence
                        edge = {
                            "parent_username": parent_username,
                            "child_username": child_username,
                            "evidence": evidence,
                            "source_site": parent_payload["site"],
                            "depth": current_depth,
                        }
                        edges.append(edge)
                        yield _sse_event("edge", edge)
                        sub_targets.append((parent_username, child_username, parent_payload))
                        children_for_parent += 1

                next_seeds: list[tuple[str, dict[str, Any]]] = []
                for parent_username, child_username, _parent_payload in sub_targets:
                    yield _sse_event(
                        "secondary_start",
                        {
                            "parent_username": parent_username,
                            "child_username": child_username,
                            "total": len(sites),
                            "depth": current_depth,
                        },
                    )
                    async for sub in stream_checks(
                        sites,
                        child_username,
                        client=client,
                        extract=child_extract,
                    ):
                        sub_payload = sub.to_dict()
                        sub_payload["parent_username"] = child_username
                        sub_payload["discovered_via"] = parent_username
                        sub_payload["depth"] = current_depth
                        yield _sse_event("result", sub_payload)
                        primary_results.append(sub_payload)
                        # Phase 10d: gate spawning of the next
                        # generation on the ``graph_secondary_min_confidence``
                        # floor. The Phase 5h floor (default 60) only
                        # gates the row from staying CLAIMED; this
                        # tighter floor (default 80) gates whether a
                        # CLAIMED child gets to spawn yet another
                        # generation, since errors compound across
                        # graph hops.
                        if (
                            current_depth < effective_depth
                            and sub.status.value == "claimed"
                            and sub.extracted
                            and sub.confidence >= secondary_conf_floor
                            and len(next_seeds) < effective_max_seed_hits
                        ):
                            next_seeds.append((child_username, sub_payload))
                    subscan_count += 1

                current_seeds = next_seeds

    # Phase 7a: stitch perceptual-hash edges between CLAIMED hits with
    # near-identical avatars. Runs after the recursive scan finishes so
    # secondary CLAIMED hits are eligible too (their ``parent_username``
    # is set to the child handle by ``_graph_search_stream``). The
    # admin knob is checked again here even though ``check_site`` only
    # populates ``avatar_phash`` when it's on, because the secondary
    # ``stream_checks`` call is invoked with ``extract=False`` and can
    # never produce hashes — so the work below is a no-op for those
    # rows by construction.
    phash_edges_count = 0
    if knobs.get("enable_image_phash"):
        from .imagephash import find_phash_edges

        phash_max_distance = knobs.get(
            "image_phash_max_distance",
            settings.image_phash_max_distance,
        )
        # Build (key, hash) pairs; key is the site name so the SPA can
        # use the same ``site:`` node ids in both the structural and
        # phash-derived edges. Skip non-CLAIMED rows and rows whose
        # extractor pass missed an avatar.
        keyed_hashes: list[tuple[str, str]] = []
        # Map site -> result payload for evidence + parent-username
        # echo on the edge event.
        result_by_site: dict[str, dict[str, Any]] = {}
        for payload in primary_results:
            if payload.get("status") != "claimed":
                continue
            extracted = payload.get("extracted") or {}
            phash = extracted.get("avatar_phash")
            if not phash:
                continue
            site = payload["site"]
            # Two CLAIMED hits on the same site (primary vs secondary)
            # would both register here; keep the first occurrence so
            # the edge keys remain stable and we don't self-edge.
            if site in result_by_site:
                continue
            result_by_site[site] = payload
            keyed_hashes.append((site, phash))

        for site_a, site_b, distance in find_phash_edges(
            keyed_hashes, max_distance=phash_max_distance
        ):
            edge_payload = {
                "site_a": site_a,
                "site_b": site_b,
                "parent_username_a": result_by_site[site_a].get("parent_username"),
                "parent_username_b": result_by_site[site_b].get("parent_username"),
                "hamming": distance,
                "max_distance": phash_max_distance,
            }
            yield _sse_event("phash_edge", edge_payload)
            phash_edges_count += 1

    scan_id: int | None = None
    if member is not None:
        scan_id = history_mod.record_scan(
            member=member.username,
            username=req.username,
            mode="graph",
            started_at=started_at,
            finished_at=history_mod.now_unix(),
            site_count=len(sites),
            results=primary_results,
        )
    yield _sse_event(
        "done",
        {
            "results": len(primary_results),
            "edges": len(edges),
            "phash_edges": phash_edges_count,
            "secondary_count": len(discovered),
            # Phase 10d: surface the (per-scan) deep-recursion stats
            # so the SPA can render a "max depth=N, +K skipped (cap)"
            # badge. ``capped_subscans`` is always 0 at depth=1 with
            # default knobs, so existing behaviour stays observably
            # the same for callers that never bump the depth knob.
            "capped_subscans": capped_subscans,
            "max_depth_reached": effective_depth,
            "subscans_run": subscan_count,
            "scan_id": scan_id,
        },
    )


@app.post("/api/search/graph")
@limiter.limit(settings.search_rate_limit)
async def search_graph(
    request: Request,
    body: GraphSearchRequest,
    catalog: SiteCatalog = Depends(get_catalog),
    member: Member = Depends(current_member),
) -> StreamingResponse:
    if not catalog.sites:
        raise HTTPException(503, "site catalog unavailable")
    return StreamingResponse(
        _graph_search_stream(body, catalog, member=member),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Phase 5c: username permutation search.
# ---------------------------------------------------------------------------


class PermuteSearchRequest(SearchRequest):
    """Run the catalog scan against the base handle and a deterministic
    set of common variants (separators / suffixes / prefixes / leetspeak).

    The base ``username`` is always probed first; the remaining
    candidates come from ``permutations.generate_permutations`` and are
    scanned sequentially over the same httpx client to amortise the
    connection pool across permutations.

    Phase 5a: permutation strategies (separator/suffix/prefix/leetspeak)
    operate on username-shaped strings; they don't make sense for
    emails/phones/etc. Non-username ``identity_type`` values are
    rejected at the validator layer.
    """

    max_permutations: int = Field(default=10, ge=0, le=40)
    strategies: list[str] | None = Field(default=None)

    @model_validator(mode="after")
    def _reject_non_username_identity(self) -> PermuteSearchRequest:
        if self.identity_type != DEFAULT_IDENTITY_TYPE:
            raise ValueError(
                "permutation search supports identity_type='username' only "
                "(got identity_type=" + str(self.identity_type) + ")"
            )
        return self

    @field_validator("strategies")
    @classmethod
    def _val_strategies(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        unknown = [s for s in v if s not in permutations_mod.STRATEGY_KEYS]
        if unknown:
            raise ValueError(
                f"unknown permutation strategy: {unknown[0]} "
                f"(allowed: {sorted(permutations_mod.STRATEGY_KEYS)})"
            )
        # Drop duplicates while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            if s in seen:
                continue
            out.append(s)
            seen.add(s)
        return out


async def _permute_search_stream(
    req: PermuteSearchRequest,
    catalog: SiteCatalog,
    *,
    member: Member | None = None,
) -> AsyncIterator[bytes]:
    sites = catalog.filtered(tags=req.tags, limit=req.limit)

    candidates: list[str] = []
    if req.max_permutations > 0:
        try:
            candidates = permutations_mod.generate_permutations(
                req.username,
                max_count=req.max_permutations,
                strategies=req.strategies,
            )
        except permutations_mod.UnknownStrategyError as e:
            yield _sse_event("error", {"detail": f"unknown strategy: {e}"})
            return

    handles: list[str] = [req.username, *candidates]

    yield _sse_event(
        "start",
        {
            "username": req.username,
            "total": len(sites),
            "candidates": candidates,
            "candidate_count": len(handles),
            "strategies": (
                req.strategies
                if req.strategies is not None
                else list(permutations_mod.DEFAULT_STRATEGY_ORDER)
            ),
        },
    )

    if not sites:
        yield _sse_event("done", {"results": 0, "candidates_run": 0})
        return

    started_at = history_mod.now_unix()
    collected: list[dict[str, Any]] = []
    candidates_run = 0
    tracker = initial_hit_mod.InitialHitTracker(
        context=initial_hit_mod.ScanContext(
            scan_token=initial_hit_mod.new_scan_token(),
            member=member.username if member is not None else "",
            primary_username=req.username,
            mode="permute",
            started_at=started_at,
            sites_total=len(sites),
        ),
    )
    async with _new_async_client() as client:
        for index, handle in enumerate(handles):
            yield _sse_event(
                "candidate_start",
                {
                    "username": handle,
                    "index": index,
                    "total": len(handles),
                    "is_base": handle == req.username,
                },
            )
            claimed_for_handle = 0
            is_base_handle = handle == req.username
            async for result in stream_checks(sites, handle, client=client):
                payload = result.to_dict()
                payload["permutation_username"] = handle
                payload["parent_username"] = req.username
                if result.status == "claimed":
                    claimed_for_handle += 1
                collected.append(payload)
                yield _sse_event("result", payload)
                if member is not None and is_base_handle:
                    fired = await tracker.consider(
                        result,
                        is_primary=True,
                        results_so_far=collected,
                    )
                    if fired is not None:
                        yield _sse_event("initial_pdf_ready", fired)
            yield _sse_event(
                "candidate_done",
                {
                    "username": handle,
                    "index": index,
                    "claimed": claimed_for_handle,
                    "total": len(sites),
                },
            )
            candidates_run += 1

    scan_id: int | None = None
    if member is not None:
        scan_id = history_mod.record_scan(
            member=member.username,
            username=req.username,
            mode="permute",
            started_at=started_at,
            finished_at=history_mod.now_unix(),
            site_count=len(sites),
            results=collected,
        )
    yield _sse_event(
        "done",
        {
            "results": len(collected),
            "candidates_run": candidates_run,
            "scan_id": scan_id,
        },
    )


@app.post("/api/search/permute")
@limiter.limit(settings.search_rate_limit)
async def search_permute(
    request: Request,
    body: PermuteSearchRequest,
    catalog: SiteCatalog = Depends(get_catalog),
    member: Member = Depends(current_member),
) -> StreamingResponse:
    if not catalog.sites:
        raise HTTPException(503, "site catalog unavailable")
    return StreamingResponse(
        _permute_search_stream(body, catalog, member=member),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/permute/strategies")
async def permute_strategies(
    _: Member = Depends(current_member),
) -> dict[str, Any]:
    """Static descriptor list for the SPA's permutation strategy selector."""
    return {"strategies": permutations_mod.describe_strategies()}


# ---------------------------------------------------------------------------
# Phase 4: admin live DB editor — site overrides on top of the bundled
# sites.json. Edits are persisted in the ``site_overrides`` SQLite table
# and merged into the runtime catalog by ``sites.load_catalog``. After
# every mutation we drop the cached catalog so the next probe sees the
# new state without a server restart.
# ---------------------------------------------------------------------------


class SiteUpsert(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    operation: str = Field(default="upsert")
    patch: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operation")
    @classmethod
    def _val_op(cls, v: str) -> str:
        if v not in overrides_mod.ALLOWED_OPS:
            raise ValueError(f"operation must be one of {sorted(overrides_mod.ALLOWED_OPS)}")
        return v


def _serialise_site_for_admin(site: Any) -> dict[str, Any]:
    """Project a runtime ``Site`` to the admin-API JSON shape."""
    return {
        "name": site.name,
        "source": site.source,
        "disabled": site.disabled,
        "engine": site.engine,
        "tags": list(site.tags),
        "raw": site_to_raw(site),
    }


@app.get("/api/admin/sites")
async def admin_list_sites(_: Member = Depends(current_admin)) -> dict[str, Any]:
    """Return the merged runtime catalog (base + overrides) annotated
    with each site's provenance."""
    if not settings.enable_site_overrides:
        raise HTTPException(404, "site overrides disabled")
    catalog = load_catalog()
    overrides = {o.name.lower(): o for o in overrides_mod.list_overrides()}
    sites: list[dict[str, Any]] = []
    for s in catalog.sites:
        entry = _serialise_site_for_admin(s)
        ov = overrides.get(s.name.lower())
        if ov is not None:
            entry["override"] = ov.to_dict()
        sites.append(entry)
    return {
        "sites": sites,
        "total": len(sites),
        "custom_count": sum(1 for s in catalog.sites if s.source == "custom"),
        "overridden_count": sum(1 for s in catalog.sites if s.source == "override"),
        "disabled_count": sum(1 for s in catalog.sites if s.disabled),
    }


@app.get("/api/admin/sites/{name}")
async def admin_get_site(
    name: str,
    _: Member = Depends(current_admin),
) -> dict[str, Any]:
    if not settings.enable_site_overrides:
        raise HTTPException(404, "site overrides disabled")
    catalog = load_catalog()
    site = catalog.by_name(name)
    if site is None:
        raise HTTPException(404, "site not found")
    entry = _serialise_site_for_admin(site)
    ov = overrides_mod.get_override(name)
    if ov is not None:
        entry["override"] = ov.to_dict()
    return entry


@app.post("/api/admin/sites", status_code=201)
async def admin_upsert_site(
    body: SiteUpsert,
    member: Member = Depends(current_admin),
) -> dict[str, Any]:
    if not settings.enable_site_overrides:
        raise HTTPException(404, "site overrides disabled")
    try:
        clean_patch = overrides_mod.sanitize_patch(body.patch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.operation == "upsert":
        # ``url`` must be templated for new sites; for existing sites we
        # let the merge keep the base url unless the patch overrides it.
        catalog = load_catalog()
        is_new = catalog.by_name(body.name) is None
        if is_new and ("{username}" not in str(clean_patch.get("url", ""))):
            raise HTTPException(400, "new sites require a url with `{username}` placeholder")
    overrides_mod.upsert_override(
        name=body.name,
        operation=body.operation,  # type: ignore[arg-type]
        patch=clean_patch,
        actor=member.username,
    )
    catalog = reload_catalog()
    site = catalog.by_name(body.name)
    return {
        "ok": True,
        "name": body.name,
        "operation": body.operation,
        "site": _serialise_site_for_admin(site) if site else None,
        "catalog_size": len(catalog.sites),
    }


@app.delete("/api/admin/sites/{name}")
async def admin_remove_override(
    name: str,
    _: Member = Depends(current_admin),
) -> dict[str, Any]:
    """Remove the override (revert to base). To remove a base site, use
    ``POST /api/admin/sites`` with ``operation='delete'`` instead."""
    if not settings.enable_site_overrides:
        raise HTTPException(404, "site overrides disabled")
    removed = overrides_mod.remove_override(name)
    if not removed:
        raise HTTPException(404, "no override found")
    reload_catalog()
    return {"ok": True, "name": name}


@app.post("/api/admin/sites/reload")
async def admin_reload_catalog(_: Member = Depends(current_admin)) -> dict[str, Any]:
    """Force a re-merge of base + overrides. Useful if the bundled
    sites.json was hot-swapped on disk without a deploy."""
    if not settings.enable_site_overrides:
        raise HTTPException(404, "site overrides disabled")
    catalog = reload_catalog()
    return {"ok": True, "sites": len(catalog.sites)}


# ---------------------------------------------------------------------------
# Phase 5d: admin-tunable runtime knobs (graph fanout, extractor floor).
# ---------------------------------------------------------------------------


if TYPE_CHECKING:

    class AdminSettingsPatch(BaseModel):
        """Body for ``PATCH /api/admin/settings``.

        Each field is ``int | None``. ``None`` deletes the override for
        that key (reverting to the env-var default); a missing field is
        a no-op so the UI can submit only the changed knobs. ``reset``
        carries ``[key, ...]`` so callers can disambiguate "don't touch"
        from "reset to default" on PATCH.

        Static stub for mypy — the runtime class is generated by
        ``create_model`` below from ``admin_settings.KNOBS`` so the
        request schema stays in lockstep with the registry without a
        parallel edit here. The dynamic build closed a regression where
        Phase 7+ knobs landed in ``KNOBS`` but the model still only
        enumerated the original Phase 5d fields, silently 422-ing every
        PATCH against a newer knob.
        """

        model_config = ConfigDict(extra="forbid")
        reset: list[str] | None = None
else:
    _admin_patch_fields: dict[str, Any] = {
        spec.key: (int | None, None) for spec in admin_settings_mod.KNOBS
    }
    _admin_patch_fields["reset"] = (list[str] | None, None)

    AdminSettingsPatch = create_model(
        "AdminSettingsPatch",
        __config__=ConfigDict(extra="forbid"),
        **_admin_patch_fields,
    )


@app.get("/api/settings")
async def public_settings(
    _: Member = Depends(current_member),
) -> dict[str, Any]:
    """Effective integer knobs visible to any signed-in member.

    The frontend uses this to surface, for example, the maximum recursion
    depth currently allowed so the depth slider can clamp itself.
    """
    return {
        "settings": admin_settings_mod.effective_map(),
        "proxy": proxy_mod.status(),
    }


@app.get("/api/admin/settings")
async def admin_get_settings(
    _: Member = Depends(current_admin),
) -> dict[str, Any]:
    """Full descriptor list for the admin settings panel."""
    return {"knobs": admin_settings_mod.describe()}


@app.patch("/api/admin/settings")
async def admin_patch_settings(
    body: AdminSettingsPatch,
    admin: Member = Depends(current_admin),
) -> dict[str, Any]:
    """Set or reset one or more admin overrides.

    Returns the post-mutation descriptor list so the UI can refresh
    without a follow-up GET.
    """
    payload = body.model_dump(exclude_none=False)
    reset_keys: set[str] = set(payload.pop("reset") or [])
    for key in reset_keys:
        if key not in admin_settings_mod.ALLOWED_KEYS:
            raise HTTPException(400, f"unknown knob: {key}")
        admin_settings_mod.delete_override(key)
    for key, value in payload.items():
        if key not in admin_settings_mod.ALLOWED_KEYS:
            continue
        if value is None:
            continue
        try:
            admin_settings_mod.set_override(key, value, actor=admin.username)
        except admin_settings_mod.UnknownKnob as e:
            raise HTTPException(400, f"unknown knob: {e}") from e
        except admin_settings_mod.KnobOutOfRange as e:
            raise HTTPException(400, str(e)) from e
    return {"knobs": admin_settings_mod.describe()}


# ---------------------------------------------------------------------------
# Phase 7d: ML reranker retrain pipeline + drift detector.
#
# Two admin-only endpoints:
#
# * ``GET  /api/admin/ml/status`` returns model age + the most recent
#   retrain status + a drift report comparing the historical training
#   set's feature distribution to the current production distribution
#   recorded by ``app.ml.RECENT_FEATURES`` (a small ring-buffer
#   maintained by the runtime scorer).
# * ``POST /api/admin/ml/retrain`` synchronously kicks off the
#   retrain pipeline (subprocesses ``scripts/train_ml.py``) and
#   returns the fresh status. We don't expose a background-job API
#   yet because the training run finishes in <2s on the synthetic set.
# ---------------------------------------------------------------------------


@app.get("/api/admin/ml/status")
async def admin_ml_status(
    _: Member = Depends(current_admin),
) -> dict[str, Any]:
    """Model age, last retrain, current drift report.

    Phase 9d also surfaces ``captcha_vendors_seen`` — a rolling
    in-process per-vendor counter populated by the CAPTCHA classifier
    (:mod:`app.captcha_signatures`). The map is empty until the
    ``enable_captcha_classifier`` admin knob is on AND a probe
    actually hits a recognised vendor signature, so an operator
    running the Phase 9d default-off path sees ``{}`` here.
    """
    from . import captcha_signatures as captcha_mod
    from . import ml as ml_mod
    from . import ml_drift as drift_mod
    from . import ml_retrain as retrain_mod

    last = retrain_mod.last_status()
    reference = ml_mod.training_feature_distribution()
    current = ml_mod.recent_feature_distribution()
    threshold = float(_admin_int("ml_drift_ks_threshold_pct") or 10) / 100.0
    report = drift_mod.compute_drift(reference, current, threshold=threshold)
    return {
        "model_age_days": retrain_mod.model_age_days(),
        "last_retrain": last.to_dict() if last else None,
        "drift": report.to_dict(),
        "captcha_vendors_seen": captcha_mod.vendor_counts(),
    }


@app.post("/api/admin/ml/retrain")
async def admin_ml_retrain(
    admin: Member = Depends(current_admin),
) -> dict[str, Any]:
    """Run the retrain pipeline synchronously and return the new status."""
    from . import ml_retrain as retrain_mod

    status = retrain_mod.run_retrain(triggered_by=admin.username)
    return {"status": status.to_dict()}


# ---------------------------------------------------------------------------
# Phase 7b: WHOIS / HIBP-style enrichment for ``identity_type=email``.
#
# Default-off, opt-in via ``enable_email_enrichment``. The handler is
# intentionally a single small wrapper around ``app.enrichment``: it
# (a) gates on the admin knob, (b) validates the email shape using the
# existing identity normaliser, (c) enforces a per-member sliding-
# window rate limit, and (d) forwards the request to the parallel
# WHOIS + HIBP fetcher. Failures degrade silently — the SPA can render
# whichever half came back successfully.
# ---------------------------------------------------------------------------


class EmailEnrichmentRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


def _admin_int(key: str) -> int:
    """Wrap ``effective_int`` with a graceful fallback for test
    contexts where the admin DB hasn't been bootstrapped yet."""
    try:
        return admin_settings_mod.effective_int(key)
    except Exception:
        spec = admin_settings_mod.KNOBS_BY_KEY.get(key)
        if spec is None:
            return 0
        return int(getattr(settings, spec.settings_attr))


@app.post("/api/enrich/email")
async def enrich_email_endpoint(
    body: EmailEnrichmentRequest,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """Run WHOIS + HIBP lookups for ``body.email``.

    Response shape (always 200, the ``disabled`` field tells the SPA
    when to hide the panel):

    .. code-block:: json

        {
            "disabled": false,
            "rate_limited": false,
            "result": {
                "email": "alice@example.com",
                "domain": "example.com",
                "whois": { ... } | null,
                "breaches": [ ... ] | null,
                "breaches_disabled": false
            }
        }

    A ``429`` is returned when the per-member rate budget is empty so
    the SPA can show a "try again in a minute" toast.
    """
    from . import enrichment as enrichment_mod

    if not _admin_int("enable_email_enrichment"):
        return {"disabled": True, "rate_limited": False, "result": None}

    raw_email = body.email.strip()
    if not is_valid_identifier(raw_email, "email"):
        raise HTTPException(400, "invalid email")
    email = normalise_identifier(raw_email, "email")

    rate_limit = _admin_int("enrichment_rate_per_min") or settings.enrichment_rate_per_min
    if not enrichment_mod.GLOBAL_LIMITER.allow(member.username, limit=rate_limit):
        raise HTTPException(429, "enrichment rate limit exceeded; retry in <60s")

    timeout = settings.enrichment_timeout
    async with httpx.AsyncClient() as client:
        result = await enrichment_mod.enrich_email(
            client,
            email,
            whois_url_template=settings.enrichment_whois_url_template,
            hibp_url_template=settings.enrichment_hibp_url_template,
            hibp_api_key=settings.enrichment_hibp_api_key,
            timeout=timeout,
        )
    return {
        "disabled": False,
        "rate_limited": False,
        "result": result.to_dict(),
    }


# ---------------------------------------------------------------------------
# Phase 7c: reverse-image lookup hook.
#
# A pure URL-builder endpoint — the SPA POSTs the avatar URL it
# already extracted, the backend returns ``{provider: lookup_url}``
# pairs, and the SPA renders click-to-open chips. The reason this
# isn't done entirely in the browser is operator policy: the admin
# knob ``enable_reverse_image`` should fully suppress the chip row
# (some operators don't want their team deep-linking into upstream
# trackers), and admin-supplied template overrides should take
# effect without the SPA having to refetch ``/api/settings``.
# ---------------------------------------------------------------------------


class ReverseImageRequest(BaseModel):
    image_url: str = Field(min_length=4, max_length=2048)


@app.post("/api/reverse-image")
async def reverse_image_endpoint(
    body: ReverseImageRequest,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """Return ``{disabled, providers}`` for an avatar URL.

    ``providers`` is a list of ``{slug, label, url}`` dicts the SPA
    renders as a chip row under the result card. Empty when the
    knob is off, the URL is unsafe, or every template was disabled.
    """

    from .reverseimage import PROVIDERS, build_lookup_urls, is_lookupable_url

    if not _admin_int("enable_reverse_image"):
        return {"disabled": True, "providers": []}

    image_url = body.image_url.strip()
    if not is_lookupable_url(image_url):
        raise HTTPException(400, "image_url must be http(s)://")

    templates = {
        "yandex": settings.reverse_image_yandex_template,
        "google": settings.reverse_image_google_template,
        "tineye": settings.reverse_image_tineye_template,
    }
    urls = build_lookup_urls(image_url, templates=templates)
    providers = [
        {"slug": p.slug, "label": p.label, "url": urls[p.slug]} for p in PROVIDERS if p.slug in urls
    ]
    return {"disabled": False, "providers": providers}


# ---------------------------------------------------------------------------
# Phase 4: per-member scan history.
# ---------------------------------------------------------------------------


@app.get("/api/history")
async def history_list(
    username: str | None = None,
    limit: int = 50,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """List the caller's recent scans, optionally filtered by username."""
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if username is not None:
        username = username.strip()
        if username and not is_valid_username(username):
            raise HTTPException(400, "invalid username filter")
    limit = max(1, min(limit, 200))
    scans = history_mod.list_scans(member=member.username, username=username, limit=limit)
    return {"scans": [s.to_dict() for s in scans]}


@app.get("/api/history/{scan_id}")
async def history_get(
    scan_id: int,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    out = history_mod.get_scan(scan_id, member.username)
    if out is None:
        raise HTTPException(404, "scan not found")
    summary, results = out
    return {"scan": summary.to_dict(), "results": results}


@app.get("/api/history/{scan_id}/export.{fmt}")
async def history_export(
    scan_id: int,
    fmt: str,
    member: Member = Depends(current_member),
) -> Response:
    """Phase 5b: download a single scan as csv / json / markdown / html.

    Auth and ownership checks mirror ``GET /api/history/<id>``; the
    payload is rendered server-side and shipped with a
    ``Content-Disposition: attachment`` so the browser writes it
    straight to disk.
    """
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    fmt_lower = fmt.lower()
    if fmt_lower not in exports_mod.ALLOWED_FORMATS:
        raise HTTPException(
            400,
            f"unsupported export format: {fmt!r} (allowed: {sorted(exports_mod.ALLOWED_FORMATS)})",
        )
    out = history_mod.get_scan(scan_id, member.username)
    if out is None:
        raise HTTPException(404, "scan not found")
    summary, results = out
    body, media_type, filename = exports_mod.render(
        cast(exports_mod.ExportFormat, fmt_lower),
        summary,
        results,
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "private, no-store",
        },
    )


@app.get("/api/history/{scan_id}/initial_pdf")
async def history_initial_pdf(
    scan_id: str,
    member: Member = Depends(current_member),
) -> Response:
    """Phase 10a: download the preliminary PDF emitted on the initial
    CLAIMED hit.

    ``scan_id`` here is the opaque token sent in the
    ``initial_pdf_ready`` SSE frame, *not* the integer
    ``history.scans.id``. Tokens are unguessable and bound to the
    member that owned the originating scan via the sidecar metadata
    written next to the PDF — a different member who somehow
    obtained the token still gets a 404.

    Returns ``404`` until the latch fires, then ``200`` with a
    ``Content-Disposition: attachment`` filename matching the regular
    history export pattern (``reckora-{token}-{username}-initial.pdf``).
    """
    meta = initial_hit_mod.read_initial_pdf_meta(scan_id)
    if meta is None:
        raise HTTPException(404, "preliminary PDF not available")
    if str(meta.get("member", "")).casefold() != member.username.casefold():
        raise HTTPException(404, "preliminary PDF not available")
    body = initial_hit_mod.read_initial_pdf(scan_id)
    if body is None:
        raise HTTPException(404, "preliminary PDF not available")
    username = str(meta.get("username") or "scan")
    safe_username = "".join(c for c in username if c.isalnum() or c in {"-", "_", "."})
    filename = f"reckora-{scan_id}-{safe_username or 'scan'}-initial.pdf"
    media_type = "application/pdf" if body.startswith(b"%PDF-") else "text/html; charset=utf-8"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "private, no-store",
        },
    )


@app.delete("/api/history/{scan_id}")
async def history_delete(
    scan_id: int,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if not history_mod.delete_scan(scan_id, member.username):
        raise HTTPException(404, "scan not found")
    # Phase 8d: cascade-revoke any outstanding public share links so
    # ``GET /share/{token}`` returns 404 immediately and a stale CDN
    # entry naturally expires within the cache window.
    from . import share_links as share_links_mod

    share_links_mod.revoke_all_for_scan(scan_id=scan_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Phase 8d: public share links + edge-cacheable read endpoint.
# ---------------------------------------------------------------------------


class ShareLinkMintRequest(BaseModel):
    ttl_sec: int | None = Field(default=None, ge=0, le=31_536_000)


def _share_links_enabled() -> None:
    if admin_settings_mod.effective_int("enable_share_links") != 1:
        raise HTTPException(503, "share links disabled")


@app.post("/api/history/{scan_id}/share", status_code=201)
async def share_link_create(
    scan_id: int,
    body: ShareLinkMintRequest,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """Mint a public share link for one of the caller's scans.

    Owner is verified by attempting to read the scan with the
    member's username; a 404 here doubles as a privacy guard for
    other members' scan ids.
    """
    _share_links_enabled()
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if history_mod.get_scan(scan_id, member.username) is None:
        raise HTTPException(404, "scan not found")

    from . import share_links as share_links_mod

    default_ttl = admin_settings_mod.effective_int("share_link_default_ttl_sec")
    requested_ttl = body.ttl_sec if body.ttl_sec is not None else default_ttl
    if default_ttl > 0:
        # Admin-set ceiling wins over the owner's request.
        ttl_sec = min(requested_ttl, default_ttl) if requested_ttl > 0 else default_ttl
    else:
        ttl_sec = max(0, requested_ttl)

    cap = admin_settings_mod.effective_int("share_link_max_per_scan")
    link = share_links_mod.mint(
        scan_id=scan_id,
        member=member.username,
        ttl_sec=ttl_sec,
        max_per_scan=cap,
    )
    if link is None:
        raise HTTPException(
            409,
            f"share link limit reached for this scan (max {cap}); "
            "revoke an existing one to free a slot",
        )
    return {"share": link.to_dict()}


@app.get("/api/history/{scan_id}/shares")
async def share_link_list(
    scan_id: int,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """List active share links for a scan owned by the caller."""
    _share_links_enabled()
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if history_mod.get_scan(scan_id, member.username) is None:
        raise HTTPException(404, "scan not found")
    from . import share_links as share_links_mod

    rows = share_links_mod.list_for_scan(scan_id=scan_id, member=member.username)
    return {"shares": [r.to_dict() for r in rows]}


@app.delete("/api/history/{scan_id}/share/{token}")
async def share_link_revoke(
    scan_id: int,
    token: str,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    """Revoke a single share link. ``scan_id`` is in the path purely
    for client-side ergonomics — the database revoke uses
    ``token + member`` so cross-scan tokens with the same prefix are
    unaffected."""
    _share_links_enabled()
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    from . import share_links as share_links_mod

    if not share_links_mod.revoke(token=token, member=member.username):
        raise HTTPException(404, "share link not found")
    return {"ok": True, "scan_id": scan_id}


@app.get("/share/{token}")
async def share_link_public(
    request: Request,
    token: str,
) -> Response:
    """Anonymous CDN-friendly read endpoint for shared scans.

    No auth, no cookies, no member identity in the response. The
    payload is the same shape as ``GET /api/history/{scan_id}`` but
    sealed at mint time — re-running the parent scan does NOT
    invalidate the link unless the owner explicitly revokes and
    re-mints.

    Cache headers:

    * ``Cache-Control: public, s-maxage=<knob>,
      stale-while-revalidate=<knob/2>`` — CDN edges keep the
      response for ``share_link_cache_max_age_sec`` and revalidate in
      the background up to half that long after expiry.
    * ``ETag: "<sha256(token|finished_at)[:16]>"`` — when the client
      sends ``If-None-Match`` we return 304.
    * ``Vary: accept-encoding`` so the same edge entry serves both
      ``gzip`` and ``br`` clients without colliding.
    """
    if admin_settings_mod.effective_int("enable_share_links") != 1:
        return ORJSONResponse(
            status_code=503,
            content={"detail": "share links disabled"},
        )
    from . import share_links as share_links_mod

    # Best-effort GC on the read path so an expired token request
    # also drops the row. The query is indexed and bounded by
    # ``share_link_max_per_scan x scan_count`` so it's cheap.
    share_links_mod.purge_expired()
    link = share_links_mod.lookup(token)
    if link is None:
        return ORJSONResponse(
            status_code=404,
            content={"detail": "share link not found or expired"},
        )
    if not settings.enable_scan_history:
        return ORJSONResponse(
            status_code=404,
            content={"detail": "scan history disabled"},
        )
    out = history_mod.get_scan(link.scan_id, link.member)
    if out is None:
        return ORJSONResponse(
            status_code=404,
            content={"detail": "underlying scan no longer available"},
        )
    summary, results = out
    etag = share_links_mod.etag_for(link, summary.finished_at)
    if request.headers.get("if-none-match") == etag:
        max_age = admin_settings_mod.effective_int("share_link_cache_max_age_sec")
        return Response(
            status_code=304,
            headers={
                "etag": etag,
                "cache-control": (
                    f"public, s-maxage={max_age}, stale-while-revalidate={max_age // 2}"
                ),
                "vary": "accept-encoding",
            },
        )
    max_age = admin_settings_mod.effective_int("share_link_cache_max_age_sec")
    payload: dict[str, Any] = {
        "share": {
            "token": link.token,
            "created_at": link.created_at,
            "expires_at": link.expires_at,
        },
        "scan": summary.to_dict(),
        "results": results,
    }
    return ORJSONResponse(
        content=payload,
        headers={
            "etag": etag,
            "cache-control": (f"public, s-maxage={max_age}, stale-while-revalidate={max_age // 2}"),
            "vary": "accept-encoding",
            # Guidance for browsers (NOT the CDN): treat this as a
            # short-lived asset; reload-from-server is fine.
            "x-content-type-options": "nosniff",
        },
    )


@app.get("/api/history/diff/{from_id}/{to_id}")
async def history_diff(
    from_id: int,
    to_id: int,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if from_id == to_id:
        raise HTTPException(400, "diff requires two distinct scan ids")
    diff = history_mod.diff_scans(member=member.username, from_id=from_id, to_id=to_id)
    if diff is None:
        raise HTTPException(404, "one or both scans not found")
    return diff


_maybe_static_dir = settings.static_dir
if _maybe_static_dir and _maybe_static_dir.exists():
    # Re-bind to a non-Optional name so the closure below type-checks
    # without per-call asserts.
    _static_dir: Path = _maybe_static_dir
    _index_html = _static_dir / "index.html"

    app.mount(
        "/assets",
        StaticFiles(directory=str(_static_dir / "assets")),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(path: str = "") -> FileResponse:
        if path.startswith("api"):
            raise HTTPException(404)
        candidate = _static_dir / path if path else _index_html
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_index_html)
else:

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": "reckora",
            "version": __version__,
            "docs": "/docs",
            "endpoints": ["/api/health", "/api/sites", "/api/search"],
        }
