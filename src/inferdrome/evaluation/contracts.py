"""Closed, bounded inputs for inference-evaluation-v1; raw values are input only."""

from __future__ import annotations

import ipaddress
import json
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from inferdrome.parsing import bounded_json_float, validate_json_structure

EndpointId = Literal["endpoint-a", "endpoint-b"]
Outcome = Literal[
    "SUCCESS",
    "REJECTED_CAPACITY",
    "REJECTED_ROUTE",
    "TIMEOUT",
    "HTTP_ERROR",
    "STREAM_ERROR",
    "STREAM_LIMIT",
    "INCOMPLETE_STREAM",
    "TRANSPORT_ERROR",
    "CANCELLED",
    "DRAIN_TIMEOUT",
    "INTERNAL_ERROR",
]
MAX_INPUT_BYTES = 16 * 1024 * 1024


class EvaluationError(ValueError):
    """Sanitized input, execution, or publication failure."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Bounds(ClosedModel):
    max_requests: Annotated[int, Field(ge=1, le=10_000)] = 1000
    concurrency: Annotated[int, Field(ge=1, le=64)] = 8
    max_queue: Annotated[int, Field(ge=0, le=1024)] = 64
    duration_ns: Annotated[int, Field(ge=1, le=300_000_000_000)] = 10_000_000_000
    request_timeout_ns: Annotated[int, Field(ge=1, le=60_000_000_000)] = 10_000_000_000
    drain_ns: Annotated[int, Field(ge=1, le=60_000_000_000)] = 10_000_000_000
    cleanup_timeout_ns: Annotated[int, Field(ge=1, le=5_000_000_000)] = 2_000_000_000
    max_stream_bytes: Annotated[int, Field(ge=1, le=1_048_576)] = 1_048_576
    max_event_bytes: Annotated[int, Field(ge=1, le=65_536)] = 65_536
    max_content_events: Annotated[int, Field(ge=1, le=4096)] = 1000

    @model_validator(mode="after")
    def aggregate_bounds(self) -> Self:
        if self.max_requests * self.max_content_events > 1_000_000:
            raise ValueError("aggregate timing storage exceeds its bound")
        if self.max_event_bytes > self.max_stream_bytes:
            raise ValueError("event bound exceeds stream bound")
        return self


class Endpoint(ClosedModel):
    endpoint_id: EndpointId
    origin: Annotated[str, Field(max_length=80, repr=False)]

    @field_validator("origin")
    @classmethod
    def admitted_origin(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = ipaddress.IPv4Address(parsed.hostname or "")
            port = parsed.port
            allowed = host == ipaddress.IPv4Address("127.0.0.1") or any(
                host in ipaddress.IPv4Network(network)
                for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
            if (
                not allowed
                or not port
                or parsed.scheme != "http"
                or value != f"http://{host}:{port}"
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "endpoint must be a canonical private IPv4 origin"
            ) from None
        return value


class Offer(ClosedModel):
    scheduled_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]
    endpoint_id: EndpointId
    prompt: Annotated[str, Field(min_length=1, max_length=32_768, repr=False)]

    @field_validator("prompt")
    @classmethod
    def bounded_prompt(cls, value: str) -> str:
        try:
            valid = len(value.encode("utf-8")) <= 32_768
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError("prompt violates its byte bound")
        return value


class EvaluationConfig(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-config.v1"]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    model: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$", repr=False)
    ]
    max_tokens: Annotated[int, Field(ge=1, le=4096)] = 128
    temperature: Literal[0] = 0
    # Explicit Qwen chat-template policy; no reasoning-token timing is claimed.
    enable_thinking: Literal[False] = False
    endpoints: Annotated[tuple[Endpoint, ...], Field(min_length=2, max_length=2)]
    bounds: Bounds
    offers: Annotated[tuple[Offer, ...], Field(min_length=1, max_length=10_000)]

    @field_validator("temperature", "enable_thinking", mode="before")
    @classmethod
    def strict_fixed_settings(cls, value: object, info: ValidationInfo) -> object:
        # Literal equality alone admits False == 0 in Pydantic. These wire
        # settings still require their declared JSON primitive types.
        expected = int if info.field_name == "temperature" else bool
        if type(value) is not expected:
            raise ValueError("fixed request setting has the wrong primitive type")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        if (
            tuple(e.endpoint_id for e in self.endpoints) != ("endpoint-a", "endpoint-b")
            or self.endpoints[0].origin == self.endpoints[1].origin
        ):
            raise ValueError("exactly two ordered distinct endpoints required")
        if len(self.offers) > self.bounds.max_requests:
            raise ValueError("offer count exceeds its bound")
        previous = -1
        total_bytes = 0
        for offer in self.offers:
            if not previous <= offer.scheduled_ns < self.bounds.duration_ns:
                raise ValueError("arrival schedule is unordered or outside duration")
            previous = offer.scheduled_ns
            total_bytes += len(offer.prompt.encode("utf-8"))
        if total_bytes > 8 * 1024 * 1024:
            raise ValueError("aggregate prompt storage exceeds its bound")
        return self


def load_config_bytes(content: bytes) -> EvaluationConfig:
    """Reject duplicate/non-finite/structurally excessive JSON before validation."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite value")

    try:
        if not 1 <= len(content) <= MAX_INPUT_BYTES:
            raise ValueError
        text = content.decode("utf-8")
        validate_json_structure(text)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=bounded_json_float,
        )
        return EvaluationConfig.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError(
            "evaluation configuration violates its contract"
        ) from None
