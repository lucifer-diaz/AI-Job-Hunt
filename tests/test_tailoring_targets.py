"""Tailoring against the posting: keyword targets (ATS terms the facts support, and the gaps), the requirement map
(the Ranker's matched requirements and the ids that prove them), and the validator reading compound names."""
from types import SimpleNamespace

from app.models import Job
from app.prompts import build_cover_letter_prompt, build_tailoring_prompt
from app.services import ats_keywords as ats
from app.services import context_builder as cb
from app.services.job_description import description_hash
from app.services.tailoring_results import (
    TailoredResumeIn,
    _term_supported,
    _with_parts,
    _words,
    validate_tailored_resume,
)
from app.services.user_settings import profile_for
from app.services.workflow import supported_keyword_coverage
from tests.test_context_builder import add_job, build, session, verify
from tests.test_context_builder import user as user  # noqa: PLC0414  the fixture, re-exported
from tests.test_prompt_architecture import COMPLETED, set_fit

STRUCTURED = {"required_skills": ["Kafka", "Python", "PHP"], "preferred_skills": ["Kubernetes"],
              "keywords": ["incident reduction", "Terraform"],
              "responsibilities": ["Own Kafka pipelines", "Lead and mentor engineers"]}


def test_targets_split_the_posting_keywords_by_what_the_facts_support():
    job = SimpleNamespace(structured_description=STRUCTURED, skills=[])
    vocabulary = _words("Led incident reduction across Kafka pipelines in Python on Kubernetes")
    sources = [{"id": 6, "text": "Reduced production incidents by 45% across Kafka pipelines"}, {"id": 7, "text": "x"}]
    targets = ats.targets(job, vocabulary, sources)
    assert [t["keyword"] for t in targets["use"]] == ["Kafka", "Python", "Kubernetes", "incident reduction"], \
        "REQUIRED first, then PREFERRED, then keywords, each in posting order"
    assert targets["use"][0] == {"keyword": "Kafka", "level": "REQUIRED", "found_in": [6]}
    assert "found_in" not in targets["use"][1], "no source mentions Python: nothing to point at"
    assert targets["do_not_use"] == ["PHP", "Terraform"], "a requirement the facts lack stays a gap"


def test_compound_names_are_supported_by_their_parts():
    vocabulary = _with_parts(_words("Built CI/CD pipelines with Node.js. Python-based ETL."))
    for term in ("ci/cd", "node", "python-based", "node.js"):
        assert _term_supported(term, vocabulary), term
    assert not _term_supported("terraform", vocabulary) and not _term_supported("ci/terraform", vocabulary)


def test_supported_keyword_coverage_names_what_the_resume_leaves_out():
    targets = {"use": [{"keyword": "Kafka", "level": "REQUIRED"}, {"keyword": "CI/CD", "level": "KEYWORD"}]}
    out = supported_keyword_coverage(targets, "## Experience\n- Ran Kafka pipelines\n## Source Resume\nCI/CD")
    assert out == {"supported": ["Kafka", "CI/CD"], "missing_supported": ["CI/CD"], "score": 50}
    assert supported_keyword_coverage({}, "text")["score"] is None


def _ready(user_id):
    job_id = add_job(user_id)
    verify("experience", "skills", "summary")
    with session(user_id) as db:
        job = db.get(Job, job_id)
        job.structured_description, job.structured_description_hash = STRUCTURED, description_hash(job.description)
        db.commit()
    return job_id


def test_the_tailoring_context_and_prompt_carry_the_map_and_targets(user):
    job_id = _ready(user)
    set_fit(user, job_id, COMPLETED)
    context = build(user, cb.build_tailoring_context, job_id)
    cited = min(context.citable_ids)
    set_fit(user, job_id, {**COMPLETED, "requirement_assessments": [
        {"requirement": "Kafka", "requirement_id": "req_001", "level": "REQUIRED", "status": "MATCH",
         "supported": "Kafka pipelines", "evidence_ids": [cited, 999999]},
        {"requirement": "Kubernetes", "requirement_id": "req_004", "level": "PREFERRED", "status": "PARTIAL_MATCH",
         "evidence_ids": []},
        {"requirement": "PHP", "requirement_id": "req_003", "level": "REQUIRED", "status": "MISSING", "evidence_ids": []}]})
    context = build(user, cb.build_tailoring_context, job_id)
    assert [row["requirement"] for row in context.requirement_map] == ["Kafka", "Kubernetes"], "MISSING stays out"
    assert context.requirement_map[0]["evidence_ids"] == [cited], "an id the context cannot cite is dropped"
    use = [t["keyword"] for t in context.keyword_targets["use"]]
    assert {"Kafka", "Python", "Kubernetes"} <= set(use)
    assert {"PHP", "Terraform"} <= set(context.keyword_targets["do_not_use"])
    prompt = build_tailoring_prompt(candidate_profile=None, job=None, context=context)
    assert prompt.version == "v8" and "REQUIREMENT MAP" in prompt.user and "KEYWORD TARGETS" in prompt.user
    assert "do_not_use" in prompt.system and "WRITE FOR THIS POSTING" in prompt.system
    assert prompt.user.index("REQUIREMENT MAP") < prompt.user.index("LENGTH PLAN")

    # Every "use" term passes validation: naming it never costs a bullet.
    with session(user) as db:
        job = db.get(Job, job_id)
        issues = validate_tailored_resume(db, profile_for(db), job, TailoredResumeIn(
            job_id=job_id, summary="Engineer working with " + ", ".join(use), skills_order=use))
    assert not [i for i in issues if i["code"] in ("UNSUPPORTED_TERM", "INVENTED_SKILL")], issues


def test_generation_terms_weight_what_the_parsed_posting_asks_for(user):
    job_id = _ready(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        plain, weighted = cb.job_terms(job), cb.generation_terms(job)
    assert plain.focus == set(), "ranking keeps the plain terms"
    assert weighted.score("Owned Kafka pipelines") > plain.score("Owned Kafka pipelines")


def test_the_cover_letter_reads_the_parsed_posting_and_the_tailored_resume():
    context = {"candidate": {"name": "Jane"}, "job": {"title": "Lead", "company": "Globex", "description": "About us"},
               "job_context": {"structured": True, "role_summary": "Own the data platform",
                               "responsibilities": ["Own Kafka pipelines"]},
               "requirement_map": [{"requirement": "Kafka", "level": "REQUIRED", "status": "MATCH", "evidence_ids": [6]}],
               "keyword_targets": {"use": [{"keyword": "Kafka", "level": "REQUIRED"}], "do_not_use": ["PHP"]},
               "tailored": {"summary": "Data platform lead", "top_bullets": ["Cut incidents by 45%"]}}
    prompt = build_cover_letter_prompt(context)
    assert prompt.version == "v6"
    for label in ("TAILORED RESUME", "JOB REQUIREMENTS", "REQUIREMENT MAP", "KEYWORD TARGETS", "Own Kafka pipelines"):
        assert label in prompt.user, label
    assert "do_not_use" in prompt.system
