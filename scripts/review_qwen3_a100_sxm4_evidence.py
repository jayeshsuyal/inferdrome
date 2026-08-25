#!/usr/bin/env python3
"""Review and pin the exact Qwen3-8B A100 SXM4 capability capture.

The capture archive and controller receipts are intentionally external-only.
This script commits only bounded, privacy-safe metadata and exact digest
anchors.  It always verifies archive bytes and the semantic bundle in an
isolated temporary directory before rendering those records.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from inferdrome.capability_profiles import canonical_document_sha256

try:
    import scripts.qwen3_gpu_capture as qwen3_capture
    import scripts.real_gpu_capture as capture
    from scripts.review_gpu_evidence_publication import (
        PublicationReviewError,
        _finding,
        _scan_capture,
        _sha256_bytes,
        _strict_json,
        publication_status,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repo root.
    import qwen3_gpu_capture as qwen3_capture  # type: ignore[no-redef]
    import real_gpu_capture as capture  # type: ignore[no-redef]
    from review_gpu_evidence_publication import (  # type: ignore[no-redef]
        PublicationReviewError,
        _finding,
        _scan_capture,
        _sha256_bytes,
        _strict_json,
        publication_status,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_RECORD = REPOSITORY_ROOT / (
    "gpu-proof-retrieved/20260823T192609Z-a02bfd7c3f8b-dcb7a227"
)
DEFAULT_ARCHIVE = DEFAULT_CAPTURE_RECORD / "capture.tar.gz"
DEFAULT_OUTPUT_DIRECTORY = (
    REPOSITORY_ROOT / "evidence" / "gpu" / "2026-08-23-qwen3-8b-a100-sxm4"
)

CAPTURE_PRODUCER_COMMIT = "a02bfd7c3f8bd0f734da0e84d476bcfa905fec4b"
CAPABILITY_PROFILE_COMMIT = "6cb774d210940073347f9045bb15611aa9e9cf27"
PUBLICATION_REVIEW_COMMIT: str | None = None
PUBLICATION_REVIEW_SHA256: str | None = (
    "sha256:f2616b08b526cb7346457dec585a98b5918e2c246b03faab9335a8a358e74208"
)
OPERATIONAL_SUMMARY_SHA256: str | None = (
    "sha256:167835a2e3c353f5ec06549cc06a5c049566a5d5201a5fae6e6567dea763488e"
)
HANDOFF_MANIFEST_SHA256: str | None = (
    "sha256:4fa7f17aee2bed78863ebd6171eb3144a55384ee78aa9e2caca12d6c18d7a59b"
)

ARCHIVE_SIZE_BYTES = 322_240
ARCHIVE_SHA256 = (
    "sha256:92e9456b33d2b8fe6b4df24ce6a487ea1fde09ddbc20b5acdfecdf19abd5efdc"
)
CAPTURE_MANIFEST_SHA256 = (
    "sha256:3d80c59117c154cdc8157a9898f14cae3b9e17b968650fc8693cf45c8436c0f6"
)
SEMANTIC_RECEIPT_SHA256 = (
    "sha256:b71382f52a9ca5593c3bd61ee06169ef2692cc40e5f67661f0c9b624fd856c1c"
)
TERMINATION_RECEIPT_SHA256 = (
    "sha256:bd9009cb7aca2d754f78b71f06112d3144f19d74cf1b179d798a5ab08e3ac5f1"
)
RETRIEVAL_RECEIPT_SHA256 = (
    "sha256:000ccb6dc98c1e0bcdaeb2cdfd5d17058a91caf5c94eb1e231fb8c130d6b133f"
)

RUN_ID = "run-9a01c9d8b4044e56eb68b2cf0345f5e0"
BUNDLE_DIGEST = (
    "sha256:6fcfa686c106a0fa1de2cf6c338d8de36cb8778d7880bb3e8701043ce5aa353a"
)
BUNDLE_MEMBER_PATH = f"capture/runs/{RUN_ID}/bundle"
MODEL_ID = "Qwen/Qwen3-8B"
MODEL_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
MODEL_SNAPSHOT_SHA256 = (
    "sha256:588d19e9e489cccdad793718d8c5efbad0738be717369f9eacb94ce514992d2c"
)
MODEL_SNAPSHOT_BYTES = 16_397_461_266
WORKLOAD_SHA256 = (
    "sha256:72db7f3a4e8e70c9fb721fe5544d1d96aac37ec6baedbce99e05ad423fdb105f"
)
REQUEST_PLAN_DIGEST = (
    "sha256:e272bf9c8d82bf3fd0eddd74c5b2b74edd21fa9358165e7d40aa3246f25b4498"
)
METRIC_DEFINITIONS_DIGEST = (
    "sha256:e237ff8613c6eec52a6053b3f6b47563ffc758c957ee298e57fe3c482a389131"
)
EXECUTION_FINGERPRINT = (
    "sha256:256f096f93a14260857d4caba59165459335af9c9c3b57d56eaa9bd369e405c3"
)
SOURCE_SPEC_DIGEST = (
    "sha256:1218bbafcded589a5b6fff13f533df8bd4e2a8e176a3995833967cc85334fd3f"
)
PROFILE_ID = "managed-vllm-0.26-qwen3-8b-bf16-v1"
PROFILE_SHA256 = (
    "sha256:858382b5ea2e86253f55ed914d11e4ab7e8b13aa6331e8699fb4d364a9ee9369"
)
WORKLOAD_PATH = REPOSITORY_ROOT / (
    "campaigns/v1/workloads/qwen-text-mixed-length-v1.jsonl"
)

EXPECTED_REQUEST_COUNT = 96
EXPECTED_TTFT_P50_NS = 42_974_685
EXPECTED_TTFT_P95_NS = 79_279_716
EXPECTED_TTFT_P99_NS = 80_570_049
EXPECTED_OUTPUT_THROUGHPUT = "73.377319"

PUBLICATION_REVIEW_PATH = (
    "evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4/publication-review.json"
)
OPERATIONAL_SUMMARY_PATH = (
    "evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4/operational-summary.json"
)
PROPOSED_FIXTURE_LOCATION = (
    "https://github.com/jayeshsuyal/inferdrome/releases/download/"
    "gpu-evidence-2026-08-23/"
    "inferdrome-qwen3-8b-a100-sxm4-92e9456b.tar.gz"
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class A100EvidenceError(RuntimeError):
    """The exact A100 capture could not be safely reviewed."""


def _safe_record_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise A100EvidenceError(f"{label} is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not content
        or len(content) > 65_536
    ):
        raise A100EvidenceError(f"{label} is unsafe")
    return content


def _strict_record(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    content = _safe_record_bytes(path, label=label)
    return content, _strict_json(path)


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise A100EvidenceError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise A100EvidenceError(f"{label} is invalid") from None
    if parsed.tzinfo != UTC:
        raise A100EvidenceError(f"{label} is invalid")
    return parsed


def _elapsed_seconds(started: datetime, ended: datetime) -> Decimal:
    delta = ended - started
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    if microseconds < 0:
        raise A100EvidenceError("provider termination predates billing start")
    return Decimal(microseconds) / Decimal(1_000_000)


def build_operational_summary(capture_record: Path) -> dict[str, Any]:
    """Derive a privacy-safe summary from exact local controller receipts."""

    try:
        metadata = os.lstat(capture_record)
    except OSError:
        raise A100EvidenceError("A100 capture record is unavailable") from None
    if capture_record.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise A100EvidenceError("A100 capture record is unsafe")

    semantic_bytes, semantic = _strict_record(
        capture_record / "semantic-verification.json",
        label="semantic-verification receipt",
    )
    termination_bytes, termination = _strict_record(
        capture_record / "lambda-termination-receipt.json",
        label="Lambda termination receipt",
    )
    retrieval_bytes, retrieval = _strict_record(
        capture_record / "retrieval-receipt.json",
        label="retrieval receipt",
    )
    if (
        _sha256_bytes(semantic_bytes) != SEMANTIC_RECEIPT_SHA256
        or _sha256_bytes(termination_bytes) != TERMINATION_RECEIPT_SHA256
        or _sha256_bytes(retrieval_bytes) != RETRIEVAL_RECEIPT_SHA256
    ):
        raise A100EvidenceError("A100 controller receipt bytes drifted")

    semantic_run = semantic.get("run")
    gpu_target = semantic.get("gpu_target")
    provider = semantic.get("provider_instance")
    provider_termination = semantic.get("provider_termination")
    cost_window = termination.get("cost_window")
    termination_value = termination.get("termination")
    if (
        semantic.get("schema_version")
        != "inferdrome.qwen3-offline-verification.v2"
        or semantic.get("semantic_verification")
        != "VALID_AFTER_PROVIDER_TERMINATION"
        or semantic.get("archive_sha256") != ARCHIVE_SHA256
        or semantic.get("capture_manifest_sha256") != CAPTURE_MANIFEST_SHA256
        or semantic.get("managed_capability_profile") != PROFILE_ID
        or semantic.get("repository_commit") != CAPTURE_PRODUCER_COMMIT
        or semantic.get("termination_receipt_sha256")
        != TERMINATION_RECEIPT_SHA256
        or semantic.get("retrieval_receipt_sha256") != RETRIEVAL_RECEIPT_SHA256
        or not isinstance(semantic_run, dict)
        or semantic_run.get("run_id") != RUN_ID
        or semantic_run.get("bundle_digest") != BUNDLE_DIGEST
        or semantic_run.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or semantic_run.get("successful_requests") != EXPECTED_REQUEST_COUNT
        or semantic_run.get("failed_requests") != 0
        or semantic_run.get("ttft_samples") != EXPECTED_REQUEST_COUNT
        or not isinstance(gpu_target, dict)
        or gpu_target.get("gpu_tier_id") != "a100-40gb-sxm4"
        or gpu_target.get("expected_nvidia_smi_name")
        != "NVIDIA A100-SXM4-40GB"
        or not isinstance(provider, dict)
        or provider.get("instance_type_name") != "gpu_1x_a100_sxm4"
        or provider.get("hourly_rate_usd") != "1.99"
        or not isinstance(provider_termination, dict)
        or provider_termination.get("final_status") != "absent"
        or provider_termination.get("request_sent") is not True
        or termination.get("record_kind")
        != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or termination.get("schema_version")
        != "inferdrome.lambda-termination-receipt.v2"
        or not isinstance(cost_window, dict)
        or cost_window.get("hourly_rate_usd") != "1.99"
        or cost_window.get("max_cost_usd") != "1.25"
        or not isinstance(termination_value, dict)
        or termination_value != provider_termination
        or retrieval.get("archive_sha256") != ARCHIVE_SHA256
        or retrieval.get("repository_commit") != CAPTURE_PRODUCER_COMMIT
        or retrieval.get("managed_capability_profile") != PROFILE_ID
        or retrieval.get("gpu_tier_id") != "a100-40gb-sxm4"
        or retrieval.get("lambda_instance_type_name") != "gpu_1x_a100_sxm4"
    ):
        raise A100EvidenceError("A100 controller receipts disagree")

    started = _timestamp(
        cost_window.get("billing_started_at"),
        label="provider billing start",
    )
    confirmed = _timestamp(
        provider_termination.get("confirmed_at"),
        label="provider termination confirmation",
    )
    elapsed = _elapsed_seconds(started, confirmed)
    cost = (elapsed * Decimal("1.99") / Decimal(3_600)).quantize(
        Decimal("0.000001"),
        rounding=ROUND_HALF_EVEN,
    )
    return {
        "archive_sha256": ARCHIVE_SHA256,
        "bundle_digest": BUNDLE_DIGEST,
        "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
        "cost_observation": {
            "billing_window_seconds": format(elapsed, "f"),
            "estimated_cost_usd": format(cost, "f"),
            "hourly_rate_usd": "1.99",
            "max_cost_usd": "1.25",
            "statement": (
                "Controller-observed estimate only; provider billing granularity "
                "and the final invoice remain external."
            ),
        },
        "evidence_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
        "provider": {
            "gpu_tier_id": "a100-40gb-sxm4",
            "instance_type_name": "gpu_1x_a100_sxm4",
            "termination_confirmed_at": provider_termination["confirmed_at"],
            "termination_final_status": "absent",
            "termination_trigger": termination["trigger"],
        },
        "repository_commit": CAPTURE_PRODUCER_COMMIT,
        "run_id": RUN_ID,
        "schema_version": "inferdrome.qwen3-gpu-operational-summary.v1",
        "semantic_verification": "VALID_AFTER_PROVIDER_TERMINATION",
        "source_receipts": {
            "retrieval_receipt_sha256": RETRIEVAL_RECEIPT_SHA256,
            "semantic_verification_sha256": SEMANTIC_RECEIPT_SHA256,
            "termination_receipt_sha256": TERMINATION_RECEIPT_SHA256,
        },
    }


def build_publication_review(
    archive: Path,
    capture_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Scan the exact archive and classify public-delivery eligibility."""

    scan = _scan_capture(capture_root)
    detectors = scan["detectors"]
    rejected = bool(
        detectors["secret_shaped_values"]["matches"]
        or detectors["personal_or_customer_data"]["status"] != "NO_DETECTOR_MATCHES"
        or detectors["binary_or_non_utf8"]["path_count"]
        or detectors["unreviewed_oversized_files"]["path_count"]
    )
    repository_has_license = any(
        path.is_file()
        for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")
        for path in REPOSITORY_ROOT.glob(pattern)
    )
    unresolved = [
        "GENERATED_OUTPUT_LICENSE_REVIEW_REQUIRED",
        "MODEL_LICENSE_RECORD_NOT_RETAINED",
        "REPOSITORY_LICENSE_UNSELECTED",
        "VLLM_LICENSE_RECORD_NOT_RETAINED",
        "WORKLOAD_PUBLICATION_LICENSE_UNRESOLVED",
    ]
    if repository_has_license:
        unresolved.remove("REPOSITORY_LICENSE_UNSELECTED")
    status = publication_status(
        rejected_findings=rejected,
        unresolved_publication_findings=bool(unresolved),
        owner_publication_approved=False,
    )
    findings = [
        _finding(
            "ABSOLUTE_PATHS_PRESENT",
            "DISCLOSE",
            "Host-local absolute paths are evidence and were not rewritten.",
            path_count=detectors["absolute_paths"]["path_count"],
            path_examples=detectors["absolute_paths"]["path_examples"],
        ),
        _finding(
            "GENERATED_RESPONSES_PRESENT",
            "SENSITIVE_CONTENT",
            "Untouched native vLLM results contain generated response text.",
            path_count=scan["content"]["generated_responses"]["artifact_file_count"],
        ),
        _finding(
            "GPU_UUIDS_PRESENT",
            "DISCLOSE",
            "GPU UUIDs are required provenance and were not rewritten.",
            path_count=detectors["gpu_uuids"]["path_count"],
            path_examples=detectors["gpu_uuids"]["path_examples"],
        ),
        _finding(
            "PROCESS_IDENTIFIERS_PRESENT",
            "DISCLOSE",
            "Ephemeral host process identifiers are retained as execution evidence.",
            path_count=detectors["process_identifiers"]["path_count"],
            path_examples=detectors["process_identifiers"]["path_examples"],
        ),
        _finding(
            "PRIVATE_NETWORK_ADDRESS_PRESENT",
            "DISCLOSE",
            "Retained server diagnostics contain a private host-network address.",
            path_count=detectors["private_network_addresses"]["path_count"],
            path_examples=detectors["private_network_addresses"]["path_examples"],
        ),
        _finding(
            "PROMPTS_PRESENT",
            "SENSITIVE_CONTENT",
            "Source workload files contain the exact measured prompt text.",
            path_count=scan["content"]["prompts"]["artifact_file_count"],
        ),
    ]
    findings.extend(
        _finding(
            code,
            "BLOCKS_PUBLICATION",
            "No owner-approved, archive-bound license decision resolves this scope.",
        )
        for code in unresolved
    )
    if rejected:
        findings.append(
            _finding(
                "CONTENT_REVIEW_REJECTED",
                "REJECT",
                "A secret, personal-data, binary, or size detector blocked "
                "complete review.",
            )
        )
    findings.sort(key=lambda item: str(item["code"]))
    try:
        archive_metadata = os.lstat(archive)
    except OSError:
        raise A100EvidenceError("A100 archive is unavailable") from None
    if archive_metadata.st_size != ARCHIVE_SIZE_BYTES:
        raise A100EvidenceError("A100 archive size drifted")
    return {
        "archive": {
            "compressed_size_bytes": archive_metadata.st_size,
            "sha256": ARCHIVE_SHA256,
        },
        "archive_integrity_and_safety": {
            "archive_member_rules": [
                "top-level capture directory required",
                "absolute and traversal paths rejected",
                "duplicate members rejected",
                "links, devices, and special files rejected",
                "member, directory, file, expanded-byte, and compressed-byte limits",
            ],
            "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
            "isolated_verification": verification["verification"]["valid"],
            "status": "PASS",
            **scan["archive_tree"],
        },
        "content_review": scan["content"],
        "decision_reasons": sorted(
            [*unresolved, "OWNER_PUBLICATION_APPROVAL_REQUIRED"]
            + (["CONTENT_REVIEW_REJECTED"] if rejected else [])
        ),
        "detector_results": detectors,
        "findings": findings,
        "license_review": {
            **scan["license_artifacts"],
            "generated_output_license_resolved": False,
            "model_license_resolved": False,
            "repository_license_present": repository_has_license,
            "vllm_license_resolved": False,
            "workload_publication_license_resolved": False,
        },
        "owner_publication_approval_required": True,
        "publication_status": status,
        "raw_archive_modified": False,
        "review_limits": scan["review_limits"],
        "review_method": (
            "isolated full-archive verification plus bounded full-member UTF-8, "
            "structured-content, secret-shape, personal-data, identity, path, and "
            "license-presence scan"
        ),
        "schema_version": "inferdrome.gpu-evidence-publication-review.v1",
        "scope": "exact_archive_bytes",
    }


