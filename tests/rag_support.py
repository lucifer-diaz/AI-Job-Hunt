"""Shared fixtures for the retrieval tests: the labelled dataset (tests/fixtures/rag_eval/dataset.json) loaded into an
account through the real services, and a controlled concept embedder."""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models import Evidence, Job, Resume
from app.services import evidence_tags, resume_verification
from app.services.embeddings import EmbeddingModel, Embeddings, EmbeddingService, hashed_vector
from app.services.rag_eval import Case
from app.services.user_settings import profile_for

DATASET = json.loads((Path(__file__).parent / "fixtures" / "rag_eval" / "dataset.json").read_text())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class Loaded:
    user_id: int
    resumes: dict[str, int]
    facts: dict[str, int]
    jobs: dict[str, int]
    cases: list[Case] = field(default_factory=list)


def session_for(sessions, user_id: int):
    db = sessions()
    db.info["user_id"] = user_id
    return db


def load(sessions, user_id: int, data: dict[str, Any] = DATASET) -> Loaded:
    """Resumes (verified experience → resume evidence), manual facts (one tagged), jobs with their pins."""
    with session_for(sessions, user_id) as db:
        profile = profile_for(db)
        resumes: dict[str, int] = {}
        for key, spec in data["resumes"].items():
            resume = resume_verification.record_version(db, profile, spec["text"], name=key,
                                                        activate=spec.get("active", False))
            resume_verification.verify_section(db, resume, "experience")
            resumes[key] = resume.id
        db.commit()
        evidence = db.scalars(select(Evidence).where(Evidence.user_id == user_id)).all()
        by_claim = {_norm(e.claim): e.id for e in evidence}
        facts: dict[str, int] = {}
        for label, spec in data["facts"].items():
            if spec.get("manual"):
                row = Evidence(user_id=user_id, claim=spec["claim"], company=spec.get("company", ""),
                               role=spec.get("role", ""), skills=spec.get("skills", []), verified=True,
                               source="manual")
                db.add(row)
                db.flush()
                if spec.get("tagged"):
                    evidence_tags.set_resumes(db, user_id, row.id, [resumes[r] for r in spec["tagged"]])
                facts[label] = row.id
            else:
                facts[label] = by_claim[_norm(spec["claim"])]
        jobs: dict[str, int] = {}
        for spec in data["jobs"]:
            job = Job(user_id=user_id, source="manual", source_ref=f"rag-{uuid.uuid4().hex[:10]}",
                      title=spec["title"], company="Hiring Co", description=spec["description"],
                      status="shortlisted", resume_id=resumes[spec["resume"]] if spec.get("resume") else None,
                      resume_choice=spec["pin"])
            db.add(job)
            db.flush()
            jobs[spec["key"]] = job.id
        db.commit()
    cases = [Case(key=s["key"], job_id=jobs[s["key"]], relevant={facts[f] for f in s["relevant"]},
                  forbidden={facts[f] for f in s["forbidden"]}) for s in data["jobs"]]
    return Loaded(user_id, resumes, facts, jobs, cases)


def profile_and_job(db, job_id: int):
    return profile_for(db), db.get(Job, job_id)


def resume(db, resume_id: int) -> Resume:
    return db.get(Resume, resume_id)


# ───────────── a controlled embedder ─────────────

class ConceptBackend:
    """Words of one concept group (dataset.json "concepts") share a direction; everything else is hashed bag-of-words.
    Synthetic on purpose: it shows that fusion uses meaning when a model supplies it, nothing about a real model."""

    def __init__(self, concepts: dict[str, list[str]] | None = None):
        groups = {k: v for k, v in (concepts or DATASET["concepts"]).items() if not k.startswith("_")}
        self.names = sorted(groups)
        self.word_to_concept = {w: name for name, words in groups.items() for w in words}

    def vector(self, text: str, dimensions: int) -> list[float]:
        words = re.findall(r"[a-z0-9/]+", text.lower())
        concept = [0.0] * len(self.names)
        for word in words:
            name = self.word_to_concept.get(word) or self.word_to_concept.get(word.rstrip("s"))
            if name:
                concept[self.names.index(name)] += 1.0
        rest = hashed_vector(text, dimensions - len(self.names))
        vector = [3.0 * c for c in concept] + rest
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed(self, model: EmbeddingModel, texts: list[str], operation: str) -> Embeddings:
        return Embeddings([self.vector(t, model.dimensions) for t in texts], model.model, model.model,
                          model.dimensions)


CONCEPT_MODEL = EmbeddingModel("mock", "concept-test", None, 64)


def concept_service(user_id: int | None = None, model: EmbeddingModel = CONCEPT_MODEL) -> EmbeddingService:
    return EmbeddingService(model, ConceptBackend(), user_id)


def use_concepts(monkeypatch) -> None:
    """Make retrieval embed queries with the concept embedder (the index is built with concept_service)."""
    monkeypatch.setattr(EmbeddingService, "for_version",
                        classmethod(lambda cls, db, user_id, version: concept_service(
                            user_id, EmbeddingModel(version.provider, version.model, version.underlying_model,
                                                    version.dimensions, version.doc_format_version))))
