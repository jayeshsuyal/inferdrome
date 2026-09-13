"""Bounded child-process supervisor; never a Vast API or billing controller.

Engines and observer share a UID, filesystem and container GPU device access.
The observer's empty CUDA visibility is configuration, not enforced isolation.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from inferdrome.deployment.gcp_private_engine_adapter import (
    _vllm_python,
    observed_vllm_version,
)
from inferdrome.deployment.vast_process import (
    VastProcessInput,
    load_plan,
    module_digests,
    parse_deadline,
    read_private,
    routing_config,
    write_private,
)
from inferdrome.errors import InferdromeError
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import fixed_selected_workload_bytes
from inferdrome.routing_execution.package import verify_execution_package
from inferdrome.vllm_compose import _require_model_snapshot

_BUILD_MARKER = Path("/opt/inferdrome-vast-build.json")
_CLEANUP_RESERVE_SECONDS = 20.0


class RuntimeFailure(ValueError):
    """Messages are fixed codes, never provider data, prompts or child logs."""


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[Sequence[str], Mapping[str, str]], Child]


def child_environment(
    spec: VastProcessInput, *, gpu_uuid: str | None
) -> dict[str, str]:
    """Construct from a closed inventory; never inherit tokens/proxy/agent state."""
    return {
        "PATH": (
            "/opt/inferdrome-runtime/bin:/usr/local/bin:/usr/local/cuda/bin:"
            "/usr/local/nvidia/bin:/usr/bin:/bin"
        ),
        "HOME": spec.cache_path,
        "TMPDIR": spec.cache_path,
        "XDG_CACHE_HOME": spec.cache_path,
        "HF_HOME": spec.cache_path,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": gpu_uuid or "",
        "NVIDIA_VISIBLE_DEVICES": gpu_uuid or "void",
        "LD_LIBRARY_PATH": (
            "/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64"
        ),
        "LANG": "C.UTF-8",
    }


def engine_argv(spec: VastProcessInput, index: int, python: str) -> tuple[str, ...]:
    if index not in (0, 1):
        raise RuntimeFailure("VAST_ENGINE_INDEX_INVALID")
    return (
        python,
        "-I",
        "/usr/local/bin/vllm",
        "serve",
        spec.model_path,
        "--host",
        "127.0.0.1",
        "--port",
        str(8000 + index),
        "--served-model-name",
        "Qwen/Qwen3-8B",
        "--tokenizer",
        spec.model_path,
        "--tokenizer-mode",
        "auto",
        "--dtype",
        "bfloat16",
        "--seed",
        "42",
        "--load-format",
        "safetensors",
        "--generation-config",
        "vllm",
        "--model-impl",
        "vllm",
        "--max-model-len",
        "2048",
        "--gpu-memory-utilization",
        "0.90",
        "--tensor-parallel-size",
        "1",
        "--no-enable-log-requests",
        "--disable-uvicorn-access-log",
        "--uvicorn-log-level",
        "warning",
    )


def spawn(argv: Sequence[str], environment: Mapping[str, str]) -> Child:
    return subprocess.Popen(
        tuple(argv),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@contextmanager
def _defer_cleanup_interrupts() -> Iterator[None]:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def stop_child(child: Child) -> None:
    """Signal the owned process group even if its leader has already exited."""
    with _defer_cleanup_interrupts():
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=3)
        # A terminated group leader does not imply its workers exited.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)


def supervise(
    commands: Sequence[tuple[Sequence[str], Mapping[str, str]]],
    observer: Callable[[Sequence[Child]], tuple[Sequence[str], Mapping[str, str]]],
    ready: Callable[[float], bool],
    *,
    readiness_seconds: float,
    campaign_seconds: float,
    total_seconds: float,
    spawn_child: Spawn = spawn,
    stop: Callable[[Child], None] = stop_child,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """No retry, bounded readiness/capture, cleanup on every terminal path."""
    children: list[Child] = []
    deadline = monotonic() + total_seconds
    try:
        for argv, environment in commands:
            if monotonic() >= deadline:
                raise RuntimeFailure("VAST_RUN_DEADLINE")
            children.append(spawn_child(argv, environment))
        ready_deadline = min(deadline, monotonic() + readiness_seconds)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeFailure("VAST_ENGINE_DIED")
            if monotonic() >= ready_deadline:
                raise RuntimeFailure("VAST_READINESS_TIMEOUT")
            is_ready = ready(ready_deadline - monotonic())
            if monotonic() >= ready_deadline:
                raise RuntimeFailure("VAST_READINESS_TIMEOUT")
            if is_ready:
                break
            sleep(min(0.1, max(0.0, ready_deadline - monotonic())))
        argv, environment = observer(children)
        if monotonic() >= deadline:
            raise RuntimeFailure("VAST_RUN_DEADLINE")
        capture = spawn_child(argv, environment)
        children.append(capture)
        capture_deadline = min(deadline, monotonic() + campaign_seconds)
        while True:
            if any(child.poll() is not None for child in children[:-1]):
                raise RuntimeFailure("VAST_ENGINE_DIED")
            status = capture.poll()
            if monotonic() >= capture_deadline:
                raise RuntimeFailure("VAST_CAMPAIGN_TIMEOUT")
            if status is not None:
                if status != 0:
                    raise RuntimeFailure("VAST_OBSERVER_FAILED")
                return
            sleep(min(0.1, max(0.0, capture_deadline - monotonic())))
    finally:
        failed = False
        with _defer_cleanup_interrupts():
            for child in reversed(children):
                try:
                    stop(child)
                except (OSError, subprocess.SubprocessError, RuntimeFailure):
                    failed = True
        if failed:
            raise RuntimeFailure("VAST_PROCESS_CLEANUP_UNCONFIRMED")


def check_gpu_observation(spec: VastProcessInput, content: str) -> list[dict[str, str]]:
    rows = [row.split(",") for row in content.strip().splitlines()]
    if len(rows) != 2 or any(len(row) != 5 for row in rows):
        raise RuntimeFailure("VAST_GPU_MISMATCH")
    observed: dict[str, dict[str, str]] = {}
    for row in rows:
        uuid, name, memory, mig, driver = (value.strip() for value in row)
        if (
            uuid not in spec.gpu_uuids
            or uuid in observed
            or name != "NVIDIA H100 80GB HBM3"
            or memory != "81559"
            or mig != "Disabled"
        ):
            raise RuntimeFailure("VAST_GPU_MISMATCH")
        if (
            not driver
            or len(driver) > 64
            or any(c not in "0123456789." for c in driver)
        ):
            raise RuntimeFailure("VAST_DRIVER_IDENTITY_INVALID")
        observed[uuid] = {
            "gpu_uuid_sha256": sha256_digest(uuid.encode()),
            "name": name,
            "memory_mib": memory,
            "mig": mig,
            "driver_version": driver,
        }
    if len({row["driver_version"] for row in observed.values()}) != 1:
        raise RuntimeFailure("VAST_DRIVER_IDENTITY_INVALID")
    return [observed[uuid] for uuid in spec.gpu_uuids]


def remaining_seconds(spec: VastProcessInput) -> float:
    remaining = (
        parse_deadline(spec.cleanup.terminate_by_utc) - datetime.now(UTC)
    ).total_seconds()
    if remaining <= 0:
        raise RuntimeFailure("VAST_RUN_DEADLINE")
    return remaining


def active_seconds(spec: VastProcessInput) -> float:
    remaining = remaining_seconds(spec) - _CLEANUP_RESERVE_SECONDS
    if remaining <= 0:
        raise RuntimeFailure("VAST_RUN_DEADLINE")
    return remaining


def verify_build(spec: VastProcessInput, marker: dict[str, Any]) -> None:
    if (
        marker
        != {"source_commit": spec.source_commit, "module_sha256": spec.module_sha256}
        or module_digests() != spec.module_sha256
    ):
        raise RuntimeFailure("VAST_INSTALLED_ARTIFACT_MISMATCH")


def require_process_paths(spec: VastProcessInput) -> None:
    """Vast's explicit 2000:0 process identity has its own access contract."""
    for value in (
        spec.model_path,
        spec.preparation_path,
        spec.evidence_path,
        spec.cache_path,
    ):
        path = Path(value)
        access = os.R_OK | os.X_OK
        if value != spec.model_path:
            access |= os.W_OK
        if (
            not path.is_dir()
            or path.resolve() != path
            or path.stat().st_uid != spec.uid
            or not os.access(path, access)
        ):
            raise RuntimeFailure("VAST_PATH_IDENTITY_MISMATCH")
    if any(Path(spec.evidence_path).iterdir()):
        raise RuntimeFailure("VAST_EVIDENCE_NOT_EMPTY")


