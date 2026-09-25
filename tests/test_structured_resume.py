"""Structured resume: raw text preservation, deterministic parsing, versions, verification, history and evidence."""
import copy
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Evidence, Resume, ResumeDataChange
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

RESUME = """# Jane Doe
Lead Engineer
Bengaluru, India | jane@example.com | +91 98765 43210 | linkedin.com/in/jane

## Summary
Engineering leader building AI and data platforms.

## Skills
- Languages: Python, Go
- Cloud: AWS, Kubernetes

## Experience
### Lead Engineer - Data Platform | Zendrive | Bengaluru | Jun 2024 - Oct 2025
- Reduced production incidents by 45% across Kafka pipelines
- Owned the Python ingestion services on AWS

### Senior Engineer | Adori AI | Remote | 2021 - Present
- Built LLM features for podcast search

## Education
B.Tech Computer Science, IIT Delhi, 2012 - 2016
"""


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def user(monkeypatch):
    """A fresh account for each test, signed in for API calls."""
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    return user_id


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def upload(name, text):
    return client.post("/api/profile/resume", files={"file": (name, io.BytesIO(text.encode()), "text/plain")})


def active(user_id):
    return client.get("/api/settings/resume").json()["active"]


def verify_all(resume_id, sections=sr.SECTIONS):
    for section in sections:
        assert client.post(f"/api/resumes/{resume_id}/verify-section", json={"section": section}).status_code == 200


def put(detail, mutate):
    data = copy.deepcopy(detail["structured"])
    mutate(data)
    return client.put(f"/api/resumes/{detail['id']}/structured", json={"revision": detail["revision"], "data": data})


# ───────────── parsing ─────────────

def test_raw_text_is_cleaned_without_changing_wording():
    dirty = "Jane\x00 Doe\r\n\r\n\r\n\r\nLed   the ﬁrst team\t of 5 — cut cost 40%   \n"
    assert sr.clean_raw_text(dirty) == "Jane Doe\n\nLed the first team of 5 — cut cost 40%"


def test_deterministic_parse_keeps_raw_dates_and_does_not_infer_skills():
    data = sr.materialize(sr.parse_deterministic(RESUME))
    assert data["personal_information"]["full_name"] == "Jane Doe"
    assert data["personal_information"]["email"] == "jane@example.com"
    zendrive, adori = data["experience"]
    assert (zendrive["id"], zendrive["company"], zendrive["job_title"]) == ("exp_zendrive_2024", "Zendrive",
                                                                          "Lead Engineer - Data Platform")
    assert (zendrive["start_date"], zendrive["end_date"], zendrive["raw_start_date"]) == ("2024-06", "2025-10", "Jun 2024")
    # A year without a month is never guessed; "Present" marks a current role.
    assert (adori["start_date"], adori["raw_start_date"], adori["is_current"], adori["end_date"]) == (None, "2021", True, None)
    assert [a["text"] for a in zendrive["achievements"]] == ["Reduced production incidents by 45% across Kafka pipelines"]
    assert zendrive["metrics"][0]["value"] == "45" and zendrive["metrics"][0]["unit"] == "%"
    # Kafka is mentioned but is not in the skills section: it is not invented as a skill or technology.
    assert zendrive["technologies"] == ["Python", "AWS"] and "Kafka" not in [s["name"] for s in data["skills"]]
    assert all(u.status == "UNVERIFIED" for u in sr.iter_units(data))
    assert data["education"][0]["institution"] == "IIT Delhi"


def test_plain_text_resumes_are_parsed_too():
    plain = "JANE DOE\nStaff Engineer\n\nEXPERIENCE\nStaff Engineer, Flow AI  Jan 2023 – Present\n• Scaled inference to 20x\n"
    role, = sr.materialize(sr.parse_deterministic(plain))["experience"]
    assert (role["job_title"], role["company"], role["start_date"], role["is_current"]) == ("Staff Engineer", "Flow AI",
                                                                                          "2023-01", True)


def test_ids_are_stable_across_parses():
    first, second = (sr.materialize(sr.parse_deterministic(RESUME)) for _ in range(2))
    assert [u.item_id for u in sr.iter_units(first)] == [u.item_id for u in sr.iter_units(second)]


# ───────────── persistence, versions, active/default ─────────────

