"""Executable orchestration converges on one verified evidence format."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import inferdrome.execution.orchestrator as orchestrator
from inferdrome.adapters.vllm_bench import (
    HttpResponse,
    VllmBenchmarkCapture,
    VllmVersionProbeCapture,
    preflight_attached_endpoint,
)
from inferdrome.bundle import verify_bundle
from inferdrome.domain.experiment import AttachedVllmTarget
from inferdrome.domain.states import (
    EnvironmentCompleteness,
    EvidenceEligibility,
    RunState,
)
from inferdrome.errors import AdapterError, CancellationRequested
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
)
from inferdrome.execution.subprocess_runner import (
    ProcessCapture,
    ProcessTermination,
)
from inferdrome.resolution import resolve_experiment
from inferdrome.workspace import RunWorkspace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_RUN_ID = "run-cccccccccccccccccccccccccccccccc"
VLLM_RUN_ID = "run-dddddddddddddddddddddddddddddddd"
FAILED_RUN_ID = "run-ffffffffffffffffffffffffffffffff"
INTERRUPTED_RUN_ID = "run-abababababababababababababababab"


def test_fake_orchestrator_executes_seals_and_verifies(tmp_path: Path) -> None:
    result = orchestrator.run_experiment(
        REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
        runs_root=tmp_path / "runs",
        run_id=FAKE_RUN_ID,
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.run_id == FAKE_RUN_ID
    assert report.descriptor.evidence_eligibility is (
        EvidenceEligibility.SYNTHETIC_ONLY
    )
    assert result.workspace.current_state().state is RunState.COMPLETE


def test_attached_vllm_orchestrator_preserves_real_shape_without_network_or_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
    source_path = fixture_root / "source.yaml"
    preview = resolve_experiment(source_path, run_id=VLLM_RUN_ID)
    target = preview.resolved_spec.target
    assert isinstance(target, AttachedVllmTarget)
    preflight_response = json.dumps(
        {"data": [{"id": target.model}]}, separators=(",", ":")
    ).encode()
    preflight = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=preflight_response,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "preflight_attached_endpoint",
        lambda _target: preflight,
    )

    started_at = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    ended_at = datetime(2026, 8, 6, 2, 1, tzinfo=UTC)
    version_process = ProcessCapture(
        argv=("vllm", "--version"),
        started_at=started_at,
        ended_at=started_at,
        exit_status=0,
        termination=ProcessTermination.EXITED,
        stdout=b"0.26.0\n",
        stderr=b"",
    )
    monkeypatch.setattr(
        orchestrator,
        "probe_vllm_version",
        lambda **_kwargs: VllmVersionProbeCapture(
            process=version_process,
            observed_version="0.26.0",
        ),
    )

    spike_native_path = (
        REPOSITORY_ROOT
        / "spikes"
        / "vllm-0.26.0"
        / "fixtures"
        / "client-macos-empty"
        / "native"
        / "benchmark-result.json"
    )

    def execute(invocation: object, *_args: object, **_kwargs: object) -> object:
        metadata = dict(invocation.metadata)  # type: ignore[attr-defined]
        tokenizer_path = str(invocation.paths.tokenizer_path)  # type: ignore[attr-defined]
        native = json.loads(spike_native_path.read_bytes())
        for key in tuple(native):
            if key.startswith("inferdrome_"):
                native.pop(key)
        native.update(metadata)
        native["tokenizer_id"] = tokenizer_path
        native_bytes = json.dumps(
            native,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        process = ProcessCapture(
            argv=invocation.argv,  # type: ignore[attr-defined]
            started_at=started_at,
            ended_at=ended_at,
            exit_status=0,
            termination=ProcessTermination.EXITED,
            stdout=b"benchmark complete\n",
            stderr=b"",
        )
        return VllmBenchmarkCapture(
            invocation=invocation,  # type: ignore[arg-type]
            process=process,
            native_result_bytes=native_bytes,
        )

    monkeypatch.setattr(orchestrator, "execute_vllm_benchmark", execute)
    result = orchestrator.run_experiment(
        source_path,
        runs_root=tmp_path / "runs",
        run_id=VLLM_RUN_ID,
        tokenizer_path=(
            REPOSITORY_ROOT / "spikes" / "vllm-0.26.0" / "tokenizer"
        ),
    )

    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )
    assert report.descriptor.evidence_eligibility is EvidenceEligibility.INELIGIBLE
    assert report.descriptor.environment_completeness is (
        EnvironmentCompleteness.PARTIAL
    )
    assert result.workspace.current_state().state is RunState.COMPLETE
    capture_directory = result.workspace.path / "native-capture"
    assert (capture_directory / "invocation.json").read_bytes()
    assert (capture_directory / "producer-version.txt").read_bytes() == b"0.26.0\n"
    assert (capture_directory / "stdout.log").read_bytes() == (
        b"benchmark complete\n"
    )
    assert (capture_directory / "stderr.log").read_bytes() == b""
    assert (capture_directory / "exit-status.txt").read_bytes() == b"0\n"


def test_invalid_run_option_does_not_reserve_workspace(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    with pytest.raises(AdapterError, match="tokenizer path is only valid"):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=FAKE_RUN_ID,
            tokenizer_path=tmp_path,
        )

    assert not runs_root.exists()


def test_execution_failure_terminalizes_reserved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_execution(*_args: object, **_kwargs: object) -> object:
        raise AdapterError("synthetic producer failed")

    monkeypatch.setattr(orchestrator.FakeAdapter, "execute", fail_execution)
    runs_root = tmp_path / "runs"
    with pytest.raises(AdapterError, match="synthetic producer failed"):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=FAILED_RUN_ID,
        )

    workspace = RunWorkspace.open(runs_root / FAILED_RUN_ID)
    assert workspace.current_state().state is RunState.FAILED
    assert not (workspace.path / "bundle").exists()


def test_cooperative_cancellation_terminalizes_workspace_as_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = CancellationToken()
    original_execute = orchestrator.FakeAdapter.execute

    def execute_and_cancel(*args: object, **kwargs: object) -> object:
        result = original_execute(*args, **kwargs)  # type: ignore[arg-type]
        cancellation.request(CancellationReason.USER)
        return result

    monkeypatch.setattr(orchestrator.FakeAdapter, "execute", execute_and_cancel)
    runs_root = tmp_path / "runs"
    with pytest.raises(CancellationRequested):
        orchestrator.run_experiment(
            REPOSITORY_ROOT / "examples" / "fake-smoke.yaml",
            runs_root=runs_root,
            run_id=INTERRUPTED_RUN_ID,
            cancellation=cancellation,
        )

    workspace = RunWorkspace.open(runs_root / INTERRUPTED_RUN_ID)
    assert workspace.current_state().state is RunState.INTERRUPTED
    assert not (workspace.path / "bundle").exists()
