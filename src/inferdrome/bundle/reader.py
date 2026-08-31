"""Bounded no-follow reader for attacker-controlled bundle directories."""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inferdrome.errors import VerificationError
from inferdrome.limits import WorkBudget, collect_bounded
from inferdrome.parsing import (
    BoundedParseError,
    bounded_json_float,
    bounded_json_int,
    validate_json_structure,
)

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class BundleLimits:
    max_files: int = 64
    max_directories: int = 32
    max_file_bytes: int = 268_435_456
    max_total_bytes: int = 536_870_912
    max_jsonl_line_bytes: int = 8_388_608
    max_jsonl_records: int = 1_000_000
    max_depth: int = 8

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_directories,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_jsonl_line_bytes,
            self.max_jsonl_records,
            self.max_depth,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("bundle limits must be positive integers")


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    size_bytes: int
    mode: int
    device: int
    inode: int
    link_count: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class ScannedDirectory:
    path: Path
    mode: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


def _file_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mode,
        file_stat.st_nlink,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _directory_identity(directory_stat: os.stat_result) -> tuple[int, ...]:
    return (
        directory_stat.st_dev,
        directory_stat.st_ino,
        directory_stat.st_mode,
        directory_stat.st_mtime_ns,
        directory_stat.st_ctime_ns,
    )


def _validate_relative_path(relative_path: str) -> None:
    parts = relative_path.split("/")
    if (
        not relative_path
        or len(relative_path) > 512
        or len(parts) > 8
        or any(not _SAFE_COMPONENT.fullmatch(part) for part in parts)
    ):
        raise VerificationError("bundle contains an unsafe relative path")


class BundleReader:
    def __init__(
        self,
        root: Path,
        *,
        limits: BundleLimits | None = None,
        require_immutable: bool = True,
        work_budget: WorkBudget | None = None,
    ) -> None:
        self.root = root.absolute()
        self.limits = limits or BundleLimits()
        self.require_immutable = require_immutable
        self.work_budget = work_budget
        self.files: dict[str, ScannedFile] = {}
        self.directories: set[str] = set()
        self.total_bytes = 0
        self._root_node: ScannedDirectory | None = None
        self._directory_nodes: dict[str, ScannedDirectory] = {}
        self._content_cache: dict[str, bytes] = {}
        self._scan()
        if self.work_budget is not None:
            self.work_budget.reserve(bytes_=self.total_bytes)

    def _scan(self) -> None:
        try:
            root_stat = self.root.stat(follow_symlinks=False)
        except OSError:
            raise VerificationError("bundle root is unavailable") from None
        if not stat.S_ISDIR(root_stat.st_mode) or self.root.is_symlink():
            raise VerificationError("bundle root must be a real directory")
        if self.require_immutable and stat.S_IMODE(root_stat.st_mode) & 0o222:
            raise VerificationError("sealed bundle root is writable")
        self._root_node = ScannedDirectory(
            path=self.root,
            mode=root_stat.st_mode,
            device=root_stat.st_dev,
            inode=root_stat.st_ino,
            modified_ns=root_stat.st_mtime_ns,
            changed_ns=root_stat.st_ctime_ns,
        )

        casefold_paths: set[str] = set()
        pending = [(self.root, "", 0)]
        while pending:
            directory, relative_directory, depth = pending.pop()
            if depth > self.limits.max_depth:
                raise VerificationError("bundle directory depth exceeds limit")
            try:
                with os.scandir(directory) as iterator:
                    remaining_entries = (
                        self.limits.max_files
                        + self.limits.max_directories
                        - len(self.files)
                        - len(self.directories)
                    )
                    entries = sorted(
                        collect_bounded(
                            iterator,
                            limit=remaining_entries,
                            error=lambda: VerificationError(
                                "bundle entry count exceeds combined limits"
                            ),
                        ),
                        key=lambda entry: entry.name,
                    )
            except OSError:
                raise VerificationError("bundle directory cannot be scanned") from None
            for entry in entries:
                relative_path = (
                    f"{relative_directory}/{entry.name}"
                    if relative_directory
                    else entry.name
                )
                _validate_relative_path(relative_path)
                folded = relative_path.casefold()
                if folded in casefold_paths:
                    raise VerificationError("bundle paths collide case-insensitively")
                casefold_paths.add(folded)

                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    raise VerificationError(
                        "bundle entry cannot be inspected"
                    ) from None
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise VerificationError("bundle symlinks are forbidden")
                if stat.S_ISDIR(entry_stat.st_mode):
                    directory_is_writable = stat.S_IMODE(entry_stat.st_mode) & 0o222
                    if self.require_immutable and directory_is_writable:
                        raise VerificationError("sealed bundle directory is writable")
                    self.directories.add(relative_path)
                    self._directory_nodes[relative_path] = ScannedDirectory(
                        path=Path(entry.path),
                        mode=entry_stat.st_mode,
                        device=entry_stat.st_dev,
                        inode=entry_stat.st_ino,
                        modified_ns=entry_stat.st_mtime_ns,
                        changed_ns=entry_stat.st_ctime_ns,
                    )
                    if len(self.directories) > self.limits.max_directories:
                        raise VerificationError("bundle directory count exceeds limit")
                    pending.append((Path(entry.path), relative_path, depth + 1))
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise VerificationError("bundle entries must be regular files")
                if entry_stat.st_nlink != 1:
                    raise VerificationError("bundle file hard links are forbidden")
                if self.require_immutable and stat.S_IMODE(entry_stat.st_mode) & 0o222:
                    raise VerificationError("sealed bundle file is writable")
                if entry_stat.st_size > self.limits.max_file_bytes:
                    raise VerificationError("bundle file exceeds size limit")
                self.total_bytes += entry_stat.st_size
                if self.total_bytes > self.limits.max_total_bytes:
                    raise VerificationError("bundle total size exceeds limit")
                self.files[relative_path] = ScannedFile(
                    path=Path(entry.path),
                    size_bytes=entry_stat.st_size,
                    mode=entry_stat.st_mode,
                    device=entry_stat.st_dev,
                    inode=entry_stat.st_ino,
                    link_count=entry_stat.st_nlink,
                    modified_ns=entry_stat.st_mtime_ns,
                    changed_ns=entry_stat.st_ctime_ns,
                )
                if len(self.files) > self.limits.max_files:
                    raise VerificationError("bundle file count exceeds limit")

    def read_bytes(self, relative_path: str) -> bytes:
        cached = self._content_cache.get(relative_path)
        if cached is not None:
            return cached
        scanned = self.files.get(relative_path)
        if scanned is None:
            raise VerificationError("declared bundle artifact is missing")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(scanned.path, flags)
        except OSError:
            raise VerificationError("bundle artifact cannot be opened safely") from None
        try:
            current = os.fstat(descriptor)
            identity = _file_identity(current)
            expected = (
                scanned.device,
                scanned.inode,
                scanned.size_bytes,
                scanned.mode,
                scanned.link_count,
                scanned.modified_ns,
                scanned.changed_ns,
            )
            if identity != expected or not stat.S_ISREG(current.st_mode):
                raise VerificationError("bundle artifact changed during verification")
            chunks: list[bytes] = []
            remaining = scanned.size_bytes
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1_048_576))
                if not chunk:
                    raise VerificationError("bundle artifact was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise VerificationError("bundle artifact grew during verification")
            if _file_identity(os.fstat(descriptor)) != expected:
                raise VerificationError("bundle artifact changed during verification")
            try:
                path_stat = os.lstat(scanned.path)
            except OSError:
                raise VerificationError(
                    "bundle artifact changed during verification"
                ) from None
            if _file_identity(path_stat) != expected:
                raise VerificationError("bundle artifact changed during verification")
            content = b"".join(chunks)
            self._content_cache[relative_path] = content
            return content
        finally:
            os.close(descriptor)

    def assert_unchanged(self) -> None:
        """Reject any tree mutation since this reader's initial snapshot."""

        current = BundleReader(
            self.root,
            limits=self.limits,
            require_immutable=self.require_immutable,
            work_budget=self.work_budget,
        )
        if (
            current._root_node != self._root_node
            or current._directory_nodes != self._directory_nodes
            or current.files != self.files
            or current.total_bytes != self.total_bytes
        ):
            raise VerificationError("bundle changed during verification")


def strict_json_value(content: bytes, *, label: str) -> Any:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise VerificationError(f"{label} is not valid UTF-8") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise VerificationError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise VerificationError(f"{label} contains a non-finite number")

    try:
        validate_json_structure(text)
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=bounded_json_float,
            parse_int=bounded_json_int,
        )
    except VerificationError:
        raise
    except (BoundedParseError, json.JSONDecodeError, RecursionError, ValueError):
        raise VerificationError(f"{label} is not valid bounded JSON") from None


