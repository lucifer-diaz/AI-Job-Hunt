"""Check real ATS application pages, read-only, and print the route map they make (Phase 6).

    python scripts/ats_live_check.py URL [URL ...]           check these application or posting URLs
    python scripts/ats_live_check.py --discover               a few live postings from public board APIs
                                                              (Greenhouse, Lever, Ashby, Workable)
    python scripts/ats_live_check.py --discover --fill-synthetic --sidecar --json out.json --matrix out.md

For each URL: the ATS route (app.services.ats_map), then the page in the application browser — the ephemeral,
anonymous page that aborts every non-GET request, so nothing can be submitted — what blocks it (a login wall, a
CAPTCHA, a closed posting), and the form the deterministic adapter reads. `--sidecar` also reads it through the
browser-use runner (BROWSER_USE_PYTHON) with the generic reader. `--fill-synthetic` fills the adapter's text fields
with obviously synthetic values (never the candidate's data) and reads them back, to check the fill path on the
real markup; nothing is posted, and the page is closed afterwards.

Nothing is written to the database: this is evidence for the ATS matrix (docs/operations/browser-use.md). Runs on
an account's own jobs record the same facts in its `ats_routes` map as they happen.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.application_browser.ephemeral import (
    EphemeralApplicationSessions,
)
from app.infrastructure.browser import playwright_service as pw
from app.services import ats_forms, ats_map
from app.services.ats_forms import generic

BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs", ("gitlab", "airtable", "stripe")),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json&limit=3", ("palantir", "plaid")),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}", ("linear", "ramp")),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{}", ("huggingface",)),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{}/postings?limit=3", ("Visa", "bosch")),
}
SYNTHETIC = {"first_name": "Testy", "last_name": "Synthetic", "full_name": "Testy Synthetic",
             "email": "synthetic.check@example.invalid", "phone": "+65 0000 0000",
             "linkedin": "https://www.linkedin.com/in/synthetic-check", "website": "https://example.invalid",
             "github": "https://github.com/synthetic-check", "location": "Singapore"}


def discover(per_board: int = 1) -> list[str]:
    """A few live posting URLs from public board APIs (GET only, through the instrumented client)."""
    from app.services.external_api import external_client

    urls: list[str] = []
    for ats, (api, boards) in BOARDS.items():
        for board in boards:
            try:
                with external_client("JOB_BOARD", service=ats, operation="live_check", timeout=20) as client:
                    response = client.get(api.format(board))
                if response.status_code != 200:
                    continue
                data = response.json()
            except Exception:  # noqa: BLE001
                continue
            if ats == "greenhouse":
                found = [j.get("absolute_url") for j in (data.get("jobs") or [])]
            elif ats == "lever":
                found = [f"{j.get('hostedUrl')}/apply" for j in data if j.get("hostedUrl")]
            elif ats == "ashby":
                found = [f"{j.get('jobUrl')}/application" for j in (data.get("jobs") or []) if j.get("jobUrl")]
            elif ats == "smartrecruiters":
                found = [f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}" for j in (data.get("content") or [])
                         if j.get("id")]
            else:
                found = [j.get("url") or j.get("shortlink") for j in (data.get("jobs") or [])]
            urls += [u for u in found if u][:per_board]
            if found:
                break
    return urls


async def check_one(sessions: EphemeralApplicationSessions, url: str, *, fill: bool) -> dict[str, Any]:
    match = ats_map.detect(url)
    row: dict[str, Any] = {"url": url, "ats": match.ats if match else None, "route": match.route if match else None,
                           "tenant": match.tenant if match else None, "auth": match.auth_required if match else None,
                           "login_domain": match.login_domain if match else None,
                           "backend": match.backend if match else None,
                           "guarded_capable": bool(match and match.guarded_capable)}
    if match is None or match.auth_required == ats_map.UNSUPPORTED:
        row["result"] = "UNSUPPORTED_ROUTE"
        return row
    try:
        async with sessions.session(user_id=0, ats=match.ats) as page:
            load = await page.goto(url)
            html = await page.html()
            row.update(status=load.status, final_host=ats_map.detect(load.final_url).apply_domain
                       if ats_map.detect(load.final_url) else None)
            problem = ats_forms.page_problem(html, status=load.status, title=load.title)
            adapter = ats_forms.adapter_for(load.final_url, html)
            row["adapter"] = adapter.NAME if adapter else None
            if problem:
                row["result"] = problem
                return row
            form = adapter.analyze(load.final_url, html) if adapter else None
            if form is None:
                row["result"] = "NO_DETERMINISTIC_FORM" if adapter is None else ats_forms.NO_FORM
                return row
            row.update(fields=len(form.fields), required=len(form.required_fields()),
                       files=sum(1 for f in form.fields if f.kind == "file"), eeo=sum(1 for f in form.fields if f.eeo),
                       legal=sum(1 for f in form.fields if f.legal), questions=sum(1 for f in form.fields
                                                                                    if f.role == "question"),
                       submit_control=form.submit.present, fingerprint=form.fingerprint[:12])
            if fill:
                values = {f.id: SYNTHETIC[f.role] for f in form.fields
                          if f.role in SYNTHETIC and f.kind in ("text", "email", "tel", "url")}
                report = await adapter.fill(page, form, values)
                back = {fid: await page.value_of(next(f.locator for f in form.fields if f.id == fid))
                        for fid in values}
                row["synthetic_filled"] = sum(1 for v in report.values() if v == "filled")
                row["synthetic_read_back"] = sum(1 for fid, v in back.items() if v == values[fid])
            row["blocked_requests"] = page.blocked_requests
            row["result"] = "FORM_READ"
    except Exception as exc:  # noqa: BLE001
        row["result"] = f"ERROR {type(exc).__name__}"
    return row


async def check_sidecar(url: str) -> dict[str, Any]:
    from app.infrastructure.application_browser import browser_use_sidecar as bus
    from app.services import application_backends

    match = ats_map.detect(url)
    manager = bus.get_sidecars()
    with tempfile.TemporaryDirectory() as folder:
        try:
            session = await manager.start(user_id=0, task_id=0, job_id=None,
                                          allowed_hosts=application_backends.allowed_hosts(match),
                                          upload_dir=folder)
        except bus.SidecarUnavailable as exc:
            return {"sidecar": f"UNAVAILABLE {exc}"[:120]}
        try:
            await session.call("goto", url=url)
            observation = await session.call("observe", html=True)
            problem = ats_forms.page_problem(observation.get("html") or "", title=observation.get("title"))
            form = generic.analyze(observation.get("url") or url, observation.get("elements") or [])
            revealed = 0
            while form is None and not problem and revealed < 4:  # the no-cost navigator, checked by Python
                step = _reveal_step(observation, match)
                if step is None:
                    break
                await session.call(step.cmd, **step.args)
                revealed += 1
                observation = await session.call("observe", html=True)
                problem = ats_forms.page_problem(observation.get("html") or "", title=observation.get("title"))
                form = generic.analyze(observation.get("url") or url, observation.get("elements") or [])
            return {"sidecar": problem or ("FORM_READ" if form else "NEEDS_REVEAL"), "reveal_steps": revealed,
                    "sidecar_required": len(form.required_fields()) if form else 0,
                    "sidecar_fields": len(form.fields) if form else 0,
                    "sidecar_elements": len(observation.get("elements") or []),
                    "sidecar_blocked_requests": observation.get("blocked_requests")}
        except (bus.SidecarRefused, bus.SidecarUnavailable) as exc:
            return {"sidecar": f"ERROR {getattr(exc, 'code', type(exc).__name__)}"}
        finally:
            await manager.close(0, 0)


def _reveal_step(observation: dict[str, Any], match: Any) -> Any:
    """The mock navigator's next step (decline cookies, open the application) through browser_actions.check."""
    from app.prompts import build_browser_navigation_prompt
    from app.prompts.contracts import BrowserActionProposal
    from app.services import application_backends, browser_actions
    from app.services.llm import LLMClient

    prompt = build_browser_navigation_prompt(job={}, fields=[], files=[], elements=observation.get("elements") or [],
                                             page_text=observation.get("text") or "", history=[], step=1, max_steps=4)
    proposal = BrowserActionProposal.model_validate(LLMClient._mock_browser_navigation(prompt.user))
    empty = ats_forms.ApplicationForm(adapter=generic.NAME, host="", fields=[],
                                      submit=ats_forms.SubmitControl(present=False))
    step = browser_actions.check(proposal, observation, empty, {}, {},
                                 hosts=set(application_backends.allowed_hosts(match)))
    return step if isinstance(step, browser_actions.Step) else None


