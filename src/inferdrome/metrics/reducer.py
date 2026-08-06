"""Pure deterministic reduction from canonical request observations."""

from dataclasses import dataclass

from pydantic import BaseModel

from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_canonical_json,
)
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.metrics import (
    Aggregation,
    Measurement,
    Measurements,
    MetricDefinition,
    MetricDefinitions,
    MetricId,
    UnavailableMeasurement,
    UnavailableMetricId,
    UnavailableReason,
    frozen_metric_definitions_v1,
)
from inferdrome.domain.request_record import (
    FakeProducerSemantics,
    RequestRecord,
    RequestStatus,
    VllmProducerSemantics,
)
from inferdrome.errors import ReductionError
from inferdrome.metrics.quantiles import decimal_ratio, nearest_rank

REDUCER_VERSION = "1.0.0"
_NANOSECONDS_PER_SECOND = 1_000_000_000
_QUANTILE_PERCENTILES: dict[Aggregation, int] = {
    Aggregation.P50: 50,
    Aggregation.P95: 95,
    Aggregation.P99: 99,
}


@dataclass(frozen=True)
class ReductionResult:
    metric_definitions: MetricDefinitions
    metric_definitions_bytes: bytes
    metric_definitions_digest: str
    request_records_bytes: bytes
    execution_bytes: bytes
    measurements: Measurements
    measurements_bytes: bytes


def _canonical_model_bytes(model: BaseModel) -> bytes:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    return canonical_json_bytes(value)


def canonical_request_records_bytes(records: tuple[RequestRecord, ...]) -> bytes:
    return b"".join(_canonical_model_bytes(record) + b"\n" for record in records)


def _validate_population(
    execution: ExecutionRecord,
    records: tuple[RequestRecord, ...],
) -> None:
    expected_count = execution.configured_traffic.measured_requests
    if len(records) != expected_count:
        raise ReductionError(
            "canonical request-record count does not match configured measurement"
        )
    if not records:
        raise ReductionError("measurement population cannot be empty")

    first_producer = records[0].producer
    if execution.measurement_window_definition == "fake_measurement_window_v1":
        if not isinstance(first_producer, FakeProducerSemantics):
            raise ReductionError("fake execution requires synthetic records")
    elif not isinstance(first_producer, VllmProducerSemantics):
        raise ReductionError("vLLM execution requires vLLM records")
    for expected_index, record in enumerate(records):
        if record.run_id != execution.run_id:
            raise ReductionError("request record belongs to a different run")
        if record.sequence_index != expected_index:
            raise ReductionError("request records must be contiguous and ordered")
        if record.producer != first_producer:
            raise ReductionError("request records disagree on producer semantics")


def _definition_map(
    definitions: MetricDefinitions,
) -> dict[MetricId, MetricDefinition]:
    return {definition.metric: definition for definition in definitions.definitions}


def _measurement(
    definitions: dict[MetricId, MetricDefinition],
    *,
    metric: MetricId,
    aggregation: Aggregation,
    value: int | str,
    sample_count: int,
) -> Measurement:
    definition = definitions[metric]
    quantile_method = (
        definition.quantile_method if aggregation in _QUANTILE_PERCENTILES else None
    )
    return Measurement(
        metric=metric,
        aggregation=aggregation,
        value=value,
        unit=definition.unit,
        sample_count=sample_count,
        population=definition.population,
        definition_id=definition.definition_id,
        quantile_method=quantile_method,
        rounding_policy=definition.rounding_policy,
    )


def _latency_measurements(
    definitions: dict[MetricId, MetricDefinition],
    *,
    metric: MetricId,
    values: tuple[int, ...],
) -> list[Measurement]:
    if not values:
        return []
    measurements = [
        _measurement(
            definitions,
            metric=metric,
            aggregation=Aggregation.MEAN,
            value=decimal_ratio(sum(values), len(values)),
            sample_count=len(values),
        )
    ]
    for aggregation, percentile in _QUANTILE_PERCENTILES.items():
        measurements.append(
            _measurement(
                definitions,
                metric=metric,
                aggregation=aggregation,
                value=nearest_rank(values, percentile),
                sample_count=len(values),
            )
        )
    return measurements


