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
| 1. LangGraph foundation | IN_REVIEW | [#72](https://github.com/triump0870/AI-Job-Hunt/pull/72) | — | 2026-10-13 note below | OFF (`AGENT_GRAPHS_ENABLED=false`) |
| 2. RAG knowledge layer | IN_REVIEW | `feat/rag-knowledge-layer` | — | 2026-10-15 note below | OFF (`RAG_RETRIEVAL_MODE=off`, `RAG_VECTOR_ENABLED=false`) |
| 3. Qualification graphs | NOT_STARTED | — | — | — | OFF |
| 4. Preparation / insights graphs | NOT_STARTED | — | — | — | OFF |
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
- **Branch / PR:** `feat/rag-knowledge-layer`, PR to `main`, not merged.
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
    - AUTO from verification and extraction; USER from the person, with removals kept.
    - Per-row editor, bulk tagging, a resume filter, and new evidence defaulting to the active resume.
  - **Derived index:**
    - `rag_index_versions` and `rag_index_entries` (migration 0045); `rag_vectors` is created at runtime when pgvector
      exists (ADR-023).
    - A flush listener handles edits, verification changes and deletions; a sweep repairs anything missed.
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
- **Tests** (local, PostgreSQL 16 + pgvector 0.8.7 built for PG16, `jobhunt` conda env):
  - **New suites:** `test_embeddings.py`, `test_rag_index.py`, `test_rag_retrieval.py`, `test_rag_eval.py` and
    `test_rag_postgres.py`, plus Evidence-bank tag tests in vitest.
  - **SQLite:** 1984 passed, 102 skipped, 1 failed. The failure is
    `test_agents_tracing[error]`, which is flaky on `main` too.
  - **PostgreSQL:** 2078 passed, 8 failed. Seven are the same failures as on untouched `main`. The eighth,
    `test_agents_postgres` API, passes 3/3 in isolation (timing).
  - **vitest:** 319 passed, 2 failed (`overview_outcomes`, which fails on `main` too).
  - **Migrations:** `test_migrations.py` on PostgreSQL covers upgrade, downgrade to baseline and back, and an empty
    metadata diff.
  - **Lint:** ruff is level with `main` (229).
- **Known gaps:**
  - Pack-level relevance cut-off is by budget and candidates only.
  - Company intelligence and application-history collections are deferred to Phase 4.
  - Existing untagged manual evidence applies to every resume until tagged.
  - `tailoring_results.verified_evidence` still validates against the account's verified evidence (citations are
    bounded by the context's allowed ids).
- **Rollback:** `RAG_RETRIEVAL_MODE=off`, `RAG_VECTOR_ENABLED=false`. Optionally downgrade 0045, which also drops
  `rag_vectors`.
- **Next dependency:** Phase 3 needs this PR approved and merged.

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
