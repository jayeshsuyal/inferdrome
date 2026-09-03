"""Additive, exact two-engine GCP_PRIVATE pre-campaign controller.

This module is deliberately separate from the frozen one-GPU GCP lifecycle.
It models one narrow, human-approved topology only: an ephemeral
``a2-highgpu-2g`` VM with two A100-SXM4-40GB GPUs and two independently
addressable, private vLLM-compatible engines.  It has no Google SDK, socket,
credential, or provider dependency at import time.  A caller supplies a
transport factory; the controller invokes that factory only after it has
validated the exact local approval and durably recorded a provider-native
DELETE/max-runtime backstop.

The built-in fake is local test infrastructure.  It exists to make the
approval ordering, hash-chained journal, crash windows, exact reconciliation,
and cleanup semantics executable without contacting a provider.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Final,
    Literal,
    Protocol,
    Self,
    cast,
    runtime_checkable,
)

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic.config import ExtraValues

from inferdrome.deployment.gcp import (
    GcpProjectId,
    GcpRegion,
    GcpTimestamp,
    GcpZone,
)
from inferdrome.deployment.gcp_lifecycle import (
    GcpExecutionError,
    _parse_timestamp,
    _timestamp,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest
from inferdrome.qwen3_campaign import qwen3_workload_sha256
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.contracts import (
    Commit,
    ImageIdentity,
    ModelIdentity,
    RuntimeIdentity,
    fixed_policy_ids,
    fixed_r1_input_digests,
    fixed_selected_workload_sha256,
)

GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-topology.v2"
)
GCP_PRIVATE_CAMPAIGN_STARTUP_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-startup.v2"
)
GCP_PRIVATE_CAMPAIGN_PROPOSAL_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-proposal.v2"
)
GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-approval.v2"
)
GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-cleanup-authorization.v2"
)
GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-create-request.v2"
)
GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-readiness.v2"
)
GCP_PRIVATE_CAMPAIGN_ENGINE_ATTESTATION_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-engine-attestation.v2"
)
GCP_PRIVATE_CAMPAIGN_EVENT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-journal-event.v2"
)
GCP_PRIVATE_CAMPAIGN_RECEIPT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-receipt.v2"
)
GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-topology:v2"
)
GCP_PRIVATE_CAMPAIGN_STARTUP_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-startup:v2"
)
GCP_PRIVATE_CAMPAIGN_PROPOSAL_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-proposal:v2"
)
GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-approval:v2"
)
GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-cleanup-authorization:v2"
)
GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-create-request:v2"
)
GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-readiness:v2"
)
GCP_PRIVATE_CAMPAIGN_ENGINE_ATTESTATION_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-engine-attestation:v2"
)
GCP_PRIVATE_CAMPAIGN_EVENT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-journal-event:v2"
)
GCP_PRIVATE_CAMPAIGN_RECEIPT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-private-campaign-receipt:v2"
)

GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION: Final = (
    "AUTHORIZE_GCP_PRIVATE_TWO_A100_PRECAMPAIGN_V2"
)
GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION: Final = (
    "AUTHORIZE_GCP_PRIVATE_EXACT_CLEANUP_V2"
)
GCP_PRIVATE_CAMPAIGN_MAX_EVENTS: Final = 96
GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES: Final = 524_288
GCP_PRIVATE_CAMPAIGN_READINESS_MAX_AGE_NS: Final = 5_000_000_000

_ERROR_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")
_CONTROLLER_RE: Final = re.compile(r"^pcctl-[a-z0-9]{8,24}$")
_INSTANCE_RE: Final = re.compile(r"^inferdrome-pc-[a-z0-9]{8,24}$")
_DISK_RE: Final = re.compile(r"^inferdrome-pc-[a-z0-9]{8,24}-boot$")
_OPERATION_RE: Final = re.compile(r"^pcop-[a-z0-9]{8,64}$")
_PROVIDER_OPERATION_RE: Final = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")
_LABEL_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_RECORD_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_BOOT_IMAGE_RE: Final = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/"
    r"[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_NETWORK_RE: Final = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/networks/"
    r"[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_SUBNETWORK_RE: Final = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/regions/"
    r"[a-z]+-[a-z]+[0-9]{1,2}/subnetworks/[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_SENSITIVE_KEYS: Final = frozenset(
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
_SENSITIVE_NORMALIZED: Final = frozenset(
    re.sub(r"[^a-z0-9]", "", value) for value in _SENSITIVE_KEYS
)
_IDENTITY_KEYS: Final = frozenset(
    {
        "sourcecommit",
        "modelrevision",
        "tokenizerrevision",
        "proposalid",
        "proposaldigest",
        "eventdigest",
        "previouseventdigest",
        "startuppayloaddigest",
        "executedconfigsha256",
        "plan_sha256",
        "plansha256",
        "tracesha256",
        "faultschedulesha256",
        "trialplansha256",
        "policysetsha256",
        "workloadsha256",
        "selectedworkloadsha256",
        "evidencedestinationsha256",
        "retaineddigest",
        "bootimageidentity",
        # Fixed confirmation literals are intentionally long and all-caps.
        # They are local authority phrases, not credential values.
        "confirmation",
    }
)
_CREDENTIAL_SHAPES: Final = (
    re.compile(r"^(?:sk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^(?:gh[pousr]_)[A-Za-z0-9_]{16,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{16,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----$"),
    re.compile(r"^[A-Za-z0-9_-]{40,}$"),
)

PrecampaignControllerId = Annotated[
    str, StringConstraints(pattern=_CONTROLLER_RE.pattern)
]
PrecampaignInstanceName = Annotated[
    str, StringConstraints(pattern=_INSTANCE_RE.pattern)
]
PrecampaignBootDiskName = Annotated[str, StringConstraints(pattern=_DISK_RE.pattern)]
PrecampaignRequestUuid = Annotated[str, StringConstraints(pattern=_UUID_RE.pattern)]
PrecampaignOperationId = Annotated[
    str, StringConstraints(pattern=_OPERATION_RE.pattern)
]
PrecampaignLabelValue = Annotated[str, StringConstraints(pattern=_LABEL_RE.pattern)]
PrecampaignRecordId = Annotated[str, StringConstraints(pattern=_RECORD_RE.pattern)]


class GcpPrivateCampaignError(GcpExecutionError):
    """A bounded pre-campaign contract, journal, or controller failure."""

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        self.code = (
            code if _ERROR_RE.fullmatch(code) is not None else "PRECAMPAIGN_ERROR"
        )
        self.ambiguous = ambiguous
        super().__init__(self.code)


class GcpPrivateCampaignTransportError(GcpPrivateCampaignError):
    """A sanitized exact-provider operation failure.

    ``ambiguous`` means a mutation may have reached the provider.  It never
    permits a new create or broad cleanup; the controller reconciles only the
    caller-supplied exact request identifier.
    """


def _normal_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_credential(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _CREDENTIAL_SHAPES)


def _reject_precampaign_values(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None
            ):
                raise ValueError("pre-campaign JSON object keys are invalid")
            normalized = _normal_key(key)
            if normalized in _SENSITIVE_NORMALIZED:
                raise ValueError(
                    "pre-campaign JSON contains forbidden credential fields"
                )
            _reject_precampaign_values(child, path=(*path, normalized))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_precampaign_values(child, path=path)
    elif isinstance(value, str):
        if re.fullmatch(r"(?:https?|ssh|ftp)://.*", value, flags=re.IGNORECASE):
            raise ValueError("pre-campaign JSON contains a public endpoint value")
        if _looks_credential(value) and (not path or path[-1] not in _IDENTITY_KEYS):
            raise ValueError("pre-campaign JSON contains a credential-shaped value")


def _preflight_precampaign_json(
    payload: str | bytes | bytearray, *, kind: str
) -> bytes:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError(f"{kind} input is invalid")
    if not 1 <= len(raw) <= GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES:
        raise ValueError(f"{kind} has an invalid size")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, child in pairs:
            if key in output:
                raise ValueError("pre-campaign JSON has duplicate keys")
            output[key] = child
        return output

    def reject_constant(_: str) -> None:
        raise ValueError("pre-campaign JSON has a non-finite number")

    try:
        decoded = json.loads(
            raw, object_pairs_hook=unique, parse_constant=reject_constant
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(f"{kind} is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise ValueError(f"{kind} root must be an object")
    _reject_precampaign_values(decoded)
    return raw


class PrecampaignModel(FrozenModel):
    """Strict local contracts that reject credentials and noncanonical input."""

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
        _preflight_precampaign_json(json_data, kind=cls.__name__)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


def _model_value(model: PrecampaignModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("pre-campaign model must serialize as an object")
    return value


def _model_python_value(model: PrecampaignModel) -> dict[str, Any]:
    """Return constructor-safe values without converting strict tuples to lists."""

    value = model.model_dump(mode="python", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("pre-campaign model must serialize as an object")
    return value


def _canonical(model: PrecampaignModel) -> bytes:
    return canonical_json_bytes(_model_value(model))


def _digest(value: bytes) -> Sha256Digest:
    return digest_bytes(DigestDomain.GCP_PRIVATE_CAMPAIGN, value)


def _journal_digest(value: bytes) -> Sha256Digest:
    return digest_bytes(DigestDomain.GCP_PRIVATE_CAMPAIGN_JOURNAL, value)


def _private_ipv4(value: str) -> bool:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(parsed, ipaddress.IPv4Address) and any(
        parsed in network
        for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    )


class GcpPrivateCampaignOwnershipLabels(PrecampaignModel):
    """Immutable labels used for exact ownership, never hostname inference."""

    inferdrome: Literal["inferdrome"]
    managed_by: Literal["inferdrome_gcp_private_campaign_v2"]
    role: Literal["two-engine-precampaign"]
    controller_id: PrecampaignControllerId
    ownership_nonce: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{16,32}$")]


class GcpPrivateCampaignTopology(PrecampaignModel):
    """The one supported same-host two-engine provider profile."""

    schema_version: Literal["inferdrome.gcp-private-campaign-topology.v2"]
    provider: Literal["gcp-compute-engine"]
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: Literal["a2-highgpu-2g"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: Literal["nvidia-tesla-a100"]
    accelerator_count: Literal[2]
    topology_kind: Literal["same_host_two_independent_engines"]
    runner_separate_from_serving: Literal[True]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    machine_fixed_local_ssd_count: Literal[2] = 2
    machine_fixed_local_ssd_kind: Literal["EPHEMERAL_SCRATCH_NVME"] = (
        "EPHEMERAL_SCRATCH_NVME"
    )
    persistent_disk_count: Literal[0]
    private_network: Annotated[str, StringConstraints(pattern=_NETWORK_RE.pattern)]
    private_subnetwork: Annotated[
        str, StringConstraints(pattern=_SUBNETWORK_RE.pattern)
    ]

    @model_validator(mode="after")
    def _closed_topology(self) -> Self:
        if self.zone.rsplit("-", 1)[0] != self.region:
            raise ValueError("zone must belong to the declared region")
        expected_subnetwork = f"/regions/{self.region}/subnetworks/"
        if expected_subnetwork not in self.private_subnetwork:
            raise ValueError("subnetwork must belong to the declared region")
        if (
            f"projects/{self.project_id}/" not in self.private_network
            or f"projects/{self.project_id}/" not in self.private_subnetwork
        ):
            raise ValueError("network identities must belong to the declared project")
        return self


class GcpPrivateCampaignBootImage(PrecampaignModel):
    """One immutable boot-image identity, not a family or tag."""

    image_ref: Annotated[str, StringConstraints(pattern=_BOOT_IMAGE_RE.pattern)]
    provider_image_id: Annotated[int, Field(ge=1, le=10**19)]
    boot_image_identity: Sha256Digest


class GcpPrivateCampaignEngine(PrecampaignModel):
    """Semantic engine declaration from which the fixed startup script is made."""

    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    container_name: Literal["inferdrome-engine-a", "inferdrome-engine-b"]
    gpu_ordinal: Literal[0, 1]
    private_port: Literal[8000, 8001]
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    health_path: Literal["/health"]
    generation_path: Literal["/v1/chat/completions"]
    metrics_path: Literal["/metrics"]
    attestation_path: Literal["/inferdrome/v2/engine-attestation"]
    listener_scope: Literal["PRIVATE_VPC_ONLY"]
    request_logging: Literal["DISABLED"]

    @model_validator(mode="after")
    def _exact_slot(self) -> Self:
        expected = {
            "endpoint-a": ("inferdrome-engine-a", 0, 8000),
            "endpoint-b": ("inferdrome-engine-b", 1, 8001),
        }[self.endpoint_id]
        if (self.container_name, self.gpu_ordinal, self.private_port) != expected:
            raise ValueError("endpoint engine must use its fixed GPU and private port")
        return self


class GcpPrivateCampaignEngineAttestation(PrecampaignModel):
    """One private engine's bounded, independently observable runtime identity.

    Stock OpenAI-compatible health and metrics responses do not identify a
    container image or GPU ordinal.  The pinned vLLM-compatible adapter must
    therefore expose this small private attestation contract before Inferdrome
    can claim the endpoint-to-engine mapping needed for campaign admission.
    """

    schema_version: Literal["inferdrome.gcp-private-engine-attestation.v2"]
    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    container_name: Literal["inferdrome-engine-a", "inferdrome-engine-b"]
    gpu_ordinal: Literal[0, 1]
    private_port: Literal[8000, 8001]
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    startup_payload_digest: Sha256Digest
    listener_scope: Literal["PRIVATE_VPC_ONLY"]

    @model_validator(mode="after")
    def _exact_slot(self) -> Self:
        expected = {
            "endpoint-a": ("inferdrome-engine-a", 0, 8000),
            "endpoint-b": ("inferdrome-engine-b", 1, 8001),
        }[self.endpoint_id]
        if (self.container_name, self.gpu_ordinal, self.private_port) != expected:
            raise ValueError("engine attestation does not match its fixed slot")
        return self


class GcpPrivateCampaignStartupPayloadPayload(PrecampaignModel):
    """Interpretable immutable startup semantics, never opaque shell bytes."""

    schema_version: Literal["inferdrome.gcp-private-campaign-startup.v2"]
    startup_kind: Literal["two_private_vllm_engines"]
    source_commit: Commit
    runner_image: ImageIdentity
    engines: tuple[GcpPrivateCampaignEngine, GcpPrivateCampaignEngine]
    startup_adapter_id: Literal["inferdrome.gcp-private-vllm-startup-v2"]
    startup_adapter_version: Literal["1.0.0"]
    no_public_inference: Literal[True]
    boot_disk_auto_delete: Literal[True]
    provider_max_runtime_delete: Literal[True]

    @model_validator(mode="after")
    def _engine_inventory(self) -> Self:
        if tuple(engine.endpoint_id for engine in self.engines) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("startup must contain ordered endpoint-a and endpoint-b")
        if self.engines[0].serving_image != self.engines[1].serving_image:
            raise ValueError("two engines must use the same immutable serving image")
        if (
            self.engines[0].model != self.engines[1].model
            or self.engines[0].runtime != self.engines[1].runtime
        ):
            raise ValueError("two engines must use the same model and runtime identity")
        return self


class GcpPrivateCampaignStartupPayload(GcpPrivateCampaignStartupPayloadPayload):
    startup_payload_id: Sha256Digest

    @model_validator(mode="after")
    def _startup_identity(self) -> Self:
        if self.startup_payload_id != gcp_private_campaign_startup_payload_id(self):
            raise ValueError("startup payload identity does not match its semantics")
        return self


def canonical_gcp_private_campaign_startup_payload_bytes(
    payload: GcpPrivateCampaignStartupPayload | GcpPrivateCampaignStartupPayloadPayload,
) -> bytes:
    value = _model_value(payload)
    value.pop("startup_payload_id", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_startup_payload_id(
    payload: GcpPrivateCampaignStartupPayload | GcpPrivateCampaignStartupPayloadPayload,
) -> Sha256Digest:
    return _digest(canonical_gcp_private_campaign_startup_payload_bytes(payload))


def canonical_gcp_private_campaign_startup_bytes(
    payload: GcpPrivateCampaignStartupPayload,
) -> bytes:
    """Encode the complete content-addressed startup contract canonically."""

    return _canonical(payload)


def issue_gcp_private_campaign_startup_payload(
    *,
    source_commit: str,
    runner_image: ImageIdentity,
    serving_image: ImageIdentity,
) -> GcpPrivateCampaignStartupPayload:
    """Create the only semantic startup payload accepted by this profile."""

    model = ModelIdentity(
        model_id="Qwen/Qwen3-8B",
        model_revision="b968826d9c46dd6066d109eabc6255188de91218",
        tokenizer_revision="b968826d9c46dd6066d109eabc6255188de91218",
    )
    runtime = RuntimeIdentity(
        runtime_name="vllm",
        runtime_version="0.26.0",
        adapter_id="openai-compatible-routing-execution-v1",
        adapter_version="1.0.0",
    )
    payload = GcpPrivateCampaignStartupPayloadPayload(
        schema_version=GCP_PRIVATE_CAMPAIGN_STARTUP_SCHEMA_VERSION,
        startup_kind="two_private_vllm_engines",
        source_commit=source_commit,
        runner_image=runner_image,
        engines=(
            GcpPrivateCampaignEngine(
                endpoint_id="endpoint-a",
                container_name="inferdrome-engine-a",
                gpu_ordinal=0,
                private_port=8000,
                serving_image=serving_image,
                model=model,
                runtime=runtime,
                health_path="/health",
                generation_path="/v1/chat/completions",
                metrics_path="/metrics",
                attestation_path="/inferdrome/v2/engine-attestation",
                listener_scope="PRIVATE_VPC_ONLY",
                request_logging="DISABLED",
            ),
            GcpPrivateCampaignEngine(
                endpoint_id="endpoint-b",
                container_name="inferdrome-engine-b",
                gpu_ordinal=1,
                private_port=8001,
                serving_image=serving_image,
                model=model,
                runtime=runtime,
                health_path="/health",
                generation_path="/v1/chat/completions",
                metrics_path="/metrics",
                attestation_path="/inferdrome/v2/engine-attestation",
                listener_scope="PRIVATE_VPC_ONLY",
                request_logging="DISABLED",
            ),
        ),
        startup_adapter_id="inferdrome.gcp-private-vllm-startup-v2",
        startup_adapter_version="1.0.0",
        no_public_inference=True,
        boot_disk_auto_delete=True,
        provider_max_runtime_delete=True,
    )
    return GcpPrivateCampaignStartupPayload(
        **_model_python_value(payload),
        startup_payload_id=gcp_private_campaign_startup_payload_id(payload),
    )


class GcpPrivateCampaignRoutingBinding(PrecampaignModel):
    """The fixed R1 workload/fault inputs consumed by the existing PR-B runner."""

    execution_id: Literal["routing-execution-v1"]
    source_commit: Commit
    campaign_id: Literal["routing-campaign-v1"]
    plan_sha256: Sha256Digest
    trace_sha256: Sha256Digest
    fault_schedule_sha256: Sha256Digest
    trial_plan_sha256: Sha256Digest
    policy_set_sha256: Sha256Digest
    workload_sha256: Sha256Digest
    selected_workload_sha256: Sha256Digest
    request_denominator: Literal[6]
    evidence_destination_sha256: Sha256Digest

    @model_validator(mode="after")
    def _fixed_r1_inputs(self) -> Self:
        expected = fixed_r1_input_digests()
        if (
            self.plan_sha256 != expected["plan"]
            or self.trace_sha256 != expected["trace"]
            or self.fault_schedule_sha256 != expected["fault"]
            or self.trial_plan_sha256 != expected["trial"]
            or self.policy_set_sha256
            != sha256_digest(canonical_json_bytes(list(fixed_policy_ids())))
            or self.workload_sha256 != qwen3_workload_sha256()
            or self.selected_workload_sha256 != fixed_selected_workload_sha256()
        ):
            raise ValueError("routing binding is not the fixed R1 campaign")
        return self


class GcpPrivateCampaignQuote(PrecampaignModel):
    """Human-reviewed controller estimate, explicitly not an invoice guarantee."""

    quote_id: Annotated[str, StringConstraints(pattern=r"^quote-[a-z0-9]{8,32}$")]
    quoted_at: GcpTimestamp
    expires_at: GcpTimestamp
    currency: Literal["USD"]
    rate_microusd_per_hour: Annotated[int, Field(ge=1, le=10**12)]
    usd_cap_microusd: Annotated[int, Field(ge=1, le=10**12)]
    estimate_only: Literal[True]

    @model_validator(mode="after")
    def _quote_window(self) -> Self:
        if _parse_timestamp(self.expires_at) <= _parse_timestamp(self.quoted_at):
            raise ValueError("quote expiry must follow quote observation")
        return self


def gcp_private_campaign_conservative_estimate_microusd(
    quote: GcpPrivateCampaignQuote,
    *,
    max_runtime_seconds: int,
    cleanup_horizon_seconds: int,
) -> int:
    """Return the bounded controller estimate; it is never an invoice claim.

    The estimate reserves the approved maximum runtime *and* the cleanup
    horizon at the approved hourly rate, rounding upward to one microusd.  It
    is intentionally a simple local guard: it cannot observe provider billing
    and must not be presented as an account limit or invoice guarantee.
    """

    bounded_seconds = max_runtime_seconds + cleanup_horizon_seconds
    return (quote.rate_microusd_per_hour * bounded_seconds + 3_599) // 3_600


class GcpPrivateCampaignRequestIds(PrecampaignModel):
    """Caller-supplied idempotency keys for the exact resource operations."""

    create_request_id: PrecampaignRequestUuid
    delete_request_id: PrecampaignRequestUuid
    boot_disk_delete_request_id: PrecampaignRequestUuid

    @model_validator(mode="after")
    def _distinct_nonzero(self) -> Self:
        values = (
            self.create_request_id,
            self.delete_request_id,
            self.boot_disk_delete_request_id,
        )
        if len(set(values)) != len(values):
            raise ValueError("caller request IDs must be distinct")
        try:
            if any(uuid.UUID(value).int == 0 for value in values):
                raise ValueError("caller request IDs cannot be zero")
        except ValueError:
            raise ValueError("caller request IDs are invalid") from None
        return self


class GcpPrivateCampaignProposalPayload(PrecampaignModel):
    """Everything a human approval must bind before a provider can initialize."""

    schema_version: Literal["inferdrome.gcp-private-campaign-proposal.v2"]
    proposal_kind: Literal["gcp_private_two_a100_precampaign"]
    source_commit: Commit
    topology: GcpPrivateCampaignTopology
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    instance_name: PrecampaignInstanceName
    boot_disk_name: PrecampaignBootDiskName
    boot_image: GcpPrivateCampaignBootImage
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    startup_payload_digest: Sha256Digest
    routing: GcpPrivateCampaignRoutingBinding
    quote: GcpPrivateCampaignQuote
    max_runtime_seconds: Annotated[int, Field(ge=60, le=86_400)]
    cleanup_horizon_seconds: Annotated[int, Field(ge=60, le=86_400)]
    request_ids: GcpPrivateCampaignRequestIds

    @model_validator(mode="after")
    def _proposal_bindings(self) -> Self:
        if self.boot_disk_name != f"{self.instance_name}-boot":
            raise ValueError("boot disk name must be the deterministic exact identity")
        if self.source_commit != self.routing.source_commit:
            raise ValueError("routing source commit disagrees with proposal")
        if self.max_runtime_seconds + self.cleanup_horizon_seconds > 86_400:
            raise ValueError("runtime and cleanup horizon must remain bounded")
        if (
            gcp_private_campaign_conservative_estimate_microusd(
                self.quote,
                max_runtime_seconds=self.max_runtime_seconds,
                cleanup_horizon_seconds=self.cleanup_horizon_seconds,
            )
            > self.quote.usd_cap_microusd
        ):
            raise ValueError("quote cap is below the bounded controller estimate")
        return self


class GcpPrivateCampaignProposal(GcpPrivateCampaignProposalPayload):
    proposal_id: Sha256Digest

    @model_validator(mode="after")
    def _proposal_identity(self) -> Self:
        if self.proposal_id != gcp_private_campaign_proposal_id(self):
            raise ValueError("proposal identity does not match its exact payload")
        return self


def canonical_gcp_private_campaign_proposal_payload_bytes(
    proposal: GcpPrivateCampaignProposal | GcpPrivateCampaignProposalPayload,
) -> bytes:
    value = _model_value(proposal)
    value.pop("proposal_id", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_proposal_id(
    proposal: GcpPrivateCampaignProposal | GcpPrivateCampaignProposalPayload,
) -> Sha256Digest:
    return _digest(canonical_gcp_private_campaign_proposal_payload_bytes(proposal))


def canonical_gcp_private_campaign_proposal_bytes(
    proposal: GcpPrivateCampaignProposal,
) -> bytes:
    return _canonical(proposal)


def issue_gcp_private_campaign_proposal(
    payload: GcpPrivateCampaignProposalPayload,
) -> GcpPrivateCampaignProposal:
    """Content-address a validated proposal without granting execution authority."""

    parsed = GcpPrivateCampaignProposalPayload.model_validate_json(_canonical(payload))
    return GcpPrivateCampaignProposal(
        **_model_python_value(parsed),
        proposal_id=gcp_private_campaign_proposal_id(parsed),
    )


class GcpPrivateCampaignApproval(PrecampaignModel):
    """External human-record binding; it is verified locally before SDK setup."""

    schema_version: Literal["inferdrome.gcp-private-campaign-approval.v2"]
    approval_kind: Literal["exact_human_campaign_approval"]
    confirmation: Literal["AUTHORIZE_GCP_PRIVATE_TWO_A100_PRECAMPAIGN_V2"]
    human_approval_record_id: PrecampaignRecordId
    approved_at: GcpTimestamp
    expires_at: GcpTimestamp
    proposal: GcpPrivateCampaignProposal
    proposal_digest: Sha256Digest

    @model_validator(mode="after")
    def _approval_binding(self) -> Self:
        if _parse_timestamp(self.expires_at) <= _parse_timestamp(self.approved_at):
            raise ValueError("approval expiry must follow approval time")
        if self.proposal_digest != self.proposal.proposal_id:
            raise ValueError("approval proposal digest is inconsistent")
        return self


class GcpPrivateCampaignCleanupAuthorization(PrecampaignModel):
    """Exact cleanup-only authority, intentionally independent of launch expiry."""

    schema_version: Literal["inferdrome.gcp-private-campaign-cleanup-authorization.v2"]
    authorization_kind: Literal["exact_cleanup_recovery"]
    confirmation: Literal["AUTHORIZE_GCP_PRIVATE_EXACT_CLEANUP_V2"]
    cleanup_record_id: PrecampaignRecordId
    authorized_at: GcpTimestamp
    proposal: GcpPrivateCampaignProposal
    proposal_digest: Sha256Digest
    cleanup_request_id: PrecampaignRequestUuid

    @model_validator(mode="after")
    def _cleanup_binding(self) -> Self:
        if self.proposal_digest != self.proposal.proposal_id:
            raise ValueError("cleanup authorization proposal digest is inconsistent")
        if self.cleanup_request_id != self.proposal.request_ids.delete_request_id:
            raise ValueError("cleanup authorization must retain the original delete ID")
        return self


def _strict_parse(
    model: type[PrecampaignModel], payload: PrecampaignModel
) -> PrecampaignModel:
    try:
        raw = _canonical(payload)
        parsed = model.model_validate_json(raw)
    except (TypeError, ValidationError, ValueError):
        raise GcpPrivateCampaignError("LOCAL_CONTRACT_INVALID") from None
    if _canonical(parsed) != raw:
        raise GcpPrivateCampaignError("LOCAL_CONTRACT_NONCANONICAL")
    return parsed


def verify_gcp_private_campaign_approval(
    approval: GcpPrivateCampaignApproval,
    *,
    expected_proposal: GcpPrivateCampaignProposal,
    now: datetime,
) -> GcpPrivateCampaignApproval:
    """Verify all exact launch bindings without constructing a transport."""

    parsed_approval = cast(
        GcpPrivateCampaignApproval,
        _strict_parse(GcpPrivateCampaignApproval, approval),
    )
    parsed_proposal = cast(
        GcpPrivateCampaignProposal,
        _strict_parse(GcpPrivateCampaignProposal, expected_proposal),
    )
    if parsed_approval.proposal != parsed_proposal:
        raise GcpPrivateCampaignError("APPROVAL_PROPOSAL_MISMATCH")
    now_timestamp = _timestamp(now)
    if now_timestamp < parsed_approval.approved_at:
        raise GcpPrivateCampaignError("APPROVAL_NOT_YET_VALID")
    if now_timestamp >= parsed_approval.expires_at:
        raise GcpPrivateCampaignError("APPROVAL_EXPIRED")
    if now_timestamp < parsed_proposal.quote.quoted_at:
        raise GcpPrivateCampaignError("QUOTE_NOT_YET_VALID")
    if now_timestamp >= parsed_proposal.quote.expires_at:
        raise GcpPrivateCampaignError("QUOTE_EXPIRED")
    return parsed_approval


def verify_gcp_private_campaign_evidence_destination(
    proposal: GcpPrivateCampaignProposal, *, evidence_root: Path
) -> SafeDirFD:
    """Bind a safe local evidence root before the provider factory is reachable.

    This is deliberately a local filesystem/identity check rather than a
    provider preflight.  The runner repeats the held-directory and visible-path
    checks at handoff, so a post-check replacement cannot silently redirect
    evidence after an otherwise approved launch.
    """

    selected = evidence_root.absolute()
    if (
        sha256_digest(str(selected).encode("utf-8"))
        != proposal.routing.evidence_destination_sha256
    ):
        raise GcpPrivateCampaignError("EVIDENCE_DESTINATION_MISMATCH")
    root: SafeDirFD | None = None
    try:
        root = SafeDirFD.open(selected)
        root.assert_open()
    except (OSError, SafeDirFSError):
        if root is not None:
            root.close()
        raise GcpPrivateCampaignError("EVIDENCE_DESTINATION_UNAVAILABLE") from None
    assert root is not None
    return root


def verify_gcp_private_campaign_cleanup_authorization(
    authorization: GcpPrivateCampaignCleanupAuthorization,
    *,
    expected_proposal: GcpPrivateCampaignProposal,
    now: datetime,
) -> GcpPrivateCampaignCleanupAuthorization:
    """Verify cleanup scope without consulting a stale launch approval."""

    parsed = cast(
        GcpPrivateCampaignCleanupAuthorization,
        _strict_parse(GcpPrivateCampaignCleanupAuthorization, authorization),
    )
    proposal = cast(
        GcpPrivateCampaignProposal,
        _strict_parse(GcpPrivateCampaignProposal, expected_proposal),
    )
    if parsed.proposal != proposal:
        raise GcpPrivateCampaignError("CLEANUP_AUTHORIZATION_MISMATCH")
    if _timestamp(now) < parsed.authorized_at:
        raise GcpPrivateCampaignError("CLEANUP_AUTHORIZATION_NOT_YET_VALID")
    return parsed


class GcpPrivateCampaignProviderBackstop(PrecampaignModel):
    """Provider-native bounded termination facts that must precede create."""

    max_runtime_seconds: Annotated[int, Field(ge=60, le=86_400)]
    instance_termination_action: Literal["DELETE"]
    boot_disk_auto_delete: Literal[True]
    persistent_disk_count: Literal[0]
    no_external_access: Literal[True]


class GcpPrivateCampaignCreateRequest(PrecampaignModel):
    """The exact request projected to an injected provider transport."""

    schema_version: Literal["inferdrome.gcp-private-campaign-create-request.v2"]
    proposal: GcpPrivateCampaignProposal
    startup_payload: GcpPrivateCampaignStartupPayload
    provider_backstop: GcpPrivateCampaignProviderBackstop
    create_request_digest: Sha256Digest

    @model_validator(mode="after")
    def _request_binding(self) -> Self:
        if (
            self.startup_payload.startup_payload_id
            != self.proposal.startup_payload_digest
        ):
            raise ValueError("startup payload digest disagrees with proposal")
        if self.startup_payload.source_commit != self.proposal.source_commit:
            raise ValueError("startup payload source commit disagrees with proposal")
        if self.startup_payload.runner_image != self.proposal.runner_image:
            raise ValueError("startup runner image disagrees with proposal")
        if any(
            engine.serving_image != self.proposal.serving_image
            for engine in self.startup_payload.engines
        ):
            raise ValueError("startup serving image disagrees with proposal")
        if any(
            engine.model != self.proposal.model
            or engine.runtime != self.proposal.runtime
            for engine in self.startup_payload.engines
        ):
            raise ValueError("startup model or runtime disagrees with proposal")
        expected_backstop = GcpPrivateCampaignProviderBackstop(
            max_runtime_seconds=self.proposal.max_runtime_seconds,
            instance_termination_action="DELETE",
            boot_disk_auto_delete=True,
            persistent_disk_count=0,
            no_external_access=True,
        )
        if self.provider_backstop != expected_backstop:
            raise ValueError("provider backstop disagrees with proposal")
        if self.create_request_digest != gcp_private_campaign_create_request_digest(
            self
        ):
            raise ValueError("create request identity does not match request")
        return self


def canonical_gcp_private_campaign_create_request_payload_bytes(
    request: GcpPrivateCampaignCreateRequest,
) -> bytes:
    value = _model_value(request)
    value.pop("create_request_digest", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_create_request_digest(
    request: GcpPrivateCampaignCreateRequest,
) -> Sha256Digest:
    return _digest(canonical_gcp_private_campaign_create_request_payload_bytes(request))


def build_gcp_private_campaign_create_request(
    *,
    proposal: GcpPrivateCampaignProposal,
    startup_payload: GcpPrivateCampaignStartupPayload,
) -> GcpPrivateCampaignCreateRequest:
    """Build and reparse the provider request before a factory may be called."""

    try:
        proposal = GcpPrivateCampaignProposal.model_validate_json(
            canonical_gcp_private_campaign_proposal_bytes(proposal)
        )
        startup_payload = GcpPrivateCampaignStartupPayload.model_validate_json(
            _canonical(startup_payload)
        )
    except (ValidationError, ValueError, TypeError):
        raise GcpPrivateCampaignError("CREATE_REQUEST_INPUT_INVALID") from None
    request_without_digest = GcpPrivateCampaignCreateRequest.model_construct(
        schema_version=GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_VERSION,
        proposal=proposal,
        startup_payload=startup_payload,
        provider_backstop=GcpPrivateCampaignProviderBackstop(
            max_runtime_seconds=proposal.max_runtime_seconds,
            instance_termination_action="DELETE",
            boot_disk_auto_delete=True,
            persistent_disk_count=0,
            no_external_access=True,
        ),
        create_request_digest="sha256:" + ("0" * 64),
    )
    digest = gcp_private_campaign_create_request_digest(request_without_digest)
    try:
        value = _model_python_value(request_without_digest)
        value["create_request_digest"] = digest
        return GcpPrivateCampaignCreateRequest(**value)
    except (ValidationError, ValueError, TypeError):
        raise GcpPrivateCampaignError("CREATE_REQUEST_INVALID") from None


class GcpPrivateCampaignOperation(PrecampaignModel):
    operation_id: PrecampaignOperationId
    operation_kind: Literal["create", "delete_instance", "delete_boot_disk"]
    request_id: PrecampaignRequestUuid
    provider_operation_name: Annotated[
        str, StringConstraints(pattern=_PROVIDER_OPERATION_RE.pattern)
    ]


class GcpPrivateCampaignOperationResult(PrecampaignModel):
    operation: GcpPrivateCampaignOperation
    status: Literal["DONE", "ERROR", "TIMEOUT"]
    error_code: Annotated[str | None, StringConstraints(pattern=_ERROR_RE.pattern)] = (
        None
    )

    @model_validator(mode="after")
    def _operation_result(self) -> Self:
        if (self.status == "ERROR") != (self.error_code is not None):
            raise ValueError("operation result error fields are inconsistent")
        if self.status != "ERROR" and self.error_code is not None:
            raise ValueError("non-error operation result cannot contain an error")
        return self


class GcpPrivateCampaignInstanceObservation(PrecampaignModel):
    """Exact provider readback needed before routing admission."""

    proposal_id: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: PrecampaignInstanceName
    provider_instance_id: Annotated[
        str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ]
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    machine_type: Literal["a2-highgpu-2g"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: Literal["nvidia-tesla-a100"]
    accelerator_count: Literal[2]
    machine_fixed_local_ssd_count: Literal[2]
    state: Literal["RUNNING", "NOT_FOUND"]
    external_access: Literal["ABSENT"]
    boot_disk_name: PrecampaignBootDiskName | None
    boot_disk_provider_id_sha256: Sha256Digest
    boot_disk_auto_delete: Literal[True] | None
    persistent_disk_count: Literal[0] | None
    max_runtime_seconds: Annotated[int | None, Field(ge=60, le=86_400)]
    instance_termination_action: Literal["DELETE"] | None
    startup_payload_digest: Sha256Digest | None
    startup_script_sha256: Sha256Digest | None


class GcpPrivateCampaignExactResourceBinding(PrecampaignModel):
    """A journal-safe binding for one observed provider instance.

    The controller deliberately records only a SHA-256 of the provider
    instance ID.  It can require a live readback to match that value before an
    exact delete without serializing a provider instance ID into the journal,
    evidence, or a command line.
    """

    proposal_id: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: PrecampaignInstanceName
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    provider_instance_id_sha256: Sha256Digest
    boot_disk_provider_id_sha256: Sha256Digest


class GcpPrivateCampaignEndpointReadiness(PrecampaignModel):
    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    gpu_ordinal: Literal[0, 1]
    private_port: Literal[8000, 8001]
    health_http_status: Literal[200]
    generation_http_status: Literal[200]
    metrics_metric_name: Literal["vllm:num_requests_running"]
    health_capability: Literal["HTTP_HEALTH_V1"]
    metrics_capability: Literal["VLLM_PROMETHEUS_V1"]
    engine_attestation_capability: Literal["INFERDROME_ENGINE_ATTESTATION_V2"]
    engine_attestation_sha256: Sha256Digest
    observation_epoch: Annotated[int, Field(ge=1, le=1_000_000)]
    observed_monotonic_ns: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _endpoint_slot(self) -> Self:
        expected = {"endpoint-a": (0, 8000), "endpoint-b": (1, 8001)}[self.endpoint_id]
        if (self.gpu_ordinal, self.private_port) != expected:
            raise ValueError("readiness endpoint does not match its fixed GPU/port")
        return self


class GcpPrivateCampaignReadiness(PrecampaignModel):
    """Observed topology and two capability probes, not a provider payload dump."""

    schema_version: Literal["inferdrome.gcp-private-campaign-readiness.v2"]
    proposal_id: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    instance_name: PrecampaignInstanceName
    provider_instance_id: Annotated[
        str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ]
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    private_ipv4: Annotated[str, StringConstraints(max_length=15)]
    machine_type: Literal["a2-highgpu-2g"]
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_count: Literal[2]
    startup_payload_digest: Sha256Digest
    startup_script_sha256: Sha256Digest
    endpoints: tuple[
        GcpPrivateCampaignEndpointReadiness, GcpPrivateCampaignEndpointReadiness
    ]
    observed_at: GcpTimestamp

    @model_validator(mode="after")
    def _ready_inventory(self) -> Self:
        if not _private_ipv4(self.private_ipv4):
            raise ValueError("readiness must retain one literal RFC1918 address")
        if tuple(endpoint.endpoint_id for endpoint in self.endpoints) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("readiness needs ordered endpoint-a and endpoint-b")
        return self


class GcpPrivateCampaignDiskObservation(PrecampaignModel):
    disk_name: PrecampaignBootDiskName
    provider_disk_id_sha256: Sha256Digest
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    source_boot_image_identity: Sha256Digest
    attached_provider_instance_id: Annotated[
        str | None, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ] = None
    attachment_state: Literal["ATTACHED", "DETACHED"]
    boot_attachment: Literal[True]


class GcpPrivateCampaignOwnedResidualInventory(PrecampaignModel):
    proposal_id: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    pagination_complete: Literal[True]
    instance_state: Literal["PRESENT", "ABSENT"]
    disks: tuple[GcpPrivateCampaignDiskObservation, ...]


class GcpPrivateCampaignAbsenceObservation(PrecampaignModel):
    proposal_id: Sha256Digest
    project_id: GcpProjectId
    zone: GcpZone
    ownership_labels: GcpPrivateCampaignOwnershipLabels
    pagination_complete: Literal[True]
    instance_absent: Literal[True]
    boot_disk_absent: Literal[True]
    no_other_owned_billable_residuals: Literal[True]


class GcpPrivateCampaignHandoffReceipt(PrecampaignModel):
    """Sanitized bridge receipt; it never contains an IP, prompt, or output."""

    proposal_id: Sha256Digest
    routing_config_sha256: Sha256Digest
    selected_workload_sha256: Sha256Digest
    endpoint_a_origin_sha256: Sha256Digest
    endpoint_b_origin_sha256: Sha256Digest
    routed_after_verified_readiness: Literal[True]


class GcpPrivateCampaignEvidenceReceipt(PrecampaignModel):
    proposal_id: Sha256Digest
    retained_digest: Sha256Digest
    collection_mode: Literal["CREATE_NO_REPLACE_RETRIEVAL"]
    raw_prompt_or_output_retained: Literal[False]


GcpPrivateCampaignJournalState = Literal[
    "BACKSTOP_READY",
    "CREATE_INTENT",
    "CREATE_SUBMITTED",
    "CREATE_RECONCILING",
    "CREATED",
    "INSTANCE_IDENTITY_BOUND",
    "BOOT_DISK_IDENTITY_BOUND",
    "READINESS_VERIFIED",
    "CAMPAIGN_HANDED_OFF",
    "EVIDENCE_RETRIEVED",
    "CLEANUP_INTENT",
    "INSTANCE_DELETE_SUBMITTED",
    "INSTANCE_DELETE_RECONCILING",
    "BOOT_DISK_DELETE_INTENT",
    "BOOT_DISK_DELETE_SUBMITTED",
    "CLEANUP_CONFIRMED",
    "CLEANUP_UNCONFIRMED",
    "CLEANUP_DEADLINE_EXCEEDED",
    "BLOCKED",
]


class GcpPrivateCampaignJournalEventPayload(PrecampaignModel):
    schema_version: Literal["inferdrome.gcp-private-campaign-journal-event.v2"]
    proposal_id: Sha256Digest
    proposal_digest: Sha256Digest
    sequence: Annotated[int, Field(ge=0, le=GCP_PRIVATE_CAMPAIGN_MAX_EVENTS - 1)]
    state: GcpPrivateCampaignJournalState
    occurred_at: GcpTimestamp
    previous_event_digest: Sha256Digest | None
    operation_id: PrecampaignOperationId | None = None
    detail_digest: Sha256Digest | None = None


class GcpPrivateCampaignJournalEvent(GcpPrivateCampaignJournalEventPayload):
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def _event_identity(self) -> Self:
        if self.event_digest != gcp_private_campaign_journal_event_digest(self):
            raise ValueError("journal event identity does not match payload")
        return self


def canonical_gcp_private_campaign_journal_event_payload_bytes(
    event: GcpPrivateCampaignJournalEvent | GcpPrivateCampaignJournalEventPayload,
) -> bytes:
    value = _model_value(event)
    value.pop("event_digest", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_journal_event_digest(
    event: GcpPrivateCampaignJournalEvent | GcpPrivateCampaignJournalEventPayload,
) -> Sha256Digest:
    return _journal_digest(
        canonical_gcp_private_campaign_journal_event_payload_bytes(event)
    )


_TRANSITIONS: Final[
    dict[GcpPrivateCampaignJournalState, set[GcpPrivateCampaignJournalState]]
] = {
    "BACKSTOP_READY": {"CREATE_INTENT", "CLEANUP_INTENT", "BLOCKED"},
    "CREATE_INTENT": {
        "CREATE_SUBMITTED",
        "CREATE_RECONCILING",
        "CLEANUP_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "BLOCKED",
    },
    "CREATE_SUBMITTED": {"CREATE_RECONCILING", "CREATED", "CLEANUP_INTENT", "BLOCKED"},
    "CREATE_RECONCILING": {"CREATED", "CLEANUP_INTENT", "BLOCKED"},
    "CREATED": {"INSTANCE_IDENTITY_BOUND", "CLEANUP_INTENT", "BLOCKED"},
    "INSTANCE_IDENTITY_BOUND": {
        "BOOT_DISK_IDENTITY_BOUND",
        "CLEANUP_INTENT",
        "BLOCKED",
    },
    "BOOT_DISK_IDENTITY_BOUND": {
        "READINESS_VERIFIED",
        "CLEANUP_INTENT",
        "BLOCKED",
    },
    "READINESS_VERIFIED": {
        "CAMPAIGN_HANDED_OFF",
        "CLEANUP_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "BLOCKED",
    },
    "CAMPAIGN_HANDED_OFF": {
        "EVIDENCE_RETRIEVED",
        "CLEANUP_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "BLOCKED",
    },
    "EVIDENCE_RETRIEVED": {
        "CLEANUP_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "BLOCKED",
    },
    "CLEANUP_INTENT": {
        "INSTANCE_DELETE_SUBMITTED",
        "INSTANCE_DELETE_RECONCILING",
        "BOOT_DISK_DELETE_INTENT",
        "CLEANUP_CONFIRMED",
        "CLEANUP_DEADLINE_EXCEEDED",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "INSTANCE_DELETE_SUBMITTED": {
        "INSTANCE_DELETE_RECONCILING",
        "BOOT_DISK_DELETE_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "CLEANUP_CONFIRMED",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "INSTANCE_DELETE_RECONCILING": {
        "BOOT_DISK_DELETE_INTENT",
        "CLEANUP_DEADLINE_EXCEEDED",
        "CLEANUP_CONFIRMED",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "BOOT_DISK_DELETE_INTENT": {
        "BOOT_DISK_DELETE_SUBMITTED",
        "CLEANUP_DEADLINE_EXCEEDED",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "BOOT_DISK_DELETE_SUBMITTED": {
        "CLEANUP_CONFIRMED",
        "CLEANUP_DEADLINE_EXCEEDED",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "CLEANUP_UNCONFIRMED": {
        "CLEANUP_INTENT",
        "CLEANUP_UNCONFIRMED",
        "CLEANUP_DEADLINE_EXCEEDED",
        "BLOCKED",
    },
    "CLEANUP_DEADLINE_EXCEEDED": {
        "CLEANUP_INTENT",
        "CLEANUP_UNCONFIRMED",
        "BLOCKED",
    },
    "BLOCKED": {"CLEANUP_INTENT", "CLEANUP_UNCONFIRMED", "BLOCKED"},
    "CLEANUP_CONFIRMED": {"CLEANUP_CONFIRMED"},
}


class GcpPrivateCampaignJournal:
    """No-follow, fsync, hash-chained lifecycle journal for one exact proposal."""

    def __init__(
        self, root: Path, *, crash_hook: Callable[[str], None] | None = None
    ) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._crash_hook = crash_hook

    @staticmethod
    def _name(proposal: GcpPrivateCampaignProposal) -> str:
        controller = proposal.ownership_labels.controller_id
        return f"{controller}.gcp-private-campaign-v2.events.jsonl"

    @staticmethod
    def _lock_name(proposal: GcpPrivateCampaignProposal) -> str:
        return (
            f".{proposal.ownership_labels.controller_id}.gcp-private-campaign-v2.lock"
        )

    def _open_root(self) -> SafeDirFD:
        if not self.root.is_absolute():
            raise GcpPrivateCampaignError("JOURNAL_PATH_INVALID")
        try:
            return SafeDirFD.open(self.root)
        except (SafeDirFSError, FileNotFoundError):
            raise GcpPrivateCampaignError("JOURNAL_UNSAFE") from None
        except OSError:
            raise GcpPrivateCampaignError("JOURNAL_UNAVAILABLE") from None

    @contextmanager
    def _exclusive(self, proposal: GcpPrivateCampaignProposal) -> Iterator[SafeDirFD]:
        root = self._open_root()
        descriptor: int | None = None
        try:
            descriptor = root.open_child(
                self._lock_name(proposal), os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpPrivateCampaignError("JOURNAL_LOCK_UNAVAILABLE") from None
            with self._lock:
                yield root
        except GcpPrivateCampaignError:
            raise
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError("JOURNAL_UNSAFE") from None
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)
            root.close()

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short journal write")
            view = view[written:]

    def _read_locked(
        self, root: SafeDirFD, proposal: GcpPrivateCampaignProposal, *, missing_ok: bool
    ) -> tuple[GcpPrivateCampaignJournalEvent, ...]:
        descriptor: int | None = None
        name = self._name(proposal)
        try:
            descriptor = root.open_child(name, os.O_RDONLY)
            before = root.validated_regular_child(name, descriptor=descriptor)
            if (
                before.st_size < 1
                or before.st_size > GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES
            ):
                raise GcpPrivateCampaignError("JOURNAL_INVALID")
            content = os.read(descriptor, GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES + 1)
            after = root.validated_regular_child(name, descriptor=descriptor)
            if len(
                content
            ) > GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES or after.st_size != len(content):
                raise GcpPrivateCampaignError("JOURNAL_CHANGED")
            if not content.endswith(b"\n"):
                raise GcpPrivateCampaignError("JOURNAL_PARTIAL_TAIL")
            rows = content.splitlines()
            if not rows or len(rows) > GCP_PRIVATE_CAMPAIGN_MAX_EVENTS:
                raise GcpPrivateCampaignError("JOURNAL_INVALID")
            events: list[GcpPrivateCampaignJournalEvent] = []
            previous: Sha256Digest | None = None
            for sequence, row in enumerate(rows):
                event = GcpPrivateCampaignJournalEvent.model_validate_json(row)
                if canonical_json_bytes(_model_value(event)) != row:
                    raise GcpPrivateCampaignError("JOURNAL_NONCANONICAL")
                if (
                    event.sequence != sequence
                    or event.previous_event_digest != previous
                    or event.proposal_id != proposal.proposal_id
                    or event.proposal_digest != proposal.proposal_id
                ):
                    raise GcpPrivateCampaignError("JOURNAL_CHAIN_INVALID")
                previous = event.event_digest
                events.append(event)
            self._validate_transitions(events)
            return tuple(events)
        except FileNotFoundError:
            if missing_ok:
                return ()
            raise GcpPrivateCampaignError("JOURNAL_MISSING") from None
        except GcpPrivateCampaignError:
            raise
        except (OSError, SafeDirFSError, ValidationError, ValueError):
            raise GcpPrivateCampaignError("JOURNAL_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate_transitions(events: Sequence[GcpPrivateCampaignJournalEvent]) -> None:
        if not events or events[0].state != "BACKSTOP_READY":
            raise GcpPrivateCampaignError("JOURNAL_TRANSITION_INVALID")
        for previous, current in pairwise(events):
            if current.state not in _TRANSITIONS[previous.state]:
                raise GcpPrivateCampaignError("JOURNAL_TRANSITION_INVALID")
            if _parse_timestamp(current.occurred_at) < _parse_timestamp(
                previous.occurred_at
            ):
                raise GcpPrivateCampaignError("JOURNAL_TIME_REGRESSED")

    def append(
        self,
        proposal: GcpPrivateCampaignProposal,
        *,
        state: GcpPrivateCampaignJournalState,
        occurred_at: datetime,
        operation_id: str | None = None,
        detail_digest: str | None = None,
    ) -> GcpPrivateCampaignJournalEvent:
        """Durably record a state before its corresponding provider mutation."""

        with self._exclusive(proposal) as root:
            events = self._read_locked(root, proposal, missing_ok=True)
            if not events and state != "BACKSTOP_READY":
                raise GcpPrivateCampaignError("JOURNAL_TRANSITION_INVALID")
            if events and state not in _TRANSITIONS[events[-1].state]:
                raise GcpPrivateCampaignError("JOURNAL_TRANSITION_INVALID")
            payload = GcpPrivateCampaignJournalEventPayload(
                schema_version=GCP_PRIVATE_CAMPAIGN_EVENT_SCHEMA_VERSION,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_id,
                sequence=len(events),
                state=state,
                occurred_at=_timestamp(occurred_at),
                previous_event_digest=events[-1].event_digest if events else None,
                operation_id=operation_id,
                detail_digest=detail_digest,
            )
            event = GcpPrivateCampaignJournalEvent(
                **_model_value(payload),
                event_digest=gcp_private_campaign_journal_event_digest(payload),
            )
            descriptor: int | None = None
            name = self._name(proposal)
            try:
                descriptor = root.open_child(
                    name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
                )
                root.validated_regular_child(name, descriptor=descriptor)
                self._write_all(
                    descriptor, canonical_json_bytes(_model_value(event)) + b"\n"
                )
                os.fsync(descriptor)
                root.validated_regular_child(name, descriptor=descriptor)
                root.fsync()
            except GcpPrivateCampaignError:
                raise
            except (OSError, SafeDirFSError):
                raise GcpPrivateCampaignError("JOURNAL_UNAVAILABLE") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if self._crash_hook is not None:
                self._crash_hook(f"after-{state.lower()}")
            return event

    def load(
        self, proposal: GcpPrivateCampaignProposal
    ) -> tuple[GcpPrivateCampaignJournalEvent, ...]:
        with self._exclusive(proposal) as root:
            return self._read_locked(root, proposal, missing_ok=False)

    def exact_resource_binding(
        self, proposal: GcpPrivateCampaignProposal
    ) -> GcpPrivateCampaignExactResourceBinding | None:
        """Recover a prior hashed instance identity without exposing its ID."""

        instance_id_sha256: Sha256Digest | None = None
        boot_disk_id_sha256: Sha256Digest | None = None
        for event in self.load(proposal):
            if event.state == "INSTANCE_IDENTITY_BOUND":
                if event.detail_digest is None or instance_id_sha256 is not None:
                    raise GcpPrivateCampaignError("JOURNAL_IDENTITY_INVALID")
                instance_id_sha256 = event.detail_digest
            elif event.state == "BOOT_DISK_IDENTITY_BOUND":
                if event.detail_digest is None or boot_disk_id_sha256 is not None:
                    raise GcpPrivateCampaignError("JOURNAL_IDENTITY_INVALID")
                boot_disk_id_sha256 = event.detail_digest
        # A crash between identity records leaves only the provider-native
        # termination backstop.  Recovery must not guess a deletion target.
        if instance_id_sha256 is None and boot_disk_id_sha256 is None:
            return None
        if instance_id_sha256 is None or boot_disk_id_sha256 is None:
            return None
        return GcpPrivateCampaignExactResourceBinding(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            ownership_labels=proposal.ownership_labels,
            provider_instance_id_sha256=instance_id_sha256,
            boot_disk_provider_id_sha256=boot_disk_id_sha256,
        )

    def cleanup_deadline(self, proposal: GcpPrivateCampaignProposal) -> datetime:
        """Derive the durable cleanup deadline from the create-intent record.

        Starting the horizon at the fsynced create intent is conservative: an
        adapter delay cannot extend the approved time available for launch,
        handoff, or destructive cleanup.  The provider-native max-runtime
        DELETE backstop remains independent of this local controller deadline.
        """

        for event in self.load(proposal):
            if event.state == "CREATE_INTENT":
                return _parse_timestamp(event.occurred_at) + timedelta(
                    seconds=proposal.cleanup_horizon_seconds
                )
        raise GcpPrivateCampaignError("JOURNAL_DEADLINE_MISSING")


class GcpPrivateCampaignTransport(Protocol):
    """Exact-resource provider and runner boundary; no broad list/delete APIs."""

    def create_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation: ...

    def reconcile_create(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult: ...

    def wait_operation(
        self,
        operation: GcpPrivateCampaignOperation,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult: ...

    def observe_exact_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignInstanceObservation: ...

    def observe_readiness(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignReadiness: ...

    def handoff_campaign(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        routing_config: bytes,
        workload: bytes,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignHandoffReceipt: ...

    def retrieve_evidence(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignEvidenceReceipt: ...

    def delete_exact_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation: ...

    def reconcile_delete_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult: ...

    def list_exact_owned_residuals(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOwnedResidualInventory: ...

    def delete_exact_owned_boot_disk(
        self,
        request: GcpPrivateCampaignCreateRequest,
        disk: GcpPrivateCampaignDiskObservation,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation: ...

    def reconcile_delete_exact_owned_boot_disk(
        self,
        request: GcpPrivateCampaignCreateRequest,
        disk: GcpPrivateCampaignDiskObservation,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult: ...

    def confirm_exact_absence(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignAbsenceObservation: ...

    def discover_exact_owned(
        self,
        proposal: GcpPrivateCampaignProposal,
        *,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOwnedResidualInventory: ...


@runtime_checkable
class GcpPrivateCampaignEvidenceRootBindingTransport(Protocol):
    """A transport that can retain the exact preflight evidence-root fd."""

    def bind_evidence_root(self, root: SafeDirFD) -> None: ...


class GcpPrivateCampaignClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@dataclass(frozen=True)
class SystemGcpPrivateCampaignClock:
    """Default local clock; it performs no provider or credential lookup."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        import time

        return time.monotonic_ns()


