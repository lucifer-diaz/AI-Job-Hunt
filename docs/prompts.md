# Prompt conventions

How to add or change an LLM prompt in Job Hunt OS. The README's "LLM Prompt Architecture" explains the flow; this
page is the checklist.

```
context builder → prompt builder → LLM gateway → output contract → evidence / policy validation → persistence → workflow
```

## Where things live

| What | Where |
|---|---|
| Prompt text, builders, `*_PROMPT_VERSION` | `app/prompts/<worker>.py` — nowhere else (`tests/test_prompts.py` enforces it) |
| Output contracts (pydantic) | `app/prompts/contracts.py`, mapped in `app.prompts.OUTPUT_CONTRACTS` |
| Shared rules and section helpers | `app/prompts/common.py` |
| Data selection | `app/services/context_builder.py` (candidate facts), `app/services/job_context.py` (the posting) |
| Worker registry and model requirements | `app/services/ai_workers.py` |
| Contract enforcement, correction retry, telemetry | `app/services/llm.py`, `app/services/llm_telemetry.py` |
| Evidence checks for generated resumes | `app/services/tailoring_results.py` |
| Golden fixtures | `tests/fixtures/prompt_golden/<worker>.json`, loaded by `tests/golden.py` |

## A prompt has

1. **One responsibility.** Say what the worker is and what it is not ("You are not the Ranker"). A worker never
   decides another worker's output, a model, or a workflow state.
2. **A short system message**: role, truth boundary, task, what is forbidden, the output contract. No dynamic data.
   Rules Python can enforce (state transitions, filtering by verification status, evidence existence) live in Python,
   not in prose. Local models get a concise contract, not the same policy repeated three ways.
3. **Data in labelled sections** of the user message. Application data uses `section()`. Anything external — job
   postings, emails, uploaded resumes, form questions, web text, another model's output under review — uses
   `untrusted()`, which strips any `<untrusted_...>` tag the text carries so it cannot close or open a block.
4. **A contract** (`output_schema`). Fields come from `describe_fields(Contract)`, so the prompt and the schema cannot
   drift. The contract forbids unknown fields and shares no field name with another worker's contract
   (`test_worker_output_schemas_do_not_overlap`).
5. **A version.** Bump `*_PROMPT_VERSION` when the behaviour changes (instructions, fields, what context it gets).
   Services store it with the result, next to provider, model and AI config version. Stored results keep the version
   they were made with; old results are never rewritten. Bumping `RANKING_PROMPT_VERSION` re-queues qualified jobs
   for ranking (`prompt_changed`), which costs a model call each.

## Context builders

- Build a projection for the task: only the facts it needs, one canonical copy of each. Never send the whole
  CandidateProfile, search or scraper settings (`candidate_preferences` removes them), or a raw document when a
  structured version exists (raw text is for parsing, auditing and fallback).
- Filter by CandidatePolicy before the prompt: unverified and rejected facts are removed unless the policy allows them.
- Send each fact once. The verified resume (experience and projects) is the single source of candidate facts: each
  bullet carries one `id`, the evidence row created from it, which is what a model cites. Evidence created from the
  resume is never listed again, and evidence repeating any resume bullet drops out, so VERIFIED EVIDENCE holds only
  verified facts that are not on the resume (and is not sent when there are none). Evidence goes to a prompt as
  `{id, company, role, claim}` only.
- Trust follows verification: verified facts are application data (`section()`); only unverified or rejected resume
  text, and external text, go through `untrusted()`. A raw posting is sent as the job context's cleaned copy
  (`posting_text`: repeats and legal boilerplate removed).
- Never send an empty placeholder for another worker's output. If an upstream result is missing, raise
  `ContextError` (`RANKING_REQUIRED`, `TAILOR_CONTEXT_INCOMPLETE`, ...) and do not call the model.
- Validate input consistency first (`validate_tailoring_context`): ids unique and valid, the FitAnalysis belongs to
  this job, the resume version exists.
- Record what was selected: `TaskContext.call_stats()` travels in `LLMCall(context_stats=...)`.

## Generated facts

- Every factual unit carries its own `evidence_ids`. Validation drops a unit whose evidence is unknown, unverified,
  from another employer or outside the context, or whose numbers or named technologies its evidence does not show.
- A requirement no verified evidence supports stays a gap. Prompts say so (`GAPS_STAY_GAPS_RULE`); validation
  enforces it (`UNSUPPORTED_TERM`, `INVENTED_SKILL`).
- Writing workers (cover letters, follow-ups) may sound natural; their facts still come from verified context.

## Adding a worker

1. `app/prompts/<worker>.py`: system text, builder, version; a contract in `contracts.py` and `OUTPUT_CONTRACTS`.
2. A `Worker` in `app/services/ai_workers.py` (prompt names, `structured_output_required`, minimum context, default
   assignment) and its stage in `llm_telemetry.STAGE_BY_PROMPT`.
3. A context builder in `app/services/context_builder.py`.
4. A golden fixture in `tests/fixtures/prompt_golden/` and a builder case in `tests/golden.py`: valid answer, answers
   in other workers' shapes, facts that must and must not reach the prompt, claims it must never make.
5. Services call `LLMClient.generate_prompt(prompt, LLMCall(...))` and use `result.validated`, never raw JSON.

## Evaluating a change

```bash
python scripts/evaluate_prompts.py --user-id 1 --out results/before.json
python scripts/evaluate_prompts.py --user-id 1 --model tier-2-reasoning --model tier-3-review
python scripts/evaluate_prompts.py --compare results/before.json results/after.json
```

Only fictional fixtures are sent. Results report schema success and failure reasons, evidence validity, prohibited
claims, expected classifications, latency and tokens; each request also appears in Monitoring (workflow `eval`).
