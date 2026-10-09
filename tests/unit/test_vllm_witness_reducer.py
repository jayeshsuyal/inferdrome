"""Exploratory witness reduction rechecks fresh synthetic evidence for each mask."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from test_vllm_bounded_search import _fixture as _search_fixture
from test_vllm_bounded_search import _observation as _search_observation
from test_vllm_bounded_search import _write_inputs as _write_search_inputs

from inferdrome import vllm_bounded_search as bounded
from inferdrome import vllm_router_study as study
from inferdrome import vllm_witness_reducer as reducer
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import validate_timing
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA as PAIRED_INPUT_SCHEMA
from inferdrome.vllm_reduced_protocol import reduced_timings
from inferdrome.vllm_search_plan import make_search_plan


def _rehash(value: dict[str, Any]) -> None:
    value["result_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != "result_sha256"}
            )
        ).hexdigest()
    )


@lru_cache
def _source_template(
    variable_groups: bool = False, later_hit: bool = False, small_universe: bool = False
) -> dict[str, Any]:
    search_plan, plans, kwargs = _search_fixture(blocks=8)
    if variable_groups:
        plans = [
            study.make_plan(
                phase="fixture",
                seed=100 + index,
                count=14,
                expected_prompt_tokens=200,
                duration_ns=3_000_000_000,
                max_tokens=4,
            )
            for index in range(8)
        ]
        search_plan = make_search_plan(plans, **(kwargs | {"group_sizes": [3]}))
    if small_universe:
        search_plan = make_search_plan(plans, **(kwargs | {"group_sizes": [4]}))
    observations = (
        [
            _search_observation(search_plan, plans, 0),
            _search_observation(search_plan, plans, 1, reversal=True),
        ]
        if later_hit
        else [_search_observation(search_plan, plans, 0, reversal=True)]
    )
    report = bounded.evaluate(search_plan, plans, observations)
    assert report["status"] == "CANDIDATE_FOUND"
    return {
        "search_plan": search_plan,
        "plans": plans,
        "observations": observations,
        "report": report,
        "previous_report": None,
    }


def _source(
    variable_groups: bool = False,
    *,
    later_hit: bool = False,
    small_universe: bool = False,
) -> dict[str, Any]:
    return copy.deepcopy(_source_template(variable_groups, later_hit, small_universe))


def _plan(source: dict[str, Any], **changes: int) -> dict[str, Any]:
    return reducer.make_reduction_plan(
        source, **{"max_comparisons": 128, "max_trial_slots": 16384, **changes}
    )


def _observation(
    source: dict[str, Any], action: dict[str, Any], *, reversal: bool = True
) -> dict[str, Any]:
    protocol = action["protocol"]
    timings = reduced_timings(source["plans"], protocol)
    originals = {
        item["trial_id"]: item for item in source["observations"][-1]["inputs"]
    }
    ordinal = int(action["proposal_id"][1:])
    inputs = []
    for trial in protocol["trials"]:
        item = copy.deepcopy(originals[trial["trial_id"]])
        plan = source["plans"][trial["block"] - 1]
        timing = timings[trial["block"] - 1][trial["condition"]]
        item["timing"] = timing
        measurement = item["result"]["measurement"]
        measurement.update(
            plan_sha256=timing["timing_sha256"],
            trace_sha256=timing["transformed_trace_sha256"],
            started_unix_ns=str(
                int(measurement["started_unix_ns"]) + ordinal * 1_000_000_000_000
            ),
        )
        favored = (trial["condition"] == "baseline") == (
            trial["policy"] == protocol["policy_a"]
        )
        good_count = (12 if favored else 6) if reversal else 9
        for offer, row, link, receipt in zip(
            validate_timing(plan, timing),
            measurement["rows"],
            item["result"]["request_links"],
            item["ledger_rows"],
            strict=True,
        ):
            scheduled = offer.scheduled_ns
            first = scheduled + (
                5_000 if offer.index < good_count else plan["first_content_slo_ns"] + 1
            )
            row.update(
                scheduled_ns=scheduled,
                ready_ns=scheduled + 1000,
                dispatch_ns=scheduled + 2000,
                response_headers_ns=scheduled + 3000,
                first_body_byte_ns=scheduled + 4000,
                first_content_ns=first,
                terminal_ns=first + 1000,
            )
            number = (100 + ordinal) * 1_000_000 + trial["sequence"] * 100 + offer.index
            request_id = f"{number:032x}"
            link.update(request_id=request_id, response_request_id=request_id)
            receipt["request_id"] = request_id
        measurement["summary"] = study.summarize(
            plan,
            [study.RequestResult(**row) for row in measurement["rows"]],
            include_client_timing=True,
        )
        _rehash(measurement)
        _rehash(item["result"])
        inputs.append(item)
    return {"proposal_id": action["proposal_id"], "inputs": inputs}


def test_initial_reducer_proposes_a_complement_with_a_fresh_full_protocol() -> None:
    source = _source()
    plan = _plan(source)
    report = reducer.reduce(plan, source, [])
    assert report["status"] == "AWAITING_EVIDENCE"
    assert report["completed_comparisons"] == 0
    assert report["incumbent"]["source"] == "SEARCH"
    assert len(report["incumbent"]["retained_groups"]) == 6
    action = report["next_action"]
    assert action["type"] == "COLLECT_REDUCTION"
    assert action["proposal_id"] == "r001"
    assert action["granularity"] == 2
    assert len(action["retained_groups"]) == len(action["removed_groups"]) == 3
    assert (
        sorted(action["retained_groups"] + action["removed_groups"])
        == action["parent_retained_groups"]
    )
    assert len(action["protocol"]["trials"]) == 32
    assert report["budget"]["trial_slots_reserved"] == 0
    assert report["evidence_eligible"] is False


def test_accepted_masks_restart_reduction_without_dropping_requests() -> None:
    source = _source()
    plan = _plan(source)
    observations = []
    sizes = []
    report = reducer.reduce(plan, source, observations)
    while report["next_action"] is not None:
        action = report["next_action"]
        sizes.append(len(action["retained_groups"]))
        assert len(sizes) <= 3
        observation = _observation(source, action)
        assert all(
            len(item["result"]["measurement"]["rows"]) == 12
            for item in observation["inputs"]
        )
        observations.append(observation)
        report = reducer.reduce(plan, source, observations)
    assert sizes == [3, 2, 1]
    assert report["status"] == "REDUCTION_COMPLETE"
    assert report["incumbent"]["source"] == "REDUCTION"
    assert report["incumbent"]["proposal_id"] == "r003"
    assert len(report["incumbent"]["retained_groups"]) == 1
    assert all(attempt["accepted"] is True for attempt in report["attempts"])
    assert report["budget"]["trial_slots_reserved"] == 96
    assert report["evidence_eligible"] is False
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in json.dumps(report)
    with pytest.raises(ValueError):
        reducer.reduce(
            plan, source, [*observations, {"proposal_id": "r004", "inputs": []}]
        )


def test_a_failed_coarse_mask_does_not_prune_a_later_finer_partition() -> None:
    source = _source()
    plan = _plan(source, max_comparisons=3)
    observations = []
    for _ in range(2):
        report = reducer.reduce(plan, source, observations)
        assert report["next_action"]["granularity"] == 2
        observations.append(_observation(source, report["next_action"], reversal=False))
    report = reducer.reduce(plan, source, observations)
    assert report["next_action"]["granularity"] == 4
    assert len(report["next_action"]["retained_groups"]) == 5
    observations.append(_observation(source, report["next_action"]))
    final = reducer.reduce(plan, source, observations)
    assert final["status"] == "BUDGET_EXHAUSTED"
    assert [attempt["accepted"] for attempt in final["attempts"]] == [
        False,
        False,
        True,
    ]
    assert len(final["incumbent"]["retained_groups"]) == 5


def test_structurally_invalid_masks_are_skipped_without_reserving_a_comparison() -> (
    None
):
    source = _source(variable_groups=True)
    plan = _plan(source)
    observations = []
    report = reducer.reduce(plan, source, [])
    while report["next_action"] is not None:
        assert len(observations) < 4
        observations.append(_observation(source, report["next_action"]))
        report = reducer.reduce(plan, source, observations)
    assert report["status"] == "REDUCTION_COMPLETE"
    assert report["structural_skips"]
    assert report["completed_comparisons"] == len(observations) == 3
    assert report["budget"]["trial_slots_reserved"] == 96


def test_previously_tested_masks_are_cached_without_replaying_raw_evidence() -> None:
    source = _source()
    plan = _plan(source)
    observations = []
    report = reducer.reduce(plan, source, [])
    while report["next_action"] is not None:
        assert len(observations) < 12
        observations.append(
            {"proposal_id": report["next_action"]["proposal_id"], "inputs": []}
        )
        report = reducer.reduce(plan, source, observations)
    masks = [tuple(attempt["retained_groups"]) for attempt in report["attempts"]]
    assert len(masks) == len(set(masks))
    assert report["cached_skips"] > 0
    assert report["status"] == "REDUCTION_COMPLETE"
    assert report["incumbent"]["source"] == "SEARCH"
    assert all(attempt["accepted"] is False for attempt in report["attempts"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_comparisons", 0),
        ("max_comparisons", 129),
        ("max_comparisons", True),
        ("max_trial_slots", 0),
        ("max_trial_slots", 16385),
        ("max_trial_slots", 32.0),
    ],
)
def test_reduction_budget_requires_bounded_exact_integers(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        _plan(_source(), **{field: value})


def test_insufficient_budget_does_not_start_an_incomplete_comparison() -> None:
    source = _source()
    report = reducer.reduce(_plan(source, max_trial_slots=31), source, [])
    assert report["status"] == "BUDGET_EXHAUSTED"
    assert report["next_action"] is None
    assert report["budget"]["trial_slots_reserved"] == 0


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "source_groups", "root_parameters", "source_hash", "float_slots"],
)
def test_rehashed_reduction_plan_cannot_change_its_verified_source(
    mutation: str,
) -> None:
    source = _source()
    plan = _plan(source)
    if mutation == "unknown":
        plan["globally_minimal"] = True
    elif mutation == "source_groups":
        plan["source_groups"].reverse()
    elif mutation == "root_parameters":
        plan["root_parameters"] = dict(plan["root_parameters"], group_size=3)
    elif mutation == "source_hash":
        plan["source_report_sha256"] = "sha256:" + "0" * 64
    else:
        plan["trial_slots_per_comparison"] = float(plan["trial_slots_per_comparison"])
    plan["reduction_plan_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in plan.items()
                    if key != "reduction_plan_sha256"
                }
            )
        ).hexdigest()
    )
    with pytest.raises(ValueError):
        reducer.reduce(plan, source, [])


@pytest.mark.parametrize("reuse", ["result", "request_id", "clock"])
def test_unsuccessful_earlier_source_candidates_remain_in_reuse_and_clock_checks(
    reuse: str,
) -> None:
    source = _source(later_hit=True)
    assert source["report"]["selected_candidate"]["candidate_id"] == "c002"
    plan = _plan(source, max_comparisons=1)
    observation = _observation(source, reducer.reduce(plan, source, [])["next_action"])
    earlier = source["observations"][0]["inputs"]
    if reuse == "result":
        original = next(item for item in earlier if "-baseline-" in item["trial_id"])
        index = next(
            index
            for index, item in enumerate(observation["inputs"])
            if item["trial_id"] == original["trial_id"]
        )
        observation["inputs"][index] = copy.deepcopy(original)
    elif reuse == "request_id":
        request_id = earlier[0]["result"]["request_links"][0]["request_id"]
        item = observation["inputs"][0]
        item["result"]["request_links"][0].update(
            request_id=request_id, response_request_id=request_id
        )
        item["ledger_rows"][0]["request_id"] = request_id
        _rehash(item["result"])
    else:
        clocks = {
            item["trial_id"]: item["result"]["measurement"]["started_unix_ns"]
            for item in earlier
        }
        for item in observation["inputs"]:
            item["result"]["measurement"]["started_unix_ns"] = clocks[item["trial_id"]]
            _rehash(item["result"]["measurement"])
            _rehash(item["result"])
        report = reducer.reduce(plan, source, [observation])
        assert report["attempts"][0]["comparison_status"] == "REVERSAL_CANDIDATE"
        assert report["attempts"][0]["attempt_status"] == "INELIGIBLE"
        assert report["attempts"][0]["accepted"] is False
        return
    with pytest.raises(ValueError, match="reused"):
        reducer.reduce(plan, source, [observation])


@pytest.mark.parametrize(
    "mutation", ["unknown", "not_found", "report_tamper", "raw_receipt", "source_plan"]
)
def test_reduction_plan_rechecks_the_entire_source_search(mutation: str) -> None:
    source = _source()
    if mutation == "unknown":
        source["verified"] = True
    elif mutation == "not_found":
        source["observations"] = []
        source["report"] = bounded.evaluate(source["search_plan"], source["plans"], [])
    elif mutation == "report_tamper":
        source["report"]["evidence_eligible"] = True
        _rehash(source["report"])
    elif mutation == "raw_receipt":
        source["observations"][0]["inputs"][0]["ledger_rows"][0]["private_debug"] = (
            "edited"
        )
    else:
        source["plans"][0] = copy.deepcopy(source["plans"][1])
    with pytest.raises(ValueError):
        _plan(source)


@pytest.mark.parametrize(
    "mutation",
    ["proposal_order", "unknown", "source_result", "source_id", "prior_report"],
)
def test_proposals_require_fresh_raw_evidence_for_the_exact_expected_mask(
    mutation: str,
) -> None:
    source = _source()
    plan = _plan(source)
    action = reducer.reduce(plan, source, [])["next_action"]
    observation = _observation(source, action)
    if mutation == "proposal_order":
        observation["proposal_id"] = "r002"
    elif mutation == "unknown":
        observation["accepted"] = True
    elif mutation == "prior_report":
        observation["inputs"] = {"status": "REVERSAL_CANDIDATE"}
    elif mutation == "source_result":
        original = next(
            item
            for item in source["observations"][0]["inputs"]
            if "-baseline-" in item["trial_id"]
        )
        index = next(
            index
            for index, item in enumerate(observation["inputs"])
            if item["trial_id"] == original["trial_id"]
        )
        observation["inputs"][index] = copy.deepcopy(original)
    else:
        request_id = source["observations"][0]["inputs"][0]["result"]["request_links"][
            0
        ]["request_id"]
        item = observation["inputs"][0]
        item["result"]["request_links"][0].update(
            request_id=request_id, response_request_id=request_id
        )
        item["ledger_rows"][0]["request_id"] = request_id
        _rehash(item["result"])
    with pytest.raises(ValueError):
        reducer.reduce(plan, source, [observation])


def test_clock_overlap_keeps_the_comparison_signal_but_rejects_the_attempt() -> None:
    source = _source()
    plan = _plan(source, max_comparisons=1)
    observation = _observation(source, reducer.reduce(plan, source, [])["next_action"])
    for item in observation["inputs"]:
        measurement = item["result"]["measurement"]
        measurement["started_unix_ns"] = str(
            int(measurement["started_unix_ns"]) - 1_000_000_000_000
        )
        _rehash(measurement)
        _rehash(item["result"])
    report = reducer.reduce(plan, source, [observation])
    assert report["status"] == "BUDGET_EXHAUSTED"
    assert report["attempts"][0]["comparison_status"] == "REVERSAL_CANDIDATE"
    assert report["attempts"][0]["attempt_status"] == "INELIGIBLE"
    assert report["attempts"][0]["accepted"] is False
    assert report["incumbent"]["source"] == "SEARCH"


def test_resume_appends_snapshots_and_cannot_repair_a_closed_partial_attempt() -> None:
    source = _source()
    plan = _plan(source)
    initial = reducer.reduce(plan, source, [])
    complete = _observation(source, initial["next_action"], reversal=False)
    incomplete = copy.deepcopy(complete)
    incomplete["inputs"].pop()
    first = reducer.reduce(plan, source, [incomplete], previous_report=initial)
    second_observation = _observation(source, first["next_action"], reversal=False)
    second = reducer.reduce(
        plan, source, [incomplete, second_observation], previous_report=first
    )
    assert first["previous_report_sha256"] == initial["result_sha256"]
    assert second["previous_report_sha256"] == first["result_sha256"]
    assert first["budget"]["trial_slots_reserved"] == 32
    with pytest.raises(ValueError):
        reducer.reduce(
            plan, source, [complete, second_observation], previous_report=first
        )
    with pytest.raises(ValueError):
        reducer.reduce(plan, source, [incomplete], previous_report=first)
    tampered = copy.deepcopy(first)
    tampered["budget"]["trial_slots_reserved"] = 0
    _rehash(tampered)
    with pytest.raises(ValueError):
        reducer.reduce(
            plan, source, [incomplete, second_observation], previous_report=tampered
        )


def _write_source(tmp_path: Path, source: dict[str, Any]) -> Path:
    search_plan = source["search_plan"]
    kwargs = {
        "comparison_options": search_plan["comparison_options"],
        **search_plan["axes"],
        "max_candidates": search_plan["max_candidates"],
        "max_trial_slots": search_plan["max_trial_slots"],
    }
    search_path, inputs_path, _ = _write_search_inputs(
        tmp_path, search_plan, source["plans"], source["observations"], kwargs
    )
    report_path = tmp_path / "source-report.json"
    report_path.write_text(json.dumps(source["report"]))
    manifest = {
        "schema": "inferdrome.vllm-router-reduction-source.v1",
        "search_plan": search_path.name,
        "inputs": inputs_path.name,
        "report": report_path.name,
        "previous_report": None,
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(manifest))
    return path


def _write_inputs(
    tmp_path: Path,
    plan: dict[str, Any],
    source: dict[str, Any],
    observations: list[dict[str, Any]],
) -> Path:
    manifest = {
        "schema": "inferdrome.vllm-router-reduction-inputs.v1",
        "reduction_plan_sha256": plan["reduction_plan_sha256"],
        "proposals": [],
    }
    for index, observation in enumerate(observations):
        action = reducer.reduce(plan, source, observations[:index])["next_action"]
        paired = {
            "schema": PAIRED_INPUT_SCHEMA,
            "protocol_sha256": action["protocol"]["protocol_sha256"],
            "plans": [
                f"plan-{ordinal}.json" for ordinal in range(len(source["plans"]))
            ],
            "trials": [],
        }
        for ordinal, item in enumerate(observation["inputs"]):
            paths = {
                name: f"reduction-{index}-trial-{ordinal}-{name}.json"
                for name in ("timing", "result", "ledger")
            }
            for name in ("timing", "result"):
                (tmp_path / paths[name]).write_text(json.dumps(item[name]))
            (tmp_path / paths["ledger"]).write_text(
                "".join(json.dumps(row) + "\n" for row in item["ledger_rows"])
            )
            paired["trials"].append(
                {
                    "trial_id": item["trial_id"],
                    **paths,
                    "token_certificate": None,
                    "execution": item["execution"],
                }
            )
        path = tmp_path / f"proposal-{index}-inputs.json"
        path.write_text(json.dumps(paired))
        manifest["proposals"].append(
            {"proposal_id": observation["proposal_id"], "inputs": path.name}
        )
    path = tmp_path / "reduction-inputs.json"
    path.write_text(json.dumps(manifest))
    return path


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_witness_reducer", *args])
    reducer.main()


def test_cli_prepare_report_verify_and_exclusive_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    source_path = _write_source(tmp_path, source)
    plan_path = tmp_path / "reduction-plan.json"
    prepare_args = (
        "prepare",
        "--source",
        str(source_path),
        "--max-comparisons",
        "1",
        "--max-trial-slots",
        "32",
        "--output",
        str(plan_path),
    )
    _cli(monkeypatch, *prepare_args)
    plan = json.loads(plan_path.read_text())
    assert plan == _plan(source, max_comparisons=1, max_trial_slots=32)
    action = reducer.reduce(plan, source, [])["next_action"]
    observations = [_observation(source, action, reversal=False)]
    inputs_path = _write_inputs(tmp_path, plan, source, observations)
    output = tmp_path / "report.json"
    report_args = (
        "report",
        "--plan",
        str(plan_path),
        "--source",
        str(source_path),
        "--inputs",
        str(inputs_path),
        "--output",
        str(output),
    )
    verify_args = (
        "verify",
        "--plan",
        str(plan_path),
        "--source",
        str(source_path),
        "--inputs",
        str(inputs_path),
        "--report",
        str(output),
    )
    _cli(monkeypatch, *report_args)
    assert json.loads(output.read_text()) == reducer.reduce(plan, source, observations)
    _cli(monkeypatch, *verify_args)
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in output.read_text()
    assert str(tmp_path) not in output.read_text()
    for arguments, path in ((prepare_args, plan_path), (report_args, output)):
        before = path.read_bytes()
        with pytest.raises(FileExistsError):
            _cli(monkeypatch, *arguments)
        assert path.read_bytes() == before
    report = json.loads(output.read_text())
    report["attempts"][0]["accepted"] = True
    _rehash(report)
    output.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        _cli(monkeypatch, *verify_args)


def test_cli_explicit_null_previous_snapshot_is_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    plan = _plan(source)
    source_path = _write_source(tmp_path, source)
    plan_path = tmp_path / "reduction-plan.json"
    plan_path.write_text(json.dumps(plan))
    inputs_path = _write_inputs(tmp_path, plan, source, [])
    previous_path = tmp_path / "previous.json"
    previous_path.write_text("null")
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "report",
            "--plan",
            str(plan_path),
            "--source",
            str(source_path),
            "--inputs",
            str(inputs_path),
            "--previous-report",
            str(previous_path),
            "--output",
            str(output),
        )
    assert not output.exists()


def test_cli_replays_adaptive_accepted_masks_and_rejects_extra_terminal_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(small_universe=True)
    plan = _plan(source)
    initial = reducer.reduce(plan, source, [])
    assert len(initial["incumbent"]["retained_groups"]) == 3
    first = _observation(source, initial["next_action"])
    interim = reducer.reduce(plan, source, [first])
    assert len(interim["next_action"]["retained_groups"]) == 1
    second = _observation(source, interim["next_action"])
    observations = [first, second]
    expected = reducer.reduce(plan, source, observations)
    assert expected["status"] == "REDUCTION_COMPLETE"
    source_path = _write_source(tmp_path, source)
    plan_path = tmp_path / "reduction-plan.json"
    plan_path.write_text(json.dumps(plan))
    inputs_path = _write_inputs(tmp_path, plan, source, observations)
    output = tmp_path / "report.json"
    args = (
        "report",
        "--plan",
        str(plan_path),
        "--source",
        str(source_path),
        "--inputs",
        str(inputs_path),
        "--output",
        str(output),
    )
    _cli(monkeypatch, *args)
    assert json.loads(output.read_text()) == expected
    manifest = json.loads(inputs_path.read_text())
    manifest["proposals"].append(
        {"proposal_id": "r003", "inputs": "must-not-be-read.json"}
    )
    inputs_path.write_text(json.dumps(manifest))
    invalid_output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(monkeypatch, *args[:-1], str(invalid_output))
    assert not invalid_output.exists()


@pytest.mark.parametrize(
    "target",
    [
        "source.json",
        "source-report.json",
        "reduction-inputs.json",
        "proposal-0-inputs.json",
        "reduction-0-trial-0-result.json",
        "reduction-0-trial-0-ledger.json",
    ],
)
@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_cli_rejects_ambiguous_raw_inputs_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, bad_json: str
) -> None:
    source = _source()
    plan = _plan(source, max_comparisons=1)
    source_path = _write_source(tmp_path, source)
    plan_path = tmp_path / "reduction-plan.json"
    plan_path.write_text(json.dumps(plan))
    observation = _observation(source, reducer.reduce(plan, source, [])["next_action"])
    inputs_path = _write_inputs(tmp_path, plan, source, [observation])
    (tmp_path / target).write_text(bad_json)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "report",
            "--plan",
            str(plan_path),
            "--source",
            str(source_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output),
        )
    assert not output.exists()
