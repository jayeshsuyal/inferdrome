"""Independent health, vLLM metrics, and typed unavailable telemetry records."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.contracts import (
    Admissibility,
    EndpointId,
    Signal,
    SignalState,
    TelemetryObservation,
)
from inferdrome.routing_execution.transport import EndpointTransport, TransportError

_METRIC_NAME = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)")
_LABEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
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


def _labels(text: str) -> dict[str, str]:
    """Parse one target sample's Prometheus labels without accepting aliases."""

    labels: dict[str, str] = {}
    index = 0
    while index < len(text):
        name_match = _LABEL_NAME.match(text, index)
        if name_match is None:
            raise TelemetryError("target metrics syntax is malformed")
        name = name_match.group(0)
        index = name_match.end()
        if index >= len(text) or text[index] != "=":
            raise TelemetryError("target metrics syntax is malformed")
        index += 1
        if index >= len(text) or text[index] != '"':
            raise TelemetryError("target metrics syntax is malformed")
        index += 1
        characters: list[str] = []
        while index < len(text):
            character = text[index]
            if character == '"':
                index += 1
                break
            if character == "\\":
                index += 1
                if index >= len(text):
                    raise TelemetryError("target metrics syntax is malformed")
                escaped = text[index]
                if escaped == "n":
                    characters.append("\n")
                elif escaped in {'"', "\\"}:
                    characters.append(escaped)
                else:
                    raise TelemetryError("target metrics syntax is malformed")
                index += 1
                continue
            if ord(character) < 0x20:
                raise TelemetryError("target metrics syntax is malformed")
            characters.append(character)
            index += 1
        else:
            raise TelemetryError("target metrics syntax is malformed")
        if name in labels:
            raise TelemetryError("target metrics labels are ambiguous")
        labels[name] = "".join(characters)
        if index == len(text):
            break
        if text[index] != ",":
            raise TelemetryError("target metrics syntax is malformed")
        index += 1
        if index == len(text):
            raise TelemetryError("target metrics syntax is malformed")
    return labels


def _target_metric_value(
    line: str, *, metric_name: str, expected_model_id: str
) -> int:
    """Parse the one declared vLLM gauge and no other metric family."""

    remainder = line[len(metric_name) :]
    labels: dict[str, str] = {}
    if remainder.startswith("{"):
        index = 1
        quoted = False
        escaped = False
        while index < len(remainder):
            character = remainder[index]
            if escaped:
                escaped = False
            elif character == "\\" and quoted:
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif character == "}" and not quoted:
                labels = _labels(remainder[1:index])
                remainder = remainder[index + 1 :]
                break
            index += 1
        else:
            raise TelemetryError("target metrics syntax is malformed")
    if not remainder or remainder[0] not in " \t":
        raise TelemetryError("target metrics syntax is malformed")
    remainder = remainder.lstrip(" \t")
    value_match = _NUMBER.match(remainder)
    if value_match is None:
        raise TelemetryError("target metric value is malformed")
    raw_value = value_match.group(0)
    trailing = remainder[value_match.end() :]
    if trailing and (trailing[0] not in " \t" or trailing.strip(" \t")):
        raise TelemetryError("target metrics syntax is malformed")
    if labels.get("model_name") != expected_model_id:
        raise TelemetryError("target metric model label is inadmissible")
    try:
        number = Decimal(raw_value)
    except InvalidOperation:
        raise TelemetryError("target metric value is malformed") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise TelemetryError("target metric value is inadmissible")
    try:
        return int(number)
    except (OverflowError, ValueError):
        raise TelemetryError("target metric value is inadmissible") from None


def _metric_values(
    content: bytes, *, metric_name: str, expected_model_id: str
) -> dict[str, int]:
    """Extract exactly one declared model-bound vLLM gauge, never aggregate it."""

    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise TelemetryError("metrics are malformed") from None
    target_values: list[int] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name_match = _METRIC_NAME.match(line)
        if name_match is None or name_match.group(0) != metric_name:
            # Other families may contain repeated histogram buckets. They are
            # intentionally outside the declared load-telemetry contract.
            continue
        target_values.append(
            _target_metric_value(
                line, metric_name=metric_name, expected_model_id=expected_model_id
            )
        )
    if len(target_values) != 1:
        raise TelemetryError("required target metric is missing or ambiguous")
    return {metric_name: target_values[0]}


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
    observed_at_ns: Callable[[], int] | None = None,
) -> TelemetrySample:
    """Sample vLLM health independently; exact HTTP 200 is healthy."""

    sampled_at = now_ns
    try:
        response = transport.get(origin, "/health", timeout_ms=timeout_ms)
        if observed_at_ns is not None:
            sampled_at = observed_at_ns()
        body_hash = sha256_digest(response.body)
        # Pinned vLLM responds with HTTP 200 and an empty body. The body stays
        # bounded by transport and is digested if present, but is not a health
        # protocol dependency.
        healthy = response.status == 200
    except TransportError:
        if observed_at_ns is not None:
            sampled_at = observed_at_ns()
        healthy = False
        body_hash = None
    return TelemetrySample(
        sampled_at_monotonic_ns=sampled_at,
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
    expected_model_id: str,
    observed_at_ns: Callable[[], int] | None = None,
) -> tuple[TelemetrySample, dict[str, int]]:
    """Sample one vLLM metrics response, keeping only its digest and values."""

    sampled_at = now_ns
    try:
        response = transport.get(origin, "/metrics", timeout_ms=timeout_ms)
        if observed_at_ns is not None:
            sampled_at = observed_at_ns()
        body_hash = sha256_digest(response.body)
        if response.status != 200:
            raise TelemetryError("required load metric is unavailable")
        values = _metric_values(
            response.body,
            metric_name=metric_name,
            expected_model_id=expected_model_id,
        )
        return (
            TelemetrySample(
                sampled_at_monotonic_ns=sampled_at,
                epoch=epoch,
                state="AVAILABLE",
                value=values[metric_name],
                source="VLLM_METRICS",
                payload_sha256=body_hash,
            ),
            values,
        )
    except (TelemetryError, TransportError):
        if observed_at_ns is not None:
            sampled_at = observed_at_ns()
        return (
            TelemetrySample(
                sampled_at_monotonic_ns=sampled_at,
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
