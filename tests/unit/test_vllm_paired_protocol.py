"""Frozen paired-block design and exact finite-sample directional checks."""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from fractions import Fraction
from itertools import pairwise
from typing import Any

import pytest

from inferdrome import vllm_paired_protocol as paired
from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes


def _plans(count: int = 8) -> list[dict[str, Any]]:
    return [
        study.make_plan(
            phase="fixture",
            seed=100 + index,
            count=12,
            expected_prompt_tokens=200,
            duration_ns=3_000_000_000,
            max_tokens=4,
        )
        for index in range(count)
    ]


def _kwargs(**changes: Any) -> dict[str, Any]:
    return {
        "policy_a": "cache_only",
        "policy_b": "least_busy",
        "candidate_parameters": {
            "group_size": 4,
            "retained_spacing_bps": 0,
            "max_advance_ns": 1_000_000_000,
        },
        "order_seed": 41,
        "minimum_effect_microrps": 100_000,
        "max_scheduling_lag_p95_ns": 1_000_000,
        "max_client_queue_p95_ns": 1_000_000,
        "model": "synthetic-fixture-model",
        "source_revision": "a" * 40,
        "environment_sha256": "sha256:" + "b" * 64,
        "reset_procedure_sha256": "sha256:" + "c" * 64,
        **changes,
    }


def _rehash(value: dict[str, Any]) -> None:
    value["protocol_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != "protocol_sha256"}
            )
        ).hexdigest()
    )


def test_protocol_is_deterministic_and_binds_every_plan_and_trial() -> None:
    plans = _plans()
    before = canonical_json_bytes(plans)
    protocol = paired.make_protocol(plans, **_kwargs())
    assert protocol == paired.make_protocol(plans, **_kwargs())
    assert canonical_json_bytes(plans) == before
    assert protocol["schema"] == paired.SCHEMA
    assert protocol["scope"] == "EXPLORATORY_SEARCH_ONLY"
    assert protocol["evidence_class"] == "SYNTHETIC_ONLY"
    assert protocol["base_plan_sha256s"] == [plan["plan_sha256"] for plan in plans]
    assert len(protocol["trials"]) == 4 * len(plans)
    assert len({trial["trial_id"] for trial in protocol["trials"]}) == 4 * len(plans)
    assert [trial["sequence"] for trial in protocol["trials"]] == list(range(1, 33))
    for block, plan in enumerate(plans, 1):
        trials = [trial for trial in protocol["trials"] if trial["block"] == block]
        assert {trial["position"] for trial in trials} == {1, 2, 3, 4}
        assert {(trial["condition"], trial["policy"]) for trial in trials} == {
            (condition, policy)
            for condition in ("baseline", "candidate")
            for policy in ("cache_only", "least_busy")
        }
        assert all(
            trial["base_plan_sha256"] == plan["plan_sha256"]
            and trial["seed"] == plan["seed"]
            for trial in trials
        )
        assert len({trial["timing_sha256"] for trial in trials}) == 2
    paired.validate_protocol(protocol, plans)


def test_each_four_block_group_balances_position_and_predecessors() -> None:
    protocol = paired.make_protocol(_plans(), **_kwargs())
    for first_block in (1, 5):
        position_counts: Counter = Counter()
        transitions: Counter = Counter()
        for block in range(first_block, first_block + 4):
            trials = [trial for trial in protocol["trials"] if trial["block"] == block]
            cells = [(trial["condition"], trial["policy"]) for trial in trials]
            position_counts.update(
                (position, cell) for position, cell in enumerate(cells)
            )
            transitions.update(pairwise(cells))
        assert len(position_counts) == 16 and set(position_counts.values()) == {1}
        assert len(transitions) == 12 and set(transitions.values()) == {1}
    other = paired.make_protocol(_plans(), **_kwargs(order_seed=42))
    assert protocol["protocol_sha256"] != other["protocol_sha256"]


@pytest.mark.parametrize("count", [0, 1, 3, 5, 33, 36])
def test_only_complete_bounded_four_block_groups_are_supported(count: int) -> None:
    with pytest.raises(ValueError):
        paired.make_protocol(_plans(count), **_kwargs())