def matrix(rows: list[dict[str, Any]]) -> str:
    head = ("| ATS | Route | Auth | Backend | Result | Fields (required) | Files | EEO | Legal | Synthetic fill | "
            "browser-use reader |\n|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for r in rows:
        fill = f"{r.get('synthetic_read_back', '–')}/{r.get('synthetic_filled', '–')}" if "synthetic_filled" in r \
            else "–"
        reader = (f"{r.get('sidecar', '–')} ({r.get('sidecar_fields', 0)} fields, "
                  f"{r.get('sidecar_required', 0)} required, {r.get('reveal_steps', 0)} reveal steps)"
                  if "sidecar" in r else "–")
        lines.append(f"| {r['ats']} | `{r['route']}` | {r['auth']} | {r['backend']} | {r['result']} | "
                     f"{r.get('fields', '–')} ({r.get('required', '–')}) | {r.get('files', '–')} | "
                     f"{r.get('eeo', '–')} | {r.get('legal', '–')} | {fill} | {reader} |")
    return "\n".join(lines)


async def main(args: argparse.Namespace) -> list[dict[str, Any]]:
    from app.db import Base, engine

    if engine.dialect.name == "sqlite":  # a scratch database for the instrumented client's bookkeeping
        Base.metadata.create_all(engine)
    urls = list(args.urls) + (discover(args.per_board) if args.discover else [])
    browser = pw.PlaywrightBrowserService(record_telemetry=False, navigation_timeout_seconds=30)
    sessions = EphemeralApplicationSessions(browser, record_telemetry=False)
    rows = []
    try:
        for url in urls:
            row = await check_one(sessions, url, fill=args.fill_synthetic)
            if args.sidecar and row.get("adapter") is None and row["result"] not in ("UNSUPPORTED_ROUTE",):
                row.update(await check_sidecar(url))
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        await browser.stop()
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--per-board", type=int, default=1)
    parser.add_argument("--fill-synthetic", action="store_true")
    parser.add_argument("--sidecar", action="store_true")
    parser.add_argument("--json")
    parser.add_argument("--matrix")
    parsed = parser.parse_args()
    results = asyncio.run(main(parsed))
    if parsed.json:
        Path(parsed.json).write_text(json.dumps(results, indent=2))
    if parsed.matrix:
        Path(parsed.matrix).write_text(matrix(results) + "\n")
    print(matrix(results))
