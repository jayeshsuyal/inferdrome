"""Pure SGLang serving preparation; no process, endpoint, or evidence executor.

Source and image metadata are pinned in docs/SGLANG_SERVING_PREPARATION.md.
Declarations and server-reported identity never prove installed artifact identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from inferdrome.adapters.sglang import SGLANG_RELEASE_COMMIT, SGLANG_VERSION
from inferdrome.evaluation.contracts import (
    ClosedModel,
    Endpoint,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.parsing import (
    StructuredDataLimits,
    bounded_json_float,
    validate_json_structure,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

SGLANG_IMAGE_TAG: Final = "lmsysorg/sglang:v0.5.18-cu129-runtime"
SGLANG_IMAGE_REFERENCE: Final = (
    "lmsysorg/sglang@sha256:"
    "291c8e3d2c6268128f1d8fd36455083533ed65e5b6c3f1bb223b096360419374"
)
SGLANG_STREAM_PATH: Final = "/v1/chat/completions"
SGLANG_READINESS_PATHS: Final = ("/health", "/health_generate", "/model_info")
_INFO_LIMITS = StructuredDataLimits(
    max_depth=16, max_tokens=4096, max_integer_digits=16
)
_MAX_INFO_BYTES = 65_536
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
LocalPath = Annotated[str, Field(min_length=2, max_length=4096, repr=False)]


class SglangServingConfig(ClosedModel):
    """Closed single-device, text-only BF16 recipe, with no arbitrary argv/env.

    Paths and snapshot digests are declarations. A future owner must preflight
    real directories/files, verify hashes and model config, and isolate ambient
    environment before executing. Construction performs no filesystem access.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
    )
    schema_version: Literal["inferdrome.sglang-serving-preparation.v1"] = (
        "inferdrome.sglang-serving-preparation.v1"
    )
    origin: Annotated[str, Field(max_length=80, repr=False)]
    served_model_name: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    ]
    model_path: LocalPath
    tokenizer_path: LocalPath
    chat_template_path: LocalPath
    model_revision: Revision
    tokenizer_revision: Revision
    model_snapshot_sha256: Digest
    tokenizer_snapshot_sha256: Digest
    chat_template_sha256: Digest
    context_length: Annotated[int, Field(ge=2, le=32_768)]
    max_running_requests: Annotated[int, Field(ge=1, le=64)]
    max_queued_requests: Annotated[int, Field(ge=0, le=1024)]
    mem_fraction_static: Annotated[
        Decimal,
        Field(
            ge=Decimal("0.1"),
            le=Decimal("0.95"),
            max_digits=3,
            decimal_places=3,
            allow_inf_nan=False,
        ),
    ]
    random_seed: Annotated[int, Field(ge=0, le=2**31 - 1)]
    prefix_cache: Literal["RADIX_ENABLED", "RADIX_DISABLED"]

    @field_validator("origin")
    @classmethod
    def private_origin(cls, value: str) -> str:
        return Endpoint(endpoint_id="endpoint-a", origin=value).origin

    @field_validator("mem_fraction_static", mode="before")
    @classmethod
    def bounded_memory_fraction(cls, value: object, info: ValidationInfo) -> object:
        # Bound compact Decimal/string exponents before normalization/formatting.
        if isinstance(value, str) and len(value) > 16:
            raise ValueError("memory fraction exceeds its lexical bound")
        if info.mode == "json" and isinstance(value, str):
            try:
                value = Decimal(value)
            except InvalidOperation:
                raise ValueError("memory fraction is invalid") from None
        if isinstance(value, Decimal) and (
            not value.is_finite()
            or len(value.as_tuple().digits) > 16
            or value.adjusted() < -1
            or value.adjusted() > 0
            or not isinstance(value.as_tuple().exponent, int)
            or int(value.as_tuple().exponent) < -6
        ):
            raise ValueError("memory fraction exceeds its numeric bound")
        return value

    @field_validator("model_path", "tokenizer_path", "chat_template_path")
    @classmethod
    def canonical_local_path(cls, value: str) -> str:
        # A deliberately small path alphabet, excluding shell/options/URI shapes.
        if (
            not value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/")[1:])
            or any(not (c.isascii() and (c.isalnum() or c in "/._-")) for c in value)
        ):
            raise ValueError("serving snapshot path is outside the local contract")
        return value

    @model_validator(mode="after")
    def matched_tokenizer(self) -> Self:
        if self.model_revision != self.tokenizer_revision:
            raise ValueError("model and tokenizer must use the same snapshot revision")
        if not self.chat_template_path.endswith(".jinja"):
            raise ValueError("the serving profile requires a Jinja template file")
        return self


