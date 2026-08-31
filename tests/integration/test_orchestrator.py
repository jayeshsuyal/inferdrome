"""Executable orchestration converges on one verified evidence format."""

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import inferdrome.execution.orchestrator as orchestrator
from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    VllmBenchmarkCapture,
    VllmVersionProbeCapture,
    preflight_attached_endpoint,
)
from inferdrome.bundle import (
    recalculate_bundle,
    verify_bundle,
    verify_bundle_matches_workspace,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.domain.states import (
    EnvironmentCompleteness,
    EvidenceEligibility,
    RunState,
)
from inferdrome.errors import (
    AdapterError,
    CancellationRequested,
    ResolutionError,
    VerificationError,
)
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
)
from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.execution.subprocess_runner import (
    ExecutableIdentity,
    ProcessCapture,
    ProcessTermination,
    resolve_executable_identity,
)
from inferdrome.gpu_proof import (
    MANAGED_PROCESS_ENVIRONMENT_OVERRIDES,
    MANAGED_PROCESS_ENVIRONMENT_POLICY,
    GpuComputeProcessEvidence,
    GpuDeviceEvidence,
    LocalGpuProof,
    ManagedServerEvidence,
    ManagedVllmConfig,
    VllmDistributionIdentity,
    build_managed_server_argv,
    gpu_compute_process_argv,
    gpu_inventory_argv,
)
from inferdrome.resolution import resolve_experiment
from inferdrome.workspace import RunWorkspace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_RUN_ID = "run-cccccccccccccccccccccccccccccccc"
VLLM_RUN_ID = "run-dddddddddddddddddddddddddddddddd"
FAILED_RUN_ID = "run-ffffffffffffffffffffffffffffffff"
INTERRUPTED_RUN_ID = "run-abababababababababababababababab"
MANAGED_RUN_ID = "run-34343434343434343434343434343434"
MANAGED_GPU_UUID = "GPU-bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def _fake_executable_identity(
    directory: Path,
    *,
    name: str = "vllm",
) -> ExecutableIdentity:
    executable = directory / name
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return resolve_executable_identity(str(executable))


def test_fake_orchestrator_executes_seals_and_verifies(tmp_path: Path) -> None:
    result = orchestrator.run_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        runs_root=tmp_path / "runs",
        run_id=FAKE_RUN_ID,
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.run_id == FAKE_RUN_ID
    assert report.descriptor.evidence_eligibility is (
        EvidenceEligibility.SYNTHETIC_ONLY
    )
    assert result.workspace.current_state().state is RunState.COMPLETE


def test_bundle_analysis_cannot_be_reused_for_a_different_workspace(
    tmp_path: Path,
) -> None:
    first = orchestrator.run_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        runs_root=tmp_path / "first-runs",
        run_id=FAKE_RUN_ID,
    )
    second = orchestrator.run_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        runs_root=tmp_path / "second-runs",
        run_id="run-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    second_analysis = recalculate_bundle(second.sealed_bundle.path)

    with pytest.raises(VerificationError, match="not attached"):
        verify_bundle_matches_workspace(first.workspace, second_analysis)


def test_bundle_must_match_exact_frozen_workspace_bytes(tmp_path: Path) -> None:
    result = orchestrator.run_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        runs_root=tmp_path / "runs",
        run_id=FAKE_RUN_ID,
    )
    analysis = recalculate_bundle(result.sealed_bundle.path)
    resolved_path = (
        result.workspace.path / "inputs" / "experiment.resolved.json"
    )
    changed_bytes = resolved_path.read_bytes() + b" "
    resolved_path.chmod(0o600)
    resolved_path.write_bytes(changed_bytes)
    resolved_path.chmod(0o400)

    metadata = result.workspace.metadata
    changed_inputs = tuple(
        descriptor.model_copy(
            update={
                "size_bytes": len(changed_bytes),
                "sha256": f"sha256:{hashlib.sha256(changed_bytes).hexdigest()}",
            }
        )
        if descriptor.path == "inputs/experiment.resolved.json"
        else descriptor
        for descriptor in metadata.frozen_inputs
    )
    changed_metadata = metadata.model_copy(
        update={"frozen_inputs": changed_inputs}
    )
    metadata_path = result.workspace.path / "control" / "resolution.json"
    metadata_path.chmod(0o600)
    metadata_path.write_bytes(
        canonical_json_bytes(
            changed_metadata.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
        )
    )
    metadata_path.chmod(0o400)

    changed_workspace = RunWorkspace.open(result.workspace.path)
    changed_workspace.verify_frozen_inputs()
    assert recalculate_bundle(result.sealed_bundle.path) == analysis
    with pytest.raises(
        VerificationError,
        match="bundle bytes disagree with frozen workspace inputs",
    ):
        verify_bundle_matches_workspace(changed_workspace, analysis)


