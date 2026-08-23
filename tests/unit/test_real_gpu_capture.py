"""Remote real-GPU capture transport and verification boundaries."""

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.capture_real_gpu_over_ssh as remote
import scripts.real_gpu_capture as capture

COMMIT = "a" * 40
SOURCE_ARCHIVE_SHA256 = "sha256:" + "f" * 64


def _assert_embedded_python_compiles(script: str) -> None:
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, flags=re.DOTALL)
    assert blocks
    for index, block in enumerate(blocks):
        compile(block, f"<generated-remote-python-{index}>", "exec")


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
    host_digest = (
        "sha256:"
        + hashlib.sha256((support / "host-preparation.json").read_bytes()).hexdigest()
    )
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
            "schema_version": ("inferdrome.real-gpu-comparison-demo-receipt.v1"),
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


def test_ssh_transport_ignores_user_config_and_disables_forwarding(
    tmp_path: Path,
) -> None:
    options = remote._ssh_options(
        identity=tmp_path / "id_ed25519",
        known_hosts=tmp_path / "known-hosts",
        port=22,
    )
    rendered = " ".join(options)

    assert options[:2] == ["-F", "/dev/null"]
    assert "ForwardAgent=no" in rendered
    assert "ForwardX11=no" in rendered
    assert "ClearAllForwardings=yes" in rendered
    assert "SendEnv=-*" in rendered
    assert "ProxyCommand=none" in rendered
    assert "ProxyJump=none" in rendered
    assert "IdentitiesOnly=yes" in rendered


def test_exact_tree_export_excludes_removed_secret_and_git_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Inferdrome Test"],
        check=True,
    )
    secret = repository / "removed-secret.txt"
    secret.write_text("SENTINEL_PRIVATE_SECRET", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "secret history"],
        check=True,
    )
    secret.unlink()
    (repository / "README.md").write_text("safe tree\n", encoding="utf-8")
    nested = repository / "src" / "nested.txt"
    nested.parent.mkdir()
    nested.write_text("nested tree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "safe tree"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(remote, "REPOSITORY_ROOT", repository)
    archive = tmp_path / "repo.tar"

    digest, size = remote._create_source_archive(archive, commit)

    assert digest == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert size == archive.stat().st_size
    assert b"SENTINEL_PRIVATE_SECRET" not in archive.read_bytes()
    with tarfile.open(archive, "r:") as retained:
        assert [member.name for member in retained.getmembers()] == [
            "README.md",
            "src",
            "src/nested.txt",
        ]

    environment = repository / ".env.production"
    environment.write_text(
        "LAMBDA_" + "CLOUD_API_KEY=do-not-transfer\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "tracked environment"],
        check=True,
    )
    environment_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(remote.RemoteCaptureError, match="secret-bearing"):
        remote._create_source_archive(tmp_path / "blocked-env.tar", environment_commit)

    environment.unlink()
    private_material = repository / "apparently-safe.txt"
    private_material.write_text(
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-real-key\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "tracked private marker"],
        check=True,
    )
    private_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    blocked_key_archive = tmp_path / "blocked-key.tar"
    with pytest.raises(remote.RemoteCaptureError, match="private-key material"):
        remote._create_source_archive(blocked_key_archive, private_commit)
    assert not blocked_key_archive.exists()

    private_material.unlink()
    (repository / ".gitattributes").write_text(
        "README.md export-ignore\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "archive mutation"],
        check=True,
    )
    attributes_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(remote.RemoteCaptureError, match="attributes alter"):
        remote._create_source_archive(
            tmp_path / "blocked-attributes.tar", attributes_commit
        )


