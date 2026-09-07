/**
 * Exact TypeScript mirrors of src/inferdrome/dashboard/models.py.
 *
 * ApiObject permits additive response fields so a compatible backend extension
 * does not break the client, while every v1 field consumed below stays required
 * and precisely typed.
 */
export interface ApiObject {
  readonly [key: string]: unknown;
}

export type JsonPrimitive = string | number | boolean | null;
export type ComparisonStatus =
  | "COMPARABLE"
  | "COMPARABLE_WITH_CONTEXT_CHANGES"
  | "INCOMPARABLE";
export type EvidenceEligibility = string;
export type IntegrityStatus = "VALID";

export interface MetricView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly value: string;
  readonly display_value: string;
  readonly unit: string;
  readonly sample_count: number;
  readonly population: string;
  readonly definition_id: string;
  readonly quantile_method: string | null;
  readonly rounding_policy: string;
}

export type Measurement = MetricView;
export type MeasurementValue = string;

export interface RunSummary extends ApiObject {
  readonly run_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly model: string;
  readonly producer_name: string;
  readonly producer_version: string;
  readonly adapter_name: string;
  readonly adapter_version: string;
  readonly execution_mode: string;
  readonly started_at: string;
  readonly ended_at: string;
  readonly duration_ns: number;
  readonly integrity_status: "VALID";
  readonly evidence_eligibility: string;
  readonly environment_completeness: string;
  readonly replayability: string;
  readonly bundle_digest: string;
  readonly measured_requests: number;
  readonly successful_requests: number;
  readonly failed_requests: number;
  readonly error_rate: string;
  readonly ttft_p50_ns: number | null;
  readonly ttft_p95_ns: number | null;
  readonly output_token_throughput_per_s: string;
  readonly headline_metrics: readonly MetricView[];
}

export interface ExecutionView extends ApiObject {
  readonly terminal_state: "COMPLETE";
  readonly started_at: string;
  readonly ended_at: string;
  readonly duration_ns: number;
  readonly measurement_window_ns: number;
  readonly measurement_window_definition: string;
  readonly traffic_kind: string;
  readonly concurrency: number | null;
  readonly requests_per_second: string | null;
  readonly max_concurrency: number | null;
  readonly warmup_requests: number;
  readonly measured_requests: number;
  readonly producer_exit_status: number;
}

export interface DistributionBin extends ApiObject {
  readonly lower_bound: number;
  readonly upper_bound: number;
  readonly count: number;
}

export interface DistributionView extends ApiObject {
  readonly metric: string;
  readonly label: string;
  readonly unit: string;
  readonly sample_count: number;
  readonly minimum: number | null;
  readonly maximum: number | null;
  readonly bins: readonly DistributionBin[];
}

export interface ContextFieldView extends ApiObject {
  readonly key: string;
  readonly label: string;
  readonly value: string | null;
  readonly group: "experiment" | "execution" | "target" | "traffic" | "measurement" | "producer";
}

export interface EnvironmentFieldView extends ApiObject {
  readonly name: string;
  readonly label: string;
  readonly value: string | number | boolean | null;
  readonly provenance: string;
  readonly evidence_path: string | null;
}

export interface ArtifactView extends ApiObject {
  readonly role: string;
  readonly path: string;
  readonly media_type: string;
  readonly sensitivity: string;
  readonly size_bytes: number;
  readonly content_exposed: false;
}

export interface UnavailableMetricView extends ApiObject {
  readonly metric: string;
  readonly reason: string;
  readonly capability_matrix: string;
}

export interface DigestView extends ApiObject {
  readonly source_spec_digest: string;
  readonly execution_fingerprint: string;
  readonly request_plan_digest: string;
  readonly metric_definitions_digest: string;
  readonly exitspec_contract_digest: string | null;
}

export interface SensitivityView extends ApiObject {
  readonly prompt_content_in_request_plan: boolean;
  readonly canonical_response_content_included: boolean;
  readonly native_response_content_present: boolean;
  readonly secrets_permitted: false;
}

export interface VerificationView extends ApiObject {
  readonly bundle_digest: string;
  readonly artifact_count: number;
  readonly total_bytes: number;
  readonly integrity_status: "VALID";
  readonly evidence_eligibility: string;
  readonly environment_completeness: string;
  readonly replayability: string;
  readonly verified_by_recalculation: true;
}

export interface ComparisonContractView extends ApiObject {
  readonly execution_fingerprint: string;
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly execution_mode: string;
  readonly workload_sha256: string;
  readonly requested_output_tokens: number;
  readonly temperature: string;
  readonly seed: number;
  readonly traffic_signature: string;
  readonly measurement_signature: string;
}

