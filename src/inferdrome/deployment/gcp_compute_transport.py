"""Optional Google Compute Engine transport for the guarded controller.

Nothing in this module imports the Google SDK or discovers ADC at import time.
``create_google_compute_transport`` is the explicit live boundary and is only
callable by an operator after the pure arm/plan/request/quote/journal gates have
passed.  Unit tests inject a fake SDK module and clients; no test constructs ADC
or contacts Google.
"""

from __future__ import annotations

import hashlib
import importlib
import ipaddress
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from inferdrome.deployment.gcp_lifecycle import (
    GCP_BOOT_DISK_ABSENCE_SCHEMA_VERSION,
    GCP_INSTANCE_SAFETY_OBSERVATION_SCHEMA_VERSION,
    GCP_OWNED_RESOURCE_INVENTORY_SCHEMA_VERSION,
    GcpBootDiskAbsenceObservation,
    GcpBootDiskObservation,
    GcpComputeTransport,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpExecutionModel,
    GcpImageObservation,
    GcpInsertRequest,
    GcpInstanceObservation,
    GcpInstanceSafetyObservation,
    GcpOperationHandle,
    GcpOperationResult,
    GcpOwnedResourceInventory,
    GcpTransportError,
    gcp_execution_request_digest,
    validate_gcp_a2_profile,
)
from inferdrome.deployment.gcp_v2_contracts import (
    GcpCleanupRecoveryAuthorization,
    GcpV2MutationCapability,
    GcpV2StartupProjection,
    gcp_v2_startup_projection_digest,
    validate_gcp_v2_startup_projection,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    GcpV2DiskAbsenceObservation,
    GcpV2DiskCleanupBinding,
    GcpV2DiskDeleteOperation,
    GcpV2DiskDeleteResult,
    GcpV2ExactOwnedBootDisk,
    GcpV2ExactOwnedInstance,
    GcpV2OwnedBootDiskInventory,
    gcp_v2_disk_delete_request_id,
)
from inferdrome.domain.digests import canonical_json_bytes

_GOOGLE_SCOPE_URLS = {
    "logging.write": "https://www.googleapis.com/auth/logging.write",
    "monitoring.write": "https://www.googleapis.com/auth/monitoring.write",
    "trace.append": "https://www.googleapis.com/auth/trace.append",
}
_SUPPORTED_COMPUTE_HOSTS = frozenset({"www.googleapis.com", "compute.googleapis.com"})
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
GCP_LIVE_MUTATIONS_ENABLED: Final[Literal[False]] = False
_CAPABILITY_BOUND_TRANSPORT_SEAL = object()
_CLEANUP_BOUND_TRANSPORT_SEAL = object()
_WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL = object()


class _LazyAuthorityTransport:
    """One inert raw-transport supplier shared by sealed v2 adapters.

    A v2 approval/capability proves only a narrow local authority edge.  It
    must not also construct a future SDK/ADC client merely because an adapter
    was armed or a watchdog runner was made durable.  The adapter therefore
    validates its current authority, request, and opaque payload binding
    first; only the immediately following provider operation can initialize
    this supplier.  A supplier failure never clears a consumed create proof.
    """

    def _configure_lazy_transport(
        self,
        supplier: Callable[[], GcpComputeTransport],
        *,
        failure_code: str,
    ) -> None:
        if not callable(supplier):
            raise GcpTransportError(failure_code)
        self._lazy_transport_supplier = supplier
        self._lazy_transport_failure_code = failure_code
        self._lazy_transport: GcpComputeTransport | None = None
        self._lazy_transport_lock = threading.Lock()

    def _transport_after_authority(self) -> GcpComputeTransport:
        """Return the one raw transport only after the caller's checks."""

        with self._lazy_transport_lock:
            if self._lazy_transport is None:
                try:
                    candidate = self._lazy_transport_supplier()
                except BaseException:
                    raise GcpTransportError(
                        self._lazy_transport_failure_code
                    ) from None
                if candidate is None:
                    raise GcpTransportError(self._lazy_transport_failure_code)
                self._lazy_transport = candidate
            return self._lazy_transport


def _strict_startup_projection_for_authority(
    projection: object,
    *,
    request_digest: str,
    startup_projection_digest: str,
    execution_payload_digest: str,
    code: str,
) -> GcpV2StartupProjection:
    """Validate the opaque binding before a future transport supplier exists.

    This deliberately proves only content-addressed facts.  The frozen request
    and environment are rechecked at each request-bearing method below; no
    payload bytes, startup behavior, credentials, or transfer semantics cross
    this provider boundary.
    """

    try:
        if not isinstance(projection, GcpExecutionModel):
            raise TypeError("startup projection is not a GCP execution model")
        raw = canonical_json_bytes(projection.model_dump(mode="json"))
        parsed = GcpV2StartupProjection.model_validate_json(raw)
    except (AttributeError, ValidationError, TypeError, ValueError):
        raise GcpTransportError(code) from None
    if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
        raise GcpTransportError(code)
    if (
        parsed.request_digest != request_digest
        or gcp_v2_startup_projection_digest(parsed) != startup_projection_digest
        or parsed.execution_payload_digest != execution_payload_digest
    ):
        raise GcpTransportError(code)
    return parsed


def _get_exact_owned_boot_disk_v2(
    transport: GcpComputeTransport,
    request: GcpInsertRequest,
    *,
    timeout_seconds: int,
) -> GcpV2ExactOwnedBootDisk:
    """Read only a typed, exact v2 boot-disk observation."""

    try:
        method = transport.get_exact_owned_boot_disk_v2
    except AttributeError:
        raise GcpTransportError("EXACT_BOOT_DISK_OBSERVATION_UNAVAILABLE") from None
    if not callable(method):
        raise GcpTransportError("EXACT_BOOT_DISK_OBSERVATION_UNAVAILABLE")
    observed = method(request, timeout_seconds=timeout_seconds)
    if not isinstance(observed, GcpV2ExactOwnedBootDisk):
        raise GcpTransportError("EXACT_BOOT_DISK_OBSERVATION_UNAVAILABLE")
    return observed


def _get_exact_owned_instance_v2(
    transport: GcpComputeTransport,
    request: GcpInsertRequest,
    *,
    timeout_seconds: int,
) -> GcpV2ExactOwnedInstance:
    """Read only a typed, exact v2 instance observation."""

    try:
        method = transport.get_exact_owned_instance_v2
    except AttributeError:
        raise GcpTransportError("EXACT_OWNED_INSTANCE_UNAVAILABLE") from None
    if not callable(method):
        raise GcpTransportError("EXACT_OWNED_INSTANCE_UNAVAILABLE")
    observed = method(request, timeout_seconds=timeout_seconds)
    if not isinstance(observed, GcpV2ExactOwnedInstance):
        raise GcpTransportError("EXACT_OWNED_INSTANCE_UNAVAILABLE")
    return observed


def _get_exact_owned_boot_disk_inventory_v2(
    transport: GcpComputeTransport,
    request: GcpInsertRequest,
    *,
    timeout_seconds: int,
) -> GcpV2OwnedBootDiskInventory:
    """Read only a typed, complete v2 boot-disk inventory."""

    try:
        method = transport.get_exact_owned_boot_disk_inventory_v2
    except AttributeError:
        raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_UNAVAILABLE") from None
    if not callable(method):
        raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_UNAVAILABLE")
    observed = method(request, timeout_seconds=timeout_seconds)
    if not isinstance(observed, GcpV2OwnedBootDiskInventory):
        raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_UNAVAILABLE")
    return observed


def _assert_startup_projection_matches_request(
    projection: GcpV2StartupProjection,
    request: GcpInsertRequest,
    *,
    execution_payload_digest: str,
    code: str,
) -> None:
    """Fail closed before a raw request can reach an injected transport."""

    try:
        validate_gcp_v2_startup_projection(
            projection,
            request=request,
            execution_payload_digest=execution_payload_digest,
        )
    except GcpExecutionError:
        raise GcpTransportError(code) from None


class GcpOptionalDependencyUnavailable(GcpExecutionError):
    """The separately installed ``gcp`` extra is not present."""

    def __init__(self) -> None:
        super().__init__("GCP optional dependency unavailable")


class _InstancesClient(Protocol):
    def insert(
        self,
        *,
        request: object,
        timeout: int,
    ) -> object: ...

    def get(
        self, *, project: str, zone: str, instance: str, timeout: int
    ) -> object: ...

    def list(self, *, request: object, timeout: int) -> Sequence[Any]: ...

    def delete(
        self,
        *,
        request: object,
        timeout: int,
    ) -> object: ...


class _ZoneOperationsClient(Protocol):
    def get(
        self, *, project: str, zone: str, operation: str, timeout: int
    ) -> object: ...


class _ImagesClient(Protocol):
    def get(self, *, project: str, image: str, timeout: int) -> object: ...


