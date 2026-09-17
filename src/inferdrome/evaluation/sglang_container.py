"""Fixed SGLang container projection and bounded local artifact preflight.

Profile construction is pure. Explicit artifact verification only reads caller-
selected local files and reuses the existing snapshot hash policy; it never
downloads artifacts, discovers credentials, invokes Docker, or accesses a GPU.
Neither operation establishes runtime qualification or subsequent immutability.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from inferdrome.errors import AdapterError
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.sglang_profile import (
    SglangServingConfig,
    SglangServingProfile,
    build_sglang_serving_profile,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

SGLANG_CONTAINER_MODEL_PATH: Final = "/model"
SGLANG_CONTAINER_TOKENIZER_PATH: Final = "/tokenizer"
SGLANG_CONTAINER_TEMPLATE_PATH: Final = "/chat-template.jinja"
SGLANG_CONTAINER_PORT: Final = 8000
MAX_SGLANG_TEMPLATE_BYTES: Final = 1_048_576


@dataclass(frozen=True)
class SglangContainerProfile:
    """Explicit projection of a native profile, never a claim it was launched.

    launch_sha256 binds the engine/mount/port/GPU projection. Ownership names,
    labels, cleanup, and the complete Docker command remain the lifecycle's
    responsibility. Private source paths occur only in in-memory launch inputs.
    """

    source_profile: SglangServingProfile = field(repr=False)
    readiness_config: SglangServingConfig = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    environment: tuple[tuple[str, str], ...]
    mounts: tuple[str, ...] = field(repr=False)
    publish: str = field(repr=False)
    gpu_index: int
    source_profile_sha256: str
    readiness_config_sha256: str
    launch_sha256: str
    image_reference: str
    image_platform: Literal["linux/amd64"]
    execution_mode: Literal["DOCKER_BRIDGE"] = "DOCKER_BRIDGE"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


def _strict_config(config: SglangServingConfig) -> SglangServingConfig:
    if type(config) is not SglangServingConfig:
        raise ValueError
    # Python mode retains invalid primitive types in forged model_copy inputs.
    return SglangServingConfig.model_validate(
        config.model_dump(mode="python", warnings=False)
    )


def build_sglang_container_profile(
    config: SglangServingConfig, index: int
) -> SglangContainerProfile:
    """Map one of exactly two loopback endpoints onto a fixed container recipe.

    The native config/origin and its hash remain unchanged. Only host, port and
    three artifact paths are projected; all other source argv/env settings stay
    exact. Existing native profile behavior is not altered.
    """
    try:
        config = _strict_config(config)
        if type(index) is not int or index not in (0, 1):
            raise ValueError
        source = build_sglang_serving_profile(config)
        origin = urlsplit(config.origin)
        if origin.hostname != "127.0.0.1" or origin.port is None:
            raise ValueError
        projected_config = SglangServingConfig.model_validate(
            {
                **config.model_dump(mode="python"),
                "model_path": SGLANG_CONTAINER_MODEL_PATH,
                "tokenizer_path": SGLANG_CONTAINER_TOKENIZER_PATH,
                "chat_template_path": SGLANG_CONTAINER_TEMPLATE_PATH,
            }
        )
        projected_profile = build_sglang_serving_profile(projected_config)
        argv = list(projected_profile.argv)
        argv[argv.index("--host") + 1] = "0.0.0.0"
        argv[argv.index("--port") + 1] = str(SGLANG_CONTAINER_PORT)
        mounts = tuple(
            f"type=bind,src={source_path},dst={target},readonly"
            for source_path, target in (
                (config.model_path, SGLANG_CONTAINER_MODEL_PATH),
                (config.tokenizer_path, SGLANG_CONTAINER_TOKENIZER_PATH),
                (config.chat_template_path, SGLANG_CONTAINER_TEMPLATE_PATH),
            )
        )
        publish = f"127.0.0.1:{origin.port}:{SGLANG_CONTAINER_PORT}"
        payload = {
            "schema_version": "inferdrome.sglang-container-launch.v1",
            "execution_mode": "DOCKER_BRIDGE",
            "source_profile_sha256": source.config_sha256,
            "readiness_config_sha256": projected_profile.config_sha256,
            "argv": argv,
            "environment": [list(pair) for pair in source.environment],
            "mounts": list(mounts),
            "publish": publish,
            "gpu_index": index,
            "image_reference": source.image_reference,
            "image_platform": source.image_platform,
        }
        return SglangContainerProfile(
            source_profile=source,
            readiness_config=projected_config,
            argv=tuple(argv),
            environment=source.environment,
            mounts=mounts,
            publish=publish,
            gpu_index=index,
            source_profile_sha256=source.config_sha256,
            readiness_config_sha256=projected_profile.config_sha256,
            launch_sha256=sha256_digest(canonical_json_bytes(payload) + b"\n"),
            image_reference=source.image_reference,
            image_platform=source.image_platform,
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise EvaluationError("SGLang container projection is invalid") from None


@dataclass(frozen=True)
class SglangVerifiedArtifacts:
    """Local read-time hash match; no loaded-model or GPU attestation."""

    source_profile_sha256: str
    model_snapshot_sha256: str
    tokenizer_snapshot_sha256: str
    chat_template_sha256: str
    snapshot_hash_policy: Literal["regular-files-excluding-dot-cache-v1"] = (
        "regular-files-excluding-dot-cache-v1"
    )
    verification: Literal["LOCAL_FILES_MATCH_AT_PREFLIGHT"] = (
        "LOCAL_FILES_MATCH_AT_PREFLIGHT"
    )
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


def _real_ancestors(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    """Reject redirected ancestors without treating sibling writes as changes."""
    result: list[tuple[str, int, int, int]] = []
    for parent in reversed(path.parents):
        metadata = os.lstat(parent)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        result.append((str(parent), metadata.st_dev, metadata.st_ino, metadata.st_mode))
    return tuple(result)


def verify_sglang_artifacts(config: SglangServingConfig) -> SglangVerifiedArtifacts:
    """Read and compare declared local snapshots and exact Jinja template bytes.

    Model/tokenizer revisions are labels bound into the declaration, not proof of
    remote repository contents. Snapshot hashes use the existing full regular-file
    policy, excluding .cache. Links, special files, hardlinks, oversized templates
    and detected changes fail closed. Call immediately before owned launch; these
    finite checks cannot guarantee the files remain immutable after return.
    """
    # Keep the pure projection/binding import path independent of lifecycle code.
    # These existing read-only hashing helpers are used only by explicit preflight.
    from inferdrome.execution.managed_vllm import (
        _discover_snapshot_files,
        _file_identity,
        _read_small_regular_file,
        snapshot_directory_identity,
    )

    try:
        config = _strict_config(config)
        source = build_sglang_serving_profile(config)
        model_path, tokenizer_path, template_path = (
            Path(config.model_path),
            Path(config.tokenizer_path),
            Path(config.chat_template_path),
        )
        paths = (model_path, tokenizer_path, template_path)
        ancestors = tuple(_real_ancestors(path) for path in paths)
        roots = tuple(_file_identity(os.lstat(path)) for path in paths[:2])
        if any(not stat.S_ISDIR(identity.mode) for identity in roots):
            raise ValueError
        model_before = _discover_snapshot_files(model_path)
        tokenizer_before = _discover_snapshot_files(tokenizer_path)
        template_before = _file_identity(os.lstat(template_path))
        if template_before.size < 1:
            raise ValueError
        model = snapshot_directory_identity(
            model_path, kind="model", revision=config.model_revision
        )
        tokenizer = snapshot_directory_identity(
            tokenizer_path, kind="tokenizer", revision=config.tokenizer_revision
        )
        template = _read_small_regular_file(
            template_path,
            template_before,
            label="SGLang chat template",
            limit=MAX_SGLANG_TEMPLATE_BYTES,
        )
        template_sha256 = sha256_digest(template)
        if (
            model.sha256 != config.model_snapshot_sha256
            or tokenizer.sha256 != config.tokenizer_snapshot_sha256
            or template_sha256 != config.chat_template_sha256
            or tuple(_real_ancestors(path) for path in paths) != ancestors
            or tuple(_file_identity(os.lstat(path)) for path in paths[:2]) != roots
            or _discover_snapshot_files(model_path) != model_before
            or _discover_snapshot_files(tokenizer_path) != tokenizer_before
            or _file_identity(os.lstat(template_path)) != template_before
        ):
            raise ValueError
        return SglangVerifiedArtifacts(
            source_profile_sha256=source.config_sha256,
            model_snapshot_sha256=model.sha256,
            tokenizer_snapshot_sha256=tokenizer.sha256,
            chat_template_sha256=template_sha256,
        )
    except (
        AdapterError,
        ValueError,
        TypeError,
        OSError,
        AttributeError,
        RecursionError,
    ):
        raise EvaluationError("SGLang local artifact verification failed") from None
