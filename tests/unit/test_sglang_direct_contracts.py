"""SYNTHETIC_ONLY additive release contracts and native statistical replay."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    SglangDirectEngineBinding,
    build_sglang_direct_binding,
    build_sglang_engine_binding,
    engine_binding_bytes,
    engine_choice_sha256,
    load_engine_binding_bytes,
)
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.evaluation.sglang_direct_profile import (
    build_sglang_direct_profile,
    validate_sglang_direct_readiness,
)
from inferdrome.evaluation.sglang_direct_readiness import (
    acquire_sglang_direct_readiness,
    reset_sglang_direct_cache,
)
from inferdrome.evaluation.sglang_direct_runtime import runtime_manifest_sha256
from inferdrome.evaluation.sglang_profile import (
    build_sglang_serving_profile,
    validate_sglang_readiness,
)
from inferdrome.evaluation.sglang_report import (
    bind_sglang_report,
    load_sglang_report_bytes,
    read_sglang_direct_report_bytes,
    read_sglang_report_bytes,
    render_sglang_markdown,
)
from inferdrome.evaluation.sglang_results import (
    load_sglang_trial_bytes,
    sglang_trial_bytes,
    wrap_sglang_result,
)
from inferdrome.evaluation.study_config import compile_study
from inferdrome.evaluation.study_report import summarize_study, summarize_trial
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.sglang_direct_support import (
    direct_profiles,
    direct_study,
    synthetic_native,
    synthetic_runtime,
)
from tests.unit.test_evaluation_study_report import _manifest
from tests.unit.test_sglang_readiness import FakeProbes
from tests.unit.test_sglang_serving_profile import config, model_info


def bound(scenario="HEALTHY"):
    plan = compile_study(direct_study(scenario))
    binding = build_sglang_direct_binding(
        plan,
        direct_profiles(),
        runtime_manifest_sha256=runtime_manifest_sha256(synthetic_runtime()),
    )
    return plan, binding


def info015():
    value = json.loads(model_info())
    for name in (
        "served_model_name",
        "load_format",
        "reasoning_parser",
        "tool_call_parser",
    ):
        del value[name]
    value.update(model_type="qwen3", architectures=["Qwen3ForCausalLM"])
    return canonical_json_bytes(value)


def test_distinct_binding_has_no_image_and_binds_runtime():
    plan, binding = bound()
    old = build_sglang_engine_binding(plan, direct_profiles())
    assert type(binding) is SglangDirectEngineBinding
    assert binding.producer_version == "0.5.15"
    assert binding.source_commit == "f63458b5beaceabbd9d749b9fc956370e1b649e6"
    assert b"image" not in engine_binding_bytes(binding)
    assert (
        load_engine_binding_bytes(
            engine_binding_bytes(binding), plan, profiles=direct_profiles()
        )
        == binding
    )
    assert engine_choice_sha256(binding) != engine_choice_sha256(old)
    changed = binding.model_copy(
        update={"runtime_manifest_sha256": "sha256:" + "f" * 64}
    )
    assert engine_choice_sha256(changed) != engine_choice_sha256(binding)
    for p in direct_profiles().values():
        new, legacy = build_sglang_direct_profile(p), build_sglang_serving_profile(p)
        assert new.argv == legacy.argv and new.config_sha256 != legacy.config_sha256
    with pytest.raises(ValueError):
        EvaluationEngineBinding.model_validate_json(engine_binding_bytes(binding))


@pytest.mark.parametrize(
    "field,value",
    [
        ("producer_version", "0.5.18"),
        ("source_commit", "a" * 40),
        ("execution_mode", "DOCKER_BRIDGE"),
        ("evidence_eligible", 0),
        ("schema_version", "inferdrome.evaluation-engine-binding.v1"),
    ],
)
def test_forged_release_binding_rejected(field, value):
    plan, binding = bound()
    with pytest.raises(EvaluationError):
        engine_binding_bytes(binding.model_copy(update={field: value}))
    raw = json.loads(engine_binding_bytes(binding))
    raw[field] = value
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(canonical_json_bytes(raw) + b"\n", plan)


def test_new_binding_rejects_even_historical_image_declaration():
    from tests.unit.test_evaluation_engine_binding import profiles, study

    with pytest.raises(EvaluationError):
        build_sglang_direct_binding(
            study(), profiles(), runtime_manifest_sha256="sha256:" + "0" * 64
        )


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_new_trial_and_report_roundtrip_and_cross_version_rejection(scenario):
    plan, binding = bound(scenario)
    summaries = []
    for trial in plan.trials:
        result = wrap_sglang_result(synthetic_native(trial), binding, plan, trial)
        content = sglang_trial_bytes(plan, trial, result, binding)
        checked = load_sglang_trial_bytes(content, plan, trial, binding)
        assert checked.to_dict()["schema_version"].endswith(".v3")
        assert checked.to_dict()["telemetry_semantics"] == binding.telemetry_semantics
        summaries.append(summarize_trial(trial, checked._statistical_input))
        raw = json.loads(content)
        raw["schema_version"] = "inferdrome.evaluation-study-trial-result.v2"
        with pytest.raises(EvaluationError):
            load_sglang_trial_bytes(
                canonical_json_bytes(raw) + b"\n", plan, trial, binding
            )
        old = build_sglang_engine_binding(plan, direct_profiles())
        with pytest.raises(EvaluationError):
            load_sglang_trial_bytes(content, plan, trial, old)
    statistical = summarize_study(plan, summaries, _manifest(plan, summaries))
    report = bind_sglang_report(statistical, binding, plan)
    content = canonical_json_bytes(report) + b"\n"
    assert report["schema_version"].endswith(".v3")
    assert report["dashboard_projection"] == "UNSUPPORTED_ENGINE_BINDING"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert load_sglang_report_bytes(content, plan, binding) == report
    assert read_sglang_direct_report_bytes(content).engine_binding == binding
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(content)
    assert render_sglang_markdown(report, plan, binding).startswith("# SGLang 0.5.15")


def test_actual_015_readiness_shape_and_reset_sequence():
    statuses = dict(
        health_status=200,
        generation_status=200,
        model_info_status=200,
        model_info=info015(),
    )
    assert (
        validate_sglang_direct_readiness(config(), **statuses).config_sha256
        == build_sglang_direct_profile(config()).config_sha256
    )
    with pytest.raises(EvaluationError):
        validate_sglang_readiness(config(), **statuses)

    async def run():
        probes = FakeProbes()
        probes.responses["/model_info"] = [ProbeResponse(200, info015())]
        readiness = await acquire_sglang_direct_readiness(
            config(), probes, deadline_ns=1000, now_ns=probes.now
        )
        receipt = await reset_sglang_direct_cache(
            config(),
            probes,
            readiness=readiness,
            locally_owned_requests_drained=True,
            max_age_ns=100,
            deadline_ns=1000,
            now_ns=probes.now,
        )
        assert (
            receipt.runtime_verification == "UNVERIFIED"
            and receipt.evidence_eligible is False
        )
        assert probes.calls.count(("POST", "/flush_cache")) == 1
        assert probes.calls[4:] == [
            ("POST", "/flush_cache"),
            ("GET", "/health"),
            ("GET", "/model_info"),
            ("GET", "/metrics"),
        ]
        wrong = replace(
            readiness,
            readiness=replace(
                readiness.readiness,
                config_sha256=build_sglang_serving_profile(config()).config_sha256,
            ),
        )
        with pytest.raises(EvaluationError):
            await reset_sglang_direct_cache(
                config(),
                probes,
                readiness=wrong,
                locally_owned_requests_drained=True,
                max_age_ns=100,
                deadline_ns=1000,
                now_ns=probes.now,
            )

    asyncio.run(run())


def test_generated_contracts_and_legacy_binding_schema_are_current():
    from inferdrome.routing_execution.canonical import sha256_digest
    from scripts.generate_sglang_direct_contracts import render_contracts

    for path, content in render_contracts().items():
        assert path.read_bytes() == content
    # Independently captured from authoritative base 8baa732; no new schema fields.
    assert (
        sha256_digest(canonical_json_bytes(EvaluationEngineBinding.model_json_schema()))
        == "sha256:cfff82fb44c5c427dea03715d338a7f35399390ae7c73dedaa04c8f1f20730bd"
    )
