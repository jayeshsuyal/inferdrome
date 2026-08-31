"""Bundle reads use one stable byte snapshot across semantic verification."""

from pathlib import Path

import pytest

import inferdrome.bundle.reader as reader_module
from inferdrome.bundle.reader import BundleReader
from inferdrome.errors import VerificationError, WorkLimitError
from inferdrome.limits import WorkBudget, WorkLimits


def test_repeated_reads_return_one_snapshot_and_end_check_detects_change(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b'{"value":"first"}')
    reader = BundleReader(tmp_path, require_immutable=False)

    first = reader.read_bytes("artifact.json")
    artifact.write_bytes(b'{"value":"other"}')
    artifact.chmod(0o640)

    assert reader.read_bytes("artifact.json") == first
    with pytest.raises(VerificationError, match="changed during verification"):
        reader.assert_unchanged()


def test_end_check_detects_late_file_injection(tmp_path: Path) -> None:
    (tmp_path / "artifact.json").write_bytes(b"{}")
    reader = BundleReader(tmp_path, require_immutable=False)
    reader.read_bytes("artifact.json")

    (tmp_path / "injected.json").write_bytes(b"{}")

    with pytest.raises(VerificationError, match="changed during verification"):
        reader.assert_unchanged()


def test_same_size_rewrite_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"a" * 2_000_000)
    reader = BundleReader(tmp_path, require_immutable=False)
    original_read = reader_module.os.read
    changed = False

    def rewriting_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if content and not changed:
            changed = True
            artifact.write_bytes(b"b" * 2_000_000)
        return content

    monkeypatch.setattr(reader_module.os, "read", rewriting_read)

    with pytest.raises(VerificationError, match="changed during verification"):
        reader.read_bytes("artifact.bin")


def test_unchanged_snapshot_passes_end_check(tmp_path: Path) -> None:
    (tmp_path / "artifact.json").write_bytes(b"{}")
    reader = BundleReader(tmp_path, require_immutable=False)

    assert reader.read_bytes("artifact.json") == b"{}"
    reader.assert_unchanged()


def test_work_budget_charges_each_authoritative_tree_snapshot(
    tmp_path: Path,
) -> None:
    (tmp_path / "artifact.json").write_bytes(b"{}")
    exact_budget = WorkBudget(
        WorkLimits(max_units=1, max_bytes=4, max_seconds=1.0),
        clock=lambda: 1.0,
    )

    reader = BundleReader(
        tmp_path,
        require_immutable=False,
        work_budget=exact_budget,
    )
    reader.assert_unchanged()

    assert exact_budget.bytes == 4

    bounded_budget = WorkBudget(
        WorkLimits(max_units=1, max_bytes=3, max_seconds=1.0),
        clock=lambda: 1.0,
    )
    reader = BundleReader(
        tmp_path,
        require_immutable=False,
        work_budget=bounded_budget,
    )

    with pytest.raises(WorkLimitError, match="byte limit"):
        reader.assert_unchanged()
    assert bounded_budget.bytes == 2
