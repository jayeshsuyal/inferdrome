"""Optional Google Compute Engine transport for the guarded controller.

Nothing in this module imports the Google SDK or discovers ADC at import time.
``create_google_compute_transport`` is the explicit live boundary and is only
callable by an operator after the pure arm/plan/request/quote/journal gates have
passed.  Unit tests inject a fake SDK module and clients; no test constructs ADC
or contacts Google.
"""

from __future__ import annotations

import importlib
import threading
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
        project: str,
        zone: str,
        instance: object,
        request_id: str,
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
        project: str,
        zone: str,
        instance: str,
        request_id: str,
        timeout: int,
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
        status = cast(Literal["RUNNING", "TERMINATED"], str(value.status))
        labels = _safe_labels(getattr(value, "labels", None))
        if status not in {"RUNNING", "TERMINATED"}:
            raise GcpTransportError("INSTANCE_STATE_UNSUPPORTED")
        return GcpInstanceObservation(
            instance_name=name,
            project_id=request.project_id,
            zone=request.zone,
            machine_type=request.machine_type,
            accelerator_model=request.accelerator_model,
            accelerator_count=request.accelerator_count,
            state=status,
            labels=labels,
            external_access_config="absent",
            ip_forwarding=False,
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

    def __init__(self, *, sdk: Any, client: _InstancesClient) -> None:
        self._sdk = sdk
        self._client = client
        self._lock = threading.Lock()
        self._operations: dict[str, object] = {}
        self._sequence = 0

    def _remember(self, operation: object, kind: str) -> GcpOperationHandle:
        self._sequence += 1
        operation_id = f"op-{self._sequence:08d}"
        with self._lock:
            self._operations[operation_id] = operation
        return GcpOperationHandle(operation_id=operation_id, operation_kind=kind)  # type: ignore[arg-type]

    def insert(
        self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
    ) -> GcpOperationHandle:
        try:
            instance = self._project_instance(request)
            operation = self._client.insert(
                project=request.project_id,
                zone=request.zone,
                instance=instance,
                request_id=request_id,
                timeout=timeout_seconds,
            )
            return self._remember(operation, "insert")
        except GcpTransportError:
            raise
        except Exception as error:
            raise _sanitize_exception(error, "INSERT_REQUEST_FAILED") from None

    def wait_operation(
        self, operation: GcpOperationHandle, *, timeout_seconds: int
    ) -> GcpOperationResult:
        with self._lock:
            provider_operation = self._operations.pop(operation.operation_id, None)
        if provider_operation is None:
            raise GcpTransportError("OPERATION_UNKNOWN")
        try:
            provider_operation.result(timeout=timeout_seconds)  # type: ignore[attr-defined]
        except TimeoutError:
            return GcpOperationResult(
                status="TIMEOUT",
                operation_id=operation.operation_id,
                instance_name=None,
            )
        except Exception:
            return GcpOperationResult(
                status="ERROR",
                operation_id=operation.operation_id,
                instance_name=None,
                error_code="PROVIDER_OPERATION_FAILED",
            )
        return GcpOperationResult(
            status="DONE",
            operation_id=operation.operation_id,
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
            operation = self._client.delete(
                project=request.project_id,
                zone=request.zone,
                instance=request.instance_name,
                request_id=request_id,
                timeout=timeout_seconds,
            )
            return self._remember(operation, "delete")
        except Exception:
            raise GcpTransportError("DELETE_REQUEST_FAILED") from None

    def _project_instance(self, request: GcpInsertRequest) -> object:
        accelerator_type = "nvidia-tesla-a100"
        return self._sdk.Instance(
            name=request.instance_name,
            machine_type=(f"zones/{request.zone}/machineTypes/{request.machine_type}"),
            disks=[
                self._sdk.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    initialize_params=self._sdk.DiskInitializeParams(
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
                        f"zones/{request.zone}/acceleratorTypes/{accelerator_type}"
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
) -> GoogleComputeTransport:
    """Explicitly select the live transport; import SDK/ADC only here."""

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
    return GoogleComputeTransport(sdk=sdk_module, client=client)
