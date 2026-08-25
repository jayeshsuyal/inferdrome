"""Adversarial offline tests for the minimal Kubernetes Job contract."""

from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.kubernetes import (
    GPU_MANIFEST_RELATIVE_PATH,
    KUBERNETES_MAX_MANIFEST_BYTES,
    KUBERNETES_MAX_YAML_TOKENS,
    MOCK_MANIFEST_RELATIVE_PATH,
    KubernetesContractError,
    kubernetes_contract,
    parse_kubernetes_yaml,
    publish_synthetic_output,
    validate_kubernetes_experiment,
    validate_kubernetes_job,
    validate_kubernetes_manifest,
    validate_kubernetes_server_version,
    validate_synthetic_output_directory,
    verify_synthetic_output,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MOCK_MANIFEST = REPOSITORY_ROOT / MOCK_MANIFEST_RELATIVE_PATH
GPU_MANIFEST = REPOSITORY_ROOT / GPU_MANIFEST_RELATIVE_PATH
WRAPPER = REPOSITORY_ROOT / "scripts" / "run_kubernetes_mock_e2e.sh"


def _document(path: Path, profile: str) -> dict[str, object]:
    raw = path.read_bytes()
    value = parse_kubernetes_yaml(raw)
    validate_kubernetes_job(value, profile, template=profile == "gpu")
    return value


def _synthetic_output() -> dict[str, object]:
    return {
        "schema_version": "inferdrome.runner-probe-output.v1",
        "runner_version": "0.1.0.dev0",
        "execution_mode": "synthetic_endpoint_probe",
        "synthetic_only": True,
        "evidence_eligible": False,
        "status": "SUCCEEDED",
        "endpoint_sha256": "sha256:" + "a" * 64,
        "model": "inferdrome/mock-model",
        "request_sha256": "sha256:" + "b" * 64,
        "response_sha256": "sha256:" + "c" * 64,
        "response_status": 200,
        "response_bytes": 64,
    }


def _fake_cluster_bins(root: Path, *, source: Path) -> tuple[Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log_path = root / "commands.log"
    kind = bin_dir / "kind"
    kind.write_text(
        "#!/bin/sh\n"
        "printf 'kind %s kubeconfig=%s\\n' \"$*\" \"$KUBECONFIG\" "
        ">> \"$INFERDROME_K8S_TEST_LOG\"\n"
        "if [ \"$1\" = --kubeconfig ]; then exit 91; fi\n"
        "case \"$1 $2\" in\n"
        "  'create cluster')\n"
        "    for arg in \"$@\"; do\n"
        "      if [ \"$previous\" = --name ]; then "
        "printf '%s\\n' \"$arg\" > \"$INFERDROME_K8S_CLUSTER_MARKER\"; fi\n"
        "      previous=\"$arg\"\n"
        "    done\n"
        "    exit \"${INFERDROME_K8S_FAIL_CREATE:-0}\"\n"
        "    ;;\n"
        "  'load docker-image') exit \"${INFERDROME_K8S_FAIL_LOAD:-0}\" ;;\n"
        "  'get clusters')\n"
        "    if [ \"${INFERDROME_K8S_COLLISION:-0}\" = 1 ] && "
        "[ ! -s \"$INFERDROME_K8S_CLUSTER_MARKER\" ]; then "
        "printf '%s\\n' \"$INFERDROME_K8S_EXPECTED_CLUSTER_NAME\"; "
        "fi\n"
        "    cat \"$INFERDROME_K8S_CLUSTER_MARKER\"; "
        "if [ \"${INFERDROME_K8S_FAIL_GET_AFTER_CREATE:-0}\" = 17 ] && "
        "[ -s \"$INFERDROME_K8S_CLUSTER_MARKER\" ]; then exit 17; fi; "
        "exit \"${INFERDROME_K8S_FAIL_GET:-0}\" ;;\n"
        "  'delete cluster') exit \"${INFERDROME_K8S_FAIL_DELETE_CLUSTER:-0}\" ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        "printf 'kubectl %s kubeconfig=%s\\n' \"$*\" \"$KUBECONFIG\" "
        ">> \"$INFERDROME_K8S_TEST_LOG\"\n"
        "case \"$*\" in\n"
        "  *' version '*) printf '%s\\n' \"$INFERDROME_K8S_SERVER_VERSION_JSON\" "
        "> \"$INFERDROME_K8S_VERSION_CAPTURE\"; "
        "cat \"$INFERDROME_K8S_VERSION_CAPTURE\"; "
        "exit \"${INFERDROME_K8S_FAIL_VERSION:-0}\" ;;\n"
        "  *' create namespace '*) exit \"${INFERDROME_K8S_FAIL_NAMESPACE:-0}\" ;;\n"
        "  *' apply '*) exit \"${INFERDROME_K8S_FAIL_APPLY:-0}\" ;;\n"
        "  *' wait '*)\n"
        "    if [ \"${INFERDROME_K8S_INTERRUPT_WAIT:-0}\" = 1 ]; then sleep 30; fi\n"
        "    exit \"${INFERDROME_K8S_FAIL_WAIT:-0}\"\n"
        "    ;;\n"
        "  *' get pods '*) printf 'inferdrome-benchmark-mock-pod\\n'; exit 0 ;;\n"
        "  *' logs '*) cat \"$INFERDROME_K8S_LOG_SOURCE\"; "
        "exit \"${INFERDROME_K8S_FAIL_LOGS:-0}\" ;;\n"
        "  *' delete namespace '*) "
        "exit \"${INFERDROME_K8S_FAIL_DELETE_NAMESPACE:-0}\" ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    kind.chmod(0o755)
    kubectl.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf 'docker %s\\n' \"$*\" >> \"$INFERDROME_K8S_TEST_LOG\"\n"
        "exit \"${INFERDROME_K8S_FAIL_DOCKER:-0}\"\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    (root / "cluster.marker").write_text("", encoding="ascii")
    source.chmod(0o600)
    log_path.touch(mode=0o600)
    return bin_dir, log_path


def _run_fake_wrapper(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "runner.log"
    source.write_bytes(canonical_json_bytes(_synthetic_output()) + b"\n")
    bin_dir, log_path = _fake_cluster_bins(tmp_path, source=source)
    output = tmp_path / "evidence"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "INFERDROME_ALLOW_LOCAL_KUBERNETES": "1",
            "INFERDROME_KUBERNETES_OUTPUT_DIR": str(output),
            "INFERDROME_KUBERNETES_PYTHON": str(REPOSITORY_ROOT / ".venv/bin/python"),
            "INFERDROME_K8S_LOG_SOURCE": str(source),
            "INFERDROME_K8S_TEST_LOG": str(log_path),
            "INFERDROME_K8S_CLUSTER_MARKER": str(tmp_path / "cluster.marker"),
            "INFERDROME_KUBERNETES_TEST_MODE": "1",
            "INFERDROME_KIND_NODE_IMAGE": "kindest/node:v1.33.1@sha256:" + "a" * 64,
            "INFERDROME_K8S_SERVER_VERSION_JSON":
            '{"clientVersion":{"major":"1","minor":"33"},'
            '"kustomizeVersion":"v5.6.0",'
            '"serverVersion":{"major":"1","minor":"33",'
            '"gitVersion":"v1.33.1"}}',
            "INFERDROME_K8S_VERSION_CAPTURE": str(tmp_path / "version.capture"),
        }
    )
    environment.update(extra)
    return subprocess.run(
        ["bash", str(WRAPPER), "mock", "--confirm-local-cluster"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_checked_in_manifests_and_generated_contract_are_current() -> None:
    mock = validate_kubernetes_manifest(MOCK_MANIFEST, "mock")
    gpu = validate_kubernetes_manifest(GPU_MANIFEST, "gpu", template=True)
    assert mock["kind"] == gpu["kind"] == "Job"
    contract_path = REPOSITORY_ROOT / "kubernetes/inferdrome-kubernetes-contract.json"
    assert json.loads(contract_path.read_text()) == kubernetes_contract()
    assert kubernetes_contract()["kubernetes_min_version"] == "1.33"


def test_yaml_parser_rejects_duplicate_nested_keys_extra_documents_and_bounds() -> None:
    duplicate = b"apiVersion: batch/v1\nmetadata:\n  labels: {}\n  labels: {}\n"
    with pytest.raises(KubernetesContractError):
        parse_kubernetes_yaml(duplicate)
    with pytest.raises(KubernetesContractError):
        parse_kubernetes_yaml(b"apiVersion: batch/v1\n---\nkind: Job\n")
    with pytest.raises(KubernetesContractError):
        parse_kubernetes_yaml(b"x" * (KUBERNETES_MAX_MANIFEST_BYTES + 1))


@pytest.mark.parametrize(
    "raw",
    [
        b"root:\n" + b"  child:\n" * 70 + b"    value: true\n",
        b"root: &anchor\n  value: true\ncopy: *anchor\n",
        b"!custom {value: true}\n",
    ],
)
def test_yaml_parser_rejects_depth_alias_and_custom_tag(raw: bytes) -> None:
    with pytest.raises(KubernetesContractError):
        parse_kubernetes_yaml(raw)


def test_yaml_parser_has_an_explicit_token_work_bound() -> None:
    below = max(1, KUBERNETES_MAX_YAML_TOKENS // 4 - 20)
    below_raw = "".join(f"key_{index}: {index}\n" for index in range(below)).encode()
    assert len(below_raw) < KUBERNETES_MAX_MANIFEST_BYTES
    parse_kubernetes_yaml(below_raw)
    above = KUBERNETES_MAX_YAML_TOKENS // 4 + 100
    above_raw = "".join(f"key_{index}: {index}\n" for index in range(above)).encode()
    with pytest.raises(KubernetesContractError):
        parse_kubernetes_yaml(above_raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe\x00",
        b"apiVersion: batch/v1\n---\nkind: Job\n",
        b"a: &anchor\n  b: *anchor\n",
        b"!unsafe value\n",
        b"x" * (KUBERNETES_MAX_MANIFEST_BYTES + 1),
    ],
)
def test_kubernetes_cli_errors_are_bounded_for_malformed_yaml(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "malformed.yaml"
    path.write_bytes(raw)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    result = subprocess.run(
        [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            "-m",
            "inferdrome.kubernetes",
            "validate",
            "--profile",
            "mock",
            "--manifest",
            str(path),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert str(path) not in result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["spec"]["template"]["spec"].update(
            {"unknownField": True}
        ),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0].update(
            {"restartPolicy": "Never"}
        ),
        lambda value: value["spec"]["template"]["spec"]["containers"].append(
            copy.deepcopy(value["spec"]["template"]["spec"]["containers"][0])
        ),
        lambda value: value["spec"]["template"]["spec"]["initContainers"].clear(),
        lambda value: value["spec"]["template"]["spec"].update(
            {"hostNetwork": True}
        ),
        lambda value: value["spec"]["template"]["spec"]["securityContext"].update(
            {"allowPrivilegeEscalation": False}
        ),
        lambda value: value["spec"]["template"]["spec"]["securityContext"][
            "seccompProfile"
        ].update({"type": "Unconfined"}),
        lambda value: value["spec"]["template"]["spec"]["volumes"][0].update(
            {"hostPath": {"path": "/"}}
        ),
    ],
)
def test_mock_job_rejects_unsafe_shape_mutations(mutation: Any) -> None:
    value = _document(MOCK_MANIFEST, "mock")
    mutation(value)
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_job(value, "mock")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["spec"]["template"]["spec"]["containers"][0].update(
            {"image": "registry.example.invalid/runner:latest"}
        ),
        lambda value: value["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]["limits"].update({"nvidia.com/gpu": "1"}),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "resources"
        ]["limits"].update({"nvidia.com/gpu": "2"}),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "startupProbe"
        ]["httpGet"].update({"host": "0.0.0.0"}),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "args"
        ].__setitem__(5, "Other/Qwen"),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "args"
        ].__setitem__(-1, "error"),
        lambda value: value["spec"]["template"]["spec"]["containers"][0][
            "args"
        ].__setitem__(-1, "wrong-profile"),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "resources"
        ]["limits"].update({"cpu": 8}),
        lambda value: value["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ][0].update({"readOnly": False}),
        lambda value: value["spec"]["template"]["spec"]["initContainers"][0][
            "volumeMounts"
        ][1].update({"readOnly": True}),
        lambda value: value["spec"]["template"]["spec"]["volumes"][3][
            "emptyDir"
        ].update({"sizeLimit": "1Gi"}),
        lambda value: value["spec"]["template"]["spec"]["volumes"][4].update(
            {"emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}}
        ),
        lambda value: value["spec"]["template"]["spec"]["volumes"][2].update(
            {
                "persistentVolumeClaim": {
                    "claimName": "REQUIRED_EVIDENCE_PVC",
                    "readOnly": True,
                }
            }
        ),
    ],
)
def test_gpu_template_rejects_identity_security_and_storage_drift(
    mutation: Any,
) -> None:
    value = _document(GPU_MANIFEST, "gpu")
    mutation(value)
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_job(value, "gpu", template=True)


