"""One Job Insights research run, from a claimed task to a saved insight package.

    claim → load an immutable job snapshot (one short session)
          → sources side by side, bounded (JOB_INSIGHTS_SOURCE_CONCURRENCY); fresh cached observations reused
          → store observations (lease-guarded)          ← provenance for everything below
          → model synthesis (app.services.job_insights.synthesis)
          → deterministic sections: compensation (app.services.compensation), values, sentiment, questions
          → save the sections that this run produced, finish the task (lease-guarded)

A source that fails keeps the observations it found last time; a section is only replaced by a run that produced
it; recruiter notes are never written by research. So one provider failing never erases good data, and a run where
every outside source and the model failed keeps everything and is retried.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Application, InsightObservation, Job, JobInsight
from app.services import backoff, compensation, system_log, tracing, worker_settings
from app.services.job_insights import matching, synthesis
from app.services.job_insights.apify import (
    GlassdoorCompensationSource,
    GlassdoorInterviewSource,
    GlassdoorReviewsSource,
    PayScaleSource,
)
from app.services.job_insights.base import (
    BLOCKED,
    FAILED,
    NEEDS_REVIEW,
    NO_DATA,
    NOT_CONFIGURED,
    OK,
    ObservationIn,
    Snapshot,
    SourceResult,
)
from app.services.job_insights.sources import (
    OfficialCompanySource,
    PostingSource,
    PublicWebSource,
    RecruiterSource,
)
from app.services.job_insights.tasks import (
    COMPLETED,
    KIND,
    PARTIAL,
    SKIPPED,
    WORKER,
    InsightClaim,
    finish,
    held,
    release,
    utcnow,
)
from app.services.lifecycle import ErrorCategory
from app.settings import get_settings

OUTCOMES = ("completed", "partial", "failed", "retrying", "skipped", "lost")
# Every source, in priority order (the screen lists them in this order). Tests replace this list with fakes.
SOURCES: list[Any] = [PostingSource(), RecruiterSource(), OfficialCompanySource(), GlassdoorCompensationSource(),
                      GlassdoorInterviewSource(), GlassdoorReviewsSource(), PayScaleSource(), PublicWebSource()]
LOCAL_SOURCES = ("job_posting", "recruiter")  # read stored data only; they never fail for outside reasons
ANSWERED = (OK, NO_DATA, NEEDS_REVIEW)
# Sections the company synthesis shapes: a run without it never replaces a previous run's version.
SYNTHESIZED = ("company", "interview_process", "reported_questions", "risks", "unknowns")
WEBSITE_KEYS = ("company_website", "companyWebsite", "company_url", "companyUrl", "employer_website",
                "employerWebsite", "company.website", "company.url", "hiringOrganization.sameAs",
                "hiringOrganization.url")
# Hosts that are job boards or applicant-tracking systems, never the employer's own website.
BOARD_HOSTS = ("linkedin.com", "indeed.", "glassdoor.", "naukri.com", "greenhouse.io", "lever.co", "workday",
               "ashbyhq.com", "smartrecruiters.com", "jobstreet", "monster.", "ziprecruiter", "wellfound.com",
               "angel.co", "myworkdayjobs.com", "bamboohr.com", "workable.com", "icims.com", "taleo.net",
               "google.com", "apify.com")


def _dig(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def posting_website(raw: dict[str, Any] | None) -> str | None:
    """The employer's website as the job board reported it, never a board or ATS host, never guessed."""
    for key in WEBSITE_KEYS:
        value = _dig(raw or {}, key)
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, str)), None)
        if not isinstance(value, str) or "." not in value or " " in value.strip():
            continue
        url = value.strip() if "://" in value else f"https://{value.strip()}"
        host = matching.domain_of(url) or ""
        if host and not any(board in host for board in BOARD_HOSTS):
            return f"https://{host}"
    return None


def ensure_insight(db: Session, job: Job) -> JobInsight:
    """The job's insight row, created on first use; its matching identity refreshed from the job (never the person's
    own website or recruiter notes)."""
    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    if insight is None:
        insight = JobInsight(user_id=job.user_id, job_id=job.id, status="NONE")
        db.add(insight)
    role = matching.normalize_title(job.title)
    place = matching.parse_location(job.location)
    insight.company_name, insight.canonical_company = (job.company or "")[:300], matching.canonical_company(job.company)
    insight.original_title, insight.normalized_title = (job.title or "")[:300], role.normalized[:300]
    insight.seniority, insight.discipline = role.seniority, role.discipline
    insight.city, insight.country = place.city, place.country
    if insight.company_website_source != "USER":
        website = posting_website(job.raw)
        insight.company_website, insight.company_website_source = website, "POSTING" if website else None
    insight.company_domain = matching.domain_of(insight.company_website)
    db.flush()
    return insight


