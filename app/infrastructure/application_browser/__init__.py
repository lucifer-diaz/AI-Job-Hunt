"""The application browser: the only way application work reaches a web page (Phase 5, AGENTS.md "Application
execution and pre-submit claims"). Separate from the anonymous JD-recovery browser by design."""
from app.infrastructure.application_browser.service import (
    ApplicationPage,
    ApplicationSessionManager,
    AuthenticatedApplicationBrowser,
    get_application_sessions,
    set_application_sessions,
)

__all__ = ["ApplicationPage", "ApplicationSessionManager", "AuthenticatedApplicationBrowser",
           "get_application_sessions", "set_application_sessions"]
