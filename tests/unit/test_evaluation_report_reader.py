"""Retained report validation copies reducer values and rejects contradictions."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.report_reader import (
    MAX_REPORT_BYTES,
    CacheReport,
    StudyReport,
    load_evaluation_report_bytes,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.evaluation_dashboard_support import write_cache_report, write_study_report


def encoded(report: dict[str, Any]) -> bytes:
    return canonical_json_bytes(report) + b"\n"


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    root = tmp_path_factory.mktemp("evaluation-reader")
    values = {}
    for variant in (
        "complete",
        "partial",
        "cancelled",
        "no-measurements",
        "warmup-failed",
        "censored",
    ):
        _, values["study-" + variant] = write_study_report(
            root / ("study-" + variant), variant=variant
        )
    _, values["study-healthy"] = write_study_report(
        root / "healthy", scenario="HEALTHY"
    )
    for variant in (
        "complete",
        "all-failure",
        "cancelled",
        "aborted",
        "missing",
        "invalid",
        "declarations",
        "no-measurements",
        "drain",
    ):
        _, values["cache-" + variant] = write_cache_report(
            root / ("cache-" + variant), variant=variant
        )
    for count in (4, 8):
        _, values[f"cache-{count}"] = write_cache_report(
            root / f"cache-{count}", block_count=count
        )
    return values


@pytest.mark.parametrize(
    "label",
    [
        "study-complete",
        "study-partial",
        "study-cancelled",
        "study-no-measurements",
        "study-warmup-failed",
        "study-censored",
        "study-healthy",
        "cache-complete",
        "cache-all-failure",
        "cache-cancelled",
        "cache-aborted",
        "cache-missing",
        "cache-invalid",
        "cache-declarations",
        "cache-no-measurements",
        "cache-drain",
        "cache-4",
        "cache-8",
    ],
)
def test_reader_preserves_complete_authoritative_bytes(
    reports: dict, label: str
) -> None:
    raw = reports[label]
    kind = "STUDY" if label.startswith("study") else "PREFIX_CACHE"
    parsed = load_evaluation_report_bytes(encoded(raw), kind=kind)
    assert isinstance(parsed, StudyReport if kind == "STUDY" else CacheReport)
    assert parsed.model_dump(mode="json") == raw
    assert canonical_json_bytes(parsed.model_dump(mode="json")) + b"\n" == encoded(raw)


def test_exact_synthetic_values_and_trust_limits(reports: dict) -> None:
    parsed = load_evaluation_report_bytes(
        encoded(reports["cache-complete"]), kind="PREFIX_CACHE"
    )
    assert isinstance(parsed, CacheReport)
    assert parsed.coverage.planned_cells == 4
    assert parsed.coverage.returned_records == 16
    assert parsed.evidence_class == "SYNTHETIC_ONLY"
    assert parsed.evidence_eligible is False
    assert (
        parsed.runtime_verification
        == parsed.cache_treatment_attribution
        == "UNVERIFIED"
    )
    assert parsed.workload_verification == "SYNTHETIC_TOKENIZER"
    assert {
        row.condition: row.population.slo_good_count
        for row in parsed.cells
        if row.population
    } == {"S0": 1, "S1": 4, "U0": 1, "U1": 2}
    assert [
        row.mean_goodput_difference_rps.decimal
        for row in parsed.contrasts
        if row.mean_goodput_difference_rps
    ] == ["33.333333", "11.111111", "22.222222"]
    assert all(row.interval_rps is None for row in parsed.contrasts)
    assert parsed.prefix_potential is not None
    assert all(
        row.claim == "TOKEN_PREFIX_POTENTIAL_NOT_CACHE_HITS"
        for row in parsed.prefix_potential
    )


def test_missing_zero_cancelled_and_declarations_remain_distinct(reports: dict) -> None:
    empty = load_evaluation_report_bytes(
        encoded(reports["study-no-measurements"]), kind="STUDY"
    )
    assert isinstance(empty, StudyReport)
    assert empty.evidence_class == "LOCAL_MEASUREMENT_ONLY"
    assert empty.coverage.returned_trials == 0 and empty.status == "ABORTED"
    zero = load_evaluation_report_bytes(
        encoded(reports["cache-all-failure"]), kind="PREFIX_CACHE"
    )
    assert isinstance(zero, CacheReport)
    assert zero.status == "COMPLETED" and zero.comparison_status == "AVAILABLE"
    assert all(
        row.population is not None
        and row.population.slo_goodput_rps.decimal == "0.000000"
        for row in zero.cells
    )
    missing = load_evaluation_report_bytes(
        encoded(reports["cache-no-measurements"]), kind="PREFIX_CACHE"
    )
    assert isinstance(missing, CacheReport)
    assert missing.coverage.returned_records == 0 and missing.status == "INCOMPLETE"
    assert all(row.population is None for row in missing.cells)
    for name in ("cancelled", "invalid", "declarations"):
        parsed = load_evaluation_report_bytes(
            encoded(reports["cache-" + name]), kind="PREFIX_CACHE"
        )
        assert isinstance(parsed, CacheReport)
        assert parsed.comparison_status != "AVAILABLE"
        assert all(row.mean_goodput_difference_rps is None for row in parsed.contrasts)
        assert (parsed.status == "COMPLETED") == (name == "declarations")


def test_recovery_preserves_observed_zero_and_censored_null(reports: dict) -> None:
    parsed = load_evaluation_report_bytes(
        encoded(reports["study-censored"]), kind="STUDY"
    )
    assert isinstance(parsed, StudyReport)
    for trial in parsed.trials:
        assert trial.recovery.actual_restore_ns == 100_000_000
        assert trial.recovery.intervals.publication.duration_ns == 0
        assert trial.recovery.intervals.publication.status == "OBSERVED"
        interval = trial.recovery.intervals.dispatch
        assert interval.duration_ns is None
        assert interval.status == (
            "NOT_APPLICABLE"
            if trial.policy_id == "evaluation_round_robin_v1"
            else "UNOBSERVED_OR_CENSORED"
        )


def mutate(raw: dict, path: tuple, value: Any) -> dict:
    result = deepcopy(raw)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    return result


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        ("study-complete", ("schema_version",), "unsupported"),
        ("study-complete", ("evidence_eligible",), True),
        ("study-complete", ("evidence_eligible",), 0),
        ("study-complete", ("runtime_identity",), "VERIFIED_GPU"),
        (
            "study-complete",
            ("preparation", "model_warmup_reference"),
            "/private/operator-secret",
        ),
        ("study-complete", ("coverage", "planned_trials"), 8),
        ("study-complete", ("coverage", "returned_trials"), 3),
        ("study-complete", ("coverage", "completed_trial_results"), 0),
        ("study-complete", ("coverage", "foreground", "returned_records"), 0),
        ("study-complete", ("duplicate_payload_hash_groups",), 64),
        ("study-complete", ("study_elapsed_ns",), 0),
        ("study-complete", ("reason",), "CANCELLED"),
        ("study-partial", ("status",), "COMPLETED"),
        (
            "study-partial",
            ("comparative_headline",),
            "DESCRIPTIVE_UNCALIBRATED_REHEARSAL",
        ),
        (
            "study-partial",
            ("strata", 0, "paired_contrasts", 0, "interval_status"),
            "AVAILABLE",
        ),
        ("study-complete", ("trials", 0, "trial_id"), "trial-0001"),
        ("study-complete", ("trials", 0, "scenario"), "HEALTHY"),
        ("study-complete", ("trials", 0, "foreground_offered_count"), 7),
        ("study-complete", ("trials", 0, "window_end_ns"), 99),
        ("study-complete", ("trials", 0, "first_content_slo_ns"), 60_000_000_000),
        ("study-complete", ("trials", 0, "foreground", "successful_count"), 0),
        (
            "study-complete",
            ("trials", 0, "foreground", "outcomes", "SUCCESS", "count"),
            0,
        ),
        ("study-complete", ("trials", 0, "foreground", "slo_good_count"), 100),
        (
            "study-complete",
            ("trials", 0, "foreground", "slo_success_fraction", "decimal"),
            "0.123456",
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "slo_goodput_rps", "numerator"),
            1,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "slo_goodput_rps", "denominator"),
            0,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "offered_rate_rps", "denominator"),
            1,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "dispatch_rate_rps", "decimal"),
            "999.000000",
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "successful_completions_during_drain"),
            99,
        ),
        ("study-complete", ("trials", 0, "foreground", "arrival_observed_count"), 0),
        ("study-complete", ("trials", 0, "foreground", "dispatched_count"), 0),
        ("study-complete", ("trials", 0, "foreground", "peak_active"), 64),
        ("study-complete", ("trials", 0, "foreground", "cancelled"), 1),
        ("study-complete", ("trials", 0, "foreground", "slo_good_count"), True),
        (
            "study-complete",
            ("trials", 0, "foreground", "usage_success", "missing_count"),
            99,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "usage_success", "prompt_tokens_total_decimal"),
            "9" * 32,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "successful_latency_ns", "count"),
            0,
        ),
        (
            "study-complete",
            (
                "trials",
                0,
                "foreground",
                "successful_latency_ns",
                "scheduled_to_terminal",
                "p99",
            ),
            0,
        ),
        (
            "study-complete",
            ("trials", 0, "foreground", "dispatch_lag_ns", "p99_status"),
            "DESCRIPTIVE_ONLY",
        ),
        ("study-complete", ("trials", 0, "foreground", "dispatch_lag_ns", "count"), 99),
        (
            "study-complete",
            (
                "trials",
                0,
                "foreground",
                "partial_timing_by_outcome",
                "HTTP_ERROR",
                "offered_count",
            ),
            99,
        ),
        (
            "study-complete",
            ("trials", 0, "recovery", "actual_restore_ns"),
            9_000_000_000,
        ),
        (
            "study-complete",
            (
                "trials",
                0,
                "recovery",
                "intervals",
                "publication",
                "observation_horizon_ns",
            ),
            0,
        ),
        (
            "study-censored",
            ("trials", 1, "recovery", "intervals", "dispatch", "duration_ns"),
            0,
        ),
        (
            "study-censored",
            ("trials", 1, "recovery", "intervals", "dispatch", "status"),
            "OBSERVED",
        ),
        ("study-complete", ("strata", 0, "policies", 0, "returned_trials"), 0),
        (
            "study-complete",
            ("strata", 0, "policies", 0, "slo_good_counts_by_trial"),
            [999],
        ),
        (
            "study-complete",
            ("strata", 0, "policies", 0, "mean_goodput_rps", "decimal"),
            "0.000000",
        ),
        ("study-complete", ("strata", 0, "complete_blocks"), 0),
        (
            "study-complete",
            ("strata", 0, "recovery_coverage", "publication", "observed_trials"),
            0,
        ),
        (
            "study-complete",
            ("strata", 0, "paired_contrasts", 0, "mean_goodput_difference_rps", "sign"),
            True,
        ),
        ("study-complete", ("strata", 0, "paired_contrasts", 0, "bootstrap_seed"), 999),
        ("study-complete", ("reporting", "bootstrap_resamples"), True),
        ("study-complete", ("limitations", 0), "private text"),
        ("cache-complete", ("coverage", "completed_cells"), 0),
        ("cache-complete", ("coverage", "planned_blocks"), True),
        ("cache-complete", ("coverage", "returned_records"), 0),
        ("cache-complete", ("status",), "INCOMPLETE"),
        ("cache-complete", ("sum_returned_owned_elapsed_ns",), 0),
        ("cache-complete", ("reporting", "percentile_ranks"), [True, 95]),
        ("cache-complete", ("planned_evidence_class",), "LOCAL_MEASUREMENT_ONLY"),
        ("cache-complete", ("evidence_classes",), []),
        ("cache-complete", ("cache_hits",), "OBSERVED"),
        ("cache-complete", ("external_preparation_elapsed_ns",), 0),
        ("cache-complete", ("cells", 0, "cell_id"), "cell-0001"),
        ("cache-complete", ("cells", 0, "prefix_enabled"), True),
        ("cache-complete", ("cells", 0, "condition"), "U0"),
        ("cache-complete", ("cells", 0, "planned_offers"), 5),
        ("cache-complete", ("cells", 0, "config_sha256"), "sha256:" + "a" * 64),
        ("cache-complete", ("cells", 0, "result_sha256"), None),
        ("cache-complete", ("cells", 0, "preparation_sha256"), None),
        ("cache-complete", ("cells", 0, "reason"), "MISSING_CELL_INPUT"),
        ("cache-complete", ("cells", 0, "cleanup"), "UNCONFIRMED"),
        ("cache-complete", ("cells", 0, "attribution_eligible"), False),
        (
            "cache-complete",
            ("cells", 0, "endpoint_assignment", 0, "actual_dispatches"),
            999,
        ),
        (
            "cache-complete",
            ("cells", 0, "reported_output_lengths", "success", "mean_decimal"),
            "99.000000",
        ),
        (
            "cache-complete",
            ("cells", 0, "reported_output_lengths", "success", "total_decimal"),
            "99",
        ),
        ("cache-complete", ("prefix_potential", 0, "extra_shared_prefix_blocks"), 99),
        ("cache-complete", ("prefix_potential", 0, "shared_document_lcp_tokens"), 0),
        (
            "cache-complete",
            ("prefix_potential", 0, "shared_complete_prefix_blocks"),
            999,
        ),
        ("cache-complete", ("prefix_potential", 0, "claim"), "CACHE_HITS"),
        (
            "cache-complete",
            ("contrasts", 0, "mean_goodput_difference_rps", "decimal"),
            "99.000000",
        ),
        ("cache-complete", ("contrasts", 0, "block_goodput_differences_rps"), []),
        ("cache-complete", ("comparison_block_ids",), []),
        (
            "cache-complete",
            ("output_length_differences", 0, "shared_mean_tokens_difference_decimal"),
            "99.000000",
        ),
        ("cache-declarations", ("comparison_status",), "AVAILABLE"),
        ("cache-cancelled", ("cells", 1, "status"), "COMPLETED"),
        ("cache-invalid", ("cells", 1, "attribution_reasons"), []),
        ("cache-invalid", ("cells", 1, "cleanup"), "NOT_STARTED"),
        ("cache-8", ("contrasts", 0, "interval_rps", "lower", "decimal"), "999.000000"),
        ("cache-4", ("low_replication",), False),
    ],
)
def test_rejects_inconsistent_closed_fields(
    reports: dict, label: str, path: tuple, value: Any
) -> None:
    raw = mutate(reports[label], path, value)
    with pytest.raises(
        EvaluationError,
        match=r"^evaluation report violates its bounded closed contract$",
    ):
        load_evaluation_report_bytes(
            encoded(raw), kind="STUDY" if label.startswith("study") else "PREFIX_CACHE"
        )


def dictionaries(value: Any, path: tuple = ()) -> list[tuple]:
    found = []
    if isinstance(value, dict):
        found.append(path)
        for key, child in value.items():
            found.extend(dictionaries(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(dictionaries(child, (*path, index)))
    return found


@pytest.mark.parametrize("kind", ["STUDY", "PREFIX_CACHE"])
def test_every_nested_object_is_closed(reports: dict, kind: str) -> None:
    original = reports["study-complete" if kind == "STUDY" else "cache-complete"]
    for path in dictionaries(original):
        raw = deepcopy(original)
        parent = raw
        for key in path:
            parent = parent[key]
        parent["private-unknown-field"] = "private-source-origin"
        with pytest.raises(EvaluationError) as caught:
            load_evaluation_report_bytes(encoded(raw), kind=kind)
        assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}",
        b"{}\n",
        b"null\n",
        b"[]\n",
        b"\xff\n",
        b'{"private":1,"private":1}\n',
        b'{"private":1.0}\n',
        b'{"private":NaN}\n',
        b'{"private":Infinity}\n',
        b'{"private":-Infinity}\n',
        b'{"private":1e999999}\n',
        b'{"private":12345678901234567}\n',
        b"[" * 33 + b"0" + b"]" * 33 + b"\n",
        b"[" + b"0," * 500_000 + b"0]\n",
        b" " * (MAX_REPORT_BYTES + 1),
    ],
)
def test_bounded_lexical_errors_are_sanitized(payload: bytes) -> None:
    with pytest.raises(
        EvaluationError,
        match=r"^evaluation report violates its bounded closed contract$",
    ):
        load_evaluation_report_bytes(payload, kind="STUDY")


def test_requires_exact_kind_canonical_bytes_and_one_final_lf(reports: dict) -> None:
    raw = reports["study-complete"]
    for content in (
        encoded(raw)[:-1],
        encoded(raw) + b"\n",
        b" " + encoded(raw),
        json.dumps(raw, indent=2).encode() + b"\n",
    ):
        with pytest.raises(EvaluationError):
            load_evaluation_report_bytes(content, kind="STUDY")
    with pytest.raises(EvaluationError):
        load_evaluation_report_bytes(encoded(raw), kind="PREFIX_CACHE")
    with pytest.raises(EvaluationError):
        load_evaluation_report_bytes(encoded(raw), kind="UNKNOWN")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "path", "count"),
    [
        ("study-complete", ("trials",), 257),
        ("study-complete", ("strata",), 65),
        ("cache-complete", ("cells",), 33),
        ("cache-complete", ("prefix_potential",), 9),
        ("cache-complete", ("output_length_differences",), 9),
    ],
)
def test_cardinality_limits_withhold_whole_report(
    reports: dict, label: str, path: tuple, count: int
) -> None:
    raw = deepcopy(reports[label])
    raw[path[0]] = [raw[path[0]][0]] * count
    with pytest.raises(EvaluationError):
        load_evaluation_report_bytes(
            encoded(raw), kind="STUDY" if label.startswith("study") else "PREFIX_CACHE"
        )


def test_unknown_tokenization_report_without_measurements_is_supported(
    tmp_path: Path,
) -> None:
    from inferdrome.evaluation.cache_config import compile_cache_experiment
    from inferdrome.evaluation.cache_report import report_cache_experiment
    from tests.unit.test_evaluation_cache_config import cache_payload, load

    plan = compile_cache_experiment(load(cache_payload()))
    assert plan.verification_status == "UNAVAILABLE"
    raw = report_cache_experiment(plan, {}, tmp_path / "unavailable")
    parsed = load_evaluation_report_bytes(encoded(raw), kind="PREFIX_CACHE")
    assert isinstance(parsed, CacheReport)
    assert (
        parsed.prefix_potential is None
        and parsed.workload_verification == "UNAVAILABLE"
    )
    assert (
        parsed.coverage.returned_records == 0
        and parsed.comparison_status == "SUPPRESSED_INCOMPLETE"
    )


def test_reader_performs_no_source_reads_writes_execution_or_tokenization(
    reports: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import socket

    from inferdrome.evaluation import cache, cache_report, cache_workload, runner, study

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("report reader attempted an external action")

    content = encoded(reports["cache-complete"])
    for obj, name in (
        (builtins, "open"),
        (Path, "open"),
        (socket, "socket"),
        (cache, "run_cache_cell"),
        (cache_report, "report_cache_experiment"),
        (cache_workload, "verify_cache_workloads"),
        (runner, "run_evaluation"),
        (study, "run_study"),
    ):
        monkeypatch.setattr(obj, name, forbidden)
    parsed = load_evaluation_report_bytes(content, kind="PREFIX_CACHE")
    assert isinstance(parsed, CacheReport)
