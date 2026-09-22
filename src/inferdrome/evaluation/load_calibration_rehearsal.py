"""Local-only execution bridge for the frozen load-calibration protocol.

The bridge deliberately reuses native ``StudyConfig`` compilation, ``run_study``
execution, trial artifacts, and offline reports.  It does not construct a new
router or alter historical study-report classifications.  Callers supply every
candidate recipe and an owned lifecycle before any trial runs; the helper then
binds the protocol declarations to exact compiled trial IDs.
"""

from __future__ import annotations

import asyncio
import errno
import json
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Annotated, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import Field, ValidationError, model_validator

from inferdrome.evaluation.contracts import ClosedModel, EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    engine_binding_bytes,
    engine_binding_sha256,
    engine_choice_sha256,
)
from inferdrome.evaluation.files import OutputFile, read_input
from inferdrome.evaluation.load_calibration import (
    CalibrationObservationSet,
    CalibrationSelection,
    CalibrationTrial,
    CalibrationTrialObservation,
    CompiledCalibrationPlan,
    ConfirmationPlan,
    ConfirmationTrial,
    LoadCalibrationProtocol,
    LoadLevel,
    calibration_plan_bytes,
    compile_calibration,
    compile_confirmation_trials,
    confirmation_plan,
    select_calibration_level,
)
from inferdrome.evaluation.report_reader import (
    StudyReport,
    load_evaluation_report_bytes,
)
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.sglang_rehearsal import bind_sglang_rehearsal
from inferdrome.evaluation.sglang_report import (
    MAX_SGLANG_REPORT_BYTES,
    read_sglang_report_bytes,
)
from inferdrome.evaluation.sglang_results import load_sglang_trial_bytes
from inferdrome.evaluation.study import (
    StudyExecutor,
    StudyManifest,
    TrialResult,
    load_study_trial_bytes,
    plan_bytes,
    read_manifest,
    report_study,
    run_study,
)
from inferdrome.evaluation.study_config import (
    CompiledStudy,
    CompiledTrial,
    StudyConfig,
    compile_study,
)
from inferdrome.evaluation.study_files import StudyDirectory, trial_filename
from inferdrome.evaluation.study_report import summarize_trial
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import (
    VLLM_RUNTIME_IMAGE_REFERENCE,
    _require_model_snapshot,
)

_SIDECAR_OUTPUT_BYTES = 1_048_576
_REPORT_OUTPUT_BYTES = 2 * 4 * 1024 * 1024
_CONTROL_OUTPUT_BYTES = 4 * _SIDECAR_OUTPUT_BYTES
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_LEVEL_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SAFE_OWNERSHIP_ID = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_READINESS_BODY_BYTES = 32_768
_SUBPROCESS_OUTPUT_BYTES = 65_536
_LOCAL_ENGINE_COMMAND_TIMEOUT_NS = 30_000_000_000
_LOCAL_ENGINE_WARMUP_TIMEOUT_NS = 5_000_000_000
_LOCAL_ENGINE_STARTUP_TIMEOUT_NS = 120_000_000_000
_SESSION_FINAL_CLEANUP_RESERVE_NS = 5_000_000_000
_SESSION_RETRIEVAL_RESERVE_NS = 5_000_000_000


class TrialLifecycle(Protocol):
    """Caller-owned reset/warmup and cleanup around one native study trial."""

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None: ...

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None: ...


@dataclass(frozen=True)
class RehearsalSessionWindow:
    """An already-started bounded session supplied by a host-local owner.

    The native default remains unchanged when no window is supplied.  A caller
    that stages an approved host before execution can supply its original
    monotonic start and a non-renewable external cutoff rather than minting a
    new full session at dispatch time.
    """

    started_ns: int
    hard_deadline_ns: int

    def validated_for(self, protocol: LoadCalibrationProtocol, *, now_ns: int) -> None:
        if (
            type(self.started_ns) is not int
            or type(self.hard_deadline_ns) is not int
            or type(now_ns) is not int
            or self.started_ns < 0
            or self.hard_deadline_ns <= self.started_ns
            or now_ns < self.started_ns
            or now_ns >= self.hard_deadline_ns
            or self.hard_deadline_ns - self.started_ns
            > protocol.max_session_duration_ns
        ):
            raise EvaluationError("rehearsal session window is invalid")


class _NoRedirect(HTTPRedirectHandler):
    """Never let a local readiness probe follow an endpoint-controlled redirect."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _loopback_origin(value: str) -> str:
    """Accept only one canonical no-path loopback origin for the local helper."""

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise EvaluationError("loopback readiness origin is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc != f"127.0.0.1:{port}"
    ):
        raise EvaluationError("loopback readiness origin is invalid")
    return value


def _probe_loopback_readiness(origin: str, path: str, timeout_seconds: float) -> None:
    """Make one proxy-free bounded GET without retaining endpoint response bytes."""

    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(origin + path, headers={"Accept": "text/plain"})
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise ValueError
            response.read(_READINESS_BODY_BYTES)
    except (HTTPError, OSError, URLError, ValueError):
        raise EvaluationError("loopback readiness is unavailable") from None


class LoopbackTwoEndpointReadinessLifecycle:
    """Concrete CPU-local readiness boundary for exactly two already-owned fakes.

    It deliberately does not claim to reset a serving engine: vLLM has no
    portable reset API.  A separate lifecycle owner must perform any declared
    reset/cleanup operation.  This helper makes the local two-endpoint path
    non-noop by revalidating the fixed pair's health and metrics before every
    native trial, with no proxy or redirect escape.
    """

    def __init__(
        self,
        origins: tuple[str, str],
        *,
        readiness_timeout_ns: int = 1_000_000_000,
    ) -> None:
        if (
            len(origins) != 2
            or origins[0] == origins[1]
            or type(readiness_timeout_ns) is not int
            or not 1 <= readiness_timeout_ns <= 5_000_000_000
        ):
            raise EvaluationError("loopback readiness lifecycle is invalid")
        self._origins = tuple(_loopback_origin(origin) for origin in origins)
        self._timeout_seconds = readiness_timeout_ns / 1_000_000_000

    def _bound_to_trial(self, trial: CompiledTrial) -> None:
        observed = tuple(
            endpoint.origin for endpoint in trial.config.foreground.endpoints
        )
        if observed != self._origins:
            raise EvaluationError("loopback readiness target differs from trial")

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        if stop.is_set():
            raise EvaluationError("loopback readiness was cancelled")
        self._bound_to_trial(trial)
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    _probe_loopback_readiness, origin, path, self._timeout_seconds
                )
                for origin in self._origins
                for path in ("/health", "/metrics")
            )
        )

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        # Cleanup must remain callable after cancellation or a failed prepare.
        del stop
        self._bound_to_trial(trial)


@dataclass(frozen=True)
class SubprocessResult:
    """One bounded local command result, retained only in process memory."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class LocalSubprocessRunner(Protocol):
    """Injected command boundary for the local two-engine lifecycle."""

    async def run(
        self, argv: tuple[str, ...], *, timeout_ns: int
    ) -> SubprocessResult: ...


@dataclass
class _OwnedEngine:
    """One exact-owned engine target, retained until its exact ID is absent."""

    name: str
    attempt_label: str
    gpu_index: int
    port: int
    container_id: str | None = None


class _SubprocessOutputLimit(Exception):
    """A local command tried to exceed the fixed in-memory output bound."""


async def _read_subprocess_stream(stream: asyncio.StreamReader) -> bytes:
    content = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > _SUBPROCESS_OUTPUT_BYTES:
            raise _SubprocessOutputLimit


