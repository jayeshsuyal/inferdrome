"""Protocol-only tests for deterministic load-sweep selection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.load_calibration import (
    CompiledCalibrationPlan,
    calibration_plan_bytes,
    compile_calibration,
    confirmation_plan,
    load_calibration_observations_bytes,
    load_calibration_protocol_bytes,
    protocol_bytes,
    select_calibration_level,
)
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.routing_execution.canonical import canonical_json_bytes


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def protocol_payload() -> dict[str, Any]:
    return {
        "schema_version": "inferdrome.evaluation-load-calibration-protocol.v1",
        "protocol_id": "qwen3-load-sweep",
        "source_commit": "a" * 40,
        "workload": {
            "model": "Qwen/Qwen3-8B",
            "model_revision_sha256": _digest("a"),
            "workload_sha256": _digest("b"),
            "trace_sha256": _digest("c"),
            "token_lengths": {
                "prompt_tokens": 128,
                "completion_tokens": 64,
            },
        },
        "levels": [
            {
                "level_id": "load-40",
                "study_config_sha256": _digest("d"),
                "study_plan_sha256": _digest("e"),
                "offered_rate_millirps": 40_000,
                "planned_requests": 8,
                "worst_case_duration_ns": 200_000_000,
            },
            {
                "level_id": "load-80",
                "study_config_sha256": _digest("f"),
                "study_plan_sha256": _digest("0"),
                "offered_rate_millirps": 80_000,
                "planned_requests": 16,
                "worst_case_duration_ns": 200_000_000,
            },
        ],
        "policy_order": list(POLICY_IDS),
        "calibration_repetitions": 2,
        "confirmation_repetitions": 3,
        "first_content_slo_ns": 5_000_000,
        "completion_slo_ns": 10_000_000,
        "preparation": {"warmup_reset_max_duration_ns": 10_000_000},
        "selection_rule": {},
        "per_trial_output_bytes": 100_000,
        "max_session_duration_ns": 20_000_000_000,
        "max_session_output_bytes": 4_000_000,
    }


def _observations(
    plan: CompiledCalibrationPlan,
    *,
    fail_high: bool = False,
    incomplete_high: bool = False,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for trial in plan.trials:
        high = trial.level_id == "load-80"
        offered = trial.planned_requests
        terminal = offered - 1 if high and incomplete_high else offered
        success = terminal
        good = offered - 1 if high and fail_high else success
        rows.append(
            {
                "trial_id": trial.trial_id,
                "level_id": trial.level_id,
                "repeat_index": trial.repeat_index,
                "policy_id": trial.policy_id,
                "study_plan_sha256": trial.study_plan_sha256,
                "offered_count": offered,
                "dispatched_count": terminal,
                "terminal_count": terminal,
                "outcomes": {
                    "SUCCESS": success,
                    "REJECTED_CAPACITY": 0,
                    "REJECTED_ROUTE": 0,
                    "TIMEOUT": 0,
                    "HTTP_ERROR": 0,
                    "STREAM_ERROR": 0,
                    "STREAM_LIMIT": 0,
                    "INCOMPLETE_STREAM": 0,
                    "TRANSPORT_ERROR": 0,
                    "CANCELLED": 0,
                    "DRAIN_TIMEOUT": 0,
                    "INTERNAL_ERROR": 0,
                },
                "slo_good_count": good,
                "achieved_concurrency_peak": 2,
                "client_queue_peak": 0,
                "first_content_p95_ns": 2_000_000 if success else None,
                "terminal_p95_ns": 4_000_000 if success else None,
                "offered_window_ns": 200_000_000,
            }
        )
    return {
        "schema_version": "inferdrome.evaluation-load-calibration-observations.v1",
        "protocol_sha256": plan.protocol_sha256,
        "trials": rows,
    }


def test_protocol_freezes_sweep_reset_selection_and_confirmation_order() -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)

    assert len(plan.trials) == 16
    assert [trial.trial_id for trial in plan.trials] == [
        f"calibration-{index:04d}" for index in range(16)
    ]
    assert [trial.level_id for trial in plan.trials[:8]] == ["load-40"] * 8
    assert [trial.policy_id for trial in plan.trials[:4]] == list(POLICY_IDS)
    assert [trial.policy_id for trial in plan.trials[4:8]] == list(
        POLICY_IDS[1:] + POLICY_IDS[:1]
    )
    plan_payload = plan.to_dict()
    preparation = cast(dict[str, str], plan_payload["preparation"])
    selection_rule = cast(dict[str, str], plan_payload["selection_rule"])
    assert preparation["reset_before_each_trial"] == "DECLARED_COLD_RESET"
    assert selection_rule["primary_metric"] == (
        "MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS"
    )
    assert calibration_plan_bytes(plan) == canonical_json_bytes(plan.to_dict()) + b"\n"

    observations = load_calibration_observations_bytes(
        json.dumps(_observations(plan)).encode()
    )
    selection = select_calibration_level(plan, observations)
    confirmation = confirmation_plan(plan, selection)
    assert selection.status == "SELECTED"
    assert selection.selected_level_id == "load-80"
    assert confirmation.status == "READY"
    assert len(confirmation.trials) == 24
    assert {row.level_id for row in confirmation.trials} == {"load-80"}
    assert [row.policy_id for row in confirmation.trials[:4]] == list(
        POLICY_IDS[2:] + POLICY_IDS[:2]
    )
    assert [row.scenario for row in confirmation.trials[:4]] == ["HEALTHY"] * 4
    assert [row.scenario for row in confirmation.trials[4:8]] == ["STALE_LOAD"] * 4
    assert [row.policy_id for row in confirmation.trials[4:8]] == list(POLICY_IDS)


def test_legacy_protocol_canonical_bytes_do_not_gain_cleanup_allowance() -> None:
    payload = protocol_payload()
    protocol = load_calibration_protocol_bytes(json.dumps(payload).encode())
    encoded = protocol_bytes(protocol)

    assert protocol.preparation.cleanup_max_duration_ns is None
    assert hashlib.sha256(encoded).hexdigest() == (
        "472083071f861ccc67d002a665c85bbea9096f7b74db7650bffbb868789c1262"
    )
    assert b"cleanup_max_duration_ns" not in encoded


@pytest.mark.parametrize("kind", ["slo", "incomplete"])
def test_selection_never_promotes_a_level_with_a_missing_or_bad_terminal(
    kind: str,
) -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)
    observations = load_calibration_observations_bytes(
        json.dumps(
            _observations(
                plan, fail_high=kind == "slo", incomplete_high=kind == "incomplete"
            )
        ).encode()
    )

    selection = select_calibration_level(plan, observations)
    confirmation = confirmation_plan(plan, selection)
    assert selection.status == (
        "NO_ADMISSIBLE_LEVEL" if kind == "incomplete" else "SELECTED"
    )
    assert selection.selected_level_id == (None if kind == "incomplete" else "load-40")
    assert confirmation.selected_level_id == (
        None if kind == "incomplete" else "load-40"
    )
    assert confirmation.status == (
        "NO_CONFIRMATION_ALLOWED" if kind == "incomplete" else "READY"
    )
    if kind == "incomplete":
        assert not confirmation.trials
    high = next(row for row in selection.assessments if row.level_id == "load-80")
    assert high.reason == (
        "SLO_MISS_OR_LATENCY_TARGET_MISS"
        if kind == "slo"
        else "INCOMPLETE_TERMINAL_POPULATION"
    )


def test_population_claims_require_dispatch_and_observed_activity() -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)
    payload = _observations(plan)
    payload["trials"][0].update(
        dispatched_count=0,
        achieved_concurrency_peak=0,
    )
    with pytest.raises(EvaluationError, match="load calibration input"):
        load_calibration_observations_bytes(json.dumps(payload).encode())

    payload = _observations(plan)
    payload["trials"][0]["achieved_concurrency_peak"] = 0
    with pytest.raises(EvaluationError, match="load calibration input"):
        load_calibration_observations_bytes(json.dumps(payload).encode())


def test_impossible_rate_window_is_rejected_before_observation() -> None:
    payload = protocol_payload()
    payload["levels"][0]["offered_rate_millirps"] = 40_001
    with pytest.raises(EvaluationError, match="load calibration input"):
        load_calibration_protocol_bytes(json.dumps(payload).encode())


def test_preflight_and_confirmation_share_one_trial_bound() -> None:
    payload = protocol_payload()
    payload.update(
        calibration_repetitions=1,
        confirmation_repetitions=17,
        max_session_duration_ns=40_000_000_000,
        max_session_output_bytes=20_000_000,
    )
    protocol = load_calibration_protocol_bytes(json.dumps(payload).encode())
    plan = compile_calibration(protocol)
    selection = select_calibration_level(
        plan,
        load_calibration_observations_bytes(json.dumps(_observations(plan)).encode()),
    )
    confirmation = confirmation_plan(plan, selection)
    assert len(plan.trials) + len(confirmation.trials) == 144
    assert len(confirmation.trials) == 136


def test_missing_or_misbound_observation_fails_closed_before_selection() -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)
    payload = _observations(plan)
    payload["trials"] = payload["trials"][:-1]
    observations = load_calibration_observations_bytes(json.dumps(payload).encode())
    with pytest.raises(EvaluationError, match="fixed sweep"):
        select_calibration_level(plan, observations)

    payload = _observations(plan)
    payload["trials"][0]["study_plan_sha256"] = _digest("9")
    observations = load_calibration_observations_bytes(json.dumps(payload).encode())
    with pytest.raises(EvaluationError, match="identity or offered rate"):
        select_calibration_level(plan, observations)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["levels"].reverse(),
        lambda value: value.update(policy_order=list(reversed(POLICY_IDS))),
        lambda value: value["levels"].__setitem__(1, value["levels"][0].copy()),
        lambda value: value["workload"].update(model="different-model"),
        lambda value: value.update(max_session_output_bytes=1),
    ],
)
def test_malformed_protocol_is_sanitized_and_cannot_be_compiled(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    value = deepcopy(protocol_payload())
    mutation(value)
    with pytest.raises(EvaluationError) as error:
        load_calibration_protocol_bytes(json.dumps(value).encode())
    assert str(error.value) == "load calibration input violates its contract"


def test_observation_reader_rejects_duplicate_nonfinite_and_terminal_mismatch() -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)
    bad = _observations(plan)
    bad["trials"][0]["outcomes"]["SUCCESS"] -= 1
    with pytest.raises(EvaluationError):
        load_calibration_observations_bytes(json.dumps(bad).encode())
    duplicate = (
        b'{"schema_version":"inferdrome.evaluation-load-calibration-observations.v1",'
        b'"schema_version":"x"}'
    )
    with pytest.raises(EvaluationError):
        load_calibration_observations_bytes(duplicate)
    with pytest.raises(EvaluationError):
        load_calibration_observations_bytes(b'{"x":NaN}')


def test_no_admissible_level_cannot_emit_confirmation_trials() -> None:
    protocol = load_calibration_protocol_bytes(json.dumps(protocol_payload()).encode())
    plan = compile_calibration(protocol)
    observations = load_calibration_observations_bytes(
        json.dumps(_observations(plan, fail_high=True)).encode()
    )
    payload = observations.model_dump(mode="json")
    for row in payload["trials"]:
        if row["level_id"] == "load-40":
            row["slo_good_count"] -= 1
    no_level = load_calibration_observations_bytes(json.dumps(payload).encode())
    selection = select_calibration_level(plan, no_level)
    confirmation = confirmation_plan(plan, selection)
    assert selection.status == "NO_ADMISSIBLE_LEVEL"
    assert confirmation.status == "NO_CONFIRMATION_ALLOWED"
    assert not confirmation.trials


def test_cli_writes_only_protocol_derived_no_replace_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protocol_path = tmp_path / "protocol.json"
    plan_path = tmp_path / "plan.json"
    protocol_path.write_bytes(json.dumps(protocol_payload()).encode())

    assert (
        main(
            [
                "load-calibration-plan",
                "--protocol",
                str(protocol_path),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    plan = compile_calibration(
        load_calibration_protocol_bytes(protocol_path.read_bytes())
    )
    observations_path = tmp_path / "observations.json"
    observations_path.write_bytes(json.dumps(_observations(plan)).encode())
    selection_path = tmp_path / "selection.json"
    confirmation_path = tmp_path / "confirmation.json"

    assert (
        main(
            [
                "load-calibration-select",
                "--protocol",
                str(protocol_path),
                "--observations",
                str(observations_path),
                "--selection-output",
                str(selection_path),
                "--confirmation-output",
                str(confirmation_path),
            ]
        )
        == 0
    )
    assert json.loads(plan_path.read_bytes())["phase"] == "CALIBRATION"
    assert json.loads(selection_path.read_bytes())["selected_level_id"] == "load-80"
    assert json.loads(confirmation_path.read_bytes())["status"] == "READY"
    assert "evidence_eligible" in capsys.readouterr().out
