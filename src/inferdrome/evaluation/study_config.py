"""Bounded declarations and deterministic compilation of matched local studies."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    ClosedModel,
    EndpointId,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.fault_config import (
    FaultTiming,
    RoutingFaultConfig,
    TelemetryBounds,
)
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.policies import POLICY_IDS, PolicyId
from inferdrome.parsing import bounded_json_float, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

MAX_BLOCKS = 64
MAX_TRIALS = 256
MAX_PLANNED_REQUESTS = 100_000
MAX_STUDY_DURATION_NS = 86_400_000_000_000
MAX_TOTAL_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_TRIAL_RESULT_BYTES = 64 * 1024 * 1024
PLAN_MANIFEST_RESERVE_BYTES = 1024 * 1024

SafeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Seed = Annotated[int, Field(ge=0, le=2**32 - 1)]
Scenario = Literal["HEALTHY", "STALE_LOAD"]
TrialConfig = HealthyRoutingConfig | RoutingFaultConfig


class StudyProfile(ClosedModel):
    profile_id: SafeId
    load_level: Literal["REHEARSAL", "MODERATE", "NEAR_CAPACITY"] = "REHEARSAL"
    calibration_status: Literal["UNCALIBRATED_REHEARSAL"] = "UNCALIBRATED_REHEARSAL"
    window_start_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]
    window_end_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    first_content_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    completion_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]

    @model_validator(mode="after")
    def ordered_window_and_slos(self) -> Self:
        if (
            self.window_start_ns >= self.window_end_ns
            or self.first_content_slo_ns > self.completion_slo_ns
        ):
            raise ValueError("profile window or latency targets are unordered")
        return self


class StudyPreparation(ClosedModel):
    """Declarations only: the runner cannot prepare or verify server state."""

    cache_state: Literal["UNKNOWN", "DECLARED_COLD", "DECLARED_WARM"]
    prefix_caching: Literal["UNKNOWN", "DECLARED_ENABLED", "DECLARED_DISABLED"]
    model_warmup: Literal["EXTERNALLY_PREPARED_UNMEASURED"] = (
        "EXTERNALLY_PREPARED_UNMEASURED"
    )
    model_warmup_reference: Digest
    serving_image_reference: Digest | None = None
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    cost: Literal["UNAVAILABLE"] = "UNAVAILABLE"


class StudyLimits(ClosedModel):
    max_trials: Annotated[int, Field(ge=4, le=MAX_TRIALS)] = MAX_TRIALS
    max_planned_requests: Annotated[int, Field(ge=4, le=MAX_PLANNED_REQUESTS)] = (
        MAX_PLANNED_REQUESTS
    )
    max_duration_ns: Annotated[int, Field(ge=1, le=MAX_STUDY_DURATION_NS)] = (
        MAX_STUDY_DURATION_NS
    )
    cooldown_ns: Annotated[int, Field(ge=0, le=60_000_000_000)] = 0
    per_trial_result_bytes: Annotated[int, Field(ge=1, le=MAX_TRIAL_RESULT_BYTES)] = (
        2 * 1024 * 1024
    )
    total_output_bytes: Annotated[
        int, Field(ge=PLAN_MANIFEST_RESERVE_BYTES + 4, le=MAX_TOTAL_OUTPUT_BYTES)
    ] = MAX_TOTAL_OUTPUT_BYTES


class StudyReporting(ClosedModel):
    p99_min_successes: Literal[1000] = 1000
    bootstrap_resamples: Literal[2000] = 2000
    confidence_percent: Literal[90] = 90
    minimum_complete_blocks: Literal[8] = 8
    bootstrap_seed: Seed

    @field_validator(
        "p99_min_successes",
        "bootstrap_resamples",
        "confidence_percent",
        "minimum_complete_blocks",
        mode="before",
    )
    @classmethod
    def exact_integer_conventions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("reporting conventions require integer primitives")
        return value


class StudyBlock(ClosedModel):
    block_id: SafeId
    profile_id: SafeId
    scenario: Scenario
    repeat_index: Annotated[int, Field(ge=0, le=MAX_BLOCKS - 1)]
    workload_seed: Seed
    order_seed: Seed
    policy_order: Annotated[tuple[PolicyId, ...], Field(min_length=4, max_length=4)]
    foreground: EvaluationConfig = Field(repr=False)
    telemetry: TelemetryBounds
    background: EvaluationConfig | None = Field(default=None, repr=False)
    fault: FaultTiming | None = None

    @model_validator(mode="after")
    def one_matched_recipe(self) -> Self:
        if set(self.policy_order) != set(POLICY_IDS):
            raise ValueError("a block must order all four policies exactly once")
        self.trial_config(self.policy_order[0])
        return self

    def trial_config(self, policy_id: PolicyId) -> TrialConfig:
        if self.scenario == "HEALTHY":
            if self.background is not None or self.fault is not None:
                raise ValueError("healthy blocks cannot declare a background or fault")
            return HealthyRoutingConfig(
                schema_version="inferdrome.evaluation-healthy-config.v1",
                foreground=self.foreground,
                policy_id=policy_id,
                telemetry=self.telemetry,
            )
        if self.background is None or self.fault is None:
            raise ValueError("stale-load blocks require a background and fault")
        if self.background.max_tokens != self.foreground.max_tokens:
            raise ValueError("study populations must share generation settings")
        return RoutingFaultConfig(
            schema_version="inferdrome.evaluation-routing-config.v1",
            foreground=self.foreground,
            background=self.background,
            policy_id=policy_id,
            telemetry=self.telemetry,
            fault=self.fault,
        )

    @property
    def target_endpoint_id(self) -> EndpointId | None:
        return None if self.fault is None else self.fault.target_endpoint_id

    @property
    def planned_request_count(self) -> int:
        return len(self.foreground.offers) + (
            0 if self.background is None else len(self.background.offers)
        )

    @property
    def worst_case_duration_ns(self) -> int:
        bounds = self.foreground.bounds
        replay_cleanup_ns = max(
            bounds.cleanup_timeout_ns,
            0 if self.background is None else self.background.bounds.cleanup_timeout_ns,
        )
        # Replay cleanup can precede owner cleanup. The owner separately permits
        # worker join 2*T, population join 3*R + cancellation R, probe close 2*T.
        return (
            bounds.duration_ns
            + bounds.drain_ns
            + 7 * replay_cleanup_ns
            + 4 * self.telemetry.cleanup_timeout_ns
        )


class StudyConfig(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-study-config.v1"]
    profiles: Annotated[tuple[StudyProfile, ...], Field(min_length=1, max_length=16)]
    blocks: Annotated[
        tuple[StudyBlock, ...], Field(min_length=1, max_length=MAX_BLOCKS)
    ]
    preparation: StudyPreparation
    limits: StudyLimits
    reporting: StudyReporting

    @model_validator(mode="after")
    def matched_bounded_study(self) -> Self:
        profiles = {profile.profile_id: profile for profile in self.profiles}
        if len(profiles) != len(self.profiles):
            raise ValueError("profile identities must be unique")
        if set(profiles) != {block.profile_id for block in self.blocks}:
            raise ValueError("study profiles must match the used block profiles")
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("block identities must be unique")
        baseline = self.blocks[0].foreground
        repetitions: dict[tuple[str, Scenario, EndpointId | None], list[int]] = (
            defaultdict(list)
        )
        positions: dict[tuple[str, Scenario], list[Counter[PolicyId]]] = defaultdict(
            lambda: [Counter() for _ in POLICY_IDS]
        )
        previous_targets: dict[str, EndpointId] = {}
        for block in self.blocks:
            profile = profiles[block.profile_id]
            foreground = block.foreground
            if (
                foreground.endpoints != baseline.endpoints
                or foreground.model != baseline.model
                or foreground.source_commit != baseline.source_commit
                or foreground.max_tokens != baseline.max_tokens
            ):
                raise ValueError("study blocks must share serving and request settings")
            if not (
                block.telemetry.warmup_ns
                <= profile.window_start_ns
                < profile.window_end_ns
                == foreground.bounds.duration_ns
                and profile.completion_slo_ns < foreground.bounds.request_timeout_ns
                and profile.completion_slo_ns <= foreground.bounds.drain_ns
            ) or any(
                not profile.window_start_ns
                <= offer.scheduled_ns
                < profile.window_end_ns
                for offer in foreground.offers
            ):
                raise ValueError("foreground violates the declared window or SLOs")
            key = (block.profile_id, block.scenario, block.target_endpoint_id)
            repetitions[key].append(block.repeat_index)
            for position, policy in enumerate(block.policy_order):
                positions[(block.profile_id, block.scenario)][position][policy] += 1
            target = block.target_endpoint_id
            if target is not None:
                if previous_targets.get(block.profile_id) == target:
                    raise ValueError("fault targets must alternate within each profile")
                previous_targets[block.profile_id] = target
        if any(
            indices != list(range(len(indices))) for indices in repetitions.values()
        ):
            raise ValueError("repeat indices must be contiguous in each target stratum")
        for position_counts in positions.values():
            for counts in position_counts:
                values = [counts[policy] for policy in POLICY_IDS]
                if max(values) - min(values) > 1:
                    raise ValueError(
                        "policy positions must balance within each stratum"
                    )
        trials = 4 * len(self.blocks)
        requests = 4 * sum(block.planned_request_count for block in self.blocks)
        duration = 4 * sum(block.worst_case_duration_ns for block in self.blocks)
        duration += (trials - 1) * self.limits.cooldown_ns
        output = (
            trials * self.limits.per_trial_result_bytes + PLAN_MANIFEST_RESERVE_BYTES
        )
        if (
            trials > self.limits.max_trials
            or requests > self.limits.max_planned_requests
            or duration > self.limits.max_duration_ns
            or output > self.limits.total_output_bytes
        ):
            raise ValueError("compiled campaign exceeds a declared resource bound")
        return self


@dataclass(frozen=True)
class CompiledTrial:
    trial_id: str
    block_id: str
    profile_id: str
    scenario: Scenario
    target_endpoint_id: EndpointId | None
    repeat_index: int
    workload_seed: int
    order_seed: int
    policy_id: PolicyId
    config: TrialConfig = field(repr=False)
    config_sha256: str
    workload_sha256: str
    window_start_ns: int
    window_end_ns: int
    first_content_slo_ns: int
    completion_slo_ns: int
    cooldown_ns: int
    worst_case_duration_ns: int

    def to_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "block_id": self.block_id,
            "profile_id": self.profile_id,
            "scenario": self.scenario,
            "target_endpoint_id": self.target_endpoint_id,
            "repeat_index": self.repeat_index,
            "workload_seed": self.workload_seed,
            "order_seed": self.order_seed,
            "policy_id": self.policy_id,
            "config_sha256": self.config_sha256,
            "workload_sha256": self.workload_sha256,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "first_content_slo_ns": self.first_content_slo_ns,
            "completion_slo_ns": self.completion_slo_ns,
            "cooldown_ns": self.cooldown_ns,
            "worst_case_duration_ns": self.worst_case_duration_ns,
            "foreground_offered_count": len(self.config.foreground.offers),
            "background_offered_count": (
                len(self.config.background.offers)
                if isinstance(self.config, RoutingFaultConfig)
                else 0
            ),
        }


@dataclass(frozen=True)
class CompiledStudy:
    config_sha256: str
    trials: tuple[CompiledTrial, ...]
    planned_request_count: int
    worst_case_duration_ns: int
    reserved_result_bytes: int
    config: StudyConfig = field(repr=False)

    @property
    def reporting(self) -> StudyReporting:
        return self.config.reporting

    def to_dict(self) -> dict[str, object]:
        foreground = self.config.blocks[0].foreground
        return {
            "schema_version": "inferdrome.evaluation-study-plan.v1",
            "compiler": "FINITE_MATCHED_REPLAY_V1",
            "config_sha256": self.config_sha256,
            "trials": [trial.to_dict() for trial in self.trials],
            "planned_request_count": self.planned_request_count,
            "worst_case_duration_ns": self.worst_case_duration_ns,
            "reserved_result_bytes": self.reserved_result_bytes,
            "plan_manifest_reserve_bytes": PLAN_MANIFEST_RESERVE_BYTES,
            "limits": self.config.limits.model_dump(mode="json"),
            "reporting": self.config.reporting.model_dump(mode="json"),
            "profiles": [
                profile.model_dump(mode="json") for profile in self.config.profiles
            ],
            "preparation": self.config.preparation.model_dump(mode="json"),
            "declared_source_commit": foreground.source_commit,
            "model_sha256": sha256_digest(foreground.model.encode("utf-8")),
            "model_warmup_requests": "EXTERNAL_UNMEASURED",
            "model_warmup_duration_ns": "EXTERNAL_UNMEASURED",
            "model_warmup_tokens": "EXTERNAL_UNMEASURED",
            "planned_request_scope": "FOREGROUND_AND_BACKGROUND_REPLAY_ONLY",
            "seed_semantics": "DECLARED_FINITE_WORKLOAD_AND_ORDER_NOT_GENERATION_SEED",
            "evidence_class": "LOCAL_MEASUREMENT_ONLY",
            "evidence_eligible": False,
            "endpoint_identity": "UNVERIFIED",
            "source_identity": "UNVERIFIED",
            "calibration": "UNCALIBRATED_REHEARSAL",
            "cost": "UNAVAILABLE",
        }


def compile_study(config: StudyConfig) -> CompiledStudy:
    """Freeze four policy trials from each supplied finite recipe without I/O."""
    profiles = {profile.profile_id: profile for profile in config.profiles}
    trials: list[CompiledTrial] = []
    for block in config.blocks:
        profile = profiles[block.profile_id]
        # Exclude policy order and endpoint origins from the matched payload.
        # Keep fixed endpoint IDs, preparation, seeds, limits and every offer byte.
        payload = block.model_dump(mode="json", exclude={"policy_order"})
        for population in ("foreground", "background"):
            if payload[population] is not None:
                payload[population]["endpoints"] = [
                    {"endpoint_id": endpoint.endpoint_id}
                    for endpoint in block.foreground.endpoints
                ]
        payload["profile"] = profile.model_dump(mode="json")
        payload["preparation"] = config.preparation.model_dump(mode="json")
        workload_sha256 = sha256_digest(canonical_json_bytes(payload))
        for policy in block.policy_order:
            trial_config = block.trial_config(policy)
            trials.append(
                CompiledTrial(
                    trial_id=f"trial-{len(trials):04d}",
                    block_id=block.block_id,
                    profile_id=block.profile_id,
                    scenario=block.scenario,
                    target_endpoint_id=block.target_endpoint_id,
                    repeat_index=block.repeat_index,
                    workload_seed=block.workload_seed,
                    order_seed=block.order_seed,
                    policy_id=policy,
                    config=trial_config,
                    config_sha256=sha256_digest(
                        canonical_json_bytes(trial_config.model_dump(mode="json"))
                    ),
                    workload_sha256=workload_sha256,
                    window_start_ns=profile.window_start_ns,
                    window_end_ns=profile.window_end_ns,
                    first_content_slo_ns=profile.first_content_slo_ns,
                    completion_slo_ns=profile.completion_slo_ns,
                    cooldown_ns=config.limits.cooldown_ns,
                    worst_case_duration_ns=block.worst_case_duration_ns,
                )
            )
    return CompiledStudy(
        config_sha256=sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
        trials=tuple(trials),
        planned_request_count=4
        * sum(block.planned_request_count for block in config.blocks),
        worst_case_duration_ns=(
            sum(trial.worst_case_duration_ns for trial in trials)
            + (len(trials) - 1) * config.limits.cooldown_ns
        ),
        reserved_result_bytes=(
            len(trials) * config.limits.per_trial_result_bytes
            + PLAN_MANIFEST_RESERVE_BYTES
        ),
        config=config,
    )


def load_study_config_bytes(content: bytes) -> StudyConfig:
    """Load a closed, bounded study; all invalid private inputs get one error."""

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
        return StudyConfig.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError("study configuration violates its contract") from None