@dataclass(frozen=True)
class SglangServingProfile:
    config: SglangServingConfig
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    config_sha256: str
    image_reference: str = SGLANG_IMAGE_REFERENCE
    image_tag: str = SGLANG_IMAGE_TAG
    image_platform: Literal["linux/amd64"] = "linux/amd64"
    producer_version: str = SGLANG_VERSION
    source_commit: str = SGLANG_RELEASE_COMMIT
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


def _validated(config: SglangServingConfig) -> SglangServingConfig:
    try:
        if type(config) is not SglangServingConfig:
            raise ValueError
        # Recheck model_copy/model_construct instances at each public boundary.
        return SglangServingConfig.model_validate_json(
            canonical_json_bytes(config.model_dump(mode="json", warnings=False))
        )
    except (ValueError, TypeError, ValidationError):
        raise EvaluationError("SGLang serving configuration is invalid") from None


def build_sglang_serving_profile(config: SglangServingConfig) -> SglangServingProfile:
    """Build an argv tuple for a future owned process; never invoke a shell."""
    config = _validated(config)
    endpoint = urlsplit(config.origin)
    argv: tuple[str, ...] = (
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        config.model_path,
        "--tokenizer-path",
        config.tokenizer_path,
        "--chat-template",
        config.chat_template_path,
        "--revision",
        config.model_revision,
        "--served-model-name",
        config.served_model_name,
        "--weight-version",
        config.model_revision,
        "--host",
        str(endpoint.hostname),
        "--port",
        str(endpoint.port),
        "--device",
        "cuda",
        "--tp-size",
        "1",
        "--pp-size",
        "1",
        "--dp-size",
        "1",
        "--nnodes",
        "1",
        "--dtype",
        "bfloat16",
        "--kv-cache-dtype",
        "bfloat16",
        "--quantization",
        "unquant",
        "--load-format",
        "safetensors",
        "--tokenizer-mode",
        "auto",
        "--tokenizer-backend",
        "huggingface",
        "--context-length",
        str(config.context_length),
        "--max-running-requests",
        str(config.max_running_requests),
        "--max-queued-requests",
        str(config.max_queued_requests),
        "--mem-fraction-static",
        format(config.mem_fraction_static, "f").rstrip("0").rstrip("."),
        "--random-seed",
        str(config.random_seed),
        "--schedule-policy",
        "fcfs",
        "--chunked-prefill-size",
        "-1",
        "--stream-interval",
        "1",
        "--decode-log-interval",
        "40",
        "--enable-metrics",
    )
    if config.prefix_cache == "RADIX_DISABLED":
        argv += ("--disable-radix-cache",)
    return SglangServingProfile(
        config=config,
        argv=argv,
        environment=(
            ("HF_HUB_OFFLINE", "1"),
            ("HF_DATASETS_OFFLINE", "1"),
            ("TRANSFORMERS_OFFLINE", "1"),
            ("HF_HUB_DISABLE_TELEMETRY", "1"),
        ),
        config_sha256=sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
    )


def validate_sglang_evaluation_binding(
    config: SglangServingConfig, evaluation: EvaluationConfig
) -> None:
    """Check declared model/endpoint/output bounds; tokenization remains external."""
    config = _validated(config)
    try:
        evaluation = EvaluationConfig.model_validate_json(
            canonical_json_bytes(evaluation.model_dump(mode="json"))
        )
        if (
            config.served_model_name != evaluation.model
            or config.origin not in {e.origin for e in evaluation.endpoints}
            or evaluation.max_tokens >= config.context_length
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError("SGLang evaluation binding is invalid") from None


@dataclass(frozen=True)
class SglangReadiness:
    """Accepted probe responses, not a GPU qualification or snapshot attestation."""

    config_sha256: str
    identity: Literal["SERVER_DECLARED_MATCH"] = "SERVER_DECLARED_MATCH"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"


def validate_sglang_readiness(
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
    profile = build_sglang_serving_profile(config)
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
            parse_float=bounded_json_float,
        )
        expected = {
            "model_path": config.model_path,
            "served_model_name": config.served_model_name,
            "tokenizer_path": config.tokenizer_path,
            "is_generation": True,
            "weight_version": config.model_revision,
            "load_format": "safetensors",
            "reasoning_parser": None,
            "tool_call_parser": None,
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
