"""The model stage of Job Insights: organise fetched research, then write interview preparation.

    source adapters → stored observations (ids) → research view (no salaries, accepted matches only)
      → COMPANY_INSIGHTS (app.prompts.company_insights) → validated against the ids it was shown
      → INTERVIEW_INSIGHTS (app.prompts.interview_insights) with the structured JD, the fit analysis and verified
        evidence → validated against those requirement and evidence ids

Both calls go through the centralized LLM gateway (LLMClient.generate_prompt), so the worker's model comes from its
configuration and every request is in Monitoring with its worker, prompt version, tokens and trace. A model never sees
a salary and never produces one. An answer citing an id it was not shown is rejected outright; an item in the wrong
list (a review theme offered as an official value) is dropped. Either call failing leaves the deterministic sections
standing (the caller marks the run PARTIAL).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Evidence, Job
from app.services import tracing

REVIEW_SOURCES = ("GLASSDOOR", "INDEED", "PUBLIC_WEB")
RESEARCH_CATEGORIES = ("COMPANY", "VALUES", "CULTURE", "SENTIMENT", "INTERVIEW_PROCESS", "INTERVIEW_QUESTION")
MAX_EVIDENCE = 20


class SynthesisRejected(ValueError):
    """The model cited something it was not shown."""


@dataclass
class Synthesis:
    company: Any = None  # CompanyInsightSynthesis
    interview: Any = None  # InterviewPrepResult
    model: str | None = None
    prompt_versions: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    requirement_labels: dict[str, str] = field(default_factory=dict)
    evidence_labels: dict[int, str] = field(default_factory=dict)


def research_view(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the company worker reads: accepted, non-compensation observations with their ids. No salary ever."""
    out = []
    for o in observations:
        if o.get("match_status") != "ACCEPTED" or o.get("category") not in RESEARCH_CATEGORIES:
            continue
        year = (o.get("observed_at") or "")[:4] or None
        out.append({"id": o["id"], "category": o["category"], "source_type": o["source_type"],
                    "title": o.get("title"), "year": year, "data": o.get("data") or {}})
    return out


def _check_ids(ids: list[int], known: set[int], where: str, problems: list[str]) -> None:
    unknown = [i for i in ids if i not in known]
    if unknown:
        problems.append(f"{where} cites unknown source ids {unknown[:5]}")


def validate_company(answer: Any, observations: list[dict[str, Any]], dropped: list[str]) -> Any:
    """Every cited id must be one of the observations shown; items in the wrong list are dropped (and noted)."""
    by_id = {o["id"]: o for o in observations}
    known = set(by_id)
    problems: list[str] = []
    _check_ids(answer.summary_source_ids, known, "company_summary", problems)
    for name in ("profile_facts", "official_values", "culture_themes", "interview_stages", "reported_questions",
                 "risks_and_unknowns"):
        for item in getattr(answer, name):
            _check_ids(item.supporting_source_ids, known, name, problems)
    if problems:
        raise SynthesisRejected("; ".join(problems))

    def cited(item: Any, test: Callable[[dict[str, Any]], bool]) -> bool:
        return bool(item.supporting_source_ids) and all(test(by_id[i]) for i in item.supporting_source_ids)

    official = lambda o: o["category"] == "VALUES" and o["source_type"] == "OFFICIAL_COMPANY"
    reviews = lambda o: o["category"] in ("CULTURE", "SENTIMENT") and o["source_type"] != "OFFICIAL_COMPANY"
    questions = lambda o: o["category"] == "INTERVIEW_QUESTION"
    keep = [v for v in answer.official_values if cited(v, official)]
    dropped += [f"official value '{v.value}' not stated on an official page" for v in answer.official_values
                if v not in keep]
    answer.official_values = keep
    keep = [t for t in answer.culture_themes if cited(t, reviews)]
    dropped += [f"culture theme '{t.theme}' not from employee reviews" for t in answer.culture_themes if t not in keep]
    answer.culture_themes = keep
    keep = [q for q in answer.reported_questions if cited(q, questions)]
    dropped += [f"reported question without a reporting source: '{q.question[:60]}'" for q in answer.reported_questions
                if q not in keep]
    answer.reported_questions = keep
    for stage in answer.interview_stages:
        if stage.origin == "REPORTED" and not stage.supporting_source_ids:
            stage.origin = "INFERRED"  # a stage nothing reports is never shown as reported
        if stage.origin == "INFERRED":
            stage.supporting_source_ids = []
    answer.interview_stages = [s for s in answer.interview_stages if s.origin == "REPORTED" or s.rationale]
    answer.profile_facts = [f for f in answer.profile_facts if f.field and f.supporting_source_ids]
    answer.risks_and_unknowns = [r for r in answer.risks_and_unknowns
                                 if r.kind == "UNKNOWN" or r.supporting_source_ids]
    for r in answer.risks_and_unknowns:
        if r.kind == "UNKNOWN":
            r.supporting_source_ids = []
    return answer


