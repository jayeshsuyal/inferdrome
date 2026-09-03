"""Lazy official-GCE adapter for the exact two-engine pre-campaign profile.

Importing this module is inert.  The official Google SDK is imported only by
``create_google_private_campaign_transport``; the lifecycle controller calls
that factory only after exact approval validation and a durable DELETE/max-run
backstop.  Normal tests inject local fakes and never call the factory.
"""

from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import ValidationError

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_VERSION,
    GcpPrivateCampaignAbsenceObservation,
    GcpPrivateCampaignCreateRequest,
    GcpPrivateCampaignDiskObservation,
    GcpPrivateCampaignEndpointReadiness,
    GcpPrivateCampaignEngine,
    GcpPrivateCampaignEngineAttestation,
    GcpPrivateCampaignError,
    GcpPrivateCampaignEvidenceReceipt,
    GcpPrivateCampaignExactResourceBinding,
    GcpPrivateCampaignHandoffReceipt,
    GcpPrivateCampaignInstanceObservation,
    GcpPrivateCampaignOperation,
    GcpPrivateCampaignOperationResult,
    GcpPrivateCampaignOwnedResidualInventory,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignReadiness,
    GcpPrivateCampaignTransport,
    GcpPrivateCampaignTransportError,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.executor import run_execution
from inferdrome.routing_execution.verifier import verify_execution_package

_GOOGLE_SCOPE_URLS: Final = {
    "logging.write": "https://www.googleapis.com/auth/logging.write",
    "monitoring.write": "https://www.googleapis.com/auth/monitoring.write",
}
_GAUGE_RE: Final = re.compile(
    r"^vllm:num_requests_running\{(?P<labels>[^}]*)\}\s+(?P<value>[^\s#]+)(?:\s+[^\s#]+)?\s*$"
)
_LABEL_RE: Final = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_EXPECTED_MODEL_ID: Final = "Qwen/Qwen3-8B"
_A2_HIGHGPU_2G_ACCELERATOR_PROFILE: Final = (
    "NVIDIA A100-SXM4-40GB",
    "nvidia-tesla-a100",
    2,
)
_A2_HIGHGPU_2G_MACHINE_FIXED_LOCAL_SSD_COUNT: Final = 2
_MAX_HTTP_BODY_BYTES: Final = 524_288
_GENERATION_PROBE: Final = (
    b'{"max_tokens":1,"messages":[{"content":"readiness-probe","role":"user"}],'
    b'"model":"Qwen/Qwen3-8B","stream":false}'
)
_COMPUTE_API_HOSTS: Final = frozenset({"www.googleapis.com", "compute.googleapis.com"})
_STARTUP_SCRIPT_METADATA_KEY: Final = "startup-script"
_STARTUP_SCRIPT_DIGEST_METADATA_KEY: Final = "inferdrome-startup-script-sha256"
_STARTUP_PAYLOAD_METADATA_KEY: Final = "inferdrome-startup-payload-digest"
_PROPOSAL_METADATA_KEY: Final = "inferdrome-proposal-digest"


class GcpPrivateCampaignOptionalDependencyUnavailable(GcpPrivateCampaignError):
    """The separately-installed GCP SDK is absent at the explicit live edge."""

    def __init__(self) -> None:
        super().__init__("GCP_OPTIONAL_DEPENDENCY_UNAVAILABLE")


class _InstancesClient(Protocol):
    def insert(self, *, request: object, timeout: int) -> object: ...

    def get(
        self, *, project: str, zone: str, instance: str, timeout: int
    ) -> object: ...

    def delete(self, *, request: object, timeout: int) -> object: ...

    def list(self, *, request: object, timeout: int) -> Sequence[Any]: ...


class _DisksClient(Protocol):
    def get(self, *, request: object, timeout: int) -> object: ...

    def delete(self, *, request: object, timeout: int) -> object: ...

    def list(self, *, request: object, timeout: int) -> Sequence[Any]: ...


class _ImagesClient(Protocol):
    def get(self, *, project: str, image: str, timeout: int) -> object: ...


@dataclass(frozen=True)
class _CompletedProviderOperation:
    """Local acknowledgement for a readback-proven already-absent resource."""

    name: str


