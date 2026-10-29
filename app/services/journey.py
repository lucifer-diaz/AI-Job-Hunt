"""One application journey, end to end: discovery → description → retrieval → model → packet → execution →
confirmation → Gmail outcome, correlated by the ids each stage already stores.

A read model, like app.services.orchestration (whose per-job trace it complements). It owns no table and writes
nothing: every stage is assembled from the rows the domain already keeps.

    discovery       jobs                                         discovered_at, source
    description     job_description_fetch_tasks / _parse_tasks   status, result code, quality
    retrieval       llm_request_logs.retrieval_ms / _mode        measured by rag_retrieval, recorded with the call
    model           llm_request_logs                             actual vs configured model, fallback, failover, cost
    packet          resume_variants, approved_packets            version, digest prefix, override
    execution       application_execution_tasks                  mode, backend, stage, result code
    confirmation    submission_intents, applications             status, the verifier's flags, confirmation id
    gmail_outcome   application_email_messages / _communication_events   match status, event type, corrections

Rules it keeps:
* Safe ids only. Ids, enums, counts, timings, model names and a 12-character digest prefix. Never a description,
  prompt, resume, answer, email subject, sender, body or page text.
* Unknown is UNKNOWN. A stage with no stored fact says so; nothing is inferred from a neighbouring stage.
* Cost is counted once. LLM cost comes from llm_request_logs; an external row adds its cost only when no LLM row
  points at it (external_request_id). An unpriced row leaves the total NULL-safe: `cost` is None when nothing was
  priced, and the unpriced count is shown beside it.
* Gmail stays confidence-aware. A message the matcher could not place with confidence is NEEDS_REVIEW and is never
  counted as this application's outcome; a person's correction wins over the classifier and is labelled `person`.
* Model quality and the recruiter's decision are separate facts. `recruiter_outcome` reports the tracker stage and
  is never attributed to a model, a packet or a stage.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationExecutionTask,
    ApprovedPacket,
    ExternalAPIRequestLog,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    LLMRequestLog,
    ResumeVariant,
    SubmissionIntent,
)

STAGES = ("discovery", "description", "retrieval", "model", "packet", "execution", "confirmation", "gmail_outcome")
# Every status a stage can show. UNKNOWN: no stored fact.
STATUSES = ("DONE", "IN_PROGRESS", "WAITING", "FAILED", "BLOCKED", "NEEDS_REVIEW", "UNCERTAIN", "NOT_REACHED",
            "UNKNOWN")
ROW_LIMIT = 200
REVIEW_SCAN_LIMIT = 200  # the account's open NEEDS_REVIEW messages read to find candidates naming this application
DIGEST_PREFIX = 12

_TASK_STATUS = {"PENDING": "WAITING", "PROCESSING": "IN_PROGRESS", "COMPLETED": "DONE", "FAILED": "FAILED",
                "BLOCKED": "BLOCKED", "SKIPPED": "DONE"}
_INTENT_STATUS = {"ARMED": "IN_PROGRESS", "CLICKING": "IN_PROGRESS", "CONFIRMED": "DONE", "UNCERTAIN": "UNCERTAIN",
                  "ABORTED": "NOT_REACHED", "RESOLVED_SUBMITTED": "DONE", "RESOLVED_NOT_SUBMITTED": "NOT_REACHED"}
_EXECUTION_STATUS = {"READY_TO_SUBMIT": "DONE", "CONFIRMED": "DONE", "NEEDS_INPUT": "WAITING", "NEEDS_HUMAN": "WAITING",
                     "BLOCKED": "BLOCKED", "FAILED": "FAILED", "SUBMISSION_UNCERTAIN": "UNCERTAIN",
                     "CANCELLED": "NOT_REACHED"}


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


def _stage(stage: str, status: str, *, source: list[str], at: datetime | None = None, **facts: Any) -> dict[str, Any]:
    assert status in STATUSES, status
    return {"stage": stage, "status": status, "source": source, "at": _iso(at), **facts}


def _unknown(stage: str, source: list[str], why: str) -> dict[str, Any]:
    return _stage(stage, "UNKNOWN", source=source, detail=why)


# ───────────── stages ─────────────

def _discovery(job: Job) -> dict[str, Any]:
    return _stage("discovery", "DONE", source=["jobs"], at=job.discovered_at, job_id=job.id,
                  board=job.source_board or job.source or None,
                  domain_match=job.domain_match)


def _description(db: Session, job: Job, user_id: int) -> dict[str, Any]:
    fetch = db.scalars(select(JobDescriptionFetchTask).where(JobDescriptionFetchTask.job_id == job.id,
                                                             JobDescriptionFetchTask.user_id == user_id)
                       .order_by(JobDescriptionFetchTask.id.desc()).limit(5)).all()
    parse = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id,
                                                             JobDescriptionParseTask.user_id == user_id)
                       .order_by(JobDescriptionParseTask.id.desc()).limit(5)).all()
    if not fetch and not parse:
        return _unknown("description", ["job_description_fetch_tasks", "job_description_parse_tasks"],
                        "No recovery or parse task recorded for this job")
    latest = parse[0] if parse else fetch[0]
    return _stage("description", _TASK_STATUS.get(latest.status, "UNKNOWN"),
                  source=["job_description_fetch_tasks", "job_description_parse_tasks"],
                  at=getattr(latest, "completed_at", None) or latest.created_at,
                  fetch_task_ids=[t.id for t in fetch], parse_task_ids=[t.id for t in parse],
                  fetch_result=getattr(fetch[0], "result_code", None) if fetch else None,
                  parse_status=parse[0].status if parse else None,
                  description_source=job.description_source, quality=job.description_quality)


def _llm_rows(db: Session, job: Job, application: Application | None, user_id: int) -> list[Any]:
    where = and_(LLMRequestLog.workflow_type == "job", LLMRequestLog.workflow_entity_id == job.id)
    if application is not None:
        where = or_(where, and_(LLMRequestLog.workflow_type == "application",
                                LLMRequestLog.workflow_entity_id == application.id))
    return db.execute(select(LLMRequestLog.id, LLMRequestLog.created_at, LLMRequestLog.stage,
                             LLMRequestLog.worker_type, LLMRequestLog.status, LLMRequestLog.model,
                             LLMRequestLog.requested_model, LLMRequestLog.configured_model,
                             LLMRequestLog.fallback_used, LLMRequestLog.routing_cause,
                             LLMRequestLog.failed_over_from, LLMRequestLog.estimated_cost,
                             LLMRequestLog.cost_source, LLMRequestLog.retrieval_ms, LLMRequestLog.retrieval_mode,
                             LLMRequestLog.trace_id, LLMRequestLog.external_request_id, LLMRequestLog.retry_count)
                      .where(LLMRequestLog.user_id == user_id, where)
                      .order_by(LLMRequestLog.created_at, LLMRequestLog.id).limit(ROW_LIMIT)).all()


def _retrieval(rows: list[Any]) -> dict[str, Any]:
    measured = [r for r in rows if r.retrieval_mode]
    if not measured:
        return _unknown("retrieval", ["llm_request_logs"], "No retrieval recorded with this job's model calls")
    timed = [r.retrieval_ms for r in measured if r.retrieval_ms is not None]
    modes = Counter(r.retrieval_mode for r in measured)
    return _stage("retrieval", "DONE", source=["llm_request_logs"], at=measured[-1].created_at,
                  llm_request_ids=[r.id for r in measured], modes=dict(modes),
                  retrieval_ms_max=max(timed) if timed else None,
                  retrieval_ms_total=sum(timed) if timed else None)


def _model(rows: list[Any]) -> dict[str, Any]:
    if not rows:
        return _unknown("model", ["llm_request_logs"], "No model call recorded for this job")
    calls = []
    for r in rows:
        calls.append({"id": r.id, "at": _iso(r.created_at), "stage": r.stage, "worker": r.worker_type,
                      "status": r.status, "actual_model": r.model, "requested_model": r.requested_model,
                      "configured_model": r.configured_model,
                      # Unknown stays unknown: a row that never recorded which model answered is not "matched".
                      "matches_configuration": (r.model == r.configured_model) if r.model and r.configured_model
                      else None,
                      "fallback_used": r.fallback_used, "failover": r.routing_cause == "FAILOVER",
                      "failed_over_from": r.failed_over_from, "retry_count": r.retry_count,
                      "cost": r.estimated_cost, "cost_source": r.cost_source, "trace_id": r.trace_id})
    failed = [r for r in rows if r.status != "SUCCEEDED"]
    status = "DONE" if len(failed) < len(rows) else "FAILED"
    return _stage("model", status, source=["llm_request_logs"], at=rows[-1].created_at, calls=calls,
                  actual_models=sorted({r.model for r in rows if r.model}),
                  configured_models=sorted({r.configured_model for r in rows if r.configured_model}))


def _packet(db: Session, job: Job, application: Application | None, user_id: int) -> dict[str, Any]:
    variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job.id, ResumeVariant.user_id == user_id)
                        .order_by(ResumeVariant.version.desc()).limit(1))
    approved = None
    if application is not None:
        approved = db.scalar(select(ApprovedPacket).where(ApprovedPacket.application_id == application.id,
                                                          ApprovedPacket.user_id == user_id,
                                                          ApprovedPacket.revoked_at.is_(None))
                             .order_by(ApprovedPacket.id.desc()).limit(1))
    if variant is None and approved is None:
        return _unknown("packet", ["resume_variants", "approved_packets"], "No tailored version saved")
    status = "DONE" if approved is not None else "WAITING"
    return _stage("packet", status, source=["resume_variants", "approved_packets"],
                  at=approved.created_at if approved else variant.created_at,
                  variant_id=variant.id if variant else None, variant_version=variant.version if variant else None,
                  approved_packet_id=approved.id if approved else None,
                  digest_prefix=approved.digest[:DIGEST_PREFIX] if approved else None,
                  approval_override=bool(approved.override) if approved else None)


def _execution(db: Session, job: Job, user_id: int) -> tuple[dict[str, Any], list[ApplicationExecutionTask]]:
    tasks = list(db.scalars(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job.id,
                                                                   ApplicationExecutionTask.user_id == user_id)
                            .order_by(ApplicationExecutionTask.id.desc()).limit(10)).all())
    if not tasks:
        return _unknown("execution", ["application_execution_tasks"], "No execution run for this job"), tasks
    t = tasks[0]
    status = (_EXECUTION_STATUS.get(t.stage) or _TASK_STATUS.get(t.status, "UNKNOWN")) if t.status != "PROCESSING" \
        else "IN_PROGRESS"
    return _stage("execution", status, source=["application_execution_tasks"], at=t.completed_at or t.updated_at,
                  execution_task_ids=[x.id for x in tasks], mode=t.mode, backend=t.backend, route=t.route,
                  execution_stage=t.stage, task_status=t.status, outcome=t.outcome, result_code=t.result_code,
                  attempts=t.attempt_count), tasks


def _confirmation(db: Session, job: Job, application: Application | None, user_id: int) -> dict[str, Any]:
    intent = db.scalar(select(SubmissionIntent).where(SubmissionIntent.job_id == job.id,
                                                      SubmissionIntent.user_id == user_id)
                       .order_by(SubmissionIntent.id.desc()).limit(1))
    if intent is not None:
        ev = intent.evidence or {}
        return _stage("confirmation", _INTENT_STATUS.get(intent.status, "UNKNOWN"),
                      source=["submission_intents", "applications"], at=intent.resolved_at or intent.clicking_at,
                      intent_id=intent.id, intent_status=intent.status, claim_id=intent.claim_id,
                      evidence="verified_browser" if intent.status == "CONFIRMED" else None,
                      # The verifier's flags only: never page text.
                      form_gone=ev.get("form_gone"), confirmation_wording=ev.get("confirmation_wording"),
                      confirmation_path=ev.get("confirmation_path"),
                      site_responses=len(ev.get("responses") or []),
                      confirmation_id=application.confirmation_id if application else None)
    if application is not None and application.applied_at is not None:
        # Recorded by a person or an MCP client: a report, not the site's evidence.
        return _stage("confirmation", "DONE", source=["applications"], at=application.applied_at,
                      evidence="reported", submitted_via=application.submitted_via,
                      confirmation_id=application.confirmation_id)
    return _unknown("confirmation", ["submission_intents", "applications"], "Not submitted")


def _effective(event: ApplicationCommunicationEvent) -> tuple[str, str]:
    """The event type a person settled on, else the classifier's. ('event_type', 'person' | 'classifier')."""
    for c in reversed(event.corrections or []):
        if c.get("field") == "event_type" and c.get("new"):
            return str(c["new"]), "person"
    return event.event_type, "person" if event.classification_method == "USER" else "classifier"


