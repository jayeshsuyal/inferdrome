"""Bounded exploratory search consumes immutable raw synthetic candidate evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_bounded_search as bounded
from inferdrome import vllm_router_study as study
from inferdrome import vllm_search_plan as search
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    TIMED_RESULT_SCHEMA,
    make_timing,
    validate_timing,
)
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA as PAIRED_INPUT_SCHEMA
from inferdrome.vllm_request_identity import correlated_result


def _rehash(value: dict[str, Any]) -> None:
    value["result_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != "result_sha256"}
            )
        ).hexdigest()
    )


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


def _fixture(
    *, blocks: int = 4, max_candidates: int = 128, max_trial_slots: int = 16384
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    plans = [
        study.make_plan(
            phase="fixture",
            seed=100 + index,
            count=12,
            expected_prompt_tokens=200,
            duration_ns=3_000_000_000,
            max_tokens=4,
        )
        for index in range(blocks)
    ]
    kwargs = {
        "comparison_options": _options(),
        "group_sizes": [2],
        "retained_spacing_bps": [0],
        "max_advances_ns": [1_000_000, 100_000_000, 1_000_000_000],
        "max_candidates": max_candidates,
        "max_trial_slots": max_trial_slots,
    }
    return search.make_search_plan(plans, **kwargs), plans, kwargs


def _observation(
    search_plan: dict[str, Any],
    plans: list[dict[str, Any]],
    index: int,
    *,
    reversal: bool = False,
) -> dict[str, Any]:
    candidate = search_plan["candidates"][index]
    protocol = search.candidate_protocol(search_plan, plans, candidate["candidate_id"])
    inputs = []
    for trial in protocol["trials"]:
        plan = plans[trial["block"] - 1]
        descriptor = make_timing(
            plan,
            **(
                {"group_size": 1, "retained_spacing_bps": 10000, "max_advance_ns": 0}
                if trial["condition"] == "baseline"
                else candidate["parameters"]
            ),
        )
        favored = (trial["condition"] == "baseline") == (
            trial["policy"] == protocol["policy_a"]
        )
        good_count = (12 if favored else 6) if reversal else 9
        rows = []
        links = []
        ledger = []
        for offer in validate_timing(plan, descriptor):
            scheduled = offer.scheduled_ns
            first = scheduled + (
                5_000 if offer.index < good_count else plan["first_content_slo_ns"] + 1
            )
            rows.append(
                study.RequestResult(
                    index=offer.index,
                    scheduled_ns=scheduled,
                    epoch=offer.epoch,
                    traffic_class=offer.traffic_class,
                    tenant=offer.tenant,
                    ready_ns=scheduled + 1000,
                    dispatch_ns=scheduled + 2000,
                    response_headers_ns=scheduled + 3000,
                    first_body_byte_ns=scheduled + 4000,
                    first_content_ns=first,
                    terminal_ns=first + 1000,
                    max_content_gap_ns=0,
                    outcome="completed",
                    http_status=200,
                    prompt_tokens=200,
                    completion_tokens=4,
                    document_id=offer.document_id,
                )
            )
            identity_number = (
                (index + 1) * 1_000_000 + trial["sequence"] * 100 + offer.index
            )
            request_id = f"{identity_number:032x}"
            links.append(
                {
                    "index": offer.index,
                    "request_id": request_id,
                    "response_request_id": request_id,
                }
            )
            ledger.append(
                {
                    "request_id": request_id,
                    "policy": trial["policy"],
                    "outcome": "completed",
                    "replica": offer.index % 2,
                    "private_debug": "PRIVATE_SEARCH_LEDGER_SENTINEL",
                }
            )
        measurement = {
            "schema": TIMED_RESULT_SCHEMA,
            "plan_sha256": descriptor["timing_sha256"],
            "trace_sha256": descriptor["transformed_trace_sha256"],
            "token_certificate_sha256": None,
            "policy": trial["policy"],
            "model": protocol["model"],
            "router_origin": "http://127.0.0.1:8090",
            "started_unix_ns": str(
                1_700_000_000_000_000_000
                + index * 1_000_000_000_000
                + trial["sequence"] * 13_000_000_000
            ),
            "evidence_class": "SYNTHETIC_ONLY",
            "router_accounting_valid": True,
            "status": "COMPLETED",
            "comparison_valid": True,
            "router_stats_after": {
                "policy": trial["policy"],
                "offered": len(rows),
                "terminal": len(rows),
                "ledger_rows": len(rows),
                "in_flight": 0,
                "pending": 0,
                "active": 0,
                "busy": [0, 0],
                "accounting_failed": False,
            },
            "rows": [asdict(row) for row in rows],
            "summary": study.summarize(plan, rows, include_client_timing=True),
            "base_plan_schema": plan["schema"],
            "base_plan_sha256": plan["plan_sha256"],
            "base_trace_sha256": plan["trace_sha256"],
            "token_certificate_scope": "BASE_WORKLOAD_UNCHANGED_TIMING_ONLY",
            "arrival_timing_scope": "PLANNED_OFFERS_NOT_OBSERVED_ARRIVALS",
        }
        _rehash(measurement)
        inputs.append(
            {
                "trial_id": trial["trial_id"],
                "timing": descriptor,
                "result": correlated_result(measurement, links),
                "ledger_rows": ledger,
                "token_certificate": None,
                "execution": {
                    field: protocol[field]
                    for field in (
                        "source_revision",
                        "environment_sha256",
                        "reset_procedure_sha256",
                    )
                }
                | {"reset_completed": True},
            }
        )
    return {"candidate_id": candidate["candidate_id"], "inputs": inputs}


def test_initial_state_returns_exact_first_collection_protocol() -> None:
    plan, sources, _ = _fixture()
    report = bounded.evaluate(plan, sources, [])
    assert report["status"] == "AWAITING_EVIDENCE"
    assert report["evaluated_candidates"] == 0
    assert report["candidate_results"] == []
    assert report["selected_candidate"] is None
    assert report["evidence_eligible"] is False
    assert report["previous_report_sha256"] is None
    assert report["next_action"] == {
        "type": "COLLECT_CANDIDATE",
        "candidate_id": "c001",
        "protocol": search.candidate_protocol(plan, sources, "c001"),
    }
    assert report["budget"]["trial_slots_reserved"] == 0


def test_first_screened_in_candidate_stops_an_ordered_multi_candidate_search() -> None:
    plan, sources, _ = _fixture(blocks=8)
    observations = [
        _observation(plan, sources, 0),
        _observation(plan, sources, 1, reversal=True),
    ]
    report = bounded.evaluate(plan, sources, observations)
    assert report["status"] == "CANDIDATE_FOUND"
    assert report["evaluated_candidates"] == 2
    assert report["selected_candidate"]["candidate_id"] == "c002"
    assert report["selected_candidate"]["distance"] == plan["candidates"][1]["distance"]
    assert report["next_action"] is None
    assert report["coverage"]["inconclusive_candidates"] == 1
    assert report["coverage"]["screened_in_candidates"] == 1
    assert report["budget"]["trial_slots_reserved"] == 64
    assert report["evidence_eligible"] is False
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert (
        report["selection_scope"]
        == "NEAREST_SCREENED_IN_AMONG_EVALUATED_SCHEDULES_UNDER_DECLARED_ORDER"
    )
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in json.dumps(report)
    with pytest.raises(ValueError):
        bounded.evaluate(plan, sources, [*observations, _observation(plan, sources, 2)])


def test_ineligible_earlier_candidate_remains_visible_after_a_later_hit() -> None:
    plan, sources, _ = _fixture(blocks=8)
    observations = [
        _observation(plan, sources, 0),
        _observation(plan, sources, 1, reversal=True),
    ]
    observations[0]["inputs"].pop()
    report = bounded.evaluate(plan, sources, observations)
    assert report["status"] == "CANDIDATE_FOUND"
    assert report["coverage"]["ineligible_candidates"] == 1
    assert report["coverage"]["earlier_unresolved_candidates"] == ["c001"]
    assert report["candidate_results"][0]["comparison_status"] == "INELIGIBLE"
    assert report["budget"]["trial_slots_reserved"] == 64


@pytest.mark.parametrize("budget", ["candidate", "trials", "cannot_afford_one"])
def test_budget_exhaustion_never_starts_a_partial_reservation(budget: str) -> None:
    plan, sources, _ = _fixture(
        max_candidates=1 if budget == "candidate" else 128,
        max_trial_slots=15
        if budget == "cannot_afford_one"
        else 16
        if budget == "trials"
        else 16384,
    )
    observations = (
        [] if budget == "cannot_afford_one" else [_observation(plan, sources, 0)]
    )
    report = bounded.evaluate(plan, sources, observations)
    assert report["status"] == "BUDGET_EXHAUSTED"
    assert report["next_action"] is None
    assert report["budget"]["trial_slots_reserved"] == len(observations) * 16
    if observations:
        unlimited, _, _ = _fixture()
        extra = _observation(unlimited, sources, 1)
        with pytest.raises(ValueError):
            bounded.evaluate(plan, sources, [*observations, extra])


def test_exhausting_the_finite_grid_does_not_claim_no_reversal_exists() -> None:
    plan, sources, _ = _fixture()
    observations = [
        _observation(plan, sources, index) for index in range(len(plan["candidates"]))
    ]
    report = bounded.evaluate(plan, sources, observations)
    assert report["status"] == "GRID_EXHAUSTED"
    assert report["next_action"] is None
    assert report["selected_candidate"] is None
    assert report["evidence_eligible"] is False
    assert report["coverage"]["inconclusive_candidates"] == len(plan["candidates"])


@pytest.mark.parametrize(
    "malformed",
    ["skip", "duplicate", "reorder", "unknown", "comparison_report", "wrong_inputs"],
)
def test_observations_must_be_raw_evidence_for_a_contiguous_prefix(
    malformed: str,
) -> None:
    plan, sources, _ = _fixture()
    observations = [_observation(plan, sources, 0), _observation(plan, sources, 1)]
    if malformed == "skip":
        observations = observations[1:]
    elif malformed == "duplicate":
        observations[1] = copy.deepcopy(observations[0])
    elif malformed == "reorder":
        observations.reverse()
    elif malformed == "unknown":
        observations[0]["already_verified"] = True
    elif malformed == "comparison_report":
        observations[0]["inputs"] = {"status": "REVERSAL_CANDIDATE"}
    else:
        observations[1]["inputs"] = copy.deepcopy(observations[0]["inputs"])
    with pytest.raises(ValueError):
        bounded.evaluate(plan, sources, observations)


def test_request_ids_cannot_be_reused_across_candidate_comparisons() -> None:
    plan, sources, _ = _fixture()
    observations = [_observation(plan, sources, 0), _observation(plan, sources, 1)]
    request_id = observations[0]["inputs"][0]["result"]["request_links"][0][
        "request_id"
    ]
    second = observations[1]["inputs"][0]
    second["result"]["request_links"][0].update(
        request_id=request_id, response_request_id=request_id
    )
    second["ledger_rows"][0]["request_id"] = request_id
    _rehash(second["result"])
    with pytest.raises(ValueError):
        bounded.evaluate(plan, sources, observations)


def test_cross_candidate_clock_overlap_does_not_rewrite_the_paired_report() -> None:
    plan, sources, _ = _fixture(blocks=8)
    observations = [
        _observation(plan, sources, 0),
        _observation(plan, sources, 1, reversal=True),
    ]
    for item in observations[1]["inputs"]:
        measurement = item["result"]["measurement"]
        measurement["started_unix_ns"] = str(
            int(measurement["started_unix_ns"]) - 1_000_000_000_000
        )
        _rehash(measurement)
        _rehash(item["result"])
    report = bounded.evaluate(plan, sources, observations)
    second = report["candidate_results"][1]
    assert (
        second["comparison_status"]
        == second["comparison"]["status"]
        == "REVERSAL_CANDIDATE"
    )
    assert second["search_status"] == "INELIGIBLE"
    assert (
        "CROSS_CANDIDATE_REPORTED_WINDOWS_OVERLAP"
        in second["search_ineligibility_reasons"]
    )
    assert report["status"] == "AWAITING_EVIDENCE"
    assert report["selected_candidate"] is None


def test_resume_rechecks_the_old_raw_prefix_and_extends_the_report_chain() -> None:
    plan, sources, _ = _fixture()
    observations = [_observation(plan, sources, 0), _observation(plan, sources, 1)]
    initial = bounded.evaluate(plan, sources, [])
    first = bounded.evaluate(plan, sources, observations[:1], previous_report=initial)
    second = bounded.evaluate(plan, sources, observations, previous_report=first)
    assert first["previous_report_sha256"] == initial["result_sha256"]
    assert second["previous_report_sha256"] == first["result_sha256"]
    assert second["evaluated_candidates"] == 2
    assert second["next_action"]["candidate_id"] == "c003"


@pytest.mark.parametrize("empty", [False, True])
def test_closed_incomplete_attempt_consumes_slots_and_cannot_be_repaired_on_resume(
    empty: bool,
) -> None:
    plan, sources, _ = _fixture()
    complete = _observation(plan, sources, 0)
    closed = copy.deepcopy(complete)
    closed["inputs"] = [] if empty else closed["inputs"][:-1]
    previous = bounded.evaluate(plan, sources, [closed])
    assert previous["candidate_results"][0]["search_status"] == "INELIGIBLE"
    assert previous["trial_artifacts_supplied"] == (0 if empty else 15)
    assert previous["budget"]["trial_slots_reserved"] == 16
    assert previous["next_action"]["candidate_id"] == "c002"
    with pytest.raises(ValueError, match="previous snapshot"):
        bounded.evaluate(
            plan,
            sources,
            [complete, _observation(plan, sources, 1)],
            previous_report=previous,
        )


def test_an_exact_trial_result_cannot_be_reused_across_candidates() -> None:
    plan, sources, _ = _fixture()
    observations = [_observation(plan, sources, 0), _observation(plan, sources, 1)]
    baseline = next(
        item for item in observations[0]["inputs"] if "-baseline-" in item["trial_id"]
    )
    index = next(
        index
        for index, item in enumerate(observations[1]["inputs"])
        if item["trial_id"] == baseline["trial_id"]
    )
    observations[1]["inputs"][index] = copy.deepcopy(baseline)
    with pytest.raises(ValueError, match="result reused"):
        bounded.evaluate(plan, sources, observations)


@pytest.mark.parametrize(
    "mutation",
    [
        "same_length",
        "removed",
        "old_receipt",
        "previous_counts",
        "previous_hash",
        "invalid_ancestry",
    ],
)
def test_resume_rejects_history_removal_editing_or_report_forgery(
    mutation: str,
) -> None:
    plan, sources, _ = _fixture()
    observations = [_observation(plan, sources, 0), _observation(plan, sources, 1)]
    previous = bounded.evaluate(plan, sources, observations[:1])
    if mutation == "same_length":
        observations = observations[:1]
    elif mutation == "removed":
        observations = []
    elif mutation == "old_receipt":
        observations[0]["inputs"][0]["ledger_rows"][0]["private_debug"] = (
            "changed source"
        )
    elif mutation == "previous_counts":
        previous["budget"]["trial_slots_reserved"] = 0
        _rehash(previous)
    elif mutation == "previous_hash":
        previous["result_sha256"] = "sha256:" + "0" * 64
    else:
        previous["previous_report_sha256"] = "not-a-hash"
        _rehash(previous)
    with pytest.raises(ValueError):
        bounded.evaluate(plan, sources, observations, previous_report=previous)


def _write_inputs(
    tmp_path: Path,
    plan: dict[str, Any],
    sources: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> tuple[Path, Path, Path]:
    plan_paths = [f"plan-{index}.json" for index in range(len(sources))]
    for name, source in zip(plan_paths, sources, strict=True):
        (tmp_path / name).write_text(json.dumps(source))
    manifest = {
        "schema": "inferdrome.vllm-router-search-inputs.v1",
        "search_plan_sha256": plan["search_plan_sha256"],
        "plans": plan_paths,
        "candidates": [],
    }
    for index, observation in enumerate(observations):
        protocol = search.candidate_protocol(plan, sources, observation["candidate_id"])
        paired_manifest = {
            "schema": PAIRED_INPUT_SCHEMA,
            "protocol_sha256": protocol["protocol_sha256"],
            "plans": plan_paths,
            "trials": [],
        }
        for ordinal, item in enumerate(observation["inputs"]):
            paths = {
                name: f"candidate-{index}-trial-{ordinal}-{name}.json"
                for name in ("timing", "result", "ledger")
            }
            for name in ("timing", "result"):
                (tmp_path / paths[name]).write_text(json.dumps(item[name]))
            (tmp_path / paths["ledger"]).write_text(
                "".join(json.dumps(row) + "\n" for row in item["ledger_rows"])
            )
            paired_manifest["trials"].append(
                {
                    "trial_id": item["trial_id"],
                    **paths,
                    "token_certificate": None,
                    "execution": item["execution"],
                }
            )
        name = f"candidate-{index}-inputs.json"
        (tmp_path / name).write_text(json.dumps(paired_manifest))
        manifest["candidates"].append(
            {"candidate_id": observation["candidate_id"], "inputs": name}
        )
    plan_path, inputs_path, config_path = (
        tmp_path / name for name in ("search-plan.json", "inputs.json", "config.json")
    )
    plan_path.write_text(json.dumps(plan))
    inputs_path.write_text(json.dumps(manifest))
    config_path.write_text(json.dumps(kwargs | {"plans": plan_paths}))
    return plan_path, inputs_path, config_path


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_bounded_search", *args])
    bounded.main()


def test_cli_prepares_candidate_and_report_with_reverification_and_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, kwargs = _fixture(max_candidates=1)
    observations = [_observation(plan, sources, 0)]
    plan_path, inputs_path, config_path = _write_inputs(
        tmp_path, plan, sources, observations, kwargs
    )
    prepared, candidate_path, report_path = (
        tmp_path / name for name in ("prepared.json", "candidate.json", "report.json")
    )
    prepare_args = ("prepare", "--config", str(config_path), "--output", str(prepared))
    candidate_args = (
        "candidate",
        "--search-plan",
        str(plan_path),
        "--plans",
        *(str(tmp_path / f"plan-{index}.json") for index in range(len(sources))),
        "--candidate-id",
        "c001",
        "--output",
        str(candidate_path),
    )
    report_args = (
        "report",
        "--search-plan",
        str(plan_path),
        "--inputs",
        str(inputs_path),
        "--output",
        str(report_path),
    )
    verify_args = (
        "verify",
        "--search-plan",
        str(plan_path),
        "--inputs",
        str(inputs_path),
        "--report",
        str(report_path),
    )
    for args in (prepare_args, candidate_args, report_args):
        _cli(monkeypatch, *args)
    assert json.loads(prepared.read_text()) == plan
    assert json.loads(candidate_path.read_text()) == search.candidate_protocol(
        plan, sources, "c001"
    )
    report = json.loads(report_path.read_text())
    assert report == bounded.evaluate(plan, sources, observations)
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in report_path.read_text()
    assert str(tmp_path) not in report_path.read_text()
    _cli(monkeypatch, *verify_args)
    for args, output in (
        (prepare_args, prepared),
        (candidate_args, candidate_path),
        (report_args, report_path),
    ):
        before = output.read_bytes()
        with pytest.raises(FileExistsError):
            _cli(monkeypatch, *args)
        assert output.read_bytes() == before
    report["status"] = "CANDIDATE_FOUND"
    _rehash(report)
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        _cli(monkeypatch, *verify_args)


def test_cli_awaiting_report_is_saved_with_exit_two_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, kwargs = _fixture()
    plan_path, inputs_path, _ = _write_inputs(tmp_path, plan, sources, [], kwargs)
    previous_path = tmp_path / "previous.json"
    with pytest.raises(SystemExit) as raised:
        _cli(
            monkeypatch,
            "report",
            "--search-plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(previous_path),
        )
    assert raised.value.code == 2
    assert json.loads(previous_path.read_text())["status"] == "AWAITING_EVIDENCE"
    observations = [_observation(plan, sources, 0)]
    _write_inputs(tmp_path, plan, sources, observations, kwargs)
    resumed = tmp_path / "resumed.json"
    with pytest.raises(SystemExit) as raised:
        _cli(
            monkeypatch,
            "report",
            "--search-plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--previous-report",
            str(previous_path),
            "--output",
            str(resumed),
        )
    assert raised.value.code == 2
    assert (
        json.loads(resumed.read_text())["previous_report_sha256"]
        == json.loads(previous_path.read_text())["result_sha256"]
    )


def test_cli_explicit_null_previous_snapshot_cannot_disable_resume_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, kwargs = _fixture()
    plan_path, inputs_path, _ = _write_inputs(tmp_path, plan, sources, [], kwargs)
    previous_path = tmp_path / "previous.json"
    previous_path.write_text("null")
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "report",
            "--search-plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--previous-report",
            str(previous_path),
            "--output",
            str(output),
        )
    assert not output.exists()


def test_cli_candidate_manifest_cannot_substitute_other_source_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, kwargs = _fixture(max_candidates=1)
    plan_path, inputs_path, _ = _write_inputs(
        tmp_path, plan, sources, [_observation(plan, sources, 0)], kwargs
    )
    paired_path = tmp_path / "candidate-0-inputs.json"
    manifest = json.loads(paired_path.read_text())
    manifest["plans"][0] = "plan-1.json"
    paired_path.write_text(json.dumps(manifest))
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError, match="plans differ"):
        _cli(
            monkeypatch,
            "report",
            "--search-plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output),
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "target",
    [
        "search-plan.json",
        "inputs.json",
        "plan-0.json",
        "candidate-0-inputs.json",
        "candidate-0-trial-0-result.json",
        "candidate-0-trial-0-ledger.json",
    ],
)
@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_cli_rejects_ambiguous_or_nonfinite_input_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, bad_json: str
) -> None:
    plan, sources, kwargs = _fixture(max_candidates=1)
    plan_path, inputs_path, _ = _write_inputs(
        tmp_path, plan, sources, [_observation(plan, sources, 0)], kwargs
    )
    (tmp_path / target).write_text(bad_json)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "report",
            "--search-plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output),
        )
    assert not output.exists()
