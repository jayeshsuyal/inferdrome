"""Two actual localhost listeners, fake SGLang telemetry/SSE, no GPU or engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from inferdrome.evaluation import sglang_execution, study
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import build_sglang_direct_binding
from inferdrome.evaluation.sglang_direct_runtime import runtime_manifest_sha256
from inferdrome.evaluation.sglang_report import (
    read_sglang_direct_report_bytes,
    read_sglang_report_bytes,
)
from inferdrome.evaluation.study_config import compile_study
from tests.integration.test_sglang_study_loopback import _MODEL, _payload, _replicas
from tests.sglang_direct_support import synthetic_runtime
from tests.unit.test_evaluation_study_config import load
from tests.unit.test_sglang_serving_profile import config as serving_config


def test_direct_015_four_policy_native_streams_and_offline_report(
    tmp_path, monkeypatch
):
    original_wrap = sglang_execution.wrap_sglang_result

    def synthetic(native, binding, plan, trial):
        native = replace(
            native,
            evidence_class="SYNTHETIC_ONLY",
            foreground=replace(native.foreground, evidence_class="SYNTHETIC_ONLY"),
        )
        return original_wrap(native, binding, plan, trial)

    monkeypatch.setattr(sglang_execution, "wrap_sglang_result", synthetic)

    async def run():
        async with _replicas() as (origins, replicas):
            value = _payload(origins)
            value["blocks"] = value["blocks"][:1]
            value["preparation"].update(
                cache_state="DECLARED_COLD",
                prefix_caching="DECLARED_ENABLED",
                serving_image_reference=None,
            )
            config = load(value)
            plan = compile_study(config)
            profiles = {
                e: serving_config(served_model_name=_MODEL, origin=origin)
                for e, origin in zip(("endpoint-a", "endpoint-b"), origins, strict=True)
            }
            binding = build_sglang_direct_binding(
                plan,
                profiles,
                runtime_manifest_sha256=runtime_manifest_sha256(synthetic_runtime()),
            )
            executor = sglang_execution.SGLangStudyExecutor(plan, binding, profiles)
            manifest = await asyncio.wait_for(
                study.run_study(
                    config,
                    tmp_path / "study",
                    executor=executor,
                    engine_binding=binding,
                ),
                15,
            )
            assert manifest.status == "COMPLETED" and len(manifest.trials) == 4
            assert all(r.foreground_requests > 0 for r in replicas)
            for path in sorted((tmp_path / "study").glob("trial-*.json")):
                result = json.loads(path.read_bytes())
                assert result["schema_version"].endswith(".v3")
                assert (
                    result["result"]["telemetry_semantics"]
                    == "SGLANG_0_5_15_SCHEDULER_GAUGES"
                )
                assert all(
                    r["outcome"] == "SUCCESS"
                    for r in result["result"]["foreground"]["records"]
                )
            before = sum(len(r.requests) for r in replicas)
            report = study.report_study(
                config, tmp_path / "study", tmp_path / "report", engine_binding=binding
            )
            assert report["evidence_class"] == "SYNTHETIC_ONLY"
            assert report["dashboard_projection"] == "UNSUPPORTED_ENGINE_BINDING"
            content = (tmp_path / "report/report.json").read_bytes()
            assert read_sglang_direct_report_bytes(content).engine_binding == binding
            with pytest.raises(EvaluationError):
                read_sglang_report_bytes(content)
            assert sum(len(r.requests) for r in replicas) == before
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())