export interface RunDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: RunSummary;
  readonly hypothesis: string | null;
  readonly verification: VerificationView;
  readonly execution: ExecutionView;
  readonly measurements: readonly MetricView[];
  readonly distributions: readonly DistributionView[];
  readonly context: readonly ContextFieldView[];
  readonly environment: readonly EnvironmentFieldView[];
  readonly artifacts: readonly ArtifactView[];
  readonly unavailable: readonly UnavailableMetricView[];
  readonly digests: DigestView;
  readonly sensitivity: SensitivityView;
  readonly comparison_contract: ComparisonContractView;
}

export interface RejectedRun extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "VERIFICATION_FAILED"
    | "UNSAFE_ENTRY"
    | "BUNDLE_UNAVAILABLE"
    | "DUPLICATE_RUN_ID";
  readonly message: string;
}

export interface PageView extends ApiObject {
  readonly limit: number;
  readonly returned: number;
  readonly total: number;
  readonly has_more: boolean;
  readonly next_cursor: string | null;
}

export interface RunsPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly runs: readonly RunSummary[];
  readonly rejected: readonly RejectedRun[];
  readonly page: PageView;
}

export interface RunIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly runs: readonly RunSummary[];
  readonly rejected: readonly RejectedRun[];
}

export interface MetricDeltaView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly unit: string;
  readonly baseline_value: string;
  readonly candidate_value: string;
  readonly absolute_delta: string;
  readonly percent_delta: string | null;
  readonly baseline_display_value: string;
  readonly candidate_display_value: string;
  readonly delta_display_value: string;
}

export type ComparisonDelta = MetricDeltaView;

export interface ContextChangeView extends ApiObject {
  readonly key: string;
  readonly label: string;
  readonly baseline_value: string | null;
  readonly candidate_value: string | null;
  readonly group: string;
}

export interface Comparison extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly baseline_run_id: string;
  readonly candidate_run_id: string;
  readonly status: ComparisonStatus;
  readonly reasons: readonly string[];
  readonly metric_deltas: readonly MetricDeltaView[];
  readonly context_changes: readonly ContextChangeView[];
  readonly directionality: "NEUTRAL";
}

export interface TrialRunPointView extends ApiObject {
  readonly repetition_index: number;
  readonly run_id: string;
  readonly value: string | null;
  readonly display_value: string | null;
  readonly sample_count: number | null;
}

export interface TrialMetricVariationView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly unit: string;
  readonly total_run_count: number;
  readonly available_run_count: number;
  readonly minimum: string | null;
  readonly maximum: string | null;
  readonly median: string | null;
  readonly mean: string | null;
  readonly span: string | null;
  readonly sample_standard_deviation: string | null;
  readonly minimum_display_value: string | null;
  readonly maximum_display_value: string | null;
  readonly median_display_value: string | null;
  readonly mean_display_value: string | null;
  readonly span_display_value: string | null;
  readonly sample_standard_deviation_display_value: string | null;
  readonly points: readonly TrialRunPointView[];
  readonly population: "run_level_measurements";
  readonly weighting: "equal_per_run";
  readonly summary_method: "per_run_scalar_sample_variation_v1";
}

export interface TrialSetSummary extends ApiObject {
  readonly trial_set_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly created_at: string;
  readonly member_count: number;
  readonly earliest_run_at: string;
  readonly latest_run_at: string;
  readonly model: string;
  readonly execution_fingerprint: string;
  readonly trial_set_digest: string;
  readonly evidence_eligibilities: readonly string[];
  readonly environment_status: "CONSISTENT" | "DRIFT_DETECTED";
}

export interface TrialSetMemberView extends ApiObject {
  readonly repetition_index: number;
  readonly run: RunSummary;
}

export interface TrialSetDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: TrialSetSummary;
  readonly hypothesis: string | null;
  readonly membership_policy: "same_execution_fingerprint_v1";
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly members: readonly TrialSetMemberView[];
  readonly variations: readonly TrialMetricVariationView[];
  readonly environment_drift_fields: readonly string[];
  readonly design_status: "RETROSPECTIVE";
  readonly inference: "DESCRIPTIVE_ONLY";
  readonly request_population_policy: "separate_per_run_v1";
}

export interface RejectedTrialSet extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "VERIFICATION_FAILED"
    | "UNSAFE_ENTRY"
    | "MEMBER_UNAVAILABLE"
    | "DECLARATION_UNAVAILABLE"
    | "DUPLICATE_TRIAL_SET_ID";
  readonly message: string;
}

export interface TrialSetPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly trial_sets: readonly TrialSetSummary[];
  readonly rejected: readonly RejectedTrialSet[];
  readonly page: PageView;
}

export interface TrialSetIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly trial_sets: readonly TrialSetSummary[];
  readonly rejected: readonly RejectedTrialSet[];
}

/**
 * Read-only projection types for the sealed R1 routing-campaign package.
 *
 * These deliberately do not share the v0.1 run, trial-set, or comparison
 * contracts.  A campaign appears only after the backend has independently
 * verified its closed package and replayed the declared synthetic scenario.
 */