def test_remote_command_pins_commit_and_bounds_workload() -> None:
    script = remote._remote_capture_script(
        "/tmp/inferdrome-safe",
        COMMIT,
        SOURCE_ARCHIVE_SHA256,
        gpu_index=0,
        startup_timeout_seconds=900,
        remote_timeout_seconds=9_900,
    )

    assert COMMIT in script
    assert SOURCE_ARCHIVE_SHA256 in script
    assert "git clone" not in script
    assert "env -i" in script
    assert "timeout --foreground --signal=TERM --kill-after=60s 9900s" in script
    assert "--capture-root /tmp/inferdrome-safe/capture" in script
    assert "unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES" in script
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    _assert_embedded_python_compiles(script)


def test_qwen3_remote_command_requires_explicit_profile() -> None:
    script = remote._remote_capture_script(
        "/tmp/inferdrome-safe",
        COMMIT,
        SOURCE_ARCHIVE_SHA256,
        gpu_index=0,
        startup_timeout_seconds=300,
        remote_timeout_seconds=1_500,
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    assert "--managed-capability-profile" in script
    assert "--qwen3-gpu-tier" in script
    assert "a10-24gb-pcie" in script
    assert remote._QWEN3_PROFILE_ID in script
    assert "\n+  --managed-capability-profile" not in script
    assert "\n+  --qwen3-gpu-tier" not in script
    preflight = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        gpu_index=0,
        expected_gpu_model="NVIDIA A10",
    )
    assert "nvidia-smi --id=0" in preflight
    assert "NVIDIA A10" in preflight
    assert "at least 40 GiB free" in preflight
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    _assert_embedded_python_compiles(script)
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=preflight,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_qwen3_capture_mode_enforces_lambda_rate_instance_and_cap() -> None:
    base = {
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.29"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a10",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("0.75"),
        "qwen3_gpu_tier": "a10-24gb-pcie",
        "identity_file": "/tmp/inferdrome-key",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }
    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_instance_id": None}, "instance-id"),
        ({"lambda_instance_type_name": None}, "instance-type-name"),
        ({"lambda_hourly_rate_usd": Decimal("1.30")}, "1.29"),
        ({"max_cost_usd": Decimal("0.76")}, "0.75"),
        ({"identity_file": None}, "identity"),
        ({"startup_timeout_seconds": 301}, "300-second"),
        ({"lambda_billing_started_at": None}, "billing-started-at"),
    ):
        values = {**base, **mutation}
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**values))

    with pytest.raises(remote.RemoteCaptureError, match="unsupported"):
        remote._validate_capture_mode(
            SimpleNamespace(
                **{
                    **base,
                    "managed_capability_profile": "unknown-profile",
                }
            )
        )


def test_qwen3_a100_capture_mode_freezes_exact_tier_rate_and_cap() -> None:
    base = {
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.99"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a100_runtime_api_value",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("1.25"),
        "qwen3_gpu_tier": "a100-40gb-pcie",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("1.98")}, "1.99"),
        ({"max_cost_usd": Decimal("1.26")}, "1.25"),
        ({"qwen3_gpu_tier": "h100-80gb-pcie"}, "3.29"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(
                SimpleNamespace(**{**base, **mutation})
            )


def test_qwen3_a100_sxm4_capture_mode_freezes_exact_rate_and_cap() -> None:
    base = {
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 23, 17, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.99"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a100_sxm4",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("1.25"),
        "qwen3_gpu_tier": "a100-40gb-sxm4",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("2.00")}, "1.99"),
        ({"max_cost_usd": Decimal("1.26")}, "1.25"),
        ({"qwen3_gpu_tier": "h100-80gb-pcie"}, "3.29"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**{**base, **mutation}))


def test_qwen3_h100_capture_mode_freezes_exact_tier_rate_and_cap() -> None:
    base = {
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("3.29"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_h100_runtime_api_value",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("2.25"),
        "qwen3_gpu_tier": "h100-80gb-pcie",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("4.29")}, "3.29"),
        ({"max_cost_usd": Decimal("2.26")}, "2.25"),
        ({"qwen3_gpu_tier": "a100-40gb-pcie"}, "1.99"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**{**base, **mutation}))


