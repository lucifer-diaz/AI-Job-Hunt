"""The guarded page (Phase 6): the one place in app/ that may click a final Submit, and only with a SubmitTicket.

    GuardedSessions.open(user_id=…, job_id=…, ats=…)  → GuardedPage: a fresh anonymous context, like an ephemeral
                                                         page, that stays open for one guarded run (fill, validate,
                                                         submit, confirm) and is closed with everything in it
    page.submit(ticket)                                → re-checks the ticket against the database (the intent is
                                                         CLICKING, this run's, this domain), arms non-GET requests for
                                                         that one domain for a few seconds, clicks the adapter's own
                                                         submit control once, waits, disarms
    page.evidence()                                    → what the confirmation verifier reads: the final URL and
                                                         title, the non-GET responses after the click (method, status,
                                                         host, path), whether the form is still there

Until a ticket arms it, the page is exactly an ephemeral application page: every non-GET request is aborted at the
page and its context. A ticket is only built by app.services.submission after the pre-submit validator, the shared
claim and the CLICKING commit; `tests/test_no_automated_submit.py` allows `click` in this file only.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.infrastructure.application_browser.ephemeral import _Page
from app.infrastructure.browser.service import BrowserService

log = logging.getLogger("jobhunt.guarded_submit")
ARM_SECONDS = 30
# Each deterministic adapter's own submit control. Never a locator a page or a model supplied.
SUBMIT_SELECTORS = {"greenhouse": ("#application_form #submit_app", "#application_form button[type=submit]",
                                   "#application_form input[type=submit]", "form#application-form button[type=submit]")}


class SubmitRefused(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SubmitTicket:
    """Authority for one click. `recheck` runs on a worker thread just before it and must say yes: it re-reads the
    SubmissionIntent (CLICKING, this execution, this claim, still held) from the database."""

    intent_id: int
    domain: str
    adapter: str
    recheck: Callable[[], bool]


class GuardedPage(_Page):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._armed: tuple[str, float] | None = None
        self._responses: list[dict[str, Any]] = []
        self._clicked = False

    async def _guard(self, route: Any) -> None:
        try:
            request = route.request
            if request.method.upper() != "GET":
                host = (urlparse(request.url).hostname or "").lower()
                armed = self._armed is not None and self._armed[0] == host and time.monotonic() < self._armed[1]
                if not armed:
                    self._blocked += 1
                    await route.abort("blockedbyclient")
                    return
            await route.fallback()
        except Exception:  # noqa: BLE001  the page closing mid-request is normal
            log.debug("guarded page routing ended early")

    def _on_response(self, response: Any) -> None:
        try:
            if response.request.method.upper() != "GET" and len(self._responses) < 50:
                parsed = urlparse(response.url)
                self._responses.append({"method": response.request.method.upper(), "status": response.status,
                                        "host": (parsed.hostname or "").lower(), "path": parsed.path[:200]})
        except Exception:  # noqa: BLE001
            pass

    @property
    def host(self) -> str:
        return (urlparse(self._page.url).hostname or "").lower()

    @property
    def url(self) -> str:
        return self._page.url

    async def submit(self, ticket: SubmitTicket) -> dict[str, Any]:
        if self._clicked:
            raise SubmitRefused("ALREADY_CLICKED", "This page already clicked Submit once")
        selectors = SUBMIT_SELECTORS.get(ticket.adapter)
        if not selectors:
            raise SubmitRefused("NO_ADAPTER", "Only a deterministic adapter's own submit control can be clicked")
        if self.host != ticket.domain:
            raise SubmitRefused("WRONG_DOMAIN", "The page is not on the domain the intent names")
        control = None
        for selector in selectors:
            candidate = self._page.locator(selector)
            if await candidate.count() == 1:
                control = candidate
                break
        if control is None:
            raise SubmitRefused("NO_SUBMIT_CONTROL", "The form's submit control is not on the page (exactly once)")
        if not await asyncio.to_thread(ticket.recheck):
            raise SubmitRefused("INTENT_NOT_CLICKING", "The submission intent is no longer authorized")
        self._clicked = True
        self._page.on("response", self._on_response)
        self._armed = (ticket.domain, time.monotonic() + ARM_SECONDS)
        started = time.monotonic()
        try:
            await control.first.click(timeout=10000)
            for state in ("load", "networkidle"):
                try:
                    await self._page.wait_for_load_state(state, timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._armed = None
            try:
                self._page.remove_listener("response", self._on_response)
            except Exception:  # noqa: BLE001
                pass
        return {"clicked": True, "seconds": round(time.monotonic() - started, 2)}

    async def evidence(self) -> dict[str, Any]:
        html = ""
        try:
            html = await self._page.content()
        except Exception:  # noqa: BLE001
            pass
        title = ""
        try:
            title = await self._page.title()
        except Exception:  # noqa: BLE001
            pass
        return {"url": self._page.url, "title": title, "responses": list(self._responses), "html": html}

    async def close(self) -> None:
        raise NotImplementedError  # replaced by GuardedSessions.open


class GuardedSessions:
    """Opens guarded pages on the shared Chromium process; one per guarded run, never reused."""

    def __init__(self, browser: BrowserService, *, record_telemetry: bool = True):
        self._browser = browser
        self._record_telemetry = record_telemetry

    def is_available(self) -> bool:
        return self._browser.is_available()

    async def open(self, *, user_id: int, job_id: int | None, ats: str) -> GuardedPage:
        context = await self._browser.new_context()
        page = None
        try:
            page = await self._browser.new_page(context)
            wrapped = GuardedPage(page, allow_private=bool(getattr(self._browser, "allow_private_hosts", False)),
                                  user_id=user_id, job_id=job_id, ats=ats, record_telemetry=self._record_telemetry,
                                  context=context)
            await wrapped.install()
        except BaseException:
            if page is not None:
                await self._browser.close_page(page)
            await self._browser.close_context(context)
            raise
        browser = self._browser

        async def close() -> None:
            try:
                await browser.close_page(page)
            finally:
                await browser.close_context(context)

        wrapped.close = close  # type: ignore[method-assign]
        return wrapped


_guarded: GuardedSessions | None = None


def get_guarded_sessions() -> GuardedSessions:
    global _guarded
    if _guarded is None:
        from app.infrastructure.browser import get_browser_service

        _guarded = GuardedSessions(get_browser_service())
    return _guarded


def set_guarded_sessions(sessions: GuardedSessions | None) -> None:
    global _guarded
    _guarded = sessions
