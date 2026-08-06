"""Canonical measured-request record grounded in vLLM 0.26.0 capabilities."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import (
    ProducerRequestId,
    RelativeArtifactPath,
    RequestId,
    RunId,
    SemanticVersion,
    Sha256Digest,
    request_id_from_index,
    sha256_digest,
)


class RequestStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ANOMALOUS_EMPTY_STREAM = "ANOMALOUS_EMPTY_STREAM"


class NativeSourceLocator(FrozenModel):
    artifact_path: RelativeArtifactPath
    array_index: Annotated[int, Field(strict=True, ge=0, le=99_999_999)]


class VllmProducerSemantics(FrozenModel):
    producer_name: Literal["vllm"]
    producer_version: Literal["0.26.0"]
    adapter_name: Literal["vllm_bench_serve"]
    adapter_version: SemanticVersion
    native_schema_fingerprint: Sha256Digest
    request_id_derivation: Literal["frozen_order_and_prefix_v1"]


class FakeProducerSemantics(FrozenModel):
    producer_name: Literal["inferdrome_fake"]
    producer_version: SemanticVersion
    adapter_name: Literal["fake"]
    adapter_version: SemanticVersion
    native_schema_fingerprint: Sha256Digest
    request_id_derivation: Literal["frozen_order_and_prefix_v1"]


ProducerSemantics = Annotated[
    VllmProducerSemantics | FakeProducerSemantics,
    Field(discriminator="producer_name"),
]


class TokenObservations(FrozenModel):
    input_tokens: Annotated[int, Field(strict=True, ge=0)]
    output_tokens: Annotated[int, Field(strict=True, ge=0)]
    input_tokens_semantics: Literal["vllm_output_prompt_len_v0_26"]
    output_tokens_semantics: Literal["vllm_output_len_v0_26"]


class TimingObservations(FrozenModel):
    clock_domain: Literal["producer_monotonic_normalized_v1"]
    start_offset_ns: Annotated[int, Field(strict=True, ge=0)]
    ttft_ns: Annotated[int, Field(strict=True, ge=0)] | None
    ttft_definition: Literal["vllm_first_choices_event_v0_26"]
    itl_ns: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    itl_definition: Literal["vllm_subsequent_choices_event_interval_v0_26"]

    @model_validator(mode="after")
    def itls_require_a_first_event(self) -> "TimingObservations":
        if self.ttft_ns is None and self.itl_ns:
            raise ValueError("ITL observations require an observed first choices event")
        return self


class RequestOutcome(FrozenModel):
    status: RequestStatus
    producer_error: Annotated[str, Field(min_length=1, max_length=65_536)] | None

    @model_validator(mode="after")
    def error_must_match_status(self) -> "RequestOutcome":
        if self.status is RequestStatus.FAILED and self.producer_error is None:
            raise ValueError("failed request requires producer error text")
        if self.status is not RequestStatus.FAILED and self.producer_error is not None:
            raise ValueError("non-failed request cannot carry producer error text")
        return self


class RequestContentEvidence(FrozenModel):
    prompt_sha256: Sha256Digest
    response_sha256: Sha256Digest | None
    canonical_response_content: Annotated[str, Field(max_length=4_194_304)] | None
    native_response_content_present: Literal[True]

    @model_validator(mode="after")
    def included_response_must_match_digest(self) -> "RequestContentEvidence":
        if self.canonical_response_content is not None:
            actual_digest = sha256_digest(
                self.canonical_response_content.encode("utf-8")
            )
            if self.response_sha256 != actual_digest:
                raise ValueError("canonical response content digest mismatch")
        return self


class RequestRecord(FrozenModel):
    schema_version: Literal["inferdrome.request-record.v1"]
    run_id: RunId
    request_id: RequestId
    producer_request_id: ProducerRequestId
    sequence_index: Annotated[int, Field(strict=True, ge=0, le=99_999_999)]
    native_source: NativeSourceLocator
    producer: ProducerSemantics
    tokens: TokenObservations
    timing: TimingObservations
    outcome: RequestOutcome
    content: RequestContentEvidence

    @model_validator(mode="after")
    def validate_cross_field_invariants(self) -> "RequestRecord":
        if self.request_id != request_id_from_index(self.sequence_index):
            raise ValueError("request ID does not match sequence index")
        if self.native_source.array_index != self.sequence_index:
            raise ValueError("native array index does not match sequence index")

        if self.outcome.status is RequestStatus.SUCCESS:
            if self.timing.ttft_ns is None:
                raise ValueError("successful request requires an observed TTFT")
            if self.content.response_sha256 is None:
                raise ValueError("successful request requires a response digest")
        elif self.outcome.status is RequestStatus.ANOMALOUS_EMPTY_STREAM:
            if self.timing.ttft_ns is not None or self.timing.itl_ns:
                raise ValueError(
                    "empty-stream anomaly cannot have choices-event timings"
                )
            if self.tokens.output_tokens != 0:
                raise ValueError("empty-stream anomaly must have zero output tokens")
            expected_empty_digest = sha256_digest(b"")
            if self.content.response_sha256 != expected_empty_digest:
                raise ValueError("empty-stream anomaly must digest the empty response")
        return self
