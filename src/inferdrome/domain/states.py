"""Run and evidence state enumerations."""

from enum import StrEnum


class RunState(StrEnum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    WARMUP = "WARMUP"
    MEASURING = "MEASURING"
    FINALIZING = "FINALIZING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class IntegrityStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    VALID = "VALID"
    INVALID = "INVALID"


class EnvironmentCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class EvidenceEligibility(StrEnum):
    CUSTOMER_ELIGIBLE = "CUSTOMER_ELIGIBLE"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
    INELIGIBLE = "INELIGIBLE"


class Replayability(StrEnum):
    FULL = "FULL"
    LIMITED = "LIMITED"
    NONE = "NONE"


_TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.FAILED, RunState.INTERRUPTED})
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset(
        {RunState.PREFLIGHT, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.PREFLIGHT: frozenset(
        {RunState.WARMUP, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.WARMUP: frozenset(
        {RunState.MEASURING, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.MEASURING: frozenset(
        {RunState.FINALIZING, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.FINALIZING: frozenset(
        {RunState.COMPLETE, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.COMPLETE: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.INTERRUPTED: frozenset(),
}


def is_terminal_run_state(state: RunState) -> bool:
    return state in _TERMINAL_STATES


def validate_run_transition(current: RunState, target: RunState) -> None:
    """Raise when a run-state transition is not in the frozen v0.1 graph."""

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(
            f"invalid run-state transition: {current.value} -> {target.value}"
        )
