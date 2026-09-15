"""Canonical PR3 output compatibility through the pure SLO summary extraction."""

import pytest

from inferdrome.evaluation.study_config import compile_study
from inferdrome.evaluation.study_report import summarize_population, summarize_trial
from inferdrome.evaluation.study_validation import validate_trial_result
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_study_config import load, study_payload
from tests.unit.test_evaluation_study_report import measure


# Captured from the unextracted PR3 summarize_trial at guard commit 7d68d5e.
@pytest.mark.parametrize(
    ("scenario", "mode", "digest"),
    [
        (
            "HEALTHY",
            "COMPLETED",
            "9061a26ab47f3d2cabaa89164e4afe1c0bcffcbf03a9284bb8ab63929139cf0b",
        ),
        (
            "HEALTHY",
            "WARMUP_FAILED",
            "fcd9fcb88219412bbea2ac51c5fe55acb221332d502a677f607690de47c8f412",
        ),
        (
            "HEALTHY",
            "CANCELLED_BEFORE_START",
            "653c625ebdf2ff1ade5625cd315f323faa1597ca84264e2e9d014f5791684e37",
        ),
        (
            "HEALTHY",
            "CANCELLED_DURING_FREEZE",
            "aa55017a4378884a8c32d92351f32edff8a5c22c72f350ccef0a5ba81a88d498",
        ),
        (
            "STALE_LOAD",
            "COMPLETED",
            "60d9c295a24c6ec4ebaff28648e67bf229897ad86595d532bafd377c640df9ea",
        ),
        (
            "STALE_LOAD",
            "WARMUP_FAILED",
            "bb24eb307f646e19423ea9d13d9390ac8705b5feb273131c5f436da2da8862dd",
        ),
        (
            "STALE_LOAD",
            "CANCELLED_BEFORE_START",
            "83251a352c0bb696232cb701cdcc7d413fe4e131a430edbc7a329ce438a9a476",
        ),
        (
            "STALE_LOAD",
            "CANCELLED_DURING_FREEZE",
            "4e6ea1c65bbd89def49356018eef84ce9d7b35ba0f65823d7ca9591665c9a01a",
        ),
    ],
)
def test_extraction_preserves_canonical_pr3_summary_bytes(
    scenario: str, mode: str, digest: str
) -> None:
    trial = compile_study(load(study_payload(scenario))).trials[0]
    result = validate_trial_result(measure(trial, mode), trial.config)
    summary = summarize_trial(trial, result)
    assert sha256_digest(canonical_json_bytes(summary)) == "sha256:" + digest
    assert summary["foreground"] == summarize_population(
        trial.config.foreground,
        result.foreground,
        window_start_ns=trial.window_start_ns,
        window_end_ns=trial.window_end_ns,
        first_content_slo_ns=trial.first_content_slo_ns,
        completion_slo_ns=trial.completion_slo_ns,
    )
