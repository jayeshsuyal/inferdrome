"""Read-only dashboard contracts for one verified routing-execution package."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.auth import DashboardKeyringStore
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.deployment.manual_host import (
    H100ManualHostInput,
    ManualHostInput,
    prepare_artifacts,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.executor import (
    ManualMonotonicClock,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.transport import EndpointTransport
from tests.routing_execution_support import (
    StaticEndpointTransport,
    h100_manual_host_fixture_input,
    manual_host_fixture_input,
)

_ROOT = Path(__file__).resolve().parents[2]
_EXECUTION_ID = "routing-execution-v1"


def _source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def _sealed_execution(
    output_root: Path,
    *,
    transport_factory: Callable[[], EndpointTransport] = StaticEndpointTransport,
    cancel_requested: Callable[[], bool] | None = None,
) -> Path:
    spec = ManualHostInput.model_validate_json(
        canonical_json_bytes(manual_host_fixture_input(source_commit=_source_commit()))
    )
    files = prepare_artifacts(spec, _ROOT)
    return run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        output_root,
        transport_factory=transport_factory,
        clock=ManualMonotonicClock(),
        cancel_requested=cancel_requested,
    ).path


def _sealed_h100_execution(output_root: Path) -> Path:
    spec = H100ManualHostInput.model_validate_json(
        canonical_json_bytes(
            h100_manual_host_fixture_input(source_commit=_source_commit())
        )
    )
    files = prepare_artifacts(spec, _ROOT)
    return run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        output_root,
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    ).path


def _retained_digest(package: Path) -> str:
    from inferdrome.routing_execution.verifier import verify_execution_package

    return verify_execution_package(package).report.retained_digest


def _make_tree_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _make_tree_immutable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o400)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o500)
        current.chmod(0o500)


def _mutable_copy(source: Path, destination: Path) -> Path:
    copied = Path(shutil.copytree(source, destination, copy_function=shutil.copy2))
    _make_tree_writable(copied)
    return copied


def _client(
    package: Path | None,
    digest: str | None,
    *,
    keyring_path: Path | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            DashboardIndex(Path("unused-runs")),
            routing_execution_root=package,
            expected_routing_execution_digest=digest,
            keyring_path=keyring_path,
        )
    )


def test_verified_v2_execution_index_and_detail_are_allowlisted(tmp_path: Path) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(package)

    with _client(package, digest) as client:
        index = client.get("/api/v1/routing-executions")
        detail = client.get(f"/api/v1/routing-executions/{_EXECUTION_ID}")

    assert index.status_code == detail.status_code == 200
    index_payload = index.json()
    detail_payload = detail.json()
    assert index.headers["cache-control"] == "no-store"
    assert index_payload["projection_version"] == (
        "inferdrome.routing-execution-dashboard.v1"
    )
    assert index_payload["rejected"] == []
    assert index_payload["page"] == {
        "limit": 25,
        "returned": 1,
        "total": 1,
        "has_more": False,
        "next_cursor": None,
    }
    summary = index_payload["routing_executions"]
    assert len(summary) == 1
    assert summary[0]["execution_id"] == _EXECUTION_ID
    assert summary[0]["retained_digest"] == digest
    assert summary[0]["mode"] == "LAMBDA_MANUAL_HOST"
    assert summary[0]["topology"] == {
        "accelerator_model": "NVIDIA A100-PCIE-40GB",
        "accelerator_count": 2,
        "runner_separate_from_serving": True,
        "serving_engine_count": 2,
        "one_engine_per_endpoint": True,
        "declared_provider": "LAMBDA",
        "declared_provisioning": "OPERATOR_SUPPLIED_VM",
        "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
        "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
    }
    assert detail_payload["summary"] == summary[0]
    assert detail_payload["interpretation_boundary"] == "MEASUREMENT_EVIDENCE_ONLY"
    assert detail_payload["evidence"]["endpoints"] == [
        {"endpoint_id": "endpoint-a"},
        {"endpoint_id": "endpoint-b"},
    ]
    assert detail_payload["trials"][0]["reset"]["endpoint_runtime_identities"] == [
        {"endpoint_id": "endpoint-a"},
        {"endpoint_id": "endpoint-b"},
    ]
    assert len(detail_payload["trials"]) == 3
    for trial in detail_payload["trials"]:
        assert trial["reset"]["endpoint_engine_reset_assertion"] == (
            "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
        )
        assert trial["fault"]["activated_at_sequence_index"] == 2
        assert len(trial["requests"]) == 6
        stale_request = trial["requests"][2]
        for candidate in stale_request["candidates"]:
            assert candidate["health"]["admissibility"] == "ADMISSIBLE"
            assert candidate["load"]["state"] == "STALE"
            assert candidate["load"]["admissibility"] == "INADMISSIBLE"
            assert candidate["gpu_dcgm"] == {
                **candidate["gpu_dcgm"],
                "state": "UNAVAILABLE",
                "value": "UNAVAILABLE",
                "source": "UNAVAILABLE_CAPABILITY",
            }
            assert candidate["kv_cache"] == {
                **candidate["kv_cache"],
                "state": "UNAVAILABLE",
                "value": "UNAVAILABLE",
                "source": "UNAVAILABLE_CAPABILITY",
            }
        assert sum(row["count"] for row in trial["terminal_population"]) == 6
        assert trial["terminal_population_total"] == 6
        for request in trial["requests"]:
            assert request["terminal"]["decision_id"] == request["decision_id"]

    serialized = json.dumps(detail_payload, sort_keys=True)
    for forbidden in (
        str(package),
        "172.29.71.2",
        "172.29.71.3",
        "0123456789abcdef0123456789abcdef",
        "GPU-00000000",
        "/srv/test",
        "synthetic-operator",
        "prompt",
        "response_sha256",
        "payload_sha256",
        "origin_sha256",
        "capability_identity_sha256",
        "evidence_destination_sha256",
    ):
        assert forbidden not in serialized


def test_verified_v3_h100_profile_uses_the_existing_read_only_projection(
    tmp_path: Path,
) -> None:
    package = _sealed_h100_execution(tmp_path / "sealed-h100-execution")
    with _client(package, _retained_digest(package)) as client:
        response = client.get("/api/v1/routing-executions")

    assert response.status_code == 200
    summary = response.json()["routing_executions"]
    assert len(summary) == 1
    assert summary[0]["mode"] == "LAMBDA_MANUAL_HOST"
    assert summary[0]["topology"] == {
        "accelerator_model": "NVIDIA H100-SXM5-80GB",
        "accelerator_count": 2,
        "runner_separate_from_serving": True,
        "serving_engine_count": 2,
        "one_engine_per_endpoint": True,
        "declared_provider": "LAMBDA",
        "declared_provisioning": "OPERATOR_SUPPLIED_VM",
        "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
        "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
    }


def test_execution_id_is_only_resolved_from_verified_snapshot(tmp_path: Path) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(package)
    traversal = quote("../../outside", safe="")
    with _client(package, digest) as client:
        known = client.get(f"/api/v1/routing-executions/{_EXECUTION_ID}")
        unknown = client.get("/api/v1/routing-executions/not-an-execution")
        traversal_response = client.get(f"/api/v1/routing-executions/{traversal}")
        arbitrary = client.get(
            f"/api/v1/routing-executions/{quote(str(package), safe='')}"
        )
    assert known.status_code == 200
    assert all(
        response.status_code == 404
        for response in (unknown, traversal_response, arbitrary)
    )
    assert all(
        str(package) not in response.text
        for response in (unknown, traversal_response, arbitrary)
    )


def test_verified_degraded_health_and_pre_dispatch_cancellation_remain_renderable(
    tmp_path: Path,
) -> None:
    health_unavailable = _sealed_execution(
        tmp_path / "health-unavailable",
        transport_factory=lambda: StaticEndpointTransport(mode="unavailable_health"),
    )
    cancelled = _sealed_execution(
        tmp_path / "cancelled",
        cancel_requested=lambda: True,
    )
    for package, expected_fallback, expected_status, expected_attempts in (
        (
            health_unavailable,
            "HEALTH_NOT_ADMISSIBLE",
            "NO_SAFE_ROUTE",
            0,
        ),
        (cancelled, "NONE", "CANCELLED", 0),
    ):
        with _client(package, _retained_digest(package)) as client:
            response = client.get(f"/api/v1/routing-executions/{_EXECUTION_ID}")
        assert response.status_code == 200
        first_request = response.json()["trials"][0]["requests"][0]
        assert first_request["fallback_reason"] == expected_fallback
        assert first_request["terminal"]["status"] == expected_status
        assert first_request["terminal"]["attempt_count"] == expected_attempts
        if expected_fallback == "HEALTH_NOT_ADMISSIBLE":
            assert first_request["candidates"][0]["health"] == {
                **first_request["candidates"][0]["health"],
                "state": "UNAVAILABLE",
                "source": "HTTP_HEALTH",
                "value": "UNAVAILABLE",
            }


@pytest.mark.parametrize("root,digest", [(None, None), (None, "sha256:" + "a" * 64)])
def test_unconfigured_or_partial_execution_configuration_is_bounded(
    tmp_path: Path, root: Path | None, digest: str | None
) -> None:
    del tmp_path
    with _client(root, digest) as client:
        index = client.get("/api/v1/routing-executions")
        detail = client.get(f"/api/v1/routing-executions/{_EXECUTION_ID}")
    assert index.status_code == 200
    if root is None and digest is None:
        assert index.json()["routing_executions"] == []
        assert index.json()["rejected"] == []
    else:
        assert index.json()["routing_executions"] == []
        assert index.json()["rejected"][0]["code"] == "CONFIGURATION_INVALID"
    assert detail.status_code == 404


@pytest.mark.parametrize("mutation", ["injected", "oversized", "malformed"])
def test_tampered_inventory_size_and_content_are_withheld(
    tmp_path: Path, mutation: str
) -> None:
    sealed = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(sealed)
    candidate = _mutable_copy(sealed, tmp_path / mutation)
    if mutation == "injected":
        (candidate / "unexpected.json").write_text("{}", encoding="utf-8")
    elif mutation == "oversized":
        (candidate / "producer-receipt.json").write_bytes(b"x" * 8_388_609)
    else:
        (candidate / "executed-manifest.json").write_bytes(b"{")
    _make_tree_immutable(candidate)
    with _client(candidate, digest) as client:
        index = client.get("/api/v1/routing-executions")
        detail = client.get(f"/api/v1/routing-executions/{_EXECUTION_ID}")
    assert index.status_code == 200
    assert index.json()["routing_executions"] == []
    assert index.json()["rejected"] == [
        {
            "entry": "<configured-root>",
            "status": "REJECTED",
            "code": "VERIFICATION_FAILED",
            "message": "Routing execution could not be verified.",
        }
    ]
    assert detail.status_code == 404
    assert str(candidate) not in index.text


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "fifo"])
def test_unsafe_execution_artifacts_are_withheld(tmp_path: Path, mutation: str) -> None:
    sealed = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(sealed)
    candidate = _mutable_copy(sealed, tmp_path / mutation)
    target = candidate / "producer-receipt.json"
    target.unlink()
    if mutation == "symlink":
        target.symlink_to("executed-manifest.json")
    elif mutation == "hardlink":
        os.link(candidate / "executed-manifest.json", target)
    else:
        os.mkfifo(target, 0o600)
    _make_tree_immutable(candidate)
    with _client(candidate, digest) as client:
        index = client.get("/api/v1/routing-executions")
    assert index.status_code == 200
    assert index.json()["routing_executions"] == []
    assert index.json()["rejected"][0]["code"] == "VERIFICATION_FAILED"
    assert str(candidate) not in index.text


def test_root_symlink_and_staging_root_are_withheld(tmp_path: Path) -> None:
    sealed = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(sealed)
    root_link = tmp_path / "execution-link"
    root_link.symlink_to(sealed, target_is_directory=True)
    with _client(root_link, digest) as client:
        symlink = client.get("/api/v1/routing-executions")
    assert symlink.json()["routing_executions"] == []
    assert symlink.json()["rejected"][0]["code"] == "UNSAFE_ENTRY"

    staging = tmp_path / ".routing-execution-stage-test"
    sealed.replace(staging)
    with _client(staging, digest) as client:
        response = client.get("/api/v1/routing-executions")
    assert response.json()["routing_executions"] == []
    assert response.json()["rejected"][0]["code"] == "UNSAFE_ENTRY"


def test_execution_routes_are_get_only_and_auth_protected(tmp_path: Path) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(package)
    keyring_path = tmp_path / "dashboard-keyring.json"
    token, _ = DashboardKeyringStore(keyring_path).create("execution-test")
    routes = (
        "/api/v1/routing-executions",
        f"/api/v1/routing-executions/{_EXECUTION_ID}",
    )
    with _client(package, digest, keyring_path=keyring_path) as client:
        unauthenticated = [client.get(route) for route in routes]
        authenticated = [
            client.get(route, headers={"Authorization": f"Bearer {token}"})
            for route in routes
        ]
        mutating = [
            client.request(method, route, headers={"Authorization": f"Bearer {token}"})
            for route in routes
            for method in ("POST", "PUT", "PATCH", "DELETE")
        ]
    assert all(response.status_code == 401 for response in unauthenticated)
    assert all(response.status_code == 200 for response in authenticated)
    assert all(response.status_code == 405 for response in mutating)


def test_execution_page_bound_is_enforced(tmp_path: Path) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = _retained_digest(package)
    with _client(package, digest) as client:
        valid = client.get("/api/v1/routing-executions", params={"limit": 1})
        too_large = client.get("/api/v1/routing-executions", params={"limit": 26})
    assert valid.status_code == 200
    assert valid.json()["page"]["returned"] == 1
    assert too_large.status_code == 422
