#!/usr/bin/env python3
"""Review the exact A10 archive and render its deterministic handoff records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from inferdrome.bundle import verify_bundle
from inferdrome.capability_profiles import canonical_document_sha256
from inferdrome.domain import EvidenceEligibility
from inferdrome.errors import InferdromeError

try:
    import scripts.real_gpu_capture as capture
except ModuleNotFoundError:  # Direct script execution adds scripts/, not the repo root.
    import real_gpu_capture as capture

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = REPOSITORY_ROOT / (
    "gpu-proof-retrieved/"
    "20260820T003703Z-c08b46d9fbd8-4fbd20e6-UNVERIFIED/capture.tar.gz"
)
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "evidence" / "gpu" / "2026-08-20-a10"

CAPTURE_PRODUCER_COMMIT = "c08b46d9fbd87477f45d130aa3c63615937c4dc3"
CAPABILITY_PROFILE_COMMIT = "53a5c55bdb146f29804c5490ce1a020d70f26bb4"
PUBLICATION_REVIEW_COMMIT: str | None = (
    "79b62e6b13d40604a569e80cea9b6cecb1cb6310"
)
# Frozen input from the exact historical review. Later checkout state must not
# rewrite the sealed publication review or imply archive publication rights.
REPOSITORY_LICENSE_PRESENT_AT_REVIEW = False
ARCHIVE_SIZE_BYTES = 689_272
ARCHIVE_SHA256 = (
    "sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424"
)
CAPTURE_MANIFEST_SHA256 = (
    "sha256:1d4ea1e251c5a84a104333ab8579d580838701a70cc38b64b68c88f66266e0cb"
)
PUBLICATION_REVIEW_SHA256 = (
    "sha256:7f1b3be53695e9e3a2009eb28ce008bb2486ae882e52364e26bece770a6d33ff"
)
HANDOFF_MANIFEST_SHA256 = (
    "sha256:bc90ac7d0044b32556ce8e78181635f2a2d218e3de7a793062e5dc2b3d6cd4bd"
)
RUN_ID = "run-533c9f5f783958fb6077069a6c577144"
BUNDLE_DIGEST = (
    "sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675"
)
BUNDLE_MEMBER_PATH = (
    "capture/single/real-gpu-5osfyjjl/runs/"
    f"{RUN_ID}/bundle"
)
SINGLE_PROOF_ROOT = "capture/single/real-gpu-5osfyjjl"
CORRUPTED_BUNDLE_MEMBER_PATH = f"{SINGLE_PROOF_ROOT}/corrupted-bundle-copy"
SYNTHETIC_RUN_ID = "run-5f8d7617421b9f4d0484f5807baa7849"
SYNTHETIC_BUNDLE_MEMBER_PATH = (
    f"{SINGLE_PROOF_ROOT}/synthetic-runs/{SYNTHETIC_RUN_ID}/bundle"
)
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
WORKLOAD_SHA256 = (
    "sha256:22bf3389cc29ee946ae567870d7f8d7b458594224542a796e8990c15b1cfcd63"
)
REQUEST_PLAN_DIGEST = (
    "sha256:0fb852366933598da4139114f416b441c52d2c83cae07b7d8938bd482a12fc8e"
)
METRIC_DEFINITIONS_DIGEST = (
    "sha256:e237ff8613c6eec52a6053b3f6b47563ffc758c957ee298e57fe3c482a389131"
)
EXECUTION_FINGERPRINT = (
    "sha256:76d984ea57a0e7cb00520255a6e362f22885d713a875195a7397771937060edd"
)
SOURCE_SPEC_DIGEST = (
    "sha256:4b43fc218cb4d4a260dc70b92ff3a263fbbec2e8224f6f11cb748128931dce97"
)
MANAGED_PROFILE_SHA256 = (
    "sha256:9d03b5d0822ed829ddbfa4c87c75530885b9ad51ee2c0cb7c5e31a075996fe34"
)
LOCAL_PROOF_SCHEMA_SHA256 = (
    "sha256:cf83bbdea2bba4c30b8f0e2c5f34f34a4077501207881fdbdab021571d665547"
)
EXPECTED_REQUEST_COUNT = 100
EXPECTED_SUCCESS_COUNT = 100
EXPECTED_TTFT_P95_NS = 14_797_213
PROFILE_PATH = REPOSITORY_ROOT / (
    "profiles/v1/managed-vllm-0.26-evidence-profile.json"
)
LOCAL_PROOF_SCHEMA_PATH = REPOSITORY_ROOT / (
    "profiles/v1/local-gpu-proof.schema.json"
)
PROPOSED_FIXTURE_LOCATION = (
    "https://github.com/jayeshsuyal/inferdrome/releases/download/"
    "gpu-evidence-2026-08-20/"
    "inferdrome-a10-capture-f2408fd0.tar.gz"
)

_MAX_REVIEW_FILE_BYTES = 16_777_216
_MAX_REVIEW_TOTAL_BYTES = 268_435_456
_MAX_PATH_EXAMPLES = 8
_GPU_UUID = re.compile(r"\bGPU-[0-9A-Za-z-]{8,120}\b")
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9:])/(?:tmp|usr|opt|home|var|Users)/[^\s\"'<>]*"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_SECRET_DETECTORS = {
    "aws-access-key-id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bearer-token": re.compile(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
    ),
    "credential-assignment": re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|"
        r"client[_-]?secret)\s*[\"']?\s*[:=]\s*[\"']?"
        r"(?!null\b|none\b|redacted\b|\$\{)[A-Za-z0-9/+_.=-]{12,}"
    ),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private-key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}

PublicationStatus = Literal["APPROVED_PUBLIC", "EXTERNAL_ONLY", "REJECTED"]


class PublicationReviewError(RuntimeError):
    """The exact archive could not be completely and safely reviewed."""


def _contains_floating_number(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_floating_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_floating_number(item) for item in value)
    return False


def _strict_json(
    path: Path,
    *,
    require_deterministic_render: bool = False,
) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")

        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        parsed = json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PublicationReviewError(f"{path.name} is not strict JSON") from None
    if not isinstance(parsed, dict):
        raise PublicationReviewError(f"{path.name} is not a JSON object")
    if require_deterministic_render and (
        _contains_floating_number(parsed)
        or text
        != json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ):
        raise PublicationReviewError(
            f"{path.name} is not deterministic committed JSON"
        )
    return parsed


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _archive_member(capture_root: Path, member_path: str) -> Path:
    member = PurePosixPath(member_path)
    if member.is_absolute() or not member.parts or member.parts[0] != "capture":
        raise PublicationReviewError("handoff member path is unsafe")
    path = capture_root.joinpath(*member.parts[1:])
    try:
        path.resolve(strict=True).relative_to(capture_root.resolve(strict=True))
    except (OSError, ValueError):
        raise PublicationReviewError(
            "handoff member is absent or escapes capture"
        ) from None
    return path


def _paths(paths: Iterable[str]) -> list[str]:
    return sorted(set(paths))[:_MAX_PATH_EXAMPLES]


def _walk_json_process_fields(value: Any) -> int:
    if isinstance(value, dict):
        count = sum(
            1
            for key, item in value.items()
            if key in {"pid", "process_group_id"}
            and isinstance(item, int)
            and not isinstance(item, bool)
        )
        return count + sum(_walk_json_process_fields(item) for item in value.values())
    if isinstance(value, list):
        return sum(_walk_json_process_fields(item) for item in value)
    return 0


def _parsed_ipv4(value: str) -> tuple[int, int, int, int] | None:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError:
        return None
    if len(parts) != 4 or any(not 0 <= part <= 255 for part in parts):
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _is_private_ipv4(parts: tuple[int, int, int, int]) -> bool:
    return bool(
        parts[0] == 10
        or (parts[0] == 172 and 16 <= parts[1] <= 31)
        or (parts[0] == 192 and parts[1] == 168)
        or (parts[0] == 169 and parts[1] == 254)
    )


def publication_status(
    *,
    rejected_findings: bool,
    unresolved_publication_findings: bool,
    owner_publication_approved: bool,
) -> PublicationStatus:
    """Apply the frozen three-state publication decision table."""

    if rejected_findings:
        return "REJECTED"
    if unresolved_publication_findings or not owner_publication_approved:
        return "EXTERNAL_ONLY"
    return "APPROVED_PUBLIC"


def _scan_capture(capture_root: Path) -> dict[str, Any]:
    file_count = 0
    directory_count = 0
    total_bytes = 0
    scanned_bytes = 0
    binary_paths: list[str] = []
    oversized_paths: list[str] = []
    absolute_path_files: list[str] = []
    gpu_uuid_files: list[str] = []
    process_id_files: list[str] = []
    prompt_files: list[str] = []
    response_files: list[str] = []
    log_files: list[str] = []
    environment_files: list[str] = []
    license_files: list[str] = []
    email_files: list[str] = []
    private_network_address_files: list[str] = []
    public_network_address_files: list[str] = []
    package_version_candidate_files: list[str] = []
    secret_files: dict[str, list[str]] = {
        identifier: [] for identifier in _SECRET_DETECTORS
    }
    prompt_records = 0
    generated_response_records = 0
    intentionally_corrupted_response_files = 0
    process_field_count = 0
    absolute_path_match_count = 0
    gpu_uuid_match_count = 0

    for directory, directory_names, filenames in os.walk(
        capture_root,
        followlinks=False,
    ):
        directory_names.sort()
        filenames.sort()
        directory_count += 1
        current = Path(directory)
        for name in filenames:
            path = current / name
            relative = "capture/" + path.relative_to(capture_root).as_posix()
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise PublicationReviewError("capture changed during publication scan")
            file_count += 1
            total_bytes += metadata.st_size
            if metadata.st_size > _MAX_REVIEW_FILE_BYTES:
                oversized_paths.append(relative)
                continue
            scanned_bytes += metadata.st_size
            if scanned_bytes > _MAX_REVIEW_TOTAL_BYTES:
                raise PublicationReviewError(
                    "publication scan exceeds total-byte limit"
                )
            content = path.read_bytes()
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                binary_paths.append(relative)
                continue

            lowercase = relative.lower()
            if lowercase.endswith("workload.source.jsonl"):
                prompt_files.append(relative)
                for line in text.splitlines():
                    if line:
                        try:
                            if not isinstance(json.loads(line), dict):
                                raise ValueError
                        except (json.JSONDecodeError, ValueError):
                            raise PublicationReviewError(
                                "workload source contains invalid JSONL"
                            ) from None
                        prompt_records += 1
            if lowercase.endswith("benchmark-result.json"):
                response_files.append(relative)
                if "/corrupted-bundle-copy/" in lowercase:
                    intentionally_corrupted_response_files += 1
                else:
                    parsed = _strict_json(path)
                    generated = parsed.get("generated_texts")
                    rows = parsed.get("rows")
                    if isinstance(generated, list) and not any(
                        not isinstance(item, str) for item in generated
                    ):
                        generated_response_records += len(generated)
                    elif isinstance(rows, list) and not any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("response_content"), str)
                        for item in rows
                    ):
                        generated_response_records += len(rows)
                    else:
                        raise PublicationReviewError(
                            "native benchmark result omits generated response rows"
                        )
            if lowercase.endswith((".stdout", ".stderr", ".log")):
                log_files.append(relative)
            if (
                lowercase.endswith("environment.json")
                or lowercase.endswith("host-preparation.json")
                or lowercase.endswith("python-packages.txt")
                or lowercase.endswith("producer-version.txt")
                or lowercase.endswith("vllm-version.txt")
            ):
                environment_files.append(relative)
            if any(
                part.lower().startswith(("license", "licence", "notice", "copying"))
                for part in PurePosixPath(relative).parts
            ):
                license_files.append(relative)

            path_matches = tuple(_ABSOLUTE_PATH.finditer(text))
            if path_matches:
                absolute_path_files.append(relative)
                absolute_path_match_count += len(path_matches)
            uuid_matches = tuple(_GPU_UUID.finditer(text))
            if uuid_matches:
                gpu_uuid_files.append(relative)
                gpu_uuid_match_count += len(uuid_matches)
            if _EMAIL.search(text):
                email_files.append(relative)
            for match in _IPV4.finditer(text):
                address = _parsed_ipv4(match.group())
                if address is None or address[0] in {0, 127}:
                    continue
                if lowercase.endswith(
                    ("python-packages.txt", "01-prepare-host.log")
                ):
                    package_version_candidate_files.append(relative)
                elif _is_private_ipv4(address):
                    private_network_address_files.append(relative)
                else:
                    public_network_address_files.append(relative)
            for identifier, detector in _SECRET_DETECTORS.items():
                if detector.search(text):
                    secret_files[identifier].append(relative)

            if lowercase.endswith(".json") and not (
                "/corrupted-bundle-copy/" in lowercase
                and lowercase.endswith("benchmark-result.json")
            ):
                parsed = _strict_json(path)
                count = _walk_json_process_fields(parsed)
                if count:
                    process_id_files.append(relative)
                    process_field_count += count

    secret_matches = [
        {
            "detector": identifier,
            "path_count": len(set(paths)),
            "path_examples": _paths(paths),
        }
        for identifier, paths in sorted(secret_files.items())
        if paths
    ]
    return {
        "archive_tree": {
            "directory_count": directory_count,
            "expanded_bytes": total_bytes,
            "file_count": file_count,
        },
        "content": {
            "environment_and_package_inventory": {
                "file_count": len(set(environment_files)),
                "path_examples": _paths(environment_files),
                "status": "PRESENT_REVIEWED",
            },
            "generated_responses": {
                "artifact_file_count": len(set(response_files)),
                "intentionally_corrupted_artifact_file_count": (
                    intentionally_corrupted_response_files
                ),
                "record_count_including_retained_duplicates": (
                    generated_response_records
                ),
                "status": "PRESENT_REVIEWED",
            },
            "prompts": {
                "artifact_file_count": len(set(prompt_files)),
                "record_count_including_separate_runs": prompt_records,
                "status": "PRESENT_REVIEWED",
            },
            "stdout_and_stderr": {
                "file_count": len(set(log_files)),
                "path_examples": _paths(log_files),
                "status": "PRESENT_REVIEWED",
            },
        },
        "detectors": {
            "absolute_paths": {
                "match_count": absolute_path_match_count,
                "path_count": len(set(absolute_path_files)),
                "path_examples": _paths(absolute_path_files),
            },
            "binary_or_non_utf8": {
                "path_count": len(set(binary_paths)),
                "path_examples": _paths(binary_paths),
            },
            "gpu_uuids": {
                "match_count": gpu_uuid_match_count,
                "path_count": len(set(gpu_uuid_files)),
                "path_examples": _paths(gpu_uuid_files),
            },
            "personal_or_customer_data": {
                "email_path_count": len(set(email_files)),
                "email_path_examples": _paths(email_files),
                "public_network_address_path_count": len(
                    set(public_network_address_files)
                ),
                "public_network_address_path_examples": _paths(
                    public_network_address_files
                ),
                "status": (
                    "NO_DETECTOR_MATCHES"
                    if not email_files and not public_network_address_files
                    else "DETECTOR_MATCHES"
                ),
            },
            "private_network_addresses": {
                "path_count": len(set(private_network_address_files)),
                "path_examples": _paths(private_network_address_files),
                "status": "PRESENT_DISCLOSE",
            },
            "version_like_dotted_quad_candidates": {
                "adjudication": "PACKAGE_VERSION_CONTEXT",
                "path_count": len(set(package_version_candidate_files)),
                "path_examples": _paths(package_version_candidate_files),
            },
            "process_identifiers": {
                "json_field_count": process_field_count,
                "path_count": len(set(process_id_files)),
                "path_examples": _paths(process_id_files),
            },
            "secret_shaped_values": {
                "matches": secret_matches,
                "status": "NO_DETECTOR_MATCHES" if not secret_matches else "MATCHES",
            },
            "unreviewed_oversized_files": {
                "path_count": len(set(oversized_paths)),
                "path_examples": _paths(oversized_paths),
            },
        },
        "license_artifacts": {
            "archive_license_file_count": len(set(license_files)),
            "archive_license_path_examples": _paths(license_files),
        },
        "review_limits": {
            "max_file_bytes": _MAX_REVIEW_FILE_BYTES,
            "max_total_scanned_bytes": _MAX_REVIEW_TOTAL_BYTES,
            "scanned_bytes": scanned_bytes,
        },
    }


def _finding(
    code: str,
    disposition: str,
    summary: str,
    *,
    path_count: int | None = None,
    path_examples: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "code": code,
        "disposition": disposition,
        "summary": summary,
    }
    if path_count is not None:
        value["path_count"] = path_count
    if path_examples is not None:
        value["path_examples"] = path_examples
    return value


def build_publication_review(
    archive: Path,
    capture_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Scan all bounded text members and classify public-delivery eligibility."""

    scan = _scan_capture(capture_root)
    detectors = scan["detectors"]
    rejected = bool(
        detectors["secret_shaped_values"]["matches"]
        or detectors["personal_or_customer_data"]["status"]
        != "NO_DETECTOR_MATCHES"
        or detectors["binary_or_non_utf8"]["path_count"]
        or detectors["unreviewed_oversized_files"]["path_count"]
    )
    repository_has_license = REPOSITORY_LICENSE_PRESENT_AT_REVIEW
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
                "At least one secret, personal-data, binary, or size detector blocked "
                "complete public review.",
            )
        )
    findings.sort(key=lambda item: str(item["code"]))

    archive_metadata = archive.stat()
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
            [
                *unresolved_license_codes,
                "OWNER_PUBLICATION_APPROVAL_REQUIRED",
            ]
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


