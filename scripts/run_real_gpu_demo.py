#!/usr/bin/env python3
"""Run and challenge one managed real-GPU Inferdrome evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inferdrome.domain.experiment import AttachedVllmTarget, ConcurrentTraffic
from inferdrome.gpu_proof import expected_vllm_source_wheel
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml"
HOST_PIN_PATH = REPOSITORY_ROOT / "examples" / "real-gpu" / "host-pin.json"
PRODUCER_PIN_PATH = REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "producer-pin.json"
FAKE_SOURCE_PATH = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
STATIC_RUN_ID = "run-00000000000000000000000000000000"
_MAX_PACKAGE_INVENTORY_BYTES = 2_097_152


class DemoError(RuntimeError):
    """Expected, user-facing real-GPU demonstration failure."""


@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int


def _strict_json_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DemoError(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise DemoError(f"{label} must be a JSON object")
    return value


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError:
        raise DemoError(f"{label} cannot be read") from None
    return _strict_json_bytes(content, label=label)


def _canonical_package_inventory(content: bytes) -> bytes:
    if not content or len(content) > _MAX_PACKAGE_INVENTORY_BYTES:
        raise DemoError("Python package inventory is empty or exceeds its limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise DemoError("Python package inventory is not UTF-8") from None
    lines = text.splitlines()
    if not lines or any(
        not line
        or len(line.encode("utf-8")) > 8_192
        or any(ord(character) < 32 for character in line)
        for line in lines
    ):
        raise DemoError("Python package inventory contains an invalid line")
    if len(lines) != len(set(lines)):
        raise DemoError("Python package inventory contains duplicate lines")
    return ("\n".join(sorted(lines)) + "\n").encode()


def _current_package_inventory() -> bytes:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DemoError("installed Python packages cannot be inspected") from None
    if completed.returncode != 0:
        raise DemoError("installed Python package inspection failed")
    return _canonical_package_inventory(completed.stdout)


def _require_package_environment_unchanged(packages_path: Path) -> None:
    try:
        retained = packages_path.read_bytes()
    except OSError:
        raise DemoError("prepared Python package inventory cannot be read") from None
    canonical = _canonical_package_inventory(retained)
    if retained != canonical:
        raise DemoError("prepared Python package inventory is not canonical")
    if _current_package_inventory() != retained:
        raise DemoError("installed Python packages changed after host preparation")
    try:
        checked = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DemoError(
            "installed Python package consistency cannot be checked"
        ) from None
    if checked.returncode != 0:
        raise DemoError("installed Python package environment is inconsistent")


def _host_pin() -> dict[str, str]:
    value = _read_json(HOST_PIN_PATH, label="real-GPU host pin")
    expected = {
        "model_directory",
        "model_id",
        "pandas_version",
        "revision",
        "schema_version",
        "vllm_version",
    }
    if set(value) != expected:
        raise DemoError("real-GPU host pin has an unexpected shape")
    if value.get("schema_version") != "inferdrome.real-gpu-host-pin.v1":
        raise DemoError("real-GPU host pin version is unsupported")
    for name in expected - {"schema_version"}:
        if not isinstance(value.get(name), str) or not value[name]:
            raise DemoError(f"real-GPU host pin field is invalid: {name}")
    return {name: str(item) for name, item in value.items()}


def _producer_wheel_pin(machine: str, version: str) -> dict[str, str]:
    producer_pin = _read_json(PRODUCER_PIN_PATH, label="vLLM producer pin")
    producer = producer_pin.get("producer")
    distributions = producer_pin.get("distributions")
    if (
        not isinstance(producer, dict)
        or producer.get("version") != version
        or not isinstance(distributions, list)
    ):
        raise DemoError("vLLM producer pin disagrees with the host pin")
    suffix = f"_{machine}.whl"
    matches = [
        item
        for item in distributions
        if isinstance(item, dict)
        and item.get("kind") == "wheel"
        and isinstance(item.get("filename"), str)
        and item["filename"].endswith(suffix)
    ]
    if len(matches) != 1:
        raise DemoError(f"vLLM producer pin lacks one {machine} wheel")
    wheel = matches[0]
    expected = {"filename", "sha256", "url"}
    for name in expected:
        if not isinstance(wheel.get(name), str) or not wheel[name]:
            raise DemoError(f"vLLM producer wheel field is invalid: {name}")
    if (
        len(wheel["sha256"]) != 64
        or not wheel["url"].startswith("https://files.pythonhosted.org/")
    ):
        raise DemoError("vLLM producer wheel identity is invalid")
    return {name: str(wheel[name]) for name in expected}


def _check_static_assets() -> dict[str, str]:
    pin = _host_pin()
    resolution = resolve_experiment(
        SOURCE_PATH,
        run_id=STATIC_RUN_ID,
        strict=True,
    )
    spec = resolution.resolved_spec
    target = spec.target
    traffic = spec.traffic
    if not isinstance(target, AttachedVllmTarget):
        raise DemoError("real-GPU example is not an attached-vLLM experiment")
    if not isinstance(traffic, ConcurrentTraffic):
        raise DemoError("real-GPU example does not use concurrent traffic")
    if (
        target.model != pin["model_id"]
        or target.model_revision != pin["revision"]
        or target.tokenizer_revision != pin["revision"]
        or target.engine_version != pin["vllm_version"]
        or str(target.endpoint).rstrip("/") != "http://127.0.0.1:18080"
        or traffic.measured_requests != 100
        or traffic.warmup_requests != 10
        or traffic.concurrency != 4
    ):
        raise DemoError("real-GPU example and host pin disagree")
    for architecture in ("aarch64", "x86_64"):
        wheel = _producer_wheel_pin(architecture, pin["vllm_version"])
        if expected_vllm_source_wheel(architecture) != (
            wheel["filename"],
            f"sha256:{wheel['sha256']}",
        ):
            raise DemoError("runtime and recorded vLLM wheel pins disagree")
    return pin


def _run_cli(
    demo_directory: Path,
    name: str,
    arguments: list[str],
    *,
    expect_success: bool,
    timeout_seconds: int = 60,
) -> CommandResult:
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "inferdrome", *arguments],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        raise DemoError(f"{name} could not be started") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        (demo_directory / f"{name}.stdout").write_bytes(stdout)
        (demo_directory / f"{name}.stderr").write_bytes(stderr)
        raise DemoError(f"{name} exceeded its outer demo timeout") from None
    (demo_directory / f"{name}.stdout").write_bytes(stdout)
    (demo_directory / f"{name}.stderr").write_bytes(stderr)
    returncode = process.returncode
    if returncode is None:
        raise DemoError(f"{name} did not report an exit status")
    succeeded = returncode == 0
    if succeeded != expect_success:
        expectation = "succeed" if expect_success else "be rejected"
        raise DemoError(
            f"{name} was expected to {expectation}; inspect its saved diagnostics"
        )
    return CommandResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise DemoError("Inferdrome repository identity cannot be inspected") from None
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise DemoError("Inferdrome repository identity is not UTF-8") from None


def _require_clean_prepared_host(
    state_root: Path,
    pin: dict[str, str],
) -> tuple[Path, str]:
    if platform.system() != "Linux":
        raise DemoError("the real-GPU demonstration requires Linux")
    if _git_output("status", "--porcelain", "--untracked-files=normal"):
        raise DemoError("the Inferdrome checkout must be clean")
    repository_commit = _git_output("rev-parse", "--verify", "HEAD")
    preparation = _read_json(
        state_root / "host-preparation.json",
        label="GPU host preparation receipt",
    )
    expected_preparation_fields = {
        "architecture",
        "model_directory",
        "model_id",
        "model_revision",
        "prepared_at",
        "python_packages_sha256",
        "repository_commit",
        "schema_version",
        "vllm_wheel_filename",
        "vllm_wheel_sha256",
    }
    if set(preparation) != expected_preparation_fields:
        raise DemoError("GPU host preparation receipt has an unexpected shape")
    machine = platform.machine()
    wheel = _producer_wheel_pin(machine, pin["vllm_version"])
    packages_path = state_root / "python-packages.txt"
    try:
        packages_sha256 = "sha256:" + hashlib.sha256(
            packages_path.read_bytes()
        ).hexdigest()
    except OSError:
        raise DemoError("prepared Python package inventory cannot be read") from None
    model_path = state_root / "models" / pin["model_directory"]
    if (
        preparation.get("schema_version")
        != "inferdrome.real-gpu-host-preparation.v1"
        or preparation.get("repository_commit") != repository_commit
        or preparation.get("architecture") != machine
        or preparation.get("model_id") != pin["model_id"]
        or preparation.get("model_revision") != pin["revision"]
        or preparation.get("model_directory") != str(model_path)
        or preparation.get("python_packages_sha256") != packages_sha256
        or preparation.get("vllm_wheel_filename") != wheel["filename"]
        or preparation.get("vllm_wheel_sha256")
        != f"sha256:{wheel['sha256']}"
    ):
        raise DemoError("GPU host preparation does not match this checkout")
    expected_python_parent = (state_root / "venv" / "bin").absolute()
    if Path(sys.executable).absolute().parent != expected_python_parent:
        raise DemoError("run the demo with the prepared virtual-environment Python")
    _require_package_environment_unchanged(packages_path)
    try:
        model_stat = os.lstat(model_path)
    except OSError:
        raise DemoError("the pinned local model snapshot is unavailable") from None
    if model_path.is_symlink() or not stat.S_ISDIR(model_stat.st_mode):
        raise DemoError("the pinned local model snapshot must be a real directory")
    return model_path.absolute(), repository_commit


def _bundle_output(
    output: dict[str, Any],
    *,
    demo_directory: Path,
) -> tuple[Path, str, str]:
    bundle_text = output.get("bundle_path")
    digest = output.get("bundle_digest")
    run_id = output.get("run_id")
    identities = (bundle_text, digest, run_id)
    if not all(isinstance(item, str) and item for item in identities):
        raise DemoError("run output omits its bundle identity")
    bundle = Path(str(bundle_text)).absolute()
    try:
        bundle.relative_to(demo_directory.absolute())
    except ValueError:
        raise DemoError(
            "run output points outside the demonstration directory"
        ) from None
    if not bundle.is_dir():
        raise DemoError("run output bundle is unavailable")
    return bundle, str(digest), str(run_id)


def _corrupt_bundle_copy(bundle: Path, demo_directory: Path) -> Path:
    destination = demo_directory / "corrupted-bundle-copy"
    shutil.copytree(bundle, destination, symlinks=True)
    native_result = destination / "native" / "benchmark-result.json"
    try:
        mode = os.lstat(native_result).st_mode
        os.chmod(native_result, mode | stat.S_IWUSR, follow_symlinks=False)
        with native_result.open("ab") as stream:
            stream.write(b"\n")
    except OSError:
        raise DemoError("disposable bundle copy could not be corrupted") from None
    return destination


def _write_receipt(
    demo_directory: Path,
    *,
    host_preparation_path: Path,
    repository_commit: str,
    bundle: Path,
    bundle_digest: str,
    run_id: str,
    real_output: dict[str, Any],
) -> Path:
    host_receipt_bytes = host_preparation_path.read_bytes()
    receipt = {
        "acceptance_boundary": "PENDING_EXTERNAL_EXITSPEC",
        "bundle_digest": bundle_digest,
        "bundle_path": str(bundle),
        "corrupted_artifact_rejected": True,
        "evidence_eligibility": real_output.get("evidence_eligibility"),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "host_preparation_sha256": "sha256:"
        + hashlib.sha256(host_receipt_bytes).hexdigest(),
        "integrity_status": real_output.get("integrity_status"),
        "repository_commit": repository_commit,
        "run_id": run_id,
        "schema_version": "inferdrome.real-gpu-demo-receipt.v1",
        "synthetic_fixture_rejected": True,
    }
    path = demo_directory / "demo-receipt.json"
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _execute(args: argparse.Namespace) -> Path:
    pin = _check_static_assets()
    state_root = Path(args.state_root).absolute()
    model_path, repository_commit = _require_clean_prepared_host(
        state_root,
        pin,
    )
    output_root = Path(args.output_root).absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    demo_directory = Path(
        tempfile.mkdtemp(prefix="real-gpu-", dir=output_root)
    ).absolute()

    _run_cli(
        demo_directory,
        "01-validate",
        ["validate", str(SOURCE_PATH)],
        expect_success=True,
    )
    real_run = _run_cli(
        demo_directory,
        "02-real-run",
        [
            "run",
            str(SOURCE_PATH),
            "--runs-root",
            str(demo_directory / "runs"),
            "--tokenizer-path",
            str(model_path),
            "--managed-local-vllm",
            "--managed-model-path",
            str(model_path),
            "--managed-gpu-index",
            str(args.gpu_index),
            "--managed-startup-timeout-seconds",
            str(args.startup_timeout_seconds),
        ],
        expect_success=True,
        timeout_seconds=math.ceil(args.startup_timeout_seconds) + 1020,
    )
    real_output = _strict_json_bytes(real_run.stdout, label="real run output")
    bundle, bundle_digest, run_id = _bundle_output(
        real_output,
        demo_directory=demo_directory,
    )
    if (
        real_output.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
        or real_output.get("integrity_status") != "VALID"
    ):
        raise DemoError("managed GPU bundle is not valid and customer-eligible")

    _run_cli(
        demo_directory,
        "03-customer-verify",
        [
            "bundle",
            "verify",
            str(bundle),
            "--expected-digest",
            bundle_digest,
            "--require-customer-eligible",
        ],
        expect_success=True,
    )
    _run_cli(
        demo_directory,
        "04-reduce",
        ["reduce", str(bundle), "--expected-digest", bundle_digest],
        expect_success=True,
    )
    _run_cli(
        demo_directory,
        "05-summarize",
        ["summarize", str(bundle), "--expected-digest", bundle_digest],
        expect_success=True,
    )

    corrupted = _corrupt_bundle_copy(bundle, demo_directory)
    corrupted_result = _run_cli(
        demo_directory,
        "06-corrupted-rejection",
        [
            "bundle",
            "verify",
            str(corrupted),
            "--expected-digest",
            bundle_digest,
            "--require-customer-eligible",
        ],
        expect_success=False,
    )
    if b"artifact hash does not match manifest" not in corrupted_result.stderr:
        raise DemoError("corrupted bundle failed for an unexpected reason")

    synthetic_run = _run_cli(
        demo_directory,
        "07-synthetic-run",
        [
            "run",
            str(FAKE_SOURCE_PATH),
            "--runs-root",
            str(demo_directory / "synthetic-runs"),
        ],
        expect_success=True,
    )
    synthetic_output = _strict_json_bytes(
        synthetic_run.stdout,
        label="synthetic run output",
    )
    synthetic_bundle, synthetic_digest, _ = _bundle_output(
        synthetic_output,
        demo_directory=demo_directory,
    )
    synthetic_result = _run_cli(
        demo_directory,
        "08-synthetic-rejection",
        [
            "bundle",
            "verify",
            str(synthetic_bundle),
            "--expected-digest",
            synthetic_digest,
            "--require-customer-eligible",
        ],
        expect_success=False,
    )
    if b"bundle is not customer-eligible" not in synthetic_result.stderr:
        raise DemoError("synthetic bundle failed for an unexpected reason")

    return _write_receipt(
        demo_directory,
        host_preparation_path=state_root / "host-preparation.json",
        repository_commit=repository_commit,
        bundle=bundle,
        bundle_digest=bundle_digest,
        run_id=run_id,
        real_output=real_output,
    )


def _gpu_index(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("GPU index must be an integer")
    index = int(value)
    if index > 255:
        raise argparse.ArgumentTypeError("GPU index must be between 0 and 255")
    return index


def _startup_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("startup timeout must be a number") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
        raise argparse.ArgumentTypeError(
            "startup timeout must be between 1 and 3600 seconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the pinned Inferdrome managed real-GPU demonstration"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate static pins and example inputs without requiring a GPU",
    )
    parser.add_argument(
        "--state-root",
        default=str(REPOSITORY_ROOT / ".inferdrome-gpu"),
        help="directory created by prepare_real_gpu_host.sh",
    )
    parser.add_argument(
        "--output-root",
        default=str(REPOSITORY_ROOT / "gpu-proof-output"),
        help="parent directory for a new disposable proof run",
    )
    parser.add_argument(
        "--gpu-index",
        type=_gpu_index,
        default=0,
        help="physical NVIDIA GPU index",
    )
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_startup_timeout,
        default=900.0,
        help="bounded local vLLM model-load timeout",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.check:
            _check_static_assets()
            print("real-GPU demonstration assets: OK")
            return 0
        receipt = _execute(args)
    except DemoError as error:
        print(f"real-gpu-demo: {error}", file=sys.stderr)
        return 1
    print(receipt.read_text(encoding="utf-8"), end="")
    print(f"receipt_path={receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
