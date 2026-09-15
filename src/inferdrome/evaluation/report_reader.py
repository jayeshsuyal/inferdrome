"""Closed readers for retained reducer reports, without replay or attestation.

These checks establish the supported report shape and internal consistency only.
They do not establish reducer authorship, source execution, or cache treatment.
No source artifact, tokenizer, endpoint, or filesystem is consulted here.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from inferdrome.evaluation.contracts import EndpointId, EvaluationError, Outcome
from inferdrome.evaluation.policies import POLICY_IDS, PolicyId
from inferdrome.metrics.quantiles import decimal_ratio
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes

MAX_REPORT_BYTES = 4 * 1024 * 1024
REPORT_LIMITS = StructuredDataLimits(
    max_depth=32, max_tokens=1_000_000, max_integer_digits=16
)
Integer = Annotated[int, Field(ge=0, le=2**53 - 1)]
Count = Annotated[int, Field(ge=0, le=100_000)]
Positive = Annotated[int, Field(ge=1, le=2**53 - 1)]
Seed = Annotated[int, Field(ge=0, le=2**32 - 1)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SafeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
DecimalText = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,31})\.[0-9]{6}$")]
SignedDecimalText = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]{0,31})\.[0-9]{6}$")]
TotalText = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,31})$")]
EvidenceClass = Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]
Scenario = Literal["HEALTHY", "STALE_LOAD"]
Condition = Literal["S0", "S1", "U0", "U1"]
TrialStatus = Literal["COMPLETED", "WARMUP_FAILED", "CANCELLED"]
RecoveryStatus = Literal[
    "NOT_APPLICABLE", "RESTORE_NOT_OBSERVED", "UNOBSERVED_OR_CENSORED", "OBSERVED"
]
AttributionReason = Literal[
    "UNKNOWN_CACHE_MODE",
    "UNKNOWN_PROCESS_GENERATION",
    "UNKNOWN_RUNTIME_RECIPE",
    "UNKNOWN_INITIAL_PREFIX_STATE",
    "UNKNOWN_PREPARATION_METHOD",
    "UNKNOWN_WARMUP",
    "UNKNOWN_CHRONOLOGY",
    "UNCONTROLLED_TRAFFIC",
    "TOKENIZATION_UNAVAILABLE",
    "MISSING_CELL_INPUT",
    "INVALID_CELL_INPUT",
]
PreparationIssue = Literal[
    "REUSED_PREPARATION_ID",
    "RUNTIME_RECIPE_UNRESOLVED_OR_MISMATCHED",
    "PREPARATION_METHOD_MISMATCH",
    "PROCESS_GENERATION_UNRESOLVED",
    "REUSED_FRESH_PROCESS_GENERATION",
    "EVIDENCE_CLASS_MISMATCH",
]
_OUTCOMES = get_args(Outcome)
_CONDITIONS = ("S0", "S1", "U0", "U1")
_CONTRASTS = (
    "shared_enabled_minus_disabled",
    "unique_enabled_minus_disabled",
    "interaction",
)
_STUDY_LIMITATIONS = (
    "DIGESTS_CHECK_INTEGRITY_NOT_RUNTIME_ATTESTATION_OR_INDEPENDENT_EXECUTION",
    "SCHEDULED_ARRIVAL_ORIGIN_AND_FIXED_OFFERED_WINDOW",
    "REJECTIONS_FAILURES_CANCELLATIONS_REMAIN_IN_OFFERED_DENOMINATOR",
    "TRIAL_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS",
    "P99_FLOOR_IS_NOT_A_PRECISION_OR_INDEPENDENCE_GUARANTEE",
    "DECLARED_CACHE_PREPARATION_AND_RUNTIME_IDENTITIES_UNVERIFIED",
    "MALFORMED_AIOHTTP_FRAMING_CAN_BE_CLASSIFIED_TIMEOUT",
    "NO_LIVE_CAPACITY_COST_OR_ACTUAL_OVERLOAD_ESTABLISHED",
)
_CACHE_LIMITATIONS = (
    "DECLARATIONS_AND_DIGESTS_DO_NOT_ATTEST_RUNTIME_OR_INDEPENDENT_EXECUTION",
    "ALL_OFFERED_SCHEDULED_ORIGIN_SLOS_WITH_FIXED_WINDOW",
    "MISSING_OR_INVALID_CELLS_ARE_UNAVAILABLE_NOT_ZERO",
    "WHOLE_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS",
    "INTERACTION_IS_NOT_A_PREFILL_ONLY_OR_CACHE_HIT_MEASUREMENT",
    "GENERATED_LENGTHS_USE_ONLY_VALID_SERVER_USAGE_WITH_EXPLICIT_COVERAGE",
    "EXTERNAL_PREPARATION_AND_BETWEEN_CELL_TIME_ARE_UNMEASURED",
)


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def exact_literal_primitives(cls, value: object, info: Any) -> object:
        annotation = cls.model_fields[info.field_name].annotation
        if get_origin(annotation) is Literal:
            options = get_args(annotation)
            if (
                options
                and type(options[0]) in (int, bool)
                and type(value) is not type(options[0])
            ):
                raise ValueError("literal primitive differs")
        if info.field_name == "percentile_ranks" and (
            type(value) is not list or any(type(item) is not int for item in value)
        ):
            raise ValueError("rank primitives differ")
        # Every array in these closed contracts is a bounded tuple. A before
        # validator receives Python primitives even on model_validate_json.
        if type(value) is list:
            return tuple(value)
        return value


class Ratio(ReportModel):
    numerator: Integer
    denominator: Positive
    decimal: DecimalText


class SignedRatio(ReportModel):
    sign: Literal[-1, 0, 1]
    magnitude_numerator: Integer
    denominator: Positive
    decimal: SignedDecimalText


class RatioInterval(ReportModel):
    lower: SignedRatio
    upper: SignedRatio


class Quantiles(ReportModel):
    count: Count
    method: Literal["NEAREST_RANK"]
    p50: Integer | None
    p90: Integer | None
    p95: Integer | None
    p99: Integer | None
    p99_status: Literal[
        "NOT_A_SUCCESS_LATENCY_POPULATION", "DESCRIPTIVE_ONLY", "BELOW_REPORTING_FLOOR"
    ]
    p99_min_successes: Literal[1000]


class DispatchLag(Quantiles):
    population: Literal["ALL_DISPATCHED_OUTCOMES"]


class SuccessfulLatency(ReportModel):
    population: Literal["ALL_SUCCESS_INCLUDING_SLO_MISSES"]
    count: Count
    scheduled_to_first_content: Quantiles
    scheduled_to_terminal: Quantiles
    dispatch_to_first_content: Quantiles
    dispatch_to_terminal: Quantiles


class PartialTiming(ReportModel):
    offered_count: Count
    scheduled_to_first_content_ns: Quantiles


class Usage(ReportModel):
    population_count: Count
    reported_count: Count
    missing_count: Count
    prompt_tokens_total_decimal: TotalText
    completion_tokens_total_decimal: TotalText
    provenance: Literal["SERVER_REPORTED_STREAM_USAGE_ONLY"]


class OutcomeCount(ReportModel):
    count: Count
    offered_fraction: Ratio


class PopulationCounts(ReportModel):
    offered_count: Annotated[int, Field(ge=1, le=10_000)]
    successful_count: Count
    outcomes: dict[Outcome, OutcomeCount]
    arrival_observed_count: Count
    dispatched_count: Count
    peak_active: Annotated[int, Field(ge=0, le=64)]
    peak_queue: Annotated[int, Field(ge=0, le=1024)]
    elapsed_ns: Integer
    cancelled: bool
    usage_success: Usage
    usage_other_outcomes: Usage


class PopulationSummary(PopulationCounts):
    slo_good_count: Count
    slo_success_fraction: Ratio
    slo_goodput_rps: Ratio
    offered_rate_rps: Ratio
    dispatch_rate_rps: Ratio
    fixed_offered_window_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    drain_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    last_offer_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]
    successful_completions_during_drain: Count
    dispatch_lag_ns: DispatchLag
    successful_latency_ns: SuccessfulLatency
    partial_timing_by_outcome: dict[Outcome, PartialTiming]


class RecoveryInterval(ReportModel):
    duration_ns: Integer | None
    status: RecoveryStatus
    observation_horizon_ns: Integer | None


class RecoveryIntervals(ReportModel):
    publication: RecoveryInterval
    decision: RecoveryInterval
    dispatch: RecoveryInterval


class Recovery(ReportModel):
    applicability: Literal[
        "NOT_APPLICABLE_SCENARIO", "NOT_APPLICABLE_POLICY", "APPLICABLE"
    ]
    origin: Literal["ACTUAL_TELEMETRY_RESTORED_EVENT"]
    planned_restore_ns: Integer | None
    actual_restore_ns: Integer | None
    intervals: RecoveryIntervals
    background_active_at_restore: bool | None
    fault_actual_overload: Literal[
        "NOT_ESTABLISHED_BY_FAULT_SCHEDULE", "NOT_APPLICABLE"
    ]


class TrialSummary(ReportModel):
    schema_version: Literal["inferdrome.evaluation-study-trial-summary.v1"]
    trial_id: Annotated[str, Field(pattern=r"^trial-[0-9]{4}$")]
    block_id: SafeId
    profile_id: SafeId
    scenario: Scenario
    target_endpoint_id: EndpointId | None
    repeat_index: Annotated[int, Field(ge=0, le=63)]
    workload_seed: Seed
    order_seed: Seed
    policy_id: PolicyId
    config_sha256: Digest
    workload_sha256: Digest
    window_start_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]
    window_end_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    first_content_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    completion_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    cooldown_ns: Annotated[int, Field(ge=0, le=60_000_000_000)]
    worst_case_duration_ns: Positive
    foreground_offered_count: Count
    background_offered_count: Count
    result_sha256: Digest
    status: TrialStatus
    evidence_class: EvidenceClass
    evidence_eligible: Literal[False]
    calibration: Literal["UNCALIBRATED_REHEARSAL"]
    runtime_identity: Literal["UNVERIFIED"]
    cost: Literal["UNAVAILABLE"]
    timing_semantics: Literal["CONTENT_EVENTS_NOT_EXACT_TOKENS_OR_WIRE_TIMING"]
    foreground: PopulationSummary
    background: PopulationCounts | None
    model_warmup: Literal["EXTERNALLY_PREPARED_UNMEASURED"]
    trial_elapsed_ns: Integer
    recovery: Recovery


class OfferCoverage(ReportModel):
    planned_offers: Count
    returned_records: Count
    offers_in_aborted_trials_without_measurements: Count
    offers_in_not_run_trials: Count


class StudyCoverage(ReportModel):
    planned_trials: Annotated[int, Field(ge=4, le=256)]
    started_trials: Count
    returned_trials: Count
    aborted_trials: Annotated[int, Field(ge=0, le=1)]
    not_run_trials: Count
    completed_trial_results: Count
    warmup_failed_results: Count
    cancelled_results: Count
    foreground: OfferCoverage
    background: OfferCoverage


class StudyReporting(ReportModel):
    p99_min_successes: Literal[1000]
    bootstrap_resamples: Literal[2000]
    confidence_percent: Literal[90]
    minimum_complete_blocks: Literal[8]
    bootstrap_seed: Seed


class PolicySummary(ReportModel):
    policy_id: PolicyId
    returned_trials: Annotated[int, Field(ge=0, le=64)]
    completed_trials: Annotated[int, Field(ge=0, le=64)]
    slo_good_counts_by_trial: Annotated[tuple[Count, ...], Field(max_length=64)]
    mean_goodput_rps: Ratio | None
    observed_min_goodput_rps: Ratio | None
    observed_max_goodput_rps: Ratio | None


class StudyContrast(ReportModel):
    policy_id: PolicyId
    reference_policy_id: Literal["evaluation_round_robin_v1"]
    replication_unit: Literal["MATCHED_TRIAL_BLOCK"]
    complete_blocks: Annotated[int, Field(ge=0, le=64)]
    mean_goodput_difference_rps: SignedRatio | None
    interval_status: Literal[
        "INCOMPLETE_STUDY", "INSUFFICIENT_TRIAL_REPLICATION", "AVAILABLE"
    ]
    confidence_percent: Literal[90]
    percentile_ranks: tuple[Literal[5], Literal[95]]
    bootstrap_resamples: Literal[2000]
    bootstrap_seed: Seed
    interval_rps: RatioInterval | None


class RecoveryCounts(ReportModel):
    applicable_trials: Count
    observed_trials: Count
    censored_or_restore_unobserved_trials: Count
    not_applicable_trials: Count


class RecoveryCoverage(ReportModel):
    publication: RecoveryCounts
    decision: RecoveryCounts
    dispatch: RecoveryCounts


class StudyStratum(ReportModel):
    scenario: Scenario
    profile_id: SafeId
    target_endpoint_id: EndpointId | None
    planned_blocks: Annotated[int, Field(ge=1, le=64)]
    complete_blocks: Annotated[int, Field(ge=0, le=64)]
    fixed_window_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    policies: Annotated[tuple[PolicySummary, ...], Field(min_length=4, max_length=4)]
    paired_contrasts: Annotated[
        tuple[StudyContrast, ...], Field(min_length=3, max_length=3)
    ]
    recovery_coverage: RecoveryCoverage


class StudyPreparation(ReportModel):
    cache_state: Literal["UNKNOWN", "DECLARED_COLD", "DECLARED_WARM"]
    prefix_caching: Literal["UNKNOWN", "DECLARED_ENABLED", "DECLARED_DISABLED"]
    model_warmup: Literal["EXTERNALLY_PREPARED_UNMEASURED"]
    model_warmup_reference: Digest
    serving_image_reference: Digest | None
    runtime_verification: Literal["UNVERIFIED"]
    cost: Literal["UNAVAILABLE"]


class StudyReport(ReportModel):
    schema_version: Literal["inferdrome.evaluation-study-report.v1"]
    config_sha256: Digest
    plan_sha256: Digest
    status: Literal["COMPLETED", "CANCELLED", "ABORTED"]
    reason: (
        Literal[
            "CANCELLED",
            "STUDY_DEADLINE",
            "WARMUP_FAILED",
            "CONTROLLER_OR_CLEANUP_FAILED",
            "RESULT_VALIDATION_FAILED",
            "OUTPUT_FAILED",
        ]
        | None
    )
    study_elapsed_ns: Integer
    study_elapsed_semantics: Literal[
        "WHOLE_OPERATION_INCLUDING_TRIALS_COOLDOWNS_AND_IO"
    ]
    comparative_headline: Literal[
        "DESCRIPTIVE_UNCALIBRATED_REHEARSAL", "SUPPRESSED_INCOMPLETE_STUDY"
    ]
    coverage: StudyCoverage
    reporting: StudyReporting
    strata: Annotated[tuple[StudyStratum, ...], Field(min_length=1, max_length=64)]
    trials: Annotated[tuple[TrialSummary, ...], Field(max_length=256)]
    evidence_class: EvidenceClass
    evidence_eligible: Literal[False]
    runtime_identity: Literal["UNVERIFIED"]
    calibration: Literal["UNCALIBRATED_REHEARSAL"]
    cost: Literal["UNAVAILABLE"]
    model_warmup: Literal["EXTERNALLY_PREPARED_UNMEASURED"]
    preparation: StudyPreparation
    duplicate_payload_hash_groups: Annotated[int, Field(ge=0, le=128)]
    limitations: Annotated[tuple[str, ...], Field(min_length=8, max_length=8)]


class EndpointAssignment(ReportModel):
    endpoint_id: EndpointId
    planned_offers: Count
    actual_dispatches: Count


class OutputLengths(ReportModel):
    population: Literal["SERVER_REPORTED_COMPLETION_TOKENS"]
    population_count: Count
    reported_count: Count
    missing_count: Count
    total_decimal: TotalText
    mean_decimal: DecimalText | None
    p50: Integer | None
    p90: Integer | None
    p95: Integer | None


class ReportedOutputLengths(ReportModel):
    success: OutputLengths
    other_outcomes: OutputLengths


class CacheCell(ReportModel):
    index: Annotated[int, Field(ge=0, le=31)]
    cell_id: Annotated[str, Field(pattern=r"^cell-[0-9]{4}$")]
    block_id: SafeId
    condition: Condition
    workload_family: Literal["SHARED", "UNIQUE"]
    prefix_enabled: bool
    attempt_id: SafeId
    cell_sha256: Digest
    config_sha256: Digest
    workload_sha256: Digest
    planned_offers: Annotated[int, Field(ge=4, le=4000)]
    status: Literal["UNAVAILABLE", "COMPLETED", "CANCELLED", "ABORTED"]
    reason: Literal[
        "MISSING_CELL_INPUT",
        "INVALID_CELL_INPUT",
        "COMPLETED",
        "CANCELLED",
        "EXECUTION_FAILED",
        "RESULT_LIMIT",
        "DURATION_LIMIT",
    ]
    cleanup: Literal["CONFIRMED_BY_LOCAL_RESULT", "UNCONFIRMED", "NOT_STARTED"]
    preparation_sha256: Digest | None
    result_sha256: Digest | None
    evidence_class: EvidenceClass | None
    attribution_eligible: bool
    attribution_reasons: Annotated[tuple[AttributionReason, ...], Field(max_length=11)]
    elapsed_ns: Integer | None
    population: PopulationSummary | None
    endpoint_assignment: tuple[EndpointAssignment, EndpointAssignment] | None
    reported_output_lengths: ReportedOutputLengths | None


class CacheCoverage(ReportModel):
    planned_cells: Annotated[int, Field(ge=4, le=32)]
    completed_cells: Count
    cancelled_cells: Count
    aborted_cells: Count
    missing_cells: Count
    invalid_cells: Count
    planned_blocks: Literal[1, 4, 8]
    complete_blocks: Annotated[int, Field(ge=0, le=8)]
    planned_offers: Count
    returned_records: Count
    offers_without_measurements: Count


class CacheReporting(StudyReporting):
    fixed_offered_window_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    first_content_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    completion_slo_ns: Annotated[int, Field(ge=1, le=60_000_000_000)]
    cache_block_size: Annotated[int, Field(ge=1, le=256)]
    percentile_ranks: tuple[Literal[5], Literal[95]]


class PrefixPotential(ReportModel):
    block_id: SafeId
    input_sha256: Digest
    shared_lcp_tokens: Annotated[int, Field(ge=0, le=2048)]
    shared_document_lcp_tokens: Annotated[int, Field(ge=0, le=2048)]
    shared_document_complete_prefix_blocks: Annotated[int, Field(ge=0, le=2048)]
    shared_lcp_token_ids_sha256: Digest
    shared_document_lcp_token_ids_sha256: Digest
    unique_max_pairwise_lcp_tokens: Annotated[int, Field(ge=0, le=2048)]
    wrapper_overlap_tokens: Annotated[int, Field(ge=0, le=2048)]
    shared_complete_prefix_blocks: Annotated[int, Field(ge=0, le=2048)]
    unique_max_complete_prefix_blocks: Annotated[int, Field(ge=0, le=2048)]
    extra_shared_prefix_blocks: Annotated[int, Field(ge=1, le=2048)]
    claim: Literal["TOKEN_PREFIX_POTENTIAL_NOT_CACHE_HITS"]


class CacheContrast(ReportModel):
    contrast: Literal[
        "shared_enabled_minus_disabled", "unique_enabled_minus_disabled", "interaction"
    ]
    replication_unit: Literal["MATCHED_FOUR_CELL_BLOCK"]
    complete_blocks: Annotated[int, Field(ge=0, le=8)]
    mean_goodput_difference_rps: SignedRatio | None
    block_goodput_differences_rps: (
        Annotated[tuple[SignedRatio, ...], Field(max_length=8)] | None
    )
    interval_status: Literal[
        "SUPPRESSED_COMPARISON", "INSUFFICIENT_COMPLETE_BLOCKS", "DESCRIPTIVE_ONLY"
    ]
    interval_rps: RatioInterval | None


class OutputLengthDifference(ReportModel):
    block_id: SafeId
    population: Literal["FULLY_COVERED_SERVER_REPORTED_SUCCESS_MEANS"]
    shared_mean_tokens_difference_decimal: SignedDecimalText | None
    unique_mean_tokens_difference_decimal: SignedDecimalText | None
    status: Literal[
        "SUPPRESSED_COMPARISON",
        "UNAVAILABLE_USAGE",
        "DESCRIPTIVE_SUCCESS_CONDITIONAL_NOT_MATCHED_OUTPUTS",
    ]


class CacheReport(ReportModel):
    schema_version: Literal["inferdrome.evaluation-cache-report.v1"]
    plan_sha256: Digest
    status: Literal["COMPLETED", "INCOMPLETE"]
    comparison_status: Literal[
        "AVAILABLE",
        "SUPPRESSED_INCOMPLETE",
        "SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH",
    ]
    comparative_headline: Literal[
        "SYNTHETIC_ONLY_DESCRIPTIVE_CONTRAST",
        "DECLARED_CONDITION_DESCRIPTIVE_CONTRAST",
        "SUPPRESSED",
    ]
    low_replication: bool
    evidence_class: EvidenceClass | None
    planned_evidence_class: EvidenceClass
    evidence_classes: Annotated[tuple[EvidenceClass, ...], Field(max_length=2)]
    evidence_eligible: Literal[False]
    runtime_verification: Literal["UNVERIFIED"]
    cache_treatment_attribution: Literal["UNVERIFIED"]
    workload_verification: Literal[
        "UNAVAILABLE", "SYNTHETIC_TOKENIZER", "VERIFIED_PINNED_QWEN3"
    ]
    prefix_potential: (
        Annotated[tuple[PrefixPotential, ...], Field(min_length=1, max_length=8)] | None
    )
    calibration: Literal["UNAVAILABLE"]
    cost: Literal["UNAVAILABLE"]
    cache_hits: Literal["UNAVAILABLE"]
    external_preparation_elapsed_ns: None
    external_preparation_requests: None
    external_preparation_tokens: None
    model_warmup: Literal["EXTERNALLY_PREPARED_UNMEASURED"]
    sum_returned_owned_elapsed_ns: Integer
    elapsed_semantics: Literal[
        "SUM_OF_OWNED_CELL_RUNS_NOT_EXTERNAL_PREPARATION_OR_WALL_SPAN"
    ]
    coverage: CacheCoverage
    preparation_consistency_issues: Annotated[
        tuple[PreparationIssue, ...], Field(max_length=6)
    ]
    reporting: CacheReporting
    comparison_block_ids: Annotated[tuple[SafeId, ...], Field(max_length=8)]
    contrasts: Annotated[tuple[CacheContrast, ...], Field(min_length=3, max_length=3)]
    output_length_differences: Annotated[
        tuple[OutputLengthDifference, ...], Field(min_length=1, max_length=8)
    ]
    cells: Annotated[tuple[CacheCell, ...], Field(min_length=4, max_length=32)]
    limitations: Annotated[tuple[str, ...], Field(min_length=7, max_length=7)]


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("report contract mismatch")


def _ratio(value: Ratio, numerator: int, denominator: int) -> None:
    _require(value.numerator == numerator and value.denominator == denominator)
    _require(value.decimal == decimal_ratio(numerator, denominator))


def _signed(value: SignedRatio | None, numerator: int, denominator: int) -> None:
    _require(value is not None)
    assert value is not None
    magnitude = decimal_ratio(abs(numerator), denominator)
    _require(value.sign == (-1 if numerator < 0 else 1 if numerator else 0))
    _require(
        value.magnitude_numerator == abs(numerator) and value.denominator == denominator
    )
    _require(
        value.decimal
        == ("-" if numerator < 0 and magnitude != "0.000000" else "") + magnitude
    )


def _interval(
    value: RatioInterval | None,
    *,
    required: bool,
    denominator: int,
    floor: int,
    ceiling: int,
) -> None:
    _require((value is not None) == required)
    if value is None:
        return
    for bound in (value.lower, value.upper):
        number = bound.sign * bound.magnitude_numerator
        _signed(bound, number, denominator)
        _require(floor <= number <= ceiling)
    _require(
        value.lower.sign * value.lower.magnitude_numerator
        <= value.upper.sign * value.upper.magnitude_numerator
    )


def _quantiles(value: Quantiles, *, count: int, success: bool, ceiling: int) -> None:
    _require(value.count == count)
    expected = (
        "NOT_A_SUCCESS_LATENCY_POPULATION"
        if not success
        else "DESCRIPTIVE_ONLY"
        if count >= 1000
        else "BELOW_REPORTING_FLOOR"
    )
    _require(value.p99_status == expected)
    _require((value.p99 is not None) == (success and count >= 1000))
    _require(
        all(
            (item is not None) == bool(count)
            for item in (value.p50, value.p90, value.p95)
        )
    )
    numbers = [
        item
        for item in (value.p50, value.p90, value.p95, value.p99)
        if item is not None
    ]
    _require(numbers == sorted(numbers) and all(item <= ceiling for item in numbers))
    ranked = [(percent, getattr(value, f"p{percent}")) for percent in (50, 90, 95, 99)]
    for left_rank, left in ranked:
        for right_rank, right in ranked:
            if (
                left is not None
                and right is not None
                and (
                    (left_rank * count + 99) // 100 == (right_rank * count + 99) // 100
                )
            ):
                _require(left == right)


def _usage(value: Usage, count: int) -> None:
    _require(
        value.population_count == count == value.reported_count + value.missing_count
    )
    for total in (
        value.prompt_tokens_total_decimal,
        value.completion_tokens_total_decimal,
    ):
        _require(int(total) <= value.reported_count * (2**53 - 1))


def _population(value: PopulationCounts) -> None:
    n, success = value.offered_count, value.successful_count
    _require(set(value.outcomes) == set(_OUTCOMES))
    _require(sum(row.count for row in value.outcomes.values()) == n)
    _require(value.outcomes["SUCCESS"].count == success)
    _require(value.cancelled or value.outcomes["CANCELLED"].count == 0)
    for row in value.outcomes.values():
        _ratio(row.offered_fraction, row.count, n)
    _require(success <= value.dispatched_count <= value.arrival_observed_count <= n)
    mandatory_dispatches = sum(
        row.count
        for name, row in value.outcomes.items()
        if name
        in (
            "SUCCESS",
            "HTTP_ERROR",
            "STREAM_ERROR",
            "STREAM_LIMIT",
            "INCOMPLETE_STREAM",
            "TRANSPORT_ERROR",
        )
    )
    _require(mandatory_dispatches <= value.dispatched_count)
    _require(not value.dispatched_count or value.peak_active > 0)
    _require(
        value.dispatched_count
        <= n
        - value.outcomes["REJECTED_ROUTE"].count
        - value.outcomes["REJECTED_CAPACITY"].count
    )
    _require(
        value.peak_active <= value.dispatched_count
        and value.peak_queue <= value.arrival_observed_count
    )
    _usage(value.usage_success, success)
    _usage(value.usage_other_outcomes, n - success)
    _require(
        int(value.usage_success.completion_tokens_total_decimal)
        >= value.usage_success.reported_count
    )
    if not isinstance(value, PopulationSummary):
        return
    window, good = value.fixed_offered_window_ns, value.slo_good_count
    _require(good <= success and value.successful_completions_during_drain <= success)
    _ratio(value.slo_success_fraction, good, n)
    _ratio(value.slo_goodput_rps, good * 1_000_000_000, window)
    _ratio(value.offered_rate_rps, n * 1_000_000_000, window)
    _ratio(value.dispatch_rate_rps, value.dispatched_count * 1_000_000_000, window)
    _quantiles(
        value.dispatch_lag_ns,
        count=value.dispatched_count,
        success=False,
        ceiling=value.elapsed_ns,
    )
    latency = value.successful_latency_ns
    _require(latency.count == success)
    series = (
        latency.scheduled_to_first_content,
        latency.scheduled_to_terminal,
        latency.dispatch_to_first_content,
        latency.dispatch_to_terminal,
    )
    for timing in series:
        _quantiles(timing, count=success, success=True, ceiling=value.elapsed_ns)
    for earlier, later in (
        (series[0], series[1]),
        (series[2], series[3]),
        (series[2], series[0]),
        (series[3], series[1]),
    ):
        for percentile in ("p50", "p90", "p95", "p99"):
            left, right = getattr(earlier, percentile), getattr(later, percentile)
            _require(left is None or (right is not None and left <= right))
    _require(set(value.partial_timing_by_outcome) == set(_OUTCOMES) - {"SUCCESS"})
    for outcome, partial in value.partial_timing_by_outcome.items():
        count = partial.scheduled_to_first_content_ns.count
        _require(count <= partial.offered_count == value.outcomes[outcome].count)
        _quantiles(
            partial.scheduled_to_first_content_ns,
            count=count,
            success=False,
            ceiling=value.elapsed_ns,
        )
        if outcome in ("REJECTED_ROUTE", "REJECTED_CAPACITY"):
            _require(count == 0)


def _recovery(trial: TrialSummary) -> None:
    value = trial.recovery
    healthy = trial.scenario == "HEALTHY"
    applicable = not healthy and trial.policy_id != POLICY_IDS[0]
    _require(
        value.applicability
        == (
            "NOT_APPLICABLE_SCENARIO"
            if healthy
            else "APPLICABLE"
            if applicable
            else "NOT_APPLICABLE_POLICY"
        )
    )
    _require((trial.target_endpoint_id is None) == healthy)
    _require((value.planned_restore_ns is None) == healthy)
    _require(
        value.fault_actual_overload
        == ("NOT_APPLICABLE" if healthy else "NOT_ESTABLISHED_BY_FAULT_SCHEDULE")
    )
    if healthy:
        _require(
            value.actual_restore_ns is None
            and value.background_active_at_restore is None
        )
    if value.actual_restore_ns is not None:
        _require(value.actual_restore_ns <= trial.trial_elapsed_ns)
    _require(
        (value.background_active_at_restore is None)
        == (value.actual_restore_ns is None)
    )
    for name in ("publication", "decision", "dispatch"):
        interval = getattr(value.intervals, name)
        used = not healthy and (name == "publication" or applicable)
        if not used:
            _require(
                interval.status == "NOT_APPLICABLE"
                and interval.duration_ns is None
                and interval.observation_horizon_ns is None
            )
        elif value.actual_restore_ns is None:
            _require(
                interval.status == "RESTORE_NOT_OBSERVED"
                and interval.duration_ns is None
                and interval.observation_horizon_ns is None
            )
        else:
            horizon = trial.trial_elapsed_ns - value.actual_restore_ns
            _require(interval.observation_horizon_ns == horizon)
            _require(interval.status in ("OBSERVED", "UNOBSERVED_OR_CENSORED"))
            _require(
                (interval.duration_ns is not None) == (interval.status == "OBSERVED")
            )
            if interval.duration_ns is not None:
                _require(interval.duration_ns <= horizon)


def _trial(trial: TrialSummary) -> None:
    _population(trial.foreground)
    _require(trial.window_start_ns < trial.window_end_ns)
    _require(trial.first_content_slo_ns <= trial.completion_slo_ns)
    _require(
        trial.foreground.fixed_offered_window_ns
        == trial.window_end_ns - trial.window_start_ns
    )
    _require(
        trial.window_start_ns <= trial.foreground.last_offer_ns < trial.window_end_ns
    )
    _require(trial.foreground_offered_count == trial.foreground.offered_count)
    # Planned duration is a reservation; actual elapsed can include scheduling
    # or cleanup overshoot. Elapsed time is never converted to a success claim.
    _require(trial.foreground.elapsed_ns <= trial.trial_elapsed_ns)
    _require(
        trial.worst_case_duration_ns >= trial.window_end_ns + trial.foreground.drain_ns
    )
    _require(trial.completion_slo_ns <= trial.foreground.drain_ns)
    _require((trial.background is None) == (trial.scenario == "HEALTHY"))
    _require(
        trial.background_offered_count
        == (trial.background.offered_count if trial.background else 0)
    )
    if trial.background is not None:
        _population(trial.background)
        _require(trial.background.elapsed_ns <= trial.trial_elapsed_ns)
    if trial.status == "COMPLETED":
        # The controller stops background load after foreground completion.
        _require(not trial.foreground.cancelled)
    if trial.status == "WARMUP_FAILED":
        _require(
            trial.foreground.dispatched_count == 0
            and (trial.background is None or trial.background.dispatched_count == 0)
        )
    _recovery(trial)


def _study(report: StudyReport) -> None:
    coverage, trials = report.coverage, report.trials
    _require(report.limitations == _STUDY_LIMITATIONS)
    _require(coverage.planned_trials % 4 == 0)
    _require(coverage.returned_trials == len(trials))
    _require(coverage.started_trials == len(trials) + coverage.aborted_trials)
    _require(
        coverage.planned_trials == coverage.started_trials + coverage.not_run_trials
    )
    statuses = Counter(trial.status for trial in trials)
    _require(
        coverage.completed_trial_results == statuses["COMPLETED"]
        and coverage.warmup_failed_results == statuses["WARMUP_FAILED"]
        and coverage.cancelled_results == statuses["CANCELLED"]
    )
    _require(statuses["WARMUP_FAILED"] + statuses["CANCELLED"] <= 1)
    if statuses["WARMUP_FAILED"] or statuses["CANCELLED"]:
        _require(coverage.aborted_trials == 0)
    _require(all(trial.status == "COMPLETED" for trial in trials[:-1]))
    _require(
        tuple(trial.trial_id for trial in trials)
        == tuple(f"trial-{i:04d}" for i in range(len(trials)))
    )
    _require(
        all(
            trial.block_id == trials[index - index % 4].block_id
            for index, trial in enumerate(trials)
        )
    )
    _require(len({trial.profile_id for trial in trials}) <= 16)
    _require(report.study_elapsed_ns >= sum(trial.trial_elapsed_ns for trial in trials))
    complete = report.status == "COMPLETED"
    _require((report.reason is None) == complete)
    if complete:
        _require(len(trials) == coverage.planned_trials == statuses["COMPLETED"])
    if report.status == "ABORTED":
        _require(report.reason not in ("CANCELLED", "STUDY_DEADLINE"))
    if report.reason == "WARMUP_FAILED":
        _require(statuses["WARMUP_FAILED"] == 1)
    _require(
        report.comparative_headline
        == (
            "DESCRIPTIVE_UNCALIBRATED_REHEARSAL"
            if complete
            else "SUPPRESSED_INCOMPLETE_STUDY"
        )
    )
    _require(all(trial.evidence_class == report.evidence_class for trial in trials))
    if not trials:
        _require(report.evidence_class == "LOCAL_MEASUREMENT_ONLY")
    _require(
        report.duplicate_payload_hash_groups
        == sum(
            count > 1
            for count in Counter(trial.result_sha256 for trial in trials).values()
        )
    )
    for population in ("foreground", "background"):
        offered = getattr(coverage, population)
        measured = sum(
            getattr(trial, population).offered_count
            if getattr(trial, population) is not None
            else 0
            for trial in trials
        )
        _require(offered.returned_records == measured)
        _require(
            offered.planned_offers
            == offered.returned_records
            + offered.offers_in_aborted_trials_without_measurements
            + offered.offers_in_not_run_trials
        )
        if coverage.aborted_trials == 0:
            _require(offered.offers_in_aborted_trials_without_measurements == 0)
        if coverage.not_run_trials == 0:
            _require(offered.offers_in_not_run_trials == 0)
    _require(
        coverage.foreground.planned_offers + coverage.background.planned_offers
        <= 100_000
    )
    _require(coverage.foreground.planned_offers >= coverage.planned_trials)
    for trial in trials:
        _trial(trial)
    if trials:
        cooldown = trials[0].cooldown_ns
        _require(all(trial.cooldown_ns == cooldown for trial in trials))
        _require(
            report.study_elapsed_ns
            >= sum(trial.trial_elapsed_ns for trial in trials)
            + max(0, coverage.started_trials - 1) * cooldown
        )
    strata_keys = [
        (row.scenario, row.profile_id, row.target_endpoint_id) for row in report.strata
    ]
    _require(len(set(strata_keys)) == len(strata_keys))
    _require(strata_keys == sorted(strata_keys, key=str))
    _require(
        sum(row.planned_blocks for row in report.strata) * 4 == coverage.planned_trials
    )
    _require(
        {
            (trial.scenario, trial.profile_id, trial.target_endpoint_id)
            for trial in trials
        }
        <= set(strata_keys)
    )
    blocks: dict[str, list[TrialSummary]] = {}
    for trial in trials:
        blocks.setdefault(trial.block_id, []).append(trial)
    _require(len(blocks) <= 64)
    for group in blocks.values():
        _require(len({trial.policy_id for trial in group}) == len(group) <= 4)
        first = group[0]
        for trial in group[1:]:
            for field in (
                "profile_id",
                "scenario",
                "target_endpoint_id",
                "repeat_index",
                "workload_seed",
                "order_seed",
                "workload_sha256",
                "window_start_ns",
                "window_end_ns",
                "first_content_slo_ns",
                "completion_slo_ns",
                "foreground_offered_count",
                "background_offered_count",
            ):
                _require(getattr(trial, field) == getattr(first, field))
    for stratum in report.strata:
        _study_stratum(report, stratum, blocks, complete)


def _study_stratum(
    report: StudyReport,
    stratum: StudyStratum,
    blocks: dict[str, list[TrialSummary]],
    complete: bool,
) -> None:
    _require((stratum.target_endpoint_id is None) == (stratum.scenario == "HEALTHY"))
    matching = [
        group
        for _, group in sorted(blocks.items())
        if (group[0].scenario, group[0].profile_id, group[0].target_endpoint_id)
        == (stratum.scenario, stratum.profile_id, stratum.target_endpoint_id)
    ]
    measured = [trial for group in matching for trial in group]
    full = [
        {trial.policy_id: trial for trial in group}
        for group in matching
        if len(group) == 4 and all(trial.status == "COMPLETED" for trial in group)
    ]
    _require(
        len(matching) <= stratum.planned_blocks and stratum.complete_blocks == len(full)
    )
    _require({group[0].repeat_index for group in matching} == set(range(len(matching))))
    _require(
        all(
            trial.foreground.fixed_offered_window_ns == stratum.fixed_window_ns
            for trial in measured
        )
    )
    _require(tuple(row.policy_id for row in stratum.policies) == POLICY_IDS)
    for row in stratum.policies:
        selected = [trial for trial in measured if trial.policy_id == row.policy_id]
        good = tuple(trial.foreground.slo_good_count for trial in selected)
        _require(
            row.slo_good_counts_by_trial == good and row.returned_trials == len(good)
        )
        _require(
            row.completed_trials
            == sum(trial.status == "COMPLETED" for trial in selected)
        )
        if not good:
            _require(
                row.mean_goodput_rps is None
                and row.observed_min_goodput_rps is None
                and row.observed_max_goodput_rps is None
            )
        else:
            _require(
                row.mean_goodput_rps is not None
                and row.observed_min_goodput_rps is not None
                and row.observed_max_goodput_rps is not None
            )
            assert (
                row.mean_goodput_rps
                and row.observed_min_goodput_rps
                and row.observed_max_goodput_rps
            )
            _ratio(
                row.mean_goodput_rps,
                sum(good) * 1_000_000_000,
                len(good) * stratum.fixed_window_ns,
            )
            _ratio(
                row.observed_min_goodput_rps,
                min(good) * 1_000_000_000,
                stratum.fixed_window_ns,
            )
            _ratio(
                row.observed_max_goodput_rps,
                max(good) * 1_000_000_000,
                stratum.fixed_window_ns,
            )
    _require(tuple(row.policy_id for row in stratum.paired_contrasts) == POLICY_IDS[1:])
    for contrast in stratum.paired_contrasts:
        _require(
            contrast.complete_blocks == len(full)
            and contrast.bootstrap_seed == report.reporting.bootstrap_seed
        )
        _require(
            contrast.interval_status
            == (
                "INCOMPLETE_STUDY"
                if not complete
                else "INSUFFICIENT_TRIAL_REPLICATION"
                if len(full) < 8
                else "AVAILABLE"
            )
        )
        differences = [
            block[contrast.policy_id].foreground.slo_good_count
            - block[POLICY_IDS[0]].foreground.slo_good_count
            for block in full
        ]
        denominator = len(full) * stratum.fixed_window_ns
        if complete:
            _signed(
                contrast.mean_goodput_difference_rps,
                sum(differences) * 1_000_000_000,
                denominator,
            )
        else:
            _require(contrast.mean_goodput_difference_rps is None)
        _interval(
            contrast.interval_rps,
            required=complete and len(full) >= 8,
            denominator=denominator,
            floor=min(differences, default=0) * len(full) * 1_000_000_000,
            ceiling=max(differences, default=0) * len(full) * 1_000_000_000,
        )
    for name in ("publication", "decision", "dispatch"):
        observed = Counter(
            getattr(trial.recovery.intervals, name).status for trial in measured
        )
        value = getattr(stratum.recovery_coverage, name)
        _require(
            value.observed_trials == observed["OBSERVED"]
            and value.not_applicable_trials == observed["NOT_APPLICABLE"]
        )
        _require(
            value.censored_or_restore_unobserved_trials
            == observed["RESTORE_NOT_OBSERVED"] + observed["UNOBSERVED_OR_CENSORED"]
        )
        _require(
            value.applicable_trials
            == value.observed_trials + value.censored_or_restore_unobserved_trials
        )


def _lengths(value: OutputLengths, usage: Usage) -> None:
    _require(
        value.population_count == usage.population_count
        and value.reported_count == usage.reported_count
        and value.missing_count == usage.missing_count
    )
    _require(value.total_decimal == usage.completion_tokens_total_decimal)
    _require(
        value.mean_decimal
        == (
            decimal_ratio(int(value.total_decimal), value.reported_count)
            if value.reported_count
            else None
        )
    )
    numbers = (value.p50, value.p90, value.p95)
    _require(all((item is not None) == bool(value.reported_count) for item in numbers))
    present = [item for item in numbers if item is not None]
    _require(
        present == sorted(present)
        and all(item <= int(value.total_decimal) for item in present)
    )


def _cache_cell(cell: CacheCell) -> None:
    _require(cell.cell_id == f"cell-{cell.index:04d}")
    _require(
        cell.workload_family
        == ("SHARED" if cell.condition.startswith("S") else "UNIQUE")
    )
    _require(cell.prefix_enabled == cell.condition.endswith("1"))
    _require(len(set(cell.attribution_reasons)) == len(cell.attribution_reasons))
    _require(cell.attribution_eligible == (not cell.attribution_reasons))
    returned = cell.status in ("COMPLETED", "CANCELLED")
    _require(
        all(
            (value is not None) == returned
            for value in (
                cell.population,
                cell.result_sha256,
                cell.endpoint_assignment,
                cell.reported_output_lengths,
            )
        )
    )
    if cell.status == "UNAVAILABLE":
        _require(cell.reason in ("MISSING_CELL_INPUT", "INVALID_CELL_INPUT"))
        _require(cell.attribution_reasons == (cell.reason,))
        _require(
            cell.cleanup
            == ("UNCONFIRMED" if cell.reason == "INVALID_CELL_INPUT" else "NOT_STARTED")
        )
        _require(
            cell.preparation_sha256 is None
            and cell.elapsed_ns is None
            and cell.evidence_class is None
        )
        return
    _require(
        cell.preparation_sha256 is not None
        and cell.elapsed_ns is not None
        and cell.evidence_class is not None
    )
    _require(
        not (
            {"MISSING_CELL_INPUT", "INVALID_CELL_INPUT"} & set(cell.attribution_reasons)
        )
    )
    if cell.status == "ABORTED":
        _require(
            cell.reason
            in ("CANCELLED", "EXECUTION_FAILED", "RESULT_LIMIT", "DURATION_LIMIT")
        )
        if cell.reason == "CANCELLED":
            _require(cell.cleanup == "UNCONFIRMED")
        if cell.reason == "RESULT_LIMIT":
            _require(cell.cleanup == "CONFIRMED_BY_LOCAL_RESULT")
        return
    _require(cell.reason == cell.status and cell.cleanup == "CONFIRMED_BY_LOCAL_RESULT")
    population = cell.population
    assert population is not None and cell.elapsed_ns is not None
    _population(population)
    _require(
        population.offered_count == cell.planned_offers
        and population.elapsed_ns <= cell.elapsed_ns
    )
    _require(population.cancelled == (cell.status == "CANCELLED"))
    _require(population.outcomes["REJECTED_ROUTE"].count == 0)
    assignment = cell.endpoint_assignment
    assert assignment is not None
    _require(
        tuple(row.endpoint_id for row in assignment) == ("endpoint-a", "endpoint-b")
    )
    _require(
        assignment[0].planned_offers == (cell.planned_offers + 1) // 2
        and assignment[1].planned_offers == cell.planned_offers // 2
    )
    _require(
        sum(row.actual_dispatches for row in assignment) == population.dispatched_count
    )
    _require(all(row.actual_dispatches <= row.planned_offers for row in assignment))
    lengths = cell.reported_output_lengths
    assert lengths is not None
    _lengths(lengths.success, population.usage_success)
    _lengths(lengths.other_outcomes, population.usage_other_outcomes)


def _cache(report: CacheReport) -> None:
    coverage, cells = report.coverage, report.cells
    _require(report.limitations == _CACHE_LIMITATIONS)
    _require(tuple(row.index for row in cells) == tuple(range(len(cells))))
    _require(len({row.attempt_id for row in cells}) == len(cells))
    _require(len({row.cell_sha256 for row in cells}) == len(cells))
    _require(
        report.reporting.first_content_slo_ns <= report.reporting.completion_slo_ns
    )
    for cell in cells:
        _cache_cell(cell)
        if cell.population is not None:
            _require(
                cell.population.fixed_offered_window_ns
                == report.reporting.fixed_offered_window_ns
            )
        if cell.status != "UNAVAILABLE":
            _require(
                ("TOKENIZATION_UNAVAILABLE" in cell.attribution_reasons)
                == (report.workload_verification == "UNAVAILABLE")
            )
    blocks: dict[str, dict[str, CacheCell]] = {}
    for cell in cells:
        block = blocks.setdefault(cell.block_id, {})
        _require(cell.condition not in block)
        block[cell.condition] = cell
    _require(
        all(
            cell.block_id == cells[index - index % 4].block_id
            for index, cell in enumerate(cells)
        )
    )
    _require(
        len(blocks) in (1, 4, 8)
        and all(set(block) == set(_CONDITIONS) for block in blocks.values())
    )
    for block in blocks.values():
        _require(len({cell.planned_offers for cell in block.values()}) == 1)
        for family in ("S", "U"):
            left, right = block[family + "0"], block[family + "1"]
            _require(
                left.config_sha256 == right.config_sha256
                and left.workload_sha256 == right.workload_sha256
            )
    full = [
        block
        for block in blocks.values()
        if all(cell.status == "COMPLETED" for cell in block.values())
    ]
    statuses = Counter(cell.status for cell in cells)
    complete = statuses["COMPLETED"] == len(cells)
    _require(report.status == ("COMPLETED" if complete else "INCOMPLETE"))
    _require(report.low_replication == (len(full) < 8))
    _require(
        coverage.planned_cells == len(cells)
        and coverage.planned_blocks == len(blocks)
        and coverage.complete_blocks == len(full)
    )
    _require(
        coverage.completed_cells == statuses["COMPLETED"]
        and coverage.cancelled_cells == statuses["CANCELLED"]
        and coverage.aborted_cells == statuses["ABORTED"]
    )
    _require(
        coverage.missing_cells
        == sum(cell.reason == "MISSING_CELL_INPUT" for cell in cells)
        and coverage.invalid_cells
        == sum(cell.reason == "INVALID_CELL_INPUT" for cell in cells)
    )
    _require(coverage.planned_offers == sum(cell.planned_offers for cell in cells))
    _require(
        coverage.returned_records
        == sum(
            cell.population.offered_count
            for cell in cells
            if cell.population is not None
        )
    )
    _require(
        coverage.offers_without_measurements
        == sum(cell.planned_offers for cell in cells if cell.population is None)
    )
    _require(
        report.sum_returned_owned_elapsed_ns
        == sum(cell.elapsed_ns for cell in cells if cell.elapsed_ns is not None)
    )
    classes = tuple(
        sorted(
            {cell.evidence_class for cell in cells if cell.evidence_class is not None}
        )
    )
    _require(
        report.evidence_classes == classes
        and report.evidence_class == (classes[0] if len(classes) == 1 else None)
    )
    _require(
        report.planned_evidence_class
        == (
            "SYNTHETIC_ONLY"
            if report.workload_verification == "SYNTHETIC_TOKENIZER"
            else "LOCAL_MEASUREMENT_ONLY"
        )
    )
    issues = report.preparation_consistency_issues
    _require(len(set(issues)) == len(issues))
    evidence_consistent = not classes or classes == (report.planned_evidence_class,)
    _require(("EVIDENCE_CLASS_MISMATCH" in issues) == (not evidence_consistent))
    _require(
        tuple(issue for issue in issues if issue != "EVIDENCE_CLASS_MISMATCH")
        == tuple(
            sorted(issue for issue in issues if issue != "EVIDENCE_CLASS_MISMATCH")
        )
    )
    available = (
        complete
        and not issues
        and all(cell.attribution_eligible for cell in cells)
        and evidence_consistent
    )
    _require(
        report.comparison_status
        == (
            "AVAILABLE"
            if available
            else "SUPPRESSED_INCOMPLETE"
            if not complete
            else "SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH"
        )
    )
    headline = (
        "SYNTHETIC_ONLY_DESCRIPTIVE_CONTRAST"
        if report.planned_evidence_class == "SYNTHETIC_ONLY"
        else "DECLARED_CONDITION_DESCRIPTIVE_CONTRAST"
    )
    _require(report.comparative_headline == (headline if available else "SUPPRESSED"))
    _require(report.comparison_block_ids == (tuple(blocks) if available else ()))
    _cache_prefix(report, blocks)
    _cache_contrasts(report, full, available)
    _cache_lengths(report, blocks, available)


def _cache_prefix(report: CacheReport, blocks: dict[str, dict[str, CacheCell]]) -> None:
    potential = report.prefix_potential
    _require((potential is None) == (report.workload_verification == "UNAVAILABLE"))
    if potential is None:
        return
    _require(tuple(row.block_id for row in potential) == tuple(blocks))
    size = report.reporting.cache_block_size
    for row in potential:
        _require(
            row.wrapper_overlap_tokens
            <= row.unique_max_pairwise_lcp_tokens
            < row.wrapper_overlap_tokens + size
        )
        _require(
            row.wrapper_overlap_tokens
            <= row.shared_document_lcp_tokens
            <= row.shared_lcp_tokens
        )
        _require(row.shared_complete_prefix_blocks == row.shared_lcp_tokens // size)
        _require(
            row.shared_document_complete_prefix_blocks
            == row.shared_document_lcp_tokens // size
        )
        _require(
            row.unique_max_complete_prefix_blocks
            == row.unique_max_pairwise_lcp_tokens // size
        )
        _require(
            row.extra_shared_prefix_blocks
            == row.shared_document_complete_prefix_blocks
            - max(
                row.unique_max_complete_prefix_blocks,
                row.wrapper_overlap_tokens // size,
            )
        )


def _cache_contrasts(
    report: CacheReport, full: list[dict[str, CacheCell]], available: bool
) -> None:
    _require(tuple(row.contrast for row in report.contrasts) == _CONTRASTS)
    window, n = report.reporting.fixed_offered_window_ns, len(full)
    differences: list[tuple[int, int, int]] = []
    for block in full:
        good = {
            key: cell.population.slo_good_count
            for key, cell in block.items()
            if cell.population is not None
        }
        shared, unique = good["S1"] - good["S0"], good["U1"] - good["U0"]
        differences.append((shared, unique, shared - unique))
    for index, contrast in enumerate(report.contrasts):
        _require(contrast.complete_blocks == n)
        _require(
            contrast.interval_status
            == (
                "SUPPRESSED_COMPARISON"
                if not available
                else "INSUFFICIENT_COMPLETE_BLOCKS"
                if n < 8
                else "DESCRIPTIVE_ONLY"
            )
        )
        values = [row[index] for row in differences]
        if available:
            _signed(
                contrast.mean_goodput_difference_rps,
                sum(values) * 1_000_000_000,
                n * window,
            )
            _require(
                contrast.block_goodput_differences_rps is not None
                and len(contrast.block_goodput_differences_rps) == n
            )
            assert contrast.block_goodput_differences_rps is not None
            for ratio, number in zip(
                contrast.block_goodput_differences_rps, values, strict=True
            ):
                _signed(ratio, number * 1_000_000_000, window)
        else:
            _require(
                contrast.mean_goodput_difference_rps is None
                and contrast.block_goodput_differences_rps is None
            )
        _interval(
            contrast.interval_rps,
            required=available and n >= 8,
            denominator=n * window,
            floor=min(values, default=0) * n * 1_000_000_000,
            ceiling=max(values, default=0) * n * 1_000_000_000,
        )


def _cache_lengths(
    report: CacheReport, blocks: dict[str, dict[str, CacheCell]], available: bool
) -> None:
    _require(
        tuple(row.block_id for row in report.output_length_differences) == tuple(blocks)
    )
    for difference in report.output_length_differences:
        block = blocks[difference.block_id]
        usage = {
            key: cell.reported_output_lengths.success
            for key, cell in block.items()
            if cell.reported_output_lengths is not None
        }
        covered = (
            available
            and len(usage) == 4
            and all(
                row.reported_count and not row.missing_count for row in usage.values()
            )
        )
        expected = (
            "SUPPRESSED_COMPARISON"
            if not available
            else "DESCRIPTIVE_SUCCESS_CONDITIONAL_NOT_MATCHED_OUTPUTS"
            if covered
            else "UNAVAILABLE_USAGE"
        )
        _require(difference.status == expected)
        for family, name in (("S", "shared"), ("U", "unique")):
            value = getattr(difference, name + "_mean_tokens_difference_decimal")
            if not covered:
                _require(value is None)
            else:
                off, on = usage[family + "0"], usage[family + "1"]
                numerator = (
                    int(on.total_decimal) * off.reported_count
                    - int(off.total_decimal) * on.reported_count
                )
                magnitude = decimal_ratio(
                    abs(numerator), off.reported_count * on.reported_count
                )
                _require(
                    value
                    == ("-" if numerator < 0 and magnitude != "0.000000" else "")
                    + magnitude
                )


def load_evaluation_report_bytes(
    content: bytes, *, kind: Literal["STUDY", "PREFIX_CACHE"]
) -> StudyReport | CacheReport:
    """Validate a bounded canonical report; never repair or replace its statistics."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    def reject_number(_number: str) -> Any:
        raise ValueError("unsupported number")

    try:
        _require(type(content) is bytes and 1 <= len(content) <= MAX_REPORT_BYTES)
        text = content.decode("utf-8")
        validate_json_structure(text, limits=REPORT_LIMITS)
        raw = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
        _require(canonical_json_bytes(raw) + b"\n" == content)
        if kind == "STUDY":
            study = StudyReport.model_validate_json(content)
            _study(study)
            return study
        _require(kind == "PREFIX_CACHE")
        cache = CacheReport.model_validate_json(content)
        _cache(cache)
        return cache
    except (
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
        ValidationError,
        AssertionError,
    ):
        raise EvaluationError(
            "evaluation report violates its bounded closed contract"
        ) from None
