# Browser-use backend, guarded submission and ATS logins (Phase 6)

What runs where, how to set it up on a local machine, what it was measured to handle on real ATS pages, and how to
roll it out. The rules are in AGENTS.md ("Guarded submission, the browser-use backend and ATS logins") and the
decisions in docs/architecture/agentic-roadmap.md (ADR-031 – ADR-035).

## How the pieces fit

```text
Job Hunt OS (FastAPI + scheduler, one process)
  application worker (app.agents.application, graph v2)
    ROUTE ─┬─ playwright        Greenhouse adapter, ephemeral POST-blocking page; GUARDED_AUTO: GuardedPage (one click)
           └─ browser_use_webui  sidecar runner per execution ──stdio JSON lines──▶ sidecars/browser_use_runner/runner.py
                                   ▲  every step decided by Python (browser_actions.check);   (web-ui conda env Python,
                                   │  the BROWSER_NAVIGATOR worker only proposes, through      browser-use 0.1.48,
                                   │  the LLM gateway                                          plain Playwright Chromium)
  browser_use_webui.probe ──GET /config──▶ browser-use Web UI (Gradio, 127.0.0.1:7788)   health check only
```

The Web UI's own agent is never driven. Its Gradio API has these problems:
- Its inputs are positional and auto-named, and their order can change on restart.
- It has no authentication, and every caller shares one global session.
- Its results come back as Markdown chat, and it has no way to veto an action before it runs.

Job Hunt OS runs the same browser-use release itself, from the checkout's own Python, so the two stay in step
(`/api/application-backends` reports both versions).

## Local setup (macOS)

1. **Install the browser-use Web UI and its Chromium.** Use the checkout's README with its own conda env
   (`/Users/rohanroy/development/AI-agents/web-ui`, env `web`). Job Hunt OS does not modify that checkout.
2. **Add the settings to `.env`.** These are local values: no developer path or address is hard-coded in the code.

   ```bash
   BROWSER_USE_WEBUI_ENABLED=true
   BROWSER_USE_PYTHON=/Users/rohanroy/miniconda3/envs/web/bin/python
   BROWSER_USE_WEBUI_PATH=/Users/rohanroy/development/AI-agents/web-ui
   BROWSER_USE_WEBUI_URL=http://127.0.0.1:7788/
   BROWSER_USE_TIMEOUT_SECONDS=300
   BROWSER_USE_HEADLESS=false          # show the window so you can take over (login, code, signup)
   ```
3. **Check it** (as an admin): `GET /api/application-backends`. It should report `sidecar.compatible: true` and
   `browser_use: 0.1.48`, and the Web UI as reachable. The warning about missing authentication means: keep the Web UI
   bound to 127.0.0.1.
4. **Run the read-only live check** whenever an ATS changes its pages:

   ```bash
   EXTERNAL_API_TELEMETRY_ENABLED=false python scripts/ats_live_check.py --discover --fill-synthetic --sidecar --matrix out.md
   ```

**Connectivity.** `127.0.0.1` is the executing process's own network namespace. In Docker or on a remote worker:
- The sidecar runs inside the Job Hunt OS container, so `BROWSER_USE_PYTHON` must point at a Python there that has
  browser-use 0.1.48 and Playwright Chromium installed.
- `BROWSER_USE_WEBUI_URL` must be a private address reachable from that container, never a published port. The Web
  UI has no authentication.
- Takeover needs a visible window. Remote and headless installs end a login or a code at NEEDS_HUMAN, and you apply
  through the Apply kit.

## Variables