def observation_dict(row: InsightObservation) -> dict[str, Any]:
    return {"id": row.id, "category": row.category, "source_type": row.source_type, "source_name": row.source_name,
            "provider": row.provider, "actor_id": row.actor_id, "source_url": row.source_url,
            "source_reference": row.source_reference, "title": row.title, "match_scope": row.match_scope,
            "match_status": row.match_status, "match_reason": row.match_reason, "confidence": row.confidence,
            "recency": row.recency, "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None, "data": row.data or {}}


def _ttl_days(source: Any) -> int:
    settings = get_settings()
    return {"compensation": settings.compensation_insight_ttl_days, "interview": settings.interview_insight_ttl_days}\
        .get(getattr(source, "ttl", "company"), settings.company_insight_ttl_days)


def cached(db: Session, *, user_id: int, insight_id: int, source: Any, key: str,
           now: datetime) -> list[ObservationIn] | None:
    """The account's own latest observations from this source for the same company / role / location, when still
    fresh. Never another account's: research is per account, like everything it owns."""
    o = InsightObservation
    latest = db.scalar(select(func.max(o.fetched_at)).where(
        o.user_id == user_id, o.cache_key == key, o.source_name == source.name, o.job_insight_id != insight_id))
    if latest is None or latest < now - timedelta(days=_ttl_days(source)):
        return None
    rows = db.scalars(select(o).where(o.user_id == user_id, o.cache_key == key, o.source_name == source.name,
                                      o.fetched_at == latest, o.job_insight_id != insight_id).order_by(o.id)).all()
    insight_ids = {r.job_insight_id for r in rows}
    if len(insight_ids) > 1:  # one batch only: the newest insight's copy
        newest = max(insight_ids)
        rows = [r for r in rows if r.job_insight_id == newest]
    return [ObservationIn(r.category, r.source_type, dict(r.data or {}), r.confidence, title=r.title,
                          source_url=r.source_url, source_reference=r.source_reference, provider=r.provider,
                          actor_id=r.actor_id, observed_at=r.observed_at, match_scope=r.match_scope,
                          match_status=r.match_status, match_reason=r.match_reason, recency=r.recency,
                          cache_key=r.cache_key, fetched_at=r.fetched_at) for r in rows]


# ───────────── 1. snapshot ─────────────

def load(sessions: Callable[[], Session], claim_: InsightClaim) -> tuple[Snapshot, dict[str, list[ObservationIn]]] | str:
    from app.services.job_description import is_current
    from app.services.user_settings import apify_token

    now = utcnow()
    with sessions() as db:
        task = held(db, claim_)
        if task is None:
            return "lost"
        db.info["user_id"] = task.user_id
        job = db.get(Job, task.job_id)
        if job is None:
            finish(task, SKIPPED, now, "The job was deleted", ErrorCategory.VALIDATION)
            db.commit()
            return "skipped"
        insight = ensure_insight(db, job)
        application = db.scalar(select(Application).where(Application.job_id == job.id))
        recruiter = dict(insight.recruiter_input or {})
        if application is not None and application.recruiter_name and not recruiter.get("recruiter_name"):
            recruiter["recruiter_name"] = application.recruiter_name
        target = matching.target_for(job.company, job.title, job.location, insight.company_website)
        snapshot = Snapshot(
            task_id=task.id, job_id=job.id, user_id=task.user_id,
            application_id=application.id if application else None, title=job.title or "",
            company=job.company or "", location=job.location or "", target=target, website=insight.company_website,
            posting_salary=(job.compensation or {}).get("posting") or compensation.extract_posting_salary(job.raw),
            board_salary={"salary_min": job.salary_min, "salary_max": job.salary_max,
                          "salary_currency": job.salary_currency, "salary_interval": job.salary_interval,
                          "salary_source": job.salary_source},
            structured=dict(job.structured_description or {}) if is_current(job) else {},
            recruiter=recruiter, force=bool(task.force_refresh), now=now, attempt=task.attempt_count,
            apify_token=apify_token(db, task.user_id))
        reused: dict[str, list[ObservationIn]] = {}
        if not snapshot.force:
            for source in SOURCES:
                if getattr(source, "cacheable", False) and not source.configured():
                    key = matching.cache_key(source.name, target, company_level=source.company_level)
                    found = cached(db, user_id=task.user_id, insight_id=insight.id, source=source, key=key, now=now)
                    if found:
                        reused[source.name] = found
        db.commit()
    return snapshot, reused


# ───────────── 2. sources ─────────────

def _safe_fetch(source: Any, snapshot: Snapshot) -> SourceResult:
    try:
        with tracing.trace(job_id=snapshot.job_id, user_id=snapshot.user_id):
            result = source.fetch(snapshot)
        if not isinstance(result, SourceResult):
            return SourceResult(source.name, FAILED, detail="The source returned nothing usable")
        return result
    except Exception as exc:  # noqa: BLE001  one source failing never fails the others
        return SourceResult(source.name, FAILED, detail=f"{type(exc).__name__}"[:200])


def collect(snapshot: Snapshot, reused: dict[str, list[ObservationIn]]) -> list[SourceResult]:
    """Every source, side by side and bounded. A source that hangs past its time is recorded as failed."""
    settings = get_settings()
    results: dict[str, SourceResult] = {}
    to_run = []
    for source in SOURCES:
        if source.name in reused:
            results[source.name] = SourceResult(source.name, OK, reused[source.name], cached=True,
                                                detail="Reused recent research for the same company and role")
        elif (why := source.configured()) is not None:
            results[source.name] = SourceResult(source.name, NOT_CONFIGURED, detail=why)
        else:
            to_run.append(source)
    if to_run:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, settings.job_insights_source_concurrency),
                                                     thread_name_prefix="job-insights")
        try:
            futures = {pool.submit(contextvars.copy_context().run, _safe_fetch, s, snapshot): s for s in to_run}
            done, pending = concurrent.futures.wait(futures, timeout=settings.job_insights_source_timeout_seconds + 30)
            for future in done:
                results[futures[future].name] = future.result()
            for future in pending:
                results[futures[future].name] = SourceResult(futures[future].name, FAILED, detail="Timed out")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return [results[s.name] for s in SOURCES if s.name in results]


