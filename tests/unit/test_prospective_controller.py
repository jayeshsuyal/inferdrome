"""Prospective-only controller sequencing and transport boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import select
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
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


def _waitpid_bounded(pid: int, *, timeout_seconds: float = 3.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.01)
    raise AssertionError("signal-test child did not exit within the bound")


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
        "expected_commit": COMMIT,
        "expected_handoff_manifest_sha256": "sha256:" + "2" * 64,
        "expected_source_archive_sha256": SOURCE_DIGEST,
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


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_commit": None},
        {"expected_commit": "b" * 40},
        {"expected_source_archive_sha256": None},
    ],
)
def test_prospective_source_pins_fail_before_snapshot_watchdog_or_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, str | None],
) -> None:
    args = _prospective_args(tmp_path, **mutation)
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: pytest.fail("source pins must precede snapshot"),
    )
    monkeypatch.setattr(
        remote,
        "_arm_lambda_watchdog",
        lambda *_args, **_kwargs: pytest.fail(
            "source pins must precede provider action"
        ),
    )
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("source pins must precede SSH"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="prospective"):
        remote._capture_prospective_with_handoff(
            args,
            COMMIT,
            tmp_path / "id_ed25519",
        )


def test_wrong_prospective_archive_pin_fails_before_watchdog_or_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_digest = "sha256:" + "0" * 64
    args = _prospective_args(
        tmp_path,
        expected_source_archive_sha256=wrong_digest,
    )
    snapshot = _snapshot()
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "create_handoff_archive",
        lambda value, _path: value,
    )
    monkeypatch.setattr(
        remote,
        "_validate_prospective_snapshot",
        lambda *_args, **_kwargs: None,
    )

    def reject_source_pin(
        _path: Path,
        _commit: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> tuple[str, int]:
        assert expected_archive_sha256 == wrong_digest
        raise remote.RemoteCaptureError(
            "exact source archive disagrees with its independent digest pin"
        )

    monkeypatch.setattr(remote, "_create_source_archive", reject_source_pin)
    monkeypatch.setattr(
        remote,
        "_arm_lambda_watchdog",
        lambda *_args, **_kwargs: pytest.fail(
            "pin mismatch must precede provider action"
        ),
    )
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("pin mismatch must precede SSH"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="digest pin"):
        remote._capture_prospective_with_handoff(
            args,
            COMMIT,
            tmp_path / "id_ed25519",
        )


@pytest.mark.parametrize(
    "missing",
    [
        "expected_commit",
        "expected_source_archive_sha256",
        "identity_file",
        "host_key_sha256",
    ],
)
@pytest.mark.parametrize("git_checkout_present", [True, False])
def test_prospective_mode_requires_all_pins_in_git_and_exported_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    git_checkout_present: bool,
) -> None:
    monkeypatch.setattr(
        remote,
        "_git_checkout_present",
        lambda: git_checkout_present,
    )
    values = {missing: None}
    with pytest.raises(remote.RemoteCaptureError, match="explicit"):
        remote._validate_capture_mode(_prospective_args(tmp_path, **values))


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_commit": "A" * 40},
        {"expected_source_archive_sha256": "a" * 64},
        {"expected_source_archive_sha256": "sha256:" + "A" * 64},
    ],
)
def test_prospective_source_pins_require_exact_lowercase_tagged_shapes(
    tmp_path: Path,
    mutation: dict[str, str],
) -> None:
    with pytest.raises(remote.RemoteCaptureError, match="prospective"):
        remote._validate_capture_mode(_prospective_args(tmp_path, **mutation))


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

    def source_archive(path: Path, _commit: str, **_kwargs: object) -> tuple[str, int]:
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
    local_identity = remote.resolve_executable_identity(sys.executable)
    monkeypatch.setattr(remote, "_local_executable", lambda _name: local_identity)

    def run(arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = [str(item) for item in arguments]  # type: ignore[arg-type]
        assert "StrictHostKeyChecking=yes" in argv
        assert "IdentityAgent=none" in argv
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

    def bounded(arguments: object, destination: Path, **_kwargs: object) -> None:
        events.append("metadata")
        argv = [str(item) for item in arguments]  # type: ignore[arg-type]
        assert "StrictHostKeyChecking=yes" in argv
        assert "IdentityAgent=none" in argv
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

    def exact(arguments: object, destination: Path, **_kwargs: object) -> None:
        events.append("archive")
        argv = [str(item) for item in arguments]  # type: ignore[arg-type]
        assert "StrictHostKeyChecking=yes" in argv
        assert "IdentityAgent=none" in argv
        destination.write_bytes(b"archive")

    monkeypatch.setattr(remote, "_download_bounded_remote_file", bounded)
    monkeypatch.setattr(remote, "_download_exact_remote_file", exact)
    original_publish = remote._publish_no_replace

    def publish(source: Path, destination: Path, *, label: str) -> None:
        events.append("publish")
        assert label == "local prospective capture destination"
        original_publish(source, destination, label=label)

    monkeypatch.setattr(remote, "_publish_no_replace", publish)
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
        "publish",
    ]
    assert "StrictHostKeyChecking=yes" in remote._ssh_options(
        identity=identity,
        known_hosts=tmp_path / "known",
        port=22,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_sigterm_enters_immediate_guard_cleanup_for_prospective_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    identity = tmp_path / "id_ed25519"
    identity.write_bytes(b"synthetic key")
    ready_read, ready_write = os.pipe()
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(
        cost_window=None,
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(
        remote.prospective_handoff,
        "snapshot_handoff",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(remote, "_validate_prospective_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        remote,
        "_prospective_source_archive_pin",
        lambda *_args: SOURCE_DIGEST,
    )

    def create_source(
        path: Path,
        _commit: str,
        **_kwargs: object,
    ) -> tuple[str, int]:
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    def block_capture(*_args: object, **_kwargs: object) -> tuple[Path, int]:
        os.write(ready_write, b"r")
        while True:
            signal.pause()

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.write(cleanup_write, b"c")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)
    monkeypatch.setattr(remote, "_capture_prospective_over_ssh", block_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = _prospective_args(
        tmp_path,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        max_cost_usd=Decimal("0.75"),
    )

    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(cleanup_read)
        try:
            remote._capture(args, COMMIT, identity)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(ready_write)
    os.close(cleanup_write)
    reaped = False
    try:
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b"r"
        os.kill(pid, signal.SIGTERM)
        status = _waitpid_bounded(pid)
        reaped = True
        cleanup, _, _ = select.select([cleanup_read], [], [], 1)
        assert cleanup and os.read(cleanup_read, 1) == b"c"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(ready_read)
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_prospective_final_publication_never_replaces_raced_destination(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".capture.staging"
    staging.mkdir()
    (staging / "owned").write_bytes(b"staged evidence")
    destination = tmp_path / "capture"
    destination.mkdir()
    marker = destination / "unowned"
    marker.write_bytes(b"must remain")

    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._publish_no_replace(
            staging,
            destination,
            label="local prospective capture destination",
        )

    assert marker.read_bytes() == b"must remain"
    assert (staging / "owned").read_bytes() == b"staged evidence"


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
    local_identity = remote.resolve_executable_identity(sys.executable)
    monkeypatch.setattr(remote, "_local_executable", lambda _name: local_identity)
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
        handoff_manifest_sha256="sha256:" + "d" * 64,
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
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )
    for index, block in enumerate(
        re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, flags=re.DOTALL)
    ):
        compile(block, f"<prospective-remote-python-{index}>", "exec")


def test_remote_source_retention_stream_is_bounded_and_exclusive(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    script = remote._remote_prospective_capture_script(
        "/tmp/r",
        COMMIT,
        SOURCE_DIGEST,
        snapshot,
        handoff_archive_sha256="sha256:" + "b" * 64,
        handoff_archive_size=100,
        handoff_manifest_sha256="sha256:" + "d" * 64,
        workload_sha256="sha256:" + "c" * 64,
        remote_state_root="/prepared/state",
        gpu_index=0,
        startup_timeout_seconds=900,
        remote_timeout_seconds=300,
    )
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, flags=re.DOTALL)
    retention = blocks[0]

    def run(
        source: Path,
        repository: Path,
        digest: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-", str(repository), str(source), COMMIT, digest],
            input=retention,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    source = tmp_path / "repo.tar"
    source.write_bytes(SOURCE_BYTES)
    repository = tmp_path / "repository"
    repository.mkdir()
    result = run(source, repository, SOURCE_DIGEST)
    assert result.returncode == 0, result.stderr
    assert (
        repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    ).read_bytes() == SOURCE_BYTES
    assert (repository / ".inferdrome-source-export.json").is_file()

    oversized_source = tmp_path / "oversized.tar"
    oversized_source.write_bytes(b"x")
    with oversized_source.open("r+b") as stream:
        stream.truncate(remote._MAX_SOURCE_ARCHIVE_BYTES + 1)
    oversized_repository = tmp_path / "oversized-repository"
    oversized_repository.mkdir()
    assert run(oversized_source, oversized_repository, SOURCE_DIGEST).returncode != 0
    assert not (oversized_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).exists()

    truncated_source = tmp_path / "truncated.tar"
    truncated_source.write_bytes(b"short")
    truncated_repository = tmp_path / "truncated-repository"
    truncated_repository.mkdir()
    assert run(truncated_source, truncated_repository, SOURCE_DIGEST).returncode != 0
    assert (
        truncated_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    ).stat().st_size <= len(SOURCE_BYTES)

    growing_source = tmp_path / "growing.tar"
    growing_source.write_bytes(SOURCE_BYTES + b"growth")
    growing_repository = tmp_path / "growing-repository"
    growing_repository.mkdir()
    assert run(growing_source, growing_repository, SOURCE_DIGEST).returncode != 0
    assert (
        growing_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    ).stat().st_size <= len(SOURCE_BYTES) + len(b"growth")

    linked_source = tmp_path / "linked.tar"
    linked_source.symlink_to(source)
    linked_repository = tmp_path / "linked-repository"
    linked_repository.mkdir()
    assert run(linked_source, linked_repository, SOURCE_DIGEST).returncode != 0
    assert not (linked_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).exists()

    existing_link_repository = tmp_path / "existing-link-repository"
    existing_link_repository.mkdir()
    target = tmp_path / "retained-target"
    target.write_bytes(b"must remain")
    (existing_link_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).symlink_to(target)
    assert run(source, existing_link_repository, SOURCE_DIGEST).returncode != 0
    assert (
        existing_link_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    ).is_symlink()
    assert target.read_bytes() == b"must remain"

    replacement_repository = tmp_path / "replacement-repository"
    replacement_repository.mkdir()
    replacement_target = tmp_path / "replacement-target"
    replacement_target.write_bytes(b"must remain")
    hook_root = tmp_path / "retention-race-hook"
    hook_root.mkdir()
    (hook_root / "sitecustomize.py").write_text(
        """import os
