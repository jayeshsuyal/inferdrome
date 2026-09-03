"""Bounded no-shell subprocess execution with exact diagnostic capture."""

import os
import re
import shutil
import signal
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import IO, Final

from inferdrome.errors import AdapterError
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
    TerminationPolicy,
    terminate_bounded,
)

_EXECUTABLE_MAX_BYTES: Final = 17_179_869_184
_TRUSTED_EXECUTABLE_SEARCH_PATH: Final = os.pathsep.join(
    dict.fromkeys(
        (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            *os.defpath.split(os.pathsep),
        )
    )
)
_CREDENTIAL_ASSIGNMENT_PATTERN: Final = re.compile(
    rb"(?i)([\"']?(?:(?:[a-z0-9]+[_-])*(?:api[ _-]?key|access[ _-]?key|"
    rb"access[ _-]?key[ _-]?id|account[ _-]?key|auth[ _-]?config|jwt|"
    rb"connection[ _-]?string|"
    rb"(?:api|access|auth|bearer|refresh|session|id)[ _-]?token|"
    rb"client[ _-]?secret|credentials?|password|passwd|private[ _-]?key|"
    rb"secret(?:[ _-]?key)?)|authorization|proxy-authorization)[\"']?"
    rb"(?:\s*(?:=|:)\s*|\s+))[^\r\n]+"
)
_GENERIC_TOKEN_ASSIGNMENT_PATTERN: Final = re.compile(
    rb"(?i)([\"']?(?:(?:[a-z0-9]+[_-])+token|token)[\"']?"
    rb"(?:\s*(?:=|:)\s*|\s+))(?=[^\r\n,;]{8,}(?:[\r\n,;]|$))[^\r\n,;]+"
)
_URL_CREDENTIAL_PATTERN: Final = re.compile(
    rb"(?i)(https?://)[^\s/@:]+:[^\s/@]+@"
)
_BEARER_CREDENTIAL_PATTERN: Final = re.compile(
    rb"(?i)(\bbearer\s+)[a-z0-9._~+/-]{8,}={0,2}"
)
_QUERY_CREDENTIAL_PATTERN: Final = re.compile(
    rb"(?i)([?&](?:api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|"
    rb"client[_-]?secret|sig|signature|x-amz-(?:credential|signature|security-token)|"
    rb"x-goog-(?:credential|signature))=)[^&#\s]+"
)
_PEM_PRIVATE_KEY_HEADER_PATTERN: Final = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)


class ProcessTermination(StrEnum):
    EXITED = "EXITED"
    CANCELLED = "CANCELLED"
    DEADLINE = "DEADLINE"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    ORPHANED_DESCENDANTS = "ORPHANED_DESCENDANTS"


@dataclass(frozen=True)
class ExecutableIdentity:
    """One validated absolute executable identity reused across process starts."""

    path: Path
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class ProcessCapture:
    argv: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    exit_status: int
    termination: ProcessTermination
    stdout: bytes
    stderr: bytes


ProcessStartObserver = Callable[[int, datetime], None]


def _executable_metadata(path: Path) -> tuple[int, int, int, int, int, int, int]:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise AdapterError("subprocess executable is unavailable") from None
    if (
        path.is_symlink()
        or not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink < 1
        or not 1 <= metadata.st_size <= _EXECUTABLE_MAX_BYTES
        or not os.access(path, os.X_OK)
    ):
        raise AdapterError("subprocess executable is unsafe")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def resolve_executable_identity(
    executable: str,
    *,
    search_path: str | None = None,
) -> ExecutableIdentity:
    """Resolve one executable once and bind its stable filesystem identity."""

    if (
        not isinstance(executable, str)
        or not executable
        or len(executable.encode("utf-8")) > 4_096
        or any(ord(character) < 32 for character in executable)
    ):
        raise AdapterError("subprocess executable name is invalid")
    candidate: str | None
    if Path(executable).is_absolute():
        candidate = executable
    elif os.sep in executable or (os.altsep is not None and os.altsep in executable):
        raise AdapterError("subprocess executable path must be absolute")
    else:
        candidate = shutil.which(
            executable,
            path=(
                _TRUSTED_EXECUTABLE_SEARCH_PATH
                if search_path is None
                else search_path
            ),
        )
    if candidate is None:
        raise AdapterError("subprocess executable is unavailable")
    try:
        path = Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AdapterError("subprocess executable is unavailable") from None
    metadata = _executable_metadata(path)
    return ExecutableIdentity(path, *metadata)


