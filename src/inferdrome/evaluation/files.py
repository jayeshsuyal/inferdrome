"""Private descriptor-anchored input and no-replace measurement-file output.

This is a local measurement artifact, not an independently sealed R1 package.
A reserved empty/partial file is never a valid report. Handled failures remove
only that reservation; a crash may leave it for the owner to inspect.
"""

import os
from pathlib import Path
from types import TracebackType

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.evaluation.contracts import MAX_INPUT_BYTES, EvaluationError

MAX_RESULT_BYTES = 64 * 1024 * 1024


def read_input(path: Path) -> bytes:
    parent = SafeDirFD.open(path.absolute().parent)
    descriptor: int | None = None
    try:
        descriptor = parent.open_child(path.name, os.O_RDONLY | os.O_NONBLOCK)
        before = parent.validated_regular_child(path.name, descriptor=descriptor)
        if not 1 <= before.st_size <= MAX_INPUT_BYTES:
            raise EvaluationError("evaluation input exceeds its file bound")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            data = os.read(descriptor, min(remaining, 65_536))
            if not data:
                raise EvaluationError("evaluation input changed during read")
            chunks.append(data)
            remaining -= len(data)
        if os.read(descriptor, 1):
            raise EvaluationError("evaluation input changed during read")
        after = parent.validated_regular_child(path.name, descriptor=descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise EvaluationError("evaluation input changed during read")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        parent.close()


class OutputFile:
    """Reserve before sockets; never replace an existing destination."""

    def __init__(self, path: Path) -> None:
        self._parent = SafeDirFD.open(path.absolute().parent)
        self._name = path.name
        self._committed = False
        try:
            self._descriptor = self._parent.open_child(
                self._name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except BaseException:
            self._parent.close()
            raise

    def __enter__(self) -> "OutputFile":
        return self

    def write(self, content: bytes) -> None:
        if self._committed or not 1 <= len(content) <= MAX_RESULT_BYTES:
            raise EvaluationError("evaluation output violates its bound")
        self._parent.validated_regular_child(self._name, descriptor=self._descriptor)
        view = memoryview(content)
        while view:
            count = os.write(self._descriptor, view[:65_536])
            if count <= 0:
                raise EvaluationError("evaluation output write failed")
            view = view[count:]
        os.fchmod(self._descriptor, 0o400)
        os.fsync(self._descriptor)
        self._parent.validated_regular_child(self._name, descriptor=self._descriptor)
        self._parent.fsync()
        self._committed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if not self._committed:
                expected = os.fstat(self._descriptor)
                self._parent.unlink_child(self._name, expected=expected)
        finally:
            os.close(self._descriptor)
            self._parent.close()
