"""Guarded, injected GCP Compute lifecycle boundary.

This module is the execution boundary for the GCP provider.  It is deliberately
separate from :mod:`inferdrome.deployment.gcp`: the latter only plans from an
offline catalog and its ``execution_authorized=False`` value is never treated as
permission to mutate a provider.

The controller has no Google SDK, socket, subprocess, metadata-server, or
credential-discovery dependency.  A concrete optional Compute transport is
implemented in :mod:`inferdrome.deployment.gcp_compute_transport`; all normal
tests and local installs use the protocol and deterministic fake below.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
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

from inferdrome.deployment.gcp import (
    GcpDryRunPlan,
    GcpInventorySnapshot,
    GcpPlanError,
    GcpPlanImage,
    GcpPlanningContext,
    GcpPlanProvider,
    GcpPlanScope,
    GcpProjectId,
    GcpRegion,
    GcpResourceName,
    GcpTimestamp,
    GcpZone,
    canonical_gcp_inventory_bytes,
    canonical_gcp_plan_bytes,
    gcp_inventory_digest,
    gcp_plan_id,
    gcp_plan_sha256,
    parse_gcp_inventory_json,
    parse_gcp_plan_json,
    verify_gcp_dry_run_plan,
)
from inferdrome.deployment.spec import (
    CostCeiling,
    DeploymentSpec,
    canonical_deployment_spec_bytes,
    deployment_spec_digest,
    parse_deployment_spec_json,
)
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest, sha256_digest

GCP_EXECUTION_ARM_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-arm.v1"
GCP_EXECUTION_ARM_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-arm:v1"
GCP_EXECUTION_RESULT_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-result.v1"
GCP_EXECUTION_RESULT_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-result:v1"
GCP_EXECUTION_ANCHOR_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-intent-anchor:v1"
GCP_EXECUTION_EVENT_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-journal-event.v1"
GCP_EXECUTION_EVENT_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-journal-event:v1"
GCP_EXECUTION_JOURNAL_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-lease.v1"
GCP_EXECUTION_REQUEST_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-request.v1"
GCP_EXECUTION_QUOTE_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-quote.v1"
GCP_EXECUTION_ANCHOR_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-intent-anchor.v1"
GCP_EXECUTION_ADAPTER_ID: Final = "inferdrome.provider.gcp.compute_guarded"
GCP_EXECUTION_ADAPTER_VERSION: Final = "1.0.0"
GCP_EXECUTION_TRANSPORT_ID: Final = "inferdrome.transport.gcp.compute"
GCP_EXECUTION_TRANSPORT_VERSION: Final = "1.0.0"
GCP_MAX_EXECUTION_BYTES: Final = 524_288
GCP_MAX_JOURNAL_BYTES: Final = 262_144
GCP_MAX_JOURNAL_EVENT_BYTES: Final = 8_388_608
ARM_CONFIRMATION: Final = "EXECUTE_GCP_ONCE"
RECOVERY_CONFIRMATION: Final = "RECOVER_GCP_LEASE"

_TIMESTAMP_RE = re.compile(
    r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_SAFE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROLLER_RE = re.compile(r"^ctl-[a-z0-9]{8,24}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_QUOTE_RE = re.compile(r"^quote-[a-z0-9]{8,32}$")
_INSTANCE_RE = re.compile(r"^inferdrome-ctl-[a-z0-9]{8,24}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LABEL_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_NETWORK_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/networks/[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_SUBNETWORK_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/regions/[a-z]+-[a-z]+[0-9]{1,2}/subnetworks/[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_BOOT_IMAGE_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/[0-9]+$"
)
_SERVICE_ACCOUNT_RE = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credential",
        "credential_value",
        "password",
        "private_key",
        "secret",
        "secret_value",
        "token",
    }
)
_SENSITIVE_NORMALIZED = frozenset(
    re.sub(r"[^a-z0-9]", "", key) for key in _SENSITIVE_KEYS
)
_CREDENTIAL_SHAPES = (
    re.compile(r"^(?:sk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^(?:gh[pousr]_)[A-Za-z0-9_]{16,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{16,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----$"),
    re.compile(r"^[A-Za-z0-9_-]{40,}$"),
)
_IDENTITY_KEYS = frozenset(
    {
        "armid",
        "controllerid",
        "deploymentdigest",
        "deployment_spec_digest",
        "digest",
        "inventorydigest",
        "inventory_digest",
        "modelrevision",
        "model_revision",
        "nonce",
        "planid",
        "plan_id",
        "plan_sha256",
        "pricingstatus",
        "requestdigest",
        "request_digest",
        "sha256",
        "tokenizerrevision",
        "tokenizer_revision",
    }
)


class GcpExecutionError(ValueError):
    """A bounded execution-contract or controller error."""


class GcpArmError(GcpExecutionError):
    """The one-shot arm is invalid, stale, substituted, or already consumed."""


class GcpJournalError(GcpExecutionError):
    """The local ownership journal cannot be trusted or updated."""


class GcpTransportError(GcpExecutionError):
    """A sanitized provider transport error represented only by a bounded code."""

    def __init__(
        self,
        code: str,
        *,
        operation: GcpOperationHandle | None = None,
        ambiguous: bool = False,
    ) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", code):
            code = "TRANSPORT_ERROR"
        self.code = code
        self.operation = operation
        self.ambiguous = ambiguous
        super().__init__(code)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_credential(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _CREDENTIAL_SHAPES)


def _compact_label(prefix: str, value: str) -> str:
    """Return a provider-safe ownership key with an explicit collision domain.

    The full digest remains in the local request/lease.  Provider labels are
    only an index; exact full-value comparison is required before deletion.
    """

    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,7}", prefix):
        raise ValueError("invalid GCP label prefix")
    compact = sha256_digest(value.encode("utf-8"))[len("sha256:") :][:48]
    result = f"{prefix}_{compact}"
    if _LABEL_VALUE_RE.fullmatch(result) is None:
        raise ValueError("invalid compact GCP label")
    return result


def _stable_request_uuid(arm_id: str, controller_id: str, action: str) -> str:
    namespace = uuid.UUID("e4dbd74e-0df1-4cd6-8e86-4f789d8e3b65")
    value = uuid.uuid5(namespace, f"inferdrome:gcp:{arm_id}:{controller_id}:{action}")
    if value.int == 0:
        raise ValueError("request UUID cannot be zero")
    return str(value)


def _reject_unsafe_values(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or _SAFE_KEY_RE.fullmatch(key) is None:
                raise ValueError("GCP execution object keys are invalid")
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_NORMALIZED:
                raise ValueError("GCP execution contains forbidden credential fields")
            _reject_unsafe_values(child, path=(*path, normalized))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_unsafe_values(child, path=path)
    elif isinstance(value, str):
        if re.fullmatch(r"(?:https?|ssh|ftp)://.*", value, flags=re.IGNORECASE):
            raise ValueError("GCP execution contains a public endpoint value")
        if _looks_credential(value) and (not path or path[-1] not in _IDENTITY_KEYS):
            raise ValueError("GCP execution contains a credential-shaped value")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("GCP execution JSON object keys must be unique")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise ValueError("GCP execution JSON contains a non-finite number")


def _preflight_json(payload: str | bytes | bytearray, *, kind: str) -> bytes:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError(f"{kind} input is invalid")
    if len(raw) > GCP_MAX_EXECUTION_BYTES:
        raise ValueError(f"{kind} exceeds its bound")
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(f"{kind} is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise ValueError(f"{kind} root must be an object")
    _reject_unsafe_values(decoded)
    return raw


def _parse_timestamp(value: str) -> datetime:
    if _TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("timestamp must use canonical UTC form")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("timestamp is not a real UTC time") from None
    return parsed


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock time must be timezone-aware")
    # Provider and journal timestamps are deliberately second precision.  A
    # real system clock has microseconds; truncation at this contract boundary
    # keeps the default clock usable while retaining one canonical spelling.
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


class GcpExecutionModel(FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

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
        _preflight_json(json_data, kind=cls.__name__)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


GcpControllerId = Annotated[str, StringConstraints(pattern=_CONTROLLER_RE.pattern)]
GcpNonce = Annotated[str, StringConstraints(pattern=_NONCE_RE.pattern)]
GcpQuoteId = Annotated[str, StringConstraints(pattern=_QUOTE_RE.pattern)]
GcpInstanceName = Annotated[str, StringConstraints(pattern=_INSTANCE_RE.pattern)]
GcpRequestUuid = Annotated[str, StringConstraints(pattern=_UUID_RE.pattern)]
GcpLabelValue = Annotated[str, StringConstraints(pattern=_LABEL_VALUE_RE.pattern)]
GcpPrivateNetworkRef = Annotated[str, StringConstraints(pattern=_NETWORK_RE.pattern)]
GcpPrivateSubnetworkRef = Annotated[
    str, StringConstraints(pattern=_SUBNETWORK_RE.pattern)
]
GcpServiceAccountRef = Annotated[
    str, StringConstraints(pattern=_SERVICE_ACCOUNT_RE.pattern)
]
GcpMicroUsd = Annotated[int, Field(strict=True, ge=0, le=10**12)]
GcpLeaseState = Literal[
    "PREPARED",
    "ARM_CONSUMED",
    "CREATE_SUBMITTED",
    "OWNED",
    "CLEANUP_PENDING",
    "CLEANUP_CONFIRMED",
    "ORPHANED",
    "BLOCKED",
]
GcpTracePhase = Literal[
    "VALIDATION",
    "JOURNAL_PREPARE",
    "ARM_CONSUME",
    "CREATE",
    "OPERATION_WAIT",
    "OWNERSHIP_VERIFY",
    "WORK",
    "DELETE",
    "FINAL_CONFIRMATION",
]
GcpTraceStatus = Literal["SUCCEEDED", "FAILED", "SKIPPED"]


class GcpExecutionArmPayload(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-arm.v1"]
    arm_kind: Literal["one_shot_operator_capability"]
    plan_id: Sha256Digest
    plan_sha256: Sha256Digest
    deployment_spec_digest: Sha256Digest
    inventory_digest: Sha256Digest
    planning_scope: GcpPlanScope
    selected_provider: GcpPlanProvider
    controller_id: GcpControllerId
    nonce: GcpNonce
    issued_at: GcpTimestamp
    expires_at: GcpTimestamp
    max_controller_duration_seconds: int = Field(strict=True, ge=1, le=86_400)
    max_provider_lifetime_seconds: int = Field(strict=True, ge=1, le=86_400)
    cost_ceiling: CostCeiling
    authorization_granted: Literal[True]
    provider_mutation_performed: Literal[False]
    credentials_resolved: Literal[False]
    evidence_eligible: Literal[False]
    invoice_truth: Literal["unavailable_external_provider_invoice"]

    @model_validator(mode="after")
    def validate_time_and_scope(self) -> Self:
        issued = _parse_timestamp(self.issued_at)
        expires = _parse_timestamp(self.expires_at)
        if expires <= issued:
            raise ValueError("execution arm expiry must be after issuance")
        duration = int((expires - issued).total_seconds())
        if duration != self.max_controller_duration_seconds:
            raise ValueError("execution arm duration does not match expiry")
        if self.max_provider_lifetime_seconds != self.max_controller_duration_seconds:
            raise ValueError("provider lifetime must match controller duration")
        return self


class GcpExecutionArm(GcpExecutionArmPayload):
    arm_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.arm_id != gcp_execution_arm_id(self):
            raise ValueError("execution arm identity does not match its payload")
        return self


class GcpExecutionNetwork(GcpExecutionModel):
    network: GcpPrivateNetworkRef
    subnetwork: GcpPrivateSubnetworkRef
    external_access_config: Literal["absent"]
    ip_forwarding: Literal[False]


class GcpExecutionEnvironment(GcpExecutionModel):
    """Exact provider-side environment, with references but no secret values."""

    schema_version: Literal["inferdrome.gcp-execution-environment.v1"]
    boot_image: GcpPlanImage
    runner_image: GcpPlanImage
    serving_runtime_image: GcpPlanImage
    network: GcpExecutionNetwork
    service_account: GcpServiceAccountRef
    service_account_scopes: tuple[
        Literal["logging.write", "monitoring.write", "trace.append"], ...
    ] = Field(min_length=1, max_length=3)
    boot_disk_size_gib: int = Field(strict=True, ge=10, le=16_384)
    boot_disk_type: Literal["pd-balanced", "pd-ssd"]
    architecture: Literal["amd64"]
    deletion_protection: Literal[False]
    automatic_restart: Literal[False]
    maintenance_policy: Literal["TERMINATE"]
    startup_script_digest: Sha256Digest | None

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        if _BOOT_IMAGE_RE.fullmatch(self.boot_image.repository) is None:
            raise ValueError("boot image must use an immutable numeric image identity")
        if len(set(self.service_account_scopes)) != len(self.service_account_scopes):
            raise ValueError("service-account scopes must be unique")
        if "cloud-platform" in self.service_account_scopes:
            raise ValueError("broad cloud-platform scope is forbidden")
        return self


class GcpExecutionLabels(GcpExecutionModel):
    inferdrome: Literal["inferdrome"]
    controller_id: GcpLabelValue
    plan_id: GcpLabelValue
    arm_id: GcpLabelValue
    managed_by: Literal["inferdrome_gcp_execution_v1"]
    role: Literal["provider-envelope"]


class GcpInsertRequest(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-request.v1"]
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: GcpInstanceName
    machine_type: GcpResourceName
    architecture: Literal["amd64"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: GcpResourceName
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    boot_image: GcpPlanImage
    runner_image: GcpPlanImage
    serving_runtime_image: GcpPlanImage
    boot_disk_size_gib: int = Field(strict=True, ge=10, le=16_384)
    boot_disk_type: Literal["pd-balanced", "pd-ssd"]
    boot_disk_auto_delete: Literal[True]
    network: GcpExecutionNetwork
    service_account: GcpServiceAccountRef
    service_account_scopes: tuple[
        Literal["logging.write", "monitoring.write", "trace.append"], ...
    ] = Field(min_length=1, max_length=3)
    deletion_protection: Literal[False]
    automatic_restart: Literal[False]
    maintenance_policy: Literal["TERMINATE"]
    provider_max_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    plan_id: Sha256Digest
    arm_id: Sha256Digest
    controller_id: GcpControllerId
    insert_request_id: GcpRequestUuid
    delete_request_id: GcpRequestUuid
    labels: GcpExecutionLabels
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tokenizer_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    runtime_engine: Literal["vllm"]
    runtime_version: Literal["0.26.0"]
    endpoint_scope: Literal["private"]
    runner_runtime_colocation: Literal["colocated"]
    startup_script_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def validate_private_request(self) -> Self:
        if self.network.external_access_config != "absent":
            raise ValueError("external access configuration must be absent")
        if self.network.ip_forwarding:
            raise ValueError("IP forwarding must be disabled")
        if self.network.network.split("/")[1] != self.project_id:
            raise ValueError("network project must match compute project")
        if self.network.subnetwork.split("/")[1] != self.project_id:
            raise ValueError("subnetwork project must match compute project")
        if self.network.subnetwork.split("/")[3] != self.region:
            raise ValueError("subnetwork region must match compute region")
        if self.labels.controller_id != _compact_label("c", self.controller_id):
            raise ValueError("controller ownership label does not match request")
        if self.labels.plan_id != _compact_label("p", self.plan_id):
            raise ValueError("plan ownership label does not match request")
        if self.labels.arm_id != _compact_label("a", self.arm_id):
            raise ValueError("arm ownership label does not match request")
        for action, request_id in (
            ("insert", self.insert_request_id),
            ("delete", self.delete_request_id),
        ):
            try:
                parsed = uuid.UUID(request_id)
            except ValueError:
                raise ValueError(f"{action} request ID is invalid") from None
            if parsed.int == 0:
                raise ValueError(f"{action} request ID cannot be zero")
        return self


class GcpQuoteComponent(GcpExecutionModel):
    component_id: Literal["compute", "gpu", "boot_disk", "network"]
    maximum_microusd: GcpMicroUsd


class GcpCostQuote(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-quote.v1"]
    quote_id: GcpQuoteId
    plan_id: Sha256Digest
    controller_id: GcpControllerId
    request_digest: Sha256Digest
    environment_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: GcpResourceName
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: GcpResourceName
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    network: GcpPrivateNetworkRef
    subnetwork: GcpPrivateSubnetworkRef
    boot_image_digest: Sha256Digest
    runner_image_digest: Sha256Digest
    serving_runtime_image_digest: Sha256Digest
    boot_disk_size_gib: int = Field(strict=True, ge=10, le=16_384)
    boot_disk_type: Literal["pd-balanced", "pd-ssd"]
    service_account: GcpServiceAccountRef
    provider_max_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    billable_duration_seconds: int = Field(strict=True, ge=1, le=86_400)
    issued_at: GcpTimestamp
    valid_until: GcpTimestamp
    freshness_seconds: int = Field(strict=True, ge=1, le=86_400)
    currency: Literal["USD"]
    components: tuple[GcpQuoteComponent, ...] = Field(min_length=4, max_length=4)
    safety_margin_microusd: GcpMicroUsd
    complete: Literal[True]
    pricing_proven: Literal[False]
    pricing_status: Literal["operator_supplied_estimate_not_invoice_truth"]
    invoice_truth: Literal["unavailable_external_provider_invoice"]

    @model_validator(mode="after")
    def validate_quote(self) -> Self:
        identifiers = [component.component_id for component in self.components]
        if set(identifiers) != {"compute", "gpu", "boot_disk", "network"}:
            raise ValueError("quote must cover each declared billable component once")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("quote components must be unique")
        issued = _parse_timestamp(self.issued_at)
        valid_until = _parse_timestamp(self.valid_until)
        if valid_until <= issued:
            raise ValueError("quote validity must be positive")
        if int((valid_until - issued).total_seconds()) > self.freshness_seconds:
            raise ValueError("quote validity exceeds its freshness bound")
        return self

    @property
    def worst_case_microusd(self) -> int:
        return (
            sum(component.maximum_microusd for component in self.components)
            + self.safety_margin_microusd
        )


class GcpCapacityInput(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-capacity.v1"]
    plan_id: Sha256Digest
    request_digest: Sha256Digest
    source: Literal["operator_supplied_read_only_observation"]
    observed_at: GcpTimestamp
    freshness_seconds: int = Field(strict=True, ge=1, le=86_400)
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: GcpResourceName
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    capacity_status: Literal["operator_supplied_eligible", "unknown"]
    matching_active_resources: int = Field(strict=True, ge=0, le=1_000_000)
    capacity_proven: Literal[False]


class GcpOperationHandle(GcpExecutionModel):
    operation_id: Annotated[str, StringConstraints(pattern=r"^op-[a-z0-9]{8,48}$")]
    operation_kind: Literal["insert", "delete"]
    operation_name: Annotated[
        str | None, StringConstraints(min_length=1, max_length=256)
    ] = None
    project_id: GcpProjectId | None = None
    zone: GcpZone | None = None


class GcpOperationResult(GcpExecutionModel):
    status: Literal["DONE", "ERROR", "TIMEOUT"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^op-[a-z0-9]{8,48}$")]
    instance_name: GcpInstanceName | None
    operation_name: Annotated[
        str | None, StringConstraints(min_length=1, max_length=256)
    ] = None
    error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.status == "DONE" and self.error_code is not None:
            raise ValueError("successful operation cannot contain an error")
        if self.status == "ERROR" and self.error_code is None:
            raise ValueError("failed operation requires a bounded error code")
        return self


class GcpInstanceObservation(GcpExecutionModel):
    instance_name: GcpInstanceName
    project_id: GcpProjectId
    zone: GcpZone
    machine_type: GcpResourceName
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: GcpResourceName | None = None
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    state: Literal["RUNNING", "TERMINATED", "NOT_FOUND"]
    labels: GcpExecutionLabels | None
    external_access_config: Literal["absent"]
    ip_forwarding: Literal[False]
    network: GcpPrivateNetworkRef | None = None
    subnetwork: GcpPrivateSubnetworkRef | None = None
    private_ipv4_addresses: tuple[str, ...] = Field(default=(), max_length=8)
    external_ipv6_present: Literal[False] = False

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.state != "NOT_FOUND" and (
            self.accelerator_provider_type is None
            or self.network is None
            or self.subnetwork is None
        ):
            raise ValueError("provider observation is incomplete")
        if self.external_ipv6_present or self.ip_forwarding:
            raise ValueError("provider observation is not private")
        return self


class GcpComputeTransport(Protocol):
    """Narrow transport surface used only after all pure gates pass."""

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle: ...

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult: ...

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation: ...

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]: ...

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle: ...


@dataclass(frozen=True)
class GcpClock:
    """Injected UTC/monotonic clock; no provider interaction."""

    now_fn: Callable[[], datetime]
    monotonic_fn: Callable[[], float]

    def now(self) -> datetime:
        return self.now_fn()

    def monotonic(self) -> float:
        return self.monotonic_fn()


def system_gcp_clock() -> GcpClock:
    import time

    return GcpClock(
        now_fn=lambda: datetime.now(UTC).replace(microsecond=0),
        monotonic_fn=time.monotonic,
    )


class ExecutionArmStore(Protocol):
    def consume(self, arm_id: Sha256Digest, arm_sha256: Sha256Digest) -> bool: ...


class InMemoryExecutionArmStore:
    """Thread-safe one-shot store for local tests and an injected controller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consumed: set[tuple[str, str]] = set()

    def consume(self, arm_id: Sha256Digest, arm_sha256: Sha256Digest) -> bool:
        key = (arm_id, arm_sha256)
        with self._lock:
            if key in self._consumed:
                return False
            self._consumed.add(key)
            return True


