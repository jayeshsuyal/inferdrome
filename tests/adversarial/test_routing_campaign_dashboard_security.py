"""TOCTOU and fail-closed checks for the R2 routing-campaign projection."""

import os
import shutil
from pathlib import Path

import pytest

import inferdrome.dashboard.routing_campaign as routing_dashboard
import inferdrome.routing_campaign.package as routing_package
from inferdrome.errors import VerificationError
from inferdrome.routing_campaign import load_verified_campaign, run_campaign

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def _sealed_campaign(output_root: Path) -> Path:
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


def _mutable_copy(source: Path, destination: Path) -> Path:
    copied = Path(shutil.copytree(source, destination, copy_function=shutil.copy2))
    _make_tree_writable(copied)
    return copied


def test_verified_snapshot_refuses_same_file_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutable = _mutable_copy(
        _sealed_campaign(tmp_path / "sealed-campaign"),
        tmp_path / "mutable-campaign",
    )
    original_read = routing_package._read_scanned_file
    replaced = False

    def replace_after_read(scanned: routing_package._ScannedFile) -> bytes:
        nonlocal replaced
        content = original_read(scanned)
        if not replaced and scanned.relative_path == "campaign-plan.json":
            replacement = scanned.path.with_name(".campaign-plan-replacement")
            replacement.write_bytes(content)
            replacement.replace(scanned.path)
            replaced = True
        return content

    monkeypatch.setattr(routing_package, "_read_scanned_file", replace_after_read)

    with pytest.raises(VerificationError, match="changed during verification"):
        load_verified_campaign(mutable, require_immutable=False)
    assert replaced


def test_verified_snapshot_bounds_directory_inventory_before_sorting(
    tmp_path: Path,
) -> None:
    mutable = _mutable_copy(
        _sealed_campaign(tmp_path / "sealed-campaign"),
        tmp_path / "mutable-campaign",
    )
    for index in range(129):
        (mutable / f"overflow-{index:03d}").write_bytes(b"")

    with pytest.raises(VerificationError, match="bounded inventory"):
        load_verified_campaign(mutable, require_immutable=False)


def test_verified_snapshot_refuses_root_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutable = _mutable_copy(
        _sealed_campaign(tmp_path / "sealed-campaign"),
        tmp_path / "mutable-campaign",
    )
    replacement = Path(shutil.copytree(mutable, tmp_path / "replacement-campaign"))
    original_read = routing_package._read_scanned_file
    replaced = False

    def replace_root_after_read(scanned: routing_package._ScannedFile) -> bytes:
        nonlocal replaced
        content = original_read(scanned)
        if not replaced and scanned.relative_path == "campaign-plan.json":
            displaced = mutable.with_name("displaced-campaign")
            mutable.replace(displaced)
            replacement.replace(mutable)
            replaced = True
        return content

    monkeypatch.setattr(routing_package, "_read_scanned_file", replace_root_after_read)

    with pytest.raises(VerificationError, match="changed during verification"):
        load_verified_campaign(mutable, require_immutable=False)
    assert replaced


def test_dashboard_projects_captured_snapshot_without_post_verify_path_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An after-verification replacement cannot alter the in-memory projection."""

    mutable = _mutable_copy(
        _sealed_campaign(tmp_path / "sealed-campaign"),
        tmp_path / "mutable-campaign",
    )
    original_loader = routing_package.load_verified_campaign
    replaced = False

    def load_then_replace(
        path: Path,
        **_kwargs: object,
    ) -> routing_package.VerifiedCampaign:
        nonlocal replaced
        verified = original_loader(path, require_immutable=False)
        replacement = path / ".after-verified-replacement"
        replacement.write_bytes(b"{")
        replacement.replace(path / "campaign-plan.json")
        replaced = True
        return verified

    monkeypatch.setattr(routing_dashboard, "load_verified_campaign", load_then_replace)
    index = routing_dashboard.RoutingCampaignDashboardIndex(mutable)
    response = index.refresh()

    assert replaced
    assert response.rejected == ()
    assert response.routing_campaigns[0].campaign_id == "routing-campaign-v1"
    assert (
        index.get_campaign("routing-campaign-v1")
        .trials[0]
        .requests[2]
        .candidates[0]
        .load.admissibility
        == "INADMISSIBLE"
    )


def test_dashboard_does_not_cache_or_project_a_failed_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _sealed_campaign(tmp_path / "sealed-campaign")

    def reject(*_args: object, **_kwargs: object) -> routing_package.VerifiedCampaign:
        raise VerificationError("test-only verifier failure")

    monkeypatch.setattr(routing_dashboard, "load_verified_campaign", reject)
    index = routing_dashboard.RoutingCampaignDashboardIndex(package)
    response = index.refresh()

    assert response.routing_campaigns == ()
    assert response.rejected[0].code == "VERIFICATION_FAILED"
    assert index._cache_by_digest == {}
    assert index._details == {}
    with pytest.raises(routing_dashboard.DashboardRoutingCampaignNotFound):
        index.get_campaign("routing-campaign-v1")
