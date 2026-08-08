"""Typed, bounded response contracts for the local dashboard."""

from datetime import datetime
from typing import Literal

from inferdrome.domain.base import FrozenModel


class MetricView(FrozenModel):
    key: str
    metric: str
    aggregation: str
    label: str
    value: str
    display_value: str
    unit: str
    sample_count: int
    population: str
    definition_id: str
    quantile_method: str | None
    rounding_policy: str


class RunSummary(FrozenModel):
    run_id: str
    experiment_id: str
    title: str
    model: str
    producer_name: str
    producer_version: str
    adapter_name: str
    adapter_version: str
    execution_mode: str
    started_at: datetime
    ended_at: datetime
    duration_ns: int
    integrity_status: Literal["VALID"]
    evidence_eligibility: str
    environment_completeness: str
    replayability: str
    bundle_digest: str
    measured_requests: int
    successful_requests: int
    failed_requests: int
    error_rate: str
    ttft_p50_ns: int | None
    ttft_p95_ns: int | None
    output_token_throughput_per_s: str
    headline_metrics: tuple[MetricView, ...]


class ExecutionView(FrozenModel):
    terminal_state: Literal["COMPLETE"]
    started_at: datetime
    ended_at: datetime
    duration_ns: int
    measurement_window_ns: int
    measurement_window_definition: str
    traffic_kind: str
    concurrency: int | None
    requests_per_second: str | None
    max_concurrency: int | None
    warmup_requests: int
    measured_requests: int
    producer_exit_status: int


class DistributionBin(FrozenModel):
    lower_bound: int
    upper_bound: int
    count: int


class DistributionView(FrozenModel):
    metric: str
    label: str
    unit: str
    sample_count: int
    minimum: int | None
    maximum: int | None
    bins: tuple[DistributionBin, ...]


class ContextFieldView(FrozenModel):
    key: str
    label: str
    value: str | None
    group: Literal[
        "experiment",
        "execution",
        "target",
        "traffic",
        "measurement",
        "producer",
    ]


class EnvironmentFieldView(FrozenModel):
    name: str
    label: str
    value: str | int | bool | None
    provenance: str
    evidence_path: str | None


class ArtifactView(FrozenModel):
    role: str
    path: str
    media_type: str
    sensitivity: str
    size_bytes: int
    content_exposed: Literal[False]


class UnavailableMetricView(FrozenModel):
    metric: str
    reason: str
    capability_matrix: str


class DigestView(FrozenModel):
    source_spec_digest: str
    execution_fingerprint: str
    request_plan_digest: str
    metric_definitions_digest: str
    exitspec_contract_digest: str | None


class SensitivityView(FrozenModel):
    prompt_content_in_request_plan: bool
    canonical_response_content_included: bool
    native_response_content_present: bool
    secrets_permitted: Literal[False]


class VerificationView(FrozenModel):
    bundle_digest: str
    artifact_count: int
    total_bytes: int
    integrity_status: Literal["VALID"]
    evidence_eligibility: str
    environment_completeness: str
    replayability: str
    verified_by_recalculation: Literal[True]


class ComparisonContractView(FrozenModel):
    execution_fingerprint: str
    metric_definitions_digest: str
    reducer_version: str
    execution_mode: str
    workload_sha256: str
    requested_output_tokens: int
    temperature: str
    seed: int
    traffic_signature: str
    measurement_signature: str


class RunDetail(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = (
        "inferdrome.dashboard.v1"
    )
    summary: RunSummary
    hypothesis: str | None
    verification: VerificationView
    execution: ExecutionView
    measurements: tuple[MetricView, ...]
    distributions: tuple[DistributionView, ...]
    context: tuple[ContextFieldView, ...]
    environment: tuple[EnvironmentFieldView, ...]
    artifacts: tuple[ArtifactView, ...]
    unavailable: tuple[UnavailableMetricView, ...]
    digests: DigestView
    sensitivity: SensitivityView
    comparison_contract: ComparisonContractView


class RejectedRun(FrozenModel):
    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal[
        "VERIFICATION_FAILED",
        "UNSAFE_ENTRY",
        "BUNDLE_UNAVAILABLE",
        "DUPLICATE_RUN_ID",
    ]
    message: str = "Bundle could not be verified."


class PageView(FrozenModel):
    limit: int
    returned: int
    total: int
    has_more: bool
    next_cursor: str | None


class RunIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = (
        "inferdrome.dashboard.v1"
    )
    generated_at: datetime
    runs: tuple[RunSummary, ...]
    rejected: tuple[RejectedRun, ...]
    page: PageView


class MetricDeltaView(FrozenModel):
    key: str
    metric: str
    aggregation: str
    label: str
    unit: str
    baseline_value: str
    candidate_value: str
    absolute_delta: str
    percent_delta: str | None
    baseline_display_value: str
    candidate_display_value: str
    delta_display_value: str


class ContextChangeView(FrozenModel):
    key: str
    label: str
    baseline_value: str | None
    candidate_value: str | None
    group: str


class ComparisonResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = (
        "inferdrome.dashboard.v1"
    )
    baseline_run_id: str
    candidate_run_id: str
    status: Literal[
        "COMPARABLE",
        "COMPARABLE_WITH_CONTEXT_CHANGES",
        "INCOMPARABLE",
    ]
    reasons: tuple[str, ...]
    metric_deltas: tuple[MetricDeltaView, ...]
    context_changes: tuple[ContextChangeView, ...]
    directionality: Literal["NEUTRAL"] = "NEUTRAL"