def _gpu_probe(spec: VastProcessInput, environment: Mapping[str, str]) -> bytes:
    """Keep the bounded probe inside preflight's group, including on hard kill."""
    deadline = time.monotonic() + min(20, remaining_seconds(spec))
    child = subprocess.Popen(
        (
            "/usr/bin/nvidia-smi",
            "--query-gpu=uuid,name,memory.total,mig.mode.current,driver_version",
            "--format=csv,noheader,nounits",
        ),
        env=dict(environment),
        cwd=spec.preparation_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        # The outer supervisor owns this whole group. Do not detach this probe.
        start_new_session=False,
    )
    content = bytearray()
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeFailure("VAST_GPU_PROBE_TIMEOUT")
                if not selector.select(min(0.1, remaining)):
                    continue
                block = os.read(child.stdout.fileno(), 4097 - len(content))
                if not block:
                    break
                content.extend(block)
                if len(content) > 4096:
                    raise RuntimeFailure("VAST_GPU_MISMATCH")
        if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise RuntimeFailure("VAST_GPU_MISMATCH")
        if time.monotonic() >= deadline:
            raise RuntimeFailure("VAST_GPU_PROBE_TIMEOUT")
        return bytes(content)
    finally:
        with _defer_cleanup_interrupts():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()


def preflight(spec: VastProcessInput) -> dict[str, Any]:
    remaining_seconds(spec)
    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or sys.version_info[:2] != (3, 12)
        or os.getuid() != spec.uid
        or os.getgid() != spec.gid
    ):
        raise RuntimeFailure("VAST_HOST_IDENTITY_MISMATCH")
    verify_build(spec, json.loads(read_private(_BUILD_MARKER)))
    require_process_paths(spec)
    _require_model_snapshot(spec.model_path)
    environment = child_environment(spec, gpu_uuid=None)
    gpu_rows = check_gpu_observation(
        spec, _gpu_probe(spec, environment).decode("ascii")
    )
    python = _vllm_python()
    version = observed_vllm_version(python=python, environment=environment)
    for port in (8000, 8001):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
    remaining_seconds(spec)
    return {
        "schema_version": "inferdrome.vast-local-observation.v1",
        "source_commit_assertion": "OPERATOR_DECLARED_BUILD_MARKER",
        "module_sha256": spec.module_sha256,
        "uid": spec.uid,
        "gid": spec.gid,
        "os": "Linux",
        "architecture": "x86_64",
        "runtime_version": version,
        "gpu_observations": gpu_rows,
        "model_verification": "FROZEN_SNAPSHOT_AND_TOKENIZER_VERIFIED",
        "container_image_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
        "process_allocation_assertion": (
            "UUID_BOUND_CHILD_ENVIRONMENT_NOT_DRIVER_PROCESS_ATTESTATION"
        ),
    }


