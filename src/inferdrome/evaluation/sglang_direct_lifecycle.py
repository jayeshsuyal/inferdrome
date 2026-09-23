"""SGLang 0.5.15 exact-owned direct-process lifecycle for two-GPU hosts.

This is deliberately separate from the reviewed Docker lifecycle.  It is for
an already-rented ordinary container that cannot safely run a nested daemon:
two pinned serving processes bind only literal loopback ports, one per GPU.
The owner knows only process groups it started itself.  It never rents a host,
discovers a provider, pulls an image, downloads a model, or treats the outer
container image declaration as an observed runtime attestation.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from functools import partial
from pathlib import Path
from time import monotonic_ns

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    _ENDPOINTS,
    _STARTUP_TIMEOUT_NS,
    DirectProcessRunner,
    _TwoEngineDirectProcessLifecycle,
)
from inferdrome.evaluation.engine_binding import (
    SglangDirectEngineBinding,
    engine_binding_bytes,
    engine_choice_sha256,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    LocalSubprocessRunner,
    _assert_loopback_port_closed,
    _probe_loopback_warmup,
)
from inferdrome.evaluation.sglang_container import verify_sglang_artifacts
from inferdrome.evaluation.sglang_direct_profile import build_sglang_direct_profile
from inferdrome.evaluation.sglang_direct_readiness import (
    acquire_sglang_direct_readiness,
    reset_sglang_direct_cache,
)
from inferdrome.evaluation.sglang_direct_runtime import (
    SglangDirectRuntimeManifest,
    load_runtime_manifest,
    runtime_environment,
    runtime_manifest_bytes,
    runtime_manifest_sha256,
    verify_loaded_runtime,
    verify_runtime_host,
)
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.sglang_readiness import (
    AiohttpSGLangProbeTransport,
    SGLangCacheReset,
)
from inferdrome.evaluation.stream import StreamParser
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.execution.subprocess_runner import (
    ExecutableIdentity,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes


class Sglang015DirectLifecycle(_TwoEngineDirectProcessLifecycle):
    """Pinned SGLang direct process pair using the same exact group ownership.

    Each trial receives newly started engines, SGLang readiness and one native
    SSE warmup per replica, then drain/flush and provenance readbacks. Every
    receipt remains a local observation, never GPU or cache-state attestation.
    """

    def __init__(
        self,
        profiles: Mapping[EndpointId, SglangServingConfig],
        *,
        contexts: tuple[tuple[CompiledStudy, SglangDirectEngineBinding], ...],
        runtime_manifest: SglangDirectRuntimeManifest,
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
        self.runtime_manifest = load_runtime_manifest(
            runtime_manifest_bytes(runtime_manifest)
        )
        if str(executable.path) != self.runtime_manifest.roles["python"] or any(
            type(binding) is not SglangDirectEngineBinding
            or binding.runtime_manifest_sha256
            != runtime_manifest_sha256(self.runtime_manifest)
            for _, binding in contexts
        ):
            raise EvaluationError("direct runtime differs from prebound manifest")
        self._worker: asyncio.Task[object] | None = None
        self._cache_roots: list[Path] = []
        self.resets: list[tuple[SGLangCacheReset, ...]] = []
        self.loaded_runtime_receipts: list[str] = []
        self._profiles = dict(profiles)
        self._profiles_by_index = tuple(
            build_sglang_direct_profile(self._profiles[endpoint])
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
        first_binding = self._contexts[0][1]
        if (
            type(first_binding) is not SglangDirectEngineBinding
            or runtime_manifest_sha256(self.runtime_manifest)
            != first_binding.runtime_manifest_sha256
        ):
            raise EvaluationError("direct runtime manifest changed")
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

    async def _owned_worker(self, work: Callable[[], object]) -> object:
        if self._worker is not None:
            raise EvaluationError("direct runtime worker remains unresolved")
        self._worker = asyncio.create_task(asyncio.to_thread(work))
        result = await asyncio.shield(self._worker)
        self._worker = None
        return result

    async def _verify_artifacts(self, *, stop: asyncio.Event) -> None:
        if stop.is_set() or self._cache_roots:
            raise EvaluationError("direct runtime is stopped or has unresolved caches")
        await self._owned_worker(lambda: verify_runtime_host(self.runtime_manifest))
        for endpoint in _ENDPOINTS:
            if stop.is_set():
                raise EvaluationError("direct SGLang lifecycle was cancelled")
            selected = self._profiles[endpoint]
            await self._owned_worker(partial(self._artifact_verifier, selected))
        for _ in _ENDPOINTS:
            self._cache_roots.append(
                Path(tempfile.mkdtemp(prefix="inferdrome-sglang015-"))
            )

    def _engine_environment(self, *, index: int) -> Mapping[str, str]:
        if len(self._cache_roots) != 2:
            raise EvaluationError("direct runtime cache reservation is absent")
        environment = runtime_environment(
            self.runtime_manifest, cache=self._cache_roots[index], gpu_index=index
        )
        environment.update(dict(self._profiles_by_index[index].environment))
        return environment

    async def _verify_gpu_idle(self, *, deadline_ns: int) -> None:
        result = await self._command(
            (
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,driver_version,mig.mode.current",
                "--format=csv,noheader,nounits",
            ),
            deadline_ns=deadline_ns,
        )
        rows = [
            [field.strip() for field in line.split(",")]
            for line in result.stdout.decode().strip().splitlines()
        ]
        if len(rows) != 2:
            raise EvaluationError("direct runtime requires exactly two GPUs")
        for index, fields in enumerate(rows):
            if (
                len(fields) != 6
                or fields[0] != str(index)
                or fields[1] != self.runtime_manifest.gpu_uuids[index]
                or fields[2].removeprefix("NVIDIA ") != "A100-SXM4-40GB"
                or not fields[3].isdecimal()
                or not 39000 <= int(fields[3]) <= 41000
                or fields[4] != self.runtime_manifest.driver_version
                or fields[5] != "Disabled"
            ):
                raise EvaluationError(
                    "direct runtime GPU, driver or MIG differs from its binding"
                )
        await super()._verify_gpu_idle(deadline_ns=deadline_ns)

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        uncertain = False
        if self._worker is not None:
            done, _ = await asyncio.wait({self._worker}, timeout=30)
            if not done:
                uncertain = True
            else:
                with suppress(Exception):
                    self._worker.result()
                self._worker = None
        await super().cleanup(trial, stop=stop)
        if uncertain:
            raise EvaluationError("direct runtime worker cleanup is unconfirmed")
        for root in self._cache_roots:
            shutil.rmtree(root)
        self._cache_roots.clear()

    def _engine_argv(self, trial: CompiledTrial, *, index: int) -> tuple[str, ...]:
        del trial
        if index not in (0, 1):
            raise EvaluationError("direct-process engine index is invalid")
        profile = self._profiles_by_index[index]
        # The source profile argv is an exact finite allowlist.  The executable
        # path is separately observed and revalidated at spawn time.
        return (str(self._executable.path), "-I", "-B", *profile.argv[1:])

    async def _await_ready_and_warm(
        self, trial: CompiledTrial, stop: asyncio.Event
    ) -> None:
        deadline = min(self._deadline(), self._clock() + 300 * 1_000_000_000)
        probe = AiohttpSGLangProbeTransport(tuple(self._profiles.values()))
        stream = None
        try:
            while True:
                if stop.is_set() or self._clock() >= deadline:
                    raise RuntimeError("SGLang startup deadline/stop")
                try:
                    ready = [
                        await acquire_sglang_direct_readiness(
                            p,
                            probe,
                            deadline_ns=min(
                                deadline, self._clock() + 10 * 1_000_000_000
                            ),
                            now_ns=self._clock,
                        )
                        for p in self._profiles.values()
                    ]
                    break
                except Exception:
                    if self._clock() >= deadline:
                        raise
                    await asyncio.sleep(0.5)
            stream = AiohttpTransport(trial.config.foreground)
            body = canonical_json_bytes(
                dict(
                    model=trial.config.foreground.model,
                    messages=[dict(role="user", content="Inferdrome local warmup.")],
                    max_tokens=1,
                    temperature=0,
                    n=1,
                    stream=True,
                    stream_options=dict(include_usage=True),
                    chat_template_kwargs=dict(enable_thinking=False),
                )
            )
            for p in self._profiles.values():
                parser = StreamParser(
                    max_stream_bytes=65536, max_event_bytes=16384, max_content_events=64
                )
                statuses: list[int] = []

                def feed(data: bytes, target: StreamParser = parser) -> None:
                    target.feed(data, self._clock())

                async with asyncio.timeout(
                    min(30, max(0, (deadline - self._clock()) / 1_000_000_000))
                ):
                    await stream.stream(
                        p.origin,
                        body,
                        statuses.append,
                        feed,
                    )
                if statuses != [200]:
                    raise RuntimeError("Warmup HTTP failure")
                parser.finish()
            await stream.close()
            stream = None
            resets = []
            for p, r in zip(self._profiles.values(), ready, strict=True):
                if stop.is_set():
                    raise RuntimeError("Stopped before reset")
                resets.append(
                    await reset_sglang_direct_cache(
                        p,
                        probe,
                        readiness=r,
                        locally_owned_requests_drained=True,
                        max_age_ns=10 * 1_000_000_000,
                        deadline_ns=deadline,
                        now_ns=self._clock,
                    )
                )
            self.resets.append(tuple(resets))
            await self.check_loaded_runtime()
        finally:
            if stream is not None:
                await stream.close()
            await probe.close()

    async def check_loaded_runtime(self) -> None:
        groups = tuple(lease.process_group_id for _, lease in self._active)
        if len(groups) != 2:
            raise EvaluationError("direct runtime has no exact owned pair")
        receipt = await self._owned_worker(
            lambda: verify_loaded_runtime(
                self.runtime_manifest, process_groups=(groups[0], groups[1])
            )
        )
        if type(receipt) is not str:
            raise EvaluationError("direct loaded runtime receipt is invalid")
        self.loaded_runtime_receipts.append(receipt)
