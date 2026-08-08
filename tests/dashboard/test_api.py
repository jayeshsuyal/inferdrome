"""In-process HTTP contract for the read-only dashboard API."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex

BASELINE_RUN_ID = "run-44444444444444444444444444444444"
CANDIDATE_RUN_ID = "run-55555555555555555555555555555555"
UNKNOWN_RUN_ID = "run-ffffffffffffffffffffffffffffffff"


def _client(runs_root: Path) -> TestClient:
    return TestClient(create_app(DashboardIndex(runs_root)))


def test_list_detail_and_compare_routes_return_verified_projections(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "api-runs"
    run_fake_bundle(runs_root, BASELINE_RUN_ID)
    run_fake_bundle(runs_root, CANDIDATE_RUN_ID)

    with _client(runs_root) as client:
        list_response = client.get("/api/v1/runs")
        detail_response = client.get(f"/api/v1/runs/{BASELINE_RUN_ID}")
        compare_response = client.get(
            "/api/v1/compare",
            params={
                "baseline_run_id": BASELINE_RUN_ID,
                "candidate_run_id": CANDIDATE_RUN_ID,
            },
        )

    assert list_response.status_code == 200
    assert list_response.json()["page"] == {
        "limit": 100,
        "returned": 2,
        "total": 2,
        "has_more": False,
        "next_cursor": None,
    }
    assert {item["run_id"] for item in list_response.json()["runs"]} == {
        BASELINE_RUN_ID,
        CANDIDATE_RUN_ID,
    }
    assert detail_response.status_code == 200
    assert detail_response.json()["summary"]["run_id"] == BASELINE_RUN_ID
    assert compare_response.status_code == 200
    assert compare_response.json()["status"] == "COMPARABLE"

    serialized = json.dumps(
        {
            "list": list_response.json(),
            "detail": detail_response.json(),
            "compare": compare_response.json(),
        },
        sort_keys=True,
    ).lower()
    assert "fake response" not in serialized
    assert "better" not in serialized
    assert "worse" not in serialized


def test_api_exposes_rejected_bundle_without_error_details_or_metrics(
    sealed_fake_bundle: Any,
    tmp_path: Path,
    tampered_bundle: Callable[[Path, Path], Path],
) -> None:
    runs_root = tmp_path / "api-tampered-runs"
    runs_root.mkdir()
    tampered_path = tampered_bundle(
        sealed_fake_bundle.sealed.path,
        runs_root / "tampered-evidence",
    )

    with _client(runs_root) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runs"] == []
    assert len(payload["rejected"]) == 1
    rejected = payload["rejected"][0]
    assert rejected["status"] == "REJECTED"
    assert rejected["code"] == "VERIFICATION_FAILED"
    assert 0 < len(rejected["message"]) <= 160
    serialized = json.dumps(rejected, sort_keys=True).lower()
    assert str(tampered_path).lower() not in serialized
    assert "artifact hash does not match manifest" not in serialized
    assert "measurements" not in serialized
    assert "distributions" not in serialized


def test_ids_are_resolved_from_the_index_not_as_paths(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "api-indexed-runs"
    result = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    outside_path = result.sealed_bundle.path

    with _client(runs_root) as client:
        unknown = client.get(f"/api/v1/runs/{UNKNOWN_RUN_ID}")
        traversal = client.get(f"/api/v1/runs/{quote('../../outside', safe='')}")
        arbitrary_path = client.get(f"/api/v1/runs/{quote(str(outside_path), safe='')}")
        missing_compare = client.get(
            "/api/v1/compare",
            params={
                "baseline_run_id": BASELINE_RUN_ID,
                "candidate_run_id": UNKNOWN_RUN_ID,
            },
        )

    assert unknown.status_code == 404
    assert traversal.status_code == 404
    assert arbitrary_path.status_code == 404
    assert missing_compare.status_code == 404
    responses = (unknown, traversal, arbitrary_path, missing_compare)
    assert all(str(outside_path) not in response.text for response in responses)


def test_dashboard_api_routes_reject_mutating_methods(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "api-read-only-runs"
    run_fake_bundle(runs_root, BASELINE_RUN_ID)
    route_paths = (
        "/api/v1/runs",
        f"/api/v1/runs/{BASELINE_RUN_ID}",
        "/api/v1/trial-sets",
        "/api/v1/trial-sets/trial-set-11111111111111111111111111111111",
        "/api/v1/controlled-comparisons",
        (
            "/api/v1/controlled-comparisons/"
            "comparison-plan-11111111111111111111111111111111"
        ),
        (
            "/api/v1/compare"
            f"?baseline_run_id={BASELINE_RUN_ID}"
            f"&candidate_run_id={BASELINE_RUN_ID}"
        ),
    )

    with _client(runs_root) as client:
        responses = [
            client.request(method, path)
            for path in route_paths
            for method in ("POST", "PUT", "PATCH", "DELETE")
        ]

    assert responses
    assert all(response.status_code == 405 for response in responses)


def test_packaged_frontend_supports_deep_links_without_masking_api_404s(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "static"
    assets_root = static_root / "assets"
    assets_root.mkdir(parents=True)
    index_bytes = b"<!doctype html><title>Inferdrome dashboard</title>"
    (static_root / "index.html").write_bytes(index_bytes)
    (assets_root / "app.js").write_bytes(b"export {};")

    app = create_app(
        DashboardIndex(tmp_path / "runs"),
        static_dir=static_root,
    )
    with TestClient(app) as client:
        root = client.get("/")
        deep_link = client.get(f"/runs/{BASELINE_RUN_ID}")
        trial_set_deep_link = client.get(
            "/trial-sets/trial-set-11111111111111111111111111111111"
        )
        comparison_deep_link = client.get(
            "/comparisons/comparison-plan-11111111111111111111111111111111"
        )
        asset = client.get("/assets/app.js")
        missing_api = client.get("/api/v1/not-a-route")

    assert root.status_code == 200
    assert root.content == index_bytes
    assert deep_link.status_code == 200
    assert deep_link.content == index_bytes
    assert trial_set_deep_link.status_code == 200
    assert trial_set_deep_link.content == index_bytes
    assert comparison_deep_link.status_code == 200
    assert comparison_deep_link.content == index_bytes
    assert asset.status_code == 200
    assert asset.content == b"export {};"
    assert missing_api.status_code == 404
    assert root.headers["content-security-policy"].startswith("default-src")
    assert root.headers["x-content-type-options"] == "nosniff"


def test_run_index_pagination_is_bounded_and_cursor_driven(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "api-paged-runs"
    run_fake_bundle(runs_root, BASELINE_RUN_ID)
    run_fake_bundle(runs_root, CANDIDATE_RUN_ID)

    with _client(runs_root) as client:
        first = client.get("/api/v1/runs", params={"limit": 1})
        cursor = first.json()["page"]["next_cursor"]
        second = client.get(
            "/api/v1/runs",
            params={"limit": 1, "cursor": cursor},
        )
        too_large = client.get("/api/v1/runs", params={"limit": 201})
        invalid_cursor = client.get(
            "/api/v1/runs",
            params={"cursor": "not-a-dashboard-cursor"},
        )

    assert first.status_code == 200
    assert first.json()["page"]["returned"] == 1
    assert first.json()["page"]["has_more"] is True
    assert isinstance(cursor, str)
    assert second.status_code == 200
    assert second.json()["page"]["has_more"] is False
    assert {
        item["run_id"]
        for response in (first, second)
        for item in response.json()["runs"]
    } == {BASELINE_RUN_ID, CANDIDATE_RUN_ID}
    assert too_large.status_code == 422
    assert invalid_cursor.status_code == 400


def test_dashboard_rejects_untrusted_host_headers(tmp_path: Path) -> None:
    app = create_app(DashboardIndex(tmp_path / "runs"))
    with TestClient(app) as client:
        hostile = client.get(
            "/api/v1/runs",
            headers={"host": "attacker.example:8787"},
        )
        loopback = client.get(
            "/api/v1/runs",
            headers={"host": "127.0.0.1:8787"},
        )

    assert hostile.status_code == 400
    assert loopback.status_code == 200


def test_run_index_cursor_rejects_a_changed_snapshot(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "api-changing-runs"
    run_fake_bundle(runs_root, BASELINE_RUN_ID)
    run_fake_bundle(runs_root, CANDIDATE_RUN_ID)
    client = _client(runs_root)

    with client:
        first = client.get("/api/v1/runs", params={"limit": 1})
        cursor = first.json()["page"]["next_cursor"]
        run_fake_bundle(
            runs_root,
            "run-66666666666666666666666666666666",
        )
        stale = client.get(
            "/api/v1/runs",
            params={"limit": 1, "cursor": cursor},
        )

    assert first.status_code == 200
    assert stale.status_code == 400
