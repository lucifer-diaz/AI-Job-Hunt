"""Monitoring & AI Optimization API.

Every endpoint shows only the signed-in account's LLM telemetry. The admin may pass all_accounts=true to see every
account. Nothing here returns API keys, prompt text (unless LLM_PROMPT_DEBUG stored it) or resume and job content.
Analyses are queued for the scheduler and produce recommendations only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.models import AIOptimizationReport, AIOptimizationTask
from app.scheduler_hooks import run_soon
from app.services import ai_advisor, monitoring
from app.services.user_settings import lock_settings_row, settings_for

router = APIRouter(prefix="/api/monitoring")


def monitoring_filter(
    user: CurrentUser = Depends(current_user),
    range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002  query parameter name
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = Query(None, max_length=30),
    model: str | None = Query(None, max_length=300),
    stage: str | None = Query(None, max_length=30),
    prompt_version: str | None = Query(None, max_length=20),
    status: str | None = Query(None, pattern="^(SUCCEEDED|FAILED)$"),
    workflow_type: str | None = Query(None, max_length=30),
    job_id: int | None = None,
    entity_id: int | None = None,
    all_accounts: bool = False,
) -> monitoring.MonitoringFilter:
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    begin, finish = monitoring.time_range(range, start, end)
    if finish < begin:
        raise HTTPException(422, "end must be after start")
    if job_id is not None:
        workflow_type, entity_id = "job", job_id
    return monitoring.MonitoringFilter(start=begin, end=finish, user_id=None if all_accounts else user.id,
                                       provider=provider, model=model, stage=stage, prompt_version=prompt_version,
                                       status=status, workflow_type=workflow_type, entity_id=entity_id)


Filter = Depends(monitoring_filter)


@router.get("/dashboard")
def dashboard(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    """Everything the Monitoring tab shows, in one call."""
    return monitoring.dashboard(db, f)


@router.get("/summary")
def summary(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.summary(monitoring.rows(db, f))}


@router.get("/stages")
def stages(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), "stages": monitoring.by_stage(monitoring.rows(db, f))}


@router.get("/models")
def models(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), "models": monitoring.by_model(monitoring.rows(db, f))}


@router.get("/costs")
def costs(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    items = monitoring.rows(db, f)
    return {"filter": f.public(), "total": monitoring.summary(items)["estimated_cost"],
            "timeseries": monitoring.timeseries(items, f),
            "by_model": [{k: m[k] for k in ("provider", "model", "requests", "estimated_cost", "unpriced_requests", "free")}
                         for m in monitoring.by_model(items)],
            "by_stage": [{k: s[k] for k in ("stage", "requests", "estimated_cost", "cost_share", "unpriced_requests")}
                         for s in monitoring.by_stage(items)]}


@router.get("/latency")
def latency(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.latency(monitoring.rows(db, f))}


@router.get("/context")
def context(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.context(monitoring.rows(db, f))}


@router.get("/errors")
def errors(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.errors(monitoring.rows(db, f))}


@router.get("/requests")
def requests(f: monitoring.MonitoringFilter = Filter, page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=100),
             db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.list_requests(db, f, page, per_page)}


@router.get("/requests/{request_id}")
def request_detail(request_id: int, all_accounts: bool = False, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_user_db)):
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    detail = monitoring.request_detail(db, request_id, None if all_accounts else user.id)
    if detail is None:
        raise HTTPException(404, "LLM request not found")
    return detail


# ───────────── AI Optimization Advisor ─────────────

class AnalyzeIn(BaseModel):
    range: str = Field("7d", pattern="^(today|7d|30d|90d)$")
    start: datetime | None = None
    end: datetime | None = None


@router.post("/analyze", status_code=202)
def analyze(payload: AnalyzeIn, request: Request, background: BackgroundTasks,
            user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """Queue an analysis of this account's usage. It runs in the background and only produces recommendations."""
    begin, finish = monitoring.time_range(payload.range, payload.start, payload.end)
    try:
        task = ai_advisor.queue_analysis(db, user.id, begin, finish)
    except ai_advisor.ActiveAnalysis as exc:
        raise HTTPException(409, str(exc)) from exc
    run_soon(background, request.app, "ai_optimization")
    return {"task": ai_advisor.task_dict(task)}


@router.get("/recommendations")
def recommendations(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """The latest report, recent analysis tasks, and this account's analysis schedule."""
    report = db.scalar(select(AIOptimizationReport).order_by(AIOptimizationReport.created_at.desc(),
                                                             AIOptimizationReport.id.desc()).limit(1))
    tasks = db.scalars(select(AIOptimizationTask).order_by(AIOptimizationTask.id.desc()).limit(10)).all()
    return {"report": ai_advisor.report_dict(report) if report else None,
            "tasks": [ai_advisor.task_dict(t) for t in tasks],
            "active": any(t.status in ai_advisor.ACTIVE for t in tasks),
            "schedule": ai_advisor.schedule_of(db, user.id),
            "notice": "Recommendations only. Nothing is changed automatically; apply them yourself if you agree."}


@router.get("/reports/{report_id}")
def report(report_id: int, db: Session = Depends(get_user_db)):
    item = db.get(AIOptimizationReport, report_id)
    if item is None:
        raise HTTPException(404, "Report not found")
    return ai_advisor.report_dict(item, include_aggregates=True)


class ScheduleIn(BaseModel):
    schedule: str = Field(pattern="^(off|daily|weekly)$")


@router.put("/schedule")
def schedule(payload: ScheduleIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """The account's own choice of scheduled analysis. This is a user preference, not production configuration."""
    row = lock_settings_row(db, settings_for(db, user.id))
    preferences = dict(row.preferences or {})
    preferences["ai_advisor"] = {**(preferences.get("ai_advisor") or {}), "schedule": payload.schedule}
    row.preferences = preferences
    db.commit()
    return {"schedule": payload.schedule}
