"""Offline timing mutations preserve a frozen request population and its window."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_arrival_timing as timing
from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes


def _plan(*, phase: str = "fixture", seed: int = 101) -> dict[str, Any]:
    return study.make_plan(
        phase=phase,  # type: ignore[arg-type]
        seed=seed,
        count=30,
        expected_prompt_tokens=286,
        duration_ns=101 if phase == "fixture" else 300_000_000_000,
    )


def _capacity_plan(*, pattern: str | None = None) -> dict[str, Any]:
    workload: dict[str, Any] = {
        "document_count": 8,
        "target_prefix_tokens": 256,
        "document_repeats": [12] * 8,
        "document_prefix_tokens": [256] * 8,
        "hot_group_size": 2,
        "cycle_percent": 60,
        "hotspot_epochs": ["A", "B", "A"],
        "context_length": 512,
        "output_tokens": 128,
    }
    if pattern is not None:
        workload["pattern_version"] = pattern
    if pattern == "burst-hot-shift.v1":
        workload.update(cycle_percent=20, burst_size=8, burst_window_ns=200_000_000)
    # These are synthetic lengths; no tokenizer or token certificate is implied.
    return study.make_capacity_plan(
        phase="fixture",
        seed=101,
        count=30,
        duration_ns=12_000_000_000,
        workload=workload,
        prompt_tokens_by_index=[272 + index % 3 for index in range(30)],
    )


def _make(plan: dict[str, Any], **parameters: Any) -> dict[str, Any]:
    return timing.make_timing(
        plan,
        **{
            "group_size": 4,
            "retained_spacing_bps": 2500,
            "max_advance_ns": plan["duration_ns"],
            **parameters,
        },
    )


def _rehash(artifact: dict[str, Any], field: str = "timing_sha256") -> None:
    artifact[field] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: value for key, value in artifact.items() if key != field}
            )
        ).hexdigest()
    )


@pytest.mark.parametrize("kind", ["fixture", "evaluation", "capacity", "control"])
def test_only_arrivals_change_and_the_frozen_window_is_preserved(kind: str) -> None:
    plan = (
        _capacity_plan(pattern="control.v1" if kind == "control" else None)
        if kind in {"capacity", "control"}
        else _plan(phase=kind)
    )
    frozen = canonical_json_bytes(plan)
    original = study.validate_plan(plan)
    descriptor = _make(plan)
    transformed = timing.validate_timing(plan, descriptor)

    assert descriptor == _make(plan)
    assert canonical_json_bytes(plan) == frozen
    assert descriptor["schema"] == timing.TIMING_SCHEMA
    assert descriptor["base_plan_sha256"] == plan["plan_sha256"]
    assert descriptor["base_trace_sha256"] == plan["trace_sha256"]
    assert descriptor["base_plan_schema"] == plan["schema"]
    assert descriptor["duration_ns"] == plan["duration_ns"]
    assert descriptor["offered_count"] == len(original) == len(transformed)
    assert descriptor["moved_count"] > 0
    assert descriptor["transformed_trace_sha256"] != descriptor["base_trace_sha256"]
    assert [
        replace(new, scheduled_ns=old.scheduled_ns)
        for old, new in zip(original, transformed, strict=True)
    ] == list(original)
    assert all(
        0 <= new.scheduled_ns <= old.scheduled_ns < plan["duration_ns"]
        and new.scheduled_ns * 3 // plan["duration_ns"] == old.epoch
        for old, new in zip(original, transformed, strict=True)
    )
    assert [offer.scheduled_ns for offer in transformed] == sorted(
        offer.scheduled_ns for offer in transformed
    )
    assert descriptor["max_applied_advance_ns"] == max(
        old.scheduled_ns - new.scheduled_ns
        for old, new in zip(original, transformed, strict=True)
    )
    identity = _make(plan, group_size=1)
    assert descriptor["population_sha256"] == identity["population_sha256"]
    # Public timing artifacts contain indices and hashes, never prompt text.
    serialized = json.dumps(descriptor)
    assert all(offer.prompt not in serialized for offer in original)


@pytest.mark.parametrize(
    "parameters",
    [{"group_size": 1}, {"retained_spacing_bps": 10000}, {"max_advance_ns": 0}],
)
def test_identity_controls_produce_the_original_trace(
    parameters: dict[str, int],
) -> None:
    plan = _plan()
    descriptor = _make(plan, **parameters)
    assert timing.validate_timing(plan, descriptor) == study.validate_plan(plan)
    assert descriptor["max_applied_advance_ns"] == descriptor["moved_count"] == 0


def test_ties_reset_at_each_epoch_without_crossing_its_boundary() -> None:
    plan = _plan()
    original = study.validate_plan(plan)
    descriptor = _make(plan, group_size=1024, retained_spacing_bps=0)
    transformed = timing.validate_timing(plan, descriptor)
    for epoch in range(3):
        original_epoch = [offer for offer in original if offer.epoch == epoch]
        transformed_epoch = [offer for offer in transformed if offer.epoch == epoch]
        assert len(original_epoch) > 1
        assert {offer.scheduled_ns for offer in transformed_epoch} == {
            original_epoch[0].scheduled_ns
        }
    assert len({offer.scheduled_ns for offer in transformed}) == 3


def test_maximum_advance_is_applied_without_moving_group_anchors() -> None:
    plan = _plan()
    original = study.validate_plan(plan)
    descriptor = _make(plan, group_size=4, retained_spacing_bps=0, max_advance_ns=1)
    transformed = timing.validate_timing(plan, descriptor)
    for epoch in range(3):
        old_epoch = [offer for offer in original if offer.epoch == epoch]
        new_epoch = [offer for offer in transformed if offer.epoch == epoch]
        for offset, (old, new) in enumerate(zip(old_epoch, new_epoch, strict=True)):
            anchor = old_epoch[offset // 4 * 4].scheduled_ns
            assert new.scheduled_ns == max(old.scheduled_ns - 1, anchor)
    assert descriptor["max_applied_advance_ns"] == 1


def test_fractional_spacing_floors_nanoseconds_before_the_advance_cap() -> None:
    plan = _plan()
    assert [offer.scheduled_ns for offer in study.validate_plan(plan)[:8]] == [
        1,
        6,
        8,
        10,
        14,
        17,
        20,
        23,
    ]
    descriptor = _make(plan, max_advance_ns=4)
    assert [row["scheduled_ns"] for row in descriptor["arrivals"][:8]] == [
        1,
        2,
        4,
        6,
        14,
        14,
        16,
        19,
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("group_size", value) for value in (0, -1, 1025, True, 2.0, "2")]
    + [("retained_spacing_bps", value) for value in (-1, 10001, False, 1.0, "1")]
    + [("max_advance_ns", value) for value in (-1, 102, True, 1.0, "1")],
)
def test_parameters_require_exact_integers_with_explicit_bounds(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        _make(_plan(), **{field: value})


@pytest.mark.parametrize("kind", ["seed", "phase", "tokens", "capacity_tokens"])
def test_timing_is_bound_to_the_exact_base_plan(kind: str) -> None:
    if kind == "capacity_tokens":
        plan = _capacity_plan()
        other = copy.deepcopy(plan)
        other["prompt_tokens_by_index"][0] += 1
        _rehash(other, "plan_sha256")
    else:
        plan = _plan(phase="evaluation")
        other = study.make_plan(
            phase="calibration" if kind == "phase" else "evaluation",
            seed=102 if kind == "seed" else 101,
            count=30,
            expected_prompt_tokens=285 if kind == "tokens" else 286,
        )
    study.validate_plan(other)
    with pytest.raises(ValueError):
        timing.validate_timing(other, _make(plan))


@pytest.mark.parametrize(
    "mutation",
    [
        "scheduled",
        "original",
        "order",
        "duplicate",
        "unknown",
        "parameters",
        "method",
        "population",
        "boolean_index",
        "boolean_moved",
        "float_schedule",
    ],
)
def test_rehashing_cannot_authorize_a_different_recipe(mutation: str) -> None:
    plan = _plan()
    descriptor = (
        _make(plan, group_size=1) if mutation == "boolean_moved" else _make(plan)
    )
    if mutation == "scheduled":
        descriptor["arrivals"][0]["scheduled_ns"] += 1
    elif mutation == "original":
        descriptor["arrivals"][0]["original_scheduled_ns"] += 1
    elif mutation == "order":
        descriptor["arrivals"].reverse()
    elif mutation == "duplicate":
        descriptor["arrivals"][1] = copy.deepcopy(descriptor["arrivals"][0])
    elif mutation == "unknown":
        descriptor["approval"] = "approved"
    elif mutation == "parameters":
        descriptor["parameters"]["group_size"] = 5
    elif mutation == "method":
        descriptor["method"] = "epoch-local-compression.v2"
    elif mutation == "population":
        descriptor["population_sha256"] = "sha256:" + "0" * 64
    elif mutation == "boolean_index":
        descriptor["arrivals"][0]["index"] = False
    elif mutation == "boolean_moved":
        descriptor["moved_count"] = False
    elif mutation == "float_schedule":
        descriptor["arrivals"][0]["scheduled_ns"] = float(
            descriptor["arrivals"][0]["scheduled_ns"]
        )
    _rehash(descriptor)
    with pytest.raises(ValueError):
        timing.validate_timing(plan, descriptor)


def test_changed_payload_without_rehash_is_rejected() -> None:
    plan = _plan()
    descriptor = _make(plan)
    descriptor["timing_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        timing.validate_timing(plan, descriptor)


def test_burst_hot_shift_cannot_be_used_as_a_timing_only_source() -> None:
    plan = _capacity_plan(pattern="burst-hot-shift.v1")
    study.validate_plan(plan)
    with pytest.raises(ValueError):
        _make(plan)


def _cli(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_arrival_timing", *arguments])
    return timing.main()


def test_cli_prepares_and_verifies_without_overwriting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_path, timing_path, report_path = (
        tmp_path / name for name in ("plan.json", "timing.json", "verified.json")
    )
    plan = _plan()
    plan_path.write_text(json.dumps(plan))
    arguments = (
        "prepare",
        "--plan",
        str(plan_path),
        "--group-size",
        "4",
        "--retained-spacing-bps",
        "2500",
        "--max-advance-ns",
        "101",
        "--output",
        str(timing_path),
    )
    _cli(monkeypatch, *arguments)
    descriptor = json.loads(timing_path.read_text())
    assert descriptor == _make(plan)
    _cli(
        monkeypatch,
        "verify",
        "--plan",
        str(plan_path),
        "--timing",
        str(timing_path),
        "--output",
        str(report_path),
    )
    report = json.loads(report_path.read_text())
    assert report["scope"] == "PLANNED_ARRIVAL_TIMING_ONLY"
    assert report["status"] == "VERIFIED"
    assert report["timing_sha256"] == descriptor["timing_sha256"]
    assert report["offered_count"] == len(descriptor["arrivals"])
    for repeat_arguments, output in (
        (arguments, timing_path),
        (
            (
                "verify",
                "--plan",
                str(plan_path),
                "--timing",
                str(timing_path),
                "--output",
                str(report_path),
            ),
            report_path,
        ),
    ):
        before = output.read_bytes()
        with pytest.raises((FileExistsError, SystemExit)):
            _cli(monkeypatch, *repeat_arguments)
        assert output.read_bytes() == before


@pytest.mark.parametrize(
    "bad_json",
    [
        '{"seed": 1, "seed": 2}',
        '{"seed": NaN}',
        '{"seed": Infinity}',
        '{"seed": -Infinity}',
    ],
)
@pytest.mark.parametrize("target", ["plan", "timing"])
def test_cli_rejects_ambiguous_or_nonfinite_json_before_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_json: str, target: str
) -> None:
    plan = _plan()
    plan_path, timing_path, output = (
        tmp_path / name for name in ("plan.json", "timing.json", "output.json")
    )
    plan_path.write_text(json.dumps(plan))
    timing_path.write_text(json.dumps(_make(plan)))
    (plan_path if target == "plan" else timing_path).write_text(bad_json)
    with pytest.raises((ValueError, SystemExit)):
        _cli(
            monkeypatch,
            "verify",
            "--plan",
            str(plan_path),
            "--timing",
            str(timing_path),
            "--output",
            str(output),
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed", True),
        ("offered_count", 30.0),
        ("ignore_eos", 1),
        ("stream_usage_required", 1),
    ],
)
def test_source_plan_rejects_boolean_number_aliases_even_when_rehashed(
    field: str, value: Any
) -> None:
    plan = _plan()
    plan[field] = value
    _rehash(plan, "plan_sha256")
    with pytest.raises(ValueError):
        _make(plan)
