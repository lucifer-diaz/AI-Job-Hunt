"""Application browser sessions: the boundary between application work and a web page.

    application execution worker (app.agents.application)
      → get_application_sessions()          ← ApplicationSessionManager
      → session(user_id=…, ats=…)           ← one isolated session per step, closed whatever happens
      → ApplicationPage                     ← goto, html, fill, select, check, upload, read back. Nothing that clicks.

Rules (AGENTS.md):
  * This is not the JD-recovery browser and never shares a session with it. Today's only implementation
    (`EphemeralApplicationSessions`) takes a fresh anonymous context from the shared Chromium process for each step:
    no storage state, cookies, credentials or profile, and nothing survives the step.
  * Every non-GET request a page makes is aborted, so a form can never be posted from here, by a click or a script.
  * `ApplicationPage` has no click, press, tap, submit or evaluate, and its Playwright page is private.
  * `AuthenticatedApplicationBrowser` (persistent per-account, per-ATS sessions with a human login) is the reviewed
    interface such a feature must implement; it has no implementation. Adding one needs an ADR and an AGENTS.md
    update first. Until then a login wall, an OTP or a challenge pauses the execution for a person (NEEDS_HUMAN).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PageLoad:
    url: str
    final_url: str
    status: int | None
    title: str = ""
    blocked_requests: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class ApplicationPage(ABC):
    """What application work may do to a page. Deliberately no click, press, tap, submit or script evaluation."""

    @abstractmethod
    async def goto(self, url: str) -> PageLoad:
        """Load `url` (GET). Raises the browser's BrowserUnavailable / NavigationFailed."""

    @abstractmethod
    async def html(self) -> str:
        """The page's current HTML (for the adapter to read; never sent to a model)."""

    @abstractmethod
    async def fill(self, locator: str, text: str) -> None:
        """Type `text` into a text input or textarea (replacing what was there)."""

    @abstractmethod
    async def select(self, locator: str, value: str) -> None:
        """Choose the option with this value in a select."""

    @abstractmethod
    async def check(self, locator: str) -> None:
        """Tick a checkbox or choose a radio option."""

    @abstractmethod
    async def upload(self, locator: str, path: str) -> None:
        """Attach a local file to a file input (no request is made until a form is posted, which never happens)."""

    @abstractmethod
    async def value_of(self, locator: str) -> str:
        """What a text, select or file input now holds (a file input reports the file's name)."""

    @abstractmethod
    async def is_checked(self, locator: str) -> bool:
        """Whether a checkbox or radio option is ticked."""

    @property
    @abstractmethod
    def blocked_requests(self) -> int:
        """How many non-GET requests this page tried and had aborted."""


class ApplicationSessionManager(ABC):
    """Hands out application pages. Implementations decide how a session lives; callers never see a context."""

    persistent: bool = False

    @abstractmethod
    def session(self, *, user_id: int, ats: str, job_id: int | None = None) -> AbstractAsyncContextManager[
            ApplicationPage]:
        """An isolated page for one step of one account's application on `ats`, closed with everything in it."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether a page can be opened at all (the browser is enabled and installed)."""

    def diagnostics(self) -> dict[str, Any]:
        return {"persistent": self.persistent}


class AuthenticatedApplicationBrowser(ApplicationSessionManager, ABC):
    """Reserved: persistent, per-account and per-ATS sessions a person logs in to once. Not implemented.

    An implementation must keep each profile encrypted at rest and owned by one account, lease it to one execution at
    a time, let a person take over for login and OTP, never share anything with JD recovery, and never hand cookies,
    session storage, passwords or OTPs to a model, a log or a checkpoint. It needs an ADR and an AGENTS.md update
    before it is written."""

    persistent = True

    @abstractmethod
    async def requires_login(self, *, user_id: int, ats: str) -> bool:
        """Whether this account's session for `ats` needs a person to log in again."""


_sessions: ApplicationSessionManager | None = None


def get_application_sessions() -> ApplicationSessionManager:
    """The process's application session manager: ephemeral sessions on the shared Chromium process."""
    global _sessions
    if _sessions is None:
        from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions
        from app.infrastructure.browser import get_browser_service

        _sessions = EphemeralApplicationSessions(get_browser_service())
    return _sessions


def set_application_sessions(sessions: ApplicationSessionManager | None) -> None:
    """Replace the process's session manager (tests); None goes back to the configured one."""
    global _sessions
    _sessions = sessions
