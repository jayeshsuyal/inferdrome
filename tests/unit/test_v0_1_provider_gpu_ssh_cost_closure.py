"""Keep the v0.1 provider-evidence closure inside its historical boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import review_qwen3_a100_sxm4_evidence as a100_publication
from scripts import review_qwen3_gpu_evidence_publication as a10_publication

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPOSITORY_ROOT / "docs/reviews/V0_1_PROVIDER_GPU_SSH_COST_CLOSURE.md"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_closure_report_uses_the_existing_genuine_gpu_records() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    a10_root = REPOSITORY_ROOT / "evidence/gpu/2026-08-21-qwen3-8b-a10"
    a100_root = REPOSITORY_ROOT / "evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4"
    a10_handoff = _load(a10_root / "handoff-manifest.json")
    a10_operational = _load(a10_root / "operational-summary.json")
    a100_handoff = _load(a100_root / "handoff-manifest.json")
    a100_operational = _load(a100_root / "operational-summary.json")

    assert a10_publication.check_committed_records() == 0
    assert a100_publication.check_committed_records() == 0
    assert "EXISTING_TRACKED_EVIDENCE_SUFFICIENT_NO_NEW_PAID_RUN_REQUIRED" in report
    assert a10_handoff["runtime_capability"]["expected_gpu_model"] == "NVIDIA A10"
    assert a100_handoff["runtime_capability"]["expected_gpu_model"] == (
        "NVIDIA A100-SXM4-40GB"
    )
    assert a10_operational["provider"]["termination_final_status"] == "absent"
    assert a100_operational["provider"]["termination_final_status"] == "absent"
    assert a10_operational["semantic_verification"] == (
        "VALID_AFTER_PROVIDER_TERMINATION"
    )
    assert a100_operational["semantic_verification"] == (
        "VALID_AFTER_PROVIDER_TERMINATION"
    )
    assert a10_operational["cost_observation"] == {
        "billing_window_seconds": "972.454467",
        "estimated_cost_usd": "0.348463",
        "hourly_rate_usd": "1.29",
        "max_cost_usd": "0.75",
        "statement": (
            "Controller-observed estimate only; provider billing granularity and "
            "the final invoice remain external."
        ),
    }
    assert a100_operational["cost_observation"] == {
        "billing_window_seconds": "781.806878",
        "estimated_cost_usd": "0.432165",
        "hourly_rate_usd": "1.99",
        "max_cost_usd": "1.25",
        "statement": (
            "Controller-observed estimate only; provider billing granularity and "
            "the final invoice remain external."
        ),
    }


def test_closure_report_preserves_all_live_provider_claim_limits() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    a10_root = REPOSITORY_ROOT / "evidence/gpu/2026-08-21-qwen3-8b-a10"
    a100_root = REPOSITORY_ROOT / "evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4"

    for root in (a10_root, a100_root):
        handoff = _load(root / "handoff-manifest.json")
        operational = _load(root / "operational-summary.json")

        assert handoff["acceptance_boundary"]["inferdrome_acceptance_verdict"] is None
        assert handoff["contract_binding"]["chronology"] == "RETROSPECTIVE"
        assert handoff["contract_binding"]["producer_exitspec_contract_digest"] is None
        assert handoff["fixture_delivery"]["publication_state"] == (
            "BLOCKED_PENDING_OWNER_APPROVAL"
        )
        assert handoff["publication_review"]["publication_status"] == "EXTERNAL_ONLY"
        assert handoff["runtime_capability"]["hardware_attestation"] is False
        assert operational["evidence_kind"] == (
            "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        )

    for unsupported_claim in (
        "NO_CURRENT_PROVIDER_CAPACITY_OR_ACTIVE_INSTANCE_CLAIM",
        "NO_PROVIDER_HARDWARE_ATTESTATION",
        "NO_LIVE_SSH_OR_HOST_LEGITIMACY_VALIDATION",
        "NO_CURRENT_BILLING_OR_FINAL_INVOICE_CLAIM",
        "NO_CURRENT_TERMINATION_OR_ACCOUNT_CLEANUP_CLAIM",
        "NO_RAW_ARCHIVE_PUBLICATION_OR_OWNER_PRIVACY_LICENSE_APPROVAL",
        "NO_EXITSPEC_PASS_FAIL_NOT_PROVEN_OR_RECEIPT_CLAIM",
        "NO_RELEASE_AUTHORIZATION",
    ):
        assert unsupported_claim in report
