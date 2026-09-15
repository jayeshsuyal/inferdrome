"""Detached POSIX host cleanup using the existing exact-ID deadline service.

Construction is inert. arm forks only on a single-threaded POSIX main thread.
The child receives the explicit provider in memory, not credentials in argv,
environment, a pipe, or a file. It starts a new session, closes ambient file
descriptors, reopens its journal lock and acknowledges over a private pipe.
No resource is created by this module. It cannot survive host power loss.
"""

from __future__ import annotations

import math
import os
import select
import signal
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from types import FrameType

from inferdrome.deployment.vast_control import (
    UNCONFIRMED,
    UNKNOWN_CREATE,
    ControlFailure,
    ControlIntent,
    ControlJournal,
    DeadlineGuard,
    GuardReady,
    _bounded_call,
    _require_alarm_support,
)
from inferdrome.deployment.vast_provider import VastProvider
from inferdrome.routing_execution.canonical import canonical_json_bytes

_ACK_LIMIT = 4096


class _WorkerDeadline(BaseException):
    """Hard child deadline that ordinary provider retry handlers cannot consume."""


def _hard_call[Result](call: Callable[[], Result], deadline: float) -> Result:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise _WorkerDeadline

    def expired(_number: int, _frame: FrameType | None) -> None:
        raise _WorkerDeadline

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _retain_worker_failure(journal: ControlJournal) -> None:
    journal.record_once("guard-failed.json", {"status": "GUARD_FAILED"})
    instance_id = journal.exact_instance_id()
    if instance_id is None and journal.has("create-start.json"):
        journal.record_once("create-unresolved.json", {"status": UNKNOWN_CREATE})
    elif instance_id is not None and journal.cleanup_status() is None:
        journal.record_once(
            "cleanup-unconfirmed.json",
            {"instance_id": instance_id, "status": UNCONFIRMED},
        )


def _close_ambient_fds(keep: set[int]) -> None:
    # No other Python thread is admitted, so the descriptor inventory cannot
    # race another application thread opening a descriptor before fork.
    for entry in os.listdir("/dev/fd"):
        if entry.isdecimal():
            descriptor = int(entry)
            if descriptor > 2 and descriptor not in keep:
                with suppress(OSError):
                    os.close(descriptor)


def _worker(
    intent: ControlIntent,
    provider: VastProvider,
    root_fd: int,
    acknowledgement: int,
    setup_deadline: float,
) -> None:
    journal: ControlJournal | None = None
    status = 1
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
            signal.signal(number, signal.SIG_IGN)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

        def setup() -> DeadlineGuard:
            nonlocal journal, acknowledgement
            os.setsid()
            os.umask(0o077)
            _close_ambient_fds({root_fd, acknowledgement})
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null, descriptor)
            if null > 2:
                os.close(null)
            os.environ.clear()
            journal = ControlJournal.from_inherited_fd(root_fd)
            os.close(root_fd)
            cleanup_provider = provider.cleanup_for(journal)
            worker = DeadlineGuard(intent, journal, cleanup_provider)
            ready = worker.readiness()
            payload = canonical_json_bytes(ready.model_dump(mode="json"))
            if len(payload) > _ACK_LIMIT or os.write(acknowledgement, payload) != len(
                payload
            ):
                raise ControlFailure("VAST_GUARD_ACK_FAILED")
            os.close(acknowledgement)
            acknowledgement = -1
            return worker

        guard = _hard_call(setup, setup_deadline)
        assert journal is not None
        # Also bound journal-lock waits and other local IO, not just provider
        # callbacks. A wedged controller must not leave the worker unbounded.
        outcome = _hard_call(guard.run, time.monotonic() + guard.cleanup.remaining())
        _bounded_call(
            lambda: journal.record_once("guard-finished.json", {"status": outcome}),
            0.1,
        )
        status = 0
    except BaseException:
        if journal is not None:
            with suppress(BaseException):
                _bounded_call(
                    lambda: _retain_worker_failure(journal),
                    0.1,
                )
    finally:
        if acknowledgement >= 0:
            with suppress(OSError):
                os.close(acknowledgement)
        if journal is not None:
            with suppress(OSError):
                journal.close()
        os._exit(status)


