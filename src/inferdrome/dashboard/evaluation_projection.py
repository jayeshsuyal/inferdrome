"""Explicit display mapping of validated reports; no statistical reduction."""

from typing import Any, cast

from inferdrome.dashboard.evaluation_report_models import (
    EvaluationCacheDetail,
    EvaluationReportDetail,
    EvaluationStudyDetail,
    EvaluationSummary,
    ReportKind,
    Unit,
)
from inferdrome.domain.base import FrozenModel
from inferdrome.routing_execution.canonical import sha256_digest


def evaluation_report_id(kind: ReportKind, digest: str) -> str:
    return "ev-" + sha256_digest(
        b"inferdrome.evaluation-dashboard.report.v1\0"
        + kind.encode("ascii")
        + b"\0"
        + digest.encode("ascii")
    ).removeprefix("sha256:")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return cast(str, value["decimal"])
    if type(value) is int:
        return str(value)
    assert type(value) is str
    return value


def _metric(key: str, label: str, value: Any, unit: Unit = "count") -> dict[str, Any]:
    return {"key": key, "label": label, "value": _text(value), "unit": unit}


def _population(source: dict[str, Any]) -> dict[str, Any]:
    metrics = [
        _metric(key, label, source[key])
        for key, label in (
            ("offered_count", "Offered requests"),
            ("successful_count", "Successful requests"),
            ("arrival_observed_count", "Observed arrivals"),
            ("dispatched_count", "Actual dispatches"),
            ("peak_active", "Peak active"),
            ("peak_queue", "Peak queue"),
        )
    ]
    metrics.append(
        _metric("elapsed_ns", "Population elapsed", source["elapsed_ns"], "ns")
    )
    if "slo_good_count" in source:
        for key, label, unit in (
            ("slo_good_count", "SLO-good requests", "count"),
            ("slo_success_fraction", "SLO success fraction", "ratio"),
            ("slo_goodput_rps", "SLO goodput", "requests/s"),
            ("offered_rate_rps", "Offered rate", "requests/s"),
            ("dispatch_rate_rps", "Dispatch rate", "requests/s"),
            ("fixed_offered_window_ns", "Fixed offered window", "ns"),
            ("drain_ns", "Drain allowance", "ns"),
            (
                "successful_completions_during_drain",
                "Successful completions in drain",
                "count",
            ),
        ):
            metrics.append(_metric(key, label, source[key], cast(Unit, unit)))
    latency: list[dict[str, Any]] = []

    def series(label: str, population: str, values: dict[str, Any]) -> None:
        latency.append(
            {
                "label": label,
                "population": population,
                "count": values["count"],
                **{f"p{p}_ns": _text(values[f"p{p}"]) for p in (50, 90, 95, 99)},
                "p99_status": values["p99_status"],
            }
        )

    if "successful_latency_ns" in source:
        success = source["successful_latency_ns"]
        for key, label in (
            ("scheduled_to_first_content", "Scheduled arrival to first content"),
            ("scheduled_to_terminal", "Scheduled arrival to terminal"),
            ("dispatch_to_first_content", "Dispatch to first content"),
            ("dispatch_to_terminal", "Dispatch to terminal"),
        ):
            series(label, success["population"], success[key])
        lag = source["dispatch_lag_ns"]
        series("Dispatch lag", lag["population"], lag)
        for outcome, row in source["partial_timing_by_outcome"].items():
            series(
                "Partial first content: " + outcome,
                outcome,
                row["scheduled_to_first_content_ns"],
            )
    usage: list[dict[str, Any]] = []
    for key, label in (
        ("usage_success", "Success usage"),
        ("usage_other_outcomes", "Other outcomes usage"),
    ):
        row = source[key]
        for field, suffix, unit in (
            ("population_count", "population", "count"),
            ("reported_count", "reported", "count"),
            ("missing_count", "missing", "count"),
            ("prompt_tokens_total_decimal", "prompt tokens", "tokens"),
            ("completion_tokens_total_decimal", "completion tokens", "tokens"),
        ):
            usage.append(
                _metric(
                    key + "_" + field,
                    label + " · " + suffix,
                    row[field],
                    cast(Unit, unit),
                )
            )
    return {
        "metrics": metrics,
        "outcomes": [
            {
                "outcome": name,
                "count": row["count"],
                "offered_fraction": row["offered_fraction"]["decimal"],
            }
            for name, row in source["outcomes"].items()
        ],
        "latency": latency,
        "usage": usage,
        "cancelled": source["cancelled"],
    }