export type RoutingCampaignProjectionVersion = "inferdrome.routing-campaign-dashboard.v1";
export type RoutingCampaignId = "routing-campaign-v1";
export type RoutingEndpointId = "endpoint-a" | "endpoint-b";
export type RoutingSignal = "HEALTH" | "LOAD" | "KV";
export type RoutingAdmissibility = "ADMISSIBLE" | "INADMISSIBLE";
export type RoutingTerminalStatus =
  | "SUCCEEDED"
  | "TIMED_OUT"
  | "FAILED"
  | "CANCELLED"
  | "NO_SAFE_ROUTE";
export type RoutingFallbackReason =
  | "NONE"
  | "REQUIRED_LOAD_STALE"
  | "STALE_LOAD_FAIL_OPEN"
  | "HEALTH_ONLY_TIE_BREAK";

export interface RoutingCampaignSummary extends ApiObject {
  readonly campaign_id: RoutingCampaignId;
  readonly retained_digest: string;
  readonly execution_mode: "SYNTHETIC_CPU_ONLY";
  readonly trial_count: number;
  readonly planned_request_count: number;
  readonly policy_ids: readonly string[];
  readonly verified_by_replay: true;
}

export interface RoutingFaultTimelineView extends ApiObject {
  readonly load_collection_paused_at_ms: number;
  readonly health_collection_continues: true;
  readonly load_freshness_bound_ms: number;
  readonly health_freshness_bound_ms: number;
}

export interface RoutingEndpointInstanceView extends ApiObject {
  readonly endpoint_id: RoutingEndpointId;
  readonly instance_id: string;
}

export interface RoutingObserverEpochView extends ApiObject {
  readonly observer_id: string;
  readonly epoch: number;
}

export interface RoutingResetView extends ApiObject {
  readonly virtual_time_ms: number;
  readonly endpoint_instances: readonly RoutingEndpointInstanceView[];
  readonly observer_epochs: readonly RoutingObserverEpochView[];
  readonly queue_cleared: true;
  readonly load_state_cleared: true;
  readonly kv_state_cleared: true;
}

export interface RoutingTelemetryView extends ApiObject {
  readonly signal: RoutingSignal;
  readonly observer_id: string;
  readonly endpoint_id: RoutingEndpointId;
  readonly epoch: number;
  readonly observed_at_ms: number;
  readonly decision_time_ms: number;
  readonly age_ms: number;
  readonly freshness_bound_ms: number;
  readonly value: string | number;
  readonly admissibility: RoutingAdmissibility;
}

export interface RoutingCandidateView extends ApiObject {
  readonly endpoint_id: RoutingEndpointId;
  readonly eligible: boolean;
  readonly health: RoutingTelemetryView;
  readonly load: RoutingTelemetryView;
  readonly kv: RoutingTelemetryView;
}

export interface RoutingTerminalOutcomeView extends ApiObject {
  readonly terminal_outcome_id: string;
  readonly decision_id: string;
  readonly status: RoutingTerminalStatus;
  readonly reason: string;
  readonly started_at_ms: number;
  readonly ended_at_ms: number;
}

export interface RoutingRequestView extends ApiObject {
  readonly request_id: string;
  readonly sequence_index: number;
  readonly decision_id: string;
  readonly decision_time_ms: number;
  readonly candidates: readonly RoutingCandidateView[];
  readonly selected_endpoint_id: RoutingEndpointId | null;
  readonly claims_used: readonly string[];
  readonly claims_permitted_stale: readonly string[];
  readonly claims_discarded: readonly string[];
  readonly fallback_reason: RoutingFallbackReason;
  readonly terminal: RoutingTerminalOutcomeView;
}

export interface RoutingTerminalPopulationView extends ApiObject {
  readonly status: RoutingTerminalStatus;
  readonly count: number;
}

export interface RoutingCampaignTrialView extends ApiObject {
  readonly trial_id: string;
  readonly policy_id: string;
  readonly reset: RoutingResetView;
  readonly requests: readonly RoutingRequestView[];
  readonly terminal_population: readonly RoutingTerminalPopulationView[];
  readonly terminal_population_total: number;
}

export interface RoutingCampaignDetail extends ApiObject {
  readonly projection_version: RoutingCampaignProjectionVersion;
  readonly summary: RoutingCampaignSummary;
  readonly fault_timeline: RoutingFaultTimelineView;
  readonly trials: readonly RoutingCampaignTrialView[];
  readonly interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY";
}

export interface RejectedRoutingCampaign extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code: "VERIFICATION_FAILED" | "UNSAFE_ENTRY";
  readonly message: string;
}

export interface RoutingCampaignPageResponse extends ApiObject {
  readonly projection_version: RoutingCampaignProjectionVersion;
  readonly routing_campaigns: readonly RoutingCampaignSummary[];
  readonly rejected: readonly RejectedRoutingCampaign[];
  readonly page: PageView;
}