def _request_records(bundle: Path) -> list[dict[str, Any]]:
    path = bundle / "records" / "requests.jsonl"
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                raise ValueError
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PublicationReviewError("request records are not strict JSONL") from None
    return records


def _independent_ttft(records: list[dict[str, Any]]) -> tuple[int, int, int]:
    ttft_values: list[int] = []
    success_count = 0
    for record in records:
        outcome = record.get("outcome")
        timing = record.get("timing")
        if not isinstance(outcome, dict) or not isinstance(timing, dict):
            raise PublicationReviewError("request row has an invalid shape")
        if outcome.get("status") == "SUCCESS":
            success_count += 1
            value = timing.get("ttft_ns")
            if (
                timing.get("ttft_definition")
                != "vllm_first_choices_event_v0_26"
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise PublicationReviewError("successful row lacks native TTFT")
            ttft_values.append(value)
    if not ttft_values:
        raise PublicationReviewError("native TTFT population is empty")
    rank = (95 * len(ttft_values) + 99) // 100
    p95 = sorted(ttft_values)[rank - 1]
    return success_count, len(ttft_values), p95


def _verify_rejection_facts(capture_root: Path) -> dict[str, Any]:
    corrupted = _archive_member(capture_root, CORRUPTED_BUNDLE_MEMBER_PATH)
    try:
        verify_bundle(corrupted)
    except InferdromeError:
        corrupted_rejected = True
    else:
        corrupted_rejected = False
    if not corrupted_rejected:
        raise PublicationReviewError("corrupted bundle unexpectedly verified")

    synthetic = _archive_member(capture_root, SYNTHETIC_BUNDLE_MEMBER_PATH)
    try:
        synthetic_report = verify_bundle(synthetic)
    except InferdromeError:
        raise PublicationReviewError("synthetic fixture no longer verifies") from None
    if (
        synthetic_report.run_id != SYNTHETIC_RUN_ID
        or synthetic_report.descriptor.evidence_eligibility
        is not EvidenceEligibility.SYNTHETIC_ONLY
    ):
        raise PublicationReviewError("synthetic fixture eligibility drifted")
    return {
        "corrupted": {
            "archive_member_path": CORRUPTED_BUNDLE_MEMBER_PATH,
            "independently_replayed": True,
            "rejection_class": "INTEGRITY_MISMATCH",
            "rejected": True,
        },
        "synthetic": {
            "archive_member_path": SYNTHETIC_BUNDLE_MEMBER_PATH,
            "independently_replayed": True,
            "rejection_class": "EVIDENCE_INELIGIBLE",
            "rejected_from_customer_evidence": True,
            "run_id": SYNTHETIC_RUN_ID,
            "verified_eligibility": "SYNTHETIC_ONLY",
        },
    }


def build_handoff_manifest(
    capture_root: Path,
    verification: dict[str, Any],
    publication_review: dict[str, Any],
) -> dict[str, Any]:
    """Recalculate and pin the exact single-run ExitSpec handoff anchors."""

    bundle = _archive_member(capture_root, BUNDLE_MEMBER_PATH)
    descriptor = _strict_json(bundle / "bundle.json")
    resolved = _strict_json(bundle / "experiment.resolved.json")
    invocation = _strict_json(bundle / "native" / "invocation.json")
    measurements = _strict_json(bundle / "derived" / "measurements.json")
    records = _request_records(bundle)
    success_count, ttft_sample_count, p95 = _independent_ttft(records)
    measured_p95 = next(
        (
            item
            for item in measurements.get("measurements", [])
            if isinstance(item, dict)
            and item.get("definition_id") == "vllm_first_choices_event_v0_26"
            and item.get("aggregation") == "p95"
        ),
        None,
    )
    if not isinstance(measured_p95, dict) or measured_p95.get("value") != p95:
        raise PublicationReviewError("stored native TTFT p95 disagrees")

    workload = _archive_member(
        capture_root,
        f"{SINGLE_PROOF_ROOT}/runs/{RUN_ID}/inputs/workload.source.jsonl",
    )
    workload_digest = _sha256_bytes(workload.read_bytes())
    profile = _strict_json(PROFILE_PATH)
    local_schema = _strict_json(LOCAL_PROOF_SCHEMA_PATH)
    profile_digest = canonical_document_sha256(profile)
    local_schema_digest = canonical_document_sha256(local_schema)
    review_digest = canonical_document_sha256(publication_review)
    local_proof = invocation.get("local_gpu_proof")
    if not isinstance(local_proof, dict):
        raise PublicationReviewError("producer invocation omits local GPU proof")
    target = resolved.get("target")
    resolved_workload = resolved.get("workload")
    digests = descriptor.get("digests")
    single = verification["verification"]["single"]
    if (
        verification["capture_manifest_sha256"] != CAPTURE_MANIFEST_SHA256
        or verification["verification"]["repository_commit"]
        != CAPTURE_PRODUCER_COMMIT
        or single.get("run_id") != RUN_ID
        or single.get("bundle_digest") != BUNDLE_DIGEST
        or descriptor.get("run_id") != RUN_ID
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
        or not isinstance(resolved_workload, dict)
        or resolved_workload.get("sha256") != WORKLOAD_SHA256
        or workload_digest != WORKLOAD_SHA256
        or len(records) != EXPECTED_REQUEST_COUNT
        or success_count != EXPECTED_SUCCESS_COUNT
        or ttft_sample_count != EXPECTED_SUCCESS_COUNT
        or p95 != EXPECTED_TTFT_P95_NS
        or profile.get("profile_id")
        != "inferdrome.managed-vllm-0.26-evidence-profile.v1"
        or local_proof.get("schema_version") != "inferdrome.local-gpu-proof.v1"
    ):
        raise PublicationReviewError("handoff anchors disagree with exact capture")

    rejection_facts = _verify_rejection_facts(capture_root)
    comparison = verification["verification"]["comparison"]
    return {
        "acceptance_boundary": {
            "current_status": "PENDING_EXTERNAL_EXITSPEC",
            "inferdrome_acceptance_verdict": None,
            "statement": (
                "Inferdrome measures and verifies evidence; it assigns no PASS, "
                "FAIL, or NOT_PROVEN acceptance verdict."
            ),
        },
        "archive": {
            "bundle_member_path": BUNDLE_MEMBER_PATH,
            "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
            "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
            "sha256": ARCHIVE_SHA256,
        },
        "capability_profile": {
            "commit": CAPABILITY_PROFILE_COMMIT,
            "local_gpu_proof_schema": {
                "identity": "inferdrome.local-gpu-proof.v1",
                "path": "profiles/v1/local-gpu-proof.schema.json",
                "sha256": local_schema_digest,
            },
            "managed_profile": {
                "identity": "inferdrome.managed-vllm-0.26-evidence-profile.v1",
                "path": "profiles/v1/managed-vllm-0.26-evidence-profile.json",
                "sha256": profile_digest,
            },
        },
        "comparison_context": {
            "comparison_plan_digest": comparison["comparison_plan_digest"],
            "comparison_plan_id": comparison["comparison_plan_id"],
            "comparison_result_digest": comparison["comparison_result_digest"],
            "comparison_result_id": comparison["comparison_result_id"],
            "status": comparison["status"],
        },
        "contract_binding": {
            "chronology": "RETROSPECTIVE",
            "chronology_disclosure": (
                "A future ExitSpec contract may be frozen before evaluation, but this "
                "capture does not prove that contract preceded measurement."
            ),
            "required_consumer_mode": "EXTERNAL_RECEIPT_BINDING",
            "producer_exitspec_contract_digest": None,
        },
        "fixture_delivery": {
            "proposed_checksum_pinned_location": PROPOSED_FIXTURE_LOCATION,
            "publication_state": "BLOCKED_PENDING_OWNER_APPROVAL",
            "required_sha256": ARCHIVE_SHA256,
            "statement": (
                "The proposed release asset would be detectably replaceable through "
                "this checksum, not intrinsically immutable. Vendoring these exact "
                "reviewed bytes remains an alternative owner and ExitSpec decision."
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
        "publication_review": {
            "owner_publication_approval_required": publication_review[
                "owner_publication_approval_required"
            ],
            "path": "evidence/gpu/2026-08-20-a10/publication-review.json",
            "publication_status": publication_review["publication_status"],
            "sha256": review_digest,
        },
        "rejection_facts": rejection_facts,
        "run": {
            "bundle_digest": BUNDLE_DIGEST,
            "execution_fingerprint": EXECUTION_FINGERPRINT,
            "metric_definitions_digest": METRIC_DEFINITIONS_DIGEST,
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
            },
            "request_plan_digest": REQUEST_PLAN_DIGEST,
            "request_population": {
                "measured_requests": len(records),
                "successful_requests": success_count,
                "ttft_samples": ttft_sample_count,
            },
            "run_id": RUN_ID,
            "source_spec_digest": SOURCE_SPEC_DIGEST,
            "tokenizer": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
            },
            "ttft": {
                "aggregation": "p95",
                "definition_id": "vllm_first_choices_event_v0_26",
                "independently_expected_value": p95,
                "population": "successful_measured_requests_with_observed_ttft",
                "quantile_method": "nearest_rank_v1",
                "unit": "ns",
            },
            "workload_sha256": workload_digest,
        },
        "schema_version": "inferdrome.gpu-evidence-handoff.v1",
    }


def _is_exact_closed_record(
    value: Any,
    *,
    fields: set[str],
    sha256: str,
) -> bool:
    """Reject any root or nested field/value outside one frozen record."""

    try:
        return bool(
            isinstance(value, dict)
            and set(value) == fields
            and not _contains_floating_number(value)
            and canonical_document_sha256(value) == sha256
        )
    except (TypeError, ValueError):
        return False


def validate_publication_review(value: Any) -> bool:
    """Validate every field and immutable identity in the committed A10 review."""

    if not _is_exact_closed_record(
        value,
        fields={
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
        },
        sha256=PUBLICATION_REVIEW_SHA256,
    ):
        return False
    try:
        licenses = value["license_review"]
        return bool(
            value["schema_version"]
            == "inferdrome.gpu-evidence-publication-review.v1"
            and value["scope"] == "exact_archive_bytes"
            and value["archive"]
            == {
                "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
                "sha256": ARCHIVE_SHA256,
            }
            and value["archive_integrity_and_safety"]["capture_manifest_sha256"]
            == CAPTURE_MANIFEST_SHA256
            and value["archive_integrity_and_safety"]["isolated_verification"] is True
            and value["publication_status"] == "EXTERNAL_ONLY"
            and value["owner_publication_approval_required"] is True
            and value["raw_archive_modified"] is False
            and all(
                licenses[field] is False
                for field in (
                    "generated_output_license_resolved",
                    "model_license_resolved",
                    "repository_license_present",
                    "vllm_license_resolved",
                    "workload_publication_license_resolved",
                )
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def validate_handoff_manifest(value: Any) -> bool:
    """Validate every field and immutable identity in the committed A10 handoff."""

    if not _is_exact_closed_record(
        value,
        fields={
            "acceptance_boundary",
            "archive",
            "capability_profile",
            "comparison_context",
            "contract_binding",
            "fixture_delivery",
            "history_provenance",
            "publication_review",
            "rejection_facts",
            "run",
            "schema_version",
        },
        sha256=HANDOFF_MANIFEST_SHA256,
    ):
        return False
    try:
        history = value["history_provenance"]
        profile = value["capability_profile"]
        review = value["publication_review"]
        run = value["run"]
        return bool(
            value["schema_version"] == "inferdrome.gpu-evidence-handoff.v1"
            and value["archive"]
            == {
                "bundle_member_path": BUNDLE_MEMBER_PATH,
                "capture_manifest_sha256": CAPTURE_MANIFEST_SHA256,
                "compressed_size_bytes": ARCHIVE_SIZE_BYTES,
                "sha256": ARCHIVE_SHA256,
            }
            and value["comparison_context"]
            == {
                "comparison_plan_digest": (
                    "sha256:25dd7f87d02572b6c3f992014944241595e8240d7301a58cba55da11eae1c60e"
                ),
                "comparison_plan_id": (
                    "comparison-plan-5f4abd9b24ab717e910b166c5b793038"
                ),
                "comparison_result_digest": (
                    "sha256:6943eb577b368f036b4536626076d7b7a4f23caf8df7f839e5a1248dbaae774a"
                ),
                "comparison_result_id": (
                    "comparison-result-5f4abd9b24ab717e910b166c5b793038"
                ),
                "status": "COMPARABLE",
            }
            and run["run_id"] == RUN_ID
            and run["bundle_digest"] == BUNDLE_DIGEST
            and run["execution_fingerprint"] == EXECUTION_FINGERPRINT
            and run["metric_definitions_digest"] == METRIC_DEFINITIONS_DIGEST
            and run["request_plan_digest"] == REQUEST_PLAN_DIGEST
            and run["source_spec_digest"] == SOURCE_SPEC_DIGEST
            and run["workload_sha256"] == WORKLOAD_SHA256
            and run["model"] == {"id": MODEL_ID, "revision": MODEL_REVISION}
            and run["tokenizer"] == {"id": MODEL_ID, "revision": MODEL_REVISION}
            and run["request_population"]
            == {
                "measured_requests": EXPECTED_REQUEST_COUNT,
                "successful_requests": EXPECTED_SUCCESS_COUNT,
                "ttft_samples": EXPECTED_SUCCESS_COUNT,
            }
            and run["ttft"]["independently_expected_value"] == EXPECTED_TTFT_P95_NS
            and profile["commit"] == CAPABILITY_PROFILE_COMMIT
            and profile["managed_profile"]
            == {
                "identity": "inferdrome.managed-vllm-0.26-evidence-profile.v1",
                "path": "profiles/v1/managed-vllm-0.26-evidence-profile.json",
                "sha256": MANAGED_PROFILE_SHA256,
            }
            and profile["local_gpu_proof_schema"]
            == {
                "identity": "inferdrome.local-gpu-proof.v1",
                "path": "profiles/v1/local-gpu-proof.schema.json",
                "sha256": LOCAL_PROOF_SCHEMA_SHA256,
            }
            and review
            == {
                "owner_publication_approval_required": True,
                "path": "evidence/gpu/2026-08-20-a10/publication-review.json",
                "publication_status": "EXTERNAL_ONLY",
                "sha256": PUBLICATION_REVIEW_SHA256,
            }
            and history["capture_producer_commit"] == CAPTURE_PRODUCER_COMMIT
            and history["capability_profile_commit"] == CAPABILITY_PROFILE_COMMIT
            and history["publication_review_commit"] == PUBLICATION_REVIEW_COMMIT
            and history["eventual_merge_commit"] is None
            and value["contract_binding"]["producer_exitspec_contract_digest"] is None
            and value["contract_binding"]["chronology"] == "RETROSPECTIVE"
            and value["contract_binding"]["required_consumer_mode"]
            == "EXTERNAL_RECEIPT_BINDING"
            and value["acceptance_boundary"]["inferdrome_acceptance_verdict"] is None
            and value["acceptance_boundary"]["current_status"]
            == "PENDING_EXTERNAL_EXITSPEC"
            and value["fixture_delivery"]["publication_state"]
            == "BLOCKED_PENDING_OWNER_APPROVAL"
            and value["fixture_delivery"]["required_sha256"] == ARCHIVE_SHA256
            and value["rejection_facts"]["synthetic"]["run_id"] == SYNTHETIC_RUN_ID
            and value["rejection_facts"]["synthetic"]["verified_eligibility"]
            == "SYNTHETIC_ONLY"
        )
    except (KeyError, TypeError, ValueError):
        return False


def _render(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def render_outputs(archive: Path) -> dict[Path, bytes]:
    """Verify, scan, and render the two committed machine-readable records."""

    archive = archive.absolute()
    metadata = os.lstat(archive)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or archive.is_symlink()
        or metadata.st_size != ARCHIVE_SIZE_BYTES
    ):
        raise PublicationReviewError("archive size or file type differs from handoff")
    archive = archive.resolve(strict=True)
    verification = capture.verify_capture_archive(
        archive,
        expected_archive_sha256=ARCHIVE_SHA256,
        expected_repository_commit=CAPTURE_PRODUCER_COMMIT,
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-publication-review-"
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
                )
            finally:
                if capture_root is not None:
                    capture._make_directories_writable_for_cleanup(capture_root)
    except OSError:
        raise PublicationReviewError(
            "isolated publication review directory is unavailable"
        ) from None
    if not validate_publication_review(review) or not validate_handoff_manifest(
        handoff
    ):
        raise PublicationReviewError("rendered publication records failed validation")
    return {
        DEFAULT_OUTPUT_DIRECTORY / "handoff-manifest.json": _render(handoff),
        DEFAULT_OUTPUT_DIRECTORY / "publication-review.json": _render(review),
    }


def _load_committed(name: str, *, directory: Path) -> dict[str, Any]:
    return _strict_json(
        directory / name,
        require_deterministic_render=True,
    )


def check_committed_records(directory: Path | None = None) -> int:
    """Validate the tracked A10 records without requiring the external archive."""

    record_directory = DEFAULT_OUTPUT_DIRECTORY if directory is None else directory
    try:
        review = _load_committed(
            "publication-review.json",
            directory=record_directory,
        )
        handoff = _load_committed(
            "handoff-manifest.json",
            directory=record_directory,
        )
        managed_profile = _strict_json(
            PROFILE_PATH,
            require_deterministic_render=True,
        )
        local_proof_schema = _strict_json(
            LOCAL_PROOF_SCHEMA_PATH,
            require_deterministic_render=True,
        )
    except PublicationReviewError as error:
        print(f"A10 publication records: {error}")
        return 1

    try:
        review_digest = canonical_document_sha256(review)
        valid = bool(
            validate_publication_review(review)
            and validate_handoff_manifest(handoff)
            and review_digest == PUBLICATION_REVIEW_SHA256
            and canonical_document_sha256(handoff) == HANDOFF_MANIFEST_SHA256
            and handoff["publication_review"]["sha256"] == review_digest
            and handoff["publication_review"]["publication_status"]
            == review["publication_status"]
            and handoff["publication_review"][
                "owner_publication_approval_required"
            ]
            is review["owner_publication_approval_required"]
            and handoff["archive"]["sha256"] == review["archive"]["sha256"]
            and handoff["archive"]["compressed_size_bytes"]
            == review["archive"]["compressed_size_bytes"]
            and handoff["archive"]["capture_manifest_sha256"]
            == review["archive_integrity_and_safety"]["capture_manifest_sha256"]
            and canonical_document_sha256(managed_profile)
            == MANAGED_PROFILE_SHA256
            and canonical_document_sha256(local_proof_schema)
            == LOCAL_PROOF_SCHEMA_SHA256
            and handoff["capability_profile"]["managed_profile"]["sha256"]
            == MANAGED_PROFILE_SHA256
            and handoff["capability_profile"]["local_gpu_proof_schema"]["sha256"]
            == LOCAL_PROOF_SCHEMA_SHA256
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        print("A10 publication records are invalid or cross-identity drifted")
        return 1
    print("A10 publication records and cross-identities are valid")
    return 0


def _write_outputs(outputs: dict[Path, bytes]) -> int:
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print("GPU publication review and handoff rendered")
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
        print("GPU publication records are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print("GPU publication records match the exact reviewed archive")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review the exact A10 archive without rewriting it"
    )
    parser.add_argument("archive", nargs="?", default=str(DEFAULT_ARCHIVE))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check",
        action="store_true",
        help="fail when committed review or handoff bytes differ",
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
        outputs = render_outputs(Path(args.archive))
    except (OSError, PublicationReviewError, capture.CaptureError) as error:
        print(f"gpu publication review: {error}")
        return 1
    return _check_outputs(outputs) if args.check else _write_outputs(outputs)


if __name__ == "__main__":
    raise SystemExit(main())