class _DisksClient(Protocol):
    def get(self, *, project: str, zone: str, disk: str, timeout: int) -> object: ...

    def delete(self, *, request: object, timeout: int) -> object: ...


def _sanitize_exception(error: BaseException, fallback: str) -> GcpTransportError:
    del error
    return GcpTransportError(fallback)


def _safe_labels(value: object) -> GcpExecutionLabels | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return GcpExecutionLabels.model_validate(
            {
                "inferdrome": value.get("inferdrome"),
                "controller_id": value.get("controller_id"),
                "plan_id": value.get("plan_id"),
                "arm_id": value.get("arm_id"),
                "managed_by": value.get("managed_by"),
                "role": value.get("role"),
            }
        )
    except Exception:
        return None


def _canonical_resource_ref(
    value: object, *, kind: str, project: str, region: str, zone: str | None = None
) -> str:
    """Canonicalize documented Compute full, partial, and relative references."""

    malformed_code = (
        "INSTANCE_NETWORK_MALFORMED"
        if kind == "network"
        else "INSTANCE_SUBNETWORK_MALFORMED"
        if kind == "subnetwork"
        else "INSTANCE_RESOURCE_MALFORMED"
    )
    parts = _compute_resource_parts(value, code=malformed_code)
    if kind == "network":
        if (
            len(parts) == 5
            and parts[:2] == ["projects", project]
            and parts[2:4] == ["global", "networks"]
            and _RESOURCE_NAME_RE.fullmatch(parts[4])
        ):
            return "/".join(parts)
        if (
            len(parts) == 3
            and parts[0:2] == ["global", "networks"]
            and _RESOURCE_NAME_RE.fullmatch(parts[2])
        ):
            return f"projects/{project}/global/networks/{parts[2]}"
        if len(parts) == 1 and _RESOURCE_NAME_RE.fullmatch(parts[0]):
            return f"projects/{project}/global/networks/{parts[0]}"
        raise GcpTransportError("INSTANCE_NETWORK_MALFORMED")
    if kind == "subnetwork":
        if zone is None:
            raise GcpTransportError("INSTANCE_SUBNETWORK_MALFORMED")
        if (
            len(parts) == 6
            and parts[:2] == ["projects", project]
            and parts[2:4] == ["regions", region]
            and parts[4] == "subnetworks"
            and _RESOURCE_NAME_RE.fullmatch(parts[5])
        ):
            return "/".join(parts)
        if (
            len(parts) == 4
            and parts[:3] == ["regions", region, "subnetworks"]
            and _RESOURCE_NAME_RE.fullmatch(parts[3])
        ):
            return f"projects/{project}/regions/{region}/subnetworks/{parts[3]}"
        if (
            len(parts) == 2
            and parts[0] == "subnetworks"
            and _RESOURCE_NAME_RE.fullmatch(parts[1])
        ):
            return f"projects/{project}/regions/{region}/subnetworks/{parts[1]}"
        if len(parts) == 1 and _RESOURCE_NAME_RE.fullmatch(parts[0]):
            return f"projects/{project}/regions/{region}/subnetworks/{parts[0]}"
        raise GcpTransportError("INSTANCE_SUBNETWORK_MALFORMED")
    raise GcpTransportError("INSTANCE_RESOURCE_MALFORMED")


def _compute_resource_parts(
    value: object, *, code: str, require_full: bool = False
) -> list[str]:
    """Parse only relative Compute refs or exact Google Compute URLs.

    In particular, do not search for a ``projects`` segment: doing so would
    accept arbitrary hosts, insecure URLs, and path prefixes while appearing
    to normalize an official resource.
    """

    text = value if isinstance(value, str) else ""
    if not text or len(text) > 512 or any(char.isspace() for char in text):
        raise GcpTransportError(code)
    if "://" in text:
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError:
            raise GcpTransportError(code) from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _SUPPORTED_COMPUTE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/compute/v1/")
            or parsed.path.startswith("/compute/v1//")
            or parsed.path.endswith("/")
        ):
            raise GcpTransportError(code)
        raw = parsed.path[len("/compute/v1/") :]
    else:
        if require_full or text.startswith("/") or text.startswith("//"):
            raise GcpTransportError(code)
        raw = text
    parts = raw.split("/")
    if (
        not parts
        or any(
            not part
            or part in {".", ".."}
            or any(char in part for char in "?#%")
            for part in parts
        )
        or parts.count("projects") > 1
    ):
        raise GcpTransportError(code)
    return parts


def _canonical_expected_ref(
    value: object,
    *,
    kind: Literal["instance", "zone", "machine", "accelerator"],
    project: str,
    zone: str,
    instance_name: str | None = None,
    require_full: bool = False,
) -> str:
    parts = _compute_resource_parts(
        value, code="INSTANCE_RESOURCE_MALFORMED", require_full=require_full
    )
    expected: list[str]
    if kind == "instance":
        expected = [
            "projects",
            project,
            "zones",
            zone,
            "instances",
            instance_name or "",
        ]
    elif kind == "zone":
        expected = ["projects", project, "zones", zone]
    elif kind == "machine":
        expected = [
            "projects",
            project,
            "zones",
            zone,
            "machineTypes",
            instance_name or "",
        ]
    else:
        expected = [
            "projects",
            project,
            "zones",
            zone,
            "acceleratorTypes",
            instance_name or "",
        ]
    partial = expected[2:]
    if parts not in (expected, partial):
        raise GcpTransportError("INSTANCE_RESOURCE_MISMATCH")
    return "/".join(expected)


