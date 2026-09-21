"""Unit tests for the read-only MCP run index over retrieved evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inferdrome.mcp import RunSummary, list_runs


def _bundle(
    root: Path,
    name: str,
    *,
    receipt: dict[str, object] | None,
    model_id: str | None,
    verified_at: str | None = None,
) -> None:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    if receipt is not None:
        payload = dict(receipt)
        if verified_at is not None:
            payload["verified_at"] = verified_at
        (run_dir / "retrieval-receipt.json").write_bytes(
            json.dumps(payload).encode()
        )
    if model_id is not None:
        support = run_dir / "capture" / "support"
        support.mkdir(parents=True)
        (support / "workload-manifest.json").write_bytes(
            json.dumps({"model_id": model_id, "model_revision": "abc"}).encode()
        )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _bundle(
        root,
        "20260821T203940Z-058482df4737-68efd4f4",
        receipt={
            "managed_capability_profile": "managed-vllm-0.26-qwen3-8b-bf16-v1",
            "repository_commit": "058482df47377aaae6303015746f9a8e05d7e0f7",
            "archive_sha256": "sha256:" + "a" * 64,
            "source_archive_sha256": "sha256:" + "b" * 64,
            "semantic_verification": "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED",
        },
        model_id="Qwen/Qwen3-8B",
        verified_at="2026-08-21T20:54:06.590230Z",
    )
    _bundle(
        root,
        "20260818T201045Z-209f0bb9f629-771043fa-FAILED",
        receipt={"repository_commit": "209f0bb9f629"},
        model_id=None,
        verified_at="2026-08-18T20:10:45Z",
    )
    # Only a workload manifest, no receipt: listed, but metadata is incomplete.
    _bundle(
        root, "20260101T000000Z-partial-run", receipt=None, model_id="Qwen/Qwen3-8B"
    )
    # Skipped: in-progress staging, derived debug dir, and an empty directory.
    (root / ".20260819T215001Z-d272ad983d31-7a40b4c6.staging").mkdir()
    (root / "reextract-debug-c08b").mkdir()
    (root / "empty-no-evidence").mkdir()
    return root


def test_list_runs_reads_typed_summaries_and_skips_noise(tmp_path: Path) -> None:
    runs = list_runs(_root(tmp_path))
    ids = {r.run_id for r in runs}
    # staging, reextract, and empty dirs are skipped; three real runs remain.
    assert ids == {
        "20260821T203940Z-058482df4737-68efd4f4",
        "20260818T201045Z-209f0bb9f629-771043fa-FAILED",
        "20260101T000000Z-partial-run",
    }
    assert all(isinstance(r, RunSummary) for r in runs)


def test_list_runs_classifies_status_and_metadata_completeness(tmp_path: Path) -> None:
    by_id = {r.run_id: r for r in list_runs(_root(tmp_path))}

    good = by_id["20260821T203940Z-058482df4737-68efd4f4"]
    assert good.status == "RETRIEVED"
    assert good.metadata_complete is True
    assert good.model_id == "Qwen/Qwen3-8B"
    assert good.managed_capability_profile == "managed-vllm-0.26-qwen3-8b-bf16-v1"
    assert good.semantic_verification == (
        "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
    )

    failed = by_id["20260818T201045Z-209f0bb9f629-771043fa-FAILED"]
    assert failed.status == "FAILED"
    assert failed.metadata_complete is False
    assert failed.model_id is None

    partial = by_id["20260101T000000Z-partial-run"]
    assert partial.metadata_complete is False
    assert partial.archive_sha256 is None


def test_list_runs_orders_most_recent_first(tmp_path: Path) -> None:
    runs = list_runs(_root(tmp_path))
    verified = [r.verified_at for r in runs]
    assert verified == sorted(verified, key=lambda v: v or "", reverse=True)
    assert runs[0].run_id == "20260821T203940Z-058482df4737-68efd4f4"


def test_list_runs_filters_by_model_and_status(tmp_path: Path) -> None:
    root = _root(tmp_path)
    qwen = list_runs(root, model_id="Qwen/Qwen3-8B")
    assert {r.run_id for r in qwen} == {
        "20260821T203940Z-058482df4737-68efd4f4",
        "20260101T000000Z-partial-run",
    }
    failed = list_runs(root, status="FAILED")
    assert [r.run_id for r in failed] == [
        "20260818T201045Z-209f0bb9f629-771043fa-FAILED"
    ]


def test_list_runs_rejects_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list_runs(tmp_path / "does-not-exist")
