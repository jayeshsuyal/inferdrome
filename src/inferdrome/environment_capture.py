"""Allowlist-only environment capture for executable Inferdrome runs."""

import platform
from datetime import datetime

from inferdrome.adapters.vllm_bench import EndpointPreflightCapture
from inferdrome.domain.environment import (
    EnvironmentField,
    EnvironmentFieldName,
    EnvironmentManifest,
    ProvenanceKind,
)
from inferdrome.domain.experiment import AttachedVllmTarget, ExperimentSpec
from inferdrome.domain.states import EnvironmentCompleteness
from inferdrome.gpu_proof import LocalGpuProof
from inferdrome.normalization.vllm_0_26 import VLLM_VERSION

_PRODUCER_VERSION_PATH = "native/producer-version.txt"
_INVOCATION_PATH = "native/invocation.json"


def _unknown(name: EnvironmentFieldName) -> EnvironmentField:
    return EnvironmentField(
        name=name,
        value=None,
        provenance=ProvenanceKind.UNKNOWN,
        evidence_path=None,
    )


def _text_field(
    name: EnvironmentFieldName,
    value: str | None,
    provenance: ProvenanceKind,
    *,
    evidence_path: str | None = None,
) -> EnvironmentField:
    if value is None or not value.strip():
        return _unknown(name)
    return EnvironmentField(
        name=name,
        value=value,
        provenance=provenance,
        evidence_path=evidence_path,
    )


def _client_fields() -> dict[EnvironmentFieldName, EnvironmentField]:
    return {
        EnvironmentFieldName.CLIENT_OS: _text_field(
            EnvironmentFieldName.CLIENT_OS,
            platform.platform(aliased=True, terse=True),
            ProvenanceKind.CLIENT_OBSERVED,
        ),
        EnvironmentFieldName.CLIENT_ARCH: _text_field(
            EnvironmentFieldName.CLIENT_ARCH,
            platform.machine(),
            ProvenanceKind.CLIENT_OBSERVED,
        ),
        EnvironmentFieldName.CLIENT_PYTHON_VERSION: _text_field(
            EnvironmentFieldName.CLIENT_PYTHON_VERSION,
            platform.python_version(),
            ProvenanceKind.CLIENT_OBSERVED,
        ),
    }


def _manifest(
    *,
    run_id: str,
    captured_at: datetime,
    fields_by_name: dict[EnvironmentFieldName, EnvironmentField],
) -> EnvironmentManifest:
    fields = tuple(fields_by_name[name] for name in EnvironmentFieldName)
    unknown_count = sum(
        field.provenance is ProvenanceKind.UNKNOWN for field in fields
    )
    if unknown_count == 0:
        completeness = EnvironmentCompleteness.COMPLETE
    elif unknown_count == len(fields):
        completeness = EnvironmentCompleteness.UNKNOWN
    else:
        completeness = EnvironmentCompleteness.PARTIAL
    return EnvironmentManifest(
        schema_version="inferdrome.environment.v1",
        run_id=run_id,
        field_set_version="inferdrome.environment-fields.v1",
        captured_at=captured_at,
        completeness=completeness,
        fields=fields,
    )


