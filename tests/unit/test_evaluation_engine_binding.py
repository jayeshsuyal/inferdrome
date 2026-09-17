"""SYNTHETIC_ONLY declarations; no GPU, serving runtime or artifact attestation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    MAX_ENGINE_BINDING_BYTES,
    EvaluationEngineBinding,
    build_sglang_engine_binding,
    engine_binding_bytes,
    engine_binding_sha256,
    engine_choice_sha256,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.sglang_container import build_sglang_container_profile
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SglangServingConfig,
    build_sglang_serving_profile,
)
from inferdrome.evaluation.study_config import StudyConfig, compile_study
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_study_config import load, study_payload
from tests.unit.test_sglang_serving_profile import config as serving_config


def study(scenario: str = "HEALTHY") -> StudyConfig:
    value = study_payload(scenario)
    value["preparation"].update(
        cache_state="DECLARED_COLD",
        prefix_caching="DECLARED_ENABLED",
        serving_image_reference=SGLANG_IMAGE_REFERENCE.split("@", 1)[1],
    )
    return load(value)


def profiles() -> dict[EndpointId, SglangServingConfig]:
    return {
        "endpoint-a": serving_config(served_model_name="private-model"),
        "endpoint-b": serving_config(
            served_model_name="private-model", origin="http://127.0.0.1:8002"
        ),
    }


def encoded(value: dict) -> bytes:
    return canonical_json_bytes(value) + b"\n"


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_binding_roundtrip_uses_native_plan_digest_and_both_profiles(
    scenario: str,
) -> None:
    config = study(scenario)
    selected = profiles()
    plan = compile_study(config)
    binding = build_sglang_engine_binding(config, selected)
    assert binding == build_sglang_engine_binding(
        plan, dict(reversed(selected.items()))
    )
    assert binding.config_sha256 == plan.config_sha256
    assert binding.plan_sha256 == sha256_digest(encoded(plan.to_dict()))
    assert binding.engine == "sglang"
    assert binding.source_commit == "71de97b264b04dcd514cf904003028aefe9775c8"
    assert binding.producer_version == "0.5.18"
    assert binding.image_reference == SGLANG_IMAGE_REFERENCE
    assert binding.telemetry_semantics == "SGLANG_0_5_18_SCHEDULER_GAUGES"
    assert binding.telemetry_freshness == "ACQUISITION_START_AGE_ONLY"
    assert binding.telemetry_source_age == "UNAVAILABLE"
    assert binding.runtime_verification == "UNVERIFIED"
    assert binding.evidence_eligible is False
    assert binding.cache_preparation == "WARMUP_DRAIN_FLUSH"
    assert binding.model_identity.attestation == "DECLARED_NOT_ARTIFACT_VERIFIED"
    assert tuple(e.endpoint_id for e in binding.endpoints) == (
        "endpoint-a",
        "endpoint-b",
    )
    for endpoint in binding.endpoints:
        profile = build_sglang_serving_profile(selected[endpoint.endpoint_id])
        assert endpoint.profile_config_sha256 == profile.config_sha256
        assert endpoint.origin_sha256 == sha256_digest(profile.config.origin.encode())
    raw = engine_binding_bytes(binding)
    assert engine_binding_sha256(binding) == sha256_digest(raw)
    assert load_engine_binding_bytes(raw, plan) == binding
    assert load_engine_binding_bytes(raw, config, profiles=selected) == binding


def test_binding_does_not_persist_private_paths_origins_names_or_prompts() -> None:
    binding = build_sglang_engine_binding(study("STALE_LOAD"), profiles())
    raw = engine_binding_bytes(binding)
    for forbidden in (
        b"private-model",
        b"private foreground",
        b"private background",
        b"127.0.0.1",
        b"/opt/inferdrome",
        b"synthetic.jinja",
    ):
        assert forbidden not in raw
        assert forbidden.decode() not in repr(binding)
    assert len(raw) <= MAX_ENGINE_BINDING_BYTES


def test_build_and_parse_are_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("binding performs no filesystem I/O")

    config, selected = study(), profiles()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", no_io)
        patch.setattr(Path, "open", no_io)
        binding = build_sglang_engine_binding(config, selected)
        assert (
            load_engine_binding_bytes(
                engine_binding_bytes(binding), config, profiles=selected
            )
            == binding
        )


@pytest.mark.parametrize("cache", ["UNKNOWN", "DECLARED_WARM"])
def test_only_declared_cold_preparation_is_supported(cache: str) -> None:
    value = study().model_dump(mode="json")
    value["preparation"]["cache_state"] = cache
    with pytest.raises(EvaluationError, match="engine binding"):
        build_sglang_engine_binding(load(value), profiles())


@pytest.mark.parametrize("prefix", ["UNKNOWN", "DECLARED_DISABLED"])
def test_preparation_must_match_selected_prefix_cache(prefix: str) -> None:
    value = study().model_dump(mode="json")
    value["preparation"]["prefix_caching"] = prefix
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(load(value), profiles())


def test_radix_disabled_and_omitted_native_image_reference_are_explicit() -> None:
    value = study().model_dump(mode="json")
    value["preparation"]["prefix_caching"] = "DECLARED_DISABLED"
    value["preparation"]["serving_image_reference"] = None
    selected = {
        key: config.model_copy(update={"prefix_cache": "RADIX_DISABLED"})
        for key, config in profiles().items()
    }
    binding = build_sglang_engine_binding(load(value), selected)
    assert binding.settings.prefix_cache == "RADIX_DISABLED"
    assert binding.image_reference == SGLANG_IMAGE_REFERENCE


def test_conflicting_native_image_digest_is_rejected() -> None:
    value = study().model_dump(mode="json")
    value["preparation"]["serving_image_reference"] = "sha256:" + "0" * 64
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(load(value), profiles())


@pytest.mark.parametrize("mapping", ["missing", "extra", "swapped", "duplicate"])
def test_two_exact_real_endpoint_bindings_are_required(mapping: str) -> None:
    selected = profiles()
    if mapping == "missing":
        del selected["endpoint-b"]
    elif mapping == "extra":
        selected["endpoint-c"] = selected["endpoint-a"]
    elif mapping == "swapped":
        selected["endpoint-a"], selected["endpoint-b"] = (
            selected["endpoint-b"],
            selected["endpoint-a"],
        )
    else:
        selected["endpoint-b"] = selected["endpoint-a"]
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(study(), selected)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("served_model_name", "different-model"),
        ("model_snapshot_sha256", "sha256:" + "1" * 64),
        ("tokenizer_snapshot_sha256", "sha256:" + "2" * 64),
        ("chat_template_sha256", "sha256:" + "3" * 64),
        ("context_length", 8192),
        ("max_running_requests", 9),
        ("max_queued_requests", 65),
        ("random_seed", 43),
        ("prefix_cache", "RADIX_DISABLED"),
    ],
)
def test_heterogeneous_endpoint_identity_and_settings_are_rejected(
    field: str, value: object
) -> None:
    selected = profiles()
    selected["endpoint-b"] = selected["endpoint-b"].model_copy(update={field: value})
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(study(), selected)


def test_different_local_paths_are_allowed_and_bound_without_persistence() -> None:
    selected = profiles()
    selected["endpoint-b"] = selected["endpoint-b"].model_copy(
        update={"model_path": "/second/model", "tokenizer_path": "/second/tokenizer"}
    )
    binding = build_sglang_engine_binding(study(), selected)
    assert b"/second" not in engine_binding_bytes(binding)
    assert (
        binding.endpoints[1].profile_config_sha256
        == build_sglang_serving_profile(selected["endpoint-b"]).config_sha256
    )


@pytest.mark.parametrize("malformed", ["type", "bounds", "model", "revision"])
def test_copied_profile_instances_are_revalidated(malformed: str) -> None:
    updates = {
        "type": {"max_running_requests": True},
        "bounds": {"context_length": 128},
        "model": {"served_model_name": "different-model"},
        "revision": {"tokenizer_revision": "f" * 40},
    }
    selected = {
        key: config.model_copy(update=updates[malformed])
        for key, config in profiles().items()
    }
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(study(), selected)


@pytest.mark.parametrize("mutation", ["config_digest", "trials", "private_config"])
def test_stale_or_forged_compiled_plan_is_rejected(mutation: str) -> None:
    plan = compile_study(study())
    if mutation == "config_digest":
        plan = replace(plan, config_sha256="sha256:" + "f" * 64)
    elif mutation == "trials":
        plan = replace(plan, trials=tuple(reversed(plan.trials)))
    else:
        trial = plan.trials[0]
        config = trial.config.model_copy(
            update={
                "foreground": trial.config.foreground.model_copy(
                    update={"model": "other"}
                )
            }
        )
        plan = replace(plan, trials=(replace(trial, config=config), *plan.trials[1:]))
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(plan, profiles())


def test_invalid_copied_study_is_revalidated() -> None:
    config = study().model_copy(update={"blocks": ()})
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(config, profiles())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "inferdrome.evaluation-engine-binding.v2"),
        ("engine", "vllm"),
        ("producer_version", "0.5.19"),
        ("source_commit", "0" * 40),
        ("image_reference", "lmsysorg/sglang:latest"),
        ("image_platform", "linux/arm64"),
        ("telemetry_semantics", "VLLM_RUNNING_WAITING"),
        ("telemetry_source_age", "FRESH"),
        ("runtime_verification", "VERIFIED"),
        ("evidence_eligible", True),
        ("evidence_eligible", 0),
        ("cache_preparation", "NONE"),
        ("health_path", "/ready"),
        ("config_sha256", "sha256:" + "0" * 64),
        ("plan_sha256", "sha256:" + "0" * 64),
        ("origin", "http://127.0.0.1:8001"),
    ],
)
def test_parser_rejects_unsupported_contract_and_study_bindings(
    field: str, value: object
) -> None:
    config = study()
    payload = build_sglang_engine_binding(config, profiles()).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(encoded(payload), config)


@pytest.mark.parametrize(
    "field", ["origin_sha256", "profile_config_sha256", "endpoint_id"]
)
def test_parser_rejects_duplicate_endpoint_identity(field: str) -> None:
    config = study()
    payload = build_sglang_engine_binding(config, profiles()).model_dump(mode="json")
    payload["endpoints"][1][field] = payload["endpoints"][0][field]
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(encoded(payload), config)


@pytest.mark.parametrize("nested", ["model_identity", "settings", "endpoints"])
def test_nested_extra_fields_are_rejected(nested: str) -> None:
    config = study()
    payload = build_sglang_engine_binding(config, profiles()).model_dump(mode="json")
    target = payload[nested][0] if nested == "endpoints" else payload[nested]
    target["private_path"] = "/private/secret"
    with pytest.raises(EvaluationError) as error:
        load_engine_binding_bytes(encoded(payload), config)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "malformed",
    [
        b"",
        b"\xff",
        b"{}",
        b"null",
        b"[]",
        b"NaN",
        b"Infinity",
        b"[" * 9 + b"]" * 9,
        b'{"x":12345678901234567}',
        b" " * (MAX_ENGINE_BINDING_BYTES + 1),
    ],
)
def test_parser_rejects_malformed_or_structurally_excessive_bytes(
    malformed: bytes,
) -> None:
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(malformed, study())


@pytest.mark.parametrize(
    "variant", ["no_lf", "space", "pretty", "duplicate", "omission"]
)
def test_parser_requires_exact_canonical_bytes(variant: str) -> None:
    config = study()
    binding = build_sglang_engine_binding(config, profiles())
    raw = engine_binding_bytes(binding)
    if variant == "no_lf":
        raw = raw[:-1]
    elif variant == "space":
        raw += b" "
    elif variant == "pretty":
        raw = json.dumps(binding.model_dump(mode="json"), indent=2).encode() + b"\n"
    elif variant == "duplicate":
        raw = raw.replace(b"{", b'{"engine":"sglang",', 1)
    else:
        payload = binding.model_dump(mode="json")
        del payload["evidence_eligible"]
        raw = encoded(payload)
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(raw, config)


def test_parser_with_profiles_verifies_declared_artifact_identity() -> None:
    config = study()
    binding = build_sglang_engine_binding(config, profiles())
    payload = binding.model_dump(mode="json")
    payload["model_identity"]["model_snapshot_sha256"] = "sha256:" + "f" * 64
    # An offline parser can validate an unauthenticated declaration without
    # launch profiles; pre-dispatch verification must supply the actual inputs.
    assert (
        load_engine_binding_bytes(encoded(payload), config).model_identity
        != binding.model_identity
    )
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(encoded(payload), config, profiles=profiles())


@pytest.mark.parametrize(
    "field", ["stream_interval", "decode_log_interval", "chunked_prefill_size"]
)
def test_fixed_numeric_settings_reject_float_and_bool(field: str) -> None:
    config = study()
    payload = build_sglang_engine_binding(config, profiles()).model_dump(mode="json")
    payload["settings"][field] = (
        True if field == "stream_interval" else float(payload["settings"][field])
    )
    # Standard JSON preserves 40.0 rather than RFC8785 normalizing it to 40.
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(json.dumps(payload).encode() + b"\n", config)


def test_output_revalidates_constructed_or_copied_binding() -> None:
    binding = build_sglang_engine_binding(study(), profiles())
    forged = binding.model_copy(update={"evidence_eligible": 0})
    with pytest.raises(EvaluationError):
        engine_binding_bytes(forged)
    with pytest.raises(EvaluationError):
        engine_choice_sha256(forged)
    with pytest.raises(EvaluationError):
        engine_binding_bytes(EvaluationEngineBinding.model_construct())


def test_choice_digest_is_stable_across_workloads_but_binds_every_engine_setting() -> (
    None
):
    baseline = study()
    value = baseline.model_dump(mode="json")
    value["blocks"][0]["foreground"]["offers"][0]["prompt"] += "new calibration level"
    changed_plan = load(value)
    original = build_sglang_engine_binding(baseline, profiles())
    other_workload = build_sglang_engine_binding(changed_plan, profiles())
    assert engine_binding_sha256(original) != engine_binding_sha256(other_workload)
    assert engine_choice_sha256(original) == engine_choice_sha256(other_workload)
    selected = {
        key: config.model_copy(update={"random_seed": 43})
        for key, config in profiles().items()
    }
    changed_engine = build_sglang_engine_binding(changed_plan, selected)
    assert engine_choice_sha256(original) != engine_choice_sha256(changed_engine)
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(engine_binding_bytes(original), changed_plan)


def test_origin_and_path_changes_change_engine_choice_digest() -> None:
    config = study()
    original = build_sglang_engine_binding(config, profiles())
    selected = profiles()
    selected["endpoint-a"] = selected["endpoint-a"].model_copy(
        update={"model_path": "/alternative/model"}
    )
    assert engine_choice_sha256(original) != engine_choice_sha256(
        build_sglang_engine_binding(config, selected)
    )
    value = config.model_dump(mode="json")
    value["blocks"][0]["foreground"]["endpoints"][0]["origin"] = "http://127.0.0.1:8003"
    selected = profiles()
    selected["endpoint-a"] = selected["endpoint-a"].model_copy(
        update={"origin": "http://127.0.0.1:8003"}
    )
    assert engine_choice_sha256(original) != engine_choice_sha256(
        build_sglang_engine_binding(load(value), selected)
    )


def test_vllm_native_plan_remains_identical() -> None:
    config = study()
    before = encoded(compile_study(config).to_dict())
    build_sglang_engine_binding(config, profiles())
    assert before == encoded(compile_study(config).to_dict())
    assert b"sglang" not in before


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
def test_trial_membership_accepts_exact_native_trials(scenario: str) -> None:
    plan = compile_study(study(scenario))
    binding = build_sglang_engine_binding(plan, profiles())
    for trial in plan.trials:
        validate_engine_binding_trial(binding, plan, trial)


@pytest.mark.parametrize("mutation", ["id", "metadata", "private", "type", "plan"])
def test_trial_membership_rejects_rebound_or_tampered_trial(mutation: str) -> None:
    plan = compile_study(study())
    binding = build_sglang_engine_binding(plan, profiles())
    trial = plan.trials[0]
    if mutation == "id":
        trial = replace(trial, trial_id="trial-0010")
    elif mutation == "metadata":
        trial = replace(trial, config_sha256="sha256:" + "f" * 64)
    elif mutation == "private":
        foreground = trial.config.foreground.model_copy(update={"model": "other"})
        trial = replace(
            trial, config=trial.config.model_copy(update={"foreground": foreground})
        )
    elif mutation == "type":
        trial = replace(trial, repeat_index=False)
    else:
        value = plan.config.model_dump(mode="json")
        value["blocks"][0]["foreground"]["offers"][0]["prompt"] += "different plan"
        plan = compile_study(load(value))
        trial = plan.trials[0]
    with pytest.raises(EvaluationError):
        validate_engine_binding_trial(binding, plan, trial)


def test_docker_binding_freezes_launch_and_preserves_source_identity() -> None:
    plan = compile_study(study())
    selected = profiles()
    native = build_sglang_engine_binding(plan, selected)
    docker = build_sglang_engine_binding(plan, selected, containerized=True)
    assert native.execution_mode == "NATIVE_PROCESS"
    assert all(
        endpoint.projected_launch_sha256 is None for endpoint in native.endpoints
    )
    assert docker.execution_mode == "DOCKER_BRIDGE"
    for index, endpoint in enumerate(docker.endpoints):
        projected = build_sglang_container_profile(
            selected[endpoint.endpoint_id], index
        )
        assert (
            endpoint.profile_config_sha256
            == native.endpoints[index].profile_config_sha256
        )
        assert endpoint.projected_launch_sha256 == projected.launch_sha256
    assert engine_choice_sha256(native) != engine_choice_sha256(docker)
    assert (
        load_engine_binding_bytes(engine_binding_bytes(docker), plan, profiles=selected)
        == docker
    )


@pytest.mark.parametrize(
    "mutation", ["mode", "missing", "native-present", "duplicate", "wrong-launch"]
)
def test_docker_binding_cannot_drop_or_swap_projected_identity(mutation: str) -> None:
    plan = compile_study(study())
    selected = profiles()
    binding = build_sglang_engine_binding(plan, selected, containerized=True)
    payload = binding.model_dump(mode="json")
    if mutation == "mode":
        payload["execution_mode"] = "UNMANAGED"
    elif mutation == "missing":
        payload["endpoints"][0]["projected_launch_sha256"] = None
    elif mutation == "native-present":
        payload["execution_mode"] = "NATIVE_PROCESS"
    elif mutation == "duplicate":
        payload["endpoints"][1]["projected_launch_sha256"] = payload["endpoints"][0][
            "projected_launch_sha256"
        ]
    else:
        payload["endpoints"][0]["projected_launch_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(EvaluationError):
        load_engine_binding_bytes(encoded(payload), plan, profiles=selected)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_containerized_selection_requires_boolean_primitive(value: object) -> None:
    with pytest.raises(EvaluationError):
        build_sglang_engine_binding(study(), profiles(), containerized=value)