class AsyncioLocalSubprocessRunner:
    """Concrete no-shell local command runner for an explicitly invoked rehearsal.

    Constructing this runner is inert.  It never discovers a daemon, pulls an
    image, or launches a process until a caller explicitly uses the two-engine
    lifecycle.  Tests inject a CPU-only fake runner, so CI does not require
    Docker, a model, or an NVIDIA device.
    """

    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        if environment is not None and (
            not isinstance(environment, Mapping)
            or any(
                type(key) is not str or type(value) is not str
                for key, value in environment.items()
            )
        ):
            raise EvaluationError("local subprocess environment is invalid")
        self._environment = None if environment is None else dict(environment)

    async def run(self, argv: tuple[str, ...], *, timeout_ns: int) -> SubprocessResult:
        if not argv or type(timeout_ns) is not int or timeout_ns < 1:
            raise EvaluationError("local subprocess command is invalid")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment,
            )
        except OSError as error:
            raise EvaluationError("local subprocess could not start") from error
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_read_subprocess_stream(process.stdout))
        stderr_task = asyncio.create_task(_read_subprocess_stream(process.stderr))
        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, process.wait()),
                timeout=_operation_timeout_seconds(timeout_ns=timeout_ns),
            )
        except (Exception, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise EvaluationError("local subprocess did not complete safely") from error
        return SubprocessResult(
            argv=argv,
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
        )


def _probe_loopback_warmup(origin: str, timeout_seconds: float) -> None:
    """Run one bounded fixed-shape local vLLM warmup without retaining output."""

    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(
        origin + "/v1/chat/completions",
        data=(
            b'{"model":"Qwen/Qwen3-8B","messages":[{"role":"user",'
            b'"content":"Inferdrome local warmup."}],"max_tokens":1,'
            b'"stream":true,"temperature":0}'
        ),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise ValueError
            response.read(_READINESS_BODY_BYTES)
    except (HTTPError, OSError, URLError, ValueError):
        raise EvaluationError("loopback warmup is unavailable") from None


def _verify_pinned_model_snapshot(path: Path) -> None:
    """Reuse the reviewed no-download Qwen3 snapshot verification boundary."""

    try:
        _require_model_snapshot(str(path))
    except Exception as error:
        raise EvaluationError("preloaded model snapshot is unavailable") from error


def _assert_loopback_port_closed(port: int, timeout_seconds: float) -> None:
    """Fail if a declared local serving port still accepts a connection."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout_seconds)
            result = probe.connect_ex(("127.0.0.1", port))
    except OSError as error:
        raise EvaluationError("local serving-port readback is unavailable") from error
    if result == 0:
        raise EvaluationError("declared local serving port is still open")
    if result != errno.ECONNREFUSED:
        raise EvaluationError("local serving-port readback is unavailable")


class _TwoEngineOwnedSubprocessLifecycle:
    """Shared exact-owned process lifecycle with the original vLLM defaults.

    Every native trial gets two fresh exact-owned containers: endpoint A is
    constrained to GPU 0 and endpoint B to GPU 1.  Startup verifies only the
    two declared loopback ports; it does not adopt an existing container or
    remove anything it did not create.  The default runner uses ``exec`` (not a
    shell), never pulls an image, and mounts a caller-provided preloaded model
    snapshot read-only with offline model flags.  This is a local operator path
    only.  Its artifacts remain ``UNVERIFIED`` and cannot become live evidence
    merely because the process lifecycle completed.
    """

    def __init__(
        self,
        origins: tuple[str, str],
        *,
        ownership_id: str,
        model_snapshot_path: Path,
        runner: LocalSubprocessRunner | None = None,
        warmup_probe: Callable[[str, float], None] = _probe_loopback_warmup,
        snapshot_verifier: Callable[[Path], None] = _verify_pinned_model_snapshot,
        port_closed_probe: Callable[[int, float], None] = _assert_loopback_port_closed,
        clock: Callable[[], int] = monotonic_ns,
        readiness_timeout_ns: int = 1_000_000_000,
        startup_timeout_ns: int = _LOCAL_ENGINE_STARTUP_TIMEOUT_NS,
    ) -> None:
        if not _SAFE_OWNERSHIP_ID.fullmatch(ownership_id):
            raise EvaluationError("local engine ownership identifier is invalid")
        if (
            not model_snapshot_path.is_absolute()
            or model_snapshot_path.is_symlink()
            or any(
                character in str(model_snapshot_path) for character in (",", "\n", "\r")
            )
            or type(readiness_timeout_ns) is not int
            or not 1 <= readiness_timeout_ns <= _LOCAL_ENGINE_COMMAND_TIMEOUT_NS
            or type(startup_timeout_ns) is not int
            or not readiness_timeout_ns <= startup_timeout_ns <= 300_000_000_000
        ):
            raise EvaluationError("local engine lifecycle is invalid")
        self._readiness = LoopbackTwoEndpointReadinessLifecycle(
            origins, readiness_timeout_ns=readiness_timeout_ns
        )
        self._origins = tuple(_loopback_origin(origin) for origin in origins)
        self._ownership_id = ownership_id
        self._model_snapshot_path = model_snapshot_path
        self._runner = runner or AsyncioLocalSubprocessRunner()
        self._warmup_probe = warmup_probe
        self._snapshot_verifier = snapshot_verifier
        self._port_closed_probe = port_closed_probe
        self._clock = clock
        self._readiness_timeout_ns = readiness_timeout_ns
        self._startup_timeout_ns = startup_timeout_ns
        self._active: list[_OwnedEngine] = []
        self._snapshot_verified = False
        self._operation_deadline_ns: int | None = None

    @property
    def required_operation_timeout_ns(self) -> int:
        """Maximum one prepare/cleanup operation time that a protocol must reserve."""

        # Prepare checks both GPUs, starts two engines, then consumes the
        # bounded readiness/warmup allowance. Cleanup permits reconciliation
        # plus removal for both targets, two port readbacks, and both GPU
        # readbacks, all under the same original operation deadline.
        prepare = 4 * _LOCAL_ENGINE_COMMAND_TIMEOUT_NS + self._startup_timeout_ns
        cleanup = 8 * _LOCAL_ENGINE_COMMAND_TIMEOUT_NS
        return max(prepare, cleanup)

    def _engine_name(self, trial: CompiledTrial, endpoint_id: str) -> str:
        return f"inferdrome-lcr-{self._ownership_id}-{trial.trial_id}-{endpoint_id}"

    def _attempt_label(self, trial: CompiledTrial, endpoint_id: str) -> str:
        return f"{self._ownership_id}-{trial.trial_id}-{endpoint_id}"

    def set_operation_deadline(self, deadline_ns: int) -> None:
        """Accept one absolute phase deadline from the native rehearsal bridge."""

        if type(deadline_ns) is not int or deadline_ns < 1:
            raise EvaluationError("local engine operation deadline is invalid")
        self._operation_deadline_ns = deadline_ns

    def _deadline(self) -> int:
        if self._operation_deadline_ns is not None:
            return self._operation_deadline_ns
        return self._clock() + self.required_operation_timeout_ns

    def _remaining_timeout_ns(self, deadline_ns: int) -> int:
        remaining = deadline_ns - self._clock()
        if remaining < 1:
            raise EvaluationError("local engine operation deadline expired")
        return min(_LOCAL_ENGINE_COMMAND_TIMEOUT_NS, remaining)

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        if index not in (0, 1):
            raise EvaluationError("local engine index is invalid")
        parsed = urlsplit(self._origins[index])
        assert parsed.port is not None
        endpoint_id = ("endpoint-a", "endpoint-b")[index]
        return (
            "docker",
            "run",
            "--pull",
            "never",
            "--detach",
            "--rm",
            "--name",
            self._engine_name(trial, endpoint_id),
            "--label",
            f"io.inferdrome.load-calibration.owner={self._ownership_id}",
            "--label",
            "io.inferdrome.load-calibration.attempt="
            f"{self._attempt_label(trial, endpoint_id)}",
            "--gpus",
            f"device={index}",
            "--network",
            "bridge",
            "--publish",
            f"127.0.0.1:{parsed.port}:8000",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs",
            "/home/vllm:rw,nosuid,nodev,size=1g",
            "--mount",
            f"type=bind,src={self._model_snapshot_path},dst=/model,readonly",
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
            "--entrypoint",
            "vllm",
            VLLM_RUNTIME_IMAGE_REFERENCE,
            "serve",
            "/model",
            "--served-model-name",
            QWEN3_8B_MODEL_ID,
            "--revision",
            QWEN3_8B_REVISION,
            "--tokenizer",
            "/model",
            "--tokenizer-revision",
            QWEN3_8B_REVISION,
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--disable-log-requests",
        )

    def _expected_image_reference(self) -> str:
        return VLLM_RUNTIME_IMAGE_REFERENCE

    def _bind_trial(self, trial: CompiledTrial) -> None:
        self._readiness._bound_to_trial(trial)

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
        result = await self._runner.run(
            argv, timeout_ns=self._remaining_timeout_ns(deadline_ns)
        )
        if (
            result.argv != argv
            or result.returncode != 0
            or len(result.stdout) > _SUBPROCESS_OUTPUT_BYTES
            or len(result.stderr) > _SUBPROCESS_OUTPUT_BYTES
        ):
            raise EvaluationError("exact-owned local engine command failed")
        return result

    async def _verify_gpu_idle(self, *, deadline_ns: int) -> None:
        errors: list[Exception] = []
        for index in (0, 1):
            try:
                content = (
                    await self._command(
                        (
                            "nvidia-smi",
                            f"--id={index}",
                            "--query-compute-apps=pid",
                            "--format=csv,noheader",
                        ),
                        deadline_ns=deadline_ns,
                    )
                ).stdout
                if content.strip():
                    raise EvaluationError("declared GPU is not idle")
            except Exception as error:
                errors.append(error)
                if self._clock() >= deadline_ns:
                    break
        if errors:
            raise EvaluationError(
                "declared GPU-idle readback is unconfirmed"
            ) from errors[0]

    async def _verify_ports_closed(self, *, deadline_ns: int) -> None:
        errors: list[Exception] = []
        for origin in self._origins:
            try:
                port = urlsplit(origin).port
                assert port is not None
                timeout_ns = self._remaining_timeout_ns(deadline_ns)
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._port_closed_probe,
                        port,
                        timeout_ns / 1_000_000_000,
                    ),
                    timeout=timeout_ns / 1_000_000_000,
                )
            except Exception as error:
                errors.append(error)
                if self._clock() >= deadline_ns:
                    break
        if errors:
            raise EvaluationError(
                "declared serving-port readback is unconfirmed"
            ) from errors[0]

    async def _inspect_pending(self, target: _OwnedEngine, *, deadline_ns: int) -> None:
        """Bind a pending create only after exact name/image/label readback."""

        argv = (
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .}}",
            target.name,
        )
        result = await self._runner.run(
            argv, timeout_ns=self._remaining_timeout_ns(deadline_ns)
        )
        if result.argv != argv or len(result.stdout) > _SUBPROCESS_OUTPUT_BYTES:
            raise EvaluationError("exact-owned local engine reconciliation failed")
        missing_messages = {
            f"Error: No such container: {target.name}\n".encode(),
            f"Error response from daemon: No such container: {target.name}\n".encode(),
        }
        if (
            result.returncode == 1
            and result.stdout == b""
            and result.stderr in missing_messages
        ):
            # A killed or timed-out Docker CLI does not prove that the daemon
            # did not subsequently materialize this dispatched create. Keep
            # the pending target so a later cleanup can reconcile only this
            # exact attempt; closed ports and idle GPUs are not ownership proof.
            raise EvaluationError("exact-owned local engine target remains pending")
        if result.returncode != 0 or len(result.stderr) > _SUBPROCESS_OUTPUT_BYTES:
            raise EvaluationError("exact-owned local engine reconciliation failed")
        try:
            observed = json.loads(result.stdout)
            config = observed["Config"]
            labels = config["Labels"]
            container_id = observed["Id"]
            if (
                type(config) is not dict
                or type(labels) is not dict
                or type(container_id) is not str
                or not _CONTAINER_ID.fullmatch(container_id)
                or observed.get("Name") != f"/{target.name}"
                or config.get("Image") != self._expected_image_reference()
                or labels.get("io.inferdrome.load-calibration.owner")
                != self._ownership_id
                or labels.get("io.inferdrome.load-calibration.attempt")
                != target.attempt_label
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise EvaluationError(
                "exact-owned local engine reconciliation failed"
            ) from None
        target.container_id = container_id

    async def _remove_active(self, *, deadline_ns: int) -> None:
        errors: list[Exception] = []
        for target in tuple(reversed(self._active)):
            try:
                if target.container_id is None:
                    await self._inspect_pending(target, deadline_ns=deadline_ns)
                assert target.container_id is not None
                await self._command(
                    ("docker", "rm", "--force", target.container_id),
                    deadline_ns=deadline_ns,
                )
                self._active.remove(target)
            except Exception as error:
                errors.append(error)
                if self._clock() >= deadline_ns:
                    break
        for readback in (self._verify_ports_closed, self._verify_gpu_idle):
            try:
                await readback(deadline_ns=deadline_ns)
            except Exception as error:
                errors.append(error)
        if self._active:
            errors.append(
                EvaluationError("exact-owned local engine remains unresolved")
            )
        if errors:
            raise EvaluationError(
                "exact-owned local engine cleanup is unconfirmed"
            ) from errors[0]

    async def _await_ready_and_warm(
        self, trial: CompiledTrial, stop: asyncio.Event
    ) -> None:
        deadline_ns = min(self._deadline(), self._clock() + self._startup_timeout_ns)
        delay_seconds = 0.05
        last_error: Exception | None = None
        while self._clock() < deadline_ns:
            if stop.is_set():
                raise EvaluationError("local engine lifecycle was cancelled")
            try:
                await self._readiness.prepare(trial, stop=stop)
                timeout_ns = self._remaining_timeout_ns(deadline_ns)
                warmup_timeout_seconds = (
                    min(
                        self._readiness_timeout_ns,
                        _LOCAL_ENGINE_WARMUP_TIMEOUT_NS,
                        timeout_ns,
                    )
                    / 1_000_000_000
                )
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            asyncio.to_thread(
                                self._warmup_probe,
                                origin,
                                warmup_timeout_seconds,
                            )
                            for origin in self._origins
                        )
                    ),
                    timeout=timeout_ns / 1_000_000_000,
                )
                return
            except (TimeoutError, EvaluationError, OSError) as error:
                last_error = error
            remaining_ns = deadline_ns - self._clock()
            if remaining_ns < 1:
                break
            await asyncio.sleep(min(delay_seconds, remaining_ns / 1_000_000_000))
            delay_seconds = min(1.0, delay_seconds * 2)
        raise EvaluationError("local engine readiness deadline expired") from last_error

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        if stop.is_set():
            raise EvaluationError("local engine lifecycle was cancelled")
        self._bind_trial(trial)
        if self._active:
            raise EvaluationError("local engine lifecycle has an active trial")
        await self._verify_artifacts(stop=stop)
        if stop.is_set():
            raise EvaluationError("local engine lifecycle was cancelled")
        deadline_ns = self._deadline()
        await self._verify_gpu_idle(deadline_ns=deadline_ns)
        try:
            for index, endpoint_id in enumerate(("endpoint-a", "endpoint-b")):
                if stop.is_set():
                    raise EvaluationError("local engine lifecycle was cancelled")
                parsed = urlsplit(self._origins[index])
                assert parsed.port is not None
                target = _OwnedEngine(
                    name=self._engine_name(trial, endpoint_id),
                    attempt_label=self._attempt_label(trial, endpoint_id),
                    gpu_index=index,
                    port=parsed.port,
                )
                # Track before command dispatch: a timeout/lost response may
                # still have materialized a container that must be reconciled.
                self._active.append(target)
                result = await self._command(
                    self._engine_argv(trial, index=index), deadline_ns=deadline_ns
                )
                if not _CONTAINER_ID.fullmatch(result.stdout.decode("ascii").strip()):
                    raise EvaluationError("local engine did not return an exact ID")
                target.container_id = result.stdout.decode("ascii").strip()
            await self._await_ready_and_warm(trial, stop)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            try:
                await self._remove_active(deadline_ns=deadline_ns)
            except Exception as cleanup_error:
                raise EvaluationError(
                    "local engine startup failed and cleanup is unconfirmed"
                ) from cleanup_error
            raise EvaluationError("local engine startup or warmup failed") from error

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        del stop
        self._bind_trial(trial)
        await self._remove_active(deadline_ns=self._deadline())


class TwoEngineVllmSubprocessLifecycle(_TwoEngineOwnedSubprocessLifecycle):
    """Original two-engine vLLM constructor, argv, ownership and cleanup behavior."""


class RehearsalLifecycleReceipt(ClosedModel):
    """Sanitized sidecar evidence; never part of a hashed StudyConfig."""

    schema_version: Literal["inferdrome.evaluation-load-rehearsal-lifecycle.v1"]
    phase: Literal["CALIBRATION", "CONFIRMATION"]
    source_trial_id: str
    reset_and_warmup: Literal["COMPLETED", "FAILED"]
    cleanup: Literal["CONFIRMED", "FAILED", "NOT_STARTED"]
    prepare_elapsed_ns: int
    execute_elapsed_ns: int | None
    cleanup_elapsed_ns: int | None
    evidence_eligible: Literal[False] = False
    runtime_identity: Literal["UNVERIFIED"] = "UNVERIFIED"


class SGLangResetReceipt(ClosedModel):
    """Sanitized successful SGLang prepare/reset facts for one endpoint.

    These fields bind a declared profile, projected launch, readiness contract,
    and server-accepted flush to the phase trial.  They deliberately do not
    retain origins, model paths, response bodies, or claim cache attestation.
    """

    endpoint_id: Literal["endpoint-a", "endpoint-b"]
    source_profile_sha256: str
    projected_launch_sha256: str
    readiness_config_sha256: str
    flush_completed_ns: Annotated[int, Field(ge=0)]
    before_metrics_started_ns: Annotated[int, Field(ge=0)]
    before_metrics_completed_ns: Annotated[int, Field(ge=0)]
    after_metrics_started_ns: Annotated[int, Field(ge=0)]
    after_metrics_completed_ns: Annotated[int, Field(ge=0)]
    disposition: Literal["SERVER_ACCEPTED"]
    scheduler_source_age: Literal["UNKNOWN"]
    runtime_verification: Literal["UNVERIFIED"]
    evidence_eligible: Literal[False]

    @model_validator(mode="after")
    def ordered_acquisitions(self) -> SGLangResetReceipt:
        if (
            not _SHA256_DIGEST.fullmatch(self.source_profile_sha256)
            or not _SHA256_DIGEST.fullmatch(self.projected_launch_sha256)
            or not _SHA256_DIGEST.fullmatch(self.readiness_config_sha256)
            or self.before_metrics_started_ns > self.before_metrics_completed_ns
            or self.after_metrics_started_ns > self.after_metrics_completed_ns
            or self.flush_completed_ns < self.before_metrics_completed_ns
            or self.after_metrics_started_ns < self.flush_completed_ns
        ):
            raise ValueError("SGLang reset receipt is invalid")
        return self


class SGLangPrepareReceipt(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-load-rehearsal-sglang-prepare.v1"]
    phase: Literal["CALIBRATION", "CONFIRMATION"]
    source_trial_id: str
    resets: tuple[SGLangResetReceipt, SGLangResetReceipt]
    evidence_eligible: Literal[False] = False
    runtime_identity: Literal["UNVERIFIED"] = "UNVERIFIED"

    @model_validator(mode="after")
    def one_reset_per_endpoint(self) -> SGLangPrepareReceipt:
        if tuple(reset.endpoint_id for reset in self.resets) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise ValueError("SGLang prepare receipt endpoint order is invalid")
        return self


@dataclass(frozen=True)
class CandidateStudyRecipe:
    """One candidate's separate healthy calibration and fault confirmation plans."""

    level_id: str
    calibration_config: StudyConfig
    confirmation_config: StudyConfig


type DeclaredTrial = CalibrationTrial | ConfirmationTrial


@dataclass(frozen=True)
class TrialBinding:
    declared_trial: DeclaredTrial
    source_trial: CompiledTrial
    source_index: int


@dataclass(frozen=True)
class BoundCandidateRecipe:
    level: LoadLevel
    calibration_config: StudyConfig
    calibration_plan: CompiledStudy
    calibration: tuple[TrialBinding, ...]
    confirmation_config: StudyConfig
    confirmation_plan: CompiledStudy
    confirmation: tuple[TrialBinding, ...]
    calibration_recipe_sha256: str
    confirmation_recipe_sha256: str


@dataclass(frozen=True)
class CompiledRehearsal:
    calibration_plan: CompiledCalibrationPlan
    candidates: tuple[BoundCandidateRecipe, ...]
    execution_worst_case_duration_ns: int
    final_cleanup_reserve_ns: int
    retrieval_reserve_ns: int
    worst_case_duration_ns: int
    reserved_output_bytes: int


@dataclass(frozen=True)
class RehearsalResult:
    calibration_selection: CalibrationSelection
    confirmation: ConfirmationPlan
    calibration_manifests: tuple[tuple[str, StudyManifest], ...]
    confirmation_manifest: StudyManifest | None
    confirmation_report_path: Path | None
    candidate_recipe_bindings_sha256: str
    evidence_eligible: bool = False
    engine_choice_bindings_sha256: str | None = None


def _plan_sha256(plan: CompiledStudy) -> str:
    return sha256_digest(plan_bytes(plan))


def _expected_calibration(
    plan: CompiledCalibrationPlan, level: LoadLevel
) -> tuple[CalibrationTrial, ...]:
    return tuple(trial for trial in plan.trials if trial.level_id == level.level_id)


def _phase_execution_identity(
    protocol: LoadCalibrationProtocol, config: StudyConfig
) -> bytes:
    """Freeze the settings shared by calibration and confirmation.

    This comparison happens only in memory: it deliberately includes raw
    origins and prompts to reject a changed serving or arrival recipe, but does
    not retain either in rehearsal sidecars.
    """

    foreground = config.blocks[0].foreground
    if (
        foreground.source_commit != protocol.source_commit
        or foreground.model != protocol.workload.model
        or foreground.max_tokens != protocol.workload.token_lengths.completion_tokens
    ):
        raise EvaluationError("study recipe does not bind source or requested tokens")
    for block in config.blocks:
        if (
            block.foreground != foreground
            or block.telemetry != config.blocks[0].telemetry
        ):
            raise EvaluationError("study recipe does not retain one arrival admission")
        background = block.background
        if background is not None and (
            background.source_commit != protocol.source_commit
            or background.model != protocol.workload.model
            or background.max_tokens
            != protocol.workload.token_lengths.completion_tokens
            or background.endpoints != foreground.endpoints
        ):
            raise EvaluationError("study recipe background does not bind protocol")
    return canonical_json_bytes(
        {
            "foreground": foreground.model_dump(mode="json"),
            "profiles": [
                profile.model_dump(mode="json") for profile in config.profiles
            ],
            "telemetry": config.blocks[0].telemetry.model_dump(mode="json"),
            "preparation": config.preparation.model_dump(mode="json"),
        }
    )


def _recipe_sha256(config: StudyConfig, plan: CompiledStudy) -> str:
    """Bind the exact compiled recipe before observations can be read."""

    return sha256_digest(
        canonical_json_bytes(
            {
                "config_sha256": plan.config_sha256,
                "plan_sha256": _plan_sha256(plan),
                "config": config.model_dump(mode="json"),
            }
        )
    )


def _candidate_recipe_bindings_bytes(rehearsal: CompiledRehearsal) -> bytes:
    """Return the pre-dispatch identity ledger for every selectable candidate.

    The ledger intentionally contains only hashed native configuration and plan
    identities.  It binds both phases before calibration observations could
    select one candidate, without retaining endpoint origins or request text.
    """

    return (
        canonical_json_bytes(
            {
                "schema_version": (
                    "inferdrome.evaluation-load-rehearsal-candidate-bindings.v1"
                ),
                "protocol_sha256": rehearsal.calibration_plan.protocol_sha256,
                "candidates": [
                    {
                        "level_id": candidate.level.level_id,
                        "calibration": {
                            "config_sha256": candidate.calibration_plan.config_sha256,
                            "plan_sha256": _plan_sha256(candidate.calibration_plan),
                            "recipe_sha256": candidate.calibration_recipe_sha256,
                        },
                        "confirmation": {
                            "config_sha256": candidate.confirmation_plan.config_sha256,
                            "plan_sha256": _plan_sha256(candidate.confirmation_plan),
                            "recipe_sha256": candidate.confirmation_recipe_sha256,
                        },
                    }
                    for candidate in rehearsal.candidates
                ],
                "evidence_eligible": False,
                "runtime_identity": "UNVERIFIED",
            }
        )
        + b"\n"
    )


def _budgeted(
    config: StudyConfig, plan: CompiledStudy, protocol: LoadCalibrationProtocol
) -> None:
    # ``run_study`` owns a wall deadline around its injected executor.  The
    # injected lifecycle is therefore part of—not a hidden prelude to—that
    # deadline.  The protocol separately reserves the same work study-wide.
    cleanup_duration_ns = (
        protocol.preparation.warmup_reset_max_duration_ns
        if protocol.preparation.cleanup_max_duration_ns is None
        else protocol.preparation.cleanup_max_duration_ns
    )
    lifecycle_reserve = len(plan.trials) * (
        protocol.preparation.warmup_reset_max_duration_ns + cleanup_duration_ns
    )
    required = plan.worst_case_duration_ns + lifecycle_reserve
    if required > config.limits.max_duration_ns:
        raise EvaluationError("study deadline does not reserve lifecycle work")
    if config.limits.per_trial_result_bytes > protocol.per_trial_output_bytes:
        raise EvaluationError("study result bound exceeds calibration protocol")


def _bind_phase(
    protocol: LoadCalibrationProtocol,
    level: LoadLevel,
    config: StudyConfig,
    expected: tuple[DeclaredTrial, ...],
    *,
    calibration: bool,
) -> tuple[CompiledStudy, tuple[TrialBinding, ...]]:
    plan = compile_study(config)
    _budgeted(config, plan, protocol)
    if calibration and (
        plan.config_sha256 != level.study_config_sha256
        or _plan_sha256(plan) != level.study_plan_sha256
    ):
        raise EvaluationError("calibration study recipe does not bind its level")
    if len(plan.trials) != len(expected):
        raise EvaluationError("study recipe does not cover its declared trials")
    bindings: list[TrialBinding] = []
    for index, (source, declared) in enumerate(zip(plan.trials, expected, strict=True)):
        offered_window_ns = source.window_end_ns - source.window_start_ns
        if (
            source.scenario != declared.scenario
            or source.policy_id != declared.policy_id
            or len(source.config.foreground.offers) != declared.planned_requests
            or declared.planned_requests * 1_000_000_000_000
            != declared.offered_rate_millirps * offered_window_ns
            or source.worst_case_duration_ns != declared.worst_case_duration_ns
            or source.first_content_slo_ns != protocol.first_content_slo_ns
            or source.completion_slo_ns != protocol.completion_slo_ns
        ):
            raise EvaluationError("study recipe trial differs from its declaration")
        bindings.append(TrialBinding(declared, source, index))
    return plan, tuple(bindings)


def compile_rehearsal(
    protocol: LoadCalibrationProtocol, candidates: tuple[CandidateStudyRecipe, ...]
) -> CompiledRehearsal:
    """Preflight every candidate and both phases before any transport is opened."""
    calibration_plan = compile_calibration(protocol)
    by_id = {candidate.level_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates) or set(by_id) != {
        level.level_id for level in protocol.levels
    }:
        raise EvaluationError("rehearsal recipes do not cover every declared level")
    bound: list[BoundCandidateRecipe] = []
    for level in protocol.levels:
        candidate = by_id[level.level_id]
        calibration_identity = _phase_execution_identity(
            protocol, candidate.calibration_config
        )
        confirmation_identity = _phase_execution_identity(
            protocol, candidate.confirmation_config
        )
        if calibration_identity != confirmation_identity:
            raise EvaluationError("confirmation recipe changes execution identity")
        calibration_study, calibration = _bind_phase(
            protocol,
            level,
            candidate.calibration_config,
            _expected_calibration(calibration_plan, level),
            calibration=True,
        )
        confirmation_study, confirmation = _bind_phase(
            protocol,
            level,
            candidate.confirmation_config,
            compile_confirmation_trials(protocol, level),
            calibration=False,
        )
        bound.append(
            BoundCandidateRecipe(
                level=level,
                calibration_config=candidate.calibration_config,
                calibration_plan=calibration_study,
                calibration=calibration,
                confirmation_config=candidate.confirmation_config,
                confirmation_plan=confirmation_study,
                confirmation=confirmation,
                calibration_recipe_sha256=_recipe_sha256(
                    candidate.calibration_config, calibration_study
                ),
                confirmation_recipe_sha256=_recipe_sha256(
                    candidate.confirmation_config, confirmation_study
                ),
            )
        )
    calibration_duration = sum(
        item.calibration_plan.worst_case_duration_ns for item in bound
    )
    confirmation_duration = max(
        item.confirmation_plan.worst_case_duration_ns for item in bound
    )
    lifecycle_trials = sum(len(item.calibration_plan.trials) for item in bound) + max(
        len(item.confirmation_plan.trials) for item in bound
    )
    cleanup_duration_ns = (
        protocol.preparation.warmup_reset_max_duration_ns
        if protocol.preparation.cleanup_max_duration_ns is None
        else protocol.preparation.cleanup_max_duration_ns
    )
    execution_worst_case_duration_ns = (
        calibration_duration
        + confirmation_duration
        + lifecycle_trials
        * (protocol.preparation.warmup_reset_max_duration_ns + cleanup_duration_ns)
    )
    worst_case_duration_ns = (
        execution_worst_case_duration_ns
        + _SESSION_FINAL_CLEANUP_RESERVE_NS
        + _SESSION_RETRIEVAL_RESERVE_NS
    )
    reserved_output_bytes = (
        sum(item.calibration_plan.reserved_result_bytes for item in bound)
        + max(item.confirmation_plan.reserved_result_bytes for item in bound)
        + (len(bound) + 1) * _REPORT_OUTPUT_BYTES
        + (len(bound) + 1) * _SIDECAR_OUTPUT_BYTES
        + _CONTROL_OUTPUT_BYTES
    )
    if worst_case_duration_ns > protocol.max_session_duration_ns:
        raise EvaluationError(
            "native rehearsal duration exceeds protocol session bound"
        )
    if reserved_output_bytes > protocol.max_session_output_bytes:
        raise EvaluationError("native rehearsal output exceeds protocol session bound")
    return CompiledRehearsal(
        calibration_plan=calibration_plan,
        candidates=tuple(bound),
        execution_worst_case_duration_ns=execution_worst_case_duration_ns,
        final_cleanup_reserve_ns=_SESSION_FINAL_CLEANUP_RESERVE_NS,
        retrieval_reserve_ns=_SESSION_RETRIEVAL_RESERVE_NS,
        worst_case_duration_ns=worst_case_duration_ns,
        reserved_output_bytes=reserved_output_bytes,
    )


def _lifecycle_bytes(
    *,
    phase: Literal["CALIBRATION", "CONFIRMATION"],
    plan: CompiledStudy,
    receipts: tuple[RehearsalLifecycleReceipt, ...],
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.evaluation-load-rehearsal-receipts.v1",
                "phase": phase,
                "config_sha256": plan.config_sha256,
                "plan_sha256": _plan_sha256(plan),
                "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
                "evidence_eligible": False,
                "runtime_identity": "UNVERIFIED",
            }
        )
        + b"\n"
    )