def reduce_measurements(
    execution: ExecutionRecord,
    records: tuple[RequestRecord, ...],
    *,
    metric_definitions: MetricDefinitions | None = None,
) -> ReductionResult:
    """Reduce one ordered measured population without external state."""

    _validate_population(execution, records)
    expected_definitions = frozen_metric_definitions_v1()
    definitions = metric_definitions or expected_definitions
    if definitions != expected_definitions:
        raise ReductionError("metric definitions do not match the frozen v1 set")
    definition_by_metric = _definition_map(definitions)

    successful = tuple(
        record for record in records if record.outcome.status is RequestStatus.SUCCESS
    )
    failed = tuple(
        record
        for record in records
        if record.outcome.status is not RequestStatus.SUCCESS
    )
    ttfts = tuple(
        record.timing.ttft_ns
        for record in successful
        if record.timing.ttft_ns is not None
    )
    choices_spans = tuple(
        record.timing.ttft_ns + sum(record.timing.itl_ns)
        for record in successful
        if record.timing.ttft_ns is not None
    )

    measured_count = len(records)
    successful_count = len(successful)
    failed_count = len(failed)
    measurements = [
        _measurement(
            definition_by_metric,
            metric=MetricId.MEASURED_REQUEST_COUNT,
            aggregation=Aggregation.COUNT,
            value=measured_count,
            sample_count=measured_count,
        ),
        _measurement(
            definition_by_metric,
            metric=MetricId.SUCCESSFUL_REQUEST_COUNT,
            aggregation=Aggregation.COUNT,
            value=successful_count,
            sample_count=successful_count,
        ),
        _measurement(
            definition_by_metric,
            metric=MetricId.FAILED_REQUEST_COUNT,
            aggregation=Aggregation.COUNT,
            value=failed_count,
            sample_count=failed_count,
        ),
        _measurement(
            definition_by_metric,
            metric=MetricId.ERROR_RATE,
            aggregation=Aggregation.RATIO,
            value=decimal_ratio(failed_count, measured_count),
            sample_count=measured_count,
        ),
    ]
    measurements.extend(
        _latency_measurements(
            definition_by_metric,
            metric=MetricId.TTFT_NS,
            values=ttfts,
        )
    )
    measurements.extend(
        _latency_measurements(
            definition_by_metric,
            metric=MetricId.LAST_CHOICES_EVENT_SPAN_NS,
            values=choices_spans,
        )
    )

    window_ns = execution.measurement_window_ns
    measurements.extend(
        [
            _measurement(
                definition_by_metric,
                metric=MetricId.ATTEMPTED_REQUEST_THROUGHPUT,
                aggregation=Aggregation.RATE,
                value=decimal_ratio(
                    measured_count * _NANOSECONDS_PER_SECOND, window_ns
                ),
                sample_count=measured_count,
            ),
            _measurement(
                definition_by_metric,
                metric=MetricId.SUCCESSFUL_REQUEST_THROUGHPUT,
                aggregation=Aggregation.RATE,
                value=decimal_ratio(
                    successful_count * _NANOSECONDS_PER_SECOND, window_ns
                ),
                sample_count=successful_count,
            ),
            _measurement(
                definition_by_metric,
                metric=MetricId.OUTPUT_TOKEN_THROUGHPUT,
                aggregation=Aggregation.RATE,
                value=decimal_ratio(
                    sum(record.tokens.output_tokens for record in successful)
                    * _NANOSECONDS_PER_SECOND,
                    window_ns,
                ),
                sample_count=successful_count,
            ),
        ]
    )

    definitions_value = definitions.model_dump(
        mode="json", by_alias=True, exclude_none=False
    )
    definitions_bytes = canonical_json_bytes(definitions_value)
    definitions_digest = digest_canonical_json(
        DigestDomain.METRIC_DEFINITIONS,
        definitions_value,
    )
    records_bytes = canonical_request_records_bytes(records)
    execution_bytes = _canonical_model_bytes(execution)
    output = Measurements(
        schema_version="inferdrome.measurements.v1",
        run_id=execution.run_id,
        request_records_sha256=sha256_digest(records_bytes),
        execution_sha256=sha256_digest(execution_bytes),
        metric_definitions_digest=definitions_digest,
        reducer_version=REDUCER_VERSION,
        measurements=tuple(measurements),
        unavailable=tuple(
            UnavailableMeasurement(
                metric=metric,
                reason=UnavailableReason.SOURCE_OBSERVATION_UNAVAILABLE,
                capability_matrix="vllm-0.26.0",
            )
            for metric in UnavailableMetricId
        ),
    )
    return ReductionResult(
        metric_definitions=definitions,
        metric_definitions_bytes=definitions_bytes,
        metric_definitions_digest=definitions_digest,
        request_records_bytes=records_bytes,
        execution_bytes=execution_bytes,
        measurements=output,
        measurements_bytes=_canonical_model_bytes(output),
    )
