"""Offline integrity, all-offered metrics and paired block reporting guards."""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import FaultEvent, run_routing_fault
from inferdrome.evaluation.healthy import run_routing_healthy
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from inferdrome.evaluation.study_report import (
    _paired_contrasts,
    _quantiles,
    _signed_ratio,
    render_markdown,
    summarize_study,
    summarize_trial,
)
from inferdrome.evaluation.study_validation import (
    StudyValidationError,
    _observations,
    load_trial_result_bytes,
    validate_trial_result,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_faults import MS, clients, finish
from tests.unit.test_evaluation_runner import ManualClock, advance, settle
from tests.unit.test_evaluation_study_config import blocks, load, study_payload


def measure(trial: CompiledTrial, mode: str = "COMPLETED") -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        clock, stop = ManualClock(), asyncio.Event()
        owned = clients(clock)
        if mode == "WARMUP_FAILED":
            owned[2].mode = "HTTP_ERROR"
        if mode == "CANCELLED_BEFORE_START":
            stop.set()
        if isinstance(trial.config, RoutingFaultConfig):
            work = run_routing_fault(trial.config, *owned, clock=clock, stop=stop)
        else:
            work = run_routing_healthy(
                trial.config, owned[0], owned[2], owned[3], clock=clock, stop=stop
            )
        task = asyncio.create_task(work)
        if mode == "CANCELLED_DURING_FREEZE":
            await settle()
            for at in range(5, 66, 5):
                await advance(clock, at * MS)
            stop.set()
            await settle()
        else:
            await finish(clock)
        result = await task
        return json.loads(canonical_json_bytes(result.to_dict()))

    return asyncio.run(scenario())


@pytest.fixture(scope="module")
def rehearsal() -> tuple[Any, dict[str, dict[str, Any]]]:
    plan = compile_study(load(study_payload("STALE_LOAD")))
    return plan, {trial.policy_id: measure(trial) for trial in plan.trials}


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
@pytest.mark.parametrize(
    "mode",
    ["COMPLETED", "WARMUP_FAILED", "CANCELLED_BEFORE_START", "CANCELLED_DURING_FREEZE"],
)
def test_accept_real_controller_shapes_with_complete_terminal_populations(
    scenario: str,
    mode: str,
) -> None:
    trial = compile_study(load(study_payload(scenario))).trials[0]
    raw = measure(trial, mode)
    encoded = canonical_json_bytes(raw) + b"\n"
    result = load_trial_result_bytes(encoded, trial.config)
    assert result.result_sha256 == sha256_digest(encoded)
    assert result.status == ("CANCELLED" if mode.startswith("CANCELLED") else mode)
    assert len(result.foreground.records) == len(trial.config.foreground.offers)
    assert (
        result.background is None
        if scenario == "HEALTHY"
        else result.background is not None
    )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("config_sha256",), "sha256:" + "0" * 64),
        (("policy_id",), "private-policy-error"),
        (("schema_version",), "unknown"),
        (("evidence_eligible",), True),
        (("evidence_class",), "PRIVATE_RAW"),
        (("elapsed_ns",), 0),
        (("foreground", "config_sha256"), "sha256:" + "0" * 64),
        (("foreground", "model_sha256"), "sha256:" + "0" * 64),
        (("foreground", "evidence_class"), "SYNTHETIC_ONLY"),
        (("foreground", "bounds", "concurrency"), 64),
        (("foreground", "request_settings", "stream"), False),
        (("foreground", "offered_count"), 500),
        (("foreground", "dispatched_count"), 500),
        (("foreground", "outcomes"), {"SUCCESS": 500}),
        (("foreground", "peak_active"), 64),
        (("foreground", "records", 0, "request_index"), True),
        (("foreground", "records", 0, "scheduled_ns"), 0),
        (("foreground", "records", 0, "arrival_observed_ns"), 0),
        (("foreground", "records", 0, "dispatch_lag_ns"), 999),
        (("foreground", "records", 0, "attempts"), 2),
        (("foreground", "records", 0, "http_status"), 429),
        (("foreground", "records", 0, "first_content_ns"), 1),
        (("foreground", "records", 0, "protocol_done_ns"), None),
        (("foreground", "records", 0, "content_event_times_ns"), [0] * 11),
        (("foreground", "records", 0, "finish_reason"), "private-error-message"),
        (("foreground", "records", 0, "prompt_tokens"), 7),
        (("foreground", "records", 0, "usage_provenance"), "EXACT_TOKEN_MEASUREMENT"),
        (("decisions", 0, "reason"), "FRESH_LOAD"),
        (("decisions", 0, "selected_endpoint_id"), "endpoint-b"),
        (("decisions", 0, "decision_ns"), 0),
        (("decisions", 0, "snapshot", "endpoints", 0, "load", "running"), 999),
        (("observations", 0, "sequence"), 1),
        (("observations", 0, "published_ns"), 2**53),
        (("observations", 0, "published_to_router"), False),
        (("events", 0, "kind"), "FAKE_EVENT"),
        (("events", 0, "observed_ns"), 0),
        (("fault_schedule", "restore_ns"), 999),
        (("actual_overload",), "VERIFIED_GPU_OVERLOAD"),
        (("recovery", "first_decision_using_restored_load_ns"), 0),
    ],
)
def test_import_rejects_forged_closed_identity_timing_aggregates_and_provenance(
    rehearsal: tuple[Any, dict],
    path: tuple[Any, ...],
    replacement: Any,
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = deepcopy(results[trial.policy_id])
    destination = raw
    for component in path[:-1]:
        destination = destination[component]
    destination[path[-1]] = replacement
    with pytest.raises(StudyValidationError) as error:
        validate_trial_result(raw, trial.config)
    assert "private" not in repr(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "missing-record",
        "duplicate-record",
        "duplicate-decision",
        "duplicate-event",
        "observer-route",
        "frozen-value",
        "mixed-snapshot-history",
    ],
)
def test_import_rejects_missing_duplicate_and_cross_channel_rows(
    rehearsal: tuple[Any, dict],
    mutation: str,
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = deepcopy(results[trial.policy_id])
    if mutation == "extra-field":
        raw["private_prompt"] = "secret"
    elif mutation == "missing-record":
        raw["foreground"]["records"].pop()
    elif mutation == "duplicate-record":
        raw["foreground"]["records"][1] = deepcopy(raw["foreground"]["records"][0])
    elif mutation == "duplicate-decision":
        raw["decisions"][1] = deepcopy(raw["decisions"][0])
    elif mutation == "duplicate-event":
        raw["events"].append(deepcopy(raw["events"][0]))
    elif mutation == "observer-route":
        next(
            row for row in raw["observations"] if row["channel"] == "INDEPENDENT_LOAD"
        )["published_to_router"] = True
    elif mutation == "frozen-value":
        next(row for row in raw["observations"] if row["published_to_router"] is False)[
            "running"
        ] = 99
    else:
        raw["decisions"][1]["snapshot"] = deepcopy(raw["decisions"][0]["snapshot"])
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


def test_completed_status_cannot_relabel_a_cancelled_foreground() -> None:
    trial = compile_study(load(study_payload())).trials[0]
    raw = measure(trial, "CANCELLED_DURING_FREEZE")
    assert raw["foreground"]["cancelled"] is True
    raw["status"] = "COMPLETED"
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


@pytest.mark.parametrize("population", ["foreground", "background"])
def test_failed_warmup_requires_unadmitted_cancelled_populations(
    population: str,
) -> None:
    trial = compile_study(load(study_payload("STALE_LOAD"))).trials[0]
    raw = measure(trial, "WARMUP_FAILED")
    row = raw[population]["records"][0]
    row.update(
        outcome="REJECTED_CAPACITY",
        arrival_observed_ns=row["scheduled_ns"],
        terminal_ns=row["scheduled_ns"],
    )
    raw["elapsed_ns"] = raw[population]["elapsed_ns"] = 100 * MS
    raw[population]["arrivals_observed_count"] = 1
    raw[population]["outcomes"] = dict(
        Counter(item["outcome"] for item in raw[population]["records"])
    )
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


@pytest.mark.parametrize("mutation", ["historical-health", "incoherent-prefix"])
def test_warmup_replays_latest_health_in_one_coherent_trace_prefix(
    mutation: str,
) -> None:
    payload = study_payload()
    payload["blocks"][0]["foreground"]["offers"][0]["scheduled_ns"] = 35 * MS
    trial = compile_study(load(payload)).trials[0]
    raw = measure(trial)
    for row in raw["observations"]:
        failed = row["channel"] == "HEALTH" and (
            (row["endpoint_id"] == "endpoint-a" and row["sequence"] == 1)
            or (
                row["sequence"] == 2
                and row["endpoint_id"]
                == ("endpoint-a" if mutation == "historical-health" else "endpoint-b")
            )
        )
        if failed:
            row.update(status="HTTP_ERROR", healthy=None)
    if mutation == "incoherent-prefix":
        # At the warmup timestamp B fails before A recovers: no trace prefix
        # contains two healthy endpoints despite each having a valid history.
        raw["observations"].sort(
            key=lambda row: (
                row["published_ns"],
                0
                if row["published_ns"] == 20 * MS
                and row["channel"] == "HEALTH"
                and row["endpoint_id"] == "endpoint-b"
                else 1,
            )
        )
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


@pytest.mark.parametrize("mutation", ["late-start", "phase-order", "no-start"])
def test_background_execution_requires_its_actual_start_and_supported_phase_order(
    rehearsal: tuple[Any, dict], mutation: str
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = (
        measure(trial, "CANCELLED_DURING_FREEZE")
        if mutation == "no-start"
        else deepcopy(results[trial.policy_id])
    )
    event = next(row for row in raw["events"] if row["kind"] == "BACKGROUND_STARTED")
    if mutation == "no-start":
        raw["events"].remove(event)
    elif mutation == "phase-order":
        event["observed_ns"] = 105 * MS
        raw["events"].sort(key=lambda row: row["observed_ns"])
    else:
        event["observed_ns"] = 55 * MS
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


@pytest.mark.parametrize("outcome", ["TIMEOUT", "DRAIN_TIMEOUT"])
def test_scheduled_background_stop_precedes_timeout_classification(
    rehearsal: tuple[Any, dict], outcome: str
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = deepcopy(results[trial.policy_id])
    row = raw["background"]["records"][0]
    if outcome == "TIMEOUT":
        # The second offer has a request deadline after the drain, so change
        # only this expected config's request timeout to make TIMEOUT applicable.
        trial_config = trial.config.model_copy(
            update={
                "background": trial.config.background.model_copy(
                    update={
                        "bounds": trial.config.background.bounds.model_copy(
                            update={"request_timeout_ns": 150 * MS}
                        )
                    }
                )
            }
        )
        raw = measure(replace(trial, config=trial_config))
        row = raw["background"]["records"][0]
    else:
        trial_config = trial.config
    row.update(outcome=outcome, terminal_ns=250 * MS)
    raw["background"]["elapsed_ns"] = raw["elapsed_ns"] = 300 * MS
    raw["background"]["outcomes"] = {outcome: 1, "SUCCESS": 1}
    raw["recovery"]["background_active_at_restore"] = True
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial_config)


@pytest.mark.parametrize("peak", [1, 2])
def test_dispatch_intervals_cannot_exceed_declared_peak_or_concurrency(
    peak: int,
) -> None:
    trial = compile_study(load(study_payload())).trials[0]
    raw = measure(trial)
    for row in raw["foreground"]["records"][:3]:
        row.update(protocol_done_ns=100 * MS, terminal_ns=100 * MS)
    raw["foreground"]["peak_active"] = peak
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


def test_observation_start_cannot_bypass_configured_polling_cadence() -> None:
    trial = compile_study(load(study_payload())).trials[0]
    raw = measure(trial)
    next(
        row
        for row in raw["observations"]
        if row["channel"] == "INDEPENDENT_LOAD"
        and row["endpoint_id"] == "endpoint-a"
        and row["sequence"] == 1
    )["started_ns"] = MS
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


def test_old_acquisition_at_exact_restoration_time_remains_suppressed(
    rehearsal: tuple[Any, dict],
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    row = deepcopy(
        next(
            row
            for row in results[trial.policy_id]["observations"]
            if row["channel"] == "ROUTER_LOAD" and row["endpoint_id"] == "endpoint-a"
        )
    )
    row.update(
        started_ns=90 * MS,
        completed_ns=100 * MS,
        published_ns=100 * MS,
        published_to_router=False,
        running=None,
        waiting=None,
    )
    phases = (
        FaultEvent("FREEZE_STARTED", 40 * MS),
        FaultEvent("TELEMETRY_RESTORED", 100 * MS),
    )
    assert _observations([row], trial.config, phases, 200 * MS)[0].running is None
    row.update(published_to_router=True, running=0, waiting=0)
    with pytest.raises(StudyValidationError, match="old acquisition at restoration"):
        _observations([row], trial.config, phases, 200 * MS)


@pytest.mark.parametrize(
    "content",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1.0}',
        b"\xff",
        b'["wrong top level"]',
        b'{"x":9999999999999999999999999}',
    ],
)
def test_byte_loader_rejects_ambiguous_noninteger_or_invalid_json(
    rehearsal: tuple[Any, dict],
    content: bytes,
) -> None:
    with pytest.raises(StudyValidationError):
        load_trial_result_bytes(content, rehearsal[0].trials[0].config)


def test_byte_loader_enforces_bound_before_decode(
    rehearsal: tuple[Any, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inferdrome.evaluation.study_validation.MAX_RESULT_BYTES", 8)
    with pytest.raises(StudyValidationError):
        load_trial_result_bytes(b"x" * 9, rehearsal[0].trials[0].config)


def _rewrite_progress(row: dict, first: int, terminal: int) -> None:
    row["first_content_ns"] = row["scheduled_ns"] + first
    row["content_event_times_ns"] = [row["first_content_ns"]]
    row["protocol_done_ns"] = row["scheduled_ns"] + terminal
    row["terminal_ns"] = row["scheduled_ns"] + terminal


def test_slo_requires_success_both_slos_and_scheduled_origin_with_exact_equality(
    rehearsal: tuple[Any, dict],
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = deepcopy(results[trial.policy_id])
    rows = raw["foreground"]["records"]
    _rewrite_progress(rows[0], 5 * MS, 10 * MS)  # Exact equality counts.
    _rewrite_progress(rows[1], 6 * MS, 7 * MS)  # First content misses.
    rows[1]["dispatch_ns"] += 4 * MS
    rows[1]["dispatch_lag_ns"] = 4 * MS
    rows[1]["response_headers_ns"] = rows[1]["first_body_byte_ns"] = rows[1][
        "dispatch_ns"
    ]
    _rewrite_progress(rows[2], MS, 11 * MS)  # Completion misses despite fast content.
    rows[3]["outcome"] = (
        "STREAM_ERROR"  # Fast content followed by failure never counts.
    )
    rows[3]["protocol_done_ns"] = None
    raw["foreground"]["outcomes"] = dict(Counter(row["outcome"] for row in rows))
    summary = summarize_trial(trial, validate_trial_result(raw, trial.config))
    foreground = summary["foreground"]
    assert foreground["offered_count"] == 6
    assert foreground["successful_count"] == 5
    assert foreground["slo_good_count"] == 3
    assert foreground["fixed_offered_window_ns"] == 180 * MS
    assert foreground["slo_goodput_rps"] == {
        "numerator": 3_000_000_000,
        "denominator": 180 * MS,
        "decimal": "16.666667",
    }
    assert foreground["slo_success_fraction"]["decimal"] == "0.500000"
    assert foreground["successful_latency_ns"]["count"] == 5
    assert (
        foreground["partial_timing_by_outcome"]["STREAM_ERROR"][
            "scheduled_to_first_content_ns"
        ]["count"]
        == 1
    )
    assert foreground["dispatch_lag_ns"]["p95"] == 4 * MS
    assert summary["background"]["offered_count"] == 2


def test_reject_all_and_never_due_cancelled_requests_stay_in_fixed_denominator() -> (
    None
):
    trial = compile_study(load(study_payload())).trials[0]
    validated = validate_trial_result(
        measure(trial, "CANCELLED_BEFORE_START"), trial.config
    )
    summary = summarize_trial(trial, validated)
    assert summary["foreground"]["offered_count"] == 6
    assert summary["foreground"]["dispatched_count"] == 0
    assert summary["foreground"]["slo_good_count"] == 0
    assert summary["foreground"]["slo_goodput_rps"]["denominator"] == 180 * MS
    assert summary["foreground"]["slo_success_fraction"]["decimal"] == "0.000000"
    assert summary["foreground"]["outcomes"]["CANCELLED"]["count"] == 6
    assert (
        summary["foreground"]["successful_latency_ns"]["scheduled_to_terminal"]["p50"]
        is None
    )


def test_quantile_floor_is_success_population_only() -> None:
    assert _quantiles(tuple(range(999)))["p99"] is None
    assert _quantiles(tuple(range(1000)))["p99"] == 989
    failed = _quantiles(tuple(range(1000)), success_population=False)
    assert failed["p99"] is None
    assert failed["p99_status"] == "NOT_A_SUCCESS_LATENCY_POPULATION"
    assert _signed_ratio(-1, 10**12)["decimal"] == "0.000000"
    assert _signed_ratio(-1, 10**12)["sign"] == -1


def test_usage_coverage_separates_success_from_partial_server_reported_tokens(
    rehearsal: tuple[Any, dict],
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    raw = deepcopy(results[trial.policy_id])
    for row in raw["foreground"]["records"][:2]:
        row.update(
            prompt_tokens=11,
            completion_tokens=9,
            usage_provenance="SERVER_REPORTED_STREAM_USAGE",
        )
    raw["foreground"]["records"][1].update(
        outcome="INCOMPLETE_STREAM", protocol_done_ns=None
    )
    raw["foreground"]["outcomes"] = {"SUCCESS": 5, "INCOMPLETE_STREAM": 1}
    summary = summarize_trial(trial, validate_trial_result(raw, trial.config))
    success, partial = (
        summary["foreground"][key] for key in ("usage_success", "usage_other_outcomes")
    )
    assert (success["reported_count"], success["missing_count"]) == (1, 4)
    assert (partial["reported_count"], partial["missing_count"]) == (1, 0)
    assert (
        success["completion_tokens_total_decimal"]
        == partial["completion_tokens_total_decimal"]
        == "9"
    )


def test_recovery_uses_actual_restore_and_preserves_censoring_and_rr_applicability(
    rehearsal: tuple[Any, dict],
) -> None:
    plan, results = rehearsal
    rr = plan.trials[0]
    summary = summarize_trial(
        rr, validate_trial_result(results[rr.policy_id], rr.config)
    )
    assert summary["recovery"]["applicability"] == "NOT_APPLICABLE_POLICY"
    assert summary["recovery"]["intervals"]["dispatch"]["duration_ns"] is None
    trial = plan.trials[1]
    validated = validate_trial_result(results[trial.policy_id], trial.config)
    recovery = dict(validated.recovery)
    recovery.update(
        telemetry_restored_ns=110 * MS,
        first_restored_publication_ns=115 * MS,
        first_decision_using_restored_load_ns=None,
        first_dispatch_using_restored_load_ns=None,
    )
    measured = summarize_trial(trial, replace(validated, recovery=recovery))["recovery"]
    assert measured["planned_restore_ns"] == 100 * MS
    assert measured["intervals"]["publication"]["duration_ns"] == 5 * MS
    assert measured["intervals"]["dispatch"]["status"] == "UNOBSERVED_OR_CENSORED"
    assert measured["intervals"]["dispatch"]["duration_ns"] is None


def _manifest(plan: Any, summaries: list[dict], status: str = "COMPLETED") -> dict:
    by_id = {summary["trial_id"]: summary for summary in summaries}
    return {
        "config_sha256": plan.config_sha256,
        "plan_sha256": sha256_digest(canonical_json_bytes(plan.to_dict()) + b"\n"),
        "status": status,
        "reason": None if status == "COMPLETED" else "CONTROLLER_OR_CLEANUP_FAILED",
        "elapsed_ns": sum(summary["trial_elapsed_ns"] for summary in summaries),
        "trials": [
            {
                "trial_id": trial.trial_id,
                "state": "RETURNED" if trial.trial_id in by_id else "NOT_RUN",
                **(
                    {
                        "result_sha256": by_id[trial.trial_id]["result_sha256"],
                        "result_status": by_id[trial.trial_id]["status"],
                    }
                    if trial.trial_id in by_id
                    else {}
                ),
            }
            for trial in plan.trials
        ],
    }


def test_low_replication_and_incomplete_blocks_suppress_precision_and_comparisons() -> (
    None
):
    plan = compile_study(load(study_payload()))
    summaries = [
        summarize_trial(trial, validate_trial_result(measure(trial), trial.config))
        for trial in plan.trials
    ]
    complete = summarize_study(plan, summaries, _manifest(plan, summaries))
    contrast = complete["strata"][0]["paired_contrasts"][0]
    assert contrast["interval_status"] == "INSUFFICIENT_TRIAL_REPLICATION"
    assert contrast["interval_rps"] is None
    partial = summaries[:-1]
    report = summarize_study(plan, partial, _manifest(plan, partial, "ABORTED"))
    assert report["coverage"]["not_run_trials"] == 1
    assert report["comparative_headline"] == "SUPPRESSED_INCOMPLETE_STUDY"
    for contrast in report["strata"][0]["paired_contrasts"]:
        assert contrast["interval_status"] == "INCOMPLETE_STUDY"
        assert contrast["mean_goodput_difference_rps"] is None
        assert contrast["interval_rps"] is None
    markdown = render_markdown(report)
    assert "SUPPRESSED_INCOMPLETE_STUDY" in markdown
    assert "LOCAL_MEASUREMENT_ONLY" in markdown
    assert "1 not run" in markdown
    assert "90%" in markdown and "2,000" in markdown
    for secret in ("private-model", "private foreground", "127.0.0.1"):
        assert secret not in markdown


def test_incomplete_report_exposes_elapsed_reason_and_unmeasured_offer_coverage(
    rehearsal: tuple[Any, dict],
) -> None:
    plan, results = rehearsal
    trial = plan.trials[0]
    summary = summarize_trial(
        trial,
        replace(
            validate_trial_result(results[trial.policy_id], trial.config),
            evidence_class="SYNTHETIC_ONLY",
        ),
    )
    manifest = _manifest(plan, [summary], "ABORTED")
    manifest["trials"][1]["state"] = "ABORTED"
    manifest["elapsed_ns"] += 1234
    report = summarize_study(plan, [summary], manifest)
    assert report["reason"] == "CONTROLLER_OR_CLEANUP_FAILED"
    assert report["study_elapsed_ns"] == manifest["elapsed_ns"]
    assert report["study_elapsed_semantics"] == (
        "WHOLE_OPERATION_INCLUDING_TRIALS_COOLDOWNS_AND_IO"
    )
    assert report["coverage"]["foreground"] == {
        "planned_offers": 24,
        "returned_records": 6,
        "offers_in_aborted_trials_without_measurements": 6,
        "offers_in_not_run_trials": 12,
    }
    assert report["coverage"]["background"] == {
        "planned_offers": 8,
        "returned_records": 2,
        "offers_in_aborted_trials_without_measurements": 2,
        "offers_in_not_run_trials": 4,
    }
    assert len(report["trials"]) == 1
    assert report["trials"][0]["foreground"]["fixed_offered_window_ns"] == 180 * MS
    markdown = render_markdown(report)
    assert "SYNTHETIC_ONLY" in markdown
    assert "evidence eligible: **false**" in markdown
    assert "CONTROLLER_OR_CLEANUP_FAILED" in markdown
    assert "whole operation including trials, cooldowns and file I/O" in markdown
    assert "24 planned, 6 returned records" in markdown
    assert "6 in aborted trials without measurements, 12 in trials not run" in markdown


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-summary",
        "wrong-trial",
        "wrong-config",
        "wrong-ledger-status",
        "mixed-evidence",
        "wrong-plan-hash",
    ],
)
def test_aggregate_rejects_duplicate_or_mismatched_trial_coverage(
    mutation: str,
) -> None:
    plan = compile_study(load(study_payload()))
    summaries = [
        summarize_trial(trial, validate_trial_result(measure(trial), trial.config))
        for trial in plan.trials
    ]
    manifest = _manifest(plan, summaries)
    if mutation == "duplicate-summary":
        summaries[1] = summaries[0]
    elif mutation == "wrong-trial":
        summaries[0]["trial_id"] = "trial-9999"
    elif mutation == "wrong-config":
        summaries[0]["config_sha256"] = "sha256:" + "0" * 64
    elif mutation == "wrong-ledger-status":
        manifest["trials"][0]["result_status"] = "CANCELLED"
    elif mutation == "mixed-evidence":
        summaries[0]["evidence_class"] = "SYNTHETIC_ONLY"
    else:
        manifest["plan_sha256"] = sha256_digest(canonical_json_bytes(plan.to_dict()))
    with pytest.raises(StudyValidationError):
        summarize_study(plan, summaries, manifest)


def test_block_bootstrap_preserves_pairing_and_predeclared_90_percent_ranks() -> None:
    values = []
    for index in range(8):
        base = 100 * index
        values.append(
            {
                POLICY_IDS[0]: {"foreground": {"slo_good_count": base}},
                POLICY_IDS[1]: {"foreground": {"slo_good_count": base + 2}},
                POLICY_IDS[2]: {"foreground": {"slo_good_count": base + index}},
                POLICY_IDS[3]: {"foreground": {"slo_good_count": base - 1}},
            }
        )
    result = _paired_contrasts(values, 1_000_000_000, 37, True)
    assert result == _paired_contrasts(values, 1_000_000_000, 37, True)
    assert result[0]["interval_rps"]["lower"]["decimal"] == "2.000000"
    assert result[0]["interval_rps"]["upper"]["decimal"] == "2.000000"
    assert result[2]["interval_rps"]["upper"]["decimal"] == "-1.000000"
    generator = random.Random(37)
    draws = sorted(sum(generator.randrange(8) for _ in range(8)) for _ in range(2000))
    assert (
        result[1]["interval_rps"]["lower"]["magnitude_numerator"]
        == draws[99] * 1_000_000_000
    )
    assert (
        result[1]["interval_rps"]["upper"]["magnitude_numerator"]
        == draws[1899] * 1_000_000_000
    )
    assert result[1]["confidence_percent"] == 90
    assert result[1]["percentile_ranks"] == [5, 95]
    assert result[1]["replication_unit"] == "MATCHED_TRIAL_BLOCK"


def test_eight_complete_matched_blocks_allow_only_block_level_intervals() -> None:
    value = study_payload()
    blocks(value, 8)
    plan = compile_study(load(value))
    raw_by_policy = {trial.policy_id: measure(trial) for trial in plan.trials[:4]}
    summaries = [
        summarize_trial(
            trial, validate_trial_result(raw_by_policy[trial.policy_id], trial.config)
        )
        for trial in plan.trials
    ]
    report = summarize_study(plan, summaries, _manifest(plan, summaries))
    assert report["coverage"]["completed_trial_results"] == 32
    assert report["strata"][0]["complete_blocks"] == 8
    assert all(
        contrast["interval_status"] == "AVAILABLE"
        for contrast in report["strata"][0]["paired_contrasts"]
    )
    # Identical deterministic payloads are not an execution-attestation claim.
    assert report["duplicate_payload_hash_groups"] == 4
    assert "INDEPENDENT_EXECUTION" in report["limitations"][0]
