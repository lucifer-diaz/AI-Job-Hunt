"""Hybrid retrieval of a job's candidate evidence, and of saved answers for a question.

    typed query (a job)
    → scope FIRST, before any scoring:  the account (explicit user_id everywhere, raw SQL included)
                                         + the job's own resume (resume_verification.for_job: USER/SETUP/ACTIVE/AUTO pin)
                                         + evidence_tags.allowed_evidence_ids (that resume's evidence, untagged evidence)
                                         + verified only, provenance conflicts held back
    → canonical SQL:   the parsed posting's required skills that an evidence row names exactly
    → keyword:         the context builder's own term scoring (JobTerms), and PostgreSQL full-text (ts_rank_cd)
    → vector:          cosine over the SERVING index version, query embedded with that version's own model, a vector
                        used only while its content hash equals the live row's (RAG_VECTOR_ENABLED)
    → reciprocal-rank fusion (bounded candidate lists), deterministic rerank (canonical matches)
    → ContextPack: real Evidence ids, token budget, what was used and why (fallback reasons), latency

A semantic match never widens the scope: evidence of another resume is not a candidate, however similar it is. A
person's pin always beats similarity because it decides the scope. Retrieval reads; it never writes business data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Evidence, Job, KnowledgeAnswer, RagIndexEntry
from app.services import evidence_tags, rag_index, vector_store
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.llm_telemetry import estimate_tokens
from app.settings import get_settings

log = logging.getLogger("jobhunt.rag_retrieval")
RETRIEVAL_VERSION = "rag-v1"
RRF_K = 60
WORD = re.compile(r"[a-z0-9][a-z0-9+#]*")


@dataclass
class PackItem:
    evidence_id: int
    claim: str
    company: str
    role: str
    scope: str  # RESUME (this resume's own fact) | TAGGED (linked to it) | ALL (untagged: every resume)
    scores: dict[str, float] = field(default_factory=dict)
    tokens: int = 0

    def projection(self) -> dict[str, Any]:
        """What a prompt receives — the same shape the deterministic builder sends ({id, company, role, claim})."""
        return {k: v for k, v in (("id", self.evidence_id), ("company", self.company), ("role", self.role),
                                  ("claim", self.claim)) if v}


@dataclass
class ContextPack:
    job_id: int | None
    resume_id: int | None
    items: list[PackItem]
    budget_tokens: int
    used_tokens: int
    mode: str  # hybrid | lexical
    allowed: int  # evidence rows in scope
    candidates: int  # rows that scored at all
    fallback_reason: str | None = None
    index_version: int | None = None
    latency_ms: int = 0
    truncated: bool = False
    version: str = RETRIEVAL_VERSION

    @property
    def evidence_ids(self) -> list[int]:
        return [item.evidence_id for item in self.items]

    def projected(self) -> list[dict[str, Any]]:
        return sorted((item.projection() for item in self.items), key=lambda e: e["id"])

    def metadata(self) -> dict[str, Any]:
        """Ids, scores and counts only — safe for logs and comparisons (no fact text)."""
        return {"job_id": self.job_id, "resume_id": self.resume_id, "evidence_ids": self.evidence_ids,
                "budget_tokens": self.budget_tokens, "used_tokens": self.used_tokens, "mode": self.mode,
                "allowed": self.allowed, "candidates": self.candidates, "fallback_reason": self.fallback_reason,
                "index_version": self.index_version, "latency_ms": self.latency_ms, "truncated": self.truncated,
                "version": self.version}

    def to_dict(self) -> dict[str, Any]:
        return {**self.metadata(), "items": [asdict(item) for item in self.items]}


def verify_citations(pack: ContextPack, cited: list[int] | set[int]) -> tuple[list[int], list[int]]:
    """(valid, invalid) cited evidence ids: valid only when the pack actually offered that evidence."""
    offered = set(pack.evidence_ids)
    valid = sorted({int(c) for c in cited if int(c) in offered})
    return valid, sorted({int(c) for c in cited} - offered)


# ───────────── query text ─────────────

def job_query(job: Job) -> tuple[str, set[str]]:
    """(query text for embedding / full-text, the posting's required skills, lower-cased)."""
    from app.services import job_context as jc

    data = jc._structured(job)
    required = {str(s).strip().lower() for s in (data.get("required_skills") or []) if str(s).strip()}
    parts = [job.title or ""]
    for key in ("required_skills", "preferred_skills", "responsibilities", "keywords", "technical_domains"):
        values = [str(v) for v in data.get(key) or [] if v]
        if values:
            parts.append(", ".join(values))
    if len(parts) == 1:  # nothing parsed: the posting itself, bounded
        parts.append((job.description or "")[:2000])
        required |= {str(s).strip().lower() for s in (job.skills or []) if str(s).strip()}
    return "\n".join(p for p in parts if p).strip()[:4000], required


# ───────────── query embedding cache (per account and index version) ─────────────

_cache_lock = threading.Lock()
_query_cache: OrderedDict[tuple[int, int, str], list[float]] = OrderedDict()


def _cached_query_vector(user_id: int, version_id: int, query: str, service: EmbeddingService) -> list[float]:
    key = (user_id, version_id, hashlib.sha256(query.encode()).hexdigest())
    with _cache_lock:
        hit = _query_cache.get(key)
        if hit is not None:
            _query_cache.move_to_end(key)
            return hit
    vector = service.embed_query(query)
    with _cache_lock:
        _query_cache[key] = vector
        while len(_query_cache) > max(1, int(get_settings().rag_query_cache_size or 512)):
            _query_cache.popitem(last=False)
    return vector


def clear_cache() -> None:
    with _cache_lock:
        _query_cache.clear()


# ───────────── signals ─────────────

def _pg_fulltext(db: Session, user_id: int, ids: list[int], query: str, limit: int) -> list[tuple[int, float]]:
    words = sorted({w for w in WORD.findall(query.lower()) if len(w) > 2})[:64]
    if not words or not ids:
        return []
    tsquery = " | ".join(words)
    marks = ", ".join(f":id{i}" for i in range(len(ids)))
    rows = db.execute(text(f"""
        SELECT id, ts_rank_cd(to_tsvector('english', coalesce(claim, '') || ' ' || coalesce(role, '') || ' ' ||
                                           coalesce(company, '')),
                              to_tsquery('english', :q)) AS rank
        FROM evidence WHERE user_id = :u AND verified IS TRUE AND id IN ({marks})
        ORDER BY rank DESC, id LIMIT :limit"""),
        {"q": tsquery, "u": user_id, "limit": limit, **{f"id{i}": v for i, v in enumerate(ids)}}).all()
    return [(int(r[0]), float(r[1])) for r in rows if r[1] and float(r[1]) > 0]


def _vector_hits(db: Session, user_id: int, rows: dict[int, Evidence], query: str, limit: int,
                 allow_model_call: bool) -> tuple[list[tuple[int, float]], int | None, str | None]:
    settings = get_settings()
    if not settings.rag_vector_enabled:
        return [], None, "VECTOR_DISABLED"
    if not allow_model_call:
        return [], None, "MODEL_CALL_NOT_ALLOWED"
    version = rag_index.serving_version(db, user_id)
    if version is None:
        return [], None, "NO_SERVING_INDEX"
    if not vector_store.available(db):
        return [], version.id, "VECTOR_STORE_UNAVAILABLE"
    entries = db.execute(select(RagIndexEntry.id, RagIndexEntry.source_id).where(
        RagIndexEntry.user_id == user_id, RagIndexEntry.index_version_id == version.id,
        RagIndexEntry.source_type == rag_index.EVIDENCE, RagIndexEntry.status == "ACTIVE",
        RagIndexEntry.source_id.in_(list(rows)))).all()
    if not entries:
        return [], version.id, "NO_INDEXED_EVIDENCE"
    by_entry = {int(e[0]): int(e[1]) for e in entries}
    live = {entry_id: rag_index.content_hash(rag_index.evidence_text(rows[source_id]))
            for entry_id, source_id in by_entry.items()}
    try:
        service = EmbeddingService.for_version(db, user_id, version)
        vector = _cached_query_vector(user_id, version.id, query, service)
    except EmbeddingError as exc:
        return [], version.id, f"EMBEDDING_{exc.code}"
    hits = vector_store.search(db, user_id=user_id, index_version_id=version.id, dimensions=version.dimensions,
                               query=vector, entry_hashes=live, limit=limit)
    return [(by_entry[entry_id], score) for entry_id, score in hits], version.id, None


SUFFIXES = ("ing", "ed", "es", "s", "e")


def stem(word: str) -> str:
    """A deliberately small stemmer (profiling / profile → profil) so keyword search is not defeated by word forms."""
    for suffix in SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _stems(text: str) -> list[str]:
    from app.services.knowledge import STOPWORDS

    return [stem(w) for w in WORD.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def evidence_terms(e: Evidence) -> list[str]:
    """What keyword search reads of an evidence row: the claim, where it happened and its skills (not the
    provenance note in `context`, which is the same for every fact of a resume)."""
    return _stems(" ".join([e.claim or "", e.role or "", e.company or "", " ".join(e.skills or []),
                            " ".join(e.tags or [])]))


def query_terms(terms: Any, query: str) -> list[str]:
    words = set(_stems(query)) | {stem(w) for w in (getattr(terms, "words", set()) | getattr(terms, "focus", set()))}
    return sorted(words)


def bm25(query: list[str], docs: dict[int, list[str]], k1: float = 1.2, b: float = 0.75) -> list[tuple[int, float]]:
    """Okapi BM25 over the in-scope rows only (so a word common in this candidate's evidence counts for little)."""
    if not query or not docs:
        return []
    n = len(docs)
    avg = sum(len(d) for d in docs.values()) / n or 1.0
    df = {q: sum(1 for d in docs.values() if q in d) for q in query}
    out = []
    for doc_id, words in docs.items():
        score = 0.0
        for q in query:
            tf = words.count(q)
            if not tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(words) / avg))
        if score > 0:
            out.append((doc_id, round(score, 6)))
    return sorted(out, key=lambda p: (-p[1], p[0]))


