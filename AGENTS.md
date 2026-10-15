# Working on Job Hunt OS

Conventions that are not obvious from the code, and the rules a change has to keep. Read the README for what
the system does; this file is about how to add to it.

## Third-party APIs: use the instrumentation layer

**Every production call to a third party goes through `app/services/external_api.py`. Do not create an
unmanaged HTTP client in business-service code.**

```
Job Hunt OS
  → external_client(...) / external_operation(...)      ← app/services/external_api.py
  → the third-party API
  → ExternalAPIRequestLog (+ a log line when it fails)  ← written automatically
  → Monitoring → External APIs
```

Logging is infrastructure, not something a service remembers to do. There is no `log_external_call(...)` to
call before a request, because a developer can forget one; the client *is* the telemetry.

### Adding a new provider

1. **HTTP APIs** — use the shared client. That is the whole integration:

   ```python
   from app.services.external_api import external_client

   def fetch_listings(token: str, query: str) -> list[dict]:
       with external_client("SOME_BOARD", service="listings_api", operation="search",
                            headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
           response = client.get("https://api.example.test/v1/listings", params={"q": query})
           response.raise_for_status()
           return response.json()["items"]
   ```

   Latency, HTTP status, byte sizes, retries, rate-limit headers, failure classification, the trace ID and the
   account all come with it. Name a different operation per request with
   `client.get(url, extensions={"operation": "listing_detail", "metadata": {"listing_id": 7}})`.

2. **Vendor SDKs that own their transport** (OpenAI, Anthropic, Gemini) — wrap the boundary once, in the
   provider adapter, never in each method:

   ```python
   with external_operation("SOME_SDK", service="v1", operation="generate", user_id=user_id) as call:
       result = sdk.generate(...)
       call.note(model=result.model).record_usage(cost=result.cost, source="provider_reported")
   ```

   or decorate the integration method with `@track_external_call("SOME_SDK", service="v1")`.

3. `provider` is a free string, upper-cased. Adding one needs no migration and no enum change. Use an existing
   name when the call belongs to an existing service (`APIFY`, `OPENROUTER`, `OPENAI`, `ANTHROPIC`, `GEMINI`,
   `LITELLM`, `OLLAMA`, `EMAIL`, `SEARCH`, `JOB_BOARD`, `WEB`).

4. `tests/test_external_api_guard.py` fails the build when an unmanaged `httpx.Client`, `requests` or
   `aiohttp` appears anywhere under `app/`. Only `app/services/external_api.py` is allowlisted. Tests may use
   whatever they like.

### Rules for what gets stored

* **Never store credentials.** Redaction lives in `app/services/redaction.py` and runs inside the
  instrumentation layer, so a caller cannot forget it. Authorization, cookies and API-key headers are replaced
  wholesale; secret-shaped strings are replaced anywhere they appear.
* **Never store request or response bodies by default.** Sizes are recorded instead. Bodies are kept only on request:
  an account's own capture switch (Monitoring → Logs, the same one as LLM prompts) keeps the redacted request and
  response of its external calls in `external_api_request_logs.payload` (`external_api.capture_payload`, capped at
  `EXTERNAL_API_CAPTURE_LIMIT`), cleared after `LLM_PROMPT_DEBUG_RETENTION_DAYS` by the cleanup job
  (`external_monitoring.clear_captured`); `EXTERNAL_API_DEBUG_PAYLOADS=true` does the same, briefer, for every account.
  Bodies go through `redaction.redact_body` (credential-named JSON and form fields replaced, then secret shapes), and
  are redacted again on read. Never add another path that stores a body.
* **LLM prompts and responses are captured only on request**: `LLM_PROMPT_DEBUG` (every account) or an account's own
  switch in Monitoring → LLM requests (`AppSetting.preferences["monitoring"]["capture_llm_payloads"]`, off by default).
  The text is redacted on write and on read, and cleared after `LLM_PROMPT_DEBUG_RETENTION_DAYS` (7).
* **Never guess a cost.** `record_usage(cost=...)` takes a figure the provider reported. Unknown cost stays
  NULL; NULL means unknown, not free.
* **Telemetry must not change an outcome.** Every failure inside `external_api` and `system_log` is caught and
  logged. If the monitoring database is unreachable, the API call still succeeds.

## LLM calls and model configuration

These rules keep the model a worker uses a matter of configuration, never of code:

* **All production LLM calls use the centralized LLM gateway abstraction**: `LLMClient.generate_prompt(prompt,
  LLMCall(...))` in `app/services/llm.py`. Never call a provider SDK, LiteLLM or Ollama from a service.
* **Workers must not hard-code provider model names.** No `model="gpt-oss:20b"` anywhere in a worker. A worker is
  identified by its prompt's name (`app/services/ai_workers.py`), and the LLM layer resolves its model.
* **Worker model selection comes from `WorkerModelConfig`**, read at request time through
  `model_resolution.get(user_id, "RANKER")`, which returns the model and generation settings of the account's
  *active* `AIConfigVersion` (cached for `AI_CONFIG_CACHE_SECONDS`, cleared on Apply).
* **When LiteLLM gateway mode is enabled (provider `litellm`), workers must not bypass LiteLLM.** Every request goes
  to the proxy; there is no direct-provider shortcut in that mode.
