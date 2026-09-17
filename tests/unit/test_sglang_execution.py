"""Synthetic SGLang telemetry on native sessions; no serving runtime claims."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

import inferdrome.evaluation.sglang_execution as execution_module
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import (
    build_sglang_engine_binding,
    engine_binding_bytes,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.sglang_execution import run_sglang_trial
from inferdrome.evaluation.sglang_results import load_sglang_trial_bytes
from inferdrome.evaluation.study import report_study, run_study
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_faults import MS, Stream, finish
from tests.unit.test_evaluation_healthy import Probe
from tests.unit.test_evaluation_runner import ManualClock, advance, settle


@pytest.fixture(autouse=True)
def synthetic_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    original = execution_module.wrap_sglang_result

    def wrap(native, binding, plan, trial):
        changes = {
            "evidence_class": "SYNTHETIC_ONLY",
            "foreground": replace(native.foreground, evidence_class="SYNTHETIC_ONLY"),
        }
        if hasattr(native, "background"):
            changes["background"] = replace(
                native.background, evidence_class="SYNTHETIC_ONLY"
            )
        return original(replace(native, **changes), binding, plan, trial)

    monkeypatch.setattr(execution_module, "wrap_sglang_result", wrap)


class SGLangProbe(Probe):
    """Hand-authored gauge labels on the existing independent fake clock probe."""

    async def get(self, origin: str, path: str) -> ProbeResponse:
        response = await super().get(origin, path)
        if self.mode != "VALID" or path != "/metrics":
            return response
        count = self.scores[0 if origin.endswith("8001") else 1]
        labels = (
            'model_name="private-model",engine_type="unified",'
            'tp_rank="0",pp_rank="0",moe_ep_rank="0"'
        )
        return ProbeResponse(
            200,
            (
                f"sglang:num_running_reqs{{{labels}}} {count}\n"
                f"sglang:num_queue_reqs{{{labels}}} 0\n"
            ).encode(),
        )


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
@pytest.mark.parametrize("policy", POLICY_IDS)
def test_native_sessions_keep_typed_counts_and_every_offered_request(
    scenario: str, policy: str
) -> None:
    async def run() -> None:
        plan = compile_study(study(scenario))
        trial = next(trial for trial in plan.trials if trial.policy_id == policy)
        binding = build_sglang_engine_binding(plan, profiles())
        clock = ManualClock()
        foreground = Stream(clock)
        router, observer = SGLangProbe(clock, (1, 3)), SGLangProbe(clock, (90, 0))
        background = Stream(clock, block=True) if scenario == "STALE_LOAD" else None
        task = asyncio.create_task(
            run_sglang_trial(
                plan,
                trial,
                binding,
                foreground,
                router,
                observer,
                background_transport=background,
                clock=clock,
            )
        )
        await finish(clock)
        result = await task
        raw = result.to_dict()
        assert result.status == "COMPLETED"
        assert raw["schema_version"].endswith(".v2")
        assert raw["engine"] == "sglang"
        assert raw["telemetry_source_age"] == "UNAVAILABLE"
        assert raw["endpoint_identity"] == "UNVERIFIED"
        assert raw["evidence_eligible"] is False
        assert [row["request_index"] for row in raw["foreground"]["records"]] == list(
            range(6)
        )
        assert [row["outcome"] for row in raw["foreground"]["records"]] == [
            "REJECTED_ROUTE"
            if scenario == "STALE_LOAD"
            and policy == "evaluation_fail_closed_v1"
            and offer.scheduled_ns == 80 * MS
            else "SUCCESS"
            for offer in trial.config.foreground.offers
        ]
        assert [row["scheduled_ns"] for row in raw["foreground"]["records"]] == [
            offer.scheduled_ns for offer in trial.config.foreground.offers
        ]
        for row in raw["observations"]:
            assert "running" not in row and "waiting" not in row
            assert "reported_running_requests" in row
            assert "reported_queued_requests" in row
            if row["published_to_router"] is False:
                assert row["reported_running_requests"] is None
                assert row["reported_queued_requests"] is None
        for decision in raw["decisions"]:
            for index, endpoint in enumerate(decision["snapshot"]["endpoints"]):
                assert endpoint["load"]["reported_running_requests"] == (1, 3)[index]
        if scenario == "STALE_LOAD":
            assert all(
                row["outcome"] == "CANCELLED" for row in raw["background"]["records"]
            )
            assert any(
                row["published_to_router"] is False for row in raw["observations"]
            )
            assert background is not None and background.closed
        assert foreground.closed and router.closed and observer.closed
        assert router.close_calls == observer.close_calls == 1
        assert not clock.waiters
        assert asyncio.all_tasks() == {asyncio.current_task()}
        encoded = json.dumps(raw)
        for forbidden in (
            "private-model",
            "private foreground",
            "127.0.0.1",
            '"running"',
            '"waiting"',
        ):
            assert forbidden not in encoded

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode,status",
    [
        ("MISSING", "MISSING"),
        ("MALFORMED", "MISSING"),
        ("HTTP_ERROR", "HTTP_ERROR"),
        ("TIMEOUT", "TIMEOUT"),
    ],
)
def test_failed_sglang_telemetry_keeps_offers_and_closes(
    mode: str, status: str
) -> None:
    async def run() -> None:
        plan = compile_study(study())
        binding = build_sglang_engine_binding(plan, profiles())
        clock = ManualClock()
        foreground, router, observer = (
            Stream(clock),
            SGLangProbe(clock, (1, 3)),
            SGLangProbe(clock, (90, 0)),
        )
        observer.mode = mode
        task = asyncio.create_task(
            run_sglang_trial(
                plan, plan.trials[0], binding, foreground, router, observer, clock=clock
            )
        )
        await finish(clock)
        result = await task
        assert result.status == "WARMUP_FAILED"
        raw = result.to_dict()
        assert len(raw["foreground"]["records"]) == 6
        assert not foreground.calls
        assert any(
            row["channel"] == "INDEPENDENT_LOAD" and row["status"] == status
            for row in raw["observations"]
        )
        assert all(client.closed for client in (foreground, router, observer))
        assert not clock.waiters
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


def test_external_cancel_retains_populations_and_restores_publication() -> None:
    async def run() -> None:
        plan = compile_study(study("STALE_LOAD"))
        binding = build_sglang_engine_binding(plan, profiles())
        clock, stop = ManualClock(), asyncio.Event()
        foreground, background = Stream(clock, block=True), Stream(clock, block=True)
        router, observer = SGLangProbe(clock, (1, 3)), SGLangProbe(clock, (90, 0))
        task = asyncio.create_task(
            run_sglang_trial(
                plan,
                plan.trials[0],
                binding,
                foreground,
                router,
                observer,
                background_transport=background,
                clock=clock,
                stop=stop,
            )
        )
        await finish(clock, 70)
        stop.set()
        await settle()
        await advance(clock, 80 * MS)
        result = await task
        raw = result.to_dict()
        assert result.status == "CANCELLED"
        assert len(raw["foreground"]["records"]) == 6
        assert len(raw["background"]["records"]) == 2
        assert any(
            event["kind"] == "RESTORED_DURING_CLEANUP" for event in raw["events"]
        )
        assert all(
            client.closed for client in (foreground, background, router, observer)
        )
        assert not clock.waiters
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_bound_study_writes_binding_before_dispatch_and_reports_with_identity(
    tmp_path: Path, scenario: str
) -> None:
    async def run() -> None:
        plan = compile_study(study(scenario))
        binding = build_sglang_engine_binding(plan, profiles())
        output = tmp_path / "study"
        study_clock = ManualClock()
        completed_clients = []

        async def execute(trial: CompiledTrial, *, stop: asyncio.Event):
            assert (
                output / "engine-binding.json"
            ).read_bytes() == engine_binding_bytes(binding)
            assert (output / "engine-binding.json").stat().st_mode & 0o777 == 0o400
            assert all(client.closed for client in completed_clients)
            clock = ManualClock()
            foreground, router, observer = (
                Stream(clock),
                SGLangProbe(clock, (1, 3)),
                SGLangProbe(clock, (90, 0)),
            )
            background = (
                Stream(clock, block=True)
                if isinstance(trial.config, RoutingFaultConfig)
                else None
            )
            task = asyncio.create_task(
                run_sglang_trial(
                    plan,
                    trial,
                    binding,
                    foreground,
                    router,
                    observer,
                    background_transport=background,
                    clock=clock,
                    stop=stop,
                )
            )
            await finish(clock)
            result = await task
            completed_clients.extend([foreground, router, observer])
            if background is not None:
                completed_clients.append(background)
            study_clock.advance_to(
                study_clock.elapsed_ns + result.to_dict()["elapsed_ns"]
            )
            return result

        manifest = await run_study(
            plan.config,
            output,
            executor=execute,
            clock=study_clock,
            engine_binding=binding,
        )
        assert manifest.status == "COMPLETED"
        for trial, entry in zip(plan.trials, manifest.trials, strict=True):
            content = (output / entry.result_filename).read_bytes()
            validated = load_sglang_trial_bytes(content, plan, trial, binding)
            assert validated.result_sha256 == entry.result_sha256
            assert validated.binding == binding
        with pytest.raises(EvaluationError, match="explicit binding"):
            report_study(plan.config, output, tmp_path / "unbound-report")
        report = report_study(
            plan.config, output, tmp_path / "report", engine_binding=binding
        )
        assert report["schema_version"] == "inferdrome.evaluation-study-report.v2"
        assert report["engine_binding"] == binding.model_dump(mode="json")
        assert report["statistical_report"]["coverage"]["returned_trials"] == 4
        markdown = (tmp_path / "report" / "report.md").read_text()
        assert "SGLang" in markdown and binding.producer_version in markdown
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


def test_bound_study_rejects_default_executor_before_creating_output(
    tmp_path: Path,
) -> None:
    plan = compile_study(study())
    binding = build_sglang_engine_binding(plan, profiles())
    with pytest.raises(EvaluationError, match="explicit SGLang executor"):
        asyncio.run(run_study(plan.config, tmp_path / "study", engine_binding=binding))
    assert not (tmp_path / "study").exists()


@pytest.mark.parametrize("forgery", ["digest", "primitive_type"])
def test_stale_trial_rejected_before_client_ownership(forgery: str) -> None:
    async def run() -> None:
        plan = compile_study(study())
        binding = build_sglang_engine_binding(plan, profiles())
        clock = ManualClock()
        owned = Stream(clock), SGLangProbe(clock, (1, 3)), SGLangProbe(clock, (90, 0))
        trial = plan.trials[0]
        if forgery == "digest":
            forged = replace(trial, config_sha256="sha256:" + "0" * 64)
        else:
            foreground = trial.config.foreground.model_copy(
                update={"max_tokens": float(trial.config.foreground.max_tokens)}
            )
            forged = replace(
                trial, config=trial.config.model_copy(update={"foreground": foreground})
            )
        with pytest.raises(EvaluationError):
            await run_sglang_trial(plan, forged, binding, *owned, clock=clock)
        assert all(not client.calls and not client.closed for client in owned)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())
