"""Reviews of tailored resumes: one schema for every reviewer, and the deterministic decision made from a review.

The reviewer (Claude Desktop, or the backend model) only recommends PASS, RETAILOR or SKIP. Python decides:

    deterministic evidence problems, unsupported or overstated claims    → RETAILOR     job back to the Tailor queue
    PASS + score ≥ minimum reviewer score + confidence ≥ policy minimum
         + the job still passes the candidate's hard filters             → PASS         packet stays ready for approval
    anything else (SKIP, a low score, a policy failure)                  → NEEDS_HUMAN  sent back for your decision

A reviewer can never archive, approve or submit anything by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Job, ResumeVariant
from app.services.ranker import hard_filter_reason, policy_for
from app.services.tailoring_results import from_variant_content, validate_tailored_resume
from app.services.user_settings import settings_for
from app.services.workflow import NEEDS_RETAILOR, get_profile, store_review

RECOMMENDATIONS = ("PASS", "RETAILOR", "SKIP")
# The backend review prompt's recommendation vocabulary, mapped onto the shared one.
BACKEND_RECOMMENDATIONS = {"approve": "PASS", "revise": "RETAILOR", "reject": "SKIP"}
ClaimList = list[str]


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    review_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    unsupported_claims: ClaimList = Field(default_factory=list, max_length=30)
    missing_requirements: ClaimList = Field(default_factory=list, max_length=30)
    overstated_claims: ClaimList = Field(default_factory=list, max_length=30)
    risks: ClaimList = Field(default_factory=list, max_length=30)
    recommendation: Literal["PASS", "RETAILOR", "SKIP"]
    # The version that was reviewed; a review of an older version is rejected.
    tailored_resume_version: int | None = None
    summary: str = Field(default="", max_length=2000)

    @field_validator("recommendation", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("unsupported_claims", "missing_requirements", "overstated_claims", "risks")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        return [v.strip()[:500] for v in values if v.strip()]


@dataclass(frozen=True)
class ReviewDecision:
    decision: str  # PASS | RETAILOR | NEEDS_HUMAN
    reasons: list[str]
    deterministic_issues: list[dict[str, str]]


def normalize_backend_review(result: dict[str, Any]) -> dict[str, Any]:
    """A backend ReviewResult (app.prompts.contracts) as stored notes, with the field names the Tailor screen reads.

    reject_reasons and improvements are derived from the review's own lists; nothing is inferred beyond them. Older
    stored reviews (approve / revise / reject) are mapped onto PASS / RETAILOR / SKIP."""
    score = result.get("review_score", result.get("score", 0))
    recommendation = str(result.get("recommendation") or "").strip()
    shared = BACKEND_RECOMMENDATIONS.get(recommendation.lower(), recommendation.upper())
    if shared not in RECOMMENDATIONS:
        shared = "RETAILOR" if result.get("reject_reasons") or result.get("unsupported_claims") else "PASS"
    reject = result.get("reject_reasons")
    if reject is None:
        reject = [*(result.get("unsupported_claims") or []), *(result.get("overstated_claims") or [])]
    missing = result.get("missing_requirements")
    if missing is None:
        missing = result.get("unaddressed_requirements") or []
    improvements = result.get("improvements")
    if improvements is None:
        improvements = [*missing, *(result.get("risks") or [])]
    return {**result, "score": score, "review_score": score, "recommendation": shared, "reject_reasons": reject,
            "missing_requirements": missing, "improvements": improvements, "source": "backend"}


def latest_variant(db: Session, job_id: int) -> ResumeVariant | None:
    return db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                     .order_by(desc(ResumeVariant.version)).limit(1))


def decide_review(db: Session, job: Job, variant: ResumeVariant, review: ReviewIn) -> ReviewDecision:
    profile = get_profile(db)
    data, legacy = from_variant_content(job.id, variant.content or {})
    issues = [] if (variant.content or {}).get("untailored") else validate_tailored_resume(
        db, profile, job, data, legacy=legacy)
    if issues or review.unsupported_claims or review.overstated_claims:
        reasons = [f"{i['code']}: {i['message']}" for i in issues[:5]]
        if review.unsupported_claims:
            reasons.append(f"{len(review.unsupported_claims)} unsupported claim(s) found by the reviewer")
        if review.overstated_claims:
            reasons.append(f"{len(review.overstated_claims)} overstated claim(s) found by the reviewer")
        return ReviewDecision("RETAILOR", reasons, issues)

    policy = policy_for(db, job.user_id)
    minimum_score = settings_for(db, job.user_id).min_review_score
    reasons = []
    if review.recommendation != "PASS":
        reasons.append(f"The reviewer recommended {review.recommendation}; you decide")
    if review.review_score < minimum_score:
        reasons.append(f"Review score {review.review_score:.0f} is below your minimum of {minimum_score}")
    if review.confidence < policy.minimum_confidence:
        reasons.append(f"Confidence {review.confidence:.2f} is below the minimum of {policy.minimum_confidence:.2f}")
    if (blocked := hard_filter_reason(job, policy)) is not None:
        reasons.append(f"Candidate policy: {blocked}")
    if reasons:
        return ReviewDecision("NEEDS_HUMAN", reasons, [])
    reason = (f"Review score {review.review_score:.0f} meets your minimum of {minimum_score}, "
              "no unsupported claims and the evidence checks passed")
    return ReviewDecision("PASS", [reason], [])


# Where a decision leaves the job (legacy dashboard status, the one the Tailor and Approve screens read).
DECISION_STATUS = {"PASS": "tailored", "RETAILOR": NEEDS_RETAILOR, "NEEDS_HUMAN": "pending_approval"}


def apply_review(db: Session, job: Job, variant: ResumeVariant, review: ReviewIn, *, source: str) -> ReviewDecision:
    """Decide, store the review on the version and move the job. Returns the decision."""
    decision = decide_review(db, job, variant, review)
    notes = {**review.model_dump(), "score": review.review_score, "decision": decision.decision,
             "decision_reasons": decision.reasons, "deterministic_issues": decision.deterministic_issues,
             "reject_reasons": decision.reasons if decision.decision != "PASS" else [],
             "improvements": review.missing_requirements, "source": source}
    job.status = DECISION_STATUS[decision.decision]
    store_review(db, variant, notes)
    return decision
