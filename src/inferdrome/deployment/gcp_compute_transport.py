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
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, cast

from inferdrome.deployment.gcp_lifecycle import (
    GcpComputeTransport,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpInsertRequest,
    GcpInstanceObservation,
    GcpOperationHandle,
    GcpOperationResult,
    GcpTransportError,
)

_GOOGLE_SCOPE_URLS = {
    "logging.write": "https://www.googleapis.com/auth/logging.write",
    "monitoring.write": "https://www.googleapis.com/auth/monitoring.write",
    "trace.append": "https://www.googleapis.com/auth/trace.append",
}


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

    def list(
        self, *, project: str, zone: str, filter: str, timeout: int
    ) -> Sequence[Any]: ...

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


def _observation(value: Any, request: GcpInsertRequest) -> GcpInstanceObservation:
    try:
        name = str(value.name)
        if name != request.instance_name:
            raise GcpTransportError("INSTANCE_IDENTITY_MISMATCH")
        self_link = str(getattr(value, "self_link", ""))
        self_link_parts = self_link.split("/")
        if (
            len(self_link_parts) != 11
            or self_link_parts[0] != "https:"
            or not self_link_parts[2].endswith(".googleapis.com")
            or self_link_parts[3:6] != ["compute", "v1", "projects"]
            or self_link_parts[6] != request.project_id
            or self_link_parts[7] != "zones"
            or self_link_parts[8] != request.zone
            or self_link_parts[9] != "instances"
            or self_link_parts[10] != request.instance_name
        ):
            raise GcpTransportError("INSTANCE_PROJECT_MISMATCH")
        status = cast(Literal["RUNNING", "TERMINATED"], str(value.status))
        labels = _safe_labels(getattr(value, "labels", None))
        if labels != request.labels:
            raise GcpTransportError("INSTANCE_OWNERSHIP_LABEL_MISMATCH")
        if status not in {"RUNNING", "TERMINATED"}:
            raise GcpTransportError("INSTANCE_STATE_UNSUPPORTED")
        zone_ref = str(getattr(value, "zone", ""))
        if "/zones/" not in zone_ref:
            raise GcpTransportError("INSTANCE_ZONE_MALFORMED")
        zone = zone_ref.rsplit("/zones/", 1)[-1]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", zone):
            raise GcpTransportError("INSTANCE_ZONE_MALFORMED")
        if zone != request.zone:
            raise GcpTransportError("INSTANCE_ZONE_MISMATCH")
        machine_ref = str(getattr(value, "machine_type", ""))
        if "/machineTypes/" not in machine_ref:
            raise GcpTransportError("INSTANCE_MACHINE_TYPE_MALFORMED")
        machine_type = machine_ref.rsplit("/machineTypes/", 1)[-1]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", machine_type):
            raise GcpTransportError("INSTANCE_MACHINE_TYPE_MALFORMED")
        if machine_type != request.machine_type:
            raise GcpTransportError("INSTANCE_MACHINE_TYPE_MISMATCH")
        accelerators = getattr(value, "guest_accelerators", None)
        try:
            accelerator_values = list(accelerators or ())
        except TypeError:
            accelerator_values = []
        if len(accelerator_values) != 1:
            raise GcpTransportError("INSTANCE_ACCELERATOR_MISMATCH")
        accelerator = accelerator_values[0]
        accelerator_ref = str(getattr(accelerator, "accelerator_type", ""))
        if "/acceleratorTypes/" not in accelerator_ref:
            raise GcpTransportError("INSTANCE_ACCELERATOR_MALFORMED")
        accelerator_type = accelerator_ref.rsplit("/acceleratorTypes/", 1)[-1]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", accelerator_type):
            raise GcpTransportError("INSTANCE_ACCELERATOR_MALFORMED")
        accelerator_count = int(getattr(accelerator, "accelerator_count", 0))
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
        if len(interface_values) < 1:
            raise GcpTransportError("INSTANCE_NETWORK_INVALID")
        private_ips: list[str] = []
        for interface in interface_values:
            if (
                str(getattr(interface, "network", "")) != request.network.network
                or str(getattr(interface, "subnetwork", ""))
                != request.network.subnetwork
            ):
                raise GcpTransportError("INSTANCE_NETWORK_MISMATCH")
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
            project_id=self_link_parts[self_link_parts.index("projects") + 1],
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