export interface RoutingCampaignIndex extends ApiObject {
  readonly projection_version: RoutingCampaignProjectionVersion;
  readonly routing_campaigns: readonly RoutingCampaignSummary[];
  readonly rejected: readonly RejectedRoutingCampaign[];
}

/**
 * Read-only causal qualification projection over one R1 package and its
 * independently bound PR4 descriptor. It intentionally remains separate from
 * the strict routing-campaign-v1 API contract above.
 */
export type RoutingQualificationProjectionVersion = "inferdrome.routing-qualification-dashboard.v1";
export type RoutingQualificationId = "stale-telemetry-qualification-v1";
export type RoutingQualificationPolicyId =
  | "fail_closed_required_load_v1"
  | "explicit_fail_open_stale_load_v1"
  | "typed_admissible_state_only_v1";

export interface RoutingQualificationSummary extends ApiObject {
  readonly qualification_id: RoutingQualificationId;
  readonly retained_digest: string;
  readonly source_campaign_id: "routing-campaign-v1";
  readonly source_package_retained_digest: string;
  readonly source_execution_mode: "SYNTHETIC_CPU_ONLY";
  readonly repetitions_per_mode: 1;
  readonly population_accounting: "SEPARATE_PER_TRIAL_NO_POOLING";
  readonly verified_by_source_replay: true;
  readonly verified_descriptor_binding: true;
}

export interface RoutingQualificationFaultView extends ApiObject {
  readonly load_observer_pause_at_ms: 15;
  readonly health_collection_continues: true;
  readonly focal_decision_time_ms: 20;
  readonly health_age_ms: 0;
  readonly load_age_ms: 10;
  readonly freshness_bound_ms: 5;
}

export interface RoutingQualificationEndpointStateView extends ApiObject {
  readonly endpoint_id: RoutingEndpointId;
  readonly health_epoch: number;
  readonly health_age_ms: 0;
  readonly health_admissibility: "ADMISSIBLE";
  readonly load_epoch: number;
  readonly load_age_ms: 10;
  readonly load_admissibility: "INADMISSIBLE";
}

export interface RoutingQualificationPopulationEntry extends ApiObject {
  readonly status: RoutingTerminalStatus;
  readonly count: number;
}

export interface RoutingQualificationResetView extends ApiObject {
  readonly virtual_time_ms: 0;
  readonly endpoint_a_instance_id: string;
  readonly endpoint_b_instance_id: string;
  readonly observer_epochs: readonly [number, number, number];
  readonly queue_cleared: true;
  readonly load_state_cleared: true;
  readonly kv_state_cleared: true;
}

export interface RoutingQualificationTrialView extends ApiObject {
  readonly policy_id: RoutingQualificationPolicyId;
  readonly repetition_index: 0;
  readonly trial_id: string;
  readonly request_denominator: 6;
  readonly reset: RoutingQualificationResetView;
  readonly focal_request_id: "request-002";
  readonly focal_decision_id: string;
  readonly focal_endpoint_states: readonly [
    RoutingQualificationEndpointStateView,
    RoutingQualificationEndpointStateView,
  ];
  readonly selected_endpoint_id: RoutingEndpointId | null;
  readonly fallback_reason:
    | "REQUIRED_LOAD_STALE"
    | "STALE_LOAD_FAIL_OPEN"
    | "HEALTH_ONLY_TIE_BREAK";
  readonly terminal_status: RoutingTerminalStatus;
  readonly terminal_reason: string;
  readonly reset_receipt_sha256: string;
  readonly state_observations_sha256: string;
  readonly route_decisions_sha256: string;
  readonly terminal_outcomes_sha256: string;
  readonly terminal_population: readonly [
    RoutingQualificationPopulationEntry,
    RoutingQualificationPopulationEntry,
    RoutingQualificationPopulationEntry,
    RoutingQualificationPopulationEntry,
    RoutingQualificationPopulationEntry,
  ];
  readonly terminal_population_total: 6;
}

export interface RoutingQualificationDetail extends ApiObject {
  readonly projection_version: RoutingQualificationProjectionVersion;
  readonly summary: RoutingQualificationSummary;
  readonly fault_timeline: RoutingQualificationFaultView;
  readonly trials: readonly [
    RoutingQualificationTrialView,
    RoutingQualificationTrialView,
    RoutingQualificationTrialView,
  ];
  readonly source_receipts_path: "/routing-campaigns/routing-campaign-v1";
  readonly descriptor_download_path: "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence";
  readonly interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY";
}

export interface RejectedRoutingQualification extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code: "CONFIGURATION_INVALID" | "UNSAFE_ENTRY" | "VERIFICATION_FAILED";
  readonly message: string;
}

export interface RoutingQualificationPageResponse extends ApiObject {
  readonly projection_version: RoutingQualificationProjectionVersion;
  readonly routing_qualifications: readonly RoutingQualificationSummary[];
  readonly rejected: readonly RejectedRoutingQualification[];
  readonly page: PageView;
}

