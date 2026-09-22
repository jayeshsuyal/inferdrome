"""Exact source-side contract for the public Vast startup-ready engine image."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.evaluation.contracts import ClosedModel
from inferdrome.qwen3_campaign import QWEN3_8B_MODEL_ID, QWEN3_8B_REVISION
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE

VAST_STARTUP_READY_REPOSITORY = "ghcr.io/jayeshsuyal/inferdrome-private-engine"
VAST_SSH_READINESS_SECONDS = 180
VAST_ENGINE_READINESS_SECONDS = 300
_PUBLIC_DIGEST = (
    r"^ghcr[.]io/jayeshsuyal/inferdrome-private-engine@sha256:[0-9a-f]{64}$"
)


def is_vast_startup_ready_image_reference(value: str) -> bool:
    """Return whether ``value`` is the one public engine repository at a digest."""

    return re.fullmatch(_PUBLIC_DIGEST, value) is not None


class VastStartupReadyImageProfile(ClosedModel):
    """Immutable declared profile; registry reachability remains externally observed."""

    schema_version: Literal["inferdrome.vast-startup-ready-image-profile.v1"]
    image_reference: Annotated[str, Field(pattern=_PUBLIC_DIGEST)]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    base_image_reference: Annotated[str, Field(min_length=1, max_length=256)]
    runtime_role: Literal["private-engine"]
    runtime: Literal["vllm"]
    runtime_version: Literal["0.26.0"]
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    startup_profile: Literal["vast-ssh-public-v1"]
    ssh_server_preinstalled: Literal[True]
    startup_supervisor: Literal["SSHD_FOREGROUND_DIRECT_EXEC"]
    serving_uid: Literal[2000]
    runtime_package_bootstrap: Literal["FORBIDDEN"]
    registry_access: Literal["ANONYMOUS_PUBLIC_PULL_REQUIRED_UNVERIFIED"]
    ssh_readiness_seconds: Literal[180]
    engine_readiness_seconds: Literal[300]

    @model_validator(mode="after")
    def exact_runtime_binding(self) -> VastStartupReadyImageProfile:
        if (
            self.base_image_reference != VLLM_RUNTIME_IMAGE_REFERENCE
            or self.model_id != QWEN3_8B_MODEL_ID
            or self.model_revision != QWEN3_8B_REVISION
        ):
            raise ValueError("vast startup-ready image bindings are invalid")
        return self