def test_qwen3_phase_budget_fits_the_exact_cost_window() -> None:
    allowed_seconds = int(Decimal("0.75") / Decimal("1.29") * Decimal(3_600))

    assert allowed_seconds == 2_093
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert remote._QWEN3_POST_REMOTE_BUDGET_SECONDS == 298
    assert remote._QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS == 300


def test_a100_phase_budget_fits_its_exact_cost_window() -> None:
    policy = remote.qwen3_gpu_tier_policy("a100-40gb-pcie")

    assert policy.allowed_seconds == 2_261
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) < policy.allowed_seconds


def test_h100_phase_budget_preserves_termination_slack() -> None:
    policy = remote.qwen3_gpu_tier_policy("h100-80gb-pcie")

    assert policy.allowed_seconds == 2_462
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert policy.allowed_seconds - sum(
        remote._QWEN3_PHASE_BUDGET_SECONDS.values()
    ) == 384


def test_qwen3_transfer_metadata_rejects_oversized_archive(tmp_path: Path) -> None:
    metadata = tmp_path / "capture.tar.gz.metadata.json"
    _write_json(
        metadata,
        {
            "archive_name": "capture.tar.gz",
            "archive_sha256": "sha256:" + "a" * 64,
            "schema_version": "inferdrome.qwen3-transfer-metadata.v1",
            "size_bytes": remote._QWEN3_MAX_ARCHIVE_BYTES + 1,
        },
    )

    with pytest.raises(remote.RemoteCaptureError, match="invalid"):
        remote._read_transfer_metadata(metadata)


def test_qwen3_dry_run_discloses_termination_before_semantic_verification(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)

    plan = json.loads(capsys.readouterr().out)
    assert plan["expected_gpu_model"] == "NVIDIA A10"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a10"
    assert plan["qwen3_gpu_tier"] == "a10-24gb-pcie"
    assert plan["source_archive_sha256"] == SOURCE_ARCHIVE_SHA256
    assert plan["source_archive_bytes"] == 1_024
    assert sum(plan["phase_budget_seconds"].values()) == 2_078
    assert plan["managed_capability_profile"] == remote._QWEN3_PROFILE_ID
    watchdog_index = plan["steps"].index(
        "arm and validate the independent Lambda termination watchdog"
    )
    guarded_source_index = plan["steps"].index(
        "rebuild the checked source archive under watchdog protection"
    )
    termination_index = plan["steps"].index(
        "terminate and confirm the Lambda instance through its API"
    )
    verification_index = plan["steps"].index(
        "independently recalculate and verify every retrieved proof artifact"
    )
    assert watchdog_index < guarded_source_index
    assert termination_index < verification_index


def test_qwen3_a100_dry_run_binds_runtime_instance_type_and_exact_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a100_api_runtime",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA A100-PCIE-40GB"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a100_api_runtime"
    assert plan["qwen3_gpu_tier"] == "a100-40gb-pcie"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "1.99"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "1.25"


def test_qwen3_a100_sxm4_dry_run_binds_extension_instance_and_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 23, 17, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a100_sxm4",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-sxm4",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA A100-SXM4-40GB"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a100_sxm4"
    assert plan["qwen3_gpu_tier"] == "a100-40gb-sxm4"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "1.99"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "1.25"


