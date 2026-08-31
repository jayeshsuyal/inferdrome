#!/usr/bin/env python3
"""Run the pinned Inferdrome proof pack on one operator-provided SSH host."""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from inferdrome.errors import AdapterError
from inferdrome.execution.subprocess_runner import (
    ExecutableIdentity,
    redact_subprocess_diagnostics,
    resolve_executable_identity,
)
from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A10_GPU_TIER_ID,
    QWEN3_ARCHIVE_TRANSFER_SECONDS,
    QWEN3_IMPLEMENTED_GPU_TIERS,
    QWEN3_MAX_ARCHIVE_BYTES,
    QWEN3_METADATA_TRANSFER_SECONDS,
    QWEN3_PHASE_BUDGET_SECONDS,
    QWEN3_POST_REMOTE_BUDGET_SECONDS,
    QWEN3_REMOTE_CAPTURE_SECONDS,
    QWEN3_STARTUP_TIMEOUT_SECONDS,
    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS,
    Qwen3GpuTierPolicy,
    qwen3_gpu_tier_policy,
)

if __package__:
    from scripts import (
        lambda_gpu_guard,
        prospective_handoff,
        prospective_real_gpu_capture,
        qwen3_gpu_capture,
        real_gpu_capture,
    )
else:
    import lambda_gpu_guard
    import prospective_handoff
    import prospective_real_gpu_capture
    import qwen3_gpu_capture
    import real_gpu_capture

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DESTINATION_PATTERN = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])\Z"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_INSTANCE_TYPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DEFAULT_REMOTE_TIMEOUT_SECONDS = 9_900
_MINIMUM_PROFILE_REMOTE_SECONDS = 300
_LEGACY_MINIMUM_REMOTE_SECONDS = 1_800
_QWEN3_PROFILE_ID = "managed-vllm-0.26-qwen3-8b-bf16-v1"
_QWEN3_A10_POLICY = qwen3_gpu_tier_policy(QWEN3_A10_GPU_TIER_ID)
_QWEN3_A10_HOURLY_RATE_USD = _QWEN3_A10_POLICY.hourly_rate_usd
_QWEN3_A10_MAX_COST_USD = _QWEN3_A10_POLICY.max_session_cost_usd
_QWEN3_EXPECTED_GPU_MODEL = _QWEN3_A10_POLICY.expected_nvidia_smi_name
_QWEN3_EXPECTED_INSTANCE_TYPE = "gpu_1x_a10"
_QWEN3_STARTUP_TIMEOUT_SECONDS = QWEN3_STARTUP_TIMEOUT_SECONDS
_QWEN3_REMOTE_CAPTURE_SECONDS = QWEN3_REMOTE_CAPTURE_SECONDS
_QWEN3_POST_REMOTE_BUDGET_SECONDS = QWEN3_POST_REMOTE_BUDGET_SECONDS
_QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS = QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
_QWEN3_METADATA_TRANSFER_SECONDS = QWEN3_METADATA_TRANSFER_SECONDS
_QWEN3_ARCHIVE_TRANSFER_SECONDS = QWEN3_ARCHIVE_TRANSFER_SECONDS
_QWEN3_MAX_ARCHIVE_BYTES = QWEN3_MAX_ARCHIVE_BYTES
_MAX_SOURCE_ARCHIVE_BYTES = 134_217_728
_RETAINED_SOURCE_ARCHIVE_NAME = ".inferdrome-source-archive.tar"
_QWEN3_PHASE_BUDGET_SECONDS = dict(QWEN3_PHASE_BUDGET_SECONDS)
_PROSPECTIVE_MAX_ARCHIVE_BYTES = prospective_handoff.handoff_archive_limit()
_PROSPECTIVE_TRANSFER_METADATA_SCHEMA = "inferdrome.prospective-transfer-metadata.v1"
_PROSPECTIVE_REPOSITORY_EXPORT_SCHEMA = "inferdrome.source-tree-export.v1"
_PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES = 268_435_456
_PROSPECTIVE_POST_REMOTE_BUDGET_SECONDS = 300
_PROSPECTIVE_MAX_PATH_DEPTH = 32
_PROSPECTIVE_MAX_IMPLICIT_DIRECTORIES = 1_024
_MAX_PRIVATE_KEY_BLOCK_BYTES = 1_048_576
_MAX_PINNED_KNOWN_HOSTS_BYTES = 1_048_576
_MAX_KEYSCAN_DIAGNOSTIC_BYTES = 8_192
_PRIVATE_KEY_LABELS = (
    b"EC PRIVATE KEY",
    b"ENCRYPTED PRIVATE KEY",
    b"OPENSSH PRIVATE KEY",
    b"PRIVATE KEY",
    b"RSA PRIVATE KEY",
)
_DENIED_SOURCE_NAMES = frozenset(
    {
        ".env",
        ".inferdrome-source-export.json",
        _RETAINED_SOURCE_ARCHIVE_NAME,
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_DENIED_SOURCE_DIRECTORIES = frozenset({".aws", ".git", ".gnupg", ".ssh"})
_DENIED_SOURCE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_EXECUTABLE_IDENTITIES: dict[str, ExecutableIdentity] = {}
_LOCAL_TOOL_SEARCH_PATH = os.pathsep.join(
    (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
)


class RemoteCaptureError(RuntimeError):
    """Expected, user-facing remote capture failure."""


def _raise_rename_error() -> None:
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish one path without replacing an existing destination."""

    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable",
            ) from None
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if (
            rename(
                _AT_FDCWD,
                source_bytes,
                _AT_FDCWD,
                destination_bytes,
                _RENAME_NOREPLACE,
            )
            != 0
        ):
            _raise_rename_error()
        return
    if sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable",
            ) from None
        rename.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if rename(source_bytes, destination_bytes, _RENAME_EXCL) != 0:
            _raise_rename_error()
        return
    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace rename is unsupported on this platform",
    )


def _publish_no_replace(source: Path, destination: Path, *, label: str) -> None:
    try:
        _rename_no_replace(source, destination)
    except FileExistsError:
        raise RemoteCaptureError(f"{label} already exists") from None
    except OSError:
        raise RemoteCaptureError(f"{label} could not be published safely") from None


def _source_path_is_secret_bearing(path: PurePosixPath) -> bool:
    folded_parts = tuple(part.casefold() for part in path.parts)
    folded_name = path.name.casefold()
    return (
        any(part in _DENIED_SOURCE_DIRECTORIES for part in folded_parts)
        or folded_name in _DENIED_SOURCE_NAMES
        or folded_name.startswith(".env.")
        or PurePosixPath(folded_name).suffix in _DENIED_SOURCE_SUFFIXES
    )


def _run(
    arguments: Sequence[str],
    *,
    label: str,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not arguments
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > 65_536
            for argument in arguments
        )
    ):
        raise RemoteCaptureError(f"{label} has an invalid argument vector")
    try:
        executable = _local_executable(arguments[0])
    except AdapterError:
        raise RemoteCaptureError(f"{label} executable is unavailable") from None
    try:
        completed = subprocess.run(
            list(arguments),
            executable=str(executable.path),
            env=_local_child_environment(executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(f"{label} could not complete") from None
    stdout = (
        redact_subprocess_diagnostics(completed.stdout)
        if completed.stdout is not None
        else None
    )
    stderr = (
        redact_subprocess_diagnostics(completed.stderr)
        if completed.stderr is not None
        else None
    )
    completed = subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout,
        stderr,
    )
    if check and completed.returncode != 0:
        detail = ""
        if capture_output and completed.stderr:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
        suffix = f": {detail}" if detail else ""
        raise RemoteCaptureError(f"{label} failed{suffix}")
    return completed


def _kill_isolated_process_group(process: subprocess.Popen[bytes]) -> None:
    """Immediately stop the isolated child group without retaining its output."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        with suppress(ProcessLookupError):
            process.kill()


def _run_bounded_capture(
    arguments: Sequence[str],
    *,
    label: str,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Capture a small local helper result with independent per-stream bounds.

    The process is isolated so either stream exceeding its cap can terminate the
    complete child group before unbounded bytes are retained or interpreted.
    """

    if (
        not arguments
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > 65_536
            for argument in arguments
        )
    ):
        raise RemoteCaptureError(f"{label} has an invalid argument vector")
    if (
        isinstance(stdout_limit, bool)
        or not isinstance(stdout_limit, int)
        or isinstance(stderr_limit, bool)
        or not isinstance(stderr_limit, int)
        or stdout_limit < 1
        or stderr_limit < 1
        or timeout <= 0
    ):
        raise RemoteCaptureError(f"{label} has invalid output bounds")
    try:
        executable = _local_executable(arguments[0])
    except AdapterError:
        raise RemoteCaptureError(f"{label} executable is unavailable") from None

    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    completed_safely = False
    try:
        process = subprocess.Popen(
            list(arguments),
            executable=str(executable.path),
            env=_local_child_environment(executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("capture pipes are unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                content, limit = (
                    (stdout, stdout_limit)
                    if key.data == "stdout"
                    else (stderr, stderr_limit)
                )
                if len(content) + len(chunk) > limit:
                    _kill_isolated_process_group(process)
                    raise RemoteCaptureError(
                        f"{label} exceeded its configured output byte limit"
                    )
                content.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
        captured_stdout = redact_subprocess_diagnostics(bytes(stdout))
        captured_stderr = redact_subprocess_diagnostics(bytes(stderr))
        completed = subprocess.CompletedProcess(
            list(arguments),
            returncode,
            captured_stdout,
            captured_stderr,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            suffix = f": {detail}" if detail else ""
            raise RemoteCaptureError(f"{label} failed{suffix}")
        completed_safely = True
        return completed
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(f"{label} could not complete") from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if not completed_safely:
                _kill_isolated_process_group(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_isolated_process_group(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise RemoteCaptureError(
                        f"{label} cleanup did not complete"
                    ) from None


def _local_executable(name: str) -> ExecutableIdentity:
    """Resolve a local tool once, then reject any identity drift."""

    cached = _EXECUTABLE_IDENTITIES.get(name)
    if cached is None:
        cached = resolve_executable_identity(
            name,
            search_path=_LOCAL_TOOL_SEARCH_PATH,
        )
        _EXECUTABLE_IDENTITIES[name] = cached
        return cached
    observed = resolve_executable_identity(str(cached.path))
    if observed != cached:
        raise AdapterError("local executable identity changed")
    return cached


def _local_child_environment(executable: ExecutableIdentity) -> dict[str, str]:
    """Build a tool-only environment with no cloud or SSH-agent credentials."""

    environment = {
        name: value
        for name in ("LANG", "LC_ALL")
        if (value := os.environ.get(name)) is not None
        and len(value) <= 4_096
        and "\x00" not in value
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    (str(executable.path.parent), *os.defpath.split(os.pathsep))
                )
            ),
        }
    )
    return environment


def _git(*arguments: str) -> str:
    completed = _run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        label="local Git inspection",
        capture_output=True,
        timeout=30,
    )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RemoteCaptureError("local Git output is not UTF-8") from None


def _git_checkout_present() -> bool:
    try:
        result = _run(
            ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--is-inside-work-tree"],
            label="local Git inspection",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except RemoteCaptureError:
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def _source_export_marker() -> dict[str, str]:
    marker_path = REPOSITORY_ROOT / ".inferdrome-source-export.json"
    try:
        content, _identity = prospective_handoff._read_regular_once(
            marker_path,
            label="Inferdrome source export marker",
            maximum_bytes=4_096,
        )
        value = prospective_handoff._strict_json(
            content,
            label="Inferdrome source export marker",
        )
    except (
        prospective_handoff.ProspectiveHandoffError,
        TypeError,
    ):
        raise RemoteCaptureError(
            "Inferdrome source export marker is unavailable or unsafe"
        ) from None
    if not isinstance(value, dict) or set(value) != {
        "repository_commit",
        "schema_version",
        "source_archive_sha256",
        "transport",
    }:
        raise RemoteCaptureError("source export marker has an unexpected shape")
    if value["schema_version"] != _PROSPECTIVE_REPOSITORY_EXPORT_SCHEMA:
        raise RemoteCaptureError("source export marker version is unsupported")
    if value["transport"] != "git-archive-exact-head-tree-v1":
        raise RemoteCaptureError("source export transport is unsupported")
    repository_commit = value["repository_commit"]
    source_archive_sha256 = value["source_archive_sha256"]
    if (
        not isinstance(repository_commit, str)
        or _COMMIT_PATTERN.fullmatch(repository_commit) is None
    ):
        raise RemoteCaptureError("source export commit is invalid")
    if (
        not isinstance(source_archive_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", source_archive_sha256) is None
    ):
        raise RemoteCaptureError("source export digest is invalid")
    return {
        "repository_commit": repository_commit,
        "schema_version": value["schema_version"],
        "source_archive_sha256": source_archive_sha256,
        "transport": value["transport"],
    }


def _verify_exported_tree_matches_archive(archive_bytes: bytes) -> None:
    """Check that the extracted tree still matches the retained Git archive."""

    expected_files: dict[str, tuple[bool, bytes]] = {}
    expected_directories: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as retained:
            members = retained.getmembers()
            if not members or len(members) > 200_000:
                raise RemoteCaptureError("retained source archive has invalid members")
            total_bytes = 0
            for member in members:
                pure = PurePosixPath(member.name)
                normalized = str(pure)
                if member.isdir() and member.name.endswith("/"):
                    normalized_name = member.name[:-1]
                else:
                    normalized_name = member.name
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or "\\" in member.name
                    or normalized != normalized_name
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in member.name
                    )
                    or normalized in expected_files
                    or normalized in expected_directories
                    or _source_path_is_secret_bearing(pure)
                    or not (member.isdir() or member.isfile())
                ):
                    raise RemoteCaptureError(
                        "retained source archive has unsafe members"
                    )
                if member.isdir():
                    expected_directories.add(normalized.rstrip("/"))
                    continue
                total_bytes += member.size
                if total_bytes > _MAX_SOURCE_ARCHIVE_BYTES:
                    raise RemoteCaptureError(
                        "retained source archive expands beyond its limit"
                    )
                stream = retained.extractfile(member)
                if stream is None:
                    raise RemoteCaptureError(
                        "retained source archive member is unreadable"
                    )
                content = stream.read(_MAX_SOURCE_ARCHIVE_BYTES + 1)
                if len(content) != member.size:
                    raise RemoteCaptureError(
                        "retained source archive member is truncated"
                    )
                if _stream_contains_private_key_material(io.BytesIO(content)):
                    raise RemoteCaptureError(
                        "retained source archive contains private-key material"
                    )
                expected_files[normalized] = (bool(member.mode & 0o111), content)
    except (OSError, tarfile.TarError):
        raise RemoteCaptureError("retained source archive is unavailable") from None

    actual_files: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    actual_entry_count = 0
    actual_total_bytes = 0
    pending = [REPOSITORY_ROOT]
    while pending:
        directory = pending.pop()
        try:
            directory_identity = prospective_handoff._identity(os.lstat(directory))
            if not stat.S_ISDIR(directory_identity[2]):
                raise OSError
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise RemoteCaptureError(
                "exported source tree cannot be inspected"
            ) from None
        for child in children:
            actual_entry_count += 1
            if actual_entry_count > 200_000:
                raise RemoteCaptureError("exported source tree has too many entries")
            relative = Path(child.path).relative_to(REPOSITORY_ROOT).as_posix()
            if relative in {
                ".inferdrome-source-export.json",
                _RETAINED_SOURCE_ARCHIVE_NAME,
            }:
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError:
                raise RemoteCaptureError(
                    "exported source tree cannot be inspected"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise RemoteCaptureError("exported source tree contains an unsafe node")
            if stat.S_ISDIR(metadata.st_mode):
                actual_directories.add(relative)
                pending.append(Path(child.path))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise RemoteCaptureError(
                        "exported source tree contains an unsafe node"
                    )
                actual_total_bytes += metadata.st_size
                if actual_total_bytes > _MAX_SOURCE_ARCHIVE_BYTES:
                    raise RemoteCaptureError(
                        "exported source tree exceeds its byte limit"
                    )
                try:
                    content, _identity = prospective_handoff._read_regular_once(
                        Path(child.path),
                        label=f"exported source {relative}",
                        maximum_bytes=_MAX_SOURCE_ARCHIVE_BYTES,
                    )
                except prospective_handoff.ProspectiveHandoffError:
                    raise RemoteCaptureError(
                        "exported source tree changed while it was read"
                    ) from None
                actual_files[relative] = content
            else:
                raise RemoteCaptureError("exported source tree contains an unsafe node")
        try:
            if prospective_handoff._identity(os.lstat(directory)) != directory_identity:
                raise OSError
        except OSError:
            raise RemoteCaptureError(
                "exported source tree changed while it was inspected"
            ) from None
    expected_content = {
        path: content for path, (_executable, content) in expected_files.items()
    }
    if actual_files != expected_content:
        raise RemoteCaptureError("exported source tree disagrees with retained archive")
    for path, (executable, _content) in expected_files.items():
        try:
            actual_mode = os.lstat(REPOSITORY_ROOT / path).st_mode
        except OSError:
            raise RemoteCaptureError(
                "exported source tree cannot be inspected"
            ) from None
        if bool(actual_mode & 0o111) != executable:
            raise RemoteCaptureError(
                "exported source tree mode disagrees with retained archive"
            )
    if actual_directories != expected_directories:
        raise RemoteCaptureError("exported source tree disagrees with retained archive")


def _copy_retained_source_archive(
    destination: Path,
    *,
    expected_archive_sha256: str,
) -> tuple[str, int]:
    """Reuse the verified original Git archive in an exported no-Git tree."""

    if destination.exists() or destination.is_symlink():
        raise RemoteCaptureError("exact source archive destination already exists")
    retained = REPOSITORY_ROOT / _RETAINED_SOURCE_ARCHIVE_NAME
    try:
        content, _identity = prospective_handoff._read_regular_once(
            retained,
            label="retained source archive",
            maximum_bytes=_MAX_SOURCE_ARCHIVE_BYTES,
        )
    except prospective_handoff.ProspectiveHandoffError:
        raise RemoteCaptureError(
            "retained source archive is unavailable or unsafe"
        ) from None
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    if digest != expected_archive_sha256:
        raise RemoteCaptureError(
            "retained source archive disagrees with its independent digest"
        )
    _verify_exported_tree_matches_archive(content)
    try:
        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.source-",
                dir=destination.parent,
            )
        )
        staging = staging_root / "repo.tar"
    except OSError:
        raise RemoteCaptureError("exact source archive could not be staged") from None
    try:
        created_inode = _write_bytes_exclusive(staging, content)
        copied, copied_identity = prospective_handoff._read_regular_once(
            staging,
            label="copied source archive",
            maximum_bytes=_MAX_SOURCE_ARCHIVE_BYTES,
        )
    except (RemoteCaptureError, prospective_handoff.ProspectiveHandoffError):
        raise RemoteCaptureError("exact source archive could not be copied") from None
    if copied != content or copied_identity[:2] != created_inode:
        raise RemoteCaptureError("copied source archive changed")
    _publish_no_replace(
        staging,
        destination,
        label="exact source archive destination",
    )
    try:
        published = os.lstat(destination)
    except OSError:
        raise RemoteCaptureError("copied source archive changed") from None
    if (
        not stat.S_ISREG(published.st_mode)
        or published.st_nlink != 1
        or published.st_size != len(content)
        or prospective_handoff._inode_identity(published) != created_inode
    ):
        raise RemoteCaptureError("copied source archive changed")
    return digest, len(content)


def _stream_git_source_archive(
    destination: Path,
    commit: str,
) -> tuple[int, tuple[int, int], int, str]:
    """Stream Git's exact tar bytes into one bounded, owned descriptor."""

    descriptor: int | None = None
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    successful = False
    try:
        executable = _local_executable("git")
        descriptor = os.open(
            destination,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created_inode = prospective_handoff._inode_identity(os.fstat(descriptor))
        process = subprocess.Popen(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "archive",
                "--format=tar",
                commit,
            ],
            executable=str(executable.path),
            env=_local_child_environment(executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("Git archive pipes were not created")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + 120
        digest = hashlib.sha256()
        received = 0
        stderr = bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("git archive", 120)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired("git archive", 120)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    if len(stderr) < 8_192:
                        stderr.extend(chunk[: 8_192 - len(stderr)])
                    continue
                if received + len(chunk) > _MAX_SOURCE_ARCHIVE_BYTES:
                    raise RemoteCaptureError(
                        "exact source archive exceeds its byte limit"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("Git archive short write")
                    view = view[written:]
                received += len(chunk)
                digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git archive", 120)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            detail = redact_subprocess_diagnostics(bytes(stderr)).decode(
                "utf-8", errors="replace"
            ).strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise RemoteCaptureError(f"exact source archive creation failed{suffix}")
        if not 1 <= received <= _MAX_SOURCE_ARCHIVE_BYTES:
            raise RemoteCaptureError("exact source archive has an invalid size")
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        path_metadata = os.lstat(destination)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != received
            or prospective_handoff._inode_identity(metadata) != created_inode
            or prospective_handoff._inode_identity(path_metadata) != created_inode
            or not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
        ):
            raise RemoteCaptureError(
                "exact source archive changed while it was written"
            )
        successful = True
        return descriptor, created_inode, received, "sha256:" + digest.hexdigest()
    except RemoteCaptureError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(
            "exact source archive creation could not complete"
        ) from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if descriptor is not None and not successful:
            os.close(descriptor)


def _private_key_payload_is_plausible(label: bytes, payload: bytes) -> bool:
    compact = b"".join(payload.split())
    if not 40 <= len(compact) <= _MAX_PRIVATE_KEY_BLOCK_BYTES:
        return False
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return False
    if label == b"OPENSSH PRIVATE KEY":
        return decoded.startswith(b"openssh-key-v1\x00")
    return len(decoded) >= 32 and decoded.startswith(b"\x30")


def _stream_contains_private_key_material(stream: Any) -> bool:
    """Recognize complete, structurally plausible key blocks without marker noise."""

    headers = tuple(
        (b"-----BEGIN " + label + b"-----", label) for label in _PRIVATE_KEY_LABELS
    )
    longest_header = max(len(header) for header, _label in headers)
    buffered = b""
    for chunk in iter(lambda: stream.read(65_536), b""):
        buffered += chunk
        while True:
            candidates = [
                (position, header, label)
                for header, label in headers
                if (position := buffered.find(header)) >= 0
            ]
            if not candidates:
                buffered = buffered[-(longest_header - 1) :]
                break
            position, header, label = min(candidates, key=lambda item: item[0])
            footer = b"-----END " + label + b"-----"
            body_start = position + len(header)
            footer_position = buffered.find(footer, body_start)
            if footer_position < 0:
                buffered = buffered[position:]
                if len(buffered) > _MAX_PRIVATE_KEY_BLOCK_BYTES:
                    return True
                break
            if _private_key_payload_is_plausible(
                label,
                buffered[body_start:footer_position],
            ):
                return True
            buffered = buffered[footer_position + len(footer) :]
    return False


def _create_source_archive(
    destination: Path,
    commit: str,
    *,
    expected_archive_sha256: str | None = None,
) -> tuple[str, int]:
    """Export only the exact HEAD tree, refusing links, submodules, and secrets."""

    if expected_archive_sha256 is not None and re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_archive_sha256
    ) is None:
        raise RemoteCaptureError(
            "expected source archive digest must be tagged lowercase SHA-256"
        )
    if not _git_checkout_present():
        marker = _source_export_marker()
        if marker["repository_commit"] != commit:
            raise RemoteCaptureError("source export commit does not match local HEAD")
        if expected_archive_sha256 is None:
            raise RemoteCaptureError(
                "no-Git source export requires the independent expected archive digest"
            )
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", expected_archive_sha256) is None
            or expected_archive_sha256 != marker["source_archive_sha256"]
        ):
            raise RemoteCaptureError(
                "source export marker does not match the independent archive digest"
            )
        return _copy_retained_source_archive(
            destination, expected_archive_sha256=expected_archive_sha256
        )

    if destination.exists() or destination.is_symlink():
        raise RemoteCaptureError("exact source archive destination already exists")
    listing = _run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-tree",
            "-rz",
            "-r",
            "-l",
            "--full-tree",
            commit,
        ],
        label="exact source tree inspection",
        capture_output=True,
        timeout=60,
    ).stdout
    records = listing.split(b"\0")
    if not records or records[-1] != b"":
        raise RemoteCaptureError("exact source tree listing is malformed")
    seen: set[str] = set()
    file_sizes: dict[str, int] = {}
    for raw in records[:-1]:
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, kind, _object_id, raw_size = metadata.decode("ascii").split(" ", 3)
            path = raw_path.decode("utf-8")
            object_size = int(raw_size)
        except (UnicodeDecodeError, ValueError):
            raise RemoteCaptureError("exact source tree listing is malformed") from None
        pure = PurePosixPath(path)
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or _source_path_is_secret_bearing(pure)
            or path in seen
        ):
            raise RemoteCaptureError(
                "exact source tree contains an unsafe or secret-bearing entry"
            )
        seen.add(path)
        if object_size < 0:
            raise RemoteCaptureError("exact source tree listing has an invalid size")
        file_sizes[path] = object_size
    if not seen or len(seen) > 100_000:
        raise RemoteCaptureError("exact source tree has an invalid file count")
    directory_names: set[str] = set()
    archive_bound = 2 * 512

    def pax_record_bound(path: str) -> int:
        path_bytes = len(path.encode("utf-8"))
        if path_bytes <= 100:
            return 0
        # Git emits a pax extended header when a path does not fit USTAR.
        # Include a deliberately conservative record payload and its header.
        payload = path_bytes + len("path=") + 128
        return 512 + ((payload + 511) // 512) * 512

    for path in seen:
        archive_bound += (
            2 * 512 + pax_record_bound(path) + ((file_sizes[path] + 511) // 512) * 512
        )
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directory_names.add(str(parent))
            parent = parent.parent
    archive_bound += sum(2 * 512 + pax_record_bound(path) for path in directory_names)
    if archive_bound > _MAX_SOURCE_ARCHIVE_BYTES:
        raise RemoteCaptureError("exact source tree exceeds its archive bound")
    try:
        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.source-",
                dir=destination.parent,
            )
        )
        staging = staging_root / "repo.tar"
    except OSError:
        raise RemoteCaptureError("exact source archive could not be staged") from None
    descriptor, created_inode, archive_size, digest = _stream_git_source_archive(
        staging, commit
    )
    try:
        archived_files: set[str] = set()
        archived_members: set[str] = set()
        os.lseek(descriptor, 0, os.SEEK_SET)
        with (
            os.fdopen(os.dup(descriptor), "rb") as archive_stream,
            tarfile.open(fileobj=archive_stream, mode="r:") as retained,
        ):
            members = retained.getmembers()
            if not members or len(members) > 200_000:
                raise RemoteCaptureError("exact source archive has invalid members")
            for member in members:
                pure = PurePosixPath(member.name)
                normalized = str(pure)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or "\\" in member.name
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in member.name
                    )
                    or normalized in archived_members
                    or not (member.isdir() or member.isfile())
                ):
                    raise RemoteCaptureError(
                        "exact source archive contains an unsafe member"
                    )
                archived_members.add(normalized)
                if member.isfile():
                    archived_files.add(normalized)
                    secret_stream = retained.extractfile(member)
                    if secret_stream is None:
                        raise RemoteCaptureError(
                            "exact source archive member cannot be inspected"
                        )
                    if _stream_contains_private_key_material(secret_stream):
                        raise RemoteCaptureError(
                            "exact source tree contains private-key material"
                        )
                    if pure.name == ".gitattributes":
                        stream = retained.extractfile(member)
                        if stream is None:
                            raise RemoteCaptureError(
                                "exact source attributes cannot be inspected"
                            )
                        attributes = stream.read(1_048_577)
                        if (
                            len(attributes) > 1_048_576
                            or b"export-ignore" in attributes
                            or b"export-subst" in attributes
                        ):
                            raise RemoteCaptureError(
                                "exact source attributes alter Git archive bytes"
                            )
        if archived_files != seen:
            raise RemoteCaptureError(
                "exact source archive does not match the committed file set"
            )
        final_digest = hashlib.sha256()
        final_size = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                final_digest.update(chunk)
                final_size += len(chunk)
        metadata = os.fstat(descriptor)
        path_metadata = os.lstat(staging)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != archive_size
            or final_size != archive_size
            or "sha256:" + final_digest.hexdigest() != digest
            or prospective_handoff._inode_identity(metadata) != created_inode
            or prospective_handoff._inode_identity(path_metadata) != created_inode
            or not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
        ):
            raise RemoteCaptureError(
                "exact source archive changed while it was validated"
            )
        if (
            expected_archive_sha256 is not None
            and digest != expected_archive_sha256
        ):
            raise RemoteCaptureError(
                "exact source archive disagrees with its independent digest pin"
            )
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        _publish_no_replace(
            staging,
            destination,
            label="exact source archive destination",
        )
        published = os.lstat(destination)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or published.st_size != archive_size
            or prospective_handoff._inode_identity(published) != created_inode
        ):
            raise RemoteCaptureError(
                "exact source archive changed during publication"
            )
    except (OSError, tarfile.TarError):
        raise RemoteCaptureError("exact source archive is unavailable") from None
    except RemoteCaptureError:
        raise
    finally:
        os.close(descriptor)
    return digest, archive_size


