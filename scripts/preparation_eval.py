"""Run the Phase 4 evaluations (tests/fixtures/preparation_eval/dataset.json) and print the scores.

    python scripts/preparation_eval.py            # a summary per evaluator, then any failing case
    python scripts/preparation_eval.py --json     # every result
    python scripts/preparation_eval.py --jsonl    # the cases as LangSmith Examples, one per line, for a manual import

The evidence cases run the real checks in a throwaway SQLite database (never DATABASE_URL); every other case runs the
graphs' own rules, the provenance check and the compensation rules. Mock providers only: nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    args = parser.parse_args()
    workdir = tempfile.mkdtemp(prefix="preparation-eval-")
    os.environ.update({"DATABASE_URL": f"sqlite:///{workdir}/eval.db", "LLM_PROVIDER": "mock",
                       "AGENT_LANGSMITH_EXPORT_ENABLED": "false", "SCHEDULER_ENABLED": "false"})
    sys.path.insert(0, str(ROOT))

    from app.services import preparation_eval

    examples = preparation_eval.load()
    if args.jsonl:
        for example in examples:
            print(json.dumps(example.langsmith()))
        return 0

    from app.db import SessionLocal, engine
    from app.db_migrations import run_migrations
    from app.models import CandidateProfile, Evidence, Job, User
    from app.services import resume_verification as rv
    from app.services import user_settings
    from app.services.lifecycle import WorkflowState as S
    from app.services.tailoring_results import BulletIn, TailoredResumeIn, validate_tailored_resume
    from app.services.workflow import get_profile

    run_migrations(engine)
    user_settings.CANDIDATE_POLICY_DEFAULTS["allow_unverified_resume_facts"] = True  # the fixture resume is text
    grounded = "Led a team of eight engineers building Python and FastAPI platforms"
    resume = ("Engineering manager with twelve years building Python and FastAPI platforms. Led a team of eight "
              "engineers at Acme, moved the platform to Kubernetes on AWS and cut cloud cost by forty percent.")
    with SessionLocal() as db:
        user = User(email="eval@jobhunt.test", password_hash="!")
        db.add(user)
        db.flush()
        db.info["user_id"] = user.id
        db.add(CandidateProfile(user_id=user.id, name="Asha Rao", headline="Engineering Manager",
                                master_resume=resume, work_authorization="Singapore PR"))
        fact = Evidence(user_id=user.id, company="Acme", role="Engineering manager", claim=grounded,
                        skills=["Python", "FastAPI"], verified=True)
        job = Job(user_id=user.id, source="eval", source_ref="eval", title="Engineering Manager", company="Globex",
                  description="Lead Python platform teams.", state=S.QUALIFIED, status="shortlisted")
        db.add_all([fact, job])
        db.commit()
        user_id, fact_id, job_id = user.id, fact.id, job.id

    def claims(inputs: dict) -> list[str]:
        aliases = {"lead": [fact_id], "missing": [987654], None: []}
        bullets = [BulletIn.model_construct(text=b["text"], evidence_ids=aliases[b.get("evidence")])
                   for b in inputs.get("bullets") or []]
        with SessionLocal() as db:
            db.info["user_id"] = user_id
            job = db.get(Job, job_id)
            data = TailoredResumeIn.model_construct(job_id=job_id, summary="", skills_order=inputs.get("skills") or [],
                                                    experience_sections=[], resume_bullets=bullets, cover_letter="",
                                                    screening_answer_drafts={})
            return sorted({i["code"] for i in validate_tailored_resume(db, rv.for_job(db, get_profile(db), job), job,
                                                                         data)})

    results = preparation_eval.run(examples, claims=claims)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        summary = preparation_eval.summary(results)
        for name, row in summary["by_evaluator"].items():
            print(f"{name:24} {row['examples']:3} case(s)  mean {row['mean']:.3f}")
        print(f"\n{summary['passed']}/{summary['examples']} cases scored 1.0")
        for r in results:
            if r["score"] != 1.0:
                print(f"FAILED {r['example']}: {r['comment'] or r['outputs']}")
    return 0 if all(r["score"] == 1.0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
