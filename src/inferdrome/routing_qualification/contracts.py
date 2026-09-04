"""Strict, additive contracts for stale-telemetry qualification v1."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from inferdrome.domain.base import FrozenModel

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
TerminalStatus = Literal[
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
]
PolicyId = Literal[
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
]

_TERMINAL_STATUSES: tuple[TerminalStatus, ...] = (
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
)
_TRIALS: tuple[tuple[PolicyId, str], ...] = (
    ("fail_closed_required_load_v1", "trial-fail-closed-v1"),
    ("explicit_fail_open_stale_load_v1", "trial-fail-open-v1"),
    ("typed_admissible_state_only_v1", "trial-typed-v1"),
)


class QualificationModel(FrozenModel):
    """Strict immutable model base for one local qualification descriptor."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class SourceInputDigests(QualificationModel):
    """Identities of the canonical input vector already sealed by R1."""

    campaign_plan_sha256: Digest
    request_trace_sha256: Digest
    fault_schedule_sha256: Digest
    trial_plan_sha256: Digest


class TerminalPopulation(QualificationModel):
    """One never-pooled terminal population for a policy/repetition trial."""

    values: dict[TerminalStatus, Annotated[int, Field(ge=0)]]

    @model_validator(mode="after")
    def _is_complete_for_one_fixed_trace(self) -> TerminalPopulation:
        if set(self.values) != set(_TERMINAL_STATUSES):
            raise ValueError("terminal population keys are not the fixed inventory")
        if sum(self.values.values()) != 6:
            raise ValueError("terminal population does not close the six-request trial")
        return self


class StaleTelemetryFaultTimeline(QualificationModel):
    """The one falsifiable virtual-time fault condition qualified by this slice."""

    load_observer_pause_at_ms: Literal[15]
    health_continues: Literal[True]
    first_stale_load_fresh_health_decision_time_ms: Literal[20]
    health_age_ms: Literal[0]
    load_age_ms: Literal[10]
    freshness_bound_ms: Literal[5]


class QualifiedTrial(QualificationModel):
    """One declared cold repetition and its digested request-level receipts."""

    policy_id: PolicyId
    repetition_index: Literal[0]
    trial_id: str
    request_denominator: Literal[6]
    reset_receipt_sha256: Digest
    state_observations_sha256: Digest
    state_observation_count: Literal[36]
    route_decisions_sha256: Digest
    route_decision_count: Literal[6]
    terminal_outcomes_sha256: Digest
    terminal_outcome_count: Literal[6]
    terminal_population: TerminalPopulation


class StaleTelemetryQualification(QualificationModel):
    """Canonical overlay that qualifies one verified R1 package without pooling."""

    schema_version: Literal["inferdrome.stale-telemetry-qualification.v1"]
    qualification_id: Literal["stale-telemetry-qualification-v1"]
    source_campaign_id: Literal["routing-campaign-v1"]
    source_execution_mode: Literal["SYNTHETIC_CPU_ONLY"]
    source_package_retained_digest: Digest
    source_inputs: SourceInputDigests
    fault_timeline: StaleTelemetryFaultTimeline
    repetitions_per_mode: Literal[1]
    population_accounting: Literal["SEPARATE_PER_TRIAL_NO_POOLING"]
    trials: tuple[QualifiedTrial, QualifiedTrial, QualifiedTrial]

    @model_validator(mode="after")
    def _is_the_one_declared_repetition_for_every_mode(
        self,
    ) -> StaleTelemetryQualification:
        actual = tuple((trial.policy_id, trial.trial_id) for trial in self.trials)
        if actual != _TRIALS:
            raise ValueError("qualification trials are not the fixed policy order")
        if any(trial.repetition_index != 0 for trial in self.trials):
            raise ValueError("qualification repetition identity is not zero-based")
        if len({trial.trial_id for trial in self.trials}) != len(self.trials):
            raise ValueError("qualification trial identities are not unique")
        return self