def _validate_destination(value: str) -> str:
    if _DESTINATION_PATTERN.fullmatch(value) is None or value.startswith("-"):
        raise argparse.ArgumentTypeError(
            "SSH destination must be a simple user@host, hostname, IPv4, "
            "or bracketed IPv6"
        )
    return value


def _bounded_integer(
    value: str,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(f"{label} must be an integer")
    selected = int(value)
    if not minimum <= selected <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return selected


def _port(value: str) -> int:
    return _bounded_integer(value, label="SSH port", minimum=1, maximum=65_535)


def _gpu_index(value: str) -> int:
    return _bounded_integer(value, label="GPU index", minimum=0, maximum=255)


def _startup_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="startup timeout",
        minimum=1,
        maximum=3_600,
    )


def _remote_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="remote timeout",
        minimum=_MINIMUM_PROFILE_REMOTE_SECONDS,
        maximum=10_800,
    )


def _lambda_instance_type_name(value: str) -> str:
    if _INSTANCE_TYPE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "Lambda instance type name must contain only letters, digits, '.', '_', "
            "and '-'"
        )
    return value


def _require_checkout(expected_commit: str | None) -> str:
    try:
        _local_executable("git")
    except AdapterError:
        if _git_checkout_present():
            raise RemoteCaptureError("git is required") from None
        marker = _source_export_marker()
        commit = marker["repository_commit"]
        if expected_commit is not None:
            if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
                raise RemoteCaptureError(
                    "--expected-commit must be 40 lowercase hex digits"
                ) from None
            if expected_commit != commit:
                raise RemoteCaptureError(
                    "source export commit is not --expected-commit"
                ) from None
        return commit
    if not _git_checkout_present():
        marker = _source_export_marker()
        commit = marker["repository_commit"]
        if expected_commit is not None:
            if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
                raise RemoteCaptureError(
                    "--expected-commit must be 40 lowercase hex digits"
                )
            if expected_commit != commit:
                raise RemoteCaptureError(
                    "source export commit is not --expected-commit"
                )
        return commit
    commit = _git("rev-parse", "--verify", "HEAD")
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise RemoteCaptureError("local HEAD is not a full lowercase Git commit")
    if expected_commit is not None:
        if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
            raise RemoteCaptureError(
                "--expected-commit must be 40 lowercase hex digits"
            )
        if expected_commit != commit:
            raise RemoteCaptureError("local HEAD is not --expected-commit")
    if _git("status", "--porcelain", "--untracked-files=normal"):
        raise RemoteCaptureError("the local Inferdrome checkout must be clean")
    return commit


def _require_identity(path_text: str | None) -> Path:
    if path_text is None:
        raise RemoteCaptureError("capture requires explicit --identity-file")
    path = Path(path_text).expanduser().absolute()
    try:
        metadata = os.lstat(path)
    except OSError:
        raise RemoteCaptureError("SSH identity file is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RemoteCaptureError("SSH identity must be a regular, non-symlink file")
    return path


def _host_key_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "host-key SHA-256 must be 64 lowercase hex digits"
        )
    return value


def _ssh_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    if identity is None:
        raise RemoteCaptureError("SSH transport requires an explicit identity file")
    options = [
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ForwardAgent=no",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "SendEnv=-*",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "RequestTTY=no",
        "-p",
        str(port),
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
    ]
    return options


def _scp_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    if identity is None:
        raise RemoteCaptureError("SCP transport requires an explicit identity file")
    options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=port,
    )
    port_index = options.index("-p")
    options[port_index] = "-P"
    return options


def _bash_command(script: str) -> str:
    return "bash -c " + shlex.quote(script)


def _remote_preflight_script(
    remote_root: str,
    *,
    gpu_index: int = 0,
    expected_gpu_model: str | None = None,
) -> str:
    quoted_root = shlex.quote(remote_root)
    gpu_check = ""
    resource_check = ""
    if expected_gpu_model is not None:
        gpu_check = f"""
gpu_name=$(nvidia-smi --id={gpu_index} --query-gpu=name --format=csv,noheader,nounits)
[[ $gpu_name == {shlex.quote(expected_gpu_model)} ]] || {{
  echo "expected GPU {expected_gpu_model} at index {gpu_index}" >&2
  echo "observed GPU: $gpu_name" >&2
  exit 1
}}
"""
        resource_check = """if shutil.disk_usage("/tmp").free < 40 * 1024**3:
    raise SystemExit("at least 40 GiB free under /tmp is required")
"""
    return f"""set -euo pipefail
umask 077
[[ $(uname -s) == Linux ]]
for executable in bash curl python3.12 nvidia-smi sha256sum tar timeout; do
  command -v "$executable" >/dev/null || {{
    echo "missing required host executable: $executable" >&2
    exit 1
  }}
done
python3.12 - <<'PY'
import ensurepip
from pathlib import Path
import shutil
import sysconfig
import venv

include = sysconfig.get_path("include")
if not include or not (Path(include) / "Python.h").is_file():
    raise SystemExit("missing Python.h; install the Python 3.12 development headers")
{resource_check}
del ensurepip, venv
PY
[[ ! -e {quoted_root} && ! -L {quoted_root} ]]
mkdir -m 700 -- {quoted_root}
nvidia-smi --query-gpu=index,name,uuid,driver_version --format=csv,noheader,nounits
{gpu_check}
"""


