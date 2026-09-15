"""Small public views of pinned evaluation reports, never source replay."""

from typing import Annotated, Literal

from pydantic import Field

from inferdrome.domain.base import FrozenModel
from inferdrome.evaluation.policies import PolicyId

ReportKind = Literal["STUDY", "PREFIX_CACHE"]
ReportId = Annotated[str, Field(pattern=r"^ev-[0-9a-f]{64}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=120)]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,95}$")]
Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
DecimalText = Annotated[str, Field(pattern=r"^-?[0-9]{1,128}(?:\.[0-9]{1,6})?$")]
Unit = Literal["count", "ns", "ratio", "requests/s", "tokens", "blocks"]


class EvaluationMetric(FrozenModel):
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,95}$")]
    label: Label
    value: DecimalText | None
    unit: Unit


class EvaluationLatency(FrozenModel):
    label: Label
    population: Code
    count: Count
    p50_ns: DecimalText | None
    p90_ns: DecimalText | None
    p95_ns: DecimalText | None
    p99_ns: DecimalText | None
    p99_status: Literal[
        "BELOW_REPORTING_FLOOR", "DESCRIPTIVE_ONLY", "NOT_A_SUCCESS_LATENCY_POPULATION"
    ]


class EvaluationOutcome(FrozenModel):
    outcome: Code
    count: Count
    offered_fraction: DecimalText


class EvaluationPopulation(FrozenModel):
    metrics: Annotated[tuple[EvaluationMetric, ...], Field(max_length=32)]
    outcomes: Annotated[tuple[EvaluationOutcome, ...], Field(max_length=16)]
    latency: Annotated[tuple[EvaluationLatency, ...], Field(max_length=24)]
    usage: Annotated[tuple[EvaluationMetric, ...], Field(max_length=16)]
    cancelled: bool


class EvaluationRecoveryInterval(FrozenModel):
    metric: Literal["publication", "decision", "dispatch"]
    status: Literal[
        "NOT_APPLICABLE", "RESTORE_NOT_OBSERVED", "UNOBSERVED_OR_CENSORED", "OBSERVED"
    ]
    duration_ns: DecimalText | None
    observation_horizon_ns: DecimalText | None


class EvaluationRecovery(FrozenModel):
    applicability: Literal[
        "NOT_APPLICABLE_SCENARIO", "NOT_APPLICABLE_POLICY", "APPLICABLE"
    ]
    origin: Literal["ACTUAL_TELEMETRY_RESTORED_EVENT"]
    planned_restore_ns: DecimalText | None
    actual_restore_ns: DecimalText | None
    intervals: tuple[
        EvaluationRecoveryInterval,
        EvaluationRecoveryInterval,
        EvaluationRecoveryInterval,
    ]
    background_active_at_restore: bool | None


class EvaluationContrast(FrozenModel):
    label: Label
    complete_blocks: Count
    mean_rps: DecimalText | None
    lower_rps: DecimalText | None
    upper_rps: DecimalText | None
    interval_status: Code


class EvaluationPolicy(FrozenModel):
    policy_id: PolicyId
    metrics: Annotated[tuple[EvaluationMetric, ...], Field(max_length=8)]


class EvaluationStratum(FrozenModel):
    index: Annotated[int, Field(ge=1, le=64)]
    scenario: Code
    profile_index: Annotated[int, Field(ge=1, le=64)]
    target_endpoint: Literal["endpoint-a", "endpoint-b"] | None
    metrics: Annotated[tuple[EvaluationMetric, ...], Field(max_length=8)]
    policies: tuple[
        EvaluationPolicy, EvaluationPolicy, EvaluationPolicy, EvaluationPolicy
    ]
    contrasts: tuple[EvaluationContrast, EvaluationContrast, EvaluationContrast]


class EvaluationTrial(FrozenModel):
    index: Annotated[int, Field(ge=1, le=256)]
    block_index: Annotated[int, Field(ge=1, le=64)]
    profile_index: Annotated[int, Field(ge=1, le=64)]
    policy_id: PolicyId
    scenario: Code
    status: Code
    result_sha256: Digest
    metrics: Annotated[tuple[EvaluationMetric, ...], Field(max_length=8)]
    foreground: EvaluationPopulation
    background: EvaluationPopulation | None
    recovery: EvaluationRecovery


