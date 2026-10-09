"""Restore selected original timing groups without removing or regrouping offers."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    TIMING_SCHEMA,
    _artifact_json_types,
    _population_rows,
    _source_offers,
    make_timing,
    validate_timing,
)
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_router_study import Offer

SCHEMA = "inferdrome.vllm-router-reduced-timing.v1"
METHOD = "original-group-retention.v1"
VERIFICATION_SCHEMA = "inferdrome.vllm-router-reduced-timing-check.v1"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _source_groups(
    plan: dict[str, Any], parameters: dict[str, Any]
) -> tuple[dict[str, Any], tuple[Offer, ...], list[dict[str, Any]]]:
    if not isinstance(parameters, dict) or set(parameters) != {
        "group_size",
        "retained_spacing_bps",
        "max_advance_ns",
    }:
        raise ValueError("source timing parameters are invalid")
    source = make_timing(plan, **parameters)
    offers = _source_offers(plan)
    groups: list[dict[str, Any]] = []
    previous_epoch = -1
    ordinal = 0
    position = 0
    for offer, arrival in zip(offers, source["arrivals"], strict=True):
        if offer.epoch != previous_epoch:
            ordinal = 0
            position = 0
        elif position == parameters["group_size"]:
            ordinal += 1
            position = 0
        if position == 0:
            groups.append(
                {
                    "group_id": f"e{offer.epoch}-g{ordinal:04d}",
                    "epoch": offer.epoch,
                    "ordinal": ordinal,
                    "indices": [],
                    "moved_count": 0,
                }
            )
        groups[-1]["indices"].append(offer.index)
        groups[-1]["moved_count"] += int(
            arrival["scheduled_ns"] != arrival["original_scheduled_ns"]
        )
        previous_epoch = offer.epoch
        position += 1
    return source, offers, groups


def original_groups(
    plan: dict[str, Any], parameters: dict[str, Any]
) -> list[dict[str, Any]]:
    """List every source group, including groups unchanged by the transform."""
    return _source_groups(plan, parameters)[2]


def make_reduced_timing(
    plan: dict[str, Any],
    *,
    source_parameters: dict[str, Any],
    retained_groups: list[str],
) -> dict[str, Any]:
    """Keep compressed times in named original groups; restore the rest."""
    source, offers, groups = _source_groups(plan, source_parameters)
    active = {group["group_id"]: group for group in groups if group["moved_count"] > 0}
    if (
        not isinstance(retained_groups, list)
        or len(retained_groups) > len(active)
        or any(not isinstance(group_id, str) for group_id in retained_groups)
    ):
        raise ValueError("retained groups must be a list of local active group IDs")
    if len(set(retained_groups)) != len(retained_groups):
        raise ValueError("retained group IDs must be unique")
    if any(group_id not in active for group_id in retained_groups):
        raise ValueError("retained group is unknown or has no moved offers")
    retained = sorted(retained_groups)
    selected_indices = {
        index for group_id in retained for index in active[group_id]["indices"]
    }
    arrivals = [
        {
            **arrival,
            "scheduled_ns": arrival["scheduled_ns"]
            if arrival["index"] in selected_indices
            else arrival["original_scheduled_ns"],
        }
        for arrival in source["arrivals"]
    ]
    population = _population_rows(plan, offers)
    trace = [
        {**row, "scheduled_ns": arrival["scheduled_ns"]}
        for row, arrival in zip(population, arrivals, strict=True)
    ]
    advances = [row["original_scheduled_ns"] - row["scheduled_ns"] for row in arrivals]
    timing: dict[str, Any] = {
        "schema": SCHEMA,
        "method": METHOD,
        "base_plan_schema": source["base_plan_schema"],
        "base_plan_sha256": source["base_plan_sha256"],
        "base_trace_sha256": source["base_trace_sha256"],
        "duration_ns": source["duration_ns"],
        "offered_count": source["offered_count"],
        "source_parameters": dict(source_parameters),
        "source_timing_sha256": source["timing_sha256"],
        "retained_groups": retained,
        "population_sha256": source["population_sha256"],
        "transformed_trace_sha256": _digest(trace),
        "max_applied_advance_ns": max(advances),
        "moved_count": sum(advance > 0 for advance in advances),
        "arrivals": arrivals,
    }
    timing["timing_sha256"] = _digest(timing)
    return timing


def validate_reduced_timing(
    plan: dict[str, Any], timing: dict[str, Any]
) -> tuple[Offer, ...]:
    """Regenerate the whole artifact; a mask never changes the original grouping."""
    try:
        if not isinstance(timing, dict):
            raise ValueError("reduced timing must be an object")
        _artifact_json_types(timing)
        expected = make_reduced_timing(
            plan,
            source_parameters=timing["source_parameters"],
            retained_groups=timing["retained_groups"],
        )
        if canonical_json_bytes(timing) != canonical_json_bytes(expected):
            raise ValueError("reduced timing differs from its deterministic recipe")
        return tuple(
            replace(offer, scheduled_ns=arrival["scheduled_ns"])
            for offer, arrival in zip(
                _source_offers(plan), expected["arrivals"], strict=True
            )
        )
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed reduced timing descriptor") from error


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline original-group timing reduction"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--source-timing", type=Path, required=True)
    prepare.add_argument("--retained-groups", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--timing", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = _read_json(args.plan.read_bytes())
    if args.command == "prepare":
        source = _read_json(args.source_timing.read_bytes())
        if not isinstance(source, dict) or source.get("schema") != TIMING_SCHEMA:
            raise ValueError(
                "reduction source must be an original arrival timing artifact"
            )
        validate_timing(plan, source)
        result = make_reduced_timing(
            plan,
            source_parameters=source["parameters"],
            retained_groups=_read_json(args.retained_groups.read_bytes()),
        )
    else:
        timing = _read_json(args.timing.read_bytes())
        validate_reduced_timing(plan, timing)
        result = {
            "schema": VERIFICATION_SCHEMA,
            "scope": "PLANNED_ARRIVAL_TIMING_ONLY",
            "status": "VERIFIED",
            "timing_sha256": timing["timing_sha256"],
            "source_timing_sha256": timing["source_timing_sha256"],
            "base_plan_sha256": timing["base_plan_sha256"],
            "population_sha256": timing["population_sha256"],
            "transformed_trace_sha256": timing["transformed_trace_sha256"],
            "retained_groups": timing["retained_groups"],
            "offered_count": timing["offered_count"],
            "moved_count": timing["moved_count"],
            "max_applied_advance_ns": timing["max_applied_advance_ns"],
        }
        result["result_sha256"] = _digest(result)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
