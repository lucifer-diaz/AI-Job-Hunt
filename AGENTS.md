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
* **Never store request or response bodies by default.** Sizes are recorded instead.
  `EXTERNAL_API_DEBUG_PAYLOADS=true` stores short, redacted, capped extracts for debugging. It is off in
  production, and leaving it off is the default answer.
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
  old snapshot as a new version.
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

## Telemetry tables and retention

| Table | Written by | Retention |
| --- | --- | --- |
| `external_api_request_logs` | `app/services/external_api.py` | `EXTERNAL_API_LOG_RETENTION_DAYS` (90), rolled into `external_api_usage_daily` |
| `llm_request_logs` | `app/services/llm_telemetry.py` (routing columns: worker, requested and actual model, tier, fallback, escalation; structured output: schema enforcement, validation failure reason, context builder version) | `LLM_TELEMETRY_RETENTION_DAYS` (90), rolled into `llm_usage_daily` |
| `system_logs` | `app/services/system_log.py` | `SYSTEM_LOG_RETENTION_DAYS` (30), deleted |

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
`ai_routing_configs`, `ai_config_versions`, `ai_config_audit`) are owned and in that list. The two telemetry tables
are deliberately not in that list — a call
can belong to no account — so `app/services/monitoring.py` and `app/services/external_monitoring.py` filter by
`user_id` explicitly in every query.

## Tests

`pytest` for the backend, `npx vitest run` for `app/static/*.js`. Ranking and cleanup tests need PostgreSQL
(`TEST_DATABASE_URL`) and skip on SQLite. Run both suites before finishing.
