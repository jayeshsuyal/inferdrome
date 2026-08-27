#!/usr/bin/env python3
"""Create and independently verify a retrieved real-GPU capture pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

_CAPTURE_SCHEMA = "inferdrome.real-gpu-capture.v1"
_FAILURE_SCHEMA = "inferdrome.real-gpu-capture-failure.v1"
_ACCEPTANCE_BOUNDARY = "PENDING_EXTERNAL_EXITSPEC"
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}\Z")
_MAX_JSON_BYTES = 8_388_608
_MAX_CAPTURE_FILES = 20_000
_MAX_CAPTURE_MEMBERS = 40_000
_MAX_CAPTURE_DIRECTORIES = 20_000
_MAX_CAPTURE_BYTES = 2_147_483_648
_MAX_ARCHIVE_BYTES = 2_147_483_648


class CaptureError(RuntimeError):
    """Expected, user-facing capture creation or verification failure."""


def _strict_json_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    if not content or len(content) > _MAX_JSON_BYTES:
        raise CaptureError(f"{label} is empty or exceeds its size limit")
    try:
        text = content.decode("utf-8")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CaptureError(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise CaptureError(f"{label} must be a JSON object")
    return value


def _read_regular(path: Path, *, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise CaptureError(f"{label} is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise CaptureError(f"{label} must be a regular file")
    if metadata.st_size > _MAX_JSON_BYTES:
        raise CaptureError(f"{label} exceeds its size limit")
    try:
        return path.read_bytes()
    except OSError:
        raise CaptureError(f"{label} cannot be read") from None


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    return _strict_json_bytes(_read_regular(path, label=label), label=label)


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path, *, label: str) -> str:
    return _sha256_bytes(_read_regular(path, label=label))


def _version_file(path: Path, *, label: str) -> str:
    content = _read_regular(path, label=label)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise CaptureError(f"{label} is not UTF-8") from None
    lines = text.splitlines()
    if (
        len(lines) != 1
        or not lines[0]
        or text != lines[0] + "\n"
        or len(lines[0]) > 128
        or any(ord(character) < 32 for character in lines[0])
    ):
        raise CaptureError(f"{label} has an invalid shape")
    return lines[0]


def _required_text(value: dict[str, Any], key: str, *, label: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise CaptureError(f"{label} omits {key}")
    return selected


def _required_digest(value: dict[str, Any], key: str, *, label: str) -> str:
    selected = _required_text(value, key, label=label)
    if _SHA256_PATTERN.fullmatch(selected) is None:
        raise CaptureError(f"{label} has an invalid {key}")
    return selected


def _require_exact_fields(
    value: dict[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise CaptureError(f"{label} has an unexpected shape")


def _require_commit(value: str, *, label: str) -> str:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise CaptureError(f"{label} is not a full lowercase Git commit")
    return value


def _require_timestamp(value: str, *, label: str) -> None:
    if not value.endswith("Z"):
        raise CaptureError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CaptureError(f"{label} is not an ISO 8601 timestamp") from None
    if parsed.tzinfo != UTC:
        raise CaptureError(f"{label} is not a UTC timestamp")


def _relative_file(root: Path, value: str, *, label: str) -> Path:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in value
    ):
        raise CaptureError(f"{label} is not a safe relative path")
    path = root.joinpath(*candidate.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        raise CaptureError(f"{label} escapes the capture root") from None
    _read_regular(path, label=label)
    return path


def _validate_capture_tree(root: Path) -> None:
    try:
        root_metadata = os.lstat(root)
    except OSError:
        raise CaptureError("capture root is unavailable") from None
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise CaptureError("capture root must be a real directory")
    member_count = 1
    directory_count = 1
    file_count = 0
    total_bytes = 0
    if (
        member_count > _MAX_CAPTURE_MEMBERS
        or directory_count > _MAX_CAPTURE_DIRECTORIES
    ):
        raise CaptureError("capture tree exceeds its safety limits")
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in directory_names:
            path = current / name
            try:
                metadata = os.lstat(path)
            except OSError:
                raise CaptureError("capture tree changed during inspection") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise CaptureError("capture tree contains a symbolic link")
            if not stat.S_ISDIR(metadata.st_mode):
                raise CaptureError("capture tree contains a special directory entry")
            member_count += 1
            directory_count += 1
            if (
                member_count > _MAX_CAPTURE_MEMBERS
                or directory_count > _MAX_CAPTURE_DIRECTORIES
            ):
                raise CaptureError("capture tree exceeds its safety limits")
        for name in filenames:
            path = current / name
            try:
                metadata = os.lstat(path)
            except OSError:
                raise CaptureError("capture tree changed during inspection") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise CaptureError("capture tree contains a symbolic link")
            if not stat.S_ISREG(metadata.st_mode):
                raise CaptureError("capture tree contains a special file")
            member_count += 1
            file_count += 1
            total_bytes += metadata.st_size
            if (
                member_count > _MAX_CAPTURE_MEMBERS
                or file_count > _MAX_CAPTURE_FILES
                or total_bytes > _MAX_CAPTURE_BYTES
            ):
                raise CaptureError("capture tree exceeds its safety limits")


def _one_receipt(root: Path, pattern: str, *, label: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise CaptureError(f"{label} must contain exactly one receipt")
    _read_regular(matches[0], label=label)
    return matches[0]


def _support_entry(capture_root: Path, relative: str) -> dict[str, str]:
    path = _relative_file(capture_root, relative, label=f"support file {relative}")
    return {
        "path": relative,
        "sha256": _sha256_file(path, label=f"support file {relative}"),
    }


def write_capture_manifest(capture_root: Path, repository_commit: str) -> Path:
    """Write the immutable top-level identity for one completed host capture."""

    repository_commit = _require_commit(
        repository_commit,
        label="repository commit",
    )
    capture_root = capture_root.absolute()
    _validate_capture_tree(capture_root)
    manifest_path = capture_root / "capture-manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise CaptureError("capture manifest destination already exists")
    single_path = _one_receipt(
        capture_root,
        "single/real-gpu-*/demo-receipt.json",
        label="single proof",
    )
    comparison_path = _one_receipt(
        capture_root,
        "comparison/real-gpu-comparison-*/comparison-demo-receipt.json",
        label="comparison proof",
    )
    single = _read_json(single_path, label="single proof receipt")
    comparison = _read_json(comparison_path, label="comparison proof receipt")
    if (
        single.get("schema_version") != "inferdrome.real-gpu-demo-receipt.v1"
        or comparison.get("schema_version")
        != "inferdrome.real-gpu-comparison-demo-receipt.v1"
        or single.get("repository_commit") != repository_commit
        or comparison.get("repository_commit") != repository_commit
    ):
        raise CaptureError("proof receipts disagree with the capture commit")
    host_preparation = _support_entry(
        capture_root,
        "support/host-preparation.json",
    )
    if (
        single.get("host_preparation_sha256") != host_preparation["sha256"]
        or comparison.get("host_preparation_sha256")
        != host_preparation["sha256"]
    ):
        raise CaptureError("proof receipts disagree with host preparation")
    manifest = {
        "acceptance_boundary": _ACCEPTANCE_BOUNDARY,
        "comparison": {
            "receipt_path": comparison_path.relative_to(capture_root).as_posix(),
            "receipt_sha256": _sha256_file(
                comparison_path,
                label="comparison proof receipt",
            ),
        },
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "repository_commit": repository_commit,
        "schema_version": _CAPTURE_SCHEMA,
        "single": {
            "receipt_path": single_path.relative_to(capture_root).as_posix(),
            "receipt_sha256": _sha256_file(
                single_path,
                label="single proof receipt",
            ),
        },
        "support": {
            "host_preparation": host_preparation,
            "inferdrome_version": _support_entry(
                capture_root,
                "support/inferdrome-version.txt",
            ),
            "python_packages": _support_entry(
                capture_root,
                "support/python-packages.txt",
            ),
            "vllm_version": _support_entry(
                capture_root,
                "support/vllm-version.txt",
            ),
        },
    }
    content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_path = capture_root / ".capture-manifest.json.tmp"
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, manifest_path)
        manifest_path.chmod(0o444)
    except OSError:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise CaptureError("capture manifest could not be published") from None
    return manifest_path


def write_failure_receipt(
    capture_root: Path,
    repository_commit: str,
    *,
    failed_step: str,
    exit_code: int,
) -> Path:
    """Retain a bounded non-proof diagnostic when host capture fails."""

    repository_commit = _require_commit(
        repository_commit,
        label="repository commit",
    )
    if (
        not failed_step
        or len(failed_step) > 128
        or any(ord(character) < 32 for character in failed_step)
    ):
        raise CaptureError("failed step is invalid")
    if not 1 <= exit_code <= 255:
        raise CaptureError("failure exit code must be between 1 and 255")
    capture_root = capture_root.absolute()
    try:
        capture_root.mkdir(parents=True, exist_ok=True)
        metadata = os.lstat(capture_root)
    except OSError:
        raise CaptureError("capture root cannot be prepared") from None
    if not stat.S_ISDIR(metadata.st_mode) or capture_root.is_symlink():
        raise CaptureError("capture root must be a real directory")
    destination = capture_root / "capture-failure.json"
    if destination.exists() or destination.is_symlink():
        raise CaptureError("failure receipt destination already exists")
    receipt = {
        "failed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "failed_step": failed_step,
        "process_exit_code": exit_code,
        "proof_status": "INCOMPLETE_NOT_EVIDENCE",
        "repository_commit": repository_commit,
        "schema_version": _FAILURE_SCHEMA,
    }
    destination.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o444)
    return destination


def verify_failure_capture(
    capture_root: Path,
    *,
    expected_repository_commit: str | None = None,
) -> dict[str, Any]:
    """Verify the bounded status receipt for an incomplete host capture."""

    capture_root = capture_root.absolute()
    _validate_capture_tree(capture_root)
    receipt = _read_json(
        capture_root / "capture-failure.json",
        label="capture failure receipt",
    )
    _require_exact_fields(
        receipt,
        {
            "failed_at",
            "failed_step",
            "process_exit_code",
            "proof_status",
            "repository_commit",
            "schema_version",
        },
        label="capture failure receipt",
    )
    repository_commit = _require_commit(
        _required_text(
            receipt,
            "repository_commit",
            label="capture failure receipt",
        ),
        label="capture failure repository commit",
    )
    if (
        expected_repository_commit is not None
        and repository_commit
        != _require_commit(
            expected_repository_commit,
            label="expected repository commit",
        )
    ):
        raise CaptureError(
            "capture failure repository commit is not the expected commit"
        )
    failed_step = _required_text(
        receipt,
        "failed_step",
        label="capture failure receipt",
    )
    exit_code = receipt.get("process_exit_code")
    if (
        receipt.get("schema_version") != _FAILURE_SCHEMA
        or receipt.get("proof_status") != "INCOMPLETE_NOT_EVIDENCE"
        or len(failed_step) > 128
        or any(ord(character) < 32 for character in failed_step)
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not 1 <= exit_code <= 255
    ):
        raise CaptureError("capture failure receipt is invalid")
    _require_timestamp(
        _required_text(
            receipt,
            "failed_at",
            label="capture failure receipt",
        ),
        label="capture failure time",
    )
    return receipt


def _manifest_entry(
    root: Path,
    value: Any,
    *,
    label: str,
    path_key: str,
    digest_key: str,
) -> Path:
    if not isinstance(value, dict):
        raise CaptureError(f"{label} is not an object")
    _require_exact_fields(value, {path_key, digest_key}, label=label)
    path = _relative_file(
        root,
        _required_text(value, path_key, label=label),
        label=f"{label} path",
    )
    expected_digest = _required_digest(value, digest_key, label=label)
    if _sha256_file(path, label=label) != expected_digest:
        raise CaptureError(f"{label} failed SHA-256 verification")
    return path


def _require_remote_path_suffix(
    value: str,
    suffix: PurePosixPath,
    *,
    label: str,
) -> None:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[-len(suffix.parts) :] != suffix.parts
    ):
        raise CaptureError(f"{label} disagrees with its artifact identity")


def _verify_single_receipt(
    capture_root: Path,
    receipt_path: Path,
    *,
    repository_commit: str,
    host_preparation_sha256: str,
) -> dict[str, str]:
    receipt = _read_json(receipt_path, label="single proof receipt")
    _require_exact_fields(
        receipt,
        {
            "acceptance_boundary",
            "bundle_digest",
            "bundle_path",
            "corrupted_artifact_rejected",
            "evidence_eligibility",
            "generated_at",
            "host_preparation_sha256",
            "integrity_status",
            "repository_commit",
            "run_id",
            "schema_version",
            "synthetic_fixture_rejected",
        },
        label="single proof receipt",
    )
    run_id = _required_text(receipt, "run_id", label="single proof receipt")
    bundle_digest = _required_digest(
        receipt,
        "bundle_digest",
        label="single proof receipt",
    )
    if (
        receipt.get("schema_version") != "inferdrome.real-gpu-demo-receipt.v1"
        or receipt.get("acceptance_boundary") != _ACCEPTANCE_BOUNDARY
        or receipt.get("repository_commit") != repository_commit
        or receipt.get("host_preparation_sha256") != host_preparation_sha256
        or receipt.get("corrupted_artifact_rejected") is not True
        or receipt.get("synthetic_fixture_rejected") is not True
        or receipt.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or receipt.get("integrity_status") != "VALID"
        or _RUN_ID_PATTERN.fullmatch(run_id) is None
    ):
        raise CaptureError("single proof receipt is not a successful GPU proof")
    _require_timestamp(
        _required_text(receipt, "generated_at", label="single proof receipt"),
        label="single proof generation time",
    )
    proof_root = receipt_path.parent
    bundle = proof_root / "runs" / run_id / "bundle"
    expected_suffix = PurePosixPath("runs") / run_id / "bundle"
    _require_remote_path_suffix(
        _required_text(receipt, "bundle_path", label="single proof receipt"),
        expected_suffix,
        label="single receipt bundle path",
    )
    _verify_customer_bundle(bundle, bundle_digest, run_id=run_id)
    try:
        proof_root.resolve(strict=True).relative_to(capture_root.resolve(strict=True))
    except (OSError, ValueError):
        raise CaptureError("single proof escapes the capture root") from None
    return {"bundle_digest": bundle_digest, "run_id": run_id}


def _verify_customer_bundle(
    bundle: Path,
    bundle_digest: str,
    *,
    run_id: str,
) -> None:
    try:
        from inferdrome.bundle import verify_bundle
        from inferdrome.domain import EvidenceEligibility
        from inferdrome.errors import InferdromeError
    except ImportError:
        raise CaptureError("Inferdrome is unavailable for local verification") from None
    try:
        report = verify_bundle(
            bundle,
            expected_bundle_digest=bundle_digest,
        )
    except InferdromeError:
        raise CaptureError(f"bundle verification failed for {run_id}") from None
    if (
        report.run_id != run_id
        or report.bundle_digest != bundle_digest
        or report.descriptor.evidence_eligibility
        is not EvidenceEligibility.CUSTOMER_ELIGIBLE
        or report.descriptor.integrity_status != "VALID"
    ):
        raise CaptureError(f"bundle identity disagrees for {run_id}")


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaptureError(f"{label} is not an object")
    return value


def _verify_comparison_receipt(
    capture_root: Path,
    receipt_path: Path,
    *,
    repository_commit: str,
    host_preparation_sha256: str,
) -> dict[str, Any]:
    receipt = _read_json(receipt_path, label="comparison proof receipt")
    _require_exact_fields(
        receipt,
        {
            "acceptance_boundary",
            "all_bundles_customer_eligible",
            "comparison_plan",
            "comparison_result",
            "generated_at",
            "host_preparation_sha256",
            "repository_commit",
            "resume_reverification_succeeded",
            "runs",
            "schema_version",
            "trial_sets",
        },
        label="comparison proof receipt",
    )
    if (
        receipt.get("schema_version")
        != "inferdrome.real-gpu-comparison-demo-receipt.v1"
        or receipt.get("acceptance_boundary") != _ACCEPTANCE_BOUNDARY
        or receipt.get("repository_commit") != repository_commit
        or receipt.get("host_preparation_sha256") != host_preparation_sha256
        or receipt.get("all_bundles_customer_eligible") is not True
        or receipt.get("resume_reverification_succeeded") is not True
    ):
        raise CaptureError("comparison receipt is not a successful GPU proof")
    _require_timestamp(
        _required_text(receipt, "generated_at", label="comparison proof receipt"),
        label="comparison proof generation time",
    )
    proof_root = receipt_path.parent
    try:
        proof_root.resolve(strict=True).relative_to(capture_root.resolve(strict=True))
    except (OSError, ValueError):
        raise CaptureError("comparison proof escapes the capture root") from None
    runs = receipt.get("runs")
    if not isinstance(runs, list) or len(runs) != 4:
        raise CaptureError("comparison receipt must contain four runs")
    run_ids: list[str] = []
    for index, item in enumerate(runs):
        run = _require_mapping(item, label=f"comparison run {index + 1}")
        _require_exact_fields(
            run,
            {
                "arm",
                "block_index",
                "bundle_digest",
                "bundle_path",
                "environment_completeness",
                "evidence_eligibility",
                "run_id",
                "sequence_index",
            },
            label=f"comparison run {index + 1}",
        )
        run_id = _required_text(run, "run_id", label="comparison run")
        digest = _required_digest(run, "bundle_digest", label="comparison run")
        if (
            _RUN_ID_PATTERN.fullmatch(run_id) is None
            or run_id in run_ids
            or run.get("sequence_index") != index
            or run.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
            or run.get("environment_completeness") != "COMPLETE"
        ):
            raise CaptureError("comparison run identity is invalid")
        expected_suffix = PurePosixPath("runs") / run_id / "bundle"
        _require_remote_path_suffix(
            _required_text(run, "bundle_path", label="comparison run"),
            expected_suffix,
            label="comparison bundle path",
        )
        _verify_customer_bundle(
            proof_root / "runs" / run_id / "bundle",
            digest,
            run_id=run_id,
        )
        run_ids.append(run_id)

    plan = _require_mapping(receipt.get("comparison_plan"), label="comparison plan")
    result = _require_mapping(
        receipt.get("comparison_result"),
        label="comparison result",
    )
    trial_sets = _require_mapping(receipt.get("trial_sets"), label="Trial Sets")
    _require_exact_fields(
        plan,
        {
            "comparison_plan_digest",
            "comparison_plan_id",
            "path",
            "predeclaration_assurance",
            "schedule",
        },
        label="comparison plan",
    )
    _require_exact_fields(
        result,
        {
            "comparison_result_digest",
            "comparison_result_id",
            "inference_scope",
            "outcomes",
            "path",
            "status",
            "unsatisfied_controls",
        },
        label="comparison result",
    )
    _require_exact_fields(
        trial_sets,
        {"baseline", "candidate"},
        label="Trial Sets",
    )
    plan_id = _required_text(plan, "comparison_plan_id", label="comparison plan")
    plan_digest = _required_digest(
        plan,
        "comparison_plan_digest",
        label="comparison plan",
    )
    result_id = _required_text(
        result,
        "comparison_result_id",
        label="comparison result",
    )
    result_digest = _required_digest(
        result,
        "comparison_result_digest",
        label="comparison result",
    )
    _require_remote_path_suffix(
        _required_text(plan, "path", label="comparison plan"),
        PurePosixPath("comparison-plans") / plan_id,
        label="comparison plan path",
    )
    _require_remote_path_suffix(
        _required_text(result, "path", label="comparison result"),
        PurePosixPath("comparison-results") / result_id,
        label="comparison result path",
    )
    try:
        from inferdrome.comparisons import (
            verify_comparison_plan,
            verify_comparison_result,
        )
        from inferdrome.errors import InferdromeError
        from inferdrome.trials import verify_trial_set
    except ImportError:
        raise CaptureError("Inferdrome is unavailable for local verification") from None
    plans_root = proof_root / "comparison-plans"
    results_root = proof_root / "comparison-results"
    runs_root = proof_root / "runs"
    trial_sets_root = proof_root / "trial-sets"
    try:
        verified_plan = verify_comparison_plan(
            plans_root / plan_id,
            expected_comparison_plan_digest=plan_digest,
        )
        verified_trials = {}
        trial_entries: dict[str, dict[str, Any]] = {}
        for arm in ("baseline", "candidate"):
            entry = _require_mapping(trial_sets.get(arm), label=f"{arm} Trial Set")
            _require_exact_fields(
                entry,
                {"path", "trial_set_digest", "trial_set_id"},
                label=f"{arm} Trial Set",
            )
            trial_id = _required_text(entry, "trial_set_id", label=f"{arm} Trial Set")
            trial_digest = _required_digest(
                entry,
                "trial_set_digest",
                label=f"{arm} Trial Set",
            )
            _require_remote_path_suffix(
                _required_text(entry, "path", label=f"{arm} Trial Set"),
                PurePosixPath("trial-sets") / trial_id,
                label=f"{arm} Trial Set path",
            )
            trial_entries[arm] = entry
            verified_trials[arm] = verify_trial_set(
                trial_sets_root / trial_id,
                runs_root=runs_root,
                expected_trial_set_digest=trial_digest,
            )
        verified_result = verify_comparison_result(
            results_root / result_id,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=plans_root,
            expected_comparison_result_digest=result_digest,
        )
    except InferdromeError:
        raise CaptureError("controlled-comparison verification failed") from None
    expected_schedule = [
        {
            "arm": slot.arm.value,
            "block_index": slot.block_index,
            "run_id": slot.run_id,
            "sequence_index": slot.sequence_index,
        }
        for slot in verified_plan.descriptor.ordered_schedule
    ]
    observed_schedule = [
        {
            "arm": item["arm"],
            "block_index": item["block_index"],
            "run_id": item["run_id"],
            "sequence_index": item["sequence_index"],
        }
        for item in runs
        if isinstance(item, dict)
    ]
    result_descriptor = verified_result.descriptor
    expected_outcomes = [
        outcome.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        for outcome in result_descriptor.outcomes
    ]
    expected_unsatisfied = [
        control.value for control in result_descriptor.unsatisfied_controls
    ]
    receipt_digests = {
        str(item["run_id"]): str(item["bundle_digest"])
        for item in runs
        if isinstance(item, dict)
    }
    recalculated_digests = {
        member.verification.run_id: member.verification.bundle_digest
        for member in (
            *verified_result.baseline.members,
            *verified_result.candidate.members,
        )
    }
    if (
        expected_schedule != plan.get("schedule")
        or expected_schedule != observed_schedule
        or [slot["run_id"] for slot in expected_schedule] != run_ids
        or plan.get("predeclaration_assurance") != "OPERATOR_ATTESTED"
        or verified_result.plan.comparison_plan_digest != plan_digest
        or result_descriptor.comparison_plan_id != plan_id
        or result_descriptor.comparison_result_id != result_id
        or result_descriptor.status.value != result.get("status")
        or result_descriptor.inference_scope != result.get("inference_scope")
        or expected_outcomes != result.get("outcomes")
        or expected_unsatisfied != result.get("unsatisfied_controls")
        or receipt_digests != recalculated_digests
        or verified_result.baseline.trial_set_digest
        != verified_trials["baseline"].trial_set_digest
        or verified_result.candidate.trial_set_digest
        != verified_trials["candidate"].trial_set_digest
        or trial_entries["baseline"].get("trial_set_id")
        != verified_result.baseline.descriptor.trial_set_id
        or trial_entries["candidate"].get("trial_set_id")
        != verified_result.candidate.descriptor.trial_set_id
    ):
        raise CaptureError("comparison receipt disagrees with recalculated artifacts")
    return {
        "comparison_plan_digest": plan_digest,
        "comparison_plan_id": plan_id,
        "comparison_result_digest": result_digest,
        "comparison_result_id": result_id,
        "run_ids": run_ids,
        "status": verified_result.descriptor.status.value,
    }


def verify_capture(
    capture_root: Path,
    *,
    expected_repository_commit: str | None = None,
) -> dict[str, Any]:
    """Independently verify a complete capture after transport."""

    capture_root = capture_root.absolute()
    _validate_capture_tree(capture_root)
    manifest = _read_json(
        capture_root / "capture-manifest.json",
        label="capture manifest",
    )
    _require_exact_fields(
        manifest,
        {
            "acceptance_boundary",
            "comparison",
            "generated_at",
            "repository_commit",
            "schema_version",
            "single",
            "support",
        },
        label="capture manifest",
    )
    repository_commit = _require_commit(
        _required_text(manifest, "repository_commit", label="capture manifest"),
        label="capture repository commit",
    )
    if (
        manifest.get("schema_version") != _CAPTURE_SCHEMA
        or manifest.get("acceptance_boundary") != _ACCEPTANCE_BOUNDARY
    ):
        raise CaptureError("capture manifest has an unsupported boundary")
    if (
        expected_repository_commit is not None
        and repository_commit
        != _require_commit(
            expected_repository_commit,
            label="expected repository commit",
        )
    ):
        raise CaptureError("capture repository commit is not the expected commit")
    _require_timestamp(
        _required_text(manifest, "generated_at", label="capture manifest"),
        label="capture generation time",
    )
    support = _require_mapping(manifest.get("support"), label="capture support")
    _require_exact_fields(
        support,
        {
            "host_preparation",
            "inferdrome_version",
            "python_packages",
            "vllm_version",
        },
        label="capture support",
    )
    support_paths = {
        name: _manifest_entry(
            capture_root,
            support[name],
            label=f"capture support {name}",
            path_key="path",
            digest_key="sha256",
        )
        for name in sorted(support)
    }
    host_preparation_sha256 = _sha256_file(
        support_paths["host_preparation"],
        label="host preparation receipt",
    )
    python_packages_sha256 = _sha256_file(
        support_paths["python_packages"],
        label="Python package inventory",
    )
    preparation = _read_json(
        support_paths["host_preparation"],
        label="host preparation receipt",
    )
    _require_exact_fields(
        preparation,
        {
            "architecture",
            "model_directory",
            "model_id",
            "model_revision",
            "prepared_at",
            "python_packages_sha256",
            "repository_commit",
            "schema_version",
            "vllm_wheel_filename",
            "vllm_wheel_sha256",
        },
        label="host preparation receipt",
    )
    inferdrome_version = _version_file(
        support_paths["inferdrome_version"],
        label="Inferdrome version record",
    )
    vllm_version = _version_file(
        support_paths["vllm_version"],
        label="vLLM version record",
    )
    if (
        preparation.get("schema_version")
        != "inferdrome.real-gpu-host-preparation.v1"
        or preparation.get("repository_commit") != repository_commit
        or preparation.get("python_packages_sha256")
        != python_packages_sha256
        or inferdrome_version != "0.1.0.dev0"
        or vllm_version != "0.26.0"
    ):
        raise CaptureError("host preparation disagrees with the capture pins")
    _require_timestamp(
        _required_text(
            preparation,
            "prepared_at",
            label="host preparation receipt",
        ),
        label="host preparation time",
    )
    _required_digest(
        preparation,
        "vllm_wheel_sha256",
        label="host preparation receipt",
    )
    single_path = _manifest_entry(
        capture_root,
        manifest.get("single"),
        label="single proof receipt",
        path_key="receipt_path",
        digest_key="receipt_sha256",
    )
    comparison_path = _manifest_entry(
        capture_root,
        manifest.get("comparison"),
        label="comparison proof receipt",
        path_key="receipt_path",
        digest_key="receipt_sha256",
    )
    single = _verify_single_receipt(
        capture_root,
        single_path,
        repository_commit=repository_commit,
        host_preparation_sha256=host_preparation_sha256,
    )
    comparison = _verify_comparison_receipt(
        capture_root,
        comparison_path,
        repository_commit=repository_commit,
        host_preparation_sha256=host_preparation_sha256,
    )
    return {
        "acceptance_boundary": _ACCEPTANCE_BOUNDARY,
        "comparison": comparison,
        "repository_commit": repository_commit,
        "schema_version": "inferdrome.real-gpu-capture-verification.v1",
        "single": single,
        "valid": True,
    }


@contextmanager
def _open_archive(path: Path) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Open one archive inode without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_ARCHIVE_BYTES
        ):
            raise OSError
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            yield stream, metadata
    except OSError:
        raise CaptureError("capture archive is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _archive_digest(stream: BinaryIO, metadata: os.stat_result) -> str:
    digest = hashlib.sha256()
    try:
        stream.seek(0)
        remaining = metadata.st_size
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError
            digest.update(block)
            remaining -= len(block)
        if stream.read(1):
            raise OSError
        current = os.fstat(stream.fileno())
    except (OSError, ValueError):
        raise CaptureError("capture archive is unavailable or unsafe") from None
    if (
        current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
        or current.st_size != metadata.st_size
        or current.st_mtime_ns != metadata.st_mtime_ns
        or current.st_ctime_ns != metadata.st_ctime_ns
    ):
        raise CaptureError("capture archive changed during verification")
    stream.seek(0)
    return "sha256:" + digest.hexdigest()


def _snapshot_archive(
    stream: BinaryIO,
    metadata: os.stat_result,
) -> tuple[BinaryIO, str]:
    """Copy a bounded archive before parsing so later source writes cannot race it."""

    snapshot: BinaryIO | None = None
    try:
        # Ownership transfers to the caller, which closes the unlinked snapshot.
        snapshot = tempfile.TemporaryFile(  # noqa: SIM115
            prefix="inferdrome-capture-archive-"
        )
        digest = hashlib.sha256()
        stream.seek(0)
        remaining = metadata.st_size
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError
            if snapshot.write(block) != len(block):
                raise OSError
            digest.update(block)
            remaining -= len(block)
        if stream.read(1):
            raise OSError
        current = os.fstat(stream.fileno())
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or current.st_size != metadata.st_size
            or current.st_mtime_ns != metadata.st_mtime_ns
            or current.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise CaptureError("capture archive changed during verification")
        snapshot.flush()
        snapshot.seek(0)
        return snapshot, "sha256:" + digest.hexdigest()
    except CaptureError:
        if snapshot is not None:
            snapshot.close()
        raise
    except (OSError, ValueError):
        if snapshot is not None:
            snapshot.close()
        raise CaptureError("capture archive is unavailable or unsafe") from None


def _extract_capture_archive_stream(
    stream: BinaryIO,
    destination_parent: Path,
    *,
    expected_archive_sha256: str | None = None,
) -> Path:
    if expected_archive_sha256 is not None:
        if _SHA256_PATTERN.fullmatch(expected_archive_sha256) is None:
            raise CaptureError("expected archive SHA-256 has an invalid shape")
        metadata = os.fstat(stream.fileno())
        snapshot, actual_archive_sha256 = _snapshot_archive(stream, metadata)
        if actual_archive_sha256 != expected_archive_sha256:
            snapshot.close()
            raise CaptureError("retrieved archive failed SHA-256 verification")
        try:
            return _extract_capture_archive_stream(snapshot, destination_parent)
        finally:
            snapshot.close()

    destination_parent = destination_parent.absolute()
    capture_destination = destination_parent / "capture"
    if capture_destination.exists() or capture_destination.is_symlink():
        raise CaptureError("capture extraction destination already exists")
    try:
        destination_parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = os.lstat(destination_parent)
    except OSError:
        raise CaptureError("capture extraction destination is unavailable") from None
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or destination_parent.is_symlink()
    ):
        raise CaptureError("capture extraction destination must be a real directory")
    seen_paths: dict[str, bool] = {}
    member_count = 0
    directory_count = 0
    total_bytes = 0
    file_count = 0
    members: list[tarfile.TarInfo] = []
    retained_modes: list[tuple[PurePosixPath, int, bool]] = []
    try:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:gz") as retained:
            has_capture_root = False
            for member in retained:
                path = PurePosixPath(member.name)
                canonical_name = path.as_posix()
                identity = unicodedata.normalize("NFKC", canonical_name).casefold()
                mode = stat.S_IMODE(member.mode)
                if (
                    path.is_absolute()
                    or not path.parts
                    or path.parts[0] != "capture"
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                    or member.size < 0
                    or member.mode & ~0o777
                    or (member.isdir() and mode & 0o500 != 0o500)
                    or (member.isfile() and mode & 0o400 != 0o400)
                ):
                    raise CaptureError("capture archive contains an unsafe member")

                for depth in range(1, len(path.parts)):
                    directory = PurePosixPath(*path.parts[:depth]).as_posix()
                    directory_identity = unicodedata.normalize(
                        "NFKC", directory
                    ).casefold()
                    if seen_paths.get(directory_identity) is False:
                        raise CaptureError(
                            "capture archive contains an unsafe member"
                        )
                    if directory_identity not in seen_paths:
                        seen_paths[directory_identity] = True
                        directory_count += 1
                if identity in seen_paths:
                    raise CaptureError("capture archive contains an unsafe member")
                seen_paths[identity] = member.isdir()
                if path.parts == ("capture",):
                    has_capture_root = True
                members.append(member)
                member_count += 1
                if member.isdir():
                    directory_count += 1
                retained_modes.append((path, mode, member.isdir()))
                if member.isfile():
                    file_count += 1
                    total_bytes += member.size
                if (
                    member_count > _MAX_CAPTURE_MEMBERS
                    or directory_count > _MAX_CAPTURE_DIRECTORIES
                    or file_count > _MAX_CAPTURE_FILES
                    or total_bytes > _MAX_CAPTURE_BYTES
                ):
                    raise CaptureError("capture archive exceeds its safety limits")
            if not has_capture_root:
                raise CaptureError("capture archive omits its top-level directory")
            retained.extractall(destination_parent, members=members, filter="data")
        ordered_modes = sorted(
            retained_modes,
            key=lambda item: (item[2], -len(item[0].parts)),
        )
        for path, mode, is_directory in ordered_modes:
            destination = destination_parent.joinpath(*path.parts)
            metadata = os.lstat(destination)
            if is_directory != stat.S_ISDIR(metadata.st_mode) or (
                not is_directory and not stat.S_ISREG(metadata.st_mode)
            ):
                raise CaptureError("capture archive member changed during extraction")
            os.chmod(destination, mode, follow_symlinks=False)
    except (OSError, tarfile.TarError):
        raise CaptureError("capture archive cannot be extracted") from None
    _validate_capture_tree(capture_destination)
    return capture_destination


def extract_capture_archive(
    archive: Path,
    destination_parent: Path,
    *,
    expected_archive_sha256: str | None = None,
) -> Path:
    """Safely extract one bounded archive without trusting paths or links.

    An expected digest selects a private snapshot for integrity-bound parsing;
    without one, the direct extraction remains bounded but is not digest-bound.
    """

    archive = archive.absolute()
    with _open_archive(archive) as (stream, _metadata):
        return _extract_capture_archive_stream(
            stream,
            destination_parent,
            expected_archive_sha256=expected_archive_sha256,
        )


def archive_sha256(path: Path) -> str:
    """Hash a potentially large archive without applying the JSON size limit."""

    with _open_archive(path) as (stream, metadata):
        return _archive_digest(stream, metadata)


def _make_directories_writable_for_cleanup(root: Path) -> None:
    """Restore private directory write bits after isolated verification."""

    if not root.exists() or root.is_symlink():
        return
    for directory, directory_names, _filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        current.chmod(0o700, follow_symlinks=False)
        for name in directory_names:
            child = current / name
            if child.is_symlink():
                raise OSError("temporary capture tree contains a symbolic link")


def verify_capture_archive(
    archive: Path,
    *,
    expected_archive_sha256: str,
    expected_repository_commit: str | None = None,
) -> dict[str, Any]:
    """Verify one archive in an isolated extraction directory."""

    if _SHA256_PATTERN.fullmatch(expected_archive_sha256) is None:
        raise CaptureError("expected archive SHA-256 has an invalid shape")
    actual_archive_sha256 = archive_sha256(archive)
    if actual_archive_sha256 != expected_archive_sha256:
        raise CaptureError("retrieved archive failed SHA-256 verification")
    try:
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-capture-verification-"
        ) as temporary:
            temporary_root = Path(temporary)
            capture_root: Path | None = None
            try:
                capture_root = extract_capture_archive(
                    archive,
                    temporary_root,
                    expected_archive_sha256=expected_archive_sha256,
                )
                capture_manifest_sha256 = archive_sha256(
                    capture_root / "capture-manifest.json"
                )
                verification = verify_capture(
                    capture_root,
                    expected_repository_commit=expected_repository_commit,
                )
            finally:
                if capture_root is not None:
                    _make_directories_writable_for_cleanup(capture_root)
    except OSError:
        raise CaptureError(
            "isolated capture verification directory is unavailable"
        ) from None
    return {
        "archive_sha256": actual_archive_sha256,
        "capture_manifest_sha256": capture_manifest_sha256,
        "verification": verification,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or independently verify a real-GPU capture pack"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    write = subparsers.add_parser("write", help="write a completed host manifest")
    write.add_argument("--capture-root", required=True)
    write.add_argument("--repository-commit", required=True)
    failure = subparsers.add_parser(
        "write-failure",
        help="write a bounded incomplete-capture diagnostic",
    )
    failure.add_argument("--capture-root", required=True)
    failure.add_argument("--repository-commit", required=True)
    failure.add_argument("--failed-step", required=True)
    failure.add_argument("--exit-code", required=True, type=int)
    verify = subparsers.add_parser("verify", help="verify one retrieved capture")
    verify.add_argument("capture_root")
    verify.add_argument("--expected-commit")
    verify_archive = subparsers.add_parser(
        "verify-archive",
        help="extract and verify one retrieved capture archive in isolation",
    )
    verify_archive.add_argument("archive")
    verify_archive.add_argument("--expected-sha256", required=True)
    verify_archive.add_argument("--expected-commit")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "write":
            path = write_capture_manifest(
                Path(args.capture_root),
                args.repository_commit,
            )
            print(f"capture_manifest_path={path}")
        elif args.command == "write-failure":
            path = write_failure_receipt(
                Path(args.capture_root),
                args.repository_commit,
                failed_step=args.failed_step,
                exit_code=args.exit_code,
            )
            print(f"capture_failure_path={path}")
        elif args.command == "verify":
            result = verify_capture(
                Path(args.capture_root),
                expected_repository_commit=args.expected_commit,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            result = verify_capture_archive(
                Path(args.archive),
                expected_archive_sha256=args.expected_sha256,
                expected_repository_commit=args.expected_commit,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except CaptureError as error:
        print(f"real-gpu-capture: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
