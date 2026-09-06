"""Local-only preparation for an operator-supplied Lambda Linux host.

No provider client, credential reader, provisioning, retry, or execution API.
Startup requires explicit exact-plan/operator intent; generation is not authority.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.manual_host_docker import (
    DOCKER_CONFIG_FILE,
    EMPTY_DOCKER_CONFIG,
    LOCAL_DOCKER_HOST,
    docker_argv,
    docker_target,
)
from inferdrome.qwen3_campaign import (
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
    qwen3_workload_sha256,
)
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    input_transfer_digest,
    sha256_digest,
)
from inferdrome.routing_execution.contracts import (
    Commit,
    Digest,
    ExecutionModel,
    ImageIdentity,
    ModelIdentity,
    RoutingInputIdentity,
    RuntimeIdentity,
    SafeId,
    WorkloadIdentity,
    fixed_policy_ids,
    fixed_r1_input_digests,
    fixed_selected_workload_bytes,
    fixed_selected_workload_sha256,
    oci_content_digest,
)
from inferdrome.routing_execution.manual_host_contracts import ManualHostRoutingConfig
from inferdrome.routing_execution.topology import admit_topology

PROFILE_ID = "lambda-manual-two-a100-pcie-40gb-v1"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
InstanceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
GpuUuid = Annotated[str, Field(pattern=rf"^GPU-{_UUID}$")]
HostPath = Annotated[str, Field(pattern=r"^/[a-zA-Z0-9._/-]{1,240}$")]


class CleanupHandoff(ExecutionModel):
    instance_id: InstanceId
    accountable_operator: SafeId
    terminate_by_utc: str
    method: Literal["OPERATOR_EXACT_ID_TERMINATION_AND_READBACK"]
    guest_stop_ends_billing: Literal[False]
    termination_state: Literal["NOT_REQUESTED_NOT_VERIFIED"]
    lifecycle_protection: Literal["UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"]

    @model_validator(mode="after")
    def _deadline(self) -> CleanupHandoff:
        parsed = datetime.strptime(self.terminate_by_utc, "%Y-%m-%dT%H:%M:%SZ")
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != self.terminate_by_utc:
            raise ValueError("cleanup deadline must be an exact UTC timestamp")
        return self


class ManualHostInput(ExecutionModel):
    schema_version: Literal["inferdrome.manual-host-input.v1"]
    profile_id: Literal["lambda-manual-two-a100-pcie-40gb-v1"]
    provider: Literal["LAMBDA"]
    provisioning: Literal["OPERATOR_SUPPLIED_VM"]
    source_commit: Commit
    instance_id: InstanceId
    region: SafeId
    instance_type: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,127}$")]
    os: Literal["Linux"]
    architecture: Literal["x86_64"]
    accelerator_model: Literal["NVIDIA A100-PCIE-40GB"]
    gpu_uuids: tuple[GpuUuid, GpuUuid]
    tensor_parallel_size: Literal[1]
    uid: Annotated[int, Field(ge=1, le=65_534)]
    gid: Annotated[int, Field(ge=1, le=65_534)]
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    model_manifest_sha256: Digest
    model_snapshot_sha256: Digest
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    model_path: HostPath
    preparation_path: HostPath
    evidence_path: HostPath
    compose_project: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")]
    container_subnet: str
    endpoint_ipv4: tuple[str, str]
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    cleanup: CleanupHandoff

    @model_validator(mode="after")
    def _exact_inputs(self) -> ManualHostInput:
        if len(set(self.gpu_uuids)) != 2:
            raise ValueError("exactly two distinct GPU UUIDs are required")
        if self.cleanup.instance_id != self.instance_id:
            raise ValueError("cleanup exact instance ID disagrees")
        if oci_content_digest(self.runner_image) == oci_content_digest(
            self.serving_image
        ):
            raise ValueError("image role content digests must be distinct")
        pins = (
            self.source_commit,
            oci_content_digest(self.runner_image)[7:],
            oci_content_digest(self.serving_image)[7:],
        )
        if any(len(set(pin)) == 1 for pin in pins):
            raise ValueError("obvious identity placeholders are forbidden")
        if (
            self.model_manifest_sha256 != qwen3_model_manifest_sha256()
            or self.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
        ):
            raise ValueError("model snapshot pins disagree with the frozen model")
        paths = tuple(
            PurePosixPath(p)
            for p in (self.model_path, self.preparation_path, self.evidence_path)
        )
        for path in paths:
            if (
                len(path.parts) < 3
                or ".." in path.parts
                or str(path)
                not in (self.model_path, self.preparation_path, self.evidence_path)
            ):
                raise ValueError("host paths must be canonical scoped directories")
        if any(
            a == b or a in b.parents or b in a.parents
            for i, a in enumerate(paths)
            for b in paths[i + 1 :]
        ):
            raise ValueError("host model, preparation and evidence paths overlap")
        subnet = ipaddress.IPv4Network(self.container_subnet, strict=True)
        private = tuple(
            ipaddress.IPv4Network(n)
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        addresses = tuple(ipaddress.IPv4Address(a) for a in self.endpoint_ipv4)
        if (
            not any(subnet.subnet_of(n) for n in private)
            or not 24 <= subnet.prefixlen <= 28
            or len(set(addresses)) != 2
            or any(
                a not in subnet
                or int(a) - int(subnet.network_address) < 2
                or a == subnet.broadcast_address
                for a in addresses
            )
        ):
            raise ValueError("dedicated container subnet or endpoint IPs are invalid")
        return self


def input_template() -> dict[str, Any]:
    """An intentionally invalid template: unknown real values are JSON null."""
    r1 = fixed_r1_input_digests()
    return {
        "schema_version": "inferdrome.manual-host-input.v1",
        "profile_id": PROFILE_ID,
        "provider": "LAMBDA",
        "provisioning": "OPERATOR_SUPPLIED_VM",
        "source_commit": None,
        "instance_id": None,
        "region": None,
        "instance_type": None,
        "os": "Linux",
        "architecture": "x86_64",
        "accelerator_model": "NVIDIA A100-PCIE-40GB",
        "gpu_uuids": [None, None],
        "tensor_parallel_size": 1,
        "uid": None,
        "gid": None,
        "runner_image": {"reference": None},
        "serving_image": {"reference": None},
        "model": {
            "model_id": "Qwen/Qwen3-8B",
            "model_revision": QWEN3_8B_REVISION,
            "tokenizer_revision": QWEN3_8B_REVISION,
        },
        "runtime": {
            "runtime_name": "vllm",
            "runtime_version": "0.26.0",
            "adapter_id": "openai-compatible-routing-execution-v1",
            "adapter_version": "1.0.0",
        },
        "model_manifest_sha256": qwen3_model_manifest_sha256(),
        "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
        "routing_inputs": {
            "campaign_id": "routing-campaign-v1",
            "plan_sha256": r1["plan"],
            "trace_sha256": r1["trace"],
            "fault_schedule_sha256": r1["fault"],
            "trial_plan_sha256": r1["trial"],
            "policies": list(fixed_policy_ids()),
        },
        "workload": {
            "workload_id": "inferdrome.qwen-text-mixed-length.v1",
            "workload_sha256": qwen3_workload_sha256(),
            "selected_workload_sha256": fixed_selected_workload_sha256(),
            "selected_request_ids": [f"request-{i:03d}" for i in range(6)],
            "request_denominator": 6,
        },
        "model_path": None,
        "preparation_path": None,
        "evidence_path": None,
        "compose_project": None,
        "container_subnet": None,
        "endpoint_ipv4": [None, None],
        "request_timeout_ms": None,
        "cleanup": {
            "instance_id": None,
            "accountable_operator": None,
            "terminate_by_utc": None,
            "method": "OPERATOR_EXACT_ID_TERMINATION_AND_READBACK",
            "guest_stop_ends_billing": False,
            "termination_state": "NOT_REQUESTED_NOT_VERIFIED",
            "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
        },
    }


def routing_config(spec: ManualHostInput) -> ManualHostRoutingConfig:
    """Pure bridge, with declarations explicitly distinguished from proof."""
    value = spec.model_dump(mode="json")
    config: dict[str, Any] = {
        "schema_version": "inferdrome.routing-execution-config.v2",
        "execution_id": "routing-execution-v1",
        "mode": "LAMBDA_MANUAL_HOST",
        **{
            key: value[key]
            for key in (
                "source_commit",
                "runner_image",
                "serving_image",
                "model",
                "runtime",
                "routing_inputs",
                "workload",
                "request_timeout_ms",
            )
        },
        "endpoints": [
            {
                "endpoint_id": endpoint,
                "origin": f"http://{address}:8000",
                **{key: value[key] for key in ("model", "runtime", "serving_image")},
                "workload_sha256": spec.workload.workload_sha256,
                "capabilities": {
                    "health": "HTTP_HEALTH_V1",
                    "load": "VLLM_PROMETHEUS_V1",
                    "gpu_dcgm": "UNAVAILABLE",
                    "kv_cache": "UNAVAILABLE",
                },
            }
            for endpoint, address in zip(
                ("endpoint-a", "endpoint-b"), spec.endpoint_ipv4, strict=True
            )
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
            "profile_id": PROFILE_ID,
            "provider": "LAMBDA",
            "provisioning": "OPERATOR_SUPPLIED_VM",
            "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
            "lifecycle_protection": spec.cleanup.lifecycle_protection,
            "declaration_sha256": sha256_digest(canonical_json_bytes(value)),
            "host_identity_sha256": sha256_digest(
                canonical_json_bytes(
                    {
                        "instance_id": spec.instance_id,
                        "region": spec.region,
                        "instance_type": spec.instance_type,
                    }
                )
            ),
            "gpu_uuid_sha256": [sha256_digest(g.encode()) for g in spec.gpu_uuids],
            "runner_separate_from_serving": True,
            "serving_engine_count": 2,
            "one_engine_per_endpoint": True,
            "tensor_parallel_size": 1,
            "accelerator_model": spec.accelerator_model,
            "accelerator_count": 2,
        },
        "evidence_destination": {
            "destination_sha256": sha256_digest(spec.evidence_path.encode()),
            "declared_input_transfer_sha256": "sha256:" + "0" * 64,
            "publication_mode": "LOCAL_CREATE_NO_REPLACE_V1",
        },
        "no_retry": True,
    }
    # Zero is only the existing internal hash-domain projection, never an
    # operator-supplied pin or a value emitted in a completed artifact.
    transfer = input_transfer_digest(config, spec.workload.selected_workload_sha256)
    config["evidence_destination"]["declared_input_transfer_sha256"] = transfer
    parsed = ManualHostRoutingConfig.model_validate_json(canonical_json_bytes(config))
    admit_topology(parsed)
    return parsed


def compose_plan(
    spec: ManualHostInput, engine_template: dict[str, Any]
) -> dict[str, Any]:
    """Reuse compose.gpu.yaml's pinned serving arguments and hardening."""
    services: dict[str, Any] = {}
    common = {
        "platform": "linux/amd64",
        "pull_policy": "never",
        "user": f"{spec.uid}:{spec.gid}",
        "init": True,
        "read_only": True,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "restart": "no",
        "tmpfs": engine_template["tmpfs"],
        "environment": engine_template["environment"],
    }
    for index, endpoint in enumerate(("endpoint-a", "endpoint-b")):
        services[endpoint] = {
            **common,
            "image": spec.serving_image.reference,
            "entrypoint": ["vllm", "serve"],
            "command": engine_template["command"],
            "shm_size": engine_template["shm_size"],
            "healthcheck": engine_template["healthcheck"],
            "networks": {"campaign": {"ipv4_address": spec.endpoint_ipv4[index]}},
            "volumes": [
                {
                    "type": "bind",
                    "source": spec.model_path,
                    "target": "/models/qwen3-8b",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ],
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [
                            {
                                "driver": "nvidia",
                                "device_ids": [spec.gpu_uuids[index]],
                                "capabilities": ["gpu"],
                            }
                        ]
                    }
                }
            },
        }
    services["runner"] = {
        **common,
        "image": spec.runner_image.reference,
        "networks": ["campaign"],
        "environment": {
            **common["environment"],
            "NVIDIA_VISIBLE_DEVICES": "void",
            "CUDA_VISIBLE_DEVICES": "",
        },
        "entrypoint": [
            "/opt/inferdrome-runtime/bin/python",
            "-m",
            "inferdrome.routing_execution",
        ],
        "command": [
            "run",
            "--deployment-config",
            "/inputs/deployment-config.json",
            "--workload",
            "/inputs/selected-workload.jsonl",
            "--output",
            "/evidence/routing-execution-package",
        ],
        "depends_on": {
            e: {"condition": "service_healthy"} for e in ("endpoint-a", "endpoint-b")
        },
        "volumes": [
            {
                "type": "bind",
                "source": spec.preparation_path,
                "target": "/inputs",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": spec.evidence_path,
                "target": "/evidence",
                "bind": {"create_host_path": False},
            },
        ],
    }
    return {
        "name": spec.compose_project,
        "services": services,
        "networks": {
            "campaign": {
                "internal": True,
                "ipam": {
                    "config": [
                        {
                            "subnet": spec.container_subnet,
                        }
                    ]
                },
            }
        },
    }


