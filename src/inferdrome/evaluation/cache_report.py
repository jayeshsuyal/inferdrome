"""Bounded offline four-cell cache reporting without runtime attribution claims."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.study_files import MAX_METADATA_BYTES, StudyDirectory
from inferdrome.evaluation.study_report import _signed_ratio, summarize_population
from inferdrome.metrics.quantiles import decimal_ratio, nearest_rank
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

if TYPE_CHECKING:
    from inferdrome.evaluation.cache import ValidatedCacheCell
    from inferdrome.evaluation.cache_config import CompiledCacheCell, CompiledCachePlan

_CONDITIONS = ("S0", "S1", "U0", "U1")
_CONTRASTS = (
    "shared_enabled_minus_disabled",
    "unique_enabled_minus_disabled",
    "interaction",
)
_RESAMPLES = 2000
_INTERVAL_BLOCKS = 8


@dataclass(frozen=True)
class _PreparationSummary:
    preparation_id: str
    runtime_recipe_sha256: str | None
    method: str
    generations: tuple[tuple[str, str | None], ...]


def _require(condition: bool) -> None:
    if not condition:
        raise EvaluationError("cache report input violates the compiled plan")


def _cell_identity(cell: CompiledCacheCell) -> dict[str, Any]:
    return {
        "index": cell.index,
        "cell_id": cell.cell_id,
        "block_id": cell.block_id,
        "condition": cell.condition,
        "workload_family": cell.workload_family,
        "prefix_enabled": cell.prefix_enabled,
        "attempt_id": cell.attempt_id,
        "cell_sha256": cell.cell_sha256,
        "config_sha256": cell.config_sha256,
        "workload_sha256": cell.workload_sha256,
        "planned_offers": len(cell.config.offers),
    }


def _unavailable(cell: CompiledCacheCell, reason: str) -> dict[str, Any]:
    return {
        **_cell_identity(cell),
        "status": "UNAVAILABLE",
        "reason": reason,
        "cleanup": "UNCONFIRMED" if reason == "INVALID_CELL_INPUT" else "NOT_STARTED",
        "preparation_sha256": None,
        "result_sha256": None,
        "evidence_class": None,
        "attribution_eligible": False,
        "attribution_reasons": [reason],
        "elapsed_ns": None,
        "population": None,
        "endpoint_assignment": None,
        "reported_output_lengths": None,
    }


def _summarize_cell(
    cell: CompiledCacheCell, validated: ValidatedCacheCell
) -> tuple[dict[str, Any], _PreparationSummary]:
    _require(
        validated.cell_id == cell.cell_id and validated.attempt_id == cell.attempt_id
    )
    result = validated.result
    population = (
        summarize_population(
            cell.config,
            result,
            window_start_ns=cell.window_start_ns,
            window_end_ns=cell.window_end_ns,
            first_content_slo_ns=cell.first_content_slo_ns,
            completion_slo_ns=cell.completion_slo_ns,
        )
        if result is not None
        else None
    )
    assignment = None
    output_lengths = None
    if result is not None:
        assignment = [
            {
                "endpoint_id": endpoint.endpoint_id,
                "planned_offers": sum(
                    offer.endpoint_id == endpoint.endpoint_id
                    for offer in cell.config.offers
                ),
                "actual_dispatches": sum(
                    row.endpoint_id == endpoint.endpoint_id and row.attempts == 1
                    for row in result.records
                ),
            }
            for endpoint in cell.config.endpoints
        ]
        output_lengths = {}
        for label, success in (("success", True), ("other_outcomes", False)):
            records = tuple(
                row for row in result.records if (row.outcome == "SUCCESS") == success
            )
            values = tuple(
                row.completion_tokens
                for row in records
                if row.completion_tokens is not None
            )
            output_lengths[label] = {
                "population": "SERVER_REPORTED_COMPLETION_TOKENS",
                "population_count": len(records),
                "reported_count": len(values),
                "missing_count": len(records) - len(values),
                "total_decimal": str(sum(values)),
                "mean_decimal": decimal_ratio(sum(values), len(values))
                if values
                else None,
                "p50": nearest_rank(values, 50) if values else None,
                "p90": nearest_rank(values, 90) if values else None,
                "p95": nearest_rank(values, 95) if values else None,
            }
    preparation = validated.preparation
    return (
        {
            **_cell_identity(cell),
            "status": validated.status,
            "reason": validated.reason,
            "cleanup": validated.cleanup,
            "preparation_sha256": validated.preparation_sha256,
            "result_sha256": validated.result_sha256,
            "evidence_class": validated.evidence_class,
            "attribution_eligible": validated.attribution_eligible,
            "attribution_reasons": list(validated.attribution_reasons),
            "elapsed_ns": validated.elapsed_ns,
            "population": population,
            "endpoint_assignment": assignment,
            "reported_output_lengths": output_lengths,
        },
        _PreparationSummary(
            preparation.preparation_id,
            preparation.runtime_recipe_sha256,
            preparation.method,
            tuple(
                (endpoint.endpoint_id, endpoint.process_generation_sha256)
                for endpoint in preparation.endpoints
            ),
        ),
    )


def _preparation_issues(
    preparations: list[_PreparationSummary],
) -> list[str]:
    issues: list[str] = []
    identifiers = [item.preparation_id for item in preparations]
    if len(set(identifiers)) != len(identifiers):
        issues.append("REUSED_PREPARATION_ID")
    recipes = {item.runtime_recipe_sha256 for item in preparations}
    if None in recipes or len(recipes) > 1:
        issues.append("RUNTIME_RECIPE_UNRESOLVED_OR_MISMATCHED")
    if len({item.method for item in preparations}) > 1:
        issues.append("PREPARATION_METHOD_MISMATCH")
    seen: set[str] = set()
    for item in preparations:
        if item.method != "DECLARED_FRESH_PROCESSES":
            continue
        for _, generation in item.generations:
            if generation is None:
                issues.append("PROCESS_GENERATION_UNRESOLVED")
            elif generation in seen:
                issues.append("REUSED_FRESH_PROCESS_GENERATION")
            else:
                seen.add(generation)
    return sorted(set(issues))


def _paired_contrasts(
    blocks: list[dict[str, dict[str, Any]]],
    *,
    window_ns: int,
    seed: int,
    available: bool,
) -> list[dict[str, Any]]:
    values: dict[str, list[int]] = {name: [] for name in _CONTRASTS}
    for block in blocks:
        good = {
            condition: block[condition]["population"]["slo_good_count"]
            for condition in _CONDITIONS
        }
        shared, unique = good["S1"] - good["S0"], good["U1"] - good["U0"]
        for name, value in zip(
            _CONTRASTS, (shared, unique, shared - unique), strict=True
        ):
            values[name].append(value)
    n = len(blocks)
    draws: dict[str, list[int]] = {name: [] for name in _CONTRASTS}
    if available and n >= _INTERVAL_BLOCKS:
        generator = random.Random(seed)
        for _ in range(_RESAMPLES):
            indexes = [generator.randrange(n) for _ in range(n)]
            for name in _CONTRASTS:
                draws[name].append(sum(values[name][index] for index in indexes))
    return [
        {
            "contrast": name,
            "replication_unit": "MATCHED_FOUR_CELL_BLOCK",
            "complete_blocks": n,
            "mean_goodput_difference_rps": _signed_ratio(
                sum(values[name]) * 1_000_000_000, n * window_ns
            )
            if available and n
            else None,
            "block_goodput_differences_rps": [
                _signed_ratio(value * 1_000_000_000, window_ns)
                for value in values[name]
            ]
            if available
            else None,
            "interval_status": "SUPPRESSED_COMPARISON"
            if not available
            else "INSUFFICIENT_COMPLETE_BLOCKS"
            if n < _INTERVAL_BLOCKS
            else "DESCRIPTIVE_ONLY",
            "interval_rps": {
                "lower": _signed_ratio(
                    nearest_rank(tuple(draws[name]), 5) * 1_000_000_000,
                    n * window_ns,
                ),
                "upper": _signed_ratio(
                    nearest_rank(tuple(draws[name]), 95) * 1_000_000_000,
                    n * window_ns,
                ),
            }
            if draws[name]
            else None,
        }
        for name in _CONTRASTS
    ]


def _output_length_differences(
    block: dict[str, dict[str, Any]], *, available: bool
) -> dict[str, Any]:
    """Contrast covered successful-output means without imputing missing usage."""
    result: dict[str, Any] = {
        "population": "FULLY_COVERED_SERVER_REPORTED_SUCCESS_MEANS",
        "shared_mean_tokens_difference_decimal": None,
        "unique_mean_tokens_difference_decimal": None,
        "status": "SUPPRESSED_COMPARISON" if not available else "UNAVAILABLE_USAGE",
    }
    if not available:
        return result
    usage = {
        condition: block[condition]["reported_output_lengths"]["success"]
        for condition in _CONDITIONS
    }
    if any(row["missing_count"] or not row["reported_count"] for row in usage.values()):
        return result
    for family, label in (("S", "shared"), ("U", "unique")):
        off, on = usage[family + "0"], usage[family + "1"]
        numerator = (
            int(on["total_decimal"]) * off["reported_count"]
            - int(off["total_decimal"]) * on["reported_count"]
        )
        result[label + "_mean_tokens_difference_decimal"] = _signed_ratio(
            numerator, on["reported_count"] * off["reported_count"]
        )["decimal"]
    result["status"] = "DESCRIPTIVE_SUCCESS_CONDITIONAL_NOT_MATCHED_OUTPUTS"
    return result


def _assemble_report(
    plan: CompiledCachePlan,
    rows: list[dict[str, Any]],
    preparations: list[_PreparationSummary],
    *,
    plan_sha256: str,
) -> dict[str, Any]:
    _require(len(rows) == len(plan.cells) <= 32)
    statuses = Counter(row["status"] for row in rows)
    issues = _preparation_issues(preparations)
    complete = all(row["status"] == "COMPLETED" for row in rows)
    evidence = {
        row["evidence_class"] for row in rows if row["evidence_class"] is not None
    }
    expected_class = "SYNTHETIC_ONLY" if plan.synthetic else "LOCAL_MEASUREMENT_ONLY"
    evidence_consistent = not evidence or evidence == {expected_class}
    if not evidence_consistent:
        issues.append("EVIDENCE_CLASS_MISMATCH")
    preparation_consistent = not issues and all(
        row["attribution_eligible"] for row in rows
    )
    available = complete and preparation_consistent and evidence_consistent
    blocks: dict[str, dict[str, dict[str, Any]]] = {}
    for cell, row in zip(plan.cells, rows, strict=True):
        _require(row["cell_id"] == cell.cell_id)
        block = blocks.setdefault(cell.block_id, {})
        _require(cell.condition not in block)
        block[cell.condition] = row
    _require(all(set(block) == set(_CONDITIONS) for block in blocks.values()))
    full = [
        block
        for block in blocks.values()
        if all(row["status"] == "COMPLETED" for row in block.values())
    ]
    windows = {cell.window_end_ns - cell.window_start_ns for cell in plan.cells}
    _require(len(windows) == 1 and next(iter(windows)) > 0)
    window = next(iter(windows))
    measured = [row for row in rows if row["population"] is not None]
    return {
        "schema_version": "inferdrome.evaluation-cache-report.v1",
        "plan_sha256": plan_sha256,
        "status": "COMPLETED" if complete else "INCOMPLETE",
        "comparison_status": "AVAILABLE"
        if available
        else "SUPPRESSED_INCOMPLETE"
        if not complete
        else "SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH",
        "comparative_headline": "SYNTHETIC_ONLY_DESCRIPTIVE_CONTRAST"
        if available and plan.synthetic
        else "DECLARED_CONDITION_DESCRIPTIVE_CONTRAST"
        if available
        else "SUPPRESSED",
        "low_replication": len(full) < _INTERVAL_BLOCKS,
        "evidence_class": next(iter(evidence)) if len(evidence) == 1 else None,
        "planned_evidence_class": expected_class,
        "evidence_classes": sorted(evidence),
        "evidence_eligible": False,
        "runtime_verification": "UNVERIFIED",
        "cache_treatment_attribution": "UNVERIFIED",
        "workload_verification": plan.verification_status,
        "prefix_potential": [
            {
                "block_id": plan.config.blocks[family.family_index].block_id,
                "input_sha256": family.input_sha256,
                "shared_lcp_tokens": family.shared_lcp_tokens,
                "shared_document_lcp_tokens": family.shared_document_lcp_tokens,
                "shared_document_complete_prefix_blocks": (
                    family.shared_document_complete_prefix_blocks
                ),
                "shared_lcp_token_ids_sha256": family.shared_lcp_token_ids_sha256,
                "shared_document_lcp_token_ids_sha256": (
                    family.shared_document_lcp_token_ids_sha256
                ),
                "unique_max_pairwise_lcp_tokens": family.unique_max_pairwise_lcp_tokens,
                "wrapper_overlap_tokens": family.wrapper_overlap_tokens,
                "shared_complete_prefix_blocks": family.shared_complete_prefix_blocks,
                "unique_max_complete_prefix_blocks": (
                    family.unique_max_complete_prefix_blocks
                ),
                "extra_shared_prefix_blocks": family.extra_shared_prefix_blocks,
                "claim": "TOKEN_PREFIX_POTENTIAL_NOT_CACHE_HITS",
            }
            for family in plan.verification.families
        ]
        if plan.verification is not None
        else None,
        "calibration": "UNAVAILABLE",
        "cost": "UNAVAILABLE",
        "cache_hits": "UNAVAILABLE",
        "external_preparation_elapsed_ns": None,
        "external_preparation_requests": None,
        "external_preparation_tokens": None,
        "model_warmup": "EXTERNALLY_PREPARED_UNMEASURED",
        "sum_returned_owned_elapsed_ns": sum(
            row["elapsed_ns"] for row in rows if row["elapsed_ns"] is not None
        ),
        "elapsed_semantics": (
            "SUM_OF_OWNED_CELL_RUNS_NOT_EXTERNAL_PREPARATION_OR_WALL_SPAN"
        ),
        "coverage": {
            "planned_cells": len(plan.cells),
            "completed_cells": statuses["COMPLETED"],
            "cancelled_cells": statuses["CANCELLED"],
            "aborted_cells": statuses["ABORTED"],
            "missing_cells": sum(row["reason"] == "MISSING_CELL_INPUT" for row in rows),
            "invalid_cells": sum(row["reason"] == "INVALID_CELL_INPUT" for row in rows),
            "planned_blocks": len(blocks),
            "complete_blocks": len(full),
            "planned_offers": sum(row["planned_offers"] for row in rows),
            "returned_records": sum(
                row["population"]["offered_count"] for row in measured
            ),
            "offers_without_measurements": sum(
                row["planned_offers"] for row in rows if row["population"] is None
            ),
        },
        "preparation_consistency_issues": issues,
        "reporting": {
            "fixed_offered_window_ns": window,
            "first_content_slo_ns": plan.config.profile.first_content_slo_ns,
            "completion_slo_ns": plan.config.profile.completion_slo_ns,
            "cache_block_size": plan.config.cache_block_size,
            "p99_min_successes": 1000,
            "bootstrap_resamples": _RESAMPLES,
            "bootstrap_seed": plan.config.reporting.bootstrap_seed,
            "confidence_percent": 90,
            "percentile_ranks": [5, 95],
            "minimum_complete_blocks": _INTERVAL_BLOCKS,
        },
        "comparison_block_ids": list(blocks) if available else [],
        "contrasts": _paired_contrasts(
            full,
            window_ns=window,
            seed=plan.config.reporting.bootstrap_seed,
            available=available,
        ),
        "output_length_differences": [
            {
                "block_id": block_id,
                **_output_length_differences(block, available=available),
            }
            for block_id, block in blocks.items()
        ],
        "cells": rows,
        "limitations": [
            "DECLARATIONS_AND_DIGESTS_DO_NOT_ATTEST_RUNTIME_OR_INDEPENDENT_EXECUTION",
            "ALL_OFFERED_SCHEDULED_ORIGIN_SLOS_WITH_FIXED_WINDOW",
            "MISSING_OR_INVALID_CELLS_ARE_UNAVAILABLE_NOT_ZERO",
            "WHOLE_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS",
            "INTERACTION_IS_NOT_A_PREFILL_ONLY_OR_CACHE_HIT_MEASUREMENT",
            "GENERATED_LENGTHS_USE_ONLY_VALID_SERVER_USAGE_WITH_EXPLICIT_COVERAGE",
            "EXTERNAL_PREPARATION_AND_BETWEEN_CELL_TIME_ARE_UNMEASURED",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the bounded, sanitized cache report without private preparation data."""
    classes = ", ".join(report["evidence_classes"]) or "NO_RETURNED_EVIDENCE"
    lines = [
        "# Prefix-cache experiment",
        "",
        f"Status: **{report['status']}**. "
        f"Comparison: **{report['comparison_status']}**.",
        f"Plan evidence: **{report['planned_evidence_class']}**. "
        f"Returned evidence: **{classes}**; evidence eligible: **false**. "
        "Runtime and cache-treatment attribution: **UNVERIFIED**.",
        "",
        "All offered requests remain in N. G counts successful requests meeting both "
        "scheduled-origin SLOs; goodput is G divided by the fixed offered window. "
        "Missing and invalid cells are unavailable. Cache hits, calibration and cost "
        "are unavailable.",
        "",
        f"First-content SLO: {report['reporting']['first_content_slo_ns']} ns; "
        f"completion SLO: {report['reporting']['completion_slo_ns']} ns. "
        f"Fixed offered window: {report['reporting']['fixed_offered_window_ns']} ns.",
        "",
    ]
    coverage = report["coverage"]
    for family in report["prefix_potential"] or []:
        lines.extend(
            [
                f"{family['block_id']} token-prefix potential: "
                f"{family['shared_document_lcp_tokens']} document-prefix tokens, "
                f"{family['shared_document_complete_prefix_blocks']} complete "
                "document-prefix blocks; "
                f"{family['unique_max_complete_prefix_blocks']} "
                "maximum unique-prefix blocks; "
                f"{family['extra_shared_prefix_blocks']} additional document blocks. "
                "These counts do not measure cache hits.",
                "",
            ]
        )
    lines.extend(
        [
            f"Cells: {coverage['completed_cells']}/{coverage['planned_cells']} "
            f"completed; {coverage['cancelled_cells']} cancelled, "
            f"{coverage['aborted_cells']} aborted, "
            f"{coverage['missing_cells']} missing, "
            f"{coverage['invalid_cells']} invalid.",
            f"Offers: {coverage['planned_offers']} planned, "
            f"{coverage['returned_records']} returned records, "
            f"{coverage['offers_without_measurements']} without measurements.",
            "",
            "| Cell | Block | Condition | Status | N | G | "
            "Goodput (requests/s) | G/N |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for cell in report["cells"]:
        population = cell["population"]
        values = (
            f"{population['offered_count']} | {population['slo_good_count']} | "
            f"{population['slo_goodput_rps']['decimal']} | "
            f"{population['slo_success_fraction']['decimal']}"
            if population
            else "unavailable | unavailable | unavailable | unavailable"
        )
        lines.append(
            f"| {cell['cell_id']} | {cell['block_id']} | {cell['condition']} | "
            f"{cell['status']} | {values} |"
        )
    lines.extend(
        [
            "",
            "The shared and unique deltas subtract disabled from enabled. "
            "The interaction subtracts the unique delta from the shared delta. "
            "All four cells remain together in each resampled block. "
            "Descriptive 90% intervals use 2,000 resamples and nearest-rank "
            "5th/95th bounds; fewer than eight complete blocks withhold intervals.",
            f"Bootstrap seed: {report['reporting']['bootstrap_seed']}.",
            "",
            "| Contrast | Mean difference (requests/s) | 90% interval | Status |",
            "|---|---:|---|---|",
        ]
    )
    for contrast in report["contrasts"]:
        mean, interval = (
            contrast["mean_goodput_difference_rps"],
            contrast["interval_rps"],
        )
        bounds = (
            f"[{interval['lower']['decimal']}, {interval['upper']['decimal']}]"
            if interval
            else "unavailable"
        )
        lines.append(
            f"| {contrast['contrast']} | "
            f"{mean['decimal'] if mean else 'unavailable'} | "
            f"{bounds} | {contrast['interval_status']} |"
        )
    for difference in report["output_length_differences"]:
        lines.extend(
            [
                "",
                f"{difference['block_id']} enabled-minus-disabled mean reported "
                "successful completion tokens: "
                f"shared={difference['shared_mean_tokens_difference_decimal']}, "
                f"unique={difference['unique_mean_tokens_difference_decimal']} "
                f"({difference['status']}). These are success-conditional means; "
                "the successful request sets can differ.",
            ]
        )
    for cell in report["cells"]:
        lines.extend(["", f"## {cell['cell_id']} diagnostics", ""])
        reasons = [cell["reason"]] if cell["reason"] else []
        reasons += cell["attribution_reasons"]
        lines.append("Reasons: " + (", ".join(dict.fromkeys(reasons)) or "none") + ".")
        if cell["population"] is None:
            continue
        population = cell["population"]
        lines.append(
            "Outcomes: "
            + ", ".join(
                f"{name}={value['count']} ({value['offered_fraction']['decimal']})"
                for name, value in population["outcomes"].items()
            )
            + "."
        )
        for name, value in population["successful_latency_ns"].items():
            if not isinstance(value, dict):
                continue
            lines.append(
                f"Success {name}: count={value['count']}; p50={value['p50']}; "
                f"p90={value['p90']}; p95={value['p95']}; p99={value['p99']} "
                f"({value['p99_status']})."
            )
        lag = population["dispatch_lag_ns"]
        lines.append(
            f"All-dispatched lag: count={lag['count']}; p50={lag['p50']}; "
            f"p90={lag['p90']}; p95={lag['p95']} ns."
        )
        for outcome, partial in population["partial_timing_by_outcome"].items():
            timing = partial["scheduled_to_first_content_ns"]
            if timing["count"]:
                lines.append(
                    f"{outcome} partial first-content observations: "
                    f"count={timing['count']}; p50={timing['p50']}; "
                    f"p95={timing['p95']} ns."
                )
        for assignment in cell["endpoint_assignment"]:
            lines.append(
                f"{assignment['endpoint_id']}: {assignment['planned_offers']} planned, "
                f"{assignment['actual_dispatches']} dispatched."
            )
        for label, usage in cell["reported_output_lengths"].items():
            lines.append(
                f"{label} server-reported completion tokens: "
                f"{usage['reported_count']}/{usage['population_count']} covered, "
                f"{usage['missing_count']} missing; mean={usage['mean_decimal']}, "
                f"total={usage['total_decimal']}."
            )
    lines.extend(
        [
            "",
            "Preparation consistency issues: "
            + (", ".join(report["preparation_consistency_issues"]) or "none")
            + ".",
            f"Owned cell elapsed sum: {report['sum_returned_owned_elapsed_ns']} ns. "
            "External preparation and the wall-clock span between invocations "
            "are unmeasured and do not change the goodput divisor.",
            "",
        ]
    )
    return "\n".join(lines)


def report_cache_experiment(
    plan: CompiledCachePlan,
    cell_inputs: Mapping[str, Path],
    output_dir: Path,
) -> dict[str, Any]:
    """Load one explicitly named cell at a time and write a new private report."""
    from inferdrome.evaluation.cache import cache_plan_bytes

    _require(
        len(cell_inputs) <= len(plan.cells) <= 32
        and set(cell_inputs) <= {cell.cell_id for cell in plan.cells}
        and all(isinstance(path, Path) for path in cell_inputs.values())
    )
    rows: list[dict[str, Any]] = []
    preparations: list[_PreparationSummary] = []
    for cell in plan.cells:
        path = cell_inputs.get(cell.cell_id)
        if path is None:
            rows.append(_unavailable(cell, "MISSING_CELL_INPUT"))
            continue
        try:
            row, preparation = _read_cell_summary(plan, cell, path)
        except (EvaluationError, OSError):
            rows.append(_unavailable(cell, "INVALID_CELL_INPUT"))
            continue
        rows.append(row)
        preparations.append(preparation)
    report = _assemble_report(
        plan, rows, preparations, plan_sha256=sha256_digest(cache_plan_bytes(plan))
    )
    encoded = canonical_json_bytes(report) + b"\n"
    markdown = render_markdown(report).encode("utf-8")
    _require(len(encoded) <= MAX_METADATA_BYTES and len(markdown) <= MAX_METADATA_BYTES)
    with StudyDirectory.create(output_dir, budget=2 * MAX_METADATA_BYTES) as output:
        output.write("report.json", encoded, limit=MAX_METADATA_BYTES)
        output.write("report.md", markdown, limit=MAX_METADATA_BYTES)
    return report


def _read_cell_summary(
    plan: CompiledCachePlan, cell: CompiledCacheCell, path: Path
) -> tuple[dict[str, Any], _PreparationSummary]:
    # Keep the raw validated result inside this frame, including on a failed
    # summary, so the next file is opened only after that population is released.
    from inferdrome.evaluation.cache import load_cache_cell

    return _summarize_cell(cell, load_cache_cell(plan, cell, path))
