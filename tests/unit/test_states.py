"""Frozen run-state transition graph."""

import itertools

import pytest

from inferdrome.domain.states import RunState, validate_run_transition

HAPPY_PATH = [
    RunState.CREATED,
    RunState.PREFLIGHT,
    RunState.WARMUP,
    RunState.MEASURING,
    RunState.FINALIZING,
    RunState.COMPLETE,
]


def test_happy_path_transitions() -> None:
    for current, target in itertools.pairwise(HAPPY_PATH):
        validate_run_transition(current, target)


@pytest.mark.parametrize(
    "current",
    [
        RunState.CREATED,
        RunState.PREFLIGHT,
        RunState.WARMUP,
        RunState.MEASURING,
        RunState.FINALIZING,
    ],
)
@pytest.mark.parametrize("target", [RunState.FAILED, RunState.INTERRUPTED])
def test_nonterminal_states_can_fail_or_interrupt(
    current: RunState, target: RunState
) -> None:
    validate_run_transition(current, target)


@pytest.mark.parametrize(
    "current", [RunState.COMPLETE, RunState.FAILED, RunState.INTERRUPTED]
)
@pytest.mark.parametrize("target", list(RunState))
def test_terminal_states_have_no_outgoing_transitions(
    current: RunState, target: RunState
) -> None:
    with pytest.raises(ValueError):
        validate_run_transition(current, target)


def test_phase_skips_are_rejected() -> None:
    with pytest.raises(ValueError):
        validate_run_transition(RunState.CREATED, RunState.MEASURING)
