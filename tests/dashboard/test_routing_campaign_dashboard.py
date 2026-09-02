"""Read-only dashboard contracts for a verified routing-campaign-v1 package."""

import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.auth import DashboardKeyringStore
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.routing_campaign import run_campaign

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"
_CAMPAIGN_ID = "routing-campaign-v1"


def _seal_campaign(output_root: Path) -> Path:
    return run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output_root,
    ).path


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
    package_root: Path | None,
    *,
    keyring_path: Path | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            DashboardIndex(Path("unused-runs")),
            routing_campaigns_root=package_root,
            keyring_path=keyring_path,
        )
    )


def test_verified_campaign_index_and_detail_allowlist_complete_receipts(
    tmp_path: Path,
) -> None:
    package = _seal_campaign(tmp_path / "sealed-campaign")

    with _client(package) as client:
        index = client.get("/api/v1/routing-campaigns")
        detail = client.get(f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}")

    assert index.status_code == detail.status_code == 200
    index_payload = index.json()
    detail_payload = detail.json()
    assert index.headers["cache-control"] == "no-store"
    assert index_payload["projection_version"] == (
        "inferdrome.routing-campaign-dashboard.v1"
    )
    assert index_payload["rejected"] == []
    assert index_payload["page"] == {
        "limit": 25,
        "returned": 1,
        "total": 1,
        "has_more": False,
        "next_cursor": None,
    }
    summary = index_payload["routing_campaigns"]
    assert len(summary) == 1
    assert summary[0]["campaign_id"] == _CAMPAIGN_ID
    assert summary[0]["trial_count"] == 3
    assert summary[0]["planned_request_count"] == 18
    assert summary[0]["verified_by_replay"] is True

    assert detail_payload["summary"] == summary[0]
    assert detail_payload["interpretation_boundary"] == "MEASUREMENT_EVIDENCE_ONLY"
    assert detail_payload["fault_timeline"] == {
        "load_collection_paused_at_ms": 15,
        "health_collection_continues": True,
        "load_freshness_bound_ms": 5,
        "health_freshness_bound_ms": 5,
    }
    assert len(detail_payload["trials"]) == 3
    expected_statuses = {
        "SUCCEEDED",
        "TIMED_OUT",
        "FAILED",
        "CANCELLED",
        "NO_SAFE_ROUTE",
    }
    for trial in detail_payload["trials"]:
        reset = trial["reset"]
        assert reset["virtual_time_ms"] == 0
        assert reset["queue_cleared"] is True
        assert reset["load_state_cleared"] is True
        assert reset["kv_state_cleared"] is True
        assert {item["endpoint_id"] for item in reset["endpoint_instances"]} == {
            "endpoint-a",
            "endpoint-b",
        }
        assert len(reset["observer_epochs"]) == 3
        assert len(trial["requests"]) == 6
        stale_load_request = trial["requests"][2]
        for candidate in stale_load_request["candidates"]:
            assert candidate["health"]["age_ms"] == 0
            assert candidate["health"]["admissibility"] == "ADMISSIBLE"
            assert candidate["load"]["age_ms"] == 10
            assert candidate["load"]["admissibility"] == "INADMISSIBLE"
            assert {candidate["health"]["epoch"], candidate["load"]["epoch"]}
        population = trial["terminal_population"]
        assert {item["status"] for item in population} == expected_statuses
        assert sum(item["count"] for item in population) == 6
        assert trial["terminal_population_total"] == 6
        for request in trial["requests"]:
            assert request["terminal"]["decision_id"] == request["decision_id"]

    serialized = json.dumps(detail_payload, sort_keys=True)
    assert str(package) not in serialized
    assert "artifact-hashes.json" not in serialized
    assert "campaign-plan.json" not in serialized


