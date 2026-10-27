"""BrowserUseWebUIAdapter: what the local browser-use Web UI is, and whether Job Hunt OS's sidecar matches it.

    probe()  → reachability of BROWSER_USE_WEBUI_URL (GET /config, through the instrumented client), its Gradio
               version and whether it asks for authentication, the browser-use version its checkout pins
               (BROWSER_USE_WEBUI_PATH/requirements.txt, read only) and what the sidecar runner reports

The Web UI's own HTTP interface is not used to run applications (ADR-031): its Gradio endpoints are positional and
auto-named (their order can change on restart), every caller shares one agent and one browser, it has no
authentication, no per-user session, and no way to veto an action before it runs. Job Hunt OS therefore runs the same
browser-use release through its own runner (app.infrastructure.application_browser.browser_use_sidecar) and only
health-checks the service, so an operator sees at a glance whether the two still match. Nothing here modifies the
Web UI's checkout or sends it anything but a GET.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from app.settings import get_settings


def _pinned(path: str) -> str | None:
    requirements = Path(path).expanduser() / "requirements.txt"
    try:
        match = re.search(r"^browser-use\s*==\s*([\w.]+)", requirements.read_text(), re.MULTILINE)
    except OSError:
        return None
    return match.group(1) if match else None


def probe_service(url: str) -> dict[str, Any]:
    """GET the Gradio /config of the running Web UI. Never raises."""
    from app.services.external_api import external_client

    base = url.rstrip("/")
    try:
        with external_client("BROWSER_USE_WEBUI", service="webui", operation="health", timeout=10) as client:
            response = client.get(f"{base}/config")
        if response.status_code != 200:
            return {"reachable": True, "status": response.status_code, "gradio": None}
        config = response.json()
    except Exception as exc:  # noqa: BLE001  unreachable is an answer, not an error
        return {"reachable": False, "error": type(exc).__name__}
    endpoints = sum(1 for d in config.get("dependencies") or [] if d.get("api_name"))
    return {"reachable": True, "status": 200, "gradio": config.get("version"),
            "auth_required": bool(config.get("auth_required")), "api_exposed": bool(config.get("show_api", True)),
            "named_endpoints": endpoints, "protocol": config.get("protocol")}


def probe() -> dict[str, Any]:
    from app.infrastructure.application_browser import browser_use_sidecar as sidecar

    settings = get_settings()
    out: dict[str, Any] = {"enabled": settings.browser_use_webui_enabled,
                           "integration": "sidecar runner (browser-use components); the Web UI is health-checked only"}
    if settings.browser_use_webui_url:
        out["service"] = probe_service(settings.browser_use_webui_url)
        if out["service"].get("reachable") and not out["service"].get("auth_required"):
            out.setdefault("warnings", []).append(
                "The Web UI has no authentication: keep it bound to 127.0.0.1 and never expose its port")
    if settings.browser_use_webui_path:
        out["webui_pins_browser_use"] = _pinned(settings.browser_use_webui_path)
    try:
        out["sidecar"] = asyncio.run(sidecar.health())
    except RuntimeError:  # called on a running loop: the caller awaits sidecar.health() itself
        out["sidecar"] = {"configured": sidecar.configured()}
    pinned, runner = out.get("webui_pins_browser_use"), (out.get("sidecar") or {}).get("browser_use")
    out["compatible"] = bool(out.get("sidecar", {}).get("compatible")) and (pinned is None or pinned == runner)
    return out
