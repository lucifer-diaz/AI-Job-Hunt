# Deployment guide — Apify Hunter + OpenRouter

## Recommended: Render Free + Neon Free + Apify

This is the lowest-fixed-cost topology for personal use when cold starts are acceptable.

1. Create a Neon Postgres project and copy its pooled connection string.
2. Push this repository to GitHub.
3. Create a Render Web Service from the repository using Docker.
4. Create an Apify account and API token.
5. Add environment variables:

```env
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>/<db>?sslmode=require
# Encrypts the API keys people save. Keep it stable; every service (web, cron) needs the same value.
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
CRON_SECRET=<another random secret>
# Anyone who can reach the site can sign up while this is true.
ALLOW_SIGNUP=true

APIFY_API_TOKEN=<token>
APIFY_ACTOR_ID=openclawai/job-board-scraper
APIFY_DATASET_LIMIT=5000

LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<sk-or-v1-key>
OPENROUTER_MODEL=openrouter/free
OPENROUTER_REQUIRE_FREE=true
MIN_FIT_SCORE=72
FOLLOWUP_DAYS=5
```

6. Health check: `/health`. The web service applies database migrations on startup; to run them
   by hand, use `python -m app.db_migrations`.
7. Scheduling is built in: the web service starts the scheduler on boot (`SCHEDULER_ENABLED=true`) and runs Apify search and sync, ranking, salary enrichment, follow-ups, analytics and cleanup on the intervals in `.env.example`. Check it at `GET /api/system/scheduler`. Set `ADMIN_TOKEN` to enable the manual triggers, `POST /api/admin/scheduler/{job}/run` with header `X-Admin-Token`.

   Render's free web services sleep when idle, and a sleeping instance runs no jobs. On a free plan, either keep the service awake or add a Render Cron Job that runs one job at a time:

```bash
python -m app.cron ranking
python -m app.cron apify_sync
python -m app.cron followups
```

   Cron runs use the same code and the same database lease as the in-app scheduler, so the two never overlap.

### Running more than one instance

Every instance runs the scheduler. The `scheduler_jobs` table decides which instance runs each job: a conditional UPDATE takes a lease, and it only succeeds when the job is due and no other instance holds a live lease. Row-level work (ranking) is claimed with `FOR UPDATE SKIP LOCKED`. You can scale the web service horizontally, or run several uvicorn workers, without jobs running twice. PostgreSQL is required for this; SQLite is only for single-process local use.

## Railway alternative

Railway is convenient if you prefer a single platform for web + database + cron.

1. Deploy the Dockerfile from GitHub.
2. Add Railway Postgres or connect Neon.
3. Add the same environment variables above.
4. No separate cron service is needed: the web service runs the scheduler (see "Running more than one instance" above). Railway services do not sleep, so the in-app intervals apply as configured.


## OpenRouter deployment options

