"""Private, create-only preparation for one explicitly declared Vast container.

No provider, credential, network, Docker or GPU operation occurs here. The
single image and provider readback are declarations, not OCI attestations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from inferdrome.deployment.manual_host import H100_PROFILE_ID, input_template
from inferdrome.errors import InferdromeError
from inferdrome.qwen3_campaign import (
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    Commit,
    Digest,
    ExecutionModel,
    ImageIdentity,
)
from inferdrome.routing_execution.vast_contracts import VastRoutingConfig

MODULES = (
    "vast_process",
    "vast_process_runtime",
    "vast_process_observer",
    "vast_bootstrap",
    "vast_control",
    "vast_transfer",
)
GPUUUID = Annotated[
    str, Field(pattern=r"^GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
]
ProviderId = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
SafeOperator = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]


class VastCleanup(ExecutionModel):
    provider: Literal["VAST_AI"] = "VAST_AI"
    instance_id: ProviderId
    accountable_operator: SafeOperator
    terminate_by_utc: str
    method: Literal["EXTERNAL_EXACT_ID_DESTROY_AND_ABSENCE_READBACK"]
    state: Literal["NOT_REQUESTED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def _deadline(self) -> VastCleanup:
        parse_deadline(self.terminate_by_utc)
        return self


class VastLaunchReadback(ExecutionModel):
    """Allowlisted owner input, never a raw credential-bearing API dump."""

    instance_id: ProviderId
    requested_image: ImageIdentity
    launch_mode: Literal["args"]
    user: Literal["2000:0"]
    public_port_mappings: tuple[()]
    persistent_volume_ids: tuple[()]
    assertion: Literal["OPERATOR_SUPPLIED_NOT_PROVIDER_ATTESTED"]
    resolved_image_digest: None = None


class VastProcessInput(ExecutionModel):
    schema_version: Literal["inferdrome.vast-process-input.v1"]
    provider: Literal["VAST_AI"]
    source_commit: Commit
    instance_id: ProviderId
    offer_id: ProviderId
    container_image: ImageIdentity
    launch_readback: VastLaunchReadback
    gpu_uuids: tuple[GPUUUID, GPUUUID]
    uid: Literal[2000]
    gid: Literal[0]
    model_path: str
    preparation_path: str
    evidence_path: str
    cache_path: str
    module_sha256: dict[str, Digest]
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    readiness_timeout_seconds: Annotated[int, Field(ge=1, le=600)]
    campaign_timeout_seconds: Annotated[int, Field(ge=1, le=1800)]
    cleanup: VastCleanup

    @model_validator(mode="after")
    def _bindings(self) -> VastProcessInput:
        if len(set(self.gpu_uuids)) != 2:
            raise ValueError("two distinct GPU UUIDs are required")
        if (
            self.instance_id != self.cleanup.instance_id
            or self.instance_id != self.launch_readback.instance_id
            or self.container_image != self.launch_readback.requested_image
            or set(self.module_sha256) != set(MODULES)
        ):
            raise ValueError("Vast input identities disagree")
        paths = [
            self.model_path,
            self.preparation_path,
            self.evidence_path,
            self.cache_path,
        ]
        for value in paths:
            if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", value):
                raise ValueError("host paths must be bounded absolute paths")
            if str(Path(value)) != value or ".." in Path(value).parts:
                raise ValueError("host paths must be canonical")
        if any(
            a == b or Path(a).is_relative_to(b)
            for i, a in enumerate(paths)
            for j, b in enumerate(paths)
            if i != j
        ):
            raise ValueError("model, preparation, evidence and cache must not overlap")
        return self


def parse_deadline(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("deadline must be canonical UTC")
    return parsed


def module_digests() -> dict[str, str]:
    return {
        name: sha256_digest(Path(__file__).with_name(f"{name}.py").read_bytes())
        for name in MODULES
    }


def template() -> dict[str, Any]:
    """Unknown live values are deliberately invalid nulls."""
    return {
        "schema_version": "inferdrome.vast-process-input.v1",
        "provider": "VAST_AI",
        "source_commit": None,
        "instance_id": None,
        "offer_id": None,
        "container_image": {"reference": None},
        "launch_readback": {
            "instance_id": None,
            "requested_image": {"reference": None},
            "launch_mode": "args",
            "user": "2000:0",
            "public_port_mappings": [],
            "persistent_volume_ids": [],
            "assertion": "OPERATOR_SUPPLIED_NOT_PROVIDER_ATTESTED",
            "resolved_image_digest": None,
        },
        "gpu_uuids": [None, None],
        "uid": 2000,
        "gid": 0,
        "model_path": None,
        "preparation_path": None,
        "evidence_path": None,
        "cache_path": None,
        "module_sha256": {name: None for name in MODULES},
        "request_timeout_ms": None,
        "readiness_timeout_seconds": 600,
        "campaign_timeout_seconds": None,
        "cleanup": {
            "provider": "VAST_AI",
            "instance_id": None,
            "accountable_operator": None,
            "terminate_by_utc": None,
            "method": "EXTERNAL_EXACT_ID_DESTROY_AND_ABSENCE_READBACK",
            "state": "NOT_REQUESTED_NOT_VERIFIED",
        },
    }


def read_private(path: Path, maximum: int = 131_072) -> bytes:
    """Reject links, non-regular files, oversized data and changing reads."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise ValueError("private input file is invalid")
        content = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(content) != metadata.st_size or any(
            getattr(after, key) != getattr(metadata, key) for key in fields
        ):
            raise ValueError("private input changed during read")
        return content
    finally:
        os.close(descriptor)


