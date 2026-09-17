"""SYNTHETIC_ONLY local files and projection; no Docker, CUDA, or model downloads."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.sglang_container import (
    MAX_SGLANG_TEMPLATE_BYTES,
    build_sglang_container_profile,
    verify_sglang_artifacts,
)
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SglangServingConfig,
    build_sglang_serving_profile,
    validate_sglang_readiness,
)
from inferdrome.execution import managed_vllm
from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_sglang_serving_profile import config, model_info


def artifacts(tmp_path: Path) -> SglangServingConfig:
    # Resolve pytest's temp root so macOS /var aliases are not test inputs.
    root = tmp_path.resolve()
    model, tokenizer = root / "model-source", root / "tokenizer-source"
    model.mkdir()
    tokenizer.mkdir()
    (model / "weights.safetensors").write_bytes(b"SYNTHETIC_ONLY model bytes")
    (tokenizer / "tokenizer.json").write_bytes(b'{"SYNTHETIC_ONLY":true}')
    template = root / "source-template.jinja"
    template.write_bytes(b"{{ messages }}\n")
    revision = "a" * 40
    return config(
        served_model_name="arbitrary-supported-model",
        model_path=str(model),
        tokenizer_path=str(tokenizer),
        chat_template_path=str(template),
        model_snapshot_sha256=snapshot_directory_identity(
            model, kind="model", revision=revision
        ).sha256,
        tokenizer_snapshot_sha256=snapshot_directory_identity(
            tokenizer, kind="tokenizer", revision=revision
        ).sha256,
        chat_template_sha256=sha256_digest(template.read_bytes()),
    )


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("cache", ["RADIX_ENABLED", "RADIX_DISABLED"])
def test_projection_changes_only_fixed_network_and_artifact_paths(
    index: int, cache: str
) -> None:
    source_config = config(prefix_cache=cache)
    native = build_sglang_serving_profile(source_config)
    profile = build_sglang_container_profile(source_config, index)
    expected = list(native.argv)
    for flag, value in (
        ("--host", "0.0.0.0"),
        ("--port", "8000"),
        ("--model-path", "/model"),
        ("--tokenizer-path", "/tokenizer"),
        ("--chat-template", "/chat-template.jinja"),
    ):
        expected[expected.index(flag) + 1] = value
    assert profile.argv == tuple(expected)
    assert profile.source_profile == native
    assert profile.source_profile_sha256 == native.config_sha256
    assert profile.environment == native.environment
    assert profile.image_reference == SGLANG_IMAGE_REFERENCE
    assert profile.image_platform == "linux/amd64"
    assert profile.execution_mode == "DOCKER_BRIDGE"
    assert profile.gpu_index == index
    assert profile.publish == "127.0.0.1:8001:8000"
    assert profile.mounts == (
        f"type=bind,src={source_config.model_path},dst=/model,readonly",
        f"type=bind,src={source_config.tokenizer_path},dst=/tokenizer,readonly",
        f"type=bind,src={source_config.chat_template_path},dst=/chat-template.jinja,readonly",
    )
    assert profile.runtime_verification == "UNVERIFIED"
    assert profile.evidence_eligible is False
    assert build_sglang_serving_profile(source_config) == native


def test_launch_digest_covers_exact_projection_source_identity_and_gpu() -> None:
    profile = build_sglang_container_profile(config(), 0)
    payload = {
        "schema_version": "inferdrome.sglang-container-launch.v1",
        "execution_mode": "DOCKER_BRIDGE",
        "source_profile_sha256": profile.source_profile_sha256,
        "readiness_config_sha256": profile.readiness_config_sha256,
        "argv": list(profile.argv),
        "environment": [list(pair) for pair in profile.environment],
        "mounts": list(profile.mounts),
        "publish": profile.publish,
        "gpu_index": 0,
        "image_reference": profile.image_reference,
        "image_platform": profile.image_platform,
    }
    assert profile.launch_sha256 == sha256_digest(canonical_json_bytes(payload) + b"\n")
    assert profile.launch_sha256 != profile.source_profile_sha256
    assert (
        profile.launch_sha256
        != build_sglang_container_profile(config(), 1).launch_sha256
    )
    assert "127.0.0.1" not in repr(profile)
    assert "/opt/inferdrome" not in repr(profile)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("origin", "http://127.0.0.1:9001"),
        ("model_path", "/different/model"),
        ("tokenizer_path", "/different/tokenizer"),
        ("chat_template_path", "/different/template.jinja"),
        ("model_snapshot_sha256", "sha256:" + "9" * 64),
        ("tokenizer_snapshot_sha256", "sha256:" + "9" * 64),
        ("chat_template_sha256", "sha256:" + "9" * 64),
        ("random_seed", 7),
        ("prefix_cache", "RADIX_DISABLED"),
    ],
)
def test_projection_digest_changes_with_every_selected_input(
    key: str, value: object
) -> None:
    original = build_sglang_container_profile(config(), 0)
    changed = build_sglang_container_profile(config(**{key: value}), 0)
    assert changed.launch_sha256 != original.launch_sha256


def test_readiness_projection_retains_external_origin_and_original_identity() -> None:
    source = config()
    profile = build_sglang_container_profile(source, 0)
    expected = profile.readiness_config
    assert expected.origin == source.origin
    assert expected.model_path == "/model"
    assert expected.tokenizer_path == "/tokenizer"
    assert expected.chat_template_path == "/chat-template.jinja"
    assert expected.model_snapshot_sha256 == source.model_snapshot_sha256
    assert expected.tokenizer_snapshot_sha256 == source.tokenizer_snapshot_sha256
    assert expected.chat_template_sha256 == source.chat_template_sha256
    ready = validate_sglang_readiness(
        expected,
        health_status=200,
        generation_status=200,
        model_info_status=200,
        model_info=model_info(model_path="/model", tokenizer_path="/tokenizer"),
    )
    assert ready.config_sha256 == profile.readiness_config_sha256
    assert ready.config_sha256 != profile.source_profile_sha256
    with pytest.raises(EvaluationError):
        validate_sglang_readiness(
            expected,
            health_status=200,
            generation_status=200,
            model_info_status=200,
            model_info=model_info(),
        )


def test_projection_never_touches_files_or_launches_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure projection attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", no_io)
        patch.setattr(Path, "open", no_io)
        profile = build_sglang_container_profile(config(), 0)
    assert profile.argv[0:3] == ("python3", "-m", "sglang.launch_server")


@pytest.mark.parametrize("index", [-1, 2, True, False, 0.0, "0", None])
def test_projection_rejects_unsupported_device_primitive(index: object) -> None:
    with pytest.raises(EvaluationError):
        build_sglang_container_profile(config(), index)


def test_owned_projection_restricts_host_binding_without_changing_native_profile() -> (
    None
):
    source = config(origin="http://10.0.0.2:8001")
    assert build_sglang_serving_profile(source).config == source
    with pytest.raises(EvaluationError):
        build_sglang_container_profile(source, 0)


def test_projection_revalidates_forged_config_primitives() -> None:
    with pytest.raises(EvaluationError):
        build_sglang_container_profile(
            config().model_copy(update={"max_running_requests": True}), 0
        )


def test_local_artifact_preflight_matches_existing_snapshot_policy(
    tmp_path: Path,
) -> None:
    source = artifacts(tmp_path)
    result = verify_sglang_artifacts(source)
    assert (
        result.source_profile_sha256
        == build_sglang_serving_profile(source).config_sha256
    )
    assert result.model_snapshot_sha256 == source.model_snapshot_sha256
    assert result.tokenizer_snapshot_sha256 == source.tokenizer_snapshot_sha256
    assert result.chat_template_sha256 == source.chat_template_sha256
    assert result.snapshot_hash_policy == "regular-files-excluding-dot-cache-v1"
    assert result.verification == "LOCAL_FILES_MATCH_AT_PREFLIGHT"
    assert result.runtime_verification == "UNVERIFIED"
    assert result.evidence_eligible is False
    assert str(tmp_path) not in repr(result)


@pytest.mark.parametrize(
    "field",
    ["model_snapshot_sha256", "tokenizer_snapshot_sha256", "chat_template_sha256"],
)
def test_preflight_rejects_each_declared_digest_mismatch(
    tmp_path: Path, field: str
) -> None:
    source = artifacts(tmp_path).model_copy(update={field: "sha256:" + "0" * 64})
    with pytest.raises(EvaluationError) as error:
        verify_sglang_artifacts(source)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(
    "target", ["model", "tokenizer", "template", "ancestor", "snapshot-member"]
)
def test_preflight_rejects_symlinks(tmp_path: Path, target: str) -> None:
    source = artifacts(tmp_path)
    model, tokenizer, template = (
        Path(source.model_path),
        Path(source.tokenizer_path),
        Path(source.chat_template_path),
    )
    if target == "ancestor":
        link = tmp_path / "alias"
        link.symlink_to(tmp_path.resolve(), target_is_directory=True)
        source = source.model_copy(update={"model_path": str(link / model.name)})
    elif target == "snapshot-member":
        file = model / "weights.safetensors"
        moved = tmp_path / "outside-weights"
        file.rename(moved)
        file.symlink_to(moved)
    else:
        path = {"model": model, "tokenizer": tokenizer, "template": template}[target]
        moved = path.with_name("moved-" + path.name)
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=target != "template")
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


@pytest.mark.parametrize("target", ["model", "tokenizer", "template"])
def test_preflight_rejects_hardlinks(tmp_path: Path, target: str) -> None:
    source = artifacts(tmp_path)
    file = {
        "model": Path(source.model_path) / "weights.safetensors",
        "tokenizer": Path(source.tokenizer_path) / "tokenizer.json",
        "template": Path(source.chat_template_path),
    }[target]
    os.link(file, tmp_path / "second-link")
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


@pytest.mark.parametrize("target", ["model", "tokenizer", "template"])
def test_preflight_rejects_unavailable_inputs(tmp_path: Path, target: str) -> None:
    source = artifacts(tmp_path)
    field = {
        "model": "model_path",
        "tokenizer": "tokenizer_path",
        "template": "chat_template_path",
    }[target]
    source = source.model_copy(update={field: str(tmp_path / "missing.jinja")})
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


@pytest.mark.parametrize("size", [0, MAX_SGLANG_TEMPLATE_BYTES + 1])
def test_preflight_requires_bounded_nonempty_template(
    tmp_path: Path, size: int
) -> None:
    source = artifacts(tmp_path)
    Path(source.chat_template_path).write_bytes(b"x" * size)
    source = source.model_copy(
        update={"chat_template_sha256": sha256_digest(b"x" * size)}
    )
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


def test_preflight_rejects_special_file_without_reading_it(tmp_path: Path) -> None:
    source = artifacts(tmp_path)
    path = Path(source.chat_template_path)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


def test_snapshot_cache_exclusion_preserves_existing_hash_domain(
    tmp_path: Path,
) -> None:
    source = artifacts(tmp_path)
    cache = Path(source.model_path) / ".cache"
    cache.mkdir()
    (cache / "synthetic-cache").write_bytes(b"excluded by existing hash policy")
    assert (
        verify_sglang_artifacts(source).model_snapshot_sha256
        == source.model_snapshot_sha256
    )


def test_preflight_detects_model_change_after_its_snapshot_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = artifacts(tmp_path)
    original = managed_vllm.snapshot_directory_identity

    def changed(path: Path, *, kind: str, revision: str):
        result = original(path, kind=kind, revision=revision)
        if kind == "model":
            (path / "weights.safetensors").write_bytes(b"replaced after model hash")
        return result

    monkeypatch.setattr(managed_vllm, "snapshot_directory_identity", changed)
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


def test_preflight_detects_template_change_after_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = artifacts(tmp_path)
    original = managed_vllm._read_small_regular_file

    def changed(path: Path, expected, *, label: str, limit: int):
        result = original(path, expected, label=label, limit=limit)
        path.write_bytes(b"changed template")
        return result

    monkeypatch.setattr(managed_vllm, "_read_small_regular_file", changed)
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


def test_preflight_detects_root_replacement_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = artifacts(tmp_path)
    original = managed_vllm.snapshot_directory_identity

    def changed(path: Path, *, kind: str, revision: str):
        result = original(path, kind=kind, revision=revision)
        if kind == "model":
            moved = path.with_name("replaced-model")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
        return result

    monkeypatch.setattr(managed_vllm, "snapshot_directory_identity", changed)
    with pytest.raises(EvaluationError):
        verify_sglang_artifacts(source)


def test_preflight_revalidates_config_and_revisions(tmp_path: Path) -> None:
    source = artifacts(tmp_path)
    for updates in ({"random_seed": False}, {"tokenizer_revision": "b" * 40}):
        with pytest.raises(EvaluationError):
            verify_sglang_artifacts(source.model_copy(update=updates))