from pathlib import Path

original_lstat = os.lstat
triggered = False

def replace_retained(path, *args, **kwargs):
    global triggered
    selected = Path(path)
    if not triggered and selected.name == '.inferdrome-source-archive.tar':
        selected.unlink()
        selected.symlink_to(Path(os.environ['INFERDROME_TEST_REPLACEMENT']))
        triggered = True
    return original_lstat(path, *args, **kwargs)

os.lstat = replace_retained
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(hook_root)
    environment["INFERDROME_TEST_REPLACEMENT"] = str(replacement_target)
    assert (
        run(
            source,
            replacement_repository,
            SOURCE_DIGEST,
            environment=environment,
        ).returncode
        != 0
    )
    replaced_retained = (
        replacement_repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    )
    assert replaced_retained.is_symlink()
    assert replacement_target.read_bytes() == b"must remain"


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


def test_prospective_download_preserves_destination_created_at_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "archive"
    original_rename = remote._rename_no_replace

    def race_publication(source: Path, selected: Path) -> None:
        assert selected == destination
        selected.write_bytes(b"unowned replacement")
        original_rename(source, selected)

    monkeypatch.setattr(remote, "_rename_no_replace", race_publication)
    code = "import sys; sys.stdout.buffer.write(b'complete')"

    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._download_remote_file(
            [sys.executable, "-c", code],
            destination,
            minimum_size=8,
            maximum_size=8,
            timeout=5,
        )

    assert destination.read_bytes() == b"unowned replacement"


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
        remote._extract_prospective_archive(
            archive_path.read_bytes(), tmp_path / "extracted"
        )


