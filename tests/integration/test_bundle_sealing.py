"""Staged fake evidence seals only after offline verification succeeds."""

import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

import inferdrome.bundle.writer as bundle_writer
from inferdrome.bundle import verify_bundle
from inferdrome.bundle.manifest import IntegrityManifest
from inferdrome.domain.states import IntegrityStatus, RunState
from inferdrome.errors import BundleError
from inferdrome.execution.orchestrator import run_experiment
from tests.conftest import SealedFakeFixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"


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


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


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


def test_publication_collision_preserves_destination_and_staged_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    run_id = "run-cccccccccccccccccccccccccccccccc"
    workspace_path = runs_root / run_id
    staging_path = workspace_path / "bundle.staging"
    final_path = workspace_path / "bundle"
    original_rename = bundle_writer._rename_no_replace
    collision_identity: tuple[int, int] | None = None

    def collide_before_publish(source: Path, destination: Path) -> None:
        nonlocal collision_identity
        destination.mkdir(mode=0o700)
        metadata = os.lstat(destination)
        collision_identity = metadata.st_dev, metadata.st_ino
        original_rename(source, destination)

    monkeypatch.setattr(
        bundle_writer,
        "_rename_no_replace",
        collide_before_publish,
    )
    try:
        with pytest.raises(
            BundleError,
            match="bundle sealing or final verification failed",
        ):
            run_experiment(FAKE_SOURCE, runs_root=runs_root, run_id=run_id)

        assert collision_identity is not None
        final_metadata = os.lstat(final_path)
        assert (final_metadata.st_dev, final_metadata.st_ino) == collision_identity
        assert list(final_path.iterdir()) == []
        assert staging_path.is_dir()
        assert (staging_path / "bundle.json").is_file()
        staged_files = tuple(
            path for path in staging_path.rglob("*") if path.is_file()
        )
        assert len(staged_files) == 16
    finally:
        _make_tree_writable(tmp_path)


def test_publication_rejects_content_identical_directory_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    run_id = "run-dddddddddddddddddddddddddddddddd"
    workspace_path = runs_root / run_id
    final_path = workspace_path / "bundle"
    displaced_path = workspace_path / "bundle.displaced"
    original_rename = bundle_writer._rename_no_replace
    identities: tuple[tuple[int, int], tuple[int, int]] | None = None

    def substitute_after_publish(source: Path, destination: Path) -> None:
        nonlocal identities
        original_rename(source, destination)
        destination.rename(displaced_path)
        shutil.copytree(displaced_path, destination, copy_function=shutil.copy2)
        displaced = os.lstat(displaced_path)
        replacement = os.lstat(destination)
        identities = (
            (displaced.st_dev, displaced.st_ino),
            (replacement.st_dev, replacement.st_ino),
        )

    monkeypatch.setattr(
        bundle_writer,
        "_rename_no_replace",
        substitute_after_publish,
    )
    try:
        with pytest.raises(
            BundleError,
            match="bundle sealing or final verification failed",
        ):
            run_experiment(FAKE_SOURCE, runs_root=runs_root, run_id=run_id)

        assert identities is not None
        assert identities[0] != identities[1]
        assert final_path.joinpath("bundle.json").read_bytes() == (
            displaced_path / "bundle.json"
        ).read_bytes()
    finally:
        _make_tree_writable(tmp_path)