def _sglang_prepare_receipt(
    lifecycle: TrialLifecycle,
    *,
    phase: Literal["CALIBRATION", "CONFIRMATION"],
    trial: CompiledTrial,
) -> SGLangPrepareReceipt | None:
    """Copy only a completed SGLang ``last_reset`` receipt after prepare.

    The vLLM/default lifecycle has no such property, so it emits no new file
    and retains the historical receipt bytes.  A malformed optional value is a
    fail-closed lifecycle violation rather than a reason to fabricate a reset.
    """

    try:
        value = lifecycle.last_reset  # type: ignore[attr-defined]
    except AttributeError:
        return None
    try:
        if type(value) is not tuple:
            raise ValueError
        resets: list[SGLangResetReceipt] = []
        for item in value:
            reset = item.reset
            before = reset.before
            after = reset.after
            resets.append(
                SGLangResetReceipt(
                    endpoint_id=item.endpoint_id,
                    source_profile_sha256=item.source_profile_sha256,
                    projected_launch_sha256=item.projected_launch_sha256,
                    readiness_config_sha256=item.readiness_config_sha256,
                    flush_completed_ns=reset.flush_completed_ns,
                    before_metrics_started_ns=before.started_ns,
                    before_metrics_completed_ns=before.completed_ns,
                    after_metrics_started_ns=after.started_ns,
                    after_metrics_completed_ns=after.completed_ns,
                    disposition=reset.disposition,
                    scheduler_source_age=reset.scheduler_source_age,
                    runtime_verification=reset.runtime_verification,
                    evidence_eligible=reset.evidence_eligible,
                )
            )
        return SGLangPrepareReceipt(
            schema_version="inferdrome.evaluation-load-rehearsal-sglang-prepare.v1",
            phase=phase,
            source_trial_id=trial.trial_id,
            resets=tuple(resets),  # type: ignore[arg-type]
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise EvaluationError("SGLang lifecycle reset receipt is invalid") from None


def _sglang_prepare_bytes(
    *,
    phase: Literal["CALIBRATION", "CONFIRMATION"],
    plan: CompiledStudy,
    receipts: tuple[SGLangPrepareReceipt, ...],
) -> bytes:
    content = (
        canonical_json_bytes(
            {
                "schema_version": (
                    "inferdrome.evaluation-load-rehearsal-sglang-prepares.v1"
                ),
                "phase": phase,
                "config_sha256": plan.config_sha256,
                "plan_sha256": _plan_sha256(plan),
                "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
                "evidence_eligible": False,
                "runtime_identity": "UNVERIFIED",
            }
        )
        + b"\n"
    )
    if len(content) > _SIDECAR_OUTPUT_BYTES:
        raise EvaluationError("SGLang reset receipt exceeds its output reserve")
    return content


def _write_sidecar(path: Path, content: bytes) -> None:
    """Write one bounded, no-replace rehearsal control artifact."""

    if not 1 <= len(content) <= _SIDECAR_OUTPUT_BYTES:
        raise EvaluationError("rehearsal sidecar exceeds its output bound")
    with OutputFile(path) as destination:
        destination.write(content)


def _read_confirmation_report(
    report_path: Path,
    *,
    expected_config_sha256: str,
    expected_plan_sha256: str,
    engine_binding: EvaluationEngineBinding | None = None,
) -> tuple[bytes, str, StudyReport]:
    """Re-read the full envelope; private statistics only check its ledger."""

    if (
        report_path.name != "report.json"
        or not _SHA256_DIGEST.fullmatch(expected_config_sha256)
        or not _SHA256_DIGEST.fullmatch(expected_plan_sha256)
    ):
        raise EvaluationError("rehearsal confirmation report is unavailable")
    try:
        maximum = (
            MAX_SGLANG_REPORT_BYTES
            if engine_binding is not None
            else _SIDECAR_OUTPUT_BYTES
        )
        with StudyDirectory.open(report_path.parent, budget=maximum) as directory:
            content = directory.read("report.json", limit=maximum)
        if engine_binding is None:
            loaded = load_evaluation_report_bytes(content, kind="STUDY")
        else:
            envelope = read_sglang_report_bytes(content)
            if (
                envelope.dashboard_projection != "ENGINE_BOUND_V2"
                or engine_binding_bytes(envelope.engine_binding)
                != engine_binding_bytes(engine_binding)
            ):
                raise ValueError
            loaded = envelope.statistical_report
    except (OSError, ValueError, EvaluationError):
        raise EvaluationError("rehearsal confirmation report is unavailable") from None
    report_config_sha256 = loaded.model_dump(mode="json").get("config_sha256")
    report_plan_sha256 = loaded.model_dump(mode="json").get("plan_sha256")
    if (
        loaded.status != "COMPLETED"
        or report_config_sha256 != expected_config_sha256
        or report_plan_sha256 != expected_plan_sha256
    ):
        raise EvaluationError("rehearsal confirmation report is not complete")
    if not isinstance(loaded, StudyReport):
        raise EvaluationError("rehearsal confirmation report is unavailable")
    return content, sha256_digest(content), loaded


def _validate_confirmation_report_ledger(
    manifest: StudyManifest, report: StudyReport
) -> None:
    """Require the durable study ledger to name exactly the reported trials.

    Report validation proves a native report is canonical.  Recovery also
    needs the independently durable manifest to bind the same ordered native
    results before it creates a dashboard pin; neither artifact is allowed to
    stand in for the other.
    """

    if len(manifest.trials) != len(report.trials):
        raise EvaluationError("rehearsal recovery report is inconsistent")
    for index, (entry, summary) in enumerate(
        zip(manifest.trials, report.trials, strict=True)
    ):
        if (
            entry.trial_id != summary.trial_id
            or entry.state != "RETURNED"
            or entry.result_filename != trial_filename(index)
            or entry.result_status != summary.status
            or entry.result_sha256 != summary.result_sha256
        ):
            raise EvaluationError("rehearsal recovery report is inconsistent")


def _write_pinned_catalog(
    *,
    report_path: Path,
    report_sha256: str,
    catalog_path: Path,
    sglang: bool = False,
) -> str:
    _write_sidecar(
        catalog_path,
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
                "entries": [
                    {
                        "kind": "SGLANG_STUDY" if sglang else "STUDY",
                        "report_path": str(report_path.absolute()),
                        "expected_sha256": report_sha256,
                    }
                ],
            }
        )
        + b"\n",
    )
    return report_sha256