def _remote_capture_script(
    remote_root: str,
    commit: str,
    source_archive_sha256: str,
    *,
    gpu_index: int,
    startup_timeout_seconds: int,
    remote_timeout_seconds: int,
    managed_capability_profile: str | None = None,
    qwen3_gpu_tier: str | None = None,
) -> str:
    if managed_capability_profile not in {None, _QWEN3_PROFILE_ID}:
        raise RemoteCaptureError("managed capability profile is unsupported")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", source_archive_sha256) is None:
        raise RemoteCaptureError("source archive digest is invalid")
    if managed_capability_profile is None:
        if qwen3_gpu_tier is not None:
            raise RemoteCaptureError("Qwen3 GPU tier requires the managed profile")
    else:
        try:
            qwen3_gpu_tier_policy(str(qwen3_gpu_tier))
        except ValueError as error:
            raise RemoteCaptureError(str(error)) from None
    root = shlex.quote(remote_root)
    expected = shlex.quote(commit)
    expected_source_sha256 = shlex.quote(source_archive_sha256.removeprefix("sha256:"))
    profile_argument = ""
    if managed_capability_profile is not None:
        profile_argument = " \\\n  --managed-capability-profile " + shlex.quote(
            managed_capability_profile
        )
        profile_argument += " \\\n  --qwen3-gpu-tier " + shlex.quote(
            str(qwen3_gpu_tier)
        )
    return f"""set -euo pipefail
umask 077
[[ $(sha256sum {root}/repo.tar | cut -d ' ' -f 1) == {expected_source_sha256} ]]
python3.12 - {root}/repo.tar {root}/repo {expected} \
  {shlex.quote(source_archive_sha256)} <<'PY'
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
repository_commit = sys.argv[3]
source_archive_sha256 = sys.argv[4]
metadata = os.lstat(archive)
if archive.is_symlink() or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("source archive is not a regular file")
destination.mkdir(mode=0o700)
with tarfile.open(archive, mode="r:") as retained:
    members = retained.getmembers()
    if not members or len(members) > 100_000:
        raise SystemExit("source archive file count is invalid")
    seen = set()
    total_bytes = 0
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {{"", ".", ".."}} for part in path.parts)
            or "\\\\" in member.name
            or any(ord(character) < 32 for character in member.name)
            or member.name in seen
            or not (member.isdir() or member.isfile())
        ):
            raise SystemExit("source archive contains an unsafe member")
        seen.add(member.name)
        if member.isfile():
            total_bytes += member.size
            if total_bytes > 536_870_912:
                raise SystemExit("source archive expands beyond its limit")
    retained.extractall(destination, members=members, filter="data")
marker = {{
    "repository_commit": repository_commit,
    "schema_version": "inferdrome.source-tree-export.v1",
    "source_archive_sha256": source_archive_sha256,
    "transport": "git-archive-exact-head-tree-v1",
}}
(destination / ".inferdrome-source-export.json").write_text(
    json.dumps(marker, indent=2, sort_keys=True) + "\\n",
    encoding="utf-8",
)
PY
cd {root}/repo
unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES
mkdir -m 700 -- {root}/home
env -i \\
  HOME={root}/home \\
  LANG=C.UTF-8 \\
  LC_ALL=C.UTF-8 \\
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
  timeout --foreground --signal=TERM --kill-after=60s {remote_timeout_seconds}s \\
  ./scripts/run_real_gpu_capture.sh \\
  --state-root {root}/state \\
  --capture-root {root}/capture \\
  --gpu-index {gpu_index} \\
  --startup-timeout-seconds {startup_timeout_seconds}{profile_argument}
"""


def _remote_prospective_capture_script(
    remote_root: str,
    commit: str,
    source_archive_sha256: str,
    handoff: prospective_handoff.HandoffSnapshot,
    *,
    handoff_archive_sha256: str,
    handoff_archive_size: int,
    handoff_manifest_sha256: str,
    workload_sha256: str,
    remote_state_root: str,
    gpu_index: int,
    startup_timeout_seconds: float,
    remote_timeout_seconds: int,
) -> str:
    """Build the prospective remote command from controller-pinned bytes."""

    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise RemoteCaptureError("prospective repository commit is invalid")
    for label, digest in (
        ("source archive", source_archive_sha256),
        ("handoff archive", handoff_archive_sha256),
        ("handoff manifest", handoff_manifest_sha256),
        ("workload", workload_sha256),
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise RemoteCaptureError(f"prospective {label} digest is invalid")
    if not 1 <= handoff_archive_size <= _PROSPECTIVE_MAX_ARCHIVE_BYTES:
        raise RemoteCaptureError("prospective handoff archive size is invalid")
    root = shlex.quote(remote_root)
    state = shlex.quote(remote_state_root)
    prepared_python = shlex.quote(f"{remote_state_root}/venv/bin/python")
    source_digest = shlex.quote(source_archive_sha256.removeprefix("sha256:"))
    handoff_digest = shlex.quote(handoff_archive_sha256.removeprefix("sha256:"))
    pythonpath = shlex.quote(remote_root + "/repo/src:" + remote_root + "/repo")
    case_args = "".join(
        " \\\n  --case "
        + shlex.quote(
            f"{case.case_id}={remote_root}/handoff/{case.source.relative_path}"
        )
        for case in handoff.cases
    )
    contract_args = "".join(
        " \\\n  --expected-contract-digest "
        + shlex.quote(f"{case.case_id}={case.contract_digest}")
        for case in handoff.cases
    )
    wrapper = (
        f"{prepared_python} "
        f"{remote_root}/repo/scripts/prospective_real_gpu_capture.py "
        f"--state-root {state} --output-root {root}/output "
        f"--gpu-index {gpu_index} --startup-timeout-seconds "
        f"{startup_timeout_seconds} --expected-source-archive-sha256 "
        f"{shlex.quote(source_archive_sha256)} --expected-repository-commit "
        f"{shlex.quote(commit)}{case_args}{contract_args}"
    )
    return f"""set -euo pipefail
umask 077
source_size=$(stat -c %s {root}/repo.tar)
(( source_size >= 1 && source_size <= {_MAX_SOURCE_ARCHIVE_BYTES} ))
[[ $(sha256sum {root}/repo.tar | cut -d ' ' -f 1) == {source_digest} ]]
[[ $(stat -c %s {root}/handoff.tar.gz) == {handoff_archive_size} ]]
[[ $(sha256sum {root}/handoff.tar.gz | cut -d ' ' -f 1) == {handoff_digest} ]]
mkdir -m 700 -- {root}/repo {root}/handoff {root}/output {root}/home
tar --extract --file {root}/repo.tar \
  --directory {root}/repo --no-same-owner --no-same-permissions
tar --extract --gzip --file {root}/handoff.tar.gz \
  --directory {root}/handoff --no-same-owner --no-same-permissions
python3.12 - {root}/repo {root}/repo.tar {shlex.quote(commit)} \
  {shlex.quote(source_archive_sha256)} <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

destination = Path(sys.argv[1])
archive = Path(sys.argv[2])
repository_commit = sys.argv[3]
source_archive_sha256 = sys.argv[4]
marker = destination / ".inferdrome-source-export.json"
retained = destination / ".inferdrome-source-archive.tar"
descriptor = os.open(
    archive,
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
retained_descriptor = None

def identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )

def inode_identity(value):
    return value.st_dev, value.st_ino

try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= 134217728
    ):
        raise SystemExit("source archive is not a bounded regular file")
    retained_descriptor = os.open(
        retained,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o400,
    )
    retained_inode = inode_identity(os.fstat(retained_descriptor))
    digest = hashlib.sha256()
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise SystemExit("source archive is truncated")
        digest.update(chunk)
        offset = 0
        while offset < len(chunk):
            written = os.write(retained_descriptor, chunk[offset:])
            if written <= 0:
                raise SystemExit("retained source archive write was truncated")
            offset += written
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if identity(after) != identity(before):
        raise SystemExit("source archive changed while retained")
    if identity(os.lstat(archive)) != identity(before):
        raise SystemExit("source archive path changed while retained")
    if "sha256:" + digest.hexdigest() != source_archive_sha256:
        raise SystemExit("source archive digest changed while retained")
    os.fsync(retained_descriptor)
    retained_after = os.fstat(retained_descriptor)
    retained_path = os.lstat(retained)
    if (
        inode_identity(retained_after) != retained_inode
        or inode_identity(retained_path) != retained_inode
        or not stat.S_ISREG(retained_path.st_mode)
        or retained_path.st_nlink != 1
        or retained_path.st_size != before.st_size
    ):
        raise SystemExit("retained source archive path changed")
finally:
    os.close(descriptor)
    if retained_descriptor is not None:
        os.close(retained_descriptor)
value = json.dumps({{"repository_commit": repository_commit,
    "schema_version": "inferdrome.source-tree-export.v1",
    "source_archive_sha256": source_archive_sha256,
    "transport": "git-archive-exact-head-tree-v1",
}}, indent=2, sort_keys=True).encode("utf-8") + b"\\n"
try:
    descriptor = os.open(
        marker,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o400,
    )
    marker_inode = inode_identity(os.fstat(descriptor))
    with os.fdopen(descriptor, "wb") as stream:
        if stream.write(value) != len(value):
            raise SystemExit("source marker write was truncated")
        stream.flush()
        os.fsync(stream.fileno())
    marker_path = os.lstat(marker)
    if (
        inode_identity(marker_path) != marker_inode
        or not stat.S_ISREG(marker_path.st_mode)
        or marker_path.st_nlink != 1
        or marker_path.st_size != len(value)
    ):
        raise SystemExit("source marker path changed")
except (OSError, SystemExit):
    raise SystemExit("source marker could not be published") from None
PY
PYTHONPATH={pythonpath} python3.12 - {root}/handoff \
  {shlex.quote(handoff_manifest_sha256)} {shlex.quote(workload_sha256)} <<'PY'
from pathlib import Path
import sys
from scripts.prospective_handoff import snapshot_handoff

snapshot_handoff(
    Path(sys.argv[1]),
    expected_manifest_sha256=sys.argv[2],
    expected_workload_sha256=sys.argv[3],
)
PY
[[ -x {state}/venv/bin/python ]]
wrapper_status=0
env -i HOME={root}/home LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONPATH={pythonpath} timeout --foreground --signal=TERM --kill-after=60s \
  {remote_timeout_seconds}s {wrapper} >{root}/wrapper-output.log 2>&1 \
  || wrapper_status=$?
session_path=$(PYTHONPATH={pythonpath} python3.12 - \
  {root}/wrapper-output.log {root}/output <<'PY'
from pathlib import Path
import sys
log, output = Path(sys.argv[1]), Path(sys.argv[2]).resolve(strict=True)
paths = [
    line.removeprefix("receipt_path=")
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
    if line.startswith("receipt_path=")
]
if len(paths) != 1:
    raise SystemExit("prospective wrapper did not emit one receipt path")
selected = Path(paths[0]).resolve(strict=True)
selected.relative_to(output)
print(selected.parent)
PY
)
session_name=$(basename -- "$session_path")
tar -C {root}/output --create --gzip \
  --file {root}/prospective-session.tar.gz -- "$session_name"
session_size=$(stat -c %s {root}/prospective-session.tar.gz)
(( session_size <= {_PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES} ))
session_digest=$(sha256sum {root}/prospective-session.tar.gz | cut -d ' ' -f 1)
python3.12 - {root}/prospective-session.tar.gz \
  {root}/prospective-session.tar.gz.metadata.json \
  "$session_digest" "$session_size" <<'PY'
import json
from pathlib import Path
import sys
metadata = Path(sys.argv[2])
digest = sys.argv[3]
size = sys.argv[4]
metadata.write_text(json.dumps({{"archive_name": "prospective-session.tar.gz",
    "archive_sha256": "sha256:" + digest,
    "schema_version": "{_PROSPECTIVE_TRANSFER_METADATA_SCHEMA}",
    "size_bytes": int(size),
}}, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
PY
python3.12 - {root}/wrapper-output.log <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"), end="")
PY
exit "$wrapper_status"
"""


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
    except OSError:
        raise RemoteCaptureError(f"could not publish {path.name}") from None


def _checksum_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise RemoteCaptureError("remote archive checksum is unreadable") from None
    lines = content.splitlines()
    if len(lines) != 1:
        raise RemoteCaptureError("remote archive checksum has an invalid shape")
    fields = lines[0].split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
        raise RemoteCaptureError("remote archive checksum is invalid")
    if Path(fields[1].lstrip("*")).name != "capture.tar.gz":
        raise RemoteCaptureError("remote checksum names an unexpected archive")
    return "sha256:" + fields[0]


def _read_transfer_metadata(path: Path) -> tuple[int, str]:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise RemoteCaptureError("remote transfer metadata is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 2 <= metadata.st_size <= 4_096
    ):
        raise RemoteCaptureError("remote transfer metadata is unsafe")
    value = _strict_json_bytes(content, label="remote transfer metadata")
    if (
        not isinstance(value, dict)
        or set(value)
        != {"archive_name", "archive_sha256", "schema_version", "size_bytes"}
        or value.get("archive_name") != "capture.tar.gz"
        or value.get("schema_version") != "inferdrome.qwen3-transfer-metadata.v1"
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(value.get("archive_sha256")),
        )
        is None
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or not 1 <= value["size_bytes"] <= _QWEN3_MAX_ARCHIVE_BYTES
    ):
        raise RemoteCaptureError("remote transfer metadata is invalid")
    return value["size_bytes"], value["archive_sha256"]


def _read_prospective_transfer_metadata(path: Path) -> tuple[int, str]:
    try:
        content, identity = prospective_handoff._read_regular_once(
            path,
            label="prospective transfer metadata",
            maximum_bytes=4_096,
        )
        metadata = os.lstat(path)
    except (OSError, prospective_handoff.ProspectiveHandoffError):
        raise RemoteCaptureError(
            "prospective transfer metadata is unavailable"
        ) from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or prospective_handoff._identity(metadata) != identity
        or not 2 <= metadata.st_size <= 4_096
    ):
        raise RemoteCaptureError("prospective transfer metadata is unsafe")
    try:
        value = prospective_handoff._strict_json(
            content, label="prospective transfer metadata"
        )
    except prospective_handoff.ProspectiveHandoffError as error:
        raise RemoteCaptureError(str(error)) from None
    if (
        set(value) != {"archive_name", "archive_sha256", "schema_version", "size_bytes"}
        or value.get("archive_name") != "prospective-session.tar.gz"
        or value.get("schema_version") != _PROSPECTIVE_TRANSFER_METADATA_SCHEMA
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("archive_sha256")))
        is None
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or not 1 <= value["size_bytes"] <= _PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES
    ):
        raise RemoteCaptureError("prospective transfer metadata is invalid")
    return value["size_bytes"], value["archive_sha256"]


def _prepare_pinned_known_hosts(
    *,
    destination: str,
    known_hosts: Path,
    host_key_file: Path | None,
    expected_digest: str,
    port: int,
) -> str:
    """Materialize the operator-pinned known_hosts bytes before SSH connects."""

    expected = "sha256:" + expected_digest
    if host_key_file is not None:
        content, _identity = prospective_handoff._read_regular_once(
            host_key_file,
            label="operator host-key file",
            maximum_bytes=_MAX_PINNED_KNOWN_HOSTS_BYTES,
        )
    else:
        host = _destination_host(destination)
        result = _run_bounded_capture(
            [
                "ssh-keyscan",
                "-T",
                "20",
                "-p",
                str(port),
                host,
            ],
            label="pinned SSH host-key scan",
            stdout_limit=_MAX_PINNED_KNOWN_HOSTS_BYTES,
            stderr_limit=_MAX_KEYSCAN_DIAGNOSTIC_BYTES,
            timeout=30,
        )
        content = result.stdout
    if not content or hashlib.sha256(content).hexdigest() != expected_digest:
        raise RemoteCaptureError("SSH host key bytes do not match their pin")
    _write_bytes_exclusive(known_hosts, content)
    actual = _host_identity_digest(known_hosts)
    if actual != expected:
        raise RemoteCaptureError("SSH host identity does not match its expected digest")
    return actual