def capture_fake_environment(
    *,
    run_id: str,
    target_model: str,
    captured_at: datetime,
) -> EnvironmentManifest:
    """Capture honest synthetic-run provenance without implying a real GPU."""

    fields = _client_fields()
    fields.update(
        {
            EnvironmentFieldName.PRODUCER_VERSION: EnvironmentField(
                name=EnvironmentFieldName.PRODUCER_VERSION,
                value="1.0.0",
                provenance=ProvenanceKind.LOCALLY_VERIFIED,
                evidence_path=_PRODUCER_VERSION_PATH,
            ),
            EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: _unknown(
                EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256
            ),
            EnvironmentFieldName.TARGET_ENGINE_VERSION: EnvironmentField(
                name=EnvironmentFieldName.TARGET_ENGINE_VERSION,
                value="synthetic-fixture-v1",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.TARGET_MODEL_REVISION: EnvironmentField(
                name=EnvironmentFieldName.TARGET_MODEL_REVISION,
                value="synthetic-fixture",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.TARGET_TOKENIZER_REVISION: EnvironmentField(
                name=EnvironmentFieldName.TARGET_TOKENIZER_REVISION,
                value="synthetic-fixture",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.SERVER_MODEL_ID: EnvironmentField(
                name=EnvironmentFieldName.SERVER_MODEL_ID,
                value=target_model,
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.GPU_MODEL: EnvironmentField(
                name=EnvironmentFieldName.GPU_MODEL,
                value="not-applicable",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.GPU_COUNT: EnvironmentField(
                name=EnvironmentFieldName.GPU_COUNT,
                value=0,
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.CUDA_VERSION: EnvironmentField(
                name=EnvironmentFieldName.CUDA_VERSION,
                value="not-applicable",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
            EnvironmentFieldName.DRIVER_VERSION: EnvironmentField(
                name=EnvironmentFieldName.DRIVER_VERSION,
                value="not-applicable",
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            ),
        }
    )
    return _manifest(
        run_id=run_id,
        captured_at=captured_at,
        fields_by_name=fields,
    )


def capture_attached_environment(
    spec: ExperimentSpec,
    preflight: EndpointPreflightCapture,
    *,
    run_id: str,
    captured_at: datetime,
) -> EnvironmentManifest:
    """Capture only client-observed, configured, and server-reported facts."""

    target = spec.target
    if not isinstance(target, AttachedVllmTarget):
        raise TypeError("attached environment capture requires a vLLM target")
    fields = _client_fields()
    fields.update(
        {
            EnvironmentFieldName.PRODUCER_VERSION: EnvironmentField(
                name=EnvironmentFieldName.PRODUCER_VERSION,
                value=VLLM_VERSION,
                provenance=ProvenanceKind.LOCALLY_VERIFIED,
                evidence_path=_PRODUCER_VERSION_PATH,
            ),
            EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: _unknown(
                EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256
            ),
            EnvironmentFieldName.TARGET_ENGINE_VERSION: _text_field(
                EnvironmentFieldName.TARGET_ENGINE_VERSION,
                target.engine_version,
                ProvenanceKind.CONFIGURED,
            ),
            EnvironmentFieldName.TARGET_MODEL_REVISION: _text_field(
                EnvironmentFieldName.TARGET_MODEL_REVISION,
                target.model_revision,
                ProvenanceKind.CONFIGURED,
            ),
            EnvironmentFieldName.TARGET_TOKENIZER_REVISION: _text_field(
                EnvironmentFieldName.TARGET_TOKENIZER_REVISION,
                target.tokenizer_revision,
                ProvenanceKind.CONFIGURED,
            ),
            EnvironmentFieldName.SERVER_MODEL_ID: EnvironmentField(
                name=EnvironmentFieldName.SERVER_MODEL_ID,
                value=preflight.result.target_model,
                provenance=ProvenanceKind.SERVER_REPORTED,
                evidence_path=_INVOCATION_PATH,
            ),
            EnvironmentFieldName.GPU_MODEL: _unknown(
                EnvironmentFieldName.GPU_MODEL
            ),
            EnvironmentFieldName.GPU_COUNT: _unknown(
                EnvironmentFieldName.GPU_COUNT
            ),
            EnvironmentFieldName.CUDA_VERSION: _unknown(
                EnvironmentFieldName.CUDA_VERSION
            ),
            EnvironmentFieldName.DRIVER_VERSION: _unknown(
                EnvironmentFieldName.DRIVER_VERSION
            ),
        }
    )
    return _manifest(
        run_id=run_id,
        captured_at=captured_at,
        fields_by_name=fields,
    )


def capture_managed_gpu_environment(
    spec: ExperimentSpec,
    preflight: EndpointPreflightCapture,
    proof: LocalGpuProof,
    *,
    run_id: str,
    captured_at: datetime,
) -> EnvironmentManifest:
    """Project a validated local-GPU proof onto the frozen public allowlist."""

    target = spec.target
    if not isinstance(target, AttachedVllmTarget):
        raise TypeError("managed environment capture requires a vLLM target")
    if proof.run_id != run_id:
        raise ValueError("managed environment proof belongs to a different run")
    if target.model_revision is None or target.tokenizer_revision is None:
        raise ValueError("managed environment requires exact target revisions")
    fields = {
        EnvironmentFieldName.CLIENT_OS: EnvironmentField(
            name=EnvironmentFieldName.CLIENT_OS,
            value=proof.client_os,
            provenance=ProvenanceKind.CLIENT_OBSERVED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.CLIENT_ARCH: EnvironmentField(
            name=EnvironmentFieldName.CLIENT_ARCH,
            value=proof.client_arch,
            provenance=ProvenanceKind.CLIENT_OBSERVED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.CLIENT_PYTHON_VERSION: EnvironmentField(
            name=EnvironmentFieldName.CLIENT_PYTHON_VERSION,
            value=proof.client_python_version,
            provenance=ProvenanceKind.CLIENT_OBSERVED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.PRODUCER_VERSION: EnvironmentField(
            name=EnvironmentFieldName.PRODUCER_VERSION,
            value=VLLM_VERSION,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_PRODUCER_VERSION_PATH,
        ),
        EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: EnvironmentField(
            name=EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256,
            value=proof.producer_distribution.sha256,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.TARGET_ENGINE_VERSION: EnvironmentField(
            name=EnvironmentFieldName.TARGET_ENGINE_VERSION,
            value=VLLM_VERSION,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.TARGET_MODEL_REVISION: EnvironmentField(
            name=EnvironmentFieldName.TARGET_MODEL_REVISION,
            value=target.model_revision,
            provenance=ProvenanceKind.CONFIGURED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.TARGET_TOKENIZER_REVISION: EnvironmentField(
            name=EnvironmentFieldName.TARGET_TOKENIZER_REVISION,
            value=target.tokenizer_revision,
            provenance=ProvenanceKind.CONFIGURED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.SERVER_MODEL_ID: EnvironmentField(
            name=EnvironmentFieldName.SERVER_MODEL_ID,
            value=preflight.result.target_model,
            provenance=ProvenanceKind.SERVER_REPORTED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.GPU_MODEL: EnvironmentField(
            name=EnvironmentFieldName.GPU_MODEL,
            value=proof.gpu_model,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.GPU_COUNT: EnvironmentField(
            name=EnvironmentFieldName.GPU_COUNT,
            value=len(proof.gpus),
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.CUDA_VERSION: EnvironmentField(
            name=EnvironmentFieldName.CUDA_VERSION,
            value=proof.cuda_runtime_version,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
        EnvironmentFieldName.DRIVER_VERSION: EnvironmentField(
            name=EnvironmentFieldName.DRIVER_VERSION,
            value=proof.driver_version,
            provenance=ProvenanceKind.LOCALLY_VERIFIED,
            evidence_path=_INVOCATION_PATH,
        ),
    }
    return _manifest(
        run_id=run_id,
        captured_at=captured_at,
        fields_by_name=fields,
    )
