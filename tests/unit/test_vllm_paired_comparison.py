"""Synthetic raw artifacts exercise exploratory comparisons without a serving run."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_paired_comparison as comparison
from inferdrome import vllm_paired_protocol as paired
from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    TIMED_RESULT_SCHEMA,
    make_timing,
    validate_timing,
)
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


def _kwargs() -> dict[str, Any]:
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
    }


def _fixture(
    *, blocks: int = 8, signal: str = "reversal", measured: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    plans = [
        study.make_plan(
            phase="evaluation" if measured else "fixture",
            seed=100 + block,
            count=12,
            expected_prompt_tokens=200,
            duration_ns=300_000_000_000 if measured else 3_000_000_000,
            max_tokens=4,
        )
        for block in range(blocks)
    ]
    protocol = paired.make_protocol(plans, **_kwargs())
    inputs = []
    for trial in protocol["trials"]:
        plan = plans[trial["block"] - 1]
        descriptor = make_timing(
            plan,
            **(
                {"group_size": 1, "retained_spacing_bps": 10000, "max_advance_ns": 0}
                if trial["condition"] == "baseline"
                else protocol["candidate_parameters"]
            ),
        )
        assert descriptor["timing_sha256"] == trial["timing_sha256"]
        favored = (trial["condition"] == "baseline") == (
            trial["policy"] == protocol["policy_a"]
        )
        good_count = 12 if favored else 6
        if signal == "tie":
            good_count = 9
        elif signal == "small":
            good_count = 9 if favored else 8
        elif (
            signal == "noisy"
            and trial["block"] == 8
            and trial["condition"] == "baseline"
        ):
            good_count = 6 if favored else 12
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
                    ready_ns=scheduled + 1_000,
                    dispatch_ns=scheduled + 2_000,
                    response_headers_ns=scheduled + 3_000,
                    first_body_byte_ns=scheduled + 4_000,
                    first_content_ns=first,
                    terminal_ns=first + 1_000,
                    max_content_gap_ns=0,
                    outcome="completed",
                    http_status=200,
                    prompt_tokens=200,
                    completion_tokens=4,
                    document_id=offer.document_id,
                )
            )
            request_id = f"{trial['sequence'] * 100 + offer.index:032x}"
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
                    "private_debug": "PRIVATE_LEDGER_SENTINEL",
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
                + trial["sequence"] * (plan["duration_ns"] + 10_000_000_000)
            ),
            "evidence_class": "LOCAL_MEASUREMENT_ONLY"
            if measured
            else "SYNTHETIC_ONLY",
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
                    key: protocol[key]
                    for key in (
                        "source_revision",
                        "environment_sha256",
                        "reset_procedure_sha256",
                    )
                }
                | {"reset_completed": True},
            }
        )
    return protocol, plans, inputs


def _resummarize(
    protocol: dict[str, Any], plans: list[dict[str, Any]], item: dict[str, Any]
) -> None:
    trial = next(
        trial for trial in protocol["trials"] if trial["trial_id"] == item["trial_id"]
    )
    measurement = item["result"]["measurement"]
    measurement["summary"] = study.summarize(
        plans[trial["block"] - 1],
        [study.RequestResult(**row) for row in measurement["rows"]],
        include_client_timing=True,
    )
    _rehash(measurement)
    _rehash(item["result"])


def test_consistent_reversal_uses_blocks_and_stays_exploratory() -> None:
    protocol, plans, inputs = _fixture()
    report = comparison.compare(protocol, plans, inputs)
    assert report["status"] == "REVERSAL_CANDIDATE"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert report["independent_execution"] == "UNVERIFIED"
    assert report["held_out_confirmation"] == "NOT_IMPLEMENTED"
    assert (
        report["execution_declarations_scope"] == "OPERATOR_SUPPLIED_NOT_AUTHENTICATED"
    )
    assert report["chronology_scope"] == "REPORTED_CLIENT_CLOCKS_NOT_TRUSTED_CHRONOLOGY"
    assert report["planned_trials"] == report["supplied_trials"] == 32
    assert len(report["blocks"]) == 8
    assert report["statistics"]["baseline"]["block_count"] == 8
    assert report["statistics"]["baseline"]["mean_rps"] == 2
    assert report["statistics"]["candidate"]["mean_rps"] == -2
    assert all(
        block["goodput_denominator_ns"] == 3_000_000_000 for block in report["blocks"]
    )
    assert report["missing_trials"] == report["ineligibility_reasons"] == []
    serialized = json.dumps(report)
    assert "PRIVATE_LEDGER_SENTINEL" not in serialized
    assert "http://127.0.0.1:8090" not in serialized
    assert "Record a00000" not in serialized
    assert "request_links" not in serialized


@pytest.mark.parametrize("signal", ["tie", "noisy", "small"])
def test_ties_noise_and_submargin_changes_are_inconclusive(signal: str) -> None:
    protocol, plans, inputs = _fixture(signal=signal)
    if signal == "small":
        kwargs = _kwargs() | {"minimum_effect_microrps": 500_000}
        protocol = paired.make_protocol(plans, **kwargs)
    report = comparison.compare(protocol, plans, inputs)
    assert report["status"] == "INCONCLUSIVE"
    assert report["statistics"] is not None
    assert report["ineligibility_reasons"] == []
    assert report["evidence_eligible"] is False


def test_four_blocks_do_not_become_more_replicates_because_they_contain_requests() -> (
    None
):
    protocol, plans, inputs = _fixture(blocks=4)
    report = comparison.compare(protocol, plans, inputs)
    assert report["status"] == "INCONCLUSIVE"
    assert report["statistics"]["baseline"]["block_count"] == 4


@pytest.mark.parametrize("removed", ["one", "all"])
def test_missing_trials_make_the_whole_design_ineligible(removed: str) -> None:
    protocol, plans, inputs = _fixture()
    supplied = inputs[:-1] if removed == "one" else []
    report = comparison.compare(protocol, plans, supplied)
    assert report["status"] == "INELIGIBLE"
    assert report["statistics"] is None
    assert len(report["missing_trials"]) == (1 if removed == "one" else 32)
    assert all(
        reason["reason"] == "MISSING_TRIAL"
        for reason in report["ineligibility_reasons"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unplanned",
        "order",
        "unknown",
        "prior_report",
        "policy",
        "model",
        "source",
        "environment",
        "reset_type",
        "reuse_id",
        "summary",
    ],
)
def test_malformed_or_forged_inputs_are_rejected(mutation: str) -> None:
    protocol, plans, inputs = _fixture()
    item = inputs[0]
    if mutation == "duplicate":
        inputs.insert(1, copy.deepcopy(item))
    elif mutation == "unplanned":
        item["trial_id"] = "unplanned-trial"
    elif mutation == "order":
        inputs[:2] = reversed(inputs[:2])
    elif mutation == "unknown":
        item["already_verified"] = True
    elif mutation == "prior_report":
        item["result"] = {
            "scope": "TIMING_AND_CLIENT_ACCOUNTING_ONLY",
            "status": "VERIFIED",
        }
    elif mutation == "source":
        item["execution"]["source_revision"] = "d" * 40
    elif mutation == "environment":
        item["execution"]["environment_sha256"] = "sha256:" + "d" * 64
    elif mutation == "reset_type":
        item["execution"]["reset_completed"] = 1
    elif mutation == "reuse_id":
        old_id = item["result"]["request_links"][0]["request_id"]
        second = inputs[1]
        second["result"]["request_links"][0].update(
            request_id=old_id, response_request_id=old_id
        )
        second["ledger_rows"][0]["request_id"] = old_id
        _rehash(second["result"])
    else:
        measurement = item["result"]["measurement"]
        if mutation == "policy":
            measurement["policy"] = "round_robin"
            measurement["router_stats_after"]["policy"] = "round_robin"
            for row in item["ledger_rows"]:
                row["policy"] = "round_robin"
        elif mutation == "model":
            measurement["model"] = "different-model"
        elif mutation == "summary":
            measurement["summary"]["all_offered"]["slo_good"] += 1
        _rehash(measurement)
        _rehash(item["result"])
    with pytest.raises(ValueError):
        comparison.compare(protocol, plans, inputs)


@pytest.mark.parametrize(
    "failure",
    [
        "reset",
        "overlap",
        "incomplete_links",
        "interrupted",
        "operational",
        "disagreement",
        "lag",
        "queue",
    ],
)
def test_failed_gate_keeps_all_blocks_visible_and_suppresses_statistics(
    failure: str,
) -> None:
    protocol, plans, inputs = _fixture()
    item = inputs[1]
    measurement = item["result"]["measurement"]
    if failure == "reset":
        item["execution"]["reset_completed"] = False
    elif failure == "overlap":
        measurement["started_unix_ns"] = inputs[0]["result"]["measurement"][
            "started_unix_ns"
        ]
    elif failure == "incomplete_links":
        item["ledger_rows"].pop()
    elif failure == "interrupted":
        measurement.update(status="INTERRUPTED", comparison_valid=False)
    elif failure == "operational":
        measurement["rows"][0]["outcome"] = "protocol_error"
        item["ledger_rows"][0]["outcome"] = "upstream_protocol_error"
    elif failure == "disagreement":
        item["ledger_rows"][0]["outcome"] = "timeout"
    else:
        stages = (
            "dispatch_ns",
            "response_headers_ns",
            "first_body_byte_ns",
            "first_content_ns",
            "terminal_ns",
        )
        if failure == "lag":
            stages = ("ready_ns", *stages)
        for row in measurement["rows"]:
            for field in stages:
                row[field] += 2_000_000
    _resummarize(protocol, plans, item)
    report = comparison.compare(protocol, plans, inputs)
    assert report["status"] == "INELIGIBLE"
    assert report["statistics"] is None
    assert len(report["blocks"]) == 8 and len(report["trials"]) == 32
    assert any(
        reason["trial_id"] == item["trial_id"]
        for reason in report["ineligibility_reasons"]
    )


@pytest.mark.parametrize(
    "router_outcome", ["rejected_capacity", "rejected_queue_timeout", "rejected_input"]
)
def test_only_load_related_rejections_are_eligible(router_outcome: str) -> None:
    protocol, plans, inputs = _fixture()
    item = inputs[0]
    row = item["result"]["measurement"]["rows"][0]
    row.update(
        outcome="rejected",
        http_status=503,
        first_body_byte_ns=None,
        first_content_ns=None,
        max_content_gap_ns=None,
        prompt_tokens=None,
        completion_tokens=None,
        terminal_ns=row["response_headers_ns"] + 1_000,
    )
    item["ledger_rows"][0].update(outcome=router_outcome, replica=None)
    _resummarize(protocol, plans, item)
    report = comparison.compare(protocol, plans, inputs)
    assert report["status"] == (
        "INELIGIBLE" if router_outcome == "rejected_input" else "REVERSAL_CANDIDATE"
    )


@pytest.mark.parametrize("start", [0, "0", "01", "-1", "1.0", "9" * 21, True])
def test_start_clock_requires_exact_positive_decimal_strings(start: Any) -> None:
    protocol, plans, inputs = _fixture()
    inputs[0]["result"]["measurement"]["started_unix_ns"] = start
    _rehash(inputs[0]["result"]["measurement"])
    _rehash(inputs[0]["result"])
    with pytest.raises(ValueError, match="start"):
        comparison.compare(protocol, plans, inputs)


@pytest.mark.parametrize("measured", [False, True])
def test_bad_or_missing_token_certificates_cannot_enter_comparison(
    measured: bool,
) -> None:
    protocol, plans, inputs = _fixture(measured=measured)
    if not measured:
        inputs[0]["token_certificate"] = {}
    with pytest.raises(ValueError, match="certificate"):
        comparison.compare(protocol, plans, inputs)


def _write_inputs(
    tmp_path: Path,
    protocol: dict[str, Any],
    plans: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    for index, plan in enumerate(plans):
        (tmp_path / f"plan-{index}.json").write_text(json.dumps(plan))
    manifest = {
        "schema": comparison.INPUT_SCHEMA,
        "protocol_sha256": protocol["protocol_sha256"],
        "plans": [f"plan-{index}.json" for index in range(len(plans))],
        "trials": [],
    }
    for index, item in enumerate(inputs):
        paths = {
            name: f"trial-{index}-{name}.json"
            for name in ("timing", "result", "ledger")
        }
        (tmp_path / paths["timing"]).write_text(json.dumps(item["timing"]))
        (tmp_path / paths["result"]).write_text(json.dumps(item["result"]))
        (tmp_path / paths["ledger"]).write_text(
            "".join(json.dumps(row) + "\n" for row in item["ledger_rows"])
        )
        manifest["trials"].append(
            {
                "trial_id": item["trial_id"],
                **paths,
                "token_certificate": None,
                "execution": item["execution"],
            }
        )
    protocol_path, manifest_path, config_path = (
        tmp_path / name for name in ("protocol.json", "inputs.json", "config.json")
    )
    protocol_path.write_text(json.dumps(protocol))
    manifest_path.write_text(json.dumps(manifest))
    config_path.write_text(json.dumps(_kwargs() | {"plans": manifest["plans"]}))
    return protocol_path, manifest_path, config_path


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["vllm_paired_comparison", *args])
    comparison.main()


def test_cli_prepare_compare_verify_and_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, plans, inputs = _fixture()
    protocol_path, input_path, config_path = _write_inputs(
        tmp_path, protocol, plans, inputs
    )
    prepared, report_path = tmp_path / "prepared.json", tmp_path / "report.json"
    prepare_args = ("prepare", "--config", str(config_path), "--output", str(prepared))
    compare_args = (
        "compare",
        "--protocol",
        str(protocol_path),
        "--inputs",
        str(input_path),
        "--output",
        str(report_path),
    )
    verify_args = (
        "verify",
        "--protocol",
        str(protocol_path),
        "--inputs",
        str(input_path),
        "--report",
        str(report_path),
    )
    _cli(monkeypatch, *prepare_args)
    assert json.loads(prepared.read_text()) == protocol
    _cli(monkeypatch, *compare_args)
    report = json.loads(report_path.read_text())
    assert report == comparison.compare(protocol, plans, inputs)
    _cli(monkeypatch, *verify_args)
    assert str(tmp_path) not in report_path.read_text()
    assert "PRIVATE_LEDGER_SENTINEL" not in report_path.read_text()
    for arguments, output in ((prepare_args, prepared), (compare_args, report_path)):
        before = output.read_bytes()
        with pytest.raises(FileExistsError):
            _cli(monkeypatch, *arguments)
        assert output.read_bytes() == before
    report["status"] = "INCONCLUSIVE"
    _rehash(report)
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="regenerated"):
        _cli(monkeypatch, *verify_args)


def test_cli_rechecks_raw_sources_and_retains_ineligible_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, plans, inputs = _fixture()
    protocol_path, input_path, _ = _write_inputs(tmp_path, protocol, plans, inputs)
    report_path = tmp_path / "report.json"
    compare_args = (
        "compare",
        "--protocol",
        str(protocol_path),
        "--inputs",
        str(input_path),
        "--output",
        str(report_path),
    )
    _cli(monkeypatch, *compare_args)
    ledger_path = tmp_path / "trial-0-ledger.json"
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    rows[0]["outcome"] = "timeout"
    ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="regenerated"):
        _cli(
            monkeypatch,
            "verify",
            "--protocol",
            str(protocol_path),
            "--inputs",
            str(input_path),
            "--report",
            str(report_path),
        )
    failed = tmp_path / "ineligible.json"
    with pytest.raises(SystemExit) as raised:
        _cli(monkeypatch, *compare_args[:-1], str(failed))
    assert raised.value.code == 2
    assert json.loads(failed.read_text())["status"] == "INELIGIBLE"


@pytest.mark.parametrize(
    "target",
    [
        "protocol.json",
        "inputs.json",
        "plan-0.json",
        "trial-0-timing.json",
        "trial-0-result.json",
        "trial-0-ledger.json",
    ],
)
@pytest.mark.parametrize("bad_json", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_cli_rejects_ambiguous_or_nonfinite_raw_input_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, bad_json: str
) -> None:
    protocol, plans, inputs = _fixture(blocks=4)
    protocol_path, input_path, _ = _write_inputs(tmp_path, protocol, plans, inputs)
    (tmp_path / target).write_text(bad_json)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        _cli(
            monkeypatch,
            "compare",
            "--protocol",
            str(protocol_path),
            "--inputs",
            str(input_path),
            "--output",
            str(output),
        )
    assert not output.exists()
