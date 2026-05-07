"""Phase 8a tests: distributed worker fleet (Celery + Redis fan-out).

Tests run with ``task_always_eager=True`` so they exercise the real
``check_site_task`` body without needing a Redis broker. ``pytest-httpx``
mocks the upstream HTTP fetches the task issues against a synthetic
site catalog.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app import tasks as tasks_mod
from app.checker import (
    CheckResult,
    Status,
    check_result_from_dict,
    stream_checks,
    stream_checks_via_celery,
)
from app.sites import Site, SiteCatalog
from tests._phase_5h import _force_admin_int


def _site(**overrides: Any) -> Site:
    base: dict[str, Any] = {
        "name": "Example",
        "url": "https://example.com/{username}",
        "url_main": "https://example.com/",
        "check_type": "status_code",
    }
    base.update(overrides)
    return Site(**base)


@pytest.fixture(autouse=True)
def _disable_phase_5h_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 5h precision-floor knobs default to 60+ on production
    settings; for the synthetic check_site_task tests below the
    floor is irrelevant — disable it so a 200 response cleanly maps
    to CLAIMED. The Phase 5h knobs themselves have their own coverage
    in ``test_checker.py`` / ``test_link_liveness.py``.
    """

    _force_admin_int(monkeypatch, {})


@pytest.fixture(autouse=True)
def _eager_celery() -> Iterator[None]:
    """Run tasks inline so we don't need a real broker.

    ``task_eager_propagates`` re-raises worker-side exceptions on the
    caller, matching the behaviour of ``AsyncResult.get(propagate=True)``
    used by the production collector.
    """

    prev_eager = tasks_mod.celery_app.conf.task_always_eager
    prev_propagate = tasks_mod.celery_app.conf.task_eager_propagates
    tasks_mod.celery_app.conf.task_always_eager = True
    tasks_mod.celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        tasks_mod.celery_app.conf.task_always_eager = prev_eager
        tasks_mod.celery_app.conf.task_eager_propagates = prev_propagate


