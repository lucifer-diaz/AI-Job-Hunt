"""Measure candidate-evidence retrieval on the labelled benchmark (tests/fixtures/rag_eval/dataset.json).

    python scripts/rag_eval.py                       # deterministic builder vs keyword vs hybrid (concept stand-in)
    python scripts/rag_eval.py --json                # the same, machine-readable
    LITELLM_API_KEY=... python scripts/rag_eval.py --litellm-url http://localhost:4000 --model embed-small \\
        --dimensions 1536                            # adds hybrid retrieval with a real embedding model via LiteLLM

The benchmark is loaded into a throwaway SQLite database (never DATABASE_URL), so nothing is written to a real
account. Reported per variant: Recall@5/10, Precision@3/5, R-precision, citation correctness, leaks (facts of
another resume that reached a job), p50/p95 retrieval latency and token reduction against the whole evidence bank.
The concept stand-in is synthetic — it shows the fusion working, not a model's quality; only --model measures that.
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
    parser.add_argument("--litellm-url")
    parser.add_argument("--model", help="LiteLLM model name of an embedding model")
    parser.add_argument("--dimensions", type=int, help="its vector size")
    args = parser.parse_args()
    workdir = tempfile.mkdtemp(prefix="rag-eval-")
    os.environ["DATABASE_URL"] = f"sqlite:///{workdir}/rag_eval.db"
    os.environ["LLM_PROVIDER"] = "mock"
    sys.path.insert(0, str(ROOT))

    from app.db import SessionLocal, engine
    from app.db_migrations import run_migrations
    from app.models import Job, User
    from app.services import rag_eval, rag_index, rag_retrieval
    from app.services.embeddings import EmbeddingModel, EmbeddingService, LiteLLMBackend
    from app.settings import get_settings
    from tests import rag_support as rs

    run_migrations(engine)
    with SessionLocal() as db:
        user = User(email="rag-eval@example.invalid", password_hash="!")
        db.add(user)
        db.commit()
        user_id = user.id
    data = rs.load(SessionLocal, user_id)

    def evaluate(allow_model_call: bool = True) -> dict:
        with rs.session_for(SessionLocal, user_id) as db:
            def retrieve(job):
                profile, _ = rs.profile_and_job(db, job.id)
                return rag_retrieval.retrieve_evidence(db, profile, job, allow_model_call=allow_model_call)
            report = rag_eval.evaluate(db, user_id, data.cases, retrieve).as_dict()
        report.pop("per_case")
        return report

    results: dict[str, dict] = {}
    with rs.session_for(SessionLocal, user_id) as db:
        profile, _ = rs.profile_and_job(db, data.cases[0].job_id)
        rows = rag_eval.compare_builders(db, profile, [db.get(Job, c.job_id) for c in data.cases])
        forbidden = {c.job_id: c.forbidden for c in data.cases}
        relevant = {c.job_id: c.relevant for c in data.cases}
        old = {r["job_id"]: r["deterministic"] for r in rows}
        # The deterministic builder ranks by its own term score; its recall over everything it sends:
        results["deterministic builder"] = {
            "leaks": sum(len(set(ids) & forbidden[j]) for j, ids in old.items()),
            "recall_at_all": round(sum(len(set(ids) & relevant[j]) / len(relevant[j]) for j, ids in old.items())
                                   / len(old), 4),
            "mean_items_sent": round(sum(len(ids) for ids in old.values()) / len(old), 2)}
    results["keyword"] = evaluate()

    settings = get_settings()
    settings.rag_vector_enabled = True
    EmbeddingService.for_version = classmethod(lambda cls, db, uid, version: rs.concept_service(uid))
    rag_index.run_account(SessionLocal, user_id, service=rs.concept_service(user_id))
    rag_retrieval.clear_cache()
    results["hybrid (concept stand-in)"] = evaluate()

    if args.model:
        if not (args.litellm_url and args.dimensions):
            parser.error("--model needs --litellm-url and --dimensions")
        key = os.environ.get("LITELLM_API_KEY")
        model = EmbeddingModel("litellm", args.model, None, args.dimensions)
        real = EmbeddingService(model, LiteLLMBackend(args.litellm_url, key, None), None)
        EmbeddingService.for_version = classmethod(lambda cls, db, uid, version: real)
        summary = rag_index.run_account(SessionLocal, user_id, service=real)
        if summary.get("status") != "SERVING":
            print(f"Indexing with {args.model} failed: {summary}", file=sys.stderr)
            return 1
        rag_retrieval.clear_cache()
        results[f"hybrid ({args.model})"] = evaluate()

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0
    columns = ("recall_at_5", "recall_at_10", "precision_at_3", "precision_at_5", "r_precision",
               "citation_correctness", "leaks", "latency_p50_ms", "latency_p95_ms", "token_reduction")
    print(f"Cases: {len(data.cases)} jobs, {len(data.facts)} facts, 2 resumes\n")
    print("| variant | " + " | ".join(columns) + " |")
    print("|---" * (len(columns) + 1) + "|")
    for name, report in results.items():
        if name == "deterministic builder":
            continue
        print(f"| {name} | " + " | ".join(str(report.get(c)) for c in columns) + " |")
    det = results["deterministic builder"]
    print(f"\nDeterministic builder (account-wide): {det['leaks']} cross-resume facts sent across the jobs, "
          f"{det['mean_items_sent']} facts per job on average, recall over everything sent {det['recall_at_all']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
