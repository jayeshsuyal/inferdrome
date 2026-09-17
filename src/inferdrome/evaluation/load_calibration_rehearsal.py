"""Local-only execution bridge for the frozen load-calibration protocol.

The bridge deliberately reuses native ``StudyConfig`` compilation, ``run_study``
execution, trial artifacts, and offline reports.  It does not construct a new
router or alter historical study-report classifications.  Callers supply every
candidate recipe and an owned lifecycle before any trial runs; the helper then
binds the protocol declarations to exact compiled trial IDs.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import ValidationError

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
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

    async def run(
        self, argv: tuple[str, ...], *, timeout_ns: int
    ) -> SubprocessResult:
        if not argv or type(timeout_ns) is not int or timeout_ns < 1:
            raise EvaluationError("local subprocess command is invalid")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
        except BaseException as error:
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


class TwoEngineVllmSubprocessLifecycle:
    """Concrete local cold-reset lifecycle for two pinned vLLM engine processes.

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
        readiness_timeout_ns: int = 1_000_000_000,
        startup_timeout_ns: int = _LOCAL_ENGINE_STARTUP_TIMEOUT_NS,
    ) -> None:
        if not _SAFE_OWNERSHIP_ID.fullmatch(ownership_id):
            raise EvaluationError("local engine ownership identifier is invalid")
        if (
            not model_snapshot_path.is_absolute()
            or model_snapshot_path.is_symlink()
            or any(
                character in str(model_snapshot_path)
                for character in (",", "\n", "\r")
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
        self._readiness_timeout_ns = readiness_timeout_ns
        self._startup_timeout_ns = startup_timeout_ns
        self._active_names: tuple[str, ...] = ()
        self._snapshot_verified = False

    @property
    def required_operation_timeout_ns(self) -> int:
        """Maximum one prepare/cleanup operation time that a protocol must reserve."""

        prepare = 2 * _LOCAL_ENGINE_COMMAND_TIMEOUT_NS + self._startup_timeout_ns
        cleanup = 4 * _LOCAL_ENGINE_COMMAND_TIMEOUT_NS
        return max(prepare, cleanup)

    def _engine_name(self, trial: CompiledTrial, endpoint_id: str) -> str:
        return f"inferdrome-lcr-{self._ownership_id}-{trial.trial_id}-{endpoint_id}"

    def _engine_argv(
        self, trial: CompiledTrial, *, index: int
    ) -> tuple[str, ...]:
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
            "type=bind,src="
            f"{self._model_snapshot_path},dst=/model,readonly",
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

    async def _command(self, argv: tuple[str, ...]) -> bytes:
        result = await self._runner.run(
            argv, timeout_ns=_LOCAL_ENGINE_COMMAND_TIMEOUT_NS
        )
        if (
            result.argv != argv
            or result.returncode != 0
            or len(result.stdout) > _SUBPROCESS_OUTPUT_BYTES
            or len(result.stderr) > _SUBPROCESS_OUTPUT_BYTES
        ):
            raise EvaluationError("exact-owned local engine command failed")
        return result.stdout

    async def _verify_gpu_idle(self) -> None:
        for index in (0, 1):
            content = await self._command(
                (
                    "nvidia-smi",
                    f"--id={index}",
                    "--query-compute-apps=pid",
                    "--format=csv,noheader",
                )
            )
            if content.strip():
                raise EvaluationError("declared GPU is not idle")

    async def _remove_active(self) -> None:
        errors: list[BaseException] = []
        for name in reversed(self._active_names):
            try:
                await self._command(("docker", "rm", "--force", name))
            except BaseException as error:
                errors.append(error)
        self._active_names = ()
        try:
            await self._verify_gpu_idle()
        except BaseException as error:
            errors.append(error)
        if errors:
            raise EvaluationError(
                "exact-owned local engine cleanup is unconfirmed"
            ) from errors[0]

    async def _await_ready_and_warm(
        self, trial: CompiledTrial, stop: asyncio.Event
    ) -> None:
        deadline_ns = monotonic_ns() + self._startup_timeout_ns
        delay_seconds = 0.05
        last_error: BaseException | None = None
        while monotonic_ns() < deadline_ns:
            if stop.is_set():
                raise EvaluationError("local engine lifecycle was cancelled")
            try:
                await self._readiness.prepare(trial, stop=stop)
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            self._warmup_probe,
                            origin,
                            min(
                                self._readiness_timeout_ns,
                                _LOCAL_ENGINE_WARMUP_TIMEOUT_NS,
                            )
                            / 1_000_000_000,
                        )
                        for origin in self._origins
                    )
                )
                return
            except (EvaluationError, OSError) as error:
                last_error = error
            remaining_ns = deadline_ns - monotonic_ns()
            if remaining_ns < 1:
                break
            await asyncio.sleep(min(delay_seconds, remaining_ns / 1_000_000_000))
            delay_seconds = min(1.0, delay_seconds * 2)
        raise EvaluationError("local engine readiness deadline expired") from last_error

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        if stop.is_set():
            raise EvaluationError("local engine lifecycle was cancelled")
        self._readiness._bound_to_trial(trial)
        if self._active_names:
            raise EvaluationError("local engine lifecycle has an active trial")
        if (
            not self._model_snapshot_path.is_dir()
            or self._model_snapshot_path.is_symlink()
        ):
            raise EvaluationError("preloaded model snapshot is unavailable")
        if not self._snapshot_verified:
            self._snapshot_verifier(self._model_snapshot_path)
            self._snapshot_verified = True
        await self._verify_gpu_idle()
        try:
            for index, endpoint_id in enumerate(("endpoint-a", "endpoint-b")):
                content = await self._command(self._engine_argv(trial, index=index))
                if not re.fullmatch(rb"[0-9a-f]{64}\n?", content):
                    raise EvaluationError("local engine did not return an exact ID")
                self._active_names = (
                    *self._active_names,
                    self._engine_name(trial, endpoint_id),
                )
            await self._await_ready_and_warm(trial, stop)
        except BaseException as error:
            try:
                await self._remove_active()
            except BaseException as cleanup_error:
                raise EvaluationError(
                    "local engine startup failed and cleanup is unconfirmed"
                ) from cleanup_error
            raise EvaluationError("local engine startup or warmup failed") from error

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        # Cleanup deliberately remains available after a deadline/cancellation.
        del stop
        self._readiness._bound_to_trial(trial)
        await self._remove_active()


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
    lifecycle_reserve = (
        2 * len(plan.trials) * protocol.preparation.warmup_reset_max_duration_ns
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
    execution_worst_case_duration_ns = (
        calibration_duration
        + confirmation_duration
        + 2 * lifecycle_trials * protocol.preparation.warmup_reset_max_duration_ns
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


def _write_sidecar(path: Path, content: bytes) -> None:
    """Write one bounded, no-replace rehearsal control artifact."""

    if not 1 <= len(content) <= _SIDECAR_OUTPUT_BYTES:
        raise EvaluationError("rehearsal sidecar exceeds its output bound")
    with OutputFile(path) as destination:
        destination.write(content)


def _read_confirmation_report(
    report_path: Path, *, expected_config_sha256: str, expected_plan_sha256: str
) -> tuple[bytes, str]:
    """Independently re-read one complete native confirmation report."""

    if (
        report_path.name != "report.json"
        or not _SHA256_DIGEST.fullmatch(expected_config_sha256)
        or not _SHA256_DIGEST.fullmatch(expected_plan_sha256)
    ):
        raise EvaluationError("rehearsal confirmation report is unavailable")
    try:
        with StudyDirectory.open(report_path.parent) as directory:
            content = directory.read("report.json", limit=_SIDECAR_OUTPUT_BYTES)
        from inferdrome.evaluation.report_reader import load_evaluation_report_bytes

        loaded = load_evaluation_report_bytes(content, kind="STUDY")
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
    return content, sha256_digest(content)


def _write_pinned_catalog(
    *, report_path: Path, report_sha256: str, catalog_path: Path
) -> str:
    _write_sidecar(
        catalog_path,
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
                "entries": [
                    {
                        "kind": "STUDY",
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
    _, digest = _read_confirmation_report(
        report_path,
        expected_config_sha256=manifest.config_sha256,
        expected_plan_sha256=manifest.plan_sha256,
    )
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
        or selection_binding.get("candidate_recipe_bindings_sha256")
        != binding_sha256
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
    _, report_sha256 = _read_confirmation_report(
        report_path,
        expected_config_sha256=expected_config_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if linkage.get("confirmation_report_sha256") != report_sha256:
        raise EvaluationError("rehearsal recovery report is inconsistent")
    return _write_pinned_catalog(
        report_path=report_path,
        report_sha256=report_sha256,
        catalog_path=catalog_path,
    )


def _operation_timeout_seconds(*, timeout_ns: int) -> float:
    if type(timeout_ns) is not int or timeout_ns < 1:
        raise EvaluationError("rehearsal lifecycle timeout is invalid")
    return timeout_ns / 1_000_000_000


def _validate_lifecycle_reservation(
    lifecycle: TrialLifecycle, protocol: LoadCalibrationProtocol
) -> None:
    """Reject a known lifecycle whose reset/readiness cannot fit its declaration."""

    required = getattr(lifecycle, "required_operation_timeout_ns", None)
    if required is None:
        return
    if type(required) is not int or required < 1:
        raise EvaluationError("rehearsal lifecycle reservation is invalid")
    if required > protocol.preparation.warmup_reset_max_duration_ns:
        raise EvaluationError(
            "protocol lifecycle reserve cannot run the supplied lifecycle"
        )


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
    lifecycle_timeout_ns: int,
    execution_deadline_ns: int,
    hard_deadline_ns: int,
) -> StudyManifest:
    receipts: list[RehearsalLifecycleReceipt] = []

    def remaining_timeout_seconds(deadline_ns: int) -> float:
        remaining_ns = deadline_ns - monotonic_ns()
        if remaining_ns < 1:
            stop.set()
            raise EvaluationError("rehearsal whole-session deadline expired")
        return _operation_timeout_seconds(
            timeout_ns=min(lifecycle_timeout_ns, remaining_ns)
        )

    async def owned_trial(trial: CompiledTrial, *, stop: asyncio.Event) -> TrialResult:
        prepare_started = monotonic_ns()
        execute_started: int | None = None
        prepared = False
        result: TrialResult | None = None
        try:
            await asyncio.wait_for(
                lifecycle.prepare(trial, stop=stop),
                timeout=remaining_timeout_seconds(execution_deadline_ns),
            )
            prepared = True
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
                await asyncio.wait_for(
                    lifecycle.cleanup(trial, stop=stop),
                    timeout=remaining_timeout_seconds(hard_deadline_ns),
                )
            except BaseException:
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

    manifest = await run_study(config, output_dir, executor=owned_trial, stop=stop)
    _write_sidecar(
        receipt_path,
        _lifecycle_bytes(phase=phase, plan=plan, receipts=tuple(receipts)),
    )
    return manifest


def _observations(
    recipe: BoundCandidateRecipe, study_dir: Path
) -> tuple[CalibrationTrialObservation, ...]:
    rows: list[CalibrationTrialObservation] = []
    with StudyDirectory.open(
        study_dir, budget=recipe.calibration_config.limits.total_output_bytes
    ) as directory:
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
            summary = summarize_trial(
                binding.source_trial,
                load_study_trial_bytes(
                    content, recipe.calibration_plan, binding.source_trial
                ),
            )
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
    executor: StudyExecutor,
) -> RehearsalResult:
    """Run one non-renewable session through calibration and confirmation.

    The absolute session clock starts before the no-replace candidate ledger.
    Its execution window ends early enough to retain fixed cleanup and
    retrieval reserves; neither a restarted phase nor a later report can
    extend that original hard cutoff.
    """
    protocol = rehearsal.calibration_plan.protocol
    _validate_lifecycle_reservation(lifecycle, protocol)
    started_ns = monotonic_ns()
    hard_deadline_ns = started_ns + protocol.max_session_duration_ns
    execution_deadline_ns = (
        hard_deadline_ns
        - rehearsal.final_cleanup_reserve_ns
        - rehearsal.retrieval_reserve_ns
    )
    if execution_deadline_ns <= started_ns:
        raise EvaluationError("rehearsal session has no execution window")

    def require_before(deadline_ns: int, *, message: str) -> None:
        if monotonic_ns() >= deadline_ns:
            stop.set()
            raise EvaluationError(message)

    candidate_recipe_bindings = _candidate_recipe_bindings_bytes(rehearsal)
    # This no-replace write is deliberately before every lifecycle call. A
    # failed/tampered/reserved ledger therefore means no
    # endpoint reset, warmup, transport, or native dispatch.
    # The session clock is deliberately already running: a stalled durable
    # reservation cannot be used to mint a new full execution interval.
    _write_sidecar(
        output_root / "candidate-recipe-bindings.json", candidate_recipe_bindings
    )
    candidate_recipe_bindings_sha256 = sha256_digest(candidate_recipe_bindings)
    stop = asyncio.Event()

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
            manifest = await _run_phase(
                phase="CALIBRATION",
                config=candidate.calibration_config,
                plan=candidate.calibration_plan,
                output_dir=study_dir,
                receipt_path=output_root / f"{stem}-lifecycle.json",
                lifecycle=lifecycle,
                executor=executor,
                stop=stop,
                lifecycle_timeout_ns=protocol.preparation.warmup_reset_max_duration_ns,
                execution_deadline_ns=execution_deadline_ns,
                hard_deadline_ns=hard_deadline_ns,
            )
            require_active_session()
            manifests.append((candidate.level.level_id, manifest))
            report_dir = output_root / f"{stem}-report"
            report_study(candidate.calibration_config, study_dir, report_dir)
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
            observations.extend(_observations(candidate, study_dir))
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
                        canonical_json_bytes(selection.model_dump(mode="json"))
                        + b"\n"
                    ),
                    "selected_level_id": selection.selected_level_id,
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
                executor=executor,
                stop=stop,
                lifecycle_timeout_ns=(
                    protocol.preparation.warmup_reset_max_duration_ns
                ),
                execution_deadline_ns=execution_deadline_ns,
                hard_deadline_ns=hard_deadline_ns,
            )
            require_active_session()
            report_dir = output_root / f"confirmation-{candidate.level.level_id}-report"
            report_study(candidate.confirmation_config, study_dir, report_dir)
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
                    "session_reserved_output_bytes": rehearsal.reserved_output_bytes,
                    "candidate_recipe_bindings_sha256": (
                        candidate_recipe_bindings_sha256
                    ),
                    "selection_sha256": sha256_digest(
                        canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
                    ),
                    "candidate_bindings": calibration_reports,
                    "confirmation_report_sha256": confirmation_report_sha256,
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
        )
    finally:
        deadline.cancel()
        await asyncio.gather(deadline, return_exceptions=True)