export interface RoutingQualificationIndex extends ApiObject {
  readonly projection_version: RoutingQualificationProjectionVersion;
  readonly routing_qualifications: readonly RoutingQualificationSummary[];
  readonly rejected: readonly RejectedRoutingQualification[];
}

/**
 * A deliberately separate reader contract for the sealed real-endpoint bridge.
 * It is not interchangeable with the synthetic routing-campaign projection.
 */
export type RoutingExecutionProjectionVersion = "inferdrome.routing-execution-dashboard.v1";
export type RoutingExecutionMode = "LOCAL_LOOPBACK" | "GCP_PRIVATE" | "LAMBDA_MANUAL_HOST";
export type RoutingExecutionTerminalStatus =
  | "SUCCEEDED"
  | "TIMED_OUT"
  | "FAILED"
  | "CANCELLED"
  | "NO_SAFE_ROUTE";
export type RoutingExecutionSignal = "HEALTH" | "LOAD" | "GPU_DCGM" | "KV_CACHE";
export type RoutingExecutionAdmissibility = "ADMISSIBLE" | "INADMISSIBLE";
export type RoutingExecutionObservationState = "AVAILABLE" | "STALE" | "UNAVAILABLE";
export type RoutingExecutionFallbackReason =
  | "NONE"
  | "REQUIRED_LOAD_STALE"
  | "REQUIRED_LOAD_UNAVAILABLE"
  | "STALE_LOAD_FAIL_OPEN"
  | "HEALTH_ONLY_TIE_BREAK"
  | "HEALTH_NOT_ADMISSIBLE";

export interface RoutingExecutionModelView extends ApiObject {
  readonly model_id: "Qwen/Qwen3-8B";
  readonly model_revision: string;
  readonly tokenizer_revision: string;
}

export interface RoutingExecutionRuntimeView extends ApiObject {
  readonly runtime_name: "vllm";
  readonly runtime_version: string;
  readonly adapter_id: string;
  readonly adapter_version: string;
}

export interface RoutingExecutionTopologyView extends ApiObject {
  readonly accelerator_model: string;
  readonly accelerator_count: number;
  readonly runner_separate_from_serving: true;
  readonly serving_engine_count: 2;
  readonly one_engine_per_endpoint: true;
  readonly declared_provider: "LAMBDA" | null;
  readonly declared_provisioning: "OPERATOR_SUPPLIED_VM" | null;
  readonly identity_assertion: "OPERATOR_DECLARED_NOT_OBSERVED" | "NOT_RETAINED_BY_V1";
  readonly lifecycle_protection: "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY" | "NOT_RETAINED_BY_V1";
}

export interface RoutingExecutionSummary extends ApiObject {
  readonly execution_id: "routing-execution-v1";
  readonly retained_digest: string;
  readonly mode: RoutingExecutionMode;
  readonly source_commit: string;
  readonly model: RoutingExecutionModelView;
  readonly runtime: RoutingExecutionRuntimeView;
  readonly topology: RoutingExecutionTopologyView;
  readonly policy_ids: readonly [string, string, string];
  readonly trial_count: 3;
  readonly request_denominator_per_trial: 6;
  readonly terminal_denominator: 18;
  readonly verified_by_offline_replay: true;
}

export interface RejectedRoutingExecution extends ApiObject {
  readonly entry: "<configured-root>";
  readonly status: "REJECTED";
  readonly code: "CONFIGURATION_INVALID" | "UNSAFE_ENTRY" | "VERIFICATION_FAILED";
  readonly message: string;
}

export interface RoutingExecutionPageResponse extends ApiObject {
  readonly projection_version: RoutingExecutionProjectionVersion;
  readonly routing_executions: readonly RoutingExecutionSummary[];
  readonly rejected: readonly RejectedRoutingExecution[];
  readonly page: PageView;
}

export interface RoutingExecutionIndex extends ApiObject {
  readonly projection_version: RoutingExecutionProjectionVersion;
  readonly routing_executions: readonly RoutingExecutionSummary[];
  readonly rejected: readonly RejectedRoutingExecution[];
}

export interface RoutingExecutionEndpointIdentityView extends ApiObject {
  readonly endpoint_id: RoutingEndpointId;
}

export interface RoutingExecutionInputTransferView extends ApiObject {
  readonly config_sha256: string;
  readonly selected_workload_sha256: string;
  readonly workload_size_bytes: number;
  readonly declared_input_transfer_sha256: string;
  readonly verified_before_transport: true;
}

export interface RoutingExecutionEvidenceView extends ApiObject {
  readonly runner_image: string;
  readonly serving_image: string;
  readonly endpoints: readonly [RoutingExecutionEndpointIdentityView, RoutingExecutionEndpointIdentityView];
  readonly input_transfer_receipt_sha256: string;
  readonly input_transfer: RoutingExecutionInputTransferView;
}