def _open_owned_directory(
    root_descriptor: int,
    parts: tuple[str, ...],
    owned_directories: dict[tuple[str, ...], tuple[int, int]],
) -> int:
    """Create/traverse one owned directory chain without following path links."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.dup(root_descriptor)
    walked: tuple[str, ...] = ()
    try:
        for part in parts:
            walked += (part,)
            expected = owned_directories.get(walked)
            if expected is None:
                os.mkdir(part, mode=0o700, dir_fd=current)
                created = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(created.st_mode):
                    raise OSError(errno.ENOTDIR, "created archive directory changed")
                expected = prospective_handoff._inode_identity(created)
            child = os.open(part, flags, dir_fd=current)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or prospective_handoff._inode_identity(metadata) != expected
            ):
                os.close(child)
                raise OSError(errno.ENOTDIR, "archive directory ownership changed")
            owned_directories.setdefault(walked, expected)
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _write_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    parts: tuple[str, ...],
    *,
    root_descriptor: int,
    owned_directories: dict[tuple[str, ...], tuple[int, int]],
) -> None:
    parent = _open_owned_directory(
        root_descriptor,
        parts[:-1],
        owned_directories,
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
            dir_fd=parent,
        )
        created_inode = prospective_handoff._inode_identity(os.fstat(descriptor))
        stream = archive.extractfile(member)
        if stream is None:
            raise OSError(errno.EIO, "archive member cannot be read")
        remaining = member.size
        while remaining:
            chunk = stream.read(min(remaining, 65_536))
            if not chunk:
                raise OSError(errno.EIO, "archive member is truncated")
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short archive member write")
                view = view[written:]
            remaining -= len(chunk)
        if stream.read(1):
            raise OSError(errno.EIO, "archive member exceeds its declared size")
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            parts[-1],
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != member.size
            or prospective_handoff._inode_identity(metadata) != created_inode
            or prospective_handoff._inode_identity(path_metadata) != created_inode
        ):
            raise OSError(errno.EIO, "archive member ownership changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _extract_prospective_archive(archive_bytes: bytes, destination: Path) -> Path:
    """Extract verified bytes privately, then publish with atomic no-replace."""

    selected_destination = destination.absolute()
    if selected_destination.exists() or selected_destination.is_symlink():
        raise RemoteCaptureError("prospective extraction destination already exists")
    if not 1 <= len(archive_bytes) <= _PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES:
        raise RemoteCaptureError("prospective session archive exceeds its limit")
    try:
        parent_metadata = os.lstat(selected_destination.parent)
    except OSError:
        raise RemoteCaptureError(
            "prospective extraction destination parent is unsafe"
        ) from None
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
        parent_metadata.st_mode
    ):
        raise RemoteCaptureError(
            "prospective extraction destination parent is unsafe"
        )

    staging_path: Path | None = None
    root_descriptor: int | None = None
    root_inode: tuple[int, int] | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > 8_192:
                raise ValueError("invalid prospective archive member count")
            normalized: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
            seen: set[tuple[str, ...]] = set()
            explicit_directories: set[tuple[str, ...]] = set()
            file_paths: set[tuple[str, ...]] = set()
            top_levels: set[str] = set()
            total_bytes = 0
            for member in members:
                raw_name = (
                    member.name[:-1]
                    if member.isdir() and member.name.endswith("/")
                    else member.name
                )
                pure = PurePosixPath(raw_name)
                parts = pure.parts
                if (
                    pure.is_absolute()
                    or not parts
                    or pure.as_posix() != raw_name
                    or len(parts) > _PROSPECTIVE_MAX_PATH_DEPTH
                    or any(part in {"", ".", ".."} for part in parts)
                    or any(len(part.encode("utf-8")) > 255 for part in parts)
                    or "\\" in raw_name
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in raw_name
                    )
                    or parts in seen
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError("unsafe prospective archive member")
                seen.add(parts)
                normalized.append((member, parts))
                top_levels.add(parts[0])
                if member.isdir():
                    explicit_directories.add(parts)
                    continue
                if member.size < 0:
                    raise ValueError("prospective archive member size is invalid")
                file_paths.add(parts)
                total_bytes += member.size
                if total_bytes > 1_073_741_824:
                    raise ValueError("prospective archive expands beyond its limit")
            if len(top_levels) != 1:
                raise ValueError("prospective archive has multiple roots")
            required_directories = set(explicit_directories)
            for _member, parts in normalized:
                required_directories.update(
                    parts[:depth] for depth in range(1, len(parts))
                )
            if file_paths & required_directories:
                raise ValueError("prospective archive has a file/directory collision")
            implicit_directories = required_directories - explicit_directories
            if len(implicit_directories) > _PROSPECTIVE_MAX_IMPLICIT_DIRECTORIES:
                raise ValueError(
                    "prospective archive has too many implicit directories"
                )

            staging_path = Path(
                tempfile.mkdtemp(
                    prefix=f".{selected_destination.name}.extract-",
                    dir=selected_destination.parent,
                )
            )
            root_descriptor = os.open(
                staging_path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_metadata = os.fstat(root_descriptor)
            root_inode = prospective_handoff._inode_identity(root_metadata)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or prospective_handoff._inode_identity(os.lstat(staging_path))
                != root_inode
            ):
                raise OSError(errno.ENOTDIR, "private extraction root changed")
            owned_directories = {(): root_inode}
            ordered_directories = sorted(
                required_directories,
                key=lambda value: (len(value), value),
            )
            for parts in ordered_directories:
                descriptor = _open_owned_directory(
                    root_descriptor,
                    parts,
                    owned_directories,
                )
                os.close(descriptor)
            for member, parts in normalized:
                if member.isfile():
                    _write_archive_member(
                        archive,
                        member,
                        parts,
                        root_descriptor=root_descriptor,
                        owned_directories=owned_directories,
                    )
            os.fsync(root_descriptor)
            if (
                prospective_handoff._inode_identity(os.fstat(root_descriptor))
                != root_inode
                or prospective_handoff._inode_identity(os.lstat(staging_path))
                != root_inode
            ):
                raise OSError(errno.ENOTDIR, "private extraction root changed")
            os.close(root_descriptor)
            root_descriptor = None
        if staging_path is None:
            raise OSError(errno.ENOENT, "private extraction root is unavailable")
        _publish_no_replace(
            staging_path,
            selected_destination,
            label="prospective extraction destination",
        )
        staging_path = None
        published = os.lstat(selected_destination)
        if (
            root_inode is None
            or not stat.S_ISDIR(published.st_mode)
            or prospective_handoff._inode_identity(published) != root_inode
        ):
            raise RemoteCaptureError(
                "prospective extraction destination changed during publication"
            )
        return selected_destination / next(iter(top_levels))
    except RemoteCaptureError:
        raise
    except (OSError, tarfile.TarError, ValueError):
        raise RemoteCaptureError("prospective session archive is unsafe") from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _verify_prospective_session(
    session_root: Path,
    handoff: prospective_handoff.HandoffSnapshot,
) -> dict[str, Any]:
    expected = [f"{case.case_id}={case.contract_digest}" for case in handoff.cases]
    try:
        return prospective_real_gpu_capture.verify_session(session_root, expected)
    except (prospective_real_gpu_capture.ProspectiveCaptureError, OSError) as error:
        raise RemoteCaptureError(
            f"retrieved prospective session failed offline verification: {error}"
        ) from None


def _validate_prospective_case_identities(
    raw_cases: object,
) -> list[dict[str, str]]:
    """Validate the three producer identities without making a verdict."""

    if not isinstance(raw_cases, list) or len(raw_cases) != 3:
        raise RemoteCaptureError("prospective capture receipt has an invalid case list")
    identities: list[dict[str, str]] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise RemoteCaptureError("prospective capture case identity is invalid")
        values = {
            "bundle_digest": raw_case.get("bundle_digest"),
            "case_id": raw_case.get("case_id"),
            "request_plan_digest": raw_case.get("request_plan_digest"),
            "run_id": raw_case.get("run_id"),
        }
        if (
            not isinstance(values["case_id"], str)
            or not isinstance(values["run_id"], str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", values["run_id"]) is None
            or any(
                not isinstance(values[field], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", values[field]) is None
                for field in ("bundle_digest", "request_plan_digest")
            )
        ):
            raise RemoteCaptureError("prospective capture case identity is incomplete")
        identities.append({key: str(value) for key, value in values.items()})
    if [item["case_id"] for item in identities] != list(prospective_handoff.CASE_IDS):
        raise RemoteCaptureError("prospective capture cases are not canonical")
    if (
        len({item["run_id"] for item in identities}) != 3
        or len({item["bundle_digest"] for item in identities}) != 3
    ):
        raise RemoteCaptureError("prospective capture case identities are not distinct")
    return identities


def _prospective_session_identities(
    session_root: Path,
) -> tuple[str, list[dict[str, str]]]:
    """Read only the producer identities needed for the transport receipt."""

    try:
        receipt = prospective_real_gpu_capture._read_json(
            session_root / "prospective-capture-receipt.json",
            label="prospective capture receipt",
        )
    except prospective_real_gpu_capture.ProspectiveCaptureError as error:
        raise RemoteCaptureError(str(error)) from None
    identities = _validate_prospective_case_identities(receipt.get("cases"))
    session_id = session_root.name
    if not re.fullmatch(r"prospective-real-gpu-[A-Za-z0-9_.-]{1,128}", session_id):
        raise RemoteCaptureError("prospective session identity is invalid")
    return session_id, identities


def _remote_file_stream_command(
    path: str,
    expected_size: int | None = None,
    *,
    minimum_size: int = 1,
    maximum_size: int | None = None,
) -> str:
    if expected_size is not None:
        minimum_size = expected_size
        maximum_size = expected_size
    if (
        isinstance(minimum_size, bool)
        or not isinstance(minimum_size, int)
        or isinstance(maximum_size, bool)
        or not isinstance(maximum_size, int)
        or not 1 <= minimum_size <= maximum_size <= _QWEN3_MAX_ARCHIVE_BYTES
    ):
        raise RemoteCaptureError("remote file stream bounds are invalid")
    code = """import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
minimum_size = int(sys.argv[2])
maximum_size = int(sys.argv[3])
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not minimum_size <= before.st_size <= maximum_size
    ):
        raise SystemExit("remote file identity is invalid")
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 65_536))
        if not chunk:
            raise SystemExit("remote file was truncated")
        sys.stdout.buffer.write(chunk)
        remaining -= len(chunk)
    sys.stdout.buffer.flush()
    if os.read(descriptor, 1):
        raise SystemExit("remote file grew")
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SystemExit("remote file changed during transfer")
finally:
    os.close(descriptor)
