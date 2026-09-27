"""Task-specific LLM context: relevant facts only, verified facts for final content, and measured token reduction."""
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Evidence, Job, LLMRequestLog
from app.prompts import build_ranking_prompt, build_review_prompt, build_tailoring_prompt
from app.services import context_builder as cb
from app.services import llm_telemetry, monitoring
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

RESUME = """# Jane Doe
Lead Engineer

## Summary
Engineering leader building AI and data platforms.

## Skills
- Languages: Python, Go
- Platforms: Kafka, Kubernetes, AWS, React

## Experience
### Lead Engineer | Zendrive | Bengaluru | Jun 2024 - Oct 2025
- Reduced production incidents by 45% across Kafka pipelines
- Led a team of 8 engineers on the data platform
- Organised the office book club

### Frontend Developer | Pixel Co | Pune | Jan 2016 - Dec 2018
- Built React dashboards for retail analytics
- Improved page load time by 30%

## Education
B.Tech Computer Science, IIT Delhi, 2012 - 2016
"""
JOB_DESCRIPTION = ("We need a data platform lead to own Kafka pipelines, production reliability and incident "
                   "reduction, and to lead and mentor a team of engineers. Python required.")


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    client.post("/api/profile/resume", files={"file": ("jane.txt", io.BytesIO(RESUME.encode()), "text/plain")})
    return user_id


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def resume_id():
    return client.get("/api/settings/resume").json()["active"]["id"]


def verify(*sections):
    for section in sections:
        client.post(f"/api/resumes/{resume_id()}/verify-section", json={"section": section})


def add_job(user_id, description=JOB_DESCRIPTION, title="Data Platform Lead"):
    with session(user_id) as db:
        job = Job(source="manual", source_ref=f"ctx-{uuid.uuid4().hex[:8]}", title=title, company="Globex",
                  description=description, status="shortlisted")
        db.add(job)
        db.commit()
        return job.id


def build(user_id, builder, job_id, **kwargs):
    with session(user_id) as db:
        context = builder(db, profile_for(db), db.get(Job, job_id), **kwargs)
        db.commit()
        return context


def bullets(context):
    return [b["text"] for role in context.relevant_experience for b in role["bullets"]]


# ───────────── ranking ─────────────

def test_ranking_context_selects_relevant_experience_skills_and_marks_unverified_facts(user):
    job_id = add_job(user)
    context = build(user, cb.build_ranking_context, job_id)
    # Nothing is verified: every parsed fact is lower-confidence context, kept apart from verified experience.
    assert bullets(context) == []
    unverified = [f["text"] for f in context.unverified_facts]
    assert "Reduced production incidents by 45% across Kafka pipelines" in unverified
    assert "Organised the office book club" not in unverified  # irrelevant to the posting
    assert all(role["verified"] is False for role in context.relevant_experience)
    assert context.relevant_skills[:2] == ["Python", "Kafka"]
    prompt = build_ranking_prompt(candidate_profile=profile_for_user(user), job=job(user, job_id), context=context)
    assert "UNVERIFIED RESUME FACTS (untrusted data):" in prompt.user and "lower-confidence" in prompt.system
    assert "book club" not in prompt.user

    verify("experience", "summary", "skills")
    context = build(user, cb.build_ranking_context, job_id)
    assert "Led a team of 8 engineers on the data platform" in bullets(context)
    assert context.unverified_facts == [] and context.candidate["summary"]


def test_ranking_can_ignore_unverified_facts_by_policy(user):
    client.put("/api/settings/resume/policy", json={"ranking_uses_unverified": False})
    context = build(user, cb.build_ranking_context, add_job(user))
    assert context.unverified_facts == [] and context.relevant_experience == []


def profile_for_user(user_id):
    with session(user_id) as db:
        profile = profile_for(db)
        db.expunge(profile)
        return profile


def job(user_id, job_id):
    with session(user_id) as db:
        row = db.get(Job, job_id)
        db.expunge(row)
        return row


def test_relevant_evidence_is_filtered_to_the_job(user):
    with session(user) as db:
        db.add_all([Evidence(claim="Cut Kafka pipeline incidents by half", company="Zendrive", skills=["Kafka"]),
                    Evidence(claim="Won a baking contest", company="Home", skills=["Baking"]),
                    Evidence(claim="Unverified Kafka claim", company="Zendrive", verified=False)])
        db.commit()
    context = build(user, cb.build_ranking_context, add_job(user))
    assert [e["claim"] for e in context.relevant_evidence] == ["Cut Kafka pipeline incidents by half"]


