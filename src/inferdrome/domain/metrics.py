"""Versioned metric definitions and deterministic measurement contracts."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import DecimalString, RunId, SemanticVersion, Sha256Digest


class MetricId(StrEnum):
    MEASURED_REQUEST_COUNT = "measured_request_count"
    SUCCESSFUL_REQUEST_COUNT = "successful_request_count"
    FAILED_REQUEST_COUNT = "failed_request_count"
    ERROR_RATE = "error_rate"
    TTFT_NS = "ttft_ns"
    LAST_CHOICES_EVENT_SPAN_NS = "last_choices_event_span_ns"
    ATTEMPTED_REQUEST_THROUGHPUT = "attempted_request_throughput_per_s"
    SUCCESSFUL_REQUEST_THROUGHPUT = "successful_request_throughput_per_s"
    OUTPUT_TOKEN_THROUGHPUT = "output_token_throughput_per_s"


class DefinitionId(StrEnum):
    MEASURED_REQUEST_COUNT_V1 = "measured_request_count_v1"
    SUCCESSFUL_REQUEST_COUNT_V1 = "successful_request_count_v1"
    FAILED_REQUEST_COUNT_V1 = "failed_request_count_v1"
    MEASURED_FAILURE_RATIO_V1 = "measured_failure_ratio_v1"
    VLLM_FIRST_CHOICES_EVENT_V0_26 = "vllm_first_choices_event_v0_26"
    LAST_CHOICES_EVENT_SPAN_V1 = "last_choices_event_span_v1"
    ATTEMPTED_REQUESTS_PER_WINDOW_SECOND_V1 = (
        "attempted_measured_requests_per_window_second_v1"
    )
    SUCCESSFUL_REQUESTS_PER_WINDOW_SECOND_V1 = (
        "successful_measured_requests_per_window_second_v1"
    )
    SUCCESSFUL_OUTPUT_TOKENS_PER_WINDOW_SECOND_V1 = (
        "successful_output_tokens_per_window_second_v1"
    )


class Population(StrEnum):
    ALL_MEASURED_REQUESTS = "all_measured_requests"
    SUCCESSFUL_MEASURED_REQUESTS = "successful_measured_requests"
    FAILED_MEASURED_REQUESTS = "failed_measured_requests"
    SUCCESSFUL_WITH_OBSERVED_TTFT = "successful_measured_requests_with_observed_ttft"


class Unit(StrEnum):
    COUNT = "count"
    RATIO = "ratio"
    NANOSECONDS = "ns"
    REQUESTS_PER_SECOND = "requests/s"
    TOKENS_PER_SECOND = "tokens/s"


class Aggregation(StrEnum):
    COUNT = "count"
    RATIO = "ratio"
    MEAN = "mean"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"
    RATE = "rate"


class QuantileMethod(StrEnum):
    NEAREST_RANK_V1 = "nearest_rank_v1"


class RoundingPolicy(StrEnum):
    NONE = "none"
    DECIMAL_HALF_EVEN_6_V1 = "decimal_half_even_6_v1"


class SourceObservation(StrEnum):
    REQUEST_OUTCOME_STATUS = "request.outcome.status"
    REQUEST_OUTPUT_TOKENS = "request.tokens.output_tokens"
    REQUEST_TTFT_NS = "request.timing.ttft_ns"
    REQUEST_ITL_NS = "request.timing.itl_ns"
    EXECUTION_MEASUREMENT_WINDOW_NS = "execution.measurement_window_ns"


@dataclass(frozen=True)
class _MetricSpec:
    definition_id: DefinitionId
    unit: Unit
    population: Population
    allowed_aggregations: tuple[Aggregation, ...]
    required_observations: tuple[SourceObservation, ...]
    quantile_method: QuantileMethod | None
    rounding_policy: RoundingPolicy


_METRIC_SPECS: dict[MetricId, _MetricSpec] = {
    MetricId.MEASURED_REQUEST_COUNT: _MetricSpec(
        definition_id=DefinitionId.MEASURED_REQUEST_COUNT_V1,
        unit=Unit.COUNT,
        population=Population.ALL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.COUNT,),
        required_observations=(SourceObservation.REQUEST_OUTCOME_STATUS,),
        quantile_method=None,
        rounding_policy=RoundingPolicy.NONE,
    ),
    MetricId.SUCCESSFUL_REQUEST_COUNT: _MetricSpec(
        definition_id=DefinitionId.SUCCESSFUL_REQUEST_COUNT_V1,
        unit=Unit.COUNT,
        population=Population.SUCCESSFUL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.COUNT,),
        required_observations=(SourceObservation.REQUEST_OUTCOME_STATUS,),
        quantile_method=None,
        rounding_policy=RoundingPolicy.NONE,
    ),
    MetricId.FAILED_REQUEST_COUNT: _MetricSpec(
        definition_id=DefinitionId.FAILED_REQUEST_COUNT_V1,
        unit=Unit.COUNT,
        population=Population.FAILED_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.COUNT,),
        required_observations=(SourceObservation.REQUEST_OUTCOME_STATUS,),
        quantile_method=None,
        rounding_policy=RoundingPolicy.NONE,
    ),
    MetricId.ERROR_RATE: _MetricSpec(
        definition_id=DefinitionId.MEASURED_FAILURE_RATIO_V1,
        unit=Unit.RATIO,
        population=Population.ALL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.RATIO,),
        required_observations=(SourceObservation.REQUEST_OUTCOME_STATUS,),
        quantile_method=None,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
    MetricId.TTFT_NS: _MetricSpec(
        definition_id=DefinitionId.VLLM_FIRST_CHOICES_EVENT_V0_26,
        unit=Unit.NANOSECONDS,
        population=Population.SUCCESSFUL_WITH_OBSERVED_TTFT,
        allowed_aggregations=(
            Aggregation.MEAN,
            Aggregation.P50,
            Aggregation.P95,
            Aggregation.P99,
        ),
        required_observations=(SourceObservation.REQUEST_TTFT_NS,),
        quantile_method=QuantileMethod.NEAREST_RANK_V1,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
    MetricId.LAST_CHOICES_EVENT_SPAN_NS: _MetricSpec(
        definition_id=DefinitionId.LAST_CHOICES_EVENT_SPAN_V1,
        unit=Unit.NANOSECONDS,
        population=Population.SUCCESSFUL_WITH_OBSERVED_TTFT,
        allowed_aggregations=(
            Aggregation.MEAN,
            Aggregation.P50,
            Aggregation.P95,
            Aggregation.P99,
        ),
        required_observations=(
            SourceObservation.REQUEST_TTFT_NS,
            SourceObservation.REQUEST_ITL_NS,
        ),
        quantile_method=QuantileMethod.NEAREST_RANK_V1,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
    MetricId.ATTEMPTED_REQUEST_THROUGHPUT: _MetricSpec(
        definition_id=DefinitionId.ATTEMPTED_REQUESTS_PER_WINDOW_SECOND_V1,
        unit=Unit.REQUESTS_PER_SECOND,
        population=Population.ALL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.RATE,),
        required_observations=(
            SourceObservation.REQUEST_OUTCOME_STATUS,
            SourceObservation.EXECUTION_MEASUREMENT_WINDOW_NS,
        ),
        quantile_method=None,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
    MetricId.SUCCESSFUL_REQUEST_THROUGHPUT: _MetricSpec(
        definition_id=DefinitionId.SUCCESSFUL_REQUESTS_PER_WINDOW_SECOND_V1,
        unit=Unit.REQUESTS_PER_SECOND,
        population=Population.SUCCESSFUL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.RATE,),
        required_observations=(
            SourceObservation.REQUEST_OUTCOME_STATUS,
            SourceObservation.EXECUTION_MEASUREMENT_WINDOW_NS,
        ),
        quantile_method=None,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
    MetricId.OUTPUT_TOKEN_THROUGHPUT: _MetricSpec(
        definition_id=DefinitionId.SUCCESSFUL_OUTPUT_TOKENS_PER_WINDOW_SECOND_V1,
        unit=Unit.TOKENS_PER_SECOND,
        population=Population.SUCCESSFUL_MEASURED_REQUESTS,
        allowed_aggregations=(Aggregation.RATE,),
        required_observations=(
            SourceObservation.REQUEST_OUTPUT_TOKENS,
            SourceObservation.REQUEST_OUTCOME_STATUS,
            SourceObservation.EXECUTION_MEASUREMENT_WINDOW_NS,
        ),
        quantile_method=None,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    ),
}


class MetricDefinition(FrozenModel):
    metric: MetricId
    definition_id: DefinitionId
    unit: Unit
    population: Population
    allowed_aggregations: Annotated[tuple[Aggregation, ...], Field(min_length=1)]
    required_observations: Annotated[tuple[SourceObservation, ...], Field(min_length=1)]
    quantile_method: QuantileMethod | None
    rounding_policy: RoundingPolicy

    @model_validator(mode="after")
    def semantics_must_match_frozen_metric(self) -> "MetricDefinition":
        expected = _METRIC_SPECS[self.metric]
        actual = _MetricSpec(
            definition_id=self.definition_id,
            unit=self.unit,
            population=self.population,
            allowed_aggregations=self.allowed_aggregations,
            required_observations=self.required_observations,
            quantile_method=self.quantile_method,
            rounding_policy=self.rounding_policy,
        )
        if actual != expected:
            raise ValueError("metric definition does not match frozen v1 semantics")
        return self


class MetricDefinitions(FrozenModel):
    schema_version: Literal["inferdrome.metric-definitions.v1"]
    definition_set_version: Literal["1.0.0"]
    definitions: Annotated[tuple[MetricDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def definitions_must_cover_the_frozen_set(self) -> "MetricDefinitions":
        metrics = [definition.metric for definition in self.definitions]
        if metrics != list(MetricId):
            raise ValueError(
                "metric definitions must contain the ordered v1 metric set"
            )
        return self


def frozen_metric_definitions_v1() -> MetricDefinitions:
    """Build the one exact, ordered metric-definition artifact for v1."""

    definitions = tuple(
        MetricDefinition(
            metric=metric,
            definition_id=spec.definition_id,
            unit=spec.unit,
            population=spec.population,
            allowed_aggregations=spec.allowed_aggregations,
            required_observations=spec.required_observations,
            quantile_method=spec.quantile_method,
            rounding_policy=spec.rounding_policy,
        )
        for metric, spec in _METRIC_SPECS.items()
    )
    return MetricDefinitions(
        schema_version="inferdrome.metric-definitions.v1",
        definition_set_version="1.0.0",
        definitions=definitions,
    )


MeasurementValue = Annotated[int, Field(strict=True, ge=0)] | DecimalString


class Measurement(FrozenModel):
    metric: MetricId
    aggregation: Aggregation
    value: MeasurementValue
    unit: Unit
    sample_count: Annotated[int, Field(strict=True, ge=0)]
    population: Population
    definition_id: DefinitionId
    quantile_method: QuantileMethod | None
    rounding_policy: RoundingPolicy

    @model_validator(mode="after")
    def validate_frozen_metric_semantics(self) -> "Measurement":
        expected = _METRIC_SPECS[self.metric]
        if self.definition_id is not expected.definition_id:
            raise ValueError("measurement metric and definition ID do not match")
        if self.unit is not expected.unit:
            raise ValueError("measurement unit does not match metric definition")
        if self.population is not expected.population:
            raise ValueError("measurement population does not match metric definition")
        if self.rounding_policy is not expected.rounding_policy:
            raise ValueError(
                "measurement rounding policy does not match metric definition"
            )
        if self.aggregation not in expected.allowed_aggregations:
            raise ValueError("measurement aggregation is not allowed for metric")

        is_quantile = self.aggregation in {
            Aggregation.P50,
            Aggregation.P95,
            Aggregation.P99,
        }
        expected_quantile_method = expected.quantile_method if is_quantile else None
        if self.quantile_method is not expected_quantile_method:
            raise ValueError("measurement quantile method does not match aggregation")

        if self.aggregation is Aggregation.COUNT:
            if not isinstance(self.value, int):
                raise ValueError("count measurements require integer values")
            if self.value != self.sample_count:
                raise ValueError("count value must equal its population sample count")
        elif is_quantile:
            if not isinstance(self.value, int):
                raise ValueError("nearest-rank quantiles require integer values")
            if self.sample_count == 0:
                raise ValueError("quantiles require a non-empty population")
        elif not isinstance(self.value, str):
            raise ValueError("mean, ratio, and rate values require decimal strings")

        if self.metric is MetricId.ERROR_RATE and (
            not isinstance(self.value, str) or Decimal(self.value) > 1
        ):
            raise ValueError(
                "error rate must be a decimal string between zero and one"
            )
        return self


class UnavailableMetricId(StrEnum):
    FIRST_NONEMPTY_CONTENT_TTFT_NS = "first_nonempty_content_ttft_ns"
    TERMINAL_E2E_LATENCY_NS = "terminal_e2e_latency_ns"
    UPSTREAM_TPOT_NS = "upstream_tpot_ns"
    EXACT_ACHIEVED_CONCURRENCY = "exact_achieved_concurrency"
    SCHEDULED_OFFSET_NS = "scheduled_offset_ns"
    HTTP_STATUS = "http_status"
    FINISH_REASON = "finish_reason"


class UnavailableReason(StrEnum):
    SOURCE_OBSERVATION_UNAVAILABLE = "SOURCE_OBSERVATION_UNAVAILABLE"


class UnavailableMeasurement(FrozenModel):
    metric: UnavailableMetricId
    reason: Literal[UnavailableReason.SOURCE_OBSERVATION_UNAVAILABLE]
    capability_matrix: Literal["vllm-0.26.0"]


class Measurements(FrozenModel):
    schema_version: Literal["inferdrome.measurements.v1"]
    run_id: RunId
    request_records_sha256: Sha256Digest
    execution_sha256: Sha256Digest
    metric_definitions_digest: Sha256Digest
    reducer_version: SemanticVersion
    measurements: tuple[Measurement, ...]
    unavailable: tuple[UnavailableMeasurement, ...]

    @model_validator(mode="after")
    def validate_measurement_identity(self) -> "Measurements":
        keys = [
            (measurement.metric, measurement.aggregation)
            for measurement in self.measurements
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("measurement metric and aggregation pairs must be unique")
        unavailable_metrics = [item.metric for item in self.unavailable]
        if unavailable_metrics != list(UnavailableMetricId):
            raise ValueError("unavailable list must contain the ordered v1 exclusions")
        return self