def strict_jsonl_lines(
    content: bytes,
    *,
    label: str,
    max_line_bytes: int,
    max_records: int,
    expected_records: int | None = None,
) -> tuple[bytes, ...]:
    if (
        isinstance(max_records, bool)
        or not isinstance(max_records, int)
        or max_records <= 0
    ):
        raise ValueError("JSONL record limit must be a positive integer")
    if expected_records is not None and (
        isinstance(expected_records, bool)
        or not isinstance(expected_records, int)
        or expected_records < 0
    ):
        raise ValueError("expected JSONL record count must be non-negative")
    if expected_records is not None and expected_records > max_records:
        raise VerificationError(f"{label} record contract exceeds limit")
    if not content.endswith(b"\n"):
        raise VerificationError(f"{label} must end with one newline")
    if not content:
        raise VerificationError(f"{label} cannot be empty")

    # Count with constant auxiliary memory and stop on the N+1 sentinel before
    # the LF-only second pass allocates one object per bounded record.
    record_count = 0
    start = 0
    while start < len(content):
        end = content.find(b"\n", start)
        if end < 0:
            raise VerificationError(f"{label} must end with one newline")
        line_end = end - 1 if end > start and content[end - 1] == 13 else end
        record_count += 1
        if record_count > max_records:
            raise VerificationError(f"{label} record count exceeds limit")
        if line_end == start or line_end - start > max_line_bytes:
            raise VerificationError(f"{label} contains an invalid line size")
        start = end + 1
    if expected_records is not None and record_count != expected_records:
        raise VerificationError(f"{label} record count disagrees with its contract")

    lines: list[bytes] = []
    start = 0
    for _ in range(record_count):
        end = content.find(b"\n", start)
        line_end = end - 1 if end > start and content[end - 1] == 13 else end
        line = content[start:line_end]
        strict_json_value(line, label=label)
        lines.append(line)
        start = end + 1
    return tuple(lines)