def prepare_artifacts(spec: ManualHostInput, repository: Path) -> dict[str, bytes]:
    """Return deterministic artifacts without any host/provider/runtime probe."""
    template = (repository / "compose.gpu.yaml").read_bytes()
    # Bind exactly the consumed template bytes to the operator's source commit.
    # This is a local Git object read, not a fetch or a claim about image builds.
    committed = subprocess.run(
        ["git", "show", f"{spec.source_commit}:compose.gpu.yaml"],
        cwd=repository,
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout
    if committed != template:
        raise ValueError("consumed Compose template differs from declared source")
    engine = yaml.safe_load(template)["services"]["vllm-engine"]
    config = routing_config(spec)
    compose = compose_plan(spec, engine)
    prefix = docker_argv(
        spec.preparation_path,
        "compose",
        "--project-name",
        spec.compose_project,
        "--file",
        f"{spec.preparation_path}/compose.manual-host.json",
    )
    commands = {
        "start": [
            *prefix,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "600",
            "--pull",
            "never",
            "endpoint-a",
            "endpoint-b",
        ],
        "capture": [*prefix, "run", "--rm", "--no-deps", "runner"],
        "verify": [
            *prefix,
            "run",
            "--rm",
            "--no-deps",
            "runner",
            "verify",
            "/evidence/routing-execution-package",
        ],
        "stop_containers_only": [*prefix, "down", "--timeout", "30"],
    }
    cleanup = {
        **spec.cleanup.model_dump(mode="json"),
        "provider": "LAMBDA",
        "region": spec.region,
        "steps": [
            "Retain the sealed package and externally retain its printed digest.",
            "Stop only this Compose project; this does NOT end VM billing.",
            "Operator requests provider termination of this exact instance ID.",
            "Independently read back this exact ID until termination is verified.",
            "Requested/unknown/timeout/guest stop/container stop is NOT verified.",
            "Readback/handoff failure: escalate to the operator; billing may continue.",
        ],
        "provider_enforced_ttl": "NOT_ESTABLISHED",
        "provider_create_idempotency": "NOT_ESTABLISHED",
        "api_key_scope": "ALL_OPERATIONS_NO_KEY_ACCEPTED_HERE",
    }
    plan: dict[str, Any] = {
        "schema_version": "inferdrome.manual-host-plan.v1",
        "status": "LOCAL_PREPARATION_ONLY",
        "execute_authority": "NONE",
        "declaration_sha256": config.topology.declaration_sha256,
        "profile_id": PROFILE_ID,
        "docker_target": docker_target(spec.preparation_path),
        "compose_template_sha256": sha256_digest(template),
        "compose_template_source_commit": spec.source_commit,
        "availability_observation": None,
        "price_observation": None,
        "observed_host_proof": None,
        "lifecycle_protection": spec.cleanup.lifecycle_protection,
        "required_before_start": [
            "Separate execution authority and explicit manual lifecycle-risk decision.",
            "Exact-ID cleanup duty accepted; deadline is NOT an enforced TTL.",
            "Linux x86_64, Docker Compose and NVIDIA toolkit checked.",
            "Only the reviewed local /var/run/docker.sock daemon is supported.",
            "Exactly two A100 PCIe 40 GB; distinct UUIDs and MIG disabled.",
            "Container allocation matches UUIDs; one GPU per engine and TP=1.",
            "Preloaded distinct OCI role digests and source revision labels checked.",
            "Model/tokenizer bytes match frozen snapshot and manifest.",
            "Scoped paths have UID/GID access; container subnet has no host conflicts.",
        ],
        "commands_after_separate_authority": commands,
        "readiness": "Compose /health then runner /v1/models and telemetry admission",
        "capture_failure": (
            "Claim evidence only after sealing and offline verification; "
            "hand off cleanup on every exit."
        ),
        "reset": "NOT_ASSERTED_SEPARATE_SERVING_ENGINE",
        "qualification": "NOT_STALE_TELEMETRY_QUALIFICATION_V1",
    }
    artifacts = {
        DOCKER_CONFIG_FILE: EMPTY_DOCKER_CONFIG,
        "operator-input.json": canonical_json_bytes(spec.model_dump(mode="json")),
        "deployment-config.json": canonical_json_bytes(config.model_dump(mode="json")),
        "selected-workload.jsonl": fixed_selected_workload_bytes(),
        "compose.manual-host.json": canonical_json_bytes(compose),
        "cleanup-handoff.json": canonical_json_bytes(cleanup),
    }
    plan["input_files"] = {
        name: sha256_digest(content) for name, content in sorted(artifacts.items())
    }
    artifacts["plan.json"] = canonical_json_bytes(plan)
    plan_digest = sha256_digest(artifacts["plan.json"])
    host_check = [
        "python3.12",
        "-m",
        "inferdrome.deployment.manual_host_preflight",
        "--directory",
        spec.preparation_path,
        "--expected-plan-digest",
        plan_digest,
    ]
    script = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# Generation is NOT execution authority. Obtain separate operator approval.",
        'if [[ "$#" -ne 5 || "$1" != --execute-plan ||',
        f'      "$2" != {shlex.quote(plan_digest)} || "$3" != --operator ||',
        f'      "$4" != {shlex.quote(spec.cleanup.accountable_operator)} ||',
        '      "$5" != --accept-manual-cleanup-risk ]]; then',
        "  echo 'Exact plan/operator control and manual risk decision required' >&2",
        "  exit 78",
        "fi",
        # Clear selectors before preflight and every lifecycle edge. Child
        # commands cannot alter this parent shell environment during cleanup.
        "for routing_variable in ${!DOCKER_@} ${!COMPOSE_@}; do",
        '  unset "$routing_variable"',
        "done",
        f"export DOCKER_HOST={shlex.quote(LOCAL_DOCKER_HOST)}",
        f"export DOCKER_CONFIG={shlex.quote(spec.preparation_path)}",
        "export COMPOSE_DISABLE_ENV_FILE=1",
        "cleanup() {",
        "  result=$?",
        f"  {shlex.join(commands['stop_containers_only'])} || result=74",
        "  echo 'VM billing may continue; termination requested != verified.' >&2",
        "  echo 'Follow cleanup-handoff.json; escalate failures to its operator.' >&2",
        '  exit "$result"',
        "}",
        # Validate host/input before any container mutation. Even preflight
        # failure requires the already-provided VM's operator cleanup handoff.
        "trap 'echo \"Preflight failed: operator VM cleanup still required\" >&2' EXIT",
        shlex.join(host_check),
        "trap cleanup EXIT",
        shlex.join(commands["start"]),
        shlex.join([*host_check, "--check-running-allocation"]),
        shlex.join(commands["capture"]),
        shlex.join(commands["verify"]),
    ]
    artifacts["startup.sh"] = ("\n".join(script) + "\n").encode()
    artifacts["preparation-integrity.json"] = canonical_json_bytes(
        {
            "schema_version": "inferdrome.manual-host-preparation-integrity.v1",
            "assertion": "LOCAL_PREPARATION_NOT_EXECUTION_EVIDENCE",
            "files": {
                name: sha256_digest(content)
                for name, content in sorted(artifacts.items())
            },
        }
    )
    return artifacts


