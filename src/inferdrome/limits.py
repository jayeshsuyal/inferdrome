"""Small reusable limits for untrusted populations and bounded verification work."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import isfinite
from time import monotonic

from inferdrome.errors import WorkLimitError


def collect_bounded[T](
    values: Iterable[T],
    *,
    limit: int,
    error: Callable[[], Exception],
) -> tuple[T, ...]:
    """Collect at most ``limit`` values, consuming only the sentinel at limit + 1."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("collection limit must be a non-negative integer")
    collected: list[T] = []
    for value in values:
        if len(collected) == limit:
            raise error()
        collected.append(value)
    return tuple(collected)


@dataclass(frozen=True)
class WorkLimits:
    """Aggregate ceilings for one synchronous, independently bounded operation."""

    max_units: int
    max_bytes: int
    max_seconds: float

    def __post_init__(self) -> None:
        integer_values = (self.max_units, self.max_bytes)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_values
        ) or isinstance(self.max_seconds, bool):
            raise ValueError("work limits must be positive numbers")
        if (
            not isinstance(self.max_seconds, (int, float))
            or not isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("work limits must be positive numbers")


class WorkBudget:
    """Track aggregate bytes, work units, and a cooperative monotonic deadline."""

    def __init__(
        self,
        limits: WorkLimits,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        self._units = 0
        self._bytes = 0

    @property
    def units(self) -> int:
        return self._units

    @property
    def bytes(self) -> int:
        return self._bytes

    def checkpoint(self) -> None:
        """Fail after a bounded unit when the monotonic work window is exhausted."""

        if self._clock() - self._started_at > self.limits.max_seconds:
            raise WorkLimitError("bounded verification time limit was exceeded")

    def reserve(self, *, units: int = 0, bytes_: int = 0) -> None:
        """Reserve work before it begins so an N+1 unit is never started."""

        if (
            isinstance(units, bool)
            or isinstance(bytes_, bool)
            or not isinstance(units, int)
            or not isinstance(bytes_, int)
            or units < 0
            or bytes_ < 0
        ):
            raise ValueError("work reservations must be non-negative integers")
        self.checkpoint()
        if self._units + units > self.limits.max_units:
            raise WorkLimitError("bounded verification work limit was exceeded")
        if self._bytes + bytes_ > self.limits.max_bytes:
            raise WorkLimitError("bounded verification byte limit was exceeded")
        self._units += units
        self._bytes += bytes_
