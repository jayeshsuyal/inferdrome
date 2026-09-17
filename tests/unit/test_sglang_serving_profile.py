"""SYNTHETIC_ONLY profile/readiness probes; SGLang runtime remains unverified."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SGLANG_IMAGE_TAG,
    SglangServingConfig,
    build_sglang_serving_profile,
    validate_sglang_evaluation_binding,
    validate_sglang_readiness,
)
from tests.unit.test_evaluation_runner import config as evaluation_config


def config(**overrides: object) -> SglangServingConfig:
    return SglangServingConfig.model_validate(
        {
            "origin": "http://127.0.0.1:8001",
            "served_model_name": "test/private-model",
            "model_path": "/opt/inferdrome/models/synthetic",
            "tokenizer_path": "/opt/inferdrome/tokenizers/synthetic",
            "chat_template_path": "/opt/inferdrome/templates/synthetic.jinja",
            "model_revision": "a" * 40,
            "tokenizer_revision": "a" * 40,
            "model_snapshot_sha256": "sha256:" + "b" * 64,
            "tokenizer_snapshot_sha256": "sha256:" + "c" * 64,
            "chat_template_sha256": "sha256:" + "d" * 64,
            "context_length": 4096,
            "max_running_requests": 8,
            "max_queued_requests": 64,
            "mem_fraction_static": Decimal("0.80"),
            "random_seed": 42,
            "prefix_cache": "RADIX_ENABLED",
            **overrides,
        }
    )


def model_info(**overrides: object) -> bytes:
    c = config()
    return json.dumps(
        {
            "model_path": c.model_path,
            "served_model_name": c.served_model_name,
            "tokenizer_path": c.tokenizer_path,
            "is_generation": True,
            "weight_version": c.model_revision,
            "load_format": "safetensors",
            "preferred_sampling_params": None,
            "reasoning_parser": None,
            "tool_call_parser": None,
            "has_image_understanding": False,
            "has_audio_understanding": False,
            "model_type": "synthetic",
            "architectures": ["SyntheticOnly"],
            **overrides,
        }
    ).encode()


def ready(body: bytes | None = None, **statuses: int) -> object:
    return validate_sglang_readiness(
        config(),
        model_info=model_info() if body is None else body,
        **{
            "health_status": 200,
            "generation_status": 200,
            "model_info_status": 200,
            **statuses,
        },
    )


def test_pure_profile_pinned_flags_and_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_filesystem(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure preparation must not read snapshot paths")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", no_filesystem)
        profile = build_sglang_serving_profile(config())
    assert profile.argv[:3] == ("python3", "-m", "sglang.launch_server")
    pairs = dict(zip(profile.argv[3:-1:2], profile.argv[4:-1:2], strict=True))
    assert pairs["--dtype"] == pairs["--kv-cache-dtype"] == "bfloat16"
    assert pairs["--quantization"] == "unquant"
    assert pairs["--tp-size"] == pairs["--dp-size"] == pairs["--pp-size"] == "1"
    assert pairs["--revision"] == pairs["--weight-version"] == "a" * 40
    assert pairs["--mem-fraction-static"] == "0.8"
    assert pairs["--stream-interval"] == "1"
    assert pairs["--chunked-prefill-size"] == "-1"
    assert profile.argv[-1] == "--enable-metrics"
    assert "--trust-remote-code" not in profile.argv
    assert "--disable-radix-cache" not in profile.argv
    assert profile.image_reference == SGLANG_IMAGE_REFERENCE
    assert profile.image_tag == SGLANG_IMAGE_TAG
    assert profile.image_platform == "linux/amd64"
    assert profile.source_commit == "71de97b264b04dcd514cf904003028aefe9775c8"
    assert profile.runtime_verification == "UNVERIFIED"
    assert profile.evidence_eligible is False
    assert dict(profile.environment)["HF_HUB_OFFLINE"] == "1"


def test_cache_policy_and_configuration_binding() -> None:
    enabled = build_sglang_serving_profile(config())
    disabled = build_sglang_serving_profile(config(prefix_cache="RADIX_DISABLED"))
    assert disabled.argv == (*enabled.argv, "--disable-radix-cache")
    assert disabled.config_sha256 != enabled.config_sha256
    changed = build_sglang_serving_profile(
        config(model_snapshot_sha256="sha256:" + "f" * 64)
    )
    assert changed.argv == enabled.argv
    assert changed.config_sha256 != enabled.config_sha256
    assert enabled == build_sglang_serving_profile(config())


@pytest.mark.parametrize(
    "overrides",
    [
        {"origin": "http://example.com:8001"},
        {"origin": "http://127.0.0.1:8001/v1"},
        {"origin": "http://user:secret@127.0.0.1:8001"},
        {"model_path": "remote/model"},
        {"model_path": "/opt/../model"},
        {"model_path": "/opt//model"},
        {"model_path": "/opt/model/"},
        {"model_path": "/opt/$(touch-sentinel)"},
        {"model_path": "/opt/model\n--api-key"},
        {"tokenizer_path": "/opt/./model"},
        {"chat_template_path": "builtin-template"},
        {"chat_template_path": "/opt/template.txt"},
        {"chat_template_path": "/opt/template.json"},
        {"served_model_name": "model:adapter"},
        {"tokenizer_revision": "b" * 40},
        {"model_revision": "main"},
        {"model_snapshot_sha256": "unknown"},
        {"max_running_requests": True},
        {"max_running_requests": 65},
        {"max_queued_requests": -1},
        {"mem_fraction_static": Decimal("NaN")},
        {"mem_fraction_static": Decimal("1e-10000")},
        {"mem_fraction_static": Decimal("0.1234")},
        {"mem_fraction_static": Decimal("0.96")},
        {"mem_fraction_static": 0.8},
        {"random_seed": "42"},
        {"dtype": "fp8"},
        {"tp_size": 2},
        {"speculative_algorithm": "EAGLE"},
        {"extra_args": ["--api-key", "sentinel"]},
        {"environment": {"SGLANG_DP_RANK": "0"}},
        {"prefix_cache": "UNKNOWN"},
    ],
)
def test_unsupported_configuration_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        config(**overrides)


def test_unvalidated_model_copy_rejected() -> None:
    forged = config().model_copy(update={"model_path": "/opt/../wrong"})
    with pytest.raises(EvaluationError, match="configuration is invalid"):
        build_sglang_serving_profile(forged)


def test_evaluation_binding() -> None:
    evaluation = evaluation_config([0])
    validate_sglang_evaluation_binding(config(), evaluation)
    for changed in (
        config(served_model_name="wrong/model"),
        config(origin="http://127.0.0.1:30000"),
        config(context_length=evaluation.max_tokens),
    ):
        with pytest.raises(EvaluationError, match="binding is invalid"):
            validate_sglang_evaluation_binding(changed, evaluation)


def test_readiness_is_only_server_declared_match() -> None:
    result = validate_sglang_readiness(
        config(),
        health_status=200,
        generation_status=200,
        model_info_status=200,
        model_info=model_info(),
    )
    assert result.config_sha256 == build_sglang_serving_profile(config()).config_sha256
    assert result.identity == "SERVER_DECLARED_MATCH"
    assert result.runtime_verification == "UNVERIFIED"


@pytest.mark.parametrize(
    "changes",
    [
        {"served_model_name": "wrong/model"},
        {"model_path": "/opt/other-model"},
        {"tokenizer_path": "/opt/other-tokenizer"},
        {"is_generation": 1},
        {"is_generation": False},
        {"weight_version": "default"},
        {"load_format": "dummy"},
        {"reasoning_parser": "qwen3"},
        {"tool_call_parser": "qwen25"},
        {"has_image_understanding": True},
        {"preferred_sampling_params": {"temperature": 1}},
    ],
)
def test_model_mismatch_fails_closed(changes: dict[str, object]) -> None:
    with pytest.raises(EvaluationError, match="readiness responses are invalid"):
        ready(model_info(**changes))


@pytest.mark.parametrize(
    "statuses",
    [
        {"health_status": 503},
        {"generation_status": 503},
        {"model_info_status": 404},
        {"health_status": True},
    ],
)
def test_all_readiness_statuses_required(statuses: dict[str, int]) -> None:
    with pytest.raises(EvaluationError):
        ready(**statuses)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"{}",
        b"\xff",
        b'{"x":NaN}',
        b'{"x":1,"x":2}',
        b" " * 65_537,
        b"[" * 17 + b"0" + b"]" * 17,
        b'{"ignored":1e999}',
        b'{"ignored":12345678901234567}',
    ],
)
def test_malformed_readiness_is_bounded_and_sanitized(body: bytes) -> None:
    with pytest.raises(
        EvaluationError, match=r"^SGLang readiness responses are invalid$"
    ):
        ready(body)


def test_missing_identity_field_never_inferred() -> None:
    value = json.loads(model_info())
    del value["tokenizer_path"]
    with pytest.raises(EvaluationError):
        ready(json.dumps(value).encode())


def test_duplicate_required_identity_rejected() -> None:
    body = model_info()[:-1] + b', "served_model_name": "test/private-model"}'
    with pytest.raises(EvaluationError):
        ready(body)


def test_invalid_memory_fraction_in_unvalidated_config_is_sanitized() -> None:
    for value in ("invalid", "1e-10000", "1e999999999", "1" * 100):
        forged = config().model_copy(update={"mem_fraction_static": value})
        with pytest.raises(EvaluationError, match="configuration is invalid"):
            build_sglang_serving_profile(forged)
