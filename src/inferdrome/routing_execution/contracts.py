"""Strict additive contracts for the PR-B real routing-execution bridge.

The source configuration contains endpoint origins and workload input only long
enough to perform a bounded run.  Published models intentionally contain only
logical endpoint identifiers and digests; this package is evidence, never a
network-control or verdict contract.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from inferdrome.qwen3_campaign import qwen3_workload_prompts, qwen3_workload_sha256
from inferdrome.routing_execution.canonical import canonical_jsonl_bytes, sha256_digest

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
ExecutionId = Literal["routing-execution-v1"]
EndpointId = Literal["endpoint-a", "endpoint-b"]
PolicyId = Literal[
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
]
TerminalStatus = Literal[
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
]
Admissibility = Literal["ADMISSIBLE", "INADMISSIBLE"]
Signal = Literal["HEALTH", "LOAD", "GPU_DCGM", "KV_CACHE"]
SignalState = Literal["AVAILABLE", "UNAVAILABLE", "STALE"]
FallbackReason = Literal[
    "NONE",
    "REQUIRED_LOAD_STALE",
    "REQUIRED_LOAD_UNAVAILABLE",
    "STALE_LOAD_FAIL_OPEN",
    "HEALTH_ONLY_TIE_BREAK",
    "HEALTH_NOT_ADMISSIBLE",
]

_POLICIES: tuple[PolicyId, ...] = (
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
)
_REQUEST_IDS = tuple(f"request-{index:03d}" for index in range(6))
_R1_INPUT_DIGESTS = {
    "plan": "sha256:f088a28730ee4e2ba9265f4d4309259c2c4098f267858b2b6d8ddc9b89df460a",
    "trace": "sha256:5697ccf98ea1ebb125e7754e93443c16e903a99ba0cd41fb796efb2a6b53438f",
    "fault": "sha256:1eacfd1444323022c48e649cbc8856c4513ceebac4dd337f49e3765a3d3a311a",
    "trial": "sha256:e622a919d37c7ba5ab2deb88710cf3f2f978ea250c37d01cb729b22135d104a9",
}


def fixed_selected_workload_bytes() -> bytes:
    """Return the exact first six fixed Qwen3 workload rows used by PR B."""

    return canonical_jsonl_bytes(
        {"prompt": prompt} for prompt in qwen3_workload_prompts()[:6]
    )


def fixed_selected_workload_sha256() -> str:
    """Return the selected six-row workload identity bound by this bridge."""

    return sha256_digest(fixed_selected_workload_bytes())


class ExecutionModel(BaseModel):
    """Strict, immutable models whose validation errors do not echo inputs."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ImageIdentity(ExecutionModel):
    """An immutable OCI repository digest, never a mutable tag."""

    reference: Annotated[
        str,
        Field(
            pattern=r"^[a-z0-9][a-z0-9._/-]{0,220}@sha256:[0-9a-f]{64}$",
            max_length=300,
        ),
    ]


def oci_content_digest(image: ImageIdentity) -> str:
    """Return the immutable SHA-256 content digest from a pinned OCI reference.

    Repository names are transport locations, not a role-separation boundary:
    two different repositories can point at the same manifest/content digest.
    Callers that need distinct OCI roles must compare this value rather than the
    complete ``repository@sha256:...`` reference.
    """

    return image.reference.rsplit("@", maxsplit=1)[1]


class ModelIdentity(ExecutionModel):
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]
    tokenizer_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]


class RuntimeIdentity(ExecutionModel):
    runtime_name: Literal["vllm"]
    runtime_version: Literal["0.26.0"]
    adapter_id: Literal["openai-compatible-routing-execution-v1"]
    adapter_version: Literal["1.0.0"]


class RoutingInputIdentity(ExecutionModel):
    """Digest bindings to the frozen R1 experiment inputs, not their bytes."""

    campaign_id: Literal["routing-campaign-v1"]
    plan_sha256: Digest
    trace_sha256: Digest
    fault_schedule_sha256: Digest
    trial_plan_sha256: Digest
    policies: tuple[PolicyId, ...]

    @model_validator(mode="after")
    def _fixed_policy_order(self) -> RoutingInputIdentity:
        if self.policies != _POLICIES:
            raise ValueError("routing policy inventory is not the fixed R1 order")
        if (
            self.plan_sha256 != _R1_INPUT_DIGESTS["plan"]
            or self.trace_sha256 != _R1_INPUT_DIGESTS["trace"]
            or self.fault_schedule_sha256 != _R1_INPUT_DIGESTS["fault"]
            or self.trial_plan_sha256 != _R1_INPUT_DIGESTS["trial"]
        ):
            raise ValueError("routing input identities are not the frozen R1 bytes")
        return self


