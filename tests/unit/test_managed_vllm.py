"""Managed server supervision binds readiness to live GPU process evidence."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import inferdrome.execution.managed_vllm as managed_vllm
from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    preflight_attached_endpoint,
)
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.errors import AdapterError
from inferdrome.execution.cancellation import CancellationToken
from inferdrome.execution.subprocess_runner import (
    ProcessCapture,
    ProcessTermination,
)
from inferdrome.gpu_proof import ManagedVllmConfig, VllmDistributionIdentity
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "run-56565656565656565656565656565656"
GPU_UUID = "GPU-cccccccc-dddd-eeee-ffff-000000000000"
WHEEL_FILENAME = "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl"
WHEEL_SHA256 = (
    "adb1e4c9b46d0dfdb094121ae5aad670"
    "a42412dd813ed4e5db069ed6a15006de"
)


def test_managed_environment_is_allowlisted_with_fixed_cuda_loader_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(managed_vllm.platform, "machine", lambda: "x86_64")
    environment = managed_vllm.managed_process_environment(
        executable_path="/opt/inferdrome-gpu/bin/vllm",
        home_directory=Path("/private/inferdrome-home"),
        include_cuda_runtime=True,
        source={
            "PATH": "/ambient/bin",
            "VLLM_CONFIG_ROOT": "/untrusted/config",
            "VLLM_NO_USAGE_STATS": "0",
            "HF_HUB_OFFLINE": "0",
            "INFERDROME_FIXTURE": "must-not-cross",
            "LAMBDA_CLOUD_API_KEY": "synthetic-placeholder",
            "AWS_ACCESS_KEY_ID": "synthetic-placeholder",
            "GOOGLE_APPLICATION_CREDENTIALS": "/synthetic/placeholder.json",
            "CC": "/synthetic/compiler",
            "CXX": "/synthetic/compiler++",
            "CUDA_HOME": "/synthetic/cuda",
            "LD_LIBRARY_PATH": "/synthetic/loader",
            "TRITON_LIBCUDA_PATH": "/synthetic/triton-loader",
        },
    )

    assert environment["PATH"].split(os.pathsep)[0] == "/opt/inferdrome-gpu/bin"
    assert "/ambient/bin" not in environment["PATH"]
    assert environment["HOME"] == "/private/inferdrome-home"
    assert environment["TMPDIR"] == "/private/inferdrome-home"
    assert environment["CUDA_HOME"] == "/usr/local/cuda"
    assert environment["TRITON_LIBCUDA_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert "/synthetic/loader" not in environment["LD_LIBRARY_PATH"]
    assert environment["VLLM_NO_USAGE_STATS"] == "1"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"
    assert "VLLM_CONFIG_ROOT" not in environment
    assert "INFERDROME_FIXTURE" not in environment
    assert "LAMBDA_CLOUD_API_KEY" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert "CC" not in environment
    assert "CXX" not in environment


def test_managed_process_environment_ignores_ambient_path() -> None:
    environment = managed_vllm.managed_process_environment(
        executable_path="/opt/inferdrome-gpu/bin/vllm",
        source={
            "PATH": (
                "/usr/bin:/opt/inferdrome-gpu/bin:"
                "/bin:/opt/inferdrome-gpu/bin"
            ),
            "LD_LIBRARY_PATH": "/synthetic/loader",
        },
    )

    assert environment["PATH"].split(os.pathsep)[0] == "/opt/inferdrome-gpu/bin"
    assert environment["PATH"].count("/opt/inferdrome-gpu/bin") == 1
    assert "LD_LIBRARY_PATH" not in environment


def test_distribution_source_wheel_requires_exact_direct_url_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_directory = tmp_path / "vllm-0.26.0.dist-info"
    metadata_directory.mkdir()
    source_wheel = tmp_path / WHEEL_FILENAME
    source_wheel.write_bytes(b"fixture source wheel")
    direct_url = metadata_directory / "direct_url.json"
    direct_url.write_text(
        json.dumps(
            {
                "archive_info": {
                    "hash": f"sha256={WHEEL_SHA256}",
                    "hashes": {"sha256": WHEEL_SHA256},
                },
                "url": source_wheel.as_uri(),
            }
        )
    )
    item = managed_vllm._DiscoveredFile(
        relative_path=f"vllm-0.26.0.dist-info/{direct_url.name}",
        absolute_path=direct_url,
        identity=managed_vllm._file_identity(os.lstat(direct_url)),
    )
    monkeypatch.setattr(managed_vllm.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        managed_vllm,
        "_hash_regular_file",
        lambda *_args, **_kwargs: (f"sha256:{WHEEL_SHA256}", 20),
    )

    assert managed_vllm._distribution_source_wheel((item,)) == (
        WHEEL_FILENAME,
        f"sha256:{WHEEL_SHA256}",
        str(source_wheel),
    )

    direct_url.write_text(
        json.dumps(
            {
                "archive_info": {"hash": f"sha256={'0' * 64}"},
                "url": source_wheel.as_uri(),
            }
        )
    )
    changed = managed_vllm._DiscoveredFile(
        relative_path=item.relative_path,
        absolute_path=direct_url,
        identity=managed_vllm._file_identity(os.lstat(direct_url)),
    )
    with pytest.raises(AdapterError, match="hash differs"):
        managed_vllm._distribution_source_wheel((changed,))


def test_managed_server_rejects_gpu_visibility_remapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = resolve_experiment(
        REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml",
        run_id=RUN_ID,
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n")
    monkeypatch.setattr(managed_vllm.platform, "system", lambda: "Linux")
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")

    with pytest.raises(AdapterError, match="visibility remapping"):
        managed_vllm.ManagedVllmServer(
            resolution.resolved_spec,
            run_id=RUN_ID,
            config=ManagedVllmConfig(model_path=snapshot.absolute()),
            tokenizer_path=snapshot.absolute(),
            cwd=tmp_path.absolute(),
            cancellation=CancellationToken(),
            process_runner=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            preflight_probe=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("foreign_process", [False, True])
def test_managed_server_requires_exclusive_bound_gpu_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_process: bool,
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    resolution = resolve_experiment(
        REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml",
        run_id=RUN_ID,
    )
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n")
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi_bytes = b"#!/bin/sh\nexit 0\n"
    nvidia_smi.write_bytes(nvidia_smi_bytes)
    nvidia_smi.chmod(0o700)
    nvidia_smi_identity = managed_vllm.resolve_executable_identity(str(nvidia_smi))
    vllm = tmp_path / "vllm"
    vllm_bytes = b"#!/bin/sh\nexit 0\n"
    vllm.write_bytes(vllm_bytes)
    vllm.chmod(0o700)
    distribution = VllmDistributionIdentity(
        name="vllm",
        version="0.26.0",
        sha256=f"sha256:{'7' * 64}",
        file_count=100,
        total_bytes=1_000_000,
        hash_policy="installed-wheel-files-v1",
        executable_path=str(vllm),
        executable_sha256=f"sha256:{hashlib.sha256(vllm_bytes).hexdigest()}",
        source_wheel_filename=(
            "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl"
        ),
        source_wheel_path=(
            "/opt/inferdrome-gpu/downloads/"
            "vllm-0.26.0-cp38-abi3-manylinux_2_28_x86_64.whl"
        ),
        source_wheel_sha256=(
            "sha256:adb1e4c9b46d0dfdb094121ae5aad670"
            "a42412dd813ed4e5db069ed6a15006de"
        ),
    )
    monkeypatch.setattr(managed_vllm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(managed_vllm.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        managed_vllm,
        "collect_vllm_distribution_identity",
        lambda: distribution,
    )
    monkeypatch.setattr(
        managed_vllm,
        "_resolve_nvidia_smi",
        lambda: (
            nvidia_smi_identity,
            f"sha256:{hashlib.sha256(nvidia_smi_bytes).hexdigest()}",
        ),
    )
    monkeypatch.setattr(
        managed_vllm,
        "_collect_torch_cuda_runtime",
        lambda: ("2.9.0+cu130", "13.0", 1),
    )
    monkeypatch.setattr(
        managed_vllm.os,
        "getpgid",
        lambda pid: 7999 if pid == 7999 else 7001,
    )
    started_at = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)

    def process_runner(argv: tuple[str, ...], **kwargs: Any) -> ProcessCapture:
        if "--query-gpu=index,name,uuid,driver_version" in argv:
            assert kwargs["executable_identity"] is nvidia_smi_identity
            return ProcessCapture(
                argv=argv,
                started_at=started_at,
                ended_at=started_at,
                exit_status=0,
                termination=ProcessTermination.EXITED,
                stdout=f"0, NVIDIA L4, {GPU_UUID}, 580.65.06\n".encode(),
                stderr=b"",
            )
        if "--query-compute-apps=pid,gpu_uuid" in argv:
            assert argv[-1] == "--id=0"
            assert kwargs["executable_identity"] is nvidia_smi_identity
            output = f"7002, {GPU_UUID}\n"
            if foreign_process:
                output += f"7999, {GPU_UUID}\n"
            return ProcessCapture(
                argv=argv,
                started_at=started_at,
                ended_at=started_at,
                exit_status=0,
                termination=ProcessTermination.EXITED,
                stdout=output.encode(),
                stderr=b"",
            )
        observer = kwargs["on_start"]
        cancellation = kwargs["cancellation"]
        environment = kwargs["environment"]
        assert kwargs["executable_identity"].path == vllm
        assert environment["PATH"].split(os.pathsep)[0] == str(tmp_path)
        assert environment["VLLM_NO_USAGE_STATS"] == "1"
        assert environment["HF_HUB_OFFLINE"] == "1"
        observer(7001, started_at)
        cancellation.wait(5)
        return ProcessCapture(
            argv=argv,
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=30),
            exit_status=-15,
            termination=ProcessTermination.CANCELLED,
            stdout=b"server ready\n",
            stderr=b"",
        )

    preflight_response = json.dumps(
        {"data": [{"id": target.model}]}, separators=(",", ":")
    ).encode()

    def preflight_probe(*_args: object, **_kwargs: object) -> object:
        return preflight_attached_endpoint(
            target,
            transport=lambda _url, _timeout, _limit: HttpResponse(
                status=200,
                body=preflight_response,
            ),
        )

    server = managed_vllm.ManagedVllmServer.start(
        resolution.resolved_spec,
        run_id=RUN_ID,
        config=ManagedVllmConfig(
            model_path=snapshot.absolute(),
            startup_timeout_seconds=2,
        ),
        tokenizer_path=snapshot.absolute(),
        cwd=tmp_path.absolute(),
        cancellation=CancellationToken(),
        process_runner=process_runner,
        preflight_probe=preflight_probe,  # type: ignore[arg-type]
    )
    if foreign_process:
        with pytest.raises(AdapterError, match="shared with an unmanaged"):
            server.wait_until_ready()
        server.stop()
        return
    preflight, proof = server.wait_until_ready()

    assert preflight.result.target_model == target.model
    assert proof.server.pid == 7001
    assert proof.server.gpu_processes[0].pid == 7002
    assert proof.gpu_model == "NVIDIA L4"
    server.assert_running()
    server.assert_inputs_unchanged()
    capture = server.stop()
    assert capture.termination is ProcessTermination.CANCELLED
    assert capture.stdout == b"server ready\n"