class FileExecutionArmStore:
    """Crash-safe one-shot arm consumption for real controller processes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpArmError("arm store path must be absolute")
        try:
            metadata = self.root.lstat()
        except OSError:
            raise GcpArmError("arm store directory is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GcpArmError("arm store directory is unsafe")
        return self.root

    @staticmethod
    def _marker(arm_id: str, arm_sha256: str) -> str:
        if not (
            re.fullmatch(r"sha256:[0-9a-f]{64}", arm_id)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", arm_sha256)
        ):
            raise GcpArmError("arm identity is invalid")
        token = sha256_digest(f"{arm_id}\0{arm_sha256}".encode())[len("sha256:") :]
        return f"arm-{token}.consumed"

    @contextmanager
    def _exclusive(self, root: Path) -> Any:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / ".arm.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpArmError("arm store locking is unavailable") from None
            yield
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def consume(self, arm_id: Sha256Digest, arm_sha256: Sha256Digest) -> bool:
        root = self._checked_root()
        with self._exclusive(root):
            return self._consume_locked(arm_id, arm_sha256)

    def _consume_locked(self, arm_id: Sha256Digest, arm_sha256: Sha256Digest) -> bool:
        root = self.root
        marker = root / self._marker(arm_id, arm_sha256)
        content = canonical_json_bytes({"arm_id": arm_id, "arm_sha256": arm_sha256})
        try:
            descriptor = os.open(
                marker,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        except FileExistsError:
            read_descriptor: int | None = None
            try:
                read_descriptor = os.open(
                    marker,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                existing = os.read(read_descriptor, len(content) + 1)
                if existing != content:
                    raise GcpArmError("arm consumption marker collision")
                return False
            except GcpArmError:
                raise
            except OSError:
                raise GcpArmError("arm consumption marker is unsafe") from None
            finally:
                if read_descriptor is not None:
                    os.close(read_descriptor)
        except OSError:
            raise GcpArmError("arm consumption failed") from None


class GcpExecutionTraceEvent(GcpExecutionModel):
    sequence: int = Field(strict=True, ge=0, le=255)
    phase: GcpTracePhase
    status: GcpTraceStatus
    error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None


class GcpLeaseRecord(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-lease.v1"]
    state: GcpLeaseState
    controller_id: GcpControllerId
    arm_id: Sha256Digest
    plan_id: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: GcpInstanceName
    request: GcpInsertRequest
    request_digest: Sha256Digest
    environment_digest: Sha256Digest
    quote_digest: Sha256Digest
    capacity_digest: Sha256Digest
    estimated_max_microusd: GcpMicroUsd
    declared_cost_ceiling_usd: Annotated[
        str, StringConstraints(min_length=1, max_length=32)
    ]
    estimate_basis: Literal["operator_supplied_fixed_point_quote"]
    labels: GcpExecutionLabels
    intent_anchor_digest: Sha256Digest
    created_at: GcpTimestamp
    updated_at: GcpTimestamp
    provider_operation_id: Annotated[
        str | None, StringConstraints(pattern=r"^op-[a-z0-9]{8,48}$")
    ] = None
    provider_operation_name: Annotated[
        str | None, StringConstraints(min_length=1, max_length=256)
    ] = None
    provider_operation_kind: Literal["insert", "delete"] | None = None
    provider_operation_status: (
        Literal["PENDING", "DONE", "ERROR", "TIMEOUT", "UNKNOWN"] | None
    ) = None
    provider_operation_terminal: bool = False
    provider_mutation_ambiguous: bool = False
    arm_consumed: bool = False
    provider_mutation_attempted: bool = False
    cleanup_confirmed: bool = False
    orphaned: bool = False
    delete_attempts: int = Field(strict=True, ge=0, le=3)
    max_cleanup_attempts: int = Field(strict=True, ge=1, le=3)
    cleanup_timeout_seconds: int = Field(strict=True, ge=1, le=3_600)
    last_error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.request_digest != gcp_execution_request_digest(self.request):
            raise ValueError("lease request digest does not match its exact request")
        if (
            self.request.project_id,
            self.request.region,
            self.request.zone,
            self.request.instance_name,
            self.request.labels,
        ) != (
            self.project_id,
            self.region,
            self.zone,
            self.instance_name,
            self.labels,
        ):
            raise ValueError("lease request ownership binding is inconsistent")
        if self.max_cleanup_attempts < self.delete_attempts:
            raise ValueError("lease cleanup attempt bound is inconsistent")
        if self.state == "CLEANUP_CONFIRMED" and (
            not self.cleanup_confirmed or self.orphaned
        ):
            raise ValueError("confirmed lease state is inconsistent")
        if self.orphaned and self.cleanup_confirmed:
            raise ValueError("orphaned lease cannot be confirmed")
        if self.provider_mutation_attempted and not self.arm_consumed:
            raise ValueError("provider mutation requires an consumed arm")
        if self.provider_operation_terminal and self.provider_operation_status not in {
            "DONE",
            "ERROR",
        }:
            raise ValueError("terminal provider operation status is inconsistent")
        if self.provider_operation_id is None and (
            self.provider_operation_name is not None
            or self.provider_operation_kind is not None
            or self.provider_operation_status not in {None, "UNKNOWN"}
            or self.provider_operation_terminal
        ):
            raise ValueError("provider operation identity is incomplete")
        if (
            self.provider_operation_status == "UNKNOWN"
            and not self.provider_mutation_ambiguous
        ):
            raise ValueError("unknown provider operation requires ambiguity state")
        if self.provider_mutation_ambiguous and self.cleanup_confirmed:
            raise ValueError("ambiguous provider mutation cannot be cleanup confirmed")
        return self


class GcpLeaseIntentAnchor(GcpExecutionModel):
    """Immutable local intent binding used to protect crash recovery."""

    schema_version: Literal["inferdrome.gcp-execution-intent-anchor.v1"]
    controller_id: GcpControllerId
    arm_id: Sha256Digest
    plan_id: Sha256Digest
    request_digest: Sha256Digest
    request: GcpInsertRequest
    labels: GcpExecutionLabels
    anchor_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_anchor(self) -> Self:
        value = _json_value(self)
        value.pop("anchor_digest", None)
        if self.anchor_digest != digest_bytes(
            DigestDomain.GCP_EXECUTION_JOURNAL, canonical_json_bytes(value)
        ):
            raise ValueError("journal intent anchor identity is invalid")
        if self.request_digest != gcp_execution_request_digest(self.request):
            raise ValueError("journal intent request identity is invalid")
        if (
            self.controller_id != self.request.controller_id
            or self.arm_id != self.request.arm_id
            or self.plan_id != self.request.plan_id
            or self.labels != self.request.labels
        ):
            raise ValueError("journal intent ownership is inconsistent")
        return self


class GcpLeaseJournalEvent(GcpExecutionModel):
    """Append-only hash-chain entry for a persisted controller transition."""

    schema_version: Literal["inferdrome.gcp-execution-journal-event.v1"]
    controller_id: GcpControllerId
    sequence: int = Field(strict=True, ge=0, le=4_096)
    previous_event_digest: Sha256Digest | None
    record_sha256: Sha256Digest
    record: GcpLeaseRecord
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        record_raw = canonical_json_bytes(_json_value(self.record))
        if self.record_sha256 != sha256_digest(record_raw):
            raise ValueError("journal event record digest is invalid")
        if self.controller_id != self.record.controller_id:
            raise ValueError("journal event controller binding is invalid")
        value = _json_value(self)
        value.pop("event_digest", None)
        expected = digest_bytes(
            DigestDomain.GCP_EXECUTION_JOURNAL, canonical_json_bytes(value)
        )
        if self.event_digest != expected:
            raise ValueError("journal event identity is invalid")
        return self


class GcpExecutionOutcome(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-result.v1"]
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "BLOCKED"]
    plan_id: Sha256Digest
    controller_id: GcpControllerId
    arm_id: Sha256Digest
    request_digest: Sha256Digest
    environment_digest: Sha256Digest
    quote_digest: Sha256Digest
    capacity_digest: Sha256Digest
    estimated_max_microusd: GcpMicroUsd
    declared_cost_ceiling_usd: Annotated[
        str, StringConstraints(min_length=1, max_length=32)
    ]
    estimate_basis: Literal["operator_supplied_fixed_point_quote"]
    arm_consumed: bool
    provider_mutation_attempted: bool
    cleanup_confirmed: bool
    orphaned: bool
    evidence_eligible: Literal[False]
    invoice_truth: Literal["unavailable_external_provider_invoice"]
    error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None
    primary_error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None
    cleanup_error_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None
    journal_state: GcpLeaseState
    trace: tuple[GcpExecutionTraceEvent, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (
            self.cleanup_error_code is not None
            and self.error_code != self.cleanup_error_code
        ):
            raise ValueError("cleanup error must dominate terminal error")
        if self.cleanup_confirmed and self.orphaned:
            raise ValueError("cleanup cannot be confirmed for an orphan")
        if self.status == "SUCCEEDED" and (
            self.primary_error_code is not None
            or self.cleanup_error_code is not None
            or not self.cleanup_confirmed
            or self.provider_mutation_attempted is False
        ):
            raise ValueError("success outcome is inconsistent")
        return self


@dataclass(frozen=True)
class GcpExecutionPreflight:
    arm: GcpExecutionArm
    request: GcpInsertRequest
    quote: GcpCostQuote
    capacity: GcpCapacityInput
    arm_sha256: Sha256Digest


def _json_value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("GCP execution contract must serialize as an object")
    return value


def _strict_model(
    model: GcpExecutionModel, cls: type[GcpExecutionModel]
) -> GcpExecutionModel:
    raw = canonical_json_bytes(_json_value(model))
    parsed = cls.model_validate_json(raw)
    if canonical_json_bytes(_json_value(parsed)) != raw:
        raise GcpExecutionError(
            "GCP execution model is not canonically self-consistent"
        )
    return parsed


def canonical_gcp_execution_arm_payload_bytes(
    arm: GcpExecutionArm | GcpExecutionArmPayload,
) -> bytes:
    value = _json_value(arm)
    value.pop("arm_id", None)
    return canonical_json_bytes(value)


def gcp_execution_arm_id(arm: GcpExecutionArm | GcpExecutionArmPayload) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_ARM, canonical_gcp_execution_arm_payload_bytes(arm)
    )


def canonical_gcp_execution_arm_bytes(arm: GcpExecutionArm) -> bytes:
    return canonical_json_bytes(_json_value(arm))


def gcp_execution_arm_sha256(arm: GcpExecutionArm) -> Sha256Digest:
    return sha256_digest(canonical_gcp_execution_arm_bytes(arm))


def parse_gcp_execution_arm_json(payload: str | bytes) -> GcpExecutionArm:
    _preflight_json(payload, kind="GCP execution arm")
    return GcpExecutionArm.model_validate_json(payload)


def _strict_spec_input(spec: DeploymentSpec) -> DeploymentSpec:
    return parse_deployment_spec_json(canonical_deployment_spec_bytes(spec))


def _strict_inventory_input(snapshot: GcpInventorySnapshot) -> GcpInventorySnapshot:
    return parse_gcp_inventory_json(canonical_gcp_inventory_bytes(snapshot))


def _strict_context_input(context: GcpPlanningContext) -> GcpPlanningContext:
    return GcpPlanningContext.model_validate_json(
        canonical_json_bytes(context.model_dump(mode="json"))
    )


def _strict_plan_input(plan: GcpDryRunPlan) -> GcpDryRunPlan:
    return parse_gcp_plan_json(canonical_gcp_plan_bytes(plan))


def canonical_gcp_execution_request_bytes(request: GcpInsertRequest) -> bytes:
    value = _json_value(request)
    value["service_account_scopes"] = sorted(value["service_account_scopes"])
    return canonical_json_bytes(value)


def gcp_execution_request_digest(request: GcpInsertRequest) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_REQUEST,
        canonical_gcp_execution_request_bytes(request),
    )


def _environment_binding_digest(request: GcpInsertRequest) -> Sha256Digest:
    value = {
        "boot_image": request.boot_image.model_dump(mode="json"),
        "runner_image": request.runner_image.model_dump(mode="json"),
        "serving_runtime_image": request.serving_runtime_image.model_dump(mode="json"),
        "network": request.network.model_dump(mode="json"),
        "service_account": request.service_account,
        "service_account_scopes": sorted(request.service_account_scopes),
        "boot_disk_size_gib": request.boot_disk_size_gib,
        "boot_disk_type": request.boot_disk_type,
        "architecture": request.architecture,
        "deletion_protection": request.deletion_protection,
        "automatic_restart": request.automatic_restart,
        "maintenance_policy": request.maintenance_policy,
        "startup_script_digest": request.startup_script_digest,
    }
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_ENVIRONMENT, canonical_json_bytes(value)
    )


def gcp_execution_environment_digest(
    environment: GcpExecutionEnvironment | GcpInsertRequest,
) -> Sha256Digest:
    if isinstance(environment, GcpInsertRequest):
        return _environment_binding_digest(environment)
    environment = GcpExecutionEnvironment.model_validate_json(
        canonical_json_bytes(_json_value(environment))
    )
    value = _json_value(environment)
    value["service_account_scopes"] = sorted(value["service_account_scopes"])
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_ENVIRONMENT, canonical_json_bytes(value)
    )


def canonical_gcp_cost_quote_bytes(quote: GcpCostQuote) -> bytes:
    value = _json_value(quote)
    value["components"] = sorted(
        value["components"], key=lambda component: component["component_id"]
    )
    return canonical_json_bytes(value)


def gcp_cost_quote_digest(quote: GcpCostQuote) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_QUOTE, canonical_gcp_cost_quote_bytes(quote)
    )


def gcp_capacity_digest(capacity: GcpCapacityInput) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_CAPACITY,
        canonical_json_bytes(_json_value(capacity)),
    )


def canonical_gcp_execution_outcome_bytes(outcome: GcpExecutionOutcome) -> bytes:
    return canonical_json_bytes(_json_value(outcome))


def gcp_execution_outcome_sha256(outcome: GcpExecutionOutcome) -> Sha256Digest:
    return sha256_digest(canonical_gcp_execution_outcome_bytes(outcome))


def parse_gcp_execution_outcome_json(payload: str | bytes) -> GcpExecutionOutcome:
    _preflight_json(payload, kind="GCP execution outcome")
    return GcpExecutionOutcome.model_validate_json(payload)


def issue_gcp_execution_arm(
    *,
    plan: GcpDryRunPlan,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
    controller_id: GcpControllerId,
    nonce: GcpNonce,
    issued_at: datetime,
    max_controller_duration_seconds: int,
    confirmation: str,
) -> GcpExecutionArm:
    """Issue a one-shot operator capability without resolving credentials."""

    if confirmation != ARM_CONFIRMATION:
        raise GcpArmError("explicit GCP execution confirmation is required")
    if max_controller_duration_seconds < 1:
        raise GcpArmError("GCP execution duration is invalid")
    try:
        strict_spec = _strict_spec_input(expected_spec)
        strict_inventory = _strict_inventory_input(expected_inventory)
        strict_context = _strict_context_input(expected_context)
        strict_plan = _strict_plan_input(plan)
        verified_plan = verify_gcp_dry_run_plan(
            strict_plan,
            expected_spec=strict_spec,
            expected_inventory=strict_inventory,
            expected_context=strict_context,
        )
    except (GcpPlanError, ValidationError, ValueError):
        raise GcpArmError("GCP plan and execution inputs do not match") from None
    if verified_plan.execution_authorized or verified_plan.provider_mutation_performed:
        raise GcpArmError("offline GCP plan cannot be used as execution authority")
    maximum_lifetime = (
        verified_plan.timeouts.total_seconds
        - verified_plan.timeouts.cleanup_seconds
        - verified_plan.timeouts.termination_confirmation_seconds
    )
    if max_controller_duration_seconds > maximum_lifetime:
        raise GcpArmError("execution duration exceeds the deployment total timeout")
    issued_text = _timestamp(issued_at)
    expires_text = _timestamp(
        issued_at + timedelta(seconds=max_controller_duration_seconds)
    )
    payload = GcpExecutionArmPayload(
        schema_version=GCP_EXECUTION_ARM_SCHEMA_VERSION,
        arm_kind="one_shot_operator_capability",
        plan_id=gcp_plan_id(verified_plan),
        plan_sha256=gcp_plan_sha256(verified_plan),
        deployment_spec_digest=deployment_spec_digest(strict_spec),
        inventory_digest=gcp_inventory_digest(strict_inventory),
        planning_scope=verified_plan.planning_scope,
        selected_provider=verified_plan.selection.selected_provider,
        controller_id=controller_id,
        nonce=nonce,
        issued_at=issued_text,
        expires_at=expires_text,
        max_controller_duration_seconds=max_controller_duration_seconds,
        max_provider_lifetime_seconds=max_controller_duration_seconds,
        cost_ceiling=verified_plan.cost_ceiling,
        authorization_granted=True,
        provider_mutation_performed=False,
        credentials_resolved=False,
        evidence_eligible=False,
        invoice_truth="unavailable_external_provider_invoice",
    )
    return GcpExecutionArm(**_json_value(payload), arm_id=gcp_execution_arm_id(payload))


def verify_gcp_execution_arm_bytes(
    payload: bytes,
    *,
    expected_plan: GcpDryRunPlan,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
    now: datetime,
    expected_controller_id: str | None = None,
) -> GcpExecutionArm:
    arm = parse_gcp_execution_arm_json(payload)
    if canonical_gcp_execution_arm_bytes(arm) != payload:
        raise GcpArmError("execution arm bytes are not canonical")
    try:
        strict_spec = _strict_spec_input(expected_spec)
        strict_inventory = _strict_inventory_input(expected_inventory)
        strict_context = _strict_context_input(expected_context)
        strict_plan = _strict_plan_input(expected_plan)
        verified_plan = verify_gcp_dry_run_plan(
            strict_plan,
            expected_spec=strict_spec,
            expected_inventory=strict_inventory,
            expected_context=strict_context,
        )
    except (GcpPlanError, ValidationError, ValueError):
        raise GcpArmError("GCP plan and execution inputs do not match") from None
    if (
        expected_controller_id is not None
        and arm.controller_id != expected_controller_id
    ):
        raise GcpArmError("execution arm controller does not match")
    if arm.plan_id != gcp_plan_id(verified_plan) or arm.plan_sha256 != gcp_plan_sha256(
        verified_plan
    ):
        raise GcpArmError("execution arm plan binding does not match")
    if arm.deployment_spec_digest != deployment_spec_digest(strict_spec):
        raise GcpArmError("execution arm deployment binding does not match")
    if arm.inventory_digest != gcp_inventory_digest(strict_inventory):
        raise GcpArmError("execution arm inventory binding does not match")
    if (
        arm.planning_scope != verified_plan.planning_scope
        or arm.selected_provider != verified_plan.selection.selected_provider
    ):
        raise GcpArmError("execution arm selection binding does not match")
    if arm.cost_ceiling != verified_plan.cost_ceiling:
        raise GcpArmError("execution arm cost ceiling does not match")
    now_text = _timestamp(now)
    if not (
        _parse_timestamp(arm.issued_at)
        <= _parse_timestamp(now_text)
        < _parse_timestamp(arm.expires_at)
    ):
        raise GcpArmError("execution arm is expired or not yet valid")
    return arm


def _instance_name(controller_id: str) -> str:
    if _CONTROLLER_RE.fullmatch(controller_id) is None:
        raise GcpExecutionError("controller identifier is invalid")
    return f"inferdrome-{controller_id}"


def build_gcp_insert_request(
    *,
    plan: GcpDryRunPlan,
    arm: GcpExecutionArm,
    environment: GcpExecutionEnvironment,
) -> GcpInsertRequest:
    """Purely project a verified plan into an exact private insert request."""

    plan = _strict_plan_input(plan)
    arm = parse_gcp_execution_arm_json(canonical_gcp_execution_arm_bytes(arm))
    environment = GcpExecutionEnvironment.model_validate_json(
        canonical_json_bytes(_json_value(environment))
    )
    provider = plan.selection.selected_provider
    if arm.plan_id != gcp_plan_id(plan):
        raise GcpExecutionError("request plan binding does not match arm")
    if arm.selected_provider != provider:
        raise GcpExecutionError("request selection does not match arm")
    if environment.architecture != provider.architecture:
        raise GcpExecutionError("execution architecture does not match selected GPU")
    if environment.boot_image.repository.split("/")[1] != provider.project_id:
        raise GcpExecutionError("boot image project does not match selected project")
    if (
        environment.runner_image != plan.runner_image
        or environment.serving_runtime_image != plan.serving_runtime_image
    ):
        raise GcpExecutionError("runtime image identity does not match plan")
    if (
        environment.boot_disk_size_gib
        < plan.selection.requested_resources.ephemeral_storage_gib
    ):
        raise GcpExecutionError(
            "boot disk is smaller than the declared resource requirement"
        )
    subnetwork_parts = environment.network.subnetwork.split("/")
    network_parts = environment.network.network.split("/")
    if (
        subnetwork_parts[1] != provider.project_id
        or subnetwork_parts[3] != provider.region
        or network_parts[1] != provider.project_id
    ):
        raise GcpExecutionError("private network scope does not match selected project")
    insert_request_id = _stable_request_uuid(arm.arm_id, arm.controller_id, "insert")
    delete_request_id = _stable_request_uuid(arm.arm_id, arm.controller_id, "delete")
    return GcpInsertRequest(
        schema_version=GCP_EXECUTION_REQUEST_SCHEMA_VERSION,
        project_id=provider.project_id,
        region=provider.region,
        zone=provider.zone,
        instance_name=_instance_name(arm.controller_id),
        machine_type=provider.machine_type,
        architecture=provider.architecture,
        accelerator_model=provider.accelerator_model,
        accelerator_provider_type=provider.accelerator_provider_type,
        accelerator_count=provider.accelerator_count,
        boot_image=environment.boot_image,
        runner_image=environment.runner_image,
        serving_runtime_image=environment.serving_runtime_image,
        boot_disk_size_gib=environment.boot_disk_size_gib,
        boot_disk_type=environment.boot_disk_type,
        boot_disk_auto_delete=True,
        network=environment.network,
        service_account=environment.service_account,
        service_account_scopes=environment.service_account_scopes,
        deletion_protection=environment.deletion_protection,
        automatic_restart=environment.automatic_restart,
        maintenance_policy=environment.maintenance_policy,
        provider_max_runtime_seconds=arm.max_provider_lifetime_seconds,
        plan_id=arm.plan_id,
        arm_id=arm.arm_id,
        controller_id=arm.controller_id,
        insert_request_id=insert_request_id,
        delete_request_id=delete_request_id,
        labels=GcpExecutionLabels(
            inferdrome="inferdrome",
            controller_id=_compact_label("c", arm.controller_id),
            plan_id=_compact_label("p", arm.plan_id),
            arm_id=_compact_label("a", arm.arm_id),
            managed_by="inferdrome_gcp_execution_v1",
            role="provider-envelope",
        ),
        model_id=plan.runtime.model_id,
        model_revision=plan.runtime.model_revision,
        tokenizer_revision=plan.runtime.tokenizer_revision,
        runtime_engine=plan.runtime.engine,
        runtime_version=plan.runtime.engine_version,
        endpoint_scope=plan.runtime.endpoint_scope,
        runner_runtime_colocation=plan.runtime.runner_runtime_colocation,
        startup_script_digest=environment.startup_script_digest,
    )


def validate_cost_and_capacity(
    *,
    plan: GcpDryRunPlan,
    arm: GcpExecutionArm,
    request: GcpInsertRequest,
    quote: GcpCostQuote,
    capacity: GcpCapacityInput,
    now: datetime,
) -> None:
    plan = _strict_plan_input(plan)
    arm = parse_gcp_execution_arm_json(canonical_gcp_execution_arm_bytes(arm))
    request = GcpInsertRequest.model_validate_json(
        canonical_json_bytes(_json_value(request))
    )
    quote = GcpCostQuote.model_validate_json(canonical_json_bytes(_json_value(quote)))
    capacity = GcpCapacityInput.model_validate_json(
        canonical_json_bytes(_json_value(capacity))
    )
    if quote.plan_id != arm.plan_id or quote.controller_id != arm.controller_id:
        raise GcpExecutionError("cost quote binding does not match the execution arm")
    if quote.request_digest != gcp_execution_request_digest(request):
        raise GcpExecutionError("cost quote request binding does not match")
    if quote.environment_digest != _environment_binding_digest(request):
        raise GcpExecutionError("cost quote environment binding does not match")
    if (quote.project_id, quote.region, quote.zone, quote.machine_type) != (
        request.project_id,
        request.region,
        request.zone,
        request.machine_type,
    ):
        raise GcpExecutionError("cost quote selection does not match the request")
    if (
        quote.accelerator_model != request.accelerator_model
        or quote.accelerator_provider_type != request.accelerator_provider_type
        or quote.accelerator_count != request.accelerator_count
    ):
        raise GcpExecutionError("cost quote accelerator does not match the request")
    if (
        quote.network != request.network.network
        or quote.subnetwork != request.network.subnetwork
        or quote.boot_image_digest != request.boot_image.digest
        or quote.runner_image_digest != request.runner_image.digest
        or quote.serving_runtime_image_digest != request.serving_runtime_image.digest
        or quote.boot_disk_size_gib != request.boot_disk_size_gib
        or quote.boot_disk_type != request.boot_disk_type
        or quote.service_account != request.service_account
        or quote.provider_max_runtime_seconds != request.provider_max_runtime_seconds
    ):
        raise GcpExecutionError("cost quote environment selection does not match")
    required_billable_seconds = (
        request.provider_max_runtime_seconds
        + plan.timeouts.cleanup_seconds
        + plan.timeouts.termination_confirmation_seconds
    )
    if quote.billable_duration_seconds < required_billable_seconds:
        raise GcpExecutionError("cost quote duration undercovers cleanup tail")
    now_text = _timestamp(now)
    now_value = _parse_timestamp(now_text)
    if not (
        _parse_timestamp(quote.issued_at)
        <= now_value
        <= _parse_timestamp(quote.valid_until)
    ):
        raise GcpExecutionError("cost quote is stale or not yet valid")
    if (
        now_value - _parse_timestamp(quote.issued_at)
    ).total_seconds() > quote.freshness_seconds:
        raise GcpExecutionError("cost quote freshness window has elapsed")
    try:
        ceiling_micro = int(
            (
                Decimal(plan.cost_ceiling.max_cost_usd) * Decimal(1_000_000)
            ).to_integral_exact()
        )
    except (InvalidOperation, ValueError):
        raise GcpExecutionError(
            "cost ceiling is not an exact fixed-point USD amount"
        ) from None
    if quote.worst_case_microusd > ceiling_micro:
        raise GcpExecutionError(
            "worst-case controller estimate exceeds the hard ceiling"
        )
    if (
        capacity.plan_id != arm.plan_id
        or capacity.project_id != request.project_id
        or capacity.region != request.region
        or capacity.zone != request.zone
    ):
        raise GcpExecutionError("capacity input does not match the selected request")
    if (
        capacity.machine_type,
        capacity.accelerator_model,
        capacity.accelerator_count,
    ) != (
        request.machine_type,
        request.accelerator_model,
        request.accelerator_count,
    ):
        raise GcpExecutionError("capacity input resource does not match the request")
    if capacity.request_digest != gcp_execution_request_digest(request):
        raise GcpExecutionError("capacity observation request binding does not match")
    observed_at = _parse_timestamp(capacity.observed_at)
    if observed_at > now_value:
        raise GcpExecutionError("capacity observation is from the future")
    if (now_value - observed_at).total_seconds() > capacity.freshness_seconds:
        raise GcpExecutionError("capacity observation is stale")
    if capacity.capacity_status != "operator_supplied_eligible":
        raise GcpExecutionError("capacity is not eligible for execution")
    if capacity.matching_active_resources != 0:
        raise GcpExecutionError("matching active resources block execution")
    if capacity.capacity_proven:
        raise GcpExecutionError("capacity proof cannot be asserted by this controller")


def validate_gcp_execution_preflight(
    *,
    plan: GcpDryRunPlan,
    expected_spec: DeploymentSpec,
    expected_inventory: GcpInventorySnapshot,
    expected_context: GcpPlanningContext,
    arm_bytes: bytes,
    environment: GcpExecutionEnvironment,
    quote: GcpCostQuote,
    capacity: GcpCapacityInput,
    now: datetime,
) -> GcpExecutionPreflight:
    expected_spec = _strict_spec_input(expected_spec)
    expected_inventory = _strict_inventory_input(expected_inventory)
    expected_context = _strict_context_input(expected_context)
    plan = _strict_plan_input(plan)
    verified_plan = verify_gcp_dry_run_plan(
        plan,
        expected_spec=expected_spec,
        expected_inventory=expected_inventory,
        expected_context=expected_context,
    )
    arm = verify_gcp_execution_arm_bytes(
        arm_bytes,
        expected_plan=verified_plan,
        expected_spec=expected_spec,
        expected_inventory=expected_inventory,
        expected_context=expected_context,
        now=now,
    )
    request = build_gcp_insert_request(
        plan=verified_plan, arm=arm, environment=environment
    )
    validate_cost_and_capacity(
        plan=verified_plan,
        arm=arm,
        request=request,
        quote=quote,
        capacity=capacity,
        now=now,
    )
    return GcpExecutionPreflight(
        arm=arm,
        request=request,
        quote=quote,
        capacity=capacity,
        arm_sha256=sha256_digest(arm_bytes),
    )


def _parse_lease_bytes(raw: bytes) -> GcpLeaseRecord:
    if len(raw) > GCP_MAX_JOURNAL_BYTES:
        raise GcpJournalError("GCP journal exceeds its bound")
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise GcpJournalError("GCP journal is not valid JSON") from None
    if not isinstance(value, dict):
        raise GcpJournalError("GCP journal root is invalid")
    _reject_unsafe_values(value)
    try:
        record = GcpLeaseRecord.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpJournalError("GCP journal is invalid") from None
    if canonical_json_bytes(_json_value(record)) != raw:
        raise GcpJournalError("GCP journal is not canonical")
    return record


def _strict_lease(record: GcpLeaseRecord) -> GcpLeaseRecord:
    """Re-parse model objects before treating them as a storage authority."""

    raw = canonical_json_bytes(_json_value(record))
    try:
        parsed = GcpLeaseRecord.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpJournalError("GCP journal record is invalid") from None
    if canonical_json_bytes(_json_value(parsed)) != raw:
        raise GcpJournalError("GCP journal record is not canonical")
    return parsed


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short journal write")
        offset += written


def _journal_event(
    record: GcpLeaseRecord,
    *,
    sequence: int,
    previous_event_digest: Sha256Digest | None,
) -> GcpLeaseJournalEvent:
    payload = {
        "schema_version": GCP_EXECUTION_EVENT_SCHEMA_VERSION,
        "controller_id": record.controller_id,
        "sequence": sequence,
        "previous_event_digest": previous_event_digest,
        "record_sha256": sha256_digest(canonical_json_bytes(_json_value(record))),
        "record": _json_value(record),
    }
    payload["event_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_JOURNAL, canonical_json_bytes(payload)
    )
    return GcpLeaseJournalEvent.model_validate_json(canonical_json_bytes(payload))


class GcpLeaseJournal:
    """Small atomic local lease store; it never stores provider payloads/secrets."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpJournalError("GCP journal path must be absolute")
        try:
            metadata = self.root.lstat()
        except OSError:
            raise GcpJournalError("GCP journal directory is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GcpJournalError("GCP journal directory is unsafe")
        return self.root

    @staticmethod
    def _path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpJournalError("GCP controller identifier is invalid")
        return root / f"{controller_id}.lease.json"

    @staticmethod
    def _event_path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpJournalError("GCP controller identifier is invalid")
        return root / f"{controller_id}.events.jsonl"

    @staticmethod
    def _anchor_path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpJournalError("GCP controller identifier is invalid")
        return root / f"{controller_id}.intent.json"

    @staticmethod
    def _anchor_for(record: GcpLeaseRecord) -> GcpLeaseIntentAnchor:
        payload = {
            "schema_version": GCP_EXECUTION_ANCHOR_SCHEMA_VERSION,
            "controller_id": record.controller_id,
            "arm_id": record.arm_id,
            "plan_id": record.plan_id,
            "request_digest": record.request_digest,
            "request": _json_value(record.request),
            "labels": _json_value(record.labels),
        }
        payload["anchor_digest"] = digest_bytes(
            DigestDomain.GCP_EXECUTION_JOURNAL, canonical_json_bytes(payload)
        )
        return GcpLeaseIntentAnchor.model_validate_json(canonical_json_bytes(payload))

    @contextmanager
    def _exclusive(self) -> Any:
        """Serialize journal operations across controller processes."""

        root = self._checked_root()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / ".journal.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpJournalError("GCP journal locking is unavailable") from None
            with self._lock:
                yield
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def _read_anchor(self, path: Path) -> GcpLeaseIntentAnchor:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_MAX_JOURNAL_BYTES
            ):
                raise GcpJournalError("GCP journal intent is unsafe")
            raw = os.read(descriptor, GCP_MAX_JOURNAL_BYTES + 1)
            if len(raw) > GCP_MAX_JOURNAL_BYTES:
                raise GcpJournalError("GCP journal intent exceeds its bound")
            final = os.fstat(descriptor)
            if final.st_ino != metadata.st_ino or final.st_size != len(raw):
                raise GcpJournalError("GCP journal intent changed during read")
            _preflight_json(raw, kind="GCP journal intent")
            anchor = GcpLeaseIntentAnchor.model_validate_json(raw)
            if canonical_json_bytes(_json_value(anchor)) != raw:
                raise GcpJournalError("GCP journal intent is not canonical")
            return anchor
        except GcpJournalError:
            raise
        except (OSError, ValueError, ValidationError):
            raise GcpJournalError("GCP journal intent is unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_event_chain(self, path: Path) -> GcpLeaseJournalEvent:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_MAX_JOURNAL_EVENT_BYTES
            ):
                raise GcpJournalError("GCP journal event chain is unsafe")
            raw = os.read(descriptor, GCP_MAX_JOURNAL_EVENT_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_MAX_JOURNAL_EVENT_BYTES
                or final.st_ino != metadata.st_ino
                or final.st_size != len(raw)
            ):
                raise GcpJournalError("GCP journal event chain changed during read")
            lines = raw.splitlines()
            if not lines:
                raise GcpJournalError("GCP journal event chain is empty")
            previous: Sha256Digest | None = None
            last: GcpLeaseJournalEvent | None = None
            for sequence, line in enumerate(lines):
                if not line:
                    raise GcpJournalError("GCP journal event chain is malformed")
                _preflight_json(line, kind="GCP journal event")
                event = GcpLeaseJournalEvent.model_validate_json(line)
                if canonical_json_bytes(_json_value(event)) != line:
                    raise GcpJournalError("GCP journal event is not canonical")
                if (
                    event.sequence != sequence
                    or event.previous_event_digest != previous
                ):
                    raise GcpJournalError("GCP journal event chain is not contiguous")
                previous = event.event_digest
                last = event
            assert last is not None
            return last
        except GcpJournalError:
            raise
        except (OSError, ValueError, ValidationError):
            raise GcpJournalError("GCP journal event chain is unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_path(self, path: Path) -> GcpLeaseRecord:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_MAX_JOURNAL_BYTES
            ):
                raise GcpJournalError("GCP journal file is unsafe")
            raw = os.read(descriptor, GCP_MAX_JOURNAL_BYTES + 1)
            if len(raw) > GCP_MAX_JOURNAL_BYTES:
                raise GcpJournalError("GCP journal exceeds its bound")
            final = os.fstat(descriptor)
            if final.st_ino != metadata.st_ino or final.st_size != len(raw):
                raise GcpJournalError("GCP journal changed during read")
            record = _parse_lease_bytes(raw)
            anchor = self._read_anchor(
                self._anchor_path_for(path.parent, record.controller_id)
            )
            if (
                record.intent_anchor_digest != anchor.anchor_digest
                or record.request_digest != anchor.request_digest
                or record.request != anchor.request
                or record.labels != anchor.labels
            ):
                raise GcpJournalError("GCP journal intent binding is invalid")
            event = self._read_event_chain(
                self._event_path_for(path.parent, record.controller_id)
            )
            if event.record != record:
                raise GcpJournalError("GCP journal event head does not match lease")
            return record
        except GcpJournalError:
            raise
        except (OSError, ValueError):
            raise GcpJournalError("GCP journal file is unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _scan(self) -> list[GcpLeaseRecord]:
        root = self._checked_root()
        records: list[GcpLeaseRecord] = []
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            raise GcpJournalError("GCP journal directory is unavailable") from None
        for entry in entries:
            if (
                entry.name == ".journal.lock"
                or entry.name.endswith(".intent.json")
                or entry.name.endswith(".events.jsonl")
            ):
                continue
            if entry.name.startswith(".") and entry.name.endswith(".stage"):
                raise GcpJournalError("GCP journal contains an incomplete stage")
            if not entry.name.endswith(".lease.json"):
                raise GcpJournalError("GCP journal contains an unexpected entry")
            if entry.is_symlink():
                raise GcpJournalError("GCP journal contains a symlink")
            records.append(self._read_path(entry))
        return records

    def reserve(self, record: GcpLeaseRecord) -> None:
        record = _strict_lease(record)
        root = self._checked_root()
        path = self._path_for(root, record.controller_id)
        anchor = self._anchor_for(record)
        if record.intent_anchor_digest != anchor.anchor_digest:
            raise GcpJournalError("GCP journal intent digest is invalid")
        with self._exclusive():
            for existing in self._scan():
                if (
                    existing.state != "CLEANUP_CONFIRMED"
                    and existing.plan_id == record.plan_id
                ):
                    raise GcpJournalError("an unresolved GCP lease already exists")
            raw = canonical_json_bytes(_json_value(record))
            event_raw = (
                canonical_json_bytes(
                    _json_value(
                        _journal_event(record, sequence=0, previous_event_digest=None)
                    )
                )
                + b"\n"
            )
            if len(raw) > GCP_MAX_JOURNAL_BYTES:
                raise GcpJournalError("GCP journal record exceeds its bound")
            try:
                anchor_path = self._anchor_path_for(root, record.controller_id)
                event_path = self._event_path_for(root, record.controller_id)
                anchor_raw = canonical_json_bytes(_json_value(anchor))
                anchor_descriptor = os.open(
                    anchor_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    _write_all(anchor_descriptor, anchor_raw)
                    os.fsync(anchor_descriptor)
                finally:
                    os.close(anchor_descriptor)
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                event_descriptor = os.open(
                    event_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    _write_all(event_descriptor, event_raw)
                    os.fsync(event_descriptor)
                finally:
                    os.close(event_descriptor)
                directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                raise GcpJournalError("GCP controller lease already exists") from None
            except OSError:
                raise GcpJournalError("GCP journal reservation failed") from None

    def load(self, controller_id: str) -> GcpLeaseRecord:
        with self._exclusive():
            path = self._path_for(self._checked_root(), controller_id)
            return self._read_path(path)

    def update(self, record: GcpLeaseRecord) -> None:
        record = _strict_lease(record)
        root = self._checked_root()
        path = self._path_for(root, record.controller_id)
        stage = root / f".{record.controller_id}.{threading.get_ident()}.stage"
        raw = canonical_json_bytes(_json_value(record))
        with self._exclusive():
            try:
                previous = self._read_path(path)
            except GcpJournalError:
                raise
            previous_event = self._read_event_chain(
                self._event_path_for(root, record.controller_id)
            )
            allowed = {
                "PREPARED": {"PREPARED", "ARM_CONSUMED", "CLEANUP_CONFIRMED"},
                "ARM_CONSUMED": {
                    "ARM_CONSUMED",
                    "CREATE_SUBMITTED",
                    "CLEANUP_PENDING",
                    "CLEANUP_CONFIRMED",
                },
                "CREATE_SUBMITTED": {
                    "CREATE_SUBMITTED",
                    "OWNED",
                    "CLEANUP_PENDING",
                    "ORPHANED",
                    "BLOCKED",
                },
                "OWNED": {"OWNED", "CLEANUP_PENDING"},
                "CLEANUP_PENDING": {
                    "CLEANUP_PENDING",
                    "CLEANUP_CONFIRMED",
                    "ORPHANED",
                    "BLOCKED",
                },
                "ORPHANED": {
                    "ORPHANED",
                    "CLEANUP_PENDING",
                    "CLEANUP_CONFIRMED",
                    "BLOCKED",
                },
                "BLOCKED": {"BLOCKED", "CLEANUP_PENDING", "ORPHANED"},
                "CLEANUP_CONFIRMED": {"CLEANUP_CONFIRMED"},
            }
            if record.state not in allowed.get(previous.state, set()):
                raise GcpJournalError("GCP journal state transition is invalid")
            if record.delete_attempts < previous.delete_attempts:
                raise GcpJournalError("GCP journal cleanup attempts regressed")
            for field in (
                "controller_id",
                "arm_id",
                "plan_id",
                "request",
                "request_digest",
                "labels",
                "intent_anchor_digest",
            ):
                if getattr(record, field) != getattr(previous, field):
                    raise GcpJournalError("GCP journal immutable binding changed")
            event_raw = (
                canonical_json_bytes(
                    _json_value(
                        _journal_event(
                            record,
                            sequence=previous_event.sequence + 1,
                            previous_event_digest=previous_event.event_digest,
                        )
                    )
                )
                + b"\n"
            )
            if len(event_raw) > GCP_MAX_JOURNAL_EVENT_BYTES:
                raise GcpJournalError("GCP journal event exceeds its bound")
            try:
                descriptor = os.open(
                    stage,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                event_descriptor = os.open(
                    self._event_path_for(root, record.controller_id),
                    os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    _write_all(event_descriptor, event_raw)
                    os.fsync(event_descriptor)
                finally:
                    os.close(event_descriptor)
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise GcpJournalError("GCP journal target is a symlink")
                os.replace(stage, path)
                directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except GcpJournalError:
                raise
            except OSError:
                raise GcpJournalError("GCP journal update failed") from None
            finally:
                try:
                    if stage.exists() or stage.is_symlink():
                        stage.unlink()
                except OSError:
                    pass

    def unresolved_for_plan(self, plan_id: Sha256Digest) -> tuple[GcpLeaseRecord, ...]:
        with self._exclusive():
            return tuple(
                record
                for record in self._scan()
                if record.plan_id == plan_id and record.state != "CLEANUP_CONFIRMED"
            )


class FakeGcpComputeTransport:
    """Deterministic provider fake, including late-operation reconciliation."""

    def __init__(
        self,
        *,
        insert_error: str | None = None,
        insert_error_ambiguous: bool = False,
        insert_status: Literal["DONE", "ERROR", "TIMEOUT"] = "DONE",
        delete_error: str | None = None,
        delete_error_ambiguous: bool = False,
        delete_status: Literal["DONE", "ERROR", "TIMEOUT"] = "DONE",
        false_not_found: bool = False,
        insert_timeout_reconcile_status: Literal[
            "TIMEOUT", "DONE", "ERROR"
        ] = "TIMEOUT",
        insert_timeout_late_instance: bool = False,
    ) -> None:
        self.insert_error = insert_error
        self.insert_error_ambiguous = insert_error_ambiguous
        self.insert_status = insert_status
        self.delete_error = delete_error
        self.delete_error_ambiguous = delete_error_ambiguous
        self.delete_status = delete_status
        self.false_not_found = false_not_found
        self.insert_timeout_reconcile_status = insert_timeout_reconcile_status
        self.insert_timeout_late_instance = insert_timeout_late_instance
        self.insert_calls = 0
        self.delete_calls = 0
        self.get_calls = 0
        self.list_calls = 0
        self.wait_calls = 0
        self.requests: list[GcpInsertRequest] = []
        self.request_ids: list[str] = []
        self._owned: GcpInstanceObservation | None = None
        self._operations: dict[str, GcpOperationResult] = {}
        self._sequence = 0

    @staticmethod
    def _not_found(request: GcpInsertRequest) -> GcpInstanceObservation:
        return GcpInstanceObservation(
            instance_name=request.instance_name,
            project_id=request.project_id,
            zone=request.zone,
            machine_type=request.machine_type,
            accelerator_model=request.accelerator_model,
            accelerator_count=request.accelerator_count,
            state="NOT_FOUND",
            labels=None,
            external_access_config="absent",
            ip_forwarding=False,
        )

    @staticmethod
    def _running(request: GcpInsertRequest) -> GcpInstanceObservation:
        return GcpInstanceObservation(
            instance_name=request.instance_name,
            project_id=request.project_id,
            zone=request.zone,
            machine_type=request.machine_type,
            accelerator_model=request.accelerator_model,
            accelerator_provider_type=request.accelerator_provider_type,
            accelerator_count=request.accelerator_count,
            state="RUNNING",
            labels=request.labels,
            external_access_config="absent",
            ip_forwarding=False,
            network=request.network.network,
            subnetwork=request.network.subnetwork,
            private_ipv4_addresses=("10.0.0.2",),
        )

    def _operation(
        self,
        kind: Literal["insert", "delete"],
        status: Literal["DONE", "ERROR", "TIMEOUT"],
        error: str | None,
        instance_name: GcpInstanceName,
        request: GcpInsertRequest,
    ) -> GcpOperationHandle:
        self._sequence += 1
        operation_id = f"op-{self._sequence:08d}"
        operation_name = "/".join(
            (
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "operations",
                operation_id,
            )
        )
        result = GcpOperationResult(
            status=status,
            operation_id=operation_id,
            operation_name=operation_name,
            instance_name=instance_name,
            error_code=error if status == "ERROR" else None,
        )
        self._operations[operation_id] = result
        return GcpOperationHandle(
            operation_id=operation_id,
            operation_kind=kind,
            operation_name=operation_name,
            project_id=request.project_id,
            zone=request.zone,
        )

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del timeout_seconds
        self.insert_calls += 1
        self.request_ids.append(request_id)
        self.requests.append(request)
        if request_id != request.insert_request_id:
            raise GcpTransportError("REQUEST_ID_MISMATCH")
        if self.insert_error is not None:
            raise GcpTransportError(
                self.insert_error, ambiguous=self.insert_error_ambiguous
            )
        operation = self._operation(
            "insert",
            self.insert_status,
            "INSERT_FAILED" if self.insert_status == "ERROR" else None,
            request.instance_name,
            request,
        )
        if self.insert_status == "DONE":
            self._owned = self._running(request)
        return operation

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        del timeout_seconds
        self.wait_calls += 1
        try:
            result = self._operations[operation.operation_id]
        except KeyError:
            raise GcpTransportError("OPERATION_UNKNOWN") from None
        if (
            result.status == "TIMEOUT"
            and operation.operation_kind == "insert"
            and self.wait_calls > 1
        ):
            reconciled_status = self.insert_timeout_reconcile_status
            result = GcpOperationResult(
                status=reconciled_status,
                operation_id=result.operation_id,
                operation_name=result.operation_name,
                instance_name=result.instance_name,
                error_code=("INSERT_FAILED" if reconciled_status == "ERROR" else None),
            )
            self._operations[operation.operation_id] = result
            if result.status == "DONE" and self.insert_timeout_late_instance:
                request = self.requests[0]
                self._owned = self._running(request)
        return result

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        del timeout_seconds
        self.get_calls += 1
        if self.false_not_found or self._owned is None:
            return self._not_found(request)
        return self._owned

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        del timeout_seconds
        self.list_calls += 1
        if self._owned is None or self._owned.labels != request.labels:
            return ()
        return (self._owned,)

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del timeout_seconds
        self.delete_calls += 1
        self.request_ids.append(request_id)
        if request_id != request.delete_request_id:
            raise GcpTransportError("REQUEST_ID_MISMATCH")
        if self.delete_error is not None:
            raise GcpTransportError(
                self.delete_error, ambiguous=self.delete_error_ambiguous
            )
        operation = self._operation(
            "delete",
            self.delete_status,
            "DELETE_FAILED" if self.delete_status == "ERROR" else None,
            request.instance_name,
            request,
        )
        if self.delete_status == "DONE" and not self.false_not_found:
            self._owned = None
        return operation


def _owned(observation: GcpInstanceObservation, request: GcpInsertRequest) -> bool:
    return (
        observation.state in {"RUNNING", "TERMINATED"}
        and observation.instance_name == request.instance_name
        and observation.project_id == request.project_id
        and observation.zone == request.zone
        and observation.machine_type == request.machine_type
        and observation.accelerator_model == request.accelerator_model
        and observation.accelerator_provider_type == request.accelerator_provider_type
        and observation.accelerator_count == request.accelerator_count
        and observation.labels == request.labels
        and observation.external_access_config == "absent"
        and observation.ip_forwarding is False
        and observation.network == request.network.network
        and observation.subnetwork == request.network.subnetwork
        and observation.external_ipv6_present is False
    )


class GcpGuardedLifecycleController:
    """One-shot controller with exact ownership and fail-closed cleanup."""

    def __init__(
        self,
        *,
        transport: GcpComputeTransport,
        arm_store: ExecutionArmStore,
        journal: GcpLeaseJournal,
        clock: GcpClock | None = None,
    ) -> None:
        self.transport = transport
        self.arm_store = arm_store
        self.journal = journal
        self.clock = clock or system_gcp_clock()

    def _trace(
        self,
        trace: list[GcpExecutionTraceEvent],
        phase: GcpTracePhase,
        status: GcpTraceStatus,
        error: str | None = None,
    ) -> None:
        trace.append(
            GcpExecutionTraceEvent(
                sequence=len(trace), phase=phase, status=status, error_code=error
            )
        )

    def _record(
        self, record: GcpLeaseRecord, *, state: GcpLeaseState, now: str, **updates: Any
    ) -> GcpLeaseRecord:
        value = _json_value(record)
        value.update(updates)
        value["state"] = state
        value["updated_at"] = now
        updated = GcpLeaseRecord.model_validate_json(canonical_json_bytes(value))
        self.journal.update(updated)
        return updated

    @staticmethod
    def _memory_record(
        record: GcpLeaseRecord, *, state: GcpLeaseState, now: str, **updates: Any
    ) -> GcpLeaseRecord:
        value = _json_value(record)
        value.update(updates)
        value["state"] = state
        value["updated_at"] = now
        return GcpLeaseRecord.model_validate_json(canonical_json_bytes(value))

    def _record_resilient(
        self, record: GcpLeaseRecord, *, state: GcpLeaseState, now: str, **updates: Any
    ) -> tuple[GcpLeaseRecord, str | None]:
        try:
            return self._record(record, state=state, now=now, **updates), None
        except BaseException:
            # A journal write failure cannot suppress cleanup.  Keep a strictly
            # revalidated in-memory copy and make the final result blocked.
            try:
                return self._memory_record(
                    record, state=state, now=now, **updates
                ), "JOURNAL_UPDATE_FAILED"
            except BaseException:
                return record, "JOURNAL_UPDATE_FAILED"

    @staticmethod
    def _operation_from_record(record: GcpLeaseRecord) -> GcpOperationHandle | None:
        if (
            record.provider_operation_id is None
            or record.provider_operation_kind is None
            or record.provider_operation_terminal
        ):
            return None
        return GcpOperationHandle(
            operation_id=record.provider_operation_id,
            operation_kind=record.provider_operation_kind,
            operation_name=record.provider_operation_name,
            project_id=record.project_id,
            zone=record.zone,
        )

    @staticmethod
    def _validate_operation_handle(
        operation: GcpOperationHandle,
        request: GcpInsertRequest,
        expected_kind: Literal["insert", "delete"],
    ) -> GcpOperationHandle:
        try:
            parsed = GcpOperationHandle.model_validate_json(
                canonical_json_bytes(_json_value(operation))
            )
        except (ValidationError, ValueError, TypeError):
            raise GcpTransportError(
                "OPERATION_IDENTITY_INVALID", ambiguous=True
            ) from None
        name = parsed.operation_name
        name_parts = name.split("/") if name is not None else ()
        if (
            parsed.operation_kind != expected_kind
            or parsed.project_id != request.project_id
            or parsed.zone != request.zone
            or len(name_parts) != 6
            or name_parts[:5]
            != ["projects", request.project_id, "zones", request.zone, "operations"]
            or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name_parts[5]) is None
        ):
            raise GcpTransportError("OPERATION_IDENTITY_MISMATCH", ambiguous=True)
        return parsed

    def _reconcile_operation(
        self,
        record: GcpLeaseRecord,
        trace: list[GcpExecutionTraceEvent],
        *,
        timeout_seconds: int,
        pending_operation: GcpOperationHandle | None = None,
    ) -> tuple[
        GcpLeaseRecord, GcpOperationHandle | None, GcpOperationResult | None, str | None
    ]:
        operation = pending_operation or self._operation_from_record(record)
        if operation is None:
            return record, None, None, None
        operation = self._validate_operation_handle(
            operation, record.request, operation.operation_kind
        )
        try:
            raw_result = self.transport.wait_operation(
                operation, timeout_seconds=timeout_seconds
            )
        except GcpTransportError as error:
            self._trace(trace, "OPERATION_WAIT", "FAILED", error.code)
            return record, operation, None, error.code
        try:
            result = GcpOperationResult.model_validate_json(
                canonical_json_bytes(_json_value(raw_result))
            )
        except (ValidationError, ValueError, TypeError):
            self._trace(trace, "OPERATION_WAIT", "FAILED", "OPERATION_RESULT_INVALID")
            return record, operation, None, "OPERATION_RESULT_INVALID"
        if (
            result.operation_id != operation.operation_id
            or (
                result.operation_name is not None
                and result.operation_name != operation.operation_name
            )
            or (
                result.instance_name is not None
                and result.instance_name != record.request.instance_name
            )
        ):
            self._trace(trace, "OPERATION_WAIT", "FAILED", "OPERATION_RESULT_MISMATCH")
            return record, operation, None, "OPERATION_RESULT_MISMATCH"
        status = result.status
        terminal = status in {"DONE", "ERROR"}
        now = _timestamp(self.clock.now())
        prior_journal_error: str | None = None
        updated, journal_error = self._record_resilient(
            record,
            state="CLEANUP_PENDING",
            now=now,
            provider_operation_id=operation.operation_id,
            provider_operation_name=operation.operation_name,
            provider_operation_kind=operation.operation_kind,
            provider_operation_status=status,
            provider_operation_terminal=terminal,
            provider_mutation_ambiguous=(
                False if status == "DONE" else record.provider_mutation_ambiguous
            ),
            last_error_code=(
                result.error_code if status == "ERROR" else prior_journal_error
            ),
        )
        if status == "TIMEOUT":
            self._trace(
                trace, "OPERATION_WAIT", "FAILED", "OPERATION_RECONCILIATION_TIMEOUT"
            )
            return (
                updated,
                operation,
                result,
                journal_error or "OPERATION_RECONCILIATION_TIMEOUT",
            )
        if status == "ERROR":
            self._trace(
                trace,
                "OPERATION_WAIT",
                "FAILED",
                result.error_code or "PROVIDER_OPERATION_FAILED",
            )
            return (
                updated,
                None,
                result,
                journal_error or result.error_code or "PROVIDER_OPERATION_FAILED",
            )
        if operation.operation_kind == "delete":
            self._trace(trace, "DELETE", "SUCCEEDED")
        self._trace(trace, "OPERATION_WAIT", "SUCCEEDED")
        return updated, None, result, journal_error

    def _final_confirm(
        self, request: GcpInsertRequest, *, timeout_seconds: int = 1
    ) -> tuple[bool, str | None]:
        try:
            observed = self.transport.get_instance(
                request, timeout_seconds=timeout_seconds
            )
            owned = self.transport.list_owned(request, timeout_seconds=timeout_seconds)
        except GcpTransportError as error:
            return False, error.code
        except KeyboardInterrupt:
            return False, "CLEANUP_INTERRUPTED"
        except BaseException:
            return False, "CLEANUP_EXCEPTION"
        if observed.state != "NOT_FOUND" or owned:
            return False, "CLEANUP_UNCONFIRMED"
        return True, None

    def _cleanup(
        self,
        record: GcpLeaseRecord,
        request: GcpInsertRequest,
        trace: list[GcpExecutionTraceEvent],
        *,
        max_attempts: int,
        timeout_seconds: int,
        pending_operation: GcpOperationHandle | None = None,
        ambiguous_mutation: bool = False,
    ) -> tuple[GcpLeaseRecord, str | None]:
        now = _timestamp(self.clock.now())
        current, journal_error = self._record_resilient(
            record, state="CLEANUP_PENDING", now=now
        )
        cleanup_error: str | None = None
        if journal_error is not None:
            cleanup_error = journal_error
        ambiguous_mutation = ambiguous_mutation or current.provider_mutation_ambiguous
        cleanup_deadline = self.clock.monotonic() + timeout_seconds

        def remaining_timeout() -> int:
            remaining = int(cleanup_deadline - self.clock.monotonic())
            if remaining < 1:
                raise GcpTransportError("CLEANUP_DEADLINE_EXCEEDED")
            return remaining

        def mark_cleanup_ambiguous(code: str) -> None:
            nonlocal ambiguous_mutation, cleanup_error, current
            ambiguous_mutation = True
            cleanup_error = code
            current, write_error = self._record_resilient(
                current,
                state="CLEANUP_PENDING",
                now=now,
                provider_operation_id=None,
                provider_operation_name=None,
                provider_operation_kind=None,
                provider_operation_status="UNKNOWN",
                provider_operation_terminal=False,
                provider_mutation_ambiguous=True,
                last_error_code=code,
            )
            if write_error is not None:
                cleanup_error = write_error

        # Reconciliation polls are bounded separately from delete attempts.
        # The latter is the safety-critical provider mutation budget and is
        # persisted before each delete call, including recovery processes.
        for _ in range(max_attempts + 1):
            try:
                current, pending_operation, _operation_result, operation_error = (
                    self._reconcile_operation(
                        current,
                        trace,
                        timeout_seconds=remaining_timeout(),
                        pending_operation=pending_operation,
                    )
                )
                ambiguous_mutation = current.provider_mutation_ambiguous
            except GcpTransportError as error:
                _operation_result = None
                operation_error = error.code
            if operation_error is not None:
                cleanup_error = operation_error
            if pending_operation is not None:
                # A non-terminal insert/delete is ambiguous.  Never infer
                # absence from GET/list while the provider operation is live.
                continue
            try:
                call_timeout = remaining_timeout()
                observation = self.transport.get_instance(
                    request, timeout_seconds=call_timeout
                )
                owned = self.transport.list_owned(request, timeout_seconds=call_timeout)
            except GcpTransportError as error:
                cleanup_error = error.code
                continue
            except KeyboardInterrupt:
                cleanup_error = "CLEANUP_INTERRUPTED"
                continue
            except BaseException:
                cleanup_error = "CLEANUP_EXCEPTION"
                continue
            if ambiguous_mutation and observation.state == "NOT_FOUND" and not owned:
                cleanup_error = "AMBIGUOUS_MUTATION_UNRESOLVED"
                self._trace(
                    trace,
                    "FINAL_CONFIRMATION",
                    "FAILED",
                    cleanup_error,
                )
                continue
            if observation.state == "NOT_FOUND" and not owned:
                self._trace(trace, "DELETE", "SKIPPED")
                try:
                    confirmed, confirmation_error = self._final_confirm(
                        request, timeout_seconds=remaining_timeout()
                    )
                except GcpTransportError as error:
                    cleanup_error = error.code
                    self._trace(trace, "FINAL_CONFIRMATION", "FAILED", cleanup_error)
                    continue
                if confirmed:
                    self._trace(trace, "FINAL_CONFIRMATION", "SUCCEEDED")
                    confirmed_record, write_error = self._record_resilient(
                        current,
                        state="CLEANUP_CONFIRMED",
                        now=now,
                        cleanup_confirmed=True,
                        orphaned=False,
                        last_error_code=None,
                    )
                    if write_error is not None:
                        return confirmed_record, write_error
                    return confirmed_record, None
                cleanup_error = confirmation_error or "CLEANUP_UNCONFIRMED"
                self._trace(trace, "FINAL_CONFIRMATION", "FAILED", cleanup_error)
                continue
            if observation.state != "NOT_FOUND" and not _owned(observation, request):
                cleanup_error = "OWNERSHIP_MISMATCH"
                continue
            if owned and any(not _owned(item, request) for item in owned):
                cleanup_error = "OWNERSHIP_MISMATCH"
                continue
            if current.delete_attempts >= max_attempts:
                cleanup_error = cleanup_error or "CLEANUP_ATTEMPTS_EXHAUSTED"
                break
            # Persist/increment before the mutation.  Recovery sees the
            # consumed attempt even if the delete call raises or is canceled.
            current, write_error = self._record_resilient(
                current,
                state="CLEANUP_PENDING",
                now=now,
                delete_attempts=current.delete_attempts + 1,
            )
            if write_error is not None:
                cleanup_error = write_error
            try:
                operation = self._validate_operation_handle(
                    self.transport.delete(
                        request,
                        timeout_seconds=remaining_timeout(),
                        request_id=request.delete_request_id,
                    ),
                    request,
                    "delete",
                )
                current, write_error = self._record_resilient(
                    current,
                    state="CLEANUP_PENDING",
                    now=now,
                    provider_operation_id=operation.operation_id,
                    provider_operation_name=operation.operation_name,
                    provider_operation_kind=operation.operation_kind,
                    provider_operation_status="PENDING",
                    provider_operation_terminal=False,
                )
                if write_error is not None:
                    cleanup_error = write_error
                pending_operation = operation
            except GcpTransportError as error:
                if error.ambiguous:
                    mark_cleanup_ambiguous(error.code)
                else:
                    cleanup_error = error.code
                pending_operation = error.operation
                continue
            except KeyboardInterrupt:
                mark_cleanup_ambiguous("CLEANUP_INTERRUPTED")
                continue
            except BaseException:
                mark_cleanup_ambiguous("CLEANUP_EXCEPTION")
                continue
        self._trace(trace, "DELETE", "FAILED", cleanup_error or "CLEANUP_UNCONFIRMED")
        orphan_record, write_error = self._record_resilient(
            current,
            state="ORPHANED",
            now=now,
            cleanup_confirmed=False,
            orphaned=True,
            last_error_code=cleanup_error or "CLEANUP_UNCONFIRMED",
        )
        return orphan_record, write_error or cleanup_error or "CLEANUP_UNCONFIRMED"

    def execute(
        self,
        *,
        plan: GcpDryRunPlan,
        expected_spec: DeploymentSpec,
        expected_inventory: GcpInventorySnapshot,
        expected_context: GcpPlanningContext,
        arm_bytes: bytes,
        environment: GcpExecutionEnvironment,
        quote: GcpCostQuote,
        capacity: GcpCapacityInput,
        work: Callable[[GcpInstanceObservation], object] | None = None,
    ) -> GcpExecutionOutcome:
        """Execute only with a verified, unexpired, atomically consumed arm."""

        preflight = validate_gcp_execution_preflight(
            plan=plan,
            expected_spec=expected_spec,
            expected_inventory=expected_inventory,
            expected_context=expected_context,
            arm_bytes=arm_bytes,
            environment=environment,
            quote=quote,
            capacity=capacity,
            now=self.clock.now(),
        )
        trace: list[GcpExecutionTraceEvent] = []
        self._trace(trace, "VALIDATION", "SUCCEEDED")
        request = preflight.request
        arm = preflight.arm
        now = _timestamp(self.clock.now())
        record = GcpLeaseRecord(
            schema_version=GCP_EXECUTION_JOURNAL_SCHEMA_VERSION,
            state="PREPARED",
            controller_id=arm.controller_id,
            arm_id=arm.arm_id,
            plan_id=arm.plan_id,
            project_id=request.project_id,
            region=request.region,
            zone=request.zone,
            instance_name=request.instance_name,
            request=request,
            request_digest=gcp_execution_request_digest(request),
            environment_digest=_environment_binding_digest(request),
            quote_digest=gcp_cost_quote_digest(preflight.quote),
            capacity_digest=gcp_capacity_digest(preflight.capacity),
            estimated_max_microusd=preflight.quote.worst_case_microusd,
            declared_cost_ceiling_usd=preflight.arm.cost_ceiling.max_cost_usd,
            estimate_basis="operator_supplied_fixed_point_quote",
            labels=request.labels,
            intent_anchor_digest="sha256:" + "0" * 64,
            created_at=now,
            updated_at=now,
            arm_consumed=False,
            provider_mutation_attempted=False,
            cleanup_confirmed=False,
            orphaned=False,
            provider_mutation_ambiguous=False,
            delete_attempts=0,
            max_cleanup_attempts=plan.cleanup_policy.max_cleanup_attempts,
            cleanup_timeout_seconds=plan.timeouts.cleanup_seconds,
        )
        anchor_payload = {
            "schema_version": GCP_EXECUTION_ANCHOR_SCHEMA_VERSION,
            "controller_id": record.controller_id,
            "arm_id": record.arm_id,
            "plan_id": record.plan_id,
            "request_digest": record.request_digest,
            "request": _json_value(record.request),
            "labels": _json_value(record.labels),
        }
        record = self._memory_record(
            record,
            state="PREPARED",
            now=now,
            intent_anchor_digest=digest_bytes(
                DigestDomain.GCP_EXECUTION_JOURNAL,
                canonical_json_bytes(anchor_payload),
            ),
        )
        self.journal.reserve(record)
        self._trace(trace, "JOURNAL_PREPARE", "SUCCEEDED")
        if not self.arm_store.consume(arm.arm_id, preflight.arm_sha256):
            record = self._record(
                record,
                state="CLEANUP_CONFIRMED",
                now=now,
                cleanup_confirmed=True,
                last_error_code="ARM_ALREADY_CONSUMED",
            )
            self._trace(trace, "ARM_CONSUME", "FAILED", "ARM_ALREADY_CONSUMED")
            return _outcome(
                record,
                trace,
                status="FAILED",
                error_code="ARM_ALREADY_CONSUMED",
                primary_error_code="ARM_ALREADY_CONSUMED",
            )
        record = self._record(record, state="ARM_CONSUMED", now=now, arm_consumed=True)
        self._trace(trace, "ARM_CONSUME", "SUCCEEDED")
        provider_attempted = False
        primary_error: str | None = None
        ambiguous_mutation = False
        controller_deadline = (
            self.clock.monotonic() + arm.max_controller_duration_seconds
        )

        def controller_timeout() -> int:
            remaining = int(controller_deadline - self.clock.monotonic())
            if remaining < 1:
                raise GcpExecutionError("CONTROLLER_DEADLINE_EXCEEDED")
            return remaining

        # This is a read-only preflight observation, not a capacity proof.  It
        # runs only after the pure plan/arm/quote/journal gates and before the
        # first insert.  An existing owned target blocks the run.
        try:
            existing = self.transport.list_owned(
                request, timeout_seconds=controller_timeout()
            )
            if existing:
                self._trace(
                    trace, "OWNERSHIP_VERIFY", "FAILED", "ACTIVE_RESOURCE_EXISTS"
                )
                record, _ = self._record_resilient(
                    record,
                    state="CLEANUP_CONFIRMED",
                    now=now,
                    cleanup_confirmed=True,
                    orphaned=False,
                    last_error_code="ACTIVE_RESOURCE_EXISTS",
                )
                return _outcome(
                    record,
                    trace,
                    status="FAILED",
                    error_code="ACTIVE_RESOURCE_EXISTS",
                    primary_error_code="ACTIVE_RESOURCE_EXISTS",
                )
        except GcpTransportError as error:
            self._trace(trace, "OWNERSHIP_VERIFY", "FAILED", error.code)
            record, _ = self._record_resilient(
                record,
                state="CLEANUP_CONFIRMED",
                now=now,
                cleanup_confirmed=True,
                orphaned=False,
                last_error_code=error.code,
            )
            return _outcome(
                record,
                trace,
                status="FAILED",
                error_code=error.code,
                primary_error_code=error.code,
            )
        except BaseException:
            self._trace(
                trace, "OWNERSHIP_VERIFY", "FAILED", "PREFLIGHT_OBSERVATION_FAILED"
            )
            record, _ = self._record_resilient(
                record,
                state="CLEANUP_CONFIRMED",
                now=now,
                cleanup_confirmed=True,
                orphaned=False,
                last_error_code="PREFLIGHT_OBSERVATION_FAILED",
            )
            return _outcome(
                record,
                trace,
                status="FAILED",
                error_code="PREFLIGHT_OBSERVATION_FAILED",
                primary_error_code="PREFLIGHT_OBSERVATION_FAILED",
            )

        pending_operation: GcpOperationHandle | None = None

        def mark_ambiguous_mutation() -> None:
            nonlocal ambiguous_mutation, record, primary_error
            if not provider_attempted or record.provider_operation_terminal:
                return
            ambiguous_mutation = True
            record, journal_error = self._record_resilient(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_operation_status="UNKNOWN",
                provider_operation_terminal=False,
                provider_mutation_ambiguous=True,
                last_error_code="CREATE_MUTATION_AMBIGUOUS",
            )
            if journal_error is not None:
                primary_error = primary_error or journal_error

        try:
            provider_attempted = True
            record, journal_error = self._record_resilient(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_mutation_attempted=True,
            )
            if journal_error is not None:
                primary_error = journal_error
            operation = self._validate_operation_handle(
                self.transport.insert(
                    request,
                    timeout_seconds=controller_timeout(),
                    request_id=request.insert_request_id,
                ),
                request,
                "insert",
            )
            pending_operation = operation
            record, journal_error = self._record_resilient(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_operation_id=operation.operation_id,
                provider_operation_name=operation.operation_name,
                provider_operation_kind=operation.operation_kind,
                provider_operation_status="PENDING",
                provider_operation_terminal=False,
            )
            if journal_error is not None:
                primary_error = primary_error or journal_error
            self._trace(trace, "CREATE", "SUCCEEDED")
            result = self.transport.wait_operation(
                operation, timeout_seconds=controller_timeout()
            )
            terminal = result.status in {"DONE", "ERROR"}
            record, journal_error = self._record_resilient(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_operation_status=result.status,
                provider_operation_terminal=terminal,
                last_error_code=(
                    result.error_code if result.status == "ERROR" else None
                ),
            )
            if journal_error is not None:
                primary_error = primary_error or journal_error
            if result.status != "DONE":
                primary_error = result.error_code or (
                    "CREATE_OPERATION_TIMEOUT_AMBIGUOUS"
                    if result.status == "TIMEOUT"
                    else "CREATE_OPERATION_FAILED"
                )
                self._trace(trace, "OPERATION_WAIT", "FAILED", primary_error)
                raise GcpExecutionError(primary_error)
            pending_operation = None
            self._trace(trace, "OPERATION_WAIT", "SUCCEEDED")
            observation = self.transport.get_instance(
                request, timeout_seconds=controller_timeout()
            )
            if not _owned(observation, request):
                primary_error = "OWNERSHIP_MISMATCH"
                self._trace(trace, "OWNERSHIP_VERIFY", "FAILED", primary_error)
                raise GcpExecutionError(primary_error)
            record = self._record(record, state="OWNED", now=now)
            self._trace(trace, "OWNERSHIP_VERIFY", "SUCCEEDED")
            if work is not None:
                work(observation)
            if self.clock.monotonic() >= controller_deadline:
                raise GcpExecutionError("CONTROLLER_DEADLINE_EXCEEDED")
            self._trace(trace, "WORK", "SUCCEEDED")
        except KeyboardInterrupt:
            if pending_operation is None:
                mark_ambiguous_mutation()
            primary_error = primary_error or "KEYBOARD_INTERRUPT"
            self._trace(trace, "WORK", "FAILED", primary_error)
        except BaseException as error:
            if isinstance(error, GcpTransportError) and error.operation is not None:
                pending_operation = error.operation
                record, _ = self._record_resilient(
                    record,
                    state="CREATE_SUBMITTED",
                    now=now,
                    provider_operation_id=error.operation.operation_id,
                    provider_operation_name=error.operation.operation_name,
                    provider_operation_kind=error.operation.operation_kind,
                    provider_operation_status="PENDING",
                    provider_operation_terminal=False,
                )
            elif not isinstance(error, GcpTransportError) or error.ambiguous:
                if pending_operation is None:
                    mark_ambiguous_mutation()
            if primary_error is None:
                if isinstance(error, GcpTransportError):
                    primary_error = error.code
                elif type(error).__name__ in {"CancelledError", "CancellationError"}:
                    primary_error = "CANCELLED"
                elif isinstance(error, GcpExecutionError):
                    primary_error = (
                        str(error)
                        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", str(error))
                        else "EXECUTION_FAILURE"
                    )
                else:
                    primary_error = "BENCHMARK_FAILURE"
            if not trace or trace[-1].status != "FAILED":
                self._trace(trace, "WORK", "FAILED", primary_error)
        if not provider_attempted:
            raise GcpExecutionError("provider mutation was not reached")
        record, cleanup_error = self._cleanup(
            record,
            request,
            trace,
            max_attempts=plan.cleanup_policy.max_cleanup_attempts,
            timeout_seconds=plan.timeouts.cleanup_seconds,
            pending_operation=pending_operation,
            ambiguous_mutation=ambiguous_mutation,
        )
        cleanup_state_error = cleanup_error
        terminal_error = cleanup_state_error or primary_error
        if cleanup_state_error is not None:
            status: Literal[
                "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "BLOCKED"
            ] = "FAILED"
        elif primary_error == "KEYBOARD_INTERRUPT":
            status = "INTERRUPTED"
        elif primary_error == "CANCELLED":
            status = "CANCELLED"
        elif primary_error is not None:
            status = "FAILED"
        else:
            status = "SUCCEEDED"
        return _outcome(
            record,
            trace,
            status=status,
            error_code=terminal_error,
            primary_error_code=primary_error,
            cleanup_error_code=cleanup_state_error,
        )

    def recover_cleanup(
        self, *, controller_id: str, confirmation: str
    ) -> GcpExecutionOutcome:
        if confirmation != RECOVERY_CONFIRMATION:
            raise GcpExecutionError("explicit GCP recovery confirmation is required")
        record = self.journal.load(controller_id)
        if record.state == "CLEANUP_CONFIRMED":
            raise GcpExecutionError("GCP lease is already confirmed absent")
        request = record.request
        if (
            request.labels != record.labels
            or gcp_execution_request_digest(request) != record.request_digest
        ):
            raise GcpJournalError("GCP lease request binding is invalid")
        trace: list[GcpExecutionTraceEvent] = []
        self._trace(trace, "VALIDATION", "SUCCEEDED")
        record, cleanup_error = self._cleanup(
            record,
            request,
            trace,
            max_attempts=record.max_cleanup_attempts,
            timeout_seconds=record.cleanup_timeout_seconds,
        )
        return _outcome(
            record,
            trace,
            status="SUCCEEDED" if cleanup_error is None else "FAILED",
            error_code=cleanup_error,
            cleanup_error_code=cleanup_error,
        )


def _outcome(
    record: GcpLeaseRecord,
    trace: list[GcpExecutionTraceEvent],
    *,
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "BLOCKED"],
    error_code: str | None,
    primary_error_code: str | None = None,
    cleanup_error_code: str | None = None,
) -> GcpExecutionOutcome:
    return GcpExecutionOutcome(
        schema_version=GCP_EXECUTION_RESULT_SCHEMA_VERSION,
        status=status,
        plan_id=record.plan_id,
        controller_id=record.controller_id,
        arm_id=record.arm_id,
        request_digest=record.request_digest,
        environment_digest=record.environment_digest,
        quote_digest=record.quote_digest,
        capacity_digest=record.capacity_digest,
        estimated_max_microusd=record.estimated_max_microusd,
        declared_cost_ceiling_usd=record.declared_cost_ceiling_usd,
        estimate_basis=record.estimate_basis,
        arm_consumed=record.arm_consumed,
        provider_mutation_attempted=record.provider_mutation_attempted,
        cleanup_confirmed=record.cleanup_confirmed,
        orphaned=record.orphaned,
        evidence_eligible=False,
        invoice_truth="unavailable_external_provider_invoice",
        error_code=error_code,
        primary_error_code=primary_error_code,
        cleanup_error_code=cleanup_error_code,
        journal_state=record.state,
        trace=tuple(trace),
    )


def gcp_execution_arm_schema() -> dict[str, Any]:
    schema = GcpExecutionArm.model_json_schema()
    schema["$id"] = GCP_EXECUTION_ARM_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def gcp_execution_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return additive closed schemas for the non-evidence execution contracts."""

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        ("gcp-execution-arm.schema.json", GcpExecutionArm, GCP_EXECUTION_ARM_SCHEMA_ID),
        (
            "gcp-execution-environment.schema.json",
            GcpExecutionEnvironment,
            "urn:inferdrome:gcp-execution-environment:v1",
        ),
        (
            "gcp-execution-request.schema.json",
            GcpInsertRequest,
            "urn:inferdrome:gcp-execution-request:v1",
        ),
        (
            "gcp-execution-quote.schema.json",
            GcpCostQuote,
            "urn:inferdrome:gcp-execution-quote:v1",
        ),
        (
            "gcp-execution-capacity.schema.json",
            GcpCapacityInput,
            "urn:inferdrome:gcp-execution-capacity:v1",
        ),
        (
            "gcp-execution-lease.schema.json",
            GcpLeaseRecord,
            "urn:inferdrome:gcp-execution-lease:v1",
        ),
        (
            "gcp-execution-intent-anchor.schema.json",
            GcpLeaseIntentAnchor,
            GCP_EXECUTION_ANCHOR_SCHEMA_ID,
        ),
        (
            "gcp-execution-journal-event.schema.json",
            GcpLeaseJournalEvent,
            GCP_EXECUTION_EVENT_SCHEMA_ID,
        ),
        (
            "gcp-execution-result.schema.json",
            GcpExecutionOutcome,
            GCP_EXECUTION_RESULT_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
