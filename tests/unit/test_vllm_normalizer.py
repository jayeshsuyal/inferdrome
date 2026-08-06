"""Pinned vLLM native output is normalized without guessed observations."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from inferdrome.domain.execution import PhaseTimingStatus
from inferdrome.domain.experiment import (
    CanonicalResponseContentPolicy,
    EvidenceConfig,
    NativeOutputSensitivity,
)
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.request_record import RequestStatus
from inferdrome.errors import NormalizationError
from inferdrome.metrics import reduce_measurements
from inferdrome.normalization import (
    build_vllm_execution_record,
    normalize_vllm_native,
    vllm_native_schema_fingerprint,
)
from inferdrome.resolution import ResolutionResult, resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
NATIVE_PATH = (
    REPOSITORY_ROOT
    / "spikes"
    / "vllm-0.26.0"
    / "fixtures"
    / "client-macos-empty"
    / "native"
    / "benchmark-result.json"
)
RUN_ID = "run-77777777777777777777777777777777"
EXPECTED_METADATA = {
    "inferdrome_producer_version": "0.26.0",
    "inferdrome_spike_id": "vllm-0.26.0-client-capability",
}
EXPECTED_TOKENIZER_ID = (
    "/private/tmp/inferdrome-capability-fixture-workspace-clean-2/"
    "spikes/vllm-0.26.0/tokenizer"
)
EXPECTED_FINGERPRINT = (
    "sha256:3a4fdee6fe9b45ce5b42c41fd3bfc6614245a36ecfe6f94de92b59717a136abb"
)


def _resolution() -> ResolutionResult:
    return resolve_experiment(FIXTURE_ROOT / "source.yaml", run_id=RUN_ID)


def _native_value() -> dict[str, Any]:
    value = json.loads(NATIVE_PATH.read_bytes())
    assert isinstance(value, dict)
    return value


def _encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def _normalize(content: bytes | None = None):  # type: ignore[no-untyped-def]
    resolution = _resolution()
    return normalize_vllm_native(
        content if content is not None else NATIVE_PATH.read_bytes(),
        resolution.resolved_spec,
        resolution.request_plan,
        expected_metadata=EXPECTED_METADATA,
        expected_tokenizer_id=EXPECTED_TOKENIZER_ID,
    )


def test_real_pinned_fixture_normalizes_exact_measured_rows() -> None:
    result = _normalize()

    assert result.native_schema_fingerprint == EXPECTED_FINGERPRINT
    assert result.measurement_window_ns == 140_038_542
    assert [record.outcome.status for record in result.request_records] == [
        RequestStatus.SUCCESS,
        RequestStatus.SUCCESS,
        RequestStatus.FAILED,
        RequestStatus.SUCCESS,
    ]
    assert [record.timing.start_offset_ns for record in result.request_records] == [
        0,
        52_541,
        71_276_250,
        72_513_083,
    ]
    assert [record.timing.ttft_ns for record in result.request_records] == [
        14_906_291,
        14_806_459,
        None,
        11_741_333,
    ]
    assert result.request_records[0].timing.itl_ns == (
        15_206_667,
        13_338_333,
        15_110_250,
    )
    failed = result.request_records[2]
    assert failed.outcome.producer_error == "Service Unavailable"
    assert failed.tokens.input_tokens == 14
    assert failed.tokens.output_tokens == 0
    assert failed.content.response_sha256 == sha256_digest(b"")
    assert all(
        record.native_source.array_index == record.sequence_index
        and record.native_source.artifact_path == "native/benchmark-result.json"
        and record.content.native_response_content_present
        for record in result.request_records
    )
    assert result.request_records_bytes.endswith(b"\n")
    assert result.request_records_bytes.count(b"\n") == 4


def test_native_fingerprint_is_frozen_for_vllm_0_26() -> None:
    assert vllm_native_schema_fingerprint() == EXPECTED_FINGERPRINT


def test_canonical_response_policy_does_not_change_native_evidence() -> None:
    resolution = _resolution()
    included_spec = resolution.resolved_spec.model_copy(
        update={
            "evidence": EvidenceConfig(
                native_output_sensitivity=NativeOutputSensitivity.RESPONSE_CONTENT,
                canonical_response_content=CanonicalResponseContentPolicy.INCLUDE,
                include_request_plan=True,
            )
        }
    )
    result = normalize_vllm_native(
        NATIVE_PATH.read_bytes(),
        included_spec,
        resolution.request_plan,
        expected_metadata=EXPECTED_METADATA,
        expected_tokenizer_id=EXPECTED_TOKENIZER_ID,
    )

    assert result.request_records[0].content.canonical_response_content == "alpha beta"
    assert result.request_records[2].content.canonical_response_content == ""


def test_execution_and_reducer_use_only_supported_native_observations() -> None:
    resolution = _resolution()
    native_bytes = NATIVE_PATH.read_bytes()
    normalized = _normalize(native_bytes)
    started_at = datetime(2026, 8, 5, 22, 18, tzinfo=UTC)
    execution = build_vllm_execution_record(
        normalized,
        resolution.request_plan,
        native_bytes,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
        producer_exit_status=0,
    )
    reduction = reduce_measurements(execution, normalized.request_records)
    measurements = {
        (item.metric.value, item.aggregation.value): item.value
        for item in reduction.measurements.measurements
    }

    assert execution.measurement_window_ns == 140_038_542
    assert execution.native_result_sha256 == sha256_digest(native_bytes)
    assert all(
        phase.timing_status is PhaseTimingStatus.UNAVAILABLE
        and phase.started_at is None
        and phase.ended_at is None
        for phase in execution.phases
    )
    assert measurements[("measured_request_count", "count")] == 4
    assert measurements[("successful_request_count", "count")] == 3
    assert measurements[("failed_request_count", "count")] == 1
    assert measurements[("error_rate", "ratio")] == "0.250000"
    assert measurements[("ttft_ns", "p95")] == 14_906_291
    assert measurements[("attempted_request_throughput_per_s", "rate")] == (
        "28.563565"
    )
    assert measurements[("successful_request_throughput_per_s", "rate")] == (
        "21.422674"
    )
    assert measurements[("output_token_throughput_per_s", "rate")] == (
        "42.845348"
    )


def _remove_required_field(value: dict[str, Any]) -> None:
    value.pop("ttfts")


def _add_unknown_field(value: dict[str, Any]) -> None:
    value["upstream_new_metric"] = 1


def _shorten_detailed_array(value: dict[str, Any]) -> None:
    value["start_times"].pop()


def _change_population_aggregate(value: dict[str, Any]) -> None:
    value["total_input_tokens"] = 13


def _move_start_outside_duration(value: dict[str, Any]) -> None:
    value["start_times"][3] = value["start_times"][0] + 1


def _make_success_semantics_ambiguous(value: dict[str, Any]) -> None:
    value["ttfts"][0] = 0


@pytest.mark.parametrize(
    "mutation",
    [
        _remove_required_field,
        _add_unknown_field,
        _shorten_detailed_array,
        _change_population_aggregate,
        _move_start_outside_duration,
        _make_success_semantics_ambiguous,
    ],
)
def test_changed_or_incoherent_native_shapes_fail_closed(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    value = _native_value()
    mutation(value)

    with pytest.raises(NormalizationError):
        _normalize(_encoded(value))


def test_invocation_echoes_are_required_for_normalization() -> None:
    resolution = _resolution()
    native_bytes = NATIVE_PATH.read_bytes()

    with pytest.raises(NormalizationError, match="metadata"):
        normalize_vllm_native(
            native_bytes,
            resolution.resolved_spec,
            resolution.request_plan,
            expected_metadata={"inferdrome_producer_version": "0.26.0"},
            expected_tokenizer_id=EXPECTED_TOKENIZER_ID,
        )
    with pytest.raises(NormalizationError, match="tokenizer"):
        normalize_vllm_native(
            native_bytes,
            resolution.resolved_spec,
            resolution.request_plan,
            expected_metadata=EXPECTED_METADATA,
            expected_tokenizer_id="/different/tokenizer",
        )


def test_duplicate_and_nonfinite_json_are_rejected_before_normalization() -> None:
    duplicate = b'{"date":"20260805-221836",' + NATIVE_PATH.read_bytes()[1:]
    nonfinite = NATIVE_PATH.read_bytes().replace(
        b'"duration": 0.14003854198381305',
        b'"duration": NaN',
    )

    with pytest.raises(NormalizationError, match="duplicate"):
        _normalize(duplicate)
    with pytest.raises(NormalizationError, match="non-finite"):
        _normalize(nonfinite)
