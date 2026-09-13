"""Allowlisted read-only contracts for one verified routing-execution package."""

from typing import Literal

from inferdrome.dashboard.models import PageView
from inferdrome.domain.base import FrozenModel

RoutingExecutionId = Literal["routing-execution-v1"]
RoutingExecutionMode = Literal[
    "LOCAL_LOOPBACK", "GCP_PRIVATE", "LAMBDA_MANUAL_HOST", "VAST_MANUAL_CONTAINER"
]
RoutingTerminalStatus = Literal[
    "SUCCEEDED", "TIMED_OUT", "FAILED", "CANCELLED", "NO_SAFE_ROUTE"
]
RoutingExecutionSignal = Literal["HEALTH", "LOAD", "GPU_DCGM", "KV_CACHE"]
RoutingExecutionState = Literal["AVAILABLE", "STALE", "UNAVAILABLE"]
RoutingExecutionAdmissibility = Literal["ADMISSIBLE", "INADMISSIBLE"]


class RoutingExecutionModelView(FrozenModel):
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: str
    tokenizer_revision: str


class RoutingExecutionRuntimeView(FrozenModel):
    runtime_name: Literal["vllm"]
    runtime_version: str
    adapter_id: str
    adapter_version: str


class RoutingExecutionTopologyView(FrozenModel):
    accelerator_model: str
    accelerator_count: int
    runner_separate_from_serving: Literal[True]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    declared_provider: Literal["LAMBDA"] | None
    declared_provisioning: Literal["OPERATOR_SUPPLIED_VM"] | None
    identity_assertion: Literal[
        "OPERATOR_DECLARED_NOT_OBSERVED", "NOT_RETAINED_BY_V1"
    ]
    lifecycle_protection: Literal[
        "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY", "NOT_RETAINED_BY_V1"
    ]


class VastRoutingExecutionTopologyView(FrozenModel):
    profile_id: Literal["vast-container-two-h100-sxm5-80gb-v1"]
    accelerator_model: Literal["NVIDIA H100-SXM5-80GB"]
    accelerator_count: Literal[2]
    container_count: Literal[1]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    tensor_parallel_size: Literal[1]
    declared_provider: Literal["VAST_AI"]
    declared_provisioning: Literal["OPERATOR_SUPPLIED_CONTAINER"]
    identity_assertion: Literal["OPERATOR_DECLARED_NOT_OBSERVED"]
    lifecycle_protection: Literal["UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"]
    isolation_boundary: Literal["SEPARATE_PROCESSES_SHARED_CONTAINER"]
    observer_gpu_isolation: Literal["ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED"]


class RoutingExecutionSummary(FrozenModel):
    execution_id: RoutingExecutionId
    retained_digest: str
    mode: RoutingExecutionMode
    source_commit: str
    model: RoutingExecutionModelView
    runtime: RoutingExecutionRuntimeView
    topology: RoutingExecutionTopologyView | VastRoutingExecutionTopologyView
    policy_ids: tuple[str, str, str]
    trial_count: Literal[3]
    request_denominator_per_trial: Literal[6]
    terminal_denominator: Literal[18]
    verified_by_offline_replay: Literal[True] = True


class RejectedRoutingExecution(FrozenModel):
    """A package was withheld without path or verifier-detail disclosure."""

    entry: Literal["<configured-root>"]
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal["CONFIGURATION_INVALID", "UNSAFE_ENTRY", "VERIFICATION_FAILED"]
    message: str = "Routing execution could not be verified."


class RoutingExecutionIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.routing-execution-dashboard.v1"] = (
        "inferdrome.routing-execution-dashboard.v1"
    )
    routing_executions: tuple[RoutingExecutionSummary, ...]
    rejected: tuple[RejectedRoutingExecution, ...]
    page: PageView


class RoutingExecutionEndpointIdentityView(FrozenModel):
    endpoint_id: Literal["endpoint-a", "endpoint-b"]


class RoutingExecutionInputTransferView(FrozenModel):
    config_sha256: str
    selected_workload_sha256: str
    workload_size_bytes: int
    declared_input_transfer_sha256: str
    verified_before_transport: Literal[True]


class RoutingExecutionEvidenceView(FrozenModel):
    runner_image: str
    serving_image: str
    endpoints: tuple[
        RoutingExecutionEndpointIdentityView, RoutingExecutionEndpointIdentityView
    ]
    input_transfer_receipt_sha256: str
    input_transfer: RoutingExecutionInputTransferView


class RoutingExecutionArtifactProvenanceView(FrozenModel):
    container_image_assertion: Literal["OPERATOR_DECLARED_NOT_OBSERVED"]
    source_commit: str
    observer_artifact_sha256: str
    supervisor_artifact_sha256: str
    model_manifest_sha256: str
    model_snapshot_sha256: str
    runtime_observation_sha256: str
    runtime_assertion: Literal["LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION"]


class VastRoutingExecutionEvidenceView(FrozenModel):
    container_image: str
    artifact_provenance: RoutingExecutionArtifactProvenanceView
    endpoints: tuple[
        RoutingExecutionEndpointIdentityView, RoutingExecutionEndpointIdentityView
    ]
    input_transfer_receipt_sha256: str
    input_transfer: RoutingExecutionInputTransferView


class RoutingExecutionRoutingInputsView(FrozenModel):
    campaign_id: Literal["routing-campaign-v1"]
    plan_sha256: str
    trace_sha256: str
    fault_schedule_sha256: str
    trial_plan_sha256: str
    policies: tuple[str, str, str]