* **Model configuration changes go through the AI configuration service** (`app/services/ai_model_config.py`):
  edit the draft → preview → validate → apply → version. Do not write `WorkerModelConfig`, `ModelTier`,
  `AIRoutingConfig` or `AIModel` rows anywhere else, and never mutate an `AIConfigVersion`; roll back by applying an
  old snapshot as a new version. The Models & routing screen saves by writing the draft and applying it at once; when
  the apply is refused it offers Undo (`discard_draft`, which puts the draft back to the active version and never
  creates a version). Keep that pairing: a refused save must never leave edits silently waiting in the draft.
* **Deleting a model is a soft removal** (`ai_model_config.remove_model`, `AIModel.removed_at`): it leaves the draft,
  every picker and the next version, and `ai_registry.discover` never brings it back; Restore (or a rollback to a version
  that had it) does. It is refused while anything still uses it (`model_references`). Only a deployment Job Hunt OS owns
  leaves LiteLLM (through the normal apply; `managed` is kept, so Restore redeploys it); a discovered model stays in
  LiteLLM. Undo (`discard_draft`) removes again a model restored since the active version.
* **Prompts remain under `app/prompts/`.** Prompts say *what* a model is asked to do; worker configuration decides
  *which* model, its generation settings and routing. Never put model selection in a prompt, or a prompt in the
  configuration.
* **Adding a worker**: add a `Worker` to `app/services/ai_workers.py` with the prompt names it sends, whether it
  requires structured output, its minimum context and its default assignment. Nothing else needs to change; its
  config row is seeded, and it appears in Settings and Monitoring.
* **Routing policy is central.** Quality escalation (`escalation_reason`), request metadata and parameter filtering
  (`request_params`) live in `app/services/model_resolution.py`. A worker never decides to retry on a bigger model.
  Infrastructure fallback (the worker's `fallback_model`, sent to LiteLLM) and quality escalation stay separate.
* **Unsupported parameters are never sent blindly**: a parameter goes out only when the model is not known to lack
  it; reasoning effort needs a model known to reason. Unknown capability stays unknown (NULL), never guessed.
* **Job Hunt OS never destroys LiteLLM configuration it does not own.** `LiteLLMConfigManager` creates and removes
  only deployments marked `jobhunt_managed` with this install's owner tag, and in `config_file` mode writes only
  `LITELLM_MANAGED_CONFIG_PATH`. Do not add code that edits LiteLLM's own config file or `router_settings`.
* **LiteLLM changes are atomic where possible**: validate first, apply, verify, and undo everything on failure so the
  previous version stays active. Do not use a LiteLLM admin endpoint the installed version does not have; check its
  `/openapi.json` first.
* **No secret in configuration**: a managed deployment's credentials are `os.environ/NAME` references; the LiteLLM
  key is encrypted like the provider keys and never returned, logged or snapshotted.

## Model failover (route health)

`app/services/model_health.py` (health), `app/services/failover.py` (choice) and `app/services/failover_settings.py`
(thresholds) move a worker's requests off a model that is rate limited or failing, and back when it recovers. The
rules a change has to keep:

* **Failover is a runtime overlay, never a configuration edit.** It writes `model_route_states` and `failover_events`
  only. It never writes `WorkerModelConfig`, `ModelTier`, `AIRoutingConfig` or an `AIConfigVersion`; the models a worker
  may fail over to (`fallback_model`, `secondary_models`) are configuration and change through draft → preview → apply.
* **Health is judged from what happened, deterministically.** Request outcomes (`llm_request_logs`) and the account's
  thresholds decide; no model is called. Too few samples (`min_samples`) is never a breach: unknown stays healthy.
  Configuration errors and context overflows say nothing about a model and are not counted.
* **PostgreSQL is the only shared state.** No Redis, no cross-process in-memory counters: a trip is a database row,
  readers cache it for a couple of seconds (`model_health.states`), like `backoff.cooling_until`.
* **Fails open.** Any error in health tracking or ordering leaves the configured worker untouched and never changes a
  request's outcome (`record_outcome` and `failover.apply` catch everything).
* **A chain is the worker's backups, then its model's own fallbacks** (`AIModel.fallback_models`, `ResolvedWorker.chain`).
  Only the primary model's fallbacks join it, never a fallback's fallbacks, so two models may name each other without a
  loop. A model's `rest_seconds` is exactly how long it rests after a trip, every time; without it the account's cooldown
  doubles per trip. Both are configuration (versioned, draft → apply).
* **A resting model is a last resort, not removed**, so a request is never left with nothing to call. After its
  cooldown a route is half-open: traffic probes it, `recovery_successes` successes restore it, one failure rests it
  again for twice as long.
* **A 429 rests the model, not the gateway.** The LiteLLM cooldown is kept per model (`external_api.cooldown_provider`,
  `LITELLM:<model>`), so a rate-limited model never blocks the model failover sends the request to. Other providers
  still cool down as a whole.
* **Quality escalation and infrastructure failover stay separate.** Failover never decides on answer quality, and an
  escalated request gets no failover.
* **Notifications are metadata only** (`/api/failover/events`: model names, cause, counts) and use the existing browser
  Notification helpers in `app/static/app.js`; permission is asked only on a click, with a toast fallback.
* **Log lines after the commit.** `model_health._emit` queues the Monitoring line and `_flush_logs` writes it once the
  transaction is committed; `system_log` uses its own connection.

## Job description recovery and the shared browser

`app/services/jd_recovery.py` reads a posting's description from its URL when the board sent none (README §13).
The rules a change has to keep:

* **JD recovery runs asynchronously.** Ingestion (`sources.upsert_job`), API requests and other workers only write a
  `JobDescriptionFetchTask`; the `jd_recovery` scheduler job does the fetching. Never fetch a posting inside a
  request or an import. PostgreSQL (the task table) is the only queue: no Celery, Redis or in-memory queues.
* **One active task per job** (`uq_jd_fetch_tasks_active_job`), claimed with SKIP LOCKED plus a guarded UPDATE, with
  bounded attempts. A failure is one task's failure; it never raises out of the scheduled job.
* **HTTP before the browser.** HTTP goes through `external_client` (`app/services/external_api.py`) with redirects
  followed one by one and each hop checked by `app/services/url_safety.py`. The browser is used only when HTTP found
  no usable description. A generic HTTP `ACCESS_DENIED` may receive one normal browser rendering attempt; an explicit
  login wall, anti-bot challenge or 429 never does.
* **Playwright access goes through the shared `BrowserService`** (`app.infrastructure.browser.get_browser_service()`).
  Workers and features must not import Playwright or launch Chromium themselves. Pages get an isolated context each,
  are closed in `finally`, and never receive API keys, credentials, cookies or environment values.
* **CAPTCHA and anti-bot protections are never bypassed.** No solvers, stealth plugins, fingerprint spoofing or
  challenge retries: a blocked page is recorded as `BLOCKED` with its reason, and a person pastes the text.
* **Recovered descriptions require deterministic validation before persistence** (`jd_quality.validate_recovered`).
  Detection (`jd_quality.description_problem`) never decides by length alone.
* **Original scraped descriptions are never silently destroyed.** The board's text moves to
  `original_description`; a usable description is replaced only by a fuller version of the same posting
  (`jd_quality.better_than`); text a person saved (`description_source = USER`) is never replaced. Record provenance
  (`description_source`, `description_fetch_method`, URLs, `description_fetched_at`, task id) — never invent it.
* **LLM extraction is a fallback, not the retrieval mechanism.** It runs only on an ambiguous page, sees at most a few
  already-fetched candidate blocks (never the whole page), and returns block ids (`JobDescriptionRecoveryResult`);
  the saved text is the page's own. Its prompt lives in `app/prompts/job_description_recovery.py` (this repository's
  prompt package is `app/prompts/`), treats page text as untrusted data, and has a version constant.