def write_pinned_confirmation_catalog(
    result: RehearsalResult, *, catalog_path: Path
) -> str:
    """Validate and pin only the selected native confirmation report.

    Candidate calibration reports remain local diagnostic artifacts.  The
    dashboard receives exactly one independently re-read confirmation report
    through its existing digest-pinned, read-only catalog contract.
    """

    report_path = result.confirmation_report_path
    manifest = result.confirmation_manifest
    if (
        result.calibration_selection.selected_level_id is None
        or result.confirmation.status != "READY"
        or report_path is None
        or manifest is None
        or manifest.status != "COMPLETED"
    ):
        raise EvaluationError("rehearsal has no confirmation report to pin")
    root = report_path.parent.parent
    selected_level_id = result.calibration_selection.selected_level_id
    engine_binding = _confirmation_engine_binding(
        root,
        selected_level_id=selected_level_id,
        expected_ledger_sha256=result.engine_choice_bindings_sha256,
        expected_candidate_sha256=result.candidate_recipe_bindings_sha256,
    )
    _, digest, report = _read_confirmation_report(
        report_path,
        expected_config_sha256=manifest.config_sha256,
        expected_plan_sha256=manifest.plan_sha256,
        engine_binding=engine_binding,
    )
    _validate_confirmation_report_ledger(manifest, report)
    if engine_binding is not None:
        if (
            report_path.absolute()
            != (
                root / f"confirmation-{selected_level_id}-report" / "report.json"
            ).absolute()
        ):
            raise EvaluationError("SGLang confirmation location is inconsistent")
        # The immediate path also verifies durable selection, phase binding and
        # manifest antecedents before publishing the same recovery-safe pin.
        return recover_pinned_confirmation_catalog(root, catalog_path=catalog_path)
    return _write_pinned_catalog(
        report_path=report_path, report_sha256=digest, catalog_path=catalog_path
    )


