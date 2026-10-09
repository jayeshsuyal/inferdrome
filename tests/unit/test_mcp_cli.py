"""Tests for the read-only MCP CLI entrypoint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from inferdrome.mcp.cli import main


def _full_bundle(root: Path, name: str, *, gpu_model: str) -> None:
    run_dir = root / name
    support = run_dir / "capture" / "support"
    support.mkdir(parents=True)
    content = f"archive-{name}".encode()
    digest = hashlib.sha256(content).hexdigest()
    (run_dir / "capture.tar.gz").write_bytes(content)
    (run_dir / "capture.tar.gz.metadata.json").write_bytes(
        json.dumps(
            {"archive_sha256": "sha256:" + digest, "size_bytes": len(content)}
        ).encode()
    )
    (run_dir / "retrieval-receipt.json").write_bytes(
        json.dumps({"managed_capability_profile": "p"}).encode()
    )
    (support / "workload-manifest.json").write_bytes(
        json.dumps(
            {"model_id": "Qwen/Qwen3-8B", "model_revision": "abc", "line_count": 96}
        ).encode()
    )
    bundle = run_dir / "capture" / "runs" / f"run-{name}" / "bundle"
    (bundle / "derived").mkdir(parents=True)
    (bundle / "environment.json").write_bytes(
        json.dumps({"fields": [{"name": "gpu.model", "value": gpu_model}]}).encode()
    )
    (bundle / "derived" / "measurements.json").write_bytes(
        json.dumps(
            {
                "measurements": [
                    {
                        "metric": "ttft_ns",
                        "aggregation": "mean",
                        "unit": "ns",
                        "value": 100.0,
                    }
                ]
            }
        ).encode()
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _full_bundle(root, "run-a", gpu_model="NVIDIA A10")
    _full_bundle(root, "run-b", gpu_model="NVIDIA A100-SXM4-40GB")
    return root


def test_cli_list_runs_emits_json_array(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list-runs", "--evidence-root", str(_root(tmp_path))]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["run_id"] for row in payload} == {"run-a", "run-b"}


def test_cli_get_run_and_verify_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    assert main(["get-run", "--evidence-root", str(root), "--run-id", "run-a"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["gpu_model"] == "NVIDIA A10"

    assert (
        main(["verify-evidence", "--evidence-root", str(root), "--run-id", "run-a"])
        == 0
    )
    verification = json.loads(capsys.readouterr().out)
    assert verification["status"] == "VERIFIED"


def test_cli_compare_runs_flags_incomparable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    code = main(
        [
            "compare-runs",
            "--evidence-root",
            str(root),
            "--baseline",
            "run-a",
            "--candidate",
            "run-b",
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["comparability"] == "INCOMPARABLE"
    assert result["differences"][0]["dimension"] == "gpu_model"


def test_cli_unknown_run_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["get-run", "--evidence-root", str(_root(tmp_path)), "--run-id", "nope"]
    )
    assert code == 2
    assert "unknown run" in capsys.readouterr().err
