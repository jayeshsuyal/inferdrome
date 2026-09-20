"""Private no-shell supervisor for one exact-owned direct engine process group.

The supervisor remains the process-group leader while its engine child is
running or has unexpectedly exited with workers still present.  That retained
leader lets the controller verify group ownership before TERM/KILL escalation;
the controller never reuses a bare numeric process-group id after the leader
is gone.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import suppress

_MAX_PAYLOAD_BYTES = 32_768
_READY = b"READY\n"
_ERROR = b"ERROR\n"


def _payload() -> tuple[tuple[str, ...], dict[str, str]]:
    raw = sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES + 1)
    if not raw or len(raw) > _MAX_PAYLOAD_BYTES:
        raise ValueError("direct-process supervisor payload is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("direct-process supervisor payload is invalid") from error
    if not isinstance(value, dict) or set(value) != {"argv", "environment"}:
        raise ValueError("direct-process supervisor payload is invalid")
    argv, environment = value["argv"], value["environment"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
        or not isinstance(environment, Mapping)
        or any(
            not isinstance(name, str)
            or not isinstance(item, str)
            or not name
            or "=" in name
            or "\x00" in name
            or "\x00" in item
            for name, item in environment.items()
        )
    ):
        raise ValueError("direct-process supervisor payload is invalid")
    return tuple(argv), dict(environment)


def _has_other_group_members(process_group_id: int) -> bool:
    """Conservatively inspect Linux's isolated session before the leader exits.

    On non-Linux systems, or if procfs cannot be read exactly, retain the
    supervisor so the controller's bounded KILL can safely finish cleanup.
    """

    proc = "/proc"
    if sys.platform != "linux" or not os.path.isdir(proc):
        return True
    try:
        names = os.listdir(proc)
    except OSError:
        return True
    for name in names:
        if not name.isdecimal() or int(name) == os.getpid():
            continue
        try:
            with open(f"{proc}/{name}/stat", encoding="utf-8") as stat_file:
                stat = stat_file.read()
            _head, separator, tail = stat.rpartition(")")
            if not separator:
                return True
            fields = tail.split()
            # proc(5): state, ppid, pgrp, session ... after the final ')'.
            if len(fields) < 4:
                return True
            if int(fields[2]) == process_group_id:
                return True
        except (OSError, ValueError):
            # A concurrent exit or malformed observation cannot prove absence.
            return True
    return False


def _write_status(status: bytes) -> None:
    try:
        sys.stdout.buffer.write(status)
        sys.stdout.buffer.flush()
    except OSError:
        pass


def main() -> int:
    try:
        argv, environment = _payload()
        engine = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
        )
    except Exception:
        _write_status(_ERROR)
        return 70

    termination_requested = False

    def request_termination(_signum: int, _frame: object) -> None:
        nonlocal termination_requested
        termination_requested = True

    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGINT, request_termination)
    _write_status(_READY)
    with suppress(OSError):
        os.close(sys.stdout.fileno())

    while True:
        returncode = engine.poll()
        if (
            termination_requested
            and returncode is not None
            and not _has_other_group_members(os.getpid())
        ):
            return returncode
        time.sleep(0.025)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess.
    raise SystemExit(main())