class WorkloadIdentity(ExecutionModel):
    """A six-request bounded selection from the fixed Qwen3 workload."""

    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    workload_sha256: Digest
    selected_workload_sha256: Digest
    selected_request_ids: tuple[SafeId, ...]
    request_denominator: Literal[6]

    @model_validator(mode="after")
    def _fixed_selection(self) -> WorkloadIdentity:
        if self.selected_request_ids != _REQUEST_IDS:
            raise ValueError("workload selection is not the fixed six-request trace")
        if self.workload_sha256 != qwen3_workload_sha256():
            raise ValueError("workload identity is not the fixed Qwen3 workload")
        if self.selected_workload_sha256 != fixed_selected_workload_sha256():
            raise ValueError("workload selection is not the fixed Qwen3 trace")
        return self


class EndpointCapabilities(ExecutionModel):
    """Capabilities declared by a separately operated serving endpoint."""

    health: Literal["HTTP_HEALTH_V1"]
    load: Literal["VLLM_PROMETHEUS_V1"]
    gpu_dcgm: Literal["UNAVAILABLE"]
    kv_cache: Literal["UNAVAILABLE"]


class EndpointDeclaration(ExecutionModel):
    """Input-only endpoint facts; ``origin`` is never serialized to evidence."""

    endpoint_id: EndpointId
    origin: Annotated[str, Field(min_length=1, max_length=2048)]
    model: ModelIdentity
    runtime: RuntimeIdentity
    serving_image: ImageIdentity
    workload_sha256: Digest
    capabilities: EndpointCapabilities


class TelemetryPlan(ExecutionModel):
    clock_domain: Literal["RUNNER_MONOTONIC_NS"]
    health_freshness_ms: Literal[5]
    load_freshness_ms: Literal[5]
    gpu_freshness_ms: Literal[5]
    load_metric_name: Literal["vllm:num_requests_running"]


class FaultPlan(ExecutionModel):
    fault_id: Literal["stale-load-fresh-health-v1"]
    load_collection_pause_after_sequence_index: Literal[1]
    health_collection_continues: Literal[True]
    inter_request_interval_ms: Literal[10]


class TopologyFacts(ExecutionModel):
    """Admitted facts only; this contract does not create or control a provider."""

    runner_separate_from_serving: Literal[True]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    accelerator_model: Literal["NONE_LOCAL", "NVIDIA A100-SXM4-40GB"]
    accelerator_count: Annotated[int, Field(ge=0, le=16)]


class EvidenceDestination(ExecutionModel):
    """Opaque destination identity; a local path is supplied only at invocation."""

    destination_sha256: Digest
    declared_input_transfer_sha256: Digest
    publication_mode: Literal["LOCAL_CREATE_NO_REPLACE_V1"]


class RoutingExecutionConfig(ExecutionModel):
    """Versioned source contract for a bounded two-endpoint execution."""

    schema_version: Literal["inferdrome.routing-execution-config.v1"]
    execution_id: ExecutionId
    mode: Literal["LOCAL_LOOPBACK", "GCP_PRIVATE"]
    source_commit: Commit
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[EndpointDeclaration, ...]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: TopologyFacts
    evidence_destination: EvidenceDestination
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _closed_config(self) -> RoutingExecutionConfig:
        if tuple(endpoint.endpoint_id for endpoint in self.endpoints) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("execution needs ordered endpoint-a and endpoint-b")
        for endpoint in self.endpoints:
            if endpoint.model != self.model or endpoint.runtime != self.runtime:
                raise ValueError("endpoint model or runtime compatibility disagrees")
            if endpoint.serving_image != self.serving_image:
                raise ValueError("endpoint immutable serving identity disagrees")
            if endpoint.workload_sha256 != self.workload.workload_sha256:
                raise ValueError("endpoint workload compatibility disagrees")
        if self.mode == "LOCAL_LOOPBACK" and (
            self.topology.accelerator_model != "NONE_LOCAL"
            or self.topology.accelerator_count != 0
        ):
            raise ValueError("local mode cannot declare an accelerator topology")
        if self.mode == "GCP_PRIVATE" and self.topology.accelerator_count < 1:
            raise ValueError("GCP mode requires a declared accelerator topology")
        return self


class InputTransferReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-input-transfer-receipt.v1"]
    config_sha256: Digest
    selected_workload_sha256: Digest
    workload_size_bytes: Annotated[int, Field(ge=1, le=1_048_576)]
    declared_input_transfer_sha256: Digest
    verified_before_transport: Literal[True]


class PublishedEndpointIdentity(ExecutionModel):
    endpoint_id: EndpointId
    origin_sha256: Digest
    capability_identity_sha256: Digest


class ExecutedManifest(ExecutionModel):
    schema_version: Literal["inferdrome.routing-executed-manifest.v1"]
    execution_id: ExecutionId
    mode: Literal["LOCAL_LOOPBACK", "GCP_PRIVATE"]
    source_commit: Commit
    config_sha256: Digest
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[PublishedEndpointIdentity, ...]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: TopologyFacts
    evidence_destination_sha256: Digest
    input_transfer_receipt_sha256: Digest
    planned_requests_per_trial: Literal[6]
    planned_terminal_denominator: Literal[18]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _ordered_endpoints(self) -> ExecutedManifest:
        if tuple(endpoint.endpoint_id for endpoint in self.endpoints) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("executed manifest endpoint order is invalid")
        if self.mode == "LOCAL_LOOPBACK" and (
            self.topology.accelerator_model != "NONE_LOCAL"
            or self.topology.accelerator_count != 0
        ):
            raise ValueError("local executed manifest accelerator facts are invalid")
        if self.mode == "GCP_PRIVATE" and (
            self.topology.accelerator_model != "NVIDIA A100-SXM4-40GB"
            or self.topology.accelerator_count < 2
        ):
            raise ValueError("GCP executed manifest topology facts are unsupported")
        return self


class ResetReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-reset-receipt.v1"]
    trial_id: SafeId
    policy_id: PolicyId
    reset_at_monotonic_ns: Annotated[int, Field(ge=0)]
    observer_epochs: dict[Signal, Annotated[int, Field(ge=0)]]
    runner_connection_state_cleared: Literal[True]
    runner_telemetry_state_cleared: Literal[True]
    endpoint_runtime_identities: tuple[PublishedEndpointIdentity, ...]
    endpoint_engine_reset_assertion: Literal["NOT_ASSERTED_SEPARATE_SERVING_ENGINE"]


class FaultReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-fault-receipt.v1"]
    trial_id: SafeId
    fault_id: Literal["stale-load-fresh-health-v1"]
    activated_at_sequence_index: Literal[2]
    activated_at_monotonic_ns: Annotated[int, Field(ge=0)]
    load_collection_paused: Literal[True]
    health_collection_continues: Literal[True]


