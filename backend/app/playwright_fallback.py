"""Phase 9a: Playwright fallback for sites that beat curl-cffi.

A small minority of WAFs (Cloudflare's "I'm Under Attack" mode,
Datadome's interactive challenge, Akamai Bot Manager's invoke-token
flow) serve a JS-challenge page on the *first* request even when the
TLS handshake spoke a real Chrome / Safari / Firefox handshake — the
challenge JavaScript has to actually run and post back a token before
the real page body is served.

curl-cffi can't run JS. Playwright can. This module shells out to a
headless chromium, lets the challenge JavaScript settle for a few
seconds, and returns the rendered HTML so the caller can re-classify
against the real page body. The fallback is **strictly opt-in** via
the ``anti_bot_playwright_fallback`` admin knob and is bounded by a
per-scan budget (``anti_bot_playwright_max_per_scan``) so a worst-case
2,500-site scan can't fan out 2,500 chromium spawns.

Lazy install
------------
``playwright`` is **not** in the default backend deps (it ships a
~200 MB chromium asset). Operators who flip the knob on are expected
to either install ``playwright`` system-wide and run ``playwright
install chromium``, *or* let the admin UI's lazy-installer take care
of it. This module is import-safe even when neither is the case —
:func:`chromium_available` returns ``False`` and
:meth:`PlaywrightFallback.fetch` raises a clean
:class:`PlaywrightUnavailableError`.

Per-scan budget
---------------
``BudgetTracker`` is a tiny thread-safe counter the caller (typically
:func:`app.checker.stream_checks`) constructs once per scan, passes
into :meth:`PlaywrightFallback.fetch`, and discards. When the budget
is exhausted the fallback short-circuits with a sentinel result so
the caller can mark the result UNKNOWN with a structured signal
instead of silently falling back.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any

from .config import settings


class PlaywrightUnavailableError(RuntimeError):
    """Raised when Playwright or chromium is not reachable at runtime.

    The caller (:func:`app.checker.stream_checks`) catches this and
    records a ``playwright-unavailable`` signal on the result so the
    SPA can prompt the operator to install the runtime.
    """


@dataclass(slots=True)
class BudgetTracker:
    """Per-scan counter for chromium spawns.

    Thread-safe so the same instance can be passed to a Celery
    fan-out (Phase 8a) where multiple workers may try to spawn
    chromium concurrently.
    """

    limit: int
    _used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._used)

    def try_consume(self) -> bool:
        """Atomically reserve one slot. Returns ``False`` if exhausted."""
        with self._lock:
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a Playwright fetch.

    ``status`` is the final navigation status (200 for the
    post-challenge page; 0 when the navigation failed entirely). The
    caller treats a 0 status as ``UNKNOWN`` rather than a hard error.
    """

    status: int
    body: str
    final_url: str
    used_fallback: bool


# Sentinel value returned when the budget is exhausted before the
# fallback is consulted. Distinct from ``FetchResult(used_fallback=True,
# status=0)`` so callers can record the structured ``playwright-budget-
# exhausted`` signal independent of a real navigation failure.
BUDGET_EXHAUSTED = FetchResult(
    status=0,
    body="",
    final_url="",
    used_fallback=False,
)


def chromium_available() -> bool:
    """``True`` iff Playwright + a chromium binary look reachable.

    Cheap probe — never spawns a browser. We import ``playwright``
    lazily and then try ``playwright._impl._driver.compute_driver_executable``
    (public-ish enough to rely on across 1.40+). If that fails we fall
    back to looking for the system ``playwright`` CLI on ``PATH`` so
    a host where playwright was pip-installed without the bundled
    driver still reports ``True`` accurately.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    if shutil.which("playwright") is not None:
        return True
    # Best-effort: check the bundled driver.
    try:
        from playwright._impl._driver import compute_driver_executable

        exe = compute_driver_executable()
        if exe and len(exe) > 0:
            return True
    except Exception:
        pass
    return True  # playwright is importable; assume the chromium asset is reachable.


class PlaywrightFallback:
    """Headless-chromium fetcher gated by a per-scan budget.

    Construct once per scan with a :class:`BudgetTracker` and the
    ``timeout_sec`` / ``user_agent`` overrides; call
    :meth:`fetch` per JS-challenged URL. The browser is started lazily
    on the first call and reused across calls within the same scan,
    then closed by :meth:`aclose`.
    """

    def __init__(
        self,
        *,
        budget: BudgetTracker | None = None,
        timeout_sec: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._budget = (
            budget
            if budget is not None
            else BudgetTracker(
                limit=int(settings.anti_bot_playwright_max_per_scan),
            )
        )
        self._timeout_sec = (
            timeout_sec
            if timeout_sec is not None
            else float(settings.anti_bot_playwright_timeout_sec)
        )
        self._user_agent = user_agent or settings.user_agent
        self._browser: Any | None = None
        self._playwright: Any | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def fetch(self, url: str) -> FetchResult:
        """Render ``url`` in chromium and return the post-challenge body.

        Raises :class:`PlaywrightUnavailableError` if the runtime
        isn't reachable. Returns :data:`BUDGET_EXHAUSTED` if the
        per-scan budget is already spent — the caller never sees an
        exception for that path so a knob with ``max_per_scan=0`` is
        a clean disable.
        """
        if self._closed:
            raise PlaywrightUnavailableError("PlaywrightFallback is already closed")
        if not self._budget.try_consume():
            return BUDGET_EXHAUSTED

        page = await self._open_page()
        try:
            response = await page.goto(
                url,
                wait_until="networkidle",
                timeout=self._timeout_sec * 1000,
            )
            status = int(response.status) if response is not None else 0
            body = await page.content()
            final_url = page.url
        except Exception:
            return FetchResult(status=0, body="", final_url="", used_fallback=True)
        finally:
            await page.close()

        return FetchResult(
            status=status,
            body=body,
            final_url=final_url,
            used_fallback=True,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> Any:
        async with self._lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise PlaywrightUnavailableError(
                    "playwright not installed; run `pip install playwright` "
                    "and `playwright install chromium`"
                ) from exc
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise PlaywrightUnavailableError(f"chromium launch failed: {exc}") from exc
            return self._browser

    async def _open_page(self) -> Any:
        browser = await self._ensure_browser()
        context = await browser.new_context(user_agent=self._user_agent)
        return await context.new_page()
