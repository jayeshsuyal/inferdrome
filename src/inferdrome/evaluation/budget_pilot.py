"""Pure compilation for the fixed four-cell Vast budget pilot.

This module is deliberately additive.  It selects a small, declared experiment
without relaxing the frozen four-policy load-calibration or study contracts.
It performs no transport, runtime, filesystem discovery, or provider action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    ClosedModel,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.direct_process_lifecycle import (
    REAL_GPU_STARTUP_TIMEOUT_NS,
    direct_process_lifecycle_reservation,
)
from inferdrome.evaluation.fault_config import (
    FaultTiming,
    RoutingFaultConfig,
    TelemetryBounds,
)
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.load_calibration import (
    CalibrationPreparation,
    WorkloadIdentity,
)
from inferdrome.evaluation.policies import PolicyId
from inferdrome.evaluation.study_config import Digest, SafeId, Scenario, TrialConfig
from inferdrome.parsing import bounded_json_float, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

BASELINE_POLICY: Literal["evaluation_round_robin_v1"] = "evaluation_round_robin_v1"
CANDIDATE_POLICY: Literal["evaluation_freshness_fallback_v1"] = (
    "evaluation_freshness_fallback_v1"
)
PILOT_POLICIES: tuple[PolicyId, PolicyId] = (BASELINE_POLICY, CANDIDATE_POLICY)
PILOT_CONDITIONS: tuple[Scenario, Scenario] = ("HEALTHY", "STALE_LOAD")
PILOT_TRIAL_COUNT = 4
FINAL_CLEANUP_RESERVE_NS = 5_000_000_000
RETRIEVAL_RESERVE_NS = 5_000_000_000
PLAN_MANIFEST_RESERVE_BYTES = 1_048_576
REPORT_RESERVE_BYTES = 8_388_608
SIDECAR_RESERVE_BYTES = 1_048_576
CONTROL_RESERVE_BYTES = 4_194_304


class PilotLoadLevel(ClosedModel):
    """One declared fixed load; it is not a calibration result."""

    level_id: SafeId
    offered_rate_millirps: Annotated[int, Field(ge=1, le=1_000_000)]
    planned_foreground_requests: Annotated[int, Field(ge=4, le=10_000)]
    first_content_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    completion_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]

    @model_validator(mode="after")
    def ordered_slos(self) -> Self:
        if self.first_content_slo_ns > self.completion_slo_ns:
            raise ValueError("pilot latency targets are unordered")
        return self


class BudgetPilotProtocol(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-budget-pilot-protocol.v1"]
    protocol_id: SafeId
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    workload: WorkloadIdentity
    level: PilotLoadLevel
    policy_order: Annotated[tuple[PolicyId, ...], Field(min_length=2, max_length=2)]
    condition_order: Annotated[tuple[Scenario, ...], Field(min_length=2, max_length=2)]
    repetitions_per_cell: Literal[1]
    preparation: CalibrationPreparation
    per_trial_output_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)]
    max_session_duration_ns: Annotated[int, Field(ge=1)]
    max_session_output_bytes: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def exact_four_cell_design(self) -> Self:
        reservation = direct_process_lifecycle_reservation(
            startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS
        )
        if tuple(self.policy_order) != PILOT_POLICIES:
            raise ValueError("pilot policy order must be baseline then candidate")
        if tuple(self.condition_order) != PILOT_CONDITIONS:
            raise ValueError("pilot condition order must be healthy then stale load")
        if (
            self.preparation.warmup_reset_max_duration_ns
            != reservation.required_prepare_timeout_ns
            or self.preparation.cleanup_max_duration_ns
            != reservation.required_cleanup_timeout_ns
        ):
            raise ValueError("pilot lifecycle reserve must match the live lifecycle")
        return self


class BudgetPilotRecipe(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-budget-pilot-recipe.v1"]
    protocol_sha256: Digest
    level_id: SafeId
    window_start_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]
    window_end_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    foreground: EvaluationConfig = Field(repr=False)
    background: EvaluationConfig = Field(repr=False)
    telemetry: TelemetryBounds
    fault: FaultTiming

    @model_validator(mode="after")
    def bounded_shared_recipe(self) -> Self:
        if self.window_start_ns >= self.window_end_ns:
            raise ValueError("pilot recipe window is unordered")
        return self


@dataclass(frozen=True)
class CompiledBudgetPilot:
    protocol_sha256: str
    recipe_sha256: str
    trial_count: int
    planned_foreground_requests: int
    planned_background_requests: int
    lifecycle_prepare_ns_per_trial: int
    lifecycle_cleanup_ns_per_trial: int
    trial_execution_worst_case_duration_ns: int
    execution_worst_case_duration_ns: int
    final_cleanup_reserve_ns: int
    retrieval_reserve_ns: int
    worst_case_duration_ns: int
    reserved_output_bytes: int
    protocol: BudgetPilotProtocol
    recipe: BudgetPilotRecipe

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "inferdrome.evaluation-budget-pilot-plan.v1",
            "compiler": "FIXED_FOUR_CELL_BUDGET_PILOT_V1",
            "protocol_sha256": self.protocol_sha256,
            "recipe_sha256": self.recipe_sha256,
            "source_commit": self.protocol.source_commit,
            "level": self.protocol.level.model_dump(mode="json"),
            "policy_order": list(self.protocol.policy_order),
            "condition_order": list(self.protocol.condition_order),
            "trials": [
                {
                    "trial_id": f"pilot-{index:04d}",
                    "condition": condition,
                    "policy_id": policy,
                    "repeat_index": 0,
                }
                for index, (condition, policy) in enumerate(
                    (condition, policy)
                    for condition in self.protocol.condition_order
                    for policy in self.protocol.policy_order
                )
            ],
            "trial_count": self.trial_count,
            "planned_foreground_requests": self.planned_foreground_requests,
            "planned_background_requests": self.planned_background_requests,
            "lifecycle_prepare_ns_per_trial": self.lifecycle_prepare_ns_per_trial,
            "lifecycle_cleanup_ns_per_trial": self.lifecycle_cleanup_ns_per_trial,
            "trial_execution_worst_case_duration_ns": (
                self.trial_execution_worst_case_duration_ns
            ),
            "execution_worst_case_duration_ns": (self.execution_worst_case_duration_ns),
            "final_cleanup_reserve_ns": self.final_cleanup_reserve_ns,
            "retrieval_reserve_ns": self.retrieval_reserve_ns,
            "worst_case_duration_ns": self.worst_case_duration_ns,
            "reserved_output_bytes": self.reserved_output_bytes,
            "cold_reset_before_every_trial": True,
            "load_selection": "DECLARED_FIXED_LOWER_LOAD_NOT_CALIBRATED",
            "runtime_identity": "UNVERIFIED",
            "provider_action_performed": False,
            "evidence_eligible": False,
        }


def _trial_worst_case_duration_ns(config: TrialConfig) -> int:
    background = config.background if isinstance(config, RoutingFaultConfig) else None
    bounds = config.foreground.bounds
    replay_cleanup_ns = max(
        bounds.cleanup_timeout_ns,
        0 if background is None else background.bounds.cleanup_timeout_ns,
    )
    return (
        bounds.duration_ns
        + bounds.drain_ns
        + 7 * replay_cleanup_ns
        + 4 * config.telemetry.cleanup_timeout_ns
    )


def compile_budget_pilot(
    protocol: BudgetPilotProtocol, recipe: BudgetPilotRecipe
) -> CompiledBudgetPilot:
    """Bind exactly four existing routing configs and calculate all reserves."""

    protocol_sha256 = sha256_digest(
        canonical_json_bytes(protocol.model_dump(mode="json"))
    )
    if (
        recipe.protocol_sha256 != protocol_sha256
        or recipe.level_id != protocol.level.level_id
    ):
        raise EvaluationError("budget pilot recipe does not bind its protocol")
    reference = recipe.foreground
    foreground_requests = 0
    background_requests = 0
    trial_duration = 0
    configs: list[TrialConfig] = []
    for condition in protocol.condition_order:
        for policy in protocol.policy_order:
            config: TrialConfig = (
                HealthyRoutingConfig(
                    schema_version="inferdrome.evaluation-healthy-config.v1",
                    foreground=recipe.foreground,
                    policy_id=policy,
                    telemetry=recipe.telemetry,
                )
                if condition == "HEALTHY"
                else RoutingFaultConfig(
                    schema_version="inferdrome.evaluation-routing-config.v1",
                    foreground=recipe.foreground,
                    background=recipe.background,
                    policy_id=policy,
                    telemetry=recipe.telemetry,
                    fault=recipe.fault,
                )
            )
            configs.append(config)
    for config in configs:
        foreground = config.foreground
        window_ns = recipe.window_end_ns - recipe.window_start_ns
        if (
            foreground != reference
            or config.telemetry != recipe.telemetry
            or foreground.source_commit != protocol.source_commit
            or foreground.model != protocol.workload.model
            or foreground.max_tokens
            != protocol.workload.token_lengths.completion_tokens
            or len(foreground.offers) != protocol.level.planned_foreground_requests
            or protocol.level.planned_foreground_requests * 1_000_000_000_000
            != protocol.level.offered_rate_millirps * window_ns
            or not all(
                recipe.window_start_ns <= offer.scheduled_ns < recipe.window_end_ns
                for offer in foreground.offers
            )
        ):
            raise EvaluationError("budget pilot trial changes the fixed workload")
        background = (
            config.background if isinstance(config, RoutingFaultConfig) else None
        )
        if background is not None and (
            background.source_commit != protocol.source_commit
            or background.model != foreground.model
            or background.max_tokens != foreground.max_tokens
            or background.endpoints != foreground.endpoints
        ):
            raise EvaluationError("budget pilot background changes execution identity")
        foreground_requests += len(foreground.offers)
        background_requests += 0 if background is None else len(background.offers)
        trial_duration += _trial_worst_case_duration_ns(config)

    reservation = direct_process_lifecycle_reservation(
        startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS
    )
    execution_duration = trial_duration + PILOT_TRIAL_COUNT * (
        reservation.required_prepare_timeout_ns
        + reservation.required_cleanup_timeout_ns
    )
    worst_case_duration = (
        execution_duration + FINAL_CLEANUP_RESERVE_NS + RETRIEVAL_RESERVE_NS
    )
    reserved_output = (
        PILOT_TRIAL_COUNT * protocol.per_trial_output_bytes
        + PLAN_MANIFEST_RESERVE_BYTES
        + REPORT_RESERVE_BYTES
        + SIDECAR_RESERVE_BYTES
        + CONTROL_RESERVE_BYTES
    )
    if (
        worst_case_duration != protocol.max_session_duration_ns
        or reserved_output != protocol.max_session_output_bytes
    ):
        raise EvaluationError("budget pilot session bounds are not exact")
    return CompiledBudgetPilot(
        protocol_sha256=protocol_sha256,
        recipe_sha256=sha256_digest(
            canonical_json_bytes(recipe.model_dump(mode="json"))
        ),
        trial_count=PILOT_TRIAL_COUNT,
        planned_foreground_requests=foreground_requests,
        planned_background_requests=background_requests,
        lifecycle_prepare_ns_per_trial=reservation.required_prepare_timeout_ns,
        lifecycle_cleanup_ns_per_trial=reservation.required_cleanup_timeout_ns,
        trial_execution_worst_case_duration_ns=trial_duration,
        execution_worst_case_duration_ns=execution_duration,
        final_cleanup_reserve_ns=FINAL_CLEANUP_RESERVE_NS,
        retrieval_reserve_ns=RETRIEVAL_RESERVE_NS,
        worst_case_duration_ns=worst_case_duration,
        reserved_output_bytes=reserved_output,
        protocol=protocol,
        recipe=recipe,
    )


def _load_strict[ModelT: ClosedModel](content: bytes, model: type[ModelT]) -> ModelT:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if not 1 <= len(content) <= MAX_INPUT_BYTES:
            raise ValueError
        text = content.decode("utf-8")
        validate_json_structure(text)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            parse_float=bounded_json_float,
        )
        return model.model_validate_json(content)
    except (UnicodeError, ValueError, ValidationError, TypeError, RecursionError):
        raise EvaluationError("budget pilot input violates its contract") from None


def load_budget_pilot_protocol_bytes(content: bytes) -> BudgetPilotProtocol:
    return _load_strict(content, BudgetPilotProtocol)


def load_budget_pilot_recipe_bytes(content: bytes) -> BudgetPilotRecipe:
    return _load_strict(content, BudgetPilotRecipe)


def budget_pilot_plan_bytes(value: CompiledBudgetPilot) -> bytes:
    return canonical_json_bytes(value.to_dict()) + b"\n"
