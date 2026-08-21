"""Strict operational design for the planned cross-GPU evidence campaign."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import OpaqueName, RunId, Sha256Digest

CAMPAIGN_SCHEMA_ID = "urn:inferdrome:gpu-campaign-plan:v1"
CAMPAIGN_SCHEMA_VERSION = "inferdrome.gpu-campaign-plan.v1"
CANONICAL_CAMPAIGN_ID = "qwen-gpu-capability-campaign-v1"

CampaignId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]
CampaignKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]
GitCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
UsdDecimal = Annotated[
    str,
    StringConstraints(
        max_length=16,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$",
    ),
]


class GpuInterconnect(StrEnum):
    PCIE = "PCIE"
    SXM6 = "SXM6"


class CheckpointPrecision(StrEnum):
    BF16 = "BF16"
    OFFICIAL_FP8_E4M3 = "OFFICIAL_FP8_E4M3"


class QuantizationPolicy(StrEnum):
    NONE = "NONE"
    CHECKPOINT_DEFINED_FP8 = "CHECKPOINT_DEFINED_FP8"


class ServerModelMode(StrEnum):
    TEXT_GENERATION = "TEXT_GENERATION"
    LANGUAGE_MODEL_ONLY = "LANGUAGE_MODEL_ONLY"


class ModelPin(FrozenModel):
    model_key: CampaignKey
    model_id: OpaqueName
    model_revision: GitCommit
    tokenizer_revision: GitCommit
    license: Literal["apache-2.0"]
    total_parameters: Annotated[int, Field(strict=True, ge=1)]
    checkpoint_precision: CheckpointPrecision
    activation_dtype: Literal["bfloat16"]
    kv_cache_dtype: Literal["bfloat16"]
    quantization_policy: QuantizationPolicy
    weight_format: Literal["safetensors"]
    benchmark_modality: Literal["TEXT_ONLY"]
    server_model_mode: ServerModelMode
    thinking_mode: Literal["DISABLED"]

    @model_validator(mode="after")
    def precision_configuration_must_be_coherent(self) -> ModelPin:
        if self.checkpoint_precision is CheckpointPrecision.BF16:
            if self.quantization_policy is not QuantizationPolicy.NONE:
                raise ValueError("BF16 checkpoints cannot declare quantization")
            if self.server_model_mode is not ServerModelMode.TEXT_GENERATION:
                raise ValueError("dense Qwen3 checkpoints use text-generation mode")
        else:
            if (
                self.quantization_policy
                is not QuantizationPolicy.CHECKPOINT_DEFINED_FP8
            ):
                raise ValueError("official FP8 checkpoints require pinned quantization")
            if self.server_model_mode is not ServerModelMode.LANGUAGE_MODEL_ONLY:
                raise ValueError("the Qwen3.5 frontier profile must disable vision")
        return self


class GpuTarget(FrozenModel):
    gpu_tier_id: CampaignKey
    provider: Literal["lambda_cloud"]
    gpu_model: Annotated[str, Field(min_length=1, max_length=128)]
    vram_gib: Annotated[int, Field(strict=True, ge=1, le=1_024)]
    interconnect: GpuInterconnect
    gpu_count: Literal[1]
    provider_instance_type_policy: Literal["api_resolved_exact_gpu_tier_v1"]


class PlannedProfileAssignment(FrozenModel):
    gpu_tier_id: CampaignKey
    model_key: CampaignKey
    planned_profile_id: CampaignKey
    producer_name: Literal["vllm"]
    producer_version_candidate: Literal["0.26.0"]
    capability_state: Literal["UNPROVEN_REQUIRES_SPIKE"]
    tensor_parallel_size: Literal[1]


class HardwareControlTrack(FrozenModel):
    track_id: Literal["qwen3-8b-hardware-control"]
    track_kind: Literal["HARDWARE_CONTROL"]
    purpose: Literal["hold_model_runtime_and_workload_constant_across_gpu_tiers"]
    assignments: Annotated[
        tuple[PlannedProfileAssignment, ...], Field(min_length=4, max_length=4)
    ]
    current_comparison_contract: Literal[
        "NONE_CONTROLLED_COMPARISON_V1_DOES_NOT_SUPPORT_HARDWARE"
    ]
    cross_hardware_ratio_publication: Literal[
        "WITHHELD_PENDING_REVIEWED_HARDWARE_CONTRACT"
    ]
    causal_claim: Literal["NONE"]


class CapabilityLadderTrack(FrozenModel):
    track_id: Literal["qwen-capability-ladder"]
    track_kind: Literal["CAPABILITY_LADDER"]
    purpose: Literal["demonstrate_distinct_model_capacity_per_gpu_tier"]
    assignments: Annotated[
        tuple[PlannedProfileAssignment, ...], Field(min_length=4, max_length=4)
    ]
    current_comparison_contract: Literal["NONE_DIFFERENT_MODEL_PROFILES"]
    cross_profile_speed_ranking: Literal["PROHIBITED"]
    causal_claim: Literal["NONE"]


class PromptBucket(FrozenModel):
    target_input_tokens_under_control_tokenizer: Literal[128, 512, 1024]
    measured_requests: Literal[32]


class SamplingDesign(FrozenModel):
    thinking_mode: Literal["DISABLED"]
    temperature: Literal["0.7"]
    top_p: Literal["0.8"]
    top_k: Literal[20]
    min_p: Literal["0"]
    seed: Literal[42]
    ignore_eos: Literal[True]
    requested_output_tokens: Literal[128]


class CampaignWorkload(FrozenModel):
    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    prompt_content_class: Literal["PUBLIC_SYNTHETIC_NON_SENSITIVE"]
    ordered_prompt_bytes_policy: Literal[
        "same_ordered_utf8_prompt_bytes_across_tracks_v1"
    ]
    sizing_reference_model_key: Literal["qwen3-8b"]
    non_reference_token_counts: Literal["OBSERVED_NOT_CONFIGURED"]
    prompt_buckets: Annotated[
        tuple[PromptBucket, ...], Field(min_length=3, max_length=3)
    ]
    measured_requests_per_run: Literal[96]
    warmup_requests_per_run: Literal[12]
    warmup_strategy: Literal["vllm_first_measured_request_repeated_v0_26"]
    warmup_prompt_sequence_index: Literal[0]
    warmup_population: Literal["EXCLUDED_FROM_MEASUREMENTS"]
    readiness_probe_policy: Literal[
        "vllm_first_measured_request_until_success_bounded_v0_26"
    ]
    readiness_probe_population: Literal["EXCLUDED_FROM_MEASUREMENTS"]
    readiness_probe_timeout_seconds: Literal[5]
    concurrency_levels: tuple[Literal[1, 4, 16], Literal[1, 4, 16], Literal[1, 4, 16]]
    repetitions_per_condition: Literal[3]
    request_order: Literal["FIXED"]
    sampling: SamplingDesign

    @model_validator(mode="after")
    def buckets_and_traffic_must_be_exact(self) -> CampaignWorkload:
        expected_targets = (128, 512, 1024)
        targets = tuple(
            bucket.target_input_tokens_under_control_tokenizer
            for bucket in self.prompt_buckets
        )
        if targets != expected_targets:
            raise ValueError("prompt buckets must be ordered 128, 512, 1024")
        measured_total: int = sum(
            int(bucket.measured_requests) for bucket in self.prompt_buckets
        )
        if measured_total != int(self.measured_requests_per_run):
            raise ValueError("prompt buckets must cover every measured request")
        if self.concurrency_levels != (1, 4, 16):
            raise ValueError("campaign concurrency levels must be ordered 1, 4, 16")
        return self


class SessionBudget(FrozenModel):
    gpu_tier_id: CampaignKey
    hourly_rate_snapshot_usd: UsdDecimal
    max_session_cost_usd: UsdDecimal


class CampaignBudget(FrozenModel):
    rate_snapshot_date: Literal["2026-08-20"]
    currency: Literal["USD"]
    sessions: Annotated[tuple[SessionBudget, ...], Field(min_length=4, max_length=4)]
    max_compute_cost_usd: Literal["9.50"]
    price_verification: Literal["lambda_api_exact_match_before_capture_v1"]
    storage_cost: Literal["SEPARATE_OPERATOR_APPROVAL_REQUIRED"]

    @model_validator(mode="after")
    def session_caps_must_equal_campaign_cap(self) -> CampaignBudget:
        total = sum(
            (Decimal(session.max_session_cost_usd) for session in self.sessions),
            start=Decimal(0),
        )
        if total != Decimal(self.max_compute_cost_usd):
            raise ValueError("session cost caps must equal the campaign compute cap")
        return self


class HistoricalCanary(FrozenModel):
    role: Literal["HISTORICAL_CANARY_ONLY"]
    campaign_execution: Literal[False]
    model_id: Literal["Qwen/Qwen2.5-0.5B-Instruct"]
    model_revision: Literal["7ae557604adf67be50417f59c2c2f167def9a775"]
    producer_version: Literal["0.26.0"]
    profile_id: Literal["inferdrome.managed-vllm-0.26-evidence-profile.v1"]
    run_id: RunId
    bundle_digest: Sha256Digest
    mutation_policy: Literal["PRESERVE_EXISTING_EVIDENCE_BYTES"]


class ExecutionSafetyPolicy(FrozenModel):
    one_paid_instance_at_a_time: Literal[True]
    operator_confirmation_per_launch: Literal[True]
    api_rate_match_required: Literal[True]
    watchdog_ready_before_remote_setup: Literal[True]
    terminate_in_controller_finally: Literal[True]
    provider_termination_confirmation_required: Literal[True]
    shell_shutdown_is_termination: Literal[False]
    local_archive_checksum_before_immediate_termination: Literal[True]
    offline_semantic_verification_after_termination: Literal[True]


class PublicationPolicy(FrozenModel):
    verified_real_bundles_only: Literal[True]
    generated_metrics_only: Literal[True]
    synthetic_campaign_results: Literal["EXCLUDED"]
    hardware_control_ratios: Literal["WITHHELD_PENDING_REVIEWED_HARDWARE_CONTRACT"]
    capability_ladder_speed_ranking: Literal["PROHIBITED"]
    inferdrome_acceptance_verdict: None


class GpuCampaignPlan(FrozenModel):
    """Design-only plan that cannot itself authorize or attest execution."""

    schema_version: Literal["inferdrome.gpu-campaign-plan.v1"]
    campaign_id: CampaignId
    plan_status: Literal["DESIGN_FROZEN_EXECUTION_BLOCKED"]
    frozen_on: Literal["2026-08-20"]
    historical_canary: HistoricalCanary
    model_pins: Annotated[tuple[ModelPin, ...], Field(min_length=4, max_length=4)]
    gpu_targets: Annotated[tuple[GpuTarget, ...], Field(min_length=4, max_length=4)]
    hardware_control: HardwareControlTrack
    capability_ladder: CapabilityLadderTrack
    workload: CampaignWorkload
    budget: CampaignBudget
    execution_safety: ExecutionSafetyPolicy
    publication: PublicationPolicy
    profile_implementation_gate: Literal[
        "EVERY_PLANNED_PROFILE_MUST_PASS_LOCAL_CONFORMANCE_BEFORE_PAID_LAUNCH"
    ]
    runtime_compatibility_gate: Literal[
        "EVERY_MODEL_RUNTIME_GPU_TUPLE_REQUIRES_A_BOUNDED_CAPABILITY_SPIKE"
    ]
    paid_launch_authorization: Literal["NOT_GRANTED_BY_THIS_DOCUMENT"]
    hardware_attestation: Literal[False]
    acceptance_authority: Literal["EXTERNAL_CONSUMER_ONLY"]

    @model_validator(mode="after")
    def campaign_design_must_be_closed(self) -> GpuCampaignPlan:
        expected_targets = (
            "a10-24gb-pcie",
            "a100-40gb-pcie",
            "h100-80gb-pcie",
            "b200-180gb-sxm6",
        )
        target_ids = tuple(target.gpu_tier_id for target in self.gpu_targets)
        if target_ids != expected_targets or len(set(target_ids)) != len(target_ids):
            raise ValueError("GPU targets must be the frozen ordered four-tier matrix")

        model_by_key = {model.model_key: model for model in self.model_pins}
        if len(model_by_key) != len(self.model_pins):
            raise ValueError("campaign model keys must be unique")
        expected_models = {
            "qwen3-8b",
            "qwen3-14b",
            "qwen3-32b",
            "qwen3.5-122b-a10b-fp8",
        }
        if set(model_by_key) != expected_models:
            raise ValueError("campaign model pins must match the frozen model matrix")

        control = self.hardware_control.assignments
        ladder = self.capability_ladder.assignments
        for label, assignments in (("control", control), ("ladder", ladder)):
            assignment_targets = tuple(item.gpu_tier_id for item in assignments)
            if assignment_targets != expected_targets:
                raise ValueError(f"{label} assignments must cover GPU tiers in order")

        if {assignment.model_key for assignment in control} != {"qwen3-8b"}:
            raise ValueError("hardware-control assignments must use only Qwen3-8B")
        ladder_keys = tuple(assignment.model_key for assignment in ladder)
        expected_ladder = (
            "qwen3-8b",
            "qwen3-14b",
            "qwen3-32b",
            "qwen3.5-122b-a10b-fp8",
        )
        if ladder_keys != expected_ladder:
            raise ValueError("capability ladder must use the frozen model ordering")
        ladder_sizes = tuple(model_by_key[key].total_parameters for key in ladder_keys)
        if any(candidate <= baseline for baseline, candidate in pairwise(ladder_sizes)):
            raise ValueError("capability-ladder parameter counts must increase")

        all_assignments = (*control, *ladder)
        if any(item.model_key not in model_by_key for item in all_assignments):
            raise ValueError("campaign assignment references an unknown model pin")
        if any(
            item.capability_state != "UNPROVEN_REQUIRES_SPIKE"
            for item in all_assignments
        ):
            raise ValueError("design-only assignments cannot claim proven capability")

        budget_targets = tuple(session.gpu_tier_id for session in self.budget.sessions)
        if budget_targets != expected_targets:
            raise ValueError("session budgets must cover GPU tiers in order")
        if self.historical_canary.model_id in {
            model.model_id for model in self.model_pins
        }:
            raise ValueError("the historical canary cannot enter the new campaign")
        return self


def gpu_campaign_plan_schema() -> dict[str, Any]:
    """Render the closed, explicitly operational campaign-plan schema."""

    schema = copy.deepcopy(
        GpuCampaignPlan.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = CAMPAIGN_SCHEMA_ID
    schema["title"] = "Inferdrome operational GPU campaign plan v1"
    return schema


def _assignment(
    gpu_tier_id: str,
    model_key: str,
    profile_id: str,
) -> dict[str, Any]:
    return {
        "capability_state": "UNPROVEN_REQUIRES_SPIKE",
        "gpu_tier_id": gpu_tier_id,
        "model_key": model_key,
        "planned_profile_id": profile_id,
        "producer_name": "vllm",
        "producer_version_candidate": "0.26.0",
        "tensor_parallel_size": 1,
    }


def canonical_qwen_gpu_campaign() -> GpuCampaignPlan:
    """Return the exact design frozen before profile implementation or GPU spend."""

    target_ids = (
        "a10-24gb-pcie",
        "a100-40gb-pcie",
        "h100-80gb-pcie",
        "b200-180gb-sxm6",
    )
    qwen3_8b_profile = "managed-vllm-0.26-qwen3-8b-bf16-v1"
    payload = {
        "acceptance_authority": "EXTERNAL_CONSUMER_ONLY",
        "budget": {
            "currency": "USD",
            "max_compute_cost_usd": "9.50",
            "price_verification": "lambda_api_exact_match_before_capture_v1",
            "rate_snapshot_date": "2026-08-20",
            "sessions": [
                {
                    "gpu_tier_id": target_ids[0],
                    "hourly_rate_snapshot_usd": "1.29",
                    "max_session_cost_usd": "0.75",
                },
                {
                    "gpu_tier_id": target_ids[1],
                    "hourly_rate_snapshot_usd": "1.99",
                    "max_session_cost_usd": "1.25",
                },
                {
                    "gpu_tier_id": target_ids[2],
                    "hourly_rate_snapshot_usd": "3.29",
                    "max_session_cost_usd": "2.25",
                },
                {
                    "gpu_tier_id": target_ids[3],
                    "hourly_rate_snapshot_usd": "6.99",
                    "max_session_cost_usd": "5.25",
                },
            ],
            "storage_cost": "SEPARATE_OPERATOR_APPROVAL_REQUIRED",
        },
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "capability_ladder": {
            "assignments": [
                _assignment(target_ids[0], "qwen3-8b", qwen3_8b_profile),
                _assignment(
                    target_ids[1],
                    "qwen3-14b",
                    "managed-vllm-0.26-qwen3-14b-bf16-v1",
                ),
                _assignment(
                    target_ids[2],
                    "qwen3-32b",
                    "managed-vllm-0.26-qwen3-32b-bf16-v1",
                ),
                _assignment(
                    target_ids[3],
                    "qwen3.5-122b-a10b-fp8",
                    "managed-vllm-0.26-qwen3.5-122b-a10b-fp8-v1",
                ),
            ],
            "causal_claim": "NONE",
            "cross_profile_speed_ranking": "PROHIBITED",
            "current_comparison_contract": "NONE_DIFFERENT_MODEL_PROFILES",
            "purpose": "demonstrate_distinct_model_capacity_per_gpu_tier",
            "track_id": "qwen-capability-ladder",
            "track_kind": "CAPABILITY_LADDER",
        },
        "execution_safety": {
            "api_rate_match_required": True,
            "local_archive_checksum_before_immediate_termination": True,
            "offline_semantic_verification_after_termination": True,
            "one_paid_instance_at_a_time": True,
            "operator_confirmation_per_launch": True,
            "provider_termination_confirmation_required": True,
            "shell_shutdown_is_termination": False,
            "terminate_in_controller_finally": True,
            "watchdog_ready_before_remote_setup": True,
        },
        "frozen_on": "2026-08-20",
        "hardware_attestation": False,
        "hardware_control": {
            "assignments": [
                _assignment(target, "qwen3-8b", qwen3_8b_profile)
                for target in target_ids
            ],
            "causal_claim": "NONE",
            "cross_hardware_ratio_publication": (
                "WITHHELD_PENDING_REVIEWED_HARDWARE_CONTRACT"
            ),
            "current_comparison_contract": (
                "NONE_CONTROLLED_COMPARISON_V1_DOES_NOT_SUPPORT_HARDWARE"
            ),
            "purpose": "hold_model_runtime_and_workload_constant_across_gpu_tiers",
            "track_id": "qwen3-8b-hardware-control",
            "track_kind": "HARDWARE_CONTROL",
        },
        "historical_canary": {
            "bundle_digest": (
                "sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675"
            ),
            "campaign_execution": False,
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "model_revision": "7ae557604adf67be50417f59c2c2f167def9a775",
            "mutation_policy": "PRESERVE_EXISTING_EVIDENCE_BYTES",
            "producer_version": "0.26.0",
            "profile_id": "inferdrome.managed-vllm-0.26-evidence-profile.v1",
            "role": "HISTORICAL_CANARY_ONLY",
            "run_id": "run-533c9f5f783958fb6077069a6c577144",
        },
        "model_pins": [
            {
                "activation_dtype": "bfloat16",
                "benchmark_modality": "TEXT_ONLY",
                "checkpoint_precision": "BF16",
                "kv_cache_dtype": "bfloat16",
                "license": "apache-2.0",
                "model_id": "Qwen/Qwen3-8B",
                "model_key": "qwen3-8b",
                "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "quantization_policy": "NONE",
                "server_model_mode": "TEXT_GENERATION",
                "thinking_mode": "DISABLED",
                "tokenizer_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "total_parameters": 8_190_735_360,
                "weight_format": "safetensors",
            },
            {
                "activation_dtype": "bfloat16",
                "benchmark_modality": "TEXT_ONLY",
                "checkpoint_precision": "BF16",
                "kv_cache_dtype": "bfloat16",
                "license": "apache-2.0",
                "model_id": "Qwen/Qwen3-14B",
                "model_key": "qwen3-14b",
                "model_revision": "40c069824f4251a91eefaf281ebe4c544efd3e18",
                "quantization_policy": "NONE",
                "server_model_mode": "TEXT_GENERATION",
                "thinking_mode": "DISABLED",
                "tokenizer_revision": "40c069824f4251a91eefaf281ebe4c544efd3e18",
                "total_parameters": 14_768_307_200,
                "weight_format": "safetensors",
            },
            {
                "activation_dtype": "bfloat16",
                "benchmark_modality": "TEXT_ONLY",
                "checkpoint_precision": "BF16",
                "kv_cache_dtype": "bfloat16",
                "license": "apache-2.0",
                "model_id": "Qwen/Qwen3-32B",
                "model_key": "qwen3-32b",
                "model_revision": "9216db5781bf21249d130ec9da846c4624c16137",
                "quantization_policy": "NONE",
                "server_model_mode": "TEXT_GENERATION",
                "thinking_mode": "DISABLED",
                "tokenizer_revision": "9216db5781bf21249d130ec9da846c4624c16137",
                "total_parameters": 32_762_123_264,
                "weight_format": "safetensors",
            },
            {
                "activation_dtype": "bfloat16",
                "benchmark_modality": "TEXT_ONLY",
                "checkpoint_precision": "OFFICIAL_FP8_E4M3",
                "kv_cache_dtype": "bfloat16",
                "license": "apache-2.0",
                "model_id": "Qwen/Qwen3.5-122B-A10B-FP8",
                "model_key": "qwen3.5-122b-a10b-fp8",
                "model_revision": "a099dee70ccfcd8d5dda56aaa0b60cb8ecadabc9",
                "quantization_policy": "CHECKPOINT_DEFINED_FP8",
                "server_model_mode": "LANGUAGE_MODEL_ONLY",
                "thinking_mode": "DISABLED",
                "tokenizer_revision": "a099dee70ccfcd8d5dda56aaa0b60cb8ecadabc9",
                "total_parameters": 125_094_006_512,
                "weight_format": "safetensors",
            },
        ],
        "paid_launch_authorization": "NOT_GRANTED_BY_THIS_DOCUMENT",
        "plan_status": "DESIGN_FROZEN_EXECUTION_BLOCKED",
        "profile_implementation_gate": (
            "EVERY_PLANNED_PROFILE_MUST_PASS_LOCAL_CONFORMANCE_BEFORE_PAID_LAUNCH"
        ),
        "publication": {
            "capability_ladder_speed_ranking": "PROHIBITED",
            "generated_metrics_only": True,
            "hardware_control_ratios": ("WITHHELD_PENDING_REVIEWED_HARDWARE_CONTRACT"),
            "inferdrome_acceptance_verdict": None,
            "synthetic_campaign_results": "EXCLUDED",
            "verified_real_bundles_only": True,
        },
        "runtime_compatibility_gate": (
            "EVERY_MODEL_RUNTIME_GPU_TUPLE_REQUIRES_A_BOUNDED_CAPABILITY_SPIKE"
        ),
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "gpu_targets": [
            {
                "gpu_count": 1,
                "gpu_model": "NVIDIA A10",
                "gpu_tier_id": target_ids[0],
                "interconnect": "PCIE",
                "provider": "lambda_cloud",
                "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
                "vram_gib": 24,
            },
            {
                "gpu_count": 1,
                "gpu_model": "NVIDIA A100",
                "gpu_tier_id": target_ids[1],
                "interconnect": "PCIE",
                "provider": "lambda_cloud",
                "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
                "vram_gib": 40,
            },
            {
                "gpu_count": 1,
                "gpu_model": "NVIDIA H100",
                "gpu_tier_id": target_ids[2],
                "interconnect": "PCIE",
                "provider": "lambda_cloud",
                "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
                "vram_gib": 80,
            },
            {
                "gpu_count": 1,
                "gpu_model": "NVIDIA B200",
                "gpu_tier_id": target_ids[3],
                "interconnect": "SXM6",
                "provider": "lambda_cloud",
                "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
                "vram_gib": 180,
            },
        ],
        "workload": {
            "concurrency_levels": [1, 4, 16],
            "measured_requests_per_run": 96,
            "non_reference_token_counts": "OBSERVED_NOT_CONFIGURED",
            "ordered_prompt_bytes_policy": (
                "same_ordered_utf8_prompt_bytes_across_tracks_v1"
            ),
            "prompt_buckets": [
                {
                    "measured_requests": 32,
                    "target_input_tokens_under_control_tokenizer": 128,
                },
                {
                    "measured_requests": 32,
                    "target_input_tokens_under_control_tokenizer": 512,
                },
                {
                    "measured_requests": 32,
                    "target_input_tokens_under_control_tokenizer": 1024,
                },
            ],
            "prompt_content_class": "PUBLIC_SYNTHETIC_NON_SENSITIVE",
            "readiness_probe_policy": (
                "vllm_first_measured_request_until_success_bounded_v0_26"
            ),
            "readiness_probe_population": "EXCLUDED_FROM_MEASUREMENTS",
            "readiness_probe_timeout_seconds": 5,
            "repetitions_per_condition": 3,
            "request_order": "FIXED",
            "sampling": {
                "ignore_eos": True,
                "min_p": "0",
                "requested_output_tokens": 128,
                "seed": 42,
                "temperature": "0.7",
                "thinking_mode": "DISABLED",
                "top_k": 20,
                "top_p": "0.8",
            },
            "sizing_reference_model_key": "qwen3-8b",
            "warmup_population": "EXCLUDED_FROM_MEASUREMENTS",
            "warmup_prompt_sequence_index": 0,
            "warmup_requests_per_run": 12,
            "warmup_strategy": "vllm_first_measured_request_repeated_v0_26",
            "workload_id": "inferdrome.qwen-text-mixed-length.v1",
        },
    }
    return GpuCampaignPlan.model_validate_json(
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    )


def conformance_cases() -> list[dict[str, Any]]:
    """Return structural and semantic mutation vectors for independent readers."""

    fixture = "valid/qwen-gpu-capability-campaign.json"
    return [
        {
            "fixture": fixture,
            "name": "valid-canonical-campaign",
            "plan_valid": True,
            "schema_valid": True,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "add",
                "path": "/acceptance_verdict",
                "value": "PASS",
            },
            "name": "rejects-unknown-acceptance-field",
            "plan_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/hardware_control/assignments/1/model_key",
                "value": "qwen3-14b",
            },
            "name": "rejects-control-model-drift",
            "plan_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/capability_ladder/assignments/3/model_key",
                "value": "qwen3-32b",
            },
            "name": "rejects-capability-ladder-collapse",
            "plan_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/model_pins/0/thinking_mode",
                "value": "ENABLED",
            },
            "name": "rejects-thinking-mode-drift",
            "plan_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/model_pins/1/model_revision",
                "value": "main",
            },
            "name": "rejects-floating-model-revision",
            "plan_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/budget/sessions/3/max_session_cost_usd",
                "value": "5.24",
            },
            "name": "rejects-budget-total-drift",
            "plan_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": fixture,
            "mutation": {
                "operation": "replace",
                "path": "/capability_ladder/cross_profile_speed_ranking",
                "value": "ALLOWED",
            },
            "name": "rejects-ladder-speed-ranking",
            "plan_valid": False,
            "schema_valid": False,
        },
    ]


def campaign_documents() -> dict[str, Any]:
    """Return every generated operational campaign document and fixture."""

    plan = canonical_qwen_gpu_campaign().model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    return {
        "campaigns/v1/gpu-campaign-plan.schema.json": gpu_campaign_plan_schema(),
        "campaigns/v1/qwen-gpu-capability-campaign.json": plan,
        "tests/fixtures/campaigns/v1/cases.json": conformance_cases(),
        "tests/fixtures/campaigns/v1/valid/qwen-gpu-capability-campaign.json": (
            copy.deepcopy(plan)
        ),
    }
