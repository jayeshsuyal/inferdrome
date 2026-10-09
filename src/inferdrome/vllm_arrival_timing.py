"""Deterministic arrival compression over an unchanged, frozen request population.

These artifacts describe planned arrivals. They do not attest that a client or
router observed those arrival times, and they make no performance claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_router_study import (
    CAPACITY_SCHEMA,
    SCHEMA,
    Offer,
    validate_plan,
)

TIMING_SCHEMA = "inferdrome.vllm-router-arrival-timing.v1"
TIMED_RESULT_SCHEMA = "inferdrome.vllm-router-timed-result.v1"
TIMING_VERIFICATION_SCHEMA = "inferdrome.vllm-router-arrival-timing-check.v1"
METHOD = "epoch-local-compression.v1"
MAX_EXACT_INTEGER = 2**53 - 1


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _artifact_json_types(
    value: object, path: tuple[str, ...] = (), *, plan_flags: bool = False
) -> None:
    """These schemas use integer numbers; bool equality must not alias them."""
    if isinstance(value, bool):
        if not plan_flags or path not in {
            ("ignore_eos",),
            ("stream_usage_required",),
        }:
            raise ValueError("artifact boolean used outside a boolean field")
    elif isinstance(value, int):
        _integer(value, -MAX_EXACT_INTEGER, MAX_EXACT_INTEGER, "artifact number")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("artifact contains a non-string field")
            _artifact_json_types(item, (*path, key), plan_flags=plan_flags)
    elif isinstance(value, list):
        for item in value:
            _artifact_json_types(item, (*path, "[]"), plan_flags=plan_flags)
    elif value is not None and not isinstance(value, str):
        raise ValueError("artifact contains an unsupported JSON value")


def _source_offers(plan: dict[str, Any]) -> tuple[Offer, ...]:
    try:
        if not isinstance(plan, dict):
            raise ValueError("source plan must be an object")
        _artifact_json_types(plan, plan_flags=True)
        schema = plan.get("schema")
        if schema not in (SCHEMA, CAPACITY_SCHEMA):
            raise ValueError("unsupported source plan schema")
        if plan.get("plan_sha256") != _digest(
            {key: value for key, value in plan.items() if key != "plan_sha256"}
        ):
            raise ValueError("source plan digest mismatch")
        if schema == SCHEMA and (
            plan.get("ignore_eos") is not True
            or plan.get("stream_usage_required") is not True
        ):
            raise ValueError("source study boolean settings are invalid")
        duration_ns = _integer(
            plan.get("duration_ns"), 3, MAX_EXACT_INTEGER, "source duration_ns"
        )
        _integer(plan.get("seed"), 0, 2**32 - 1, "source seed")
        _integer(
            plan.get("offered_count"),
            1,
            20_000 if schema == CAPACITY_SCHEMA else 10_000,
            "source offered_count",
        )
        phases = (
            ("pilot", "evaluation", "fixture")
            if schema == CAPACITY_SCHEMA
            else ("calibration", "evaluation", "fixture")
        )
        if plan.get("phase") not in phases:
            raise ValueError("unsupported source plan phase")
        if schema == CAPACITY_SCHEMA and plan["workload"].get(
            "pattern_version"
        ) not in (None, "control.v1"):
            raise ValueError("timing requires an unmodified capacity control pattern")
        offers = validate_plan(plan)
        previous_ns = -1
        for index, offer in enumerate(offers):
            scheduled_ns = _integer(
                offer.scheduled_ns, 0, duration_ns - 1, "source scheduled_ns"
            )
            if (
                type(offer.index) is not int
                or offer.index != index
                or scheduled_ns < previous_ns
                or type(offer.epoch) is not int
                or offer.epoch != scheduled_ns * 3 // duration_ns
            ):
                raise ValueError("source offer order or epoch is invalid")
            previous_ns = scheduled_ns
        return offers
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed source study plan") from error


def _population_rows(
    plan: dict[str, Any], offers: tuple[Offer, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "index": offer.index,
            "epoch": offer.epoch,
            "traffic_class": offer.traffic_class,
            "tenant": offer.tenant,
            "document_id": offer.document_id,
            "prompt_sha256": hashlib.sha256(offer.prompt.encode("utf-8")).hexdigest(),
            "expected_prompt_tokens": (
                plan["prompt_tokens_by_index"][offer.index]
                if plan["schema"] == CAPACITY_SCHEMA
                else plan["expected_prompt_tokens"]
            ),
            "max_tokens": plan["max_tokens"],
            "context_length": plan["context_length"],
        }
        for offer in offers
    ]


def make_timing(
    plan: dict[str, Any],
    *,
    group_size: int,
    retained_spacing_bps: int,
    max_advance_ns: int,
) -> dict[str, Any]:
    """Compress epoch-local groups; only each offer's scheduled time can change."""
    offers = _source_offers(plan)
    _integer(group_size, 1, 1024, "group_size")
    _integer(retained_spacing_bps, 0, 10_000, "retained_spacing_bps")
    _integer(max_advance_ns, 0, plan["duration_ns"], "max_advance_ns")
    arrivals: list[dict[str, int]] = []
    anchor_ns = 0
    group_position = 0
    previous_epoch = -1
    for offer in offers:
        if offer.epoch != previous_epoch or group_position == group_size:
            anchor_ns = offer.scheduled_ns
            group_position = 0
        scheduled_ns = max(
            offer.scheduled_ns - max_advance_ns,
            anchor_ns
            + (offer.scheduled_ns - anchor_ns) * retained_spacing_bps // 10_000,
        )
        arrivals.append(
            {
                "index": offer.index,
                "original_scheduled_ns": offer.scheduled_ns,
                "scheduled_ns": scheduled_ns,
            }
        )
        previous_epoch = offer.epoch
        group_position += 1
    population = _population_rows(plan, offers)
    transformed_trace = [
        {**row, "scheduled_ns": arrival["scheduled_ns"]}
        for row, arrival in zip(population, arrivals, strict=True)
    ]
    advances = [row["original_scheduled_ns"] - row["scheduled_ns"] for row in arrivals]
    timing: dict[str, Any] = {
        "schema": TIMING_SCHEMA,
        "method": METHOD,
        "base_plan_schema": plan["schema"],
        "base_plan_sha256": plan["plan_sha256"],
        "base_trace_sha256": plan["trace_sha256"],
        "duration_ns": plan["duration_ns"],
        "offered_count": len(offers),
        "parameters": {
            "group_size": group_size,
            "retained_spacing_bps": retained_spacing_bps,
            "max_advance_ns": max_advance_ns,
        },
        "population_sha256": _digest(population),
        "transformed_trace_sha256": _digest(transformed_trace),
        "max_applied_advance_ns": max(advances),
        "moved_count": sum(advance > 0 for advance in advances),
        "arrivals": arrivals,
    }
    timing["timing_sha256"] = _digest(timing)
    return timing