"""
    return shlex.join(
        [
            "python3.12",
            "-c",
            code,
            path,
            str(minimum_size),
            str(maximum_size),
        ]
    )


def _download_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    minimum_size: int,
    maximum_size: int,
    timeout: int,
    expected_sha256: str | None = None,
) -> tuple[int, str]:
    """Stream one remote file without writing more than the local byte bound."""

    if (
        isinstance(minimum_size, bool)
        or not isinstance(minimum_size, int)
        or isinstance(maximum_size, bool)
        or not isinstance(maximum_size, int)
        or not 1 <= minimum_size <= maximum_size <= _QWEN3_MAX_ARCHIVE_BYTES
        or timeout < 1
        or (
            expected_sha256 is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None
        )
    ):
        raise RemoteCaptureError("bounded archive transfer inputs are invalid")
    if destination.exists() or destination.is_symlink():
        raise RemoteCaptureError("bounded archive destination already exists")
    try:
        parent_metadata = os.lstat(destination.parent)
    except OSError:
        raise RemoteCaptureError(
            "bounded archive destination parent is unsafe"
        ) from None
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
        parent_metadata.st_mode
    ):
        raise RemoteCaptureError("bounded archive destination parent is unsafe")
    descriptor: int | None = None
    temporary_path: Path | None = None
    created_inode: tuple[int, int] | None = None
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        try:
            executable = _local_executable(arguments[0])
        except (AdapterError, IndexError):
            raise RemoteCaptureError(
                "bounded archive retrieval executable is unavailable"
            ) from None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.download-",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        created_inode = prospective_handoff._inode_identity(os.fstat(descriptor))
        process = subprocess.Popen(
            list(arguments),
            executable=str(executable.path),
            env=_local_child_environment(executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout
        digest = hashlib.sha256()
        received = 0
        stderr = bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    if len(stderr) < 8_192:
                        stderr.extend(chunk[: 8_192 - len(stderr)])
                    continue
                if received + len(chunk) > maximum_size:
                    raise RemoteCaptureError(
                        "remote file exceeded its maximum byte count"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short bounded archive write")
                    view = view[written:]
                received += len(chunk)
                digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            detail = redact_subprocess_diagnostics(bytes(stderr)).decode(
                "utf-8", errors="replace"
            ).strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise RemoteCaptureError(f"bounded archive retrieval failed{suffix}")
        if not minimum_size <= received <= maximum_size:
            raise RemoteCaptureError("remote file byte count is outside its bounds")
        actual = "sha256:" + digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise RemoteCaptureError("remote archive SHA-256 disagrees")
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            created_inode is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != received
            or prospective_handoff._inode_identity(metadata) != created_inode
            or temporary_path is None
            or prospective_handoff._inode_identity(os.lstat(temporary_path))
            != created_inode
        ):
            raise OSError("bounded archive staging ownership changed")
        os.close(descriptor)
        descriptor = None
        _publish_no_replace(
            temporary_path,
            destination,
            label="bounded archive destination",
        )
        temporary_path = None
        published = os.lstat(destination)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or published.st_size != received
            or prospective_handoff._inode_identity(published) != created_inode
        ):
            raise RemoteCaptureError(
                "bounded archive destination changed during publication"
            )
        return received, actual
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(
            "bounded archive retrieval could not complete"
        ) from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if descriptor is not None:
            os.close(descriptor)


def _download_bounded_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    minimum_size: int,
    maximum_size: int,
    timeout: int,
) -> None:
    _download_remote_file(
        arguments,
        destination,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        timeout=timeout,
    )


def _download_exact_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    timeout: int,
) -> None:
    _download_remote_file(
        arguments,
        destination,
        minimum_size=expected_size,
        maximum_size=expected_size,
        expected_sha256=expected_sha256,
        timeout=timeout,
    )


def _host_identity_digest(known_hosts: Path) -> str:
    try:
        content = known_hosts.read_bytes()
    except OSError:
        raise RemoteCaptureError("SSH host identity record is unavailable") from None
    if not content or len(content) > 1_048_576:
        raise RemoteCaptureError("SSH host identity record is invalid")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _static_check() -> None:
    required = [
        REPOSITORY_ROOT / "scripts" / "prepare_real_gpu_host.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_capture.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_demo.py",
        REPOSITORY_ROOT / "scripts" / "real_gpu_capture.py",
        REPOSITORY_ROOT / "scripts" / "qwen3_gpu_capture.py",
        REPOSITORY_ROOT / "scripts" / "lambda_gpu_guard.py",
        REPOSITORY_ROOT / "scripts" / "prospective_handoff.py",
        REPOSITORY_ROOT / "scripts" / "prospective_real_gpu_capture.py",
    ]
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise RemoteCaptureError(
                f"required capture asset is unavailable: {path.name}"
            )


def _destination_host(destination: str) -> str:
    host = destination.rsplit("@", 1)[-1]
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _lambda_guard_requested(args: argparse.Namespace) -> bool:
    selected = (
        getattr(args, "lambda_instance_id", None),
        getattr(args, "lambda_instance_type_name", None),
        getattr(args, "lambda_hourly_rate_usd", None),
        getattr(args, "max_cost_usd", None),
        getattr(args, "lambda_billing_started_at", None),
    )
    if not any(value is not None for value in selected):
        return False
    if (
        args.lambda_hourly_rate_usd is None
        or args.max_cost_usd is None
        or args.lambda_billing_started_at is None
    ):
        raise RemoteCaptureError(
            "Lambda protection requires --lambda-hourly-rate-usd, --max-cost-usd, "
            "and --lambda-billing-started-at"
        )
    return True


def _qwen3_profile_requested(args: argparse.Namespace) -> bool:
    return getattr(args, "managed_capability_profile", None) == _QWEN3_PROFILE_ID


def _prospective_requested(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "prospective", False)
        or getattr(args, "prospective_handoff_root", None) is not None
    )


def _prospective_digest(value: str) -> str:
    if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("prospective SHA-256 digest is invalid")
    return value if value.startswith("sha256:") else "sha256:" + value


def _tagged_prospective_digest(value: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "prospective source archive digest must be tagged lowercase SHA-256"
        )
    return value


def _bounded_remote_path(value: str) -> str:
    if (
        not value
        or len(value) > 512
        or "\n" in value
        or "\r" in value
        or not value.startswith("/")
        or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise argparse.ArgumentTypeError("remote path must be a bounded absolute path")
    return value


def _qwen3_tier_policy(args: argparse.Namespace) -> Qwen3GpuTierPolicy:
    gpu_tier_id = getattr(args, "qwen3_gpu_tier", None)
    if gpu_tier_id is None:
        raise RemoteCaptureError("Qwen3 capability capture requires --qwen3-gpu-tier")
    try:
        return qwen3_gpu_tier_policy(gpu_tier_id)
    except ValueError as error:
        raise RemoteCaptureError(str(error)) from None


def _validate_capture_mode(args: argparse.Namespace) -> None:
    """Validate one capture mode before any provider or SSH action."""

    if getattr(args, "host_key_sha256", None) is None:
        raise RemoteCaptureError("capture requires explicit --host-key-sha256")
    if getattr(args, "identity_file", None) is None:
        raise RemoteCaptureError("capture requires explicit --identity-file")

    if _prospective_requested(args):
        if (
            getattr(args, "managed_capability_profile", None) is not None
            or getattr(args, "qwen3_gpu_tier", None) is not None
        ):
            raise RemoteCaptureError(
                "prospective capture cannot use the managed Qwen3 profile"
            )
        required = {
            "host_key_sha256": getattr(args, "host_key_sha256", None),
            "prospective_handoff_root": getattr(args, "prospective_handoff_root", None),
            "expected_handoff_manifest_sha256": getattr(
                args, "expected_handoff_manifest_sha256", None
            ),
            "expected_workload_sha256": getattr(args, "expected_workload_sha256", None),
            "expected_commit": getattr(args, "expected_commit", None),
            "expected_source_archive_sha256": getattr(
                args, "expected_source_archive_sha256", None
            ),
            "remote_state_root": getattr(args, "remote_state_root", None),
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise RemoteCaptureError(
                "prospective capture requires explicit " + ", ".join(missing)
            )
        if _COMMIT_PATTERN.fullmatch(str(args.expected_commit)) is None:
            raise RemoteCaptureError(
                "prospective expected commit must be 40 lowercase hex digits"
            )
        if (
            re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(args.expected_source_archive_sha256),
            )
            is None
        ):
            raise RemoteCaptureError(
                "prospective source archive pin must be tagged lowercase SHA-256"
            )
        if args.startup_timeout_seconds < 1 or args.startup_timeout_seconds > 3_600:
            raise RemoteCaptureError("prospective startup timeout is out of bounds")
        if args.remote_timeout_seconds < _MINIMUM_PROFILE_REMOTE_SECONDS:
            raise RemoteCaptureError(
                "prospective remote timeout must be at least 300 seconds"
            )
        guarded = _lambda_guard_requested(args)
        if guarded:
            if args.lambda_instance_id is None:
                raise RemoteCaptureError(
                    "guarded prospective capture requires --lambda-instance-id"
                )
            if args.lambda_instance_type_name is None:
                raise RemoteCaptureError(
                    "guarded prospective capture requires --lambda-instance-type-name"
                )
            if args.lambda_instance_type_name.startswith("gpu_") is False:
                raise RemoteCaptureError(
                    "guarded prospective capture requires a GPU Lambda instance type"
                )
        return

    if any(
        getattr(args, name, None) is not None
        for name in (
            "expected_handoff_manifest_sha256",
            "expected_workload_sha256",
            "expected_source_archive_sha256",
            "remote_state_root",
        )
    ):
        raise RemoteCaptureError("prospective transport options require --prospective")

    profile = getattr(args, "managed_capability_profile", None)
    if profile is None:
        if (
            getattr(args, "qwen3_gpu_tier", None) is not None
            or getattr(args, "lambda_instance_type_name", None) is not None
        ):
            raise RemoteCaptureError(
                "Qwen3 GPU tier and Lambda instance type require the managed profile"
            )
        if args.remote_timeout_seconds < _LEGACY_MINIMUM_REMOTE_SECONDS:
            raise RemoteCaptureError(
                "legacy remote timeout must be at least 1800 seconds"
            )
        return
    if profile != _QWEN3_PROFILE_ID:
        raise RemoteCaptureError("managed capability profile is unsupported")
    policy = _qwen3_tier_policy(args)
    if not _lambda_guard_requested(args):
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the complete Lambda cost guard"
        )
    if args.lambda_instance_id is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires --lambda-instance-id"
        )
    if getattr(args, "lambda_instance_type_name", None) is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires --lambda-instance-type-name"
        )
    if args.startup_timeout_seconds != _QWEN3_STARTUP_TIMEOUT_SECONDS:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the frozen 300-second startup timeout"
        )
    if args.remote_timeout_seconds < _QWEN3_REMOTE_CAPTURE_SECONDS:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires at least 1300 remote seconds"
        )
    if args.lambda_hourly_rate_usd != policy.hourly_rate_usd:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the frozen "
            f"${policy.hourly_rate_usd} hourly rate for {policy.gpu_tier_id}"
        )
    if args.max_cost_usd != policy.max_session_cost_usd:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the exact "
            f"${policy.max_session_cost_usd} session cap for {policy.gpu_tier_id}"
        )
    if sum(_QWEN3_PHASE_BUDGET_SECONDS.values()) > policy.allowed_seconds:
        raise RemoteCaptureError("Qwen3 paid-session phase budget exceeds its cost cap")


def _prospective_source_archive_pin(
    args: argparse.Namespace,
    commit: str,
) -> str:
    expected_commit = getattr(args, "expected_commit", None)
    expected_digest = getattr(args, "expected_source_archive_sha256", None)
    if (
        not isinstance(expected_commit, str)
        or _COMMIT_PATTERN.fullmatch(expected_commit) is None
        or expected_commit != commit
    ):
        raise RemoteCaptureError(
            "prospective expected commit does not match the selected source commit"
        )
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) is None
    ):
        raise RemoteCaptureError(
            "prospective source archive pin must be tagged lowercase SHA-256"
        )
    return expected_digest


def _effective_remote_timeout(
    requested_seconds: int,
    *,
    termination_deadline: datetime | None,
    now: datetime | None = None,
    max_remote_seconds: int = _QWEN3_REMOTE_CAPTURE_SECONDS,
    post_remote_budget_seconds: int = _QWEN3_POST_REMOTE_BUDGET_SECONDS,
) -> int:
    """Clamp remote work so control returns before provider termination."""

    if termination_deadline is None:
        return requested_seconds
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    deadline = termination_deadline.astimezone(UTC)
    available = int((deadline - selected_now).total_seconds())
    bounded = min(
        requested_seconds,
        max_remote_seconds,
        available - post_remote_budget_seconds,
    )
    if bounded < _MINIMUM_PROFILE_REMOTE_SECONDS:
        raise RemoteCaptureError(
            "Lambda termination window leaves less than 300 seconds for remote work"
        )
    return bounded


def _transfer_deadline(
    *,
    termination_deadline: datetime,
    now: datetime | None = None,
    monotonic: float | None = None,
) -> float:
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    available = (
        termination_deadline.astimezone(UTC) - selected_now
    ).total_seconds() - _QWEN3_PHASE_BUDGET_SECONDS["controller_handoff"]
    if available < 1:
        raise RemoteCaptureError("Lambda termination window is exhausted")
    return (time.monotonic() if monotonic is None else monotonic) + available


def _remaining_transfer_timeout(
    deadline: float,
    *,
    phase_limit_seconds: int,
    monotonic: float | None = None,
) -> int:
    remaining = deadline - (time.monotonic() if monotonic is None else monotonic)
    bounded = min(phase_limit_seconds, int(remaining))
    if bounded < 1:
        raise RemoteCaptureError("Lambda retrieval deadline is exhausted")
    return bounded


def _arm_lambda_watchdog(
    args: argparse.Namespace,
) -> lambda_gpu_guard.WatchdogHandle | None:
    if not _lambda_guard_requested(args):
        return None
    host = _destination_host(args.destination)
    reference = args.lambda_instance_id or host
    try:
        handle = lambda_gpu_guard.arm_watchdog(
            reference,
            hourly_rate_usd=args.lambda_hourly_rate_usd,
            max_cost_usd=args.max_cost_usd,
            billing_started_at=args.lambda_billing_started_at,
            state_root=Path(args.lambda_guard_state_root),
            expected_endpoint=host,
            termination_safety_margin_seconds=(
                _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
                if _qwen3_profile_requested(args)
                else lambda_gpu_guard._TERMINATION_SAFETY_MARGIN_SECONDS
            ),
        )
    except lambda_gpu_guard.LambdaGuardError as error:
        raise RemoteCaptureError(
            f"Lambda cost guard could not be armed: {error}"
        ) from None
    if _qwen3_profile_requested(args) or _prospective_requested(args):
        policy = _qwen3_tier_policy(args) if _qwen3_profile_requested(args) else None
        expected_instance_type = args.lambda_instance_type_name
        try:
            active_ids = [
                instance.instance_id
                for instance in handle.client.list_instances()
                if instance.status not in {"terminated", "preempted"}
            ]
            if (
                handle.instance.status != "active"
                or active_ids != [handle.instance.instance_id]
                or handle.instance.instance_type_name != expected_instance_type
            ):
                message = (
                    "Qwen3 campaign requires exactly one active "
                    f"{expected_instance_type} target for {policy.gpu_tier_id}"
                    if policy is not None
                    else "prospective capture requires exactly one active "
                    f"{expected_instance_type} target"
                )
                raise lambda_gpu_guard.LambdaGuardError(message)
        except lambda_gpu_guard.LambdaGuardError:
            label = (
                "Qwen3 single-instance check"
                if policy is not None
                else "prospective single-instance check"
            )
            termination = _terminate_guarded_capture(
                handle,
                capture_failed=True,
                capture_label=label,
                termination_trigger="campaign-single-instance-check-failed",
            )
            if termination is None:
                raise RemoteCaptureError(
                    f"{label} failed; immediate termination was not confirmed and "
                    "explicit unresolved state was retained"
                ) from None
            raise RemoteCaptureError(f"{label} failed; target terminated") from None
    print(
        "Lambda termination watchdog armed for "
        f"{lambda_gpu_guard._timestamp(handle.cost_window.deadline)}."
    )
    print(f"Lambda guard state: {handle.state_directory}")
    return handle


def _dry_run(args: argparse.Namespace, commit: str, identity: Path | None) -> None:
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-<random>"
    guarded = _lambda_guard_requested(args)
    policy = _qwen3_tier_policy(args) if _qwen3_profile_requested(args) else None
    prospective: prospective_handoff.HandoffSnapshot | None = None
    prospective_source_pin = (
        _prospective_source_archive_pin(args, commit)
        if _prospective_requested(args)
        else None
    )
    with tempfile.TemporaryDirectory(prefix="inferdrome-source-tree-") as temporary:
        temporary_root = Path(temporary)
        if _prospective_requested(args):
            prospective = prospective_handoff.snapshot_handoff(
                Path(args.prospective_handoff_root),
                expected_manifest_sha256=args.expected_handoff_manifest_sha256,
                expected_workload_sha256=args.expected_workload_sha256,
            )
            prospective = prospective_handoff.create_handoff_archive(
                prospective,
                temporary_root / "handoff.tar.gz",
            )
            _validate_prospective_snapshot(
                prospective,
                temporary_root / "validated-handoff",
            )
        source_archive_sha256, source_archive_bytes = _create_source_archive(
            temporary_root / "repo.tar",
            commit,
            expected_archive_sha256=prospective_source_pin,
        )
    if _prospective_requested(args) and prospective is None:
        raise AssertionError
    plan = {
        "billing_boundary": (
            "LAMBDA API TERMINATION WATCHDOG PLUS CONTROLLER FINALLY"
            if guarded
            else (
                "OPERATOR_PROVIDED_HOST; NO COST-TERMINATION CLAIM"
                if prospective is not None
                else "OPERATOR MUST TERMINATE THE CLOUD INSTANCE"
            )
        ),
        "destination": args.destination,
        "expected_gpu_model": (
            policy.expected_nvidia_smi_name if policy is not None else None
        ),
        "expected_lambda_instance_type": (
            args.lambda_instance_type_name if policy is not None else None
        ),
        "gpu_index": args.gpu_index,
        "identity_file_configured": identity is not None,
        "lambda_cost_guard": (
            {
                "billing_started_at": lambda_gpu_guard._timestamp(
                    args.lambda_billing_started_at
                ),
                "hourly_rate_usd": str(args.lambda_hourly_rate_usd),
                "instance_reference": args.lambda_instance_id
                or _destination_host(args.destination),
                "max_cost_usd": str(args.max_cost_usd),
            }
            if guarded
            else None
        ),
        "repository_commit": commit,
        "source_archive_bytes": source_archive_bytes,
        "source_archive_sha256": source_archive_sha256,
        "prospective_handoff": (
            {
                "archive_sha256": prospective.archive_sha256,
                "archive_size_bytes": prospective.archive_size_bytes,
                "manifest_sha256": prospective.manifest_sha256,
                "workload_sha256": prospective.workload_sha256,
            }
            if prospective is not None
            else None
        ),
        "managed_capability_profile": args.managed_capability_profile,
        "qwen3_gpu_tier": policy.gpu_tier_id if policy is not None else None,
        "phase_budget_seconds": (
            _QWEN3_PHASE_BUDGET_SECONDS if _qwen3_profile_requested(args) else None
        ),
        "remote_root": remote_root,
        "remote_timeout_seconds": args.remote_timeout_seconds,
        "startup_timeout_seconds": args.startup_timeout_seconds,
        "steps": (
            [
                "snapshot the complete three-case P1 handoff exactly once",
                "create and digest-check distinct source and handoff archives",
                "verify the explicit SSH identity and host-key pin before connecting",
                "preflight the pinned operator-provided NVIDIA host",
                "upload only the bounded source and handoff archives",
                "run three independent prospective cases through the merged wrapper",
                "retrieve and checksum the immutable bounded session archive",
                "offline-verify after the required termination boundary",
                "report CAPTURED_PENDING_EXTERNAL_EXITSPEC and EXTERNAL_ONLY",
            ]
            if prospective is not None
            else [
                "verify a clean exact local commit",
                "validate an exact-HEAD tree archive locally without Git history",
                "arm and validate the independent Lambda termination watchdog",
                "rebuild the checked source archive under watchdog protection",
                "preflight Linux, Python 3.12, NVIDIA, and required host tools",
                "upload and digest-check the exact source archive",
                "prepare the pinned vLLM/model environment",
                (
                    "run one Qwen3-8B concurrency-1 capability spike on "
                    f"{policy.expected_nvidia_smi_name}"
                    if policy is not None
                    else "run the single proof and four-run controlled comparison"
                ),
                (
                    "retrieve bounded size/checksum metadata, then exactly those "
                    "archive bytes"
                ),
                (
                    "terminate and confirm the Lambda instance through its API"
                    if guarded
                    else "prompt the operator to terminate the billable instance"
                ),
                "independently recalculate and verify every retrieved proof artifact",
            ]
        ),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))


def _prospective_transfer_deadline(
    termination_deadline: datetime | None,
) -> float | None:
    if termination_deadline is None:
        return None
    available = (
        int((termination_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        - _PROSPECTIVE_POST_REMOTE_BUDGET_SECONDS
    )
    if available < 1:
        raise RemoteCaptureError("Lambda prospective retrieval deadline is exhausted")
    return time.monotonic() + available


def _prospective_transfer_timeout(
    deadline: float | None,
    phase_limit_seconds: int,
) -> int:
    if deadline is None:
        return phase_limit_seconds
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise RemoteCaptureError("Lambda prospective retrieval deadline is exhausted")
    return min(phase_limit_seconds, remaining)


def _validate_prospective_snapshot(
    snapshot: prospective_handoff.HandoffSnapshot,
    validation_root: Path,
) -> None:
    """Run Inferdrome's inert three-case preflight over captured bytes."""

    validation_root.mkdir(mode=0o700)
    for file in snapshot.files:
        prospective_handoff._write_snapshot_file(validation_root, file)
    case_arguments = [
        f"{case.case_id}={validation_root / case.source.relative_path}"
        for case in snapshot.cases
    ]
    expected_arguments = [
        f"{case.case_id}={case.contract_digest}" for case in snapshot.cases
    ]
    try:
        cases = prospective_real_gpu_capture.validate_prospective_cases(
            case_arguments,
            expected_arguments,
        )
    except prospective_real_gpu_capture.ProspectiveCaptureError as error:
        raise RemoteCaptureError(
            f"captured P1 handoff failed Inferdrome preflight: {error}"
        ) from None
    if [case.case_id for case in cases] != list(prospective_handoff.CASE_IDS):
        raise RemoteCaptureError("captured P1 handoff preflight changed case order")
    if [case.expected_contract_digest for case in cases] != list(
        snapshot.expected_contract_digests
    ):
        raise RemoteCaptureError("captured P1 handoff preflight changed contract links")


def _capture_prospective_over_ssh(
    args: argparse.Namespace,
    commit: str,
    identity: Path,
    source_archive: Path,
    source_archive_sha256: str,
    handoff: prospective_handoff.HandoffSnapshot,
    *,
    termination_deadline: datetime | None = None,
) -> tuple[Path, int]:
    """Transport one immutable P1 snapshot and retrieve its raw archive.

    This phase deliberately does not inspect the retrieved archive.  Guarded
    callers must confirm provider termination before any local extraction or
    session parsing occurs.
    """

    if handoff.archive_path is None or handoff.archive_sha256 is None:
        raise RemoteCaptureError("prospective handoff archive was not prepared")
    if handoff.archive_size_bytes is None:
        raise RemoteCaptureError("prospective handoff archive size is unavailable")
    for path, label, maximum in (
        (source_archive, "source archive", _MAX_SOURCE_ARCHIVE_BYTES),
        (handoff.archive_path, "handoff archive", _PROSPECTIVE_MAX_ARCHIVE_BYTES),
    ):
        try:
            metadata = os.lstat(path)
        except OSError:
            raise RemoteCaptureError(f"{label} is unavailable") from None
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= maximum
        ):
            raise RemoteCaptureError(f"{label} is unsafe")
    try:
        source_bytes, _source_identity = prospective_handoff._read_regular_once(
            source_archive,
            label="source archive",
            maximum_bytes=_MAX_SOURCE_ARCHIVE_BYTES,
        )
        handoff_bytes, _handoff_identity = prospective_handoff._read_regular_once(
            handoff.archive_path,
            label="handoff archive",
            maximum_bytes=_PROSPECTIVE_MAX_ARCHIVE_BYTES,
        )
    except prospective_handoff.ProspectiveHandoffError as error:
        raise RemoteCaptureError(str(error)) from None
    if (
        "sha256:" + hashlib.sha256(source_bytes).hexdigest() != source_archive_sha256
        or len(handoff_bytes) != handoff.archive_size_bytes
        or "sha256:" + hashlib.sha256(handoff_bytes).hexdigest()
        != handoff.archive_sha256
    ):
        raise RemoteCaptureError("prospective transport archive identity changed")

    for executable in ("ssh", "scp"):
        try:
            _local_executable(executable)
        except AdapterError:
            raise RemoteCaptureError(f"{executable} is required") from None
    output_root = Path(args.output_root).expanduser().absolute()
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        output_metadata = os.lstat(output_root)
    except OSError:
        raise RemoteCaptureError(
            "local prospective output root is unavailable"
        ) from None
    if output_root.is_symlink() or not stat.S_ISDIR(output_metadata.st_mode):
        raise RemoteCaptureError("local prospective output root is unsafe")
    token = os.urandom(4).hex()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"prospective-{timestamp}-{commit[:12]}-{token}"
    final_path = output_root / name
    staging_path = output_root / f".{name}.staging"
    if final_path.exists() or staging_path.exists():
        raise RemoteCaptureError("local prospective capture destination already exists")
    staging_path.mkdir(mode=0o700)
    known_hosts = staging_path / "ssh-known-hosts"
    archive = staging_path / "prospective-session.tar.gz"
    transfer_metadata = staging_path / "prospective-session.tar.gz.metadata.json"
    checksum = staging_path / "prospective-session.tar.gz.sha256"
    remote_root = f"/tmp/inferdrome-prospective-{commit[:12]}-{token}"
    try:
        _prepare_pinned_known_hosts(
            destination=args.destination,
            known_hosts=known_hosts,
            host_key_file=(
                Path(args.host_key_file).expanduser().absolute()
                if getattr(args, "host_key_file", None) is not None
                else None
            ),
            expected_digest=args.host_key_sha256,
            port=args.port,
        )
        ssh_options = _ssh_options(
            identity=identity,
            known_hosts=known_hosts,
            port=args.port,
        )
        scp_options = _scp_options(
            identity=identity,
            known_hosts=known_hosts,
            port=args.port,
        )
        print(f"Inferdrome commit: {commit}")
        print(f"Prospective remote workspace: {remote_root}")
        print("Preflighting the pinned operator-provided GPU host…", flush=True)
        _run(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _bash_command(
                    _remote_preflight_script(remote_root, gpu_index=args.gpu_index)
                ),
            ],
            label="prospective remote GPU preflight",
            timeout=90,
        )
        _run(
            [
                "scp",
                *scp_options,
                str(source_archive),
                f"{args.destination}:{remote_root}/repo.tar",
            ],
            label="prospective source archive upload",
            timeout=600,
        )
        _run(
            [
                "scp",
                *scp_options,
                str(handoff.archive_path),
                f"{args.destination}:{remote_root}/handoff.tar.gz",
            ],
            label="prospective handoff archive upload",
            timeout=600,
        )
        effective_timeout = _effective_remote_timeout(
            args.remote_timeout_seconds,
            termination_deadline=termination_deadline,
            max_remote_seconds=args.remote_timeout_seconds,
            post_remote_budget_seconds=_PROSPECTIVE_POST_REMOTE_BUDGET_SECONDS,
        )
        remote_result = _run(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _bash_command(
                    _remote_prospective_capture_script(
                        remote_root,
                        commit,
                        source_archive_sha256,
                        handoff,
                        handoff_archive_sha256=handoff.archive_sha256,
                        handoff_archive_size=handoff.archive_size_bytes,
                        handoff_manifest_sha256=handoff.manifest_sha256,
                        workload_sha256=handoff.workload_sha256,
                        remote_state_root=args.remote_state_root,
                        gpu_index=args.gpu_index,
                        startup_timeout_seconds=args.startup_timeout_seconds,
                        remote_timeout_seconds=effective_timeout,
                    )
                ),
            ],
            label="prospective remote capture",
            timeout=effective_timeout + 600,
            check=False,
        )
        if remote_result.returncode != 0:
            raise RemoteCaptureError(
                "prospective remote capture failed; child output was suppressed "
                f"and local staging remains at {staging_path} "
                f"(remote workspace {remote_root})"
            )
        transfer_deadline = _prospective_transfer_deadline(termination_deadline)
        _download_bounded_remote_file(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _remote_file_stream_command(
                    f"{remote_root}/prospective-session.tar.gz.metadata.json",
                    minimum_size=2,
                    maximum_size=4_096,
                ),
            ],
            transfer_metadata,
            minimum_size=2,
            maximum_size=4_096,
            timeout=_prospective_transfer_timeout(transfer_deadline, 300),
        )
        expected_size, expected_archive_sha256 = _read_prospective_transfer_metadata(
            transfer_metadata
        )
        _download_exact_remote_file(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _remote_file_stream_command(
                    f"{remote_root}/prospective-session.tar.gz",
                    expected_size,
                ),
            ],
            archive,
            expected_size=expected_size,
            expected_sha256=expected_archive_sha256,
            timeout=_prospective_transfer_timeout(transfer_deadline, 1_800),
        )
        _write_bytes_exclusive(
            checksum,
            (
                expected_archive_sha256.removeprefix("sha256:")
                + "  prospective-session.tar.gz\n"
            ).encode("ascii"),
        )
        _publish_no_replace(
            staging_path,
            final_path,
            label="local prospective capture destination",
        )
    except Exception:
        if staging_path.exists():
            print(
                f"Partial prospective local diagnostics remain at {staging_path}",
                file=sys.stderr,
            )
        raise
    print(f"Prospective session retrieved: {final_path}")
    return final_path, len(source_bytes)


