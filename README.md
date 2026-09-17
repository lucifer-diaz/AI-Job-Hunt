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
| Discovery · table | Filter, sort, paginate and bulk-shortlist the whole pipeline. The stage filter includes "Unranked" (`stage=unranked`: jobs without a fit score) | `GET /api/jobs/search`, `POST /api/jobs/bulk-triage` |
| Discovery · ranking tracker | The asynchronous ranker's progress for your account: jobs being filtered, queued, ranking now, retrying or waiting (with the reason and when it retries), and rankings finished in the last 24 hours with fit, candidate and resume scores and the qualify/archive decision. Retry failed rankings, queue unranked jobs; refreshes itself while work is in flight | `GET /api/ranking/tracker`, `POST /api/jobs/{id}/rank`, `POST /api/jobs/rank-batch` |
| Discovery · setup | Saved search profile (roles × locations × boards), run history with what each run searched and pulled, add a job from a URL | `GET/PUT /api/search/profile`, `POST /api/search/profile/run`, `GET /api/search/apify/runs/{id}/detail`, `POST /api/jobs/from-url` |
| Tailor | Before → after edits with the evidence each one cites; edit, accept or revert; cover letter; keyword coverage; PDF/DOCX/TXT exports. "Skip tailoring" sends the master resume unchanged (still reviewed) for postings that need no tailoring. The "Bulk tailor" view tailors or skips tailoring for many queued jobs one at a time, with progress and stop | `GET /api/tailor/{job_id}`, `PATCH /api/resumes/{id}/edits/{n}`, `POST /api/jobs/{id}/tailor`, `POST /api/jobs/{id}/skip-tailoring`, `POST /api/jobs/bulk-tailor` |
| Approvals | Pre-submit checklist, resume preview, reusable answers, notes; approve opens the apply kit | `GET /api/approvals/{job_id}`, `POST /api/jobs/{id}/revision` |
| Apply kit | Files to upload, answers to paste, link to the posting, confirm "mark applied" | `GET /api/jobs/{id}/apply-kit` |
| Tracker | Kanban by post-submission stage (drag or "move to"), optional extra stages, timeline, recruiter, interview scheduling, AI follow-up draft | `GET /api/tracker`, `POST /api/applications/{id}/stage`, `POST /api/applications/{id}/events` |
| Monitoring | Every LLM request: requests, tokens, estimated cost, latency (avg/p50/p95/p99), failures by type, retries, context utilisation, usage by stage and cost by model, with filters (range, provider, model, stage, prompt version, status, job), a request detail view and the AI Optimization Advisor | `GET /api/monitoring/dashboard`, `POST /api/monitoring/analyze`, `GET /api/monitoring/recommendations` |
| Knowledge bank | ATS answer library with edit/verify, evidence bank with verify/remove | `/api/knowledge`, `/api/evidence` |
| Settings & profile | Candidate details and chip fields, resume variants, re-parse resume into (unverified) evidence, provider/model, your own API keys (stored encrypted), approval mode, cadence | `/api/profile`, `/api/settings`, `PUT /api/settings/keys`, `/api/preferences` |
| Sign-in | Create an account (email, password, confirm password), sign in, sign out | `/signup`, `/login`, `POST /api/auth/signup`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` |

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
- Email and password accounts: each account has its own jobs, resumes, settings, schedule and encrypted API keys
- Prompt-injection hardening for untrusted job descriptions
- OpenAI, Anthropic, Gemini, **OpenRouter**, or no-cost mock provider
- LLM monitoring (tokens, context size, latency, estimated cost, failures) and a recommend-only AI Optimization Advisor
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

Set `SECRET_KEY` in `.env` first (it encrypts the API keys people save), then open http://localhost:8000 and create an account. See [Accounts and data isolation](#accounts-and-data-isolation).

Uploaded resumes are stored in `./data/resumes` by default. Set `UPLOAD_DIR` to a
persistent private volume in production and `MAX_RESUME_UPLOAD_MB` to change the
default 8 MB limit. Files must not be placed on a publicly served path.

On startup, the application runs its Alembic migrations (`app/migrations`) to the latest
revision. You can also run them explicitly with `python -m app.db_migrations` or `alembic upgrade head`.
Installs created before migrations existed are upgraded in place. Back up an existing production
database before upgrading.

### Local PostgreSQL (recommended)

With a local Postgres (for example `brew install postgresql@16 && brew services start postgresql@16`):

```bash
scripts/setup_local_postgres.sh          # creates jobhunt and jobhunt_test for $USER
```

Then set `DATABASE_URL=postgresql+psycopg://<your-user>@localhost:5432/jobhunt` in `.env` and start the app; tables are created and migrated on startup. To run the tests on Postgres instead of throwaway SQLite files:

```bash
TEST_DATABASE_URL=postgresql+psycopg://<your-user>@localhost:5432/jobhunt_test pytest
```

Or run the app and Postgres in containers:

```bash
docker compose up --build
```

## Accounts and data isolation

Anyone who can reach the site can create an account at `/signup` with an email address, a password (at least 10 characters) and the password again. Set `ALLOW_SIGNUP=false` to close sign-up once everyone you want has joined.

