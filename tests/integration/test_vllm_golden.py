"""Committed vLLM 0.26.0 goldens fail loudly if normalization drifts."""

import hashlib
from pathlib import Path

from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.metrics import Measurements, MetricDefinitions
from inferdrome.domain.request_record import RequestRecord, RequestStatus
from inferdrome.metrics import reduce_measurements
from inferdrome.normalization import (
    build_vllm_execution_record,
    normalize_vllm_native,
)
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
GOLDEN_ROOT = FIXTURE_ROOT / "golden"
RUN_ID = "run-77777777777777777777777777777777"
EXPECTED_METADATA = {
    "inferdrome_producer_version": "0.26.0",
    "inferdrome_spike_id": "vllm-0.26.0-client-capability",
}
EXPECTED_TOKENIZER_ID = (
    "/private/tmp/inferdrome-capability-fixture-workspace-clean-2/"
    "spikes/vllm-0.26.0/tokenizer"
)


def test_vllm_golden_artifacts_reproduce_from_native_capture() -> None:
    resolution = resolve_experiment(FIXTURE_ROOT / "source.yaml", run_id=RUN_ID)
    native_bytes = (GOLDEN_ROOT / "native-result.json").read_bytes()
    record_bytes = (GOLDEN_ROOT / "request-records.jsonl").read_bytes()
    execution_bytes = (GOLDEN_ROOT / "execution.json").read_bytes()
    definition_bytes = (GOLDEN_ROOT / "metric-definitions.json").read_bytes()
    measurement_bytes = (GOLDEN_ROOT / "measurements.json").read_bytes()

    normalized = normalize_vllm_native(
        native_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        expected_metadata=EXPECTED_METADATA,
        expected_tokenizer_id=EXPECTED_TOKENIZER_ID,
    )
    records = tuple(
        RequestRecord.model_validate_json(line)
        for line in record_bytes.splitlines()
    )
    execution = ExecutionRecord.model_validate_json(execution_bytes)
    definitions = MetricDefinitions.model_validate_json(definition_bytes)
    measurements = Measurements.model_validate_json(measurement_bytes)
    rebuilt_execution = build_vllm_execution_record(
        normalized,
        resolution.request_plan,
        native_bytes,
        started_at=execution.started_at,
        ended_at=execution.ended_at,
        producer_exit_status=execution.producer_exit_status,
    )

    assert normalized.request_records == records
    assert normalized.request_records_bytes == record_bytes
    assert rebuilt_execution == execution
    assert [record.outcome.status for record in records] == [
        RequestStatus.SUCCESS,
        RequestStatus.SUCCESS,
        RequestStatus.FAILED,
        RequestStatus.SUCCESS,
    ]
    recalculated = reduce_measurements(
        execution,
        records,
        metric_definitions=definitions,
    )
    assert recalculated.execution_bytes == execution_bytes
    assert recalculated.metric_definitions_bytes == definition_bytes
    assert recalculated.measurements == measurements
    assert recalculated.measurements_bytes == measurement_bytes


def test_vllm_golden_manifest_and_source_capture_match_exact_bytes() -> None:
    source_capture = (
        REPOSITORY_ROOT
        / "spikes"
        / "vllm-0.26.0"
        / "fixtures"
        / "client-macos-empty"
        / "native"
        / "benchmark-result.json"
    )
    assert (GOLDEN_ROOT / "native-result.json").read_bytes() == (
        source_capture.read_bytes()
    )
    entries = (GOLDEN_ROOT / "MANIFEST.sha256").read_text(
        encoding="utf-8"
    ).splitlines()
    for entry in entries:
        expected_digest, filename = entry.split("  ", maxsplit=1)
        actual = hashlib.sha256((GOLDEN_ROOT / filename).read_bytes()).hexdigest()
        assert actual == expected_digest
