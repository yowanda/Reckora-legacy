"""Phase 8b: httpx transport that ties the proxy pool + rate limiter
together.

This module is the thin glue between :mod:`app.proxy_pool` and
:mod:`app.host_rate_limit` and the rest of the codebase. Every place
that today builds an ``httpx.AsyncClient`` for outbound traffic uses
:func:`build_async_client` instead, which:

1. Reads the per-host token bucket *before* dispatch, so a burst of
   2,506 site checks doesn't pound any one host faster than its
   admin-configured rate.
2. Picks a proxy from :func:`app.proxy_pool.get_pool` per request, so
   the load is spread across every egress IP an operator has
   configured.
3. Marks the proxy URL as failed on transport errors so the next
   request skips it for ``proxy_health_cooldown_sec`` seconds.

When both knobs are off (the HF Space single-container default) the
transport is a thin pass-through over a regular ``httpx.AsyncHTTPTransport``
with the Phase 5e single-proxy gate applied — behaviour is byte-for-byte
identical to the pre-Phase-8b world.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx

from . import host_rate_limit, proxy_pool
from . import proxy as proxy_mod
from .config import settings


class RotatingProxyTransport(httpx.AsyncBaseTransport):
    """Per-request proxy rotation + per-host rate limiting.

    ``handle_async_request`` is the only override; we cache one
    ``httpx.AsyncHTTPTransport`` per proxy URL (or for direct egress)
    so the cost of rotating proxies is just a dict lookup, not a fresh
    connection pool.

    Failure handling
    ----------------
    Any ``httpx.HTTPError`` (transport-level — connection refused, TLS
    handshake failure, read timeout, ...) marks the proxy URL as
    failed and re-raises. Application-level non-2xx responses are *not*
    treated as proxy failures; the host might be returning ``502`` for
    its own reasons and that doesn't tell us anything useful about the
    proxy's health.
    """

    def __init__(
        self,
        *,
        verify: bool = True,
        http2: bool = True,
        retries: int = 0,
    ) -> None:
        self._verify = verify
        self._http2 = http2
        self._retries = retries
        self._transports: dict[str | None, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        await host_rate_limit.get_limiter().acquire(host)
        proxy_url = self._select_proxy()
        transport = self._transport_for(proxy_url)
        try:
            response = await transport.handle_async_request(request)
        except httpx.HTTPError:
            proxy_pool.get_pool().mark_failed(proxy_url)
            raise
        else:
            proxy_pool.get_pool().mark_success(proxy_url)
            return response

    async def aclose(self) -> None:
        for t in self._transports.values():
            with contextlib.suppress(Exception):  # best-effort cleanup
                await t.aclose()
        self._transports.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _select_proxy(self) -> str | None:
        """Pick the URL for *this* request.

        Order of precedence:

        1. Phase 8b pool when ``enable_proxy_rotator=1`` and the pool
           has at least one URL.
        2. Phase 5e single-proxy gate
           (:func:`app.proxy.proxy_url_or_none`).
        3. ``None`` — direct egress.
        """
        if proxy_pool.is_enabled():
            url = proxy_pool.get_pool().acquire()
            if url is not None:
                return url
        return proxy_mod.proxy_url_or_none()

    def _transport_for(self, proxy_url: str | None) -> httpx.AsyncHTTPTransport:
        cached = self._transports.get(proxy_url)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {
            "verify": self._verify,
            "http2": self._http2,
            "retries": self._retries,
        }
        if proxy_url is not None:
            kwargs["proxy"] = proxy_url
        transport = httpx.AsyncHTTPTransport(**kwargs)
        self._transports[proxy_url] = transport
        return transport


def build_async_client(
    *,
    timeout: float | None = None,
    max_connections: int | None = None,
    follow_redirects: bool = True,
    http2: bool = True,
) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` pre-wired for Phase 8b.

    The single entrypoint replaces the ad-hoc
    ``httpx.AsyncClient(... proxy=proxy_mod.proxy_url_or_none(), ...)``
    pattern that lived in ``main._new_async_client`` and a couple of
    other call sites. Keeping the wiring in one place means the next
    capability (e.g. a per-host circuit breaker) can be added without
    touching every outbound call.
    """
    timeout_value = timeout if timeout is not None else settings.request_timeout
    pool_size = max_connections if max_connections is not None else settings.total_concurrency
    transport = RotatingProxyTransport(http2=http2)
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout_value,
        limits=httpx.Limits(
            max_connections=pool_size,
            max_keepalive_connections=pool_size,
        ),
        follow_redirects=follow_redirects,
    )
