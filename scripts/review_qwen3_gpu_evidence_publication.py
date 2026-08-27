#!/usr/bin/env python3
"""Review and pin the exact Qwen3-8B A10 capability capture."""

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

from inferdrome.bundle import recalculate_bundle
from inferdrome.capability_profiles import canonical_document_sha256

try:
    import scripts.qwen3_gpu_capture as qwen3_capture
    import scripts.real_gpu_capture as capture
    from scripts.review_gpu_evidence_publication import (
        PublicationReviewError,
        _finding,
        _independent_ttft,
        _render,
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
        _independent_ttft,
        _render,
        _scan_capture,
        _sha256_bytes,
        _strict_json,
        publication_status,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_RECORD = REPOSITORY_ROOT / (
    "gpu-proof-retrieved/20260821T203940Z-058482df4737-68efd4f4"
)
DEFAULT_ARCHIVE = DEFAULT_CAPTURE_RECORD / "capture.tar.gz"
DEFAULT_OUTPUT_DIRECTORY = (
    REPOSITORY_ROOT / "evidence" / "gpu" / "2026-08-21-qwen3-8b-a10"
)

CAPTURE_PRODUCER_COMMIT = "058482df47377aaae6303015746f9a8e05d7e0f7"
CAPABILITY_PROFILE_COMMIT = "6cb774d210940073347f9045bb15611aa9e9cf27"
PUBLICATION_REVIEW_COMMIT: str | None = (
    "68854817da3ddd5a0b667f703960744cd6669e25"
)

ARCHIVE_SIZE_BYTES = 321_010
ARCHIVE_SHA256 = (
    "sha256:27cdcc0192c5d6f05b5350e380b53caa158c249de28ed6880fe3d7971032172f"
)
CAPTURE_MANIFEST_SHA256 = (
    "sha256:da24b867be15285ebd41a7187b3d96cfd5f18ffe373a0b7b883f2de5bd5b0869"
)
SEMANTIC_RECEIPT_SHA256 = (
    "sha256:d2c6dec2e62dc8cf22de3545b1385a771b32c1d366b2852e55b676b6f0f7ebf7"
)
TERMINATION_RECEIPT_SHA256 = (
    "sha256:7e63e680be7843d8bb823e52754290e62f58a35bff43bd8084bc222f00e77e0c"
)
RETRIEVAL_RECEIPT_SHA256 = (
    "sha256:4927a6bd448dbb89258647f5de1c9e74913c0f9ef334e4494d09d29903201e85"
)
PUBLICATION_REVIEW_SHA256 = (
    "sha256:1e0e1834777f6451ddeb2ecc299fe1aaf1d3a246536dbae3bc1d595ca06e477a"
)
OPERATIONAL_SUMMARY_SHA256 = (
    "sha256:e0a46bd9c6cb623a9f6203e177408a61fcf67acb01faf8167892b9f3cc0b1860"
)
HANDOFF_MANIFEST_SHA256: str | None = (
    "sha256:c14ce6985278575116ad4110b4bb466dad76b22cc8e3233ede85d1b2a1f06ad9"
)

RUN_ID = "run-fcbd9a0a4031826ea601c5f14637e8dc"
BUNDLE_DIGEST = (
    "sha256:48514166f6c284613052fedb0264ed83e2212156233d9f6103cb8946c0211ad6"
)
BUNDLE_MEMBER_PATH = f"capture/runs/{RUN_ID}/bundle"
RUN_MEMBER_PATH = f"capture/runs/{RUN_ID}"
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
    "sha256:e4f7002626bbbe23ecd4e3658f53fcba6a365a177c6d0ca3acb62955c0a5f7f7"
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
PROFILE_PATH = REPOSITORY_ROOT / (
    "campaigns/v1/profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json"
)
WORKLOAD_PATH = REPOSITORY_ROOT / (
    "campaigns/v1/workloads/qwen-text-mixed-length-v1.jsonl"
)

EXPECTED_REQUEST_COUNT = 96
EXPECTED_SUCCESS_COUNT = 96
EXPECTED_TTFT_P50_NS = 127_123_958
EXPECTED_TTFT_P95_NS = 242_426_174
EXPECTED_TTFT_P99_NS = 244_030_050
EXPECTED_OUTPUT_THROUGHPUT = "28.870215"
EXPECTED_E2E_P50_NS = 4_421_866_794
EXPECTED_E2E_P95_NS = 4_554_172_554

PROPOSED_FIXTURE_LOCATION = (
    "https://github.com/jayeshsuyal/inferdrome/releases/download/"
    "gpu-evidence-2026-08-21/"
    "inferdrome-qwen3-8b-a10-27cdcc01.tar.gz"
)
PUBLICATION_REVIEW_PATH = (
    "evidence/gpu/2026-08-21-qwen3-8b-a10/publication-review.json"
)
OPERATIONAL_SUMMARY_PATH = (
    "evidence/gpu/2026-08-21-qwen3-8b-a10/operational-summary.json"
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _safe_record_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise PublicationReviewError(f"{label} is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not content
        or len(content) > 65_536
    ):
        raise PublicationReviewError(f"{label} is unsafe")
    return content


def _strict_record(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    content = _safe_record_bytes(path, label=label)
    return content, _strict_json(path)


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PublicationReviewError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PublicationReviewError(f"{label} is invalid") from None
    if parsed.tzinfo != UTC:
        raise PublicationReviewError(f"{label} is invalid")
    return parsed


def _elapsed_seconds(started: datetime, ended: datetime) -> Decimal:
    delta = ended - started
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    if microseconds < 0:
        raise PublicationReviewError("provider termination predates billing start")
    return Decimal(microseconds) / Decimal(1_000_000)


def build_operational_summary(capture_record: Path) -> dict[str, Any]:
    """Derive a privacy-safe summary from exact local controller receipts."""

    try:
        metadata = os.lstat(capture_record)
    except OSError:
        raise PublicationReviewError("Qwen3 capture record is unavailable") from None
    if capture_record.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PublicationReviewError("Qwen3 capture record is unsafe")

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
        raise PublicationReviewError("Qwen3 controller receipt bytes drifted")

    semantic_run = semantic.get("run")
    provider = semantic.get("provider_instance")
    provider_termination = semantic.get("provider_termination")
    cost_window = termination.get("cost_window")
    termination_value = termination.get("termination")
    if (
        semantic.get("schema_version")
        != "inferdrome.qwen3-offline-verification.v1"
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
        or semantic_run.get("successful_requests") != EXPECTED_SUCCESS_COUNT
        or semantic_run.get("failed_requests") != 0
        or semantic_run.get("ttft_samples") != EXPECTED_SUCCESS_COUNT
        or not isinstance(provider, dict)
        or provider.get("instance_type_name") != "gpu_1x_a10"
        or provider.get("hourly_rate_usd") != "1.29"
        or not isinstance(provider_termination, dict)
        or provider_termination.get("final_status") != "absent"
        or provider_termination.get("request_sent") is not True
        or termination.get("record_kind")
        != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or termination.get("schema_version")
        != "inferdrome.lambda-termination-receipt.v2"
        or not isinstance(cost_window, dict)
        or cost_window.get("hourly_rate_usd") != "1.29"
        or cost_window.get("max_cost_usd") != "0.75"
        or not isinstance(termination_value, dict)
        or termination_value != provider_termination
        or retrieval.get("archive_sha256") != ARCHIVE_SHA256
        or retrieval.get("repository_commit") != CAPTURE_PRODUCER_COMMIT
        or retrieval.get("managed_capability_profile") != PROFILE_ID
    ):
        raise PublicationReviewError("Qwen3 controller receipts disagree")

    started = _timestamp(
        cost_window.get("billing_started_at"),
        label="provider billing start",
    )
    confirmed = _timestamp(
        provider_termination.get("confirmed_at"),
        label="provider termination confirmation",
    )
    elapsed = _elapsed_seconds(started, confirmed)
    cost = (elapsed * Decimal("1.29") / Decimal(3_600)).quantize(
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
            "hourly_rate_usd": "1.29",
            "max_cost_usd": "0.75",
            "statement": (
                "Controller-observed estimate only; provider billing granularity "
                "and the final invoice remain external."
            ),
        },
        "evidence_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
        "provider": {
            "instance_type_name": "gpu_1x_a10",
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
    """Scan every bounded member and classify public-delivery eligibility."""

    scan = _scan_capture(capture_root)
    detectors = scan["detectors"]
    rejected = bool(
        detectors["secret_shaped_values"]["matches"]
        or detectors["personal_or_customer_data"]["status"]
        != "NO_DETECTOR_MATCHES"
        or detectors["binary_or_non_utf8"]["path_count"]
        or detectors["unreviewed_oversized_files"]["path_count"]
    )
    repository_has_license = any(
        path.is_file()
        for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")
        for path in REPOSITORY_ROOT.glob(pattern)
    )
    unresolved_license_codes = [
        "GENERATED_OUTPUT_LICENSE_REVIEW_REQUIRED",
        "MODEL_LICENSE_RECORD_NOT_RETAINED",
        "REPOSITORY_LICENSE_UNSELECTED",
        "VLLM_LICENSE_RECORD_NOT_RETAINED",
        "WORKLOAD_PUBLICATION_LICENSE_UNRESOLVED",
    ]
    if repository_has_license:
        unresolved_license_codes.remove("REPOSITORY_LICENSE_UNSELECTED")
    status = publication_status(
        rejected_findings=rejected,
        unresolved_publication_findings=bool(unresolved_license_codes),
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
            path_count=scan["content"]["generated_responses"][
                "artifact_file_count"
            ],
        ),
        _finding(
            "GPU_UUIDS_PRESENT",
            "DISCLOSE",
            "GPU UUIDs are required provenance and were not rewritten.",
            path_count=detectors["gpu_uuids"]["path_count"],
            path_examples=detectors["gpu_uuids"]["path_examples"],
        ),
        _finding(
            "PRIVATE_NETWORK_ADDRESS_PRESENT",
            "DISCLOSE",
            "Retained server diagnostics contain a private host-network address.",
            path_count=detectors["private_network_addresses"]["path_count"],
            path_examples=detectors["private_network_addresses"]["path_examples"],
        ),
        _finding(
            "PROCESS_IDENTIFIERS_PRESENT",
            "DISCLOSE",
            "Ephemeral host process identifiers are retained as execution evidence.",
            path_count=detectors["process_identifiers"]["path_count"],
            path_examples=detectors["process_identifiers"]["path_examples"],
        ),
        _finding(
            "PROMPTS_PRESENT",
            "SENSITIVE_CONTENT",
            "Source workload files contain the exact measured prompt text.",
            path_count=scan["content"]["prompts"]["artifact_file_count"],
        ),
    ]
    for code in unresolved_license_codes:
        findings.append(
            _finding(
                code,
                "BLOCKS_PUBLICATION",
                "No owner-approved, archive-bound license decision resolves "
                "this scope.",
            )
        )
    if rejected:
        findings.append(
            _finding(
                "CONTENT_REVIEW_REJECTED",
                "REJECT",
                "A secret, personal-data, binary, or size detector blocked "
                "complete public review.",
            )
        )
    findings.sort(key=lambda item: str(item["code"]))

    return {
        "archive": {
            "compressed_size_bytes": archive.stat().st_size,
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
            [*unresolved_license_codes, "OWNER_PUBLICATION_APPROVAL_REQUIRED"]
            + (["CONTENT_REVIEW_REJECTED"] if rejected else [])
        ),
        "detector_results": scan["detectors"],
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


def _measurement(
    measurements: dict[str, Any],
    *,
    definition_id: str,
    aggregation: str,
) -> dict[str, Any]:
    values = measurements.get("measurements")
    if not isinstance(values, list):
        raise PublicationReviewError("Qwen3 measurements have an invalid shape")
    matches = [
        value
        for value in values
        if isinstance(value, dict)
        and value.get("definition_id") == definition_id
        and value.get("aggregation") == aggregation
    ]
    if len(matches) != 1:
        raise PublicationReviewError("Qwen3 measurement identity is ambiguous")
    return matches[0]


def _build_run_handoff(
    capture_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    bundle = capture_root / "runs" / RUN_ID / "bundle"
    analysis = recalculate_bundle(bundle, expected_bundle_digest=BUNDLE_DIGEST)
    descriptor = _strict_json(bundle / "bundle.json")
    resolved = _strict_json(bundle / "experiment.resolved.json")
    invocation = _strict_json(bundle / "native" / "invocation.json")
    measurements = _strict_json(bundle / "derived" / "measurements.json")
    records_path = bundle / "records" / "requests.jsonl"
    try:
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PublicationReviewError("Qwen3 request records are invalid") from None
    if any(not isinstance(record, dict) for record in records):
        raise PublicationReviewError("Qwen3 request records are invalid")
    success_count, ttft_count, independent_p95 = _independent_ttft(records)

    target = resolved.get("target")
    workload = resolved.get("workload")
    traffic = resolved.get("traffic")
    digests = descriptor.get("digests")
    profile_binding = invocation.get("campaign_profile")
    local_proof = invocation.get("local_gpu_proof")
    model_snapshot = (
        local_proof.get("model_snapshot") if isinstance(local_proof, dict) else None
    )
    run_verification = verification["verification"]["run"]
    ttft_p50 = _measurement(
        measurements,
        definition_id="vllm_first_choices_event_v0_26",
        aggregation="p50",
    )
    ttft_p95 = _measurement(
        measurements,
        definition_id="vllm_first_choices_event_v0_26",
        aggregation="p95",
    )
    ttft_p99 = _measurement(
        measurements,
        definition_id="vllm_first_choices_event_v0_26",
        aggregation="p99",
    )
    e2e_p50 = _measurement(
        measurements,
        definition_id="last_choices_event_span_v1",
        aggregation="p50",
    )
    e2e_p95 = _measurement(
        measurements,
        definition_id="last_choices_event_span_v1",
        aggregation="p95",
    )
    output_throughput = _measurement(
        measurements,
        definition_id="successful_output_tokens_per_window_second_v1",
        aggregation="rate",
    )
    profile = _strict_json(PROFILE_PATH)
    workload_bytes = WORKLOAD_PATH.read_bytes()
    if (
        analysis.verification.bundle_digest != BUNDLE_DIGEST
        or verification["capture_manifest_sha256"] != CAPTURE_MANIFEST_SHA256
        or verification["verification"]["repository_commit"]
        != CAPTURE_PRODUCER_COMMIT
        or run_verification.get("run_id") != RUN_ID
        or run_verification.get("bundle_digest") != BUNDLE_DIGEST
        or run_verification.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or descriptor.get("run_id") != RUN_ID
        or descriptor.get("integrity_status") != "VALID"
        or descriptor.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or not isinstance(digests, dict)
        or digests.get("request_plan_digest") != REQUEST_PLAN_DIGEST
        or digests.get("metric_definitions_digest") != METRIC_DEFINITIONS_DIGEST
        or digests.get("execution_fingerprint") != EXECUTION_FINGERPRINT
        or digests.get("source_spec_digest") != SOURCE_SPEC_DIGEST
        or digests.get("exitspec_contract_digest") is not None
        or not isinstance(target, dict)
        or target.get("model") != MODEL_ID
        or target.get("model_revision") != MODEL_REVISION
        or target.get("tokenizer_revision") != MODEL_REVISION
        or not isinstance(workload, dict)
        or workload.get("sha256") != WORKLOAD_SHA256
        or _sha256_bytes(workload_bytes) != WORKLOAD_SHA256
        or not isinstance(traffic, dict)
        or traffic.get("concurrency") != 1
        or traffic.get("measured_requests") != EXPECTED_REQUEST_COUNT
        or traffic.get("warmup_requests") != 12
        or len(records) != EXPECTED_REQUEST_COUNT
        or success_count != EXPECTED_SUCCESS_COUNT
        or ttft_count != EXPECTED_SUCCESS_COUNT
        or independent_p95 != EXPECTED_TTFT_P95_NS
        or ttft_p50.get("value") != EXPECTED_TTFT_P50_NS
        or ttft_p95.get("value") != EXPECTED_TTFT_P95_NS
        or ttft_p99.get("value") != EXPECTED_TTFT_P99_NS
        or e2e_p50.get("value") != EXPECTED_E2E_P50_NS
        or e2e_p95.get("value") != EXPECTED_E2E_P95_NS
        or output_throughput.get("value") != EXPECTED_OUTPUT_THROUGHPUT
        or canonical_document_sha256(profile) != PROFILE_SHA256
        or not isinstance(profile_binding, dict)
        or profile_binding.get("profile_id") != PROFILE_ID
        or profile_binding.get("profile_sha256") != PROFILE_SHA256
        or not isinstance(local_proof, dict)
        or local_proof.get("schema_version") != "inferdrome.local-gpu-proof.v1"
        or local_proof.get("torch_cuda_device_count") != 1
        or not isinstance(model_snapshot, dict)
        or model_snapshot.get("revision") != MODEL_REVISION
        or model_snapshot.get("sha256") != MODEL_SNAPSHOT_SHA256
        or model_snapshot.get("total_bytes") != MODEL_SNAPSHOT_BYTES
    ):
        raise PublicationReviewError("Qwen3 handoff anchors disagree with capture")

    return {
        "bundle_digest": BUNDLE_DIGEST,
        "execution_fingerprint": EXECUTION_FINGERPRINT,
        "metric_definitions_digest": METRIC_DEFINITIONS_DIGEST,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "request_plan_digest": REQUEST_PLAN_DIGEST,
        "request_population": {
            "failed_requests": 0,
            "measured_requests": len(records),
            "successful_requests": success_count,
            "ttft_samples": ttft_count,
        },
        "run_id": RUN_ID,
        "source_spec_digest": SOURCE_SPEC_DIGEST,
        "summary_measurements": {
            "last_choices_event_span_ns": {
                "p50": EXPECTED_E2E_P50_NS,
                "p95": EXPECTED_E2E_P95_NS,
            },
            "output_token_throughput_per_s": EXPECTED_OUTPUT_THROUGHPUT,
            "ttft_ns": {
                "definition_id": "vllm_first_choices_event_v0_26",
                "p50": EXPECTED_TTFT_P50_NS,
                "p95": EXPECTED_TTFT_P95_NS,
                "p99": EXPECTED_TTFT_P99_NS,
                "population": "successful_measured_requests_with_observed_ttft",
                "quantile_method": "nearest_rank_v1",
            },
        },
        "tokenizer": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "workload_sha256": WORKLOAD_SHA256,
    }


def build_handoff_manifest(
    capture_root: Path,
    verification: dict[str, Any],
    publication_review: dict[str, Any],
    operational_summary: dict[str, Any],
) -> dict[str, Any]:
    """Pin the exact capability-spike, review, and operational anchors."""

    run = _build_run_handoff(capture_root, verification)
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
                "path": PROFILE_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
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
            "semantic_verification": operational_summary[
                "semantic_verification"
            ],
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
        "run": run,
        "runtime_capability": {
            "expected_gpu_model": "NVIDIA A10",
            "hardware_attestation": False,
            "hardware_observation": (
                "SELECTED_GPU_REPORTED_NVIDIA_A10_SINGLE_CUDA_DEVICE"
            ),
            "profile_id": PROFILE_ID,
            "spike_outcome": "SPIKE_SUCCEEDED",
            "torch_cuda_device_count": 1,
        },
        "schema_version": "inferdrome.qwen3-gpu-evidence-handoff.v1",
    }


def validate_publication_review(value: Any) -> bool:
    expected_fields = {
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
    if not isinstance(value, dict) or set(value) != expected_fields:
        return False
    try:
        return bool(
            canonical_document_sha256(value) == PUBLICATION_REVIEW_SHA256
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
    expected_fields = {
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
    if not isinstance(value, dict) or set(value) != expected_fields:
        return False
    try:
        return bool(
            canonical_document_sha256(value) == OPERATIONAL_SUMMARY_SHA256
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
            and value["provider"]["instance_type_name"] == "gpu_1x_a10"
            and value["provider"]["termination_final_status"] == "absent"
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
    expected_fields = {
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
    if not isinstance(value, dict) or set(value) != expected_fields:
        return False
    try:
        history = value["history_provenance"]
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
            and value["run"]["workload_sha256"] == WORKLOAD_SHA256
            and value["run"]["request_population"]
            == {
                "failed_requests": 0,
                "measured_requests": EXPECTED_REQUEST_COUNT,
                "successful_requests": EXPECTED_SUCCESS_COUNT,
                "ttft_samples": EXPECTED_SUCCESS_COUNT,
            }
            and value["run"]["summary_measurements"]["ttft_ns"]["p95"]
            == EXPECTED_TTFT_P95_NS
            and value["capability_profile"]["commit"]
            == CAPABILITY_PROFILE_COMMIT
            and value["capability_profile"]["managed_profile"]["sha256"]
            == PROFILE_SHA256
            and value["runtime_capability"]["hardware_attestation"] is False
            and value["runtime_capability"]["spike_outcome"]
            == "SPIKE_SUCCEEDED"
            and value["publication_review"]["publication_status"]
            == "EXTERNAL_ONLY"
            and _DIGEST.fullmatch(value["publication_review"]["sha256"])
            and _DIGEST.fullmatch(value["operational_completion"]["sha256"])
            and history["capture_producer_commit"] == CAPTURE_PRODUCER_COMMIT
            and history["capability_profile_commit"]
            == CAPABILITY_PROFILE_COMMIT
            and (
                history["publication_review_commit"] is None
                or _COMMIT.fullmatch(history["publication_review_commit"])
            )
            and history["eventual_merge_commit"] is None
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


def render_outputs(archive: Path, capture_record: Path) -> dict[Path, bytes]:
    """Verify exact bytes and render the three committed records."""

    archive = archive.absolute()
    try:
        metadata = os.lstat(archive)
    except OSError:
        raise PublicationReviewError("Qwen3 archive is unavailable") from None
    if (
        archive.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != ARCHIVE_SIZE_BYTES
    ):
        raise PublicationReviewError("Qwen3 archive size or type drifted")
    archive = archive.resolve(strict=True)
    verification = qwen3_capture.verify_capture_archive(
        archive,
        expected_archive_sha256=ARCHIVE_SHA256,
        expected_repository_commit=CAPTURE_PRODUCER_COMMIT,
    )
    operational_summary = build_operational_summary(capture_record.absolute())
    try:
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-qwen3-publication-review-"
        ) as root:
            temporary_root = Path(root)
            capture_root: Path | None = None
            try:
                capture_root = capture.extract_capture_archive(
                    archive,
                    temporary_root,
                    expected_archive_sha256=ARCHIVE_SHA256,
                )
                review = build_publication_review(
                    archive,
                    capture_root,
                    verification,
                )
                handoff = build_handoff_manifest(
                    capture_root,
                    verification,
                    review,
                    operational_summary,
                )
            finally:
                if capture_root is not None:
                    capture._make_directories_writable_for_cleanup(capture_root)
    except OSError:
        raise PublicationReviewError(
            "isolated Qwen3 publication review directory is unavailable"
        ) from None
    if (
        not validate_publication_review(review)
        or not validate_operational_summary(operational_summary)
        or not validate_handoff_manifest(handoff)
    ):
        raise PublicationReviewError("rendered Qwen3 publication records are invalid")
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
    """Validate committed anchors without pretending the raw archive is in CI."""

    try:
        review = _load_committed("publication-review.json")
        operational = _load_committed("operational-summary.json")
        handoff = _load_committed("handoff-manifest.json")
    except PublicationReviewError as error:
        print(f"Qwen3 publication records: {error}")
        return 1
    if (
        not validate_publication_review(review)
        or not validate_operational_summary(operational)
        or not validate_handoff_manifest(handoff)
        or canonical_document_sha256(review) != PUBLICATION_REVIEW_SHA256
        or canonical_document_sha256(operational) != OPERATIONAL_SUMMARY_SHA256
        or (
            HANDOFF_MANIFEST_SHA256 is not None
            and canonical_document_sha256(handoff) != HANDOFF_MANIFEST_SHA256
        )
        or handoff["publication_review"]["sha256"]
        != canonical_document_sha256(review)
        or handoff["operational_completion"]["sha256"]
        != canonical_document_sha256(operational)
    ):
        print("Qwen3 publication records are invalid or cross-digest drifted")
        return 1
    print("Qwen3 publication records and cross-digests are valid")
    return 0


def _write_outputs(outputs: dict[Path, bytes]) -> int:
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print("Qwen3 GPU publication review and handoff rendered")
    for path, content in sorted(outputs.items()):
        value = json.loads(content)
        print(
            f"{path.relative_to(REPOSITORY_ROOT)} "
            f"{canonical_document_sha256(value)}"
        )
    return 0


def _check_outputs(outputs: dict[Path, bytes]) -> int:
    mismatches = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path, expected in outputs.items()
        if not path.exists() or path.read_bytes() != expected
    ]
    if mismatches:
        print("Qwen3 publication records are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print("Qwen3 publication records match the exact reviewed archive")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review the exact Qwen3-8B A10 archive without rewriting it"
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
        PublicationReviewError,
        capture.CaptureError,
        qwen3_capture.Qwen3CaptureError,
    ) as error:
        print(f"Qwen3 GPU publication review: {error}")
        return 1
    return _check_outputs(outputs) if args.check else _write_outputs(outputs)


if __name__ == "__main__":
    raise SystemExit(main())
