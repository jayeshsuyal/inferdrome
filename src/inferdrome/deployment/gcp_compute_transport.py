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
from urllib.parse import urlsplit

from inferdrome.deployment.gcp_lifecycle import (
    GcpBootDiskObservation,
    GcpComputeTransport,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpImageObservation,
    GcpInsertRequest,
    GcpInstanceObservation,
    GcpOperationHandle,
    GcpOperationResult,
    GcpTransportError,
    validate_gcp_a2_profile,
)

_GOOGLE_SCOPE_URLS = {
    "logging.write": "https://www.googleapis.com/auth/logging.write",
    "monitoring.write": "https://www.googleapis.com/auth/monitoring.write",
    "trace.append": "https://www.googleapis.com/auth/trace.append",
}
_SUPPORTED_COMPUTE_HOSTS = frozenset({"www.googleapis.com", "compute.googleapis.com"})
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")


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
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
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
                    raise GcpTransportError("LIST_OWNED_LIMIT")
                output.append(_observation(value, request))
            return tuple(output)
        except GcpTransportError:
            raise
        except Exception:
            raise GcpTransportError("LIST_OWNED_FAILED") from None

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
                        source_image=request.boot_image.image_name,
                        disk_size_gb=request.boot_disk_size_gib,
                        disk_type=(
                            f"zones/{request.zone}/diskTypes/{request.boot_disk_type}"
                        ),
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


def create_google_compute_transport(
    *,
    sdk_module: Any | None = None,
    client: _InstancesClient | None = None,
    operations_client: _ZoneOperationsClient | None = None,
    images_client: _ImagesClient | None = None,
    disks_client: _DisksClient | None = None,
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
