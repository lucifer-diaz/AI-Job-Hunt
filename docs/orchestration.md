# AI Orchestration — implementation map and observability gaps

How the AI Orchestration workspace (README §9a) is built from what Job Hunt OS already stores, what it cannot see
yet, and how to add a stage. The rules a change has to keep are in AGENTS.md ("AI Orchestration").

## Implementation map

| Existing source | Information it holds | Orchestration feature that uses it |
| --- | --- | --- |
| `app/services/lifecycle.py` (`WorkflowState`, `TRANSITIONS`) | The canonical states a job may hold | `Node.states`; placing FAILED/SKIPPED jobs at the stage they stopped in |
| `workflow_transitions` (via `work_queue.transition`) | Every canonical state change, actor, reason, attempt, error category | Ranker → Tailor / Archived edge counts; trace "workflow" events |
| `job_description_fetch_tasks` | JD recovery attempts: status, method, result, Playwright/LLM use, timing | JD recovery queue and throughput, live packets, BLOCKED/FAILED steps, edges into and out of recovery |
| `job_description_parse_tasks` | JD parse attempts and timing | JD parser queue and throughput, live packets, JD parser → Ranker edge |
| `job_ranking_tasks`, `job_rankings` | Ranking attempts; the stored fit analysis with its model and config version | Ranker queue, live packets, the job path's ranking decision |
| `tailor_tasks` (`stage`: queued / ranking / tailoring / reviewing) | Background tailoring runs | Tailor and Reviewer live counts and packets |
| `resume_variants` (`reviewer_score`, `reviewer_notes.decision`) | Tailored versions and their reviews | Tailor → Reviewer, Reviewer → Approval / re-tailor edges; reviewer outcome quality |
| `applications`, `application_events`, `followups` | Approval, submission, tracker stage moves, follow-ups | Approval → Application → Inbox → Interview / Outcome / Follow-up edges and the job path after QUALIFIED |
| `application_communication_events` | Inbox classification, stage decision, user corrections | Inbox correction rate; trace classification events (type only) |
| `search_runs` | Apify runs in flight | Discovery "running" count |
| `llm_request_logs` | Per-call stage, worker, configured / requested / actual model, model group, router tier and cause, fallback, escalation, schema result, latency, tokens, cost, trace id, job/application | AI node metrics, routing tab, recent activity, actual model on packets and steps, trace LLM events, model history per period |
| `external_api_request_logs` | Per-call provider, operation, status, latency, retries, job/application, trace id | External node health (Apify, job boards, WEB `jd_recovery_*`, Gmail), trace external events |
| `system_logs` | Failures, scheduler runs, milestones, by trace id and entity | Trace log events |
| `ai_config_versions` + `model_resolution.active_snapshot` / `resolve` | The applied configuration per account and its history | Current assignment (FIXED / TIER / AUTO_ROUTER, fallback, escalation, router tiers), model history periods and changes |
| `ai_config.resolve` + `user_settings.ai_overrides` | Direct-provider mode and its stage models (Models & routing) | Assignment in direct mode (`DIRECT`) |
| `ai_models.health_status` | The last health check of each model | DEGRADED when a worker's model failed its check |
| `ai_optimization_reports.recommendations[].affected_stage` | Advisor recommendations | Recommendation badges and the drawer list (advice only) |
| `monitoring.percentile`, `external_monitoring.health` | Shared aggregate helpers | Reused for every latency percentile and external health block |

No database migration was needed.

## Observability gaps (not fabricated — shown as missing)

These transitions cannot be fully reconstructed from what is stored today. Orchestration shows them as "no record"
or leaves the metric out rather than guessing.

1. **The canonical state stops at QUALIFIED.** Tailoring, review, approval and submission move the legacy
   `jobs.status` (and `applications`) but do not write `workflow_transitions` (TAILORING → TAILORED → REVIEWED →
   READY_TO_APPLY → APPLYING → APPLIED). The path after QUALIFIED is rebuilt from `tailor_tasks`, `resume_variants`
   and `applications`; per-stage timings there are only what those rows store. *Fix at the shared layer:* have
   `workflow.persist_tailored_resume`, `review_results.apply_review`, `workflow.approve_job` and
   `workflow.mark_applied` call `work_queue.transition`.
2. **`workflow_transitions` has no `trace_id`.** A transition cannot be joined to the LLM and external calls of the
   same run except through the job id and time. *Fix:* add a nullable `trace_id` column filled from
   `tracing.current_trace_id()` inside `work_queue.transition`.
3. **Review time is not stored separately.** A review is stored on the version it reviewed, so the Reviewer's own
   duration and the Tailor → Reviewer wait are not recorded (only the LLM call latency is).
4. **Domain filter and resume matching are not timed.** `domain_checked_at` is stored, resume choice has no
   timestamp; neither has a duration.
5. **Direct-provider mode records no worker, mode or router fields.** In that mode `llm_request_logs.worker_type`,
   `model_mode`, `configured_model` and routing columns are NULL; orchestration attributes calls by stage (and by
   prompt name for model history), and routing tabs show "No historical routing metadata".
6. **Router tiers before LiteLLM routing import.** Requests made before routing metadata was imported carry no
   `routing_tier`; their tier distribution is reported as unrecorded.
7. **Telemetry retention.** Raw LLM rows older than `LLM_TELEMETRY_RETENTION_DAYS` are rolled into daily totals
   without worker or config version, so model-history periods older than that show no requests.
8. **Application answers.** Answer-drafting calls are recorded under the APPLICATION stage, but whether a person
   corrected an answer is not stored, so no answer-correction rate is shown.

## Adding a stage (e.g. an ATS adapter, a browser worker, interview preparation)

1. Add the worker to `app/services/ai_workers.py` (if it calls a model) and its prompt under `app/prompts/`.
2. Add a `Node` to `orchestration_topology.NODES` with a stable id, honest `types`, `workers`, `llm_stages`,
   `external` matches, `states`, a free grid `position` and `lane`, plus `monitoring_tab` / `screen` links.
3. Add the `Edge`s it takes part in. An edge with a `measure` needs a fact in `orchestration._fact_rows` that returns
   `(job_id, at, start, end)` rows from a stored table; leave `measure=None` for a topology-only relation.
4. If it has a task table, add a `TaskTable` entry so its queue, live packets and throughput appear.
5. `orchestration_topology.validate()` must stay empty (tests check it). The frontend needs no change.

Designed-but-not-built stages are registered with `implemented=False`: they are listed in the legend as planned and
never drawn as if they ran.

## Models & routing

AI Orchestration has two workspaces: **Pipeline** (the graph above) and **Models & routing** (`#orchestration/models`,
`app/static/ai_models.js`), which moved here from Settings. It shows the route (LiteLLM gateway or direct provider), how
a request finds its model (worker → fixed model, tier or auto router → model → fallback), each worker's assignment as a
card, the model catalog, tiers and the auto router, the LiteLLM connection and the version history and audit trail.
Every edit goes to the draft; **Preview → Apply** makes it a new version. A node's **Change model** button opens its
worker's editor there. Old `#settings/ai` links redirect.

