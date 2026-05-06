"""Phase 6b — engine-aware probe heuristics.

Engine-specific classifiers produce cleaner signals than the generic
regex/presence/absence model for sites that run on well-known CMS/forum
platforms.  Each probe function takes the HTTP response + body obtained by
the generic ``check_site`` pipeline and returns an *override* ``(status,
signals)`` tuple, or ``None`` to fall back to the default classifier.

These are called from ``checker._classify_response`` when ``site.engine``
is set and a probe is registered for that engine.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from .checker import Status
    from .sites import Site

# Public export: mapping from engine name → probe function.
# The checker imports this map at classify time.
_ProbeResult = tuple["Status", list[str]] | None
_ProbeFn = Callable[["httpx.Response", str, "Site"], _ProbeResult]
ENGINE_PROBES: dict[str, _ProbeFn] = {}


def _register(engine_name: str):
    """Decorator to register an engine probe."""

    def _decorator(fn):
        ENGINE_PROBES[engine_name] = fn
        return fn

    return _decorator


# ---------------------------------------------------------------------------
# XenForo
# ---------------------------------------------------------------------------

_XENFORO_USER_ID_RE = re.compile(r'data-user-id="(\d+)"')


@_register("XenForo")
def _xenforo_probe(resp: httpx.Response, body: str, site: Site) -> tuple[Status, list[str]] | None:
    """XenForo: look for ``data-user-id="<n>"`` in the profile HTML.

    A real profile page always contains this attribute on the ``<div
    class="memberProfilePage">`` wrapper.  A 404 or "user not found"
    page never does.
    """
    from .checker import Status

    if resp.status_code == 404:
        return Status.FREE, ["engine-xenforo:404"]
    if resp.status_code == 200 and _XENFORO_USER_ID_RE.search(body):
        return Status.CLAIMED, ["engine-xenforo:user-id-match"]
    if resp.status_code == 200:
        return Status.FREE, ["engine-xenforo:no-user-id"]
    return None


# ---------------------------------------------------------------------------
# Discourse
# ---------------------------------------------------------------------------

_DISCOURSE_JSON_USERNAME_RE = re.compile(r'"username"\s*:\s*"[^"]+"')


@_register("Discourse")
def _discourse_probe(
    resp: httpx.Response, body: str, site: Site
) -> tuple[Status, list[str]] | None:
    """Discourse: JSON profile endpoint gives a clean 200/404 signal.

    The generic classifier is fragile on Discourse HTML (layout varies
    between Discourse versions). The JSON endpoint ``/u/<u>.json`` is
    stable and returns:
    - 200 + ``{"user": {"username": ...}}`` → CLAIMED
    - 404 → FREE
    """
    from .checker import Status

    if resp.status_code == 404:
        return Status.FREE, ["engine-discourse:404"]
    if resp.status_code == 200 and _DISCOURSE_JSON_USERNAME_RE.search(body):
        return Status.CLAIMED, ["engine-discourse:json-match"]
    if resp.status_code == 200:
        # 200 but no username field — might be a redirect to the homepage.
        return Status.UNKNOWN, ["engine-discourse:200-no-username"]
    return None


# ---------------------------------------------------------------------------
# vBulletin
# ---------------------------------------------------------------------------

_VBULLETIN_USERINFO_RE = re.compile(r'id="userinfo"|class="userinfo"', re.IGNORECASE)


@_register("vBulletin")
def _vbulletin_probe(
    resp: httpx.Response, body: str, site: Site
) -> tuple[Status, list[str]] | None:
    """vBulletin: look for the ``userinfo`` block in the profile HTML.

    ``member.php?username=<u>`` returns:
    - 200 + a ``<div id="userinfo">`` block → CLAIMED
    - 200 without it (or a generic "not found" page) → FREE
    - 404 → FREE (some vBulletin installs return 404 for missing users)
    """
    from .checker import Status

    if resp.status_code == 404:
        return Status.FREE, ["engine-vbulletin:404"]
    if resp.status_code == 200 and _VBULLETIN_USERINFO_RE.search(body):
        return Status.CLAIMED, ["engine-vbulletin:userinfo-match"]
    if resp.status_code == 200:
        return Status.FREE, ["engine-vbulletin:no-userinfo"]
    return None


# ---------------------------------------------------------------------------
# uCoz
# ---------------------------------------------------------------------------


@_register("uCoz")
def _ucoz_probe(resp: httpx.Response, body: str, site: Site) -> tuple[Status, list[str]] | None:
    """uCoz: subdomain-based site — the URL is ``<site>.ucoz.com/<u>``.

    uCoz sites are notoriously noisy (many return 200 for everything).
    We tighten the classification by checking:
    - 404 → FREE (some installs do return 404)
    - 200 + body contains a "user not found" / "does not exist" marker → FREE
    - 200 + body has a real user profile indicator → CLAIMED
    """
    from .checker import Status

    if resp.status_code == 404:
        return Status.FREE, ["engine-ucoz:404"]
    if resp.status_code == 200:
        lower_body = body.lower()
        not_found_markers = [
            "пользователь не найден",  # Russian: "user not found"
            "user not found",
            "does not exist",
            "страница не найдена",  # Russian: "page not found"
        ]
        for marker in not_found_markers:
            if marker in lower_body:
                return Status.FREE, ["engine-ucoz:not-found-marker"]
        # If we got here with 200, it's likely a real profile page.
        return Status.CLAIMED, ["engine-ucoz:200-present"]
    return None