def _canonical_sidecar(path: Path, *, schema_version: str) -> dict[str, object]:
    """Read exactly one canonical bounded control artifact after a restart."""

    try:
        content = read_input(path)
        if len(content) > _SIDECAR_OUTPUT_BYTES:
            raise ValueError
        value = json.loads(content)
        if (
            type(value) is not dict
            or value.get("schema_version") != schema_version
            or canonical_json_bytes(value) + b"\n" != content
        ):
            raise ValueError
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise EvaluationError("rehearsal recovery artifact is unavailable") from None


def _confirmation_engine_binding(
    output_root: Path,
    *,
    selected_level_id: str,
    expected_ledger_sha256: object,
    expected_candidate_sha256: str,
) -> EvaluationEngineBinding | None:
    """Bind a SGLang pin to every durable candidate/phase engine declaration."""
    if type(selected_level_id) is not str or not _SAFE_LEVEL_ID.fullmatch(
        selected_level_id
    ):
        raise EvaluationError("SGLang confirmation selection is inconsistent")
    ledger_path = output_root / "engine-choice-bindings.json"
    study_path = output_root / f"confirmation-{selected_level_id}"
    binding_path = study_path / "engine-binding.json"
    if expected_ledger_sha256 is None:
        if any(
            path.exists() or path.is_symlink() for path in (ledger_path, binding_path)
        ):
            raise EvaluationError("SGLang confirmation requires its engine ledger")
        return None
    if type(expected_ledger_sha256) is not str or not _SHA256_DIGEST.fullmatch(
        expected_ledger_sha256
    ):
        raise EvaluationError("SGLang confirmation engine ledger is inconsistent")
    candidates = _canonical_sidecar(
        output_root / "candidate-recipe-bindings.json",
        schema_version="inferdrome.evaluation-load-rehearsal-candidate-bindings.v1",
    )
    ledger = _canonical_sidecar(
        ledger_path,
        schema_version="inferdrome.evaluation-load-rehearsal-engine-bindings.v1",
    )
    try:
        if (
            sha256_digest(canonical_json_bytes(ledger) + b"\n")
            != expected_ledger_sha256
            or sha256_digest(canonical_json_bytes(candidates) + b"\n")
            != expected_candidate_sha256
            or set(ledger)
            != {
                "schema_version",
                "protocol_sha256",
                "candidate_recipe_bindings_sha256",
                "engine_choice_sha256",
                "phases",
                "runtime_identity",
                "evidence_eligible",
            }
            or ledger["protocol_sha256"] != candidates.get("protocol_sha256")
            or ledger["candidate_recipe_bindings_sha256"] != expected_candidate_sha256
            or ledger["runtime_identity"] != "UNVERIFIED"
            or ledger["evidence_eligible"] is not False
        ):
            raise ValueError
        recipes = candidates.get("candidates")
        phases = ledger["phases"]
        if type(recipes) is not list or not recipes or type(phases) is not list:
            raise ValueError
        expected = []
        level_ids: set[str] = set()
        for recipe in recipes:
            if type(recipe) is not dict:
                raise ValueError
            level = recipe.get("level_id")
            if (
                type(level) is not str
                or not _SAFE_LEVEL_ID.fullmatch(level)
                or level in level_ids
            ):
                raise ValueError
            level_ids.add(level)
            for phase in ("CALIBRATION", "CONFIRMATION"):
                context = recipe.get(phase.lower())
                if type(context) is not dict:
                    raise ValueError
                expected.append((level, phase, context))
        if len(phases) != len(expected):
            raise ValueError
        selected: EvaluationEngineBinding | None = None
        for item, (level, phase, context) in zip(phases, expected, strict=True):
            if (
                type(item) is not dict
                or set(item)
                != {"level_id", "phase", "engine_binding", "engine_binding_sha256"}
                or item["level_id"] != level
                or item["phase"] != phase
            ):
                raise ValueError
            binding_bytes = canonical_json_bytes(item["engine_binding"]) + b"\n"
            binding = EvaluationEngineBinding.model_validate_json(binding_bytes)
            if (
                engine_binding_bytes(binding) != binding_bytes
                or engine_binding_sha256(binding) != item["engine_binding_sha256"]
                or engine_choice_sha256(binding) != ledger["engine_choice_sha256"]
                or binding.config_sha256 != context.get("config_sha256")
                or binding.plan_sha256 != context.get("plan_sha256")
            ):
                raise ValueError
            if level == selected_level_id and phase == "CONFIRMATION":
                selected = binding
        if selected is None:
            raise ValueError
        if read_input(binding_path) != engine_binding_bytes(selected):
            raise ValueError
        return selected
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise EvaluationError(
            "SGLang confirmation engine ledger is inconsistent"
        ) from None


