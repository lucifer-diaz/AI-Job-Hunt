"""Canonical job lifecycle: the only states a job may hold and the only moves between them.

This module is the single definition of the workflow. Models, migrations, workers and tests all
read it; nothing else may decide whether a state change is legal.

    DISCOVERED → NORMALIZED → FILTERED → RANK_PENDING → RANKING → RANKED → QUALIFIED → TAILORING → TAILORED
    → REVIEWED → READY_TO_APPLY → APPLYING → APPLIED → FOLLOWUP_DUE → FOLLOWED_UP → INTERVIEW / REJECTED / OFFER

Terminal states: OFFER, REJECTED, ARCHIVED, SKIPPED, FAILED. FAILED is terminal for automatic
processing; an operator may requeue it, and only back to the state it failed in. An ARCHIVED job that was
archived by ranking (from RANKED or QUALIFIED) may be reopened for re-ranking, and only to RANK_PENDING.
A job SKIPPED before ranking for want of a description returns to NORMALIZED once it has one.
"""
from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class WorkflowState(StrEnum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    FILTERED = "FILTERED"
    RANK_PENDING = "RANK_PENDING"
    RANKING = "RANKING"
    RANKED = "RANKED"
    QUALIFIED = "QUALIFIED"
    TAILORING = "TAILORING"
    TAILORED = "TAILORED"
    REVIEWED = "REVIEWED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    FOLLOWUP_DUE = "FOLLOWUP_DUE"
    FOLLOWED_UP = "FOLLOWED_UP"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    ARCHIVED = "ARCHIVED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ErrorCategory(StrEnum):
    TRANSIENT = "TRANSIENT"                # network blip, deadlock, temporary unavailability
    RATE_LIMITED = "RATE_LIMITED"          # provider throttling
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"  # Apify, LLM provider, email provider returned an error
    TIMEOUT = "TIMEOUT"
    LEASE_EXPIRED = "LEASE_EXPIRED"        # a worker crashed or stalled while holding the job
    VALIDATION = "VALIDATION"              # bad or incomplete data; retrying cannot help
    CONFIGURATION = "CONFIGURATION"        # missing credentials or settings
    BLOCKED = "BLOCKED"                    # CAPTCHA, login wall or anti-bot control; never bypassed
    UNKNOWN = "UNKNOWN"


# Whether an error of each category is worth another attempt when the caller does not say.
DEFAULT_RETRYABLE: dict[ErrorCategory, bool] = {
    ErrorCategory.TRANSIENT: True,
    ErrorCategory.RATE_LIMITED: True,
    ErrorCategory.EXTERNAL_SERVICE: True,
    ErrorCategory.TIMEOUT: True,
    ErrorCategory.LEASE_EXPIRED: True,
    ErrorCategory.VALIDATION: False,
    ErrorCategory.CONFIGURATION: False,
    ErrorCategory.BLOCKED: False,
    ErrorCategory.UNKNOWN: True,
}

S = WorkflowState

TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    S.DISCOVERED: frozenset({S.NORMALIZED, S.SKIPPED, S.FAILED}),
    S.NORMALIZED: frozenset({S.FILTERED, S.SKIPPED, S.FAILED}),
    S.FILTERED: frozenset({S.RANK_PENDING, S.SKIPPED, S.FAILED}),
    S.RANK_PENDING: frozenset({S.RANKING, S.SKIPPED, S.FAILED}),
    # A ranking attempt either finishes or goes back to wait; it never fails in place, so a FAILED ranking
    # job is always requeued to RANK_PENDING, where a new ranking task can pick it up.
    S.RANKING: frozenset({S.RANKED, S.RANK_PENDING, S.SKIPPED}),
    # Python, not the model, decides between QUALIFIED and ARCHIVED (app.services.ranker.qualify).
    S.RANKED: frozenset({S.QUALIFIED, S.ARCHIVED, S.RANK_PENDING, S.SKIPPED, S.FAILED}),
    # Re-ranking (profile, resume or prompt changed, or the user asked) is possible until tailoring starts.
    S.QUALIFIED: frozenset({S.TAILORING, S.ARCHIVED, S.RANK_PENDING, S.SKIPPED}),
    S.TAILORING: frozenset({S.TAILORED, S.SKIPPED, S.FAILED}),
    S.TAILORED: frozenset({S.REVIEWED, S.SKIPPED, S.FAILED}),
    # A review may send the packet back for another tailoring pass.
    S.REVIEWED: frozenset({S.READY_TO_APPLY, S.TAILORING, S.SKIPPED, S.FAILED}),
    S.READY_TO_APPLY: frozenset({S.APPLYING, S.ARCHIVED, S.SKIPPED}),
    S.APPLYING: frozenset({S.APPLIED, S.SKIPPED, S.FAILED}),
    # After submission the job never becomes FAILED: a failed follow-up must not erase that we applied.
    S.APPLIED: frozenset({S.FOLLOWUP_DUE, S.INTERVIEW, S.REJECTED, S.OFFER, S.ARCHIVED}),
    S.FOLLOWUP_DUE: frozenset({S.FOLLOWED_UP, S.INTERVIEW, S.REJECTED, S.OFFER, S.ARCHIVED}),
    S.FOLLOWED_UP: frozenset({S.FOLLOWUP_DUE, S.INTERVIEW, S.REJECTED, S.OFFER, S.ARCHIVED}),
    S.INTERVIEW: frozenset({S.OFFER, S.REJECTED, S.ARCHIVED}),
    S.OFFER: frozenset(),
    S.REJECTED: frozenset(),
    S.ARCHIVED: frozenset(),
    S.SKIPPED: frozenset(),
    S.FAILED: frozenset(),
}

