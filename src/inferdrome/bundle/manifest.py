"""Canonical exact-byte artifact-hash manifest."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.evidence import ArtifactRole
from inferdrome.domain.ids import RelativeArtifactPath, RunId, Sha256Digest


class ManifestEntry(FrozenModel):
    path: RelativeArtifactPath
    role: ArtifactRole
    size_bytes: Annotated[int, Field(strict=True, ge=0)]
    sha256: Sha256Digest


_HASHED_ROLES = frozenset(ArtifactRole) - {ArtifactRole.INTEGRITY_MANIFEST}


class IntegrityManifest(FrozenModel):
    schema_version: Literal["inferdrome.integrity-manifest.v1"]
    run_id: RunId
    hash_algorithm: Literal["sha256"]
    path_ordering: Literal["normalized_posix_ascending_v1"]
    entries: Annotated[tuple[ManifestEntry, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_closed_manifest(self) -> "IntegrityManifest":
        paths = tuple(entry.path for entry in self.entries)
        roles = tuple(entry.role for entry in self.entries)
        if paths != tuple(sorted(paths)):
            raise ValueError("manifest entries must use ascending path order")
        if len(paths) != len(set(paths)) or len(roles) != len(set(roles)):
            raise ValueError("manifest paths and roles must be unique")
        if set(roles) != _HASHED_ROLES:
            raise ValueError("manifest must hash every non-manifest artifact role")
        return self
