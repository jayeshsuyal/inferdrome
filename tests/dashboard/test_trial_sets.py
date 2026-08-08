"""Dashboard trial-set discovery, projection, pagination, and API behavior."""

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.errors import DashboardPaginationError
from inferdrome.trials import create_trial_set


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _two_runs(tmp_path: Path, run_fake_bundle: Any) -> tuple[Path, tuple[str, str]]:
    runs_root = tmp_path / "runs"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)
    return runs_root, run_ids


def test_trial_set_index_and_detail_are_descriptive_only(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root, run_ids = _two_runs(tmp_path, run_fake_bundle)
    trial_sets_root = tmp_path / "trial-sets"
    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Repeated fake treatment",
            hypothesis="Run-level values should remain visible.",
            trial_set_id="trial-set-11111111111111111111111111111111",
        )
        index = DashboardIndex(
            runs_root,
            trial_sets_root=trial_sets_root,
        )

        listing = index.list_trial_sets()
        detail = index.get_trial_set(created.descriptor.trial_set_id)

        assert listing.page.total == 1
        assert listing.trial_sets[0].member_count == 2
        assert detail.design_status == "RETROSPECTIVE"
        assert detail.inference == "DESCRIPTIVE_ONLY"
        assert detail.request_population_policy == "separate_per_run_v1"
        assert detail.summary.environment_status == "CONSISTENT"
        assert [member.run.run_id for member in detail.members] == list(run_ids)
        assert all(item.total_run_count == 2 for item in detail.variations)
        assert all(item.weighting == "equal_per_run" for item in detail.variations)
        assert all(len(item.points) == 2 for item in detail.variations)
        rendered = detail.model_dump_json()
        for forbidden in (
            "winner",
            "statistically significant",
            '"PASS"',
            '"FAIL"',
            '"NOT_PROVEN"',
        ):
            assert forbidden not in rendered
    finally:
        _make_tree_writable(trial_sets_root)


def test_trial_set_api_lists_reads_and_rejects_unknown_identity(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root, run_ids = _two_runs(tmp_path, run_fake_bundle)
    trial_sets_root = tmp_path / "trial-sets"
    trial_set_id = "trial-set-11111111111111111111111111111111"
    try:
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="API trial",
            trial_set_id=trial_set_id,
        )
        client = TestClient(
            create_app(
                DashboardIndex(
                    runs_root,
                    trial_sets_root=trial_sets_root,
                )
            )
        )

        listing = client.get("/api/v1/trial-sets?limit=200")
        detail = client.get(f"/api/v1/trial-sets/{trial_set_id}")
        missing = client.get("/api/v1/trial-sets/not-a-trial-set")

        assert listing.status_code == 200
        assert listing.json()["trial_sets"][0]["trial_set_id"] == trial_set_id
        assert detail.status_code == 200
        assert detail.json()["inference"] == "DESCRIPTIVE_ONLY"
        assert missing.status_code == 404
        assert listing.headers["cache-control"] == "no-store"
    finally:
        _make_tree_writable(trial_sets_root)


def test_trial_set_pagination_is_snapshot_bound(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root, run_ids = _two_runs(tmp_path, run_fake_bundle)
    trial_sets_root = tmp_path / "trial-sets"
    try:
        for digit in ("1", "2"):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title=f"Trial {digit}",
                trial_set_id=f"trial-set-{digit * 32}",
            )
        index = DashboardIndex(
            runs_root,
            trial_sets_root=trial_sets_root,
        )
        first = index.list_trial_sets(limit=1)
        assert first.page.has_more is True
        assert first.page.next_cursor is not None

        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Trial 3",
            trial_set_id=f"trial-set-{'3' * 32}",
        )
        with pytest.raises(DashboardPaginationError, match="stale snapshot"):
            index.list_trial_sets(
                limit=1,
                cursor=first.page.next_cursor,
            )
    finally:
        _make_tree_writable(trial_sets_root)


def test_trial_set_is_withheld_when_a_pinned_member_changes(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root, run_ids = _two_runs(tmp_path, run_fake_bundle)
    trial_sets_root = tmp_path / "trial-sets"
    try:
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Pinned members",
        )
        index = DashboardIndex(
            runs_root,
            trial_sets_root=trial_sets_root,
        )
        assert len(index.list_trial_sets().trial_sets) == 1

        measurements = (
            runs_root / run_ids[0] / "bundle" / "derived" / "measurements.json"
        )
        measurements.chmod(0o600)
        measurements.write_bytes(measurements.read_bytes() + b" ")
        measurements.chmod(0o400)

        refreshed = index.list_trial_sets()
        assert refreshed.trial_sets == ()
        assert refreshed.rejected[0].code == "MEMBER_UNAVAILABLE"
    finally:
        _make_tree_writable(trial_sets_root)


def test_trial_set_index_rejects_symlinked_declarations(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    outside = tmp_path / "outside"
    runs_root.mkdir()
    trial_sets_root.mkdir()
    outside.mkdir()
    (trial_sets_root / f"trial-set-{'1' * 32}").symlink_to(
        outside,
        target_is_directory=True,
    )

    listing = DashboardIndex(
        runs_root,
        trial_sets_root=trial_sets_root,
    ).list_trial_sets()

    assert listing.trial_sets == ()
    assert listing.rejected[0].code == "UNSAFE_ENTRY"