_POLICY_LABELS = {
    "evaluation_round_robin_v1": "Round robin",
    "evaluation_least_reported_load_v1": "Least reported load",
    "evaluation_freshness_fallback_v1": "Freshness fallback",
    "evaluation_fail_closed_v1": "Fail closed",
}


def _contrast(row: dict[str, Any]) -> dict[str, Any]:
    labels = {
        "shared_enabled_minus_disabled": "Shared: on minus off",
        "unique_enabled_minus_disabled": "Unique: on minus off",
        "interaction": "Shared contrast minus unique contrast",
    }
    if "contrast" in row:
        label = labels[row["contrast"]]
    else:
        label = (
            _POLICY_LABELS[row["policy_id"]]
            + " minus "
            + _POLICY_LABELS[row["reference_policy_id"]]
        )
    interval = row["interval_rps"]
    return {
        "label": label,
        "complete_blocks": row["complete_blocks"],
        "mean_rps": _text(row["mean_goodput_difference_rps"]),
        "lower_rps": _text(interval["lower"]) if interval else None,
        "upper_rps": _text(interval["upper"]) if interval else None,
        "interval_status": row["interval_status"],
    }


def project_report(
    source: dict[str, Any], *, kind: ReportKind, digest: str, entry: int
) -> EvaluationReportDetail:
    """Accept only the strict reader's model dump; allowlist every public field."""
    returned = (
        source["coverage"]["foreground"]["returned_records"]
        if kind == "STUDY"
        else source["coverage"]["returned_records"]
    )
    summary = EvaluationSummary(
        report_id=evaluation_report_id(kind, digest),
        kind=kind,
        label=("Study report " if kind == "STUDY" else "Prefix-cache report ")
        + str(entry),
        report_sha256=digest,
        plan_sha256=source["plan_sha256"],
        config_sha256=source.get("config_sha256"),
        source_schema=source["schema_version"],
        status=source["status"],
        comparison_status=source["comparative_headline"]
        if kind == "STUDY"
        else source["comparison_status"],
        reason=source.get("reason"),
        calibration=source["calibration"],
        evidence_class=source["evidence_class"],
        returned_records=returned,
    )
    coverage: list[dict[str, Any]] = []
    for key, value in source["coverage"].items():
        # All keys are from the closed report contract, but use fixed labels below.
        if isinstance(value, dict):
            for field, count in value.items():
                coverage.append(
                    _metric(
                        key + "_" + field,
                        _COVERAGE_LABELS[key] + " · " + _COVERAGE_LABELS[field],
                        count,
                    )
                )
        else:
            coverage.append(_metric(key, _COVERAGE_LABELS[key], value))
    if kind == "STUDY":
        return _study(source, summary, coverage)
    return _cache(source, summary, coverage)


_COVERAGE_LABELS = {
    "planned_trials": "Planned trials",
    "started_trials": "Started trials",
    "returned_trials": "Returned trials",
    "aborted_trials": "Aborted trials",
    "not_run_trials": "Not-run trials",
    "completed_trial_results": "Completed trial results",
    "warmup_failed_results": "Warmup-failed results",
    "cancelled_results": "Cancelled results",
    "foreground": "Foreground",
    "background": "Background",
    "planned_offers": "Planned offers",
    "returned_records": "Returned records",
    "offers_in_aborted_trials_without_measurements": (
        "Aborted-trial offers without measurements"
    ),
    "offers_in_not_run_trials": "Not-run offers",
    "planned_cells": "Planned cells",
    "completed_cells": "Completed cells",
    "cancelled_cells": "Cancelled cells",
    "aborted_cells": "Aborted cells",
    "missing_cells": "Missing cells",
    "invalid_cells": "Invalid cells",
    "planned_blocks": "Planned blocks",
    "complete_blocks": "Complete blocks",
    "offers_without_measurements": "Offers without measurements",
}


