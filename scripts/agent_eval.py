"""Run the Phase 7 evaluation suites and their review gates (app/services/agent_eval).

    python scripts/agent_eval.py                          # every suite: a summary and the gate per suite
    python scripts/agent_eval.py --suite confirmation     # one suite (repeatable)
    python scripts/agent_eval.py --report docs/evals/phase7-report.md   # also write the Markdown report
    python scripts/agent_eval.py --json                   # every metric, gate and failing case
    python scripts/agent_eval.py --jsonl                  # the cases as LangSmith Examples, for a manual import

Exits 0 only when every gate passes (PASS); 1 when any is BLOCKED. Suites needing a database run in a throwaway
SQLite file (never DATABASE_URL); providers are mocked and LangSmith export is off, so nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _answer_resolver(fixture: dict) -> object:
    """answer_resolver.plan against the dataset's synthetic profile and knowledge bank, in the throwaway database."""
    from app.db import SessionLocal, engine
    from app.db_migrations import run_migrations
    from app.models import CandidateProfile, Job, KnowledgeAnswer, User
    from app.services import answer_resolver, knowledge
    from app.services.ats_forms import ApplicationForm, FormField, SubmitControl
    from app.services.lifecycle import WorkflowState as S

    run_migrations(engine)
    with SessionLocal() as db:
        user = User(email="ats-eval@jobhunt.test", password_hash="!")
        db.add(user)
        db.flush()
        db.info["user_id"] = user.id
        db.add(CandidateProfile(user_id=user.id, **fixture["profile"]))
        job = Job(user_id=user.id, source="eval", source_ref="eval", title="Backend Engineer", company="Globex",
                  description="Python services.", state=S.READY_TO_APPLY, status="approved")
        db.add(job)
        sensitive = []
        for a in fixture["answers"]:
            row = KnowledgeAnswer(user_id=user.id, question=a["question"], question_key=knowledge.question_key(a["question"]),
                                  answer=a["answer"], verified=a["verified"], sensitive=a["sensitive"],
                                  category=a.get("category", "general"), answer_options=a.get("answer_options"))
            db.add(row)
            db.flush()
            if a["sensitive"]:
                sensitive.append(row.id)
        db.commit()
        user_id, job_id = user.id, job.id

    def resolve(fields: list[dict], confirm: bool) -> dict:
        with SessionLocal() as db:
            db.info["user_id"] = user_id
            form = ApplicationForm(adapter="eval", host="jobs.example.test", submit=SubmitControl(present=True),
                                   fields=[FormField(**f) for f in fields])
            out = answer_resolver.plan(db, db.get(Job, job_id), form, confirmed=sensitive if confirm else [])
            db.rollback()  # a question request is a side effect of planning; each case starts from the same bank
            return out["fields"]
    return resolve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", action="append")
    parser.add_argument("--report")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    args = parser.parse_args()
    workdir = tempfile.mkdtemp(prefix="agent-eval-")
    os.environ.update({"DATABASE_URL": f"sqlite:///{workdir}/eval.db", "LLM_PROVIDER": "mock",
                       "AGENT_LANGSMITH_EXPORT_ENABLED": "false", "SCHEDULER_ENABLED": "false"})
    for name in [k for k in os.environ if k.startswith(("LANGSMITH_", "LANGCHAIN_"))]:
        os.environ.pop(name)
    sys.path.insert(0, str(ROOT))

    from app.services.agent_eval import SUITES, dataset_for, report, suites

    names = args.suite or list(SUITES)
    unknown = sorted(set(names) - set(SUITES))
    if unknown:
        parser.error(f"unknown suite(s): {', '.join(unknown)} (known: {', '.join(SUITES)})")
    if args.jsonl:
        print("\n".join(report.jsonl(names)))
        return 0
    ctx = {}
    if "ats_answers" in names:
        ctx["resolve"] = _answer_resolver(dataset_for("ats_answers").fixture)
    results = suites.run(names, ctx)
    data = report.as_dict(results)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report.markdown(results))
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        for s in data["suites"]:
            d = s["dataset"]
            print(f"{s['gate']['status']:8} {s['name']:20} {d['name']} v{d['version']} ({d['sha256'][:12]})"
                  f"{'  ' + s['gate']['reason'] if s['gate']['reason'] else ''}")
            for case in s["failed_cases"]:
                print(f"         failing case {case['key']}: {json.dumps(case.get('outputs') or case.get('comment'))}")
        print(f"\nOverall: {data['status']}")
    return 0 if data["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