def _health_probe() -> bool:
    for port in (8000, 8001):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            if response.status != 200 or len(response.read(1025)) > 1024:
                return False
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
    return True


def _ready(environment: Mapping[str, str], seconds: float) -> bool:
    """A trickling HTTP response cannot block the supervising process."""
    if seconds <= 0:
        return False
    child = spawn(
        (
            sys.executable,
            "-I",
            "-m",
            "inferdrome.deployment.vast_process_runtime",
            "probe",
        ),
        environment,
    )
    try:
        try:
            return child.wait(timeout=min(1.2, seconds)) == 0
        except subprocess.TimeoutExpired:
            return False
    finally:
        stop_child(child)


def _stage(argv: Sequence[str], environment: Mapping[str, str], seconds: float) -> None:
    child = spawn(argv, environment)
    try:
        if child.wait(timeout=seconds) != 0:
            raise RuntimeFailure("VAST_PREFLIGHT_FAILED")
    finally:
        stop_child(child)


def execute(
    directory: Path, digest: str, operator: str, accepted: bool
) -> dict[str, str]:
    spec = load_plan(directory, digest)
    if not accepted or operator != spec.cleanup.accountable_operator:
        raise RuntimeFailure("VAST_EXACT_PLAN_APPROVAL_REQUIRED")
    active_seconds(spec)
    write_private(
        directory / "execution-attempt.json",
        canonical_json_bytes({"plan_sha256": digest, "no_retry": True}),
    )
    env = child_environment(spec, gpu_uuid=None)
    # Model hashing and local probes cannot hang the supervisor past its bound.
    _stage(
        (
            sys.executable,
            "-I",
            "-m",
            "inferdrome.deployment.vast_process_runtime",
            "preflight",
            "--directory",
            str(directory),
            "--execute-plan",
            digest,
        ),
        env,
        min(600.0, active_seconds(spec)),
    )
    observation = json.loads(read_private(directory / "runtime-observation.json"))
    python = _vllm_python()
    commands = [
        (engine_argv(spec, index, python), child_environment(spec, gpu_uuid=uuid))
        for index, uuid in enumerate(spec.gpu_uuids)
    ]

    def observer(children: Sequence[Child]) -> tuple[Sequence[str], Mapping[str, str]]:
        # Process IDs stay private; published configuration binds only a digest.
        observation["engine_process_ids"] = [child.pid for child in children]
        observation["engine_readiness"] = "HTTP_HEALTH_200_BOTH"
        observation["engine_command_sha256"] = [
            sha256_digest(canonical_json_bytes(list(command)))
            for command, _ in commands
        ]
        config = routing_config(spec, observation)
        write_private(
            directory / "runtime-observation-ready.json",
            canonical_json_bytes(observation),
        )
        write_private(
            directory / "execution-config.json",
            canonical_json_bytes(config.model_dump(mode="json")),
        )
        write_private(
            directory / "selected-workload.jsonl", fixed_selected_workload_bytes()
        )
        return (
            (
                sys.executable,
                "-I",
                "-m",
                "inferdrome.deployment.vast_process_observer",
                "--directory",
                str(directory),
                "--execute-plan",
                digest,
            ),
            env,
        )

    supervise(
        commands,
        observer,
        lambda seconds: _ready(env, seconds),
        readiness_seconds=spec.readiness_timeout_seconds,
        campaign_seconds=spec.campaign_timeout_seconds,
        total_seconds=active_seconds(spec),
    )
    package = Path(spec.evidence_path) / "routing-execution-package"
    verified = verify_execution_package(package)
    expected_config = read_private(directory / "execution-config.json")
    if verified.executed_manifest.config_sha256 != sha256_digest(expected_config):
        raise RuntimeFailure("VAST_PACKAGE_PLAN_MISMATCH")
    remaining_seconds(spec)
    return {
        "status": "VERIFIED_PACKAGE",
        "retained_digest": verified.report.retained_digest,
        "provider_cleanup": "CLEANUP_UNCONFIRMED",
    }


def _interrupt(signum: int, frame: Any) -> None:
    del signum, frame
    raise RuntimeFailure("VAST_OPERATOR_INTERRUPTED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("execute", "preflight", "probe"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--execute-plan")
    parser.add_argument("--operator")
    parser.add_argument("--accept-manual-cleanup-risk", action="store_true")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    try:
        if args.command == "probe":
            return 0 if _health_probe() else 1
        if args.directory is None or args.execute_plan is None:
            parser.error("--directory and --execute-plan are required")
        if args.command == "preflight":
            spec = load_plan(args.directory, args.execute_plan)
            write_private(
                args.directory / "runtime-observation.json",
                canonical_json_bytes(preflight(spec)),
            )
        else:
            print(
                json.dumps(
                    execute(
                        args.directory,
                        args.execute_plan,
                        args.operator or "",
                        args.accept_manual_cleanup_risk,
                    )
                )
            )
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, InferdromeError):
        print(
            json.dumps({"status": "FAILED", "provider_cleanup": "CLEANUP_UNCONFIRMED"})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