def test_profiles_cannot_be_cross_substituted() -> None:
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_manifest(MOCK_MANIFEST, "gpu", template=True)
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_manifest(GPU_MANIFEST, "mock")
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_manifest(GPU_MANIFEST, "gpu")


def test_gpu_runner_uses_attached_canonical_command() -> None:
    document = _document(GPU_MANIFEST, "gpu")
    runner = cast(Any, document)["spec"]["template"]["spec"]["containers"][0]
    assert runner["command"] == ["inferdrome"]
    assert "--managed-capability-profile" not in runner["args"]
    result = subprocess.run(
        [
            str(REPOSITORY_ROOT / ".venv/bin/inferdrome"),
            "run",
            "/private/nonexistent/kubernetes-experiment.yaml",
            "--runs-root",
            "/private/nonexistent/kubernetes-runs",
            "--tokenizer-path",
            "/private/nonexistent/tokenizer",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "managed vLLM options require" not in result.stderr


def test_attached_qwen3_experiment_binding_maps_loopback_to_frozen_validation(
    tmp_path: Path,
) -> None:
    campaign_root = tmp_path / "campaigns" / "v1"
    shutil.copytree(REPOSITORY_ROOT / "campaigns/v1", campaign_root)
    experiment = campaign_root / "qwen3-8b-concurrency-1.yaml"
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:18080", "http://127.0.0.1:8000"
        ),
        encoding="utf-8",
    )
    resolved = validate_kubernetes_experiment(experiment)
    assert isinstance(resolved.target, AttachedVllmTarget)
    assert str(resolved.target.endpoint).rstrip("/") == "http://127.0.0.1:8000"
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:8000", "https://public.invalid"
        ),
        encoding="utf-8",
    )
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_experiment(experiment)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b'{"serverVersion":{"major":"1","minor":"33"}}', (1, 33)),
        (b'{"serverVersion":{"major":"1","minor":"34+"}}', (1, 34)),
    ],
)
def test_kubernetes_server_version_accepts_current_bounded_shape(
    raw: bytes, expected: tuple[int, int]
) -> None:
    assert validate_kubernetes_server_version(raw) == expected


