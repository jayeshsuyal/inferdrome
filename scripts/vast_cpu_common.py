"""Bounded local command and evidence helpers for the single Vast CPU workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

IMAGE_BASE = (
    "vllm/vllm-openai@sha256:"
    "ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52"
)
TARGET = "ghcr.io/jayeshsuyal/inferdrome-vast-process"
PYTHON = "/opt/inferdrome-runtime/bin/python"
CID = re.compile(r"^[0-9a-f]{64}$")


class Failure(RuntimeError):
    """Fixed diagnostics only; command stderr and credentials are never logged."""


def parse_time(value: str) -> datetime:
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise Failure("INVALID_UTC_DEADLINE") from None
    if result.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise Failure("INVALID_UTC_DEADLINE")
    return result


class Deadline:
    def __init__(self, timestamp: str, max_seconds: float = 7200) -> None:
        self.utc = parse_time(timestamp)
        seconds = (self.utc - datetime.now(UTC)).total_seconds()
        if not 0 < seconds <= max_seconds:
            raise Failure("DEADLINE_EXPIRED_OR_UNBOUNDED")
        self.end = time.monotonic() + seconds
        self.original_timestamp = timestamp

    @property
    def timestamp(self) -> str:
        return self.utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def remaining(self) -> float:
        remaining = min(
            self.end - time.monotonic(),
            (self.utc - datetime.now(UTC)).total_seconds(),
        )
        if remaining <= 0:
            raise Failure("ORIGINAL_DEADLINE_EXPIRED")
        return remaining

    def child(self, seconds: float) -> Deadline:
        if seconds <= 0:
            raise Failure("INVALID_PHASE_BUDGET")
        self.remaining()
        child = object.__new__(Deadline)
        child.utc = min(self.utc, datetime.now(UTC) + timedelta(seconds=seconds))
        child.end = min(self.end, time.monotonic() + seconds)
        child.original_timestamp = self.original_timestamp
        return child


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024 * 1024:
                raise Failure("INVALID_JSON_EVIDENCE")
            data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise Failure("INVALID_JSON_EVIDENCE")
            return json.loads(data)
    except (OSError, ValueError, UnicodeError):
        raise Failure("INVALID_JSON_EVIDENCE") from None


def write_json(path: Path, value: object) -> None:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > 8 * 1024 * 1024:
        raise Failure("EVIDENCE_TOO_LARGE")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def network_bytes() -> dict[str, int]:
    result = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        interface, counters = line.split(":", 1)
        if interface.strip() != "lo":
            result[interface.strip()] = int(counters.split()[8])
    if not result:
        raise Failure("NETWORK_COUNTER_UNAVAILABLE")
    return result


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner:
    def __init__(self, work_root: Path, cleanup: Deadline) -> None:
        self.root, self.cleanup = work_root, cleanup
        self.home = work_root / "command-home"
        self.home.mkdir(mode=0o700, exist_ok=True)
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(work_root),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONFIG": str(self.home / "docker-config"),
        }

    def record_container(self, cid: str) -> None:
        if not isinstance(cid, str) or not CID.fullmatch(cid):
            raise Failure("INVALID_CONTAINER_ID")
        values = self._owned()
        if cid in values:
            raise Failure("DUPLICATE_CONTAINER_ID")
        self._save_owned([*values, cid])

    def forget_container(self, cid: str) -> None:
        if not isinstance(cid, str) or not CID.fullmatch(cid):
            raise Failure("INVALID_CONTAINER_ID")
        values = self._owned()
        self._save_owned([value for value in values if value != cid])

    def _owned(self) -> list[str]:
        path = self.root / "owned-containers.json"
        values = read_json(path) if path.exists() or path.is_symlink() else []
        self._validate_owned(values)
        return values

    @staticmethod
    def _validate_owned(values: object) -> None:
        if (
            not isinstance(values, list)
            or any(
                not isinstance(value, str) or not CID.fullmatch(value)
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise Failure("INVALID_OWNED_CONTAINER_RECORD")

    def _save_owned(self, values: list[str]) -> None:
        self._validate_owned(values)
        temporary = self.root / "owned-containers.next"
        write_json(temporary, values)
        os.replace(temporary, self.root / "owned-containers.json")

    def check_egress(self) -> dict[str, object]:
        path = self.root / "network-start.json"
        if not path.exists():
            return {"observed": False}
        baseline = read_json(path)
        current = network_bytes()
        before = baseline["interfaces"]
        if any(
            name not in current or current[name] < value
            for name, value in before.items()
        ):
            raise Failure("NETWORK_COUNTER_RESET")
        transferred = sum(
            value - before.get(name, 0) for name, value in current.items()
        )
        if transferred > baseline["max_egress_bytes"]:
            raise Failure("EGRESS_OBSERVATION_LIMIT")
        return {"observed": True, "conservative_tx_bytes": transferred}

    def run(
        self,
        argv: Sequence[str],
        *,
        deadline: Deadline,
        input: bytes | None = None,
        limit: int = 1048576,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        deadline.remaining()
        if not argv or limit <= 0 or limit > 64 * 1024 * 1024:
            raise Failure("INVALID_COMMAND_BOUND")
        environment = dict(self.environment if env is None else env)
        productive = deadline.original_timestamp != self.cleanup.original_timestamp
        if productive:
            self.check_egress()
        # Input is staged privately, avoiding pipe backpressure and argv secrets.
        with (
            tempfile.TemporaryFile() as stdin,
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            if input is not None:
                stdin.write(input)
                stdin.seek(0)
            child = subprocess.Popen(
                list(argv),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
            )
            try:
                while child.poll() is None:
                    deadline.remaining()
                    if productive:
                        self.check_egress()
                    if (
                        os.fstat(stdout.fileno()).st_size
                        + os.fstat(stderr.fileno()).st_size
                        > limit
                    ):
                        raise Failure("COMMAND_OUTPUT_LIMIT")
                    time.sleep(min(0.05, deadline.remaining()))
                deadline.remaining()
                if productive:
                    self.check_egress()
                if (
                    os.fstat(stdout.fileno()).st_size
                    + os.fstat(stderr.fileno()).st_size
                    > limit
                ):
                    raise Failure("COMMAND_OUTPUT_LIMIT")
                stdout.seek(0)
                stderr.seek(0)
                captured_stdout = stdout.read(limit + 1)
                captured_stderr = stderr.read(max(0, limit - len(captured_stdout)) + 1)
                if len(captured_stdout) + len(captured_stderr) > limit:
                    raise Failure("COMMAND_OUTPUT_LIMIT")
                result = Result(child.returncode, captured_stdout, captured_stderr)
                if check and result.returncode != 0:
                    raise Failure("COMMAND_FAILED")
                return result
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=min(2, self.cleanup.remaining()))
                    except (OSError, subprocess.TimeoutExpired, Failure):
                        with suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                        with suppress(subprocess.TimeoutExpired, Failure):
                            child.wait(timeout=min(1, self.cleanup.remaining()))