class EvaluationCell(FrozenModel):
    index: Annotated[int, Field(ge=1, le=32)]
    condition: Literal["S0", "S1", "U0", "U1"]
    workload_family: Literal["SHARED", "UNIQUE"]
    mode: Literal["DECLARED_ENABLED", "DECLARED_DISABLED"]
    status: Code
    reason: Code
    cleanup: Code
    config_sha256: Digest
    result_sha256: Digest | None
    declarations_consistent: bool
    declaration_reasons: Annotated[tuple[Code, ...], Field(max_length=32)]
    metrics: Annotated[tuple[EvaluationMetric, ...], Field(max_length=8)]
    population: EvaluationPopulation | None
    assignment: Annotated[tuple[EvaluationMetric, ...], Field(max_length=4)]
    output_lengths: Annotated[tuple[EvaluationMetric, ...], Field(max_length=16)]


class EvaluationCacheBlock(FrozenModel):
    index: Annotated[int, Field(ge=1, le=8)]
    cells: tuple[EvaluationCell, EvaluationCell, EvaluationCell, EvaluationCell]
    prefix_potential: Annotated[tuple[EvaluationMetric, ...], Field(max_length=12)]
    output_length_differences: Annotated[
        tuple[EvaluationMetric, ...], Field(max_length=2)
    ]


class EvaluationSummary(FrozenModel):
    report_id: ReportId
    kind: ReportKind
    label: Label
    report_sha256: Digest
    plan_sha256: Digest
    config_sha256: Digest | None
    source_schema: Literal[
        "inferdrome.evaluation-study-report.v1", "inferdrome.evaluation-cache-report.v1"
    ]
    status: Code
    comparison_status: Code
    reason: Code | None
    calibration: Literal["UNCALIBRATED_REHEARSAL", "UNAVAILABLE"]
    evidence_class: Literal["SYNTHETIC_ONLY", "LOCAL_MEASUREMENT_ONLY"] | None
    returned_records: Count
    report_integrity: Literal["EXPECTED_DIGEST_MATCH"] = "EXPECTED_DIGEST_MATCH"
    report_contract: Literal["VALIDATED"] = "VALIDATED"
    source_replay: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False
    tokenizer_reverified_here: Literal[False] = False


class RejectedEvaluationReport(FrozenModel):
    entry: Annotated[int, Field(ge=1, le=8)]
    code: Literal[
        "CONFIGURATION_INVALID",
        "REPORT_UNAVAILABLE",
        "DIGEST_MISMATCH",
        "REPORT_INVALID",
        "PROJECTION_LIMIT",
    ]
    message: Literal["Configured report was withheld."] = (
        "Configured report was withheld."
    )


class EvaluationReportIndex(FrozenModel):
    projection_version: Literal["inferdrome.evaluation-dashboard.v1"] = (
        "inferdrome.evaluation-dashboard.v1"
    )
    reports: Annotated[tuple[EvaluationSummary, ...], Field(max_length=8)]
    rejected: Annotated[tuple[RejectedEvaluationReport, ...], Field(max_length=8)]


class _EvaluationDetail(FrozenModel):
    projection_version: Literal["inferdrome.evaluation-dashboard.v1"] = (
        "inferdrome.evaluation-dashboard.v1"
    )
    summary: EvaluationSummary
    coverage: Annotated[tuple[EvaluationMetric, ...], Field(max_length=24)]
    reporting: Annotated[tuple[EvaluationMetric, ...], Field(max_length=16)]
    limitations: Annotated[tuple[Code, ...], Field(max_length=16)]


class EvaluationStudyDetail(_EvaluationDetail):
    kind: Literal["STUDY"] = "STUDY"
    strata: Annotated[tuple[EvaluationStratum, ...], Field(max_length=64)]
    trials: Annotated[tuple[EvaluationTrial, ...], Field(max_length=256)]


class EvaluationCacheDetail(_EvaluationDetail):
    kind: Literal["PREFIX_CACHE"] = "PREFIX_CACHE"
    cache_treatment_attribution: Literal["UNVERIFIED"] = "UNVERIFIED"
    workload_verification: Literal[
        "VERIFIED_PINNED_QWEN3", "SYNTHETIC_TOKENIZER", "UNAVAILABLE"
    ]
    low_replication: bool
    preparation_issues: Annotated[tuple[Code, ...], Field(max_length=128)]
    blocks: Annotated[tuple[EvaluationCacheBlock, ...], Field(max_length=8)]
    contrasts: tuple[EvaluationContrast, EvaluationContrast, EvaluationContrast]


EvaluationReportDetail = Annotated[
    EvaluationStudyDetail | EvaluationCacheDetail, Field(discriminator="kind")
]