* **Successful recovery feeds the existing Structured JD pipeline**: `job_description.queue_parse`, then
  `ranker.reopen_for_description` / `ranker.queue_job`. Recovery never parses or ranks by itself, and there is no
  second parser or a second workflow state model — recovery status lives on the task.
* **Site extractors are small.** Add a site to `app/services/jd_extractors.py` only for a documented public endpoint
  or the one container its postings use; the Generic extractor must keep working for everything else.

## Discovery quality: domain filter, feedback, removed postings, resume per job

* **The domain filter is deterministic** (`app/services/domain_match.py`): never a model. It runs where jobs are
  created (`app/services/discovery.admit`, called by `sources.upsert_job` and `hunter._save`) before description
  recovery, parsing or ranking are queued, and in the ranking and recovery sweeps for older jobs. A hidden job
  (`domain_match = NOT_MATCHING`) must never get a recovery, parse or ranking task; `ranker.hard_filter_reason` refuses
  it whatever path queued it. When unsure, keep the job. A person's own terms (roles, skills, include list) always win,
  jobs added by hand are never hidden, and KEPT jobs are never re-assessed.
* **Learning is counting, not inference** (`app/services/feedback_learning.py`): record feedback with
  `feedback_learning.record(db, job, action)` *before* deleting a job, then `after_feedback`, which learns inside a
  savepoint so a failure never undoes the person's action. Automatic deletions (dead postings) are never feedback.
* **Dead postings go through `job_removal.remove_posting`**: tombstone, log line, `delete_job` (which also removes task
  rows, for SQLite). Approved or applied jobs are never deleted, and only a page that says the posting is gone counts.
  Import paths check `job_removal.is_removed` before creating a job.
* **One resume selection: the active version (`Resume.is_active`).** Only `resume_verification.set_active` changes it,
  and only because the person asked (Make active). Uploads, saved resume text, structured edits, file deletes and
  re-parses never activate a version; the one exception is a profile's very first resume. `CandidateProfile.master_resume`
  and `ResumeDocument.active` are mirrors that `set_active` keeps in step — never write them anywhere else. There is no
  "default" resume.
* **Resolve the resume through `resume_verification.for_job(db, profile, job)`** (`selected(db, profile)` without a job)
  in every workflow (ranking, tailoring, review, follow-ups, MCP). It returns a view whose `chosen_resume` is the job's own
  resume, else the active one, and whose `master_resume` is that resume in the form the candidate policy's
  `resume_source` names: `structured` renders its structured data (`structured_resume.render_document`, rejected units
  left out), `master_text` is its raw text as written. Never read `profile.master_resume` of a bare profile in a
  workflow, and never parse the raw text in structured mode. Ranking identity (snapshot, hashes) uses
  `resume_verification.raw_text(view)`, so verifying facts does not re-rank every job. The view only changes what is read;
  it never activates a resume or writes the master text.
* **Setups pin resumes, and their jobs follow.** Changing a setup's resume re-applies it to the jobs it found
  (`discovery.follow_setup`) — never to jobs the person pinned (`USER`, `ACTIVE`) or approved/applied ones.