class _NoRedirect(HTTPRedirectHandler):
    """Make every 3xx readiness response fail closed at the private origin."""

    def redirect_request(
        self,
        request: Request,
        file: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file, code, message, headers, new_url
        return None


def _positive_provider_id(value: object, *, code: str) -> int:
    if isinstance(value, bool):
        raise GcpPrivateCampaignTransportError(code)
    text = str(value)
    if re.fullmatch(r"[1-9][0-9]{0,18}", text) is None:
        raise GcpPrivateCampaignTransportError(code)
    return int(text)


def _canonical_compute_reference(value: object, *, expected: str, code: str) -> str:
    """Accept only the exact relative resource or an exact official self link."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise GcpPrivateCampaignTransportError(code)
    if value == expected:
        return expected
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise GcpPrivateCampaignTransportError(code) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _COMPUTE_API_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/compute/v1/{expected}"
    ):
        raise GcpPrivateCampaignTransportError(code)
    return expected


def _rfc1918(value: object) -> str:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        raise GcpPrivateCampaignTransportError("PRIVATE_ADDRESS_INVALID") from None
    networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in networks
    ):
        raise GcpPrivateCampaignTransportError("PRIVATE_ADDRESS_INVALID")
    return str(address)


def _operation_id(kind: str, name: str) -> str:
    if not name or len(name) > 256:
        raise GcpPrivateCampaignTransportError(
            "OPERATION_IDENTITY_MISSING", ambiguous=True
        )
    return "pcop-" + hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()[:32]


def _operation(
    provider_operation: object,
    *,
    kind: Literal["create", "delete_instance", "delete_boot_disk"],
    request_id: str,
) -> GcpPrivateCampaignOperation:
    name = getattr(provider_operation, "name", None)
    if not isinstance(name, str) or not name:
        raise GcpPrivateCampaignTransportError(
            "OPERATION_IDENTITY_MISSING", ambiguous=True
        )
    return GcpPrivateCampaignOperation(
        operation_id=_operation_id(kind, name),
        operation_kind=kind,
        request_id=request_id,
        provider_operation_name=name,
    )


def _metric_is_present(metrics: bytes) -> None:
    """Require exactly one finite integral target-model vLLM gauge.

    Unrelated duplicate histogram families are intentionally ignored.  The
    target gauge is not aggregated, and duplicate/wrong-model target samples
    fail closed.
    """

    try:
        text = metrics.decode("utf-8")
    except UnicodeDecodeError:
        raise GcpPrivateCampaignTransportError("METRICS_MALFORMED") from None
    target_values: list[int] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("vllm:num_requests_running{") or stripped.startswith(
            "vllm:num_requests_running "
        ):
            matched = _GAUGE_RE.fullmatch(stripped)
            if matched is None:
                raise GcpPrivateCampaignTransportError("METRICS_TARGET_MALFORMED")
        else:
            matched = _GAUGE_RE.fullmatch(stripped)
        if matched is None:
            continue
        labels: dict[str, str] = {}
        position = 0
        raw_labels = matched.group("labels")
        while position < len(raw_labels):
            label = _LABEL_RE.match(raw_labels, position)
            if label is None:
                raise GcpPrivateCampaignTransportError("METRICS_MALFORMED")
            key, value = label.groups()
            if key in labels:
                raise GcpPrivateCampaignTransportError("METRICS_MALFORMED")
            labels[key] = bytes(value, "utf-8").decode("unicode_escape")
            position = label.end()
            if position == len(raw_labels):
                break
            if raw_labels[position] != ",":
                raise GcpPrivateCampaignTransportError("METRICS_MALFORMED")
            position += 1
        if labels.get("model_name") != _EXPECTED_MODEL_ID:
            raise GcpPrivateCampaignTransportError("METRICS_TARGET_MODEL_MISMATCH")
        raw_value = matched.group("value")
        try:
            number = Decimal(raw_value)
        except InvalidOperation:
            raise GcpPrivateCampaignTransportError("METRICS_TARGET_INVALID") from None
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise GcpPrivateCampaignTransportError("METRICS_TARGET_INVALID")
        target_values.append(int(number))
    if len(target_values) != 1:
        raise GcpPrivateCampaignTransportError("METRICS_TARGET_AMBIGUOUS")


def render_gcp_private_campaign_startup_script(
    request: GcpPrivateCampaignCreateRequest,
) -> str:
    """Render the reviewed semantic payload to a credential-free startup script.

    The script contains only immutable image/model identities and private
    serving flags.  It has no prompt, output, bucket URL, auth header, SSH
    material, or mutable image tag.  The provider request records only the
    script itself and its separately bound semantic payload digest.
    """

    engines = request.startup_payload.engines
    lines = ["#!/bin/sh", "set -eu", "umask 077"]
    for engine in engines:
        model = engine.model
        lines.extend(
            (
                f"docker pull {engine.serving_image.reference}",
                "docker run --detach --rm "
                f"--name {engine.container_name} "
                f"--gpus device={engine.gpu_ordinal} --network host "
                f"{engine.serving_image.reference} "
                f"vllm serve {model.model_id} "
                f"--revision {model.model_revision} "
                f"--tokenizer {model.model_id} "
                f"--tokenizer-revision {model.tokenizer_revision} "
                f"--host 0.0.0.0 --port {engine.private_port} "
                "--disable-log-requests",
            )
        )
    return "\n".join(lines) + "\n"


class LocalGcpPrivateCampaignRunner:
    """Bounded local PR-B runner/evidence adapter for a private reachable VM.

    It is useful when the controller runs from the same private VPC or another
    approved private path.  It performs the existing PR-B run and offline
    verifier, creates evidence once, and retains no input staging directory.
    It never makes a provider call.
    """

    def __init__(self, evidence_root: Path) -> None:
        self._evidence_root = evidence_root.absolute()
        self._sealed: dict[str, tuple[Path, str]] = {}
        self._bound_root: SafeDirFD | None = None

    def bind_evidence_root(self, root: SafeDirFD) -> None:
        """Retain the approval-preflight root by descriptor, never pathname."""

        if self._bound_root is not None or root.path.absolute() != self._evidence_root:
            raise GcpPrivateCampaignTransportError("EVIDENCE_ROOT_BINDING_MISMATCH")
        try:
            root.assert_open()
            bound = SafeDirFD.from_inherited_fd(root.fd)
            # ``from_inherited_fd`` deliberately uses a diagnostic path.  This
            # runner needs the approved lexical path only for run_execution's
            # consistency check; every filesystem mutation still uses the fd.
            bound.path = self._evidence_root
            self._bound_root = bound
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignTransportError(
                "EVIDENCE_ROOT_BINDING_UNAVAILABLE"
            ) from None

    def _release_bound_root(self) -> None:
        if self._bound_root is not None:
            self._bound_root.close()
            self._bound_root = None

    def _root(self) -> SafeDirFD:
        try:
            if self._bound_root is None:
                raise SafeDirFSError("evidence root was not retained at preflight")
            self._bound_root.assert_open()
            root = SafeDirFD.from_inherited_fd(self._bound_root.fd)
            root.path = self._evidence_root
            return root
        except (SafeDirFSError, OSError):
            raise GcpPrivateCampaignTransportError("EVIDENCE_ROOT_UNSAFE") from None

    @staticmethod
    def _write_stage_file(root: SafeDirFD, name: str, content: bytes) -> None:
        descriptor: int | None = None
        try:
            descriptor = root.open_child(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short staged input write")
                view = view[written:]
            os.fsync(descriptor)
            root.validated_regular_child(name, descriptor=descriptor)
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignTransportError(
                "ROUTING_INPUT_STAGE_UNSAFE"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _assert_visible_root(self, held: SafeDirFD) -> None:
        """Reject a same-UID evidence-root replacement before issuing a receipt."""

        visible: SafeDirFD | None = None
        try:
            held.assert_open()
            visible = SafeDirFD.open(self._evidence_root)
            if (visible.device, visible.inode) != (held.device, held.inode):
                raise GcpPrivateCampaignTransportError("EVIDENCE_ROOT_CHANGED")
        finally:
            if visible is not None:
                visible.close()

    def handoff(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        routing_config: bytes,
        workload: bytes,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignHandoffReceipt:
        del timeout_seconds
        expected_root_identity = sha256_digest(str(self._evidence_root).encode("utf-8"))
        if (
            expected_root_identity
            != request.proposal.routing.evidence_destination_sha256
        ):
            raise GcpPrivateCampaignTransportError("EVIDENCE_DESTINATION_MISMATCH")
        root: SafeDirFD | None = None
        handoff_succeeded = False
        final = (
            self._evidence_root
            / f"routing-execution-{request.proposal.proposal_id[7:39]}"
        )
        try:
            root = self._root()
            self._assert_visible_root(root)
            try:
                os.stat(final.name, dir_fd=root.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise GcpPrivateCampaignTransportError("EVIDENCE_CREATE_NO_REPLACE")
            with tempfile.TemporaryDirectory(
                prefix="inferdrome-gcp-private-input-"
            ) as stage:
                stage_root = Path(stage)
                stage_fd = SafeDirFD.open(stage_root)
                try:
                    self._write_stage_file(
                        stage_fd, "deployment-config.json", routing_config
                    )
                    self._write_stage_file(stage_fd, "workload.jsonl", workload)
                    stage_fd.assert_open()
                    sealed = run_execution(
                        stage_root / "deployment-config.json",
                        stage_root / "workload.jsonl",
                        final,
                        evidence_parent=root,
                    )
                    stage_fd.assert_open()
                finally:
                    stage_fd.close()
            self._assert_visible_root(root)
            verified = verify_execution_package(sealed.path)
            self._assert_visible_root(root)
            if (
                verified.executed_manifest.config_sha256
                != sha256_digest(routing_config)
                or verified.executed_manifest.mode != "GCP_PRIVATE"
            ):
                raise GcpPrivateCampaignTransportError(
                    "ROUTING_HANDOFF_RECEIPT_INVALID"
                )
            endpoints = {
                item.endpoint_id: item.origin_sha256
                for item in verified.executed_manifest.endpoints
            }
            if set(endpoints) != {"endpoint-a", "endpoint-b"}:
                raise GcpPrivateCampaignTransportError(
                    "ROUTING_HANDOFF_RECEIPT_INVALID"
                )
            self._sealed[request.proposal.proposal_id] = (
                sealed.path,
                sealed.retained_digest,
            )
            handoff_succeeded = True
            return GcpPrivateCampaignHandoffReceipt(
                proposal_id=request.proposal.proposal_id,
                routing_config_sha256=verified.executed_manifest.config_sha256,
                selected_workload_sha256=(
                    request.proposal.routing.selected_workload_sha256
                ),
                endpoint_a_origin_sha256=endpoints["endpoint-a"],
                endpoint_b_origin_sha256=endpoints["endpoint-b"],
                routed_after_verified_readiness=True,
            )
        except GcpPrivateCampaignError:
            raise
        except (OSError, SafeDirFSError, ValueError):
            raise GcpPrivateCampaignTransportError("ROUTING_HANDOFF_FAILED") from None
        finally:
            if root is not None:
                root.close()
            if not handoff_succeeded:
                self._release_bound_root()

    def retrieve(
        self, request: GcpPrivateCampaignCreateRequest
    ) -> GcpPrivateCampaignEvidenceReceipt:
        root: SafeDirFD | None = None
        try:
            root = self._root()
            self._assert_visible_root(root)
            path, digest = self._sealed[request.proposal.proposal_id]
            verified = verify_execution_package(path, expected_digest=digest)
            self._assert_visible_root(root)
        except (KeyError, OSError, ValueError):
            raise GcpPrivateCampaignTransportError(
                "EVIDENCE_RETRIEVAL_FAILED"
            ) from None
        finally:
            if root is not None:
                root.close()
            self._release_bound_root()
        return GcpPrivateCampaignEvidenceReceipt(
            proposal_id=request.proposal.proposal_id,
            retained_digest=verified.report.retained_digest,
            collection_mode="CREATE_NO_REPLACE_RETRIEVAL",
            raw_prompt_or_output_retained=False,
        )


class GoogleGcpPrivateCampaignTransport(GcpPrivateCampaignTransport):
    """Exact projection over already-initialized official Compute SDK clients."""

    def __init__(
        self,
        *,
        sdk: Any,
        instances_client: _InstancesClient,
        disks_client: _DisksClient,
        images_client: _ImagesClient,
        runner: LocalGcpPrivateCampaignRunner,
    ) -> None:
        self._sdk = sdk
        self._instances = instances_client
        self._disks = disks_client
        self._images = images_client
        self._runner = runner
        self._operations: dict[str, object] = {}
        self._lock = threading.Lock()

    def bind_evidence_root(self, root: SafeDirFD) -> None:
        """Bind the controller-held preflight root before any create request."""

        self._runner.bind_evidence_root(root)

    def project_create_request(
        self, request: GcpPrivateCampaignCreateRequest, *, request_id: str
    ) -> object:
        proposal = request.proposal
        if request_id != proposal.request_ids.create_request_id:
            raise GcpPrivateCampaignTransportError("REQUEST_ID_MISMATCH")
        try:
            script = render_gcp_private_campaign_startup_script(request)
            script_digest = sha256_digest(script.encode("utf-8"))
            instance = self._sdk.Instance(
                name=proposal.instance_name,
                machine_type=(
                    f"zones/{proposal.topology.zone}/machineTypes/a2-highgpu-2g"
                ),
                disks=[
                    self._sdk.AttachedDisk(
                        boot=True,
                        auto_delete=True,
                        initialize_params=self._sdk.AttachedDiskInitializeParams(
                            source_image=proposal.boot_image.image_ref,
                            disk_name=proposal.boot_disk_name,
                            labels=proposal.ownership_labels.model_dump(mode="json"),
                        ),
                    )
                ],
                network_interfaces=[
                    self._sdk.NetworkInterface(
                        network=proposal.topology.private_network,
                        subnetwork=proposal.topology.private_subnetwork,
                        stack_type="IPV4_ONLY",
                    )
                ],
                can_ip_forward=False,
                deletion_protection=False,
                scheduling=self._sdk.Scheduling(
                    automatic_restart=False,
                    on_host_maintenance="TERMINATE",
                    max_run_duration={"seconds": str(proposal.max_runtime_seconds)},
                    instance_termination_action="DELETE",
                ),
                labels=proposal.ownership_labels.model_dump(mode="json"),
                metadata=self._sdk.Metadata(
                    items=[
                        self._sdk.Items(key=_STARTUP_SCRIPT_METADATA_KEY, value=script),
                        self._sdk.Items(
                            key=_STARTUP_SCRIPT_DIGEST_METADATA_KEY,
                            value=script_digest,
                        ),
                        self._sdk.Items(
                            key=_STARTUP_PAYLOAD_METADATA_KEY,
                            value=proposal.startup_payload_digest,
                        ),
                        self._sdk.Items(
                            key=_PROPOSAL_METADATA_KEY, value=proposal.proposal_id
                        ),
                    ]
                ),
            )
            return self._sdk.InsertInstanceRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                instance_resource=instance,
                request_id=request_id,
            )
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "CREATE_PROJECTION_INVALID"
            ) from None

    def create_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation:
        self._verify_boot_image_identity(request, timeout_seconds=timeout_seconds)
        projected = self.project_create_request(request, request_id=request_id)
        try:
            result = self._instances.insert(request=projected, timeout=timeout_seconds)
            operation = _operation(result, kind="create", request_id=request_id)
            with self._lock:
                self._operations[operation.operation_id] = result
            return operation
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "CREATE_PROVIDER_FAILED", ambiguous=True
            ) from None

    def _verify_boot_image_identity(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> None:
        """Read the approved image identity before the only create mutation."""

        proposal = request.proposal
        parts = proposal.boot_image.image_ref.split("/")
        if (
            len(parts) != 5
            or parts[0] != "projects"
            or parts[2:4]
            != [
                "global",
                "images",
            ]
        ):
            raise GcpPrivateCampaignTransportError("BOOT_IMAGE_REFERENCE_INVALID")
        try:
            image = self._images.get(
                project=parts[1], image=parts[4], timeout=timeout_seconds
            )
            if (
                _positive_provider_id(
                    getattr(image, "id", None), code="BOOT_IMAGE_IDENTITY_MISMATCH"
                )
                != proposal.boot_image.provider_image_id
                or str(getattr(image, "status", "")) != "READY"
            ):
                raise GcpPrivateCampaignTransportError("BOOT_IMAGE_IDENTITY_MISMATCH")
            _canonical_compute_reference(
                getattr(image, "self_link", None),
                expected=proposal.boot_image.image_ref,
                code="BOOT_IMAGE_IDENTITY_MISMATCH",
            )
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "BOOT_IMAGE_OBSERVATION_FAILED"
            ) from None

    def _provider_operation(self, operation: GcpPrivateCampaignOperation) -> object:
        with self._lock:
            value = self._operations.get(operation.operation_id)
        if value is None:
            raise GcpPrivateCampaignTransportError(
                "OPERATION_RECONCILIATION_UNAVAILABLE", ambiguous=True
            )
        return value

    def wait_operation(
        self, operation: GcpPrivateCampaignOperation, *, timeout_seconds: int
    ) -> GcpPrivateCampaignOperationResult:
        provider_operation = self._provider_operation(operation)
        try:
            waiter = getattr(provider_operation, "result", None)
            if callable(waiter):
                waiter(timeout=timeout_seconds)
            error = getattr(provider_operation, "error", None)
            if error not in (None, "", (), [], {}):
                return GcpPrivateCampaignOperationResult(
                    operation=operation,
                    status="ERROR",
                    error_code="PROVIDER_OPERATION_FAILED",
                )
            return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")
        except TimeoutError:
            return GcpPrivateCampaignOperationResult(
                operation=operation, status="TIMEOUT"
            )
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "OPERATION_WAIT_FAILED", ambiguous=True
            ) from None

    def reconcile_create(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult:
        # Compute's caller request UUID is the exact idempotency/reconciliation
        # key. Re-projecting the same request can return the original operation
        # without selecting a resource by name, IP, or broad inventory diff.
        operation = self.create_instance(
            request, request_id=request_id, timeout_seconds=timeout_seconds
        )
        return self.wait_operation(operation, timeout_seconds=timeout_seconds)

    @staticmethod
    def _not_found(error: BaseException) -> bool:
        code = getattr(error, "code", None)
        if callable(code):
            try:
                code = code()
            except Exception:
                code = None
        return code in {404, "404"} or type(error).__name__ in {
            "NotFound",
            "ResourceNotFoundError",
        }

    def _maybe_instance(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> Any | None:
        proposal = request.proposal
        try:
            return self._instances.get(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                instance=proposal.instance_name,
                timeout=timeout_seconds,
            )
        except Exception as error:
            if self._not_found(error):
                return None
            raise GcpPrivateCampaignTransportError("INSTANCE_READBACK_FAILED") from None

    def _instance(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> Any:
        proposal = request.proposal
        value = self._maybe_instance(request, timeout_seconds=timeout_seconds)
        if value is None:
            raise GcpPrivateCampaignTransportError("INSTANCE_NOT_FOUND")
        self._assert_instance_identity(value, proposal)
        _positive_provider_id(
            getattr(value, "id", None), code="INSTANCE_IDENTITY_MISMATCH"
        )
        _canonical_compute_reference(
            getattr(value, "machine_type", None),
            expected=(
                f"projects/{proposal.topology.project_id}/zones/"
                f"{proposal.topology.zone}/machineTypes/a2-highgpu-2g"
            ),
            code="INSTANCE_TOPOLOGY_MISMATCH",
        )
        try:
            interfaces = list(getattr(value, "network_interfaces", ()) or ())
            if len(interfaces) != 1:
                raise GcpPrivateCampaignTransportError("INSTANCE_NETWORK_MISMATCH")
            interface = interfaces[0]
            _canonical_compute_reference(
                getattr(interface, "network", None),
                expected=proposal.topology.private_network,
                code="INSTANCE_NETWORK_MISMATCH",
            )
            _canonical_compute_reference(
                getattr(interface, "subnetwork", None),
                expected=proposal.topology.private_subnetwork,
                code="INSTANCE_NETWORK_MISMATCH",
            )
            if (
                list(getattr(interface, "access_configs", ()) or ())
                or list(getattr(interface, "ipv6_access_configs", ()) or ())
                or str(getattr(interface, "stack_type", "")) != "IPV4_ONLY"
                or getattr(value, "can_ip_forward", None) is not False
            ):
                raise GcpPrivateCampaignTransportError("INSTANCE_NETWORK_MISMATCH")
            # A2 high-GPU shapes are fixed-GPU machine types.  Like the
            # established one-GPU guarded path, Compute's normal readback
            # proves the accelerator through the exact machine type rather
            # than a guest_accelerators attachment.  Any attachment is
            # contradictory to this closed profile and is rejected.
            if (
                proposal.topology.accelerator_model,
                proposal.topology.accelerator_provider_type,
                proposal.topology.accelerator_count,
            ) != _A2_HIGHGPU_2G_ACCELERATOR_PROFILE or list(
                getattr(value, "guest_accelerators", ()) or ()
            ):
                raise GcpPrivateCampaignTransportError("INSTANCE_TOPOLOGY_MISMATCH")
        except GcpPrivateCampaignError:
            raise
        except (TypeError, ValueError):
            raise GcpPrivateCampaignTransportError(
                "INSTANCE_TOPOLOGY_MISMATCH"
            ) from None
        return value

    @staticmethod
    def _assert_instance_identity(
        value: Any, proposal: GcpPrivateCampaignProposal
    ) -> None:
        if str(getattr(value, "name", "")) != proposal.instance_name:
            raise GcpPrivateCampaignTransportError("INSTANCE_IDENTITY_MISMATCH")
        if dict(
            getattr(value, "labels", {}) or {}
        ) != proposal.ownership_labels.model_dump(mode="json"):
            raise GcpPrivateCampaignTransportError("INSTANCE_OWNERSHIP_MISMATCH")

    @staticmethod
    def _metadata(value: Any) -> dict[str, str]:
        metadata = getattr(value, "metadata", None)
        items = getattr(metadata, "items", ()) if metadata is not None else ()
        output: dict[str, str] = {}
        for item in items or ():
            key = getattr(item, "key", None)
            child = getattr(item, "value", None)
            if not isinstance(key, str) or not isinstance(child, str) or key in output:
                raise GcpPrivateCampaignTransportError("INSTANCE_METADATA_INVALID")
            output[key] = child
        return output

    def _owned_disk_values(
        self, proposal: GcpPrivateCampaignProposal, *, timeout_seconds: int
    ) -> tuple[Any, ...]:
        try:
            query = self._sdk.ListDisksRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                filter=self._owned_filter(proposal),
                max_results=16,
            )
            values = self._disks.list(request=query, timeout=timeout_seconds)
            if getattr(values, "next_page_token", None) not in (None, ""):
                raise GcpPrivateCampaignTransportError("OWNED_INVENTORY_INCOMPLETE")
            return tuple(values)
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "OWNED_INVENTORY_INCOMPLETE"
            ) from None

    def _maybe_boot_disk(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> Any | None:
        proposal = request.proposal
        try:
            query = self._sdk.GetDiskRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                disk=proposal.boot_disk_name,
            )
            return self._disks.get(request=query, timeout=timeout_seconds)
        except Exception as error:
            if self._not_found(error):
                return None
            raise GcpPrivateCampaignTransportError(
                "BOOT_DISK_READBACK_FAILED"
            ) from None

    @staticmethod
    def _disk_observation(
        proposal: GcpPrivateCampaignProposal,
        value: Any,
        *,
        attached_provider_instance_id: str | None,
    ) -> GcpPrivateCampaignDiskObservation:
        if str(getattr(value, "name", "")) != proposal.boot_disk_name:
            raise GcpPrivateCampaignTransportError("OWNED_RESIDUAL_AMBIGUOUS")
        if dict(
            getattr(value, "labels", {}) or {}
        ) != proposal.ownership_labels.model_dump(mode="json"):
            raise GcpPrivateCampaignTransportError("BOOT_DISK_OWNERSHIP_MISMATCH")
        _canonical_compute_reference(
            getattr(value, "self_link", None),
            expected=(
                f"projects/{proposal.topology.project_id}/zones/{proposal.topology.zone}/"
                f"disks/{proposal.boot_disk_name}"
            ),
            code="BOOT_DISK_IDENTITY_MISMATCH",
        )
        _canonical_compute_reference(
            getattr(value, "source_image", None),
            expected=proposal.boot_image.image_ref,
            code="BOOT_DISK_PROVENANCE_MISMATCH",
        )
        if (
            _positive_provider_id(
                getattr(value, "source_image_id", None),
                code="BOOT_DISK_PROVENANCE_MISMATCH",
            )
            != proposal.boot_image.provider_image_id
        ):
            raise GcpPrivateCampaignTransportError("BOOT_DISK_PROVENANCE_MISMATCH")
        try:
            users = tuple(getattr(value, "users", ()) or ())
        except TypeError:
            raise GcpPrivateCampaignTransportError(
                "BOOT_DISK_ATTACHMENT_MISMATCH"
            ) from None
        if attached_provider_instance_id is None:
            if users:
                raise GcpPrivateCampaignTransportError("BOOT_DISK_ATTACHMENT_MISMATCH")
            attachment_state: Literal["ATTACHED", "DETACHED"] = "DETACHED"
        else:
            expected_user = (
                f"projects/{proposal.topology.project_id}/zones/{proposal.topology.zone}/"
                f"instances/{proposal.instance_name}"
            )
            if len(users) != 1:
                raise GcpPrivateCampaignTransportError("BOOT_DISK_ATTACHMENT_MISMATCH")
            _canonical_compute_reference(
                users[0],
                expected=expected_user,
                code="BOOT_DISK_ATTACHMENT_MISMATCH",
            )
            attachment_state = "ATTACHED"
        provider_disk_id = str(
            _positive_provider_id(
                getattr(value, "id", None), code="BOOT_DISK_IDENTITY_MISMATCH"
            )
        )
        return GcpPrivateCampaignDiskObservation(
            disk_name=proposal.boot_disk_name,
            provider_disk_id_sha256=sha256_digest(provider_disk_id.encode("ascii")),
            ownership_labels=proposal.ownership_labels,
            # The provider exposes an immutable source image ref/ID rather than
            # an OCI-like content digest.  It is checked above; this approved
            # identity is retained only after that exact provenance check.
            source_boot_image_identity=proposal.boot_image.boot_image_identity,
            attached_provider_instance_id=attached_provider_instance_id,
            attachment_state=attachment_state,
            boot_attachment=True,
        )

    def _verify_live_boot_disk(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        provider_instance_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignDiskObservation:
        rows = self._owned_disk_values(
            request.proposal, timeout_seconds=timeout_seconds
        )
        if len(rows) != 1:
            raise GcpPrivateCampaignTransportError("BOOT_DISK_PROVENANCE_MISMATCH")
        return self._disk_observation(
            request.proposal,
            rows[0],
            attached_provider_instance_id=provider_instance_id,
        )

    def _observe_exact_instance_value(
        self,
        request: GcpPrivateCampaignCreateRequest,
        value: Any,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignInstanceObservation:
        proposal = request.proposal
        provider_instance_id = str(
            _positive_provider_id(
                getattr(value, "id", None), code="INSTANCE_IDENTITY_MISMATCH"
            )
        )
        disks = list(getattr(value, "disks", ()) or ())
        boot = [item for item in disks if getattr(item, "boot", None) is True]
        scheduling = getattr(value, "scheduling", None)
        duration = getattr(
            getattr(scheduling, "max_run_duration", None), "seconds", None
        )
        scratch = [item for item in disks if getattr(item, "boot", None) is not True]
        if (
            len(disks) != 1 + _A2_HIGHGPU_2G_MACHINE_FIXED_LOCAL_SSD_COUNT
            or len(boot) != 1
            or len(scratch) != _A2_HIGHGPU_2G_MACHINE_FIXED_LOCAL_SSD_COUNT
            or getattr(boot[0], "auto_delete", None) is not True
            or str(getattr(boot[0], "device_name", "")) != proposal.boot_disk_name
            or any(
                str(getattr(item, "type", "")) != "SCRATCH"
                or getattr(item, "auto_delete", None) is not True
                or str(getattr(item, "interface", "")) != "NVME"
                for item in scratch
            )
            or getattr(value, "deletion_protection", None) is not False
            or getattr(scheduling, "automatic_restart", None) is not False
            or str(getattr(scheduling, "on_host_maintenance", "")) != "TERMINATE"
            or str(getattr(scheduling, "instance_termination_action", "")) != "DELETE"
            or str(duration) != str(proposal.max_runtime_seconds)
        ):
            raise GcpPrivateCampaignTransportError("INSTANCE_SAFETY_MISMATCH")
        metadata = self._metadata(value)
        expected_script = render_gcp_private_campaign_startup_script(request)
        expected_script_sha256 = sha256_digest(expected_script.encode("utf-8"))
        if (
            set(metadata)
            != {
                _STARTUP_SCRIPT_METADATA_KEY,
                _STARTUP_SCRIPT_DIGEST_METADATA_KEY,
                _STARTUP_PAYLOAD_METADATA_KEY,
                _PROPOSAL_METADATA_KEY,
            }
            or metadata.get(_STARTUP_SCRIPT_METADATA_KEY) != expected_script
            or metadata.get(_STARTUP_SCRIPT_DIGEST_METADATA_KEY)
            != expected_script_sha256
            or metadata.get(_STARTUP_PAYLOAD_METADATA_KEY)
            != proposal.startup_payload_digest
            or metadata.get(_PROPOSAL_METADATA_KEY) != proposal.proposal_id
        ):
            raise GcpPrivateCampaignTransportError("INSTANCE_STARTUP_BINDING_MISMATCH")
        boot_disk = self._verify_live_boot_disk(
            request,
            provider_instance_id=provider_instance_id,
            timeout_seconds=timeout_seconds,
        )
        return GcpPrivateCampaignInstanceObservation(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            provider_instance_id=provider_instance_id,
            ownership_labels=proposal.ownership_labels,
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_provider_type="nvidia-tesla-a100",
            accelerator_count=2,
            machine_fixed_local_ssd_count=2,
            state="RUNNING"
            if str(getattr(value, "status", "")) == "RUNNING"
            else "NOT_FOUND",
            external_access="ABSENT",
            boot_disk_name=proposal.boot_disk_name,
            boot_disk_provider_id_sha256=boot_disk.provider_disk_id_sha256,
            boot_disk_auto_delete=True,
            persistent_disk_count=0,
            max_runtime_seconds=proposal.max_runtime_seconds,
            instance_termination_action="DELETE",
            startup_payload_digest=proposal.startup_payload_digest,
            startup_script_sha256=expected_script_sha256,
        )

    def observe_exact_instance(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignInstanceObservation:
        return self._observe_exact_instance_value(
            request,
            self._instance(request, timeout_seconds=timeout_seconds),
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _http(
        origin: str,
        path: str,
        *,
        method: Literal["GET", "POST"],
        timeout_seconds: int,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        """Use direct, non-redirecting private probes with a bounded body."""

        try:
            if body is not None and method != "POST":
                raise ValueError("unexpected request body")
            request = Request(
                origin + path,
                data=body,
                headers={"Content-Type": "application/json"} if body else {},
                method=method,
            )
            # Do not inherit HTTP(S)_PROXY and do not follow a private endpoint
            # redirect to another origin.  A 3xx becomes a non-success status.
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=timeout_seconds) as response:
                content = response.read(_MAX_HTTP_BODY_BYTES + 1)
                if len(content) > _MAX_HTTP_BODY_BYTES:
                    raise GcpPrivateCampaignTransportError("PRIVATE_RESPONSE_TOO_LARGE")
                return int(response.status), content
        except GcpPrivateCampaignError:
            raise
        except HTTPError as error:
            with suppress(OSError):
                error.close()
            return error.code, b""
        except (URLError, OSError, ValueError):
            raise GcpPrivateCampaignTransportError(
                "PRIVATE_READINESS_TRANSPORT_FAILED"
            ) from None

    @staticmethod
    def _strict_response_json(content: bytes, *, code: str) -> object:
        try:
            text = content.decode("utf-8")

            def pairs(rows: list[tuple[str, object]]) -> dict[str, object]:
                output: dict[str, object] = {}
                for key, value in rows:
                    if key in output:
                        raise ValueError("duplicate key")
                    output[key] = value
                return output

            return json.loads(
                text,
                object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise GcpPrivateCampaignTransportError(code) from None

    @classmethod
    def _model_capability_present(cls, content: bytes) -> bool:
        value = cls._strict_response_json(content, code="PRIVATE_MODELS_MALFORMED")
        return (
            isinstance(value, dict)
            and isinstance(value.get("data"), list)
            and any(
                isinstance(row, dict) and row.get("id") == _EXPECTED_MODEL_ID
                for row in value["data"]
            )
        )

    @classmethod
    def _valid_generation_response(cls, content: bytes) -> bool:
        value = cls._strict_response_json(content, code="PRIVATE_GENERATION_MALFORMED")
        if not isinstance(value, dict):
            return False
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        return all(
            isinstance(choice, dict)
            and type(choice.get("index")) is int
            and isinstance(choice.get("message"), dict)
            and choice["message"].get("role") == "assistant"
            and isinstance(choice["message"].get("content"), str)
            and bool(choice["message"]["content"].strip())
            and isinstance(choice.get("finish_reason"), str)
            and bool(choice["finish_reason"].strip())
            for choice in choices
        )

    @classmethod
    def _engine_attestation_digest(
        cls,
        content: bytes,
        *,
        engine: GcpPrivateCampaignEngine,
        startup_payload_digest: str,
    ) -> str:
        """Parse and bind the adapter's private endpoint-to-engine attestation."""

        value = cls._strict_response_json(
            content, code="PRIVATE_ENGINE_ATTESTATION_MALFORMED"
        )
        try:
            observed = GcpPrivateCampaignEngineAttestation.model_validate(value)
        except (TypeError, ValidationError, ValueError):
            raise GcpPrivateCampaignTransportError(
                "PRIVATE_ENGINE_ATTESTATION_MALFORMED"
            ) from None
        expected = GcpPrivateCampaignEngineAttestation(
            schema_version="inferdrome.gcp-private-engine-attestation.v2",
            endpoint_id=engine.endpoint_id,
            container_name=engine.container_name,
            gpu_ordinal=engine.gpu_ordinal,
            private_port=engine.private_port,
            serving_image=engine.serving_image,
            model=engine.model,
            runtime=engine.runtime,
            startup_payload_digest=startup_payload_digest,
            listener_scope=engine.listener_scope,
        )
        if observed != expected:
            raise GcpPrivateCampaignTransportError(
                "PRIVATE_ENGINE_ATTESTATION_MISMATCH"
            )
        return sha256_digest(canonical_json_bytes(observed.model_dump(mode="json")))

    def observe_readiness(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignReadiness:
        proposal = request.proposal
        value = self._instance(request, timeout_seconds=timeout_seconds)
        exact_instance = self._observe_exact_instance_value(
            request, value, timeout_seconds=timeout_seconds
        )
        if exact_instance.state != "RUNNING":
            raise GcpPrivateCampaignTransportError(
                "PRIVATE_READINESS_INSTANCE_NOT_RUNNING"
            )
        if exact_instance.startup_script_sha256 is None:
            raise GcpPrivateCampaignTransportError("INSTANCE_STARTUP_BINDING_MISMATCH")
        interface = next(iter(getattr(value, "network_interfaces", ()) or ()))
        private_ip = _rfc1918(
            getattr(interface, "network_i_p", None)
            or getattr(interface, "network_ip", None)
        )
        endpoints: list[GcpPrivateCampaignEndpointReadiness] = []
        import time

        for engine in request.startup_payload.engines:
            origin = f"http://{private_ip}:{engine.private_port}"
            health, _ = self._http(
                origin,
                engine.health_path,
                method="GET",
                timeout_seconds=timeout_seconds,
            )
            models, model_content = self._http(
                origin, "/v1/models", method="GET", timeout_seconds=timeout_seconds
            )
            generation, generation_content = self._http(
                origin,
                engine.generation_path,
                method="POST",
                body=_GENERATION_PROBE,
                timeout_seconds=timeout_seconds,
            )
            metrics_status, metrics = self._http(
                origin,
                engine.metrics_path,
                method="GET",
                timeout_seconds=timeout_seconds,
            )
            attestation_status, attestation_content = self._http(
                origin,
                engine.attestation_path,
                method="GET",
                timeout_seconds=timeout_seconds,
            )
            if (
                health != 200
                or models != 200
                or generation != 200
                or metrics_status != 200
                or attestation_status != 200
                or not self._model_capability_present(model_content)
                or not self._valid_generation_response(generation_content)
            ):
                raise GcpPrivateCampaignTransportError("PRIVATE_READINESS_UNAVAILABLE")
            _metric_is_present(metrics)
            attestation_digest = self._engine_attestation_digest(
                attestation_content,
                engine=engine,
                startup_payload_digest=proposal.startup_payload_digest,
            )
            endpoints.append(
                GcpPrivateCampaignEndpointReadiness(
                    endpoint_id=engine.endpoint_id,
                    gpu_ordinal=engine.gpu_ordinal,
                    private_port=engine.private_port,
                    health_http_status=200,
                    generation_http_status=200,
                    metrics_metric_name="vllm:num_requests_running",
                    health_capability="HTTP_HEALTH_V1",
                    metrics_capability="VLLM_PROMETHEUS_V1",
                    engine_attestation_capability="INFERDROME_ENGINE_ATTESTATION_V2",
                    engine_attestation_sha256=attestation_digest,
                    observation_epoch=1,
                    observed_monotonic_ns=time.monotonic_ns(),
                )
            )
        return GcpPrivateCampaignReadiness(
            schema_version=GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_VERSION,
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            provider_instance_id=exact_instance.provider_instance_id,
            ownership_labels=proposal.ownership_labels,
            private_ipv4=private_ip,
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_count=2,
            startup_payload_digest=proposal.startup_payload_digest,
            startup_script_sha256=exact_instance.startup_script_sha256,
            endpoints=cast(
                tuple[
                    GcpPrivateCampaignEndpointReadiness,
                    GcpPrivateCampaignEndpointReadiness,
                ],
                tuple(endpoints),
            ),
            observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def handoff_campaign(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        routing_config: bytes,
        workload: bytes,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignHandoffReceipt:
        return self._runner.handoff(
            request,
            routing_config=routing_config,
            workload=workload,
            timeout_seconds=timeout_seconds,
        )

    def retrieve_evidence(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignEvidenceReceipt:
        del timeout_seconds
        return self._runner.retrieve(request)

    def project_delete_instance_request(
        self, request: GcpPrivateCampaignCreateRequest, *, request_id: str
    ) -> object:
        proposal = request.proposal
        if request_id != proposal.request_ids.delete_request_id:
            raise GcpPrivateCampaignTransportError("REQUEST_ID_MISMATCH")
        try:
            return self._sdk.DeleteInstanceRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                instance=proposal.instance_name,
                request_id=request_id,
            )
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "DELETE_PROJECTION_INVALID"
            ) from None

    def _already_absent_operation(
        self,
        *,
        kind: Literal["delete_instance", "delete_boot_disk"],
        request_id: str,
    ) -> GcpPrivateCampaignOperation:
        """Record a no-op only after an exact read proves absence."""

        result = _CompletedProviderOperation(
            name=f"local-already-absent-{kind}-{request_id}"
        )
        operation = _operation(result, kind=kind, request_id=request_id)
        with self._lock:
            self._operations[operation.operation_id] = result
        return operation

    @staticmethod
    def _assert_resource_binding(
        binding: GcpPrivateCampaignExactResourceBinding,
        proposal: GcpPrivateCampaignProposal,
        *,
        provider_instance_id: str,
    ) -> None:
        if any(
            (
                binding.proposal_id != proposal.proposal_id,
                binding.project_id != proposal.topology.project_id,
                binding.zone != proposal.topology.zone,
                binding.instance_name != proposal.instance_name,
                binding.ownership_labels != proposal.ownership_labels,
                binding.provider_instance_id_sha256
                != sha256_digest(provider_instance_id.encode("ascii")),
            )
        ):
            raise GcpPrivateCampaignTransportError("INSTANCE_IDENTITY_MISMATCH")

    def delete_exact_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation:
        projected = self.project_delete_instance_request(request, request_id=request_id)
        try:
            current = self._maybe_instance(request, timeout_seconds=timeout_seconds)
            if current is None:
                return self._already_absent_operation(
                    kind="delete_instance", request_id=request_id
                )
            self._assert_instance_identity(current, request.proposal)
            provider_instance_id = str(
                _positive_provider_id(
                    getattr(current, "id", None), code="INSTANCE_IDENTITY_MISMATCH"
                )
            )
            self._assert_resource_binding(
                exact_resource_binding,
                request.proposal,
                provider_instance_id=provider_instance_id,
            )
            boot_disk = self._verify_live_boot_disk(
                request,
                provider_instance_id=provider_instance_id,
                timeout_seconds=timeout_seconds,
            )
            if (
                boot_disk.provider_disk_id_sha256
                != exact_resource_binding.boot_disk_provider_id_sha256
            ):
                raise GcpPrivateCampaignTransportError("BOOT_DISK_IDENTITY_MISMATCH")
            value = self._instances.delete(request=projected, timeout=timeout_seconds)
            operation = _operation(value, kind="delete_instance", request_id=request_id)
            with self._lock:
                self._operations[operation.operation_id] = value
            return operation
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "DELETE_PROVIDER_FAILED", ambiguous=True
            ) from None

    def reconcile_delete_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult:
        operation = self.delete_exact_instance(
            request,
            exact_resource_binding=exact_resource_binding,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        return self.wait_operation(operation, timeout_seconds=timeout_seconds)

    @staticmethod
    def _owned_filter(proposal: GcpPrivateCampaignProposal) -> str:
        labels = proposal.ownership_labels
        return " AND ".join(
            (
                "labels.inferdrome = inferdrome",
                f"labels.controller_id = {labels.controller_id}",
                f"labels.ownership_nonce = {labels.ownership_nonce}",
            )
        )

    def _list_disks(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        attached_provider_instance_id: str | None,
        timeout_seconds: int,
    ) -> tuple[GcpPrivateCampaignDiskObservation, ...]:
        proposal = request.proposal
        try:
            output: list[GcpPrivateCampaignDiskObservation] = []
            for value in self._owned_disk_values(
                proposal, timeout_seconds=timeout_seconds
            ):
                output.append(
                    self._disk_observation(
                        proposal,
                        value,
                        attached_provider_instance_id=attached_provider_instance_id,
                    )
                )
            return tuple(output)
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "OWNED_INVENTORY_INCOMPLETE"
            ) from None

    def _list_instances(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> tuple[Literal["PRESENT", "ABSENT"], str | None]:
        proposal = request.proposal
        try:
            query = self._sdk.ListInstancesRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                filter=self._owned_filter(proposal),
                max_results=16,
            )
            values = self._instances.list(request=query, timeout=timeout_seconds)
            if getattr(values, "next_page_token", None) not in (None, ""):
                raise GcpPrivateCampaignTransportError("OWNED_INVENTORY_INCOMPLETE")
            rows = list(values)
            if not rows:
                return "ABSENT", None
            if (
                len(rows) != 1
                or str(getattr(rows[0], "name", "")) != proposal.instance_name
            ):
                raise GcpPrivateCampaignTransportError("OWNED_RESIDUAL_AMBIGUOUS")
            if dict(
                getattr(rows[0], "labels", {}) or {}
            ) != proposal.ownership_labels.model_dump(mode="json"):
                raise GcpPrivateCampaignTransportError("INSTANCE_OWNERSHIP_MISMATCH")
            provider_instance_id = str(
                _positive_provider_id(
                    getattr(rows[0], "id", None), code="INSTANCE_IDENTITY_MISMATCH"
                )
            )
            return "PRESENT", provider_instance_id
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "OWNED_INVENTORY_INCOMPLETE"
            ) from None

    def list_exact_owned_residuals(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignOwnedResidualInventory:
        proposal = request.proposal
        instance_state, provider_instance_id = self._list_instances(
            request, timeout_seconds=timeout_seconds
        )
        disks = self._list_disks(
            request,
            attached_provider_instance_id=provider_instance_id,
            timeout_seconds=timeout_seconds,
        )
        return GcpPrivateCampaignOwnedResidualInventory(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            ownership_labels=proposal.ownership_labels,
            pagination_complete=True,
            instance_state=instance_state,
            disks=disks,
        )

    def delete_exact_owned_boot_disk(
        self,
        request: GcpPrivateCampaignCreateRequest,
        disk: GcpPrivateCampaignDiskObservation,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation:
        proposal = request.proposal
        if (
            request_id != proposal.request_ids.boot_disk_delete_request_id
            or disk.disk_name != proposal.boot_disk_name
            or disk.provider_disk_id_sha256
            != exact_resource_binding.boot_disk_provider_id_sha256
        ):
            raise GcpPrivateCampaignTransportError("BOOT_DISK_DELETE_SCOPE_MISMATCH")
        try:
            current = self._list_disks(
                request,
                attached_provider_instance_id=None,
                timeout_seconds=timeout_seconds,
            )
            if not current:
                return self._already_absent_operation(
                    kind="delete_boot_disk", request_id=request_id
                )
            if (
                len(current) != 1
                or current[0] != disk
                or current[0].provider_disk_id_sha256
                != exact_resource_binding.boot_disk_provider_id_sha256
            ):
                raise GcpPrivateCampaignTransportError(
                    "BOOT_DISK_DELETE_SCOPE_MISMATCH"
                )
            projected = self._sdk.DeleteDiskRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                disk=proposal.boot_disk_name,
                request_id=request_id,
            )
            value = self._disks.delete(request=projected, timeout=timeout_seconds)
            operation = _operation(
                value, kind="delete_boot_disk", request_id=request_id
            )
            with self._lock:
                self._operations[operation.operation_id] = value
            return operation
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "BOOT_DISK_DELETE_FAILED", ambiguous=True
            ) from None

    def reconcile_delete_exact_owned_boot_disk(
        self,
        request: GcpPrivateCampaignCreateRequest,
        disk: GcpPrivateCampaignDiskObservation,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult:
        operation = self.delete_exact_owned_boot_disk(
            request,
            disk,
            exact_resource_binding=exact_resource_binding,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        return self.wait_operation(operation, timeout_seconds=timeout_seconds)

    def confirm_exact_absence(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignAbsenceObservation:
        # A label-scoped list alone is not enough for final absence: a
        # malfunctioning/mutated resource can lose an ownership label while
        # retaining the exact approved name.  Direct reads prove those exact
        # names absent, then the bounded label scope proves no other owned
        # residual remains.
        exact_instance = self._maybe_instance(request, timeout_seconds=timeout_seconds)
        if exact_instance is not None:
            self._assert_instance_identity(exact_instance, request.proposal)
            raise GcpPrivateCampaignTransportError("INSTANCE_RESIDUAL")
        exact_disk = self._maybe_boot_disk(request, timeout_seconds=timeout_seconds)
        if exact_disk is not None:
            self._disk_observation(
                request.proposal,
                exact_disk,
                attached_provider_instance_id=None,
            )
            raise GcpPrivateCampaignTransportError("BOOT_DISK_RESIDUAL")
        inventory = self.list_exact_owned_residuals(
            request, timeout_seconds=timeout_seconds
        )
        proposal = request.proposal
        if inventory.instance_state != "ABSENT":
            raise GcpPrivateCampaignTransportError("INSTANCE_RESIDUAL")
        if inventory.disks:
            raise GcpPrivateCampaignTransportError("BOOT_DISK_RESIDUAL")
        return GcpPrivateCampaignAbsenceObservation(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            ownership_labels=proposal.ownership_labels,
            pagination_complete=True,
            instance_absent=True,
            boot_disk_absent=True,
            no_other_owned_billable_residuals=True,
        )

    def discover_exact_owned(
        self, proposal: GcpPrivateCampaignProposal, *, timeout_seconds: int
    ) -> GcpPrivateCampaignOwnedResidualInventory:
        # Discovery is deliberately read-only and label-scoped.  It does not
        # infer a target from an IP/name or call a mutation method.
        try:
            query = self._sdk.ListInstancesRequest(
                project=proposal.topology.project_id,
                zone=proposal.topology.zone,
                filter=self._owned_filter(proposal),
                max_results=16,
            )
            instances = self._instances.list(request=query, timeout=timeout_seconds)
            if getattr(instances, "next_page_token", None) not in (None, ""):
                raise GcpPrivateCampaignTransportError("OWNED_INVENTORY_INCOMPLETE")
            rows = list(instances)
            if not rows:
                instance_state: Literal["PRESENT", "ABSENT"] = "ABSENT"
                provider_instance_id: str | None = None
            elif (
                len(rows) == 1
                and str(getattr(rows[0], "name", "")) == proposal.instance_name
                and dict(getattr(rows[0], "labels", {}) or {})
                == proposal.ownership_labels.model_dump(mode="json")
            ):
                instance_state = "PRESENT"
                provider_instance_id = str(
                    _positive_provider_id(
                        getattr(rows[0], "id", None),
                        code="INSTANCE_IDENTITY_MISMATCH",
                    )
                )
            else:
                raise GcpPrivateCampaignTransportError("OWNED_RESIDUAL_AMBIGUOUS")
            disks = tuple(
                self._disk_observation(
                    proposal,
                    value,
                    attached_provider_instance_id=provider_instance_id,
                )
                for value in self._owned_disk_values(
                    proposal, timeout_seconds=timeout_seconds
                )
            )
            return GcpPrivateCampaignOwnedResidualInventory(
                proposal_id=proposal.proposal_id,
                project_id=proposal.topology.project_id,
                zone=proposal.topology.zone,
                ownership_labels=proposal.ownership_labels,
                pagination_complete=True,
                instance_state=instance_state,
                disks=disks,
            )
        except GcpPrivateCampaignError:
            raise
        except Exception:
            raise GcpPrivateCampaignTransportError(
                "OWNED_INVENTORY_INCOMPLETE"
            ) from None


def create_google_private_campaign_transport(
    *, evidence_root: Path
) -> GoogleGcpPrivateCampaignTransport:
    """Construct the only live adapter, at the controller's gated factory edge."""

    try:
        sdk = importlib.import_module("google.cloud.compute_v1")
        instances = sdk.InstancesClient()
        disks = sdk.DisksClient()
        images = sdk.ImagesClient()
    except Exception:
        raise GcpPrivateCampaignOptionalDependencyUnavailable() from None
    return GoogleGcpPrivateCampaignTransport(
        sdk=sdk,
        instances_client=instances,
        disks_client=disks,
        images_client=images,
        runner=LocalGcpPrivateCampaignRunner(evidence_root),
    )
