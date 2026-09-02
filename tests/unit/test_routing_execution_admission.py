"""Topology admission is pure and fail closed before any transport is built."""

from __future__ import annotations

from pathlib import Path

import pytest

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.contracts import (
    RoutingExecutionConfig,
    fixed_selected_workload_sha256,
)
from inferdrome.routing_execution.executor import ExecutionError, run_execution
from inferdrome.routing_execution.topology import TopologyAdmissionError, admit_topology
from tests.routing_execution_support import (
    StaticEndpointTransport,
    config_value,
    write_inputs,
)


def test_normalized_duplicate_loopback_origin_is_rejected() -> None:
    value = config_value(selected_sha256=fixed_selected_workload_sha256())
    endpoints = value["endpoints"]
    assert isinstance(endpoints, list)
    endpoints[1]["origin"] = "http://127.0.0.1:18081/"
    config = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    with pytest.raises(TopologyAdmissionError):
        admit_topology(config)


def test_gcp_one_a100_two_engine_profile_is_rejected() -> None:
    value = config_value(
        origin_a="http://10.1.2.3:8000",
        origin_b="http://10.1.2.4:8000",
        selected_sha256=fixed_selected_workload_sha256(),
    )
    value["mode"] = "GCP_PRIVATE"
    topology = value["topology"]
    assert isinstance(topology, dict)
    topology["accelerator_model"] = "NVIDIA A100-SXM4-40GB"
    topology["accelerator_count"] = 1
    config = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    with pytest.raises(TopologyAdmissionError):
        admit_topology(config)


def test_gcp_none_local_two_engine_profile_is_rejected() -> None:
    value = config_value(
        origin_a="http://10.1.2.3:8000",
        origin_b="http://10.1.2.4:8000",
        selected_sha256=fixed_selected_workload_sha256(),
    )
    value["mode"] = "GCP_PRIVATE"
    topology = value["topology"]
    assert isinstance(topology, dict)
    topology["accelerator_model"] = "NONE_LOCAL"
    topology["accelerator_count"] = 2
    config = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    with pytest.raises(TopologyAdmissionError):
        admit_topology(config)


def test_gcp_internal_name_is_rejected_pending_connection_pinning() -> None:
    value = config_value(
        origin_a="http://endpoint-a.internal:8000",
        origin_b="http://10.1.2.4:8000",
        selected_sha256=fixed_selected_workload_sha256(),
    )
    value["mode"] = "GCP_PRIVATE"
    topology = value["topology"]
    assert isinstance(topology, dict)
    topology["accelerator_model"] = "NVIDIA A100-SXM4-40GB"
    topology["accelerator_count"] = 2
    config = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    with pytest.raises(TopologyAdmissionError):
        admit_topology(config)


def test_public_gcp_target_is_rejected() -> None:
    value = config_value(
        origin_a="https://198.51.100.3:8443",
        origin_b="https://10.1.2.4:8443",
        selected_sha256=fixed_selected_workload_sha256(),
    )
    value["mode"] = "GCP_PRIVATE"
    topology = value["topology"]
    assert isinstance(topology, dict)
    topology["accelerator_model"] = "NVIDIA A100-SXM4-40GB"
    topology["accelerator_count"] = 2
    config = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    with pytest.raises(TopologyAdmissionError):
        admit_topology(config)


def test_invalid_topology_never_constructs_a_transport(
    tmp_path: Path,
) -> None:
    root = tmp_path
    config_path, workload_path, config, _ = write_inputs(root)
    endpoints = config["endpoints"]
    assert isinstance(endpoints, list)
    endpoints[1]["origin"] = endpoints[0]["origin"]
    config_path.write_bytes(canonical_json_bytes(config))
    calls = 0

    def factory() -> StaticEndpointTransport:
        nonlocal calls
        calls += 1
        return StaticEndpointTransport()

    with pytest.raises(ExecutionError):
        run_execution(
            config_path,
            workload_path,
            root / "package",
            transport_factory=factory,
        )
    assert calls == 0