- **Your data is yours.** Jobs, resumes, evidence, knowledge answers, search profile and profiles, settings, search runs, applications, their events, tailored resumes, follow-ups and workflow history all belong to one account. Ownership is in the schema itself: every one of those tables has a `user_id` foreign key to `users`, indexed, `NOT NULL`, and `ON DELETE CASCADE`, so deleting an account removes its data and no row can exist without an owner.
  - **In the ORM:** `user.jobs`, `user.applications`, `user.followups`, `user.search_profiles` and so on, with `job.user` in the other direction — no manual `user_id` filtering needed.
  - **Enforced centrally** in `app/tenancy.py`, not endpoint by endpoint: every database session opened for a signed-in request only reads, updates and deletes that account's rows, and `get()` by ID returns nothing for another account's row, so another account's IDs simply return 404.
  - **New rows are stamped automatically.** A row that hangs off a job or an application (a tailored resume, an application, its events, follow-ups, workflow history) takes its owner from that parent, so work done by the scheduler and the workers — whose sessions belong to no one — is owned too.
  - An install that still holds data from before accounts existed upgrades with those rows unowned; the first account claims them, and `app.db_migrations.enforce_ownership` makes the columns `NOT NULL` on the next start.
- **Your API keys.** Each account saves its own OpenRouter, OpenAI, Anthropic and Apify keys under **Settings → Your API keys**. Searches, ranking, tailoring and follow-ups always use the account's own keys, with no fallback to anyone else's.
  - Keys are encrypted with `SECRET_KEY` and never returned by the API; only the last four characters are shown.
  - Keep `SECRET_KEY` stable and private. Changing it makes saved keys unreadable, and everyone has to re-enter them.
  - Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **The first account.** The first account ever created becomes the admin. It takes ownership of everything that existed before accounts were introduced, so an existing install carries on unchanged. Any `*_API_KEY` / `APIFY_API_TOKEN` values in `.env` are copied into that account, encrypted. After that, server `.env` keys are not used for anyone's work.
- **Running the system your way.** Every account's own settings control what the scheduler does for it:
  - its search schedule (manual or daily);
  - the **Auto-rank on import** switch;
  - its AI provider and models;
  - its approval mode and follow-up cadence.

  Accounts without the needed key are simply skipped until they add one.
- **Sessions.** Passwords are hashed with scrypt. Signing in sets an HttpOnly, SameSite=Lax cookie holding a random token.
  - Only the token's SHA-256 is stored, and signing out revokes it on the server.
  - Sessions last `SESSION_DAYS` (30).
  - The cookie is `Secure` over HTTPS; force it with `SESSION_COOKIE_SECURE=true` behind a TLS proxy.
  - Five wrong passwords for an email block sign-in for 15 minutes, counted per app process.
  - Cross-site write requests are rejected.
- **Admin-only views.** The scheduler status shows job results and errors to the admin only. `ADMIN_TOKEN` still protects manual scheduler triggers and `CRON_SECRET` still protects `/api/cron/*`.

## 2. Apify job discovery

Save your Apify API token under **Settings → Your API keys** (create one in Apify Console → Settings → Integrations). The server sets the default actor:

```env
APIFY_ACTOR_ID=openclawai/job-board-scraper
```

### Multiple actors

Discovery → Search setup lets you tick one or more actors. One search profile is translated into each actor's own input schema (`app/services/actors.py`), and each actor gets the filters it supports:

| Actor | Runs | Filters it receives |
| --- | --- | --- |
| `openclawai/job-board-scraper` (Multi Job Board) | one per location, all roles | `searchTerm`/`searchTerms`, `location`, `sites`, `maxResults`, `hoursOld`, `jobType`, `isRemote`, `easyApply`, `countryIndeed`, `distance`, `offset`, `linkedinFetchDescription`, `linkedinCompanyIds`, `enforceAnnualSalary`, `proxyConfiguration` |
| `fantastic-jobs/advanced-linkedin-job-search-api` (Advanced LinkedIn) | one for everything | `titleSearch`, `locationSearch` (needs `City, State, Country`), `timeRange`, `limit`, `aiWorkArrangementFilter`, `aiEmploymentTypeFilter`, `seniorityFilter`, `directApply`, `hasSalary`, `aiVisaSponsorshipFilter`, `removeAgency`, `titleExclusionSearch` |
| `benthepythondev/remote-jobs-aggregator` | one per role | `searchKeywords`, `platforms`, `maxJobsPerPlatform`, `postedWithinDays`, `locationKeywords`, `jobTypes`, `seniorityLevels`, `salaryOnly`, `excludeKeywords` |
| `shahidirfan/Remoteok-Job-Scraper` | one per role | `startUrl`, `keyword`, `results_wanted` |

Conflicting filters are reported before a run starts (for example, Multi Job Board drops `hoursOld` when Easy Apply is on). "Show exact inputs" displays the JSON each run will send, and one profile may start at most 30 runs.

**Your own actors:** "+ Add your own actor" takes a Store actor ID, how it should fan out (per role × location, per location, per role, or once) and a JSON input template. "Look up on Apify" confirms the actor exists and pre-fills the template from its input schema. Placeholders: `{{role}}`, `{{roles}}`, `{{location}}`, `{{locations}}`, `{{max_results}}`, `{{hours_old}}`, `{{days_old}}`, `{{is_remote}}`, `{{job_type}}`, `{{sites}}`, `{{country}}`. A value that is only a placeholder keeps its type. Output items are normalized from common field names (title, company/organization, location/locations_derived, url/apply_url, description/description_text).