export interface RoutingExecutionRoutingInputsView extends ApiObject {
  readonly campaign_id: "routing-campaign-v1";
  readonly plan_sha256: string;
  readonly trace_sha256: string;
  readonly fault_schedule_sha256: string;
  readonly trial_plan_sha256: string;
  readonly policies: readonly [string, string, string];
}

export interface RoutingExecutionWorkloadView extends ApiObject {
  readonly workload_id: string;
  readonly workload_sha256: string;
  readonly selected_workload_sha256: string;
  readonly selected_request_ids: readonly [string, string, string, string, string, string];
  readonly request_denominator: 6;
}

export interface RoutingExecutionTelemetryPlanView extends ApiObject {
  readonly clock_domain: "RUNNER_MONOTONIC_NS";
  readonly health_freshness_ms: 5;
  readonly load_freshness_ms: 5;
  readonly gpu_freshness_ms: 5;
  readonly load_metric_name: "vllm:num_requests_running";
}

export interface RoutingExecutionFaultPlanView extends ApiObject {
  readonly fault_id: "stale-load-fresh-health-v1";
  readonly load_collection_pause_after_sequence_index: 1;
  readonly health_collection_continues: true;
  readonly inter_request_interval_ms: number;
}

export interface RoutingExecutionCampaignView extends ApiObject {
  readonly routing_inputs: RoutingExecutionRoutingInputsView;
  readonly workload: RoutingExecutionWorkloadView;
  readonly telemetry: RoutingExecutionTelemetryPlanView;
  readonly fault: RoutingExecutionFaultPlanView;
}

export interface RoutingExecutionObserverEpochView extends ApiObject {
  readonly signal: RoutingExecutionSignal;
  readonly epoch: number;
}

export interface RoutingExecutionResetView extends ApiObject {
  readonly reset_at_monotonic_ns: number;
  readonly observer_epochs: readonly RoutingExecutionObserverEpochView[];
  readonly runner_connection_state_cleared: true;
  readonly runner_telemetry_state_cleared: true;
  readonly endpoint_engine_reset_assertion: "NOT_ASSERTED_SEPARATE_SERVING_ENGINE";
  readonly endpoint_runtime_identities: readonly [RoutingExecutionEndpointIdentityView, RoutingExecutionEndpointIdentityView];
}

export interface RoutingExecutionFaultReceiptView extends ApiObject {
  readonly fault_id: "stale-load-fresh-health-v1";
  readonly activated_at_sequence_index: 2;
  readonly activated_at_monotonic_ns: number;
  readonly load_collection_paused: true;
  readonly health_collection_continues: true;
}

export interface RoutingExecutionTelemetryView extends ApiObject {
  readonly signal: RoutingExecutionSignal;
  readonly observer_id: string;
  readonly endpoint_id: RoutingEndpointId;
  readonly epoch: number;
  readonly sampled_at_monotonic_ns: number;
  readonly decision_at_monotonic_ns: number;
  readonly age_ns: number;
  readonly freshness_bound_ns: number;
  readonly state: RoutingExecutionObservationState;
  readonly admissibility: RoutingExecutionAdmissibility;
  readonly value: string | number | null;
  readonly source: string;
}

export interface RoutingExecutionCandidateView extends ApiObject {
  readonly endpoint_id: RoutingEndpointId;
  readonly eligible: boolean;
  readonly health: RoutingExecutionTelemetryView;
  readonly load: RoutingExecutionTelemetryView;
  readonly gpu_dcgm: RoutingExecutionTelemetryView;
  readonly kv_cache: RoutingExecutionTelemetryView;
}

export interface RoutingExecutionTerminalView extends ApiObject {
  readonly terminal_outcome_id: string;
  readonly decision_id: string;
  readonly selected_endpoint_id: RoutingEndpointId | null;
  readonly status: RoutingExecutionTerminalStatus;
  readonly reason: string;
  readonly started_at_monotonic_ns: number;
  readonly ended_at_monotonic_ns: number;
  readonly http_status: number | null;
  readonly attempt_count: 0 | 1;
}

export interface RoutingExecutionRequestView extends ApiObject {
  readonly request_id: string;
  readonly sequence_index: number;
  readonly decision_id: string;
  readonly decision_at_monotonic_ns: number;
  readonly candidates: readonly [RoutingExecutionCandidateView, RoutingExecutionCandidateView];
  readonly selected_endpoint_id: RoutingEndpointId | null;
  readonly claims_used: readonly string[];
  readonly claims_permitted_stale: readonly string[];
  readonly claims_discarded: readonly string[];
  readonly fallback_reason: RoutingExecutionFallbackReason;
  readonly terminal: RoutingExecutionTerminalView;
}

export interface RoutingExecutionTerminalPopulationView extends ApiObject {
  readonly status: RoutingExecutionTerminalStatus;
  readonly count: number;
}

