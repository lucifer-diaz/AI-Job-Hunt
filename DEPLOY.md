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
APP_USERNAME=<your username>
APP_PASSWORD=<long random password>
CRON_SECRET=<another random secret>

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

6. Back up the database and run `alembic upgrade head` as a release command.
   The supplied Docker command also performs this controlled upgrade immediately
   before starting the web process. Health check: `/health`.
7. Add a Render Cron Job for due follow-up drafts:

```bash
python -m app.cron
```

A once-daily run is sufficient for follow-ups.

## Schema migration and rollback runbook

All processes use `DATABASE_URL`, including Alembic. Schema creation is not an
application import side effect. For production, take a provider snapshot/backup,
stop writers (or put the service in maintenance mode), and run:

```bash
alembic current
alembic upgrade head
alembic current
```

The Docker, Compose, Render, and Railway commands in this repository run the
idempotent upgrade before Uvicorn starts. On a multi-replica deployment, prefer
a single platform release/pre-deploy job instead, then start all replicas without
the migration prefix so only one actor changes the schema.

Inspect history and roll back one incremental revision with:

```bash
alembic history --verbose
alembic downgrade -1
```

Or roll back to a reviewed revision explicitly, for example:

```bash
alembic downgrade 0005_application_execution_state
```

Downgrading removes data owned by the removed feature tables. Restore the backup
instead if that data must be recovered. The baseline downgrade intentionally
removes the legacy schema and should never be used against production unless the
database is being destroyed.

Before releasing, verify the migration chain against a disposable copy of the
old schema:

```bash
python scripts/verify_migrations.py
```

The verifier creates the old schema at `0001_baseline`, seeds representative
profile, job, resume, and application rows, upgrades to `head`, checks retained
records and new-table backfills, downgrades to the baseline, rechecks the legacy
data, and finally confirms that the baseline itself can downgrade on a disposable
database. For a staging copy of production, separately run `alembic upgrade head`
and application smoke tests; never point the disposable verifier at production.

## Railway alternative

Railway is convenient if you prefer a single platform for web + database + cron.

1. Deploy the Dockerfile from GitHub.
2. Add Railway Postgres or connect Neon.
3. Add the same environment variables above. The supplied Railway start command
   runs `alembic upgrade head` before Uvicorn.
4. Create a second service using `python -m app.cron` as the start command and configure a daily cron schedule.


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

- Always set `APP_USERNAME` and `APP_PASSWORD` on a public host.
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
3. Provision PostgreSQL reachable from Agent Engine and apply the schema with
   `alembic upgrade head` from a release job or trusted workstation.
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
- Agent Engine does not run database migrations itself; run the release migration
  before deploying agent code that relies on a newer schema.
- The tool layer has no end-user identity/tenant boundary; deploy this personal
  agent with restricted IAM access rather than exposing it as a public multi-user bot.
- Agent output quality depends on the selected Gemini model. Persistence tools
  validate ranges, types, workflow order, and evidence IDs, but cannot prove that
  every paraphrase is semantically faithful; human review remains required.
- Deployment commands require live Google Cloud credentials, billing, APIs,
  IAM, networking, and a staging bucket, so unit tests cannot validate the final
  cloud control plane from an offline development environment.
