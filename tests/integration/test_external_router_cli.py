"""Exercise the namespaced local attached-router adapter CLI."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from tests.external_router_support import FIXTURE_ROOT


def _run_module_cli(
    arguments: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expected_exit_code: int = 0,
) -> tuple[str, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m inferdrome.external_router", *arguments],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("inferdrome.external_router", run_name="__main__")
    assert exited.value.code == expected_exit_code
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_module_cli_adapts_and_offline_verifies_one_local_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, errors = _run_module_cli(
        [
            "adapt-llmd",
            "--record",
            str(FIXTURE_ROOT / "attached-record.json"),
            "--router-config",
            str(FIXTURE_ROOT / "router-config.json"),
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert errors == ""
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(output, encoding="utf-8")

    summary, errors = _run_module_cli(
        ["verify", str(evidence_path)],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert errors == ""
    assert json.loads(summary) == {
        "evidence_admissibility": "ADMISSIBLE",
        "retained_digest": (
            "sha256:9ffa5c35e784a32dfda13b18d8667f6addd26676cf8b3e4a1e196c975ddb890f"
        ),
        "valid": True,
    }
