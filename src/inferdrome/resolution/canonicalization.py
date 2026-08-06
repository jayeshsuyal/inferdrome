"""Canonical resolver projections and exact bytes."""

from typing import Any

from pydantic import BaseModel

from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_canonical_json,
)
from inferdrome.domain.experiment import ExperimentSpec


def model_json_value(model: BaseModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("public root model must serialize as an object")
    return value


def canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_json_bytes(model_json_value(model))


def execution_fingerprint_projection(spec: ExperimentSpec) -> dict[str, Any]:
    """Select only measurement-affecting resolved fields."""

    workload = spec.workload.model_dump(mode="json", exclude_none=False)
    return {
        "projection_version": "inferdrome.execution-fingerprint-input.v1",
        "execution": spec.execution.model_dump(mode="json", exclude_none=False),
        "target": spec.target.model_dump(mode="json", exclude_none=False),
        "workload": {
            "sha256": workload["sha256"],
            "requested_output_tokens": workload["requested_output_tokens"],
            "temperature": workload["temperature"],
            "seed": workload["seed"],
        },
        "traffic": spec.traffic.model_dump(mode="json", exclude_none=False),
        "measurement": spec.measurement.model_dump(mode="json", exclude_none=False),
    }


def execution_fingerprint(spec: ExperimentSpec) -> str:
    return digest_canonical_json(
        DigestDomain.EXECUTION_FINGERPRINT,
        execution_fingerprint_projection(spec),
    )
