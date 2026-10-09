"""Frozen exploratory paired comparisons and exact directional diagnostics.

The protocol binds declarations and artifact identities. Its statistics assume
independent blocks; neither hashes nor a directional diagnostic establish that
independence, execution provenance, or a confirmed routing counterexample.
"""

from __future__ import annotations

import hashlib
import math
import re
from fractions import Fraction
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    MAX_EXACT_INTEGER,
    _artifact_json_types,
    make_timing,
)
from inferdrome.vllm_router_study import CAPACITY_SCHEMA, POLICIES

SCHEMA = "inferdrome.vllm-router-paired-protocol.v1"
ORDER_METHOD = "SHA256_SORTED_WILLIAMS_FOUR_CELL_V1"
MAX_MINIMUM_EFFECT_MICRORPS = 10**12
_WILLIAMS_ROWS = (
    (0, 1, 3, 2),
    (1, 2, 0, 3),
    (2, 3, 1, 0),
    (3, 0, 2, 1),
)
_PARAMETER_FIELDS = {
    "policy_a",
    "policy_b",
    "candidate_parameters",
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


def _integer(value: object, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _block_count(value: int) -> None:
    _integer(value, 4, 32, "block count")
    if value % 4:
        raise ValueError("block count must be a multiple of four")


def _common_plan(plan: dict[str, Any]) -> bytes:
    excluded = {"seed", "trace_sha256", "plan_sha256"}
    if plan["schema"] == CAPACITY_SCHEMA:
        excluded.add("prompt_tokens_by_index")
    return canonical_json_bytes(
        {key: value for key, value in plan.items() if key not in excluded}
    )


def make_protocol(
    plans: list[dict[str, Any]],
    *,
    policy_a: str,
    policy_b: str,
    candidate_parameters: dict[str, Any],
    order_seed: int,
    minimum_effect_microrps: int,
    max_scheduling_lag_p95_ns: int,
    max_client_queue_p95_ns: int,
    model: str,
    source_revision: str,
    environment_sha256: str,
    reset_procedure_sha256: str,
) -> dict[str, Any]:
    """Declare all four cells before selecting outcomes; no trials execute here."""
    if not isinstance(plans, list):
        raise ValueError("paired plans must be an ordered list")
    _block_count(len(plans))
    for policy in (policy_a, policy_b):
        if not isinstance(policy, str) or policy not in POLICIES:
            raise ValueError("paired policy is unsupported")
    if policy_a == policy_b:
        raise ValueError("paired policies must differ")
    if not isinstance(candidate_parameters, dict) or set(candidate_parameters) != {
        "group_size",
        "retained_spacing_bps",
        "max_advance_ns",
    }:
        raise ValueError("candidate timing parameters are invalid")
    _integer(order_seed, 0, 2**32 - 1, "order_seed")
    _integer(
        minimum_effect_microrps,
        0,
        MAX_MINIMUM_EFFECT_MICRORPS,
        "minimum_effect_microrps",
    )
    _integer(
        max_scheduling_lag_p95_ns,
        0,
        MAX_EXACT_INTEGER,
        "max_scheduling_lag_p95_ns",
    )
    _integer(
        max_client_queue_p95_ns,
        0,
        MAX_EXACT_INTEGER,
        "max_client_queue_p95_ns",
    )
    if (
        not isinstance(model, str)
        or not 1 <= len(model) <= 200
        or model.strip() != model
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise ValueError("model declaration is invalid")
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    ):
        raise ValueError("source revision declaration must be a lowercase Git SHA")
    for name, value in (
        ("environment_sha256", environment_sha256),
        ("reset_procedure_sha256", reset_procedure_sha256),
    ):
        if (
            not isinstance(value, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        ):
            raise ValueError(f"{name} declaration is invalid")

    common: bytes | None = None
    seeds: set[int] = set()
    timing_hashes: list[tuple[str, str]] = []
    for plan in plans:
        # The timing reader validates exact scalar types, the source digest, and
        # the frozen source recipe before any field is used as an identity.
        baseline = make_timing(
            plan, group_size=1, retained_spacing_bps=10_000, max_advance_ns=0
        )
        if plan["phase"] not in ("fixture", "evaluation"):
            raise ValueError("paired comparison excludes calibration and pilot plans")
        source_contract = _common_plan(plan)
        if common is None:
            common = source_contract
        elif source_contract != common:
            raise ValueError(
                "paired source plans differ beyond the allowed block fields"
            )
        seed = plan["seed"]
        if seed in seeds:
            raise ValueError("paired blocks require distinct workload seeds")
        seeds.add(seed)
        candidate = make_timing(plan, **candidate_parameters)
        if candidate["moved_count"] == 0:
            raise ValueError("candidate timing must move offers in every block")
        timing_hashes.append((baseline["timing_sha256"], candidate["timing_sha256"]))

    cells = (
        ("baseline", policy_a),
        ("baseline", policy_b),
        ("candidate", policy_a),
        ("candidate", policy_b),
    )
    orders: dict[int, list[int]] = {}
    trials: list[dict[str, Any]] = []
    for index, plan in enumerate(plans):
        group = index // 4
        if group not in orders:
            orders[group] = sorted(
                range(4),
                key=lambda row: hashlib.sha256(
                    canonical_json_bytes(
                        {"order_seed": order_seed, "group": group, "row": row}
                    )
                ).digest(),
            )
        row = _WILLIAMS_ROWS[orders[group][index % 4]]
        block = index + 1
        for position, cell in enumerate(row, start=1):
            condition, policy = cells[cell]
            trials.append(
                {
                    "trial_id": f"b{block:02d}-{condition}-{policy}",
                    "block": block,
                    "position": position,
                    "sequence": len(trials) + 1,
                    "seed": plan["seed"],
                    "condition": condition,
                    "policy": policy,
                    "base_plan_sha256": plan["plan_sha256"],
                    "timing_sha256": timing_hashes[index][
                        0 if condition == "baseline" else 1
                    ],
                }
            )
    protocol: dict[str, Any] = {
        "schema": SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "evidence_class": "SYNTHETIC_ONLY"
        if plans[0]["phase"] == "fixture"
        else "LOCAL_MEASUREMENT_ONLY",
        "order_method": ORDER_METHOD,
        "execution_identity_scope": "OPERATOR_DECLARATIONS_NOT_ATTESTATION",
        "policy_a": policy_a,
        "policy_b": policy_b,
        "candidate_parameters": dict(candidate_parameters),
        "order_seed": order_seed,
        "minimum_effect_microrps": minimum_effect_microrps,
        "max_scheduling_lag_p95_ns": max_scheduling_lag_p95_ns,
        "max_client_queue_p95_ns": max_client_queue_p95_ns,
        "model": model,
        "source_revision": source_revision,
        "environment_sha256": environment_sha256,
        "reset_procedure_sha256": reset_procedure_sha256,
        "base_plan_sha256s": [plan["plan_sha256"] for plan in plans],
        "trials": trials,
    }
    protocol["protocol_sha256"] = _digest(protocol)
    return protocol


def validate_protocol(protocol: dict[str, Any], plans: list[dict[str, Any]]) -> None:
    """Rebuild the full declared design; a rehashed edited schedule is invalid."""
    try:
        if not isinstance(protocol, dict):
            raise ValueError("paired protocol must be an object")
        _artifact_json_types(protocol)
        expected = make_protocol(
            plans, **{key: protocol[key] for key in _PARAMETER_FIELDS}
        )
        if canonical_json_bytes(protocol) != canonical_json_bytes(expected):
            raise ValueError("paired protocol differs from its deterministic recipe")
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed paired protocol") from error


def _fraction(value: Fraction) -> dict[str, str]:
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _directional_summary(
    values: list[Fraction], *, direction: int, margin: Fraction
) -> dict[str, Any]:
    count = len(values)
    ordered = sorted(values)
    mean = sum(values, Fraction(0)) / count
    middle = count // 2
    median = (ordered[middle - 1] + ordered[middle]) / 2
    exact = {"mean": mean, "median": median, "min": ordered[0], "max": ordered[-1]}
    oriented = [direction * value for value in values]
    exceedances = sum(value > margin for value in oriented)
    at_margin = sum(value == margin for value in oriented)
    pvalue = Fraction(
        sum(math.comb(count, successes) for successes in range(exceedances, count + 1)),
        2**count,
    )
    alpha = Fraction(1, 40)
    try:
        approximations = {name: float(value) for name, value in exact.items()}
    except OverflowError as error:
        raise ValueError(
            "paired rate lies outside the finite reporting range"
        ) from error
    if any(not math.isfinite(value) for value in approximations.values()):
        raise ValueError("paired rate lies outside the finite reporting range")
    return {
        "block_count": count,
        "direction": "POLICY_A_ADVANTAGE" if direction == 1 else "POLICY_B_ADVANTAGE",
        **{f"{name}_rps": value for name, value in approximations.items()},
        "exact_rps": {name: _fraction(value) for name, value in exact.items()},
        "exceedances": exceedances,
        "at_margin": at_margin,
        "opposite_or_below": count - exceedances - at_margin,
        "pvalue": _fraction(pvalue),
        "alpha": _fraction(alpha),
        "direction_supported": pvalue <= alpha and direction * mean > margin,
    }


def paired_statistics(
    baseline_deltas: list[Fraction],
    candidate_deltas: list[Fraction],
    minimum_effect_microrps: int,
) -> dict[str, Any]:
    """Count directional margin exceedances; this is not inference on a mean."""
    if not isinstance(baseline_deltas, list) or not isinstance(candidate_deltas, list):
        raise ValueError("paired differences must be ordered lists")
    _block_count(len(baseline_deltas))
    if len(candidate_deltas) != len(baseline_deltas):
        raise ValueError("paired difference populations must have equal lengths")
    if any(
        type(value) is not Fraction for value in [*baseline_deltas, *candidate_deltas]
    ):
        raise ValueError("paired differences must be exact Fraction values")
    _integer(
        minimum_effect_microrps,
        0,
        MAX_MINIMUM_EFFECT_MICRORPS,
        "minimum_effect_microrps",
    )
    margin = Fraction(minimum_effect_microrps, 1_000_000)
    baseline = _directional_summary(baseline_deltas, direction=1, margin=margin)
    candidate = _directional_summary(candidate_deltas, direction=-1, margin=margin)
    return {
        "replication_unit": "MATCHED_FOUR_TRIAL_BLOCK",
        "contrast": "POLICY_A_MINUS_POLICY_B_SLO_GOODPUT",
        "minimum_effect_microrps": minimum_effect_microrps,
        "minimum_effect_rps": float(margin),
        "baseline": baseline,
        "candidate": candidate,
        "reversal_signal": baseline["direction_supported"]
        and candidate["direction_supported"],
        "test": "EXACT_ONE_SIDED_BINOMIAL_MARGIN_EXCEEDANCE",
        "null_exceedance_probability": _fraction(Fraction(1, 2)),
        "planned_familywise_alpha": _fraction(Fraction(1, 20)),
        "multiplicity_scope": "TWO_PREDECLARED_DIRECTIONS_FOR_ONE_CANDIDATE_ONLY",
        "ties": "AT_MARGIN_COUNTS_AS_FAILURE_WITH_ALL_PLANNED_BLOCKS_RETAINED",
        "assumptions": [
            "INDEPENDENT_BLOCKS",
            "NULL_DIRECTIONAL_MARGIN_EXCEEDANCE_PROBABILITY_AT_MOST_ONE_HALF",
            "DIRECTIONS_AND_MARGIN_PREDECLARED",
        ],
        "inference_scope": "EXCEEDANCE_PROBABILITY_NOT_POPULATION_MEAN",
        "independent_execution": "UNVERIFIED",
        "search_multiplicity": "UNCONTROLLED",
        "confidence_interval": None,
    }
