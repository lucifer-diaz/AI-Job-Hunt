"""Helpers for the qualification graph tests: one world where the current path and the graph run side by side.

A `World` is a PostgreSQL schema with the real scheduler passes (ranking, JD parsing, JD recovery, qualification). It
has a fake ranking model (tests.test_ranker.FakeModel), the mock provider for parsing, counted, a local posting server
and a fake browser. `drive` runs the passes until nothing changes, so a job ends where the system would leave it.
`facts` is what parity compares: the job's state, status and domain verdict; its transition history (from, to,
reason; never the actor, which names the path); its task rows; and the paid work done for it.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import AppSession
from app.models import (
    AgentThread,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobQualificationTask,
    JobRanking,
    JobRankingTask,
    RemovedPosting,
    SearchRun,
    WorkflowTransition,
)
from app.services import job_description, qualification, ranker, sources
from app.services import scheduled_jobs as sj
from app.settings import get_settings

POSTING = ("Lead Python platform teams building services on Kubernetes and AWS. You will own the roadmap for our data "
           "platform, mentor five engineers, and work with product to ship reliably. We look for experience with "
           "FastAPI, distributed systems and a track record of leading delivery. You will set engineering standards, "
           "run hiring for the team, and partner with security and infrastructure on reliability goals. The role "
           "reports to the VP of Engineering and works across three time zones with a remote-first team. Benefits "
           "include equity, a learning budget and four weeks of leave.")
STUB = "View job"
TASK_MODELS = {"recovery": JobDescriptionFetchTask, "parse": JobDescriptionParseTask, "ranking": JobRankingTask}
# The current path ranks a job before its recovery ends and puts it back when the text arrives; the graph waits for
# the text instead. Those two transitions say nothing about the job, so parity leaves them out.
DESCRIPTION_WAIT = (("NORMALIZED", "SKIPPED", ranker.NO_DESCRIPTION),
                    ("SKIPPED", "NORMALIZED", "A job description arrived, so it can be ranked after all"))


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class Counters:
    parses: list[int] = field(default_factory=list)


def configure(monkeypatch, mode: str) -> None:
    settings = get_settings()
    for name, value in (("agent_graphs_enabled", True), ("qualification_graph_mode", mode),
                        ("jd_fetch_enabled", True), ("jd_parse_enabled", True), ("jd_fetch_allow_private_hosts", True),
                        ("llm_provider", "mock"), ("rag_retrieval_mode", "off"), ("rag_vector_enabled", False)):
        monkeypatch.setattr(settings, name, value)
    from app.services import user_settings

    # The per-account ranking cadence paces automatic runs; a retry must not wait for it in a test that drives passes.
    monkeypatch.setattr(user_settings, "ranking_interval_minutes", lambda db, user_id: 0)


def set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(get_settings(), "qualification_graph_mode", mode)


def count_parses(monkeypatch) -> Counters:
    counters = Counters()
    original = job_description.parse_job_description

    def counted(*args: Any, **kwargs: Any):
        job = kwargs.get("job") or next((a for a in args if isinstance(a, Job)), None)
        counters.parses.append(getattr(job, "id", 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(job_description, "parse_job_description", counted)
    return counters


def clear_checkpoints(pg_engine) -> None:
    """LangGraph's own tables are not truncated with the jobs; ids restart, so clear them too."""
    from sqlalchemy import inspect, text

    names = [t for t in ("checkpoints", "checkpoint_writes", "checkpoint_blobs") if inspect(pg_engine).has_table(t)]
    if names:
        with pg_engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {', '.join(names)}"))


def app_sessions(pg_engine) -> sessionmaker:
    return sessionmaker(bind=pg_engine, class_=AppSession, autoflush=False)


def ctx(sessions, worker: str | None = None) -> sj.JobContext:
    return sj.JobContext(sessions=sessions, trigger="schedule", now=utcnow(),
                         worker_id=worker or f"test:{uuid.uuid4().hex[:6]}", interval_minutes=1)


def snapshot(sessions) -> tuple:
    with sessions() as db:
        rows = []
        for model in (*TASK_MODELS.values(), JobQualificationTask):
            rows += [(model.__tablename__, r.id, r.status, r.attempt_count) for r in db.scalars(select(model)).all()]
        rows += [("job", j.id, str(j.state), 0) for j in db.scalars(select(Job)).all()]
        return tuple(sorted(rows))


def make_due(sessions) -> None:
    """Retry waits are part of the schedule, not of the outcome: the next pass may take every waiting task."""
    from sqlalchemy import update

    with sessions() as db:
        for model in (*TASK_MODELS.values(), JobQualificationTask):
            db.execute(update(model).where(model.status == "PENDING").values(next_attempt_at=None))
        db.commit()