# ───────────── 3. observations ─────────────

def store(sessions: Callable[[], Session], claim_: InsightClaim, results: list[SourceResult],
          now: datetime) -> list[dict[str, Any]] | None:
    """Replace the observations of every source that answered; keep those of sources that did not. Lease-guarded.
    Returns every observation the insight now has, or None when the lease was lost."""
    with sessions() as db:
        task = held(db, claim_)
        if task is None:
            return None
        db.info["user_id"] = task.user_id
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == task.job_id))
        if insight is None:
            return None
        answered = [r for r in results if r.status in ANSWERED]
        if answered:
            db.execute(delete(InsightObservation).where(InsightObservation.job_insight_id == insight.id,
                                                        InsightObservation.source_name.in_([r.name for r in answered]))
                       .execution_options(synchronize_session=False))
        for result in answered:
            for o in result.observations:
                db.add(InsightObservation(
                    user_id=task.user_id, job_insight_id=insight.id, category=o.category, source_type=o.source_type,
                    source_name=result.name, provider=o.provider, actor_id=o.actor_id,
                    source_url=(o.source_url or "")[:1200] or None, source_reference=o.source_reference,
                    title=(o.title or "")[:300] or None, match_scope=o.match_scope, match_status=o.match_status,
                    match_reason=(o.match_reason or "")[:300] or None, confidence=max(0.0, min(1.0, o.confidence)),
                    recency=o.recency, observed_at=o.observed_at, fetched_at=o.fetched_at or now,
                    cache_key=o.cache_key, data=o.data, created_at=now))
        labels = {s.name: s.label for s in SOURCES}
        task.source_status = {r.name: {**r.summary(now), "label": labels.get(r.name, r.name)} for r in results}
        db.commit()
        rows = db.scalars(select(InsightObservation).where(InsightObservation.job_insight_id == insight.id)
                          .order_by(InsightObservation.id)).all()
        return [observation_dict(r) for r in rows]


# ───────────── 4. sections ─────────────

