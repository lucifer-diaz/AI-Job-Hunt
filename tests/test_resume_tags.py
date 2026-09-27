"""Resume tags: each resume version is tagged with a domain and the roles it targets, new jobs are matched to the
best-tagged resume (deterministically), and a choice the saved search or the person pinned always wins."""
import uuid

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CandidateProfile, Job, Resume, ResumeDocument, SavedSearch, SearchRun
from app.services import resume_matching as rm
from app.services import resume_verification as rv
from tests.test_discovery_quality import (  # noqa: F401  fixtures
    RESUME_A,
    RESUME_B,
    add,
    client,
    job_of,
    user,
)


def tagged(id_: int, domain: str | None, roles: list[str]) -> Resume:
    return Resume(id=id_, name=f"r{id_}", version=id_, raw_text="", raw_text_hash="x", domain=domain, roles=roles)


BACKEND = tagged(1, "Backend", ["Lead Engineer", "Engineering Manager"])
PRODUCT = tagged(2, "Product", ["Associate Product Manager", "Product Manager"])
UNTAGGED = tagged(3, None, [])


def picked(title: str, resumes=(BACKEND, PRODUCT, UNTAGGED), description: str = ""):
    match = rm.pick(list(resumes), Job(title=title, description=description))
    return match.resume_id if match else None


# ───────────── matching (no database) ─────────────

@pytest.mark.parametrize("title,expected", [
    ("Lead Engineer, Payments", 1), ("Engineering Manager - Platform", 1), ("Senior Backend Developer", 1),
    ("Associate Product Manager", 2), ("Senior Product Manager, Growth", 2), ("Data Scientist", None),
    ("Account Executive", None)])
def test_a_job_gets_the_resume_whose_tags_its_title_names(title, expected):
    assert picked(title) == expected


def test_the_most_specific_role_wins():
    apm, pm = tagged(4, "Product", ["Associate Product Manager"]), tagged(5, "Product", ["Product Manager"])
    assert picked("Associate Product Manager", (apm, pm)) == 4
    assert picked("Product Manager, Payments", (apm, pm)) == 5


def test_a_close_call_is_left_to_the_person():
    other = tagged(6, "Backend", ["Senior Backend Engineer"])
    domain_only = tagged(7, "Backend", [])
    assert picked("Backend Developer", (domain_only, tagged(8, "Backend", []))) is None, "two equal matches"
    assert picked("Senior Backend Engineer", (other, domain_only)) == 6, "a role beats a domain"


def test_the_description_only_breaks_ties():
    ml, data = tagged(9, "AI / ML", []), tagged(10, "Data", [])
    text = "We train machine learning models and ship LLM features. ML ops too."
    assert picked("Software Engineer", (ml,), text) is None, "the title must name a role or the domain"
    assert picked("AI Data Engineer", (ml, data), text) == 9, "both domains in the title: the description decides"


def test_untagged_resumes_are_never_picked():
    assert picked("Lead Engineer", (UNTAGGED,)) is None


def test_the_catalog_suggests_roles_per_domain():
    catalog = {c["domain"]: c["roles"] for c in rm.catalog()}
    assert "Engineering Manager" in catalog["Backend"] and "Associate Product Manager" in catalog["Product"]


# ───────────── in the pipeline ─────────────

@pytest.fixture
def tagged_resumes(user):  # noqa: F811
    with SessionLocal() as db:
        db.info["user_id"] = user
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        backend = rv.record_version(db, profile, RESUME_A, source="manual_text", name="Backend resume")
        product = rv.record_version(db, profile, RESUME_B, source="manual_text", name="Product resume",
                                    activate=False)
        backend.domain, backend.roles = "Backend", ["Lead Engineer", "Engineering Manager"]
        product.domain, product.roles = "Product", ["Associate Product Manager", "Product Manager"]
        profile.master_resume = RESUME_A
        db.commit()
        return backend.id, product.id


