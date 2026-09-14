# Job Hunt OS v0.5 — Guided application workspace

A human-in-the-loop AI workflow for personal job hunting:

**Apify Hunter → Ranker → Tailor → Reviewer → Approval → Application Tracker → Follow-up**

It deliberately does **not** bypass anti-bot/CAPTCHA controls. It discovers and prepares applications, then either queues them for human approval or auto-approves reviewed materials according to your settings. External submission remains a user action.

## Workspace (v0.5 redesign)

The dashboard is a single-page workspace served from `app/static/app.js`; every number on it comes from the API.

| Screen | What it does | Main endpoints |
| --- | --- | --- |
| Overview | 30-day funnel, reply rate per board, next 10 days, weekly trend, time in stage | `GET /api/workspace/overview` |
| Today | "Do this next" (approve → tailor → follow up), follow-ups due, weekly pace; run-sheet variant | `GET /api/workspace/today` |
| Discovery · triage | One job at a time with fit reasons; keys `S` shortlist, `M` maybe, `X` skip, `⏎` shortlist & tailor | `GET /api/jobs/search?queue=true`, `POST /api/jobs/{id}/triage` |
| Discovery · table | Filter, sort, paginate and bulk-shortlist the whole pipeline | `GET /api/jobs/search`, `POST /api/jobs/bulk-triage` |
| Discovery · setup | Saved search profile (roles × locations × boards), run history with what each run searched and pulled, add a job from a URL | `GET/PUT /api/search/profile`, `POST /api/search/profile/run`, `GET /api/search/apify/runs/{id}/detail`, `POST /api/jobs/from-url` |
| Tailor | Before → after edits with the evidence each one cites; edit, accept or revert; cover letter; keyword coverage; PDF/DOCX/TXT exports | `GET /api/tailor/{job_id}`, `PATCH /api/resumes/{id}/edits/{n}` |
| Approvals | Pre-submit checklist, resume preview, reusable answers, notes; approve opens the apply kit | `GET /api/approvals/{job_id}`, `POST /api/jobs/{id}/revision` |
| Apply kit | Files to upload, answers to paste, link to the posting, confirm "mark applied" | `GET /api/jobs/{id}/apply-kit` |
| Tracker | Kanban by post-submission stage (drag or "move to"), optional extra stages, timeline, recruiter, interview scheduling, AI follow-up draft | `GET /api/tracker`, `POST /api/applications/{id}/stage`, `POST /api/applications/{id}/events` |
| Knowledge bank | ATS answer library with edit/verify, evidence bank with verify/remove | `/api/knowledge`, `/api/evidence` |
| Settings & profile | Candidate details and chip fields, resume variants, re-parse resume into (unverified) evidence, provider/model, approval mode, cadence | `/api/profile`, `/api/settings`, `/api/preferences` |

`⌘K` / `Ctrl+K` opens a command palette that searches every job.

Auto-submit is intentionally not offered: approved packets always become an apply kit, and the submit click stays yours.

## What works

- Candidate profile + master resume
- TXT, PDF, and DOCX resume upload with local storage and text extraction
- Dedicated Discovery, Approvals, Knowledge Bank, and Settings workspaces
- Reusable, human-verifiable ATS question answers
- Manual or review-threshold-based automatic approval
- Verified evidence bank to prevent resume hallucinations
- **Apify-first multi-board job discovery**
- Default Apify Actor searches LinkedIn, Indeed, Glassdoor, Naukri, Bayt and ZipRecruiter
- Configurable Apify Actor ID and custom Actor input escape hatch
- Asynchronous Actor runs persisted in PostgreSQL/SQLite
- Automatic Apify dataset ingestion + cross-run deduplication
- Lever and Greenhouse direct imports as optional fallbacks
- AI job-fit scoring
- AI resume tailoring constrained to verified evidence
- Independent hiring-manager review score
- Human approval gate
- Application tracking + follow-up due date
- AI-generated follow-up drafts
- Simple PDF export
- HTTP Basic protection for public hosting
- Prompt-injection hardening for untrusted job descriptions
- OpenAI, Anthropic, **OpenRouter**, or no-cost mock provider
- OpenRouter `openrouter/free` support plus arbitrary `:free` model slugs
- Per-stage OpenRouter models for ranking, tailoring, review, and follow-up
- OpenRouter model fallbacks and a free-only safety guard
- SQLite locally; PostgreSQL in production
- Docker deployment
- Separate cron command for scheduled follow-ups

