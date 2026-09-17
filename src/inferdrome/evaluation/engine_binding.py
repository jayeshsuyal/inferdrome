"""Pure pre-dispatch SGLang identity binding for the existing native study path.

No process, filesystem, network, or lifecycle work occurs here. Persisted digests
bind private declarations; they do not attest installed artifacts or GPU behavior.
The absent-binding vLLM path and frozen study/configuration contracts are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from inferdrome.evaluation.contracts import ClosedModel, EndpointId, EvaluationError
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SGLANG_IMAGE_TAG,
    SglangServingConfig,
    build_sglang_serving_profile,
    validate_sglang_evaluation_binding,
)
from inferdrome.evaluation.study_config import (
    CompiledStudy,
    CompiledTrial,
    Digest,
    StudyConfig,
    compile_study,
)
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

ENGINE_BINDING_SCHEMA: Final = "inferdrome.evaluation-engine-binding.v1"
SGLANG_TELEMETRY_SEMANTICS: Final = "SGLANG_0_5_18_SCHEDULER_GAUGES"
MAX_ENGINE_BINDING_BYTES: Final = 16_384
_BINDING_LIMITS = StructuredDataLimits(
    max_depth=8, max_tokens=2048, max_integer_digits=16
)
_ENDPOINT_IDS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class _BindingModel(ClosedModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class SglangModelIdentity(_BindingModel):
    """Declared artifact identity; names, snapshot paths and prompts are absent."""

    served_model_sha256: Digest
    model_revision: Revision
    tokenizer_revision: Revision
    model_snapshot_sha256: Digest
    tokenizer_snapshot_sha256: Digest
    chat_template_sha256: Digest
    attestation: Literal["DECLARED_NOT_ARTIFACT_VERIFIED"] = (
        "DECLARED_NOT_ARTIFACT_VERIFIED"
    )

    @model_validator(mode="after")
    def matched_revision(self) -> Self:
        if self.model_revision != self.tokenizer_revision:
            raise ValueError("engine model and tokenizer revisions must match")
        return self


class SglangEngineSettings(_BindingModel):
    """Variable settings within the fixed single-device BF16 serving profile."""

    context_length: Annotated[int, Field(ge=2, le=32_768)]
    max_running_requests: Annotated[int, Field(ge=1, le=64)]
    max_queued_requests: Annotated[int, Field(ge=0, le=1024)]
    mem_fraction_static_millis: Annotated[int, Field(ge=100, le=950)]
    random_seed: Annotated[int, Field(ge=0, le=2**31 - 1)]
    prefix_cache: Literal["RADIX_ENABLED", "RADIX_DISABLED"]
    weight_dtype: Literal["bfloat16"] = "bfloat16"
    kv_cache_dtype: Literal["bfloat16"] = "bfloat16"
    schedule_policy: Literal["fcfs"] = "fcfs"
    stream_interval: Literal[1] = 1
    decode_log_interval: Literal[40] = 40
    chunked_prefill_size: Literal[-1] = -1

    @field_validator(
        "stream_interval", "decode_log_interval", "chunked_prefill_size", mode="before"
    )
    @classmethod
    def strict_fixed_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("engine settings require integer primitives")
        return value


class EngineEndpointBinding(_BindingModel):
    endpoint_id: EndpointId
    origin_sha256: Digest
    profile_config_sha256: Digest


class EvaluationEngineBinding(_BindingModel):
    """One homogeneous engine selection bound before any native request replay."""

    schema_version: Literal["inferdrome.evaluation-engine-binding.v1"] = (
        "inferdrome.evaluation-engine-binding.v1"
    )
    engine: Literal["sglang"] = "sglang"
    profile: Literal["SGLANG_0_5_18_SINGLE_DEVICE_BF16"] = (
        "SGLANG_0_5_18_SINGLE_DEVICE_BF16"
    )
    config_sha256: Digest
    plan_sha256: Digest
    producer_version: Literal["0.5.18"] = "0.5.18"
    source_repository: Literal["https://github.com/sgl-project/sglang"] = (
        "https://github.com/sgl-project/sglang"
    )
    source_commit: Literal["71de97b264b04dcd514cf904003028aefe9775c8"] = (
        "71de97b264b04dcd514cf904003028aefe9775c8"
    )
    image_reference: Annotated[str, Field(max_length=128)] = SGLANG_IMAGE_REFERENCE
    image_tag: Literal["lmsysorg/sglang:v0.5.18-cu129-runtime"] = SGLANG_IMAGE_TAG
    image_platform: Literal["linux/amd64"] = "linux/amd64"
    endpoints: Annotated[
        tuple[EngineEndpointBinding, ...], Field(min_length=2, max_length=2)
    ]
    model_identity: SglangModelIdentity
    settings: SglangEngineSettings
    stream_path: Literal["/v1/chat/completions"] = "/v1/chat/completions"
    health_path: Literal["/health"] = "/health"
    generation_health_path: Literal["/health_generate"] = "/health_generate"
    model_identity_path: Literal["/model_info"] = "/model_info"
    readiness_identity: Literal["SERVER_DECLARED_MATCH"] = "SERVER_DECLARED_MATCH"
    telemetry_semantics: Literal["SGLANG_0_5_18_SCHEDULER_GAUGES"] = (
        "SGLANG_0_5_18_SCHEDULER_GAUGES"
    )
    telemetry_freshness: Literal["ACQUISITION_START_AGE_ONLY"] = (
        "ACQUISITION_START_AGE_ONLY"
    )
    telemetry_source_age: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    cache_preparation: Literal["WARMUP_DRAIN_FLUSH"] = "WARMUP_DRAIN_FLUSH"
    cache_state: Literal["DECLARED_COLD"] = "DECLARED_COLD"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False

    @field_validator("image_reference")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if value != SGLANG_IMAGE_REFERENCE:
            raise ValueError("engine image must match the pinned profile")
        return value

    @field_validator("evidence_eligible", mode="before")
    @classmethod
    def strict_false(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("engine eligibility requires a boolean primitive")
        return value

    @model_validator(mode="after")
    def two_distinct_identities(self) -> Self:
        if (
            tuple(endpoint.endpoint_id for endpoint in self.endpoints) != _ENDPOINT_IDS
            or len({endpoint.origin_sha256 for endpoint in self.endpoints}) != 2
            or len({endpoint.profile_config_sha256 for endpoint in self.endpoints}) != 2
        ):
            raise ValueError("engine binding requires two ordered distinct endpoints")
        return self


def _validated_plan(study: StudyConfig | CompiledStudy) -> CompiledStudy:
    config = study.config if type(study) is CompiledStudy else study
    if type(config) is not StudyConfig:
        raise ValueError
    validated = StudyConfig.model_validate(
        config.model_dump(mode="python", warnings=False)
    )
    plan = compile_study(validated)
    # Reject hand-edited/stale compiled dataclasses, including private trial config.
    if type(study) is CompiledStudy:
        if study != plan or canonical_json_bytes(
            study.to_dict()
        ) != canonical_json_bytes(plan.to_dict()):
            raise ValueError
        for actual, expected in zip(study.trials, plan.trials, strict=True):
            _check_trial(actual, expected)
    return plan


def _check_trial(actual: CompiledTrial, expected: CompiledTrial) -> None:
    if (
        type(actual) is not CompiledTrial
        or type(actual.config) is not type(expected.config)
        or canonical_json_bytes(actual.to_dict())
        != canonical_json_bytes(expected.to_dict())
    ):
        raise ValueError
    validated = type(expected.config).model_validate(
        actual.config.model_dump(mode="python", warnings=False)
    )
    if validated != expected.config:
        raise ValueError


def _identity(config: SglangServingConfig) -> SglangModelIdentity:
    return SglangModelIdentity(
        served_model_sha256=sha256_digest(config.served_model_name.encode("utf-8")),
        model_revision=config.model_revision,
        tokenizer_revision=config.tokenizer_revision,
        model_snapshot_sha256=config.model_snapshot_sha256,
        tokenizer_snapshot_sha256=config.tokenizer_snapshot_sha256,
        chat_template_sha256=config.chat_template_sha256,
    )


def _settings(config: SglangServingConfig) -> SglangEngineSettings:
    return SglangEngineSettings(
        context_length=config.context_length,
        max_running_requests=config.max_running_requests,
        max_queued_requests=config.max_queued_requests,
        mem_fraction_static_millis=int(config.mem_fraction_static * 1000),
        random_seed=config.random_seed,
        prefix_cache=config.prefix_cache,
    )


def _check_plan(binding: EvaluationEngineBinding, plan: CompiledStudy) -> None:
    foreground = plan.config.blocks[0].foreground
    preparation = plan.config.preparation
    expected_prefix = (
        "DECLARED_ENABLED"
        if binding.settings.prefix_cache == "RADIX_ENABLED"
        else "DECLARED_DISABLED"
    )
    if (
        binding.config_sha256 != plan.config_sha256
        or binding.plan_sha256
        != sha256_digest(canonical_json_bytes(plan.to_dict()) + b"\n")
        or binding.model_identity.served_model_sha256
        != sha256_digest(foreground.model.encode("utf-8"))
        or tuple(endpoint.origin_sha256 for endpoint in binding.endpoints)
        != tuple(sha256_digest(e.origin.encode("utf-8")) for e in foreground.endpoints)
        or foreground.max_tokens >= binding.settings.context_length
        or preparation.cache_state != binding.cache_state
        or preparation.prefix_caching != expected_prefix
        or preparation.serving_image_reference
        not in (None, SGLANG_IMAGE_REFERENCE.split("@", 1)[1])
    ):
        raise ValueError


def build_sglang_engine_binding(
    study: StudyConfig | CompiledStudy,
    profiles: Mapping[EndpointId, SglangServingConfig],
) -> EvaluationEngineBinding:
    """Bind both real native endpoints to one declared SGLang recipe, without I/O.

    The study must declare cold cache preparation and the matching radix-cache
    setting. Runtime checks still own readiness, warmup, draining and flushing.
    Profiles may use different local paths; their artifact identities and all
    serving settings must match. Raw names, origins and paths are only hashed.
    """
    try:
        plan = _validated_plan(study)
        if set(profiles) != set(_ENDPOINT_IDS) or any(
            type(profiles[endpoint_id]) is not SglangServingConfig
            for endpoint_id in _ENDPOINT_IDS
        ):
            raise ValueError
        endpoints = plan.config.blocks[0].foreground.endpoints
        prepared = tuple(
            build_sglang_serving_profile(
                SglangServingConfig.model_validate(
                    profiles[endpoint.endpoint_id].model_dump(
                        mode="python", warnings=False
                    )
                )
            )
            for endpoint in endpoints
        )
        first = prepared[0].config
        identity, settings = _identity(first), _settings(first)
        bindings: list[EngineEndpointBinding] = []
        for endpoint, profile in zip(endpoints, prepared, strict=True):
            if (
                profile.config.origin != endpoint.origin
                or _identity(profile.config) != identity
                or _settings(profile.config) != settings
            ):
                raise ValueError
            for block in plan.config.blocks:
                validate_sglang_evaluation_binding(profile.config, block.foreground)
                if block.background is not None:
                    validate_sglang_evaluation_binding(profile.config, block.background)
            bindings.append(
                EngineEndpointBinding(
                    endpoint_id=endpoint.endpoint_id,
                    origin_sha256=sha256_digest(endpoint.origin.encode("utf-8")),
                    profile_config_sha256=profile.config_sha256,
                )
            )
        binding = EvaluationEngineBinding(
            config_sha256=plan.config_sha256,
            plan_sha256=sha256_digest(canonical_json_bytes(plan.to_dict()) + b"\n"),
            endpoints=tuple(bindings),
            model_identity=identity,
            settings=settings,
        )
        _check_plan(binding, plan)
        return binding
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise EvaluationError(
            "engine binding violates its study/profile contract"
        ) from None


def engine_binding_bytes(binding: EvaluationEngineBinding) -> bytes:
    """Revalidate constructed/copied instances and return exact canonical bytes."""
    try:
        if type(binding) is not EvaluationEngineBinding:
            raise ValueError
        value = EvaluationEngineBinding.model_validate(
            binding.model_dump(mode="python", warnings=False)
        )
        raw = canonical_json_bytes(value.model_dump(mode="json"))
        if not 1 <= len(raw) + 1 <= MAX_ENGINE_BINDING_BYTES:
            raise ValueError
        return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"
    except (ValueError, TypeError, ValidationError, AttributeError, RecursionError):
        raise EvaluationError("engine binding violates its contract") from None


def engine_binding_sha256(binding: EvaluationEngineBinding) -> str:
    """Bind the complete pre-dispatch artifact, including its native study plan."""
    return sha256_digest(engine_binding_bytes(binding))


def engine_choice_sha256(binding: EvaluationEngineBinding) -> str:
    """Compare engine choice across calibration and confirmation study plans.

    Callers must require this digest unchanged for every phase before dispatch.
    Only the two native study digests are excluded; endpoint, snapshot, serving,
    preparation and telemetry identities remain bound. This is not attestation.
    """
    validated = EvaluationEngineBinding.model_validate_json(
        engine_binding_bytes(binding)
    )
    return sha256_digest(
        canonical_json_bytes(
            validated.model_dump(mode="json", exclude={"config_sha256", "plan_sha256"})
        )
        + b"\n"
    )


def load_engine_binding_bytes(
    content: bytes,
    study: StudyConfig | CompiledStudy,
    *,
    profiles: Mapping[EndpointId, SglangServingConfig] | None = None,
) -> EvaluationEngineBinding:
    """Reject noncanonical, malformed, mismatched or unsupported declarations.

    Supplying profiles also verifies every selected launch declaration. Without
    them, this validates the persisted declaration and native study association,
    not the unprovided profile inputs or any live server state.
    """
    try:
        if (
            type(content) is not bytes
            or not 1 <= len(content) <= MAX_ENGINE_BINDING_BYTES
        ):
            raise ValueError
        validate_json_structure(content.decode("utf-8"), limits=_BINDING_LIMITS)
        binding = EvaluationEngineBinding.model_validate_json(content)
        # Exact canonical bytes reject duplicate keys, omitted fields, extra
        # whitespace, alternate numeric encodings, BOMs and trailing JSON.
        if engine_binding_bytes(binding) != content:
            raise ValueError
        _check_plan(binding, _validated_plan(study))
        if profiles is not None and binding != build_sglang_engine_binding(
            study, profiles
        ):
            raise ValueError
        return binding
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise EvaluationError(
            "engine binding violates its study/profile contract"
        ) from None


def validate_engine_binding_trial(
    binding: EvaluationEngineBinding,
    plan: CompiledStudy,
    trial: CompiledTrial,
) -> None:
    """Require an exact bound native trial before dispatch or policy replay.

    The binding contains only study-level digests, so raw/trial verifiers must
    also receive the expected compiled study. This checks the whole study and
    the selected trial's metadata and private configuration, not merely its ID.
    """
    try:
        expected_plan = _validated_plan(plan)
        checked = load_engine_binding_bytes(
            engine_binding_bytes(binding), expected_plan
        )
        _check_plan(checked, expected_plan)
        matches = [t for t in expected_plan.trials if t.trial_id == trial.trial_id]
        if len(matches) != 1:
            raise ValueError
        _check_trial(trial, matches[0])
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise EvaluationError(
            "engine binding does not contain the expected study trial"
        ) from None
