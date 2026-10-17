"""Retrieval evaluation: labelled recall and precision, citation correctness, scope leaks, latency and token savings,
and a comparison of the deterministic context builder with retrieval on the same jobs.

Ground truth is a person's labelling (which facts a job's evidence pack should hold, and which must never appear), not
a model's opinion. Used by tests/test_rag_eval.py on a fixed fixture and by scripts/rag_eval.py on a real account.
"""
from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Evidence, Job
from app.services.llm_telemetry import estimate_tokens
from app.services.rag_retrieval import ContextPack, verify_citations


def recall_at_k(ranked: list[int], relevant: set[int], k: int) -> float | None:
    """Share of the relevant facts found in the first k. None when nothing is relevant (not 0: undefined)."""
    if not relevant:
        return None
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(ranked: list[int], relevant: set[int], k: int) -> float | None:
    """Share of the first k (or of all returned, when fewer) that are relevant. None when nothing was returned."""
    top = ranked[:k]
    if not top:
        return None
    return len(set(top) & relevant) / len(top)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


@dataclass
class Case:
    key: str
    job_id: int
    relevant: set[int]
    forbidden: set[int] = field(default_factory=set)


@dataclass
class Report:
    cases: int
    recall_at_5: float | None
    recall_at_10: float | None
    precision_at_3: float | None
    precision_at_5: float | None
    r_precision: float | None
    citation_correctness: float | None
    leaks: int  # forbidden (another resume's) facts that appeared in any pack
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    tokens_used_mean: float | None
    token_reduction: float | None  # 1 - pack tokens / tokens of the whole verified evidence bank
    fallback_reasons: dict[str, int]
    per_case: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _mean(values: Iterable[float | None]) -> float | None:
    found = [v for v in values if v is not None]
    return round(statistics.fmean(found), 4) if found else None


def evaluate(db: Session, user_id: int, cases: list[Case], retrieve: Callable[[Job], ContextPack]) -> Report:
    bank = [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim}
            for e in db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.verified.is_(True)))]
    bank_tokens = estimate_tokens(json.dumps(bank)) or 1
    rows, latencies, tokens, reasons, leaks = [], [], [], {}, 0
    citation = []
    for case in cases:
        pack = retrieve(db.get(Job, case.job_id))
        ranked = pack.evidence_ids
        leaked = sorted(set(ranked) & case.forbidden)
        leaks += len(leaked)
        valid, invalid = verify_citations(pack, ranked)
        citation.append(len(valid) / len(ranked) if ranked else None)
        latencies.append(float(pack.latency_ms))
        tokens.append(float(pack.used_tokens))
        if pack.fallback_reason:
            reasons[pack.fallback_reason] = reasons.get(pack.fallback_reason, 0) + 1
        rows.append({"case": case.key, "returned": len(ranked), "mode": pack.mode,
                     "recall@5": recall_at_k(ranked, case.relevant, 5), "recall@10": recall_at_k(ranked, case.relevant, 10),
                     "precision@3": precision_at_k(ranked, case.relevant, 3),
                     "precision@5": precision_at_k(ranked, case.relevant, 5),
                     "r_precision": precision_at_k(ranked, case.relevant, len(case.relevant) or 1),
                     "leaked": leaked, "invalid_citations": invalid, "used_tokens": pack.used_tokens})
    mean_tokens = _mean(tokens)
    return Report(cases=len(cases), recall_at_5=_mean(r["recall@5"] for r in rows),
                  recall_at_10=_mean(r["recall@10"] for r in rows),
                  precision_at_3=_mean(r["precision@3"] for r in rows),
                  precision_at_5=_mean(r["precision@5"] for r in rows),
                  r_precision=_mean(r["r_precision"] for r in rows), citation_correctness=_mean(citation),
                  leaks=leaks, latency_p50_ms=percentile(latencies, 0.5), latency_p95_ms=percentile(latencies, 0.95),
                  tokens_used_mean=mean_tokens,
                  token_reduction=round(1 - mean_tokens / bank_tokens, 4) if mean_tokens is not None else None,
                  fallback_reasons=reasons, per_case=rows)


def compare_builders(db: Session, profile: Any, jobs: list[Job], limit: int = 40) -> list[dict[str, Any]]:
    """Per job: the evidence ids the deterministic builder selects against those retrieval selects, and which of the
    deterministic ones fall outside the job's resume scope (the account-wide selection retrieval removes)."""
    from app.services import context_builder as cb
    from app.services import rag_retrieval

    out = []
    for job in jobs:
        terms = cb.job_terms(job)
        old, _every = cb.select_evidence(db, profile.user_id, terms, limit)
        pack = rag_retrieval.retrieve_evidence(db, profile, job, terms=terms, limit=limit, allow_model_call=False)
        scope = rag_retrieval.scope(db, profile, job)[1]
        old_ids, new_ids = {e["id"] for e in old}, set(pack.evidence_ids)
        out.append({"job_id": job.id, "deterministic": sorted(old_ids), "retrieval": sorted(new_ids),
                    "only_deterministic": sorted(old_ids - new_ids), "only_retrieval": sorted(new_ids - old_ids),
                    "deterministic_out_of_scope": sorted(old_ids - scope)})
    return out
