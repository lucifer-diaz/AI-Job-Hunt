"""Application-form questions on Job Insights: pasted questions drafted in the background, predicted ones drafted in a
research run, the person's library answers winning, personal questions never drafted, grounding checks, the last good
drafts kept, tenancy and the API. The model is the no-cost mock provider or a stub returning a crafted answer."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Evidence, JobInsight, KnowledgeAnswer
from app.prompts.contracts import ApplicationFormPrepResult
from app.services.job_insights import application_form, provenance, service, synthesis
from app.services.knowledge import question_key
from app.services.llm import LLMClient, LLMResult
from tests.conftest import make_user
from tests.test_job_insights import (  # noqa: F401  fixtures are used by name
    FakeSource,
    insight_of,
    insights,
    new_job,
    owner,
    refresh,
    run_insights,
    signed_in,
    tasks_of,
)

MARK = "test:application-form"
NOTICE = "What is your notice period for Northwind?"


def _seed(owner: int) -> int:
    """A verified fact the posting shares words with, and a verified saved answer to one form question."""
    with SessionLocal() as db:
        db.execute(delete(Evidence).where(Evidence.context == MARK))
        db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.category == "test_form"))
        fact = Evidence(user_id=owner, company="Northwind Labs", role="Backend Engineer", context=MARK,
                        claim="Built reliable Python services for payments", verified=True)
        db.add(fact)
        db.add(KnowledgeAnswer(user_id=owner, question=NOTICE, question_key=question_key(NOTICE),
                               answer="One month", category="test_form", verified=True))
        db.commit()
        return fact.id


def _unseed() -> None:
    with SessionLocal() as db:
        db.execute(delete(Evidence).where(Evidence.context == MARK))
        db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.category == "test_form"))
        db.commit()


def _crafted(monkeypatch, items):
    """The model answers the application-form prompt with `items` (a function of the prompt's user text)."""
    real = LLMClient.generate_prompt

    def generate(self, prompt, call=None):
        if prompt.name != "application_form":
            return real(self, prompt, call)
        data = {"form_questions": items(prompt.user)}
        return LLMResult(data, "mock", "mock", validated=call.validate(data))
    monkeypatch.setattr(LLMClient, "generate_prompt", generate)


def by_question(job_id):
    return {q["question"]: q for q in insight_of(job_id).application_questions or []}


# ───────────── pasted questions ─────────────

def test_pasted_questions_are_drafted_in_the_background_without_research(owner, signed_in, monkeypatch):
    fact = _seed(owner)
    try:
        job_id = new_job(owner)
        run_insights()
        counted = FakeSource("glassdoor_interview")
        monkeypatch.setattr(service, "SOURCES", [counted])
        client = signed_in(owner)
        out = client.post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [
            "1. Why do you want to join Northwind Labs?", "Do you require visa sponsorship to work here?", NOTICE,
            "short"]}).json()
        assert out["added"] == 3, "list markers are stripped and fragments dropped"
        form = out["application_form"]
        assert form["pending"] and {q["status"] for q in form["from_form"]} == {"PENDING"}
        assert [t.scope for t in tasks_of(job_id)][-1] == "APPLICATION_FORM"
        assert out["task"]["scope"] == "APPLICATION_FORM"

        assert run_insights() == ["completed"]
        assert counted.calls == 0, "a form-only task never calls a research source"
        got = by_question(job_id)
        why = got["Why do you want to join Northwind Labs?"]
        assert why["status"] == "DRAFTED" and why["kind"] == "GENERATED" and why["evidence_ids"]
        with SessionLocal() as db:  # the mock drafts from the first evidence it is shown: verified, this account's
            cited = db.scalars(select(Evidence).where(Evidence.id.in_(why["evidence_ids"]))).all()
        assert all(e.verified and e.user_id == owner for e in cited) and fact
        assert why["answer"] and "source_ids" not in why
        visa = got["Do you require visa sponsorship to work here?"]
        assert visa["status"] == "NEEDS_INPUT" and visa["answer"] is None
        notice = got[NOTICE]
        assert notice["status"] == "LIBRARY" and notice["answer"] == "One month" and notice["kind"] == "FACT"
        with SessionLocal() as db:
            assert db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question.like("Why do you want%"))) \
                is None, "a draft is never saved as an answer"

        view = client.get(f"/api/jobs/{job_id}/insights").json()["application_form"]
        assert not view["pending"] and len(view["from_form"]) == 3
        assert client.post(f"/api/jobs/{job_id}/insights/application-questions",
                           json={"questions": [NOTICE]}).json()["added"] == 0, "the same question is not added twice"
    finally:
        _unseed()


def test_removing_a_pasted_question_removes_its_draft(owner, signed_in):
    job_id = new_job(owner)
    run_insights()
    client = signed_in(owner)
    client.post(f"/api/jobs/{job_id}/insights/application-questions",
                json={"questions": ["Tell us about a system you scaled."]})
    run_insights()
    qid = insight_of(job_id).application_form_input[0]["id"]
    assert client.delete(f"/api/jobs/{job_id}/insights/application-questions/{qid}").status_code == 200
    insight = insight_of(job_id)
    assert insight.application_form_input == [] and not any(q["id"] == qid for q in insight.application_questions)
    assert client.delete(f"/api/jobs/{job_id}/insights/application-questions/{qid}").status_code == 404