def test_upload_persists_raw_text_structured_data_and_original_file(user):
    out = upload("jane.txt", RESUME).json()
    detail = active(user)
    assert detail["id"] == out["resume_id"] and detail["version"] == 1
    assert detail["is_active"] and detail["is_default"] and detail["verification_status"] == "UNVERIFIED"
    assert detail["structured"]["experience"][0]["company"] == "Zendrive"
    assert client.get(f"/api/resumes/{detail['id']}/raw").json()["raw_text"] == RESUME.strip()
    original = client.get(f"/api/resumes/{detail['id']}/original")
    assert original.status_code == 200 and original.content == RESUME.encode()
    with session(user) as db:
        parse_log = db.scalars(select(ResumeDataChange).where(ResumeDataChange.resume_id == detail["id"])).all()
    assert [(c.change_type, c.source) for c in parse_log] == [("PARSE", "PARSER")]


def test_multiple_versions_are_kept_and_the_same_file_reuses_its_version(user):
    first = upload("v1.txt", RESUME).json()["resume_id"]
    second = upload("v2.txt", RESUME.replace("Go", "Rust")).json()["resume_id"]
    assert first != second
    versions = client.get("/api/settings/resume").json()["versions"]
    assert [(v["version"], v["is_active"], v["is_default"]) for v in versions] == [(2, True, False), (1, False, True)]
    assert upload("v1-again.txt", RESUME).json()["resume_id"] == first  # nothing overwritten, nothing duplicated
    assert active(user)["id"] == first
    assert client.post(f"/api/resumes/{second}/default").json()["is_default"] is True
    body = client.get("/api/settings/resume").json()
    assert {v["id"]: (v["is_active"], v["is_default"]) for v in body["versions"]} == {first: (True, False),
                                                                                       second: (False, True)}
    assert client.post(f"/api/resumes/{second}/activate").status_code == 200
    assert client.get("/api/profile").json()["master_resume"] == RESUME.replace("Go", "Rust").strip()


def test_edited_master_text_becomes_a_new_version(user):
    upload("jane.txt", RESUME)
    assert client.patch("/api/profile", json={"master_resume": RESUME + "\n## Awards\n- Hackathon winner"}).status_code == 200
    detail = active(user)
    assert detail["version"] == 2 and detail["source"] == "manual_text"
    assert detail["structured"]["awards"][0]["title"] == "Hackathon winner"


def test_deleting_an_upload_never_loses_the_original_a_version_uses(user):
    document = upload("jane.txt", RESUME).json()
    resume_id = document["resume_id"]
    assert client.delete(f"/api/profile/resumes/{document['id']}").status_code == 200
    assert client.get(f"/api/resumes/{resume_id}/original").status_code == 200


def test_resume_data_belongs_to_its_owner(user, monkeypatch):
    resume_id = upload("jane.txt", RESUME).json()["resume_id"]
    other = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(other, "o@jobhunt.test", False))
    assert client.get(f"/api/resumes/{resume_id}/structured").status_code == 404
    assert client.post(f"/api/resumes/{resume_id}/verify-item", json={"item_id": "summary"}).status_code == 404


# ───────────── verification ─────────────

def test_item_verification_and_rejection(user):
    detail = upload("jane.txt", RESUME) and active(user)
    out = client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "summary", "status": "VERIFIED"}).json()
    assert out["structured"]["summary"]["verification"]["status"] == "VERIFIED"
    assert out["verification_status"] == "PARTIALLY_VERIFIED" and out["progress"]["counts"]["VERIFIED"] == 1
    out = client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "summary", "status": "REJECTED"}).json()
    assert out["structured"]["summary"]["verification"]["status"] == "REJECTED"
    out = client.post(f"/api/resumes/{detail['id']}/unverify-item", json={"item_id": "summary"}).json()
    assert out["structured"]["summary"]["verification"]["status"] == "UNVERIFIED"
    assert client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "nope"}).status_code == 404
    types = [c["change_type"] for c in client.get(f"/api/resumes/{detail['id']}/changes").json()["items"]]
    assert types[:3] == ["UNVERIFY", "REJECT", "VERIFY"]


def test_section_verification_leaves_rejected_items_alone(user):
    detail = upload("jane.txt", RESUME) and active(user)
    bullet = detail["structured"]["experience"][0]["responsibilities"][0]["id"]
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": bullet, "status": "REJECTED"})
    out = client.post(f"/api/resumes/{detail['id']}/verify-section", json={"section": "experience"}).json()
    statuses = {u.item_id: u.status for u in sr.iter_units(out["structured"]) if u.section == "experience"}
    assert statuses.pop(bullet) == "REJECTED" and set(statuses.values()) == {"VERIFIED"}
    assert client.post(f"/api/resumes/{detail['id']}/verify-section", json={"section": "bogus"}).status_code == 404


