"""SGLang 0.5.15 direct-host recipe; never an image or runtime attestation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.sglang_profile import (
    _INFO_LIMITS,
    _MAX_INFO_BYTES,
    SglangReadiness,
    SglangServingConfig,
    build_sglang_serving_profile,
)
from inferdrome.parsing import validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

SGLANG_DIRECT_VERSION: Final = "0.5.15"
SGLANG_DIRECT_COMMIT: Final = "f63458b5beaceabbd9d749b9fc956370e1b649e6"
SGLANG_DIRECT_PROFILE: Final = "SGLANG_0_5_15_DIRECT_SINGLE_DEVICE_BF16"
SGLANG_DIRECT_TELEMETRY: Final = "SGLANG_0_5_15_SCHEDULER_GAUGES"


@dataclass(frozen=True)
class SglangDirectServingProfile:
    config: SglangServingConfig
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    config_sha256: str
    profile: str = SGLANG_DIRECT_PROFILE
    producer_version: str = SGLANG_DIRECT_VERSION
    source_commit: str = SGLANG_DIRECT_COMMIT
    execution_mode: Literal["NATIVE_PROCESS"] = "NATIVE_PROCESS"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


def build_sglang_direct_profile(
    config: SglangServingConfig,
) -> SglangDirectServingProfile:
    """Reuse the audited finite argv contract, with a distinct release identity.

    Runtime environment is supplied only by the separately validated runtime
    manifest at process dispatch. No historical container identity is copied.
    """
    common = build_sglang_serving_profile(config)
    return SglangDirectServingProfile(
        config=common.config,
        argv=common.argv,
        environment=common.environment,
        config_sha256=sha256_digest(
            canonical_json_bytes(
                {
                    "profile": SGLANG_DIRECT_PROFILE,
                    "config": common.config.model_dump(mode="json"),
                }
            )
        ),
    )


def validate_sglang_direct_readiness(
    config: SglangServingConfig,
    *,
    health_status: int,
    generation_status: int,
    model_info_status: int,
    model_info: bytes,
) -> SglangReadiness:
    """Validate bounded injected responses gathered before cache preparation.

    /health_generate only establishes scheduler/detokenizer responsiveness, and
    can affect cache state. /model_info contains server declarations; it cannot
    verify the model/tokenizer/template digests. Unknown bounded fields are
    ignored, never persisted. No raw body or endpoint text appears in errors.
    """
    profile = build_sglang_direct_profile(config)
    config = profile.config

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def nonfinite(_: str) -> None:
        raise ValueError

    try:
        if (
            any(
                type(s) is not int or s != 200
                for s in (health_status, generation_status, model_info_status)
            )
            or not isinstance(model_info, bytes)
            or not 1 <= len(model_info) <= _MAX_INFO_BYTES
        ):
            raise ValueError
        text = model_info.decode("utf-8")
        validate_json_structure(text, limits=_INFO_LIMITS)
        value = json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=nonfinite,
            parse_float=nonfinite,
        )
        expected = {
            "model_path": config.model_path,
            "tokenizer_path": config.tokenizer_path,
            "is_generation": True,
            "model_type": "qwen3",
            "architectures": ["Qwen3ForCausalLM"],
            "weight_version": config.model_revision,
            "preferred_sampling_params": None,
            "has_image_understanding": False,
            "has_audio_understanding": False,
        }
        if not isinstance(value, dict) or any(
            key not in value
            or type(value[key]) is not type(wanted)
            or value[key] != wanted
            for key, wanted in expected.items()
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EvaluationError("SGLang readiness responses are invalid") from None
    return SglangReadiness(config_sha256=profile.config_sha256)