For the lowest LLM cost, start with:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<key>
OPENROUTER_MODEL=openrouter/free
OPENROUTER_REQUIRE_FREE=true
```

To tune quality by stage without redeploying code:

```env
OPENROUTER_MODEL_RANK=openrouter/free
OPENROUTER_MODEL_TAILOR=openrouter/free
OPENROUTER_MODEL_REVIEW=openrouter/free
OPENROUTER_MODEL_FOLLOWUP=openrouter/free
```

Later, you can replace any value with a specific current `:free` slug. If you intentionally add a paid model or paid fallback, set `OPENROUTER_REQUIRE_FREE=false`; otherwise Job Hunt OS rejects the configuration before making the model call.

Optional model failover:

```env
OPENROUTER_FALLBACK_MODELS=vendor/backup:free,openrouter/free
```

Do not hard-code old free-model names from blog posts or examples into long-lived deployment config. The free catalog changes; use OpenRouter's current model browser or keep `openrouter/free` as the default.

## How Apify is used

The web app does **not** wait synchronously for the scraper. It:

1. Starts an Actor run through the Apify API.
2. Saves the external Apify run ID in `search_runs`.
3. Polls `/api/search/apify/runs/{id}` from the dashboard.
4. When the Actor succeeds, downloads its default dataset.
5. Normalizes and deduplicates the jobs into the local `jobs` table.
6. The Ranker can then score the newly discovered jobs.

This separation is friendlier to low-cost hosting request timeouts.

## Apify cost control

- Start with 10–20 results per board/search term.
- Use `hoursOld` on recurring searches so you focus on fresh postings.
- Search fewer boards for a role/location combination when one board is clearly sufficient.
- Actor pricing is independent of the Job Hunt OS, so check the selected Actor's current Store pricing.
- You can replace `APIFY_ACTOR_ID` without changing the rest of the job pipeline.

## Security checklist

- Set `SECRET_KEY` before anyone saves API keys, keep it private, and use the same value on every service. Changing it makes saved keys unreadable.
- Sign up first: the first account becomes the admin and takes over existing data and any `.env` API keys. Then decide whether to keep sign-up open; set `ALLOW_SIGNUP=false` to close it.
- Serve the app over HTTPS so session cookies are sent with the `Secure` flag (or set `SESSION_COOKIE_SECURE=true` behind a TLS-terminating proxy).
- Each account uses its own API keys, so other people's usage is billed to their keys, not yours.
- Never commit `.env` or the Apify/LLM API tokens.
- Keep tokens only in host secrets/environment variables.
- Keep the human approval step before external application submission.
- Treat job descriptions as untrusted model input.
- Do not automate around CAPTCHAs or access controls.

## Future: fully scheduled Hunter

Apify supports schedules and webhooks. The clean production upgrade is to schedule Actor runs inside Apify and have a Job Hunt OS webhook ingest the completed dataset, removing dashboard polling entirely. The current persisted `SearchRun` model was designed so that upgrade does not require changing the ranking/tailoring pipeline.

## Vertex AI Agent Engine deployment

`job_hunt_agent/agent.py` exposes the ADK-required `root_agent`. The coordinator
delegates to four specialist agents, whose prompts are kept in
`job_hunt_agent/prompts.py` and whose validated persistence tools are in
`job_hunt_agent/tools.py`.

### Setup

1. Select or create a Google Cloud project with billing enabled, install and
   initialize the Google Cloud CLI, and authenticate Application Default
   Credentials.
2. Enable `aiplatform.googleapis.com` and create a regional Cloud Storage
   staging bucket.
3. Provision PostgreSQL reachable from Agent Engine. Apply the schema with
   `python -m app.db_migrations` (the web app also runs it on startup).
4. Configure these runtime values using your deployment's secret/environment
   controls (never bake credentials into the source bundle):

```env
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_AGENT_MODEL=gemini-2.5-flash
DATABASE_URL=postgresql+psycopg://user:password@host/database?sslmode=require
```

5. From the repository root, install and test the agent, then deploy:

```bash
pip install -e .
adk web
adk deploy agent_engine \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --region="$GOOGLE_CLOUD_LOCATION" \
  --staging_bucket="gs://YOUR_STAGING_BUCKET" \
  job_hunt_agent
```

### Smoke checks

Ask the deployed coordinator to `Rank job <existing-id>`, then `Tailor job
<same-id>`, and finally `Review resume <new-resume-id>`. Confirm each write in
the web dashboard/database. Follow-up drafting requires an application already
marked `applied`; the tool saves a draft but never sends it.

### Current limitations

- Agent Engine does not host the FastAPI dashboard or the Apify polling worker;
  deploy those separately and point both services at the same PostgreSQL database.
- The agent tools act as the account that owns the job they are given, but the
  agent itself has no sign-in: anyone who can call it can name any job ID. Deploy
  it with restricted IAM access rather than exposing it as a public multi-user bot.
- Agent output quality depends on the selected Gemini model. Persistence tools
  validate ranges, types, workflow order, and evidence IDs, but cannot prove that
  every paraphrase is semantically faithful; human review remains required.
- Deployment commands require live Google Cloud credentials, billing, APIs,
  IAM, networking, and a staging bucket, so unit tests cannot validate the final
  cloud control plane from an offline development environment.
