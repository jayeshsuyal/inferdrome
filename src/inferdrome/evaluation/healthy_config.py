"""Closed bounds for routing with live observations and no injected fault."""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import ValidationError, model_validator

from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    ClosedModel,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.fault_config import TelemetryBounds
from inferdrome.evaluation.policies import PolicyId
from inferdrome.parsing import bounded_json_float, validate_json_structure


class HealthyRoutingConfig(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-healthy-config.v1"]
    foreground: EvaluationConfig
    policy_id: PolicyId
    telemetry: TelemetryBounds

    @model_validator(mode="after")
    def bounded_observed_population(self) -> Self:
        bounds = self.foreground.bounds
        if self.telemetry.warmup_ns >= bounds.duration_ns or any(
            offer.scheduled_ns < self.telemetry.warmup_ns
            for offer in self.foreground.offers
        ):
            raise ValueError("healthy foreground precedes telemetry warmup")
        if (
            bounds.max_requests > 4000
            or bounds.max_requests * bounds.max_content_events > 500_000
        ):
            raise ValueError("healthy population resources exceed their bounds")
        end = bounds.duration_ns + bounds.drain_ns
        interval = self.telemetry.interval_ns
        if 6 * ((end + interval - 1) // interval) > self.telemetry.max_observations:
            raise ValueError("healthy telemetry schedule exceeds its sample bound")
        return self


def load_healthy_config_bytes(content: bytes) -> HealthyRoutingConfig:
    """Reject ambiguous or excessive JSON before strict closed-model validation."""

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
        return HealthyRoutingConfig.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError(
            "healthy routing configuration violates its contract"
        ) from None