def test_kubernetes_server_version_accepts_real_kubectl_metadata() -> None:
    raw = (
        b'{"clientVersion":{"major":"1","minor":"33",'
        b'"gitVersion":"v1.33.1","platform":"darwin/arm64"},'
        b'"kustomizeVersion":"v5.6.0",'
        b'"serverVersion":{"major":"1","minor":"33",'
        b'"gitVersion":"v1.33.1","platform":"linux/amd64"}}'
    )
    assert validate_kubernetes_server_version(raw) == (1, 33)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"serverVersion":{"major":"1","minor":"32"}}',
        b'{"serverVersion":{"major":"one","minor":"33"}}',
        b'{"serverVersion":{"major":"1","minor":"33","minor":"34"}}',
        b'{"serverVersion":{"major":"1","minor":"99"},"extra":true}',
    ],
)
def test_kubernetes_server_version_rejects_old_malformed_and_duplicate(
    raw: bytes,
) -> None:
    with pytest.raises(KubernetesContractError):
        validate_kubernetes_server_version(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"evidence_eligible": True}),
        lambda value: value.update({"synthetic_only": False}),
        lambda value: value.update({"unexpected": "field"}),
        lambda value: value.update({"response_status": 500}),
    ],
)
def test_synthetic_output_is_strictly_ineligible(mutation: Any, tmp_path: Path) -> None:
    value = _synthetic_output()
    mutation(value)
    path = tmp_path / "output.json"
    path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(KubernetesContractError):
        verify_synthetic_output(path)


