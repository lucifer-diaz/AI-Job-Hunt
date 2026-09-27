"""Shared headless browser (see service.py)."""
from app.infrastructure.browser.service import (
    BrowserService,
    BrowserUnavailable,
    NavigationFailed,
    RenderedPage,
    get_browser_service,
    set_browser_service,
    shutdown_browser_service,
)

__all__ = ["BrowserService", "BrowserUnavailable", "NavigationFailed", "RenderedPage", "get_browser_service",
           "set_browser_service", "shutdown_browser_service"]