The dashboard starts the Actor **asynchronously**. The app persists the Apify run ID, polls its status, and imports the default dataset after the run succeeds. This avoids keeping a Render/Railway HTTP request open while the scraper runs.

### Hunter API and search profiles

The Hunter runs searches asynchronously and imports their results later:

```text
SearchProfile → Apify Hunter → async actor runs → sync results → normalize → deduplicate → RANK_PENDING
```

Start a search (it returns as soon as the actor runs are started, never waiting for the scraper):

```http
POST /api/hunter/search
```

```json
{
  "roles": ["Engineering Manager", "Head of Engineering", "Lead AI Engineer"],
  "locations": ["Singapore", "Dubai"],
  "sites": ["linkedin", "indeed", "glassdoor"],
  "hours_old": 72,
  "max_results": 20,
  "remote_only": false
}
```

One run is started per location, with up to 5 roles each (more roles are split into further runs). Every run is saved in `search_runs` with its Apify run ID and status: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED` or `NEEDS_INVESTIGATION`. A run that fails to start never stops the others.

**Actors and adapters.** Each actor has its own adapter in `app/services/apify_adapters.py`, which builds input using only the fields that actor's schema publishes and converts its output into the internal job. Actor IDs are configuration (`.env.example`), never hard-coded in the business logic.

| Actor | Setting | Runs | Recency filter |
| --- | --- | --- | --- |
| `openclawai/job-board-scraper` | `APIFY_PRIMARY_JOB_ACTOR` | one per location, 5 roles each | `hoursOld` (exact) |
| `thirdwatch/linkedin-jobs-scraper` | `APIFY_LINKEDIN_ACTOR` | one per location, 5 queries each | `datePosted` (24h/week/month) |
| `crawlerbros/indeed-jobs-scraper` | `APIFY_INDEED_ACTOR` | one per role and location | none: filtered after import |
| `crawlerbros/glassdoor-jobs-scraper` | `APIFY_GLASSDOOR_ACTOR` | one per role and location | none: filtered after import |
| `crawlerbros/naukri-scraper` | `APIFY_NAUKRI_ACTOR` | one per role and city | `jobAge` (1/3/7/15/30 days) |

`"mode": "fallback"` searches each board with its dedicated actor instead of the multi-board one (ZipRecruiter and Bayt have no dedicated actor, so they stay on the multi-board one). When a multi-board run fails, its boards are searched once more with their fallback actors automatically (`APIFY_AUTO_FALLBACK`).

**Posting dates.** `posted_at` is the employer's posting date and is never replaced by `discovered_at`. Relative values ("3 days ago", Glassdoor's `ageInDays`) are counted back from the time the actor scraped the page, and the original value is kept in `posted_date_raw`. Values that are not exact ("30+ days ago", "2 months ago") leave `posted_at` empty rather than guessing, and a job is never dropped for being old when its date is unknown.

**Deduplication** uses, in order: the board's own job ID, the canonical posting or apply URL, then company + title + city. A posting found again by another role, board, profile or scheduled run adds an entry to the job's `discoveries` (role, location, board, actor, run) and fills in details the first discovery lacked, instead of creating a second job. Every job records `matched_search_role`, `matched_search_location`, `source_board`, `apify_actor_id` and `apify_run_id`, so you can later ask which role, location or board produces the best results.

**Search profiles** are reusable searches the scheduler runs on their own cadence:

```http
GET|POST /api/hunter/profiles          PUT|DELETE /api/hunter/profiles/{id}
POST /api/hunter/profiles/{id}/run     GET /api/hunter/runs      POST /api/hunter/runs/sync
```

A profile stores its roles, locations, sites, `hours_old`, `remote_only`, `job_type`, `max_results`, whether it is `enabled`, and `last_run_at` / `next_run_at`. The scheduler's `hunter` job starts every enabled profile that is due (a guarded UPDATE means two app instances cannot start it twice), and the `apify_sync` job imports each finished run.

**When something goes wrong:** a failed, aborted or timed-out run still imports whatever it collected; an empty dataset is a normal, successful run; items without a title or company are skipped and counted; and if a dataset no longer carries the fields an adapter needs, the run is marked `NEEDS_INVESTIGATION` and imports nothing rather than creating wrong jobs. Every run's counts (created, duplicates, missing dates, dropped as too old) are on the run itself.

### Why the Actor is configurable

Apify Actors do not all use the same input/output schema. `APIFY_ACTOR_ID` can point to another Actor. Use `custom_input` to add/override fields on the default schema, or `raw_actor_input` to send an entirely different Actor input object verbatim. The normalizer understands the default Actor plus common names such as `jobTitle`, `companyName`, `applyUrl`, `jobUrl`, and `jobDescription`.

### Default Actor inputs used by this app

```json
{
  "searchTerm": "Engineering Manager",
  "searchTerms": ["Engineering Manager", "Lead AI Engineer"],
  "location": "Singapore",
  "sites": ["linkedin", "indeed", "glassdoor"],
  "maxResults": 20,
  "hoursOld": 168,
  "isRemote": false,
  "easyApply": false,
  "countryIndeed": "singapore",
  "distance": 50,
  "linkedinFetchDescription": true,
  "enforceAnnualSalary": false,
  "descriptionFormat": "markdown",
  "proxyConfiguration": {"useApifyProxy": true, "apifyProxyGroups": ["RESIDENTIAL"]}
}
```

`searchTerms` is only sent when there is more than one role.

Use smaller `maxResults` values while testing. Apify Actor pricing is Actor-specific, so check the chosen Actor's live pricing before large searches.

## 3. LLM configuration

API keys are saved per account under **Settings → Your API keys**. The `*_API_KEY` values below only seed the first account at sign-up; the other variables set server-wide defaults (provider, models, free-only guard) that each account can override in Settings.

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

### Direct Gemini

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

Uses a Gemini API (Google AI Studio) key through the `google-genai` SDK. The ADK agent (section 11) keeps using
Vertex AI credentials.

### No LLM spend / local tests

```env
LLM_PROVIDER=mock
```

The SDK/client libraries do not remove model inference cost. The model layer is isolated so providers can be switched without changing the workflow.

### LLM prompts

All production prompts live under `app/prompts/`. Backend services must not define long LLM prompts inline.

Each prompt has:
- a dedicated builder (`build_ranking_prompt`, `build_tailoring_prompt`, `build_review_prompt`, ...) that returns a provider-independent `Prompt`;
- a version identifier (`RANKING_PROMPT_VERSION = "v1"`, ...), stored with the result where the data model has room (`jobs.fit_analysis`, `resume_variants.content` and `reviewer_notes`);
- structured input: trusted instructions in the system message, data in labelled sections of the user message, with job descriptions, resumes and emails wrapped in `<untrusted_...>` tags;
- a structured JSON output whose fields are listed in the instructions.

| Module | Builders | Used by |
|---|---|---|
| `ranking.py` | `build_ranking_prompt` | the Job Ranker (`app.services.ranker`) |
| `tailoring.py` | `build_tailoring_prompt` | `tailor_job` |
| `review.py` | `build_review_prompt` | `review_resume` |
| `followup.py` | `build_followup_prompt` | `draft_followup` |
| `resume_parser.py` | `build_evidence_extraction_prompt`, `build_resume_parser_prompt` | evidence parsing; the structured parser is not wired in yet |
| `application.py` | `build_application_question_prompt` | not wired in yet |
| `inbox.py` | `build_inbox_classification_prompt` | not wired in yet (inbox sync is a placeholder) |
| `agents.py` | ADK agent instructions | `job_hunt_agent` |

When you change a prompt's wording or output fields, bump its version.

## 4. API endpoints

Start a Hunter search (asynchronous; see "Hunter API and search profiles" above):

```http
POST /api/hunter/search
```

Older, single-actor search endpoint:

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
- Scheduling: built into the web app (see section 7). No separate cron service is required; `python -m app.cron <job>` remains available for hosts that sleep between requests.

## 6. Workflow state engine

PostgreSQL is the source of truth for where every job is in its lifecycle. The foundation is in
place; the automated workers that move jobs through it are the next step.

```text
Scheduler (cron)
  → Python Job Hunt OS
  → PostgreSQL: jobs.state + retry/lease columns + workflow_transitions history
  → deterministic workers that claim, work, then complete or fail   (app.services.work_queue)
  → AI reasoning only inside a worker, never deciding the state      (not in this layer)
