"""Phase 5 checkpoint: a safe end-to-end dry run of the application worker, ending at READY_TO_SUBMIT.

    python scripts/application_dry_run.py postgresql+psycopg://localhost/jobhunt_test

What it does, all locally (nothing reaches the internet, nothing is submitted):
  1. migrates a disposable schema (`jobhunt_application_dry_run`) in the given test database and seeds one account:
     a profile, two saved answers (one sensitive) and one approved job whose posting is a Greenhouse-shaped fixture
     form served from 127.0.0.1;
  2. queues the application worker in ASSIST mode and runs the real `application_execution` scheduler pass with real
     Chromium (the ephemeral application browser), until it waits for the person;
  3. confirms the sensitive answer as the person would, and runs the pass again;
  4. prints the stage trail, every request the form server received by method (POST count must be 0), the Running
     workers row, the orchestration node and edge counts — then drops the schema.

The database name must contain "test" (like TEST_DATABASE_URL). Needs Chromium (`python -m playwright install
chromium`).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMA = "jobhunt_application_dry_run"


def main(url: str) -> int:
    from sqlalchemy import create_engine, make_url, text
    from sqlalchemy.orm import sessionmaker

    if "test" not in (make_url(url).database or ""):
        print("Refusing: use a disposable database whose name contains 'test'.")
        return 2
    from app.db import AppSession
    from app.db_migrations import run_migrations
    from app.settings import get_settings

    settings = get_settings()
    settings.agent_graphs_enabled, settings.application_execution_mode = True, "assist"
    settings.llm_provider, settings.rag_retrieval_mode, settings.job_insights_enabled = "mock", "off", False
    settings.mcp_packet_dir = tempfile.mkdtemp(prefix="jobhunt-dry-run-packets-")

    engine = create_engine(url, connect_args={"options": f"-csearch_path={SCHEMA}"})
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    try:
        run_migrations(engine)
        sessions = sessionmaker(bind=engine, class_=AppSession, autoflush=False)
        return _run(sessions)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()


def _run(sessions) -> int:
    from sqlalchemy import select

    from app.infrastructure.application_browser import set_application_sessions
    from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions
    from app.infrastructure.browser.playwright_service import PlaywrightBrowserService
    from app.models import AgentThread, ApplicationExecutionAttempt, Job
    from app.services import application_execution, orchestration, workers
    from tests import application_support as aps

    browser = PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False)
    set_application_sessions(EphemeralApplicationSessions(browser, record_telemetry=False))
    trail: list[str] = []
    with aps.FormServer() as server:
        user = aps.owner(sessions)
        ids = aps.standard_answers(sessions, user)
        job_id = aps.approved_job(sessions, user, server.url())
        task_id = aps.enqueue(sessions, user, job_id)
        trail.append(f"enqueued task {task_id} for job {job_id}: {aps.task_of(sessions, job_id).stage}")

        async def run() -> None:
            try:
                await aps.drive_async(sessions)
                waiting = aps.task_of(sessions, job_id)
                trail.append(f"after pass 1: status={waiting.status} stage={waiting.stage} "
                             f"interrupt={json.dumps(waiting.interrupt)}")
                with sessions() as db:
                    db.info["user_id"] = user
                    application_execution.confirm_answers(db, task_id, [ids["auth"]])
                trail.append(f"the person confirmed sensitive answer {ids['auth']}: "
                             f"stage={aps.task_of(sessions, job_id).stage}")
                await aps.drive_async(sessions)
            finally:
                await browser.stop()

        asyncio.run(run())
        requests = list(server.requests)
    set_application_sessions(None)

    task = aps.task_of(sessions, job_id)
    trail.append(f"after pass 2: status={task.status} stage={task.stage} outcome={task.outcome}")
    with sessions() as db:
        db.info["user_id"] = user
        thread = db.get(AgentThread, task.agent_thread_id)
        attempts = db.scalars(select(ApplicationExecutionAttempt).where(
            ApplicationExecutionAttempt.task_id == task.id)).all()
        job = db.get(Job, job_id)
        job_line = f"status={job.status} application={job.application.status} state={job.state.value}"
        job_status = job.status
        result = dict(thread.result or {})
        row = workers.rows(db, user, "application")["rows"][0]
        now = datetime.now(UTC).replace(tzinfo=None)
        scope = orchestration.Scope(viewer_id=user, user_id=user, start=now - timedelta(days=1), end=now)
        edges = {key: orchestration.edge_detail(db, scope, *key)["count"]
                 for key in (("approval", "application_execution"), ("application_execution", "application"))}
        node = orchestration.task_metrics(db, scope, orchestration.TASK_BY_NODE["application_execution"])
        plan = {fid: (f["status"], f["source"]) for fid, f in (task.plan or {}).get("fields", {}).items()}

    print("\n== Stage trail ==")
    print("\n".join(f"  {line}" for line in trail))
    print("\n== Graph result (ids, enums and counts only) ==")
    print(f"  {json.dumps(result, sort_keys=True)}")
    print("\n== Field plan (sources, never values) ==")
    for fid, (status, source) in plan.items():
        print(f"  {fid:58} {status:20} {source}")
    print("\n== Attempts ==")
    for a in attempts:
        print(f"  attempt {a.attempt}: reached {a.stage_reached}, outcome {a.outcome}, files {a.file_digests}")
    print("\n== Requests the form server received ==")
    for method, path in requests:
        print(f"  {method:5} {path}")
    posts = [p for m, p in requests if m != "GET"]
    print(f"  non-GET requests: {len(posts)}")
    print("\n== Job afterwards ==")
    print(f"  {job_line}")
    print("\n== Running workers row ==")
    print(f"  {json.dumps({k: row[k] for k in ('task_id', 'kind', 'status', 'stage', 'attempt', 'max_attempts')})}")
    print("\n== AI Orchestration ==")
    print(f"  node application_execution tasks: {json.dumps({k: node[k] for k in ('tasks', 'completed', 'failed')})}")
    print(f"  edges: {json.dumps({f'{a}->{b}': n for (a, b), n in edges.items()})}")
    ok = task.stage == "READY_TO_SUBMIT" and not posts and job_status == "approved"
    print(f"\n{'PASS' if ok else 'FAIL'}: ready to submit, nothing posted, nothing recorded as applied")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
