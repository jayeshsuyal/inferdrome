"""Dashboard discovery and aggregate-work ceilings fail closed."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.dashboard.work import DashboardLimits, DashboardWorkController
from inferdrome.errors import DashboardError
from inferdrome.limits import WorkLimits


def test_dashboard_controller_rejects_a_concurrent_snapshot_build() -> None:
    limits = DashboardLimits(max_concurrent_snapshot_builds=1)
    controller = DashboardWorkController(limits)

    with (
        controller.session(limits.run_snapshot_work),
        pytest.raises(DashboardError, match="concurrency limit"),
        controller.session(limits.run_snapshot_work),
    ):
        raise AssertionError("concurrent work session should not start")


def test_empty_root_still_obeys_the_snapshot_deadline(tmp_path: Path) -> None:
    limits = DashboardLimits(
        run_snapshot_work=WorkLimits(
            max_units=1,
            max_bytes=1,
            max_seconds=0.5,
        )
    )
    times: Iterator[float] = iter((0.0, 1.0))
    index = DashboardIndex(tmp_path / "absent-runs", limits=limits)
    index._work_controller = DashboardWorkController(
        limits,
        clock=lambda: next(times),
    )

    with pytest.raises(DashboardError, match="work limits"):
        index.refresh()


@pytest.mark.parametrize(
    "work_limits",
    [
        WorkLimits(max_units=1, max_bytes=4_294_967_296, max_seconds=120.0),
        WorkLimits(max_units=1_000, max_bytes=1, max_seconds=120.0),
    ],
)
def test_run_snapshot_unit_and_byte_exhaustion_map_to_http_503(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    work_limits: WorkLimits,
) -> None:
    runs_root = tmp_path / "bounded-runs"
    for digit in ("1", "2"):
        run_fake_bundle(runs_root, f"run-{digit * 32}")
    limits = DashboardLimits(run_snapshot_work=work_limits)

    with TestClient(create_app(DashboardIndex(runs_root, limits=limits))) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "dashboard verification work is temporarily unavailable"
    }


def test_run_discovery_rejects_the_limit_plus_one_entry(tmp_path: Path) -> None:
    runs_root = tmp_path / "crowded-runs"
    runs_root.mkdir()
    (runs_root / "a").mkdir()
    (runs_root / "b").mkdir()
    index = DashboardIndex(
        runs_root,
        limits=DashboardLimits(max_run_entries=1),
    )

    with pytest.raises(DashboardError, match="entry limit"):
        index.refresh()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_run_entries": 1_001},
        {"max_trial_set_entries": 201},
        {"max_comparison_entries_per_root": 201},
        {"max_concurrent_snapshot_builds": 9},
    ],
)
def test_dashboard_limits_cannot_exceed_cursor_protocol_maxima(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="protocol maxima"):
        DashboardLimits(**kwargs)  # type: ignore[arg-type]