def review_messages(db: Session, application_id: int, user_id: int) -> list[Any]:
    """Messages waiting for a person that name this application among their candidates. An ambiguous message is
    linked to no application (application_inbox.matching never guesses), so this is the only way to find it."""
    return [m for m in db.execute(select(ApplicationEmailMessage.id, ApplicationEmailMessage.match_candidates,
                                         ApplicationEmailMessage.received_at, ApplicationEmailMessage.match_score)
                                  .where(ApplicationEmailMessage.user_id == user_id,
                                         ApplicationEmailMessage.match_status == "NEEDS_REVIEW")
                                  .order_by(ApplicationEmailMessage.id.desc()).limit(REVIEW_SCAN_LIMIT)).all()
            if any(c.get("application_id") == application_id for c in (m.match_candidates or []))]


def _gmail(db: Session, application: Application | None, user_id: int) -> dict[str, Any]:
    source = ["application_email_messages", "application_communication_events"]
    if application is None:
        return _unknown("gmail_outcome", source, "No application to match email to")
    messages = db.execute(select(ApplicationEmailMessage.id, ApplicationEmailMessage.match_status,
                                 ApplicationEmailMessage.match_score, ApplicationEmailMessage.received_at)
                          .where(ApplicationEmailMessage.user_id == user_id,
                                 ApplicationEmailMessage.application_id == application.id)
                          .order_by(ApplicationEmailMessage.received_at.desc()).limit(50)).all()
    linked = {x.id for x in messages}
    review = [m for m in review_messages(db, application.id, user_id) if m.id not in linked]
    events = db.scalars(select(ApplicationCommunicationEvent)
                        .where(ApplicationCommunicationEvent.user_id == user_id,
                               ApplicationCommunicationEvent.application_id == application.id)
                        .order_by(ApplicationCommunicationEvent.received_at.desc()).limit(50)).all()
    if not messages and not review and not events:
        return _unknown("gmail_outcome", source, "No email matched to this application")
    rows = []
    for e in events:
        effective, by = _effective(e)
        rows.append({"event_id": e.id, "message_id": e.message_id, "at": _iso(e.received_at),
                     "event_type": effective, "decided_by": by, "classifier_event_type": e.event_type,
                     "confidence": e.confidence, "stage_decision": e.stage_decision, "needs_review": e.needs_review,
                     "review_status": e.review_status, "user_corrected": bool(e.user_corrected)})
    matched = [m for m in messages if m.match_status == "MATCHED"]
    open_review = review or [m for m in messages if m.match_status == "NEEDS_REVIEW"] or \
        [r for r in rows if r["needs_review"] and r["review_status"] == "OPEN" and not r["user_corrected"]]
    if open_review:
        status = "NEEDS_REVIEW"
    elif matched or rows:
        status = "DONE"
    else:
        status = "UNKNOWN"
    return _stage("gmail_outcome", status, source=source,
                  at=max((m.received_at for m in messages if m.received_at), default=None),
                  matched_message_ids=[m.id for m in matched],
                  review_message_ids=sorted({m.id for m in review} |
                                            {m.id for m in messages if m.match_status == "NEEDS_REVIEW"}),
                  events=rows)


