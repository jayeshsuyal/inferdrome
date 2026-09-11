"""Injected host observations and fake shell commands, never a GPU/daemon."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from inferdrome.deployment.manual_host import prepare_artifacts
from inferdrome.deployment.manual_host_preflight import (
    check_allocation,
    check_gpu_inventory,
    check_host,
    check_image,
    verify_preparation,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_manual_host import (
    ROOT,
    h100_parsed,
    parsed,
    synthetic_input,
)


def inventory() -> bytes:
    return "\n".join(
        f"{gpu}, NVIDIA A100-PCIE-40GB, 40536, Disabled" for gpu in parsed().gpu_uuids
    ).encode()


def h100_inventory() -> bytes:
    """Documented synthetic H100 contract fixture, never GPU evidence."""

    return "\n".join(
        f"{gpu}, NVIDIA H100 80GB HBM3, 81559, Disabled"
        for gpu in h100_parsed().gpu_uuids
    ).encode()


def image_observation(role: str = "private-engine") -> dict[str, Any]:
    spec = parsed()
    return {
        "Id": sha256_digest(role.encode()),
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": [
            (
                spec.serving_image if role == "private-engine" else spec.runner_image
            ).reference
        ],
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": spec.source_commit,
                "com.inferdrome.runtime-role": role,
                "com.inferdrome.vllm-version": "0.26.0",
            }
        },
    }


def container_observation(index: int = 0) -> dict[str, Any]:
    spec = parsed()
    return {
        "Image": image_observation()["Id"],
        "State": {"Running": True},
        "HostConfig": {
            "DeviceRequests": [
                {
                    "Driver": "nvidia",
                    "DeviceIDs": [spec.gpu_uuids[index]],
                    "Count": 0,
                    "Capabilities": [["gpu"]],
                }
            ]
        },
        "Config": {
            "Cmd": ["--tensor-parallel-size", "1"],
            "Labels": {
                "com.docker.compose.project": spec.compose_project,
                "com.docker.compose.service": ("endpoint-a", "endpoint-b")[index],
            },
        },
    }


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing", "sxm4", "80gb", "mig", "wrong_uuid"]
)
def test_wrong_observed_gpu_inventory_is_rejected(mutation: str) -> None:
    value = inventory()
    if mutation == "duplicate":
        value = value.splitlines()[0] + b"\n" + value.splitlines()[0]
    elif mutation == "missing":
        value = value.splitlines()[0]
    elif mutation == "sxm4":
        value = value.replace(b"PCIE", b"SXM4")
    elif mutation == "80gb":
        value = value.replace(b"40536", b"81920")
    elif mutation == "mig":
        value = value.replace(b"Disabled", b"Enabled")
    else:
        value = value.replace(b"000000000002", b"000000000099")
    with pytest.raises(ValueError):
        check_gpu_inventory(parsed(), value)


def test_exact_gpu_inventory_and_image_observations() -> None:
    spec = parsed()
    check_gpu_inventory(spec, inventory())
    check_image(
        spec,
        spec.serving_image.reference,
        "private-engine",
        canonical_json_bytes(image_observation()),
    )
    value = image_observation()
    value["Config"]["Labels"]["org.opencontainers.image.revision"] = "0" * 40
    with pytest.raises(ValueError):
        check_image(
            spec,
            spec.serving_image.reference,
            "private-engine",
            canonical_json_bytes(value),
        )


@pytest.mark.parametrize(
    "mutation", ["duplicate", "mixed", "wrong_name", "wrong_memory", "mig"]
)
def test_h100_documented_synthetic_inventory_fails_closed(
    mutation: str,
) -> None:
    value = h100_inventory()
    if mutation == "duplicate":
        value = value.splitlines()[0] + b"\n" + value.splitlines()[0]
    elif mutation == "mixed":
        value = value.replace(
            b"NVIDIA H100 80GB HBM3, 81559",
            b"NVIDIA A100-PCIE-40GB, 40536",
            1,
        )
    elif mutation == "wrong_name":
        value = value.replace(b"NVIDIA H100 80GB HBM3", b"NVIDIA H100 NVL")
    elif mutation == "wrong_memory":
        value = value.replace(b"81559", b"81560")
    else:
        value = value.replace(b"Disabled", b"Enabled")
    with pytest.raises(ValueError):
        check_gpu_inventory(h100_parsed(), value)


def test_h100_documented_synthetic_inventory_and_single_gpu_allocation() -> None:
    spec = h100_parsed()
    check_gpu_inventory(spec, h100_inventory())
    check_allocation(
        spec,
        0,
        image_observation()["Id"],
        canonical_json_bytes(container_observation()),
    )


@pytest.mark.parametrize(
    "mutation",
    ["wrong_uuid", "both_gpus", "all_gpus", "tp2", "wrong_image", "wrong_project"],
)
def test_wrong_running_allocation_is_rejected(mutation: str) -> None:
    value = container_observation()
    device = value["HostConfig"]["DeviceRequests"][0]
    if mutation == "wrong_uuid":
        device["DeviceIDs"] = [parsed().gpu_uuids[1]]
    elif mutation == "both_gpus":
        device["DeviceIDs"] = list(parsed().gpu_uuids)
    elif mutation == "all_gpus":
        device["Count"] = -1
    elif mutation == "tp2":
        value["Config"]["Cmd"][-1] = "2"
    elif mutation == "wrong_image":
        value["Image"] = "wrong"
    else:
        value["Config"]["Labels"]["com.docker.compose.project"] = "someone-else"
    with pytest.raises(ValueError):
        check_allocation(
            parsed(), 0, image_observation()["Id"], canonical_json_bytes(value)
        )


def test_host_checks_are_injectable_and_do_not_adopt_existing_containers() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> bytes:
        calls.append(argv)
        if argv[0] == "nvidia-smi":
            return inventory()
        if argv[5:7] == ["image", "inspect"]:
            role = (
                "private-engine"
                if argv[-1] == parsed().serving_image.reference
                else "cpu-runner-observer"
            )
            return canonical_json_bytes(image_observation(role))
        if "--all" in argv:
            return b"old-container-id"
        return b""

    with pytest.raises(ValueError, match="unused"):
        check_host(parsed(), running=False, run=run, system="Linux", machine="x86_64")
    assert not any("run" in argv or "up" in argv or "down" in argv for argv in calls)
    calls.clear()
    with pytest.raises(ValueError, match="Linux"):
        check_host(parsed(), running=False, run=run, system="Darwin", machine="arm64")
    assert calls == []


def test_source_template_must_match_declared_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = parsed()
    template = tmp_path / "compose.gpu.yaml"
    template.write_bytes((ROOT / "compose.gpu.yaml").read_bytes() + b"\n# changed\n")
    monkeypatch.setattr(
        "inferdrome.deployment.manual_host.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=(ROOT / "compose.gpu.yaml").read_bytes()
        ),
    )
    with pytest.raises(ValueError, match="template"):
        prepare_artifacts(spec, tmp_path)


def test_preparation_readback_rejects_tampering_and_binds_exact_host_path(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "input"
    directory.mkdir(mode=0o700)
    value = synthetic_input()
    value["preparation_path"] = str(directory)
    files = prepare_artifacts(parsed(value), ROOT)
    for name, content in files.items():
        (directory / name).write_bytes(content)
    digest = sha256_digest(files["plan.json"])
    assert verify_preparation(directory, digest).instance_id == value["instance_id"]
    (directory / "cleanup-handoff.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="differs"):
        verify_preparation(directory, digest)


@pytest.mark.parametrize(
    "failure", ["none", "preflight", "start", "capture", "verify", "down"]
)
def test_startup_gates_and_failure_cleanup_with_fake_commands_only(
    tmp_path: Path, failure: str
) -> None:
    files = prepare_artifacts(parsed(), ROOT)
    script = tmp_path / "startup.sh"
    script.write_bytes(files["startup.sh"])
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "calls.log"
    fake_python = fakebin / "python3.12"
    fake_python.write_text(
        '#!/bin/bash\nprintf "preflight\\n" >> "$TEST_LOG"\n'
        '[[ "$TEST_FAILURE" != preflight ]]\n'
    )
    fake_docker = fakebin / "docker"
    fake_docker.write_text("""#!/bin/bash
