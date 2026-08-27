"""Structural, binding, and fail-closed policy tests for vLLM Compose."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import inferdrome.vllm_compose as compose_policy
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    _attached_source_yaml,
    qwen3_workload_bytes,
)
from inferdrome.vllm_compose import (
    COMPOSE_CLEANUP_COMMAND,
    COMPOSE_ENGINE_ENDPOINT,
    QWEN3_COMPOSE_BINDING_SCHEMA_VERSION,
    SOURCE_REPOSITORY_URL,
    VLLM_RUNTIME_IMAGE_REFERENCE,
    VLLM_RUNTIME_SCHEMA_VERSION,
    VLLM_VERSION,
    ComposePreflightError,
    require_immutable_image_reference,
    validate_compose_identity,
    validate_gpu_preflight,
    vllm_compose_contract,
    vllm_runtime_contract,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPOSITORY_ROOT / "compose.yaml"
GPU_COMPOSE_PATH = REPOSITORY_ROOT / "compose.gpu.yaml"


def _compose(path: Path = COMPOSE_PATH) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _experiment_dir(root: Path, endpoint: str = COMPOSE_ENGINE_ENDPOINT) -> Path:
    directory = root / "experiment"
    workload = directory / "workloads"
    workload.mkdir(parents=True)
    source = _attached_source_yaml(1).decode("utf-8")
    source = source.replace("http://127.0.0.1:18080", endpoint)
    (directory / "experiment.yaml").write_text(source, encoding="utf-8")
    (workload / "qwen-text-mixed-length-v1.jsonl").write_bytes(qwen3_workload_bytes())
    return directory


def _valid_preflight_kwargs(tmp_path: Path) -> dict[str, object]:
    model = tmp_path / "model"
    model.mkdir()
    experiment = _experiment_dir(tmp_path)
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
        "compose_uid": os.getuid(),
        "compose_gid": os.getgid(),
        "compose_available": True,
    }


def test_default_compose_is_mock_only_and_gpu_free() -> None:
    services = _compose()["services"]
    assert set(services) == {"mock-engine", "synthetic-smoke"}
    assert all(
        "INFERDROME_COMPOSE_UID:?" in service["user"]
        and "INFERDROME_COMPOSE_GID:?" in service["user"]
        for service in services.values()
    )
    assert all("deploy" not in service for service in services.values())
    assert all("ports" not in service for service in services.values())
    assert "vllm" not in str(services).lower()
    assert "Qwen/" not in str(services)
    assert "inferdrome-runner-probe" in str(services["synthetic-smoke"])
    assert "HF_TOKEN" not in COMPOSE_PATH.read_text(encoding="utf-8")


def test_direct_gpu_profile_on_safe_base_has_no_gpu_services() -> None:
    services = _compose()["services"]
    assert not any(
        "gpu" in service.get("profiles", []) for service in services.values()
    )
    assert "vllm-engine" not in services
    assert "vllm-benchmark-runner" not in services


def test_gpu_override_is_separate_gated_and_has_distinct_roles() -> None:
    compose = _compose(GPU_COMPOSE_PATH)
    services = compose["services"]
    assert set(services) == {"vllm-engine", "vllm-benchmark-runner"}
    assert "INFERDROME_GPU_COMPOSE_GATE" in compose["x-inferdrome-gpu-gate"]
    engine = services["vllm-engine"]
    runner = services["vllm-benchmark-runner"]
    assert engine["profiles"] == ["gpu"]
    assert runner["profiles"] == ["gpu"]
    assert ":?" in engine["image"]
    assert ":?" in runner["image"]
    assert runner["entrypoint"] == ["inferdrome"]
    assert runner["command"][0] == "run"
    assert "inferdrome-runner-probe" not in str(runner)
    assert engine["networks"]["inferdrome-internal"]["aliases"] == [
        "vllm-engine.internal"
    ]
    assert engine["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    assert engine["healthcheck"]["retries"] == 60
    assert runner["depends_on"]["vllm-engine"]["condition"] == "service_healthy"
    assert engine["command"][0] == "/models/qwen3-8b"
    assert QWEN3_8B_MODEL_ID in engine["command"]
    assert engine["read_only"] is True
    assert runner["read_only"] is True
    assert "INFERDROME_COMPOSE_UID" in engine["user"]
    assert "INFERDROME_COMPOSE_GID" in engine["user"]
    assert "INFERDROME_COMPOSE_UID" in runner["user"]
    assert "INFERDROME_COMPOSE_GID" in runner["user"]
    assert engine["restart"] == "no"
    assert runner["restart"] == "no"


def test_compose_has_no_public_or_privileged_escape_hatches() -> None:
    for path in (COMPOSE_PATH, GPU_COMPOSE_PATH):
        compose = _compose(path)
        text = path.read_text(encoding="utf-8").lower()
        assert "privileged: true" not in text
        assert "network_mode: host" not in text
        assert "/var/run/docker.sock" not in text
        assert "restart: always" not in text
        assert "restart: unless-stopped" not in text
        for service in compose.get("services", {}).values():
            assert "ports" not in service
            assert service["security_opt"] == ["no-new-privileges:true"]
            assert service["cap_drop"] == ["ALL"]


def test_runtime_contract_is_current_and_model_identity_is_reused() -> None:
    path = REPOSITORY_ROOT / "compose" / "vllm-runtime-contract.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == vllm_runtime_contract()
    assert committed["schema_version"] == VLLM_RUNTIME_SCHEMA_VERSION
    assert committed["engine_version"] == VLLM_VERSION
    assert committed["image"]["reference"] == VLLM_RUNTIME_IMAGE_REFERENCE
    assert committed["model"]["model_id"] == QWEN3_8B_MODEL_ID
    assert committed["model"]["model_revision"] == QWEN3_8B_REVISION
    assert committed["model"]["tokenizer_revision"] == QWEN3_8B_REVISION
    assert committed["source"]["license"] == "Apache-2.0"


def test_compose_contract_is_current_and_cleanup_is_exact() -> None:
    path = REPOSITORY_ROOT / "compose" / "vllm-compose-contract.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == vllm_compose_contract()
    assert committed["engine"]["image_reference"] == VLLM_RUNTIME_IMAGE_REFERENCE
    assert committed["runner"]["proof_identity"] == "immutable_digest_required"
    assert committed["modes"]["mock"]["evidence_eligible"] is False
    assert committed["modes"]["gpu"]["confirmation_required"] is True
    assert committed["compose_binding"]["schema_version"] == (
        QWEN3_COMPOSE_BINDING_SCHEMA_VERSION
    )
    assert committed["compose_binding"]["workload"]["sha256"].startswith("sha256:")
    assert committed["compose_binding"]["traffic"]["measured_requests"] == 96
    assert committed["compose_binding"]["measurement"]["streaming"] is True
    assert committed["cleanup"]["command"] == list(COMPOSE_CLEANUP_COMMAND)


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
    assert f'org.opencontainers.image.source="{SOURCE_REPOSITORY_URL}"' in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "HF_TOKEN" not in dockerfile
    assert not re.search(
        r"(?im)^\s*(?:ARG|ENV)\s+[^\n]*(?:secret|token|password|api[_-]?key)",
        dockerfile,
    )


def test_benchmark_runner_uses_the_frozen_lock_and_cannot_start_server() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.vllm-benchmark-runner").read_text(
        encoding="utf-8"
    )
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "import jsonschema, pydantic, yaml, rfc8785" in dockerfile
    assert 'ENTRYPOINT ["inferdrome"]' in dockerfile
    assert 'ENTRYPOINT ["vllm", "serve"]' not in dockerfile
    assert "separate-vllm-engine-service" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/inferdrome-runtime" in dockerfile
    assert "/build/.venv" not in dockerfile
    assert (
        "COPY --from=inferdrome-dependencies --chown=2000:0 "
        "/opt/inferdrome-runtime /opt/inferdrome-runtime"
    ) in dockerfile
    assert "/opt/inferdrome-runtime/bin/inferdrome --version" in dockerfile


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


def test_gpu_preflight_accepts_exact_binding_with_read_only_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _valid_preflight_kwargs(tmp_path)
    monkeypatch.setattr(
        compose_policy,
        "_require_model_snapshot",
        lambda value: Path(str(value)),
    )
    model = Path(str(kwargs["model_path"]))
    experiment = Path(str(kwargs["experiment_dir"]))
    try:
        model.chmod(0o555)
        experiment.chmod(0o555)
        validate_gpu_preflight(**kwargs)
    finally:
        model.chmod(0o755)
        experiment.chmod(0o755)


@pytest.mark.parametrize(
    "missing",
    ("config.json", "tokenizer.json", "tokenizer_config.json"),
)
def test_model_snapshot_rejects_placeholder_or_missing_files(
    tmp_path: Path,
    missing: str,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    for filename in ("config.json", "tokenizer.json", "tokenizer_config.json"):
        if filename != missing:
            (model / filename).write_text("{}", encoding="utf-8")
    with pytest.raises(ComposePreflightError):
        compose_policy._require_model_snapshot(str(model))


def test_model_snapshot_rejects_extra_corrupt_and_symlink_entries(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    for filename in ("config.json", "tokenizer.json", "tokenizer_config.json"):
        (model / filename).write_text("{}", encoding="utf-8")
    (model / "unexpected.bin").write_bytes(b"extra")
    with pytest.raises(ComposePreflightError):
        compose_policy._require_model_snapshot(str(model))

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "config.json").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ComposePreflightError):
        compose_policy._require_model_snapshot(str(corrupt))

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "config.json").symlink_to(model / "config.json")
    with pytest.raises(ComposePreflightError):
        compose_policy._require_model_snapshot(str(linked))


def test_read_only_input_directories_pass_and_read_only_output_fails(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    try:
        input_dir.chmod(0o555)
        output_dir.chmod(0o555)
        assert (
            compose_policy._require_absolute_directory(
                str(input_dir), "input", writable=False
            )
            == input_dir
        )
        with pytest.raises(ComposePreflightError, match="not writable"):
            compose_policy._require_absolute_directory(
                str(output_dir), "output", writable=True
            )
    finally:
        input_dir.chmod(0o755)
        output_dir.chmod(0o755)


def test_compose_identity_matches_bind_mount_permissions_and_rejects_root(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    experiment = tmp_path / "experiment"
    evidence = tmp_path / "evidence"
    model.mkdir(mode=0o755)
    experiment.mkdir(mode=0o755)
    evidence.mkdir(mode=0o755)
    validate_compose_identity(
        uid=os.getuid(),
        gid=os.getgid(),
        model_path=str(model),
        experiment_dir=str(experiment),
        evidence_dir=str(evidence),
    )
    with pytest.raises(ComposePreflightError):
        validate_compose_identity(
            uid=0,
            gid=os.getgid(),
            evidence_dir=str(evidence),
        )
    evidence.chmod(0o555)
    try:
        with pytest.raises(ComposePreflightError):
            validate_compose_identity(
                uid=os.getuid(),
                gid=os.getgid(),
                evidence_dir=str(evidence),
            )
    finally:
        evidence.chmod(0o755)


@pytest.mark.parametrize("invalid", (0, "0", 65_535, "65535", "not-an-id"))
def test_compose_identity_rejects_root_and_unbounded_components(
    tmp_path: Path,
    invalid: int | str,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(ComposePreflightError) as exc_info:
        validate_compose_identity(
            uid=invalid,
            gid=os.getgid(),
            evidence_dir=str(evidence),
        )
    assert str(invalid) not in str(exc_info.value)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.replace(
            "schema_version: inferdrome.source-experiment.v1",
            "schema_version: totally.wrong",
        ),
        lambda value: value.replace(
            "endpoint: http://vllm-engine.internal:8000",
            "endpoint: https://public.invalid",
        ),
        lambda value: value.replace("model: Qwen/Qwen3-8B", "model: not-qwen"),
        lambda value: value.replace(
            "requested_output_tokens: 128", "requested_output_tokens: 127"
        ),
        lambda value: value.replace("concurrency: 1", "concurrency: 2"),
    ),
)
def test_experiment_binding_rejects_schema_endpoint_and_methodology_drift(
    tmp_path: Path,
    mutation: Any,
) -> None:
    directory = _experiment_dir(tmp_path)
    source_path = directory / "experiment.yaml"
    source_path.write_text(
        mutation(source_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(ComposePreflightError) as exc_info:
        compose_policy._require_experiment_input(str(directory))
    assert "public.invalid" not in str(exc_info.value)
    assert "not-qwen" not in str(exc_info.value)


def test_compose_wrapper_has_explicit_targets_and_truthful_preflight_order() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "run_vllm_compose.sh").read_text(
        encoding="utf-8"
    )
    assert "trap on_exit EXIT" in script
    assert "trap 'exit 130' INT TERM" in script
    assert "--abort-on-container-exit" in script
    assert "--exit-code-from" in script
    assert "down --remove-orphans --volumes" in script
    assert '"$exit_service"' in script
    assert "--compose-available" in script
    assert script.index("docker compose version") < script.index(
        '"${preflight_args[@]}" --compose-available'
    )


def _fake_docker_bin(tmp_path: Path, *, up_status: int = 17) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "printf 'identity=%s:%s\\n' "
        '"$INFERDROME_COMPOSE_UID" "$INFERDROME_COMPOSE_GID" '
        '>> "$INFERDROME_TEST_DOCKER_LOG"\n'
        'printf \'%s\\n\' "$*" >> "$INFERDROME_TEST_DOCKER_LOG"\n'
        'case "$*" in\n'
        '  *\'context inspect\'*) printf \'"%s"\\n\' '
        '"${INFERDROME_TEST_DOCKER_CONTEXT_HOST:-unix:///var/run/docker.sock}"; '
        'exit 0 ;;\n'
        "  *version*) exit 0 ;;\n"
        f"  *' up '*) exit {up_status} ;;\n"
        "  *' down '*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_bin, log_path


def test_compose_wrapper_mock_targets_only_synthetic_runner_and_cleans_up(
    tmp_path: Path,
) -> None:
    fake_bin, log_path = _fake_docker_bin(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["INFERDROME_TEST_DOCKER_LOG"] = str(log_path)
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(tmp_path / "evidence")
    incompatible_python = fake_bin / "python3"
    python_log = tmp_path / "python.log"
    incompatible_python.write_text(
        f"#!/bin/sh\nprintf called > '{python_log}'\nexit 1\n",
        encoding="utf-8",
    )
    incompatible_python.chmod(0o755)
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "mock"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 17
    lines = log_path.read_text(encoding="utf-8").splitlines()
    up_line = next(line for line in lines if " up " in line)
    down_line = next(line for line in lines if " down " in line)
    assert up_line.endswith("--remove-orphans synthetic-smoke")
    assert "vllm-engine" not in up_line
    assert "vllm-benchmark-runner" not in up_line
    assert down_line.endswith("down --remove-orphans --volumes")
    assert f"identity={os.getuid()}:{os.getgid()}" in lines
    assert not python_log.exists()
    assert "Traceback" not in completed.stderr
    assert str(tmp_path) not in completed.stderr


@pytest.mark.parametrize(
    "variable",
    ("INFERDROME_COMPOSE_FILE", "INFERDROME_GPU_COMPOSE_FILE"),
)
def test_compose_wrapper_rejects_compose_file_overrides(
    tmp_path: Path,
    variable: str,
) -> None:
    environment = os.environ.copy()
    environment[variable] = str(tmp_path / "untrusted-compose.yaml")
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "mock"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stderr == (
        "vLLM Compose preflight failed: Compose file overrides are not supported\n"
    )
    assert str(tmp_path) not in completed.stderr


@pytest.mark.parametrize("variable", ("DOCKER_HOST", "DOCKER_CONTEXT"))
def test_compose_wrapper_rejects_remote_docker_overrides(
    tmp_path: Path,
    variable: str,
) -> None:
    fake_bin, log_path = _fake_docker_bin(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment[variable] = "ssh://operator@example.invalid/var/run/docker.sock"
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(tmp_path / "evidence")
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "mock"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stderr == (
        "vLLM Compose preflight failed: "
        "explicit Docker host or context overrides are not supported\n"
    )
    assert not log_path.exists()


def test_compose_wrapper_rejects_remote_active_docker_context(
    tmp_path: Path,
) -> None:
    fake_bin, log_path = _fake_docker_bin(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["INFERDROME_TEST_DOCKER_LOG"] = str(log_path)
    environment["INFERDROME_TEST_DOCKER_CONTEXT_HOST"] = (
        "ssh://operator@example.invalid/var/run/docker.sock"
    )
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(tmp_path / "evidence")
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "mock"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stderr == (
        "vLLM Compose preflight failed: active Docker context is not local\n"
    )
    assert " up " not in log_path.read_text(encoding="utf-8")


def test_compose_wrapper_gpu_targets_only_benchmark_runner_and_cleans_up(
    tmp_path: Path,
) -> None:
    fake_bin, log_path = _fake_docker_bin(tmp_path)
    (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n", encoding="utf-8")
    (fake_bin / "nvidia-smi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python = fake_bin / "python"
    python_log = tmp_path / "python.log"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{python_log}'\nexit 0\n",
        encoding="utf-8",
    )
    for path in (fake_bin / "uname", fake_bin / "nvidia-smi", fake_python):
        path.chmod(0o755)
    model = tmp_path / "model"
    experiment = tmp_path / "experiment"
    evidence = tmp_path / "evidence"
    model.mkdir()
    experiment.mkdir()
    evidence.mkdir()
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["INFERDROME_TEST_DOCKER_LOG"] = str(log_path)
    environment["INFERDROME_PYTHON"] = str(fake_python)
    environment["INFERDROME_VLLM_RUNNER_IMAGE"] = (
        "registry.example.invalid/inferdrome-runner@sha256:" + "a" * 64
    )
    environment["INFERDROME_VLLM_RUNTIME_IMAGE"] = VLLM_RUNTIME_IMAGE_REFERENCE
    environment["INFERDROME_QWEN3_PROFILE_ID"] = QWEN3_8B_PROFILE_ID
    environment["INFERDROME_QWEN3_MODEL_ID"] = QWEN3_8B_MODEL_ID
    environment["INFERDROME_QWEN3_MODEL_REVISION"] = QWEN3_8B_REVISION
    environment["INFERDROME_QWEN3_TOKENIZER_REVISION"] = QWEN3_8B_REVISION
    environment["INFERDROME_QWEN3_MODEL_PATH"] = str(model)
    environment["INFERDROME_EXPERIMENT_DIR"] = str(experiment)
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(evidence)
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "gpu", "--confirm-gpu"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 17
    lines = log_path.read_text(encoding="utf-8").splitlines()
    up_line = next(line for line in lines if " up " in line)
    down_line = next(line for line in lines if " down " in line)
    assert "compose.gpu.yaml --profile gpu up" in up_line
    assert up_line.endswith("--remove-orphans vllm-benchmark-runner")
    assert "mock-engine" not in up_line
    assert "synthetic-smoke" not in up_line
    assert down_line.endswith("down --remove-orphans --volumes")
    assert f"identity={os.getuid()}:{os.getgid()}" in lines
    assert python_log.exists()


def test_compose_wrapper_gpu_rejects_incompatible_python_without_traceback(
    tmp_path: Path,
) -> None:
    fake_bin, log_path = _fake_docker_bin(tmp_path)
    (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n", encoding="utf-8")
    (fake_bin / "nvidia-smi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    incompatible_python = fake_bin / "incompatible-python"
    incompatible_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    for path in (fake_bin / "uname", fake_bin / "nvidia-smi", incompatible_python):
        path.chmod(0o755)
    model = tmp_path / "model"
    experiment = tmp_path / "experiment"
    evidence = tmp_path / "evidence"
    model.mkdir()
    experiment.mkdir()
    evidence.mkdir()
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["INFERDROME_PYTHON"] = str(incompatible_python)
    environment["INFERDROME_VLLM_RUNNER_IMAGE"] = (
        "registry.example.invalid/inferdrome-runner@sha256:" + "a" * 64
    )
    environment["INFERDROME_VLLM_RUNTIME_IMAGE"] = VLLM_RUNTIME_IMAGE_REFERENCE
    environment["INFERDROME_QWEN3_PROFILE_ID"] = QWEN3_8B_PROFILE_ID
    environment["INFERDROME_QWEN3_MODEL_ID"] = QWEN3_8B_MODEL_ID
    environment["INFERDROME_QWEN3_MODEL_REVISION"] = QWEN3_8B_REVISION
    environment["INFERDROME_QWEN3_TOKENIZER_REVISION"] = QWEN3_8B_REVISION
    environment["INFERDROME_QWEN3_MODEL_PATH"] = str(model)
    environment["INFERDROME_EXPERIMENT_DIR"] = str(experiment)
    environment["INFERDROME_COMPOSE_EVIDENCE_DIR"] = str(evidence)
    completed = subprocess.run(
        ["bash", "scripts/run_vllm_compose.sh", "gpu", "--confirm-gpu"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 2
    assert "compatible Inferdrome Python 3.12 interpreter" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert str(incompatible_python) not in completed.stderr
    assert not log_path.exists()


def test_compose_wrapper_documents_gpu_python_resolution_order() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "run_vllm_compose.sh").read_text(
        encoding="utf-8"
    )
    assert script.index("INFERDROME_PYTHON:-") < script.index(
        'repository_python="$repository_root/.venv/bin/python"'
    )
    assert script.index('repository_python="$repository_root/.venv/bin/python"') < (
        script.index("command -v python3")
    )


def test_build_context_defense_in_depth_excludes_host_state() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for allowed in (
        "!Dockerfile.compose-mock",
        "!Dockerfile.vllm-benchmark-runner",
    ):
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
