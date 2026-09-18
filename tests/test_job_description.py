"""Structured job descriptions: parsing, requirement evidence, corrections, async tasks and job context."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Job, JobDescriptionParseTask, JobRequirementEvidence
from app.services import job_context as jc
from app.services import job_description as jd
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

JD_HTML = """<p>About Globex</p>
<p>We are a fast-growing data company. Our mission is to make fleets safer.</p>
<h3>Responsibilities</h3>
<ul><li>You will own the Kafka pipelines behind our analytics platform.</li>
<li>Partner with product to deliver real-time reporting.</li></ul>
<h3>Requirements</h3>
<ul><li>Strong experience with Python is required.</li>
<li>Minimum of 8 years building distributed systems.</li>
<li>Experience leading and mentoring engineers.</li>
<li>Bachelor&#39;s degree in Computer Science.</li>
<li>You must be based in Singapore and able to work onsite twice a week.</li></ul>
<h3>Nice to have</h3>
<ul><li>Familiarity with Kubernetes is a plus.</li>
<li>Exposure to Apache Flink would be great.</li></ul>
<p>Salary: $180,000 - $220,000 per year.</p>"""


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    return user_id


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def add_job(user_id, description=JD_HTML, **fields):
    with session(user_id) as db:
        job = Job(source="manual", source_ref=f"jd-{uuid.uuid4().hex[:8]}", title="Data Platform Lead",
                  company="Globex", location="Singapore", description=description, status="discovered", **fields)
        db.add(job)
        db.commit()
        return job.id


def parse(user_id, job_id):
    with session(user_id) as db:
        job = db.get(Job, job_id)
        jd.parse_now(db, job)
        db.commit()
        return job.structured_description


def structured(job_id):
    return client.get(f"/api/jobs/{job_id}/structured-description").json()


def requirements(job_id, **params):
    return client.get(f"/api/jobs/{job_id}/requirements", params=params).json()["items"]


# ───────────── normalization and raw preservation ─────────────

def test_the_raw_description_is_never_rewritten(user):
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        job = db.get(Job, job_id)
    assert job.description == JD_HTML, "the employer's text is kept exactly as it arrived"
    assert "<li>" not in job.normalized_description and "Bachelor's degree" in job.normalized_description
    assert job.normalized_description.startswith("About Globex")


def test_normalization_keeps_wording_and_finds_sections():
    text = jd.normalize_description(JD_HTML)
    assert "- Strong experience with Python is required." in text
    assert [heading for heading, _ in jd.sections_of(text)] == ["About Globex", "Responsibilities", "Requirements",
                                                                "Nice to have"]


# ───────────── parsing ─────────────

def test_required_and_preferred_requirements_are_kept_apart(user):
    job_id = add_job(user)
    parse(user, job_id)
    data = structured(job_id)
    assert data["status"] == "PARSED" and data["version"] == jd.PARSER_VERSION
    levels = {item["value"]: item["level"] for item in data["requirements"]}
    assert levels["Strong experience with Python is required."] == "REQUIRED"
    assert levels["Familiarity with Kubernetes is a plus."] == "PREFERRED"
    assert levels["Exposure to Apache Flink would be great."] == "PREFERRED"
    # Marketing copy asks nothing of the candidate.
    assert levels["We are a fast-growing data company."] == "INFORMATIONAL"
    assert data["counts"]["REQUIRED"] >= 4 and data["counts"]["PREFERRED"] == 2
    structured_description = data["structured_description"]
    assert any("Kubernetes" in skill for skill in structured_description["preferred_skills"])
    assert all("Kubernetes" not in skill for skill in structured_description["required_skills"])


def test_responsibilities_experience_leadership_and_education_are_extracted(user):
    job_id = add_job(user)
    data = parse(user, job_id)
    assert any("Kafka pipelines" in item for item in data["responsibilities"])
    assert data["experience_requirements"]["minimum_years"] == 8
    assert data["experience_requirements"]["maximum_years"] is None
    assert data["experience_requirements"]["leadership_required"] is True
    assert any("mentoring" in item for item in data["leadership_expectations"])
    assert any("Computer Science" in item for item in data["education_requirements"])
    assert any("Singapore" in item for item in data["hard_requirements"])


def test_visa_and_sponsorship_stay_unknown_when_the_posting_says_nothing(user):
    data = parse(user, add_job(user))
    assert data["location_requirements"]["visa_sponsorship"] is None
    assert data["location_requirements"]["work_authorization_required"] is None


def test_compensation_comes_from_the_posting_and_the_board_never_overwrites_it(user):
    data = parse(user, add_job(user))
    assert data["compensation"] == {"min": 180000.0, "max": 220000.0, "currency": "USD", "period": "year",
                                    "source_text": "Salary: $180,000 - $220,000 per year.",
                                    "source": "job_description"}
    # A posting without a salary falls back to what the board reported, marked as such.
    board = add_job(user, description="Requirements\nStrong Python experience is required.",
                    salary_min=90000, salary_max=120000, salary_currency="SGD", salary_interval="year")
    data = parse(user, board)
    assert data["compensation"]["min"] == 90000 and data["compensation"]["source"] == "job_board"


def test_every_requirement_keeps_its_source_text_and_confidence(user):
    job_id = add_job(user)
    parse(user, job_id)
    for item in requirements(job_id):
        assert item["source_text"] and 0 <= item["confidence"] <= 1
    python = next(item for item in requirements(job_id) if "Python" in item["value"])
    assert python["source_text"] == "Strong experience with Python is required."
    assert python["source_section"] == "Requirements" and python["level"] == "REQUIRED"


def test_explicit_and_derived_are_stored_apart(user):
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        job = db.get(Job, job_id)
        db.add(JobRequirementEvidence(user_id=user, job_id=job.id, item_id="der_001", requirement_type="DOMAIN",
                                      normalized_value="Distributed systems interview", requirement_level="UNKNOWN",
                                      source_text="", confidence=0.4, is_explicit=False, is_derived=True))
        db.commit()
    assert all(not item["derived"] for item in requirements(job_id)), "derived signals are excluded by default"
    with_derived = requirements(job_id, include_derived=True)
    assert any(item["derived"] for item in with_derived)
    insights = client.get(f"/api/jobs/{job_id}/interview-insights").json()
    assert "not stated by the employer" in insights["derived_signals"]["label"]
    assert any(item["value"] == "Distributed systems interview"
               for item in insights["derived_signals"]["derived_requirements"])


def test_parser_output_is_validated_against_the_schema(user, monkeypatch):
    from pydantic import ValidationError

    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: {"structured_description": {"experience_requirements":
                                                                                    {"minimum_years": "many"}},
                                                         "requirements": "not a list"})
    job_id = add_job(user)
    with session(user) as db, pytest.raises(ValidationError):
        jd.parse_now(db, db.get(Job, job_id))
    # A long posting is trimmed to the schema's limits instead of failing validation.
    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: {"structured_description":
                                                         {"required_skills": [f"Skill {i}" for i in range(200)]},
                                                         "requirements": []})
    with session(user) as db:
        parsed = jd.parse_now(db, db.get(Job, job_id))
        assert len(parsed.structured_description.required_skills) == 60


# ───────────── the async task ─────────────

def claim_for(job_id):
    """Claim this job's task. The test database holds other accounts' tasks, so the batch is filtered to ours."""
    with SessionLocal() as db:
        mine = db.execute(JobDescriptionParseTask.__table__.update()
                          .where(JobDescriptionParseTask.job_id == job_id).values(next_attempt_at=None))
        db.commit()
        assert mine.rowcount, "nothing queued for this job"
        task_id = db.scalar(select(JobDescriptionParseTask.id).where(JobDescriptionParseTask.job_id == job_id))
        return task_id if task_id in jd.claim(db, worker_id="test-worker", limit=200) else None


def test_a_new_job_is_queued_once_and_parsed_by_the_worker(user):
    from app.services.sources import upsert_job

    source_ref = uuid.uuid4().hex
    with session(user) as db:
        job, created = upsert_job(db, source="test", source_ref=source_ref, title="Data Platform Lead",
                                  company="Globex", description=JD_HTML)
        job_id = job.id
        assert created
        # Upserting the same posting again never queues a second task.
        upsert_job(db, source="test", source_ref=source_ref, title="Data Platform Lead", company="Globex",
                   description=JD_HTML)
        tasks = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id)).all()
        assert len(tasks) == 1 and tasks[0].status == "PENDING"

    task_id = claim_for(job_id)
    assert task_id is not None
    assert jd.process(SessionLocal, task_id, "test-worker") == "parsed"
    with session(user) as db:
        job = db.get(Job, job_id)
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id))
    assert job.structured_description_status == "PARSED" and job.structured_description
    assert task.status == "COMPLETED" and task.completed_at and task.last_error is None
    # Nothing is queued again while the description is unchanged.
    with session(user) as db:
        assert jd.queue_parse(db, job) is None or db.scalar(
            select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id,
                                                  JobDescriptionParseTask.status == "PENDING")) is None


def test_a_failing_parse_retries_and_then_gives_up_without_blocking_the_job(user, monkeypatch):
    from app.services.llm import LLMClient, LLMTimeoutError

    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: (_ for _ in ()).throw(LLMTimeoutError("provider timed out")))
    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
    outcomes = []
    for _ in range(get_settings().jd_parse_max_attempts):
        task_id = claim_for(job_id)
        outcomes.append(jd.process(SessionLocal, task_id, "test-worker"))
    assert outcomes == ["retrying"] * (len(outcomes) - 1) + ["failed"]
    with session(user) as db:
        job, task = db.get(Job, job_id), db.scalar(select(JobDescriptionParseTask)
                                                   .where(JobDescriptionParseTask.job_id == job_id))
        assert task.status == "FAILED" and "timed out" in task.last_error
        assert job.structured_description_status == "NEEDS_REVIEW"
        # The pipeline is never held up by a posting that cannot be parsed.
        assert jd.waiting_reason(db, job) is None


def test_ranking_waits_only_while_a_parse_is_still_possible(user):
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        assert jd.waiting_reason(db, job) is None, "nothing queued yet: ranking goes ahead"
        jd.queue_parse(db, job)
        db.commit()
        assert jd.waiting_reason(db, job) == "The job description is still being parsed"
        jd.parse_now(db, job)
        db.commit()
        assert jd.waiting_reason(db, job) is None


def test_existing_jobs_are_picked_up_in_batches(user):
    """The backlog is queued a batch at a time, once per job."""
    ids = [add_job(user) for _ in range(3)]
    with SessionLocal() as db:
        first = jd.sweep(db, limit=2)["queued"]
        second = jd.sweep(db, limit=len(ids) + 20)["queued"]
        assert first == 2 and second >= 1
        counts = {job_id: db.scalar(select(JobDescriptionParseTask.id).where(
            JobDescriptionParseTask.job_id == job_id)) for job_id in ids}
        assert all(task is not None for task in counts.values()), "every backlog job is queued"
        tasks = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id.in_(ids))).all()
        assert len(tasks) == len(ids), "and only once each"
        assert jd.sweep(db, limit=len(ids) + 20)["queued"] == 0


# ───────────── manual correction, re-parse and history ─────────────

def correct(job_id, mutate):
    data = structured(job_id)["structured_description"]
    mutate(data)
    return client.put(f"/api/jobs/{job_id}/structured-description", json={"structured_description": data})


def test_manual_corrections_are_recorded_and_survive_a_reparse(user):
    job_id = add_job(user)
    parse(user, job_id)

    out = correct(job_id, lambda data: data["job_identity"].update(seniority="Staff")).json()
    assert out["status"] == "MANUALLY_CORRECTED"
    assert out["structured_description"]["job_identity"]["seniority"] == "Staff"
    history = client.get(f"/api/jobs/{job_id}/structured-description/history").json()["items"]
    assert history[0]["field_path"] == "job_identity.seniority" and history[0]["change_source"] == "USER"
    assert history[0]["new_value"] == "Staff"

    # A re-parse of a corrected job proposes changes instead of overwriting them.
    reparsed = client.post(f"/api/jobs/{job_id}/structured-description/reparse").json()
    assert reparsed["applied"] is False
    assert reparsed["structured_description"]["job_identity"]["seniority"] == "Staff"
    change = next(c for c in reparsed["changes"] if c["path"] == "job_identity.seniority")
    assert change["current"] == "Staff" and change["corrected"] is True

    kept = client.post(f"/api/jobs/{job_id}/structured-description/reparse/resolve", json={"decisions": {}}).json()
    assert kept["structured_description"]["job_identity"]["seniority"] == "Staff" and kept["accepted"] == 0
    assert kept["candidate"] is None

    client.post(f"/api/jobs/{job_id}/structured-description/reparse")
    change = next(c for c in structured(job_id)["candidate"]["changes"] if c["path"] == "job_identity.seniority")
    used = client.post(f"/api/jobs/{job_id}/structured-description/reparse/resolve",
                       json={"decisions": {change["id"]: "use_new"}}).json()
    assert used["structured_description"]["job_identity"]["seniority"] != "Staff" and used["accepted"] == 1
    sources = {item["change_source"] for item in
               client.get(f"/api/jobs/{job_id}/structured-description/history").json()["items"]}
    assert {"PARSER", "USER"} <= sources


def test_a_reparse_of_an_uncorrected_job_simply_replaces_it(user):
    job_id = add_job(user)
    parse(user, job_id)
    out = client.post(f"/api/jobs/{job_id}/structured-description/reparse").json()
    assert out["applied"] is True and out["status"] == "PARSED" and out["candidate"] is None


def test_structured_job_data_belongs_to_its_owner(user, monkeypatch):
    job_id = add_job(user)
    parse(user, job_id)
    other = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(other, "other@jobhunt.test", False))
    assert client.get(f"/api/jobs/{job_id}/structured-description").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/structured-description/reparse").status_code == 404


# ───────────── job context builders ─────────────

def contexts(user_id, job_id):
    with session(user_id) as db:
        job = db.get(Job, job_id)
        return {"ranking": jc.build_ranking_job_context(db, job),
                "tailoring": jc.build_tailoring_job_context(db, job),
                "review": jc.build_review_job_context(db, job),
                "insights": jc.build_interview_insights_job_context(db, job)}


def test_context_builders_select_only_what_each_task_needs(user):
    job_id = add_job(user)
    parse(user, job_id)
    built = contexts(user, job_id)

    ranking = built["ranking"]
    assert ranking.structured and ranking.identity["title"] == "Data Platform Lead"
    assert [item["level"] for item in ranking.required] == ["REQUIRED"] * len(ranking.required)
    assert [item["level"] for item in ranking.preferred] == ["PREFERRED"] * len(ranking.preferred)
    assert ranking.raw_description == "", "a parsed posting is never sent as raw text"
    assert ranking.source_evidence and all(item["source_text"] for item in ranking.source_evidence)

    tailoring = built["tailoring"]
    assert len(tailoring.required) <= jc.TAILOR_REQUIREMENTS and not tailoring.source_evidence

    review = built["review"]
    assert all(item.get("source_text") is not None for item in review.source_evidence)

    insights = built["insights"]
    assert insights.derived_signals["label"] == jc.DERIVED_LABEL


def test_an_unparsed_posting_still_reaches_the_task_as_raw_text(user):
    job_id = add_job(user)
    ranking = contexts(user, job_id)["ranking"]
    assert not ranking.structured and "Kafka pipelines" in ranking.raw_description


def test_candidate_matches_and_gaps_are_computed_from_verified_facts(user):
    job_id = add_job(user)
    parse(user, job_id)
    requirement_list = [{"id": "req_1", "value": "Strong experience with Python is required.", "level": "REQUIRED"},
                        {"id": "req_2", "value": "Familiarity with Rust is a plus.", "level": "PREFERRED"}]
    matches, gaps = jc.candidate_match(requirement_list, "Built Python services and Kafka pipelines at scale")
    assert [item["id"] for item in matches] == ["req_1"] and [item["id"] for item in gaps] == ["req_2"]


def test_job_context_reduction_is_measured(user):
    job_id = add_job(user, description=JD_HTML + "\n" + "Extra paragraph about our benefits. " * 200)
    parse(user, job_id)
    stats = contexts(user, job_id)["tailoring"].stats
    assert stats.raw_job_tokens > stats.selected_job_context_tokens > 0
    assert stats.job_context_reduction_percent > 50


def test_the_parser_prompt_lives_in_the_prompts_package():
    from pathlib import Path

    from app.prompts import PROMPT_VERSIONS
    from app.prompts.job_description_parser import (
        JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
        build_job_description_parser_prompt,
    )

    root = Path(__file__).resolve().parents[1]
    assert (root / "app" / "prompts" / "job_description_parser.py").is_file()
    assert PROMPT_VERSIONS["job_description_parser"] == JOB_DESCRIPTION_PARSER_PROMPT_VERSION
    source = (root / "app" / "services" / "job_description.py").read_text()
    assert "You convert one job posting" not in source, "the prompt text lives in app/prompts, not in the service"

    class FakeJob:
        id, title, company, location = 1, "Data Platform Lead", "Globex", "Singapore"
        remote_type = job_type = seniority = experience = None
        salary_min = salary_max = salary_currency = salary_interval = None

    prompt = build_job_description_parser_prompt(FakeJob(), "Strong Python experience is required.", ["Requirements"])
    for rule in ("REQUIRED", "PREFERRED", "source_text", "is_derived", "Never guess salary", "derived_signals"):
        assert rule in prompt.system, rule
    assert "<untrusted_job_description>" in prompt.user and "Data Platform Lead" in prompt.user


# ───────────── what the prompts receive ─────────────

def test_prompts_get_requirements_instead_of_the_whole_advert(user):
    import io

    from app.prompts import build_ranking_prompt, build_review_prompt, build_tailoring_prompt
    from app.services import context_builder as cb
    from app.services.user_settings import profile_for

    client.post("/api/profile/resume", files={"file": ("r.txt", io.BytesIO(
        b"# Jane Doe\n\n## Skills\n- Languages: Python\n\n## Experience\n"
        b"### Lead Engineer | Zendrive | Remote | Jun 2024 - Oct 2025\n- Ran Kafka pipelines in production\n"),
        "text/plain")})
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        ranking = cb.build_ranking_context(db, profile, job)
        tailoring_context = cb.build_tailoring_context(db, profile, job)
        review = cb.build_review_context(db, profile, job)
        db.commit()
        prompts = {"ranking": build_ranking_prompt(candidate_profile=profile, job=job, context=ranking),
                   "tailoring": build_tailoring_prompt(candidate_profile=profile, job=job, context=tailoring_context),
                   "review": build_review_prompt(candidate_profile=profile, job=job, context=review,
                                                 tailored_resume="# Jane Doe")}
    for name, prompt in prompts.items():
        assert "JOB REQUIREMENTS:" in prompt.user, name
        assert "<untrusted_job_description>" not in prompt.user, f"{name} no longer repeats the whole advert"
        assert "Strong experience with Python is required." in prompt.user, name
        assert "REQUIRED" in prompt.system and "PREFERRED" in prompt.system, name
    # The reviewer can quote the employer; ranking sees the hard requirements it may call fatal.
    assert "source_text" in prompts["review"].user
    assert ranking.job_context["candidate_matches"] or ranking.job_context["candidate_gaps"]
    # Both sides of context selection travel with the call.
    assert set(ranking.call_stats()) == {"full_available_context_tokens", "selected_context_tokens",
                                         "context_reduction_percent", "raw_job_tokens",
                                         "selected_job_context_tokens", "job_context_reduction_percent"}


def test_an_unparsed_posting_still_sends_the_description(user):
    from app.prompts import build_ranking_prompt
    from app.services import context_builder as cb
    from app.services.user_settings import profile_for

    job_id = add_job(user)
    with session(user) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        context = cb.build_ranking_context(db, profile, job)
        db.commit()
        prompt = build_ranking_prompt(candidate_profile=profile, job=job, context=context)
    assert "<untrusted_job_description>" in prompt.user and "Kafka pipelines" in prompt.user
