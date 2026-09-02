"""Small no-follow, descriptor-anchored filesystem primitives for v2 journals.

The v2 watchdog, supervisor, and disk-cleanup journals are security boundaries:
checking a directory path and later reopening children by that path permits a
rename/symlink race. SafeDirFD holds one verified directory descriptor and
performs every child operation relative to that descriptor. It intentionally
has no deployment-model imports so it can be used without creating lifecycle
or provider import cycles.
"""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_NOFOLLOW: Final[int | None] = getattr(os, "O_NOFOLLOW", None)
_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY: Final[int | None] = getattr(os, "O_DIRECTORY", None)
# macOS exposes its system temporary hierarchy through these fixed aliases.
# They are not user-configurable journal links: each is accepted only when it
# resolves to its platform-owned, exact target before the fd walk starts.
# All caller-controlled intermediate symlinks remain a hard failure.
_TRUSTED_SYSTEM_DIRECTORY_ALIASES: Final[dict[str, str]] = {
    "/var": "/private/var",
    "/tmp": "/private/tmp",
}


class SafeDirFSError(OSError):
    """A requested journal directory or child is not safe to use."""


def _require_dirfd_primitives() -> tuple[int, int]:
    """Return the mandatory POSIX primitives or fail closed.

    A best-effort fallback such as opening a child through ``root / name`` is
    not safe for these journals: it reintroduces a rename/symlink race between
    verifying the root and using the child.  Keep this check at operation time
    rather than import time so a constrained runtime cannot accidentally start
    with a partially available implementation.
    """

    if (
        not isinstance(_NOFOLLOW, int)
        or _NOFOLLOW == 0
        or not isinstance(_DIRECTORY, int)
        or _DIRECTORY == 0
    ):
        raise SafeDirFSError("secure no-follow directory primitives unavailable")

    required = (os.open, os.stat, os.unlink)
    if any(
        operation not in getattr(os, "supports_dir_fd", frozenset())
        for operation in required
    ):
        raise SafeDirFSError("secure dir_fd primitives unavailable")
    if os.stat not in getattr(os, "supports_follow_symlinks", frozenset()):
        raise SafeDirFSError("secure no-follow stat primitive unavailable")
    return _NOFOLLOW, _DIRECTORY


def _child_name(name: str) -> str:
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or "\x00" in name
    ):
        raise SafeDirFSError("unsafe journal child name")
    return name


def _assert_safe_directory(
    metadata: os.stat_result, *, inherited: bool = False
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_uid != os.getuid()
    ):
        label = "inherited journal root" if inherited else "journal root"
        raise SafeDirFSError(f"unsafe {label}")


def _assert_safe_regular_child(metadata: os.stat_result) -> None:
    """Require a private, non-hard-linked ordinary journal child."""

    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise SafeDirFSError("unsafe journal child")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _canonical_system_root_alias(root: Path) -> Path:
    """Translate only fixed macOS system aliases before no-follow traversal.

    ``/var`` and ``/tmp`` are commonly symlink aliases for ``/private`` on
    macOS.  Refusing every intermediate link is correct for caller-controlled
    paths but would make normal system temporary roots unusable.  Permit only
    these exact aliases, only while their resolved target is the fixed
    platform location; do not generalize this to ``realpath(root)`` because
    that would bless a caller-controlled journal symlink.
    """

    parts = root.parts
    if len(parts) < 2:
        return root
    alias = os.path.join(parts[0], parts[1])
    target = _TRUSTED_SYSTEM_DIRECTORY_ALIASES.get(alias)
    if target is None:
        return root
    try:
        metadata = os.lstat(alias)
    except OSError:
        return root
    if not stat.S_ISLNK(metadata.st_mode) or os.path.realpath(alias) != target:
        return root
    return Path(target, *parts[2:])


