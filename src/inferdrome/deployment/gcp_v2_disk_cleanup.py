"""Additive v0.2 exact boot-disk cleanup boundary.

This module deliberately has no Google SDK dependency.  It captures a
provider-observed boot disk as an exact, content-addressed local binding and
then offers a narrow injected transport protocol for cleanup only.  A caller
must not derive a disk name from a hostname, infer ownership from account-wide
diffs, or delete a disk from a partial inventory.

The file-backed fake provider is intentionally local test infrastructure.  It
exists so a watchdog or recovery worker can be exercised across process death
without contacting a provider.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self

from pydantic import Field, StringConstraints, ValidationError, model_validator

from inferdrome.deployment.gcp import GcpProjectId, GcpRegion, GcpTimestamp, GcpZone
from inferdrome.deployment.gcp_lifecycle import (
    _CONTROLLER_RE,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpExecutionModel,
    GcpInstanceName,
    GcpLeaseRecord,
    GcpRequestUuid,
    _parse_timestamp,
    _preflight_json,
    _timestamp,
    _write_all,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_V2_OWNED_BOOT_DISK_SCHEMA_VERSION: Final = "inferdrome.gcp-owned-boot-disk.v2"
GCP_V2_OWNED_INSTANCE_SCHEMA_VERSION: Final = "inferdrome.gcp-owned-instance.v2"
GCP_V2_OWNED_BOOT_DISK_INVENTORY_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-owned-boot-disk-inventory.v2"
)
GCP_V2_DISK_CLEANUP_BINDING_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-disk-cleanup-binding.v2"
)
GCP_V2_DISK_DELETE_OPERATION_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-disk-delete-operation.v2"
)
GCP_V2_DISK_DELETE_RESULT_SCHEMA_VERSION: Final = "inferdrome.gcp-disk-delete-result.v2"
GCP_V2_DISK_ABSENCE_SCHEMA_VERSION: Final = "inferdrome.gcp-disk-absence-observation.v2"
GCP_V2_DISK_CLEANUP_EVENT_SCHEMA_VERSION: Final = "inferdrome.gcp-disk-cleanup-event.v2"
GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-disk-cleanup-outcome.v2"
)
GCP_V2_OWNED_BOOT_DISK_SCHEMA_ID: Final = "urn:inferdrome:gcp-owned-boot-disk:v2"
GCP_V2_OWNED_INSTANCE_SCHEMA_ID: Final = "urn:inferdrome:gcp-owned-instance:v2"
GCP_V2_OWNED_BOOT_DISK_INVENTORY_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-owned-boot-disk-inventory:v2"
)
GCP_V2_DISK_CLEANUP_BINDING_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-cleanup-binding:v2"
)
GCP_V2_DISK_DELETE_OPERATION_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-delete-operation:v2"
)
GCP_V2_DISK_DELETE_RESULT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-delete-result:v2"
)
GCP_V2_DISK_ABSENCE_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-absence-observation:v2"
)
GCP_V2_DISK_CLEANUP_EVENT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-cleanup-event:v2"
)
GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-disk-cleanup-outcome:v2"
)

GCP_V2_DISK_CLEANUP_MAX_EVENTS: Final = 64
GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES: Final = 262_144
GCP_V2_DISK_PROVIDER_MAX_BYTES: Final = 131_072

_DISK_OPERATION_RE = re.compile(r"^disk-op-[a-z0-9]{8,64}$")
_OPERATION_NAME_RE = re.compile(r"^[a-zA-Z0-9_./-]{1,256}$")
_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")
_EVENT_PATH_RE = re.compile(r"^(ctl-[a-z0-9]{8,24})\.disk-cleanup-v2\.events\.jsonl$")

GcpV2DiskCleanupState = Literal[
    "PREPARED",
    "DELETE_INTENT",
    "DELETE_SUBMITTED",
    "DELETE_RECONCILING",
    "ABSENCE_CONFIRMED",
    "BLOCKED",
]
GcpV2DiskCleanupErrorCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
]
GcpV2DiskName = Annotated[
    str,
    StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$"),
]
GcpV2SourceImageName = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/"
            r"[a-z][a-z0-9-]{0,61}[a-z0-9]$"
        )
    ),
]


class GcpV2DiskCleanupError(GcpExecutionError):
    """A bounded local-only disk-cleanup failure.

    ``ambiguous`` means a delete may have reached the injected provider but no
    exact operation receipt was returned.  It never authorizes a broad retry;
    the runner must read the exact disk and reconcile the deterministic request
    identity first.
    """

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        self.code = (
            code if _ERROR_RE.fullmatch(code) is not None else "DISK_CLEANUP_ERROR"
        )
        self.ambiguous = ambiguous
        super().__init__(self.code)


class GcpV2ExactOwnedBootDisk(GcpExecutionModel):
    """One provider-observed owned boot disk with immutable provenance.

    The disk's labels and provenance alone are insufficient: a matching data
    disk must never become a cleanup target.  This record therefore carries
    the exact provider observation of the instance attachment and its boot
    source.  All of those facts are immutable input to the content-addressed
    cleanup binding; callers cannot derive a target from an instance name.

    ``boot_attachment`` and the attached-instance fields are the immutable
    provenance observed before workload execution.  ``attachment_state`` is
    the *current* read-only state: after exact instance deletion, GCE can
    report the same owned boot disk as detached.  That does not discard the
    original provenance or permit a same-name/data-disk substitution.
    """

    schema_version: Literal["inferdrome.gcp-owned-boot-disk.v2"]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: GcpInstanceName
    disk_name: GcpV2DiskName
    provider_disk_id: int = Field(strict=True, ge=1, le=10**19)
    self_link: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    # These are a live provider observation.  They are mandatory for the
    # immutable pre-delete inventory binding, but a correctly detached residual
    # disk has no current attachment.  The binding retains the original exact
    # instance proof independently and comparison below never permits a
    # different attachment to replace it.
    attached_instance_provider_id: int | None = Field(
        default=None, strict=True, ge=1, le=10**19
    )
    attached_instance_self_link: Annotated[
        str | None, StringConstraints(min_length=1, max_length=512)
    ] = None
    boot_device_name: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,61}$")
    ]
    boot_source_disk_self_link: Annotated[
        str, StringConstraints(min_length=1, max_length=512)
    ]
    boot_attachment: Literal[True]
    attachment_state: Literal["ATTACHED", "DETACHED"] = "ATTACHED"
    labels: GcpExecutionLabels
    source_image_name: GcpV2SourceImageName
    source_image_provider_id: int = Field(strict=True, ge=1, le=10**19)
    source_image_digest: Sha256Digest
    disk_type: Literal["pd-balanced", "pd-ssd"]
    size_gib: int = Field(strict=True, ge=10, le=16_384)
    state: Literal["PRESENT"]

    @model_validator(mode="after")
    def validate_exact_resource_identity(self) -> Self:
        expected_disk = (
            f"projects/{self.project_id}/zones/{self.zone}/disks/{self.disk_name}"
        )
        expected_instance = (
            f"projects/{self.project_id}/zones/{self.zone}/instances/"
            f"{self.instance_name}"
        )
        if self.self_link != expected_disk:
            raise ValueError("owned boot-disk self link is inconsistent")
        if (
            self.boot_source_disk_self_link != expected_disk
            or self.boot_attachment is not True
        ):
            raise ValueError("owned boot-disk attachment is inconsistent")
        if self.attachment_state == "ATTACHED" and (
            self.attached_instance_provider_id is None
            or self.attached_instance_self_link != expected_instance
        ):
            raise ValueError("owned boot-disk attachment is inconsistent")
        if self.attachment_state == "DETACHED" and (
            self.attached_instance_provider_id is not None
            or self.attached_instance_self_link is not None
        ):
            raise ValueError("detached boot disk retains a live attachment")
        return self


class GcpV2ExactOwnedInstance(GcpExecutionModel):
    """One exact provider-observed instance attached to a v2 boot disk.

    The frozen v1 instance observation does not expose a stable provider ID.
    This additive read-only fact prevents a same-name replacement instance
    from being treated as the one that owns the observed boot attachment.
    """

    schema_version: Literal["inferdrome.gcp-owned-instance.v2"]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: GcpInstanceName
    provider_instance_id: int = Field(strict=True, ge=1, le=10**19)
    self_link: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    labels: GcpExecutionLabels
    state: Literal["RUNNING", "TERMINATED"]

    @model_validator(mode="after")
    def validate_exact_resource_identity(self) -> Self:
        if self.self_link != (
            f"projects/{self.project_id}/zones/{self.zone}/instances/"
            f"{self.instance_name}"
        ):
            raise ValueError("owned instance self link is inconsistent")
        return self


class GcpV2OwnedBootDiskInventory(GcpExecutionModel):
    """A complete, label-scoped exact inventory containing one boot disk."""

    schema_version: Literal["inferdrome.gcp-owned-boot-disk-inventory.v2"]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: GcpInstanceName
    labels: GcpExecutionLabels
    observed_at: GcpTimestamp
    disks: tuple[GcpV2ExactOwnedBootDisk, ...] = Field(min_length=1, max_length=1)
    pagination_complete: Literal[True]
    inventory_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_inventory(self) -> Self:
        disk = self.disks[0]
        if (
            disk.request_digest != self.request_digest
            or disk.project_id != self.project_id
            or disk.zone != self.zone
            or disk.instance_name != self.instance_name
            or disk.labels != self.labels
        ):
            raise ValueError("owned boot-disk inventory entry is inconsistent")
        if self.inventory_digest != gcp_v2_owned_boot_disk_inventory_digest(self):
            raise ValueError("owned boot-disk inventory identity is inconsistent")
        return self


class GcpV2DiskCleanupBinding(GcpExecutionModel):
    """Exact cleanup authority input for one observed owned boot disk."""

    schema_version: Literal["inferdrome.gcp-disk-cleanup-binding.v2"]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    controller_id: Annotated[str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")]
    instance_name: GcpInstanceName
    labels: GcpExecutionLabels
    attached_instance: GcpV2ExactOwnedInstance
    owned_inventory_digest: Sha256Digest
    owned_inventory: GcpV2OwnedBootDiskInventory
    disk: GcpV2ExactOwnedBootDisk
    cleanup_actions: tuple[
        Literal[
            "delete_exact_boot_disk",
            "reconcile_exact_delete_operation",
            "confirm_exact_boot_disk_absence",
        ],
        ...,
    ]
    binding_id: Sha256Digest

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if _CONTROLLER_RE.fullmatch(self.controller_id) is None:
            raise ValueError("disk cleanup controller identity is invalid")
        if (
            self.owned_inventory.inventory_digest != self.owned_inventory_digest
            or self.owned_inventory.request_digest != self.request_digest
            or self.owned_inventory.project_id != self.project_id
            or self.owned_inventory.zone != self.zone
            or self.owned_inventory.instance_name != self.instance_name
            or self.owned_inventory.labels != self.labels
            or self.owned_inventory.pagination_complete is not True
            or self.owned_inventory.disks != (self.disk,)
            or self.disk.attachment_state != "ATTACHED"
        ):
            raise ValueError("disk cleanup binding inventory is incomplete or changed")
        if (
            self.disk.request_digest != self.request_digest
            or self.disk.project_id != self.project_id
            or self.disk.zone != self.zone
            or self.disk.instance_name != self.instance_name
            or self.disk.labels != self.labels
        ):
            raise ValueError("disk cleanup binding does not match observed disk")
        if (
            self.attached_instance.request_digest != self.request_digest
            or self.attached_instance.project_id != self.project_id
            or self.attached_instance.zone != self.zone
            or self.attached_instance.instance_name != self.instance_name
            or self.attached_instance.labels != self.labels
            or self.disk.attached_instance_provider_id
            != self.attached_instance.provider_instance_id
            or self.disk.attached_instance_self_link
            != self.attached_instance.self_link
        ):
            raise ValueError("disk cleanup binding attached instance is inconsistent")
        if self.cleanup_actions != (
            "delete_exact_boot_disk",
            "reconcile_exact_delete_operation",
            "confirm_exact_boot_disk_absence",
        ):
            raise ValueError("disk cleanup actions must be exact and ordered")
        if self.binding_id != gcp_v2_disk_cleanup_binding_id(self):
            raise ValueError("disk cleanup binding identity is inconsistent")
        return self


class GcpV2DiskDeleteOperation(GcpExecutionModel):
    """An exact delete operation returned by an injected local transport."""

    schema_version: Literal["inferdrome.gcp-disk-delete-operation.v2"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^disk-op-[a-z0-9]{8,64}$")]
    operation_name: Annotated[
        str, StringConstraints(pattern=r"^[a-zA-Z0-9_./-]{1,256}$")
    ]
    request_id: GcpRequestUuid
    request_digest: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: GcpInstanceName
    disk_name: GcpV2DiskName
    labels: GcpExecutionLabels


class GcpV2DiskDeleteResult(GcpExecutionModel):
    """Reconciled result for exactly one named-disk delete operation."""

    schema_version: Literal["inferdrome.gcp-disk-delete-result.v2"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^disk-op-[a-z0-9]{8,64}$")]
    status: Literal["DONE", "ERROR", "TIMEOUT"]
    error_code: GcpV2DiskCleanupErrorCode | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "ERROR" and self.error_code is None:
            raise ValueError("failed disk operation requires an error code")
        if self.status != "ERROR" and self.error_code is not None:
            raise ValueError("nonfailed disk operation cannot contain an error")
        return self


class GcpV2DiskAbsenceObservation(GcpExecutionModel):
    """Authoritative absence confirmation for the one bound disk identity."""

    schema_version: Literal["inferdrome.gcp-disk-absence-observation.v2"]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: GcpInstanceName
    disk_name: GcpV2DiskName
    labels: GcpExecutionLabels
    state: Literal["NOT_FOUND"]


class GcpV2DiskCleanupJournalEvent(GcpExecutionModel):
    """One bounded, hash-linked local disk-cleanup transition."""

    schema_version: Literal["inferdrome.gcp-disk-cleanup-event.v2"]
    sequence: int = Field(strict=True, ge=0, le=GCP_V2_DISK_CLEANUP_MAX_EVENTS - 1)
    state: GcpV2DiskCleanupState
    binding: GcpV2DiskCleanupBinding
    occurred_at: GcpTimestamp
    delete_attempts: int = Field(strict=True, ge=0, le=16)
    operation: GcpV2DiskDeleteOperation | None = None
    absence: GcpV2DiskAbsenceObservation | None = None
    error_code: GcpV2DiskCleanupErrorCode | None = None
    previous_event_digest: Sha256Digest | None
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.state == "PREPARED" and (
            self.sequence != 0
            or self.delete_attempts != 0
            or self.operation is not None
            or self.absence is not None
            or self.error_code is not None
            or self.previous_event_digest is not None
        ):
            raise ValueError("initial disk cleanup event is inconsistent")
        if self.state == "DELETE_INTENT" and self.delete_attempts < 1:
            raise ValueError("disk delete intent requires a bounded attempt")
        if self.state == "DELETE_SUBMITTED" and self.operation is None:
            raise ValueError("submitted disk delete requires an operation")
        if self.state == "ABSENCE_CONFIRMED" and self.absence is None:
            raise ValueError("confirmed disk absence requires observation")
        if self.state != "ABSENCE_CONFIRMED" and self.absence is not None:
            raise ValueError("disk absence observation is only terminal")
        if self.state == "BLOCKED" and self.error_code is None:
            raise ValueError("blocked disk cleanup requires an error code")
        if self.state != "BLOCKED" and self.error_code is not None:
            raise ValueError("only blocked disk cleanup may carry an error")
        if self.operation is not None and not _operation_matches_binding(
            self.operation, self.binding
        ):
            raise ValueError("disk delete operation does not match binding")
        if self.absence is not None and not _absence_matches_binding(
            self.absence, self.binding
        ):
            raise ValueError("disk absence does not match binding")
        value = _model_value(self)
        value.pop("event_digest", None)
        if self.event_digest != digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
        ):
            raise ValueError("disk cleanup event identity is inconsistent")
        return self


class GcpV2DiskCleanupOutcome(GcpExecutionModel):
    """Terminal local outcome; only exact absence counts as cleanup proof."""

    schema_version: Literal["inferdrome.gcp-disk-cleanup-outcome.v2"]
    binding_id: Sha256Digest
    state: Literal["ABSENCE_CONFIRMED", "BLOCKED"]
    delete_attempts: int = Field(strict=True, ge=0, le=16)
    operation_id: Annotated[
        str | None, StringConstraints(pattern=r"^disk-op-[a-z0-9]{8,64}$")
    ] = None
    absence: GcpV2DiskAbsenceObservation | None = None
    error_code: GcpV2DiskCleanupErrorCode | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.state == "ABSENCE_CONFIRMED" and self.absence is None:
            raise ValueError("confirmed disk cleanup requires absence")
        if self.state == "BLOCKED" and self.error_code is None:
            raise ValueError("blocked disk cleanup requires an error")
        return self


def _model_value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("disk cleanup contract must serialize as an object")
    return value


def canonical_gcp_v2_owned_boot_disk_inventory_payload_bytes(
    inventory: GcpV2OwnedBootDiskInventory,
) -> bytes:
    """Return the exact inventory content excluding its local identity."""

    value = _model_value(inventory)
    value.pop("inventory_digest", None)
    return canonical_json_bytes(value)


def gcp_v2_owned_boot_disk_inventory_digest(
    inventory: GcpV2OwnedBootDiskInventory,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG,
        canonical_gcp_v2_owned_boot_disk_inventory_payload_bytes(inventory),
    )


def issue_gcp_v2_owned_boot_disk_inventory(
    *,
    disk: GcpV2ExactOwnedBootDisk,
    observed_at: datetime,
) -> GcpV2OwnedBootDiskInventory:
    """Create a complete exact one-disk inventory from a read-only observation."""

    try:
        observed_text = _timestamp(observed_at)
    except ValueError:
        raise GcpV2DiskCleanupError("DISK_INVENTORY_TIME_INVALID") from None
    payload: dict[str, Any] = {
        "schema_version": GCP_V2_OWNED_BOOT_DISK_INVENTORY_SCHEMA_VERSION,
        "request_digest": disk.request_digest,
        "project_id": disk.project_id,
        "zone": disk.zone,
        "instance_name": disk.instance_name,
        "labels": _model_value(disk.labels),
        "observed_at": observed_text,
        "disks": [_model_value(disk)],
        "pagination_complete": True,
    }
    payload["inventory_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
    )
    return GcpV2OwnedBootDiskInventory.model_validate_json(
        canonical_json_bytes(payload)
    )


def canonical_gcp_v2_disk_cleanup_binding_payload_bytes(
    binding: GcpV2DiskCleanupBinding,
) -> bytes:
    """Return the exact cleanup binding excluding its local identity."""

    value = _model_value(binding)
    value.pop("binding_id", None)
    return canonical_json_bytes(value)


def gcp_v2_disk_cleanup_binding_id(
    binding: GcpV2DiskCleanupBinding,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG,
        canonical_gcp_v2_disk_cleanup_binding_payload_bytes(binding),
    )


def issue_gcp_v2_disk_cleanup_binding(
    inventory: GcpV2OwnedBootDiskInventory,
    *,
    controller_id: str,
    attached_instance: GcpV2ExactOwnedInstance,
) -> GcpV2DiskCleanupBinding:
    """Bind cleanup to one complete exact disk inventory, never a hostname guess."""

    if _CONTROLLER_RE.fullmatch(controller_id) is None:
        raise GcpV2DiskCleanupError("DISK_CLEANUP_CONTROLLER_INVALID")
    if inventory.pagination_complete is not True or len(inventory.disks) != 1:
        raise GcpV2DiskCleanupError("DISK_INVENTORY_INCOMPLETE")
    disk = inventory.disks[0]
    try:
        instance_raw = canonical_json_bytes(_model_value(attached_instance))
        instance = GcpV2ExactOwnedInstance.model_validate_json(instance_raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpV2DiskCleanupError("OWNED_INSTANCE_OBSERVATION_INVALID") from None
    if canonical_json_bytes(_model_value(instance)) != instance_raw:
        raise GcpV2DiskCleanupError("OWNED_INSTANCE_OBSERVATION_INVALID")
    if (
        instance.request_digest != inventory.request_digest
        or instance.project_id != inventory.project_id
        or instance.zone != inventory.zone
        or instance.instance_name != inventory.instance_name
        or instance.labels != inventory.labels
        or disk.attached_instance_provider_id != instance.provider_instance_id
        or disk.attached_instance_self_link != instance.self_link
        or disk.attachment_state != "ATTACHED"
    ):
        raise GcpV2DiskCleanupError("OWNED_INSTANCE_OBSERVATION_MISMATCH")
    payload: dict[str, Any] = {
        "schema_version": GCP_V2_DISK_CLEANUP_BINDING_SCHEMA_VERSION,
        "request_digest": inventory.request_digest,
        "project_id": inventory.project_id,
        "zone": inventory.zone,
        "controller_id": controller_id,
        "instance_name": inventory.instance_name,
        "labels": _model_value(inventory.labels),
        "attached_instance": _model_value(instance),
        "owned_inventory_digest": inventory.inventory_digest,
        "owned_inventory": _model_value(inventory),
        "disk": _model_value(disk),
        "cleanup_actions": [
            "delete_exact_boot_disk",
            "reconcile_exact_delete_operation",
            "confirm_exact_boot_disk_absence",
        ],
    }
    payload["binding_id"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
    )
    return GcpV2DiskCleanupBinding.model_validate_json(canonical_json_bytes(payload))


def gcp_v2_disk_delete_request_id(binding: GcpV2DiskCleanupBinding) -> GcpRequestUuid:
    """Return a stable UUIDv5 bound to the exact request/project/zone/disk."""

    binding = _strict_binding(binding)
    value = {
        "request_digest": binding.request_digest,
        "project_id": binding.project_id,
        "zone": binding.zone,
        "disk_name": binding.disk.disk_name,
    }
    name = canonical_json_bytes(value).decode("utf-8")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _strict_binding(binding: GcpV2DiskCleanupBinding) -> GcpV2DiskCleanupBinding:
    raw = canonical_json_bytes(_model_value(binding))
    try:
        parsed = GcpV2DiskCleanupBinding.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpV2DiskCleanupError("DISK_CLEANUP_BINDING_INVALID") from None
    if canonical_json_bytes(_model_value(parsed)) != raw:
        raise GcpV2DiskCleanupError("DISK_CLEANUP_BINDING_NONCANONICAL")
    return parsed


def _operation_matches_binding(
    operation: GcpV2DiskDeleteOperation, binding: GcpV2DiskCleanupBinding
) -> bool:
    return (
        operation.request_id == gcp_v2_disk_delete_request_id(binding)
        and operation.request_digest == binding.request_digest
        and operation.project_id == binding.project_id
        and operation.zone == binding.zone
        and operation.instance_name == binding.instance_name
        and operation.disk_name == binding.disk.disk_name
        and operation.labels == binding.labels
    )


def _absence_matches_binding(
    absence: GcpV2DiskAbsenceObservation, binding: GcpV2DiskCleanupBinding
) -> bool:
    return (
        absence.request_digest == binding.request_digest
        and absence.project_id == binding.project_id
        and absence.zone == binding.zone
        and absence.instance_name == binding.instance_name
        and absence.disk_name == binding.disk.disk_name
        and absence.labels == binding.labels
        and absence.state == "NOT_FOUND"
    )


def _observed_disk_matches_binding(
    observed: GcpV2ExactOwnedBootDisk, binding: GcpV2DiskCleanupBinding
) -> bool:
    """Accept only the same bound disk, optionally now detached.

    Instance deletion legitimately changes the current attachment state of a
    residual boot disk.  It must not change any named resource identity,
    labels, source-image provenance, original attached-instance identity, or
    boot-disk fact stored in the pre-create binding.
    """

    try:
        observed_value = _model_value(observed)
        bound_value = _model_value(binding.disk)
    except (AttributeError, TypeError, ValueError):
        return False
    observed_state = observed_value.pop("attachment_state", None)
    bound_value.pop("attachment_state", None)
    if observed_state == "ATTACHED":
        return observed_value == bound_value
    if observed_state != "DETACHED":
        return False
    # A detached disk cannot provide a current instance attachment.  Drop only
    # that live observation from the immutable binding comparison; every named
    # disk identity, label, source-image, and boot provenance fact remains
    # exact.  In particular, a different non-null attachment is never accepted.
    if (
        observed_value.pop("attached_instance_provider_id", object()) is not None
        or observed_value.pop("attached_instance_self_link", object()) is not None
    ):
        return False
    bound_value.pop("attached_instance_provider_id", None)
    bound_value.pop("attached_instance_self_link", None)
    return observed_value == bound_value


def _event(
    *,
    sequence: int,
    state: GcpV2DiskCleanupState,
    binding: GcpV2DiskCleanupBinding,
    occurred_at: str,
    delete_attempts: int,
    operation: GcpV2DiskDeleteOperation | None,
    absence: GcpV2DiskAbsenceObservation | None,
    error_code: str | None,
    previous_event_digest: Sha256Digest | None,
) -> GcpV2DiskCleanupJournalEvent:
    payload: dict[str, Any] = {
        "schema_version": GCP_V2_DISK_CLEANUP_EVENT_SCHEMA_VERSION,
        "sequence": sequence,
        "state": state,
        "binding": _model_value(binding),
        "occurred_at": occurred_at,
        "delete_attempts": delete_attempts,
        "operation": _model_value(operation) if operation is not None else None,
        "absence": _model_value(absence) if absence is not None else None,
        "error_code": error_code,
        "previous_event_digest": previous_event_digest,
    }
    payload["event_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
    )
    return GcpV2DiskCleanupJournalEvent.model_validate_json(
        canonical_json_bytes(payload)
    )


class GcpV2DiskCleanupTransport(Protocol):
    """Exact cleanup-only provider surface, injectable only after authorization."""

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk: ...

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: GcpRequestUuid,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation: ...

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult: ...

    def confirm_exact_boot_disk_absent(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2DiskAbsenceObservation: ...


_CAPABILITY_BOUND_DISK_TRANSPORT_SEAL = object()
_RECOVERY_BOUND_DISK_TRANSPORT_SEAL = object()


class GcpV2AuthorityBoundDiskCleanupTransport:
    """A sealed exact-disk adapter with a live local authority horizon.

    The raw injected disk provider is deliberately never handed to the core
    coordinator.  Every read, delete, reconciliation, and absence check
    revalidates the same exact disk binding and either the active watchdog
    capability or cleanup-only recovery authorization before calling it.
    """

    def __init__(
        self,
        *,
        provider_supplier: Callable[[], GcpV2DiskCleanupTransport],
        binding: GcpV2DiskCleanupBinding,
        authority: object,
        authority_kind: Literal["capability", "recovery"],
        startup_projection_digest: Sha256Digest,
        execution_payload_digest: Sha256Digest,
        now_fn: Callable[[], datetime],
        _seal: object,
    ) -> None:
        expected_seal = (
            _CAPABILITY_BOUND_DISK_TRANSPORT_SEAL
            if authority_kind == "capability"
            else _RECOVERY_BOUND_DISK_TRANSPORT_SEAL
        )
        if _seal is not expected_seal:
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_TRANSPORT_INVALID")
        if not callable(provider_supplier):
            raise GcpV2DiskCleanupError("DISK_PROVIDER_SUPPLIER_INVALID")
        self._provider_supplier = provider_supplier
        self._provider: GcpV2DiskCleanupTransport | None = None
        self._provider_lock = threading.Lock()
        self.binding = _strict_binding(binding)
        self._authority = authority
        self._authority_kind = authority_kind
        self._startup_projection_digest = startup_projection_digest
        self._execution_payload_digest = execution_payload_digest
        self._now_fn = now_fn
        self._seal = _seal
        self._assert_authority()

    def _now(self) -> datetime:
        try:
            return self._now_fn().astimezone(UTC)
        except (AttributeError, TypeError, ValueError):
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_TIME_INVALID") from None

    def _assert_authority(self) -> None:
        """Revalidate the exact narrow authority before each raw call."""

        now = self._now()
        if self._authority_kind == "capability":
            try:
                from inferdrome.deployment.gcp_v2_contracts import (
                    GcpV2MutationCapability,
                )

                if not isinstance(self._authority, GcpExecutionModel):
                    raise TypeError("authority is not a GCP execution model")
                raw = canonical_json_bytes(_model_value(self._authority))
                capability = GcpV2MutationCapability.model_validate_json(raw)
                activated = _parse_timestamp(capability.activated_at)
                deadline = _parse_timestamp(capability.watchdog_cleanup_deadline_at)
            except (
                AttributeError,
                ImportError,
                ValidationError,
                ValueError,
                TypeError,
            ):
                raise GcpV2DiskCleanupError("DISK_CAPABILITY_INVALID") from None
            if canonical_json_bytes(_model_value(capability)) != raw:
                raise GcpV2DiskCleanupError("DISK_CAPABILITY_INVALID")
            if (
                capability.startup_projection_digest
                != self._startup_projection_digest
                or capability.execution_payload_digest
                != self._execution_payload_digest
            ):
                raise GcpV2DiskCleanupError("DISK_CAPABILITY_PAYLOAD_MISMATCH")
            if not (
                capability.request_digest == self.binding.request_digest
                and capability.project_id == self.binding.project_id
                and capability.zone == self.binding.zone
                and capability.instance_name == self.binding.instance_name
                and capability.labels == self.binding.labels
                and activated <= now < deadline
            ):
                raise GcpV2DiskCleanupError("DISK_CAPABILITY_MISMATCH")
            return
        try:
            from inferdrome.deployment.gcp_v2_contracts import (
                GcpCleanupRecoveryAuthorization,
            )

            if not isinstance(self._authority, GcpExecutionModel):
                raise TypeError("authority is not a GCP execution model")
            raw = canonical_json_bytes(_model_value(self._authority))
            authorization = GcpCleanupRecoveryAuthorization.model_validate_json(raw)
            issued = _parse_timestamp(authorization.issued_at)
            expires = _parse_timestamp(authorization.expires_at)
            recovery_deadline = _parse_timestamp(authorization.recovery_deadline_at)
        except (AttributeError, ImportError, ValidationError, ValueError, TypeError):
            raise GcpV2DiskCleanupError("DISK_RECOVERY_AUTHORIZATION_INVALID") from None
        if canonical_json_bytes(_model_value(authorization)) != raw:
            raise GcpV2DiskCleanupError("DISK_RECOVERY_AUTHORIZATION_INVALID")
        if (
            authorization.startup_projection_digest
            != self._startup_projection_digest
            or authorization.execution_payload_digest
            != self._execution_payload_digest
        ):
            raise GcpV2DiskCleanupError(
                "DISK_RECOVERY_AUTHORIZATION_PAYLOAD_MISMATCH"
            )
        if not (
            authorization.create_authority is False
            and authorization.disk_cleanup_binding == self.binding
            and authorization.request_digest == self.binding.request_digest
            and authorization.project_id == self.binding.project_id
            and authorization.zone == self.binding.zone
            and authorization.instance_name == self.binding.instance_name
            and authorization.labels == self.binding.labels
            and authorization.allowed_actions
            == (
                "reconcile_operation",
                "get_exact_instance",
                "list_exact_label_inventory",
                "delete_exact_instance",
                "get_exact_disk",
                "delete_exact_disk",
                "confirm_absence",
            )
            and issued <= now < expires <= recovery_deadline
        ):
            raise GcpV2DiskCleanupError("DISK_RECOVERY_AUTHORIZATION_MISMATCH")

    def _provider_after_authority(self) -> GcpV2DiskCleanupTransport:
        """Lazily construct the raw boundary only after current local checks.

        This is intentionally inside the sealed adapter rather than the
        factory.  Expired/tampered authority must not even initialize a future
        SDK/ADC supplier; every provider-facing method also rechecks its live
        horizon immediately before dispatch.
        """

        self._assert_authority()
        with self._provider_lock:
            if self._provider is None:
                try:
                    candidate = self._provider_supplier()
                except BaseException:
                    raise GcpV2DiskCleanupError(
                        "DISK_PROVIDER_SUPPLIER_FAILED"
                    ) from None
                if candidate is None:
                    raise GcpV2DiskCleanupError("DISK_PROVIDER_SUPPLIER_FAILED")
                self._provider = candidate
            return self._provider

    def _exact_binding(self, binding: GcpV2DiskCleanupBinding) -> None:
        if _strict_binding(binding) != self.binding:
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_BINDING_MISMATCH")

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_authority()
        self._exact_binding(binding)
        return self._provider_after_authority().read_exact_owned_boot_disk(
            binding, timeout_seconds=timeout_seconds
        )

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: GcpRequestUuid,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        self._assert_authority()
        self._exact_binding(binding)
        return self._provider_after_authority().delete_exact_boot_disk(
            binding, request_id=request_id, timeout_seconds=timeout_seconds
        )

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        self._assert_authority()
        if not _operation_matches_binding(operation, self.binding):
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_OPERATION_MISMATCH")
        return self._provider_after_authority().reconcile_exact_disk_delete(
            operation, timeout_seconds=timeout_seconds
        )

    def confirm_exact_boot_disk_absent(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2DiskAbsenceObservation:
        self._assert_authority()
        self._exact_binding(binding)
        return self._provider_after_authority().confirm_exact_boot_disk_absent(
            binding, timeout_seconds=timeout_seconds
        )


def is_gcp_v2_authority_bound_disk_transport(value: object) -> bool:
    """Recognize only disk transports minted by the nominal local factory."""

    return type(value) is GcpV2AuthorityBoundDiskCleanupTransport and getattr(
        value, "_seal", None
    ) in {
        _CAPABILITY_BOUND_DISK_TRANSPORT_SEAL,
        _RECOVERY_BOUND_DISK_TRANSPORT_SEAL,
    }


class GcpV2DiskCleanupJournal:
    """Bounded fsync/no-follow JSONL journal for one exact disk cleanup.

    The journal is intentionally separate from the v1 lease and v0.2
    supervisor journals.  Its first event is durable delete *intent*; every
    later event is hash-linked and it permits recovery of a lost delete
    response without guessing what a provider did.
    """

    def __init__(
        self,
        root: Path,
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._crash_hook = crash_hook

    def _crash_point(self, point: str) -> None:
        """Test-only process-death injection; production leaves this unset."""

        if self._crash_hook is not None:
            self._crash_hook(point)

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpV2DiskCleanupError("DISK_JOURNAL_PATH_INVALID")
        try:
            metadata = self.root.lstat()
            resolved = os.path.realpath(os.fspath(self.root))
        except OSError:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_UNAVAILABLE") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
            or os.path.normcase(os.path.abspath(os.fspath(self.root)))
            != os.path.normcase(resolved)
        ):
            raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
        return self.root

    @staticmethod
    def _path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_CONTROLLER_INVALID")
        return root / f"{controller_id}.disk-cleanup-v2.events.jsonl"

    @contextmanager
    def _exclusive(self) -> Iterator[Path]:
        root = self._checked_root()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / ".disk-cleanup-v2.lock",
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
                raise GcpV2DiskCleanupError("DISK_JOURNAL_LOCK_UNAVAILABLE") from None
            with self._lock:
                yield root
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def _fsync_directory(self) -> None:
        descriptor = os.open(
            self._checked_root(), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_events_locked(
        self, path: Path
    ) -> tuple[GcpV2DiskCleanupJournalEvent, ...]:
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
                or metadata.st_size > GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES
                or final.st_ino != metadata.st_ino
                or final.st_size != len(raw)
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_CHANGED")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                raw = raw[: newline + 1] if newline >= 0 else b""
            lines = raw.splitlines()
            if not lines or len(lines) > GCP_V2_DISK_CLEANUP_MAX_EVENTS:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_INVALID")
            previous: Sha256Digest | None = None
            binding: GcpV2DiskCleanupBinding | None = None
            events: list[GcpV2DiskCleanupJournalEvent] = []
            for sequence, line in enumerate(lines):
                _preflight_json(line, kind="GCP v2 disk cleanup event")
                event = GcpV2DiskCleanupJournalEvent.model_validate_json(line)
                if canonical_json_bytes(_model_value(event)) != line:
                    raise GcpV2DiskCleanupError("DISK_JOURNAL_NONCANONICAL")
                if (
                    event.sequence != sequence
                    or event.previous_event_digest != previous
                ):
                    raise GcpV2DiskCleanupError("DISK_JOURNAL_CHAIN_INVALID")
                if binding is not None and event.binding != binding:
                    raise GcpV2DiskCleanupError("DISK_JOURNAL_BINDING_CHANGED")
                previous = event.event_digest
                binding = event.binding
                events.append(event)
            self._validate_transitions(events)
            return tuple(events)
        except GcpV2DiskCleanupError:
            raise
        except (OSError, ValidationError, ValueError):
            raise GcpV2DiskCleanupError("DISK_JOURNAL_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _repair_partial_tail_locked(self, path: Path) -> bool:
        """Repair one incomplete final write and report whether history remains.

        A crash before the very first event reaches a newline leaves no durable
        cleanup authority to preserve.  Under the exact controller lock and
        no-follow checks, that zero-history prefix is removed so a restart can
        reserve the same binding.  Once any complete event exists, only the
        incomplete *tail* is truncated; malformed completed history remains
        fail-closed.
        """

        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES + 1)
            if len(raw) > GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                if newline < 0:
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                    os.unlink(path)
                    self._fsync_directory()
                    return False
                os.ftruncate(descriptor, newline + 1)
                os.fsync(descriptor)
                self._fsync_directory()
            return True
        except FileNotFoundError:
            return False
        except GcpV2DiskCleanupError:
            raise
        except OSError:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_REPAIR_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate_transitions(events: list[GcpV2DiskCleanupJournalEvent]) -> None:
        allowed: dict[GcpV2DiskCleanupState, set[GcpV2DiskCleanupState]] = {
            "PREPARED": {"DELETE_INTENT", "ABSENCE_CONFIRMED", "BLOCKED"},
            "DELETE_INTENT": {
                "DELETE_SUBMITTED",
                "DELETE_RECONCILING",
                "ABSENCE_CONFIRMED",
                "BLOCKED",
            },
            "DELETE_SUBMITTED": {
                "DELETE_RECONCILING",
                "ABSENCE_CONFIRMED",
                "BLOCKED",
            },
            "DELETE_RECONCILING": {
                "DELETE_INTENT",
                "DELETE_SUBMITTED",
                "DELETE_RECONCILING",
                "ABSENCE_CONFIRMED",
                "BLOCKED",
            },
            "BLOCKED": {"ABSENCE_CONFIRMED", "BLOCKED"},
            "ABSENCE_CONFIRMED": {"ABSENCE_CONFIRMED"},
        }
        for previous, current in pairwise(events):
            if current.state not in allowed[previous.state]:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_TRANSITION_INVALID")
            if _parse_timestamp(current.occurred_at) < _parse_timestamp(
                previous.occurred_at
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_TIMESTAMP_REGRESSED")
            if current.delete_attempts < previous.delete_attempts:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_ATTEMPTS_REGRESSED")
            if current.delete_attempts > previous.delete_attempts + 1:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_ATTEMPTS_INVALID")
            if (
                previous.operation is not None
                and current.operation != previous.operation
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_OPERATION_CHANGED")
            if current.state == "DELETE_INTENT" and (
                current.delete_attempts != previous.delete_attempts + 1
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_INTENT_INVALID")

    def prepare(
        self, binding: GcpV2DiskCleanupBinding, *, now: datetime
    ) -> GcpV2DiskCleanupJournalEvent:
        """Persist or recover the exact cleanup intent without provider mutation."""

        binding = _strict_binding(binding)
        try:
            occurred_at = _timestamp(now)
        except ValueError:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_TIME_INVALID") from None
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                metadata = None
            except OSError:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNAVAILABLE") from None
            if metadata is not None:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
                if self._repair_partial_tail_locked(path):
                    latest = self._read_events_locked(path)[-1]
                    if latest.binding != binding:
                        raise GcpV2DiskCleanupError("DISK_JOURNAL_BINDING_MISMATCH")
                    return latest
            event = _event(
                sequence=0,
                state="PREPARED",
                binding=binding,
                occurred_at=occurred_at,
                delete_attempts=0,
                operation=None,
                absence=None,
                error_code=None,
                previous_event_digest=None,
            )
            raw = canonical_json_bytes(_model_value(event)) + b"\n"
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._crash_point("disk_cleanup_prepare_after_event_fsync")
                self._fsync_directory()
                self._crash_point("disk_cleanup_prepare_after_directory_fsync")
            except FileExistsError:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_ALREADY_EXISTS") from None
            except OSError:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_RESERVE_FAILED") from None
            return event

    def load(self, controller_id: str) -> GcpV2DiskCleanupJournalEvent:
        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_MISSING")
            return self._read_events_locked(path)[-1]

    def advance(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        state: GcpV2DiskCleanupState,
        now: datetime,
        delete_attempts: int | None = None,
        operation: GcpV2DiskDeleteOperation | None = None,
        absence: GcpV2DiskAbsenceObservation | None = None,
        error_code: str | None = None,
    ) -> GcpV2DiskCleanupJournalEvent:
        """Append one exact durable transition after local validation."""

        binding = _strict_binding(binding)
        if error_code is not None and _ERROR_RE.fullmatch(error_code) is None:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_ERROR_CODE_INVALID")
        try:
            occurred_at = _timestamp(now)
        except ValueError:
            raise GcpV2DiskCleanupError("DISK_JOURNAL_TIME_INVALID") from None
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_MISSING")
            events = self._read_events_locked(path)
            previous = events[-1]
            if previous.binding != binding:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_BINDING_MISMATCH")
            attempts = (
                previous.delete_attempts if delete_attempts is None else delete_attempts
            )
            event = _event(
                sequence=previous.sequence + 1,
                state=state,
                binding=binding,
                occurred_at=occurred_at,
                delete_attempts=attempts,
                operation=operation,
                absence=absence,
                error_code=error_code,
                previous_event_digest=previous.event_digest,
            )
            candidate = [*events, event]
            self._validate_transitions(candidate)
            raw = canonical_json_bytes(_model_value(event)) + b"\n"
            try:
                metadata = path.lstat()
            except OSError:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNAVAILABLE") from None
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o022
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_UNSAFE")
            size = metadata.st_size
            if (
                len(candidate) > GCP_V2_DISK_CLEANUP_MAX_EVENTS
                or size + len(raw) > GCP_V2_DISK_CLEANUP_MAX_EVENT_BYTES
            ):
                raise GcpV2DiskCleanupError("DISK_JOURNAL_BOUND_EXCEEDED")
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_APPEND
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._crash_point("disk_cleanup_advance_after_event_fsync")
                self._fsync_directory()
                self._crash_point("disk_cleanup_advance_after_directory_fsync")
            except OSError:
                raise GcpV2DiskCleanupError("DISK_JOURNAL_APPEND_FAILED") from None
            return event


def _strict_provider_value(
    value: GcpExecutionModel, model_type: type[GcpExecutionModel], *, code: str
) -> GcpExecutionModel:
    try:
        raw = canonical_json_bytes(_model_value(value))
        parsed = model_type.model_validate_json(raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpV2DiskCleanupError(code) from None
    if canonical_json_bytes(_model_value(parsed)) != raw:
        raise GcpV2DiskCleanupError(code)
    return parsed


def _outcome(
    event: GcpV2DiskCleanupJournalEvent,
) -> GcpV2DiskCleanupOutcome:
    if event.state == "ABSENCE_CONFIRMED":
        state: Literal["ABSENCE_CONFIRMED", "BLOCKED"] = "ABSENCE_CONFIRMED"
    elif event.state == "BLOCKED":
        state = "BLOCKED"
    else:
        raise GcpV2DiskCleanupError("DISK_CLEANUP_NOT_TERMINAL")
    return GcpV2DiskCleanupOutcome(
        schema_version=GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_VERSION,
        binding_id=event.binding.binding_id,
        state=state,
        delete_attempts=event.delete_attempts,
        operation_id=event.operation.operation_id if event.operation else None,
        absence=event.absence,
        error_code=event.error_code,
    )


def _confirm_absence(
    binding: GcpV2DiskCleanupBinding,
    *,
    provider: GcpV2DiskCleanupTransport,
    timeout_seconds: int,
) -> GcpV2DiskAbsenceObservation | None:
    try:
        raw = provider.confirm_exact_boot_disk_absent(
            binding, timeout_seconds=timeout_seconds
        )
    except GcpV2DiskCleanupError as error:
        if error.code == "DISK_STILL_PRESENT":
            return None
        raise
    parsed = _strict_provider_value(
        raw, GcpV2DiskAbsenceObservation, code="DISK_ABSENCE_INVALID"
    )
    if not isinstance(parsed, GcpV2DiskAbsenceObservation) or not (
        _absence_matches_binding(parsed, binding)
    ):
        raise GcpV2DiskCleanupError("DISK_ABSENCE_MISMATCH")
    return parsed


def _validate_observed_disk(
    binding: GcpV2DiskCleanupBinding,
    *,
    provider: GcpV2DiskCleanupTransport,
    timeout_seconds: int,
) -> None:
    raw = provider.read_exact_owned_boot_disk(binding, timeout_seconds=timeout_seconds)
    parsed = _strict_provider_value(
        raw, GcpV2ExactOwnedBootDisk, code="DISK_OBSERVATION_INVALID"
    )
    if not isinstance(parsed, GcpV2ExactOwnedBootDisk) or not (
        _observed_disk_matches_binding(parsed, binding)
    ):
        raise GcpV2DiskCleanupError("DISK_OWNERSHIP_MISMATCH")


def _validate_operation(
    raw: GcpV2DiskDeleteOperation, binding: GcpV2DiskCleanupBinding
) -> GcpV2DiskDeleteOperation:
    parsed = _strict_provider_value(
        raw, GcpV2DiskDeleteOperation, code="DISK_OPERATION_INVALID"
    )
    if not isinstance(parsed, GcpV2DiskDeleteOperation) or not (
        _operation_matches_binding(parsed, binding)
    ):
        raise GcpV2DiskCleanupError("DISK_OPERATION_MISMATCH", ambiguous=True)
    return parsed


def _validate_result(
    raw: GcpV2DiskDeleteResult, operation: GcpV2DiskDeleteOperation
) -> GcpV2DiskDeleteResult:
    parsed = _strict_provider_value(
        raw, GcpV2DiskDeleteResult, code="DISK_RESULT_INVALID"
    )
    if not isinstance(parsed, GcpV2DiskDeleteResult) or (
        parsed.operation_id != operation.operation_id
    ):
        raise GcpV2DiskCleanupError("DISK_RESULT_MISMATCH", ambiguous=True)
    return parsed


def _block(
    journal: GcpV2DiskCleanupJournal,
    binding: GcpV2DiskCleanupBinding,
    current: GcpV2DiskCleanupJournalEvent,
    *,
    now: datetime,
    error_code: str,
) -> GcpV2DiskCleanupOutcome:
    event = journal.advance(
        binding,
        state="BLOCKED",
        now=now,
        delete_attempts=current.delete_attempts,
        operation=current.operation,
        error_code=error_code,
    )
    return _outcome(event)


def cleanup_exact_owned_boot_disk(
    binding: GcpV2DiskCleanupBinding,
    *,
    journal: GcpV2DiskCleanupJournal,
    provider: GcpV2DiskCleanupTransport,
    now: datetime,
    timeout_seconds: int = 5,
    max_delete_attempts: int = 3,
    max_reconcile_attempts: int = 3,
) -> GcpV2DiskCleanupOutcome:
    """Delete and confirm exactly one observed boot disk, or fail closed.

    The helper first asks the provider for an authoritative *exact* absence
    result.  It only persists delete intent after the observed disk exactly
    equals the immutable binding.  A timeout or lost response is reconciled
    using the deterministic request identity; no host-derived target or broad
    inventory is accepted.
    """

    binding = _strict_binding(binding)
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise GcpV2DiskCleanupError("DISK_TIMEOUT_INVALID")
    if not 1 <= max_delete_attempts <= 16:
        raise GcpV2DiskCleanupError("DISK_MAX_DELETE_ATTEMPTS_INVALID")
    if not 1 <= max_reconcile_attempts <= 16:
        raise GcpV2DiskCleanupError("DISK_MAX_RECONCILE_ATTEMPTS_INVALID")
    current = journal.prepare(binding, now=now)
    if current.state == "ABSENCE_CONFIRMED":
        return _outcome(current)

    # ``timeout_seconds`` is the whole locally-authorized cleanup attempt,
    # not a fresh timeout for every provider call.  Floor each remaining
    # provider timeout so a sequence of read/reconcile/delete calls cannot
    # silently run past the bound that the watchdog promised to enforce.
    deadline = time.monotonic() + float(timeout_seconds)

    def remaining_timeout() -> int:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise GcpV2DiskCleanupError("DISK_CLEANUP_DEADLINE_EXPIRED")
        return min(timeout_seconds, int(remaining))

    # An explicit retry never reopens a permanent failure blindly.  It may
    # only turn BLOCKED into a confirmed result when authoritative absence is
    # later observed.
    try:
        absence = _confirm_absence(
            binding, provider=provider, timeout_seconds=remaining_timeout()
        )
    except GcpV2DiskCleanupError as error:
        return _block(journal, binding, current, now=now, error_code=error.code)
    if absence is not None:
        event = journal.advance(
            binding,
            state="ABSENCE_CONFIRMED",
            now=now,
            delete_attempts=current.delete_attempts,
            operation=current.operation,
            absence=absence,
        )
        return _outcome(event)
    if current.state == "BLOCKED":
        return _outcome(current)

    try:
        _validate_observed_disk(
            binding, provider=provider, timeout_seconds=remaining_timeout()
        )
    except GcpV2DiskCleanupError as error:
        return _block(journal, binding, current, now=now, error_code=error.code)

    request_id = gcp_v2_disk_delete_request_id(binding)
    reconcile_count = 0
    while reconcile_count < max_reconcile_attempts:
        operation = current.operation
        if current.state in {"DELETE_SUBMITTED", "DELETE_RECONCILING"} and operation:
            try:
                result = _validate_result(
                    provider.reconcile_exact_disk_delete(
                        operation, timeout_seconds=remaining_timeout()
                    ),
                    operation,
                )
            except GcpV2DiskCleanupError as error:
                return _block(journal, binding, current, now=now, error_code=error.code)
            current = journal.advance(
                binding,
                state="DELETE_RECONCILING",
                now=now,
                delete_attempts=current.delete_attempts,
                operation=operation,
            )
            reconcile_count += 1
            if result.status == "DONE":
                try:
                    absence = _confirm_absence(
                        binding,
                        provider=provider,
                        timeout_seconds=remaining_timeout(),
                    )
                except GcpV2DiskCleanupError as error:
                    return _block(
                        journal, binding, current, now=now, error_code=error.code
                    )
                if absence is None:
                    return _block(
                        journal,
                        binding,
                        current,
                        now=now,
                        error_code="DISK_ABSENCE_UNCONFIRMED",
                    )
                event = journal.advance(
                    binding,
                    state="ABSENCE_CONFIRMED",
                    now=now,
                    delete_attempts=current.delete_attempts,
                    operation=operation,
                    absence=absence,
                )
                return _outcome(event)
            if result.status == "ERROR":
                return _block(
                    journal,
                    binding,
                    current,
                    now=now,
                    error_code=result.error_code or "DISK_DELETE_FAILED",
                )
            # TIMEOUT: do not submit another mutation.  Reconcile the exact
            # returned operation only, with a bounded local retry count.
            continue

        if current.state in {"DELETE_INTENT", "DELETE_RECONCILING"}:
            # A process may have died after the intent fsync.  Use the same
            # deterministic request ID instead of making a new delete intent.
            # The reconciling/no-handle case is the same lost-response path:
            # reissuing the exact id may return its original operation, but it
            # never creates a second disk target or an unrecorded intent.
            pass
        elif current.delete_attempts >= max_delete_attempts:
            return _block(
                journal,
                binding,
                current,
                now=now,
                error_code="DISK_DELETE_RETRY_EXHAUSTED",
            )
        else:
            current = journal.advance(
                binding,
                state="DELETE_INTENT",
                now=now,
                delete_attempts=current.delete_attempts + 1,
            )
        try:
            operation = _validate_operation(
                provider.delete_exact_boot_disk(
                    binding,
                    request_id=request_id,
                    timeout_seconds=remaining_timeout(),
                ),
                binding,
            )
        except GcpV2DiskCleanupError as error:
            if error.ambiguous:
                current = journal.advance(
                    binding,
                    state="DELETE_RECONCILING",
                    now=now,
                    delete_attempts=current.delete_attempts,
                    operation=None,
                )
                # Re-observe the exact identity before trying the same request
                # ID.  This makes a lost response recoverable without a broad
                # provider list or a new mutation identity.
                try:
                    absence = _confirm_absence(
                        binding,
                        provider=provider,
                        timeout_seconds=remaining_timeout(),
                    )
                except GcpV2DiskCleanupError as absence_error:
                    return _block(
                        journal,
                        binding,
                        current,
                        now=now,
                        error_code=absence_error.code,
                    )
                if absence is not None:
                    event = journal.advance(
                        binding,
                        state="ABSENCE_CONFIRMED",
                        now=now,
                        delete_attempts=current.delete_attempts,
                        absence=absence,
                    )
                    return _outcome(event)
                try:
                    _validate_observed_disk(
                        binding,
                        provider=provider,
                        timeout_seconds=remaining_timeout(),
                    )
                except GcpV2DiskCleanupError as observation_error:
                    return _block(
                        journal,
                        binding,
                        current,
                        now=now,
                        error_code=observation_error.code,
                    )
                reconcile_count += 1
                continue
            return _block(journal, binding, current, now=now, error_code=error.code)
        current = journal.advance(
            binding,
            state="DELETE_SUBMITTED",
            now=now,
            delete_attempts=current.delete_attempts,
            operation=operation,
        )

    return _block(
        journal,
        binding,
        current,
        now=now,
        error_code="DISK_OPERATION_RECONCILIATION_TIMEOUT",
    )


class GcpV2ExactDiskCleanupCoordinator:
    """Inject an already-observed v2 disk binding into core cleanup safely.

    The coordinator is intentionally callable by the core controller but keeps
    all disk identity, delete-operation, and recovery state in additive v2
    contracts.  It refuses any lease whose exact immutable request/location/
    labels/provenance facts do not match the observed disk binding.
    """

    def __init__(
        self,
        *,
        binding: GcpV2DiskCleanupBinding,
        journal: GcpV2DiskCleanupJournal,
        provider: GcpV2DiskCleanupTransport,
        timeout_seconds: int = 5,
        max_delete_attempts: int = 3,
        max_reconcile_attempts: int = 3,
    ) -> None:
        self.binding = _strict_binding(binding)
        self.journal = journal
        if not is_gcp_v2_authority_bound_disk_transport(provider):
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_TRANSPORT_REQUIRED")
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.max_delete_attempts = max_delete_attempts
        self.max_reconcile_attempts = max_reconcile_attempts

    def _matches_record(self, record: GcpLeaseRecord) -> bool:
        disk = self.binding.disk
        attached_instance = self.binding.attached_instance
        request = record.request
        return (
            self.binding.request_digest == record.request_digest
            and self.binding.project_id == record.project_id
            and self.binding.zone == record.zone
            and self.binding.controller_id == record.controller_id
            and self.binding.instance_name == record.instance_name
            and self.binding.labels == record.labels
            and attached_instance.request_digest == record.request_digest
            and attached_instance.project_id == record.project_id
            and attached_instance.region == record.region
            and attached_instance.zone == record.zone
            and attached_instance.instance_name == record.instance_name
            and attached_instance.labels == record.labels
            and disk.request_digest == record.request_digest
            and disk.project_id == record.project_id
            and disk.zone == record.zone
            and disk.instance_name == record.instance_name
            and disk.labels == record.labels
            and disk.attached_instance_self_link
            == attached_instance.self_link
            and disk.attached_instance_provider_id
            == attached_instance.provider_instance_id
            and disk.boot_source_disk_self_link == disk.self_link
            and disk.boot_attachment is True
            and disk.source_image_name == request.boot_image.image_name
            and disk.source_image_provider_id == request.boot_image.provider_image_id
            and disk.source_image_digest == request.boot_image.digest
            and disk.disk_type == request.boot_disk_type
            and disk.size_gib == request.boot_disk_size_gib
        )

    def __call__(
        self, record: GcpLeaseRecord, now: datetime
    ) -> GcpV2DiskCleanupOutcome:
        if not self._matches_record(record):
            raise GcpV2DiskCleanupError("DISK_CLEANUP_LEASE_MISMATCH")
        if (
            self.timeout_seconds > record.cleanup_timeout_seconds
            or self.max_delete_attempts > record.max_cleanup_attempts
            or self.max_reconcile_attempts > record.max_cleanup_attempts
        ):
            raise GcpV2DiskCleanupError("DISK_CLEANUP_BUDGET_EXCEEDED")
        return cleanup_exact_owned_boot_disk(
            self.binding,
            journal=self.journal,
            provider=self.provider,
            now=now,
            timeout_seconds=self.timeout_seconds,
            max_delete_attempts=self.max_delete_attempts,
            max_reconcile_attempts=self.max_reconcile_attempts,
        )

    @property
    def configuration_digest(self) -> Sha256Digest:
        """Return the durable local configuration identity for this coordinator.

        The digest intentionally includes the physical journal root and every
        bounded retry setting.  It is a content-addressed local binding, not
        a provider credential or an authorization signature.  A restarted
        watchdog uses it to reject a coordinator pointed at a different
        sidecar journal.
        """

        root = self.journal._checked_root()
        payload = {
            "kind": "gcp_v2_exact_disk_cleanup_coordinator",
            "binding_id": self.binding.binding_id,
            "journal_root": os.path.realpath(os.fspath(root)),
            "timeout_seconds": self.timeout_seconds,
            "max_delete_attempts": self.max_delete_attempts,
            "max_reconcile_attempts": self.max_reconcile_attempts,
        }
        return digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
        )


class GcpV2LocalDiskCleanupFactory:
    """Nominal local-only factory for a bound exact-disk coordinator.

    It stores no provider credentials and performs no provider operation.  A
    watchdog can use it only after its journal supplies a canonical exact disk
    binding; each resulting coordinator keeps its own fsync-backed journal.
    """

    def __init__(
        self,
        *,
        provider_supplier: Callable[[], GcpV2DiskCleanupTransport],
        journal_root: Path,
        now_fn: Callable[[], datetime],
        startup_projection: object,
        timeout_seconds: int = 5,
        max_delete_attempts: int = 3,
        max_reconcile_attempts: int = 3,
    ) -> None:
        if not callable(provider_supplier):
            raise GcpV2DiskCleanupError("DISK_PROVIDER_SUPPLIER_INVALID")
        self._provider_supplier = provider_supplier
        self._provider: GcpV2DiskCleanupTransport | None = None
        self._provider_lock = threading.Lock()
        self._now_fn = now_fn
        try:
            from inferdrome.deployment.gcp_v2_contracts import GcpV2StartupProjection

            if not isinstance(startup_projection, GcpExecutionModel):
                raise TypeError("startup projection is not a GCP execution model")
            projection_raw = canonical_json_bytes(_model_value(startup_projection))
            self._startup_projection = GcpV2StartupProjection.model_validate_json(
                projection_raw
            )
            if (
                canonical_json_bytes(_model_value(self._startup_projection))
                != projection_raw
            ):
                raise ValueError("noncanonical startup projection")
        except (AttributeError, ImportError, ValidationError, TypeError, ValueError):
            raise GcpV2DiskCleanupError("DISK_STARTUP_PROJECTION_INVALID") from None
        # Validate the sidecar root immediately, before any future create
        # authority can be armed.  This is deliberately a local filesystem
        # check only; it neither discovers credentials nor calls a provider.
        journal = GcpV2DiskCleanupJournal(journal_root)
        self._journal_root = journal._checked_root()
        self._timeout_seconds = timeout_seconds
        self._max_delete_attempts = max_delete_attempts
        self._max_reconcile_attempts = max_reconcile_attempts

    @property
    def configuration_digest(self) -> Sha256Digest:
        path = os.path.realpath(os.fspath(self._journal_root))
        payload = {
            "kind": "gcp_v2_local_disk_cleanup_factory",
            "journal_root": path,
            "timeout_seconds": self._timeout_seconds,
            "max_delete_attempts": self._max_delete_attempts,
            "max_reconcile_attempts": self._max_reconcile_attempts,
            "startup_projection_digest": self._startup_projection.projection_id,
        }
        return digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
        )

    @property
    def journal_root(self) -> Path:
        """Return the prevalidated, non-symlinked sidecar root."""

        return self._journal_root

    def _lazy_provider(self) -> GcpV2DiskCleanupTransport:
        """Construct an injected disk provider only after authority validates."""

        with self._provider_lock:
            if self._provider is None:
                try:
                    candidate = self._provider_supplier()
                except BaseException:
                    raise GcpV2DiskCleanupError(
                        "DISK_PROVIDER_SUPPLIER_FAILED"
                    ) from None
                if candidate is None:
                    raise GcpV2DiskCleanupError("DISK_PROVIDER_SUPPLIER_FAILED")
                self._provider = candidate
            return self._provider

    @staticmethod
    def _strict_inventory(value: object) -> GcpV2OwnedBootDiskInventory:
        try:
            if not isinstance(value, GcpExecutionModel):
                raise TypeError("inventory is not a GCP execution model")
            raw = canonical_json_bytes(_model_value(value))
            parsed = GcpV2OwnedBootDiskInventory.model_validate_json(raw)
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise GcpV2DiskCleanupError("DISK_INVENTORY_INVALID") from None
        if canonical_json_bytes(_model_value(parsed)) != raw:
            raise GcpV2DiskCleanupError("DISK_INVENTORY_INVALID")
        return parsed

    @staticmethod
    def _strict_instance(value: object) -> GcpV2ExactOwnedInstance:
        try:
            if not isinstance(value, GcpExecutionModel):
                raise TypeError("instance is not a GCP execution model")
            raw = canonical_json_bytes(_model_value(value))
            parsed = GcpV2ExactOwnedInstance.model_validate_json(raw)
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise GcpV2DiskCleanupError("OWNED_INSTANCE_OBSERVATION_INVALID") from None
        if canonical_json_bytes(_model_value(parsed)) != raw:
            raise GcpV2DiskCleanupError("OWNED_INSTANCE_OBSERVATION_INVALID")
        return parsed

    def _bound_provider(
        self,
        *,
        record: GcpLeaseRecord,
        binding: GcpV2DiskCleanupBinding,
        authority: object,
        now: datetime,
    ) -> GcpV2AuthorityBoundDiskCleanupTransport:
        """Mint the only coordinator-visible provider adapter after authority.

        A sealed activation proof is required on the post-create controller
        path.  The independently durable watchdog reconstructs from its
        persisted capability, and recovery uses the already-validated exact
        cleanup authorization.  All three adapters recheck their horizon on
        every provider method.
        """

        try:
            from inferdrome.deployment.gcp_supervisor import (
                GcpV2ActivatedMutationProof,
                consume_gcp_v2_activated_mutation_proof,
            )
            from inferdrome.deployment.gcp_v2_contracts import (
                GcpCleanupRecoveryAuthorization,
                GcpV2MutationCapability,
                gcp_v2_startup_projection_digest,
                validate_gcp_v2_startup_projection,
            )
        except ImportError:
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_UNAVAILABLE") from None
        authority_kind: Literal["capability", "recovery"]
        sealed_authority: object
        seal: object
        if isinstance(authority, GcpV2ActivatedMutationProof):
            try:
                sealed_authority = consume_gcp_v2_activated_mutation_proof(
                    authority, now=now, purpose="disk_cleanup"
                ).capability
            except GcpExecutionError:
                raise GcpV2DiskCleanupError("DISK_CAPABILITY_INVALID") from None
            authority_kind = "capability"
            seal = _CAPABILITY_BOUND_DISK_TRANSPORT_SEAL
        elif type(authority) is GcpV2MutationCapability:
            # This route is only used by FileGcpWatchdog after it has loaded
            # and validated its own fsync-backed activation event.  It is not
            # exposed through the guarded controller's post-create path.
            sealed_authority = authority
            authority_kind = "capability"
            seal = _CAPABILITY_BOUND_DISK_TRANSPORT_SEAL
        elif type(authority) is GcpCleanupRecoveryAuthorization:
            sealed_authority = authority
            authority_kind = "recovery"
            seal = _RECOVERY_BOUND_DISK_TRANSPORT_SEAL
        else:
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_INVALID")
        try:
            expected_projection_digest = sealed_authority.startup_projection_digest
            expected_payload_digest = sealed_authority.execution_payload_digest
            if (
                self._startup_projection.request_digest != record.request_digest
                or gcp_v2_startup_projection_digest(self._startup_projection)
                != expected_projection_digest
                or self._startup_projection.execution_payload_digest
                != expected_payload_digest
            ):
                raise ValueError("startup projection mismatch")
            validate_gcp_v2_startup_projection(
                self._startup_projection,
                request=record.request,
                execution_payload_digest=expected_payload_digest,
            )
        except (AttributeError, GcpExecutionError, ValueError):
            raise GcpV2DiskCleanupError("DISK_AUTHORITY_PAYLOAD_MISMATCH") from None
        # The sealed adapter validates authority now and leaves the raw
        # supplier untouched until one exact provider operation is actually
        # required.  This avoids SDK/ADC initialization on malformed,
        # expired, or otherwise mismatched recovery input.
        return GcpV2AuthorityBoundDiskCleanupTransport(
            provider_supplier=self._lazy_provider,
            binding=binding,
            authority=sealed_authority,
            authority_kind=authority_kind,
            startup_projection_digest=gcp_v2_startup_projection_digest(
                self._startup_projection
            ),
            execution_payload_digest=self._startup_projection.execution_payload_digest,
            now_fn=self._now_fn,
            _seal=seal,
        )

    def bind(
        self,
        *,
        record: GcpLeaseRecord,
        binding: GcpV2DiskCleanupBinding,
        authority: object,
        now: datetime,
    ) -> GcpV2ExactDiskCleanupCoordinator:
        if type(binding) is not GcpV2DiskCleanupBinding:
            raise GcpV2DiskCleanupError("DISK_CLEANUP_BINDING_INVALID")
        binding = _strict_binding(binding)
        if (
            binding.request_digest != record.request_digest
            or binding.project_id != record.project_id
            or binding.zone != record.zone
            or binding.controller_id != record.controller_id
            or binding.instance_name != record.instance_name
            or binding.labels != record.labels
        ):
            raise GcpV2DiskCleanupError("DISK_CLEANUP_LEASE_MISMATCH")
        return GcpV2ExactDiskCleanupCoordinator(
            binding=binding,
            journal=GcpV2DiskCleanupJournal(self._journal_root),
            provider=self._bound_provider(
                record=record, binding=binding, authority=authority, now=now
            ),
            timeout_seconds=self._timeout_seconds,
            max_delete_attempts=self._max_delete_attempts,
            max_reconcile_attempts=self._max_reconcile_attempts,
        )

    def bind_inventory(
        self,
        *,
        record: GcpLeaseRecord,
        inventory: object,
        attached_instance: object,
        authority: object,
        now: datetime,
    ) -> GcpV2ExactDiskCleanupCoordinator:
        """Bind a complete provider-returned exact inventory after create.

        This boundary refuses to manufacture an inventory-complete assertion
        from one disk.  The caller must provide a canonical, label-scoped,
        complete inventory and a separate exact attached-instance observation.
        """

        inventory = self._strict_inventory(inventory)
        instance = self._strict_instance(attached_instance)
        request = record.request
        if (
            inventory.request_digest != record.request_digest
            or inventory.project_id != record.project_id
            or inventory.zone != record.zone
            or inventory.instance_name != record.instance_name
            or inventory.labels != record.labels
            or inventory.pagination_complete is not True
            or len(inventory.disks) != 1
            or instance.request_digest != record.request_digest
            or instance.project_id != record.project_id
            or instance.region != record.region
            or instance.zone != record.zone
            or instance.instance_name != record.instance_name
            or instance.labels != record.labels
        ):
            raise GcpV2DiskCleanupError("DISK_INVENTORY_BINDING_MISMATCH")
        disk = inventory.disks[0]
        if (
            disk.source_image_name != request.boot_image.image_name
            or disk.source_image_provider_id != request.boot_image.provider_image_id
            or disk.source_image_digest != request.boot_image.digest
            or disk.disk_type != request.boot_disk_type
            or disk.size_gib != request.boot_disk_size_gib
            or disk.attached_instance_provider_id != instance.provider_instance_id
            or disk.attached_instance_self_link != instance.self_link
        ):
            raise GcpV2DiskCleanupError("DISK_INVENTORY_BINDING_MISMATCH")
        return self.bind(
            record=record,
            binding=issue_gcp_v2_disk_cleanup_binding(
                inventory,
                controller_id=record.controller_id,
                attached_instance=instance,
            ),
            authority=authority,
            now=now,
        )

    def bind_recovery(
        self,
        *,
        record: GcpLeaseRecord,
        authorization: object,
        now: datetime,
    ) -> GcpV2ExactDiskCleanupCoordinator:
        """Bind one existing exact disk only for a validated cleanup recovery.

        This nominal factory cannot issue an authorization and has no create
        method.  It rechecks the narrow recovery shape before it can produce a
        coordinator, so a fresh controller cannot substitute a disk or broaden
        a cleanup-only action while rebuilding its sidecar state.
        """

        binding = getattr(authorization, "disk_cleanup_binding", None)
        if type(binding) is not GcpV2DiskCleanupBinding:
            raise GcpV2DiskCleanupError("DISK_RECOVERY_BINDING_INVALID")
        if (
            getattr(authorization, "create_authority", None) is not False
            or getattr(authorization, "request_digest", None)
            != record.request_digest
            or getattr(authorization, "controller_id", None) != record.controller_id
            or getattr(authorization, "project_id", None) != record.project_id
            or getattr(authorization, "region", None) != record.region
            or getattr(authorization, "zone", None) != record.zone
            or getattr(authorization, "instance_name", None) != record.instance_name
            or getattr(authorization, "labels", None) != record.labels
            or getattr(authorization, "lease_intent_anchor_digest", None)
            != record.intent_anchor_digest
            or getattr(authorization, "arm_id", None) != record.arm_id
            or getattr(authorization, "plan_id", None) != record.plan_id
            or getattr(authorization, "allowed_actions", None)
            != (
                "reconcile_operation",
                "get_exact_instance",
                "list_exact_label_inventory",
                "delete_exact_instance",
                "get_exact_disk",
                "delete_exact_disk",
                "confirm_absence",
            )
        ):
            raise GcpV2DiskCleanupError("DISK_RECOVERY_AUTHORIZATION_MISMATCH")
        return self.bind(
            record=record,
            binding=binding,
            authority=authorization,
            now=now,
        )


def gcp_v2_disk_cleanup_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return generated additive schemas for exact disk cleanup only."""

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        (
            "gcp-owned-instance.schema.json",
            GcpV2ExactOwnedInstance,
            GCP_V2_OWNED_INSTANCE_SCHEMA_ID,
        ),
        (
            "gcp-owned-boot-disk.schema.json",
            GcpV2ExactOwnedBootDisk,
            GCP_V2_OWNED_BOOT_DISK_SCHEMA_ID,
        ),
        (
            "gcp-owned-boot-disk-inventory.schema.json",
            GcpV2OwnedBootDiskInventory,
            GCP_V2_OWNED_BOOT_DISK_INVENTORY_SCHEMA_ID,
        ),
        (
            "gcp-disk-cleanup-binding.schema.json",
            GcpV2DiskCleanupBinding,
            GCP_V2_DISK_CLEANUP_BINDING_SCHEMA_ID,
        ),
        (
            "gcp-disk-delete-operation.schema.json",
            GcpV2DiskDeleteOperation,
            GCP_V2_DISK_DELETE_OPERATION_SCHEMA_ID,
        ),
        (
            "gcp-disk-delete-result.schema.json",
            GcpV2DiskDeleteResult,
            GCP_V2_DISK_DELETE_RESULT_SCHEMA_ID,
        ),
        (
            "gcp-disk-absence-observation.schema.json",
            GcpV2DiskAbsenceObservation,
            GCP_V2_DISK_ABSENCE_SCHEMA_ID,
        ),
        (
            "gcp-disk-cleanup-event.schema.json",
            GcpV2DiskCleanupJournalEvent,
            GCP_V2_DISK_CLEANUP_EVENT_SCHEMA_ID,
        ),
        (
            "gcp-disk-cleanup-outcome.schema.json",
            GcpV2DiskCleanupOutcome,
            GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output


class _FakeDiskProviderState(GcpExecutionModel):
    """Strict local state for :class:`FileBackedFakeDiskProvider`."""

    schema_version: Literal["inferdrome.gcp-file-backed-fake-disk-provider.v2"]
    observation: GcpV2ExactOwnedBootDisk
    present: bool
    operation: GcpV2DiskDeleteOperation | None = None
    operation_status: Literal["PENDING", "DONE", "ERROR"] | None = None
    operation_error: GcpV2DiskCleanupErrorCode | None = None
    configured_operation_error: GcpV2DiskCleanupErrorCode | None = None
    lost_delete_response_once: bool = False
    reconciliation_timeouts_remaining: int = Field(strict=True, ge=0, le=16)
    ambiguous_inventory: bool = False
    delete_calls: int = Field(strict=True, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.operation is None and (
            self.operation_status is not None or self.operation_error is not None
        ):
            raise ValueError("fake disk operation state is inconsistent")
        if self.operation_status != "ERROR" and self.operation_error is not None:
            raise ValueError("fake disk operation error is inconsistent")
        if self.operation_status == "ERROR" and self.operation_error is None:
            raise ValueError("fake disk operation error is required")
        return self


class FileBackedFakeDiskProvider:
    """A local, process-shareable fake exact-disk provider.

    It stores only a strict boot-disk observation and deterministic fake
    operation state in a caller-provided directory.  It never imports a cloud
    SDK or reads credentials.  Separate processes may instantiate it against
    the same root to model controller death and watchdog recovery.
    """

    _STATE_FILE: Final = "state.json"
    _LOCK_FILE: Final = ".provider.lock"

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    @classmethod
    def initialize(
        cls,
        root: Path,
        *,
        observation: GcpV2ExactOwnedBootDisk,
        lost_delete_response_once: bool = False,
        reconciliation_timeouts: int = 0,
        ambiguous_inventory: bool = False,
        operation_error: str | None = None,
    ) -> FileBackedFakeDiskProvider:
        """Create strict local fake state for one disk, without provider I/O."""

        if not root.is_absolute():
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_INVALID")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = root.lstat()
        except OSError:
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_UNAVAILABLE") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_UNSAFE")
        if operation_error is not None and _ERROR_RE.fullmatch(operation_error) is None:
            raise GcpV2DiskCleanupError("FAKE_DISK_ERROR_CODE_INVALID")
        provider = cls(root)
        state = _FakeDiskProviderState(
            schema_version="inferdrome.gcp-file-backed-fake-disk-provider.v2",
            observation=observation,
            present=True,
            operation=None,
            operation_status=None,
            operation_error=None,
            configured_operation_error=operation_error,
            lost_delete_response_once=lost_delete_response_once,
            reconciliation_timeouts_remaining=reconciliation_timeouts,
            ambiguous_inventory=ambiguous_inventory,
            delete_calls=0,
        )
        provider._write_state(state)
        return provider

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_INVALID")
        try:
            metadata = self.root.lstat()
        except OSError:
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_UNAVAILABLE") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GcpV2DiskCleanupError("FAKE_DISK_PATH_UNSAFE")
        return self.root

    @contextmanager
    def _exclusive(self) -> Iterator[Path]:
        root = self._checked_root()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / self._LOCK_FILE,
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
                raise GcpV2DiskCleanupError("FAKE_DISK_LOCK_UNAVAILABLE") from None
            with self._lock:
                yield root
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def _fsync_directory(self) -> None:
        descriptor = os.open(
            self._checked_root(), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _state_path(self) -> Path:
        return self._checked_root() / self._STATE_FILE

    def _read_state(self) -> _FakeDiskProviderState:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._state_path(),
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_V2_DISK_PROVIDER_MAX_BYTES
            ):
                raise GcpV2DiskCleanupError("FAKE_DISK_STATE_UNSAFE")
            raw = os.read(descriptor, GCP_V2_DISK_PROVIDER_MAX_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_V2_DISK_PROVIDER_MAX_BYTES
                or final.st_ino != metadata.st_ino
                or final.st_size != len(raw)
            ):
                raise GcpV2DiskCleanupError("FAKE_DISK_STATE_CHANGED")
            _preflight_json(raw, kind="file-backed fake disk provider state")
            state = _FakeDiskProviderState.model_validate_json(raw)
            if canonical_json_bytes(_model_value(state)) != raw:
                raise GcpV2DiskCleanupError("FAKE_DISK_STATE_NONCANONICAL")
            return state
        except GcpV2DiskCleanupError:
            raise
        except (OSError, ValidationError, ValueError):
            raise GcpV2DiskCleanupError("FAKE_DISK_STATE_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write_state(self, state: _FakeDiskProviderState) -> None:
        root = self._checked_root()
        destination = self._state_path()
        stage = root / f".{self._STATE_FILE}.{uuid.uuid4().hex}.stage"
        raw = canonical_json_bytes(_model_value(state))
        descriptor: int | None = None
        try:
            descriptor = os.open(
                stage,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _write_all(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                descriptor = None
            os.replace(stage, destination)
            self._fsync_directory()
        except OSError:
            raise GcpV2DiskCleanupError("FAKE_DISK_STATE_WRITE_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if stage.exists() and not stage.is_symlink():
                    stage.unlink()
            except OSError:
                pass

    @staticmethod
    def _operation_for(
        binding: GcpV2DiskCleanupBinding, request_id: GcpRequestUuid
    ) -> GcpV2DiskDeleteOperation:
        operation_suffix = request_id.replace("-", "")[:16]
        return GcpV2DiskDeleteOperation(
            schema_version=GCP_V2_DISK_DELETE_OPERATION_SCHEMA_VERSION,
            operation_id=f"disk-op-{operation_suffix}",
            operation_name=(
                f"projects/{binding.project_id}/zones/{binding.zone}/operations/"
                f"disk-op-{operation_suffix}"
            ),
            request_id=request_id,
            request_digest=binding.request_digest,
            project_id=binding.project_id,
            zone=binding.zone,
            instance_name=binding.instance_name,
            disk_name=binding.disk.disk_name,
            labels=binding.labels,
        )

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        del timeout_seconds
        binding = _strict_binding(binding)
        with self._exclusive():
            state = self._read_state()
            if state.ambiguous_inventory:
                raise GcpV2DiskCleanupError("DISK_INVENTORY_AMBIGUOUS")
            if not state.present:
                raise GcpV2DiskCleanupError("DISK_NOT_FOUND")
            return state.observation

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: GcpRequestUuid,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        del timeout_seconds
        binding = _strict_binding(binding)
        if request_id != gcp_v2_disk_delete_request_id(binding):
            raise GcpV2DiskCleanupError("DISK_REQUEST_ID_MISMATCH")
        with self._exclusive():
            state = self._read_state()
            if state.ambiguous_inventory:
                raise GcpV2DiskCleanupError("DISK_INVENTORY_AMBIGUOUS")
            if not state.present:
                raise GcpV2DiskCleanupError("DISK_NOT_FOUND")
            if not _observed_disk_matches_binding(state.observation, binding):
                raise GcpV2DiskCleanupError("DISK_OWNERSHIP_MISMATCH")
            operation = state.operation or self._operation_for(binding, request_id)
            if state.operation is not None and state.operation != operation:
                raise GcpV2DiskCleanupError("DISK_OPERATION_AMBIGUOUS", ambiguous=True)
            state = state.model_copy(
                update={
                    "operation": operation,
                    "operation_status": "PENDING",
                    "operation_error": None,
                    "delete_calls": state.delete_calls + 1,
                }
            )
            self._write_state(state)
            if state.lost_delete_response_once:
                state = state.model_copy(update={"lost_delete_response_once": False})
                self._write_state(state)
                raise GcpV2DiskCleanupError("DISK_DELETE_RESPONSE_LOST", ambiguous=True)
            return operation

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        del timeout_seconds
        with self._exclusive():
            state = self._read_state()
            if state.operation != operation:
                raise GcpV2DiskCleanupError("DISK_OPERATION_MISMATCH", ambiguous=True)
            if state.reconciliation_timeouts_remaining > 0:
                state = state.model_copy(
                    update={
                        "reconciliation_timeouts_remaining": (
                            state.reconciliation_timeouts_remaining - 1
                        )
                    }
                )
                self._write_state(state)
                return GcpV2DiskDeleteResult(
                    schema_version=GCP_V2_DISK_DELETE_RESULT_SCHEMA_VERSION,
                    operation_id=operation.operation_id,
                    status="TIMEOUT",
                    error_code=None,
                )
            error_code = state.configured_operation_error
            if error_code is not None:
                state = state.model_copy(
                    update={"operation_status": "ERROR", "operation_error": error_code}
                )
                self._write_state(state)
                return GcpV2DiskDeleteResult(
                    schema_version=GCP_V2_DISK_DELETE_RESULT_SCHEMA_VERSION,
                    operation_id=operation.operation_id,
                    status="ERROR",
                    error_code=error_code,
                )
            state = state.model_copy(
                update={
                    "present": False,
                    "operation_status": "DONE",
                    "operation_error": None,
                }
            )
            self._write_state(state)
            return GcpV2DiskDeleteResult(
                schema_version=GCP_V2_DISK_DELETE_RESULT_SCHEMA_VERSION,
                operation_id=operation.operation_id,
                status="DONE",
                error_code=None,
            )

    def confirm_exact_boot_disk_absent(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2DiskAbsenceObservation:
        del timeout_seconds
        binding = _strict_binding(binding)
        with self._exclusive():
            state = self._read_state()
            if state.ambiguous_inventory:
                raise GcpV2DiskCleanupError("DISK_INVENTORY_AMBIGUOUS")
            if state.present:
                raise GcpV2DiskCleanupError("DISK_STILL_PRESENT")
            if not _observed_disk_matches_binding(state.observation, binding):
                raise GcpV2DiskCleanupError("DISK_OWNERSHIP_MISMATCH")
            return GcpV2DiskAbsenceObservation(
                schema_version=GCP_V2_DISK_ABSENCE_SCHEMA_VERSION,
                request_digest=binding.request_digest,
                project_id=binding.project_id,
                zone=binding.zone,
                instance_name=binding.instance_name,
                disk_name=binding.disk.disk_name,
                labels=binding.labels,
                state="NOT_FOUND",
            )
