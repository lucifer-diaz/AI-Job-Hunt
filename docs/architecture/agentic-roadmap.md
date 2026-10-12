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
| Application submission | Approved Apply Kit and manual submission | **Not autonomous yet**; must pass separate safety milestones |

Read AGENTS.md before implementation. This roadmap does not supersede repository-specific invariants or source code. Re-audit immediately before each phase because the baseline will evolve.

### Known repository-specific constraints

- Every production LLM call uses the existing LLMClient route; no direct provider bypass in LangGraph/LangChain/browser-use.
- Model changes use the AI configuration draft → preview → apply → version path. Runtime failover never rewrites configuration.
- External HTTP/SDK requests use app/services/external_api.py instrumentation; costs unknown to the provider stay NULL.
- Prompts live in app/prompts/ and untrusted external text is data, not instructions.
- AI Orchestration is a read-only projection of canonical records, not a second workflow database.
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
    R --> I["Job Insights"]
    I --> G{"Approval / candidate policy"}
    G -->|"Approved"| A["Application execution"]
    G -->|"Revision"| R
    A -->|"Confirmed"| P["Post-application"]
    A -->|"Human required"| H["Human handoff"]
    H -->|"Authorized resumption"| A
    P -->|"Interview"| I
~~~

- **DiscoveryGraph:** existing scheduled searches → Apify runs → ingestion/deduplication → per-job qualification work. A single search may fan out into many jobs.
- **QualificationGraph:** JD validation/recovery → structured requirements → deterministic hard filters → candidate retrieval → existing Ranker → verified qualification decision.
- **ResumePreparationGraph:** pinned resume selection → context retrieval → Tailor → evidence check → Reviewer → bounded revision/approval.
- **JobInsightsGraph:** approved-job source collection → normalized observations → grounded synthesis → provenance validation → interview preparation.
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

Record accepted changes to these decisions with an appended rationale, date, PR, and consequences. Do not silently edit previously accepted decision history.

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

**Replay invariant:** LangGraph may retry or replay code. Every side effect must have an idempotency key, safe reconciliation, or a guard that requires human review when its outcome is uncertain.

**Transactional dispatch:** if a domain transaction requires a subsequent graph run, prefer a transactional outbox plus idempotent consumer. Do not commit an application state and independently publish an in-memory event whose loss can strand it.

**Graph thread identity:** stable, tenant-scoped identifiers such as qualification:user:job:version and application:user:execution:attempt, stored alongside DB execution metadata. Never let callers supply an arbitrary cross-tenant graph thread ID.

**Checkpoint discipline:** store small serializable fields, source IDs, safe status metadata and control-flow decisions. No raw browser DOM, tokens, full resumes, Gmail bodies, confidential form answers or infinite chat history.

### Proposed application execution model (Phase 5+, not currently shipped)

ApplicationExecution (tenant-owned) references a Job, approved resume version/hash, approval record, execution mode, route, status, lease, attempt and timing. ApplicationExecutionAttempt and a small audit/event representation may be introduced only if existing rows cannot satisfy the invariant.

Execution-level states are distinct from canonical job states:

~~~text
QUEUED → DISCOVERING → ROUTED → FORM_ANALYZING → ANSWERING
       → FILLING → PRE_SUBMIT_VALIDATION → READY_TO_SUBMIT
       → SUBMITTING → CONFIRMING → CONFIRMED

Exceptional:
NEEDS_INPUT, NEEDS_HUMAN, RETRYING, BLOCKED,
SUBMISSION_UNCERTAIN, FAILED, CANCELLED.
~~~

The domain transition to APPLIED is allowed only after sufficiently strong confirmation, via the existing lifecycle policy. An inconclusive click is not a successful application.

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
- The embedding record keeps a source ID, content hash, verification status, embedding model/version, and source version; it does not supersede the underlying row.
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

**Playwright:** deterministic form inspection, accessible locators, controlled page navigation, file uploads and confirmation inspection. Use the existing BrowserService interface; JD-recovery browser contexts remain anonymous and ephemeral.

**browser-use:** bounded page reasoning for unfamiliar multi-step interfaces. Its output is a proposed typed action (e.g., CLICK, FILL, SELECT, UPLOAD, WAIT, REQUEST_HUMAN, STOP), not raw executable JavaScript. Enforce domain allowlists and policy validation before each action. A browser-use WebUI may be used in an isolated development environment; it is not the production frontend or authorization boundary.

**Persistent application sessions:** distinct per-account browser profiles, encrypted/restricted storage, single-owner profile locks and protected manual login. Do not use the person's normal Chrome profile. Do not send browser cookies, passwords, OTPs, session storage or authentication credentials to an LLM.

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

Correlate only safe identifiers: trace ID, graph thread ID, job ID, execution ID, worker, prompt version, actual/requested/configured model, routing reason, configuration version and adapter version. Use pseudonymized user identifiers for external traces, with opt-in and redaction. Hosted traces must not contain resume bodies, application answers, legal/demographic inputs, credentials, cookies, Gmail bodies or browser dumps by default.

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
- Never bypass CAPTCHAs, login walls, anti-bot systems, paywalls or access restrictions.
- Keep third-party cost and tracing failures nonfatal for domain workflows unless a necessary business operation failed.
- Changes to legal/sensitive answers require direct user confirmation; demographic information is never inferred.

## 11. Eight-phase implementation plan

Every phase starts from updated main, uses an isolated branch, produces tests/documentation, and stops for independent review. **A merge is distinct from feature activation.**

