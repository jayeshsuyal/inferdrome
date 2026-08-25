"""Immutable outer deployment receipts and offline verification.

Receipts are additive deployment provenance.  They are not evidence bundles,
measurement outputs, or acceptance verdicts.  This slice issues only the
synthetic local form from the PR3 lifecycle; the executed form is reserved for
a later adapter that can supply independently verified proof inputs.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic.config import ExtraValues

from inferdrome import __version__
from inferdrome.deployment.lifecycle import (
    LifecycleOutcome,
    LifecycleStatus,
    canonical_lifecycle_outcome_bytes,
)
from inferdrome.deployment.spec import (
    BenchmarkTopology,
    BoundedName,
    CleanupPolicy,
    CostString,
    DeploymentSpec,
    ImageIdentity,
    LifecycleTimeouts,
    PinnedRevision,
    ResourceRequirements,
    deployment_spec_digest,
)
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_bytes,
)
from inferdrome.domain.ids import RunId, SemanticVersion, Sha256Digest, sha256_digest
from inferdrome.errors import AdapterError, VerificationError
from inferdrome.immutable import publish_immutable_directory

DEPLOYMENT_RECEIPT_SCHEMA_VERSION: Final = "inferdrome.deployment-receipt.v1"
DEPLOYMENT_RECEIPT_SCHEMA_ID: Final = "urn:inferdrome:deployment-receipt:v1"
RECEIPT_FILENAME: Final = "receipt.json"
_MAX_RECEIPT_BYTES = 1_048_576

LOCAL_PROVIDER_ADAPTER_ID: Final = "inferdrome.provider.local"
LOCAL_PROVIDER_ADAPTER_VERSION: Final = "1.0.0"
LOCAL_RUNTIME_ADAPTER_ID: Final = "inferdrome.runtime.vllm.mock"
LOCAL_RUNTIME_ADAPTER_VERSION: Final = "1.0.0"

GitCommit = Annotated[
    str,
    StringConstraints(
        min_length=40,
        max_length=64,
        pattern=r"^[0-9a-f]{40,64}$",
    ),
]
AdapterId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=96,
        pattern=(
            r"^inferdrome\.(?:provider|runtime)\.[a-z0-9]"
            r"(?:[a-z0-9._-]{0,92}[a-z0-9])?$"
        ),
    ),
]
ReceiptVersion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=64,
        pattern=(
            r"^[0-9]+\.[0-9]+\.[0-9]+"
            r"(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        ),
    ),
]


class ReceiptModel(FrozenModel):
    """Closed, strict, non-disclosing receipt model behavior."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class ReceiptAdapterIdentity(ReceiptModel):
    """Versioned adapter contract identity, never a mutable class name."""

    adapter_id: AdapterId
    adapter_version: SemanticVersion


class ReceiptDeclaredCostCeiling(ReceiptModel):
    """Declared controller estimate; this is not an invoice amount."""

    currency: Literal["USD"]
    max_cost_usd: CostString
    hard_limit: Literal[True]
    estimate_basis: Literal["controller_estimate"]


class ReceiptDeclaredConfiguration(ReceiptModel):
    """Safe projection of declared deployment intent, excluding credentials."""

    deployment_id: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
        ),
    ]
    mode: Literal["development", "proof"]
    execution_intent: Literal["mock_only", "local_execute", "dry_run_reference"]
    provider_id: Literal["local", "lambda_cloud", "gcp"]
    runtime_engine: Literal["vllm", "sglang"]
    runtime_version: SemanticVersion
    model_id: BoundedName
    model_revision: PinnedRevision
    tokenizer_revision: PinnedRevision
    runner_image: ImageIdentity
    serving_runtime_image: ImageIdentity | None
    topology: BenchmarkTopology
    resources: ResourceRequirements
    timeouts: LifecycleTimeouts
    cleanup_policy: CleanupPolicy
    cost_ceiling: ReceiptDeclaredCostCeiling


class ReceiptCleanupState(ReceiptModel):
    """The cleanup/final-confirmation projection bound to the lifecycle result."""

    runtime_stop_attempted: bool
    runtime_stop_confirmed: bool
    provider_cleanup_attempted: bool
    provider_cleanup_confirmed: bool
    provider_final_confirmation_attempted: bool
    provider_final_confirmation: bool
    orphaned: bool


