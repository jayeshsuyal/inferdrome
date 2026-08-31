"""Shared high-fidelity synthetic bundle fixture."""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inferdrome.adapters.fake import FakeAdapter, FakeRunResult
from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    VllmInvocation,
    VllmInvocationPaths,
    build_vllm_invocation,
    preflight_attached_endpoint,
)
from inferdrome.bundle import BundleMetadata, SealedBundle, seal_bundle
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.environment import (
    EnvironmentField,
    EnvironmentFieldName,
    EnvironmentManifest,
    ProvenanceKind,
)
from inferdrome.domain.evidence import (
    ArtifactRole,
    DigestDomains,
    EvidenceExecutionMode,
    FakeProducerDescriptor,
    SensitivityDeclaration,
    VllmProducerDescriptor,
)
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.domain.states import (
    EnvironmentCompleteness,
    EvidenceEligibility,
    RunState,
)
from inferdrome.metrics import ReductionResult, reduce_measurements
from inferdrome.normalization import (
    VllmNormalizationResult,
    build_vllm_execution_record,
    normalize_vllm_native,
)
from inferdrome.resolution import ResolutionResult, resolve_experiment
from inferdrome.workspace import RunWorkspace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SealedFakeFixture:
    workspace: RunWorkspace
    resolution: ResolutionResult
    fake: FakeRunResult
    reduction: ReductionResult
    environment: EnvironmentManifest
    sealed: SealedBundle


@dataclass(frozen=True)
class SealedVllmFixture:
    workspace: RunWorkspace
    resolution: ResolutionResult
    invocation: VllmInvocation
    native_result_bytes: bytes
    normalization: VllmNormalizationResult
    execution: ExecutionRecord
    reduction: ReductionResult
    environment: EnvironmentManifest
    sealed: SealedBundle


def _canonical_model_bytes(model: object) -> bytes:
    if not hasattr(model, "model_dump"):
        raise TypeError
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _fake_environment(run_id: str) -> EnvironmentManifest:
    fields = []
    for field_name in EnvironmentFieldName:
        value: str | int = "synthetic"
        if field_name is EnvironmentFieldName.GPU_COUNT:
            value = 0
        elif field_name is EnvironmentFieldName.PRODUCER_VERSION:
            value = "1.0.0"
        fields.append(
            EnvironmentField(
                name=field_name,
                value=value,
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            )
        )
    return EnvironmentManifest(
        schema_version="inferdrome.environment.v1",
        run_id=run_id,
        field_set_version="inferdrome.environment-fields.v1",
        captured_at=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        completeness=EnvironmentCompleteness.COMPLETE,
        fields=tuple(fields),
    )


def _vllm_environment(
    run_id: str,
    *,
    model_revision: str,
    tokenizer_revision: str,
    server_model_id: str,
) -> EnvironmentManifest:
    values: dict[EnvironmentFieldName, str | int] = {
        EnvironmentFieldName.CLIENT_OS: "fixture-macos",
        EnvironmentFieldName.CLIENT_ARCH: "fixture-arm64",
        EnvironmentFieldName.CLIENT_PYTHON_VERSION: "3.12.13",
        EnvironmentFieldName.PRODUCER_VERSION: "0.26.0",
        EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: (
            "fixture-source-pin-recorded"
        ),
        EnvironmentFieldName.TARGET_ENGINE_VERSION: "0.26.0",
        EnvironmentFieldName.TARGET_MODEL_REVISION: model_revision,
        EnvironmentFieldName.TARGET_TOKENIZER_REVISION: tokenizer_revision,
        EnvironmentFieldName.SERVER_MODEL_ID: server_model_id,
        EnvironmentFieldName.GPU_MODEL: "fixture-no-gpu",
        EnvironmentFieldName.GPU_COUNT: 0,
        EnvironmentFieldName.CUDA_VERSION: "not-applicable",
        EnvironmentFieldName.DRIVER_VERSION: "not-applicable",
    }
    fields = tuple(
        EnvironmentField(
            name=field_name,
            value=values[field_name],
            provenance=(
                ProvenanceKind.SERVER_REPORTED
                if field_name is EnvironmentFieldName.SERVER_MODEL_ID
                else ProvenanceKind.LOCALLY_VERIFIED
            ),
            evidence_path=(
                "native/producer-version.txt"
                if field_name is EnvironmentFieldName.PRODUCER_VERSION
                else "native/invocation.json"
                if field_name is EnvironmentFieldName.SERVER_MODEL_ID
                else None
            ),
        )
        for field_name in EnvironmentFieldName
    )
    return EnvironmentManifest(
        schema_version="inferdrome.environment.v1",
        run_id=run_id,
        field_set_version="inferdrome.environment-fields.v1",
        captured_at=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        completeness=EnvironmentCompleteness.COMPLETE,
        fields=fields,
    )


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root):
        current = Path(directory)
        current.chmod(0o700)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        for filename in filenames:
            (current / filename).chmod(0o600)


