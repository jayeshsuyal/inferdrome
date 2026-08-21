"""Qwen3 capability captures bind the exact profile, A10, and sealed bundle."""

from __future__ import annotations

import hashlib
import json
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.qwen3_gpu_capture as capture
import scripts.real_gpu_capture as generic_capture
from inferdrome import __version__
from inferdrome.domain.metrics import Aggregation, MetricId
from inferdrome.domain.states import EvidenceEligibility
from inferdrome.gpu_proof import expected_vllm_source_wheel
from inferdrome.qwen3_campaign import (
    CANONICAL_CAMPAIGN_ID,
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_host_dependencies,
    qwen3_host_dependencies_sha256,
    qwen3_launch_documents,
    qwen3_model_manifest,
    qwen3_model_manifest_sha256,
    qwen3_profile_sha256,
)
from inferdrome.qwen3_tokenizer import (
    expected_qwen3_tokenizer_file_verification,
)

COMMIT = "a" * 40
RUN_ID = "run-" + "b" * 32
BUNDLE_DIGEST = "sha256:" + "c" * 64
SOURCE_ARCHIVE_SHA256 = "sha256:" + "d" * 64


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fake_capture(
    root: Path,
    *,
    gpu_model: str = "NVIDIA A10",
    torch_cuda_device_count: int = 1,
    runtime_snapshot_sha256: str | None = None,
) -> None:
    support = root / "support"
    support.mkdir(parents=True)
    generated = qwen3_launch_documents()
    (support / "campaign-profile.json").write_bytes(
        generated[capture._PROFILE_RELATIVE]
    )
    (support / "host-dependencies.json").write_bytes(
        generated[capture._HOST_DEPENDENCIES_RELATIVE]
    )
    (support / "model-files.json").write_bytes(
        generated[capture._MODEL_MANIFEST_RELATIVE]
    )
    (support / "source.yaml").write_bytes(generated[capture._SOURCE_RELATIVE])
    (support / "workload-manifest.json").write_bytes(
        generated[capture._WORKLOAD_MANIFEST_RELATIVE]
    )
    packages = b"inferdrome==0.1.0.dev0\nvllm==0.26.0\n"
    (support / "python-packages.txt").write_bytes(packages)
    (support / "vllm-version.txt").write_text("0.26.0\n", encoding="utf-8")
    (support / "inferdrome-version.txt").write_text(
        f"{__version__}\n",
        encoding="utf-8",
    )
    wheel_filename, wheel_sha256 = expected_vllm_source_wheel("x86_64")
    tokenizers_pin = next(
        item
        for item in qwen3_host_dependencies()["distributions"]
        if item["architecture"] == "x86_64"
    )
    model_manifest = qwen3_model_manifest()
    host_snapshot = {
        "file_count": model_manifest["file_count"],
        "hash_policy": "regular-files-excluding-dot-cache-v1",
        "kind": "model",
        "revision": QWEN3_8B_REVISION,
        "root": "/data/Qwen3-8B-snapshot",
        "sha256": qwen3_expected_snapshot_sha256(),
        "total_bytes": model_manifest["total_bytes"],
    }
    _write_json(
        support / "host-preparation.json",
        {
            "architecture": "x86_64",
            "campaign_id": CANONICAL_CAMPAIGN_ID,
            "host_dependencies_sha256": qwen3_host_dependencies_sha256(),
            "model_directory": "/data/Qwen3-8B-snapshot",
            "model_id": QWEN3_8B_MODEL_ID,
            "model_manifest_sha256": qwen3_model_manifest_sha256(),
            "model_revision": QWEN3_8B_REVISION,
            "model_snapshot": host_snapshot,
            "prepared_at": "2026-08-20T20:00:00Z",
            "profile_id": QWEN3_8B_PROFILE_ID,
            "profile_sha256": qwen3_profile_sha256(),
            "python_packages_sha256": "sha256:" + hashlib.sha256(packages).hexdigest(),
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-host-preparation.v1",
            "source_provenance": {
                "repository_commit": COMMIT,
                "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
                "transport": "git-archive-exact-head-tree-v1",
            },
            "tokenizer_files": (
                expected_qwen3_tokenizer_file_verification().model_dump(mode="json")
            ),
            "tokenizer_revision": QWEN3_8B_REVISION,
            "tokenizers_wheel_filename": tokenizers_pin["filename"],
            "tokenizers_wheel_sha256": tokenizers_pin["sha256"],
            "vllm_wheel_filename": wheel_filename,
            "vllm_wheel_sha256": wheel_sha256,
        },
    )
    bundle = root / "runs" / RUN_ID / "bundle"
    _write_json(
        bundle / "native" / "invocation.json",
        {
            "campaign_profile": {
                "profile_id": QWEN3_8B_PROFILE_ID,
                "profile_sha256": qwen3_profile_sha256(),
            },
            "local_gpu_proof": {
                "gpus": [{"index": 0, "model": gpu_model}],
                "model_snapshot": {
                    **host_snapshot,
                    "sha256": (
                        runtime_snapshot_sha256 or qwen3_expected_snapshot_sha256()
                    ),
                },
                "selected_gpu_indices": [0],
                "tokenizer_snapshot": {
                    **host_snapshot,
                    "kind": "tokenizer",
                    "sha256": (
                        runtime_snapshot_sha256 or qwen3_expected_snapshot_sha256()
                    ),
                },
                "torch_cuda_device_count": torch_cuda_device_count,
            },
        },
    )
    _write_json(
        bundle / "experiment.resolved.json",
        {"traffic": {"concurrency": 1}},
    )


