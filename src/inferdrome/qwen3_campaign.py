"""Executable, still-unproven Qwen3-8B campaign profile and workload bytes."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final, Literal

from pydantic import model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    CanonicalResponseContentPolicy,
    ConcurrentTraffic,
    ExperimentSpec,
    NativeOutputSensitivity,
    PromptContentPolicy,
    VllmExecution,
)
from inferdrome.domain.ids import Sha256Digest
from inferdrome.errors import AdapterError
from inferdrome.gpu_campaign import CANONICAL_CAMPAIGN_ID
from inferdrome.qwen3_tokenizer import (
    QWEN3_TOKENIZER_CONFIG_SHA256,
    QWEN3_TOKENIZER_JSON_SHA256,
    QWEN3_TOKENIZERS_VERSION,
    Qwen3TokenizerFileVerification,
    expected_qwen3_tokenizer_file_verification,
)

QWEN3_8B_PROFILE_ID: Final = "managed-vllm-0.26-qwen3-8b-bf16-v1"
QWEN3_8B_MODEL_ID: Final = "Qwen/Qwen3-8B"
QWEN3_8B_REVISION: Final = "b968826d9c46dd6066d109eabc6255188de91218"
QWEN3_WORKLOAD_ID: Final = "inferdrome.qwen-text-mixed-length.v1"
QWEN3_WORKLOAD_PATH: Final = "workloads/qwen-text-mixed-length-v1.jsonl"
QWEN3_BINDING_SCHEMA_VERSION: Final = "inferdrome.campaign-profile-binding.v1"
QWEN3_REQUEST_EXTRA_BODY: Final = (
    '{"chat_template_kwargs":{"enable_thinking":false},"seed":42}'
)
QWEN3_RENDERING_PREFIX: Final = "<|im_start|>user\n"
QWEN3_RENDERING_SUFFIX: Final = (
    "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
)
QWEN3_TEMPLATE_OVERHEAD_TOKENS: Final = 12
QWEN3_TARGET_INPUT_TOKENS: Final = (128, 512, 1024)
QWEN3_CONCURRENCY_LEVELS: Final = (1, 4, 16)


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _prompt(sequence_index: int, target_input_tokens: int) -> str:
    if not 0 <= sequence_index < 96:
        raise ValueError("campaign prompt sequence index is outside its range")
    if target_input_tokens not in QWEN3_TARGET_INPUT_TOKENS:
        raise ValueError("campaign prompt token target is unsupported")
    raw_target = target_input_tokens - QWEN3_TEMPLATE_OVERHEAD_TOKENS
    prefix = f"request {sequence_index} "
    # Under the exact tokenizer pin: `request`, one standalone space, and each
    # decimal digit are separate tokens. Every following ` hello` is one token.
    prefix_tokens = 2 + len(str(sequence_index))
    filler_tokens = raw_target - prefix_tokens
    if filler_tokens < 1:
        raise AssertionError
    return prefix + ("hello " * (filler_tokens - 1)) + "hello"


def qwen3_rendered_prompt(prompt: str) -> str:
    """Render the exact one-user-message, non-thinking server input."""

    return QWEN3_RENDERING_PREFIX + prompt + QWEN3_RENDERING_SUFFIX


def qwen3_workload_prompts() -> tuple[str, ...]:
    """Return 96 ordered, unique prompts across the frozen three buckets."""

    prompts: list[str] = []
    for bucket_index, target in enumerate(QWEN3_TARGET_INPUT_TOKENS):
        for offset in range(32):
            prompts.append(_prompt(bucket_index * 32 + offset, target))
    return tuple(prompts)


def qwen3_workload_bytes() -> bytes:
    """Return exact custom-JSONL bytes consumed by the pinned benchmark."""

    lines = (
        json.dumps(
            {"prompt": prompt},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for prompt in qwen3_workload_prompts()
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def qwen3_workload_sha256() -> str:
    return _sha256(qwen3_workload_bytes())


def qwen3_workload_manifest() -> dict[str, Any]:
    """Describe every line and the one-time tokenizer verification boundary."""

    prompts = qwen3_workload_prompts()
    records = []
    for sequence_index, prompt in enumerate(prompts):
        bucket_index = sequence_index // 32
        target = QWEN3_TARGET_INPUT_TOKENS[bucket_index]
        records.append(
            {
                "prompt_sha256": _sha256(prompt.encode("utf-8")),
                "raw_prompt_tokens": (target - QWEN3_TEMPLATE_OVERHEAD_TOKENS),
                "rendered_input_tokens": target,
                "sequence_index": sequence_index,
            }
        )
    return {
        "bucket_order": [
            {
                "first_sequence_index": index * 32,
                "measured_requests": 32,
                "rendered_input_tokens": target,
            }
            for index, target in enumerate(QWEN3_TARGET_INPUT_TOKENS)
        ],
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "canonicalization": {
            "document_digest": "sha256_of_rfc8785_canonical_json_bytes",
            "workload_digest": "sha256_of_exact_utf8_jsonl_bytes",
        },
        "generation_algorithm": (
            "request_decimal_prefix_plus_repeated_hello_tokens_v1"
        ),
        "line_count": len(prompts),
        "model_id": QWEN3_8B_MODEL_ID,
        "model_revision": QWEN3_8B_REVISION,
        "records": records,
        "rendering": {
            "add_generation_prompt": True,
            "enable_thinking": False,
            "prefix": QWEN3_RENDERING_PREFIX,
            "suffix": QWEN3_RENDERING_SUFFIX,
            "template_overhead_tokens": QWEN3_TEMPLATE_OVERHEAD_TOKENS,
        },
        "schema_version": "inferdrome.campaign-workload-manifest.v1",
        "tokenization_freeze": {
            "library": "tokenizers",
            "library_version": QWEN3_TOKENIZERS_VERSION,
            "file_verification_policy": ("bounded-regular-files-no-follow-sha256-v1"),
            "result": "96_OF_96_EXACT",
            "tokenizer_config_sha256": QWEN3_TOKENIZER_CONFIG_SHA256,
            "tokenizer_json_sha256": QWEN3_TOKENIZER_JSON_SHA256,
        },
        "tokenizer_revision": QWEN3_8B_REVISION,
        "readiness_probe": {
            "policy": ("vllm_first_measured_request_until_success_bounded_v0_26"),
            "population": "EXCLUDED_FROM_MEASUREMENTS",
            "sequence_index": 0,
            "timeout_seconds": 5,
        },
        "warmup": {
            "population": "EXCLUDED_FROM_MEASUREMENTS",
            "preceded_by_readiness_probe": (
                "vllm_first_measured_request_until_success_bounded_v0_26"
            ),
            "requests": 12,
            "sequence_index": 0,
            "strategy": "vllm_first_measured_request_repeated_v0_26",
        },
        "workload_bytes": len(qwen3_workload_bytes()),
        "workload_id": QWEN3_WORKLOAD_ID,
        "workload_sha256": qwen3_workload_sha256(),
    }


class Qwen3ProfileSampling(FrozenModel):
    ignore_eos: Literal[True]
    min_p: Literal["0"]
    seed: Literal[42]
    temperature: Literal["0.7"]
    top_k: Literal[20]
    top_p: Literal["0.8"]


class Qwen3BenchmarkInvocation(FrozenModel):
    request_extra_body: Literal[
        '{"chat_template_kwargs":{"enable_thinking":false},"seed":42}'
    ]
    ordered_profile_arguments: tuple[str, ...]
    readiness_probe_policy: Literal[
        "vllm_first_measured_request_until_success_bounded_v0_26"
    ]
    readiness_probe_population: Literal["EXCLUDED_FROM_MEASUREMENTS"]
    ready_check_timeout_seconds: Literal[5]
    requested_output_tokens: Literal[128]
    sampling: Qwen3ProfileSampling

    @model_validator(mode="after")
    def argument_order_must_be_exact(self) -> Qwen3BenchmarkInvocation:
        if self.ordered_profile_arguments != qwen3_benchmark_profile_arguments():
            raise ValueError("Qwen3 benchmark profile arguments drifted")
        return self


class Qwen3ProfileClaimsBoundary(FrozenModel):
    acceptance_verdict: Literal["NONE"]
    hardware_attestation: Literal[False]
    runtime_compatibility: Literal["UNPROVEN_REQUIRES_BOUNDED_SPIKE"]


class Qwen3ProfileDigestPolicy(FrozenModel):
    profile_sha256: Literal["sha256_of_rfc8785_canonical_json_bytes"]
    workload_manifest_sha256: Literal["sha256_of_rfc8785_canonical_json_bytes"]
    workload_sha256: Literal["sha256_of_exact_utf8_jsonl_bytes"]


class Qwen3ProfileModelPin(FrozenModel):
    activation_dtype: Literal["bfloat16"]
    checkpoint_precision: Literal["BF16"]
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]
    tokenizer_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]


class Qwen3ProfileProducerPin(FrozenModel):
    adapter_name: Literal["vllm_bench_serve"]
    adapter_version: Literal["1.0.0"]
    name: Literal["vllm"]
    version: Literal["0.26.0"]


class Qwen3ServerInvocation(FrozenModel):
    device_ids: Literal["selected_single_physical_gpu"]
    dtype: Literal["bfloat16"]
    generation_config: Literal["vllm"]
    gpu_memory_utilization: Literal["0.90"]
    load_format: Literal["safetensors"]
    max_model_len: Literal[2048]
    model_impl: Literal["vllm"]
    tensor_parallel_size: Literal[1]


class Qwen3ProfileWorkloadBinding(FrozenModel):
    manifest_sha256: Sha256Digest
    measured_requests: Literal[96]
    warmup_requests: Literal[12]
    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    workload_sha256: Sha256Digest

    @model_validator(mode="after")
    def exact_workload_documents_must_be_bound(
        self,
    ) -> Qwen3ProfileWorkloadBinding:
        manifest_digest = _sha256(canonical_json_bytes(qwen3_workload_manifest()))
        if self.manifest_sha256 != manifest_digest:
            raise ValueError("Qwen3 workload manifest digest drifted")
        if self.workload_sha256 != qwen3_workload_sha256():
            raise ValueError("Qwen3 workload digest drifted")
        return self


class Qwen3ManagedProfile(FrozenModel):
    benchmark_invocation: Qwen3BenchmarkInvocation
    campaign_id: Literal["qwen-gpu-capability-campaign-v1"]
    claims_boundary: Qwen3ProfileClaimsBoundary
    digest_policy: Qwen3ProfileDigestPolicy
    implementation_state: Literal["LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"]
    model: Qwen3ProfileModelPin
    producer: Qwen3ProfileProducerPin
    profile_id: Literal["managed-vllm-0.26-qwen3-8b-bf16-v1"]
    schema_version: Literal["inferdrome.campaign-managed-vllm-profile.v1"]
    server_invocation: Qwen3ServerInvocation
    workload_binding: Qwen3ProfileWorkloadBinding


def qwen3_profile_document() -> dict[str, Any]:
    """Return the standalone operational profile; real capability is unproven."""

    manifest = qwen3_workload_manifest()
    value = {
        "benchmark_invocation": {
            "request_extra_body": QWEN3_REQUEST_EXTRA_BODY,
            "ordered_profile_arguments": qwen3_benchmark_profile_arguments(),
            "readiness_probe_policy": (
                "vllm_first_measured_request_until_success_bounded_v0_26"
            ),
            "readiness_probe_population": "EXCLUDED_FROM_MEASUREMENTS",
            "ready_check_timeout_seconds": 5,
            "requested_output_tokens": 128,
            "sampling": {
                "ignore_eos": True,
                "min_p": "0",
                "seed": 42,
                "temperature": "0.7",
                "top_k": 20,
                "top_p": "0.8",
            },
        },
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "claims_boundary": {
            "acceptance_verdict": "NONE",
            "hardware_attestation": False,
            "runtime_compatibility": "UNPROVEN_REQUIRES_BOUNDED_SPIKE",
        },
        "digest_policy": {
            "profile_sha256": "sha256_of_rfc8785_canonical_json_bytes",
            "workload_manifest_sha256": ("sha256_of_rfc8785_canonical_json_bytes"),
            "workload_sha256": "sha256_of_exact_utf8_jsonl_bytes",
        },
        "implementation_state": "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN",
        "model": {
            "activation_dtype": "bfloat16",
            "checkpoint_precision": "BF16",
            "model_id": QWEN3_8B_MODEL_ID,
            "model_revision": QWEN3_8B_REVISION,
            "tokenizer_revision": QWEN3_8B_REVISION,
        },
        "producer": {
            "adapter_name": "vllm_bench_serve",
            "adapter_version": "1.0.0",
            "name": "vllm",
            "version": "0.26.0",
        },
        "profile_id": QWEN3_8B_PROFILE_ID,
        "schema_version": "inferdrome.campaign-managed-vllm-profile.v1",
        "server_invocation": {
            "device_ids": "selected_single_physical_gpu",
            "dtype": "bfloat16",
            "generation_config": "vllm",
            "gpu_memory_utilization": "0.90",
            "load_format": "safetensors",
            "max_model_len": 2048,
            "model_impl": "vllm",
            "tensor_parallel_size": 1,
        },
        "workload_binding": {
            "manifest_sha256": _sha256(canonical_json_bytes(manifest)),
            "measured_requests": 96,
            "warmup_requests": 12,
            "workload_id": QWEN3_WORKLOAD_ID,
            "workload_sha256": qwen3_workload_sha256(),
        },
    }
    return Qwen3ManagedProfile.model_validate(value).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )


def qwen3_profile_schema() -> dict[str, Any]:
    schema = Qwen3ManagedProfile.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:inferdrome:qwen3-8b-campaign-profile:v1"
    schema["title"] = "Inferdrome Qwen3-8B campaign managed-vLLM profile v1"
    return schema


def qwen3_profile_sha256() -> str:
    return _sha256(canonical_json_bytes(qwen3_profile_document()))


class Qwen3CampaignProfileBinding(FrozenModel):
    """Invocation-level binding to the exact operational profile and workload."""

    schema_version: Literal["inferdrome.campaign-profile-binding.v1"]
    campaign_id: Literal["qwen-gpu-capability-campaign-v1"]
    profile_id: Literal["managed-vllm-0.26-qwen3-8b-bf16-v1"]
    profile_sha256: Sha256Digest
    tokenizer_files: Qwen3TokenizerFileVerification
    workload_id: Literal["inferdrome.qwen-text-mixed-length.v1"]
    workload_sha256: Sha256Digest

    @model_validator(mode="after")
    def exact_generated_documents_must_be_bound(
        self,
    ) -> Qwen3CampaignProfileBinding:
        if self.profile_sha256 != qwen3_profile_sha256():
            raise ValueError("campaign profile digest disagrees")
        if self.workload_sha256 != qwen3_workload_sha256():
            raise ValueError("campaign workload digest disagrees")
        return self


def validate_qwen3_campaign_spec(spec: ExperimentSpec) -> None:
    """Reject any resolved input that drifts from the Qwen3 profile."""

    execution = spec.execution
    target = spec.target
    traffic = spec.traffic
    if (
        str(spec.experiment.id) != "qwen3-8b-campaign-v1"
        or spec.experiment.title != "Pinned Qwen3-8B campaign execution profile"
        or spec.experiment.hypothesis
        != "The frozen Qwen3-8B workload can produce one verified run."
    ):
        raise AdapterError("Qwen3 campaign experiment identity drifted")
    if not isinstance(execution, VllmExecution) or not isinstance(
        target, AttachedVllmTarget
    ):
        raise AdapterError("Qwen3 campaign profile requires attached vLLM")
    if (
        execution.adapter != "vllm_bench_serve"
        or execution.adapter_version != "1.0.0"
        or execution.producer_name != "vllm"
        or execution.producer_version != "0.26.0"
        or execution.max_runtime_seconds != 1_800
        or execution.max_measured_requests != 96
    ):
        raise AdapterError("Qwen3 campaign execution configuration drifted")
    if (
        target.model != QWEN3_8B_MODEL_ID
        or target.model_revision != QWEN3_8B_REVISION
        or target.tokenizer_revision != QWEN3_8B_REVISION
        or target.engine_version != "0.26.0"
        or str(target.endpoint).rstrip("/") != "http://127.0.0.1:18080"
    ):
        raise AdapterError("Qwen3 campaign target identity drifted")
    if (
        str(spec.workload.path) != QWEN3_WORKLOAD_PATH
        or spec.workload.sha256 != qwen3_workload_sha256()
        or spec.workload.prompt_content_policy is not PromptContentPolicy.INCLUDE
        or spec.workload.requested_output_tokens != 128
        or spec.workload.temperature != "0.7"
        or spec.workload.seed != 42
    ):
        raise AdapterError("Qwen3 campaign workload configuration drifted")
    if (
        not isinstance(traffic, ConcurrentTraffic)
        or traffic.concurrency not in QWEN3_CONCURRENCY_LEVELS
        or traffic.warmup_requests != 12
        or traffic.measured_requests != 96
    ):
        raise AdapterError("Qwen3 campaign traffic configuration drifted")
    if (
        spec.measurement.streaming is not True
        or spec.measurement.ttft_definition != "vllm_first_choices_event_v0_26"
        or spec.measurement.choices_span_definition != "last_choices_event_span_v1"
        or spec.measurement.metric_definitions_version != "1.0.0"
        or spec.measurement.reducer_version != "1.0.0"
    ):
        raise AdapterError("Qwen3 campaign measurement configuration drifted")
    if (
        spec.evidence.native_output_sensitivity
        is not NativeOutputSensitivity.RESPONSE_CONTENT
        or spec.evidence.canonical_response_content
        is not CanonicalResponseContentPolicy.OMIT
        or spec.evidence.include_request_plan is not True
        or spec.links.exitspec_contract_digest is not None
    ):
        raise AdapterError("Qwen3 campaign evidence configuration drifted")


def require_qwen3_campaign_profile(
    spec: ExperimentSpec,
    capability_profile_id: str | None,
) -> None:
    """Prevent the named campaign source from falling back to legacy controls."""

    if (
        str(spec.experiment.id) == "qwen3-8b-campaign-v1"
        and capability_profile_id != QWEN3_8B_PROFILE_ID
    ):
        raise AdapterError(
            "Qwen3 campaign requires its explicit managed capability profile"
        )


def qwen3_campaign_profile_binding(
    spec: ExperimentSpec,
    tokenizer_files: Qwen3TokenizerFileVerification,
) -> Qwen3CampaignProfileBinding:
    validate_qwen3_campaign_spec(spec)
    if tokenizer_files != expected_qwen3_tokenizer_file_verification():
        raise AdapterError("Qwen3 campaign tokenizer verification drifted")
    return Qwen3CampaignProfileBinding(
        schema_version=QWEN3_BINDING_SCHEMA_VERSION,
        campaign_id="qwen-gpu-capability-campaign-v1",
        profile_id=QWEN3_8B_PROFILE_ID,
        profile_sha256=qwen3_profile_sha256(),
        tokenizer_files=tokenizer_files,
        workload_id=QWEN3_WORKLOAD_ID,
        workload_sha256=qwen3_workload_sha256(),
    )


def validate_qwen3_campaign_profile_binding(
    value: Any,
    spec: ExperimentSpec,
) -> Qwen3CampaignProfileBinding:
    validate_qwen3_campaign_spec(spec)
    try:
        binding = Qwen3CampaignProfileBinding.model_validate(value)
    except (TypeError, ValueError):
        raise AdapterError("Qwen3 campaign profile binding is invalid") from None
    expected = qwen3_campaign_profile_binding(
        spec,
        expected_qwen3_tokenizer_file_verification(),
    )
    if binding != expected:
        raise AdapterError("Qwen3 campaign profile binding drifted")
    return binding


def qwen3_benchmark_profile_arguments() -> tuple[str, ...]:
    return (
        "--top-p",
        "0.8",
        "--top-k",
        "20",
        "--min-p",
        "0",
        "--ignore-eos",
        "--extra-body",
        QWEN3_REQUEST_EXTRA_BODY,
    )


def _attached_source_yaml(concurrency: int) -> bytes:
    if concurrency not in QWEN3_CONCURRENCY_LEVELS:
        raise ValueError("Qwen3 campaign concurrency is unsupported")
    return f"""schema_version: inferdrome.source-experiment.v1