* **Resume choice is deterministic and pins win** (`app/services/resume_matching.py`): tags live on `Resume`
  (`domain`, `roles`); `discovery.choose_resume` applies the setup's pin, else `auto_assign`. Only `resume_choice`
  NULL or `AUTO` may ever be re-matched; `SETUP`, `USER` and `ACTIVE` are the person's decisions. Changing tags never
  re-ranks by itself — re-matching is an explicit action, because it can queue model calls.

## Job Insights (company, compensation and interview research)

`app/services/job_insights/` builds a source-backed research package for an approved job (README §14a). The rules a
change has to keep:

* **Never present generated interview questions as reported questions.** REPORTED questions come only from
  INTERVIEW_QUESTION observations and cite them; ROLE_DERIVED and CANDIDATE_SPECIFIC questions carry a rationale and
  requirement / evidence ids, never source attribution. Interview stages are REPORTED or INFERRED, never equivalent.
* **Never present a market estimate as an employer-approved salary band**, and **never replace the employer's salary
  with a lower-confidence estimate.** The posting's own salary (`compensation.extract_posting_salary`) is priority 1,
  a recruiter budget the person entered is priority 2; a board's *estimate* is market data. Compensation is computed in
  code (`compensation.compensation_insight`): no model output ever contains a figure, currencies are never converted
  without a configured FX source, base / total cash / total compensation are never compared, and conflicting
  estimates are listed, not averaged.
* **Every external factual insight has provenance**: an `InsightObservation` (source type, provider, actor, URL or
  run/dataset reference, observed and fetched dates, match scope and status). Model output that cites an id it was not
  shown is rejected (`synthesis.validate_company` / `validate_interview`); items are labelled FACT, DERIVED, GENERATED
  or UNKNOWN in storage and in the UI.
* **External content is untrusted.** Pages, review text, actor output and job descriptions reach a model only through
  `untrusted()` in `app/prompts/company_insights.py` / `interview_insights.py`. The model never browses.
* **A refresh never erases last known good data because one provider failed**: a failed source keeps its previous
  observations, a run without the model keeps the previous synthesized sections (`service.SYNTHESIZED`), and a run
  where everything failed changes nothing and is retried. Recruiter input is never written by research.
* **Do not invent Apify actor IDs or provider schemas.** Actors are settings (`APIFY_*_ACTOR`) and their input is an
  admin template (`APIFY_*_INPUT`); a source missing either is NOT_CONFIGURED, never run with a guessed input, their output is read through aliases in `apify.py`, and an
  unrecognized shape is FAILED ("schema not recognized").
* **Add a source as an adapter** (`base.InsightSource`: `name`, `label`, `source_type`, `company_level`,
  `configured()`, `fetch(snapshot)`) in `service.SOURCES`; it never raises, never touches the database and calls out
  only through `external_client` / `apify_client`. Matching to the job's company, role and location goes through
  `matching.assess` — deterministic, never a model; an ambiguous company is NEEDS_REVIEW and unused.
* **Never guess a company website** (no `name.com` probing); it comes from the posting's data or the person.
* **The cache is per account** (`service.cached`): observations are reused only within the same `user_id`.
* **Approval never waits for research**: `workflow.approve_job` calls `tasks.on_approved` in a savepoint; research
  runs in the `job_insights` scheduler job with the same claim / lease / run-now / kill rules as JD recovery.

## Application Inbox (Gmail)

`app/services/application_inbox/` reads Gmail, read-only, for updates on submitted applications (README: Application
Inbox Agent). The rules a change has to keep:

* **Gmail processing is asynchronous.** The scheduler's `inbox` job and `POST /api/application-inbox/sync` (a background
  task) run `sync.sync_account`; no request reads Gmail or calls a model while it waits. An account's sync lease on
  `gmail_connections` keeps runs from overlapping.
* **Gmail is read-only for this worker.** The only scope is `gmail.readonly`, and `app/services/gmail.py` issues GET
  requests only, through `external_client` (providers `GMAIL`, `GOOGLE_OAUTH`). Never add a send, modify or label call.
* **Only submitted, tracked applications are monitored**, and searches are targeted (`queries.py`): a sender, a domain,
  ATS senders *with* the company name, or a reference — bounded by `after:`. Never list or download the mailbox.
* **Only emails relevant to tracked applications may be analysed.** A body is fetched only when the headers already
  point at an application; only MATCHED (or review-band) messages reach a model; unmatched ones keep ids only.
* **Gmail content is untrusted external data**: it goes through `untrusted()` in `app/prompts/application_inbox.py`, and
  instructions in it are never followed. Reusable prompts remain under `app/prompts/` (this repository's name for the
  prompt package).
* **Application-email matching must be confidence-aware, and never guesses** (`matching.py`): deterministic signals
  first; `APPLICATION_EMAIL_AUTO_MATCH_THRESHOLD` links, the review band waits for a person (the model may only confirm
  the deterministic favourite among the candidates it was shown), ambiguity or another tracked company → NEEDS_REVIEW.
* **LLM output never directly mutates tracker state.** The model recommends `recommended_tracker_action`;
  `stage_policy.decide` validates it and only `tracker.set_stage` changes a stage. Low confidence, non-explicit wording
  or an invalid transition → a stored suggestion, never a change. No response never means rejection.
* **Duplicate emails are never reprocessed**: `(user_id, gmail_message_id)` is unique on messages and events.
* **User corrections override automated analysis**: corrections are recorded per field on the event
  (`corrections`, `user_corrected`) and `analysis.save_event` never overwrites a corrected field.