def test_campaign_id_is_resolved_only_from_verified_snapshot(tmp_path: Path) -> None:
    package = _seal_campaign(tmp_path / "sealed-campaign")
    traversal = quote("../../outside", safe="")

    with _client(package) as client:
        known = client.get(f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}")
        unknown = client.get("/api/v1/routing-campaigns/not-a-campaign")
        traversal_response = client.get(f"/api/v1/routing-campaigns/{traversal}")
        arbitrary = client.get(
            f"/api/v1/routing-campaigns/{quote(str(package), safe='')}"
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


def test_unconfigured_campaign_root_is_an_empty_bounded_index() -> None:
    with _client(None) as client:
        index = client.get("/api/v1/routing-campaigns")
        detail = client.get(f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}")

    assert index.status_code == 200
    assert index.json()["routing_campaigns"] == []
    assert index.json()["rejected"] == []
    assert index.json()["page"] == {
        "limit": 25,
        "returned": 0,
        "total": 0,
        "has_more": False,
        "next_cursor": None,
    }
    assert detail.status_code == 404


@pytest.mark.parametrize("mutation", ["injected", "oversized", "malformed"])
def test_unverified_inventory_size_and_malformed_content_are_withheld(
    tmp_path: Path,
    mutation: str,
) -> None:
    sealed = _seal_campaign(tmp_path / "sealed-campaign")
    candidate = _mutable_copy(sealed, tmp_path / mutation)
    if mutation == "injected":
        (candidate / "unexpected.json").write_text("{}", encoding="utf-8")
    elif mutation == "oversized":
        (candidate / "campaign-plan.json").write_bytes(b"x" * 8_388_609)
    else:
        (candidate / "fault-schedule.json").write_bytes(b"{")
    _make_tree_immutable(candidate)

    with _client(candidate) as client:
        index = client.get("/api/v1/routing-campaigns")
        detail = client.get(f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}")

    assert index.status_code == 200
    assert index.json()["routing_campaigns"] == []
    assert index.json()["rejected"] == [
        {
            "entry": "<configured-root>",
            "status": "REJECTED",
            "code": "VERIFICATION_FAILED",
            "message": "Routing campaign could not be verified.",
        }
    ]
    assert detail.status_code == 404
    for response in (index, detail):
        assert str(candidate) not in response.text
        assert "fault-schedule" not in response.text
        assert "candidates" not in response.text


@pytest.mark.parametrize("mutation", ["symlink", "hardlink"])
def test_symlink_and_hardlink_campaign_artifacts_are_withheld(
    tmp_path: Path,
    mutation: str,
) -> None:
    sealed = _seal_campaign(tmp_path / "sealed-campaign")
    candidate = _mutable_copy(sealed, tmp_path / mutation)
    target = candidate / "campaign-plan.json"
    target.unlink()
    if mutation == "symlink":
        target.symlink_to("fault-schedule.json")
    else:
        os.link(candidate / "fault-schedule.json", target)
    _make_tree_immutable(candidate)

    with _client(candidate) as client:
        index = client.get("/api/v1/routing-campaigns")
        detail = client.get(f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}")

    assert index.status_code == 200
    assert index.json()["routing_campaigns"] == []
    assert index.json()["rejected"][0]["code"] == "VERIFICATION_FAILED"
    assert detail.status_code == 404
    assert "candidates" not in index.text
    assert str(candidate) not in index.text


def test_root_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    sealed = _seal_campaign(tmp_path / "sealed-campaign")
    root_link = tmp_path / "routing-link"
    root_link.symlink_to(sealed, target_is_directory=True)

    with _client(root_link) as client:
        index = client.get("/api/v1/routing-campaigns")

    assert index.status_code == 200
    assert index.json()["routing_campaigns"] == []
    assert index.json()["rejected"] == [
        {
            "entry": "<configured-root>",
            "status": "REJECTED",
            "code": "UNSAFE_ENTRY",
            "message": "Routing campaign could not be verified.",
        }
    ]
    assert str(sealed) not in index.text


def test_transient_staging_root_is_withheld_without_reading_it(tmp_path: Path) -> None:
    sealed = _seal_campaign(tmp_path / "sealed-campaign")
    staging = tmp_path / ".routing-campaign-stage-test"
    sealed.replace(staging)

    with _client(staging) as client:
        index = client.get("/api/v1/routing-campaigns")

    assert index.status_code == 200
    assert index.json()["routing_campaigns"] == []
    assert index.json()["rejected"][0]["code"] == "UNSAFE_ENTRY"
    assert "candidates" not in index.text


def test_campaign_routes_are_get_only_and_auth_protected(tmp_path: Path) -> None:
    package = _seal_campaign(tmp_path / "sealed-campaign")
    keyring_path = tmp_path / "dashboard-keyring.json"
    token, _ = DashboardKeyringStore(keyring_path).create("routing-test")
    routes = (
        "/api/v1/routing-campaigns",
        f"/api/v1/routing-campaigns/{_CAMPAIGN_ID}",
    )

    with _client(package, keyring_path=keyring_path) as client:
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


def test_routing_campaign_pagination_bounds_and_snapshot_cursor(tmp_path: Path) -> None:
    package = _seal_campaign(tmp_path / "sealed-campaign")

    with _client(package) as client:
        valid = client.get("/api/v1/routing-campaigns", params={"limit": 1})
        too_large = client.get("/api/v1/routing-campaigns", params={"limit": 26})
        invalid_cursor = client.get(
            "/api/v1/routing-campaigns", params={"cursor": "bad-cursor"}
        )

    assert valid.status_code == 200
    assert valid.json()["page"]["returned"] == 1
    assert too_large.status_code == 422
    assert invalid_cursor.status_code == 400
