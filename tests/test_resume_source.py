"""The candidate policy picks where AI takes the resume from: the structured data (the resume document is rendered
from it, so the user's corrections win everywhere) or the selected resume's text as written."""
from sqlalchemy.orm.attributes import flag_modified

from app.models import Resume
from app.services import context_builder as cb
from app.services.resume_format import parse_resume
from app.services.user_settings import profile_for
from app.services.workflow import build_resume_markdown
from tests.test_context_builder import add_job, build, client, resume_id, session, verify
from tests.test_context_builder import (
    user as user,  # noqa: PLC0414  the fixture, re-exported for this module
)
from tests.test_prompt_architecture import COMPLETED, set_fit


def correct_title(user_id, title="Principal Engineer", company="Zendrive India"):
    """What Settings → Resume Data does when the user fixes a role's title and employer and verifies it."""
    with session(user_id) as db:
        resume = db.get(Resume, resume_id())
        data = dict(resume.structured_data)
        data["experience"] = [dict(role) for role in data["experience"]]
        data["experience"][0].update(job_title=title, company=company,
                                     verification={**(data["experience"][0].get("verification") or {}),
                                                   "status": "VERIFIED"})
        resume.structured_data = data
        flag_modified(resume, "structured_data")
        db.commit()


def policy(**changes):
    response = client.put("/api/settings/resume/policy", json=changes)
    assert response.status_code == 200, response.text
    return response.json()


def headings(user_id, content=None):
    with session(user_id) as db:
        markdown = build_resume_markdown(profile_for(db), content or {})
    return [line for line in markdown.splitlines() if line.startswith("### ")]


def test_the_policy_names_a_resume_source_and_rejects_anything_else(user):
    assert client.get("/api/settings/resume").json()["policy"]["resume_source"] == "structured", "the default"
    assert policy(resume_source="master_text")["resume_source"] == "master_text"
    assert client.put("/api/settings/resume/policy", json={"resume_source": "pdf"}).status_code == 422
    assert policy(allow_unverified_resume_facts=True)["resume_source"] == "master_text", "other changes keep it"


def test_structured_source_puts_verified_corrections_in_the_headings(user):
    verify("experience", "skills", "summary")
    correct_title(user)
    assert headings(user)[0].startswith("### Principal Engineer | Zendrive India | Bengaluru"), \
        "the correction wins; location and dates are the data's"
    assert headings(user)[1].startswith("### Frontend Developer | Pixel Co"), "uncorrected roles are unchanged"

    policy(resume_source="master_text")
    assert headings(user)[0].startswith("### Lead Engineer | Zendrive |"), "master text is used as written"


def test_structured_source_renders_the_data_as_corrected_and_leaves_out_rejected_roles(user):
    """With the structured source the resume document *is* the structured data: whatever the person corrected in
    Resume Data shows, verified or not; only what they rejected is left out. Never the raw text's own version."""
    with session(user) as db:
        resume = db.get(Resume, resume_id())
        data = dict(resume.structured_data)
        data["experience"] = [dict(role) for role in data["experience"]]
        data["experience"][0].update(job_title="Principal Engineer", verification={"status": "EDITED"})
        data["experience"][1].update(verification={"status": "REJECTED"})
        resume.structured_data = data
        flag_modified(resume, "structured_data")
        db.commit()
    lines = headings(user)
    assert lines[0].startswith("### Principal Engineer | Zendrive | Bengaluru"), "the edited title, not the text's"
    assert not any("Pixel Co" in line for line in lines), "a rejected role is not in the resume"

    policy(resume_source="master_text")
    assert [line.split(" | ")[0] for line in headings(user)] == ["### Lead Engineer", "### Frontend Developer"], \
        "the text as written ignores Resume Data entirely"


def test_roles_are_matched_by_position_when_the_title_changed_completely():
    parsed = parse_resume("## Experience\n### SDE 4 - Data Platform | Zendrive | Bangalore\n- Built pipelines\n"
                          "### Staff Engineer | Adori | Bangalore\n- Led a team\n")
    experience = [{"id": "a", "job_title": "SDE 3", "company": "Zendrive India", "source_role_index": 5},
                  {"id": "b", "job_title": "Staff Engineer", "company": "Adori", "source_role_index": 7}]
    assert cb.map_roles(parsed, experience) == {"a": 0, "b": 1}, "a stale source_role_index does not drop a role"


def test_master_text_source_tailors_from_the_text_without_verified_facts(user):
    job_id = add_job(user)
    set_fit(user, job_id, COMPLETED)
    policy(resume_source="master_text")
    context = build(user, cb.build_tailoring_context, job_id)  # nothing verified: no UnverifiedResumeError
    titles = [(role["title"], role["company"]) for role in context.roles]
    assert titles == [("Lead Engineer", "Zendrive"), ("Frontend Developer", "Pixel Co")]
    assert "Reduced production incidents by 45% across Kafka pipelines" in context.roles[0]["bullets"]
    roles = context.source_resume["roles"]
    assert roles[0]["title"] == "Lead Engineer" and roles[0]["bullets"], "the Tailor sees the text's own bullets"
