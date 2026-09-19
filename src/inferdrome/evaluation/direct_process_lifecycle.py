"""Exact-owned direct-process lifecycle for ordinary two-GPU container hosts.

This is deliberately separate from the reviewed Docker lifecycle.  It is for
an already-rented ordinary container that cannot safely run a nested daemon:
two pinned serving processes bind only literal loopback ports, one per GPU.
The owner knows only process groups it started itself.  It never rents a host,
discovers a provider, pulls an image, downloads a model, or treats the outer
container image declaration as an observed runtime attestation.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Literal, Protocol
from urllib.parse import urlsplit

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    engine_binding_bytes,
    engine_choice_sha256,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    AsyncioLocalSubprocessRunner,
    LocalSubprocessRunner,
    LoopbackTwoEndpointReadinessLifecycle,
    SubprocessResult,
    _assert_loopback_port_closed,
    _loopback_origin,
    _probe_loopback_warmup,
    _verify_pinned_model_snapshot,
)
from inferdrome.evaluation.sglang_container import verify_sglang_artifacts
from inferdrome.evaluation.sglang_profile import (
    SglangServingConfig,
    build_sglang_serving_profile,
)
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial
from inferdrome.execution.subprocess_runner import (
    ExecutableIdentity,
    resolve_executable_identity,
    validate_executable_identity,
)
from inferdrome.qwen3_campaign import QWEN3_8B_MODEL_ID, QWEN3_8B_REVISION
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

_ENDPOINTS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")
GpuIndex = Literal[0, 1]
_GPU_INDICES: tuple[GpuIndex, GpuIndex] = (0, 1)
_OWNERSHIP = re.compile(r"^[a-z][a-z0-9-]{2,23}$")
_COMMAND_TIMEOUT_NS = 30_000_000_000
_WARMUP_TIMEOUT_NS = 5_000_000_000
_STARTUP_TIMEOUT_NS = 120_000_000_000
_PROCESS_GRACE_NS = 5_000_000_000
_SUPERVISOR_START_TIMEOUT_NS = 5_000_000_000
_SUPERVISOR_MODULE = "inferdrome.evaluation.direct_process_supervisor"
_SUPERVISOR_READY = b"READY\n"


def executable_identity_sha256(identity: ExecutableIdentity) -> str:
    """Hash observed executable metadata without retaining its local path."""

    try:
        validate_executable_identity(identity)
        payload = {
            "schema_version": "inferdrome.direct-process-executable.v1",
            # File timestamps and inode values exceed RFC 8785's safe JSON
            # number domain on normal hosts; retain their exact decimal text.
            "device": str(identity.device),
            "inode": str(identity.inode),
            "mode": str(identity.mode),
            "links": str(identity.links),
            "size": str(identity.size),
            "modified_ns": str(identity.modified_ns),
            "changed_ns": str(identity.changed_ns),
        }
    except Exception as error:
        raise EvaluationError(
            "direct-process executable identity is unavailable"
        ) from error
    return sha256_digest(canonical_json_bytes(payload))


def resolve_direct_runtime(executable: str) -> ExecutableIdentity:
    """Read one local executable identity; this performs no process launch."""

    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise EvaluationError("direct-process runtime executable must be absolute")
    try:
        return resolve_executable_identity(executable)
    except Exception as error:
        raise EvaluationError(
            "direct-process runtime executable is unavailable"
        ) from error


@dataclass(frozen=True)
class DirectProcessLease:
    """One exact new process group returned only after a successful spawn."""

    pid: int
    process_group_id: int
    argv_sha256: str


class DirectProcessRunner(Protocol):
    """Injected no-shell process-group boundary used by the direct lifecycle."""

    async def start(
        self,
        executable: ExecutableIdentity,
        argv: tuple[str, ...],
        *,
        environment: Mapping[str, str],
    ) -> DirectProcessLease: ...

    async def terminate(
        self, lease: DirectProcessLease, *, timeout_ns: int
    ) -> int | None: ...


@dataclass
class _LiveProcess:
    lease: DirectProcessLease
    process: asyncio.subprocess.Process


class AsyncioDirectProcessRunner:
    """No-shell direct runner that kills only a newly-created process group.

    Standard output and error are deliberately not captured: a serving process
    must not turn its unbounded diagnostic stream into retained evidence.  A
    small private supervisor remains the group leader through an engine-leader
    exit, so the controller can still verify ownership before escalation.  A
    successful engine launch is the only way a lease enters the owner lifecycle.
    """

    def __init__(self) -> None:
        self._live: dict[int, _LiveProcess] = {}

    @staticmethod
    def _checked_environment(environment: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(environment, Mapping) or not environment:
            raise EvaluationError("direct-process environment is invalid")
        value = dict(environment)
        if any(
            type(name) is not str
            or type(item) is not str
            or not name
            or "=" in name
            or "\x00" in name
            or "\x00" in item
            or len(name) > 128
            or len(item) > 4096
            for name, item in value.items()
        ):
            raise EvaluationError("direct-process environment is invalid")
        return value

    async def start(
        self,
        executable: ExecutableIdentity,
        argv: tuple[str, ...],
        *,
        environment: Mapping[str, str],
    ) -> DirectProcessLease:
        try:
            validate_executable_identity(executable)
        except Exception as error:
            raise EvaluationError(
                "direct-process executable identity changed"
            ) from error
        if (
            not argv
            or argv[0] != str(executable.path)
            or any(
                type(value) is not str or not value or "\x00" in value for value in argv
            )
        ):
            raise EvaluationError("direct-process argv is invalid")
        checked_environment = self._checked_environment(environment)
        payload = canonical_json_bytes(
            {"argv": list(argv), "environment": checked_environment}
        )
        if len(payload) > 32_768:
            raise EvaluationError("direct-process argv is too large")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                _SUPERVISOR_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=checked_environment,
                start_new_session=True,
            )
            if process.stdin is None or process.stdout is None:
                raise EvaluationError("direct-process supervisor is unavailable")
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            status = await asyncio.wait_for(
                process.stdout.readline(), _SUPERVISOR_START_TIMEOUT_NS / 1_000_000_000
            )
            if status != _SUPERVISOR_READY:
                raise EvaluationError("direct-process engine could not start")
        except (OSError, TimeoutError) as error:
            if process is not None:
                await self._discard_unleased_process(process)
            raise EvaluationError("direct-process engine could not start") from error
        except BaseException:
            if process is not None:
                await self._discard_unleased_process(process)
            raise
        if process.pid is None or process.pid < 1:
            raise EvaluationError("direct-process engine did not return an exact pid")
        lease = DirectProcessLease(
            pid=process.pid,
            process_group_id=process.pid,
            argv_sha256=sha256_digest(canonical_json_bytes(list(argv))),
        )
        self._live[lease.pid] = _LiveProcess(lease, process)
        return lease

    @staticmethod
    async def _discard_unleased_process(process: asyncio.subprocess.Process) -> None:
        """Bound cleanup for a just-spawned supervisor that never received a lease."""

        if process.pid is None or process.returncode is not None:
            return
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return
        try:
            await asyncio.wait_for(
                process.wait(), _PROCESS_GRACE_NS / 1_000_000_000
            )
        except (TimeoutError, OSError):
            return

    async def terminate(
        self, lease: DirectProcessLease, *, timeout_ns: int
    ) -> int | None:
        if (
            type(lease) is not DirectProcessLease
            or type(timeout_ns) is not int
            or timeout_ns < 1
        ):
            raise EvaluationError("direct-process cleanup input is invalid")
        live = self._live.get(lease.pid)
        if live is None or live.lease != lease:
            raise EvaluationError("direct-process cleanup target is not owned")
        process = live.process
        cleanup_confirmed = False
        try:
            if process.returncode is not None:
                if self._process_group_is_absent(lease.process_group_id):
                    cleanup_confirmed = True
                    return process.returncode
                raise EvaluationError(
                    "direct-process group cleanup is unconfirmed after supervisor exit"
                )
            self._signal_owned_group(live, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=min(timeout_ns, _PROCESS_GRACE_NS) / 1_000_000_000,
                )
            except TimeoutError:
                self._signal_owned_group(live, signal.SIGKILL)
                await asyncio.wait_for(
                    process.wait(), timeout=timeout_ns / 1_000_000_000
                )
            if not await self._wait_for_process_group_absence(
                lease.process_group_id, timeout_ns=timeout_ns
            ):
                raise EvaluationError("direct-process group cleanup is unconfirmed")
            cleanup_confirmed = True
            return process.returncode
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError) as error:
            raise EvaluationError("direct-process cleanup is unconfirmed") from error
        finally:
            if cleanup_confirmed:
                self._live.pop(lease.pid, None)

    @staticmethod
    def _signal_owned_group(live: _LiveProcess, signal_value: signal.Signals) -> None:
        lease, process = live.lease, live.process
        if process.returncode is not None:
            raise EvaluationError("direct-process supervisor exited before cleanup")
        try:
            if os.getpgid(lease.pid) != lease.process_group_id:
                raise EvaluationError("direct-process group identity changed")
            os.killpg(lease.process_group_id, signal_value)
        except ProcessLookupError as error:
            raise EvaluationError(
                "direct-process group ownership is unavailable"
            ) from error

    @staticmethod
    def _process_group_is_absent(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        except PermissionError as error:
            raise EvaluationError(
                "direct-process group ownership cannot be verified"
            ) from error
        return False

    async def _wait_for_process_group_absence(
        self, process_group_id: int, *, timeout_ns: int
    ) -> bool:
        deadline_ns = monotonic_ns() + min(timeout_ns, _PROCESS_GRACE_NS)
        while not self._process_group_is_absent(process_group_id):
            if monotonic_ns() >= deadline_ns:
                return False
            await asyncio.sleep(0.05)
        return True


@dataclass(frozen=True)
class DirectProcessReceipt:
    """Sanitized one-process receipt; never retains argv, PID or local paths."""

    gpu_index: Literal[0, 1]
    argv_sha256: str
    executable_identity_sha256: str
    state: Literal["STARTED", "TERMINATED", "CLEANUP_UNCONFIRMED"]
    returncode: int | None


class _TwoEngineDirectProcessLifecycle:
    """Fresh exact-owned direct process pairs for one native study trial."""

    def __init__(
        self,
        origins: tuple[str, str],
        *,
        ownership_id: str,
        model_snapshot_path: Path,
        executable: ExecutableIdentity,
        command_runner: LocalSubprocessRunner | None = None,
        process_runner: DirectProcessRunner | None = None,
        warmup_probe: Callable[[str, float], None] = _probe_loopback_warmup,
        snapshot_verifier: Callable[[Path], None] = _verify_pinned_model_snapshot,
        port_closed_probe: Callable[[int, float], None] = _assert_loopback_port_closed,
        clock: Callable[[], int] = monotonic_ns,
        readiness_timeout_ns: int = 1_000_000_000,
        startup_timeout_ns: int = _STARTUP_TIMEOUT_NS,
    ) -> None:
        if (
            not _OWNERSHIP.fullmatch(ownership_id)
            or not model_snapshot_path.is_absolute()
            or model_snapshot_path.is_symlink()
            or type(readiness_timeout_ns) is not int
            or not 1 <= readiness_timeout_ns <= _COMMAND_TIMEOUT_NS
            or type(startup_timeout_ns) is not int
            or not readiness_timeout_ns <= startup_timeout_ns <= 300_000_000_000
        ):
            raise EvaluationError("direct-process lifecycle is invalid")
        try:
            executable_identity_sha256(executable)
        except EvaluationError:
            raise
        self._origins = tuple(_loopback_origin(origin) for origin in origins)
        if len(self._origins) != 2 or self._origins[0] == self._origins[1]:
            raise EvaluationError("direct-process lifecycle is invalid")
        self._readiness = LoopbackTwoEndpointReadinessLifecycle(
            (self._origins[0], self._origins[1]),
            readiness_timeout_ns=readiness_timeout_ns,
        )
        self._ownership_id = ownership_id
        self._model_snapshot_path = model_snapshot_path
        self._executable = executable
        self._executable_sha256 = executable_identity_sha256(executable)
        self._command_runner = command_runner or AsyncioLocalSubprocessRunner()
        self._process_runner = process_runner or AsyncioDirectProcessRunner()
        self._warmup_probe = warmup_probe
        self._snapshot_verifier = snapshot_verifier
        self._port_closed_probe = port_closed_probe
        self._clock = clock
        self._readiness_timeout_ns = readiness_timeout_ns
        self._startup_timeout_ns = startup_timeout_ns
        self._operation_deadline_ns: int | None = None
        self._snapshot_verified = False
        self._active: list[tuple[GpuIndex, DirectProcessLease]] = []
        self._receipts: list[DirectProcessReceipt] = []

    @property
    def required_operation_timeout_ns(self) -> int:
        return max(
            4 * _COMMAND_TIMEOUT_NS + self._startup_timeout_ns,
            8 * _COMMAND_TIMEOUT_NS,
        )

    @property
    def executable_identity_sha256(self) -> str:
        return self._executable_sha256

    @property
    def receipts(self) -> tuple[DirectProcessReceipt, ...]:
        return tuple(self._receipts)

    def set_operation_deadline(self, deadline_ns: int) -> None:
        if type(deadline_ns) is not int or deadline_ns < 1:
            raise EvaluationError("direct-process operation deadline is invalid")
        self._operation_deadline_ns = deadline_ns

    def _deadline(self) -> int:
        return (
            self._operation_deadline_ns
            or self._clock() + self.required_operation_timeout_ns
        )

    def _remaining(self, deadline_ns: int) -> int:
        remaining = deadline_ns - self._clock()
        if remaining < 1:
            raise EvaluationError("direct-process operation deadline expired")
        return min(_COMMAND_TIMEOUT_NS, remaining)

    def _bind_trial(self, trial: CompiledTrial) -> None:
        self._readiness._bound_to_trial(trial)

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        raise NotImplementedError

    def _engine_environment(self, *, index: int) -> Mapping[str, str]:
        if index not in (0, 1):
            raise EvaluationError("direct-process engine index is invalid")
        return {
            "PATH": os.defpath,
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "CUDA_VISIBLE_DEVICES": str(index),
        }

    async def _verify_artifacts(self, *, stop: asyncio.Event) -> None:
        del stop
        if (
            not self._model_snapshot_path.is_dir()
            or self._model_snapshot_path.is_symlink()
        ):
            raise EvaluationError("preloaded model snapshot is unavailable")
        if not self._snapshot_verified:
            self._snapshot_verifier(self._model_snapshot_path)
            self._snapshot_verified = True

    async def _command(
        self, argv: tuple[str, ...], *, deadline_ns: int
    ) -> SubprocessResult:
        result = await self._command_runner.run(
            argv, timeout_ns=self._remaining(deadline_ns)
        )
        if (
            result.argv != argv
            or result.returncode != 0
            or len(result.stdout) > 65_536
            or len(result.stderr) > 65_536
        ):
            raise EvaluationError("direct-process local command failed")
        return result

    async def _verify_gpu_idle(self, *, deadline_ns: int) -> None:
        for index in _GPU_INDICES:
            result = await self._command(
                (
                    "nvidia-smi",
                    f"--id={index}",
                    "--query-compute-apps=pid",
                    "--format=csv,noheader",
                ),
                deadline_ns=deadline_ns,
            )
            if result.stdout.strip():
                raise EvaluationError("declared GPU is not idle")

    async def _verify_ports_closed(self, *, deadline_ns: int) -> None:
        for origin in self._origins:
            port = urlsplit(origin).port
            assert port is not None
            remaining = self._remaining(deadline_ns)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._port_closed_probe, port, remaining / 1_000_000_000
                    ),
                    timeout=remaining / 1_000_000_000,
                )
            except TimeoutError as error:
                raise EvaluationError(
                    "declared serving-port readback is unconfirmed"
                ) from error

    async def _await_ready_and_warm(
        self, trial: CompiledTrial, stop: asyncio.Event
    ) -> None:
        deadline_ns = min(self._deadline(), self._clock() + self._startup_timeout_ns)
        delay = 0.05
        last_error: Exception | None = None
        while self._clock() < deadline_ns:
            if stop.is_set():
                raise EvaluationError("direct-process lifecycle was cancelled")
            try:
                await self._readiness.prepare(trial, stop=stop)
                timeout_ns = min(
                    self._remaining(deadline_ns),
                    self._readiness_timeout_ns,
                    _WARMUP_TIMEOUT_NS,
                )
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            asyncio.to_thread(
                                self._warmup_probe, origin, timeout_ns / 1_000_000_000
                            )
                            for origin in self._origins
                        )
                    ),
                    timeout=timeout_ns / 1_000_000_000,
                )
                return
            except (EvaluationError, OSError, TimeoutError) as error:
                last_error = error
            remaining = deadline_ns - self._clock()
            if remaining < 1:
                break
            await asyncio.sleep(min(delay, remaining / 1_000_000_000))
            delay = min(1.0, delay * 2)
        raise EvaluationError(
            "direct-process readiness deadline expired"
        ) from last_error

    async def _remove_active(self, *, deadline_ns: int) -> None:
        errors: list[Exception] = []
        for index, lease in tuple(reversed(self._active)):
            try:
                returncode = await self._process_runner.terminate(
                    lease, timeout_ns=self._remaining(deadline_ns)
                )
                self._active.remove((index, lease))
                self._receipts.append(
                    DirectProcessReceipt(
                        index,
                        lease.argv_sha256,
                        self._executable_sha256,
                        "TERMINATED",
                        returncode,
                    )
                )
            except Exception as error:
                self._receipts.append(
                    DirectProcessReceipt(
                        index,
                        lease.argv_sha256,
                        self._executable_sha256,
                        "CLEANUP_UNCONFIRMED",
                        None,
                    )
                )
                errors.append(error)
        for readback in (self._verify_ports_closed, self._verify_gpu_idle):
            try:
                await readback(deadline_ns=deadline_ns)
            except Exception as error:
                errors.append(error)
        if self._active:
            errors.append(EvaluationError("direct-process engine remains unresolved"))
        if errors:
            raise EvaluationError("direct-process cleanup is unconfirmed") from errors[
                0
            ]

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        if stop.is_set():
            raise EvaluationError("direct-process lifecycle was cancelled")
        self._bind_trial(trial)
        if self._active:
            raise EvaluationError("direct-process lifecycle has an active trial")
        await self._verify_artifacts(stop=stop)
        deadline_ns = self._deadline()
        await self._verify_ports_closed(deadline_ns=deadline_ns)
        await self._verify_gpu_idle(deadline_ns=deadline_ns)
        try:
            for index in _GPU_INDICES:
                if stop.is_set():
                    raise EvaluationError("direct-process lifecycle was cancelled")
                argv = self._engine_argv(trial, index=index)
                lease = await self._process_runner.start(
                    self._executable,
                    argv,
                    environment=self._engine_environment(index=index),
                )
                if type(
                    lease
                ) is not DirectProcessLease or lease.argv_sha256 != sha256_digest(
                    canonical_json_bytes(list(argv))
                ):
                    raise EvaluationError(
                        "direct-process engine did not return an exact lease"
                    )
                self._active.append((index, lease))
                self._receipts.append(
                    DirectProcessReceipt(
                        index,
                        lease.argv_sha256,
                        self._executable_sha256,
                        "STARTED",
                        None,
                    )
                )
            await self._await_ready_and_warm(trial, stop)
        except BaseException as error:
            try:
                await self._remove_active(deadline_ns=deadline_ns)
            except Exception as cleanup_error:
                raise EvaluationError(
                    "direct-process startup failed and cleanup is unconfirmed"
                ) from cleanup_error
            if isinstance(error, asyncio.CancelledError):
                raise
            raise EvaluationError("direct-process startup or warmup failed") from error

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        del stop
        self._bind_trial(trial)
        await self._remove_active(deadline_ns=self._deadline())


class TwoEngineVllmDirectProcessLifecycle(_TwoEngineDirectProcessLifecycle):
    """Pinned vLLM direct process pair for an ordinary two-GPU container."""

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        if index not in (0, 1):
            raise EvaluationError("direct-process engine index is invalid")
        parsed = urlsplit(self._origins[index])
        assert parsed.port is not None
        return (
            str(self._executable.path),
            "serve",
            str(self._model_snapshot_path),
            "--served-model-name",
            QWEN3_8B_MODEL_ID,
            "--revision",
            QWEN3_8B_REVISION,
            "--tokenizer",
            str(self._model_snapshot_path),
            "--tokenizer-revision",
            QWEN3_8B_REVISION,
            "--dtype",
            "bfloat16",
            "--tensor-parallel-size",
            "1",
            "--gpu-memory-utilization",
            "0.90",
            "--max-model-len",
            "2048",
            "--host",
            "127.0.0.1",
            "--port",
            str(parsed.port),
            "--disable-log-requests",
        )


class TwoEngineSglangDirectProcessLifecycle(_TwoEngineDirectProcessLifecycle):
    """Pinned SGLang direct process pair using the same exact group ownership.

    Each trial receives newly-started engines, so the cold-reset declaration is
    backed by process replacement rather than an in-place cache flush.  The
    existing SGLang binding still owns the model, profile and cache declaration
    checks; this lifecycle only maps its already-declared argv onto two local
    executable process groups.
    """

    def __init__(
        self,
        profiles: Mapping[EndpointId, SglangServingConfig],
        *,
        contexts: tuple[tuple[CompiledStudy, EvaluationEngineBinding], ...],
        ownership_id: str,
        executable: ExecutableIdentity,
        command_runner: LocalSubprocessRunner | None = None,
        process_runner: DirectProcessRunner | None = None,
        artifact_verifier: Callable[
            [SglangServingConfig], object
        ] = verify_sglang_artifacts,
        warmup_probe: Callable[[str, float], None] = _probe_loopback_warmup,
        port_closed_probe: Callable[[int, float], None] = _assert_loopback_port_closed,
        clock: Callable[[], int] = monotonic_ns,
        readiness_timeout_ns: int = 1_000_000_000,
        startup_timeout_ns: int = _STARTUP_TIMEOUT_NS,
    ) -> None:
        if set(profiles) != set(_ENDPOINTS) or not 1 <= len(contexts) <= 32:
            raise EvaluationError("direct SGLang lifecycle context is invalid")
        self._profiles = dict(profiles)
        self._profiles_by_index = tuple(
            build_sglang_serving_profile(self._profiles[endpoint])
            for endpoint in _ENDPOINTS
        )
        self._contexts = tuple(
            (
                plan,
                load_engine_binding_bytes(
                    engine_binding_bytes(binding), plan, profiles=self._profiles
                ),
            )
            for plan, binding in contexts
        )
        choices = {engine_choice_sha256(binding) for _, binding in self._contexts}
        if len(choices) != 1 or any(
            binding.execution_mode != "NATIVE_PROCESS" for _, binding in self._contexts
        ):
            raise EvaluationError("direct SGLang lifecycle requires one native choice")
        self._engine_choice_sha256 = choices.pop()
        self._artifact_verifier = artifact_verifier
        origins = tuple(self._profiles[endpoint].origin for endpoint in _ENDPOINTS)
        super().__init__(
            (origins[0], origins[1]),
            ownership_id=ownership_id,
            model_snapshot_path=Path(self._profiles["endpoint-a"].model_path),
            executable=executable,
            command_runner=command_runner,
            process_runner=process_runner,
            warmup_probe=warmup_probe,
            # SGLang has a separately pinned model/tokenizer/template verifier.
            snapshot_verifier=lambda _path: None,
            port_closed_probe=port_closed_probe,
            clock=clock,
            readiness_timeout_ns=readiness_timeout_ns,
            startup_timeout_ns=startup_timeout_ns,
        )

    @property
    def engine_choice_sha256(self) -> str:
        return self._engine_choice_sha256

    def _bind_trial(self, trial: CompiledTrial) -> None:
        super()._bind_trial(trial)
        for plan, binding in self._contexts:
            if trial in plan.trials:
                checked = load_engine_binding_bytes(
                    engine_binding_bytes(binding), plan, profiles=self._profiles
                )
                validate_engine_binding_trial(checked, plan, trial)
                if engine_choice_sha256(checked) != self._engine_choice_sha256:
                    raise EvaluationError("direct SGLang engine choice changed")
                return
        raise EvaluationError("direct SGLang trial has no prebound engine context")

    async def _verify_artifacts(self, *, stop: asyncio.Event) -> None:
        if stop.is_set():
            raise EvaluationError("direct SGLang lifecycle was cancelled")
        # Verify immediately before every fresh pair.  This remains a local
        # file readback, not a claim that a loaded runtime is immutable.
        for endpoint in _ENDPOINTS:
            if stop.is_set():
                raise EvaluationError("direct SGLang lifecycle was cancelled")
            try:
                await asyncio.to_thread(
                    self._artifact_verifier, self._profiles[endpoint]
                )
            except Exception as error:
                raise EvaluationError(
                    "direct SGLang artifacts are unavailable"
                ) from error

    def _engine_environment(self, *, index: int) -> Mapping[str, str]:
        environment = dict(super()._engine_environment(index=index))
        environment.update(dict(self._profiles_by_index[index].environment))
        return environment

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        del trial
        if index not in (0, 1):
            raise EvaluationError("direct-process engine index is invalid")
        profile = self._profiles_by_index[index]
        # The source profile argv is an exact finite allowlist.  The executable
        # path is separately observed and revalidated at spawn time.
        return (str(self._executable.path), *profile.argv[1:])
