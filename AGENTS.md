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
   `EMAIL`, `SEARCH`, `JOB_BOARD`, `WEB`).

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
* **Never guess a cost.** `record_usage(cost=...)` takes a figure the provider reported. Unknown cost stays
  NULL; NULL means unknown, not free.
* **Telemetry must not change an outcome.** Every failure inside `external_api` and `system_log` is caught and
  logged. If the monitoring database is unreachable, the API call still succeeds.

## Traces

`app/services/tracing.py` holds an ambient correlation ID. Scheduler jobs and HTTP requests open one; nested
work narrows it (`trace("ranking", job_id=...)`) while keeping the same ID. Services never pass it around, and
a new integration inherits it. Annotate what becomes known mid-flight with `tracing.annotate(job_id=...)`.

## Operational logging

`app/services/system_log.py` writes the Monitoring → Logs table: failed or slow external calls, scheduler runs
and workflow milestones. It is for events a person would want to see, not a debug stream — ordinary successful
calls are already one row each in the request table.

## Prompts

LLM prompts live in `app/prompts/` and nowhere else (`tests/test_prompts.py` enforces it). Bump the prompt's
version when its behaviour materially changes.

## Telemetry tables and retention

| Table | Written by | Retention |
| --- | --- | --- |
| `external_api_request_logs` | `app/services/external_api.py` | `EXTERNAL_API_LOG_RETENTION_DAYS` (90), rolled into `external_api_usage_daily` |
| `llm_request_logs` | `app/services/llm_telemetry.py` | `LLM_TELEMETRY_RETENTION_DAYS` (90), rolled into `llm_usage_daily` |
| `system_logs` | `app/services/system_log.py` | `SYSTEM_LOG_RETENTION_DAYS` (30), deleted |

An LLM call writes one row in each of the first two: the generic row is the provider call, the LLM row is the
token and context detail, and `llm_request_logs.external_request_id` joins them. Do not record an LLM call
twice by wrapping it again at a call site.

## The AI Optimization Advisor recommends, never acts

`app/services/ai_advisor.py` may analyse, compare and recommend. It must never disable a provider, change an
actor, a scheduler interval, a model or any credential or configuration. Keep new rules deterministic and
grounded in the aggregates; add advisor instructions to `app/prompts/ai_optimization.py`, not to a service.

## Tenancy

Every user-owned table carries `user_id` and is registered in `app/tenancy.py::OWNED_MODELS`, which filters
every ORM query by the session's account. The two telemetry tables are deliberately not in that list — a call
can belong to no account — so `app/services/monitoring.py` and `app/services/external_monitoring.py` filter by
`user_id` explicitly in every query.

## Tests

`pytest` for the backend, `npx vitest run` for `app/static/*.js`. Ranking and cleanup tests need PostgreSQL
(`TEST_DATABASE_URL`) and skip on SQLite. Run both suites before finishing.