@pytest.fixture
def sealed_fake_bundle(tmp_path: Path) -> Iterator[SealedFakeFixture]:
    resolution = resolve_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        run_id="run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab",
    )
    workspace = RunWorkspace.reserve(tmp_path / "runs", resolution)
    for state in (
        RunState.PREFLIGHT,
        RunState.WARMUP,
        RunState.MEASURING,
        RunState.FINALIZING,
    ):
        workspace.transition(state)

    fake = FakeAdapter().execute(
        resolution.resolved_spec,
        resolution.request_plan,
        started_at=datetime(2026, 8, 5, 23, 45, tzinfo=UTC),
    )
    reduction = reduce_measurements(fake.execution, fake.request_records)
    environment = _fake_environment(resolution.run_id)
    invocation_bytes = canonical_json_bytes(
        {
            "argv": ["inferdrome_fake", "--run-id", resolution.run_id],
            "schema_version": "inferdrome.producer-invocation.v1",
        }
    )
    payloads = {
        ArtifactRole.ORIGINAL_SPEC: resolution.source_bytes,
        ArtifactRole.RESOLVED_SPEC: resolution.resolved_spec_bytes,
        ArtifactRole.REQUEST_PLAN: resolution.request_plan_bytes,
        ArtifactRole.ENVIRONMENT: _canonical_model_bytes(environment),
        ArtifactRole.EXECUTION: fake.execution_bytes,
        ArtifactRole.PRODUCER_INVOCATION: invocation_bytes,
        ArtifactRole.PRODUCER_VERSION: b"1.0.0\n",
        ArtifactRole.PRODUCER_EXIT_STATUS: b"0\n",
        ArtifactRole.NATIVE_RESULT: fake.native_result_bytes,
        ArtifactRole.NATIVE_STDOUT: b"synthetic producer completed\n",
        ArtifactRole.NATIVE_STDERR: b"",
        ArtifactRole.REQUEST_RECORDS: fake.request_records_bytes,
        ArtifactRole.METRIC_DEFINITIONS: reduction.metric_definitions_bytes,
        ArtifactRole.MEASUREMENTS: reduction.measurements_bytes,
    }
    metadata = BundleMetadata(
        experiment_id=resolution.resolved_spec.experiment.id,
        created_at=datetime(2026, 8, 6, 0, 1, tzinfo=UTC),
        execution_mode=EvidenceExecutionMode.SYNTHETIC_FIXTURE,
        environment_completeness=environment.completeness,
        evidence_eligibility=EvidenceEligibility.SYNTHETIC_ONLY,
        replayability=resolution.request_plan.replayability,
        producer=FakeProducerDescriptor(
            name="inferdrome_fake",
            version="1.0.0",
            adapter="fake",
            adapter_version="1.0.0",
            native_schema_fingerprint=fake.native_schema_fingerprint,
        ),
        digests=DigestDomains(
            source_spec_digest=resolution.source_spec_digest,
            execution_fingerprint=resolution.execution_fingerprint,
            request_plan_digest=resolution.request_plan_digest,
            metric_definitions_digest=reduction.metric_definitions_digest,
            exitspec_contract_digest=(
                resolution.resolved_spec.links.exitspec_contract_digest
            ),
        ),
        sensitivity=SensitivityDeclaration(
            prompt_content_in_request_plan=True,
            canonical_response_content_included=False,
            native_response_content_present=True,
            secrets_permitted=False,
        ),
    )
    sealed = seal_bundle(workspace, metadata, payloads)
    fixture = SealedFakeFixture(
        workspace=workspace,
        resolution=resolution,
        fake=fake,
        reduction=reduction,
        environment=environment,
        sealed=sealed,
    )
    try:
        yield fixture
    finally:
        _make_tree_writable(workspace.path)


