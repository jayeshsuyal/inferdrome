"""Closed bounds for one local routing/fault trial, independent of campaign v1."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    ClosedModel,
    EndpointId,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.policies import PolicyId
from inferdrome.parsing import bounded_json_float, validate_json_structure


class TelemetryBounds(ClosedModel):
    interval_ns: Annotated[int, Field(ge=1_000_000, le=10_000_000_000)] = 100_000_000
    poll_timeout_ns: Annotated[int, Field(ge=1_000_000, le=5_000_000_000)] = 100_000_000
    health_freshness_ns: Annotated[int, Field(ge=1, le=60_000_000_000)] = 500_000_000
    load_freshness_ns: Annotated[int, Field(ge=1, le=60_000_000_000)] = 500_000_000
    warmup_ns: Annotated[int, Field(ge=1_000_000, le=30_000_000_000)] = 500_000_000
    max_observations: Annotated[int, Field(ge=6, le=10_000)] = 10_000
    max_response_bytes: Annotated[int, Field(ge=1, le=1_048_576)] = 1_048_576
    cleanup_timeout_ns: Annotated[int, Field(ge=1_000_000, le=5_000_000_000)] = (
        2_000_000_000
    )


class FaultTiming(ClosedModel):
    target_endpoint_id: EndpointId
    freeze_start_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    restore_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]
    background_stop_ns: Annotated[int, Field(ge=1, le=300_000_000_000)]


class RoutingFaultConfig(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-routing-config.v1"]
    foreground: EvaluationConfig
    background: EvaluationConfig
    policy_id: PolicyId
    telemetry: TelemetryBounds
    fault: FaultTiming

    @model_validator(mode="after")
    def compatible_populations(self) -> Self:
        foreground, background = self.foreground, self.background
        if (
            foreground.endpoints != background.endpoints
            or foreground.model != background.model
            or foreground.source_commit != background.source_commit
            or foreground.bounds.duration_ns != background.bounds.duration_ns
            or foreground.bounds.drain_ns != background.bounds.drain_ns
        ):
            raise ValueError(
                "populations must share endpoints, model, source and clock"
            )
        timing = self.fault
        if not (
            self.telemetry.warmup_ns
            < timing.freeze_start_ns
            < timing.restore_ns
            < timing.background_stop_ns
            <= foreground.bounds.duration_ns
        ):
            raise ValueError("fault phases are unordered or outside duration")
        if any(
            offer.scheduled_ns < self.telemetry.warmup_ns for offer in foreground.offers
        ):
            raise ValueError("foreground offers precede warmup")
        if any(
            offer.endpoint_id != timing.target_endpoint_id
            or not timing.freeze_start_ns
            <= offer.scheduled_ns
            < timing.background_stop_ns
            for offer in background.offers
        ):
            raise ValueError("background offers violate the selected fault phase")
        bounds = (foreground.bounds, background.bounds)
        if (
            sum(bound.concurrency for bound in bounds) > 64
            or sum(bound.max_queue for bound in bounds) > 1024
            or sum(bound.max_requests for bound in bounds) > 4000
            or sum(bound.max_requests * bound.max_content_events for bound in bounds)
            > 500_000
        ):
            raise ValueError("combined population resources exceed their bounds")
        if (
            sum(
                len(offer.prompt.encode("utf-8"))
                for config in (foreground, background)
                for offer in config.offers
            )
            > 8 * 1024 * 1024
        ):
            raise ValueError("combined prompts exceed their storage bound")
        end = foreground.bounds.duration_ns + foreground.bounds.drain_ns
        interval = self.telemetry.interval_ns
        # Six sequential channels; each starts at most once in an interval.
        if 6 * ((end + interval - 1) // interval) > self.telemetry.max_observations:
            raise ValueError("telemetry schedule exceeds its sample bound")
        return self


def load_routing_config_bytes(content: bytes) -> RoutingFaultConfig:
    """Reject ambiguous/oversized JSON before strict closed-model validation."""

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
        return RoutingFaultConfig.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError("routing configuration violates its contract") from None
