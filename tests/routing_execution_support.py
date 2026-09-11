"""Small deterministic fixtures for the additive routing-execution tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from inferdrome.routing_execution.cli import _demo_config
from inferdrome.routing_execution.contracts import fixed_selected_workload_bytes
from inferdrome.routing_execution.loopback import vllm_0_26_metrics
from inferdrome.routing_execution.transport import (
    TransportCancelled,
    TransportResponse,
    TransportTimedOut,
)


def manual_host_fixture_input(*, source_commit: str) -> dict[str, Any]:
    """Return a local-only declaration that can produce a v2 test package.

    These values are deterministic test declarations. They are intentionally
    not a provider plan or a host observation, and callers must use an injected
    transport rather than Docker or a network endpoint.
    """

    from inferdrome.deployment.manual_host import input_template

    value = input_template()
    value.update(
        {
            "source_commit": source_commit,
            "instance_id": "0123456789abcdef0123456789abcdef",
            "region": "synthetic-region",
            "instance_type": "gpu_2x_a100",
            "gpu_uuids": [
                "GPU-00000000-0000-0000-0000-000000000001",
                "GPU-00000000-0000-0000-0000-000000000002",
            ],
            "uid": 2000,
            "gid": 2000,
            "runner_image": {
                "reference": "example.invalid/test-runner@"
                + sha256_digest(b"routing-execution-dashboard-runner")
            },
            "serving_image": {
                "reference": "example.invalid/test-engine@"
                + sha256_digest(b"routing-execution-dashboard-engine")
            },
            "model_path": "/srv/test-model",
            "preparation_path": "/srv/test-inputs",
            "evidence_path": "/srv/test-evidence",
            "compose_project": "synthetic-campaign",
            "container_subnet": "172.29.71.0/24",
            "endpoint_ipv4": ["172.29.71.2", "172.29.71.3"],
            "request_timeout_ms": 1000,
        }
    )
    cleanup = value["cleanup"]
    assert isinstance(cleanup, dict)
    cleanup.update(
        {
            "instance_id": value["instance_id"],
            "accountable_operator": "synthetic-operator",
            "terminate_by_utc": "2030-01-01T00:00:00Z",
        }
    )
    return value


def h100_manual_host_fixture_input(*, source_commit: str) -> dict[str, Any]:
    """Return a synthetic v2 H100 declaration, never a host observation."""

    from inferdrome.deployment.manual_host import H100_PROFILE_ID, input_template

    value = input_template(H100_PROFILE_ID)
    value.update(
        {
            "source_commit": source_commit,
            "instance_id": "0123456789abcdef0123456789abcdef",
            "region": "synthetic-region",
            "instance_type": "gpu_2x_h100_sxm5",
            "gpu_uuids": [
                "GPU-00000000-0000-0000-0000-000000000001",
                "GPU-00000000-0000-0000-0000-000000000002",
            ],
            "uid": 2000,
            "gid": 2000,
            "runner_image": {
                "reference": "example.invalid/test-runner@"
                + sha256_digest(b"routing-execution-dashboard-h100-runner")
            },
            "serving_image": {
                "reference": "example.invalid/test-engine@"
                + sha256_digest(b"routing-execution-dashboard-h100-engine")
            },
            "model_path": "/srv/test-model",
            "preparation_path": "/srv/test-inputs",
            "evidence_path": "/srv/test-evidence",
            "compose_project": "synthetic-campaign",
            "container_subnet": "172.29.71.0/24",
            "endpoint_ipv4": ["172.29.71.2", "172.29.71.3"],
            "request_timeout_ms": 1000,
        }
    )
    cleanup = value["cleanup"]
    assert isinstance(cleanup, dict)
    cleanup.update(
        {
            "instance_id": value["instance_id"],
            "accountable_operator": "synthetic-operator",
            "terminate_by_utc": "2030-01-01T00:00:00Z",
        }
    )
    return value


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
            if self.mode == "unavailable_health":
                return TransportResponse(503, b"")
            return TransportResponse(200, b"")
        if path == "/metrics":
            if self.mode == "malformed_metrics":
                return TransportResponse(
                    200,
                    b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} NaN\n',
                )
            if self.mode == "ambiguous_metrics":
                return TransportResponse(
                    200,
                    b"\n".join(
                        (
                            b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1',
                            b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 2',
                        )
                    )
                    + b"\n",
                )
            if self.mode == "wrong_model_metrics":
                return TransportResponse(
                    200,
                    vllm_0_26_metrics(running=1, model_name="Qwen/Qwen3-4B"),
                )
            load = 4 if origin.endswith(":18081") else 1
            return TransportResponse(200, vllm_0_26_metrics(running=load))
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
        if self.mode == "empty_choices":
            return TransportResponse(200, b'{"choices":[]}')
        if self.mode == "empty_choice_content":
            return TransportResponse(
                200,
                b'{"choices":[{"index":0,"message":{"role":"assistant","content":""},"finish_reason":"stop"}]}',
            )
        return TransportResponse(
            200,
            b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}',
        )

    def close(self) -> None:
        self.closed = True
