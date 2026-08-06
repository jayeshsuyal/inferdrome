"""Staged fake evidence seals only after offline verification succeeds."""

import hashlib
import os
import stat
from pathlib import Path

from inferdrome.bundle import verify_bundle
from inferdrome.bundle.manifest import IntegrityManifest
from inferdrome.domain.states import IntegrityStatus, RunState
from tests.conftest import SealedFakeFixture


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    snapshot = {}
    files = sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    )
    for path in files:
        content = path.read_bytes()
        snapshot[path.relative_to(root).as_posix()] = (
            stat.S_IMODE(path.stat().st_mode),
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
    return snapshot


def test_bundle_is_verified_read_only_and_completed(
    sealed_fake_bundle: SealedFakeFixture,
) -> None:
    fixture = sealed_fake_bundle
    bundle_path = fixture.sealed.path

    assert bundle_path.name == "bundle"
    assert not (fixture.workspace.path / "bundle.staging").exists()
    assert fixture.workspace.current_state().state is RunState.COMPLETE
    assert (
        fixture.workspace.current_state().integrity_status
        is IntegrityStatus.VALID
    )
    assert fixture.sealed.verification.artifact_count == 16
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o500
    for path in bundle_path.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o500 if path.is_dir() else 0o400)

    descriptor_bytes = (bundle_path / "bundle.json").read_bytes()
    assert b'"bundle_digest"' not in descriptor_bytes
    manifest = IntegrityManifest.model_validate_json(
        (bundle_path / "integrity" / "artifact-hashes.json").read_bytes()
    )
    assert all(entry.role.value != "integrity_manifest" for entry in manifest.entries)


def test_offline_verification_is_read_only_and_digest_anchored(
    sealed_fake_bundle: SealedFakeFixture,
) -> None:
    sealed = sealed_fake_bundle.sealed
    before = _tree_snapshot(sealed.path)

    report = verify_bundle(
        sealed.path,
        expected_bundle_digest=sealed.bundle_digest,
    )

    assert report.bundle_digest == sealed.bundle_digest
    assert report.run_id == sealed_fake_bundle.workspace.run_id
    assert _tree_snapshot(sealed.path) == before
    assert not any(path.name.endswith(".tmp") for path in sealed.path.rglob("*"))


def test_bundle_digest_is_domain_separated_from_plain_manifest_hash(
    sealed_fake_bundle: SealedFakeFixture,
) -> None:
    manifest_bytes = (
        sealed_fake_bundle.sealed.path / "integrity" / "artifact-hashes.json"
    ).read_bytes()
    plain = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    assert sealed_fake_bundle.sealed.bundle_digest != plain


def test_no_bundle_file_or_directory_remains_owner_writable(
    sealed_fake_bundle: SealedFakeFixture,
) -> None:
    bundle_path = sealed_fake_bundle.sealed.path
    for path in [bundle_path, *bundle_path.rglob("*")]:
        assert not stat.S_IMODE(os.lstat(path).st_mode) & 0o222