def _materialize_prospective_capture(
    capture_path: Path,
    handoff: prospective_handoff.HandoffSnapshot,
    *,
    commit: str,
    source_archive_sha256: str,
    source_archive_size_bytes: int,
) -> tuple[str, list[dict[str, str]]]:
    """Inspect the raw session only after the provider boundary is complete."""

    if not 1 <= source_archive_size_bytes <= _MAX_SOURCE_ARCHIVE_BYTES:
        raise RemoteCaptureError("prospective source archive size is invalid")
    try:
        archive_bytes, archive_identity = prospective_handoff._read_regular_once(
            capture_path / "prospective-session.tar.gz",
            label="retained prospective session archive",
            maximum_bytes=_PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES,
        )
    except prospective_handoff.ProspectiveHandoffError as error:
        raise RemoteCaptureError(str(error)) from None
    archive_size, archive_sha256 = _read_prospective_transfer_metadata(
        capture_path / "prospective-session.tar.gz.metadata.json"
    )
    if (
        len(archive_bytes) != archive_size
        or "sha256:" + hashlib.sha256(archive_bytes).hexdigest() != archive_sha256
        or archive_identity[4] != archive_size
    ):
        raise RemoteCaptureError(
            "retained prospective session archive changed before extraction"
        )
    verified_archive = capture_path / "prospective-session.verified.tar.gz"
    _write_bytes_exclusive(verified_archive, archive_bytes)
    session_root = _extract_prospective_archive(
        archive_bytes,
        capture_path / "session",
    )
    session_id, case_identities = _prospective_session_identities(session_root)
    _write_json(
        capture_path / "prospective-transport.json",
        {
            "case_identities": case_identities,
            "handoff_archive_sha256": handoff.archive_sha256,
            "handoff_archive_size_bytes": handoff.archive_size_bytes,
            "handoff_manifest_sha256": handoff.manifest_sha256,
            "repository_commit": commit,
            "schema_version": "inferdrome.prospective-ssh-transport.v1",
            "session_id": session_id,
            "source_archive_sha256": source_archive_sha256,
            "source_archive_size_bytes": source_archive_size_bytes,
            "ssh_host_identity_sha256": _host_identity_digest(
                capture_path / "ssh-known-hosts"
            ),
            "workload_sha256": handoff.workload_sha256,
        },
    )
    return session_id, case_identities


def _finalize_prospective_capture(
    capture_path: Path,
    handoff: prospective_handoff.HandoffSnapshot,
    *,
    guarded_termination: lambda_gpu_guard.TerminationResult | None,
    expected_commit: str | None = None,
    expected_source_archive_sha256: str | None = None,
    expected_source_archive_size_bytes: int | None = None,
    expected_host_identity_sha256: str | None = None,
    verified_session_identities: tuple[str, list[dict[str, str]]] | None = None,
) -> Path:
    """Verify offline after the required termination boundary."""

    transport_bytes = _safe_record_bytes(
        capture_path / "prospective-transport.json",
        label="prospective transport receipt",
    )
    transport = _strict_json_bytes(
        transport_bytes,
        label="prospective transport receipt",
    )
    if (
        set(transport)
        != {
            "case_identities",
            "handoff_archive_sha256",
            "handoff_archive_size_bytes",
            "handoff_manifest_sha256",
            "repository_commit",
            "schema_version",
            "session_id",
            "source_archive_sha256",
            "source_archive_size_bytes",
            "ssh_host_identity_sha256",
            "workload_sha256",
        }
        or transport.get("schema_version") != "inferdrome.prospective-ssh-transport.v1"
    ):
        raise RemoteCaptureError("prospective transport receipt schema is unsupported")
    digest_values = (
        transport.get("handoff_archive_sha256"),
        transport.get("handoff_manifest_sha256"),
        transport.get("source_archive_sha256"),
        transport.get("ssh_host_identity_sha256"),
        transport.get("workload_sha256"),
    )
    if any(
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        for value in digest_values
    ):
        raise RemoteCaptureError("prospective transport receipt has invalid digests")
    if (
        transport["handoff_archive_sha256"] != handoff.archive_sha256
        or transport["handoff_manifest_sha256"] != handoff.manifest_sha256
        or transport["workload_sha256"] != handoff.workload_sha256
        or not isinstance(transport.get("repository_commit"), str)
        or _COMMIT_PATTERN.fullmatch(transport["repository_commit"]) is None
        or not isinstance(transport.get("session_id"), str)
        or not isinstance(transport.get("source_archive_size_bytes"), int)
        or not isinstance(transport.get("handoff_archive_size_bytes"), int)
        or transport["handoff_archive_size_bytes"] != handoff.archive_size_bytes
        or not 1 <= transport["source_archive_size_bytes"] <= _MAX_SOURCE_ARCHIVE_BYTES
        or (
            expected_commit is not None
            and transport["repository_commit"] != expected_commit
        )
        or (
            expected_source_archive_sha256 is not None
            and transport["source_archive_sha256"] != expected_source_archive_sha256
        )
        or (
            expected_source_archive_size_bytes is not None
            and transport["source_archive_size_bytes"]
            != expected_source_archive_size_bytes
        )
        or (
            expected_host_identity_sha256 is not None
            and transport["ssh_host_identity_sha256"] != expected_host_identity_sha256
        )
    ):
        raise RemoteCaptureError("prospective transport provenance disagrees")
    session_id_value = transport.get("session_id")
    if (
        not isinstance(session_id_value, str)
        or re.fullmatch(r"prospective-real-gpu-[A-Za-z0-9_.-]{1,128}", session_id_value)
        is None
    ):
        raise RemoteCaptureError("prospective transport session identity is invalid")
    session_root = capture_path / "session" / session_id_value
    if verified_session_identities is None:
        verified_session_identities = _prospective_session_identities(session_root)
    verification = _verify_prospective_session(session_root, handoff)
    expected_cases = _validate_prospective_case_identities(
        transport.get("case_identities")
    )
    session_id, session_cases = verified_session_identities
    if transport["session_id"] != session_id or expected_cases != session_cases:
        raise RemoteCaptureError(
            "prospective transport receipt identities disagree with the session"
        )
    if verification.get("valid") is not True:
        raise RemoteCaptureError("prospective offline verification is incomplete")
    archive_size, archive_sha256 = _read_prospective_transfer_metadata(
        capture_path / "prospective-session.tar.gz.metadata.json"
    )
    receipt = {
        "case_identities": expected_cases,
        "chronology": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
        "handoff_archive": {
            "sha256": transport.get("handoff_archive_sha256"),
            "size_bytes": transport.get("handoff_archive_size_bytes"),
        },
        "handoff_archive_sha256": transport.get("handoff_archive_sha256"),
        "handoff_manifest_sha256": transport.get("handoff_manifest_sha256"),
        "offline_verification": verification,
        "publication_status": "EXTERNAL_ONLY",
        "repository_commit": transport.get("repository_commit"),
        "retrieved_archive": {
            "sha256": archive_sha256,
            "size_bytes": archive_size,
        },
        "schema_version": "inferdrome.prospective-real-gpu-retrieval.v1",
        "session_id": transport.get("session_id"),
        "source_archive": {
            "sha256": transport.get("source_archive_sha256"),
            "size_bytes": transport.get("source_archive_size_bytes"),
        },
        "source_archive_sha256": transport.get("source_archive_sha256"),
        "ssh_host_identity_sha256": transport.get("ssh_host_identity_sha256"),
        "status": "CAPTURED_PENDING_EXTERNAL_EXITSPEC",
        "termination_boundary": (
            "PROVIDER_TERMINATION_CONFIRMED"
            if guarded_termination is not None
            else "OPERATOR_PROVIDED_HOST_NO_COST_TERMINATION_CLAIM"
        ),
        "workload_sha256": transport.get("workload_sha256"),
    }
    if guarded_termination is not None:
        receipt["provider_termination"] = guarded_termination.public_record()
    _write_json(capture_path / "retrieval-receipt.json", receipt)
    print(
        "\nPROSPECTIVE CAPTURE VERIFIED OFFLINE; no ExitSpec acceptance verdict "
        "was emitted."
    )
    print(f"Local prospective capture record: {capture_path}")
    return capture_path


def _capture_over_ssh(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    source_archive: Path,
    source_archive_sha256: str,
    *,
    termination_deadline: datetime | None = None,
) -> Path:
    for executable in ("ssh", "scp"):
        try:
            _local_executable(executable)
        except AdapterError:
            raise RemoteCaptureError(f"{executable} is required") from None
    output_root = Path(args.output_root).expanduser().absolute()
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        output_metadata = os.lstat(output_root)
    except OSError:
        raise RemoteCaptureError("local capture output root is unavailable") from None
    if output_root.is_symlink() or not stat.S_ISDIR(output_metadata.st_mode):
        raise RemoteCaptureError("local capture output root is unsafe")
    token = os.urandom(4).hex()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{timestamp}-{commit[:12]}-{token}"
    final_path = output_root / name
    staging_path = output_root / f".{name}.staging"
    if final_path.exists() or staging_path.exists():
        raise RemoteCaptureError("local capture destination already exists")
    staging_path.mkdir(mode=0o700)
    known_hosts = staging_path / "ssh-known-hosts"
    archive = staging_path / "capture.tar.gz"
    checksum = staging_path / "capture.tar.gz.sha256"
    transfer_metadata = staging_path / "capture.tar.gz.metadata.json"
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-{token}"
    expected_host_key_digest = getattr(args, "host_key_sha256", None)
    if (
        not isinstance(expected_host_key_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_host_key_digest) is None
    ):
        raise RemoteCaptureError("capture requires a pinned SSH host-key digest")
    observed_host_identity_sha256 = _prepare_pinned_known_hosts(
        destination=args.destination,
        known_hosts=known_hosts,
        host_key_file=(
            Path(args.host_key_file).expanduser().absolute()
            if getattr(args, "host_key_file", None) is not None
            else None
        ),
        expected_digest=expected_host_key_digest,
        port=args.port,
    )
    ssh_options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )
    scp_options = _scp_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )
    policy = _qwen3_tier_policy(args) if _qwen3_profile_requested(args) else None
    profile_deadline = termination_deadline if _qwen3_profile_requested(args) else None
    if _qwen3_profile_requested(args):
        _effective_remote_timeout(
            args.remote_timeout_seconds,
            termination_deadline=profile_deadline,
        )

    print(f"Inferdrome commit: {commit}")
    print(f"Remote workspace: {remote_root}")
    print("Preflighting the operator-provided GPU host…", flush=True)
    _run(
        [
            "ssh",
            *ssh_options,
            args.destination,
            _bash_command(
                _remote_preflight_script(
                    remote_root,
                    gpu_index=args.gpu_index,
                    expected_gpu_model=(
                        policy.expected_nvidia_smi_name if policy is not None else None
                    ),
                )
            ),
        ],
        label="remote GPU preflight",
        timeout=90,
    )
    try:
        _run(
            [
                "scp",
                *scp_options,
                str(source_archive),
                f"{args.destination}:{remote_root}/repo.tar",
            ],
            label="exact source tree upload",
            timeout=(
                _QWEN3_PHASE_BUDGET_SECONDS["source_upload"]
                if _qwen3_profile_requested(args)
                else 600
            ),
        )

        print(
            "Running the bounded proof pack; model preparation is usually "
            "the slowest step…",
            flush=True,
        )
        effective_remote_timeout = _effective_remote_timeout(
            args.remote_timeout_seconds,
            termination_deadline=profile_deadline,
        )
        if effective_remote_timeout != args.remote_timeout_seconds:
            print(
                "Remote timeout clamped to the live Lambda termination window: "
                f"{effective_remote_timeout}s",
                flush=True,
            )
        remote_result = _run(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _bash_command(
                    _remote_capture_script(
                        remote_root,
                        commit,
                        source_archive_sha256,
                        gpu_index=args.gpu_index,
                        startup_timeout_seconds=args.startup_timeout_seconds,
                        remote_timeout_seconds=effective_remote_timeout,
                        managed_capability_profile=args.managed_capability_profile,
                        qwen3_gpu_tier=(
                            policy.gpu_tier_id if policy is not None else None
                        ),
                    )
                ),
            ],
            label="remote proof pack",
            timeout=effective_remote_timeout
            + (65 if _qwen3_profile_requested(args) else 600),
            check=False,
        )
        retrieval_error: RemoteCaptureError | None = None
        try:
            if _qwen3_profile_requested(args):
                if profile_deadline is None:
                    raise RemoteCaptureError(
                        "Qwen3 retrieval requires a provider termination deadline"
                    )
                shared_deadline = _transfer_deadline(
                    termination_deadline=profile_deadline
                )
                _download_bounded_remote_file(
                    [
                        "ssh",
                        *ssh_options,
                        args.destination,
                        _remote_file_stream_command(
                            f"{remote_root}/capture.tar.gz.metadata.json",
                            minimum_size=2,
                            maximum_size=4_096,
                        ),
                    ],
                    transfer_metadata,
                    minimum_size=2,
                    maximum_size=4_096,
                    timeout=_remaining_transfer_timeout(
                        shared_deadline,
                        phase_limit_seconds=_QWEN3_METADATA_TRANSFER_SECONDS,
                    ),
                )
                expected_size, expected_archive_sha256 = _read_transfer_metadata(
                    transfer_metadata
                )
                _download_exact_remote_file(
                    [
                        "ssh",
                        *ssh_options,
                        args.destination,
                        _remote_file_stream_command(
                            f"{remote_root}/capture.tar.gz",
                            expected_size,
                        ),
                    ],
                    archive,
                    expected_size=expected_size,
                    expected_sha256=expected_archive_sha256,
                    timeout=_remaining_transfer_timeout(
                        shared_deadline,
                        phase_limit_seconds=_QWEN3_ARCHIVE_TRANSFER_SECONDS,
                    ),
                )
                _write_bytes_exclusive(
                    checksum,
                    (
                        expected_archive_sha256.removeprefix("sha256:")
                        + "  capture.tar.gz\n"
                    ).encode("ascii"),
                )
                actual_archive_sha256 = expected_archive_sha256
            else:
                for remote_name, local_path in (
                    ("capture.tar.gz", archive),
                    ("capture.tar.gz.sha256", checksum),
                ):
                    _run(
                        [
                            "scp",
                            *scp_options,
                            f"{args.destination}:{remote_root}/{remote_name}",
                            str(local_path),
                        ],
                        label=f"{remote_name} retrieval",
                        timeout=1_800,
                    )
        except RemoteCaptureError as error:
            retrieval_error = error
        if retrieval_error is not None:
            raise retrieval_error

        expected_archive_sha256 = _checksum_file(checksum)
        if not _qwen3_profile_requested(args):
            try:
                actual_archive_sha256 = real_gpu_capture.archive_sha256(archive)
                if actual_archive_sha256 != expected_archive_sha256:
                    raise RemoteCaptureError(
                        "retrieved archive failed SHA-256 verification"
                    )
            except real_gpu_capture.CaptureError as error:
                raise RemoteCaptureError(str(error)) from None
        if remote_result.returncode != 0:
            if not _qwen3_profile_requested(args):
                try:
                    real_gpu_capture.extract_capture_archive(
                        archive,
                        staging_path,
                        expected_archive_sha256=expected_archive_sha256,
                    )
                except real_gpu_capture.CaptureError as error:
                    raise RemoteCaptureError(str(error)) from None
            failure_path = output_root / f"{name}-FAILED"
            os.replace(staging_path, failure_path)
            raise RemoteCaptureError(
                "remote proof pack failed; retained diagnostics at "
                f"{failure_path} (remote workspace {remote_root})"
            )
        if _qwen3_profile_requested(args):
            if policy is None:
                raise AssertionError
            retrieval = {
                "archive_sha256": actual_archive_sha256,
                "billing_action_required": "PROVIDER_TERMINATION_PENDING",
                "gpu_tier_id": policy.gpu_tier_id,
                "lambda_instance_type_name": args.lambda_instance_type_name,
                "managed_capability_profile": _QWEN3_PROFILE_ID,
                "repository_commit": commit,
                "schema_version": "inferdrome.qwen3-gpu-retrieval.v2",
                "source_archive_sha256": source_archive_sha256,
                "semantic_verification": (
                    "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
                ),
                "ssh_host_identity_sha256": observed_host_identity_sha256,
                "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        else:
            try:
                real_gpu_capture.extract_capture_archive(
                    archive,
                    staging_path,
                    expected_archive_sha256=expected_archive_sha256,
                )
                archive_verification = real_gpu_capture.verify_capture_archive(
                    archive,
                    expected_archive_sha256=expected_archive_sha256,
                    expected_repository_commit=commit,
                )
            except real_gpu_capture.CaptureError as error:
                failure_path = output_root / f"{name}-UNVERIFIED"
                os.replace(staging_path, failure_path)
                raise RemoteCaptureError(
                    f"retrieved capture failed local verification: {error}; "
                    f"retained at {failure_path}"
                ) from None
            retrieval = {
                "archive_sha256": archive_verification["archive_sha256"],
                "billing_action_required": "TERMINATE_THE_GPU_INSTANCE",
                "capture_manifest_sha256": archive_verification[
                    "capture_manifest_sha256"
                ],
                "repository_commit": commit,
                "schema_version": "inferdrome.real-gpu-retrieval.v1",
                "ssh_host_identity_sha256": observed_host_identity_sha256,
                "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "verification": archive_verification["verification"],
            }
        _write_json(staging_path / "retrieval-receipt.json", retrieval)
        os.replace(staging_path, final_path)
    except Exception:
        if staging_path.exists():
            print(
                f"Partial local diagnostics remain at {staging_path}",
                file=sys.stderr,
            )
        raise

    if _qwen3_profile_requested(args):
        print(
            "\nARCHIVE CHECKSUM VERIFIED. LAMBDA TERMINATION IS BEING CONFIRMED "
            "BEFORE OFFLINE SEMANTIC VERIFICATION."
        )
        print(f"Pending local capture record: {final_path}")
        print(f"Remote workspace retained until instance termination: {remote_root}")
        return final_path
    if _lambda_guard_requested(args):
        print("\nCAPTURE VERIFIED. LAMBDA TERMINATION IS BEING CONFIRMED.")
    else:
        print("\nCAPTURE VERIFIED. TERMINATE THE BILLABLE GPU INSTANCE NOW.")
    print(f"Local capture record: {final_path}")
    print(f"Remote workspace retained until instance termination: {remote_root}")
    comparison_roots = sorted(
        (final_path / "capture" / "comparison").glob("real-gpu-comparison-*")
    )
    if len(comparison_roots) == 1:
        comparison_root = comparison_roots[0]
        try:
            dashboard_executable = str(_local_executable("inferdrome").path)
        except AdapterError:
            dashboard_executable = "inferdrome"
        dashboard_arguments = [
            dashboard_executable,
            "dashboard",
            "--runs-root",
            str(comparison_root / "runs"),
            "--trial-sets-root",
            str(comparison_root / "trial-sets"),
            "--comparison-plans-root",
            str(comparison_root / "comparison-plans"),
            "--comparison-results-root",
            str(comparison_root / "comparison-results"),
            "--open",
        ]
        print("Dashboard inspection command:")
        print("  " + shlex.join(dashboard_arguments))
    return final_path


def _write_bytes_exclusive(path: Path, content: bytes) -> tuple[int, int]:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o444,
        )
        created_inode = prospective_handoff._inode_identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as stream:
            if stream.write(content) != len(content):
                raise OSError("short write")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RemoteCaptureError(f"could not publish {path.name}") from None
    return created_inode