def _bundle(capture_root: Path) -> Path:
    bundle = capture_root.joinpath(*BUNDLE_MEMBER_PATH.split("/")[1:])
    try:
        bundle.resolve(strict=True).relative_to(capture_root.resolve(strict=True))
    except (OSError, ValueError):
        raise A100EvidenceError("A100 bundle escapes the capture root") from None
    if not bundle.is_dir() or bundle.is_symlink():
        raise A100EvidenceError("A100 bundle is unavailable")
    return bundle


def _measurement(
    measurements: dict[str, Any],
    *,
    metric: str,
    aggregation: str,
    expected: object,
) -> object:
    matches = [
        value
        for value in measurements.get("measurements", [])
        if isinstance(value, dict)
        and value.get("metric") == metric
        and value.get("aggregation") == aggregation
    ]
    if len(matches) != 1 or matches[0].get("value") != expected:
        raise A100EvidenceError(f"stored measurement drifted: {metric}/{aggregation}")
    return expected


def _build_run_handoff(
    capture_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    bundle = _bundle(capture_root)
    descriptor = _strict_json(bundle / "bundle.json")
    environment = _strict_json(bundle / "environment.json")
    measurements = _strict_json(bundle / "derived" / "measurements.json")
    if (
        descriptor.get("run_id") != RUN_ID
        or descriptor.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or descriptor.get("integrity_status") != "VALID"
        or descriptor.get("environment_completeness") != "COMPLETE"
        or descriptor.get("producer", {}).get("version") != "0.26.0"
        or descriptor.get("digests", {}).get("execution_fingerprint")
        != EXECUTION_FINGERPRINT
        or descriptor.get("digests", {}).get("request_plan_digest")
        != REQUEST_PLAN_DIGEST
        or descriptor.get("digests", {}).get("metric_definitions_digest")
        != METRIC_DEFINITIONS_DIGEST
        or descriptor.get("digests", {}).get("source_spec_digest")
        != SOURCE_SPEC_DIGEST
        or measurements.get("run_id") != RUN_ID
        or measurements.get("metric_definitions_digest") != METRIC_DEFINITIONS_DIGEST
        or environment.get("run_id") != RUN_ID
    ):
        raise A100EvidenceError("A100 bundle identity drifted")
    _measurement(
        measurements,
        metric="measured_request_count",
        aggregation="count",
        expected=EXPECTED_REQUEST_COUNT,
    )
    _measurement(
        measurements,
        metric="successful_request_count",
        aggregation="count",
        expected=EXPECTED_REQUEST_COUNT,
    )
    _measurement(
        measurements,
        metric="failed_request_count",
        aggregation="count",
        expected=0,
    )
    ttft = {
        "definition_id": "vllm_first_choices_event_v0_26",
        "p50": _measurement(
            measurements,
            metric="ttft_ns",
            aggregation="p50",
            expected=EXPECTED_TTFT_P50_NS,
        ),
        "p95": _measurement(
            measurements,
            metric="ttft_ns",
            aggregation="p95",
            expected=EXPECTED_TTFT_P95_NS,
        ),
        "p99": _measurement(
            measurements,
            metric="ttft_ns",
            aggregation="p99",
            expected=EXPECTED_TTFT_P99_NS,
        ),
        "population": "successful_measured_requests_with_observed_ttft",
        "quantile_method": "nearest_rank_v1",
    }
    output_throughput = _measurement(
        measurements,
        metric="output_token_throughput_per_s",
        aggregation="rate",
        expected=EXPECTED_OUTPUT_THROUGHPUT,
    )
    if verification["run"]["run_id"] != RUN_ID:
        raise A100EvidenceError("A100 verification run identity drifted")
    return {
        "bundle_digest": BUNDLE_DIGEST,
        "execution_fingerprint": EXECUTION_FINGERPRINT,
        "metric_definitions_digest": METRIC_DEFINITIONS_DIGEST,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "request_plan_digest": REQUEST_PLAN_DIGEST,
        "request_population": {
            "failed_requests": 0,
            "measured_requests": EXPECTED_REQUEST_COUNT,
            "successful_requests": EXPECTED_REQUEST_COUNT,
            "ttft_samples": EXPECTED_REQUEST_COUNT,
        },
        "run_id": RUN_ID,
        "source_spec_digest": SOURCE_SPEC_DIGEST,
        "summary_measurements": {
            "output_token_throughput_per_s": output_throughput,
            "ttft_ns": ttft,
        },
        "workload_sha256": WORKLOAD_SHA256,
    }


def build_handoff_manifest(
    capture_root: Path,
    verification: dict[str, Any],
    publication_review: dict[str, Any],
    operational_summary: dict[str, Any],
) -> dict[str, Any]:
    """Pin A100 identity and claims without copying sensitive evidence."""

    return {
        "acceptance_boundary": {
            "capture_kind": "BOUNDED_RUNTIME_CAPABILITY_SPIKE",
            "inferdrome_acceptance_verdict": None,
            "publication_state": "OBSERVATION_ONLY_PENDING_REVIEW",
            "statement": (
                "This capture proves one bounded runtime observation. It does not "
                "assign PASS, FAIL, or NOT_PROVEN and is not a cross-GPU result."
            ),
        },
        "archive": {
            "bundle_member_path": BUNDLE_MEMBER_PATH,
            "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
            "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
            "sha256": ARCHIVE_SHA256,
        },
        "capability_profile": {
            "campaign_id": "qwen-gpu-capability-campaign-v1",
            "commit": CAPABILITY_PROFILE_COMMIT,
            "managed_profile": {
                "identity": PROFILE_ID,
                "path": "campaigns/v1/profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json",
                "sha256": PROFILE_SHA256,
            },
            "model_snapshot": {
                "file_count": 15,
                "revision": MODEL_REVISION,
                "sha256": MODEL_SNAPSHOT_SHA256,
                "total_bytes": MODEL_SNAPSHOT_BYTES,
            },
            "workload": {
                "id": "inferdrome.qwen-text-mixed-length.v1",
                "path": WORKLOAD_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
                "prompt_count": EXPECTED_REQUEST_COUNT,
                "sha256": WORKLOAD_SHA256,
            },
        },
        "contract_binding": {
            "chronology": "RETROSPECTIVE",
            "chronology_disclosure": (
                "No producer-side ExitSpec contract digest was frozen before this "
                "measurement. A later consumer must use an explicit external receipt "
                "binding without rewriting chronology."
            ),
            "producer_exitspec_contract_digest": None,
            "required_consumer_mode": "EXTERNAL_RECEIPT_BINDING",
        },
        "fixture_delivery": {
            "proposed_checksum_pinned_location": PROPOSED_FIXTURE_LOCATION,
            "publication_state": "BLOCKED_PENDING_OWNER_APPROVAL",
            "required_sha256": ARCHIVE_SHA256,
            "statement": (
                "The raw archive remains ignored. Public CI can validate committed "
                "anchors, but byte-level archive reverification requires these exact "
                "reviewed bytes until owner license and publication approval exists."
            ),
        },
        "history_provenance": {
            "capability_profile_commit": CAPABILITY_PROFILE_COMMIT,
            "capture_producer_commit": CAPTURE_PRODUCER_COMMIT,
            "eventual_merge_commit": None,
            "merge_requirement": (
                "Use a merge commit that preserves capture_producer_commit ancestry; "
                "do not squash or rebase away the producer commit."
            ),
            "publication_review_commit": PUBLICATION_REVIEW_COMMIT,
        },
        "operational_completion": {
            "path": OPERATIONAL_SUMMARY_PATH,
            "semantic_verification": operational_summary["semantic_verification"],
            "sha256": canonical_document_sha256(operational_summary),
            "termination_final_status": operational_summary["provider"][
                "termination_final_status"
            ],
        },
        "publication_review": {
            "owner_publication_approval_required": publication_review[
                "owner_publication_approval_required"
            ],
            "path": PUBLICATION_REVIEW_PATH,
            "publication_status": publication_review["publication_status"],
            "sha256": canonical_document_sha256(publication_review),
        },
        "run": _build_run_handoff(capture_root, verification),
        "runtime_capability": {
            "expected_gpu_model": "NVIDIA A100-SXM4-40GB",
            "gpu_tier_id": "a100-40gb-sxm4",
            "hardware_attestation": False,
            "hardware_observation": (
                "SELECTED_GPU_REPORTED_NVIDIA_A100-SXM4-40GB_SINGLE_CUDA_DEVICE"
            ),
            "profile_id": PROFILE_ID,
            "spike_outcome": "SPIKE_SUCCEEDED",
            "torch_cuda_device_count": 1,
        },
        "schema_version": "inferdrome.qwen3-gpu-evidence-handoff.v1",
    }


def _expected_fields(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def validate_publication_review(value: Any) -> bool:
    fields = {
        "archive",
        "archive_integrity_and_safety",
        "content_review",
        "decision_reasons",
        "detector_results",
        "findings",
        "license_review",
        "owner_publication_approval_required",
        "publication_status",
        "raw_archive_modified",
        "review_limits",
        "review_method",
        "schema_version",
        "scope",
    }
    if not _expected_fields(value, fields):
        return False
    try:
        return bool(
            (
                PUBLICATION_REVIEW_SHA256 is None
                or canonical_document_sha256(value) == PUBLICATION_REVIEW_SHA256
            )
            and value["schema_version"]
            == "inferdrome.gpu-evidence-publication-review.v1"
            and value["scope"] == "exact_archive_bytes"
            and value["archive"]
            == {
                "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
                "sha256": ARCHIVE_SHA256,
            }
            and value["archive_integrity_and_safety"]["status"] == "PASS"
            and value["archive_integrity_and_safety"]["capture_manifest_sha256"]
            == CAPTURE_MANIFEST_SHA256
            and value["publication_status"] == "EXTERNAL_ONLY"
            and value["owner_publication_approval_required"] is True
            and value["raw_archive_modified"] is False
            and value["detector_results"]["secret_shaped_values"]["status"]
            == "NO_DETECTOR_MATCHES"
            and value["detector_results"]["personal_or_customer_data"]["status"]
            == "NO_DETECTOR_MATCHES"
        )
    except (KeyError, TypeError):
        return False


def validate_operational_summary(value: Any) -> bool:
    fields = {
        "archive_sha256",
        "bundle_digest",
        "capture_manifest_sha256",
        "cost_observation",
        "evidence_kind",
        "provider",
        "repository_commit",
        "run_id",
        "schema_version",
        "semantic_verification",
        "source_receipts",
    }
    if not _expected_fields(value, fields):
        return False
    try:
        return bool(
            (
                OPERATIONAL_SUMMARY_SHA256 is None
                or canonical_document_sha256(value) == OPERATIONAL_SUMMARY_SHA256
            )
            and value["schema_version"]
            == "inferdrome.qwen3-gpu-operational-summary.v1"
            and value["archive_sha256"] == ARCHIVE_SHA256
            and value["capture_manifest_sha256"] == CAPTURE_MANIFEST_SHA256
            and value["bundle_digest"] == BUNDLE_DIGEST
            and value["repository_commit"] == CAPTURE_PRODUCER_COMMIT
            and value["run_id"] == RUN_ID
            and value["evidence_kind"]
            == "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
            and value["semantic_verification"]
            == "VALID_AFTER_PROVIDER_TERMINATION"
            and value["provider"]["gpu_tier_id"] == "a100-40gb-sxm4"
            and value["provider"]["instance_type_name"] == "gpu_1x_a100_sxm4"
            and value["provider"]["termination_final_status"] == "absent"
            and value["cost_observation"]["estimated_cost_usd"] == "0.432165"
            and value["source_receipts"]["semantic_verification_sha256"]
            == SEMANTIC_RECEIPT_SHA256
            and value["source_receipts"]["termination_receipt_sha256"]
            == TERMINATION_RECEIPT_SHA256
            and value["source_receipts"]["retrieval_receipt_sha256"]
            == RETRIEVAL_RECEIPT_SHA256
        )
    except (KeyError, TypeError):
        return False


def validate_handoff_manifest(value: Any) -> bool:
    fields = {
        "acceptance_boundary",
        "archive",
        "capability_profile",
        "contract_binding",
        "fixture_delivery",
        "history_provenance",
        "operational_completion",
        "publication_review",
        "run",
        "runtime_capability",
        "schema_version",
    }
    if not _expected_fields(value, fields):
        return False
    try:
        return bool(
            (
                HANDOFF_MANIFEST_SHA256 is None
                or canonical_document_sha256(value) == HANDOFF_MANIFEST_SHA256
            )
            and value["schema_version"]
            == "inferdrome.qwen3-gpu-evidence-handoff.v1"
            and value["archive"]
            == {
                "bundle_member_path": BUNDLE_MEMBER_PATH,
                "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
                "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
                "sha256": ARCHIVE_SHA256,
            }
            and value["run"]["run_id"] == RUN_ID
            and value["run"]["bundle_digest"] == BUNDLE_DIGEST
            and value["run"]["request_population"]
            == {
                "failed_requests": 0,
                "measured_requests": EXPECTED_REQUEST_COUNT,
                "successful_requests": EXPECTED_REQUEST_COUNT,
                "ttft_samples": EXPECTED_REQUEST_COUNT,
            }
            and value["run"]["summary_measurements"]["ttft_ns"]["p95"]
            == EXPECTED_TTFT_P95_NS
            and value["runtime_capability"]["hardware_attestation"] is False
            and value["runtime_capability"]["gpu_tier_id"] == "a100-40gb-sxm4"
            and value["publication_review"]["publication_status"]
            == "EXTERNAL_ONLY"
            and _DIGEST.fullmatch(value["publication_review"]["sha256"])
            and _DIGEST.fullmatch(value["operational_completion"]["sha256"])
            and value["history_provenance"]["capture_producer_commit"]
            == CAPTURE_PRODUCER_COMMIT
            and value["history_provenance"]["eventual_merge_commit"] is None
            and value["contract_binding"]["producer_exitspec_contract_digest"]
            is None
            and value["contract_binding"]["chronology"] == "RETROSPECTIVE"
            and value["acceptance_boundary"]["inferdrome_acceptance_verdict"]
            is None
            and value["fixture_delivery"]["publication_state"]
            == "BLOCKED_PENDING_OWNER_APPROVAL"
        )
    except (KeyError, TypeError):
        return False


def _render(value: dict[str, Any]) -> bytes:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return content.encode("utf-8")


def render_outputs(archive: Path, capture_record: Path) -> dict[Path, bytes]:
    """Verify exact bytes and render the three committed outer records."""

    archive = archive.absolute()
    try:
        metadata = os.lstat(archive)
    except OSError:
        raise A100EvidenceError("A100 archive is unavailable") from None
    if (
        archive.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != ARCHIVE_SIZE_BYTES
    ):
        raise A100EvidenceError("A100 archive size or type drifted")
    verification = qwen3_capture.verify_capture_archive(
        archive,
        expected_archive_sha256=ARCHIVE_SHA256,
        expected_repository_commit=CAPTURE_PRODUCER_COMMIT,
        expected_gpu_tier_id="a100-40gb-sxm4",
    )
    operational_summary = build_operational_summary(capture_record.absolute())
    try:
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-a100-publication-review-"
        ) as root:
            temporary_root = Path(root)
            capture_root: Path | None = None
            try:
                capture_root = capture.extract_capture_archive(archive, temporary_root)
                semantic_verification = qwen3_capture.verify_capture(
                    capture_root,
                    expected_repository_commit=CAPTURE_PRODUCER_COMMIT,
                    expected_gpu_tier_id="a100-40gb-sxm4",
                )
                review = build_publication_review(
                    archive,
                    capture_root,
                    verification,
                )
                handoff = build_handoff_manifest(
                    capture_root,
                    semantic_verification,
                    review,
                    operational_summary,
                )
            finally:
                if capture_root is not None:
                    capture._make_directories_writable_for_cleanup(capture_root)
    except OSError:
        raise A100EvidenceError(
            "isolated A100 publication review directory is unavailable"
        ) from None
    if (
        not validate_publication_review(review)
        or not validate_operational_summary(operational_summary)
        or not validate_handoff_manifest(handoff)
    ):
        raise A100EvidenceError("rendered A100 publication records are invalid")
    return {
        DEFAULT_OUTPUT_DIRECTORY / "handoff-manifest.json": _render(handoff),
        DEFAULT_OUTPUT_DIRECTORY / "operational-summary.json": _render(
            operational_summary
        ),
        DEFAULT_OUTPUT_DIRECTORY / "publication-review.json": _render(review),
    }


def _load_committed(name: str) -> dict[str, Any]:
    return _strict_json(DEFAULT_OUTPUT_DIRECTORY / name)


def check_committed_records() -> int:
    """Validate committed anchors without pretending the archive is in CI."""

    try:
        review = _load_committed("publication-review.json")
        operational = _load_committed("operational-summary.json")
        handoff = _load_committed("handoff-manifest.json")
    except A100EvidenceError as error:
        print(f"A100 publication records: {error}")
        return 1
    if (
        not validate_publication_review(review)
        or not validate_operational_summary(operational)
        or not validate_handoff_manifest(handoff)
        or handoff["publication_review"]["sha256"]
        != canonical_document_sha256(review)
        or handoff["operational_completion"]["sha256"]
        != canonical_document_sha256(operational)
    ):
        print("A100 publication records are invalid or cross-digest drifted")
        return 1
    print("A100 publication records and cross-digests are valid")
    return 0


def _write_outputs(outputs: dict[Path, bytes]) -> int:
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print("A100 SXM4 publication review and handoff rendered")
    for path, content in sorted(outputs.items()):
        print(
            f"{path.relative_to(REPOSITORY_ROOT)} "
            f"{canonical_document_sha256(json.loads(content))}"
        )
    return 0


def _check_outputs(outputs: dict[Path, bytes]) -> int:
    mismatches = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path, expected in outputs.items()
        if not path.exists() or path.read_bytes() != expected
    ]
    if mismatches:
        print("A100 publication records are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print("A100 publication records match the exact reviewed archive")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review the exact Qwen3-8B A100 SXM4 archive without rewriting it"
    )
    parser.add_argument("archive", nargs="?", default=str(DEFAULT_ARCHIVE))
    parser.add_argument(
        "--capture-record",
        default=str(DEFAULT_CAPTURE_RECORD),
        help="retrieved record containing termination and semantic receipts",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check",
        action="store_true",
        help="reverify exact local bytes and require committed outputs to match",
    )
    modes.add_argument(
        "--check-records",
        action="store_true",
        help="validate committed metadata when the EXTERNAL_ONLY archive is absent",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.check_records:
        return check_committed_records()
    try:
        outputs = render_outputs(Path(args.archive), Path(args.capture_record))
    except (
        OSError,
        A100EvidenceError,
        PublicationReviewError,
        capture.CaptureError,
        qwen3_capture.Qwen3CaptureError,
    ) as error:
        print(f"A100 GPU publication review: {error}")
        return 1
    return _check_outputs(outputs) if args.check else _write_outputs(outputs)


if __name__ == "__main__":
    raise SystemExit(main())