def _fake_recalculation(
    bundle: Path,
    *,
    successful: int = 96,
    failed: int = 0,
    ttft_samples: int = 96,
) -> SimpleNamespace:
    assert bundle.name == "bundle"
    assert bundle.parent.name == RUN_ID
    report = SimpleNamespace(
        bundle_digest=BUNDLE_DIGEST,
        descriptor=SimpleNamespace(
            evidence_eligibility=EvidenceEligibility.CUSTOMER_ELIGIBLE
        ),
        run_id=RUN_ID,
    )
    measurements = [
        SimpleNamespace(
            aggregation=Aggregation.COUNT,
            metric=MetricId.MEASURED_REQUEST_COUNT,
            sample_count=96,
            value=96,
        ),
        SimpleNamespace(
            aggregation=Aggregation.COUNT,
            metric=MetricId.SUCCESSFUL_REQUEST_COUNT,
            sample_count=successful,
            value=successful,
        ),
        SimpleNamespace(
            aggregation=Aggregation.COUNT,
            metric=MetricId.FAILED_REQUEST_COUNT,
            sample_count=failed,
            value=failed,
        ),
        *[
            SimpleNamespace(
                aggregation=aggregation,
                metric=MetricId.TTFT_NS,
                sample_count=ttft_samples,
                value=1,
            )
            for aggregation in (
                Aggregation.MEAN,
                Aggregation.P50,
                Aggregation.P95,
                Aggregation.P99,
            )
        ],
    ]
    return SimpleNamespace(
        reduction=SimpleNamespace(
            measurements=SimpleNamespace(measurements=measurements)
        ),
        verification=report,
    )


def test_qwen3_capture_writes_and_reverifies_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)

    manifest_path = capture.write_capture_manifest(root, COMMIT)
    verification = capture.verify_capture(
        root,
        expected_repository_commit=COMMIT,
    )

    assert verification["valid"] is True
    assert verification["run"] == {
        "bundle_digest": BUNDLE_DIGEST,
        "bundle_path": f"runs/{RUN_ID}/bundle",
        "evidence_eligibility": "CUSTOMER_ELIGIBLE",
        "failed_requests": 0,
        "run_id": RUN_ID,
        "spike_outcome": "SPIKE_SUCCEEDED",
        "successful_requests": 96,
        "torch_cuda_device_count": 1,
        "ttft_samples": 96,
        "workspace_path": f"runs/{RUN_ID}",
    }
    assert verification["source_archive_sha256"] == SOURCE_ARCHIVE_SHA256
    assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o222 == 0

    archive = tmp_path / "capture.tar.gz"
    with tarfile.open(archive, "w:gz") as retained:
        retained.add(root, arcname="capture", recursive=True)
    archive_verification = capture.verify_capture_archive(
        archive,
        expected_archive_sha256=generic_capture.archive_sha256(archive),
        expected_repository_commit=COMMIT,
    )
    assert archive_verification["verification"] == verification


def test_qwen3_capture_rejects_non_a10_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root, gpu_model="NVIDIA A100-SXM4-40GB")
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)

    with pytest.raises(capture.Qwen3CaptureError, match="one NVIDIA A10"):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_one_failed_measured_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(
        capture,
        "recalculate_bundle",
        lambda bundle: _fake_recalculation(bundle, successful=95, failed=1),
    )

    with pytest.raises(capture.Qwen3CaptureError, match="96 of 96"):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_missing_ttft_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(
        capture,
        "recalculate_bundle",
        lambda bundle: _fake_recalculation(bundle, ttft_samples=95),
    )

    with pytest.raises(capture.Qwen3CaptureError, match="96 observed TTFT"):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_multi_device_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root, torch_cuda_device_count=2)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)

    with pytest.raises(capture.Qwen3CaptureError, match="one NVIDIA A10"):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_spliced_model_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root, runtime_snapshot_sha256="sha256:" + "e" * 64)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)

    with pytest.raises(capture.Qwen3CaptureError, match="snapshots disagree"):
        capture.write_capture_manifest(root, COMMIT)


@pytest.mark.parametrize(
    ("filename", "support_key"),
    (
        ("host-dependencies.json", "host_dependencies"),
        ("model-files.json", "model_files"),
    ),
)
def test_qwen3_capture_rejects_generated_support_byte_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    support_key: str,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)
    path = root / "support" / filename
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(
        capture.Qwen3CaptureError,
        match=f"support bytes drifted: {support_key}",
    ):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_tokenizers_wheel_pin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)
    receipt_path = root / "support" / "host-preparation.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["tokenizers_wheel_sha256"] = "sha256:" + "e" * 64
    _write_json(receipt_path, receipt)

    with pytest.raises(
        capture.Qwen3CaptureError,
        match="host-preparation field drifted: tokenizers_wheel_sha256",
    ):
        capture.write_capture_manifest(root, COMMIT)


def test_qwen3_capture_rejects_support_mutation_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)
    capture.write_capture_manifest(root, COMMIT)
    profile = root / "support" / "campaign-profile.json"
    profile.chmod(0o644)
    profile.write_bytes(profile.read_bytes() + b" ")

    with pytest.raises(capture.Qwen3CaptureError, match="support digest drifted"):
        capture.verify_capture(root, expected_repository_commit=COMMIT)


def test_qwen3_capture_rejects_manifest_run_digest_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    _fake_capture(root)
    monkeypatch.setattr(capture, "recalculate_bundle", _fake_recalculation)
    manifest_path = capture.write_capture_manifest(root, COMMIT)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run"]["bundle_digest"] = "sha256:" + "d" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(capture.Qwen3CaptureError, match="run binding drifted"):
        capture.verify_capture(root, expected_repository_commit=COMMIT)