def _write_bytes_idempotent(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        try:
            metadata = os.lstat(path)
            existing = path.read_bytes()
        except OSError:
            raise RemoteCaptureError(f"could not verify {path.name}") from None
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or existing != content
        ):
            raise RemoteCaptureError(f"existing {path.name} disagrees")
        return
    _write_bytes_exclusive(path, content)


def _strict_json_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RemoteCaptureError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise RemoteCaptureError(f"{label} is invalid")
    return value


def _safe_record_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise RemoteCaptureError(f"{label} is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not content
        or len(content) > 65_536
    ):
        raise RemoteCaptureError(f"{label} is unsafe")
    return content


@dataclass(frozen=True)
class _TerminationEvidence:
    cost_window: dict[str, Any]
    instance: dict[str, Any]
    receipt: bytes
    termination: dict[str, Any]
    trigger: str


@dataclass(frozen=True)
class _Qwen3RetrievalIdentity:
    gpu_tier_id: str
    instance_type_name: str
    legacy: bool
    receipt: bytes
    schema_version: str
    value: dict[str, Any]


def _qwen3_retrieval_identity(capture_path: Path) -> _Qwen3RetrievalIdentity:
    receipt = _safe_record_bytes(
        capture_path / "retrieval-receipt.json",
        label="Qwen3 retrieval receipt",
    )
    value = _strict_json_bytes(receipt, label="Qwen3 retrieval receipt")
    schema_version = value.get("schema_version")
    if schema_version == "inferdrome.qwen3-gpu-retrieval.v1":
        return _Qwen3RetrievalIdentity(
            gpu_tier_id=QWEN3_A10_GPU_TIER_ID,
            instance_type_name=_QWEN3_EXPECTED_INSTANCE_TYPE,
            legacy=True,
            receipt=receipt,
            schema_version=schema_version,
            value=value,
        )
    if schema_version != "inferdrome.qwen3-gpu-retrieval.v2":
        raise RemoteCaptureError("Qwen3 retrieval receipt schema is unsupported")
    gpu_tier_id = value.get("gpu_tier_id")
    instance_type_name = value.get("lambda_instance_type_name")
    if not isinstance(gpu_tier_id, str):
        raise RemoteCaptureError("Qwen3 retrieval GPU tier is invalid")
    try:
        qwen3_gpu_tier_policy(gpu_tier_id)
    except ValueError as error:
        raise RemoteCaptureError(str(error)) from None
    if (
        not isinstance(instance_type_name, str)
        or _INSTANCE_TYPE_PATTERN.fullmatch(instance_type_name) is None
    ):
        raise RemoteCaptureError("Qwen3 retrieval instance type is invalid")
    return _Qwen3RetrievalIdentity(
        gpu_tier_id=gpu_tier_id,
        instance_type_name=instance_type_name,
        legacy=False,
        receipt=receipt,
        schema_version=schema_version,
        value=value,
    )


