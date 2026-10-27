"""Prompt token budgets: no budget changes nothing; over a budget the least relevant items go, deterministically."""
import copy
import io
import json
import re

import pytest

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.models import Evidence, Job, JobRequirementEvidence
from app.prompts import build_ranking_prompt, build_tailoring_prompt
from app.prompts.ai_optimization import build_ai_optimization_prompt
from app.services import ai_advisor, prompt_budget
from app.services import context_builder as cb
from app.services.job_description import description_hash
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.conftest import make_user
from tests.test_context_builder import JOB_DESCRIPTION, RESUME, client, resume_id, session


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    client.post("/api/profile/resume", files={"file": ("jane.txt", io.BytesIO(RESUME.encode()), "text/plain")})
    return user_id


REQUIRED = [("req_1", "SKILL", "Kafka"), ("req_2", "EXPERIENCE", "5+ years of data platform engineering"),
            ("req_3", "SKILL", "Python")]
PREFERRED = [(f"pref_{i}", "SKILL", f"Familiarity with tool number {i} for streaming analytics") for i in range(12)]
RESPONSIBILITIES = [f"Own reliability of Kafka pipeline area {i} and mentor the engineers who run it" for i in range(10)]


def structured_job(user_id):
    """A parsed posting: three REQUIRED requirements, many PREFERRED ones and many responsibilities."""
    with session(user_id) as db:
        job = Job(source="manual", source_ref="budget-job", title="Data Platform Lead", company="Globex",
                  description=JOB_DESCRIPTION, status="shortlisted", skills=["Kafka", "Python"])
        db.add(job)
        db.flush()
        job.structured_description = {
            "job_identity": {"title": "Data Platform Lead", "company": "Globex"},
            "role_summary": "Lead the data platform", "responsibilities": RESPONSIBILITIES,
            "required_skills": ["Kafka", "Python"], "preferred_skills": [p[2] for p in PREFERRED],
            "hard_requirements": ["5+ years of data platform engineering"]}
        job.structured_description_hash = description_hash(job.description)
        job.structured_description_status = "PARSED"
        for level, rows in (("REQUIRED", REQUIRED), ("PREFERRED", PREFERRED)):
            for index, (item, kind, value) in enumerate(rows):
                db.add(JobRequirementEvidence(job_id=job.id, item_id=item, requirement_type=kind,
                                              normalized_value=value, requirement_level=level,
                                              source_text=value, confidence=0.9 - index / 100))
        topics = ["Kafka consumer lag alerts", "Python ingestion services", "data platform cost reviews",
                  "incident postmortems for pipelines", "mentoring programme for engineers", "schema registry rollout",
                  "streaming backfill tooling", "on-call rotation redesign", "warehouse migration to Snowflake",
                  "Airflow DAG reliability checks", "Kafka cluster upgrade", "pipeline SLO dashboards"]
        db.add_all([Evidence(claim=f"Delivered {topic} for the {['payments', 'search', 'ads', 'risk'][i % 4]} group",
                             company=f"Company {i}", role="Engineer", skills=["Kafka"])
                    for i, topic in enumerate(topics)])
        job.fit_analysis = {"candidate_fit_score": 80, "resume_fit_score": 75, "resume_gaps": ["Go is not shown"],
                            "requirement_assessments": [
                                {"requirement_id": "req_1", "requirement": "Kafka", "level": "REQUIRED",
                                 "status": "MATCH", "evidence_ids": []},
                                *[{"requirement_id": p[0], "requirement": p[2], "level": "PREFERRED",
                                   "status": "PARTIAL_MATCH", "evidence_ids": []} for p in PREFERRED[:6]]]}
        db.commit()
        return job.id


def verify_resume():
    rid = resume_id()
    for section in ("experience", "skills", "summary", "education"):
        client.post(f"/api/resumes/{rid}/verify-section", json={"section": section})


def build_and_render(user_id, job_id, task, budget=None):
    builder, render = {"ranking": (cb.build_ranking_context, build_ranking_prompt),
                       "tailoring": (cb.build_tailoring_context, build_tailoring_prompt)}[task]
    with session(user_id) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        context = builder(db, profile, job)
        prompt = cb.fit_prompt(context, job, lambda: render(candidate_profile=profile, job=job, context=context),
                               budget)
        db.rollback()
        return context, prompt


def shown_ids(prompt):
    return {int(i) for i in re.findall(r'"id": (\d+)', prompt.user)}


@pytest.fixture
def ranked(user):
    verify_resume()
    return user, structured_job(user)


# ───────────── no budget: exactly the builders ─────────────

