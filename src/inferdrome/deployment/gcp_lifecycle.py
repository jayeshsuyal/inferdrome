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
from collections.abc import Callable, Mapping, Sequence
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
GCP_EXECUTION_JOURNAL_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-lease.v1"
GCP_EXECUTION_REQUEST_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-request.v1"
GCP_EXECUTION_QUOTE_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-quote.v1"
GCP_EXECUTION_ADAPTER_ID: Final = "inferdrome.provider.gcp.compute_guarded"
GCP_EXECUTION_ADAPTER_VERSION: Final = "1.0.0"
GCP_EXECUTION_TRANSPORT_ID: Final = "inferdrome.transport.gcp.compute"
GCP_EXECUTION_TRANSPORT_VERSION: Final = "1.0.0"
GCP_MAX_EXECUTION_BYTES: Final = 524_288
GCP_MAX_JOURNAL_BYTES: Final = 262_144
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

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", code):
            code = "TRANSPORT_ERROR"
        self.code = code
        super().__init__(code)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_credential(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _CREDENTIAL_SHAPES)


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
    normalized = value.astimezone(UTC).replace(microsecond=0)
    if normalized != value.astimezone(UTC):
        raise ValueError("execution timestamps have one-second precision")
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
    controller_id: GcpControllerId
    plan_id: Sha256Digest
    arm_id: Sha256Digest
    managed_by: Literal["inferdrome-gcp-execution-v1"]
    role: Literal["benchmark-serving-boundary"]


