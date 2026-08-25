"""Strict, offline Kubernetes Job/native-sidecar contract for PR10.

This module validates checked-in Kubernetes shapes and verifies the bounded
synthetic runner output used by the local mock wrapper.  It never constructs a
Kubernetes client, contacts a cluster, or assigns evidence eligibility.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import AttachedVllmTarget, ExperimentSpec
from inferdrome.errors import AdapterError, ResolutionError, SourceInputError
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    QWEN3_WORKLOAD_ID,
    QWEN3_WORKLOAD_PATH,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
    qwen3_workload_sha256,
    validate_qwen3_campaign_spec,
)
from inferdrome.resolution import resolve_experiment
from inferdrome.runner import RUNNER_OUTPUT_SCHEMA_VERSION
from inferdrome.vllm_compose import (
    VLLM_RUNTIME_IMAGE_REFERENCE,
    VLLM_VERSION,
)

KUBERNETES_CONTRACT_SCHEMA_VERSION: Final = "inferdrome.kubernetes-job.v1"
KUBERNETES_MIN_VERSION: Final = "1.33"
MOCK_MANIFEST_RELATIVE_PATH: Final = "kubernetes/inferdrome-benchmark-mock-job.yaml"
GPU_MANIFEST_RELATIVE_PATH: Final = (
    "kubernetes/inferdrome-benchmark-gpu-job.template.yaml"
)
MOCK_ENGINE_IMAGE: Final = "inferdrome/compose-mock:development"
MOCK_RUNNER_IMAGE: Final = "inferdrome/runner:development"
GPU_RUNNER_IMAGE_PLACEHOLDER: Final = "REQUIRED_IMMUTABLE_RUNNER_IMAGE_DIGEST"
MOCK_JOB_NAME: Final = "inferdrome-benchmark-mock"
GPU_JOB_NAME: Final = "inferdrome-benchmark-gpu"
MOCK_OUTPUT_NAME: Final = "runner-output.json"
KUBERNETES_MAX_MANIFEST_BYTES: Final = 512 * 1024
KUBERNETES_MAX_OUTPUT_BYTES: Final = 64 * 1024
KUBERNETES_MAX_VERSION_BYTES: Final = 16 * 1024
KUBERNETES_MAX_YAML_DEPTH: Final = 64
KUBERNETES_MAX_YAML_TOKENS: Final = 20_000
KUBERNETES_LOOPBACK_ENDPOINT: Final = "http://127.0.0.1:8000"
KUBERNETES_FROZEN_CAMPAIGN_ENDPOINT: Final = "http://127.0.0.1:18080"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_COMPONENT_RE = re.compile(r"^[0-9]{1,3}\+?$")


class KubernetesContractError(ValueError):
    """A bounded Kubernetes contract, output, or publication failure."""


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise yaml.YAMLError("manifest mapping is ambiguous")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _bounded_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    _reject_symlinked_ancestors(path, label)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise KubernetesContractError(f"{label} is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > maximum
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise KubernetesContractError(f"{label} is unavailable")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final = os.fstat(descriptor)
        if final.st_ino != metadata.st_ino or len(raw) != final.st_size:
            raise KubernetesContractError(f"{label} changed during read")
    except KubernetesContractError:
        raise
    except OSError:
        raise KubernetesContractError(f"{label} is unavailable") from None
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise KubernetesContractError(f"{label} exceeds its bound")
    return raw


def _reject_symlinked_ancestors(
    path: Path, label: str, *, allow_missing_leaf: bool = False
) -> None:
    """Require an existing lexical path and every ancestor to be real."""

    if ".." in Path(path).parts:
        raise KubernetesContractError(f"{label} is unavailable")
    selected = Path(os.path.abspath(path))
    current = Path(selected.anchor)
    for index, component in enumerate(selected.parts[1:], start=1):
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(selected.parts) - 1:
                return
            raise KubernetesContractError(f"{label} is unavailable") from None
        except OSError:
            raise KubernetesContractError(f"{label} is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or (
            index < len(selected.parts) - 1 and not stat.S_ISDIR(metadata.st_mode)
        ):
            raise KubernetesContractError(f"{label} is unavailable")


def _preflight_yaml_tokens(raw: bytes) -> None:
    """Reject YAML features and nesting that are unnecessary for these Jobs."""

    starts = (
        yaml.tokens.BlockMappingStartToken,
        yaml.tokens.BlockSequenceStartToken,
        yaml.tokens.FlowMappingStartToken,
        yaml.tokens.FlowSequenceStartToken,
    )
    ends = (
        yaml.tokens.BlockEndToken,
        yaml.tokens.FlowMappingEndToken,
        yaml.tokens.FlowSequenceEndToken,
    )
    depth = 0
    token_count = 0
    try:
        for token in yaml.scan(raw):
            token_count += 1
            if token_count > KUBERNETES_MAX_YAML_TOKENS:
                raise KubernetesContractError("Kubernetes YAML is too complex")
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise KubernetesContractError("Kubernetes YAML feature is unsupported")
            if isinstance(token, starts):
                depth += 1
                if depth > KUBERNETES_MAX_YAML_DEPTH:
                    raise KubernetesContractError("Kubernetes YAML is too deep")
            elif isinstance(token, ends):
                depth = max(0, depth - 1)
    except KubernetesContractError:
        raise
    except (RecursionError, yaml.YAMLError, TypeError, ValueError):
        raise KubernetesContractError("Kubernetes manifest is invalid") from None


def parse_kubernetes_yaml(raw: bytes) -> dict[str, object]:
    """Parse exactly one bounded YAML document with duplicate-key rejection."""

    if len(raw) > KUBERNETES_MAX_MANIFEST_BYTES:
        raise KubernetesContractError("Kubernetes manifest exceeds its bound")
    try:
        _preflight_yaml_tokens(raw)
        documents = list(yaml.load_all(raw, Loader=_UniqueSafeLoader))
    except (
        UnicodeDecodeError,
        RecursionError,
        yaml.YAMLError,
        TypeError,
        ValueError,
    ):
        raise KubernetesContractError("Kubernetes manifest is invalid") from None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise KubernetesContractError("Kubernetes manifest must contain one object")
    return documents[0]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KubernetesContractError(f"Kubernetes {label} is invalid")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise KubernetesContractError(f"Kubernetes {label} is invalid")
    return value


def _string(value: object, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise KubernetesContractError(f"Kubernetes {label} is invalid")
    return value


def _integer(
    value: object, label: str, *, minimum: int = 0, maximum: int = 86_400
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KubernetesContractError(f"Kubernetes {label} is invalid")
    if not minimum <= value <= maximum:
        raise KubernetesContractError(f"Kubernetes {label} is invalid")
    return value


def _expect_keys(
    value: dict[str, object],
    allowed: set[str],
    required: set[str],
    label: str,
) -> None:
    if set(value) - allowed or required - set(value):
        raise KubernetesContractError(f"Kubernetes {label} has unsupported fields")


def _validate_labels(metadata: dict[str, object], profile: str) -> None:
    labels = _mapping(metadata.get("labels"), "metadata labels")
    expected = {
        "app": "inferdrome-benchmark",
        "component": "benchmark-job",
        "inferdrome.io/evidence-eligible": "false",
        "inferdrome.io/profile": profile,
        "inferdrome.io/synthetic-only": "true" if profile == "mock" else "false",
        "inferdrome.io/topology": "colocated-loopback",
    }
    if labels != expected:
        raise KubernetesContractError("Kubernetes metadata labels are not bound")
    for key, value in labels.items():
        if len(key) > 63 or len(str(value)) > 63:
            raise KubernetesContractError("Kubernetes label exceeds its bound")


def _validate_metadata(value: object, profile: str) -> dict[str, object]:
    metadata = _mapping(value, "metadata")
    _expect_keys(
        metadata,
        {"name", "labels", "annotations"},
        {"name", "labels", "annotations"},
        "metadata",
    )
    expected_name = MOCK_JOB_NAME if profile == "mock" else GPU_JOB_NAME
    if metadata.get("name") != expected_name:
        raise KubernetesContractError("Kubernetes Job name is not bound")
    if "namespace" in metadata:
        raise KubernetesContractError(
            "Kubernetes manifest must use a wrapper namespace"
        )
    _validate_labels(metadata, profile)
    annotations = _mapping(metadata.get("annotations", {}), "metadata annotations")
    expected_persistence = (
        "disposable-emptydir-log-retrieval"
        if profile == "mock"
        else "operator-provided-evidence-pvc-required"
    )
    expected = {
        "inferdrome.io/kubernetes-min-version": KUBERNETES_MIN_VERSION,
        "inferdrome.io/evidence-persistence": expected_persistence,
        "inferdrome.io/claim-boundary": "synthetic-only"
        if profile == "mock"
        else "not-executed",
    }
    if annotations != expected:
        raise KubernetesContractError("Kubernetes metadata annotations are not bound")
    return metadata


def _validate_pod_security(value: object, label: str, *, uid: int) -> None:
    security = _mapping(value, label)
    _expect_keys(
        security,
        {
            "runAsNonRoot",
            "runAsUser",
            "runAsGroup",
            "seccompProfile",
        },
        {
            "runAsNonRoot",
            "runAsUser",
            "runAsGroup",
            "seccompProfile",
        },
        label,
    )
    if security.get("runAsNonRoot") is not True:
        raise KubernetesContractError("Kubernetes security must run as non-root")
    if security.get("runAsUser") != uid or security.get("runAsGroup") != uid:
        raise KubernetesContractError("Kubernetes container identity is not bound")
    if security.get("seccompProfile") != {"type": "RuntimeDefault"}:
        raise KubernetesContractError("Kubernetes seccomp profile is not bound")


def _validate_container_security(value: object, label: str, *, uid: int) -> None:
    security = _mapping(value, label)
    _expect_keys(
        security,
        {
            "runAsNonRoot",
            "runAsUser",
            "runAsGroup",
            "seccompProfile",
            "allowPrivilegeEscalation",
            "readOnlyRootFilesystem",
            "capabilities",
        },
        {
            "runAsNonRoot",
            "runAsUser",
            "runAsGroup",
            "seccompProfile",
            "allowPrivilegeEscalation",
            "readOnlyRootFilesystem",
            "capabilities",
        },
        label,
    )
    if security.get("runAsNonRoot") is not True:
        raise KubernetesContractError("Kubernetes security must run as non-root")
    if security.get("runAsUser") != uid or security.get("runAsGroup") != uid:
        raise KubernetesContractError("Kubernetes container identity is not bound")
    if security.get("allowPrivilegeEscalation") is not False:
        raise KubernetesContractError("Kubernetes privilege escalation is forbidden")
    if security.get("readOnlyRootFilesystem") is not True:
        raise KubernetesContractError("Kubernetes root filesystem must be read-only")
    if security.get("capabilities") != {"drop": ["ALL"]}:
        raise KubernetesContractError("Kubernetes capabilities must be dropped")
    if security.get("seccompProfile") != {"type": "RuntimeDefault"}:
        raise KubernetesContractError("Kubernetes seccomp profile is not bound")


def _validate_probe(value: object, label: str) -> None:
    probe = _mapping(value, label)
    _expect_keys(
        probe,
        {
            "httpGet",
            "periodSeconds",
            "timeoutSeconds",
            "failureThreshold",
            "successThreshold",
            "initialDelaySeconds",
        },
        {"httpGet", "periodSeconds", "timeoutSeconds", "failureThreshold"},
        label,
    )
    http_get = _mapping(probe.get("httpGet"), f"{label} httpGet")
    _expect_keys(
        http_get,
        {"path", "port", "host", "scheme"},
        {"path", "port", "host"},
        f"{label} httpGet",
    )
    if http_get != {
        "path": "/health",
        "port": 8000,
        "host": "127.0.0.1",
        "scheme": "HTTP",
    }:
        raise KubernetesContractError("Kubernetes health probe is not loopback-bound")
    _integer(probe.get("periodSeconds"), f"{label} period", minimum=1, maximum=60)
    _integer(probe.get("timeoutSeconds"), f"{label} timeout", minimum=1, maximum=10)
    _integer(
        probe.get("failureThreshold"),
        f"{label} failure threshold",
        minimum=1,
        maximum=120,
    )


def _validate_resources(
    value: object, label: str, *, profile: str, engine: bool
) -> None:
    expected_resources: dict[
        str, dict[bool, dict[str, dict[str, str | int]]]
    ] = {
        "mock": {
            True: {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "250m", "memory": "256Mi"},
            },
            False: {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
        },
        "gpu": {
            True: {
                "requests": {"cpu": 2, "memory": "8Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": 4, "memory": "16Gi", "nvidia.com/gpu": "1"},
            },
            False: {
                "requests": {"cpu": 1, "memory": "2Gi"},
                "limits": {"cpu": 2, "memory": "4Gi"},
            },
        },
    }
    expected = expected_resources[profile][engine]
    resources = _mapping(value, label)
    _expect_keys(resources, {"requests", "limits"}, {"requests", "limits"}, label)
    if resources != expected:
        raise KubernetesContractError("Kubernetes resources are not bound")


def _validate_env(value: object, label: str, *, gpu: bool) -> None:
    entries = _list(value, label)
    allowed = (
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "VLLM_CACHE_ROOT": "/tmp/vllm-cache",
            "TRITON_CACHE_DIR": "/tmp/triton-cache",
        }
        if gpu
        else {}
    )
    seen: set[str] = set()
    for entry in entries:
        item = _mapping(entry, f"{label} entry")
        _expect_keys(item, {"name", "value"}, {"name", "value"}, f"{label} entry")
        name = _string(item["name"], f"{label} name", maximum=128)
        if name in seen or name not in allowed or item["value"] != allowed[name]:
            raise KubernetesContractError("Kubernetes environment is unsupported")
        seen.add(name)
    if gpu and seen != set(allowed):
        raise KubernetesContractError("Kubernetes offline environment is incomplete")


def _validate_mounts(value: object, label: str, expected: dict[str, bool]) -> None:
    mounts = _list(value, label)
    seen: dict[str, bool] = {}
    for mount in mounts:
        item = _mapping(mount, f"{label} entry")
        _expect_keys(
            item,
            {"name", "mountPath", "readOnly"},
            {"name", "mountPath"},
            f"{label} entry",
        )
        name = _string(item["name"], f"{label} name", maximum=64)
        mount_path = _string(item["mountPath"], f"{label} path", maximum=256)
        if name in seen or name not in expected:
            raise KubernetesContractError("Kubernetes volume mount is unsupported")
        expected_path = {
            "model": "/models/qwen3-8b",
            "experiment": "/inputs",
            "evidence": "/evidence",
            "engine-tmp": "/tmp",
            "engine-shm": "/dev/shm",
            "runner-tmp": "/tmp",
        }.get(name)
        if expected_path is not None and mount_path != expected_path:
            raise KubernetesContractError("Kubernetes volume mount path is not bound")
        read_only = bool(item.get("readOnly", False))
        if read_only != expected[name]:
            raise KubernetesContractError(
                "Kubernetes volume mount mutability is unsafe"
            )
        seen[name] = read_only
    if set(seen) != set(expected):
        raise KubernetesContractError("Kubernetes volume mounts are incomplete")


def _validate_volumes(value: object, profile: str) -> None:
    volumes = _list(value, "volumes")
    seen: set[str] = set()
    for volume in volumes:
        item = _mapping(volume, "volume")
        _expect_keys(
            item, {"name", "emptyDir", "persistentVolumeClaim"}, {"name"}, "volume"
        )
        name = _string(item["name"], "volume name", maximum=64)
        if name in seen:
            raise KubernetesContractError("Kubernetes volumes are duplicated")
        seen.add(name)
        if "emptyDir" in item and "persistentVolumeClaim" in item:
            raise KubernetesContractError("Kubernetes volume source is ambiguous")
        if profile == "mock" and name == "evidence":
            empty_dir = _mapping(item.get("emptyDir"), "evidence emptyDir")
            _expect_keys(empty_dir, {"sizeLimit"}, {"sizeLimit"}, "evidence emptyDir")
            if empty_dir["sizeLimit"] != "64Mi":
                raise KubernetesContractError("Kubernetes evidence bound is not fixed")
        elif profile == "gpu" and name in {"model", "experiment", "evidence"}:
            pvc = _mapping(item.get("persistentVolumeClaim"), f"{name} PVC")
            _expect_keys(
                pvc, {"claimName", "readOnly"}, {"claimName", "readOnly"}, f"{name} PVC"
            )
            claim = _string(pvc["claimName"], f"{name} PVC claim", maximum=128)
            expected_claim = {
                "model": "REQUIRED_QWEN3_MODEL_PVC",
                "experiment": "REQUIRED_EXPERIMENT_PVC",
                "evidence": "REQUIRED_EVIDENCE_PVC",
            }[name]
            if claim != expected_claim or pvc["readOnly"] != (name != "evidence"):
                raise KubernetesContractError("Kubernetes PVC boundary is not bound")
        elif profile == "gpu" and name in {"engine-tmp", "engine-shm", "runner-tmp"}:
            empty_dir = _mapping(item.get("emptyDir"), f"{name} emptyDir")
            expected_empty_dir = {
                "engine-tmp": {"sizeLimit": "2Gi"},
                "engine-shm": {"medium": "Memory", "sizeLimit": "8Gi"},
                "runner-tmp": {"sizeLimit": "512Mi"},
            }[name]
            _expect_keys(
                empty_dir,
                set(expected_empty_dir),
                set(expected_empty_dir),
                f"{name} emptyDir",
            )
            if empty_dir != expected_empty_dir:
                raise KubernetesContractError("Kubernetes scratch volume is not bound")
        else:
            raise KubernetesContractError("Kubernetes volume shape is unsupported")
    if (
        seen != {"evidence"}
        if profile == "mock"
        else seen
        != {
            "model",
            "experiment",
            "evidence",
            "engine-tmp",
            "engine-shm",
            "runner-tmp",
        }
    ):
        raise KubernetesContractError("Kubernetes volumes are incomplete")


def _validate_container(
    value: object,
    profile: str,
    *,
    engine: bool,
    template: bool = False,
) -> None:
    container = _mapping(value, "container")
    required_keys = {
        "name",
        "image",
        "command",
        "securityContext",
        "resources",
    }
    if not engine or profile == "gpu":
        required_keys.add("volumeMounts")
    _expect_keys(
        container,
        {
            "name",
            "image",
            "imagePullPolicy",
            "command",
            "args",
            "securityContext",
            "resources",
            "volumeMounts",
            "env",
            "startupProbe",
            "readinessProbe",
            "livenessProbe",
            "restartPolicy",
        },
        required_keys,
        "container",
    )
    name = _string(container["name"], "container name", maximum=63)
    expected_name = (
        ("mock-engine" if profile == "mock" else "vllm-engine")
        if engine
        else ("synthetic-runner" if profile == "mock" else "benchmark-runner")
    )
    if name != expected_name:
        raise KubernetesContractError("Kubernetes container role is not bound")
    image = _string(container["image"], "container image", maximum=320)
    if profile == "mock":
        expected_image = MOCK_ENGINE_IMAGE if engine else MOCK_RUNNER_IMAGE
        if image != expected_image or container.get("imagePullPolicy") != "Never":
            raise KubernetesContractError("Kubernetes mock image is not bound")
    elif engine:
        if image != VLLM_RUNTIME_IMAGE_REFERENCE:
            raise KubernetesContractError("Kubernetes vLLM image is not pinned")
        if container.get("imagePullPolicy") != "IfNotPresent":
            raise KubernetesContractError("Kubernetes GPU image pull policy is unsafe")
    else:
        if image == GPU_RUNNER_IMAGE_PLACEHOLDER:
            if not template:
                raise KubernetesContractError(
                    "Kubernetes GPU runner image must be immutable"
                )
        elif _DIGEST_RE.fullmatch(image.rsplit("@", 1)[-1]) is None:
            raise KubernetesContractError(
                "Kubernetes GPU runner image must be immutable"
            )
        if container.get("imagePullPolicy") != "IfNotPresent":
            raise KubernetesContractError("Kubernetes GPU image pull policy is unsafe")
    command = _list(container["command"], "container command")
    if not command or any(
        not isinstance(item, str) or item in {"sh", "bash", "-c", "--command"}
        for item in command
    ):
        raise KubernetesContractError("Kubernetes command is unsafe")
    args = _list(container.get("args", []), "container args")
    if any(not isinstance(item, str) or len(item) > 512 for item in args):
        raise KubernetesContractError("Kubernetes command argument is invalid")
    if any(item in {"sh", "bash", "-c", "--command"} for item in command + args):
        raise KubernetesContractError("Kubernetes command construction is forbidden")
    if profile == "mock" and engine:
        if command != [
            "python",
            "/opt/inferdrome/compose_mock.py",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]:
            raise KubernetesContractError("Kubernetes mock engine command is not bound")
    elif profile == "mock":
        if command != [
            "/opt/inferdrome-runtime/bin/inferdrome-runner-probe"
        ] or args != [
            "--endpoint",
            "http://127.0.0.1:8000/v1/chat/completions",
            "--model",
            "inferdrome/mock-model",
            "--prompt",
            "deterministic Kubernetes mock smoke",
            "--evidence-dir",
            "/evidence",
        ]:
            raise KubernetesContractError(
                "Kubernetes synthetic runner command is not bound"
            )
    elif engine:
        expected_command = ["vllm", "serve", "/models/qwen3-8b"]
        expected_args = [
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--served-model-name",
            QWEN3_8B_MODEL_ID,
            "--tokenizer",
            "/models/qwen3-8b",
            "--tokenizer-mode",
            "auto",
            "--dtype",
            "bfloat16",
            "--seed",
            "42",
            "--load-format",
            "safetensors",
            "--generation-config",
            "vllm",
            "--model-impl",
            "vllm",
            "--max-model-len",
            "2048",
            "--gpu-memory-utilization",
            "0.90",
            "--tensor-parallel-size",
            "1",
            "--no-enable-log-requests",
            "--disable-uvicorn-access-log",
            "--uvicorn-log-level",
            "warning",
        ]
        if command != expected_command or args != expected_args:
            raise KubernetesContractError("Kubernetes vLLM command is not bound")
    else:
        expected_args = [
            "run",
            "/inputs/experiment.yaml",
            "--runs-root",
            "/evidence/runs",
            "--tokenizer-path",
            "/models/qwen3-8b",
        ]
        if command != ["inferdrome"] or args != expected_args:
            raise KubernetesContractError(
                "Kubernetes benchmark command is not canonical"
            )
    if engine:
        expected_mounts = (
            {"model": True, "engine-tmp": False, "engine-shm": False}
            if profile == "gpu"
            else {}
        )
    else:
        expected_mounts = (
            {"evidence": False}
            if profile == "mock"
            else {
                "model": True,
                "experiment": True,
                "evidence": False,
                "runner-tmp": False,
            }
        )
    if expected_mounts:
        _validate_mounts(
            container["volumeMounts"], "container volume mounts", expected_mounts
        )
    elif "volumeMounts" in container:
        raise KubernetesContractError("Kubernetes engine volume mounts are unsupported")
    _validate_container_security(
        container["securityContext"],
        "container security",
        uid=10001 if profile == "mock" else 2000,
    )
    _validate_resources(
        container["resources"],
        "container resources",
        profile=profile,
        engine=engine,
    )
    if profile == "mock" and "env" in container:
        raise KubernetesContractError("Kubernetes mock environment is unsupported")
    if "env" in container:
        _validate_env(container["env"], "container environment", gpu=profile == "gpu")
    elif profile == "gpu":
        raise KubernetesContractError("Kubernetes GPU environment is incomplete")
    if engine:
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            _validate_probe(container.get(probe_name), probe_name)
    elif any(
        name in container
        for name in ("startupProbe", "readinessProbe", "livenessProbe")
    ):
        raise KubernetesContractError("Kubernetes runner must not expose engine probes")


def validate_kubernetes_job(
    document: dict[str, object], profile: str, *, template: bool = False
) -> None:
    """Validate one checked-in mock Job or GPU template."""

    if profile not in {"mock", "gpu"}:
        raise KubernetesContractError("Kubernetes profile is unsupported")
    _expect_keys(
        document,
        {"apiVersion", "kind", "metadata", "spec"},
        {"apiVersion", "kind", "metadata", "spec"},
        "manifest",
    )
    if document["apiVersion"] != "batch/v1" or document["kind"] != "Job":
        raise KubernetesContractError("Kubernetes manifest must be a batch Job")
    _validate_metadata(document["metadata"], profile)
    spec = _mapping(document["spec"], "Job spec")
    _expect_keys(
        spec,
        {
            "backoffLimit",
            "activeDeadlineSeconds",
            "ttlSecondsAfterFinished",
            "template",
        },
        {
            "backoffLimit",
            "activeDeadlineSeconds",
            "ttlSecondsAfterFinished",
            "template",
        },
        "Job spec",
    )
    if spec["backoffLimit"] != 0:
        raise KubernetesContractError("Kubernetes Job retries must be disabled")
    expected_deadline = 300 if profile == "mock" else 1_800
    expected_ttl = 60 if profile == "mock" else 300
    if spec["activeDeadlineSeconds"] != expected_deadline:
        raise KubernetesContractError("Kubernetes Job active deadline is not bound")
    if spec["ttlSecondsAfterFinished"] != expected_ttl:
        raise KubernetesContractError("Kubernetes Job TTL is not bound")
    pod_template = _mapping(spec["template"], "Pod template")
    _expect_keys(
        pod_template, {"metadata", "spec"}, {"metadata", "spec"}, "Pod template"
    )
    template_metadata = _mapping(pod_template["metadata"], "Pod metadata")
    _expect_keys(template_metadata, {"labels"}, {"labels"}, "Pod metadata")
    if (
        template_metadata["labels"]
        != _mapping(document["metadata"], "metadata")["labels"]
    ):
        raise KubernetesContractError("Kubernetes Pod labels are not bound")
    pod = _mapping(pod_template["spec"], "Pod spec")
    _expect_keys(
        pod,
        {
            "automountServiceAccountToken",
            "restartPolicy",
            "terminationGracePeriodSeconds",
            "securityContext",
            "initContainers",
            "containers",
            "volumes",
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "nodeSelector",
        },
        {
            "automountServiceAccountToken",
            "restartPolicy",
            "terminationGracePeriodSeconds",
            "securityContext",
            "initContainers",
            "containers",
            "volumes",
        },
        "Pod spec",
    )
    if (
        pod["automountServiceAccountToken"] is not False
        or pod["restartPolicy"] != "Never"
    ):
        raise KubernetesContractError("Kubernetes Pod lifecycle is unsafe")
    expected_grace = 30 if profile == "mock" else 60
    if pod["terminationGracePeriodSeconds"] != expected_grace:
        raise KubernetesContractError("Kubernetes termination grace is not bound")
    _validate_pod_security(pod["securityContext"], "Pod security", uid=2000)
    if any(pod.get(key) is True for key in ("hostNetwork", "hostPID", "hostIPC")):
        raise KubernetesContractError("Kubernetes host namespace sharing is forbidden")
    if profile == "gpu" and pod.get("nodeSelector") != {"kubernetes.io/arch": "amd64"}:
        raise KubernetesContractError("Kubernetes GPU architecture is not pinned")
    if profile == "mock" and "nodeSelector" in pod:
        raise KubernetesContractError("Kubernetes mock node selection is unsupported")
    init_containers = _list(pod["initContainers"], "initContainers")
    containers = _list(pod["containers"], "containers")
    if len(init_containers) != 1 or len(containers) != 1:
        raise KubernetesContractError(
            "Kubernetes Job must split engine and runner roles"
        )
    init = _mapping(init_containers[0], "initContainer")
    if init.get("restartPolicy") != "Always":
        raise KubernetesContractError("Kubernetes engine must be a native sidecar")
    _validate_container(init, profile, engine=True, template=template)
    _validate_container(containers[0], profile, engine=False, template=template)
    _validate_volumes(pod["volumes"], profile)
    if (
        template
        and profile == "gpu"
        and GPU_RUNNER_IMAGE_PLACEHOLDER not in json.dumps(document)
    ):
        raise KubernetesContractError(
            "Kubernetes GPU template runner image placeholder is missing"
        )


def validate_kubernetes_manifest(
    path: Path, profile: str, *, template: bool = False
) -> dict[str, object]:
    raw = _bounded_bytes(
        path, maximum=KUBERNETES_MAX_MANIFEST_BYTES, label="Kubernetes manifest"
    )
    document = parse_kubernetes_yaml(raw)
    validate_kubernetes_job(document, profile, template=template)
    return document


def validate_kubernetes_experiment(path: Path) -> ExperimentSpec:
    """Validate one operator-supplied attached Qwen3 experiment offline.

    The PVC contents are outside this static check.  Only the exact experiment
    bytes and the repository's frozen campaign validator are examined here.
    """

    try:
        _bounded_bytes(path, maximum=512 * 1024, label="Kubernetes experiment")
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise KubernetesContractError("Kubernetes experiment is unavailable")
        resolution = resolve_experiment(path, strict=True)
        spec = resolution.resolved_spec
        if not isinstance(spec.target, AttachedVllmTarget):
            raise KubernetesContractError(
                "Kubernetes experiment target is unsupported"
            )
        if str(spec.target.endpoint).rstrip("/") != KUBERNETES_LOOPBACK_ENDPOINT:
            raise KubernetesContractError(
                "Kubernetes experiment endpoint is not loopback"
            )
        frozen_target = spec.target.model_copy(
            update={"endpoint": KUBERNETES_FROZEN_CAMPAIGN_ENDPOINT}
        )
        frozen_spec = spec.model_copy(update={"target": frozen_target})
        validate_qwen3_campaign_spec(frozen_spec)
        if spec.workload.sha256 != qwen3_workload_sha256():
            raise KubernetesContractError(
                "Kubernetes experiment workload is not pinned"
            )
        return spec
    except KubernetesContractError:
        raise
    except (
        AdapterError,
        OSError,
        ResolutionError,
        SourceInputError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise KubernetesContractError(
            "Kubernetes experiment does not match the frozen Qwen3 binding"
        ) from None


def validate_kubernetes_server_version(raw: bytes) -> tuple[int, int]:
    """Validate bounded ``kubectl version --output=json`` server metadata."""

    if len(raw) > KUBERNETES_MAX_VERSION_BYTES:
        raise KubernetesContractError("Kubernetes server version exceeds its bound")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        root = _mapping(value, "server version")
        _expect_keys(
            root,
            {"clientVersion", "kustomizeVersion", "serverVersion"},
            {"serverVersion"},
            "server version root",
        )
        version_fields = {
            "major",
            "minor",
            "gitVersion",
            "gitCommit",
            "gitTreeState",
            "buildDate",
            "goVersion",
            "compiler",
            "platform",
        }
        for info_name in ("clientVersion", "serverVersion"):
            if info_name not in root:
                continue
            info = _mapping(root[info_name], f"{info_name}")
            _expect_keys(info, version_fields, set(), info_name)
            for field_name, field_value in info.items():
                _string(field_value, f"{info_name} {field_name}", maximum=256)
        if "kustomizeVersion" in root:
            _string(root["kustomizeVersion"], "kustomize version", maximum=128)
        server = _mapping(root.get("serverVersion"), "server version")
        _expect_keys(
            server,
            version_fields,
            {"major", "minor"},
            "server version",
        )
        major_text = _string(server["major"], "server major", maximum=3)
        minor_text = _string(server["minor"], "server minor", maximum=4)
        if _VERSION_COMPONENT_RE.fullmatch(major_text) is None:
            raise ValueError
        if _VERSION_COMPONENT_RE.fullmatch(minor_text) is None:
            raise ValueError
        major = int(major_text, 10)
        minor = int(minor_text.rstrip("+"), 10)
    except KubernetesContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise KubernetesContractError("Kubernetes server version is invalid") from None
    if (major, minor) < (1, 33):
        raise KubernetesContractError("Kubernetes server version is unsupported")
    return major, minor


def _manifest_path(relative: str) -> Path:
    return Path(__file__).resolve().parents[2] / relative


def kubernetes_contract() -> dict[str, object]:
    mock_path = _manifest_path(MOCK_MANIFEST_RELATIVE_PATH)
    gpu_path = _manifest_path(GPU_MANIFEST_RELATIVE_PATH)
    mock_raw = mock_path.read_bytes()
    gpu_raw = gpu_path.read_bytes()
    validate_kubernetes_manifest(mock_path, "mock")
    validate_kubernetes_manifest(gpu_path, "gpu", template=True)
    return {
        "schema_version": KUBERNETES_CONTRACT_SCHEMA_VERSION,
        "kubernetes_min_version": KUBERNETES_MIN_VERSION,
        "job_shape": "batch/v1-job-native-sidecar",
        "profiles": {
            "mock": {
                "manifest": MOCK_MANIFEST_RELATIVE_PATH,
                "manifest_sha256": "sha256:" + hashlib.sha256(mock_raw).hexdigest(),
                "synthetic_only": True,
                "evidence_eligible": False,
                "evidence_persistence": "disposable-emptydir-log-retrieval",
            },
            "gpu": {
                "manifest_template": GPU_MANIFEST_RELATIVE_PATH,
                "manifest_sha256": "sha256:" + hashlib.sha256(gpu_raw).hexdigest(),
                "synthetic_only": False,
                "evidence_eligible": False,
                "evidence_persistence": "operator-provided-evidence-pvc-required",
                "runner_image": "immutable-digest-required-at-render-time",
            },
        },
        "runtime": {
            "engine": "vllm",
            "version": VLLM_VERSION,
            "image_reference": VLLM_RUNTIME_IMAGE_REFERENCE,
            "endpoint": "http://127.0.0.1:8000",
            "topology": "same-pod-loopback-native-sidecar",
        },
        "model": {
            "profile_id": QWEN3_8B_PROFILE_ID,
            "model_id": QWEN3_8B_MODEL_ID,
            "model_revision": QWEN3_8B_REVISION,
            "tokenizer_revision": QWEN3_8B_REVISION,
            "model_manifest_sha256": qwen3_model_manifest_sha256(),
            "snapshot_manifest_sha256": qwen3_expected_snapshot_sha256(),
        },
        "benchmark": {
            "methodology": "existing-qwen3-campaign-source-of-truth",
            "workload_id": QWEN3_WORKLOAD_ID,
            "workload_path": QWEN3_WORKLOAD_PATH,
            "workload_sha256": qwen3_workload_sha256(),
            "command": "inferdrome run",
        },
        "claims": {
            "mock_execution": "synthetic-contract-simulation-only-until-cluster-gate",
            "gpu_execution": "not-executed",
            "evidence": "no-eligible-evidence-claim",
        },
    }


def _parse_output(raw: bytes) -> dict[str, object]:
    if len(raw) > KUBERNETES_MAX_OUTPUT_BYTES:
        raise KubernetesContractError("synthetic runner output exceeds its bound")
    candidate = raw[:-1] if raw.endswith(b"\n") else raw
    if not candidate or b"\n" in candidate:
        raise KubernetesContractError("synthetic runner output must be one JSON object")
    try:
        value = json.loads(
            candidate,
            object_pairs_hook=lambda pairs: _unique_json_pairs(pairs),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise KubernetesContractError("synthetic runner output is invalid") from None
    if not isinstance(value, dict) or canonical_json_bytes(value) != candidate:
        raise KubernetesContractError("synthetic runner output is not canonical")
    expected = {
        "schema_version",
        "runner_version",
        "execution_mode",
        "synthetic_only",
        "evidence_eligible",
        "status",
        "endpoint_sha256",
        "model",
        "request_sha256",
        "response_sha256",
        "response_status",
        "response_bytes",
    }
    if set(value) != expected:
        raise KubernetesContractError("synthetic runner output has unsupported fields")
    if (
        value["schema_version"] != RUNNER_OUTPUT_SCHEMA_VERSION
        or value["execution_mode"] != "synthetic_endpoint_probe"
        or value["synthetic_only"] is not True
        or value["evidence_eligible"] is not False
        or value["status"] != "SUCCEEDED"
        or value["model"] != "inferdrome/mock-model"
        or value["response_status"] != 200
    ):
        raise KubernetesContractError(
            "synthetic runner output is not eligible for this contract"
        )
    for name in ("endpoint_sha256", "request_sha256", "response_sha256"):
        digest = value[name]
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise KubernetesContractError("synthetic runner output digest is invalid")
    if (
        not isinstance(value["runner_version"], str)
        or len(value["runner_version"]) > 32
    ):
        raise KubernetesContractError("synthetic runner output version is invalid")
    if (
        isinstance(value["response_bytes"], bool)
        or not isinstance(value["response_bytes"], int)
        or not 0 <= value["response_bytes"] <= 4 * 1024 * 1024
    ):
        raise KubernetesContractError("synthetic runner output size is invalid")
    return value


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def verify_synthetic_output(path: Path) -> dict[str, object]:
    return _parse_output(
        _bounded_bytes(
            path, maximum=KUBERNETES_MAX_OUTPUT_BYTES, label="synthetic runner output"
        )
    )


def validate_synthetic_output_destination(destination: Path) -> None:
    """Preflight a no-replace output path before cluster allocation."""

    _reject_symlinked_ancestors(
        destination, "synthetic output destination", allow_missing_leaf=True
    )
    parent = destination.parent
    try:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise KubernetesContractError("synthetic output directory is unsafe")
        leaf = os.lstat(destination)
    except FileNotFoundError:
        return
    except KubernetesContractError:
        raise
    except OSError:
        raise KubernetesContractError(
            "synthetic output destination is unsafe"
        ) from None
    if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode):
        raise KubernetesContractError("synthetic output destination is occupied")
    raise KubernetesContractError("synthetic output already exists")


def validate_synthetic_output_directory(directory: Path) -> None:
    """Preflight an output directory before creating it or allocating a cluster."""

    if ".." in Path(directory).parts:
        raise KubernetesContractError("synthetic output directory is unsafe")
    selected = Path(os.path.abspath(directory))
    current = Path(selected.anchor)
    missing_suffix = False
    for component in selected.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            missing_suffix = True
            continue
        except OSError:
            raise KubernetesContractError(
                "synthetic output directory is unsafe"
            ) from None
        if missing_suffix or stat.S_ISLNK(metadata.st_mode):
            raise KubernetesContractError("synthetic output directory is unsafe")
        if not stat.S_ISDIR(metadata.st_mode):
            raise KubernetesContractError("synthetic output directory is unsafe")


def publish_synthetic_output(source: Path, destination: Path) -> None:
    source_raw = _bounded_bytes(
        source, maximum=KUBERNETES_MAX_OUTPUT_BYTES, label="synthetic output source"
    )
    _parse_output(source_raw)
    _reject_symlinked_ancestors(source, "synthetic output source")
    _reject_symlinked_ancestors(
        destination, "synthetic output destination", allow_missing_leaf=True
    )
    parent = destination.parent
    try:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise KubernetesContractError("synthetic output directory is unsafe")
        os.lstat(destination)
    except FileNotFoundError:
        pass
    except KubernetesContractError:
        raise
    except OSError:
        raise KubernetesContractError(
            "synthetic output destination is unsafe"
        ) from None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
        try:
            os.link(source, destination, follow_symlinks=False)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        raise KubernetesContractError("synthetic output already exists") from None
    except OSError:
        raise KubernetesContractError(
            "synthetic output could not be published"
        ) from None
    try:
        destination_raw = _bounded_bytes(
            destination,
            maximum=KUBERNETES_MAX_OUTPUT_BYTES,
            label="synthetic output destination",
        )
        _parse_output(destination_raw)
        if destination_raw != source_raw:
            raise KubernetesContractError("synthetic output read-back differs")
    except KubernetesContractError:
        with contextlib.suppress(OSError):
            os.unlink(destination)
        raise


def _pretty(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inferdrome-kubernetes-contract")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--profile", choices=("mock", "gpu"), required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--template", action="store_true")
    verify = subparsers.add_parser("verify-output")
    verify.add_argument("path", type=Path)
    publish = subparsers.add_parser("publish-output")
    publish.add_argument("source", type=Path)
    publish.add_argument("destination", type=Path)
    preflight = subparsers.add_parser("preflight-experiment")
    preflight.add_argument("--experiment", type=Path, required=True)
    preflight_output = subparsers.add_parser("preflight-output")
    preflight_output.add_argument("path", type=Path)
    preflight_output_directory = subparsers.add_parser("preflight-output-dir")
    preflight_output_directory.add_argument("path", type=Path)
    server_version = subparsers.add_parser("server-version")
    server_version.add_argument("path", type=Path)
    subparsers.add_parser("contract")
    try:
        if arguments := parser.parse_args(argv):
            if arguments.command == "validate":
                validate_kubernetes_manifest(
                    arguments.manifest, arguments.profile, template=arguments.template
                )
                print("Kubernetes manifest contract: OK")
            elif arguments.command == "verify-output":
                verify_synthetic_output(arguments.path)
                print("synthetic Kubernetes output: OK")
            elif arguments.command == "publish-output":
                publish_synthetic_output(arguments.source, arguments.destination)
                print("synthetic Kubernetes output publication: OK")
            elif arguments.command == "preflight-experiment":
                validate_kubernetes_experiment(arguments.experiment)
                print("Kubernetes experiment binding: OK")
            elif arguments.command == "preflight-output":
                validate_synthetic_output_destination(arguments.path)
                print("synthetic Kubernetes output destination: OK")
            elif arguments.command == "preflight-output-dir":
                validate_synthetic_output_directory(arguments.path)
                print("synthetic Kubernetes output directory: OK")
            elif arguments.command == "server-version":
                raw = _bounded_bytes(
                    arguments.path,
                    maximum=KUBERNETES_MAX_VERSION_BYTES,
                    label="Kubernetes server version",
                )
                major, minor = validate_kubernetes_server_version(raw)
                print(f"Kubernetes server version: {major}.{minor}")
            else:
                sys.stdout.buffer.write(_pretty(kubernetes_contract()))
    except KubernetesContractError as error:
        print(f"Kubernetes contract failed: {error}", file=sys.stderr)
        return 2
    return 0


__all__ = [
    "GPU_MANIFEST_RELATIVE_PATH",
    "GPU_RUNNER_IMAGE_PLACEHOLDER",
    "KUBERNETES_CONTRACT_SCHEMA_VERSION",
    "KUBERNETES_FROZEN_CAMPAIGN_ENDPOINT",
    "KUBERNETES_LOOPBACK_ENDPOINT",
    "KUBERNETES_MAX_MANIFEST_BYTES",
    "KUBERNETES_MAX_OUTPUT_BYTES",
    "KUBERNETES_MAX_VERSION_BYTES",
    "KUBERNETES_MAX_YAML_TOKENS",
    "KUBERNETES_MIN_VERSION",
    "MOCK_ENGINE_IMAGE",
    "MOCK_JOB_NAME",
    "MOCK_MANIFEST_RELATIVE_PATH",
    "MOCK_OUTPUT_NAME",
    "MOCK_RUNNER_IMAGE",
    "KubernetesContractError",
    "kubernetes_contract",
    "parse_kubernetes_yaml",
    "publish_synthetic_output",
    "validate_kubernetes_experiment",
    "validate_kubernetes_job",
    "validate_kubernetes_manifest",
    "validate_kubernetes_server_version",
    "validate_synthetic_output_destination",
    "validate_synthetic_output_directory",
    "verify_synthetic_output",
]


if __name__ == "__main__":
    raise SystemExit(main())
