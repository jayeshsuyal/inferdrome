"""Bounded private pilot archives; byte integrity is separate from evidence validity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_request_identity import _read_json

SCHEMA = "inferdrome.breakpoint-pilot-export.v1"
MAX_FILES = 2048
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MANIFEST = "export-inventory.json"
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED", "CLEANUP_UNCONFIRMED"}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _terminal_status(data: bytes) -> None:
    """Check the claimed state, without authenticating the producer or cleanup."""
    try:
        value = _read_json(data)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(
            "terminal session status is invalid or ambiguous JSON"
        ) from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("status"), str)
        or value["status"] not in _TERMINAL
    ):
        raise ValueError("collection requires a terminal session status")


def _safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or len(name) > 512
        or path.is_absolute()
        or str(path) != name
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in name
        or any(ord(character) < 32 or ord(character) > 126 for character in name)
    ):
        raise ValueError("archive path must be a bounded relative POSIX filename")


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("private export paths must not traverse symlinks")


def _read(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    _no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise ValueError("export requires bounded, owned regular files")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("export input changed while reading")
        return data


def _files(root: Path, prefix: str) -> dict[str, Path]:
    _no_links(root)
    observed = root.stat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid():
        raise ValueError("export root must be an owned directory")
    result: dict[str, Path] = {}
    pending = [root]
    directories = 0
    while pending:
        directory = pending.pop()
        _no_links(directory)
        directories += 1
        if directories > MAX_FILES:
            raise ValueError("too many export directories")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                observed = entry.stat(follow_symlinks=False)
                if observed.st_uid != os.getuid():
                    raise ValueError("export input must be owned by this user")
                if stat.S_ISDIR(observed.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(observed.st_mode):
                    name = prefix + "/" + path.relative_to(root).as_posix()
                    _safe_name(name)
                    result[name] = path
                    if len(result) > MAX_FILES:
                        raise ValueError("too many export files")
                else:
                    raise ValueError("export rejects links and special files")
    return result


def archive_digest(path: Path) -> str:
    """Hash an owned archive without loading it into memory."""
    _no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        observed = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or observed.st_size > MAX_TOTAL_BYTES + 4 * 1024 * 1024
        ):
            raise ValueError("archive is not a bounded owned regular file")
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("archive changed during hashing")
        return "sha256:" + digest.hexdigest()


def pack(prepared_dir: Path, collection_dir: Path, output: Path) -> dict[str, Any]:
    """Seal completed or failed collection bytes without publishing raw data."""
    _no_links(output)
    destination = Path(os.path.abspath(output))
    for root in (prepared_dir, collection_dir):
        if destination.is_relative_to(Path(os.path.abspath(root))):
            raise ValueError("archive must be outside both input directories")
    if prepared_dir.absolute() == collection_dir.absolute():
        raise ValueError("prepared and collection directories must differ")
    files = _files(prepared_dir, "prepared") | _files(collection_dir, "collection")
    if not {
        "prepared/inventory.json",
        "collection/session-status.json",
    }.issubset(files):
        raise ValueError("prepared inventory and terminal session status are required")
    if len(files) > MAX_FILES:
        raise ValueError("too many export files")
    _terminal_status(_read(files["collection/session-status.json"], 1024 * 1024))
    inventory: list[dict[str, Any]] = []
    total = 0
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    # An interrupted write stays at this exclusive path; no completion receipt
    # is returned. Re-run with a new destination after retaining the failure.
    with os.fdopen(fd, "wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in sorted(files.items()):
                data = _read(path)
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("export exceeds total byte limit")
                inventory.append(
                    {"path": name, "bytes": len(data), "sha256": _digest(data)}
                )
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, data)
            manifest = {
                "schema": SCHEMA,
                "scope": "PRIVATE_BYTE_INTEGRITY_ONLY",
                "files": inventory,
            }
            info = zipfile.ZipInfo(_MANIFEST)
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, canonical_json_bytes(manifest) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    current_files = _files(prepared_dir, "prepared") | _files(
        collection_dir, "collection"
    )
    if files != current_files:
        raise ValueError("export tree changed during packaging")
    for item in inventory:
        if _digest(_read(files[item["path"]])) != item["sha256"]:
            raise ValueError("export payload changed during packaging")
    return verify_archive(output, archive_digest(output))


def verify_archive(archive_path: Path, expected_sha256: str) -> dict[str, Any]:
    """Independently verify retrieved archive bytes against a retained digest.

    Nothing is extracted or executed. This does not authenticate a producer,
    validate reset claims, or turn a synthetic run into GPU evidence.
    """
    if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
        raise ValueError("expected SHA-256 must be retained outside the archive")
    if archive_digest(archive_path) != expected_sha256:
        raise ValueError("retrieved archive SHA-256 mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not 1 < len(members) <= MAX_FILES + 1:
            raise ValueError("invalid archive file count")
        names: set[str] = set()
        total = 0
        for member in members:
            _safe_name(member.filename)
            if (
                member.filename in names
                or member.is_dir()
                or member.flag_bits & 1
                or member.compress_type != zipfile.ZIP_STORED
                or member.compress_size != member.file_size
                or member.file_size > MAX_FILE_BYTES
                or not stat.S_ISREG(member.external_attr >> 16)
            ):
                raise ValueError("archive contains duplicate or unsupported members")
            names.add(member.filename)
            total += member.file_size
        if total > MAX_TOTAL_BYTES + 1024 * 1024 or _MANIFEST not in names:
            raise ValueError("archive exceeds limits or lacks its inventory")
        if archive.getinfo(_MANIFEST).file_size > 1024 * 1024:
            raise ValueError("archive inventory exceeds limit")
        try:
            manifest = _read_json(archive.read(_MANIFEST))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ValueError("invalid or ambiguous export inventory") from error
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "scope", "files"}
            or manifest["schema"] != SCHEMA
            or manifest["scope"] != "PRIVATE_BYTE_INTEGRITY_ONLY"
            or not isinstance(manifest["files"], list)
        ):
            raise ValueError("invalid export inventory")
        expected: set[str] = set()
        for item in manifest["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                raise ValueError("invalid export inventory entry")
            name = item["path"]
            if not isinstance(name, str):
                raise ValueError("invalid export inventory path")
            _safe_name(name)
            if (
                name in expected
                or name not in names
                or not name.startswith(("prepared/", "collection/"))
                or type(item["bytes"]) is not int
                or item["bytes"] != archive.getinfo(name).file_size
            ):
                raise ValueError("export inventory membership/size mismatch")
            if _digest(archive.read(name)) != item["sha256"]:
                raise ValueError("export inventory payload hash mismatch")
            expected.add(name)
        if expected != names - {_MANIFEST} or not {
            "prepared/inventory.json",
            "collection/session-status.json",
        }.issubset(expected):
            raise ValueError("export inventory has missing or additional files")
        if archive.getinfo("collection/session-status.json").file_size > 1024 * 1024:
            raise ValueError("terminal session status exceeds limit")
        _terminal_status(archive.read("collection/session-status.json"))
    if archive_digest(archive_path) != expected_sha256:
        raise ValueError("archive changed during verification")
    return {
        "schema": SCHEMA,
        "status": "VERIFIED_PRIVATE_BYTES",
        "archive_sha256": expected_sha256,
        "payload_files": len(expected),
        "evidence_eligible": False,
        "limitation": "Byte integrity only; replay experiment artifacts separately.",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="archive private terminal run bytes")
    export.add_argument("--prepared-dir", type=Path, required=True)
    export.add_argument("--collection-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-export", help="verify a retrieved archive")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        result = pack(args.prepared_dir, args.collection_dir, args.output)
    else:
        result = verify_archive(args.archive, args.sha256)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