def test_prospective_extraction_preserves_unowned_destination_on_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "session.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        member = tarfile.TarInfo("session/result.json")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"{}"))
    target = tmp_path / "destination-target"
    target.mkdir()
    destination = tmp_path / "extracted"
    destination.symlink_to(target, target_is_directory=True)

    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._extract_prospective_archive(archive_path.read_bytes(), destination)
    assert destination.is_symlink()
    assert target.is_dir()

    destination.unlink()
    original_rename = remote._rename_no_replace

    def race_publication(source: Path, selected: Path) -> None:
        assert selected == destination
        destination.symlink_to(target, target_is_directory=True)
        original_rename(source, selected)

    monkeypatch.setattr(remote, "_rename_no_replace", race_publication)
    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._extract_prospective_archive(archive_path.read_bytes(), destination)
    assert destination.is_symlink()
    assert target.is_dir()


def test_prospective_extraction_rejects_nested_directory_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "session.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        member = tarfile.TarInfo("session/nested/result.json")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"{}"))
    target = tmp_path / "nested-target"
    target.mkdir()
    marker = target / "marker"
    marker.write_bytes(b"must remain")
    original_mkdir = remote.os.mkdir

    def race_nested_directory(
        path: os.PathLike[str] | str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode=mode, dir_fd=dir_fd)
        if path == "nested" and dir_fd is not None:
            os.rmdir(path, dir_fd=dir_fd)
            os.symlink(target, path, dir_fd=dir_fd, target_is_directory=True)

    monkeypatch.setattr(remote.os, "mkdir", race_nested_directory)

    with pytest.raises(remote.RemoteCaptureError, match="unsafe"):
        remote._extract_prospective_archive(
            archive_path.read_bytes(),
            tmp_path / "extracted",
        )

    assert marker.read_bytes() == b"must remain"
    assert not (target / "result.json").exists()


