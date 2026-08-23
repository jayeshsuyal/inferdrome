"""Dated, fail-closed GPU tiers that do not mutate the frozen campaign."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from inferdrome.domain.base import FrozenModel
from inferdrome.gpu_campaign import CANONICAL_CAMPAIGN_ID
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    QWEN3_WORKLOAD_ID,
    qwen3_profile_sha256,
    qwen3_workload_sha256,
)
from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A100_SXM4_GPU_TIER_ID,
    QWEN3_PHASE_BUDGET_SECONDS,
    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS,
    A100ModelBinding,
    A100PhaseBudget,
    A100ProfileBinding,
    A100WorkloadBinding,
    qwen3_gpu_tier_policy,
)

QWEN3_A100_SXM4_EXTENSION_ID = (
    "qwen3-8b-a100-sxm4-capacity-extension-2026-08-23"
)
QWEN3_A100_SXM4_EXTENSION_SCHEMA_ID = (
    "urn:inferdrome:qwen3-a100-sxm4-capacity-extension:v1"
)
QWEN3_A100_SXM4_EXTENSION_SCHEMA_VERSION = (
    "inferdrome.qwen3-a100-sxm4-capacity-extension.v1"
)


class A100Sxm4GpuTarget(FrozenModel):
    campaign_gpu_model: Literal["NVIDIA A100"]
    expected_nvidia_smi_name: Literal["NVIDIA A100-SXM4-40GB"]
    gpu_count: Literal[1]
    gpu_tier_id: Literal["a100-40gb-sxm4"]
    interconnect: Literal["SXM4"]
    provider: Literal["lambda_cloud"]
    provider_instance_type_policy: Literal["api_resolved_exact_gpu_tier_v1"]
    vram_gib: Literal[40]


class A100Sxm4CostBoundary(FrozenModel):
    allowed_seconds: Literal[2261]
    api_rate_match_required: Literal[True]
    hourly_rate_snapshot_usd: Literal["1.99"]
    max_session_cost_usd: Literal["1.25"]
    rate_snapshot_date: Literal["2026-08-23"]
    termination_safety_margin_seconds: Literal[300]


class A100Sxm4ProviderBinding(FrozenModel):
    architecture: Literal["x86_64"]
    exact_instance_type_name: Literal["gpu_1x_a100_sxm4"]
    memory_gib: Literal[200]
    one_active_instance_required: Literal[True]
    provider_description: Literal["1x A100 (40 GB SXM4)"]
    provider_gpu_description: Literal["A100 (40 GB SXM4)"]
    region_policy: Literal["API_RESOLVED_CAPACITY_REGION"]
    remote_gpu_observation_required: Literal[True]
    storage_gib: Literal[512]
    vcpus: Literal[30]


class A100Sxm4CapacityExtensionPack(FrozenModel):
    acceptance_verdict: None
    base_campaign_id: Literal["qwen-gpu-capability-campaign-v1"]
    campaign_extension_id: Literal[
        "qwen3-8b-a100-sxm4-capacity-extension-2026-08-23"
    ]
    campaign_relationship: Literal[
        "DISTINCT_HARDWARE_TIER_DOES_NOT_REPLACE_A100_PCIE"
    ]
    capability_state: Literal["LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"]
    cost_boundary: A100Sxm4CostBoundary
    extension_frozen_on: Literal["2026-08-23"]
    gpu_target: A100Sxm4GpuTarget
    hardware_attestation: Literal[False]
    launch_authorization: Literal["EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED"]
    model_binding: A100ModelBinding
    phase_budget_seconds: A100PhaseBudget
    profile_binding: A100ProfileBinding
    provider_binding: A100Sxm4ProviderBinding
    schema_version: Literal[
        "inferdrome.qwen3-a100-sxm4-capacity-extension.v1"
    ]
    track_id: Literal["qwen3-8b-hardware-control-capacity-extension"]
    workload_binding: A100WorkloadBinding


def qwen3_a100_sxm4_capacity_extension_pack() -> A100Sxm4CapacityExtensionPack:
    """Return the extension frozen before any A100 SXM4 measurement."""

    policy = qwen3_gpu_tier_policy(QWEN3_A100_SXM4_GPU_TIER_ID)
    return A100Sxm4CapacityExtensionPack.model_validate(
        {
            "acceptance_verdict": None,
            "base_campaign_id": CANONICAL_CAMPAIGN_ID,
            "campaign_extension_id": QWEN3_A100_SXM4_EXTENSION_ID,
            "campaign_relationship": (
                "DISTINCT_HARDWARE_TIER_DOES_NOT_REPLACE_A100_PCIE"
            ),
            "capability_state": "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN",
            "cost_boundary": {
                "allowed_seconds": policy.allowed_seconds,
                "api_rate_match_required": True,
                "hourly_rate_snapshot_usd": str(policy.hourly_rate_usd),
                "max_session_cost_usd": str(policy.max_session_cost_usd),
                "rate_snapshot_date": "2026-08-23",
                "termination_safety_margin_seconds": (
                    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
                ),
            },
            "extension_frozen_on": "2026-08-23",
            "gpu_target": policy.public_target(),
            "hardware_attestation": False,
            "launch_authorization": "EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED",
            "model_binding": {
                "model_id": QWEN3_8B_MODEL_ID,
                "model_revision": QWEN3_8B_REVISION,
                "tokenizer_revision": QWEN3_8B_REVISION,
            },
            "phase_budget_seconds": dict(QWEN3_PHASE_BUDGET_SECONDS),
            "profile_binding": {
                "profile_id": QWEN3_8B_PROFILE_ID,
                "profile_sha256": qwen3_profile_sha256(),
                "producer_name": "vllm",
                "producer_version": "0.26.0",
            },
            "provider_binding": {
                "architecture": "x86_64",
                "exact_instance_type_name": "gpu_1x_a100_sxm4",
                "memory_gib": 200,
                "one_active_instance_required": True,
                "provider_description": "1x A100 (40 GB SXM4)",
                "provider_gpu_description": "A100 (40 GB SXM4)",
                "region_policy": "API_RESOLVED_CAPACITY_REGION",
                "remote_gpu_observation_required": True,
                "storage_gib": 512,
                "vcpus": 30,
            },
            "schema_version": QWEN3_A100_SXM4_EXTENSION_SCHEMA_VERSION,
            "track_id": "qwen3-8b-hardware-control-capacity-extension",
            "workload_binding": {
                "concurrency": 1,
                "measured_requests": 96,
                "warmup_requests": 12,
                "workload_id": QWEN3_WORKLOAD_ID,
                "workload_sha256": qwen3_workload_sha256(),
            },
        }
    )


def qwen3_a100_sxm4_capacity_extension_schema() -> dict[str, Any]:
    schema = copy.deepcopy(
        A100Sxm4CapacityExtensionPack.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = QWEN3_A100_SXM4_EXTENSION_SCHEMA_ID
    schema["title"] = "Inferdrome Qwen3-8B A100 SXM4 capacity extension v1"
    schema["$defs"]["A100ProfileBinding"]["properties"]["profile_sha256"] = {
        "const": qwen3_profile_sha256(),
        "title": "Profile Sha256",
        "type": "string",
    }
    schema["$defs"]["A100WorkloadBinding"]["properties"]["workload_sha256"] = {
        "const": qwen3_workload_sha256(),
        "title": "Workload Sha256",
        "type": "string",
    }
    return schema


def qwen3_a100_sxm4_capacity_extension_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "valid-qwen3-a100-sxm4-capacity-extension",
            "pack_valid": True,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/campaign_relationship",
                "value": "REPLACES_A100_PCIE",
            },
            "name": "rejects-pcie-substitution-claim",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/gpu_target/expected_nvidia_smi_name",
                "value": "NVIDIA A100-PCIE-40GB",
            },
            "name": "rejects-pcie-runtime-identity",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/provider_binding/exact_instance_type_name",
                "value": "gpu_1x_a100",
            },
            "name": "rejects-provider-instance-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/cost_boundary/max_session_cost_usd",
                "value": "1.26",
            },
            "name": "rejects-cost-cap-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/profile_binding/profile_sha256",
                "value": "sha256:" + "0" * 64,
            },
            "name": "rejects-profile-digest-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/workload_binding/workload_sha256",
                "value": "sha256:" + "0" * 64,
            },
            "name": "rejects-workload-digest-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
    ]


def qwen3_capacity_extension_documents() -> dict[str, bytes]:
    """Render deterministic capacity-extension artifacts."""

    def pretty_json(value: Any) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()

    return {
        (
            "campaigns/v1/execution-packs/"
            "qwen3-8b-a100-sxm4-capacity-extension.json"
        ): pretty_json(
            qwen3_a100_sxm4_capacity_extension_pack().model_dump(mode="json")
        ),
        (
            "campaigns/v1/execution-packs/"
            "qwen3-a100-sxm4-capacity-extension.schema.json"
        ): pretty_json(qwen3_a100_sxm4_capacity_extension_schema()),
        (
            "tests/fixtures/campaigns/v1/"
            "qwen3-a100-sxm4-capacity-extension-cases.json"
        ): pretty_json(qwen3_a100_sxm4_capacity_extension_cases()),
    }