def test_whole_resume_verification_requires_reviewed_required_sections(user):
    detail = upload("jane.txt", RESUME) and active(user)
    blocked = client.post(f"/api/resumes/{detail['id']}/verify")
    assert blocked.status_code == 422 and blocked.json()["detail"]["blocking"]
    verify_all(detail["id"], sr.REQUIRED_SECTIONS)
    out = client.post(f"/api/resumes/{detail['id']}/verify").json()
    assert out["verification_status"] == "VERIFIED" and out["verified_at"] and out["progress"]["blocking"] == []


def test_editing_a_verified_field_needs_review_and_keeps_history(user):
    detail = upload("jane.txt", RESUME) and active(user)
    verify_all(detail["id"], sr.REQUIRED_SECTIONS)
    detail = client.post(f"/api/resumes/{detail['id']}/verify").json()

    def rename(data):
        data["experience"][0]["job_title"] = "Staff Engineer"
    out = put(detail, rename).json()
    role = out["structured"]["experience"][0]
    assert role["job_title"] == "Staff Engineer" and role["id"] == "exp_zendrive_2024"
    assert role["verification"]["status"] == "EDITED" and out["verification_status"] == "NEEDS_REVIEW"
    assert out["revision"] == detail["revision"] + 1
    change = client.get(f"/api/resumes/{detail['id']}/changes").json()["items"][0]
    assert (change["field_path"], change["old_value"], change["new_value"], change["change_type"], change["source"]) == (
        "experience[exp_zendrive_2024].job_title", "Lead Engineer - Data Platform", "Staff Engineer", "UPDATE", "USER")
    # The raw text is never rewritten by edits.
    assert client.get(f"/api/resumes/{detail['id']}/raw").json()["raw_text"] == RESUME.strip()
    # A stale editor cannot overwrite the newer data.
    assert put(detail, rename).status_code == 409
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "exp_zendrive_2024"})
    assert client.post(f"/api/resumes/{detail['id']}/verify").json()["verification_status"] == "VERIFIED"


def test_added_removed_and_reordered_items_are_recorded(user):
    detail = upload("jane.txt", RESUME) and active(user)

    def change(data):
        data["skills"].reverse()
        data["skills"].append({"name": "Terraform", "category": "Cloud", "verification": {"status": "VERIFIED"}})
        data["experience"].pop()
    out = put(detail, change).json()
    terraform = next(s for s in out["structured"]["skills"] if s["name"] == "Terraform")
    assert terraform["verification"]["status"] == "UNVERIFIED"  # a client cannot mark its own input verified
    types = {c["change_type"] for c in client.get(f"/api/resumes/{detail['id']}/changes").json()["items"]}
    assert {"ADD", "REMOVE", "REORDER"} <= types


# ───────────── re-parse ─────────────

def test_reparse_is_a_comparison_and_never_overwrites_verified_values(user):
    detail = upload("jane.txt", RESUME) and active(user)
    client.post(f"/api/resumes/{detail['id']}/verify-section", json={"section": "experience"})
    detail = active(user)

    def rename(data):
        data["experience"][0]["job_title"] = "Principal Engineer"
    detail = put(detail, rename).json()
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "exp_zendrive_2024"})
    out = client.post(f"/api/resumes/{detail['id']}/reparse", json={"use_ai": False}).json()
    assert out["structured"]["experience"][0]["job_title"] == "Principal Engineer"
    change = next(c for c in out["pending_reparse"]["changes"] if c["path"].endswith(".job_title"))
    assert (change["current"], change["proposed"], change["current_status"]) == (
        "Principal Engineer", "Lead Engineer - Data Platform", "VERIFIED")

    kept = client.post(f"/api/resumes/{detail['id']}/reparse/resolve", json={"decisions": {}}).json()
    assert kept["structured"]["experience"][0]["job_title"] == "Principal Engineer" and kept["pending_reparse"] is None

    client.post(f"/api/resumes/{detail['id']}/reparse", json={"use_ai": True})  # the mock AI parser
    pending = active(user)["pending_reparse"]
    change = next(c for c in pending["changes"] if c["path"].endswith(".job_title"))
    used = client.post(f"/api/resumes/{detail['id']}/reparse/resolve", json={"decisions": {change["id"]: "use_new"}}).json()
    role = used["structured"]["experience"][0]
    assert role["job_title"] == "Lead Engineer - Data Platform" and role["verification"]["status"] == "UNVERIFIED"
    history = client.get(f"/api/resumes/{detail['id']}/changes").json()["items"]
    assert any(c["change_type"] == "REPARSE_ACCEPT" and c["source"] == "PARSER" for c in history)
    assert any(c["change_type"] == "REPARSE_KEEP" for c in history)


