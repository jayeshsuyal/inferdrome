"""Static and preflight policy for the vLLM/Compose deployment boundary.

This module does not build images, download model weights, start containers, or
contact a provider.  It centralizes the immutable runtime identity and the
fail-closed checks used by the local orchestration script.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Final

from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
)

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
CANONICAL_CONTAINER_PLATFORM: Final = "linux/amd64"
COMPOSE_NETWORK_ALIAS: Final = "vllm-engine.internal"
COMPOSE_ENGINE_ENDPOINT: Final = "http://vllm-engine.internal:8000"

_IMAGE_REFERENCE = re.compile(
    r"^[a-z0-9](?:[a-z0-9./_-]{0,254}[a-z0-9])?@sha256:[0-9a-f]{64}$"
)
_EXPECTED_MODEL_FILES: Final = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
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
        "cleanup": {
            "command": ["docker", "compose", "down", "--remove-orphans"],
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


def _require_absolute_directory(value: str | None, label: str) -> Path:
    if not isinstance(value, str) or len(value) == 0 or len(value) > 4_096:
        raise ComposePreflightError(f"{label} path is missing or invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ComposePreflightError(f"{label} path must be absolute")
    try:
        stat = path.lstat()
    except OSError:
        raise ComposePreflightError(f"{label} path is unavailable") from None
    if not path.is_dir() or path.is_symlink() or not os.access(path, os.W_OK):
        del stat
        raise ComposePreflightError(f"{label} path is not a writable real directory")
    return path


def _require_model_snapshot(value: str | None) -> Path:
    path = _require_absolute_directory(value, "Qwen3 model snapshot")
    for filename in _EXPECTED_MODEL_FILES:
        candidate = path / filename
        try:
            if not candidate.is_file() or candidate.is_symlink():
                raise ComposePreflightError(
                    "Qwen3 model snapshot is missing required files"
                )
        except OSError:
            raise ComposePreflightError(
                "Qwen3 model snapshot is missing required files"
            ) from None
    return path


def _require_experiment_input(value: str | None) -> Path:
    path = _require_absolute_directory(value, "experiment input")
    experiment = path / "experiment.yaml"
    try:
        if not experiment.is_file() or experiment.is_symlink():
            raise ComposePreflightError("experiment input is missing experiment.yaml")
    except OSError:
        raise ComposePreflightError(
            "experiment input is missing experiment.yaml"
        ) from None
    return path


def _require_evidence_output(value: str | None) -> Path:
    return _require_absolute_directory(value, "evidence output")


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
    compose_available: bool,
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
    if not compose_available:
        raise ComposePreflightError("Docker Compose is unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inferdrome-vllm-compose")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("gpu-preflight")
    preflight.add_argument("--confirmation")
    preflight.add_argument("--platform", dest="platform_name")
    preflight.add_argument("--nvidia-available", action="store_true")
    preflight.add_argument("--compose-available", action="store_true")
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
    print("vLLM Compose GPU preflight: OK")
    return 0


__all__ = [
    "CANONICAL_CONTAINER_PLATFORM",
    "COMPOSE_ENGINE_ENDPOINT",
    "COMPOSE_NETWORK_ALIAS",
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