@dataclass(frozen=True)
class GcpPrivateCampaignCleanupOutcome:
    confirmed: bool
    journal_state: GcpPrivateCampaignJournalState


class GcpPrivateCampaignLifecycleController:
    """Approval-gated exact lifecycle owner for the two-engine profile."""

    def __init__(
        self,
        *,
        journal: GcpPrivateCampaignJournal,
        transport_factory: Callable[[], GcpPrivateCampaignTransport],
        launch_preflight: (
            Callable[[GcpPrivateCampaignProposal], SafeDirFD | None] | None
        ) = None,
        clock: GcpPrivateCampaignClock | None = None,
        operation_timeout_seconds: int = 60,
    ) -> None:
        if (
            not callable(transport_factory)
            or (launch_preflight is not None and not callable(launch_preflight))
            or not 1 <= operation_timeout_seconds <= 600
        ):
            raise GcpPrivateCampaignError("CONTROLLER_CONFIGURATION_INVALID")
        self._journal = journal
        self._transport_factory = transport_factory
        self._launch_preflight = launch_preflight
        self._clock = clock or SystemGcpPrivateCampaignClock()
        self._operation_timeout_seconds = operation_timeout_seconds

    def _transport_after_authority(self) -> GcpPrivateCampaignTransport:
        try:
            transport = self._transport_factory()
        except BaseException:
            raise GcpPrivateCampaignError("TRANSPORT_FACTORY_UNAVAILABLE") from None
        if transport is None:
            raise GcpPrivateCampaignError("TRANSPORT_FACTORY_UNAVAILABLE")
        return transport

    @staticmethod
    def _bind_evidence_root(
        transport: GcpPrivateCampaignTransport, root: SafeDirFD
    ) -> None:
        """Transfer a duplicate of the preflight root only to a capable adapter."""

        if not isinstance(transport, GcpPrivateCampaignEvidenceRootBindingTransport):
            raise GcpPrivateCampaignError("EVIDENCE_ROOT_BINDING_UNAVAILABLE")
        try:
            root.assert_open()
            transport.bind_evidence_root(root)
        except GcpPrivateCampaignError:
            raise
        except (OSError, SafeDirFSError, ValueError):
            raise GcpPrivateCampaignError("EVIDENCE_ROOT_BINDING_UNAVAILABLE") from None

    @staticmethod
    def _assert_operation(
        operation: GcpPrivateCampaignOperation,
        *,
        kind: Literal["create", "delete_instance", "delete_boot_disk"],
        request_id: str,
    ) -> None:
        if operation.operation_kind != kind or operation.request_id != request_id:
            raise GcpPrivateCampaignError("OPERATION_IDENTITY_MISMATCH")

    @staticmethod
    def _assert_done(
        result: GcpPrivateCampaignOperationResult,
        *,
        operation: GcpPrivateCampaignOperation,
    ) -> None:
        if result.operation != operation or result.status != "DONE":
            raise GcpPrivateCampaignError("OPERATION_NOT_CONFIRMED")

    def _assert_instance(
        self,
        observed: GcpPrivateCampaignInstanceObservation,
        request: GcpPrivateCampaignCreateRequest,
    ) -> None:
        proposal = request.proposal
        if observed.state != "RUNNING" or any(
            (
                observed.proposal_id != proposal.proposal_id,
                observed.project_id != proposal.topology.project_id,
                observed.zone != proposal.topology.zone,
                observed.instance_name != proposal.instance_name,
                observed.ownership_labels != proposal.ownership_labels,
                observed.machine_type != proposal.topology.machine_type,
                observed.accelerator_model != proposal.topology.accelerator_model,
                observed.accelerator_provider_type
                != proposal.topology.accelerator_provider_type,
                observed.accelerator_count != proposal.topology.accelerator_count,
                observed.machine_fixed_local_ssd_count
                != proposal.topology.machine_fixed_local_ssd_count,
                observed.external_access != "ABSENT",
                observed.boot_disk_name != proposal.boot_disk_name,
                observed.boot_disk_auto_delete is not True,
                observed.persistent_disk_count != 0,
                observed.max_runtime_seconds != proposal.max_runtime_seconds,
                observed.instance_termination_action != "DELETE",
                observed.startup_payload_digest != proposal.startup_payload_digest,
                observed.startup_script_sha256 is None,
            )
        ):
            raise GcpPrivateCampaignError("OBSERVED_TOPOLOGY_DRIFT")

    @staticmethod
    def _exact_resource_binding(
        observed: GcpPrivateCampaignInstanceObservation,
        proposal: GcpPrivateCampaignProposal,
    ) -> GcpPrivateCampaignExactResourceBinding:
        if any(
            (
                observed.proposal_id != proposal.proposal_id,
                observed.project_id != proposal.topology.project_id,
                observed.zone != proposal.topology.zone,
                observed.instance_name != proposal.instance_name,
                observed.ownership_labels != proposal.ownership_labels,
            )
        ):
            raise GcpPrivateCampaignError("OBSERVED_RESOURCE_IDENTITY_MISMATCH")
        return GcpPrivateCampaignExactResourceBinding(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            ownership_labels=proposal.ownership_labels,
            provider_instance_id_sha256=sha256_digest(
                observed.provider_instance_id.encode("ascii")
            ),
            boot_disk_provider_id_sha256=observed.boot_disk_provider_id_sha256,
        )

    def _assert_readiness(
        self,
        readiness: GcpPrivateCampaignReadiness,
        request: GcpPrivateCampaignCreateRequest,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        observed_startup_script_sha256: Sha256Digest | None,
    ) -> None:
        proposal = request.proposal
        if (
            sha256_digest(readiness.provider_instance_id.encode("ascii"))
            != exact_resource_binding.provider_instance_id_sha256
        ):
            raise GcpPrivateCampaignError("READINESS_RESOURCE_IDENTITY_MISMATCH")
        if (
            observed_startup_script_sha256 is None
            or readiness.startup_script_sha256 != observed_startup_script_sha256
        ):
            raise GcpPrivateCampaignError("READINESS_STARTUP_SCRIPT_MISMATCH")
        if any(
            (
                readiness.proposal_id != proposal.proposal_id,
                readiness.project_id != proposal.topology.project_id,
                readiness.zone != proposal.topology.zone,
                readiness.instance_name != proposal.instance_name,
                readiness.ownership_labels != proposal.ownership_labels,
                readiness.machine_type != proposal.topology.machine_type,
                readiness.accelerator_model != proposal.topology.accelerator_model,
                readiness.accelerator_count != proposal.topology.accelerator_count,
                readiness.startup_payload_digest != proposal.startup_payload_digest,
            )
        ):
            raise GcpPrivateCampaignError("READINESS_TOPOLOGY_DRIFT")
        now_ns = self._clock.monotonic_ns()
        for endpoint in readiness.endpoints:
            if (
                now_ns < endpoint.observed_monotonic_ns
                or now_ns - endpoint.observed_monotonic_ns
                > GCP_PRIVATE_CAMPAIGN_READINESS_MAX_AGE_NS
            ):
                raise GcpPrivateCampaignError("READINESS_STALE")

    def _build_handoff(
        self,
        proposal: GcpPrivateCampaignProposal,
        readiness: GcpPrivateCampaignReadiness,
    ) -> tuple[bytes, bytes, Sha256Digest]:
        # Import lazily to keep the pure deployment contracts importable even
        # when the PR-B bridge is not being invoked.
        from inferdrome.deployment.gcp_private_routing_handoff import (
            build_gcp_private_routing_execution_inputs,
        )

        handoff = build_gcp_private_routing_execution_inputs(proposal, readiness)
        return (
            handoff.routing_config_bytes,
            handoff.workload_bytes,
            handoff.routing_config_sha256,
        )

    def execute(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        approval: GcpPrivateCampaignApproval,
        startup_payload: GcpPrivateCampaignStartupPayload,
    ) -> GcpPrivateCampaignEvidenceReceipt:
        """Run one exact campaign lifecycle after all local authority gates pass."""

        # These operations are intentionally before journal creation and before
        # the transport factory, so malformed approvals cannot initialize an
        # SDK, ADC, or network client.  A returned root remains held until the
        # entire lifecycle returns, including the runner's final receipt check.
        verify_gcp_private_campaign_approval(
            approval, expected_proposal=proposal, now=self._clock.now()
        )
        preflight_root: SafeDirFD | None = None
        if self._launch_preflight is not None:
            try:
                candidate = self._launch_preflight(proposal)
                if candidate is not None:
                    if not isinstance(candidate, SafeDirFD):
                        raise GcpPrivateCampaignError("LAUNCH_PREFLIGHT_FAILED")
                    preflight_root = candidate
                    preflight_root.assert_open()
            except GcpPrivateCampaignError:
                if preflight_root is not None:
                    preflight_root.close()
                raise
            except (OSError, ValueError, ValidationError):
                if preflight_root is not None:
                    preflight_root.close()
                raise GcpPrivateCampaignError("LAUNCH_PREFLIGHT_FAILED") from None
        try:
            return self._execute_after_preflight(
                proposal=proposal,
                approval=approval,
                startup_payload=startup_payload,
                preflight_root=preflight_root,
            )
        finally:
            if preflight_root is not None:
                preflight_root.close()

    def _execute_after_preflight(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        approval: GcpPrivateCampaignApproval,
        startup_payload: GcpPrivateCampaignStartupPayload,
        preflight_root: SafeDirFD | None,
    ) -> GcpPrivateCampaignEvidenceReceipt:
        """Execute after authority and retained-evidence-root preflight."""

        request = build_gcp_private_campaign_create_request(
            proposal=proposal, startup_payload=startup_payload
        )
        self._journal.append(
            proposal,
            state="BACKSTOP_READY",
            occurred_at=self._clock.now(),
            detail_digest=request.create_request_digest,
        )
        self._journal.append(
            proposal, state="CREATE_INTENT", occurred_at=self._clock.now()
        )
        # A second local authority check closes the fsync/lock delay window:
        # the provider factory remains unreachable if approval or quote expiry
        # crosses between the first parse and durable create intent.
        try:
            verify_gcp_private_campaign_approval(
                approval, expected_proposal=proposal, now=self._clock.now()
            )
            self._assert_cleanup_deadline(proposal)
        except GcpPrivateCampaignError:
            if self._journal.load(proposal)[-1].state != "BLOCKED":
                self._journal.append(
                    proposal, state="BLOCKED", occurred_at=self._clock.now()
                )
            raise

        transport = self._transport_after_authority()
        # Factory initialization can itself consume enough wall time for an
        # exact human approval or quote to expire.  Recheck at the final local
        # boundary before the first provider mutation; a factory is not a
        # create authority.
        try:
            if preflight_root is not None:
                self._bind_evidence_root(transport, preflight_root)
            verify_gcp_private_campaign_approval(
                approval, expected_proposal=proposal, now=self._clock.now()
            )
            self._assert_cleanup_deadline(proposal)
        except GcpPrivateCampaignError:
            if self._journal.load(proposal)[-1].state != "BLOCKED":
                self._journal.append(
                    proposal, state="BLOCKED", occurred_at=self._clock.now()
                )
            raise
        created_or_ambiguous = False
        primary_error: GcpPrivateCampaignError | None = None
        evidence: GcpPrivateCampaignEvidenceReceipt | None = None
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding | None = None
        try:
            try:
                operation = transport.create_instance(
                    request,
                    request_id=proposal.request_ids.create_request_id,
                    timeout_seconds=self._operation_timeout_seconds,
                )
                self._assert_operation(
                    operation,
                    kind="create",
                    request_id=proposal.request_ids.create_request_id,
                )
                created_or_ambiguous = True
                self._journal.append(
                    proposal,
                    state="CREATE_SUBMITTED",
                    occurred_at=self._clock.now(),
                    operation_id=operation.operation_id,
                )
                result = transport.wait_operation(
                    operation, timeout_seconds=self._operation_timeout_seconds
                )
            except GcpPrivateCampaignTransportError as error:
                if not error.ambiguous:
                    raise
                created_or_ambiguous = True
                self._journal.append(
                    proposal, state="CREATE_RECONCILING", occurred_at=self._clock.now()
                )
                result = transport.reconcile_create(
                    request,
                    request_id=proposal.request_ids.create_request_id,
                    timeout_seconds=self._operation_timeout_seconds,
                )
                operation = result.operation
                self._assert_operation(
                    operation,
                    kind="create",
                    request_id=proposal.request_ids.create_request_id,
                )
            self._assert_done(result, operation=operation)
            self._journal.append(
                proposal,
                state="CREATED",
                occurred_at=self._clock.now(),
                operation_id=operation.operation_id,
            )
            observed = transport.observe_exact_instance(
                request, timeout_seconds=self._operation_timeout_seconds
            )
            exact_resource_binding = self._exact_resource_binding(observed, proposal)
            self._journal.append(
                proposal,
                state="INSTANCE_IDENTITY_BOUND",
                occurred_at=self._clock.now(),
                detail_digest=exact_resource_binding.provider_instance_id_sha256,
            )
            self._journal.append(
                proposal,
                state="BOOT_DISK_IDENTITY_BOUND",
                occurred_at=self._clock.now(),
                detail_digest=exact_resource_binding.boot_disk_provider_id_sha256,
            )
            self._assert_instance(observed, request)
            readiness = transport.observe_readiness(
                request, timeout_seconds=self._operation_timeout_seconds
            )
            self._assert_readiness(
                readiness,
                request,
                exact_resource_binding,
                observed.startup_script_sha256,
            )
            self._journal.append(
                proposal, state="READINESS_VERIFIED", occurred_at=self._clock.now()
            )
            config, workload, config_digest = self._build_handoff(proposal, readiness)
            self._assert_cleanup_deadline(proposal)
            handoff = transport.handoff_campaign(
                request,
                routing_config=config,
                workload=workload,
                timeout_seconds=self._operation_timeout_seconds,
            )
            if (
                handoff.proposal_id != proposal.proposal_id
                or handoff.routing_config_sha256 != config_digest
                or handoff.selected_workload_sha256
                != proposal.routing.selected_workload_sha256
                or handoff.routed_after_verified_readiness is not True
            ):
                raise GcpPrivateCampaignError("CAMPAIGN_HANDOFF_MISMATCH")
            self._journal.append(
                proposal,
                state="CAMPAIGN_HANDED_OFF",
                occurred_at=self._clock.now(),
                detail_digest=handoff.routing_config_sha256,
            )
            self._assert_cleanup_deadline(proposal)
            evidence = transport.retrieve_evidence(
                request, timeout_seconds=self._operation_timeout_seconds
            )
            if (
                evidence.proposal_id != proposal.proposal_id
                or evidence.raw_prompt_or_output_retained is not False
            ):
                raise GcpPrivateCampaignError("EVIDENCE_RECEIPT_MISMATCH")
            self._journal.append(
                proposal,
                state="EVIDENCE_RETRIEVED",
                occurred_at=self._clock.now(),
                detail_digest=evidence.retained_digest,
            )
        except GcpPrivateCampaignError as error:
            primary_error = error
        except (OSError, ValueError, ValidationError):
            primary_error = GcpPrivateCampaignError("CAMPAIGN_OPERATION_FAILED")
        cleanup = self._cleanup_after_transport(
            proposal=proposal,
            request=request,
            transport=transport,
            may_exist=created_or_ambiguous,
            exact_resource_binding=exact_resource_binding,
        )
        if primary_error is not None:
            raise primary_error
        if not cleanup.confirmed:
            raise GcpPrivateCampaignError("CLEANUP_UNCONFIRMED")
        if evidence is None:
            raise GcpPrivateCampaignError("EVIDENCE_RECEIPT_MISSING")
        return evidence

    def _cleanup_after_transport(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        request: GcpPrivateCampaignCreateRequest,
        transport: GcpPrivateCampaignTransport,
        may_exist: bool,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding | None,
    ) -> GcpPrivateCampaignCleanupOutcome:
        if not may_exist:
            return GcpPrivateCampaignCleanupOutcome(
                confirmed=False, journal_state="BLOCKED"
            )
        try:
            self._ensure_cleanup_intent(proposal)
            if exact_resource_binding is None:
                raise GcpPrivateCampaignError("EXACT_RESOURCE_BINDING_MISSING")
            inventory = transport.list_exact_owned_residuals(
                request, timeout_seconds=self._operation_timeout_seconds
            )
            self._assert_inventory(
                inventory,
                proposal,
                exact_resource_binding=exact_resource_binding,
            )
            if inventory.instance_state == "PRESENT":
                self._assert_cleanup_deadline(proposal)
                instance_phase = self._cleanup_resume_phase(proposal)
                if instance_phase in {
                    "INSTANCE_DELETE_SUBMITTED",
                    "INSTANCE_DELETE_RECONCILING",
                }:
                    self._append_instance_reconciling_if_needed(proposal)
                    result = transport.reconcile_delete_instance(
                        request,
                        exact_resource_binding=exact_resource_binding,
                        request_id=proposal.request_ids.delete_request_id,
                        timeout_seconds=self._operation_timeout_seconds,
                    )
                    operation = result.operation
                else:
                    try:
                        operation = transport.delete_exact_instance(
                            request,
                            exact_resource_binding=exact_resource_binding,
                            request_id=proposal.request_ids.delete_request_id,
                            timeout_seconds=self._operation_timeout_seconds,
                        )
                    except GcpPrivateCampaignTransportError as error:
                        if not error.ambiguous:
                            raise
                        self._append_instance_reconciling_if_needed(proposal)
                        result = transport.reconcile_delete_instance(
                            request,
                            exact_resource_binding=exact_resource_binding,
                            request_id=proposal.request_ids.delete_request_id,
                            timeout_seconds=self._operation_timeout_seconds,
                        )
                        operation = result.operation
                    else:
                        self._journal.append(
                            proposal,
                            state="INSTANCE_DELETE_SUBMITTED",
                            occurred_at=self._clock.now(),
                            operation_id=operation.operation_id,
                        )
                        result = transport.wait_operation(
                            operation, timeout_seconds=self._operation_timeout_seconds
                        )
                self._assert_operation(
                    operation,
                    kind="delete_instance",
                    request_id=proposal.request_ids.delete_request_id,
                )
                self._assert_done(result, operation=operation)
                inventory = transport.list_exact_owned_residuals(
                    request, timeout_seconds=self._operation_timeout_seconds
                )
                self._assert_inventory(
                    inventory,
                    proposal,
                    exact_resource_binding=exact_resource_binding,
                )
            if inventory.instance_state != "ABSENT":
                raise GcpPrivateCampaignError("INSTANCE_ABSENCE_UNCONFIRMED")
            if inventory.disks:
                if len(inventory.disks) != 1:
                    raise GcpPrivateCampaignError("OWNED_RESIDUAL_AMBIGUOUS")
                disk = inventory.disks[0]
                self._assert_boot_disk(disk, proposal)
                self._assert_cleanup_deadline(proposal)
                disk_phase = self._cleanup_resume_phase(proposal)
                if disk_phase in {
                    "BOOT_DISK_DELETE_INTENT",
                    "BOOT_DISK_DELETE_SUBMITTED",
                }:
                    disk_result = transport.reconcile_delete_exact_owned_boot_disk(
                        request,
                        disk,
                        exact_resource_binding=exact_resource_binding,
                        request_id=proposal.request_ids.boot_disk_delete_request_id,
                        timeout_seconds=self._operation_timeout_seconds,
                    )
                    disk_operation = disk_result.operation
                else:
                    self._journal.append(
                        proposal,
                        state="BOOT_DISK_DELETE_INTENT",
                        occurred_at=self._clock.now(),
                    )
                    disk_operation = transport.delete_exact_owned_boot_disk(
                        request,
                        disk,
                        exact_resource_binding=exact_resource_binding,
                        request_id=proposal.request_ids.boot_disk_delete_request_id,
                        timeout_seconds=self._operation_timeout_seconds,
                    )
                    self._journal.append(
                        proposal,
                        state="BOOT_DISK_DELETE_SUBMITTED",
                        occurred_at=self._clock.now(),
                        operation_id=disk_operation.operation_id,
                    )
                    disk_result = transport.wait_operation(
                        disk_operation, timeout_seconds=self._operation_timeout_seconds
                    )
                self._assert_operation(
                    disk_operation,
                    kind="delete_boot_disk",
                    request_id=proposal.request_ids.boot_disk_delete_request_id,
                )
                self._assert_done(
                    disk_result,
                    operation=disk_operation,
                )
            absence = transport.confirm_exact_absence(
                request, timeout_seconds=self._operation_timeout_seconds
            )
            self._assert_absence(absence, proposal)
            self._journal.append(
                proposal, state="CLEANUP_CONFIRMED", occurred_at=self._clock.now()
            )
            return GcpPrivateCampaignCleanupOutcome(True, "CLEANUP_CONFIRMED")
        except GcpPrivateCampaignError:
            self._mark_cleanup_unconfirmed(proposal)
            return GcpPrivateCampaignCleanupOutcome(False, "CLEANUP_UNCONFIRMED")
        except (OSError, ValueError, ValidationError):
            self._mark_cleanup_unconfirmed(proposal)
            return GcpPrivateCampaignCleanupOutcome(False, "CLEANUP_UNCONFIRMED")

    def _ensure_cleanup_intent(self, proposal: GcpPrivateCampaignProposal) -> None:
        state = self._journal.load(proposal)[-1].state
        if state in {
            "CLEANUP_INTENT",
            "INSTANCE_DELETE_SUBMITTED",
            "INSTANCE_DELETE_RECONCILING",
            "BOOT_DISK_DELETE_INTENT",
            "BOOT_DISK_DELETE_SUBMITTED",
        }:
            return
        self._journal.append(
            proposal, state="CLEANUP_INTENT", occurred_at=self._clock.now()
        )

    def _cleanup_resume_phase(
        self, proposal: GcpPrivateCampaignProposal
    ) -> GcpPrivateCampaignJournalState | None:
        phases = {
            "CLEANUP_INTENT",
            "INSTANCE_DELETE_SUBMITTED",
            "INSTANCE_DELETE_RECONCILING",
            "BOOT_DISK_DELETE_INTENT",
            "BOOT_DISK_DELETE_SUBMITTED",
        }
        events = self._journal.load(proposal)
        retry_intent = (
            len(events) >= 2
            and events[-1].state == "CLEANUP_INTENT"
            and events[-2].state in {"CLEANUP_UNCONFIRMED", "CLEANUP_DEADLINE_EXCEEDED"}
        )
        rows = events[:-1] if retry_intent else events
        for event in reversed(rows):
            if event.state in phases:
                return event.state
        return None

    def _append_instance_reconciling_if_needed(
        self, proposal: GcpPrivateCampaignProposal
    ) -> None:
        if self._journal.load(proposal)[-1].state != "INSTANCE_DELETE_RECONCILING":
            self._journal.append(
                proposal,
                state="INSTANCE_DELETE_RECONCILING",
                occurred_at=self._clock.now(),
            )

    def _assert_cleanup_deadline(self, proposal: GcpPrivateCampaignProposal) -> None:
        if self._clock.now() < self._journal.cleanup_deadline(proposal):
            return
        state = self._journal.load(proposal)[-1].state
        if state != "CLEANUP_DEADLINE_EXCEEDED":
            self._journal.append(
                proposal,
                state="CLEANUP_DEADLINE_EXCEEDED",
                occurred_at=self._clock.now(),
            )
        raise GcpPrivateCampaignError("CLEANUP_DEADLINE_EXCEEDED")

    def _mark_cleanup_unconfirmed(self, proposal: GcpPrivateCampaignProposal) -> None:
        if self._journal.load(proposal)[-1].state != "CLEANUP_UNCONFIRMED":
            self._journal.append(
                proposal,
                state="CLEANUP_UNCONFIRMED",
                occurred_at=self._clock.now(),
            )

    @staticmethod
    def _assert_inventory(
        inventory: GcpPrivateCampaignOwnedResidualInventory,
        proposal: GcpPrivateCampaignProposal,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding | None = None,
    ) -> None:
        if (
            inventory.proposal_id != proposal.proposal_id
            or inventory.project_id != proposal.topology.project_id
            or inventory.zone != proposal.topology.zone
            or inventory.ownership_labels != proposal.ownership_labels
            or inventory.pagination_complete is not True
        ):
            raise GcpPrivateCampaignError("OWNED_INVENTORY_MISMATCH")
        if exact_resource_binding is None or inventory.instance_state != "PRESENT":
            return
        if len(inventory.disks) != 1:
            raise GcpPrivateCampaignError("OWNED_RESIDUAL_AMBIGUOUS")
        disk = inventory.disks[0]
        if (
            disk.attachment_state != "ATTACHED"
            or disk.attached_provider_instance_id is None
            or sha256_digest(disk.attached_provider_instance_id.encode("ascii"))
            != exact_resource_binding.provider_instance_id_sha256
            or disk.provider_disk_id_sha256
            != exact_resource_binding.boot_disk_provider_id_sha256
        ):
            raise GcpPrivateCampaignError("EXACT_RESOURCE_BINDING_MISMATCH")

    @staticmethod
    def _assert_boot_disk(
        disk: GcpPrivateCampaignDiskObservation,
        proposal: GcpPrivateCampaignProposal,
    ) -> None:
        if (
            disk.disk_name != proposal.boot_disk_name
            or disk.ownership_labels != proposal.ownership_labels
            or disk.source_boot_image_identity
            != proposal.boot_image.boot_image_identity
            or disk.boot_attachment is not True
        ):
            raise GcpPrivateCampaignError("BOOT_DISK_OWNERSHIP_MISMATCH")

    @staticmethod
    def _assert_absence(
        absence: GcpPrivateCampaignAbsenceObservation,
        proposal: GcpPrivateCampaignProposal,
    ) -> None:
        if (
            absence.proposal_id != proposal.proposal_id
            or absence.project_id != proposal.topology.project_id
            or absence.zone != proposal.topology.zone
            or absence.ownership_labels != proposal.ownership_labels
            or absence.pagination_complete is not True
            or absence.instance_absent is not True
            or absence.boot_disk_absent is not True
            or absence.no_other_owned_billable_residuals is not True
        ):
            raise GcpPrivateCampaignError("CLEANUP_ABSENCE_UNCONFIRMED")

    def recover_exact_cleanup(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        authorization: GcpPrivateCampaignCleanupAuthorization,
        startup_payload: GcpPrivateCampaignStartupPayload,
    ) -> GcpPrivateCampaignCleanupOutcome:
        """Run only exact owned cleanup, even after the launch approval expired."""

        verify_gcp_private_campaign_cleanup_authorization(
            authorization, expected_proposal=proposal, now=self._clock.now()
        )
        request = build_gcp_private_campaign_create_request(
            proposal=proposal, startup_payload=startup_payload
        )
        events = self._journal.load(proposal)
        if events[-1].state == "CLEANUP_CONFIRMED":
            return GcpPrivateCampaignCleanupOutcome(True, "CLEANUP_CONFIRMED")
        exact_resource_binding = self._journal.exact_resource_binding(proposal)
        if exact_resource_binding is None:
            # A post-create crash before both immutable provider identities are
            # durable leaves the provider-native DELETE backstop in place. A
            # label-scoped absence read is safe, but deletion may never target
            # a name/label match without both durable provider-ID bindings.
            transport = self._transport_after_authority()
            try:
                absence = transport.confirm_exact_absence(
                    request, timeout_seconds=self._operation_timeout_seconds
                )
                self._assert_absence(absence, proposal)
                self._ensure_cleanup_intent(proposal)
                self._journal.append(
                    proposal,
                    state="CLEANUP_CONFIRMED",
                    occurred_at=self._clock.now(),
                )
                return GcpPrivateCampaignCleanupOutcome(True, "CLEANUP_CONFIRMED")
            except (GcpPrivateCampaignError, OSError, ValueError, ValidationError):
                self._ensure_cleanup_intent(proposal)
                self._mark_cleanup_unconfirmed(proposal)
                return GcpPrivateCampaignCleanupOutcome(False, "CLEANUP_UNCONFIRMED")
        # Factory comes after the cleanup-only authority and trusted history;
        # this method has no create/handoff code path.
        transport = self._transport_after_authority()
        return self._cleanup_after_transport(
            proposal=proposal,
            request=request,
            transport=transport,
            may_exist=True,
            exact_resource_binding=exact_resource_binding,
        )

    def discover_exact_orphans(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        authorization: GcpPrivateCampaignCleanupAuthorization,
    ) -> GcpPrivateCampaignOwnedResidualInventory:
        """Perform label-scoped discovery only; it cannot launch or delete."""

        verify_gcp_private_campaign_cleanup_authorization(
            authorization, expected_proposal=proposal, now=self._clock.now()
        )
        transport = self._transport_after_authority()
        inventory = transport.discover_exact_owned(
            proposal, timeout_seconds=self._operation_timeout_seconds
        )
        self._assert_inventory(inventory, proposal)
        return inventory


