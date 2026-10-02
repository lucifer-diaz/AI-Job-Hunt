"""Whether an email moves an application on the tracker: deterministic Python, never the model.

The model (or a rule) recommends; this module checks that the recommendation matches the kind of email, that the move
is a valid forward transition, and that the reading is confident and explicit. Only then is the stage applied
(APPLIED). A valid but uncertain move is stored as a suggestion for a person (SUGGESTED); an invalid one is recorded
and ignored (INVALID). A person's own newer stage change always wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.prompts.contracts import ApplicationEmailAnalysis

APPLIED, SUGGESTED, NONE, INVALID = "APPLIED", "SUGGESTED", "NONE", "INVALID"
ACTION_STAGE = {"MOVE_TO_ACKNOWLEDGED": "acknowledged", "MOVE_TO_SCREENING": "screening",
                "MOVE_TO_INTERVIEWING": "interviewing", "MOVE_TO_TAKEHOME": "takehome", "MOVE_TO_OFFER": "offer",
                "MOVE_TO_ON_HOLD": "onhold", "MOVE_TO_REJECTED": "rejected"}
# The tracker stage each kind of email can justify. Anything not listed never moves an application.
EVENT_STAGES = {
    "APPLICATION_CONFIRMATION": {"acknowledged"},
    "RECRUITER_RESPONSE": {"screening"},
    "FOLLOWUP_RESPONSE": {"screening"},
    "INTERVIEW_INVITATION": {"screening", "interviewing"},
    "INTERVIEW_RESCHEDULE": {"interviewing"},
    "ASSESSMENT_INVITATION": {"takehome"},
    "ASSESSMENT_REMINDER": {"takehome"},
    "APPLICATION_ON_HOLD": {"onhold"},
    "APPLICATION_REJECTION": {"rejected"},
    "OFFER": {"offer"},
    "OFFER_UPDATE": {"offer"},
}
# Progress order of the live stages. Take-home and interviews are the same step: either can follow the other.
RANK = {"applied": 0, "referral": 0, "acknowledged": 1, "screening": 2, "takehome": 3, "interviewing": 3, "offer": 5,
        "negotiating": 6}
TERMINAL = {"rejected", "closed"}


@dataclass(frozen=True)
class StageDecision:
    decision: str
    stage_from: str
    stage_to: str | None
    reason: str


def valid_transition(current: str, target: str) -> tuple[bool, str]:
    if current == target:
        return False, "already at this stage"
    if current in TERMINAL:
        return False, f"the application is {current}; an email never reopens it"
    if target == "rejected":
        return True, ""
    if target == "onhold":
        return (current not in ("offer", "negotiating")), "an offer is not put on hold by an email"
    if current == "onhold":
        return (target in ("screening", "takehome", "interviewing", "offer")), "an email only resumes a paused process"
    if target not in RANK or current not in RANK:
        return False, f"no transition from {current} to {target}"
    if RANK[target] > RANK[current] or (RANK[target] == RANK[current] == 3):
        return True, ""
    return False, f"{target} would move the application backwards from {current}"


def decide(analysis: ApplicationEmailAnalysis, *, current: str, match_status: str, explicit: bool,
           user_changed_after: datetime | None, received_at: datetime | None, threshold: float) -> StageDecision:
    """What to do with the stage change an analysed email recommends. See the module docstring."""
    current = current or "applied"
    target = ACTION_STAGE.get(analysis.recommended_tracker_action)
    if target is None:
        return StageDecision(NONE, current, None, "no stage change recommended")
    allowed = EVENT_STAGES.get(analysis.event_type, set())
    if target not in allowed:
        return StageDecision(INVALID, current, target,
                             f"a {analysis.event_type.lower().replace('_', ' ')} does not justify {target}")
    ok, why = valid_transition(current, target)
    if not ok:
        return StageDecision(NONE if current == target else INVALID, current, target, why)
    if user_changed_after and received_at and user_changed_after >= received_at:
        return StageDecision(SUGGESTED, current, target, "you changed the stage after this email arrived")
    problems = []
    if match_status != "MATCHED":
        problems.append("the email is not confidently linked to this application")
    if analysis.needs_review:
        problems.append("the analysis asked for review")
    if analysis.confidence < threshold:
        problems.append(f"confidence {analysis.confidence:.2f} is below {threshold:.2f}")
    if not explicit:
        problems.append("the wording is not explicit enough to change the stage automatically")
    if problems:
        return StageDecision(SUGGESTED, current, target, "; ".join(problems))
    return StageDecision(APPLIED, current, target, "explicit, confident and a valid transition")