```

### States and transitions

`app/services/lifecycle.py` is the only definition of the lifecycle. Nothing else decides
whether a state change is legal.

| From | Allowed next states |
| --- | --- |
| DISCOVERED | NORMALIZED, SKIPPED, FAILED |
| NORMALIZED | FILTERED, SKIPPED, FAILED |
| FILTERED | RANK_PENDING, SKIPPED, FAILED |
| RANK_PENDING | RANKING, SKIPPED, FAILED |
| RANKING | RANKED, RANK_PENDING (retry or wait), SKIPPED |
| RANKED | QUALIFIED, ARCHIVED, RANK_PENDING (re-rank), SKIPPED, FAILED |
| QUALIFIED | TAILORING, ARCHIVED, RANK_PENDING (re-rank), SKIPPED |
| TAILORING | TAILORED, SKIPPED, FAILED |
| TAILORED | REVIEWED, SKIPPED, FAILED |
| REVIEWED | READY_TO_APPLY, TAILORING (revision), SKIPPED, FAILED |
| READY_TO_APPLY | APPLYING, ARCHIVED, SKIPPED |
| APPLYING | APPLIED, SKIPPED, FAILED |
| APPLIED | FOLLOWUP_DUE, INTERVIEW, REJECTED, OFFER, ARCHIVED |
| FOLLOWUP_DUE | FOLLOWED_UP, INTERVIEW, REJECTED, OFFER, ARCHIVED |
| FOLLOWED_UP | FOLLOWUP_DUE, INTERVIEW, REJECTED, OFFER, ARCHIVED |
| INTERVIEW | OFFER, REJECTED, ARCHIVED |
| OFFER, REJECTED, ARCHIVED, SKIPPED, FAILED | terminal |

- **No arbitrary states.** `jobs.state` accepts only these values. This is enforced three times:
  a strict parser, an ORM validator that also rejects illegal moves on direct assignment, and
  named PostgreSQL `CHECK` constraints.
- **No shortcuts.** There is no path around NORMALIZED, FILTERED, RANK_PENDING, RANKING, RANKED, TAILORED or REVIEWED.
- **Re-ranking.** A job archived by ranking (from RANKED or QUALIFIED) may be reopened, only to RANK_PENDING. A job
  archived for any other reason, for example after applying, cannot.
- **FAILED only before submission.** Once a job is APPLIED it can never become FAILED, so a broken
  follow-up can't erase the fact that you applied.
- **Requeue.** FAILED is terminal for automation. An operator can call `requeue_failed`, which only
  returns the job to the state it failed in.

Every change is recorded in `workflow_transitions` with from/to state, actor, reason, attempt
count and error category.

### Claiming work safely

Each job row carries `state`, `previous_state`, `attempt_count`, `last_error`, `error_category`,
`retryable`, `last_attempt_at`, `next_attempt_at`, `locked_at`, `locked_by`, `lock_token`,
`lock_expires_at`, `created_at` and `updated_at`.

```python
from app.services import work_queue as wq
from app.services.lifecycle import ErrorCategory, WorkflowState as S