def test_synthetic_output_rejects_duplicate_keys_and_noncanonical_bytes(
    tmp_path: Path,
) -> None:
    value = _synthetic_output()
    path = tmp_path / "output.json"
    path.write_bytes(
        b'{"schema_version":"inferdrome.runner-probe-output.v1",'
        b'"schema_version":"inferdrome.runner-probe-output.v1"}'
    )
    with pytest.raises(KubernetesContractError):
        verify_synthetic_output(path)
    path.write_bytes(json.dumps(value).encode())
    with pytest.raises(KubernetesContractError):
        verify_synthetic_output(path)


def test_synthetic_publication_is_no_replace_and_readback_verified(
    tmp_path: Path,
) -> None:
    source = tmp_path / "stage"
    destination = tmp_path / "runner-output.json"
    source.write_bytes(canonical_json_bytes(_synthetic_output()) + b"\n")
    publish_synthetic_output(source, destination)
    original = destination.read_bytes()
    with pytest.raises(KubernetesContractError):
        publish_synthetic_output(source, destination)
    assert destination.read_bytes() == original
    destination.unlink()
    destination.symlink_to(tmp_path / "redirect")
    with pytest.raises(KubernetesContractError):
        publish_synthetic_output(source, destination)


def test_fake_kind_wrapper_retrieves_logs_verifies_and_cleans_exact_resources(
    tmp_path: Path,
) -> None:
    result = _run_fake_wrapper(tmp_path)
    assert result.returncode == 0, result.stderr
    artifact = tmp_path / "evidence/runner-output.json"
    assert verify_synthetic_output(artifact)["synthetic_only"] is True
    log = (tmp_path / "commands.log").read_text()
    assert "create cluster" in log
    assert "load docker-image" in log
    assert " apply " in log
    assert " wait " in log
    assert " logs " in log
    assert log.count(" logs ") == 1
    assert " cp " not in log
    assert " exec " not in log
    assert "delete namespace" in log
    assert "delete cluster" in log
    assert not list((tmp_path / "evidence").glob(".runner-output.*"))
    kind_lines = [line for line in log.splitlines() if line.startswith("kind ")]
    assert kind_lines
    assert not any(line.startswith("kind --kubeconfig") for line in kind_lines)
    create_line = next(line for line in kind_lines if "create cluster" in line)
    assert create_line.index("--kubeconfig") > create_line.index("create cluster")
    assert "kubeconfig=" in create_line


