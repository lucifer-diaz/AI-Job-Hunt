"""Discovery quality: jobs outside the candidate's domain are hidden at discovery (deterministic, no model), the
filter learns from what the person dismisses, dead postings are deleted and never imported again, and each job can
be ranked and tailored with its own resume.

Everything runs as a dedicated account, so the shared test account's profile is never changed."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.main  # noqa: F401  migrates the test database, as every API test does
from app import auth
from app.db import SessionLocal
from app.main import app as web_app
from app.models import (
    CandidateProfile,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobFeedbackEvent,
    JobRankingTask,
    RemovedPosting,
    Resume,
    ResumeVersion,
    SavedSearch,
    SearchRun,
    WorkflowTransition,
)
from app.services import domain_match, feedback_learning, job_removal, ranker, sources
from app.services import resume_verification as rv
from app.services.domain_match import DEFAULT_DOMAIN_EXCLUDES, DEFAULT_SKILL_EXCLUDES, Rules, assess
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from tests.conftest import make_user
from tests.jd_support import PostingServer

SOURCE = "test:discovery-quality"
POSTING = ("About the role\nWe are looking for a backend engineer to build Python services.\nResponsibilities\n"
           "- Design and build APIs\n- Own services in production\nRequirements\n- 5+ years of experience with "
           "Python and PostgreSQL\n- Experience with AWS\nNice to have: Kubernetes. Benefits: remote, learning "
           "budget, health insurance and a friendly team that collaborates across time zones.")
RESUME_A = "Asha Rao\nSenior Backend Engineer at Acme 2020-2025\n- Built Python services serving 2M requests a day\n"
RESUME_B = "Asha Rao\nMachine Learning Engineer at Globex 2019-2025\n- Trained ranking models in PyTorch\n"


# ───────────── the verdict itself (no database) ─────────────

RULES = Rules(domain_excludes=list(DEFAULT_DOMAIN_EXCLUDES), skill_excludes=list(DEFAULT_SKILL_EXCLUDES),
              roles=["senior backend engineer", "ai engineer"], skills=["python", "fastapi", "machine learning"])


@pytest.mark.parametrize("title,code", [
    ("Sales Manager", "DOMAIN"), ("Digital Marketing Lead", "DOMAIN"), ("Business Development Executive", "DOMAIN"),
    ("Senior Java Developer", "SKILL"), ("Frontend Engineer", "SKILL"), ("C++ Developer", "SKILL"),
    ("iOS Engineer", "SKILL"), ("QA Analyst", "SKILL"), (".NET Developer", "SKILL")])
def test_off_domain_titles_are_hidden(title, code):
    verdict = assess(Job(title=title, company="X", description=""), RULES)
    assert not verdict.match and verdict.code == code and verdict.reason


@pytest.mark.parametrize("title", ["Backend Engineer (Python)", "Senior AI Engineer", "Java / Python Backend Engineer",
                                   "Full Stack Engineer - JavaScript", "Staff Engineer", "Platform Engineer"])
def test_matching_or_unclear_titles_are_kept(title):
    assert assess(Job(title=title, company="X", description=""), RULES).match


def test_include_terms_always_win():
    rules = Rules(**{**RULES.__dict__, "includes": ["sales engineer"]})
    assert assess(Job(title="AI Sales Engineer", company="X", description=""), rules).match


def test_the_description_decides_only_a_title_that_says_nothing():
    java = "We build with Java, Spring and Angular. Experience with PHP is a plus."
    verdict = assess(Job(title="Software Engineer", company="X", description=java), RULES)
    assert not verdict.match and verdict.code == "DESCRIPTION"
    assert assess(Job(title="Software Engineer", company="X", description=java + " Python too."), RULES).match
    assert assess(Job(title="Backend Engineer", company="X", description=java), RULES).match, "a role word keeps it"


def test_a_disabled_filter_keeps_everything():
    rules = Rules(**{**RULES.__dict__, "enabled": False})
    assert assess(Job(title="Sales Manager", company="X", description=""), rules).match


def test_learned_rules_hide_similar_jobs():
    rules = Rules(**{**RULES.__dict__, "learned_titles": [{"term": "underwriter", "evidence": 4}],
                     "learned_companies": [{"term": "spammy staffing", "evidence": 3}]})
    verdict = assess(Job(title="Underwriter Intern", company="X", description=""), rules)
    assert not verdict.match and verdict.code == "LEARNED" and "underwriter" in verdict.reason
    assert assess(Job(title="Backend Engineer", company="Spammy Staffing", description=""), rules).code == "LEARNED"


def test_title_terms_skip_generic_words():
    assert feedback_learning.title_terms("Senior Claims Adjuster Intern - Remote") == [
        "claims", "adjuster", "claims adjuster"]


# ───────────── an account with a profile ─────────────

def _clear_orphans() -> None:
    """SQLite does not enforce cascades: task rows of jobs deleted by earlier tests would attach to a reused job id."""
    with SessionLocal() as db:
        for model in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, WorkflowTransition):
            db.execute(delete(model).where(model.job_id.not_in(select(Job.id))))
        for model in (ResumeVersion, Resume):
            db.execute(delete(model).where(model.candidate_profile_id.not_in(select(CandidateProfile.id))))
        db.commit()


@pytest.fixture
def user(monkeypatch):
    _clear_orphans()
    owner = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        profile = CandidateProfile(user_id=owner, name="Asha Rao", headline="Senior Backend Engineer",
                                   target_roles=["Backend Engineer", "AI Engineer"], master_resume=RESUME_A,
                                   search_preferences={"primary_skills": ["python"]})
        db.add(profile)
        db.commit()
    monkeypatch.setattr(auth, "authenticate", lambda request: auth.CurrentUser(owner, "dq@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    yield owner
    with SessionLocal() as db:
        ids = select(Job.id).where(Job.user_id == owner)
        for model in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, WorkflowTransition):
            db.execute(delete(model).where(model.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.user_id == owner))
        for model in (JobFeedbackEvent, RemovedPosting, ResumeVersion, Resume, SearchRun, SavedSearch, CandidateProfile):
            db.execute(delete(model).where(model.user_id == owner))
        db.commit()


def add(owner: int, title: str, description: str = POSTING, *, source: str = SOURCE, url: str = "",
        search_run_id: int | None = None, company: str | None = None) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        # A company of its own each time: the filter also learns companies the person keeps dismissing.
        job, _ = sources.upsert_job(db, source=source, source_ref=uuid.uuid4().hex, title=title,
                                    company=company or f"Company {uuid.uuid4().hex[:6]}",
                                    url=url, description=description, search_run_id=search_run_id)
        return job.id


def job_of(job_id: int) -> Job:
    with SessionLocal() as db:
        return db.get(Job, job_id)


def rows(model, job_id: int) -> list:
    with SessionLocal() as db:
        return list(db.scalars(select(model).where(model.job_id == job_id)).all())


@pytest.fixture
def client():
    return TestClient(web_app)


# ───────────── hidden at discovery ─────────────

def test_an_off_domain_job_is_hidden_and_costs_nothing(user):
    job_id = add(user, "Sales Manager")
    job = job_of(job_id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason == "Outside your domain: sales"
    assert job.state is S.SKIPPED
    assert rows(JobDescriptionParseTask, job_id) == [] and rows(JobDescriptionFetchTask, job_id) == []
    assert rows(JobRankingTask, job_id) == []
    with SessionLocal() as db:
        assert ranker.hard_filter_reason(db.get(Job, job_id), ranker.RankingPolicy(70, 0.6)) == job.domain_reason


def test_a_matching_job_goes_on_as_before(user):
    job_id = add(user, "Backend Engineer")
    assert job_of(job_id).domain_match == "MATCH" and len(rows(JobDescriptionParseTask, job_id)) == 1


def test_a_job_the_person_added_is_never_hidden(user):
    job_id = add(user, "Sales Manager", source="manual")
    assert job_of(job_id).domain_match == "KEPT" and len(rows(JobDescriptionParseTask, job_id)) == 1


def test_the_profile_decides_what_is_foreign(user):
    with SessionLocal() as db:
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        profile.target_roles = ["Sales Engineer"]
        profile.search_preferences = {"primary_skills": ["java"]}
        db.commit()
    assert job_of(add(user, "Solutions Sales Engineer")).domain_match == "MATCH", "their own role is not foreign"
    assert job_of(add(user, "Senior Java Developer")).domain_match == "MATCH", "their own skill is not foreign"


def test_discovery_lists_hidden_jobs_only_when_asked(user, client):
    hidden, shown = add(user, "Digital Marketing Lead"), add(user, "Backend Engineer")
    default = client.get("/api/jobs/search").json()
    assert shown in [i["id"] for i in default["items"]] and hidden not in [i["id"] for i in default["items"]]
    assert default["domain_counts"] == {"matching": 1, "not_matching": 1}
    only = client.get("/api/jobs/search?domain=not_matching").json()
    assert [i["id"] for i in only["items"]] == [hidden]
    assert only["items"][0]["domain_reason"] == "Outside your domain: digital marketing"
    triage = client.get("/api/jobs/search?queue=true").json()
    assert hidden not in [i["id"] for i in triage["items"]] and triage["domain_counts"]["not_matching"] == 1


def test_keeping_a_hidden_job_puts_it_through_the_pipeline(user, client):
    job_id = add(user, "Sales Engineer for AI Platforms")
    assert job_of(job_id).domain_match == "NOT_MATCHING"
    out = client.post(f"/api/jobs/{job_id}/domain-keep").json()
    assert out["kept"] == [job_id]
    job = job_of(job_id)
    assert job.domain_match == "KEPT" and job.state is S.RANK_PENDING
    assert len(rows(JobDescriptionParseTask, job_id)) == 1 and len(rows(JobRankingTask, job_id)) == 1
    with SessionLocal() as db:
        event = db.scalar(select(JobFeedbackEvent).where(JobFeedbackEvent.job_id == job_id))
        assert event.action == "KEPT"
        assert domain_match.reapply(db, user, actor="test")["hidden"] == 0, "kept jobs are never re-assessed"


def test_older_jobs_are_assessed_once_by_the_sweep(user):
    with SessionLocal() as db:
        job = Job(user_id=user, source=SOURCE, source_ref=uuid.uuid4().hex, title="Android Developer",
                  company="Acme", description=POSTING, state=S.DISCOVERED)
        db.add(job)
        db.commit()
        job_id = job.id
    with SessionLocal() as db:
        assert domain_match.sweep(db)["hidden"] >= 1
    assert job_of(job_id).domain_match == "NOT_MATCHING" and job_of(job_id).state is S.SKIPPED
    with SessionLocal() as db:
        assert job_id not in [j.id for j in db.scalars(select(Job).where(Job.domain_match.is_(None))).all()]


def test_changing_the_lists_can_bring_jobs_back(user, client):
    job_id = add(user, "Growth Marketing Engineer")
    assert job_of(job_id).domain_match == "NOT_MATCHING"
    settings = client.get("/api/settings/domain").json()
    assert "marketing" in settings["exclude_terms"] and "backend" in settings["derived"]["role_words"]
    client.put("/api/settings/domain", json={**{k: settings[k] for k in (
        "enabled", "exclude_terms", "skill_excludes", "learn_from_feedback")}, "include_terms": ["growth"]})
    out = client.post("/api/jobs/domain/reapply").json()
    assert out["restored"] == 1 and job_of(job_id).domain_match == "MATCH"


# ───────────── learning from the person ─────────────

def dismiss(client, owner: int, titles: list[str]) -> list[int]:
    ids = [add(owner, title) for title in titles]
    assert client.post("/api/jobs/bulk-triage", json={"ids": ids, "action": "dismiss"}).status_code == 200
    return ids


def learned(owner: int) -> list[dict]:
    with SessionLocal() as db:
        return domain_match.preferences(db, owner)["learned"]


def test_three_dismissals_teach_the_filter(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    rule = next(e for e in learned(user) if e["term"] == "underwriter")
    assert rule["status"] == "active" and rule["evidence"] == 3 and rule["examples"]
    job = job_of(add(user, "Underwriter Team Lead"))
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason.startswith("Similar to jobs you dismissed: 'underwriter'")


def test_two_dismissals_are_not_enough(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee"])
    assert not [e for e in learned(user) if e["term"] == "underwriter"]
    assert job_of(add(user, "Underwriter Team Lead")).domain_match == "MATCH"


def test_the_candidates_own_roles_and_skills_are_never_learned(user, client):
    dismiss(client, user, ["Python Backend Engineer", "Backend Engineer Python", "Senior Python Backend Engineer"])
    terms = {e["term"] for e in learned(user)}
    assert not any("python" in t or "backend" in t for t in terms)


def test_keeping_similar_jobs_turns_a_rule_off(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    hidden = add(user, "Underwriter Team Lead")
    client.post(f"/api/jobs/{hidden}/domain-keep")
    rule = next(e for e in learned(user) if e["term"] == "underwriter")
    assert rule["status"] == "disabled" and rule["positive"] == 1


def test_the_person_can_forget_or_promote_a_rule(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    out = client.put("/api/settings/domain/learned", json={"term": "underwriter", "kind": "title",
                                                          "action": "promote"}).json()
    assert "underwriter" in out["exclude_terms"] and not [e for e in out["learned"] if e["term"] == "underwriter"]
    dismiss(client, user, ["Claims Adjuster", "Claims Adjuster Trainee", "Senior Claims Adjuster"])
    client.put("/api/settings/domain/learned", json={"term": "claims", "kind": "title", "action": "delete"})
    dismiss(client, user, ["Claims Lead"])  # learning runs again: a forgotten rule stays forgotten
    rule = next(e for e in learned(user) if e["term"] == "claims")
    assert rule["status"] == "disabled" and rule["forgotten"]


def test_deletions_are_remembered_after_the_job_is_gone(user, client):
    ids = [add(user, t) for t in ("Account Director", "Account Director EMEA", "Account Director APAC")]
    client.post("/api/jobs/bulk-delete", json={"ids": ids})
    assert all(job_of(i) is None for i in ids)
    with SessionLocal() as db:
        events = db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user,
                                                           JobFeedbackEvent.action == "DELETED")).all()
    assert {e.job_id for e in events} == set(ids)
    assert any(e["term"] == "account" for e in learned(user)), "the shortest phrase: director is a generic word"


# ───────────── postings that no longer exist ─────────────

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_fetch_allow_private_hosts", True)
    with PostingServer() as posting_server:
        yield posting_server


def check(owner: int, job_id: int) -> str:
    from app.services.jd_recovery import utcnow

    return job_removal.check_one(SessionLocal, job_id, owner, utcnow())


def test_the_listing_check_deletes_only_postings_that_are_gone(user, server):
    gone, closed = add(user, "Backend Engineer", url=server.url("gone")), add(user, "AI Engineer",
                                                                             url=server.url("closed.html"))
    live, login, busy = (add(user, "Backend Engineer", url=server.url(p)) for p in ("posting.html", "login.html", "busy"))
    assert check(user, gone) == "deleted" and check(user, closed) == "deleted"
    assert job_of(gone) is None and job_of(closed) is None
    assert check(user, live) == "live" and check(user, login) == "unknown" and check(user, busy) == "unknown"
    for job_id in (live, login, busy):
        assert job_of(job_id).listing_checked_at is not None, "checked, never deleted on a guess"
    with SessionLocal() as db:
        assert {r.reason for r in db.scalars(select(RemovedPosting).where(RemovedPosting.user_id == user))} == \
            {"JOB_REMOVED", "JOB_CLOSED"}
        assert db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user)).all() == [], \
            "a dead posting says nothing about the person's taste"


def test_approved_or_applied_jobs_are_never_deleted(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("gone"))
    with SessionLocal() as db:
        db.get(Job, job_id).status = "applied"
        db.commit()
    assert check(user, job_id) == "kept" and job_of(job_id) is not None


def test_a_deleted_posting_is_not_imported_again(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("gone"))
    with SessionLocal() as db:
        ref = db.get(Job, job_id).source_ref
    check(user, job_id)
    item = {"title": "Backend Engineer", "company": "Acme", "url": server.url("gone"), "description": POSTING,
            "site": "linkedin", "id": "dead-1"}
    with SessionLocal() as db:
        db.info["user_id"] = user
        first = sources.import_apify_items(db, [item], user_id=user)
    assert first["created"] == 1
    with SessionLocal() as db:
        db.info["user_id"] = user
        created = db.scalar(select(Job).where(Job.user_id == user, Job.source_ref == "linkedin:dead-1"))
        assert job_removal.remove_posting(db, created, "JOB_REMOVED", actor="test") == "deleted"
        db.commit()
    with SessionLocal() as db:
        db.info["user_id"] = user
        again = sources.import_apify_items(db, [item], user_id=user)
        assert job_removal.is_removed(db, user, source=SOURCE, source_ref=ref)
    assert again["created"] == 0 and again["previously_removed"] == 1


def test_the_scheduled_listing_check_is_bounded(user, server, monkeypatch):
    monkeypatch.setattr(get_settings(), "listing_check_batch_size", 2)
    for _ in range(3):
        add(user, "Backend Engineer", url=server.url("posting.html"))
    with SessionLocal() as db:
        due = job_removal.due_jobs(db, job_removal.utcnow(), 2)
    assert len(due) == 2
    summary = asyncio.run(job_removal.check_listings(SessionLocal, job_removal.utcnow(), limit=2))
    assert summary["checked"] == 2


# ───────────── the resume per search and per job ─────────────

@pytest.fixture
def resumes(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        first = rv.record_version(db, profile, RESUME_A, source="manual_text", name="Backend resume")
        second = rv.record_version(db, profile, RESUME_B, source="manual_text", name="ML resume", activate=False)
        profile.master_resume = RESUME_A
        db.commit()
        return first.id, second.id


def test_a_saved_search_chooses_the_resume_of_the_jobs_it_finds(user, resumes):
    _, ml = resumes
    with SessionLocal() as db:
        db.info["user_id"] = user
        setup = SavedSearch(user_id=user, name="ML roles", settings={"roles": ["ML Engineer"], "resume_id": ml})
        db.add(setup)
        db.flush()
        run = SearchRun(user_id=user, provider="apify", saved_search_id=setup.id, external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        run_id = run.id
    job_id = add(user, "Machine Learning Engineer", search_run_id=run_id)
    assert job_of(job_id).resume_id == ml
    assert job_of(add(user, "Backend Engineer")).resume_id is None, "no setup: the active resume"


def test_workflows_read_the_jobs_resume_and_never_change_the_active_one(user, resumes):
    active, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        assert rv.for_job(db, profile, job) is profile, "no choice: exactly the profile as before"
        job.resume_id = ml
        view = rv.for_job(db, profile, job)
        assert view.master_resume == RESUME_B and rv.resume_for(db, view).id == ml
        assert view.name == "Asha Rao" and view.id == profile.id
        snapshot = ranker.resume_snapshot(db, view)
        assert snapshot.content == RESUME_B
        from app.services.context_builder import build_ranking_context

        context = build_ranking_context(db, view, job)
        assert context.resume["id"] == ml
        with pytest.raises(AttributeError):
            view.master_resume = "changed"
        db.commit()
    with SessionLocal() as db:
        assert db.get(Resume, active).is_active and not db.get(Resume, ml).is_active
        assert db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user)).master_resume == RESUME_A


def test_choosing_a_resume_for_old_jobs_ranks_them_again(user, resumes, client):
    _, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        ranker.queue_job(db, job, policy=ranker.RankingPolicy(70, 0.6), actor="test")
        db.commit()
    choices = client.get("/api/resumes/choices").json()["items"]
    assert {c["name"] for c in choices} >= {"Backend resume", "ML resume"}
    out = client.post("/api/jobs/bulk-resume", json={"ids": [job_id], "resume_id": ml}).json()
    assert out["changed"] == [job_id] and out["ranking_queued"] == [job_id]
    assert job_of(job_id).resume_id == ml and client.get(f"/api/jobs/{job_id}").json()["resume_choice_id"] == ml
    back = client.put(f"/api/jobs/{job_id}/resume", json={"resume_id": None}).json()
    assert back["resume_id"] is None and job_of(job_id).resume_id is None
    assert client.put("/api/resumes/999999/", json={"name": "x"}).status_code in (404, 405)


def test_a_resume_can_be_renamed_for_the_pickers(user, resumes, client):
    active, _ = resumes
    assert client.put(f"/api/resumes/{active}", json={"name": "Platform resume"}).json()["name"] == "Platform resume"


def test_the_mcp_context_carries_the_jobs_own_resume(user, resumes):
    from app.mcp import tools

    _, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        assert tools._job_resume(db, profile, job) == {}
        job.resume_id = ml
        own = tools._job_resume(db, profile, job)["resume"]
    assert own["resume_id"] == ml and own["text"] == RESUME_B and own["name"] == "ML resume"


def test_archiving_puts_a_job_away_without_teaching_the_filter(user, client):
    job_id = add(user, "Backend Engineer")
    assert client.post(f"/api/jobs/{job_id}/triage", json={"action": "archive"}).json()["status"] == "archived"
    job = job_of(job_id)
    assert job.status == "archived" and job.triaged_at is not None
    assert job_id not in [i["id"] for i in client.get("/api/jobs/search?queue=true").json()["items"]]
    assert job_id in [i["id"] for i in client.get("/api/jobs/search?stage=archived").json()["items"]]
    with SessionLocal() as db:
        assert ranker.hard_filter_reason(db.get(Job, job_id), ranker.RankingPolicy(70, 0.6)) == "Archived by you"
        assert db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.job_id == job_id)).all() == []
    client.post(f"/api/jobs/{job_id}/triage", json={"action": "restore"})
    job = job_of(job_id)
    assert job.status == "discovered" and job.triaged_at is None


# ───────────── the search's own scope ─────────────

def _scoped_run(owner: int, **params) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        run = SearchRun(user_id=owner, provider="apify", external_run_id=uuid.uuid4().hex,
                        actor_id="openclawai/job-board-scraper", roles=["Backend Engineer"],
                        location="Bengaluru, India", search_params={"primary_skills": ["Python"], **params})
        db.add(run)
        db.commit()
        return run.id


def _item(title: str, location: str, description: str = POSTING) -> dict:
    return {"title": title, "company": f"Company {uuid.uuid4().hex[:6]}", "location": location, "site": "linkedin",
            "id": uuid.uuid4().hex, "description": description, "job_url": f"https://example.test/{uuid.uuid4().hex}"}


def _import(owner: int, run_id: int, items: list[dict]) -> dict:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        return sources.import_apify_items(db, items, run_id, "linkedin", user_id=owner)


def _by_title(owner: int, run_id: int) -> dict[str, Job]:
    with SessionLocal() as db:
        return {j.title: j for j in db.scalars(select(Job).where(Job.user_id == owner, Job.search_run_id == run_id))}


def test_jobs_outside_the_searchs_roles_skills_or_country_are_hidden(user, client):
    run_id = _scoped_run(user)
    stats = _import(user, run_id, [
        _item("Senior Backend Engineer", "Bengaluru, Karnataka, India"),
        _item("Data Analyst", "Bengaluru, Karnataka, India"),
        _item("Backend Engineer", "Austin, TX, United States"),
        _item("Backend Engineer II", "Pune, India", description=POSTING.replace("Python", "Go")),
    ])
    jobs = _by_title(user, run_id)
    assert stats["created"] == 4 and stats["not_matching"] == 3
    assert jobs["Senior Backend Engineer"].domain_match == "MATCH"
    assert jobs["Data Analyst"].domain_reason.startswith("Not one of this search's roles")
    assert jobs["Backend Engineer"].domain_reason.startswith("Outside this search's location")
    assert jobs["Backend Engineer II"].domain_reason == "Does not mention Python"
    for title in ("Data Analyst", "Backend Engineer", "Backend Engineer II"):
        hidden = jobs[title]
        assert hidden.state is S.SKIPPED and rows(JobDescriptionParseTask, hidden.id) == []
        assert rows(JobRankingTask, hidden.id) == [] and rows(JobDescriptionFetchTask, hidden.id) == []
    assert client.post(f"/api/jobs/{jobs['Data Analyst'].id}/domain-keep").json()["kept"] == [jobs["Data Analyst"].id]
    assert job_of(jobs["Data Analyst"].id).domain_match == "KEPT"


def test_runs_without_a_recorded_scope_import_as_before(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        run = SearchRun(user_id=user, provider="apify", external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        run_id = run.id
    _import(user, run_id, [_item("Data Analyst", "Austin, TX")])
    assert _by_title(user, run_id)["Data Analyst"].domain_match == "MATCH"


def test_the_import_stops_at_the_setups_limit_even_when_pulled_again(user):
    run_id = _scoped_run(user, max_jobs=2)
    items = [_item(f"Backend Engineer {n}", "Bengaluru, India") for n in range(5)]
    stats = _import(user, run_id, items)
    assert stats["created"] == 2 and stats["over_limit"] == 3 and len(_by_title(user, run_id)) == 2
    again = _import(user, run_id, items)
    assert again["created"] == 0 and len(_by_title(user, run_id)) == 2


def test_reapplying_the_domain_lists_keeps_scope_hidden_jobs_hidden(user, client):
    run_id = _scoped_run(user)
    _import(user, run_id, [_item("Data Analyst", "Bengaluru, India")])
    hidden = _by_title(user, run_id)["Data Analyst"]
    assert client.post("/api/jobs/domain/reapply").status_code == 200
    job = job_of(hidden.id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason.startswith("Not one of this search's roles")
    assert rows(JobDescriptionParseTask, hidden.id) == [] and rows(JobRankingTask, hidden.id) == []


def test_a_recovered_description_is_checked_against_the_searchs_skills(user):
    from types import SimpleNamespace

    from app.services import jd_recovery

    run_id = _scoped_run(user)
    _import(user, run_id, [_item("Backend Engineer", "Bengaluru, India", description="")])
    job_id = _by_title(user, run_id)["Backend Engineer"].id
    assert job_of(job_id).domain_match == "MATCH", "no description yet: kept"
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        task = JobDescriptionFetchTask(user_id=user, job_id=job_id, status="PROCESSING")
        db.add(task)
        db.flush()
        outcome = SimpleNamespace(text=POSTING.replace("Python", "Java"), method="HTTP", final_url=job.url)
        assert jd_recovery.apply_recovered(db, job, task, outcome, jd_recovery.utcnow()) == "recovered"
        db.commit()
    job = job_of(job_id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason == "Does not mention Python"
    assert rows(JobDescriptionParseTask, job_id) == [] and rows(JobRankingTask, job_id) == []


def test_a_setups_primary_skill_is_never_a_foreign_skill(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(SavedSearch(user_id=user, name="Java roles", settings={"roles": ["Backend Engineer"],
                                                                      "primary_skills": ["Java"]}, weekdays=[]))
        db.commit()
    assert job_of(add(user, "Senior Java Developer")).domain_match == "MATCH"


def test_a_compound_primary_skill_protects_the_skill_it_names(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(SavedSearch(user_id=user, name="Spring roles", settings={"roles": ["Backend Engineer"],
                                                                        "primary_skills": ["Java 17"]}, weekdays=[]))
        db.commit()
    assert job_of(add(user, "Java Backend Engineer")).domain_match == "MATCH"
    assert job_of(add(user, "Senior PHP Developer")).domain_match == "NOT_MATCHING", "other exclusions still apply"
