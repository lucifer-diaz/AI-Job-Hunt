"""Old vs new: representative packets through the current tailoring path and through the preparation graph (Phase 4).

    python scripts/preparation_parity.py --database-url postgresql+psycopg://me@localhost/jobhunt_parity_test
    python scripts/preparation_parity.py --database-url ... --json

Each case is built twice: in an account tailored by the current path (the tailor worker, then the review decision
Claude Desktop makes through review_results) and in one prepared by the graph (PREPARATION_GRAPH_MODE=on). Both use
the free mock Tailor and the same scripted Reviewer, so no paid or external call is made. The accounts' resumes are
unverified text, so the candidate policy allows unverified resume facts here (as the test suite's lenient default).
The report shows, per case: whether the first tailored version (units and letter) is the same, whether the first review
decision is the same, where each path left the job, how many versions each made, that neither wrote the source resume
or ranked, and that no version with an unsupported claim was passed.

The database must be a disposable PostgreSQL database whose name contains "test". The script works in its own
schema (preparation_parity), drops it at the end, and never touches DATABASE_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "preparation_parity"


def markdown(rows: list[dict]) -> str:
    head = ("| Case | Same first version | Same first decision | Current path | Graph | Versions current/graph | "
            "Source resume unchanged | Unsupported claim passed |\n| --- | --- | --- | --- | --- | --- | --- | --- |")
    yes = lambda v: "yes" if v else "**no**"
    lines = [f"| {r['case']} | {yes(r['same_tailoring'])} | {yes(r['same_first_decision'])} | "
             f"{r['current']['first_decision']} → {r['current']['status']} | "
             f"{r['graph']['first_decision']} → {r['graph']['status']} | "
             f"{r['current']['versions']}/{r['graph']['versions']} | "
             f"{yes(r['current']['source_resume_unchanged'] and r['graph']['source_resume_unchanged'])} | "
             f"{'**yes**' if r['graph']['passed_with_unsupported'] else 'no'} |" for r in rows]
    return "\n".join([head, *lines])


def agrees(row: dict) -> bool:
    return (row["same_tailoring"] and row["same_first_decision"] and not row["graph"]["passed_with_unsupported"]
            and row["current"]["source_resume_unchanged"] and row["graph"]["source_resume_unchanged"]
            and row["current"]["rankings"] == row["graph"]["rankings"] == 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="a disposable PostgreSQL database (name contains 'test')")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    from sqlalchemy.engine import make_url

    try:
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
    from app.services import user_settings
    from tests import preparation_support as ps
    from tests import qualification_support as qs

    engine = create_engine(args.database_url, connect_args={"options": f"-csearch_path={SCHEMA}"})
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    try:
        run_migrations(engine)
        checkpoint.forget_setup()
        user_settings.CANDIDATE_POLICY_DEFAULTS["allow_unverified_resume_facts"] = True
        patch = ps.Patcher()
        ps.configure(patch, "on")
        try:
            rows = ps.report(qs.app_sessions(engine))
        finally:
            patch.undo()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(markdown(rows))
        print(f"\n{sum(agrees(r) for r in rows)}/{len(rows)} cases agree on the first version and decision, with the "
              "source resume untouched, no ranking and no unsupported claim passed.")
    return 0 if all(agrees(r) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
