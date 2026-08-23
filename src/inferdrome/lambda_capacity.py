"""Read-only Lambda capacity observations for frozen Qwen3 GPU tiers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A100_GPU_TIER_ID,
    QWEN3_H100_GPU_TIER_ID,
    qwen3_gpu_tier_policy,
)

LAMBDA_CLOUD_API_BASE_URL = "https://cloud.lambda.ai/api/v1"
LAMBDA_CLOUD_API_KEY_ENVIRONMENT_VARIABLE = "LAMBDA_CLOUD_API_KEY"
LAMBDA_CAPACITY_SCHEMA_VERSION = "inferdrome.lambda-capacity-observation.v1"

LAMBDA_A100_PCIE_DESCRIPTION = "1x A100 (40 GB PCIe)"
LAMBDA_A100_PCIE_GPU_DESCRIPTION = "A100 (40 GB PCIe)"
LAMBDA_H100_PCIE_DESCRIPTION = "1x H100 (80 GB PCIe)"
LAMBDA_H100_PCIE_GPU_DESCRIPTION = "H100 (80 GB PCIe)"
LAMBDA_CAPACITY_GPU_TIERS = (
    QWEN3_A100_GPU_TIER_ID,
    QWEN3_H100_GPU_TIER_ID,
)

_API_USER_AGENT = "Inferdrome-Lambda-Capacity/1.0"
_ALLOWED_API_PATHS = frozenset({"/instance-types", "/instances"})
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_CATALOG_ITEMS = 256
_MAX_REGIONS = 128
_MAX_RUNNING_INSTANCES = 256
_MIN_REQUEST_SPACING_SECONDS = 1.05
_INSTANCE_TYPE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_]{0,127}\Z")
_INSTANCE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_REGION_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INSTANCE_STATUSES = frozenset(
    {"booting", "active", "unhealthy", "terminating", "terminated", "preempted"}
)

CapacityStatus = Literal[
    "READY_FOR_OPERATOR_CONFIRMATION",
    "OUT_OF_CAPACITY",
    "TARGET_NOT_OFFERED",
    "TARGET_METADATA_MISMATCH",
    "TARGET_AMBIGUOUS",
    "RATE_MISMATCH",
    "ACTIVE_INSTANCE_CONFLICT",
]
_CAPACITY_STATUSES = frozenset(
    {
        "READY_FOR_OPERATOR_CONFIRMATION",
        "OUT_OF_CAPACITY",
        "TARGET_NOT_OFFERED",
        "TARGET_METADATA_MISMATCH",
        "TARGET_AMBIGUOUS",
        "RATE_MISMATCH",
        "ACTIVE_INSTANCE_CONFLICT",
    }
)


class LambdaCapacityError(RuntimeError):
    """Expected, secret-safe capacity watcher failure."""


class LambdaCapacityApiError(LambdaCapacityError):
    """A bounded read-only Lambda API request failed."""


@dataclass(frozen=True)
class LambdaCapacityTarget:
    """Exact provider metadata for one implemented read-only target."""

    architecture: str
    gpu_tier_id: str
    gpus: int
    memory_gib: int | None
    provider_description: str
    provider_gpu_description: str
    storage_gib: int | None
    vcpus: int | None


_CAPACITY_TARGETS = MappingProxyType(
    {
        QWEN3_A100_GPU_TIER_ID: LambdaCapacityTarget(
            architecture="x86_64",
            gpu_tier_id=QWEN3_A100_GPU_TIER_ID,
            gpus=1,
            memory_gib=None,
            provider_description=LAMBDA_A100_PCIE_DESCRIPTION,
            provider_gpu_description=LAMBDA_A100_PCIE_GPU_DESCRIPTION,
            storage_gib=None,
            vcpus=None,
        ),
        QWEN3_H100_GPU_TIER_ID: LambdaCapacityTarget(
            architecture="x86_64",
            gpu_tier_id=QWEN3_H100_GPU_TIER_ID,
            gpus=1,
            memory_gib=225,
            provider_description=LAMBDA_H100_PCIE_DESCRIPTION,
            provider_gpu_description=LAMBDA_H100_PCIE_GPU_DESCRIPTION,
            storage_gib=1_024,
            vcpus=26,
        ),
    }
)


def lambda_capacity_target(gpu_tier_id: str) -> LambdaCapacityTarget:
    """Resolve exact provider metadata for one implemented capacity target."""

    target = _CAPACITY_TARGETS.get(gpu_tier_id)
    if target is None:
        raise LambdaCapacityError(
            f"Lambda capacity GPU tier is not implemented: {gpu_tier_id}"
        )
    policy = qwen3_gpu_tier_policy(gpu_tier_id)
    if target.gpu_tier_id != policy.gpu_tier_id:
        raise AssertionError("Lambda capacity target drifted from Qwen3 policy")
    return target


@dataclass(frozen=True)
class LambdaRegion:
    """Secret-free Lambda region projection."""

    name: str
    description: str

    def public_record(self) -> dict[str, str]:
        return {"description": self.description, "name": self.name}


@dataclass(frozen=True)
class LambdaInstanceTypeOffer:
    """Strict projection of one Lambda instance-type catalog item."""

    name: str
    description: str
    gpu_description: str
    price_cents_per_hour: int
    vcpus: int
    memory_gib: int
    storage_gib: int
    gpus: int
    architecture: str
    regions_with_capacity: tuple[LambdaRegion, ...]

    @property
    def hourly_rate_usd(self) -> Decimal:
        return Decimal(self.price_cents_per_hour) / Decimal(100)

    def public_record(self) -> dict[str, object]:
        return {
            "architecture": self.architecture,
            "description": self.description,
            "gpu_description": self.gpu_description,
            "name": self.name,
            "price_cents_per_hour": self.price_cents_per_hour,
            "regions_with_capacity": [
                region.public_record() for region in self.regions_with_capacity
            ],
            "specs": {
                "gpus": self.gpus,
                "memory_gib": self.memory_gib,
                "storage_gib": self.storage_gib,
                "vcpus": self.vcpus,
            },
        }


@dataclass(frozen=True)
class LambdaCapacityObservation:
    """One launch-free observation of a frozen GPU target."""

    observed_at: datetime
    status: CapacityStatus
    catalog_projection_sha256: str
    api_paths_observed: tuple[str, ...]
    instance_type: LambdaInstanceTypeOffer | None
    active_instance_count: int | None
    gpu_tier_id: str = QWEN3_A100_GPU_TIER_ID

    def __post_init__(self) -> None:
        target = lambda_capacity_target(self.gpu_tier_id)
        if self.status not in _CAPACITY_STATUSES:
            raise LambdaCapacityError("capacity observation status is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise LambdaCapacityError(
                "capacity observation time must be timezone-aware"
            )
        if _SHA256_PATTERN.fullmatch(self.catalog_projection_sha256) is None:
            raise LambdaCapacityError("capacity catalog projection digest is invalid")
        if self.status in {
            "READY_FOR_OPERATOR_CONFIRMATION",
            "ACTIVE_INSTANCE_CONFLICT",
        }:
            if (
                self.instance_type is None
                or not self.instance_type.regions_with_capacity
                or self.api_paths_observed != ("/instance-types", "/instances")
                or isinstance(self.active_instance_count, bool)
                or not isinstance(self.active_instance_count, int)
            ):
                raise LambdaCapacityError(
                    "capacity-ready observation is internally inconsistent"
                )
            policy = qwen3_gpu_tier_policy(self.gpu_tier_id)
            if (
                self.instance_type.description != target.provider_description
                or self.instance_type.gpu_description
                != target.provider_gpu_description
                or self.instance_type.gpus != target.gpus
                or self.instance_type.architecture != target.architecture
                or (
                    target.memory_gib is not None
                    and self.instance_type.memory_gib != target.memory_gib
                )
                or (
                    target.storage_gib is not None
                    and self.instance_type.storage_gib != target.storage_gib
                )
                or (
                    target.vcpus is not None
                    and self.instance_type.vcpus != target.vcpus
                )
                or self.instance_type.hourly_rate_usd != policy.hourly_rate_usd
            ):
                raise LambdaCapacityError(
                    "capacity-ready observation is internally inconsistent"
                )
            if self.status == "READY_FOR_OPERATOR_CONFIRMATION":
                if self.active_instance_count != 0:
                    raise LambdaCapacityError(
                        "capacity-ready observation is internally inconsistent"
                    )
            elif self.active_instance_count < 1:
                raise LambdaCapacityError(
                    "active-instance conflict observation is internally inconsistent"
                )
        elif (
            self.active_instance_count is not None
            or self.api_paths_observed != ("/instance-types",)
        ):
            raise LambdaCapacityError("capacity observation is internally inconsistent")
        if self.status == "OUT_OF_CAPACITY" and (
            self.instance_type is None or self.instance_type.regions_with_capacity
        ):
            raise LambdaCapacityError(
                "out-of-capacity observation is internally inconsistent"
            )

    @property
    def launch_preflight_ready(self) -> bool:
        return self.status == "READY_FOR_OPERATOR_CONFIRMATION"

    def public_record(self) -> dict[str, object]:
        target = lambda_capacity_target(self.gpu_tier_id)
        policy = qwen3_gpu_tier_policy(self.gpu_tier_id)
        instance_type = self.instance_type
        return {
            "active_instance_count": self.active_instance_count,
            "api_methods_observed": ["GET"],
            "api_paths_observed": list(self.api_paths_observed),
            "catalog_projection_sha256": self.catalog_projection_sha256,
            "gpu_target": {
                "expected_nvidia_smi_name": policy.expected_nvidia_smi_name,
                "expected_provider_description": target.provider_description,
                "expected_provider_gpu_description": (
                    target.provider_gpu_description
                ),
                "gpu_tier_id": policy.gpu_tier_id,
            },
            "hardware_attestation": False,
            "instance_launch_performed": False,
            "instance_type": (
                None
                if instance_type is None
                else {
                    "architecture": instance_type.architecture,
                    "description": instance_type.description,
                    "gpu_description": instance_type.gpu_description,
                    "gpus": instance_type.gpus,
                    "hourly_rate_usd": _decimal_text(
                        instance_type.hourly_rate_usd
                    ),
                    "name": instance_type.name,
                    "regions_with_capacity": [
                        region.public_record()
                        for region in instance_type.regions_with_capacity
                    ],
                }
            ),
            "launch_authorization": "EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED",
            "launch_preflight_ready": self.launch_preflight_ready,
            "observed_at": _timestamp(self.observed_at),
            "provider": "lambda_cloud",
            "record_kind": (
                "READ_ONLY_PROVIDER_OBSERVATION_NOT_HARDWARE_ATTESTATION"
            ),
            "schema_version": LAMBDA_CAPACITY_SCHEMA_VERSION,
            "status": self.status,
        }


JsonGetTransport = Callable[[str, str, float], object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _urllib_get(path: str, api_key: str, timeout: float) -> object:
    if path not in _ALLOWED_API_PATHS:
        raise LambdaCapacityApiError("Lambda capacity API path is not permitted")
    request = urllib.request.Request(
        LAMBDA_CLOUD_API_BASE_URL + path,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _API_USER_AGENT,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise LambdaCapacityApiError(
            f"Lambda capacity API request failed with HTTP {error.code}"
        ) from None
    except (OSError, urllib.error.URLError):
        raise LambdaCapacityApiError(
            "Lambda capacity API request could not complete"
        ) from None
    if len(content) > _MAX_RESPONSE_BYTES:
        raise LambdaCapacityApiError(
            "Lambda capacity API response exceeded its size limit"
        )
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LambdaCapacityApiError(
            "Lambda capacity API response is not valid JSON"
        ) from None


class LambdaCapacityClient:
    """Minimal client whose transport surface permits only two GET endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: JsonGetTransport = _urllib_get,
        timeout_seconds: float = 20,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _valid_api_key(api_key):
            raise LambdaCapacityError(
                f"{LAMBDA_CLOUD_API_KEY_ENVIRONMENT_VARIABLE} is missing or invalid"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 60
        ):
            raise LambdaCapacityError("Lambda capacity API timeout is outside limits")
        self._api_key = api_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._sleep = sleeper
        self._monotonic = monotonic
        self._last_request_started: float | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> LambdaCapacityClient:
        selected = os.environ if environment is None else environment
        return cls(
            selected.get(LAMBDA_CLOUD_API_KEY_ENVIRONMENT_VARIABLE, ""),
            **kwargs,
        )

    def list_instance_types(self) -> tuple[LambdaInstanceTypeOffer, ...]:
        response = self._get("/instance-types")
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise LambdaCapacityApiError(
                "Lambda instance-type catalog has an unexpected shape"
            )
        raw_catalog = response["data"]
        if len(raw_catalog) > _MAX_CATALOG_ITEMS:
            raise LambdaCapacityApiError("Lambda instance-type catalog is too large")
        if any(
            not isinstance(key, str)
            or _INSTANCE_TYPE_NAME_PATTERN.fullmatch(key) is None
            for key in raw_catalog
        ):
            raise LambdaCapacityApiError(
                "Lambda instance-type catalog contains an invalid key"
            )
        offers = tuple(
            _instance_type_from_api(key, value)
            for key, value in sorted(raw_catalog.items())
        )
        names = [offer.name for offer in offers]
        if len(names) != len(set(names)):
            raise LambdaCapacityApiError(
                "Lambda instance-type catalog contains duplicate names"
            )
        return offers

    def count_active_instances(self) -> int:
        response = self._get("/instances")
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise LambdaCapacityApiError(
                "Lambda active-instance list has an unexpected shape"
            )
        values = response["data"]
        if len(values) > _MAX_RUNNING_INSTANCES:
            raise LambdaCapacityApiError("Lambda active-instance list is too large")
        identifiers = [_active_instance_id(value) for value in values]
        if len(identifiers) != len(set(identifiers)):
            raise LambdaCapacityApiError(
                "Lambda active-instance list contains duplicate IDs"
            )
        return len(identifiers)

    def _get(self, path: str) -> object:
        if path not in _ALLOWED_API_PATHS:
            raise LambdaCapacityApiError("Lambda capacity API path is not permitted")
        started = self._monotonic()
        if self._last_request_started is not None:
            remaining = (
                _MIN_REQUEST_SPACING_SECONDS
                - (started - self._last_request_started)
            )
            if remaining > 0:
                self._sleep(remaining)
                started = self._monotonic()
        self._last_request_started = started
        return self._transport(path, self._api_key, self._timeout_seconds)