kind=unknown
for arg in "$@"; do
  case "$arg" in
    up) kind=start;; run) kind=capture;; verify) kind=verify;; down) kind=down;;
  esac
done
printf '%s\\n' "$kind" >> "$TEST_LOG"
[[ "$TEST_FAILURE" != "$kind" ]]
""")
    fake_python.chmod(0o700)
    fake_docker.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(fakebin),
        "TEST_LOG": str(log),
        "TEST_FAILURE": failure,
    }
    argv = [
        "/bin/bash",
        str(script),
        "--execute-plan",
        sha256_digest(files["plan.json"]),
        "--operator",
        parsed().cleanup.accountable_operator,
        "--accept-manual-cleanup-risk",
    ]
    rejected = subprocess.run(
        [*argv[:3], "sha256:wrong", *argv[4:]], env=env, capture_output=True, timeout=5
    )
    assert rejected.returncode == 78 and not log.exists()
    result = subprocess.run(argv, env=env, capture_output=True, timeout=5)
    calls = log.read_text().splitlines()
    if failure == "preflight":
        assert calls == ["preflight"]
        assert result.returncode != 0 and b"cleanup still required" in result.stderr
    else:
        assert calls[-1] == "down"
        assert b"requested != verified" in result.stderr
        assert (result.returncode == 0) == (failure == "none")
        if failure == "start":
            assert "capture" not in calls
        if failure == "capture":
            assert "verify" not in calls
