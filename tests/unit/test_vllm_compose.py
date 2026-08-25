"""Structural and fail-closed policy tests for the vLLM Compose boundary."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
)
from inferdrome.vllm_compose import (
    VLLM_RUNTIME_IMAGE_REFERENCE,
    VLLM_RUNTIME_SCHEMA_VERSION,
    VLLM_VERSION,
    ComposePreflightError,
    require_immutable_image_reference,
    validate_gpu_preflight,
    vllm_compose_contract,
    vllm_runtime_contract,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPOSITORY_ROOT / "compose.yaml"


def _compose() -> dict[str, Any]:
    value = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _valid_preflight_kwargs(tmp_path: Path) -> dict[str, object]:
    model = tmp_path / "model"
    model.mkdir()
    for filename in ("config.json", "tokenizer.json", "tokenizer_config.json"):
        (model / filename).write_text("{}", encoding="utf-8")
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    (experiment / "experiment.yaml").write_text(
        "schema_version: inferdrome.source-experiment.v1\n",
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return {
        "confirmation": "1",
        "platform_name": "Linux",
        "nvidia_available": True,
        "runner_image": "registry.example.invalid/inferdrome-runner@sha256:" + "a" * 64,
        "runtime_image": VLLM_RUNTIME_IMAGE_REFERENCE,
        "model_path": str(model),
        "experiment_dir": str(experiment),
        "evidence_dir": str(evidence),
        "profile_id": QWEN3_8B_PROFILE_ID,
        "model_id": QWEN3_8B_MODEL_ID,
        "model_revision": QWEN3_8B_REVISION,
        "tokenizer_revision": QWEN3_8B_REVISION,
        "compose_available": True,
    }


def test_default_compose_is_synthetic_and_has_no_gpu_or_model_download() -> None:
    services = _compose()["services"]
    default_services = {
        name: service
        for name, service in services.items()
        if "profiles" not in service
    }

    assert set(default_services) == {"mock-engine", "synthetic-smoke"}
    assert all("deploy" not in service for service in default_services.values())
    assert all("ports" not in service for service in default_services.values())
    assert "vllm" not in str(default_services).lower()
    assert "Qwen/" not in str(default_services)
    assert "inferdrome-runner-probe" in str(default_services["synthetic-smoke"])
    assert "evidence_eligible" not in str(default_services)
    assert "HF_TOKEN" not in COMPOSE_PATH.read_text(encoding="utf-8")


def test_gpu_compose_has_distinct_internal_engine_and_benchmark_runner() -> None:
    services = _compose()["services"]
    engine = services["vllm-engine"]
    runner = services["vllm-benchmark-runner"]

    assert engine["profiles"] == ["gpu"]
    assert runner["profiles"] == ["gpu"]
    assert engine["image"].startswith("${INFERDROME_VLLM_RUNTIME_IMAGE:-")
    assert runner["entrypoint"] == ["inferdrome"]
    assert runner["command"][0] == "run"
    assert "inferdrome-runner-probe" not in str(runner)
    assert "127.0.0.1" not in str(runner)
    assert engine["networks"]["inferdrome-internal"]["aliases"] == [
        "vllm-engine.internal"
    ]
    assert engine["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    assert engine["healthcheck"]["retries"] == 60
    assert runner["depends_on"]["vllm-engine"]["condition"] == "service_healthy"
    assert engine["command"][0] == "/models/qwen3-8b"
    assert "--served-model-name" in engine["command"]
    assert QWEN3_8B_MODEL_ID in engine["command"]
    assert "--dtype" in engine["command"]
    assert engine["command"][engine["command"].index("--dtype") + 1] == "bfloat16"
    assert engine["read_only"] is True
    assert runner["read_only"] is True
    assert engine["restart"] == "no"
    assert runner["restart"] == "no"


def test_compose_has_no_public_or_privileged_escape_hatches() -> None:
    compose = _compose()
    text = COMPOSE_PATH.read_text(encoding="utf-8").lower()

    assert "privileged: true" not in text
    assert "network_mode: host" not in text
    assert "/var/run/docker.sock" not in text
    assert "restart: always" not in text
    assert "restart: unless-stopped" not in text
    assert compose["networks"]["inferdrome-internal"]["internal"] is True
    for service in compose["services"].values():
        assert "ports" not in service
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]


def test_runtime_contract_is_current_and_model_identity_is_reused() -> None:
    contract_path = REPOSITORY_ROOT / "compose" / "vllm-runtime-contract.json"
    committed = json.loads(contract_path.read_text(encoding="utf-8"))
    assert committed == vllm_runtime_contract()
    assert committed["schema_version"] == VLLM_RUNTIME_SCHEMA_VERSION
    assert committed["engine_version"] == VLLM_VERSION
    assert committed["image"]["reference"] == VLLM_RUNTIME_IMAGE_REFERENCE
    assert committed["model"]["model_id"] == QWEN3_8B_MODEL_ID
    assert committed["model"]["model_revision"] == QWEN3_8B_REVISION
    assert committed["model"]["tokenizer_revision"] == QWEN3_8B_REVISION
    assert committed["model"]["model_manifest_sha256"].startswith("sha256:")
    assert committed["model"]["snapshot_manifest_sha256"].startswith("sha256:")
    assert committed["source"]["license"] == "Apache-2.0"


def test_compose_contract_is_current_and_proof_runner_is_digest_gated() -> None:
    contract_path = REPOSITORY_ROOT / "compose" / "vllm-compose-contract.json"
    committed = json.loads(contract_path.read_text(encoding="utf-8"))
    assert committed == vllm_compose_contract()
    assert committed["engine"]["image_reference"] == VLLM_RUNTIME_IMAGE_REFERENCE
    assert committed["runner"]["image_reference_env"] == (
        "INFERDROME_VLLM_RUNNER_IMAGE"
    )
    assert committed["runner"]["proof_identity"] == "immutable_digest_required"
    assert committed["modes"]["mock"]["evidence_eligible"] is False
    assert committed["modes"]["gpu"]["confirmation_required"] is True


@pytest.mark.parametrize(
    "filename",
    ("Dockerfile.compose-mock", "Dockerfile.vllm-benchmark-runner"),
)
def test_new_dockerfiles_are_pinned_non_root_and_provenanced(filename: str) -> None:
    dockerfile = (REPOSITORY_ROOT / filename).read_text(encoding="utf-8")
    from_images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    assert from_images
    assert all("@sha256:" in image for image in from_images)
    assert "USER 10001:10001" in dockerfile or "USER 2000:0" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "org.opencontainers.image.source" in dockerfile
    assert "HF_TOKEN" not in dockerfile
    assert not re.search(
        r"(?im)^\s*(?:ARG|ENV)\s+[^\n]*(?:secret|token|password|api[_-]?key)",
        dockerfile,
    )


def test_runner_image_cannot_start_the_serving_engine() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.vllm-benchmark-runner").read_text(
        encoding="utf-8"
    )
    assert 'ENTRYPOINT ["inferdrome"]' in dockerfile
    assert 'ENTRYPOINT ["vllm", "serve"]' not in dockerfile
    assert "separate-vllm-engine-service" in dockerfile


@pytest.mark.parametrize(
    "reference",
    (
        "vllm/vllm-openai:v0.26.0",
        "vllm/vllm-openai:latest",
        "vllm/vllm-openai@sha256:" + "A" * 64,
        "vllm/vllm-openai@sha256:" + "a" * 63,
    ),
)
def test_image_reference_rejects_floating_or_malformed_values(reference: str) -> None:
    with pytest.raises(ComposePreflightError) as exc_info:
        require_immutable_image_reference(reference, "runner")
    assert reference not in str(exc_info.value)


def test_gpu_preflight_accepts_exact_local_contract(tmp_path: Path) -> None:
    validate_gpu_preflight(**_valid_preflight_kwargs(tmp_path))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("confirmation", None),
        ("platform_name", "Darwin"),
        ("nvidia_available", False),
        ("runner_image", "inferdrome/runner:latest"),
        ("runtime_image", "vllm/vllm-openai:latest"),
        ("profile_id", None),
        ("model_id", None),
        ("model_revision", None),
        ("tokenizer_revision", None),
        ("model_path", "/does/not/exist"),
        ("experiment_dir", "/does/not/exist"),
    ),
)
def test_gpu_preflight_rejects_unsafe_or_missing_inputs(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    kwargs = _valid_preflight_kwargs(tmp_path)
    submitted = value if value is not None else "sk-secret-shaped-value"
    kwargs[field] = submitted

    with pytest.raises(ComposePreflightError) as exc_info:
        validate_gpu_preflight(**kwargs)
    assert str(submitted) not in str(exc_info.value)


def test_gpu_preflight_rejects_symlinked_model_and_output_paths(tmp_path: Path) -> None:
    kwargs = _valid_preflight_kwargs(tmp_path)
    model = Path(str(kwargs["model_path"]))
    output = Path(str(kwargs["evidence_dir"]))
    linked_model = tmp_path / "linked-model"
    linked_output = tmp_path / "linked-output"
    linked_model.symlink_to(model, target_is_directory=True)
    linked_output.symlink_to(output, target_is_directory=True)

    for field, value in (
        ("model_path", str(linked_model)),
        ("evidence_dir", str(linked_output)),
    ):
        changed = dict(kwargs)
        changed[field] = value
        with pytest.raises(ComposePreflightError):
            validate_gpu_preflight(**changed)


def test_compose_wrapper_has_one_shot_abort_and_cleanup_traps() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "run_vllm_compose.sh").read_text(
        encoding="utf-8"
    )
    assert "trap on_exit EXIT" in script
    assert "trap 'exit 130' INT TERM" in script
    assert "--abort-on-container-exit" in script
    assert "--exit-code-from" in script
    assert "down --remove-orphans --volumes" in script
    assert "vllm-benchmark-runner" in script


def test_compose_wrapper_runs_cleanup_after_stack_failure(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$INFERDROME_TEST_DOCKER_LOG\"\n"
        "case \"$*\" in\n"
        "  *version*) exit 0 ;;\n"
        "  *' up '*) exit 17 ;;\n"
        "  *' down '*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["INFERDROME_TEST_DOCKER_LOG"] = str(log_path)
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(tmp_path / "evidence")

    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "mock"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 17
    log = log_path.read_text(encoding="utf-8")
    assert "up --abort-on-container-exit" in log
    assert "down --remove-orphans --volumes" in log


def test_build_context_defense_in_depth_excludes_host_state() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for allowed in ("!Dockerfile.compose-mock", "!Dockerfile.vllm-benchmark-runner"):
        assert allowed in dockerignore
    for excluded in (
        ".git/",
        ".venv/",
        "**/.DS_Store",
        "**/.pytest_cache/",
        "**/.mypy_cache/",
        "**/.ruff_cache/",
        "**/*.egg-info/",
        "**/*credentials*",
        "**/*secret*",
        "gpu-proof-retrieved/",
    ):
        assert excluded in dockerignore