def drive(sessions, *, rounds: int = 12, browser: Any = None, passes: tuple[str, ...] | None = None,
          settle: bool = True) -> int:
    """Run the scheduler passes until a whole round changes nothing (or `rounds` rounds, with settle=False)."""
    from app.infrastructure.browser import set_browser_service

    if browser is not None:
        set_browser_service(browser)
    jobs = {"qualification": sj.qualification_job, "jd_recovery": sj.jd_recovery_job, "jd_parsing": sj.jd_parsing,
            "ranking": sj.ranking}
    order = passes or ("ranking", "jd_parsing", "jd_recovery", "qualification")  # the scheduler's own order
    for done in range(1, rounds + 1):
        before = snapshot(sessions)
        for name in order:
            asyncio.run(jobs[name](ctx(sessions)))
        make_due(sessions)
        if snapshot(sessions) == before:
            return done
    if settle:
        raise AssertionError("the passes did not settle")
    return rounds


def new_job(sessions, user_id: int, *, title: str = "Engineering Manager", company: str = "Globex",
            description: str = POSTING, url: str = "", search_run_id: int | None = None, source: str = "test",
            ref: str | None = None) -> int:
    """A job as an import creates it (sources.upsert_job → discovery.admit)."""
    with sessions() as db:
        db.info["user_id"] = user_id
        job, _ = sources.upsert_job(db, source=source, source_ref=ref or uuid.uuid4().hex, title=title,
                                    company=company, url=url, description=description, user_id=user_id,
                                    search_run_id=search_run_id)
        db.commit()
        return job.id


def old_job(sessions, user_id: int, **fields: Any) -> int:
    """A job from before admission existed: no domain verdict, no description assessment, no task."""
    with sessions() as db:
        values = {"title": "Engineering Manager", "company": "Globex", "description": POSTING, **fields}
        job = Job(user_id=user_id, source="legacy", source_ref=uuid.uuid4().hex, **values)
        db.add(job)
        db.commit()
        return job.id


def search_run(sessions, user_id: int, roles: list[str]) -> int:
    with sessions() as db:
        run = SearchRun(user_id=user_id, provider="apify", external_run_id=uuid.uuid4().hex, roles=roles,
                        location="Singapore")
        db.add(run)
        db.commit()
        return run.id


def transitions(db, job_id: int) -> list[tuple[str, str, str]]:
    rows = db.scalars(select(WorkflowTransition).where(WorkflowTransition.job_id == job_id)
                      .order_by(WorkflowTransition.id)).all()
    value = lambda s: getattr(s, "value", s)  # noqa: E731
    return [(value(r.from_state), value(r.to_state), r.reason) for r in rows]


def without_description_wait(history: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    out, skip = [], 0
    for i, step in enumerate(history):
        if skip:
            skip -= 1
            continue
        if step == DESCRIPTION_WAIT[0] and i + 1 < len(history) and history[i + 1] == DESCRIPTION_WAIT[1]:
            skip = 1
            continue
        out.append(step)
    return out


def facts(sessions, job_id: int, *, title: str | None = None) -> dict[str, Any]:
    """Everything parity compares, for one job (or, when it was deleted, its tombstone)."""
    with sessions() as db:
        job = db.get(Job, job_id)
        if job is None:
            removed = db.scalar(select(RemovedPosting.reason).where(RemovedPosting.title == title).limit(1)) \
                if title else None
            return {"deleted": True, "tombstone": removed}
        tasks = {name: [(t.status, getattr(t, "result_code", None)) for t in db.scalars(
            select(m).where(m.job_id == job_id).order_by(m.id)).all()] for name, m in TASK_MODELS.items()}
        rankings = db.scalars(select(JobRanking).where(JobRanking.job_id == job_id)).all()
        return {"deleted": False, "state": getattr(job.state, "value", job.state), "status": job.status,
                "domain_match": job.domain_match, "resume_id": job.resume_id, "resume_choice": job.resume_choice,
                "history": without_description_wait(transitions(db, job_id)), "tasks": tasks,
                "rankings": len(rankings), "decision": rankings[-1].decision if rankings else None}


def qualification_tasks(sessions, job_id: int) -> list[JobQualificationTask]:
    with sessions() as db:
        rows = db.scalars(select(JobQualificationTask).where(JobQualificationTask.job_id == job_id)
                          .order_by(JobQualificationTask.id)).all()
        for r in rows:
            db.expunge(r)
        return list(rows)


def thread_of(sessions, task: JobQualificationTask) -> AgentThread | None:
    with sessions() as db:
        row = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if row is not None:
            db.expunge(row)
        return row


__all__ = ["qualification"]
