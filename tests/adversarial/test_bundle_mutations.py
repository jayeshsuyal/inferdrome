"""Material mutation, deletion, injection, and unsafe-node rejection."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from inferdrome.bundle import verify_bundle
from inferdrome.bundle.reader import BundleLimits
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import VerificationError
from tests.conftest import SealedFakeFixture


def _mutable_copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    for directory, directory_names, filenames in os.walk(destination):
        current = Path(directory)
        current.chmod(0o700)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        for filename in filenames:
            (current / filename).chmod(0o600)
    return destination


def _rehash_manifest_entry(bundle: Path, relative_path: str) -> None:
    manifest_path = bundle / "integrity" / "artifact-hashes.json"
    manifest = json.loads(manifest_path.read_bytes())
    content = (bundle / relative_path).read_bytes()
    for entry in manifest["entries"]:
        if entry["path"] == relative_path:
            entry["size_bytes"] = len(content)
            entry["sha256"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
            break
    else:
        raise AssertionError("manifest entry not found")
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def test_changed_artifact_is_detected(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "changed")
    measurements = bundle / "derived" / "measurements.json"
    measurements.write_bytes(measurements.read_bytes() + b" ")

    with pytest.raises(VerificationError, match=r"size|hash"):
        verify_bundle(bundle, require_immutable=False)


def test_deleted_and_undeclared_artifacts_are_detected(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    deleted = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "deleted")
    (deleted / "native" / "stdout.log").unlink()
    with pytest.raises(VerificationError, match="missing or undeclared"):
        verify_bundle(deleted, require_immutable=False)

    injected = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "injected")
    (injected / "extra.txt").write_text("undeclared", encoding="utf-8")
    with pytest.raises(VerificationError, match="missing or undeclared"):
        verify_bundle(injected, require_immutable=False)


def test_symlink_is_rejected_before_content_use(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "symlink")
    stdout = bundle / "native" / "stdout.log"
    stdout.unlink()
    stdout.symlink_to("producer-version.txt")

    with pytest.raises(VerificationError, match="symlinks"):
        verify_bundle(bundle, require_immutable=False)


def test_hard_link_is_rejected_before_content_use(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "hardlink")
    stdout = bundle / "native" / "stdout.log"
    stdout.unlink()
    os.link(bundle / "native" / "producer-version.txt", stdout)

    with pytest.raises(VerificationError, match="hard links"):
        verify_bundle(bundle, require_immutable=False)


def test_retained_bundle_digest_detects_coherent_rehash(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "rehashed")
    stdout = bundle / "native" / "stdout.log"
    stdout.write_bytes(b"rewritten synthetic output\n")
    _rehash_manifest_entry(bundle, "native/stdout.log")

    with pytest.raises(VerificationError, match="retained digest"):
        verify_bundle(
            bundle,
            expected_bundle_digest=sealed_fake_bundle.sealed.bundle_digest,
            require_immutable=False,
        )


def test_rehashed_diagnostic_is_a_new_internally_valid_bundle(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "new-digest")
    stdout = bundle / "native" / "stdout.log"
    stdout.write_bytes(b"different but valid diagnostic output\n")
    _rehash_manifest_entry(bundle, "native/stdout.log")

    report = verify_bundle(bundle, require_immutable=False)

    assert report.bundle_digest != sealed_fake_bundle.sealed.bundle_digest


def test_duplicate_json_key_is_rejected_even_after_manifest_rehash(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "duplicate")
    descriptor = bundle / "bundle.json"
    original = descriptor.read_bytes()
    duplicate = b'{"schema_version":"inferdrome.evidence.v1",' + original[1:]
    descriptor.write_bytes(duplicate)
    _rehash_manifest_entry(bundle, "bundle.json")

    with pytest.raises(VerificationError, match="duplicate JSON keys"):
        verify_bundle(bundle, require_immutable=False)


def test_writable_copy_is_not_accepted_as_sealed(
    sealed_fake_bundle: SealedFakeFixture, tmp_path: Path
) -> None:
    bundle = _mutable_copy(sealed_fake_bundle.sealed.path, tmp_path / "writable")
    with pytest.raises(VerificationError, match="root is writable"):
        verify_bundle(bundle)


def test_reader_limits_fail_before_artifact_use(
    sealed_fake_bundle: SealedFakeFixture,
) -> None:
    with pytest.raises(VerificationError, match="file count"):
        verify_bundle(
            sealed_fake_bundle.sealed.path,
            limits=BundleLimits(max_files=1),
        )
    with pytest.raises(VerificationError, match="file exceeds"):
        verify_bundle(
            sealed_fake_bundle.sealed.path,
            limits=BundleLimits(max_file_bytes=1),
        )


def test_bundle_limits_require_positive_integers() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        BundleLimits(max_files=1.5)  # type: ignore[arg-type]
