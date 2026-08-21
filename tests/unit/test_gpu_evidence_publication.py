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


def test_handoff_binds_profile_review_and_no_acceptance_verdict() -> None:
    handoff = _load("handoff-manifest.json")
    review = _load("publication-review.json")
    profile = json.loads(publication.PROFILE_PATH.read_text(encoding="utf-8"))
    local_schema = json.loads(
        publication.LOCAL_PROOF_SCHEMA_PATH.read_text(encoding="utf-8")
    )

    assert publication.validate_handoff_manifest(handoff)
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


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("archive", "sha256"), f"sha256:{'0' * 64}"),
        (("run", "run_id"), "run-fedcba9876543210fedcba9876543210"),
        (("run", "ttft", "independently_expected_value"), 14_797_214),
        (("contract_binding", "producer_exitspec_contract_digest"), "invented"),
        (("acceptance_boundary", "inferdrome_acceptance_verdict"), "PASS"),
        (("fixture_delivery", "publication_state"), "PUBLISHED"),
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
    mutated["acceptance_verdict"] = "PASS"

    assert publication.validate_handoff_manifest(mutated) is False
