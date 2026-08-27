#!/usr/bin/env python3
"""Run the prospective, externally contract-linked real-GPU capture path.

This wrapper deliberately has no built-in ExitSpec digest or contract bytes.
It accepts three explicit source files and three explicit expected digests only
after an external owner has frozen the corresponding contracts.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import signal
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import TypeAdapter, ValidationError

from inferdrome.bundle import verify_bundle
from inferdrome.domain.evidence import EvidenceEligibility
from inferdrome.domain.ids import RunId, Sha256Digest, sha256_digest
from inferdrome.errors import InferdromeError
from inferdrome.resolution import ResolutionResult, resolve_experiment
from inferdrome.resolution.io import resolve_safe_child

try:
    import scripts.run_real_gpu_demo as demo
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repo root.
    import run_real_gpu_demo as demo  # type: ignore[no-redef]


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "inferdrome.prospective-real-gpu-capture.v1"
HANDOFF_SCHEMA_VERSION = "inferdrome.prospective-real-gpu-handoff.v1"
PUBLICATION_SCHEMA_VERSION = "inferdrome.prospective-real-gpu-publication.v1"
RECEIPT_SCHEMA_VERSION = "inferdrome.prospective-real-gpu-receipt.v1"
CASE_IDS = (
    "native-p95-under-20ms",
    "native-p95-under-10ms",
    "semantic-first-nonempty-under-20ms",
)
_CASE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_MAX_METADATA_BYTES = 8_388_608
_MAX_CASES = 3


class ProspectiveCaptureError(RuntimeError):
    """Expected prospective-capture validation or execution failure."""


@dataclass(frozen=True)
class ProspectiveCase:
    case_id: str
    source_path: Path
    expected_contract_digest: str
    resolution: ResolutionResult


@dataclass(frozen=True)
class CompletedCase:
    case: ProspectiveCase
    bundle_path: Path
    bundle_digest: str
    run_id: str
    source_spec_digest: str
    execution_fingerprint: str
    request_plan_digest: str
    exitspec_contract_digest: str


def _strict_json_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    if not content or len(content) > _MAX_METADATA_BYTES:
        raise ProspectiveCaptureError(f"{label} is empty or exceeds its size limit")
    try:

        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProspectiveCaptureError(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise ProspectiveCaptureError(f"{label} must be a JSON object")
    return value


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        identity = _file_identity(metadata)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("input is not a regular file")
        if metadata.st_size > _MAX_METADATA_BYTES:
            raise ValueError("input exceeds its size limit")
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_METADATA_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, _MAX_METADATA_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        if (
            _file_identity(final_metadata) != identity
            or len(content) > _MAX_METADATA_BYTES
        ):
            raise ValueError("input changed during read")
        try:
            path_metadata = os.lstat(path)
        except OSError:
            raise ValueError("input changed during read") from None
        if _file_identity(path_metadata) != identity:
            raise ValueError("input changed during read")
        return content
    except (OSError, ValueError):
        raise ProspectiveCaptureError(f"{label} is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    return _strict_json_bytes(_read_regular(path, label=label), label=label)


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
    except OSError:
        raise ProspectiveCaptureError(
            "prospective metadata destination is unavailable"
        ) from None
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        raise ProspectiveCaptureError(
            "prospective metadata publication failed"
        ) from None
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_new(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _file_digest(path: Path, *, label: str) -> str:
    return sha256_digest(_read_regular(path, label=label))


def _metadata_digest(path: Path, *, label: str) -> str:
    return _file_digest(path, label=label)


def _validate_contract_digest(value: object, *, label: str) -> str:
    try:
        return TypeAdapter(Sha256Digest).validate_python(value, strict=True)
    except ValidationError:
        raise ProspectiveCaptureError(f"{label} has an invalid SHA-256 shape") from None


def _validate_run_id(value: object, *, label: str) -> str:
    try:
        return TypeAdapter(RunId).validate_python(value, strict=True)
    except ValidationError:
        raise ProspectiveCaptureError(f"{label} has an invalid run identity") from None


def _parse_case(value: str) -> tuple[str, Path]:
    case_id, separator, source_text = value.partition("=")
    if (
        not separator
        or _CASE_ID.fullmatch(case_id) is None
        or not source_text
        or len(source_text) > 4096
        or "\x00" in source_text
        or "\n" in source_text
        or "\r" in source_text
    ):
        raise ProspectiveCaptureError(
            "each --case must be CASE_ID=SOURCE_PATH with a safe case ID"
        )
    return case_id, Path(source_text).absolute()


def _parse_expected(value: str) -> tuple[str, str]:
    case_id, separator, digest = value.partition("=")
    if not separator or _CASE_ID.fullmatch(case_id) is None or not digest:
        raise ProspectiveCaptureError(
            "each --expected-contract-digest must be CASE_ID=SHA256_DIGEST"
        )
    return case_id, _validate_contract_digest(
        digest,
        label=f"expected contract digest for {case_id}",
    )


def _static_pin() -> dict[str, str]:
    try:
        return demo._check_static_assets()
    except demo.DemoError as error:
        raise ProspectiveCaptureError(str(error)) from None


def _reference_resolution() -> ResolutionResult:
    try:
        return resolve_experiment(
            demo.SOURCE_PATH,
            run_id=demo.STATIC_RUN_ID,
            strict=True,
        )
    except InferdromeError:
        raise ProspectiveCaptureError(
            "the pinned Qwen2.5 reference source cannot be resolved"
        ) from None


def _methodology_projection(resolution: ResolutionResult) -> dict[str, Any]:
    value = resolution.resolved_spec.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    value.pop("experiment", None)
    value.pop("links", None)
    return value


def _validate_methodology(
    resolution: ResolutionResult,
    reference: ResolutionResult,
    pin: dict[str, str],
) -> None:
    spec = resolution.resolved_spec
    target = spec.target
    traffic = spec.traffic
    if not isinstance(target, type(reference.resolved_spec.target)):
        raise ProspectiveCaptureError(
            "prospective source must use the pinned attached-vLLM target"
        )
    if _methodology_projection(resolution) != _methodology_projection(reference):
        raise ProspectiveCaptureError(
            "prospective source does not match the pinned Qwen2.5 methodology"
        )
    if (
        spec.execution.mode != "attached_endpoint"
        or spec.execution.producer_name != "vllm"
        or spec.execution.producer_version != pin["vllm_version"]
        or target.engine != "vllm"
        or target.model != pin["model_id"]
        or target.model_revision != pin["revision"]
        or target.tokenizer_revision != pin["revision"]
        or target.engine_version != pin["vllm_version"]
        or str(target.endpoint).rstrip("/") != "http://127.0.0.1:18080"
        or traffic.kind != "concurrent"
        or traffic.concurrency != 4
        or traffic.warmup_requests != 10
        or traffic.measured_requests != 100
    ):
        raise ProspectiveCaptureError(
            "prospective source does not use the exact pinned capture methodology"
        )


def validate_prospective_cases(
    case_arguments: list[str],
    expected_arguments: list[str],
) -> tuple[ProspectiveCase, ...]:
    """Resolve and cross-check all inputs before any host or model operation."""

    if not case_arguments and not expected_arguments:
        return ()
    if len(case_arguments) > _MAX_CASES or len(expected_arguments) > _MAX_CASES:
        raise ProspectiveCaptureError(
            "a prospective session supports at most three cases"
        )

    cases: dict[str, Path] = {}
    for argument in case_arguments:
        case_id, source_path = _parse_case(argument)
        if case_id in cases:
            raise ProspectiveCaptureError(f"prospective case is repeated: {case_id}")
        cases[case_id] = source_path
    expected: dict[str, str] = {}
    for argument in expected_arguments:
        case_id, digest = _parse_expected(argument)
        if case_id in expected:
            raise ProspectiveCaptureError(
                f"expected contract digest is repeated: {case_id}"
            )
        expected[case_id] = digest
    required = set(CASE_IDS)
    if set(cases) != required or set(expected) != required:
        raise ProspectiveCaptureError(
            "a runnable prospective session requires exactly the three named cases "
            "and one expected digest per case"
        )
    if len(set(expected.values())) != len(CASE_IDS):
        raise ProspectiveCaptureError(
            "the three expected ExitSpec contract digests must be pairwise distinct"
        )

    pin = _static_pin()
    reference = _reference_resolution()
    resolved: list[ProspectiveCase] = []
    for case_id in CASE_IDS:
        try:
            resolution = resolve_experiment(
                cases[case_id],
                run_id=demo.STATIC_RUN_ID,
                strict=True,
            )
        except InferdromeError:
            raise ProspectiveCaptureError(
                f"prospective source failed strict resolution: {case_id}"
            ) from None
        actual = resolution.resolved_spec.links.exitspec_contract_digest
        if actual is None or not hmac.compare_digest(actual, expected[case_id]):
            raise ProspectiveCaptureError(
                f"prospective source and expected ExitSpec digest disagree: {case_id}"
            )
        _validate_methodology(resolution, reference, pin)
        resolved.append(
            ProspectiveCase(
                case_id=case_id,
                source_path=cases[case_id],
                expected_contract_digest=expected[case_id],
                resolution=resolution,
            )
        )
    return tuple(resolved)


def _safe_member(root: Path, relative: str, *, label: str) -> Path:
    member = PurePosixPath(relative)
    if (
        member.is_absolute()
        or not member.parts
        or any(part in {"", ".", ".."} for part in member.parts)
    ):
        raise ProspectiveCaptureError(f"{label} has an unsafe relative path")
    candidate = root.joinpath(*member.parts)
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise ProspectiveCaptureError(
            f"{label} escapes the prospective session"
        ) from None
    return candidate


def _stage_case_input(case: ProspectiveCase, session_root: Path) -> Path:
    stage_root = session_root / "staged-inputs" / case.case_id
    source_path = stage_root / "experiment.yaml"
    _write_new(source_path, case.resolution.source_bytes)
    try:
        workload_path = resolve_safe_child(
            stage_root,
            case.resolution.resolved_spec.workload.path,
            label=f"{case.case_id} workload",
        )
    except InferdromeError:
        raise ProspectiveCaptureError(
            f"prospective workload path is unsafe: {case.case_id}"
        ) from None
    _write_new(workload_path, case.resolution.workload_bytes)
    return source_path


def _run_case(
    case: ProspectiveCase,
    *,
    session_root: Path,
    model_path: Path,
    gpu_index: int,
    startup_timeout_seconds: float,
) -> CompletedCase:
    source_path = _stage_case_input(case, session_root)
    case_root = session_root / "cases" / case.case_id
    case_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = demo._run_cli(
            case_root,
            "01-run",
            [
                "run",
                str(source_path),
                "--runs-root",
                str(case_root / "runs"),
                "--tokenizer-path",
                str(model_path),
                "--managed-local-vllm",
                "--managed-model-path",
                str(model_path),
                "--managed-gpu-index",
                str(gpu_index),
                "--managed-startup-timeout-seconds",
                str(startup_timeout_seconds),
                "--expected-exitspec-contract-digest",
                case.expected_contract_digest,
            ],
            expect_success=True,
            timeout_seconds=math.ceil(startup_timeout_seconds) + 1020,
        )
    except demo.DemoError as error:
        raise ProspectiveCaptureError(
            f"prospective case execution failed: {case.case_id}: {error}"
        ) from None
    output = _strict_json_bytes(result.stdout, label=f"{case.case_id} run output")
    bundle_text = output.get("bundle_path")
    bundle_digest = output.get("bundle_digest")
    run_id = output.get("run_id")
    if not isinstance(bundle_text, str) or not bundle_text:
        raise ProspectiveCaptureError(
            f"prospective case output omits its bundle identity: {case.case_id}"
        )
    declared_bundle_digest = _validate_contract_digest(
        bundle_digest,
        label=f"{case.case_id} output bundle digest",
    )
    declared_run_id = _validate_run_id(
        run_id,
        label=f"{case.case_id} output run identity",
    )
    if (
        output.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or output.get("integrity_status") != "VALID"
    ):
        raise ProspectiveCaptureError(
            f"prospective case bundle is not customer-eligible: {case.case_id}"
        )
    try:
        bundle_path = Path(bundle_text).resolve(strict=True)
        bundle_path.relative_to(session_root.resolve(strict=True))
    except (OSError, ValueError):
        raise ProspectiveCaptureError(
            f"prospective case bundle escapes its session: {case.case_id}"
        ) from None
    try:
        report = verify_bundle(
            bundle_path,
            expected_bundle_digest=declared_bundle_digest,
        )
    except InferdromeError:
        raise ProspectiveCaptureError(
            f"prospective case bundle failed verification: {case.case_id}"
        ) from None
    actual_digests = report.descriptor.digests
    actual_bundle_digest = _validate_contract_digest(
        report.bundle_digest,
        label=f"{case.case_id} verified bundle digest",
    )
    actual_source_spec_digest = _validate_contract_digest(
        actual_digests.source_spec_digest,
        label=f"{case.case_id} verified source-spec digest",
    )
    actual_execution_fingerprint = _validate_contract_digest(
        actual_digests.execution_fingerprint,
        label=f"{case.case_id} verified execution fingerprint",
    )
    actual_request_plan_digest = _validate_contract_digest(
        actual_digests.request_plan_digest,
        label=f"{case.case_id} verified request-plan digest",
    )
    actual_contract_digest = actual_digests.exitspec_contract_digest
    if actual_contract_digest is None:
        raise ProspectiveCaptureError(
            f"prospective case bundle omits its ExitSpec contract link: {case.case_id}"
        )
    actual_contract_digest = _validate_contract_digest(
        actual_contract_digest,
        label=f"{case.case_id} verified ExitSpec contract digest",
    )
    actual_run_id = _validate_run_id(
        report.run_id,
        label=f"{case.case_id} verified run identity",
    )
    if (
        report.descriptor.evidence_eligibility
        is not EvidenceEligibility.CUSTOMER_ELIGIBLE
        or actual_bundle_digest != declared_bundle_digest
        or actual_run_id != declared_run_id
        or actual_contract_digest != case.expected_contract_digest
        or actual_source_spec_digest != case.resolution.source_spec_digest
    ):
        raise ProspectiveCaptureError(
            f"prospective case bundle linkage disagrees: {case.case_id}"
        )
    return CompletedCase(
        case=case,
        bundle_path=bundle_path,
        bundle_digest=actual_bundle_digest,
        run_id=actual_run_id,
        source_spec_digest=actual_source_spec_digest,
        execution_fingerprint=actual_execution_fingerprint,
        request_plan_digest=actual_request_plan_digest,
        exitspec_contract_digest=actual_contract_digest,
    )


def _methodology_metadata(reference: ResolutionResult) -> dict[str, Any]:
    target = reference.resolved_spec.target
    if not hasattr(target, "model"):
        raise ProspectiveCaptureError("pinned reference target is not a model target")
    return {
        "concurrency": 4,
        "measured_requests": 100,
        "model": target.model,
        "producer_version": reference.resolved_spec.execution.producer_version,
        "requested_output_tokens": (
            reference.resolved_spec.workload.requested_output_tokens
        ),
        "seed": reference.resolved_spec.workload.seed,
        "temperature": reference.resolved_spec.workload.temperature,
        "warmup_requests": 10,
    }


def _case_metadata(completed: CompletedCase, session_root: Path) -> dict[str, Any]:
    bundle_relative = completed.bundle_path.relative_to(session_root).as_posix()
    original_spec = completed.bundle_path / "experiment.original.yaml"
    return {
        "bundle_digest": completed.bundle_digest,
        "bundle_path": bundle_relative,
        "case_id": completed.case.case_id,
        "execution_fingerprint": completed.execution_fingerprint,
        "exitspec_contract_digest": completed.exitspec_contract_digest,
        "original_spec_sha256": _file_digest(
            original_spec,
            label=f"{completed.case.case_id} original specification",
        ),
        "request_plan_digest": completed.request_plan_digest,
        "run_id": completed.run_id,
        "source_spec_digest": completed.source_spec_digest,
    }


def _repository_commit() -> str:
    try:
        value = demo._git_output("rev-parse", "--verify", "HEAD")
    except demo.DemoError:
        raise ProspectiveCaptureError(
            "Inferdrome repository identity cannot be inspected"
        ) from None
    if _COMMIT.fullmatch(value) is None:
        raise ProspectiveCaptureError("Inferdrome repository commit is invalid")
    return value


def _write_session_metadata(
    session_root: Path,
    completed: tuple[CompletedCase, ...],
    *,
    repository_commit: str,
    host_preparation_sha256: str,
    reference: ResolutionResult,
) -> Path:
    cases = [_case_metadata(item, session_root) for item in completed]
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    capture = {
        "acceptance_boundary": "PENDING_EXTERNAL_EXITSPEC",
        "cases": cases,
        "contract_binding": {
            "chronology": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
            "expected_digest_source": "EXPLICIT_OPERATOR_INPUT",
            "verdict_owner": "EXTERNAL_EXITSPEC",
        },
        "generated_at": generated_at,
        "host_preparation_sha256": host_preparation_sha256,
        "methodology": _methodology_metadata(reference),
        "repository_commit": repository_commit,
        "schema_version": SCHEMA_VERSION,
    }
    capture_path = session_root / "capture-manifest.json"
    _write_json(capture_path, capture)
    capture_digest = _metadata_digest(
        capture_path, label="prospective capture manifest"
    )

    handoff = {
        "acceptance_boundary": {
            "inferdrome_acceptance_verdict": None,
            "verdict_owner": "EXTERNAL_EXITSPEC",
        },
        "capture_manifest_sha256": capture_digest,
        "cases": [
            {
                "bundle_digest": item["bundle_digest"],
                "case_id": item["case_id"],
                "exitspec_contract_digest": item["exitspec_contract_digest"],
                "source_spec_digest": item["source_spec_digest"],
            }
            for item in cases
        ],
        "contract_binding": {
            "chronology": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
            "producer_digests_are_observational": True,
        },
        "generated_at": generated_at,
        "host_preparation_sha256": host_preparation_sha256,
        "repository_commit": repository_commit,
        "schema_version": HANDOFF_SCHEMA_VERSION,
    }
    handoff_path = session_root / "handoff-manifest.json"
    _write_json(handoff_path, handoff)
    handoff_digest = _metadata_digest(
        handoff_path, label="prospective handoff manifest"
    )

    publication = {
        "acceptance_verdict": None,
        "capture_manifest_sha256": capture_digest,
        "contract_digests": [item["exitspec_contract_digest"] for item in cases],
        "handoff_manifest_sha256": handoff_digest,
        "host_preparation_sha256": host_preparation_sha256,
        "owner_publication_approval_required": True,
        "publication_performed": False,
        "publication_status": "EXTERNAL_ONLY",
        "schema_version": PUBLICATION_SCHEMA_VERSION,
    }
    publication_path = session_root / "publication-review.json"
    _write_json(publication_path, publication)
    publication_digest = _metadata_digest(
        publication_path,
        label="prospective publication metadata",
    )

    receipt = {
        "acceptance_boundary": "PENDING_EXTERNAL_EXITSPEC",
        "capture_manifest_sha256": capture_digest,
        "cases": cases,
        "contract_digests": [item["exitspec_contract_digest"] for item in cases],
        "handoff_manifest_sha256": handoff_digest,
        "host_preparation_sha256": host_preparation_sha256,
        "publication_review_sha256": publication_digest,
        "publication_status": "EXTERNAL_ONLY",
        "repository_commit": repository_commit,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "CAPTURED_PENDING_EXTERNAL_EXITSPEC",
    }
    receipt_path = session_root / "prospective-capture-receipt.json"
    _write_json(receipt_path, receipt)
    return receipt_path


def _expected_from_arguments(expected_arguments: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for argument in expected_arguments:
        case_id, digest = _parse_expected(argument)
        if case_id in expected:
            raise ProspectiveCaptureError(
                f"expected contract digest is repeated: {case_id}"
            )
        expected[case_id] = digest
    if set(expected) != set(CASE_IDS):
        raise ProspectiveCaptureError(
            "verification requires one expected contract digest for each named case"
        )
    if len(set(expected.values())) != len(CASE_IDS):
        raise ProspectiveCaptureError(
            "the three expected ExitSpec contract digests must be pairwise distinct"
        )
    return expected


def verify_session(session_root: Path, expected_arguments: list[str]) -> dict[str, Any]:
    """Verify prospective metadata and all linked bundles without side effects."""

    root = session_root.absolute()
    if not root.is_dir() or root.is_symlink():
        raise ProspectiveCaptureError(
            "prospective session root is unavailable or unsafe"
        )
    expected = _expected_from_arguments(expected_arguments)
    capture_path = root / "capture-manifest.json"
    handoff_path = root / "handoff-manifest.json"
    publication_path = root / "publication-review.json"
    receipt_path = root / "prospective-capture-receipt.json"
    capture = _read_json(capture_path, label="prospective capture manifest")
    handoff = _read_json(handoff_path, label="prospective handoff manifest")
    publication = _read_json(publication_path, label="prospective publication metadata")
    receipt = _read_json(receipt_path, label="prospective capture receipt")
    _require_fields(
        capture,
        {
            "acceptance_boundary",
            "cases",
            "contract_binding",
            "generated_at",
            "host_preparation_sha256",
            "methodology",
            "repository_commit",
            "schema_version",
        },
        label="prospective capture manifest",
    )
    _require_fields(
        handoff,
        {
            "acceptance_boundary",
            "capture_manifest_sha256",
            "cases",
            "contract_binding",
            "generated_at",
            "host_preparation_sha256",
            "repository_commit",
            "schema_version",
        },
        label="prospective handoff manifest",
    )
    _require_fields(
        publication,
        {
            "acceptance_verdict",
            "capture_manifest_sha256",
            "contract_digests",
            "handoff_manifest_sha256",
            "host_preparation_sha256",
            "owner_publication_approval_required",
            "publication_performed",
            "publication_status",
            "schema_version",
        },
        label="prospective publication metadata",
    )
    _require_fields(
        receipt,
        {
            "acceptance_boundary",
            "capture_manifest_sha256",
            "cases",
            "contract_digests",
            "handoff_manifest_sha256",
            "host_preparation_sha256",
            "publication_review_sha256",
            "publication_status",
            "repository_commit",
            "schema_version",
            "status",
        },
        label="prospective capture receipt",
    )
    if (
        capture.get("schema_version") != SCHEMA_VERSION
        or capture.get("acceptance_boundary") != "PENDING_EXTERNAL_EXITSPEC"
    ):
        raise ProspectiveCaptureError(
            "prospective capture manifest has an unsupported shape"
        )
    cases = capture.get("cases")
    if not isinstance(cases, list) or len(cases) != _MAX_CASES:
        raise ProspectiveCaptureError(
            "prospective capture manifest has the wrong case count"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for item in cases:
        if not isinstance(item, dict):
            raise ProspectiveCaptureError("prospective capture case is not an object")
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id in by_id:
            raise ProspectiveCaptureError("prospective capture cases are not unique")
        by_id[case_id] = item
    if set(by_id) != set(CASE_IDS):
        raise ProspectiveCaptureError("prospective capture cases are not unique")
    if [item.get("case_id") for item in cases] != list(CASE_IDS):
        raise ProspectiveCaptureError(
            "prospective capture cases are not canonically ordered"
        )
    case_digest_fields = (
        "bundle_digest",
        "execution_fingerprint",
        "exitspec_contract_digest",
        "original_spec_sha256",
        "request_plan_digest",
        "source_spec_digest",
    )
    for case_id in CASE_IDS:
        item = by_id[case_id]
        _require_fields(
            item,
            {
                "bundle_digest",
                "bundle_path",
                "case_id",
                "execution_fingerprint",
                "exitspec_contract_digest",
                "original_spec_sha256",
                "request_plan_digest",
                "run_id",
                "source_spec_digest",
            },
            label="prospective capture case",
        )
        for field in case_digest_fields:
            _validate_contract_digest(
                item[field],
                label=f"{case_id} {field}",
            )
        _validate_run_id(item["run_id"], label=f"{case_id} run identity")

    contract_binding = capture.get("contract_binding")
    if not isinstance(contract_binding, dict) or contract_binding != {
        "chronology": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
        "expected_digest_source": "EXPLICIT_OPERATOR_INPUT",
        "verdict_owner": "EXTERNAL_EXITSPEC",
    }:
        raise ProspectiveCaptureError("prospective capture contract binding is invalid")
    if (
        not isinstance(capture.get("host_preparation_sha256"), str)
        or _COMMIT.fullmatch(str(capture.get("repository_commit"))) is None
    ):
        raise ProspectiveCaptureError("prospective capture provenance is invalid")
    _validate_contract_digest(
        str(capture["host_preparation_sha256"]),
        label="prospective capture host preparation digest",
    )
    if capture.get("methodology") != _methodology_metadata(_reference_resolution()):
        raise ProspectiveCaptureError("prospective capture methodology is invalid")

    for case_id in CASE_IDS:
        item = by_id[case_id]
        if item.get("exitspec_contract_digest") != expected[case_id]:
            raise ProspectiveCaptureError(
                f"prospective capture contract digest disagrees: {case_id}"
            )
        bundle = _safe_member(
            root, str(item.get("bundle_path", "")), label=f"{case_id} bundle"
        )
        try:
            report = verify_bundle(
                bundle,
                expected_bundle_digest=item["bundle_digest"],
            )
        except (InferdromeError, TypeError):
            raise ProspectiveCaptureError(
                f"prospective capture bundle failed verification: {case_id}"
            ) from None
        original = bundle / "experiment.original.yaml"
        actual_digests = report.descriptor.digests
        actual_bundle_digest = _validate_contract_digest(
            report.bundle_digest,
            label=f"{case_id} verified bundle digest",
        )
        actual_execution_fingerprint = _validate_contract_digest(
            actual_digests.execution_fingerprint,
            label=f"{case_id} verified execution fingerprint",
        )
        actual_request_plan_digest = _validate_contract_digest(
            actual_digests.request_plan_digest,
            label=f"{case_id} verified request-plan digest",
        )
        actual_source_spec_digest = _validate_contract_digest(
            actual_digests.source_spec_digest,
            label=f"{case_id} verified source-spec digest",
        )
        actual_contract_digest = actual_digests.exitspec_contract_digest
        if actual_contract_digest is None:
            raise ProspectiveCaptureError(
                f"{case_id} verified bundle omits its ExitSpec contract link"
            )
        actual_contract_digest = _validate_contract_digest(
            actual_contract_digest,
            label=f"{case_id} verified ExitSpec contract digest",
        )
        actual_run_id = _validate_run_id(
            report.run_id,
            label=f"{case_id} verified run identity",
        )
        if (
            report.descriptor.evidence_eligibility
            is not EvidenceEligibility.CUSTOMER_ELIGIBLE
            or actual_bundle_digest != item["bundle_digest"]
            or actual_execution_fingerprint != item["execution_fingerprint"]
            or actual_request_plan_digest != item["request_plan_digest"]
            or actual_source_spec_digest != item["source_spec_digest"]
            or actual_contract_digest != item["exitspec_contract_digest"]
            or _file_digest(original, label=f"{case_id} original specification")
            != item["original_spec_sha256"]
            or actual_run_id != item["run_id"]
        ):
            raise ProspectiveCaptureError(
                f"prospective capture bundle linkage disagrees: {case_id}"
            )

    capture_digest = _metadata_digest(
        capture_path, label="prospective capture manifest"
    )
    handoff_digest = _metadata_digest(
        handoff_path, label="prospective handoff manifest"
    )
    publication_digest = _metadata_digest(
        publication_path,
        label="prospective publication metadata",
    )
    handoff_acceptance = handoff.get("acceptance_boundary")
    handoff_binding = handoff.get("contract_binding")
    if not isinstance(handoff_acceptance, dict) or not isinstance(
        handoff_binding, dict
    ):
        raise ProspectiveCaptureError(
            "prospective handoff metadata has an invalid shape"
        )
    expected_handoff_cases = [
        {
            "bundle_digest": by_id[case_id]["bundle_digest"],
            "case_id": case_id,
            "exitspec_contract_digest": by_id[case_id]["exitspec_contract_digest"],
            "source_spec_digest": by_id[case_id]["source_spec_digest"],
        }
        for case_id in CASE_IDS
    ]
    if (
        handoff.get("cases") != expected_handoff_cases
        or handoff_acceptance
        != {
            "inferdrome_acceptance_verdict": None,
            "verdict_owner": "EXTERNAL_EXITSPEC",
        }
        or handoff_binding
        != {
            "chronology": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
            "producer_digests_are_observational": True,
        }
    ):
        raise ProspectiveCaptureError("prospective handoff contract binding is invalid")
    if (
        handoff.get("schema_version") != HANDOFF_SCHEMA_VERSION
        or handoff.get("capture_manifest_sha256") != capture_digest
        or handoff.get("repository_commit") != capture.get("repository_commit")
        or handoff.get("host_preparation_sha256")
        != capture.get("host_preparation_sha256")
        or publication.get("schema_version") != PUBLICATION_SCHEMA_VERSION
        or publication.get("capture_manifest_sha256") != capture_digest
        or publication.get("handoff_manifest_sha256") != handoff_digest
        or publication.get("host_preparation_sha256")
        != capture.get("host_preparation_sha256")
        or publication.get("contract_digests")
        != [expected[case_id] for case_id in CASE_IDS]
        or publication.get("owner_publication_approval_required") is not True
        or publication.get("acceptance_verdict") is not None
        or publication.get("publication_status") != "EXTERNAL_ONLY"
        or publication.get("publication_performed") is not False
        or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("capture_manifest_sha256") != capture_digest
        or receipt.get("handoff_manifest_sha256") != handoff_digest
        or receipt.get("host_preparation_sha256")
        != capture.get("host_preparation_sha256")
        or receipt.get("publication_review_sha256") != publication_digest
        or receipt.get("cases") != cases
        or receipt.get("repository_commit") != capture.get("repository_commit")
        or receipt.get("status") != "CAPTURED_PENDING_EXTERNAL_EXITSPEC"
        or receipt.get("publication_status") != "EXTERNAL_ONLY"
        or receipt.get("contract_digests")
        != [expected[case_id] for case_id in CASE_IDS]
    ):
        raise ProspectiveCaptureError(
            "prospective handoff, publication, and receipt metadata disagree"
        )
    return {
        "capture_manifest_sha256": capture_digest,
        "contract_digests": [expected[case_id] for case_id in CASE_IDS],
        "publication_status": publication["publication_status"],
        "receipt_path": str(receipt_path),
        "schema_version": "inferdrome.prospective-real-gpu-verification.v1",
        "valid": True,
    }


def _require_fields(
    value: dict[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise ProspectiveCaptureError(f"{label} has an unexpected shape")


@contextmanager
def _interruptible_capture() -> Any:
    """Forward controller interrupts through the existing child cleanup path."""

    previous: dict[signal.Signals, Any] = {}

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            previous[selected_signal] = signal.getsignal(selected_signal)
            signal.signal(selected_signal, interrupt)
        yield
    finally:
        for selected_signal, handler in previous.items():
            signal.signal(selected_signal, handler)


def _gpu_index(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("GPU index must be an integer")
    index = int(value)
    if index > 255:
        raise argparse.ArgumentTypeError("GPU index must be between 0 and 255")
    return index


def _startup_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("startup timeout must be a number") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
        raise argparse.ArgumentTypeError(
            "startup timeout must be between 1 and 3600 seconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a prospective ExitSpec-linked Qwen2.5 real-GPU capture"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate static assets and optional linked inputs without "
            "provider/GPU mutation"
        ),
    )
    mode.add_argument(
        "--verify",
        metavar="SESSION_ROOT",
        help="verify one completed prospective session without mutation",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="CASE_ID=SOURCE_PATH",
        help="explicit linked source; repeat once for each named case",
    )
    parser.add_argument(
        "--expected-contract-digest",
        action="append",
        default=[],
        metavar="CASE_ID=SHA256_DIGEST",
        help="exact externally frozen ExitSpec digest; repeat once per case",
    )
    parser.add_argument(
        "--state-root",
        default=str(REPOSITORY_ROOT / ".inferdrome-gpu"),
        help="directory created by prepare_real_gpu_host.sh",
    )
    parser.add_argument(
        "--output-root",
        default=str(REPOSITORY_ROOT / "gpu-proof-output" / "prospective"),
        help="parent directory for a new disposable prospective session",
    )
    parser.add_argument("--gpu-index", type=_gpu_index, default=0)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_startup_timeout,
        default=900.0,
    )
    return parser


def _check(args: argparse.Namespace) -> int:
    pin = _static_pin()
    cases = validate_prospective_cases(args.case, args.expected_contract_digest)
    if not cases:
        print(
            json.dumps(
                {
                    "model_id": pin["model_id"],
                    "provider_or_gpu_mutation": "NONE",
                    "status": "INERT_NO_CONTRACTS",
                    "valid": True,
                },
                sort_keys=True,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": case.case_id,
                        "expected_contract_digest": case.expected_contract_digest,
                        "source_spec_digest": case.resolution.source_spec_digest,
                    }
                    for case in cases
                ],
                "provider_or_gpu_mutation": "NONE",
                "status": "PROSPECTIVE_INPUTS_VALID",
                "valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _run(args: argparse.Namespace) -> Path:
    with _interruptible_capture():
        return _run_without_interrupts(args)


def _run_without_interrupts(args: argparse.Namespace) -> Path:
    cases = validate_prospective_cases(args.case, args.expected_contract_digest)
    if not cases:
        raise ProspectiveCaptureError(
            "a runnable prospective session requires explicit linked sources "
            "and digests"
        )
    pin = _static_pin()
    state_root = Path(args.state_root).absolute()
    try:
        model_path, repository_commit = demo._require_clean_prepared_host(
            state_root,
            pin,
        )
    except demo.DemoError as error:
        raise ProspectiveCaptureError(str(error)) from None
    host_preparation_sha256 = _file_digest(
        state_root / "host-preparation.json",
        label="GPU host preparation receipt",
    )
    output_root = Path(args.output_root).absolute()
    if "\n" in str(output_root) or "\r" in str(output_root):
        raise ProspectiveCaptureError(
            "prospective output path cannot contain line breaks"
        )
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        session_root = Path(
            tempfile.mkdtemp(prefix="prospective-real-gpu-", dir=output_root)
        ).absolute()
    except OSError:
        raise ProspectiveCaptureError(
            "prospective output directory is unavailable"
        ) from None
    completed: list[CompletedCase] = []
    for case in cases:
        completed.append(
            _run_case(
                case,
                session_root=session_root,
                model_path=model_path,
                gpu_index=args.gpu_index,
                startup_timeout_seconds=args.startup_timeout_seconds,
            )
        )
    return _write_session_metadata(
        session_root,
        tuple(completed),
        repository_commit=repository_commit,
        host_preparation_sha256=host_preparation_sha256,
        reference=_reference_resolution(),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check:
            return _check(args)
        if args.verify is not None:
            if args.case:
                raise ProspectiveCaptureError("--verify does not accept --case")
            result = verify_session(
                Path(args.verify),
                args.expected_contract_digest,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        receipt_path = _run(args)
        print(receipt_path.read_text(encoding="utf-8"), end="")
        print(f"receipt_path={receipt_path}")
        return 0
    except KeyboardInterrupt:
        print(
            "prospective-real-gpu-capture: interrupted; child cleanup was requested",
            file=sys.stderr,
        )
        return 130
    except (InferdromeError, ProspectiveCaptureError) as error:
        print(f"prospective-real-gpu-capture: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
