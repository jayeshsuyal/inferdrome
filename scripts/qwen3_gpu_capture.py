#!/usr/bin/env python3
"""Create and independently verify one bounded Qwen3 A10 capability spike."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from inferdrome import __version__
from inferdrome.bundle import recalculate_bundle
from inferdrome.domain.metrics import Aggregation, MetricId
from inferdrome.domain.states import EvidenceEligibility
from inferdrome.errors import InferdromeError
from inferdrome.gpu_proof import expected_vllm_source_wheel
from inferdrome.qwen3_campaign import (
    CANONICAL_CAMPAIGN_ID,
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_host_dependencies,
    qwen3_host_dependencies_sha256,
    qwen3_launch_documents,
    qwen3_model_manifest,
    qwen3_model_manifest_sha256,
    qwen3_profile_sha256,
)
from inferdrome.qwen3_tokenizer import (
    expected_qwen3_tokenizer_file_verification,
)

if __package__:
    from scripts import real_gpu_capture
else:
    import real_gpu_capture

_CAPTURE_SCHEMA = "inferdrome.qwen3-a10-capability-capture.v1"
_HOST_PREPARATION_SCHEMA = "inferdrome.qwen3-host-preparation.v1"
_SOURCE_RELATIVE = "campaigns/v1/qwen3-8b-concurrency-1.yaml"
_PROFILE_RELATIVE = "campaigns/v1/profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json"
_WORKLOAD_MANIFEST_RELATIVE = (
    "campaigns/v1/workloads/qwen-text-mixed-length-v1.manifest.json"
)
_MODEL_MANIFEST_RELATIVE = "campaigns/v1/profiles/qwen3-8b-model-files.json"
_HOST_DEPENDENCIES_RELATIVE = "campaigns/v1/profiles/qwen3-8b-host-dependencies.json"
_EXPECTED_GPU_MODEL = "NVIDIA A10"
_HARDWARE_OBSERVATION = "SELECTED_GPU_REPORTED_NVIDIA_A10_SINGLE_CUDA_DEVICE"
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}\Z")

_SUPPORT_PATHS = {
    "campaign_profile": "support/campaign-profile.json",
    "host_preparation": "support/host-preparation.json",
    "host_dependencies": "support/host-dependencies.json",
    "inferdrome_version": "support/inferdrome-version.txt",
    "model_files": "support/model-files.json",
    "python_packages": "support/python-packages.txt",
    "source_experiment": "support/source.yaml",
    "vllm_version": "support/vllm-version.txt",
    "workload_manifest": "support/workload-manifest.json",
}


class Qwen3CaptureError(RuntimeError):
    """Expected, user-facing Qwen3 capture failure."""


def _raise_capture(error: real_gpu_capture.CaptureError) -> None:
    raise Qwen3CaptureError(str(error)) from None


def _read_regular(path: Path, *, label: str) -> bytes:
    try:
        return real_gpu_capture._read_regular(path, label=label)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        return real_gpu_capture._read_json(path, label=label)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)


def _sha256_file(path: Path, *, label: str) -> str:
    try:
        return real_gpu_capture._sha256_file(path, label=label)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)


def _version_file(path: Path, *, label: str) -> str:
    try:
        return real_gpu_capture._version_file(path, label=label)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)


def _require_exact_fields(
    value: dict[str, Any], expected: set[str], *, label: str
) -> None:
    if set(value) != expected:
        raise Qwen3CaptureError(f"{label} has an unexpected shape")


def _required_text(value: dict[str, Any], key: str, *, label: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise Qwen3CaptureError(f"{label} omits {key}")
    return selected


def _require_commit(value: str, *, label: str) -> str:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise Qwen3CaptureError(f"{label} is not a full lowercase Git commit")
    return value


def _require_timestamp(value: str, *, label: str) -> None:
    if not value.endswith("Z"):
        raise Qwen3CaptureError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise Qwen3CaptureError(f"{label} is not an ISO 8601 timestamp") from None
    if parsed.tzinfo != UTC:
        raise Qwen3CaptureError(f"{label} is not a UTC timestamp")


def _relative_directory(root: Path, value: str, *, label: str) -> Path:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in value
    ):
        raise Qwen3CaptureError(f"{label} is not a safe relative path")
    path = root.joinpath(*candidate.parts)
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        path_resolved.relative_to(root_resolved)
        metadata = os.lstat(path)
    except (OSError, ValueError):
        raise Qwen3CaptureError(f"{label} is unavailable or escapes capture") from None
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise Qwen3CaptureError(f"{label} must be a real directory")
    return path


def _support_entry(capture_root: Path, relative: str) -> dict[str, str]:
    path = capture_root / relative
    return {
        "path": relative,
        "sha256": _sha256_file(path, label=f"support file {relative}"),
    }


def _expected_support_bytes() -> dict[str, bytes]:
    generated = qwen3_launch_documents()
    return {
        "campaign_profile": generated[_PROFILE_RELATIVE],
        "host_dependencies": generated[_HOST_DEPENDENCIES_RELATIVE],
        "model_files": generated[_MODEL_MANIFEST_RELATIVE],
        "source_experiment": generated[_SOURCE_RELATIVE],
        "workload_manifest": generated[_WORKLOAD_MANIFEST_RELATIVE],
    }


def _verify_support(
    capture_root: Path,
    support: object,
    repository_commit: str,
) -> str:
    if not isinstance(support, dict) or set(support) != set(_SUPPORT_PATHS):
        raise Qwen3CaptureError("capture support map has an unexpected shape")
    for key, relative in _SUPPORT_PATHS.items():
        entry = support.get(key)
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise Qwen3CaptureError(f"capture support entry is invalid: {key}")
        if entry.get("path") != relative:
            raise Qwen3CaptureError(f"capture support path drifted: {key}")
        if entry.get("sha256") != _support_entry(capture_root, relative)["sha256"]:
            raise Qwen3CaptureError(f"capture support digest drifted: {key}")

    for key, expected in _expected_support_bytes().items():
        path = capture_root / _SUPPORT_PATHS[key]
        if _read_regular(path, label=f"support file {key}") != expected:
            raise Qwen3CaptureError(f"capture support bytes drifted: {key}")

    packages = capture_root / _SUPPORT_PATHS["python_packages"]
    host_path = capture_root / _SUPPORT_PATHS["host_preparation"]
    host = _read_json(host_path, label="Qwen3 host-preparation receipt")
    _require_exact_fields(
        host,
        {
            "architecture",
            "campaign_id",
            "host_dependencies_sha256",
            "model_directory",
            "model_id",
            "model_manifest_sha256",
            "model_revision",
            "model_snapshot",
            "prepared_at",
            "profile_id",
            "profile_sha256",
            "python_packages_sha256",
            "repository_commit",
            "schema_version",
            "source_provenance",
            "tokenizer_files",
            "tokenizer_revision",
            "tokenizers_wheel_filename",
            "tokenizers_wheel_sha256",
            "vllm_wheel_filename",
            "vllm_wheel_sha256",
        },
        label="Qwen3 host-preparation receipt",
    )
    architecture = _required_text(host, "architecture", label="host preparation")
    if architecture not in {"aarch64", "x86_64"}:
        raise Qwen3CaptureError("host-preparation architecture is unsupported")
    expected_wheel, expected_wheel_digest = expected_vllm_source_wheel(architecture)
    dependency_manifest = qwen3_host_dependencies()
    tokenizers_matches = [
        item
        for item in dependency_manifest["distributions"]
        if item["architecture"] == architecture
    ]
    if len(tokenizers_matches) != 1:
        raise AssertionError
    tokenizers_pin = tokenizers_matches[0]
    expected = {
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "host_dependencies_sha256": qwen3_host_dependencies_sha256(),
        "model_id": QWEN3_8B_MODEL_ID,
        "model_manifest_sha256": qwen3_model_manifest_sha256(),
        "model_revision": QWEN3_8B_REVISION,
        "profile_id": QWEN3_8B_PROFILE_ID,
        "profile_sha256": qwen3_profile_sha256(),
        "repository_commit": repository_commit,
        "schema_version": _HOST_PREPARATION_SCHEMA,
        "tokenizer_revision": QWEN3_8B_REVISION,
        "tokenizers_wheel_filename": tokenizers_pin["filename"],
        "tokenizers_wheel_sha256": tokenizers_pin["sha256"],
        "vllm_wheel_filename": expected_wheel,
        "vllm_wheel_sha256": expected_wheel_digest,
    }
    for key, expected_value in expected.items():
        if host.get(key) != expected_value:
            raise Qwen3CaptureError(f"host-preparation field drifted: {key}")
    model_directory = _required_text(host, "model_directory", label="host preparation")
    if not Path(model_directory).is_absolute() or any(
        ord(character) < 32 for character in model_directory
    ):
        raise Qwen3CaptureError("host-preparation model directory is invalid")
    source_provenance = host.get("source_provenance")
    if (
        not isinstance(source_provenance, dict)
        or set(source_provenance)
        != {"repository_commit", "source_archive_sha256", "transport"}
        or source_provenance.get("repository_commit") != repository_commit
        or source_provenance.get("transport") != "git-archive-exact-head-tree-v1"
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(source_provenance.get("source_archive_sha256")),
        )
        is None
    ):
        raise Qwen3CaptureError("host-preparation source provenance drifted")
    model_snapshot = host.get("model_snapshot")
    model_manifest = qwen3_model_manifest()
    if (
        not isinstance(model_snapshot, dict)
        or model_snapshot.get("kind") != "model"
        or model_snapshot.get("root") != model_directory
        or model_snapshot.get("revision") != QWEN3_8B_REVISION
        or model_snapshot.get("sha256") != qwen3_expected_snapshot_sha256()
        or model_snapshot.get("file_count") != model_manifest["file_count"]
        or model_snapshot.get("total_bytes") != model_manifest["total_bytes"]
        or model_snapshot.get("hash_policy") != "regular-files-excluding-dot-cache-v1"
    ):
        raise Qwen3CaptureError("host-preparation model snapshot drifted")
    _require_timestamp(
        _required_text(host, "prepared_at", label="host preparation"),
        label="host preparation time",
    )
    tokenizer_files = expected_qwen3_tokenizer_file_verification().model_dump(
        mode="json"
    )
    if host.get("tokenizer_files") != tokenizer_files:
        raise Qwen3CaptureError("host-preparation tokenizer verification drifted")
    if host.get("python_packages_sha256") != _sha256_file(
        packages, label="captured Python package inventory"
    ):
        raise Qwen3CaptureError("host-preparation package inventory drifted")
    if (
        _version_file(
            capture_root / _SUPPORT_PATHS["vllm_version"],
            label="captured vLLM version",
        )
        != "0.26.0"
    ):
        raise Qwen3CaptureError("captured vLLM version drifted")
    if (
        _version_file(
            capture_root / _SUPPORT_PATHS["inferdrome_version"],
            label="captured Inferdrome version",
        )
        != __version__
    ):
        raise Qwen3CaptureError("captured Inferdrome version drifted")
    return source_provenance["source_archive_sha256"]


def _run_record(capture_root: Path) -> dict[str, Any]:
    runs_root = _relative_directory(capture_root, "runs", label="capture runs root")
    try:
        entries = sorted(runs_root.iterdir(), key=lambda item: item.name)
    except OSError:
        raise Qwen3CaptureError("capture runs root cannot be read") from None
    if len(entries) != 1 or _RUN_ID_PATTERN.fullmatch(entries[0].name) is None:
        raise Qwen3CaptureError("Qwen3 spike must contain exactly one run")
    _relative_directory(
        capture_root,
        f"runs/{entries[0].name}",
        label="Qwen3 run workspace",
    )
    bundle_path = _relative_directory(
        capture_root,
        f"runs/{entries[0].name}/bundle",
        label="Qwen3 evidence bundle",
    )
    try:
        analysis = recalculate_bundle(bundle_path)
    except InferdromeError as error:
        raise Qwen3CaptureError(
            f"Qwen3 evidence bundle failed semantic recalculation: {error}"
        ) from None
    report = analysis.verification
    if report.run_id != entries[0].name:
        raise Qwen3CaptureError("Qwen3 bundle run identity drifted")
    if (
        report.descriptor.evidence_eligibility
        is not EvidenceEligibility.CUSTOMER_ELIGIBLE
    ):
        raise Qwen3CaptureError("Qwen3 capability evidence is not customer-eligible")
    measurements = analysis.reduction.measurements.measurements
    by_key = {
        (measurement.metric, measurement.aggregation): measurement
        for measurement in measurements
    }
    expected_counts = {
        MetricId.MEASURED_REQUEST_COUNT: 96,
        MetricId.SUCCESSFUL_REQUEST_COUNT: 96,
        MetricId.FAILED_REQUEST_COUNT: 0,
    }
    for metric, expected_count in expected_counts.items():
        measurement = by_key.get((metric, Aggregation.COUNT))
        if (
            measurement is None
            or measurement.value != expected_count
            or measurement.sample_count != expected_count
        ):
            raise Qwen3CaptureError(
                "Qwen3 capability spike did not complete 96 of 96 requests"
            )
    ttft_measurements = [
        measurement
        for measurement in measurements
        if measurement.metric is MetricId.TTFT_NS
    ]
    if {measurement.aggregation for measurement in ttft_measurements} != {
        Aggregation.MEAN,
        Aggregation.P50,
        Aggregation.P95,
        Aggregation.P99,
    } or any(measurement.sample_count != 96 for measurement in ttft_measurements):
        raise Qwen3CaptureError(
            "Qwen3 capability spike does not contain 96 observed TTFT samples"
        )

    invocation = _read_json(
        bundle_path / "native" / "invocation.json",
        label="Qwen3 producer invocation",
    )
    profile = invocation.get("campaign_profile")
    proof = invocation.get("local_gpu_proof")
    if (
        not isinstance(profile, dict)
        or profile.get("profile_id") != QWEN3_8B_PROFILE_ID
    ):
        raise Qwen3CaptureError("Qwen3 invocation profile binding is absent")
    if profile.get("profile_sha256") != qwen3_profile_sha256():
        raise Qwen3CaptureError("Qwen3 invocation profile digest drifted")
    if not isinstance(proof, dict):
        raise Qwen3CaptureError("Qwen3 local GPU proof is absent")
    gpus = proof.get("gpus")
    if (
        not isinstance(gpus, list)
        or len(gpus) != 1
        or not isinstance(gpus[0], dict)
        or gpus[0].get("model") != _EXPECTED_GPU_MODEL
        or proof.get("torch_cuda_device_count") != 1
        or proof.get("selected_gpu_indices") != [gpus[0].get("index")]
    ):
        raise Qwen3CaptureError("Qwen3 capability spike did not run on one NVIDIA A10")
    host = _read_json(
        capture_root / _SUPPORT_PATHS["host_preparation"],
        label="Qwen3 host-preparation receipt",
    )
    host_snapshot = host.get("model_snapshot")
    proof_snapshot = proof.get("model_snapshot")
    tokenizer_snapshot = proof.get("tokenizer_snapshot")
    if (
        not isinstance(host_snapshot, dict)
        or proof_snapshot != host_snapshot
        or not isinstance(tokenizer_snapshot, dict)
        or tokenizer_snapshot != {**host_snapshot, "kind": "tokenizer"}
    ):
        raise Qwen3CaptureError(
            "Qwen3 host preparation and runtime model snapshots disagree"
        )

    resolved = _read_json(
        bundle_path / "experiment.resolved.json",
        label="Qwen3 resolved experiment",
    )
    traffic = resolved.get("traffic")
    if not isinstance(traffic, dict) or traffic.get("concurrency") != 1:
        raise Qwen3CaptureError("Qwen3 capability spike concurrency drifted")
    return {
        "bundle_digest": report.bundle_digest,
        "bundle_path": f"runs/{entries[0].name}/bundle",
        "evidence_eligibility": report.descriptor.evidence_eligibility.value,
        "failed_requests": 0,
        "run_id": entries[0].name,
        "spike_outcome": "SPIKE_SUCCEEDED",
        "successful_requests": 96,
        "torch_cuda_device_count": 1,
        "ttft_samples": 96,
        "workspace_path": f"runs/{entries[0].name}",
    }


def _manifest_value(capture_root: Path, repository_commit: str) -> dict[str, Any]:
    support = {
        key: _support_entry(capture_root, relative)
        for key, relative in _SUPPORT_PATHS.items()
    }
    source_archive_sha256 = _verify_support(
        capture_root,
        support,
        repository_commit,
    )
    run = _run_record(capture_root)
    return {
        "acceptance_verdict": None,
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "capture_kind": "BOUNDED_RUNTIME_CAPABILITY_SPIKE",
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "expected_gpu_model": _EXPECTED_GPU_MODEL,
        "hardware_attestation": False,
        "hardware_observation": _HARDWARE_OBSERVATION,
        "profile_id": QWEN3_8B_PROFILE_ID,
        "profile_sha256": qwen3_profile_sha256(),
        "publication_state": "OBSERVATION_ONLY_PENDING_REVIEW",
        "repository_commit": repository_commit,
        "run": run,
        "schema_version": _CAPTURE_SCHEMA,
        "source_archive_sha256": source_archive_sha256,
        "support": support,
    }


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    content = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise Qwen3CaptureError(
            "Qwen3 capture manifest could not be published"
        ) from None


def write_capture_manifest(capture_root: Path, repository_commit: str) -> Path:
    """Publish one immutable manifest after a locally reverified Qwen3 run."""

    _require_commit(repository_commit, label="repository commit")
    try:
        real_gpu_capture._validate_capture_tree(capture_root)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)
    path = capture_root / "qwen3-capability-capture.json"
    if path.exists() or path.is_symlink():
        raise Qwen3CaptureError("Qwen3 capture manifest already exists")
    value = _manifest_value(capture_root, repository_commit)
    _write_json_exclusive(path, value)
    verified = verify_capture(
        capture_root,
        expected_repository_commit=repository_commit,
    )
    if verified["run"] != value["run"]:
        raise Qwen3CaptureError("published Qwen3 capture manifest changed")
    return path


def verify_capture(
    capture_root: Path,
    *,
    expected_repository_commit: str | None = None,
) -> dict[str, Any]:
    """Recalculate the sealed bundle and verify all Qwen3 spike bindings."""

    try:
        real_gpu_capture._validate_capture_tree(capture_root)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)
    manifest_path = capture_root / "qwen3-capability-capture.json"
    manifest = _read_json(manifest_path, label="Qwen3 capture manifest")
    _require_exact_fields(
        manifest,
        {
            "acceptance_verdict",
            "campaign_id",
            "capture_kind",
            "captured_at",
            "expected_gpu_model",
            "hardware_attestation",
            "hardware_observation",
            "profile_id",
            "profile_sha256",
            "publication_state",
            "repository_commit",
            "run",
            "schema_version",
            "source_archive_sha256",
            "support",
        },
        label="Qwen3 capture manifest",
    )
    static = {
        "acceptance_verdict": None,
        "campaign_id": CANONICAL_CAMPAIGN_ID,
        "capture_kind": "BOUNDED_RUNTIME_CAPABILITY_SPIKE",
        "expected_gpu_model": _EXPECTED_GPU_MODEL,
        "hardware_attestation": False,
        "hardware_observation": _HARDWARE_OBSERVATION,
        "profile_id": QWEN3_8B_PROFILE_ID,
        "profile_sha256": qwen3_profile_sha256(),
        "publication_state": "OBSERVATION_ONLY_PENDING_REVIEW",
        "schema_version": _CAPTURE_SCHEMA,
    }
    for key, expected in static.items():
        if manifest.get(key) != expected:
            raise Qwen3CaptureError(f"Qwen3 capture field drifted: {key}")
    _require_timestamp(
        _required_text(manifest, "captured_at", label="Qwen3 capture manifest"),
        label="Qwen3 capture time",
    )
    repository_commit = _require_commit(
        _required_text(
            manifest,
            "repository_commit",
            label="Qwen3 capture manifest",
        ),
        label="Qwen3 capture repository commit",
    )
    if (
        expected_repository_commit is not None
        and repository_commit != expected_repository_commit
    ):
        raise Qwen3CaptureError("Qwen3 capture is not from the expected commit")
    source_archive_sha256 = _verify_support(
        capture_root,
        manifest.get("support"),
        repository_commit,
    )
    if manifest.get("source_archive_sha256") != source_archive_sha256:
        raise Qwen3CaptureError("Qwen3 capture source archive binding drifted")
    run = _run_record(capture_root)
    if manifest.get("run") != run:
        raise Qwen3CaptureError("Qwen3 capture run binding drifted")
    return {
        "capture_manifest_sha256": _sha256_file(
            manifest_path,
            label="Qwen3 capture manifest",
        ),
        "profile_id": QWEN3_8B_PROFILE_ID,
        "repository_commit": repository_commit,
        "run": run,
        "source_archive_sha256": source_archive_sha256,
        "valid": True,
    }


def verify_capture_archive(
    archive: Path,
    *,
    expected_archive_sha256: str,
    expected_repository_commit: str | None = None,
) -> dict[str, Any]:
    """Verify archive bytes, safe extraction, and the full semantic run offline."""

    try:
        actual = real_gpu_capture.archive_sha256(archive)
    except real_gpu_capture.CaptureError as error:
        _raise_capture(error)
    if actual != expected_archive_sha256:
        raise Qwen3CaptureError("Qwen3 capture archive failed SHA-256 verification")
    with tempfile.TemporaryDirectory(
        prefix="inferdrome-qwen3-capture-verification-"
    ) as temporary:
        try:
            root = real_gpu_capture.extract_capture_archive(
                archive,
                Path(temporary),
            )
        except real_gpu_capture.CaptureError as error:
            _raise_capture(error)
        verification = verify_capture(
            root,
            expected_repository_commit=expected_repository_commit,
        )
    return {
        "archive_sha256": actual,
        "capture_manifest_sha256": verification["capture_manifest_sha256"],
        "verification": verification,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify one Qwen3 A10 capability-spike capture"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write")
    write.add_argument("--capture-root", required=True)
    write.add_argument("--repository-commit", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("capture_root")
    verify.add_argument("--expected-commit")
    verify_archive = commands.add_parser("verify-archive")
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
            print(path)
        elif args.command == "verify":
            print(
                json.dumps(
                    verify_capture(
                        Path(args.capture_root),
                        expected_repository_commit=args.expected_commit,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "verify-archive":
            print(
                json.dumps(
                    verify_capture_archive(
                        Path(args.archive),
                        expected_archive_sha256=args.expected_sha256,
                        expected_repository_commit=args.expected_commit,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            raise AssertionError
    except Qwen3CaptureError as error:
        print(f"qwen3-gpu-capture: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
