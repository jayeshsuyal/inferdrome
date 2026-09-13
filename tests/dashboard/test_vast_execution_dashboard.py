"""Actual JSON API projection of sealed, socket-free synthetic Vast evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inferdrome.routing_execution.verifier import verify_execution_package
from tests.dashboard.test_routing_execution_dashboard import (
    _client,
    _make_tree_immutable,
    _mutable_copy,
)
from tests.unit.test_vast_execution import seal_vast_fixture, vast_config_value


@pytest.mark.parametrize("mode", ["success", "timeout"])
def test_vast_v4_json_index_and_detail_preserve_process_provenance(
    tmp_path: Path, mode: str
) -> None:
    package = seal_vast_fixture(tmp_path / "synthetic-vast", mode=mode)
    verified = verify_execution_package(package)
    digest = verified.report.retained_digest
    with _client(package, digest) as client:
        index = client.get("/api/v1/routing-executions")
        detail = client.get("/api/v1/routing-executions/routing-execution-v1")

    assert index.status_code == detail.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert index.json()["rejected"] == []
    assert index.json()["page"]["returned"] == 1
    summary = index.json()["routing_executions"][0]
    payload = detail.json()
    assert payload["summary"] == summary
    assert summary["mode"] == "VAST_MANUAL_CONTAINER"
    assert summary["retained_digest"] == digest
    assert summary["topology"] == {
        "profile_id": "vast-container-two-h100-sxm5-80gb-v1",
        "accelerator_model": "NVIDIA H100-SXM5-80GB",
        "accelerator_count": 2,
        "container_count": 1,
        "serving_engine_count": 2,
        "one_engine_per_endpoint": True,
        "tensor_parallel_size": 1,
        "declared_provider": "VAST_AI",
        "declared_provisioning": "OPERATOR_SUPPLIED_CONTAINER",
        "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
        "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
        "isolation_boundary": "SEPARATE_PROCESSES_SHARED_CONTAINER",
        "observer_gpu_isolation": "ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED",
    }
    declaration = vast_config_value()
    evidence = payload["evidence"]
    assert evidence["container_image"] == declaration["container_image"]["reference"]
    assert evidence["artifact_provenance"] == declaration["artifact_provenance"]
    assert "runner_image" not in evidence
    assert "serving_image" not in evidence
    assert "runner_separate_from_serving" not in summary["topology"]
    assert evidence["endpoints"] == [
        {"endpoint_id": "endpoint-a"}, {"endpoint_id": "endpoint-b"}
    ]
    assert evidence["input_transfer"]["verified_before_transport"] is True
    assert payload["interpretation_boundary"] == "MEASUREMENT_EVIDENCE_ONLY"
    assert len(payload["trials"]) == 3
    for trial in payload["trials"]:
        assert len(trial["requests"]) == 6
        assert trial["terminal_population_total"] == 6
        assert sum(row["count"] for row in trial["terminal_population"]) == 6
        for request in trial["requests"]:
            assert request["terminal"]["decision_id"] == request["decision_id"]
            for candidate in request["candidates"]:
                assert candidate["gpu_dcgm"]["state"] == "UNAVAILABLE"
                assert candidate["kv_cache"]["state"] == "UNAVAILABLE"
    serialized = json.dumps(payload)
    for private_field in (
        str(package), "127.0.0.1", "declaration_sha256", "instance_identity_sha256",
        "gpu_uuid_sha256", "origin_sha256", "payload_sha256", "request_body",
    ):
        assert private_field not in serialized


@pytest.mark.parametrize(
    "mutation", ["wrong_digest", "invalid_json", "legacy_image", "receipt_tamper"]
)
def test_invalid_vast_v4_package_is_withheld_before_json_projection(
    tmp_path: Path, mutation: str
) -> None:
    sealed = seal_vast_fixture(tmp_path / "synthetic-vast")
    digest = verify_execution_package(sealed).report.retained_digest
    candidate = _mutable_copy(sealed, tmp_path / "invalid-vast")
    if mutation == "wrong_digest":
        digest = "sha256:" + "0" * 64
    elif mutation == "invalid_json":
        (candidate / "executed-manifest.json").write_bytes(b"{")
    elif mutation == "legacy_image":
        path = candidate / "executed-manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["runner_image"] = manifest["container_image"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        path = candidate / "producer-receipt.json"
        path.write_bytes(path.read_bytes() + b" ")
    _make_tree_immutable(candidate)
    with _client(candidate, digest) as client:
        index = client.get("/api/v1/routing-executions")
        detail = client.get("/api/v1/routing-executions/routing-execution-v1")
    assert index.status_code == 200
    assert index.json()["routing_executions"] == []
    assert index.json()["rejected"] == [{
        "entry": "<configured-root>", "status": "REJECTED",
        "code": "VERIFICATION_FAILED",
        "message": "Routing execution could not be verified.",
    }]
    assert detail.status_code == 404
    assert str(candidate) not in index.text
