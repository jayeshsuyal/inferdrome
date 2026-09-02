"""Strict contracts for the synthetic routing-campaign-v1 evidence package."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CampaignId = Literal["routing-campaign-v1"]
PolicyId = Literal[
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
]
EndpointId = Literal["endpoint-a", "endpoint-b"]
TerminalStatus = Literal[
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
]
Admissibility = Literal["ADMISSIBLE", "INADMISSIBLE"]
FallbackReason = Literal[
    "NONE",
    "REQUIRED_LOAD_STALE",
    "STALE_LOAD_FAIL_OPEN",
    "HEALTH_ONLY_TIE_BREAK",
]


class RoutingModel(BaseModel):
    """Immutable, strict, routing-local model base."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EndpointPlan(RoutingModel):
    endpoint_id: EndpointId


class VirtualTiming(RoutingModel):
    request_times_ms: tuple[int, ...]

    @model_validator(mode="after")
    def validate_fixed_trace(self) -> VirtualTiming:
        if self.request_times_ms != (0, 10, 20, 30, 40, 50):
            raise ValueError("routing campaign requires the fixed six-request trace")
        return self


class HealthObserverPlan(RoutingModel):
    observer_id: Literal["health-observer-v1"]
    interval_ms: Literal[5]
    freshness_bound_ms: Literal[5]


class LoadObserverPlan(RoutingModel):
    observer_id: Literal["load-observer-v1"]
    update_times_ms: tuple[int, ...]
    freshness_bound_ms: Literal[5]
    initial_values: dict[EndpointId, int]

    @model_validator(mode="after")
    def validate_initial_values(self) -> LoadObserverPlan:
        if self.update_times_ms != (0, 10):
            raise ValueError("routing campaign requires load updates at 0 and 10 ms")
        if self.initial_values != {"endpoint-a": 4, "endpoint-b": 1}:
            raise ValueError("routing campaign requires the fixed initial load values")
        return self


class KvObserverPlan(RoutingModel):
    observer_id: Literal["kv-observer-v1"]
    freshness_bound_ms: Literal[5]


class EndpointBehavior(RoutingModel):
    endpoint_b_saturated_at_ms: Literal[20]


class RoutingCampaignPlan(RoutingModel):
    schema_version: Literal["inferdrome.routing-campaign-plan.v1"]
    campaign_id: CampaignId
    execution_mode: Literal["SYNTHETIC_CPU_ONLY"]
    endpoints: tuple[EndpointPlan, ...]
    virtual_timing: VirtualTiming
    health_observer: HealthObserverPlan
    load_observer: LoadObserverPlan
    kv_observer: KvObserverPlan
    endpoint_behavior: EndpointBehavior
    retry_policy: Literal["NO_RETRY"]
    policies: tuple[PolicyId, ...]

    @model_validator(mode="after")
    def validate_fixed_campaign(self) -> RoutingCampaignPlan:
        if tuple(endpoint.endpoint_id for endpoint in self.endpoints) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError(
                "routing campaign requires ordered endpoint-a and endpoint-b"
            )
        if self.policies != (
            "fail_closed_required_load_v1",
            "explicit_fail_open_stale_load_v1",
            "typed_admissible_state_only_v1",
        ):
            raise ValueError("routing campaign requires the three fixed policies")
        return self


class RequestTraceRecord(RoutingModel):
    request_id: Annotated[str, Field(pattern=r"^request-[0-9]{3}$")]
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    decision_time_ms: Annotated[int, Field(ge=0, le=50)]


class FaultSchedule(RoutingModel):
    schema_version: Literal["inferdrome.routing-fault-schedule.v1"]
    campaign_id: CampaignId
    load_observer_pause_at_ms: Literal[15]
    health_continues: Literal[True]


class TrialDefinition(RoutingModel):
    trial_id: Annotated[str, Field(pattern=r"^trial-[a-z-]+-v1$")]
    policy_id: PolicyId


class TrialPlan(RoutingModel):
    schema_version: Literal["inferdrome.routing-trial-plan.v1"]
    campaign_id: CampaignId
    trials: tuple[TrialDefinition, ...]

    @model_validator(mode="after")
    def validate_one_cold_trial_per_policy(self) -> TrialPlan:
        expected = (
            ("trial-fail-closed-v1", "fail_closed_required_load_v1"),
            ("trial-fail-open-v1", "explicit_fail_open_stale_load_v1"),
            ("trial-typed-v1", "typed_admissible_state_only_v1"),
        )
        actual = tuple((trial.trial_id, trial.policy_id) for trial in self.trials)
        if actual != expected:
            raise ValueError(
                "routing campaign requires one fixed cold trial per policy"
            )
        return self


class ResetReceipt(RoutingModel):
    schema_version: Literal["inferdrome.routing-reset-receipt.v1"]
    campaign_id: CampaignId
    trial_id: str
    policy_id: PolicyId
    virtual_time_ms: Literal[0]
    endpoint_instance_ids: dict[EndpointId, str]
    observer_epochs: dict[str, int]
    queue_cleared: Literal[True]
    load_state_cleared: Literal[True]
    kv_state_cleared: Literal[True]


