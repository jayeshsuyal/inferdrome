"""Remote real-GPU capture transport and verification boundaries."""

import argparse
import hashlib
import io
import json
import stat
import tarfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.capture_real_gpu_over_ssh as remote
import scripts.real_gpu_capture as capture

COMMIT = "a" * 40


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _capture_tree(root: Path, *, receipt_commit: str = COMMIT) -> str:
    support = root / "support"
    support.mkdir(parents=True)
    packages = b"inferdrome==0.1\n"
    host_preparation = {
        "architecture": "x86_64",
        "model_directory": "/tmp/model",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "b" * 40,
        "prepared_at": "2026-08-11T00:00:00Z",
        "python_packages_sha256": "sha256:" + hashlib.sha256(packages).hexdigest(),
        "repository_commit": COMMIT,
        "schema_version": "inferdrome.real-gpu-host-preparation.v1",
        "vllm_wheel_filename": "vllm-0.26.0.whl",
        "vllm_wheel_sha256": f"sha256:{'c' * 64}",
    }
    _write_json(support / "host-preparation.json", host_preparation)
    (support / "python-packages.txt").write_bytes(packages)
    (support / "vllm-version.txt").write_text("0.26.0\n", encoding="utf-8")
    (support / "inferdrome-version.txt").write_text("0.1.0.dev0\n", encoding="utf-8")
    host_digest = "sha256:" + hashlib.sha256(
        (support / "host-preparation.json").read_bytes()
    ).hexdigest()
    _write_json(
        root / "single" / "real-gpu-example" / "demo-receipt.json",
        {
            "host_preparation_sha256": host_digest,
            "repository_commit": receipt_commit,
            "schema_version": "inferdrome.real-gpu-demo-receipt.v1",
        },
    )
    _write_json(
        root
        / "comparison"
        / "real-gpu-comparison-example"
        / "comparison-demo-receipt.json",
        {
            "host_preparation_sha256": host_digest,
            "repository_commit": receipt_commit,
            "schema_version": (
                "inferdrome.real-gpu-comparison-demo-receipt.v1"
            ),
        },
    )
    return host_digest


@pytest.mark.parametrize(
    "value",
    ["ubuntu@203.0.113.4", "gpu.example.test", "[2001:db8::1]"],
)
def test_remote_destination_accepts_bounded_openssh_targets(value: str) -> None:
    assert remote._validate_destination(value) == value