for claim in wq.claim(db, [S.DISCOVERED], worker_id=wq.default_worker_id(), limit=10):
    try:
        ...  # deterministic work
        wq.complete(db, claim, S.NORMALIZED)
    except TimeoutError as exc:
        wq.fail(db, claim, ErrorCategory.TIMEOUT, str(exc))
```

- **Claiming.** A claim uses `SELECT … FOR UPDATE SKIP LOCKED`. Concurrent workers never receive
  the same job and never wait on each other. The claim writes a lease (`locked_by`, a random
  `lock_token`, `lock_expires_at`) and counts the attempt.
- **Ownership checks.** `complete`, `fail`, `advance` (for example QUALIFIED → TAILORING while
  keeping the lease), `heartbeat` and `release` re-lock the row and verify the token. A worker whose
  lease expired and was taken over gets `LeaseLost` and cannot write a stale result.
- **Bounded retries.** `fail` keeps the job in place and retries with exponential backoff
  (`WORKFLOW_RETRY_BASE_SECONDS`, capped by `WORKFLOW_RETRY_MAX_SECONDS`) until
  `WORKFLOW_MAX_ATTEMPTS`. Non-retryable categories (VALIDATION, CONFIGURATION, BLOCKED) stop at
  once. When a job gives up before submission it becomes FAILED with the category, message,
  attempt count and `retryable = false`. After submission it stays in its state, parked.
- **Crash recovery.** A crashed worker's job becomes claimable again when its lease
  (`WORKFLOW_LEASE_SECONDS`) expires, and the crash is recorded as `LEASE_EXPIRED`.
  `recover_expired_leases` sweeps expired leases, scheduling a retry or failing jobs that are out
  of attempts. Attempts are counted at claim time, so a job that keeps crashing workers still
  runs out of attempts.

Claiming requires PostgreSQL. SQLite remains usable for the dashboard during local development.

### Relationship to the dashboard

The dashboard still reads and writes the legacy `jobs.status` column. Migration `0002` backfilled
`state` from it (for example `shortlisted` → QUALIFIED, `approved` → READY_TO_APPLY, applied jobs
from their tracker stage). New jobs start DISCOVERED. The Job Ranker (section 8) moves `state` from
DISCOVERED to QUALIFIED or ARCHIVED and mirrors its result into `fit_score`, `fit_analysis` and, for jobs
nobody has triaged yet, `status`. Tailoring, approval and the tracker do not move `state` yet; that
happens as each stage gets a worker.

### Tests

The locking, retry and migration tests need a disposable PostgreSQL database whose name contains
`test`. They wipe it.

```bash
createdb jobhunt_test
TEST_DATABASE_URL=postgresql+psycopg://localhost/jobhunt_test pytest
```

Without `TEST_DATABASE_URL`, those tests are skipped and the rest of the suite runs on SQLite.

## 7. Scheduler

The scheduler runs inside FastAPI. It starts when the app starts and stops when it shuts down, and scheduled jobs call Python services directly. There is no n8n, Redis, Celery or separate worker process.

```text
FastAPI starts → scheduler loop (every SCHEDULER_TICK_SECONDS)
  → for each due job: take its row in scheduler_jobs (one conditional UPDATE, a row lock)
  → run the job's service function, e.g. ranking:
        claim a limited batch of waiting jobs (FOR UPDATE SKIP LOCKED leases)
        → process at most RANKING_CONCURRENCY at a time → save each result on its own
  → record status, result, error and next run time; release the lease
