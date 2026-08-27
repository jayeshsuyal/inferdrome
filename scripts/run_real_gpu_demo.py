#!/usr/bin/env python3
"""Run one managed real-GPU proof or a controlled-comparison proof pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
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

from inferdrome.comparisons import (
    VerifiedComparisonPlan,
    VerifiedComparisonResult,
    verify_comparison_plan,
    verify_comparison_result,
)
from inferdrome.domain.experiment import AttachedVllmTarget, ConcurrentTraffic
from inferdrome.errors import InferdromeError
from inferdrome.gpu_proof import expected_vllm_source_wheel
from inferdrome.resolution import ResolutionResult, resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml"
BASELINE_SOURCE_PATH = (
    REPOSITORY_ROOT / "examples" / "real-gpu-concurrency-2.yaml"
)
CANDIDATE_SOURCE_PATH = (
    REPOSITORY_ROOT / "examples" / "real-gpu-concurrency-4.yaml"
)
HOST_PIN_PATH = REPOSITORY_ROOT / "examples" / "real-gpu" / "host-pin.json"
PRODUCER_PIN_PATH = REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "producer-pin.json"
FAKE_SOURCE_PATH = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
STATIC_RUN_ID = "run-00000000000000000000000000000000"
COMPARISON_REPETITIONS_PER_ARM = 2
COMPARISON_SCHEDULE_SEED = "6a" * 32
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


def _check_real_gpu_source(
    source_path: Path,
    pin: dict[str, str],
    *,
    expected_concurrency: int,
) -> ResolutionResult:
    resolution = resolve_experiment(
        source_path,
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
        or traffic.concurrency != expected_concurrency
    ):
        raise DemoError("real-GPU example and host pin disagree")
    return resolution


def _check_static_assets() -> dict[str, str]:
    pin = _host_pin()
    _check_real_gpu_source(SOURCE_PATH, pin, expected_concurrency=4)
    baseline = _check_real_gpu_source(
        BASELINE_SOURCE_PATH,
        pin,
        expected_concurrency=2,
    )
    candidate = _check_real_gpu_source(
        CANDIDATE_SOURCE_PATH,
        pin,
        expected_concurrency=4,
    )
    baseline_projection = baseline.resolved_spec.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    candidate_projection = candidate.resolved_spec.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    baseline_projection["traffic"]["concurrency"] = 0
    candidate_projection["traffic"]["concurrency"] = 0
    if (
        baseline_projection != candidate_projection
        or baseline.source_spec_digest == candidate.source_spec_digest
        or baseline.execution_fingerprint == candidate.execution_fingerprint
    ):
        raise DemoError(
            "real-GPU comparison arms must differ only by traffic concurrency"
        )
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
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as interruption:
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
        if isinstance(interruption, KeyboardInterrupt):
            raise DemoError(
                f"{name} was interrupted; inspect its saved diagnostics"
            ) from None
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


def _source_export_identity() -> tuple[str, str]:
    marker_path = REPOSITORY_ROOT / ".inferdrome-source-export.json"
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags)
        before = os.fstat(descriptor)
        def identity(metadata: os.stat_result) -> tuple[int, ...]:
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )

        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError
        if not 2 <= before.st_size <= 4_096:
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        path_identity = os.lstat(marker_path)
        if (
            len(content) != before.st_size
            or identity(after) != identity(before)
            or identity(path_identity) != identity(before)
        ):
            raise OSError
        value = _strict_json_bytes(content, label="Inferdrome source export marker")
    except (OSError, DemoError):
        raise DemoError(
            "Inferdrome source export marker is unavailable or unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if set(value) != {
        "repository_commit",
        "schema_version",
        "source_archive_sha256",
        "transport",
    }:
        raise DemoError("source export marker has an unexpected shape")
    repository_commit = value.get("repository_commit")
    source_archive_sha256 = value.get("source_archive_sha256")
    if (
        value.get("schema_version") != "inferdrome.source-tree-export.v1"
        or value.get("transport") != "git-archive-exact-head-tree-v1"
        or not isinstance(repository_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", repository_commit) is None
        or not isinstance(source_archive_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", source_archive_sha256) is None
    ):
        raise DemoError("source export marker identity is invalid")
    return repository_commit, source_archive_sha256


def _git_checkout_present() -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "rev-parse",
                "--is-inside-work-tree",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def _require_clean_prepared_host(
    state_root: Path,
    pin: dict[str, str],
    *,
    expected_source_archive_sha256: str | None = None,
    expected_repository_commit: str | None = None,
) -> tuple[Path, str]:
    if platform.system() != "Linux":
        raise DemoError("the real-GPU demonstration requires Linux")
    exported_source_digest: str | None = None
    if _git_checkout_present():
        if _git_output("status", "--porcelain", "--untracked-files=normal"):
            raise DemoError("the Inferdrome checkout must be clean")
        repository_commit = _git_output("rev-parse", "--verify", "HEAD")
    else:
        repository_commit, exported_source_digest = _source_export_identity()
        if expected_repository_commit is not None and (
            repository_commit != expected_repository_commit
        ):
            raise DemoError("source export commit does not match the controller pin")
        if expected_source_archive_sha256 is not None and (
            exported_source_digest != expected_source_archive_sha256
        ):
            raise DemoError("source export archive does not match the controller pin")
    if expected_source_archive_sha256 is not None and exported_source_digest is None:
        raise DemoError("a prospective exported source identity is required")
    if (
        expected_repository_commit is not None
        and repository_commit != expected_repository_commit
    ):
        raise DemoError(
            "prepared host repository commit does not match the controller pin"
        )
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
    try:
        bundle = Path(str(bundle_text)).resolve(strict=True)
        proof_root = demo_directory.resolve(strict=True)
        bundle.relative_to(proof_root)
    except (OSError, ValueError):
        raise DemoError(
            "run output points outside the demonstration directory"
        ) from None
    if not bundle.is_dir():
        raise DemoError("run output bundle is unavailable")
    return bundle, str(digest), str(run_id)


def _required_text(value: dict[str, Any], key: str, *, label: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise DemoError(f"{label} omits {key}")
    return selected


def _contained_directory(
    value: dict[str, Any],
    key: str,
    *,
    root: Path,
    label: str,
) -> Path:
    try:
        selected = Path(
            _required_text(value, key, label=label)
        ).resolve(strict=True)
        proof_root = root.resolve(strict=True)
        selected.relative_to(proof_root)
    except (OSError, ValueError):
        raise DemoError(f"{label} points outside its proof root") from None
    if not selected.is_dir():
        raise DemoError(f"{label} directory is unavailable")
    return selected


def _comparison_plan_output(
    output: dict[str, Any],
    *,
    comparison_plans_root: Path,
) -> VerifiedComparisonPlan:
    label = "comparison plan output"
    path = _contained_directory(
        output,
        "path",
        root=comparison_plans_root,
        label=label,
    )
    digest = _required_text(output, "comparison_plan_digest", label=label)
    try:
        verified = verify_comparison_plan(
            path,
            expected_comparison_plan_digest=digest,
        )
    except InferdromeError:
        raise DemoError("comparison plan output failed verification") from None
    descriptor = verified.descriptor
    expected_arms = {
        "baseline": {
            "concurrency": descriptor.independent_variable.baseline_value,
            "planned_trial_set_id": (
                descriptor.baseline_arm.planned_trial_set_id
            ),
            "run_ids": list(descriptor.baseline_arm.run_ids),
            "source": str(BASELINE_SOURCE_PATH.absolute()),
        },
        "candidate": {
            "concurrency": descriptor.independent_variable.candidate_value,
            "planned_trial_set_id": (
                descriptor.candidate_arm.planned_trial_set_id
            ),
            "run_ids": list(descriptor.candidate_arm.run_ids),
            "source": str(CANDIDATE_SOURCE_PATH.absolute()),
        },
    }
    expected_schedule = [
        {
            "arm": slot.arm.value,
            "block_index": slot.block_index,
            "run_id": slot.run_id,
            "sequence_index": slot.sequence_index,
        }
        for slot in descriptor.ordered_schedule
    ]
    if (
        output.get("valid") is not True
        or output.get("comparison_plan_id") != descriptor.comparison_plan_id
        or output.get("predeclaration_assurance") != "OPERATOR_ATTESTED"
        or output.get("arms") != expected_arms
        or output.get("schedule") != expected_schedule
    ):
        raise DemoError("comparison plan output disagrees with its artifact")
    return verified


def _comparison_result_output(
    output: dict[str, Any],
    *,
    plan: VerifiedComparisonPlan,
    runs_root: Path,
    trial_sets_root: Path,
    comparison_plans_root: Path,
    comparison_results_root: Path,
    expected_executed_run_ids: tuple[str, ...],
    expected_reused_run_ids: tuple[str, ...],
) -> VerifiedComparisonResult:
    label = "comparison execution output"
    path = _contained_directory(
        output,
        "path",
        root=comparison_results_root,
        label=label,
    )
    digest = _required_text(output, "comparison_result_digest", label=label)
    try:
        verified = verify_comparison_result(
            path,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=comparison_plans_root,
            expected_comparison_result_digest=digest,
        )
    except InferdromeError:
        raise DemoError("comparison execution output failed verification") from None
    descriptor = verified.descriptor
    if (
        output.get("valid") is not True
        or output.get("comparison_result_id") != descriptor.comparison_result_id
        or output.get("comparison_plan_id") != descriptor.comparison_plan_id
        or output.get("comparison_plan_digest") != plan.comparison_plan_digest
        or output.get("status") != descriptor.status.value
        or output.get("planned_run_count")
        != len(plan.descriptor.ordered_schedule)
        or output.get("executed_run_ids") != list(expected_executed_run_ids)
        or output.get("reused_run_ids") != list(expected_reused_run_ids)
        or output.get("baseline_trial_set_id")
        != verified.baseline.descriptor.trial_set_id
        or output.get("baseline_trial_set_digest")
        != verified.baseline.trial_set_digest
        or output.get("candidate_trial_set_id")
        != verified.candidate.descriptor.trial_set_id
        or output.get("candidate_trial_set_digest")
        != verified.candidate.trial_set_digest
    ):
        raise DemoError("comparison execution output disagrees with its artifacts")
    return verified


def _inspected_customer_bundle(
    output: dict[str, Any],
    *,
    run_id: str,
    runs_root: Path,
) -> dict[str, Any]:
    bundle = output.get("bundle")
    if not isinstance(bundle, dict):
        raise DemoError("run inspection omits its bundle")
    path = _contained_directory(
        bundle,
        "path",
        root=runs_root / run_id,
        label="inspected bundle",
    )
    digest = _required_text(bundle, "bundle_digest", label="inspected bundle")
    if (
        output.get("run_id") != run_id
        or output.get("state") != "COMPLETE"
        or output.get("integrity_status") != "VALID"
        or bundle.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
    ):
        raise DemoError("run inspection is not valid customer evidence")
    return {
        "bundle_digest": digest,
        "bundle_path": str(path),
        "environment_completeness": bundle.get("environment_completeness"),
        "evidence_eligibility": bundle.get("evidence_eligibility"),
        "run_id": run_id,
    }


def _corrupt_bundle_copy(bundle: Path, demo_directory: Path) -> Path:
    destination = demo_directory / "corrupted-bundle-copy"
    shutil.copytree(bundle, destination, symlinks=True)
    native_result = destination / "native" / "benchmark-result.json"
    original_mode: int | None = None
    try:
        original_mode = stat.S_IMODE(os.lstat(native_result).st_mode)
        os.chmod(
            native_result,
            original_mode | stat.S_IWUSR,
            follow_symlinks=False,
        )
        with native_result.open("r+b") as stream:
            first_byte = stream.read(1)
            if not first_byte:
                raise DemoError("disposable corruption target is empty")
            stream.seek(0)
            stream.write(b"[" if first_byte != b"[" else b"{")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise DemoError("disposable bundle copy could not be corrupted") from None
    finally:
        if original_mode is not None:
            try:
                os.chmod(native_result, original_mode, follow_symlinks=False)
            except OSError:
                raise DemoError(
                    "disposable corrupted bundle could not be resealed"
                ) from None
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


def _write_comparison_receipt(
    demo_directory: Path,
    *,
    host_preparation_path: Path,
    repository_commit: str,
    plan: VerifiedComparisonPlan,
    result: VerifiedComparisonResult,
    run_evidence: list[dict[str, Any]],
) -> Path:
    schedule = plan.descriptor.ordered_schedule
    verified_run_descriptors = {
        member.verification.run_id: member.verification.descriptor
        for member in (*result.baseline.members, *result.candidate.members)
    }
    if (
        set(verified_run_descriptors)
        != {slot.run_id for slot in schedule}
        or any(
            descriptor.evidence_eligibility.value != "CUSTOMER_ELIGIBLE"
            or descriptor.environment_completeness.value != "COMPLETE"
            for descriptor in verified_run_descriptors.values()
        )
        or len(run_evidence) != len(schedule)
        or any(
            evidence.get("run_id") != slot.run_id
            or evidence.get("arm") != slot.arm.value
            or evidence.get("block_index") != slot.block_index
            or evidence.get("sequence_index") != slot.sequence_index
            or evidence.get("evidence_eligibility") != "CUSTOMER_ELIGIBLE"
            or evidence.get("environment_completeness") != "COMPLETE"
            for evidence, slot in zip(run_evidence, schedule, strict=True)
        )
    ):
        raise DemoError("comparison receipt run evidence is inconsistent")
    host_receipt_bytes = host_preparation_path.read_bytes()
    descriptor = result.descriptor
    receipt = {
        "acceptance_boundary": "PENDING_EXTERNAL_EXITSPEC",
        "all_bundles_customer_eligible": True,
        "comparison_plan": {
            "comparison_plan_digest": plan.comparison_plan_digest,
            "comparison_plan_id": plan.descriptor.comparison_plan_id,
            "path": str(plan.path),
            "predeclaration_assurance": (
                plan.descriptor.predeclaration_assurance
            ),
            "schedule": [
                {
                    "arm": slot.arm.value,
                    "block_index": slot.block_index,
                    "run_id": slot.run_id,
                    "sequence_index": slot.sequence_index,
                }
                for slot in plan.descriptor.ordered_schedule
            ],
        },
        "comparison_result": {
            "comparison_result_digest": result.comparison_result_digest,
            "comparison_result_id": descriptor.comparison_result_id,
            "inference_scope": descriptor.inference_scope,
            "outcomes": [
                outcome.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=False,
                )
                for outcome in descriptor.outcomes
            ],
            "path": str(result.path),
            "status": descriptor.status.value,
            "unsatisfied_controls": [
                control.value for control in descriptor.unsatisfied_controls
            ],
        },
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "host_preparation_sha256": "sha256:"
        + hashlib.sha256(host_receipt_bytes).hexdigest(),
        "repository_commit": repository_commit,
        "resume_reverification_succeeded": True,
        "runs": run_evidence,
        "schema_version": "inferdrome.real-gpu-comparison-demo-receipt.v1",
        "trial_sets": {
            "baseline": {
                "path": str(result.baseline.path),
                "trial_set_digest": result.baseline.trial_set_digest,
                "trial_set_id": result.baseline.descriptor.trial_set_id,
            },
            "candidate": {
                "path": str(result.candidate.path),
                "trial_set_digest": result.candidate.trial_set_digest,
                "trial_set_id": result.candidate.descriptor.trial_set_id,
            },
        },
    }
    path = demo_directory / "comparison-demo-receipt.json"
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


def _execute_comparison(args: argparse.Namespace) -> Path:
    pin = _check_static_assets()
    state_root = Path(args.state_root).absolute()
    model_path, repository_commit = _require_clean_prepared_host(
        state_root,
        pin,
    )
    output_root = Path(args.output_root).absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    demo_directory = Path(
        tempfile.mkdtemp(prefix="real-gpu-comparison-", dir=output_root)
    ).absolute()
    runs_root = demo_directory / "runs"
    trial_sets_root = demo_directory / "trial-sets"
    comparison_plans_root = demo_directory / "comparison-plans"
    comparison_results_root = demo_directory / "comparison-results"

    _run_cli(
        demo_directory,
        "01-validate-baseline",
        ["validate", str(BASELINE_SOURCE_PATH)],
        expect_success=True,
    )
    _run_cli(
        demo_directory,
        "02-validate-candidate",
        ["validate", str(CANDIDATE_SOURCE_PATH)],
        expect_success=True,
    )
    created = _run_cli(
        demo_directory,
        "03-create-plan",
        [
            "comparison-plan",
            "create",
            "--baseline-source",
            str(BASELINE_SOURCE_PATH),
            "--candidate-source",
            str(CANDIDATE_SOURCE_PATH),
            "--title",
            "Managed real-GPU concurrency 2 versus 4",
            "--hypothesis",
            "Increasing concurrency from 2 to 4 changes attempted throughput.",
            "--repetitions",
            str(COMPARISON_REPETITIONS_PER_ARM),
            "--primary-outcome",
            "attempted_request_throughput_per_s:rate",
            "--schedule-seed",
            COMPARISON_SCHEDULE_SEED,
            "--runs-root",
            str(runs_root),
            "--comparison-plans-root",
            str(comparison_plans_root),
        ],
        expect_success=True,
    )
    created_output = _strict_json_bytes(
        created.stdout,
        label="comparison plan output",
    )
    plan = _comparison_plan_output(
        created_output,
        comparison_plans_root=comparison_plans_root,
    )
    retained_plan_digest = demo_directory / "retained-plan-digest.txt"
    retained_plan_digest.write_text(
        plan.comparison_plan_digest + "\n",
        encoding="ascii",
    )
    retained_plan_digest.chmod(0o400)

    verified_plan = _run_cli(
        demo_directory,
        "04-verify-plan",
        [
            "comparison-plan",
            "verify",
            str(plan.path),
            "--expected-digest",
            plan.comparison_plan_digest,
        ],
        expect_success=True,
    )
    verified_plan_output = _strict_json_bytes(
        verified_plan.stdout,
        label="verified comparison plan output",
    )
    if (
        verified_plan_output.get("valid") is not True
        or verified_plan_output.get("comparison_plan_id")
        != plan.descriptor.comparison_plan_id
        or verified_plan_output.get("comparison_plan_digest")
        != plan.comparison_plan_digest
    ):
        raise DemoError("verified comparison plan output disagrees")

    execution_arguments = [
        "comparison-plan",
        "execute",
        str(plan.path),
        "--expected-digest",
        plan.comparison_plan_digest,
        "--baseline-source",
        str(BASELINE_SOURCE_PATH),
        "--candidate-source",
        str(CANDIDATE_SOURCE_PATH),
        "--runs-root",
        str(runs_root),
        "--trial-sets-root",
        str(trial_sets_root),
        "--comparison-results-root",
        str(comparison_results_root),
        "--tokenizer-path",
        str(model_path),
        "--managed-local-vllm",
        "--managed-model-path",
        str(model_path),
        "--managed-gpu-index",
        str(args.gpu_index),
        "--managed-startup-timeout-seconds",
        str(args.startup_timeout_seconds),
    ]
    scheduled_run_ids = tuple(
        slot.run_id for slot in plan.descriptor.ordered_schedule
    )
    outer_timeout = (
        len(scheduled_run_ids)
        * (math.ceil(args.startup_timeout_seconds) + 1020)
        + 600
    )
    executed = _run_cli(
        demo_directory,
        "05-execute-comparison",
        execution_arguments,
        expect_success=True,
        timeout_seconds=outer_timeout,
    )
    executed_output = _strict_json_bytes(
        executed.stdout,
        label="comparison execution output",
    )
    result = _comparison_result_output(
        executed_output,
        plan=plan,
        runs_root=runs_root,
        trial_sets_root=trial_sets_root,
        comparison_plans_root=comparison_plans_root,
        comparison_results_root=comparison_results_root,
        expected_executed_run_ids=scheduled_run_ids,
        expected_reused_run_ids=(),
    )

    run_evidence: list[dict[str, Any]] = []
    step = 6
    for slot in plan.descriptor.ordered_schedule:
        inspected = _run_cli(
            demo_directory,
            f"{step:02d}-inspect-run-{slot.sequence_index + 1}",
            ["inspect", slot.run_id, "--runs-root", str(runs_root)],
            expect_success=True,
        )
        inspected_output = _strict_json_bytes(
            inspected.stdout,
            label=f"run {slot.sequence_index + 1} inspection",
        )
        evidence = _inspected_customer_bundle(
            inspected_output,
            run_id=slot.run_id,
            runs_root=runs_root,
        )
        evidence.update(
            {
                "arm": slot.arm.value,
                "block_index": slot.block_index,
                "sequence_index": slot.sequence_index,
            }
        )
        step += 1
        customer_verified = _run_cli(
            demo_directory,
            f"{step:02d}-verify-run-{slot.sequence_index + 1}",
            [
                "bundle",
                "verify",
                str(evidence["bundle_path"]),
                "--expected-digest",
                str(evidence["bundle_digest"]),
                "--require-customer-eligible",
            ],
            expect_success=True,
        )
        customer_output = _strict_json_bytes(
            customer_verified.stdout,
            label=f"run {slot.sequence_index + 1} customer verification",
        )
        if (
            customer_output.get("valid") is not True
            or customer_output.get("run_id") != slot.run_id
            or customer_output.get("bundle_digest")
            != evidence["bundle_digest"]
            or customer_output.get("evidence_eligibility")
            != "CUSTOMER_ELIGIBLE"
            or customer_output.get("integrity_status") != "VALID"
        ):
            raise DemoError("customer bundle verification output disagrees")
        run_evidence.append(evidence)
        step += 1

    for arm_name, verified_trial_set in (
        ("baseline", result.baseline),
        ("candidate", result.candidate),
    ):
        verified_trial = _run_cli(
            demo_directory,
            f"{step:02d}-verify-{arm_name}-trial-set",
            [
                "trial-set",
                "verify",
                str(verified_trial_set.path),
                "--runs-root",
                str(runs_root),
                "--expected-digest",
                verified_trial_set.trial_set_digest,
            ],
            expect_success=True,
        )
        trial_output = _strict_json_bytes(
            verified_trial.stdout,
            label=f"{arm_name} Trial Set verification",
        )
        if (
            trial_output.get("valid") is not True
            or trial_output.get("trial_set_id")
            != verified_trial_set.descriptor.trial_set_id
            or trial_output.get("trial_set_digest")
            != verified_trial_set.trial_set_digest
            or trial_output.get("member_count")
            != COMPARISON_REPETITIONS_PER_ARM
        ):
            raise DemoError(f"{arm_name} Trial Set verification disagrees")
        step += 1

    verified_result = _run_cli(
        demo_directory,
        f"{step:02d}-verify-comparison-result",
        [
            "comparison-result",
            "verify",
            str(result.path),
            "--runs-root",
            str(runs_root),
            "--trial-sets-root",
            str(trial_sets_root),
            "--comparison-plans-root",
            str(comparison_plans_root),
            "--expected-digest",
            result.comparison_result_digest,
        ],
        expect_success=True,
    )
    verified_result_output = _strict_json_bytes(
        verified_result.stdout,
        label="verified comparison result output",
    )
    if (
        verified_result_output.get("valid") is not True
        or verified_result_output.get("comparison_result_id")
        != result.descriptor.comparison_result_id
        or verified_result_output.get("comparison_result_digest")
        != result.comparison_result_digest
        or verified_result_output.get("status") != result.descriptor.status.value
    ):
        raise DemoError("verified comparison result output disagrees")
    step += 1

    resumed = _run_cli(
        demo_directory,
        f"{step:02d}-resume-reverification",
        execution_arguments,
        expect_success=True,
        timeout_seconds=600,
    )
    resumed_output = _strict_json_bytes(
        resumed.stdout,
        label="resumed comparison execution output",
    )
    resumed_result = _comparison_result_output(
        resumed_output,
        plan=plan,
        runs_root=runs_root,
        trial_sets_root=trial_sets_root,
        comparison_plans_root=comparison_plans_root,
        comparison_results_root=comparison_results_root,
        expected_executed_run_ids=(),
        expected_reused_run_ids=scheduled_run_ids,
    )
    if resumed_result.comparison_result_digest != result.comparison_result_digest:
        raise DemoError("resume changed the comparison result identity")

    return _write_comparison_receipt(
        demo_directory,
        host_preparation_path=state_root / "host-preparation.json",
        repository_commit=repository_commit,
        plan=plan,
        result=result,
        run_evidence=run_evidence,
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate static pins and example inputs without requiring a GPU",
    )
    mode.add_argument(
        "--comparison",
        action="store_true",
        help="run a two-arm controlled comparison and emit its proof receipt",
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
        receipt = _execute_comparison(args) if args.comparison else _execute(args)
    except DemoError as error:
        print(f"real-gpu-demo: {error}", file=sys.stderr)
        return 1
    print(receipt.read_text(encoding="utf-8"), end="")
    print(f"receipt_path={receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
