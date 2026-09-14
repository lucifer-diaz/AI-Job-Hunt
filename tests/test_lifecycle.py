"""The transition table itself: pure checks, no database."""
from collections import deque
from itertools import product

import pytest

from app.services.lifecycle import (
    DEFAULT_RETRYABLE,
    FAILABLE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    ErrorCategory,
    InvalidState,
    InvalidTransition,
    WorkflowState,
    can_transition,
    is_terminal,
    parse_state,
    validate_transition,
)

S = WorkflowState
CANONICAL = ["DISCOVERED", "NORMALIZED", "FILTERED", "RANKED", "QUALIFIED", "TAILORING", "TAILORED", "REVIEWED",
             "READY_TO_APPLY", "APPLYING", "APPLIED", "FOLLOWUP_DUE", "FOLLOWED_UP", "INTERVIEW", "REJECTED", "OFFER",
             "ARCHIVED", "SKIPPED", "FAILED"]

# The examples from the specification, all of which must be allowed.
REQUIRED = [
    (S.DISCOVERED, S.NORMALIZED), (S.NORMALIZED, S.FILTERED), (S.FILTERED, S.RANKED),
    (S.RANKED, S.QUALIFIED), (S.RANKED, S.ARCHIVED), (S.QUALIFIED, S.TAILORING),
    (S.TAILORING, S.TAILORED), (S.TAILORING, S.FAILED), (S.TAILORED, S.REVIEWED),
    (S.REVIEWED, S.READY_TO_APPLY), (S.REVIEWED, S.SKIPPED), (S.READY_TO_APPLY, S.APPLYING),
    (S.APPLYING, S.APPLIED), (S.APPLYING, S.SKIPPED), (S.APPLYING, S.FAILED),
    (S.APPLIED, S.FOLLOWUP_DUE), (S.FOLLOWUP_DUE, S.FOLLOWED_UP),
    (S.FOLLOWED_UP, S.INTERVIEW), (S.FOLLOWED_UP, S.REJECTED), (S.FOLLOWED_UP, S.OFFER),
]

# Shortcuts that would let a job skip evidence-based work, which must never be allowed.
FORBIDDEN = [
    (S.DISCOVERED, S.RANKED), (S.DISCOVERED, S.APPLYING), (S.RANKED, S.TAILORING), (S.QUALIFIED, S.READY_TO_APPLY),
    (S.TAILORED, S.READY_TO_APPLY), (S.TAILORING, S.APPLYING), (S.READY_TO_APPLY, S.APPLIED),
    (S.APPLIED, S.FAILED), (S.INTERVIEW, S.FAILED), (S.APPLIED, S.APPLYING), (S.OFFER, S.REJECTED),
    (S.SKIPPED, S.DISCOVERED), (S.ARCHIVED, S.QUALIFIED),
]


def test_exactly_the_canonical_states_exist():
    assert [s.value for s in WorkflowState] == CANONICAL
    assert set(TRANSITIONS) == set(WorkflowState)


@pytest.mark.parametrize("current,target", REQUIRED)
def test_required_transitions_are_allowed(current, target):
    assert can_transition(current, target)
    assert validate_transition(current, target) is target


@pytest.mark.parametrize("current,target", FORBIDDEN)
def test_forbidden_shortcuts_raise(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransition) as error:
        validate_transition(current, target)
    assert error.value.current is current and error.value.target is target


@pytest.mark.parametrize("current,target", list(product(WorkflowState, WorkflowState)))
def test_every_pair_is_decided_by_the_table_alone(current, target):
    expected = target in TRANSITIONS[current]
    assert can_transition(current, target) is expected
    if not expected:
        with pytest.raises(InvalidTransition):
            validate_transition(current, target)


def test_no_state_transitions_to_itself():
    assert all(state not in targets for state, targets in TRANSITIONS.items())


def test_terminal_states():
    assert TERMINAL_STATES == {S.OFFER, S.REJECTED, S.ARCHIVED, S.SKIPPED, S.FAILED}
    for state in TERMINAL_STATES:
        assert is_terminal(state) and not TRANSITIONS[state]
        assert all(not can_transition(state, target) for target in WorkflowState)
    assert not is_terminal(S.INTERVIEW)


def test_every_state_is_reachable_and_every_path_can_end():
    seen, queue = {S.DISCOVERED}, deque([S.DISCOVERED])
    while queue:
        for nxt in TRANSITIONS[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert seen == set(WorkflowState)
    for state in set(WorkflowState) - TERMINAL_STATES:
        frontier, reached = {state}, set()
        while frontier:
            reached |= frontier
            frontier = {n for s in frontier for n in TRANSITIONS[s]} - reached
        assert reached & TERMINAL_STATES, f"{state} can never finish"


def test_failed_is_only_possible_before_submission():
    assert S.APPLYING in FAILABLE_STATES
    assert not FAILABLE_STATES & {S.APPLIED, S.FOLLOWUP_DUE, S.FOLLOWED_UP, S.INTERVIEW}


@pytest.mark.parametrize("value", ["BOGUS", "discovered", "Discovered", " DISCOVERED", "", None, 3, "shortlisted"])
def test_arbitrary_values_are_not_states(value):
    with pytest.raises(InvalidState):
        parse_state(value)
    with pytest.raises(InvalidState):
        can_transition(S.DISCOVERED, value)


def test_parse_accepts_exact_strings():
    assert parse_state("READY_TO_APPLY") is S.READY_TO_APPLY


def test_failed_can_only_be_requeued_to_the_state_it_failed_in():
    assert can_transition(S.FAILED, S.TAILORING, previous_state=S.TAILORING)
    assert not can_transition(S.FAILED, S.TAILORING, previous_state=S.APPLYING)
    assert not can_transition(S.FAILED, S.TAILORING)
    # APPLIED can never fail, so a FAILED job can never claim to return there.
    assert not can_transition(S.FAILED, S.APPLIED, previous_state=S.APPLIED)
    with pytest.raises(InvalidTransition, match="requeued"):
        validate_transition(S.FAILED, S.DISCOVERED, previous_state=S.TAILORING)


def test_every_error_category_has_a_retry_default():
    assert set(DEFAULT_RETRYABLE) == set(ErrorCategory)
    assert DEFAULT_RETRYABLE[ErrorCategory.BLOCKED] is False
    assert DEFAULT_RETRYABLE[ErrorCategory.LEASE_EXPIRED] is True
