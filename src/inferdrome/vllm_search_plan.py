"""Finite, deterministic timing searches; preparation never executes a trial."""

from __future__ import annotations

import hashlib
import itertools
import re
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    MAX_EXACT_INTEGER,
    _artifact_json_types,
    make_timing,
)
from inferdrome.vllm_paired_protocol import (
    MAX_MINIMUM_EFFECT_MICRORPS,
    _common_plan,
    make_protocol,
)
from inferdrome.vllm_router_study import POLICIES

SCHEMA = "inferdrome.vllm-router-search-plan.v1"
ORDER_METHOD = (
    "MINIMUM_APPLIED_ADVANCE_THEN_TOTAL_THEN_MOVED_OFFERS_THEN_SCHEDULE_SHA256"
)
STOP_RULE = "FIRST_REVERSAL_CANDIDATE_OR_GRID_OR_BUDGET_EXHAUSTED"
MAX_GRID_RECIPES = 128
MAX_GRID_OFFER_WORK = 2_000_000
_COMPARISON_FIELDS = {
    "policy_a",
    "policy_b",
    "order_seed",
    "minimum_effect_microrps",
    "max_scheduling_lag_p95_ns",
    "max_client_queue_p95_ns",
    "model",
    "source_revision",
    "environment_sha256",
    "reset_procedure_sha256",
}


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _axis(value: object, low: int, high: int, label: str) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError(f"{label} must contain one to sixteen values")
    checked = [_integer(item, low, high, label) for item in value]
    if len(set(checked)) != len(checked):
        raise ValueError(f"{label} contains duplicate values")
    return sorted(checked)