@pytest.mark.parametrize("task", ["ranking", "tailoring"])
def test_without_a_budget_the_prompt_and_context_are_the_builders_own(ranked, task):
    user_id, job_id = ranked
    builder, render = {"ranking": (cb.build_ranking_context, build_ranking_prompt),
                       "tailoring": (cb.build_tailoring_context, build_tailoring_prompt)}[task]
    with session(user_id) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        plain = builder(db, profile, job)
        expected = render(candidate_profile=profile, job=job, context=plain)
        db.rollback()
    for budget in (None, 0):
        context, prompt = build_and_render(user_id, job_id, task, budget)
        assert (prompt.system, prompt.user) == (expected.system, expected.user)
        assert context.to_dict() == plain.to_dict() and context.call_stats() == plain.call_stats()
        assert "trimmed" not in context.stats.as_dict() and "prompt_token_budget" not in context.call_stats()


def test_a_budget_the_prompt_already_fits_trims_nothing(ranked):
    user_id, job_id = ranked
    plain, prompt = build_and_render(user_id, job_id, "ranking")
    context, fitted = build_and_render(user_id, job_id, "ranking", prompt_budget.prompt_tokens(prompt))
    assert fitted.user == prompt.user and context.stats.trimmed == {}
    assert context.stats.prompt_token_budget == context.stats.prompt_tokens == prompt_budget.prompt_tokens(prompt)
    assert context.stats.selected_context_tokens == plain.stats.selected_context_tokens


# ───────────── ranking ─────────────

def test_ranking_trims_in_order_never_drops_required_and_never_cites_what_left(ranked):
    user_id, job_id = ranked
    plain, full = build_and_render(user_id, job_id, "ranking")
    budget = prompt_budget.prompt_tokens(full) - 500
    context, prompt = build_and_render(user_id, job_id, "ranking", budget)

    assert prompt_budget.prompt_tokens(prompt) <= budget
    trimmed = context.stats.trimmed
    assert trimmed and list(trimmed) == [k for k in cb.RANKING_TRIM_ORDER if k in trimmed]
    assert sum(trimmed.values()) > 0 and context.stats.prompt_tokens_before_trim == prompt_budget.prompt_tokens(full)
    # Every REQUIRED requirement and the employer's sentences for them stay; PREFERRED ones went first.
    assert [r["id"] for r in context.job_context["required"]] == [r[0] for r in REQUIRED]
    assert all(f'"{item}"' in prompt.user for item, *_ in REQUIRED)
    assert len(context.job_context["preferred"]) < len(plain.job_context["preferred"])
    kept = {r["id"] for r in context.job_context["required"] + context.job_context["preferred"]}
    overlap = context.job_context["candidate_matches"] + context.job_context["candidate_gaps"]
    assert {m["id"] for m in overlap} <= kept, "the term overlap never names a requirement no longer shown"
    assert len(context.job_context["responsibilities"]) >= cb.TRIM_KEEP_RESPONSIBILITIES
    # The validator's citable ids are what the prompt still shows: dropped evidence cannot be cited.
    dropped = {e["id"] for e in plain.relevant_evidence} - {e["id"] for e in context.relevant_evidence}
    assert not dropped & context.evidence_ids
    assert {e["id"] for e in context.relevant_evidence} <= shown_ids(prompt)
    assert all(role["bullets"] for role in context.relevant_experience if role in plain.relevant_experience)
    # Recorded in the existing stats.
    assert context.stats.selected_context_tokens < plain.stats.selected_context_tokens
    assert context.stats.context_reduction_percent > plain.stats.context_reduction_percent
    assert context.job_stats.selected_job_context_tokens < plain.job_stats.selected_job_context_tokens
    stats = context.call_stats()
    assert stats["trimmed"] == trimmed and stats["prompt_token_budget"] == budget


def test_ranking_trimming_is_deterministic(ranked):
    user_id, job_id = ranked
    _, full = build_and_render(user_id, job_id, "ranking")
    budget = prompt_budget.prompt_tokens(full) // 2
    first, a = build_and_render(user_id, job_id, "ranking", budget)
    second, b = build_and_render(user_id, job_id, "ranking", budget)
    assert a.user == b.user and first.to_dict() == second.to_dict()
    assert cb.latest_ranking_context_hash(first) == cb.latest_ranking_context_hash(second)


def test_an_impossible_budget_keeps_what_a_decision_rests_on(ranked):
    user_id, job_id = ranked
    context, prompt = build_and_render(user_id, job_id, "ranking", 300)
    assert prompt_budget.prompt_tokens(prompt) > 300  # sent as small as it gets; the gateway's window still guards
    assert [r["id"] for r in context.job_context["required"]] == [r[0] for r in REQUIRED]
    assert context.job_context["preferred"] == [] and context.relevant_evidence == []
    assert len(context.job_context["responsibilities"]) == cb.TRIM_KEEP_RESPONSIBILITIES
    assert all(len(role["bullets"]) <= 1 for role in context.relevant_experience)
    assert context.candidate.get("summary")


