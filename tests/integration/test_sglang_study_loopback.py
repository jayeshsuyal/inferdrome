"""SYNTHETIC_ONLY two-server native study; no SGLang/GPU qualification.

Hand-authored OpenAI streams and pinned SGLang metric shapes run on two actual
localhost listeners. Cache preparation is a synthetic declaration here, not an
observed cache reset. Runtime and server artifact identity remain UNVERIFIED.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web

from inferdrome.evaluation import sglang_execution, study
from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    build_sglang_engine_binding,
    engine_binding_bytes,
    engine_binding_sha256,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import RoutingFaultResult
from inferdrome.evaluation.healthy import HealthyRoutingResult
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SglangServingConfig,
)
from inferdrome.evaluation.sglang_report import load_sglang_report_bytes
from inferdrome.evaluation.sglang_results import (
    SGLangTrialResult,
    load_sglang_trial_bytes,
    sglang_trial_bytes,
)
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from inferdrome.evaluation.study_files import trial_filename
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.integration.test_evaluation_routing_loopback import (
    _BACKGROUND,
    _FOREGROUND,
    _MODEL,
    _OUTPUT,
    _Replica,
)
from tests.integration.test_evaluation_study_loopback import _payload
from tests.unit.test_evaluation_study_config import load
from tests.unit.test_sglang_serving_profile import config as serving_config


class _SGLangReplica(_Replica):
    """Synthetic scheduler gauges, with the existing hand-authored SSE fixture."""

    async def metrics(self, request: web.Request) -> web.StreamResponse:
        labels = (
            f'model_name="{_MODEL}",engine_type="unified",'
            'tp_rank="0",pp_rank="0",moe_ep_rank="0"'
        )
        body = (
            f"sglang:num_running_reqs{{{labels}}} {self.active}\n"
            f"sglang:num_queue_reqs{{{labels}}} 0\n"
        ).encode()
        return web.Response(body=body, content_type="text/plain")


@asynccontextmanager
async def _replicas() -> AsyncIterator[
    tuple[tuple[str, str], tuple[_SGLangReplica, _SGLangReplica]]
]:
    runners: list[web.AppRunner] = []
    listeners: list[socket.socket] = []
    replicas: list[_SGLangReplica] = []
    origins: list[str] = []
    try:
        for _ in range(2):
            replica = _SGLangReplica()
            replicas.append(replica)
            app = web.Application(client_max_size=262_144)
            app.router.add_get("/health", replica.handle)
            app.router.add_get("/metrics", replica.handle)
            app.router.add_post("/v1/chat/completions", replica.handle)
            runner = web.AppRunner(
                app, access_log=None, shutdown_timeout=1, handler_cancellation=True
            )
            runners.append(runner)
            await runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", 0))
            origins.append(f"http://127.0.0.1:{listener.getsockname()[1]}")
            await web.SockSite(runner, listener).start()
        assert origins[0] != origins[1]
        yield (origins[0], origins[1]), (replicas[0], replicas[1])
    finally:
        for replica in replicas:
            replica.release_streams.set()
        for runner in runners:
            await asyncio.wait_for(runner.cleanup(), 2)
        for listener in listeners:
            listener.close()
        assert all(not replica.handlers and replica.active == 0 for replica in replicas)


def test_sglang_native_study_keeps_populations_bound_through_offline_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            value = _payload(origins)
            value["preparation"].update(
                cache_state="DECLARED_COLD",
                prefix_caching="DECLARED_ENABLED",
                serving_image_reference=SGLANG_IMAGE_REFERENCE.split("@", 1)[1],
            )
            config = load(value)
            plan = compile_study(config)
            profiles: dict[EndpointId, SglangServingConfig] = {
                "endpoint-a": serving_config(
                    served_model_name=_MODEL, origin=origins[0]
                ),
                "endpoint-b": serving_config(
                    served_model_name=_MODEL, origin=origins[1]
                ),
            }
            binding = build_sglang_engine_binding(plan, profiles)
            executor = sglang_execution.SGLangStudyExecutor(plan, binding, profiles)
            native_results: dict[str, HealthyRoutingResult | RoutingFaultResult] = {}
            original_wrap = sglang_execution.wrap_sglang_result

            def synthetic_result(
                native: HealthyRoutingResult | RoutingFaultResult,
                selected_binding,
                selected_plan,
                trial: CompiledTrial,
            ) -> SGLangTrialResult:
                # Test-only provenance classification, before the production
                # wrapper validates and serializes these native populations.
                if isinstance(native, RoutingFaultResult):
                    native = replace(
                        native,
                        evidence_class="SYNTHETIC_ONLY",
                        foreground=replace(
                            native.foreground, evidence_class="SYNTHETIC_ONLY"
                        ),
                        background=replace(
                            native.background, evidence_class="SYNTHETIC_ONLY"
                        ),
                    )
                else:
                    native = replace(
                        native,
                        evidence_class="SYNTHETIC_ONLY",
                        foreground=replace(
                            native.foreground, evidence_class="SYNTHETIC_ONLY"
                        ),
                    )
                native_results[trial.trial_id] = native
                return original_wrap(native, selected_binding, selected_plan, trial)

            monkeypatch.setattr(
                sglang_execution, "wrap_sglang_result", synthetic_result
            )
            output = tmp_path / "study"
            manifest = await asyncio.wait_for(
                study.run_study(
                    config, output, executor=executor, engine_binding=binding
                ),
                20,
            )
            assert manifest.status == "COMPLETED", manifest.model_dump()
            assert len(manifest.trials) == 8
            assert all(row.state == "RETURNED" for row in manifest.trials)
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
            assert all(replica.foreground_requests > 0 for replica in replicas)
            assert replicas[0].background_requests == 8
            assert replicas[1].background_requests == 0
            assert (
                output / "engine-binding.json"
            ).read_bytes() == engine_binding_bytes(binding)

            for index, trial in enumerate(plan.trials):
                content = (output / trial_filename(index)).read_bytes()
                envelope = json.loads(content)
                assert canonical_json_bytes(envelope) + b"\n" == content
                assert envelope["schema_version"] == (
                    "inferdrome.evaluation-study-trial-result.v2"
                )
                assert envelope["engine_binding_sha256"] == engine_binding_sha256(
                    binding
                )
                checked = load_sglang_trial_bytes(content, plan, trial, binding)
                assert sglang_trial_bytes(plan, trial, checked, binding) == content
                assert checked.result_sha256 == manifest.trials[index].result_sha256
                raw = checked.to_dict()
                native = native_results[trial.trial_id]
                assert raw["engine"] == "sglang"
                assert raw["telemetry_source_age"] == "UNAVAILABLE"
                assert raw["telemetry_semantics"] == "SGLANG_0_5_18_SCHEDULER_GAUGES"
                assert raw["status"] == "COMPLETED"
                assert raw["evidence_class"] == "SYNTHETIC_ONLY"
                assert all(
                    "reported_running_requests" in row
                    and "reported_queued_requests" in row
                    for row in raw["observations"]
                )
                assert b'"running":' not in content and b'"waiting":' not in content
                assert checked._statistical_input.foreground.records == (
                    native.foreground.records
                )
                assert canonical_json_bytes(raw["foreground"]) == canonical_json_bytes(
                    native.to_dict()["foreground"]
                )
                foreground = raw["foreground"]["records"]
                assert [row["request_index"] for row in foreground] == list(range(7))
                assert [row["scheduled_ns"] for row in foreground] == [
                    offer.scheduled_ns for offer in trial.config.foreground.offers
                ]
                rejected = [row for row in foreground if row["outcome"] != "SUCCESS"]
                if (
                    isinstance(trial.config, RoutingFaultConfig)
                    and trial.policy_id == "evaluation_fail_closed_v1"
                ):
                    assert rejected  # Frozen load must retain all rejected offers.
                    assert all(row["outcome"] == "REJECTED_ROUTE" for row in rejected)
                    assert all(
                        row["attempts"] == 0
                        and row["dispatch_ns"] is None
                        and row["usage_provenance"] == "UNAVAILABLE"
                        for row in rejected
                    )
                else:
                    assert not rejected
                assert all(
                    row["scheduled_ns"]
                    <= row["arrival_observed_ns"]
                    <= row["dispatch_ns"]
                    <= row["first_content_ns"]
                    <= row["terminal_ns"]
                    and row["usage_provenance"] == "SERVER_REPORTED_STREAM_USAGE"
                    and row["prompt_tokens"] == 3
                    and row["completion_tokens"] == 4
                    for row in foreground
                    if row["outcome"] == "SUCCESS"
                )
                if isinstance(trial.config, RoutingFaultConfig):
                    assert isinstance(native, RoutingFaultResult)
                    assert checked._statistical_input.background is not None
                    assert checked._statistical_input.background.records == (
                        native.background.records
                    )
                    assert canonical_json_bytes(raw["background"]) == (
                        canonical_json_bytes(native.to_dict()["background"])
                    )
                    background = raw["background"]["records"]
                    assert [row["request_index"] for row in background] == [0, 1]
                    assert [row["scheduled_ns"] for row in background] == [
                        offer.scheduled_ns for offer in trial.config.background.offers
                    ]
                    assert all(row["outcome"] == "CANCELLED" for row in background)
                    assert all(
                        row["usage_provenance"] == "UNAVAILABLE"
                        and row["prompt_tokens"] is None
                        and row["completion_tokens"] is None
                        for row in background
                    )
                    assert any(
                        row["channel"] == "INDEPENDENT_LOAD"
                        and row["reported_running_requests"] == 2
                        for row in raw["observations"]
                    )
                    assert any(
                        row["channel"] == "ROUTER_LOAD"
                        and row["published_to_router"] is False
                        and row["reported_running_requests"] is None
                        and row["reported_queued_requests"] is None
                        for row in raw["observations"]
                    )

            requests_before_report = sum(len(replica.requests) for replica in replicas)

            def forbid_clients(*args, **kwargs):
                raise AssertionError("offline SGLang report constructed a client")

            monkeypatch.setattr(sglang_execution, "AiohttpTransport", forbid_clients)
            monkeypatch.setattr(
                sglang_execution, "AiohttpProbeTransport", forbid_clients
            )
            monkeypatch.setattr(study, "AiohttpTransport", forbid_clients)
            monkeypatch.setattr(study, "AiohttpProbeTransport", forbid_clients)
            with pytest.raises(EvaluationError, match="explicit binding"):
                study.report_study(config, output, tmp_path / "unbound-report")
            assert not (tmp_path / "unbound-report").exists()
            report = study.report_study(
                config, output, tmp_path / "report", engine_binding=binding
            )
            report_bytes = (tmp_path / "report" / "report.json").read_bytes()
            assert load_sglang_report_bytes(report_bytes, plan, binding) == report
            assert report["schema_version"] == "inferdrome.evaluation-study-report.v2"
            assert report["engine"] == "sglang"
            assert report["engine_binding_sha256"] == engine_binding_sha256(binding)
            assert report["evidence_class"] == "SYNTHETIC_ONLY"
            assert report["runtime_verification"] == "UNVERIFIED"
            assert report["evidence_eligible"] is False
            assert report["dashboard_projection"] == "UNSUPPORTED_ENGINE_BINDING"
            statistics = report["statistical_report"]
            assert report["statistical_report_sha256"] == sha256_digest(
                canonical_json_bytes(statistics) + b"\n"
            )
            assert statistics["coverage"]["returned_trials"] == 8
            assert len(statistics["strata"]) == 2
            assert (
                sum(len(replica.requests) for replica in replicas)
                == requests_before_report
            )
            markdown = (tmp_path / "report" / "report.md").read_text()
            assert "SGLang 0.5.18" in markdown
            assert "SYNTHETIC_ONLY" in markdown
            assert "UNSUPPORTED_ENGINE_BINDING" in markdown
            for directory in (output, tmp_path / "report"):
                assert directory.stat().st_mode & 0o777 == 0o700
                for path in directory.iterdir():
                    assert path.stat().st_mode & 0o777 == 0o400
                    text = path.read_text()
                    assert all(
                        private not in text
                        for private in (
                            _MODEL,
                            _FOREGROUND,
                            _BACKGROUND,
                            _OUTPUT,
                            *origins,
                        )
                    )
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(exercise())
