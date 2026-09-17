"""A CPU-only rehearsal proving protocol-to-native-study trial mapping."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.load_calibration import load_calibration_protocol_bytes
from inferdrome.evaluation.load_calibration_rehearsal import (
    CandidateStudyRecipe,
    CompiledRehearsal,
    LoopbackTwoEndpointReadinessLifecycle,
    SubprocessResult,
    TrialLifecycle,
    TwoEngineVllmSubprocessLifecycle,
    compile_rehearsal,
    recover_pinned_confirmation_catalog,
    run_rehearsal,
    write_pinned_confirmation_catalog,
)
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.study import execute_trial, plan_bytes
from inferdrome.evaluation.study_config import CompiledTrial, StudyConfig, compile_study
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE
from tests.integration import test_evaluation_routing_loopback as routing_loopback
from tests.integration.test_evaluation_routing_loopback import _replicas
from tests.integration.test_evaluation_study_loopback import _payload
from tests.unit.test_evaluation_study_config import load


def _rotation(index: int) -> list[str]:
    return list(POLICY_IDS[index:] + POLICY_IDS[:index])


def _offers(template: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    return [
        {
            **deepcopy(template[0]),
            "scheduled_ns": (150 + index * 40) * 1_000_000,
        }
        for index in range(count)
    ]


def _recipe_configs(
    origins: tuple[str, str], *, level_id: str, offered_count: int, seed: int
) -> tuple[StudyConfig, StudyConfig]:
    value = _payload(origins)
    healthy_template, stale_template = value["blocks"]
    for block in (healthy_template, stale_template):
        for population_name in ("foreground", "background"):
            population = block.get(population_name)
            if population is not None:
                population.update(
                    source_commit="1" * 40,
                    model="Qwen/Qwen3-8B",
                    max_tokens=4,
                )
        block["foreground"]["offers"] = _offers(
            block["foreground"]["offers"], offered_count
        )
        block["workload_seed"] = seed
        block["order_seed"] = seed + 1

    calibration = deepcopy(value)
    calibration["blocks"] = []
    for repeat_index in range(2):
        block = deepcopy(healthy_template)
        block.update(
            block_id=f"{level_id}-cal-{repeat_index}",
            repeat_index=repeat_index,
            policy_order=_rotation(repeat_index),
        )
        calibration["blocks"].append(block)

    confirmation = deepcopy(value)
    healthy = deepcopy(healthy_template)
    healthy.update(
        block_id=f"{level_id}-confirm-healthy",
        repeat_index=0,
        policy_order=_rotation(2),
    )
    stale = deepcopy(stale_template)
    stale.update(
        block_id=f"{level_id}-confirm-stale",
        repeat_index=0,
        policy_order=_rotation(0),
    )
    confirmation["blocks"] = [healthy, stale]
    return load(calibration), load(confirmation)


def _protocol(configs: tuple[tuple[str, StudyConfig, StudyConfig], ...]) -> bytes:
    levels: list[dict[str, object]] = []
    for level_id, calibration, _ in configs:
        plan = compile_study(calibration)
        foreground_count = len(plan.trials[0].config.foreground.offers)
        window = plan.trials[0].window_end_ns - plan.trials[0].window_start_ns
        levels.append(
            {
                "level_id": level_id,
                "study_config_sha256": plan.config_sha256,
                "study_plan_sha256": sha256_digest(plan_bytes(plan)),
                "offered_rate_millirps": foreground_count * 1_000_000_000_000 // window,
                "planned_requests": foreground_count,
                "worst_case_duration_ns": plan.trials[0].worst_case_duration_ns,
            }
        )
    return json.dumps(
        {
            "schema_version": "inferdrome.evaluation-load-calibration-protocol.v1",
            "protocol_id": "cpu-loopback-sweep",
            "source_commit": "1" * 40,
            "workload": {
                "model_revision_sha256": "sha256:" + "a" * 64,
                "workload_sha256": "sha256:" + "b" * 64,
                "trace_sha256": "sha256:" + "c" * 64,
                "token_lengths": {"prompt_tokens": 8, "completion_tokens": 4},
            },
            "levels": levels,
            "policy_order": list(POLICY_IDS),
            "calibration_repetitions": 2,
            "confirmation_repetitions": 1,
            "first_content_slo_ns": 100_000_000,
            "completion_slo_ns": 150_000_000,
            # This is the controller's reset/readiness allowance, distinct from
            # the frozen telemetry-freshness threshold exercised by the study.
            "preparation": {"warmup_reset_max_duration_ns": 500_000_000},
            "selection_rule": {},
            "per_trial_output_bytes": 2 * 1024 * 1024,
            "max_session_duration_ns": 86_400_000_000_000,
            "max_session_output_bytes": 128 * 1024 * 1024,
        }
    ).encode()


class _LocalLifecycle(LoopbackTwoEndpointReadinessLifecycle):
    def __init__(
        self, origins: tuple[str, str], *, binding_path: Path | None = None
    ) -> None:
        super().__init__(origins)
        self.events: list[tuple[str, str]] = []
        self.binding_path = binding_path
        self.binding_digests: list[str] = []

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        assert not stop.is_set()
        if self.binding_path is not None:
            binding = self.binding_path.read_bytes()
            assert b'"confirmation"' in binding
            assert b"127.0.0.1" not in binding
            self.binding_digests.append(sha256_digest(binding))
        assert (
            len({endpoint.origin for endpoint in trial.config.foreground.endpoints})
            == 2
        )
        await super().prepare(trial, stop=stop)
        self.events.append(("prepare", trial.trial_id))

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        await super().cleanup(trial, stop=stop)
        self.events.append(("cleanup", trial.trial_id))


class _FakeLocalEngineRunner:
    """CPU-only command seam: it records commands and never launches Docker."""

    def __init__(
        self,
        *,
        fail_second_start: bool = False,
        lost_response_start: int | None = None,
        delayed_materialization_start: int | None = None,
        fail_rm_once: bool = False,
        slow_rm: bool = False,
        gpu_busy_indices: set[int] | None = None,
        gpu_error_indices: set[int] | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.by_name: dict[str, str] = {}
        self.containers: dict[str, dict[str, object]] = {}
        self.delayed_materializations: dict[str, tuple[str, dict[str, str]]] = {}
        self.fail_second_start = fail_second_start
        self.lost_response_start = lost_response_start
        self.delayed_materialization_start = delayed_materialization_start
        self.fail_rm_once = fail_rm_once
        self.slow_rm = slow_rm
        self.gpu_busy_indices = gpu_busy_indices or set()
        self.gpu_error_indices = gpu_error_indices or set()
        self.start_count = 0
        self.remove_count = 0

    @property
    def active(self) -> set[str]:
        """Compatibility view of names still owned by this command seam."""

        return set(self.by_name)

    @staticmethod
    def _container_id(index: int) -> str:
        return f"{index:064x}"

    @staticmethod
    def _labels(argv: tuple[str, ...]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for index, value in enumerate(argv[:-1]):
            if value == "--label":
                key, label_value = argv[index + 1].split("=", 1)
                labels[key] = label_value
        return labels

    def replace_name(self, name: str) -> str:
        """Simulate a different local container reusing a once-owned name."""

        old_id = self.by_name.pop(name)
        self.containers.pop(old_id)
        replacement_id = self._container_id(self.start_count + 100)
        self.containers[replacement_id] = {
            "name": name,
            "labels": {
                "io.inferdrome.load-calibration.owner": "different-owner",
                "io.inferdrome.load-calibration.attempt": "different-attempt",
            },
        }
        self.by_name[name] = replacement_id
        return replacement_id

    def materialize_delayed(self) -> str:
        """Materialize exactly one create after its caller lost the response."""

        assert len(self.delayed_materializations) == 1
        name, (container_id, labels) = self.delayed_materializations.popitem()
        self.by_name[name] = container_id
        self.containers[container_id] = {"name": name, "labels": labels}
        return container_id

    async def run(
        self, argv: tuple[str, ...], *, timeout_ns: int
    ) -> SubprocessResult:
        assert timeout_ns > 0
        self.commands.append(argv)
        if argv[0] == "nvidia-smi":
            index = int(argv[1].removeprefix("--id="))
            return SubprocessResult(
                argv=argv,
                returncode=17 if index in self.gpu_error_indices else 0,
                stdout=b"1234\n" if index in self.gpu_busy_indices else b"",
                stderr=b"",
            )
        if argv[:2] == ("docker", "run"):
            self.start_count += 1
            name = argv[argv.index("--name") + 1]
            if self.fail_second_start and self.start_count == 2:
                return SubprocessResult(
                    argv=argv, returncode=17, stdout=b"", stderr=b"failed"
                )
            container_id = self._container_id(self.start_count)
            labels = self._labels(argv)
            if self.delayed_materialization_start == self.start_count:
                self.delayed_materializations[name] = (container_id, labels)
                raise OSError("simulated delayed create response")
            self.by_name[name] = container_id
            self.containers[container_id] = {
                "name": name,
                "labels": labels,
            }
            if self.lost_response_start == self.start_count:
                raise OSError("simulated lost create response")
            return SubprocessResult(
                argv=argv,
                returncode=0,
                stdout=container_id.encode() + b"\n",
                stderr=b"",
            )
        if argv[:3] == ("docker", "container", "inspect"):
            name = argv[-1]
            inspected_container_id = self.by_name.get(name)
            if inspected_container_id is None:
                return SubprocessResult(
                    argv=argv,
                    returncode=1,
                    stdout=b"",
                    stderr=f"Error: No such container: {name}\n".encode(),
                )
            inspected_container = self.containers[inspected_container_id]
            return SubprocessResult(
                argv=argv,
                returncode=0,
                stdout=(
                    json.dumps(
                        {
                            "Config": {
                                "Image": VLLM_RUNTIME_IMAGE_REFERENCE,
                                "Labels": inspected_container["labels"],
                            },
                            "Id": inspected_container_id,
                            "Name": "/" + name,
                        }
                    ).encode()
                ),
                stderr=b"",
            )
        if argv[:3] == ("docker", "rm", "--force"):
            self.remove_count += 1
            if self.slow_rm:
                await asyncio.sleep(0.05)
            if self.fail_rm_once and self.remove_count == 1:
                return SubprocessResult(
                    argv=argv, returncode=17, stdout=b"", stderr=b"failed"
                )
            removed_container_id = argv[3]
            removed_container = self.containers.pop(removed_container_id, None)
            if removed_container is None:
                return SubprocessResult(
                    argv=argv, returncode=1, stdout=b"", stderr=b"missing"
                )
            removed_name = removed_container["name"]
            assert isinstance(removed_name, str)
            if self.by_name.get(removed_name) == removed_container_id:
                self.by_name.pop(removed_name)
            return SubprocessResult(
                argv=argv,
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
        raise AssertionError(f"unexpected local engine command: {argv!r}")


def test_two_engine_subprocess_lifecycle_renders_exact_pinned_reset_commands(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                calibration, _ = _recipe_configs(
                    origins, level_id="load-low", offered_count=7, seed=11
                )
                plan = compile_study(calibration)
                trial, next_trial = plan.trials[:2]
                runner = _FakeLocalEngineRunner()
                warmups: list[tuple[str, float]] = []
                lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=runner,
                    warmup_probe=lambda origin, timeout: warmups.append(
                        (origin, timeout)
                    ),
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                await lifecycle.prepare(trial, stop=asyncio.Event())
                assert len(runner.active) == 2
                await lifecycle.cleanup(trial, stop=asyncio.Event())
                await lifecycle.prepare(next_trial, stop=asyncio.Event())
                await lifecycle.cleanup(next_trial, stop=asyncio.Event())
                starts = [
                    command
                    for command in runner.commands
                    if command[:2] == ("docker", "run")
                ]
                assert len(starts) == 4
                for index, command in enumerate(starts):
                    assert command[command.index("--pull") + 1] == "never"
                    endpoint_index = index % 2
                    assert command[command.index("--gpus") + 1] == (
                        f"device={endpoint_index}"
                    )
                    assert command[command.index("--publish") + 1] == (
                        f"127.0.0.1:{origins[endpoint_index].rsplit(':', 1)[1]}:8000"
                    )
                    assert command[command.index("--entrypoint") + 1] == "vllm"
                    image_index = command.index(VLLM_RUNTIME_IMAGE_REFERENCE)
                    assert command[image_index + 1 : image_index + 3] == (
                        "serve",
                        "/model",
                    )
                    assert command.count("vllm") == 1
                    assert "HF_HUB_OFFLINE=1" in command
                    assert "TRANSFORMERS_OFFLINE=1" in command
                assert runner.active == set()
                assert [origin for origin, _ in warmups] == [
                    *origins,
                    *origins,
                ]
                removals = [
                    command
                    for command in runner.commands
                    if command[:3] == ("docker", "rm", "--force")
                ]
                assert len(removals) == 4
                assert {command[3] for command in removals} == {
                    f"{index:064x}" for index in range(1, 5)
                }
                assert len({start[start.index("--name") + 1] for start in starts}) == 4
                gpu_checks = [
                    command for command in runner.commands if command[0] == "nvidia-smi"
                ]
                assert [command[1] for command in gpu_checks] == [
                    "--id=0",
                    "--id=1",
                    "--id=0",
                    "--id=1",
                    "--id=0",
                    "--id=1",
                    "--id=0",
                    "--id=1",
                ]

    asyncio.run(exercise())


def test_two_engine_subprocess_lifecycle_fails_closed_without_dropping_unknown_start(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                trial = compile_study(
                    _recipe_configs(
                        origins, level_id="load-low", offered_count=7, seed=11
                    )[0]
                ).trials[0]
                failed = _FakeLocalEngineRunner(fail_second_start=True)
                lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=failed,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                with pytest.raises(
                    EvaluationError, match="startup failed and cleanup is unconfirmed"
                ):
                    await lifecycle.prepare(trial, stop=asyncio.Event())
                assert failed.active == set()
                assert len(lifecycle._active) == 1
                removals = [
                    command
                    for command in failed.commands
                    if command[:3] == ("docker", "rm", "--force")
                ]
                assert len(removals) == 1
                assert removals[0][3] == f"{1:064x}"
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await lifecycle.cleanup(trial, stop=asyncio.Event())
                assert len(lifecycle._active) == 1

                busy = _FakeLocalEngineRunner(gpu_busy_indices={0})
                busy_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=busy,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                with pytest.raises(EvaluationError, match="GPU-idle readback"):
                    await busy_lifecycle.prepare(trial, stop=asyncio.Event())
                assert [
                    command[1]
                    for command in busy.commands
                    if command[0] == "nvidia-smi"
                ] == ["--id=0", "--id=1"]
                assert not any(
                    command[:2] == ("docker", "run") for command in busy.commands
                )

                unverified = _FakeLocalEngineRunner()
                unverified_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=unverified,
                )
                with pytest.raises(
                    EvaluationError, match="preloaded model snapshot is unavailable"
                ):
                    await unverified_lifecycle.prepare(trial, stop=asyncio.Event())
                assert not any(
                    command[:2] == ("docker", "run")
                    for command in unverified.commands
                )

    asyncio.run(exercise())


def test_two_engine_lifecycle_reconciles_only_an_exact_lost_create_attempt(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                trial = compile_study(
                    _recipe_configs(
                        origins, level_id="load-low", offered_count=7, seed=11
                    )[0]
                ).trials[0]
                runner = _FakeLocalEngineRunner(lost_response_start=1)
                lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                with pytest.raises(EvaluationError, match="startup or warmup failed"):
                    await lifecycle.prepare(trial, stop=asyncio.Event())
                assert runner.active == set()
                assert runner.containers == {}
                inspections = [
                    command
                    for command in runner.commands
                    if command[:3] == ("docker", "container", "inspect")
                ]
                removals = [
                    command
                    for command in runner.commands
                    if command[:3] == ("docker", "rm", "--force")
                ]
                assert len(inspections) == len(removals) == 1
                assert removals[0][3] == f"{1:064x}"

                delayed = _FakeLocalEngineRunner(delayed_materialization_start=1)
                delayed_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=delayed,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                with pytest.raises(
                    EvaluationError, match="startup failed and cleanup is unconfirmed"
                ):
                    await delayed_lifecycle.prepare(trial, stop=asyncio.Event())
                assert len(delayed_lifecycle._active) == 1
                assert delayed.containers == {}
                assert not any(
                    command[:3] == ("docker", "rm", "--force")
                    for command in delayed.commands
                )
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await delayed_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert len(delayed_lifecycle._active) == 1
                assert delayed.containers == {}
                assert len(
                    [
                        command
                        for command in delayed.commands
                        if command[:3] == ("docker", "container", "inspect")
                    ]
                ) == 2
                delayed_id = delayed.materialize_delayed()
                await delayed_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert delayed.containers == {}
                delayed_removals = [
                    command
                    for command in delayed.commands
                    if command[:3] == ("docker", "rm", "--force")
                ]
                assert [command[3] for command in delayed_removals] == [delayed_id]
                assert len(
                    [
                        command
                        for command in delayed.commands
                        if command[:3] == ("docker", "container", "inspect")
                    ]
                ) == 3

    asyncio.run(exercise())


def test_two_engine_lifecycle_retains_unresolved_ids_and_never_deletes_by_name(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                trial = compile_study(
                    _recipe_configs(
                        origins, level_id="load-low", offered_count=7, seed=11
                    )[0]
                ).trials[0]
                retry_runner = _FakeLocalEngineRunner(fail_rm_once=True)
                retry_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=retry_runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                async def no_warmup(
                    _trial: CompiledTrial, _stop: asyncio.Event
                ) -> None:
                    return None

                patch.setattr(retry_lifecycle, "_await_ready_and_warm", no_warmup)
                await retry_lifecycle.prepare(trial, stop=asyncio.Event())
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await retry_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert len(retry_runner.containers) == 1
                await retry_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert retry_runner.containers == {}

                replacement_runner = _FakeLocalEngineRunner()
                replacement_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=replacement_runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                patch.setattr(
                    replacement_lifecycle, "_await_ready_and_warm", no_warmup
                )
                await replacement_lifecycle.prepare(trial, stop=asyncio.Event())
                endpoint_b_name = next(
                    command[command.index("--name") + 1]
                    for command in replacement_runner.commands
                    if command[:2] == ("docker", "run")
                    and command[command.index("--name") + 1].endswith("endpoint-b")
                )
                replacement_id = replacement_runner.replace_name(endpoint_b_name)
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await replacement_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert replacement_id in replacement_runner.containers
                assert all(
                    command[3] != endpoint_b_name
                    for command in replacement_runner.commands
                    if command[:3] == ("docker", "rm", "--force")
                )

    asyncio.run(exercise())


def test_two_engine_lifecycle_cleanup_reads_every_port_and_gpu_before_confirming(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                trial = compile_study(
                    _recipe_configs(
                        origins, level_id="load-low", offered_count=7, seed=11
                    )[0]
                ).trials[0]
                ports: list[int] = []
                lingering = [True]

                def read_port(port: int, _timeout: float) -> None:
                    ports.append(port)
                    if lingering[0] and port == int(origins[0].rsplit(":", 1)[1]):
                        raise EvaluationError("simulated lingering listener")

                runner = _FakeLocalEngineRunner()
                lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=read_port,
                )
                async def no_warmup(
                    _trial: CompiledTrial, _stop: asyncio.Event
                ) -> None:
                    return None

                patch.setattr(lifecycle, "_await_ready_and_warm", no_warmup)
                await lifecycle.prepare(trial, stop=asyncio.Event())
                runner.gpu_busy_indices.add(0)
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await lifecycle.cleanup(trial, stop=asyncio.Event())
                assert ports == [
                    int(origin.rsplit(":", 1)[1]) for origin in origins
                ]
                gpu_checks = [
                    command[1]
                    for command in runner.commands
                    if command[0] == "nvidia-smi"
                ]
                assert gpu_checks[-2:] == ["--id=0", "--id=1"]
                runner.gpu_busy_indices.clear()
                lingering[0] = False
                ports.clear()
                await lifecycle.cleanup(trial, stop=asyncio.Event())
                assert ports == [
                    int(origin.rsplit(":", 1)[1]) for origin in origins
                ]

    asyncio.run(exercise())


def test_two_engine_lifecycle_preserves_unresolved_targets_on_cancellation_or_deadline(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, _replicas_unused):
                trial = compile_study(
                    _recipe_configs(
                        origins, level_id="load-low", offered_count=7, seed=11
                    )[0]
                ).trials[0]
                slow_runner = _FakeLocalEngineRunner(slow_rm=True)
                slow_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=slow_runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                )
                async def no_warmup(
                    _trial: CompiledTrial, _stop: asyncio.Event
                ) -> None:
                    return None

                patch.setattr(slow_lifecycle, "_await_ready_and_warm", no_warmup)
                await slow_lifecycle.prepare(trial, stop=asyncio.Event())
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        slow_lifecycle.cleanup(trial, stop=asyncio.Event()),
                        timeout=0.01,
                    )
                assert len(slow_runner.containers) == 2
                slow_runner.slow_rm = False
                await slow_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert slow_runner.containers == {}

                now = [0]

                class DeadlineRunner(_FakeLocalEngineRunner):
                    async def run(
                        self, argv: tuple[str, ...], *, timeout_ns: int
                    ) -> SubprocessResult:
                        result = await super().run(argv, timeout_ns=timeout_ns)
                        if argv[:3] == ("docker", "rm", "--force"):
                            now[0] += 2
                        return result

                deadline_runner = DeadlineRunner()
                deadline_lifecycle = TwoEngineVllmSubprocessLifecycle(
                    origins,
                    ownership_id="local-rehearsal",
                    model_snapshot_path=snapshot,
                    runner=deadline_runner,
                    snapshot_verifier=lambda _path: None,
                    port_closed_probe=lambda _port, _timeout: None,
                    clock=lambda: now[0],
                )
                patch.setattr(
                    deadline_lifecycle, "_await_ready_and_warm", no_warmup
                )
                await deadline_lifecycle.prepare(trial, stop=asyncio.Event())
                before = len(deadline_runner.commands)
                deadline_lifecycle.set_operation_deadline(1)
                with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
                    await deadline_lifecycle.cleanup(trial, stop=asyncio.Event())
                new_commands = deadline_runner.commands[before:]
                removals = [
                    command
                    for command in new_commands
                    if command[:3] == ("docker", "rm", "--force")
                ]
                assert removals == [new_commands[0]]
                assert len(deadline_runner.containers) == 1
                deadline_lifecycle.set_operation_deadline(now[0] + 1_000_000_000)
                await deadline_lifecycle.cleanup(trial, stop=asyncio.Event())
                assert deadline_runner.containers == {}

    asyncio.run(exercise())


def test_two_engine_subprocess_lifecycle_rejects_an_ambiguous_mount_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvaluationError, match="local engine lifecycle is invalid"):
        TwoEngineVllmSubprocessLifecycle(
            ("http://127.0.0.1:18001", "http://127.0.0.1:18002"),
            ownership_id="local-rehearsal",
            model_snapshot_path=tmp_path / "snapshot,other-field=unexpected",
            runner=_FakeLocalEngineRunner(),
        )


def test_two_engine_lifecycle_requires_its_full_protocol_reserve_before_dispatch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        low_calibration, low_confirmation = _recipe_configs(
            origins, level_id="load-low", offered_count=7, seed=11
        )
        high_calibration, high_confirmation = _recipe_configs(
            origins, level_id="load-high", offered_count=14, seed=29
        )
        protocol = load_calibration_protocol_bytes(
            _protocol(
                (
                    ("load-low", low_calibration, low_confirmation),
                    ("load-high", high_calibration, high_confirmation),
                )
            )
        )
        rehearsal = compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe(
                    "load-high", high_calibration, high_confirmation
                ),
            ),
        )
        snapshot = tmp_path / "qwen3-snapshot"
        snapshot.mkdir()
        runner = _FakeLocalEngineRunner()
        lifecycle = TwoEngineVllmSubprocessLifecycle(
            origins,
            ownership_id="local-rehearsal",
            model_snapshot_path=snapshot,
            runner=runner,
            snapshot_verifier=lambda _path: None,
        )
        output = tmp_path / "reserve-rejection"
        output.mkdir()

        async def unexpected_executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> object:
            del trial, stop
            raise AssertionError("reserve rejection must precede dispatch")

        with pytest.raises(
            EvaluationError,
            match="protocol lifecycle reserve cannot run the supplied lifecycle",
        ):
            await run_rehearsal(
                rehearsal,
                output,
                lifecycle=lifecycle,
                executor=unexpected_executor,  # type: ignore[arg-type]
            )
        assert runner.commands == []
        assert not (output / "candidate-recipe-bindings.json").exists()

    asyncio.run(exercise())


def test_cpu_loopback_rehearsal_binds_all_trials_and_uses_native_artifacts(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
            async with _replicas() as (origins, replicas):
                low_calibration, low_confirmation = _recipe_configs(
                    origins, level_id="load-low", offered_count=7, seed=11
                )
                high_calibration, high_confirmation = _recipe_configs(
                    origins, level_id="load-high", offered_count=14, seed=29
                )
                protocol = load_calibration_protocol_bytes(
                    _protocol(
                        (
                            ("load-low", low_calibration, low_confirmation),
                            ("load-high", high_calibration, high_confirmation),
                        )
                    )
                )
                rehearsal = compile_rehearsal(
                    protocol,
                    (
                        CandidateStudyRecipe(
                            "load-low", low_calibration, low_confirmation
                        ),
                        CandidateStudyRecipe(
                            "load-high", high_calibration, high_confirmation
                        ),
                    ),
                )
                assert all(
                    binding.source_trial.trial_id.startswith("trial-")
                    for candidate in rehearsal.candidates
                    for binding in (*candidate.calibration, *candidate.confirmation)
                )
                assert [
                    binding.source_trial.scenario
                    for binding in rehearsal.candidates[0].calibration
                ] == ["HEALTHY"] * 8
                assert [
                    binding.source_trial.scenario
                    for binding in rehearsal.candidates[0].confirmation
                ] == ["HEALTHY"] * 4 + ["STALE_LOAD"] * 4

                output = tmp_path / "rehearsal"
                output.mkdir(mode=0o700)
                lifecycle = _LocalLifecycle(
                    origins,
                    binding_path=output / "candidate-recipe-bindings.json"
                )
                result = await asyncio.wait_for(
                    run_rehearsal(
                        rehearsal,
                        output,
                        lifecycle=lifecycle,
                        executor=execute_trial,
                    ),
                    45,
                )
                assert result.calibration_selection.selected_level_id == "load-high"
                assert result.confirmation.status == "READY"
                assert result.confirmation_manifest is not None
                assert result.confirmation_manifest.status == "COMPLETED"
                assert len(lifecycle.events) == 48
                assert lifecycle.binding_digests == [
                    result.candidate_recipe_bindings_sha256
                ] * 24
                assert all(
                    path.stat().st_mode & 0o077 == 0 for path in output.iterdir()
                )
                linkage = (output / "calibration-linkage.json").read_bytes()
                assert b"127.0.0.1" not in linkage
                assert b'evidence_eligible":false' in linkage
                assert (
                    result.candidate_recipe_bindings_sha256.encode("ascii") in linkage
                )
                selection_binding = (
                    output / "calibration-selection-binding.json"
                ).read_bytes()
                assert (
                    result.candidate_recipe_bindings_sha256.encode("ascii")
                    in selection_binding
                )
                catalog = output / "dashboard-catalog.json"
                catalog_digest = write_pinned_confirmation_catalog(
                    result, catalog_path=catalog
                )
                recovered_catalog = output / "dashboard-catalog-recovered.json"
                assert recover_pinned_confirmation_catalog(
                    output, catalog_path=recovered_catalog
                ) == catalog_digest
                client = TestClient(
                    create_app(
                        runs_root=tmp_path / "dashboard-runs",
                        evaluation_reports_catalog=recovered_catalog,
                    )
                )
                listed = client.get("/api/v1/evaluation-reports")
                assert listed.status_code == 200
                summary = listed.json()["reports"][0]
                assert summary["report_sha256"] == catalog_digest
                assert summary["source_replay"] == "NOT_PERFORMED"
                assert (
                    client.get(
                        "/api/v1/evaluation-reports/" + summary["report_id"]
                    ).status_code
                    == 200
                )
                manifest_path = output / "confirmation-load-high" / "manifest.json"
                manifest_bytes = manifest_path.read_bytes()
                manifest_path.chmod(0o600)
                for field, drifted_value in (
                    ("trial_id", "trial-0001"),
                    ("state", "ABORTED"),
                    ("result_status", "WARMUP_FAILED"),
                    ("result_sha256", "sha256:" + "0" * 64),
                ):
                    manifest_drift = json.loads(manifest_bytes)
                    manifest_drift["trials"][0][field] = drifted_value
                    manifest_path.write_bytes(
                        canonical_json_bytes(manifest_drift) + b"\n"
                    )
                    ledger_catalog = output / f"manifest-{field}-drift-catalog.json"
                    with pytest.raises(
                        EvaluationError,
                        match="rehearsal recovery report is inconsistent",
                    ):
                        recover_pinned_confirmation_catalog(
                            output, catalog_path=ledger_catalog
                        )
                    assert not ledger_catalog.exists()
                    manifest_path.write_bytes(manifest_bytes)
                linkage_path = output / "calibration-linkage.json"
                linkage_path.chmod(0o600)
                selection_drift = json.loads(linkage)
                selection_drift["selection_sha256"] = "sha256:" + "0" * 64
                linkage_path.write_bytes(canonical_json_bytes(selection_drift) + b"\n")
                selection_catalog = output / "selection-drift-catalog.json"
                with pytest.raises(
                    EvaluationError, match="rehearsal recovery selection is unavailable"
                ):
                    recover_pinned_confirmation_catalog(
                        output, catalog_path=selection_catalog
                    )
                assert not selection_catalog.exists()
                linkage_path.write_bytes(linkage)

                report_path = result.confirmation_report_path
                assert report_path is not None
                report_path.chmod(0o600)
                report = json.loads(report_path.read_bytes())
                report["plan_sha256"] = "sha256:" + "0" * 64
                report_path.write_bytes(canonical_json_bytes(report) + b"\n")
                plan_drift = json.loads(linkage)
                plan_drift["confirmation_report_sha256"] = sha256_digest(
                    report_path.read_bytes()
                )
                linkage_path.write_bytes(canonical_json_bytes(plan_drift) + b"\n")
                direct_catalog = output / "plan-drift-direct-catalog.json"
                with pytest.raises(
                    EvaluationError,
                    match="rehearsal confirmation report is not complete",
                ):
                    write_pinned_confirmation_catalog(
                        result, catalog_path=direct_catalog
                    )
                assert not direct_catalog.exists()
                recovery_catalog = output / "plan-drift-recovery-catalog.json"
                with pytest.raises(
                    EvaluationError,
                    match="rehearsal confirmation report is not complete",
                ):
                    recover_pinned_confirmation_catalog(
                        output, catalog_path=recovery_catalog
                    )
                assert not recovery_catalog.exists()

                linkage_path.write_bytes(linkage + b" ")
                with pytest.raises(
                    EvaluationError, match="rehearsal recovery artifact is unavailable"
                ):
                    recover_pinned_confirmation_catalog(
                        output, catalog_path=output / "tampered-catalog.json"
                    )
                await asyncio.gather(
                    *(replica.assert_disconnected() for replica in replicas)
                )

    asyncio.run(exercise())


def test_rehearsal_rejects_a_confirmation_plan_with_the_wrong_native_order(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, _):
            low_calibration, low_confirmation = _recipe_configs(
                origins, level_id="load-low", offered_count=7, seed=11
            )
            high_calibration, high_confirmation = _recipe_configs(
                origins, level_id="load-high", offered_count=14, seed=29
            )
            protocol = load_calibration_protocol_bytes(
                _protocol(
                    (
                        ("load-low", low_calibration, low_confirmation),
                        ("load-high", high_calibration, high_confirmation),
                    )
                )
            )
            malformed_block = high_confirmation.blocks[0].model_copy(
                update={"policy_order": tuple(POLICY_IDS)}
            )
            malformed = high_confirmation.model_copy(
                update={"blocks": (malformed_block, *high_confirmation.blocks[1:])}
            )
            with pytest.raises(EvaluationError, match="study recipe trial"):
                compile_rehearsal(
                    protocol,
                    (
                        CandidateStudyRecipe(
                            "load-low", low_calibration, low_confirmation
                        ),
                        CandidateStudyRecipe("load-high", high_calibration, malformed),
                    ),
                )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("mode", "expected_executor_calls"),
    [
        ("prepare_error", 0),
        ("prepare_cancel", 0),
        ("prepare_timeout", 0),
        ("cleanup_error", 1),
    ],
)
def test_partial_prepare_or_cleanup_failure_stops_before_the_next_trial(
    tmp_path: Path, mode: str, expected_executor_calls: int
) -> None:
    class FailingLifecycle(TrialLifecycle):
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []

        async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
            self.events.append(("prepare", trial.trial_id))
            if mode == "prepare_error":
                raise RuntimeError("partial prepare failed")
            if mode == "prepare_cancel":
                raise asyncio.CancelledError
            if mode == "prepare_timeout":
                await asyncio.sleep(1)

        async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
            self.events.append(("cleanup", trial.trial_id))
            if mode == "cleanup_error":
                raise RuntimeError("cleanup uncertain")

    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        low_calibration, low_confirmation = _recipe_configs(
            origins, level_id="load-low", offered_count=7, seed=11
        )
        high_calibration, high_confirmation = _recipe_configs(
            origins, level_id="load-high", offered_count=14, seed=29
        )
        protocol = load_calibration_protocol_bytes(
            _protocol(
                (
                    ("load-low", low_calibration, low_confirmation),
                    ("load-high", high_calibration, high_confirmation),
                )
            )
        )
        rehearsal = compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
        output = tmp_path / mode
        output.mkdir(mode=0o700)
        lifecycle = FailingLifecycle()
        calls: list[str] = []

        async def unexpected_executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> object:
            calls.append(trial.trial_id)
            raise RuntimeError("no next trial after lifecycle failure")

        with pytest.raises(EvaluationError, match="incomplete calibration study"):
            await run_rehearsal(
                rehearsal,
                output,
                lifecycle=lifecycle,
                executor=unexpected_executor,  # type: ignore[arg-type]
            )
        assert len(calls) == expected_executor_calls
        assert lifecycle.events == [
            ("prepare", "trial-0000"),
            ("cleanup", "trial-0000"),
        ]

    asyncio.run(exercise())


def test_compile_rehearsal_rejects_a_changed_confirmation_arrival_recipe() -> None:
    origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
    low_calibration, low_confirmation = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    high_calibration, high_confirmation = _recipe_configs(
        origins, level_id="load-high", offered_count=14, seed=29
    )
    protocol = load_calibration_protocol_bytes(
        _protocol(
            (
                ("load-low", low_calibration, low_confirmation),
                ("load-high", high_calibration, high_confirmation),
            )
        )
    )
    changed_foreground = low_confirmation.blocks[0].foreground.model_copy(
        update={
            "offers": (
                low_confirmation.blocks[0]
                .foreground.offers[0]
                .model_copy(update={"prompt": "different declared arrival payload"}),
                *low_confirmation.blocks[0].foreground.offers[1:],
            )
        }
    )
    changed_confirmation = low_confirmation.model_copy(
        update={
            "blocks": tuple(
                block.model_copy(update={"foreground": changed_foreground})
                for block in low_confirmation.blocks
            )
        }
    )
    with pytest.raises(
        EvaluationError, match="confirmation recipe changes execution identity"
    ):
        compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, changed_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )


def test_compile_rehearsal_rejects_protocol_rate_or_source_mismatch() -> None:
    origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
    low_calibration, low_confirmation = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    high_calibration, high_confirmation = _recipe_configs(
        origins, level_id="load-high", offered_count=14, seed=29
    )
    value = json.loads(
        _protocol(
            (
                ("load-low", low_calibration, low_confirmation),
                ("load-high", high_calibration, high_confirmation),
            )
        )
    )
    value["levels"][0]["offered_rate_millirps"] = 5_000
    rate_mismatch = load_calibration_protocol_bytes(json.dumps(value).encode())
    with pytest.raises(EvaluationError, match="study recipe trial"):
        compile_rehearsal(
            rate_mismatch,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
    value["levels"][0]["offered_rate_millirps"] = 10_000
    value["source_commit"] = "2" * 40
    source_mismatch = load_calibration_protocol_bytes(json.dumps(value).encode())
    with pytest.raises(EvaluationError, match="source or requested tokens"):
        compile_rehearsal(
            source_mismatch,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )


def test_compile_rehearsal_rejects_native_cooldown_or_metadata_budget_overrun() -> None:
    origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
    low_calibration, low_confirmation = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    high_calibration, high_confirmation = _recipe_configs(
        origins, level_id="load-high", offered_count=14, seed=29
    )
    value = json.loads(
        _protocol(
            (
                ("load-low", low_calibration, low_confirmation),
                ("load-high", high_calibration, high_confirmation),
            )
        )
    )
    value["max_session_duration_ns"] = 600_000_000_000
    deadline_protocol = load_calibration_protocol_bytes(json.dumps(value).encode())
    delayed_confirmation = low_confirmation.model_copy(
        update={
            "limits": low_confirmation.limits.model_copy(
                update={"cooldown_ns": 10_000_000_000}
            )
        }
    )
    with pytest.raises(
        EvaluationError,
        match="native rehearsal duration exceeds protocol session bound",
    ):
        compile_rehearsal(
            deadline_protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, delayed_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
    value["max_session_duration_ns"] = 86_400_000_000_000
    value["max_session_output_bytes"] = 50 * 1024 * 1024
    output_protocol = load_calibration_protocol_bytes(json.dumps(value).encode())
    with pytest.raises(
        EvaluationError, match="native rehearsal output exceeds protocol session bound"
    ):
        compile_rehearsal(
            output_protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )


def test_compile_rehearsal_reserves_nonrenewable_cleanup_and_retrieval_window() -> None:
    origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
    low_calibration, low_confirmation = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    high_calibration, high_confirmation = _recipe_configs(
        origins, level_id="load-high", offered_count=14, seed=29
    )
    source = _protocol(
        (
            ("load-low", low_calibration, low_confirmation),
            ("load-high", high_calibration, high_confirmation),
        )
    )
    protocol = load_calibration_protocol_bytes(source)
    rehearsal = compile_rehearsal(
        protocol,
        (
            CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
            CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
        ),
    )
    assert rehearsal.worst_case_duration_ns == (
        rehearsal.execution_worst_case_duration_ns
        + rehearsal.final_cleanup_reserve_ns
        + rehearsal.retrieval_reserve_ns
    )
    too_short = json.loads(source)
    too_short["max_session_duration_ns"] = rehearsal.worst_case_duration_ns - 1
    with pytest.raises(
        EvaluationError,
        match="native rehearsal duration exceeds protocol session bound",
    ):
        compile_rehearsal(
            load_calibration_protocol_bytes(json.dumps(too_short).encode()),
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe(
                    "load-high", high_calibration, high_confirmation
                ),
            ),
        )


def test_candidate_bindings_are_durable_before_prepare_and_bind_background_content(
    tmp_path: Path,
) -> None:
    class InspectAndStopLifecycle(TrialLifecycle):
        def __init__(self, binding_path: Path) -> None:
            self.binding_path = binding_path
            self.digests: list[str] = []
            self.prepare_count = 0
            self.cleanup_count = 0

        async def prepare(
            self, trial: CompiledTrial, *, stop: asyncio.Event
        ) -> None:
            del trial, stop
            self.prepare_count += 1
            binding = self.binding_path.read_bytes()
            assert b'"confirmation"' in binding
            self.digests.append(sha256_digest(binding))
            raise RuntimeError("stop after first durable binding inspection")

        async def cleanup(
            self, trial: CompiledTrial, *, stop: asyncio.Event
        ) -> None:
            del trial, stop
            self.cleanup_count += 1

    async def observed_binding_digest(
        rehearsal_root: Path,
        rehearsal: CompiledRehearsal,
    ) -> str:
        assert not rehearsal_root.exists()
        rehearsal_root.mkdir(mode=0o700)
        lifecycle = InspectAndStopLifecycle(
            rehearsal_root / "candidate-recipe-bindings.json"
        )
        dispatched: list[str] = []

        async def unexpected_executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> object:
            del stop
            dispatched.append(trial.trial_id)
            raise AssertionError("prepare failure must prevent dispatch")

        with pytest.raises(EvaluationError, match="incomplete calibration study"):
            await run_rehearsal(
                rehearsal,
                rehearsal_root,
                lifecycle=lifecycle,
                executor=unexpected_executor,  # type: ignore[arg-type]
            )
        assert dispatched == []
        assert lifecycle.prepare_count == lifecycle.cleanup_count == 1
        return lifecycle.digests[0]

    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        low_calibration, low_confirmation = _recipe_configs(
            origins, level_id="load-low", offered_count=7, seed=11
        )
        high_calibration, high_confirmation = _recipe_configs(
            origins, level_id="load-high", offered_count=14, seed=29
        )
        protocol = load_calibration_protocol_bytes(
            _protocol(
                (
                    ("load-low", low_calibration, low_confirmation),
                    ("load-high", high_calibration, high_confirmation),
                )
            )
        )
        base = compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
        background_index, background_block = next(
            (index, block)
            for index, block in enumerate(low_confirmation.blocks)
            if block.background is not None
        )
        background = background_block.background
        assert background is not None
        changed_background = background.model_copy(
            update={
                "offers": (
                    background.offers[0].model_copy(
                        update={"prompt": "changed background binding payload"}
                    ),
                    *background.offers[1:],
                )
            }
        )
        changed_block = background_block.model_copy(
            update={"background": changed_background}
        )
        changed_confirmation = low_confirmation.model_copy(
            update={
                "blocks": tuple(
                    changed_block if index == background_index else block
                    for index, block in enumerate(low_confirmation.blocks)
                )
            }
        )
        changed = compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe(
                    "load-low", low_calibration, changed_confirmation
                ),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
        assert (
            base.candidates[0].confirmation_recipe_sha256
            != changed.candidates[0].confirmation_recipe_sha256
        )
        assert await observed_binding_digest(tmp_path / "base", base) != (
            await observed_binding_digest(tmp_path / "changed", changed)
        )

    asyncio.run(exercise())


def test_failed_candidate_binding_reservation_prevents_lifecycle_and_dispatch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        low_calibration, low_confirmation = _recipe_configs(
            origins, level_id="load-low", offered_count=7, seed=11
        )
        high_calibration, high_confirmation = _recipe_configs(
            origins, level_id="load-high", offered_count=14, seed=29
        )
        protocol = load_calibration_protocol_bytes(
            _protocol(
                (
                    ("load-low", low_calibration, low_confirmation),
                    ("load-high", high_calibration, high_confirmation),
                )
            )
        )
        rehearsal = compile_rehearsal(
            protocol,
            (
                CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
            ),
        )
        output = tmp_path / "reservation-failure"
        output.mkdir(mode=0o700)
        (output / "candidate-recipe-bindings.json").write_text(
            "reserved", encoding="utf-8"
        )
        lifecycle = _LocalLifecycle(origins)
        dispatched: list[str] = []

        async def unexpected_executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> object:
            del stop
            dispatched.append(trial.trial_id)
            raise AssertionError("candidate binding failure must prevent dispatch")

        with pytest.raises(FileExistsError):
            await run_rehearsal(
                rehearsal,
                output,
                lifecycle=lifecycle,
                executor=unexpected_executor,  # type: ignore[arg-type]
            )
        assert lifecycle.events == []
        assert dispatched == []

    asyncio.run(exercise())