def _accepted(observations: list[dict[str, Any]], category: str, *sources: str) -> list[dict[str, Any]]:
    return [o for o in observations if o["category"] == category and o["match_status"] == "ACCEPTED"
            and (not sources or o["source_type"] in sources)]


def _src(observations_by_id: dict[int, dict[str, Any]], ids: list[int]) -> list[dict[str, Any]]:
    """Short provenance for each cited observation, for the screen."""
    out = []
    for i in ids:
        o = observations_by_id.get(i)
        if o:
            out.append({"id": i, "source_type": o["source_type"], "title": o.get("title"),
                        "url": o.get("source_url"), "observed_at": o.get("observed_at"),
                        "fetched_at": o.get("fetched_at")})
    return out


def sentiment(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Employee-reported ratings: an aggregate the source states, else the mean of individual reviews. Never fact."""
    rows = _accepted(observations, "SENTIMENT")
    if not rows:
        return None
    aggregate = [r for r in rows if r["data"].get("aggregate")]
    use = aggregate[:1] or rows
    metrics: dict[str, Any] = {}
    for name in ("overall_rating", "career_opportunities", "compensation_rating", "work_life_balance", "management",
                 "culture"):
        values = [r["data"][name] for r in use if isinstance(r["data"].get(name), (int, float))]
        if values:
            metrics[name] = round(sum(values) / len(values), 2)
    if not metrics:
        return None
    sample = use[0]["data"].get("sample_size") if aggregate else len(use)
    return {**metrics, "sample_size": sample, "source": use[0]["source_type"], "kind": "FACT",
            "label": "Employee-reported (not objective fact)", "retrieved_at": use[0].get("fetched_at"),
            "source_ids": [r["id"] for r in use]}


def compensation_prep(comp: dict[str, Any], prep: Any, profile_target: str | None) -> dict[str, Any]:
    """What to say about pay: the figures as their sources state them, and questions to ask. Never a fabricated
    employer budget, and a market estimate is never called the company's band."""
    questions = list(getattr(prep, "compensation_questions", None) or []) or [
        "Could you share the approved base salary and total-compensation band for this level?",
        "How is the level for this role decided, and what does the band look like one level up?",
        "What makes up total compensation here (bonus, equity, benefits), and how is each paid out?"]
    return {"posted": comp.get("employer_posted_range"), "recruiter_budget": comp.get("recruiter_budget"),
            "market_estimate": comp.get("market_range"), "confidence": comp.get("confidence"),
            "status": comp.get("status"), "candidate_target": (profile_target or "").strip()[:300] or None,
            "discussion_range": comp.get("recommended_discussion_range"),
            "questions": [{"question": q, "kind": "GENERATED"} for q in questions[:8]],
            "note": "Market figures are estimates from third-party data, not the employer's approved band."}


def build_sections(observations: list[dict[str, Any]], result: synthesis.Synthesis, *, job: dict[str, Any],
                   statuses: dict[str, str], website: str | None, profile_target: str | None) -> dict[str, Any]:
    """Every section this run can produce, each item labelled FACT, DERIVED, GENERATED or UNKNOWN. Pure."""
    by_id = {o["id"]: o for o in observations}
    sections: dict[str, Any] = {}
    comp_obs = [o for o in _accepted(observations, "COMPENSATION")]
    comp = compensation.compensation_insight(comp_obs, role_title=job["title"], normalized_title=job["normalized"],
                                             company=job["company"], location=job["location"])
    sections["compensation"] = comp
    sections["compensation_prep"] = compensation_prep(comp, result.interview, profile_target)

    company = result.company
    mission = [{"text": o["data"]["text"], "kind": "FACT", "source_ids": [o["id"]], "sources": _src(by_id, [o["id"]])}
               for o in _accepted(observations, "COMPANY", "OFFICIAL_COMPANY") if o["data"].get("field") == "mission"]
    benefits = [{"text": o["data"]["text"], "kind": "FACT", "source_ids": [o["id"]], "source_type": o["source_type"]}
                for o in _accepted(observations, "COMPANY") if o["data"].get("field") == "benefit"]
    remote = next(({"text": o["data"]["text"], "kind": "FACT", "source_ids": [o["id"]]}
                   for o in _accepted(observations, "COMPANY", "JOB_DESCRIPTION")
                   if o["data"].get("field") == "remote_policy"), None)
    if company is not None:
        official = [{"value": v.value, "kind": "FACT", "source_ids": v.supporting_source_ids,
                     "sources": _src(by_id, v.supporting_source_ids)} for v in company.official_values]
        themes = [{"theme": t.theme, "sentiment": t.sentiment, "kind": "DERIVED", "label": "Employee-reported",
                   "source_ids": t.supporting_source_ids} for t in company.culture_themes]
        summary = ({"text": company.company_summary, "kind": "DERIVED", "source_ids": company.summary_source_ids}
                   if company.company_summary and company.summary_source_ids else None)
        facts = [{"field": f.field, "value": f.value, "kind": "FACT", "source_ids": f.supporting_source_ids}
                 for f in company.profile_facts]
    else:  # no synthesis: the official values exactly as the pages state them, and no themes or summary
        official = [{"value": o["data"]["value"], "kind": "FACT", "source_ids": [o["id"]],
                     "sources": _src(by_id, [o["id"]])}
                    for o in _accepted(observations, "VALUES", "OFFICIAL_COMPANY") if o["data"].get("value")][:15]
        themes, summary, facts = [], None, []
    company_section = {"name": job["company"], "website": website, "summary": summary, "profile_facts": facts,
                       "mission": mission[:2], "official_values": official, "culture_themes": themes,
                       "sentiment": sentiment(observations), "benefits": benefits[:12], "remote_policy": remote}
    if any(company_section[k] for k in ("summary", "profile_facts", "mission", "official_values", "culture_themes",
                                        "sentiment", "benefits", "remote_policy")):
        sections["company"] = company_section

    recruiter_process = _accepted(observations, "INTERVIEW_PROCESS", "RECRUITER")
    reported_process = _accepted(observations, "INTERVIEW_PROCESS")
    stages: list[dict[str, Any]] = []
    for o in recruiter_process[:1]:
        stages += [{"name": name[:80], "order": i + 1, "origin": "REPORTED", "kind": "FACT", "source_ids": [o["id"]],
                    "source_type": "RECRUITER"} for i, name in enumerate(o["data"].get("stages") or [])]
    if not stages and company is not None:
        stages = [{"name": s.name, "order": s.order, "origin": s.origin,
                   "kind": "FACT" if s.origin == "REPORTED" else "DERIVED", "source_ids": s.supporting_source_ids,
                   "rationale": s.rationale or None} for s in sorted(company.interview_stages, key=lambda s: s.order)]
    if stages or reported_process:
        reported_stages = [s for s in stages if s["origin"] == "REPORTED"]
        sections["interview_process"] = {
            "stages": stages,
            "estimated_rounds": {"value": len(reported_stages), "kind": "FACT"} if reported_stages else None,
            "durations": [{"text": o["data"]["duration"], "kind": "FACT", "source_ids": [o["id"]]}
                          for o in reported_process if o["data"].get("duration")][:5],
            "difficulty": [{"text": o["data"]["difficulty"], "kind": "FACT", "source_ids": [o["id"]]}
                           for o in reported_process if o["data"].get("difficulty")][:5],
            "notes": [{"text": o["data"]["text"], "kind": "FACT", "source_ids": [o["id"]]}
                      for o in reported_process if o["data"].get("text")][:6],
            "confidence": "MEDIUM" if reported_stages else "LOW"}

    question_obs = _accepted(observations, "INTERVIEW_QUESTION")
    if company is not None and company.reported_questions:
        reported = [{"question": q.question, "category": q.category, "stage": q.stage, "source_ids":
                     q.supporting_source_ids} for q in company.reported_questions]
    else:
        reported = [{"question": o["data"]["question"], "category": "OTHER", "stage": o["data"].get("stage") or
                     "UNKNOWN", "source_ids": [o["id"]]} for o in question_obs if o["data"].get("question")]
    recruiter_q = {o["id"] for o in question_obs if o["source_type"] == "RECRUITER"}
    reported += [{"question": by_id[i]["data"]["question"], "category": "OTHER", "stage": "UNKNOWN", "source_ids": [i]}
                 for i in recruiter_q if not any(i in r["source_ids"] for r in reported)]
    for item in reported:
        first = by_id.get(item["source_ids"][0]) if item["source_ids"] else None
        item.update(origin="REPORTED", kind="FACT", sources=_src(by_id, item["source_ids"]),
                    source_type=first["source_type"] if first else None,
                    reported_role=first.get("title") if first else None,
                    reported_at=first.get("observed_at") if first else None,
                    recency=first.get("recency") if first else None,
                    confidence=first.get("confidence") if first else None)
    if reported or question_obs:
        sections["reported_questions"] = reported

    prep = result.interview
    if prep is not None:
        sections["generated_questions"] = [
            {"id": f"q{i + 1}", "question": q.question, "origin": q.origin, "category": q.category, "stage": q.stage,
             "difficulty": q.difficulty, "why_likely": q.rationale, "requirement_ids": q.requirement_ids,
             "requirements": [result.requirement_labels.get(r, r) for r in q.requirement_ids],
             "evidence_ids": q.evidence_ids, "gap_requirement_ids": q.gap_requirement_ids,
             "gaps": [result.requirement_labels.get(r, r) for r in q.gap_requirement_ids],
             "preparation": q.preparation or None, "kind": "GENERATED",
             "label": "Generated preparation — not reported by candidates"} for i, q in enumerate(prep.generated_questions)]
        sections["questions_to_ask"] = [{"stage": q.stage, "question": q.question, "context": q.context or None,
                                         "kind": "GENERATED"} for q in prep.questions_to_ask]
        sections["focus_areas"] = [{"area": f.area, "why": f.why, "requirement_ids": f.requirement_ids,
                                    "kind": "DERIVED"} for f in prep.focus_areas]
        sections["value_prep"] = [{"value": by_id.get(v.value_source_id, {}).get("data", {}).get("value"),
                                   "value_source_ids": [v.value_source_id], "question": v.question,
                                   "evidence_ids": v.evidence_ids,
                                   "evidence": [result.evidence_labels.get(e, "") for e in v.evidence_ids],
                                   "kind": "GENERATED", "label": "Generated preparation"} for v in prep.value_prep]

    risks = [{"text": r.text, "kind": "DERIVED", "source_ids": r.supporting_source_ids}
             for r in (company.risks_and_unknowns if company else []) if r.kind == "RISK"]
    unknowns = [{"text": r.text, "kind": "UNKNOWN"} for r in (company.risks_and_unknowns if company else [])
                if r.kind == "UNKNOWN"]
    if comp["employer_posted_range"] is None:
        unknowns.append({"text": "The posting does not state a salary.", "kind": "UNKNOWN"})
    if not official:
        unknowns.append({"text": "No official company values were found.", "kind": "UNKNOWN"})
    if not reported:
        unknowns.append({"text": "No interview questions reported by candidates were found.", "kind": "UNKNOWN"})
    not_configured = [name for name, status in statuses.items() if status == NOT_CONFIGURED]
    if not_configured:
        unknowns.append({"text": f"Not researched (source not configured): {', '.join(not_configured)}.",
                         "kind": "UNKNOWN"})
    sections["risks"], sections["unknowns"] = risks, unknowns
    return sections


def run_status(results: list[SourceResult], result: synthesis.Synthesis) -> str:
    """COMPLETED, PARTIAL or FAILED. FAILED only when every outside source that ran failed and so did the model."""
    attempted = [r for r in results if r.status != NOT_CONFIGURED and r.name not in LOCAL_SOURCES]
    failed = [r for r in attempted if r.status in (FAILED, BLOCKED)]
    model_failed = bool(result.errors)
    if attempted and len(failed) == len(attempted) and model_failed:
        return "FAILED"
    if failed or model_failed or any(r.status == NEEDS_REVIEW for r in results):
        return PARTIAL
    return COMPLETED


# ───────────── 5. settle ─────────────

def settle(sessions: Callable[[], Session], claim_: InsightClaim, results: list[SourceResult],
           result: synthesis.Synthesis, observations: list[dict[str, Any]], now: datetime) -> str:
    from app.services.workflow import get_profile

    status = run_status(results, result)
    with sessions() as db:
        task = held(db, claim_)
        if task is None:
            return "lost"
        db.info["user_id"] = task.user_id
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == task.job_id))
        job = db.get(Job, task.job_id)
        if insight is None or job is None:
            finish(task, SKIPPED, now, "The job was deleted", ErrorCategory.VALIDATION)
            db.commit()
            return "skipped"
        failures = [f"{r.name}: {r.detail or r.status}" for r in results if r.status in (FAILED, BLOCKED)]
        error = "; ".join(failures + result.errors)[:2000] or None
        insight.last_attempt_at = now
        if status == "FAILED":
            own = worker_settings.for_user(db, task.user_id, KIND)
            insight.last_error = error
            if insight.status == "NONE":
                insight.status = "FAILED"
            if task.attempt_count < own["max_attempts"]:
                release(task, now, error or "Every source failed", retry_seconds=own["retry_seconds"],
                        category=ErrorCategory.EXTERNAL_SERVICE)
                label = "retrying"
            else:
                finish(task, "FAILED", now, error, ErrorCategory.EXTERNAL_SERVICE)
                label = "failed"
            db.commit()
            return label
        profile = get_profile(db)
        sections = build_sections(
            observations, result, job={"title": job.title or "", "company": job.company or "",
                                       "location": job.location or "", "normalized": insight.normalized_title},
            statuses={r.name: r.status for r in results}, website=insight.company_website,
            profile_target=getattr(profile, "compensation_notes", None))
        for name, value in sections.items():
            if result.company is None and name in SYNTHESIZED and getattr(insight, name) is not None:
                continue  # the model did not run this time: what the last good run wrote stands
            setattr(insight, name, value)
        if result.prompt_versions:
            insight.prompt_versions = {**(insight.prompt_versions or {}), **result.prompt_versions}
            insight.model = (result.model or "")[:200] or insight.model
        if result.company is not None:
            insight.confidence = result.company.research_confidence
        insight.status = status
        insight.generated_at = insight.generated_at or now
        insight.refreshed_at = now
        insight.last_error = error
        finish(task, status, now, error)
        db.commit()
    return status.lower()