export interface RoutingExecutionTrialView extends ApiObject {
  readonly trial_id: string;
  readonly policy_id: string;
  readonly reset: RoutingExecutionResetView;
  readonly fault: RoutingExecutionFaultReceiptView;
  readonly requests: readonly [
    RoutingExecutionRequestView,
    RoutingExecutionRequestView,
    RoutingExecutionRequestView,
    RoutingExecutionRequestView,
    RoutingExecutionRequestView,
    RoutingExecutionRequestView,
  ];
  readonly terminal_population: readonly [
    RoutingExecutionTerminalPopulationView,
    RoutingExecutionTerminalPopulationView,
    RoutingExecutionTerminalPopulationView,
    RoutingExecutionTerminalPopulationView,
    RoutingExecutionTerminalPopulationView,
  ];
  readonly terminal_population_total: 6;
}

export interface RoutingExecutionDetail extends ApiObject {
  readonly projection_version: RoutingExecutionProjectionVersion;
  readonly summary: RoutingExecutionSummary;
  readonly evidence: RoutingExecutionEvidenceView;
  readonly campaign: RoutingExecutionCampaignView;
  readonly trials: readonly [RoutingExecutionTrialView, RoutingExecutionTrialView, RoutingExecutionTrialView];
  readonly interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY";
}

export type ControlledComparisonResultStatus =
  | "COMPARABLE"
  | "INCOMPARABLE"
  | "NO_RESULT"
  | "WITHHELD";

export type ControlledComparisonCheckId =
  | "LOCAL_PLAN_ORDER"
  | "EXACT_ARM_MEMBERSHIP"
  | "OBSERVED_SCHEDULE"
  | "DECLARED_FINGERPRINT_DIFFERENCE"
  | "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"
  | "OUTCOME_COVERAGE_AND_SEMANTICS";

export interface ControlledComparisonSummary extends ApiObject {
  readonly comparison_plan_id: string;
  readonly comparison_plan_digest: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly created_at: string;
  readonly treatment_path: "traffic.concurrency";
  readonly baseline_value: number;
  readonly candidate_value: number;
  readonly planned_repetitions_per_arm: number;
  readonly baseline_trial_set_id: string;
  readonly candidate_trial_set_id: string;
  readonly design_status: "PREDECLARED";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
  readonly primary_outcome_key: string;
  readonly primary_outcome_label: string;
  readonly primary_outcome_unit: string;
  readonly result_status: ControlledComparisonResultStatus;
  readonly comparison_result_id: string | null;
  readonly comparison_result_digest: string | null;
  readonly estimate: string | null;
  readonly estimate_display_value: string | null;
}

export interface RejectedControlledComparison extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "UNSAFE_ENTRY"
    | "DECLARATION_UNAVAILABLE"
    | "PLAN_VERIFICATION_FAILED"
    | "RESULT_VERIFICATION_FAILED"
    | "DUPLICATE_RESULT_FOR_PLAN"
    | "ORPHAN_RESULT";
  readonly failure_fingerprint: string;
  readonly message: string;
}

export interface ControlledComparisonPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly comparisons: readonly ControlledComparisonSummary[];
  readonly rejected: readonly RejectedControlledComparison[];
  readonly page: PageView;
}

export interface ControlledComparisonIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly comparisons: readonly ControlledComparisonSummary[];
  readonly rejected: readonly RejectedControlledComparison[];
}

export interface ControlledComparisonIndependentVariable extends ApiObject {
  readonly value_type: "integer";
  readonly path: "traffic.concurrency";
  readonly baseline_value: number;
  readonly candidate_value: number;
}

export interface ControlledComparisonArmPlanView extends ApiObject {
  readonly arm: "BASELINE" | "CANDIDATE";
  readonly planned_trial_set_id: string;
  readonly source_spec_digest: string;
  readonly expected_execution_fingerprint: string;
  readonly run_ids: readonly string[];
}

export interface ControlledComparisonScheduleSlot extends ApiObject {
  readonly sequence_index: number;
  readonly block_index: number;
  readonly within_block_position: 0 | 1;
  readonly arm: "BASELINE" | "CANDIDATE";
  readonly repetition_index: number;
  readonly run_id: string;
}

export interface ControlledComparisonOutcomeSelector extends ApiObject {
  readonly metric: string;
  readonly aggregation: string;
  readonly definition_id: string;
  readonly unit: string;
  readonly population: string;
  readonly quantile_method: string | null;
  readonly rounding_policy: string;
}