def validate_executable_identity(identity: ExecutableIdentity) -> None:
    """Fail closed if a previously resolved executable identity changed."""

    if not isinstance(identity, ExecutableIdentity):
        raise AdapterError("subprocess executable identity is invalid")
    if _executable_metadata(identity.path) != (
        identity.device,
        identity.inode,
        identity.mode,
        identity.links,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
    ):
        raise AdapterError("subprocess executable identity changed")


def _validate_executable_identity(identity: ExecutableIdentity) -> None:
    """Compatibility alias for the internal call sites in this module."""

    validate_executable_identity(identity)


def _minimal_process_environment(
    identity: ExecutableIdentity,
    cwd: Path,
) -> dict[str, str]:
    environment = {
        name: value
        for name in ("LANG", "LC_ALL")
        if (value := os.environ.get(name)) is not None
        and len(value) <= 4_096
        and "\x00" not in value
    }
    system_path = os.defpath.split(os.pathsep)
    environment["PATH"] = os.pathsep.join(
        dict.fromkeys((str(identity.path.parent), *system_path))
    )
    environment.update(
        {
            "HOME": str(cwd),
            "TMPDIR": str(cwd),
            "XDG_CACHE_HOME": str(cwd / ".cache"),
            "XDG_CONFIG_HOME": str(cwd / ".config"),
        }
    )
    return environment


def redact_subprocess_diagnostics(content: bytes) -> bytes:
    """Redact credential-shaped values before child diagnostics are retained."""

    if _PEM_PRIVATE_KEY_HEADER_PATTERN.search(content) is not None:
        return b"[REDACTED PRIVATE KEY]\n"
    redacted = _URL_CREDENTIAL_PATTERN.sub(rb"\1[REDACTED]@", content)
    redacted = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(rb"\1[REDACTED]", redacted)
    redacted = _GENERIC_TOKEN_ASSIGNMENT_PATTERN.sub(rb"\1[REDACTED]", redacted)
    redacted = _BEARER_CREDENTIAL_PATTERN.sub(rb"\1[REDACTED]", redacted)
    return _QUERY_CREDENTIAL_PATTERN.sub(rb"\1[REDACTED]", redacted)


def diagnostics_contain_credentials(content: bytes) -> bool:
    """Detect credential-shaped output without retaining or echoing its value."""

    return redact_subprocess_diagnostics(content) != content