class DetachedGuard:
    """One forked worker; waitpid on our unreaped child is the identity handle.

    A saved PID is never sufficient. Reaping permanently invalidates this
    handle, even if the OS later reuses the number. No destructor stops cleanup.
    The worker ends itself after settlement or its original finite deadline.
    """

    def __init__(self, provider: VastProvider) -> None:
        self._provider = provider
        self._pid: int | None = None
        self._ready: GuardReady | None = None
        self._armed = False

    def arm(
        self, intent: ControlIntent, journal: ControlJournal, *, seconds: float
    ) -> GuardReady:
        _require_alarm_support()
        setup_deadline = time.monotonic() + seconds
        if (
            not hasattr(os, "fork")
            or not hasattr(signal, "pthread_sigmask")
            or signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
            or threading.active_count() != 1
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
            or not math.isfinite(seconds)
            or not 0 < seconds <= 60
            or self._armed
            or journal.has("create-start.json")
        ):
            raise ControlFailure("VAST_GUARD_ARM_REFUSED")
        journal.require_intent(intent)
        self._armed = True
        # A durable launch claim prevents a second launcher renewing protection.
        journal.record("guard-launch.json", {"intent_sha256": intent.intent_sha256})
        reader, writer = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(reader)
            os.close(writer)
            raise ControlFailure("VAST_GUARD_FORK_FAILED") from None
        if pid == 0:
            os.close(reader)
            _worker(intent, self._provider, journal.root.fd, writer, setup_deadline)
            os._exit(1)
        self._pid = pid
        os.close(writer)
        try:
            payload = _bounded_call(lambda: self._read_ack(reader, seconds), seconds)
            ready = GuardReady.model_validate_json(payload)
            if (
                canonical_json_bytes(ready.model_dump(mode="json")) != payload
                or ready.process_id != pid
                or ready.intent_sha256 != intent.intent_sha256
                or ready.journal_device != journal.root.device
                or ready.journal_inode != journal.root.inode
            ):
                raise ControlFailure("VAST_GUARD_ACK_INVALID")
            self._ready = ready
            if not self.is_alive(ready):
                raise ControlFailure("VAST_GUARD_WORKER_EXITED")
            return ready
        except BaseException:
            # No create is possible before arm returns. Kill only our still-held
            # child; never signal a saved PID after it has been reaped.
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGALRM, signal.SIGINT, signal.SIGTERM, signal.SIGCHLD},
            )
            try:
                if self._child_alive():
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                    with suppress(ChildProcessError):
                        os.waitpid(pid, 0)
            finally:
                self._pid = None
                self._ready = None
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            raise ControlFailure("VAST_GUARD_ARM_FAILED") from None
        finally:
            os.close(reader)

    @staticmethod
    def _read_ack(reader: int, seconds: float) -> bytes:
        deadline = time.monotonic() + seconds
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([reader], [], [], remaining)[0]:
                raise ControlFailure("VAST_GUARD_ACK_TIMEOUT")
            chunk = os.read(reader, _ACK_LIMIT + 1 - len(payload))
            if not chunk:
                return bytes(payload)
            payload.extend(chunk)
            if len(payload) > _ACK_LIMIT:
                raise ControlFailure("VAST_GUARD_ACK_TOO_LARGE")

    def _child_alive(self) -> bool:
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            self._pid = None
            return False
        if self._pid is None:
            return False
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._pid = None
            return False
        if pid:
            self._pid = None
            return False
        return True

    def is_alive(self, ready: GuardReady) -> bool:
        return self._ready == ready and self._child_alive()

    def wait(self, *, seconds: float) -> bool:
        """Bounded local join; timeout never cancels an armed cleanup worker."""
        if not math.isfinite(seconds) or not 0 < seconds <= 60:
            raise ControlFailure("VAST_GUARD_WAIT_INVALID")
        deadline = time.monotonic() + seconds
        while self._child_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        return True