| Phase | Branch | Deliverables | Merge gate |
| --- | --- | --- | --- |
| 1. LangGraph foundation | feat/langgraph-foundation | Graph runtime, PostgreSQL checkpointer, compatible LLM adapter, optional LangSmith pilot and flags | Checkpoint survives restart; tenant isolation; existing model routing unaffected |
| 2. RAG knowledge | feat/rag-knowledge-layer | pgvector indexing, hybrid retrieval, context packs, citation validator and eval dataset | Source-backed recall evaluation, no cross-tenant/stale retrieval, baseline regression parity |
| 3. Discovery/qualification graphs | feat/langgraph-qualification | Existing Apify/JD/ranker wrapped in graph nodes, safe branches, shadow mode | Qualified/archive parity, no duplicate work or paid calls |
| 4. Preparation/insights graphs | feat/langgraph-preparation | Resume selection/review graph and grounded company research graph | Resume lineage intact; unsupported claims blocked; insights provenance preserved |
| 5. Application execution | feat/application-execution | Durable execution, CandidatePolicy, AnswerResolver, typed form schema, Playwright observe/assist | Safe fill and restart; **no final automated submission** |
| 6. Guarded browser submission | feat/agentic-browser-apply | Generic form engine, browser-use fallback, pre-submit policy, intent, confirm/reconcile, human takeover | Crash/retry never blindly re-submits; authorization and protected actions tested |
| 7. Evaluation/feedback | feat/agent-evaluation-feedback | LangSmith datasets, Gmail event correlation, question memory, graph UI and monitoring improvements | End-to-end trace fidelity, deterministic metric sourcing, privacy checks |
| 8. Production hardening | feat/agentic-production-hardening | Fault injection, load, worker supervision, security, retention, rollout controls and runbooks | Full regression, failure recovery, security review, signed GO/NO-GO |

Phase plans are elaborated in the accompanying Claude Code implementation prompts. All prospective modules/APIs/tables remain **proposed** until an associated PR is merged. Never mark a phase complete based solely on having written a prompt or plan.

### Phase details and deliverables

#### Phase 1: Foundation and evaluation
- Audit current scheduler, LLM routing, Graph-related dependencies, migrations, telemetry and configuration.
- Add durable checkpointer and a pilot graph which branches, interrupts and resumes.
- Add opt-in LangSmith trace integration; preserve existing telemetry.
- **Exit demonstration:** terminate a pilot worker during execution, restart it, resume correctly, and show no data leak.
- **Rollback:** turn off graph flag; old production services remain unchanged.

#### Phase 2: Candidate RAG and ATS answer retrieval
- Add PostgreSQL vector extension/migrations, derived index lifecycle and hybrid retrieval.
- Reuse Evidence/KnowledgeAnswer and current context builders.
- Establish labeled retrieval benchmarks and a non-vector fallback for unsupported development databases.
- **Exit demonstration:** one JD receives a correctly attributed, bounded verified evidence pack; stale/other-tenant evidence is excluded.
- **Rollback:** disable retrieval flag and use existing deterministic context builders.

#### Phase 3: Qualification migration
- Implement graph nodes around existing discovery/JD recovery/parser/filter/ranker.
- Use shadow mode with deterministic fixtures before any production traffic cutover.
- **Exit demonstration:** qualified/archive decisions and state history agree with the current path on representative cases.
- **Rollback:** route work to the current scheduler/service implementation.

#### Phase 4: Resume preparation and job insights
- Implement bounded tailor/reviewer revision and human approval interrupts.
- Extend source-grounded company research using existing Job Insights/Apify adapters; do not invent provider schemas.
- **Exit demonstration:** immutable pinned resume; all factual claims source-checked; employer and market pay remain separate.
- **Rollback:** keep existing workflow services and Job Insights endpoints active.

#### Phase 5: Application execution
- Add durable execution-level state, normalized form, answer resolution and browser session layer.
- Test a single known ATS adapter in OBSERVE/ASSIST.
- **Exit demonstration:** inspect/fill to READY_TO_SUBMIT and resume after process crash; no submit action is reachable.
- **Rollback:** revert to approved Apply Kit/manual submission.

#### Phase 6: Guarded submission and agentic fallback
- Implement pre-submit validator, authorized SubmissionIntent, confirmation verifier and reconciliation.
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

**Open design questions before relevant phases:** supported embedding model and dimensions; vector extension availability in all environments; acceptable LangSmith deployment/privacy posture; ATS priority by observed Job Hunt OS volume; local vs remote human browser handoff; confirmation-strength thresholds; retention rules for artifacts; production autonomy eligibility and applicable website terms.

Do not silently resolve any of these by assuming a framework provides a capability or that a site's public API permits submission.

## 14. Progress ledger

This section is intended to change after *each approved phase merge*. Initial entries are placeholders, not claims of completion.

| Phase | Status | PR | Merge SHA | Evaluation / test evidence | Production flag |
| --- | --- | --- | --- | --- | --- |
| 1. LangGraph foundation | NOT_STARTED | — | — | — | OFF |
| 2. RAG knowledge layer | NOT_STARTED | — | — | — | OFF |
| 3. Qualification graphs | NOT_STARTED | — | — | — | OFF |
| 4. Preparation / insights graphs | NOT_STARTED | — | — | — | OFF |
| 5. Application execution | NOT_STARTED | — | — | — | OBSERVE only (planned) |
| 6. Guarded agentic submission | NOT_STARTED | — | — | — | OFF |
| 7. Evaluation and feedback | NOT_STARTED | — | — | — | OFF |
| 8. Production hardening | NOT_STARTED | — | — | — | OFF |

Allowed status: NOT_STARTED, PLANNING, IN_PROGRESS, IN_REVIEW, MERGED, BLOCKED, ROLLED_BACK. Update only with evidence. Prefer a new dated progress note below to explain scope, deferrals and decisions rather than overwriting history.

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