def validate_interview(answer: Any, *, requirement_ids: set[str], evidence_ids: set[int], value_ids: set[int]) -> Any:
    problems: list[str] = []
    for q in answer.generated_questions:
        if unknown := [i for i in q.requirement_ids + q.gap_requirement_ids if i not in requirement_ids]:
            problems.append(f"question cites unknown requirement ids {unknown[:5]}")
        if unknown := [i for i in q.evidence_ids if i not in evidence_ids]:
            problems.append(f"question cites unknown evidence ids {unknown[:5]}")
    for area in answer.focus_areas:
        if unknown := [i for i in area.requirement_ids if i not in requirement_ids]:
            problems.append(f"focus area cites unknown requirement ids {unknown[:5]}")
    for prep in answer.value_prep:
        if prep.value_source_id not in value_ids:
            problems.append(f"value preparation cites {prep.value_source_id}, which is not an official value")
        if unknown := [i for i in prep.evidence_ids if i not in evidence_ids]:
            problems.append(f"value preparation cites unknown evidence ids {unknown[:5]}")
    if problems:
        raise SynthesisRejected("; ".join(problems[:6]))
    return answer


# ───────────── interview context: reused from the structured JD, the ranking and the evidence bank ─────────────

def _fit_view(analysis: dict[str, Any]) -> dict[str, Any]:
    from app.prompts.common import compact

    assessments = [{"requirement_id": a.get("requirement_id"), "status": a.get("status")}
                   for a in analysis.get("requirement_assessments") or [] if a.get("requirement_id")]
    return compact({"strong_matches": analysis.get("strong_matches"), "partial_matches": analysis.get("partial_matches"),
                    "missing_requirements": analysis.get("missing_requirements"),
                    "fatal_gaps": analysis.get("fatal_gaps"), "requirement_assessments": assessments[:40]})


def interview_context(db: Session, job: Job) -> dict[str, Any]:
    """The posting's interview context (job_context.build_interview_insights_job_context, unchanged), the Ranker's
    completed fit analysis when there is one, and the verified evidence relevant to this job."""
    from app.services import job_context, ranker
    from app.services.context_builder import ContextError, completed_fit_analysis
    from app.services.tracker import stage_label

    context = job_context.build_interview_insights_job_context(db, job).to_dict()
    try:
        analysis = completed_fit_analysis(db, job)
    except ContextError:
        analysis = {}
    evidence = ranker.relevant_evidence(db, job.user_id, job, MAX_EVIDENCE)
    cited = [i for i in analysis.get("relevant_evidence_ids") or [] if i not in {e["id"] for e in evidence}]
    if cited:
        rows = db.scalars(select(Evidence).where(Evidence.id.in_(cited[:10]), Evidence.user_id == job.user_id,
                                                 Evidence.verified.is_(True))).all()
        evidence = [{"id": r.id, "company": r.company, "role": r.role, "claim": r.claim} for r in rows] + evidence
    stage = db.scalar(select(Application.stage).where(Application.job_id == job.id,
                                                      Application.status == "applied"))
    requirements = {r["id"]: r.get("normalized_value") or r.get("source_text") or r["id"]
                    for key in ("required", "preferred") for r in context.get(key) or [] if r.get("id")}
    requirements.update({r["id"]: r.get("normalized_value") or r["id"]
                         for r in (context.get("derived_signals") or {}).get("derived_requirements") or []
                         if r.get("id")})
    return {"context": {"job_context": context}, "fit": _fit_view(analysis) if analysis else {},
            "evidence": [{"id": e["id"], "claim": e.get("claim"), "role": e.get("role"), "company": e.get("company")}
                         for e in evidence[:MAX_EVIDENCE]],
            "stage": stage_label(stage) if stage else None, "requirements": requirements}


