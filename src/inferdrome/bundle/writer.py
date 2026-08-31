"""Staged, verified, read-only evidence-bundle publication."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import AwareDatetime, ValidationError

from inferdrome.bundle.manifest import IntegrityManifest, ManifestEntry
from inferdrome.bundle.verifier import VerificationReport, verify_bundle
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.evidence import (
    ArtifactDescriptor,
    ArtifactMediaType,
    ArtifactRole,
    ArtifactSensitivity,
    DigestDomains,
    EvidenceBundle,
    EvidenceExecutionMode,
    ProducerDescriptor,
    SensitivityDeclaration,
)
from inferdrome.domain.ids import ExperimentId, sha256_digest
from inferdrome.domain.states import (
    EnvironmentCompleteness,
    EvidenceEligibility,
    IntegrityStatus,
    Replayability,
    RunState,
)
from inferdrome.errors import BundleError, VerificationError, WorkspaceError
from inferdrome.immutable import _rename_no_replace
from inferdrome.workspace import RunWorkspace

_ARTIFACT_PATHS: dict[ArtifactRole, str] = {
    ArtifactRole.BUNDLE_DESCRIPTOR: "bundle.json",
    ArtifactRole.ORIGINAL_SPEC: "experiment.original.yaml",
    ArtifactRole.RESOLVED_SPEC: "experiment.resolved.json",
    ArtifactRole.REQUEST_PLAN: "request-plan.json",
    ArtifactRole.ENVIRONMENT: "environment.json",
    ArtifactRole.EXECUTION: "execution.json",
    ArtifactRole.PRODUCER_INVOCATION: "native/invocation.json",
    ArtifactRole.PRODUCER_VERSION: "native/producer-version.txt",
    ArtifactRole.PRODUCER_EXIT_STATUS: "native/exit-status.txt",
    ArtifactRole.NATIVE_RESULT: "native/benchmark-result.json",
    ArtifactRole.NATIVE_STDOUT: "native/stdout.log",
    ArtifactRole.NATIVE_STDERR: "native/stderr.log",
    ArtifactRole.REQUEST_RECORDS: "records/requests.jsonl",
    ArtifactRole.METRIC_DEFINITIONS: "definitions/metrics.json",
    ArtifactRole.MEASUREMENTS: "derived/measurements.json",
    ArtifactRole.INTEGRITY_MANIFEST: "integrity/artifact-hashes.json",
}

_MEDIA_TYPES: dict[ArtifactRole, ArtifactMediaType] = {
    ArtifactRole.BUNDLE_DESCRIPTOR: ArtifactMediaType.JSON,
    ArtifactRole.ORIGINAL_SPEC: ArtifactMediaType.YAML,
    ArtifactRole.RESOLVED_SPEC: ArtifactMediaType.JSON,
    ArtifactRole.REQUEST_PLAN: ArtifactMediaType.JSON,
    ArtifactRole.ENVIRONMENT: ArtifactMediaType.JSON,
    ArtifactRole.EXECUTION: ArtifactMediaType.JSON,
    ArtifactRole.PRODUCER_INVOCATION: ArtifactMediaType.JSON,
    ArtifactRole.PRODUCER_VERSION: ArtifactMediaType.TEXT,
    ArtifactRole.PRODUCER_EXIT_STATUS: ArtifactMediaType.TEXT,
    ArtifactRole.NATIVE_RESULT: ArtifactMediaType.JSON,
    ArtifactRole.NATIVE_STDOUT: ArtifactMediaType.TEXT,
    ArtifactRole.NATIVE_STDERR: ArtifactMediaType.TEXT,
    ArtifactRole.REQUEST_RECORDS: ArtifactMediaType.JSONL,
    ArtifactRole.METRIC_DEFINITIONS: ArtifactMediaType.JSON,
    ArtifactRole.MEASUREMENTS: ArtifactMediaType.JSON,
    ArtifactRole.INTEGRITY_MANIFEST: ArtifactMediaType.JSON,
}

_PAYLOAD_ROLES = frozenset(ArtifactRole) - {
    ArtifactRole.BUNDLE_DESCRIPTOR,
    ArtifactRole.INTEGRITY_MANIFEST,
}


class BundleMetadata(FrozenModel):
    experiment_id: ExperimentId
    created_at: AwareDatetime
    execution_mode: EvidenceExecutionMode
    environment_completeness: EnvironmentCompleteness
    evidence_eligibility: EvidenceEligibility
    replayability: Replayability
    producer: ProducerDescriptor
    digests: DigestDomains
    sensitivity: SensitivityDeclaration


@dataclass(frozen=True)
class SealedBundle:
    path: Path
    bundle_digest: str
    descriptor: EvidenceBundle
    verification: VerificationReport


def _canonical_model_bytes(model: FrozenModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sensitivity_for_role(
    role: ArtifactRole,
    declaration: SensitivityDeclaration,
) -> ArtifactSensitivity:
    if (
        role in {ArtifactRole.ORIGINAL_SPEC, ArtifactRole.REQUEST_PLAN}
        and declaration.prompt_content_in_request_plan
    ):
        return ArtifactSensitivity.PROMPT_CONTENT
    if role is ArtifactRole.NATIVE_RESULT:
        if declaration.native_response_content_present:
            return ArtifactSensitivity.RESPONSE_CONTENT
        return ArtifactSensitivity.INTERNAL_DIAGNOSTIC
    if (
        role is ArtifactRole.REQUEST_RECORDS
        and declaration.canonical_response_content_included
    ):
        return ArtifactSensitivity.RESPONSE_CONTENT
    if role in {
        ArtifactRole.ENVIRONMENT,
        ArtifactRole.PRODUCER_INVOCATION,
        ArtifactRole.NATIVE_STDOUT,
        ArtifactRole.NATIVE_STDERR,
    }:
        return ArtifactSensitivity.INTERNAL_DIAGNOSTIC
    return ArtifactSensitivity.PUBLIC


def _artifact_descriptors(
    sensitivity: SensitivityDeclaration,
) -> tuple[ArtifactDescriptor, ...]:
    return tuple(
        ArtifactDescriptor(
            path=_ARTIFACT_PATHS[role],
            role=role,
            media_type=_MEDIA_TYPES[role],
            sensitivity=_sensitivity_for_role(role, sensitivity),
            required=True,
        )
        for role in ArtifactRole
    )


def _make_read_only(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o400)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o500)
    root.chmod(0o500)


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("bundle publication path is not a real directory")
    return metadata.st_dev, metadata.st_ino


def seal_bundle(
    workspace: RunWorkspace,
    metadata: BundleMetadata,
    payloads: dict[ArtifactRole, bytes],
) -> SealedBundle:
    """Stage, verify, freeze, atomically publish, and verify one bundle."""

    current_state = workspace.current_state()
    if current_state.state is not RunState.FINALIZING:
        raise BundleError("bundle sealing requires a FINALIZING run workspace")
    workspace.verify_frozen_inputs()
    if set(payloads) != _PAYLOAD_ROLES:
        raise BundleError("bundle payloads must cover every writer-owned artifact role")
    if any(not isinstance(content, bytes) for content in payloads.values()):
        raise BundleError("bundle payloads must be exact bytes")

    staging_path = workspace.path / "bundle.staging"
    final_path = workspace.path / "bundle"
    if staging_path.exists() or final_path.exists():
        raise BundleError("bundle staging or final path already exists")

    try:
        descriptors = _artifact_descriptors(metadata.sensitivity)
        descriptor = EvidenceBundle(
            schema_version="inferdrome.evidence.v1",
            run_id=workspace.run_id,
            experiment_id=metadata.experiment_id,
            created_at=metadata.created_at,
            execution_mode=metadata.execution_mode,
            run_state="COMPLETE",
            integrity_status="VALID",
            environment_completeness=metadata.environment_completeness,
            evidence_eligibility=metadata.evidence_eligibility,
            replayability=metadata.replayability,
            producer=metadata.producer,
            digests=metadata.digests,
            sensitivity=metadata.sensitivity,
            integrity_manifest_path=(
                _ARTIFACT_PATHS[ArtifactRole.INTEGRITY_MANIFEST]
            ),
            artifacts=descriptors,
        )
    except ValidationError as error:
        raise BundleError("bundle metadata violates the evidence contract") from error
    descriptor_bytes = _canonical_model_bytes(descriptor)
    content_by_role = {
        **payloads,
        ArtifactRole.BUNDLE_DESCRIPTOR: descriptor_bytes,
    }

    try:
        staging_path.mkdir(mode=0o700)
        for role, content in content_by_role.items():
            _write_new(staging_path / _ARTIFACT_PATHS[role], content)

        entries = tuple(
            ManifestEntry(
                path=_ARTIFACT_PATHS[role],
                role=role,
                size_bytes=len(content_by_role[role]),
                sha256=sha256_digest(content_by_role[role]),
            )
            for role in sorted(
                content_by_role,
                key=lambda candidate: _ARTIFACT_PATHS[candidate],
            )
        )
        manifest = IntegrityManifest(
            schema_version="inferdrome.integrity-manifest.v1",
            run_id=workspace.run_id,
            hash_algorithm="sha256",
            path_ordering="normalized_posix_ascending_v1",
            entries=entries,
        )
        manifest_bytes = _canonical_model_bytes(manifest)
        _write_new(
            staging_path / _ARTIFACT_PATHS[ArtifactRole.INTEGRITY_MANIFEST],
            manifest_bytes,
        )
    except (OSError, ValueError, ValidationError) as error:
        raise BundleError("bundle staging failed closed") from error

    bundle_digest = digest_bytes(DigestDomain.BUNDLE_MANIFEST, manifest_bytes)
    try:
        verify_bundle(
            staging_path,
            expected_bundle_digest=bundle_digest,
            require_immutable=False,
        )
        _make_read_only(staging_path)
        staged_directory_identity = _directory_identity(staging_path)
        _rename_no_replace(staging_path, final_path)
        if _directory_identity(final_path) != staged_directory_identity:
            raise VerificationError("published bundle identity disagrees with staging")
        verification = verify_bundle(
            final_path,
            expected_bundle_digest=bundle_digest,
            require_immutable=True,
        )
        if _directory_identity(final_path) != staged_directory_identity:
            raise VerificationError(
                "published bundle identity changed during verification"
            )
        workspace.transition(
            RunState.COMPLETE,
            integrity_status=IntegrityStatus.VALID,
        )
    except (OSError, VerificationError, WorkspaceError) as error:
        raise BundleError("bundle sealing or final verification failed") from error

    return SealedBundle(
        path=final_path,
        bundle_digest=bundle_digest,
        descriptor=descriptor,
        verification=verification,
    )
