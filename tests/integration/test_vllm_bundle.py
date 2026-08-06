"""Pinned vLLM evidence seals only after independent native normalization."""

from pathlib import Path

from inferdrome.bundle import verify_bundle
from inferdrome.domain.evidence import (
    ArtifactRole,
    ArtifactSensitivity,
    EvidenceExecutionMode,
    VllmProducerDescriptor,
)
from inferdrome.domain.request_record import RequestStatus
from inferdrome.domain.states import EvidenceEligibility, RunState
from tests.conftest import SealedVllmFixture


def test_vllm_fixture_seals_through_full_offline_recalculation(
    sealed_vllm_bundle: SealedVllmFixture,
) -> None:
    fixture = sealed_vllm_bundle
    report = verify_bundle(
        fixture.sealed.path,
        expected_bundle_digest=fixture.sealed.bundle_digest,
    )

    assert report.bundle_digest == fixture.sealed.bundle_digest
    assert report.descriptor.execution_mode is EvidenceExecutionMode.ATTACHED_ENDPOINT
    assert report.descriptor.evidence_eligibility is EvidenceEligibility.INELIGIBLE
    assert isinstance(report.descriptor.producer, VllmProducerDescriptor)
    assert fixture.workspace.current_state().state is RunState.COMPLETE
    assert [
        record.outcome.status for record in fixture.normalization.request_records
    ] == [
        RequestStatus.SUCCESS,
        RequestStatus.SUCCESS,
        RequestStatus.FAILED,
        RequestStatus.SUCCESS,
    ]
    assert fixture.normalization.request_records[2].outcome.producer_error == (
        "Service Unavailable"
    )
    assert (
        fixture.sealed.path / "native" / "benchmark-result.json"
    ).read_bytes() == fixture.native_result_bytes


def test_vllm_response_bearing_artifacts_are_explicitly_classified(
    sealed_vllm_bundle: SealedVllmFixture,
) -> None:
    artifacts = {
        artifact.role: artifact
        for artifact in sealed_vllm_bundle.sealed.descriptor.artifacts
    }

    assert artifacts[ArtifactRole.NATIVE_RESULT].sensitivity is (
        ArtifactSensitivity.RESPONSE_CONTENT
    )
    assert artifacts[ArtifactRole.REQUEST_PLAN].sensitivity is (
        ArtifactSensitivity.PROMPT_CONTENT
    )
    assert artifacts[ArtifactRole.REQUEST_RECORDS].sensitivity is (
        ArtifactSensitivity.PUBLIC
    )
    assert not any(
        path.name.endswith(".tmp")
        for path in Path(sealed_vllm_bundle.sealed.path).rglob("*")
    )