| Variable | Default | What it does |
| --- | --- | --- |
| `APPLICATION_EXECUTION_MODE` | `off` | `observe`, `assist` or `guarded_auto` (needs `AGENT_GRAPHS_ENABLED`, PostgreSQL). `autonomous` is refused at startup. |
| `APPLICATION_GUARDED_ROUTES` | empty | Routes guarded runs may submit on: `greenhouse` (every tenant) or `greenhouse:acme`. Only guarded-capable ATSs count. |
| `APPLICATION_MAX_ACTIONS` | 60 | Browser actions per run, whatever proposes them. |
| `APPLICATION_TAKEOVER_TTL_MINUTES` | 15 | How long a kept window waits for you. |
| `APPLICATION_AUTH_SESSIONS_ENABLED` | false | Start runs with your saved session for the site. |
| `APPLICATION_LOGIN_DOMAINS` | empty | Domains where a saved password may be filled (exact host names). |
| `ATS_SESSION_MAX_DAYS` | 30 | Saved sessions expire after this. |
| `BROWSER_USE_WEBUI_ENABLED` | false | Use the browser-use backend for forms with no adapter. |
| `BROWSER_USE_PYTHON` | empty | The Python that has browser-use (the Web UI's env). |
| `BROWSER_USE_WEBUI_URL` / `_PATH` | empty | The Web UI to health-check / its checkout (to read its pinned version). |
| `BROWSER_USE_TIMEOUT_SECONDS` | 300 | A session's whole lifetime. |
| `BROWSER_USE_COMMAND_TIMEOUT_SECONDS` | 60 | One command; a silent runner is killed. |
| `BROWSER_USE_HEADLESS` | true | `false` shows the window (takeover). |

## ATS matrix, measured on live postings (2026-10-26)

`scripts/ats_live_check.py --discover --per-board 2 --fill-synthetic --sidecar`.
- **How it ran:** postings came from the boards' public APIs. Each page loaded in the POST-blocking application
  browser. The synthetic fill used made-up values only (never candidate data), and nothing was posted. The
  browser-use reader revealed forms with the no-cost navigator mock, every step checked by `browser_actions.check`.
- **Reading the results:** "3/5" means five fields were filled and three read back exactly. A guarded run would
  refuse on that.

| ATS | Route | Auth | Backend | Result | Fields (required) | Files | EEO | Legal | Synthetic fill | browser-use reader |
|---|---|---|---|---|---|---|---|---|---|---|
| linkedin | `linkedin` | unsupported | – | UNSUPPORTED_ROUTE | – | – | – | – | – | – |
| greenhouse | `greenhouse:gitlab` | none | playwright | FORM_READ | 21 (11) | 2 | 3 | 0 | 3/5 | – |
| greenhouse | `greenhouse:gitlab` | none | playwright | FORM_READ | 22 (11) | 2 | 3 | 0 | 5/5 | – |
| lever | `lever:palantir` | none | browser_use_webui | NO_DETERMINISTIC_FORM | – | – | – | – | – | FORM_READ (54 fields, 43 required) |
| lever | `lever:palantir` | none | browser_use_webui | NO_DETERMINISTIC_FORM | – | – | – | – | – | FORM_READ (53 fields, 41 required) |
| ashby | `ashby:linear` | unsupported | – | UNSUPPORTED_ROUTE | – | – | – | – | – | – |
| workable | `workable` | none | browser_use_webui | NO_DETERMINISTIC_FORM | – | – | – | – | – | FORM_READ (18 fields, 11 required, 2 reveal steps) |
| workable | `workable` | none | browser_use_webui | NO_DETERMINISTIC_FORM | – | – | – | – | – | FORM_READ (17 fields, 11 required, 2 reveal steps) |

What that means per ATS:

| ATS | OBSERVE / ASSIST | GUARDED_AUTO | A person is needed for |
| --- | --- | --- | --- |
| Greenhouse (classic `boards.greenhouse.io` and new `job-boards.greenhouse.io`) | Adapter reads and fills (Playwright); a new board's required dropdowns send the run to browser-use, which sets them with `choose` | Capable; only when allowlisted. Confirmation rule tested on the fake ATS only; no real submission made yet | Custom select widgets (they may not read back, so no click), EEO, unapproved attestations |
| Lever | browser-use reader (deterministic fill, navigator for the rest) | Not capable | Final submit, anything unread back |
| Workable | browser-use reader after declining cookies and opening the Application tab | Not capable | Final submit |
| Workday, iCIMS, Taleo, SuccessFactors, SmartRecruiters | browser-use reader, with a per-tenant login (saved session or vault) | Not capable | First login or signup, codes, final submit. Not measured live: these have no public posting API; run the live check on your own approved jobs' URLs |
| Ashby | Unsupported: its form loads through GraphQL POST, which the application browsers never send | No | The whole application (Apply kit) |
| LinkedIn Easy Apply, Indeed Apply | Unsupported (in-board, behind the board's login) | No | The whole application |
| `ats_api` | None: no ATS offers a candidate-side submission API Job Hunt OS may use | – | – |

## Rollout

Each step is separately reversible. A merged pull request turns nothing on.

1. **Browser-use for reading.** Set `BROWSER_USE_WEBUI_ENABLED` and `APPLICATION_EXECUTION_MODE=observe`. Watch
   Running workers → Application and Monitoring → External APIs (provider BROWSER_USE) for a few days.
2. **`assist`.** You submit every application yourself; the browser window can stay open for you (headful).
3. **Logins.** Saved sessions first (`APPLICATION_AUTH_SESSIONS_ENABLED`), then the vault for chosen domains
   (`APPLICATION_LOGIN_DOMAINS`).
4. **Guarded submission.** Requires your explicit go-ahead for one real application chosen by you.
   - Set `APPLICATION_EXECUTION_MODE=guarded_auto` and `APPLICATION_GUARDED_ROUTES=greenhouse:<one tenant>`.
   - Turn it on in Settings → Guarded submission, with the attestations you approve.
   - Watch the first run end CONFIRMED with the site's evidence before you widen the allowlist.
5. **Rollback.** `APPLICATION_EXECUTION_MODE=assist` stops guarded runs immediately; uncertain submissions stay for
   you to resolve. `BROWSER_USE_WEBUI_ENABLED=false` sends every form back to the Phase 5 path.

## Security notes

These came out of inspecting browser-use 0.1.48 and the Web UI:
- **No debugging port, no anti-detection flags.** browser-use's built-in launcher opens Chrome's remote-debugging
  port 9222 and adds `--disable-blink-features=AutomationControlled`, an anti-detection flag. The runner launches
  plain Playwright Chromium instead.
- **Telemetry off.** browser-use sends anonymous telemetry unless `ANONYMIZED_TELEMETRY=false`; the runner sets it
  before importing.
- **Web UI exposure.** The Web UI listens without authentication, and its Docker setup binds `0.0.0.0` and a VNC
  password default. Keep it on 127.0.0.1.
- **No secrets in the runner.** The runner's environment holds PATH and HOME only: no API key, database URL or
  `SECRET_KEY`.
- **No POST without a token.** Non-GET requests are blocked in the runner until Job Hunt OS signs a token for one
  domain (a LOGIN today; a SUBMIT only through `submission`, unused because guarded runs need an adapter).
- **A LOGIN token arms only the login domain.** It allows non-GET requests to the login domain only, for 30 seconds
  around that one click. On ATSs whose login and application share a host (Workday), that is the login page's own
  request; no application form is on that page.
- **The takeover window is the person's.** It allows the person's own POSTs to the route's hosts. A person could
  submit there without starting the submission in the Apply kit; a later run finds the form gone and stops.
  Submissions made entirely outside Job Hunt OS remain reconcile-only.