@pytest.mark.parametrize(
    "value",
    ["-oProxyCommand=bad", "user@host name", "user@host;bad", "user@@host", ""],
)
def test_remote_destination_rejects_option_and_shell_injection(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        remote._validate_destination(value)


def test_optional_host_identity_digest_is_strict_lowercase_hex() -> None:
    assert remote._host_key_digest("a" * 64) == "a" * 64

    for value in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(argparse.ArgumentTypeError):
            remote._host_key_digest(value)


def test_remote_command_pins_commit_and_bounds_workload() -> None:
    script = remote._remote_capture_script(
        "/tmp/inferdrome-safe",
        COMMIT,
        gpu_index=0,
        startup_timeout_seconds=900,
        remote_timeout_seconds=9_900,
    )

    assert f"[[ $(git rev-parse --verify HEAD) == {COMMIT} ]]" in script
    assert "timeout --foreground --signal=TERM --kill-after=60s 9900s" in script
    assert "--capture-root /tmp/inferdrome-safe/capture" in script
    assert "unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES" in script


def test_remote_preflight_requires_build_tools_and_python_headers() -> None:
    script = remote._remote_preflight_script("/tmp/inferdrome-safe")

    assert "bash curl git ninja python3.12 nvidia-smi" in script
    assert "Python.h" in script
    assert "Python 3.12 development headers" in script


def test_lambda_guard_requires_actual_billing_start() -> None:
    args = SimpleNamespace(
        lambda_billing_started_at=None,
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        max_cost_usd=Decimal("2.58"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="billing-started-at"):
        remote._lambda_guard_requested(args)


def test_lambda_guard_binds_explicit_instance_to_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def arm(reference: str, **kwargs: object) -> SimpleNamespace:
        observed["reference"] = reference
        observed["expected_endpoint"] = kwargs["expected_endpoint"]
        return SimpleNamespace(
            cost_window=SimpleNamespace(
                deadline=datetime(2026, 8, 18, 22, 0, tzinfo=UTC),
            ),
            state_directory=Path("/tmp/inferdrome-test-guard"),
        )

    monkeypatch.setattr(remote.lambda_gpu_guard, "arm_watchdog", arm)
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        max_cost_usd=Decimal("2.58"),
    )

    remote._arm_lambda_watchdog(args)

    assert observed == {
        "expected_endpoint": "capture.example.test",
        "reference": "b" * 32,
    }


def test_capture_terminates_guarded_instance_even_after_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    observed: list[object] = []
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def fail_capture(*_args: object) -> Path:
        raise remote.RemoteCaptureError("proof failed")

    monkeypatch.setattr(remote, "_capture_over_ssh", fail_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda handle: observed.append(handle)
        or SimpleNamespace(final_status="absent"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="proof failed"):
        remote._capture(SimpleNamespace(), COMMIT, None)

    assert observed == [watchdog]


def test_guard_failure_does_not_mask_the_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def fail_capture(*_args: object) -> Path:
        raise remote.RemoteCaptureError("original proof failure")

    def fail_termination(_handle: object) -> None:
        raise remote.lambda_gpu_guard.LambdaGuardError("provider unavailable")

    monkeypatch.setattr(remote, "_capture_over_ssh", fail_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        fail_termination,
    )

    with pytest.raises(remote.RemoteCaptureError, match="original proof failure"):
        remote._capture(SimpleNamespace(), COMMIT, None)


def test_completed_capture_manifest_anchors_receipts_and_support(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    host_digest = _capture_tree(root)

    manifest_path = capture.write_capture_manifest(root, COMMIT)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["repository_commit"] == COMMIT
    assert manifest["support"]["host_preparation"]["sha256"] == host_digest
    assert manifest["single"]["receipt_path"].endswith("demo-receipt.json")
    assert manifest["comparison"]["receipt_path"].endswith(
        "comparison-demo-receipt.json"
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o222 == 0


def test_capture_manifest_rejects_receipt_from_another_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    _capture_tree(root, receipt_commit="b" * 40)

    with pytest.raises(capture.CaptureError, match="capture commit"):
        capture.write_capture_manifest(root, COMMIT)


def test_manifest_verification_rechecks_support_and_dispatches_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    _capture_tree(root)
    capture.write_capture_manifest(root, COMMIT)
    calls: list[str] = []

    def single(*_args: object, **_kwargs: object) -> dict[str, str]:
        calls.append("single")
        return {"bundle_digest": f"sha256:{'c' * 64}", "run_id": f"run-{'c' * 32}"}

    def comparison(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("comparison")
        return {"status": "COMPARABLE", "run_ids": []}

    monkeypatch.setattr(capture, "_verify_single_receipt", single)
    monkeypatch.setattr(capture, "_verify_comparison_receipt", comparison)

    result = capture.verify_capture(root, expected_repository_commit=COMMIT)

    assert result["valid"] is True
    assert result["repository_commit"] == COMMIT
    assert calls == ["single", "comparison"]

    with pytest.raises(capture.CaptureError, match="not the expected commit"):
        capture.verify_capture(root, expected_repository_commit="d" * 40)


def test_failure_receipt_is_explicitly_not_evidence(tmp_path: Path) -> None:
    root = tmp_path / "capture"

    path = capture.write_failure_receipt(
        root,
        COMMIT,
        failed_step="comparison-proof",
        exit_code=143,
    )
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["proof_status"] == "INCOMPLETE_NOT_EVIDENCE"
    assert value["process_exit_code"] == 143
    assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0

    verified = capture.verify_failure_capture(
        root,
        expected_repository_commit=COMMIT,
    )
    assert verified == value


def test_failure_receipt_verification_rejects_commit_drift(tmp_path: Path) -> None:
    root = tmp_path / "capture"
    capture.write_failure_receipt(
        root,
        COMMIT,
        failed_step="single-proof",
        exit_code=1,
    )

    with pytest.raises(capture.CaptureError, match="not the expected commit"):
        capture.verify_failure_capture(
            root,
            expected_repository_commit="b" * 40,
        )


def _archive_with_member(path: Path, member: tarfile.TarInfo, content: bytes) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        root = tarfile.TarInfo("capture")
        root.type = tarfile.DIRTYPE
        root.mode = 0o700
        archive.addfile(root)
        if member.isfile():
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        else:
            archive.addfile(member)


def test_capture_archive_extracts_only_bounded_regular_tree(tmp_path: Path) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-failure.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")

    extracted = capture.extract_capture_archive(archive, tmp_path / "retrieved")

    assert (extracted / "capture-failure.json").read_bytes() == b"{}\n"
    assert stat.S_IMODE(extracted.stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (extracted / "capture-failure.json").stat().st_mode
    ) == 0o444


def test_capture_archive_preserves_sealed_bundle_modes(tmp_path: Path) -> None:
    archive = tmp_path / "capture.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        for name, mode in (
            ("capture", 0o700),
            ("capture/single", 0o700),
            ("capture/single/example", 0o700),
            ("capture/single/example/runs", 0o700),
            ("capture/single/example/runs/run-" + "a" * 32, 0o700),
            (
                "capture/single/example/runs/run-"
                + "a" * 32
                + "/bundle",
                0o500,
            ),
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = mode
            retained.addfile(member)
        descriptor = tarfile.TarInfo(
            "capture/single/example/runs/run-"
            + "a" * 32
            + "/bundle/bundle.json"
        )
        descriptor.mode = 0o400
        descriptor.size = 3
        retained.addfile(descriptor, io.BytesIO(b"{}\n"))

    extracted = capture.extract_capture_archive(archive, tmp_path / "retrieved")
    bundle = (
        extracted
        / "single"
        / "example"
        / "runs"
        / ("run-" + "a" * 32)
        / "bundle"
    )

    assert stat.S_IMODE(bundle.stat().st_mode) == 0o500
    assert stat.S_IMODE((bundle / "bundle.json").stat().st_mode) == 0o400


def test_capture_archive_rejects_too_many_members_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "directory-tarbomb.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        for name in ("capture", "capture/one", "capture/two"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            retained.addfile(member)
    monkeypatch.setattr(capture, "_MAX_CAPTURE_MEMBERS", 2)
    destination = tmp_path / "retrieved"

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture.extract_capture_archive(archive, destination)

    assert not (destination / "capture").exists()


def test_capture_archive_bounds_implicit_directories_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "implicit-directory-tarbomb.tar.gz"
    member = tarfile.TarInfo("capture/one/two/value.json")
    member.mode = 0o400
    _archive_with_member(archive, member, b"{}\n")
    monkeypatch.setattr(capture, "_MAX_CAPTURE_DIRECTORIES", 2)
    destination = tmp_path / "retrieved"

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture.extract_capture_archive(archive, destination)

    assert not (destination / "capture").exists()


def test_capture_tree_rejects_too_many_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    (root / "one" / "two").mkdir(parents=True)
    monkeypatch.setattr(capture, "_MAX_CAPTURE_DIRECTORIES", 2)

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture._validate_capture_tree(root)


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_capture_archive_rejects_unsafe_members(tmp_path: Path, kind: str) -> None:
    archive = tmp_path / f"{kind}.tar.gz"
    if kind == "traversal":
        member = tarfile.TarInfo("capture/../outside")
    else:
        member = tarfile.TarInfo("capture/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp/outside"
    _archive_with_member(archive, member, b"unsafe")

    with pytest.raises(capture.CaptureError, match="unsafe member"):
        capture.extract_capture_archive(archive, tmp_path / f"extract-{kind}")
