"""Private, finite study bundles anchored to one held directory descriptor.

Only generated basenames are accepted. Outputs never replace a prior artifact;
an interrupted directory is retained for inspection, never resumed implicitly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import TracebackType

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.files import MAX_RESULT_BYTES

MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
_NAMES = re.compile(
    r"(?:plan\.json|manifest\.json|report\.json|report\.md|trial-[0-9]{4}\.json)"
)


def trial_filename(index: int) -> str:
    if type(index) is not int or not 0 <= index < 256:
        raise EvaluationError("study trial index violates its bound")
    return f"trial-{index:04d}.json"


class StudyDirectory:
    """Own a new output directory, or read one existing private bundle."""

    def __init__(self, root: SafeDirFD, *, budget: int, writable: bool) -> None:
        self._root = root
        self._budget = budget
        self._writable = writable
        self._bytes = 0

    @classmethod
    def create(cls, path: Path, *, budget: int) -> StudyDirectory:
        if type(budget) is not int or not 1 <= budget <= MAX_BUNDLE_BYTES:
            raise EvaluationError("study output budget violates its bound")
        absolute = path.absolute()
        if absolute.name in {"", ".", ".."} or "\\" in absolute.name:
            raise EvaluationError("study directory name is invalid")
        parent = SafeDirFD.open(absolute.parent)
        descriptor: int | None = None
        root: SafeDirFD | None = None
        try:
            os.mkdir(absolute.name, 0o700, dir_fd=parent.fd)
            before = parent.stat_child(absolute.name)
            descriptor = os.open(
                absolute.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent.fd,
            )
            after = os.fstat(descriptor)
            named = parent.stat_child(absolute.name)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
                or after.st_mode & 0o077
                or os.listdir(descriptor)
            ):
                raise EvaluationError("study directory changed during reservation")
            root = SafeDirFD.from_inherited_fd(descriptor)
            parent.fsync()
            return cls(root, budget=budget, writable=True)
        except BaseException:
            if root is not None:
                root.close()
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            parent.close()

    @classmethod
    def open(cls, path: Path, *, budget: int = MAX_BUNDLE_BYTES) -> StudyDirectory:
        if type(budget) is not int or not 1 <= budget <= MAX_BUNDLE_BYTES:
            raise EvaluationError("study input budget violates its bound")
        root = SafeDirFD.open(path.absolute())
        try:
            if os.fstat(root.fd).st_mode & 0o077:
                raise EvaluationError("study directory is not private")
            return cls(root, budget=budget, writable=False)
        except BaseException:
            root.close()
            raise

    def __enter__(self) -> StudyDirectory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._root.close()

    def _check(self, name: str, limit: int) -> None:
        self._root.assert_open()
        if (
            not _NAMES.fullmatch(name)
            or (name.startswith("trial-") and int(name[6:10]) >= 256)
            or type(limit) is not int
            or not 1 <= limit <= MAX_RESULT_BYTES
            or os.fstat(self._root.fd).st_mode & 0o077
        ):
            raise EvaluationError("study artifact violates its ownership or bound")

    def write(self, name: str, content: bytes, *, limit: int) -> None:
        self._check(name, limit)
        if (
            not self._writable
            or not 1 <= len(content) <= limit
            or self._bytes + len(content) > self._budget
        ):
            raise EvaluationError("study output exceeds its declared byte bound")
        descriptor = self._root.open_child(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        committed = False
        try:
            view = memoryview(content)
            while view:
                count = os.write(descriptor, view[:65_536])
                if count <= 0:
                    raise EvaluationError("study output write failed")
                view = view[count:]
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            self._root.validated_regular_child(name, descriptor=descriptor)
            self._root.fsync()
            committed = True
            self._bytes += len(content)
        finally:
            try:
                if not committed:
                    self._root.unlink_child(name, expected=os.fstat(descriptor))
            finally:
                os.close(descriptor)

    def read(self, name: str, *, limit: int) -> bytes:
        self._check(name, limit)
        descriptor = self._root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = self._root.validated_regular_child(name, descriptor=descriptor)
            if (
                before.st_mode & 0o077
                or not 1 <= before.st_size <= limit
                or self._bytes + before.st_size > self._budget
            ):
                raise EvaluationError("study input exceeds its ownership or byte bound")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65_536))
                if not chunk:
                    raise EvaluationError("study artifact changed during read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise EvaluationError("study artifact changed during read")
            after = self._root.validated_regular_child(name, descriptor=descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise EvaluationError("study artifact changed during read")
            self._bytes += before.st_size
            return b"".join(chunks)
        finally:
            os.close(descriptor)