class ReceiptLocalControllerFacts(ReceiptModel):
    observation_kind: Literal["lifecycle_controller_observed"]
    cleanup_state: ReceiptCleanupState


class ReceiptProviderAttestation(ReceiptModel):
    """Future provider assertion represented only by a bounded digest."""

    provider_id: Literal["local", "lambda_cloud", "gcp"]
    attestation_digest: Sha256Digest


class ReceiptExternalFacts(ReceiptModel):
    provider_attestation: ReceiptProviderAttestation | None


class ReceiptInvoiceTruth(ReceiptModel):
    status: Literal[
        "UNAVAILABLE_EXTERNAL_PROVIDER_INVOICE",
        "EXTERNALLY_REPORTED",
    ]
    currency: Literal["USD"] | None
    amount_usd: CostString | None

    @model_validator(mode="after")
    def validate_invoice_boundary(self) -> Self:
        if self.status == "UNAVAILABLE_EXTERNAL_PROVIDER_INVOICE":
            if self.currency is not None or self.amount_usd is not None:
                raise ValueError("unavailable invoice truth cannot contain an amount")
        elif self.currency is None or self.amount_usd is None:
            raise ValueError("reported invoice truth requires currency and amount")
        return self


class ReceiptEvidenceAnchor(ReceiptModel):
    """Future independently verified evidence anchor; absent for synthetic runs."""

    run_id: RunId
    bundle_digest: Sha256Digest
    archive_digest: Sha256Digest | None
    capture_manifest_digest: Sha256Digest | None


class DeploymentReceiptPayload(ReceiptModel):
    """Receipt fields excluding the non-recursive receipt identity."""

    schema_version: Literal["inferdrome.deployment-receipt.v1"]
    receipt_kind: Literal["synthetic_local", "executed"]
    deployment_spec_digest: Sha256Digest
    lifecycle_outcome_digest: Sha256Digest
    source_repository_commit: GitCommit
    inferdrome_version: ReceiptVersion
    provider_adapter: ReceiptAdapterIdentity
    runtime_adapter: ReceiptAdapterIdentity
    declared_configuration: ReceiptDeclaredConfiguration
    local_controller_facts: ReceiptLocalControllerFacts
    externally_asserted: ReceiptExternalFacts
    invoice_truth: ReceiptInvoiceTruth
    lifecycle_outcome: LifecycleOutcome
    evidence_anchor: ReceiptEvidenceAnchor | None
    synthetic_only: bool
    evidence_eligible: bool

    @model_validator(mode="after")
    def validate_receipt_semantics(self) -> Self:
        expected_cleanup = _cleanup_state_from_outcome(self.lifecycle_outcome)
        if self.local_controller_facts.cleanup_state != expected_cleanup:
            raise ValueError("receipt cleanup state disagrees with lifecycle outcome")

        if self.receipt_kind == "synthetic_local":
            if not self.synthetic_only or self.evidence_eligible:
                raise ValueError("synthetic receipts must be ineligible")
            if not self.lifecycle_outcome.synthetic_only:
                raise ValueError("synthetic receipt requires synthetic lifecycle")
            if self.lifecycle_outcome.evidence_eligible:
                raise ValueError("synthetic receipt cannot contain GPU eligibility")
            if self.evidence_anchor is not None:
                raise ValueError("synthetic receipt cannot contain evidence anchor")
            if self.externally_asserted.provider_attestation is not None:
                raise ValueError(
                    "synthetic receipt cannot contain provider attestation"
                )
            if self.invoice_truth.status != "UNAVAILABLE_EXTERNAL_PROVIDER_INVOICE":
                raise ValueError("synthetic receipt cannot contain invoice truth")
            return self

        if self.synthetic_only or not self.evidence_eligible:
            raise ValueError("executed receipt must declare eligibility")
        if (
            self.lifecycle_outcome.synthetic_only
            or not self.lifecycle_outcome.evidence_eligible
        ):
            raise ValueError("executed receipt requires eligible lifecycle")
        if self.lifecycle_outcome.status is not LifecycleStatus.SUCCEEDED:
            raise ValueError("executed receipt requires successful lifecycle")
        if not (
            expected_cleanup.runtime_stop_confirmed
            and expected_cleanup.provider_cleanup_confirmed
            and expected_cleanup.provider_final_confirmation
            and not expected_cleanup.orphaned
        ):
            raise ValueError("executed receipt requires confirmed cleanup")
        if self.evidence_anchor is None:
            raise ValueError("executed receipt requires evidence anchor")
        if self.externally_asserted.provider_attestation is None:
            raise ValueError("executed receipt requires provider attestation")
        return self