def recover_pinned_confirmation_catalog(
    output_root: Path, *, catalog_path: Path
) -> str:
    """Rebuild a dashboard pin from durable, mutually bound rehearsal output.

    This recovery path performs no endpoint, lifecycle, or provider action. It
    is deliberately unavailable unless every antecedent binding is canonical,
    mutually consistent, and the selected native report is complete.
    """

    bindings = _canonical_sidecar(
        output_root / "candidate-recipe-bindings.json",
        schema_version="inferdrome.evaluation-load-rehearsal-candidate-bindings.v1",
    )
    binding_sha256 = sha256_digest(canonical_json_bytes(bindings) + b"\n")
    selection_binding = _canonical_sidecar(
        output_root / "calibration-selection-binding.json",
        schema_version="inferdrome.evaluation-load-rehearsal-selection-binding.v1",
    )
    linkage = _canonical_sidecar(
        output_root / "calibration-linkage.json",
        schema_version="inferdrome.evaluation-load-rehearsal-linkage.v1",
    )
    selected_level_id = selection_binding.get("selected_level_id")
    if (
        type(selected_level_id) is not str
        or not _SAFE_LEVEL_ID.fullmatch(selected_level_id)
        or selection_binding.get("candidate_recipe_bindings_sha256") != binding_sha256
        or linkage.get("candidate_recipe_bindings_sha256") != binding_sha256
        or bindings.get("protocol_sha256") != linkage.get("protocol_sha256")
    ):
        raise EvaluationError("rehearsal recovery binding is inconsistent")
    try:
        selection_bytes = read_input(output_root / "calibration-selection.json")
        selection = CalibrationSelection.model_validate_json(selection_bytes)
        if (
            canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
            != selection_bytes
            or selection.selected_level_id != selected_level_id
            or selection_binding.get("selection_sha256")
            != sha256_digest(selection_bytes)
            or linkage.get("selection_sha256") != sha256_digest(selection_bytes)
            or selection.protocol_sha256 != bindings.get("protocol_sha256")
        ):
            raise ValueError
    except (OSError, ValueError, ValidationError):
        raise EvaluationError("rehearsal recovery selection is unavailable") from None
    candidates = bindings.get("candidates")
    if type(candidates) is not list:
        raise EvaluationError("rehearsal recovery binding is inconsistent")
    candidate = next(
        (
            value
            for value in candidates
            if type(value) is dict and value.get("level_id") == selected_level_id
        ),
        None,
    )
    if type(candidate) is not dict:
        raise EvaluationError("rehearsal recovery binding is inconsistent")
    confirmation = candidate.get("confirmation")
    if type(confirmation) is not dict:
        raise EvaluationError("rehearsal recovery binding is inconsistent")
    expected_config_sha256 = confirmation.get("config_sha256")
    expected_plan_sha256 = confirmation.get("plan_sha256")
    if not (
        type(expected_config_sha256) is str
        and _SHA256_DIGEST.fullmatch(expected_config_sha256)
        and type(expected_plan_sha256) is str
        and _SHA256_DIGEST.fullmatch(expected_plan_sha256)
    ):
        raise EvaluationError("rehearsal recovery binding is inconsistent")
    study_dir = output_root / f"confirmation-{selected_level_id}"
    engine_ledger_sha256 = selection_binding.get("engine_choice_bindings_sha256")
    if linkage.get("engine_choice_bindings_sha256") != engine_ledger_sha256:
        raise EvaluationError("SGLang confirmation engine ledger is inconsistent")
    engine_binding = _confirmation_engine_binding(
        output_root,
        selected_level_id=selected_level_id,
        expected_ledger_sha256=engine_ledger_sha256,
        expected_candidate_sha256=binding_sha256,
    )
    try:
        manifest_bytes = read_input(study_dir / "manifest.json")
        manifest = StudyManifest.model_validate_json(manifest_bytes)
        if (
            canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
            != manifest_bytes
            or manifest.status != "COMPLETED"
            or manifest.config_sha256 != expected_config_sha256
            or manifest.plan_sha256 != expected_plan_sha256
        ):
            raise ValueError
    except (OSError, ValueError, ValidationError):
        raise EvaluationError("rehearsal recovery manifest is unavailable") from None
    report_path = (
        output_root / f"confirmation-{selected_level_id}-report" / "report.json"
    )
    _, report_sha256, report = _read_confirmation_report(
        report_path,
        expected_config_sha256=expected_config_sha256,
        expected_plan_sha256=expected_plan_sha256,
        engine_binding=engine_binding,
    )
    _validate_confirmation_report_ledger(manifest, report)
    if linkage.get("confirmation_report_sha256") != report_sha256:
        raise EvaluationError("rehearsal recovery report is inconsistent")
    return _write_pinned_catalog(
        report_path=report_path,
        report_sha256=report_sha256,
        catalog_path=catalog_path,
        sglang=engine_binding is not None,
    )


def _operation_timeout_seconds(*, timeout_ns: int) -> float:
    if type(timeout_ns) is not int or timeout_ns < 1:
        raise EvaluationError("rehearsal lifecycle timeout is invalid")
    return timeout_ns / 1_000_000_000


def validate_lifecycle_reservation_bounds(
    *,
    required_prepare_timeout_ns: object,
    required_cleanup_timeout_ns: object,
    protocol: LoadCalibrationProtocol,
) -> None:
    """Validate explicit phase bounds without constructing a lifecycle."""

    cleanup_reserve = (
        protocol.preparation.warmup_reset_max_duration_ns
        if protocol.preparation.cleanup_max_duration_ns is None
        else protocol.preparation.cleanup_max_duration_ns
    )
    if (
        type(required_prepare_timeout_ns) is not int
        or required_prepare_timeout_ns < 1
        or type(required_cleanup_timeout_ns) is not int
        or required_cleanup_timeout_ns < 1
    ):
        raise EvaluationError("rehearsal lifecycle reservation is invalid")
    if (
        required_prepare_timeout_ns
        > protocol.preparation.warmup_reset_max_duration_ns
        or required_cleanup_timeout_ns > cleanup_reserve
    ):
        raise EvaluationError(
            "protocol lifecycle reserve cannot run the supplied lifecycle"
        )


def _validate_lifecycle_reservation(
    lifecycle: TrialLifecycle, protocol: LoadCalibrationProtocol
) -> None:
    """Reject a known lifecycle whose reset/readiness cannot fit its declaration."""

    cleanup_reserve = (
        protocol.preparation.warmup_reset_max_duration_ns
        if protocol.preparation.cleanup_max_duration_ns is None
        else protocol.preparation.cleanup_max_duration_ns
    )
    required_prepare = getattr(lifecycle, "required_prepare_timeout_ns", None)
    required_cleanup = getattr(lifecycle, "required_cleanup_timeout_ns", None)
    if required_prepare is not None or required_cleanup is not None:
        validate_lifecycle_reservation_bounds(
            required_prepare_timeout_ns=required_prepare,
            required_cleanup_timeout_ns=required_cleanup,
            protocol=protocol,
        )
        return
    required = getattr(lifecycle, "required_operation_timeout_ns", None)
    if required is None:
        return
    if type(required) is not int or required < 1:
        raise EvaluationError("rehearsal lifecycle reservation is invalid")
    if (
        required > protocol.preparation.warmup_reset_max_duration_ns
        or required > cleanup_reserve
    ):
        raise EvaluationError(
            "protocol lifecycle reserve cannot run the supplied lifecycle"
        )


def _set_lifecycle_deadline(lifecycle: TrialLifecycle, deadline_ns: int) -> None:
    """Give a deadline-aware lifecycle the phase's original absolute cutoff."""

    setter = getattr(lifecycle, "set_operation_deadline", None)
    if setter is None:
        return
    if not callable(setter):
        raise EvaluationError("rehearsal lifecycle deadline boundary is invalid")
    try:
        setter(deadline_ns)
    except Exception as error:
        raise EvaluationError(
            "rehearsal lifecycle deadline boundary is invalid"
        ) from error