class RoutingExecutionWorkloadView(FrozenModel):
    workload_id: str
    workload_sha256: str
    selected_workload_sha256: str
    selected_request_ids: tuple[str, str, str, str, str, str]
    request_denominator: Literal[6]


class RoutingExecutionTelemetryPlanView(FrozenModel):
    clock_domain: Literal["RUNNER_MONOTONIC_NS"]
    health_freshness_ms: Literal[5]
    load_freshness_ms: Literal[5]
    gpu_freshness_ms: Literal[5]
    load_metric_name: Literal["vllm:num_requests_running"]


class RoutingExecutionFaultPlanView(FrozenModel):
    fault_id: Literal["stale-load-fresh-health-v1"]
    load_collection_pause_after_sequence_index: Literal[1]
    health_collection_continues: Literal[True]
    inter_request_interval_ms: int


class RoutingExecutionCampaignView(FrozenModel):
    routing_inputs: RoutingExecutionRoutingInputsView
    workload: RoutingExecutionWorkloadView
    telemetry: RoutingExecutionTelemetryPlanView
    fault: RoutingExecutionFaultPlanView


class RoutingExecutionObserverEpochView(FrozenModel):
    signal: RoutingExecutionSignal
    epoch: int


class RoutingExecutionResetView(FrozenModel):
    reset_at_monotonic_ns: int
    observer_epochs: tuple[RoutingExecutionObserverEpochView, ...]
    runner_connection_state_cleared: Literal[True]
    runner_telemetry_state_cleared: Literal[True]
    endpoint_engine_reset_assertion: Literal["NOT_ASSERTED_SEPARATE_SERVING_ENGINE"]
    endpoint_runtime_identities: tuple[
        RoutingExecutionEndpointIdentityView, RoutingExecutionEndpointIdentityView
    ]


class RoutingExecutionFaultReceiptView(FrozenModel):
    fault_id: Literal["stale-load-fresh-health-v1"]
    activated_at_sequence_index: Literal[2]
    activated_at_monotonic_ns: int
    load_collection_paused: Literal[True]
    health_collection_continues: Literal[True]


class RoutingExecutionTelemetryView(FrozenModel):
    signal: RoutingExecutionSignal
    observer_id: str
    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    epoch: int
    sampled_at_monotonic_ns: int
    decision_at_monotonic_ns: int
    age_ns: int
    freshness_bound_ns: int
    state: RoutingExecutionState
    admissibility: RoutingExecutionAdmissibility
    value: int | Literal["HEALTHY", "UNAVAILABLE"]
    source: Literal["HTTP_HEALTH", "VLLM_METRICS", "UNAVAILABLE_CAPABILITY"]


class RoutingExecutionCandidateView(FrozenModel):
    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    eligible: bool
    health: RoutingExecutionTelemetryView
    load: RoutingExecutionTelemetryView
    gpu_dcgm: RoutingExecutionTelemetryView
    kv_cache: RoutingExecutionTelemetryView


class RoutingExecutionTerminalView(FrozenModel):
    terminal_outcome_id: str
    decision_id: str
    selected_endpoint_id: Literal["endpoint-a", "endpoint-b"] | None
    status: RoutingTerminalStatus
    reason: str
    started_at_monotonic_ns: int
    ended_at_monotonic_ns: int
    http_status: int | None
    attempt_count: Literal[0, 1]


class RoutingExecutionRequestView(FrozenModel):
    request_id: str
    sequence_index: int
    decision_id: str
    decision_at_monotonic_ns: int
    candidates: tuple[RoutingExecutionCandidateView, RoutingExecutionCandidateView]
    selected_endpoint_id: Literal["endpoint-a", "endpoint-b"] | None
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
    fallback_reason: Literal[
        "NONE",
        "REQUIRED_LOAD_STALE",
        "REQUIRED_LOAD_UNAVAILABLE",
        "STALE_LOAD_FAIL_OPEN",
        "HEALTH_ONLY_TIE_BREAK",
        "HEALTH_NOT_ADMISSIBLE",
    ]
    terminal: RoutingExecutionTerminalView


class RoutingExecutionTerminalPopulationView(FrozenModel):
    status: RoutingTerminalStatus
    count: int


class RoutingExecutionTrialView(FrozenModel):
    trial_id: str
    policy_id: str
    reset: RoutingExecutionResetView
    fault: RoutingExecutionFaultReceiptView
    requests: tuple[
        RoutingExecutionRequestView,
        RoutingExecutionRequestView,
        RoutingExecutionRequestView,
        RoutingExecutionRequestView,
        RoutingExecutionRequestView,
        RoutingExecutionRequestView,
    ]
    terminal_population: tuple[
        RoutingExecutionTerminalPopulationView,
        RoutingExecutionTerminalPopulationView,
        RoutingExecutionTerminalPopulationView,
        RoutingExecutionTerminalPopulationView,
        RoutingExecutionTerminalPopulationView,
    ]
    terminal_population_total: Literal[6]


class RoutingExecutionDetail(FrozenModel):
    projection_version: Literal["inferdrome.routing-execution-dashboard.v1"] = (
        "inferdrome.routing-execution-dashboard.v1"
    )
    summary: RoutingExecutionSummary
    evidence: RoutingExecutionEvidenceView | VastRoutingExecutionEvidenceView
    campaign: RoutingExecutionCampaignView
    trials: tuple[
        RoutingExecutionTrialView,
        RoutingExecutionTrialView,
        RoutingExecutionTrialView,
    ]
    interpretation_boundary: Literal["MEASUREMENT_EVIDENCE_ONLY"] = (
        "MEASUREMENT_EVIDENCE_ONLY"
    )