TERMINAL_STATES: frozenset[WorkflowState] = frozenset(s for s, targets in TRANSITIONS.items() if not targets)
FAILABLE_STATES: frozenset[WorkflowState] = frozenset(s for s, targets in TRANSITIONS.items() if S.FAILED in targets)
# ARCHIVED stays terminal for automation; a job archived by ranking may be reopened, only for re-ranking.
REOPENABLE_ARCHIVE_SOURCES: frozenset[WorkflowState] = frozenset({S.RANKED, S.QUALIFIED})
# SKIPPED stays terminal too, with one exception: a posting skipped *before* ranking — because it arrived
# without a usable description — is ranked after all once that description turns up (a fuller re-import, or
# one pasted by hand). Nothing else about a skip is reversible.
REOPENABLE_SKIP_SOURCES: frozenset[WorkflowState] = frozenset({S.NORMALIZED})


class InvalidState(ValueError):
    """A value that is not one of the canonical workflow states."""


class InvalidTransition(ValueError):
    """A state change the lifecycle does not allow."""

    def __init__(self, current: WorkflowState | None, target: WorkflowState, detail: str = ""):
        self.current, self.target = current, target
        allowed = ", ".join(sorted(TRANSITIONS.get(current, ()))) if current else "any initial state"
        super().__init__(detail or f"Cannot move from {current} to {target}; allowed: {allowed or 'none (terminal)'}")


def parse_state(value: object) -> WorkflowState:
    """Strictly convert a value to a WorkflowState. Unknown or differently-cased strings are rejected."""
    if isinstance(value, WorkflowState):
        return value
    if isinstance(value, str):
        try:
            return WorkflowState(value)
        except ValueError:
            pass
    raise InvalidState(f"{value!r} is not a workflow state; expected one of: {', '.join(WorkflowState)}")


def parse_states(values: Iterable[object]) -> list[WorkflowState]:
    return [parse_state(value) for value in values]


def is_terminal(state: object) -> bool:
    return parse_state(state) in TERMINAL_STATES


def can_transition(current: object, target: object, *, previous_state: object | None = None) -> bool:
    current, target = parse_state(current), parse_state(target)
    if current is S.FAILED:
        # Requeue: only back to the state the job failed in.
        return previous_state is not None and parse_state(previous_state) == target and target in FAILABLE_STATES
    if current is S.ARCHIVED and target is S.RANK_PENDING:
        return previous_state is not None and parse_state(previous_state) in REOPENABLE_ARCHIVE_SOURCES
    if current is S.SKIPPED and target is S.NORMALIZED:
        return previous_state is not None and parse_state(previous_state) in REOPENABLE_SKIP_SOURCES
    return target in TRANSITIONS[current]


def validate_transition(current: object, target: object, *, previous_state: object | None = None) -> WorkflowState:
    current_state, target_state = parse_state(current), parse_state(target)
    if not can_transition(current_state, target_state, previous_state=previous_state):
        if current_state is S.FAILED:
            raise InvalidTransition(current_state, target_state,
                                    f"A FAILED job may only be requeued to the state it failed in ({previous_state})")
        if current_state is S.ARCHIVED and target_state is S.RANK_PENDING:
            raise InvalidTransition(current_state, target_state,
                                    f"Only a job archived by ranking can be re-ranked (it was archived from {previous_state})")
        raise InvalidTransition(current_state, target_state)
    return target_state