def _timestamp_value(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RemoteCaptureError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise RemoteCaptureError(f"{label} is invalid") from None
    if parsed.tzinfo != UTC:
        raise RemoteCaptureError(f"{label} is invalid")
    return parsed


def _validate_termination_evidence(
    *,
    cost_window: object,
    instance: object,
    receipt_value: dict[str, Any],
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    policy = qwen3_gpu_tier_policy(identity.gpu_tier_id)
    if (
        not isinstance(cost_window, dict)
        or set(cost_window)
        != {
            "allowed_seconds",
            "billing_started_at",
            "cost_limit_deadline",
            "deadline",
            "hourly_rate_usd",
            "max_cost_usd",
            "termination_safety_margin_seconds",
        }
        or cost_window.get("allowed_seconds") != policy.allowed_seconds
        or cost_window.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or cost_window.get("max_cost_usd") != str(policy.max_session_cost_usd)
        or cost_window.get("termination_safety_margin_seconds")
        != _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
    ):
        raise RemoteCaptureError("Lambda Qwen3 cost window is invalid")
    started = _timestamp_value(
        cost_window.get("billing_started_at"),
        label="Lambda billing start",
    )
    deadline = _timestamp_value(
        cost_window.get("deadline"),
        label="Lambda termination deadline",
    )
    cost_limit = _timestamp_value(
        cost_window.get("cost_limit_deadline"),
        label="Lambda cost-limit deadline",
    )
    if (
        int((cost_limit - started).total_seconds()) != policy.allowed_seconds
        or int((cost_limit - deadline).total_seconds())
        != _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
    ):
        raise RemoteCaptureError("Lambda Qwen3 cost-window timestamps disagree")
    if (
        not isinstance(instance, dict)
        or set(instance)
        != {
            "hostname",
            "hourly_rate_usd",
            "instance_id",
            "instance_type_name",
            "ip",
            "status",
        }
        or re.fullmatch(r"[0-9a-f]{32}", str(instance.get("instance_id"))) is None
        or instance.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or instance.get("instance_type_name") != identity.instance_type_name
        or instance.get("status") != "active"
    ):
        raise RemoteCaptureError("Lambda Qwen3 instance record is invalid")
    if (
        set(receipt_value)
        != {
            "cost_window",
            "record_kind",
            "schema_version",
            "termination",
            "trigger",
        }
        or receipt_value.get("cost_window") != cost_window
        or receipt_value.get("record_kind")
        != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or receipt_value.get("schema_version")
        != "inferdrome.lambda-termination-receipt.v2"
        or receipt_value.get("trigger") not in {"controller-finally", "cost-deadline"}
    ):
        raise RemoteCaptureError("Lambda termination receipt is invalid")
    termination = receipt_value.get("termination")
    if (
        not isinstance(termination, dict)
        or set(termination)
        != {"confirmed_at", "final_status", "instance_id", "request_sent"}
        or termination.get("instance_id") != instance.get("instance_id")
        or termination.get("final_status") not in {"terminated", "preempted", "absent"}
        or not isinstance(termination.get("request_sent"), bool)
    ):
        raise RemoteCaptureError("Lambda termination confirmation is invalid")
    _timestamp_value(
        termination.get("confirmed_at"),
        label="Lambda termination confirmation time",
    )
    return _TerminationEvidence(
        cost_window=cost_window,
        instance=instance,
        receipt=b"",
        termination=termination,
        trigger=receipt_value["trigger"],
    )


def _controller_termination_evidence(
    watchdog: lambda_gpu_guard.WatchdogHandle,
    termination: lambda_gpu_guard.TerminationResult,
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    receipt = _safe_record_bytes(
        watchdog.receipt_path,
        label="Lambda termination receipt",
    )
    receipt_value = _strict_json_bytes(
        receipt,
        label="Lambda termination receipt",
    )
    evidence = _validate_termination_evidence(
        cost_window=watchdog.cost_window.public_record(),
        instance=watchdog.instance.public_record(),
        receipt_value=receipt_value,
        identity=identity,
    )
    if evidence.termination != termination.public_record():
        raise RemoteCaptureError(
            "Lambda termination receipt disagrees with confirmation"
        )
    return _TerminationEvidence(
        cost_window=evidence.cost_window,
        instance=evidence.instance,
        receipt=receipt,
        termination=evidence.termination,
        trigger=evidence.trigger,
    )


def _retained_termination_evidence(
    state_directory: Path,
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    selected = state_directory.expanduser().absolute()
    try:
        metadata = os.lstat(selected)
    except OSError:
        raise RemoteCaptureError(
            "Lambda guard state directory is unavailable"
        ) from None
    if selected.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RemoteCaptureError("Lambda guard state directory is unsafe")
    armed_bytes = _safe_record_bytes(
        selected / "guard-armed.json",
        label="Lambda guard-armed receipt",
    )
    armed = _strict_json_bytes(
        armed_bytes,
        label="Lambda guard-armed receipt",
    )
    if (
        set(armed)
        != {
            "cost_window",
            "instance",
            "record_kind",
            "schema_version",
            "watchdog_ready",
        }
        or armed.get("record_kind") != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or armed.get("schema_version") != "inferdrome.lambda-guard-armed.v2"
        or armed.get("watchdog_ready") is not True
    ):
        raise RemoteCaptureError("Lambda guard-armed receipt is invalid")
    receipt = _safe_record_bytes(
        selected / "termination-receipt.json",
        label="Lambda termination receipt",
    )
    receipt_value = _strict_json_bytes(
        receipt,
        label="Lambda termination receipt",
    )
    evidence = _validate_termination_evidence(
        cost_window=armed.get("cost_window"),
        instance=armed.get("instance"),
        receipt_value=receipt_value,
        identity=identity,
    )
    return _TerminationEvidence(
        cost_window=evidence.cost_window,
        instance=evidence.instance,
        receipt=receipt,
        termination=evidence.termination,
        trigger=evidence.trigger,
    )


def _finalize_qwen3_capture_with_evidence(
    capture_path: Path,
    *,
    commit: str,
    evidence: _TerminationEvidence,
) -> Path:
    """Idempotently verify and publish only after confirmed provider termination."""

    try:
        capture_metadata = os.lstat(capture_path)
    except OSError:
        raise RemoteCaptureError("Qwen3 capture directory is unavailable") from None
    if capture_path.is_symlink() or not stat.S_ISDIR(capture_metadata.st_mode):
        raise RemoteCaptureError("Qwen3 capture directory is unsafe")
    identity = _qwen3_retrieval_identity(capture_path)
    policy = qwen3_gpu_tier_policy(identity.gpu_tier_id)
    if (
        evidence.instance.get("instance_type_name") != identity.instance_type_name
        or evidence.instance.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or evidence.cost_window.get("max_cost_usd") != str(policy.max_session_cost_usd)
    ):
        raise RemoteCaptureError(
            "Lambda termination evidence disagrees with Qwen3 retrieval identity"
        )

    archive = capture_path / "capture.tar.gz"
    checksum = capture_path / "capture.tar.gz.sha256"
    expected_archive_sha256 = _checksum_file(checksum)
    try:
        archive_verification = qwen3_gpu_capture.verify_capture_archive(
            archive,
            expected_archive_sha256=expected_archive_sha256,
            expected_repository_commit=commit,
            expected_gpu_tier_id=identity.gpu_tier_id,
        )
        extracted = capture_path / "capture"
        if extracted.exists() or extracted.is_symlink():
            extracted_verification = qwen3_gpu_capture.verify_capture(
                extracted,
                expected_repository_commit=commit,
                expected_gpu_tier_id=identity.gpu_tier_id,
            )
        else:
            with tempfile.TemporaryDirectory(
                prefix=".qwen3-finalize-",
                dir=capture_path,
            ) as temporary:
                staged = real_gpu_capture.extract_capture_archive(
                    archive,
                    Path(temporary),
                    expected_archive_sha256=expected_archive_sha256,
                )
                staged_verification = qwen3_gpu_capture.verify_capture(
                    staged,
                    expected_repository_commit=commit,
                    expected_gpu_tier_id=identity.gpu_tier_id,
                )
                os.replace(staged, extracted)
                extracted_verification = staged_verification
    except (
        qwen3_gpu_capture.Qwen3CaptureError,
        real_gpu_capture.CaptureError,
        OSError,
    ) as error:
        raise RemoteCaptureError(
            "terminated Qwen3 capture failed offline semantic verification: "
            f"{error}; retained at {capture_path}"
        ) from None
    if archive_verification["verification"] != extracted_verification:
        raise RemoteCaptureError(
            "Qwen3 archive verification changed after retained extraction"
        )
    retrieval_bytes = identity.receipt
    retrieval = identity.value
    retrieval_fields = {
        "archive_sha256",
        "billing_action_required",
        "managed_capability_profile",
        "repository_commit",
        "schema_version",
        "semantic_verification",
        "source_archive_sha256",
        "ssh_host_identity_sha256",
        "verified_at",
    }
    if not identity.legacy:
        retrieval_fields.update({"gpu_tier_id", "lambda_instance_type_name"})
    if (
        set(retrieval) != retrieval_fields
        or retrieval.get("archive_sha256") != archive_verification["archive_sha256"]
        or retrieval.get("billing_action_required") != "PROVIDER_TERMINATION_PENDING"
        or retrieval.get("managed_capability_profile") != _QWEN3_PROFILE_ID
        or retrieval.get("repository_commit") != commit
        or retrieval.get("schema_version") != identity.schema_version
        or retrieval.get("semantic_verification")
        != "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
        or retrieval.get("source_archive_sha256")
        != extracted_verification["source_archive_sha256"]
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(retrieval.get("ssh_host_identity_sha256")),
        )
        is None
        or (
            not identity.legacy
            and (
                retrieval.get("gpu_tier_id") != identity.gpu_tier_id
                or retrieval.get("lambda_instance_type_name")
                != identity.instance_type_name
            )
        )
    ):
        raise RemoteCaptureError("Qwen3 retrieval receipt disagrees with capture")
    _timestamp_value(
        retrieval.get("verified_at"),
        label="Qwen3 retrieval verification time",
    )

    retained_termination_path = capture_path / "lambda-termination-receipt.json"
    _write_bytes_idempotent(retained_termination_path, evidence.receipt)
    termination_receipt_sha256 = (
        "sha256:" + hashlib.sha256(evidence.receipt).hexdigest()
    )
    semantic_core: dict[str, Any] = {
        "archive_sha256": archive_verification["archive_sha256"],
        "capture_manifest_sha256": archive_verification["capture_manifest_sha256"],
        "managed_capability_profile": _QWEN3_PROFILE_ID,
        "provider_instance": evidence.instance,
        "provider_termination": evidence.termination,
        "retrieval_receipt_sha256": (
            "sha256:" + hashlib.sha256(retrieval_bytes).hexdigest()
        ),
        "repository_commit": commit,
        "run": extracted_verification["run"],
        "schema_version": (
            "inferdrome.qwen3-offline-verification.v1"
            if identity.legacy
            else "inferdrome.qwen3-offline-verification.v2"
        ),
        "semantic_verification": "VALID_AFTER_PROVIDER_TERMINATION",
        "termination_receipt_sha256": termination_receipt_sha256,
        "termination_trigger": evidence.trigger,
    }
    if not identity.legacy:
        semantic_core.update(
            {
                "gpu_target": extracted_verification["gpu_target"],
                "gpu_tier_id": identity.gpu_tier_id,
                "lambda_instance_type_name": identity.instance_type_name,
            }
        )
    semantic_receipt = {
        **semantic_core,
        "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    semantic_path = capture_path / "semantic-verification.json"
    if semantic_path.exists() or semantic_path.is_symlink():
        existing_bytes = _safe_record_bytes(
            semantic_path,
            label="Qwen3 semantic-verification receipt",
        )
        existing = _strict_json_bytes(
            existing_bytes,
            label="Qwen3 semantic-verification receipt",
        )
        verified_at = existing.pop("verified_at", None)
        _timestamp_value(verified_at, label="Qwen3 semantic verification time")
        if existing != semantic_core:
            raise RemoteCaptureError(
                "existing Qwen3 semantic-verification receipt disagrees"
            )
    else:
        _write_json(semantic_path, semantic_receipt)

    print("\nQWEN3 CAPTURE VERIFIED AFTER PROVIDER TERMINATION.")
    print(f"Local capture record: {capture_path}")
    try:
        dashboard_executable = str(_local_executable("inferdrome").path)
    except AdapterError:
        dashboard_executable = "inferdrome"
    dashboard_arguments = [
        dashboard_executable,
        "dashboard",
        "--runs-root",
        str(extracted / "runs"),
        "--open",
    ]
    print("Dashboard inspection command:")
    print("  " + shlex.join(dashboard_arguments))
    return capture_path


def _finalize_qwen3_capture(
    capture_path: Path,
    *,
    commit: str,
    watchdog: lambda_gpu_guard.WatchdogHandle,
    termination: lambda_gpu_guard.TerminationResult,
) -> Path:
    identity = _qwen3_retrieval_identity(capture_path)
    evidence = _controller_termination_evidence(watchdog, termination, identity)
    return _finalize_qwen3_capture_with_evidence(
        capture_path,
        commit=commit,
        evidence=evidence,
    )


def _resume_qwen3_finalization(
    capture_path: Path,
    *,
    commit: str,
    guard_state_directory: Path,
) -> Path:
    selected_capture_path = capture_path.expanduser().absolute()
    identity = _qwen3_retrieval_identity(selected_capture_path)
    evidence = _retained_termination_evidence(guard_state_directory, identity)
    return _finalize_qwen3_capture_with_evidence(
        selected_capture_path,
        commit=commit,
        evidence=evidence,
    )


def _terminate_guarded_capture(
    watchdog: lambda_gpu_guard.WatchdogHandle,
    *,
    capture_failed: bool,
    capture_label: str,
    termination_trigger: str | None = None,
) -> lambda_gpu_guard.TerminationResult | None:
    """Confirm provider termination or retain explicit unresolved guard state."""

    try:
        if termination_trigger is None:
            termination = lambda_gpu_guard.terminate_guarded_instance(watchdog)
        else:
            termination = lambda_gpu_guard.terminate_guarded_instance(
                watchdog,
                trigger=termination_trigger,
            )
    except lambda_gpu_guard.LambdaGuardFinalizationError as error:
        termination = error.result
        print(
            "WARNING: provider termination was confirmed, but local guard "
            "finalization was incomplete.",
            file=sys.stderr,
        )
        if not capture_failed:
            raise RemoteCaptureError(
                f"{capture_label} completed and Lambda terminated, but local guard "
                "finalization failed"
            ) from None
        return termination
    except lambda_gpu_guard.LambdaGuardError:
        marker: Path | None = None
        try:
            if termination_trigger is None:
                marker = lambda_gpu_guard.retain_unresolved_termination(watchdog)
            else:
                marker = lambda_gpu_guard.retain_unresolved_termination(
                    watchdog,
                    trigger=termination_trigger,
                )
        except (AttributeError, OSError, lambda_gpu_guard.LambdaGuardError):
            raise RemoteCaptureError(
                f"{capture_label} failed, provider termination was not confirmed, "
                "and explicit unresolved state could not be retained"
            ) from None
        print(
            "CRITICAL: immediate Lambda termination was not confirmed; the "
            f"deadline watchdog state is retained at {marker}.",
            file=sys.stderr,
        )
        if not capture_failed:
            raise RemoteCaptureError(
                f"{capture_label} completed, but Lambda termination was not confirmed"
            ) from None
        return None
    print(
        "Lambda termination confirmed: "
        f"{watchdog.instance.instance_id} ({termination.final_status})."
    )
    return termination


def _capture_with_source(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    source_archive: Path | None = None,
    source_archive_sha256: str | None = None,
    *,
    guard_signals: bool = False,
) -> Path:
    if (source_archive is None) != (source_archive_sha256 is None):
        raise RemoteCaptureError("source archive and digest must be supplied together")
    watchdog: lambda_gpu_guard.WatchdogHandle | None = None
    captured: Path | None = None
    termination: lambda_gpu_guard.TerminationResult | None = None
    with _guarded_cleanup_boundary(guard_signals) as deferred_interrupts:
        try:
            with _defer_guarded_interrupts():
                watchdog = _arm_lambda_watchdog(args)
            termination_deadline = getattr(
                getattr(watchdog, "cost_window", None),
                "deadline",
                None,
            )
            with (
                _capture_interrupt_boundary(deferred_interrupts),
                tempfile.TemporaryDirectory(
                    prefix="inferdrome-source-tree-"
                ) as temporary,
            ):
                    selected_archive = source_archive
                    selected_digest = source_archive_sha256
                    if selected_archive is None:
                        selected_archive = Path(temporary) / "repo.tar"
                        selected_digest, source_archive_bytes = _create_source_archive(
                            selected_archive,
                            commit,
                        )
                        print(
                            "Exact source tree prepared without Git history: "
                            f"{selected_digest} ({source_archive_bytes} bytes)"
                        )
                    if selected_digest is None:
                        raise AssertionError
                    capture_arguments = (
                        args,
                        commit,
                        identity,
                        selected_archive,
                        selected_digest,
                    )
                    if termination_deadline is None:
                        captured = _capture_over_ssh(*capture_arguments)
                    else:
                        captured = _capture_over_ssh(
                            *capture_arguments,
                            termination_deadline=termination_deadline,
                        )
        finally:
            if watchdog is not None:
                capture_failed = sys.exc_info()[0] is not None
                if deferred_interrupts is None:
                    with _defer_guarded_interrupts():
                        termination = _terminate_guarded_capture(
                            watchdog,
                            capture_failed=capture_failed,
                            capture_label="capture",
                        )
                else:
                    termination = _terminate_guarded_capture(
                        watchdog,
                        capture_failed=capture_failed,
                        capture_label="capture",
                    )
    if captured is None:
        raise AssertionError
    if _qwen3_profile_requested(args):
        if watchdog is None or termination is None:
            raise RemoteCaptureError(
                "Qwen3 capture cannot verify before provider termination"
            )
        return _finalize_qwen3_capture(
            captured,
            commit=commit,
            watchdog=watchdog,
            termination=termination,
        )
    return captured


@contextmanager
def _guarded_interruptible(
    deferred_interrupts: _DeferredInterruptState | None = None,
) -> Any:
    """Make controller SIGINT/SIGTERM enter the guarded cleanup path."""

    previous: dict[signal.Signals, Any] = {}
    interrupt_raised = False

    def interrupt(_signum: int, _frame: Any) -> None:
        nonlocal interrupt_raised
        if interrupt_raised:
            if deferred_interrupts is not None:
                deferred_interrupts.pending = True
            return
        interrupt_raised = True
        raise KeyboardInterrupt

    try:
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            previous[selected_signal] = signal.getsignal(selected_signal)
            signal.signal(selected_signal, interrupt)
        yield
    finally:
        for selected_signal, handler in previous.items():
            signal.signal(selected_signal, handler)


@dataclass
class _DeferredInterruptState:
    pending: bool = False

    def defer(self, _signum: int, _frame: Any) -> None:
        self.pending = True

    def raise_if_pending(self) -> None:
        if self.pending:
            self.pending = False
            raise KeyboardInterrupt


@contextmanager
def _defer_guarded_interrupts() -> Any:
    """Finish watchdog ownership transitions before delivering an interrupt."""

    state = _DeferredInterruptState()
    previous: dict[signal.Signals, Any] = {}

    try:
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            previous[selected_signal] = signal.getsignal(selected_signal)
            signal.signal(selected_signal, state.defer)
        yield state
    finally:
        for selected_signal, handler in previous.items():
            signal.signal(selected_signal, handler)
    state.raise_if_pending()


@contextmanager
def _guarded_cleanup_boundary(enabled: bool) -> Any:
    """Keep cleanup shielded while allowing immediate capture interruption."""

    if enabled:
        with _defer_guarded_interrupts() as deferred_interrupts:
            yield deferred_interrupts
    else:
        yield None


@contextmanager
def _capture_interrupt_boundary(
    deferred_interrupts: _DeferredInterruptState | None,
) -> Any:
    """Deliver an already-pending or new signal before more capture work."""

    if deferred_interrupts is None:
        yield
        return
    with _guarded_interruptible(deferred_interrupts):
        deferred_interrupts.raise_if_pending()
        yield


def _capture_prospective_with_handoff(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    *,
    guard_signals: bool = False,
) -> Path:
    """Snapshot P1 and only then enter the guarded remote workflow."""

    return _capture_prospective_with_handoff_impl(
        args,
        commit,
        identity,
        guard_signals=guard_signals,
    )


def _capture_prospective_with_handoff_impl(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    *,
    guard_signals: bool = False,
) -> Path:

    expected_source_archive_sha256 = _prospective_source_archive_pin(args, commit)
    if identity is None:
        raise RemoteCaptureError("prospective capture requires an SSH identity file")
    handoff_root = Path(args.prospective_handoff_root).expanduser().absolute()
    with tempfile.TemporaryDirectory(
        prefix="inferdrome-prospective-inputs-"
    ) as temporary:
        temporary_root = Path(temporary)
        handoff = prospective_handoff.snapshot_handoff(
            handoff_root,
            expected_manifest_sha256=args.expected_handoff_manifest_sha256,
            expected_workload_sha256=args.expected_workload_sha256,
        )
        handoff = prospective_handoff.create_handoff_archive(
            handoff,
            temporary_root / "handoff.tar.gz",
        )
        _validate_prospective_snapshot(handoff, temporary_root / "validated-handoff")
        source_archive = temporary_root / "repo.tar"
        source_archive_sha256, source_archive_bytes = _create_source_archive(
            source_archive,
            commit,
            expected_archive_sha256=expected_source_archive_sha256,
        )
        print(
            "Prospective P1 snapshot prepared before SSH/provider action: "
            f"{handoff.manifest_sha256} manifest, "
            f"{handoff.workload_sha256} workload"
        )
        print(
            f"Exact source archive prepared: {source_archive_sha256} "
            f"({source_archive_bytes} bytes)"
        )
        watchdog: lambda_gpu_guard.WatchdogHandle | None = None
        captured: tuple[Path, int] | None = None
        termination: lambda_gpu_guard.TerminationResult | None = None
        with _guarded_cleanup_boundary(guard_signals) as deferred_interrupts:
            try:
                with _defer_guarded_interrupts():
                    watchdog = _arm_lambda_watchdog(args)
                termination_deadline = getattr(
                    getattr(watchdog, "cost_window", None),
                    "deadline",
                    None,
                )
                with _capture_interrupt_boundary(deferred_interrupts):
                    captured = _capture_prospective_over_ssh(
                        args,
                        commit,
                        identity,
                        source_archive,
                        source_archive_sha256,
                        handoff,
                        termination_deadline=termination_deadline,
                    )
            finally:
                if watchdog is not None:
                    capture_failed = sys.exc_info()[0] is not None
                    if deferred_interrupts is None:
                        with _defer_guarded_interrupts():
                            termination = _terminate_guarded_capture(
                                watchdog,
                                capture_failed=capture_failed,
                                capture_label="prospective capture",
                            )
                    else:
                        termination = _terminate_guarded_capture(
                            watchdog,
                            capture_failed=capture_failed,
                            capture_label="prospective capture",
                        )
        if captured is None:
            raise AssertionError
        capture_path, source_archive_size_bytes = captured
        verified_session_identities = _materialize_prospective_capture(
            capture_path,
            handoff,
            commit=commit,
            source_archive_sha256=source_archive_sha256,
            source_archive_size_bytes=source_archive_size_bytes,
        )
        return _finalize_prospective_capture(
            capture_path,
            handoff,
            guarded_termination=termination,
            expected_commit=commit,
            expected_source_archive_sha256=source_archive_sha256,
            expected_source_archive_size_bytes=source_archive_size_bytes,
            expected_host_identity_sha256="sha256:" + args.host_key_sha256,
            verified_session_identities=verified_session_identities,
        )


def _capture(args: argparse.Namespace, commit: str, identity: Path | None) -> Path:
    """Protect the active instance before rebuilding the checked source payload."""

    interruptible = _prospective_requested(args) or _lambda_guard_requested(args)
    if interruptible:
        with _guarded_interruptible():
            if _prospective_requested(args):
                return _capture_prospective_with_handoff(
                    args,
                    commit,
                    identity,
                    guard_signals=True,
                )
            return _capture_with_source(
                args,
                commit,
                identity,
                guard_signals=True,
            )
    return _capture_with_source(args, commit, identity)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture the Inferdrome real-GPU proof pack over SSH"
    )
    parser.add_argument("destination", nargs="?", type=_validate_destination)
    parser.add_argument("--check", action="store_true", help="check local assets only")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the bounded workflow without contacting a host",
    )
    parser.add_argument(
        "--prospective",
        action="store_true",
        help="transport the externally frozen three-case P1 handoff",
    )
    parser.add_argument(
        "--prospective-handoff-root",
        "--handoff-root",
        dest="prospective_handoff_root",
        help="complete local ExitSpec P1 handoff directory",
    )
    parser.add_argument(
        "--expected-handoff-manifest-sha256",
        type=_prospective_digest,
        help="operator-observed handoff manifest digest",
    )
    parser.add_argument(
        "--expected-workload-sha256",
        type=_prospective_digest,
        help="operator-observed real-GPU workload digest",
    )
    parser.add_argument(
        "--expected-source-archive-sha256",
        type=_tagged_prospective_digest,
        help="required independent digest pin for the exact prospective source archive",
    )
    parser.add_argument("--expected-commit")
    parser.add_argument(
        "--resume-qwen3-finalization",
        metavar="CAPTURE_PATH",
        help="offline-resume a retained Qwen3 capture after confirmed termination",
    )
    parser.add_argument(
        "--lambda-guard-state-directory",
        help="retained guard directory used only by offline Qwen3 finalization",
    )
    parser.add_argument("--identity-file")
    parser.add_argument(
        "--host-key-sha256",
        type=_host_key_digest,
        help="required SHA-256 hex digest of the pinned known_hosts bytes",
    )
    parser.add_argument(
        "--host-key-file",
        help="optional operator-supplied pinned known_hosts bytes",
    )
    parser.add_argument("--port", type=_port, default=22)
    parser.add_argument("--gpu-index", type=_gpu_index, default=0)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_startup_timeout,
        default=900,
    )
    parser.add_argument(
        "--remote-timeout-seconds",
        type=_remote_timeout,
        default=_DEFAULT_REMOTE_TIMEOUT_SECONDS,
        help="bounded host workload time; this does not terminate the cloud instance",
    )
    parser.add_argument(
        "--managed-capability-profile",
        choices=(_QWEN3_PROFILE_ID,),
        help="explicit bounded campaign profile; omission preserves the legacy pack",
    )
    parser.add_argument(
        "--qwen3-gpu-tier",
        choices=QWEN3_IMPLEMENTED_GPU_TIERS,
        help="exact implemented Qwen3 campaign GPU tier",
    )
    parser.add_argument(
        "--output-root",
        default=str(REPOSITORY_ROOT / "gpu-proof-retrieved"),
    )
    parser.add_argument(
        "--lambda-instance-id",
        type=lambda_gpu_guard.parse_instance_id,
        help=(
            "optional Lambda instance ID; otherwise resolve the SSH host "
            "through the API"
        ),
    )
    parser.add_argument(
        "--lambda-instance-type-name",
        type=_lambda_instance_type_name,
        help=(
            "exact instance_type_name reported by the Lambda API; required for "
            "Qwen3 campaign capture"
        ),
    )
    parser.add_argument(
        "--lambda-hourly-rate-usd",
        type=lambda_gpu_guard.parse_hourly_rate,
        help="displayed Lambda hourly rate; checked against the API",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=lambda_gpu_guard.parse_max_cost,
        help="operator spend budget used for the buffered termination deadline",
    )
    parser.add_argument(
        "--lambda-billing-started-at",
        type=lambda_gpu_guard.parse_utc_timestamp,
        help="required actual provider billing start in ISO 8601",
    )
    parser.add_argument(
        "--lambda-guard-state-root",
        default=str(Path.home() / ".inferdrome" / "lambda-guards"),
    )
    parser.add_argument(
        "--remote-state-root",
        type=_bounded_remote_path,
        help="prepared host state directory used by prospective capture",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        _static_check()
        if args.resume_qwen3_finalization is not None:
            if (
                args.destination is not None
                or args.check
                or args.dry_run
                or args.managed_capability_profile is not None
                or args.qwen3_gpu_tier is not None
                or args.identity_file is not None
                or args.host_key_file is not None
                or args.host_key_sha256 is not None
                or args.expected_source_archive_sha256 is not None
                or _lambda_guard_requested(args)
            ):
                raise RemoteCaptureError(
                    "offline Qwen3 finalization does not accept capture "
                    "or cloud options"
                )
            if args.expected_commit is None:
                raise RemoteCaptureError(
                    "offline Qwen3 finalization requires --expected-commit"
                )
            if args.lambda_guard_state_directory is None:
                raise RemoteCaptureError(
                    "offline Qwen3 finalization requires the Lambda guard "
                    "state directory"
                )
            commit = _require_checkout(args.expected_commit)
            _resume_qwen3_finalization(
                Path(args.resume_qwen3_finalization),
                commit=commit,
                guard_state_directory=Path(args.lambda_guard_state_directory),
            )
            return 0
        if args.lambda_guard_state_directory is not None:
            raise RemoteCaptureError(
                "--lambda-guard-state-directory requires offline Qwen3 finalization"
            )
        if args.check:
            if (
                args.destination is not None
                or args.dry_run
                or args.prospective
                or args.prospective_handoff_root is not None
                or args.expected_handoff_manifest_sha256 is not None
                or args.expected_workload_sha256 is not None
                or args.expected_source_archive_sha256 is not None
                or args.host_key_file is not None
                or args.host_key_sha256 is not None
                or args.managed_capability_profile is not None
                or args.qwen3_gpu_tier is not None
                or args.lambda_instance_type_name is not None
                or _lambda_guard_requested(args)
            ):
                raise RemoteCaptureError(
                    "--check does not accept capture mode, destination, --dry-run, "
                    "profile, or Lambda guard"
                )
            print("real-GPU remote capture assets: OK")
            return 0
        if args.destination is None:
            raise RemoteCaptureError("an SSH destination is required")
        _validate_capture_mode(args)
        identity = _require_identity(args.identity_file)
        commit = _require_checkout(args.expected_commit)
        if args.dry_run:
            _dry_run(args, commit, identity)
        else:
            _capture(args, commit, identity)
    except KeyboardInterrupt:
        print(
            "remote-real-gpu-capture: interrupted; guarded cleanup was requested",
            file=sys.stderr,
        )
        return 130
    except RemoteCaptureError as error:
        print(f"remote-real-gpu-capture: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