* **Gmail credentials must never appear in logs**: only the encrypted refresh token is stored; access tokens stay in
  memory; `redaction.py` knows Google token shapes (`ya29.`, `1//`, `GOCSPX-`).
* **Follow-Up must check inbox events before drafting or sending**: `create_due_followups` calls
  `application_inbox.service.blocks_followup`; a recruiter reply cancels the pending follow-up.
* **Gmail failures do not affect unrelated workers**: Gmail, OAuth and rate-limit errors end that account's sync with a
  status (`failed`, `connection_required`, `rate_limited`); a model failure leaves the message PENDING for retry and
  never loses it.

## AI Orchestration

`app/services/orchestration.py` (runtime) and `app/services/orchestration_topology.py` (the stage registry) back the
AI Orchestration workspace (README §9a). It is a read-only projection; the rules a change has to keep:

* **Orchestration owns no state.** It reads business tables, Monitoring telemetry and the active AI configuration. Do
  not add a packet, workflow-event or orchestration table while those sources can answer the question; if one truly
  cannot, add the fact at the shared layer that owns it (a transition in `work_queue.transition`, a task column), not
  a parallel log.
* **Every `/api/orchestration/*` endpoint is a GET, and the graph changes nothing.** Its Models & routing workspace
  (`app/static/ai_models.js`, `#orchestration/models`) is the one place models are managed, over `/api/settings/ai/*`;
  model changes keep going through draft → preview → apply → version.
* **Deterministic and never inferred.** Node health follows `HealthPolicy` (thresholds in one place, shown in the UI).
  The actual model comes only from `llm_request_logs.model`; unknown stays unknown. Edge timing is shown only when
  both timestamps live on the same row. No model is ever called to render or describe anything.
* **Metadata only.** No prompt or response text, resume, cover letter, email subject/sender/body, application answer
  or credential in any orchestration payload; error text goes through `redaction.redact_text`. The every-account view
  (admin) returns aggregates only — titles, packets and traces stay the viewer's own.
* **Bounded.** Every query has a limit; the graph is cached briefly; the live poll reads active task rows only;
  history and node detail load lazily.

**Any new AI workflow stage must:**

1. declare a stable worker identifier (`app/services/ai_workers.py`) and stage identifier (a `Node.id` in
   `orchestration_topology.NODES`; never rename one),
2. call models only through the centralized LLM gateway (`LLMClient.generate_prompt`), so its requests are recorded
   with their worker, stage and models,
3. move or record workflow state through the shared layer (`work_queue.transition`, its task table, the tracker) so
   orchestration can place a job there,
4. be registered with the topology: its `types` (honestly — a deterministic step is not `AI`), `workers`,
   `llm_stages`, `external` matches, `states`, and the edges it takes part in, each with the stored fact it counts
   (`measure`); `orchestration_topology.validate()` must stay empty,
5. never expose secrets or prompt bodies through orchestration APIs.

The frontend (`app/static/orchestration.js`) renders whatever the registry returns; do not hard-code stage names in
other static files.

## Running workers

`app/services/workers.py` and `app/workers_api.py` back Running workers (README §9b). The rules a change has to keep:

* **Every queued worker registers a `WorkerKind`** (task model, statuses, max attempts, scheduler job, orchestration
  node). The screen renders whatever the registry returns.
* **A queue without a job is still a `WorkerKind`.** The retrieval indexer's entries (`rag_index_entries`, kind
  `indexing`) belong to an account and a source row: the kind sets `subject` (a row's label — ids and the index version,
  never the source's text — and the embedding model), `search`, `requeue` (Retry and Run now put the same row back;
  there is never a newer task), `stopped_status` and `shown` (its finished statuses in the shared words). Its rows carry
  `job_id` None, so never key a control on `task.job_id` without checking `WorkerKind.job_bound`. Edits and sweeps
  leave a stopped entry stopped; un-verifying or deleting its source still deactivates or removes it.
* **A claim may be a batch.** The indexer leases one embedding request's entries under one token; `Claimed.task_id` is
  that token and `rag_index.renew` is its `run_live` heartbeat. Run-now entries are claimed in a batch of their own, so
  only they skip the cooldown.
* **A worker's claim honours `run_now_at`:** order run-now tasks first, clear the column on claim, carry `run_now` on the
  claim, and run that task inside `backoff.bypass(claim.run_now)`. Never bypass a cooldown any other way.
* **Every write a worker makes after its model or network call stays lease-guarded** (a lock token or a per-claim holder
  name), so a kill is always safe; a run that commits business data from its own session checks its holder inside that
  same transaction (tailoring: a `before_commit` guard), never only at a checkpoint before it.
* **Kill stops, it does not requeue.** A stopped task ends with `lifecycle.STOPPED_BY_USER` and every sweep must leave
  its job alone until a person asks again (Retry, Run now). A new automatic sweep has to honour that.
* **Kill and run-now only move the account's own task rows** (and, for kill-all, clear its cooldowns). They never
  change configuration, never delete history, and never move a job past a state the workflow would not.
* **Worker settings have two scopes.** An account's own (`app/services/worker_settings.py`: run automatically, attempts,
  retry waits, recovery's fallbacks) are read per task with `worker_settings.get/for_user`, never cached across accounts,
  and a value never set follows the server. Server-wide ones (scheduler intervals, batch sizes, concurrency) are read
  through `server_settings.value(name)` — never `get_settings()` directly for an editable name — and only an admin
  changes them. Adding a setting means adding it to `FIELDS` / `EDITABLE`; the screen renders whatever they describe.
