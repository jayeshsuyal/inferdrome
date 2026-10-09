"""Held-out evidence must preserve the frozen rule and remain fresh to discovery."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from test_vllm_witness_reducer import _observation, _plan, _rehash, _source
from test_vllm_witness_reducer import _write_inputs as _write_reduction_inputs
from test_vllm_witness_reducer import _write_source as _write_search_source

from inferdrome import vllm_heldout_confirmation as confirmation
from inferdrome import vllm_router_study as study
from inferdrome import vllm_witness_reducer as reducer
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import validate_timing
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA
from inferdrome.vllm_reduced_protocol import reduced_timings


@lru_cache
def _source_template(kind: str = "budget") -> dict[str, Any]:
    search = _source(later_hit=kind == "failed_search")
    plan = _plan(
        search,
        max_comparisons=128 if kind == "complete" else 2,
        max_trial_slots=31 if kind == "search_incumbent" else 16_384,
    )
    observations = []
    report = reducer.reduce(plan, search, observations)
    while report["next_action"] is not None:
        observations.append(
            _observation(
                search,
                report["next_action"],
                reversal=not (kind == "failed_reduction" and observations),
            )
        )
        report = reducer.reduce(plan, search, observations)
    return {
        "search_source": search,
        "reduction_plan": plan,
        "observations": observations,
        "report": report,
        "previous_report": None,
    }


def _confirmation_source(kind: str = "budget") -> dict[str, Any]:
    return copy.deepcopy(_source_template(kind))


def _heldout_plans(count: int = 8, **changes: Any) -> list[dict[str, Any]]:
    return [
        study.make_plan(
            **{
                "phase": "fixture",
                "seed": 800 + index,
                "count": 12,
                "expected_prompt_tokens": 200,
                "duration_ns": 3_000_000_000,
                "max_tokens": 4,
                **changes,
            }
        )
        for index in range(count)
    ]


def _confirmation_plan(
    source: dict[str, Any], plans: list[dict[str, Any]]
) -> dict[str, Any]:
    return confirmation.make_confirmation_plan(source, plans, order_seed=73)


def _resign(value: dict[str, Any], field: str) -> None:
    value[field] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != field}
            )
        ).hexdigest()
    )


def _inputs(
    source: dict[str, Any],
    plan: dict[str, Any],
    plans: list[dict[str, Any]],
    *,
    signal: str = "reversal",
) -> list[dict[str, Any]]:
    protocol = plan["protocol"]
    timings = reduced_timings(plans, protocol)
    original = source["search_source"]["observations"][-1]["inputs"][0]
    inputs = []
    for trial in protocol["trials"]:
        base = plans[trial["block"] - 1]
        timing = timings[trial["block"] - 1][trial["condition"]]
        item = copy.deepcopy(original)
        item["trial_id"] = trial["trial_id"]
        item["timing"] = timing
        measurement = item["result"]["measurement"]
        measurement.update(
            policy=trial["policy"],
            plan_sha256=timing["timing_sha256"],
            trace_sha256=timing["transformed_trace_sha256"],
            base_plan_sha256=base["plan_sha256"],
            base_trace_sha256=base["trace_sha256"],
            started_unix_ns=str(
                1_800_000_000_000_000_000 + trial["sequence"] * 13_000_000_000
            ),
        )
        measurement["router_stats_after"]["policy"] = trial["policy"]
        favored = (trial["condition"] == "baseline") == (
            trial["policy"] == protocol["policy_a"]
        )
        if signal == "reverse_direction":
            favored = not favored
        good_count = 9 if signal == "weak" else 12 if favored else 6
        for offer, row, link, receipt in zip(
            validate_timing(base, timing),
            measurement["rows"],
            item["result"]["request_links"],
            item["ledger_rows"],
            strict=True,
        ):
            scheduled = offer.scheduled_ns
            first = scheduled + (
                5_000 if offer.index < good_count else base["first_content_slo_ns"] + 1
            )
            row.update(
                index=offer.index,
                epoch=offer.epoch,
                traffic_class=offer.traffic_class,
                tenant=offer.tenant,
                document_id=offer.document_id,
                scheduled_ns=scheduled,
                ready_ns=scheduled + 1000,
                dispatch_ns=scheduled + 2000,
                response_headers_ns=scheduled + 3000,
                first_body_byte_ns=scheduled + 4000,
                first_content_ns=first,
                terminal_ns=first + 1000,
            )
            number = 900_000_000 + trial["sequence"] * 100 + offer.index
            request_id = f"{number:032x}"
            link.update(request_id=request_id, response_request_id=request_id)
            receipt.update(request_id=request_id, policy=trial["policy"])
        measurement["summary"] = study.summarize(
            base,
            [study.RequestResult(**row) for row in measurement["rows"]],
            include_client_timing=True,
        )
        _rehash(measurement)
        _rehash(item["result"])
        inputs.append(item)
    return inputs


@pytest.mark.parametrize("kind", ["budget", "complete", "search_incumbent"])
def test_plan_freezes_terminal_incumbent_and_inherits_comparison_contract(
    kind: str,
) -> None:
    source = _confirmation_source(kind)
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    confirmation.validate_confirmation_plan(plan, source, plans)
    assert plan["source_incumbent"] == source["report"]["incumbent"]
    assert (
        plan["protocol"]["retained_groups"]
        == source["report"]["incumbent"]["retained_groups"]
    )
    assert (
        plan["protocol"]["candidate_parameters"]
        == source["reduction_plan"]["root_parameters"]
    )
    for field, value in source["reduction_plan"]["comparison_options"].items():
        assert plan["protocol"][field] == (73 if field == "order_seed" else value)
    assert plan["comparison_count"] == 1
    assert plan["planned_blocks"] == 8
    assert plan["planned_trials"] == 32
    assert set(plan["held_out_seeds"]).isdisjoint(plan["discovery_seeds"])
    assert set(plan["held_out_trace_sha256s"]).isdisjoint(
        plan["discovery_trace_sha256s"]
    )


@pytest.mark.parametrize("count", [0, 4, 7, 9, 36])
def test_confirmation_requires_a_complete_sufficient_fixed_batch(count: int) -> None:
    with pytest.raises(ValueError):
        _confirmation_plan(_confirmation_source(), _heldout_plans(count))


@pytest.mark.parametrize(
    "mutation", ["discovery_seed", "duplicate_seed", "workload", "bad_hash"]
)
def test_heldout_plans_must_be_fresh_valid_and_match_the_discovery_workload(
    mutation: str,
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    if mutation == "discovery_seed":
        plans[0] = copy.deepcopy(source["search_source"]["plans"][0])
    elif mutation == "duplicate_seed":
        plans[0] = copy.deepcopy(plans[1])
    elif mutation == "workload":
        plans = _heldout_plans(max_tokens=5)
    else:
        plans[0]["trace_sha256"] = source["search_source"]["plans"][0]["trace_sha256"]
        _resign(plans[0], "plan_sha256")
    with pytest.raises(ValueError):
        _confirmation_plan(source, plans)


@pytest.mark.parametrize(
    "mutation", ["mask", "margin", "policy", "revision", "source", "extra"]
)
def test_rehashed_confirmation_plan_cannot_relax_or_change_the_frozen_candidate(
    mutation: str,
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    if mutation == "mask":
        plan["protocol"]["retained_groups"] = source["reduction_plan"]["source_groups"]
    elif mutation == "margin":
        plan["protocol"]["minimum_effect_microrps"] = 0
    elif mutation == "policy":
        plan["protocol"]["policy_a"], plan["protocol"]["policy_b"] = (
            plan["protocol"]["policy_b"],
            plan["protocol"]["policy_a"],
        )
    elif mutation == "revision":
        plan["protocol"]["source_revision"] = "d" * 40
    elif mutation == "source":
        plan["source_reduction_report_sha256"] = "sha256:" + "0" * 64
    else:
        plan["confirmed"] = True
    _resign(plan["protocol"], "protocol_sha256")
    _resign(plan, "confirmation_plan_sha256")
    with pytest.raises(ValueError):
        confirmation.validate_confirmation_plan(plan, source, plans)


@pytest.mark.parametrize(
    "mutation", ["nonterminal", "report", "raw_receipt", "unknown", "prior_snapshot"]
)
def test_terminal_source_is_regenerated_from_its_complete_raw_history(
    mutation: str,
) -> None:
    source = _confirmation_source()
    if mutation == "nonterminal":
        source["observations"] = []
        source["report"] = reducer.reduce(
            source["reduction_plan"], source["search_source"], []
        )
    elif mutation == "report":
        source["report"]["incumbent"]["retained_groups"] = source["reduction_plan"][
            "source_groups"
        ]
        _rehash(source["report"])
    elif mutation == "raw_receipt":
        source["observations"][0]["inputs"][0]["ledger_rows"][0]["private_debug"] = (
            "edited"
        )
    elif mutation == "unknown":
        source["verified"] = True
    else:
        source["previous_report"] = {}
    with pytest.raises(ValueError):
        _confirmation_plan(source, _heldout_plans())


@pytest.mark.parametrize(
    ("signal", "status"),
    [
        ("reversal", "HELD_OUT_CRITERIA_MET"),
        ("weak", "HELD_OUT_CRITERIA_NOT_MET"),
        ("reverse_direction", "HELD_OUT_CRITERIA_NOT_MET"),
    ],
)
def test_fresh_raw_batch_applies_only_the_original_two_directions(
    signal: str, status: str
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    report = confirmation.evaluate(
        plan, source, plans, _inputs(source, plan, plans, signal=signal)
    )
    assert report["status"] == status
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert report["independent_execution"] == "UNVERIFIED"
    assert report["preregistration"] == "UNVERIFIED"
    assert (
        report["comparison"]["planned_trials"]
        == report["comparison"]["supplied_trials"]
        == 32
    )
    assert all(trial["offered"] == 12 for trial in report["comparison"]["trials"])
    assert all(
        trial["goodput_denominator_ns"] == 3_000_000_000
        for trial in report["comparison"]["trials"]
    )
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in json.dumps(report)


def test_missing_trial_is_ineligible_and_never_shrinks_the_planned_population() -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    inputs = _inputs(source, plan, plans)
    missing = inputs.pop()["trial_id"]
    report = confirmation.evaluate(plan, source, plans, inputs)
    assert report["status"] == "INELIGIBLE"
    assert report["comparison"]["missing_trials"] == [missing]
    assert report["comparison"]["planned_trials"] == 32
    assert report["comparison"]["statistics"] is None


@pytest.mark.parametrize("kind", ["failed_search", "failed_reduction"])
@pytest.mark.parametrize("reuse", ["identity", "clock"])
def test_failed_discovery_attempts_still_participate_in_freshness_checks(
    kind: str, reuse: str
) -> None:
    source = _confirmation_source(kind)
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    inputs = _inputs(source, plan, plans)
    failed = (
        source["search_source"]["observations"][0]
        if kind == "failed_search"
        else source["observations"][-1]
    )
    if kind == "failed_reduction":
        assert source["report"]["attempts"][-1]["accepted"] is False
    if reuse == "identity":
        request_id = failed["inputs"][0]["result"]["request_links"][0]["request_id"]
        inputs[0]["result"]["request_links"][0].update(
            request_id=request_id, response_request_id=request_id
        )
        inputs[0]["ledger_rows"][0]["request_id"] = request_id
        _rehash(inputs[0]["result"])
        with pytest.raises(ValueError, match="reused"):
            confirmation.evaluate(plan, source, plans, inputs)
    else:
        origin = int(failed["inputs"][0]["result"]["measurement"]["started_unix_ns"])
        for ordinal, item in enumerate(inputs):
            item["result"]["measurement"]["started_unix_ns"] = str(
                origin + ordinal * 13_000_000_000
            )
            _rehash(item["result"]["measurement"])
            _rehash(item["result"])
        report = confirmation.evaluate(plan, source, plans, inputs)
        assert report["comparison"]["status"] == "REVERSAL_CANDIDATE"
        assert report["status"] == "INELIGIBLE"
        assert report["additional_ineligibility_reasons"] == [
            "SOURCE_AND_HELD_OUT_REPORTED_WINDOWS_OVERLAP"
        ]


def test_valid_previous_reduction_snapshot_is_replayed_before_confirmation() -> None:
    source = _confirmation_source()
    prior = reducer.reduce(
        source["reduction_plan"], source["search_source"], source["observations"][:1]
    )
    source["previous_report"] = prior
    source["report"] = reducer.reduce(
        source["reduction_plan"],
        source["search_source"],
        source["observations"],
        previous_report=prior,
    )
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    assert (
        confirmation.evaluate(plan, source, plans, _inputs(source, plan, plans))[
            "status"
        ]
        == "HELD_OUT_CRITERIA_MET"
    )


def _write_source(tmp_path: Path, source: dict[str, Any]) -> Path:
    search_path = _write_search_source(tmp_path, source["search_source"])
    reduction_path = tmp_path / "reduction-plan.json"
    reduction_path.write_text(json.dumps(source["reduction_plan"]))
    inputs_path = _write_reduction_inputs(
        tmp_path,
        source["reduction_plan"],
        source["search_source"],
        source["observations"],
    )
    report_path = tmp_path / "reduction-report.json"
    report_path.write_text(json.dumps(source["report"]))
    manifest = {
        "schema": confirmation.SOURCE_SCHEMA,
        "search_source": search_path.name,
        "reduction_plan": reduction_path.name,
        "inputs": inputs_path.name,
        "report": report_path.name,
        "previous_report": None,
    }
    source_path = tmp_path / "confirmation-source.json"
    source_path.write_text(json.dumps(manifest))
    return source_path


def _write_inputs(
    tmp_path: Path,
    plan: dict[str, Any],
    plans: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
) -> tuple[Path, list[Path]]:
    paths = [tmp_path / f"heldout-plan-{index}.json" for index in range(len(plans))]
    for path, base in zip(paths, plans, strict=True):
        path.write_text(json.dumps(base))
    manifest = {
        "schema": INPUT_SCHEMA,
        "protocol_sha256": plan["protocol"]["protocol_sha256"],
        "plans": [path.name for path in paths],
        "trials": [],
    }
    for ordinal, item in enumerate(inputs):
        files = {
            name: f"heldout-trial-{ordinal}-{name}.json"
            for name in ("timing", "result", "ledger")
        }
        for name in ("timing", "result"):
            (tmp_path / files[name]).write_text(json.dumps(item[name]))
        (tmp_path / files["ledger"]).write_text(
            "".join(json.dumps(row) + "\n" for row in item["ledger_rows"])
        )
        manifest["trials"].append(
            {
                "trial_id": item["trial_id"],
                **files,
                "token_certificate": None,
                "execution": item["execution"],
            }
        )
    path = tmp_path / "heldout-inputs.json"
    path.write_text(json.dumps(manifest))
    return path, paths


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_heldout_confirmation", *args])
    confirmation.main()


def test_cli_prepare_materialize_report_verify_and_exclusive_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    source_path = _write_source(tmp_path, source)
    inputs_path, paths = _write_inputs(
        tmp_path, plan, plans, _inputs(source, plan, plans)
    )
    plan_path = tmp_path / "confirmation-plan.json"
    output = tmp_path / "confirmation-report.json"
    prepare = (
        "prepare",
        "--source",
        str(source_path),
        "--plans",
        *map(str, paths),
        "--order-seed",
        "73",
        "--output",
        str(plan_path),
    )
    _cli(monkeypatch, *prepare)
    assert json.loads(plan_path.read_text()) == plan
    materialize = (
        "materialize",
        "--source",
        str(source_path),
        "--plan",
        str(plan_path),
        "--plans",
        *map(str, paths),
        "--output-dir",
        str(tmp_path / "timings"),
    )
    _cli(monkeypatch, *materialize)
    assert (
        json.loads((tmp_path / "timings" / "protocol.json").read_text())
        == plan["protocol"]
    )
    assert len(list((tmp_path / "timings").iterdir())) == 17
    report_args = (
        "report",
        "--source",
        str(source_path),
        "--plan",
        str(plan_path),
        "--inputs",
        str(inputs_path),
        "--output",
        str(output),
    )
    _cli(monkeypatch, *report_args)
    verify = (
        "verify",
        "--source",
        str(source_path),
        "--plan",
        str(plan_path),
        "--inputs",
        str(inputs_path),
        "--report",
        str(output),
    )
    _cli(monkeypatch, *verify)
    assert json.loads(output.read_text())["status"] == "HELD_OUT_CRITERIA_MET"
    assert str(tmp_path) not in output.read_text()
    assert "PRIVATE_SEARCH_LEDGER_SENTINEL" not in output.read_text()
    for arguments, path in (
        (prepare, plan_path),
        (report_args, output),
        (materialize, tmp_path / "timings" / "protocol.json"),
    ):
        before = path.read_bytes()
        with pytest.raises(FileExistsError):
            _cli(monkeypatch, *arguments)
        assert path.read_bytes() == before
    report = json.loads(output.read_text())
    report["evidence_eligible"] = True
    _rehash(report)
    output.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        _cli(monkeypatch, *verify)


def test_cli_missing_raw_trial_publishes_ineligible_report_with_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    source_path = _write_source(tmp_path, source)
    inputs_path, _ = _write_inputs(
        tmp_path, plan, plans, _inputs(source, plan, plans)[:-1]
    )
    plan_path = tmp_path / "confirmation-plan.json"
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / "confirmation-report.json"
    with pytest.raises(SystemExit) as stopped:
        _cli(
            monkeypatch,
            "report",
            "--source",
            str(source_path),
            "--plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output),
        )
    assert stopped.value.code == 2
    assert json.loads(output.read_text())["status"] == "INELIGIBLE"
    with pytest.raises(SystemExit) as verification:
        _cli(
            monkeypatch,
            "verify",
            "--source",
            str(source_path),
            "--plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--report",
            str(output),
        )
    assert verification.value.code == 2


@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_cli_rejects_ambiguous_raw_evidence_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_json: str
) -> None:
    source = _confirmation_source()
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    source_path = _write_source(tmp_path, source)
    inputs_path, _ = _write_inputs(tmp_path, plan, plans, _inputs(source, plan, plans))
    plan_path = tmp_path / "confirmation-plan.json"
    plan_path.write_text(json.dumps(plan))
    (tmp_path / "heldout-trial-0-result.json").write_text(bad_json)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "report",
            "--source",
            str(source_path),
            "--plan",
            str(plan_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output),
        )
    assert not output.exists()
