"""Offline admission for ONE attended session; has no provider/create seam.

The operator must supply locally held, reviewed stage archives for both runtime
environments and the shared model before renting. Byte closure is not GPU proof.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
from inferdrome.evaluation.engine_comparison import Digest, encoded
from inferdrome.evaluation.engine_comparison_local import LocalInputs, launch_argv
from inferdrome.evaluation.sglang_profile import LocalPath
from inferdrome.qwen3_campaign import qwen3_model_manifest
from inferdrome.routing_execution.canonical import sha256_digest


class StageArchive(ClosedModel):
    role: Literal["source", "vllm", "sglang", "model"]
    path: LocalPath
    sha256: Digest
    unpacked_bytes: Annotated[int, Field(gt=0, le=512 * 1024**3)]
    # Digest of a reviewed sorted [member, size, sha256] inventory.
    inventory_sha256: Digest


class SessionPreparation(ClosedModel):
    inputs_sha256: Digest
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    archives: tuple[StageArchive, StageArchive, StageArchive, StageArchive]
    result_destination: LocalPath
    available_disk_bytes: Annotated[int, Field(gt=0, le=4 * 1024**4)]
    max_session_seconds: Annotated[int, Field(ge=1, le=14400)]
    staging_seconds: Annotated[int, Field(ge=1, le=3600)]
    retrieval_seconds: Annotated[int, Field(ge=1, le=900)]
    destruction_reserve_seconds: Annotated[int, Field(ge=60, le=900)]
    # Required reviewed operator recipe includes staging, exact-ID guardian and
    # one-create/no-retry/finally-destroy/two-absence-read workflow.
    operator_recipe: LocalPath
    operator_recipe_sha256: Digest
    max_creates: Literal[1] = 1
    transition: Literal["EXACT_PROCESS_CLEANUP_PORTS_CLOSED_GPU_IDLE"] = (
        "EXACT_PROCESS_CLEANUP_PORTS_CLOSED_GPU_IDLE"
    )


def verify_archive(archive: StageArchive) -> list[list[str | int]]:
    path = Path(archive.path)
    if path.resolve() != path or not path.is_file():
        raise EvaluationError("session staging archive unavailable")
    before = path.stat()
    with path.open("rb") as stream:
        if (
            "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            != archive.sha256
        ):
            raise EvaluationError("session staging archive digest changed")
    inventory: list[list[str | int]] = []
    names: set[str] = set()
    total = 0
    with tarfile.open(path, "r:*") as tar:
        for member in tar:
            if len(names) >= 100000:
                raise EvaluationError("session archive inventory exceeds bound")
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or member.name in names
                or not (member.isfile() or member.isdir())
            ):
                raise EvaluationError(
                    "session archive aliases or special files unsupported"
                )
            names.add(member.name)
            if member.isdir():
                continue
            total += member.size
            if total > archive.unpacked_bytes:
                raise EvaluationError("session archive exceeds disk reservation")
            member_stream = tar.extractfile(member)
            if member_stream is None:
                raise EvaluationError("session archive member unavailable")
            with member_stream:
                hasher = hashlib.sha256()
                while chunk := member_stream.read(1024 * 1024):
                    hasher.update(chunk)
                digest = "sha256:" + hasher.hexdigest()
            inventory.append([member.name, member.size, digest])
    inventory.sort(key=lambda row: str(row[0]))
    after = path.stat()
    if (
        (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or total != archive.unpacked_bytes
        or sha256_digest(encoded(inventory)) != archive.inventory_sha256
    ):
        raise EvaluationError("session staging inventory changed")
    return inventory


def offline_readiness(
    inputs: LocalInputs, preparation: SessionPreparation
) -> dict[str, object]:
    """Validate all inputs before a separate operator may reach a single create.

    No download/acquisition after rental is assumed: the four reviewed stage
    archives must already exist. Transfer time is explicitly reserved.
    """
    inputs = LocalInputs.model_validate_json(encoded(inputs.model_dump(mode="json")))
    preparation = SessionPreparation.model_validate_json(
        encoded(preparation.model_dump(mode="json"))
    )
    if (
        preparation.inputs_sha256
        != sha256_digest(encoded(inputs.model_dump(mode="json")))
        or preparation.source_commit != inputs.plan.workload.source_commit
    ):
        raise EvaluationError("session inputs/source binding changed")
    if tuple(a.role for a in preparation.archives) != (
        "source",
        "vllm",
        "sglang",
        "model",
    ):
        raise EvaluationError("session requires both runtimes, source and shared model")
    destination = Path(preparation.result_destination)
    if (
        destination.exists()
        or not destination.parent.is_dir()
        or destination.parent.resolve() != destination.parent
    ):
        raise EvaluationError(
            "session result destination must be new under a real parent"
        )
    recipe = Path(preparation.operator_recipe)
    if (
        recipe.resolve() != recipe
        or not recipe.is_file()
        or recipe.stat().st_size > 1024 * 1024
    ):
        raise EvaluationError("reviewed one-session operator recipe missing")
    if sha256_digest(recipe.read_bytes()) != preparation.operator_recipe_sha256:
        raise EvaluationError("one-session operator recipe digest changed")
    inventories: dict[str, list[list[str | int]]] = {
        a.role: verify_archive(a) for a in preparation.archives
    }
    # The model tar must be the exact published snapshot, not a named placeholder.
    model_manifest = qwen3_model_manifest()
    model_inventory = {str(row[0]): (row[1], row[2]) for row in inventories["model"]}
    expected_model = {
        f["path"]: (f["size_bytes"], f["sha256"]) for f in model_manifest["files"]
    }
    if model_inventory != expected_model:
        raise EvaluationError(
            "session model archive is not the complete pinned snapshot"
        )
    # Require reviewed runtime payloads to contain the exact executable bytes.
    for engine, runtime in (("vllm", inputs.vllm), ("sglang", inputs.sglang)):
        if not any(
            row[0] == runtime.python.lstrip("/") and row[2] == runtime.python_sha256
            for row in inventories[engine]
        ):
            raise EvaluationError(
                "session runtime executable absent from stage inventory"
            )
    config = inputs.plan.workload
    replay_seconds = (
        config.bounds.duration_ns
        + config.bounds.drain_ns
        + config.bounds.cleanup_timeout_ns
        + 999_999_999
    ) // 1_000_000_000
    worst = len(inputs.plan.order()) * (
        inputs.plan.prepare_seconds + replay_seconds + 20 + inputs.plan.cleanup_seconds
    )
    worst += (
        preparation.staging_seconds
        + preparation.retrieval_seconds
        + preparation.destruction_reserve_seconds
    )
    output_reserve = 64 * 1024**2
    # Reserve both archives and unpacked bytes simultaneously, plus result space.
    disk = (
        sum(
            a.unpacked_bytes + Path(a.path).stat().st_size for a in preparation.archives
        )
        + output_reserve
    )
    if (
        worst > preparation.max_session_seconds
        or disk > preparation.available_disk_bytes
    ):
        raise EvaluationError("one-session time or disk reservation is insufficient")
    launches = {
        engine: [launch_argv(runtime, inputs, i) for i in (0, 1)]
        for engine, runtime in (("vllm", inputs.vllm), ("sglang", inputs.sglang))
    }
    return {
        "status": "OFFLINE_INPUTS_COMPLETE_GPU_EXECUTION_UNVERIFIED",
        "inputs_sha256": preparation.inputs_sha256,
        "plan_sha256": inputs.plan.digest,
        "preparation_sha256": sha256_digest(
            encoded(preparation.model_dump(mode="json"))
        ),
        "engine_order": list(inputs.plan.order()),
        "launch_sha256": sha256_digest(encoded(launches)),
        "worst_case_seconds": worst,
        "disk_reserved_bytes": disk,
        "result_destination_sha256": sha256_digest(
            preparation.result_destination.encode()
        ),
        "max_creates": 1,
        "transition": preparation.transition,
        "provider_authorization": "SEPARATELY_REQUIRED",
        "provider_calls": 0,
    }