* **A queued worker's scheduler pass goes through `scheduled_jobs.run_live`**: it claims tasks as slots free up,
  re-reads its server limits every few seconds and passes a `heartbeat` (`scheduled_jobs.renew`) that renews held
  leases and lets the pass drop a stopped task. Never claim a whole batch up front behind a fixed semaphore.
* **Long blocking work runs on `threads.run_work`, scheduler bookkeeping on `threads.run_io`** — never on the default
  `asyncio.to_thread` pool, which a few slow model calls can fill and stall every scheduled job.
* **Bulk retry is many single retries** (`workers.retry`): each task goes through `_requeue_finished`, is skipped with a
  reason rather than failing the batch, and never gets `run_now_at` — a bulk retry never bypasses a provider cooldown.
* **A task row's model is read, never decided.** `workers.attach_models` adds `model` (route, model, how it was chosen;
  `source` is `used` from the task's own `llm_request_logs` rows, or `next` from `LLMClient.route_for`) and
  `model_change` (FAILOVER, RESTING, FALLBACK, ESCALATED, CONFIG) to active rows. One bounded query, metadata only, and
  it never raises. Finished rows keep the model their own last request used (`source` `used`, or `none` when the task
  never called a model) and only that request's change (FAILOVER, FALLBACK, ESCALATED); a failover's origin is the
  request's own `llm_request_logs.failed_over_from`, never today's configuration.
* **A paused worker holds its queue, never drops it.** Its claim skips the account's tasks unless `run_now_at` is set
  (ranking: unless the task is forced).

## Agent graphs (LangGraph)

`app/agents/` is a small LangGraph runtime (README: Agent graphs; `docs/architecture/agentic-roadmap.md` Phase 1).
Only a no-side-effect pilot graph uses it so far, and everything is off unless `AGENT_GRAPHS_ENABLED`. The rules a
change has to keep:

* **A checkpoint is computation, never business state.** Job states, approvals, applications and candidate facts stay
  in their own tables and change only through their services (`work_queue.transition`, `workflow`, `apply_queue`). A
  graph node calls those services; it never writes a lifecycle column itself.
* **Graph state is small and secret-free.** Keep it to ids, enums, counts and short codes; no document, resume, page,
  email body, prompt or answer. `checkpoint.GuardedSerializer` refuses a credential-named field, anything
  secret-shaped and anything over `AGENT_CHECKPOINT_MAX_BYTES` before it is written. Do not widen that check to make a
  graph fit.
* **Thread ids are built on the server** (`threads.thread_id_for`: `graph:u<user>:<subject>:v<n>`) and never taken
  from a caller. A caller names a run by its `agent_threads.id`. **Ownership is checked before every checkpointer
  access, on start and on every resume** (`threads.owned`: the account's session, plus the owner in the thread id).
  Listing reads `agent_threads` only, never the checkpointer.
* **Every side effect goes through `effects.once(db, run, key, summary)`.** LangGraph replays a node whose checkpoint
  was not written, so a node never just "does" something. `once` checks the lease in the same transaction and records
  the effect under a key that is unique per thread. A run that lost its lease (it was stopped, or taken over after its
  lease expired) raises `LeaseLost` and writes nothing.
* **The existing scheduler dispatches.** API starts and resumes claim the thread's lease (`runtime.claim`, a guarded
  UPDATE on `lock_token`) and run as background tasks. The `agent_graphs` job continues runs whose lease expired from
  their last checkpoint. No other queue. Before a graph does production work it registers its `WorkerKind` and
  orchestration node like any queued worker. The pilot is exempt (ADR-020).
* **LLM calls go only through `app.agents.llm.GatewayChatModel`**, which is `LLMClient.generate_prompt` behind
  LangChain's interface. It is bound to one registered `app/prompts` prompt and refuses any other messages. It has no
  `model` argument and records nothing itself, so routing, failover, escalation, validation and telemetry are exactly
  the gateway's. Never put a provider chat model (ChatOpenAI, ChatAnthropic, …) in a graph.
* **LangSmith goes only through `app/agents/tracing_boundary.py`** (`tests/test_external_api_guard.py` enforces it).
  Its client sends through `external_api.requests_session` and applies `app/agents/export_policy.py`:
  * allowlist hooks for inputs, outputs and metadata;
  * error class names only;
  * a final allowlist of run fields;
  * a fail-closed wire check on the bytes sent.

  Thread ids are exported as keyed pseudonyms. **No setting widens the export**, including `LLM_PROMPT_DEBUG`, the
  capture switches and `EXTERNAL_API_DEBUG_PAYLOADS`. Passwords, OTPs, cookies, tokens, authorization headers,
  browser profiles and session storage never leave the process. A graph's `EXPORT_FIELDS` names the state keys a trace
  may show; only add plain ids, enums and counts. Each run happens inside `tracing_boundary.export(...)`, so
  environment tracing variables (`LANGSMITH_TRACING`) cannot add an unpoliced tracer. An export failure never changes
  a run.
* **Retention:** the cleanup job deletes checkpoints of finished threads after `AGENT_CHECKPOINT_RETENTION_DAYS`,
  expires threads waiting for a person after `AGENT_INTERRUPT_EXPIRY_DAYS`, and removes checkpoints whose account is
  gone. LangGraph owns its checkpoint tables (`PostgresSaver.setup()`); Alembic ignores them (`app/migrations/env.py`).
  Never hold a database transaction open while opening a saver: its setup builds indexes concurrently.

