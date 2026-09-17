"""SYNTHETIC_ONLY numerical replay; no SGLang runtime or GPU qualification."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import build_sglang_engine_binding
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import RoutingFaultResult, run_routing_fault
from inferdrome.evaluation.healthy import HealthyRoutingResult, run_routing_healthy
from inferdrome.evaluation.sglang_results import (
    SGLangTrialResult,
    load_sglang_trial_bytes,
    sglang_trial_bytes,
    wrap_sglang_result,
)
from inferdrome.evaluation.study import load_study_trial_bytes
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from inferdrome.evaluation.study_report import summarize_trial
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_faults import MS, clients, finish
from tests.unit.test_evaluation_runner import ManualClock, advance, settle


def _native(
    trial: CompiledTrial, mode: str = "COMPLETED"
) -> HealthyRoutingResult | RoutingFaultResult:
    async def run() -> HealthyRoutingResult | RoutingFaultResult:
        clock, stop = ManualClock(), asyncio.Event()
        owned = clients(clock)
        if mode == "WARMUP_FAILED":
            owned[2].mode = "HTTP_ERROR"
        if mode == "CANCELLED_BEFORE_START":
            stop.set()
        if isinstance(trial.config, RoutingFaultConfig):
            work = run_routing_fault(trial.config, *owned, clock=clock, stop=stop)
        else:
            work = run_routing_healthy(
                trial.config, owned[0], owned[2], owned[3], clock=clock, stop=stop
            )
        task = asyncio.create_task(work)
        if mode == "CANCELLED_DURING_FREEZE":
            await settle()
            for at in range(5, 66, 5):
                await advance(clock, at * MS)
            stop.set()
            await settle()
        else:
            await finish(clock)
        result = await task
        changes: dict[str, Any] = {
            "evidence_class": "SYNTHETIC_ONLY",
            "foreground": replace(result.foreground, evidence_class="SYNTHETIC_ONLY"),
        }
        if isinstance(result, RoutingFaultResult):
            changes["background"] = replace(
                result.background, evidence_class="SYNTHETIC_ONLY"
            )
        return replace(result, **changes)

    return asyncio.run(run())


def _encoded(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


@pytest.fixture(scope="module")
def fixture() -> tuple[Any, Any, Any, SGLangTrialResult, bytes]:
    plan = compile_study(study("STALE_LOAD"))
    trial = plan.trials[2]
    binding = build_sglang_engine_binding(plan, profiles())
    result = wrap_sglang_result(_native(trial), binding, plan, trial)
    return (
        plan,
        trial,
        binding,
        result,
        sglang_trial_bytes(plan, trial, result, binding),
    )


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
@pytest.mark.parametrize(
    "mode",
    ["COMPLETED", "WARMUP_FAILED", "CANCELLED_BEFORE_START", "CANCELLED_DURING_FREEZE"],
)
def test_bound_roundtrip_retains_all_offers_outcomes_and_private_statistics(
    scenario: str, mode: str
) -> None:
    plan = compile_study(study(scenario))
    binding = build_sglang_engine_binding(plan, profiles())
    for trial in plan.trials:
        native = _native(trial, mode)
        result = wrap_sglang_result(native, binding, plan, trial)
        content = sglang_trial_bytes(plan, trial, result, binding)
        checked = load_sglang_trial_bytes(content, plan, trial, binding)
        assert checked.binding == binding
        assert checked.result_sha256 == sha256_digest(content)
        assert checked.status == ("CANCELLED" if mode.startswith("CANCELLED") else mode)
        assert checked.evidence_class == "SYNTHETIC_ONLY"
        assert checked.to_dict() == result.to_dict()
        assert sglang_trial_bytes(plan, trial, checked, binding) == content
        assert (
            checked._statistical_input.foreground.records == native.foreground.records
        )
        summary = summarize_trial(trial, checked._statistical_input)
        assert summary["result_sha256"] == sha256_digest(content)
        assert summary["foreground"]["offered_count"] == len(
            trial.config.foreground.offers
        )
        assert checked.to_dict()["telemetry_source_age"] == "UNAVAILABLE"
        assert checked.to_dict()["telemetry_freshness"] == "ACQUISITION_START_AGE_ONLY"
        with pytest.raises(EvaluationError):
            load_study_trial_bytes(content, plan, trial)


def test_only_explicit_sglang_counts_are_published(fixture: tuple) -> None:
    _, _, _, result, content = fixture
    raw = result.to_dict()
    assert raw["schema_version"] == "inferdrome.evaluation-routing-result.v2"
    assert raw["engine"] == "sglang"
    assert raw["telemetry_semantics"] == "SGLANG_0_5_18_SCHEDULER_GAUGES"
    for row in raw["observations"]:
        assert "reported_running_requests" in row
        assert "reported_queued_requests" in row
    for decision in raw["decisions"]:
        for endpoint in decision["snapshot"]["endpoints"]:
            for key in ("load", "last_load_attempt"):
                if endpoint[key] is not None:
                    assert "reported_running_requests" in endpoint[key]
                    assert "reported_queued_requests" in endpoint[key]
    for forbidden in (
        b'"running":',
        b'"waiting":',
        b"vllm",
        b"private-model",
        b"private foreground",
        b"private background",
        b"127.0.0.1",
        b"/opt/inferdrome",
    ):
        assert forbidden not in content
    raw["status"] = "UNTRUSTED"
    assert result.to_dict()["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), "inferdrome.evaluation-study-trial-result.v1"),
        (("plan_sha256",), "sha256:" + "0" * 64),
        (("config_sha256",), "sha256:" + "0" * 64),
        (("trial_id",), "trial-0000"),
        (("block_id",), "wrong-block"),
        (("workload_sha256",), "sha256:" + "0" * 64),
        (("engine_binding_sha256",), "sha256:" + "0" * 64),
        (("engine_binding", "engine"), "vllm"),
        (("engine_binding", "source_commit"), "0" * 40),
        (("result", "schema_version"), "inferdrome.evaluation-routing-result.v1"),
        (("result", "engine"), "vllm"),
        (("result", "engine_binding_sha256"), "sha256:" + "0" * 64),
        (("result", "telemetry_semantics"), "VLLM_RUNNING_WAITING"),
        (("result", "telemetry_freshness"), "SOURCE_STATE_AGE"),
        (("result", "telemetry_source_age"), "VERIFIED"),
        (("result", "evidence_eligible"), True),
        (("result", "observations", 0, "sequence"), 99),
        (("result", "observations", 0, "reported_running_requests"), 1),
        (("result", "observations", 2, "reported_running_requests"), True),
        (("result", "decisions", 0, "selected_endpoint_id"), "endpoint-b"),
        (
            (
                "result",
                "decisions",
                0,
                "snapshot",
                "endpoints",
                0,
                "load",
                "reported_running_requests",
            ),
            999,
        ),
        (("result", "foreground", "offered_count"), 0),
        (("result", "foreground", "records", 0, "scheduled_ns"), 0),
        (("result", "foreground", "records", 0, "request_index"), 99),
        (("result", "foreground", "records", 0, "usage_provenance"), "EXACT_TOKENS"),
    ],
)
def test_rejects_binding_semantics_population_and_replay_tampering(
    fixture: tuple, path: tuple[Any, ...], value: Any
) -> None:
    plan, trial, binding, _, content = fixture
    raw = json.loads(content)
    parent = raw
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(EvaluationError):
        load_sglang_trial_bytes(_encoded(raw), plan, trial, binding)


@pytest.mark.parametrize("target", ["observations", "load", "last_load_attempt"])
@pytest.mark.parametrize(
    "mutation", ["extra-v1", "replace-v1", "missing", "extra-field"]
)
def test_load_count_contract_is_closed(
    fixture: tuple, target: str, mutation: str
) -> None:
    plan, trial, binding, _, content = fixture
    raw = json.loads(content)
    if target == "observations":
        row = raw["result"]["observations"][0]
    else:
        row = raw["result"]["decisions"][0]["snapshot"]["endpoints"][0][target]
    if mutation == "extra-v1":
        row["running"] = row["reported_running_requests"]
    elif mutation == "replace-v1":
        row["running"] = row.pop("reported_running_requests")
    elif mutation == "missing":
        del row["reported_running_requests"]
    else:
        row["untrusted_field"] = "private-error"
    with pytest.raises(EvaluationError):
        load_sglang_trial_bytes(_encoded(raw), plan, trial, binding)


@pytest.mark.parametrize(
    "mutation", ["trailing", "duplicate", "noncanonical", "oversized", "missing"]
)
def test_envelope_encoding_is_canonical_and_bounded(
    fixture: tuple, mutation: str
) -> None:
    plan, trial, binding, _, content = fixture
    if mutation == "trailing":
        content += b" "
    elif mutation == "duplicate":
        content = b'{"trial_id":"trial-0002",' + content[1:]
    elif mutation == "noncanonical":
        content = json.dumps(json.loads(content), indent=2).encode()
    elif mutation == "oversized":
        content = b" " * (plan.config.limits.per_trial_result_bytes + 1)
    else:
        raw = json.loads(content)
        del raw["engine_binding"]
        content = _encoded(raw)
    with pytest.raises(EvaluationError):
        load_sglang_trial_bytes(content, plan, trial, binding)


def test_wrong_context_is_rejected_before_policy_replay(
    fixture: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, trial, binding, _, content = fixture

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay occurred before binding validation")

    monkeypatch.setattr(
        "inferdrome.evaluation.sglang_results.validate_trial_result", forbidden
    )
    for bad_trial in (
        replace(trial, trial_id="trial-0099"),
        replace(trial, config_sha256="sha256:" + "0" * 64),
        replace(trial, config=plan.trials[0].config),
    ):
        with pytest.raises(EvaluationError):
            load_sglang_trial_bytes(content, plan, bad_trial, binding)
    raw = json.loads(content)
    raw["result"]["telemetry_semantics"] = "VLLM_RUNNING_WAITING"
    with pytest.raises(EvaluationError):
        load_sglang_trial_bytes(_encoded(raw), plan, trial, binding)


def test_constructed_wrapper_is_revalidated_before_writing(fixture: tuple) -> None:
    plan, trial, binding, result, _ = fixture
    raw = result.to_dict()
    raw["engine"] = "vllm"
    forged = replace(result, _raw_bytes=_encoded(raw))
    with pytest.raises(EvaluationError):
        sglang_trial_bytes(plan, trial, forged, binding)