def _comparison_options(options: dict[str, Any]) -> None:
    """Check the unchanged PR3 argument contract even for an entirely no-op grid."""
    if not isinstance(options, dict) or set(options) != _COMPARISON_FIELDS:
        raise ValueError("search comparison options fields are invalid")
    _artifact_json_types(options)
    if (
        options["policy_a"] not in POLICIES
        or options["policy_b"] not in POLICIES
        or options["policy_a"] == options["policy_b"]
    ):
        raise ValueError("search comparison requires two supported distinct policies")
    _integer(options["order_seed"], 0, 2**32 - 1, "order_seed")
    _integer(
        options["minimum_effect_microrps"],
        0,
        MAX_MINIMUM_EFFECT_MICRORPS,
        "minimum_effect_microrps",
    )
    for field in ("max_scheduling_lag_p95_ns", "max_client_queue_p95_ns"):
        _integer(options[field], 0, MAX_EXACT_INTEGER, field)
    model = options["model"]
    if (
        not isinstance(model, str)
        or not 1 <= len(model) <= 200
        or model.strip() != model
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise ValueError("search model declaration is invalid")
    source = options["source_revision"]
    if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise ValueError("search source revision declaration is invalid")
    for field in ("environment_sha256", "reset_procedure_sha256"):
        value = options[field]
        if (
            not isinstance(value, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        ):
            raise ValueError(f"search {field} declaration is invalid")


def _source_plans(plans: list[dict[str, Any]]) -> None:
    common: bytes | None = None
    seeds: set[int] = set()
    for plan in plans:
        make_timing(plan, group_size=1, retained_spacing_bps=10_000, max_advance_ns=0)
        if plan["phase"] not in ("fixture", "evaluation"):
            raise ValueError("search excludes calibration and pilot plans")
        source_contract = _common_plan(plan)
        if common is None:
            common = source_contract
        elif source_contract != common:
            raise ValueError("search source plans differ beyond allowed block fields")
        if plan["seed"] in seeds:
            raise ValueError("search source plans require distinct workload seeds")
        seeds.add(plan["seed"])


def make_search_plan(
    plans: list[dict[str, Any]],
    *,
    comparison_options: dict[str, Any],
    group_sizes: list[int],
    retained_spacing_bps: list[int],
    max_advances_ns: list[int],
    max_candidates: int,
    max_trial_slots: int,
) -> dict[str, Any]:
    """Freeze a bounded grid, coalesce identical schedules, and rank actual changes."""
    if not isinstance(plans, list) or not 4 <= len(plans) <= 32 or len(plans) % 4:
        raise ValueError("search requires four to thirty-two blocks in groups of four")
    _comparison_options(comparison_options)
    _integer(max_candidates, 1, 128, "max_candidates")
    _integer(max_trial_slots, 1, 16_384, "max_trial_slots")
    # Check cheap bounds before generating any potentially large source trace.
    counts: list[int] = []
    durations: list[int] = []
    for plan in plans:
        if not isinstance(plan, dict):
            raise ValueError("search source plan must be an object")
        counts.append(_integer(plan.get("offered_count"), 1, 20_000, "offered_count"))
        durations.append(
            _integer(plan.get("duration_ns"), 3, MAX_EXACT_INTEGER, "duration_ns")
        )
    groups = _axis(group_sizes, 1, 1024, "group_sizes")
    spacings = _axis(retained_spacing_bps, 0, 10_000, "retained_spacing_bps")
    advances = _axis(max_advances_ns, 0, min(durations), "max_advances_ns")
    recipe_count = len(groups) * len(spacings) * len(advances)
    if recipe_count > MAX_GRID_RECIPES:
        raise ValueError("search grid exceeds its recipe budget")
    if recipe_count * sum(counts) > MAX_GRID_OFFER_WORK:
        raise ValueError("search grid exceeds its planned-offer work budget")
    _source_plans(plans)
    excluded: list[dict[str, Any]] = []
    by_schedule: dict[str, dict[str, Any]] = {}
    for group_size, spacing, advance in itertools.product(groups, spacings, advances):
        parameters = {
            "group_size": group_size,
            "retained_spacing_bps": spacing,
            "max_advance_ns": advance,
        }
        timings = [make_timing(plan, **parameters) for plan in plans]
        if any(timing["moved_count"] == 0 for timing in timings):
            excluded.append(
                {
                    "parameters": parameters,
                    "reason": "NO_MOVEMENT_IN_ONE_OR_MORE_BLOCKS",
                }
            )
            continue
        schedule_sha256 = _digest(
            [
                {
                    "base_plan_sha256": plan["plan_sha256"],
                    "transformed_trace_sha256": timing["transformed_trace_sha256"],
                }
                for plan, timing in zip(plans, timings, strict=True)
            ]
        )
        if schedule_sha256 in by_schedule:
            # Sorted Cartesian iteration makes the first recipe the lexical
            # representative. Descriptor hashes do not identify actual schedules.
            by_schedule[schedule_sha256]["aliases"].append(parameters)
            continue
        total_advance = sum(
            row["original_scheduled_ns"] - row["scheduled_ns"]
            for timing in timings
            for row in timing["arrivals"]
        )
        protocol = make_protocol(
            plans, candidate_parameters=parameters, **comparison_options
        )
        by_schedule[schedule_sha256] = {
            "parameters": parameters,
            "aliases": [],
            "schedule_sha256": schedule_sha256,
            "timing_sha256s": [timing["timing_sha256"] for timing in timings],
            "protocol_sha256": protocol["protocol_sha256"],
            "distance": {
                "max_advance_ns": max(
                    timing["max_applied_advance_ns"] for timing in timings
                ),
                "total_advance_ns": str(total_advance),
                "moved_offers": sum(timing["moved_count"] for timing in timings),
            },
        }
    if not by_schedule:
        raise ValueError("search grid has no eligible nonidentity candidate")
    ordered = sorted(
        by_schedule.values(),
        key=lambda candidate: (
            candidate["distance"]["max_advance_ns"],
            int(candidate["distance"]["total_advance_ns"]),
            candidate["distance"]["moved_offers"],
            candidate["schedule_sha256"],
        ),
    )
    candidates = [
        {"candidate_id": f"c{rank:03d}", "rank": rank, **candidate}
        for rank, candidate in enumerate(ordered, start=1)
    ]
    slots = 4 * len(plans)
    search_plan: dict[str, Any] = {
        "schema": SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "evidence_class": "SYNTHETIC_ONLY"
        if plans[0]["phase"] == "fixture"
        else "LOCAL_MEASUREMENT_ONLY",
        "comparison_options": dict(comparison_options),
        "axes": {
            "group_sizes": groups,
            "retained_spacing_bps": spacings,
            "max_advances_ns": advances,
        },
        "max_candidates": max_candidates,
        "max_trial_slots": max_trial_slots,
        "base_plan_sha256s": [plan["plan_sha256"] for plan in plans],
        "candidates": candidates,
        "excluded_recipes": excluded,
        "grid_recipe_count": recipe_count,
        "unique_candidate_count": len(candidates),
        "trial_slots_per_candidate": slots,
        "scheduled_candidate_count": min(
            len(candidates), max_candidates, max_trial_slots // slots
        ),
        "order_method": ORDER_METHOD,
        "stop_rule": STOP_RULE,
    }
    search_plan["search_plan_sha256"] = _digest(search_plan)
    return search_plan


def validate_search_plan(
    search_plan: dict[str, Any], plans: list[dict[str, Any]]
) -> None:
    """Regenerate every schedule, alias, protocol, distance, and budget field."""
    try:
        if not isinstance(search_plan, dict) or not isinstance(
            search_plan.get("axes"), dict
        ):
            raise ValueError("search plan or axes are invalid")
        _artifact_json_types(search_plan)
        axes = search_plan["axes"]
        if set(axes) != {"group_sizes", "retained_spacing_bps", "max_advances_ns"}:
            raise ValueError("search axes contain unsupported fields")
        expected = make_search_plan(
            plans,
            comparison_options=search_plan["comparison_options"],
            group_sizes=axes["group_sizes"],
            retained_spacing_bps=axes["retained_spacing_bps"],
            max_advances_ns=axes["max_advances_ns"],
            max_candidates=search_plan["max_candidates"],
            max_trial_slots=search_plan["max_trial_slots"],
        )
        if canonical_json_bytes(search_plan) != canonical_json_bytes(expected):
            raise ValueError("search plan differs from its deterministic recipe")
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed search plan") from error


def _candidate_protocol_unchecked(
    search_plan: dict[str, Any], plans: list[dict[str, Any]], candidate_id: str
) -> dict[str, Any]:
    """Internal helper only after full plan validation in the same operation."""
    candidate = next(
        (
            row
            for row in search_plan["candidates"]
            if row["candidate_id"] == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError("candidate ID is absent from the search plan")
    protocol = make_protocol(
        plans,
        candidate_parameters=candidate["parameters"],
        **search_plan["comparison_options"],
    )
    if protocol["protocol_sha256"] != candidate["protocol_sha256"]:
        raise ValueError("candidate protocol does not match the search plan")
    return protocol


def candidate_protocol(
    search_plan: dict[str, Any], plans: list[dict[str, Any]], candidate_id: str
) -> dict[str, Any]:
    """Materialize one declared candidate after validating the entire frozen grid."""
    validate_search_plan(search_plan, plans)
    if not isinstance(candidate_id, str):
        raise ValueError("candidate ID must be a string")
    candidate = next(
        (
            row
            for row in search_plan["candidates"]
            if row["candidate_id"] == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError("candidate ID is absent from the search plan")
    if candidate["rank"] > search_plan["scheduled_candidate_count"]:
        raise ValueError("candidate is outside the declared search budget")
    return _candidate_protocol_unchecked(search_plan, plans, candidate_id)
