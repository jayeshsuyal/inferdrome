"""Bounded, no-follow input reads used during resolution."""

import os
import stat
from pathlib import Path

from inferdrome.errors import SourceInputError


def read_bounded_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    """Read one regular file once without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SourceInputError(f"{label} is unavailable or unsafe") from None

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SourceInputError(f"{label} must be a regular file")
        if file_stat.st_size > limit:
            raise SourceInputError(f"{label} exceeds its byte limit")

        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            raise SourceInputError(f"{label} exceeds its byte limit")
        return content
    finally:
        os.close(descriptor)


def resolve_safe_child(root: Path, relative_path: str, *, label: str) -> Path:
    """Resolve an already pattern-validated child while rejecting symlinks."""

    current = root
    for component in Path(relative_path).parts:
        current = current / component
        try:
            if current.is_symlink():
                raise SourceInputError(f"{label} cannot traverse a symlink")
        except OSError:
            raise SourceInputError(f"{label} is unavailable or unsafe") from None
    return current
