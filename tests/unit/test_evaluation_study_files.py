"""Adversarial ownership, byte bounds, and no-replace study publication."""

import os
import stat
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.study_files import StudyDirectory, trial_filename


def test_private_bundle_never_replaces_and_retains_completed_files(
    tmp_path: Path,
) -> None:
    output = tmp_path / "study"
    with StudyDirectory.create(output, budget=100) as directory:
        directory.write("plan.json", b"{}\n", limit=10)
        with pytest.raises(FileExistsError):
            directory.write("plan.json", b"replacement", limit=20)
        directory.write(trial_filename(0), b"result\n", limit=10)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "plan.json").stat().st_mode) == 0o400
    with pytest.raises(FileExistsError):
        StudyDirectory.create(output, budget=100)
    with StudyDirectory.open(output) as directory:
        assert directory.read("plan.json", limit=10) == b"{}\n"
        assert directory.read(trial_filename(0), limit=10) == b"result\n"


@pytest.mark.parametrize(
    "name", ["../plan.json", "/plan.json", "x.json", "trial-0256.json"]
)
def test_only_finite_generated_names_are_admitted(tmp_path: Path, name: str) -> None:
    with (
        StudyDirectory.create(tmp_path / "study", budget=100) as directory,
        pytest.raises(EvaluationError),
    ):
        directory.write(name, b"x", limit=10)


def test_output_bounds_checked_before_reserving_file(tmp_path: Path) -> None:
    output = tmp_path / "study"
    with StudyDirectory.create(output, budget=8) as directory:
        with pytest.raises(EvaluationError):
            directory.write("plan.json", b"too large", limit=8)
        assert not (output / "plan.json").exists()
        directory.write("plan.json", b"12345", limit=8)
        with pytest.raises(EvaluationError):
            directory.write("manifest.json", b"1234", limit=8)
        assert not (output / "manifest.json").exists()
    with (
        StudyDirectory.open(output, budget=4) as directory,
        pytest.raises(EvaluationError),
    ):
        directory.read("plan.json", limit=8)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo"])
def test_unsafe_artifact_is_rejected_without_blocking(
    tmp_path: Path, kind: str
) -> None:
    output = tmp_path / "study"
    output.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_bytes(b"private")
    source.chmod(0o600)
    artifact = output / "plan.json"
    if kind == "symlink":
        artifact.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, artifact)
    elif kind == "fifo":
        os.mkfifo(artifact, 0o600)
    else:
        artifact.write_bytes(b"public")
        artifact.chmod(0o644)
    with (
        StudyDirectory.open(output) as directory,
        pytest.raises((OSError, EvaluationError)),
    ):
        directory.read("plan.json", limit=100)


def test_directory_descriptor_survives_path_replacement(tmp_path: Path) -> None:
    output, moved = tmp_path / "study", tmp_path / "moved"
    with StudyDirectory.create(output, budget=100) as directory:
        output.rename(moved)
        output.mkdir(mode=0o700)
        directory.write("plan.json", b"held inode", limit=100)
    assert not (output / "plan.json").exists()
    assert (moved / "plan.json").read_bytes() == b"held inode"


def test_symlink_ancestor_and_public_directory_are_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        StudyDirectory.create(alias / "study", budget=100)
    actual.chmod(0o755)
    with pytest.raises(EvaluationError):
        StudyDirectory.open(actual)


def test_same_size_mutation_during_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "study"
    with StudyDirectory.create(output, budget=100) as directory:
        directory.write("plan.json", b"original", limit=100)
    artifact = output / "plan.json"
    original_read = os.read
    mutated = False

    def read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        content = original_read(descriptor, count)
        if content and not mutated:
            mutated = True
            artifact.chmod(0o600)
            artifact.write_bytes(b"tampered")
        return content

    monkeypatch.setattr(os, "read", read)
    with (
        StudyDirectory.open(output) as directory,
        pytest.raises(EvaluationError, match="changed during read"),
    ):
        directory.read("plan.json", limit=100)


def test_partial_write_failure_removes_only_own_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "study"
    with StudyDirectory.create(output, budget=100) as directory:
        directory.write("plan.json", b"preserved", limit=100)

        def fail_write(descriptor: int, content: bytes) -> int:
            raise OSError("synthetic write failure")

        monkeypatch.setattr(os, "write", fail_write)
        with pytest.raises(OSError):
            directory.write("manifest.json", b"incomplete", limit=100)
    assert (output / "plan.json").read_bytes() == b"preserved"
    assert not (output / "manifest.json").exists()
