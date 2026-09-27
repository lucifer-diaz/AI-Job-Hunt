"""BrowserService backed by Playwright (async API) and one shared Chromium process.

Lifecycle:
* Nothing starts at import or at app startup. The first page load starts Playwright and Chromium, under a lock so
  concurrent callers share one launch. The app keeps running if that fails; the failure is reported, not raised at
  startup.
* Each task gets its own context (no cookies, storage, service workers or downloads) and page, closed afterwards.
* A crashed or disconnected Chromium is noticed (the `disconnected` event, `is_connected()`) and started again on the
  next use. A page crash fails only that page.
* `stop()` closes the browser and Playwright; the app calls it at shutdown so no Chromium process is left behind.
  With PLAYWRIGHT_KEEP_BROWSER_RUNNING=false the browser also closes after each batch (`release_idle`).

The service belongs to the event loop it started in. A new loop (a `python -m app.cron` run) gets a fresh browser.

Security: pages never receive application data — no extra headers, no cookies, no init scripts. Navigation requests
are checked against app.services.url_safety, so a redirect cannot reach a private address, and images, media and
fonts are not downloaded at all.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.infrastructure.browser.service import (
    BLOCKED_URL,
    BROWSER_CRASHED,
    BROWSER_START_FAILED,
    CHROMIUM_MISSING,
    CONNECTION,
    DNS,
    OTHER,
    PAGE_CRASHED,
    PLAYWRIGHT_DISABLED,
    PLAYWRIGHT_NOT_INSTALLED,
    REDIRECT_LOOP,
    SSL,
    TIMEOUT,
    UNSUPPORTED_BROWSER,
    BrowserService,
    BrowserUnavailable,
    NavigationFailed,
    RenderedPage,
)

log = logging.getLogger("jobhunt.browser")

SKIPPED_RESOURCES = frozenset({"image", "media", "font"})
SETTLE_POLL_SECONDS = 0.5
SETTLE_MAX_SECONDS = 6.0
SELECTOR_WAIT_SECONDS = 8.0
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 JobHuntOS/0.2")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _default_browsers_dir() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"


def chromium_installed() -> bool | None:
    """Whether a Playwright Chromium build is on disk: True, False, or None when it cannot be told."""
    try:
        base = _default_browsers_dir()
        if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0":
            import playwright

            base = Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
        if not base.exists():
            return False
        return any(child.name.startswith(("chromium-", "chromium_headless_shell-")) for child in base.iterdir())
    except Exception:  # noqa: BLE001  a diagnostic, never an error
        return None


def playwright_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def classify_navigation_error(exc: BaseException) -> str:
    """Map a Playwright error to a NavigationFailed category (from its type and Chromium's net:: error code)."""
    text = str(exc)
    name = type(exc).__name__
    if "ERR_NAME_NOT_RESOLVED" in text or "ERR_NAME_RESOLUTION" in text:
        return DNS
    if "ERR_TOO_MANY_REDIRECTS" in text:
        return REDIRECT_LOOP
    if "ERR_CERT" in text or "ERR_SSL" in text or "SSL_" in text:
        return SSL
    if "ERR_BLOCKED_BY_CLIENT" in text:
        return BLOCKED_URL
    if name == "TimeoutError" or "Timeout" in text and "exceeded" in text:
        return TIMEOUT
    if any(code in text for code in ("ERR_CONNECTION", "ERR_INTERNET_DISCONNECTED", "ERR_ADDRESS_UNREACHABLE",
                                     "ERR_NETWORK_CHANGED", "ERR_EMPTY_RESPONSE", "ERR_TIMED_OUT")):
        return CONNECTION
    if "crash" in text.lower():
        return PAGE_CRASHED
    if "has been closed" in text or "Browser closed" in text or "Target closed" in text or "disconnected" in text:
        return BROWSER_CRASHED
    return OTHER


class PlaywrightBrowserService(BrowserService):
    def __init__(self, *, enabled: bool = True, browser: str = "chromium", headless: bool = True,
                 navigation_timeout_seconds: float = 45, page_timeout_seconds: float = 60, max_concurrency: int = 2,
                 keep_running: bool = True, allow_private_hosts: bool = False, record_telemetry: bool = True):
        self.enabled = enabled
        self.browser_name = (browser or "chromium").lower()
        self.headless = headless
        self.navigation_timeout = max(1.0, float(navigation_timeout_seconds))
        self.page_timeout = max(self.navigation_timeout, float(page_timeout_seconds))
        self.max_concurrency = max(1, int(max_concurrency))
        self.keep_running = keep_running
        self.allow_private_hosts = allow_private_hosts
        self.record_telemetry = record_telemetry
        self._playwright: Any = None
        self._browser: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._contexts: set[Any] = set()
        self.launches = 0
        self.crashes = 0
        self.last_error: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls) -> PlaywrightBrowserService:
        from app.settings import get_settings

        s = get_settings()
        return cls(enabled=s.playwright_enabled, browser=s.playwright_browser, headless=s.playwright_headless,
                   navigation_timeout_seconds=s.playwright_navigation_timeout_seconds,
                   page_timeout_seconds=s.playwright_page_timeout_seconds,
                   max_concurrency=s.playwright_max_concurrency, keep_running=s.playwright_keep_browser_running,
                   allow_private_hosts=s.jd_fetch_allow_private_hosts)

    # ───────────── availability ─────────────

    def _unavailable_reason(self) -> tuple[str, str] | None:
        if not self.enabled:
            return PLAYWRIGHT_DISABLED, "Browser rendering is turned off (PLAYWRIGHT_ENABLED=false)"
        if self.browser_name != "chromium":
            return UNSUPPORTED_BROWSER, f"Only chromium is supported (PLAYWRIGHT_BROWSER={self.browser_name})"
        if not playwright_installed():
            return PLAYWRIGHT_NOT_INSTALLED, "The Python playwright package is not installed"
        if self.last_error and self.last_error.get("code") == CHROMIUM_MISSING and chromium_installed() is not True:
            return CHROMIUM_MISSING, "Chromium executable missing: run `python -m playwright install chromium`"
        return None

    def is_available(self) -> bool:
        return self._unavailable_reason() is None

    def diagnostics(self) -> dict[str, Any]:
        reason = self._unavailable_reason()
        installed = chromium_installed()
        if reason is None and installed is False:
            reason = (CHROMIUM_MISSING, "Chromium executable missing: run `python -m playwright install chromium`")
        return {"status": "UNAVAILABLE" if reason else "AVAILABLE", "reason_code": reason[0] if reason else None,
                "reason": reason[1] if reason else None, "browser": self.browser_name, "headless": self.headless,
                "package_installed": playwright_installed(), "chromium_installed": installed,
                "running": self._running(), "max_concurrency": self.max_concurrency,
                "keep_browser_running": self.keep_running, "launches": self.launches, "crashes": self.crashes,
                "last_error": {k: v for k, v in (self.last_error or {}).items() if k in ("code", "message", "at")}
                or None}

    def _running(self) -> bool:
        try:
            return bool(self._browser is not None and self._browser.is_connected())
        except Exception:  # noqa: BLE001
            return False

    # ───────────── lifecycle ─────────────

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A browser started in another (probably finished) loop cannot be driven from this one.
            self._playwright = self._browser = None
            self._contexts = set()
            self._loop = loop
            self._lock = asyncio.Lock()
            self._semaphore = asyncio.Semaphore(self.max_concurrency)

    def _fail(self, code: str, message: str) -> BrowserUnavailable:
        self.last_error = {"code": code, "message": message, "at": _utcnow().isoformat()}
        return BrowserUnavailable(code, message)

    async def start(self) -> None:
        self._bind_loop()
        reason = self._unavailable_reason()
        if reason:
            raise BrowserUnavailable(*reason)
        assert self._lock is not None
        async with self._lock:
            if self._running():
                return
            await self._discard()
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, args=["--disable-dev-shm-usage", "--no-first-run",
                                                  "--disable-extensions", "--mute-audio"])
            except Exception as exc:
                await self._discard()
                text = str(exc)
                if "Executable doesn't exist" in text or "playwright install" in text:
                    raise self._fail(CHROMIUM_MISSING, "Chromium executable missing: run "
                                                       "`python -m playwright install chromium`") from exc
                raise self._fail(BROWSER_START_FAILED,
                                 f"Chromium could not start ({type(exc).__name__})") from exc
            self.launches += 1
            self.last_error = None
            self._browser.on("disconnected", self._on_disconnected)
            log.info("browser started (headless=%s)", self.headless)

    def _on_disconnected(self, *_: Any) -> None:
        if self._browser is not None:
            self.crashes += 1
            log.warning("browser disconnected; it will be started again on next use")
        self._browser = None
        self._contexts = set()

    async def _discard(self) -> None:
        """Close whatever is left of a previous browser. Never raises."""
        browser, runtime = self._browser, self._playwright
        self._browser = self._playwright = None
        self._contexts = set()
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.stop()

    async def stop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is not loop:
            self._browser = self._playwright = None
            return
        for context in list(self._contexts):
            await self.close_context(context)
        await self._discard()
        log.info("browser stopped")

    async def release_idle(self) -> None:
        if not self.keep_running and not self._contexts:
            await self.stop()

    async def _ensure(self) -> Any:
        self._bind_loop()
        if not self._running():
            await self.start()
        return self._browser

    # ───────────── contexts and pages ─────────────

    async def _route(self, route: Any) -> None:
        request = route.request
        try:
            if request.resource_type in SKIPPED_RESOURCES:
                await route.abort("blockedbyclient")
                return
            from app.services.url_safety import UnsafeURL, check_public_url, obviously_private

            if not self.allow_private_hosts and obviously_private(request.url):
                await route.abort("blockedbyclient")  # a page may not make the browser reach internal services
                return
            if request.is_navigation_request():
                try:
                    await asyncio.to_thread(check_public_url, request.url, allow_private=self.allow_private_hosts)
                except UnsafeURL:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()
        except Exception:  # noqa: BLE001  the page closing mid-request is normal
            log.debug("request routing ended early for %s", request.url[:120])

    async def new_context(self) -> Any:
        browser = await self._ensure()
        try:
            context = await browser.new_context(
                accept_downloads=False, service_workers="block", java_script_enabled=True, user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900}, locale="en-US", ignore_https_errors=False)
        except Exception as exc:
            # A browser that died between the check and this call: forget it so the next task starts a new one.
            self._on_disconnected()
            raise NavigationFailed(BROWSER_CRASHED, f"The browser was not usable ({type(exc).__name__})") from exc
        context.set_default_navigation_timeout(self.navigation_timeout * 1000)
        context.set_default_timeout(self.navigation_timeout * 1000)
        await context.route("**/*", self._route)
        self._contexts.add(context)
        return context

    async def new_page(self, context: Any) -> Any:
        page = await context.new_page()
        page.on("crash", lambda *_: log.warning("a browser page crashed"))
        return page

    async def close_page(self, page: Any) -> None:
        with contextlib.suppress(Exception):
            await page.close()

    async def close_context(self, context: Any) -> None:
        self._contexts.discard(context)
        with contextlib.suppress(Exception):
            await context.close()

    # ───────────── rendering a page ─────────────

    async def fetch_rendered_page(self, url: str, *, wait_for: Sequence[str] = (),
                                  metadata: dict[str, Any] | None = None) -> RenderedPage:
        from app.services.url_safety import UnsafeURL, check_public_url

        reason = self._unavailable_reason()
        if reason:
            raise BrowserUnavailable(*reason)
        try:
            await asyncio.to_thread(check_public_url, url, allow_private=self.allow_private_hosts)
        except UnsafeURL as exc:
            raise NavigationFailed(BLOCKED_URL, str(exc)) from exc
        self._bind_loop()
        assert self._semaphore is not None
        started, clock = _utcnow(), time.monotonic()
        async with self._semaphore:  # never more than PLAYWRIGHT_MAX_CONCURRENCY pages at once
            try:
                page = await asyncio.wait_for(self._render(url, wait_for), timeout=self.page_timeout)
            except BrowserUnavailable:
                raise
            except NavigationFailed as exc:
                await self._record(url, started, clock, None, exc, metadata)
                raise
            except TimeoutError as exc:
                failure = NavigationFailed(TIMEOUT, f"The page did not finish loading in {self.page_timeout:.0f}s")
                await self._record(url, started, clock, None, failure, metadata)
                raise failure from exc
            except Exception as exc:
                category = classify_navigation_error(exc)
                if category == BROWSER_CRASHED:
                    self._on_disconnected()
                failure = NavigationFailed(category, f"{category.lower()}: {type(exc).__name__}")
                await self._record(url, started, clock, None, failure, metadata)
                raise failure from exc
        page.elapsed_ms = int((time.monotonic() - clock) * 1000)
        await self._record(url, started, clock, page, None, metadata)
        return page

    async def _render(self, url: str, wait_for: Sequence[str]) -> RenderedPage:
        async with self.page_session() as page:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.navigation_timeout * 1000)
            status = response.status if response is not None else None
            redirects, request = 0, response.request if response is not None else None
            while request is not None and request.redirected_from is not None and redirects < 50:
                redirects, request = redirects + 1, request.redirected_from
            await self._settle(page, wait_for)
            return RenderedPage(url=url, final_url=page.url, status=status, html=await page.content(),
                                title=await page.title(), redirect_count=redirects)

    async def _settle(self, page: Any, wait_for: Sequence[str]) -> None:
        """Wait, within bounds, for the posting to appear: a known container, then the text to stop growing."""
        if wait_for:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(", ".join(wait_for), state="attached",
                                             timeout=min(SELECTOR_WAIT_SECONDS, self.navigation_timeout) * 1000)
        previous, waited = -1, 0.0
        while waited < SETTLE_MAX_SECONDS:
            try:
                length = await page.evaluate("() => (document.body && document.body.innerText || '').length")
            except Exception:  # noqa: BLE001  navigating again (a client-side redirect): let it land
                length = -2
            if length == previous and length > 0:
                return
            previous = length
            await asyncio.sleep(SETTLE_POLL_SECONDS)
            waited += SETTLE_POLL_SECONDS

    async def _record(self, url: str, started: datetime, clock: float, page: RenderedPage | None,
                      error: NavigationFailed | None, metadata: dict[str, Any] | None) -> None:
        """One Monitoring → External APIs row per page load. Best effort, off the event loop."""
        if not self.record_telemetry:
            return
        from app.services import external_api

        call = external_api.ExternalCall(provider="WEB", service="playwright", operation="render_page",
                                         metadata={"browser": self.browser_name, "headless": self.headless,
                                                   **(metadata or {})})
        if page is not None:
            call.note(redirect_count=page.redirect_count, final_host=_host(page.final_url))
        kwargs: dict[str, Any] = {"started_at": started, "completed_at": _utcnow(), "method": "GET", "url": url,
                                  "status_code": page.status if page is not None else None}
        if error is not None:
            kwargs.update(error=error, error_type={TIMEOUT: "TIMEOUT", DNS: "DNS", CONNECTION: "CONNECTION",
                                                   SSL: "CONNECTION"}.get(error.category, "OTHER"))
        with contextlib.suppress(Exception):
            await asyncio.to_thread(external_api.record, call, **kwargs)


def _host(url: str | None) -> str | None:
    from urllib.parse import urlparse

    return urlparse(url or "").hostname