def _observation(value: Any, request: GcpInsertRequest) -> GcpInstanceObservation:
    try:
        name = str(value.name)
        if name != request.instance_name:
            raise GcpTransportError("INSTANCE_IDENTITY_MISMATCH")
        self_link = str(getattr(value, "self_link", ""))
        self_link_parts = _compute_resource_parts(
            self_link, code="INSTANCE_PROJECT_MISMATCH", require_full=True
        )
        expected_instance = [
            "projects",
            request.project_id,
            "zones",
            request.zone,
            "instances",
            request.instance_name,
        ]
        if self_link_parts != expected_instance:
            raise GcpTransportError("INSTANCE_PROJECT_MISMATCH")
        status = cast(Literal["RUNNING", "TERMINATED"], str(value.status))
        labels = _safe_labels(getattr(value, "labels", None))
        if labels != request.labels:
            raise GcpTransportError("INSTANCE_OWNERSHIP_LABEL_MISMATCH")
        if status not in {"RUNNING", "TERMINATED"}:
            raise GcpTransportError("INSTANCE_STATE_UNSUPPORTED")
        zone_ref = getattr(value, "zone", "")
        try:
            _canonical_expected_ref(
                zone_ref,
                kind="zone",
                project=request.project_id,
                zone=request.zone,
            )
        except GcpTransportError as error:
            raise GcpTransportError(
                "INSTANCE_ZONE_MISMATCH"
                if str(error) == "INSTANCE_RESOURCE_MISMATCH"
                else "INSTANCE_ZONE_MALFORMED"
            ) from None
        zone = request.zone
        machine_ref = str(getattr(value, "machine_type", ""))
        try:
            _canonical_expected_ref(
                machine_ref,
                kind="machine",
                project=request.project_id,
                zone=request.zone,
                instance_name=request.machine_type,
            )
        except GcpTransportError as error:
            raise GcpTransportError(
                "INSTANCE_MACHINE_TYPE_MISMATCH"
                if str(error) == "INSTANCE_RESOURCE_MISMATCH"
                else "INSTANCE_MACHINE_TYPE_MALFORMED"
            ) from None
        machine_type = request.machine_type
        accelerators = getattr(value, "guest_accelerators", None)
        try:
            accelerator_values = list(accelerators or ())
        except TypeError:
            accelerator_values = []
        validate_gcp_a2_profile(
            request.machine_type,
            request.accelerator_model,
            request.accelerator_provider_type,
            request.accelerator_count,
        )
        if len(accelerator_values) == 0 and (
            request.accelerator_attachment_mode == "a2_fixed_gpu"
        ):
            accelerator_type = request.accelerator_provider_type
            accelerator_count = request.accelerator_count
        elif len(accelerator_values) == 1:
            accelerator = accelerator_values[0]
            accelerator_ref = str(getattr(accelerator, "accelerator_type", ""))
            try:
                _canonical_expected_ref(
                    accelerator_ref,
                    kind="accelerator",
                    project=request.project_id,
                    zone=request.zone,
                    instance_name=request.accelerator_provider_type,
                )
            except GcpTransportError:
                raise GcpTransportError("INSTANCE_ACCELERATOR_MALFORMED") from None
            accelerator_type = request.accelerator_provider_type
            accelerator_count = int(getattr(accelerator, "accelerator_count", 0))
        else:
            raise GcpTransportError("INSTANCE_ACCELERATOR_MISMATCH")
        if (
            accelerator_type != request.accelerator_provider_type
            or accelerator_count != request.accelerator_count
        ):
            raise GcpTransportError("INSTANCE_ACCELERATOR_MISMATCH")
        if bool(getattr(value, "can_ip_forward", True)):
            raise GcpTransportError("INSTANCE_IP_FORWARDING_ENABLED")
        interfaces = getattr(value, "network_interfaces", None)
        try:
            interface_values = list(interfaces or ())
        except TypeError:
            interface_values = []
        if len(interface_values) != 1:
            raise GcpTransportError("INSTANCE_NETWORK_INVALID")
        private_ips: list[str] = []
        for interface in interface_values:
            network = _canonical_resource_ref(
                getattr(interface, "network", ""),
                kind="network",
                project=request.project_id,
                region=request.region,
            )
            subnetwork = _canonical_resource_ref(
                getattr(interface, "subnetwork", ""),
                kind="subnetwork",
                project=request.project_id,
                region=request.region,
                zone=request.zone,
            )
            if (
                network != request.network.network
                or subnetwork != request.network.subnetwork
            ):
                raise GcpTransportError("INSTANCE_NETWORK_MISMATCH")
            if str(getattr(interface, "stack_type", "")) not in {"IPV4_ONLY", "2"}:
                raise GcpTransportError("INSTANCE_STACK_TYPE_MISMATCH")
            access_configs = getattr(interface, "access_configs", None)
            ipv6_access_configs = getattr(interface, "ipv6_access_configs", None)
            if (
                access_configs
                or ipv6_access_configs
                or getattr(interface, "external_ipv6", None)
            ):
                raise GcpTransportError("INSTANCE_EXTERNAL_ACCESS_PRESENT")
            if getattr(interface, "ipv6_address", None) or getattr(
                interface, "ipv6_access_type", None
            ):
                raise GcpTransportError("INSTANCE_EXTERNAL_ACCESS_PRESENT")
            private_ip = getattr(interface, "network_i_p", None)
            if private_ip is None:
                private_ip = getattr(interface, "network_ip", None)
            if private_ip:
                try:
                    parsed_ip = ipaddress.ip_address(str(private_ip))
                except ValueError:
                    raise GcpTransportError("INSTANCE_PRIVATE_IP_INVALID") from None
                private_networks = (
                    ipaddress.ip_network("10.0.0.0/8"),
                    ipaddress.ip_network("172.16.0.0/12"),
                    ipaddress.ip_network("192.168.0.0/16"),
                )
                if not isinstance(parsed_ip, ipaddress.IPv4Address) or not any(
                    parsed_ip in network for network in private_networks
                ):
                    raise GcpTransportError("INSTANCE_PRIVATE_IP_INVALID")
                private_ips.append(str(parsed_ip))
            else:
                raise GcpTransportError("INSTANCE_PRIVATE_IP_INVALID")
        return GcpInstanceObservation(
            instance_name=name,
            project_id=request.project_id,
            zone=zone,
            machine_type=machine_type,
            accelerator_model=request.accelerator_model,
            accelerator_provider_type=accelerator_type,
            accelerator_count=accelerator_count,
            state=status,
            labels=labels,
            external_access_config="absent",
            ip_forwarding=False,
            network=request.network.network,
            subnetwork=request.network.subnetwork,
            private_ipv4_addresses=tuple(private_ips),
            stack_type="IPV4_ONLY",
        )
    except GcpTransportError:
        raise
    except Exception:
        raise GcpTransportError("INSTANCE_RESPONSE_INVALID") from None


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


def _duration_seconds(value: object) -> int:
    """Read an exact protobuf Duration with no fractional or implicit value."""

    if value is None:
        raise GcpTransportError("INSTANCE_TTL_MISSING")
    seconds = getattr(value, "seconds", None)
    nanos = getattr(value, "nanos", 0)
    if (
        seconds is None
        or nanos is None
        or isinstance(seconds, bool)
        or isinstance(nanos, bool)
    ):
        raise GcpTransportError("INSTANCE_TTL_INVALID")
    try:
        parsed_seconds = int(seconds)
        parsed_nanos = int(nanos)
    except (TypeError, ValueError):
        raise GcpTransportError("INSTANCE_TTL_INVALID") from None
    if (
        parsed_seconds < 1
        or parsed_seconds > 86_400
        or parsed_nanos != 0
        or str(seconds) != str(parsed_seconds)
        or str(nanos) != str(parsed_nanos)
    ):
        raise GcpTransportError("INSTANCE_TTL_INVALID")
    return parsed_seconds


def _instance_safety_observation(
    value: Any, request: GcpInsertRequest
) -> GcpInstanceSafetyObservation:
    """Project only exact provider-observed teardown protections."""

    try:
        if str(getattr(value, "name", "")) != request.instance_name:
            raise GcpTransportError("INSTANCE_SAFETY_IDENTITY_MISMATCH")
        labels = _safe_labels(getattr(value, "labels", None))
        if labels != request.labels:
            raise GcpTransportError("INSTANCE_SAFETY_OWNERSHIP_MISMATCH")
        disks = list(getattr(value, "disks", None) or ())
        boot_disks = [disk for disk in disks if getattr(disk, "boot", None) is True]
        if (
            len(boot_disks) != 1
            or getattr(boot_disks[0], "auto_delete", None) is not True
        ):
            raise GcpTransportError("INSTANCE_BOOT_DISK_AUTODELETE_MISMATCH")
        if getattr(value, "deletion_protection", None) is not False:
            raise GcpTransportError("INSTANCE_DELETION_PROTECTION_MISMATCH")
        scheduling = getattr(value, "scheduling", None)
        if scheduling is None:
            raise GcpTransportError("INSTANCE_SCHEDULING_MISSING")
        if getattr(scheduling, "automatic_restart", None) is not False:
            raise GcpTransportError("INSTANCE_RESTART_POLICY_MISMATCH")
        if str(getattr(scheduling, "on_host_maintenance", "")) != "TERMINATE":
            raise GcpTransportError("INSTANCE_MAINTENANCE_POLICY_MISMATCH")
        if (
            str(getattr(scheduling, "instance_termination_action", ""))
            != "DELETE"
        ):
            raise GcpTransportError("INSTANCE_TERMINATION_ACTION_MISMATCH")
        duration = _duration_seconds(getattr(scheduling, "max_run_duration", None))
        if duration != request.provider_max_runtime_seconds:
            raise GcpTransportError("INSTANCE_TTL_MISMATCH")
        return GcpInstanceSafetyObservation(
            schema_version=GCP_INSTANCE_SAFETY_OBSERVATION_SCHEMA_VERSION,
            request_digest=gcp_execution_request_digest(request),
            project_id=request.project_id,
            zone=request.zone,
            instance_name=request.instance_name,
            labels=request.labels,
            boot_disk_auto_delete=True,
            deletion_protection=False,
            automatic_restart=False,
            maintenance_policy="TERMINATE",
            max_run_duration_seconds=duration,
            instance_termination_action="DELETE",
        )
    except GcpTransportError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise GcpTransportError("INSTANCE_SAFETY_RESPONSE_INVALID") from None


def _is_not_found(error: BaseException) -> bool:
    status = getattr(error, "code", None)
    if callable(status):
        try:
            status = status()
        except Exception:
            status = None
    return (
        status == 404
        or status == "404"
        or type(error).__name__
        in {
            "NotFound",
            "ResourceNotFoundError",
        }
    )


def _provider_error_present(value: object) -> bool:
    """Return true only for a non-empty provider Operation.error message."""

    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(_provider_error_present(child) for child in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_provider_error_present(child) for child in value)
    for field in ("errors", "code", "message", "error_code", "error_message"):
        child = getattr(value, field, None)
        if child not in (None, "", (), [], {}):
            return _provider_error_present(child) if field == "errors" else True
    return bool(str(value).strip())


