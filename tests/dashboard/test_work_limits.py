"""Dashboard discovery and aggregate-work ceilings fail closed."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.dashboard.work import DashboardLimits, DashboardWorkController
from inferdrome.errors import DashboardError, WorkLimitError
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


def test_default_controller_admits_eight_lightweight_builds_but_not_nine() -> None:
    limits = DashboardLimits()
    controller = DashboardWorkController(limits)

    with ExitStack() as stack:
        for _ in range(8):
            stack.enter_context(controller.session(limits.run_snapshot_work))
        with pytest.raises(DashboardError, match="concurrency limit"):
            stack.enter_context(controller.session(limits.run_snapshot_work))


def test_dashboard_controller_caps_aggregate_work_across_active_builds() -> None:
    limits = DashboardLimits(
        max_concurrent_snapshot_builds=2,
        max_aggregate_active_units=10,
        max_aggregate_active_bytes=10,
    )
    controller = DashboardWorkController(limits)

    with controller.session(limits.run_snapshot_work) as first:
        first.reserve(units=6, bytes_=6)
        with controller.session(limits.run_snapshot_work) as second:
            with pytest.raises(WorkLimitError, match="aggregate verification work"):
                second.reserve(units=5, bytes_=5)
            assert second.units == 0
            assert second.bytes == 0
            second.reserve(units=4, bytes_=4)

    with controller.session(limits.run_snapshot_work) as after_release:
        after_release.reserve(units=10, bytes_=10)


def test_default_envelope_admits_the_largest_intended_page_load() -> None:
    limits = DashboardLimits()
    controller = DashboardWorkController(limits)

    with (
        controller.session(limits.run_snapshot_work) as run_budget,
        controller.session(limits.comparison_snapshot_work) as comparison_budget,
    ):
        run_budget.reserve(
            units=limits.run_snapshot_work.max_units,
            bytes_=limits.run_snapshot_work.max_bytes,
        )
        comparison_budget.reserve(
            units=limits.comparison_snapshot_work.max_units,
            bytes_=limits.comparison_snapshot_work.max_bytes,
        )


def test_aggregate_capacity_is_released_after_an_exception() -> None:
    limits = DashboardLimits(
        max_aggregate_active_units=10,
        max_aggregate_active_bytes=10,
    )
    controller = DashboardWorkController(limits)

    with (
        pytest.raises(RuntimeError, match="sentinel"),
        controller.session(limits.run_snapshot_work) as budget,
    ):
        budget.reserve(units=10, bytes_=10)
        raise RuntimeError("sentinel")

    with controller.session(limits.run_snapshot_work) as after_failure:
        after_failure.reserve(units=10, bytes_=10)


def test_session_releases_its_slot_when_budget_construction_fails() -> None:
    limits = DashboardLimits(max_concurrent_snapshot_builds=1)
    clock_calls = 0

    def failing_once_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            raise RuntimeError("clock sentinel")
        return 0.0

    controller = DashboardWorkController(limits, clock=failing_once_clock)

    with (
        pytest.raises(RuntimeError, match="clock sentinel"),
        controller.session(limits.run_snapshot_work),
    ):
        raise AssertionError("budget construction should fail")

    with controller.session(limits.run_snapshot_work):
        pass


def test_failed_local_reservation_does_not_consume_shared_capacity() -> None:
    limits = DashboardLimits(
        max_aggregate_active_units=10,
        max_aggregate_active_bytes=10,
    )
    controller = DashboardWorkController(limits)
    tiny = WorkLimits(max_units=1, max_bytes=10, max_seconds=120.0)

    with controller.session(tiny) as failed:
        with pytest.raises(WorkLimitError, match="verification work limit"):
            failed.reserve(units=2, bytes_=10)
        with controller.session(limits.run_snapshot_work) as full:
            full.reserve(units=10, bytes_=10)


def test_simultaneous_reservations_cannot_overdraw_the_shared_envelope() -> None:
    limits = DashboardLimits(
        max_concurrent_snapshot_builds=2,
        max_aggregate_active_units=1,
        max_aggregate_active_bytes=1,
    )
    controller = DashboardWorkController(limits)
    ready = Barrier(2)
    attempted = Barrier(2)
    results: list[str] = []

    def reserve() -> None:
        with controller.session(limits.run_snapshot_work) as budget:
            ready.wait(timeout=5)
            try:
                budget.reserve(units=1, bytes_=1)
            except WorkLimitError:
                results.append("rejected")
            else:
                results.append("admitted")
            attempted.wait(timeout=5)

    threads = (Thread(target=reserve), Thread(target=reserve))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["admitted", "rejected"]


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


def test_shared_exhaustion_returns_503_without_replacing_snapshot(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
) -> None:
    runs_root = tmp_path / "bounded-runs"
    run_fake_bundle(runs_root, "run-11111111111111111111111111111111")
    limits = DashboardLimits()
    index = DashboardIndex(runs_root, limits=limits)
    index.refresh()
    published = index._snapshot

    with (
        index._work_controller.session(limits.run_snapshot_work) as run_budget,
        index._work_controller.session(
            limits.comparison_snapshot_work
        ) as comparison_budget,
    ):
        run_budget.reserve(
            units=limits.run_snapshot_work.max_units,
            bytes_=limits.run_snapshot_work.max_bytes,
        )
        comparison_budget.reserve(
            units=limits.comparison_snapshot_work.max_units,
            bytes_=limits.comparison_snapshot_work.max_bytes,
        )
        with TestClient(create_app(index)) as client:
            response = client.get("/api/v1/runs")

    assert response.status_code == 503
    assert index._snapshot is published


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
        {"max_aggregate_active_units": 3_001},
        {"max_aggregate_active_bytes": 8_589_934_593},
    ],
)
def test_dashboard_limits_cannot_exceed_cursor_protocol_maxima(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="protocol maxima"):
        DashboardLimits(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "work_limits",
    [
        WorkLimits(max_units=1_001, max_bytes=4_294_967_296, max_seconds=120.0),
        WorkLimits(max_units=1_000, max_bytes=4_294_967_297, max_seconds=120.0),
        WorkLimits(max_units=1_000, max_bytes=4_294_967_296, max_seconds=120.1),
    ],
)
def test_dashboard_operation_limits_cannot_exceed_protocol_maxima(
    work_limits: WorkLimits,
) -> None:
    with pytest.raises(ValueError, match="work limits exceed"):
        DashboardLimits(run_snapshot_work=work_limits)
