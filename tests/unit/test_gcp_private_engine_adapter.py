"""Local tests for the source-owned private vLLM adapter; no Docker or GCP."""

from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest

from inferdrome.deployment import gcp_private_engine_adapter as adapter
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignEngineAttestation,
    gcp_private_campaign_engine_adapter_source_sha256,
    issue_gcp_private_campaign_startup_payload,
)
from inferdrome.routing_execution.contracts import ImageIdentity


def _arguments(endpoint_id: str = "endpoint-a") -> adapter.EngineAdapterArguments:
    runner = ImageIdentity(reference="example/inferdrome-runner@sha256:" + ("1" * 64))
    serving = ImageIdentity(reference="example/inferdrome-serving@sha256:" + ("2" * 64))
    startup = issue_gcp_private_campaign_startup_payload(
        source_commit="1" * 40, runner_image=runner, serving_image=serving
    )
    engine = startup.engines[0 if endpoint_id == "endpoint-a" else 1]
    return adapter._arguments(
        (
            "--endpoint-id",
            engine.endpoint_id,
            "--container-name",
            engine.container_name,
            "--gpu-ordinal",
            str(engine.gpu_ordinal),
            "--private-port",
            str(engine.private_port),
            "--upstream-port",
            str(18000 + engine.gpu_ordinal),
            "--serving-image-reference",
            engine.serving_image.reference,
            "--model-snapshot-path",
            engine.model_snapshot_path,
            "--model-id",
            engine.model.model_id,
            "--model-revision",
            engine.model.model_revision,
            "--tokenizer-revision",
            engine.model.tokenizer_revision,
            "--startup-payload-digest",
            startup.startup_payload_id,
            "--adapter-source-sha256",
            gcp_private_campaign_engine_adapter_source_sha256(),
            "--model-manifest-sha256",
            startup.preloaded_artifacts.model_manifest_sha256,
            "--model-snapshot-sha256",
            startup.preloaded_artifacts.model_snapshot_sha256,
        )
    )


def test_adapter_renders_the_only_vllm_serve_argv() -> None:
    arguments = _arguments()

    assert adapter.vllm_child_argv(arguments, python="/usr/bin/python3.12") == (
        "/usr/bin/python3.12",
        "-I",
        "/usr/local/bin/vllm",
        "serve",
        "/opt/inferdrome/qwen3-8b",
        "--served-model-name",
        "Qwen/Qwen3-8B",
        "--tokenizer",
        "/opt/inferdrome/qwen3-8b",
        "--host",
        "127.0.0.1",
        "--port",
        "18000",
        "--disable-log-requests",
    )


def test_adapter_rejects_crossed_endpoint_gpu_port_slots() -> None:
    arguments = _arguments().__dict__.copy()
    arguments["gpu_ordinal"] = 1
    with pytest.raises(
        adapter.EngineAdapterError, match="ENGINE_ADAPTER_SLOT_MISMATCH"
    ):
        adapter._arguments(
            (
                "--endpoint-id",
                "endpoint-a",
                "--container-name",
                "inferdrome-engine-a",
                "--gpu-ordinal",
                "1",
                "--private-port",
                "8000",
                "--upstream-port",
                "18000",
                "--serving-image-reference",
                str(arguments["serving_image_reference"]),
                "--model-snapshot-path",
                "/opt/inferdrome/qwen3-8b",
                "--model-id",
                "Qwen/Qwen3-8B",
                "--model-revision",
                str(arguments["model_revision"]),
                "--tokenizer-revision",
                str(arguments["tokenizer_revision"]),
                "--startup-payload-digest",
                str(arguments["startup_payload_digest"]),
                "--adapter-source-sha256",
                str(arguments["adapter_source_sha256"]),
                "--model-manifest-sha256",
                str(arguments["model_manifest_sha256"]),
                "--model-snapshot-sha256",
                str(arguments["model_snapshot_sha256"]),
            )
        )


def test_private_adapter_builds_bound_attestation_and_has_an_allowlisted_surface() -> (
    None
):
    arguments = _arguments()
    value = json.loads(adapter._attestation(arguments, runtime_version="0.26.0"))
    observed = GcpPrivateCampaignEngineAttestation.model_validate(value)

    assert observed.endpoint_id == "endpoint-a"
    assert observed.gpu_ordinal == 0
    assert observed.adapter_source_sha256 == arguments.adapter_source_sha256
    assert adapter._ATTESTATION_PATH not in adapter._PROXIED_GET_PATHS
    assert {"/health", "/metrics", "/v1/models"} == adapter._PROXIED_GET_PATHS
    assert {"/v1/chat/completions"} == adapter._PROXIED_POST_PATHS


