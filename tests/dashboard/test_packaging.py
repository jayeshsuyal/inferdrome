"""Packaging contract for the dashboard's production frontend."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_dashboard_install import verify_install

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_source_package_contains_and_serves_production_dashboard() -> None:
    verify_install()


def _pinned_uv() -> Path:
    configured = os.environ.get("INFERDROME_UV")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = REPOSITORY_ROOT / candidate
        if candidate.is_file():
            return candidate.resolve()
    sibling = Path(sys.executable).with_name("uv")
    if sibling.is_file():
        return sibling
    discovered = shutil.which("uv")
    if discovered is None:
        pytest.skip("uv is unavailable outside the CI bootstrap")
    return Path(discovered).resolve()


def test_uv_native_package_gate_runs_with_pipless_python(
    tmp_path: Path,
) -> None:
    uv = _pinned_uv()
    reported_version = subprocess.run(
        [str(uv), "--version"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert re.fullmatch(r"uv 0\.8\.17(?: \([^\r\n)]+\))?", reported_version)

    pip_probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if os.environ.get("CI") == "true":
        assert pip_probe.returncode != 0

    pipless_python = tmp_path / "pipless-python"
    pipless_python.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then',
                "  exit 97",
                "fi",
                f"exec {shlex.quote(sys.executable)} \"$@\"",
                "",
            )
        ),
        encoding="utf-8",
    )
    pipless_python.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "INFERDROME_PYTHON": str(pipless_python),
            "INFERDROME_UV": str(uv),
            "TMPDIR": str(tmp_path),
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    completed = subprocess.run(
        [str(REPOSITORY_ROOT / "scripts/dashboard_package_gate.sh")],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "dashboard installed-wheel smoke: ok" in completed.stdout
    assert "release installed-wheel CLI smoke: ok" in completed.stdout
