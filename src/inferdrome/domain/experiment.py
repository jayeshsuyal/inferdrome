"""Resolved experiment contract for v0.1."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import (
    DecimalString,
    ExperimentId,
    OpaqueName,
    RelativeArtifactPath,
    SemanticVersion,
    Sha256Digest,
)


class PromptContentPolicy(StrEnum):
    HASH_ONLY = "hash_only"
    INCLUDE = "include"


class CanonicalResponseContentPolicy(StrEnum):
    OMIT = "omit"
    INCLUDE = "include"


class NativeOutputSensitivity(StrEnum):
    RESPONSE_CONTENT = "RESPONSE_CONTENT"
    NON_SENSITIVE_FIXTURE = "NON_SENSITIVE_FIXTURE"


SecretFreeHttpUrl = Annotated[
    AnyHttpUrl,
    Field(
        json_schema_extra={
            "pattern": r"^https?://[^/?#@]+(?:/[^?#]*)?$",
        }
    ),
]


class ExperimentIdentity(FrozenModel):
    id: ExperimentId
    title: Annotated[str, Field(min_length=1, max_length=256)]
    hypothesis: Annotated[str, Field(min_length=1, max_length=4096)] | None


class VllmExecution(FrozenModel):
    mode: Literal["attached_endpoint"]
    adapter: Literal["vllm_bench_serve"]
    adapter_version: SemanticVersion
    producer_name: Literal["vllm"]
    producer_version: Literal["0.26.0"]
    max_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    max_measured_requests: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]


class FakeExecution(FrozenModel):
    mode: Literal["synthetic_fixture"]
    adapter: Literal["fake"]
    adapter_version: SemanticVersion
    producer_name: Literal["inferdrome_fake"]
    producer_version: SemanticVersion
    max_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)]
    max_measured_requests: Annotated[int, Field(strict=True, ge=1, le=100_000)]


ExecutionConfig = Annotated[
    VllmExecution | FakeExecution,
    Field(discriminator="mode"),
]


class AttachedVllmTarget(FrozenModel):
    engine: Literal["vllm"]
    endpoint: SecretFreeHttpUrl
    api: Literal["openai_chat_completions"]
    model: OpaqueName
    model_revision: OpaqueName | None
    tokenizer_revision: OpaqueName | None
    engine_version: SemanticVersion | None

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_not_carry_secrets(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("endpoint user information is forbidden")
        if value.query is not None or value.fragment is not None:
            raise ValueError("endpoint query strings and fragments are forbidden")
        return value


class SyntheticTarget(FrozenModel):
    engine: Literal["fake"]
    api: Literal["synthetic_fixture"]
    model: OpaqueName


TargetConfig = Annotated[
    AttachedVllmTarget | SyntheticTarget,
    Field(discriminator="engine"),
]


class WorkloadConfig(FrozenModel):
    path: RelativeArtifactPath
    sha256: Sha256Digest
    prompt_content_policy: PromptContentPolicy
    requested_output_tokens: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    temperature: DecimalString
    seed: Annotated[int, Field(strict=True, ge=0, le=4_294_967_295)]


class ConcurrentTraffic(FrozenModel):
    kind: Literal["concurrent"]
    concurrency: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    warmup_requests: NonNegativeInt
    measured_requests: PositiveInt


class RequestRateTraffic(FrozenModel):
    kind: Literal["request_rate"]
    requests_per_second: DecimalString
    burstiness: DecimalString
    max_concurrency: PositiveInt | None
    warmup_requests: NonNegativeInt
    measured_requests: PositiveInt

    @field_validator("requests_per_second", "burstiness")
    @classmethod
    def decimal_must_be_positive(cls, value: str) -> str:
        if not any(character != "0" and character != "." for character in value):
            raise ValueError("configured decimal must be greater than zero")
        return value


TrafficConfig = Annotated[
    ConcurrentTraffic | RequestRateTraffic,
    Field(discriminator="kind"),
]


class MeasurementConfig(FrozenModel):
    streaming: Literal[True]
    ttft_definition: Literal["vllm_first_choices_event_v0_26"]
    choices_span_definition: Literal["last_choices_event_span_v1"]
    metric_definitions_version: SemanticVersion
    reducer_version: SemanticVersion


class EvidenceConfig(FrozenModel):
    native_output_sensitivity: NativeOutputSensitivity
    canonical_response_content: CanonicalResponseContentPolicy
    include_request_plan: Literal[True]


class ExperimentLinks(FrozenModel):
    exitspec_contract_digest: Sha256Digest | None


class ExperimentSpec(FrozenModel):
    """Fully resolved execution specification frozen before measurement."""

    schema_version: Literal["inferdrome.experiment.v1"]
    experiment: ExperimentIdentity
    execution: ExecutionConfig
    target: TargetConfig
    workload: WorkloadConfig
    traffic: TrafficConfig
    measurement: MeasurementConfig
    evidence: EvidenceConfig
    links: ExperimentLinks

    @model_validator(mode="after")
    def validate_execution_pairing(self) -> "ExperimentSpec":
        if isinstance(self.execution, VllmExecution):
            if not isinstance(self.target, AttachedVllmTarget):
                raise ValueError("attached vLLM execution requires a vLLM target")
            if (
                self.evidence.native_output_sensitivity
                is not NativeOutputSensitivity.RESPONSE_CONTENT
            ):
                raise ValueError("detailed vLLM output is response-content-bearing")
        else:
            if not isinstance(self.target, SyntheticTarget):
                raise ValueError("fake execution requires a synthetic target")
            if (
                self.evidence.native_output_sensitivity
                is not NativeOutputSensitivity.NON_SENSITIVE_FIXTURE
            ):
                raise ValueError("fake fixture must use its explicit sensitivity class")

        if self.traffic.measured_requests > self.execution.max_measured_requests:
            raise ValueError("traffic exceeds execution measured-request limit")
        return self