def run_for(owner: int, settings: dict) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        setup = SavedSearch(user_id=owner, name=f"setup {uuid.uuid4().hex[:6]}", settings=settings)
        db.add(setup)
        db.flush()
        run = SearchRun(user_id=owner, provider="apify", saved_search_id=setup.id, external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        return run.id


def test_new_jobs_are_matched_to_the_tagged_resume(user, tagged_resumes):  # noqa: F811
    backend, product = tagged_resumes
    apm = job_of(add(user, "Associate Product Manager"))
    lead = job_of(add(user, "Lead Engineer"))
    unclear = job_of(add(user, "Solutions Architect"))
    assert (apm.resume_id, apm.resume_choice) == (product, rm.AUTO)
    assert (lead.resume_id, lead.resume_choice) == (backend, rm.AUTO)
    assert (unclear.resume_id, unclear.resume_choice) == (None, None), "no clear match: the active resume"


def test_a_setup_can_pin_a_resume_or_the_active_one(user, tagged_resumes):  # noqa: F811
    backend, _ = tagged_resumes
    pinned = job_of(add(user, "Associate Product Manager",
                        search_run_id=run_for(user, {"resume_mode": "fixed", "resume_id": backend})))
    assert (pinned.resume_id, pinned.resume_choice) == (backend, rm.SETUP)
    active = job_of(add(user, "Associate Product Manager", search_run_id=run_for(user, {"resume_mode": "active"})))
    assert (active.resume_id, active.resume_choice) == (None, rm.SETUP)
    auto = job_of(add(user, "Lead Engineer", search_run_id=run_for(user, {"resume_mode": "auto"})))
    assert auto.resume_choice == rm.AUTO


def test_tags_are_edited_on_versions_and_files_and_shown_everywhere(user, tagged_resumes, client):  # noqa: F811
    backend, product = tagged_resumes
    out = client.put(f"/api/resumes/{product}/tags", json={"domain": " Product ", "roles": ["Product Manager",
                                                                                               "Product Manager", " "]})
    assert out.json()["domain"] == "Product" and out.json()["roles"] == ["Product Manager"]
    versions = {v["id"]: v for v in client.get("/api/settings/resume").json()["versions"]}
    assert versions[product]["roles"] == ["Product Manager"] and versions[backend]["domain"] == "Backend"
    choices = client.get("/api/resumes/choices").json()
    assert {c["id"]: c["domain"] for c in choices["items"]}[backend] == "Backend" and choices["catalog"]
    with SessionLocal() as db:
        db.info["user_id"] = user
        document = ResumeDocument(user_id=user, original_filename="Founder.pdf", storage_path="/tmp/none.pdf",
                                  checksum=uuid.uuid4().hex, media_type="application/pdf", size=10, extracted_text=RESUME_B + "Founder\n",
                                  active=False)
        db.add(document)
        db.commit()
        document_id = document.id
    tagged_file = client.put(f"/api/profile/resumes/{document_id}/tags",
                             json={"domain": "Engineering Leadership", "roles": ["CTO"]}).json()
    library = {d["id"]: d for d in client.get("/api/profile/resumes").json()}
    assert library[document_id]["domain"] == "Engineering Leadership" and library[document_id]["roles"] == ["CTO"]
    assert library[document_id]["version_id"] == tagged_file["id"], "the file and its version share one set of tags"
    with SessionLocal() as db:
        assert not db.get(Resume, tagged_file["id"]).is_active, "tagging a file never makes it active"


def test_a_person_can_choose_auto_active_or_a_resume(user, tagged_resumes, client):  # noqa: F811
    backend, product = tagged_resumes
    job_id = add(user, "Associate Product Manager")
    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "fixed", "resume_id": backend})
    assert (job_of(job_id).resume_id, job_of(job_id).resume_choice) == (backend, rm.USER)
    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "active"})
    assert (job_of(job_id).resume_id, job_of(job_id).resume_choice) == (None, rm.ACTIVE)
    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "auto"})
    assert (job_of(job_id).resume_id, job_of(job_id).resume_choice) == (product, rm.AUTO)
    assert client.put(f"/api/jobs/{job_id}/resume", json={"mode": "fixed"}).status_code == 422
    card = client.get(f"/api/jobs/{job_id}").json()
    assert card["resume_choice"] == rm.AUTO and card["resume_choice_id"] == product


