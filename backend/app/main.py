"""FastAPI entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

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
from . import auth as auth_mod
from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    Member,
)
from .cache import TTLCache
from .checker import is_valid_username, stream_checks
from .config import settings
from .sites import SiteCatalog, load_catalog

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
    return {"status": "ok", "version": __version__, "sites": len(get_catalog().sites)}


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


async def _search_stream(req: SearchRequest, catalog: SiteCatalog) -> AsyncIterator[bytes]:
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

    collected: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=settings.request_timeout,
        limits=httpx.Limits(
            max_connections=settings.total_concurrency,
            max_keepalive_connections=settings.total_concurrency,
        ),
        follow_redirects=True,
    ) as client:
        async for result in stream_checks(sites, req.username, client=client):
            payload = result.to_dict()
            collected.append(payload)
            yield _sse_event("result", payload)

    cache.set(cache_key, collected)
    yield _sse_event("done", {"results": len(collected), "cached": False})


@app.post("/api/search")
@limiter.limit(settings.search_rate_limit)
async def search(
    request: Request,
    body: SearchRequest,
    catalog: SiteCatalog = Depends(get_catalog),
    _: Member = Depends(current_member),
) -> StreamingResponse:
    if not catalog.sites:
        raise HTTPException(503, "site catalog unavailable")
    return StreamingResponse(
        _search_stream(body, catalog),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


_static_dir = settings.static_dir
if _static_dir and _static_dir.exists():
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