def _study(
    source: dict[str, Any], summary: EvaluationSummary, coverage: list[dict[str, Any]]
) -> EvaluationStudyDetail:
    profiles: dict[str, int] = {}
    blocks: dict[str, int] = {}
    trials: list[dict[str, Any]] = []
    for index, row in enumerate(source["trials"], 1):
        profile = profiles.setdefault(row["profile_id"], len(profiles) + 1)
        block = blocks.setdefault(row["block_id"], len(blocks) + 1)
        rec = row["recovery"]
        trials.append(
            {
                "index": index,
                "block_index": block,
                "profile_index": profile,
                "policy_id": row["policy_id"],
                "scenario": row["scenario"],
                "status": row["status"],
                "result_sha256": row["result_sha256"],
                "metrics": [
                    _metric(key, label, row[key], "ns")
                    for key, label in (
                        ("window_start_ns", "Offered window start"),
                        ("window_end_ns", "Offered window end"),
                        ("first_content_slo_ns", "First-content SLO"),
                        ("completion_slo_ns", "Completion SLO"),
                        ("trial_elapsed_ns", "Owned trial elapsed"),
                    )
                ],
                "foreground": _population(row["foreground"]),
                "background": _population(row["background"])
                if row["background"] is not None
                else None,
                "recovery": {
                    "applicability": rec["applicability"],
                    "origin": rec["origin"],
                    "planned_restore_ns": _text(rec["planned_restore_ns"]),
                    "actual_restore_ns": _text(rec["actual_restore_ns"]),
                    "background_active_at_restore": rec["background_active_at_restore"],
                    "intervals": [
                        {
                            "metric": name,
                            "status": value["status"],
                            "duration_ns": _text(value["duration_ns"]),
                            "observation_horizon_ns": _text(
                                value["observation_horizon_ns"]
                            ),
                        }
                        for name, value in rec["intervals"].items()
                    ],
                },
            }
        )
    strata: list[dict[str, Any]] = []
    for index, row in enumerate(source["strata"], 1):
        strata.append(
            {
                "index": index,
                "scenario": row["scenario"],
                "profile_index": profiles.setdefault(
                    row["profile_id"], len(profiles) + 1
                ),
                "target_endpoint": row["target_endpoint_id"],
                "metrics": [
                    _metric(key, label, row[key], cast(Unit, unit))
                    for key, label, unit in (
                        ("planned_blocks", "Planned blocks", "blocks"),
                        ("complete_blocks", "Complete blocks", "blocks"),
                        ("fixed_window_ns", "Fixed offered window", "ns"),
                    )
                ],
                "policies": [
                    {
                        "policy_id": policy["policy_id"],
                        "metrics": [
                            _metric(key, label, policy[key], cast(Unit, unit))
                            for key, label, unit in (
                                ("returned_trials", "Returned trials", "count"),
                                ("completed_trials", "Completed trials", "count"),
                                ("mean_goodput_rps", "Mean SLO goodput", "requests/s"),
                                (
                                    "observed_min_goodput_rps",
                                    "Observed minimum SLO goodput",
                                    "requests/s",
                                ),
                                (
                                    "observed_max_goodput_rps",
                                    "Observed maximum SLO goodput",
                                    "requests/s",
                                ),
                            )
                        ],
                    }
                    for policy in row["policies"]
                ],
                "contrasts": [_contrast(value) for value in row["paired_contrasts"]],
            }
        )
    data = {
        "summary": summary,
        "coverage": coverage,
        "strata": strata,
        "trials": trials,
        "reporting": [
            _metric(
                "study_elapsed_ns",
                "Whole study operation elapsed",
                source["study_elapsed_ns"],
                "ns",
            ),
            _metric(
                "duplicate_payload_hash_groups",
                "Duplicate payload hash groups",
                source["duplicate_payload_hash_groups"],
            ),
        ],
        "limitations": source["limitations"],
    }
    # JSON-mode validation accepts source arrays while retaining strict scalar types.
    return _validate_view(EvaluationStudyDetail, data)