async def _run_phase(
    *,
    phase: Literal["CALIBRATION", "CONFIRMATION"],
    config: StudyConfig,
    plan: CompiledStudy,
    output_dir: Path,
    receipt_path: Path,
    lifecycle: TrialLifecycle,
    executor: StudyExecutor,
    stop: asyncio.Event,
    lifecycle_prepare_timeout_ns: int,
    lifecycle_cleanup_timeout_ns: int,
    execution_deadline_ns: int,
    hard_deadline_ns: int,
    engine_binding: EvaluationEngineBinding | None = None,
) -> StudyManifest:
    receipts: list[RehearsalLifecycleReceipt] = []

    def remaining_timeout_seconds(deadline_ns: int, *, timeout_ns: int) -> float:
        remaining_ns = deadline_ns - monotonic_ns()
        if remaining_ns < 1:
            stop.set()
            raise EvaluationError("rehearsal whole-session deadline expired")
        return _operation_timeout_seconds(timeout_ns=min(timeout_ns, remaining_ns))

    async def owned_trial(trial: CompiledTrial, *, stop: asyncio.Event) -> TrialResult:
        prepare_started = monotonic_ns()
        execute_started: int | None = None
        prepared = False
        result: TrialResult | None = None
        try:
            _set_lifecycle_deadline(lifecycle, execution_deadline_ns)
            await asyncio.wait_for(
                lifecycle.prepare(trial, stop=stop),
                timeout=remaining_timeout_seconds(
                    execution_deadline_ns,
                    timeout_ns=lifecycle_prepare_timeout_ns,
                ),
            )
            prepared = True
            sglang_prepare = _sglang_prepare_receipt(
                lifecycle, phase=phase, trial=trial
            )
            if sglang_prepare is not None:
                # Persist the completed reset before native request dispatch.
                # If a later request or process dies, this receipt is still a
                # bounded, no-replace record of the launch/readiness/reset
                # sequence that actually completed; it is never cache proof.
                receipt_index = plan.trials.index(trial)
                _write_sidecar(
                    receipt_path.with_name(
                        receipt_path.stem + f"-sglang-reset-{receipt_index:04d}.json"
                    ),
                    _sglang_prepare_bytes(
                        phase=phase, plan=plan, receipts=(sglang_prepare,)
                    ),
                )
            execute_started = monotonic_ns()
            result = await executor(trial, stop=stop)
        finally:
            cleanup_started = monotonic_ns()
            execute_elapsed = (
                None
                if execute_started is None
                else max(0, cleanup_started - execute_started)
            )
            try:
                _set_lifecycle_deadline(lifecycle, hard_deadline_ns)
                await asyncio.wait_for(
                    lifecycle.cleanup(trial, stop=stop),
                    timeout=remaining_timeout_seconds(
                        hard_deadline_ns,
                        timeout_ns=lifecycle_cleanup_timeout_ns,
                    ),
                )
            except (Exception, asyncio.CancelledError):
                receipts.append(
                    RehearsalLifecycleReceipt(
                        schema_version="inferdrome.evaluation-load-rehearsal-lifecycle.v1",
                        phase=phase,
                        source_trial_id=trial.trial_id,
                        reset_and_warmup="COMPLETED" if prepared else "FAILED",
                        cleanup="FAILED",
                        prepare_elapsed_ns=max(
                            0,
                            (execute_started or cleanup_started) - prepare_started,
                        ),
                        execute_elapsed_ns=execute_elapsed,
                        cleanup_elapsed_ns=max(0, monotonic_ns() - cleanup_started),
                    )
                )
                raise
            receipts.append(
                RehearsalLifecycleReceipt(
                    schema_version="inferdrome.evaluation-load-rehearsal-lifecycle.v1",
                    phase=phase,
                    source_trial_id=trial.trial_id,
                    reset_and_warmup="COMPLETED" if prepared else "FAILED",
                    cleanup="CONFIRMED",
                    prepare_elapsed_ns=max(
                        0, (execute_started or cleanup_started) - prepare_started
                    ),
                    execute_elapsed_ns=execute_elapsed,
                    cleanup_elapsed_ns=max(0, monotonic_ns() - cleanup_started),
                )
            )
        assert result is not None
        return result

    if engine_binding is None:
        manifest = await run_study(config, output_dir, executor=owned_trial, stop=stop)
    else:
        manifest = await run_study(
            config,
            output_dir,
            executor=owned_trial,
            stop=stop,
            engine_binding=engine_binding,
        )
    _write_sidecar(
        receipt_path,
        _lifecycle_bytes(phase=phase, plan=plan, receipts=tuple(receipts)),
    )
    return manifest


def _observations(
    recipe: BoundCandidateRecipe,
    study_dir: Path,
    engine_binding: EvaluationEngineBinding | None = None,
) -> tuple[CalibrationTrialObservation, ...]:
    rows: list[CalibrationTrialObservation] = []
    with StudyDirectory.open(
        study_dir, budget=recipe.calibration_config.limits.total_output_bytes
    ) as directory:
        if engine_binding is not None and directory.read(
            "engine-binding.json", limit=_SIDECAR_OUTPUT_BYTES
        ) != engine_binding_bytes(engine_binding):
            raise EvaluationError("calibration engine binding changed before reduction")
        manifest = read_manifest(directory, recipe.calibration_plan)
        if manifest.status != "COMPLETED":
            raise EvaluationError("incomplete calibration study cannot select a level")
        for binding in recipe.calibration:
            entry = manifest.trials[binding.source_index]
            if entry.state != "RETURNED" or entry.result_sha256 is None:
                raise EvaluationError("calibration study lacks a returned trial")
            content = directory.read(
                trial_filename(binding.source_index),
                limit=recipe.calibration_config.limits.per_trial_result_bytes,
            )
            if sha256_digest(content) != entry.result_sha256:
                raise EvaluationError("calibration trial bytes differ from its ledger")
            if engine_binding is None:
                validated = load_study_trial_bytes(
                    content, recipe.calibration_plan, binding.source_trial
                )
            else:
                # Only the existing numerical reducers receive this private
                # input. Public artifacts retain the full SGLang binding.
                validated = load_sglang_trial_bytes(
                    content,
                    recipe.calibration_plan,
                    binding.source_trial,
                    engine_binding,
                )._statistical_input
            summary = summarize_trial(binding.source_trial, validated)
            foreground = summary["foreground"]
            assert isinstance(foreground, dict)
            outcomes = foreground["outcomes"]
            latency = foreground["successful_latency_ns"]
            assert isinstance(outcomes, dict) and isinstance(latency, dict)
            counts = {
                name: value["count"]
                for name, value in outcomes.items()
                if isinstance(value, dict) and isinstance(value.get("count"), int)
            }
            declared = binding.declared_trial
            assert isinstance(declared, CalibrationTrial)
            rows.append(
                CalibrationTrialObservation(
                    trial_id=declared.trial_id,
                    level_id=declared.level_id,
                    repeat_index=declared.repeat_index,
                    policy_id=declared.policy_id,
                    scenario="HEALTHY",
                    study_plan_sha256=declared.study_plan_sha256,
                    offered_count=foreground["offered_count"],
                    dispatched_count=foreground["dispatched_count"],
                    terminal_count=sum(counts.values()),
                    outcomes=counts,
                    slo_good_count=foreground["slo_good_count"],
                    achieved_concurrency_peak=foreground["peak_active"],
                    client_queue_peak=foreground["peak_queue"],
                    first_content_p95_ns=latency["scheduled_to_first_content"]["p95"],
                    terminal_p95_ns=latency["scheduled_to_terminal"]["p95"],
                    offered_window_ns=foreground["fixed_offered_window_ns"],
                )
            )
    return tuple(rows)


