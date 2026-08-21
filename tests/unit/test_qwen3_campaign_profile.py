"""Fail-closed invariants for the locally conformant Qwen3 campaign profile."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from inferdrome.adapters import vllm_bench as vllm_bench_module
from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    VllmInvocationPaths,
    build_vllm_invocation,
    preflight_attached_endpoint,
    validate_vllm_invocation_evidence,
)
from inferdrome.capability_profiles import valid_local_gpu_proof_fixture
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    CanonicalResponseContentPolicy,
)
from inferdrome.errors import AdapterError
from inferdrome.gpu_proof import (
    LocalGpuProof,
    ManagedVllmConfig,
    build_managed_server_argv,
)
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_PROFILE_ID,
    QWEN3_8B_REVISION,
    QWEN3_REQUEST_EXTRA_BODY,
    QWEN3_TARGET_INPUT_TOKENS,
    qwen3_expected_snapshot_sha256,
    qwen3_host_dependencies_sha256,
    qwen3_launch_documents,
    qwen3_model_manifest_sha256,
    qwen3_profile_document,
    qwen3_profile_sha256,
    qwen3_workload_manifest,
    qwen3_workload_prompts,
    qwen3_workload_sha256,
    validate_qwen3_campaign_spec,
)
from inferdrome.qwen3_tokenizer import (
    expected_qwen3_tokenizer_file_verification,
)
from inferdrome.resolution import ResolutionResult, resolve_experiment
from inferdrome.resolution.workload import parse_custom_workload

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ROOT = REPOSITORY_ROOT / "campaigns" / "v1"
RUN_ID = "run-77777777777777777777777777777777"


def _resolution(concurrency: int = 4) -> ResolutionResult:
    return resolve_experiment(
        CAMPAIGN_ROOT / f"qwen3-8b-concurrency-{concurrency}.yaml",
        run_id=RUN_ID,
    )


def _option_value(argv: tuple[str, ...], option: str) -> str:
    position = argv.index(option)
    return argv[position + 1]


def _preflight(resolution: ResolutionResult) -> Any:
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    response = json.dumps(
        {"data": [{"id": QWEN3_8B_MODEL_ID}]},
        separators=(",", ":"),
    ).encode()
    return preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=response,
        ),
    )


def _local_proof(
    resolution: ResolutionResult,
    snapshot: Path,
) -> LocalGpuProof:
    value = copy.deepcopy(valid_local_gpu_proof_fixture())
    value["run_id"] = RUN_ID
    for key, kind in (("model_snapshot", "model"), ("tokenizer_snapshot", "tokenizer")):
        value[key].update(
            {
                "kind": kind,
                "revision": QWEN3_8B_REVISION,
                "root": str(snapshot),
            }
        )
    value["server"]["argv"] = list(
        build_managed_server_argv(
            resolution.resolved_spec,
            executable_path=value["producer_distribution"]["executable_path"],
            model_path=str(snapshot),
            tokenizer_path=str(snapshot),
            gpu_indices=(0,),
            capability_profile_id=QWEN3_8B_PROFILE_ID,
        )
    )
    return LocalGpuProof.model_validate_json(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    )


def _invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResolutionResult, Any]:
    resolution = _resolution()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    result_directory = tmp_path / "native"
    result_directory.mkdir()
    monkeypatch.setattr(
        vllm_bench_module,
        "verify_qwen3_tokenizer_files",
        lambda _root: expected_qwen3_tokenizer_file_verification(),
    )
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        VllmInvocationPaths(
            dataset_path=(
                CAMPAIGN_ROOT / "workloads" / "qwen-text-mixed-length-v1.jsonl"
            ).resolve(),
            tokenizer_path=snapshot.resolve(),
            result_directory=result_directory.resolve(),
        ),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
        local_gpu_proof=_local_proof(resolution, snapshot.resolve()),
        capability_profile_id=QWEN3_8B_PROFILE_ID,
    )
    return resolution, invocation


def test_generated_workload_and_profile_are_exact_and_current() -> None:
    rendered = qwen3_launch_documents()
    for relative, expected in rendered.items():
        assert (REPOSITORY_ROOT / relative).read_bytes() == expected

    workload = rendered["campaigns/v1/workloads/qwen-text-mixed-length-v1.jsonl"]
    prompts = parse_custom_workload(workload)
    manifest = qwen3_workload_manifest()
    assert prompts == qwen3_workload_prompts()
    assert len(prompts) == len(set(prompts)) == 96
    assert qwen3_workload_sha256() == manifest["workload_sha256"]
    assert [item["rendered_input_tokens"] for item in manifest["bucket_order"]] == list(
        QWEN3_TARGET_INPUT_TOKENS
    )
    assert manifest["warmup"] == {
        "population": "EXCLUDED_FROM_MEASUREMENTS",
        "preceded_by_readiness_probe": (
            "vllm_first_measured_request_until_success_bounded_v0_26"
        ),
        "requests": 12,
        "sequence_index": 0,
        "strategy": "vllm_first_measured_request_repeated_v0_26",
    }
    assert manifest["readiness_probe"] == {
        "policy": "vllm_first_measured_request_until_success_bounded_v0_26",
        "population": "EXCLUDED_FROM_MEASUREMENTS",
        "sequence_index": 0,
        "timeout_seconds": 5,
    }
    profile = qwen3_profile_document()
    manifest_digest = (
        "sha256:" + hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    )
    assert profile["digest_policy"] == {
        "profile_sha256": "sha256_of_rfc8785_canonical_json_bytes",
        "workload_manifest_sha256": ("sha256_of_rfc8785_canonical_json_bytes"),
        "workload_sha256": "sha256_of_exact_utf8_jsonl_bytes",
    }
    assert profile["workload_binding"]["manifest_sha256"] == manifest_digest
    assert profile["benchmark_invocation"]["readiness_probe_population"] == (
        "EXCLUDED_FROM_MEASUREMENTS"
    )
    assert profile["benchmark_invocation"]["ready_check_timeout_seconds"] == 5
    assert profile["implementation_state"] == ("LOCALLY_CONFORMANT_RUNTIME_UNPROVEN")
    assert profile["model"]["snapshot_identity_sha256"] == (
        qwen3_expected_snapshot_sha256()
    )
    assert profile["model"]["snapshot_manifest_sha256"] == (
        qwen3_model_manifest_sha256()
    )
    assert profile["producer"]["host_dependencies_sha256"] == (
        qwen3_host_dependencies_sha256()
    )
    assert qwen3_profile_sha256().startswith("sha256:")


def test_linux_host_preparation_embeds_current_qwen3_manifest_pins() -> None:
    preparation = (REPOSITORY_ROOT / "scripts/prepare_real_gpu_host.sh").read_text(
        encoding="utf-8"
    )

    assert qwen3_expected_snapshot_sha256() in preparation
    assert qwen3_model_manifest_sha256() in preparation
    assert qwen3_host_dependencies_sha256() in preparation
    assert "git-archive-exact-head-tree-v1" in preparation


@pytest.mark.parametrize("concurrency", [1, 4, 16])
def test_every_generated_attached_source_satisfies_the_profile(
    concurrency: int,
) -> None:
    resolution = _resolution(concurrency)
    validate_qwen3_campaign_spec(resolution.resolved_spec)
    assert len(resolution.request_plan.requests) == 96


def test_profile_rejects_model_workload_and_traffic_drift() -> None:
    resolution = _resolution()
    spec = resolution.resolved_spec
    target = spec.target
    assert isinstance(target, AttachedVllmTarget)

    with pytest.raises(AdapterError, match="target identity"):
        validate_qwen3_campaign_spec(
            spec.model_copy(
                update={"target": target.model_copy(update={"model": "other/model"})}
            )
        )
    with pytest.raises(AdapterError, match="workload configuration"):
        validate_qwen3_campaign_spec(
            spec.model_copy(
                update={
                    "workload": spec.workload.model_copy(update={"temperature": "0.8"})
                }
            )
        )
    with pytest.raises(AdapterError, match="traffic configuration"):
        validate_qwen3_campaign_spec(
            spec.model_copy(
                update={
                    "traffic": spec.traffic.model_copy(update={"warmup_requests": 11})
                }
            )
        )
    with pytest.raises(AdapterError, match="experiment identity"):
        validate_qwen3_campaign_spec(
            spec.model_copy(
                update={
                    "experiment": spec.experiment.model_copy(
                        update={"id": "renamed-campaign"}
                    )
                }
            )
        )
    with pytest.raises(AdapterError, match="evidence configuration"):
        validate_qwen3_campaign_spec(
            spec.model_copy(
                update={
                    "evidence": spec.evidence.model_copy(
                        update={
                            "canonical_response_content": (
                                CanonicalResponseContentPolicy.INCLUDE
                            )
                        }
                    )
                }
            )
        )


def test_profile_selects_exact_server_controls_without_changing_legacy() -> None:
    resolution = _resolution()
    with pytest.raises(AdapterError, match="requires its explicit"):
        build_managed_server_argv(
            resolution.resolved_spec,
            executable_path="/opt/inferdrome/venv/bin/vllm",
            model_path="/opt/inferdrome/models/qwen3",
            tokenizer_path="/opt/inferdrome/models/qwen3",
            gpu_indices=(0,),
        )
    renamed = resolution.resolved_spec.model_copy(
        update={
            "experiment": resolution.resolved_spec.experiment.model_copy(
                update={"id": "renamed-qwen-source"}
            )
        }
    )
    with pytest.raises(AdapterError, match="requires its explicit"):
        build_managed_server_argv(
            renamed,
            executable_path="/opt/inferdrome/venv/bin/vllm",
            model_path="/opt/inferdrome/models/qwen3",
            tokenizer_path="/opt/inferdrome/models/qwen3",
            gpu_indices=(0,),
        )

    profiled = build_managed_server_argv(
        resolution.resolved_spec,
        executable_path="/opt/inferdrome/venv/bin/vllm",
        model_path="/opt/inferdrome/models/qwen3",
        tokenizer_path="/opt/inferdrome/models/qwen3",
        gpu_indices=(0,),
        capability_profile_id=QWEN3_8B_PROFILE_ID,
    )
    assert _option_value(profiled, "--dtype") == "bfloat16"
    assert _option_value(profiled, "--max-model-len") == "2048"
    assert _option_value(profiled, "--gpu-memory-utilization") == "0.90"

    legacy = resolve_experiment(
        REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml",
        run_id=RUN_ID,
    )
    legacy_argv = build_managed_server_argv(
        legacy.resolved_spec,
        executable_path="/opt/inferdrome/venv/bin/vllm",
        model_path="/opt/inferdrome/models/qwen2",
        tokenizer_path="/opt/inferdrome/models/qwen2",
        gpu_indices=(0,),
    )
    assert _option_value(legacy_argv, "--dtype") == "auto"
    assert _option_value(legacy_argv, "--max-model-len") == "1024"
    assert _option_value(legacy_argv, "--gpu-memory-utilization") == "0.80"


def test_qwen3_invocation_round_trips_with_explicit_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution, invocation = _invocation(tmp_path, monkeypatch)
    value = json.loads(invocation.evidence_bytes)
    validated = validate_vllm_invocation_evidence(
        invocation.evidence_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        execution_fingerprint=resolution.execution_fingerprint,
    )

    assert validated == invocation
    assert set(value) == {
        "argv",
        "campaign_profile",
        "endpoint_preflight",
        "local_gpu_proof",
        "metadata",
        "schema_version",
    }
    assert value["campaign_profile"]["profile_id"] == QWEN3_8B_PROFILE_ID
    assert value["campaign_profile"]["profile_sha256"] == qwen3_profile_sha256()
    assert value["campaign_profile"]["tokenizer_files"] == (
        expected_qwen3_tokenizer_file_verification().model_dump(mode="json")
    )
    assert _option_value(invocation.argv, "--top-p") == "0.8"
    assert _option_value(invocation.argv, "--top-k") == "20"
    assert _option_value(invocation.argv, "--min-p") == "0"
    assert _option_value(invocation.argv, "--extra-body") == (QWEN3_REQUEST_EXTRA_BODY)
    assert "--ignore-eos" in invocation.argv


@pytest.mark.parametrize(
    "mutation",
    [
        "profile_digest",
        "workload_digest",
        "tokenizer_digest",
        "top_p",
        "request_seed",
        "extra_body",
        "ignore_eos",
    ],
)
def test_qwen3_invocation_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    resolution, invocation = _invocation(tmp_path, monkeypatch)
    value: dict[str, Any] = json.loads(invocation.evidence_bytes)
    argv: list[str] = value["argv"]
    if mutation == "profile_digest":
        value["campaign_profile"]["profile_sha256"] = "sha256:" + "0" * 64
    elif mutation == "workload_digest":
        value["campaign_profile"]["workload_sha256"] = "sha256:" + "0" * 64
    elif mutation == "tokenizer_digest":
        value["campaign_profile"]["tokenizer_files"]["tokenizer_json_sha256"] = (
            "sha256:" + "0" * 64
        )
    elif mutation == "top_p":
        argv[argv.index("--top-p") + 1] = "0.9"
    elif mutation == "request_seed":
        position = argv.index("--extra-body") + 1
        extra_body = json.loads(argv[position])
        extra_body["seed"] = 41
        argv[position] = json.dumps(extra_body, separators=(",", ":"), sort_keys=True)
    elif mutation == "extra_body":
        argv[argv.index("--extra-body") + 1] = "{}"
    else:
        argv.remove("--ignore-eos")

    with pytest.raises(AdapterError):
        validate_vllm_invocation_evidence(
            canonical_json_bytes(value),
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
        )


def test_profile_cannot_be_selected_without_local_gpu_proof(tmp_path: Path) -> None:
    resolution = _resolution()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result_directory = tmp_path / "native"
    result_directory.mkdir()

    with pytest.raises(AdapterError, match="requires its explicit"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=(
                    CAMPAIGN_ROOT / "workloads" / "qwen-text-mixed-length-v1.jsonl"
                ).resolve(),
                tokenizer_path=snapshot.resolve(),
                result_directory=result_directory.resolve(),
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
        )

    with pytest.raises(AdapterError, match="requires local GPU proof"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=(
                    CAMPAIGN_ROOT / "workloads" / "qwen-text-mixed-length-v1.jsonl"
                ).resolve(),
                tokenizer_path=snapshot.resolve(),
                result_directory=result_directory.resolve(),
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
            capability_profile_id=QWEN3_8B_PROFILE_ID,
        )

    with pytest.raises(AdapterError, match="unsupported"):
        ManagedVllmConfig(
            model_path=snapshot.resolve(),
            capability_profile_id="unknown-profile",
        )


def test_profile_rejects_wrong_tokenizer_bytes(tmp_path: Path) -> None:
    resolution = _resolution()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    result_directory = tmp_path / "native"
    result_directory.mkdir()

    with pytest.raises(AdapterError, match="tokenizer file digest differs"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=(
                    CAMPAIGN_ROOT / "workloads" / "qwen-text-mixed-length-v1.jsonl"
                ).resolve(),
                tokenizer_path=snapshot.resolve(),
                result_directory=result_directory.resolve(),
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
            local_gpu_proof=_local_proof(resolution, snapshot.resolve()),
            capability_profile_id=QWEN3_8B_PROFILE_ID,
        )