```

| Job | Interval setting (minutes) | What it does |
| --- | --- | --- |
| `hunter` | `HUNTER_INTERVAL_MINUTES=240` | Starts the saved search profile's Apify runs when its schedule is not `manual`, and never twice within the interval |
| `apify_sync` | `APIFY_SYNC_INTERVAL_MINUTES=5` | Imports datasets of Apify runs that finished; each run is imported once |
| `ranking` | `RANKING_INTERVAL_MINUTES=2` | The Job Ranker (section 8). A deterministic sweep hard-filters and queues new jobs, recovers crashed tasks and queues re-ranks; then it claims up to `RANKING_BATCH_SIZE` ranking tasks and runs at most `RANKING_CONCURRENCY` model calls at a time. Jobs move RANK_PENDING → RANKING → RANKED → QUALIFIED / ARCHIVED |
| `compensation` | `COMPENSATION_INTERVAL_MINUTES=5` | Records the salary stated in the original posting (structured actor fields or explicit salary text) in `jobs.compensation`. Glassdoor and PayScale are recorded as `not_configured` until those actors are integrated |
| `applications` | `APPLICATION_INTERVAL_MINUTES=5` | Reports READY_TO_APPLY / APPLYING counts. Automatic submission is not enabled, so it always reports `skipped` |
| `followups` | `FOLLOWUP_INTERVAL_MINUTES=60` | Drafts due follow-ups (never sends) |
| `inbox` | `INBOX_INTERVAL_MINUTES=5` | Reserved for an email provider; reports `skipped` until one is configured |
| `analytics` | `ANALYTICS_INTERVAL_MINUTES=60` | Stores a pipeline snapshot (jobs by state and status, pending work, failures) as its last result |
| `ai_optimization` | `AI_OPTIMIZATION_INTERVAL_MINUTES=5` | Queues scheduled analyses (per account: off, daily or weekly) and processes waiting AI Optimization tasks into recommendation reports. Never changes configuration |
| `cleanup` | `CLEANUP_INTERVAL_MINUTES=15` | Recovers jobs left leased by crashed workers: retries them or marks them FAILED. Rolls LLM telemetry older than `LLM_TELEMETRY_RETENTION_DAYS` into daily totals, then deletes it |

Set an interval to `0` to disable that job, or `SCHEDULER_ENABLED=false` to disable the loop entirely. Jobs that need an LLM report `skipped` instead of using up attempts when the provider's key is missing. Ranking and cleanup need PostgreSQL and report `skipped` on SQLite.

### Safety guarantees

- **Multiple instances:** every instance runs the loop. A job runs only on the instance that wins its `scheduler_jobs` row. The conditional UPDATE succeeds only when the job is due and no live lease exists, so two instances never run the same job at once.
- **Duplicate runs:** a job runs at most once per interval, and a second trigger while it runs gets `busy`. Within a job, row-level work is claimed with PostgreSQL locks, and finished work is detected before acting, so running any job twice is harmless.
- **Failures:** a job that raises is recorded as `failed` with its error. Other jobs and the loop keep running.
- **Restarts:** pending work lives only in PostgreSQL. Leases held by a crashed instance expire after `SCHEDULER_LEASE_MINUTES` (job level) or `WORKFLOW_LEASE_SECONDS` (row level), and the next instance picks the work up. The status endpoint shows such runs as `interrupted`.

### Status and manual triggers

```http
GET /api/system/scheduler
```

Returns whether the loop runs on this instance, plus each job's interval, lease holder, next run, last status, trigger, error, duration, result and run and failure counts.

```http
POST /api/admin/scheduler/{job}/run        X-Admin-Token: <ADMIN_TOKEN>
```

Examples: `/api/admin/scheduler/hunter/run`, `/api/admin/scheduler/ranking/run`, `/api/admin/scheduler/compensation/run`. Hyphens work too (`apify-sync`). A manual trigger runs the same job function and takes the same lease as the scheduler, but does not wait for the interval.
- The response carries the run's status (`succeeded`, `skipped` or `failed`) and result.
- It returns `409` while the job runs elsewhere.
- Triggers are disabled (`403`) until `ADMIN_TOKEN` is set.
- The status endpoint needs a signed-in account; job results and errors span all accounts, so only the admin sees them. Work for people runs per account with that account's keys: the hunter starts each account's due search, ranking claims only jobs of accounts with auto-ranking on and an AI key saved, and follow-ups are drafted per account.

`python -m app.cron <job>` runs one job once through the same path, which is useful on hosts that sleep between requests. With no argument it runs `followups`; `searches` is an alias for `hunter`.

## 8. Job Ranker

The ranker compares each job with the candidate: their **CandidateProfile**, the **active resume** saved on that
profile, and **relevant verified Evidence**. It analyses fit only; it never tailors or changes the resume.

It is asynchronous and database-backed. Apify ingestion, normalization, job search and the API requests that create
jobs never call the model: they only put work in line.

```text
APIFY RESULT → NORMALIZE → DEDUPLICATE (hunter)
→ HARD FILTER → RANK_PENDING + ranking task            (import, or the ranking sweep for jobs from other sources)
→ ranking worker claims a task (FOR UPDATE SKIP LOCKED + lease) → RANKING
→ load CandidateProfile + active resume snapshot + relevant verified evidence
→ one model call (stage: rank) → validate structured output → store a new ranking version
→ RANKED → QUALIFIED / ARCHIVED                          (decided in Python, not by the model)
```

**Hard filters** (`app.services.ranker.hard_filter_reason`) reject only clear mismatches, as SKIPPED with a reason:
missing title or company, dismissed in triage, an excluded company, an excluded title keyword, an on-site role when
the search is remote only, and a known yearly salary below the account's minimum. Unknown values never filter a job
out. An unknown salary is only rejected when the account's policy sets `reject_unknown_salary` and salary enrichment
has already looked.

**Tasks** (`job_ranking_tasks`): `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `SKIPPED`, with attempt count, last
error, backoff and lease columns. A partial unique index allows one waiting or running task per job. Claims use
`FOR UPDATE SKIP LOCKED`, so two workers never rank the same task, and every write re-checks the lease token.