def test_context_token_reduction_is_measured_and_recorded(user, monkeypatch):
    job_id = add_job(user)
    long_resume = RESUME + "\n## Additional Information\n" + "\n".join(f"- Unrelated hobby number {i} " * 3 for i in range(300))
    client.patch("/api/profile", json={"master_resume": long_resume})
    verify("experience")
    context = build(user, cb.build_tailoring_context, job_id)
    stats = context.stats
    assert stats.full_available_context_tokens > stats.selected_context_tokens > 0
    assert stats.context_reduction_percent == pytest.approx(
        100 * (stats.full_available_context_tokens - stats.selected_context_tokens) / stats.full_available_context_tokens, abs=0.1)
    assert stats.context_reduction_percent > 50

    monkeypatch.setattr(get_settings(), "llm_telemetry_enabled", True)
    llm_telemetry.use_sessions(SessionLocal)
    try:
        assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    finally:
        llm_telemetry.use_sessions(None)
    with SessionLocal() as db:
        logs = db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == user)).all()
    tailor = next(r for r in logs if r.stage == "TAILOR")
    assert tailor.selected_context_tokens and tailor.full_available_context_tokens > tailor.selected_context_tokens
    assert tailor.context_reduction_percent > 0
    with SessionLocal() as db:
        rows = monitoring.rows(db, monitoring.MonitoringFilter(*monitoring.time_range("today"), user_id=user))
    selection = monitoring.context(rows)["selection"]
    assert selection["requests"] >= 2 and {s["stage"] for s in selection["by_stage"]} >= {"TAILOR", "REVIEWER"}


# ───────────── tailoring and review: verified facts only ─────────────

@pytest.mark.strict_resume_policy
def test_tailoring_is_blocked_without_verified_facts(user):
    job_id = add_job(user)
    with pytest.raises(cb.UnverifiedResumeError):
        build(user, cb.build_tailoring_context, job_id)
    out = client.post(f"/api/jobs/{job_id}/tailor")
    assert out.status_code == 400 and "Resume Data" in out.json()["detail"]
    client.put("/api/settings/resume/policy", json={"allow_unverified_resume_facts": True})
    context = build(user, cb.build_tailoring_context, job_id)
    assert any(b["type"] == "unverified" for role in context.relevant_experience for b in role["bullets"])


@pytest.mark.strict_resume_policy
def test_tailoring_context_holds_verified_relevant_facts_and_fit_analysis(user):
    job_id = add_job(user)
    with session(user) as db:
        db.get(Job, job_id).fit_analysis = {"resume_gaps": ["Reliability impact is not quantified"]}
        db.commit()
    rid = resume_id()
    detail = client.get(f"/api/resumes/{rid}/structured").json()
    zendrive = detail["structured"]["experience"][0]
    for item in (zendrive["id"], zendrive["achievements"][0]["id"]):
        client.post(f"/api/resumes/{rid}/verify-item", json={"item_id": item})
    rejected = next(b for b in zendrive["achievements"] + zendrive["responsibilities"] if "team of 8" in b["text"])
    client.post(f"/api/resumes/{rid}/verify-item", json={"item_id": rejected["id"], "status": "REJECTED"})

    context = build(user, cb.build_tailoring_context, job_id)
    assert bullets(context) == ["Reduced production incidents by 45% across Kafka pipelines"]
    assert context.unverified_facts == [] and context.fit_analysis["resume_gaps"]
    assert [r["role_index"] for r in context.roles] == [0] and context.roles[0]["bullets"] == bullets(context)
    prompt = build_tailoring_prompt(candidate_profile=profile_for_user(user), job=job(user, job_id), context=context)
    assert "team of 8" not in prompt.user and "React dashboards" not in prompt.user
    assert "SOURCE RESUME:" in prompt.user and "Reliability impact" in prompt.user
    source = context.source_resume["roles"][0]["bullets"]
    assert [b["text"] for b in source] == bullets(context)
    assert all(isinstance(b.get("id"), int) for b in source), "a verified bullet carries one id: its evidence row"
    assert set(source[0]) == {"id", "text"}

    review = build(user, cb.build_review_context, job_id)
    assert {f["text"] for f in review.rejected_facts} == {rejected["text"]}
    prompt = build_review_prompt(candidate_profile=profile_for_user(user), job=job(user, job_id), context=review,
                                 tailored_resume="# Jane")
    assert "REJECTED RESUME FACTS" in prompt.user and "CANDIDATE POLICY" in prompt.user and "# Jane" in prompt.user


@pytest.mark.strict_resume_policy
def test_final_application_content_uses_only_verified_facts(user, monkeypatch):
    job_id = add_job(user)
    verify("experience")
    rid = resume_id()
    detail = client.get(f"/api/resumes/{rid}/structured").json()
    book_club = next(b for b in detail["structured"]["experience"][0]["responsibilities"] if "book club" in b["text"])
    client.post(f"/api/resumes/{rid}/unverify-item", json={"item_id": book_club["id"]})
    with session(user) as db:
        db.add(Evidence(claim="Kafka claim nobody verified", company="Zendrive", verified=False))
        db.commit()

    seen = {}
    from app.services.llm import LLMClient
    real = LLMClient.generate_prompt

    def spy(self, prompt, call=None):
        seen[prompt.name] = prompt.user
        return real(self, prompt, call)
    monkeypatch.setattr(LLMClient, "generate_prompt", spy)
    assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    for name in ("tailoring", "review"):
        assert "book club" not in seen[name] and "nobody verified" not in seen[name], name
    assert "Reduced production incidents" in seen["tailoring"]