def test_ranking_term_overlap_is_recounted_from_the_facts_still_shown(ranked):
    from app.services import job_context as jc

    user_id, job_id = ranked
    plain, _ = build_and_render(user_id, job_id, "ranking")
    context, _ = build_and_render(user_id, job_id, "ranking", 300)
    assert plain.relevant_evidence and context.relevant_evidence == []
    job = context.job_context
    assert (job["candidate_matches"], job["candidate_gaps"]) == tuple(
        jc.candidate_match(job["required"] + job["preferred"], context))
    shown = jc._tokens(" ".join([*context.relevant_skills, *[
        f"{r.get('job_title', '')} {r.get('company', '')} " + " ".join(b["text"] for b in r["bullets"])
        for r in context.relevant_experience]]))
    assert all(set(m["matched_terms"]) <= shown for m in job["candidate_matches"])


def test_a_trimmed_rankings_identity_covers_the_responsibilities_it_kept(ranked):
    from app.services import ranker

    user_id, job_id = ranked
    with session(user_id) as db:
        job = db.get(Job, job_id)
        plain, _ = build_and_render(user_id, job_id, "ranking")
        # Untrimmed, the hash is what it always was: no existing ranking is invalidated.
        assert ranker.ranking_job_hash(job, plain) == ranker._hash({
            **ranker.posting_facts(job), "description": job.description or "",
            "structured": job.structured_description_hash,
            "requirements": plain.job_context["required"] + plain.job_context["preferred"]})
        trimmed, _ = build_and_render(user_id, job_id, "ranking", 300)
        fewer = copy.deepcopy(trimmed)
        fewer.job_context["responsibilities"].pop()
        assert ranker.ranking_job_hash(job, fewer) != ranker.ranking_job_hash(job, trimmed)
        db.rollback()



def test_tailoring_keeps_the_last_thing_it_can_tailor_from(ranked):
    user_id, job_id = ranked
    with session(user_id) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        context = cb.build_tailoring_context(db, profile, job)
        project = {"name": "Stream", "bullets": [{"text": "Built a Kafka stream processor"}]}
        context.relevant_projects, context.relevant_evidence = [project], []
        context.source_resume = {"projects": [{"name": "Stream", "bullets": ["Built a Kafka stream processor"]}]}
        steps = {step.kind: step.drop for step in cb.trim_steps(context, job)}
        assert steps["projects"]() is False and context.source_resume["projects"]
        evidence = {"id": 1, "claim": "Ran Kafka", "company": "A", "role": "Engineer"}
        context.relevant_evidence.append(evidence)
        assert steps["projects"]() is True and not context.source_resume
        assert steps["evidence"]() is False and context.relevant_evidence == [evidence]
        cb.validate_tailoring_context(context, len(context.roles))
        db.rollback()



def test_tailoring_trims_spare_bullets_first_and_keeps_citations_consistent(ranked):
    user_id, job_id = ranked
    plain, full = build_and_render(user_id, job_id, "tailoring")
    budget = prompt_budget.prompt_tokens(full) - 700
    context, prompt = build_and_render(user_id, job_id, "tailoring", budget)

    assert prompt_budget.prompt_tokens(prompt) <= budget
    trimmed = context.stats.trimmed
    assert list(trimmed) == [k for k in cb.TAILORING_TRIM_ORDER if k in trimmed]
    assert trimmed.get("evidence") or trimmed.get("preferred_requirements") or trimmed.get("spare_bullets")
    caps = context.length_plan["role_caps"]
    for role in context.roles:  # a role never loses a bullet its budget may use
        before = next(r for r in plain.roles if r["role_index"] == role["role_index"])
        assert role["bullets"] == before["bullets"][:max(len(role["bullets"]), caps[str(role["role_index"])])]
    # Everything the Tailor may cite is in the prompt, and the requirement map cites nothing else.
    assert context.citable_ids <= shown_ids(prompt)
    assert all(set(row.get("evidence_ids") or []) <= context.citable_ids for row in context.requirement_map)
    assert [r for r in context.requirement_map if r.get("level") == "REQUIRED"] or \
        not [r for r in plain.requirement_map if r.get("level") == "REQUIRED"]
    assert [r["id"] for r in context.job_context["required"]] == [r[0] for r in REQUIRED]
    # The source resume and the selection behind it lost the same bullets.
    for role in context.source_resume.get("roles") or []:
        same = next(r for r in context.roles if r["role_index"] == role["role_index"])
        assert [b["text"] for b in role["bullets"]] == same["bullets"]
    cb.validate_tailoring_context(context, len(plain.roles) + 10)
    assert context.stats.selected_context_tokens < plain.stats.selected_context_tokens