**Inputs** are sent as separate sections: SYSTEM RANKING RULES (system message), CANDIDATE PROFILE, ACTIVE RESUME,
VERIFIED EVIDENCE and JOB POSTING. The resume and the job description are wrapped as untrusted data, and the stage is
routed from the prompt's marker, never from text inside the posting.

**Resume snapshots** (`resume_versions`) keep each distinct resume text a profile used, with its version and content
hash. Rankings point at their snapshot, so they stay reproducible after the resume is edited or its file deleted.

**Rankings** (`job_rankings`) are append-only versions. Each stores the candidate profile, resume snapshot, version and
content hash, hashes of the profile facts, evidence and posting, every score (`overall_fit_score`, `confidence`,
`candidate_fit_score`, `resume_fit_score`, seniority, skills, leadership, domain and location match), the
matches and gaps, `relevant_evidence_ids` (ids the model was not given are dropped and kept in
`discarded_evidence_ids`), `recommended_resume_focus`, the model's `qualification_recommendation`, the decision and its
reasons, the policy used, provider, model and prompt version.

`candidate_fit_score` says whether the person fits; `resume_fit_score` says whether the resume shows it. Tailoring
receives both, with `resume_gaps` and `recommended_resume_focus`, so it knows what to improve.

**Qualification** is deterministic:

```python
qualified = (overall_fit_score >= policy.minimum_fit_score
             and not fatal_gaps
             and confidence >= policy.minimum_confidence)
```

Defaults come from `MIN_FIT_SCORE` and `MIN_RANKING_CONFIDENCE`; an account can override them in `app_settings.preferences["ranking"]` (`minimum_fit_score`, `minimum_confidence`, `minimum_salary`,
`salary_currency`, `reject_unknown_salary`).

**Compensation** never blocks ranking. When salary enrichment later finds a yearly salary below an account's minimum,
the sweep archives the qualified job with that reason, without another model call.

**Re-ranking** creates a new version and never overwrites an old one. Qualified jobs are re-queued automatically when
the profile, the active resume or the ranking prompt version changes (`RANKING_AUTO_RERANK`), and any ranked or
ranking-archived job can be re-ranked on request. Automatic work with inputs that were already ranked reuses the stored
result (a unique index on job, profile, resume hash, prompt version and input hashes); a user request always stores a
new version.

**Failures**:

| Problem | What happens |
| --- | --- |
| No profile, no readable resume, missing AI key, free-only guard (`OPENROUTER_REQUIRE_FREE`) | The account is not claimed until it is fixed; a task already running is handed back without using an attempt |
| Timeout, rate limit, provider outage, malformed JSON, output that fails validation | Retried with exponential backoff (`RANKING_RETRY_*`) up to `RANKING_MAX_ATTEMPTS`, then the job becomes FAILED (never ARCHIVED) and can be queued again |
| Worker crash | The lease (`RANKING_LEASE_SECONDS`) expires and the sweep retries or fails the task |

**API** (ranking requests are queued and return `202`; poll the tasks for the result):

```http
POST /api/jobs/{id}/rank                  re-rank one job (a new version)
POST /api/jobs/rank-batch                 queue every unranked job
POST /api/jobs/bulk-rank?rerank=true      queue selected jobs
GET  /api/ranking/tasks?job_ids=1,2,3     latest task per job
GET  /api/jobs/{id}/rankings              every ranking version and task of a job
GET  /api/ranking/tracker                 the ranking tracker: counts, work in progress, the last day's results
```

The model is `RANK_MODEL` when set (any provider), otherwise the rank-stage model from Settings or
`OPENROUTER_MODEL_RANK`, through the provider-independent LLM layer.

## 9. Monitoring and the AI Optimization Advisor

### Telemetry

Every LLM request in the system is recorded once, in the shared LLM layer. Services contain no monitoring code:
they pass an optional `LLMCall(workflow_type, entity_id, attempt, validate)` saying which job or application a
request is for.

```text
Business service (ranker, tailor, reviewer, follow-up, evidence parsing, advisor)
  → LLMClient.generate_prompt(prompt, LLMCall)          app/services/llm.py
  → start telemetry → model request (OpenRouter / OpenAI / Anthropic / Gemini / mock)
  → capture provider usage + latency → validate structured output
  → persist LLMRequestLog (own session, best effort)    app/services/llm_telemetry.py
  → return the response
```

The Google ADK agents record their Gemini calls through the same pipeline with model callbacks
(`job_hunt_agent/telemetry.py`).

`llm_request_logs` stores, per request:
- **What:** the stage (derived from the prompt name) and the workflow entity.
- **Model:** provider, requested model, answering model and prompt version.
- **Tokens:** input, output and total; cache read and write.
- **Context:** estimated context tokens, context window, utilisation, and a breakdown by contributor (system prompt,
  candidate profile, resume, evidence, job description, conversation, research, other).
- **Performance and cost:** latency and estimated cost.
- **Outcome:** status, retry count, error type (timeout, rate limit, invalid JSON, schema validation, provider error,
  context overflow, configuration) and a redacted error message.
