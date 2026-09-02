"""Small deterministic fixtures for the additive routing-execution tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from inferdrome.routing_execution.cli import _demo_config
from inferdrome.routing_execution.contracts import fixed_selected_workload_bytes
from inferdrome.routing_execution.transport import (
    TransportCancelled,
    TransportResponse,
    TransportTimedOut,
)


def workload_bytes() -> bytes:
    return fixed_selected_workload_bytes()


def config_value(
    *,
    origin_a: str = "http://127.0.0.1:18081",
    origin_b: str = "http://127.0.0.1:18082",
    selected_sha256: str,
) -> dict[str, object]:
    return _demo_config(
        origin_a=origin_a,
        origin_b=origin_b,
        selected_sha256=selected_sha256,
        source_commit="0" * 40,
    )


def write_inputs(
    root: Path,
    *,
    config: dict[str, object] | None = None,
) -> tuple[Path, Path, dict[str, object], bytes]:
    workload = workload_bytes()
    selected = sha256_digest(workload)
    selected_config = config or config_value(selected_sha256=selected)
    config_path = root / "deployment-config.json"
    workload_path = root / "workload.jsonl"
    config_path.write_bytes(canonical_json_bytes(selected_config))
    workload_path.write_bytes(workload)
    return config_path, workload_path, selected_config, workload


@dataclass
class StaticEndpointTransport:
    """A socket-free, deterministic transport used for unit-level bridge tests."""

    mode: str = "normal"
    calls: list[tuple[str, str]] = field(default_factory=list)
    request_bodies: list[bytes] = field(default_factory=list)
    closed: bool = False

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        del timeout_ms
        self.calls.append((origin, path))
        if path == "/v1/models":
            return TransportResponse(200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}')
        if path == "/health":
            return TransportResponse(200, b'{"status":"ok"}')
        if path == "/metrics":
            if self.mode == "malformed_metrics":
                return TransportResponse(200, b"vllm:num_requests_running NaN\n")
            load = 4 if origin.endswith(":18081") else 1
            return TransportResponse(
                200, f"vllm:num_requests_running {load}\n".encode()
            )
        raise AssertionError(path)

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        del origin, timeout_ms
        assert path == "/v1/chat/completions"
        self.calls.append(("dispatch", path))
        self.request_bodies.append(body)
        if self.mode == "timeout":
            raise TransportTimedOut("test timeout")
        if self.mode == "cancelled":
            raise TransportCancelled("test cancellation")
        if self.mode == "malformed_completion":
            return TransportResponse(200, b"not-json")
        return TransportResponse(200, b'{"choices":[]}')

    def close(self) -> None:
        self.closed = True
