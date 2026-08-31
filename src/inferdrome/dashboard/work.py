"""Shared typed ceilings for expensive dashboard snapshot construction."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore
from time import monotonic
from typing import Final

from inferdrome.errors import DashboardError
from inferdrome.limits import WorkBudget, WorkLimits

MAX_RUN_ENTRIES: Final = 1_000
MAX_TRIAL_SET_ENTRIES: Final = 200
MAX_COMPARISON_ENTRIES_PER_ROOT: Final = 200
MAX_CONCURRENT_SNAPSHOT_BUILDS: Final = 8
DEFAULT_RUN_SNAPSHOT_WORK: Final = WorkLimits(
    max_units=1_000,
    max_bytes=4_294_967_296,
    max_seconds=120.0,
)
DEFAULT_TRIAL_SET_SNAPSHOT_WORK: Final = WorkLimits(
    max_units=1_000,
    max_bytes=4_294_967_296,
    max_seconds=120.0,
)
DEFAULT_COMPARISON_SNAPSHOT_WORK: Final = WorkLimits(
    max_units=2_000,
    max_bytes=4_294_967_296,
    max_seconds=120.0,
)


@dataclass(frozen=True)
class DashboardLimits:
    """Local v0.1 discovery and aggregate verification ceilings."""

    max_run_entries: int = MAX_RUN_ENTRIES
    max_trial_set_entries: int = MAX_TRIAL_SET_ENTRIES
    max_comparison_entries_per_root: int = MAX_COMPARISON_ENTRIES_PER_ROOT
    max_concurrent_snapshot_builds: int = MAX_CONCURRENT_SNAPSHOT_BUILDS
    run_snapshot_work: WorkLimits = DEFAULT_RUN_SNAPSHOT_WORK
    trial_set_snapshot_work: WorkLimits = DEFAULT_TRIAL_SET_SNAPSHOT_WORK
    comparison_snapshot_work: WorkLimits = DEFAULT_COMPARISON_SNAPSHOT_WORK

    def __post_init__(self) -> None:
        values = (
            self.max_run_entries,
            self.max_trial_set_entries,
            self.max_comparison_entries_per_root,
            self.max_concurrent_snapshot_builds,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("dashboard population limits must be positive integers")
        if (
            self.max_run_entries > MAX_RUN_ENTRIES
            or self.max_trial_set_entries > MAX_TRIAL_SET_ENTRIES
            or self.max_comparison_entries_per_root
            > MAX_COMPARISON_ENTRIES_PER_ROOT
            or self.max_concurrent_snapshot_builds
            > MAX_CONCURRENT_SNAPSHOT_BUILDS
        ):
            raise ValueError("dashboard limits exceed the v0.1 protocol maxima")


class DashboardWorkController:
    """Allow only an explicit number of expensive snapshot builds at once."""

    def __init__(
        self,
        limits: DashboardLimits,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._build_slots = BoundedSemaphore(
            value=limits.max_concurrent_snapshot_builds
        )

    @contextmanager
    def session(self, limits: WorkLimits) -> Iterator[WorkBudget]:
        if not self._build_slots.acquire(blocking=False):
            raise DashboardError("dashboard snapshot concurrency limit was exceeded")
        try:
            yield WorkBudget(limits, clock=self._clock)
        finally:
            self._build_slots.release()