def test_qwen3_h100_dry_run_binds_runtime_instance_type_and_exact_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("3.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_h100_api_runtime",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("2.25"),
        qwen3_gpu_tier="h100-80gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA H100 PCIe"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_h100_api_runtime"
    assert plan["qwen3_gpu_tier"] == "h100-80gb-pcie"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "3.29"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "2.25"


def test_live_cost_window_clamps_remote_work_before_termination() -> None:
    now = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    deadline = datetime(2026, 8, 20, 20, 20, tzinfo=UTC)

    assert (
        remote._effective_remote_timeout(
            1_500,
            termination_deadline=deadline,
            now=now,
        )
        == 902
    )
    with pytest.raises(remote.RemoteCaptureError, match="less than 300"):
        remote._effective_remote_timeout(
            1_500,
            termination_deadline=datetime(2026, 8, 20, 20, 5, tzinfo=UTC),
            now=now,
        )
    transfer_deadline = remote._transfer_deadline(
        termination_deadline=deadline,
        now=now,
        monotonic=100.0,
    )
    assert transfer_deadline == 1_277.0
    assert (
        remote._remaining_transfer_timeout(
            transfer_deadline,
            phase_limit_seconds=180,
            monotonic=1_150.0,
        )
        == 127
    )


def test_qwen3_fake_ssh_retrieval_stops_at_checksum_before_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_bytes = b"bounded fake archive bytes"
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    calls: list[str] = []

    def fake_run(
        arguments: object,
        *,
        label: str,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(arguments)  # type: ignore[arg-type]
        calls.append(label)
        if label == "remote GPU preflight":
            known_hosts_option = next(
                item for item in argv if item.startswith("UserKnownHostsFile=")
            )
            Path(known_hosts_option.split("=", maxsplit=1)[1]).write_text(
                "gpu.example.test ssh-ed25519 AAAATEST\n",
                encoding="utf-8",
            )
            assert "NVIDIA A10" in argv[-1]
        elif label == "remote proof pack":
            assert remote._QWEN3_PROFILE_ID in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    def download_metadata(
        _arguments: object,
        destination: Path,
        **kwargs: object,
    ) -> None:
        calls.append("bounded metadata retrieval")
        assert kwargs["minimum_size"] == 2
        assert kwargs["maximum_size"] == 4_096
        _write_json(
            destination,
            {
                "archive_name": "capture.tar.gz",
                "archive_sha256": f"sha256:{archive_digest}",
                "schema_version": "inferdrome.qwen3-transfer-metadata.v1",
                "size_bytes": len(archive_bytes),
            },
        )

    def download_archive(
        _arguments: object,
        destination: Path,
        **kwargs: object,
    ) -> None:
        calls.append("bounded archive retrieval")
        assert kwargs["expected_size"] == len(archive_bytes)
        assert kwargs["expected_sha256"] == f"sha256:{archive_digest}"
        destination.write_bytes(archive_bytes)

    monkeypatch.setattr(remote, "_run", fake_run)
    monkeypatch.setattr(remote, "_download_bounded_remote_file", download_metadata)
    monkeypatch.setattr(remote, "_download_exact_remote_file", download_archive)
    monkeypatch.setattr(remote.shutil, "which", lambda executable: executable)
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        host_key_sha256=None,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        output_root=str(tmp_path / "retrieved"),
        port=22,
        qwen3_gpu_tier="a10-24gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )
    source_archive = tmp_path / "repo.tar"
    source_archive.write_bytes(b"exact source")
    deadline = datetime(2099, 8, 20, 20, 20, tzinfo=UTC)

    result = remote._capture_over_ssh(
        args,
        COMMIT,
        None,
        source_archive,
        SOURCE_ARCHIVE_SHA256,
        termination_deadline=deadline,
    )

    assert (result / "capture.tar.gz").read_bytes() == archive_bytes
    assert not (result / "capture").exists()
    receipt = json.loads((result / "retrieval-receipt.json").read_text())
    assert receipt["gpu_tier_id"] == "a10-24gb-pcie"
    assert receipt["lambda_instance_type_name"] == "gpu_1x_a10"
    assert receipt["schema_version"] == "inferdrome.qwen3-gpu-retrieval.v2"
    assert receipt["semantic_verification"] == (
        "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
    )
    assert calls[-2:] == [
        "bounded metadata retrieval",
        "bounded archive retrieval",
    ]


def test_bounded_remote_download_discards_overflow(tmp_path: Path) -> None:
    destination = tmp_path / "bounded.bin"

    with pytest.raises(remote.RemoteCaptureError, match="maximum byte count"):
        remote._download_bounded_remote_file(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4097)"],
            destination,
            minimum_size=2,
            maximum_size=4_096,
            timeout=5,
        )

    assert not destination.exists()


def test_qwen3_controller_orders_capture_termination_then_offline_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    deadline = datetime(2026, 8, 20, 20, 20, tzinfo=UTC)
    watchdog = SimpleNamespace(
        cost_window=SimpleNamespace(deadline=deadline),
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    termination = SimpleNamespace(final_status="absent")
    captured = tmp_path / "capture-record"

    def create_source(path: Path, commit: str) -> tuple[str, int]:
        events.append("source-tree")
        assert commit == COMMIT
        path.write_bytes(b"exact tree")
        return SOURCE_ARCHIVE_SHA256, len(b"exact tree")

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)

    def capture_over_ssh(*_args: object, **kwargs: object) -> Path:
        events.append("capture-checksum")
        assert kwargs["termination_deadline"] == deadline
        return captured

    def terminate(handle: object) -> object:
        events.append("terminate")
        assert handle is watchdog
        return termination

    def finalize(*_args: object, **_kwargs: object) -> Path:
        events.append("offline-verify")
        return captured

    monkeypatch.setattr(remote, "_capture_over_ssh", capture_over_ssh)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    monkeypatch.setattr(remote, "_finalize_qwen3_capture", finalize)
    args = SimpleNamespace(managed_capability_profile=remote._QWEN3_PROFILE_ID)

    assert remote._capture(args, COMMIT, None) == captured
    assert events == [
        "arm",
        "source-tree",
        "capture-checksum",
        "terminate",
        "offline-verify",
    ]


def test_live_source_failure_still_terminates_the_guarded_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
    )

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    def fail_source(_path: Path, _commit: str) -> tuple[str, int]:
        events.append("source-failed")
        raise remote.RemoteCaptureError("tracked secret rejected")

    def terminate(handle: object) -> object:
        events.append("terminate")
        assert handle is watchdog
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)
    monkeypatch.setattr(remote, "_create_source_archive", fail_source)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )

    with pytest.raises(remote.RemoteCaptureError, match="tracked secret rejected"):
        remote._capture(SimpleNamespace(), COMMIT, None)

    assert events == ["arm", "source-failed", "terminate"]