def validate_timing(plan: dict[str, Any], timing: dict[str, Any]) -> tuple[Offer, ...]:
    """Regenerate the full descriptor; arbitrary supplied arrival times fail closed."""
    try:
        if not isinstance(timing, dict) or not isinstance(
            timing.get("parameters"), dict
        ):
            raise ValueError("timing descriptor or parameters are invalid")
        _artifact_json_types(timing)
        parameters = timing["parameters"]
        if set(parameters) != {
            "group_size",
            "retained_spacing_bps",
            "max_advance_ns",
        }:
            raise ValueError("timing parameters contain unsupported fields")
        expected = make_timing(plan, **parameters)
        if canonical_json_bytes(timing) != canonical_json_bytes(expected):
            raise ValueError("timing descriptor differs from its deterministic recipe")
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
        raise ValueError("malformed arrival timing descriptor") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline planned-arrival timing")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--group-size", type=int, required=True)
    prepare.add_argument("--retained-spacing-bps", type=int, required=True)
    prepare.add_argument("--max-advance-ns", type=int, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--timing", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = _read_json(args.plan.read_bytes())
    if args.command == "prepare":
        result = make_timing(
            plan,
            group_size=args.group_size,
            retained_spacing_bps=args.retained_spacing_bps,
            max_advance_ns=args.max_advance_ns,
        )
    else:
        timing = _read_json(args.timing.read_bytes())
        validate_timing(plan, timing)
        result = {
            "schema": TIMING_VERIFICATION_SCHEMA,
            "scope": "PLANNED_ARRIVAL_TIMING_ONLY",
            "status": "VERIFIED",
            "timing_sha256": timing["timing_sha256"],
            "base_plan_sha256": timing["base_plan_sha256"],
            "population_sha256": timing["population_sha256"],
            "transformed_trace_sha256": timing["transformed_trace_sha256"],
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
