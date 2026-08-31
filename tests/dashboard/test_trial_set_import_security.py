"""Adversarial import checks for immutable Trial Set descriptors."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import inferdrome.trials.service as trial_service
from inferdrome.errors import TrialSetError
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.trials import create_trial_set, verify_trial_set

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"


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


@pytest.fixture
def immutable_trial_set(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_experiment(FAKE_SOURCE, runs_root=runs_root, run_id=run_id)
    created = create_trial_set(
        runs_root=runs_root,
        trial_sets_root=trial_sets_root,
        run_ids=run_ids,
        title="Strict immutable import fixture",
        trial_set_id="trial-set-11111111111111111111111111111111",
    )
    try:
        yield created.path, runs_root
    finally:
        _make_tree_writable(tmp_path)


def test_exact_immutable_inventory_remains_verifiable(
    immutable_trial_set: tuple[Path, Path],
) -> None:
    trial_path, runs_root = immutable_trial_set

    verified = verify_trial_set(trial_path, runs_root=runs_root)

    assert verified.path == trial_path
    assert tuple(path.name for path in trial_path.iterdir()) == ("trial-set.json",)


def test_import_rejects_hardlinked_descriptor(
    immutable_trial_set: tuple[Path, Path],
) -> None:
    trial_path, runs_root = immutable_trial_set
    descriptor = trial_path / "trial-set.json"
    retained = trial_path.parent / "retained-trial-set.json"
    content = descriptor.read_bytes()
    trial_path.chmod(0o700)
    descriptor.unlink()
    retained.write_bytes(content)
    retained.chmod(0o400)
    os.link(retained, descriptor)
    trial_path.chmod(0o500)

    with pytest.raises(TrialSetError, match="one bounded read-only file"):
        verify_trial_set(trial_path, runs_root=runs_root)


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_import_rejects_undeclared_sibling(
    immutable_trial_set: tuple[Path, Path],
    entry_kind: str,
) -> None:
    trial_path, runs_root = immutable_trial_set
    trial_path.chmod(0o700)
    extra = trial_path / "undeclared"
    if entry_kind == "file":
        extra.write_bytes(b"undeclared\n")
        extra.chmod(0o400)
    else:
        extra.mkdir(mode=0o500)
    trial_path.chmod(0o500)

    with pytest.raises(TrialSetError, match="undeclared entries"):
        verify_trial_set(trial_path, runs_root=runs_root)


def test_import_rejects_descriptor_replacement_during_read(
    immutable_trial_set: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial_path, runs_root = immutable_trial_set
    descriptor = trial_path / "trial-set.json"
    content = descriptor.read_bytes()
    original_read = trial_service.os.read
    replaced = False

    def replace_after_read(file_descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(file_descriptor, size)
        if not replaced:
            replaced = True
            trial_path.chmod(0o700)
            descriptor.unlink()
            descriptor.write_bytes(content)
            descriptor.chmod(0o400)
            trial_path.chmod(0o500)
        return chunk

    monkeypatch.setattr(trial_service.os, "read", replace_after_read)

    with pytest.raises(TrialSetError, match="changed during its read"):
        verify_trial_set(trial_path, runs_root=runs_root)
    assert replaced


def test_import_rejects_directory_path_replacement_during_read(
    immutable_trial_set: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial_path, runs_root = immutable_trial_set
    content = (trial_path / "trial-set.json").read_bytes()
    displaced = trial_path.with_name(f"{trial_path.name}.displaced")
    original_read = trial_service.os.read
    replaced = False

    def replace_after_read(file_descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(file_descriptor, size)
        if not replaced:
            replaced = True
            trial_path.rename(displaced)
            trial_path.mkdir(mode=0o700)
            replacement = trial_path / "trial-set.json"
            replacement.write_bytes(content)
            replacement.chmod(0o400)
            trial_path.chmod(0o500)
        return chunk

    monkeypatch.setattr(trial_service.os, "read", replace_after_read)

    with pytest.raises(TrialSetError, match="directory changed during its read"):
        verify_trial_set(trial_path, runs_root=runs_root)
    assert replaced
