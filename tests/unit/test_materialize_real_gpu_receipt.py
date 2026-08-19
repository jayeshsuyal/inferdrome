"""Recovered real-GPU receipts remain sealed and explicitly single-run-only."""

import json
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.materialize_real_gpu_receipt as recovery
import scripts.real_gpu_capture as capture
from inferdrome.domain.states import EvidenceEligibility

COMMIT = "a" * 40
RUN_ID = "run-" + "b" * 32
BUNDLE_DIGEST = "sha256:" + "c" * 64


def _retained_failure_archive(
    tmp_path: Path,
    *,
    extra_run_id: str | None = None,
) -> tuple[Path, Path]:
    source = tmp_path / "source"
    capture_root = source / "capture"
    capture.write_failure_receipt(
        capture_root,
        COMMIT,
        failed_step="single-proof",
        exit_code=1,
    )
    bundle = capture_root / "single" / "real-gpu-test" / "runs" / RUN_ID / "bundle"
    bundle.mkdir(parents=True)
    descriptor = bundle / "bundle.json"
    descriptor.write_text("{}\n", encoding="utf-8")
    descriptor.chmod(0o400)
    bundle.chmod(0o500)
    if extra_run_id is not None:
        extra_bundle = (
            capture_root
            / "single"
            / "real-gpu-test"
            / "runs"
            / extra_run_id
            / "bundle"
        )
        extra_bundle.mkdir(parents=True)
        extra_descriptor = extra_bundle / "bundle.json"
        extra_descriptor.write_text("{}\n", encoding="utf-8")
        extra_descriptor.chmod(0o400)
        extra_bundle.chmod(0o500)
    archive = tmp_path / "capture.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        retained.add(capture_root, arcname="capture", recursive=True)
    return archive, source


def _report(eligibility: EvidenceEligibility) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_count=16,
        bundle_digest=BUNDLE_DIGEST,
        descriptor=SimpleNamespace(evidence_eligibility=eligibility),
        run_id=RUN_ID,
        total_bytes=330_168,
    )


def test_materialize_preserves_seal_and_publishes_dashboard_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, source = _retained_failure_archive(tmp_path)
    destination = tmp_path / "recovered"

    def verify(path: Path, *, expected_bundle_digest: str) -> SimpleNamespace:
        assert expected_bundle_digest == BUNDLE_DIGEST
        assert stat.S_IMODE(path.stat().st_mode) == 0o500
        assert stat.S_IMODE((path / "bundle.json").stat().st_mode) == 0o400
        return _report(EvidenceEligibility.CUSTOMER_ELIGIBLE)

    monkeypatch.setattr(recovery, "verify_bundle", verify)
    try:
        result = recovery.materialize(
            archive,
            destination,
            expected_archive_sha256=capture.archive_sha256(archive),
            expected_bundle_digest=BUNDLE_DIGEST,
            expected_repository_commit=COMMIT,
            expected_run_id=RUN_ID,
        )

        receipt = json.loads(
            (destination / "recovered-real-gpu-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        assert receipt["capture_proof_status"] == "INCOMPLETE_NOT_EVIDENCE"
        assert receipt["recovered_scope"] == "SINGLE_BUNDLE_ONLY"
        assert receipt["evidence_eligibility"] == "CUSTOMER_ELIGIBLE"
        assert result["runs_root"].endswith("/single/real-gpu-test/runs")
        assert result["dashboard_route"] == f"/runs/{RUN_ID}"
    finally:
        recovery._make_tree_writable(source)
        recovery._make_tree_writable(destination)


def test_materialize_rejects_non_customer_evidence_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, source = _retained_failure_archive(tmp_path)
    destination = tmp_path / "recovered"
    monkeypatch.setattr(
        recovery,
        "verify_bundle",
        lambda *_args, **_kwargs: _report(EvidenceEligibility.INELIGIBLE),
    )

    try:
        with pytest.raises(recovery.MaterializeError, match="customer-eligible"):
            recovery.materialize(
                archive,
                destination,
                expected_archive_sha256=capture.archive_sha256(archive),
                expected_bundle_digest=BUNDLE_DIGEST,
                expected_repository_commit=COMMIT,
                expected_run_id=RUN_ID,
            )
        assert not destination.exists()
        assert not list(tmp_path.glob(".recovered.staging-*"))
    finally:
        recovery._make_tree_writable(source)


def test_materialize_fails_closed_if_destination_appears_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, source = _retained_failure_archive(tmp_path)
    destination = tmp_path / "recovered"

    def verify(*_args: object, **_kwargs: object) -> SimpleNamespace:
        destination.mkdir()
        return _report(EvidenceEligibility.CUSTOMER_ELIGIBLE)

    monkeypatch.setattr(recovery, "verify_bundle", verify)
    try:
        with pytest.raises(recovery.MaterializeError, match="could not be published"):
            recovery.materialize(
                archive,
                destination,
                expected_archive_sha256=capture.archive_sha256(archive),
                expected_bundle_digest=BUNDLE_DIGEST,
                expected_repository_commit=COMMIT,
                expected_run_id=RUN_ID,
            )

        assert destination.is_dir()
        assert not list(destination.iterdir())
        assert not list(tmp_path.glob(".recovered.staging-*"))
    finally:
        recovery._make_tree_writable(source)


def test_materialize_rejects_an_additional_run_bundle(
    tmp_path: Path,
) -> None:
    archive, source = _retained_failure_archive(
        tmp_path,
        extra_run_id="run-" + "d" * 32,
    )
    destination = tmp_path / "recovered"

    try:
        with pytest.raises(recovery.MaterializeError, match="exactly one run bundle"):
            recovery.materialize(
                archive,
                destination,
                expected_archive_sha256=capture.archive_sha256(archive),
                expected_bundle_digest=BUNDLE_DIGEST,
                expected_repository_commit=COMMIT,
                expected_run_id=RUN_ID,
            )

        assert not destination.exists()
        assert not list(tmp_path.glob(".recovered.staging-*"))
    finally:
        recovery._make_tree_writable(source)