@pytest.fixture
def serving_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real local interpreter with synthetic metadata, never a vLLM install."""
    runtime = tmp_path / "serving"
    venv.EnvBuilder(with_pip=False).create(runtime)
    python = runtime / "bin/python"
    site = next(runtime.glob("lib/python*/site-packages"))
    metadata = site / "vllm-0.26.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: vllm\nVersion: 0.26.0\n")
    (site / "vllm.py").write_text("raise RuntimeError('must not import vllm')\n")
    cli = runtime / "bin/vllm"
    cli.write_text(
        f"#!{python}\n"
        "import importlib.metadata, json, os, sys\n"
        "print(json.dumps({'python': sys.executable, 'prefix': sys.prefix, "
        "'argv': sys.argv[1:], 'version': importlib.metadata.version('vllm'), "
        "'environment': {key: os.environ[key] for key in "
        "('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PATH', 'HF_HUB_OFFLINE', "
        "'TRANSFORMERS_OFFLINE', 'CUDA_VISIBLE_DEVICES') if key in os.environ}}))\n"
    )
    cli.chmod(0o755)
    monkeypatch.setattr(adapter, "_VLLM_CLI", cli, raising=False)
    return runtime


@pytest.mark.parametrize("version", ("0.26.1", "0.26.0+unapproved", "0.26.0.dev1"))
def test_adapter_observes_only_the_exact_serving_distribution_version(
    serving_runtime: Path, version: str
) -> None:
    assert adapter.observed_vllm_version() == "0.26.0"
    metadata = next(
        serving_runtime.glob("lib/python*/site-packages/*.dist-info/METADATA")
    )
    metadata.write_text(f"Name: vllm\nVersion: {version}\n")

    with pytest.raises(
        adapter.EngineAdapterError, match="ENGINE_ADAPTER_RUNTIME_VERSION_MISMATCH"
    ):
        adapter.observed_vllm_version()


def test_adapter_missing_serving_metadata_fails_closed(serving_runtime: Path) -> None:
    metadata = next(
        serving_runtime.glob("lib/python*/site-packages/*.dist-info/METADATA")
    )
    metadata.unlink()
    metadata.parent.rmdir()

    with pytest.raises(
        adapter.EngineAdapterError, match="ENGINE_ADAPTER_RUNTIME_VERSION_UNAVAILABLE"
    ):
        adapter.observed_vllm_version()


@pytest.mark.parametrize("shebang", ("#!/usr/bin/env python", "#!/missing/python", ""))
def test_adapter_rejects_unusable_cli_interpreter_binding(
    serving_runtime: Path, shebang: str
) -> None:
    (serving_runtime / "bin/vllm").write_text(shebang + "\n")
    with pytest.raises(adapter.EngineAdapterError, match="RUNTIME_UNAVAILABLE"):
        adapter.observed_vllm_version()


def test_metadata_and_child_use_the_same_isolated_serving_interpreter(
    serving_runtime: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app interpreter has no vLLM; even a competing PYTHONPATH distribution
    # and broken PYTHONHOME must not influence serving metadata or the child.
    poison = tmp_path / "poison"
    poison.mkdir()
    metadata = poison / "vllm-99.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: vllm\nVersion: 99.0.0\n")
    for name, value in {
        "PYTHONPATH": str(poison),
        "PYTHONHOME": str(poison),
        "VIRTUAL_ENV": "/opt/inferdrome-runtime",
        "PATH": "/opt/inferdrome-runtime/bin",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "CUDA_VISIBLE_DEVICES": "0",
    }.items():
        monkeypatch.setenv(name, value)

    python = adapter._vllm_python()
    environment = adapter._vllm_environment(python)
    assert adapter.observed_vllm_version() == "0.26.0"
    command = adapter.vllm_child_argv(_arguments(), python=python)
    observed = json.loads(subprocess.check_output(command, env=environment, text=True))
    assert observed["python"] == python
    assert observed["prefix"] == str(serving_runtime)
    assert observed["version"] == "0.26.0"
    assert observed["argv"] == list(command[3:])
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        assert name not in observed["environment"]
    assert "/opt/inferdrome-runtime/bin" not in observed["environment"]["PATH"]
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "CUDA_VISIBLE_DEVICES"):
        assert observed["environment"][name] == os.environ[name]
    # Manual-host's direct vllm executable follows this same console shebang.
    direct = json.loads(
        subprocess.check_output(command[2:], env=environment, text=True)
    )
    assert direct == observed


def test_serve_checks_metadata_before_starting_one_bound_child(
    serving_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments()
    monkeypatch.setattr(
        adapter, "adapter_source_sha256", lambda: arguments.adapter_source_sha256
    )
    monkeypatch.setattr(adapter, "verify_preloaded_snapshot", lambda _: None)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    launches = []
    popen = subprocess.Popen

    def launch(command, **kwargs):
        # Metadata observation uses subprocess.run/Popen too; count only the CLI.
        if str(adapter._VLLM_CLI) in command:
            launches.append((command, kwargs["env"]))
        return popen(command, **kwargs)

    class Server:
        def __init__(self, address, *, child, upstream_port, attestation):
            self.child = child
            assert json.loads(attestation)["runtime"]["runtime_version"] == "0.26.0"

        def serve_forever(self, **kwargs):
            assert self.child.wait(timeout=10) == 0

        def server_close(self):
            pass

    monkeypatch.setattr(adapter.subprocess, "Popen", launch)
    monkeypatch.setattr(adapter, "_PrivateServer", Server)
    adapter.serve(arguments)
    assert len(launches) == 1
    assert launches[0][0][0] == str(serving_runtime / "bin/python")
    assert "VIRTUAL_ENV" not in launches[0][1]

    metadata = next(
        serving_runtime.glob("lib/python*/site-packages/*.dist-info/METADATA")
    )
    metadata.write_text("Name: vllm\nVersion: 0.26.0+unapproved\n")
    with pytest.raises(adapter.EngineAdapterError, match="RUNTIME_VERSION_MISMATCH"):
        adapter.serve(arguments)
    assert len(launches) == 1


def test_adapter_attestation_uses_the_observed_runtime_value() -> None:
    arguments = _arguments()
    value = json.loads(adapter._attestation(arguments, runtime_version="0.26.0"))

    assert value["runtime"]["runtime_version"] == "0.26.0"
