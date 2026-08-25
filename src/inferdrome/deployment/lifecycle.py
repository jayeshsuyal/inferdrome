"""Bounded deployment lifecycle interfaces and the deterministic local adapter.

This module is an execution-side boundary around :mod:`inferdrome.deployment`.
It deliberately does not own benchmark methodology, evidence records, bundle
sealing, or acceptance.  The lifecycle result is an ephemeral in-memory
diagnostic; PR 4 owns the immutable deployment receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Final, Literal, Protocol, TypeVar

from pydantic import Field

from inferdrome.deployment.spec import DeploymentSpec
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import AdapterError, CancellationRequested
from inferdrome.execution.cancellation import CancellationToken


class LifecyclePhase(StrEnum):
    """The only phases a deployment lifecycle may enter."""

    VALIDATION = "VALIDATION"
    PROVIDER_ACQUISITION = "PROVIDER_ACQUISITION"
    RUNTIME_START = "RUNTIME_START"
    RUNTIME_READINESS = "RUNTIME_READINESS"
    BENCHMARK = "BENCHMARK"
    RUNTIME_STOP = "RUNTIME_STOP"
    PROVIDER_CLEANUP = "PROVIDER_CLEANUP"
    PROVIDER_FINAL_CONFIRMATION = "PROVIDER_FINAL_CONFIRMATION"


class LifecycleStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class LifecycleEventResult(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class LifecycleErrorCode(StrEnum):
    INVALID_SPEC = "INVALID_SPEC"
    INVALID_CALLBACK = "INVALID_CALLBACK"
    UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
    UNSUPPORTED_RUNTIME = "UNSUPPORTED_RUNTIME"
    UNSUPPORTED_INTENT = "UNSUPPORTED_INTENT"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    PROVIDER_ACQUIRE_FAILED = "PROVIDER_ACQUIRE_FAILED"
    RUNTIME_START_FAILED = "RUNTIME_START_FAILED"
    READINESS_FAILED = "READINESS_FAILED"
    BENCHMARK_FAILED = "BENCHMARK_FAILED"
    RUNTIME_STOP_FAILED = "RUNTIME_STOP_FAILED"
    PROVIDER_CLEANUP_FAILED = "PROVIDER_CLEANUP_FAILED"
    CLEANUP_UNCONFIRMED = "CLEANUP_UNCONFIRMED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    BASE_EXCEPTION = "BASE_EXCEPTION"


class LifecycleValidationError(AdapterError):
    """A bounded capability rejection raised before any side effect."""

    def __init__(self, code: LifecycleErrorCode) -> None:
        self.code = code
        super().__init__(f"deployment lifecycle rejected: {code.value}")


@dataclass(frozen=True)
class ProviderHandle:
    """Opaque provider state retained only for the current lifecycle."""

    handle_id: str


@dataclass(frozen=True)
class RuntimeEndpoint:
    """The private/loopback port handed to the unchanged benchmark callback."""

    scheme: Literal["http", "https"]
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class RuntimeHandle:
    """Opaque runtime state retained only for the current lifecycle."""

    handle_id: str
    endpoint: RuntimeEndpoint
    process: object | None = None


@dataclass(frozen=True)
class CleanupResult:
    """Bounded adapter response for stop, cleanup, or final confirmation."""

    confirmed: bool
    orphaned: bool = False
    error_code: LifecycleErrorCode | None = None


class Clock(Protocol):
    """Monotonic clock injected into readiness and lifecycle tests."""

    def monotonic(self) -> float: ...


class ProcessController(Protocol):
    """Process boundary injected by a real runtime adapter in a later slice."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> object: ...

    def stop(self, process: object) -> bool: ...


class ReadinessProbe(Protocol):
    """Readiness boundary; network probing is not part of this local slice."""

    def wait_ready(
        self,
        endpoint: RuntimeEndpoint,
        *,
        timeout_seconds: int,
        clock: Clock,
    ) -> bool: ...


type BenchmarkCallback = Callable[[RuntimeEndpoint], object]
_PhaseValue = TypeVar("_PhaseValue")


