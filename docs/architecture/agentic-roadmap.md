# Job Hunt OS — Agentic Architecture Roadmap

> **Status:** Proposed architecture / implementation roadmap, not a claim that these features are shipped.  
> **Repository:** triump0870/AI-Job-Hunt  
> **Last reviewed:** 2026-10-13  
> **Maintainers:** Job Hunt OS engineering  
> **Related:** [README](../../README.md) · [Engineering rules](../../AGENTS.md) · [Current orchestration implementation](../orchestration.md) · [Prompt conventions](../prompts.md)

## 1. Purpose and outcomes

Evolve the existing human-in-the-loop Job Hunt OS into an evaluated, durable and policy-governed agentic platform. It should discover opportunities, evaluate actual candidate fit, retrieve verified knowledge, prepare and review application materials, research companies, assist or submit explicitly authorized applications, verify submission, and interpret recruiter responses. It should operate continuously without treating unattended automation as permission to bypass access controls or make unverified claims.

Success means *trusted completed tasks*, not maximum LLM calls or blind application volume. Target outcomes:

- Stable behavior across restart, duplicate delivery, rate limits, and browser failures.
- Candidate facts and business state remain deterministic and verifiable.
- Lower context cost and measurable retrieval quality.
- Reusable tools, graphs and browser adapters rather than a universal unrestricted agent.
- Reviewable phase branches and explicit approvals before merges or production activation.
- Every consequential step can be traced without leaking personal or credential data.

This is a **target-state design**, not a replacement specification to implement in one PR.

## 2. Current baseline: preserve what works

The main branch already includes these capabilities, according to repository code and documentation at the review date:

| Area | Current source of truth / implementation | Roadmap treatment |
| --- | --- | --- |
| Job lifecycle | app/services/lifecycle.py, WorkflowState and TRANSITIONS | **Preserve** as canonical business state; graph state is not a replacement |
| Async work | app/services/scheduled_jobs.py and app/services/work_queue.py, PostgreSQL claims/leases | **Reuse** to dispatch graph runs and own retries |
| Discovery | Apify search setups, SearchRun, ingestion/deduplication | **Reuse** existing integrations; wrap orchestration, not scraping |
| JD recovery | app/services/jd_recovery.py and shared BrowserService | **Preserve** its isolated anonymous browser behavior |
| JD parsing/ranking | Existing parsers, ranker, qualification policy | **Reuse** inside graph nodes |
| Resume context | resume_verification.for_job, Evidence, Resume/ResumeVariant | **Preserve** resume selection and immutable version lineage |
| Knowledge answers | app/services/knowledge.py; verified reusable answers and safe unknown status | **Extend** with canonical/hybrid retrieval, do not auto-trust semantic matches |
| Tailoring/review | app/services/workflow.py, centralized prompts and output validation | **Reuse** and wrap with bounded revision graph |
| Company research | app/services/job_insights/, InsightObservation and tasks | **Reuse** sourced observations and deterministic compensation |
| Email/tracker | Existing read-only Gmail Application Inbox and tracker stage policy | **Reuse**; not an email-sending agent |
| LLM gateway | app/services/llm.py, model_resolution, ai_workers, AIConfigVersion, LiteLLM | **Retain** as one authorized model route |
| Telemetry | LLMRequestLog, ExternalAPIRequestLog, system logs | **Extend/correlate** rather than duplicate |
| Orchestration UI | app/services/orchestration.py and orchestration_topology.py | **Retain** read-only graph projection, distinguish planned and implemented nodes |
| Application submission | Approved Apply Kit, existing Claude Desktop/MCP submission-assistance workflow (`app/services/apply_queue.py`, `app/mcp/tools.py`), and manual outcome recording | **Reuse** its readiness, duplicate, blocker, locking and recording safeguards; autonomous browser/API submission is **not yet implemented** |

Read AGENTS.md before implementation. This roadmap does not supersede repository-specific invariants or source code. Re-audit immediately before each phase because the baseline will evolve.

### Known repository-specific constraints

- Every production LLM call uses the existing LLMClient route; no direct provider bypass in LangGraph/LangChain/browser-use.
- Model changes use the AI configuration draft → preview → apply → version path. Runtime failover never rewrites configuration.
- External HTTP/SDK requests use app/services/external_api.py instrumentation; costs unknown to the provider stay NULL.
- Prompts live in app/prompts/ and untrusted external text is data, not instructions.
- AI Orchestration is a read-only projection of canonical records, not a second workflow database.
- **Existing MCP application path is a first-class integration:** `apply_queue.ready_to_apply`, `not_ready_reason`, `approved_resume`, `applied_duplicate`, `open_blocker`, `lock_application`, and `record_submission`/`workflow.mark_applied` provide established controls; `app/mcp/tools.py` exposes packet retrieval, knowledge lookup, human-answer requests, blocked outcomes and submission-result recording. New automation must reuse shared services or migrate callers atomically rather than bypass these checks. Existing MCP outcome recording is not evidence that browser/API submission is implemented.
- A user-pinned resume or approved resume version must not be silently swapped.
- Source-backed Job Insights distinguish employer pay from third-party estimates, company values from sentiment, and reported interview questions from generated preparation.
- Gmail Inbox is read-only; ambiguous email/application matches wait for review.
- CAPTCHA, login walls and anti-bot controls are never bypassed by stealth, solvers, identity spoofing or proxy evasion.
- No Redis/Celery/Temporal is required initially: evaluate the existing PostgreSQL queue before adding distributed infrastructure.

## 3. Reference architecture

~~~mermaid
flowchart TD
    UI["Existing web UI / Admin"] --> API["FastAPI control plane"]
    API --> DB[("PostgreSQL business state")]
    DB --> QUEUE["Existing scheduler + durable work queue"]
    QUEUE --> GRAPHS["LangGraph worker / specialized graphs"]
    GRAPHS --> SERVICES["Existing domain services and policies"]
    GRAPHS --> RETRIEVAL["Retrieval service: hybrid RAG"]
    RETRIEVAL --> KB[("PostgreSQL + pgvector derived index")]
    GRAPHS --> LLM["Job Hunt OS LLM adapter"]
    LLM --> ROUTER["Existing model resolution / LiteLLM"]
    GRAPHS --> EXTERNAL["Apify / approved research / Gmail"]
    GRAPHS --> APP["ApplicationExecution coordinator"]
    APP --> BROWSER["Shared BrowserService / executor"]
    BROWSER --> PW["Playwright deterministic adapters"]
    BROWSER --> AGENT["browser-use guarded fallback"]
    GRAPHS -.-> LS["Optional LangSmith traces / evals"]
    SERVICES -.-> MON["Monitoring and Orchestration read models"]
    APP -.-> MON
    MON --> UI
~~~

**Separate responsibilities:**

1. **Business control plane:** FastAPI, PostgreSQL, tenancy, CandidatePolicy, immutable approvals, canonical workflow state and submission authorization.
2. **AI reasoning plane:** LangGraph conditional transitions, LangChain-compatible tools/models, RAG context selection, output contracts and evaluations.
3. **Execution plane:** Apify, sanctioned API integrations, deterministic Playwright adapters, guarded browser-use, and read-only Gmail.
4. **Observability plane:** existing Monitoring as operational source; optional LangSmith for trace inspection, datasets and experiments; existing orchestration projection for user-facing workflow state.

### Graphs are specialized, not one mega-agent

~~~mermaid
flowchart LR
    D["Discovery"] --> Q["Qualification"]
    Q -->|"Qualified"| R["Resume preparation"]
    Q -->|"Not qualified"| X["Archive / skip"]
    R --> G{"Approval / candidate policy"}
    G -->|"Approved"| A["Application execution"]
    G -->|"enqueue async after approval"| I["Job Insights (non-blocking)"]
    G -->|"Revision"| R
    A -->|"Confirmed"| P["Post-application"]
    A -->|"Human required"| H["Human handoff"]
    H -->|"Authorized resumption"| A
    P -->|"Interview"| I
~~~

- **DiscoveryGraph:** existing scheduled searches → Apify runs → ingestion/deduplication → per-job qualification work. A single search may fan out into many jobs.
- **QualificationGraph:** JD validation/recovery → structured requirements → deterministic hard filters → candidate retrieval → existing Ranker → verified qualification decision.
- **ResumePreparationGraph:** pinned resume selection → context retrieval → Tailor → evidence check → Reviewer → bounded revision/approval.
- **JobInsightsGraph:** started **after approval**, via the existing `workflow.approve_job` → `job_insights.tasks.on_approved` enqueue path → scheduled source collection → normalized observations → grounded synthesis → provenance validation → optional interview preparation. It runs **asynchronously and never blocks approval or the ApplicationGraph**. When insight data is not yet available, downstream consumers proceed with available verified context or omit the optional insights; they never fabricate research.
- **ApplicationGraph:** route detection → form inspection → answer resolution → fill/step → policy gate → guarded submission → confirmation or human handoff.
- **PostApplicationGraph:** Gmail events → deterministic matching/policy → tracker update or review → follow-up/interview preparation as new events arrive.

A graph node should wrap the existing service API where possible. It must not independently mutate a job lifecycle or create another domain source of truth.

## 4. Architectural decisions (ADRs)