def _open_absolute_directory(root: Path, *, nofollow: int, directory: int) -> int:
    """Walk an absolute root one held no-follow directory fd at a time.

    ``O_NOFOLLOW`` on one pathname protects only its final component.  Journal
    roots are supplied as absolute paths, so walk from ``/`` with ``openat`` to
    reject a symlink in *any* component and keep each parent anchored until its
    child descriptor has been acquired.  The final root is owner/mode checked
    by :meth:`SafeDirFD.open`; trusted system ancestors need not be user-owned.
    """

    descriptor: int | None = None
    try:
        descriptor = os.open(
            "/", os.O_RDONLY | directory | nofollow | _CLOEXEC
        )
        for component in root.parts[1:]:
            if component in {"", ".", ".."}:
                raise SafeDirFSError("journal root contains an unsafe path component")
            child = os.open(
                component,
                os.O_RDONLY | directory | nofollow | _CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise


@dataclass
class SafeDirFD:
    """A verified owner-controlled directory held open for child operations."""

    path: Path
    fd: int
    device: int
    inode: int

    @classmethod
    def open(cls, root: Path) -> SafeDirFD:
        if not root.is_absolute():
            raise SafeDirFSError("journal root must be absolute")
        nofollow, directory = _require_dirfd_primitives()
        safe_root = _canonical_system_root_alias(root)
        descriptor: int | None = None
        try:
            descriptor = _open_absolute_directory(
                safe_root, nofollow=nofollow, directory=directory
            )
            metadata = os.fstat(descriptor)
            _assert_safe_directory(metadata)
            return cls(
                path=root,
                fd=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        except BaseException:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            raise

    @classmethod
    def from_inherited_fd(cls, descriptor: int) -> SafeDirFD:
        """Adopt one inherited trusted directory descriptor in an exec worker.

        The worker receives the descriptor through ``pass_fds`` rather than a
        mutable root path.  It duplicates ownership locally and verifies the
        same owner/mode invariants as :meth:`open` before reading any event.
        """

        if not isinstance(descriptor, int) or descriptor < 0:
            raise SafeDirFSError("journal directory descriptor is invalid")
        _require_dirfd_primitives()
        duplicate: int | None = None
        try:
            duplicate = os.dup(descriptor)
            os.set_inheritable(duplicate, False)
            metadata = os.fstat(duplicate)
            _assert_safe_directory(metadata, inherited=True)
            return cls(
                path=Path(f"<inherited-fd-{descriptor}>"),
                fd=duplicate,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        except BaseException:
            if duplicate is not None:
                with suppress(OSError):
                    os.close(duplicate)
            raise

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def assert_open(self) -> None:
        _require_dirfd_primitives()
        if self.fd < 0:
            raise SafeDirFSError("journal directory descriptor is closed")
        metadata = os.fstat(self.fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != self.device
            or metadata.st_ino != self.inode
            or metadata.st_mode & 0o022
            or metadata.st_uid != os.getuid()
        ):
            raise SafeDirFSError("journal directory changed")

    def open_child(self, name: str, flags: int, mode: int = 0o600) -> int:
        self.assert_open()
        nofollow, _ = _require_dirfd_primitives()
        child = _child_name(name)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                child,
                flags | nofollow | _CLOEXEC,
                mode,
                dir_fd=self.fd,
            )
            self.validated_regular_child(child, descriptor=descriptor)
            return descriptor
        except BaseException:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            raise

    def stat_child(self, name: str) -> os.stat_result:
        self.assert_open()
        return os.stat(_child_name(name), dir_fd=self.fd, follow_symlinks=False)

    def validated_regular_child(
        self, name: str, *, descriptor: int | None = None
    ) -> os.stat_result:
        """Validate one named private regular child against this directory fd.

        With ``descriptor`` this also proves that the already-open object is
        still the object named by no-follow ``statat``.  Call it before and
        after a read, truncate, append, or fsync so a replacement cannot be
        mistaken for a durable journal transition.
        """

        self.assert_open()
        child = _child_name(name)
        try:
            named = self.stat_child(child)
            _assert_safe_regular_child(named)
            if descriptor is None:
                return named
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_mode & 0o022
                or opened.st_uid != os.getuid()
            ):
                raise SafeDirFSError("unsafe journal child")
            if not _same_inode(named, opened):
                raise SafeDirFSError("journal child changed")
            _assert_safe_regular_child(opened)
            return opened
        except SafeDirFSError:
            raise
        except FileNotFoundError:
            raise
        except OSError as error:
            raise SafeDirFSError("journal child unavailable") from error

    def unlink_child(
        self,
        name: str,
        *,
        expected: os.stat_result | None = None,
    ) -> None:
        """Unlink a verified private child relative to the held directory fd.

        ``expected`` lets callers preserve the identity they checked before a
        repair action.  ``unlinkat`` does not follow a final symlink; the
        identity check turns a visible replacement into a fail-closed error.
        """

        self.assert_open()
        child = _child_name(name)
        current = self.validated_regular_child(child)
        if expected is not None and not _same_inode(current, expected):
            raise SafeDirFSError("journal child changed")
        try:
            os.unlink(child, dir_fd=self.fd)
        except (NotImplementedError, TypeError) as error:
            raise SafeDirFSError("secure unlink primitive unavailable") from error

    def replace_child(
        self,
        source: str,
        destination: str,
        *,
        expected_source: os.stat_result | None = None,
        expected_destination: os.stat_result | None = None,
    ) -> None:
        """Atomically replace one verified child using only directory fds."""

        self.assert_open()
        source_name = _child_name(source)
        destination_name = _child_name(destination)
        current_source = self.validated_regular_child(source_name)
        if expected_source is not None and not _same_inode(
            current_source, expected_source
        ):
            raise SafeDirFSError("journal child changed")
        if expected_destination is not None:
            current_destination = self.validated_regular_child(destination_name)
            if not _same_inode(current_destination, expected_destination):
                raise SafeDirFSError("journal child changed")
        try:
            os.replace(
                source_name,
                destination_name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
            )
        except (NotImplementedError, TypeError) as error:
            raise SafeDirFSError("secure replace primitive unavailable") from error
        self.validated_regular_child(destination_name)
        self.fsync()

    def fsync(self) -> None:
        self.assert_open()
        os.fsync(self.fd)

    def child_path_for_configuration(self, name: str) -> Path:
        """Return a display-only path after validating a bounded child name."""

        return self.path / _child_name(name)
