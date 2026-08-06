"""Synthetic producer remains deterministic and unmistakably synthetic."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.adapters.fake import (
    FakeAdapter,
    FakeObservation,
    fake_native_schema_fingerprint,
)
from inferdrome.domain.experiment import (
    CanonicalResponseContentPolicy,
    EvidenceConfig,
    NativeOutputSensitivity,
)
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.request_record import RequestStatus
from inferdrome.errors import AdapterError
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
RUN_ID = "run-66666666666666666666666666666666"
STARTED_AT = datetime(2026, 8, 5, 22, 30, tzinfo=UTC)


def _resolved():  # type: ignore[no-untyped-def]
    return resolve_experiment(EXAMPLE, run_id=RUN_ID)


def test_default_fake_run_is_byte_deterministic() -> None:
    resolved = _resolved()

    first = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )
    second = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )

    assert first.native_result_bytes == second.native_result_bytes
    assert first.request_records_bytes == second.request_records_bytes
    assert first.execution_bytes == second.execution_bytes
    assert first.native_result.evidence_eligibility == "SYNTHETIC_ONLY"
    assert first.native_result.execution_mode == "synthetic_fixture"
    assert all(
        record.producer.producer_name == "inferdrome_fake"
        for record in first.request_records
    )
    assert first.execution.native_result_sha256 == sha256_digest(
        first.native_result_bytes
    )
    assert first.native_schema_fingerprint == fake_native_schema_fingerprint()


def test_custom_failure_and_empty_stream_are_preserved() -> None:
    resolved = _resolved()
    observations = (
        FakeObservation(
            status=RequestStatus.FAILED,
            input_tokens=4,
            output_tokens=0,
            start_offset_ns=0,
            ttft_ns=None,
            itl_ns=(),
            response_content=None,
            producer_error="synthetic failure",
        ),
        FakeObservation(
            status=RequestStatus.ANOMALOUS_EMPTY_STREAM,
            input_tokens=5,
            output_tokens=0,
            start_offset_ns=100,
            ttft_ns=None,
            itl_ns=(),
            response_content="",
            producer_error=None,
        ),
    )

    result = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        observations=observations,
        started_at=STARTED_AT,
    )

    assert [record.outcome.status for record in result.request_records] == [
        RequestStatus.FAILED,
        RequestStatus.ANOMALOUS_EMPTY_STREAM,
    ]
    assert result.request_records[0].outcome.producer_error == "synthetic failure"
    assert result.request_records[1].content.response_sha256 == sha256_digest(b"")


def test_canonical_response_policy_controls_only_canonical_copy() -> None:
    resolved = _resolved()
    included_spec = resolved.resolved_spec.model_copy(
        update={
            "evidence": EvidenceConfig(
                native_output_sensitivity=NativeOutputSensitivity.NON_SENSITIVE_FIXTURE,
                canonical_response_content=CanonicalResponseContentPolicy.INCLUDE,
                include_request_plan=True,
            )
        }
    )

    omitted = FakeAdapter().execute(
        resolved.resolved_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )
    included = FakeAdapter().execute(
        included_spec,
        resolved.request_plan,
        started_at=STARTED_AT,
    )

    assert omitted.request_records[0].content.canonical_response_content is None
    assert included.request_records[0].content.canonical_response_content is not None
    assert omitted.native_result.rows[0].response_content is not None


def test_fake_adapter_rejects_cardinality_and_window_mismatches() -> None:
    resolved = _resolved()
    one_observation = (
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=1,
            output_tokens=1,
            start_offset_ns=0,
            ttft_ns=10,
            itl_ns=(),
            response_content="ok",
            producer_error=None,
        ),
    )
    with pytest.raises(AdapterError, match="count"):
        FakeAdapter().execute(
            resolved.resolved_spec,
            resolved.request_plan,
            observations=one_observation,
        )
    with pytest.raises(AdapterError, match="window"):
        FakeAdapter().execute(
            resolved.resolved_spec,
            resolved.request_plan,
            measurement_window_ns=1,
        )
    with pytest.raises(AdapterError, match="positive integer"):
        FakeAdapter().execute(
            resolved.resolved_spec,
            resolved.request_plan,
            measurement_window_ns=1.5,  # type: ignore[arg-type]
        )


def test_invalid_fake_observation_fails_at_construction() -> None:
    with pytest.raises(ValidationError):
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=1,
            output_tokens=1,
            start_offset_ns=0,
            ttft_ns=None,
            itl_ns=(),
            response_content="impossible",
            producer_error=None,
        )
