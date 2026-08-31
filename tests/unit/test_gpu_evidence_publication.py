"""Publication-review and handoff boundaries for the exact A10 capture."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.review_gpu_evidence_publication as publication
from inferdrome.capability_profiles import canonical_document_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "evidence" / "gpu" / "2026-08-20-a10"


def _load(name: str) -> dict[str, Any]:
    value = json.loads((EVIDENCE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_records(directory: Path) -> None:
    directory.mkdir(exist_ok=True)
    for name in ("publication-review.json", "handoff-manifest.json"):
        (directory / name).write_bytes((EVIDENCE_ROOT / name).read_bytes())


@pytest.mark.parametrize(
    ("rejected", "unresolved", "approved", "expected"),
    [
        (False, False, True, "APPROVED_PUBLIC"),
        (False, True, True, "EXTERNAL_ONLY"),
        (False, False, False, "EXTERNAL_ONLY"),
        (True, False, True, "REJECTED"),
    ],
)
def test_publication_decision_table_is_fail_closed(
    rejected: bool,
    unresolved: bool,
    approved: bool,
    expected: str,
) -> None:
    assert (
        publication.publication_status(
            rejected_findings=rejected,
            unresolved_publication_findings=unresolved,
            owner_publication_approved=approved,
        )
        == expected
    )


def test_exact_archive_review_remains_external_only() -> None:
    review = _load("publication-review.json")

    assert review["publication_status"] == "EXTERNAL_ONLY"
    assert review["archive"]["sha256"] == publication.ARCHIVE_SHA256
    assert review["archive"]["compressed_size_bytes"] == 689_272
    assert review["archive_integrity_and_safety"]["status"] == "PASS"
    assert review["archive_integrity_and_safety"]["isolated_verification"] is True
    assert review["detector_results"]["secret_shaped_values"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert review["detector_results"]["personal_or_customer_data"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert review["raw_archive_modified"] is False
    assert review["owner_publication_approval_required"] is True
    assert all(
        review["license_review"][name] is False
        for name in (
            "generated_output_license_resolved",
            "model_license_resolved",
            "repository_license_present",
            "vllm_license_resolved",
            "workload_publication_license_resolved",
        )
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("archive", "sha256"), f"sha256:{'0' * 64}"),
        (("owner_publication_approval_required",), False),
        (("raw_archive_modified",), True),
        (("license_review", "model_license_resolved"), True),
    ],
)
def test_publication_review_mutations_fail_closed(
    path: tuple[str, ...],
    value: Any,
) -> None:
    mutated = copy.deepcopy(_load("publication-review.json"))
    parent = mutated
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value

    assert publication.validate_publication_review(mutated) is False


def test_publication_review_rejects_integer_to_float_type_drift() -> None:
    mutated = copy.deepcopy(_load("publication-review.json"))
    mutated["archive"]["compressed_size_bytes"] = 689_272.0

    assert publication.validate_publication_review(mutated) is False


def test_handoff_binds_profile_review_and_no_acceptance_verdict() -> None:
    handoff = _load("handoff-manifest.json")
    review = _load("publication-review.json")
    profile = json.loads(publication.PROFILE_PATH.read_text(encoding="utf-8"))
    local_schema = json.loads(
        publication.LOCAL_PROOF_SCHEMA_PATH.read_text(encoding="utf-8")
    )

    assert publication.validate_publication_review(review)
    assert publication.validate_handoff_manifest(handoff)
    assert canonical_document_sha256(review) == (publication.PUBLICATION_REVIEW_SHA256)
    assert canonical_document_sha256(handoff) == publication.HANDOFF_MANIFEST_SHA256
    assert handoff["publication_review"]["sha256"] == (
        canonical_document_sha256(review)
    )
    assert handoff["capability_profile"]["managed_profile"]["sha256"] == (
        canonical_document_sha256(profile)
    )
    assert handoff["capability_profile"]["local_gpu_proof_schema"]["sha256"] == (
        canonical_document_sha256(local_schema)
    )
    assert handoff["contract_binding"]["producer_exitspec_contract_digest"] is None
    assert handoff["contract_binding"]["chronology"] == "RETROSPECTIVE"
    assert handoff["acceptance_boundary"]["inferdrome_acceptance_verdict"] is None
    assert handoff["rejection_facts"]["corrupted"]["rejection_class"] == (
        "INTEGRITY_MISMATCH"
    )
    assert handoff["rejection_facts"]["synthetic"]["rejection_class"] == (
        "EVIDENCE_INELIGIBLE"
    )
    assert publication.check_committed_records() == 0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("archive", "sha256"), f"sha256:{'0' * 64}"),
        (("run", "run_id"), "run-fedcba9876543210fedcba9876543210"),
        (("run", "ttft", "independently_expected_value"), 14_797_214),
        (("contract_binding", "producer_exitspec_contract_digest"), "invented"),
        (
            ("acceptance_boundary", "inferdrome_acceptance_verdict"),
            "UNAUTHORIZED_SENTINEL",
        ),
        (("fixture_delivery", "publication_state"), "PUBLISHED"),
        (("publication_review", "publication_status"), "APPROVED_PUBLIC"),
        (("publication_review", "owner_publication_approval_required"), False),
        (("run", "request_plan_digest"), f"sha256:{'0' * 64}"),
        (("run", "source_spec_digest"), f"sha256:{'0' * 64}"),
        (("contract_binding", "chronology"), "PROSPECTIVE"),
        (
            ("capability_profile", "managed_profile", "identity"),
            "unreviewed-profile",
        ),
    ],
)
def test_handoff_anchor_mutations_fail_closed(
    path: tuple[str, ...],
    value: Any,
) -> None:
    mutated = copy.deepcopy(_load("handoff-manifest.json"))
    parent = mutated
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value

    assert publication.validate_handoff_manifest(mutated) is False


def test_handoff_rejects_unknown_root_fields() -> None:
    mutated = copy.deepcopy(_load("handoff-manifest.json"))
    mutated["acceptance_verdict"] = "UNAUTHORIZED_SENTINEL"

    assert publication.validate_handoff_manifest(mutated) is False


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("publication-review.json", ("archive_integrity_and_safety",)),
        ("handoff-manifest.json", ("acceptance_boundary",)),
        ("handoff-manifest.json", ("contract_binding",)),
    ],
)
def test_record_validators_reject_unknown_nested_authority_fields(
    name: str,
    path: tuple[str, ...],
) -> None:
    mutated = copy.deepcopy(_load(name))
    parent = mutated
    for part in path:
        parent = parent[part]
    parent["unreviewed_authority"] = "UNAUTHORIZED_SENTINEL"

    validator = (
        publication.validate_publication_review
        if name == "publication-review.json"
        else publication.validate_handoff_manifest
    )
    assert validator(mutated) is False


@pytest.mark.parametrize(
    ("name", "needle", "replacement"),
    [
        (
            "publication-review.json",
            b'  "publication_status": "EXTERNAL_ONLY",',
            (
                b'  "publication_status": "UNAUTHORIZED_SENTINEL",\n'
                b'  "publication_status": "EXTERNAL_ONLY",'
            ),
        ),
        (
            "handoff-manifest.json",
            b'    "inferdrome_acceptance_verdict": null,',
            (
                b'    "inferdrome_acceptance_verdict": "UNAUTHORIZED_SENTINEL",\n'
                b'    "inferdrome_acceptance_verdict": null,'
            ),
        ),
    ],
)
def test_committed_record_gate_rejects_duplicate_keys(
    tmp_path: Path,
    name: str,
    needle: bytes,
    replacement: bytes,
) -> None:
    _write_records(tmp_path)
    path = tmp_path / name
    content = path.read_bytes()
    assert content.count(needle) == 1
    path.write_bytes(content.replace(needle, replacement, 1))

    assert publication.check_committed_records(tmp_path) == 1


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            b'    "compressed_size_bytes": 689272,',
            b'    "compressed_size_bytes": 689272.0,',
        ),
        (
            b'    "archive_license_file_count": 0,',
            b'    "archive_license_file_count": -0,',
        ),
    ],
    ids=["integer-to-float", "negative-zero"],
)
def test_committed_record_gate_rejects_noncanonical_numeric_lexemes(
    tmp_path: Path,
    needle: bytes,
    replacement: bytes,
) -> None:
    _write_records(tmp_path)
    path = tmp_path / "publication-review.json"
    content = path.read_bytes()
    assert content.count(needle) == 1
    path.write_bytes(content.replace(needle, replacement, 1))

    assert publication.check_committed_records(tmp_path) == 1


def test_committed_record_gate_rejects_self_consistent_state_mutation(
    tmp_path: Path,
) -> None:
    _write_records(tmp_path)
    review = _load("publication-review.json")
    handoff = _load("handoff-manifest.json")
    review["publication_status"] = "APPROVED_PUBLIC"
    handoff["publication_review"]["publication_status"] = "APPROVED_PUBLIC"
    handoff["publication_review"]["sha256"] = canonical_document_sha256(review)
    (tmp_path / "publication-review.json").write_text(
        json.dumps(review, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "handoff-manifest.json").write_text(
        json.dumps(handoff, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert publication.check_committed_records(tmp_path) == 1
