"""Attached endpoint preflight and vLLM invocation stay exact and secret-free."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import inferdrome.adapters.vllm_bench as vllm_bench_module
from inferdrome.adapters.vllm_bench import (
    EndpointPreflightCapture,
    HttpResponse,
    VllmInvocationPaths,
    build_vllm_invocation,
    execute_vllm_benchmark,
    parse_vllm_version_output,
    preflight_attached_endpoint,
    probe_vllm_version,
    validate_vllm_invocation_evidence,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    RequestRateTraffic,
)
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import AdapterError
from inferdrome.execution import ProcessCapture, ProcessTermination
from inferdrome.resolution import ResolutionResult, resolve_experiment
from inferdrome.resolution.canonicalization import execution_fingerprint

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
SPIKE_FIXTURE = (
    REPOSITORY_ROOT
    / "spikes"
    / "vllm-0.26.0"
    / "fixtures"
    / "client-macos-empty"
)
RUN_ID = "run-88888888888888888888888888888888"


def _resolution() -> ResolutionResult:
    return resolve_experiment(FIXTURE_ROOT / "source.yaml", run_id=RUN_ID)


def _paths(tmp_path: Path) -> VllmInvocationPaths:
    result_directory = tmp_path / "native"
    result_directory.mkdir()
    return VllmInvocationPaths(
        dataset_path=(FIXTURE_ROOT / "workload.jsonl").resolve(),
        tokenizer_path=(
            REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
        ).resolve(),
        result_directory=result_directory.resolve(),
    )


def _preflight(resolution: ResolutionResult) -> EndpointPreflightCapture:
    target = resolution.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    response = b'{"data":[{"id":"inferdrome/mock-model"}]}'
    return preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=response,
        ),
    )


def _option_value(argv: tuple[str, ...], option: str) -> str:
    index = argv.index(option)
    return argv[index + 1]


def test_preflight_preserves_response_and_confirms_target_model() -> None:
    resolution = _resolution()
    assert isinstance(resolution.resolved_spec.target, AttachedVllmTarget)
    target = resolution.resolved_spec.target
    response_bytes = (
        b'{"object":"list","data":['
        b'{"id":"inferdrome/mock-model","object":"model"},'
        b'{"id":"another/model","object":"model"}]}'
    )
    calls: list[tuple[str, float, int]] = []

    def transport(url: str, timeout: float, limit: int) -> HttpResponse:
        calls.append((url, timeout, limit))
        return HttpResponse(status=200, body=response_bytes)

    capture = preflight_attached_endpoint(
        target,
        timeout_seconds=3,
        transport=transport,
    )

    assert calls == [("http://127.0.0.1:18083/v1/models", 3.0, 1_048_576)]
    assert capture.response_bytes == response_bytes
    assert capture.result.target_model == "inferdrome/mock-model"
    assert capture.result.server_reported_models == (
        "inferdrome/mock-model",
        "another/model",
    )
    assert capture.result.response_sha256 == sha256_digest(response_bytes)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (HttpResponse(status=503, body=b"{}"), "not successful"),
        (HttpResponse(status=200, body=b"[]"), "JSON object"),
        (HttpResponse(status=200, body=b'{"data":[]}'), "no model entries"),
        (
            HttpResponse(status=200, body=b'{"data":[{"id":"different"}]}'),
            "absent",
        ),
        (
            HttpResponse(
                status=200,
                body=b'{"data":[{"id":"inferdrome/mock-model"}],"data":[]}',
            ),
            "duplicate",
        ),
        (
            HttpResponse(
                status=200,
                body=b'{"data":[{"id":"inferdrome/mock-model"}],"x":NaN}',
            ),
            "non-finite",
        ),
    ],
)
def test_preflight_rejects_unusable_endpoint_evidence(
    response: HttpResponse,
    message: str,
) -> None:
    target = _resolution().resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)

    with pytest.raises(AdapterError, match=message):
        preflight_attached_endpoint(
            target,
            transport=lambda _url, _timeout, _limit: response,
        )


def test_invocation_round_trips_against_frozen_inputs(tmp_path: Path) -> None:
    resolution = _resolution()
    paths = _paths(tmp_path)
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        paths,
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    validated = validate_vllm_invocation_evidence(
        invocation.evidence_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        execution_fingerprint=resolution.execution_fingerprint,
    )

    assert invocation.argv[:3] == ("vllm", "bench", "serve")
    assert validated == invocation
    assert invocation.paths == paths
    assert _option_value(invocation.argv, "--base-url") == (
        "http://127.0.0.1:18083"
    )
    assert _option_value(invocation.argv, "--model") == "inferdrome/mock-model"
    assert _option_value(invocation.argv, "--num-prompts") == "4"
    assert _option_value(invocation.argv, "--num-warmups") == "2"
    assert _option_value(invocation.argv, "--request-rate") == "inf"
    assert _option_value(invocation.argv, "--max-concurrency") == "2"
    assert _option_value(invocation.argv, "--burstiness") == "1"
    assert "--save-detailed" in invocation.argv
    assert "--disable-shuffle" in invocation.argv
    assert "--skip-chat-template" in invocation.argv
    assert "--api-key" not in invocation.argv
    assert invocation.metadata == {
        "inferdrome_adapter_version": "1.0.0",
        "inferdrome_execution_fingerprint": resolution.execution_fingerprint,
        "inferdrome_producer_version": "0.26.0",
        "inferdrome_run_id": RUN_ID,
        "inferdrome_workload_sha256": resolution.resolved_spec.workload.sha256,
    }
    evidence = _invocation_value(invocation.evidence_bytes)
    assert evidence["endpoint_preflight"]["result"] == (
        invocation.preflight.result.model_dump(mode="json")
    )
    assert invocation.evidence_bytes == canonical_json_bytes(
        json.loads(invocation.evidence_bytes)
    )


def test_request_rate_invocation_uses_explicit_rate_controls(tmp_path: Path) -> None:
    resolution = _resolution()
    traffic = RequestRateTraffic(
        kind="request_rate",
        requests_per_second="3.5",
        burstiness="1.25",
        max_concurrency=7,
        warmup_requests=2,
        measured_requests=4,
    )
    spec = resolution.resolved_spec.model_copy(update={"traffic": traffic})
    plan = resolution.request_plan.model_copy(update={"traffic": traffic})
    fingerprint = execution_fingerprint(spec)
    invocation = build_vllm_invocation(
        spec,
        plan,
        _paths(tmp_path),
        execution_fingerprint=fingerprint,
        preflight=_preflight(resolution),
    )

    assert _option_value(invocation.argv, "--request-rate") == "3.5"
    assert _option_value(invocation.argv, "--burstiness") == "1.25"
    assert _option_value(invocation.argv, "--max-concurrency") == "7"


def _invocation_value(content: bytes) -> dict[str, Any]:
    value = json.loads(content)
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        "arguments",
        "metadata",
        "preflight_response",
        "preflight_result",
        "relative_path",
        "unknown_field",
    ],
)
def test_changed_invocation_evidence_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    value = _invocation_value(invocation.evidence_bytes)
    argv = value["argv"]
    if mutation == "arguments":
        argv[argv.index("--max-concurrency") + 1] = "3"
    elif mutation == "metadata":
        value["metadata"]["inferdrome_run_id"] = (
            "run-99999999999999999999999999999999"
        )
    elif mutation == "preflight_response":
        value["endpoint_preflight"]["response_base64"] = "e30="
    elif mutation == "preflight_result":
        value["endpoint_preflight"]["result"]["status"] = 201
    elif mutation == "relative_path":
        argv[argv.index("--dataset-path") + 1] = "relative.jsonl"
    else:
        value["extra"] = True

    with pytest.raises(AdapterError):
        validate_vllm_invocation_evidence(
            canonical_json_bytes(value),
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
        )


def test_noncanonical_and_invalid_fingerprint_invocations_fail(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    pretty = json.dumps(_invocation_value(invocation.evidence_bytes), indent=2).encode()

    with pytest.raises(AdapterError, match="canonical"):
        validate_vllm_invocation_evidence(
            pretty,
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
        )
    with pytest.raises(AdapterError, match="fingerprint"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            invocation.paths,
            execution_fingerprint="not-a-digest",
            preflight=_preflight(resolution),
        )


def test_builder_rejects_symlinked_material_inputs(tmp_path: Path) -> None:
    resolution = _resolution()
    paths = _paths(tmp_path)
    dataset_link = tmp_path / "workload-link.jsonl"
    dataset_link.symlink_to(paths.dataset_path)

    with pytest.raises(AdapterError, match="non-symlink"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=dataset_link,
                tokenizer_path=paths.tokenizer_path,
                result_directory=paths.result_directory,
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
        )


def test_builder_rejects_dataset_that_differs_from_frozen_workload(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    paths = _paths(tmp_path)
    changed_dataset = tmp_path / "changed-workload.jsonl"
    changed_dataset.write_bytes(paths.dataset_path.read_bytes() + b"\n")

    with pytest.raises(AdapterError, match="resolved workload"):
        build_vllm_invocation(
            resolution.resolved_spec,
            resolution.request_plan,
            VllmInvocationPaths(
                dataset_path=changed_dataset.resolve(),
                tokenizer_path=paths.tokenizer_path,
                result_directory=paths.result_directory,
            ),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
        )


def test_builder_rejects_request_sampling_that_differs_from_workload(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    first = resolution.request_plan.requests[0]
    changed_first = first.model_copy(
        update={
            "sampling": first.sampling.model_copy(
                update={"seed": first.sampling.seed + 1}
            )
        }
    )
    changed_plan = resolution.request_plan.model_copy(
        update={"requests": (changed_first, *resolution.request_plan.requests[1:])}
    )

    with pytest.raises(AdapterError, match="sampling"):
        build_vllm_invocation(
            resolution.resolved_spec,
            changed_plan,
            _paths(tmp_path),
            execution_fingerprint=resolution.execution_fingerprint,
            preflight=_preflight(resolution),
        )


def test_raw_pinned_version_output_is_preserved_and_identified() -> None:
    raw = (SPIKE_FIXTURE / "producer-version.txt").read_bytes()

    assert parse_vllm_version_output(raw) == "0.26.0+empty"
    assert parse_vllm_version_output(b"0.26.0\n") == "0.26.0"


@pytest.mark.parametrize(
    "raw",
    [
        b"0.25.0\n",
        b"0.26.0\n0.26.0+other\n",
        b"0.27.0\n0.26.0\n",
        b"vLLM 0.26.0\n",
        b"\xff\n",
    ],
)
def test_unpinned_or_ambiguous_version_output_fails(raw: bytes) -> None:
    with pytest.raises(AdapterError):
        parse_vllm_version_output(raw)


def _process_capture(
    argv: tuple[str, ...],
    *,
    exit_status: int = 0,
    termination: ProcessTermination = ProcessTermination.EXITED,
    stdout: bytes = b"producer stdout\n",
    stderr: bytes = b"",
) -> ProcessCapture:
    started_at = datetime(2026, 8, 5, 22, 0, tzinfo=UTC)
    return ProcessCapture(
        argv=argv,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
        exit_status=exit_status,
        termination=termination,
        stdout=stdout,
        stderr=stderr,
    )


def test_version_probe_preserves_raw_merged_output(tmp_path: Path) -> None:
    raw = (SPIKE_FIXTURE / "producer-version.txt").read_bytes()
    calls: list[dict[str, Any]] = []
    environment = {"VLLM_NO_USAGE_STATS": "1"}

    def runner(argv: tuple[str, ...], **kwargs: Any) -> ProcessCapture:
        calls.append(kwargs)
        return _process_capture(argv, stdout=raw)

    capture = probe_vllm_version(
        cwd=tmp_path.resolve(),
        process_runner=runner,
        environment=environment,
    )

    assert capture.observed_version == "0.26.0+empty"
    assert capture.process.stdout == raw
    assert capture.process.argv == ("vllm", "--version")
    assert calls[0]["merge_stderr"] is True
    assert calls[0]["output_limit_bytes"] == 65_536
    assert calls[0]["environment"] == environment


def test_benchmark_capture_preserves_untouched_native_and_streams(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    native_bytes = (
        SPIKE_FIXTURE / "native" / "benchmark-result.json"
    ).read_bytes()
    calls: list[dict[str, Any]] = []
    environment = {"VLLM_NO_USAGE_STATS": "1"}

    def runner(argv: tuple[str, ...], **kwargs: Any) -> ProcessCapture:
        calls.append(kwargs)
        result_path = Path(kwargs["cwd"]) / "benchmark-result.json"
        result_path.write_bytes(native_bytes)
        return _process_capture(
            argv,
            stdout=b"exact stdout\n",
            stderr=b"exact stderr\n",
        )

    capture = execute_vllm_benchmark(
        invocation,
        resolution.resolved_spec,
        resolution.request_plan,
        execution_fingerprint=resolution.execution_fingerprint,
        process_runner=runner,
        environment=environment,
    )

    assert capture.native_result_bytes == native_bytes
    assert capture.process.stdout == b"exact stdout\n"
    assert capture.process.stderr == b"exact stderr\n"
    assert capture.process.exit_status == 0
    assert calls[0]["max_runtime_seconds"] == 60
    assert calls[0]["merge_stderr"] is False
    assert calls[0]["cwd"] == invocation.paths.result_directory
    assert calls[0]["environment"] == environment


def test_failed_benchmark_capture_keeps_diagnostics_without_native_guess(
    tmp_path: Path,
) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessCapture:
        return _process_capture(
            argv,
            exit_status=9,
            stderr=b"benchmark failed\n",
        )

    capture = execute_vllm_benchmark(
        invocation,
        resolution.resolved_spec,
        resolution.request_plan,
        execution_fingerprint=resolution.execution_fingerprint,
        process_runner=runner,
    )

    assert capture.process.exit_status == 9
    assert capture.process.stderr == b"benchmark failed\n"
    assert capture.native_result_bytes is None


def test_success_without_native_output_is_rejected(tmp_path: Path) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )

    with pytest.raises(AdapterError, match="did not produce"):
        execute_vllm_benchmark(
            invocation,
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
            process_runner=lambda argv, **_kwargs: _process_capture(argv),
        )


def test_preexisting_native_output_is_never_overwritten(tmp_path: Path) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    native_path = invocation.paths.result_directory / "benchmark-result.json"
    native_path.write_bytes(b"preexisting")
    called = False

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessCapture:
        nonlocal called
        called = True
        return _process_capture(argv)

    with pytest.raises(AdapterError, match="already exists"):
        execute_vllm_benchmark(
            invocation,
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
            process_runner=runner,
        )
    assert not called


def test_dataset_replacement_during_execution_is_rejected(tmp_path: Path) -> None:
    resolution = _resolution()
    base_paths = _paths(tmp_path)
    dataset_path = tmp_path / "workload.jsonl"
    dataset_bytes = base_paths.dataset_path.read_bytes()
    dataset_path.write_bytes(dataset_bytes)
    paths = VllmInvocationPaths(
        dataset_path=dataset_path.resolve(),
        tokenizer_path=base_paths.tokenizer_path,
        result_directory=base_paths.result_directory,
    )
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        paths,
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessCapture:
        replacement = tmp_path / "replacement-workload.jsonl"
        replacement.write_bytes(dataset_bytes)
        replacement.replace(dataset_path)
        return _process_capture(argv, exit_status=9)

    with pytest.raises(AdapterError, match="changed during execution"):
        execute_vllm_benchmark(
            invocation,
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
            process_runner=runner,
        )


def test_native_same_size_rewrite_during_capture_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = _resolution()
    invocation = build_vllm_invocation(
        resolution.resolved_spec,
        resolution.request_plan,
        _paths(tmp_path),
        execution_fingerprint=resolution.execution_fingerprint,
        preflight=_preflight(resolution),
    )
    native_path = invocation.paths.result_directory / "benchmark-result.json"

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessCapture:
        native_path.write_bytes(b"a" * 2_000_000)
        return _process_capture(argv)

    original_read = vllm_bench_module.os.read
    changed = False

    def rewriting_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if content and not changed and native_path.exists():
            changed = True
            native_path.write_bytes(b"b" * 2_000_000)
        return content

    monkeypatch.setattr(vllm_bench_module.os, "read", rewriting_read)

    with pytest.raises(AdapterError, match="changed during capture"):
        execute_vllm_benchmark(
            invocation,
            resolution.resolved_spec,
            resolution.request_plan,
            execution_fingerprint=resolution.execution_fingerprint,
            process_runner=runner,
        )
