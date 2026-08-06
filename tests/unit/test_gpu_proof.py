"""Managed real-GPU evidence stays exact, local, and independently replayable."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    VllmInvocationPaths,
    build_vllm_invocation,
    preflight_attached_endpoint,
    validate_vllm_invocation_evidence,
)
from inferdrome.domain.environment import EnvironmentFieldName, ProvenanceKind
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.domain.states import EnvironmentCompleteness
from inferdrome.environment_capture import capture_managed_gpu_environment
from inferdrome.errors import AdapterError
from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.gpu_proof import (
    MANAGED_PROCESS_ENVIRONMENT_OVERRIDES,
    MANAGED_PROCESS_ENVIRONMENT_POLICY,
    GpuComputeProcessEvidence,
    GpuDeviceEvidence,
    LocalGpuProof,
    ManagedServerEvidence,
    ManagedVllmConfig,
    SnapshotIdentity,
    VllmDistributionIdentity,
    build_managed_server_argv,
    gpu_compute_process_argv,
    gpu_inventory_argv,
    parse_gpu_compute_process_output,
    parse_gpu_inventory_output,
    validate_local_gpu_proof,
)
from inferdrome.resolution import ResolutionResult, resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "run-12121212121212121212121212121212"
GPU_UUID = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _resolution() -> ResolutionResult:
    return resolve_experiment(
        REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml",
        run_id=RUN_ID,
    )


def _proof(
    resolution: ResolutionResult,
    snapshot_root: Path,
) -> LocalGpuProof:
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    assert target.model_revision is not None
    assert target.tokenizer_revision is not None
    model = snapshot_directory_identity(
        snapshot_root,
        kind="model",
        revision=target.model_revision,
    )
    tokenizer = SnapshotIdentity(
        kind="tokenizer",
        root=model.root,
        revision=target.tokenizer_revision,
        sha256=model.sha256,
        file_count=model.file_count,
        total_bytes=model.total_bytes,
        hash_policy=model.hash_policy,
    )
    distribution = VllmDistributionIdentity(
        name="vllm",
        version="0.26.0",
        sha256=f"sha256:{'1' * 64}",
        file_count=100,
        total_bytes=1_000_000,
        hash_policy="installed-wheel-files-v1",
        executable_path="/opt/inferdrome-gpu/bin/vllm",
        executable_sha256=f"sha256:{'2' * 64}",
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
    inventory_stdout = (
        f"0, NVIDIA L4, {GPU_UUID}, 580.65.06\n"
    )
    started_at = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    ready_at = started_at + timedelta(seconds=20)
    server_argv = build_managed_server_argv(
        resolution.resolved_spec,
        executable_path=distribution.executable_path,
        model_path=model.root,
        tokenizer_path=tokenizer.root,
        gpu_indices=selected,
    )
    return LocalGpuProof(
        schema_version="inferdrome.local-gpu-proof.v1",
        run_id=RUN_ID,
        capture_mode="managed_local_vllm",
        captured_at=ready_at,
        client_os="Linux",
        client_arch="x86_64",
        client_python_version="3.12.11",
        torch_version="2.9.0+cu130",
        cuda_runtime_version="13.0",
        torch_cuda_device_count=1,
        nvidia_smi_path=nvidia_smi_path,
        nvidia_smi_sha256=f"sha256:{'3' * 64}",
        gpu_query_argv=gpu_inventory_argv(nvidia_smi_path, selected),
        gpu_query_stdout=inventory_stdout,
        selected_gpu_indices=selected,
        gpus=(
            GpuDeviceEvidence(
                index=0,
                model="NVIDIA L4",
                uuid=GPU_UUID,
                driver_version="580.65.06",
            ),
        ),
        producer_distribution=distribution,
        model_snapshot=model,
        tokenizer_snapshot=tokenizer,
        server=ManagedServerEvidence(
            argv=server_argv,
            endpoint=str(target.endpoint).rstrip("/"),
            environment_policy=MANAGED_PROCESS_ENVIRONMENT_POLICY,
            environment_overrides=MANAGED_PROCESS_ENVIRONMENT_OVERRIDES,
            pid=4321,
            process_group_id=4321,
            started_at=started_at,
            ready_at=ready_at,
            compute_query_argv=gpu_compute_process_argv(nvidia_smi_path, (0,)),
            compute_query_stdout=f"4322, {GPU_UUID}\n",
            gpu_processes=(
                GpuComputeProcessEvidence(
                    pid=4322,
                    process_group_id=4321,
                    gpu_uuid=GPU_UUID,
                ),
            ),
        ),
    )


def test_gpu_query_parsers_reject_ambiguous_or_duplicate_rows() -> None:
    assert parse_gpu_inventory_output(
        f"0, NVIDIA L4, {GPU_UUID}, 580.65.06\n"
    ) == ((0, "NVIDIA L4", GPU_UUID, "580.65.06"),)
    assert parse_gpu_compute_process_output(f"4322, {GPU_UUID}\n") == (
        (4322, GPU_UUID),
    )

    with pytest.raises(ValueError, match="duplicate"):
        parse_gpu_inventory_output(
            f"0, NVIDIA L4, {GPU_UUID}, 580.65.06\n"
            f"0, NVIDIA L4, {GPU_UUID}, 580.65.06\n"
        )
    with pytest.raises(ValueError, match="column count"):
        parse_gpu_compute_process_output("4322\n")


def test_snapshot_identity_is_stable_and_rejects_links(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text('{"model":"fixture"}\n')
    (snapshot / ".cache").mkdir()
    (snapshot / ".cache" / "volatile").write_text("ignored\n")
    first = snapshot_directory_identity(
        snapshot,
        kind="model",
        revision="a" * 40,
    )
    (snapshot / ".cache" / "volatile").write_text("changed but excluded\n")
    second = snapshot_directory_identity(
        snapshot,
        kind="model",
        revision="a" * 40,
    )
    assert first == second

    (snapshot / ".cache-link-target").mkdir()
    (snapshot / ".cache").rename(snapshot / ".cache-original")
    (snapshot / ".cache").symlink_to(
        snapshot / ".cache-link-target",
        target_is_directory=True,
    )
    with pytest.raises(AdapterError, match="non-directory"):
        snapshot_directory_identity(
            snapshot,
            kind="model",
            revision="a" * 40,
        )
    (snapshot / ".cache").unlink()

    os.link(snapshot / "config.json", snapshot / "hard-linked-config.json")
    with pytest.raises(AdapterError, match="independent regular"):
        snapshot_directory_identity(
            snapshot,
            kind="model",
            revision="a" * 40,
        )
    (snapshot / "hard-linked-config.json").unlink()

    (snapshot / "linked-config.json").symlink_to(snapshot / "config.json")
    with pytest.raises(AdapterError, match="non-regular"):
        snapshot_directory_identity(
            snapshot,
            kind="model",
            revision="a" * 40,
        )


def test_managed_server_argv_and_proof_are_cross_bound(tmp_path: Path) -> None:
    resolution = _resolution()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n")
    proof = _proof(resolution, snapshot)

    validated = validate_local_gpu_proof(
        resolution.resolved_spec,
        proof,
        run_id=RUN_ID,
    )
    assert validated.server.argv[:3] == (
        "/opt/inferdrome-gpu/bin/vllm",
        "serve",
        str(snapshot),
    )
    assert "--device-ids" in validated.server.argv
    assert "--no-enable-log-requests" in validated.server.argv

    changed = proof.server.model_copy(
        update={"argv": (*proof.server.argv[:-1], "debug")}
    )
    tampered = proof.model_copy(update={"server": changed})
    with pytest.raises(AdapterError, match="server invocation"):
        validate_local_gpu_proof(
            resolution.resolved_spec,
            tampered,
            run_id=RUN_ID,
        )

    raw = proof.model_dump(mode="json")
    raw["server"]["compute_query_stdout"] += (
        "9999, GPU-bbbbbbbb-cccc-dddd-eeee-ffffffffffff\n"
    )
    with pytest.raises(ValidationError, match="disagrees with raw"):
        LocalGpuProof.model_validate_json(json.dumps(raw))

    wrong_wheel = proof.model_dump(mode="json")
    wrong_wheel["producer_distribution"]["source_wheel_sha256"] = (
        f"sha256:{'0' * 64}"
    )
    with pytest.raises(ValidationError, match="source wheel disagrees"):
        LocalGpuProof.model_validate_json(json.dumps(wrong_wheel))

    changed_environment = proof.server.model_copy(
        update={"environment_overrides": ("VLLM_NO_USAGE_STATS=0",)}
    )
    tampered_environment = proof.model_copy(update={"server": changed_environment})
    with pytest.raises(AdapterError, match="environment policy"):
        validate_local_gpu_proof(
            resolution.resolved_spec,
            tampered_environment,
            run_id=RUN_ID,
        )


def test_managed_invocation_round_trips_with_local_gpu_proof(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n")
    proof = _proof(resolution, snapshot)
    response = json.dumps(
        {"data": [{"id": target.model}]},
        separators=(",", ":"),
    ).encode()
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=response,
        ),
    )
    capture = tmp_path / "capture"
    capture.mkdir()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        VllmInvocationPaths(
            dataset_path=(
                REPOSITORY_ROOT / "examples" / "real-gpu" / "workload.jsonl"
            ),
            tokenizer_path=snapshot,
            result_directory=capture,
        ),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=preflight,
        local_gpu_proof=proof,
    )
    replayed = validate_vllm_invocation_evidence(
        invocation.evidence_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        execution_fingerprint=resolution.execution_fingerprint,
    )

    assert replayed == invocation
    assert replayed.argv[0] == proof.producer_distribution.executable_path
    assert replayed.local_gpu_proof == proof


def test_managed_environment_is_complete_and_proof_backed(tmp_path: Path) -> None:
    resolution = _resolution()
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n")
    proof = _proof(resolution, snapshot)
    response = json.dumps({"data": [{"id": target.model}]}).encode()
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=response,
        ),
    )
    manifest = capture_managed_gpu_environment(
        resolution.resolved_spec,
        preflight,
        proof,
        run_id=RUN_ID,
        captured_at=proof.captured_at,
    )
    fields = {field.name: field for field in manifest.fields}

    assert manifest.completeness is EnvironmentCompleteness.COMPLETE
    assert fields[EnvironmentFieldName.GPU_COUNT].value == 1
    assert fields[EnvironmentFieldName.GPU_MODEL].value == "NVIDIA L4"
    assert fields[EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256].value == (
        proof.producer_distribution.sha256
    )
    assert fields[EnvironmentFieldName.GPU_MODEL].provenance is (
        ProvenanceKind.LOCALLY_VERIFIED
    )
    assert all(field.value is not None for field in manifest.fields)


def test_managed_config_rejects_ambiguous_gpu_selection(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="exactly one GPU index"):
        ManagedVllmConfig(
            model_path=tmp_path.absolute(),
            gpu_indices=(0, 1),
        )

    proof = {
        "schema_version": "inferdrome.local-gpu-proof.v1",
        "run_id": RUN_ID,
    }
    with pytest.raises(ValidationError):
        LocalGpuProof.model_validate(proof)