@pytest.mark.parametrize(
    "failure_env",
    [
        {"INFERDROME_K8S_FAIL_CREATE": "17"},
        {"INFERDROME_K8S_FAIL_GET_AFTER_CREATE": "17"},
        {"INFERDROME_K8S_FAIL_APPLY": "17"},
        {"INFERDROME_K8S_FAIL_WAIT": "17"},
        {"INFERDROME_K8S_FAIL_LOGS": "17"},
        {"INFERDROME_K8S_FAIL_DELETE_NAMESPACE": "17"},
        {"INFERDROME_K8S_FAIL_DELETE_CLUSTER": "17"},
    ],
)
def test_fake_kind_wrapper_attempts_cleanup_on_failures(
    tmp_path: Path,
    failure_env: dict[str, str],
) -> None:
    result = _run_fake_wrapper(tmp_path, **failure_env)
    log = (tmp_path / "commands.log").read_text()
    if "FAIL_CREATE" in "".join(failure_env) or "FAIL_GET" in "".join(failure_env):
        assert result.returncode == 2
        assert "delete namespace" not in log
        assert "delete cluster" in log
    else:
        assert "delete namespace" in log
        assert "delete cluster" in log
    if "DELETE_" in "".join(failure_env):
        assert result.returncode == 70
    elif "FAIL_CREATE" not in "".join(failure_env):
        assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_fake_wrapper_does_not_delete_preexisting_collision(tmp_path: Path) -> None:
    result = _run_fake_wrapper(tmp_path, INFERDROME_K8S_COLLISION="1")
    assert result.returncode == 2
    log = (tmp_path / "commands.log").read_text()
    assert "kind get clusters" in log
    assert "kind create cluster" not in log
    assert "delete cluster" not in log


