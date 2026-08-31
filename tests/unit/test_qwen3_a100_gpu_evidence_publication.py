"""Publication and dashboard boundaries for the genuine A100 SXM4 capture."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from inferdrome.capability_profiles import canonical_document_sha256
from scripts import review_qwen3_a100_sxm4_evidence as publication
from scripts import run_qwen3_a100_sxm4_evidence_dashboard as dashboard

EVIDENCE_ROOT = (
    publication.REPOSITORY_ROOT
    / "evidence"
    / "gpu"
    / "2026-08-23-qwen3-8b-a100-sxm4"
)


def _load(name: str) -> dict[str, object]:
    value = json.loads((EVIDENCE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_committed_a100_publication_records_cross_bind() -> None:
    review = _load("publication-review.json")
    operational = _load("operational-summary.json")
    handoff = _load("handoff-manifest.json")

    assert publication.validate_publication_review(review)
    assert publication.validate_operational_summary(operational)
    assert publication.validate_handoff_manifest(handoff)
    assert canonical_document_sha256(review) == publication.PUBLICATION_REVIEW_SHA256
    assert canonical_document_sha256(operational) == (
        publication.OPERATIONAL_SUMMARY_SHA256
    )
    assert canonical_document_sha256(handoff) == publication.HANDOFF_MANIFEST_SHA256
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
def test_a100_record_validators_reject_unreviewed_fields(
    name: str,
    validator: object,
) -> None:
    mutated = copy.deepcopy(_load(name))
    mutated["unreviewed_field"] = True

    assert callable(validator)
    assert validator(mutated) is False


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
def test_a100_handoff_anchor_mutations_fail_closed(
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


def test_a100_operational_summary_omits_provider_instance_identity() -> None:
    operational = _load("operational-summary.json")
    rendered = json.dumps(operational, sort_keys=True)

    assert "129.146.24.230" not in rendered
    assert "26a82071c4274d9ab278b47ec693debe" not in rendered
    assert operational["cost_observation"]["estimated_cost_usd"] == "0.432165"
    assert operational["evidence_kind"] == (
        "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
    )


def test_a100_publication_review_is_external_only() -> None:
    review = _load("publication-review.json")

    assert (publication.REPOSITORY_ROOT / "LICENSE").is_file()
    assert publication.REPOSITORY_LICENSE_PRESENT_AT_REVIEW is False
    assert review["publication_status"] == "EXTERNAL_ONLY"
    assert review["raw_archive_modified"] is False
    assert review["detector_results"]["secret_shaped_values"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert review["detector_results"]["personal_or_customer_data"]["status"] == (
        "NO_DETECTOR_MATCHES"
    )
    assert "OWNER_PUBLICATION_APPROVAL_REQUIRED" in review["decision_reasons"]


def test_a100_dashboard_command_is_no_shell_and_uses_verified_runs_root(
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


def test_a100_dashboard_rejects_stale_publication_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "REPOSITORY_ROOT", tmp_path)
    path = tmp_path / "evidence" / "handoff.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stale")

    with pytest.raises(
        dashboard.A100DashboardError,
        match="reviewed publication metadata is stale or missing",
    ):
        dashboard._require_published_outputs({path: b"expected"})
