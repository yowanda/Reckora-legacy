"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import __version__
from . import admin_settings as admin_settings_mod
from . import auth as auth_mod
from . import exports as exports_mod
from . import history as history_mod
from . import permutations as permutations_mod
from . import proxy as proxy_mod
from . import site_overrides as overrides_mod
from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    Member,
)
from .cache import TTLCache
from .checker import is_valid_username, stream_checks
from .config import settings
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


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(_: Request, exc: RateLimitExceeded) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=429,
        content={"detail": "rate limit exceeded, slow down"},
    )


def get_catalog() -> SiteCatalog:
    return load_catalog()


class SearchRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=600)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        v = v.strip()
        if not is_valid_username(v):
            raise ValueError("invalid username (allowed: letters, digits, _ . -)")
        return v


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
    return {
        "status": "ok",
        "version": __version__,
        "sites": len(get_catalog().sites),
        # Phase 5e: surface proxy state without leaking the URL itself
        # so an external uptime probe can flag a dropped Tor circuit.
        "proxy": proxy_mod.status(),
    }


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

    Centralised so the proxy gate (Phase 5e) is uniformly applied; the
    extra arg is harmless when the proxy is off (``None``).
    """
    return httpx.AsyncClient(
        timeout=settings.request_timeout,
        http2=True,
        limits=httpx.Limits(
            max_connections=settings.total_concurrency,
            max_keepalive_connections=settings.total_concurrency,
        ),
        follow_redirects=True,
        proxy=proxy_mod.proxy_url_or_none(),
    )


async def _search_stream(
    req: SearchRequest,
    catalog: SiteCatalog,
    *,
    member: Member | None = None,
) -> AsyncIterator[bytes]:
    sites = catalog.filtered(tags=req.tags, limit=req.limit)
    yield _sse_event("start", {"username": req.username, "total": len(sites)})

    if not sites:
        yield _sse_event("done", {"results": 0})
        return

    cache_key = json.dumps(
        {"u": req.username, "t": sorted(req.tags), "l": req.limit},
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
    # Phase 1: HTTP/2 + connection pool. Phase 5e: optional proxy.
    async with _new_async_client() as client:
        async for result in stream_checks(sites, req.username, client=client):
            payload = result.to_dict()
            collected.append(payload)
            yield _sse_event("result", payload)

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
    """

    depth: int = Field(default=1, ge=0, le=2)
    max_secondary: int | None = Field(default=None, ge=1, le=20)


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

    async with _new_async_client() as client:
        # ---- depth 0: primary scan ------------------------------------
        seed_hits: list[tuple[str, dict[str, Any]]] = []  # (parent, payload)
        async for result in stream_checks(sites, req.username, client=client):
            payload = result.to_dict()
            payload["parent_username"] = None
            primary_results.append(payload)
            yield _sse_event("result", payload)
            if (
                effective_depth >= 1
                and result.status.value == "claimed"
                and result.extracted
                and len(seed_hits) < effective_max_seed_hits
            ):
                seed_hits.append((req.username, payload))

        # ---- depth 1: extract + sub-scan ------------------------------
        if effective_depth >= 1 and seed_hits:
            from .extractor import find_secondary_usernames

            sub_targets: list[tuple[str, str, dict[str, Any]]] = []
            for parent_username, payload in seed_hits:
                extracted = payload.get("extracted") or {}
                yield _sse_event(
                    "extracted",
                    {
                        "parent_username": parent_username,
                        "site": payload["site"],
                        "url": payload["url"],
                        "fields": extracted,
                    },
                )
                for child_username, evidence in find_secondary_usernames(
                    extracted, parent_username
                ):
                    if child_username in discovered:
                        continue
                    if not is_valid_username(child_username):
                        continue
                    if len([t for t in sub_targets if t[0] == parent_username]) >= max_secondary:
                        break
                    discovered[child_username] = evidence
                    edge = {
                        "parent_username": parent_username,
                        "child_username": child_username,
                        "evidence": evidence,
                        "source_site": payload["site"],
                    }
                    edges.append(edge)
                    yield _sse_event("edge", edge)
                    sub_targets.append((parent_username, child_username, payload))

            for parent_username, child_username, _payload in sub_targets:
                yield _sse_event(
                    "secondary_start",
                    {
                        "parent_username": parent_username,
                        "child_username": child_username,
                        "total": len(sites),
                    },
                )
                # Sub-scans don't recurse further (depth-1 hard cap) and
                # skip extraction to keep payload size bounded.
                async for sub in stream_checks(
                    sites,
                    child_username,
                    client=client,
                    extract=False,
                ):
                    sub_payload = sub.to_dict()
                    sub_payload["parent_username"] = child_username
                    sub_payload["discovered_via"] = parent_username
                    yield _sse_event("result", sub_payload)
                    primary_results.append(sub_payload)

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
            "secondary_count": len(discovered),
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
    """

    max_permutations: int = Field(default=10, ge=0, le=40)
    strategies: list[str] | None = Field(default=None)

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
            async for result in stream_checks(sites, handle, client=client):
                payload = result.to_dict()
                payload["permutation_username"] = handle
                payload["parent_username"] = req.username
                if result.status == "claimed":
                    claimed_for_handle += 1
                collected.append(payload)
                yield _sse_event("result", payload)
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


class AdminSettingsPatch(BaseModel):
    """Body for ``PATCH /api/admin/settings``.

    Each field is ``int | None``. ``None`` deletes the override for that
    key (reverting to the env-var default); a missing field is a no-op
    so the UI can submit only the changed knobs.
    """

    model_config = {"extra": "forbid"}

    graph_max_depth: int | None = None
    graph_max_secondary: int | None = None
    graph_max_seed_hits: int | None = None
    extract_min_confidence: int | None = None
    # Phase 5e: proxy_enabled is a 0/1 toggle gating the configured
    # ``RECKORA_PROXY_URL``. The URL itself is intentionally NOT
    # exposed via this endpoint — it stays env-only.
    proxy_enabled: int | None = None
    # Per-key sentinel: when present in ``reset`` the override row is
    # deleted regardless of whether ``<key>=None`` was sent (allows
    # disambiguating "don't touch" vs "reset to default" on PATCH).
    reset: list[str] | None = None


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


@app.delete("/api/history/{scan_id}")
async def history_delete(
    scan_id: int,
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    if not settings.enable_scan_history:
        raise HTTPException(404, "scan history disabled")
    if not history_mod.delete_scan(scan_id, member.username):
        raise HTTPException(404, "scan not found")
    return {"ok": True}


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