def test_another_account_cannot_add_questions_to_a_job(owner, signed_in):
    job_id = new_job(owner)
    other = make_user(SessionLocal)
    assert signed_in(other).post(f"/api/jobs/{job_id}/insights/application-questions",
                                 json={"questions": ["Why do you want to join?"]}).status_code == 404


def test_questions_pasted_during_a_full_run_are_drafted_afterwards(owner, monkeypatch):
    job_id = new_job(owner)
    real = synthesis.run

    def run_then_paste(*args, **kwargs):
        out = real(*args, **kwargs)
        with SessionLocal() as db:  # the person pastes while the run is past reading the form questions
            insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
            application_form.add_questions(insight, ["Describe a project you are proud of."], service.utcnow())
            db.commit()
        return out
    monkeypatch.setattr(synthesis, "run", run_then_paste)
    run_insights()
    assert tasks_of(job_id)[-1].scope == "APPLICATION_FORM" and tasks_of(job_id)[-1].status == "PENDING"
    monkeypatch.setattr(synthesis, "run", real)
    run_insights()
    assert "Describe a project you are proud of." in by_question(job_id)


# ───────────── predicted questions and the rules after the model ─────────────

def test_predicted_questions_follow_the_rules_whatever_the_model_says(owner, monkeypatch):
    fact = _seed(owner)
    try:
        def items(_user):
            return [
                {"form_question": "What are your salary expectations for this role?", "form_category": "OTHER",
                 "form_decision": "DRAFT", "form_answer": "Around 200k.", "evidence_ids": [fact]},
                {"form_question": "Describe your experience building reliable services.",
                 "form_category": "EXPERIENCE", "form_decision": "DRAFT",
                 "form_answer": "I built reliable Python services for payments.", "evidence_ids": [fact],
                 "likely_because": "The posting centres on reliable services."},
                {"form_question": "How large was the team you led?", "form_category": "LEADERSHIP",
                 "form_decision": "DRAFT", "form_answer": "I led 4173 engineers.", "evidence_ids": [fact]},
                {"form_question": "Tell us about your Kafka work.", "form_category": "TECHNICAL",
                 "form_decision": "DRAFT", "form_answer": "Kafka pipelines.", "evidence_ids": [987654321]},
                {"form_question": "What is your greatest strength?", "form_category": "BEHAVIORAL",
                 "form_decision": "DRAFT", "form_answer": "Calm under pressure."},
            ]
        _crafted(monkeypatch, items)
        job_id = new_job(owner)
        run_insights()
        got = by_question(job_id)
        salary = got["What are your salary expectations for this role?"]
        assert salary["status"] == "NEEDS_INPUT" and salary["answer"] is None, "pay is never drafted"
        good = got["Describe your experience building reliable services."]
        assert good["status"] == "DRAFTED" and good["origin"] == "PREDICTED" and good["rationale"]
        metric = got["How large was the team you led?"]
        assert metric["status"] == "NEEDS_INPUT" and "4173" in metric["reason"], "an invented number withholds it"
        assert "Tell us about your Kafka work." not in got, "an item citing an unknown id is dropped alone"
        uncited = got["What is your greatest strength?"]
        assert uncited["status"] == "NEEDS_INPUT", "a factual draft needs evidence"
        assert insight_of(job_id).prompt_versions["application_form"] == "v1"
    finally:
        _unseed()


def test_a_run_without_the_model_keeps_the_last_drafts(owner, monkeypatch):
    _seed(owner)
    try:
        job_id = new_job(owner)
        run_insights()
        before = insight_of(job_id).application_questions
        assert before, "the mock predicts at least one question"
        monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
        refresh(job_id)
        run_insights()
        assert insight_of(job_id).application_questions == before
    finally:
        _unseed()


# ───────────── units ─────────────

def test_the_contract_never_keeps_an_answer_outside_a_draft():
    out = ApplicationFormPrepResult.model_validate({"form_questions": [
        {"form_question": "Why do you want this job?", "form_decision": "DRAFT"},
        {"form_question": "Are you willing to relocate?", "form_decision": "NEEDS_INPUT", "form_answer": "Yes"}]})
    assert [(q.form_decision, q.form_answer) for q in out.form_questions] == [("NEEDS_INPUT", None),
                                                                                ("NEEDS_INPUT", None)]


def test_personal_questions_are_recognised_without_catching_ordinary_ones():
    assert application_form.personal("Do you now or in the future require sponsorship?")
    assert application_form.personal("What is your expected salary?")
    assert application_form.personal("When can you start?")
    assert application_form.personal("Anything", "DEMOGRAPHIC")
    assert not application_form.personal("How did you address a conflict in your team?")
    assert not application_form.personal("Describe a high availability system you built.")


def test_provenance_drops_a_draft_that_claims_a_research_source():
    sections = {"application_questions": [
        {"id": "f1", "status": "DRAFTED", "kind": "GENERATED", "answer": "x", "source_ids": [1]},
        {"id": "f2", "status": "DRAFTED", "kind": "GENERATED", "answer": "y"},
        {"id": "f3", "status": "NEEDS_INPUT", "kind": "UNKNOWN", "answer": "z"}]}
    out, report = provenance.enforce(sections, [])
    assert [q["id"] for q in out["application_questions"]] == ["f2"]