class ProviderAdapter(Protocol):
    """Provider resource lifecycle, independent of serving and measurement."""

    provider_id: str

    def validate(self, spec: DeploymentSpec) -> None: ...

    def acquire(self, spec: DeploymentSpec) -> ProviderHandle: ...

    def cleanup(
        self,
        spec: DeploymentSpec,
        handle: ProviderHandle | None,
    ) -> CleanupResult: ...

    def confirm_cleanup(
        self,
        spec: DeploymentSpec,
        handle: ProviderHandle | None,
    ) -> CleanupResult: ...


class RuntimeAdapter(Protocol):
    """Serving-engine lifecycle, independent of provider and benchmark policy."""

    engine_id: str

    def validate(self, spec: DeploymentSpec) -> None: ...

    def start(
        self,
        spec: DeploymentSpec,
        provider: ProviderHandle,
        *,
        process_control: ProcessController,
    ) -> RuntimeHandle: ...

    def wait_ready(
        self,
        spec: DeploymentSpec,
        runtime: RuntimeHandle,
        *,
        readiness: ReadinessProbe,
        clock: Clock,
    ) -> bool: ...

    def stop(
        self,
        spec: DeploymentSpec,
        runtime: RuntimeHandle | None,
        *,
        process_control: ProcessController,
    ) -> CleanupResult: ...


class LifecycleTraceEvent(FrozenModel):
    """One bounded state-machine event; no adapter payload is retained."""

    sequence_index: int = Field(strict=True, ge=0, le=15)
    phase: LifecyclePhase
    result: LifecycleEventResult
    error_code: LifecycleErrorCode | None


class LifecycleOutcome(FrozenModel):
    """Ephemeral, synthetic-only lifecycle result; not a deployment receipt."""

    schema_version: Literal["inferdrome.lifecycle-result.v1"]
    status: LifecycleStatus
    synthetic_only: Literal[True]
    evidence_eligible: Literal[False]
    terminal_phase: LifecyclePhase
    error_code: LifecycleErrorCode | None
    primary_error_code: LifecycleErrorCode | None
    cleanup_error_code: LifecycleErrorCode | None
    trace: tuple[LifecycleTraceEvent, ...] = Field(min_length=1, max_length=16)
    runtime_stop_attempted: bool
    runtime_stop_confirmed: bool
    provider_cleanup_attempted: bool
    provider_cleanup_confirmed: bool
    provider_final_confirmation_attempted: bool
    provider_final_confirmation: bool
    orphaned: bool


def lifecycle_outcome_json_value(outcome: LifecycleOutcome) -> dict[str, object]:
    """Return the bounded JSON value for deterministic diagnostic inspection."""

    value = outcome.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("lifecycle outcome root must serialize as an object")
    return value


def canonical_lifecycle_outcome_bytes(outcome: LifecycleOutcome) -> bytes:
    """Canonicalize an ephemeral outcome without creating a receipt or digest."""

    return canonical_json_bytes(lifecycle_outcome_json_value(outcome))


class _SystemClock:
    def monotonic(self) -> float:
        return monotonic()