def test_fake_partial_create_cleanup_failure_is_distinct(tmp_path: Path) -> None:
    result = _run_fake_wrapper(
        tmp_path,
        INFERDROME_K8S_FAIL_CREATE="17",
        INFERDROME_K8S_FAIL_DELETE_CLUSTER="17",
    )
    assert result.returncode == 70
    log = (tmp_path / "commands.log").read_text()
    assert "kind create cluster" in log
    assert "kind get clusters" in log
    assert "delete cluster" in log


def test_cleanup_failure_cannot_erase_published_log_artifact(tmp_path: Path) -> None:
    result = _run_fake_wrapper(
        tmp_path,
        INFERDROME_K8S_FAIL_DELETE_NAMESPACE="17",
    )
    assert result.returncode == 70
    artifact = tmp_path / "evidence/runner-output.json"
    assert verify_synthetic_output(artifact)["evidence_eligible"] is False
    assert "cleanup was not confirmed" in result.stderr


def test_fake_wrapper_uses_private_kubeconfig_and_preserves_ambient_sentinel(
    tmp_path: Path,
) -> None:
    ambient = tmp_path / "ambient-kubeconfig"
    ambient.write_bytes(b"ambient sentinel")
    result = _run_fake_wrapper(tmp_path, KUBECONFIG=str(ambient))
    assert result.returncode == 0, result.stderr
    assert ambient.read_bytes() == b"ambient sentinel"
    log = (tmp_path / "commands.log").read_text()
    assert f"kubeconfig={ambient}" not in log
    assert "create cluster" in log


@pytest.mark.parametrize(
    "version_json",
    [
        '{"serverVersion":{"major":"1","minor":"32"}}',
        '{"serverVersion":{"major":"one","minor":"33"}}',
    ],
)
def test_fake_wrapper_rejects_old_or_malformed_server_before_namespace(
    tmp_path: Path, version_json: str
) -> None:
    result = _run_fake_wrapper(
        tmp_path,
        INFERDROME_K8S_SERVER_VERSION_JSON=version_json,
    )
    assert result.returncode == 2
    log = (tmp_path / "commands.log").read_text()
    assert " create namespace " not in log
    assert "delete cluster" in log


