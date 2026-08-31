"""Dashboard-only helpers built on Inferdrome's real sealing path."""

import json
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from inferdrome.execution.orchestrator import RunResult, run_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
FAKE_WORKLOAD = REPOSITORY_ROOT / "examples" / "workloads" / "fake-smoke.jsonl"


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _json_default(value: object) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal | Path):
        return str(value)
    raise TypeError(f"cannot serialize test value of type {type(value)!r}")


@pytest.fixture
def as_public_json() -> Callable[[object], Any]:
    """Normalize a public projection without requiring one concrete model class."""

    def convert(value: object) -> Any:
        return json.loads(json.dumps(value, default=_json_default))

    return convert


@pytest.fixture
def copy_sealed_bundle() -> Iterator[Callable[[Path, Path], Path]]:
    """Copy immutable bundles without hard links and restore cleanup permissions."""

    copied_roots: list[Path] = []

    def copy(source: Path, destination: Path) -> Path:
        copied = Path(shutil.copytree(source, destination, copy_function=shutil.copy2))
        copied_roots.append(copied)
        return copied

    try:
        yield copy
    finally:
        for copied_root in copied_roots:
            _make_tree_writable(copied_root)


@pytest.fixture
def tampered_bundle(
    copy_sealed_bundle: Callable[[Path, Path], Path],
) -> Callable[[Path, Path], Path]:
    """Create an immutable-looking bundle whose measurement hash is invalid."""

    def tamper(source: Path, destination: Path) -> Path:
        copied = copy_sealed_bundle(source, destination)
        measurements = copied / "derived" / "measurements.json"
        measurements.chmod(0o600)
        measurements.write_bytes(measurements.read_bytes() + b" ")
        measurements.chmod(0o400)
        return copied

    return tamper


@pytest.fixture
def run_fake_bundle(tmp_path: Path) -> Iterator[Callable[..., RunResult]]:
    """Run additional deterministic fake bundles for comparison contracts."""

    workspaces: list[Path] = []

    def run(
        runs_root: Path,
        run_id: str,
        *,
        title: str | None = None,
        experiment_id: str | None = None,
        model: str | None = None,
        max_runtime_seconds: int | None = None,
        exitspec_contract_digest: str | None = None,
    ) -> RunResult:
        source_path = FAKE_SOURCE
        if (
            title is not None
            or experiment_id is not None
            or model is not None
            or max_runtime_seconds is not None
            or exitspec_contract_digest is not None
        ):
            source_root = tmp_path / "dashboard-sources" / run_id
            workload_directory = source_root / "workloads"
            workload_directory.mkdir(parents=True)
            shutil.copy2(FAKE_WORKLOAD, workload_directory / FAKE_WORKLOAD.name)
            source_text = FAKE_SOURCE.read_text(encoding="utf-8")
            if title is not None:
                original_title = "Deterministic fake-adapter smoke run"
                assert source_text.count(original_title) == 1
                source_text = source_text.replace(original_title, title)
            if experiment_id is not None:
                original_id = "id: fake-smoke"
                assert source_text.count(original_id) == 1
                source_text = source_text.replace(
                    original_id,
                    f"id: {experiment_id}",
                )
            if model is not None:
                original_model = "inferdrome/fake-model"
                assert source_text.count(original_model) == 1
                source_text = source_text.replace(original_model, model)
            if max_runtime_seconds is not None:
                original_runtime = "max_runtime_seconds: 60"
                assert source_text.count(original_runtime) == 1
                source_text = source_text.replace(
                    original_runtime,
                    f"max_runtime_seconds: {max_runtime_seconds}",
                )
            if exitspec_contract_digest is not None:
                source_text += (
                    "\nlinks:\n  exitspec_contract_digest: "
                    f"{exitspec_contract_digest}\n"
                )
            source_path = source_root / "fake-smoke.yaml"
            source_path.write_text(source_text, encoding="utf-8")

        result = run_experiment(
            source_path,
            runs_root=runs_root,
            run_id=run_id,
        )
        workspaces.append(result.workspace.path)
        return result

    try:
        yield run
    finally:
        for workspace in workspaces:
            _make_tree_writable(workspace)
