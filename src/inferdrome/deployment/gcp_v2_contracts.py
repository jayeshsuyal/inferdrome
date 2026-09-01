"""Additive, local-only v0.2 activation and cleanup-recovery contracts.

These contracts deliberately sit beside the frozen v1 execution arm and
lease.  They do not make a provider call, construct an SDK client, discover
credentials, or grant a default mutation path.  Their purpose is to make a
future, separately enabled path prove its exact authority and horizons before
it may cross an injected transport boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

from inferdrome.deployment.gcp import GcpTimestamp
from inferdrome.deployment.gcp_lifecycle import (
    GcpExecutionError,
    GcpExecutionLabels,
    GcpExecutionModel,
    GcpExecutionPreflight,
    GcpInsertRequest,
    GcpLeaseRecord,
    _parse_timestamp,
    _preflight_json,
    _timestamp,
    gcp_capacity_digest,
    gcp_cost_quote_digest,
    gcp_execution_environment_digest,
    gcp_execution_request_digest,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import GcpV2DiskCleanupBinding
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_V2_ACTIVATION_DEADLINE_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-activation-deadline.v2"
)
GCP_V2_MUTATION_CAPABILITY_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-mutation-capability.v2"
)
GCP_V2_STARTUP_PROJECTION_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-startup-projection.v2"
)
GCP_CLEANUP_RECOVERY_AUTHORIZATION_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-cleanup-recovery-authorization.v2"
)
GCP_V2_ACTIVATION_DEADLINE_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-activation-deadline:v2"
)
GCP_V2_MUTATION_CAPABILITY_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-mutation-capability:v2"
)
GCP_V2_STARTUP_PROJECTION_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-startup-projection:v2"
)
GCP_CLEANUP_RECOVERY_AUTHORIZATION_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-cleanup-recovery-authorization:v2"
)
GCP_CLEANUP_RECOVERY_CONFIRMATION: Final = "AUTHORIZE_GCP_V2_EXACT_CLEANUP"


class GcpV2ContractError(GcpExecutionError):
    """A bounded local v0.2 authority or deadline validation error."""

    def __init__(self, code: str) -> None:
        super().__init__(code if code.isupper() else "V2_CONTRACT_ERROR")


def _model_value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("v2 GCP contract must serialize as an object")
    return value


def gcp_v2_execution_payload_digest(execution_payload: bytes) -> Sha256Digest:
    """Return the domain-separated digest of opaque execution-payload bytes.

    This helper does not parse, execute, persist, transfer, or otherwise assign
    semantics to the bytes.  It exists solely so a future, separately reviewed
    producer can bind an exact byte sequence to the v2 provider boundary.
    """

    if type(execution_payload) is not bytes:
        raise GcpV2ContractError("EXECUTION_PAYLOAD_BYTES_REQUIRED")
    return digest_bytes(DigestDomain.GCP_EXECUTION_PAYLOAD, execution_payload)


class GcpV2StartupProjectionPayload(GcpExecutionModel):
    """Opaque v2 payload binding for a frozen v1 request and environment.

    ``startup_script_digest`` remains an independently preserved v1-compatible
    fact.  It is not interpreted as, or substituted for, the opaque
    ``execution_payload_digest``.  This model deliberately defines no command,
    input-transfer, credential, evidence, or execution semantics.
    """

    schema_version: Literal["inferdrome.gcp-startup-projection.v2"]
    projection_kind: Literal["opaque_execution_payload_binding"]
    request_digest: Sha256Digest
    environment_digest: Sha256Digest
    startup_script_digest: Sha256Digest | None
    execution_payload_digest: Sha256Digest


class GcpV2StartupProjection(GcpV2StartupProjectionPayload):
    """Content-addressed local payload binding, not a signature or authority."""

    projection_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.projection_id != gcp_v2_startup_projection_id(self):
            raise ValueError("startup projection identity does not match payload")
        return self


def canonical_gcp_v2_startup_projection_payload_bytes(
    projection: GcpV2StartupProjection | GcpV2StartupProjectionPayload,
) -> bytes:
    """Encode the opaque payload-binding fields without its local identity."""

    value = _model_value(projection)
    value.pop("projection_id", None)
    return canonical_json_bytes(value)


def gcp_v2_startup_projection_id(
    projection: GcpV2StartupProjection | GcpV2StartupProjectionPayload,
) -> Sha256Digest:
    """Return the content-addressed identity of one startup projection."""

    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_v2_startup_projection_payload_bytes(projection),
    )


def canonical_gcp_v2_startup_projection_bytes(
    projection: GcpV2StartupProjection,
) -> bytes:
    """Encode the complete startup projection canonically."""

    return canonical_json_bytes(_model_value(projection))


def gcp_v2_startup_projection_digest(
    projection: GcpV2StartupProjection,
) -> Sha256Digest:
    """Return the canonical local digest of one startup projection."""

    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_v2_startup_projection_bytes(projection),
    )


def issue_gcp_v2_startup_projection(
    *,
    request: GcpInsertRequest,
    execution_payload: bytes,
) -> GcpV2StartupProjection:
    """Bind opaque bytes to the exact frozen request and environment digests.

    The byte sequence is consumed only to derive a domain-separated digest; it
    is never retained in the projection or any error message.
    """

    try:
        request = GcpInsertRequest.model_validate_json(
            canonical_json_bytes(_model_value(request))
        )
        execution_payload_digest = gcp_v2_execution_payload_digest(
            execution_payload
        )
    except (TypeError, ValidationError, ValueError):
        raise GcpV2ContractError("STARTUP_PROJECTION_INPUT_INVALID") from None
    payload = GcpV2StartupProjectionPayload(
        schema_version=GCP_V2_STARTUP_PROJECTION_SCHEMA_VERSION,
        projection_kind="opaque_execution_payload_binding",
        request_digest=gcp_execution_request_digest(request),
        environment_digest=gcp_execution_environment_digest(request),
        startup_script_digest=request.startup_script_digest,
        execution_payload_digest=execution_payload_digest,
    )
    return GcpV2StartupProjection(
        **_model_value(payload),
        projection_id=gcp_v2_startup_projection_id(payload),
    )


def parse_gcp_v2_startup_projection_json(
    payload: str | bytes,
) -> GcpV2StartupProjection:
    """Parse one strict, content-addressed opaque startup projection."""

    try:
        _preflight_json(payload, kind="GCP v2 startup projection")
        return GcpV2StartupProjection.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpV2ContractError("STARTUP_PROJECTION_INVALID") from None


def validate_gcp_v2_startup_projection(
    projection: GcpV2StartupProjection,
    *,
    request: GcpInsertRequest,
    execution_payload_digest: Sha256Digest,
) -> GcpV2StartupProjection:
    """Fail closed unless request, environment, and opaque payload all match."""

    try:
        parsed = GcpV2StartupProjection.model_validate_json(
            canonical_gcp_v2_startup_projection_bytes(projection)
        )
        request = GcpInsertRequest.model_validate_json(
            canonical_json_bytes(_model_value(request))
        )
    except (ValidationError, ValueError, TypeError):
        raise GcpV2ContractError("STARTUP_PROJECTION_INVALID") from None
    expected = {
        "request_digest": gcp_execution_request_digest(request),
        "environment_digest": gcp_execution_environment_digest(request),
        "startup_script_digest": request.startup_script_digest,
        "execution_payload_digest": execution_payload_digest,
    }
    if any(getattr(parsed, key) != value for key, value in expected.items()):
        raise GcpV2ContractError("STARTUP_PROJECTION_BINDING_MISMATCH")
    return parsed


class GcpV2ActivationDeadlinePayload(GcpExecutionModel):
    """Exact setup/authorization/runtime/cleanup horizons for one v2 lease.

    ``setup_deadline_at`` is the only deadline that must hold at activation.
    The provider runtime begins at the actual activation edge and is bounded by
    the independently durable watchdog horizon.  This avoids treating setup
    time as if it had already consumed the provider runtime while retaining a
    finite, explicit authorization window.
    """

    schema_version: Literal["inferdrome.gcp-activation-deadline.v2"]
    contract_kind: Literal["v2_activation_deadline"]
    provider: Literal["gcp-compute-engine"]
    plan_id: Sha256Digest
    arm_id: Sha256Digest
    arm_sha256: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    quote_digest: Sha256Digest
    capacity_digest: Sha256Digest
    rate_basis_digest: Sha256Digest
    cost_guard_digest: Sha256Digest
    controller_id: str = Field(pattern=r"^ctl-[a-z0-9]{8,24}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]$")
    zone: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]-[a-z]$")
    instance_name: str = Field(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    labels: GcpExecutionLabels
    machine_type: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    accelerator_count: Literal[1]
    boot_image_name: str = Field(min_length=1, max_length=256)
    boot_image_provider_id: int = Field(strict=True, ge=1, le=10**19)
    boot_image_digest: Sha256Digest
    runner_image_digest: Sha256Digest
    serving_runtime_image_digest: Sha256Digest
    issued_at: GcpTimestamp
    authorization_expires_at: GcpTimestamp
    setup_margin_seconds: int = Field(strict=True, ge=1, le=3_600)
    setup_deadline_at: GcpTimestamp
    provider_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    quoted_cleanup_horizon_seconds: int = Field(strict=True, ge=1, le=7_200)
    watchdog_cleanup_horizon_seconds: int = Field(strict=True, ge=1, le=7_200)
    watchdog_deadline_at: GcpTimestamp

    @model_validator(mode="after")
    def validate_horizons(self) -> Self:
        issued = _parse_timestamp(self.issued_at)
        authorization = _parse_timestamp(self.authorization_expires_at)
        setup = _parse_timestamp(self.setup_deadline_at)
        watchdog = _parse_timestamp(self.watchdog_deadline_at)
        if not (issued < setup <= authorization):
            raise ValueError("activation authorization and setup horizons conflict")
        if int((setup - issued).total_seconds()) != self.setup_margin_seconds:
            raise ValueError("activation setup margin is inconsistent")
        if self.watchdog_cleanup_horizon_seconds < self.quoted_cleanup_horizon_seconds:
            raise ValueError("watchdog cleanup horizon is shorter than quoted tail")
        required_watchdog = setup + timedelta(
            seconds=(
                self.provider_runtime_seconds + self.watchdog_cleanup_horizon_seconds
            )
        )
        if watchdog != required_watchdog:
            raise ValueError("watchdog horizon must exactly match its bounded contract")
        return self


class GcpV2ActivationDeadline(GcpV2ActivationDeadlinePayload):
    """Content-addressed local binding, not a signature or operator proof."""

    contract_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.contract_id != gcp_v2_activation_deadline_id(self):
            raise ValueError("activation deadline identity does not match payload")
        return self


def canonical_gcp_v2_activation_deadline_payload_bytes(
    contract: GcpV2ActivationDeadline | GcpV2ActivationDeadlinePayload,
) -> bytes:
    value = _model_value(contract)
    value.pop("contract_id", None)
    return canonical_json_bytes(value)


def gcp_v2_activation_deadline_id(
    contract: GcpV2ActivationDeadline | GcpV2ActivationDeadlinePayload,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_v2_activation_deadline_payload_bytes(contract),
    )


def canonical_gcp_v2_activation_deadline_bytes(
    contract: GcpV2ActivationDeadline,
) -> bytes:
    return canonical_json_bytes(_model_value(contract))


def gcp_v2_activation_deadline_digest(
    contract: GcpV2ActivationDeadline,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_v2_activation_deadline_bytes(contract),
    )


def issue_gcp_v2_activation_deadline(
    *,
    preflight: GcpExecutionPreflight,
    startup_projection: GcpV2StartupProjection,
    rate_basis_digest: Sha256Digest,
    cost_guard_digest: Sha256Digest,
    issued_at: datetime,
    authorization_expires_at: datetime,
    setup_margin_seconds: int,
    watchdog_cleanup_horizon_seconds: int | None = None,
) -> GcpV2ActivationDeadline:
    """Issue an exact local deadline contract from already-read-only inputs."""

    try:
        issued_text = _timestamp(issued_at)
        authorization_text = _timestamp(authorization_expires_at)
        setup_deadline = issued_at + timedelta(seconds=setup_margin_seconds)
        setup_text = _timestamp(setup_deadline)
    except (TypeError, ValueError):
        raise GcpV2ContractError("ACTIVATION_TIME_INVALID") from None
    request = preflight.request
    try:
        startup_projection = validate_gcp_v2_startup_projection(
            startup_projection,
            request=request,
            execution_payload_digest=startup_projection.execution_payload_digest,
        )
    except GcpExecutionError as error:
        raise GcpV2ContractError(str(error)) from None
    quoted_tail = (
        preflight.quote.billable_duration_seconds - request.provider_max_runtime_seconds
    )
    if quoted_tail < 1:
        raise GcpV2ContractError("ACTIVATION_QUOTED_CLEANUP_INVALID")
    cleanup_horizon = (
        quoted_tail
        if watchdog_cleanup_horizon_seconds is None
        else watchdog_cleanup_horizon_seconds
    )
    watchdog_deadline = setup_deadline + timedelta(
        seconds=request.provider_max_runtime_seconds + cleanup_horizon
    )
    payload = GcpV2ActivationDeadlinePayload(
        schema_version=GCP_V2_ACTIVATION_DEADLINE_SCHEMA_VERSION,
        contract_kind="v2_activation_deadline",
        provider="gcp-compute-engine",
        plan_id=preflight.arm.plan_id,
        arm_id=preflight.arm.arm_id,
        arm_sha256=preflight.arm.plan_sha256,
        request_digest=gcp_execution_request_digest(request),
        startup_projection_digest=gcp_v2_startup_projection_digest(
            startup_projection
        ),
        execution_payload_digest=startup_projection.execution_payload_digest,
        quote_digest=gcp_cost_quote_digest(preflight.quote),
        capacity_digest=gcp_capacity_digest(preflight.capacity),
        rate_basis_digest=rate_basis_digest,
        cost_guard_digest=cost_guard_digest,
        controller_id=preflight.arm.controller_id,
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        instance_name=request.instance_name,
        labels=request.labels,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_provider_type=request.accelerator_provider_type,
        accelerator_count=1,
        boot_image_name=request.boot_image.image_name,
        boot_image_provider_id=request.boot_image.provider_image_id,
        boot_image_digest=request.boot_image.digest,
        runner_image_digest=request.runner_image.digest,
        serving_runtime_image_digest=request.serving_runtime_image.digest,
        issued_at=issued_text,
        authorization_expires_at=authorization_text,
        setup_margin_seconds=setup_margin_seconds,
        setup_deadline_at=setup_text,
        provider_runtime_seconds=request.provider_max_runtime_seconds,
        quoted_cleanup_horizon_seconds=quoted_tail,
        watchdog_cleanup_horizon_seconds=cleanup_horizon,
        watchdog_deadline_at=_timestamp(watchdog_deadline),
    )
    return GcpV2ActivationDeadline(
        **_model_value(payload), contract_id=gcp_v2_activation_deadline_id(payload)
    )


def parse_gcp_v2_activation_deadline_json(
    payload: str | bytes,
) -> GcpV2ActivationDeadline:
    try:
        _preflight_json(payload, kind="GCP v2 activation deadline")
        return GcpV2ActivationDeadline.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpV2ContractError("ACTIVATION_CONTRACT_INVALID") from None


def validate_gcp_v2_activation_deadline(
    contract: GcpV2ActivationDeadline,
    *,
    preflight: GcpExecutionPreflight,
    startup_projection: GcpV2StartupProjection,
    rate_basis_digest: Sha256Digest,
    cost_guard_digest: Sha256Digest,
) -> GcpV2ActivationDeadline:
    """Verify static v2 binding without treating it as launch authority."""

    try:
        parsed = GcpV2ActivationDeadline.model_validate_json(
            canonical_gcp_v2_activation_deadline_bytes(contract)
        )
    except (ValidationError, ValueError):
        raise GcpV2ContractError("ACTIVATION_CONTRACT_INVALID") from None
    request = preflight.request
    try:
        startup_projection = validate_gcp_v2_startup_projection(
            startup_projection,
            request=request,
            execution_payload_digest=startup_projection.execution_payload_digest,
        )
    except GcpExecutionError as error:
        raise GcpV2ContractError(str(error)) from None
    expected = {
        "provider": "gcp-compute-engine",
        "plan_id": preflight.arm.plan_id,
        "arm_id": preflight.arm.arm_id,
        "arm_sha256": preflight.arm.plan_sha256,
        "request_digest": gcp_execution_request_digest(request),
        "startup_projection_digest": gcp_v2_startup_projection_digest(
            startup_projection
        ),
        "execution_payload_digest": startup_projection.execution_payload_digest,
        "quote_digest": gcp_cost_quote_digest(preflight.quote),
        "capacity_digest": gcp_capacity_digest(preflight.capacity),
        "rate_basis_digest": rate_basis_digest,
        "cost_guard_digest": cost_guard_digest,
        "controller_id": preflight.arm.controller_id,
        "project_id": request.project_id,
        "region": request.region,
        "zone": request.zone,
        "instance_name": request.instance_name,
        "labels": request.labels,
        "machine_type": request.machine_type,
        "accelerator_model": request.accelerator_model,
        "accelerator_provider_type": request.accelerator_provider_type,
        "accelerator_count": request.accelerator_count,
        "boot_image_name": request.boot_image.image_name,
        "boot_image_provider_id": request.boot_image.provider_image_id,
        "boot_image_digest": request.boot_image.digest,
        "runner_image_digest": request.runner_image.digest,
        "serving_runtime_image_digest": request.serving_runtime_image.digest,
        "provider_runtime_seconds": request.provider_max_runtime_seconds,
    }
    if any(getattr(parsed, key) != value for key, value in expected.items()):
        raise GcpV2ContractError("ACTIVATION_CONTRACT_BINDING_MISMATCH")
    return parsed


class GcpV2MutationCapability(GcpExecutionModel):
    """One exact in-memory future-create capability; it is not a credential."""

    schema_version: Literal["inferdrome.gcp-mutation-capability.v2"]
    capability_kind: Literal["one_exact_future_create"]
    activation_contract_digest: Sha256Digest
    approval_digest: Sha256Digest
    controller_id: str = Field(pattern=r"^ctl-[a-z0-9]{8,24}$")
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]$")
    zone: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]-[a-z]$")
    instance_name: str = Field(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    labels: GcpExecutionLabels
    activated_at: GcpTimestamp
    provider_runtime_deadline_at: GcpTimestamp
    watchdog_cleanup_deadline_at: GcpTimestamp
    capability_id: Sha256Digest

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if not (
            _parse_timestamp(self.activated_at)
            < _parse_timestamp(self.provider_runtime_deadline_at)
            < _parse_timestamp(self.watchdog_cleanup_deadline_at)
        ):
            raise ValueError("mutation capability horizons are inconsistent")
        if self.capability_id != gcp_v2_mutation_capability_id(self):
            raise ValueError("mutation capability identity does not match payload")
        return self


def canonical_gcp_v2_mutation_capability_payload_bytes(
    capability: GcpV2MutationCapability,
) -> bytes:
    value = _model_value(capability)
    value.pop("capability_id", None)
    return canonical_json_bytes(value)


def gcp_v2_mutation_capability_id(capability: GcpV2MutationCapability) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_v2_mutation_capability_payload_bytes(capability),
    )


def issue_gcp_v2_mutation_capability(
    *,
    contract: GcpV2ActivationDeadline,
    approval_digest: Sha256Digest,
    now: datetime,
) -> GcpV2MutationCapability:
    """Derive one capability only while setup/auth/watchdog horizons hold."""

    try:
        activated_at = _timestamp(now)
        now_value = _parse_timestamp(activated_at)
    except ValueError:
        raise GcpV2ContractError("ACTIVATION_TIME_INVALID") from None
    if now_value >= _parse_timestamp(contract.authorization_expires_at):
        raise GcpV2ContractError("ACTIVATION_AUTHORIZATION_EXPIRED")
    if now_value >= _parse_timestamp(contract.setup_deadline_at):
        raise GcpV2ContractError("ACTIVATION_SETUP_HORIZON_EXPIRED")
    provider_deadline = now_value + timedelta(
        seconds=contract.provider_runtime_seconds
    )
    cleanup_deadline = provider_deadline + timedelta(
        seconds=contract.watchdog_cleanup_horizon_seconds
    )
    if cleanup_deadline > _parse_timestamp(contract.watchdog_deadline_at):
        raise GcpV2ContractError("ACTIVATION_WATCHDOG_HORIZON_INSUFFICIENT")
    payload: dict[str, Any] = {
        "schema_version": GCP_V2_MUTATION_CAPABILITY_SCHEMA_VERSION,
        "capability_kind": "one_exact_future_create",
        "activation_contract_digest": gcp_v2_activation_deadline_digest(contract),
        "approval_digest": approval_digest,
        "controller_id": contract.controller_id,
        "request_digest": contract.request_digest,
        "startup_projection_digest": contract.startup_projection_digest,
        "execution_payload_digest": contract.execution_payload_digest,
        "project_id": contract.project_id,
        "region": contract.region,
        "zone": contract.zone,
        "instance_name": contract.instance_name,
        "labels": _model_value(contract.labels)
        if isinstance(contract.labels, GcpExecutionModel)
        else contract.labels,
        "activated_at": activated_at,
        "provider_runtime_deadline_at": _timestamp(provider_deadline),
        "watchdog_cleanup_deadline_at": _timestamp(cleanup_deadline),
    }
    provisional = GcpV2MutationCapability.model_construct(
        **{**payload, "capability_id": "sha256:" + "0" * 64}
    )
    payload["capability_id"] = gcp_v2_mutation_capability_id(provisional)
    return GcpV2MutationCapability.model_validate_json(canonical_json_bytes(payload))


GcpCleanupAction = Literal[
    "reconcile_operation",
    "get_exact_instance",
    "list_exact_label_inventory",
    "delete_exact_instance",
    "get_exact_disk",
    "delete_exact_disk",
    "confirm_absence",
]
_CLEANUP_ACTIONS: Final[tuple[GcpCleanupAction, ...]] = (
    "reconcile_operation",
    "get_exact_instance",
    "list_exact_label_inventory",
    "delete_exact_instance",
    "get_exact_disk",
    "delete_exact_disk",
    "confirm_absence",
)


class GcpCleanupRecoveryAuthorizationPayload(GcpExecutionModel):
    """Exact cleanup-only authority that cannot authorize a future create."""

    schema_version: Literal["inferdrome.gcp-cleanup-recovery-authorization.v2"]
    authorization_kind: Literal["exact_cleanup_only_recovery"]
    operator_identity: str = Field(pattern=r"^operator-[a-z0-9][a-z0-9_-]{2,61}$")
    operator_confirmation: Literal["AUTHORIZE_GCP_V2_EXACT_CLEANUP"]
    create_authority: Literal[False]
    provider: Literal["gcp-compute-engine"]
    approval_digest: Sha256Digest
    lease_intent_anchor_digest: Sha256Digest
    arm_id: Sha256Digest
    plan_id: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    controller_id: str = Field(pattern=r"^ctl-[a-z0-9]{8,24}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]$")
    zone: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]-[a-z]$")
    instance_name: str = Field(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    boot_disk_name: str = Field(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    labels: GcpExecutionLabels
    disk_cleanup_binding: GcpV2DiskCleanupBinding
    allowed_actions: tuple[GcpCleanupAction, ...]
    max_cleanup_attempts: int = Field(strict=True, ge=1, le=3)
    cleanup_timeout_seconds: int = Field(strict=True, ge=1, le=3_600)
    lease_created_at: GcpTimestamp
    watchdog_cleanup_deadline_at: GcpTimestamp
    recovery_deadline_at: GcpTimestamp
    issued_at: GcpTimestamp
    expires_at: GcpTimestamp

    @model_validator(mode="after")
    def validate_cleanup_authorization(self) -> Self:
        disk_binding = self.disk_cleanup_binding
        if (
            disk_binding.request_digest != self.request_digest
            or disk_binding.project_id != self.project_id
            or disk_binding.zone != self.zone
            or disk_binding.controller_id != self.controller_id
            or disk_binding.instance_name != self.instance_name
            or disk_binding.labels != self.labels
            or disk_binding.disk.disk_name != self.boot_disk_name
        ):
            raise ValueError("cleanup authorization disk binding is inconsistent")
        if self.allowed_actions != _CLEANUP_ACTIONS:
            raise ValueError("cleanup authorization actions are incomplete or broad")
        issued = _parse_timestamp(self.issued_at)
        expires = _parse_timestamp(self.expires_at)
        lease_created = _parse_timestamp(self.lease_created_at)
        watchdog_cleanup_deadline = _parse_timestamp(
            self.watchdog_cleanup_deadline_at
        )
        recovery_deadline = _parse_timestamp(self.recovery_deadline_at)
        if (
            watchdog_cleanup_deadline != recovery_deadline
            or not (lease_created <= issued < expires <= watchdog_cleanup_deadline)
        ):
            raise ValueError("cleanup authorization expiry is invalid")
        return self


class GcpCleanupRecoveryAuthorization(GcpCleanupRecoveryAuthorizationPayload):
    """Content-addressed local cleanup binding, not an operator signature."""

    authorization_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.authorization_id != gcp_cleanup_recovery_authorization_id(self):
            raise ValueError("cleanup authorization identity does not match payload")
        return self


def canonical_gcp_cleanup_recovery_authorization_payload_bytes(
    authorization: GcpCleanupRecoveryAuthorization
    | GcpCleanupRecoveryAuthorizationPayload,
) -> bytes:
    value = _model_value(authorization)
    value.pop("authorization_id", None)
    return canonical_json_bytes(value)


def gcp_cleanup_recovery_authorization_id(
    authorization: GcpCleanupRecoveryAuthorization
    | GcpCleanupRecoveryAuthorizationPayload,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_cleanup_recovery_authorization_payload_bytes(authorization),
    )


def canonical_gcp_cleanup_recovery_authorization_bytes(
    authorization: GcpCleanupRecoveryAuthorization,
) -> bytes:
    return canonical_json_bytes(_model_value(authorization))


def issue_gcp_cleanup_recovery_authorization(
    *,
    record: GcpLeaseRecord,
    approval_digest: Sha256Digest,
    startup_projection_digest: Sha256Digest,
    execution_payload_digest: Sha256Digest,
    disk_cleanup_binding: GcpV2DiskCleanupBinding,
    operator_identity: str,
    issued_at: datetime,
    expires_at: datetime,
    watchdog_cleanup_deadline_at: datetime,
    confirmation: str,
) -> GcpCleanupRecoveryAuthorization:
    """Issue an exact recovery-only authorization; it never grants create."""

    if confirmation != GCP_CLEANUP_RECOVERY_CONFIRMATION:
        raise GcpV2ContractError("CLEANUP_RECOVERY_CONFIRMATION_REQUIRED")
    try:
        disk_cleanup_binding = GcpV2DiskCleanupBinding.model_validate_json(
            canonical_json_bytes(_model_value(disk_cleanup_binding))
        )
        lease_created_at = _parse_timestamp(record.created_at)
        watchdog_cleanup_deadline = _parse_timestamp(
            _timestamp(watchdog_cleanup_deadline_at)
        )
        issued_value = _parse_timestamp(_timestamp(issued_at))
        expires_value = _parse_timestamp(_timestamp(expires_at))
    except (ValidationError, ValueError):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_INVALID") from None
    if not (
        lease_created_at
        <= issued_value
        < expires_value
        <= watchdog_cleanup_deadline
    ):
        raise GcpV2ContractError("CLEANUP_RECOVERY_HORIZON_INVALID")
    if (
        disk_cleanup_binding.request_digest != record.request_digest
        or disk_cleanup_binding.project_id != record.project_id
        or disk_cleanup_binding.zone != record.zone
        or disk_cleanup_binding.controller_id != record.controller_id
        or disk_cleanup_binding.instance_name != record.instance_name
        or disk_cleanup_binding.labels != record.labels
    ):
        raise GcpV2ContractError("CLEANUP_RECOVERY_DISK_BINDING_MISMATCH")
    payload = GcpCleanupRecoveryAuthorizationPayload(
        schema_version=GCP_CLEANUP_RECOVERY_AUTHORIZATION_SCHEMA_VERSION,
        authorization_kind="exact_cleanup_only_recovery",
        operator_identity=operator_identity,
        operator_confirmation=GCP_CLEANUP_RECOVERY_CONFIRMATION,
        create_authority=False,
        provider="gcp-compute-engine",
        approval_digest=approval_digest,
        lease_intent_anchor_digest=record.intent_anchor_digest,
        arm_id=record.arm_id,
        plan_id=record.plan_id,
        request_digest=record.request_digest,
        startup_projection_digest=startup_projection_digest,
        execution_payload_digest=execution_payload_digest,
        controller_id=record.controller_id,
        project_id=record.project_id,
        region=record.region,
        zone=record.zone,
        instance_name=record.instance_name,
        boot_disk_name=disk_cleanup_binding.disk.disk_name,
        labels=record.labels,
        disk_cleanup_binding=disk_cleanup_binding,
        allowed_actions=_CLEANUP_ACTIONS,
        max_cleanup_attempts=record.max_cleanup_attempts,
        cleanup_timeout_seconds=record.cleanup_timeout_seconds,
        lease_created_at=_timestamp(lease_created_at),
        watchdog_cleanup_deadline_at=_timestamp(watchdog_cleanup_deadline),
        recovery_deadline_at=_timestamp(watchdog_cleanup_deadline),
        issued_at=_timestamp(issued_at),
        expires_at=_timestamp(expires_at),
    )
    return GcpCleanupRecoveryAuthorization.model_validate_json(
        canonical_json_bytes(
            {
                **_model_value(payload),
                "authorization_id": gcp_cleanup_recovery_authorization_id(payload),
            }
        )
    )


def parse_gcp_cleanup_recovery_authorization_json(
    payload: str | bytes,
) -> GcpCleanupRecoveryAuthorization:
    try:
        _preflight_json(payload, kind="GCP cleanup recovery authorization")
        return GcpCleanupRecoveryAuthorization.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_INVALID") from None


def validate_gcp_cleanup_recovery_authorization(
    authorization: GcpCleanupRecoveryAuthorization,
    *,
    record: GcpLeaseRecord,
    approval_digest: Sha256Digest,
    startup_projection_digest: Sha256Digest,
    execution_payload_digest: Sha256Digest,
    now: datetime,
    watchdog_cleanup_deadline_at: datetime,
) -> GcpCleanupRecoveryAuthorization:
    """Fail closed unless recovery authority exactly matches one original lease."""

    try:
        parsed = GcpCleanupRecoveryAuthorization.model_validate_json(
            canonical_gcp_cleanup_recovery_authorization_bytes(authorization)
        )
        now_value = _parse_timestamp(_timestamp(now))
    except (ValidationError, ValueError):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_INVALID") from None
    try:
        watchdog_cleanup_deadline = _parse_timestamp(
            _timestamp(watchdog_cleanup_deadline_at)
        )
    except ValueError:
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_INVALID") from None
    disk_binding = parsed.disk_cleanup_binding
    expected = {
        "approval_digest": approval_digest,
        "lease_intent_anchor_digest": record.intent_anchor_digest,
        "arm_id": record.arm_id,
        "plan_id": record.plan_id,
        "request_digest": record.request_digest,
        "startup_projection_digest": startup_projection_digest,
        "execution_payload_digest": execution_payload_digest,
        "controller_id": record.controller_id,
        "project_id": record.project_id,
        "region": record.region,
        "zone": record.zone,
        "instance_name": record.instance_name,
        "labels": record.labels,
        "max_cleanup_attempts": record.max_cleanup_attempts,
        "cleanup_timeout_seconds": record.cleanup_timeout_seconds,
        "create_authority": False,
        "allowed_actions": _CLEANUP_ACTIONS,
        "lease_created_at": record.created_at,
        "watchdog_cleanup_deadline_at": _timestamp(watchdog_cleanup_deadline),
        "recovery_deadline_at": _timestamp(watchdog_cleanup_deadline),
    }
    if any(getattr(parsed, key) != value for key, value in expected.items()):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_MISMATCH")
    if (
        disk_binding.request_digest != record.request_digest
        or disk_binding.project_id != record.project_id
        or disk_binding.zone != record.zone
        or disk_binding.controller_id != record.controller_id
        or disk_binding.instance_name != record.instance_name
        or disk_binding.labels != record.labels
        or parsed.boot_disk_name != disk_binding.disk.disk_name
    ):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_MISMATCH")
    if not (
        _parse_timestamp(parsed.issued_at)
        <= now_value
        < _parse_timestamp(parsed.expires_at)
    ):
        raise GcpV2ContractError("CLEANUP_RECOVERY_AUTHORIZATION_EXPIRED")
    return parsed


def gcp_v2_activation_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return additive v2 activation, payload-binding, and cleanup schemas."""

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        (
            "gcp-activation-deadline.schema.json",
            GcpV2ActivationDeadline,
            GCP_V2_ACTIVATION_DEADLINE_SCHEMA_ID,
        ),
        (
            "gcp-mutation-capability.schema.json",
            GcpV2MutationCapability,
            GCP_V2_MUTATION_CAPABILITY_SCHEMA_ID,
        ),
        (
            "gcp-startup-projection.schema.json",
            GcpV2StartupProjection,
            GCP_V2_STARTUP_PROJECTION_SCHEMA_ID,
        ),
        (
            "gcp-cleanup-recovery-authorization.schema.json",
            GcpCleanupRecoveryAuthorization,
            GCP_CLEANUP_RECOVERY_AUTHORIZATION_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