## Candidate knowledge retrieval (RAG)

`app/services/embeddings.py`, `evidence_tags.py`, `rag_index.py`, `rag_retrieval.py`, `vector_store.py` and
`rag_eval.py` back retrieval (README §9d). The rules a change has to keep:

* **Embeddings go through `EmbeddingService`, never `LLMClient`.** `LLMClient.generate_prompt` is for generation. An
  embedding call goes through `EmbeddingService.embed_documents/embed_query`, which uses the LiteLLM gateway's
  `/v1/embeddings` through the instrumented client. Never call a provider's embedding API from a service.
* **The embedding model is configuration.** It is the EMBEDDER worker's FIXED model (`ai_workers`, capability
  `EMBEDDING`, seeded off), changed through draft → apply.
  * Apply refuses an embedding model of unknown size, a chat model on the Embedder, and an embedding model on a chat
    worker.
  * An embedder has no fallbacks: vectors of another model cannot be compared.
  * Never hard-code a model or a vector size outside tests.
* **Never guess an embedding cost.** The cost is what LiteLLM reports (`x-litellm-response-cost`), recorded on the
  external call row; when it reports none, the cost stays NULL. Embedding calls do not write `llm_request_logs`.
  Text and vectors are never logged.
* **Never compare incompatible vectors.** An index version is one model × vector size × document format
  (`EmbeddingModel.identity_hash`; bump `RAG_DOC_FORMAT_VERSION` when `rag_index.evidence_text/answer_text` change).
  * A new identity builds a new version beside the SERVING one and replaces it only at full coverage.
  * Queries are embedded with the serving version's own model (`EmbeddingService.for_version`).
  * Every vector is checked against the configured size before it is stored.
* **Scope before scoring, for every job.** Resolve the job's resume with `resume_verification.for_job`, then take
  `evidence_tags.allowed_evidence_ids` for it, before any keyword or vector work.
  * A fact verified only on another resume is never a candidate, however similar it is.
  * A person's pin and a person's tags beat similarity.
  * Do not add a retrieval path that starts from the account's whole evidence bank.
  * In `on` mode, citations are checked against the same scope (`tailoring_results.verified_evidence`).
* **Resume tags: the person's choice wins.** Automation writes AUTO links only (`sync_evidence`, extraction) and never
  recreates a link a person excluded. Only `evidence_tags` writes `evidence_resume_links`.
* **The index is derived and never authoritative.**
  * Retrieval reads the live Evidence row every time, and uses a vector only while its `content_hash` equals the live
    text's.
  * The flush listener in `rag_index` keeps entries in step with edits, verification changes and deletions. It does
    metadata-only writes and never calls a model in a request; a sweep repairs anything missed. Un-verifying a fact
    deletes its vector at once: a withdrawn fact keeps no vector.
  * Index writes after the model call are lease-guarded.
* **Explicit `user_id` in raw SQL.** Vector and full-text queries are `text()` SQL, which tenancy does not filter, so
  every one names the account. The rag tables cascade on account deletion.
* **Sensitive answers.** Semantic matches are suggestions, never answers: `knowledge.search` suggestions are
  `authoritative: false`, never change its status, and show no answer text for work-authorization, visa or
  sponsorship questions. A sensitive saved answer is embedded by its question only.
* **The deterministic builder is the fallback.** With `RAG_RETRIEVAL_MODE=off` the context builders must stay
  byte-identical to the old builders (a test checks this). In `on` mode a retrieval error falls back to them.
* **`rag_vectors` is created at runtime, not by Alembic** (`vector_store.setup`). A server without pgvector must still
  start; retrieval then stays full-text. There is no ANN index until a measured account needs one.

## Traces

`app/services/tracing.py` holds an ambient correlation ID. Scheduler jobs and HTTP requests open one; nested
work narrows it (`trace("ranking", job_id=...)`) while keeping the same ID. Services never pass it around, and
a new integration inherits it. Annotate what becomes known mid-flight with `tracing.annotate(job_id=...)`.

## Operational logging

`app/services/system_log.py` writes the Monitoring → Logs table: failed or slow external calls, every LLM call
(`llm_telemetry.log_call`, INFO on success, linked to its request row; `SYSTEM_LOG_LLM_CALLS=false` keeps only
failures), scheduler runs and workflow milestones. It is for events a person would want to see, not a debug
stream — ordinary successful non-LLM calls are already one row each in the request table.

## Prompts and LLM workers

Checklist and examples: `docs/prompts.md`. The rules a change must keep:

* **All reusable production prompts live under `app/prompts/`** — including retry and correction wording.
  `tests/test_prompts.py` fails the build on an inline prompt elsewhere. Prompt modules import no provider SDK.
* **Each LLM worker has a single responsibility.** The Ranker produces the FitAnalysis; the Tailor only presents
  verified experience and never scores, reranks, reclassifies requirements or fills gaps; the Reviewer checks and
  recommends, never rewrites. Say in the prompt what the worker is *not*.
* **Never pass an empty output template belonging to an upstream worker into a downstream prompt.** Missing upstream
  data raises `ContextError` (`RANKING_REQUIRED`, `TAILOR_CONTEXT_INCOMPLETE`, `REVIEW_CONTEXT_INCOMPLETE`, ...)
  before any model call.