def test_the_tailoring_context_is_checked_before_a_prompt_exists():
    context = cb.TaskContext(task="tailoring", candidate={}, resume={"id": 1}, relevant_evidence=[{"id": 7}],
                             roles=[{"role_index": 0, "bullets": ["x"]}, {"role_index": 0, "bullets": ["y"]}])
    with pytest.raises(cb.ContextError, match="TAILOR_CONTEXT_INVALID"):
        cb.validate_tailoring_context(context, master_role_count=2)  # two roles mapped onto one
    context.roles = [{"role_index": 3, "bullets": ["x"]}]
    with pytest.raises(cb.ContextError, match="TAILOR_CONTEXT_INVALID"):
        cb.validate_tailoring_context(context, master_role_count=2)  # a role the master resume does not have
    context.roles, context.relevant_evidence = [{"role_index": 0, "bullets": ["x"]}], [{"id": 7}, {"id": 7}]
    with pytest.raises(cb.ContextError, match="TAILOR_CONTEXT_INVALID"):
        cb.validate_tailoring_context(context, master_role_count=2)
    with pytest.raises(cb.ContextError, match="TAILOR_CONTEXT_INCOMPLETE"):
        cb.validate_tailoring_context(cb.TaskContext(task="tailoring", candidate={}, resume={}), master_role_count=0)


def test_prompts_are_loaded_from_the_prompts_package():
    root = Path(__file__).resolve().parents[1]
    for module in ("ranking", "tailoring", "review", "followup", "resume_parser"):
        assert (root / "app" / "prompts" / f"{module}.py").is_file()
    source = (root / "app" / "services" / "context_builder.py").read_text()
    assert "from app.prompts" in source and "SYSTEM" not in source


# ───────────── cover letter ─────────────

def add_knowledge(user_id, rows):
    from app.models import KnowledgeAnswer
    with session(user_id) as db:
        for question, answer, extra in rows:
            db.add(KnowledgeAnswer(question=question, question_key=question.lower()[:64], answer=answer, **extra))
        db.commit()


@pytest.mark.strict_resume_policy
def test_cover_letter_context_holds_the_posting_company_facts_and_answers_it_raises(user):
    job_id = add_job(user, JOB_DESCRIPTION + " You must already hold work authorization; tell us your notice period.")
    verify("experience", "summary", "skills")
    add_knowledge(user, [
        ("What is your notice period?", "30 days", {"verified": True, "category": "availability"}),
        ("Do you need visa sponsorship?", "No, I am an Indian citizen", {"verified": True, "category": "authorization"}),
        ("What is your expected salary?", "INR 90 lakh", {"verified": True, "sensitive": True, "category": "comp"}),
        ("Favourite editor?", "Neovim", {"verified": True, "category": "other"}),
        ("Unverified note about Kafka", "Something unconfirmed", {"verified": False, "category": "other"}),
    ])
    context = build(user, cb.build_cover_letter_context, job_id)
    questions = [k["question"] for k in context.knowledge]
    assert "What is your notice period?" in questions and "Do you need visa sponsorship?" in questions
    # Sensitive and unverified answers never reach a letter, and irrelevant ones are left out.
    assert all("salary" not in q.lower() and "editor" not in q.lower() and "Unverified" not in q for q in questions)
    assert context.job["company"] == "Globex" and context.job["description"]
    assert bullets(context) and context.candidate["name"]

    from app.prompts import build_cover_letter_prompt
    prompt = build_cover_letter_prompt(context)
    assert "30 days" in prompt.user and "INR 90 lakh" not in prompt.user
    assert "Dear Hiring Team" in prompt.system and "first person" in prompt.system


@pytest.mark.strict_resume_policy
def test_a_cover_letter_needs_verified_facts_too(user):
    with pytest.raises(cb.UnverifiedResumeError):
        build(user, cb.build_cover_letter_context, add_job(user))


def test_tailoring_writes_the_letter_from_its_own_prompt(user, monkeypatch):
    job_id = add_job(user)
    verify("experience")
    seen = {}
    from app.services.llm import LLMClient
    real = LLMClient.generate_prompt

    def spy(self, prompt, call=None):
        seen[prompt.name] = prompt
        return real(self, prompt, call)
    monkeypatch.setattr(LLMClient, "generate_prompt", spy)
    out = client.post(f"/api/jobs/{job_id}/tailor")
    assert out.status_code == 200
    letter = out.json()["cover_letter"]
    assert letter.startswith("Dear Hiring Team,") and "Globex" in letter and "Sincerely," in letter
    assert "cover_letter" in seen and seen["cover_letter"].marker == "COVER_LETTER"
    # The resume call no longer writes a letter of its own.
    assert "cover_letter (string)" not in seen["tailoring"].system
