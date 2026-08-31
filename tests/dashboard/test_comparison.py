"""Behavioral contract for conservative, neutral run comparisons."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from inferdrome.dashboard.comparison import compare_runs
from inferdrome.dashboard.models import RunDetail
from inferdrome.dashboard.projection import load_run_detail

BASELINE_RUN_ID = "run-11111111111111111111111111111111"
SAME_CONTEXT_RUN_ID = "run-22222222222222222222222222222222"
CHANGED_CONTEXT_RUN_ID = "run-33333333333333333333333333333333"
CONTRACT_DIGEST = f"sha256:{'a' * 64}"


def _detail(bundle_path: Path) -> RunDetail:
    return load_run_detail(bundle_path)


def _authoritative_detail(
    bundle_path: Path,
    *,
    contract_digest: str = CONTRACT_DIGEST,
) -> RunDetail:
    detail = _detail(bundle_path)
    return detail.model_copy(
        update={
            "summary": detail.summary.model_copy(
                update={"evidence_eligibility": "CUSTOMER_ELIGIBLE"}
            ),
            "verification": detail.verification.model_copy(
                update={"evidence_eligibility": "CUSTOMER_ELIGIBLE"}
            ),
            "digests": detail.digests.model_copy(
                update={"exitspec_contract_digest": contract_digest}
            ),
        }
    )


def _assert_neutral_language(payload: object) -> None:
    serialized = json.dumps(payload, sort_keys=True).lower()
    assert "better" not in serialized
    assert "worse" not in serialized


def test_same_measurement_contract_is_comparable_with_neutral_zero_deltas(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "comparable-runs"
    baseline = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate = run_fake_bundle(runs_root, SAME_CONTEXT_RUN_ID)

    comparison = as_public_json(
        compare_runs(
            _authoritative_detail(baseline.sealed_bundle.path),
            _authoritative_detail(candidate.sealed_bundle.path),
        )
    )

    assert comparison["status"] == "COMPARABLE"
    assert comparison["reasons"]
    assert comparison["context_changes"] == []
    assert comparison["metric_deltas"]
    assert all(
        Decimal(str(item["absolute_delta"])) == 0
        for item in comparison["metric_deltas"]
    )
    assert comparison["directionality"] == "NEUTRAL"
    _assert_neutral_language(comparison)


def test_non_measurement_source_change_is_comparable_with_context_changes(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "context-change-runs"
    baseline = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate = run_fake_bundle(
        runs_root,
        CHANGED_CONTEXT_RUN_ID,
        model="inferdrome/alternate-fake-model",
    )

    comparison = as_public_json(
        compare_runs(
            _authoritative_detail(baseline.sealed_bundle.path),
            _authoritative_detail(candidate.sealed_bundle.path),
        )
    )

    assert comparison["status"] == "COMPARABLE_WITH_CONTEXT_CHANGES"
    assert comparison["context_changes"]
    assert comparison["metric_deltas"]
    assert comparison["directionality"] == "NEUTRAL"
    _assert_neutral_language(comparison)


def test_execution_limit_change_is_explicit_context_not_full_comparability(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "execution-context-change-runs"
    baseline = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate = run_fake_bundle(
        runs_root,
        CHANGED_CONTEXT_RUN_ID,
        max_runtime_seconds=61,
    )

    comparison = as_public_json(
        compare_runs(
            _authoritative_detail(baseline.sealed_bundle.path),
            _authoritative_detail(candidate.sealed_bundle.path),
        )
    )

    assert comparison["status"] == "COMPARABLE_WITH_CONTEXT_CHANGES"
    assert {
        change["key"] for change in comparison["context_changes"]
    } >= {"execution.max_runtime_seconds"}
    assert comparison["metric_deltas"]


def test_unexplained_execution_fingerprint_change_suppresses_deltas(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "fingerprint-guard-runs"
    baseline_result = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate_result = run_fake_bundle(runs_root, SAME_CONTEXT_RUN_ID)
    baseline = _authoritative_detail(baseline_result.sealed_bundle.path)
    candidate = _authoritative_detail(candidate_result.sealed_bundle.path)
    changed_contract = candidate.comparison_contract.model_copy(
        update={"execution_fingerprint": f"sha256:{'f' * 64}"}
    )
    candidate = candidate.model_copy(
        update={"comparison_contract": changed_contract}
    )

    comparison = as_public_json(compare_runs(baseline, candidate))

    assert comparison["status"] == "INCOMPARABLE"
    assert comparison["metric_deltas"] == []
    assert any(
        "execution fingerprints differ" in reason.lower()
        for reason in comparison["reasons"]
    )


def test_incompatible_runs_explain_rejection_and_suppress_all_deltas(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    sealed_vllm_bundle: Any,
    as_public_json: Callable[[object], Any],
) -> None:
    fake = run_fake_bundle(tmp_path / "incompatible-runs", BASELINE_RUN_ID)

    comparison = as_public_json(
        compare_runs(
            _detail(fake.sealed_bundle.path),
            _detail(sealed_vllm_bundle.sealed.path),
        )
    )

    assert comparison["status"] == "INCOMPARABLE"
    assert comparison["reasons"]
    assert comparison["metric_deltas"] == []
    assert comparison["directionality"] == "NEUTRAL"
    _assert_neutral_language(comparison)


@pytest.mark.parametrize("eligibility", ["SYNTHETIC_ONLY", "INELIGIBLE"])
def test_ineligible_evidence_is_incomparable_and_suppresses_deltas(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
    eligibility: str,
) -> None:
    runs_root = tmp_path / f"{eligibility.lower()}-runs"
    baseline = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate = run_fake_bundle(runs_root, SAME_CONTEXT_RUN_ID)
    details = tuple(
        _detail(result.sealed_bundle.path)
        for result in (baseline, candidate)
    )
    details = tuple(
        detail.model_copy(
            update={
                "summary": detail.summary.model_copy(
                    update={"evidence_eligibility": eligibility}
                ),
                "verification": detail.verification.model_copy(
                    update={"evidence_eligibility": eligibility}
                ),
            }
        )
        for detail in details
    )

    comparison = as_public_json(
        compare_runs(*details)
    )

    assert comparison["status"] == "INCOMPARABLE"
    assert comparison["metric_deltas"] == []
    assert any(
        "customer_eligible" in reason.lower()
        for reason in comparison["reasons"]
    )


def test_same_run_is_incomparable_even_when_authority_prerequisites_match(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    result = run_fake_bundle(tmp_path / "same-run", BASELINE_RUN_ID)
    detail = _authoritative_detail(result.sealed_bundle.path)

    comparison = as_public_json(compare_runs(detail, detail))

    assert comparison["status"] == "INCOMPARABLE"
    assert comparison["metric_deltas"] == []
    assert any("itself" in reason.lower() for reason in comparison["reasons"])


def test_cross_contract_evidence_is_incomparable_and_suppresses_deltas(
    tmp_path: Path,
    run_fake_bundle: Callable[..., Any],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "cross-contract-runs"
    baseline = run_fake_bundle(runs_root, BASELINE_RUN_ID)
    candidate = run_fake_bundle(runs_root, SAME_CONTEXT_RUN_ID)

    comparison = as_public_json(
        compare_runs(
            _authoritative_detail(
                baseline.sealed_bundle.path,
                contract_digest=f"sha256:{'a' * 64}",
            ),
            _authoritative_detail(
                candidate.sealed_bundle.path,
                contract_digest=f"sha256:{'b' * 64}",
            ),
        )
    )

    assert comparison["status"] == "INCOMPARABLE"
    assert comparison["metric_deltas"] == []
    assert any(
        "contract identities differ" in reason.lower()
        for reason in comparison["reasons"]
    )