@pytest.fixture
def sealed_vllm_bundle(tmp_path: Path) -> Iterator[SealedVllmFixture]:
    fixture_root = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
    spike_capture = (
        REPOSITORY_ROOT
        / "spikes"
        / "vllm-0.26.0"
        / "fixtures"
        / "client-macos-empty"
    )
    resolution = resolve_experiment(
        fixture_root / "source.yaml",
        run_id="run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    workspace = RunWorkspace.reserve(tmp_path / "runs", resolution)
    for state in (
        RunState.PREFLIGHT,
        RunState.WARMUP,
        RunState.MEASURING,
        RunState.FINALIZING,
    ):
        workspace.transition(state)

    capture_directory = workspace.path / "native-capture"
    capture_directory.mkdir(mode=0o700)
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    preflight_response = json.dumps(
        {"data": [{"id": target.model}]},
        separators=(",", ":"),
    ).encode("utf-8")
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=preflight_response,
        ),
    )
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        VllmInvocationPaths(
            dataset_path=(
                workspace.input_directory / "workload.source.jsonl"
            ).resolve(),
            tokenizer_path=(
                REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
            ).resolve(),
            result_directory=capture_directory.resolve(),
        ),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=preflight,
    )

    native_value = json.loads(
        (spike_capture / "native" / "benchmark-result.json").read_bytes()
    )
    for key in tuple(native_value):
        if key.startswith("inferdrome_"):
            native_value.pop(key)
    native_value.update(invocation.metadata)
    native_value["tokenizer_id"] = str(invocation.paths.tokenizer_path)
    native_result_bytes = json.dumps(
        native_value,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    normalization = normalize_vllm_native(
        native_result_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        expected_metadata=invocation.metadata,
        expected_tokenizer_id=str(invocation.paths.tokenizer_path),
    )
    execution = build_vllm_execution_record(
        normalization,
        resolution.request_plan,
        native_result_bytes,
        started_at=datetime(2026, 8, 6, 0, 45, tzinfo=UTC),
        ended_at=datetime(2026, 8, 6, 0, 46, tzinfo=UTC),
        producer_exit_status=0,
    )
    reduction = reduce_measurements(execution, normalization.request_records)
    assert target.model_revision is not None
    assert target.tokenizer_revision is not None
    environment = _vllm_environment(
        resolution.run_id,
        model_revision=target.model_revision,
        tokenizer_revision=target.tokenizer_revision,
        server_model_id=target.model,
    )
    payloads = {
        ArtifactRole.ORIGINAL_SPEC: resolution.source_bytes,
        ArtifactRole.RESOLVED_SPEC: resolution.resolved_spec_bytes,
        ArtifactRole.REQUEST_PLAN: resolution.request_plan_bytes,
        ArtifactRole.ENVIRONMENT: _canonical_model_bytes(environment),
        ArtifactRole.EXECUTION: reduction.execution_bytes,
        ArtifactRole.PRODUCER_INVOCATION: invocation.evidence_bytes,
        ArtifactRole.PRODUCER_VERSION: (
            spike_capture / "producer-version.txt"
        ).read_bytes(),
        ArtifactRole.PRODUCER_EXIT_STATUS: b"0\n",
        ArtifactRole.NATIVE_RESULT: native_result_bytes,
        ArtifactRole.NATIVE_STDOUT: (spike_capture / "stdout.log").read_bytes(),
        ArtifactRole.NATIVE_STDERR: (spike_capture / "stderr.log").read_bytes(),
        ArtifactRole.REQUEST_RECORDS: normalization.request_records_bytes,
        ArtifactRole.METRIC_DEFINITIONS: reduction.metric_definitions_bytes,
        ArtifactRole.MEASUREMENTS: reduction.measurements_bytes,
    }
    metadata = BundleMetadata(
        experiment_id=resolution.resolved_spec.experiment.id,
        created_at=datetime(2026, 8, 6, 1, 1, tzinfo=UTC),
        execution_mode=EvidenceExecutionMode.ATTACHED_ENDPOINT,
        environment_completeness=environment.completeness,
        evidence_eligibility=EvidenceEligibility.INELIGIBLE,
        replayability=resolution.request_plan.replayability,
        producer=VllmProducerDescriptor(
            name="vllm",
            version="0.26.0",
            adapter="vllm_bench_serve",
            adapter_version="1.0.0",
            native_schema_fingerprint=(
                normalization.native_schema_fingerprint
            ),
        ),
        digests=DigestDomains(
            source_spec_digest=resolution.source_spec_digest,
            execution_fingerprint=resolution.execution_fingerprint,
            request_plan_digest=resolution.request_plan_digest,
            metric_definitions_digest=reduction.metric_definitions_digest,
            exitspec_contract_digest=(
                resolution.resolved_spec.links.exitspec_contract_digest
            ),
        ),
        sensitivity=SensitivityDeclaration(
            prompt_content_in_request_plan=True,
            canonical_response_content_included=False,
            native_response_content_present=True,
            secrets_permitted=False,
        ),
    )
    sealed = seal_bundle(workspace, metadata, payloads)
    fixture = SealedVllmFixture(
        workspace=workspace,
        resolution=resolution,
        invocation=invocation,
        native_result_bytes=native_result_bytes,
        normalization=normalization,
        execution=execution,
        reduction=reduction,
        environment=environment,
        sealed=sealed,
    )
    try:
        yield fixture
    finally:
        _make_tree_writable(workspace.path)


@pytest.fixture
def emulated_customer_eligible_recalculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emulate an already-verified eligible descriptor for downstream gates."""

    from inferdrome.trials import service as trial_service

    original = trial_service.recalculate_bundle

    def recalculate(*args: object, **kwargs: object) -> object:
        analysis = original(*args, **kwargs)  # type: ignore[arg-type]
        descriptor = analysis.verification.descriptor.model_copy(
            update={"evidence_eligibility": EvidenceEligibility.CUSTOMER_ELIGIBLE}
        )
        return replace(
            analysis,
            verification=replace(
                analysis.verification,
                descriptor=descriptor,
            ),
        )

    monkeypatch.setattr(trial_service, "recalculate_bundle", recalculate)


@pytest.fixture
def emulated_ineligible_recalculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emulate an explicitly INELIGIBLE verifier result downstream."""

    from inferdrome.trials import service as trial_service

    original = trial_service.recalculate_bundle

    def recalculate(*args: object, **kwargs: object) -> object:
        analysis = original(*args, **kwargs)  # type: ignore[arg-type]
        descriptor = analysis.verification.descriptor.model_copy(
            update={"evidence_eligibility": EvidenceEligibility.INELIGIBLE}
        )
        return replace(
            analysis,
            verification=replace(
                analysis.verification,
                descriptor=descriptor,
            ),
        )

    monkeypatch.setattr(trial_service, "recalculate_bundle", recalculate)
