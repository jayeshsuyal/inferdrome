"""All-offered SLO metrics and paired trial-block uncertainty, without live claims."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from inferdrome.evaluation.contracts import EvaluationConfig
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.runner import EvaluationResult, RequestRecord
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial
from inferdrome.evaluation.study_validation import (
    OUTCOMES,
    StudyValidationError,
    ValidatedTrialResult,
)
from inferdrome.metrics.quantiles import decimal_ratio, nearest_rank
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

_P99_FLOOR = 1000
_BOOTSTRAP_RESAMPLES = 2000
_MIN_BLOCKS = 8
_REFERENCE = "evaluation_round_robin_v1"


def _require(condition: bool) -> None:
    if not condition:
        raise StudyValidationError(
            "study report inputs violate compiled trial coverage"
        )


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "decimal": decimal_ratio(numerator, denominator),
    }


def _signed_ratio(numerator: int, denominator: int) -> dict[str, Any]:
    magnitude = decimal_ratio(abs(numerator), denominator)
    return {
        "sign": -1 if numerator < 0 else 0 if numerator == 0 else 1,
        "magnitude_numerator": abs(numerator),
        "denominator": denominator,
        "decimal": ("-" if numerator < 0 and magnitude != "0.000000" else "")
        + magnitude,
    }


def _quantiles(
    values: tuple[int, ...], *, success_population: bool = True
) -> dict[str, Any]:
    count = len(values)
    eligible = success_population and count >= _P99_FLOOR
    return {
        "count": count,
        "method": "NEAREST_RANK",
        "p50": nearest_rank(values, 50) if values else None,
        "p90": nearest_rank(values, 90) if values else None,
        "p95": nearest_rank(values, 95) if values else None,
        "p99": nearest_rank(values, 99) if eligible else None,
        "p99_status": "NOT_A_SUCCESS_LATENCY_POPULATION"
        if not success_population
        else "DESCRIPTIVE_ONLY"
        if eligible
        else "BELOW_REPORTING_FLOOR",
        "p99_min_successes": _P99_FLOOR,
    }


def _usage(records: tuple[RequestRecord, ...]) -> dict[str, Any]:
    reported = tuple(
        row for row in records if row.usage_provenance == "SERVER_REPORTED_STREAM_USAGE"
    )
    # Strings preserve exact totals even if a server reports an unusually large
    # count; JSON binary64 consumers must not silently round accumulated tokens.
    return {
        "population_count": len(records),
        "reported_count": len(reported),
        "missing_count": len(records) - len(reported),
        "prompt_tokens_total_decimal": str(
            sum(row.prompt_tokens or 0 for row in reported)
        ),
        "completion_tokens_total_decimal": str(
            sum(row.completion_tokens or 0 for row in reported)
        ),
        "provenance": "SERVER_REPORTED_STREAM_USAGE_ONLY",
    }


def _population_counts(population: EvaluationResult) -> dict[str, Any]:
    counts: Counter[str] = Counter(row.outcome for row in population.records)
    count = len(population.records)
    return {
        "offered_count": count,
        "successful_count": counts["SUCCESS"],
        "outcomes": {
            name: {
                "count": counts[name],
                "offered_fraction": _ratio(counts[name], count),
            }
            for name in OUTCOMES
        },
        "arrival_observed_count": sum(
            row.arrival_observed_ns is not None for row in population.records
        ),
        "dispatched_count": sum(row.attempts for row in population.records),
        "peak_active": population.peak_active,
        "peak_queue": population.peak_queue,
        "elapsed_ns": population.elapsed_ns,
        "cancelled": population.cancelled,
        "usage_success": _usage(
            tuple(row for row in population.records if row.outcome == "SUCCESS")
        ),
        "usage_other_outcomes": _usage(
            tuple(row for row in population.records if row.outcome != "SUCCESS")
        ),
    }


def _recovery_summary(
    trial: CompiledTrial, result: ValidatedTrialResult
) -> dict[str, Any]:
    source = result.recovery
    applicability = (
        "NOT_APPLICABLE_SCENARIO"
        if source is None
        else "NOT_APPLICABLE_POLICY"
        if not source["load_recovery_applicable"]
        else "APPLICABLE"
    )
    restored = source["telemetry_restored_ns"] if source else None
    intervals: dict[str, Any] = {}
    names = {
        "publication": "first_restored_publication_ns",
        "decision": "first_decision_using_restored_load_ns",
        "dispatch": "first_dispatch_using_restored_load_ns",
    }
    for name, key in names.items():
        observed = source[key] if source is not None else None
        # Publication remains descriptive for round-robin; load-use recovery
        # itself is not an applicable policy metric in that case.
        applicable = source is not None and (
            name == "publication" or applicability == "APPLICABLE"
        )
        interval = (
            observed - restored
            if applicable and restored is not None and observed is not None
            else None
        )
        intervals[name] = {
            "duration_ns": interval,
            "status": "NOT_APPLICABLE"
            if not applicable
            else "RESTORE_NOT_OBSERVED"
            if restored is None
            else "UNOBSERVED_OR_CENSORED"
            if observed is None
            else "OBSERVED",
            "observation_horizon_ns": result.elapsed_ns - restored
            if applicable and restored is not None
            else None,
        }
    return {
        "applicability": applicability,
        "origin": "ACTUAL_TELEMETRY_RESTORED_EVENT",
        "planned_restore_ns": trial.config.fault.restore_ns
        if isinstance(trial.config, RoutingFaultConfig)
        else None,
        "actual_restore_ns": restored,
        "intervals": intervals,
        "background_active_at_restore": source["background_active_at_restore"]
        if source
        else None,
        "fault_actual_overload": "NOT_ESTABLISHED_BY_FAULT_SCHEDULE"
        if source
        else "NOT_APPLICABLE",
    }


def summarize_population(
    config: EvaluationConfig,
    result: EvaluationResult,
    *,
    window_start_ns: int,
    window_end_ns: int,
    first_content_slo_ns: int,
    completion_slo_ns: int,
) -> dict[str, Any]:
    """Summarize one validated population with a fixed all-offered SLO window."""
    window = window_end_ns - window_start_ns
    _require(
        window > 0
        and window_end_ns == config.bounds.duration_ns
        and result.config_sha256
        == sha256_digest(canonical_json_bytes(config.model_dump(mode="json")))
    )
    rows = result.records
    _require(
        len(rows) == len(config.offers)
        and all(window_start_ns <= row.scheduled_ns < window_end_ns for row in rows)
    )
    successful = tuple(row for row in rows if row.outcome == "SUCCESS")
    good = sum(
        row.first_content_ns is not None
        and row.first_content_ns - row.scheduled_ns <= first_content_slo_ns
        and row.terminal_ns - row.scheduled_ns <= completion_slo_ns
        for row in successful
    )
    summary = _population_counts(result)
    summary.update(
        {
            "slo_good_count": good,
            "slo_success_fraction": _ratio(good, len(rows)),
            "slo_goodput_rps": _ratio(good * 1_000_000_000, window),
            "offered_rate_rps": _ratio(len(rows) * 1_000_000_000, window),
            "dispatch_rate_rps": _ratio(
                sum(row.attempts for row in rows) * 1_000_000_000, window
            ),
            "fixed_offered_window_ns": window,
            "drain_ns": config.bounds.drain_ns,
            "last_offer_ns": max(row.scheduled_ns for row in rows),
            "successful_completions_during_drain": sum(
                row.terminal_ns >= window_end_ns for row in successful
            ),
            "dispatch_lag_ns": {
                "population": "ALL_DISPATCHED_OUTCOMES",
                **_quantiles(
                    tuple(
                        row.dispatch_lag_ns
                        for row in rows
                        if row.dispatch_lag_ns is not None
                    ),
                    success_population=False,
                ),
            },
            "successful_latency_ns": {
                "population": "ALL_SUCCESS_INCLUDING_SLO_MISSES",
                "count": len(successful),
                "scheduled_to_first_content": _quantiles(
                    tuple(
                        row.first_content_ns - row.scheduled_ns
                        for row in successful
                        if row.first_content_ns is not None
                    )
                ),
                "scheduled_to_terminal": _quantiles(
                    tuple(row.terminal_ns - row.scheduled_ns for row in successful)
                ),
                "dispatch_to_first_content": _quantiles(
                    tuple(
                        row.first_content_ns - row.dispatch_ns
                        for row in successful
                        if row.first_content_ns is not None
                        and row.dispatch_ns is not None
                    )
                ),
                "dispatch_to_terminal": _quantiles(
                    tuple(
                        row.terminal_ns - row.dispatch_ns
                        for row in successful
                        if row.dispatch_ns is not None
                    )
                ),
            },
            "partial_timing_by_outcome": {
                outcome: {
                    "offered_count": sum(row.outcome == outcome for row in rows),
                    "scheduled_to_first_content_ns": _quantiles(
                        tuple(
                            row.first_content_ns - row.scheduled_ns
                            for row in rows
                            if row.outcome == outcome
                            and row.first_content_ns is not None
                        ),
                        success_population=False,
                    ),
                }
                for outcome in OUTCOMES
                if outcome != "SUCCESS"
            },
        }
    )
    return summary


def summarize_trial(
    trial: CompiledTrial, result: ValidatedTrialResult
) -> dict[str, Any]:
    """Summarize a validated result; every planned foreground offer remains in N."""
    _require(
        result.config_sha256 == trial.config_sha256
        and result.policy_id == trial.policy_id
    )
    foreground = summarize_population(
        trial.config.foreground,
        result.foreground,
        window_start_ns=trial.window_start_ns,
        window_end_ns=trial.window_end_ns,
        first_content_slo_ns=trial.first_content_slo_ns,
        completion_slo_ns=trial.completion_slo_ns,
    )
    return {
        "schema_version": "inferdrome.evaluation-study-trial-summary.v1",
        **trial.to_dict(),
        "result_sha256": result.result_sha256,
        "status": result.status,
        "evidence_class": result.evidence_class,
        "evidence_eligible": False,
        "calibration": "UNCALIBRATED_REHEARSAL",
        "runtime_identity": "UNVERIFIED",
        "cost": "UNAVAILABLE",
        "timing_semantics": "CONTENT_EVENTS_NOT_EXACT_TOKENS_OR_WIRE_TIMING",
        "foreground": foreground,
        "background": _population_counts(result.background)
        if result.background
        else None,
        "model_warmup": "EXTERNALLY_PREPARED_UNMEASURED",
        "trial_elapsed_ns": result.elapsed_ns,
        "recovery": _recovery_summary(trial, result),
    }


def _stratum(trial: CompiledTrial) -> tuple[str, str, str | None]:
    return trial.scenario, trial.profile_id, trial.target_endpoint_id


def _paired_contrasts(
    blocks: list[dict[str, dict[str, Any]]], window: int, seed: int, available: bool
) -> list[dict[str, Any]]:
    n = len(blocks)
    differences = {
        policy: [
            block[policy]["foreground"]["slo_good_count"]
            - block[_REFERENCE]["foreground"]["slo_good_count"]
            for block in blocks
        ]
        for policy in POLICY_IDS
        if policy != _REFERENCE
    }
    samples: dict[str, list[int]] = {policy: [] for policy in differences}
    if available and n >= _MIN_BLOCKS:
        generator = random.Random(seed)
        for _ in range(_BOOTSTRAP_RESAMPLES):
            indices = [generator.randrange(n) for _ in range(n)]
            for policy, values in differences.items():
                samples[policy].append(sum(values[index] for index in indices))
    result: list[dict[str, Any]] = []
    for policy, values in differences.items():
        draws = tuple(samples[policy])
        denominator = n * window
        result.append(
            {
                "policy_id": policy,
                "reference_policy_id": _REFERENCE,
                "replication_unit": "MATCHED_TRIAL_BLOCK",
                "complete_blocks": n,
                "mean_goodput_difference_rps": _signed_ratio(
                    sum(values) * 1_000_000_000, denominator
                )
                if n and available
                else None,
                "interval_status": "INCOMPLETE_STUDY"
                if not available
                else "INSUFFICIENT_TRIAL_REPLICATION"
                if n < _MIN_BLOCKS
                else "AVAILABLE",
                "confidence_percent": 90,
                "percentile_ranks": [5, 95],
                "bootstrap_resamples": _BOOTSTRAP_RESAMPLES,
                "bootstrap_seed": seed,
                "interval_rps": {
                    "lower": _signed_ratio(
                        nearest_rank(draws, 5) * 1_000_000_000, denominator
                    ),
                    "upper": _signed_ratio(
                        nearest_rank(draws, 95) * 1_000_000_000, denominator
                    ),
                }
                if draws
                else None,
            }
        )
    return result


def summarize_study(
    plan: CompiledStudy, summaries: Sequence[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Combine bounded internally generated summaries with the verified run ledger.

    Artifact bytes and manifest file/identity rules are checked by the bundle
    owner before this call. Duplicate payload hashes are disclosed rather than
    treated as proof of re-use: deterministic local executions can be identical.
    """
    _require(len(summaries) <= len(plan.trials) <= 256)
    _require(
        type(manifest["elapsed_ns"]) is int
        and 0 <= manifest["elapsed_ns"] <= 2**53 - 1
        and manifest["reason"]
        in (
            None,
            "CANCELLED",
            "STUDY_DEADLINE",
            "WARMUP_FAILED",
            "CONTROLLER_OR_CLEANUP_FAILED",
            "RESULT_VALIDATION_FAILED",
            "OUTPUT_FAILED",
        )
    )
    _require(manifest["config_sha256"] == plan.config_sha256)
    _require(
        manifest["plan_sha256"]
        == sha256_digest(canonical_json_bytes(plan.to_dict()) + b"\n")
    )
    ledger = manifest["trials"]
    _require(len(ledger) == len(plan.trials))
    by_id: dict[str, dict[str, Any]] = {}
    for measured_summary in summaries:
        identity = measured_summary["trial_id"]
        _require(identity not in by_id)
        by_id[identity] = measured_summary
    returned = 0
    evidence: set[str] = set()
    blocks: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    planned_blocks: dict[tuple[str, str, str | None], set[str]] = defaultdict(set)
    for trial, entry in zip(plan.trials, ledger, strict=True):
        _require(entry["trial_id"] == trial.trial_id)
        planned_blocks[_stratum(trial)].add(trial.block_id)
        summary = by_id.get(trial.trial_id)
        if entry["state"] == "RETURNED":
            returned += 1
            _require(summary is not None)
            assert summary is not None
            for name, expected in trial.to_dict().items():
                _require(summary[name] == expected)
            _require(
                summary["result_sha256"] == entry["result_sha256"]
                and summary["status"] == entry["result_status"]
            )
            _require(
                summary["evidence_class"]
                in ("LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY")
            )
            evidence.add(summary["evidence_class"])
            blocks[trial.block_id][trial.policy_id] = summary
        else:
            _require(entry["state"] in ("ABORTED", "NOT_RUN") and summary is None)
    _require(returned == len(summaries) and len(evidence) <= 1)
    complete = (
        manifest["status"] == "COMPLETED"
        and returned == len(plan.trials)
        and all(summary["status"] == "COMPLETED" for summary in summaries)
    )
    _require(manifest["status"] != "COMPLETED" or complete)
    strata: list[dict[str, Any]] = []
    for key, block_ids in sorted(planned_blocks.items(), key=lambda item: str(item[0])):
        scenario, profile, target = key
        matching_trials = [trial for trial in plan.trials if _stratum(trial) == key]
        windows = {
            trial.window_end_ns - trial.window_start_ns for trial in matching_trials
        }
        _require(len(windows) == 1)
        window = next(iter(windows))
        full = [
            blocks[block_id]
            for block_id in sorted(block_ids)
            if set(blocks[block_id]) == set(POLICY_IDS)
            and all(item["status"] == "COMPLETED" for item in blocks[block_id].values())
        ]
        policy_summaries: list[dict[str, Any]] = []
        for policy in POLICY_IDS:
            measured = [
                blocks[block_id][policy]
                for block_id in sorted(block_ids)
                if policy in blocks[block_id]
            ]
            good = [item["foreground"]["slo_good_count"] for item in measured]
            policy_summaries.append(
                {
                    "policy_id": policy,
                    "returned_trials": len(measured),
                    "completed_trials": sum(
                        item["status"] == "COMPLETED" for item in measured
                    ),
                    "slo_good_counts_by_trial": good,
                    "mean_goodput_rps": _ratio(
                        sum(good) * 1_000_000_000, len(good) * window
                    )
                    if good
                    else None,
                    "observed_min_goodput_rps": _ratio(
                        min(good) * 1_000_000_000, window
                    )
                    if good
                    else None,
                    "observed_max_goodput_rps": _ratio(
                        max(good) * 1_000_000_000, window
                    )
                    if good
                    else None,
                }
            )
        recovery_rows = [
            summary["recovery"]
            for block_id in sorted(block_ids)
            for summary in blocks[block_id].values()
        ]
        recovery_counts: dict[str, Any] = {}
        for metric in ("publication", "decision", "dispatch"):
            values = [row["intervals"][metric] for row in recovery_rows]
            recovery_counts[metric] = {
                "applicable_trials": sum(
                    value["status"] != "NOT_APPLICABLE" for value in values
                ),
                "observed_trials": sum(
                    value["status"] == "OBSERVED" for value in values
                ),
                "censored_or_restore_unobserved_trials": sum(
                    value["status"]
                    in ("UNOBSERVED_OR_CENSORED", "RESTORE_NOT_OBSERVED")
                    for value in values
                ),
                "not_applicable_trials": sum(
                    value["status"] == "NOT_APPLICABLE" for value in values
                ),
            }
        strata.append(
            {
                "scenario": scenario,
                "profile_id": profile,
                "target_endpoint_id": target,
                "planned_blocks": len(block_ids),
                "complete_blocks": len(full),
                "fixed_window_ns": window,
                "policies": policy_summaries,
                "paired_contrasts": _paired_contrasts(
                    full, window, plan.reporting.bootstrap_seed, complete
                ),
                "recovery_coverage": recovery_counts,
            }
        )
    states = Counter(entry["state"] for entry in ledger)
    statuses = Counter(summary["status"] for summary in summaries)
    digest_counts = Counter(summary["result_sha256"] for summary in summaries)
    offer_coverage: dict[str, dict[str, int]] = {}
    for population in ("foreground", "background"):
        counts = {
            "planned_offers": 0,
            "returned_records": 0,
            "offers_in_aborted_trials_without_measurements": 0,
            "offers_in_not_run_trials": 0,
        }
        for trial, entry in zip(plan.trials, ledger, strict=True):
            population_config = getattr(trial.config, population, None)
            offers = len(population_config.offers) if population_config else 0
            counts["planned_offers"] += offers
            if entry["state"] == "RETURNED":
                measured = by_id[trial.trial_id][population]
                counts["returned_records"] += (
                    measured["offered_count"] if measured else 0
                )
            elif entry["state"] == "ABORTED":
                counts["offers_in_aborted_trials_without_measurements"] += offers
            else:
                counts["offers_in_not_run_trials"] += offers
        offer_coverage[population] = counts
    return {
        "schema_version": "inferdrome.evaluation-study-report.v1",
        "config_sha256": plan.config_sha256,
        "plan_sha256": manifest["plan_sha256"],
        "status": manifest["status"],
        "reason": manifest["reason"],
        "study_elapsed_ns": manifest["elapsed_ns"],
        "study_elapsed_semantics": "WHOLE_OPERATION_INCLUDING_TRIALS_COOLDOWNS_AND_IO",
        "comparative_headline": "DESCRIPTIVE_UNCALIBRATED_REHEARSAL"
        if complete
        else "SUPPRESSED_INCOMPLETE_STUDY",
        "coverage": {
            "planned_trials": len(plan.trials),
            "started_trials": states["RETURNED"] + states["ABORTED"],
            "returned_trials": returned,
            "aborted_trials": states["ABORTED"],
            "not_run_trials": states["NOT_RUN"],
            "completed_trial_results": statuses["COMPLETED"],
            "warmup_failed_results": statuses["WARMUP_FAILED"],
            "cancelled_results": statuses["CANCELLED"],
            **offer_coverage,
        },
        "reporting": plan.reporting.model_dump(mode="json"),
        "strata": strata,
        "trials": [
            by_id[trial.trial_id] for trial in plan.trials if trial.trial_id in by_id
        ],
        "evidence_class": next(iter(evidence), "LOCAL_MEASUREMENT_ONLY"),
        "evidence_eligible": False,
        "runtime_identity": "UNVERIFIED",
        "calibration": "UNCALIBRATED_REHEARSAL",
        "cost": "UNAVAILABLE",
        "model_warmup": "EXTERNALLY_PREPARED_UNMEASURED",
        "preparation": plan.config.preparation.model_dump(mode="json"),
        "duplicate_payload_hash_groups": sum(
            count > 1 for count in digest_counts.values()
        ),
        "limitations": [
            "DIGESTS_CHECK_INTEGRITY_NOT_RUNTIME_ATTESTATION_OR_INDEPENDENT_EXECUTION",
            "SCHEDULED_ARRIVAL_ORIGIN_AND_FIXED_OFFERED_WINDOW",
            "REJECTIONS_FAILURES_CANCELLATIONS_REMAIN_IN_OFFERED_DENOMINATOR",
            "TRIAL_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS",
            "P99_FLOOR_IS_NOT_A_PRECISION_OR_INDEPENDENCE_GUARANTEE",
            "DECLARED_CACHE_PREPARATION_AND_RUNTIME_IDENTITIES_UNVERIFIED",
            "MALFORMED_AIOHTTP_FRAMING_CAN_BE_CLASSIFIED_TIMEOUT",
            "NO_LIVE_CAPACITY_COST_OR_ACTUAL_OVERLOAD_ESTABLISHED",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the same metric values and explicit populations as the JSON report."""
    lines = [
        "# Inference evaluation study",
        "",
        f"Status: **{report['status']}**. "
        f"Comparison status: **{report['comparative_headline']}**.",
        "",
        f"Evidence class: **{report['evidence_class']}**; "
        "evidence eligible: **false**.",
        "",
        f"Reason: **{report['reason'] or 'none'}**. "
        f"Study elapsed: **{report['study_elapsed_ns']} ns**, covering the whole "
        "operation including trials, cooldowns and file I/O. "
        "This duration is separate from the fixed offered-window goodput divisor.",
        "",
        "Local, uncalibrated measurements. Runtime identities are unverified; "
        "cost is unavailable.",
        "",
        "Goodput counts successful offered requests meeting both "
        "scheduled-origin SLOs. The divisor is the fixed offered window. "
        "Rejections, errors and cancellation remain in the offered denominator.",
        "",
    ]
    coverage = report["coverage"]
    for population in ("foreground", "background"):
        counts = coverage[population]
        lines.extend(
            [
                f"{population.capitalize()} offers: {counts['planned_offers']} "
                f"planned, {counts['returned_records']} returned records, "
                f"{counts['offers_in_aborted_trials_without_measurements']} "
                "in aborted trials without measurements, "
                f"{counts['offers_in_not_run_trials']} in trials not run.",
                "",
            ]
        )
    lines.extend(
        [
            f"Trials: {coverage['returned_trials']}/{coverage['planned_trials']} "
            f"returned; {coverage['completed_trial_results']} completed, "
            f"{coverage['warmup_failed_results']} warmup failed, "
            f"{coverage['cancelled_results']} cancelled; "
            f"{coverage['aborted_trials']} aborted without a result and "
            f"{coverage['not_run_trials']} not run.",
            "",
            "| Trial | Policy | Status | Offered | SLO successes | "
            "Goodput (requests/s) | SLO fraction |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for trial in report["trials"]:
        measured = trial["foreground"]
        lines.append(
            f"| {trial['trial_id']} | {trial['policy_id']} | {trial['status']} | "
            f"{measured['offered_count']} | {measured['slo_good_count']} | "
            f"{measured['slo_goodput_rps']['decimal']} | "
            f"{measured['slo_success_fraction']['decimal']} |"
        )
    lines.extend(
        [
            "",
            "Paired differences use whole matched trial blocks against "
            "round-robin. Intervals use 2,000 resamples, the predeclared seed, "
            "and 5th/95th percentile ranks for 90% coverage. Fewer than eight "
            "complete blocks withhold intervals; incomplete studies suppress "
            "comparisons.",
            "",
        ]
    )
    for stratum in report["strata"]:
        lines.extend(
            [
                f"## {stratum['profile_id']} / {stratum['scenario']} / "
                f"{stratum['target_endpoint_id'] or 'no fault target'}",
                "",
                f"Complete matched blocks: {stratum['complete_blocks']}/"
                f"{stratum['planned_blocks']}.",
                "",
                "| Policy versus round-robin | Mean difference (requests/s) | "
                "90% interval | Status |",
                "|---|---:|---|---|",
            ]
        )
        for contrast in stratum["paired_contrasts"]:
            mean = contrast["mean_goodput_difference_rps"]
            interval = contrast["interval_rps"]
            interval_text = (
                f"[{interval['lower']['decimal']}, {interval['upper']['decimal']}]"
                if interval
                else "unavailable"
            )
            mean_text = mean["decimal"] if mean else "unavailable"
            lines.append(
                f"| {contrast['policy_id']} | {mean_text} | {interval_text} | "
                f"{contrast['interval_status']} |"
            )
    lines.extend(["", "## Per-trial diagnostics", ""])
    for trial in report["trials"]:
        measured = trial["foreground"]
        outcomes = ", ".join(
            f"{name}={item['count']}"
            for name, item in measured["outcomes"].items()
            if item["count"]
        )
        latency = measured["successful_latency_ns"]
        content = latency["scheduled_to_first_content"]
        terminal = latency["scheduled_to_terminal"]
        usage = measured["usage_success"]
        lines.extend(
            [
                f"- **{trial['trial_id']}**: {outcomes}. "
                f"Dispatched {measured['dispatched_count']}/"
                f"{measured['offered_count']}; fixed window "
                f"{measured['fixed_offered_window_ns']} ns. "
                f"Successful latency population N={latency['count']} "
                "includes SLO misses. "
                f"Scheduled-to-content p50/p95={content['p50']}/{content['p95']} ns; "
                f"scheduled-to-terminal p50/p95={terminal['p50']}/"
                f"{terminal['p95']} ns; p99={terminal['p99']} "
                f"({terminal['p99_status']}). "
                f"Successful usage coverage {usage['reported_count']}/"
                f"{usage['population_count']}; completion-token total "
                f"{usage['completion_tokens_total_decimal']}. "
                f"Recovery: {trial['recovery']['applicability']}; "
                f"dispatch {trial['recovery']['intervals']['dispatch']['status']}.",
            ]
        )
        if trial["background"] is not None:
            background = trial["background"]
            lines.append(
                f"  Background separately: {background['offered_count']} offered, "
                f"{background['dispatched_count']} dispatched, "
                f"{background['successful_count']} successful."
            )
    lines.extend(
        [
            "",
            "p99 is withheld below 1,000 observations; this floor does not "
            "establish tail precision or independence. Content-event timing is "
            "not exact token timing. External model warmup and declared cache "
            "preparation are unmeasured. No live capacity, actual overload or "
            "cost is established.",
            "",
        ]
    )
    return "\n".join(lines)