def _cache(
    source: dict[str, Any], summary: EvaluationSummary, coverage: list[dict[str, Any]]
) -> EvaluationCacheDetail:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in source["cells"]:
        assignment = []
        for item in row["endpoint_assignment"] or ():
            endpoint = "A" if item["endpoint_id"] == "endpoint-a" else "B"
            for field, label in (
                ("planned_offers", "planned offers"),
                ("actual_dispatches", "actual dispatches"),
            ):
                assignment.append(
                    _metric(
                        "endpoint_" + endpoint.lower() + "_" + field,
                        "Endpoint " + endpoint + " " + label,
                        item[field],
                    )
                )
        output: list[dict[str, Any]] = []
        for name, lengths in (row["reported_output_lengths"] or {}).items():
            label = "Success" if name == "success" else "Other outcomes"
            for key, suffix, unit in (
                ("population_count", "population", "count"),
                ("reported_count", "usage reported", "count"),
                ("missing_count", "usage missing", "count"),
                ("total_decimal", "completion tokens", "tokens"),
                ("mean_decimal", "mean completion tokens", "tokens"),
                ("p50", "p50 completion tokens", "tokens"),
                ("p90", "p90 completion tokens", "tokens"),
                ("p95", "p95 completion tokens", "tokens"),
            ):
                output.append(
                    _metric(
                        name + "_" + key,
                        label + " " + suffix,
                        lengths[key],
                        cast(Unit, unit),
                    )
                )
        grouped.setdefault(row["block_id"], []).append(
            {
                "index": row["index"] + 1,
                "condition": row["condition"],
                "workload_family": row["workload_family"],
                "mode": "DECLARED_ENABLED"
                if row["prefix_enabled"]
                else "DECLARED_DISABLED",
                "status": row["status"],
                "reason": row["reason"],
                "cleanup": row["cleanup"],
                "config_sha256": row["config_sha256"],
                "result_sha256": row["result_sha256"],
                "declarations_consistent": row["attribution_eligible"],
                "declaration_reasons": row["attribution_reasons"],
                "metrics": [
                    _metric("planned_offers", "Planned offers", row["planned_offers"]),
                    _metric(
                        "elapsed_ns", "Owned cell elapsed", row["elapsed_ns"], "ns"
                    ),
                ],
                "population": _population(row["population"])
                if row["population"] is not None
                else None,
                "assignment": assignment,
                "output_lengths": output,
            }
        )
    prefixes = {row["block_id"]: row for row in source["prefix_potential"] or ()}
    lengths = {row["block_id"]: row for row in source["output_length_differences"]}
    blocks = []
    for index, (block_id, cells) in enumerate(grouped.items(), 1):
        prefix = prefixes.get(block_id)
        potential = (
            []
            if prefix is None
            else [
                _metric(key, label, prefix[key], cast(Unit, unit))
                for key, label, unit in (
                    ("wrapper_overlap_tokens", "Common wrapper overlap", "tokens"),
                    ("shared_lcp_tokens", "Shared complete-prompt prefix", "tokens"),
                    ("shared_document_lcp_tokens", "Shared document prefix", "tokens"),
                    (
                        "unique_max_pairwise_lcp_tokens",
                        "Unique-arm maximum prefix overlap",
                        "tokens",
                    ),
                    (
                        "shared_document_complete_prefix_blocks",
                        "Shared document complete matching blocks",
                        "blocks",
                    ),
                    (
                        "unique_max_complete_prefix_blocks",
                        "Unique-arm complete matching blocks",
                        "blocks",
                    ),
                    (
                        "extra_shared_prefix_blocks",
                        "Additional shared matching blocks",
                        "blocks",
                    ),
                )
            ]
        )
        length = lengths[block_id]
        blocks.append(
            {
                "index": index,
                "cells": sorted(cells, key=lambda cell: cell["condition"]),
                "prefix_potential": potential,
                "output_length_differences": [
                    _metric(
                        "shared_mean_tokens_difference",
                        "Shared on-minus-off successful output mean",
                        length["shared_mean_tokens_difference_decimal"],
                        "tokens",
                    ),
                    _metric(
                        "unique_mean_tokens_difference",
                        "Unique on-minus-off successful output mean",
                        length["unique_mean_tokens_difference_decimal"],
                        "tokens",
                    ),
                ],
            }
        )
    reporting = [
        _metric(key, label, source["reporting"][key], cast(Unit, unit))
        for key, label, unit in (
            ("fixed_offered_window_ns", "Fixed offered window", "ns"),
            ("first_content_slo_ns", "First-content SLO", "ns"),
            ("completion_slo_ns", "Completion SLO", "ns"),
            ("cache_block_size", "Declared cache block size", "tokens"),
            ("p99_min_successes", "P99 reporting floor", "count"),
            ("minimum_complete_blocks", "Interval complete-block minimum", "blocks"),
        )
    ]
    reporting.append(
        _metric(
            "sum_returned_owned_elapsed_ns",
            "Owned cell elapsed sum; external preparation excluded",
            source["sum_returned_owned_elapsed_ns"],
            "ns",
        )
    )
    return _validate_view(
        EvaluationCacheDetail,
        {
            "summary": summary,
            "coverage": coverage,
            "reporting": reporting,
            "limitations": source["limitations"],
            "workload_verification": source["workload_verification"],
            "low_replication": source["low_replication"],
            "preparation_issues": source["preparation_consistency_issues"],
            "blocks": blocks,
            "contrasts": [_contrast(row) for row in source["contrasts"]],
        },
    )


def _validate_view[T: FrozenModel](model: type[T], data: dict[str, Any]) -> T:
    from pydantic_core import to_json

    return model.model_validate_json(to_json(data))
