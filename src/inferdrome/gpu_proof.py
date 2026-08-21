"""Strict internal contract for the opt-in managed real-GPU proof path."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, field_validator, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.experiment import AttachedVllmTarget, ExperimentSpec
from inferdrome.domain.ids import RunId, Sha256Digest
from inferdrome.errors import AdapterError
from inferdrome.normalization.vllm_0_26 import VLLM_VERSION
from inferdrome.qwen3_campaign import (
    QWEN3_8B_PROFILE_ID,
    require_qwen3_campaign_profile,
    validate_qwen3_campaign_spec,
)

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_GPU_UUID = re.compile(r"^GPU-[0-9A-Za-z-]{8,120}$")
_VERSION_TEXT = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_VLLM_SOURCE_WHEELS = {
    "aarch64": (
        "vllm-0.26.0-cp38-abi3-manylinux_2_28_aarch64.whl",
        "sha256:52a4c3e55c2c80cc8793e52ccc244457ceade25b0ad7caa1c15e5002a95a1b2c",
    ),
    "x86_64": (
        "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl",
        "sha256:adb1e4c9b46d0dfdb094121ae5aad670a42412dd813ed4e5db069ed6a15006de",
    ),
}
MANAGED_PROCESS_ENVIRONMENT_POLICY: Final[
    Literal["vllm-scrubbed-offline-v1"]
] = "vllm-scrubbed-offline-v1"
MANAGED_PROCESS_ENVIRONMENT_OVERRIDES: Final = (
    "DO_NOT_TRACK=1",
    "HF_HUB_DISABLE_TELEMETRY=1",
    "HF_HUB_OFFLINE=1",
    "TRANSFORMERS_OFFLINE=1",
    "VLLM_NO_USAGE_STATS=1",
)

AbsolutePathText = Annotated[str, Field(min_length=1, max_length=4096)]
CommandArgument = Annotated[str, Field(min_length=1, max_length=8192)]
CapturedText = Annotated[str, Field(max_length=262_144)]
EnvironmentOverride = Annotated[str, Field(min_length=3, max_length=256)]


def _is_safe_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() and not any(
        ord(character) < 32 for character in value
    )


@dataclass(frozen=True)
class ManagedVllmConfig:
    """Runtime-only controls for the narrow managed local-server mode."""

    model_path: Path
    gpu_indices: tuple[int, ...] = (0,)
    startup_timeout_seconds: float = 900.0
    capability_profile_id: str | None = None

    def __post_init__(self) -> None:
        if not self.model_path.is_absolute():
            raise AdapterError("managed vLLM model path must be absolute")
        if (
            len(self.gpu_indices) != 1
            or any(
                isinstance(index, bool) or not 0 <= index <= 255
                for index in self.gpu_indices
            )
        ):
            raise AdapterError("managed vLLM requires exactly one GPU index")
        timeout = self.startup_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= 3600
        ):
            raise AdapterError("managed vLLM startup timeout is outside limits")
        if self.capability_profile_id not in {None, QWEN3_8B_PROFILE_ID}:
            raise AdapterError("managed vLLM capability profile is unsupported")


class GpuDeviceEvidence(FrozenModel):
    index: Annotated[int, Field(strict=True, ge=0, le=255)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    uuid: Annotated[str, Field(min_length=12, max_length=128)]
    driver_version: Annotated[str, Field(min_length=1, max_length=128)]

    @field_validator("model")
    @classmethod
    def model_must_be_bounded_visible_text(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("GPU model is invalid")
        return value

    @field_validator("uuid")
    @classmethod
    def uuid_must_be_nvidia_gpu_identity(cls, value: str) -> str:
        if not _GPU_UUID.fullmatch(value):
            raise ValueError("GPU UUID is invalid")
        return value

    @field_validator("driver_version")
    @classmethod
    def driver_version_must_be_bounded_token(cls, value: str) -> str:
        if not _VERSION_TEXT.fullmatch(value):
            raise ValueError("GPU driver version is invalid")
        return value


class GpuComputeProcessEvidence(FrozenModel):
    pid: Annotated[int, Field(strict=True, ge=2)]
    process_group_id: Annotated[int, Field(strict=True, ge=2)]
    gpu_uuid: Annotated[str, Field(min_length=12, max_length=128)]

    @field_validator("gpu_uuid")
    @classmethod
    def uuid_must_be_nvidia_gpu_identity(cls, value: str) -> str:
        if not _GPU_UUID.fullmatch(value):
            raise ValueError("compute-process GPU UUID is invalid")
        return value


class SnapshotIdentity(FrozenModel):
    kind: Literal["model", "tokenizer"]
    root: AbsolutePathText
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    sha256: Sha256Digest
    file_count: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    total_bytes: Annotated[int, Field(strict=True, ge=1, le=137_438_953_472)]
    hash_policy: Literal["regular-files-excluding-dot-cache-v1"]

    @field_validator("root")
    @classmethod
    def root_must_be_absolute(cls, value: str) -> str:
        if not _is_safe_absolute_path(value):
            raise ValueError("snapshot root must be a safe absolute path")
        return value


class VllmDistributionIdentity(FrozenModel):
    name: Literal["vllm"]
    version: Literal["0.26.0"]
    sha256: Sha256Digest
    file_count: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    total_bytes: Annotated[int, Field(strict=True, ge=1, le=17_179_869_184)]
    hash_policy: Literal["installed-wheel-files-v1"]
    executable_path: AbsolutePathText
    executable_sha256: Sha256Digest
    source_wheel_filename: Annotated[str, Field(min_length=1, max_length=256)]
    source_wheel_path: AbsolutePathText
    source_wheel_sha256: Sha256Digest

    @field_validator("executable_path", "source_wheel_path")
    @classmethod
    def distribution_path_must_be_absolute(cls, value: str) -> str:
        if not _is_safe_absolute_path(value):
            raise ValueError("vLLM distribution path must be a safe absolute path")
        return value


class ManagedServerEvidence(FrozenModel):
    argv: Annotated[tuple[CommandArgument, ...], Field(min_length=1, max_length=256)]
    endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    environment_policy: Literal["vllm-scrubbed-offline-v1"]
    environment_overrides: Annotated[
        tuple[EnvironmentOverride, ...], Field(min_length=5, max_length=5)
    ]
    pid: Annotated[int, Field(strict=True, ge=2)]
    process_group_id: Annotated[int, Field(strict=True, ge=2)]
    started_at: AwareDatetime
    ready_at: AwareDatetime
    compute_query_argv: Annotated[
        tuple[CommandArgument, ...], Field(min_length=1, max_length=16)
    ]
    compute_query_stdout: CapturedText
    gpu_processes: Annotated[
        tuple[GpuComputeProcessEvidence, ...], Field(min_length=1, max_length=256)
    ]

    @model_validator(mode="after")
    def process_evidence_must_be_coherent(self) -> ManagedServerEvidence:
        if self.environment_overrides != MANAGED_PROCESS_ENVIRONMENT_OVERRIDES:
            raise ValueError("managed server environment overrides differ from policy")
        if self.process_group_id != self.pid:
            raise ValueError("managed server must own its isolated process group")
        if self.ready_at < self.started_at:
            raise ValueError("managed server readiness precedes process start")
        if any(
            process.process_group_id != self.process_group_id
            for process in self.gpu_processes
        ):
            raise ValueError("GPU process is outside the managed server process group")
        identities = [(process.pid, process.gpu_uuid) for process in self.gpu_processes]
        if len(identities) != len(set(identities)):
            raise ValueError("managed server GPU process evidence is duplicated")
        return self


class LocalGpuProof(FrozenModel):
    """Self-contained, allowlisted evidence from one managed Linux host."""

    schema_version: Literal["inferdrome.local-gpu-proof.v1"]
    run_id: RunId
    capture_mode: Literal["managed_local_vllm"]
    captured_at: AwareDatetime
    client_os: Literal["Linux"]
    client_arch: Literal["x86_64", "aarch64"]
    client_python_version: Annotated[str, Field(min_length=1, max_length=128)]
    torch_version: Annotated[str, Field(min_length=1, max_length=128)]
    cuda_runtime_version: Annotated[str, Field(min_length=1, max_length=128)]
    torch_cuda_device_count: Annotated[int, Field(strict=True, ge=1, le=256)]
    nvidia_smi_path: AbsolutePathText
    nvidia_smi_sha256: Sha256Digest
    gpu_query_argv: Annotated[
        tuple[CommandArgument, ...], Field(min_length=1, max_length=16)
    ]
    gpu_query_stdout: CapturedText
    selected_gpu_indices: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=0, le=255)], ...],
        Field(min_length=1, max_length=1),
    ]
    gpus: Annotated[tuple[GpuDeviceEvidence, ...], Field(min_length=1, max_length=1)]
    producer_distribution: VllmDistributionIdentity
    model_snapshot: SnapshotIdentity
    tokenizer_snapshot: SnapshotIdentity
    server: ManagedServerEvidence

    @field_validator("nvidia_smi_path")
    @classmethod
    def nvidia_smi_path_must_be_absolute(cls, value: str) -> str:
        if not _is_safe_absolute_path(value):
            raise ValueError("nvidia-smi must be a safe absolute path")
        return value

    @field_validator(
        "client_python_version",
        "torch_version",
        "cuda_runtime_version",
    )
    @classmethod
    def runtime_version_must_be_bounded_token(cls, value: str) -> str:
        if not _VERSION_TEXT.fullmatch(value):
            raise ValueError("runtime version is invalid")
        return value

    @model_validator(mode="after")
    def proof_must_be_internally_coherent(self) -> LocalGpuProof:
        selected = self.selected_gpu_indices
        if selected != tuple(sorted(set(selected))):
            raise ValueError("selected GPU indices must be unique and ordered")
        if tuple(gpu.index for gpu in self.gpus) != selected:
            raise ValueError("selected GPU indices disagree with inventory")
        if self.torch_cuda_device_count <= max(selected):
            raise ValueError("selected GPU index is absent from the CUDA runtime")
        try:
            expected_wheel = expected_vllm_source_wheel(self.client_arch)
        except ValueError:
            raise ValueError("managed proof architecture is unsupported") from None
        if (
            self.producer_distribution.source_wheel_filename != expected_wheel[0]
            or self.producer_distribution.source_wheel_sha256 != expected_wheel[1]
        ):
            raise ValueError("managed proof vLLM source wheel disagrees with its pin")
        expected_gpu_query = gpu_inventory_argv(
            self.nvidia_smi_path,
            selected,
        )
        if self.gpu_query_argv != expected_gpu_query:
            raise ValueError("GPU inventory command differs from the frozen query")
        parsed_inventory = parse_gpu_inventory_output(self.gpu_query_stdout)
        observed = tuple(
            (gpu.index, gpu.model, gpu.uuid, gpu.driver_version) for gpu in self.gpus
        )
        if parsed_inventory != observed:
            raise ValueError("GPU inventory values disagree with raw query output")
        expected_compute_query = gpu_compute_process_argv(
            self.nvidia_smi_path,
            selected,
        )
        if self.server.compute_query_argv != expected_compute_query:
            raise ValueError("GPU process command differs from the frozen query")
        parsed_processes = parse_gpu_compute_process_output(
            self.server.compute_query_stdout
        )
        stored_processes = tuple(
            (process.pid, process.gpu_uuid)
            for process in self.server.gpu_processes
        )
        if parsed_processes != stored_processes:
            raise ValueError("GPU process evidence disagrees with raw query output")
        selected_uuids = {gpu.uuid for gpu in self.gpus}
        process_uuids = {process.gpu_uuid for process in self.server.gpu_processes}
        if process_uuids != selected_uuids:
            raise ValueError("managed server does not cover every selected GPU")
        if self.model_snapshot.kind != "model":
            raise ValueError("model snapshot kind is invalid")
        if self.tokenizer_snapshot.kind != "tokenizer":
            raise ValueError("tokenizer snapshot kind is invalid")
        if self.captured_at < self.server.ready_at:
            raise ValueError("local GPU proof was captured before server readiness")
        return self

    @property
    def gpu_model(self) -> str:
        return self.gpus[0].model

    @property
    def driver_version(self) -> str:
        return self.gpus[0].driver_version


def gpu_inventory_argv(
    nvidia_smi_path: str,
    indices: tuple[int, ...],
) -> tuple[str, ...]:
    index = _single_gpu_index(indices)
    return (
        nvidia_smi_path,
        "--query-gpu=index,name,uuid,driver_version",
        "--format=csv,noheader,nounits",
        f"--id={index}",
    )


def expected_vllm_source_wheel(architecture: str) -> tuple[str, str]:
    try:
        return _VLLM_SOURCE_WHEELS[architecture]
    except KeyError:
        raise ValueError("unsupported vLLM wheel architecture") from None


def _single_gpu_index(indices: tuple[int, ...]) -> int:
    if (
        len(indices) != 1
        or isinstance(indices[0], bool)
        or not 0 <= indices[0] <= 255
    ):
        raise ValueError("GPU query requires exactly one valid index")
    return indices[0]


def gpu_compute_process_argv(
    nvidia_smi_path: str,
    indices: tuple[int, ...],
) -> tuple[str, ...]:
    index = _single_gpu_index(indices)
    return (
        nvidia_smi_path,
        "--query-compute-apps=pid,gpu_uuid",
        "--format=csv,noheader,nounits",
        f"--id={index}",
    )


def _csv_rows(content: str, *, label: str) -> tuple[tuple[str, ...], ...]:
    if len(content.encode("utf-8")) > 262_144:
        raise ValueError(f"{label} exceeds its size limit")
    try:
        rows = tuple(
            tuple(column.strip() for column in row)
            for row in csv.reader(StringIO(content), strict=True)
            if row and any(column.strip() for column in row)
        )
    except csv.Error:
        raise ValueError(f"{label} is not valid CSV") from None
    return rows


def parse_gpu_inventory_output(
    content: str,
) -> tuple[tuple[int, str, str, str], ...]:
    rows = _csv_rows(content, label="GPU inventory output")
    parsed: list[tuple[int, str, str, str]] = []
    for row in rows:
        if len(row) != 4:
            raise ValueError("GPU inventory output has an unexpected column count")
        raw_index, model, uuid, driver = row
        try:
            index = int(raw_index)
        except ValueError:
            raise ValueError("GPU inventory index is invalid") from None
        if (
            not 0 <= index <= 255
            or not model
            or len(model) > 256
            or any(ord(character) < 32 for character in model)
            or not _GPU_UUID.fullmatch(uuid)
            or not _VERSION_TEXT.fullmatch(driver)
        ):
            raise ValueError("GPU inventory row is invalid")
        parsed.append((index, model, uuid, driver))
    if not parsed:
        raise ValueError("GPU inventory output is empty")
    indices = [row[0] for row in parsed]
    uuids = [row[2] for row in parsed]
    if len(indices) != len(set(indices)) or len(uuids) != len(set(uuids)):
        raise ValueError("GPU inventory contains duplicate identities")
    return tuple(parsed)


def parse_gpu_compute_process_output(
    content: str,
) -> tuple[tuple[int, str], ...]:
    rows = _csv_rows(content, label="GPU compute-process output")
    parsed: list[tuple[int, str]] = []
    for row in rows:
        if len(row) != 2:
            raise ValueError(
                "GPU compute-process output has an unexpected column count"
            )
        raw_pid, uuid = row
        try:
            pid = int(raw_pid)
        except ValueError:
            raise ValueError("GPU compute-process PID is invalid") from None
        if pid < 2 or not _GPU_UUID.fullmatch(uuid):
            raise ValueError("GPU compute-process row is invalid")
        parsed.append((pid, uuid))
    if len(parsed) != len(set(parsed)):
        raise ValueError("GPU compute-process output contains duplicate rows")
    return tuple(parsed)


def _managed_endpoint(target: AttachedVllmTarget) -> tuple[str, int]:
    endpoint = str(target.endpoint).rstrip("/")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError:
        raise AdapterError("managed vLLM endpoint port is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AdapterError(
            "managed vLLM requires http://127.0.0.1:<port> with no path"
        )
    return endpoint, port


def validate_managed_vllm_target(spec: ExperimentSpec) -> AttachedVllmTarget:
    target = spec.target
    if not isinstance(target, AttachedVllmTarget):
        raise AdapterError("managed vLLM requires an attached vLLM target")
    _managed_endpoint(target)
    if target.engine_version != VLLM_VERSION:
        raise AdapterError("managed vLLM requires target engine version 0.26.0")
    if (
        target.model_revision is None
        or not _REVISION.fullmatch(target.model_revision)
        or target.tokenizer_revision is None
        or not _REVISION.fullmatch(target.tokenizer_revision)
    ):
        raise AdapterError(
            "managed vLLM requires exact 40-character model and tokenizer revisions"
        )
    return target


def build_managed_server_argv(
    spec: ExperimentSpec,
    *,
    executable_path: str,
    model_path: str,
    tokenizer_path: str,
    gpu_indices: tuple[int, ...],
    capability_profile_id: str | None = None,
) -> tuple[str, ...]:
    target = validate_managed_vllm_target(spec)
    require_qwen3_campaign_profile(spec, capability_profile_id)
    if capability_profile_id is not None:
        if capability_profile_id != QWEN3_8B_PROFILE_ID:
            raise AdapterError("managed vLLM capability profile is unsupported")
        validate_qwen3_campaign_spec(spec)
    endpoint, port = _managed_endpoint(target)
    del endpoint
    if not _is_safe_absolute_path(executable_path):
        raise AdapterError("managed vLLM executable must be a safe absolute path")
    if not _is_safe_absolute_path(model_path) or not _is_safe_absolute_path(
        tokenizer_path
    ):
        raise AdapterError("managed vLLM snapshots must use safe absolute paths")
    if (
        len(gpu_indices) != 1
        or isinstance(gpu_indices[0], bool)
        or not 0 <= gpu_indices[0] <= 255
    ):
        raise AdapterError("managed vLLM server requires exactly one GPU index")
    selected_devices = ",".join(str(index) for index in gpu_indices)
    dtype = "bfloat16" if capability_profile_id is not None else "auto"
    max_model_len = "2048" if capability_profile_id is not None else "1024"
    gpu_memory_utilization = (
        "0.90" if capability_profile_id is not None else "0.80"
    )
    return (
        executable_path,
        "serve",
        model_path,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        target.model,
        "--tokenizer",
        tokenizer_path,
        "--tokenizer-mode",
        "auto",
        "--dtype",
        dtype,
        "--seed",
        str(spec.workload.seed),
        "--load-format",
        "safetensors",
        "--generation-config",
        "vllm",
        "--model-impl",
        "vllm",
        "--max-model-len",
        max_model_len,
        "--gpu-memory-utilization",
        gpu_memory_utilization,
        "--tensor-parallel-size",
        str(len(gpu_indices)),
        "--device-ids",
        selected_devices,
        "--no-enable-log-requests",
        "--disable-uvicorn-access-log",
        "--uvicorn-log-level",
        "warning",
    )


def validate_local_gpu_proof(
    spec: ExperimentSpec,
    proof: LocalGpuProof,
    *,
    run_id: str,
    capability_profile_id: str | None = None,
) -> LocalGpuProof:
    target = validate_managed_vllm_target(spec)
    endpoint, _ = _managed_endpoint(target)
    if proof.run_id != run_id:
        raise AdapterError("local GPU proof belongs to a different run")
    if proof.client_os != "Linux":
        raise AdapterError("local GPU proof requires a Linux host")
    if proof.model_snapshot.revision != target.model_revision:
        raise AdapterError("local GPU proof model revision disagrees")
    if proof.tokenizer_snapshot.revision != target.tokenizer_revision:
        raise AdapterError("local GPU proof tokenizer revision disagrees")
    if proof.server.endpoint != endpoint:
        raise AdapterError("local GPU proof endpoint disagrees")
    if (
        proof.server.environment_policy != MANAGED_PROCESS_ENVIRONMENT_POLICY
        or proof.server.environment_overrides
        != MANAGED_PROCESS_ENVIRONMENT_OVERRIDES
    ):
        raise AdapterError("local GPU proof server environment policy disagrees")
    expected_server_argv = build_managed_server_argv(
        spec,
        executable_path=proof.producer_distribution.executable_path,
        model_path=proof.model_snapshot.root,
        tokenizer_path=proof.tokenizer_snapshot.root,
        gpu_indices=proof.selected_gpu_indices,
        capability_profile_id=capability_profile_id,
    )
    if proof.server.argv != expected_server_argv:
        raise AdapterError("local GPU proof server invocation disagrees")
    if proof.server.argv[0] != proof.producer_distribution.executable_path:
        raise AdapterError("local GPU proof producer executable disagrees")
    return proof