@pytest.mark.parametrize(
    "change", ["duplicate_seed", "max_tokens", "prompt_tokens", "phase", "calibration"]
)
def test_blocks_cannot_change_the_base_workload_contract(change: str) -> None:
    plans = _plans()
    if change == "duplicate_seed":
        plans[1] = copy.deepcopy(plans[0])
    elif change == "calibration":
        plans = [
            study.make_plan(
                phase="calibration",
                seed=100 + index,
                count=12,
                expected_prompt_tokens=200,
            )
            for index in range(8)
        ]
    else:
        plans[1] = study.make_plan(
            phase="evaluation" if change == "phase" else "fixture",
            seed=101,
            count=12,
            expected_prompt_tokens=201 if change == "prompt_tokens" else 200,
            duration_ns=300_000_000_000 if change == "phase" else 3_000_000_000,
            max_tokens=5 if change == "max_tokens" else 4,
        )
    with pytest.raises(ValueError):
        paired.make_protocol(plans, **_kwargs())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_b", "cache_only"),
        ("policy_a", "invented"),
        ("policy_a", "cache_saturation"),
        ("order_seed", True),
        ("order_seed", -1),
        ("order_seed", 2**32),
        ("minimum_effect_microrps", -1),
        ("minimum_effect_microrps", 1.0),
        ("minimum_effect_microrps", True),
        ("minimum_effect_microrps", 10**12 + 1),
        ("max_scheduling_lag_p95_ns", -1),
        ("max_client_queue_p95_ns", False),
        ("model", ""),
        ("source_revision", "main"),
        ("source_revision", "A" * 40),
        ("environment_sha256", "b" * 64),
        ("reset_procedure_sha256", "sha256:" + "z" * 64),
    ],
)
def test_protocol_parameters_are_strict_and_bounded(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        paired.make_protocol(_plans(), **_kwargs(**{field: value}))


@pytest.mark.parametrize(
    "parameters",
    [
        {"group_size": 1, "retained_spacing_bps": 0, "max_advance_ns": 1_000_000_000},
        {
            "group_size": 4,
            "retained_spacing_bps": 10000,
            "max_advance_ns": 1_000_000_000,
        },
        {"group_size": 4, "retained_spacing_bps": 0, "max_advance_ns": 0},
    ],
)
def test_candidate_requires_an_actual_timing_change(parameters: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        paired.make_protocol(_plans(), **_kwargs(candidate_parameters=parameters))


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "reorder", "duplicate", "block_bool", "timing", "plan", "margin"],
)
def test_rehashing_does_not_authorize_a_modified_design(mutation: str) -> None:
    plans = _plans()
    protocol = paired.make_protocol(plans, **_kwargs())
    if mutation == "unknown":
        protocol["selected_winner"] = "cache_only"
    elif mutation == "reorder":
        protocol["trials"].reverse()
    elif mutation == "duplicate":
        protocol["trials"][1] = copy.deepcopy(protocol["trials"][0])
    elif mutation == "block_bool":
        protocol["trials"][0]["block"] = True
    elif mutation == "timing":
        protocol["trials"][0]["timing_sha256"] = "sha256:" + "0" * 64
    elif mutation == "plan":
        protocol["base_plan_sha256s"][0] = "sha256:" + "0" * 64
    elif mutation == "margin":
        protocol["minimum_effect_microrps"] = False
    _rehash(protocol)
    with pytest.raises(ValueError):
        paired.validate_protocol(protocol, plans)


def test_eight_consistent_block_reversals_are_a_signal_with_explicit_limits() -> None:
    stats = paired.paired_statistics(
        [Fraction(1, 3)] * 8, [Fraction(-1, 2)] * 8, 100_000
    )
    assert stats["reversal_signal"] is True
    assert stats["baseline"]["pvalue"] == {"numerator": "1", "denominator": "256"}
    assert stats["candidate"]["pvalue"] == {"numerator": "1", "denominator": "256"}
    assert stats["baseline"]["exact_rps"]["mean"] == {
        "numerator": "1",
        "denominator": "3",
    }
    assert stats["candidate"]["exact_rps"]["mean"] == {
        "numerator": "-1",
        "denominator": "2",
    }
    assert stats["baseline"]["alpha"] == {"numerator": "1", "denominator": "40"}
    assert stats["baseline"]["block_count"] == 8
    assert stats["confidence_interval"] is None
    assert stats["independent_execution"] == "UNVERIFIED"
    assert stats["search_multiplicity"] == "UNCONTROLLED"
    assert stats["inference_scope"] == "EXCEEDANCE_PROBABILITY_NOT_POPULATION_MEAN"