export interface ControlledComparisonPlanView extends ApiObject {
  readonly schema_version: "inferdrome.controlled-comparison-plan.v1";
  readonly comparison_plan_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly hypothesis: string;
  readonly created_at: string;
  readonly design_status: "PREDECLARED";
  readonly arm_membership_policy: "exact_ordered_run_ids_v1";
  readonly schedule_policy: "predeclared_permuted_pairs_v1";
  readonly schedule_seed: string;
  readonly statistical_unit: "run";
  readonly request_population_policy: "separate_per_run_v1";
  readonly weighting: "equal_per_run";
  readonly planned_repetitions_per_arm: number;
  readonly independent_variable: ControlledComparisonIndependentVariable;
  readonly baseline_arm: ControlledComparisonArmPlanView;
  readonly candidate_arm: ControlledComparisonArmPlanView;
  readonly ordered_schedule: readonly ControlledComparisonScheduleSlot[];
  readonly primary_outcome: ControlledComparisonOutcomeSelector;
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly estimator: "paired_run_mean_difference_v1";
  readonly contrast_direction: "candidate_minus_baseline";
  readonly uncertainty_method: "none_v1";
  readonly missing_data_policy: "incomparable_if_any_outcome_missing_v1";
  readonly exclusion_policy: "no_post_assignment_exclusions_v1";
  readonly environment_policy: "complete_and_equal_observed_environment_v1";
  readonly environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY";
  readonly predeclaration_anchor: "operator_retained_plan_digest_required_v1";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
}

export type ControlledComparisonRunProgressState =
  | "PENDING"
  | "CREATED"
  | "PREFLIGHT"
  | "WARMUP"
  | "MEASURING"
  | "FINALIZING"
  | "COMPLETE"
  | "FAILED"
  | "INTERRUPTED"
  | "INVALID";

export interface ControlledComparisonRunProgress extends ApiObject {
  readonly sequence_index: number;
  readonly run_id: string;
  readonly state: ControlledComparisonRunProgressState;
  readonly verified_bundle: boolean;
}

export interface ControlledComparisonExecutionView extends ApiObject {
  readonly status:
    | "NOT_STARTED"
    | "PARTIAL"
    | "BLOCKED"
    | "EVIDENCE_COMPLETE";
  readonly result_published: boolean;
  readonly completed_run_count: number;
  readonly planned_run_count: number;
  readonly next_sequence_index: number | null;
  readonly exact_schedule_prefix: boolean;
  readonly issue: "PROGRESS_INSPECTION_FAILED" | null;
  readonly slots: readonly ControlledComparisonRunProgress[];
}

export interface ControlledComparisonControlCheck extends ApiObject {
  readonly check: ControlledComparisonCheckId;
  readonly status: "SATISFIED" | "UNSATISFIED";
}

export interface ControlledComparisonRunValue extends ApiObject {
  readonly repetition_index: number;
  readonly run_id: string;
  readonly value: string;
  readonly sample_count: number;
}

export interface ControlledComparisonPairedDifference extends ApiObject {
  readonly block_index: number;
  readonly baseline_run_id: string;
  readonly candidate_run_id: string;
  readonly candidate_minus_baseline: string;
}

export interface ControlledComparisonOutcomeEstimate extends ApiObject {
  readonly selector: ControlledComparisonOutcomeSelector;
  readonly unit: string;
  readonly baseline_values: readonly ControlledComparisonRunValue[];
  readonly candidate_values: readonly ControlledComparisonRunValue[];
  readonly paired_differences: readonly ControlledComparisonPairedDifference[];
  readonly baseline_mean: string;
  readonly candidate_mean: string;
  readonly estimate: string;
}

export interface ControlledComparisonResult extends ApiObject {
  readonly schema_version: "inferdrome.controlled-comparison-result.v1";
  readonly comparison_result_id: string;
  readonly comparison_plan_id: string;
  readonly comparison_plan_digest: string;
  readonly created_at: string;
  readonly baseline_trial_set: {
    readonly trial_set_id: string;
    readonly trial_set_digest: string;
  };
  readonly candidate_trial_set: {
    readonly trial_set_id: string;
    readonly trial_set_digest: string;
  };
  readonly status: "COMPARABLE" | "INCOMPARABLE";
  readonly inference_scope: "POINT_ESTIMATE_ONLY";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
  readonly environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY";
  readonly statistical_unit: "run";
  readonly weighting: "equal_per_run";
  readonly estimator: "paired_run_mean_difference_v1";
  readonly contrast_direction: "candidate_minus_baseline";
  readonly uncertainty_method: "none_v1";
  readonly control_checks: readonly ControlledComparisonControlCheck[];
  readonly unsatisfied_controls: readonly ControlledComparisonCheckId[];
  readonly outcomes: readonly ControlledComparisonOutcomeEstimate[];
}

export interface ControlledComparisonDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: ControlledComparisonSummary;
  readonly plan: ControlledComparisonPlanView;
  readonly execution: ControlledComparisonExecutionView;
  readonly result: ControlledComparisonResult | null;
  readonly baseline_trial_set: TrialSetSummary | null;
  readonly candidate_trial_set: TrialSetSummary | null;
  readonly result_issue:
    | "RESULT_VERIFICATION_FAILED"
    | "DUPLICATE_RESULT_FOR_PLAN"
    | null;
}
