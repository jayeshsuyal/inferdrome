"""Independent health, vLLM metrics, and typed unavailable telemetry records."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.contracts import (
    Admissibility,
    EndpointId,
    Signal,
    SignalState,
    TelemetryObservation,
)
from inferdrome.routing_execution.transport import EndpointTransport, TransportError

_METRIC_LINE = re.compile(
    r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^{}]*\})?[ \t]+"
    r"([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?:[ \t].*)?$"
)


class TelemetryError(ValueError):
    """A health or metric response cannot produce an admissible observation."""


@dataclass(frozen=True)
class TelemetrySample:
    """A pre-decision raw sample stripped of endpoint and payload contents."""

    sampled_at_monotonic_ns: int
    epoch: int
    state: SignalState
    value: int | str
    source: str
    payload_sha256: str | None


def _strict_json(content: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise TelemetryError("telemetry response is malformed")
            result[key] = value
        return result

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise TelemetryError("telemetry response is malformed") from None


def _metric_values(content: bytes) -> dict[str, int]:
    """Parse a deliberately tiny, unambiguous Prometheus metric subset."""

    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise TelemetryError("metrics are malformed") from None
    values: dict[str, int] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _METRIC_LINE.fullmatch(line)
        if matched is None:
            raise TelemetryError("metrics are malformed")
        name, raw_value = matched.groups()
        try:
            number = float(raw_value)
        except ValueError:
            raise TelemetryError("metrics are malformed") from None
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            raise TelemetryError("metrics are inadmissible")
        if name in values:
            raise TelemetryError("metrics are ambiguous")
        values[name] = int(number)
    return values


def unavailable_sample(*, now_ns: int) -> TelemetrySample:
    """Represent a declared unavailable capability without fabricating a value."""

    return TelemetrySample(
        sampled_at_monotonic_ns=now_ns,
        epoch=0,
        state="UNAVAILABLE",
        value="UNAVAILABLE",
        source="UNAVAILABLE_CAPABILITY",
        payload_sha256=None,
    )


def sample_health(
    transport: EndpointTransport,
    origin: str,
    *,
    now_ns: int,
    epoch: int,
    timeout_ms: int,
) -> TelemetrySample:
    """Sample health independently from load; malformed/non-2xx is unavailable."""

    try:
        response = transport.get(origin, "/health", timeout_ms=timeout_ms)
        body_hash = sha256_digest(response.body)
        value = _strict_json(response.body)
        healthy = (
            response.status == 200
            and isinstance(value, dict)
            and value.get("status") == "ok"
        )
    except (TelemetryError, TransportError):
        healthy = False
        body_hash = None
    return TelemetrySample(
        sampled_at_monotonic_ns=now_ns,
        epoch=epoch,
        state="AVAILABLE" if healthy else "UNAVAILABLE",
        value="HEALTHY" if healthy else "UNAVAILABLE",
        source="HTTP_HEALTH",
        payload_sha256=body_hash,
    )


def sample_metrics(
    transport: EndpointTransport,
    origin: str,
    *,
    now_ns: int,
    epoch: int,
    timeout_ms: int,
    metric_name: str,
) -> tuple[TelemetrySample, dict[str, int]]:
    """Sample one vLLM metrics response, keeping only its digest and values."""

    try:
        response = transport.get(origin, "/metrics", timeout_ms=timeout_ms)
        body_hash = sha256_digest(response.body)
        values = _metric_values(response.body)
        if response.status != 200 or metric_name not in values:
            raise TelemetryError("required load metric is unavailable")
        return (
            TelemetrySample(
                sampled_at_monotonic_ns=now_ns,
                epoch=epoch,
                state="AVAILABLE",
                value=values[metric_name],
                source="VLLM_METRICS",
                payload_sha256=body_hash,
            ),
            values,
        )
    except (TelemetryError, TransportError):
        return (
            TelemetrySample(
                sampled_at_monotonic_ns=now_ns,
                epoch=epoch,
                state="UNAVAILABLE",
                value="UNAVAILABLE",
                source="VLLM_METRICS",
                payload_sha256=None,
            ),
            {},
        )


def observation(
    *,
    trial_id: str,
    request_id: str,
    sequence_index: int,
    endpoint_id: EndpointId,
    signal: Signal,
    observer_id: str,
    sample: TelemetrySample,
    decision_ns: int,
    freshness_bound_ns: int,
) -> TelemetryObservation:
    """Turn one independent sample into an age-bound decision receipt row."""

    age = max(0, decision_ns - sample.sampled_at_monotonic_ns)
    state: SignalState = sample.state
    if state == "AVAILABLE" and age > freshness_bound_ns:
        state = "STALE"
    admissibility: Admissibility = (
        "ADMISSIBLE" if state == "AVAILABLE" else "INADMISSIBLE"
    )
    return TelemetryObservation(
        schema_version="inferdrome.routing-execution-telemetry-observation.v1",
        trial_id=trial_id,
        request_id=request_id,
        sequence_index=sequence_index,
        endpoint_id=endpoint_id,
        signal=signal,
        observer_id=observer_id,
        epoch=sample.epoch,
        sampled_at_monotonic_ns=sample.sampled_at_monotonic_ns,
        decision_at_monotonic_ns=decision_ns,
        age_ns=age,
        freshness_bound_ns=freshness_bound_ns,
        state=state,
        admissibility=admissibility,
        value=sample.value,  # type: ignore[arg-type]
        source=sample.source,  # type: ignore[arg-type]
        payload_sha256=sample.payload_sha256,
    )
