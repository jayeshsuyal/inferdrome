"""Small local CLI for adapting and verifying attached-router receipts."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from inferdrome.errors import VerificationError
from inferdrome.external_router.llmd import (
    MAX_ATTACHED_RECORD_BYTES,
    MAX_ROUTER_CONFIG_BYTES,
    ExternalRouterAdapterError,
    adapt_llmd_attached_record,
)
from inferdrome.external_router.verifier import verify_external_router_evidence


class _InputError(ValueError):
    """A local CLI input cannot be safely read as one regular file snapshot."""


def _required_safe_open_flag(name: str) -> int:
    """Return one required safe-open flag or fail before opening a path."""

    value = getattr(os, name, None)
    if type(value) is not int or value <= 0:
        raise _InputError("required safe file-open flags are unavailable")
    return value


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    no_follow = _required_safe_open_flag("O_NOFOLLOW")
    non_block = _required_safe_open_flag("O_NONBLOCK")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | no_follow
        | non_block
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _InputError("input file is unavailable or unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise _InputError("input file is not one bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise _InputError("input file changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _InputError("input file changed while being read")
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
            raise _InputError("input file changed while being read")
        return b"".join(chunks)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _InputError("input file is unavailable or unsafe") from error
        raise _InputError("input file could not be read safely") from error
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m inferdrome.external_router")
    commands = parser.add_subparsers(dest="command", required=True)
    adapt = commands.add_parser(
        "adapt-llmd",
        help="adapt one local llm-d-attached-v1 record to canonical evidence",
    )
    adapt.add_argument("--record", required=True, type=Path)
    adapt.add_argument("--router-config", required=True, type=Path)
    verify = commands.add_parser(
        "verify", help="offline verify canonical external-router evidence"
    )
    verify.add_argument("evidence", type=Path)
    verify.add_argument("--expected-digest")
    return parser


def _emit_canonical(content: bytes) -> None:
    sys.stdout.write(content.decode("utf-8"))


def _emit_summary(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    """Run a local file-only adapter command without router or provider access."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "adapt-llmd":
            captured = adapt_llmd_attached_record(
                _read_regular_file(
                    arguments.record,
                    maximum_bytes=MAX_ATTACHED_RECORD_BYTES,
                ),
                router_config_bytes=_read_regular_file(
                    arguments.router_config,
                    maximum_bytes=MAX_ROUTER_CONFIG_BYTES,
                ),
            )
            _emit_canonical(captured.canonical_bytes)
            return 0
        if arguments.command == "verify":
            verified = verify_external_router_evidence(
                _read_regular_file(
                    arguments.evidence,
                    maximum_bytes=MAX_ATTACHED_RECORD_BYTES,
                ),
                expected_digest=arguments.expected_digest,
            )
            _emit_summary(
                {
                    "evidence_admissibility": verified.evidence.evidence_admissibility,
                    "retained_digest": verified.retained_digest,
                    "valid": True,
                }
            )
            return 0
    except (ExternalRouterAdapterError, _InputError, VerificationError) as error:
        parser.error(str(error))
    raise AssertionError("external-router command dispatch is incomplete")
