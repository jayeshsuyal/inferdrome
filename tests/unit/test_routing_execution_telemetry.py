"""Pinned vLLM health and target-gauge interoperability checks."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.loopback import vllm_0_26_metrics
from inferdrome.routing_execution.telemetry import (
    TelemetrySample,
    sample_health,
    sample_metrics,
)
from inferdrome.routing_execution.transport import TransportResponse

_MODEL_ID = "Qwen/Qwen3-8B"
_METRIC_NAME = "vllm:num_requests_running"


@dataclass
class _TelemetryTransport:
    health: TransportResponse
    metrics: TransportResponse

    def get(self, _origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        del timeout_ms
        if path == "/health":
            return self.health
        if path == "/metrics":
            return self.metrics
        raise AssertionError(path)

    def post_json(
        self, _origin: str, _path: str, _body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        del timeout_ms
        raise AssertionError("telemetry never dispatches")

    def close(self) -> None:
        return None


def _sample_metrics(body: bytes) -> tuple[TelemetrySample, dict[str, int]]:
    transport = _TelemetryTransport(
        TransportResponse(200, b""), TransportResponse(200, body)
    )
    return sample_metrics(
        transport,
        "http://127.0.0.1:18081",
        now_ns=7,
        epoch=1,
        timeout_ms=1000,
        metric_name=_METRIC_NAME,
        expected_model_id=_MODEL_ID,
    )


def test_empty_vllm_health_and_repeated_histogram_buckets_are_admissible(
) -> None:
    transport = _TelemetryTransport(
        TransportResponse(200, b""),
        TransportResponse(200, vllm_0_26_metrics(running=4)),
    )
    health = sample_health(
        transport,
        "http://127.0.0.1:18081",
        now_ns=5,
        epoch=1,
        timeout_ms=1000,
    )
    load, values = sample_metrics(
        transport,
        "http://127.0.0.1:18081",
        now_ns=7,
        epoch=1,
        timeout_ms=1000,
        metric_name=_METRIC_NAME,
        expected_model_id=_MODEL_ID,
    )
    assert health.state == "AVAILABLE"
    assert health.value == "HEALTHY"
    assert health.payload_sha256 == sha256_digest(b"")
    assert load.state == "AVAILABLE"
    assert load.value == 4
    assert values == {_METRIC_NAME: 4}


def test_non_success_health_is_unavailable_but_its_bounded_body_is_hashed() -> None:
    body = b'{"detail":"not ready"}'
    transport = _TelemetryTransport(
        TransportResponse(503, body),
        TransportResponse(200, vllm_0_26_metrics(running=0)),
    )
    health = sample_health(
        transport,
        "http://127.0.0.1:18081",
        now_ns=5,
        epoch=1,
        timeout_ms=1000,
    )
    assert health.state == "UNAVAILABLE"
    assert health.value == "UNAVAILABLE"
    assert health.payload_sha256 == sha256_digest(body)


def test_target_gauge_is_converted_without_float_precision_loss() -> None:
    exact = 9_007_199_254_740_993
    sample, values = _sample_metrics(
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 9007199254740993\n'
    )
    assert sample.state == "AVAILABLE"
    assert sample.value == exact
    assert values == {_METRIC_NAME: exact}


@pytest.mark.parametrize(
    "body",
    (
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-4B"} 1\n',
        b"vllm:num_requests_running 1\n",
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1\n'
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1\n'
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 2\n',
        b'vllm:num_requests_running{model_name=Qwen/Qwen3-8B} 1\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B",} 1\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1 # trailing\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} NaN\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} +Inf\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1.5\n',
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} -1\n',
    ),
)
def test_inadmissible_target_gauges_fail_closed(body: bytes) -> None:
    sample, values = _sample_metrics(body)
    assert sample.state == "UNAVAILABLE"
    assert sample.value == "UNAVAILABLE"
    assert sample.payload_sha256 is None
    assert values == {}