* **Machine-facing workers use typed output schemas** (`app/prompts/contracts.py`, `Prompt.output_schema`). Valid JSON
  is not enough: contracts forbid unknown fields, do not share field names across workers, and the gateway diagnoses
  another worker's shape as `POSSIBLE_CROSS_WORKER_OUTPUT`. Services use `result.validated`, never the raw dict.
* **External text is untrusted data**: job descriptions, emails, uploaded resumes, form questions, web text and another
  model's output under review go through `untrusted()`; instructions inside them are never followed.
* **Generated factual claims are evidence-backed**: each unit carries `evidence_ids`, and
  `app.services.tailoring_results` drops units whose evidence does not support them. A requirement without evidence
  stays missing.
* **Context builders remove irrelevant and disallowed information before prompt construction**: task projections,
  no search/scraper settings, unverified and rejected facts filtered by CandidatePolicy, provenance conflicts held
  back (`app.services.evidence_provenance`, which only diagnoses and never edits candidate history).
* **Workflow state transitions are deterministic Python.** A model may recommend (PASS / RETAILOR / SKIP, QUALIFY /
  ARCHIVE); code decides and moves the job.
* **Secrets are centrally redacted before logging** (`app/services/redaction.py`): telemetry, debug text, system logs
  and Monitoring payloads. Never add a logging path that bypasses it.
* Bump a prompt's `*_PROMPT_VERSION` when its behaviour materially changes; outputs store prompt version, provider,
  model and config version. Add a golden fixture (`tests/fixtures/prompt_golden/`) for a new worker.

## Model performance

`app/services/model_performance.py` turns raw `llm_request_logs` rows into the comparison Monitoring → Model
performance and the AI Orchestration nodes show. The rules a change has to keep:

* **Aggregates only ever come from the raw request rows.** No second telemetry table, no counters, no call to a model
  or to LiteLLM, and the browser never talks to LiteLLM. A new metric starts as a nullable column written by
  `llm_telemetry.build_entry` (from `Attempt`/`Usage`), then an aggregate in `model_performance.block`.
* **Unknown is None, never 0.** A figure a provider did not report (TTFT, generation time, reasoning tokens, cost)
  stays NULL and is left out of its own average; add the sample count next to any new rate. Never derive a number from
  something that is not the same quantity (no tokens/sec from wall-clock latency alone, no guessed price).
* **Six dimensions, no composite score**: reliability, latency, throughput, cost, quality, context. Do not add a
  single "AI quality score". Quality uses signals the product really stores; list a missing one in
  `model_performance.UNSUPPORTED` instead of showing it as zero.
* **Timing facts are best effort.** Reading gateway headers (`llm._gateway_timings`) and recording them must never
  fail or slow a request; it catches its own errors like the rest of telemetry.
* **Distinguish configured from actual**: the model shown on a node or in a table is `llm_request_logs.model` (what
  answered), with `configured_model` / `requested_model` kept beside it.
* Aggregation stays in Python over a bounded row set (`monitoring.MAX_ROWS`, `orchestration.LLM_ROW_LIMIT`), so
  PostgreSQL and SQLite agree; the column lists are `model_performance.COLUMNS` and never include prompt/response text.

## Telemetry tables and retention

| Table | Written by | Retention |
| --- | --- | --- |
| `external_api_request_logs` | `app/services/external_api.py` | `EXTERNAL_API_LOG_RETENTION_DAYS` (90), rolled into `external_api_usage_daily` |
| `llm_request_logs` | `app/services/llm_telemetry.py` (routing columns: worker, requested and actual model, tier, fallback, escalation; structured output: schema enforcement, validation failure reason, context builder version) | `LLM_TELEMETRY_RETENTION_DAYS` (90), rolled into `llm_usage_daily` |
| `system_logs` | `app/services/system_log.py` | `SYSTEM_LOG_RETENTION_DAYS` (30), deleted |
| `job_description_fetch_tasks` | `app/services/jd_recovery.py` (owned, in `OWNED_MODELS`) | kept: a job's recovery history and the JD recovery metrics |

An LLM call writes one row in each of the first two: the generic row is the provider call, the LLM row is the
token and context detail, and `llm_request_logs.external_request_id` joins them. Do not record an LLM call
twice by wrapping it again at a call site.

## The AI Optimization Advisor recommends, never acts

`app/services/ai_advisor.py` may analyse, compare and recommend. It must never disable a provider, change an
actor, a scheduler interval, a model, a worker's model assignment, a tier, the auto router, or any credential or
configuration. Keep new rules deterministic and
grounded in the aggregates; add advisor instructions to `app/prompts/ai_optimization.py`, not to a service.

## Tenancy

Every user-owned table carries `user_id` and is registered in `app/tenancy.py::OWNED_MODELS`, which filters
every ORM query by the session's account. The AI model configuration tables (`litellm_connections`, `ai_models`, `model_tiers`, `worker_model_configs`,
`ai_routing_configs`, `ai_config_versions`, `ai_config_audit`) are owned and in that list, and so are the retrieval tables
(`evidence_resume_links`, `rag_index_versions`, `rag_index_entries`; the runtime `rag_vectors` table carries `user_id`
with an ON DELETE CASCADE and is filtered explicitly). The two telemetry tables
are deliberately not in that list — a call
can belong to no account — so `app/services/monitoring.py` and `app/services/external_monitoring.py` filter by
`user_id` explicitly in every query.

## Tests

`pytest` for the backend, `npx vitest run` for `app/static/*.js`. Ranking and cleanup tests need PostgreSQL
(`TEST_DATABASE_URL`) and skip on SQLite. Run both suites before finishing.