def test_fake_wrapper_rejects_missing_local_kind_image_before_cluster(
    tmp_path: Path,
) -> None:
    result = _run_fake_wrapper(tmp_path, INFERDROME_K8S_FAIL_DOCKER="17")
    assert result.returncode == 2
    log = (tmp_path / "commands.log").read_text()
    assert "docker image inspect" in log
    assert "kind create cluster" not in log


def test_fake_wrapper_rejects_unsafe_output_directory_before_cluster(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    result = _run_fake_wrapper(
        tmp_path,
        INFERDROME_KUBERNETES_OUTPUT_DIR=str(link / "nested"),
    )
    assert result.returncode == 2
    assert (tmp_path / "commands.log").read_text() == ""


def test_fake_wrapper_rejects_traversal_alias_before_cluster(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    result = _run_fake_wrapper(
        tmp_path,
        INFERDROME_KUBERNETES_OUTPUT_DIR=str(real / ".." / "evidence"),
    )
    assert result.returncode == 2
    assert (tmp_path / "commands.log").read_text() == ""


def test_output_directory_preflight_allows_a_safe_missing_suffix(
    tmp_path: Path,
) -> None:
    target = tmp_path / "nested" / "evidence"
    validate_synthetic_output_directory(target)
    target.parent.mkdir()
    target.mkdir()
    validate_synthetic_output_directory(target)


def test_publication_rejects_symlinked_ancestor_without_writing_through_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(canonical_json_bytes(_synthetic_output()) + b"\n")
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    link = tmp_path / "link"
    link.symlink_to(redirected, target_is_directory=True)
    with pytest.raises(KubernetesContractError):
        publish_synthetic_output(source, link / "nested" / "runner-output.json")
    assert not (redirected / "nested" / "runner-output.json").exists()

    linked_source_root = tmp_path / "source-link"
    linked_source_root.symlink_to(real, target_is_directory=True)
    linked_source = linked_source_root / "nested" / "source"
    linked_source.write_bytes(canonical_json_bytes(_synthetic_output()) + b"\n")
    with pytest.raises(KubernetesContractError):
        verify_synthetic_output(linked_source)


def test_fake_kind_wrapper_interrupt_attempts_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "runner.log"
    source.write_bytes(canonical_json_bytes(_synthetic_output()) + b"\n")
    bin_dir, log_path = _fake_cluster_bins(tmp_path, source=source)
    output = tmp_path / "evidence"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "INFERDROME_ALLOW_LOCAL_KUBERNETES": "1",
            "INFERDROME_KUBERNETES_OUTPUT_DIR": str(output),
            "INFERDROME_KUBERNETES_PYTHON": str(REPOSITORY_ROOT / ".venv/bin/python"),
            "INFERDROME_K8S_LOG_SOURCE": str(source),
            "INFERDROME_K8S_TEST_LOG": str(log_path),
            "INFERDROME_K8S_CLUSTER_MARKER": str(tmp_path / "cluster.marker"),
            "INFERDROME_KIND_NODE_IMAGE": "kindest/node:v1.33.1@sha256:" + "a" * 64,
            "INFERDROME_K8S_INTERRUPT_WAIT": "1",
            "INFERDROME_K8S_SERVER_VERSION_JSON":
            '{"clientVersion":{"major":"1","minor":"33"},'
            '"kustomizeVersion":"v5.6.0",'
            '"serverVersion":{"major":"1","minor":"33",'
            '"gitVersion":"v1.33.1"}}',
            "INFERDROME_K8S_VERSION_CAPTURE": str(tmp_path / "version.capture"),
        }
    )
    process = subprocess.Popen(
        ["bash", str(WRAPPER), "mock", "--confirm-local-cluster"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "create namespace" in log_path.read_text():
            break
        time.sleep(0.05)
    os.killpg(os.getpgid(process.pid), signal.SIGINT)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode in {2, 130}
    log = (tmp_path / "commands.log").read_text()
    assert "delete namespace" in log
    assert "delete cluster" in log
    assert "Traceback" not in stderr
    assert stdout == ""
