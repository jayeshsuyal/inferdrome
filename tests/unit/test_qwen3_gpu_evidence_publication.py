"""Publication and dashboard boundaries for the genuine Qwen3 A10 capture."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from inferdrome.capability_profiles import canonical_document_sha256
from scripts import review_qwen3_gpu_evidence_publication as publication
from scripts import run_qwen3_evidence_dashboard as dashboard

EVIDENCE_ROOT = (
    publication.REPOSITORY_ROOT
    / "evidence"
    / "gpu"
    / "2026-08-21-qwen3-8b-a10"
)


def _load(name: str) -> dict[str, object]:
    value = json.loads((EVIDENCE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_committed_qwen3_publication_records_cross_bind() -> None:
    review = _load("publication-review.json")
    operational = _load("operational-summary.json")
    handoff = _load("handoff-manifest.json")

    assert publication.validate_publication_review(review)
    assert publication.validate_operational_summary(operational)
    assert publication.validate_handoff_manifest(handoff)
    assert canonical_document_sha256(review) == (
        publication.PUBLICATION_REVIEW_SHA256
    )
    assert canonical_document_sha256(operational) == (
        publication.OPERATIONAL_SUMMARY_SHA256
    )
    assert canonical_document_sha256(handoff) == (
        publication.HANDOFF_MANIFEST_SHA256
    )
    assert handoff["publication_review"]["sha256"] == (
        canonical_document_sha256(review)
    )
    assert handoff["operational_completion"]["sha256"] == (
        canonical_document_sha256(operational)
    )
    assert publication.check_committed_records() == 0


@pytest.mark.parametrize(
    ("name", "validator"),
    [
        ("publication-review.json", publication.validate_publication_review),
        ("operational-summary.json", publication.validate_operational_summary),
        ("handoff-manifest.json", publication.validate_handoff_manifest),
    ],
)
def test_qwen3_record_validators_reject_unreviewed_fields(
    name: str,
    validator: object,
) -> None:
    mutated = copy.deepcopy(_load(name))
    mutated["unreviewed_field"] = True

    assert callable(validator)
    assert validator(mutated) is False


def test_qwen3_handoff_preserves_claim_boundaries() -> None:
    handoff = _load("handoff-manifest.json")

    assert handoff["acceptance_boundary"] == {
        "capture_kind": "BOUNDED_RUNTIME_CAPABILITY_SPIKE",
        "inferdrome_acceptance_verdict": None,
        "publication_state": "OBSERVATION_ONLY_PENDING_REVIEW",
        "statement": (
            "This capture proves one bounded runtime observation. It does not "
            "assign PASS, FAIL, or NOT_PROVEN and is not a cross-GPU result."
        ),
    }
    assert handoff["runtime_capability"]["hardware_attestation"] is False
    assert handoff["runtime_capability"]["spike_outcome"] == "SPIKE_SUCCEEDED"
    assert handoff["contract_binding"]["chronology"] == "RETROSPECTIVE"
    assert handoff["contract_binding"]["producer_exitspec_contract_digest"] is None
    assert handoff["fixture_delivery"]["publication_state"] == (
        "BLOCKED_PENDING_OWNER_APPROVAL"
    )


def test_qwen3_handoff_pins_independently_recalculated_measurements() -> None:
    handoff = _load("handoff-manifest.json")
    run = handoff["run"]

    assert run["request_population"] == {
        "failed_requests": 0,
        "measured_requests": 96,
        "successful_requests": 96,
        "ttft_samples": 96,
    }
    assert run["summary_measurements"]["ttft_ns"] == {
        "definition_id": "vllm_first_choices_event_v0_26",
        "p50": 127_123_958,
        "p95": 242_426_174,
        "p99": 244_030_050,
        "population": "successful_measured_requests_with_observed_ttft",
        "quantile_method": "nearest_rank_v1",
    }
    assert run["summary_measurements"]["output_token_throughput_per_s"] == (
        "28.870215"
    )


def test_qwen3_public_summary_omits_provider_endpoint_and_instance_identity() -> None:
    operational = _load("operational-summary.json")
    rendered = json.dumps(operational, sort_keys=True)

    assert "147.224.47.155" not in rendered
    assert "147-224-47-155" not in rendered
    assert "c2f68be244214643a13c1489d4c92fb1" not in rendered
    assert operational["evidence_kind"] == (
        "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
    )
    assert operational["cost_observation"]["estimated_cost_usd"] == "0.348463"


def test_qwen3_publication_review_is_external_only_without_detector_rejection() -> None:
    review = _load("publication-review.json")

    assert review["publication_status"] == "EXTERNAL_ONLY"
    assert review["detector_results"]["secret_shaped_values"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert review["detector_results"]["personal_or_customer_data"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert "OWNER_PUBLICATION_APPROVAL_REQUIRED" in review["decision_reasons"]
    assert "REPOSITORY_LICENSE_UNSELECTED" in review["decision_reasons"]


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("archive", "sha256"), "sha256:" + "0" * 64),
        (("run", "request_population", "successful_requests"), 95),
        (("runtime_capability", "hardware_attestation"), True),
        (("contract_binding", "chronology"), "PROSPECTIVE"),
        (("fixture_delivery", "publication_state"), "APPROVED_PUBLIC"),
    ],
)
def test_qwen3_handoff_anchor_mutations_fail_closed(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    mutated = copy.deepcopy(_load("handoff-manifest.json"))
    selected: dict[str, object] = mutated
    for key in path[:-1]:
        child = selected[key]
        assert isinstance(child, dict)
        selected = child
    selected[path[-1]] = replacement

    assert publication.validate_handoff_manifest(mutated) is False


def test_dashboard_command_is_no_shell_and_points_at_exact_runs_root(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "capture" / "runs"
    command = dashboard.dashboard_command(
        runs_root,
        port=8791,
        open_browser=True,
    )

    assert command == [
        dashboard.sys.executable,
        "-m",
        "inferdrome",
        "dashboard",
        "--runs-root",
        str(runs_root),
        "--port",
        "8791",
        "--open",
    ]


def test_dashboard_rejects_stale_publication_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "REPOSITORY_ROOT", tmp_path)
    path = tmp_path / "evidence" / "handoff.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stale")

    with pytest.raises(
        dashboard.Qwen3DashboardError,
        match="reviewed publication metadata is stale or missing",
    ):
        dashboard._require_published_outputs({path: b"expected"})