- **Structured output:** validity and the number of earlier structured-output failures for the same item.

Rules:
- **Provider-reported first.** Provider-reported usage is used when present (`usage_source=provider_reported`);
  otherwise tokens are estimated locally (`estimated`). Anything that can be neither stays NULL.
- **No prompt text.** Prompt and response text are not stored. Only metadata and token counts are kept, unless
  `LLM_PROMPT_DEBUG=true`. API keys and bearer tokens are redacted from error messages.
- **Monitoring never breaks a request.** If storing telemetry fails, the request still returns its result or its own
  error.
- **Retention.** Raw rows are kept for `LLM_TELEMETRY_RETENTION_DAYS` (90). The cleanup job then rolls them into
  `llm_usage_daily` totals, which are kept.

### Cost

Prices and context windows live in one file, `app/model_catalog.json` (override with `MODEL_CATALOG_FILE`).
- **Entry fields:** provider, model, input, output, cached-input and cache-write prices per million tokens, optional
  long-context rates, context window, `effective_from` and `source_url`.
- **Updating prices:** add a new entry with a later `effective_from`; don't edit history.
- **How cost is estimated:**
  - Free models (`openrouter/free`, `:free` models, mock) cost 0.
  - Models without an entry have unknown cost (NULL). It is never guessed, and the dashboard counts those requests
    as "unpriced".
  - OpenRouter requests ask for usage accounting, and its reported cost is used when present.
  - A dated snapshot answering for a catalogued model is priced as the requested model.

### Monitoring tab and API

The Monitoring tab shows:
- **Metric tiles:** requests, tokens, estimated cost, average and P95 latency, failures, retry rate and average context.
- **Charts:** cost and tokens over time, usage by stage, cost by model, latency percentiles, context-utilisation
  buckets, and failures by type. Every chart has hover details and a table view.
- **Requests:** a paginated request list with a detail view that shows metadata only.

Each account sees its own usage; the admin can switch to all accounts.

```http
GET  /api/monitoring/dashboard         everything the tab shows
GET  /api/monitoring/summary | stages | models | costs | latency | context | errors
GET  /api/monitoring/requests          paginated list
GET  /api/monitoring/requests/{id}     one request (no prompt text unless debugging stored it)
POST /api/monitoring/analyze           queue an AI Optimization analysis (202)
GET  /api/monitoring/recommendations   latest report, tasks and the account's analysis schedule
GET  /api/monitoring/reports/{id}      one report with the aggregates it was built from
PUT  /api/monitoring/schedule          off | daily | weekly
```

Filters (query parameters): `range=today|7d|30d|90d` or `start`/`end`, `provider`, `model`, `stage`,
`prompt_version`, `status`, `job_id`, and `all_accounts` (admin only).

### AI Optimization Advisor

```text
Analyze AI usage (or a daily/weekly schedule) → AIOptimizationTask PENDING
→ scheduler job ai_optimization claims it (conditional UPDATE + lease)
→ deterministic aggregates (by stage and model: tokens, cost share, context contributors, p95 context,
  latency, failure/retry/schema-failure rates, prompt versions, duplicate calls, a few metadata-only outliers)
→ deterministic rule findings (excessive context, stage cost share, paid vs free models per stage,
  schema-failure gaps, retry waste, slow tails, input-heavy prompts, repeated calls, unpriced models)
→ one validated LLM call writes prioritised recommendations with the account's own key
  (app/prompts/ai_optimization.py, AI_OPTIMIZATION_PROMPT_VERSION); with no usable key or an invalid
  answer the report is built from the rule findings
→ AIOptimizationReport (summary, findings by area, recommendations, estimated savings)
```

Each recommendation has a title, category (COST, LATENCY, CONTEXT, MODEL, PROMPT, RELIABILITY or
ARCHITECTURE), priority, finding, evidence, recommended change, expected benefit, estimated cost saving, risk and
affected stage.

**The advisor only recommends.** It can analyse, compare, recommend and estimate. It cannot change model
configuration, prompts, candidate policy, thresholds or any other configuration, and it cannot deploy anything.
Tests check this both ways: statically, that the module has no configuration writes, and at runtime, that settings,
the profile, the catalog and the prompts are unchanged after an analysis. Every recommendation needs a person to
implement it.

## 10. Recommended next upgrades

- Apify webhook endpoint so completed runs are ingested without polling
- Gmail integration for follow-up drafts and thread matching
- Resume DOCX template preservation
- Company watchlists
- Recruiter discovery/manual outreach queue
- Evaluation dataset: application → response → interview → offer conversion
- Evaluate stage-specific OpenRouter routing from application conversion data
- Optional Playwright **assistive** form filling where permitted, with a hard stop before submit

See [DEPLOY.md](DEPLOY.md) for hosting instructions.

## 11. Google Gemini agent deployment

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

### Focused settings

Settings now has separate Profile, Job preferences, Resumes, Account & security,
API keys, AI models, and Workflow & cadence sections, with independent saves and
mobile navigation. Account & security supports password changes that keep this
browser signed in and revoke other sessions. See [settings contracts, testing,
and rollback notes](docs/settings.md).

Frontend unit tests run with `npm ci && npm run test:coverage`; Node dependencies
are development-only and the production UI still runs without a build step.
