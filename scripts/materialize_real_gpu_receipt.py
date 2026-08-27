#!/usr/bin/env python3
"""Recover one verified bundle from a retained incomplete GPU capture archive."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inferdrome.bundle import verify_bundle
from inferdrome.domain.states import EvidenceEligibility
from inferdrome.errors import InferdromeError

if __package__:
    from scripts import real_gpu_capture
else:
    import real_gpu_capture

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}\Z")
_RECOVERY_SCHEMA = "inferdrome.recovered-real-gpu-receipt.v1"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004


class MaterializeError(RuntimeError):
    """Expected, user-facing recovery failure."""


def _required_pattern(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise MaterializeError(f"{label} has an invalid shape")
    return value


def _prepare_destination(destination: Path) -> tuple[Path, Path]:
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise MaterializeError("recovery destination already exists")
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        metadata = os.lstat(parent)
    except OSError:
        raise MaterializeError("recovery destination parent is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
        raise MaterializeError("recovery destination parent must be a real directory")
    return destination, parent


def _make_tree_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for directory, directory_names, filenames in os.walk(root):
        current = Path(directory)
        with suppress(OSError):
            current.chmod(0o700)
        for directory_name in directory_names:
            with suppress(OSError):
                (current / directory_name).chmod(0o700)
        for filename in filenames:
            with suppress(OSError):
                (current / filename).chmod(0o600)


def _remove_staging(root: Path) -> None:
    _make_tree_writable(root)
    with suppress(OSError):
        shutil.rmtree(root)


def _raise_rename_error() -> None:
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish one directory without replacing any destination."""

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


def _single_bundle(
    capture_root: Path,
    *,
    expected_run_id: str,
) -> tuple[Path, Path]:
    single_root = capture_root / "single"
    try:
        demo_roots = sorted(single_root.iterdir())
    except OSError:
        raise MaterializeError(
            "capture does not contain a single-run workspace"
        ) from None
    if len(demo_roots) > 32:
        raise MaterializeError("capture contains too many single-run workspaces")
    candidates: list[Path] = []
    for demo_root in demo_roots:
        try:
            run_roots = sorted((demo_root / "runs").iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            raise MaterializeError(
                "capture single-run workspace cannot be inspected"
            ) from None
        candidates.extend(
            run_root / "bundle"
            for run_root in run_roots
            if (run_root / "bundle").is_dir()
        )
    if len(candidates) != 1:
        raise MaterializeError("capture does not contain exactly one run bundle")
    bundle = candidates[0]
    if bundle.parent.name != expected_run_id:
        raise MaterializeError("capture does not contain the expected run bundle")
    runs_root = bundle.parent.parent
    try:
        bundle.resolve(strict=True).relative_to(capture_root.resolve(strict=True))
    except (OSError, ValueError):
        raise MaterializeError("recovered bundle escapes the capture root") from None
    return bundle, runs_root


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
    except OSError:
        raise MaterializeError("recovery receipt could not be published") from None


def materialize(
    archive: Path,
    destination: Path,
    *,
    expected_archive_sha256: str,
    expected_bundle_digest: str,
    expected_repository_commit: str,
    expected_run_id: str,
) -> dict[str, Any]:
    """Extract, verify, and publish one single-run-only dashboard workspace."""

    expected_archive_sha256 = _required_pattern(
        expected_archive_sha256,
        _SHA256_PATTERN,
        label="expected archive SHA-256",
    )
    expected_bundle_digest = _required_pattern(
        expected_bundle_digest,
        _SHA256_PATTERN,
        label="expected bundle digest",
    )
    expected_repository_commit = _required_pattern(
        expected_repository_commit,
        _COMMIT_PATTERN,
        label="expected repository commit",
    )
    expected_run_id = _required_pattern(
        expected_run_id,
        _RUN_ID_PATTERN,
        label="expected run ID",
    )
    destination, destination_parent = _prepare_destination(destination)
    archive = archive.expanduser().absolute()
    try:
        actual_archive_sha256 = real_gpu_capture.archive_sha256(archive)
    except real_gpu_capture.CaptureError as error:
        raise MaterializeError(f"capture archive is invalid: {error}") from None
    if actual_archive_sha256 != expected_archive_sha256:
        raise MaterializeError("capture archive failed SHA-256 verification")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination_parent,
        )
    )
    published = False
    try:
        capture_root = real_gpu_capture.extract_capture_archive(
            archive,
            staging,
            expected_archive_sha256=expected_archive_sha256,
        )
        failure = real_gpu_capture.verify_failure_capture(
            capture_root,
            expected_repository_commit=expected_repository_commit,
        )
        bundle, runs_root = _single_bundle(
            capture_root,
            expected_run_id=expected_run_id,
        )
        report = verify_bundle(
            bundle,
            expected_bundle_digest=expected_bundle_digest,
        )
        if (
            report.run_id != expected_run_id
            or report.bundle_digest != expected_bundle_digest
            or report.descriptor.evidence_eligibility
            is not EvidenceEligibility.CUSTOMER_ELIGIBLE
        ):
            raise MaterializeError(
                "recovered bundle is not the expected customer-eligible run"
            )
        receipt = {
            "artifact_count": report.artifact_count,
            "bundle_digest": report.bundle_digest,
            "bundle_path": bundle.relative_to(staging).as_posix(),
            "capture_failed_step": failure["failed_step"],
            "capture_proof_status": failure["proof_status"],
            "dashboard_route": f"/runs/{report.run_id}",
            "evidence_eligibility": report.descriptor.evidence_eligibility.value,
            "integrity_status": "VALID",
            "materialized_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "recovered_scope": "SINGLE_BUNDLE_ONLY",
            "repository_commit": expected_repository_commit,
            "run_id": report.run_id,
            "runs_root": runs_root.relative_to(staging).as_posix(),
            "schema_version": _RECOVERY_SCHEMA,
            "source_archive_sha256": actual_archive_sha256,
            "total_bytes": report.total_bytes,
        }
        _write_receipt(staging / "recovered-real-gpu-receipt.json", receipt)
        try:
            _rename_no_replace(staging, destination)
        except OSError:
            raise MaterializeError("verified recovery could not be published") from None
        published = True
    except (InferdromeError, real_gpu_capture.CaptureError) as error:
        raise MaterializeError(
            f"recovered bundle failed verification: {error}"
        ) from None
    finally:
        if not published:
            _remove_staging(staging)

    return {
        **receipt,
        "bundle_path": str(destination / receipt["bundle_path"]),
        "destination": str(destination),
        "receipt_path": str(destination / "recovered-real-gpu-receipt.json"),
        "runs_root": str(destination / receipt["runs_root"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize one verified real-GPU bundle for the local dashboard"
    )
    parser.add_argument("archive")
    parser.add_argument("destination")
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-bundle-digest", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-run-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = materialize(
            Path(args.archive),
            Path(args.destination),
            expected_archive_sha256=args.expected_archive_sha256,
            expected_bundle_digest=args.expected_bundle_digest,
            expected_repository_commit=args.expected_commit,
            expected_run_id=args.expected_run_id,
        )
    except MaterializeError as error:
        print(f"real-gpu-receipt: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
