"""Strict user-authored source experiment model."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.experiment import (
    CanonicalResponseContentPolicy,
    PromptContentPolicy,
    SecretFreeHttpUrl,
)
from inferdrome.domain.ids import (
    DecimalString,
    ExperimentId,
    OpaqueName,
    RelativeArtifactPath,
    SemanticVersion,
    Sha256Digest,
)

SourceDecimal = Annotated[int, Field(strict=True, ge=0)] | DecimalString


class SourceExperimentIdentity(FrozenModel):
    id: ExperimentId
    title: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    hypothesis: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class SourceVllmExecution(FrozenModel):
    mode: Literal["attached_endpoint"]
    adapter: Literal["vllm_bench_serve"] = "vllm_bench_serve"
    adapter_version: Literal["1.0.0"] = "1.0.0"
    producer_name: Literal["vllm"] = "vllm"
    producer_version: Literal["0.26.0"] = "0.26.0"
    max_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)] = 900
    max_measured_requests: Annotated[
        int, Field(strict=True, ge=1, le=1_000_000)
    ] | None = None


class SourceFakeExecution(FrozenModel):
    mode: Literal["synthetic_fixture"]
    adapter: Literal["fake"] = "fake"
    adapter_version: Literal["1.0.0"] = "1.0.0"
    producer_name: Literal["inferdrome_fake"] = "inferdrome_fake"
    producer_version: Literal["1.0.0"] = "1.0.0"
    max_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)] = 60
    max_measured_requests: Annotated[
        int, Field(strict=True, ge=1, le=100_000)
    ] | None = None


SourceExecution = Annotated[
    SourceVllmExecution | SourceFakeExecution,
    Field(discriminator="mode"),
]


class SourceAttachedVllmTarget(FrozenModel):
    engine: Literal["vllm"]
    endpoint: SecretFreeHttpUrl
    api: Literal["openai_chat_completions"] = "openai_chat_completions"
    model: OpaqueName
    model_revision: OpaqueName | None = None
    tokenizer_revision: OpaqueName | None = None
    engine_version: SemanticVersion | None = None


class SourceSyntheticTarget(FrozenModel):
    engine: Literal["fake"]
    api: Literal["synthetic_fixture"] = "synthetic_fixture"
    model: OpaqueName = "inferdrome/fake-model"


SourceTarget = Annotated[
    SourceAttachedVllmTarget | SourceSyntheticTarget,
    Field(discriminator="engine"),
]


class SourceWorkload(FrozenModel):
    path: RelativeArtifactPath
    sha256: Sha256Digest | None = None
    prompt_content_policy: PromptContentPolicy = PromptContentPolicy.INCLUDE
    requested_output_tokens: Annotated[
        int, Field(strict=True, ge=1, le=1_000_000)
    ] = 128
    temperature: SourceDecimal = 0
    seed: Annotated[int, Field(strict=True, ge=0, le=4_294_967_295)] = 42


class SourceConcurrentTraffic(FrozenModel):
    kind: Literal["concurrent"]
    concurrency: Annotated[int, Field(strict=True, ge=1, le=100_000)] = 1
    warmup_requests: Annotated[int, Field(strict=True, ge=0)] = 0
    measured_requests: Annotated[
        int, Field(strict=True, ge=1, le=1_000_000)
    ] = 100


class SourceRequestRateTraffic(FrozenModel):
    kind: Literal["request_rate"]
    requests_per_second: SourceDecimal
    burstiness: SourceDecimal = 1
    max_concurrency: Annotated[
        int, Field(strict=True, ge=1, le=100_000)
    ] | None = None
    warmup_requests: Annotated[int, Field(strict=True, ge=0)] = 0
    measured_requests: Annotated[
        int, Field(strict=True, ge=1, le=1_000_000)
    ] = 100

    @field_validator("requests_per_second", "burstiness")
    @classmethod
    def source_decimal_must_be_positive(cls, value: int | str) -> int | str:
        if Decimal(str(value)) <= 0:
            raise ValueError("configured decimal must be greater than zero")
        return value


SourceTraffic = Annotated[
    SourceConcurrentTraffic | SourceRequestRateTraffic,
    Field(discriminator="kind"),
]


class SourceMeasurement(FrozenModel):
    streaming: Literal[True] = True
    ttft_definition: Literal["vllm_first_choices_event_v0_26"] = (
        "vllm_first_choices_event_v0_26"
    )
    choices_span_definition: Literal["last_choices_event_span_v1"] = (
        "last_choices_event_span_v1"
    )
    metric_definitions_version: Literal["1.0.0"] = "1.0.0"
    reducer_version: SemanticVersion = "1.0.0"


class SourceEvidence(FrozenModel):
    canonical_response_content: CanonicalResponseContentPolicy = (
        CanonicalResponseContentPolicy.OMIT
    )
    include_request_plan: Literal[True] = True


class SourceLinks(FrozenModel):
    exitspec_contract_digest: Sha256Digest | None = None


class SourceExperimentSpec(FrozenModel):
    schema_version: Literal["inferdrome.source-experiment.v1"]
    experiment: SourceExperimentIdentity
    execution: SourceExecution
    target: SourceTarget
    workload: SourceWorkload
    traffic: SourceTraffic
    measurement: SourceMeasurement = SourceMeasurement()
    evidence: SourceEvidence = SourceEvidence()
    links: SourceLinks = SourceLinks()

    @model_validator(mode="after")
    def execution_and_target_must_match(self) -> "SourceExperimentSpec":
        is_vllm_execution = isinstance(self.execution, SourceVllmExecution)
        is_vllm_target = isinstance(self.target, SourceAttachedVllmTarget)
        if is_vllm_execution != is_vllm_target:
            raise ValueError("execution mode and target engine do not match")
        return self
