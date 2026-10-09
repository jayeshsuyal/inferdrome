"""Global reduction masks retain matched blocks and the original request population."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_reduced_protocol as reduced
from inferdrome import vllm_reduced_timing as timing
from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing, validate_timing
from inferdrome.vllm_paired_protocol import make_protocol, validate_protocol

PARAMETERS = {
    "group_size": 3,
    "retained_spacing_bps": 0,
    "max_advance_ns": 1_000_000_000,
}


def _plans() -> list[dict[str, Any]]:
    return [
        study.make_plan(
            phase="fixture",
            seed=100 + index,
            count=14,
            expected_prompt_tokens=200,
            duration_ns=3_000_000_000,
            max_tokens=4,
        )
        for index in range(4)
    ]


def _options() -> dict[str, Any]:
    return {
        "policy_a": "cache_only",
        "policy_b": "least_busy",
        "order_seed": 41,
        "minimum_effect_microrps": 100_000,
        "max_scheduling_lag_p95_ns": 1_000_000,
        "max_client_queue_p95_ns": 1_000_000,
        "model": "synthetic-reduction-fixture",
        "source_revision": "a" * 40,
        "environment_sha256": "sha256:" + "b" * 64,
        "reset_procedure_sha256": "sha256:" + "c" * 64,
    }


def _protocol(
    plans: list[dict[str, Any]], mask: list[str] | None = None
) -> dict[str, Any]:
    return reduced.make_reduced_protocol(
        plans,
        source_parameters=PARAMETERS,
        retained_groups=reduced.group_universe(plans, PARAMETERS)
        if mask is None
        else mask,
        **_options(),
    )


def _rehash(value: dict[str, Any]) -> None:
    value["protocol_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != "protocol_sha256"}
            )
        ).hexdigest()
    )


def test_global_group_universe_is_the_sorted_union_of_active_original_groups() -> None:
    plans = _plans()
    local = [
        {
            group["group_id"]
            for group in timing.original_groups(plan, PARAMETERS)
            if group["moved_count"]
        }
        for plan in plans
    ]
    universe = reduced.group_universe(plans, PARAMETERS)
    assert universe == sorted(set.union(*local))
    assert len({tuple(sorted(groups)) for groups in local}) > 1


def test_full_mask_preserves_root_schedules_and_frozen_trial_order() -> None:
    plans = _plans()
    protocol = _protocol(plans)
    root = make_protocol(plans, candidate_parameters=PARAMETERS, **_options())
    assert protocol["schema"] == reduced.SCHEMA
    assert protocol["candidate_parameters"] == PARAMETERS
    assert protocol["base_plan_sha256s"] == root["base_plan_sha256s"]
    assert protocol["retained_groups"] == reduced.group_universe(plans, PARAMETERS)
    assert len(protocol["trials"]) == len(root["trials"]) == 16
    for trial, original in zip(protocol["trials"], root["trials"], strict=True):
        assert {
            key: value for key, value in trial.items() if key != "timing_sha256"
        } == {key: value for key, value in original.items() if key != "timing_sha256"}
        if trial["condition"] == "baseline":
            assert trial["timing_sha256"] == original["timing_sha256"]
        else:
            assert trial["timing_sha256"] != original["timing_sha256"]
    timings = reduced.reduced_timings(plans, protocol)
    for plan, pair in zip(plans, timings, strict=True):
        original = make_timing(plan, **PARAMETERS)
        assert (
            pair["candidate"]["transformed_trace_sha256"]
            == original["transformed_trace_sha256"]
        )
        assert validate_timing(plan, pair["baseline"]) == study.validate_plan(plan)
        assert pair["candidate"]["offered_count"] == plan["offered_count"]
    reduced.validate_reduced_protocol(protocol, plans)
    validate_protocol(protocol, plans)


def test_shared_mask_can_restore_every_other_epoch_without_removing_offers() -> None:
    plans = _plans()
    protocol = _protocol(plans, ["e1-g0000"])
    for plan, pair in zip(plans, reduced.reduced_timings(plans, protocol), strict=True):
        original = study.validate_plan(plan)
        transformed = validate_timing(plan, pair["candidate"])
        assert len(transformed) == len(original)
        assert pair["candidate"]["retained_groups"] == ["e1-g0000"]
        assert pair["candidate"]["moved_count"] > 0
        assert all(
            new.scheduled_ns == old.scheduled_ns
            for old, new in zip(original, transformed, strict=True)
            if old.epoch != 1
        )


@pytest.mark.parametrize(
    "mask",
    [
        [],
        ["e9-g9999"],
        ["e1-g0000", "e0-g0000"],
        ["e0-g0000", "e0-g0000"],
        [True],
        "e0-g0000",
    ],
)
def test_global_masks_require_nonempty_sorted_unique_active_groups(mask: Any) -> None:
    with pytest.raises(ValueError):
        _protocol(_plans(), mask)


def test_a_mask_that_moves_no_offers_in_one_block_is_not_a_comparison() -> None:
    plans = _plans()
    local = [
        {
            group["group_id"]
            for group in timing.original_groups(plan, PARAMETERS)
            if group["moved_count"]
        }
        for plan in plans
    ]
    not_universal = sorted(set.union(*local) - set.intersection(*local))
    assert not_universal
    with pytest.raises(ValueError):
        _protocol(plans, [not_universal[0]])


@pytest.mark.parametrize(
    "mutation", ["mask", "unknown", "source", "trial", "bool_position"]
)
def test_rehashed_protocol_tampering_cannot_change_masks_or_trials(
    mutation: str,
) -> None:
    plans = _plans()
    protocol = _protocol(plans)
    if mutation == "mask":
        protocol["retained_groups"] = ["e0-g0000"]
    elif mutation == "unknown":
        protocol["kv_state_preserved"] = True
    elif mutation == "source":
        protocol["candidate_parameters"] = dict(PARAMETERS, group_size=2)
    elif mutation == "trial":
        protocol["trials"].reverse()
    else:
        protocol["trials"][0]["position"] = True
    _rehash(protocol)
    with pytest.raises(ValueError):
        reduced.validate_reduced_protocol(protocol, plans)


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_reduced_protocol", *args])
    reduced.main()


def test_materializer_validates_then_creates_an_immutable_timing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = _plans()
    protocol = _protocol(plans, ["e0-g0000"])
    source = tmp_path / "protocol.json"
    source.write_text(json.dumps(protocol))
    paths = []
    for index, plan in enumerate(plans):
        path = tmp_path / f"plan-{index}.json"
        path.write_text(json.dumps(plan))
        paths.append(str(path))
    output = tmp_path / "materialized"
    args = (
        "materialize",
        "--protocol",
        str(source),
        "--plans",
        *paths,
        "--output-dir",
        str(output),
    )
    _cli(monkeypatch, *args)
    expected = {"protocol.json"} | {
        f"b{block:02d}-{condition}-timing.json"
        for block in range(1, 5)
        for condition in ("baseline", "candidate")
    }
    assert {path.name for path in output.iterdir()} == expected
    assert json.loads((output / "protocol.json").read_text()) == protocol
    for index, pair in enumerate(reduced.reduced_timings(plans, protocol), 1):
        for condition, descriptor in pair.items():
            assert (
                json.loads(
                    (output / f"b{index:02d}-{condition}-timing.json").read_text()
                )
                == descriptor
            )
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(FileExistsError):
        _cli(monkeypatch, *args)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_materializer_rejects_ambiguous_json_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_json: str
) -> None:
    source, plan_path, output = (
        tmp_path / "protocol.json",
        tmp_path / "plan.json",
        tmp_path / "output",
    )
    source.write_text(bad_json)
    plan_path.write_text(json.dumps(_plans()[0]))
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "materialize",
            "--protocol",
            str(source),
            "--plans",
            str(plan_path),
            "--output-dir",
            str(output),
        )
    assert not output.exists()


def test_original_protocol_is_not_silently_relabelled_as_reduced() -> None:
    plans = _plans()
    original = make_protocol(plans, candidate_parameters=PARAMETERS, **_options())
    before = copy.deepcopy(original)
    with pytest.raises(ValueError):
        reduced.validate_reduced_protocol(original, plans)
    assert original == before
