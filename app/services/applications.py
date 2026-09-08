"""ATS form inspection and conservative knowledge-bank reuse."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ATSAnswer,
    ATSQuestion,
    ATSQuestionOccurrence,
    Application,
    CandidateProfile,
    now,
)

SENSITIVE_CATEGORIES = {
    "self_identification", "disability", "demographic", "work_authorization",
    "compensation", "attestation",
}
NEGATIONS = {"not", "no", "never", "without", "unable", "cannot"}


def normalize_question(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\([^)]*required[^)]*\)", " ", text)
    text = re.sub(r"\brequired\b\s*[:*]?", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def question_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode()).hexdigest()


def semantic_confidence(left: str, right: str) -> float:
    """A deliberately conservative lexical similarity score (not an equivalence claim)."""
    a, b = normalize_question(left), normalize_question(right)
    if a == b:
        return 1.0
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb or (sa & NEGATIONS) != (sb & NEGATIONS):
        return 0.0
    jaccard = len(sa & sb) / len(sa | sb)
    sequence = SequenceMatcher(None, a, b).ratio()
    # Both token content and word order must remain very close.
    return round(min(jaccard, sequence), 4)


def inferred_sensitivity(text: str, category: str, sensitivity: str) -> str:
    if sensitivity != "none":
        return sensitivity
    normalized = normalize_question(text)
    keyword_categories = {
        "disability": ("disability", "disabled"),
        "demographic": ("race", "ethnicity", "gender", "veteran"),
        "work_authorization": ("authorized to work", "sponsorship", "visa"),
        "compensation": ("salary", "compensation", "pay expectation"),
        "attestation": ("certify", "attest", "signature"),
        "self_identification": ("self identify", "self identification"),
    }
    if category in SENSITIVE_CATEGORIES:
        return category
    for name, words in keyword_categories.items():
        if any(word in normalized for word in words):
            return name
    return "none"


def answer_is_compatible(answer: Any, answer_type: str, choices: list[Any]) -> bool:
    if answer_type in {"text", "textarea", "date"}:
        valid = isinstance(answer, str)
    elif answer_type == "boolean":
        valid = isinstance(answer, bool)
    elif answer_type == "number":
        valid = isinstance(answer, (int, float)) and not isinstance(answer, bool)
    elif answer_type == "single_choice":
        valid = answer in choices
    elif answer_type == "multi_choice":
        valid = isinstance(answer, list) and all(value in choices for value in answer)
    else:
        valid = False
    return valid


def _latest_answer(question: ATSQuestion) -> ATSAnswer | None:
    return max(question.answers, key=lambda answer: answer.version, default=None)


def _sensitive_policy_allows(db: Session, sensitivity: str) -> bool:
    if sensitivity not in SENSITIVE_CATEGORIES:
        return True
    profile = db.get(CandidateProfile, 1)
    policies = (profile.search_preferences if profile else {}).get("ats_auto_answer_policies", {})
    policy = policies.get(sensitivity)
    # A boolean catch-all is intentionally insufficient: opt-in must name the category and scope.
    return isinstance(policy, dict) and policy.get("enabled") is True and bool(policy.get("scope"))


def create_answer_version(
    db: Session, question: ATSQuestion, answer: Any, verification_state: str,
    source_application_id: int | None = None, expires_at: datetime | None = None,
) -> ATSAnswer:
    if not answer_is_compatible(answer, question.answer_type, question.allowed_choices or []):
        raise ValueError(f"Answer is not compatible with {question.answer_type}")
    latest = _latest_answer(question)
    version = (latest.version if latest else 0) + 1
    result = ATSAnswer(
        question=question, version=version, answer=answer,
        verification_state=verification_state, source_application_id=source_application_id,
        expires_at=expires_at,
    )
    db.add(result)
    return result


def inspect_application_questions(
    db: Session, application: Application, observed: list[dict[str, Any]], threshold: float = 0.92,
) -> list[ATSQuestionOccurrence]:
    results: list[ATSQuestionOccurrence] = []
    for item in observed:
        existing = db.scalar(select(ATSQuestionOccurrence).where(
            ATSQuestionOccurrence.application_id == application.id,
            ATSQuestionOccurrence.external_key == item["external_key"],
        ))
        if existing:
            results.append(existing)
            continue

        normalized = normalize_question(item["text"])
        fingerprint = question_fingerprint(item["text"])
        sensitivity = inferred_sensitivity(item["text"], item["category"], item["sensitivity"])
        question = db.scalar(select(ATSQuestion).where(ATSQuestion.fingerprint == fingerprint))
        confidence, match_kind = (1.0, "exact") if question else (0.0, "new")

        if not question:
            candidates = db.scalars(select(ATSQuestion).where(
                ATSQuestion.answer_type == item["answer_type"], ATSQuestion.archived.is_(False)
            )).all()
            scored = sorted(
                ((semantic_confidence(normalized, candidate.normalized_text), candidate) for candidate in candidates),
                key=lambda value: value[0], reverse=True,
            )
            if scored and scored[0][0] >= threshold:
                confidence, question, match_kind = scored[0][0], scored[0][1], "semantic"
            else:
                question = ATSQuestion(
                    normalized_text=normalized, fingerprint=fingerprint, category=item["category"],
                    answer_type=item["answer_type"], allowed_choices=item["allowed_choices"],
                    sensitivity=sensitivity,
                )
                db.add(question)
                db.flush()

        warning = ""
        type_compatible = question.answer_type == item["answer_type"]
        choices_compatible = (question.allowed_choices or []) == (item["allowed_choices"] or [])
        if not type_compatible:
            warning = "Answer type differs from the canonical question."
        elif question.answer_type in {"single_choice", "multi_choice"} and not choices_compatible:
            warning = "Allowed choices differ from the canonical question."

        latest = _latest_answer(question)
        reusable = bool(
            latest and latest.verification_state == "verified" and
            (latest.expires_at is None or latest.expires_at > now()) and type_compatible and
            choices_compatible and confidence >= threshold and not question.archived and
            _sensitive_policy_allows(db, sensitivity)
        )
        if latest and latest.verification_state == "verified" and not reusable and not warning:
            warning = "Existing answer requires human review (confidence, expiry, or policy)."
        occurrence = ATSQuestionOccurrence(
            application=application, question=question, external_key=item["external_key"],
            raw_question_text=item["text"], normalized_text=normalized, fingerprint=fingerprint,
            answer_type=item["answer_type"], allowed_choices=item["allowed_choices"],
            answer_version=latest if reusable else None, match_kind=match_kind,
            match_confidence=confidence, resolution_state="reused" if reusable else "needs_approval",
            conflict_warning=warning,
            provenance={"canonical_question_id": question.id, "answer_version_id": latest.id if reusable else None},
            resolved_at=now() if reusable else None,
        )
        db.add(occurrence)
        results.append(occurrence)
    db.commit()
    return results


def resolve_occurrence(
    db: Session, occurrence: ATSQuestionOccurrence, answer: Any,
    verification_state: str = "verified", expires_at: datetime | None = None,
) -> ATSAnswer:
    latest = _latest_answer(occurrence.question)
    conflict = bool(latest and latest.verification_state == "verified" and latest.answer != answer)
    version = create_answer_version(
        db, occurrence.question, answer, verification_state,
        occurrence.application_id, expires_at,
    )
    db.flush()
    occurrence.answer_version = version
    occurrence.resolution_state = "human_confirmed"
    occurrence.resolved_at = now()
    occurrence.conflict_warning = "Human answer conflicts with a previous verified version." if conflict else ""
    occurrence.provenance = {
        "canonical_question_id": occurrence.question_id,
        "answer_version_id": version.id,
        "source_application_id": occurrence.application_id,
        "resolved_by": "human",
    }
    db.commit()
    return version
