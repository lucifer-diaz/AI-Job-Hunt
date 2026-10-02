"""Deterministic classification of recruiting email: phrase rules first, the model only when they are not enough.

A rule either *settles* an email (an application receipt, an explicit rejection: nothing more to extract) or gives a
*hint* for the model, which still has to read dates, links and next steps from the email itself. When no model can be
used, the hint becomes the event, marked for review and with nothing extracted: rules never invent a date.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.prompts.contracts import ApplicationEmailAnalysis


@dataclass(frozen=True)
class Rule:
    event_type: str
    confidence: float
    patterns: tuple[re.Pattern[str], ...]
    settles: bool  # nothing left to extract: no model call needed
    action: str = "NO_CHANGE"
    stage: str = "UNKNOWN"
    # The rule does not apply when one of these matches ("if you are shortlisted, we will schedule an interview").
    unless: tuple[re.Pattern[str], ...] = ()


def _p(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# Checked in order: a rejection often also thanks the candidate for applying, and an offer or an interview email may
# repeat that the application was received.
RULES = (
    Rule("APPLICATION_REJECTION", 0.95, _p(
        r"decided (?:not to|to not) (?:move|proceed|progress|go) (?:forward|further|ahead)",
        r"\bnot (?:be )?(?:moving|proceeding|progressing) (?:forward|further) with your (?:application|candidacy)",
        r"(?:decided to|will|have chosen to) (?:pursue|move forward with|proceed with) other candidates",
        r"other candidates whose (?:experience|qualifications|skills|background)",
        r"unfortunately,? (?:we|the team) (?:will not|won't|are unable to|cannot|can't) (?:be )?(?:moving|move|proceed|"
        r"progress|offer)",
        r"(?:position|role|job) has (?:now )?been filled",
        r"(?:you )?(?:have|were) not been (?:selected|shortlisted)",
        r"regret to (?:inform|let you know)"), True, "MOVE_TO_REJECTED", "REJECTED"),
    Rule("OFFER", 0.93, _p(
        r"(?:pleased|delighted|happy|excited) to (?:extend|offer|make) (?:you )?(?:an |a |the )?(?:formal |job |verbal )?"
        r"offer",
        r"\boffer letter\b", r"we(?:'d| would) like to (?:extend|make) (?:you )?an offer", r"offer of employment"),
        False, "MOVE_TO_OFFER", "OFFER"),
    Rule("INTERVIEW_RESCHEDULE", 0.9, _p(
        r"\breschedul", r"(?:interview|call|meeting) (?:has been |is )?(?:moved|postponed|cancell?ed)",
        r"(?:move|push|shift) (?:our|your|the) (?:interview|call)"), False, "MOVE_TO_INTERVIEWING"),
    Rule("ASSESSMENT_REMINDER", 0.9, _p(
        r"remind(?:er|ing you)?\b.{0,60}\b(?:assessment|coding (?:test|challenge)|take[- ]home|test)"),
        False, "MOVE_TO_TAKEHOME", "ASSESSMENT"),
    Rule("ASSESSMENT_INVITATION", 0.9, _p(
        r"hackerrank\.com", r"codility\.com", r"codesignal\.com", r"testgorilla", r"hackerearth\.com", r"coderbyte",
        r"\b(?:online|technical|coding) (?:assessment|test|challenge)\b", r"\btake[- ]home (?:assignment|exercise|test|"
        r"task|challenge)"), False, "MOVE_TO_TAKEHOME", "ASSESSMENT"),
    Rule("INTERVIEW_INVITATION", 0.88, _p(
        r"(?:schedule|book|set up|arrange) (?:an? |your |the )?(?:interview|call|chat|conversation|screen)",
        r"invite you (?:to|for) (?:an? |the )?(?:interview|call|chat|conversation)",
        r"(?:share|send|let (?:me|us) know) your availability", r"calendly\.com", r"goodtime\.io",
        r"\binterview (?:invitation|invite)\b"), False, "MOVE_TO_INTERVIEWING",
        unless=_p(r"\bif (?:your|you|we|there|selected|shortlisted)\b[^.]{0,120}\b(?:interview|call|schedul)",
                  r"(?:will|may|would) (?:contact|reach out to|be in touch with) you[^.]{0,80}(?:interview|schedul)")),
    Rule("APPLICATION_ON_HOLD", 0.9, _p(
        r"(?:position|role|requisition|search|process) (?:is|has been|was) (?:put |placed )?on hold",
        r"(?:paused|pausing) (?:the|our) (?:hiring|search|recruitment)"), False, "MOVE_TO_ON_HOLD", "ON_HOLD"),
    Rule("BACKGROUND_CHECK", 0.9, _p(r"background (?:check|verification|screening)"), False),
    Rule("DOCUMENT_REQUEST", 0.85, _p(
        r"(?:send|share|upload|provide) (?:us )?(?:your |a copy of your )?(?:id proof|identity document|passport|"
        r"payslips?|pay stubs?|relieving letter|degree certificate|transcripts?|references)"), False),
    Rule("APPLICATION_CONFIRMATION", 0.95, _p(
        r"thank(?:s| you) for (?:applying|your application|submitting your application)",
        r"(?:we(?:'ve| have)|has been|was) received (?:your|the) application",
        r"your application (?:has been|was) (?:received|submitted)", r"application (?:received|confirmation)"),
        True, "MOVE_TO_ACKNOWLEDGED", "APPLIED"),
)
# Event types that are about a person answering (not an automatic receipt): they make a follow-up unnecessary.
HUMAN_REPLY_TYPES = frozenset({
    "RECRUITER_RESPONSE", "INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE", "ASSESSMENT_INVITATION",
    "ASSESSMENT_REMINDER", "ADDITIONAL_INFORMATION_REQUEST", "DOCUMENT_REQUEST", "FOLLOWUP_RESPONSE",
    "APPLICATION_ON_HOLD", "APPLICATION_REJECTION", "OFFER", "OFFER_UPDATE", "BACKGROUND_CHECK", "ONBOARDING"})
# Families: a model's reading agrees with a rule when both land in the same family.
FAMILY = {"INTERVIEW_INVITATION": "INTERVIEW", "INTERVIEW_RESCHEDULE": "INTERVIEW",
          "ASSESSMENT_INVITATION": "ASSESSMENT", "ASSESSMENT_REMINDER": "ASSESSMENT", "OFFER": "OFFER",
          "OFFER_UPDATE": "OFFER"}


def family(event_type: str) -> str:
    return FAMILY.get(event_type, event_type)


def match_rule(subject: str, body: str) -> Rule | None:
    text = f"{subject}\n{body}"
    for rule in RULES:
        if any(p.search(text) for p in rule.patterns) and not any(p.search(text) for p in rule.unless):
            return rule
    return None


def label(event_type: str) -> str:
    return event_type.replace("_", " ").capitalize().replace("Followup", "Follow-up")


def rule_analysis(rule: Rule, *, needs_review: bool) -> ApplicationEmailAnalysis:
    """The event a rule alone supports: its type and the stage it implies — no dates, links or names."""
    return ApplicationEmailAnalysis(
        event_type=rule.event_type, confidence=rule.confidence, summary=f"{label(rule.event_type)}.",
        application_stage_detected=rule.stage, requires_user_action=rule.event_type not in (
            "APPLICATION_CONFIRMATION", "APPLICATION_REJECTION", "APPLICATION_ON_HOLD"),
        action_items=[], important_facts=[], recommended_tracker_action=rule.action, needs_review=needs_review)


def unknown_analysis(automated: bool) -> ApplicationEmailAnalysis:
    kind = "AUTOMATED_NOTIFICATION" if automated else "UNKNOWN"
    return ApplicationEmailAnalysis(event_type=kind, confidence=0.3, summary="A recruiting email that could not be "
                                    "analysed automatically; please review it.", requires_user_action=False,
                                    important_facts=[], needs_review=True)
