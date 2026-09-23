"""Unit tests for the read-only MCP run index over retrieved evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from inferdrome.mcp import (
    RunSummary,
    compare_runs,
    get_run,
    list_runs,
    verify_evidence,
)


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


def _detail_bundle(root: Path, name: str) -> None:
    run_dir = root / name
    support = run_dir / "capture" / "support"
    support.mkdir(parents=True)
    (run_dir / "retrieval-receipt.json").write_bytes(
        json.dumps({"managed_capability_profile": "p"}).encode()
    )
    (support / "workload-manifest.json").write_bytes(
        json.dumps(
            {"model_id": "Qwen/Qwen3-8B", "model_revision": "abc", "line_count": 96}
        ).encode()
    )
    (run_dir / "capture.tar.gz.metadata.json").write_bytes(
        json.dumps({"size_bytes": 321010}).encode()
    )
    bundle = run_dir / "capture" / "runs" / "run-xyz" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "environment.json").write_bytes(
        json.dumps(
            {
                "fields": [
                    {"name": "gpu.model", "value": "NVIDIA A10"},
                    {"name": "gpu.count", "value": 1},
                    {"name": "driver.version", "value": "580.105.08"},
                ]
            }
        ).encode()
    )


def test_get_run_returns_typed_detail_with_gpu_and_workload(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _detail_bundle(root, "run-a")
    detail = get_run(root, "run-a")
    assert detail.model_id == "Qwen/Qwen3-8B"
    assert detail.gpu_model == "NVIDIA A10"
    assert detail.gpu_count == 1
    assert detail.driver_version == "580.105.08"
    assert detail.workload_line_count == 96
    assert detail.archive_size_bytes == 321010


def test_get_run_rejects_unknown_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _detail_bundle(root, "run-a")
    with pytest.raises(KeyError):
        get_run(root, "missing")
    with pytest.raises(KeyError):
        get_run(root, "../secret")


def _archive_bundle(
    root: Path,
    name: str,
    *,
    content: bytes | None,
    metadata_digest: str | None,
    sidecar_digest: str | None = None,
) -> None:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "retrieval-receipt.json").write_bytes(json.dumps({}).encode())
    if content is not None:
        (run_dir / "capture.tar.gz").write_bytes(content)
    if metadata_digest is not None:
        (run_dir / "capture.tar.gz.metadata.json").write_bytes(
            json.dumps({"archive_sha256": metadata_digest}).encode()
        )
    if sidecar_digest is not None:
        (run_dir / "capture.tar.gz.sha256").write_text(
            f"{sidecar_digest}  capture.tar.gz\n"
        )


def test_verify_evidence_recomputes_and_confirms_a_match(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    content = b"pinned archive bytes"
    digest = hashlib.sha256(content).hexdigest()
    _archive_bundle(
        root,
        "run-ok",
        content=content,
        metadata_digest="sha256:" + digest,
        sidecar_digest=digest,
    )
    result = verify_evidence(root, "run-ok")
    assert result.status == "VERIFIED"
    assert result.recomputed_sha256 == digest
    assert result.recorded_sha256 == digest
    assert result.archive_size_bytes == len(content)


def test_verify_evidence_flags_a_tampered_archive(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _archive_bundle(
        root, "run-bad", content=b"tampered", metadata_digest="sha256:" + "0" * 64
    )
    result = verify_evidence(root, "run-bad")
    assert result.status == "DIGEST_MISMATCH"
    assert result.recomputed_sha256 == hashlib.sha256(b"tampered").hexdigest()
    assert result.recorded_sha256 == "0" * 64


def test_verify_evidence_flags_disagreeing_records(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _archive_bundle(
        root,
        "run-split",
        content=b"x",
        metadata_digest="sha256:" + "a" * 64,
        sidecar_digest="b" * 64,
    )
    assert verify_evidence(root, "run-split").status == "RECORDED_DIGESTS_DISAGREE"


def test_verify_evidence_reports_absent_and_unrecorded(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _archive_bundle(
        root, "run-noarchive", content=None, metadata_digest="sha256:" + "a" * 64
    )
    assert verify_evidence(root, "run-noarchive").status == "ARCHIVE_ABSENT"
    _archive_bundle(root, "run-nodigest", content=b"y", metadata_digest=None)
    assert verify_evidence(root, "run-nodigest").status == "NO_RECORDED_DIGEST"


def _compare_bundle(
    root: Path, name: str, *, gpu_model: str, ttft_mean: float
) -> None:
    run_dir = root / name
    support = run_dir / "capture" / "support"
    support.mkdir(parents=True)
    (run_dir / "retrieval-receipt.json").write_bytes(json.dumps({}).encode())
    (support / "workload-manifest.json").write_bytes(
        json.dumps(
            {"model_id": "Qwen/Qwen3-8B", "model_revision": "abc", "line_count": 96}
        ).encode()
    )
    bundle = run_dir / "capture" / "runs" / f"run-{name}" / "bundle"
    (bundle / "derived").mkdir(parents=True)
    (bundle / "environment.json").write_bytes(
        json.dumps(
            {"fields": [{"name": "gpu.model", "value": gpu_model}]}
        ).encode()
    )
    (bundle / "derived" / "measurements.json").write_bytes(
        json.dumps(
            {
                "measurements": [
                    {
                        "metric": "ttft_ns",
                        "aggregation": "mean",
                        "unit": "ns",
                        "value": ttft_mean,
                    },
                    {
                        "metric": "error_rate",
                        "aggregation": "ratio",
                        "unit": "ratio",
                        "value": "0.000000",
                    },
                ]
            }
        ).encode()
    )


def test_compare_runs_reports_comparable_with_metric_deltas(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _compare_bundle(root, "base", gpu_model="NVIDIA A10", ttft_mean=100.0)
    _compare_bundle(root, "cand", gpu_model="NVIDIA A10", ttft_mean=150.0)
    result = compare_runs(root, "base", "cand")
    assert result.comparability == "COMPARABLE"
    assert result.differences == ()
    ttft = next(m for m in result.metric_deltas if m.metric == "ttft_ns")
    assert ttft.baseline_value == 100.0
    assert ttft.candidate_value == 150.0
    assert ttft.absolute_delta == 50.0
    assert ttft.percent_change == 50.0
    zero = next(m for m in result.metric_deltas if m.metric == "error_rate")
    assert zero.percent_change is None


def test_compare_runs_flags_incomparable_gpu_but_still_returns_deltas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _compare_bundle(root, "a10", gpu_model="NVIDIA A10", ttft_mean=100.0)
    _compare_bundle(root, "a100", gpu_model="NVIDIA A100-SXM4-40GB", ttft_mean=40.0)
    result = compare_runs(root, "a10", "a100")
    assert result.comparability == "INCOMPARABLE"
    assert [d.dimension for d in result.differences] == ["gpu_model"]
    assert result.differences[0].baseline == "NVIDIA A10"
    assert result.differences[0].candidate == "NVIDIA A100-SXM4-40GB"
    assert any(m.metric == "ttft_ns" for m in result.metric_deltas)


def test_compare_runs_rejects_an_unknown_run(tmp_path: Path) -> None:
    root = tmp_path / "gpu-proof-retrieved"
    root.mkdir()
    _compare_bundle(root, "base", gpu_model="NVIDIA A10", ttft_mean=100.0)
    with pytest.raises(KeyError):
        compare_runs(root, "base", "missing")