| ID | Decision | Reason / consequences | Revisit if |
| --- | --- | --- | --- |
| ADR-001 | PostgreSQL domain tables remain authoritative; LangGraph checkpoints are computational state | Avoid competing definitions of APPLIED or authorization | A formal replacement with a proven migration plan exists |
| ADR-002 | Existing PostgreSQL scheduler/leases dispatch work; LangGraph runs inside workers | Prevent parallel Celery/graph/custom queues | Capacity and measurements justify new scheduling |
| ADR-003 | LangChain is an adapter, not a second LLM router | Preserve LiteLLM, model health, versioning and usage telemetry | An adapter cannot preserve needed metadata |
| ADR-004 | RAG indexes derive from verified relational records, with tenant-first filters | Avoid embedding-based factual or ownership confusion | A measured scale threshold justifies a separate vector service |
| ADR-005 | LangSmith is optional and privacy-gated | Evaluate traces without making external SaaS a dependency | Mandatory compliance requires self-hosted alternative |
| ADR-006 | Deterministic Playwright/API first; browser-use only for unknown UI | Control cost, reliability, security and reproducibility | Measured evals show a safer alternative |
| ADR-007 | Final submit requires separate Python policy, SubmissionIntent and independent verification | Browser/LLM replay cannot guarantee exactly once | Never relax based on model confidence alone |
| ADR-008 | Existing UI remains primary; browser-use WebUI is optional development tooling | Preserve auth, workflow and familiar operations | Approved frontend migration |
| ADR-009 | Prefer flags, shadow validation and reversible slices | Avoid full-workflow regression and unsafe releases | None without explicit architecture review |
| ADR-010 | Orchestration remains observational, backed by existing facts | No duplicated packet state or invented metrics | A new immutable fact genuinely lacks an owning table |
| ADR-011 | Existing MCP/apply-queue readiness, duplicate, blocker and result-recording services remain the shared application boundary | Prevent competing manual/MCP/agentic submission policies and inconsistent results | A verified atomic migration preserves all callers and invariants |
| ADR-012 | Authenticated application browsing requires a separate explicit session abstraction; anonymous `BrowserService` retains isolated contexts | Avoid leaking sessions into JD recovery or violating the current browser isolation contract | A reviewed browser security contract and comprehensive regression tests approve an evolution |
| ADR-013 | A durable **pre-submit claim** spans MCP/manual/automated clients and duplicate postings, acquired **before** any first-party external submission | Database locks used only when recording outcomes cannot prevent parallel external submits | Shared claimant/lease protocol passes race and crash testing |
| ADR-014 | Approval-time packet digest/snapshot is mandatory; edits revoke authorization or create a new reviewable version | Current `approved_resume_id` alone cannot detect in-place edits | Reviewed immutable packet versioning removes need for separate digest |
| ADR-015 | Shared approval/submission service advances `Job.state` through validated `work_queue.transition` calls as well as legacy status fields | Avoid canonical lifecycle / tracker disagreement | An explicitly approved canonical state migration supersedes this |
| ADR-016 | Embeddings use a centrally configured and instrumented embedding gateway with model/dimension/version metadata | Prompt-only LLMClient cannot safely be repurposed or bypassed | A reviewed supported local embedding adapter meets the same contract |
| ADR-017 | Domain/search-scope admission runs before any JD recovery or model work; RAG only sees the job's selected resume plus independent global evidence | Respect existing discovery and resume pinning | User explicitly changes admission or evidence policy |
| ADR-018 | Application execution worker must join Running Workers and AI Orchestration in Phase 5 | Queued work requires visible lease, retry, kill and tenant-scoped controls from first use | A reviewed worker registry replacement offers identical controls |

Record accepted changes to these decisions with an appended rationale, date, PR, and consequences. Do not silently edit previously accepted decision history.

**Proposed in Phase 1 (2026-10-13, branch `feat/langgraph-foundation`; accepted only when the Phase 1 PR is approved):**