def test_attached_vllm_orchestrator_preserves_real_shape_without_network_or_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
    source_path = fixture_root / "source.yaml"
    preview = resolve_experiment(source_path, run_id=VLLM_RUN_ID)
    target = preview.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    preflight_response = json.dumps(
        {"data": [{"id": target.model}]}, separators=(",", ":")
    ).encode()
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=preflight_response,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "preflight_attached_endpoint",
        lambda _target: preflight,
    )
    executable_identity = _fake_executable_identity(tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "resolve_executable_identity",
        lambda *_args, **_kwargs: executable_identity,
    )

    started_at = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    ended_at = datetime(2026, 8, 6, 2, 1, tzinfo=UTC)
    version_process = ProcessCapture(
        argv=("vllm", "--version"),
        started_at=started_at,
        ended_at=started_at,
        exit_status=0,
        termination=ProcessTermination.EXITED,
        stdout=b"0.26.0\n",
        stderr=b"",
    )
    probe_kwargs: dict[str, object] = {}

    def probe(**kwargs: object) -> VllmVersionProbeCapture:
        probe_kwargs.update(kwargs)
        return VllmVersionProbeCapture(
            process=version_process,
            observed_version="0.26.0",
        )

    monkeypatch.setattr(orchestrator, "probe_vllm_version", probe)

    spike_native_path = (
        REPOSITORY_ROOT
        / "spikes"
        / "vllm-0.26.0"
        / "fixtures"
        / "client-macos-empty"
        / "native"
        / "benchmark-result.json"
    )

    execute_kwargs: dict[str, object] = {}

    def execute(invocation: object, *_args: object, **kwargs: object) -> object:
        execute_kwargs.update(kwargs)
        metadata = dict(invocation.metadata)  # type: ignore[attr-defined]
        tokenizer_path = str(invocation.paths.tokenizer_path)  # type: ignore[attr-defined]
        native = json.loads(spike_native_path.read_bytes())
        for key in tuple(native):
            if key.startswith("inferdrome_"):
                native.pop(key)
        native.update(metadata)
        native["tokenizer_id"] = tokenizer_path
        native_bytes = json.dumps(
            native,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        process = ProcessCapture(
            argv=invocation.argv,  # type: ignore[attr-defined]
            started_at=started_at,
            ended_at=ended_at,
            exit_status=0,
            termination=ProcessTermination.EXITED,
            stdout=b"benchmark complete\n",
            stderr=b"",
        )
        return VllmBenchmarkCapture(
            invocation=invocation,  # type: ignore[arg-type]
            process=process,
            native_result_bytes=native_bytes,
        )

    monkeypatch.setattr(orchestrator, "execute_vllm_benchmark", execute)
    result = orchestrator.run_experiment(
        source_path,
        runs_root=tmp_path / "runs",
        run_id=VLLM_RUN_ID,
        tokenizer_path=(
            REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
        ),
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.descriptor.evidence_eligibility is EvidenceEligibility.INELIGIBLE
    assert report.descriptor.environment_completeness is (
        EnvironmentCompleteness.PARTIAL
    )
    assert result.workspace.current_state().state is RunState.COMPLETE
    capture_directory = result.workspace.path / "native-capture"
    assert (capture_directory / "invocation.json").read_bytes()
    assert (capture_directory / "producer-version.txt").read_bytes() == b"0.26.0\n"
    assert (capture_directory / "stdout.log").read_bytes() == (
        b"benchmark complete\n"
    )
    assert (capture_directory / "stderr.log").read_bytes() == b""
    assert (capture_directory / "exit-status.txt").read_bytes() == b"0\n"
    assert probe_kwargs["executable_identity"] is executable_identity
    assert execute_kwargs["executable_identity"] is executable_identity


def test_managed_vllm_orchestrator_seals_only_proof_backed_customer_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
    source_directory = tmp_path / "source"
    source_directory.mkdir()
    source_path = source_directory / "source.yaml"
    source_text = (fixture_root / "source.yaml").read_text()
    source_text = source_text.replace("fixture-model-revision", "a" * 40)
    source_text = source_text.replace("fixture-tokenizer-revision", "b" * 40)
    source_path.write_text(source_text)
    shutil.copyfile(
        fixture_root / "workload.jsonl",
        source_directory / "workload.jsonl",
    )
    preview = resolve_experiment(source_path, run_id=MANAGED_RUN_ID)
    target = preview.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    assert target.model_revision is not None
    assert target.tokenizer_revision is not None

    model_path = tmp_path / "model"
    model_path.mkdir()
    (model_path / "config.json").write_text("{}\n")
    tokenizer_path = REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
    executable_identity = _fake_executable_identity(tmp_path)
    model_snapshot = snapshot_directory_identity(
        model_path,
        kind="model",
        revision=target.model_revision,
    )
    tokenizer_snapshot = snapshot_directory_identity(
        tokenizer_path,
        kind="tokenizer",
        revision=target.tokenizer_revision,
    )
    distribution = VllmDistributionIdentity(
        name="vllm",
        version="0.26.0",
        sha256=f"sha256:{'4' * 64}",
        file_count=100,
        total_bytes=1_000_000,
        hash_policy="installed-wheel-files-v1",
        executable_path=str(executable_identity.path),
        executable_sha256=f"sha256:{'5' * 64}",
        source_wheel_filename=(
            "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl"
        ),
        source_wheel_path=(
            "/opt/inferdrome-gpu/downloads/"
            "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl"
        ),
        source_wheel_sha256=(
            "sha256:adb1e4c9b46d0dfdb094121ae5aad670"
            "a42412dd813ed4e5db069ed6a15006de"
        ),
    )
    selected = (0,)
    nvidia_smi_path = "/usr/bin/nvidia-smi"
    started_at = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
    ready_at = datetime(2026, 8, 6, 3, 1, tzinfo=UTC)
    proof = LocalGpuProof(
        schema_version="inferdrome.local-gpu-proof.v1",
        run_id=MANAGED_RUN_ID,
        capture_mode="managed_local_vllm",
        captured_at=ready_at,
        client_os="Linux",
        client_arch="x86_64",
        client_python_version="3.12.11",
        torch_version="2.9.0+cu130",
        cuda_runtime_version="13.0",
        torch_cuda_device_count=1,
        nvidia_smi_path=nvidia_smi_path,
        nvidia_smi_sha256=f"sha256:{'6' * 64}",
        gpu_query_argv=gpu_inventory_argv(nvidia_smi_path, selected),
        gpu_query_stdout=(
            f"0, NVIDIA L4, {MANAGED_GPU_UUID}, 580.65.06\n"
        ),
        selected_gpu_indices=selected,
        gpus=(
            GpuDeviceEvidence(
                index=0,
                model="NVIDIA L4",
                uuid=MANAGED_GPU_UUID,
                driver_version="580.65.06",
            ),
        ),
        producer_distribution=distribution,
        model_snapshot=model_snapshot,
        tokenizer_snapshot=tokenizer_snapshot,
        server=ManagedServerEvidence(
            argv=build_managed_server_argv(
                preview.resolved_spec,
                executable_path=distribution.executable_path,
                model_path=model_snapshot.root,
                tokenizer_path=tokenizer_snapshot.root,
                gpu_indices=selected,
            ),
            endpoint=str(target.endpoint).rstrip("/"),
            environment_policy=MANAGED_PROCESS_ENVIRONMENT_POLICY,
            environment_overrides=MANAGED_PROCESS_ENVIRONMENT_OVERRIDES,
            pid=5431,
            process_group_id=5431,
            started_at=started_at,
            ready_at=ready_at,
            compute_query_argv=gpu_compute_process_argv(nvidia_smi_path, (0,)),
            compute_query_stdout=f"5432, {MANAGED_GPU_UUID}\n",
            gpu_processes=(
                GpuComputeProcessEvidence(
                    pid=5432,
                    process_group_id=5431,
                    gpu_uuid=MANAGED_GPU_UUID,
                ),
            ),
        ),
    )
    preflight_response = json.dumps(
        {"data": [{"id": target.model}]}, separators=(",", ":")
    ).encode()
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=preflight_response,
        ),
    )

    class FakeManagedServer:
        @classmethod
        def start(cls, *_args: object, **_kwargs: object) -> object:
            return cls()

        def wait_until_ready(self) -> tuple[object, LocalGpuProof]:
            return preflight, proof

        @property
        def process_environment(self) -> dict[str, str]:
            return {
                item.partition("=")[0]: item.partition("=")[2]
                for item in MANAGED_PROCESS_ENVIRONMENT_OVERRIDES
            }

        @property
        def producer_executable_identity(self) -> ExecutableIdentity:
            return executable_identity

        def assert_running(self) -> None:
            return None

        def assert_inputs_unchanged(self) -> None:
            return None

        def stop(self) -> ProcessCapture:
            return ProcessCapture(
                argv=proof.server.argv,
                started_at=started_at,
                ended_at=ready_at,
                exit_status=-15,
                termination=ProcessTermination.CANCELLED,
                stdout=b"managed server ready\n",
                stderr=b"",
            )

    monkeypatch.setattr(orchestrator, "ManagedVllmServer", FakeManagedServer)
    version_process = ProcessCapture(
        argv=(distribution.executable_path, "--version"),
        started_at=started_at,
        ended_at=started_at,
        exit_status=0,
        termination=ProcessTermination.EXITED,
        stdout=b"0.26.0\n",
        stderr=b"",
    )
    probe_kwargs: dict[str, object] = {}

    def probe(**kwargs: object) -> VllmVersionProbeCapture:
        probe_kwargs.update(kwargs)
        return VllmVersionProbeCapture(
            process=version_process,
            observed_version="0.26.0",
        )

    monkeypatch.setattr(orchestrator, "probe_vllm_version", probe)
    spike_native_path = (
        REPOSITORY_ROOT
        / "spikes"
        / "vllm-0.26.0"
        / "fixtures"
        / "client-macos-empty"
        / "native"
        / "benchmark-result.json"
    )

    execute_kwargs: dict[str, object] = {}

    def execute(invocation: object, *_args: object, **kwargs: object) -> object:
        execute_kwargs.update(kwargs)
        metadata = dict(invocation.metadata)  # type: ignore[attr-defined]
        tokenizer = str(invocation.paths.tokenizer_path)  # type: ignore[attr-defined]
        native = json.loads(spike_native_path.read_bytes())
        for key in tuple(native):
            if key.startswith("inferdrome_"):
                native.pop(key)
        native.update(metadata)
        native["tokenizer_id"] = tokenizer
        native_bytes = json.dumps(
            native,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        process = ProcessCapture(
            argv=invocation.argv,  # type: ignore[attr-defined]
            started_at=ready_at,
            ended_at=datetime(2026, 8, 6, 3, 2, tzinfo=UTC),
            exit_status=0,
            termination=ProcessTermination.EXITED,
            stdout=b"benchmark complete\n",
            stderr=b"",
        )
        return VllmBenchmarkCapture(
            invocation=invocation,  # type: ignore[arg-type]
            process=process,
            native_result_bytes=native_bytes,
        )

    monkeypatch.setattr(orchestrator, "execute_vllm_benchmark", execute)
    result = orchestrator.run_experiment(
        source_path,
        runs_root=tmp_path / "runs",
        run_id=MANAGED_RUN_ID,
        tokenizer_path=tokenizer_path,
        managed_vllm=ManagedVllmConfig(model_path=model_path),
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.descriptor.evidence_eligibility is (
        EvidenceEligibility.CUSTOMER_ELIGIBLE
    )
    assert report.descriptor.environment_completeness is (
        EnvironmentCompleteness.COMPLETE
    )
    invocation = json.loads(
        (result.sealed_bundle.path / "native" / "invocation.json").read_bytes()
    )
    gpu_proof = invocation["local_gpu_proof"]
    assert gpu_proof["producer_distribution"]["source_wheel_sha256"] == (
        "sha256:adb1e4c9b46d0dfdb094121ae5aad670"
        "a42412dd813ed4e5db069ed6a15006de"
    )
    assert gpu_proof["server"]["argv"][1:3] == ["serve", str(model_path)]
    environment = json.loads(
        (result.sealed_bundle.path / "environment.json").read_bytes()
    )
    assert environment["completeness"] == "COMPLETE"
    assert all(field["value"] is not None for field in environment["fields"])
    capture_directory = result.workspace.path / "native-capture"
    assert (capture_directory / "server-stdout.log").read_bytes() == (
        b"managed server ready\n"
    )
    assert (capture_directory / "server-termination.txt").read_bytes() == (
        b"CANCELLED\n"
    )
    assert probe_kwargs["executable_identity"] is executable_identity
    assert execute_kwargs["executable_identity"] is executable_identity


def test_invalid_run_option_does_not_reserve_workspace(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    with pytest.raises(AdapterError, match="tokenizer path is only valid"):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=FAKE_RUN_ID,
            tokenizer_path=tmp_path,
        )

    assert not runs_root.exists()


def test_forged_resolution_is_rejected_before_workspace_reservation(
    tmp_path: Path,
) -> None:
    resolution = resolve_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        run_id=FAKE_RUN_ID,
    )
    forged = replace(
        resolution,
        request_plan_digest=f"sha256:{'f' * 64}",
    )
    runs_root = tmp_path / "runs"

    with pytest.raises(ResolutionError, match="internally inconsistent"):
        orchestrator.run_resolved_experiment(
            forged,
            runs_root=runs_root,
        )

    assert not runs_root.exists()


def test_attached_runtime_paths_are_checked_before_workspace_reservation(
    tmp_path: Path,
) -> None:
    source_path = (
        REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26" / "source.yaml"
    )
    runs_root = tmp_path / "runs"

    with pytest.raises(AdapterError, match="requires a tokenizer directory"):
        orchestrator.run_experiment(
            source_path,
            runs_root=runs_root,
            run_id=VLLM_RUN_ID,
        )
    assert not runs_root.exists()

    with pytest.raises(AdapterError, match="model path must be a real directory"):
        orchestrator.run_experiment(
            source_path,
            runs_root=runs_root,
            run_id=VLLM_RUN_ID,
            tokenizer_path=(
                REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
            ),
            managed_vllm=ManagedVllmConfig(
                model_path=tmp_path / "missing-model"
            ),
        )
    assert not runs_root.exists()


def test_execution_failure_terminalizes_reserved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_execution(*_args: object, **_kwargs: object) -> object:
        raise AdapterError("synthetic producer failed")

    monkeypatch.setattr(orchestrator.FakeAdapter, "execute", fail_execution)
    runs_root = tmp_path / "runs"
    with pytest.raises(AdapterError, match="synthetic producer failed"):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=FAILED_RUN_ID,
        )

    workspace = RunWorkspace.open(runs_root / FAILED_RUN_ID)
    assert workspace.current_state().state is RunState.FAILED
    assert not (workspace.path / "bundle").exists()


def test_cooperative_cancellation_terminalizes_workspace_as_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = CancellationToken()
    original_execute = orchestrator.FakeAdapter.execute

    def execute_and_cancel(*args: object, **kwargs: object) -> object:
        result = original_execute(*args, **kwargs)  # type: ignore[arg-type]
        cancellation.request(CancellationReason.USER)
        return result

    monkeypatch.setattr(orchestrator.FakeAdapter, "execute", execute_and_cancel)
    runs_root = tmp_path / "runs"
    with pytest.raises(CancellationRequested):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=INTERRUPTED_RUN_ID,
            cancellation=cancellation,
        )

    workspace = RunWorkspace.open(runs_root / INTERRUPTED_RUN_ID)
    assert workspace.current_state().state is RunState.INTERRUPTED
    assert not (workspace.path / "bundle").exists()
