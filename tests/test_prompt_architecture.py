"""The prompt architecture end to end: context builder → prompt → LLM gateway → contract → evidence validation →
persistence. Covers the Tailor/Ranker boundary, fail-early upstream checks, hallucination filtering, the schema
correction retry, telemetry of structured output, evidence provenance and central redaction."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Evidence, Job, LLMRequestLog, ResumeVariant
from app.prompts import (
    build_cover_letter_prompt,
    build_ranking_prompt,
    build_review_prompt,
    build_tailoring_prompt,
)
from app.prompts.contracts import TailoringResult
from app.prompts.cover_letter import COVER_LETTER_PROMPT_VERSION
from app.services import context_builder as cb
from app.services import model_resolution
from app.services.llm import LLMClient
from app.services.redaction import is_secret_field, redact_text, redact_value
from app.services.tailoring_results import TailoringRejected, apply_tailoring_patch
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.test_context_builder import add_job, build, client, session, verify
from tests.test_context_builder import (
    user as user,  # noqa: PLC0414  the fixture, re-exported for this module
)

COMPLETED = {"candidate_fit_score": 81, "resume_fit_score": 66, "overall_fit_score": 78, "score": 78,
             "strong_matches": ["Kafka pipelines"], "partial_matches": ["Team leadership; not shown: hiring"],
             "missing_requirements": ["PHP"], "fatal_gaps": [], "resume_gaps": ["Reliability impact not quantified"],
             "recommended_resume_focus": ["Lead with incident reduction"], "confidence": 0.8}
RANKER_SHAPED = {"score": 85, "candidate_fit_score": 85, "resume_fit_score": 80, "strong_matches": [], "gaps": []}


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def spy(monkeypatch):
    """Every prompt that reaches the LLM layer, by name."""
    seen: dict[str, list[str]] = {}
    real = LLMClient.generate_prompt

    def record(self, prompt, call=None):
        seen.setdefault(prompt.name, []).append(prompt.user)
        return real(self, prompt, call)
    monkeypatch.setattr(LLMClient, "generate_prompt", record)
    return seen


def set_fit(user_id, job_id, analysis):
    with session(user_id) as db:
        db.get(Job, job_id).fit_analysis = analysis
        db.commit()


def logs(user_id, prompt_name):
    with SessionLocal() as db:
        return db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == user_id,
                                                      LLMRequestLog.prompt_name == prompt_name)
                          .order_by(LLMRequestLog.id)).all()


# ───────────── fail early: the Tailor needs the Ranker's completed analysis ─────────────

@pytest.mark.strict_ranking
@pytest.mark.parametrize("analysis", [None, {"score": 80, "summary": "", "strong_matches": [], "gaps": []}],
                         ids=["never ranked", "legacy empty template"])
def test_tailoring_without_a_completed_ranking_never_calls_a_model(user, spy, analysis):
    job_id = add_job(user)
    set_fit(user, job_id, analysis)
    response = client.post(f"/api/jobs/{job_id}/tailor")
    assert response.status_code == 400 and response.json()["detail"].startswith("RANKING_REQUIRED")
    assert "tailoring" not in spy and "cover_letter" not in spy
    with session(user) as db:
        assert db.scalars(select(ResumeVariant).where(ResumeVariant.job_id == job_id)).all() == []


@pytest.mark.strict_ranking
def test_a_fit_analysis_from_another_job_is_refused(user, spy):
    job_id = add_job(user)
    set_fit(user, job_id, {**COMPLETED, "ranking_id": 987654})
    response = client.post(f"/api/jobs/{job_id}/tailor")
    assert response.status_code == 400 and response.json()["detail"].startswith("FIT_ANALYSIS_MISMATCH")
    assert "tailoring" not in spy


@pytest.mark.strict_ranking
def test_the_tailor_receives_the_completed_analysis_as_read_only_input(user, spy):
    verify("experience", "skills")
    job_id = add_job(user)
    set_fit(user, job_id, COMPLETED)
    assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    user_message = spy["tailoring"][0]
    fit = json.loads(user_message.split("FIT ANALYSIS (completed by the Ranker; read-only):\n", 1)[1].split("\n\n")[0])
    assert fit["strong_matches"] == ["Kafka pipelines"] and fit["gaps_to_preserve"] == ["PHP"]
    assert not {"candidate_fit_score", "resume_fit_score", "score", "confidence"} & set(fit)
    # One canonical copy of each fact: the verified bullet appears once in the source resume.
    assert user_message.count("Reduced production incidents by 45% across Kafka pipelines") == 1 + \
        user_message.split("RELEVANT EVIDENCE:", 1)[1].count("Reduced production incidents by 45%")


# ───────────── hallucinations are dropped before anything is stored ─────────────

def _patch(**parts):
    return TailoringResult.model_validate({"skills_order": [], "role_updates": [], **parts})


def test_generated_claims_the_evidence_does_not_support_are_dropped(user):
    verify("experience", "skills")
    job_id = add_job(user, description="Kafka reliability lead. PHP and Java required. Terraform is a plus.")
    with session(user) as db:
        evidence = db.scalar(select(Evidence).where(Evidence.claim.like("Reduced production incidents%")))
        profile, job = profile_for(db), db.get(Job, job_id)
        patch = _patch(
            summary={"text": "Java and PHP engineer", "evidence_ids": [evidence.id]},
            skills_order=["Python", "Kafka", "PHP", "Java", "Terraform"],
            role_updates=[{"role_index": 0, "bullets": [
                {"text": "Cut production incidents by 45% across Kafka pipelines", "evidence_ids": [evidence.id]},
                {"text": "Built PHP services behind the Kafka pipelines", "evidence_ids": [evidence.id]},
                {"text": "Owned Terraform deployments for every service", "evidence_ids": []},
                {"text": "Wrote Java consumers for Kafka", "evidence_ids": [999999]}]},
                {"role_index": 7, "bullets": [{"text": "Anything at all", "evidence_ids": [evidence.id]}]}])
        content = apply_tailoring_patch(db, profile, job, patch, allowed_evidence={evidence.id})
        codes = {issue["code"] for issue in content["validation"]["issues"]}
        assert {"UNSUPPORTED_TERM", "INVENTED_SKILL", "UNKNOWN_EVIDENCE", "UNCITED_CLAIM", "UNKNOWN_ROLE"} <= codes
        assert content["experience"] == [{"role_index": 0, "bullets": [
            "Cut production incidents by 45% across Kafka pipelines"]}]
        assert content["skills"] == ["Python", "Kafka"] and content["summary"] == ""
        assert content["bullet_evidence"][0]["evidence_ids"] == [evidence.id]
        text = json.dumps({k: content[k] for k in ("summary", "skills", "experience", "experience_bullets")})
        assert not any(word in text for word in ("PHP", "Java", "Terraform")), "missing requirements stay missing"

        # Evidence the context did not offer is not usable either, and a patch with nothing valid stores nothing.
        with pytest.raises(TailoringRejected, match="TAILOR_OUTPUT_UNSUPPORTED"):
            apply_tailoring_patch(db, profile, job, _patch(role_updates=[{"role_index": 0, "bullets": [
                {"text": "Cut production incidents by 45% across Kafka pipelines", "evidence_ids": [evidence.id]}]}]),
                allowed_evidence=set())


def test_the_renderer_keeps_every_role_the_patch_did_not_touch(user):
    from app.services.workflow import build_resume_markdown

    verify("experience", "skills")
    job_id = add_job(user)
    with session(user) as db:
        evidence = db.scalar(select(Evidence).where(Evidence.claim.like("Reduced production incidents%")))
        profile = profile_for(db)
        content = apply_tailoring_patch(db, profile, db.get(Job, job_id), _patch(role_updates=[
            {"role_index": 0, "bullets": [{"text": "Cut production incidents by 45% on Kafka pipelines",
                                           "evidence_ids": [evidence.id]}]}]), allowed_evidence={evidence.id})
        markdown = build_resume_markdown(profile, content)
    for kept in ("Lead Engineer | Zendrive", "Frontend Developer | Pixel Co", "Built React dashboards",
                 "Cut production incidents by 45% on Kafka pipelines", "B.Tech"):
        assert kept in markdown, kept


# ───────────── the gateway: contract, one correction, then failure ─────────────

def test_a_tailor_answering_like_the_ranker_is_corrected_once(user, monkeypatch):
    verify("experience", "skills")
    job_id = add_job(user)
    answers = []
    real = LLMClient._mock_tailoring

    def tailor(user_text):
        answers.append(user_text)
        return RANKER_SHAPED if len(answers) == 1 else real(user_text)
    monkeypatch.setattr(LLMClient, "_mock_tailoring", staticmethod(tailor))
    assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    assert "SCHEMA CORRECTION" not in answers[0] and "SCHEMA CORRECTION" in answers[1]
    assert "candidate_fit_score" in answers[1].split("SCHEMA CORRECTION", 1)[1]  # it says what was wrong
    first, second = logs(user, "tailoring")[-2:]
    assert (first.status, first.error_type, first.validation_failure_reason, first.structured_output_valid) == (
        "FAILED", "SCHEMA_VALIDATION", "POSSIBLE_CROSS_WORKER_OUTPUT", False)
    assert (second.status, second.structured_output_valid, second.structured_output_retry_count) == (
        "SUCCEEDED", True, 1)
    assert first.schema_enforcement == "prompt_only" and first.context_builder_version == "v2"


def test_a_second_wrong_answer_fails_and_nothing_is_stored(user, monkeypatch):
    verify("experience", "skills")
    job_id = add_job(user)
    monkeypatch.setattr(LLMClient, "_mock_tailoring", staticmethod(lambda user_text: RANKER_SHAPED))
    response = client.post(f"/api/jobs/{job_id}/tailor")
    assert response.status_code == 400 and "POSSIBLE_CROSS_WORKER_OUTPUT" in response.json()["detail"]
    assert "looks like RANKER output" in response.json()["detail"]
    assert len(logs(user, "tailoring")) == 2  # the answer, one correction, no more
    with session(user) as db:
        assert db.scalars(select(ResumeVariant).where(ResumeVariant.job_id == job_id)).all() == []


def test_native_json_schema_goes_only_to_models_known_to_support_it():
    models = {"strict": {"supports_structured_output": True}, "unknown": {},
              "plain": {"supports_structured_output": False}}

    def worker(model):
        return model_resolution.resolve({"models": models, "workers": {"TAILOR": {
            "mode": "FIXED", "primary_model": model}}}, "TAILOR")
    params, _ = model_resolution.request_params(worker("strict"), "strict", TailoringResult)
    assert params["response_format"]["json_schema"]["name"] == "TailoringResult"
    assert model_resolution.schema_enforcement(params) == "native_schema"
    params, _ = model_resolution.request_params(worker("unknown"), "unknown", TailoringResult)
    assert params["response_format"] == {"type": "json_object"} and model_resolution.schema_enforcement(params) == "json_mode"
    params, withheld = model_resolution.request_params(worker("plain"), "plain", TailoringResult)
    assert "response_format" not in params and withheld == ["response_format"]
    assert model_resolution.schema_enforcement(params) == "prompt_only"


# ───────────── context: only what the task needs ─────────────

def test_search_configuration_never_reaches_a_prompt(user):
    verify("experience", "skills")
    job_id = add_job(user)
    set_fit(user, job_id, COMPLETED)
    with session(user) as db:
        profile = profile_for(db)
        profile.search_preferences = {"primary_skills": ["Kafka"], "search_profile": {
            "sites": ["linkedin"], "hours_old": 72, "max_results": 50, "actor_id": "apify/private-actor"}}
        db.commit()
    with session(user) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        prompts = [build_ranking_prompt(profile, job=job, context=cb.build_ranking_context(db, profile, job)),
                   build_tailoring_prompt(profile, job, cb.build_tailoring_context(db, profile, job)),
                   build_review_prompt(profile, job=job, context=cb.build_review_context(db, profile, job),
                                       tailored_resume="# Jane"),
                   build_cover_letter_prompt(cb.build_cover_letter_context(db, profile, job))]
        answer = cb.build_application_answer_context(db, profile, "Do you have Kafka experience?")
    for prompt in prompts:
        for secret in ("hours_old", "max_results", "apify/private-actor", "search_profile"):
            assert secret not in prompt.user, (prompt.name, secret)
    assert "Kafka" in json.dumps(prompts[0].user)  # the candidate's own preference survives
    assert "search_profile" not in json.dumps(answer.candidate)


def test_mismatched_evidence_is_flagged_and_held_back_but_never_rewritten(user):
    verify("experience", "skills")
    job_id = add_job(user)
    with session(user) as db:
        item = db.scalar(select(Evidence).where(Evidence.source == "resume",
                                                Evidence.claim.like("Reduced production incidents%")))
        item.company = "Globex"  # filed under the wrong employer
        db.commit()
        bad = item.id
    report = client.get("/api/evidence/diagnostics").json()
    assert [(i["evidence_id"], i["code"], i["source"]["company"]) for i in report["issues"]] == [
        (bad, "EVIDENCE_COMPANY_MISMATCH", "Zendrive")]
    assert report["held_back_from_generation"] == [bad]
    context = build(user, cb.build_tailoring_context, job_id)
    assert {"id": bad, "code": "EVIDENCE_COMPANY_MISMATCH"} in context.excluded_evidence
    assert bad not in context.evidence_ids
    assert bad not in {i for role in context.source_resume["roles"] for b in role["bullets"] for i in b["evidence_ids"]}
    with session(user) as db:
        assert db.get(Evidence, bad).company == "Globex", "diagnostics never correct candidate history"
    ranking = build(user, cb.build_ranking_context, job_id)
    assert ranking.excluded_evidence == []  # only generation holds it back


def test_review_notes_keep_the_fields_the_tailor_screen_reads(user):
    verify("experience", "skills")
    job_id = add_job(user)
    assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    with session(user) as db:
        variant = db.scalars(select(ResumeVariant).where(ResumeVariant.job_id == job_id)).one()
        notes, letter = variant.reviewer_notes, variant.content["cover_letter_generation"]
    assert (letter["prompt_version"], letter["provider"]) == (COVER_LETTER_PROMPT_VERSION, "mock")
    # The mock letter claims a 45% cut its evidence does not cite: recorded for the person who approves it.
    assert isinstance(letter["validation_issues"], list) and letter["evidence_ids"] == []
    assert notes["recommendation"] == "PASS" and notes["review_score"] == notes["score"] == 86
    assert notes["strengths"] and notes["improvements"] == ["Keep bullets concise"] and notes["reject_reasons"] == []
    assert (notes["provider"], notes["prompt_version"]) == ("mock", "v4")


# ───────────── secrets: redacted centrally, before anything is stored ─────────────

@pytest.mark.parametrize("name", ["user_api_key", "user_api_key_hash", "api_key", "x-api-key", "authorization",
                                  "cookie", "set-cookie", "password", "database_url", "DATABASE_URL",
                                  "openai_api_key", "litellm_master_key", "refresh_token", "token", "client_secret"])
def test_credential_field_names_are_recognised(name):
    assert is_secret_field(name)


@pytest.mark.parametrize("name", ["input_tokens", "max_tokens", "total_tokens", "prompt_tokens_details", "model",
                                  "litellm_call_id", "keywords", "api_base"])
def test_ordinary_fields_stay_visible(name):
    assert not is_secret_field(name)


def test_free_text_and_nested_values_are_redacted():
    leaked = ('{"user_api_key": "sk-abcdef1234567890", "user_api_key_hash": "88dc28d0f030c55ed4ab77ed8faf0981", '
              '"input_tokens": 1200} postgresql+psycopg://jobhunt:hunter2secret@db:5432/jobhunt '
              "Authorization: Bearer abcdefghijklmnopqrst eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTI")
    clean = redact_text(leaked, 2000)
    for secret in ("sk-abcdef1234567890", "88dc28d0f030c55ed4ab77ed8faf0981", "hunter2secret", "abcdefghijklmnopqrst",
                   "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in clean, secret
    assert '"input_tokens": 1200' in clean and "db:5432/jobhunt" in clean
    nested = redact_value({"metadata": {"user_api_key": "x", "headers": {"set-cookie": "sid=1"}}, "max_tokens": 9,
                           "result": {"no_token": 3, "skipped": 1}})
    assert nested == {"metadata": {"user_api_key": "[redacted]", "headers": {"set-cookie": "[redacted]"}},
                      "max_tokens": 9, "result": {"no_token": 3, "skipped": 1}}  # a count is not a credential


def test_debug_prompt_text_is_redacted_before_it_is_stored(user, monkeypatch):
    from app.prompts import build_followup_prompt
    from app.services.llm import LLMCall

    monkeypatch.setattr(get_settings(), "llm_prompt_debug", True)
    prompt = build_followup_prompt(SimpleNamespace(name="Jane", headline=""), resume="key sk-live-abcdefghijklmnop99",
                                   job=SimpleNamespace(title="Lead", company="Globex", location=""))
    with session(user) as db:
        LLMClient.for_user(db).generate_prompt(prompt, LLMCall(workflow_type="system"))
    row = logs(user, "followup")[-1]
    assert row.debug_prompt and "sk-live-abcdefghijklmnop99" not in row.debug_prompt and "[redacted]" in row.debug_prompt