def process(sessions: Callable[[], Session], claim_: InsightClaim) -> str:
    """Research one claimed task. Returns an outcome (OUTCOMES); one task's failure never reaches the caller."""
    started = time.monotonic()
    loaded = load(sessions, claim_)
    if isinstance(loaded, str):
        return loaded
    snapshot, reused = loaded
    try:
        with backoff.bypass(claim_.run_now), tracing.trace("job_insights", job_id=snapshot.job_id,
                                                          user_id=snapshot.user_id):
            results = collect(snapshot, reused)
            observations = store(sessions, claim_, results, snapshot.now)
            if observations is None:
                return "lost"
            result = synthesis.run(sessions, user_id=snapshot.user_id, job_id=snapshot.job_id,
                                   attempt=snapshot.attempt, observations=observations)
            label = settle(sessions, claim_, results, result, observations, utcnow())
    except Exception as exc:  # noqa: BLE001  an unexpected failure is this task's problem, retried later
        label = _crashed(sessions, claim_, f"{type(exc).__name__}")
        results, result = [], synthesis.Synthesis(errors=[type(exc).__name__])
    _log(snapshot, claim_, results, result, label, int((time.monotonic() - started) * 1000))
    return label


def _crashed(sessions: Callable[[], Session], claim_: InsightClaim, detail: str) -> str:
    with sessions() as db:
        task = held(db, claim_)
        if task is None:
            return "lost"
        own = worker_settings.for_user(db, task.user_id, KIND)
        if task.attempt_count < own["max_attempts"]:
            release(task, utcnow(), f"Unexpected error ({detail})", retry_seconds=own["retry_seconds"])
            label = "retrying"
        else:
            finish(task, "FAILED", utcnow(), f"Unexpected error ({detail})", ErrorCategory.UNKNOWN)
            label = "failed"
        db.commit()
    return label


def _log(snapshot: Snapshot, claim_: InsightClaim, results: list[SourceResult], result: synthesis.Synthesis,
         label: str, latency: int) -> None:
    level = "INFO" if label in ("completed", "partial") else "WARNING" if label in ("retrying", "skipped") \
        else "ERROR"
    system_log.log_event(
        category="WORKFLOW", level=level, operation="job_insights", workflow=WORKER, user_id=snapshot.user_id,
        entity_type="job", entity_id=snapshot.job_id, status=label, duration_ms=latency,
        message=f"Job insights {label}",
        details={"worker": WORKER, "job_id": snapshot.job_id, "task_id": claim_.task_id,
                 "sources": {r.name: r.status for r in results}, "cached": [r.name for r in results if r.cached],
                 "model_errors": len(result.errors), "dropped_items": len(result.dropped)})
