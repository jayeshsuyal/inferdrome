"""Strict, provider-neutral deployment specification v1.

This module describes deployment intent.  It does not launch resources, resolve
credentials, contact a provider, or claim that a deployment was executed.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from pydantic.config import ExtraValues

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_canonical_json,
)
from inferdrome.domain.ids import Sha256Digest

DEPLOYMENT_SCHEMA_VERSION: Final = "inferdrome.deployment.v1"
DEPLOYMENT_SCHEMA_ID: Final = "urn:inferdrome:deployment:v1"

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credential_value",
        "password",
        "private_key",
        "secret",
        "secret_value",
        "token",
    }
)
_SENSITIVE_FIELD_NAMES_NORMALIZED = frozenset(
    name.replace("_", "") for name in _SENSITIVE_FIELD_NAMES
)
_SENSITIVE_FIELD_NORMALIZATION = re.compile(r"[^a-z0-9]+")
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"^(?:sk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^(?:gh[pousr]_)[A-Za-z0-9_]{16,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{16,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----$"),
    re.compile(r"^[A-Za-z0-9_-]{40,}$"),
)
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _cost_is_zero(value: str) -> bool:
    return Decimal(value) == Decimal("0")


def _looks_like_credential(value: str) -> bool:
    return any(
        pattern.fullmatch(value) is not None
        for pattern in _CREDENTIAL_VALUE_PATTERNS
    )

DeploymentId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]
BoundedName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}[A-Za-z0-9]$|^[A-Za-z0-9]$",
    ),
]
HardwareName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,126}[A-Za-z0-9)]$|^[A-Za-z0-9]$",
    ),
]
RegionName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$",
    ),
]
PinnedRevision = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40,64}$"),
]
ImageRepository = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[a-z0-9](?:[a-z0-9._/-]{0,254}[a-z0-9])?$",
    ),
]
ImageTag = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$",
    ),
]
CostString = Annotated[
    str,
    StringConstraints(
        max_length=16,
        pattern=r"^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?$",
    ),
]
MethodologyId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^inferdrome\.[a-z0-9](?:[a-z0-9._-]{0,120}[a-z0-9])?\.v[0-9]+$",
    ),
]
EndpointPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^/[A-Za-z0-9._/-]*$",
    ),
]
EndpointHost = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^[A-Za-z0-9]$",
    ),
]


def _normalized_field_name(value: str) -> str:
    return _SENSITIVE_FIELD_NORMALIZATION.sub("", value).lower()


def _reject_secret_shaped_fields(value: object) -> None:
    """Reject likely secret values before Pydantic can echo them in errors."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("deployment object keys must be strings")
            if _normalized_field_name(key) in _SENSITIVE_FIELD_NAMES_NORMALIZED:
                raise ValueError(
                    "credential-shaped fields are forbidden; use a secret reference"
                )
            _reject_secret_shaped_fields(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secret_shaped_fields(child)


class DeploymentModel(FrozenModel):
    """Closed deployment model with non-disclosing validation errors."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


EnvVarName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$"),
]
GcpProjectId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
]
GcpSecretId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$|^[a-z]$"),
]
GcpSecretVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[1-9][0-9]{0,19}$"),
]


class EnvironmentVariableReference(DeploymentModel):
    kind: Literal["environment_variable"]
    name: EnvVarName

    @model_validator(mode="after")
    def reject_credential_shaped_name(self) -> Self:
        if _looks_like_credential(self.name):
            raise ValueError(
                "environment-variable references cannot contain credential values"
            )
        return self


class GcpSecretManagerReference(DeploymentModel):
    kind: Literal["gcp_secret_manager"]
    project_id: GcpProjectId
    secret_id: GcpSecretId
    version: GcpSecretVersion

    @model_validator(mode="after")
    def reject_credential_shaped_components(self) -> Self:
        if any(
            _looks_like_credential(value)
            for value in (self.project_id, self.secret_id, self.version)
        ):
            raise ValueError(
                "secret-manager identifiers cannot contain credential values"
            )
        return self


SecretReference = Annotated[
    EnvironmentVariableReference | GcpSecretManagerReference,
    Field(discriminator="kind"),
]


class ProviderConfiguration(DeploymentModel):
    provider_id: Literal["local", "lambda_cloud", "gcp"]
    region: RegionName | None
    credential_refs: Annotated[tuple[SecretReference, ...], Field(max_length=8)]


class EndpointConfiguration(DeploymentModel):
    scheme: Literal["http", "https"]
    host: EndpointHost
    port: Annotated[int, Field(strict=True, ge=1, le=65_535)]
    path: EndpointPath
    visibility: Literal["loopback", "private"]

    @model_validator(mode="after")
    def validate_safe_endpoint(self) -> EndpointConfiguration:
        if ".." in self.path or "//" in self.path:
            raise ValueError(
                "endpoint paths cannot contain dot segments or empty segments"
            )
        try:
            parsed = ipaddress.ip_address(self.host)
        except ValueError:
            parsed = None

        if self.visibility == "loopback":
            if parsed is None or not parsed.is_loopback or self.host != "127.0.0.1":
                raise ValueError("loopback endpoints must use 127.0.0.1")
        else:
            if parsed is not None:
                if parsed.version != 4 or not any(
                    parsed in network for network in _RFC1918_NETWORKS
                ):
                    raise ValueError(
                        "private endpoints must use an RFC1918 IPv4 address"
                    )
            elif not self.host.endswith((".internal", ".local", ".private")):
                raise ValueError(
                    "private endpoint host must be private IP or private DNS"
                )
        return self


class VllmRuntimeConfiguration(DeploymentModel):
    engine: Literal["vllm"]
    engine_version: Literal["0.26.0"]
    adapter: Literal["vllm_bench_serve"]
    model_id: BoundedName
    model_revision: PinnedRevision
    tokenizer_revision: PinnedRevision
    endpoint: EndpointConfiguration
    registry_secret_refs: Annotated[tuple[SecretReference, ...], Field(max_length=8)]


class SglangReferenceConfiguration(DeploymentModel):
    """Reserved v1 shape; lifecycle and measurement adapters are not implemented."""

    engine: Literal["sglang"]
    engine_version: Annotated[
        str,
        StringConstraints(
            min_length=5,
            max_length=32,
            pattern=(
                r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
            ),
        ),
    ]
    adapter: Literal["sglang_reference_v1"]
    model_id: BoundedName
    model_revision: PinnedRevision
    tokenizer_revision: PinnedRevision
    endpoint: EndpointConfiguration
    registry_secret_refs: Annotated[tuple[SecretReference, ...], Field(max_length=8)]


RuntimeConfiguration = Annotated[
    VllmRuntimeConfiguration | SglangReferenceConfiguration,
    Field(discriminator="engine"),
]


class BenchmarkMethodologyReference(DeploymentModel):
    source: Literal["registered_manifest"]
    methodology_id: MethodologyId
    manifest_sha256: Sha256Digest


class BenchmarkTopology(DeploymentModel):
    methodology: BenchmarkMethodologyReference
    runner_runtime_colocation: Literal["colocated"]
    endpoint_scope: Literal["loopback", "private"]


class ImageIdentity(DeploymentModel):
    repository: ImageRepository
    digest: Sha256Digest | None
    tag: ImageTag | None

    @model_validator(mode="after")
    def require_identity(self) -> ImageIdentity:
        if self.digest is None and self.tag is None:
            raise ValueError("an image needs a digest or a tag")
        if self.tag == "latest":
            raise ValueError("floating latest image tags are forbidden")
        return self


class ArtifactIdentity(DeploymentModel):
    runner_image: ImageIdentity
    serving_runtime_image: ImageIdentity | None


class ResourceRequirements(DeploymentModel):
    gpu_count: Annotated[int, Field(strict=True, ge=0, le=16)]
    gpu_model: HardwareName | None
    cpu_cores: Annotated[int, Field(strict=True, ge=1, le=256)]
    memory_mib: Annotated[int, Field(strict=True, ge=256, le=1_048_576)]
    ephemeral_storage_gib: Annotated[int, Field(strict=True, ge=1, le=16_384)]


class LifecycleTimeouts(DeploymentModel):
    startup_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)]
    request_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)]
    total_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    cleanup_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)]
    termination_confirmation_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)]

    @model_validator(mode="after")
    def total_timeout_covers_cleanup(self) -> LifecycleTimeouts:
        if self.total_seconds < self.startup_seconds + self.cleanup_seconds:
            raise ValueError("total timeout must cover startup and cleanup budgets")
        return self


class CleanupPolicy(DeploymentModel):
    cleanup_on_every_exit: Literal[True]
    cleanup_receipt_required: Literal[True]
    orphan_policy: Literal["block_next_run"]
    max_cleanup_attempts: Annotated[int, Field(strict=True, ge=1, le=3)]


class CostCeiling(DeploymentModel):
    currency: Literal["USD"]
    max_cost_usd: CostString
    hard_limit: Literal[True]
    estimate_basis: Literal["controller_estimate"]
    invoice_truth: Literal["external_provider_invoice_required"]


class DeploymentSpec(DeploymentModel):
    """Versioned deployment intent consumed by later provider/runtime adapters."""

    schema_version: Literal["inferdrome.deployment.v1"]
    deployment_id: DeploymentId
    mode: Literal["development", "proof"]
    execution_intent: Literal["mock_only", "local_execute", "dry_run_reference"]
    provider: ProviderConfiguration
    runtime: RuntimeConfiguration
    topology: BenchmarkTopology
    artifacts: ArtifactIdentity
    resources: ResourceRequirements
    timeouts: LifecycleTimeouts
    cleanup_policy: CleanupPolicy
    cost_ceiling: CostCeiling

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        _preflight_deployment_json(json_data)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @model_validator(mode="after")
    def validate_deployment_combinations(self) -> DeploymentSpec:
        provider_id = self.provider.provider_id
        intent = self.execution_intent
        if provider_id == "local":
            if self.provider.region is not None:
                raise ValueError("local deployments cannot declare a cloud region")
            if intent == "dry_run_reference":
                raise ValueError(
                    "local deployments must execute locally or be mock-only"
                )
        elif self.provider.region is None:
            raise ValueError("cloud provider references require a region")

        if provider_id != "local" and intent != "dry_run_reference":
            raise ValueError(
                "unimplemented cloud providers are dry-run references only"
            )
        if provider_id == "local" and intent == "mock_only":
            if self.mode != "development":
                raise ValueError("mock-only deployments cannot use proof mode")
            if self.resources.gpu_count != 0 or self.resources.gpu_model is not None:
                raise ValueError("mock-only deployments cannot declare GPU resources")
            if self.runtime.endpoint.visibility != "loopback":
                raise ValueError("mock-only deployments require a loopback endpoint")
            if not _cost_is_zero(self.cost_ceiling.max_cost_usd):
                raise ValueError("mock-only deployments must have a zero cost ceiling")

        if self.mode == "proof":
            if intent == "mock_only":
                raise ValueError("proof mode cannot be mock-only")
            if self.artifacts.runner_image.digest is None:
                raise ValueError("proof mode requires an immutable runner image digest")
            serving_image = self.artifacts.serving_runtime_image
            if serving_image is None or serving_image.digest is None:
                raise ValueError(
                    "proof mode requires an immutable serving-runtime image digest"
                )
            if self.resources.gpu_count < 1 or self.resources.gpu_model is None:
                raise ValueError(
                    "proof mode requires an explicit positive GPU resource"
                )
            if _cost_is_zero(self.cost_ceiling.max_cost_usd) and provider_id != "local":
                raise ValueError(
                    "proof mode requires a positive paid-provider cost ceiling"
                )
            if self.topology.runner_runtime_colocation != "colocated":
                raise ValueError("proof mode requires runner/runtime colocation")

        if self.runtime.endpoint.visibility != self.topology.endpoint_scope:
            raise ValueError(
                "runtime endpoint visibility must match benchmark topology"
            )

        if (
            isinstance(self.runtime, SglangReferenceConfiguration)
            and (self.mode != "development" or intent != "dry_run_reference")
        ):
            raise ValueError(
                "SGLang is reserved for development dry-run references"
            )
        return self


def deployment_spec_json_value(spec: DeploymentSpec) -> dict[str, Any]:
    """Return the exact JSON-compatible value used for canonicalization."""

    value = spec.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("deployment specification root must serialize as an object")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("deployment JSON object keys must be unique")
        value[key] = child
    return value


def _preflight_deployment_json(payload: str | bytes | bytearray) -> object:
    """Parse once with duplicate-key detection and a non-disclosing scan."""

    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("deployment specification is not valid JSON") from exc
    _reject_secret_shaped_fields(decoded)
    return decoded


def parse_deployment_spec_json(payload: str | bytes) -> DeploymentSpec:
    """Parse one unambiguous JSON deployment document."""

    _preflight_deployment_json(payload)
    return DeploymentSpec.model_validate_json(payload)


def canonical_deployment_spec_bytes(spec: DeploymentSpec) -> bytes:
    """Serialize a deployment specification using RFC 8785 canonical JSON."""

    return canonical_json_bytes(deployment_spec_json_value(spec))


def deployment_spec_digest(spec: DeploymentSpec) -> str:
    """Return the domain-separated digest of the canonical deployment intent."""

    return digest_canonical_json(
        DigestDomain.DEPLOYMENT_SPEC,
        deployment_spec_json_value(spec),
    )


def deployment_spec_schema() -> dict[str, Any]:
    """Render the closed structural Draft 2020-12 schema for deployment v1."""

    generated = deepcopy(
        DeploymentSpec.model_json_schema(
            by_alias=True,
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": DEPLOYMENT_SCHEMA_ID,
        "$comment": (
            "Cross-field deployment invariants are normative; see "
            "docs/DEPLOYMENT_SPEC_V1.md. This contract declares intent only and "
            "does not attest to execution or provider billing."
        ),
        **generated,
    }
