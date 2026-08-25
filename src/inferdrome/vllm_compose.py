"""Static and preflight policy for the vLLM/Compose deployment boundary.

This module does not build images, download model weights, start containers, or
contact a provider.  It centralizes the immutable runtime identity and the
fail-closed checks used by the local orchestration script.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from inferdrome.domain.experiment import AttachedVllmTarget, ExperimentSpec
from inferdrome.errors import AdapterError, ResolutionError, SourceInputError
from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    QWEN3_CONCURRENCY_LEVELS,
    QWEN3_WORKLOAD_ID,
    QWEN3_WORKLOAD_PATH,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest,
    qwen3_model_manifest_sha256,
    qwen3_workload_sha256,
    validate_qwen3_campaign_spec,
)
from inferdrome.qwen3_tokenizer import verify_qwen3_tokenizer_files
from inferdrome.resolution import resolve_experiment

VLLM_RUNTIME_SCHEMA_VERSION: Final = "inferdrome.vllm-runtime-image.v1"
VLLM_VERSION: Final = "0.26.0"
VLLM_RUNTIME_IMAGE_REPOSITORY: Final = "vllm/vllm-openai"
VLLM_RUNTIME_IMAGE_TAG: Final = "v0.26.0"
VLLM_RUNTIME_IMAGE_DIGEST: Final = (
    "sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52"
)
VLLM_RUNTIME_IMAGE_REFERENCE: Final = (
    f"{VLLM_RUNTIME_IMAGE_REPOSITORY}@{VLLM_RUNTIME_IMAGE_DIGEST}"
)
VLLM_SOURCE_COMMIT: Final = "568afb3a13806beb53bb2e6bd518269357b237c0"
VLLM_SOURCE_URL: Final = "https://github.com/vllm-project/vllm"
VLLM_LICENSE: Final = "Apache-2.0"
SOURCE_REPOSITORY_URL: Final = "https://github.com/jayeshsuyal/inferdrome"
CANONICAL_CONTAINER_PLATFORM: Final = "linux/amd64"
COMPOSE_NETWORK_ALIAS: Final = "vllm-engine.internal"
COMPOSE_ENGINE_ENDPOINT: Final = "http://vllm-engine.internal:8000"
QWEN3_COMPOSE_BINDING_SCHEMA_VERSION: Final = (
    "inferdrome.qwen3-compose-binding.v1"
)
GPU_COMPOSE_GATE_ENV: Final = "INFERDROME_GPU_COMPOSE_GATE"
COMPOSE_CLEANUP_COMMAND: Final = (
    "docker",
    "compose",
    "down",
    "--remove-orphans",
    "--volumes",
)

_IMAGE_REFERENCE = re.compile(
    r"^[a-z0-9](?:[a-z0-9./_-]{0,254}[a-z0-9])?@sha256:[0-9a-f]{64}$"
)


class ComposePreflightError(ValueError):
    """A bounded local Compose or GPU preflight failure."""


def vllm_runtime_contract() -> dict[str, object]:
    """Return the privacy-safe immutable vLLM runtime contract."""

    return {
        "schema_version": VLLM_RUNTIME_SCHEMA_VERSION,
        "engine": "vllm",
        "engine_version": VLLM_VERSION,
        "image": {
            "repository": VLLM_RUNTIME_IMAGE_REPOSITORY,
            "tag_reference_only": VLLM_RUNTIME_IMAGE_TAG,
            "digest": VLLM_RUNTIME_IMAGE_DIGEST,
            "reference": VLLM_RUNTIME_IMAGE_REFERENCE,
            "platform": CANONICAL_CONTAINER_PLATFORM,
        },
        "source": {
            "repository": VLLM_SOURCE_URL,
            "commit": VLLM_SOURCE_COMMIT,
            "release_tag": VLLM_RUNTIME_IMAGE_TAG,
            "license": VLLM_LICENSE,
        },
        "role": "serving-engine",
        "entrypoint": ["vllm", "serve"],
        "network": "internal-only",
        "model": {
            "model_id": QWEN3_8B_MODEL_ID,
            "model_revision": QWEN3_8B_REVISION,
            "tokenizer_revision": QWEN3_8B_REVISION,
            "capability_profile_id": QWEN3_8B_PROFILE_ID,
            "model_manifest_sha256": qwen3_model_manifest_sha256(),
            "snapshot_manifest_sha256": qwen3_expected_snapshot_sha256(),
        },
    }


def vllm_compose_contract() -> dict[str, object]:
    """Return the privacy-safe runner/engine/topology Compose contract."""

    return {
        "schema_version": "inferdrome.vllm-compose.v1",
        "modes": {
            "mock": {
                "profile": "default",
                "intent": "synthetic_only",
                "evidence_eligible": False,
                "engine_role": "deterministic-openai-compatible-mock",
                "runner_role": "synthetic-endpoint-probe",
            },
            "gpu": {
                "profile": "gpu",
                "intent": "armed_linux_nvidia_runtime",
                "evidence_eligible": False,
                "engine_role": "vllm-serving-engine",
                "runner_role": "canonical-inferdrome-benchmark-runner",
                "confirmation_required": True,
                "platform": CANONICAL_CONTAINER_PLATFORM,
            },
        },
        "engine": {
            "image_reference": VLLM_RUNTIME_IMAGE_REFERENCE,
            "entrypoint": ["vllm", "serve"],
            "version": VLLM_VERSION,
            "endpoint": COMPOSE_ENGINE_ENDPOINT,
            "network_scope": "internal_only",
        },
        "runner": {
            "image_reference_env": "INFERDROME_VLLM_RUNNER_IMAGE",
            "proof_identity": "immutable_digest_required",
            "entrypoint": ["inferdrome"],
            "producer": "vllm_bench_serve",
            "producer_version": VLLM_VERSION,
        },
        "model": {
            "capability_profile_id": QWEN3_8B_PROFILE_ID,
            "model_id": QWEN3_8B_MODEL_ID,
            "model_revision": QWEN3_8B_REVISION,
            "tokenizer_revision": QWEN3_8B_REVISION,
            "model_manifest_sha256": qwen3_model_manifest_sha256(),
            "snapshot_manifest_sha256": qwen3_expected_snapshot_sha256(),
        },
        "compose_binding": {
            "schema_version": QWEN3_COMPOSE_BINDING_SCHEMA_VERSION,
            "source_profile_id": QWEN3_8B_PROFILE_ID,
            "endpoint": COMPOSE_ENGINE_ENDPOINT,
            "topology": "private_compose_dns",
            "methodology": "frozen_qwen3_campaign_v1_except_endpoint_topology",
            "execution": {
                "adapter": "vllm_bench_serve",
                "adapter_version": "1.0.0",
                "producer_name": "vllm",
                "producer_version": VLLM_VERSION,
                "max_runtime_seconds": 1_800,
                "max_measured_requests": 96,
            },
            "workload": {
                "id": QWEN3_WORKLOAD_ID,
                "path": QWEN3_WORKLOAD_PATH,
                "sha256": qwen3_workload_sha256(),
                "requested_output_tokens": 128,
                "temperature": "0.7",
                "seed": 42,
            },
            "traffic": {
                "kind": "concurrent",
                "allowed_concurrency": list(QWEN3_CONCURRENCY_LEVELS),
                "warmup_requests": 12,
                "measured_requests": 96,
            },
            "measurement": {
                "streaming": True,
                "ttft_definition": "vllm_first_choices_event_v0_26",
                "choices_span_definition": "last_choices_event_span_v1",
                "metric_definitions_version": "1.0.0",
                "reducer_version": "1.0.0",
            },
        },
        "cleanup": {
            "command": list(COMPOSE_CLEANUP_COMMAND),
            "evidence_bind_preserved": True,
        },
    }


def require_immutable_image_reference(value: str | None, label: str) -> str:
    """Validate an image reference without including the submitted value in errors."""

    if not isinstance(value, str) or len(value) > 320:
        raise ComposePreflightError(f"{label} image reference is missing or invalid")
    if _IMAGE_REFERENCE.fullmatch(value) is None:
        raise ComposePreflightError(f"{label} image reference must use a digest")
    return value


def _require_absolute_directory(
    value: str | None,
    label: str,
    *,
    writable: bool,
) -> Path:
    if not isinstance(value, str) or len(value) == 0 or len(value) > 4_096:
        raise ComposePreflightError(f"{label} path is missing or invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ComposePreflightError(f"{label} path must be absolute")
    try:
        path_stat = path.lstat()
    except OSError:
        raise ComposePreflightError(f"{label} path is unavailable") from None
    if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise ComposePreflightError(f"{label} path is not a real directory")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ComposePreflightError(f"{label} path is not readable")
    if writable and not os.access(path, os.W_OK):
        raise ComposePreflightError(f"{label} path is not writable")
    return path


def _require_model_snapshot(value: str | None) -> Path:
    path = _require_absolute_directory(
        value,
        "Qwen3 model snapshot",
        writable=False,
    )
    manifest = qwen3_model_manifest()
    expected_paths: set[str] = set()
    try:
        for item in manifest["files"]:
            relative_path = item["path"]
            expected_paths.add(relative_path)
            candidate = path / relative_path
            candidate_stat = os.lstat(candidate)
            if (
                stat.S_ISLNK(candidate_stat.st_mode)
                or not stat.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_nlink != 1
                or candidate_stat.st_size != item["size_bytes"]
            ):
                raise ComposePreflightError(
                    "Qwen3 model snapshot does not match its frozen manifest"
                )
    except ComposePreflightError:
        raise
    except (KeyError, OSError, TypeError, ValueError):
        raise ComposePreflightError(
            "Qwen3 model snapshot does not match its frozen manifest"
        ) from None
    if len(expected_paths) != manifest["file_count"]:
        raise ComposePreflightError(
            "Qwen3 model manifest has an invalid bounded file set"
        )
    try:
        snapshot = snapshot_directory_identity(
            path,
            kind="model",
            revision=QWEN3_8B_REVISION,
        )
        verify_qwen3_tokenizer_files(path)
    except (AdapterError, OSError, TypeError, ValueError):
        raise ComposePreflightError(
            "Qwen3 model snapshot verification failed"
        ) from None
    if (
        snapshot.revision != QWEN3_8B_REVISION
        or snapshot.sha256 != qwen3_expected_snapshot_sha256()
        or snapshot.file_count != manifest["file_count"]
        or snapshot.total_bytes != manifest["total_bytes"]
        or snapshot.hash_policy != "regular-files-excluding-dot-cache-v1"
    ):
        raise ComposePreflightError(
            "Qwen3 model snapshot does not match its frozen identity"
        )
    return path


def _require_experiment_input(value: str | None) -> Path:
    path = _require_absolute_directory(
        value,
        "experiment input",
        writable=False,
    )
    experiment = path / "experiment.yaml"
    try:
        experiment_stat = os.lstat(experiment)
        if (
            stat.S_ISLNK(experiment_stat.st_mode)
            or not stat.S_ISREG(experiment_stat.st_mode)
            or experiment_stat.st_nlink != 1
        ):
            raise ComposePreflightError("experiment input is not a safe regular file")
        resolution = resolve_experiment(experiment, strict=True)
        _validate_compose_experiment(resolution.resolved_spec)
    except ComposePreflightError:
        raise
    except (
        AdapterError,
        OSError,
        ResolutionError,
        SourceInputError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise ComposePreflightError(
            "experiment input does not match the pinned Compose benchmark binding"
        ) from None
    return path


def _validate_compose_experiment(spec: ExperimentSpec) -> None:
    target = spec.target
    if not isinstance(target, AttachedVllmTarget):
        raise ComposePreflightError(
            "Compose benchmark requires an attached vLLM target"
        )
    if str(target.endpoint).rstrip("/") != COMPOSE_ENGINE_ENDPOINT:
        raise ComposePreflightError(
            "experiment endpoint does not match Compose topology"
        )

    # Reuse the frozen campaign validator for every methodology/model/workload
    # field. Only the endpoint is replaced in this private validation copy: the
    # old profile remains unchanged at its loopback endpoint, while this
    # versioned binding explicitly records the private Compose DNS topology.
    frozen_target = target.model_copy(update={"endpoint": "http://127.0.0.1:18080"})
    frozen_spec = spec.model_copy(update={"target": frozen_target})
    try:
        validate_qwen3_campaign_spec(frozen_spec)
    except AdapterError:
        raise ComposePreflightError(
            "experiment does not match the frozen Qwen3 methodology binding"
        ) from None
    if spec.workload.sha256 != qwen3_workload_sha256():
        raise ComposePreflightError("experiment workload digest is not pinned")


def _require_evidence_output(value: str | None) -> Path:
    return _require_absolute_directory(value, "evidence output", writable=True)


def validate_gpu_preflight(
    *,
    confirmation: str | None,
    platform_name: str | None,
    nvidia_available: bool,
    runner_image: str | None,
    runtime_image: str | None,
    model_path: str | None,
    experiment_dir: str | None,
    evidence_dir: str | None,
    profile_id: str | None,
    model_id: str | None,
    model_revision: str | None,
    tokenizer_revision: str | None,
    compose_available: bool | None,
) -> None:
    """Fail closed before Docker for an explicitly armed GPU Compose run."""

    if confirmation != "1":
        raise ComposePreflightError("GPU Compose requires explicit confirmation")
    if platform_name != "Linux":
        raise ComposePreflightError("GPU Compose requires Linux")
    if not nvidia_available:
        raise ComposePreflightError("GPU Compose requires NVIDIA tooling and a GPU")
    require_immutable_image_reference(runner_image, "runner")
    if runtime_image != VLLM_RUNTIME_IMAGE_REFERENCE:
        raise ComposePreflightError(
            "runtime image must be the pinned vLLM 0.26.0 image"
        )
    if profile_id != QWEN3_8B_PROFILE_ID:
        raise ComposePreflightError(
            "Qwen3 capability profile is missing or unsupported"
        )
    if model_id != QWEN3_8B_MODEL_ID:
        raise ComposePreflightError("Qwen3 model identity is missing or unsupported")
    if model_revision != QWEN3_8B_REVISION:
        raise ComposePreflightError("Qwen3 model revision is missing or unsupported")
    if tokenizer_revision != QWEN3_8B_REVISION:
        raise ComposePreflightError(
            "Qwen3 tokenizer revision is missing or unsupported"
        )
    _require_model_snapshot(model_path)
    _require_experiment_input(experiment_dir)
    _require_evidence_output(evidence_dir)
    if compose_available is False:
        raise ComposePreflightError("Docker Compose is unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inferdrome-vllm-compose")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("gpu-preflight")
    preflight.add_argument("--confirmation")
    preflight.add_argument("--platform", dest="platform_name")
    preflight.add_argument("--nvidia-available", action="store_true")
    preflight.add_argument(
        "--compose-available",
        action="store_true",
        default=None,
        help="assert that Docker Compose v2 was checked by the caller",
    )
    preflight.add_argument("--runner-image")
    preflight.add_argument("--runtime-image")
    preflight.add_argument("--model-path")
    preflight.add_argument("--experiment-dir")
    preflight.add_argument("--evidence-dir")
    preflight.add_argument("--profile-id")
    preflight.add_argument("--model-id")
    preflight.add_argument("--model-revision")
    preflight.add_argument("--tokenizer-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command != "gpu-preflight":
        return 2
    try:
        validate_gpu_preflight(
            confirmation=arguments.confirmation,
            platform_name=arguments.platform_name,
            nvidia_available=arguments.nvidia_available,
            runner_image=arguments.runner_image,
            runtime_image=arguments.runtime_image,
            model_path=arguments.model_path,
            experiment_dir=arguments.experiment_dir,
            evidence_dir=arguments.evidence_dir,
            profile_id=arguments.profile_id,
            model_id=arguments.model_id,
            model_revision=arguments.model_revision,
            tokenizer_revision=arguments.tokenizer_revision,
            compose_available=arguments.compose_available,
        )
    except ComposePreflightError as error:
        print(f"vLLM Compose preflight failed: {error}", file=sys.stderr)
        return 2
    if arguments.compose_available is None:
        print("vLLM Compose GPU input preflight: OK (Compose not checked)")
    else:
        print("vLLM Compose GPU preflight: OK")
    return 0


__all__ = [
    "CANONICAL_CONTAINER_PLATFORM",
    "COMPOSE_CLEANUP_COMMAND",
    "COMPOSE_ENGINE_ENDPOINT",
    "COMPOSE_NETWORK_ALIAS",
    "GPU_COMPOSE_GATE_ENV",
    "QWEN3_COMPOSE_BINDING_SCHEMA_VERSION",
    "SOURCE_REPOSITORY_URL",
    "VLLM_LICENSE",
    "VLLM_RUNTIME_IMAGE_DIGEST",
    "VLLM_RUNTIME_IMAGE_REFERENCE",
    "VLLM_RUNTIME_IMAGE_REPOSITORY",
    "VLLM_RUNTIME_IMAGE_TAG",
    "VLLM_RUNTIME_SCHEMA_VERSION",
    "VLLM_SOURCE_COMMIT",
    "VLLM_SOURCE_URL",
    "VLLM_VERSION",
    "ComposePreflightError",
    "require_immutable_image_reference",
    "validate_gpu_preflight",
    "vllm_compose_contract",
    "vllm_runtime_contract",
]


if __name__ == "__main__":
    raise SystemExit(main())
