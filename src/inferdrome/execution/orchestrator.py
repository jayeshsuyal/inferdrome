"""End-to-end execution orchestration for the frozen Inferdrome v0.1 paths."""

import hmac
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from inferdrome.adapters.fake import (
    FAKE_ADAPTER_VERSION,
    FAKE_PRODUCER_VERSION,
    FakeAdapter,
    fake_native_schema_fingerprint,
)
from inferdrome.adapters.vllm_bench import (
    VllmInvocationPaths,
    build_vllm_invocation,
    execute_vllm_benchmark,
    preflight_attached_endpoint,
    probe_vllm_version,
)
from inferdrome.bundle import BundleMetadata, SealedBundle, seal_bundle
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.evidence import (
    ArtifactRole,
    DigestDomains,
    EvidenceExecutionMode,
    FakeProducerDescriptor,
    SensitivityDeclaration,
    VllmProducerDescriptor,
)
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    CanonicalResponseContentPolicy,
    FakeExecution,
    PromptContentPolicy,
    SyntheticTarget,
    VllmExecution,
)
from inferdrome.domain.ids import Sha256Digest
from inferdrome.domain.states import (
    EvidenceEligibility,
    RunState,
    is_terminal_run_state,
)
from inferdrome.environment_capture import (
    capture_attached_environment,
    capture_fake_environment,
    capture_managed_gpu_environment,
)
from inferdrome.errors import (
    AdapterError,
    CancellationRequested,
    InferdromeError,
    ResolutionError,
)
from inferdrome.execution.cancellation import CancellationToken
from inferdrome.execution.managed_vllm import ManagedVllmServer
from inferdrome.execution.subprocess_runner import ProcessCapture, ProcessTermination
from inferdrome.gpu_proof import LocalGpuProof, ManagedVllmConfig
from inferdrome.metrics import ReductionResult, reduce_measurements
from inferdrome.normalization import (
    build_vllm_execution_record,
    normalize_vllm_native,
)
from inferdrome.normalization.vllm_0_26 import (
    VLLM_ADAPTER_VERSION,
    VLLM_VERSION,
)
from inferdrome.resolution import (
    ResolutionResult,
    resolve_experiment,
    validate_resolution_result,
)
from inferdrome.workspace import RunWorkspace