@dataclass
class FakeGcpPrivateCampaignTransport:
    """Deterministic local fake for acceptance and crash-window tests only."""

    private_ipv4: str = "10.23.0.17"
    observed_at: datetime = datetime(2026, 9, 2, tzinfo=UTC)
    lose_create_response_once: bool = False
    lose_delete_response_once: bool = False
    retain_boot_disk_after_instance_delete: bool = False
    cleanup_failure: bool = False
    topology_drift: bool = False
    readiness_drift: bool = False
    provider_instance_id: str = "123456789"
    readiness_provider_instance_id: str | None = None
    cleanup_provider_instance_id: str | None = None
    provider_boot_disk_id: str = "246813579"
    cleanup_boot_disk_id: str | None = None
    startup_script_sha256: Sha256Digest = "sha256:" + ("5" * 64)
    readiness_startup_script_sha256: Sha256Digest | None = None
    created: bool = False
    boot_disk_present: bool = False
    create_calls: int = 0
    delete_calls: int = 0
    boot_disk_delete_calls: int = 0
    handoff_calls: int = 0
    bound_evidence_root_identity: tuple[int, int] | None = None
    _deleted_instance_request_ids: set[str] = field(default_factory=set)
    _deleted_boot_disk_request_ids: set[str] = field(default_factory=set)

    def _operation(
        self,
        kind: Literal["create", "delete_instance", "delete_boot_disk"],
        request_id: str,
    ) -> GcpPrivateCampaignOperation:
        suffix = hashlib.sha256(f"{kind}:{request_id}".encode()).hexdigest()[:16]
        return GcpPrivateCampaignOperation(
            operation_id=f"pcop-{suffix}",
            operation_kind=kind,
            request_id=request_id,
            provider_operation_name=f"local-fake-{kind}-{suffix}",
        )

    def bind_evidence_root(self, root: SafeDirFD) -> None:
        root.assert_open()
        self.bound_evidence_root_identity = (root.device, root.inode)

    def create_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation:
        del timeout_seconds
        if request_id != request.proposal.request_ids.create_request_id:
            raise GcpPrivateCampaignTransportError("REQUEST_ID_MISMATCH")
        self.create_calls += 1
        self.created = True
        self.boot_disk_present = True
        if self.lose_create_response_once:
            self.lose_create_response_once = False
            raise GcpPrivateCampaignTransportError(
                "CREATE_RESPONSE_LOST", ambiguous=True
            )
        return self._operation("create", request_id)

    def reconcile_create(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult:
        del timeout_seconds
        if (
            not self.created
            or request_id != request.proposal.request_ids.create_request_id
        ):
            raise GcpPrivateCampaignTransportError("CREATE_RECONCILIATION_FAILED")
        operation = self._operation("create", request_id)
        return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")

    def wait_operation(
        self, operation: GcpPrivateCampaignOperation, *, timeout_seconds: int
    ) -> GcpPrivateCampaignOperationResult:
        del timeout_seconds
        return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")

    def observe_exact_instance(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignInstanceObservation:
        del timeout_seconds
        proposal = request.proposal
        return GcpPrivateCampaignInstanceObservation(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            provider_instance_id=self.provider_instance_id,
            ownership_labels=proposal.ownership_labels,
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_provider_type="nvidia-tesla-a100",
            accelerator_count=2,
            machine_fixed_local_ssd_count=2,
            state="RUNNING" if self.created else "NOT_FOUND",
            external_access="ABSENT",
            boot_disk_name=proposal.boot_disk_name,
            boot_disk_provider_id_sha256=sha256_digest(
                self.provider_boot_disk_id.encode("ascii")
            ),
            boot_disk_auto_delete=True,
            persistent_disk_count=0,
            max_runtime_seconds=proposal.max_runtime_seconds,
            instance_termination_action="DELETE",
            startup_payload_digest=(
                proposal.startup_payload_digest
                if not self.topology_drift
                else "sha256:" + ("f" * 64)
            ),
            startup_script_sha256=self.startup_script_sha256,
        )

    def observe_readiness(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignReadiness:
        del timeout_seconds
        proposal = request.proposal
        endpoint_b_epoch = 1 if not self.readiness_drift else 0
        return GcpPrivateCampaignReadiness(
            schema_version=GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_VERSION,
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            provider_instance_id=(
                self.readiness_provider_instance_id or self.provider_instance_id
            ),
            ownership_labels=proposal.ownership_labels,
            private_ipv4=self.private_ipv4,
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_count=2,
            startup_payload_digest=proposal.startup_payload_digest,
            startup_script_sha256=(
                self.readiness_startup_script_sha256 or self.startup_script_sha256
            ),
            endpoints=(
                GcpPrivateCampaignEndpointReadiness(
                    endpoint_id="endpoint-a",
                    gpu_ordinal=0,
                    private_port=8000,
                    health_http_status=200,
                    generation_http_status=200,
                    metrics_metric_name="vllm:num_requests_running",
                    health_capability="HTTP_HEALTH_V1",
                    metrics_capability="VLLM_PROMETHEUS_V1",
                    engine_attestation_capability="INFERDROME_ENGINE_ATTESTATION_V2",
                    engine_attestation_sha256=sha256_digest(b"fake-engine-a"),
                    observation_epoch=1,
                    observed_monotonic_ns=0,
                ),
                GcpPrivateCampaignEndpointReadiness(
                    endpoint_id="endpoint-b",
                    gpu_ordinal=1,
                    private_port=8001,
                    health_http_status=200,
                    generation_http_status=200,
                    metrics_metric_name="vllm:num_requests_running",
                    health_capability="HTTP_HEALTH_V1",
                    metrics_capability="VLLM_PROMETHEUS_V1",
                    engine_attestation_capability="INFERDROME_ENGINE_ATTESTATION_V2",
                    engine_attestation_sha256=sha256_digest(b"fake-engine-b"),
                    observation_epoch=endpoint_b_epoch,
                    observed_monotonic_ns=0,
                ),
            ),
            observed_at=_timestamp(self.observed_at),
        )

    def handoff_campaign(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        routing_config: bytes,
        workload: bytes,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignHandoffReceipt:
        del timeout_seconds
        if not self.created or not routing_config or not workload:
            raise GcpPrivateCampaignTransportError("HANDOFF_INPUT_INVALID")
        self.handoff_calls += 1
        from inferdrome.routing_execution.contracts import RoutingExecutionConfig
        from inferdrome.routing_execution.topology import admit_topology

        config = RoutingExecutionConfig.model_validate_json(routing_config)
        topology = admit_topology(config)
        first, second = topology.endpoints
        return GcpPrivateCampaignHandoffReceipt(
            proposal_id=request.proposal.proposal_id,
            routing_config_sha256=sha256_digest(routing_config),
            selected_workload_sha256=request.proposal.routing.selected_workload_sha256,
            endpoint_a_origin_sha256=first.published_identity.origin_sha256,
            endpoint_b_origin_sha256=second.published_identity.origin_sha256,
            routed_after_verified_readiness=True,
        )

    def retrieve_evidence(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignEvidenceReceipt:
        del timeout_seconds
        return GcpPrivateCampaignEvidenceReceipt(
            proposal_id=request.proposal.proposal_id,
            retained_digest=sha256_digest(b"local-fake-precampaign-evidence-v2"),
            collection_mode="CREATE_NO_REPLACE_RETRIEVAL",
            raw_prompt_or_output_retained=False,
        )

    def delete_exact_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperation:
        del timeout_seconds
        if request_id != request.proposal.request_ids.delete_request_id:
            raise GcpPrivateCampaignTransportError("REQUEST_ID_MISMATCH")
        observed = self.cleanup_provider_instance_id or self.provider_instance_id
        if (
            exact_resource_binding.proposal_id != request.proposal.proposal_id
            or exact_resource_binding.project_id != request.proposal.topology.project_id
            or exact_resource_binding.zone != request.proposal.topology.zone
            or exact_resource_binding.instance_name != request.proposal.instance_name
            or exact_resource_binding.ownership_labels
            != request.proposal.ownership_labels
            or exact_resource_binding.provider_instance_id_sha256
            != sha256_digest(observed.encode("ascii"))
        ):
            raise GcpPrivateCampaignTransportError("INSTANCE_IDENTITY_MISMATCH")
        if request_id in self._deleted_instance_request_ids:
            return self._operation("delete_instance", request_id)
        self.delete_calls += 1
        self._deleted_instance_request_ids.add(request_id)
        self.created = False
        if not self.retain_boot_disk_after_instance_delete:
            self.boot_disk_present = False
        if self.lose_delete_response_once:
            self.lose_delete_response_once = False
            raise GcpPrivateCampaignTransportError(
                "DELETE_RESPONSE_LOST", ambiguous=True
            )
        return self._operation("delete_instance", request_id)

    def reconcile_delete_instance(
        self,
        request: GcpPrivateCampaignCreateRequest,
        *,
        exact_resource_binding: GcpPrivateCampaignExactResourceBinding,
        request_id: str,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignOperationResult:
        del timeout_seconds
        if request_id != request.proposal.request_ids.delete_request_id:
            raise GcpPrivateCampaignTransportError("DELETE_RECONCILIATION_FAILED")
        operation = self.delete_exact_instance(
            request,
            exact_resource_binding=exact_resource_binding,
            request_id=request_id,
            timeout_seconds=0,
        )
        return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")

    def list_exact_owned_residuals(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignOwnedResidualInventory:
        del timeout_seconds
        proposal = request.proposal
        disks: tuple[GcpPrivateCampaignDiskObservation, ...] = ()
        if self.boot_disk_present:
            attached_provider_instance_id = (
                self.cleanup_provider_instance_id or self.provider_instance_id
                if self.created
                else None
            )
            disks = (
                GcpPrivateCampaignDiskObservation(
                    disk_name=proposal.boot_disk_name,
                    provider_disk_id_sha256=sha256_digest(
                        (
                            self.cleanup_boot_disk_id or self.provider_boot_disk_id
                        ).encode("ascii")
                    ),
                    ownership_labels=proposal.ownership_labels,
                    source_boot_image_identity=proposal.boot_image.boot_image_identity,
                    attached_provider_instance_id=attached_provider_instance_id,
                    attachment_state=(
                        "ATTACHED"
                        if attached_provider_instance_id is not None
                        else "DETACHED"
                    ),
                    boot_attachment=True,
                ),
            )
        return GcpPrivateCampaignOwnedResidualInventory(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            ownership_labels=proposal.ownership_labels,
            pagination_complete=True,
            instance_state="PRESENT" if self.created else "ABSENT",
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
        del timeout_seconds
        if (
            self.cleanup_failure
            or request_id != request.proposal.request_ids.boot_disk_delete_request_id
        ):
            raise GcpPrivateCampaignTransportError("BOOT_DISK_DELETE_FAILED")
        if disk.disk_name != request.proposal.boot_disk_name:
            raise GcpPrivateCampaignTransportError("BOOT_DISK_OWNERSHIP_MISMATCH")
        if (
            disk.provider_disk_id_sha256
            != exact_resource_binding.boot_disk_provider_id_sha256
        ):
            raise GcpPrivateCampaignTransportError("BOOT_DISK_IDENTITY_MISMATCH")
        if request_id in self._deleted_boot_disk_request_ids:
            return self._operation("delete_boot_disk", request_id)
        self.boot_disk_delete_calls += 1
        self._deleted_boot_disk_request_ids.add(request_id)
        self.boot_disk_present = False
        return self._operation("delete_boot_disk", request_id)

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
        return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")

    def confirm_exact_absence(
        self, request: GcpPrivateCampaignCreateRequest, *, timeout_seconds: int
    ) -> GcpPrivateCampaignAbsenceObservation:
        del timeout_seconds
        proposal = request.proposal
        absent = (
            not self.created and not self.boot_disk_present and not self.cleanup_failure
        )
        if not absent:
            raise GcpPrivateCampaignTransportError("CLEANUP_ABSENCE_UNCONFIRMED")
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
        del timeout_seconds
        request = build_gcp_private_campaign_create_request(
            proposal=proposal,
            startup_payload=issue_gcp_private_campaign_startup_payload(
                source_commit=proposal.source_commit,
                runner_image=proposal.runner_image,
                serving_image=proposal.serving_image,
            ),
        )
        return self.list_exact_owned_residuals(request, timeout_seconds=1)


def gcp_private_campaign_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return generated schemas for the isolated pre-campaign contract family."""

    models: tuple[tuple[str, str, type[PrecampaignModel]], ...] = (
        (
            "gcp-private-campaign-topology.schema.json",
            GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_ID,
            GcpPrivateCampaignTopology,
        ),
        (
            "gcp-private-campaign-startup.schema.json",
            GCP_PRIVATE_CAMPAIGN_STARTUP_SCHEMA_ID,
            GcpPrivateCampaignStartupPayload,
        ),
        (
            "gcp-private-campaign-proposal.schema.json",
            GCP_PRIVATE_CAMPAIGN_PROPOSAL_SCHEMA_ID,
            GcpPrivateCampaignProposal,
        ),
        (
            "gcp-private-campaign-approval.schema.json",
            GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_ID,
            GcpPrivateCampaignApproval,
        ),
        (
            "gcp-private-campaign-cleanup-authorization.schema.json",
            GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_ID,
            GcpPrivateCampaignCleanupAuthorization,
        ),
        (
            "gcp-private-campaign-create-request.schema.json",
            GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_ID,
            GcpPrivateCampaignCreateRequest,
        ),
        (
            "gcp-private-campaign-readiness.schema.json",
            GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_ID,
            GcpPrivateCampaignReadiness,
        ),
        (
            "gcp-private-campaign-engine-attestation.schema.json",
            GCP_PRIVATE_CAMPAIGN_ENGINE_ATTESTATION_SCHEMA_ID,
            GcpPrivateCampaignEngineAttestation,
        ),
        (
            "gcp-private-campaign-journal-event.schema.json",
            GCP_PRIVATE_CAMPAIGN_EVENT_SCHEMA_ID,
            GcpPrivateCampaignJournalEvent,
        ),
        (
            "gcp-private-campaign-receipt.schema.json",
            GCP_PRIVATE_CAMPAIGN_RECEIPT_SCHEMA_ID,
            GcpPrivateCampaignEvidenceReceipt,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, schema_id, model in models:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = schema_id
        output[filename] = schema
    return output
