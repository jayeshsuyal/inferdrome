"""Finite timing grids have exact distances, deduplicated schedules, and budgets."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from inferdrome import vllm_router_study as study
from inferdrome import vllm_search_plan as search
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing
from inferdrome.vllm_paired_protocol import make_protocol


def _plans(count: int = 4) -> list[dict[str, Any]]:
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


def _options() -> dict[str, Any]:
    return {
        "policy_a": "cache_only",
        "policy_b": "least_busy",
        "order_seed": 41,
        "minimum_effect_microrps": 100_000,
        "max_scheduling_lag_p95_ns": 1_000_000,
        "max_client_queue_p95_ns": 1_000_000,
        "model": "synthetic-search-fixture",
        "source_revision": "a" * 40,
        "environment_sha256": "sha256:" + "b" * 64,
        "reset_procedure_sha256": "sha256:" + "c" * 64,
    }


def _kwargs(**changes: Any) -> dict[str, Any]:
    return {
        "comparison_options": _options(),
        "group_sizes": [2, 4],
        "retained_spacing_bps": [0, 5000],
        "max_advances_ns": [1_000_000, 1_000_000_000],
        "max_candidates": 128,
        "max_trial_slots": 16384,
        **changes,
    }


def _rehash(value: dict[str, Any]) -> None:
    value["search_plan_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {
                    key: item
                    for key, item in value.items()
                    if key != "search_plan_sha256"
                }
            )
        ).hexdigest()
    )


def test_grid_order_is_canonical_and_distances_describe_actual_changes() -> None:
    plans = _plans()
    frozen = canonical_json_bytes(plans)
    result = search.make_search_plan(plans, **_kwargs())
    shuffled = search.make_search_plan(
        plans,
        **_kwargs(
            group_sizes=[4, 2],
            retained_spacing_bps=[5000, 0],
            max_advances_ns=[1_000_000_000, 1_000_000],
        ),
    )
    assert result == shuffled
    assert canonical_json_bytes(plans) == frozen
    assert result["schema"] == search.SCHEMA
    candidates = result["candidates"]
    assert candidates
    assert [candidate["rank"] for candidate in candidates] == list(
        range(1, len(candidates) + 1)
    )
    assert [candidate["candidate_id"] for candidate in candidates] == [
        f"c{index:03d}" for index in range(1, len(candidates) + 1)
    ]
    keys = []
    for candidate in candidates:
        timings = [make_timing(plan, **candidate["parameters"]) for plan in plans]
        advances = [
            row["original_scheduled_ns"] - row["scheduled_ns"]
            for timing in timings
            for row in timing["arrivals"]
        ]
        assert candidate["distance"] == {
            "max_advance_ns": max(advances),
            "total_advance_ns": str(sum(advances)),
            "moved_offers": sum(value > 0 for value in advances),
        }
        assert candidate["timing_sha256s"] == [
            timing["timing_sha256"] for timing in timings
        ]
        protocol = search.candidate_protocol(result, plans, candidate["candidate_id"])
        assert protocol == make_protocol(
            plans, **_options(), candidate_parameters=candidate["parameters"]
        )
        assert candidate["protocol_sha256"] == protocol["protocol_sha256"]
        keys.append(
            (
                max(advances),
                sum(advances),
                sum(value > 0 for value in advances),
                candidate["schedule_sha256"],
            )
        )
    assert keys == sorted(keys)
    search.validate_search_plan(result, plans)


def test_different_recipes_with_identical_schedules_share_one_candidate() -> None:
    result = search.make_search_plan(
        _plans(),
        **_kwargs(
            group_sizes=[2],
            retained_spacing_bps=[0, 5000],
            max_advances_ns=[1],
        ),
    )
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["parameters"] == {
        "group_size": 2,
        "retained_spacing_bps": 0,
        "max_advance_ns": 1,
    }
    assert candidate["aliases"] == [
        {"group_size": 2, "retained_spacing_bps": 5000, "max_advance_ns": 1}
    ]
    assert candidate["distance"]["max_advance_ns"] == 1


def test_identity_recipes_are_inspectable_exclusions_not_budget_consumers() -> None:
    result = search.make_search_plan(
        _plans(),
        **_kwargs(
            group_sizes=[1, 2],
            retained_spacing_bps=[0, 10000],
            max_advances_ns=[0, 1],
        ),
    )
    assert len(result["candidates"]) == 1
    assert len(result["excluded_recipes"]) == 7
    assert {item["reason"] for item in result["excluded_recipes"]} == {
        "NO_MOVEMENT_IN_ONE_OR_MORE_BLOCKS"
    }
    assert result["scheduled_candidate_count"] == 1


@pytest.mark.parametrize(
    ("candidates", "slots", "expected"),
    [(1, 16384, 1), (128, 31, 1), (128, 15, 0), (2, 32, 2)],
)
def test_budgets_reserve_whole_four_trial_blocks(
    candidates: int, slots: int, expected: int
) -> None:
    result = search.make_search_plan(
        _plans(), **_kwargs(max_candidates=candidates, max_trial_slots=slots)
    )
    assert len(result["candidates"]) > 2
    assert result["scheduled_candidate_count"] == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("group_sizes", []),
        ("group_sizes", [2, 2]),
        ("group_sizes", [True]),
        ("group_sizes", [2.0]),
        ("group_sizes", [0]),
        ("group_sizes", [1025]),
        ("group_sizes", list(range(1, 18))),
        ("retained_spacing_bps", [10001]),
        ("retained_spacing_bps", [-1]),
        ("retained_spacing_bps", [False]),
        ("max_advances_ns", [-1]),
        ("max_advances_ns", [3_000_000_001]),
        ("max_advances_ns", [1.0]),
        ("max_candidates", 0),
        ("max_candidates", 129),
        ("max_candidates", True),
        ("max_trial_slots", 0),
        ("max_trial_slots", 16385),
        ("max_trial_slots", 1.0),
    ],
)
def test_grid_and_budget_inputs_require_bounded_exact_integers(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        search.make_search_plan(_plans(), **_kwargs(**{field: value}))


def test_cartesian_grid_limit_is_checked_before_generating_candidates() -> None:
    with pytest.raises(ValueError):
        search.make_search_plan(
            _plans(),
            **_kwargs(
                group_sizes=list(range(2, 11)),
                retained_spacing_bps=list(range(16)),
            ),
        )


def test_offer_work_limit_is_checked_before_source_trace_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = [{"offered_count": 20_000, "duration_ns": 3_000_000_000} for _ in range(4)]

    def unexpected_timing(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the aggregate work bound must be checked before generating traces")

    monkeypatch.setattr(search, "make_timing", unexpected_timing)
    with pytest.raises(ValueError):
        search.make_search_plan(
            plans,
            **_kwargs(
                group_sizes=list(range(2, 10)),
                retained_spacing_bps=[0, 2500, 5000, 7500],
            ),
        )


def test_entirely_identity_grid_is_rejected() -> None:
    with pytest.raises(ValueError):
        search.make_search_plan(_plans(), **_kwargs(group_sizes=[1]))


def test_candidate_protocol_cannot_materialize_outside_the_frozen_budget() -> None:
    plans = _plans()
    result = search.make_search_plan(plans, **_kwargs(max_candidates=1))
    assert len(result["candidates"]) > 1
    with pytest.raises(ValueError, match="budget"):
        search.candidate_protocol(result, plans, "c002")


@pytest.mark.parametrize(
    "malformed", ["policy", "unknown_option", "duplicate_seed", "calibration"]
)
def test_noop_grid_still_validates_the_comparison_contract(malformed: str) -> None:
    plans = _plans()
    options = _options()
    if malformed == "policy":
        options["policy_a"] = "cache_saturation"
    elif malformed == "unknown_option":
        options["selected_winner"] = "cache_only"
    elif malformed == "duplicate_seed":
        plans[1] = copy.deepcopy(plans[0])
    else:
        plans = [
            study.make_plan(
                phase="calibration",
                seed=100 + index,
                count=12,
                expected_prompt_tokens=200,
            )
            for index in range(4)
        ]
    with pytest.raises(ValueError):
        search.make_search_plan(
            plans, **_kwargs(comparison_options=options, group_sizes=[1])
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "order",
        "distance",
        "float_distance",
        "rank_bool",
        "alias",
        "schedule",
        "reservation",
    ],
)
def test_rehashing_cannot_change_the_grid_or_ranked_budget_prefix(
    mutation: str,
) -> None:
    plans = _plans()
    result = search.make_search_plan(plans, **_kwargs())
    candidate = result["candidates"][0]
    if mutation == "unknown":
        result["globally_minimal"] = True
    elif mutation == "order":
        result["candidates"].reverse()
    elif mutation == "distance":
        candidate["distance"]["total_advance_ns"] = "0"
    elif mutation == "float_distance":
        candidate["distance"]["max_advance_ns"] = float(
            candidate["distance"]["max_advance_ns"]
        )
    elif mutation == "rank_bool":
        candidate["rank"] = True
    elif mutation == "alias":
        candidate["aliases"] = []
    elif mutation == "schedule":
        candidate["schedule_sha256"] = "sha256:" + "0" * 64
    elif mutation == "reservation":
        result["scheduled_candidate_count"] = 0
    _rehash(result)
    with pytest.raises(ValueError):
        search.validate_search_plan(result, plans)


def test_unknown_candidate_and_changed_source_plan_are_rejected() -> None:
    plans = _plans()
    result = search.make_search_plan(plans, **_kwargs())
    with pytest.raises(ValueError):
        search.candidate_protocol(result, plans, "c999")
    plans[0] = study.make_plan(
        phase="fixture",
        seed=999,
        count=12,
        expected_prompt_tokens=200,
        duration_ns=3_000_000_000,
        max_tokens=4,
    )
    with pytest.raises(ValueError):
        search.validate_search_plan(result, plans)