def test_qwen3_finalization_binds_termination_then_publishes_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "retrieved"
    capture_path.mkdir()
    archive = capture_path / "capture.tar.gz"
    archive.write_bytes(b"qwen3 archive")
    archive_sha256 = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    (capture_path / "capture.tar.gz.sha256").write_text(
        f"{archive_sha256.removeprefix('sha256:')}  capture.tar.gz\n",
        encoding="ascii",
    )
    started = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=started,
        deadline=datetime(2026, 8, 20, 20, 29, 53, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 34, 53, tzinfo=UTC),
        allowed_seconds=2_093,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.75"),
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id="b" * 32,
        final_status="absent",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    guard_root.mkdir()
    guard_receipt = guard_root / "termination-receipt.json"
    guard_receipt.write_text(
        json.dumps(
            {
                "cost_window": cost_window.public_record(),
                "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
                "schema_version": "inferdrome.lambda-termination-receipt.v2",
                "termination": termination.public_record(),
                "trigger": "controller-finally",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verification = {
        "capture_manifest_sha256": "sha256:" + "d" * 64,
        "profile_id": remote._QWEN3_PROFILE_ID,
        "repository_commit": COMMIT,
        "run": {"run_id": "run-" + "e" * 32},
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "valid": True,
    }
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": archive_sha256,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v1",
            "semantic_verification": ("PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture_archive",
        lambda *_args, **_kwargs: {
            "archive_sha256": archive_sha256,
            "capture_manifest_sha256": verification["capture_manifest_sha256"],
            "verification": verification,
        },
    )

    def extract(_archive: Path, destination: Path) -> Path:
        extracted = destination / "capture"
        (extracted / "runs").mkdir(parents=True)
        return extracted

    monkeypatch.setattr(remote.real_gpu_capture, "extract_capture_archive", extract)
    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture",
        lambda *_args, **_kwargs: verification,
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.29"),
        instance_type_name="gpu_1x_a10",
    )
    watchdog = SimpleNamespace(
        cost_window=cost_window,
        instance=instance,
        receipt_path=guard_receipt,
    )

    assert (
        remote._finalize_qwen3_capture(
            capture_path,
            commit=COMMIT,
            watchdog=watchdog,
            termination=termination,
        )
        == capture_path
    )
    assert (
        remote._finalize_qwen3_capture(
            capture_path,
            commit=COMMIT,
            watchdog=watchdog,
            termination=termination,
        )
        == capture_path
    )
    semantic = json.loads(
        (capture_path / "semantic-verification.json").read_text(encoding="utf-8")
    )
    assert semantic["semantic_verification"] == ("VALID_AFTER_PROVIDER_TERMINATION")
    assert semantic["provider_termination"] == termination.public_record()
    assert semantic["provider_instance"] == instance.public_record()
    assert (capture_path / "lambda-termination-receipt.json").read_bytes() == (
        guard_receipt.read_bytes()
    )


def test_qwen3_a100_finalization_publishes_tier_bound_v2_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "retrieved"
    capture_path.mkdir()
    archive = capture_path / "capture.tar.gz"
    archive.write_bytes(b"qwen3 a100 archive")
    archive_sha256 = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    (capture_path / "capture.tar.gz.sha256").write_text(
        f"{archive_sha256.removeprefix('sha256:')}  capture.tar.gz\n",
        encoding="ascii",
    )
    instance_type = "gpu_1x_a100_api_runtime"
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": archive_sha256,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "gpu_tier_id": "a100-40gb-pcie",
            "lambda_instance_type_name": instance_type,
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v2",
            "semantic_verification": (
                "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
            ),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        deadline=datetime(2026, 8, 20, 20, 32, 41, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 37, 41, tzinfo=UTC),
        allowed_seconds=2_261,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.99"),
        max_cost_usd=Decimal("1.25"),
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id="b" * 32,
        final_status="absent",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    guard_root.mkdir()
    guard_receipt = guard_root / "termination-receipt.json"
    _write_json(
        guard_receipt,
        {
            "cost_window": cost_window.public_record(),
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-termination-receipt.v2",
            "termination": termination.public_record(),
            "trigger": "controller-finally",
        },
    )
    gpu_target = remote.qwen3_gpu_tier_policy(
        "a100-40gb-pcie"
    ).public_target()
    verification = {
        "capture_manifest_sha256": "sha256:" + "d" * 64,
        "gpu_target": gpu_target,
        "gpu_tier_id": "a100-40gb-pcie",
        "profile_id": remote._QWEN3_PROFILE_ID,
        "repository_commit": COMMIT,
        "run": {"run_id": "run-" + "e" * 32},
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "valid": True,
    }

    def verify_archive(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["expected_gpu_tier_id"] == "a100-40gb-pcie"
        return {
            "archive_sha256": archive_sha256,
            "capture_manifest_sha256": verification["capture_manifest_sha256"],
            "verification": verification,
        }

    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture_archive",
        verify_archive,
    )

    def extract(_archive: Path, destination: Path) -> Path:
        extracted = destination / "capture"
        (extracted / "runs").mkdir(parents=True)
        return extracted

    monkeypatch.setattr(remote.real_gpu_capture, "extract_capture_archive", extract)

    def verify_extracted(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["expected_gpu_tier_id"] == "a100-40gb-pcie"
        return verification

    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture",
        verify_extracted,
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.99"),
        instance_type_name=instance_type,
    )
    watchdog = SimpleNamespace(
        cost_window=cost_window,
        instance=instance,
        receipt_path=guard_receipt,
    )

    assert remote._finalize_qwen3_capture(
        capture_path,
        commit=COMMIT,
        watchdog=watchdog,
        termination=termination,
    ) == capture_path
    semantic = json.loads(
        (capture_path / "semantic-verification.json").read_text(encoding="utf-8")
    )

    assert semantic["schema_version"] == (
        "inferdrome.qwen3-offline-verification.v2"
    )
    assert semantic["gpu_tier_id"] == "a100-40gb-pcie"
    assert semantic["gpu_target"] == gpu_target
    assert semantic["lambda_instance_type_name"] == instance_type


def test_qwen3_offline_resume_uses_retained_guard_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=started,
        deadline=datetime(2026, 8, 20, 20, 29, 53, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 34, 53, tzinfo=UTC),
        allowed_seconds=2_093,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.75"),
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.29"),
        instance_type_name="gpu_1x_a10",
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id=instance.instance_id,
        final_status="terminated",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    _write_json(
        guard_root / "guard-armed.json",
        {
            "cost_window": cost_window.public_record(),
            "instance": instance.public_record(),
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-guard-armed.v2",
            "watchdog_ready": True,
        },
    )
    termination_receipt = {
        "cost_window": cost_window.public_record(),
        "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
        "schema_version": "inferdrome.lambda-termination-receipt.v2",
        "termination": termination.public_record(),
        "trigger": "cost-deadline",
    }
    _write_json(guard_root / "termination-receipt.json", termination_receipt)
    capture_path = tmp_path / "retrieved"
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": "sha256:" + "8" * 64,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v1",
            "semantic_verification": (
                "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
            ),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    observed: dict[str, object] = {}

    def finalize(
        selected: Path,
        *,
        commit: str,
        evidence: remote._TerminationEvidence,
    ) -> Path:
        observed.update(
            {
                "capture_path": selected,
                "commit": commit,
                "cost_window": evidence.cost_window,
                "instance": evidence.instance,
                "receipt": evidence.receipt,
                "termination": evidence.termination,
                "trigger": evidence.trigger,
            }
        )
        return selected

    monkeypatch.setattr(remote, "_finalize_qwen3_capture_with_evidence", finalize)

    assert (
        remote._resume_qwen3_finalization(
            capture_path,
            commit=COMMIT,
            guard_state_directory=guard_root,
        )
        == capture_path.absolute()
    )
    assert observed == {
        "capture_path": capture_path.absolute(),
        "commit": COMMIT,
        "cost_window": cost_window.public_record(),
        "instance": instance.public_record(),
        "receipt": (guard_root / "termination-receipt.json").read_bytes(),
        "termination": termination.public_record(),
        "trigger": "cost-deadline",
    }


def test_remote_preflight_requires_build_tools_and_python_headers() -> None:
    script = remote._remote_preflight_script("/tmp/inferdrome-safe")

    assert "bash curl python3.12 nvidia-smi" in script
    assert "command -v git" not in script
    assert "command -v ninja" not in script
    assert "Python.h" in script
    assert "at least 40 GiB free" not in script
    assert "import ensurepip" in script
    assert "Python 3.12 development headers" in script


def test_a100_remote_preflight_requires_exact_40gb_pcie_name() -> None:
    script = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        expected_gpu_model="NVIDIA A100-PCIE-40GB",
    )

    assert "NVIDIA A100-PCIE-40GB" in script
    assert "NVIDIA A100-SXM4-40GB" not in script
    assert "NVIDIA A100-SXM4-80GB" not in script
    assert "at least 40 GiB free" in script


def test_h100_remote_preflight_requires_exact_pcie_product_name() -> None:
    script = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        expected_gpu_model="NVIDIA H100 PCIe",
    )

    assert "NVIDIA H100 PCIe" in script
    assert "NVIDIA H100 80GB HBM3" not in script
    assert "NVIDIA H100 NVL" not in script
    assert "at least 40 GiB free" in script


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