async def run_rehearsal(
    rehearsal: CompiledRehearsal,
    output_root: Path,
    *,
    lifecycle: TrialLifecycle,
    executor: StudyExecutor | None = None,
    sglang_profiles: Mapping[EndpointId, SglangServingConfig] | None = None,
    containerized: bool = False,
    session_window: RehearsalSessionWindow | None = None,
    stop: asyncio.Event | None = None,
) -> RehearsalResult:
    """Run one non-renewable session through calibration and confirmation.

    The absolute session clock starts before the no-replace candidate ledger.
    Its execution window ends early enough to retain fixed cleanup and
    retrieval reserves; neither a restarted phase nor a later report can
    extend that original hard cutoff.
    """
    protocol = rehearsal.calibration_plan.protocol
    _validate_lifecycle_reservation(lifecycle, protocol)
    lifecycle_cleanup_timeout_ns = (
        protocol.preparation.warmup_reset_max_duration_ns
        if protocol.preparation.cleanup_max_duration_ns is None
        else protocol.preparation.cleanup_max_duration_ns
    )
    if type(containerized) is not bool:
        raise EvaluationError("rehearsal launch mapping is invalid")
    engines = None
    if sglang_profiles is None:
        if (
            executor is None
            or containerized
            or getattr(lifecycle, "engine_choice_sha256", None) is not None
        ):
            raise EvaluationError("native rehearsal requires its explicit executor")
    else:
        if executor is not None or isinstance(
            lifecycle, TwoEngineVllmSubprocessLifecycle
        ):
            raise EvaluationError("SGLang rehearsal cannot mix native engine executors")
        engines = bind_sglang_rehearsal(
            rehearsal, sglang_profiles, containerized=containerized
        )
        owned_choice = getattr(lifecycle, "engine_choice_sha256", None)
        if owned_choice is not None and owned_choice != engines.engine_choice_sha256:
            raise EvaluationError(
                "SGLang lifecycle differs from the declared engine choice"
            )
    observed_start_ns = monotonic_ns()
    if session_window is None:
        started_ns = observed_start_ns
        hard_deadline_ns = started_ns + protocol.max_session_duration_ns
    else:
        if type(session_window) is not RehearsalSessionWindow:
            raise EvaluationError("rehearsal session window is invalid")
        session_window.validated_for(protocol, now_ns=observed_start_ns)
        started_ns = session_window.started_ns
        hard_deadline_ns = session_window.hard_deadline_ns
    execution_deadline_ns = (
        hard_deadline_ns
        - rehearsal.final_cleanup_reserve_ns
        - rehearsal.retrieval_reserve_ns
    )
    if execution_deadline_ns <= started_ns:
        raise EvaluationError("rehearsal session has no execution window")

    if stop is not None and type(stop) is not asyncio.Event:
        raise EvaluationError("rehearsal stop boundary is invalid")
    external_stop = stop
    stop = stop or asyncio.Event()

    def require_before(deadline_ns: int, *, message: str) -> None:
        # A caller-provided signal boundary must prevent a later dispatch.
        # The native default event is also set by expected trial cancellation;
        # preserving its historical report/reduction path avoids relabelling a
        # lifecycle failure as an external session cancellation.
        if external_stop is not None and external_stop.is_set():
            raise EvaluationError("rehearsal session was cancelled")
        if monotonic_ns() >= deadline_ns:
            stop.set()
            raise EvaluationError(message)

    candidate_recipe_bindings = _candidate_recipe_bindings_bytes(rehearsal)
    candidate_recipe_bindings_sha256 = sha256_digest(candidate_recipe_bindings)
    engine_ledger = (
        engines.ledger_bytes(candidate_recipe_bindings_sha256)
        if engines is not None
        else None
    )
    engine_output_reserve = (
        len(engine_ledger)
        + (len(rehearsal.candidates) + 1)
        * (2 * MAX_SGLANG_REPORT_BYTES - _REPORT_OUTPUT_BYTES)
        if engine_ledger is not None
        else 0
    )
    sglang_prepare_reserve = (
        2 * len(rehearsal.candidates) * _SIDECAR_OUTPUT_BYTES
        if engines is not None
        else 0
    )
    if engine_ledger is not None and (
        len(engine_ledger) > _SIDECAR_OUTPUT_BYTES
        or rehearsal.reserved_output_bytes
        + engine_output_reserve
        + sglang_prepare_reserve
        > protocol.max_session_output_bytes
    ):
        raise EvaluationError("SGLang engine ledger exceeds the session output reserve")
    # This no-replace write is deliberately before every lifecycle call. A
    # failed/tampered/reserved ledger therefore means no
    # endpoint reset, warmup, transport, or native dispatch.
    # The session clock is deliberately already running: a stalled durable
    # reservation cannot be used to mint a new full execution interval.
    _write_sidecar(
        output_root / "candidate-recipe-bindings.json", candidate_recipe_bindings
    )
    if engine_ledger is not None:
        _write_sidecar(output_root / "engine-choice-bindings.json", engine_ledger)

    async def expire_execution_window() -> None:
        await asyncio.sleep(
            max(0, execution_deadline_ns - monotonic_ns()) / 1_000_000_000
        )
        stop.set()

    def require_active_session() -> None:
        require_before(
            execution_deadline_ns, message="rehearsal execution window expired"
        )

    def require_retrieval_window() -> None:
        require_before(
            hard_deadline_ns,
            message="rehearsal whole-session deadline expired",
        )

    deadline = asyncio.create_task(expire_execution_window())
    manifests: list[tuple[str, StudyManifest]] = []
    observations: list[CalibrationTrialObservation] = []
    calibration_reports: list[dict[str, str]] = []
    try:
        for candidate in rehearsal.candidates:
            require_active_session()
            stem = f"calibration-{candidate.level.level_id}"
            study_dir = output_root / stem
            phase_binding = (
                engines.binding_for(candidate.level.level_id, "CALIBRATION")
                if engines is not None
                else None
            )
            phase_executor = (
                engines.executor_for(candidate.level.level_id, "CALIBRATION")
                if engines is not None
                else executor
            )
            assert phase_executor is not None
            manifest = await _run_phase(
                phase="CALIBRATION",
                config=candidate.calibration_config,
                plan=candidate.calibration_plan,
                output_dir=study_dir,
                receipt_path=output_root / f"{stem}-lifecycle.json",
                lifecycle=lifecycle,
                executor=phase_executor,
                stop=stop,
                lifecycle_prepare_timeout_ns=(
                    protocol.preparation.warmup_reset_max_duration_ns
                ),
                lifecycle_cleanup_timeout_ns=lifecycle_cleanup_timeout_ns,
                execution_deadline_ns=execution_deadline_ns,
                hard_deadline_ns=hard_deadline_ns,
                engine_binding=phase_binding,
            )
            require_active_session()
            manifests.append((candidate.level.level_id, manifest))
            report_dir = output_root / f"{stem}-report"
            if phase_binding is None:
                report_study(candidate.calibration_config, study_dir, report_dir)
            else:
                report_study(
                    candidate.calibration_config,
                    study_dir,
                    report_dir,
                    engine_binding=phase_binding,
                )
            require_retrieval_window()
            calibration_reports.append(
                {
                    "level_id": candidate.level.level_id,
                    "calibration_recipe_sha256": candidate.calibration_recipe_sha256,
                    "confirmation_recipe_sha256": candidate.confirmation_recipe_sha256,
                    "calibration_report_sha256": sha256_digest(
                        (report_dir / "report.json").read_bytes()
                    ),
                }
            )
            observations.extend(_observations(candidate, study_dir, phase_binding))
        observed = CalibrationObservationSet(
            schema_version="inferdrome.evaluation-load-calibration-observations.v1",
            protocol_sha256=rehearsal.calibration_plan.protocol_sha256,
            trials=tuple(observations),
        )
        selection = select_calibration_level(rehearsal.calibration_plan, observed)
        confirmation = confirmation_plan(rehearsal.calibration_plan, selection)
        require_retrieval_window()
        _write_sidecar(
            output_root / "calibration-plan.json",
            calibration_plan_bytes(rehearsal.calibration_plan),
        )
        _write_sidecar(
            output_root / "calibration-selection.json",
            canonical_json_bytes(selection.model_dump(mode="json")) + b"\n",
        )
        _write_sidecar(
            output_root / "calibration-selection-binding.json",
            canonical_json_bytes(
                {
                    "schema_version": (
                        "inferdrome.evaluation-load-rehearsal-selection-binding.v1"
                    ),
                    "candidate_recipe_bindings_sha256": (
                        candidate_recipe_bindings_sha256
                    ),
                    "selection_sha256": sha256_digest(
                        canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
                    ),
                    "selected_level_id": selection.selected_level_id,
                    **(
                        {"engine_choice_bindings_sha256": sha256_digest(engine_ledger)}
                        if engine_ledger is not None
                        else {}
                    ),
                    "evidence_eligible": False,
                    "runtime_identity": "UNVERIFIED",
                }
            )
            + b"\n",
        )
        _write_sidecar(
            output_root / "confirmation-plan.json",
            canonical_json_bytes(confirmation.model_dump(mode="json")) + b"\n",
        )

        confirmation_manifest: StudyManifest | None = None
        confirmation_report_sha256: str | None = None
        confirmation_report_path: Path | None = None
        if selection.selected_level_id is not None:
            require_active_session()
            candidate = next(
                item
                for item in rehearsal.candidates
                if item.level.level_id == selection.selected_level_id
            )
            study_dir = output_root / f"confirmation-{candidate.level.level_id}"
            phase_binding = (
                engines.binding_for(candidate.level.level_id, "CONFIRMATION")
                if engines is not None
                else None
            )
            phase_executor = (
                engines.executor_for(candidate.level.level_id, "CONFIRMATION")
                if engines is not None
                else executor
            )
            assert phase_executor is not None
            confirmation_manifest = await _run_phase(
                phase="CONFIRMATION",
                config=candidate.confirmation_config,
                plan=candidate.confirmation_plan,
                output_dir=study_dir,
                receipt_path=(
                    output_root
                    / f"confirmation-{candidate.level.level_id}-lifecycle.json"
                ),
                lifecycle=lifecycle,
                executor=phase_executor,
                stop=stop,
                lifecycle_prepare_timeout_ns=(
                    protocol.preparation.warmup_reset_max_duration_ns
                ),
                lifecycle_cleanup_timeout_ns=lifecycle_cleanup_timeout_ns,
                execution_deadline_ns=execution_deadline_ns,
                hard_deadline_ns=hard_deadline_ns,
                engine_binding=phase_binding,
            )
            require_active_session()
            report_dir = output_root / f"confirmation-{candidate.level.level_id}-report"
            if phase_binding is None:
                report_study(candidate.confirmation_config, study_dir, report_dir)
            else:
                report_study(
                    candidate.confirmation_config,
                    study_dir,
                    report_dir,
                    engine_binding=phase_binding,
                )
            require_retrieval_window()
            confirmation_report_path = report_dir / "report.json"
            confirmation_report_sha256 = sha256_digest(
                confirmation_report_path.read_bytes()
            )
        require_retrieval_window()
        _write_sidecar(
            output_root / "calibration-linkage.json",
            canonical_json_bytes(
                {
                    "schema_version": "inferdrome.evaluation-load-rehearsal-linkage.v1",
                    "protocol_sha256": rehearsal.calibration_plan.protocol_sha256,
                    "session_worst_case_duration_ns": rehearsal.worst_case_duration_ns,
                    "session_execution_worst_case_duration_ns": (
                        rehearsal.execution_worst_case_duration_ns
                    ),
                    "session_final_cleanup_reserve_ns": (
                        rehearsal.final_cleanup_reserve_ns
                    ),
                    "session_retrieval_reserve_ns": rehearsal.retrieval_reserve_ns,
                    "session_reserved_output_bytes": (
                        rehearsal.reserved_output_bytes
                        + engine_output_reserve
                        + sglang_prepare_reserve
                    ),
                    "candidate_recipe_bindings_sha256": (
                        candidate_recipe_bindings_sha256
                    ),
                    "selection_sha256": sha256_digest(
                        canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
                    ),
                    "candidate_bindings": calibration_reports,
                    "confirmation_report_sha256": confirmation_report_sha256,
                    **(
                        {"engine_choice_bindings_sha256": sha256_digest(engine_ledger)}
                        if engine_ledger is not None
                        else {}
                    ),
                    "evidence_eligible": False,
                    "runtime_identity": "UNVERIFIED",
                }
            )
            + b"\n",
        )
        require_retrieval_window()
        return RehearsalResult(
            calibration_selection=selection,
            confirmation=confirmation,
            calibration_manifests=tuple(manifests),
            confirmation_manifest=confirmation_manifest,
            confirmation_report_path=confirmation_report_path,
            candidate_recipe_bindings_sha256=candidate_recipe_bindings_sha256,
            engine_choice_bindings_sha256=(
                sha256_digest(engine_ledger) if engine_ledger is not None else None
            ),
        )
    finally:
        deadline.cancel()
        await asyncio.gather(deadline, return_exceptions=True)