# ───────────── cost, counted once ─────────────

def job_cost(db: Session, job: Job, application: Application | None, user_id: int,
             llm: list[Any] | None = None) -> dict[str, Any]:
    """Reported cost for one job: its LLM rows plus the external rows no LLM row already accounts for.

    `cost` is None when no row carried a reported or priced figure; `unpriced` counts rows whose cost is unknown."""
    llm = llm if llm is not None else _llm_rows(db, job, application, user_id)
    linked = {r.external_request_id for r in llm if r.external_request_id}
    ext_where = ExternalAPIRequestLog.job_id == job.id
    if application is not None:
        ext_where = or_(ext_where, ExternalAPIRequestLog.application_id == application.id)
    external = db.execute(select(ExternalAPIRequestLog.request_id, ExternalAPIRequestLog.estimated_cost,
                                 ExternalAPIRequestLog.trace_id)
                          .where(ExternalAPIRequestLog.user_id == user_id, ext_where)
                          .limit(ROW_LIMIT)).all()
    own = [r for r in external if r.request_id not in linked]

    def total(rows: list[Any]) -> float | None:
        priced = [float(r.estimated_cost) for r in rows if r.estimated_cost is not None]
        return round(sum(priced), 6) if priced else None
    llm_cost, external_cost = total(llm), total(own)
    return {"cost": None if llm_cost is None and external_cost is None
            else round((llm_cost or 0.0) + (external_cost or 0.0), 6),
            "llm_cost": llm_cost, "external_cost": external_cost,
            "unpriced": sum(1 for r in llm if r.estimated_cost is None) +
                        sum(1 for r in own if r.estimated_cost is None),
            "external_rows": len(own), "external_rows_linked_to_llm": len(external) - len(own),
            "trace_ids": sorted({r.trace_id for r in [*llm, *external] if r.trace_id})}