def test_tailoring_with_a_budget_is_recorded_on_the_tailored_resume(ranked, monkeypatch):
    user_id, job_id = ranked
    _, full = build_and_render(user_id, job_id, "tailoring")
    budget = prompt_budget.prompt_tokens(full) - 700
    monkeypatch.setattr(prompt_budget, "budget_for", lambda db, uid, worker: budget if worker == "TAILOR" else None)
    out = client.post(f"/api/jobs/{job_id}/tailor")
    assert out.status_code == 200, out.text
    with session(user_id) as db:
        from app.models import ResumeVariant

        variant = db.query(ResumeVariant).filter(ResumeVariant.job_id == job_id).order_by(ResumeVariant.id.desc()).first()
        recorded = variant.content["context"]
    assert recorded["prompt_token_budget"] == budget and recorded["trimmed"]
    assert recorded["prompt_tokens"] <= budget


# ───────────── AI Optimization ─────────────

def advisor_inputs():
    stage = {"stage": "RANKER", "requests": 40, "avg_context_tokens": 6300}
    rows = lambda n, **extra: [{"n": i, "label": f"row {i} " + "x" * 60, **extra} for i in range(n)]
    external = {"range": {"days": 7}, "totals": {"requests": 300}, "providers": [{"provider": "APIFY"}],
                "operations": rows(25), "workflows": rows(10), "errors": rows(8), "apify": {"runs": 5},
                "apify_by_actor": rows(10), "apify_by_saved_search": rows(10)}
    aggregates = {"range": {"days": 7}, "totals": {"requests": 400}, "stages": [stage] * 8, "models": rows(8),
                  "stage_models": rows(30), "prompt_versions": rows(20), "errors": rows(10),
                  "context_buckets": rows(6), "duplicate_successful_calls": [],
                  "routing": {"requests": 400, "workers": rows(5), "worker_models": rows(20)},
                  "outliers": {"largest_context": rows(10), "most_expensive": rows(10)}, "external": external}
    findings = [{"category": "CONTEXT", "priority": "HIGH", "stage": "RANKER", "finding": "Large context"}]
    return aggregates, findings


def test_advisor_without_a_budget_is_the_prompt_as_before():
    aggregates, findings = advisor_inputs()
    prompt, stats = ai_advisor.fit_advisor_prompt(aggregates, findings, None)
    expected = build_ai_optimization_prompt(aggregates, findings, aggregates["external"])
    assert prompt.user == expected.user and stats is None


def test_advisor_trims_detail_lists_in_order_and_keeps_totals_and_findings():
    aggregates, findings = advisor_inputs()
    original = copy.deepcopy(aggregates)
    full = build_ai_optimization_prompt(aggregates, findings, aggregates["external"])
    budget = prompt_budget.prompt_tokens(full) // 2
    prompt, stats = ai_advisor.fit_advisor_prompt(aggregates, findings, budget)
    again, _ = ai_advisor.fit_advisor_prompt(aggregates, findings, budget)

    assert prompt_budget.prompt_tokens(prompt) <= budget and prompt.user == again.user
    assert aggregates == original, "the report keeps the full aggregates; only the prompt is trimmed"
    order = [kind for kind, _ in ai_advisor.ADVISOR_TRIM_ORDER]
    assert list(stats["trimmed"]) == [k for k in dict.fromkeys(order) if k in stats["trimmed"]]
    assert stats["trimmed"]["duplicate_external"] == 1
    assert stats["prompt_token_budget"] == budget and stats["selected_context_tokens"] < stats[
        "full_available_context_tokens"] and stats["context_reduction_percent"] > 0
    usage = json.loads(prompt.user.split("USAGE AGGREGATES:\n", 1)[1].split("\n\nEXTERNAL API AGGREGATES", 1)[0])
    assert usage["totals"] == original["totals"] and usage["range"] == original["range"] and "external" not in usage
    assert len(usage["stages"]) >= ai_advisor.ADVISOR_TRIM_KEEP
    assert json.dumps(findings, ensure_ascii=False) in prompt.user


# ───────────── the budget comes from the active configuration, through the gateway only ─────────────

def test_the_budget_applies_only_through_the_litellm_gateway(user, monkeypatch):
    from app.services import model_resolution
    from app.services.model_resolution import ResolvedWorker

    monkeypatch.setattr(model_resolution, "get", lambda uid, worker: ResolvedWorker(
        worker_type=worker, mode="TIER", configured="FAST", model="m", prompt_token_budget=7000))
    with session(user) as db:
        assert prompt_budget.budget_for(db, user, "RANKER") is None  # LLM_PROVIDER is mock here
        monkeypatch.setattr(get_settings(), "llm_provider", "litellm")
        assert prompt_budget.budget_for(db, user, "RANKER") == 7000
        assert prompt_budget.budget_for(db, None, "RANKER") is None