class DeploymentReceipt(DeploymentReceiptPayload):
    """Strict receipt with self-verifying non-recursive identity."""

    receipt_id: Sha256Digest

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
        _preflight_receipt_json(json_data)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        if self.receipt_id != deployment_receipt_id(self):
            raise ValueError("receipt identity does not match canonical payload")
        return self


@dataclass(frozen=True)
class PublishedDeploymentReceipt:
    path: Path
    receipt: DeploymentReceipt
    receipt_sha256: Sha256Digest


class ReceiptIssuanceError(AdapterError):
    """Receipt issuance failed closed without exposing adapter payloads."""


class ReceiptPublicationError(VerificationError):
    """Receipt publication/read-back failed closed."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("deployment receipt JSON object keys must be unique")
        value[key] = child
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value} is forbidden")


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("deployment receipt object keys must be strings")
            normalized = "".join(
                character.lower()
                for character in key
                if character.isalnum()
            )
            if normalized in {
                "accesstoken",
                "apikey",
                "authtoken",
                "password",
                "privatekey",
                "secret",
                "secretvalue",
                "token",
                "value",
            }:
                raise ValueError("credential-shaped receipt fields are forbidden")
            _reject_sensitive_fields(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _reject_sensitive_fields(child)


def _preflight_receipt_json(payload: str | bytes | bytearray) -> object:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
    else:
        raise ValueError("deployment receipt JSON input is invalid")
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise ValueError("deployment receipt JSON exceeds its bound")
    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("deployment receipt is not valid JSON") from None
    _reject_sensitive_fields(decoded)
    return decoded


def _model_json_value(model: BaseModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("deployment receipt must serialize as an object")
    return value


def deployment_receipt_payload_json_value(
    receipt: DeploymentReceipt | DeploymentReceiptPayload,
) -> dict[str, Any]:
    value = _model_json_value(receipt)
    value.pop("receipt_id", None)
    return value


def _deployment_receipt_payload_python_value(
    receipt: DeploymentReceiptPayload,
) -> dict[str, Any]:
    value = receipt.model_dump(mode="python", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("deployment receipt must serialize as an object")
    return value


def canonical_deployment_receipt_payload_bytes(
    receipt: DeploymentReceipt | DeploymentReceiptPayload,
) -> bytes:
    return canonical_json_bytes(deployment_receipt_payload_json_value(receipt))


def deployment_receipt_id(
    receipt: DeploymentReceipt | DeploymentReceiptPayload,
) -> str:
    return digest_bytes(
        DigestDomain.DEPLOYMENT_RECEIPT,
        canonical_deployment_receipt_payload_bytes(receipt),
    )


def canonical_deployment_receipt_bytes(receipt: DeploymentReceipt) -> bytes:
    return canonical_json_bytes(_model_json_value(receipt))


def deployment_receipt_sha256(receipt: DeploymentReceipt) -> str:
    """Hash exact complete canonical bytes; kept out to avoid recursion."""

    return sha256_digest(canonical_deployment_receipt_bytes(receipt))


def lifecycle_outcome_digest(outcome: LifecycleOutcome) -> str:
    return digest_bytes(
        DigestDomain.LIFECYCLE_OUTCOME,
        canonical_lifecycle_outcome_bytes(outcome),
    )


def parse_deployment_receipt_json(payload: str | bytes) -> DeploymentReceipt:
    _preflight_receipt_json(payload)
    return DeploymentReceipt.model_validate_json(payload)


def _cleanup_state_from_outcome(outcome: LifecycleOutcome) -> ReceiptCleanupState:
    return ReceiptCleanupState(
        runtime_stop_attempted=outcome.runtime_stop_attempted,
        runtime_stop_confirmed=outcome.runtime_stop_confirmed,
        provider_cleanup_attempted=outcome.provider_cleanup_attempted,
        provider_cleanup_confirmed=outcome.provider_cleanup_confirmed,
        provider_final_confirmation_attempted=(
            outcome.provider_final_confirmation_attempted
        ),
        provider_final_confirmation=outcome.provider_final_confirmation,
        orphaned=outcome.orphaned,
    )


def _declared_configuration_from_spec(
    spec: DeploymentSpec,
) -> ReceiptDeclaredConfiguration:
    return ReceiptDeclaredConfiguration(
        deployment_id=spec.deployment_id,
        mode=spec.mode,
        execution_intent=spec.execution_intent,
        provider_id=spec.provider.provider_id,
        runtime_engine=spec.runtime.engine,
        runtime_version=spec.runtime.engine_version,
        model_id=spec.runtime.model_id,
        model_revision=spec.runtime.model_revision,
        tokenizer_revision=spec.runtime.tokenizer_revision,
        runner_image=spec.artifacts.runner_image,
        serving_runtime_image=spec.artifacts.serving_runtime_image,
        topology=spec.topology,
        resources=spec.resources,
        timeouts=spec.timeouts,
        cleanup_policy=spec.cleanup_policy,
        cost_ceiling=ReceiptDeclaredCostCeiling(
            currency=spec.cost_ceiling.currency,
            max_cost_usd=spec.cost_ceiling.max_cost_usd,
            hard_limit=spec.cost_ceiling.hard_limit,
            estimate_basis=spec.cost_ceiling.estimate_basis,
        ),
    )


def _identity(adapter_id: str, version: str) -> ReceiptAdapterIdentity:
    return ReceiptAdapterIdentity(adapter_id=adapter_id, adapter_version=version)


def issue_synthetic_local_receipt(
    spec: DeploymentSpec,
    outcome: LifecycleOutcome,
    *,
    source_repository_commit: str,
    inferdrome_version: str = __version__,
    provider_adapter: ReceiptAdapterIdentity | None = None,
    runtime_adapter: ReceiptAdapterIdentity | None = None,
) -> DeploymentReceipt:
    """Issue only the PR3 synthetic local form from explicit immutable inputs."""

    if (
        spec.provider.provider_id != "local"
        or spec.execution_intent != "mock_only"
        or spec.mode != "development"
        or not outcome.synthetic_only
        or outcome.evidence_eligible
    ):
        raise ReceiptIssuanceError("synthetic receipt inputs are not eligible")
    selected_provider = provider_adapter or _identity(
        LOCAL_PROVIDER_ADAPTER_ID,
        LOCAL_PROVIDER_ADAPTER_VERSION,
    )
    selected_runtime = runtime_adapter or _identity(
        LOCAL_RUNTIME_ADAPTER_ID,
        LOCAL_RUNTIME_ADAPTER_VERSION,
    )
    expected_provider = _identity(
        LOCAL_PROVIDER_ADAPTER_ID,
        LOCAL_PROVIDER_ADAPTER_VERSION,
    )
    expected_runtime = _identity(
        LOCAL_RUNTIME_ADAPTER_ID,
        LOCAL_RUNTIME_ADAPTER_VERSION,
    )
    if selected_provider != expected_provider or selected_runtime != expected_runtime:
        raise ReceiptIssuanceError("synthetic adapter identity is unsupported")
    try:
        payload = DeploymentReceiptPayload(
            schema_version=DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
            receipt_kind="synthetic_local",
            deployment_spec_digest=deployment_spec_digest(spec),
            lifecycle_outcome_digest=lifecycle_outcome_digest(outcome),
            source_repository_commit=source_repository_commit,
            inferdrome_version=inferdrome_version,
            provider_adapter=selected_provider,
            runtime_adapter=selected_runtime,
            declared_configuration=_declared_configuration_from_spec(spec),
            local_controller_facts=ReceiptLocalControllerFacts(
                observation_kind="lifecycle_controller_observed",
                cleanup_state=_cleanup_state_from_outcome(outcome),
            ),
            externally_asserted=ReceiptExternalFacts(provider_attestation=None),
            invoice_truth=ReceiptInvoiceTruth(
                status="UNAVAILABLE_EXTERNAL_PROVIDER_INVOICE",
                currency=None,
                amount_usd=None,
            ),
            lifecycle_outcome=outcome,
            evidence_anchor=None,
            synthetic_only=True,
            evidence_eligible=False,
        )
        return DeploymentReceipt(
            **_deployment_receipt_payload_python_value(payload),
            receipt_id=deployment_receipt_id(payload),
        )
    except (ValidationError, TypeError, ValueError):
        raise ReceiptIssuanceError(
            "synthetic deployment receipt failed closed"
        ) from None


def verify_deployment_receipt(
    receipt: DeploymentReceipt,
    *,
    expected_spec: DeploymentSpec,
    expected_outcome: LifecycleOutcome,
    expected_source_repository_commit: str,
    expected_inferdrome_version: str,
    expected_provider_adapter: ReceiptAdapterIdentity,
    expected_runtime_adapter: ReceiptAdapterIdentity,
    expected_evidence_anchor: ReceiptEvidenceAnchor | None = None,
) -> DeploymentReceipt:
    """Verify self-identity and exact cross-input provenance bindings."""

    try:
        if receipt.deployment_spec_digest != deployment_spec_digest(expected_spec):
            raise VerificationError("receipt deployment specification digest disagrees")
        if receipt.lifecycle_outcome_digest != lifecycle_outcome_digest(
            expected_outcome
        ):
            raise VerificationError("receipt lifecycle outcome digest disagrees")
        if canonical_json_bytes(_model_json_value(receipt.lifecycle_outcome)) != (
            canonical_json_bytes(_model_json_value(expected_outcome))
        ):
            raise VerificationError("receipt lifecycle outcome disagrees")
        if receipt.source_repository_commit != expected_source_repository_commit:
            raise VerificationError("receipt source commit disagrees")
        if receipt.inferdrome_version != expected_inferdrome_version:
            raise VerificationError("receipt Inferdrome version disagrees")
        if receipt.provider_adapter != expected_provider_adapter:
            raise VerificationError("receipt provider adapter identity disagrees")
        if receipt.runtime_adapter != expected_runtime_adapter:
            raise VerificationError("receipt runtime adapter identity disagrees")
        if receipt.declared_configuration != _declared_configuration_from_spec(
            expected_spec
        ):
            raise VerificationError("receipt declared configuration disagrees")
        if expected_evidence_anchor != receipt.evidence_anchor:
            raise VerificationError("receipt evidence anchor disagrees")
        if receipt.receipt_id != deployment_receipt_id(receipt):
            raise VerificationError("receipt identity disagrees")
        return receipt
    except VerificationError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise VerificationError("receipt cross-input verification failed") from None


def verify_deployment_receipt_bytes(
    payload: str | bytes,
    **expected: Any,
) -> DeploymentReceipt:
    """Parse strict canonical bytes, then perform exact cross-input checks."""

    try:
        receipt = parse_deployment_receipt_json(payload)
        encoded = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else bytes(payload)
        )
        if encoded != canonical_deployment_receipt_bytes(receipt):
            raise VerificationError("receipt is not canonical JSON")
        return verify_deployment_receipt(receipt, **expected)
    except VerificationError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise VerificationError("receipt failed strict verification") from None


def _read_receipt_file(directory: Path) -> bytes:
    selected = directory.absolute()
    try:
        directory_metadata = os.lstat(selected)
    except OSError:
        raise ReceiptPublicationError(
            "receipt directory is unavailable or unsafe"
        ) from None
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or directory_metadata.st_mode & 0o222
    ):
        raise ReceiptPublicationError("receipt directory is unavailable or unsafe")
    descriptor_path = selected / RECEIPT_FILENAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(descriptor_path, flags)
    except OSError:
        raise ReceiptPublicationError(
            "receipt descriptor is unavailable or unsafe"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o222
            or metadata.st_size > _MAX_RECEIPT_BYTES
        ):
            raise ReceiptPublicationError("receipt descriptor is not bounded")
        content = bytearray()
        while len(content) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, _MAX_RECEIPT_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_RECEIPT_BYTES:
            raise ReceiptPublicationError("receipt descriptor exceeds its bound")
        return bytes(content)
    except ReceiptPublicationError:
        raise
    except OSError:
        raise ReceiptPublicationError("receipt descriptor could not be read") from None
    finally:
        os.close(descriptor)


def _revalidate_receipt_for_publication(
    receipt: DeploymentReceipt,
) -> tuple[DeploymentReceipt, bytes]:
    """Revalidate an in-memory receipt before any publication side effect."""

    try:
        content = canonical_deployment_receipt_bytes(receipt)
        validated = parse_deployment_receipt_json(content)
        if validated.receipt_id != receipt.receipt_id:
            raise ReceiptPublicationError(
                "receipt publication identity disagrees"
            )
        if validated.receipt_id != deployment_receipt_id(validated):
            raise ReceiptPublicationError(
                "receipt publication identity is invalid"
            )
        if canonical_deployment_receipt_bytes(validated) != content:
            raise ReceiptPublicationError(
                "receipt publication canonical bytes changed"
            )
        return validated, content
    except ReceiptPublicationError:
        raise
    except Exception:
        raise ReceiptPublicationError(
            "receipt publication input failed closed"
        ) from None


def publish_deployment_receipt(
    *,
    root: Path,
    receipt: DeploymentReceipt,
) -> PublishedDeploymentReceipt:
    """Publish one canonical receipt without replacement and verify read-back."""

    validated, content = _revalidate_receipt_for_publication(receipt)
    try:
        path = publish_immutable_directory(
            root=root,
            artifact_id=validated.receipt_id,
            filename=RECEIPT_FILENAME,
            content=content,
        )
        read_back = _read_receipt_file(path)
        if read_back != content:
            raise ReceiptPublicationError("receipt read-back bytes changed")
        parsed = parse_deployment_receipt_json(read_back)
        if canonical_deployment_receipt_bytes(parsed) != content:
            raise ReceiptPublicationError("receipt read-back verification failed")
    except ReceiptPublicationError:
        raise
    except (OSError, ValueError, ValidationError):
        raise ReceiptPublicationError("receipt publication failed closed") from None
    return PublishedDeploymentReceipt(
        path=path,
        receipt=parsed,
        receipt_sha256=sha256_digest(content),
    )


def verify_published_deployment_receipt(
    directory: Path,
    **expected: Any,
) -> PublishedDeploymentReceipt:
    content = _read_receipt_file(directory)
    try:
        receipt = verify_deployment_receipt_bytes(content, **expected)
    except VerificationError as error:
        raise ReceiptPublicationError(
            "published receipt verification failed"
        ) from error
    if directory.absolute().name != receipt.receipt_id:
        raise ReceiptPublicationError("published receipt directory identity disagrees")
    return PublishedDeploymentReceipt(
        path=directory.absolute(),
        receipt=receipt,
        receipt_sha256=sha256_digest(content),
    )


def deployment_receipt_schema() -> dict[str, Any]:
    generated = deepcopy(
        DeploymentReceipt.model_json_schema(
            by_alias=True,
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": DEPLOYMENT_RECEIPT_SCHEMA_ID,
        "$comment": (
            "Cross-field receipt identity, provenance, synthetic-only, and "
            "anchor rules are normative; this receipt never assigns an "
            "acceptance verdict and is outside frozen evidence schemas."
        ),
        **generated,
    }


__all__ = [
    "DEPLOYMENT_RECEIPT_SCHEMA_ID",
    "DEPLOYMENT_RECEIPT_SCHEMA_VERSION",
    "LOCAL_PROVIDER_ADAPTER_ID",
    "LOCAL_PROVIDER_ADAPTER_VERSION",
    "LOCAL_RUNTIME_ADAPTER_ID",
    "LOCAL_RUNTIME_ADAPTER_VERSION",
    "DeploymentReceipt",
    "DeploymentReceiptPayload",
    "GitCommit",
    "PublishedDeploymentReceipt",
    "ReceiptAdapterIdentity",
    "ReceiptCleanupState",
    "ReceiptDeclaredConfiguration",
    "ReceiptEvidenceAnchor",
    "ReceiptExternalFacts",
    "ReceiptInvoiceTruth",
    "ReceiptIssuanceError",
    "ReceiptLocalControllerFacts",
    "ReceiptProviderAttestation",
    "ReceiptPublicationError",
    "canonical_deployment_receipt_bytes",
    "canonical_deployment_receipt_payload_bytes",
    "deployment_receipt_id",
    "deployment_receipt_schema",
    "deployment_receipt_sha256",
    "issue_synthetic_local_receipt",
    "lifecycle_outcome_digest",
    "parse_deployment_receipt_json",
    "publish_deployment_receipt",
    "verify_deployment_receipt",
    "verify_deployment_receipt_bytes",
    "verify_published_deployment_receipt",
]