class GoogleComputeTransport(GcpComputeTransport):
    """Thin projection over an already constructed official SDK client."""

    def __init__(
        self,
        *,
        sdk: Any,
        client: _InstancesClient,
        operations_client: _ZoneOperationsClient | None = None,
        images_client: _ImagesClient | None = None,
        disks_client: _DisksClient | None = None,
    ) -> None:
        self._sdk = sdk
        self._client = client
        self._operations_client = operations_client
        self._images_client = images_client
        self._disks_client = disks_client
        self._lock = threading.Lock()
        self._operations: dict[str, object] = {}
        self._sequence = 0

    def _remember(
        self, operation: object, kind: str, request: GcpInsertRequest
    ) -> GcpOperationHandle:
        raw_name = getattr(operation, "name", None)
        if not isinstance(raw_name, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,127}", raw_name
        ):
            raise GcpTransportError("OPERATION_IDENTITY_MISSING", ambiguous=True)
        name = "/".join(
            (
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "operations",
                raw_name,
            )
        )
        # Keep the local operation key bounded and distinct from credential
        # shaped values; the full canonical resource remains in the journal.
        operation_id = "op-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        with self._lock:
            self._operations[operation_id] = operation
        return GcpOperationHandle(
            operation_id=operation_id,
            operation_kind=cast(Literal["insert", "delete"], kind),
            instance_name=request.instance_name,
            operation_name=name,
            project_id=request.project_id,
            zone=request.zone,
        )

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        """Refuse provider mutation while v0.2 execution remains disabled.

        ``project_create_request`` deliberately remains available to local
        plan/check callers.  Keeping the SDK call behind this unconditional
        denial prevents an imported transport from becoming an accidental
        launch authority before a separately reviewed activation path exists.
        """

        self.project_create_request(request, request_id=request_id)
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def project_create_request(
        self, request: GcpInsertRequest, *, request_id: str
    ) -> object:
        """Build the exact create request without invoking a provider client."""

        try:
            if validate_gcp_a2_profile(
                request.machine_type,
                request.accelerator_model,
                request.accelerator_provider_type,
                request.accelerator_count,
            ) == "synthetic":
                raise GcpTransportError("SYNTHETIC_PROFILE_NOT_LIVE")
            if (
                request_id != request.insert_request_id
                or uuid.UUID(request_id).int == 0
            ):
                raise GcpTransportError("REQUEST_ID_MISMATCH")
            instance = self._project_instance(request)
            insert_request = self._sdk.InsertInstanceRequest(
                project=request.project_id,
                zone=request.zone,
                instance_resource=instance,
                request_id=request_id,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("INSERT_REQUEST_INVALID") from None
        return insert_request

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        with self._lock:
            provider_operation = self._operations.get(operation.operation_id)
        if provider_operation is None and self._operations_client is not None:
            if (
                operation.operation_name is None
                or operation.project_id is None
                or operation.zone is None
            ):
                raise GcpTransportError(
                    "OPERATION_UNKNOWN", operation=operation, ambiguous=True
                )
            try:
                operation_name = operation.operation_name.rsplit("/", 1)[-1]
                provider_operation = self._operations_client.get(
                    project=operation.project_id,
                    zone=operation.zone,
                    operation=operation_name,
                    timeout=timeout_seconds,
                )
            except Exception:
                raise GcpTransportError(
                    "OPERATION_RECONCILIATION_FAILED",
                    operation=operation,
                    ambiguous=True,
                ) from None
        if provider_operation is None:
            raise GcpTransportError(
                "OPERATION_UNKNOWN", operation=operation, ambiguous=True
            )

        raw_name = getattr(provider_operation, "name", None)
        expected_raw_name = (
            operation.operation_name.rsplit("/", 1)[-1]
            if operation.operation_name is not None
            else None
        )
        if not isinstance(raw_name, str) or raw_name != expected_raw_name:
            raise GcpTransportError(
                "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
            )
        for field, expected in (
            ("project", operation.project_id),
            ("project_id", operation.project_id),
            ("zone", operation.zone),
            ("zone_id", operation.zone),
        ):
            observed = getattr(provider_operation, field, None)
            if observed in (None, ""):
                continue
            normalized_observed = str(observed)
            accepted = normalized_observed == expected
            if not accepted:
                try:
                    parts = _compute_resource_parts(
                        normalized_observed, code="OPERATION_RESPONSE_MISMATCH"
                    )
                except GcpTransportError:
                    parts = []
                if field in {"project", "project_id"}:
                    accepted = parts == ["projects", str(expected)]
                else:
                    accepted = parts in (
                        ["zones", str(expected)],
                        ["projects", str(operation.project_id), "zones", str(expected)],
                    )
            if not accepted:
                raise GcpTransportError(
                    "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
                )
        operation_type = getattr(provider_operation, "operation_type", None)
        if (
            not isinstance(operation_type, str)
            or operation_type != operation.operation_kind
        ):
            raise GcpTransportError(
                "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
            )
        target_link = getattr(provider_operation, "target_link", None)
        if not operation.instance_name or not target_link:
            raise GcpTransportError(
                "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
            )
        try:
            target_parts = _compute_resource_parts(
                target_link,
                code="OPERATION_RESPONSE_MISMATCH",
                require_full=True,
            )
        except GcpTransportError:
            raise GcpTransportError(
                "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
            ) from None
        if target_parts != [
            "projects",
            operation.project_id,
            "zones",
            operation.zone,
            "instances",
            operation.instance_name,
        ]:
            raise GcpTransportError(
                "OPERATION_RESPONSE_MISMATCH", operation=operation, ambiguous=True
            )

        def result_for(
            status: Literal["DONE", "ERROR", "TIMEOUT"], error_code: str | None = None
        ) -> GcpOperationResult:
            return GcpOperationResult(
                status=status,
                operation_id=operation.operation_id,
                operation_name=operation.operation_name,
                instance_name=operation.instance_name,
                error_code=error_code,
            )

        def status_name(status: object) -> Literal["PENDING", "RUNNING", "DONE"] | None:
            enum = getattr(getattr(self._sdk, "Operation", None), "Status", None)
            for name in ("PENDING", "RUNNING", "DONE"):
                expected = getattr(enum, name, None)
                if expected is not None and (
                    status in (name, name.lower(), expected)
                    or getattr(status, "value", object())
                    == getattr(expected, "value", None)
                ):
                    return name
            return None

        polling_error = False
        try:
            result_method = getattr(provider_operation, "result", None)
            if callable(result_method):
                result_method(timeout=timeout_seconds)
        except TimeoutError:
            polling_error = True
        except Exception:
            # A local result/polling exception never proves a provider
            # terminal error.  The exact operation handle remains durable for
            # later ZoneOperations reconciliation.
            polling_error = True
        provider_status = getattr(provider_operation, "status", None)
        observed_status = status_name(provider_status)
        if observed_status is None:
            raise GcpTransportError(
                "OPERATION_RESPONSE_INVALID", operation=operation, ambiguous=True
            )
        if observed_status != "DONE":
            return result_for("TIMEOUT")
        provider_error = getattr(provider_operation, "error", None)
        if _provider_error_present(provider_error):
            return result_for("ERROR", "PROVIDER_OPERATION_FAILED")
        if polling_error:
            return result_for("TIMEOUT")
        return result_for("DONE")

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        try:
            value = self._client.get(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                timeout=timeout_seconds,
            )
            return _observation(value, request)
        except GcpTransportError:
            raise
        except Exception as error:
            if _is_not_found(error):
                return _not_found(request)
            raise _sanitize_exception(error, "GET_INSTANCE_FAILED") from None

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        """Compatibility view of the exhaustive, label-scoped inventory."""

        return self.list_owned_complete(
            request, timeout_seconds=timeout_seconds
        ).instances

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        """Return a bounded inventory only when pagination proves completion."""

        try:
            list_request = self._sdk.ListInstancesRequest(
                project=request.project_id,
                zone=request.zone,
                filter=(
                    "labels.inferdrome = inferdrome AND "
                    f"labels.controller_id = {request.labels.controller_id} AND "
                    f"labels.plan_id = {request.labels.plan_id} AND "
                    f"labels.arm_id = {request.labels.arm_id} AND "
                    "labels.managed_by = inferdrome_gcp_execution_v1 AND "
                    "labels.role = provider-envelope"
                ),
                max_results=256,
            )
            values = self._client.list(request=list_request, timeout=timeout_seconds)
            output: list[GcpInstanceObservation] = []
            for index, value in enumerate(values):
                if index >= 256:
                    raise GcpTransportError("LIST_OWNED_INCOMPLETE")
                output.append(_observation(value, request))
            next_page_token = getattr(values, "next_page_token", None)
            if next_page_token not in (None, ""):
                raise GcpTransportError("LIST_OWNED_INCOMPLETE")
            return GcpOwnedResourceInventory(
                schema_version=GCP_OWNED_RESOURCE_INVENTORY_SCHEMA_VERSION,
                request_digest=gcp_execution_request_digest(request),
                project_id=request.project_id,
                region=request.region,
                zone=request.zone,
                labels=request.labels,
                instances=tuple(output),
                pagination_complete=True,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("LIST_OWNED_INCOMPLETE") from None

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        if self._images_client is None:
            raise GcpTransportError("IMAGE_OBSERVATION_UNAVAILABLE")
        try:
            image = self._images_client.get(
                project=request.project_id,
                image=request.boot_image.image_name.rsplit("/", 1)[-1],
                timeout=timeout_seconds,
            )
            image_value = cast(Any, image)
            value = GcpImageObservation(
                image_name=request.boot_image.image_name,
                project_id=request.project_id,
                provider_image_id=int(image_value.id),
                status=cast(Literal["READY"], str(image_value.status)),
                self_link=str(image_value.self_link),
            )
            if value.provider_image_id != request.boot_image.provider_image_id:
                raise GcpTransportError("BOOT_IMAGE_ID_MISMATCH")
            return value
        except GcpTransportError:
            raise
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("BOOT_IMAGE_NOT_FOUND") from None
            raise _sanitize_exception(error, "BOOT_IMAGE_OBSERVATION_FAILED") from None

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        if self._disks_client is None:
            raise GcpTransportError("BOOT_DISK_OBSERVATION_UNAVAILABLE")
        try:
            disk = self._disks_client.get(
                project=request.project_id,
                zone=request.zone,
                disk=request.instance_name,
                timeout=timeout_seconds,
            )
            disk_value = cast(Any, disk)
            disk_self_link = getattr(disk_value, "self_link", None)
            if disk_self_link not in (None, ""):
                disk_parts = _compute_resource_parts(
                    disk_self_link,
                    code="BOOT_DISK_IDENTITY_MISMATCH",
                    require_full=True,
                )
                if disk_parts != [
                    "projects",
                    request.project_id,
                    "zones",
                    request.zone,
                    "disks",
                    request.instance_name,
                ]:
                    raise GcpTransportError("BOOT_DISK_IDENTITY_MISMATCH")
            return GcpBootDiskObservation(
                instance_name=request.instance_name,
                disk_name=str(disk_value.name),
                source_image_id=int(disk_value.source_image_id),
            )
        except GcpTransportError:
            raise
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("BOOT_DISK_NOT_FOUND") from None
            raise _sanitize_exception(error, "BOOT_DISK_OBSERVATION_FAILED") from None

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        """Keep the full v2 disk-observation implementation disabled.

        The frozen v1 disk read intentionally lacks the attachment/provenance
        facts needed by v2 cleanup.  A future reviewed live path must add that
        read-only projection; this development-cycle transport never guesses
        those facts or makes a hidden provider call.
        """

        del request, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        """Keep the additive provider-ID readback disabled in v0.2."""

        del request, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        """Keep the complete label-scoped disk inventory disabled in v0.2."""

        del request, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        """Read back exact teardown protections after a future create path."""

        try:
            value = self._client.get(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                timeout=timeout_seconds,
            )
            return _instance_safety_observation(value, request)
        except GcpTransportError:
            raise
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("INSTANCE_SAFETY_NOT_FOUND") from None
            raise _sanitize_exception(
                error, "INSTANCE_SAFETY_OBSERVATION_FAILED"
            ) from None

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        """Confirm the exact named boot disk is absent, never infer it."""

        if self._disks_client is None:
            raise GcpTransportError("BOOT_DISK_ABSENCE_OBSERVATION_UNAVAILABLE")
        try:
            self._disks_client.get(
                project=request.project_id,
                zone=request.zone,
                disk=request.instance_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if _is_not_found(error):
                return GcpBootDiskAbsenceObservation(
                    schema_version=GCP_BOOT_DISK_ABSENCE_SCHEMA_VERSION,
                    project_id=request.project_id,
                    zone=request.zone,
                    instance_name=request.instance_name,
                    disk_name=request.instance_name,
                    state="NOT_FOUND",
                )
            raise _sanitize_exception(
                error, "BOOT_DISK_ABSENCE_OBSERVATION_FAILED"
            ) from None
        raise GcpTransportError("BOOT_DISK_RESIDUAL")

    def project_exact_boot_disk_delete_request(
        self, binding: GcpV2DiskCleanupBinding, *, request_id: str
    ) -> object:
        """Build one exact named-disk delete request without invoking GCP.

        The v2 disk journal independently validates provenance, immutable
        labels, and the stable request UUID before a future enabled transport
        could reach this projection.  This method contains no account-wide
        inventory or hostname-derived target path.
        """

        try:
            if request_id != gcp_v2_disk_delete_request_id(binding):
                raise GcpTransportError("DISK_DELETE_REQUEST_ID_MISMATCH")
            request_factory = self._sdk.DeleteDiskRequest
            return request_factory(
                project=binding.project_id,
                zone=binding.zone,
                disk=binding.disk.disk_name,
                request_id=request_id,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("DISK_DELETE_REQUEST_INVALID") from None

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        """Keep exact v2 disk deletion disabled pending separate activation."""

        del timeout_seconds
        self.project_exact_boot_disk_delete_request(binding, request_id=request_id)
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        del operation, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def read_exact_owned_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        timeout_seconds: int,
    ) -> GcpV2ExactOwnedBootDisk:
        del binding, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def confirm_exact_boot_disk_absent(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskAbsenceObservation:
        del binding, timeout_seconds
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        """Refuse provider mutation while v0.2 execution remains disabled."""

        self.project_terminate_request(request, request_id=request_id)
        raise GcpTransportError("LIVE_MUTATION_DISABLED")

    def project_terminate_request(
        self, request: GcpInsertRequest, *, request_id: str
    ) -> object:
        """Build the exact termination request without invoking a provider client."""

        try:
            if (
                request_id != request.delete_request_id
                or uuid.UUID(request_id).int == 0
            ):
                raise GcpTransportError("REQUEST_ID_MISMATCH")
            delete_request = self._sdk.DeleteInstanceRequest(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                request_id=request_id,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("DELETE_REQUEST_INVALID") from None
        return delete_request

    def _project_instance(self, request: GcpInsertRequest) -> object:
        return self._sdk.Instance(
            name=request.instance_name,
            machine_type=(f"zones/{request.zone}/machineTypes/{request.machine_type}"),
            disks=[
                self._sdk.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    initialize_params=self._sdk.AttachedDiskInitializeParams(
                        source_image=request.boot_image.image_name,
                        disk_size_gb=request.boot_disk_size_gib,
                        disk_type=(
                            f"zones/{request.zone}/diskTypes/{request.boot_disk_type}"
                        ),
                        # A future v2 disk-cleanup boundary requires observed
                        # immutable disk labels; never infer them from the
                        # instance after the fact.
                        labels=request.labels.model_dump(mode="json"),
                    ),
                )
            ],
            guest_accelerators=(
                []
                if request.accelerator_attachment_mode == "a2_fixed_gpu"
                else [
                    self._sdk.AcceleratorConfig(
                        accelerator_type=(
                            f"zones/{request.zone}/acceleratorTypes/{request.accelerator_provider_type}"
                        ),
                        accelerator_count=request.accelerator_count,
                    )
                ]
            ),
            network_interfaces=[
                self._sdk.NetworkInterface(
                    network=request.network.network,
                    subnetwork=request.network.subnetwork,
                    stack_type="IPV4_ONLY",
                )
            ],
            can_ip_forward=False,
            scheduling=self._sdk.Scheduling(
                automatic_restart=False,
                on_host_maintenance=request.maintenance_policy,
                max_run_duration={"seconds": str(request.provider_max_runtime_seconds)},
                instance_termination_action="DELETE",
            ),
            deletion_protection=False,
            service_accounts=[
                self._sdk.ServiceAccount(
                    email=request.service_account,
                    scopes=[
                        _GOOGLE_SCOPE_URLS[scope]
                        for scope in request.service_account_scopes
                    ],
                )
            ],
            labels=request.labels.model_dump(mode="json"),
        )


class GcpV2CapabilityBoundTransport(GcpComputeTransport, _LazyAuthorityTransport):
    """One-create wrapper bound to a consumed v2 activation capability.

    This adapter deliberately exposes only the controller transport protocol.
    Every request-bearing operation is checked against the immutable request
    digest and ownership identity in the capability.  It is not a credential
    or a live-enable switch: its wrapped transport remains subject to the
    immutable development-cycle mutation gate.
    """

    def __init__(
        self,
        *,
        transport_supplier: Callable[[], GcpComputeTransport],
        capability: GcpV2MutationCapability,
        startup_projection: GcpV2StartupProjection,
        now_fn: Callable[[], datetime],
        _seal: object,
    ) -> None:
        if _seal is not _CAPABILITY_BOUND_TRANSPORT_SEAL:
            raise GcpTransportError("MUTATION_CAPABILITY_TRANSPORT_INVALID")
        self._configure_lazy_transport(
            transport_supplier,
            failure_code="MUTATION_CAPABILITY_TRANSPORT_FACTORY_FAILED",
        )
        self.capability = capability
        self.startup_projection = _strict_startup_projection_for_authority(
            startup_projection,
            request_digest=capability.request_digest,
            startup_projection_digest=capability.startup_projection_digest,
            execution_payload_digest=capability.execution_payload_digest,
            code="MUTATION_CAPABILITY_PAYLOAD_MISMATCH",
        )
        self._now_fn = now_fn
        self._seal = _seal
        self._insert_lock = threading.Lock()
        self._insert_consumed = False
        self._insert_succeeded = False

    def _assert_provider_runtime_open(self) -> None:
        try:
            now = self._now_fn()
            now_value = datetime.fromisoformat(
                str(now.astimezone(UTC).isoformat()).replace("Z", "+00:00")
            )
            deadline = datetime.fromisoformat(
                str(self.capability.provider_runtime_deadline_at).replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            raise GcpTransportError("MUTATION_CAPABILITY_TIME_INVALID") from None
        if now_value >= deadline:
            raise GcpTransportError("MUTATION_CAPABILITY_RUNTIME_EXPIRED")

    def _assert_watchdog_cleanup_open(self) -> None:
        """Bound all post-create reads and cleanup to the watchdog horizon."""

        try:
            now = self._now_fn()
            now_value = now.astimezone(UTC)
            deadline = datetime.fromisoformat(
                str(self.capability.watchdog_cleanup_deadline_at).replace(
                    "Z", "+00:00"
                )
            )
        except (AttributeError, TypeError, ValueError):
            raise GcpTransportError("MUTATION_CAPABILITY_TIME_INVALID") from None
        if now_value >= deadline:
            raise GcpTransportError("MUTATION_CAPABILITY_CLEANUP_EXPIRED")

    def _exact_request(self, request: GcpInsertRequest) -> None:
        try:
            request_digest = gcp_execution_request_digest(request)
        except (TypeError, ValueError):
            raise GcpTransportError("MUTATION_CAPABILITY_REQUEST_INVALID") from None
        if (
            request_digest != self.capability.request_digest
            or request.project_id != self.capability.project_id
            or request.region != self.capability.region
            or request.zone != self.capability.zone
            or request.instance_name != self.capability.instance_name
            or request.labels != self.capability.labels
        ):
            raise GcpTransportError("MUTATION_CAPABILITY_REQUEST_MISMATCH")
        _assert_startup_projection_matches_request(
            self.startup_projection,
            request,
            execution_payload_digest=self.capability.execution_payload_digest,
            code="MUTATION_CAPABILITY_PAYLOAD_MISMATCH",
        )

    def _exact_operation(self, operation: GcpOperationHandle) -> None:
        if (
            not operation.operation_id
            or not operation.operation_name
            or operation.instance_name != self.capability.instance_name
            or operation.project_id != self.capability.project_id
            or operation.zone != self.capability.zone
        ):
            raise GcpTransportError("MUTATION_CAPABILITY_OPERATION_MISMATCH")

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._exact_request(request)
        self._assert_provider_runtime_open()
        if request_id != request.insert_request_id:
            raise GcpTransportError("MUTATION_CAPABILITY_REQUEST_ID_MISMATCH")
        with self._insert_lock:
            if self._insert_consumed:
                raise GcpTransportError("MUTATION_CAPABILITY_CREATE_CONSUMED")
            # Consume before the injected boundary.  A lost response must be
            # reconciled using the core journal, never retried as a new create.
            self._insert_consumed = True
        operation = self._transport_after_authority().insert(
            request, timeout_seconds=timeout_seconds, request_id=request_id
        )
        with self._insert_lock:
            self._insert_succeeded = True
        return operation

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        self._assert_watchdog_cleanup_open()
        self._exact_operation(operation)
        return self._transport_after_authority().wait_operation(
            operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().get_instance(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().list_owned(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().list_owned_complete(
            request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().get_image(
            request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().get_boot_disk(
            request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return _get_exact_owned_instance_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_inventory_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().get_instance_safety(
            request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._transport_after_authority().confirm_boot_disk_absent(
            request, timeout_seconds=timeout_seconds
        )

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        with self._insert_lock:
            if not self._insert_succeeded:
                raise GcpTransportError("MUTATION_CAPABILITY_CLEANUP_UNAVAILABLE")
        if request_id != request.delete_request_id:
            raise GcpTransportError("MUTATION_CAPABILITY_REQUEST_ID_MISMATCH")
        return self._transport_after_authority().delete(
            request, timeout_seconds=timeout_seconds, request_id=request_id
        )


def is_gcp_v2_capability_bound_transport(value: object) -> bool:
    """Recognize only adapters minted by the capability factory below."""

    return (
        type(value) is GcpV2CapabilityBoundTransport
        and getattr(value, "_seal", None) is _CAPABILITY_BOUND_TRANSPORT_SEAL
    )


def _consume_gcp_v2_mutation_proof_for_transport(
    capability: object, *, now: datetime
) -> GcpV2MutationCapability:
    """Consume the opaque proof before any transport construction boundary."""

    try:
        from inferdrome.deployment.gcp_supervisor import (
            consume_gcp_v2_activated_mutation_proof,
        )

        proof = consume_gcp_v2_activated_mutation_proof(
            capability, now=now, purpose="transport"
        )
    except GcpExecutionError:
        raise GcpTransportError("MUTATION_CAPABILITY_INVALID") from None
    except BaseException:
        raise GcpTransportError("MUTATION_CAPABILITY_INVALID") from None
    return proof.capability


def bind_gcp_v2_mutation_transport(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    capability: object,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2CapabilityBoundTransport:
    """Consume one sealed proof and bind an injected transport to it exactly.

    This is the local capability-only factory seam used by fake-only tests and
    a future separately enabled transport factory.  It cannot manufacture a
    proof, create an SDK client, discover ADC, or make a provider request.
    The proof is consumed before the wrapped transport is returned, so a
    failed or replayed factory attempt never opens a second create route.
    """

    parsed_capability = _consume_gcp_v2_mutation_proof_for_transport(
        capability, now=now
    )
    projection = _strict_startup_projection_for_authority(
        startup_projection,
        request_digest=parsed_capability.request_digest,
        startup_projection_digest=parsed_capability.startup_projection_digest,
        execution_payload_digest=parsed_capability.execution_payload_digest,
        code="MUTATION_CAPABILITY_PAYLOAD_MISMATCH",
    )
    return GcpV2CapabilityBoundTransport(
        transport_supplier=transport_supplier,
        capability=parsed_capability,
        startup_projection=projection,
        now_fn=now_fn,
        _seal=_CAPABILITY_BOUND_TRANSPORT_SEAL,
    )


class GcpV2WatchdogCleanupBoundTransport(
    GcpComputeTransport, _LazyAuthorityTransport
):
    """Exact no-create transport for the independently durable watchdog.

    It is derived from the same exact activation capability but exposes no
    ``insert`` method.  It can only read/reconcile/delete the one
    request-bound instance through the watchdog cleanup horizon.  A normal
    controller still needs its separate one-create wrapper.
    """

    def __init__(
        self,
        *,
        transport_supplier: Callable[[], GcpComputeTransport],
        capability: GcpV2MutationCapability,
        startup_projection: GcpV2StartupProjection,
        now_fn: Callable[[], datetime],
        _seal: object,
    ) -> None:
        if _seal is not _WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL:
            raise GcpTransportError("WATCHDOG_CLEANUP_TRANSPORT_INVALID")
        self._configure_lazy_transport(
            transport_supplier,
            failure_code="WATCHDOG_CLEANUP_TRANSPORT_FACTORY_FAILED",
        )
        self.capability = capability
        self.startup_projection = _strict_startup_projection_for_authority(
            startup_projection,
            request_digest=capability.request_digest,
            startup_projection_digest=capability.startup_projection_digest,
            execution_payload_digest=capability.execution_payload_digest,
            code="WATCHDOG_CLEANUP_PAYLOAD_MISMATCH",
        )
        self._now_fn = now_fn
        self._seal = _seal

    def _assert_cleanup_window(self) -> None:
        try:
            now_value = self._now_fn().astimezone(UTC)
            activated = datetime.fromisoformat(
                str(self.capability.activated_at).replace("Z", "+00:00")
            )
            deadline = datetime.fromisoformat(
                str(self.capability.watchdog_cleanup_deadline_at).replace(
                    "Z", "+00:00"
                )
            )
        except (AttributeError, TypeError, ValueError):
            raise GcpTransportError("WATCHDOG_CLEANUP_TIME_INVALID") from None
        if not (activated <= now_value < deadline):
            raise GcpTransportError("WATCHDOG_CLEANUP_EXPIRED")

    def _exact_request(self, request: GcpInsertRequest) -> None:
        try:
            request_digest = gcp_execution_request_digest(request)
        except (TypeError, ValueError):
            raise GcpTransportError("WATCHDOG_CLEANUP_REQUEST_INVALID") from None
        if (
            request_digest != self.capability.request_digest
            or request.project_id != self.capability.project_id
            or request.region != self.capability.region
            or request.zone != self.capability.zone
            or request.instance_name != self.capability.instance_name
            or request.labels != self.capability.labels
        ):
            raise GcpTransportError("WATCHDOG_CLEANUP_REQUEST_MISMATCH")
        _assert_startup_projection_matches_request(
            self.startup_projection,
            request,
            execution_payload_digest=self.capability.execution_payload_digest,
            code="WATCHDOG_CLEANUP_PAYLOAD_MISMATCH",
        )

    def _exact_operation(self, operation: GcpOperationHandle) -> None:
        if (
            not operation.operation_id
            or not operation.operation_name
            or operation.instance_name != self.capability.instance_name
            or operation.project_id != self.capability.project_id
            or operation.zone != self.capability.zone
        ):
            raise GcpTransportError("WATCHDOG_CLEANUP_OPERATION_MISMATCH")

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del request, timeout_seconds, request_id
        raise GcpTransportError("WATCHDOG_CLEANUP_CREATE_FORBIDDEN")

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        self._assert_cleanup_window()
        self._exact_operation(operation)
        return self._transport_after_authority().wait_operation(
            operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_instance(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().list_owned(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().list_owned_complete(
            request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_image(
            request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_boot_disk(
            request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_instance_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_inventory_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_instance_safety(
            request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().confirm_boot_disk_absent(
            request, timeout_seconds=timeout_seconds
        )

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_cleanup_window()
        self._exact_request(request)
        if request_id != request.delete_request_id:
            raise GcpTransportError("WATCHDOG_CLEANUP_REQUEST_ID_MISMATCH")
        return self._transport_after_authority().delete(
            request, timeout_seconds=timeout_seconds, request_id=request_id
        )


def bind_gcp_v2_watchdog_cleanup_transport(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    capability: GcpV2MutationCapability,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2WatchdogCleanupBoundTransport:
    """Create a no-create exact watchdog adapter from a validated capability."""

    try:
        raw = canonical_json_bytes(capability.model_dump(mode="json"))
        parsed = GcpV2MutationCapability.model_validate_json(raw)
        now_value = now.astimezone(UTC)
        activated = datetime.fromisoformat(
            str(parsed.activated_at).replace("Z", "+00:00")
        )
        deadline = datetime.fromisoformat(
            str(parsed.watchdog_cleanup_deadline_at).replace("Z", "+00:00")
        )
    except (AttributeError, ValidationError, TypeError, ValueError):
        raise GcpTransportError("WATCHDOG_CLEANUP_CAPABILITY_INVALID") from None
    if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
        raise GcpTransportError("WATCHDOG_CLEANUP_CAPABILITY_INVALID")
    if not (activated <= now_value < deadline):
        raise GcpTransportError("WATCHDOG_CLEANUP_CAPABILITY_EXPIRED")
    projection = _strict_startup_projection_for_authority(
        startup_projection,
        request_digest=parsed.request_digest,
        startup_projection_digest=parsed.startup_projection_digest,
        execution_payload_digest=parsed.execution_payload_digest,
        code="WATCHDOG_CLEANUP_PAYLOAD_MISMATCH",
    )
    return GcpV2WatchdogCleanupBoundTransport(
        transport_supplier=transport_supplier,
        capability=parsed,
        startup_projection=projection,
        now_fn=now_fn,
        _seal=_WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL,
    )


def is_gcp_v2_watchdog_cleanup_bound_transport(value: object) -> bool:
    """Recognize only no-create watchdog adapters minted above."""

    return (
        type(value) is GcpV2WatchdogCleanupBoundTransport
        and getattr(value, "_seal", None) is _WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL
    )


class GcpV2CleanupAuthorizationBoundTransport(
    GcpComputeTransport, _LazyAuthorityTransport
):
    """Exact cleanup-only wrapper which permanently forbids creation."""

    def __init__(
        self,
        *,
        transport_supplier: Callable[[], GcpComputeTransport],
        authorization: GcpCleanupRecoveryAuthorization,
        startup_projection: GcpV2StartupProjection,
        now_fn: Callable[[], datetime],
        _seal: object,
    ) -> None:
        if _seal is not _CLEANUP_BOUND_TRANSPORT_SEAL:
            raise GcpTransportError("CLEANUP_AUTHORIZATION_TRANSPORT_INVALID")
        self._configure_lazy_transport(
            transport_supplier,
            failure_code="CLEANUP_AUTHORIZATION_TRANSPORT_FACTORY_FAILED",
        )
        self.authorization = authorization
        self.startup_projection = _strict_startup_projection_for_authority(
            startup_projection,
            request_digest=authorization.request_digest,
            startup_projection_digest=authorization.startup_projection_digest,
            execution_payload_digest=authorization.execution_payload_digest,
            code="CLEANUP_AUTHORIZATION_PAYLOAD_MISMATCH",
        )
        self._now_fn = now_fn
        self._seal = _seal

    def _assert_cleanup_window(self) -> None:
        try:
            now = self._now_fn()
            now_value = now.astimezone(UTC)
            issued = datetime.fromisoformat(
                str(self.authorization.issued_at).replace("Z", "+00:00")
            )
            expires = datetime.fromisoformat(
                str(self.authorization.expires_at).replace("Z", "+00:00")
            )
            recovery = datetime.fromisoformat(
                str(self.authorization.recovery_deadline_at).replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            raise GcpTransportError("CLEANUP_AUTHORIZATION_TIME_INVALID") from None
        if not (issued <= now_value < expires <= recovery):
            raise GcpTransportError("CLEANUP_AUTHORIZATION_EXPIRED")

    def _exact_request(self, request: GcpInsertRequest) -> None:
        try:
            request_digest = gcp_execution_request_digest(request)
        except (TypeError, ValueError):
            raise GcpTransportError("CLEANUP_AUTHORIZATION_REQUEST_INVALID") from None
        if (
            request_digest != self.authorization.request_digest
            or request.project_id != self.authorization.project_id
            or request.region != self.authorization.region
            or request.zone != self.authorization.zone
            or request.instance_name != self.authorization.instance_name
            or request.labels != self.authorization.labels
        ):
            raise GcpTransportError("CLEANUP_AUTHORIZATION_REQUEST_MISMATCH")
        _assert_startup_projection_matches_request(
            self.startup_projection,
            request,
            execution_payload_digest=self.authorization.execution_payload_digest,
            code="CLEANUP_AUTHORIZATION_PAYLOAD_MISMATCH",
        )

    def _exact_operation(self, operation: GcpOperationHandle) -> None:
        if (
            not operation.operation_id
            or not operation.operation_name
            or operation.instance_name != self.authorization.instance_name
            or operation.project_id != self.authorization.project_id
            or operation.zone != self.authorization.zone
        ):
            raise GcpTransportError("CLEANUP_AUTHORIZATION_OPERATION_MISMATCH")

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del request, timeout_seconds, request_id
        raise GcpTransportError("CLEANUP_CREATE_FORBIDDEN")

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        self._assert_cleanup_window()
        self._exact_operation(operation)
        return self._transport_after_authority().wait_operation(
            operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_instance(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().list_owned(
            request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().list_owned_complete(
            request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_image(
            request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_boot_disk(
            request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_instance_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return _get_exact_owned_boot_disk_inventory_v2(
            self._transport_after_authority(), request, timeout_seconds=timeout_seconds
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().get_instance_safety(
            request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._transport_after_authority().confirm_boot_disk_absent(
            request, timeout_seconds=timeout_seconds
        )

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_cleanup_window()
        self._exact_request(request)
        if request_id != request.delete_request_id:
            raise GcpTransportError("CLEANUP_AUTHORIZATION_REQUEST_ID_MISMATCH")
        return self._transport_after_authority().delete(
            request, timeout_seconds=timeout_seconds, request_id=request_id
        )


def bind_gcp_v2_cleanup_transport(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    authorization: GcpCleanupRecoveryAuthorization,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2CleanupAuthorizationBoundTransport:
    """Bind an already-validated recovery transport to cleanup-only authority."""

    try:
        raw = canonical_json_bytes(authorization.model_dump(mode="json"))
        parsed = GcpCleanupRecoveryAuthorization.model_validate_json(raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpTransportError("CLEANUP_AUTHORIZATION_INVALID") from None
    if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
        raise GcpTransportError("CLEANUP_AUTHORIZATION_INVALID")
    try:
        now_value = now.astimezone(UTC)
        issued = datetime.fromisoformat(str(parsed.issued_at).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(parsed.expires_at).replace("Z", "+00:00"))
        recovery = datetime.fromisoformat(
            str(parsed.recovery_deadline_at).replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        raise GcpTransportError("CLEANUP_AUTHORIZATION_TIME_INVALID") from None
    if not (issued <= now_value < expires <= recovery):
        raise GcpTransportError("CLEANUP_AUTHORIZATION_EXPIRED")
    projection = _strict_startup_projection_for_authority(
        startup_projection,
        request_digest=parsed.request_digest,
        startup_projection_digest=parsed.startup_projection_digest,
        execution_payload_digest=parsed.execution_payload_digest,
        code="CLEANUP_AUTHORIZATION_PAYLOAD_MISMATCH",
    )
    return GcpV2CleanupAuthorizationBoundTransport(
        transport_supplier=transport_supplier,
        authorization=parsed,
        startup_projection=projection,
        now_fn=now_fn,
        _seal=_CLEANUP_BOUND_TRANSPORT_SEAL,
    )


def is_gcp_v2_cleanup_authorization_bound_transport(value: object) -> bool:
    """Recognize only cleanup wrappers minted by this local factory."""

    return (
        type(value) is GcpV2CleanupAuthorizationBoundTransport
        and getattr(value, "_seal", None) is _CLEANUP_BOUND_TRANSPORT_SEAL
    )


class GcpV2LocalTransportFactory:
    """Nominal local-only authority factory for one injected transport.

    The guarded controller accepts this exact class for its real v2 route
    instead of an arbitrary callback.  Its only public methods mint sealed
    request-bound wrappers after consuming a supervisor proof or validating a
    cleanup-only authorization.  It never imports an SDK, discovers ADC, or
    invokes a wrapped provider method while constructing an adapter.
    """

    def __init__(
        self,
        *,
        transport_supplier: Callable[[], GcpComputeTransport],
        now_fn: Callable[[], datetime],
        startup_projection: GcpV2StartupProjection,
    ) -> None:
        if not callable(transport_supplier):
            raise GcpTransportError("LOCAL_TRANSPORT_SUPPLIER_INVALID")
        self._transport_supplier = transport_supplier
        self._transport: GcpComputeTransport | None = None
        self._transport_lock = threading.Lock()
        self._now_fn = now_fn
        # Canonical parsing validates its content-addressed projection ID, but
        # no request/environment is accepted here; each sealed wrapper checks
        # that exact v1 request immediately before its raw transport call.
        try:
            projection_raw = canonical_json_bytes(
                startup_projection.model_dump(mode="json")
            )
            parsed_projection = GcpV2StartupProjection.model_validate_json(
                projection_raw
            )
        except (AttributeError, ValidationError, TypeError, ValueError):
            raise GcpTransportError(
                "LOCAL_TRANSPORT_STARTUP_PROJECTION_INVALID"
            ) from None
        self._startup_projection = _strict_startup_projection_for_authority(
            parsed_projection,
            request_digest=parsed_projection.request_digest,
            startup_projection_digest=gcp_v2_startup_projection_digest(
                parsed_projection
            ),
            execution_payload_digest=parsed_projection.execution_payload_digest,
            code="LOCAL_TRANSPORT_STARTUP_PROJECTION_INVALID",
        )

    def _lazy_transport(self) -> GcpComputeTransport:
        """Construct one injected client only after an authority validates.

        Keeping the supplier inert at construction prevents a future caller
        from instantiating an SDK client or discovering ADC during preflight,
        reservation, or watchdog arming.  The current project supplies only
        local fakes; the same narrow seam is intentionally suitable for a
        separately reviewed enabled path later.
        """

        with self._transport_lock:
            if self._transport is None:
                try:
                    candidate = self._transport_supplier()
                except BaseException:
                    raise GcpTransportError(
                        "LOCAL_TRANSPORT_SUPPLIER_FAILED"
                    ) from None
                if candidate is None:
                    raise GcpTransportError("LOCAL_TRANSPORT_SUPPLIER_FAILED")
                self._transport = candidate
            return self._transport

    def bind_mutation(self, proof: object) -> GcpV2CapabilityBoundTransport:
        now = self._now_fn()
        return bind_gcp_v2_mutation_transport(
            transport_supplier=self._lazy_transport,
            capability=proof,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )

    def bind_watchdog_cleanup(
        self, capability: GcpV2MutationCapability
    ) -> GcpV2WatchdogCleanupBoundTransport:
        """Bind a no-create watchdog adapter after capability activation."""

        now = self._now_fn()
        return bind_gcp_v2_watchdog_cleanup_transport(
            transport_supplier=self._lazy_transport,
            capability=capability,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )

    def bind_cleanup(
        self, authorization: GcpCleanupRecoveryAuthorization
    ) -> GcpV2CleanupAuthorizationBoundTransport:
        now = self._now_fn()
        return bind_gcp_v2_cleanup_transport(
            transport_supplier=self._lazy_transport,
            authorization=authorization,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )


def create_google_compute_transport(
    *,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
    images_client: _ImagesClient | None = None,
    disks_client: _DisksClient | None = None,
) -> GoogleComputeTransport:
    """Remain fail-closed until a future reviewed activation path exists.

    The parameters are intentionally retained for API compatibility.  Fully
    injected test doubles may still be wrapped for local-only contract tests,
    but this v0.2 development cycle does not permit SDK import or ADC client
    construction through this factory.
    """

    if (
        sdk_module is not None
        and client is not None
        and operations_client is not None
        and images_client is not None
        and disks_client is not None
    ):
        return GoogleComputeTransport(
            sdk=sdk_module,
            client=client,
            operations_client=operations_client,
            images_client=images_client,
            disks_client=disks_client,
        )
    if not GCP_LIVE_MUTATIONS_ENABLED:
        raise GcpTransportError("LIVE_TRANSPORT_DISABLED")

    # This branch is deliberately unreachable until a separately reviewed
    # activation implementation changes the immutable default above.
    if sdk_module is None:
        try:
            sdk_module = importlib.import_module("google.cloud.compute_v1")
        except (ImportError, ModuleNotFoundError):
            raise GcpOptionalDependencyUnavailable() from None
    if client is None:
        try:
            client = sdk_module.InstancesClient()
        except Exception:
            raise GcpTransportError("ADC_CLIENT_CONSTRUCTION_FAILED") from None
    if operations_client is None:
        try:
            operations_factory = sdk_module.ZoneOperationsClient
            operations_client = operations_factory()
        except AttributeError:
            raise GcpTransportError("GCP_OPERATION_CLIENT_UNAVAILABLE") from None
        except Exception:
            raise GcpTransportError(
                "ADC_OPERATION_CLIENT_CONSTRUCTION_FAILED"
            ) from None
    if operations_client is None:
        raise GcpTransportError("GCP_OPERATION_CLIENT_UNAVAILABLE")
    if images_client is None:
        try:
            images_client = sdk_module.ImagesClient()
        except AttributeError:
            raise GcpTransportError("GCP_IMAGE_CLIENT_UNAVAILABLE") from None
        except Exception:
            raise GcpTransportError("ADC_IMAGE_CLIENT_CONSTRUCTION_FAILED") from None
    if disks_client is None:
        try:
            disks_client = sdk_module.DisksClient()
        except AttributeError:
            raise GcpTransportError("GCP_DISK_CLIENT_UNAVAILABLE") from None
        except Exception:
            raise GcpTransportError("ADC_DISK_CLIENT_CONSTRUCTION_FAILED") from None
    return GoogleComputeTransport(
        sdk=sdk_module,
        client=client,
        operations_client=operations_client,
        images_client=images_client,
        disks_client=disks_client,
    )


def create_google_compute_transport_for_v2_capability(
    *,
    capability: object,
    startup_projection: object,
    now: datetime,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
    images_client: _ImagesClient | None = None,
    disks_client: _DisksClient | None = None,
) -> GcpV2CapabilityBoundTransport:
    """Controller-proof-only factory hook; default SDK/ADC activation stays disabled.

    ``GcpGuardedLifecycleController`` may call this only after its v2
    supervisor has validated exact approval/provider facts and persisted a
    watchdog activation receipt.  This local function still refuses SDK/ADC
    construction in the development series; fully injected doubles are kept
    solely for contract tests.
    """

    # Import only at this explicit future factory edge.  The supervisor module
    # never imports this transport, so this cannot create an SDK/ADC import
    # cycle.  A bare (even self-hashed) capability is deliberately insufficient:
    # it must be the sealed proof emitted after durable watchdog activation.
    parsed_capability = _consume_gcp_v2_mutation_proof_for_transport(
        capability, now=now
    )
    # Validate the opaque payload binding before the generic factory can import
    # an SDK, inspect ADC, or instantiate any injected client.  The public
    # future-facing seam must provide the same authority chain as the nominal
    # local factory; a startup-script digest alone is deliberately insufficient.
    projection = _strict_startup_projection_for_authority(
        startup_projection,
        request_digest=parsed_capability.request_digest,
        startup_projection_digest=parsed_capability.startup_projection_digest,
        execution_payload_digest=parsed_capability.execution_payload_digest,
        code="MUTATION_CAPABILITY_PAYLOAD_MISMATCH",
    )
    transport = create_google_compute_transport(
        sdk_module=sdk_module,
        client=client,
        operations_client=operations_client,
        images_client=images_client,
        disks_client=disks_client,
    )
    return GcpV2CapabilityBoundTransport(
        transport_supplier=lambda: transport,
        capability=parsed_capability,
        startup_projection=projection,
        now_fn=lambda: now,
        _seal=_CAPABILITY_BOUND_TRANSPORT_SEAL,
    )