class _ProcessGroup:
    """Termination adapter for the isolated process group created below."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def kill_remaining(self) -> bool:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        return True

    def _signal(self, selected_signal: signal.Signals) -> None:
        try:
            os.killpg(self.process.pid, selected_signal)
        except ProcessLookupError:
            if self.process.poll() is None:
                self.process.send_signal(selected_signal)


class _BoundedDrain:
    def __init__(
        self,
        stream: IO[bytes],
        *,
        limit: int,
        overflow: threading.Event,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self.content = bytearray()
        self.error: OSError | None = None

    def run(self) -> None:
        try:
            while chunk := self._stream.read(65_536):
                remaining = self._limit - len(self.content)
                if remaining > 0:
                    self.content.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._overflow.set()
        except OSError as error:
            self.error = error
        finally:
            try:
                self._stream.close()
            except OSError as error:
                self.error = self.error or error


def _join_threads(threads: list[threading.Thread], timeout: float) -> bool:
    deadline = monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _validate_process_inputs(
    argv: tuple[str, ...],
    cwd: Path,
    max_runtime_seconds: float,
    output_limit_bytes: int,
    environment: Mapping[str, str] | None,
) -> None:
    if (
        not argv
        or len(argv) > 2_048
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument) > 8_192
            for argument in argv
        )
    ):
        raise AdapterError("subprocess argument vector is invalid")
    if not cwd.is_absolute():
        raise AdapterError("subprocess working directory must be absolute")
    try:
        cwd_stat = cwd.stat(follow_symlinks=False)
    except OSError:
        raise AdapterError("subprocess working directory is unavailable") from None
    if cwd.is_symlink() or not stat.S_ISDIR(cwd_stat.st_mode):
        raise AdapterError("subprocess working directory must be a real directory")
    if (
        isinstance(max_runtime_seconds, bool)
        or not isinstance(max_runtime_seconds, int | float)
        or not isfinite(max_runtime_seconds)
        or not 0 < max_runtime_seconds <= 86_400
    ):
        raise AdapterError("subprocess runtime limit is invalid")
    if (
        isinstance(output_limit_bytes, bool)
        or not isinstance(output_limit_bytes, int)
        or not 1 <= output_limit_bytes <= 268_435_456
    ):
        raise AdapterError("subprocess output limit is invalid")
    if environment is not None and any(
        not isinstance(key, str)
        or not key
        or "=" in key
        or "\x00" in key
        or not isinstance(value, str)
        or "\x00" in value
        for key, value in environment.items()
    ):
        raise AdapterError("subprocess environment is invalid")


def run_captured_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    max_runtime_seconds: float,
    output_limit_bytes: int = 16_777_216,
    cancellation: CancellationToken | None = None,
    termination_policy: TerminationPolicy | None = None,
    environment: Mapping[str, str] | None = None,
    merge_stderr: bool = False,
    on_start: ProcessStartObserver | None = None,
    executable_identity: ExecutableIdentity | None = None,
) -> ProcessCapture:
    """Run one process without a shell and bound time plus captured output."""

    _validate_process_inputs(
        argv,
        cwd,
        max_runtime_seconds,
        output_limit_bytes,
        environment,
    )
    selected_cancellation = cancellation or CancellationToken()
    selected_cancellation.raise_if_requested()
    selected_policy = termination_policy or TerminationPolicy()
    selected_executable = executable_identity or resolve_executable_identity(argv[0])
    _validate_executable_identity(selected_executable)
    selected_environment = _minimal_process_environment(selected_executable, cwd)
    if environment is not None:
        selected_environment.update(environment)
    started_at = datetime.now(UTC)
    started_monotonic = monotonic()
    stderr_target: int = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=selected_environment,
            executable=str(selected_executable.path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            close_fds=True,
            start_new_session=True,
            shell=False,
        )
    except OSError:
        raise AdapterError("subprocess could not be started") from None
    process_group = _ProcessGroup(process)
    if on_start is not None:
        try:
            on_start(process.pid, started_at)
        except Exception:
            terminate_bounded(process_group, selected_policy)
            raise AdapterError("subprocess start observer failed") from None
    if process.stdout is None or (not merge_stderr and process.stderr is None):
        terminate_bounded(process_group, selected_policy)
        raise AdapterError("subprocess capture pipes are unavailable")

    overflow = threading.Event()
    stdout_drain = _BoundedDrain(
        process.stdout,
        limit=output_limit_bytes,
        overflow=overflow,
    )
    drains = [stdout_drain]
    if not merge_stderr:
        if process.stderr is None:
            raise AssertionError
        drains.append(
            _BoundedDrain(
                process.stderr,
                limit=output_limit_bytes,
                overflow=overflow,
            )
        )
    threads = [
        threading.Thread(target=drain.run, daemon=True, name="inferdrome-drain")
        for drain in drains
    ]
    for thread in threads:
        thread.start()

    termination = ProcessTermination.EXITED
    while process.poll() is None:
        if overflow.is_set():
            termination = ProcessTermination.OUTPUT_LIMIT
            terminate_bounded(process_group, selected_policy)
            break
        notice = selected_cancellation.notice
        if notice is not None:
            termination = (
                ProcessTermination.DEADLINE
                if notice.reason is CancellationReason.DEADLINE
                else ProcessTermination.CANCELLED
            )
            terminate_bounded(process_group, selected_policy)
            break
        remaining = max_runtime_seconds - (monotonic() - started_monotonic)
        if remaining <= 0:
            selected_cancellation.request(CancellationReason.DEADLINE)
            termination = ProcessTermination.DEADLINE
            terminate_bounded(process_group, selected_policy)
            break
        selected_cancellation.wait(min(0.05, remaining))

    exit_status = process.wait()
    if not _join_threads(threads, selected_policy.graceful_timeout_seconds):
        killed_descendants = process_group.kill_remaining()
        if (
            killed_descendants
            and termination is ProcessTermination.EXITED
        ):
            termination = ProcessTermination.ORPHANED_DESCENDANTS
        _join_threads(threads, selected_policy.forced_timeout_seconds)
    if any(thread.is_alive() for thread in threads):
        raise AdapterError("subprocess diagnostic capture did not finish")
    if any(drain.error is not None for drain in drains):
        raise AdapterError("subprocess diagnostic capture failed")
    if overflow.is_set() and termination is ProcessTermination.EXITED:
        termination = ProcessTermination.OUTPUT_LIMIT

    ended_at = datetime.now(UTC)
    stdout = bytes(stdout_drain.content)
    stderr = b"" if merge_stderr else bytes(drains[1].content)
    if diagnostics_contain_credentials(stdout) or diagnostics_contain_credentials(
        stderr
    ):
        raise AdapterError("subprocess diagnostics contain credential-shaped output")
    return ProcessCapture(
        argv=argv,
        started_at=started_at,
        ended_at=ended_at,
        exit_status=exit_status,
        termination=termination,
        stdout=stdout,
        stderr=stderr,
    )
