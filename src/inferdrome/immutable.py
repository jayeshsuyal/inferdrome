"""Crash-safe publication helpers for one-file immutable directories."""

import ctypes
import errno
import fcntl
import os
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

STAGING_DIRECTORY = ".inferdrome-staging"
STAGING_PREFIX = ".inferdrome-stage-"


def is_internal_staging_entry(name: str) -> bool:
    return name == STAGING_DIRECTORY or name.startswith(STAGING_PREFIX)


def _real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and len(value.encode("utf-8")) <= 240
        and "/" not in value
        and "\x00" not in value
        and Path(value).name == value
    )


def _write_new(path: Path, content: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short immutable-artifact write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(errno.ENOTDIR, "directory changed during publication")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _raise_rename_error() -> None:
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


@contextmanager
def _publication_lock(staging_root: Path) -> Iterator[None]:
    lock_path = staging_root / ".publication.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError(errno.EINVAL, "publication lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
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
        if rename(-100, source_bytes, -100, destination_bytes, 1) != 0:
            _raise_rename_error()
        return
    if sys.platform == "darwin":
        try:
            os.lstat(destination)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
        else:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
        # Darwin's RENAME_EXCL rejects a non-empty read-only source directory.
        # The caller holds the root publication lock around this rename.
        os.rename(source, destination)
        return
    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace rename is unsupported on this platform",
    )


def publish_immutable_directory(
    *,
    root: Path,
    artifact_id: str,
    filename: str,
    content: bytes,
) -> Path:
    """Stage privately, freeze, and atomically publish one descriptor directory."""

    if (
        not _safe_component(artifact_id)
        or not _safe_component(filename)
        or not isinstance(content, bytes)
    ):
        raise ValueError("immutable artifact publication input is invalid")
    selected_root = root.absolute()
    selected_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _real_directory(selected_root):
        raise OSError(errno.ENOTDIR, "artifact root must be a real directory")
    staging_root = selected_root / STAGING_DIRECTORY
    staging_root.mkdir(mode=0o700, exist_ok=True)
    if not _real_directory(staging_root):
        raise OSError(errno.ENOTDIR, "artifact staging root must be a real directory")

    staging = selected_root / (
        f"{STAGING_PREFIX}{artifact_id}.{secrets.token_hex(16)}"
    )
    destination = selected_root / artifact_id
    staging.mkdir(mode=0o700)
    try:
        descriptor = staging / filename
        _write_new(descriptor, content)
        descriptor.chmod(0o400)
        staging.chmod(0o500)
        _fsync_directory(staging)
        _fsync_directory(selected_root)
        with _publication_lock(staging_root):
            _rename_no_replace(staging, destination)
        _fsync_directory(selected_root)
    except Exception:
        # A private, uniquely named staging directory may remain after failure.
        # It is never treated as a published artifact and cannot poison the ID.
        raise
    return destination
