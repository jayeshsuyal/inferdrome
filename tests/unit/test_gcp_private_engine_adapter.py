"""Local tests for the source-owned private vLLM adapter; no Docker or GCP."""

from __future__ import annotations

import json

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

    assert adapter.vllm_child_argv(arguments) == (
        "vllm",
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
    value = json.loads(adapter._attestation(arguments))
    observed = GcpPrivateCampaignEngineAttestation.model_validate(value)

    assert observed.endpoint_id == "endpoint-a"
    assert observed.gpu_ordinal == 0
    assert observed.adapter_source_sha256 == arguments.adapter_source_sha256
    assert adapter._ATTESTATION_PATH not in adapter._PROXIED_GET_PATHS
    assert {"/health", "/metrics", "/v1/models"} == adapter._PROXIED_GET_PATHS
    assert {"/v1/chat/completions"} == adapter._PROXIED_POST_PATHS
