"""Reducer populations, formulas, ordering, and byte determinism."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from inferdrome.adapters.fake import FakeAdapter, FakeObservation
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.metrics import Aggregation, MetricId
from inferdrome.domain.request_record import RequestStatus
from inferdrome.errors import ReductionError
from inferdrome.metrics.reducer import reduce_measurements
from inferdrome.resolution import resolve_experiment

RUN_ID = "run-77777777777777777777777777777777"
STARTED_AT = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)


def _fake_resolution(tmp_path: Path, request_count: int = 4):  # type: ignore[no-untyped-def]
    workload = b"".join(
        f'{{"prompt":"prompt {index}"}}\n'.encode()
        for index in range(request_count)
    )
    (tmp_path / "workload.jsonl").write_bytes(workload)
    source: dict[str, Any] = {
        "schema_version": "inferdrome.source-experiment.v1",
        "experiment": {"id": "reducer-test"},
        "execution": {"mode": "synthetic_fixture"},
        "target": {"engine": "fake"},
        "workload": {
            "path": "workload.jsonl",
            "sha256": sha256_digest(workload),
        },
        "traffic": {
            "kind": "concurrent",
            "measured_requests": request_count,
        },
    }
    source_path = tmp_path / "experiment.yaml"
    source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
    return resolve_experiment(source_path, run_id=RUN_ID)


def _measurement_map(result):  # type: ignore[no-untyped-def]
    return {
        (measurement.metric, measurement.aggregation): measurement
        for measurement in result.measurements.measurements
    }


def test_reducer_uses_explicit_success_and_failure_populations(tmp_path: Path) -> None:
    resolved = _fake_resolution(tmp_path)
    observations = (
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=5,
            output_tokens=2,
            start_offset_ns=0,
            ttft_ns=10,
            itl_ns=(20, 30),
            response_content="first",
            producer_error=None,
        ),
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=6,
            output_tokens=1,
            start_offset_ns=100,
            ttft_ns=20,
            itl_ns=(),
            response_content="second",
            producer_error=None,
        ),
        FakeObservation(
            status=RequestStatus.FAILED,
            input_tokens=7,
            output_tokens=0,
            start_offset_ns=200,
            ttft_ns=None,
            itl_ns=(),
            response_content=None,
            producer_error="synthetic failure",
        ),
        FakeObservation(
            status=RequestStatus.ANOMALOUS_EMPTY_STREAM,
            input_tokens=8,
            output_tokens=0,
            start_offset_ns=300,
            ttft_ns=None,
            itl_ns=(),
            response_content="",
            producer_error=None,
        ),
    )
    fake = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        observations=observations,
        started_at=STARTED_AT,
        measurement_window_ns=1_000_000_000,
    )

    reduced = reduce_measurements(fake.execution, fake.request_records)
    values = _measurement_map(reduced)

    assert values[(MetricId.MEASURED_REQUEST_COUNT, Aggregation.COUNT)].value == 4
    assert values[(MetricId.SUCCESSFUL_REQUEST_COUNT, Aggregation.COUNT)].value == 2
    assert values[(MetricId.FAILED_REQUEST_COUNT, Aggregation.COUNT)].value == 2
    assert values[(MetricId.ERROR_RATE, Aggregation.RATIO)].value == "0.500000"
    assert values[(MetricId.TTFT_NS, Aggregation.MEAN)].value == "15.000000"
    assert values[(MetricId.TTFT_NS, Aggregation.P50)].value == 10
    assert values[(MetricId.TTFT_NS, Aggregation.P95)].value == 20
    assert (
        values[(MetricId.LAST_CHOICES_EVENT_SPAN_NS, Aggregation.MEAN)].value
        == "40.000000"
    )
    assert (
        values[(MetricId.ATTEMPTED_REQUEST_THROUGHPUT, Aggregation.RATE)].value
        == "4.000000"
    )
    assert (
        values[(MetricId.SUCCESSFUL_REQUEST_THROUGHPUT, Aggregation.RATE)].value
        == "2.000000"
    )
    assert (
        values[(MetricId.OUTPUT_TOKEN_THROUGHPUT, Aggregation.RATE)].value
        == "3.000000"
    )
    assert len(reduced.measurements.unavailable) == 7
    assert reduced.measurements.request_records_sha256 == sha256_digest(
        fake.request_records_bytes
    )
    assert reduced.measurements.execution_sha256 == sha256_digest(
        fake.execution_bytes
    )


def test_identical_inputs_produce_byte_identical_measurements(tmp_path: Path) -> None:
    resolved = _fake_resolution(tmp_path, request_count=2)
    fake = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )

    first = reduce_measurements(fake.execution, fake.request_records)
    second = reduce_measurements(fake.execution, fake.request_records)

    assert first.metric_definitions_bytes == second.metric_definitions_bytes
    assert first.request_records_bytes == second.request_records_bytes
    assert first.measurements_bytes == second.measurements_bytes


def test_empty_success_population_omits_latency_not_zero(tmp_path: Path) -> None:
    resolved = _fake_resolution(tmp_path, request_count=2)
    failures = tuple(
        FakeObservation(
            status=RequestStatus.FAILED,
            input_tokens=4,
            output_tokens=0,
            start_offset_ns=index,
            ttft_ns=None,
            itl_ns=(),
            response_content=None,
            producer_error="failed",
        )
        for index in range(2)
    )
    fake = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        observations=failures,
        started_at=STARTED_AT,
    )

    reduced = reduce_measurements(fake.execution, fake.request_records)
    metrics = {measurement.metric for measurement in reduced.measurements.measurements}
    values = _measurement_map(reduced)

    assert MetricId.TTFT_NS not in metrics
    assert MetricId.LAST_CHOICES_EVENT_SPAN_NS not in metrics
    assert values[(MetricId.ERROR_RATE, Aggregation.RATIO)].value == "1.000000"
    assert (
        values[(MetricId.SUCCESSFUL_REQUEST_THROUGHPUT, Aggregation.RATE)].value
        == "0.000000"
    )
    assert (
        values[(MetricId.OUTPUT_TOKEN_THROUGHPUT, Aggregation.RATE)].value
        == "0.000000"
    )


def test_reducer_rejects_missing_reordered_and_cross_run_records(
    tmp_path: Path,
) -> None:
    resolved = _fake_resolution(tmp_path, request_count=2)
    fake = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )

    with pytest.raises(ReductionError, match="count"):
        reduce_measurements(fake.execution, fake.request_records[:1])
    with pytest.raises(ReductionError, match="ordered"):
        reduce_measurements(fake.execution, tuple(reversed(fake.request_records)))
    changed = fake.request_records[0].model_copy(
        update={"run_id": "run-88888888888888888888888888888888"}
    )
    with pytest.raises(ReductionError, match="different run"):
        reduce_measurements(
            fake.execution,
            (changed, fake.request_records[1]),
        )


def test_reducer_rejects_producer_and_window_semantics_mismatch(
    tmp_path: Path,
) -> None:
    resolved = _fake_resolution(tmp_path, request_count=2)
    fake = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )
    incompatible_execution = fake.execution.model_copy(
        update={"measurement_window_definition": "vllm_benchmark_duration_v0_26"}
    )

    with pytest.raises(ReductionError, match="vLLM records"):
        reduce_measurements(incompatible_execution, fake.request_records)