def test_four_blocks_have_insufficient_sign_test_resolution() -> None:
    stats = paired.paired_statistics([Fraction(100)] * 4, [Fraction(-100)] * 4, 0)
    assert stats["baseline"]["pvalue"] == {"numerator": "1", "denominator": "16"}
    assert stats["reversal_signal"] is False


@pytest.mark.parametrize("value", [Fraction(0), Fraction(1, 10), Fraction(1, 2)])
def test_ties_and_submargin_effects_do_not_count_as_exceedances(
    value: Fraction,
) -> None:
    stats = paired.paired_statistics([value] * 8, [-value] * 8, 500_000)
    assert stats["reversal_signal"] is False
    assert stats["baseline"]["exceedances"] == 0
    assert stats["baseline"]["at_margin"] == (8 if value == Fraction(1, 2) else 0)
    assert stats["baseline"]["pvalue"] == {"numerator": "1", "denominator": "1"}


def test_large_request_effect_cannot_replace_independent_block_support() -> None:
    stats = paired.paired_statistics(
        [Fraction(100)] * 7 + [Fraction(0)], [Fraction(-100)] * 8, 0
    )
    assert stats["baseline"]["pvalue"] == {"numerator": "9", "denominator": "256"}
    assert stats["baseline"]["direction_supported"] is False
    assert stats["candidate"]["direction_supported"] is True
    assert stats["reversal_signal"] is False


def test_statistical_direction_follows_the_predeclared_policy_order() -> None:
    stats = paired.paired_statistics([Fraction(-1)] * 8, [Fraction(1)] * 8, 0)
    assert stats["baseline"]["direction"] == "POLICY_A_ADVANTAGE"
    assert stats["candidate"]["direction"] == "POLICY_B_ADVANTAGE"
    assert stats["reversal_signal"] is False


def test_supported_exceedance_probability_cannot_override_the_mean_guard() -> None:
    stats = paired.paired_statistics(
        [Fraction(1)] * 15 + [Fraction(-100)], [Fraction(-1)] * 16, 0
    )
    assert stats["baseline"]["pvalue"] == {"numerator": "17", "denominator": "65536"}
    assert stats["baseline"]["mean_rps"] < 0
    assert stats["baseline"]["direction_supported"] is False
    assert stats["reversal_signal"] is False


@pytest.mark.parametrize("exceedances", [22, 23])
def test_probability_just_above_alpha_is_not_rounded_into_support(
    exceedances: int,
) -> None:
    stats = paired.paired_statistics(
        [Fraction(1)] * exceedances + [Fraction(0)] * (32 - exceedances),
        [Fraction(-1)] * 32,
        0,
    )
    probability = stats["baseline"]["pvalue"]
    exact = Fraction(int(probability["numerator"]), int(probability["denominator"]))
    assert (exact <= Fraction(1, 40)) is (exceedances == 23)
    assert stats["baseline"]["direction_supported"] is (exceedances == 23)
    assert stats["reversal_signal"] is (exceedances == 23)


@pytest.mark.parametrize(
    "case", ["mismatched", "too_few", "not_multiple", "float", "integer", "bool_margin"]
)
def test_statistical_inputs_require_complete_exact_paired_blocks(case: str) -> None:
    baseline: list[Any] = [Fraction(1)] * 8
    candidate = [Fraction(-1)] * 8
    margin: Any = 0
    if case == "mismatched":
        candidate.pop()
    elif case == "too_few":
        baseline, candidate = baseline[:3], candidate[:3]
    elif case == "not_multiple":
        baseline, candidate = baseline[:5], candidate[:5]
    elif case == "float":
        baseline[0] = 1.0
    elif case == "integer":
        baseline[0] = 1
    elif case == "bool_margin":
        margin = False
    with pytest.raises(ValueError):
        paired.paired_statistics(baseline, candidate, margin)
