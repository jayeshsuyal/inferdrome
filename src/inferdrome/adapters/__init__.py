"""Benchmark producer adapters."""

from inferdrome.adapters.fake import FakeAdapter, FakeObservation, FakeRunResult
from inferdrome.adapters.vllm_bench import (
    EndpointPreflightCapture,
    VllmBenchmarkCapture,
    VllmInvocation,
    VllmInvocationPaths,
    VllmVersionProbeCapture,
    build_vllm_invocation,
    execute_vllm_benchmark,
    preflight_attached_endpoint,
    probe_vllm_version,
)

__all__ = [
    "EndpointPreflightCapture",
    "FakeAdapter",
    "FakeObservation",
    "FakeRunResult",
    "VllmBenchmarkCapture",
    "VllmInvocation",
    "VllmInvocationPaths",
    "VllmVersionProbeCapture",
    "build_vllm_invocation",
    "execute_vllm_benchmark",
    "preflight_attached_endpoint",
    "probe_vllm_version",
]
