"""Report (and optionally reconcile) approved jobs whose workflow state never reached READY_TO_APPLY (Phase 4).

    python scripts/reconcile_approved_state.py            # dry run: what would move, and what cannot
    python scripts/reconcile_approved_state.py --apply    # record the fact-backed transitions
    python scripts/reconcile_approved_state.py --json

Approvals made before Phase 4 changed only the legacy status: those jobs are still QUALIFIED (or wherever ranking left
them). `--apply` moves a job only through the stages a stored fact backs (approval.reconcile_to_ready: its approved
version → TAILORING, TAILORED; its stored review → REVIEWED; then READY_TO_APPLY), each recorded with actor
`reconcile` and the fact it stands for. A job with no such path (archived, skipped, failed, being ranked again) is
listed and left alone: nothing is invented to make it fit. Applied jobs are out of scope (Phase 5/6 moves them).
Uses DATABASE_URL; run the dry run first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="record the transitions (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Application, Job, ResumeVariant
    from app.services import approval
    from app.services.lifecycle import WorkflowState as S

    rows = []
    with SessionLocal() as db:
        found = db.execute(select(Job, Application).join(Application, Application.job_id == Job.id)
                           .where(Application.status == "approved", Job.state != S.READY_TO_APPLY)
                           .order_by(Job.id)).all()
        for job, application in found:
            db.info["user_id"] = job.user_id
            variant = db.get(ResumeVariant, application.approved_resume_id) if application.approved_resume_id \
                else None
            state = getattr(job.state, "value", job.state)
            steps = approval.path_to_ready(db, job, variant) if variant is not None else None
            row = {"job_id": job.id, "user_id": job.user_id, "state": state,
                   "path": [t.value for t, _ in steps] if steps is not None else None,
                   "reason": None if steps is not None else
                   ("no approved version" if variant is None else approval.refusal_reason(job))}
            if args.apply and steps is not None:
                approval.reconcile_to_ready(db, job, variant, actor=approval.RECONCILE_ACTOR)
                db.commit()
                row["applied"] = True
            rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        movable = [r for r in rows if r["path"] is not None]
        print(f"{len(rows)} approved job(s) behind READY_TO_APPLY: {len(movable)} reconcilable, "
              f"{len(rows) - len(movable)} left as they are" + (" (applied)" if args.apply else " (dry run)"))
        for r in rows:
            print(f"  job {r['job_id']:>6}  {r['state']:<14} " + (" → ".join(r["path"]) if r["path"] is not None
                                                                   else f"left alone: {r['reason']}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