experiment:
  id: qwen3-8b-campaign-v1
  title: Pinned Qwen3-8B campaign execution profile
  hypothesis: The frozen Qwen3-8B workload can produce one verified run.

execution:
  mode: attached_endpoint
  max_runtime_seconds: 1800
  max_measured_requests: 96

target:
  engine: vllm
  endpoint: http://127.0.0.1:18080
  model: {QWEN3_8B_MODEL_ID}
  model_revision: {QWEN3_8B_REVISION}
  tokenizer_revision: {QWEN3_8B_REVISION}
  engine_version: 0.26.0

workload:
  path: {QWEN3_WORKLOAD_PATH}
  sha256: {qwen3_workload_sha256()}
  prompt_content_policy: include
  requested_output_tokens: 128
  temperature: "0.7"
  seed: 42

traffic:
  kind: concurrent
  concurrency: {concurrency}
  warmup_requests: 12
  measured_requests: 96

evidence:
  canonical_response_content: omit
""".encode()


def _fake_source_yaml() -> bytes:
    return f"""schema_version: inferdrome.source-experiment.v1

experiment:
  id: qwen3-campaign-workload-synthetic-smoke
  title: Synthetic wiring test for the frozen Qwen3 campaign workload

execution:
  mode: synthetic_fixture
  max_runtime_seconds: 300