class GcpInsertRequest(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-request.v1"]
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: GcpInstanceName
    machine_type: GcpResourceName
    architecture: Literal["amd64"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
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
    labels: GcpExecutionLabels
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tokenizer_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    runtime_engine: Literal["vllm"]
    runtime_version: Literal["0.26.0"]
    endpoint_scope: Literal["private"]
    runner_runtime_colocation: Literal["colocated"]

    @model_validator(mode="after")
    def validate_private_request(self) -> Self:
        if self.network.external_access_config != "absent":
            raise ValueError("external access configuration must be absent")
        if self.network.ip_forwarding:
            raise ValueError("IP forwarding must be disabled")
        return self


class GcpQuoteComponent(GcpExecutionModel):
    component_id: Literal["compute", "gpu", "boot_disk", "network"]
    maximum_microusd: GcpMicroUsd


class GcpCostQuote(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-quote.v1"]
    quote_id: GcpQuoteId
    plan_id: Sha256Digest
    controller_id: GcpControllerId
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: GcpResourceName
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_count: int = Field(strict=True, ge=1, le=16)
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


class GcpOperationResult(GcpExecutionModel):
    status: Literal["DONE", "ERROR", "TIMEOUT"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^op-[a-z0-9]{8,48}$")]
    instance_name: GcpInstanceName | None
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
    accelerator_count: int = Field(strict=True, ge=1, le=16)
    state: Literal["RUNNING", "TERMINATED", "NOT_FOUND"]
    labels: GcpExecutionLabels | None
    external_access_config: Literal["absent"]
    ip_forwarding: Literal[False]


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

    return GcpClock(now_fn=lambda: datetime.now(UTC), monotonic_fn=time.monotonic)


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
    labels: GcpExecutionLabels
    created_at: GcpTimestamp
    updated_at: GcpTimestamp
    provider_operation_id: Annotated[
        str | None, StringConstraints(pattern=r"^op-[a-z0-9]{8,48}$")
    ] = None
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
        return self


class GcpExecutionOutcome(GcpExecutionModel):
    schema_version: Literal["inferdrome.gcp-execution-result.v1"]
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "BLOCKED"]
    plan_id: Sha256Digest
    controller_id: GcpControllerId
    arm_id: Sha256Digest
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
    if (
        subnetwork_parts[1] != provider.project_id
        or subnetwork_parts[3] != provider.region
    ):
        raise GcpExecutionError("subnetwork region does not match selected region")
    return GcpInsertRequest(
        schema_version=GCP_EXECUTION_REQUEST_SCHEMA_VERSION,
        project_id=provider.project_id,
        region=provider.region,
        zone=provider.zone,
        instance_name=_instance_name(arm.controller_id),
        machine_type=provider.machine_type,
        architecture=provider.architecture,
        accelerator_model=provider.accelerator_model,
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
        labels=GcpExecutionLabels(
            inferdrome="inferdrome",
            controller_id=arm.controller_id,
            plan_id=arm.plan_id,
            arm_id=arm.arm_id,
            managed_by="inferdrome-gcp-execution-v1",
            role="benchmark-serving-boundary",
        ),
        model_id=plan.runtime.model_id,
        model_revision=plan.runtime.model_revision,
        tokenizer_revision=plan.runtime.tokenizer_revision,
        runtime_engine=plan.runtime.engine,
        runtime_version=plan.runtime.engine_version,
        endpoint_scope=plan.runtime.endpoint_scope,
        runner_runtime_colocation=plan.runtime.runner_runtime_colocation,
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
    if (quote.project_id, quote.region, quote.zone, quote.machine_type) != (
        request.project_id,
        request.region,
        request.zone,
        request.machine_type,
    ):
        raise GcpExecutionError("cost quote selection does not match the request")
    if (
        quote.accelerator_model != request.accelerator_model
        or quote.accelerator_count != request.accelerator_count
    ):
        raise GcpExecutionError("cost quote accelerator does not match the request")
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
            return _parse_lease_bytes(raw)
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
        with self._lock:
            for existing in self._scan():
                if (
                    existing.state != "CLEANUP_CONFIRMED"
                    and existing.plan_id == record.plan_id
                ):
                    raise GcpJournalError("an unresolved GCP lease already exists")
            raw = canonical_json_bytes(_json_value(record))
            if len(raw) > GCP_MAX_JOURNAL_BYTES:
                raise GcpJournalError("GCP journal record exceeds its bound")
            try:
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
        path = self._path_for(self._checked_root(), controller_id)
        return self._read_path(path)

    def update(self, record: GcpLeaseRecord) -> None:
        record = _strict_lease(record)
        root = self._checked_root()
        path = self._path_for(root, record.controller_id)
        stage = root / f".{record.controller_id}.{threading.get_ident()}.stage"
        raw = canonical_json_bytes(_json_value(record))
        with self._lock:
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
        return tuple(
            record
            for record in self._scan()
            if record.plan_id == plan_id and record.state != "CLEANUP_CONFIRMED"
        )


class FakeGcpComputeTransport:
    """Deterministic fake; every response is a bounded contract model."""

    def __init__(
        self,
        *,
        insert_error: str | None = None,
        insert_status: Literal["DONE", "ERROR", "TIMEOUT"] = "DONE",
        delete_error: str | None = None,
        delete_status: Literal["DONE", "ERROR", "TIMEOUT"] = "DONE",
        false_not_found: bool = False,
    ) -> None:
        self.insert_error = insert_error
        self.insert_status = insert_status
        self.delete_error = delete_error
        self.delete_status = delete_status
        self.false_not_found = false_not_found
        self.insert_calls = 0
        self.delete_calls = 0
        self.get_calls = 0
        self.list_calls = 0
        self.requests: list[GcpInsertRequest] = []
        self._owned: GcpInstanceObservation | None = None
        self._operations: dict[str, GcpOperationResult] = {}
        self._sequence = 0

    def _operation(
        self,
        kind: Literal["insert", "delete"],
        status: Literal["DONE", "ERROR", "TIMEOUT"],
        error: str | None,
        instance_name: GcpInstanceName,
    ) -> GcpOperationHandle:
        self._sequence += 1
        operation_id = f"op-{self._sequence:08d}"
        result = GcpOperationResult(
            status=status,
            operation_id=operation_id,
            instance_name=instance_name,
            error_code=error if status == "ERROR" else None,
        )
        self._operations[operation_id] = result
        return GcpOperationHandle(operation_id=operation_id, operation_kind=kind)

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del timeout_seconds, request_id
        self.insert_calls += 1
        self.requests.append(request)
        if self.insert_error is not None:
            raise GcpTransportError(self.insert_error)
        operation = self._operation(
            "insert",
            self.insert_status,
            "INSERT_FAILED" if self.insert_status == "ERROR" else None,
            request.instance_name,
        )
        if self.insert_status == "DONE":
            self._owned = GcpInstanceObservation(
                instance_name=request.instance_name,
                project_id=request.project_id,
                zone=request.zone,
                machine_type=request.machine_type,
                accelerator_model=request.accelerator_model,
                accelerator_count=request.accelerator_count,
                state="RUNNING",
                labels=request.labels,
                external_access_config="absent",
                ip_forwarding=False,
            )
        return operation

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        del timeout_seconds
        try:
            return self._operations[operation.operation_id]
        except KeyError:
            raise GcpTransportError("OPERATION_UNKNOWN") from None

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        del timeout_seconds
        self.get_calls += 1
        if self.false_not_found:
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
        if self._owned is None:
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
        return self._owned

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        del timeout_seconds
        self.list_calls += 1
        if self._owned is None:
            return ()
        if self._owned.labels == request.labels:
            return (self._owned,)
        return ()

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del timeout_seconds, request_id
        self.delete_calls += 1
        if self.delete_error is not None:
            raise GcpTransportError(self.delete_error)
        operation = self._operation(
            "delete",
            self.delete_status,
            "DELETE_FAILED" if self.delete_status == "ERROR" else None,
            request.instance_name,
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
        and observation.accelerator_count == request.accelerator_count
        and observation.labels == request.labels
        and observation.external_access_config == "absent"
        and observation.ip_forwarding is False
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
    ) -> tuple[GcpLeaseRecord, str | None]:
        now = _timestamp(self.clock.now())
        current = self._record(record, state="CLEANUP_PENDING", now=now)
        cleanup_error: str | None = None
        cleanup_deadline = self.clock.monotonic() + timeout_seconds

        def remaining_timeout() -> int:
            remaining = int(cleanup_deadline - self.clock.monotonic())
            if remaining < 1:
                raise GcpTransportError("CLEANUP_DEADLINE_EXCEEDED")
            return remaining

        for _ in range(max_attempts):
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
                    return self._record(
                        current,
                        state="CLEANUP_CONFIRMED",
                        now=now,
                        cleanup_confirmed=True,
                        orphaned=False,
                        last_error_code=None,
                    ), None
                cleanup_error = confirmation_error or "CLEANUP_UNCONFIRMED"
                self._trace(trace, "FINAL_CONFIRMATION", "FAILED", cleanup_error)
                continue
            if observation.state != "NOT_FOUND" and not _owned(observation, request):
                cleanup_error = "OWNERSHIP_MISMATCH"
                continue
            if owned and any(not _owned(item, request) for item in owned):
                cleanup_error = "OWNERSHIP_MISMATCH"
                continue
            try:
                operation = self.transport.delete(
                    request,
                    timeout_seconds=remaining_timeout(),
                    request_id=f"delete-{request.labels.controller_id}",
                )
                current = self._record(
                    current,
                    state="CLEANUP_PENDING",
                    now=now,
                    provider_operation_id=operation.operation_id,
                    delete_attempts=current.delete_attempts + 1,
                )
                result = self.transport.wait_operation(
                    operation, timeout_seconds=remaining_timeout()
                )
                if result.status != "DONE":
                    cleanup_error = result.error_code or "DELETE_UNCONFIRMED"
                    continue
            except GcpTransportError as error:
                cleanup_error = error.code
                continue
            except KeyboardInterrupt:
                cleanup_error = "CLEANUP_INTERRUPTED"
                continue
            except BaseException:
                cleanup_error = "CLEANUP_EXCEPTION"
                continue
            try:
                confirmed, confirmation_error = self._final_confirm(
                    request, timeout_seconds=remaining_timeout()
                )
            except GcpTransportError as error:
                cleanup_error = error.code
                self._trace(trace, "FINAL_CONFIRMATION", "FAILED", cleanup_error)
                continue
            if confirmed:
                self._trace(trace, "DELETE", "SUCCEEDED")
                self._trace(trace, "FINAL_CONFIRMATION", "SUCCEEDED")
                return self._record(
                    current,
                    state="CLEANUP_CONFIRMED",
                    now=now,
                    cleanup_confirmed=True,
                    orphaned=False,
                    last_error_code=None,
                ), None
            cleanup_error = confirmation_error or "CLEANUP_UNCONFIRMED"
            self._trace(trace, "FINAL_CONFIRMATION", "FAILED", cleanup_error)
        self._trace(trace, "DELETE", "FAILED", cleanup_error or "CLEANUP_UNCONFIRMED")
        return self._record(
            current,
            state="ORPHANED",
            now=now,
            cleanup_confirmed=False,
            orphaned=True,
            last_error_code=cleanup_error or "CLEANUP_UNCONFIRMED",
        ), cleanup_error or "CLEANUP_UNCONFIRMED"

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
            labels=request.labels,
            created_at=now,
            updated_at=now,
            arm_consumed=False,
            provider_mutation_attempted=False,
            cleanup_confirmed=False,
            orphaned=False,
            delete_attempts=0,
            max_cleanup_attempts=plan.cleanup_policy.max_cleanup_attempts,
            cleanup_timeout_seconds=plan.timeouts.cleanup_seconds,
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
        controller_deadline = (
            self.clock.monotonic() + arm.max_controller_duration_seconds
        )

        def controller_timeout() -> int:
            remaining = int(controller_deadline - self.clock.monotonic())
            if remaining < 1:
                raise GcpExecutionError("CONTROLLER_DEADLINE_EXCEEDED")
            return remaining

        try:
            provider_attempted = True
            record = self._record(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_mutation_attempted=True,
            )
            operation = self.transport.insert(
                request,
                timeout_seconds=controller_timeout(),
                request_id=f"insert-{arm.controller_id}",
            )
            record = self._record(
                record,
                state="CREATE_SUBMITTED",
                now=now,
                provider_operation_id=operation.operation_id,
            )
            self._trace(trace, "CREATE", "SUCCEEDED")
            result = self.transport.wait_operation(
                operation, timeout_seconds=controller_timeout()
            )
            if result.status != "DONE":
                primary_error = result.error_code or "CREATE_OPERATION_UNCONFIRMED"
                self._trace(trace, "OPERATION_WAIT", "FAILED", primary_error)
                raise GcpExecutionError(primary_error)
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
            primary_error = primary_error or "KEYBOARD_INTERRUPT"
            self._trace(trace, "WORK", "FAILED", primary_error)
        except BaseException as error:
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
