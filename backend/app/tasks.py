"""Phase 8a: Celery task surface for the distributed worker fleet.

Default deployment (HF Space single container, ``RECKORA_USE_CELERY=0``)
never imports this module's runtime path — the in-process asyncio
fan-out in ``stream_checks`` is used instead. Importing this file is
cheap (no broker connection until ``celery_app.send_task`` or
``check_site_task.delay`` is called), so the FastAPI app can import it
unconditionally for the cases where ``settings.use_celery`` is flipped
on at runtime.

Workers run with::

    cd backend && uv run --extra scale celery -A app.tasks worker --loglevel=info

The optional ``[scale]`` extra pulls in ``celery`` + ``redis``; without
it the import below raises ``ImportError`` only when a caller actually
touches a task, so the dev install path stays slim.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import httpx

from .config import settings

T = TypeVar("T")


def _run_async_in_fresh_thread(coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a fresh async coroutine on its own event loop in a worker thread.

    Equivalent to ``asyncio.run(coro_factory())`` when called from a
    plain synchronous context (Celery worker process), but **also**
    safe when called from inside an existing event loop (e.g. pytest's
    ``task_always_eager`` mode where the task body executes inside the
    test's running asyncio loop). Without the thread hop ``asyncio.run``
    would raise ``RuntimeError("asyncio.run() cannot be called from a
    running event loop")`` and tests of the Celery path could not even
    boot.
    """

    result_box: list[T] = []
    error_box: list[BaseException] = []

    def _target() -> None:
        try:
            result_box.append(asyncio.run(coro_factory()))
        except BaseException as e:
            error_box.append(e)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if error_box:
        raise error_box[0]
    return result_box[0]


def _make_celery_app() -> Any:
    """Construct the Celery app lazily so importing this module does not
    require Celery to be installed when the in-process path is used."""

    from celery import Celery  # local import — only needed when use_celery=1

    app = Celery(
        "reckora",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )
    # Tasks must be JSON-serializable so the API process can read them
    # from the result backend without unpickling worker-side objects.
    # ``CheckResult.to_dict()`` already returns a dict of primitives,
    # which round-trips cleanly through redis.
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Acks-late + reject-on-worker-lost together guarantee that a
        # task killed mid-flight is retried exactly once on another
        # worker. Without this a SIGKILL'd worker silently loses the
        # task and the API-side collector hangs on AsyncResult.get.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Soft-/hard-timeout per task. The hard limit must be > the soft
        # limit so a task that hits the soft limit gets a chance to
        # raise and clean up before being SIGKILL'd.
        task_soft_time_limit=settings.celery_task_timeout,
        task_time_limit=settings.celery_task_timeout + 5.0,
        # ``broker_connection_retry_on_startup=True`` is the celery 5.4
        # replacement for the deprecated ``broker_connection_retry``;
        # without it the API process hard-fails on boot if Redis is
        # not yet available.
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app: Any = _make_celery_app()


def _run_check_site(
    site_name: str,
    username: str,
    identity_type: str,
    *,
    verify: bool,
    extract: bool,
) -> dict[str, Any]:
    """Synchronous wrapper around ``check_site``. Looks the site up
    against the worker's lazily-loaded catalog so callers only have to
    ship a JSON-friendly site name across the broker.
    """

    # Imports are local so this module stays importable even when the
    # catalog file is missing (e.g. CI bootstrap before ``sites.json``
    # is materialised). Each worker pays the catalog-load cost once.
    from .checker import CheckResult, Status, check_site
    from .sites import load_catalog

    catalog = load_catalog()
    site = catalog.by_name(site_name)
    if site is None:
        return CheckResult(
            site=site_name,
            status=Status.ERROR,
            url="",
            url_main="",
            tags=[],
            error="unknown-site",
        ).to_dict()

    async def _run() -> CheckResult:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            http2=True,
            limits=httpx.Limits(
                max_connections=settings.per_host_concurrency,
                max_keepalive_connections=settings.per_host_concurrency,
            ),
            follow_redirects=True,
        ) as client:
            try:
                return await check_site(
                    client,
                    site,
                    username,
                    identity_type=identity_type,  # type: ignore[arg-type]
                    verify=verify,
                    extract=extract,
                )
            except Exception as e:  # pragma: no cover - safety net
                return CheckResult(
                    site=site.name,
                    status=Status.ERROR,
                    url=site.url,
                    url_main=site.url_main,
                    tags=list(site.tags),
                    error=type(e).__name__,
                )

    result = _run_async_in_fresh_thread(_run)
    return result.to_dict()


@celery_app.task(name="reckora.check_site", bind=False)
def check_site_task(
    site_name: str,
    username: str,
    identity_type: str = "username",
    *,
    verify: bool = True,
    extract: bool = True,
) -> dict[str, Any]:
    """Celery task: probe one site for one identifier.

    Returns the JSON form of ``CheckResult`` (via ``to_dict``). The
    API-side collector reassembles it into a ``CheckResult`` before
    handing it back to the streaming SSE pipeline so existing
    downstream code (verify, extract, phash, ml_confidence) does not
    care which path the result took.

    The task does *not* share state with the API process — each worker
    builds its own httpx client, cookie prewarm cache, and curl-cffi
    rescue session as needed. This means per-host cookie prewarm hits
    re-pay the warm-up cost on every task instead of being amortised
    across a scan, but per-task cost dominates so the difference is
    bounded by ~50-200 ms on the small subset of sites that opt into
    prewarm. Trade-off documented in ROADMAP Phase 8a.
    """

    return _run_check_site(
        site_name,
        username,
        identity_type,
        verify=verify,
        extract=extract,
    )


__all__ = ["celery_app", "check_site_task"]
