"""AI Orchestration API: the system topology, live packets and one job's path, as a read-only projection.

Every endpoint is observational. Nothing here changes a job, a model assignment or a route: editing a worker's model
stays in AI Orchestration → Models (draft → preview → apply → version). The browser never talks to LiteLLM; these
endpoints read the Job Hunt OS database (business tables and Monitoring telemetry) and return metadata only.

Accounts: a person sees their own jobs and telemetry. The admin may pass all_accounts=true for aggregate node and edge
numbers across every account; packets, job titles and traces always stay the signed-in account's own.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.services import monitoring, orchestration
from app.services import orchestration_topology as topo

router = APIRouter(prefix="/api/orchestration")


def orchestration_scope(user: CurrentUser = Depends(current_user),
                        range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002  query parameter name
                        start: datetime | None = None, end: datetime | None = None,
                        all_accounts: bool = False) -> orchestration.Scope:
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account")
    begin, finish = monitoring.time_range(range, start, end)
    if finish < begin:
        raise HTTPException(422, "end must be after start")
    return orchestration.Scope(viewer_id=user.id, user_id=None if all_accounts else user.id, start=begin, end=finish,
                               range_name=range)


ScopeDep = Depends(orchestration_scope)


@contextmanager
def scoped(scope: orchestration.Scope, db: Session) -> Iterator[Session]:
    """The request's session only sees the signed-in account (app.tenancy). The admin's every-account aggregates read
    through an unscoped session; every query in app.services.orchestration still filters by account explicitly."""
    if not scope.all_accounts:
        yield db
        return
    from app.db import SessionLocal

    with SessionLocal() as everyone:
        yield everyone


@router.get("/topology")
def topology() -> dict[str, Any]:
    """The stages, their kinds and the transitions between them. Static: it changes with the code, not with jobs."""
    return topo.public()


@router.get("/graph")
def graph(scope: orchestration.Scope = ScopeDep, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Topology, current model assignments, node status and summary metrics, edge throughput for the range."""
    with scoped(scope, db) as session:
        return orchestration.graph(session, scope)


@router.get("/live")
def live(scope: orchestration.Scope = ScopeDep, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """What is in flight now: node queue counts and active packets. The lightweight poll."""
    with scoped(scope, db) as session:
        return orchestration.live(session, scope)


@router.get("/summary")
def summary(scope: orchestration.Scope = ScopeDep, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Node queue counts only (no packets): the cheapest poll."""
    with scoped(scope, db) as session:
        counts = orchestration.live_counts(session, scope.user_id)
    return {"scope": scope.public(), "counts": counts,
            "running": sum(c["running"] for c in counts.values()),
            "queued": sum(c["queued"] + c["retrying"] for c in counts.values())}


@router.get("/jobs")
def jobs(q: str = Query("", max_length=120), limit: int = Query(20, ge=1, le=50),
         user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The live-job selector: your own jobs by id, title or company."""
    return {"items": orchestration.search_jobs(db, user.id, q, limit)}


@router.get("/jobs/{job_id}")
def job(job_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Where one job is now and the path it took: each stage completed, current, waiting, failed or skipped."""
    found = orchestration.job_state(db, job_id, user.id)
    if found is None:
        raise HTTPException(404, "Job not found")
    return found


@router.get("/jobs/{job_id}/live")
def job_live(job_id: int, user: CurrentUser = Depends(current_user),
             db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Only the packet (no path): what the selected-job poll needs."""
    found = orchestration.job_state(db, job_id, user.id)
    if found is None:
        raise HTTPException(404, "Job not found")
    return {"packet": found["packet"], "current": next((s for s in found["path"] if s.get("current")), None)}


@router.get("/jobs/{job_id}/trace")
def job_trace(job_id: int, user: CurrentUser = Depends(current_user),
              db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Workflow, task, LLM, external-call and log events for one job, in timestamp order. Metadata only."""
    found = orchestration.job_trace(db, job_id, user.id)
    if found is None:
        raise HTTPException(404, "Job not found")
    return found


@router.get("/nodes/{node_id}")
def node(node_id: str, scope: orchestration.Scope = ScopeDep, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """One stage in depth: assignment, performance, routing, recent activity and the advisor's recommendations."""
    with scoped(scope, db) as session:
        found = orchestration.node_detail(session, scope, node_id)
    if found is None:
        raise HTTPException(404, "Unknown stage")
    return found


@router.get("/nodes/{node_id}/history")
def node_history(node_id: str, scope: orchestration.Scope = ScopeDep,
                 db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Model assignment history from applied configuration versions, with what was observed in each period."""
    with scoped(scope, db) as session:
        found = orchestration.model_history(session, scope, node_id)
    if found is None:
        raise HTTPException(404, "Unknown stage")
    return found


@router.get("/edges/{source}/{target}")
def edge(source: str, target: str, scope: orchestration.Scope = ScopeDep,
         db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """One transition: how many items took it in the range, the split with its siblings, and timing where stored."""
    with scoped(scope, db) as session:
        found = orchestration.edge_detail(session, scope, source, target)
    if found is None:
        raise HTTPException(404, "Unknown transition")
    return found
