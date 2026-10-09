"""Restoring whole original groups changes timing without deleting request history."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_reduced_timing as reduced
from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing, validate_timing

PARAMETERS = {
    "group_size": 3,
    "retained_spacing_bps": 2500,
    "max_advance_ns": 1_000_000_000,
}


def _plan(kind: str = "baseline") -> dict[str, Any]:
    if kind == "baseline":
        return study.make_plan(
            phase="fixture",
            seed=100,
            count=12,
            expected_prompt_tokens=200,
            duration_ns=3_000_000_000,
            max_tokens=4,
        )
    return study.make_capacity_plan(
        phase="fixture",
        seed=100,
        count=12,
        duration_ns=3_000_000_000,
        workload={
            "document_count": 8,
            "target_prefix_tokens": 256,
            "document_repeats": [12] * 8,
            "document_prefix_tokens": [256] * 8,
            "hot_group_size": 2,
            "cycle_percent": 60,
            "hotspot_epochs": ["A", "B", "A"],
            "context_length": 512,
            "output_tokens": 4,
        },
        prompt_tokens_by_index=[272 + index % 3 for index in range(12)],
    )


def _rehash(value: dict[str, Any]) -> None:
    value["timing_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != "timing_sha256"}
            )
        ).hexdigest()
    )


def test_original_group_ids_and_membership_include_inactive_tails() -> None:
    plan = _plan()
    groups = reduced.original_groups(plan, PARAMETERS)
    assert [group["group_id"] for group in groups] == [
        "e0-g0000",
        "e0-g0001",
        "e1-g0000",
        "e1-g0001",
        "e2-g0000",
        "e2-g0001",
    ]
    assert [group["indices"] for group in groups] == [
        [0, 1, 2],
        [3],
        [4, 5, 6],
        [7],
        [8, 9, 10],
        [11],
    ]
    assert [group["ordinal"] for group in groups] == [0, 1, 0, 1, 0, 1]
    assert [group["moved_count"] for group in groups] == [2, 0, 2, 0, 2, 0]
    assert [group["epoch"] for group in groups] == [0, 0, 1, 1, 2, 2]


@pytest.mark.parametrize("kind", ["baseline", "capacity"])
def test_removing_groups_restores_only_their_times_and_preserves_every_offer(
    kind: str,
) -> None:
    plan = _plan(kind)
    before = canonical_json_bytes(plan)
    root = make_timing(plan, **PARAMETERS)
    original = study.validate_plan(plan)
    retained = ["e0-g0000", "e2-g0000"]
    descriptor = reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=retained
    )
    offers = reduced.validate_reduced_timing(plan, descriptor)
    assert offers == validate_timing(plan, descriptor)
    assert descriptor["schema"] == reduced.SCHEMA
    assert descriptor["method"] == "original-group-retention.v1"
    assert descriptor["source_timing_sha256"] == root["timing_sha256"]
    assert descriptor["source_parameters"] == PARAMETERS
    assert descriptor["retained_groups"] == retained
    assert descriptor["population_sha256"] == root["population_sha256"]
    assert descriptor["duration_ns"] == plan["duration_ns"]
    assert descriptor["offered_count"] == len(original)
    assert canonical_json_bytes(plan) == before
    assert [
        replace(new, scheduled_ns=old.scheduled_ns)
        for old, new in zip(original, offers, strict=True)
    ] == list(original)
    for group in reduced.original_groups(plan, PARAMETERS):
        for index in group["indices"]:
            expected = (
                root["arrivals"][index]["scheduled_ns"]
                if group["group_id"] in retained
                else original[index].scheduled_ns
            )
            assert offers[index].scheduled_ns == expected
    assert [offer.scheduled_ns for offer in offers] == sorted(
        offer.scheduled_ns for offer in offers
    )
    assert [offer.epoch for offer in offers] == [offer.epoch for offer in original]


def test_full_mask_preserves_the_root_schedule_and_empty_mask_restores_identity() -> (
    None
):
    plan = _plan()
    root = make_timing(plan, **PARAMETERS)
    active = [
        group["group_id"]
        for group in reduced.original_groups(plan, PARAMETERS)
        if group["moved_count"]
    ]
    full = reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=list(reversed(active))
    )
    assert full["retained_groups"] == active
    assert full["arrivals"] == root["arrivals"]
    assert full["transformed_trace_sha256"] == root["transformed_trace_sha256"]
    assert full["timing_sha256"] != root["timing_sha256"]
    empty = reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=[]
    )
    assert reduced.validate_reduced_timing(plan, empty) == study.validate_plan(plan)
    assert empty["moved_count"] == empty["max_applied_advance_ns"] == 0
    assert empty["population_sha256"] == full["population_sha256"]


@pytest.mark.parametrize(
    "mask",
    [["e0-g0000", "e0-g0000"], ["e0-g0001"], ["e9-g9999"], [True], "e0-g0000", None],
)
def test_local_masks_reject_duplicates_inactive_unknown_and_nonstring_groups(
    mask: Any,
) -> None:
    with pytest.raises(ValueError):
        reduced.make_reduced_timing(
            _plan(), source_parameters=PARAMETERS, retained_groups=mask
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "scheduled",
        "original",
        "index_bool",
        "float_schedule",
        "unknown",
        "source",
        "mask",
        "root_hash",
        "population",
    ],
)
def test_rehashed_artifacts_cannot_change_the_original_group_recipe(
    mutation: str,
) -> None:
    plan = _plan()
    descriptor = reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=["e0-g0000"]
    )
    if mutation == "scheduled":
        descriptor["arrivals"][4]["scheduled_ns"] -= 1
    elif mutation == "original":
        descriptor["arrivals"][0]["original_scheduled_ns"] += 1
    elif mutation == "index_bool":
        descriptor["arrivals"][0]["index"] = False
    elif mutation == "float_schedule":
        descriptor["arrivals"][0]["scheduled_ns"] = float(
            descriptor["arrivals"][0]["scheduled_ns"]
        )
    elif mutation == "unknown":
        descriptor["state_preserved"] = True
    elif mutation == "source":
        descriptor["source_parameters"] = dict(PARAMETERS, group_size=4)
    elif mutation == "mask":
        descriptor["retained_groups"] = ["e2-g0000"]
    elif mutation == "root_hash":
        descriptor["source_timing_sha256"] = "sha256:" + "0" * 64
    else:
        descriptor["population_sha256"] = "sha256:" + "0" * 64
    _rehash(descriptor)
    with pytest.raises(ValueError):
        reduced.validate_reduced_timing(plan, descriptor)


def test_source_plan_and_parameters_cannot_be_swapped() -> None:
    plan = _plan()
    descriptor = reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=["e0-g0000"]
    )
    other = study.make_plan(
        phase="fixture",
        seed=101,
        count=12,
        expected_prompt_tokens=200,
        duration_ns=3_000_000_000,
        max_tokens=4,
    )
    with pytest.raises(ValueError):
        reduced.validate_reduced_timing(other, descriptor)
    bad_parameters = copy.deepcopy(PARAMETERS)
    bad_parameters["group_size"] = True
    with pytest.raises(ValueError):
        reduced.original_groups(plan, bad_parameters)


def _cli(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_reduced_timing", *arguments])
    reduced.main()


def test_cli_prepares_verifies_and_refuses_nested_sources_or_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    plan_path, root_path, mask_path, output, report_path = (
        tmp_path / name
        for name in (
            "plan.json",
            "root.json",
            "mask.json",
            "reduced.json",
            "verified.json",
        )
    )
    plan_path.write_text(json.dumps(plan))
    root_path.write_text(json.dumps(make_timing(plan, **PARAMETERS)))
    mask_path.write_text(json.dumps(["e0-g0000"]))
    arguments = (
        "prepare",
        "--plan",
        str(plan_path),
        "--source-timing",
        str(root_path),
        "--retained-groups",
        str(mask_path),
        "--output",
        str(output),
    )
    _cli(monkeypatch, *arguments)
    descriptor = json.loads(output.read_text())
    assert descriptor == reduced.make_reduced_timing(
        plan, source_parameters=PARAMETERS, retained_groups=["e0-g0000"]
    )
    verify_args = (
        "verify",
        "--plan",
        str(plan_path),
        "--timing",
        str(output),
        "--output",
        str(report_path),
    )
    _cli(monkeypatch, *verify_args)
    report = json.loads(report_path.read_text())
    assert report["scope"] == "PLANNED_ARRIVAL_TIMING_ONLY"
    assert report["status"] == "VERIFIED"
    assert report["timing_sha256"] == descriptor["timing_sha256"]
    for args, path in ((arguments, output), (verify_args, report_path)):
        before = path.read_bytes()
        with pytest.raises(FileExistsError):
            _cli(monkeypatch, *args)
        assert path.read_bytes() == before
    nested = tmp_path / "nested.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "prepare",
            "--plan",
            str(plan_path),
            "--source-timing",
            str(output),
            "--retained-groups",
            str(mask_path),
            "--output",
            str(nested),
        )
    assert not nested.exists()


@pytest.mark.parametrize("target", ["plan", "root", "mask"])
@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_cli_rejects_ambiguous_input_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, bad_json: str
) -> None:
    plan = _plan()
    paths = {name: tmp_path / f"{name}.json" for name in ("plan", "root", "mask")}
    paths["plan"].write_text(json.dumps(plan))
    paths["root"].write_text(json.dumps(make_timing(plan, **PARAMETERS)))
    paths["mask"].write_text(json.dumps(["e0-g0000"]))
    paths[target].write_text(bad_json)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "prepare",
            "--plan",
            str(paths["plan"]),
            "--source-timing",
            str(paths["root"]),
            "--retained-groups",
            str(paths["mask"]),
            "--output",
            str(output),
        )
    assert not output.exists()
