"""SYNTHETIC_ONLY lifecycle tests; Docker/GPU/image/model execution is forbidden."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import (
    build_sglang_engine_binding,
    engine_choice_sha256,
)
from inferdrome.evaluation.load_calibration_rehearsal import SubprocessResult
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.evaluation.sglang_container import verify_sglang_artifacts
from inferdrome.evaluation.sglang_lifecycle import TwoEngineSGLangSubprocessLifecycle
from inferdrome.evaluation.sglang_profile import SGLANG_IMAGE_REFERENCE
from inferdrome.evaluation.study_config import compile_study
from tests.integration.test_load_calibration_rehearsal import _FakeLocalEngineRunner
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_runner import DONE
from tests.unit.test_sglang_container import artifacts
from tests.unit.test_sglang_evaluation_stream import _choice
from tests.unit.test_sglang_readiness import _FLUSHED, metrics
from tests.unit.test_sglang_serving_profile import model_info


class SGLangRunner(_FakeLocalEngineRunner):
    """Reuse the repaired ownership fake, changing only its inspected image."""

    inspected_image = SGLANG_IMAGE_REFERENCE

    async def run(self, argv: tuple[str, ...], *, timeout_ns: int) -> SubprocessResult:
        result = await super().run(argv, timeout_ns=timeout_ns)
        if argv[:3] == ("docker", "container", "inspect") and result.returncode == 0:
            value = json.loads(result.stdout)
            value["Config"]["Image"] = self.inspected_image
            result = replace(result, stdout=json.dumps(value).encode())
        return result


class Probe:
    def __init__(self, configs, events: list[str], *, mode: str = "success") -> None:
        self.configs = {config.origin: config for config in configs}
        self.events = events
        self.mode = mode
        self.closed = False
        self.health_count = 0
        self.flush_count = 0
        self.flush_started = asyncio.Event()

    async def get(self, origin: str, path: str) -> ProbeResponse:
        self.events.append(path)
        config = self.configs[origin]
        if path == "/health":
            self.health_count += 1
            if self.mode == "retry" and self.health_count == 1:
                return ProbeResponse(503, b"")
        if path == "/model_info":
            return ProbeResponse(
                200,
                model_info(
                    served_model_name=config.served_model_name,
                    model_path=config.model_path,
                    tokenizer_path=config.tokenizer_path,
                    weight_version=config.model_revision,
                ),
            )
        if path == "/metrics":
            body = metrics(running=1 if self.mode == "busy" else 0)
            return ProbeResponse(
                200, body.replace(b"test/private-model", b"private-model")
            )
        assert path in ("/health", "/health_generate")
        return ProbeResponse(200, b"")

    async def post(self, origin: str, path: str) -> ProbeResponse:
        assert origin in self.configs and path == "/flush_cache"
        assert "stream-closed" in self.events
        self.events.append(path)
        self.flush_count += 1
        self.flush_started.set()
        if self.mode == "cancel-flush":
            await asyncio.Event().wait()
        return ProbeResponse(400 if self.mode == "flush-failure" else 200, _FLUSHED)

    async def close(self) -> None:
        self.events.append("probe-closed")
        self.closed = True


class Warmup:
    def __init__(self, events: list[str], *, mode: str = "success") -> None:
        self.events = events
        self.mode = mode
        self.closed = False
        self.close_count = 0
        self.origins: list[str] = []

    async def stream(self, origin, body, on_headers, on_bytes) -> None:
        self.origins.append(origin)
        self.events.append("warmup")
        request = json.loads(body)
        assert request["max_tokens"] == 1
        assert request["stream_options"] == {"include_usage": True}
        assert request["chat_template_kwargs"] == {"enable_thinking": False}
        on_headers(400 if self.mode == "http-error" else 200)
        on_bytes(_choice("synthetic warmup", finish="stop"))
        if self.mode != "partial":
            on_bytes(DONE)

    async def close(self) -> None:
        self.close_count += 1
        if self.mode == "close-failure":
            raise EvaluationError("synthetic close failed")
        if self.mode == "close-cancel":
            raise asyncio.CancelledError
        if self.mode == "close-timeout":
            await asyncio.Event().wait()
        self.events.append("stream-closed")
        self.closed = True


def harness(
    *,
    probe_mode="success",
    warmup_mode="success",
    runner=None,
    artifact_verifier=None,
    clock=time.monotonic_ns,
):
    selected = profiles()
    plans = (compile_study(study()), compile_study(study("STALE_LOAD")))
    contexts = tuple(
        (plan, build_sglang_engine_binding(plan, selected, containerized=True))
        for plan in plans
    )
    events: list[str] = []
    probes: list[Probe] = []
    streams: list[Warmup] = []
    runner = runner or SGLangRunner()

    def probe_factory(configs):
        probe = Probe(configs, events, mode=probe_mode)
        probes.append(probe)
        return probe

    def stream_factory(config):
        warmup = Warmup(events, mode=warmup_mode)
        streams.append(warmup)
        return warmup

    owner = TwoEngineSGLangSubprocessLifecycle(
        selected,
        contexts=contexts,
        ownership_id="sglang-test",
        runner=runner,
        artifact_verifier=artifact_verifier or (lambda _: events.append("artifact")),
        probe_factory=probe_factory,
        stream_factory=stream_factory,
        port_closed_probe=lambda _port, _timeout: events.append("port-closed"),
        clock=clock,
    )
    return owner, plans, contexts, runner, events, probes, streams


def test_sglang_uses_exact_shared_ownership_and_native_warmup_before_single_reset() -> (
    None
):
    async def scenario() -> None:
        owner, plans, contexts, runner, events, probes, streams = harness()
        assert owner.engine_choice_sha256 == engine_choice_sha256(contexts[0][1])
        assert owner.required_operation_timeout_ns == 240_000_000_000
        for plan in plans:
            trial = plan.trials[0]
            await owner.prepare(trial, stop=asyncio.Event())
            assert len(owner._active) == 2
            assert not owner._clients
            assert len(owner.last_reset) == 2
            for index, receipt in enumerate(owner.last_reset):
                projection = owner._projected[index]
                assert receipt.endpoint_id == ("endpoint-a", "endpoint-b")[index]
                assert receipt.source_profile_sha256 == projection.source_profile_sha256
                assert receipt.projected_launch_sha256 == projection.launch_sha256
                assert (
                    receipt.readiness_config_sha256
                    == projection.readiness_config_sha256
                )
                assert (
                    receipt.reset.readiness.config_sha256
                    == projection.readiness_config_sha256
                )
                assert receipt.reset.runtime_verification == "UNVERIFIED"
            probe, stream = probes[-1], streams[-1]
            assert probe.closed and stream.closed
            assert probe.flush_count == 2
            assert stream.origins == [p.origin for p in owner._profiles.values()]
            assert (
                events.index("warmup")
                < events.index("stream-closed")
                < events.index("/flush_cache")
            )
            after_first_reset = events[events.index("/flush_cache") + 1 :]
            assert "/health_generate" not in after_first_reset
            await owner.cleanup(trial, stop=asyncio.Event())
            assert not runner.active and not owner._active
            events.clear()
        launches = [
            command for command in runner.commands if command[:2] == ("docker", "run")
        ]
        assert len(launches) == 4
        for index, command in enumerate(launches):
            profile = owner._projected[index % 2]
            assert command[command.index("--pull") + 1] == "never"
            assert command[command.index("--platform") + 1] == "linux/amd64"
            assert command[command.index("--gpus") + 1] == f"device={index % 2}"
            assert command[command.index("--publish") + 1] == profile.publish
            assert command[command.index("--entrypoint") + 1] == "python3"
            image_index = command.index(SGLANG_IMAGE_REFERENCE)
            assert command[image_index + 1 :] == profile.argv[1:]
            assert all(
                mount in command and mount.endswith("readonly")
                for mount in profile.mounts
            )
            assert "0.0.0.0" in command and "8000" in command
            assert all(
                f"{name}={value}" in command for name, value in profile.environment
            )
        removals = [
            command[3]
            for command in runner.commands
            if command[:3] == ("docker", "rm", "--force")
        ]
        assert removals == [f"{index:064x}" for index in (2, 1, 4, 3)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["partial", "http-error", "close-failure", "close-cancel"]
)
def test_incomplete_or_unclosed_warmup_never_flushes(mode: str) -> None:
    async def scenario() -> None:
        owner, plans, _, runner, _, probes, streams = harness(warmup_mode=mode)
        expected = asyncio.CancelledError if mode == "close-cancel" else EvaluationError
        with pytest.raises(expected):
            await owner.prepare(plans[0].trials[0], stop=asyncio.Event())
        assert probes[0].flush_count == 0 and probes[0].closed
        assert not owner.last_reset
        streams[0].mode = "success"
        await owner.cleanup(plans[0].trials[0], stop=asyncio.Event())
        assert not runner.active and not owner._clients

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,flushes", [("busy", 0), ("flush-failure", 1)])
def test_reset_failures_do_not_retry_or_claim_success(mode: str, flushes: int) -> None:
    async def scenario() -> None:
        owner, plans, _, runner, _, probes, streams = harness(probe_mode=mode)
        with pytest.raises(EvaluationError, match="startup or warmup failed"):
            await owner.prepare(plans[0].trials[0], stop=asyncio.Event())
        assert probes[0].flush_count == flushes
        assert probes[0].closed and streams[0].closed
        assert not owner.last_reset and not runner.active

    asyncio.run(scenario())


def test_only_readiness_before_reset_can_retry() -> None:
    async def scenario() -> None:
        owner, plans, _, _, events, probes, streams = harness(probe_mode="retry")
        trial = plans[0].trials[0]
        await owner.prepare(trial, stop=asyncio.Event())
        assert events[:4] == ["artifact", "artifact", "/health", "/health"]
        assert len(streams) == 1 and probes[0].flush_count == 2
        await owner.cleanup(trial, stop=asyncio.Event())

    asyncio.run(scenario())


def test_uncertain_flush_cancellation_retains_owned_engines_for_cleanup() -> None:
    async def scenario() -> None:
        owner, plans, _, runner, _, probes, _ = harness(probe_mode="cancel-flush")
        trial = plans[0].trials[0]
        task = asyncio.create_task(owner.prepare(trial, stop=asyncio.Event()))
        while not probes:
            await asyncio.sleep(0)
        await probes[0].flush_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(owner._active) == 2 and probes[0].closed
        assert probes[0].flush_count == 1 and not owner.last_reset
        await owner.cleanup(trial, stop=asyncio.Event())
        assert not runner.active

    asyncio.run(scenario())


def test_delayed_create_absence_remains_pending_until_exact_image_labels_and_id() -> (
    None
):
    async def scenario() -> None:
        runner = SGLangRunner(delayed_materialization_start=1)
        owner, plans, _, _, _, probes, _ = harness(runner=runner)
        trial = plans[0].trials[0]
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await owner.prepare(trial, stop=asyncio.Event())
        assert len(owner._active) == 1 and owner._active[0].container_id is None
        assert not probes
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await owner.cleanup(trial, stop=asyncio.Event())
        exact_id = runner.materialize_delayed()
        runner.inspected_image = "unrelated/image"
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await owner.cleanup(trial, stop=asyncio.Event())
        assert exact_id in runner.containers
        assert not [c for c in runner.commands if c[:3] == ("docker", "rm", "--force")]
        runner.inspected_image = SGLANG_IMAGE_REFERENCE
        await owner.cleanup(trial, stop=asyncio.Event())
        assert not owner._active and not runner.active
        assert ("docker", "rm", "--force", exact_id) in runner.commands

    asyncio.run(scenario())


def test_artifact_or_engine_binding_failure_prevents_all_process_commands() -> None:
    def invalid_artifacts(_config) -> None:
        raise EvaluationError("synthetic artifact mismatch")

    async def scenario() -> None:
        owner, plans, _, runner, _, _, _ = harness(artifact_verifier=invalid_artifacts)
        with pytest.raises(EvaluationError, match="artifact mismatch"):
            await owner.prepare(plans[0].trials[0], stop=asyncio.Event())
        assert not runner.commands
        forged = replace(plans[0].trials[0], trial_id="trial-9999")
        with pytest.raises(EvaluationError, match="no prebound engine context"):
            await owner.prepare(forged, stop=asyncio.Event())
        assert not runner.commands

    asyncio.run(scenario())


def test_omitted_unreadable_subtree_cannot_authorize_any_lifecycle_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = artifacts(tmp_path)
    blocked = Path(source.model_path) / "unreadable-subtree"
    blocked.mkdir()
    (blocked / "omitted.safetensors").write_bytes(b"SYNTHETIC_ONLY omitted weights")
    # Declarations deliberately bind the earlier partial tree. The old os.walk
    # behavior could omit the new unreadable subtree and accept those digests.
    selected = {
        endpoint: source.model_copy(
            update={
                "served_model_name": "private-model",
                "origin": configured.origin,
            }
        )
        for endpoint, configured in profiles().items()
    }
    plan = compile_study(study())
    binding = build_sglang_engine_binding(plan, selected, containerized=True)
    runner = SGLangRunner()

    def no_clients(_config):
        raise AssertionError("artifact failure must precede HTTP client construction")

    owner = TwoEngineSGLangSubprocessLifecycle(
        selected,
        contexts=((plan, binding),),
        ownership_id="unreadable-tree",
        runner=runner,
        probe_factory=no_clients,
        stream_factory=no_clients,
        port_closed_probe=lambda _port, _timeout: None,
    )
    original = os.scandir

    def denied(path):
        if Path(path) == blocked:
            raise PermissionError(
                errno.EACCES, "synthetic unreadable subtree", str(path)
            )
        return original(path)

    async def scenario() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", denied)
            with pytest.raises(
                EvaluationError, match="local artifact verification failed"
            ):
                verify_sglang_artifacts(selected["endpoint-a"])
            with pytest.raises(
                EvaluationError, match="local artifact verification failed"
            ):
                await owner.prepare(plan.trials[0], stop=asyncio.Event())
        assert runner.commands == []
        assert not owner._active and not owner._clients and not owner.last_reset

    asyncio.run(scenario())


def test_operation_deadline_is_not_refilled_on_cleanup_or_failed_create() -> None:
    async def scenario() -> None:
        now = [1]
        owner, plans, _, runner, _, _, _ = harness(clock=lambda: now[0])
        trial = plans[0].trials[0]
        owner.set_operation_deadline(1)
        with pytest.raises(EvaluationError, match="operation deadline expired"):
            await owner.prepare(trial, stop=asyncio.Event())
        assert not runner.commands
        assert owner._operation_deadline_ns == 1
        # No worker or process was started; cleanup has no owned work to inspect.
        await owner.cleanup(trial, stop=asyncio.Event())
        assert not runner.commands

    asyncio.run(scenario())


def test_warmup_close_timeout_retains_clients_and_engines_under_original_deadline() -> (
    None
):
    async def scenario() -> None:
        owner, plans, _, runner, _, probes, streams = harness(
            warmup_mode="close-timeout"
        )
        trial = plans[0].trials[0]
        deadline = time.monotonic_ns() + 250_000_000
        owner.set_operation_deadline(deadline)
        started = time.monotonic()
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await asyncio.wait_for(owner.prepare(trial, stop=asyncio.Event()), 1)
        assert time.monotonic() - started < 1
        assert owner._operation_deadline_ns == deadline
        assert len(owner._active) == 2 and owner._clients
        assert probes[0].flush_count == 0 and not owner.last_reset
        streams[0].mode = "success"
        owner.set_operation_deadline(time.monotonic_ns() + 1_000_000_000)
        await owner.cleanup(trial, stop=asyncio.Event())
        assert not runner.active and not owner._clients

    asyncio.run(scenario())


def test_native_process_context_cannot_authorize_a_container_launch() -> None:
    selected = profiles()
    plan = compile_study(study())
    binding = build_sglang_engine_binding(plan, selected)
    with pytest.raises(EvaluationError, match="one containerized choice"):
        TwoEngineSGLangSubprocessLifecycle(
            selected, contexts=((plan, binding),), ownership_id="synthetic"
        )