# ───────────── Evidence Bank ─────────────

def resume_evidence(user_id):
    with session(user_id) as db:
        return db.scalars(select(Evidence).where(Evidence.source == "resume").order_by(Evidence.id)).all()


def test_only_verified_facts_become_evidence_with_provenance(user):
    detail = upload("jane.txt", RESUME) and active(user)
    achievement = detail["structured"]["experience"][0]["achievements"][0]["id"]
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": achievement})
    assert resume_evidence(user) == []  # the role itself is not verified yet
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": "exp_zendrive_2024"})
    row, = resume_evidence(user)
    assert (row.claim, row.company, row.verified) == ("Reduced production incidents by 45% across Kafka pipelines",
                                                      "Zendrive", True)
    assert (row.resume_id, row.structured_item_id, row.source_file_hash) == (detail["id"], achievement,
                                                                             detail["source_file_hash"])
    # Rejecting the fact takes it out of the trusted evidence without deleting it.
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": achievement, "status": "REJECTED"})
    row, = resume_evidence(user)
    assert row.verified is False
    # Unverified and rejected responsibilities never create evidence.
    assert all(e.structured_item_id == achievement for e in resume_evidence(user))


def test_editing_verified_evidence_withdraws_it_until_reverified(user):
    detail = upload("jane.txt", RESUME) and active(user)
    verify_all(detail["id"], ("experience",))
    assert len([e for e in resume_evidence(user) if e.verified]) == 3
    detail = active(user)

    def edit(data):
        data["experience"][0]["achievements"][0]["text"] = "Reduced production incidents by 50%"
    out = put(detail, edit).json()
    edited = out["structured"]["experience"][0]["achievements"][0]
    assert edited["verification"]["status"] == "EDITED"
    assert not next(e for e in resume_evidence(user) if e.structured_item_id == edited["id"]).verified
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": edited["id"]})
    row = next(e for e in resume_evidence(user) if e.structured_item_id == edited["id"])
    assert row.verified and row.claim == "Reduced production incidents by 50%"


def test_verified_facts_are_reused_across_resume_versions(user):
    first = upload("v1.txt", RESUME).json()["resume_id"]
    verify_all(first, ("experience",))
    before = {e.id for e in resume_evidence(user)}
    second = upload("v2.txt", RESUME.replace("Go", "Rust")).json()["resume_id"]
    verify_all(second, ("experience",))
    after = resume_evidence(user)
    assert {e.id for e in after} == before and {e.resume_id for e in after} == {second}


# ───────────── service rules ─────────────

def test_parser_never_writes_over_verified_data_directly(user):
    upload("jane.txt", RESUME)
    with session(user) as db:
        resume = rv.active_resume(db, profile_for(db))
        rv.verify_section(db, resume, "summary")
        before = copy.deepcopy(resume.structured_data)
        rv.reparse(db, resume)
        assert resume.structured_data == before
        db.rollback()


def test_policy_endpoint_saves_the_candidate_policy(user):
    assert client.get("/api/settings/resume").json()["policy"]["ranking_uses_unverified"] is True
    out = client.put("/api/settings/resume/policy", json={"ranking_uses_unverified": False}).json()
    assert out["ranking_uses_unverified"] is False


def test_resume_verification_works_without_structured_data_yet(user):
    """Migrated versions have no structured data until first use."""
    upload("jane.txt", RESUME)
    with session(user) as db:
        resume = db.scalar(select(Resume))
        resume.structured_data = None
        db.commit()
    assert active(user)["structured"]["experience"][0]["company"] == "Zendrive"



def test_education_keeps_university_and_grade():
    data = sr.materialize({"education": [{"degree": "B.Tech", "institution": "Kalyani Government Engineering College",
                                          "university": "MAKAUT", "grade": "8.2/10 CGPA", "end_date": "2014-06"}]})
    edu, = data["education"]
    assert (edu["university"], edu["grade"]) == ("MAKAUT", "8.2/10 CGPA")
    assert "MAKAUT | 8.2/10 CGPA" in sr.render_text(data, statuses=("UNVERIFIED",))
