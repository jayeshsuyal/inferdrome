"""Frozen, ordered request-plan contract."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.experiment import TrafficConfig
from inferdrome.domain.ids import (
    DecimalString,
    ExperimentId,
    ProducerRequestId,
    ProducerRequestIdPrefix,
    RequestId,
    RunId,
    Sha256Digest,
    request_id_from_index,
    sha256_digest,
)
from inferdrome.domain.states import Replayability


class InlinePrompt(FrozenModel):
    kind: Literal["inline"]
    text: Annotated[str, Field(min_length=1, max_length=1_048_576)]
    sha256: Sha256Digest

    @model_validator(mode="after")
    def digest_must_match_text(self) -> "InlinePrompt":
        if self.sha256 != sha256_digest(self.text.encode("utf-8")):
            raise ValueError("inline prompt digest does not match UTF-8 bytes")
        return self


class DigestOnlyPrompt(FrozenModel):
    kind: Literal["digest_only"]
    sha256: Sha256Digest


PromptMaterial = Annotated[
    InlinePrompt | DigestOnlyPrompt,
    Field(discriminator="kind"),
]


class SamplingConfig(FrozenModel):
    requested_output_tokens: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    temperature: DecimalString
    seed: Annotated[int, Field(strict=True, ge=0, le=4_294_967_295)]


class PlannedRequest(FrozenModel):
    sequence_index: Annotated[int, Field(strict=True, ge=0, le=99_999_999)]
    request_id: RequestId
    producer_request_id: ProducerRequestId
    prompt: PromptMaterial
    sampling: SamplingConfig


class RequestPlan(FrozenModel):
    schema_version: Literal["inferdrome.request-plan.v1"]
    run_id: RunId
    experiment_id: ExperimentId
    source_spec_digest: Sha256Digest
    ordering: Literal["sequence_index_ascending_v1"]
    request_id_derivation: Literal["sequence_index_decimal_v1"]
    producer_request_id_derivation: Literal["prefix_plus_sequence_index_v1"]
    producer_request_id_prefix: ProducerRequestIdPrefix
    replayability: Replayability
    traffic: TrafficConfig
    requests: Annotated[tuple[PlannedRequest, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_order_and_identity(self) -> "RequestPlan":
        if len(self.requests) != self.traffic.measured_requests:
            raise ValueError("request-plan cardinality must match measured requests")

        for expected_index, request in enumerate(self.requests):
            if request.sequence_index != expected_index:
                raise ValueError(
                    "request sequence indexes must be contiguous and ordered"
                )
            if request.request_id != request_id_from_index(expected_index):
                raise ValueError("canonical request ID does not match sequence index")
            expected_producer_id = f"{self.producer_request_id_prefix}{expected_index}"
            if request.producer_request_id != expected_producer_id:
                raise ValueError("producer request ID does not match prefix derivation")

        has_digest_only_prompt = any(
            isinstance(request.prompt, DigestOnlyPrompt) for request in self.requests
        )
        expected_replayability = (
            Replayability.LIMITED if has_digest_only_prompt else Replayability.FULL
        )
        if self.replayability is not expected_replayability:
            raise ValueError(
                "replayability does not match request-plan prompt material"
            )
        return self