## 1. Local run

```bash
cp .env.example .env
# Add APIFY_API_TOKEN and one LLM API key, or use LLM_PROVIDER=mock for no-cost LLM testing.
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload
```

Open http://localhost:8000. For any public deployment, set `APP_USERNAME` and `APP_PASSWORD`.

Uploaded resumes are stored in `./data/resumes` by default. Set `UPLOAD_DIR` to a
persistent private volume in production and `MAX_RESUME_UPLOAD_MB` to change the
default 8 MB limit. Files must not be placed on a publicly served path.

On startup, the application creates the new settings, resume-document, and knowledge
tables and applies its idempotent compatibility column migration. Back up an existing
production database before the first v0.4 startup.

For PostgreSQL:

```bash
docker compose up --build
```

## 2. Apify job discovery

Set:

```env
APIFY_API_TOKEN=apify_api_...
APIFY_ACTOR_ID=openclawai/job-board-scraper
```

The default Actor supports multi-term search plus filters such as location, boards, results per board, recency, remote-only, job type and Easy Apply.

The dashboard starts the Actor **asynchronously**. The app persists the Apify run ID, polls its status, and imports the default dataset after the run succeeds. This avoids keeping a Render/Railway HTTP request open while the scraper runs.

### Why the Actor is configurable

Apify Actors do not all use the same input/output schema. `APIFY_ACTOR_ID` can point to another Actor. Use `custom_input` to add/override fields on the default schema, or `raw_actor_input` to send an entirely different Actor input object verbatim. The normalizer understands the default Actor plus common names such as `jobTitle`, `companyName`, `applyUrl`, `jobUrl`, and `jobDescription`.

### Default Actor inputs used by this app

```json
{
  "searchTerms": ["Engineering Manager", "Lead AI Engineer"],
  "location": "Singapore",
  "sites": ["linkedin", "indeed", "glassdoor"],
  "maxResults": 20,
  "hoursOld": 168,
  "isRemote": false,
  "linkedinFetchDescription": true,
  "descriptionFormat": "markdown"
}
```

Use smaller `maxResults` values while testing. Apify Actor pricing is Actor-specific, so check the chosen Actor's live pricing before large searches.

## 3. LLM configuration

### Default: OpenRouter free models

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=openrouter/free
OPENROUTER_REQUIRE_FREE=true
```

`openrouter/free` is OpenRouter's free-model router. It chooses from currently available free models that can satisfy the request. You can also pin any currently available explicit free model by using its OpenRouter slug ending in `:free`.

The free-model catalog changes, so the application does **not** hard-code a permanent list of model names. `openrouter/free` is the most maintenance-friendly default.

### Use different models by workflow stage

All stage overrides are optional. A blank value inherits `OPENROUTER_MODEL`.

```env
OPENROUTER_MODEL=openrouter/free
OPENROUTER_MODEL_RANK=openrouter/free
OPENROUTER_MODEL_TAILOR=vendor/model-name:free
OPENROUTER_MODEL_REVIEW=openrouter/free
OPENROUTER_MODEL_FOLLOWUP=openrouter/free
```

This lets you keep high-volume ranking/follow-up work on free inference while selecting a stronger model only for resume tailoring or final review.

### Free-only spend protection

```env
OPENROUTER_REQUIRE_FREE=true
```

When enabled, Job Hunt OS refuses to call any configured OpenRouter model that is neither `openrouter/free` nor a `:free` variant. This prevents an accidental paid-model bill.

To intentionally allow paid OpenRouter models:

```env
OPENROUTER_REQUIRE_FREE=false
OPENROUTER_MODEL_REVIEW=anthropic/<model-slug>
```

Always copy current model slugs from the OpenRouter model browser rather than assuming a historical slug is still valid.

### Optional model fallback chain

```env
OPENROUTER_FALLBACK_MODELS=vendor/backup-model:free,openrouter/free
```

The fallback list is passed to OpenRouter in priority order. Provider-level failover is handled by OpenRouter, and the configured model list provides an additional model-level fallback layer. With `OPENROUTER_REQUIRE_FREE=true`, every fallback must also be free.

### Optional OpenRouter attribution headers

```env
OPENROUTER_SITE_URL=https://your-job-hunt.example
OPENROUTER_APP_NAME=Job Hunt OS
```

### Direct OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
```

