"""The real-GPU demo rejects package drift before reserving proof output."""

import subprocess
import sys
from pathlib import Path

import pytest

import scripts.run_real_gpu_demo as demo


def _completed(
    stdout: bytes = b"",
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[sys.executable, "-m", "pip"],
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
    )


def test_package_inventory_is_canonical_and_runtime_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = tmp_path / "python-packages.txt"
    retained = b"alpha==1\ninferdrome @ file:///repo\nzeta==2\n"
    packages.write_bytes(retained)
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[-2:] == ["freeze", "--all"]:
            return _completed(b"zeta==2\nalpha==1\ninferdrome @ file:///repo\n")
        return _completed(b"No broken requirements found.\n")

    monkeypatch.setattr(demo.subprocess, "run", run)

    demo._require_package_environment_unchanged(packages)

    assert calls == [
        [sys.executable, "-m", "pip", "freeze", "--all"],
        [sys.executable, "-m", "pip", "check"],
    ]


def test_package_inventory_drift_fails_before_pip_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = tmp_path / "python-packages.txt"
    packages.write_bytes(b"alpha==1\n")
    calls = 0

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        assert argv[-2:] == ["freeze", "--all"]
        return _completed(b"alpha==2\n")

    monkeypatch.setattr(demo.subprocess, "run", run)

    with pytest.raises(demo.DemoError, match="changed after host preparation"):
        demo._require_package_environment_unchanged(packages)

    assert calls == 1


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"zeta==2\nalpha==1\n",
        b"alpha==1\nalpha==1\n",
        b"alpha==1\x00\n",
        b"\xff\n",
    ],
)
def test_noncanonical_package_inventory_is_rejected(
    tmp_path: Path,
    content: bytes,
) -> None:
    packages = tmp_path / "python-packages.txt"
    packages.write_bytes(content)

    with pytest.raises(demo.DemoError, match="package inventory"):
        demo._require_package_environment_unchanged(packages)
