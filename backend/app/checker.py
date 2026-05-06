"""Per-site username checker.

Heavily inspired by maigret's check logic, but rewritten to be small,
async-first, and self-contained. We support three check types from the
upstream site database:

- ``status_code``: 2xx → claimed, 404 → free, anything else → unknown.
- ``message``: parse HTML body for ``presenseStrs`` (claimed) /
  ``absenceStrs`` (free). Either set may be present; both are honored.
- ``response_url``: if the final URL after redirects matches the templated
  URL, the username is claimed.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .config import settings
from .sites import Site


class Status(StrEnum):
    CLAIMED = "claimed"
    FREE = "free"
    UNKNOWN = "unknown"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(slots=True)
class CheckResult:
    site: str
    status: Status
    url: str
    url_main: str
    tags: list[str]
    http_status: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "status": self.status.value,
            "url": self.url,
            "url_main": self.url_main,
            "tags": self.tags,
            "http_status": self.http_status,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def is_valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


def _matches_regex(username: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    try:
        return re.match(pattern, username) is not None
    except re.error:
        return True


def _classify_message(body: str, site: Site) -> Status:
    presense_hit = any(s in body for s in site.presense_strs) if site.presense_strs else None
    absence_hit = any(s in body for s in site.absence_strs) if site.absence_strs else None

    if presense_hit and not absence_hit:
        return Status.CLAIMED
    if absence_hit and not presense_hit:
        return Status.FREE
    if site.absence_strs and not absence_hit and not site.presense_strs:
        return Status.CLAIMED
    if site.presense_strs and not presense_hit and not site.absence_strs:
        return Status.FREE
    return Status.UNKNOWN


def _classify_response(resp: httpx.Response, site: Site) -> Status:
    if site.check_type == "status_code":
        if 200 <= resp.status_code < 300:
            return Status.CLAIMED
        if resp.status_code in (404, 410):
            return Status.FREE
        return Status.UNKNOWN
    if site.check_type == "response_url":
        target = site.url.replace("{username}", "USERNAME").rstrip("/")
        final = str(resp.url).rstrip("/")
        # If we ended up somewhere different, consider it free.
        if "USERNAME" in target:
            prefix = target.split("USERNAME", 1)[0]
            if final.startswith(prefix.rstrip("/")):
                return Status.CLAIMED
            return Status.FREE
        return Status.CLAIMED if final.startswith(target) else Status.FREE
    # message
    return _classify_message(resp.text, site)


async def check_site(client: httpx.AsyncClient, site: Site, username: str) -> CheckResult:
    url = site.url.replace("{username}", username)
    if not _matches_regex(username, site.regex_check):
        return CheckResult(
            site=site.name,
            status=Status.SKIPPED,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            error="username does not match site regex",
        )

    started = time.perf_counter()
    headers = {"User-Agent": settings.user_agent}
    headers.update(dict(site.headers))
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        return CheckResult(
            site=site.name,
            status=Status.ERROR,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=type(e).__name__,
        )
    except httpx.HTTPError as e:  # pragma: no cover - generic safety net
        return CheckResult(
            site=site.name,
            status=Status.ERROR,
            url=url,
            url_main=site.url_main,
            tags=list(site.tags),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=type(e).__name__,
        )

    status = _classify_response(resp, site)
    # Treat upstream ratelimit / blocks as unknown rather than free.
    if resp.status_code in (401, 403, 429, 451) and status == Status.FREE:
        status = Status.UNKNOWN

    return CheckResult(
        site=site.name,
        status=status,
        url=url,
        url_main=site.url_main,
        tags=list(site.tags),
        http_status=resp.status_code,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def stream_checks(
    sites: list[Site],
    username: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[CheckResult]:
    """Run checks concurrently and yield each result as it arrives."""
    owned_client = client is None
    client = client or httpx.AsyncClient(
        timeout=settings.request_timeout,
        http2=False,
        limits=httpx.Limits(
            max_connections=settings.total_concurrency,
            max_keepalive_connections=settings.total_concurrency,
        ),
    )

    sem = asyncio.Semaphore(settings.total_concurrency)
    queue: asyncio.Queue[CheckResult | None] = asyncio.Queue()

    async def worker(site: Site) -> None:
        async with sem:
            try:
                result = await check_site(client, site, username)
            except Exception as e:
                result = CheckResult(
                    site=site.name,
                    status=Status.ERROR,
                    url=site.url.replace("{username}", username),
                    url_main=site.url_main,
                    tags=list(site.tags),
                    error=type(e).__name__,
                )
            await queue.put(result)

    tasks = [asyncio.create_task(worker(s)) for s in sites]

    async def waiter() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    waiter_task = asyncio.create_task(waiter())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        waiter_task.cancel()
        for t in tasks:
            t.cancel()
        if owned_client:
            await client.aclose()
