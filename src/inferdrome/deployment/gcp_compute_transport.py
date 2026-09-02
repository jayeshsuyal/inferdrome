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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast, overload
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
    issue_gcp_v2_owned_boot_disk_inventory,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import Sha256Digest

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
_SEALED_GCE_TRANSPORT_SEAL = object()

# These values are opaque, content-addressed local identifiers.  They bind a
# provider request to the reviewed v2 approval without defining how any
# payload executes, moves, authenticates, or produces evidence.  In
# particular, no payload bytes, startup script, command, credential, prompt,
# input, output, or evidence material is ever emitted as metadata.
_GCP_V2_METADATA_PROJECTION_ID: Final = "inferdrome-v2-startup-projection-id"
_GCP_V2_METADATA_REQUEST_DIGEST: Final = "inferdrome-v2-request-digest"
_GCP_V2_METADATA_EXECUTION_PAYLOAD_DIGEST: Final = (
    "inferdrome-v2-execution-payload-digest"
)


class _LazyAuthorityTransport:
    """One inert provider-operation dispatcher shared by sealed v2 adapters.

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
        allowed_operations: frozenset[str],
    ) -> None:
        if not callable(supplier) or not allowed_operations:
            raise GcpTransportError(failure_code)
        self._lazy_transport_supplier = supplier
        self._lazy_transport_failure_code = failure_code
        self._lazy_transport_allowed_operations = allowed_operations
        self._lazy_transport: GcpComputeTransport | None = None
        self._lazy_transport_lock = threading.Lock()

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["insert", "delete"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpOperationHandle: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["wait_operation"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpOperationResult: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_instance"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpInstanceObservation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["list_owned"],
        /,
        *args: object,
        **kwargs: object,
    ) -> tuple[GcpInstanceObservation, ...]: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["list_owned_complete"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpOwnedResourceInventory: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_image"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpImageObservation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_boot_disk"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpBootDiskObservation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal[
            "get_exact_owned_boot_disk_v2", "read_exact_owned_boot_disk"
        ],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2ExactOwnedBootDisk: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_exact_owned_instance_v2"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2ExactOwnedInstance: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_exact_owned_boot_disk_inventory_v2"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2OwnedBootDiskInventory: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["get_instance_safety"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpInstanceSafetyObservation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["confirm_boot_disk_absent"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpBootDiskAbsenceObservation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["delete_exact_boot_disk"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2DiskDeleteOperation: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["reconcile_exact_disk_delete"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2DiskDeleteResult: ...

    @overload
    def _call_transport_after_authority(
        self,
        operation: Literal["confirm_exact_boot_disk_absent"],
        /,
        *args: object,
        **kwargs: object,
    ) -> GcpV2DiskAbsenceObservation: ...

    def _call_transport_after_authority(
        self, operation: str, /, *args: object, **kwargs: object
    ) -> Any:
        """Dispatch one pre-approved operation without exposing raw transport.

        Cleanup adapters must never hand their underlying sealed GCE object to
        a caller: that object has a broader implementation surface than the
        adapter's opaque cleanup authority.  Keeping the supplier and object
        private means a watchdog/recovery wrapper can dispatch only its fixed
        no-create method allow-list.  This is an in-process authority boundary,
        not a credential boundary.
        """

        if operation not in self._lazy_transport_allowed_operations:
            raise GcpTransportError("SEALED_TRANSPORT_OPERATION_FORBIDDEN")

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
            transport = self._lazy_transport
        try:
            method = getattr(transport, operation)
        except AttributeError:
            raise GcpTransportError(self._lazy_transport_failure_code) from None
        if not callable(method):
            raise GcpTransportError(self._lazy_transport_failure_code)
        try:
            return method(*args, **kwargs)
        except GcpTransportError:
            raise


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

    def list(self, *, request: object, timeout: int) -> object: ...

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

    def _project_instance(
        self,
        request: GcpInsertRequest,
        *,
        metadata: object | None = None,
    ) -> object:
        """Project one request with optional sealed v2 identifier metadata.

        The public/default transport always supplies ``None`` here.  The
        private proof-only subclass below supplies only its three opaque
        binding identifiers after revalidating them.  Keeping this low-level
        projection shared makes the network, disk, scheduling, and ownership
        construction identical on both paths while preserving the default
        mutation refusal.
        """

        value: dict[str, object] = {
            "name": request.instance_name,
            "machine_type": (
                f"zones/{request.zone}/machineTypes/{request.machine_type}"
            ),
            "disks": [
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
            "guest_accelerators": (
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
            "network_interfaces": [
                self._sdk.NetworkInterface(
                    network=request.network.network,
                    subnetwork=request.network.subnetwork,
                    stack_type="IPV4_ONLY",
                )
            ],
            "can_ip_forward": False,
            "scheduling": self._sdk.Scheduling(
                automatic_restart=False,
                on_host_maintenance=request.maintenance_policy,
                max_run_duration={"seconds": str(request.provider_max_runtime_seconds)},
                instance_termination_action="DELETE",
            ),
            "deletion_protection": False,
            "service_accounts": [
                self._sdk.ServiceAccount(
                    email=request.service_account,
                    scopes=[
                        _GOOGLE_SCOPE_URLS[scope]
                        for scope in request.service_account_scopes
                    ],
                )
            ],
            "labels": request.labels.model_dump(mode="json"),
        }
        if metadata is not None:
            value["metadata"] = metadata
        return self._sdk.Instance(**value)


def _provider_positive_int(value: object, *, code: str) -> int:
    """Accept one provider integer without coercing booleans or junk."""

    if isinstance(value, bool):
        raise GcpTransportError(code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        parsed = int(value)
    else:
        raise GcpTransportError(code)
    if not 1 <= parsed <= 10**19:
        raise GcpTransportError(code)
    return parsed


def _canonical_provider_self_link(
    value: object,
    *,
    expected: Sequence[str],
    code: str,
) -> str:
    """Require one exact official Compute self-link and return its path."""

    try:
        parts = _compute_resource_parts(value, code=code, require_full=True)
    except GcpTransportError:
        raise GcpTransportError(code) from None
    if parts != list(expected):
        raise GcpTransportError(code)
    return "/".join(parts)


def _exact_ownership_filter(labels: GcpExecutionLabels) -> str:
    """Return the complete immutable ownership-label filter, never a prefix."""

    return (
        "labels.inferdrome = inferdrome AND "
        f"labels.controller_id = {labels.controller_id} AND "
        f"labels.plan_id = {labels.plan_id} AND "
        f"labels.arm_id = {labels.arm_id} AND "
        "labels.managed_by = inferdrome_gcp_execution_v1 AND "
        "labels.role = provider-envelope"
    )


class _SealedGoogleComputeTransport(GoogleComputeTransport):
    """Private executable Compute surface minted only after a sealed proof.

    ``GoogleComputeTransport`` deliberately remains a read-only/projection
    boundary even when every client object is injected.  This distinct class
    is created solely inside
    ``_create_google_compute_transport_for_v2_activation_guard``
    after that function consumes a supervisor-issued, watchdog-bound proof.
    It rechecks exact request and opaque projection bindings immediately before
    each SDK invocation.  It never initializes an SDK, ADC, or credential on
    its own; the enclosing factory receives an explicit lazy component supplier.
    """

    def __init__(
        self,
        *,
        sdk: Any,
        client: _InstancesClient,
        operations_client: _ZoneOperationsClient,
        images_client: _ImagesClient,
        disks_client: _DisksClient,
        capability: GcpV2MutationCapability,
        startup_projection: GcpV2StartupProjection,
        now_fn: Callable[[], datetime],
        _seal: object,
    ) -> None:
        if _seal is not _SEALED_GCE_TRANSPORT_SEAL:
            raise GcpTransportError("SEALED_GCE_TRANSPORT_INVALID")
        super().__init__(
            sdk=sdk,
            client=client,
            operations_client=operations_client,
            images_client=images_client,
            disks_client=disks_client,
        )
        self._capability = capability
        self._startup_projection = _strict_startup_projection_for_authority(
            startup_projection,
            request_digest=capability.request_digest,
            startup_projection_digest=capability.startup_projection_digest,
            execution_payload_digest=capability.execution_payload_digest,
            code="SEALED_GCE_PROJECTION_MISMATCH",
        )
        self._now_fn = now_fn
        self._seal = _seal
        self._disk_operations: dict[
            str, tuple[object, GcpV2DiskCleanupBinding]
        ] = {}
        self._disk_operations_lock = threading.Lock()

    def _now(self, *, code: str) -> datetime:
        try:
            return self._now_fn().astimezone(UTC)
        except (AttributeError, TypeError, ValueError):
            raise GcpTransportError(code) from None

    def _assert_provider_runtime_open(self) -> None:
        try:
            deadline = datetime.fromisoformat(
                str(self._capability.provider_runtime_deadline_at).replace(
                    "Z", "+00:00"
                )
            )
        except (TypeError, ValueError):
            raise GcpTransportError("SEALED_GCE_TIME_INVALID") from None
        if self._now(code="SEALED_GCE_TIME_INVALID") >= deadline:
            raise GcpTransportError("SEALED_GCE_RUNTIME_EXPIRED")

    def _assert_cleanup_window_open(self) -> None:
        try:
            activated = datetime.fromisoformat(
                str(self._capability.activated_at).replace("Z", "+00:00")
            )
            deadline = datetime.fromisoformat(
                str(self._capability.watchdog_cleanup_deadline_at).replace(
                    "Z", "+00:00"
                )
            )
        except (TypeError, ValueError):
            raise GcpTransportError("SEALED_GCE_TIME_INVALID") from None
        now = self._now(code="SEALED_GCE_TIME_INVALID")
        if not (activated <= now < deadline):
            raise GcpTransportError("SEALED_GCE_CLEANUP_EXPIRED")

    def _assert_exact_request(self, request: GcpInsertRequest) -> None:
        try:
            request_digest = gcp_execution_request_digest(request)
        except (TypeError, ValueError):
            raise GcpTransportError("SEALED_GCE_REQUEST_INVALID") from None
        if (
            request_digest != self._capability.request_digest
            or request.project_id != self._capability.project_id
            or request.region != self._capability.region
            or request.zone != self._capability.zone
            or request.instance_name != self._capability.instance_name
            or request.labels != self._capability.labels
        ):
            raise GcpTransportError("SEALED_GCE_REQUEST_MISMATCH")
        _assert_startup_projection_matches_request(
            self._startup_projection,
            request,
            execution_payload_digest=self._capability.execution_payload_digest,
            code="SEALED_GCE_PROJECTION_MISMATCH",
        )

    def _metadata_values(self, request: GcpInsertRequest) -> dict[str, str]:
        """Return only the three non-secret v2 provider binding values."""

        self._assert_exact_request(request)
        values = {
            _GCP_V2_METADATA_PROJECTION_ID: self._startup_projection.projection_id,
            _GCP_V2_METADATA_REQUEST_DIGEST: self._capability.request_digest,
            _GCP_V2_METADATA_EXECUTION_PAYLOAD_DIGEST: (
                self._capability.execution_payload_digest
            ),
        }
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 127
            or len(value) > 256
            for key, value in values.items()
        ):
            raise GcpTransportError("SEALED_GCE_METADATA_INVALID")
        return values

    def _project_metadata(self, request: GcpInsertRequest) -> object:
        """Build exact opaque metadata with an explicit official SDK shape."""

        values = self._metadata_values(request)
        try:
            item_factory = self._sdk.Items
            metadata_factory = self._sdk.Metadata
            items = [
                item_factory(key=key, value=value)
                for key, value in sorted(values.items())
            ]
            return metadata_factory(items=items)
        except Exception:
            raise GcpTransportError("SEALED_GCE_METADATA_INVALID") from None

    @staticmethod
    def _read_projected_metadata(instance: object) -> dict[str, str]:
        """Read an SDK projection without accepting duplicate or extra values."""

        try:
            projected = cast(Any, instance)
            metadata = projected.metadata
            raw_items = metadata.items
            items = list(raw_items)
        except (AttributeError, TypeError):
            raise GcpTransportError("SEALED_GCE_METADATA_INVALID") from None
        result: dict[str, str] = {}
        for item in items:
            key = getattr(item, "key", None)
            value = getattr(item, "value", None)
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or key in result
            ):
                raise GcpTransportError("SEALED_GCE_METADATA_INVALID")
            result[key] = value
        return result

    def _project_sealed_create_request(
        self, request: GcpInsertRequest, *, request_id: str
    ) -> object:
        """Project and then revalidate metadata immediately before insert."""

        self._assert_exact_request(request)
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
            expected_metadata = self._metadata_values(request)
            instance = self._project_instance(
                request, metadata=self._project_metadata(request)
            )
            if self._read_projected_metadata(instance) != expected_metadata:
                raise GcpTransportError("SEALED_GCE_METADATA_MISMATCH")
            projected = self._sdk.InsertInstanceRequest(
                project=request.project_id,
                zone=request.zone,
                instance_resource=instance,
                request_id=request_id,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("INSERT_REQUEST_INVALID") from None
        # Revalidate after all object construction and immediately before the
        # sole SDK insert call.  No mutable startup payload reaches this point.
        self._assert_exact_request(request)
        if self._read_projected_metadata(
            getattr(projected, "instance_resource", None)
        ) != expected_metadata:
            raise GcpTransportError("SEALED_GCE_METADATA_MISMATCH")
        return projected

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_provider_runtime_open()
        projected = self._project_sealed_create_request(request, request_id=request_id)
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        try:
            operation = self._client.insert(request=projected, timeout=timeout_seconds)
        except Exception:
            raise GcpTransportError(
                "INSERT_SUBMISSION_UNKNOWN", ambiguous=True
            ) from None
        return self._remember(operation, "insert", request)

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_cleanup_window_open()
        self._assert_exact_request(request)
        try:
            if (
                request_id != request.delete_request_id
                or uuid.UUID(request_id).int == 0
            ):
                raise GcpTransportError("REQUEST_ID_MISMATCH")
            projected = self._sdk.DeleteInstanceRequest(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                request_id=request_id,
            )
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("DELETE_REQUEST_INVALID") from None
        self._assert_cleanup_window_open()
        self._assert_exact_request(request)
        try:
            operation = self._client.delete(request=projected, timeout=timeout_seconds)
        except Exception:
            raise GcpTransportError(
                "DELETE_SUBMISSION_UNKNOWN", ambiguous=True
            ) from None
        return self._remember(operation, "delete", request)

    @staticmethod
    def _strict_attached_boot_disk(
        value: object, request: GcpInsertRequest
    ) -> tuple[str, str, str]:
        """Read one exact boot attachment; never derive a disk from a name."""

        try:
            attached = list(cast(Any, value).disks or ())
        except (AttributeError, TypeError):
            raise GcpTransportError("EXACT_BOOT_DISK_ATTACHMENT_INVALID") from None
        boot = [disk for disk in attached if getattr(disk, "boot", None) is True]
        if len(boot) != 1 or getattr(boot[0], "auto_delete", None) is not True:
            raise GcpTransportError("EXACT_BOOT_DISK_ATTACHMENT_INVALID")
        source = _canonical_provider_self_link(
            getattr(boot[0], "source", None),
            expected=(
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "disks",
                str(getattr(boot[0], "source", "")).rsplit("/", 1)[-1],
            ),
            code="EXACT_BOOT_DISK_ATTACHMENT_INVALID",
        )
        source_parts = source.split("/")
        disk_name = source_parts[-1]
        device_name = getattr(boot[0], "device_name", None)
        if not isinstance(device_name, str) or not _RESOURCE_NAME_RE.fullmatch(
            device_name
        ):
            raise GcpTransportError("EXACT_BOOT_DISK_ATTACHMENT_INVALID")
        return disk_name, device_name, source

    @staticmethod
    def _strict_v2_instance(
        value: object, request: GcpInsertRequest
    ) -> GcpV2ExactOwnedInstance:
        """Validate stable provider instance identity plus all v1 facts."""

        observation = _observation(cast(Any, value), request)
        if observation.state not in {"RUNNING", "TERMINATED"}:
            raise GcpTransportError("EXACT_OWNED_INSTANCE_INVALID")
        provider_instance_id = _provider_positive_int(
            getattr(value, "id", None), code="EXACT_OWNED_INSTANCE_INVALID"
        )
        self_link = _canonical_provider_self_link(
            getattr(value, "self_link", None),
            expected=(
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "instances",
                request.instance_name,
            ),
            code="EXACT_OWNED_INSTANCE_INVALID",
        )
        try:
            return GcpV2ExactOwnedInstance(
                schema_version="inferdrome.gcp-owned-instance.v2",
                request_digest=gcp_execution_request_digest(request),
                project_id=request.project_id,
                region=request.region,
                zone=request.zone,
                instance_name=request.instance_name,
                provider_instance_id=provider_instance_id,
                self_link=self_link,
                labels=request.labels,
                state=cast(Literal["RUNNING", "TERMINATED"], observation.state),
            )
        except (TypeError, ValueError):
            raise GcpTransportError("EXACT_OWNED_INSTANCE_INVALID") from None

    @staticmethod
    def _strict_v2_disk(
        value: object,
        request: GcpInsertRequest,
        *,
        attached_instance: GcpV2ExactOwnedInstance,
        boot_disk_name: str,
        boot_device_name: str,
        boot_source_disk_self_link: str,
    ) -> GcpV2ExactOwnedBootDisk:
        """Project one live disk only if its boot provenance is exact."""

        if getattr(value, "name", None) != boot_disk_name:
            raise GcpTransportError("EXACT_BOOT_DISK_IDENTITY_MISMATCH")
        disk_self_link = _canonical_provider_self_link(
            getattr(value, "self_link", None),
            expected=(
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "disks",
                boot_disk_name,
            ),
            code="EXACT_BOOT_DISK_IDENTITY_MISMATCH",
        )
        source_image = _canonical_provider_self_link(
            getattr(value, "source_image", None),
            expected=request.boot_image.image_name.split("/"),
            code="EXACT_BOOT_DISK_PROVENANCE_MISMATCH",
        )
        if source_image != request.boot_image.image_name:
            raise GcpTransportError("EXACT_BOOT_DISK_PROVENANCE_MISMATCH")
        source_image_id = _provider_positive_int(
            getattr(value, "source_image_id", None),
            code="EXACT_BOOT_DISK_PROVENANCE_MISMATCH",
        )
        if source_image_id != request.boot_image.provider_image_id:
            raise GcpTransportError("EXACT_BOOT_DISK_PROVENANCE_MISMATCH")
        disk_type = _canonical_provider_self_link(
            getattr(value, "type", None),
            expected=(
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "diskTypes",
                request.boot_disk_type,
            ),
            code="EXACT_BOOT_DISK_TYPE_MISMATCH",
        )
        if disk_type.rsplit("/", 1)[-1] != request.boot_disk_type:
            raise GcpTransportError("EXACT_BOOT_DISK_TYPE_MISMATCH")
        if _safe_labels(getattr(value, "labels", None)) != request.labels:
            raise GcpTransportError("EXACT_BOOT_DISK_OWNERSHIP_MISMATCH")
        try:
            users = list(getattr(value, "users", None) or ())
        except TypeError:
            raise GcpTransportError("EXACT_BOOT_DISK_ATTACHMENT_INVALID") from None
        if len(users) != 1:
            raise GcpTransportError("EXACT_BOOT_DISK_ATTACHMENT_INVALID")
        _canonical_provider_self_link(
            users[0],
            expected=(
                "projects",
                request.project_id,
                "zones",
                request.zone,
                "instances",
                request.instance_name,
            ),
            code="EXACT_BOOT_DISK_ATTACHMENT_INVALID",
        )
        try:
            return GcpV2ExactOwnedBootDisk(
                schema_version="inferdrome.gcp-owned-boot-disk.v2",
                request_digest=gcp_execution_request_digest(request),
                project_id=request.project_id,
                zone=request.zone,
                instance_name=request.instance_name,
                disk_name=boot_disk_name,
                provider_disk_id=_provider_positive_int(
                    getattr(value, "id", None),
                    code="EXACT_BOOT_DISK_IDENTITY_MISMATCH",
                ),
                self_link=disk_self_link,
                attached_instance_provider_id=attached_instance.provider_instance_id,
                attached_instance_self_link=attached_instance.self_link,
                boot_device_name=boot_device_name,
                boot_source_disk_self_link=boot_source_disk_self_link,
                boot_attachment=True,
                attachment_state="ATTACHED",
                labels=request.labels,
                source_image_name=source_image,
                source_image_provider_id=source_image_id,
                source_image_digest=request.boot_image.digest,
                disk_type=request.boot_disk_type,
                size_gib=_provider_positive_int(
                    getattr(value, "size_gb", None),
                    code="EXACT_BOOT_DISK_SIZE_MISMATCH",
                ),
                state="PRESENT",
            )
        except GcpTransportError:
            raise
        except (TypeError, ValueError):
            raise GcpTransportError("EXACT_BOOT_DISK_RESPONSE_INVALID") from None

    def _get_raw_exact_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> object:
        try:
            return self._client.get(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("EXACT_OWNED_INSTANCE_NOT_FOUND") from None
            raise _sanitize_exception(error, "EXACT_OWNED_INSTANCE_FAILED") from None

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        value = self._get_raw_exact_instance(request, timeout_seconds=timeout_seconds)
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        return self._strict_v2_instance(value, request)

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        instance_value = self._get_raw_exact_instance(
            request, timeout_seconds=timeout_seconds
        )
        instance = self._strict_v2_instance(instance_value, request)
        disk_name, device_name, source_link = self._strict_attached_boot_disk(
            instance_value, request
        )
        if self._disks_client is None:
            raise GcpTransportError("EXACT_BOOT_DISK_OBSERVATION_UNAVAILABLE")
        try:
            disk_value = self._disks_client.get(
                project=request.project_id,
                zone=request.zone,
                disk=disk_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("EXACT_BOOT_DISK_NOT_FOUND") from None
            raise _sanitize_exception(
                error, "EXACT_BOOT_DISK_OBSERVATION_FAILED"
            ) from None
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        return self._strict_v2_disk(
            disk_value,
            request,
            attached_instance=instance,
            boot_disk_name=disk_name,
            boot_device_name=device_name,
            boot_source_disk_self_link=source_link,
        )

    @staticmethod
    def _one_complete_disk_page(values: object) -> list[object]:
        """Accept exactly one bounded non-paginated label-scoped response."""

        token = getattr(values, "next_page_token", None)
        if token not in (None, ""):
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_INCOMPLETE")
        if not isinstance(values, Iterable):
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_INCOMPLETE") from None
        output = list(cast(Iterable[object], values))
        if len(output) != 1:
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_AMBIGUOUS")
        return output

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        observed = self.get_exact_owned_boot_disk_v2(
            request, timeout_seconds=timeout_seconds
        )
        if self._disks_client is None:
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_UNAVAILABLE")
        try:
            list_request = self._sdk.ListDisksRequest(
                project=request.project_id,
                zone=request.zone,
                filter=_exact_ownership_filter(request.labels),
                max_results=2,
            )
            values = self._disks_client.list(
                request=list_request, timeout=timeout_seconds
            )
            candidate_values = self._one_complete_disk_page(values)
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_INCOMPLETE") from None
        instance_value = self._get_raw_exact_instance(
            request, timeout_seconds=timeout_seconds
        )
        instance = self._strict_v2_instance(instance_value, request)
        disk_name, device_name, source_link = self._strict_attached_boot_disk(
            instance_value, request
        )
        candidate = self._strict_v2_disk(
            candidate_values[0],
            request,
            attached_instance=instance,
            boot_disk_name=disk_name,
            boot_device_name=device_name,
            boot_source_disk_self_link=source_link,
        )
        if candidate != observed:
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_MISMATCH")
        self._assert_provider_runtime_open()
        self._assert_exact_request(request)
        try:
            return issue_gcp_v2_owned_boot_disk_inventory(
                disk=candidate,
                observed_at=self._now(code="EXACT_BOOT_DISK_INVENTORY_TIME_INVALID"),
            )
        except GcpExecutionError:
            raise GcpTransportError("EXACT_BOOT_DISK_INVENTORY_INVALID") from None

    def _strict_exact_disk_binding(
        self, binding: GcpV2DiskCleanupBinding
    ) -> GcpV2DiskCleanupBinding:
        """Canonicalize and bind disk cleanup to this one capability only."""

        try:
            raw = canonical_json_bytes(binding.model_dump(mode="json"))
            parsed = GcpV2DiskCleanupBinding.model_validate_json(raw)
        except (AttributeError, ValidationError, TypeError, ValueError):
            raise GcpTransportError("EXACT_DISK_BINDING_INVALID") from None
        if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
            raise GcpTransportError("EXACT_DISK_BINDING_INVALID")
        if (
            parsed.request_digest != self._capability.request_digest
            or parsed.project_id != self._capability.project_id
            or parsed.zone != self._capability.zone
            or parsed.instance_name != self._capability.instance_name
            or parsed.labels != self._capability.labels
            or parsed.controller_id != self._capability.controller_id
        ):
            raise GcpTransportError("EXACT_DISK_BINDING_MISMATCH")
        return parsed

    @staticmethod
    def _strict_bound_disk(
        value: object, binding: GcpV2DiskCleanupBinding
    ) -> GcpV2ExactOwnedBootDisk:
        """Validate a current named disk against immutable bound provenance."""

        expected = binding.disk
        if getattr(value, "name", None) != expected.disk_name:
            raise GcpTransportError("EXACT_DISK_IDENTITY_MISMATCH")
        self_link = _canonical_provider_self_link(
            getattr(value, "self_link", None),
            expected=(
                "projects",
                binding.project_id,
                "zones",
                binding.zone,
                "disks",
                expected.disk_name,
            ),
            code="EXACT_DISK_IDENTITY_MISMATCH",
        )
        source_image = _canonical_provider_self_link(
            getattr(value, "source_image", None),
            expected=expected.source_image_name.split("/"),
            code="EXACT_DISK_PROVENANCE_MISMATCH",
        )
        if (
            self_link != expected.self_link
            or _provider_positive_int(
                getattr(value, "id", None), code="EXACT_DISK_IDENTITY_MISMATCH"
            )
            != expected.provider_disk_id
            or source_image != expected.source_image_name
            or _provider_positive_int(
                getattr(value, "source_image_id", None),
                code="EXACT_DISK_PROVENANCE_MISMATCH",
            )
            != expected.source_image_provider_id
            or _safe_labels(getattr(value, "labels", None)) != expected.labels
        ):
            raise GcpTransportError("EXACT_DISK_PROVENANCE_MISMATCH")
        disk_type = _canonical_provider_self_link(
            getattr(value, "type", None),
            expected=(
                "projects",
                binding.project_id,
                "zones",
                binding.zone,
                "diskTypes",
                expected.disk_type,
            ),
            code="EXACT_DISK_TYPE_MISMATCH",
        )
        if (
            disk_type.rsplit("/", 1)[-1] != expected.disk_type
            or _provider_positive_int(
                getattr(value, "size_gb", None), code="EXACT_DISK_SIZE_MISMATCH"
            )
            != expected.size_gib
        ):
            raise GcpTransportError("EXACT_DISK_PROVENANCE_MISMATCH")
        try:
            users = list(getattr(value, "users", None) or ())
        except TypeError:
            raise GcpTransportError("EXACT_DISK_ATTACHMENT_INVALID") from None
        if not users:
            attachment_state: Literal["ATTACHED", "DETACHED"] = "DETACHED"
            provider_id: int | None = None
            provider_link: str | None = None
        elif len(users) == 1:
            _canonical_provider_self_link(
                users[0],
                expected=(
                    "projects",
                    binding.project_id,
                    "zones",
                    binding.zone,
                    "instances",
                    binding.instance_name,
                ),
                code="EXACT_DISK_ATTACHMENT_INVALID",
            )
            attachment_state = "ATTACHED"
            provider_id = binding.attached_instance.provider_instance_id
            provider_link = binding.attached_instance.self_link
        else:
            raise GcpTransportError("EXACT_DISK_ATTACHMENT_INVALID")
        try:
            return GcpV2ExactOwnedBootDisk(
                schema_version=expected.schema_version,
                request_digest=expected.request_digest,
                project_id=expected.project_id,
                zone=expected.zone,
                instance_name=expected.instance_name,
                disk_name=expected.disk_name,
                provider_disk_id=expected.provider_disk_id,
                self_link=expected.self_link,
                attached_instance_provider_id=provider_id,
                attached_instance_self_link=provider_link,
                boot_device_name=expected.boot_device_name,
                boot_source_disk_self_link=expected.boot_source_disk_self_link,
                boot_attachment=True,
                attachment_state=attachment_state,
                labels=expected.labels,
                source_image_name=expected.source_image_name,
                source_image_provider_id=expected.source_image_provider_id,
                source_image_digest=expected.source_image_digest,
                disk_type=expected.disk_type,
                size_gib=expected.size_gib,
                state="PRESENT",
            )
        except (TypeError, ValueError):
            raise GcpTransportError("EXACT_DISK_RESPONSE_INVALID") from None

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window_open()
        parsed = self._strict_exact_disk_binding(binding)
        if self._disks_client is None:
            raise GcpTransportError("EXACT_DISK_OBSERVATION_UNAVAILABLE")
        try:
            value = self._disks_client.get(
                project=parsed.project_id,
                zone=parsed.zone,
                disk=parsed.disk.disk_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if _is_not_found(error):
                raise GcpTransportError("EXACT_DISK_NOT_FOUND") from None
            raise _sanitize_exception(error, "EXACT_DISK_OBSERVATION_FAILED") from None
        self._assert_cleanup_window_open()
        self._strict_exact_disk_binding(parsed)
        return self._strict_bound_disk(value, parsed)

    def _remember_exact_disk_operation(
        self,
        operation: object,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: str,
    ) -> GcpV2DiskDeleteOperation:
        name = getattr(operation, "name", None)
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,127}", name
        ):
            raise GcpTransportError(
                "EXACT_DISK_OPERATION_IDENTITY_MISSING", ambiguous=True
            )
        operation_name = "/".join(
            (
                "projects",
                binding.project_id,
                "zones",
                binding.zone,
                "operations",
                name,
            )
        )
        operation_id = "disk-op-" + hashlib.sha256(
            operation_name.encode("utf-8")
        ).hexdigest()[:32]
        result = GcpV2DiskDeleteOperation(
            schema_version="inferdrome.gcp-disk-delete-operation.v2",
            operation_id=operation_id,
            operation_name=operation_name,
            request_id=request_id,
            request_digest=binding.request_digest,
            project_id=binding.project_id,
            zone=binding.zone,
            instance_name=binding.instance_name,
            disk_name=binding.disk.disk_name,
            labels=binding.labels,
        )
        with self._disk_operations_lock:
            self._disk_operations[result.operation_id] = (operation, binding)
        return result

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        self._assert_cleanup_window_open()
        parsed = self._strict_exact_disk_binding(binding)
        if request_id != gcp_v2_disk_delete_request_id(parsed):
            raise GcpTransportError("DISK_DELETE_REQUEST_ID_MISMATCH")
        if self._disks_client is None:
            raise GcpTransportError("EXACT_DISK_DELETE_UNAVAILABLE")
        try:
            projected = self._sdk.DeleteDiskRequest(
                project=parsed.project_id,
                zone=parsed.zone,
                disk=parsed.disk.disk_name,
                request_id=request_id,
            )
        except Exception:
            raise GcpTransportError("DISK_DELETE_REQUEST_INVALID") from None
        self._assert_cleanup_window_open()
        self._strict_exact_disk_binding(parsed)
        try:
            operation = self._disks_client.delete(
                request=projected, timeout=timeout_seconds
            )
        except Exception:
            raise GcpTransportError(
                "DISK_DELETE_SUBMISSION_UNKNOWN", ambiguous=True
            ) from None
        return self._remember_exact_disk_operation(
            operation, parsed, request_id=request_id
        )

    @staticmethod
    def _disk_operation_status(
        sdk: Any,
        provider_operation: object,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        """Reconcile one exact named-disk operation without broad inference."""

        raw_name = getattr(provider_operation, "name", None)
        expected_name = operation.operation_name.rsplit("/", 1)[-1]
        if raw_name != expected_name:
            raise GcpTransportError(
                "EXACT_DISK_OPERATION_RESPONSE_MISMATCH", ambiguous=True
            )
        target = _canonical_provider_self_link(
            getattr(provider_operation, "target_link", None),
            expected=(
                "projects",
                operation.project_id,
                "zones",
                operation.zone,
                "disks",
                operation.disk_name,
            ),
            code="EXACT_DISK_OPERATION_RESPONSE_MISMATCH",
        )
        if not target:
            raise GcpTransportError(
                "EXACT_DISK_OPERATION_RESPONSE_MISMATCH", ambiguous=True
            )
        if getattr(provider_operation, "operation_type", None) != "delete":
            raise GcpTransportError(
                "EXACT_DISK_OPERATION_RESPONSE_MISMATCH", ambiguous=True
            )
        try:
            result_method = getattr(provider_operation, "result", None)
            if callable(result_method):
                result_method(timeout=timeout_seconds)
        except TimeoutError:
            return GcpV2DiskDeleteResult(
                schema_version="inferdrome.gcp-disk-delete-result.v2",
                operation_id=operation.operation_id,
                status="TIMEOUT",
            )
        except Exception:
            # A provider polling failure cannot prove that a submitted delete
            # did not happen.  Preserve the deterministic operation identity
            # for an exact later reconciliation.
            return GcpV2DiskDeleteResult(
                schema_version="inferdrome.gcp-disk-delete-result.v2",
                operation_id=operation.operation_id,
                status="TIMEOUT",
            )
        status = getattr(provider_operation, "status", None)
        enum = getattr(getattr(sdk, "Operation", None), "Status", None)
        done = getattr(enum, "DONE", "DONE")
        pending = getattr(enum, "PENDING", "PENDING")
        running = getattr(enum, "RUNNING", "RUNNING")
        if status in ("DONE", "done", done):
            if _provider_error_present(getattr(provider_operation, "error", None)):
                return GcpV2DiskDeleteResult(
                    schema_version="inferdrome.gcp-disk-delete-result.v2",
                    operation_id=operation.operation_id,
                    status="ERROR",
                    error_code="PROVIDER_OPERATION_FAILED",
                )
            return GcpV2DiskDeleteResult(
                schema_version="inferdrome.gcp-disk-delete-result.v2",
                operation_id=operation.operation_id,
                status="DONE",
            )
        if status in ("PENDING", "pending", "RUNNING", "running", pending, running):
            return GcpV2DiskDeleteResult(
                schema_version="inferdrome.gcp-disk-delete-result.v2",
                operation_id=operation.operation_id,
                status="TIMEOUT",
            )
        raise GcpTransportError("EXACT_DISK_OPERATION_RESPONSE_INVALID", ambiguous=True)

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        self._assert_cleanup_window_open()
        try:
            value = operation.model_dump(mode="json")
            raw = canonical_json_bytes(value)
            # Operation resource names are opaque provider identifiers.  Use
            # typed mapping validation here rather than the v1 JSON ingress
            # credential heuristic, which intentionally treats arbitrary long
            # strings as unsafe before it knows this field's schema.
            parsed = GcpV2DiskDeleteOperation.model_validate(value)
        except (AttributeError, ValidationError, TypeError, ValueError):
            raise GcpTransportError("EXACT_DISK_OPERATION_INVALID") from None
        if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
            raise GcpTransportError("EXACT_DISK_OPERATION_INVALID")
        if (
            parsed.request_digest != self._capability.request_digest
            or parsed.project_id != self._capability.project_id
            or parsed.zone != self._capability.zone
            or parsed.instance_name != self._capability.instance_name
            or parsed.labels != self._capability.labels
        ):
            raise GcpTransportError("EXACT_DISK_OPERATION_MISMATCH")
        with self._disk_operations_lock:
            remembered = self._disk_operations.get(parsed.operation_id)
        provider_operation = remembered[0] if remembered is not None else None
        if provider_operation is None:
            try:
                raw_name = parsed.operation_name.rsplit("/", 1)[-1]
                operations_client = cast(
                    _ZoneOperationsClient, self._operations_client
                )
                provider_operation = operations_client.get(
                    project=parsed.project_id,
                    zone=parsed.zone,
                    operation=raw_name,
                    timeout=timeout_seconds,
                )
            except Exception:
                raise GcpTransportError(
                    "EXACT_DISK_OPERATION_RECONCILIATION_FAILED", ambiguous=True
                ) from None
        self._assert_cleanup_window_open()
        return self._disk_operation_status(
            self._sdk,
            provider_operation,
            parsed,
            timeout_seconds=timeout_seconds,
        )

    def confirm_exact_boot_disk_absent(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskAbsenceObservation:
        self._assert_cleanup_window_open()
        parsed = self._strict_exact_disk_binding(binding)
        if self._disks_client is None:
            raise GcpTransportError("EXACT_DISK_ABSENCE_OBSERVATION_UNAVAILABLE")
        try:
            self._disks_client.get(
                project=parsed.project_id,
                zone=parsed.zone,
                disk=parsed.disk.disk_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if _is_not_found(error):
                self._assert_cleanup_window_open()
                self._strict_exact_disk_binding(parsed)
                return GcpV2DiskAbsenceObservation(
                    schema_version="inferdrome.gcp-disk-absence-observation.v2",
                    request_digest=parsed.request_digest,
                    project_id=parsed.project_id,
                    zone=parsed.zone,
                    instance_name=parsed.instance_name,
                    disk_name=parsed.disk.disk_name,
                    labels=parsed.labels,
                    state="NOT_FOUND",
                )
            raise _sanitize_exception(
                error, "EXACT_DISK_ABSENCE_OBSERVATION_FAILED"
            ) from None
        raise GcpTransportError("EXACT_DISK_RESIDUAL")


class _SealedGoogleComputeCleanupTransport(_SealedGoogleComputeTransport):
    """Private no-create GCE surface used only behind cleanup wrappers.

    The inherited exact read/delete/reconcile implementation still rechecks the
    original immutable capability and opaque projection.  This subtype removes
    its create method as defense in depth: a watchdog or recovery adapter may
    never regain create authority even if an internal implementation is changed
    later.  Recovery reads use the cleanup horizon; the outer recovery wrapper
    additionally enforces its shorter authorization expiry before every call.
    """

    def _assert_provider_runtime_open(self) -> None:
        self._assert_cleanup_window_open()

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        del request, timeout_seconds, request_id
        raise GcpTransportError("SEALED_GCE_CLEANUP_CREATE_FORBIDDEN")


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
            allowed_operations=frozenset(
                {
                    "insert",
                    "wait_operation",
                    "get_instance",
                    "list_owned",
                    "list_owned_complete",
                    "get_image",
                    "get_boot_disk",
                    "get_exact_owned_boot_disk_v2",
                    "get_exact_owned_instance_v2",
                    "get_exact_owned_boot_disk_inventory_v2",
                    "get_instance_safety",
                    "confirm_boot_disk_absent",
                    "delete",
                }
            ),
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
        operation = self._call_transport_after_authority(
            "insert", request, timeout_seconds=timeout_seconds, request_id=request_id
        )
        with self._insert_lock:
            self._insert_succeeded = True
        return operation

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        self._assert_watchdog_cleanup_open()
        self._exact_operation(operation)
        return self._call_transport_after_authority(
            "wait_operation", operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance", request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned", request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned_complete", request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_image", request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_boot_disk", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_instance_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_provider_runtime_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_inventory_v2",
            request,
            timeout_seconds=timeout_seconds,
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance_safety", request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_watchdog_cleanup_open()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "confirm_boot_disk_absent", request, timeout_seconds=timeout_seconds
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
        return self._call_transport_after_authority(
            "delete", request, timeout_seconds=timeout_seconds, request_id=request_id
        )

def is_gcp_v2_capability_bound_transport(value: object) -> bool:
    """Recognize only adapters minted by the capability factory below."""

    return (
        type(value) is GcpV2CapabilityBoundTransport
        and getattr(value, "_seal", None) is _CAPABILITY_BOUND_TRANSPORT_SEAL
    )


def _consume_gcp_v2_future_activation_guard_for_transport(
    activation_guard: object, *, now: datetime
) -> GcpV2MutationCapability:
    """Consume the supervisor guard before any transport construction edge."""

    try:
        from inferdrome.deployment.gcp_supervisor import (
            consume_gcp_v2_future_mutation_activation_guard,
        )

        proof = consume_gcp_v2_future_mutation_activation_guard(
            activation_guard, now=now, purpose="transport"
        )
    except GcpExecutionError:
        raise GcpTransportError("MUTATION_ACTIVATION_GUARD_INVALID") from None
    except BaseException:
        raise GcpTransportError("MUTATION_ACTIVATION_GUARD_INVALID") from None
    return proof.capability


def _bind_gcp_v2_mutation_transport_from_activation_guard(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    activation_guard: object,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2CapabilityBoundTransport:
    """Consume one sealed supervisor guard and bind an injected transport.

    This internal fake-only seam cannot manufacture approval, cost, watchdog,
    or activation state; it receives only the opaque guard minted by the
    concrete lifecycle supervisor.  It cannot create an SDK client, discover
    ADC, or make a provider request.  The guard's transport purpose is
    consumed before the wrapped transport is returned, so a failed or replayed
    factory attempt never opens a second create route.
    """

    parsed_capability = _consume_gcp_v2_future_activation_guard_for_transport(
        activation_guard, now=now
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
            allowed_operations=frozenset(
                {
                    "wait_operation",
                    "get_instance",
                    "list_owned",
                    "list_owned_complete",
                    "get_image",
                    "get_boot_disk",
                    "get_exact_owned_boot_disk_v2",
                    "get_exact_owned_instance_v2",
                    "get_exact_owned_boot_disk_inventory_v2",
                    "get_instance_safety",
                    "confirm_boot_disk_absent",
                    "delete",
                    "read_exact_owned_boot_disk",
                    "delete_exact_boot_disk",
                    "reconcile_exact_disk_delete",
                    "confirm_exact_boot_disk_absent",
                }
            ),
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
        return self._call_transport_after_authority(
            "wait_operation", operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance", request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned", request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned_complete", request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_image", request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_boot_disk", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_instance_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_inventory_v2",
            request,
            timeout_seconds=timeout_seconds,
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance_safety", request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "confirm_boot_disk_absent", request, timeout_seconds=timeout_seconds
        )

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_cleanup_window()
        self._exact_request(request)
        if request_id != request.delete_request_id:
            raise GcpTransportError("WATCHDOG_CLEANUP_REQUEST_ID_MISMATCH")
        return self._call_transport_after_authority(
            "delete", request, timeout_seconds=timeout_seconds, request_id=request_id
        )

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        """Read only the already-bound exact boot disk under watchdog scope."""

        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "read_exact_owned_boot_disk", binding, timeout_seconds=timeout_seconds
        )

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        """Delete only the provider-observed disk identity bound to this lease."""

        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "delete_exact_boot_disk",
            binding,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "reconcile_exact_disk_delete", operation, timeout_seconds=timeout_seconds
        )

    def confirm_exact_boot_disk_absent(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2DiskAbsenceObservation:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "confirm_exact_boot_disk_absent", binding, timeout_seconds=timeout_seconds
        )


def _bind_gcp_v2_watchdog_cleanup_transport_from_capability(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    capability: GcpV2MutationCapability,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2WatchdogCleanupBoundTransport:
    """Bind a raw capability only after a private watchdog handle was consumed.

    This helper intentionally has no public export.  A content-addressed
    capability is a content-addressed local binding artifact, not a signature
    or proof of operator authorization. Callers must first pass the opaque
    watchdog-issued handoff
    through :meth:`GcpV2LocalTransportFactory.bind_watchdog_cleanup`.
    """

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


def _watchdog_cleanup_authority_id(value: object) -> Sha256Digest | None:
    """Return only an opaque identifier for a sealed cleanup adapter.

    The executor uses this non-exported probe to bind its local configuration
    without reading or accepting a nested capability object as authority.
    """

    if not is_gcp_v2_watchdog_cleanup_bound_transport(value):
        return None
    capability = getattr(value, "capability", None)
    identifier = getattr(capability, "capability_id", None)
    return (
        identifier
        if isinstance(identifier, str) and identifier.startswith("sha256:")
        else None
    )


def _watchdog_cleanup_transport_matches(
    value: object,
    *,
    request_digest: str,
    startup_projection_digest: str,
    execution_payload_digest: str,
    project_id: str,
    region: str,
    zone: str,
    instance_name: str,
    labels: GcpExecutionLabels,
) -> bool:
    """Validate bound watchdog facts without exporting a raw capability."""

    if not is_gcp_v2_watchdog_cleanup_bound_transport(value):
        return False
    capability = getattr(value, "capability", None)
    return (
        _watchdog_cleanup_authority_id(value) is not None
        and getattr(capability, "request_digest", None) == request_digest
        and getattr(capability, "startup_projection_digest", None)
        == startup_projection_digest
        and getattr(capability, "execution_payload_digest", None)
        == execution_payload_digest
        and getattr(capability, "project_id", None) == project_id
        and getattr(capability, "region", None) == region
        and getattr(capability, "zone", None) == zone
        and getattr(capability, "instance_name", None) == instance_name
        and getattr(capability, "labels", None) == labels
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
            allowed_operations=frozenset(
                {
                    "wait_operation",
                    "get_instance",
                    "list_owned",
                    "list_owned_complete",
                    "get_image",
                    "get_boot_disk",
                    "get_exact_owned_boot_disk_v2",
                    "get_exact_owned_instance_v2",
                    "get_exact_owned_boot_disk_inventory_v2",
                    "get_instance_safety",
                    "confirm_boot_disk_absent",
                    "delete",
                    "read_exact_owned_boot_disk",
                    "delete_exact_boot_disk",
                    "reconcile_exact_disk_delete",
                    "confirm_exact_boot_disk_absent",
                }
            ),
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
        return self._call_transport_after_authority(
            "wait_operation", operation, timeout_seconds=timeout_seconds
        )

    def get_instance(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance", request, timeout_seconds=timeout_seconds
        )

    def list_owned(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> tuple[GcpInstanceObservation, ...]:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned", request, timeout_seconds=timeout_seconds
        )

    def list_owned_complete(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpOwnedResourceInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "list_owned_complete", request, timeout_seconds=timeout_seconds
        )

    def get_image(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpImageObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_image", request, timeout_seconds=timeout_seconds
        )

    def get_boot_disk(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_boot_disk", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_instance_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedInstance:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_instance_v2", request, timeout_seconds=timeout_seconds
        )

    def get_exact_owned_boot_disk_inventory_v2(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpV2OwnedBootDiskInventory:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_exact_owned_boot_disk_inventory_v2",
            request,
            timeout_seconds=timeout_seconds,
        )

    def get_instance_safety(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpInstanceSafetyObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "get_instance_safety", request, timeout_seconds=timeout_seconds
        )

    def confirm_boot_disk_absent(
        self, request: GcpInsertRequest, *, timeout_seconds: int
    ) -> GcpBootDiskAbsenceObservation:
        self._assert_cleanup_window()
        self._exact_request(request)
        return self._call_transport_after_authority(
            "confirm_boot_disk_absent", request, timeout_seconds=timeout_seconds
        )

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        self._assert_cleanup_window()
        self._exact_request(request)
        if request_id != request.delete_request_id:
            raise GcpTransportError("CLEANUP_AUTHORIZATION_REQUEST_ID_MISMATCH")
        return self._call_transport_after_authority(
            "delete", request, timeout_seconds=timeout_seconds, request_id=request_id
        )

    def read_exact_owned_boot_disk(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2ExactOwnedBootDisk:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "read_exact_owned_boot_disk", binding, timeout_seconds=timeout_seconds
        )

    def delete_exact_boot_disk(
        self,
        binding: GcpV2DiskCleanupBinding,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteOperation:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "delete_exact_boot_disk",
            binding,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )

    def reconcile_exact_disk_delete(
        self,
        operation: GcpV2DiskDeleteOperation,
        *,
        timeout_seconds: int,
    ) -> GcpV2DiskDeleteResult:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "reconcile_exact_disk_delete", operation, timeout_seconds=timeout_seconds
        )

    def confirm_exact_boot_disk_absent(
        self, binding: GcpV2DiskCleanupBinding, *, timeout_seconds: int
    ) -> GcpV2DiskAbsenceObservation:
        self._assert_cleanup_window()
        return self._call_transport_after_authority(
            "confirm_exact_boot_disk_absent", binding, timeout_seconds=timeout_seconds
        )


def _bind_gcp_v2_cleanup_transport_from_authorization(
    *,
    transport_supplier: Callable[[], GcpComputeTransport],
    authorization: GcpCleanupRecoveryAuthorization,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime],
) -> GcpV2CleanupAuthorizationBoundTransport:
    """Bind raw recovery data only after a private recovery handle was consumed."""

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


def _recovery_cleanup_authority_id(value: object) -> Sha256Digest | None:
    """Return only the opaque recovery identifier for a sealed adapter."""

    if not is_gcp_v2_cleanup_authorization_bound_transport(value):
        return None
    authorization = getattr(value, "authorization", None)
    identifier = getattr(authorization, "authorization_id", None)
    return (
        identifier
        if isinstance(identifier, str) and identifier.startswith("sha256:")
        else None
    )


def _recovery_cleanup_transport_matches(
    value: object,
    *,
    approval_digest: str,
    lease_intent_anchor_digest: str,
    arm_id: str,
    plan_id: str,
    request_digest: str,
    startup_projection_digest: str,
    execution_payload_digest: str,
    project_id: str,
    region: str,
    zone: str,
    instance_name: str,
    labels: GcpExecutionLabels,
    disk_cleanup_binding: object,
) -> bool:
    """Validate recovery wrapper facts without exposing raw authorization."""

    if not is_gcp_v2_cleanup_authorization_bound_transport(value):
        return False
    authorization = getattr(value, "authorization", None)
    return (
        _recovery_cleanup_authority_id(value) is not None
        and getattr(authorization, "approval_digest", None) == approval_digest
        and getattr(authorization, "lease_intent_anchor_digest", None)
        == lease_intent_anchor_digest
        and getattr(authorization, "arm_id", None) == arm_id
        and getattr(authorization, "plan_id", None) == plan_id
        and getattr(authorization, "create_authority", None) is False
        and getattr(authorization, "allowed_actions", None)
        == (
            "reconcile_operation",
            "get_exact_instance",
            "list_exact_label_inventory",
            "delete_exact_instance",
            "get_exact_disk",
            "delete_exact_disk",
            "confirm_absence",
        )
        and getattr(authorization, "request_digest", None) == request_digest
        and getattr(authorization, "startup_projection_digest", None)
        == startup_projection_digest
        and getattr(authorization, "execution_payload_digest", None)
        == execution_payload_digest
        and getattr(authorization, "project_id", None) == project_id
        and getattr(authorization, "region", None) == region
        and getattr(authorization, "zone", None) == zone
        and getattr(authorization, "instance_name", None) == instance_name
        and getattr(authorization, "labels", None) == labels
        and getattr(authorization, "disk_cleanup_binding", None)
        == disk_cleanup_binding
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

    def bind_mutation(
        self, activation_guard: object
    ) -> GcpV2CapabilityBoundTransport:
        """Bind only a concrete-supervisor future-activation guard.

        A post-watchdog proof by itself is intentionally insufficient here:
        it does not establish that this construction edge repeated the
        approval/cost/kill checks with the durable watchdog.  This factory is
        used only by injected local fakes in the current development cycle.
        """

        now = self._now_fn()
        return _bind_gcp_v2_mutation_transport_from_activation_guard(
            transport_supplier=self._lazy_transport,
            activation_guard=activation_guard,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )

    def bind_watchdog_cleanup(
        self, authority: object
    ) -> GcpV2WatchdogCleanupBoundTransport:
        """Bind one watchdog-issued opaque cleanup handoff.

        A bare canonical capability is rejected before this factory can invoke
        its lazy transport supplier.  The private supervisor consumer also
        makes each cleanup purpose one-shot.
        """

        now = self._now_fn()
        try:
            from inferdrome.deployment.gcp_supervisor import (
                _consume_gcp_v2_watchdog_cleanup_handle,
            )

            handle = _consume_gcp_v2_watchdog_cleanup_handle(
                authority, now=now, purpose="transport"
            )
        except GcpExecutionError:
            raise GcpTransportError("WATCHDOG_CLEANUP_HANDLE_INVALID") from None
        except BaseException:
            raise GcpTransportError("WATCHDOG_CLEANUP_HANDLE_INVALID") from None
        return _bind_gcp_v2_watchdog_cleanup_transport_from_capability(
            transport_supplier=self._lazy_transport,
            capability=handle.capability,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )

    def bind_cleanup(
        self, authority: object
    ) -> GcpV2CleanupAuthorizationBoundTransport:
        """Bind one supervisor-issued opaque recovery cleanup handoff."""

        now = self._now_fn()
        try:
            from inferdrome.deployment.gcp_supervisor import (
                _consume_gcp_v2_recovery_cleanup_handle,
            )

            handle = _consume_gcp_v2_recovery_cleanup_handle(
                authority, now=now, purpose="transport"
            )
        except GcpExecutionError:
            raise GcpTransportError("RECOVERY_CLEANUP_HANDLE_INVALID") from None
        except BaseException:
            raise GcpTransportError("RECOVERY_CLEANUP_HANDLE_INVALID") from None
        return _bind_gcp_v2_cleanup_transport_from_authorization(
            transport_supplier=self._lazy_transport,
            authorization=handle.authorization,
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


def _sealed_google_compute_transport_supplier(
    *,
    capability: GcpV2MutationCapability,
    startup_projection: GcpV2StartupProjection,
    now_fn: Callable[[], datetime],
    component_supplier: Callable[
        [],
        tuple[
            Any,
            _InstancesClient,
            _ZoneOperationsClient,
            _ImagesClient,
            _DisksClient,
        ],
    ],
    cleanup_only: bool,
) -> Callable[[], GcpComputeTransport]:
    """Return one inert sealed-GCE supplier for an already-validated authority.

    This helper deliberately receives only injected components.  It never
    imports the optional SDK, creates ADC clients, or accepts an unscoped
    generic transport.  The calling authority adapter decides whether the
    resulting implementation can create; cleanup paths always receive the
    no-create subtype.
    """

    if not callable(component_supplier):
        raise GcpTransportError("SEALED_GCE_COMPONENT_SUPPLIER_INVALID")

    def supplier() -> GcpComputeTransport:
        try:
            components = component_supplier()
        except BaseException:
            raise GcpTransportError("SEALED_GCE_COMPONENT_SUPPLIER_FAILED") from None
        if (
            not isinstance(components, tuple)
            or len(components) != 5
            or any(component is None for component in components)
        ):
            raise GcpTransportError("SEALED_GCE_COMPONENT_SUPPLIER_FAILED")
        transport_type: type[_SealedGoogleComputeTransport]
        transport_type = (
            _SealedGoogleComputeCleanupTransport
            if cleanup_only
            else _SealedGoogleComputeTransport
        )
        return transport_type(
            sdk=components[0],
            client=components[1],
            operations_client=components[2],
            images_client=components[3],
            disks_client=components[4],
            capability=capability,
            startup_projection=startup_projection,
            now_fn=now_fn,
            _seal=_SEALED_GCE_TRANSPORT_SEAL,
        )

    return supplier


def _capability_from_cleanup_recovery_authorization(
    authorization: GcpCleanupRecoveryAuthorization,
) -> GcpV2MutationCapability:
    """Recover only the prior capability facts needed by no-create GCE calls.

    The recovery authorization already binds the exact durable capability and
    has been validated by the supervisor-issued opaque handle before this
    helper is reached.  Reconstructing this typed view does not create a new
    capability or a create edge: callers use it solely with
    ``_SealedGoogleComputeCleanupTransport``, whose ``insert`` is denied.
    """

    try:
        raw = canonical_json_bytes(authorization.model_dump(mode="json"))
        parsed = GcpCleanupRecoveryAuthorization.model_validate_json(raw)
        if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
            raise ValueError("noncanonical cleanup authorization")
        return GcpV2MutationCapability(
            schema_version="inferdrome.gcp-mutation-capability.v2",
            capability_kind="one_exact_future_create",
            activation_contract_digest=parsed.activation_deadline_digest,
            approval_digest=parsed.approval_digest,
            controller_id=parsed.controller_id,
            request_digest=parsed.request_digest,
            startup_projection_digest=parsed.startup_projection_digest,
            execution_payload_digest=parsed.execution_payload_digest,
            project_id=parsed.project_id,
            region=parsed.region,
            zone=parsed.zone,
            instance_name=parsed.instance_name,
            labels=parsed.labels,
            activated_at=parsed.capability_activated_at,
            provider_runtime_deadline_at=parsed.provider_runtime_deadline_at,
            watchdog_cleanup_deadline_at=parsed.watchdog_cleanup_deadline_at,
            capability_id=parsed.mutation_capability_id,
        )
    except (AttributeError, ValidationError, TypeError, ValueError):
        raise GcpTransportError("SEALED_GCE_RECOVERY_AUTHORIZATION_INVALID") from None


def _create_google_compute_transport_for_v2_activation_guard(
    *,
    activation_guard: object,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    component_supplier: Callable[
        [],
        tuple[
            Any,
            _InstancesClient,
            _ZoneOperationsClient,
            _ImagesClient,
            _DisksClient,
        ],
    ]
    | None = None,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
    images_client: _ImagesClient | None = None,
    disks_client: _DisksClient | None = None,
) -> GcpV2CapabilityBoundTransport:
    """Return the internal sealed route after a durable supervisor guard.

    The private guard can only be minted by ``GcpLifecycleSupervisor`` after
    exact approval, read-only quote/cap, kill-switch, and concrete durable
    watchdog checks succeed. Unlike the permanently fail-closed generic
    factory, this narrow route can issue bounded SDK calls through a private
    transport object. It never imports the SDK, discovers ADC, or constructs
    a client. Tests inject all five local fake components. The supplier is not
    called until the consumed guard, request, and opaque projection have all
    revalidated immediately before the first operation.
    """

    # Import only at this explicit future factory edge.  The supervisor module
    # never imports this transport, so this cannot create an SDK/ADC import
    # cycle.  A bare (even self-hashed) capability or post-watchdog proof is
    # deliberately insufficient: the supervisor-issued construction guard
    # proves this exact factory edge repeated the cost/watchdog checks.
    parsed_capability = _consume_gcp_v2_future_activation_guard_for_transport(
        activation_guard, now=now
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
    if component_supplier is not None and any(
        value is not None
        for value in (
            sdk_module,
            client,
            operations_client,
            images_client,
            disks_client,
        )
    ):
        raise GcpTransportError("SEALED_GCE_COMPONENT_SOURCE_AMBIGUOUS")
    if component_supplier is None and any(
        value is None
        for value in (
            sdk_module,
            client,
            operations_client,
            images_client,
            disks_client,
        )
    ):
        raise GcpTransportError("SEALED_GCE_COMPONENTS_REQUIRED")
    if component_supplier is not None and not callable(component_supplier):
        raise GcpTransportError("SEALED_GCE_COMPONENT_SUPPLIER_INVALID")

    if component_supplier is None:

        def source() -> tuple[
            Any,
            _InstancesClient,
            _ZoneOperationsClient,
            _ImagesClient,
            _DisksClient,
        ]:
            return (
                sdk_module,
                cast(_InstancesClient, client),
                cast(_ZoneOperationsClient, operations_client),
                cast(_ImagesClient, images_client),
                cast(_DisksClient, disks_client),
            )

    else:
        source = component_supplier
    effective_now_fn = now_fn if now_fn is not None else lambda: datetime.now(UTC)
    sealed_transport_supplier = _sealed_google_compute_transport_supplier(
        capability=parsed_capability,
        startup_projection=projection,
        now_fn=effective_now_fn,
        component_supplier=source,
        cleanup_only=False,
    )

    return GcpV2CapabilityBoundTransport(
        transport_supplier=sealed_transport_supplier,
        capability=parsed_capability,
        startup_projection=projection,
        now_fn=effective_now_fn,
        _seal=_CAPABILITY_BOUND_TRANSPORT_SEAL,
    )


class GcpV2SealedComputeTransportFactory:
    """Nominal controller factory for the private sealed GCE implementation.

    This is deliberately not a generic client factory or an activation switch.
    It holds only a lazy, injected component supplier and an opaque startup
    projection.  ``bind_mutation`` delegates to the one private route that
    consumes a concrete-supervisor guard; cleanup methods independently
    consume supervisor/watchdog-issued opaque handles and always return
    no-create sealed adapters.  No method imports an SDK or discovers ADC.
    """

    def __init__(
        self,
        *,
        component_supplier: Callable[
            [],
            tuple[
                Any,
                _InstancesClient,
                _ZoneOperationsClient,
                _ImagesClient,
                _DisksClient,
            ],
        ],
        now_fn: Callable[[], datetime],
        startup_projection: GcpV2StartupProjection,
    ) -> None:
        if not callable(component_supplier) or not callable(now_fn):
            raise GcpTransportError("SEALED_GCE_FACTORY_INVALID")
        try:
            raw = canonical_json_bytes(startup_projection.model_dump(mode="json"))
            parsed = GcpV2StartupProjection.model_validate_json(raw)
        except (AttributeError, ValidationError, TypeError, ValueError):
            raise GcpTransportError("SEALED_GCE_FACTORY_PROJECTION_INVALID") from None
        if canonical_json_bytes(parsed.model_dump(mode="json")) != raw:
            raise GcpTransportError("SEALED_GCE_FACTORY_PROJECTION_INVALID")
        self._component_supplier = component_supplier
        self._now_fn = now_fn
        self._startup_projection = _strict_startup_projection_for_authority(
            parsed,
            request_digest=parsed.request_digest,
            startup_projection_digest=gcp_v2_startup_projection_digest(parsed),
            execution_payload_digest=parsed.execution_payload_digest,
            code="SEALED_GCE_FACTORY_PROJECTION_INVALID",
        )

    def bind_mutation(self, activation_guard: object) -> GcpV2CapabilityBoundTransport:
        """Consume only the concrete supervisor's opaque activation guard."""

        return _create_google_compute_transport_for_v2_activation_guard(
            activation_guard=activation_guard,
            startup_projection=self._startup_projection,
            now=self._now_fn(),
            now_fn=self._now_fn,
            component_supplier=self._component_supplier,
        )

    def bind_watchdog_cleanup(
        self, authority: object
    ) -> GcpV2WatchdogCleanupBoundTransport:
        """Mint an exact no-create adapter from one watchdog cleanup handle."""

        now = self._now_fn()
        try:
            from inferdrome.deployment.gcp_supervisor import (
                _consume_gcp_v2_watchdog_cleanup_handle,
            )

            handle = _consume_gcp_v2_watchdog_cleanup_handle(
                authority, now=now, purpose="transport"
            )
        except GcpExecutionError:
            raise GcpTransportError("WATCHDOG_CLEANUP_HANDLE_INVALID") from None
        except BaseException:
            raise GcpTransportError("WATCHDOG_CLEANUP_HANDLE_INVALID") from None
        capability = handle.capability
        return _bind_gcp_v2_watchdog_cleanup_transport_from_capability(
            transport_supplier=_sealed_google_compute_transport_supplier(
                capability=capability,
                startup_projection=self._startup_projection,
                now_fn=self._now_fn,
                component_supplier=self._component_supplier,
                cleanup_only=True,
            ),
            capability=capability,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )

    def bind_cleanup(
        self, authority: object
    ) -> GcpV2CleanupAuthorizationBoundTransport:
        """Mint an exact no-create adapter from one recovery cleanup handle."""

        now = self._now_fn()
        try:
            from inferdrome.deployment.gcp_supervisor import (
                _consume_gcp_v2_recovery_cleanup_handle,
            )

            handle = _consume_gcp_v2_recovery_cleanup_handle(
                authority, now=now, purpose="transport"
            )
        except GcpExecutionError:
            raise GcpTransportError("RECOVERY_CLEANUP_HANDLE_INVALID") from None
        except BaseException:
            raise GcpTransportError("RECOVERY_CLEANUP_HANDLE_INVALID") from None
        authorization = handle.authorization
        capability = _capability_from_cleanup_recovery_authorization(authorization)
        return _bind_gcp_v2_cleanup_transport_from_authorization(
            transport_supplier=_sealed_google_compute_transport_supplier(
                capability=capability,
                startup_projection=self._startup_projection,
                now_fn=self._now_fn,
                component_supplier=self._component_supplier,
                cleanup_only=True,
            ),
            authorization=authorization,
            startup_projection=self._startup_projection,
            now=now,
            now_fn=self._now_fn,
        )


def create_google_compute_transport_for_v2_capability(
    *,
    capability: object,
    startup_projection: object,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    component_supplier: Callable[
        [],
        tuple[
            Any,
            _InstancesClient,
            _ZoneOperationsClient,
            _ImagesClient,
            _DisksClient,
        ],
    ]
    | None = None,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
    images_client: _ImagesClient | None = None,
    disks_client: _DisksClient | None = None,
) -> GcpV2CapabilityBoundTransport:
    """Fail closed for the former direct v2 proof-only factory API.

    The executable route is intentionally internal and accepts only the
    concrete supervisor's opaque activation guard.  Retaining this spelling
    as an explicit failure prevents a future Boolean flip or injected client
    from reviving the old proof-only escape hatch by accident.
    """

    del (
        capability,
        startup_projection,
        now,
        now_fn,
        component_supplier,
        sdk_module,
        client,
        operations_client,
        images_client,
        disks_client,
    )
    raise GcpTransportError("MUTATION_ACTIVATION_GUARD_REQUIRED")