@dataclass(frozen=True)
class RunResult:
    resolution: ResolutionResult
    workspace: RunWorkspace
    sealed_bundle: SealedBundle


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _write_capture_file(path: Path, content: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short diagnostic write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        raise AdapterError("vLLM diagnostic capture failed closed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path.absolute())
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _write_managed_server_capture(
    capture_directory: Path,
    capture: ProcessCapture,
) -> None:
    _write_capture_file(
        capture_directory / "server-stdout.log",
        capture.stdout,
    )
    _write_capture_file(
        capture_directory / "server-stderr.log",
        capture.stderr,
    )
    _write_capture_file(
        capture_directory / "server-exit-status.txt",
        f"{capture.exit_status}\n".encode(),
    )
    _write_capture_file(
        capture_directory / "server-termination.txt",
        f"{capture.termination.value}\n".encode(),
    )


def _sensitivity(resolution: ResolutionResult) -> SensitivityDeclaration:
    spec = resolution.resolved_spec
    return SensitivityDeclaration(
        prompt_content_in_request_plan=(
            spec.workload.prompt_content_policy is PromptContentPolicy.INCLUDE
        ),
        canonical_response_content_included=(
            spec.evidence.canonical_response_content
            is CanonicalResponseContentPolicy.INCLUDE
        ),
        native_response_content_present=True,
        secrets_permitted=False,
    )


def _digests(
    resolution: ResolutionResult,
    reduction: ReductionResult,
) -> DigestDomains:
    return DigestDomains(
        source_spec_digest=resolution.source_spec_digest,
        execution_fingerprint=resolution.execution_fingerprint,
        request_plan_digest=resolution.request_plan_digest,
        metric_definitions_digest=reduction.metric_definitions_digest,
        exitspec_contract_digest=(
            resolution.resolved_spec.links.exitspec_contract_digest
        ),
    )


def _run_fake(
    resolution: ResolutionResult,
    workspace: RunWorkspace,
    cancellation: CancellationToken,
) -> SealedBundle:
    spec = resolution.resolved_spec
    if not isinstance(spec.execution, FakeExecution) or not isinstance(
        spec.target, SyntheticTarget
    ):
        raise AdapterError("fake orchestration requires a synthetic experiment")
    if (
        spec.execution.producer_version != FAKE_PRODUCER_VERSION
        or spec.execution.adapter_version != FAKE_ADAPTER_VERSION
    ):
        raise AdapterError("fake execution uses an unsupported producer contract")

    cancellation.raise_if_requested()
    workspace.transition(RunState.PREFLIGHT)
    cancellation.raise_if_requested()
    workspace.transition(RunState.WARMUP)
    cancellation.raise_if_requested()
    workspace.transition(RunState.MEASURING)
    fake = FakeAdapter().execute(
        spec,
        resolution.request_plan,
        started_at=datetime.now(UTC),
    )
    cancellation.raise_if_requested()
    reduction = reduce_measurements(fake.execution, fake.request_records)
    environment = capture_fake_environment(
        run_id=resolution.run_id,
        target_model=spec.target.model,
        captured_at=fake.execution.ended_at,
    )
    workspace.transition(RunState.FINALIZING)

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
        ArtifactRole.PRODUCER_VERSION: f"{FAKE_PRODUCER_VERSION}\n".encode(),
        ArtifactRole.PRODUCER_EXIT_STATUS: b"0\n",
        ArtifactRole.NATIVE_RESULT: fake.native_result_bytes,
        ArtifactRole.NATIVE_STDOUT: b"synthetic producer completed\n",
        ArtifactRole.NATIVE_STDERR: b"",
        ArtifactRole.REQUEST_RECORDS: fake.request_records_bytes,
        ArtifactRole.METRIC_DEFINITIONS: reduction.metric_definitions_bytes,
        ArtifactRole.MEASUREMENTS: reduction.measurements_bytes,
    }
    metadata = BundleMetadata(
        experiment_id=spec.experiment.id,
        created_at=fake.execution.ended_at,
        execution_mode=EvidenceExecutionMode.SYNTHETIC_FIXTURE,
        environment_completeness=environment.completeness,
        evidence_eligibility=EvidenceEligibility.SYNTHETIC_ONLY,
        replayability=resolution.request_plan.replayability,
        producer=FakeProducerDescriptor(
            name="inferdrome_fake",
            version=FAKE_PRODUCER_VERSION,
            adapter="fake",
            adapter_version=FAKE_ADAPTER_VERSION,
            native_schema_fingerprint=fake_native_schema_fingerprint(),
        ),
        digests=_digests(resolution, reduction),
        sensitivity=_sensitivity(resolution),
    )
    cancellation.raise_if_requested()
    return seal_bundle(workspace, metadata, payloads)


def _run_vllm(
    resolution: ResolutionResult,
    workspace: RunWorkspace,
    tokenizer_path: Path,
    cancellation: CancellationToken,
    managed_config: ManagedVllmConfig | None,
) -> SealedBundle:
    spec = resolution.resolved_spec
    if not isinstance(spec.execution, VllmExecution) or not isinstance(
        spec.target, AttachedVllmTarget
    ):
        raise AdapterError("vLLM orchestration requires an attached experiment")
    if (
        spec.execution.producer_version != VLLM_VERSION
        or spec.execution.adapter_version != VLLM_ADAPTER_VERSION
    ):
        raise AdapterError("vLLM execution uses an unsupported producer contract")

    cancellation.raise_if_requested()
    workspace.transition(RunState.PREFLIGHT)
    capture_directory = workspace.path / "native-capture"
    try:
        capture_directory.mkdir(mode=0o700)
    except OSError:
        raise AdapterError("vLLM capture directory could not be reserved") from None

    managed_server: ManagedVllmServer | None = None
    local_gpu_proof: LocalGpuProof | None = None
    managed_environment = None
    try:
        if managed_config is None:
            preflight = preflight_attached_endpoint(spec.target)
            version = probe_vllm_version(cwd=workspace.path)
        else:
            managed_server = ManagedVllmServer.start(
                spec,
                run_id=resolution.run_id,
                config=managed_config,
                tokenizer_path=tokenizer_path,
                cwd=workspace.path,
                cancellation=cancellation,
            )
            preflight, local_gpu_proof = managed_server.wait_until_ready()
            managed_environment = managed_server.process_environment
            version = probe_vllm_version(
                cwd=workspace.path,
                executable=(
                    local_gpu_proof.producer_distribution.executable_path
                ),
                environment=managed_environment,
            )
        cancellation.raise_if_requested()
        invocation = build_vllm_invocation(
            spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=(
                    workspace.input_directory / "workload.source.jsonl"
                ).absolute(),
                tokenizer_path=tokenizer_path.absolute(),
                result_directory=capture_directory.absolute(),
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=preflight,
            local_gpu_proof=local_gpu_proof,
            capability_profile_id=(
                managed_config.capability_profile_id
                if managed_config is not None
                else None
            ),
        )
        _write_capture_file(
            capture_directory / "invocation.json",
            invocation.evidence_bytes,
        )
        _write_capture_file(
            capture_directory / "producer-version.txt",
            version.process.stdout,
        )

        cancellation.raise_if_requested()
        workspace.transition(RunState.WARMUP)
        workspace.transition(RunState.MEASURING)
        capture = execute_vllm_benchmark(
            invocation,
            spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
            cancellation=cancellation,
            environment=managed_environment,
        )
        _write_capture_file(
            capture_directory / "stdout.log",
            capture.process.stdout,
        )
        _write_capture_file(
            capture_directory / "stderr.log",
            capture.process.stderr,
        )
        _write_capture_file(
            capture_directory / "exit-status.txt",
            f"{capture.process.exit_status}\n".encode(),
        )
        if managed_server is not None:
            managed_server.assert_running()
            managed_server.assert_inputs_unchanged()
            managed_server.assert_running()
    except (Exception, KeyboardInterrupt):
        if managed_server is not None:
            try:
                server_capture = managed_server.stop()
                _write_managed_server_capture(capture_directory, server_capture)
            except Exception:
                pass
        raise
    if managed_server is not None:
        server_capture = managed_server.stop()
        _write_managed_server_capture(capture_directory, server_capture)
        if server_capture.termination is not ProcessTermination.CANCELLED:
            raise AdapterError(
                "managed vLLM server exited before supervised shutdown"
            )
    if (
        capture.process.termination is not ProcessTermination.EXITED
        or capture.process.exit_status != 0
    ):
        raise AdapterError("vLLM benchmark did not complete successfully")
    native_bytes = capture.native_result_bytes
    if native_bytes is None:
        raise AdapterError("vLLM benchmark produced no native result")

    normalization = normalize_vllm_native(
        native_bytes,
        spec,
        resolution.request_plan,
        expected_metadata=invocation.metadata,
        expected_tokenizer_id=str(invocation.paths.tokenizer_path),
    )
    execution = build_vllm_execution_record(
        normalization,
        resolution.request_plan,
        native_bytes,
        started_at=capture.process.started_at,
        ended_at=capture.process.ended_at,
        producer_exit_status=capture.process.exit_status,
    )
    reduction = reduce_measurements(execution, normalization.request_records)
    if local_gpu_proof is None:
        environment = capture_attached_environment(
            spec,
            preflight,
            run_id=resolution.run_id,
            captured_at=capture.process.ended_at,
        )
        eligibility = EvidenceEligibility.INELIGIBLE
    else:
        environment = capture_managed_gpu_environment(
            spec,
            preflight,
            local_gpu_proof,
            run_id=resolution.run_id,
            captured_at=capture.process.ended_at,
        )
        eligibility = EvidenceEligibility.CUSTOMER_ELIGIBLE
    workspace.transition(RunState.FINALIZING)

    payloads = {
        ArtifactRole.ORIGINAL_SPEC: resolution.source_bytes,
        ArtifactRole.RESOLVED_SPEC: resolution.resolved_spec_bytes,
        ArtifactRole.REQUEST_PLAN: resolution.request_plan_bytes,
        ArtifactRole.ENVIRONMENT: _canonical_model_bytes(environment),
        ArtifactRole.EXECUTION: reduction.execution_bytes,
        ArtifactRole.PRODUCER_INVOCATION: invocation.evidence_bytes,
        ArtifactRole.PRODUCER_VERSION: version.process.stdout,
        ArtifactRole.PRODUCER_EXIT_STATUS: b"0\n",
        ArtifactRole.NATIVE_RESULT: native_bytes,
        ArtifactRole.NATIVE_STDOUT: capture.process.stdout,
        ArtifactRole.NATIVE_STDERR: capture.process.stderr,
        ArtifactRole.REQUEST_RECORDS: normalization.request_records_bytes,
        ArtifactRole.METRIC_DEFINITIONS: reduction.metric_definitions_bytes,
        ArtifactRole.MEASUREMENTS: reduction.measurements_bytes,
    }
    metadata = BundleMetadata(
        experiment_id=spec.experiment.id,
        created_at=capture.process.ended_at,
        execution_mode=EvidenceExecutionMode.ATTACHED_ENDPOINT,
        environment_completeness=environment.completeness,
        evidence_eligibility=eligibility,
        replayability=resolution.request_plan.replayability,
        producer=VllmProducerDescriptor(
            name="vllm",
            version=VLLM_VERSION,
            adapter="vllm_bench_serve",
            adapter_version=VLLM_ADAPTER_VERSION,
            native_schema_fingerprint=normalization.native_schema_fingerprint,
        ),
        digests=_digests(resolution, reduction),
        sensitivity=_sensitivity(resolution),
    )
    cancellation.raise_if_requested()
    return seal_bundle(workspace, metadata, payloads)


def _mark_terminal(workspace: RunWorkspace, target: RunState) -> None:
    try:
        current = workspace.current_state()
        if not is_terminal_run_state(current.state):
            workspace.transition(target)
    except InferdromeError:
        # Preserve the original execution failure. The workspace remains
        # crash-readable and its inability to transition is independently visible.
        return


def _require_expected_exitspec_contract_digest(
    resolution: ResolutionResult,
    expected_digest: str | None,
) -> None:
    """Require an explicit external contract link before reserving a run."""

    if expected_digest is None:
        return
    try:
        validated_digest = TypeAdapter(Sha256Digest).validate_python(
            expected_digest,
            strict=True,
        )
    except ValidationError:
        raise ResolutionError("expected ExitSpec contract digest is invalid") from None
    actual_digest = resolution.resolved_spec.links.exitspec_contract_digest
    if actual_digest is None or not hmac.compare_digest(
        actual_digest,
        validated_digest,
    ):
        raise ResolutionError(
            "resolved experiment does not carry the expected ExitSpec contract digest"
        )


def run_experiment(
    source_path: Path,
    *,
    runs_root: Path,
    run_id: str | None = None,
    strict: bool = True,
    tokenizer_path: Path | None = None,
    managed_vllm: ManagedVllmConfig | None = None,
    cancellation: CancellationToken | None = None,
    expected_exitspec_contract_digest: str | None = None,
) -> RunResult:
    """Resolve, execute, reduce, seal, and verify one Inferdrome run."""

    selected_cancellation = cancellation or CancellationToken()
    selected_cancellation.raise_if_requested()
    resolution = resolve_experiment(source_path, run_id=run_id, strict=strict)
    return run_resolved_experiment(
        resolution,
        runs_root=runs_root,
        tokenizer_path=tokenizer_path,
        managed_vllm=managed_vllm,
        cancellation=selected_cancellation,
        expected_exitspec_contract_digest=expected_exitspec_contract_digest,
    )


def run_resolved_experiment(
    resolution: ResolutionResult,
    *,
    runs_root: Path,
    tokenizer_path: Path | None = None,
    managed_vllm: ManagedVllmConfig | None = None,
    cancellation: CancellationToken | None = None,
    expected_exitspec_contract_digest: str | None = None,
) -> RunResult:
    """Execute one already-resolved input without rereading its source files."""

    selected_cancellation = cancellation or CancellationToken()
    selected_cancellation.raise_if_requested()
    validate_resolution_result(resolution)
    _require_expected_exitspec_contract_digest(
        resolution,
        expected_exitspec_contract_digest,
    )
    is_fake = isinstance(resolution.resolved_spec.execution, FakeExecution)
    if is_fake and tokenizer_path is not None:
        raise AdapterError("tokenizer path is only valid for attached vLLM execution")
    if is_fake and managed_vllm is not None:
        raise AdapterError("managed vLLM is only valid for attached execution")
    if not is_fake and tokenizer_path is None:
        raise AdapterError("attached vLLM execution requires a tokenizer directory")
    if not is_fake and tokenizer_path is not None and not _real_directory(
        tokenizer_path
    ):
        raise AdapterError("attached vLLM tokenizer path must be a real directory")
    if managed_vllm is not None and not _real_directory(managed_vllm.model_path):
        raise AdapterError("managed vLLM model path must be a real directory")
    workspace = RunWorkspace.reserve(runs_root, resolution)
    try:
        if is_fake:
            sealed = _run_fake(resolution, workspace, selected_cancellation)
        else:
            if tokenizer_path is None:
                raise AssertionError
            sealed = _run_vllm(
                resolution,
                workspace,
                tokenizer_path,
                selected_cancellation,
                managed_vllm,
            )
    except KeyboardInterrupt:
        _mark_terminal(workspace, RunState.INTERRUPTED)
        raise
    except Exception as error:
        interrupted = selected_cancellation.requested or isinstance(
            error, CancellationRequested
        )
        _mark_terminal(
            workspace,
            RunState.INTERRUPTED if interrupted else RunState.FAILED,
        )
        raise
    return RunResult(
        resolution=resolution,
        workspace=workspace,
        sealed_bundle=sealed,
    )