@pytest.mark.parametrize("limit", ["depth", "implicit-directories"])
def test_prospective_extraction_bounds_directory_amplification(
    tmp_path: Path,
    limit: str,
) -> None:
    archive_path = tmp_path / f"{limit}.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        if limit == "depth":
            parts = ["session"] + [
                f"d{index}" for index in range(remote._PROSPECTIVE_MAX_PATH_DEPTH)
            ]
            member = tarfile.TarInfo("/".join(parts))
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        else:
            for index in range(remote._PROSPECTIVE_MAX_IMPLICIT_DIRECTORIES + 1):
                member = tarfile.TarInfo(f"session/d{index}/result.json")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(remote.RemoteCaptureError, match="unsafe"):
        remote._extract_prospective_archive(
            archive_path.read_bytes(),
            tmp_path / "extracted",
        )


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

    def source_archive(path: Path, _commit: str, **_kwargs: object) -> tuple[str, int]:
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


@pytest.mark.parametrize(
    ("termination_confirms", "error_pattern"),
    [
        (
            True,
            "immediate exact-ID termination confirmed \\(absent\\)",
        ),
        (
            False,
            (
                r"immediate exact-ID termination was not confirmed.*"
                r"provider cleanup timeout"
            ),
        ),
    ],
    ids=["confirmed", "unconfirmed"],
)
def test_prospective_guard_arm_failure_terminates_exact_target_before_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination_confirms: bool,
    error_pattern: str,
) -> None:
    instance_id = "b" * 32
    args = _prospective_args(
        tmp_path,
        lambda_billing_started_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        lambda_hourly_rate_usd="1.00",
        lambda_instance_id=instance_id,
        lambda_instance_type_name="gpu_1x_a10",
        max_cost_usd="1.00",
    )
    snapshot = _snapshot()
    archive = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")
    events: list[str] = []
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
        remote, "_validate_prospective_snapshot", lambda *_args, **_kwargs: None
    )

    def source_archive(path: Path, _commit: str, **_kwargs: object) -> tuple[str, int]:
        events.append("source")
        path.write_bytes(SOURCE_BYTES)
        return SOURCE_DIGEST, len(SOURCE_BYTES)

    def arm(reference: str, **kwargs: object) -> object:
        events.append(f"arm:{reference}")
        assert kwargs["expected_endpoint"] == "prepared.example.test"
        raise remote.lambda_gpu_guard.LambdaGuardError(
            "target resolved but watchdog readiness failed"
        )

    def terminate_after_arm_failure(target: str) -> SimpleNamespace:
        events.append(f"terminate:{target}")
        if not termination_confirms:
            raise remote.lambda_gpu_guard.LambdaGuardError("provider cleanup timeout")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", source_archive)
    monkeypatch.setattr(remote.lambda_gpu_guard, "arm_watchdog", arm)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_after_arm_failure",
        terminate_after_arm_failure,
    )
    monkeypatch.setattr(
        remote,
        "_capture_prospective_over_ssh",
        lambda *_args, **_kwargs: pytest.fail(
            "guard arming failure must block SSH capture"
        ),
    )
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *_args, **_kwargs: pytest.fail(
            "guard arming failure must not invoke SSH or SCP"
        ),
    )

    with pytest.raises(remote.RemoteCaptureError, match=error_pattern):
        remote._capture_prospective_with_handoff(args, COMMIT, tmp_path / "id")

    assert events == ["source", f"arm:{instance_id}", f"terminate:{instance_id}"]


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

    def source_archive(path: Path, _commit: str, **_kwargs: object) -> tuple[str, int]:
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

    def source_archive(path: Path, _commit: str, **_kwargs: object) -> tuple[str, int]:
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
