"""Read-only host checks for separately authorized manual startup.

Never invoked by preparation. Commands are injectable for local tests. This
checks local driver/Docker observations, not Lambda ownership or termination.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inferdrome.deployment.manual_host import ManualHostInput, _read_input
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.executor import _read_source, _strict_json

Run = Callable[[list[str]], bytes]


def _run(argv: list[str]) -> bytes:
    return subprocess.run(
        argv,
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout


def check_gpu_inventory(spec: ManualHostInput, content: bytes) -> None:
    rows = [
        tuple(part.strip() for part in row.split(","))
        for row in content.decode("ascii").strip().splitlines()
    ]
    if len(rows) != 2 or any(len(row) != 4 for row in rows):
        raise ValueError("GPU inventory must contain exactly two devices")
    if {row[0] for row in rows} != set(spec.gpu_uuids):
        raise ValueError("observed GPU UUIDs disagree with the declared allocation")
    for _, name, memory, mig in rows:
        # Driver-reported usable MiB can be lower than nominal 40 GB. The exact
        # product name distinguishes PCIe from SXM4, not memory size alone.
        if name != spec.accelerator_model or not 39_000 <= int(memory) <= 41_000:
            raise ValueError("observed GPU variant or memory is unsupported")
        if mig != "Disabled":
            raise ValueError("manual campaign requires MIG disabled")


def check_image(
    spec: ManualHostInput, reference: str, role: str, content: bytes
) -> str:
    image: dict[str, Any] = json.loads(content)
    labels = image["Config"]["Labels"]
    if (
        reference not in image["RepoDigests"]
        or labels.get("org.opencontainers.image.revision") != spec.source_commit
        or labels.get("com.inferdrome.runtime-role") != role
        or labels.get("com.inferdrome.vllm-version") != spec.runtime.runtime_version
        or image["Os"] != "linux"
        or image["Architecture"] != "amd64"
    ):
        raise ValueError("preloaded image identity, role, platform or source differs")
    return str(image["Id"])


def check_allocation(
    spec: ManualHostInput,
    index: int,
    image_id: str,
    content: bytes,
) -> None:
    container: dict[str, Any] = json.loads(content)
    requests = container["HostConfig"]["DeviceRequests"]
    if (
        container["Image"] != image_id
        or not container["State"]["Running"]
        or len(requests) != 1
        or requests[0]["Driver"] != "nvidia"
        or requests[0]["DeviceIDs"] != [spec.gpu_uuids[index]]
        or requests[0]["Count"] != 0
        or requests[0]["Capabilities"] != [["gpu"]]
    ):
        raise ValueError("running engine image or distinct GPU allocation differs")
    command = container["Config"]["Cmd"]
    if command.count("--tensor-parallel-size") != 1 or (
        command[command.index("--tensor-parallel-size") + 1] != "1"
    ):
        raise ValueError("running engine tensor parallel size differs")
    labels = container["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.project") != spec.compose_project
        or labels.get("com.docker.compose.service")
        != ("endpoint-a", "endpoint-b")[index]
    ):
        raise ValueError("container does not belong to this exact Compose service")


def check_host(
    spec: ManualHostInput,
    *,
    running: bool,
    run: Run = _run,
    system: str | None = None,
    machine: str | None = None,
) -> None:
    if (system or platform.system()) != "Linux" or (
        machine or platform.machine()
    ) != "x86_64":
        raise ValueError("manual startup requires Linux x86_64")
    check_gpu_inventory(
        spec,
        run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,name,memory.total,mig.mode.current",
                "--format=csv,noheader,nounits",
            ]
        ),
    )
    run(["docker", "compose", "version"])
    image_ids = [
        check_image(
            spec,
            image.reference,
            role,
            run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    image.reference,
                ]
            ),
        )
        for image, role in (
            (spec.runner_image, "cpu-runner-observer"),
            (spec.serving_image, "private-engine"),
        )
    ]
    if image_ids[0] == image_ids[1]:
        raise ValueError("observed role image content is not distinct")
    prefix = [
        "docker",
        "compose",
        "--project-name",
        spec.compose_project,
        "--file",
        f"{spec.preparation_path}/compose.manual-host.json",
    ]
    if running:
        for index, endpoint in enumerate(("endpoint-a", "endpoint-b")):
            container_id = run([*prefix, "ps", "--quiet", endpoint]).decode().strip()
            if len(container_id) != 64 or any(
                c not in "0123456789abcdef" for c in container_id
            ):
                raise ValueError("one exact running container ID is required")
            check_allocation(
                spec,
                index,
                image_ids[1],
                run(
                    [
                        "docker",
                        "container",
                        "inspect",
                        "--format",
                        "{{json .}}",
                        container_id,
                    ]
                ),
            )
    elif run([*prefix, "ps", "--all", "--quiet"]).strip():
        raise ValueError("Compose project must be unused; do not adopt old containers")


def verify_preparation(directory: Path, expected_plan_digest: str) -> ManualHostInput:
    content = _read_source(directory / "plan.json", label="plan", maximum=65_536)
    if sha256_digest(content) != expected_plan_digest:
        raise ValueError("exact approved plan digest disagrees")
    plan = _strict_json(content, label="plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("input_files"), dict):
        raise ValueError("plan inventory is invalid")
    expected = {
        "operator-input.json",
        "deployment-config.json",
        "selected-workload.jsonl",
        "compose.manual-host.json",
        "cleanup-handoff.json",
    }
    if set(plan["input_files"]) != expected:
        raise ValueError("plan inventory is not closed")
    for name, digest in plan["input_files"].items():
        if (
            sha256_digest(
                _read_source(directory / name, label="plan input", maximum=65_536)
            )
            != digest
        ):
            raise ValueError("prepared file differs from approved plan")
    spec = _read_input(directory / "operator-input.json")
    if str(directory) != spec.preparation_path:
        raise ValueError("prepared host directory differs from declared path")
    if datetime.now(UTC) >= datetime.strptime(
        spec.cleanup.terminate_by_utc, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC):
        raise ValueError("cleanup handoff deadline has expired")
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expected-plan-digest", required=True)
    parser.add_argument("--check-running-allocation", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = verify_preparation(args.directory, args.expected_plan_digest)
        if sys.version_info[:2] != (3, 12) or os.getuid() != spec.uid:
            raise ValueError("host Python/UID must match the runner ownership boundary")
        check_host(spec, running=args.check_running_allocation)
        if not args.check_running_allocation:
            from inferdrome.vllm_compose import (
                _require_model_snapshot,
                validate_compose_identity,
            )

            validate_compose_identity(
                uid=spec.uid,
                gid=spec.gid,
                evidence_dir=spec.evidence_path,
                model_path=spec.model_path,
                experiment_dir=spec.preparation_path,
            )
            if (Path(spec.evidence_path) / "routing-execution-package").exists():
                raise ValueError("evidence package destination already exists")
            _require_model_snapshot(spec.model_path)
        print("Local host checks passed; provider ownership/lifecycle NOT verified.")
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ):
        print(
            "manual host preflight failed; operator VM cleanup still required",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