def test_qwen3_guard_terminates_target_when_another_instance_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "b" * 32
    other_id = "c" * 32
    target = SimpleNamespace(
        instance_id=target_id,
        instance_type_name="gpu_1x_a10",
        status="active",
    )
    other = SimpleNamespace(
        instance_id=other_id,
        instance_type_name="gpu_1x_a10",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target, other)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    terminated: list[tuple[object, str]] = []
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda selected, *, trigger: terminated.append((selected, trigger)),
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id=target_id,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    with pytest.raises(remote.RemoteCaptureError, match="target terminated"):
        remote._arm_lambda_watchdog(args)

    assert terminated == [
        (handle, "campaign-single-instance-check-failed"),
    ]


def test_qwen3_guard_terminates_same_price_wrong_instance_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        instance_id="b" * 32,
        instance_type_name="gpu_1x_a6000",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target,)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    terminated: list[object] = []
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda selected, *, trigger: terminated.append((selected, trigger)),
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id=target.instance_id,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    with pytest.raises(remote.RemoteCaptureError, match="target terminated"):
        remote._arm_lambda_watchdog(args)

    assert terminated == [(handle, "campaign-single-instance-check-failed")]


def test_qwen3_a100_guard_accepts_only_the_runtime_bound_instance_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        instance_id="b" * 32,
        instance_type_name="gpu_1x_a100_api_runtime",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target,)),
        cost_window=SimpleNamespace(
            deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)
        ),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id=target.instance_id,
        lambda_instance_type_name=target.instance_type_name,
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-pcie",
    )

    assert remote._arm_lambda_watchdog(args) is handle


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
        lambda handle: (
            observed.append(handle) or SimpleNamespace(final_status="absent")
        ),
    )

    with pytest.raises(remote.RemoteCaptureError, match="proof failed"):
        remote._capture_with_source(
            SimpleNamespace(),
            COMMIT,
            None,
            Path("/unused/repo.tar"),
            SOURCE_ARCHIVE_SHA256,
        )

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
        remote._capture_with_source(
            SimpleNamespace(),
            COMMIT,
            None,
            Path("/unused/repo.tar"),
            SOURCE_ARCHIVE_SHA256,
        )


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
    assert stat.S_IMODE((extracted / "capture-failure.json").stat().st_mode) == 0o444


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
                "capture/single/example/runs/run-" + "a" * 32 + "/bundle",
                0o500,
            ),
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = mode
            retained.addfile(member)
        descriptor = tarfile.TarInfo(
            "capture/single/example/runs/run-" + "a" * 32 + "/bundle/bundle.json"
        )
        descriptor.mode = 0o400
        descriptor.size = 3
        retained.addfile(descriptor, io.BytesIO(b"{}\n"))

    extracted = capture.extract_capture_archive(archive, tmp_path / "retrieved")
    bundle = extracted / "single" / "example" / "runs" / ("run-" + "a" * 32) / "bundle"

    assert stat.S_IMODE(bundle.stat().st_mode) == 0o500
    assert stat.S_IMODE((bundle / "bundle.json").stat().st_mode) == 0o400


def test_capture_archive_verification_uses_isolated_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-manifest.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")
    expected_archive_sha256 = capture.archive_sha256(archive)
    observed: dict[str, Path] = {}

    def verify(
        root: Path,
        *,
        expected_repository_commit: str | None = None,
    ) -> dict[str, object]:
        observed["root"] = root
        assert expected_repository_commit == COMMIT
        assert root.parent.name.startswith("inferdrome-capture-verification-")
        return {"valid": True}

    monkeypatch.setattr(capture, "verify_capture", verify)

    result = capture.verify_capture_archive(
        archive,
        expected_archive_sha256=expected_archive_sha256,
        expected_repository_commit=COMMIT,
    )

    assert result == {
        "archive_sha256": expected_archive_sha256,
        "capture_manifest_sha256": (
            "sha256:ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
        ),
        "verification": {"valid": True},
    }
    assert not observed["root"].exists()


def test_capture_archive_verification_rejects_digest_mismatch(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-manifest.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")

    with pytest.raises(capture.CaptureError, match="SHA-256 verification"):
        capture.verify_capture_archive(
            archive,
            expected_archive_sha256=f"sha256:{'0' * 64}",
            expected_repository_commit=COMMIT,
        )


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
