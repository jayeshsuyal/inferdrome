"""Prospective-only controller sequencing and transport boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.capture_real_gpu_over_ssh as remote
import scripts.prospective_handoff as handoff

COMMIT = "a" * 40
SOURCE_BYTES = b"exact source archive"
SOURCE_DIGEST = "sha256:" + hashlib.sha256(SOURCE_BYTES).hexdigest()
HOST_KEY_BYTES = b"[prepared.example.test]:22 ssh-ed25519 AAAA\n"
HOST_KEY_DIGEST = hashlib.sha256(HOST_KEY_BYTES).hexdigest()


def _snapshot() -> handoff.HandoffSnapshot:
    files: list[handoff.HandoffFile] = [
        handoff.HandoffFile(".complete", b"complete\n", "sha256:" + "1" * 64),
        handoff.HandoffFile("handoff-manifest.json", b"{}", "sha256:" + "2" * 64),
        handoff.HandoffFile(
            "sources/real-gpu/workload.jsonl", b"{}\n", "sha256:" + "3" * 64
        ),
    ]
    cases: list[handoff.HandoffCase] = []
    for index, case_id in enumerate(handoff.CASE_IDS, start=4):
        contract = handoff.HandoffFile(
            f"contracts/{case_id}.frozen.json",
            b"{}",
            "sha256:" + str(index) * 64,
        )
        confirmation = handoff.HandoffFile(
            f"confirmations/{case_id}.confirmation.json",
            b"{}",
            "sha256:" + str(index + 3) * 64,
        )
        source = handoff.HandoffFile(
            f"sources/{case_id}.yaml",
            b"experiment: {}\n",
            "sha256:" + str(index + 6) * 64,
        )
        cases.append(
            handoff.HandoffCase(
                case_id,
                contract,
                confirmation,
                source,
                "sha256:" + str(index) * 64,
            )
        )
        files.extend((contract, confirmation, source))
    return handoff.HandoffSnapshot(
        root=Path("/handoff"),
        manifest=files[1],
        complete=files[0],
        workload=files[2],
        cases=tuple(cases),
        files=tuple(files),
    )


def _prospective_args(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "destination": "operator@prepared.example.test",
        "expected_handoff_manifest_sha256": "sha256:" + "2" * 64,
        "expected_workload_sha256": "sha256:" + "3" * 64,
        "gpu_index": 0,
        "host_key_file": None,
        "host_key_sha256": HOST_KEY_DIGEST,
        "identity_file": str(tmp_path / "id_ed25519"),
        "lambda_billing_started_at": None,
        "lambda_guard_state_root": str(tmp_path / "guards"),
        "lambda_hourly_rate_usd": None,
        "lambda_instance_id": None,
        "lambda_instance_type_name": None,
        "managed_capability_profile": None,
        "max_cost_usd": None,
        "output_root": str(tmp_path / "output"),
        "port": 22,
        "prospective": True,
        "prospective_handoff_root": str(tmp_path / "handoff"),
        "qwen3_gpu_tier": None,
        "remote_state_root": "/prepared/.inferdrome-gpu",
        "remote_timeout_seconds": 300,
        "startup_timeout_seconds": 900.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mutated_p1_is_rejected_before_source_or_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "handoff"
    root.mkdir()
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            handoff.ProspectiveHandoffError("mutated P1")
        ),
    )
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda *_args, **_kwargs: pytest.fail("source must follow P1 validation"),
    )
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("SSH/provider work is out of order"),
    )
    args = _prospective_args(tmp_path)
    (tmp_path / "id_ed25519").write_bytes(b"key")

    with pytest.raises(handoff.ProspectiveHandoffError, match="mutated"):
        remote._capture_prospective_with_handoff(args, COMMIT, tmp_path / "id_ed25519")


@pytest.mark.parametrize("missing", ["identity_file", "host_key_sha256"])
def test_prospective_mode_requires_identity_and_host_pin(
    tmp_path: Path,
    missing: str,
) -> None:
    values = {missing: None}
    with pytest.raises(remote.RemoteCaptureError, match="explicit"):
        remote._validate_capture_mode(_prospective_args(tmp_path, **values))


@pytest.mark.parametrize("missing", ["lambda_instance_id", "lambda_instance_type_name"])
def test_guarded_prospective_mode_requires_lambda_target_identity(
    tmp_path: Path,
    missing: str,
) -> None:
    values: dict[str, object] = {
        "lambda_billing_started_at": datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": "1.00",
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a10",
        "max_cost_usd": "1.00",
    }
    values[missing] = None
    with pytest.raises(remote.RemoteCaptureError, match="guarded prospective"):
        remote._validate_capture_mode(_prospective_args(tmp_path, **values))


def test_prospective_dry_run_is_inert_and_reports_both_archive_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot()
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        remote,
        "_validate_prospective_snapshot",
        lambda *_args, **_kwargs: None,
    )

    def source_archive(
        path: Path, _commit: str, **_kwargs: object
    ) -> tuple[str, int]:
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    monkeypatch.setattr(remote, "_create_source_archive", source_archive)
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not invoke a command"),
    )
    args = _prospective_args(tmp_path)

    remote._dry_run(args, COMMIT, Path(args.identity_file))
    output = json.loads(capsys.readouterr().out)
    assert output["source_archive_sha256"] == SOURCE_DIGEST
    assert output["prospective_handoff"]["archive_sha256"].startswith("sha256:")
    assert output["prospective_handoff"]["manifest_sha256"] == snapshot.manifest_sha256


def test_pinned_host_material_is_prepared_before_first_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    handoff_archive = handoff.create_handoff_archive(
        snapshot, tmp_path / "handoff.tar.gz"
    )
    source_archive = tmp_path / "repo.tar"
    source_archive.write_bytes(SOURCE_BYTES)
    args = _prospective_args(tmp_path)
    identity = tmp_path / "id_ed25519"
    identity.write_bytes(b"key")
    events: list[str] = []

    def prepare(**kwargs: object) -> str:
        events.append("host-pin")
        Path(str(kwargs["known_hosts"])).write_bytes(HOST_KEY_BYTES)
        return "sha256:" + HOST_KEY_DIGEST

    monkeypatch.setattr(remote, "_prepare_pinned_known_hosts", prepare)
    monkeypatch.setattr(remote.shutil, "which", lambda _name: "/bin/tool")

    def run(arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = [str(item) for item in arguments]  # type: ignore[arg-type]
        if argv[0] == "scp":
            events.append(
                "upload-source"
                if any(item.endswith("repo.tar") for item in argv)
                else "upload-handoff"
            )
        else:
            events.append("preflight" if "nvidia-smi" in argv[-1] else "remote-run")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(remote, "_run", run)

    def bounded(_arguments: object, destination: Path, **_kwargs: object) -> None:
        events.append("metadata")
        destination.write_text(
            json.dumps(
                {
                    "archive_name": "prospective-session.tar.gz",
                    "archive_sha256": "sha256:" + "a" * 64,
                    "schema_version": remote._PROSPECTIVE_TRANSFER_METADATA_SCHEMA,
                    "size_bytes": 7,
                }
            )
        )

    def exact(_arguments: object, destination: Path, **_kwargs: object) -> None:
        events.append("archive")
        destination.write_bytes(b"archive")

    monkeypatch.setattr(remote, "_download_bounded_remote_file", bounded)
    monkeypatch.setattr(remote, "_download_exact_remote_file", exact)
    monkeypatch.setattr(
        remote,
        "_extract_prospective_archive",
        lambda *_a, **_k: pytest.fail("raw transport must not extract"),
    )
    monkeypatch.setattr(
        remote,
        "_prospective_session_identities",
        lambda *_a, **_k: pytest.fail("raw transport must not parse identities"),
    )
    selected = remote._capture_prospective_over_ssh(
        args,
        COMMIT,
        identity,
        source_archive,
        SOURCE_DIGEST,
        handoff_archive,
    )
    assert selected[0].is_dir()
    assert selected[1] == len(SOURCE_BYTES)
    assert events == [
        "host-pin",
        "preflight",
        "upload-source",
        "upload-handoff",
        "remote-run",
        "metadata",
        "archive",
    ]
    assert "StrictHostKeyChecking=yes" in remote._ssh_options(
        identity=identity,
        known_hosts=tmp_path / "known",
        port=22,
        pinned=True,
    )


@pytest.mark.parametrize("mutated", ["source", "handoff"])
def test_remote_digest_gates_precede_wrapper_and_local_mismatch_stops_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated: str,
) -> None:
    snapshot = _snapshot()
    archive = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")
    source = tmp_path / "repo.tar"
    source.write_bytes(SOURCE_BYTES)
    if mutated == "source":
        source.write_bytes(b"changed")
    else:
        archive.archive_path.write_bytes(b"changed")  # type: ignore[union-attr]
    args = _prospective_args(tmp_path)
    (tmp_path / "id_ed25519").write_bytes(b"key")
    monkeypatch.setattr(remote.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("archive mismatch must precede SSH"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="identity changed"):
        remote._capture_prospective_over_ssh(
            args,
            COMMIT,
            Path(args.identity_file),
            source,
            SOURCE_DIGEST,
            archive,
        )
    script = remote._remote_prospective_capture_script(
        "/tmp/r",
        COMMIT,
        SOURCE_DIGEST,
        snapshot,
        handoff_archive_sha256="sha256:" + "b" * 64,
        handoff_archive_size=100,
        workload_sha256="sha256:" + "c" * 64,
        remote_state_root="/prepared/state",
        gpu_index=0,
        startup_timeout_seconds=900,
        remote_timeout_seconds=300,
    )
    assert script.index("sha256sum /tmp/r/repo.tar") < script.index(
        "prospective_real_gpu_capture.py"
    )
    assert script.index("sha256sum /tmp/r/handoff.tar.gz") < script.index(
        "prospective_real_gpu_capture.py"
    )
    assert script.count("--case ") == 3
    assert script.count("--expected-contract-digest ") == 3
    assert "acceptance_verdict" not in script


@pytest.mark.parametrize("kind", ["malformed", "oversized", "truncated", "growing"])
def test_prospective_transfer_rejects_bounded_stream_failures(
    tmp_path: Path,
    kind: str,
) -> None:
    destination = tmp_path / "archive"
    if kind in {"malformed", "oversized"}:
        metadata = tmp_path / "metadata.json"
        if kind == "malformed":
            metadata.write_bytes(b"{")
        else:
            metadata.write_text(
                json.dumps(
                    {
                        "archive_name": "prospective-session.tar.gz",
                        "archive_sha256": "sha256:" + "a" * 64,
                        "schema_version": remote._PROSPECTIVE_TRANSFER_METADATA_SCHEMA,
                        "size_bytes": remote._PROSPECTIVE_MAX_SESSION_ARCHIVE_BYTES + 1,
                    }
                )
            )
        with pytest.raises(remote.RemoteCaptureError):
            remote._read_prospective_transfer_metadata(metadata)
        return
    payload = b"x" if kind == "truncated" else b"x" * 11
    code = "import sys; sys.stdout.buffer.write(" + repr(payload) + ")"
    with pytest.raises(remote.RemoteCaptureError):
        remote._download_remote_file(
            [sys.executable, "-c", code],
            destination,
            minimum_size=2,
            maximum_size=10,
            timeout=5,
        )
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["symlink", "traversal", "collision"])
def test_prospective_extraction_rejects_unsafe_members(
    tmp_path: Path,
    kind: str,
) -> None:
    archive_path = tmp_path / "session.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        if kind == "symlink":
            link = tarfile.TarInfo("session/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        elif kind == "traversal":
            archive.addfile(tarfile.TarInfo("../escape"), io.BytesIO(b"x"))
        else:
            first = tarfile.TarInfo("session")
            first.size = 1
            archive.addfile(first, io.BytesIO(b"x"))
            second = tarfile.TarInfo("session/nested")
            second.size = 1
            archive.addfile(second, io.BytesIO(b"x"))
    with pytest.raises(remote.RemoteCaptureError, match="unsafe"):
        remote._extract_prospective_archive(archive_path, tmp_path / "extracted")


def test_guarded_prospective_capture_terminates_before_offline_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _prospective_args(
        tmp_path,
        lambda_billing_started_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        lambda_hourly_rate_usd="1.00",
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        max_cost_usd="1.00",
    )
    snapshot = _snapshot()
    archive = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")
    events: list[str] = []
    watchdog = SimpleNamespace(
        cost_window=SimpleNamespace(deadline=None),
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(
        remote.prospective_handoff, "snapshot_handoff", lambda *_a, **_k: snapshot
    )
    monkeypatch.setattr(
        remote.prospective_handoff, "create_handoff_archive", lambda *_a, **_k: archive
    )
    monkeypatch.setattr(
        remote, "_validate_prospective_snapshot", lambda *_a, **_k: None
    )

    def source_archive(
        path: Path, _commit: str, **_kwargs: object
    ) -> tuple[str, int]:
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        source_archive,
    )

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    def capture(*_args: object, **_kwargs: object) -> tuple[Path, int]:
        events.append("retrieve")
        captured = tmp_path / "captured"
        captured.mkdir()
        (captured / "ssh-known-hosts").write_bytes(HOST_KEY_BYTES)
        archive_bytes = b"archive"
        (captured / "prospective-session.tar.gz").write_bytes(archive_bytes)
        (captured / "prospective-session.tar.gz.metadata.json").write_text(
            json.dumps(
                {
                    "archive_name": "prospective-session.tar.gz",
                    "archive_sha256": (
                        "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
                    ),
                    "schema_version": remote._PROSPECTIVE_TRANSFER_METADATA_SCHEMA,
                    "size_bytes": 7,
                }
            )
        )
        return captured, len(SOURCE_BYTES)

    def terminate(_handle: object) -> object:
        events.append("terminate")
        return SimpleNamespace(
            final_status="absent", public_record=lambda: {"status": "absent"}
        )

    def extract(_archive: Path, _destination: Path) -> Path:
        assert events[-1] == "terminate"
        events.append("extract")
        return tmp_path / "session"

    def identities(_root: Path) -> tuple[str, list[dict[str, str]]]:
        assert events[-1] == "extract"
        events.append("identity")
        return (
            "prospective-real-gpu-session",
            [
                {
                    "case_id": case_id,
                    "run_id": f"run-{index}",
                    "bundle_digest": f"sha256:{index:064x}",
                    "request_plan_digest": f"sha256:{index + 10:064x}",
                }
                for index, case_id in enumerate(handoff.CASE_IDS, start=1)
            ],
        )

    def offline_verify(*_args: object, **_kwargs: object) -> dict[str, bool]:
        assert events[-1] == "identity"
        events.append("offline-verify")
        return {"valid": True}

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)
    monkeypatch.setattr(remote, "_capture_prospective_over_ssh", capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard, "terminate_guarded_instance", terminate
    )
    monkeypatch.setattr(remote, "_extract_prospective_archive", extract)
    monkeypatch.setattr(remote, "_prospective_session_identities", identities)
    monkeypatch.setattr(remote, "_verify_prospective_session", offline_verify)

    assert remote._capture_prospective_with_handoff(args, COMMIT, tmp_path / "id") == (
        tmp_path / "captured"
    )
    assert events == [
        "arm",
        "retrieve",
        "terminate",
        "extract",
        "identity",
        "offline-verify",
    ]


def test_post_termination_archive_mutation_is_rejected_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    capture_path = tmp_path / "captured"
    capture_path.mkdir()
    archive = capture_path / "prospective-session.tar.gz"
    archive.write_bytes(b"mutated")
    original = b"original"
    (capture_path / "prospective-session.tar.gz.metadata.json").write_text(
        json.dumps(
            {
                "archive_name": "prospective-session.tar.gz",
                "archive_sha256": "sha256:" + hashlib.sha256(original).hexdigest(),
                "schema_version": remote._PROSPECTIVE_TRANSFER_METADATA_SCHEMA,
                "size_bytes": len(original),
            }
        )
    )
    monkeypatch.setattr(
        remote,
        "_extract_prospective_archive",
        lambda *_args, **_kwargs: pytest.fail("mutated archive must not extract"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="changed before extraction"):
        remote._materialize_prospective_capture(
            capture_path,
            snapshot,
            commit=COMMIT,
            source_archive_sha256=SOURCE_DIGEST,
            source_archive_size_bytes=len(SOURCE_BYTES),
        )


def test_prospective_remote_failure_finalizes_guard_without_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _prospective_args(
        tmp_path,
        lambda_billing_started_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        lambda_hourly_rate_usd="1.00",
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        max_cost_usd="1.00",
    )
    snapshot = _snapshot()
    archive = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")
    events: list[str] = []
    watchdog = SimpleNamespace(
        cost_window=SimpleNamespace(deadline=None),
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "create_handoff_archive",
        lambda *_args, **_kwargs: archive,
    )
    monkeypatch.setattr(
        remote, "_validate_prospective_snapshot", lambda *_a, **_k: None
    )

    def source_archive(
        path: Path, _commit: str, **_kwargs: object
    ) -> tuple[str, int]:
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    monkeypatch.setattr(remote, "_create_source_archive", source_archive)

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)

    def capture(*_args: object, **_kwargs: object) -> tuple[Path, int]:
        events.append("capture")
        raise remote.RemoteCaptureError("remote/case/retrieval failure")

    def terminate(_handle: object) -> object:
        events.append("terminate")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_capture_prospective_over_ssh", capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard, "terminate_guarded_instance", terminate
    )
    monkeypatch.setattr(
        remote,
        "_extract_prospective_archive",
        lambda *_a, **_k: pytest.fail("failed capture must not extract"),
    )
    monkeypatch.setattr(
        remote,
        "_prospective_session_identities",
        lambda *_a, **_k: pytest.fail("failed capture must not parse identities"),
    )
    monkeypatch.setattr(
        remote,
        "_verify_prospective_session",
        lambda *_a, **_k: pytest.fail("failed capture must not verify"),
    )
    monkeypatch.setattr(
        remote,
        "_finalize_prospective_capture",
        lambda *_a, **_k: pytest.fail("failed capture must not finalize or verify"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="remote/case/retrieval"):
        remote._capture_prospective_with_handoff(args, COMMIT, tmp_path / "id")
    assert events == ["arm", "capture", "terminate"]


def test_unconfirmed_guard_termination_blocks_prospective_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _prospective_args(
        tmp_path,
        lambda_billing_started_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        lambda_hourly_rate_usd="1.00",
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        max_cost_usd="1.00",
    )
    snapshot = _snapshot()
    archive = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")
    events: list[str] = []
    watchdog = SimpleNamespace(
        cost_window=SimpleNamespace(deadline=None),
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "create_handoff_archive",
        lambda *_args, **_kwargs: archive,
    )
    monkeypatch.setattr(
        remote, "_validate_prospective_snapshot", lambda *_a, **_k: None
    )

    def source_archive(
        path: Path, _commit: str, **_kwargs: object
    ) -> tuple[str, int]:
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    monkeypatch.setattr(remote, "_create_source_archive", source_archive)

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)

    def capture(*_args: object, **_kwargs: object) -> tuple[Path, int]:
        events.append("capture")
        return tmp_path / "captured", len(SOURCE_BYTES)

    def terminate(_handle: object) -> object:
        events.append("terminate")
        raise remote.lambda_gpu_guard.LambdaGuardError("termination not confirmed")

    monkeypatch.setattr(remote, "_capture_prospective_over_ssh", capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard, "terminate_guarded_instance", terminate
    )
    monkeypatch.setattr(
        remote,
        "_extract_prospective_archive",
        lambda *_a, **_k: pytest.fail("unconfirmed termination must not extract"),
    )
    monkeypatch.setattr(
        remote,
        "_prospective_session_identities",
        lambda *_a, **_k: pytest.fail(
            "unconfirmed termination must not parse identities"
        ),
    )
    monkeypatch.setattr(
        remote,
        "_verify_prospective_session",
        lambda *_a, **_k: pytest.fail("unconfirmed termination must not verify"),
    )
    monkeypatch.setattr(
        remote,
        "_finalize_prospective_capture",
        lambda *_a, **_k: pytest.fail(
            "unconfirmed termination must block verification"
        ),
    )

    with pytest.raises(remote.RemoteCaptureError, match="not confirmed"):
        remote._capture_prospective_with_handoff(args, COMMIT, tmp_path / "id")
    assert events == ["arm", "capture", "terminate"]


def test_prospective_retrieval_receipt_keeps_three_identities_without_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = handoff.create_handoff_archive(_snapshot(), tmp_path / "handoff.tar.gz")
    capture_path = tmp_path / "captured"
    capture_path.mkdir()
    (capture_path / "session").mkdir()
    (capture_path / "prospective-session.tar.gz.metadata.json").write_text(
        json.dumps(
            {
                "archive_name": "prospective-session.tar.gz",
                "archive_sha256": "sha256:" + "a" * 64,
                "schema_version": remote._PROSPECTIVE_TRANSFER_METADATA_SCHEMA,
                "size_bytes": 7,
            }
        )
    )
    identities = [
        {
            "case_id": case_id,
            "run_id": f"run-{index}",
            "bundle_digest": f"sha256:{index:064x}",
            "request_plan_digest": f"sha256:{index + 10:064x}",
        }
        for index, case_id in enumerate(handoff.CASE_IDS, start=1)
    ]
    remote._write_json(
        capture_path / "prospective-transport.json",
        {
            "case_identities": identities,
            "handoff_archive_sha256": snapshot.archive_sha256,
            "handoff_archive_size_bytes": snapshot.archive_size_bytes,
            "handoff_manifest_sha256": snapshot.manifest_sha256,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.prospective-ssh-transport.v1",
            "session_id": "prospective-real-gpu-session",
            "source_archive_sha256": SOURCE_DIGEST,
            "source_archive_size_bytes": len(SOURCE_BYTES),
            "ssh_host_identity_sha256": "sha256:" + HOST_KEY_DIGEST,
            "workload_sha256": snapshot.workload_sha256,
        },
    )
    monkeypatch.setattr(
        remote,
        "_verify_prospective_session",
        lambda *_args, **_kwargs: {"valid": True, "schema_version": "test"},
    )
    result = remote._finalize_prospective_capture(
        capture_path,
        snapshot,
        guarded_termination=None,
        verified_session_identities=(
            "prospective-real-gpu-session",
            identities,
        ),
    )
    receipt = json.loads((result / "retrieval-receipt.json").read_text())
    assert receipt["status"] == "CAPTURED_PENDING_EXTERNAL_EXITSPEC"
    assert receipt["publication_status"] == "EXTERNAL_ONLY"
    assert receipt["chronology"] == "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT"
    assert receipt["source_archive"]["sha256"] == SOURCE_DIGEST
    assert receipt["handoff_archive"]["sha256"] == snapshot.archive_sha256
    assert receipt["retrieved_archive"]["sha256"].startswith("sha256:")
    assert len(receipt["case_identities"]) == 3
    assert len({item["run_id"] for item in receipt["case_identities"]}) == 3
    assert len({item["bundle_digest"] for item in receipt["case_identities"]}) == 3
    assert "acceptance_verdict" not in receipt

    transport_path = capture_path / "prospective-transport.json"
    transport = json.loads(transport_path.read_text())
    transport["case_identities"][0]["run_id"] = "tampered-run"
    transport_path.chmod(0o600)
    transport_path.write_text(json.dumps(transport))
    with pytest.raises(remote.RemoteCaptureError, match="identities disagree"):
        remote._finalize_prospective_capture(
            capture_path,
            snapshot,
            guarded_termination=None,
            verified_session_identities=(
                "prospective-real-gpu-session",
                identities,
            ),
        )
