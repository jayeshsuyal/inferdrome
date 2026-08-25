"""Deterministic provider/runtime lifecycle coverage for the local adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from inferdrome.deployment import (
    CleanupResult,
    LifecycleCoordinator,
    LifecycleErrorCode,
    LifecycleOutcome,
    LifecyclePhase,
    LifecycleStatus,
    LifecycleValidationError,
    LocalMockRuntimeAdapter,
    LocalProviderAdapter,
    canonical_lifecycle_outcome_bytes,
    parse_deployment_spec_json,
)
from inferdrome.errors import CancellationRequested
from inferdrome.execution.cancellation import CancellationReason, CancellationToken

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPOSITORY_ROOT / "deployments" / "v1" / "examples"


def _payload(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE_ROOT / name).read_text(encoding="utf-8"))


def _spec(name: str = "local-mock.json"):
    return parse_deployment_spec_json(json.dumps(_payload(name), allow_nan=False))


def _phases(outcome: LifecycleOutcome) -> tuple[LifecyclePhase, ...]:
    return tuple(event.phase for event in outcome.trace)


def test_local_mock_lifecycle_is_synthetic_and_cleans_once() -> None:
    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()
    observed: list[str] = []

    outcome = LifecycleCoordinator(provider, runtime).run(
        _spec(),
        lambda endpoint: observed.append(
            f"{endpoint.scheme}://{endpoint.host}:{endpoint.port}{endpoint.path}"
        ),
    )

    assert outcome.status is LifecycleStatus.SUCCEEDED
    assert outcome.synthetic_only is True
    assert outcome.evidence_eligible is False
    assert _phases(outcome) == (
        LifecyclePhase.VALIDATION,
        LifecyclePhase.PROVIDER_ACQUISITION,
        LifecyclePhase.RUNTIME_START,
        LifecyclePhase.RUNTIME_READINESS,
        LifecyclePhase.BENCHMARK,
        LifecyclePhase.RUNTIME_STOP,
        LifecyclePhase.PROVIDER_CLEANUP,
        LifecyclePhase.PROVIDER_FINAL_CONFIRMATION,
    )
    assert observed == ["http://127.0.0.1:8000/v1"]
    assert runtime.start_count == 1
    assert runtime.readiness_count == 1
    assert runtime.stop_count == 1
    assert provider.acquire_count == 1
    assert provider.cleanup_count == 1
    assert provider.confirmation_count == 1
    assert outcome.provider_cleanup_confirmed
    assert outcome.provider_final_confirmation
    assert not outcome.orphaned


def test_local_outcome_serialization_is_deterministic_and_not_a_receipt() -> None:
    left = LifecycleCoordinator(
        LocalProviderAdapter(), LocalMockRuntimeAdapter()
    ).run(_spec(), lambda endpoint: None)
    right = LifecycleCoordinator(
        LocalProviderAdapter(), LocalMockRuntimeAdapter()
    ).run(_spec(), lambda endpoint: None)

    assert canonical_lifecycle_outcome_bytes(left) == canonical_lifecycle_outcome_bytes(
        right
    )
    assert b"receipt" not in canonical_lifecycle_outcome_bytes(left).lower()
    assert "deployment" not in left.model_dump(mode="json")


def test_unsupported_provider_fails_before_any_side_effect() -> None:
    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()

    outcome = LifecycleCoordinator(provider, runtime).run(
        _spec("lambda-dry-run-reference.json"),
        lambda endpoint: None,
    )

    assert outcome.status is LifecycleStatus.FAILED
    assert outcome.error_code is LifecycleErrorCode.UNSUPPORTED_PROVIDER
    assert _phases(outcome) == (LifecyclePhase.VALIDATION,)
    assert provider.acquire_count == 0
    assert provider.cleanup_count == 0
    assert provider.confirmation_count == 0
    assert runtime.start_count == 0


@pytest.mark.parametrize("adapter", ["provider", "runtime"])
def test_adapter_capability_mismatch_fails_before_acquisition(
    adapter: str,
) -> None:
    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()
    if adapter == "provider":
        provider.provider_id = "gcp"
    else:
        runtime.engine_id = "sglang"

    outcome = LifecycleCoordinator(provider, runtime).run(
        _spec(),
        lambda endpoint: None,
    )

    assert outcome.status is LifecycleStatus.FAILED
    assert outcome.error_code is LifecycleErrorCode.CAPABILITY_MISMATCH
    assert _phases(outcome) == (LifecyclePhase.VALIDATION,)
    assert provider.acquire_count == 0
    assert provider.cleanup_count == 0
    assert runtime.start_count == 0


def test_sglang_is_rejected_by_runtime_capability_before_execution() -> None:
    payload = _payload("lambda-dry-run-reference.json")
    payload["mode"] = "development"
    payload["runtime"].update(
        {
            "engine": "sglang",
            "engine_version": "0.4.0",
            "adapter": "sglang_reference_v1",
        }
    )
    spec = parse_deployment_spec_json(json.dumps(payload, allow_nan=False))

    with pytest.raises(LifecycleValidationError) as exc_info:
        LocalMockRuntimeAdapter().validate(spec)
    assert exc_info.value.code is LifecycleErrorCode.UNSUPPORTED_RUNTIME


class _FaultProvider(LocalProviderAdapter):
    def __init__(self, fault: str | None, token: CancellationToken | None = None):
        super().__init__()
        self.fault = fault
        self.token = token

    def validate(self, spec):
        if self.fault == "validation":
            raise LifecycleValidationError(LifecycleErrorCode.CAPABILITY_MISMATCH)
        super().validate(spec)

    def acquire(self, spec):
        if self.fault == "provider_acquisition":
            self.acquire_count += 1
            raise RuntimeError("provider payload sk-SECRET-MUST-NOT-APPEAR")
        handle = super().acquire(spec)
        if self.token is not None:
            self.token.request(CancellationReason.USER)
        return handle

    def cleanup(self, spec, handle):
        if self.fault == "provider_cleanup":
            self.cleanup_count += 1
            raise RuntimeError("cleanup payload ghp_SECRET-MUST-NOT-APPEAR")
        if self.fault == "cleanup_orphan":
            self.cleanup_count += 1
            return CleanupResult(
                confirmed=False,
                orphaned=True,
                error_code=LifecycleErrorCode.CLEANUP_UNCONFIRMED,
            )
        return super().cleanup(spec, handle)

    def confirm_cleanup(self, spec, handle):
        if self.fault == "provider_final_confirmation":
            self.confirmation_count += 1
            raise RuntimeError("confirmation payload AKIASECRET")
        return super().confirm_cleanup(spec, handle)


class _FaultRuntime(LocalMockRuntimeAdapter):
    def __init__(self, fault: str | None):
        super().__init__()
        self.fault = fault

    def start(self, spec, provider, *, process_control):
        if self.fault == "runtime_start":
            self.start_count += 1
            raise RuntimeError("runtime payload -----BEGIN PRIVATE KEY-----")
        return super().start(spec, provider, process_control=process_control)

    def wait_ready(self, spec, runtime, *, readiness, clock):
        if self.fault == "runtime_readiness":
            self.readiness_count += 1
            return False
        return super().wait_ready(
            spec,
            runtime,
            readiness=readiness,
            clock=clock,
        )

    def stop(self, spec, runtime, *, process_control):
        if self.fault == "runtime_stop":
            self.stop_count += 1
            raise RuntimeError("stop payload github_pat_SECRET")
        return super().stop(spec, runtime, process_control=process_control)


@pytest.mark.parametrize(
    ("fault", "expected_error", "runtime_stop", "provider_cleanup"),
    [
        ("validation", LifecycleErrorCode.CAPABILITY_MISMATCH, 0, 0),
        ("provider_acquisition", LifecycleErrorCode.PROVIDER_ACQUIRE_FAILED, 0, 1),
        ("runtime_start", LifecycleErrorCode.RUNTIME_START_FAILED, 1, 1),
        ("runtime_readiness", LifecycleErrorCode.READINESS_FAILED, 1, 1),
        ("benchmark", LifecycleErrorCode.BENCHMARK_FAILED, 1, 1),
        ("runtime_stop", LifecycleErrorCode.RUNTIME_STOP_FAILED, 1, 1),
        ("provider_cleanup", LifecycleErrorCode.PROVIDER_CLEANUP_FAILED, 1, 1),
        (
            "provider_final_confirmation",
            LifecycleErrorCode.CLEANUP_UNCONFIRMED,
            1,
            1,
        ),
    ],
)
def test_fault_injection_attempts_applicable_cleanup_once(
    fault: str,
    expected_error: LifecycleErrorCode,
    runtime_stop: int,
    provider_cleanup: int,
) -> None:
    provider = _FaultProvider(fault)
    runtime = _FaultRuntime(fault)

    def benchmark(endpoint):
        if fault == "benchmark":
            raise RuntimeError("benchmark payload sk-SECRET-MUST-NOT-APPEAR")

    outcome = LifecycleCoordinator(provider, runtime).run(_spec(), benchmark)

    assert outcome.status is LifecycleStatus.FAILED
    assert outcome.error_code is expected_error
    assert runtime.stop_count == runtime_stop
    assert provider.cleanup_count == provider_cleanup
    assert provider.confirmation_count == provider_cleanup
    assert outcome.runtime_stop_attempted is (runtime_stop == 1)
    assert outcome.provider_cleanup_attempted is (provider_cleanup == 1)
    assert "SECRET" not in canonical_lifecycle_outcome_bytes(outcome).decode()


def test_keyboard_interrupt_and_base_exception_are_cleaned_without_payloads() -> None:
    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()
    interrupted = LifecycleCoordinator(provider, runtime).run(
        _spec(), lambda endpoint: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    assert interrupted.status is LifecycleStatus.INTERRUPTED
    assert interrupted.error_code is LifecycleErrorCode.INTERRUPTED
    assert provider.cleanup_count == 1
    assert runtime.stop_count == 1

    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()
    base_exception = LifecycleCoordinator(provider, runtime).run(
        _spec(), lambda endpoint: (_ for _ in ()).throw(SystemExit("secret"))
    )

    assert base_exception.status is LifecycleStatus.FAILED
    assert base_exception.error_code is LifecycleErrorCode.BASE_EXCEPTION
    assert provider.cleanup_count == 1
    assert runtime.stop_count == 1
    assert b"secret" not in canonical_lifecycle_outcome_bytes(base_exception)


def test_cancellation_after_provider_acquisition_cleans_provider_once() -> None:
    token = CancellationToken()
    provider = _FaultProvider(None, token)
    runtime = LocalMockRuntimeAdapter()

    outcome = LifecycleCoordinator(provider, runtime).run(
        _spec(), lambda endpoint: None, cancellation=token
    )

    assert outcome.status is LifecycleStatus.CANCELLED
    assert outcome.error_code is LifecycleErrorCode.CANCELLED
    assert provider.cleanup_count == 1
    assert provider.confirmation_count == 1
    assert runtime.start_count == 0
    assert not outcome.runtime_stop_attempted


def test_cleanup_failure_and_orphan_block_success() -> None:
    provider = _FaultProvider("cleanup_orphan")
    outcome = LifecycleCoordinator(provider, LocalMockRuntimeAdapter()).run(
        _spec(), lambda endpoint: None
    )

    assert outcome.status is LifecycleStatus.FAILED
    assert outcome.error_code is LifecycleErrorCode.CLEANUP_UNCONFIRMED
    assert not outcome.provider_cleanup_confirmed
    assert outcome.provider_final_confirmation
    assert outcome.orphaned


def test_repeated_local_cleanup_is_idempotent_and_bounded() -> None:
    provider = LocalProviderAdapter()
    spec = _spec()

    first = provider.cleanup(spec, None)
    second = provider.cleanup(spec, None)
    confirmed = provider.confirm_cleanup(spec, None)

    assert first == second == CleanupResult(confirmed=True)
    assert confirmed == CleanupResult(confirmed=True)
    assert provider.cleanup_count == 2
    assert provider.confirmation_count == 1


def test_process_readiness_and_clock_boundaries_are_injected() -> None:
    class ProcessBoundary:
        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0

        def start(self, *, command, environment):
            assert command == ()
            assert environment == {}
            self.started += 1
            return "in-memory-process"

        def stop(self, process):
            assert process == "in-memory-process"
            self.stopped += 1
            return True

    class TestClock:
        def __init__(self) -> None:
            self.calls = 0

        def monotonic(self) -> float:
            self.calls += 1
            return 10.0

    class Readiness:
        def __init__(self) -> None:
            self.calls = 0

        def wait_ready(self, endpoint, *, timeout_seconds, clock) -> bool:
            assert endpoint.host == "127.0.0.1"
            assert timeout_seconds == 30
            assert clock.monotonic() == 10.0
            self.calls += 1
            return True

    process = ProcessBoundary()
    clock = TestClock()
    readiness = Readiness()
    outcome = LifecycleCoordinator(
        LocalProviderAdapter(),
        LocalMockRuntimeAdapter(),
        process_control=process,
        readiness=readiness,
        clock=clock,
    ).run(_spec(), lambda endpoint: None)

    assert outcome.status is LifecycleStatus.SUCCEEDED
    assert process.started == 1
    assert process.stopped == 1
    assert readiness.calls == 1
    assert clock.calls == 1


def test_cancellation_exception_is_classified_without_echoing_message() -> None:
    provider = LocalProviderAdapter()
    runtime = LocalMockRuntimeAdapter()

    def benchmark(endpoint):
        raise CancellationRequested("secret cancellation payload")

    outcome = LifecycleCoordinator(provider, runtime).run(_spec(), benchmark)

    assert outcome.status is LifecycleStatus.CANCELLED
    assert outcome.error_code is LifecycleErrorCode.CANCELLED
    assert "secret cancellation payload" not in str(outcome)