# ───────────── the journey ─────────────

def journey(db: Session, job_id: int, user_id: int) -> dict[str, Any] | None:
    """The account's own job, every stage in order. None when the job is not this account's."""
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        return None
    application = db.scalar(select(Application).where(Application.job_id == job.id, Application.user_id == user_id))
    llm = _llm_rows(db, job, application, user_id)
    execution, _tasks = _execution(db, job, user_id)
    stages = [_discovery(job), _description(db, job, user_id), _retrieval(llm), _model(llm),
              _packet(db, job, application, user_id), execution, _confirmation(db, job, application, user_id),
              _gmail(db, application, user_id)]
    cost = job_cost(db, job, application, user_id, llm)
    return {"job_id": job.id, "application_id": application.id if application else None,
            "stages": stages, "cost": cost, "trace_ids": cost["trace_ids"],
            # The recruiter's decision, beside the journey and never attributed to a model or a stage.
            "recruiter_outcome": {"stage": application.stage if application else None,
                                  "status": application.status if application else None,
                                  "attributed_to_model": False},
            "unknown_stages": [s["stage"] for s in stages if s["status"] == "UNKNOWN"],
            "note": "Metadata only: ids, statuses, timings and model names. A rejection is the recruiter's decision "
                    "and is never read as a model's mistake."}
