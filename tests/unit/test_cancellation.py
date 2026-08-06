"""Cancellation token and bounded termination behavior."""

import subprocess

import pytest

from inferdrome.errors import CancellationRequested, WorkspaceError
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
    TerminationOutcome,
    TerminationPolicy,
    terminate_bounded,
)


class FakeProcess:
    def __init__(self, *, exited: bool = False, graceful: bool = True) -> None:
        self.exited = exited
        self.graceful = graceful
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self.graceful or self.killed:
            self.exited = True
            return 0
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


class UnkillableProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


def test_cancellation_is_first_writer_wins() -> None:
    token = CancellationToken()

    assert token.request(CancellationReason.USER)
    assert not token.request(CancellationReason.DEADLINE)
    assert token.requested
    assert token.notice is not None
    assert token.notice.reason is CancellationReason.USER
    assert token.wait(0)
    with pytest.raises(CancellationRequested, match="USER"):
        token.raise_if_requested()


def test_uncancelled_token_is_nonblocking() -> None:
    token = CancellationToken()
    assert not token.wait(0)
    token.raise_if_requested()


def test_bounded_termination_outcomes() -> None:
    assert (
        terminate_bounded(FakeProcess(exited=True))
        is TerminationOutcome.ALREADY_EXITED
    )

    graceful = FakeProcess()
    assert terminate_bounded(graceful) is TerminationOutcome.GRACEFUL
    assert graceful.terminated and not graceful.killed

    forced = FakeProcess(graceful=False)
    assert terminate_bounded(forced) is TerminationOutcome.FORCED
    assert forced.terminated and forced.killed


def test_unkillable_process_fails_after_bounded_waits() -> None:
    with pytest.raises(WorkspaceError, match="bounded kill"):
        terminate_bounded(
            UnkillableProcess(),
            TerminationPolicy(
                graceful_timeout_seconds=0.01,
                forced_timeout_seconds=0.01,
            ),
        )


@pytest.mark.parametrize(
    ("graceful", "forced"),
    [(0, 1), (1, 0), (-1, 1), (float("inf"), 1), (float("nan"), 1)],
)
def test_termination_policy_requires_positive_bounds(
    graceful: float, forced: float
) -> None:
    with pytest.raises(ValueError, match="positive"):
        TerminationPolicy(
            graceful_timeout_seconds=graceful,
            forced_timeout_seconds=forced,
        )