class _NoopProcessController:
    """In-memory process boundary used by the local mock adapter."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> object:
        if command or environment:
            raise AssertionError("local mock process boundary must be empty")
        return object()

    def stop(self, process: object) -> bool:
        return True


class _ImmediateReadinessProbe:
    def wait_ready(
        self,
        endpoint: RuntimeEndpoint,
        *,
        timeout_seconds: int,
        clock: Clock,
    ) -> bool:
        return True


class LocalProviderAdapter:
    """Provider adapter that provisions no resource and performs no I/O."""

    provider_id = "local"

    def __init__(self) -> None:
        self.acquire_count = 0
        self.cleanup_count = 0
        self.confirmation_count = 0

    def validate(self, spec: DeploymentSpec) -> None:
        if spec.provider.provider_id != self.provider_id:
            raise LifecycleValidationError(LifecycleErrorCode.CAPABILITY_MISMATCH)
        if spec.execution_intent not in {"mock_only", "local_execute"}:
            raise LifecycleValidationError(LifecycleErrorCode.UNSUPPORTED_INTENT)

    def acquire(self, spec: DeploymentSpec) -> ProviderHandle:
        self.acquire_count += 1
        return ProviderHandle(handle_id="local-no-resource")

    def cleanup(
        self,
        spec: DeploymentSpec,
        handle: ProviderHandle | None,
    ) -> CleanupResult:
        self.cleanup_count += 1
        return CleanupResult(confirmed=True)

    def confirm_cleanup(
        self,
        spec: DeploymentSpec,
        handle: ProviderHandle | None,
    ) -> CleanupResult:
        self.confirmation_count += 1
        return CleanupResult(confirmed=True)


class LocalMockRuntimeAdapter:
    """Synthetic vLLM-shaped runtime with no CUDA, subprocess, or network use."""

    engine_id = "vllm"

    def __init__(
        self,
        *,
        readiness: ReadinessProbe | None = None,
    ) -> None:
        self._readiness = readiness
        self.start_count = 0
        self.readiness_count = 0
        self.stop_count = 0

    def validate(self, spec: DeploymentSpec) -> None:
        if spec.runtime.engine != self.engine_id:
            raise LifecycleValidationError(LifecycleErrorCode.UNSUPPORTED_RUNTIME)
        if spec.execution_intent != "mock_only":
            raise LifecycleValidationError(LifecycleErrorCode.UNSUPPORTED_INTENT)
        if spec.mode != "development" or spec.resources.gpu_count != 0:
            raise LifecycleValidationError(LifecycleErrorCode.CAPABILITY_MISMATCH)

    def start(
        self,
        spec: DeploymentSpec,
        provider: ProviderHandle,
        *,
        process_control: ProcessController,
    ) -> RuntimeHandle:
        self.start_count += 1
        process = process_control.start(command=(), environment={})
        endpoint_config = spec.runtime.endpoint
        endpoint = RuntimeEndpoint(
            scheme=endpoint_config.scheme,
            host=endpoint_config.host,
            port=endpoint_config.port,
            path=endpoint_config.path,
        )
        return RuntimeHandle(
            handle_id="local-mock-runtime",
            endpoint=endpoint,
            process=process,
        )

    def wait_ready(
        self,
        spec: DeploymentSpec,
        runtime: RuntimeHandle,
        *,
        readiness: ReadinessProbe,
        clock: Clock,
    ) -> bool:
        self.readiness_count += 1
        selected_readiness = (
            self._readiness or readiness or _ImmediateReadinessProbe()
        )
        return selected_readiness.wait_ready(
            runtime.endpoint,
            timeout_seconds=spec.timeouts.startup_seconds,
            clock=clock,
        )

    def stop(
        self,
        spec: DeploymentSpec,
        runtime: RuntimeHandle | None,
        *,
        process_control: ProcessController,
    ) -> CleanupResult:
        self.stop_count += 1
        if runtime is None or runtime.process is None:
            return CleanupResult(confirmed=True)
        return CleanupResult(confirmed=process_control.stop(runtime.process))


_PHASE_DEFAULT_ERRORS: Final[dict[LifecyclePhase, LifecycleErrorCode]] = {
    LifecyclePhase.VALIDATION: LifecycleErrorCode.CAPABILITY_MISMATCH,
    LifecyclePhase.PROVIDER_ACQUISITION: LifecycleErrorCode.PROVIDER_ACQUIRE_FAILED,
    LifecyclePhase.RUNTIME_START: LifecycleErrorCode.RUNTIME_START_FAILED,
    LifecyclePhase.RUNTIME_READINESS: LifecycleErrorCode.READINESS_FAILED,
    LifecyclePhase.BENCHMARK: LifecycleErrorCode.BENCHMARK_FAILED,
    LifecyclePhase.RUNTIME_STOP: LifecycleErrorCode.RUNTIME_STOP_FAILED,
    LifecyclePhase.PROVIDER_CLEANUP: LifecycleErrorCode.PROVIDER_CLEANUP_FAILED,
    LifecyclePhase.PROVIDER_FINAL_CONFIRMATION: LifecycleErrorCode.CLEANUP_UNCONFIRMED,
}


class _LifecycleFailure(Exception):
    def __init__(self, code: LifecycleErrorCode) -> None:
        self.code = code


def _error_code(exc: BaseException, fallback: LifecycleErrorCode) -> LifecycleErrorCode:
    if isinstance(exc, _LifecycleFailure):
        return exc.code
    if isinstance(exc, LifecycleValidationError):
        return exc.code
    if isinstance(exc, CancellationRequested):
        return LifecycleErrorCode.CANCELLED
    if isinstance(exc, KeyboardInterrupt):
        return LifecycleErrorCode.INTERRUPTED
    if isinstance(exc, Exception):
        return fallback
    return LifecycleErrorCode.BASE_EXCEPTION


def _status_for_error(code: LifecycleErrorCode) -> LifecycleStatus:
    if code is LifecycleErrorCode.CANCELLED:
        return LifecycleStatus.CANCELLED
    if code is LifecycleErrorCode.INTERRUPTED:
        return LifecycleStatus.INTERRUPTED
    return LifecycleStatus.FAILED


def _require_cleanup_result(
    value: object,
    fallback: LifecycleErrorCode,
) -> CleanupResult:
    if not isinstance(value, CleanupResult):
        raise _LifecycleFailure(fallback)
    if type(value.confirmed) is not bool or type(value.orphaned) is not bool:
        raise _LifecycleFailure(fallback)
    if value.error_code is not None and not isinstance(
        value.error_code,
        LifecycleErrorCode,
    ):
        raise _LifecycleFailure(fallback)
    if value.confirmed:
        if value.orphaned or value.error_code is not None:
            raise _LifecycleFailure(fallback)
        return value
    if value.error_code not in {fallback, LifecycleErrorCode.CLEANUP_UNCONFIRMED}:
        raise _LifecycleFailure(fallback)
    raise _LifecycleFailure(value.error_code)


class LifecycleCoordinator:
    """Run one bounded provider/runtime lifecycle around an injected benchmark."""

    def __init__(
        self,
        provider: ProviderAdapter,
        runtime: RuntimeAdapter,
        *,
        process_control: ProcessController | None = None,
        readiness: ReadinessProbe | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._provider = provider
        self._runtime = runtime
        self._process_control = process_control or _NoopProcessController()
        self._readiness = readiness or _ImmediateReadinessProbe()
        self._clock = clock or _SystemClock()

    def validate(self, spec: DeploymentSpec) -> None:
        """Validate exact provider/runtime capability before any side effect."""

        if not isinstance(spec, DeploymentSpec):
            raise LifecycleValidationError(LifecycleErrorCode.INVALID_SPEC)
        provider_id = spec.provider.provider_id
        runtime_id = spec.runtime.engine
        if provider_id != "local":
            raise LifecycleValidationError(LifecycleErrorCode.UNSUPPORTED_PROVIDER)
        if runtime_id != "vllm":
            raise LifecycleValidationError(LifecycleErrorCode.UNSUPPORTED_RUNTIME)
        if self._provider.provider_id != provider_id:
            raise LifecycleValidationError(LifecycleErrorCode.CAPABILITY_MISMATCH)
        if self._runtime.engine_id != runtime_id:
            raise LifecycleValidationError(LifecycleErrorCode.CAPABILITY_MISMATCH)
        self._provider.validate(spec)
        self._runtime.validate(spec)

    def run(
        self,
        spec: DeploymentSpec,
        benchmark: BenchmarkCallback,
        *,
        cancellation: CancellationToken | None = None,
    ) -> LifecycleOutcome:
        trace: list[LifecycleTraceEvent] = []
        provider_handle: ProviderHandle | None = None
        runtime_handle: RuntimeHandle | None = None
        provider_attempted = False
        runtime_start_attempted = False
        runtime_stop_attempted = False
        provider_cleanup_attempted = False
        provider_final_confirmation_attempted = False
        runtime_stop_confirmed = True
        provider_cleanup_confirmed = True
        provider_final_confirmation = True
        orphaned = False
        primary_error_code: LifecycleErrorCode | None = None
        cleanup_error_code: LifecycleErrorCode | None = None
        current_phase = LifecyclePhase.VALIDATION

        def record(
            phase: LifecyclePhase,
            result: LifecycleEventResult,
            error_code: LifecycleErrorCode | None = None,
        ) -> None:
            trace.append(
                LifecycleTraceEvent(
                    sequence_index=len(trace),
                    phase=phase,
                    result=result,
                    error_code=error_code,
                )
            )

        def phase(
            selected: LifecyclePhase,
            operation: Callable[[], _PhaseValue],
        ) -> _PhaseValue:
            nonlocal current_phase
            current_phase = selected
            try:
                value = operation()
            except BaseException as exc:
                code = _error_code(exc, _PHASE_DEFAULT_ERRORS[selected])
                record(selected, LifecycleEventResult.FAILED, code)
                raise
            record(selected, LifecycleEventResult.SUCCEEDED)
            return value

        def check_cancellation() -> object:
            if cancellation is not None:
                cancellation.raise_if_requested()
            return None

        def validate_inputs() -> object:
            check_cancellation()
            self.validate(spec)
            if not callable(benchmark):
                raise _LifecycleFailure(LifecycleErrorCode.INVALID_CALLBACK)
            return None

        def acquire_provider() -> ProviderHandle:
            nonlocal provider_attempted
            check_cancellation()
            provider_attempted = True
            handle = self._provider.acquire(spec)
            if not isinstance(handle, ProviderHandle):
                raise _LifecycleFailure(LifecycleErrorCode.PROVIDER_ACQUIRE_FAILED)
            return handle

        def start_runtime() -> RuntimeHandle:
            nonlocal runtime_handle, runtime_start_attempted
            check_cancellation()
            if provider_handle is None:
                raise _LifecycleFailure(LifecycleErrorCode.RUNTIME_START_FAILED)
            runtime_start_attempted = True
            handle = self._runtime.start(
                spec,
                provider_handle,
                process_control=self._process_control,
            )
            if not isinstance(handle, RuntimeHandle):
                raise _LifecycleFailure(LifecycleErrorCode.RUNTIME_START_FAILED)
            # Retain the returned handle before checking its postcondition so
            # cleanup can stop exactly the runtime the adapter actually made.
            runtime_handle = handle
            expected_endpoint = RuntimeEndpoint(
                scheme=spec.runtime.endpoint.scheme,
                host=spec.runtime.endpoint.host,
                port=spec.runtime.endpoint.port,
                path=spec.runtime.endpoint.path,
            )
            if handle.endpoint != expected_endpoint:
                raise _LifecycleFailure(LifecycleErrorCode.RUNTIME_START_FAILED)
            return handle

        def wait_for_readiness() -> bool:
            check_cancellation()
            if runtime_handle is None:
                raise _LifecycleFailure(LifecycleErrorCode.READINESS_FAILED)
            ready = self._runtime.wait_ready(
                spec,
                runtime_handle,
                readiness=self._readiness,
                clock=self._clock,
            )
            if ready is not True:
                raise _LifecycleFailure(LifecycleErrorCode.READINESS_FAILED)
            return ready

        def run_benchmark() -> object:
            check_cancellation()
            if runtime_handle is None:
                raise _LifecycleFailure(LifecycleErrorCode.BENCHMARK_FAILED)
            return benchmark(runtime_handle.endpoint)

        try:
            phase(LifecyclePhase.VALIDATION, validate_inputs)
            provider_handle = phase(
                LifecyclePhase.PROVIDER_ACQUISITION,
                acquire_provider,
            )
            if not isinstance(provider_handle, ProviderHandle):
                raise _LifecycleFailure(LifecycleErrorCode.PROVIDER_ACQUIRE_FAILED)
            runtime_handle = phase(LifecyclePhase.RUNTIME_START, start_runtime)
            if not isinstance(runtime_handle, RuntimeHandle):
                raise _LifecycleFailure(LifecycleErrorCode.RUNTIME_START_FAILED)
            phase(LifecyclePhase.RUNTIME_READINESS, wait_for_readiness)
            phase(LifecyclePhase.BENCHMARK, run_benchmark)
        except BaseException as exc:
            primary_error_code = _error_code(
                exc,
                _PHASE_DEFAULT_ERRORS.get(
                    current_phase,
                    LifecycleErrorCode.BASE_EXCEPTION,
                ),
            )
        finally:
            if runtime_start_attempted or runtime_handle is not None:
                runtime_stop_attempted = True
                try:
                    result = phase(
                        LifecyclePhase.RUNTIME_STOP,
                        lambda: _require_cleanup_result(
                            self._runtime.stop(
                                spec,
                                runtime_handle,
                                process_control=self._process_control,
                            ),
                            LifecycleErrorCode.RUNTIME_STOP_FAILED,
                        ),
                    )
                    runtime_stop_confirmed = result.confirmed and not result.orphaned
                    orphaned = orphaned or result.orphaned
                except BaseException as exc:
                    runtime_stop_confirmed = False
                    orphaned = True
                    code = _error_code(exc, LifecycleErrorCode.RUNTIME_STOP_FAILED)
                    if cleanup_error_code is None:
                        cleanup_error_code = code

            if provider_attempted or provider_handle is not None:
                provider_cleanup_attempted = True
                try:
                    result = phase(
                        LifecyclePhase.PROVIDER_CLEANUP,
                        lambda: _require_cleanup_result(
                            self._provider.cleanup(spec, provider_handle),
                            LifecycleErrorCode.PROVIDER_CLEANUP_FAILED,
                        ),
                    )
                    provider_cleanup_confirmed = (
                        result.confirmed and not result.orphaned
                    )
                    orphaned = orphaned or result.orphaned
                except BaseException as exc:
                    provider_cleanup_confirmed = False
                    orphaned = True
                    code = _error_code(exc, LifecycleErrorCode.PROVIDER_CLEANUP_FAILED)
                    if cleanup_error_code is None:
                        cleanup_error_code = code

                provider_final_confirmation_attempted = True
                try:
                    result = phase(
                        LifecyclePhase.PROVIDER_FINAL_CONFIRMATION,
                        lambda: _require_cleanup_result(
                            self._provider.confirm_cleanup(
                                spec,
                                provider_handle,
                            ),
                            LifecycleErrorCode.CLEANUP_UNCONFIRMED,
                        ),
                    )
                    provider_final_confirmation = (
                        result.confirmed and not result.orphaned
                    )
                    orphaned = orphaned or result.orphaned
                except BaseException as exc:
                    provider_final_confirmation = False
                    orphaned = True
                    code = _error_code(exc, LifecycleErrorCode.CLEANUP_UNCONFIRMED)
                    if cleanup_error_code is None:
                        cleanup_error_code = code

            cleanup_confirmed = (
                runtime_stop_confirmed
                and provider_cleanup_confirmed
                and provider_final_confirmation
            )
            if not cleanup_confirmed and cleanup_error_code is None:
                cleanup_error_code = LifecycleErrorCode.CLEANUP_UNCONFIRMED

        effective_error_code = cleanup_error_code or primary_error_code
        if cleanup_error_code is not None:
            status = LifecycleStatus.FAILED
        elif primary_error_code is not None:
            status = _status_for_error(primary_error_code)
        else:
            status = LifecycleStatus.SUCCEEDED

        terminal_phase = trace[-1].phase if trace else LifecyclePhase.VALIDATION
        return LifecycleOutcome(
            schema_version="inferdrome.lifecycle-result.v1",
            status=status,
            synthetic_only=True,
            evidence_eligible=False,
            terminal_phase=terminal_phase,
            error_code=effective_error_code,
            primary_error_code=primary_error_code,
            cleanup_error_code=cleanup_error_code,
            trace=tuple(trace),
            runtime_stop_attempted=runtime_stop_attempted,
            runtime_stop_confirmed=runtime_stop_confirmed,
            provider_cleanup_attempted=provider_cleanup_attempted,
            provider_cleanup_confirmed=provider_cleanup_confirmed,
            provider_final_confirmation_attempted=provider_final_confirmation_attempted,
            provider_final_confirmation=provider_final_confirmation,
            orphaned=orphaned,
        )


__all__ = [
    "BenchmarkCallback",
    "CleanupResult",
    "Clock",
    "LifecycleCoordinator",
    "LifecycleErrorCode",
    "LifecycleEventResult",
    "LifecycleOutcome",
    "LifecyclePhase",
    "LifecycleStatus",
    "LifecycleTraceEvent",
    "LifecycleValidationError",
    "LocalMockRuntimeAdapter",
    "LocalProviderAdapter",
    "ProcessController",
    "ProviderAdapter",
    "ProviderHandle",
    "ReadinessProbe",
    "RuntimeAdapter",
    "RuntimeEndpoint",
    "RuntimeHandle",
    "canonical_lifecycle_outcome_bytes",
    "lifecycle_outcome_json_value",
]