def run(sessions: Callable[[], Session], *, user_id: int, job_id: int, attempt: int,
        observations: list[dict[str, Any]]) -> Synthesis:
    """Both model calls. Never raises: a failure is recorded in `errors` and that part stays None."""
    from app.prompts import build_company_insights_prompt, build_interview_insights_prompt
    from app.prompts.company_insights import COMPANY_INSIGHTS_PROMPT_VERSION
    from app.prompts.contracts import CompanyInsightSynthesis, InterviewPrepResult
    from app.prompts.interview_insights import INTERVIEW_INSIGHTS_PROMPT_VERSION
    from app.services.llm import LLMCall, LLMClient, LLMError
    from app.services.user_settings import ai_ready

    out = Synthesis()
    research = research_view(observations)
    with sessions() as db:
        db.info["user_id"] = user_id
        ready, why = ai_ready(db, user_id)
        if not ready:
            out.errors.append(f"No model call: {why}")
            return out
        job = db.get(Job, job_id)
        if job is None:
            out.errors.append("The job was deleted")
            return out
        client = LLMClient.for_user(db, user_id)
        facts = {"title": job.title, "company": job.company, "location": job.location}
        with tracing.trace("job_insights", job_id=job_id, user_id=user_id):
            if research:
                try:
                    prompt = build_company_insights_prompt(facts, research)
                    result = client.generate_prompt(prompt, LLMCall(
                        workflow_type="job", entity_id=job_id, attempt=attempt,
                        validate=lambda data: validate_company(CompanyInsightSynthesis.model_validate(data),
                                                               research, out.dropped)))
                    out.company, out.model = result.validated, result.model
                    out.prompt_versions["company_insights"] = COMPANY_INSIGHTS_PROMPT_VERSION
                except (LLMError, SynthesisRejected, ValueError) as exc:
                    out.errors.append(f"Company research synthesis failed: {type(exc).__name__}: {exc}"[:300])
            try:
                ctx = interview_context(db, job)
            except Exception as exc:  # noqa: BLE001  preparation is optional; the research stands without it
                out.errors.append(f"Interview context unavailable: {type(exc).__name__}"[:300])
                return out
            out.requirement_labels = ctx["requirements"]
            out.evidence_labels = {e["id"]: (e.get("claim") or "")[:160] for e in ctx["evidence"]}
            values = [{"id": v.supporting_source_ids[0], "value": v.value}
                      for v in (out.company.official_values if out.company else []) if v.supporting_source_ids]
            if not values:
                values = [{"id": o["id"], "value": o["data"].get("value")} for o in research
                          if o["category"] == "VALUES" and o["source_type"] == "OFFICIAL_COMPANY"][:12]
            summary = {"official_values": values}
            if out.company:
                summary.update(company_summary=out.company.company_summary,
                               interview_stages=[{"name": s.name, "origin": s.origin}
                                                 for s in out.company.interview_stages],
                               reported_question_count=len(out.company.reported_questions))
            try:
                prompt = build_interview_insights_prompt(job=facts, context=ctx["context"], fit_analysis=ctx["fit"],
                                                         evidence=ctx["evidence"], research=summary,
                                                         stage=ctx["stage"])
                known_requirements = set(ctx["requirements"])
                known_evidence = {e["id"] for e in ctx["evidence"]}
                value_ids = {v["id"] for v in values}
                result = client.generate_prompt(prompt, LLMCall(
                    workflow_type="job", entity_id=job_id, attempt=attempt,
                    validate=lambda data: validate_interview(InterviewPrepResult.model_validate(data),
                                                             requirement_ids=known_requirements,
                                                             evidence_ids=known_evidence, value_ids=value_ids)))
                out.interview, out.model = result.validated, out.model or result.model
                out.prompt_versions["interview_insights"] = INTERVIEW_INSIGHTS_PROMPT_VERSION
            except (LLMError, SynthesisRejected, ValueError) as exc:
                out.errors.append(f"Interview preparation failed: {type(exc).__name__}: {exc}"[:300])
    return out