target:
  engine: fake
  model: inferdrome/qwen3-campaign-workload-fixture

workload:
  path: {QWEN3_WORKLOAD_PATH}
  sha256: {qwen3_workload_sha256()}
  prompt_content_policy: include
  requested_output_tokens: 128
  temperature: "0.7"
  seed: 42

traffic:
  kind: concurrent
  concurrency: 1
  warmup_requests: 12
  measured_requests: 96

evidence:
  canonical_response_content: omit
""".encode()


def qwen3_profile_conformance_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "valid-qwen3-profile",
            "profile_valid": True,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "add",
                "path": "/winner",
                "value": "A10",
            },
            "name": "rejects-unknown-authoritative-field",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/implementation_state",
                "value": "RUNTIME_PROVEN",
            },
            "name": "rejects-unearned-runtime-claim",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/workload_binding/manifest_sha256",
                "value": "sha256:" + "0" * 64,
            },
            "name": "rejects-workload-manifest-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/benchmark_invocation/ordered_profile_arguments/1",
                "value": "0.9",
            },
            "name": "rejects-benchmark-argument-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "mutation": {
                "operation": "replace",
                "path": "/benchmark_invocation/request_extra_body",
                "value": '{"seed":42}',
            },
            "name": "rejects-thinking-mode-drift",
            "profile_valid": False,
            "schema_valid": False,
        },
    ]


def qwen3_launch_documents() -> dict[str, bytes]:
    """Return every generated profile, workload, and source byte sequence."""

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

    documents = {
        "campaigns/v1/profiles/qwen3-8b-profile.schema.json": pretty_json(
            qwen3_profile_schema()
        ),
        "campaigns/v1/profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json": (
            pretty_json(qwen3_profile_document())
        ),
        "campaigns/v1/workloads/qwen-text-mixed-length-v1.jsonl": (
            qwen3_workload_bytes()
        ),
        "campaigns/v1/workloads/qwen-text-mixed-length-v1.manifest.json": (
            pretty_json(qwen3_workload_manifest())
        ),
        "campaigns/v1/qwen3-8b-concurrency-1.yaml": _attached_source_yaml(1),
        "campaigns/v1/qwen3-8b-concurrency-4.yaml": _attached_source_yaml(4),
        "campaigns/v1/qwen3-8b-concurrency-16.yaml": _attached_source_yaml(16),
        "campaigns/v1/qwen3-workload-synthetic-smoke.yaml": _fake_source_yaml(),
        "tests/fixtures/campaigns/v1/qwen3-profile-cases.json": pretty_json(
            qwen3_profile_conformance_cases()
        ),
    }
    return documents