def _read_input(path: Path) -> ManualHostInput:
    # Reuse the runner's bounded no-follow source read and duplicate-key check.
    from inferdrome.routing_execution.executor import _read_source, _strict_json

    content = _read_source(path, label="manual host input", maximum=65_536)
    _strict_json(content, label="manual host input")
    return ManualHostInput.model_validate_json(content)


def _publish(output: Path, artifacts: dict[str, bytes]) -> None:
    """Private create-only local preparation, not an evidence seal."""
    parent = SafeDirFD.open(output.absolute().parent)
    child: SafeDirFD | None = None
    try:
        os.mkdir(output.name, 0o700, dir_fd=parent.fd)
        child = SafeDirFD.open(output.absolute())
        for name, content in artifacts.items():
            descriptor = child.open_child(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # Integrity is written last. On failure retain the partial private
        # directory for inspection; never overwrite/retry into it.
    finally:
        if child is not None:
            child.close()
        parent.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m inferdrome.deployment.manual_host")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("template", help="print invalid template with unresolved nulls")
    prepare = commands.add_parser(
        "prepare", help="validate and render locally; never execute"
    )
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            print(json.dumps(input_template(), indent=2, sort_keys=True))
        else:
            spec = _read_input(args.input)
            artifacts = prepare_artifacts(spec, Path(__file__).resolve().parents[3])
            _publish(args.output, artifacts)
            print(
                json.dumps(
                    {
                        "status": "LOCAL_PREPARATION_ONLY",
                        "plan_sha256": sha256_digest(artifacts["plan.json"]),
                        "preparation_sha256": sha256_digest(
                            artifacts["preparation-integrity.json"]
                        ),
                    }
                )
            )
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        print(
            "manual host preparation rejected; check inputs and create-only output",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
