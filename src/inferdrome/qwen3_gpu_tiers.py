"""Fail-closed execution packs for implemented Qwen3 GPU campaign tiers."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from pydantic import model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import Sha256Digest
from inferdrome.gpu_campaign import (
    CANONICAL_CAMPAIGN_ID,
    canonical_qwen_gpu_campaign,
)
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    QWEN3_WORKLOAD_ID,
    qwen3_profile_sha256,
    qwen3_workload_sha256,
)

QWEN3_A10_GPU_TIER_ID = "a10-24gb-pcie"
QWEN3_A100_GPU_TIER_ID = "a100-40gb-pcie"
QWEN3_H100_GPU_TIER_ID = "h100-80gb-pcie"
QWEN3_IMPLEMENTED_GPU_TIERS = (
    QWEN3_A10_GPU_TIER_ID,
    QWEN3_A100_GPU_TIER_ID,
    QWEN3_H100_GPU_TIER_ID,
)
QWEN3_A100_EXECUTION_PACK_SCHEMA_ID = (
    "urn:inferdrome:qwen3-a100-execution-pack:v1"
)
QWEN3_A100_EXECUTION_PACK_SCHEMA_VERSION = (
    "inferdrome.qwen3-a100-execution-pack.v1"
)
QWEN3_H100_EXECUTION_PACK_SCHEMA_ID = (
    "urn:inferdrome:qwen3-h100-execution-pack:v1"
)
QWEN3_H100_EXECUTION_PACK_SCHEMA_VERSION = (
    "inferdrome.qwen3-h100-execution-pack.v1"
)

QWEN3_STARTUP_TIMEOUT_SECONDS = 300
QWEN3_REMOTE_CAPTURE_SECONDS = 1_300
QWEN3_POST_REMOTE_BUDGET_SECONDS = 298
QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS = 300
QWEN3_METADATA_TRANSFER_SECONDS = 30
QWEN3_ARCHIVE_TRANSFER_SECONDS = 180
QWEN3_MAX_ARCHIVE_BYTES = 268_435_456
QWEN3_PHASE_BUDGET_SECONDS = MappingProxyType(
    {
        "remote_preflight": 90,
        "source_upload": 90,
        "remote_capture": QWEN3_REMOTE_CAPTURE_SECONDS,
        "remote_kill_grace": 60,
        "ssh_close_grace": 5,
        "metadata_transfer": QWEN3_METADATA_TRANSFER_SECONDS,
        "archive_transfer": QWEN3_ARCHIVE_TRANSFER_SECONDS,
        "controller_handoff": 23,
        "termination_confirmation": QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS,
    }
)


@dataclass(frozen=True)
class Qwen3GpuTierPolicy:
    """Controller-only policy derived from the frozen campaign plan."""

    gpu_tier_id: str
    campaign_gpu_model: str
    expected_nvidia_smi_name: str
    vram_gib: int
    interconnect: str
    hourly_rate_usd: Decimal
    max_session_cost_usd: Decimal

    @property
    def allowed_seconds(self) -> int:
        return int(
            self.max_session_cost_usd
            / self.hourly_rate_usd
            * Decimal(3_600)
        )

    @property
    def hardware_observation(self) -> str:
        model = self.expected_nvidia_smi_name.upper().replace(" ", "_")
        return f"SELECTED_GPU_REPORTED_{model}_SINGLE_CUDA_DEVICE"

    def public_target(self) -> dict[str, Any]:
        return {
            "campaign_gpu_model": self.campaign_gpu_model,
            "expected_nvidia_smi_name": self.expected_nvidia_smi_name,
            "gpu_count": 1,
            "gpu_tier_id": self.gpu_tier_id,
            "interconnect": self.interconnect,
            "provider": "lambda_cloud",
            "provider_instance_type_policy": (
                "api_resolved_exact_gpu_tier_v1"
            ),
            "vram_gib": self.vram_gib,
        }


_IMPLEMENTED_POLICIES = MappingProxyType(
    {
        QWEN3_A10_GPU_TIER_ID: Qwen3GpuTierPolicy(
            gpu_tier_id=QWEN3_A10_GPU_TIER_ID,
            campaign_gpu_model="NVIDIA A10",
            expected_nvidia_smi_name="NVIDIA A10",
            hourly_rate_usd=Decimal("1.29"),
            interconnect="PCIE",
            max_session_cost_usd=Decimal("0.75"),
            vram_gib=24,
        ),
        QWEN3_A100_GPU_TIER_ID: Qwen3GpuTierPolicy(
            gpu_tier_id=QWEN3_A100_GPU_TIER_ID,
            campaign_gpu_model="NVIDIA A100",
            expected_nvidia_smi_name="NVIDIA A100-PCIE-40GB",
            hourly_rate_usd=Decimal("1.99"),
            interconnect="PCIE",
            max_session_cost_usd=Decimal("1.25"),
            vram_gib=40,
        ),
        QWEN3_H100_GPU_TIER_ID: Qwen3GpuTierPolicy(
            gpu_tier_id=QWEN3_H100_GPU_TIER_ID,
            campaign_gpu_model="NVIDIA H100",
            expected_nvidia_smi_name="NVIDIA H100 PCIe",
            hourly_rate_usd=Decimal("3.29"),
            interconnect="PCIE",
            max_session_cost_usd=Decimal("2.25"),
            vram_gib=80,
        ),
    }
)


def qwen3_gpu_tier_policy(gpu_tier_id: str) -> Qwen3GpuTierPolicy:
    """Resolve one implemented tier and cross-check it against the frozen plan."""

    selected = _IMPLEMENTED_POLICIES.get(gpu_tier_id)
    if selected is None:
        raise ValueError(f"Qwen3 GPU tier is not implemented: {gpu_tier_id}")
    plan = canonical_qwen_gpu_campaign()
    target = next(
        item for item in plan.gpu_targets if item.gpu_tier_id == gpu_tier_id
    )
    budget = next(
        item for item in plan.budget.sessions if item.gpu_tier_id == gpu_tier_id
    )
    expected_plan_fields = {
        "gpu_count": 1,
        "gpu_model": selected.campaign_gpu_model,
        "gpu_tier_id": gpu_tier_id,
        "interconnect": selected.interconnect,
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": selected.vram_gib,
    }
    if target.model_dump(mode="json") != expected_plan_fields:
        raise AssertionError("implemented Qwen3 GPU target drifted from campaign")
    if (
        budget.hourly_rate_snapshot_usd != str(selected.hourly_rate_usd)
        or budget.max_session_cost_usd != str(selected.max_session_cost_usd)
    ):
        raise AssertionError("implemented Qwen3 GPU budget drifted from campaign")
    if sum(QWEN3_PHASE_BUDGET_SECONDS.values()) > selected.allowed_seconds:
        raise AssertionError("implemented Qwen3 phase budget exceeds session cap")
    return selected


class A100GpuTarget(FrozenModel):
    campaign_gpu_model: Literal["NVIDIA A100"]
    expected_nvidia_smi_name: Literal["NVIDIA A100-PCIE-40GB"]
    gpu_count: Literal[1]
    gpu_tier_id: Literal["a100-40gb-pcie"]
    interconnect: Literal["PCIE"]
    provider: Literal["lambda_cloud"]
    provider_instance_type_policy: Literal["api_resolved_exact_gpu_tier_v1"]
    vram_gib: Literal[40]


class A100CostBoundary(FrozenModel):
    allowed_seconds: Literal[2261]
    api_rate_match_required: Literal[True]
    hourly_rate_snapshot_usd: Literal["1.99"]
    max_session_cost_usd: Literal["1.25"]
    rate_snapshot_date: Literal["2026-08-20"]
    termination_safety_margin_seconds: Literal[300]


class A100ModelBinding(FrozenModel):
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]
    tokenizer_revision: Literal[
        "b968826d9c46dd6066d109eabc6255188de91218"
    ]


class A100ProfileBinding(FrozenModel):
    profile_id: Literal["managed-vllm-0.26-qwen3-8b-bf16-v1"]
    profile_sha256: Sha256Digest
    producer_name: Literal["vllm"]
    producer_version: Literal["0.26.0"]

    @model_validator(mode="after")
    def exact_profile_must_be_bound(self) -> A100ProfileBinding:
        if self.profile_sha256 != qwen3_profile_sha256():
            raise ValueError("Qwen3 profile digest drifted")
        return self


class A100ProviderBinding(FrozenModel):
    exact_instance_type_name: Literal["RUNTIME_ARGUMENT_REQUIRED"]
    one_active_instance_required: Literal[True]
    remote_gpu_observation_required: Literal[True]


class A100WorkloadBinding(FrozenModel):
    concurrency: Literal[1]
    measured_requests: Literal[96]
    warmup_requests: Literal[12]
    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    workload_sha256: Sha256Digest

    @model_validator(mode="after")
    def exact_workload_must_be_bound(self) -> A100WorkloadBinding:
        if self.workload_sha256 != qwen3_workload_sha256():
            raise ValueError("Qwen3 workload digest drifted")
        return self


class A100PhaseBudget(FrozenModel):
    archive_transfer: Literal[180]
    controller_handoff: Literal[23]
    metadata_transfer: Literal[30]
    remote_capture: Literal[1300]
    remote_kill_grace: Literal[60]
    remote_preflight: Literal[90]
    source_upload: Literal[90]
    ssh_close_grace: Literal[5]
    termination_confirmation: Literal[300]


class A100ExecutionPack(FrozenModel):
    acceptance_verdict: None
    campaign_id: Literal["qwen-gpu-capability-campaign-v1"]
    capability_state: Literal["LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"]
    cost_boundary: A100CostBoundary
    gpu_target: A100GpuTarget
    hardware_attestation: Literal[False]
    launch_authorization: Literal["EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED"]
    model_binding: A100ModelBinding
    phase_budget_seconds: A100PhaseBudget
    profile_binding: A100ProfileBinding
    provider_binding: A100ProviderBinding
    schema_version: Literal["inferdrome.qwen3-a100-execution-pack.v1"]
    track_id: Literal["qwen3-8b-hardware-control"]
    workload_binding: A100WorkloadBinding


class H100GpuTarget(FrozenModel):
    campaign_gpu_model: Literal["NVIDIA H100"]
    expected_nvidia_smi_name: Literal["NVIDIA H100 PCIe"]
    gpu_count: Literal[1]
    gpu_tier_id: Literal["h100-80gb-pcie"]
    interconnect: Literal["PCIE"]
    provider: Literal["lambda_cloud"]
    provider_instance_type_policy: Literal["api_resolved_exact_gpu_tier_v1"]
    vram_gib: Literal[80]


class H100CostBoundary(FrozenModel):
    allowed_seconds: Literal[2462]
    api_rate_match_required: Literal[True]
    hourly_rate_snapshot_usd: Literal["3.29"]
    max_session_cost_usd: Literal["2.25"]
    rate_snapshot_date: Literal["2026-08-20"]
    termination_safety_margin_seconds: Literal[300]


class H100ModelBinding(FrozenModel):
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]
    tokenizer_revision: Literal[
        "b968826d9c46dd6066d109eabc6255188de91218"
    ]


class H100ProfileBinding(FrozenModel):
    profile_id: Literal["managed-vllm-0.26-qwen3-8b-bf16-v1"]
    profile_sha256: Sha256Digest
    producer_name: Literal["vllm"]
    producer_version: Literal["0.26.0"]

    @model_validator(mode="after")
    def exact_profile_must_be_bound(self) -> H100ProfileBinding:
        if self.profile_sha256 != qwen3_profile_sha256():
            raise ValueError("Qwen3 profile digest drifted")
        return self


class H100ProviderBinding(FrozenModel):
    exact_instance_type_name: Literal["RUNTIME_ARGUMENT_REQUIRED"]
    one_active_instance_required: Literal[True]
    remote_gpu_observation_required: Literal[True]


class H100WorkloadBinding(FrozenModel):
    concurrency: Literal[1]
    measured_requests: Literal[96]
    warmup_requests: Literal[12]
    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    workload_sha256: Sha256Digest

    @model_validator(mode="after")
    def exact_workload_must_be_bound(self) -> H100WorkloadBinding:
        if self.workload_sha256 != qwen3_workload_sha256():
            raise ValueError("Qwen3 workload digest drifted")
        return self


class H100PhaseBudget(FrozenModel):
    archive_transfer: Literal[180]
    controller_handoff: Literal[23]
    metadata_transfer: Literal[30]
    remote_capture: Literal[1300]
    remote_kill_grace: Literal[60]
    remote_preflight: Literal[90]
    source_upload: Literal[90]
    ssh_close_grace: Literal[5]
    termination_confirmation: Literal[300]


class H100ExecutionPack(FrozenModel):
    acceptance_verdict: None
    campaign_id: Literal["qwen-gpu-capability-campaign-v1"]
    capability_state: Literal["LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"]
    cost_boundary: H100CostBoundary
    gpu_target: H100GpuTarget
    hardware_attestation: Literal[False]
    launch_authorization: Literal["EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED"]
    model_binding: H100ModelBinding
    phase_budget_seconds: H100PhaseBudget
    profile_binding: H100ProfileBinding
    provider_binding: H100ProviderBinding
    schema_version: Literal["inferdrome.qwen3-h100-execution-pack.v1"]
    track_id: Literal["qwen3-8b-hardware-control"]
    workload_binding: H100WorkloadBinding


def qwen3_a100_execution_pack() -> A100ExecutionPack:
    """Return the code-complete, paid-execution-unproven A100 pack."""

    policy = qwen3_gpu_tier_policy(QWEN3_A100_GPU_TIER_ID)
    return A100ExecutionPack.model_validate(
        {
            "acceptance_verdict": None,
            "campaign_id": CANONICAL_CAMPAIGN_ID,
            "capability_state": "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN",
            "cost_boundary": {
                "allowed_seconds": policy.allowed_seconds,
                "api_rate_match_required": True,
                "hourly_rate_snapshot_usd": str(policy.hourly_rate_usd),
                "max_session_cost_usd": str(policy.max_session_cost_usd),
                "rate_snapshot_date": "2026-08-20",
                "termination_safety_margin_seconds": (
                    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
                ),
            },
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
                "exact_instance_type_name": "RUNTIME_ARGUMENT_REQUIRED",
                "one_active_instance_required": True,
                "remote_gpu_observation_required": True,
            },
            "schema_version": QWEN3_A100_EXECUTION_PACK_SCHEMA_VERSION,
            "track_id": "qwen3-8b-hardware-control",
            "workload_binding": {
                "concurrency": 1,
                "measured_requests": 96,
                "warmup_requests": 12,
                "workload_id": QWEN3_WORKLOAD_ID,
                "workload_sha256": qwen3_workload_sha256(),
            },
        }
    )


def qwen3_h100_execution_pack() -> H100ExecutionPack:
    """Return the code-complete, paid-execution-unproven H100 pack."""

    policy = qwen3_gpu_tier_policy(QWEN3_H100_GPU_TIER_ID)
    return H100ExecutionPack.model_validate(
        {
            "acceptance_verdict": None,
            "campaign_id": CANONICAL_CAMPAIGN_ID,
            "capability_state": "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN",
            "cost_boundary": {
                "allowed_seconds": policy.allowed_seconds,
                "api_rate_match_required": True,
                "hourly_rate_snapshot_usd": str(policy.hourly_rate_usd),
                "max_session_cost_usd": str(policy.max_session_cost_usd),
                "rate_snapshot_date": "2026-08-20",
                "termination_safety_margin_seconds": (
                    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
                ),
            },
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
                "exact_instance_type_name": "RUNTIME_ARGUMENT_REQUIRED",
                "one_active_instance_required": True,
                "remote_gpu_observation_required": True,
            },
            "schema_version": QWEN3_H100_EXECUTION_PACK_SCHEMA_VERSION,
            "track_id": "qwen3-8b-hardware-control",
            "workload_binding": {
                "concurrency": 1,
                "measured_requests": 96,
                "warmup_requests": 12,
                "workload_id": QWEN3_WORKLOAD_ID,
                "workload_sha256": qwen3_workload_sha256(),
            },
        }
    )


def qwen3_a100_execution_pack_schema() -> dict[str, Any]:
    schema = copy.deepcopy(
        A100ExecutionPack.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = QWEN3_A100_EXECUTION_PACK_SCHEMA_ID
    schema["title"] = "Inferdrome Qwen3-8B A100 execution pack v1"
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


def qwen3_h100_execution_pack_schema() -> dict[str, Any]:
    schema = copy.deepcopy(
        H100ExecutionPack.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = QWEN3_H100_EXECUTION_PACK_SCHEMA_ID
    schema["title"] = "Inferdrome Qwen3-8B H100 execution pack v1"
    schema["$defs"]["H100ProfileBinding"]["properties"]["profile_sha256"] = {
        "const": qwen3_profile_sha256(),
        "title": "Profile Sha256",
        "type": "string",
    }
    schema["$defs"]["H100WorkloadBinding"]["properties"]["workload_sha256"] = {
        "const": qwen3_workload_sha256(),
        "title": "Workload Sha256",
        "type": "string",
    }
    return schema


def qwen3_a100_execution_pack_conformance_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "valid-qwen3-a100-execution-pack",
            "pack_valid": True,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "add",
                "path": "/paid_launch_authorization",
                "value": "GRANTED",
            },
            "name": "rejects-unknown-launch-authority",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/capability_state",
                "value": "RUNTIME_PROVEN",
            },
            "name": "rejects-unearned-runtime-claim",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/gpu_target/expected_nvidia_smi_name",
                "value": "NVIDIA A100-SXM4-80GB",
            },
            "name": "rejects-wrong-a100-variant",
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
        {
            "mutation": {
                "operation": "replace",
                "path": "/phase_budget_seconds/remote_capture",
                "value": 1_299,
            },
            "name": "rejects-phase-budget-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
    ]


def qwen3_h100_execution_pack_conformance_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "valid-qwen3-h100-execution-pack",
            "pack_valid": True,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "add",
                "path": "/paid_launch_authorization",
                "value": "GRANTED",
            },
            "name": "rejects-unknown-launch-authority",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/capability_state",
                "value": "RUNTIME_PROVEN",
            },
            "name": "rejects-unearned-runtime-claim",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/gpu_target/expected_nvidia_smi_name",
                "value": "NVIDIA H100 80GB HBM3",
            },
            "name": "rejects-wrong-h100-variant",
            "pack_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/cost_boundary/max_session_cost_usd",
                "value": "2.26",
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
        {
            "mutation": {
                "operation": "replace",
                "path": "/phase_budget_seconds/remote_capture",
                "value": 1_299,
            },
            "name": "rejects-phase-budget-drift",
            "pack_valid": False,
            "schema_valid": False,
        },
    ]


def qwen3_gpu_execution_documents() -> dict[str, bytes]:
    """Render deterministic implemented-tier execution-pack artifacts."""

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
        "campaigns/v1/execution-packs/qwen3-8b-a100.json": pretty_json(
            qwen3_a100_execution_pack().model_dump(mode="json")
        ),
        "campaigns/v1/execution-packs/qwen3-a100-execution-pack.schema.json": (
            pretty_json(qwen3_a100_execution_pack_schema())
        ),
        "tests/fixtures/campaigns/v1/qwen3-a100-execution-pack-cases.json": (
            pretty_json(qwen3_a100_execution_pack_conformance_cases())
        ),
        "campaigns/v1/execution-packs/qwen3-8b-h100.json": pretty_json(
            qwen3_h100_execution_pack().model_dump(mode="json")
        ),
        "campaigns/v1/execution-packs/qwen3-h100-execution-pack.schema.json": (
            pretty_json(qwen3_h100_execution_pack_schema())
        ),
        "tests/fixtures/campaigns/v1/qwen3-h100-execution-pack-cases.json": (
            pretty_json(qwen3_h100_execution_pack_conformance_cases())
        ),
    }