def write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)


def prepare(spec: VastProcessInput, output: Path) -> str:
    if str(output.absolute()) != spec.preparation_path:
        raise ValueError("preparation path must match the declared destination")
    content = canonical_json_bytes(spec.model_dump(mode="json"))
    digest = sha256_digest(content)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_private(output / "plan.json", content)
    return digest


def load_plan(directory: Path, expected_digest: str) -> VastProcessInput:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("preparation directory is invalid")
    content = read_private(directory / "plan.json")
    if sha256_digest(content) != expected_digest:
        raise ValueError("plan digest differs")
    spec = VastProcessInput.model_validate_json(content)
    if (
        canonical_json_bytes(spec.model_dump(mode="json")) != content
        or str(directory.absolute()) != spec.preparation_path
    ):
        raise ValueError("plan is not canonical or bound to this directory")
    return spec


def routing_config(
    spec: VastProcessInput, observation: dict[str, Any]
) -> VastRoutingConfig:
    """Bind sanitized local observations without upgrading OCI declarations."""
    from inferdrome.routing_execution.executor import declared_input_transfer_digest

    fixed = input_template(H100_PROFILE_ID)
    provenance = {
        "container_image_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
        "source_commit": spec.source_commit,
        "observer_artifact_sha256": spec.module_sha256["vast_process_observer"],
        "supervisor_artifact_sha256": spec.module_sha256["vast_process_runtime"],
        "model_manifest_sha256": qwen3_model_manifest_sha256(),
        "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
        "runtime_observation_sha256": sha256_digest(canonical_json_bytes(observation)),
        "runtime_assertion": "LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION",
    }
    value: dict[str, Any] = {
        "schema_version": "inferdrome.routing-execution-config.v4",
        "execution_id": "routing-execution-v1",
        "mode": "VAST_MANUAL_CONTAINER",
        "source_commit": spec.source_commit,
        "container_image": spec.container_image.model_dump(mode="json"),
        **{
            key: fixed[key]
            for key in ("model", "runtime", "routing_inputs", "workload")
        },
        "artifact_provenance": provenance,
        "endpoints": [
            {
                "endpoint_id": endpoint,
                "origin": f"http://127.0.0.1:{port}",
                "model": fixed["model"],
                "runtime": fixed["runtime"],
                "container_image": spec.container_image.model_dump(mode="json"),
                "workload_sha256": fixed["workload"]["workload_sha256"],
                "capabilities": {
                    "health": "HTTP_HEALTH_V1",
                    "load": "VLLM_PROMETHEUS_V1",
                    "gpu_dcgm": "UNAVAILABLE",
                    "kv_cache": "UNAVAILABLE",
                },
            }
            for endpoint, port in (("endpoint-a", 8000), ("endpoint-b", 8001))
        ],
        "telemetry": {
            "clock_domain": "RUNNER_MONOTONIC_NS",
            "health_freshness_ms": 5,
            "load_freshness_ms": 5,
            "gpu_freshness_ms": 5,
            "load_metric_name": "vllm:num_requests_running",
        },
        "fault": {
            "fault_id": "stale-load-fresh-health-v1",
            "load_collection_pause_after_sequence_index": 1,
            "health_collection_continues": True,
            "inter_request_interval_ms": 10,
        },
        "topology": {
            "profile_id": "vast-container-two-h100-sxm5-80gb-v1",
            "provider": "VAST_AI",
            "provisioning": "OPERATOR_SUPPLIED_CONTAINER",
            "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
            "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
            "declaration_sha256": sha256_digest(
                canonical_json_bytes(spec.model_dump(mode="json"))
            ),
            "instance_identity_sha256": sha256_digest(
                canonical_json_bytes(
                    {"provider": "VAST_AI", "instance_id": spec.instance_id}
                )
            ),
            "gpu_uuid_sha256": [sha256_digest(gpu.encode()) for gpu in spec.gpu_uuids],
            "container_count": 1,
            "serving_engine_count": 2,
            "one_engine_per_endpoint": True,
            "tensor_parallel_size": 1,
            "accelerator_model": "NVIDIA H100-SXM5-80GB",
            "accelerator_count": 2,
            "isolation_boundary": "SEPARATE_PROCESSES_SHARED_CONTAINER",
            "observer_gpu_isolation": "ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED",
        },
        "evidence_destination": {
            "destination_sha256": sha256_digest(spec.evidence_path.encode()),
            "declared_input_transfer_sha256": "sha256:" + "0" * 64,
            "publication_mode": "LOCAL_CREATE_NO_REPLACE_V1",
        },
        "request_timeout_ms": spec.request_timeout_ms,
        "no_retry": True,
    }
    config = VastRoutingConfig.model_validate_json(canonical_json_bytes(value))
    value["evidence_destination"]["declared_input_transfer_sha256"] = (
        declared_input_transfer_digest(config)
    )
    return VastRoutingConfig.model_validate_json(canonical_json_bytes(value))