class TelemetryObservation(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-telemetry-observation.v1"]
    trial_id: SafeId
    request_id: SafeId
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    endpoint_id: EndpointId
    signal: Signal
    observer_id: SafeId
    epoch: Annotated[int, Field(ge=0)]
    sampled_at_monotonic_ns: Annotated[int, Field(ge=0)]
    decision_at_monotonic_ns: Annotated[int, Field(ge=0)]
    age_ns: Annotated[int, Field(ge=0)]
    freshness_bound_ns: Annotated[int, Field(ge=0)]
    state: SignalState
    admissibility: Admissibility
    value: int | Literal["HEALTHY", "UNAVAILABLE"]
    source: Literal[
        "HTTP_HEALTH",
        "VLLM_METRICS",
        "UNAVAILABLE_CAPABILITY",
    ]
    payload_sha256: Digest | None

    @model_validator(mode="after")
    def _time_and_state_consistent(self) -> TelemetryObservation:
        if self.decision_at_monotonic_ns < self.sampled_at_monotonic_ns:
            raise ValueError("telemetry decision precedes sampling")
        if self.age_ns != (
            self.decision_at_monotonic_ns - self.sampled_at_monotonic_ns
        ):
            raise ValueError("telemetry age is inconsistent")
        fresh = self.state == "AVAILABLE" and self.age_ns <= self.freshness_bound_ns
        if self.admissibility != ("ADMISSIBLE" if fresh else "INADMISSIBLE"):
            raise ValueError("telemetry admissibility is inconsistent")
        if self.state == "AVAILABLE" and self.age_ns > self.freshness_bound_ns:
            raise ValueError("available telemetry age is inconsistent")
        if self.state == "STALE" and self.age_ns <= self.freshness_bound_ns:
            raise ValueError("stale telemetry age is inconsistent")
        if self.state == "UNAVAILABLE" and self.value != "UNAVAILABLE":
            raise ValueError("unavailable telemetry cannot invent a value")
        if self.signal == "HEALTH":
            if self.source != "HTTP_HEALTH":
                raise ValueError("health telemetry source is invalid")
            if self.state != "UNAVAILABLE" and self.value != "HEALTHY":
                raise ValueError("health telemetry value is invalid")
        elif self.signal == "LOAD":
            if self.source != "VLLM_METRICS":
                raise ValueError("load telemetry source is invalid")
            if self.state != "UNAVAILABLE" and (
                type(self.value) is not int or self.value < 0
            ):
                raise ValueError("load telemetry must be a non-negative integer")
        elif (
            self.source != "UNAVAILABLE_CAPABILITY"
            or self.state != "UNAVAILABLE"
            or self.value != "UNAVAILABLE"
        ):
            raise ValueError("unsupported telemetry capability is invalid")
        return self


class CandidateState(ExecutionModel):
    endpoint_id: EndpointId
    health: TelemetryObservation
    load: TelemetryObservation
    gpu_dcgm: TelemetryObservation
    kv_cache: TelemetryObservation
    eligible: bool

    @model_validator(mode="after")
    def _bound_observations(self) -> CandidateState:
        observations = (self.health, self.load, self.gpu_dcgm, self.kv_cache)
        if any(row.endpoint_id != self.endpoint_id for row in observations):
            raise ValueError("candidate telemetry endpoint binding is invalid")
        if tuple(row.signal for row in observations) != (
            "HEALTH",
            "LOAD",
            "GPU_DCGM",
            "KV_CACHE",
        ):
            raise ValueError("candidate telemetry inventory is invalid")
        return self


class RouteDecisionReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-decision-receipt.v1"]
    trial_id: SafeId
    policy_id: PolicyId
    request_id: SafeId
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    decision_id: SafeId
    decision_at_monotonic_ns: Annotated[int, Field(ge=0)]
    candidates: tuple[CandidateState, ...]
    selected_endpoint_id: EndpointId | None
    claims_used: tuple[SafeId, ...]
    claims_permitted_stale: tuple[SafeId, ...]
    claims_discarded: tuple[SafeId, ...]
    fallback_reason: FallbackReason
    terminal_outcome_id: SafeId

    @model_validator(mode="after")
    def _decision_consistent(self) -> RouteDecisionReceipt:
        if tuple(candidate.endpoint_id for candidate in self.candidates) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("decision candidate order is invalid")
        if self.selected_endpoint_id is None and self.fallback_reason == "NONE":
            raise ValueError("a normal decision must select an endpoint")
        if (
            self.fallback_reason in {"REQUIRED_LOAD_STALE", "REQUIRED_LOAD_UNAVAILABLE"}
            and self.selected_endpoint_id is not None
        ):
            raise ValueError("required-load fail closed cannot select an endpoint")
        return self


class TerminalOutcomeReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-terminal-receipt.v1"]
    trial_id: SafeId
    request_id: SafeId
    sequence_index: Annotated[int, Field(ge=0, le=5)]
    terminal_outcome_id: SafeId
    decision_id: SafeId
    selected_endpoint_id: EndpointId | None
    status: TerminalStatus
    started_at_monotonic_ns: Annotated[int, Field(ge=0)]
    ended_at_monotonic_ns: Annotated[int, Field(ge=0)]
    reason: SafeId
    request_sha256: Digest | None
    response_sha256: Digest | None
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    attempt_count: Literal[0, 1]

    @model_validator(mode="after")
    def _terminal_consistent(self) -> TerminalOutcomeReceipt:
        if self.ended_at_monotonic_ns < self.started_at_monotonic_ns:
            raise ValueError("terminal time ordering is invalid")
        no_safe = self.status == "NO_SAFE_ROUTE"
        if no_safe != (self.selected_endpoint_id is None):
            raise ValueError("terminal endpoint and status disagree")
        if no_safe and self.attempt_count != 0:
            raise ValueError("no-safe-route terminal cannot dispatch")
        if self.status == "CANCELLED" and not no_safe:
            return self
        if not no_safe and self.attempt_count != 1:
            raise ValueError("dispatched terminal requires exactly one attempt")
        return self


class TrialSummary(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-trial-summary.v1"]
    trial_id: SafeId
    policy_id: PolicyId
    request_denominator: Literal[6]
    terminal_population: dict[TerminalStatus, Annotated[int, Field(ge=0, le=6)]]

    @model_validator(mode="after")
    def _complete_population(self) -> TrialSummary:
        expected = {
            "SUCCEEDED",
            "TIMED_OUT",
            "FAILED",
            "CANCELLED",
            "NO_SAFE_ROUTE",
        }
        if set(self.terminal_population) != expected:
            raise ValueError("terminal population is incomplete")
        if sum(self.terminal_population.values()) != 6:
            raise ValueError("terminal population does not close the denominator")
        return self


class ProducerReceipt(ExecutionModel):
    schema_version: Literal["inferdrome.routing-producer-receipt.v1"]
    executed_manifest_sha256: Digest
    input_transfer_receipt_sha256: Digest
    reset_receipts: tuple[ResetReceipt, ...]
    fault_receipts: tuple[FaultReceipt, ...]
    telemetry_observations: tuple[TelemetryObservation, ...]
    route_decisions: tuple[RouteDecisionReceipt, ...]
    terminal_outcomes: tuple[TerminalOutcomeReceipt, ...]
    trial_summaries: tuple[TrialSummary, ...]

    @model_validator(mode="after")
    def _receipt_population(self) -> ProducerReceipt:
        if tuple(row.policy_id for row in self.trial_summaries) != _POLICIES:
            raise ValueError("receipt trial policies are not complete")
        if len(self.reset_receipts) != 3 or len(self.fault_receipts) != 3:
            raise ValueError("receipt must have one reset and fault per policy")
        if len(self.route_decisions) != 18 or len(self.terminal_outcomes) != 18:
            raise ValueError("receipt must close the 18-terminal denominator")
        terminal_counts = Counter(row.trial_id for row in self.terminal_outcomes)
        if (
            any(count != 6 for count in terminal_counts.values())
            or len(terminal_counts) != 3
        ):
            raise ValueError("receipt terminal trial populations are incomplete")
        return self


class ArtifactHashEntry(ExecutionModel):
    path: Literal[
        "executed-manifest.json",
        "input-transfer-receipt.json",
        "producer-receipt.json",
    ]
    sha256: Digest
    size_bytes: Annotated[int, Field(ge=1, le=8_388_608)]


class IntegrityManifest(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-integrity-manifest.v1"]
    execution_id: ExecutionId
    hash_algorithm: Literal["sha256"]
    path_ordering: Literal["normalized_posix_ascending_v1"]
    entries: tuple[ArtifactHashEntry, ...]

    @model_validator(mode="after")
    def _closed_entries(self) -> IntegrityManifest:
        paths = tuple(row.path for row in self.entries)
        expected = (
            "executed-manifest.json",
            "input-transfer-receipt.json",
            "producer-receipt.json",
        )
        if paths != expected:
            raise ValueError("integrity manifest inventory is invalid")
        return self


def fixed_policy_ids() -> tuple[PolicyId, ...]:
    """Return the unchanged R1 policy order without importing its simulator."""

    return _POLICIES


def fixed_request_ids() -> tuple[str, ...]:
    """Return the fixed six request identifiers used by both R1 and PR B."""

    return _REQUEST_IDS


def fixed_r1_input_digests() -> dict[str, str]:
    """Return copies of the frozen R1 source identities bound by this bridge."""

    return dict(_R1_INPUT_DIGESTS)
