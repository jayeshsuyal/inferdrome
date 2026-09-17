"""Deterministic load-calibration declarations for matched routing studies.

The calibration contract is deliberately separate from a study result.  It
freezes a finite load sweep and a selection rule before any observation is
accepted, then emits a confirmation plan only when every calibration trial is
complete and admissible.  It neither opens a transport nor changes an existing
study report's historical ``UNCALIBRATED_REHEARSAL`` classification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    ClosedModel,
    EvaluationError,
    Outcome,
)
from inferdrome.evaluation.policies import POLICY_IDS, PolicyId
from inferdrome.evaluation.study_config import Digest, SafeId
from inferdrome.parsing import bounded_json_float, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

MAX_LOAD_LEVELS = 8
MAX_CALIBRATION_REPEATS = 16
MAX_CONFIRMATION_REPEATS = 32
MAX_PROTOCOL_TRIALS = 256
MAX_PROTOCOL_DURATION_NS = 86_400_000_000_000
MAX_OFFERED_WINDOW_NS = 300_000_000_000
# This is a declared reserve, not a readiness claim. It is deliberately long
# enough to bound a two-engine cold reset in the later local rehearsal rather
# than requiring an unaccounted-for setup phase outside the protocol.
MAX_WARMUP_RESET_DURATION_NS = 1_800_000_000_000
_OUTCOMES: tuple[Outcome, ...] = (
    "SUCCESS",
    "REJECTED_CAPACITY",
    "REJECTED_ROUTE",
    "TIMEOUT",
    "HTTP_ERROR",
    "STREAM_ERROR",
    "STREAM_LIMIT",
    "INCOMPLETE_STREAM",
    "TRANSPORT_ERROR",
    "CANCELLED",
    "DRAIN_TIMEOUT",
    "INTERNAL_ERROR",
)


class TokenLengthDesign(ClosedModel):
    """Declared lengths, not a tokenizer or a claim that tokenization ran."""

    prompt_tokens: Annotated[int, Field(ge=1, le=8192)]
    completion_tokens: Annotated[int, Field(ge=1, le=4096)]
    provenance: Literal["DECLARED_INPUT_NOT_TOKENIZER_VERIFIED"] = (
        "DECLARED_INPUT_NOT_TOKENIZER_VERIFIED"
    )


class WorkloadIdentity(ClosedModel):
    model: Literal["Qwen/Qwen3-8B"] = "Qwen/Qwen3-8B"
    model_revision_sha256: Digest
    workload_sha256: Digest
    trace_sha256: Digest
    token_lengths: TokenLengthDesign


class LoadLevel(ClosedModel):
    """One fully prepared candidate whose plan is frozen before execution."""

    level_id: SafeId
    study_config_sha256: Digest
    study_plan_sha256: Digest
    offered_rate_millirps: Annotated[int, Field(ge=1, le=1_000_000)]
    planned_requests: Annotated[int, Field(ge=4, le=100_000)]
    worst_case_duration_ns: Annotated[int, Field(ge=1, le=MAX_PROTOCOL_DURATION_NS)]

    @model_validator(mode="after")
    def offered_window_is_representable(self) -> Self:
        """Reject a rate that cannot be represented by the fixed ns clock."""
        numerator = self.planned_requests * 1_000_000_000_000
        if numerator % self.offered_rate_millirps:
            raise ValueError("calibration offered rate has no integral ns window")
        window_ns = numerator // self.offered_rate_millirps
        if not 1 <= window_ns <= MAX_OFFERED_WINDOW_NS:
            raise ValueError("calibration offered window exceeds its bound")
        return self


class CalibrationPreparation(ClosedModel):
    warmup_before_each_trial: Literal[True] = True
    warmup_scope: Literal["DECLARED_ENDPOINT_PAIR_ONLY"] = "DECLARED_ENDPOINT_PAIR_ONLY"
    reset_before_each_trial: Literal["DECLARED_COLD_RESET"] = "DECLARED_COLD_RESET"
    warmup_reset_max_duration_ns: Annotated[
        int, Field(ge=1, le=MAX_WARMUP_RESET_DURATION_NS)
    ]
    stop_rule: Literal["STOP_PHASE_ON_INCOMPLETE_TERMINAL_POPULATION"] = (
        "STOP_PHASE_ON_INCOMPLETE_TERMINAL_POPULATION"
    )


class CalibrationSelectionRule(ClosedModel):
    primary_metric: Literal["MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS"] = (
        "MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS"
    )
    latency_origin: Literal["SCHEDULED_OFFER_TO_CONTENT_AND_TERMINAL"] = (
        "SCHEDULED_OFFER_TO_CONTENT_AND_TERMINAL"
    )
    selection: Literal["HIGHEST_LEVEL_WITH_ALL_TERMINALS_SLO_GOOD"] = (
        "HIGHEST_LEVEL_WITH_ALL_TERMINALS_SLO_GOOD"
    )
    no_eligible_level: Literal["NO_CONFIRMATION_PLAN"] = "NO_CONFIRMATION_PLAN"


class LoadCalibrationProtocol(ClosedModel):
    """A local-only, bounded declaration with no executable endpoint fields."""

    schema_version: Literal["inferdrome.evaluation-load-calibration-protocol.v1"]
    protocol_id: SafeId
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    workload: WorkloadIdentity
    levels: Annotated[
        tuple[LoadLevel, ...], Field(min_length=2, max_length=MAX_LOAD_LEVELS)
    ]
    policy_order: Annotated[tuple[PolicyId, ...], Field(min_length=4, max_length=4)]
    calibration_repetitions: Annotated[int, Field(ge=1, le=MAX_CALIBRATION_REPEATS)]
    confirmation_repetitions: Annotated[int, Field(ge=1, le=MAX_CONFIRMATION_REPEATS)]
    first_content_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    completion_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    preparation: CalibrationPreparation
    selection_rule: CalibrationSelectionRule
    per_trial_output_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)]
    max_session_duration_ns: Annotated[int, Field(ge=1, le=MAX_PROTOCOL_DURATION_NS)]
    max_session_output_bytes: Annotated[int, Field(ge=1, le=1024 * 1024 * 1024)]

    @model_validator(mode="after")
    def finite_frozen_protocol(self) -> Self:
        if tuple(self.policy_order) != POLICY_IDS:
            raise ValueError(
                "calibration policy order must be the declared policy order"
            )
        if self.first_content_slo_ns > self.completion_slo_ns:
            raise ValueError("calibration latency targets are unordered")
        if len({level.level_id for level in self.levels}) != len(self.levels):
            raise ValueError("calibration level identities must be unique")
        if len({level.study_plan_sha256 for level in self.levels}) != len(self.levels):
            raise ValueError("each load level requires one distinct study plan")
        rates = tuple(level.offered_rate_millirps for level in self.levels)
        if rates != tuple(sorted(rates)) or len(set(rates)) != len(rates):
            raise ValueError("calibration offered rates must be strictly ascending")
        calibration = len(self.levels) * self.calibration_repetitions * len(POLICY_IDS)
        # Confirmation is deliberately two-phase: healthy context followed by
        # the first controlled stale-load fault. A calibration selector never
        # judges the expected fail-closed stale-fault outcomes.
        confirmation = self.confirmation_repetitions * 2 * len(POLICY_IDS)
        if calibration + confirmation > MAX_PROTOCOL_TRIALS:
            raise ValueError("calibration trial budget exceeds its bound")
        worst_case = (
            sum(
                level.worst_case_duration_ns
                * self.calibration_repetitions
                * len(POLICY_IDS)
                for level in self.levels
            )
            + max(level.worst_case_duration_ns for level in self.levels) * confirmation
            + 2
            * (calibration + confirmation)
            * self.preparation.warmup_reset_max_duration_ns
        )
        if worst_case > self.max_session_duration_ns:
            raise ValueError("calibration duration reserve exceeds its session bound")
        if (calibration + confirmation) * self.per_trial_output_bytes > (
            self.max_session_output_bytes
        ):
            raise ValueError("calibration output reserve exceeds its session bound")
        return self


class CalibrationTrial(ClosedModel):
    trial_id: Annotated[str, Field(pattern=r"^calibration-[0-9]{4}$")]
    level_id: SafeId
    repeat_index: Annotated[int, Field(ge=0, le=MAX_CALIBRATION_REPEATS - 1)]
    policy_id: PolicyId
    scenario: Literal["HEALTHY"] = "HEALTHY"
    study_config_sha256: Digest
    study_plan_sha256: Digest
    offered_rate_millirps: Annotated[int, Field(ge=1, le=1_000_000)]
    planned_requests: Annotated[int, Field(ge=4, le=100_000)]
    worst_case_duration_ns: Annotated[int, Field(ge=1, le=MAX_PROTOCOL_DURATION_NS)]


@dataclass(frozen=True)
class CompiledCalibrationPlan:
    protocol: LoadCalibrationProtocol
    protocol_sha256: str
    trials: tuple[CalibrationTrial, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "inferdrome.evaluation-load-calibration-plan.v1",
            "compiler": "FINITE_LOAD_SWEEP_AND_CONFIRMATION_V1",
            "protocol_sha256": self.protocol_sha256,
            "phase": "CALIBRATION",
            "trials": [trial.model_dump(mode="json") for trial in self.trials],
            "workload": self.protocol.workload.model_dump(mode="json"),
            "calibration_repetitions": self.protocol.calibration_repetitions,
            "confirmation_repetitions": self.protocol.confirmation_repetitions,
            "policy_order": list(self.protocol.policy_order),
            "first_content_slo_ns": self.protocol.first_content_slo_ns,
            "completion_slo_ns": self.protocol.completion_slo_ns,
            "preparation": self.protocol.preparation.model_dump(mode="json"),
            "selection_rule": self.protocol.selection_rule.model_dump(mode="json"),
            "max_session_duration_ns": self.protocol.max_session_duration_ns,
            "per_trial_output_bytes": self.protocol.per_trial_output_bytes,
            "max_session_output_bytes": self.protocol.max_session_output_bytes,
            "evidence_eligible": False,
            "runtime_identity": "UNVERIFIED",
            "cost": "UNAVAILABLE",
        }


class CalibrationTrialObservation(ClosedModel):
    """One returned terminal population, with no request text or endpoint origin."""

    trial_id: Annotated[str, Field(pattern=r"^calibration-[0-9]{4}$")]
    level_id: SafeId
    repeat_index: Annotated[int, Field(ge=0, le=MAX_CALIBRATION_REPEATS - 1)]
    policy_id: PolicyId
    scenario: Literal["HEALTHY"] = "HEALTHY"
    study_plan_sha256: Digest
    offered_count: Annotated[int, Field(ge=1, le=100_000)]
    dispatched_count: Annotated[int, Field(ge=0, le=100_000)]
    terminal_count: Annotated[int, Field(ge=0, le=100_000)]
    outcomes: dict[Outcome, Annotated[int, Field(ge=0, le=100_000)]]
    slo_good_count: Annotated[int, Field(ge=0, le=100_000)]
    achieved_concurrency_peak: Annotated[int, Field(ge=0, le=64)]
    client_queue_peak: Annotated[int, Field(ge=0, le=1024)]
    first_content_p95_ns: Annotated[int, Field(ge=0, le=60_000_000_000)] | None
    terminal_p95_ns: Annotated[int, Field(ge=0, le=60_000_000_000)] | None
    offered_window_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    latency_origin: Literal["SCHEDULED_OFFER_TO_CONTENT_AND_TERMINAL"] = (
        "SCHEDULED_OFFER_TO_CONTENT_AND_TERMINAL"
    )

    @model_validator(mode="after")
    def complete_terminal_population(self) -> Self:
        if set(self.outcomes) != set(_OUTCOMES):
            raise ValueError("calibration outcomes must retain every terminal category")
        if (
            sum(self.outcomes.values()) != self.terminal_count
            or self.slo_good_count > self.outcomes["SUCCESS"]
            or self.outcomes["SUCCESS"] > self.dispatched_count
            or self.dispatched_count > self.terminal_count
            or self.terminal_count > self.offered_count
        ):
            raise ValueError("calibration terminal population is inconsistent")
        if self.dispatched_count > 0 and self.achieved_concurrency_peak == 0:
            raise ValueError("calibration dispatch lacks an observed active peak")
        successes = self.outcomes["SUCCESS"]
        if (self.first_content_p95_ns is None) != (successes == 0):
            raise ValueError("calibration content latency coverage is inconsistent")
        if (self.terminal_p95_ns is None) != (successes == 0):
            raise ValueError("calibration terminal latency coverage is inconsistent")
        if self.first_content_p95_ns is not None and (
            self.terminal_p95_ns is None
            or self.first_content_p95_ns > self.terminal_p95_ns
        ):
            raise ValueError("calibration latency percentiles are unordered")
        return self


class CalibrationObservationSet(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-load-calibration-observations.v1"]
    protocol_sha256: Digest
    trials: Annotated[tuple[CalibrationTrialObservation, ...], Field(max_length=256)]


class CalibrationLevelAssessment(ClosedModel):
    level_id: SafeId
    offered_rate_millirps: Annotated[int, Field(ge=1, le=1_000_000)]
    minimum_all_offered_slo_goodput_millirps: Annotated[int, Field(ge=0, le=1_000_000)]
    admissible: bool
    reason: Literal[
        "ALL_TERMINALS_SLO_GOOD",
        "INCOMPLETE_TERMINAL_POPULATION",
        "SLO_MISS_OR_LATENCY_TARGET_MISS",
    ]


class CalibrationSelection(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-load-calibration-selection.v1"]
    protocol_sha256: Digest
    observations_sha256: Digest
    status: Literal["SELECTED", "NO_ADMISSIBLE_LEVEL"]
    selected_level_id: SafeId | None
    primary_metric: Literal["MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS"]
    selection: Literal["HIGHEST_LEVEL_WITH_ALL_TERMINALS_SLO_GOOD"]
    assessments: Annotated[
        tuple[CalibrationLevelAssessment, ...], Field(min_length=2, max_length=8)
    ]

    @model_validator(mode="after")
    def selected_level_matches_assessments(self) -> Self:
        selected = [row for row in self.assessments if row.admissible]
        if self.status == "NO_ADMISSIBLE_LEVEL":
            if self.selected_level_id is not None or selected:
                raise ValueError("calibration selection fabricates an eligible level")
        elif (
            self.selected_level_id is None
            or not selected
            or self.selected_level_id
            != max(selected, key=lambda row: row.offered_rate_millirps).level_id
        ):
            raise ValueError("calibration selected level contradicts its assessments")
        return self


class ConfirmationTrial(ClosedModel):
    trial_id: Annotated[str, Field(pattern=r"^confirmation-[0-9]{4}$")]
    level_id: SafeId
    repeat_index: Annotated[int, Field(ge=0, le=MAX_CONFIRMATION_REPEATS - 1)]
    policy_id: PolicyId
    scenario: Literal["HEALTHY", "STALE_LOAD"]
    study_config_sha256: Digest
    study_plan_sha256: Digest
    offered_rate_millirps: Annotated[int, Field(ge=1, le=1_000_000)]
    planned_requests: Annotated[int, Field(ge=4, le=100_000)]
    worst_case_duration_ns: Annotated[int, Field(ge=1, le=MAX_PROTOCOL_DURATION_NS)]


class ConfirmationPlan(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-load-confirmation-plan.v1"]
    protocol_sha256: Digest
    selection_sha256: Digest
    status: Literal["READY", "NO_CONFIRMATION_ALLOWED"]
    selected_level_id: SafeId | None
    trials: Annotated[
        tuple[ConfirmationTrial, ...], Field(max_length=MAX_PROTOCOL_TRIALS)
    ]
    preparation: CalibrationPreparation
    primary_metric: Literal["MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS"]
    evidence_eligible: Literal[False] = False

    @model_validator(mode="after")
    def confirmation_is_exact_or_absent(self) -> Self:
        if self.status == "NO_CONFIRMATION_ALLOWED":
            if self.selected_level_id is not None or self.trials:
                raise ValueError("calibration permits an unsupported confirmation")
        elif self.selected_level_id is None or not self.trials:
            raise ValueError("calibration confirmation is incomplete")
        elif any(trial.level_id != self.selected_level_id for trial in self.trials):
            raise ValueError("calibration confirmation mixes candidate levels")
        elif {trial.scenario for trial in self.trials} != {"HEALTHY", "STALE_LOAD"}:
            raise ValueError("calibration confirmation omits a declared scenario")
        return self


def protocol_bytes(protocol: LoadCalibrationProtocol) -> bytes:
    return canonical_json_bytes(protocol.model_dump(mode="json")) + b"\n"


def _policy_order_for_repeat(repeat_index: int) -> tuple[PolicyId, ...]:
    """Rotate the declared order so repeated study blocks remain balanced."""
    offset = repeat_index % len(POLICY_IDS)
    return POLICY_IDS[offset:] + POLICY_IDS[:offset]


def compile_calibration(protocol: LoadCalibrationProtocol) -> CompiledCalibrationPlan:
    protocol_sha256 = sha256_digest(protocol_bytes(protocol))
    trials: list[CalibrationTrial] = []
    for level in protocol.levels:
        for repeat_index in range(protocol.calibration_repetitions):
            for policy_id in _policy_order_for_repeat(repeat_index):
                trials.append(
                    CalibrationTrial(
                        trial_id=f"calibration-{len(trials):04d}",
                        level_id=level.level_id,
                        repeat_index=repeat_index,
                        policy_id=policy_id,
                        study_config_sha256=level.study_config_sha256,
                        study_plan_sha256=level.study_plan_sha256,
                        offered_rate_millirps=level.offered_rate_millirps,
                        planned_requests=level.planned_requests,
                        worst_case_duration_ns=level.worst_case_duration_ns,
                    )
                )
    return CompiledCalibrationPlan(protocol, protocol_sha256, tuple(trials))


def calibration_plan_bytes(plan: CompiledCalibrationPlan) -> bytes:
    return canonical_json_bytes(plan.to_dict()) + b"\n"


def _observation_bytes(observations: CalibrationObservationSet) -> bytes:
    return canonical_json_bytes(observations.model_dump(mode="json")) + b"\n"


def _validate_observations(
    plan: CompiledCalibrationPlan, observations: CalibrationObservationSet
) -> tuple[CalibrationTrialObservation, ...]:
    if observations.protocol_sha256 != plan.protocol_sha256:
        raise EvaluationError("calibration observations do not bind the protocol")
    expected = {trial.trial_id: trial for trial in plan.trials}
    supplied = {trial.trial_id: trial for trial in observations.trials}
    if len(supplied) != len(observations.trials) or set(supplied) != set(expected):
        raise EvaluationError("calibration observations do not cover the fixed sweep")
    ordered: list[CalibrationTrialObservation] = []
    for trial in plan.trials:
        observed = supplied[trial.trial_id]
        if (
            observed.level_id != trial.level_id
            or observed.repeat_index != trial.repeat_index
            or observed.policy_id != trial.policy_id
            or observed.study_plan_sha256 != trial.study_plan_sha256
            or observed.offered_count != trial.planned_requests
            or observed.offered_count * 1_000_000_000_000
            != trial.offered_rate_millirps * observed.offered_window_ns
        ):
            raise EvaluationError(
                "calibration observation identity or offered rate differs"
            )
        ordered.append(observed)
    return tuple(ordered)


def select_calibration_level(
    plan: CompiledCalibrationPlan, observations: CalibrationObservationSet
) -> CalibrationSelection:
    """Select the highest fully observed candidate; never infer missing results."""
    ordered = _validate_observations(plan, observations)
    by_level: dict[str, list[CalibrationTrialObservation]] = {
        level.level_id: [] for level in plan.protocol.levels
    }
    for observation in ordered:
        by_level[observation.level_id].append(observation)
    phase_stopped = any(row.terminal_count != row.offered_count for row in ordered)
    assessments: list[CalibrationLevelAssessment] = []
    for level in plan.protocol.levels:
        rows = by_level[level.level_id]
        minimum = min(
            row.slo_good_count * 1_000_000_000_000 // row.offered_window_ns
            for row in rows
        )
        terminal_complete = all(
            row.terminal_count == row.offered_count
            and row.dispatched_count <= row.terminal_count
            for row in rows
        )
        slo_good = all(
            row.slo_good_count == row.offered_count
            and row.first_content_p95_ns is not None
            and row.terminal_p95_ns is not None
            and row.first_content_p95_ns <= plan.protocol.first_content_slo_ns
            and row.terminal_p95_ns <= plan.protocol.completion_slo_ns
            for row in rows
        )
        assessments.append(
            CalibrationLevelAssessment(
                level_id=level.level_id,
                offered_rate_millirps=level.offered_rate_millirps,
                minimum_all_offered_slo_goodput_millirps=minimum,
                admissible=not phase_stopped and terminal_complete and slo_good,
                reason=(
                    "ALL_TERMINALS_SLO_GOOD"
                    if not phase_stopped and terminal_complete and slo_good
                    else "INCOMPLETE_TERMINAL_POPULATION"
                    if phase_stopped or not terminal_complete
                    else "SLO_MISS_OR_LATENCY_TARGET_MISS"
                ),
            )
        )
    selected = [assessment for assessment in assessments if assessment.admissible]
    return CalibrationSelection(
        schema_version="inferdrome.evaluation-load-calibration-selection.v1",
        protocol_sha256=plan.protocol_sha256,
        observations_sha256=sha256_digest(_observation_bytes(observations)),
        status="SELECTED" if selected else "NO_ADMISSIBLE_LEVEL",
        selected_level_id=(
            max(selected, key=lambda row: row.offered_rate_millirps).level_id
            if selected
            else None
        ),
        primary_metric=plan.protocol.selection_rule.primary_metric,
        selection=plan.protocol.selection_rule.selection,
        assessments=tuple(assessments),
    )


def compile_confirmation_trials(
    protocol: LoadCalibrationProtocol, level: LoadLevel
) -> tuple[ConfirmationTrial, ...]:
    """Compile the two declared confirmation scenarios for one candidate.

    This is separate from selection so an execution preflight can bind every
    candidate's healthy and stale-load study recipes before it dispatches even
    the first calibration request.
    """
    if level not in protocol.levels:
        raise EvaluationError("calibration confirmation level is not declared")
    trials: list[ConfirmationTrial] = []
    for repeat_index in range(protocol.confirmation_repetitions):
        phase_orders: tuple[tuple[Literal["HEALTHY", "STALE_LOAD"], int], ...] = (
            ("HEALTHY", protocol.calibration_repetitions + repeat_index),
            ("STALE_LOAD", repeat_index),
        )
        for scenario, order_index in phase_orders:
            for policy_id in _policy_order_for_repeat(order_index):
                trials.append(
                    ConfirmationTrial(
                        trial_id=f"confirmation-{len(trials):04d}",
                        level_id=level.level_id,
                        repeat_index=repeat_index,
                        policy_id=policy_id,
                        scenario=scenario,
                        study_config_sha256=level.study_config_sha256,
                        study_plan_sha256=level.study_plan_sha256,
                        offered_rate_millirps=level.offered_rate_millirps,
                        planned_requests=level.planned_requests,
                        worst_case_duration_ns=level.worst_case_duration_ns,
                    )
                )
    return tuple(trials)


def confirmation_plan(
    plan: CompiledCalibrationPlan, selection: CalibrationSelection
) -> ConfirmationPlan:
    """Materialize only the predeclared confirmation repetitions for selection."""
    selection_bytes = canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
    if selection.protocol_sha256 != plan.protocol_sha256:
        raise EvaluationError("calibration selection does not bind the protocol")
    if selection.status == "NO_ADMISSIBLE_LEVEL":
        return ConfirmationPlan(
            schema_version="inferdrome.evaluation-load-confirmation-plan.v1",
            protocol_sha256=plan.protocol_sha256,
            selection_sha256=sha256_digest(selection_bytes),
            status="NO_CONFIRMATION_ALLOWED",
            selected_level_id=None,
            trials=(),
            preparation=plan.protocol.preparation,
            primary_metric=plan.protocol.selection_rule.primary_metric,
        )
    level = next(
        (
            row
            for row in plan.protocol.levels
            if row.level_id == selection.selected_level_id
        ),
        None,
    )
    if level is None:
        raise EvaluationError("calibration selection does not name a declared level")
    return ConfirmationPlan(
        schema_version="inferdrome.evaluation-load-confirmation-plan.v1",
        protocol_sha256=plan.protocol_sha256,
        selection_sha256=sha256_digest(selection_bytes),
        status="READY",
        selected_level_id=level.level_id,
        trials=compile_confirmation_trials(plan.protocol, level),
        preparation=plan.protocol.preparation,
        primary_metric=plan.protocol.selection_rule.primary_metric,
    )


def _load_json(content: bytes, model: type[ClosedModel]) -> ClosedModel:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite value")

    try:
        if not 1 <= len(content) <= MAX_INPUT_BYTES:
            raise ValueError
        text = content.decode("utf-8")
        validate_json_structure(text)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=bounded_json_float,
        )
        return model.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError("load calibration input violates its contract") from None


def load_calibration_protocol_bytes(content: bytes) -> LoadCalibrationProtocol:
    return _load_json(content, LoadCalibrationProtocol)  # type: ignore[return-value]


def load_calibration_observations_bytes(content: bytes) -> CalibrationObservationSet:
    return _load_json(content, CalibrationObservationSet)  # type: ignore[return-value]