class DestroyReadback(ExecutionModel):
    """Sanitized controller observations; this pure validator performs no API call."""

    provider: Literal["VAST_AI"]
    instance_id: ProviderId
    destroy_acknowledged: bool
    destroy_requested_at_utc: str
    readback_at_utc: str
    query_instance_id: ProviderId
    readback_succeeded: bool
    matching_instance_ids: tuple[ProviderId, ...]
    pagination_exhausted: bool
    persistent_volume_ids: tuple[ProviderId, ...]


def cleanup_state(expected_id: int, receipt: DestroyReadback | None) -> str:
    """Neither guest exit nor an unsuccessful/partial listing confirms cleanup."""
    if receipt is None:
        return "CLEANUP_UNCONFIRMED"
    try:
        valid_time = parse_deadline(receipt.readback_at_utc) >= parse_deadline(
            receipt.destroy_requested_at_utc
        )
    except ValueError:
        valid_time = False
    if (
        receipt.instance_id == expected_id == receipt.query_instance_id
        and receipt.destroy_acknowledged
        and receipt.readback_succeeded
        and not receipt.matching_instance_ids
        and receipt.pagination_exhausted
        and not receipt.persistent_volume_ids
        and valid_time
    ):
        return "OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED"
    return "CLEANUP_UNCONFIRMED"


def export_package(package: Path, output: Path, expected_digest: str) -> str:
    """Export only revalidated canonical records, never a directory/archive crawl."""
    from inferdrome.routing_execution.package import (
        EvidenceReservation,
        make_integrity_manifest,
        verify_execution_package,
    )

    verified = verify_execution_package(package, expected_digest=expected_digest)
    payloads = {
        "executed-manifest.json": canonical_json_bytes(
            verified.executed_manifest.model_dump(mode="json")
        ),
        "input-transfer-receipt.json": canonical_json_bytes(
            verified.input_transfer.model_dump(mode="json")
        ),
        "producer-receipt.json": canonical_json_bytes(
            verified.producer_receipt.model_dump(mode="json")
        ),
    }
    integrity = make_integrity_manifest(
        verified.executed_manifest.execution_id, payloads
    )
    reservation = EvidenceReservation.reserve(output)
    try:
        sealed = reservation.publish(payloads, integrity)
    finally:
        reservation.close()
    if sealed.retained_digest != expected_digest:
        raise ValueError("export identity differs")
    return sealed.retained_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("template")
    sub.add_parser("module-digests")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--input", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    export_parser = sub.add_parser("export")
    export_parser.add_argument("--package", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--expected-digest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            print(json.dumps(template(), indent=2))
        elif args.command == "module-digests":
            print(json.dumps(module_digests(), sort_keys=True))
        elif args.command == "export":
            print(
                json.dumps(
                    {
                        "retained_digest": export_package(
                            args.package, args.output, args.expected_digest
                        )
                    }
                )
            )
        else:
            spec = VastProcessInput.model_validate_json(read_private(args.input))
            print(json.dumps({"plan_sha256": prepare(spec, args.output)}))
        return 0
    except (OSError, ValueError, ValidationError, InferdromeError):
        print("Vast preparation rejected; no execution or cleanup is claimed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
