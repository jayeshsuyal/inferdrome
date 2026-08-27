"""Core execution rejects an unexpected prospective contract before reservation."""

import shutil
from pathlib import Path

import pytest

from inferdrome.bundle import verify_bundle
from inferdrome.errors import ResolutionError
from inferdrome.execution.orchestrator import run_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_expected_exitspec_digest_is_checked_before_workspace_reservation(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"

    with pytest.raises(
        ResolutionError,
        match="does not carry the expected ExitSpec contract digest",
    ):
        run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            expected_exitspec_contract_digest="sha256:" + "a" * 64,
        )

    assert not runs_root.exists()


def test_expected_exitspec_digest_shape_is_checked_before_workspace_reservation(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"

    with pytest.raises(
        ResolutionError,
        match="expected ExitSpec contract digest is invalid",
    ):
        run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            expected_exitspec_contract_digest="not-a-digest",
        )

    assert not runs_root.exists()


def test_exact_link_is_carried_into_a_successful_sealed_bundle(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "source"
    source_directory.mkdir()
    shutil.copytree(
        REPOSITORY_ROOT / "examples" / "workloads",
        source_directory / "workloads",
    )
    expected = "sha256:" + "b" * 64
    source = source_directory / "experiment.yaml"
    source.write_bytes(
        (REPOSITORY_ROOT / "examples" / "fake-smoke.yaml").read_bytes()
        + f"\nlinks:\n  exitspec_contract_digest: {expected}\n".encode()
    )
    result = run_experiment(
        source,
        runs_root=tmp_path / "runs",
        expected_exitspec_contract_digest=expected,
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.descriptor.digests.exitspec_contract_digest == expected
