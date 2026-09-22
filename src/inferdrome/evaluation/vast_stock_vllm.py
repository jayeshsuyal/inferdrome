"""Pinned public Vast vLLM image and observed-host admission contract.

The public OCI metadata is a source fact, not proof of the container that a
provider started.  The short-lived host receipt binds the facts that must be
observed again inside an already-rented container before Inferdrome may spawn
either engine process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from inferdrome.evaluation.contracts import ClosedModel
from inferdrome.evaluation.load_calibration_operator import _parse_utc
from inferdrome.external_router.contracts import Digest, OpaqueId

VAST_STOCK_VLLM_TAG: Final = "vastai/vllm:v0.26.0-cuda-12.9"
VAST_STOCK_VLLM_IMAGE_REFERENCE: Final = (
    "vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77"
)
VAST_STOCK_VLLM_INDEX_DIGEST: Final = (
    "sha256:de6283189ecd5a62660d8c5da6a2a9669a4c4e7226c6533bcc7106ee071114a0"
)
VAST_STOCK_VLLM_CONFIG_DIGEST: Final = (
    "sha256:6f07455f6e17001ad04d1ddc7f8331cc29167e748269bfcf6ecb4d23ba24bc0d"
)
VAST_STOCK_VLLM_ENTRYPOINT: Final = "/opt/instance-tools/bin/entrypoint.sh"
VAST_STOCK_VLLM_VERSION: Final = "0.26.0"
VAST_STOCK_CUDA_VERSION: Final = "12.9.1"
VAST_STOCK_CUDART_VERSION: Final = "12.9.79-1"
VAST_STOCK_UPSTREAM_IMAGE_TAG: Final = "vllm/vllm-openai:v0.26.0-cu129"
VAST_STOCK_VLLM_BUILD_COMMIT: Final = (
    "ffd46bfab2128bb84146050e98b51a617c6575ab"
)
VAST_STOCK_RECEIPT_MAX_AGE_SECONDS: Final = 300
SupportedVastA100Model = Literal[
    "NVIDIA A100-PCIE-40GB",
    "NVIDIA A100-SXM4-40GB",
]

UtcTimestamp = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class VastStockVllmProfile(ClosedModel):
    """Source-derived identity of the one supported public provider image."""

    schema_version: Literal["inferdrome.vast-stock-vllm-profile.v1"]
    image_tag: Literal["vastai/vllm:v0.26.0-cuda-12.9"]
    image_reference: Literal[
        "vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77"
    ]
    multiarch_index_digest: Literal[
        "sha256:de6283189ecd5a62660d8c5da6a2a9669a4c4e7226c6533bcc7106ee071114a0"
    ]
    image_config_digest: Literal[
        "sha256:6f07455f6e17001ad04d1ddc7f8331cc29167e748269bfcf6ecb4d23ba24bc0d"
    ]
    platform: Literal["linux/amd64"]
    entrypoint: tuple[Literal["/opt/instance-tools/bin/entrypoint.sh"]]
    vllm_version: Literal["0.26.0"]
    cuda_version: Literal["12.9.1"]
    cudart_version: Literal["12.9.79-1"]
    upstream_image_tag: Literal["vllm/vllm-openai:v0.26.0-cu129"]
    vllm_build_commit: Literal["ffd46bfab2128bb84146050e98b51a617c6575ab"]
    python_series: Literal["3.12.x"]
    gpu_count: Literal[2]
    provider_startup_boundary: Literal["VAST_STOCK_ENTRYPOINT_SSH_PORTAL_SUPERVISION"]


class VastStockGpuObservation(ClosedModel):
    index: Literal[0, 1]
    gpu_alias: OpaqueId
    uuid_sha256: Digest
    model: SupportedVastA100Model


class VastStockHostReceipt(ClosedModel):
    """Short-lived operator observation; it performs no provider operation."""

    schema_version: Literal["inferdrome.vast-stock-host-receipt.v1"]
    observed_at_utc: UtcTimestamp
    valid_until_utc: UtcTimestamp
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    provider_instance_alias: OpaqueId
    image_reference: Literal[
        "vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77"
    ]
    image_config_digest: Literal[
        "sha256:6f07455f6e17001ad04d1ddc7f8331cc29167e748269bfcf6ecb4d23ba24bc0d"
    ]
    entrypoint: tuple[Literal["/opt/instance-tools/bin/entrypoint.sh"]]
    vllm_version: Literal["0.26.0"]
    cuda_version: Literal["12.9.1"]
    cudart_version: Literal["12.9.79-1"]
    python_version: Annotated[str, Field(pattern=r"^3\.12\.[0-9]+$")]
    runtime_executable_identity_sha256: Digest
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"]
    model_snapshot_sha256: Digest
    model_snapshot_path_sha256: Digest
    endpoint_origins: tuple[str, str]
    gpus: tuple[VastStockGpuObservation, VastStockGpuObservation]
    gpus_idle: Literal[True]
    ports_closed: Literal[True]
    runtime_install_performed: Literal[False]
    image_build_performed: Literal[False]

    @model_validator(mode="after")
    def exact_observed_topology(self) -> VastStockHostReceipt:
        observed = _parse_utc(self.observed_at_utc)
        valid_until = _parse_utc(self.valid_until_utc)
        if not observed < valid_until:
            raise ValueError("stock-host receipt interval is invalid")
        lifetime_seconds = (valid_until - observed).total_seconds()
        if lifetime_seconds > VAST_STOCK_RECEIPT_MAX_AGE_SECONDS:
            raise ValueError("stock-host receipt lifetime exceeds five minutes")
        if tuple(item.index for item in self.gpus) != (0, 1):
            raise ValueError("stock-host receipt requires ordered GPU 0/1")
        if len({item.uuid_sha256 for item in self.gpus}) != 2:
            raise ValueError("stock-host receipt requires distinct GPU identities")
        if len({item.model for item in self.gpus}) != 1:
            raise ValueError("stock-host receipt requires one exact GPU model")
        if len(set(self.endpoint_origins)) != 2 or any(
            not origin.startswith("http://127.0.0.1:")
            for origin in self.endpoint_origins
        ):
            raise ValueError(
                "stock-host receipt requires two distinct loopback origins"
            )
        return self

    def is_fresh_at(self, now: datetime) -> bool:
        return _parse_utc(self.observed_at_utc) <= now <= _parse_utc(
            self.valid_until_utc
        )
