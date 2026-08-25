"""Offline GCP inventory and dry-run planning contract.

This module deliberately has no GCP SDK, transport, credential, subprocess, or
metadata-server dependency.  A plan is derived only from a caller-supplied
strict inventory snapshot and the existing deployment specification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic.config import ExtraValues

from inferdrome.deployment.spec import (
    CleanupPolicy,
    CostCeiling,
    DeploymentSpec,
    GcpSecretManagerReference,
    ImageIdentity,
    LifecycleTimeouts,
    ResourceRequirements,
    canonical_deployment_spec_bytes,
    deployment_spec_digest,
    parse_deployment_spec_json,
)
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import SemanticVersion, Sha256Digest, sha256_digest
from inferdrome.errors import VerificationError
from inferdrome.immutable import publish_immutable_directory

GCP_INVENTORY_SCHEMA_VERSION: Final = "inferdrome.gcp-inventory.v1"
GCP_INVENTORY_SCHEMA_ID: Final = "urn:inferdrome:gcp-inventory:v1"
GCP_PLAN_SCHEMA_VERSION: Final = "inferdrome.gcp-dry-run-plan.v1"
GCP_PLAN_SCHEMA_ID: Final = "urn:inferdrome:gcp-dry-run-plan:v1"
GCP_PLAN_FILENAME: Final = "plan.json"
GCP_ADAPTER_ID: Final = "inferdrome.provider.gcp.read_only_plan"
GCP_ADAPTER_VERSION: Final = "1.0.0"
GCP_RUNTIME_ADAPTER_ID: Final = "inferdrome.runtime.vllm.bench_serve"
GCP_RUNTIME_ADAPTER_VERSION: Final = "0.26.0"
SUPPORTED_GCP_GPU_MODEL: Final = "NVIDIA A100-SXM4-40GB"
MAX_GCP_INVENTORY_BYTES: Final = 524_288
MAX_GCP_PLAN_BYTES: Final = 524_288

_TIMESTAMP = re.compile(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PUBLIC_ENDPOINT = re.compile(r"^(?:https?|ssh|ftp)://.*$", re.IGNORECASE)
_SENSITIVE_KEYS = frozenset(
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
        "public_ip",
        "external_ip",
        "endpoint",
        "endpoint_url",
        "url",
        "uri",
        "host",
    }
)
_SENSITIVE_KEYS_NORMALIZED = frozenset(
    re.sub(r"[^a-z0-9]+", "", key.lower()) for key in _SENSITIVE_KEYS
)
_HEX_IDENTITY_KEYS = frozenset({"modelrevision", "tokenizerrevision"})
_CREDENTIAL_SHAPES = (
    re.compile(r"^(?:sk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^(?:gh[pousr]_)[A-Za-z0-9_]{16,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{16,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----$"),
    re.compile(r"^[A-Za-z0-9_-]{40,}$"),
)

GcpProjectId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
]
GcpRegion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=32,
        pattern=r"^[a-z]+-[a-z]+[0-9]{1,2}$",
    ),
]
GcpZone = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=64,
        pattern=r"^[a-z]+-[a-z]+[0-9]{1,2}-[a-z]$",
    ),
]
GcpResourceName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=96, pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,94}[a-z0-9])?$"
    ),
]
GcpHardwareName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,126}[A-Za-z0-9)]$|^[A-Za-z0-9]$",
    ),
]
GcpTimestamp = Annotated[
    str, StringConstraints(max_length=32, pattern=_TIMESTAMP.pattern)
]


class GcpPlanError(VerificationError):
    """A bounded GCP inventory or offline-plan failure."""


class GcpPlanPublicationError(GcpPlanError):
    """A plan failed safe immutable publication or read-back verification."""


class GcpModel(FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _looks_credential_shaped(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _CREDENTIAL_SHAPES)


def _is_allowed_hex_identity(value: str, key_path: tuple[str, ...]) -> bool:
    return (
        _looks_credential_shaped(value)
        and len(key_path) > 0
        and key_path[-1] in _HEX_IDENTITY_KEYS
        and re.fullmatch(r"[0-9a-f]{40,64}", value) is not None
    )


def _reject_unsafe_inventory_values(
    value: object, *, key_path: tuple[str, ...] = ()
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("GCP inventory object keys must be strings")
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS_NORMALIZED:
                raise ValueError("GCP inventory contains forbidden sensitive fields")
            _reject_unsafe_inventory_values(child, key_path=(*key_path, normalized))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_unsafe_inventory_values(child, key_path=key_path)
    elif isinstance(value, str) and (
        _PUBLIC_ENDPOINT.fullmatch(value) is not None
        or (
            _looks_credential_shaped(value)
            and not _is_allowed_hex_identity(value, key_path)
        )
    ):
        raise ValueError("GCP inventory contains forbidden sensitive values")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("GCP JSON object keys must be unique")
        value[key] = child
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError("GCP JSON contains a non-finite number")


def _preflight_json(
    payload: str | bytes | bytearray, *, limit: int, kind: str
) -> object:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
    else:
        raise ValueError(f"{kind} JSON input is invalid")
    if len(encoded) > limit:
        raise ValueError(f"{kind} JSON exceeds its bound")
    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(f"{kind} is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise ValueError(f"{kind} root must be an object")
    _reject_unsafe_inventory_values(decoded)
    return decoded


class GcpInventorySource(GcpModel):
    kind: Literal["synthetic_fixture", "offline_snapshot"]
    observed_at: GcpTimestamp | None

    @model_validator(mode="after")
    def validate_source_time(self) -> Self:
        if self.observed_at is not None:
            try:
                datetime.strptime(self.observed_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                raise ValueError(
                    "inventory observation time is not a valid UTC timestamp"
                ) from None
        if self.kind == "synthetic_fixture" and self.observed_at is not None:
            raise ValueError("synthetic inventory cannot claim an observation time")
        if self.kind == "offline_snapshot" and self.observed_at is None:
            raise ValueError("offline inventory requires an observation time")
        return self


class GcpInventoryOffering(GcpModel):
    """One explicit zone-scoped catalog offering, never a cross-product."""

    zone: GcpZone
    machine_type: GcpResourceName
    architecture: Literal["amd64", "arm64"]
    cpu_cores: int = Field(strict=True, ge=1, le=256)
    memory_mib: int = Field(strict=True, ge=256, le=1_048_576)
    ephemeral_storage_gib: int = Field(strict=True, ge=1, le=16_384)
    accelerator_model: GcpHardwareName
    accelerator_provider_type: GcpResourceName
    accelerator_max_count: int = Field(strict=True, ge=1, le=16)
    catalog_eligibility: Literal["catalog_eligible", "catalog_unavailable", "unknown"]


class GcpInventorySnapshot(GcpModel):
    schema_version: Literal["inferdrome.gcp-inventory.v1"]
    project_id: GcpProjectId
    region: GcpRegion
    source: GcpInventorySource
    offerings: tuple[GcpInventoryOffering, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        identities = [
            (
                offering.zone,
                offering.machine_type,
                offering.accelerator_model,
                offering.accelerator_provider_type,
            )
            for offering in self.offerings
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("GCP inventory offerings must be unique")
        if any(
            not offering.zone.startswith(f"{self.region}-")
            for offering in self.offerings
        ):
            raise ValueError("GCP inventory offering is outside the declared region")
        return self

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
        _preflight_json(json_data, limit=MAX_GCP_INVENTORY_BYTES, kind="GCP inventory")
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


class GcpPlanningContext(GcpModel):
    """Explicit compute scope supplied by the operator, never a secret location."""

    compute_project_id: GcpProjectId
    region: GcpRegion

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
        _preflight_json(
            json_data, limit=MAX_GCP_INVENTORY_BYTES, kind="GCP planning context"
        )
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


class GcpPlanAdapterIdentity(GcpModel):
    adapter_id: Literal["inferdrome.provider.gcp.read_only_plan"]
    adapter_version: SemanticVersion


class GcpPlanImage(GcpModel):
    repository: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=256,
            pattern=r"^[a-z0-9](?:[a-z0-9._/-]{0,254}[a-z0-9])?$",
        ),
    ]
    digest: Sha256Digest


class GcpPlanRuntime(GcpModel):
    adapter_id: Literal["inferdrome.runtime.vllm.bench_serve"]
    adapter_version: SemanticVersion
    engine: Literal["vllm"]
    engine_version: Literal["0.26.0"]
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    model_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    tokenizer_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    endpoint_scheme: Literal["http", "https"]
    endpoint_port: int = Field(strict=True, ge=1, le=65_535)
    endpoint_path: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64, pattern=r"^/[A-Za-z0-9._/-]*$"),
    ]
    endpoint_visibility: Literal["private"]
    endpoint_scope: Literal["private"]
    runner_runtime_colocation: Literal["colocated"]


class GcpPlanProvider(GcpModel):
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: GcpResourceName
    architecture: Literal["amd64"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: GcpResourceName
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    catalog_eligibility: Literal["catalog_eligible"]


class GcpPlanScope(GcpModel):
    compute_project_id: GcpProjectId
    region: GcpRegion


class GcpPlanSelection(GcpModel):
    requested_resources: ResourceRequirements
    selected_provider: GcpPlanProvider


class GcpDryRunPlanPayload(GcpModel):
    schema_version: Literal["inferdrome.gcp-dry-run-plan.v1"]
    plan_kind: Literal["gcp_dry_run_reference"]
    deployment_spec_digest: Sha256Digest
    inventory_digest: Sha256Digest
    planning_scope: GcpPlanScope
    adapter: GcpPlanAdapterIdentity
    runtime: GcpPlanRuntime
    runner_image: GcpPlanImage
    serving_runtime_image: GcpPlanImage
    selection: GcpPlanSelection
    timeouts: LifecycleTimeouts
    cleanup_policy: CleanupPolicy
    cost_ceiling: CostCeiling
    execution_authorized: Literal[False]
    provider_mutation_performed: Literal[False]
    credentials_resolved: Literal[False]
    capacity_proven: Literal[False]
    pricing_proven: Literal[False]
    evidence_eligible: Literal[False]
    pricing_status: Literal["not_evaluated_pricing_unavailable"]
    invoice_truth: Literal["unavailable_external_provider_invoice"]


class GcpDryRunPlan(GcpDryRunPlanPayload):
    plan_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.plan_id != gcp_plan_id(self):
            raise ValueError("GCP plan identity does not match its canonical payload")
        return self

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
        _preflight_json(json_data, limit=MAX_GCP_PLAN_BYTES, kind="GCP plan")
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


class GcpReadOnlyInventorySource(Protocol):
    """Future injected read-only transport boundary; unused by offline planning."""

    def read_inventory(self, *, project_id: str, region: str) -> GcpInventorySnapshot:
        """Return a bounded snapshot without mutation or credential resolution."""


@dataclass(frozen=True)
class PublishedGcpPlan:
    path: Path
    plan: GcpDryRunPlan
    plan_sha256: Sha256Digest


def _json_value(model: GcpModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("GCP contract root must serialize as an object")
    return value


def _payload_value(plan: GcpDryRunPlan | GcpDryRunPlanPayload) -> dict[str, Any]:
    value = _json_value(plan)
    value.pop("plan_id", None)
    return value


def canonical_gcp_inventory_bytes(snapshot: GcpInventorySnapshot) -> bytes:
    value = _json_value(snapshot)
    value["offerings"] = sorted(
        value["offerings"],
        key=lambda item: (
            item["zone"],
            item["machine_type"],
            item["accelerator_model"],
            item["accelerator_provider_type"],
        ),
    )
    return canonical_json_bytes(value)


def gcp_inventory_digest(snapshot: GcpInventorySnapshot) -> str:
    return digest_bytes(
        DigestDomain.GCP_INVENTORY, canonical_gcp_inventory_bytes(snapshot)
    )


def parse_gcp_inventory_json(payload: str | bytes) -> GcpInventorySnapshot:
    _preflight_json(payload, limit=MAX_GCP_INVENTORY_BYTES, kind="GCP inventory")
    return GcpInventorySnapshot.model_validate_json(payload)


def canonical_gcp_plan_payload_bytes(
    plan: GcpDryRunPlan | GcpDryRunPlanPayload,
) -> bytes:
    return canonical_json_bytes(_payload_value(plan))


def gcp_plan_id(plan: GcpDryRunPlan | GcpDryRunPlanPayload) -> str:
    return digest_bytes(DigestDomain.GCP_PLAN, canonical_gcp_plan_payload_bytes(plan))


def canonical_gcp_plan_bytes(plan: GcpDryRunPlan) -> bytes:
    return canonical_json_bytes(_json_value(plan))


def gcp_plan_sha256(plan: GcpDryRunPlan) -> str:
    return sha256_digest(canonical_gcp_plan_bytes(plan))


def parse_gcp_plan_json(payload: str | bytes) -> GcpDryRunPlan:
    _preflight_json(payload, limit=MAX_GCP_PLAN_BYTES, kind="GCP plan")
    return GcpDryRunPlan.model_validate_json(payload)


def _strict_inventory(snapshot: GcpInventorySnapshot) -> GcpInventorySnapshot:
    raw = canonical_gcp_inventory_bytes(snapshot)
    parsed = parse_gcp_inventory_json(raw)
    if canonical_gcp_inventory_bytes(parsed) != raw:
        raise GcpPlanError("GCP inventory is not canonically self-consistent")
    return parsed


def _strict_context(context: GcpPlanningContext) -> GcpPlanningContext:
    raw = canonical_json_bytes(_json_value(context))
    parsed = GcpPlanningContext.model_validate_json(raw)
    if canonical_json_bytes(_json_value(parsed)) != raw:
        raise GcpPlanError("GCP planning context is not canonically self-consistent")
    return parsed


def _strict_spec(spec: DeploymentSpec) -> DeploymentSpec:
    raw = canonical_deployment_spec_bytes(spec)
    parsed = parse_deployment_spec_json(raw)
    if canonical_deployment_spec_bytes(parsed) != raw:
        raise GcpPlanError(
            "deployment specification is not canonically self-consistent"
        )
    return parsed


def _image(image: ImageIdentity) -> GcpPlanImage:
    if image.digest is None:
        raise GcpPlanError("GCP dry-run requires immutable image identities")
    return GcpPlanImage(repository=image.repository, digest=image.digest)


def _validate_gcp_spec(spec: DeploymentSpec, context: GcpPlanningContext) -> None:
    if not (
        spec.provider.provider_id == "gcp"
        and spec.execution_intent == "dry_run_reference"
        and spec.mode == "proof"
    ):
        raise GcpPlanError("only the proof GCP dry-run reference is supported")
    if spec.provider.region is None:
        raise GcpPlanError("GCP dry-run requires a region")
    if context.region != spec.provider.region:
        raise GcpPlanError("planning context region does not match deployment region")
    if not all(
        isinstance(ref, GcpSecretManagerReference)
        for ref in spec.provider.credential_refs
    ):
        raise GcpPlanError("GCP dry-run credential references must remain structured")
    if not (
        spec.runtime.engine == "vllm"
        and spec.runtime.engine_version == "0.26.0"
        and spec.runtime.adapter == "vllm_bench_serve"
    ):
        raise GcpPlanError("GCP dry-run runtime capability is unsupported")
    if (
        spec.runtime.endpoint.visibility != "private"
        or spec.topology.endpoint_scope != "private"
    ):
        raise GcpPlanError("GCP dry-run requires private endpoint topology")
    if spec.topology.runner_runtime_colocation != "colocated":
        raise GcpPlanError("GCP dry-run requires runner/runtime colocation")
    if (
        spec.resources.gpu_count < 1
        or spec.resources.gpu_model != SUPPORTED_GCP_GPU_MODEL
    ):
        raise GcpPlanError("GCP dry-run requires the pinned A100 SXM4 profile")
    if spec.artifacts.serving_runtime_image is None:
        raise GcpPlanError("GCP dry-run requires a serving-runtime image")
    _image(spec.artifacts.runner_image)
    _image(spec.artifacts.serving_runtime_image)
    if spec.cost_ceiling.currency != "USD" or not spec.cost_ceiling.max_cost_usd:
        raise GcpPlanError("GCP dry-run requires a bounded USD cost ceiling")


def _select(
    spec: DeploymentSpec,
    inventory: GcpInventorySnapshot,
    context: GcpPlanningContext,
) -> GcpPlanProvider:
    if (
        inventory.project_id != context.compute_project_id
        or inventory.region != context.region
    ):
        raise GcpPlanError("GCP inventory does not match deployment project and region")
    candidates = [
        offering
        for offering in inventory.offerings
        if (
            offering.catalog_eligibility == "catalog_eligible"
            and offering.architecture == "amd64"
            and offering.accelerator_model == SUPPORTED_GCP_GPU_MODEL
            and offering.accelerator_max_count >= spec.resources.gpu_count
            and offering.cpu_cores >= spec.resources.cpu_cores
            and offering.memory_mib >= spec.resources.memory_mib
            and offering.ephemeral_storage_gib >= spec.resources.ephemeral_storage_gib
        )
    ]
    candidates.sort(
        key=lambda offering: (
            offering.zone,
            offering.machine_type,
            offering.accelerator_model,
            offering.accelerator_provider_type,
        )
    )
    if not candidates:
        raise GcpPlanError("GCP inventory has no eligible resource match")
    offering = candidates[0]
    return GcpPlanProvider(
        project_id=context.compute_project_id,
        region=inventory.region,
        zone=offering.zone,
        machine_type=offering.machine_type,
        architecture="amd64",
        accelerator_model=SUPPORTED_GCP_GPU_MODEL,
        accelerator_provider_type=offering.accelerator_provider_type,
        accelerator_count=spec.resources.gpu_count,
        catalog_eligibility="catalog_eligible",
    )


def plan_gcp_dry_run(
    spec: DeploymentSpec,
    inventory: GcpInventorySnapshot,
    context: GcpPlanningContext,
) -> GcpDryRunPlan:
    """Build a deterministic plan without contacting GCP or resolving secrets."""

    selected_spec = _strict_spec(spec)
    selected_inventory = _strict_inventory(inventory)
    selected_context = _strict_context(context)
    _validate_gcp_spec(selected_spec, selected_context)
    selected = _select(selected_spec, selected_inventory, selected_context)
    runtime = selected_spec.runtime
    serving_image = selected_spec.artifacts.serving_runtime_image
    if serving_image is None:
        raise GcpPlanError("GCP dry-run requires a serving-runtime image")
    payload = GcpDryRunPlanPayload(
        schema_version=GCP_PLAN_SCHEMA_VERSION,
        plan_kind="gcp_dry_run_reference",
        deployment_spec_digest=deployment_spec_digest(selected_spec),
        inventory_digest=gcp_inventory_digest(selected_inventory),
        planning_scope=GcpPlanScope(
            compute_project_id=selected_context.compute_project_id,
            region=selected_context.region,
        ),
        adapter=GcpPlanAdapterIdentity(
            adapter_id=GCP_ADAPTER_ID, adapter_version=GCP_ADAPTER_VERSION
        ),
        runtime=GcpPlanRuntime(
            adapter_id=GCP_RUNTIME_ADAPTER_ID,
            adapter_version=GCP_RUNTIME_ADAPTER_VERSION,
            engine="vllm",
            engine_version="0.26.0",
            model_id=runtime.model_id,
            model_revision=runtime.model_revision,
            tokenizer_revision=runtime.tokenizer_revision,
            endpoint_scheme=runtime.endpoint.scheme,
            endpoint_port=runtime.endpoint.port,
            endpoint_path=runtime.endpoint.path,
            endpoint_visibility="private",
            endpoint_scope="private",
            runner_runtime_colocation="colocated",
        ),
        runner_image=_image(selected_spec.artifacts.runner_image),
        serving_runtime_image=_image(serving_image),
        selection=GcpPlanSelection(
            requested_resources=selected_spec.resources,
            selected_provider=selected,
        ),
        timeouts=selected_spec.timeouts,
        cleanup_policy=selected_spec.cleanup_policy,
        cost_ceiling=selected_spec.cost_ceiling,
        execution_authorized=False,
        provider_mutation_performed=False,
        credentials_resolved=False,
        capacity_proven=False,
        pricing_proven=False,
        evidence_eligible=False,
        pricing_status="not_evaluated_pricing_unavailable",
        invoice_truth="unavailable_external_provider_invoice",
    )
    return GcpDryRunPlan(**_payload_value(payload), plan_id=gcp_plan_id(payload))


def verify_gcp_dry_run_plan(
    plan: GcpDryRunPlan,
    *,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
) -> GcpDryRunPlan:
    """Verify plan identity and exact cross-input binding."""

    expected = plan_gcp_dry_run(expected_spec, expected_inventory, expected_context)
    if canonical_gcp_plan_bytes(plan) != canonical_gcp_plan_bytes(expected):
        raise GcpPlanError("GCP plan does not match its deployment inputs")
    return plan


def verify_gcp_dry_run_plan_bytes(
    payload: bytes,
    *,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
) -> GcpDryRunPlan:
    plan = parse_gcp_plan_json(payload)
    if canonical_gcp_plan_bytes(plan) != payload:
        raise GcpPlanError("GCP plan bytes are not canonical")
    return verify_gcp_dry_run_plan(
        plan,
        expected_spec=expected_spec,
        expected_inventory=expected_inventory,
        expected_context=expected_context,
    )


def publish_gcp_dry_run_plan(
    *,
    root: Path,
    plan: GcpDryRunPlan,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
) -> PublishedGcpPlan:
    """Validate all semantic inputs before creating a publication directory."""

    try:
        expected = plan_gcp_dry_run(expected_spec, expected_inventory, expected_context)
        raw = canonical_gcp_plan_bytes(plan)
        parsed = parse_gcp_plan_json(raw)
        if raw != canonical_gcp_plan_bytes(parsed) or raw != canonical_gcp_plan_bytes(
            expected
        ):
            raise GcpPlanPublicationError("GCP plan failed publication validation")
        destination = publish_immutable_directory(
            root=root, artifact_id=plan.plan_id, filename=GCP_PLAN_FILENAME, content=raw
        )
        read_back = destination / GCP_PLAN_FILENAME
        if read_back.is_symlink() or read_back.read_bytes() != raw:
            raise GcpPlanPublicationError("GCP plan publication read-back failed")
        return PublishedGcpPlan(
            path=destination,
            plan=parsed,
            plan_sha256=sha256_digest(raw),
        )
    except GcpPlanPublicationError:
        raise
    except (OSError, TypeError, ValueError, ValidationError):
        raise GcpPlanPublicationError("GCP plan publication failed closed") from None


def verify_published_gcp_dry_run_plan(
    path: Path,
    *,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
) -> GcpDryRunPlan:
    try:
        if path.is_symlink() or not path.is_dir():
            raise GcpPlanPublicationError("published GCP plan path is unsafe")
        plan_path = path / GCP_PLAN_FILENAME
        if plan_path.is_symlink() or not plan_path.is_file():
            raise GcpPlanPublicationError("published GCP plan file is unsafe")
        return verify_gcp_dry_run_plan_bytes(
            plan_path.read_bytes(),
            expected_spec=expected_spec,
            expected_inventory=expected_inventory,
            expected_context=expected_context,
        )
    except GcpPlanPublicationError:
        raise
    except (OSError, ValueError, ValidationError):
        raise GcpPlanPublicationError(
            "published GCP plan verification failed"
        ) from None


def gcp_inventory_schema() -> dict[str, Any]:
    generated = deepcopy(
        GcpInventorySnapshot.model_json_schema(ref_template="#/$defs/{model}")
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": GCP_INVENTORY_SCHEMA_ID,
        "$comment": (
            "Offline bounded inventory only; no provider capacity or credential truth."
        ),
        **generated,
    }


def gcp_plan_schema() -> dict[str, Any]:
    generated = deepcopy(
        GcpDryRunPlan.model_json_schema(ref_template="#/$defs/{model}")
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": GCP_PLAN_SCHEMA_ID,
        "$comment": (
            "Non-executing GCP plan; never a lifecycle outcome, receipt, or evidence."
        ),
        **generated,
    }