def observe_a100_pcie_capacity(
    client: LambdaCapacityClient,
    *,
    now: Callable[[], datetime] | None = None,
) -> LambdaCapacityObservation:
    """Observe exact A100 PCIe capacity without authorizing or launching it."""

    return observe_gpu_capacity(
        client,
        QWEN3_A100_GPU_TIER_ID,
        now=now,
    )


def observe_h100_pcie_capacity(
    client: LambdaCapacityClient,
    *,
    now: Callable[[], datetime] | None = None,
) -> LambdaCapacityObservation:
    """Observe exact H100 PCIe capacity without authorizing or launching it."""

    return observe_gpu_capacity(
        client,
        QWEN3_H100_GPU_TIER_ID,
        now=now,
    )


def observe_gpu_capacity(
    client: LambdaCapacityClient,
    gpu_tier_id: str,
    *,
    now: Callable[[], datetime] | None = None,
) -> LambdaCapacityObservation:
    """Observe one exact implemented GPU tier without any launch authority."""

    observed_at = (now or (lambda: datetime.now(UTC)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise LambdaCapacityError("capacity observation time must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)
    target = lambda_capacity_target(gpu_tier_id)
    policy = qwen3_gpu_tier_policy(gpu_tier_id)
    offers = client.list_instance_types()
    catalog_projection_sha256 = _catalog_projection_sha256(offers)
    exact = [
        offer for offer in offers if offer.description == target.provider_description
    ]
    if len(exact) > 1:
        return _observation(
            observed_at,
            "TARGET_AMBIGUOUS",
            catalog_projection_sha256,
            None,
            gpu_tier_id,
        )
    if not exact:
        near = [
            offer
            for offer in offers
            if offer.gpu_description == target.provider_gpu_description
            and offer.gpus == 1
        ]
        if len(near) > 1:
            return _observation(
                observed_at,
                "TARGET_AMBIGUOUS",
                catalog_projection_sha256,
                None,
                gpu_tier_id,
            )
        return _observation(
            observed_at,
            "TARGET_METADATA_MISMATCH" if near else "TARGET_NOT_OFFERED",
            catalog_projection_sha256,
            near[0] if len(near) == 1 else None,
            gpu_tier_id,
        )

    offer = exact[0]
    if (
        offer.gpu_description != target.provider_gpu_description
        or offer.gpus != target.gpus
        or offer.architecture != target.architecture
        or (
            target.memory_gib is not None
            and offer.memory_gib != target.memory_gib
        )
        or (
            target.storage_gib is not None
            and offer.storage_gib != target.storage_gib
        )
        or (target.vcpus is not None and offer.vcpus != target.vcpus)
    ):
        return _observation(
            observed_at,
            "TARGET_METADATA_MISMATCH",
            catalog_projection_sha256,
            offer,
            gpu_tier_id,
        )
    if offer.hourly_rate_usd != policy.hourly_rate_usd:
        return _observation(
            observed_at,
            "RATE_MISMATCH",
            catalog_projection_sha256,
            offer,
            gpu_tier_id,
        )
    if not offer.regions_with_capacity:
        return _observation(
            observed_at,
            "OUT_OF_CAPACITY",
            catalog_projection_sha256,
            offer,
            gpu_tier_id,
        )

    active_instance_count = client.count_active_instances()
    return LambdaCapacityObservation(
        active_instance_count=active_instance_count,
        api_paths_observed=("/instance-types", "/instances"),
        catalog_projection_sha256=catalog_projection_sha256,
        gpu_tier_id=gpu_tier_id,
        instance_type=offer,
        observed_at=observed_at,
        status=(
            "READY_FOR_OPERATOR_CONFIRMATION"
            if active_instance_count == 0
            else "ACTIVE_INSTANCE_CONFLICT"
        ),
    )


def _observation(
    observed_at: datetime,
    status: CapacityStatus,
    catalog_projection_sha256: str,
    instance_type: LambdaInstanceTypeOffer | None,
    gpu_tier_id: str,
) -> LambdaCapacityObservation:
    return LambdaCapacityObservation(
        active_instance_count=None,
        api_paths_observed=("/instance-types",),
        catalog_projection_sha256=catalog_projection_sha256,
        gpu_tier_id=gpu_tier_id,
        instance_type=instance_type,
        observed_at=observed_at,
        status=status,
    )


def _instance_type_from_api(key: object, value: object) -> LambdaInstanceTypeOffer:
    if (
        not isinstance(key, str)
        or _INSTANCE_TYPE_NAME_PATTERN.fullmatch(key) is None
        or not isinstance(value, dict)
    ):
        raise LambdaCapacityApiError("Lambda instance-type entry is invalid")
    instance_type = value.get("instance_type")
    regions = value.get("regions_with_capacity_available")
    if not isinstance(instance_type, dict) or not isinstance(regions, list):
        raise LambdaCapacityApiError("Lambda instance-type entry is incomplete")
    if len(regions) > _MAX_REGIONS:
        raise LambdaCapacityApiError("Lambda instance-type region list is too large")
    name = instance_type.get("name")
    if name != key:
        raise LambdaCapacityApiError("Lambda instance-type key and name disagree")
    description = _bounded_text(
        instance_type.get("description"), label="instance-type description"
    )
    gpu_description = _bounded_text(
        instance_type.get("gpu_description"), label="GPU description"
    )
    price = _positive_integer(
        instance_type.get("price_cents_per_hour"),
        label="instance-type hourly price",
        maximum=1_000_000,
    )
    specs = instance_type.get("specs")
    if not isinstance(specs, dict):
        raise LambdaCapacityApiError("Lambda instance-type specs are invalid")
    architecture = instance_type.get("architecture")
    if architecture not in {"x86_64", "arm64"}:
        raise LambdaCapacityApiError("Lambda instance-type architecture is invalid")
    parsed_regions = tuple(_region_from_api(region) for region in regions)
    region_names = [region.name for region in parsed_regions]
    if len(region_names) != len(set(region_names)):
        raise LambdaCapacityApiError(
            "Lambda instance-type region list contains duplicates"
        )
    return LambdaInstanceTypeOffer(
        architecture=architecture,
        description=description,
        gpu_description=gpu_description,
        gpus=_positive_integer(specs.get("gpus"), label="GPU count", maximum=256),
        memory_gib=_positive_integer(
            specs.get("memory_gib"), label="memory GiB", maximum=1_000_000
        ),
        name=name,
        price_cents_per_hour=price,
        regions_with_capacity=tuple(sorted(parsed_regions, key=lambda item: item.name)),
        storage_gib=_positive_integer(
            specs.get("storage_gib"), label="storage GiB", maximum=10_000_000
        ),
        vcpus=_positive_integer(
            specs.get("vcpus"), label="vCPU count", maximum=100_000
        ),
    )


def _region_from_api(value: object) -> LambdaRegion:
    if not isinstance(value, dict):
        raise LambdaCapacityApiError("Lambda capacity region is invalid")
    name = value.get("name")
    if not isinstance(name, str) or _REGION_NAME_PATTERN.fullmatch(name) is None:
        raise LambdaCapacityApiError("Lambda capacity region name is invalid")
    return LambdaRegion(
        description=_bounded_text(
            value.get("description"), label="region description"
        ),
        name=name,
    )


def _active_instance_id(value: object) -> str:
    if not isinstance(value, dict):
        raise LambdaCapacityApiError("Lambda active-instance entry is invalid")
    instance_id = value.get("id")
    status = value.get("status")
    if (
        not isinstance(instance_id, str)
        or _INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
        or not isinstance(status, str)
        or status not in _INSTANCE_STATUSES
    ):
        raise LambdaCapacityApiError("Lambda active-instance identity is invalid")
    return instance_id


def _positive_integer(value: object, *, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise LambdaCapacityApiError(f"Lambda {label} is invalid")
    return value


def _bounded_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise LambdaCapacityApiError(f"Lambda {label} is invalid")
    return value


def _catalog_projection_sha256(
    offers: tuple[LambdaInstanceTypeOffer, ...],
) -> str:
    content = json.dumps(
        [offer.public_record() for offer in offers],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _valid_api_key(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 4_096
        and not any(character.isspace() for character in value)
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
