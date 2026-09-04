"""Adversarial checks for the causal qualification dashboard boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import inferdrome.dashboard.routing_qualification as qualification_dashboard
from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.routing_campaign import run_campaign
from inferdrome.routing_qualification import (
    capture_qualification,
    publish_qualification,
)

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"
_QUALIFICATION_ID = "stale-telemetry-qualification-v1"


def _seal_pair(tmp_path: Path) -> tuple[Path, Path, str]:
    campaign = run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        tmp_path / "campaign",
    )
    captured = capture_qualification(
        campaign.path,
        expected_source_digest=campaign.retained_digest,
    )
    qualification_root = tmp_path / "qualification"
    publish_qualification(
        captured,
        campaign_package=campaign.path,
        output_root=qualification_root,
    )
    return campaign.path, qualification_root, captured.retained_digest


def _client(campaign: Path, qualification: Path, digest: str) -> TestClient:
    return TestClient(
        create_app(
            DashboardIndex(Path("unused-runs")),
            routing_campaigns_root=campaign,
            routing_qualifications_root=qualification,
            expected_routing_qualification_digest=digest,
        )
    )


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


def _assert_withheld(client: TestClient) -> None:
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


@pytest.mark.parametrize("mutation", ("malformed", "hardlink", "fifo"))
def test_special_or_malformed_descriptor_is_withheld_before_projection(
    tmp_path: Path,
    mutation: str,
) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)
    artifact = qualification_root / _QUALIFICATION_ID
    descriptor = artifact / "qualification.json"
    try:
        _make_tree_writable(qualification_root)
        if mutation == "malformed":
            descriptor.write_bytes(b"{")
        elif mutation == "hardlink":
            os.link(descriptor, qualification_root / "descriptor-copy")
        else:
            descriptor.unlink()
            os.mkfifo(descriptor)
        _make_tree_immutable(qualification_root)

        with _client(campaign, qualification_root, digest) as client:
            _assert_withheld(client)
    finally:
        _make_tree_writable(qualification_root)


def test_projection_keeps_held_snapshot_when_source_path_changes_after_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source path mutation after replay cannot change rendered causal facts."""

    campaign, qualification_root, digest = _seal_pair(tmp_path)
    original_loader = qualification_dashboard.load_verified_campaign
    changed = False

    def load_then_change(
        path: Path,
        **kwargs: object,
    ):
        nonlocal changed
        verified = original_loader(path, **kwargs)
        _make_tree_writable(campaign)
        (campaign / "campaign-plan.json").write_bytes(b"{")
        changed = True
        return verified

    monkeypatch.setattr(
        qualification_dashboard, "load_verified_campaign", load_then_change
    )
    index = qualification_dashboard.RoutingQualificationDashboardIndex(
        routing_campaigns_root=campaign,
        routing_qualifications_root=qualification_root,
        expected_qualification_digest=digest,
    )

    response = index.refresh()

    assert changed
    assert response.rejected == ()
    detail = index.get_qualification(_QUALIFICATION_ID)
    assert detail.fault_timeline.freshness_bound_ms == 5
    assert detail.trials[0].terminal_population_total == 6
    content, content_digest = index.get_evidence(_QUALIFICATION_ID)
    assert content_digest == digest
    assert b'"qualification_id"' in content


def test_failed_source_replay_never_leaves_a_renderable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, qualification_root, digest = _seal_pair(tmp_path)

    def reject(*_args: object, **_kwargs: object) -> object:
        raise ValueError("test-only replay failure")

    monkeypatch.setattr(qualification_dashboard, "load_verified_campaign", reject)
    index = qualification_dashboard.RoutingQualificationDashboardIndex(
        routing_campaigns_root=campaign,
        routing_qualifications_root=qualification_root,
        expected_qualification_digest=digest,
    )

    response = index.refresh()

    assert response.routing_qualifications == ()
    assert response.rejected[0].code == "VERIFICATION_FAILED"
    with pytest.raises(qualification_dashboard.DashboardRoutingQualificationNotFound):
        index.get_qualification(_QUALIFICATION_ID)
    with pytest.raises(qualification_dashboard.DashboardRoutingQualificationNotFound):
        index.get_evidence(_QUALIFICATION_ID)
