"""Read-only causal qualification dashboard contracts."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.auth import DashboardKeyringStore
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.routing_campaign import run_campaign
from inferdrome.routing_qualification import (
    capture_qualification,
    publish_qualification,
)

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"
_QUALIFICATION_ID = "stale-telemetry-qualification-v1"


def _seal_pair(output_root: Path) -> tuple[Path, Path, str]:
    campaign = run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output_root / "campaign",
    )
    qualification_root = output_root / "qualification"
    captured = capture_qualification(
        campaign.path,
        expected_source_digest=campaign.retained_digest,
    )
    publish_qualification(
        captured,
        campaign_package=campaign.path,
        output_root=qualification_root,
    )
    return campaign.path, qualification_root, captured.retained_digest


def _client(
    campaign: Path | None,
    qualification_root: Path | None = None,
    digest: str | None = None,
    *,
    keyring_path: Path | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            DashboardIndex(Path("unused-runs")),
            routing_campaigns_root=campaign,
            routing_qualifications_root=qualification_root,
            expected_routing_qualification_digest=digest,
            keyring_path=keyring_path,
        )
    )


def test_verified_qualification_projects_causal_state_and_canonical_download(
    tmp_path: Path,
) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)

    with _client(campaign, qualification_root, digest) as client:
        index = client.get("/api/v1/routing-qualifications")
        detail = client.get(f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}")
        evidence = client.get(
            f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}/evidence"
        )

    assert index.status_code == detail.status_code == evidence.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert detail.headers["cache-control"] == "no-store"
    assert evidence.headers["cache-control"] == "no-store"
    index_payload = index.json()
    detail_payload = detail.json()
    assert index_payload["projection_version"] == (
        "inferdrome.routing-qualification-dashboard.v1"
    )
    assert index_payload["rejected"] == []
    assert index_payload["page"] == {
        "limit": 25,
        "returned": 1,
        "total": 1,
        "has_more": False,
        "next_cursor": None,
    }
    assert index_payload["routing_qualifications"] == [detail_payload["summary"]]
    assert detail_payload["summary"]["retained_digest"] == digest
    assert detail_payload["summary"]["verified_by_source_replay"] is True
    assert detail_payload["summary"]["verified_descriptor_binding"] is True
    assert detail_payload["fault_timeline"] == {
        "load_observer_pause_at_ms": 15,
        "health_collection_continues": True,
        "focal_decision_time_ms": 20,
        "health_age_ms": 0,
        "load_age_ms": 10,
        "freshness_bound_ms": 5,
    }
    assert detail_payload["interpretation_boundary"] == "MEASUREMENT_EVIDENCE_ONLY"
    assert (
        detail_payload["source_receipts_path"]
        == "/routing-campaigns/routing-campaign-v1"
    )
    assert len(detail_payload["trials"]) == 3
    expected = (
        ("fail_closed_required_load_v1", None, "REQUIRED_LOAD_STALE", "NO_SAFE_ROUTE"),
        (
            "explicit_fail_open_stale_load_v1",
            "endpoint-b",
            "STALE_LOAD_FAIL_OPEN",
            "TIMED_OUT",
        ),
        (
            "typed_admissible_state_only_v1",
            "endpoint-a",
            "HEALTH_ONLY_TIE_BREAK",
            "SUCCEEDED",
        ),
    )
    for trial, (policy, endpoint, fallback, terminal) in zip(
        detail_payload["trials"], expected, strict=True
    ):
        assert trial["policy_id"] == policy
        assert trial["repetition_index"] == 0
        assert trial["request_denominator"] == trial["terminal_population_total"] == 6
        assert trial["selected_endpoint_id"] == endpoint
        assert trial["fallback_reason"] == fallback
        assert trial["terminal_status"] == terminal
        assert [state["endpoint_id"] for state in trial["focal_endpoint_states"]] == [
            "endpoint-a",
            "endpoint-b",
        ]
        for state in trial["focal_endpoint_states"]:
            assert state["health_age_ms"] == 0
            assert state["health_admissibility"] == "ADMISSIBLE"
            assert state["load_age_ms"] == 10
            assert state["load_admissibility"] == "INADMISSIBLE"
        assert sum(item["count"] for item in trial["terminal_population"]) == 6

    assert evidence.headers["content-type"] == "application/json"
    assert evidence.headers["content-disposition"] == (
        'attachment; filename="stale-telemetry-qualification-v1.json"'
    )
    assert evidence.headers["x-inferdrome-evidence-digest"] == digest
    assert json.loads(evidence.content) == json.loads(
        (
            qualification_root
            / "stale-telemetry-qualification-v1"
            / "qualification.json"
        ).read_bytes()
    )
    serialized = json.dumps(detail_payload, sort_keys=True)
    assert str(campaign) not in serialized
    assert str(qualification_root) not in serialized
    assert "artifact-hashes.json" not in serialized


def test_partial_or_bad_digest_configuration_withholds_overlay_not_r1(
    tmp_path: Path,
) -> None:
    campaign, qualification_root, _ = _seal_pair(tmp_path)

    with _client(campaign) as client:
        partial = client.get("/api/v1/routing-qualifications")
        r1 = client.get("/api/v1/routing-campaigns")
    with _client(campaign, qualification_root, "not-a-digest") as client:
        malformed = client.get("/api/v1/routing-qualifications")
        detail = client.get(f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}")
        evidence = client.get(
            f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}/evidence"
        )

    for response in (partial, malformed):
        assert response.status_code == 200
        assert response.json()["routing_qualifications"] == []
        assert response.json()["rejected"] == [
            {
                "entry": "<configured-root>",
                "status": "REJECTED",
                "code": "CONFIGURATION_INVALID",
                "message": "Routing qualification could not be verified.",
            }
        ]
        assert "endpoint-a" not in response.text
    assert r1.status_code == 200
    assert r1.json()["routing_campaigns"]
    assert detail.status_code == evidence.status_code == 404


def test_mismatched_digest_withholds_all_qualification_facts(tmp_path: Path) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)
    mismatched = f"sha256:{'0' * 64}" if digest[-1] != "0" else f"sha256:{'1' * 64}"

    with _client(campaign, qualification_root, mismatched) as client:
        index = client.get("/api/v1/routing-qualifications")
        detail = client.get(f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}")
        evidence = client.get(
            f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}/evidence"
        )

    assert index.status_code == 200
    assert index.json()["routing_qualifications"] == []
    assert index.json()["rejected"][0]["code"] == "VERIFICATION_FAILED"
    assert detail.status_code == evidence.status_code == 404
    assert "endpoint-a" not in index.text
    assert str(qualification_root) not in index.text


def test_qualification_routes_are_get_only_and_auth_protected(tmp_path: Path) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)
    keyring_path = tmp_path / "keyring.json"
    token, _ = DashboardKeyringStore(keyring_path).create("qualification-test")
    routes = (
        "/api/v1/routing-qualifications",
        f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}",
        f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}/evidence",
    )

    with _client(
        campaign,
        qualification_root,
        digest,
        keyring_path=keyring_path,
    ) as client:
        unauthenticated = [client.get(route) for route in routes]
        authenticated = [
            client.get(route, headers={"Authorization": f"Bearer {token}"})
            for route in routes
        ]
        non_get = [client.post(route) for route in routes]

    assert all(response.status_code == 401 for response in unauthenticated)
    assert all(response.status_code == 200 for response in authenticated)
    assert all(response.status_code == 405 for response in non_get)


def test_qualification_root_symlink_is_withheld_without_path_disclosure(
    tmp_path: Path,
) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)
    link = tmp_path / "qualification-link"
    link.symlink_to(qualification_root, target_is_directory=True)

    with _client(campaign, link, digest) as client:
        response = client.get("/api/v1/routing-qualifications")

    assert response.status_code == 200
    assert response.json()["routing_qualifications"] == []
    assert response.json()["rejected"][0]["code"] == "UNSAFE_ENTRY"
    assert str(qualification_root) not in response.text


def test_download_uses_verified_snapshot_without_reopening_descriptor_path(
    tmp_path: Path,
) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)
    descriptor = (
        qualification_root / "stale-telemetry-qualification-v1" / "qualification.json"
    )

    with _client(campaign, qualification_root, digest) as client:
        assert client.get("/api/v1/routing-qualifications").status_code == 200
        descriptor.chmod(0o600)
        descriptor.write_bytes(b"{")
        descriptor.chmod(0o400)
        evidence = client.get(
            f"/api/v1/routing-qualifications/{_QUALIFICATION_ID}/evidence"
        )

    assert evidence.status_code == 200
    assert evidence.headers["x-inferdrome-evidence-digest"] == digest
    assert json.loads(evidence.content)["qualification_id"] == _QUALIFICATION_ID