class GoogleComputeTransport(GcpComputeTransport):
    """Thin projection over an already constructed official SDK client."""

    def __init__(
        self,
        *,
        sdk: Any,
        client: _InstancesClient,
        operations_client: _ZoneOperationsClient | None = None,
    ) -> None:
        self._sdk = sdk
        self._client = client
        self._operations_client = operations_client
        self._lock = threading.Lock()
        self._operations: dict[str, object] = {}
        self._sequence = 0

    def _remember(
        self, operation: object, kind: str, request: GcpInsertRequest
    ) -> GcpOperationHandle:
        name = getattr(operation, "name", None)
        if not isinstance(name, str) or not name:
            raise GcpTransportError("OPERATION_IDENTITY_MISSING", ambiguous=True)
        operation_id = "op-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:40]
        with self._lock:
            self._operations[operation_id] = operation
        return GcpOperationHandle(
            operation_id=operation_id,
            operation_kind=cast(Literal["insert", "delete"], kind),
            operation_name=name,
            project_id=request.project_id,
            zone=request.zone,
        )

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        try:
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
        try:
            operation = self._client.insert(
                request=insert_request,
                timeout=timeout_seconds,
            )
            return self._remember(operation, "insert", request)
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("INSERT_AMBIGUOUS", ambiguous=True) from None

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
                raise GcpTransportError("OPERATION_UNKNOWN")
            try:
                operation_name = operation.operation_name.rsplit("/", 1)[-1]
                provider_operation = self._operations_client.get(
                    project=operation.project_id,
                    zone=operation.zone,
                    operation=operation_name,
                    timeout=timeout_seconds,
                )
            except Exception:
                raise GcpTransportError("OPERATION_RECONCILIATION_FAILED") from None
        if provider_operation is None:
            raise GcpTransportError("OPERATION_UNKNOWN")
        try:
            result_method = getattr(provider_operation, "result", None)
            if callable(result_method):
                result_method(timeout=timeout_seconds)
            else:
                provider_status = str(getattr(provider_operation, "status", ""))
                if not provider_status.endswith("DONE"):
                    return GcpOperationResult(
                        status="TIMEOUT",
                        operation_id=operation.operation_id,
                        operation_name=operation.operation_name,
                        instance_name=None,
                    )
                provider_error = getattr(provider_operation, "error", None)
                if provider_error:
                    return GcpOperationResult(
                        status="ERROR",
                        operation_id=operation.operation_id,
                        operation_name=operation.operation_name,
                        instance_name=None,
                        error_code="PROVIDER_OPERATION_FAILED",
                    )
        except TimeoutError:
            return GcpOperationResult(
                status="TIMEOUT",
                operation_id=operation.operation_id,
                operation_name=operation.operation_name,
                instance_name=None,
            )
        except Exception as error:
            if type(error).__name__ in {
                "DeadlineExceeded",
                "OperationTimedOut",
                "Timeout",
            }:
                return GcpOperationResult(
                    status="TIMEOUT",
                    operation_id=operation.operation_id,
                    operation_name=operation.operation_name,
                    instance_name=None,
                )
            return GcpOperationResult(
                status="ERROR",
                operation_id=operation.operation_id,
                operation_name=operation.operation_name,
                instance_name=None,
                error_code="PROVIDER_OPERATION_FAILED",
            )
        return GcpOperationResult(
            status="DONE",
            operation_id=operation.operation_id,
            operation_name=operation.operation_name,
            instance_name=None,
        )

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
        try:
            values = self._client.list(
                project=request.project_id,
                zone=request.zone,
                filter=(
                    "labels.inferdrome = inferdrome AND "
                    f"labels.controller_id = {request.labels.controller_id} AND "
                    f"labels.arm_id = {request.labels.arm_id}"
                ),
                timeout=timeout_seconds,
            )
            return tuple(_observation(value, request) for value in values)
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("LIST_OWNED_FAILED") from None

    def delete(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
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
        try:
            operation = self._client.delete(
                request=delete_request,
                timeout=timeout_seconds,
            )
            return self._remember(operation, "delete", request)
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("DELETE_AMBIGUOUS", ambiguous=True) from None

    def _project_instance(self, request: GcpInsertRequest) -> object:
        return self._sdk.Instance(
            name=request.instance_name,
            machine_type=(f"zones/{request.zone}/machineTypes/{request.machine_type}"),
            disks=[
                self._sdk.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    initialize_params=self._sdk.AttachedDiskInitializeParams(
                        # Numeric Compute image resource IDs are the provider-side
                        # immutable selector; the accompanying SHA-256 remains an
                        # Inferdrome provenance binding in the validated request.
                        source_image=request.boot_image.repository,
                        disk_size_gb=request.boot_disk_size_gib,
                        disk_type=(
                            f"zones/{request.zone}/diskTypes/{request.boot_disk_type}"
                        ),
                    ),
                )
            ],
            guest_accelerators=[
                self._sdk.AcceleratorConfig(
                    accelerator_type=(
                        f"zones/{request.zone}/acceleratorTypes/{request.accelerator_provider_type}"
                    ),
                    accelerator_count=request.accelerator_count,
                )
            ],
            network_interfaces=[
                self._sdk.NetworkInterface(
                    network=request.network.network,
                    subnetwork=request.network.subnetwork,
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


def create_google_compute_transport(
    *,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
) -> GoogleComputeTransport:
    """Explicitly select the live transport; import SDK/ADC only here."""

    client_was_injected = client is not None
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
    if operations_client is None and not client_was_injected:
        try:
            operations_factory = sdk_module.ZoneOperationsClient
            operations_client = operations_factory()
        except AttributeError:
            operations_client = None
        except Exception:
            raise GcpTransportError(
                "ADC_OPERATION_CLIENT_CONSTRUCTION_FAILED"
            ) from None
    return GoogleComputeTransport(
        sdk=sdk_module, client=client, operations_client=operations_client
    )
