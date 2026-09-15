"""Shared typed ceilings for expensive dashboard snapshot construction."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Final

from inferdrome.errors import DashboardError, WorkLimitError
from inferdrome.limits import WorkBudget, WorkLimits

MAX_RUN_ENTRIES: Final = 1_000
MAX_TRIAL_SET_ENTRIES: Final = 200
MAX_COMPARISON_ENTRIES_PER_ROOT: Final = 200
MAX_CONCURRENT_SNAPSHOT_BUILDS: Final = 8
MAX_AGGREGATE_ACTIVE_UNITS: Final = 3_000
MAX_AGGREGATE_ACTIVE_BYTES: Final = 8_589_934_592
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


class DashboardSnapshotBusy(DashboardError):
    """A snapshot slot is occupied; no source work was admitted."""


@dataclass(frozen=True)
class DashboardLimits:
    """Local v0.1 discovery and aggregate verification ceilings."""

    max_run_entries: int = MAX_RUN_ENTRIES
    max_trial_set_entries: int = MAX_TRIAL_SET_ENTRIES
    max_comparison_entries_per_root: int = MAX_COMPARISON_ENTRIES_PER_ROOT
    max_concurrent_snapshot_builds: int = MAX_CONCURRENT_SNAPSHOT_BUILDS
    max_aggregate_active_units: int = MAX_AGGREGATE_ACTIVE_UNITS
    max_aggregate_active_bytes: int = MAX_AGGREGATE_ACTIVE_BYTES
    run_snapshot_work: WorkLimits = DEFAULT_RUN_SNAPSHOT_WORK
    trial_set_snapshot_work: WorkLimits = DEFAULT_TRIAL_SET_SNAPSHOT_WORK
    comparison_snapshot_work: WorkLimits = DEFAULT_COMPARISON_SNAPSHOT_WORK

    def __post_init__(self) -> None:
        values = (
            self.max_run_entries,
            self.max_trial_set_entries,
            self.max_comparison_entries_per_root,
            self.max_concurrent_snapshot_builds,
            self.max_aggregate_active_units,
            self.max_aggregate_active_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("dashboard limits must be positive integers")
        if (
            self.max_run_entries > MAX_RUN_ENTRIES
            or self.max_trial_set_entries > MAX_TRIAL_SET_ENTRIES
            or self.max_comparison_entries_per_root
            > MAX_COMPARISON_ENTRIES_PER_ROOT
            or self.max_concurrent_snapshot_builds
            > MAX_CONCURRENT_SNAPSHOT_BUILDS
            or self.max_aggregate_active_units > MAX_AGGREGATE_ACTIVE_UNITS
            or self.max_aggregate_active_bytes > MAX_AGGREGATE_ACTIVE_BYTES
        ):
            raise ValueError("dashboard limits exceed the v0.1 protocol maxima")

        work_limits = (
            (self.run_snapshot_work, DEFAULT_RUN_SNAPSHOT_WORK),
            (self.trial_set_snapshot_work, DEFAULT_TRIAL_SET_SNAPSHOT_WORK),
            (self.comparison_snapshot_work, DEFAULT_COMPARISON_SNAPSHOT_WORK),
        )
        if any(
            configured.max_units > maximum.max_units
            or configured.max_bytes > maximum.max_bytes
            or configured.max_seconds > maximum.max_seconds
            for configured, maximum in work_limits
        ):
            raise ValueError("dashboard work limits exceed the v0.1 protocol maxima")


class _DashboardWorkBudget(WorkBudget):
    """Charge one operation to its local and shared active-work ceilings."""

    def __init__(
        self,
        limits: WorkLimits,
        *,
        clock: Callable[[], float],
        reserve_active_capacity: Callable[[int, int], None],
        release_active_capacity: Callable[[int, int], None],
    ) -> None:
        super().__init__(limits, clock=clock)
        self._reserve_active_capacity = reserve_active_capacity
        self._release_active_capacity = release_active_capacity
        self._held_active_units = 0
        self._held_active_bytes = 0

    def reserve(self, *, units: int = 0, bytes_: int = 0) -> None:
        previous_units = self.units
        previous_bytes = self.bytes
        super().reserve(units=units, bytes_=bytes_)
        try:
            self._reserve_active_capacity(units, bytes_)
        except WorkLimitError:
            self._units = previous_units
            self._bytes = previous_bytes
            raise
        self._held_active_units += units
        self._held_active_bytes += bytes_

    def close(self) -> None:
        if self._held_active_units or self._held_active_bytes:
            self._release_active_capacity(
                self._held_active_units,
                self._held_active_bytes,
            )
            self._held_active_units = 0
            self._held_active_bytes = 0


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
        self._active_units = 0
        self._active_bytes = 0
        self._active_capacity_lock = Lock()

    def _reserve_active_capacity(self, units: int, bytes_: int) -> None:
        if units == 0 and bytes_ == 0:
            return
        with self._active_capacity_lock:
            if (
                self._active_units + units
                > self.limits.max_aggregate_active_units
                or self._active_bytes + bytes_
                > self.limits.max_aggregate_active_bytes
            ):
                raise WorkLimitError(
                    "dashboard aggregate verification work limit was exceeded"
                )
            self._active_units += units
            self._active_bytes += bytes_

    def _release_active_capacity(self, units: int, bytes_: int) -> None:
        with self._active_capacity_lock:
            self._active_units -= units
            self._active_bytes -= bytes_

    @contextmanager
    def session(self, limits: WorkLimits) -> Iterator[WorkBudget]:
        if not self._build_slots.acquire(blocking=False):
            raise DashboardSnapshotBusy(
                "dashboard snapshot concurrency limit was exceeded"
            )
        try:
            budget = _DashboardWorkBudget(
                limits,
                clock=self._clock,
                reserve_active_capacity=self._reserve_active_capacity,
                release_active_capacity=self._release_active_capacity,
            )
            try:
                yield budget
            finally:
                budget.close()
        finally:
            self._build_slots.release()