@pytest.fixture
def _catalog_with(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a helper that swaps in a synthetic catalog visible to
    ``app.tasks._run_check_site`` (which calls ``load_catalog``)."""

    def _swap(*sites: Site) -> SiteCatalog:
        cat = SiteCatalog(sites=tuple(sites))
        monkeypatch.setattr("app.sites.load_catalog", lambda *a, **kw: cat)
        # Module-level cache in main.py also calls load_catalog; re-patching
        # the source covers every consumer that imports it lazily.
        return cat

    return _swap


def test_check_result_round_trip() -> None:
    """``to_dict`` <-> ``check_result_from_dict`` is loss-less for the
    fields the SSE pipeline depends on."""

    cr = CheckResult(
        site="ExampleSite",
        status=Status.CLAIMED,
        url="https://example.com/alice",
        url_main="https://example.com/",
        tags=["forum", "tier-a"],
        http_status=200,
        elapsed_ms=312,
        error=None,
        confidence=87,
        signals=["status:200", "presence:profile"],
        attempts=1,
        verified=True,
        extracted={"username": "alice", "avatar_url": "https://cdn.example.com/a.png"},
        ml_confidence=79,
    )
    rebuilt = check_result_from_dict(cr.to_dict())
    assert rebuilt == cr


def test_check_result_from_dict_falls_back_on_unknown_status() -> None:
    """A garbled ``status`` string maps to ERROR rather than blowing up
    the SSE pipeline. Defensive: keeps a buggy worker from killing
    the API process."""

    rebuilt = check_result_from_dict(
        {
            "site": "x",
            "status": "lol-not-a-status",
            "url": "https://x/",
            "url_main": "https://x/",
            "tags": [],
        }
    )
    assert rebuilt.status is Status.ERROR


def test_check_site_task_returns_dict_round_trippable_to_check_result(
    _catalog_with: Any,
    httpx_mock: Any,
) -> None:
    """End-to-end: ``check_site_task`` runs ``check_site`` against a
    mocked upstream and returns a dict the API process can rebuild."""

    site = _site(name="MockSite", url="https://mock/{username}", url_main="https://mock/")
    _catalog_with(site)

    httpx_mock.add_response(url="https://mock/alice", status_code=200, text="ok")

    payload = tasks_mod.check_site_task(
        "MockSite", "alice", "username", verify=False, extract=False
    )
    assert isinstance(payload, dict)
    assert payload["site"] == "MockSite"
    assert payload["status"] == Status.CLAIMED.value
    rebuilt = check_result_from_dict(payload)
    assert rebuilt.site == "MockSite"
    assert rebuilt.status is Status.CLAIMED


def test_check_site_task_unknown_site_returns_error(_catalog_with: Any) -> None:
    """A site name absent from the catalog yields a structured ERROR
    payload instead of an exception bubbling up the SSE stream."""

    _catalog_with()  # empty catalog

    payload = tasks_mod.check_site_task("DoesNotExist", "alice", "username")
    assert payload["status"] == Status.ERROR.value
    assert payload["error"] == "unknown-site"


@pytest.mark.asyncio
async def test_stream_checks_via_celery_yields_per_site_results(
    _catalog_with: Any,
    httpx_mock: Any,
) -> None:
    """``stream_checks_via_celery`` produces one CheckResult per site,
    in the same dataclass shape as the in-process path."""

    a = _site(name="AlphaSite", url="https://alpha/{username}", url_main="https://alpha/")
    b = _site(name="BetaSite", url="https://beta/{username}", url_main="https://beta/")
    _catalog_with(a, b)

    httpx_mock.add_response(url="https://alpha/alice", status_code=200, text="ok")
    httpx_mock.add_response(url="https://beta/alice", status_code=404, text="nope")

    out: list[CheckResult] = []
    async for r in stream_checks_via_celery(
        [a, b],
        "alice",
        verify=False,
        extract=False,
    ):
        out.append(r)

    assert {r.site for r in out} == {"AlphaSite", "BetaSite"}
    by_name = {r.site: r for r in out}
    assert by_name["AlphaSite"].status is Status.CLAIMED
    assert by_name["BetaSite"].status is Status.FREE


@pytest.mark.asyncio
async def test_stream_checks_dispatches_to_celery_when_use_celery_set(
    _catalog_with: Any,
    httpx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping ``settings.use_celery=1`` makes ``stream_checks`` route
    through the Celery path transparently — callers (the SSE stream
    pipeline) need no code change."""

    from app import checker as checker_mod
    from app.config import settings

    site = _site(name="GammaSite", url="https://gamma/{username}", url_main="https://gamma/")
    _catalog_with(site)
    httpx_mock.add_response(url="https://gamma/alice", status_code=200, text="ok")

    monkeypatch.setattr(settings, "use_celery", 1)

    captured: list[CheckResult] = []
    sentinel_called = {"hit": False}

    real_celery_path = checker_mod.stream_checks_via_celery

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        sentinel_called["hit"] = True
        async for r in real_celery_path(*args, **kwargs):
            yield r

    monkeypatch.setattr(checker_mod, "stream_checks_via_celery", _spy)

    async for r in stream_checks([site], "alice", verify=False, extract=False):
        captured.append(r)

    assert sentinel_called["hit"], "stream_checks should have routed via the celery path"
    assert len(captured) == 1
    assert captured[0].site == "GammaSite"
    assert captured[0].status is Status.CLAIMED


def test_celery_app_config_pinned_for_json_only_payloads() -> None:
    """JSON-only serialisation guards against ``pickle`` codepaths that
    would let a malicious worker inject arbitrary objects into the
    API process."""

    conf = tasks_mod.celery_app.conf
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
    assert "json" in conf.accept_content
    # Acks-late + reject-on-worker-lost together guarantee at-least-once
    # delivery on a SIGKILL'd worker.
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
