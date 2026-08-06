"""Committed fake artifacts round-trip through public models and reducer."""

import hashlib
from pathlib import Path

from inferdrome.adapters.fake import FakeNativeResult, fake_native_schema_fingerprint
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.metrics import Measurements, MetricDefinitions
from inferdrome.domain.request_record import RequestRecord
from inferdrome.metrics import reduce_measurements

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "fake" / "v1"


def test_fake_golden_artifacts_are_self_consistent() -> None:
    native_bytes = (FIXTURE_ROOT / "native-result.json").read_bytes()
    execution_bytes = (FIXTURE_ROOT / "execution.json").read_bytes()
    record_bytes = (FIXTURE_ROOT / "request-records.jsonl").read_bytes()
    definition_bytes = (FIXTURE_ROOT / "metric-definitions.json").read_bytes()
    measurement_bytes = (FIXTURE_ROOT / "measurements.json").read_bytes()

    native = FakeNativeResult.model_validate_json(native_bytes)
    execution = ExecutionRecord.model_validate_json(execution_bytes)
    records = tuple(
        RequestRecord.model_validate_json(line)
        for line in record_bytes.splitlines()
    )
    definitions = MetricDefinitions.model_validate_json(definition_bytes)
    measurements = Measurements.model_validate_json(measurement_bytes)

    assert native.evidence_eligibility == "SYNTHETIC_ONLY"
    assert len(native.rows) == len(records) == 2
    assert execution.native_result_sha256 == (
        f"sha256:{hashlib.sha256(native_bytes).hexdigest()}"
    )
    assert all(
        record.producer.native_schema_fingerprint
        == fake_native_schema_fingerprint()
        for record in records
    )

    recalculated = reduce_measurements(
        execution,
        records,
        metric_definitions=definitions,
    )
    assert recalculated.request_records_bytes == record_bytes
    assert recalculated.execution_bytes == execution_bytes
    assert recalculated.metric_definitions_bytes == definition_bytes
    assert recalculated.measurements == measurements
    assert recalculated.measurements_bytes == measurement_bytes


def test_fake_fixture_manifest_matches_exact_bytes() -> None:
    entries = (FIXTURE_ROOT / "MANIFEST.sha256").read_text(
        encoding="utf-8"
    ).splitlines()
    for entry in entries:
        expected_digest, filename = entry.split("  ", maxsplit=1)
        artifact_bytes = (FIXTURE_ROOT / filename).read_bytes()
        actual_digest = hashlib.sha256(artifact_bytes).hexdigest()
        assert actual_digest == expected_digest
