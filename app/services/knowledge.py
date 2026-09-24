"""The Knowledge bank as an application form sees it: find a sourced answer, or save the question for a person.

Everything here is deterministic. A search returns saved answers, verified evidence and profile facts with the
ID of the row each came from, and says "unknown" when nothing supports an answer; it never writes one.
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Job, KnowledgeAnswer

ANSWER_TYPES = ("text", "boolean", "choice", "number")
# A saved question counts as the same question at this word overlap (Jaccard over content words).
ANSWER_MATCH = 0.5
# Evidence is offered when it covers at least this share of the question's content words.
EVIDENCE_MATCH = 1 / 3
MAX_OPTIONS = 50
# Words that carry no meaning in an application question, so they never make two questions look alike.
STOPWORDS = frozenset("""
a an and are as at be been being by can could describe detail details did do does done else enter explain for
from give had has have how i if in into is it its list may me might must my of on or our please provide select
should so tell than that the their them then there these they this those to us was we were what when where which
who whom why will with would you your yours
""".split())

# Profile fields an application form asks for by name. Contact details answer the question as they are; the
# others are facts a person or the model still has to turn into the form's answer.
PROFILE_ANSWERS = {
    "email": ({"email", "e-mail"}, True),
    "phone": ({"phone", "mobile", "telephone", "cell"}, True),
    "linkedin_url": ({"linkedin"}, True),
    "github_url": ({"github"}, True),
    "name": ({"name"}, False),
    "location": ({"location", "located", "city", "based", "reside", "residence"}, False),
    "work_authorization": ({"authorized", "authorised", "authorization", "authorisation", "visa", "sponsorship",
                            "sponsor", "citizen", "citizenship", "permit", "eligible", "eligibility", "right"}, False),
}


def question_key(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def terms(text: str) -> set[str]:
    """Content words, lower-cased and lightly singularised: "Years of Kafka experience?" -> {years, kafka, ...}."""
    return {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9+#-]*", (text or "").lower()) if w not in STOPWORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _answer_dict(row: KnowledgeAnswer, match: float) -> dict[str, Any]:
    return {"id": row.id, "question": row.question, "answer": row.answer, "category": row.category,
            "answer_type": row.answer_type, "match": round(match, 2), "source": f"knowledge_answer:{row.id}"}


def search(db: Session, question: str, limit: int = 5) -> dict[str, Any]:
    """Sourced candidates for one form question, and a status saying how far they go.

    status: ANSWERED (a verified saved answer, or a contact detail from the profile), NEEDS_CONFIRMATION (a
    sensitive saved answer: a person confirms it each time, so its text is not returned), PENDING_HUMAN (the
    question is already waiting for a person), SUPPORTED (only facts to compose an answer from), UNKNOWN.
    """
    wanted, key = terms(question), question_key(question)
    answered, sensitive, pending = [], [], []
    for row in db.scalars(select(KnowledgeAnswer).order_by(KnowledgeAnswer.id)).all():
        match = 1.0 if row.question_key == key else _overlap(wanted, terms(row.question))
        if match < ANSWER_MATCH:
            continue
        has_answer = bool((row.answer or "").strip())
        if row.verified and has_answer and row.sensitive:
            sensitive.append({"id": row.id, "question": row.question, "category": row.category,
                              "match": round(match, 2), "source": f"knowledge_answer:{row.id}"})
        elif row.verified and has_answer:
            answered.append(_answer_dict(row, match))
        else:
            pending.append({"id": row.id, "question": row.question, "answer_options": row.answer_options or [],
                            "match": round(match, 2)})
    answered.sort(key=lambda a: (-a["match"], a["id"]))

    facts = []
    profile = db.scalar(select(CandidateProfile))
    for field, (keywords, direct) in PROFILE_ANSWERS.items():
        value = str(getattr(profile, field, "") or "").strip() if profile else ""
        if value and wanted & keywords:
            facts.append({"field": field, "value": value, "answers_directly": direct,
                          "source": f"candidate_profile.{field}"})

    evidence = []
    if wanted:
        for item in db.scalars(select(Evidence).where(Evidence.verified.is_(True)).order_by(Evidence.id)).all():
            text = " ".join([item.claim, item.context or "", item.company or "", item.role or "",
                             *(item.skills or []), *(item.tags or [])])
            share = len(wanted & terms(text)) / len(wanted)
            if share >= EVIDENCE_MATCH:
                evidence.append((share, {"id": item.id, "claim": item.claim, "company": item.company,
                                         "role": item.role, "skills": item.skills or [], "match": round(share, 2),
                                         "source": f"evidence:{item.id}"}))
    evidence = [e for _, e in sorted(evidence, key=lambda pair: (-pair[0], pair[1]["id"]))][:limit]

    direct = next((f for f in facts if f["answers_directly"]), None)
    if answered:
        status, answer, source = "ANSWERED", answered[0]["answer"], answered[0]["source"]
    elif sensitive:
        status, answer, source = "NEEDS_CONFIRMATION", None, sensitive[0]["source"]
    elif direct:
        status, answer, source = "ANSWERED", direct["value"], direct["source"]
    elif pending:
        status, answer, source = "PENDING_HUMAN", None, None
    elif facts or evidence:
        status, answer, source = "SUPPORTED", None, None
    else:
        status, answer, source = "UNKNOWN", None, None
    return {"status": status, "answer": answer, "answer_source": source, "knowledge_answers": answered[:limit],
            "sensitive_answers": sensitive[:limit], "pending_questions": pending[:limit], "profile_facts": facts,
            "evidence": evidence}


def _clean_options(options: Any) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise ValueError("answer_options must be a list of strings")
    cleaned = list(dict.fromkeys(o.strip()[:300] for o in options if o.strip()))
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"answer_options may list at most {MAX_OPTIONS} choices")
    return cleaned


def request_answer(db: Session, job: Job, question: str, *, answer_options: Any = None, answer_type: str | None = None,
                   category: str = "general", sensitive: bool = False, required: bool | None = None,
                   field_label: str | None = None, note: str | None = None, apply_url: str | None = None,
                   requested_by: str = "claude_desktop") -> dict[str, Any]:
    """Save a form question for a person to answer in the Knowledge bank, once per distinct question.

    A question that already has a verified answer is not reopened: the answer (or, when it is sensitive, the
    fact that a person must confirm it) comes back instead.
    """
    question = (question or "").strip()
    if len(question) < 2:
        raise ValueError("question must not be empty")
    if len(question) > 2000:
        raise ValueError("question is longer than 2000 characters")
    options = _clean_options(answer_options)
    answer_type = answer_type or ("choice" if options else "text")
    if answer_type not in ANSWER_TYPES:
        raise ValueError(f"answer_type must be one of {', '.join(ANSWER_TYPES)}")
    asked = {"job_id": job.id, "company": job.company, "title": job.title,
             "apply_url": (apply_url or job.direct_apply_url or job.url or "")[:1200],
             "field_label": (field_label or "").strip()[:300] or None, "required": required,
             "note": (note or "").strip()[:1000] or None, "requested_by": requested_by, "requested_at": _now().isoformat()}

    key = question_key(question)
    row = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == key))
    if row is not None and row.verified and (row.answer or "").strip():
        if row.sensitive:
            return {"status": "NEEDS_CONFIRMATION", "question_id": row.id,
                    "message": "A sensitive answer is saved for this question; the candidate confirms it each time"}
        return {"status": "ALREADY_ANSWERED", "question_id": row.id, "answer": row.answer,
                "answer_source": f"knowledge_answer:{row.id}"}
    status = "ALREADY_REQUESTED" if row is not None else "SAVED"
    if row is None:
        row = KnowledgeAnswer(question=question, question_key=key, answer="", category=(category or "general")[:80],
                              answer_type=answer_type, verified=False, sensitive=bool(sensitive), source_job_id=job.id)
        db.add(row)
    previous = row.request_context or {}
    job_ids = list(dict.fromkeys([*(previous.get("job_ids") or []), job.id]))
    row.request_context = {**asked, "job_ids": job_ids}
    if options:
        row.answer_options = options
        row.answer_type = answer_type
    row.sensitive = row.sensitive or bool(sensitive)
    try:
        db.commit()
    except IntegrityError:  # the same question saved concurrently: that row is the request
        db.rollback()
        row = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == key))
        status = "ALREADY_REQUESTED"
    return {"status": status, "question_id": row.id, "answer_options": row.answer_options or [],
            "answer_type": row.answer_type, "job_ids": (row.request_context or {}).get("job_ids") or [job.id]}


def answered(db: Session, question_ids: list[int]) -> bool:
    """Whether a person has answered (and verified) every one of these saved questions."""
    if not question_ids:
        return True
    rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(question_ids))).all()
    return len(rows) == len(set(question_ids)) and all(r.verified and (r.answer or "").strip() for r in rows)