def _ranks(scored: list[tuple[int, float]]) -> dict[int, int]:
    ordered = sorted(scored, key=lambda p: (-p[1], p[0]))
    return {evidence_id: rank for rank, (evidence_id, _score) in enumerate(ordered, start=1)}


# ───────────── retrieval ─────────────

def scope(db: Session, profile: Any, job: Job | None) -> tuple[Any, set[int]]:
    """(the resume the job uses, the evidence ids it may draw on) — decided before any scoring."""
    from app.services import resume_verification as rv

    view = rv.for_job(db, profile, job)
    resume = rv.resume_for(db, view, create=False) if view is not None else None
    return resume, evidence_tags.allowed_evidence_ids(db, profile.user_id, resume)


def retrieve_evidence(db: Session, profile: Any, job: Job, *, terms: Any = None,
                      exclude: set[int] | frozenset[int] = frozenset(), budget_tokens: int | None = None,
                      limit: int | None = None, allow_model_call: bool = True) -> ContextPack:
    """The evidence a job's prompt should carry: in scope, relevant, cited by its real id, within a token budget."""
    from app.services.context_builder import job_terms

    started = time.monotonic()
    settings = get_settings()
    user_id = profile.user_id
    budget = int(budget_tokens if budget_tokens is not None else settings.rag_context_token_budget or 1200)
    cap = max(1, int(settings.rag_max_candidates or 50))
    resume, allowed = scope(db, profile, job)
    allowed -= set(exclude)
    resume_keys = evidence_tags.verified_fact_keys(resume)
    tagged = set()
    if resume is not None:
        from app.models import EvidenceResumeLink

        tagged = set(db.scalars(select(EvidenceResumeLink.evidence_id).where(
            EvidenceResumeLink.user_id == user_id, EvidenceResumeLink.resume_id == resume.id,
            EvidenceResumeLink.excluded.is_(False))).all())
    rows = {e.id: e for e in db.scalars(select(Evidence).where(
        Evidence.user_id == user_id, Evidence.verified.is_(True), Evidence.id.in_(sorted(allowed)))).all()} \
        if allowed else {}
    query, required = job_query(job)
    terms = terms or job_terms(job)

    keyword = bm25(query_terms(terms, query), {eid: evidence_terms(e) for eid, e in rows.items()})[:cap]
    fulltext: list[tuple[int, float]] = []
    if rows and db.get_bind().dialect.name == "postgresql":
        try:
            with db.begin_nested():
                fulltext = _pg_fulltext(db, user_id, sorted(rows), query, cap)
        except Exception:  # keyword scoring above still stands
            log.warning("full-text search failed", exc_info=True)
    vector, version_id, reason = _vector_hits(db, user_id, rows, query, cap, allow_model_call) if rows \
        else ([], None, "NOTHING_IN_SCOPE")
    canonical = {eid: sum(1 for s in (e.skills or []) if str(s).strip().lower() in required)
                 for eid, e in rows.items()} if required else {}

    fused: dict[int, float] = {}
    signals: dict[int, dict[str, float]] = {}
    for name, hits in (("keyword", keyword), ("fulltext", fulltext), ("vector", vector)):
        for evidence_id, rank in _ranks(hits).items():
            fused[evidence_id] = fused.get(evidence_id, 0.0) + 1.0 / (RRF_K + rank)
        for evidence_id, score in hits:
            signals.setdefault(evidence_id, {})[name] = round(score, 6)
    for evidence_id, matches in canonical.items():
        if matches:
            signals.setdefault(evidence_id, {})["canonical"] = float(matches)
            fused[evidence_id] = fused.get(evidence_id, 0.0) + matches / (RRF_K + 1) / 2
    ordered = sorted(fused.items(), key=lambda p: (-p[1], p[0]))

    items: list[PackItem] = []
    used = 0
    truncated = False
    max_items = limit if limit is not None else cap
    for evidence_id, score in ordered:
        if len(items) >= max_items:
            truncated = True
            break
        e = rows[evidence_id]
        item = PackItem(evidence_id=e.id, claim=e.claim or "", company=e.company or "", role=e.role or "",
                        scope="TAGGED" if e.id in tagged else ("RESUME" if e.source == "resume" and
                                                               e.fact_key in resume_keys else "ALL"),
                        scores={**signals.get(evidence_id, {}), "fused": round(score, 6)})
        item.tokens = estimate_tokens(json.dumps(item.projection()))
        if used + item.tokens > budget:
            truncated = True
            continue  # a shorter item further down may still fit
        used += item.tokens
        items.append(item)
    return ContextPack(job_id=getattr(job, "id", None), resume_id=getattr(resume, "id", None), items=items,
                       budget_tokens=budget, used_tokens=used, mode="hybrid" if vector else "lexical",
                       allowed=len(rows), candidates=len(fused), fallback_reason=reason, index_version=version_id,
                       latency_ms=int((time.monotonic() - started) * 1000), truncated=truncated)


# ───────────── saved answers: suggestions only ─────────────

def suggest_answers(db: Session, user_id: int, question: str, *, limit: int = 3,
                    exclude: set[int] | frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    """Saved, verified answers to questions *like* this one, for a person to review. Never an answer: a suggestion is
    `authoritative: false`, and for a sensitive question (work authorization, sponsorship …) the answer text is
    withheld. Keyword-only (it runs inside requests, where no model is called)."""
    from app.services.knowledge import terms as question_terms

    wanted = question_terms(question)
    if not wanted:
        return []
    rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.user_id == user_id,
                                                    KnowledgeAnswer.verified.is_(True))).all()
    scored = []
    for row in rows:
        if row.id in exclude or not rag_index.eligible_answer(row):
            continue
        have = question_terms(row.question or "")
        overlap = len(wanted & have) / max(1, len(wanted | have))
        if overlap >= 0.2:
            scored.append((overlap, row))
    scored.sort(key=lambda p: (-p[0], p[1].id))
    return [{"id": row.id, "question": row.question, "category": row.category, "sensitive": bool(row.sensitive),
             "answer": None if row.sensitive else row.answer, "similarity": round(score, 3),
             "authoritative": False} for score, row in scored[:limit]]