def test_re_matching_changes_only_unpinned_jobs(user, tagged_resumes, client):  # noqa: F811
    backend, product = tagged_resumes
    auto_job = add(user, "Engineering Manager, Payments")
    pinned = add(user, "Engineering Manager, Growth")
    client.put(f"/api/jobs/{pinned}/resume", json={"mode": "fixed", "resume_id": backend})
    assert job_of(auto_job).resume_id == backend
    # Retag: engineering managers now use the product resume.
    client.put(f"/api/resumes/{backend}/tags", json={"domain": "Backend", "roles": ["Lead Engineer"]})
    client.put(f"/api/resumes/{product}/tags", json={"domain": "Product", "roles": ["Engineering Manager"]})
    plan = client.post("/api/jobs/resume/rematch?dry_run=true").json()
    assert plan["changing"] == 1 and job_of(auto_job).resume_id == backend, "a dry run changes nothing"
    out = client.post("/api/jobs/resume/rematch").json()
    assert out["changed"] == [auto_job] and job_of(auto_job).resume_id == product
    assert job_of(pinned).resume_id == backend and job_of(pinned).resume_choice == rm.USER


# ───────────── deleting a version ─────────────

def test_the_active_version_cannot_be_deleted(user, tagged_resumes, client):  # noqa: F811
    backend, _ = tagged_resumes
    out = client.delete(f"/api/resumes/{backend}")
    assert out.status_code == 409 and "Make another version active" in out.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Resume, backend) is not None


def test_deleting_a_version_undoes_everything_that_pointed_at_it(user, tagged_resumes, client, monkeypatch):  # noqa: F811
    from app.models import Evidence

    backend, product = tagged_resumes
    job_id = add(user, "Associate Product Manager")
    assert job_of(job_id).resume_id == product
    run_id = run_for(user, {"resume_mode": "fixed", "resume_id": product})
    shared, only_here = rv.fact_key("Acme", "Built Python services"), rv.fact_key("Globex", "Trained models")
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.get(Resume, backend).structured_data = {"marker": "backend"}
        for key in (shared, only_here):
            db.add(Evidence(user_id=user, source="resume", verified=True, claim=key, company="x", role="y",
                            resume_id=product, structured_item_id="exp_1", fact_key=key))
        db.commit()
    # The active (backend) version still verifies the shared fact; nothing verifies the other one.
    monkeypatch.setattr(rv, "verified_facts", lambda data: {"exp_9": {"company": "Acme",
                                                                       "claim": "Built Python services"}}
                        if (data or {}).get("marker") == "backend" else {})
    out = client.delete(f"/api/resumes/{product}").json()
    assert out == {"deleted": product, "jobs": 1, "saved_searches": 1, "evidence_relinked": 1, "evidence_retired": 1}
    job = job_of(job_id)
    assert (job.resume_id, job.resume_choice) == (None, None), "back to matching by tags"
    with SessionLocal() as db:
        assert db.get(Resume, product) is None
        setup = db.get(SavedSearch, db.get(SearchRun, run_id).saved_search_id)
        assert setup.settings["resume_mode"] == "auto" and setup.settings["resume_id"] is None
        rows = {row.fact_key: row for row in db.scalars(select(Evidence).where(Evidence.user_id == user)).all()}
        assert rows[shared].verified and rows[shared].resume_id == backend and rows[shared].structured_item_id == "exp_9"
        assert not rows[only_here].verified and rows[only_here].resume_id is None
    versions = [v["id"] for v in client.get("/api/settings/resume").json()["versions"]]
    assert product not in versions and backend in versions