class Observation(RoutingModel):
    signal: Literal["HEALTH", "LOAD", "KV"]
    observer_id: str
    endpoint_id: EndpointId
    epoch: Annotated[int, Field(ge=1)]
    observed_at_ms: Annotated[int, Field(ge=0)]
    decision_time_ms: Annotated[int, Field(ge=0)]
    age_ms: Annotated[int, Field(ge=0)]
    freshness_bound_ms: Annotated[int, Field(ge=0)]
    value: str | int
    admissibility: Admissibility

    @model_validator(mode="after")
    def validate_age_and_admissibility(self) -> Observation:
        if self.age_ms != self.decision_time_ms - self.observed_at_ms:
            raise ValueError("observation age disagrees with its clock values")
        expected = (
            "ADMISSIBLE" if self.age_ms <= self.freshness_bound_ms else "INADMISSIBLE"
        )
        if self.admissibility != expected:
            raise ValueError("observation admissibility disagrees with freshness")
        return self


class StateObservationRecord(RoutingModel):
    schema_version: Literal["inferdrome.routing-state-observation.v1"]
    campaign_id: CampaignId
    trial_id: str
    request_id: str
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    observation: Observation


class CandidateState(RoutingModel):
    endpoint_id: EndpointId
    health: Observation
    load: Observation
    kv: Observation
    eligible: bool

    @model_validator(mode="after")
    def validate_candidate_binding(self) -> CandidateState:
        if any(
            observation.endpoint_id != self.endpoint_id
            for observation in (self.health, self.load, self.kv)
        ):
            raise ValueError(
                "candidate observations must name their candidate endpoint"
            )
        return self


class RouteDecisionReceipt(RoutingModel):
    schema_version: Literal["inferdrome.route-decision-receipt.v1"]
    campaign_id: CampaignId
    trial_id: str
    policy_id: PolicyId
    request_id: str
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    decision_id: Annotated[str, Field(pattern=r"^decision-[a-z0-9-]+$")]
    decision_time_ms: Annotated[int, Field(ge=0, le=50)]
    candidates: tuple[CandidateState, ...]
    selected_endpoint_id: EndpointId | None
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
    fallback_reason: FallbackReason
    terminal_outcome_id: Annotated[str, Field(pattern=r"^terminal-[a-z0-9-]+$")]

    @model_validator(mode="after")
    def validate_decision_shape(self) -> RouteDecisionReceipt:
        if tuple(candidate.endpoint_id for candidate in self.candidates) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("route decision candidates must preserve endpoint order")
        if (
            self.fallback_reason == "REQUIRED_LOAD_STALE"
            and self.selected_endpoint_id is not None
        ):
            raise ValueError("fail-closed stale-load decisions must be null selections")
        if (
            self.fallback_reason != "REQUIRED_LOAD_STALE"
            and self.selected_endpoint_id is None
        ):
            raise ValueError("non-fail-closed decisions must select one endpoint")
        return self


class TerminalOutcomeReceipt(RoutingModel):
    schema_version: Literal["inferdrome.terminal-outcome-receipt.v1"]
    campaign_id: CampaignId
    trial_id: str
    request_id: str
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    terminal_outcome_id: Annotated[str, Field(pattern=r"^terminal-[a-z0-9-]+$")]
    decision_id: Annotated[str, Field(pattern=r"^decision-[a-z0-9-]+$")]
    selected_endpoint_id: EndpointId | None
    status: TerminalStatus
    started_at_ms: Annotated[int, Field(ge=0, le=50)]
    ended_at_ms: Annotated[int, Field(ge=0, le=60)]
    reason: str

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> TerminalOutcomeReceipt:
        if self.ended_at_ms < self.started_at_ms:
            raise ValueError("terminal outcome cannot end before it starts")
        if self.status == "NO_SAFE_ROUTE" and self.selected_endpoint_id is not None:
            raise ValueError("no-safe-route terminal must have a null endpoint")
        if self.status != "NO_SAFE_ROUTE" and self.selected_endpoint_id is None:
            raise ValueError("dispatched terminal must name its endpoint")
        return self


class TrialSummary(RoutingModel):
    schema_version: Literal["inferdrome.routing-trial-summary.v1"]
    campaign_id: CampaignId
    trial_id: str
    policy_id: PolicyId
    planned_request_count: Literal[6]
    terminal_population: dict[TerminalStatus, int]


class CampaignSummary(RoutingModel):
    schema_version: Literal["inferdrome.routing-campaign-summary.v1"]
    campaign_id: CampaignId
    trial_summaries: tuple[TrialSummary, ...]


class ArtifactHashEntry(RoutingModel):
    path: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")]
    sha256: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=0)]


class IntegrityManifest(RoutingModel):
    schema_version: Literal["inferdrome.routing-integrity-manifest.v1"]
    campaign_id: CampaignId
    hash_algorithm: Literal["sha256"]
    path_ordering: Literal["normalized_posix_ascending_v1"]
    entries: tuple[ArtifactHashEntry, ...]

    @model_validator(mode="after")
    def validate_ordered_unique_entries(self) -> IntegrityManifest:
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("integrity entries must have unique ascending paths")
        return self
