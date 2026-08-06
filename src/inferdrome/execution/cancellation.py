"""Cooperative cancellation and bounded process termination scaffolding."""

import subprocess
import threading
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol

from inferdrome.errors import CancellationRequested, WorkspaceError


class CancellationReason(StrEnum):
    USER = "USER"
    SIGNAL = "SIGNAL"
    DEADLINE = "DEADLINE"


@dataclass(frozen=True)
class CancellationNotice:
    reason: CancellationReason


class CancellationToken:
    """Thread-safe first-writer-wins cancellation state."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._notice: CancellationNotice | None = None

    def request(self, reason: CancellationReason) -> bool:
        with self._lock:
            if self._notice is not None:
                return False
            self._notice = CancellationNotice(reason=reason)
            self._event.set()
            return True

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def notice(self) -> CancellationNotice | None:
        with self._lock:
            return self._notice

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._event.wait(timeout_seconds)

    def raise_if_requested(self) -> None:
        notice = self.notice
        if notice is not None:
            raise CancellationRequested(
                f"execution cancellation requested: {notice.reason.value}"
            )


class TerminableProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class TerminationPolicy:
    graceful_timeout_seconds: float = 5.0
    forced_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        bounds = (self.graceful_timeout_seconds, self.forced_timeout_seconds)
        if any(
            isinstance(value, bool) or not isfinite(value) or value <= 0
            for value in bounds
        ):
            raise ValueError("termination timeouts must be positive")


class TerminationOutcome(StrEnum):
    ALREADY_EXITED = "ALREADY_EXITED"
    GRACEFUL = "GRACEFUL"
    FORCED = "FORCED"


def terminate_bounded(
    process: TerminableProcess,
    policy: TerminationPolicy | None = None,
) -> TerminationOutcome:
    """Terminate, then kill, while bounding both waits."""

    selected_policy = policy or TerminationPolicy()
    if process.poll() is not None:
        return TerminationOutcome.ALREADY_EXITED
    process.terminate()
    try:
        process.wait(timeout=selected_policy.graceful_timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=selected_policy.forced_timeout_seconds)
        except subprocess.TimeoutExpired:
            raise WorkspaceError("process did not exit after bounded kill") from None
        return TerminationOutcome.FORCED
    return TerminationOutcome.GRACEFUL