### Direct Anthropic

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5
```

### No LLM spend / local tests

```env
LLM_PROVIDER=mock
```

The SDK/client libraries do not remove model inference cost. The model layer is isolated so providers can be switched without changing the workflow.

## 4. API endpoints

Start an Apify search:

```http
POST /api/search/apify
```

Example body:

```json
{
  "search_terms": ["Engineering Manager", "Lead AI Engineer"],
  "location": "Remote",
  "sites": ["linkedin", "indeed"],
  "max_results": 20,
  "hours_old": 72,
  "is_remote": true,
  "linkedin_fetch_description": true
}
```

Poll/import a run:

```http
GET /api/search/apify/runs/{run_id}
```

List recent runs:

```http
GET /api/search/apify/runs
```

FastAPI interactive docs are available at `/docs`.

## 5. Cheap production architecture

For one user, do **not** start with Redis, Celery, Kafka, a vector database, or a dedicated browser worker.

- Web/API: one small Render or Railway service
- DB: Neon Free or Railway Postgres
- Job discovery: Apify Actor runs
- LLM: OpenRouter free router for most tasks; optionally use a stronger paid model only for the best jobs
- Follow-up scheduling: Render/Railway cron running `python -m app.cron` (drafts only, never sends)
- Search scheduling: a daily cron running `python -m app.cron searches`, or `POST /api/cron/searches` with the `X-Cron-Secret` header. It runs the saved search profile only when its schedule is set to daily.

## 6. Recommended next upgrades

- Apify webhook endpoint so completed runs are ingested without polling
- Gmail integration for follow-up drafts and thread matching
- Resume DOCX template preservation
- Company watchlists
- Recruiter discovery/manual outreach queue
- Evaluation dataset: application → response → interview → offer conversion
- Evaluate stage-specific OpenRouter routing from application conversion data
- Optional Playwright **assistive** form filling where permitted, with a hard stop before submit

See [DEPLOY.md](DEPLOY.md) for hosting instructions.

## 7. Google Gemini agent deployment

The repository includes a Google ADK package named `job_hunt_agent`. Its
`root_agent` coordinates four narrowly scoped agents: `job_fit_ranker`,
`resume_tailor`, `resume_reviewer`, and `follow_up_writer`. The agents use Gemini
for reasoning and validated database tools for reads/writes; no tool approves,
submits, or sends an application.

Run it locally after installing the project:

```bash
gcloud auth application-default login
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
export DATABASE_URL="postgresql+psycopg://..."
pip install -e .
adk web
```

In the ADK web UI, select `job_hunt_agent` and use stored numeric IDs, for
example: `Rank job 42`. To deploy the same package to Vertex AI Agent Engine:

```bash
gcloud services enable aiplatform.googleapis.com
gcloud storage buckets create "gs://${GOOGLE_CLOUD_PROJECT}-agent-staging" \
  --location="${GOOGLE_CLOUD_LOCATION}"
adk deploy agent_engine \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${GOOGLE_CLOUD_LOCATION}" \
  --staging_bucket="gs://${GOOGLE_CLOUD_PROJECT}-agent-staging" \
  job_hunt_agent
```

Use a reachable PostgreSQL `DATABASE_URL` in Agent Engine rather than SQLite:
local SQLite state is ephemeral and is not shared between deployed instances.
Grant the deployment identity access to Vertex AI, the staging bucket, and the
database/network secrets it needs. See [DEPLOY.md](DEPLOY.md) for limitations
and operational checks.
