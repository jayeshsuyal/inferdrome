"""Finite matched compilation, explicit declarations and study resource bounds."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from inferdrome.evaluation.contracts import MAX_INPUT_BYTES, EvaluationError
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.study_config import (
    MAX_BLOCKS,
    MAX_STUDY_DURATION_NS,
    MAX_TOTAL_OUTPUT_BYTES,
    MAX_TRIAL_RESULT_BYTES,
    PLAN_MANIFEST_RESERVE_BYTES,
    StudyConfig,
    compile_study,
    load_study_config_bytes,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_faults import MS
from tests.unit.test_evaluation_faults import payload as fault_payload


def study_payload(scenario: str = "HEALTHY") -> dict:
    recipe = fault_payload()
    block = {
        "block_id": "block-a",
        "profile_id": "rehearsal",
        "scenario": scenario,
        "repeat_index": 0,
        "workload_seed": 17,
        "order_seed": 23,
        "policy_order": list(POLICY_IDS),
        "foreground": recipe["foreground"],
        "telemetry": recipe["telemetry"],
    }
    if scenario == "STALE_LOAD":
        block.update(background=recipe["background"], fault=recipe["fault"])
    return {
        "schema_version": "inferdrome.evaluation-study-config.v1",
        "profiles": [
            {
                "profile_id": "rehearsal",
                "window_start_ns": 20 * MS,
                "window_end_ns": 200 * MS,
                "first_content_slo_ns": 5 * MS,
                "completion_slo_ns": 10 * MS,
            }
        ],
        "blocks": [block],
        "preparation": {
            "cache_state": "UNKNOWN",
            "prefix_caching": "UNKNOWN",
            "model_warmup_reference": "sha256:" + "0" * 64,
        },
        "limits": {},
        "reporting": {"bootstrap_seed": 37},
    }


def load(value: dict) -> StudyConfig:
    return load_study_config_bytes(json.dumps(value).encode())


def blocks(value: dict, count: int, *, rotate_targets: bool = True) -> None:
    original = value["blocks"][0]
    value["blocks"] = []
    for index in range(count):
        block = deepcopy(original)
        block["block_id"] = f"block-{index:02d}"
        block["repeat_index"] = index
        rotation = index % 4
        block["policy_order"] = list(POLICY_IDS[rotation:] + POLICY_IDS[:rotation])
        if block["scenario"] == "STALE_LOAD":
            target = "endpoint-b" if index % 2 and rotate_targets else "endpoint-a"
            block["fault"]["target_endpoint_id"] = target
            for offer in block["background"]["offers"]:
                offer["endpoint_id"] = target
            if rotate_targets:
                block["repeat_index"] = index // 2
        value["blocks"].append(block)


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_four_configs_differ_only_by_policy(scenario: str) -> None:
    config = load(study_payload(scenario))
    plan = compile_study(config)
    assert [trial.trial_id for trial in plan.trials] == [
        f"trial-{index:04d}" for index in range(4)
    ]
    assert tuple(trial.policy_id for trial in plan.trials) == POLICY_IDS
    assert len({trial.workload_sha256 for trial in plan.trials}) == 1
    assert len({trial.config_sha256 for trial in plan.trials}) == 4
    recipes = []
    for trial in plan.trials:
        recipe = trial.config.model_dump(mode="json")
        assert trial.config_sha256 == sha256_digest(canonical_json_bytes(recipe))
        del recipe["policy_id"]
        recipes.append(canonical_json_bytes(recipe))
        assert trial.config.foreground.offers == config.blocks[0].foreground.offers
        assert trial.window_start_ns == 20 * MS
        assert trial.window_end_ns == 200 * MS
        assert trial.first_content_slo_ns == 5 * MS
        assert trial.completion_slo_ns == 10 * MS
        assert trial.workload_seed == 17
        assert trial.order_seed == 23
        if scenario == "HEALTHY":
            assert isinstance(trial.config, HealthyRoutingConfig)
            assert trial.target_endpoint_id is None
            assert "background" not in recipe and "fault" not in recipe
        else:
            assert isinstance(trial.config, RoutingFaultConfig)
            assert trial.target_endpoint_id == "endpoint-a"
            assert trial.config.background == config.blocks[0].background
    assert len(set(recipes)) == 1
    assert plan.planned_request_count == (24 if scenario == "HEALTHY" else 32)


def test_serialization_whitespace_and_order_do_not_change_compilation() -> None:
    value = study_payload()
    first = compile_study(load(value))
    second = compile_study(
        load_study_config_bytes(json.dumps(value, sort_keys=True, indent=2).encode())
    )
    assert first == second
    assert canonical_json_bytes(first.to_dict()) == canonical_json_bytes(
        second.to_dict()
    )
    changed = deepcopy(value)
    changed["blocks"][0]["policy_order"].reverse()
    reordered = compile_study(load(changed))
    assert reordered.config_sha256 != first.config_sha256
    assert [trial.policy_id for trial in reordered.trials] == list(reversed(POLICY_IDS))
    assert reordered.trials[0].workload_sha256 == first.trials[0].workload_sha256


@pytest.mark.parametrize("population", ["foreground", "background"])
@pytest.mark.parametrize("field", ["prompt", "scheduled_ns"])
def test_matched_identity_binds_exact_offers(population: str, field: str) -> None:
    value = study_payload("STALE_LOAD")
    previous = compile_study(load(value))
    offer = value["blocks"][0][population]["offers"][0]
    offer[field] = offer[field] + ("!" if field == "prompt" else 1)
    changed = compile_study(load(value))
    assert changed.trials[0].workload_sha256 != previous.trials[0].workload_sha256
    assert changed.trials[0].config_sha256 != previous.trials[0].config_sha256


def test_origin_changes_bind_config_but_not_matched_payload() -> None:
    value = study_payload("STALE_LOAD")
    before = compile_study(load(value))
    for population in ("foreground", "background"):
        value["blocks"][0][population]["endpoints"][0]["origin"] = (
            "http://10.1.2.3:8001"
        )
    after = compile_study(load(value))
    assert before.trials[0].workload_sha256 == after.trials[0].workload_sha256
    assert before.trials[0].config_sha256 != after.trials[0].config_sha256
    assert before.config_sha256 != after.config_sha256


def test_plan_redacts_private_workload_and_keeps_unverified_declarations() -> None:
    plan = compile_study(load(study_payload("STALE_LOAD")))
    value = plan.to_dict()
    raw = canonical_json_bytes(value)
    for secret in (
        b"private foreground",
        b"private background",
        b"private-model",
        b"127.0.0.1",
    ):
        assert secret not in raw
        assert secret.decode() not in repr(plan)
    assert value["evidence_eligible"] is False
    assert value["source_identity"] == value["endpoint_identity"] == "UNVERIFIED"
    assert value["calibration"] == "UNCALIBRATED_REHEARSAL"
    assert value["cost"] == "UNAVAILABLE"
    assert value["model_warmup_requests"] == "EXTERNAL_UNMEASURED"
    assert value["model_warmup_duration_ns"] == "EXTERNAL_UNMEASURED"
    assert value["model_warmup_tokens"] == "EXTERNAL_UNMEASURED"
    assert plan.reporting.bootstrap_seed == 37
    assert plan.reporting.p99_min_successes == 1000
    assert value["reporting"] == plan.reporting.model_dump(mode="json")


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_ordered_repeats_policy_positions_and_fault_target_rotation(
    scenario: str,
) -> None:
    value = study_payload(scenario)
    blocks(value, 8)
    plan = compile_study(load(value))
    assert len(plan.trials) == 32
    for index, block in enumerate(value["blocks"]):
        group = plan.trials[index * 4 : index * 4 + 4]
        assert [trial.policy_id for trial in group] == block["policy_order"]
        assert len({trial.workload_sha256 for trial in group}) == 1
        assert {trial.repeat_index for trial in group} == {block["repeat_index"]}
    if scenario == "STALE_LOAD":
        assert [trial.target_endpoint_id for trial in plan.trials[::4]] == [
            "endpoint-a",
            "endpoint-b",
        ] * 4


@pytest.mark.parametrize(
    "case", ["positions", "repeat_gap", "repeat_duplicate", "target"]
)
def test_invalid_block_design_rejected(case: str) -> None:
    value = study_payload("STALE_LOAD")
    blocks(value, 4)
    if case == "positions":
        value["blocks"][1]["policy_order"] = list(POLICY_IDS)
    elif case == "repeat_gap":
        value["blocks"][2]["repeat_index"] = 2
    elif case == "repeat_duplicate":
        value["blocks"][2]["repeat_index"] = 0
    else:
        blocks(value, 4, rotate_targets=False)
    with pytest.raises(
        EvaluationError, match="study configuration violates its contract"
    ):
        load(value)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("unknown",), "private-sentinel"),
        (("schema_version",), "inferdrome.evaluation-config.v1"),
        (("profiles", 0, "profile_id"), "../private-sentinel"),
        (("profiles", 0, "calibration_status"), "CALIBRATED"),
        (("profiles", 0, "window_start_ns"), 31 * MS),
        (("profiles", 0, "window_start_ns"), 19 * MS),
        (("profiles", 0, "window_end_ns"), 201 * MS),
        (("profiles", 0, "first_content_slo_ns"), 11 * MS),
        (("profiles", 0, "completion_slo_ns"), 21 * MS),
        (("profiles", 0, "completion_slo_ns"), 200 * MS),
        (("profiles", 0, "window_start_ns"), True),
        (("preparation", "cache_state"), "VERIFIED_COLD"),
        (("preparation", "prefix_caching"), True),
        (("preparation", "model_warmup"), "REPLAY_HOOK"),
        (("preparation", "model_warmup_reference"), "/tmp/private-sentinel"),
        (("preparation", "runtime_verification"), "VERIFIED"),
        (("preparation", "cost"), "0.00"),
        (("preparation", "hook"), "/private/sentinel.sh"),
        (("reporting", "p99_min_successes"), 999),
        (("reporting", "p99_min_successes"), 1000.0),
        (("reporting", "bootstrap_resamples"), 2000.0),
        (("reporting", "confidence_percent"), 90.0),
        (("reporting", "minimum_complete_blocks"), 8.0),
        (("reporting", "bootstrap_seed"), True),
        (("reporting", "bootstrap_seed"), 2**32),
        (("blocks", 0, "workload_seed"), -1),
        (("blocks", 0, "workload_seed"), 17.0),
        (("blocks", 0, "order_seed"), "23"),
        (("blocks", 0, "policy_order"), [POLICY_IDS[0]] * 4),
        (("blocks", 0, "scenario"), "CALIBRATION"),
        (("blocks", 0, "profile_id"), "missing"),
        (("blocks", 0, "foreground", "source_commit"), "main"),
        (("blocks", 0, "foreground", "bounds", "max_requests"), 4001),
        (("blocks", 0, "foreground", "bounds", "max_content_events"), 4097),
        (("blocks", 0, "telemetry", "max_observations"), 6),
        (("blocks", 0, "background"), fault_payload()["background"]),
        (("blocks", 0, "fault"), fault_payload()["fault"]),
    ],
)
def test_private_malformed_contract_is_closed_and_sanitized(
    path: tuple, replacement: object
) -> None:
    value = study_payload()
    target = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    with pytest.raises(EvaluationError) as error:
        load(value)
    assert str(error.value) == "study configuration violates its contract"


@pytest.mark.parametrize("field", ["background", "fault"])
def test_fault_recipe_requires_both_populations_and_phases(field: str) -> None:
    value = study_payload("STALE_LOAD")
    del value["blocks"][0][field]
    with pytest.raises(EvaluationError):
        load(value)


def test_healthy_timing_capacity_retains_comparable_trial_ceiling() -> None:
    value = study_payload()
    bounds = value["blocks"][0]["foreground"]["bounds"]
    bounds["max_requests"] = 1000
    bounds["max_content_events"] = 500
    load(value)
    bounds["max_content_events"] = 501
    with pytest.raises(EvaluationError):
        load(value)


def test_foreground_and_background_share_generation_settings() -> None:
    value = study_payload("STALE_LOAD")
    value["blocks"][0]["background"]["max_tokens"] = 1
    with pytest.raises(EvaluationError):
        load(value)


@pytest.mark.parametrize("field", ["source_commit", "model", "max_tokens", "endpoints"])
def test_serving_and_request_settings_fixed_across_blocks(field: str) -> None:
    value = study_payload()
    blocks(value, 2)
    changed = value["blocks"][1]["foreground"]
    if field == "endpoints":
        changed[field][0]["origin"] = "http://10.0.0.1:8001"
    else:
        changed[field] = {
            "source_commit": "b" * 40,
            "model": "other",
            "max_tokens": 64,
        }[field]
    with pytest.raises(EvaluationError):
        load(value)


@pytest.mark.parametrize(
    "case", ["duplicate_profile", "unused_profile", "duplicate_block"]
)
def test_duplicate_or_unused_identities_rejected(case: str) -> None:
    value = study_payload()
    if case == "duplicate_block":
        value["blocks"].append(deepcopy(value["blocks"][0]))
    else:
        profile = deepcopy(value["profiles"][0])
        if case == "unused_profile":
            profile["profile_id"] = "unused"
        value["profiles"].append(profile)
    with pytest.raises(EvaluationError):
        load(value)


def test_exact_duration_request_and_output_bounds_include_cleanup_and_gaps() -> None:
    value = study_payload("STALE_LOAD")
    one_trial = (200 + 20 + 7 * 20 + 4 * 20) * MS
    total_duration = 4 * one_trial + 3 * MS
    total_output = 4 * 12345 + PLAN_MANIFEST_RESERVE_BYTES
    value["limits"] = {
        "max_trials": 4,
        "max_planned_requests": 32,
        "max_duration_ns": total_duration,
        "cooldown_ns": MS,
        "per_trial_result_bytes": 12345,
        "total_output_bytes": total_output,
    }
    plan = compile_study(load(value))
    assert plan.planned_request_count == 32
    assert plan.worst_case_duration_ns == total_duration
    assert plan.reserved_result_bytes == total_output
    assert {trial.worst_case_duration_ns for trial in plan.trials} == {one_trial}
    assert len(canonical_json_bytes(plan.to_dict())) < PLAN_MANIFEST_RESERVE_BYTES
    for key in ("max_planned_requests", "max_duration_ns", "total_output_bytes"):
        changed = deepcopy(value)
        changed["limits"][key] -= 1
        with pytest.raises(EvaluationError):
            load(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_trials", 257),
        ("max_planned_requests", 100001),
        ("max_duration_ns", MAX_STUDY_DURATION_NS + 1),
        ("per_trial_result_bytes", MAX_TRIAL_RESULT_BYTES + 1),
        ("total_output_bytes", MAX_TOTAL_OUTPUT_BYTES + 1),
        ("cooldown_ns", 60_000_000_001),
        ("per_trial_result_bytes", 0),
        ("max_trials", True),
    ],
)
def test_hard_campaign_limits_cannot_be_raised(field: str, value: int) -> None:
    payload = study_payload()
    payload["limits"][field] = value
    with pytest.raises(EvaluationError):
        load(payload)


def test_64_blocks_compile_to_256_trials_but_65_are_rejected() -> None:
    value = study_payload()
    blocks(value, MAX_BLOCKS)
    plan = compile_study(load(value))
    assert len(plan.trials) == 256
    assert plan.trials[-1].trial_id == "trial-0255"
    assert len(canonical_json_bytes(plan.to_dict())) < PLAN_MANIFEST_RESERVE_BYTES
    value["blocks"].append(deepcopy(value["blocks"][-1]))
    with pytest.raises(EvaluationError):
        load(value)


def test_per_result_and_aggregate_storage_caps_are_both_enforced() -> None:
    value = study_payload()
    blocks(value, 4)
    value["limits"]["per_trial_result_bytes"] = MAX_TRIAL_RESULT_BYTES
    with pytest.raises(EvaluationError):
        load(value)  # 16 * 64 MiB leaves no reserved metadata space inside 1 GiB.


@pytest.mark.parametrize("extra", [0, 1])
def test_100000_total_request_ceiling_counts_each_policy(extra: int) -> None:
    value = study_payload()
    blocks(value, 8)
    for index, block in enumerate(value["blocks"]):
        count = 3125 + (extra if index == 0 else 0)
        foreground = block["foreground"]
        foreground["offers"] = [deepcopy(foreground["offers"][0]) for _ in range(count)]
        foreground["bounds"]["max_requests"] = count
    if extra:
        with pytest.raises(EvaluationError):
            load(value)
    else:
        plan = compile_study(load(value))
        assert plan.planned_request_count == 100_000


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b'{"blocks": [], "blocks": []}',
        b'{"bootstrap_seed": NaN}',
        b'{"bootstrap_seed": Infinity}',
        b'{"bootstrap_seed": 1e999}',
        b"[" * 1000 + b"]" * 1000,
        b" " * (MAX_INPUT_BYTES + 1),
    ],
)
def test_ambiguous_or_unbounded_json_rejected(raw: bytes) -> None:
    with pytest.raises(
        EvaluationError, match="study configuration violates its contract"
    ):
        load_study_config_bytes(raw)