| ID | Decision | Reason / consequences | Revisit if |
| --- | --- | --- | --- |
| ADR-019 | LangSmith export uses the LangSmith SDK's tracer through one boundary (`app/agents/tracing_boundary.py`). Its client applies allowlist hooks, a run-field allowlist, error class names only and keyed thread pseudonyms. It sends through an instrumented `requests` session (`external_api.requests_session`) that refuses any unreadable, compressed, credential-named or secret-shaped body. No setting widens it. | Reuses the SDK's protocol while keeping a wire-level, fail-closed guarantee and Monitoring telemetry. Consequence: the hooks depend on SDK internals (`_run_transform`, `update_run`, `_hide_run_error`); versions are pinned exactly and the leak tests run against them. | An SDK upgrade changes those hooks, or a self-hosted trace store is required |
| ADR-020 | The Phase 1 pilot graph is not a `WorkerKind` or an orchestration node. The first production graph (Phase 3) registers through its own task table, like every queued worker. | `WorkerKind` and Running workers are job-bound (`job_id`, job-state transitions on kill); the pilot has no job and no production effect. Consequence: pilot threads are visible only through `/api/agents/runs`. | A graph does production work, or Running workers gains non-job tasks |
| ADR-021 | Phase 2 embeddings are served only through the LiteLLM gateway (`/v1/embeddings` via the instrumented client), configured as the EMBEDDER worker's FIXED EMBEDDING model with a known vector size. Other provider modes get keyword/full-text retrieval only. | One credential, routing and telemetry path (ADR-016) with provider-reported cost; Ollama and hosted embedding models are both reachable as LiteLLM deployments. Consequence: no embeddings in direct OpenAI/Anthropic/Gemini mode. | A reviewed direct adapter meets the same contract (size check, telemetry, NULL unknown cost) |
| ADR-022 | Job-scoped retrieval is scoped by explicit evidence ↔ resume tags (`evidence_resume_links`, AUTO from verification or extraction, USER from the person, removals kept) plus the selected resume's own verified `fact_key`s. Untagged manual or extracted evidence applies to every resume; resume-sourced evidence is never global. | `Evidence.resume_id` follows whichever resume synced a shared fact last, so it cannot say which resumes own a fact. Explicit tags let a person decide and the scope rule stays deterministic (ADR-017). Consequence: existing manual evidence stays usable by every resume until tagged. | A person needs a per-job evidence override |
| ADR-023 | Vectors live in a runtime-created `rag_vectors` table (pgvector `vector` with no fixed size, rows carry `dimensions`). Exact cosine search runs over the pre-filtered scope; there is no ANN index. The embedding indexer is account-level and is not a `WorkerKind` or orchestration node. | An install without pgvector must still start, which Alembic cannot express. Scope filtering leaves tens to hundreds of rows per query, so exact search is correct and fast. `WorkerKind` is job-bound (ADR-020). Consequence: index progress shows in `/api/rag/status` and Monitoring → External APIs, not Running workers. | A measured account makes exact search too slow, or Running workers gains non-job tasks |
| ADR-023 (amended 2026-10-16) | The embedding indexer **is** now a `WorkerKind` (`indexing`) and an orchestration node (`retrieval_index`, types ASYNC_WORKER + EXTERNAL: an embedding call makes vectors, not text, so it is not an AI node). `WorkerKind` gained a non-job variant: `subject` labels a row (Evidence #12 · index v3), `search`, `requeue` (Retry and Run now put the same row back), `stopped_status`, `shown`. Entries record `started_at` / `completed_at` / `run_now_at` (migration 0046). A retryable failure is PENDING with a retry time until the account's attempts (`worker_settings` `indexing`) run out, then FAILED; Kill ends an entry FAILED with `STOPPED_BY_USER`, and neither sweeps nor edits revive it. The `rag_index` job sweeps every account, then runs batches through `scheduled_jobs.run_live` (round-robin across accounts, `rag_index_concurrency` in flight, a batch's token as its claim id, `rag_index.renew` as heartbeat); interval, batch size, per-account budget and concurrency are `server_settings.EDITABLE`. Run-now entries are claimed first in a batch of their own under `backoff.bypass`; a paused account's queue is swept and held. | The revisit trigger fired: vectors must not be turned on in production without Kill, Retry, Run now and pause on the queue that spends embedding calls. ADR-020's trigger ("Running workers gains non-job tasks") fired too; the pilot graph stays outside the registry because it still has no production effect. Consequence: index progress shows in Running workers → Indexing as well as `/api/rag/status`. | A non-job queue needs per-row controls `requeue` cannot express (e.g. a row that must be recreated rather than reset) |

**Proposed in Phase 3 (2026-10-17, branch `feat/langgraph-qualification`; accepted only when the Phase 3 PR is approved):**

| ID | Decision | Reason / consequences | Revisit if |
| --- | --- | --- | --- |
| ADR-024 | The qualification graph is a **conductor** over the existing task rows. Each node calls the service that owns its step (`domain_match` / `search_scope` admission, `jd_recovery.enqueue`, `job_description.queue_parse`, `ranker.prepare`, `rag_retrieval.scope`, `ranker.queue_job`) and then waits for that task with a LangGraph interrupt. The recovery, parsing and ranking workers keep doing the HTTP, browser and model work; the ranker keeps deciding (`ranker.qualify` → `work_queue.transition`). Its queue is `job_qualification_tasks` (migration 0047), claimed by a `qualification` scheduler job through `run_live`; a task is not claimable while its job has an active recovery, parse or ranking task, so waiting costs no polling and no attempt. Modes: `QUALIFICATION_GRAPH_MODE` off (default) / shadow (read-only comparison, budgeted, no paid call by default) / on. | Running the services' `process()` inline would duplicate leases, tie the async BrowserService to a worker thread and break the task-row facts AI Orchestration and Running workers count. As a conductor, the graph's task rows, transitions and LLM telemetry are the same rows the current path writes, and duplicate paid work is prevented by the same unique active-task indexes plus `effects.within` keys. Consequence: a qualification takes a few scheduler hops longer (nudged), and the graph itself never calls a model. | A step needs a decision the services cannot express, or hop latency matters |
| ADR-020 (amended 2026-10-17) | The first production graph is registered like every queued worker: `WorkerKind` `qualification` (Kill also stops the thread; Retry and Run now create a new task and thread) and orchestration node `qualification` (ASYNC_WORKER + DETERMINISTIC, not AI). Its threads are worker-owned (`GraphSpec.api_resumable=False`): the agents API cannot resume them and `runtime.claim_stale` never continues them. `GraphSpec.context` replaces the pilot's hard-coded run context. | ADR-020's revisit trigger fired ("a graph does production work"). A worker-owned thread continued without its task row would run without the task's lease and mode. | A graph needs a person to resume it mid-run |
| ADR-017 (clarified 2026-10-17) | In mode `on`, the backlog sweeps (`ranker.sweep`, `jd_recovery.sweep`, `job_description.sweep`) leave pre-rank jobs to the graph, whose first node is admission. The current path's parse backlog parses a never-admitted old job before `ranker.sweep` screens it, and a stub whose recovery failed; the graph does neither. | Admission before any paid work, for old jobs too. | — |

**Proposed in Phase 4 (2026-10-19, branch `feat/langgraph-preparation`; accepted only when the Phase 4 PR is approved)**

| ID | Decision | Reason / consequences | Revisit if |
| --- | --- | --- | --- |
| ADR-025 | The resume preparation graph is a **conductor** over the tailor worker: TAILOR queues `tailor_tasks.enqueue` (conducted) and waits; VALIDATE_FACTS re-runs the deterministic evidence checks and lineage; `review_results` decides; a pure rule (`next_step`) bounds the revision loop; AWAIT_APPROVAL waits for the person's action in the existing screens (`preparation.on_decision`). The Job Insights graph runs the research service's own steps on the same task row and claim, adding once-per-attempt step records and a code-only provenance check (`job_insights.provenance`). Modes `PREPARATION_GRAPH_MODE` / `JOB_INSIGHTS_GRAPH_MODE`: off (default) / shadow / on. | Same reasons as ADR-024: one implementation of tailoring, review and research, and the same task rows, telemetry and Running workers controls. The backend reviewer's decision (previously only on the MCP path) now runs in the graph. Consequence: the backend path with the graph off is unchanged (it still stores the review without deciding). | The tailor worker's single task (tailor + review) needs splitting, or a step needs a decision `review_results` cannot express |
| ADR-026 | `workflow.approve_job` moves `Job.state` to READY_TO_APPLY through transitions backed by stored facts (`approval.reconcile_to_ready`; recorded as `reconcile` with the version), and **refuses** (409, nothing written) a job with no such path. Tailoring and review themselves still do not move `Job.state`. | The roadmap requires canonical approval without forged stages. Moving state during tailoring would stop `ranker._queue_stale` and `discovery.repoint_jobs` re-ranking tailored-but-unapproved jobs (both select QUALIFIED), a behaviour change beyond this phase; reconciling at approval, from the facts, leaves them as they are. Consequence (chosen by the owner): legacy jobs in FAILED, ARCHIVED or SKIPPED states cannot be approved until re-ranked or restored; `scripts/reconcile_approved_state.py` reports approvals made before this change. | Running workers or the tracker need TAILORING/TAILORED live, or the refusals block real use |
| ADR-027 | The approved packet is an immutable `approved_packets` snapshot plus a SHA-256 digest of a documented canonical JSON (serializer v1: rendered resume, letter, attachment identity), written inside `approve_job`. An in-place edit of an approved packet revokes the approval in the same transaction (READY_TO_APPLY → REVIEWED, a new lifecycle edge); a submitted packet cannot be edited. Export, the apply queue and MCP packet issuance verify the digest; a mismatch needs a new approval. Pre-Phase-4 approvals have no snapshot and are reported LEGACY. | Phase 5's pre-submit claim needs an approval-time digest to compare against; revoking (rather than copy-on-write versions) keeps "the latest version is the one approved" true for every existing reader. | Phase 5 needs per-file (PDF/DOCX bytes) digests, or attachments beyond the resume |

## 5. Core data and durability contracts

### Authority boundaries

| Information | Authority | LangGraph carries |
| --- | --- | --- |
| User identity and tenant | Users and tenancy/session layer | user_id reference only |
| Job progression | WorkflowState + canonical transitions | job_id and expected state/version |
| Candidate truth | CandidateProfile, verified Evidence, validated resumes | source IDs, immutable snapshot hashes |
| Candidate preferences/consent | Current persisted CandidatePolicy / approved settings | policy version/reference, never permanent authorization |
| Applied status | Application + confirmation evidence | execution ID and correlation ID |
| LLM routing | Active AIConfigVersion and model resolution | worker type/config reference |
| RAG knowledge | Relational source rows | retrieved source IDs and version stamps |
| Browser authentication | Protected per-account/browser profile | opaque session reference, not cookies/passwords |
| Graph resume point | LangGraph PostgreSQL checkpointer | bounded resumable state |

**Business state invariant:** an LLM result or graph checkpoint cannot on its own transition a Job to APPLIED, change verified user facts, approve a legal attestation, or authorize submission.

**Canonical state synchronization:** the existing `workflow.approve_job` and `workflow.mark_applied` currently update legacy `Job.status` / `Application.status` without advancing `Job.state`. Phase 4 must reconcile the approval path to `READY_TO_APPLY` via validated `work_queue.transition`; Phase 5/6 must advance `READY_TO_APPLY → APPLYING → APPLIED` through the **shared** domain service with atomic status/event updates. Respect existing intermediate states: reconcile legacy rows using an explicit audited migration/reconciliation plan rather than fabricating completed stages or invalid direct transitions. On failure, neither canonical state nor legacy status may be partially committed. Tests cover MCP, UI, automation, retries and orchestration/worker projections.

**Replay invariant:** LangGraph may retry or replay code. Every side effect must have an idempotency key, safe reconciliation, or a guard that requires human review when its outcome is uncertain.

**Transactional dispatch:** if a domain transaction requires a subsequent graph run, prefer a transactional outbox plus idempotent consumer. Do not commit an application state and independently publish an in-memory event whose loss can strand it.

**Graph thread identity:** stable, tenant-scoped identifiers such as qualification:user:job:version and application:user:execution:attempt, stored alongside DB execution metadata. Never let callers supply an arbitrary cross-tenant graph thread ID.

**Checkpoint discipline:** store small serializable fields, source IDs, safe status metadata and control-flow decisions. No raw browser DOM, tokens, full resumes, Gmail bodies, confidential form answers or infinite chat history.

### Proposed application execution model (Phase 5+, not currently shipped)

ApplicationExecution (tenant-owned) references a Job, approved resume version/hash, approval record, execution mode, route, status, lease, attempt and timing. ApplicationExecutionAttempt and a small audit/event representation may be introduced only if existing rows cannot satisfy the invariant. **It adds durable browser/API attempt state; it does not replace the existing approved Application, MCP apply queue or recorded submission.**

**Shared application compatibility contract:** before enqueue/fill, call the same readiness and approved-packet checks currently used by `apply_queue`/MCP (including review score, `approved_resume_id`, apply URL, duplicate detection, active blocker and outstanding human answers). Preserve existing `apply_queue.lock_application` account-level locking when recording the result; do not confuse its **transaction-scoped post-submit lock** with pre-submit exclusion. Introduce new durable reservations behind a shared adapter consumed by MCP, Apply Kit and automated workers; keep their entry points functional while migrating external-submit flows to the common claim. On confirmed automatic submission, use a single atomic shared transaction/outcome-recording path consistent with `apply_queue.record_submission` / `workflow.mark_applied` **and** validated canonical `Job.state` transitions. MCP `record_application_result` remains a manual/external result reporting path, **not** proof required by automated ConfirmationVerifier. Never auto-mark APPLIED from an unconfirmed click.

**Approval-time content integrity (Phase 4 prerequisite):** compute and persist an immutable approved application-packet snapshot/digest **inside `workflow.approve_job`**, covering the exact recruiter-facing resume output and cover letter (plus any other explicitly approved attachments/content), normalized with a documented deterministic serializer and version. Retain `approved_resume_id`, but it is not a substitute for content integrity: today's resume edit and cover-letter endpoints can mutate the same `ResumeVariant` after approval. Post-approval edits must create a new version requiring review/reapproval **or** atomically revoke the prior approval and invalidate its digest. Re-check the approval-time digest against the actual export/artifact bytes (or canonical source and renderer version) at packet issuance, fill, and immediately before submit. Mismatches pause/require reapproval; do not recompute a new "approved" hash at execution start. Test edits before execution, between fill and submit, alternate file formats and resume-version changes.

**Durable pre-submit claim (Phase 5 prerequisite, enforced in Phase 6):** introduce a shared first-party `begin_submission`/claim API that `MCP`, Apply Kit assisted/manual starts, and browser/API workers must call **before any external submit is initiated**. Under an account-level DB lock/transaction, derive a canonical posting identity covering dedupe keys and known URL aliases, check existing applied records and active/uncertain claims, and atomically reserve the target with a unique active claim keyed by `(user_id, canonical_posting_identity)` (not merely `execution_id`/attempt). Return a claimant token/authorization bound to the user/job/approved packet; enforce it when recording or completing outcomes. Do not hold a database transaction across the browser. An expired worker lease is **not** automatic permission to submit again: after any possibly executed submit, freeze to `SUBMISSION_UNCERTAIN` pending reconciliation/human review. Controlled retries require proof of no prior submission. Legacy MCP callers must adopt the pre-submit claim protocol before Phase 6 enables guarded auto-submit, or guarded auto-submit must remain disabled for jobs/accounts that can still initiate unclaimed external submissions. A manually submitted application **entirely outside** Job Hunt OS cannot be serialized by this system; warn users and reconcile unknown outcomes rather than claiming impossible exactly-once semantics. Test two MCP/browser workers starting together, duplicate Job rows, differing URL aliases, stale claims and crash immediately after click.

Execution-level states are distinct from canonical job states:

~~~text
QUEUED → DISCOVERING → ROUTED → FORM_ANALYZING → ANSWERING
       → FILLING → PRE_SUBMIT_VALIDATION → READY_TO_SUBMIT
       → SUBMITTING → CONFIRMING → CONFIRMED

Exceptional:
NEEDS_INPUT, NEEDS_HUMAN, RETRYING, BLOCKED,
SUBMISSION_UNCERTAIN, FAILED, CANCELLED.
~~~

The domain transition to APPLIED is allowed only after sufficiently strong confirmation, via the existing lifecycle policy and a durable claim. An inconclusive click is not a successful application. Manual MCP/user reports retain their distinct provenance and must pass shared duplicate/claim checks; do not treat them as automatically verified. Follow the canonical `READY_TO_APPLY → APPLYING → APPLIED` route through the shared domain service, including the necessary reviewed compatibility reconciliation for already-approved legacy rows.

Persist SubmissionIntent *before* an irreversible action, using a unique key for the execution/attempt. A crash after SUBMITTING requires confirmation reconciliation; never blindly click Submit again.

## 6. RAG subsystem

RAG is a **context retrieval service**, not a source of user identity, state or permission.

### Collections and retrieval policy

| Collection | Sources | Consumers | Allowed trust |
| --- | --- | --- | --- |
| Candidate evidence | Verified evidence, source resume units, structured profile | Ranker, Tailor, Reviewer | Only verified or explicitly permitted source facts support claims |
| ATS answers | KnowledgeAnswer, canonical question policies and corrections | AnswerResolver | Exact policy/verified answers outrank fuzzy semantic similarity |
| Company intelligence | JobInsight and InsightObservation, company public sources | Insights, interview prep | Facts require provenance; estimates/reviews remain labeled |
| Application history | Application events, recruiter-notes-derived summaries | Prep and analytics | Historical patterns are not causal explanations for hiring outcomes |

Retrieval flow:

~~~text
typed query
→ tenant / verification / source / version filters
→ canonical SQL match where appropriate
→ PostgreSQL full-text + pgvector similarity
→ fused candidates
→ optional reranker
→ token-budgeted ContextPack
→ citation/evidence validation
→ LLM via existing gateway.
~~~

Requirements:

- Retrieval applies user/account filters **before** ranking; caches are tenant-scoped.
- **Selected-resume scope is mandatory for job-specific RAG:** resolve `resume_verification.for_job(db, profile, job)` first, derive the selected resume ID and allowed resume-derived source/evidence IDs, and restrict retrieval to those IDs **before keyword/vector scoring**, plus explicitly verified **resume-independent, profile-global** evidence permitted by current CandidatePolicy. Do not return verified but unrelated units derived from another resume belonging to the same user. User/setup-pinned resume choices must outrank embedding similarity. Test two resumes with contradictory/overlapping skills and a pinned job.
- The embedding record keeps a source ID, content hash, verification status, embedding model/version, **vector dimensions**, and source version; it does not supersede the underlying row.
- **Configured embedding gateway is a Phase 2 entry requirement:** current `LLMClient.generate_prompt` is for generation, not embeddings. Implement `EmbeddingService.embed_documents/embed_query` behind a centralized, versioned embedding-model configuration and instrumentation route (using the existing external API wrapper for provider SDK/HTTP calls). Validate model capability, vector dimensions and credential ownership; avoid hardcoded models/direct service clients. Record actual model, per-call latency/errors/usage and attributable costs where reported; unknown costs stay NULL. On embedding model/dimension changes, build a separate index namespace or versioned column/table, backfill before flipping retrieval, and never compare incompatible vectors. If no provider can meet this contract, keep Phase 2 vector RAG disabled and ship deterministic full-text retrieval only until ready.
- Invalidate or reindex on edit, verification change, source deletion, account deletion and embedding-model change.
- Canonical work-authorization/visa/eligibility questions must never be decided from vector similarity alone.
- A semantic result can be suggested for review; it does not automatically convert a sensitive answer into a verified one.
- Provide deterministic non-vector fallbacks for SQLite test environments where pgvector is unavailable.
- Validate Recall@k, Precision@k, citation support, source freshness, context savings, retrieval latency and tenant separation on labeled datasets.

### RAG release criteria

Evidence recall is measured against manually labeled required evidence, not a model's self-assessment. Compare the existing context builder with retrieval on the same work items. Merge only when the vector layer is correct, safe, and measurably useful; otherwise keep it optional.

## 7. LLM, model routing and prompts

All LangGraph nodes call existing Job Hunt OS LLM infrastructure. A LangChain-compatible adapter may be added **behind** existing model resolution so it preserves:

- Worker identity from app/services/ai_workers.py;
- Prompt definitions and output contracts in app/prompts/;
- The active model configuration version;
- LiteLLM fixed/tier/auto routing and runtime failover;
- Configured, requested and actual served model attribution;
- Schema validation, telemetry and error classification.

Do not instantiate a separate ChatOpenAI/Anthropic client directly in arbitrary graph nodes. A model adapter can use vendor-compatible protocol clients internally only if it does not bypass the existing gateway and instrumentation. Pin compatible library versions; test structured output and tool-call support on the installed models.

Suggested worker roles (assignment is configurable, not hardcoded):

| Function | Default workload class | Reliability priority |
| --- | --- | --- |
| JD parsing / inbox classification | FAST | Schema and extraction correctness |
| Ranking / tailoring / company synthesis | PRIMARY_REASONING | Grounded quality and consistency |
| Reviewer | DEEP_REVIEW, preferably independent | Detect unsupported claims |
| Evidence reranking | Lightweight specialized | Recall and relevance |
| Application answer | Balanced | Verified factual grounding |
| Browser planning | Tool-capable/structured model | Policy-compliant valid actions |

LLMs may propose content/next steps; deterministic services validate typed outputs and make irreversible decisions. Each new worker must include schema tests, prompt-injection cases, and safe fallback when the model is absent.

## 8. Browser and external execution

### Routing policy

~~~text
Approved application
    → route discovery
    → authorized submission API (only if legitimately accessible)
    → known deterministic ATS adapter
    → generic deterministic Playwright form engine
    → guarded browser-use fallback
    → human takeover or blocked.
~~~

Public ATS job-listing APIs do not imply public applicant-side application-submission rights. An API route is supported only with documented, authorized credentials and validated protocol behavior.

**Playwright:** deterministic form inspection, accessible locators, controlled page navigation, file uploads and confirmation inspection. Reuse safe low-level URL-validation, telemetry and browser-management utilities as appropriate, **without changing the existing `BrowserService` isolated-context contract**. `app.infrastructure.browser.get_browser_service()` continues to give JD recovery isolated, credential-free contexts; application authentication must never be injected into these contexts.

**browser-use:** bounded page reasoning for unfamiliar multi-step interfaces. Its output is a proposed typed action (e.g., CLICK, FILL, SELECT, UPLOAD, WAIT, REQUEST_HUMAN, STOP), not raw executable JavaScript. Enforce domain allowlists and policy validation before each action. A browser-use WebUI may be used in an isolated development environment; it is not the production frontend or authorization boundary.

**Persistent application sessions (proposed, separate security boundary):** implement an explicit `AuthenticatedApplicationBrowser` / `ApplicationSessionManager` abstraction for authenticated application workflows, **not a silent extension of the current anonymous `BrowserService`**. Define its own lifecycle, scoped per-user/per-ATS profile, secure at-rest session storage, ownership/lease locking, explicit login/human takeover and strict no-sharing with JD recovery. Share only audited browser-agnostic safeguards (URL/redirect checks, telemetry/redaction, safe action contracts). A Phase 5 ADR/security review must approve this interface and update `AGENTS.md` before authenticated-session code is introduced; tests must prove JD recovery never sees application cookies or credentials. If such a boundary cannot be implemented safely, keep sessions ephemeral and pause for human login after restart. Do not use a person's normal Chrome profile or send cookies, passwords, OTPs, session storage or authentication material to an LLM.

**Answer resolution:** deterministic profile + current CandidatePolicy → verified Answer Bank → canonical/semantic retrieval for suggestions → evidence-grounded LLM only for permitted narrative questions → NEEDS_INPUT for unsupported required facts. Generated writing is not verified truth.

**Pre-submit validation:** check owner, approved job and resume hash, active policy/consent, domain, all required fields, legal consent, work authorization, unknowns, duplicate status and expected form fingerprint. Re-fetch current authorization immediately before submission; a checkpoint is not permission.

**Confirmation:** site success message/application ID/completion state/appropriate confirmation communication must establish success. Ambiguity → SUBMISSION_UNCERTAIN and reconciliation. Do not automatically re-submit merely after a worker crash.

### Human handoff conditions

- CAPTCHA/anti-bot challenge, login, OTP, identity checks;
- unknown required factual fields or uncertain sponsorship/work authorization;
- unexpected legal attestation or consent;
- unsupported ATS form or uncertain prior submission.

Human completion triggers page re-observation and policy revalidation. It is never proof by itself that the application was submitted.

### Operation modes

| Mode | Allowed behavior | Final external submit |
| --- | --- | --- |
| OBSERVE | Detect route and inspect fields | Never |
| ASSIST | Fill prepared and authorized values; user handles final submit | User only |
| GUARDED_AUTO | Tested supported adapter, high-confidence answers, explicit account/job authorization | Only after deterministic validation |
| AUTONOMOUS | Additional controlled fallback routes, within policy limits | **Disabled until separately approved production rollout** |

## 9. Evaluation and monitoring

**Monitoring** remains Job Hunt OS's operational observability system; **LangSmith** is optional evaluation and trace tooling. The same call must not be double-counted as separate token/cost events.

Correlate only safe identifiers: trace ID, graph thread ID, job ID, execution ID, worker, prompt version, actual/requested/configured model, routing reason, configuration version and adapter version. Use pseudonymized user identifiers for external traces, with opt-in and redaction. **Credentials and authentication material (including passwords, OTPs, API/OAuth tokens, authorization headers, browser cookies, session storage and browser profiles) MUST NEVER be exported to hosted traces, under any opt-in, debug or capture setting.** Enforce redaction/suppression **before** trace serialization/export and test it. Other sensitive content (resume bodies, application answers, legal/demographic inputs, Gmail bodies, browser dumps) is excluded from hosted traces by default; exporting it requires a separately reviewed privacy policy and explicit granular consent, never a generic debug switch.

| Area | Metrics | Quality interpretation |
| --- | --- | --- |
| AI requests | Success/failure, p50/p75/p95/p99, TTFT if available, tokens, cached tokens, concurrency, throughput, retry/fallback, reported cost | Never fabricate unsupported provider fields |
| RAG | Recall@k, Precision@k, citation coverage, retrieval p95, stale index misses, token reduction | Labeled ground truth required |
| Ranker | Fit agreement, false qualification/archival, schema errors | Evaluate on expert-reviewed examples |
| Tailor/reviewer | Unsupported claims, evidence citation validity, revision rate, reviewer agreement | Independent review, not mere model self-score |
| Browser | Inspect/fill/action success, adapter-specific reliability, human intervention, confirmation rate, uncertain submission | A click is not a verified submission |
| Pipeline | Queue age, time in stage, retry attempts, terminal failures, confirmed tasks, cost/successful task | Track meaningful business outcomes |

Create offline LangSmith-compatible datasets for each stage and gated comparisons of prompt/model/retrieval changes. Distinguish observed application outcomes from demonstrated AI quality: rejection does not establish a faulty model or resume. AI Optimization may recommend changes; production model configuration still requires the established approval/apply flow.

## 10. Security, privacy, tenancy and retention

- Every business row, derived embedding, graph checkpoint, browser session, execution and artifact must belong to an account. Assert ownership at retrieval, read, resume and write boundaries.
- Treat job descriptions, web pages, actor results, emails, ATS labels and retrieved snippets as **untrusted**. Never follow instructions embedded in them.
- Use URL safety checks across redirects and browser actions to prevent SSRF/private-network access.
- Avoid sensitive content in checkpoints, logs, trace metadata, prompts to third-party tools, screenshots and test fixtures.
- Require explicit user authorization for auto-submit with revocation effective before the next action.
- Retain only necessary debug artifacts; implement expiration, per-account deletion, audit and profile cleanup.
- Use least-privilege access for Gmail (read-only), Apify, model keys and browser profile storage.
- **Never export credentials or authentication material to hosted traces under any capture or opt-in mode.** Redact at the export boundary before instrumentation, including framework callbacks and browser-use agent traces.
- Never bypass CAPTCHAs, login walls, anti-bot systems, paywalls or access restrictions.
- Keep third-party cost and tracing failures nonfatal for domain workflows unless a necessary business operation failed.
- Changes to legal/sensitive answers require direct user confirmation; demographic information is never inferred.

## 11. Eight-phase implementation plan

Every phase starts from updated main, uses an isolated branch, produces tests/documentation, and stops for independent review. **A merge is distinct from feature activation.**

| Phase | Branch | Deliverables | Merge gate |
| --- | --- | --- | --- |
| 1. LangGraph foundation | feat/langgraph-foundation | Graph runtime, PostgreSQL checkpointer, compatible LLM adapter, optional LangSmith pilot and flags | Checkpoint survives restart; tenant isolation; existing model routing unaffected |
| 2. RAG knowledge | feat/rag-knowledge-layer | Configured/instrumented embedding gateway, dimension/version-safe pgvector indexing, job-resume-scoped hybrid retrieval, context packs and evals | Source-backed recall evaluation, model/dimension compatibility, pinned resume isolation and no cross-tenant/stale retrieval |
| 3. Discovery/qualification graphs | feat/langgraph-qualification | Existing Apify/domain admission/JD/ranker wrapped in graph nodes, safe branches, shadow mode | Hidden jobs never reach JD/LLM; qualification parity, no duplicate paid calls |
| 4. Preparation/insights graphs | feat/langgraph-preparation | Resume selection/review, **approval-time packet digest**, canonical approval transition, asynchronous Job Insights | Post-approval edits revoke approval or trigger reapproval; domain state consistent; insights nonblocking |
| 5. Application execution | feat/application-execution | Shared durable **pre-submit claim** for MCP/UI/browser paths, WorkerKind + AI Orchestration node, attempts, typed form and separate authenticated-browser security review | Atomic pre-submit claim, correct Running Workers visibility, safe fill/restart and **no final automated submission** |
| 6. Guarded browser submission | feat/agentic-browser-apply | Generic/browser-use actions, validated consent, shared claim/SubmissionIntent, canonical transitions, confirmation/reconciliation and human takeover | Competing MCP/browser starts never both submit; canonical + legacy states agree, no blind retry |
| 7. Evaluation/feedback | feat/agent-evaluation-feedback | LangSmith datasets, Gmail event correlation, question memory, graph UI and monitoring improvements | End-to-end trace fidelity, deterministic metric sourcing, privacy checks |
| 8. Production hardening | feat/agentic-production-hardening | Fault injection, load, worker supervision, security, retention, rollout controls and runbooks | Full regression, failure recovery, security review, signed GO/NO-GO |

Phase instructions, acceptance tests, and the reusable independent-review/merge checklist are version-controlled in [Claude Code implementation prompts](implementation-prompts.md). Use the prompts alongside **this roadmap and the current AGENTS.md**, and check for repository drift before each phase. All prospective modules/APIs/tables remain **proposed** until an associated PR is merged. Never mark a phase complete based solely on having written a prompt or plan.

### Phase details and deliverables

#### Phase 1: Foundation and evaluation
- Audit current scheduler, LLM routing, Graph-related dependencies, migrations, telemetry and configuration.
- Add durable checkpointer and a pilot graph which branches, interrupts and resumes.
- Add opt-in LangSmith trace integration; preserve existing telemetry.
- **Exit demonstration:** terminate a pilot worker during execution, restart it, resume correctly, and show no data leak.
- **Rollback:** turn off graph flag; old production services remain unchanged.

#### Phase 2: Candidate RAG and ATS answer retrieval
- First implement a centrally configured/instrumented `EmbeddingService` with explicit model, vector dimensions, credentials, usage, deployment/retention and index-version migration policy.
- Add PostgreSQL vector extension/migrations, derived index lifecycle and hybrid retrieval; never mix incompatible embedding dimensions.
- Reuse Evidence/KnowledgeAnswer and current context builders; enforce `resume_verification.for_job` selection and disallow other-resume-derived facts in job-scoped retrieval.
- Establish labeled retrieval benchmarks and a non-vector fallback for unsupported development databases.
- **Exit demonstration:** one JD receives a correctly attributed, bounded verified evidence pack; stale/other-tenant evidence is excluded.
- **Rollback:** disable retrieval flag and use existing deterministic context builders.

#### Phase 3: Qualification migration
- Implement graph nodes around existing discovery/JD recovery/parser/filter/ranker. Enforce deterministic domain/search-scope admission **before** checking description or queueing JD recovery/parsing; hidden `NOT_MATCHING` jobs terminate with no network/model side effects.
- Use shadow mode with deterministic fixtures before any production traffic cutover.
- **Exit demonstration:** qualified/archive decisions and state history agree with the current path on representative cases.
- **Rollback:** route work to the current scheduler/service implementation.

#### Phase 4: Resume preparation and job insights
- Implement bounded tailor/reviewer revision and human approval interrupts; persist **approval-time** resume/cover-letter packet digest or immutable approved snapshot in `workflow.approve_job`. Post-approval edits either create a new reviewed version or revoke approval, never silently change the approved artifact.
- Align `workflow.approve_job` with canonical `READY_TO_APPLY` via validated `work_queue.transition` calls and audit-compatible legacy-state reconciliation; do not forge completed intermediate stages.
- Trigger Job Insights **only after approval**, through the existing asynchronous on-approved queue; do not make approval depend on insight completion.
- Extend source-grounded company research using existing Job Insights/Apify adapters; do not invent provider schemas.
- **Exit demonstration:** immutable pinned resume; all factual claims source-checked; employer and market pay remain separate.
- **Rollback:** keep existing workflow services and Job Insights endpoints active.

#### Phase 5: Application execution
- First audit and **reuse or explicitly adapt** existing `apply_queue`/MCP readiness, approved packet, resume score, account-level locking, duplicate-posting protection, human-answer blockers and result recording; preserve backwards compatibility.
- Build and migrate a durable **pre-external-submit claim** across MCP, in-app assisted/manual, and automated entry points: account/posting identity, active/uncertain uniqueness and explicit user claim tokens. Today's `lock_application` acts only when recording a result; it is **not** a pre-submit lock. Do not enable automated submit while any first-party client bypasses this.
- Add durable execution-level state, normalized form and answer resolution, without a competing readiness/status authority; validate the **approval-time** packet digest before fill.
- Register an `ApplicationExecution` `WorkerKind` in `app/services/workers.py`, scheduler job with lease-aware controls, and implemented node/edges in `orchestration_topology.NODES`. Preserve Running Workers kill/retry/run-now and tenant ownership from the start; Phase 7 may enrich metrics but must not be the first stage that makes executions visible.
- Propose and separately approve an authenticated application-session abstraction; update `AGENTS.md` and test that anonymous JD-recovery sessions remain credential-free.
- Test a single known ATS adapter in OBSERVE/ASSIST.
- **Exit demonstration:** inspect/fill to READY_TO_SUBMIT and resume after process crash; no submit action is reachable.
- **Rollback:** revert to approved Apply Kit/manual submission.

#### Phase 6: Guarded submission and agentic fallback
- Implement pre-submit validator, authorized SubmissionIntent, confirmation verifier and reconciliation. Require the **Phase 5 shared pre-submit claim before any external click**, including MCP and automated paths, and never clear uncertainty merely because a worker lease expired.
- Route confirmed results through one atomic domain service that writes `Application`, legacy `Job.status`, and canonical `Job.state` `READY_TO_APPLY → APPLYING → APPLIED` via `work_queue.transition`, with audit events and duplicate guard; no silent skipped transitions.
- Reuse the existing `apply_queue`/MCP locking, duplicate check, blocker/human-answer semantics and common application-result recording. Test a concurrent MCP/manual result and browser execution; no second source of APPLIED truth.
- Guard browser-use action proposals; never give it direct submission authority.
- **Exit demonstration:** controlled ATS sandbox handles success, uncertainty, duplicate, legal/OTP and browser-crash cases correctly.
- **Rollback:** disable guarded submission, preserve uncertain executions for manual reconciliation.

#### Phase 7: Feedback and evaluation
- Build cross-stage correlation from Apify → job → AI → form → confirmation → Gmail.
- Store user-approved ATS corrections without promoting generated text into facts.
- Publish LangSmith-compatible evaluation suites and meaningful monitoring metrics.
- **Exit demonstration:** inspect one application journey with accurate trace sources and no sensitive payload exposure.
- **Rollback:** disable optional trace/eval integration while business logs remain intact.

#### Phase 8: 24/7 production hardening
- Separate API, graph and browser worker processes; bound concurrency and external costs.
- Test backups, restore, lease recovery, locked profiles, graceful shutdown, timeout/cooldown and retention.
- **Exit demonstration:** crash/fault-injection suite plus a controlled, explicitly authorized pilot.
- **Rollback:** revert execution mode to ASSIST or OBSERVE and preserve canonical domain data.

## 12. Merge and release governance

**Branch sequence:** current main → phase branch → implementation → tests → independent review → PR → explicit merge approval → updated main → next phase. Do not stack unreviewed phase branches or merge one phase while its dependencies are unmerged.

Before every PR:

1. Read latest AGENTS.md, README.md, this roadmap and prior accepted ADRs.
2. Confirm the implementation matches the agreed phase boundary; update diagrams/ADRs if not.
3. Run ruff, targeted tests and full pytest; report actual commands, environment and unrun checks.
4. Check Alembic upgrade and rollback/forward-compatibility strategy in a disposable database.
5. Exercise security, multitenancy, retries and restart behavior for the changed boundary.
6. Verify explicit flags and authorization: default production behavior must not silently change.
7. Create a PR with diff summary, decision references, test results, migration notes, risks and rollback.
8. Stop for independent review and user approval; never merge automatically.

### Merge decision template

~~~text
PHASE:
BRANCH:
BASE SHA:
HEAD SHA:
PR:
SCOPE IMPLEMENTED:
ADR CHANGES:
DATABASE/MIGRATION IMPACT:
TEST COMMANDS AND RESULTS:
SECURITY / TENANT ISOLATION:
FAILURE / RESTART TEST RESULTS:
OBSERVABILITY:
PERFORMANCE / COST:
FLAGS AND DEFAULTS:
KNOWN LIMITATIONS:
ROLLBACK PLAN:
BLOCKING ISSUES:
MERGE DECISION: PASS | BLOCKED
REVIEWER:
APPROVER:
MERGE SHA:
PRODUCTION FLAG STATUS:
~~~

**Gate logic:** PASS requires no unresolved critical/high-severity defects, actual relevant test results, validated tenant/secret boundaries, migration compatibility, and explicit feature-off rollback where applicable. PASS permits requesting merge approval; it does **not** authorize merge or production activation.

### Separate production-release gate for autonomous submission

To move from ASSIST to GUARDED_AUTO or AUTONOMOUS requires separate, explicit authorization, staged enablement, controlled ATS evidence, monitoring of uncertain submissions and human intervention, rollback switches, and legal/site-terms review where relevant. A merged PR alone is insufficient.

## 13. Risks, dependencies and decision register

| Risk | Mitigation | Decision owner |
| --- | --- | --- |
| LangGraph checkpoint and DB state diverge | Domain state authority, outbox, idempotency, reconciliation | Platform/backend |
| LangChain changes routing behavior | Compatibility adapter + actual-model tests | AI platform |
| RAG leaks stale/unverified/cross-account evidence | Tenant-first filters, source version/hash, offline recall/security evals | RAG/backend |
| Model quality regresses despite lower cost | Gated benchmark and held-out examples | Evaluation owner |
| Browser-use tools execute unauthorized actions | Typed actions, policy gate, domain checks, bounded tools | Browser/security |
| Crash leads to double submission | SubmissionIntent, SUBMISSION_UNCERTAIN and confirmation reconciliation | Application platform |
| Browser profile leaks credentials | Separate encrypted/restricted profile, locking, retention and deletion | Security/ops |
| LangSmith sends sensitive data | Opt-in, redaction, pseudonymized identifiers, ability to disable | Security/privacy |
| Multi-framework complexity grows | Reuse current queue, browser service, UI and model config | Tech lead |
| Provider/API availability changes | Capability registry, graceful unsupported state, human handoff | Integrations |

**Open design questions before relevant phases:** supported centrally configured embedding model and dimensions; embedding index backfill/migration policy; vector extension availability in all environments; selected resume/global-evidence provenance classification; legacy `Job.state` reconciliation; approval-time packet hash serializer/version and edit invalidation; shared pre-submit claim identity/alias canonicalization and claim takeover; how to migrate existing MCP clients to claim-before-submit without blocking all user work; acceptable LangSmith deployment/privacy posture; ATS priority by observed Job Hunt OS volume; local vs remote human browser handoff; confirmation-strength thresholds; retention rules for artifacts; production autonomy eligibility and applicable website terms.

Do not silently resolve any of these by assuming a framework provides a capability or that a site's public API permits submission.

## 14. Progress ledger

This section is intended to change after *each approved phase merge*. Initial entries are placeholders, not claims of completion.

| Phase | Status | PR | Merge SHA | Evaluation / test evidence | Production flag |
| --- | --- | --- | --- | --- | --- |
| 1. LangGraph foundation | MERGED | [#72](https://github.com/triump0870/AI-Job-Hunt/pull/72) | `74930e5` | 2026-10-13 note below | OFF (`AGENT_GRAPHS_ENABLED=false`) |
| 2. RAG knowledge layer | MERGED | [#74](https://github.com/triump0870/AI-Job-Hunt/pull/74) | `a2f0b30` | 2026-10-15 note below | OFF (`RAG_RETRIEVAL_MODE=off`, `RAG_VECTOR_ENABLED=false`) |
| 3. Qualification graphs | MERGED | [#78](https://github.com/triump0870/AI-Job-Hunt/pull/78) | `4cf1e66` | 2026-10-17 note below | OFF (`QUALIFICATION_GRAPH_MODE=off`) |
| 4. Preparation / insights graphs | IN_REVIEW | (this branch) | — | 2026-10-19 note below | OFF (`PREPARATION_GRAPH_MODE=off`, `JOB_INSIGHTS_GRAPH_MODE=off`); approval digest and state sync always on |
| 5. Application execution | NOT_STARTED | — | — | — | OBSERVE only (planned) |
| 6. Guarded agentic submission | NOT_STARTED | — | — | — | OFF |
| 7. Evaluation and feedback | NOT_STARTED | — | — | — | OFF |
| 8. Production hardening | NOT_STARTED | — | — | — | OFF |

Allowed status: NOT_STARTED, PLANNING, IN_PROGRESS, IN_REVIEW, MERGED, BLOCKED, ROLLED_BACK. Update only with evidence. Prefer a new dated progress note below to explain scope, deferrals and decisions rather than overwriting history.

### 2026-10-13 — Phase 1: LangGraph foundation
- **Branch / PR:** `feat/langgraph-foundation`. [#72](https://github.com/triump0870/AI-Job-Hunt/pull/72) to `main`, not merged.
- **Base / merge SHA:** base `26e4562`; no merge.
- **What shipped:**
  - **`app/agents/` runtime:**
    - `threads`: server-built, tenant-scoped thread ids, with ownership checked on start, resume and every checkpointer access.
    - `checkpoint`: `PostgresSaver`, plus `GuardedSerializer`, which refuses credential fields, secret-shaped text and oversize state.
    - `effects.once`: lease-checked, once-per-thread side effects.
    - `runtime`: lease, execute, interrupt and resume, stop, crash recovery and retention.
  - **Pilot graph:** a deterministic graph (`pilot`) that branches (auto or review), interrupts and survives restart.
  - **`GatewayChatModel`:** a LangChain chat-model interface over `LLMClient.generate_prompt` only.
  - **LangSmith boundary:** an opt-in `tracing_boundary` and `export_policy` (ADR-019).
  - **API and scheduler:** flagged `/api/agents/*` endpoints, the `agent_graphs` recovery job, and checkpoint pruning in `cleanup`.
- **New migrations, flags, APIs:**
  - **Migration:** `0044_agent_threads` (`agent_threads`, `agent_effects`; additive, downgradable). LangGraph creates its own `checkpoint*` tables through `setup()`, and Alembic ignores them.
  - **Flags:** `AGENT_GRAPHS_ENABLED`, `AGENT_*` limits and retention, `AGENT_LANGSMITH_*`. All are off or safe by default.
  - **Dependencies:** `langgraph==1.2.12`, `langgraph-checkpoint==4.2.0`, `langgraph-checkpoint-postgres==3.1.2`, `langchain-core==1.6.5`, `langsmith==0.14.1`.
- **Architectural decisions and deviations:**
  - **ADR-019:** LangSmith goes through the SDK tracer with allowlist hooks and a fail-closed wire check.
  - **ADR-020:** the pilot is not a `WorkerKind` or an orchestration node.
  - **No parallel queue or router.**
  - **Production unchanged:** discovery, ranking, tailoring and application paths are untouched.
- **Test/evaluation evidence** (local, PostgreSQL 16, `jobhunt` conda env):
  - **New tests:** `tests/test_agents_runtime.py`, `test_agents_tracing.py`, `test_agents_llm.py` and `test_agents_postgres.py`. Together they cover:
    - a SIGKILLed worker process resuming from its PostgreSQL checkpoint;
    - a crash after the effect, with no duplicate effect;
    - an interrupt that survives a restart and resumes with credential decryption disabled;
    - tenant A versus B on execute, list, resume and stop;
    - LangSmith off, on and down (connection refused, timeout, 503);
    - synthetic tokens absent from the exported bytes with every debug switch on;
    - routing and telemetry parity, including failover.
  - **Full suite:** `pytest` passes on SQLite (1918 passed, 95 skipped). With `TEST_DATABASE_URL` it gives 2004 passed and 7 failed. The same 7 fail on untouched `main`, so they are pre-existing.
  - **Not run in this environment:** `tests/test_mcp*.py`. The env has `mcp` 2.x, but `requirements.txt` pins `<2`.
  - **Lint:** `ruff check app tests` is level with `main` at 227 findings (ruff 0.16 default rules). The new files are clean.
  - **Disposable-DB migration check:** upgrade to 0044, downgrade to 0043, upgrade to head; the metadata diff is empty.
- **Known gaps:**
  - The adapter is JSON-only (no tool calls).
  - LangSmith export rows may lack a trace id (SDK background thread).
  - The pilot is not in Running workers or Orchestration.
  - The SDK hooks rely on pinned internals.
- **Reviewer / approval:** pending independent review.
- **Production activation and rollback:** OFF. Rollback means leaving `AGENT_GRAPHS_ENABLED=false` and `AGENT_LANGSMITH_EXPORT_ENABLED=false`. Optionally, downgrade 0044 and drop the LangGraph `checkpoint*` tables.
- **Next dependency:** Phase 2 needs this PR approved and merged.

### 2026-10-15 — Phase 2: RAG knowledge layer
- **Branch / PR:** `feat/rag-knowledge-layer`, [#74](https://github.com/triump0870/AI-Job-Hunt/pull/74) to `main`, not merged.
- **Base:** `37bebe2` (Phase 1 #72 and #73 merged).
- **What shipped:**
  - **`EmbeddingService`** (`app/services/embeddings.py`):
    - Configured as the EMBEDDER worker's FIXED EMBEDDING model (seeded off).
    - Served through LiteLLM `/v1/embeddings` via the instrumented client.
    - Records the model that answered, input tokens and latency on the external call row, plus the cost LiteLLM
      reported (NULL when none).
    - Checks every vector's size.
  - **Configuration checks:**
    - Apply refuses an unknown vector size, a chat model on the Embedder, an embedding model on a chat worker, and
      embedder fallbacks.
    - Discovery reads `output_vector_size`; model and worker tests measure it.
  - **Evidence ↔ resume tags** (`evidence_resume_links`, ADR-022), in the backend and the Evidence bank UI:
    - AUTO from verification and extraction; USER from the person. A person's removal of a resume fact's tag is kept
      (`excluded`) so verification never re-adds it; for manual or extracted evidence, removing a tag deletes the link.
    - Per-row editor, bulk tagging, a resume filter, and new evidence defaulting to the active resume.
  - **Derived index:**
    - `rag_index_versions` and `rag_index_entries` (migration 0045); `rag_vectors` is created at runtime when pgvector
      exists (ADR-023).
    - A flush listener handles edits, verification changes and deletions; a sweep repairs anything missed.
      Un-verifying a fact deletes its vector at once.
    - Claims and writes are lease-guarded; the backfill is versioned and switches over at full coverage.
    - Retired versions are purged by `cleanup`.
    - The `rag_index` scheduler job runs only when `RAG_VECTOR_ENABLED`.
  - **Hybrid retrieval** (`rag_retrieval.py`):
    - Scope comes first: the account, `resume_verification.for_job`, then `evidence_tags.allowed_evidence_ids`.
    - Signals: BM25, PostgreSQL full-text, canonical required-skill matches, and pgvector cosine (stale-hash guarded).
    - The lists are fused by RRF into a token-budgeted `ContextPack` cited by real Evidence ids, with
      `verify_citations`.
  - **Integration:**
    - `context_builder._base` and `ranker.relevant_evidence` (MCP) follow `RAG_RETRIEVAL_MODE` (off / shadow / on).
      Shadow writes id-only comparisons to Monitoring → Logs.
    - `knowledge.search` gains non-authoritative `suggestions`, with answer text withheld for
      work-authorization/visa/sponsorship questions.
    - With `on`, `tailoring_results.verified_evidence` checks citations against the job's resume scope, so a tailored
      resume or letter citing another resume's fact is refused.
  - **Evaluation:** `rag_eval.py`, `scripts/rag_eval.py`, and a labelled benchmark (`tests/fixtures/rag_eval/`, 8 jobs,
    14 facts, 2 resumes with conflicting Acme experience, all pin types).
- **Measurements** (benchmark; the concept embedder is a synthetic stand-in, not a model):

  | Variant | Recall@5 | Recall@10 | P@3 | P@5 | R-precision | Citation correctness | Cross-resume leaks | p95 ms | Token reduction |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | Keyword (BM25 + full-text) | 1.0 | 1.0 | 0.58 | 0.41 | 0.90 | 1.0 | 0 | 6 | 54% |
  | Hybrid (concept stand-in) | 1.0 | 1.0 | 0.58 | 0.40 | 0.90 | 1.0 | 0 | 8 | 47% |
  | Deterministic builder (today) | — | 1.0 over all sent | — | — | — | — | **26** across 8 jobs | — | — |

  Real-model recall was **not measured**: no reachable embedding model with credentials in this environment. Run
  `scripts/rag_eval.py --litellm-url … --model … --dimensions …` before turning vectors on.
- **Review fixes (2026-10-16, independent review):**
  - Re-verifying a resume no longer deletes the AUTO tag extraction gave evidence extracted from it. Before, that
    evidence became untagged and reached every resume's jobs.
  - Clearing every tag on manual or extracted evidence now makes it apply to every resume, as the Evidence bank shows.
    Before, the resume it came from stayed excluded.
  - Resume names (by default the uploaded file name) are escaped in the new-evidence form.
  - Un-verifying a fact deletes its vector immediately, also from a process that never set the vector store up; the
    sweep removes any left behind.
  - With `on`, tailoring citations are checked against the job's resume scope.
- **Code review fixes (2026-10-16):**
  - Embedding requests are sent with `external_api.NO_CAPTURE`. Before, an account's payload capture or
    `EXTERNAL_API_DEBUG_PAYLOADS` kept candidate evidence text and vectors in external-call telemetry.
  - Apply refuses an embedding model in any chat failover slot (`fallback_model`, `secondary_models`, a text model's
    own `fallback_models`), not only as the primary.
  - A version is promoted only after a re-sweep: a fact verified or added while it was building now keeps it from
    serving until that fact is embedded.
  - With `on`, the verified-facts gate of tailoring and cover letters counts only evidence of the job's own resume.
  - The indexer in Running workers was a separate follow-up, done the same day (ADR-023 amendment: Running workers →
    Indexing).
- **Tests** (the reviewer's runs on head with the review fixes: PostgreSQL 16.8 + pgvector 0.8.7, `jobhunt` conda env,
  `mcp` 1.30 on `PYTHONPATH` because the env has 2.x):
  - **New suites:** `test_embeddings.py`, `test_rag_index.py`, `test_rag_retrieval.py`, `test_rag_eval.py` and
    `test_rag_postgres.py`, plus Evidence-bank tag tests in vitest. On PostgreSQL with `test_migrations.py`: 75 passed.
  - **SQLite:** 2057 passed, 102 skipped, 1 failed (`test_mcp` stdio server, which fails on `main` too).
  - **PostgreSQL:** 2150 passed, 2 skipped, 8 failed: the same 8 tests fail on untouched `main`.
  - **vitest:** 320 passed, 2 failed (`overview_outcomes`, which fails on `main` too).
  - **Migrations:** `test_migrations.py` on PostgreSQL covers upgrade, downgrade to baseline and back, and an empty
    metadata diff. A manual upgrade → `rag_vectors` setup → downgrade to 0044 → upgrade cycle left no rag table behind.
  - **Lint:** ruff is level with `main` (229); the changed files are clean.
- **Known gaps:**
  - Pack-level relevance cut-off is by budget and candidates only.
  - Company intelligence and application-history collections are deferred to Phase 4.
  - Existing untagged manual evidence applies to every resume until tagged.
  - With `off` or `shadow`, tailoring still checks citations against the account's verified evidence, as on `main`.
  - ~~The indexer is not a `WorkerKind` (ADR-023): no kill, run-now or retry in Running workers~~ — resolved
    2026-10-16 (ADR-023 amendment: Running workers → Indexing). Embedding calls still do not appear in Model
    performance (they are not LLM requests).
  - With `shadow` and vectors on, every context build embeds one query (cached per account); follow the rollout order.
- **Rollback:** `RAG_RETRIEVAL_MODE=off`, `RAG_VECTOR_ENABLED=false`. Optionally downgrade 0045, which also drops
  `rag_vectors`.
- **Compose upgrade:** the image changes from `postgres:17-alpine` to `pgvector/pgvector:pg17` (same major version,
  glibc instead of musl). On an existing volume, if PostgreSQL warns about a collation version mismatch, run
  `REINDEX DATABASE jobhunt;` and `ALTER DATABASE jobhunt REFRESH COLLATION VERSION;` once.
- **Next dependency:** Phase 3 needs this PR approved and merged.

### 2026-10-17 — Phase 3: Qualification graph
- **Branch / PR:** `feat/langgraph-qualification`, [#78](https://github.com/triump0870/AI-Job-Hunt/pull/78) to `main`, not merged.
- **Base:** `a2f0b30`. Phase 1 (#72, merge `74930e5`) and Phase 2 (#74, merge `a2f0b30`; #77) are merged.
- **What shipped:**
  - **Qualification graph** (`app/agents/qualification.py`), with this node order:
    `VALIDATE_JOB → DOMAIN_AND_SEARCH_SCOPE_ADMISSION → CHECK_JD → RECOVER_JD_IF_NEEDED → PARSE_JD →
    QUALIFICATION_HARD_FILTER → RETRIEVE_VERIFIED_JOB_RESUME_EVIDENCE → RANK → VALIDATE → QUALIFY/ARCHIVE`.
    - It is a conductor over the existing services and task rows (ADR-024).
    - Waits are interrupts; graph-side writes go through `effects.within` (lease check, effect key and business
      write in one transaction).
    - SHADOW runs take the same nodes read-only.
  - **Queue** `job_qualification_tasks` (migration 0047, owned, in `OWNED_MODELS`) and `app/services/qualification.py`:
    dispatch, claim (SKIP LOCKED and guarded UPDATE; a job with active recovery, parse or ranking work is skipped),
    run and settle, recover expired leases, shadow sweep and parity, hand-back.
  - **Scheduler job** `qualification` (through `run_live`), nudged by the recovery, parse and ranking passes.
  - **Running workers → Qualification:** Kill stops the thread; Retry and Run now create a new task and thread.
  - **Orchestration node** `qualification` (ASYNC_WORKER + DETERMINISTIC); `validate()` stays empty.
  - **Runtime:** `GraphSpec.context` and `api_resumable`; worker-owned threads are left out of `claim_stale`.
  - **Cutover hooks**, active only in mode `on`: `discovery.admit`, `hunter._save`, the `ranker.sweep` pre-rank
    branch, the `jd_recovery` and `job_description` backlogs, `jd_recovery.apply_recovered`, `domain_match.keep`,
    `job_removal.delete_job`.
  - **Parity report:** `scripts/qualification_parity.py`.
- **New flags:**
  - `QUALIFICATION_GRAPH_MODE=off` (default).
  - `QUALIFICATION_MINUTES`, `_BATCH_SIZE`, `_CONCURRENCY`, `_MAX_ATTEMPTS`, `_RETRY_SECONDS`, `_LEASE_SECONDS`.
  - `QUALIFICATION_SHADOW_DAILY_LIMIT=50` and `QUALIFICATION_SHADOW_PAID_DAILY_LIMIT=0`.
  - Interval, batch, concurrency and both shadow budgets are `server_settings.EDITABLE`. Attempts and retry wait are
    per-account `worker_settings`.
- **Old vs new** (`scripts/qualification_parity.py`, PostgreSQL 16, mock parser, scripted ranking answers; history
  compared as (from, to, reason), with the current path's description wait left out):

  | Case | Current path | Graph | Graph run | Same state | Same history | Model calls old/new | Parses old/new |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | qualified | QUALIFIED | QUALIFIED | QUALIFIED | yes | yes | 1/1 | 1/1 |
  | archived: low score | ARCHIVED | ARCHIVED | ARCHIVED | yes | yes | 1/1 | 1/1 |
  | archived: fatal gap | ARCHIVED | ARCHIVED | ARCHIVED | yes | yes | 1/1 | 1/1 |
  | qualified: non-fatal gaps | QUALIFIED | QUALIFIED | QUALIFIED | yes | yes | 1/1 | 1/1 |
  | archived: low confidence | ARCHIVED | ARCHIVED | ARCHIVED | yes | yes | 1/1 | 1/1 |
  | hidden: outside the domain | SKIPPED | SKIPPED | hidden at import | yes | yes | 0/0 | 0/0 |
  | recovered description | QUALIFIED | QUALIFIED | QUALIFIED | yes | yes | 1/1 | 1/1 |
  | blocked description | SKIPPED | SKIPPED | BLOCKED | yes | yes | 0/0 | **1/0** |
  | removed posting | deleted | deleted | (deleted with the job) | yes | yes | 0/0 | 0/0 |
  | failed model | FAILED | FAILED | FAILED | yes | yes | 3/3 | 1/1 |
  | invalid JSON | FAILED | FAILED | FAILED | yes | yes | 3/3 | 1/1 |

  11 of 11 cases agree. The one difference is spend: on a blocked stub, the current path's parse backlog parses text
  the ranker then refuses to rank; the graph does not (ADR-017 clarification).
- **Two current-path behaviours found while testing:**
  - Its parse backlog (`job_description.sweep`) parses a never-admitted old job before `ranker.sweep` screens it.
  - `ranker.sweep` does not apply the search scope to old jobs.

  In mode `on` the graph admits first in both cases. Mode `off` is unchanged.
- **Tests:**
  - **New suite:** `tests/test_qualification_graph.py` (29 tests, PostgreSQL):
    - golden parity: qualified, archived and gap cases, and a hard filter;
    - hidden jobs, new, old and scope-hidden, with zero recovery, parse, browser or model calls;
    - recovered, blocked and removed postings;
    - pinned resume;
    - concurrent claims;
    - a killed worker (exit after a committed effect, and SIGKILL while waiting), continued with one task and one
      call per step;
    - failed model and invalid JSON;
    - Kill and Retry;
    - duplicate delivery and dispatch;
    - tenant isolation, and the API refusing a worker-owned resume;
    - equal orchestration edges, node task counts and live counts for both paths;
    - shadow: no writes, no paid call by default, daily and paid budgets;
    - mode off hand-back;
    - the parity report.
  - **Full runs** (jobhunt conda env, `mcp` 1.30 on `PYTHONPATH`), each next to an untouched `main` run:
    - SQLite: 2082 passed, 128 skipped, 1 failed (`test_mcp` stdio, fails on `main` too). `main`: 2078 passed,
      2 failed.
    - PostgreSQL (final run on head): 2202 passed, 2 skipped, 8 failed: the same 8 tests fail on untouched `main`.
    - One intermittent failure is not this branch's: an earlier full PostgreSQL run had a ninth failure,
      `test_rag_index::test_the_scheduler_job_is_off_unless_vector_retrieval_is_on`. It is a PostgreSQL deadlock
      inside `rag_index.renew` (Phase 2 code, untouched here). It failed 1 of 13 file-level reruns on the branch and
      0 of 15 on `main`; isolated, it passed 16 of 16 on both.
  - **vitest:** 326 passed, 2 failed (`overview_outcomes`, fails on `main` too).
  - **Migrations:** `test_migrations.py` on PostgreSQL (upgrade, downgrade, empty metadata diff) passes.
  - **Lint:** ruff level with `main`.
- **Known gaps:**
  - Re-ranks (`resume_changed`, `_queue_stale`, `_recheck_salary`, `follow_setup`) stay on the existing services.
  - The graph adds a scheduler hop per step; nudges make it immediate when the scheduler runs in-process.
  - The orchestration edges `qualification → domain_filter` and `qualification → ranker` are unmeasured support
    edges: every count stays with the existing nodes, which are the facts.
  - The RETRIEVE step reports the job's resume and its evidence scope (ids and counts). The ranker builds its own
    context from the same scope; the graph does not hand the ranker a pack.
  - No real provider was called: parity uses the mock parser and scripted ranking answers.
- **Rollback:** `QUALIFICATION_GRAPH_MODE=off`. The next pass ends waiting ON runs `ROUTED_TO_LEGACY`, and the current
  path takes their jobs on. Optionally, downgrade 0047.
- **Next dependency:** Phase 4 needs this PR reviewed and merged. Stop here.

### 2026-10-19 — Phase 4: Preparation and insights graphs
- **Branch / PR:** `feat/langgraph-preparation` → `main`, not merged.
- **Base:** `main` @ `fbb3fd7` (merged in; the tailoring prompt is v10 because `main`'s #83 took v9).
- **What shipped:**
  - **ResumePreparationGraph** (`app/agents/preparation.py`, queue `app/services/preparation.py`, ADR-025):
    - SELECT_RESUME → LOAD_FIT_ANALYSIS → RETRIEVE_VERIFIED_EVIDENCE → BUILD_TAILOR_CONTEXT → TAILOR →
      VALIDATE_FACTS → REVIEW → DECIDE_PASS_OR_REVISION → AWAIT_APPROVAL.
    - A conductor over the tailor worker. It never ranks: no analysis, or one made from another resume, ends the run.
    - A deterministic evidence or lineage problem overrules a reviewer PASS.
    - The revision loop is bounded (`max_revisions`, per account, default 2). Past it the packet waits for a person
      as NEEDS_HUMAN; it never passes by exhaustion.
    - Revision passes get the previous review (tailoring prompt v10, `PREVIOUS REVIEW`).
    - A person decides in the existing screens.
    - Registered as `WorkerKind` / node `preparation`, scheduler job `preparation`, migration 0049.
  - **JobInsightsGraph** (`app/agents/job_insights.py`, `app/services/job_insights/graph.py`):
    - LOAD_JOB_CONTEXT → … → PERSIST_INSIGHTS, over the research service's own steps on the same task row and claim.
    - Step records are written once per attempt. The synthesis is staged on the task row, never checkpointed.
    - The code-only provenance check (`job_insights/provenance.py`) drops, never relabels.
  - **Approved packet** (`app/services/approval.py`, ADR-026, ADR-027; always on):
    - Approval moves `Job.state` to READY_TO_APPLY through fact-backed, reconciled transitions, or is refused (409).
    - An immutable snapshot and digest are kept; an edit after approval revokes it (new edge READY_TO_APPLY →
      REVIEWED).
    - Export, the apply queue and MCP packet issuance verify the digest.
  - **Evaluations** (`app/services/preparation_eval.py`, 33 hand-labelled cases in LangSmith Example shape, mock
    providers): evidence grounding, unsupported skills and metrics, resume version, keyword relevance, reviewer
    disagreement, loop termination, company provenance, conflicting salary, reported vs generated questions.
    33/33 score 1.0.
- **Old vs new** (`scripts/preparation_parity.py`, PostgreSQL 16, mock Tailor, scripted Reviewer; the current path is
  the tailor worker plus the `review_results` decision Claude Desktop makes):

  | Case | Same first version | Same first decision | Current path | Graph | Versions current/graph |
  | --- | --- | --- | --- | --- | --- |
  | pass | yes | yes | PASS → tailored | PASS → tailored | 1/1 |
  | reviewer sends back, then passes | yes | yes | RETAILOR → needs_retailor | RETAILOR → tailored | 1/2 |
  | reviewer always sends back | yes | yes | RETAILOR → needs_retailor | RETAILOR → pending_approval | 1/3 |
  | score below the minimum | yes | yes | NEEDS_HUMAN → pending_approval | NEEDS_HUMAN → pending_approval | 1/1 |
  | reviewer says skip | yes | yes | NEEDS_HUMAN → pending_approval | NEEDS_HUMAN → pending_approval | 1/1 |
  | invented claim, reviewer passes it | yes | yes | RETAILOR → needs_retailor | RETAILOR → pending_approval | 1/3 |
  | letter with an invented number | yes | yes | RETAILOR → needs_retailor | RETAILOR → pending_approval | 1/3 |

  - In 7 of 7 cases, both paths saved the same first version and made the same first decision.
  - In every case the source resume rows were unchanged and no ranking was made.
  - No version with an unsupported claim was passed.
  - Where the current path stops (needs_retailor), the graph continues its bounded loop.
  - Job Insights: the graph and `service.process` give the same API view on the same fake sources and mock model
    (ids and timestamps aside), with the same source and model calls.
- **Tests:**
  - New suites:
    - `test_preparation_graph.py` (13, PostgreSQL)
    - `test_preparation_parity.py` (7, PostgreSQL)
    - `test_job_insights_graph.py` (16)
    - `test_approval_packet.py` (11, `strict_approval`)
    - `test_preparation_eval.py` (35)
  - Older tests approve jobs the ranker never saw. The new autouse `qualified_before_approval` stand-in treats such a
    job as QUALIFIED at approval, like `ranked_before_tailoring`.
  - Full runs, each in a clean checkout (no local `.env`) next to `main` @ `fbb3fd7`:
    - PostgreSQL: 2299 passed, 7 failed. The same 7 fail on `main`.
    - SQLite: 2150 passed, 0 failed (`main`: 2091 passed).
    - One intermittent PostgreSQL failure:
      `test_agents_postgres::test_the_api_runs_interrupts_and_resumes_the_pilot_for_its_owner_only` failed in 1 of 3
      full runs, the one run alongside the SQLite suite. It passed 3 of 3 times with the new graph suites, and alone.
    - vitest: 330 passed, 2 failed (`overview_outcomes`, as on `main`).
    - Migrations: `test_migrations.py` upgrade, downgrade and empty diff pass.
    - Lint: ruff level with `main`.
- **Known gaps:**
  - Tailoring and review still do not move `Job.state` themselves. It is reconciled at approval (ADR-026). Legacy
    FAILED / ARCHIVED / SKIPPED jobs cannot be approved until re-ranked or restored.
  - The backend path with the graph off still stores reviews without deciding (unchanged).
  - The digest covers the rendered markdown and letter, not the exported PDF/DOCX bytes (ADR-027 revisit).
  - Insights shadow results go to Monitoring → Logs only; there is no parity column.
  - No dataset upload to LangSmith: export stays traces-only.
- **Rollback:**
  - `PREPARATION_GRAPH_MODE=off` and `JOB_INSIGHTS_GRAPH_MODE=off`. Waiting runs are handed back
    (`ROUTED_TO_LEGACY`).
  - The approval rules need a code revert.
  - Migration 0049 is additive and can be downgraded.
- **Next dependency:** Phase 5 needs this PR reviewed and merged. Stop here.

### Phase update template

~~~markdown
### YYYY-MM-DD — Phase N: title
- Branch / PR:
- Base / merge SHA:
- What shipped:
- New migrations, flags, APIs:
- Architectural decisions and deviations:
- Test/evaluation evidence:
- Known gaps:
- Reviewer / approval:
- Production activation and rollback:
- Next dependency:
~~~

## 15. Definition of done for the overall program

The target program is complete only when:

- Existing Job Hunt OS flows retain correctness and tenant isolation.
- Graph orchestration survives worker restart and never silently mutates unauthorized business state.
- RAG demonstrably improves evidence selection with valid citations and lifecycle-safe indexing.
- AI/model/prompt changes are evaluated on reproducible datasets with tracked versions.
- Supported application paths fill approved candidate facts and stop safely on unknowns, CAPTCHA and login.
- Every autonomous submission is authorized, protected by deterministic validation and independently confirmed or marked uncertain.
- Worker lease recovery, browser profile safety, failure injection, audit, tracing, privacy and retention are tested.
- The UI accurately distinguishes live, paused, historical, unsupported and **planned** stages.
- Operators can disable autonomy immediately without deleting applications or history.
- Each of the eight phases has an approved merge decision and actual evidence in the progress ledger.

**Non-goals:** unrestricted web browsing, CAPTCHA solving, credential harvesting, evasion of website protections, fabricated ATS facts, guaranteed automatic submission to every company, undocumented direct access to employer-only APIs, and replacing existing reliable services merely to increase framework usage.

---

*Roadmap maintenance rule: update this document in the same PR as any architectural boundary change. Changes to accepted ADRs, core security invariants, submission authorization or merge gates require explicit reviewer approval. Keep design intent separate from implemented facts.*
