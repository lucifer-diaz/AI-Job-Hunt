"""Old vs new: every representative case through the current qualification path and through the graph (Phase 3).

    python scripts/qualification_parity.py --database-url postgresql+psycopg://me@localhost/jobhunt_parity_test
    python scripts/qualification_parity.py --database-url ... --json

Each case is built twice: in an account qualified by the current path (QUALIFICATION_GRAPH_MODE=off) and in one
qualified by the graph (on). Both run through the real scheduler passes (ranking, JD parsing, JD recovery,
qualification) with the same scripted ranking answers. Parsing uses the free mock provider, and postings come from a
local server with a fake browser, so no paid or external call is made. The report shows, per case:
  * where each path left the job;
  * whether the state and the transition history agree (the current path's description wait left out);
  * the model calls and parses each path spent.

The database must be a disposable PostgreSQL database whose name contains "test". The script works in its own
schema (qualification_parity), drops it at the end, and never touches DATABASE_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "qualification_parity"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="a disposable PostgreSQL database (name contains 'test')")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    from sqlalchemy.engine import make_url

    try:  # the database name, also for a socket URL (…/name?host=/tmp), whose last "/" is in the query
        url = make_url(args.database_url)
    except Exception:  # noqa: BLE001
        parser.error("--database-url is not a database URL")
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or ""):
        parser.error("use a disposable PostgreSQL database whose name contains 'test'")
    os.environ.update({"DATABASE_URL": args.database_url, "LLM_PROVIDER": "mock", "AGENT_GRAPHS_ENABLED": "true",
                       "RAG_RETRIEVAL_MODE": "off", "RAG_VECTOR_ENABLED": "false", "AGENT_LANGSMITH_EXPORT_ENABLED":
                       "false", "SCHEDULER_ENABLED": "false"})
    sys.path.insert(0, str(ROOT))

    from sqlalchemy import create_engine, text

    from app.agents import checkpoint
    from app.db_migrations import run_migrations
    from app.services import job_description, ranker, user_settings
    from app.settings import get_settings
    from tests import qualification_support as qs
    from tests.jd_support import FakeBrowser, PostingServer
    from tests.test_ranker import FakeModel

    engine = create_engine(args.database_url, connect_args={"options": f"-csearch_path={SCHEMA}"})
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    try:
        run_migrations(engine)
        checkpoint.forget_setup()
        settings = get_settings()
        for name, value in (("agent_graphs_enabled", True), ("jd_fetch_enabled", True), ("jd_parse_enabled", True),
                            ("jd_fetch_allow_private_hosts", True), ("llm_provider", "mock"),
                            ("rag_retrieval_mode", "off"), ("rag_vector_enabled", False)):
            setattr(settings, name, value)
        user_settings.ranking_interval_minutes = lambda db, user_id: 0
        model = FakeModel()
        ranker.LLMClient = model
        counters = qs.Counters()
        original = job_description.parse_job_description

        def counted(db, job, **kwargs):
            counters.parses.append(job.id)
            return original(db, job, **kwargs)

        job_description.parse_job_description = counted
        sessions = qs.app_sessions(engine)

        def set_mode(mode: str) -> None:
            settings.qualification_graph_mode = mode

        with PostingServer() as server:
            rows = qs.report(sessions, set_mode, model, counters, server, FakeBrowser)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(qs.markdown(rows))
        agree = sum(r["same_state"] and r["same_history"] for r in rows)
        print(f"\n{agree}/{len(rows)} cases agree on state and history; "
              f"{sum(qs.no_more_paid_work(r) for r in rows)}/{len(rows)} spend no more paid calls on the graph.")
    return 0 if all(r["same_state"] and r["same_history"] and qs.no_more_paid_work(r) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
