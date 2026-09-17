"""Pinned SGLang launch/preparation hooks on the shared exact-owned lifecycle.

Constructing this owner does not start Docker or contact a GPU. Runtime callers
must preload image and declared artifacts. Tests inject CPU-only command and
HTTP seams. Reset acceptance and readbacks remain runtime UNVERIFIED.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns

from inferdrome.evaluation.contracts import (
    EndpointId,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    engine_binding_bytes,
    engine_choice_sha256,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    LocalSubprocessRunner,
    _assert_loopback_port_closed,
    _TwoEngineOwnedSubprocessLifecycle,
)
from inferdrome.evaluation.runner import Transport
from inferdrome.evaluation.sglang_container import (
    build_sglang_container_profile,
    verify_sglang_artifacts,
)
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.sglang_readiness import (
    AiohttpSGLangProbeTransport,
    SGLangCacheReset,
    SGLangProbeTransport,
    SGLangReadinessAcquisition,
    acquire_sglang_readiness,
    reset_sglang_cache,
)
from inferdrome.evaluation.stream import StreamParser
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.routing_execution.canonical import canonical_json_bytes

_ENDPOINTS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")


@dataclass(frozen=True, slots=True)
class SGLangOwnedReset:
    """Bind mapped server declarations back to the original launch declaration."""

    endpoint_id: EndpointId
    source_profile_sha256: str
    projected_launch_sha256: str
    readiness_config_sha256: str
    reset: SGLangCacheReset


class TwoEngineSGLangSubprocessLifecycle(_TwoEngineOwnedSubprocessLifecycle):
    """Two containerized profiles with one predeclared choice across study phases.

    The base owns GPU 0/1, exact creates, pending reconciliation, removal, port
    and GPU readbacks and operation reservations. These hooks only select the
    pinned image/argv, artifact checks and SGLang preparation semantics.
    """

    def __init__(
        self,
        profiles: Mapping[EndpointId, SglangServingConfig],
        *,
        contexts: tuple[tuple[CompiledStudy, EvaluationEngineBinding], ...],
        ownership_id: str,
        runner: LocalSubprocessRunner | None = None,
        artifact_verifier: Callable[[SglangServingConfig], object] = (
            verify_sglang_artifacts
        ),
        probe_factory: Callable[
            [tuple[SglangServingConfig, ...]], SGLangProbeTransport
        ] = AiohttpSGLangProbeTransport,
        stream_factory: Callable[[EvaluationConfig], Transport] = AiohttpTransport,
        port_closed_probe: Callable[[int, float], None] = _assert_loopback_port_closed,
        clock: Callable[[], int] = monotonic_ns,
        readiness_timeout_ns: int = 1_000_000_000,
        startup_timeout_ns: int = 120_000_000_000,
    ) -> None:
        if set(profiles) != set(_ENDPOINTS) or not 1 <= len(contexts) <= 32:
            raise EvaluationError("SGLang lifecycle context is invalid")
        self._profiles = dict(profiles)
        self._projected = tuple(
            build_sglang_container_profile(self._profiles[endpoint], index=index)
            for index, endpoint in enumerate(_ENDPOINTS)
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
            binding.execution_mode != "DOCKER_BRIDGE" for _, binding in self._contexts
        ):
            raise EvaluationError("SGLang lifecycle requires one containerized choice")
        self._engine_choice_sha256 = choices.pop()
        origins = tuple(self._profiles[endpoint].origin for endpoint in _ENDPOINTS)
        super().__init__(
            (origins[0], origins[1]),
            ownership_id=ownership_id,
            model_snapshot_path=Path(self._profiles["endpoint-a"].model_path),
            runner=runner,
            port_closed_probe=port_closed_probe,
            clock=clock,
            readiness_timeout_ns=readiness_timeout_ns,
            startup_timeout_ns=startup_timeout_ns,
        )
        self._artifact_verifier = artifact_verifier
        self._probe_factory = probe_factory
        self._stream_factory = stream_factory
        self._clients: list[Transport | SGLangProbeTransport] = []
        self._last_reset: tuple[SGLangOwnedReset, ...] = ()
        self._artifact_task: asyncio.Task[object] | None = None
        self._runtime_readbacks_required = False

    @property
    def engine_choice_sha256(self) -> str:
        return self._engine_choice_sha256

    @property
    def last_reset(self) -> tuple[SGLangOwnedReset, ...]:
        """Only populated after both resets/readbacks and client closure succeed."""
        return self._last_reset

    def _bind_trial(self, trial: CompiledTrial) -> None:
        super()._bind_trial(trial)
        for plan, binding in self._contexts:
            if trial in plan.trials:
                checked = load_engine_binding_bytes(
                    engine_binding_bytes(binding), plan, profiles=self._profiles
                )
                validate_engine_binding_trial(checked, plan, trial)
                if engine_choice_sha256(checked) != self._engine_choice_sha256:
                    raise EvaluationError("SGLang lifecycle engine choice changed")
                return
        raise EvaluationError("SGLang lifecycle trial has no prebound engine context")

    async def _settle_artifact_task(
        self,
        *,
        deadline_ns: int,
        stop: asyncio.Event | None = None,
        propagate_failure: bool = False,
    ) -> None:
        """Wait without cancelling the thread wrapper or concealing its liveness.

        Python cannot terminate an in-flight filesystem read safely. An expired
        deadline or cancelled owner retains the read-only worker for cleanup;
        an externally cancelled wrapper can no longer prove worker completion.
        """
        task = self._artifact_task
        if task is None:
            return
        if not task.done():
            remaining = deadline_ns - self._clock()
            if remaining <= 0:
                raise EvaluationError("SGLang artifact worker cleanup is unconfirmed")
            stopped = asyncio.create_task(stop.wait()) if stop is not None else None
            try:
                waiters = {task} if stopped is None else {task, stopped}
                await asyncio.wait(
                    waiters,
                    timeout=remaining / 1_000_000_000,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if stopped is not None:
                    stopped.cancel()
                    await asyncio.gather(stopped, return_exceptions=True)
        if stop is not None:
            self._require_running(stop, deadline_ns)
        if not task.done() or task.cancelled():
            raise EvaluationError("SGLang artifact worker cleanup is unconfirmed")
        self._artifact_task = None
        if propagate_failure:
            task.result()
        else:
            # A finished failed verifier owns no runtime resources. Prepare
            # reports its failure; cleanup only confirms the worker has ended.
            with suppress(Exception):
                task.result()

    async def _verify_artifacts(self, *, stop: asyncio.Event) -> None:
        # Verify each endpoint's declared local snapshot before every launch.
        # No cached prior success can conceal artifacts changed between trials.
        deadline_ns = self._deadline()

        def verify(config: SglangServingConfig) -> object:
            # Executor saturation may delay thread entry beyond dispatch. Check
            # the captured original deadline/stop before touching artifact files.
            self._require_running(stop, deadline_ns)
            return self._artifact_verifier(config)

        for endpoint in _ENDPOINTS:
            self._require_running(stop, deadline_ns)
            if self._artifact_task is not None:
                raise EvaluationError(
                    "SGLang preparation has an unresolved artifact worker"
                )
            self._artifact_task = asyncio.create_task(
                asyncio.to_thread(verify, self._profiles[endpoint])
            )
            await self._settle_artifact_task(
                deadline_ns=deadline_ns, stop=stop, propagate_failure=True
            )
            self._require_running(stop, deadline_ns)

    async def _remove_active(self, *, deadline_ns: int) -> None:
        # Artifact failure before any create must not produce Docker/GPU edges.
        # Once removal/readbacks begin, retain the obligation until both shared
        # readbacks succeed, including retries after the last target was removed.
        if self._active:
            self._runtime_readbacks_required = True
        if not self._runtime_readbacks_required:
            return
        await super()._remove_active(deadline_ns=deadline_ns)
        self._runtime_readbacks_required = False

    async def _verify_gpu_idle(self, *, deadline_ns: int) -> None:
        # Only failures confined to artifact preflight skip runtime readbacks.
        # Entering the inherited runtime preflight retains its cleanup contract.
        self._runtime_readbacks_required = True
        await super()._verify_gpu_idle(deadline_ns=deadline_ns)

    def _expected_image_reference(self) -> str:
        return self._projected[0].image_reference

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        if type(index) is not int or index not in (0, 1):
            raise EvaluationError("local engine index is invalid")
        profile = self._projected[index]
        endpoint = _ENDPOINTS[index]
        argv: tuple[str, ...] = (
            "docker",
            "run",
            "--pull",
            "never",
            "--platform",
            profile.image_platform,
            "--detach",
            "--rm",
            "--name",
            self._engine_name(trial, endpoint),
            "--label",
            f"io.inferdrome.load-calibration.owner={self._ownership_id}",
            "--label",
            "io.inferdrome.load-calibration.attempt="
            f"{self._attempt_label(trial, endpoint)}",
            "--gpus",
            f"device={index}",
            "--network",
            "bridge",
            "--publish",
            profile.publish,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs",
            "/root:rw,nosuid,nodev,size=1g",
        )
        for mount in profile.mounts:
            argv += ("--mount", mount)
        for name, value in profile.environment:
            argv += ("--env", f"{name}={value}")
        return (
            *argv,
            "--entrypoint",
            profile.argv[0],
            profile.image_reference,
            *profile.argv[1:],
        )

    def _require_running(self, stop: asyncio.Event, deadline_ns: int) -> None:
        if stop.is_set():
            raise EvaluationError("SGLang lifecycle preparation was cancelled")
        self._remaining_timeout_ns(deadline_ns)

    async def _readiness_attempts(
        self, probe: SGLangProbeTransport, stop: asyncio.Event, deadline_ns: int
    ) -> tuple[SGLangReadinessAcquisition, ...]:
        last_error: Exception | None = None
        delay = 0.05
        while self._clock() < deadline_ns:
            self._require_running(stop, deadline_ns)
            try:
                ready = []
                for projected in self._projected:
                    self._require_running(stop, deadline_ns)
                    ready.append(
                        await acquire_sglang_readiness(
                            projected.readiness_config,
                            probe,
                            deadline_ns=min(
                                deadline_ns, self._clock() + self._readiness_timeout_ns
                            ),
                            now_ns=self._clock,
                        )
                    )
                return tuple(ready)
            except (EvaluationError, OSError) as error:
                last_error = error
            remaining = deadline_ns - self._clock()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining / 1_000_000_000))
            delay = min(1.0, delay * 2)
        raise EvaluationError("SGLang readiness deadline expired") from last_error

    async def _warmup(
        self,
        trial: CompiledTrial,
        transport: Transport,
        stop: asyncio.Event,
        deadline_ns: int,
    ) -> None:
        body = canonical_json_bytes(
            {
                "model": trial.config.foreground.model,
                "messages": [{"role": "user", "content": "Inferdrome local warmup."}],
                "max_tokens": 1,
                "temperature": 0,
                "n": 1,
                "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
        for projected in self._projected:
            self._require_running(stop, deadline_ns)
            parser = StreamParser(
                max_stream_bytes=65_536, max_event_bytes=16_384, max_content_events=64
            )
            status: int | None = None

            def headers(value: int) -> None:
                nonlocal status
                status = value

            def content(data: bytes, parser: StreamParser = parser) -> None:
                parser.feed(data, self._clock())

            timeout_ns = min(
                self._readiness_timeout_ns,
                5_000_000_000,
                self._remaining_timeout_ns(deadline_ns),
            )
            async with asyncio.timeout(timeout_ns / 1_000_000_000):
                await transport.stream(
                    projected.readiness_config.origin, body, headers, content
                )
            self._require_running(stop, deadline_ns)
            if status != 200:
                raise EvaluationError("SGLang native warmup did not return HTTP 200")
            parser.finish()

    async def _close_clients(
        self, *, deadline_ns: int, selected: Transport | None = None
    ) -> None:
        errors: list[Exception] = []
        interrupted = False
        for client in tuple(self._clients):
            if selected is not None and client is not selected:
                continue
            try:
                remaining = self._remaining_timeout_ns(deadline_ns)
                async with asyncio.timeout(remaining / 1_000_000_000):
                    await client.close()
                self._remaining_timeout_ns(deadline_ns)
                self._clients.remove(client)
            except asyncio.CancelledError:
                interrupted = True
            except Exception as error:
                errors.append(error)
        if interrupted:
            raise asyncio.CancelledError
        if errors:
            raise EvaluationError(
                "SGLang preparation client closure is unconfirmed"
            ) from errors[0]

    async def _await_ready_and_warm(
        self, trial: CompiledTrial, stop: asyncio.Event
    ) -> None:
        deadline_ns = min(self._deadline(), self._clock() + self._startup_timeout_ns)
        self._last_reset = ()
        probe = self._probe_factory(tuple(p.readiness_config for p in self._projected))
        self._clients.append(probe)
        resets: list[SGLangOwnedReset] = []
        try:
            readiness = await self._readiness_attempts(probe, stop, deadline_ns)
            transport = self._stream_factory(trial.config.foreground)
            self._clients.append(transport)
            await self._warmup(trial, transport, stop, deadline_ns)
            # Each stream has reached successful EOF and its owned connection
            # pool is closed. No measured requests are dispatched during prepare.
            await self._close_clients(deadline_ns=deadline_ns, selected=transport)
            for endpoint, projected, ready in zip(
                _ENDPOINTS, self._projected, readiness, strict=True
            ):
                self._require_running(stop, deadline_ns)
                reset = await reset_sglang_cache(
                    projected.readiness_config,
                    probe,
                    readiness=ready,
                    locally_owned_requests_drained=True,
                    max_age_ns=self._readiness_timeout_ns,
                    deadline_ns=deadline_ns,
                    now_ns=self._clock,
                )
                resets.append(
                    SGLangOwnedReset(
                        endpoint,
                        projected.source_profile_sha256,
                        projected.launch_sha256,
                        projected.readiness_config_sha256,
                        reset,
                    )
                )
        finally:
            await self._close_clients(deadline_ns=deadline_ns)
        self._last_reset = tuple(resets)

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        self._last_reset = ()
        if self._clients or self._artifact_task is not None:
            raise EvaluationError("SGLang preparation has unresolved owned work")
        original = self._operation_deadline_ns
        if original is None:
            self.set_operation_deadline(self._deadline())
        try:
            await super().prepare(trial, stop=stop)
        finally:
            self._operation_deadline_ns = original

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        # Retry only owned-client closure, never an uncertain cache mutation.
        self._bind_trial(trial)
        deadline_ns = self._deadline()
        try:
            try:
                await self._settle_artifact_task(deadline_ns=deadline_ns)
            finally:
                await self._close_clients(deadline_ns=deadline_ns)
        finally:
            await self._remove_active(deadline_ns=deadline_ns)
