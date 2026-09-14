"""Sequential study ownership and durable ledgers using real healthy sessions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import inferdrome.evaluation.study as study_module
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.faults import close_routing_clients
from inferdrome.evaluation.healthy import HealthyRoutingResult, run_routing_healthy
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.study import (
    StudyManifest,
    execute_trial,
    load_study_trial_bytes,
    plan_bytes,
    read_manifest,
    report_study,
    run_study,
    validate_manifest,
)
from inferdrome.evaluation.study_config import CompiledTrial, StudyConfig, compile_study
from inferdrome.evaluation.study_files import StudyDirectory
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_faults import MS, finish
from tests.unit.test_evaluation_healthy import Probe, Stream, clients
from tests.unit.test_evaluation_runner import ManualClock, advance, settle
from tests.unit.test_evaluation_study_config import load, study_payload


def config(*, cooldown_ns: int = 0) -> StudyConfig:
    source = study_payload()
    source["limits"].update(max_duration_ns=2000 * MS, cooldown_ns=cooldown_ns)
    return load(source)


class LocalExecutor:
    """Return genuine measured results; no fabricated stream/route records."""

    def __init__(self, clock: ManualClock | None = None) -> None:
        self.clock = clock or ManualClock()
        self.calls: list[str] = []
        self.owned: list[tuple[Stream, Probe, Probe]] = []
        self.active = 0
        self.peak = 0
        self.mode: dict[int, str] = {}
        self.held = asyncio.Event()
        self.after: Callable[[int], None] | None = None

    async def __call__(
        self, trial: CompiledTrial, *, stop: asyncio.Event
    ) -> HealthyRoutingResult:
        index = len(self.calls)
        self.calls.append(trial.trial_id)
        assert self.active == 0
        assert all(client.closed for previous in self.owned for client in previous)
        self.active += 1
        self.peak = max(self.peak, self.active)
        mode = self.mode.get(index)
        try:
            if mode == "controller":
                raise RuntimeError("private controller failure")
            assert isinstance(trial.config, HealthyRoutingConfig)
            clock = ManualClock()
            owned = clients(clock)
            self.owned.append(owned)
            if mode == "warmup":
                owned[2].mode = "MISSING"
            if mode == "cleanup":
                owned[2].close_error = True
            if mode == "hold":
                owned[0].block = True
            task = asyncio.create_task(
                run_routing_healthy(trial.config, *owned, clock=clock, stop=stop)
            )
            await finish(clock, 65 if mode == "hold" else 220)
            if mode == "hold":
                self.held.set()
            result = await task
            assert not clock.waiters
            self.clock.advance_to(self.clock.elapsed_ns + result.elapsed_ns)
            if self.after is not None:
                self.after(index)
            if mode == "invalid_result":
                return replace(result, config_sha256="sha256:" + "0" * 64)
            return result
        finally:
            self.active -= 1


def assert_no_tasks() -> None:
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 1)


def test_trials_are_sequential_and_each_result_is_bound_to_plan(tmp_path: Path) -> None:
    async def scenario() -> None:
        parsed = config()
        plan = compile_study(parsed)
        executor = LocalExecutor()
        output = tmp_path / "study"
        result = await run_study(
            parsed, output, executor=executor, clock=executor.clock
        )
        assert result.status == "COMPLETED" and result.reason is None
        assert executor.calls == [trial.trial_id for trial in plan.trials]
        assert executor.peak == 1 and executor.active == 0
        assert len({id(client) for owned in executor.owned for client in owned}) == 12
        assert all(client.closed for owned in executor.owned for client in owned)
        assert set(path.name for path in output.iterdir()) == {
            "plan.json",
            "manifest.json",
            *(f"trial-{index:04d}.json" for index in range(4)),
        }
        assert (output / "plan.json").read_bytes() == plan_bytes(plan)
        with StudyDirectory.open(output) as directory:
            assert read_manifest(directory, plan) == result
        for index, entry in enumerate(result.trials):
            assert entry.state == "RETURNED"
            assert entry.cleanup == "CONFIRMED_BY_LOCAL_RESULT"
            content = (output / f"trial-{index:04d}.json").read_bytes()
            validated = load_study_trial_bytes(content, plan, plan.trials[index])
            assert (
                validated.result_sha256 == entry.result_sha256 == sha256_digest(content)
            )
            assert validated.status == entry.result_status == "COMPLETED"
            assert len(validated.foreground.records) == 6
            assert len(validated.decisions) == 6
        serialized = b"".join(path.read_bytes() for path in output.iterdir())
        assert b"private" not in serialized and b"127.0.0.1" not in serialized
        assert_no_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("warmup", "WARMUP_FAILED"),
        ("controller", "CONTROLLER_OR_CLEANUP_FAILED"),
        ("cleanup", "CONTROLLER_OR_CLEANUP_FAILED"),
        ("invalid_result", "RESULT_VALIDATION_FAILED"),
    ],
)
def test_failure_stops_once_preserves_prior_results_and_marks_unrun_trials(
    tmp_path: Path, mode: str, reason: str
) -> None:
    async def scenario() -> None:
        parsed = config()
        executor = LocalExecutor()
        executor.mode[1] = mode
        output = tmp_path / "study"
        result = await run_study(
            parsed, output, executor=executor, clock=executor.clock
        )
        assert result.status == "ABORTED" and result.reason == reason
        assert executor.calls == ["trial-0000", "trial-0001"]
        assert result.trials[0].state == "RETURNED"
        assert result.trials[0].result_status == "COMPLETED"
        assert (output / "trial-0000.json").exists()
        if mode == "warmup":
            assert result.trials[1].state == "RETURNED"
            assert result.trials[1].result_status == "WARMUP_FAILED"
            assert result.trials[1].cleanup == "CONFIRMED_BY_LOCAL_RESULT"
            assert (output / "trial-0001.json").exists()
        else:
            assert result.trials[1].state == "ABORTED"
            assert result.trials[1].failure_reason == reason
            assert result.trials[1].cleanup == "UNCONFIRMED"
            assert not (output / "trial-0001.json").exists()
        assert all(entry.state == "NOT_RUN" for entry in result.trials[2:])
        assert all(entry.cleanup == "NOT_STARTED" for entry in result.trials[2:])
        assert not (output / "trial-0002.json").exists()
        validate_manifest(compile_study(parsed), result)
        assert executor.active == 0
        assert all(client.active == 0 for owned in executor.owned for client in owned)
        assert_no_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("where", ["initial", "inflight", "cooldown"])
@pytest.mark.parametrize("mechanism", ["event", "task"])
def test_stop_ends_study_without_retry_or_later_trial(
    tmp_path: Path, where: str, mechanism: str
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        executor = LocalExecutor(clock)
        parsed = config(cooldown_ns=20 * MS if where == "cooldown" else 0)
        stop = asyncio.Event()
        if where == "initial":
            stop.set()
        elif where == "inflight":
            executor.mode[0] = "hold"
        task = asyncio.create_task(
            run_study(
                parsed, tmp_path / "study", executor=executor, clock=clock, stop=stop
            )
        )
        if where != "initial":
            if where == "inflight":
                await asyncio.wait_for(executor.held.wait(), 1)
            else:
                await wait_until(
                    lambda: (
                        bool(executor.owned)
                        and all(client.closed for client in executor.owned[0])
                        and (tmp_path / "study" / "trial-0000.json").exists()
                    )
                )
                await settle()
            if mechanism == "event":
                stop.set()
            else:
                task.cancel()
        result = await asyncio.wait_for(task, 1)
        assert result.status == "CANCELLED" and result.reason == "CANCELLED"
        assert len(executor.calls) == (0 if where == "initial" else 1)
        if where == "initial":
            assert all(entry.state == "NOT_RUN" for entry in result.trials)
        else:
            assert result.trials[0].state == "RETURNED"
            assert result.trials[0].result_status == (
                "COMPLETED" if where == "cooldown" else "CANCELLED"
            )
            assert all(entry.state == "NOT_RUN" for entry in result.trials[1:])
        assert all(client.closed for owned in executor.owned for client in owned)
        assert not clock.waiters
        assert_no_tasks()

    asyncio.run(scenario())


def test_cooldown_begins_after_cleanup_and_prevents_early_next_trial(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        executor = LocalExecutor(clock)
        task = asyncio.create_task(
            run_study(
                config(cooldown_ns=20 * MS),
                tmp_path / "study",
                executor=executor,
                clock=clock,
            )
        )
        for index in range(4):
            await wait_until(
                lambda index=index: (
                    len(executor.owned) > index
                    and all(client.closed for client in executor.owned[index])
                    and (tmp_path / "study" / f"trial-{index:04d}.json").exists()
                )
            )
            await settle()
            assert len(executor.calls) == index + 1
            if index < 3:
                cooldown_started = clock.elapsed_ns
                await advance(clock, cooldown_started + 19 * MS)
                assert len(executor.calls) == index + 1
                await advance(clock, cooldown_started + 20 * MS)
        result = await task
        assert result.status == "COMPLETED"
        assert result.elapsed_ns == (4 * 170 + 60) * MS
        assert not clock.waiters
        assert_no_tasks()
        raw = result.model_dump(mode="json")
        raw["elapsed_ns"] -= 60 * MS
        manifest_path = tmp_path / "study" / "manifest.json"
        manifest_path.chmod(0o600)
        manifest_path.write_bytes(canonical_json_bytes(raw) + b"\n")
        manifest_path.chmod(0o400)
        with pytest.raises(EvaluationError):
            report_study(
                config(cooldown_ns=20 * MS), tmp_path / "study", tmp_path / "report"
            )
        assert not (tmp_path / "report").exists()

    asyncio.run(scenario())


def test_cooldown_clock_cannot_wake_early_and_dispatch_next_trial(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parsed = config(cooldown_ns=20 * MS)

        class EarlyClock(ManualClock):
            async def sleep_until(self, absolute_ns: int) -> None:
                if absolute_ns == self.epoch_ns + parsed.limits.max_duration_ns:
                    await super().sleep_until(absolute_ns)

        clock = EarlyClock()
        executor = LocalExecutor(clock)
        result = await run_study(
            parsed, tmp_path / "study", executor=executor, clock=clock
        )
        assert result.status == "ABORTED"
        assert result.reason == "CONTROLLER_OR_CLEANUP_FAILED"
        assert result.trials[0].state == "RETURNED"
        assert all(entry.state == "NOT_RUN" for entry in result.trials[1:])
        assert executor.calls == ["trial-0000"]
        assert (tmp_path / "study" / "trial-0000.json").exists()
        assert not (tmp_path / "study" / "trial-0001.json").exists()
        assert all(client.closed for owned in executor.owned for client in owned)
        assert not clock.waiters
        assert_no_tasks()

    asyncio.run(scenario())


def test_deadline_rechecked_before_next_trial_when_timer_callback_is_delayed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parsed = config()
        clock = ManualClock()
        executor = LocalExecutor(clock)
        executor.after = lambda _index: clock.advance_to(parsed.limits.max_duration_ns)
        result = await run_study(
            parsed, tmp_path / "study", executor=executor, clock=clock
        )
        assert executor.calls == ["trial-0000"]
        assert result.status == "CANCELLED" and result.reason == "STUDY_DEADLINE"
        assert result.trials[0].state == "RETURNED"
        assert result.trials[0].result_status == "COMPLETED"
        assert all(entry.state == "NOT_RUN" for entry in result.trials[1:])
        assert not clock.waiters
        assert_no_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("where", ["inflight", "cooldown"])
def test_study_deadline_stops_current_work_and_preserves_all_planned_trials(
    tmp_path: Path, where: str
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        parsed = config(cooldown_ns=20 * MS if where == "cooldown" else 0)
        executor = LocalExecutor(clock)
        if where == "inflight":
            executor.mode[0] = "hold"
        task = asyncio.create_task(
            run_study(parsed, tmp_path / "study", executor=executor, clock=clock)
        )
        if where == "inflight":
            await asyncio.wait_for(executor.held.wait(), 1)
        else:
            await wait_until(lambda: (tmp_path / "study" / "trial-0000.json").exists())
        await advance(clock, parsed.limits.max_duration_ns)
        result = await asyncio.wait_for(task, 1)
        assert result.status == "CANCELLED" and result.reason == "STUDY_DEADLINE"
        assert executor.calls == ["trial-0000"]
        assert result.trials[0].state == "RETURNED"
        assert result.trials[0].result_status == (
            "CANCELLED" if where == "inflight" else "COMPLETED"
        )
        assert all(entry.state == "NOT_RUN" for entry in result.trials[1:])
        assert all(client.closed for owned in executor.owned for client in owned)
        assert not clock.waiters
        assert_no_tasks()

    asyncio.run(scenario())


def test_executor_error_at_deadline_preserves_error_and_deadline_precedence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        parsed = config()
        calls: list[str] = []

        async def executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> HealthyRoutingResult:
            calls.append(trial.trial_id)
            clock.advance_to(parsed.limits.max_duration_ns)
            raise EvaluationError("controller failure")

        result = await run_study(
            parsed, tmp_path / "study", executor=executor, clock=clock
        )
        assert calls == ["trial-0000"]
        assert result.status == "CANCELLED" and result.reason == "STUDY_DEADLINE"
        assert result.trials[0].state == "ABORTED"
        assert result.trials[0].failure_reason == "CONTROLLER_OR_CLEANUP_FAILED"
        assert result.trials[0].cleanup == "UNCONFIRMED"
        assert all(entry.state == "NOT_RUN" for entry in result.trials[1:])
        assert not clock.waiters
        assert_no_tasks()

    asyncio.run(scenario())


def test_preflight_failure_creates_no_directory_or_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(_plan: object) -> None:
        raise EvaluationError("metadata bound")

    monkeypatch.setattr(study_module, "preflight_metadata", reject)
    executor = LocalExecutor()
    output = tmp_path / "study"
    with pytest.raises(EvaluationError, match="metadata bound"):
        asyncio.run(run_study(config(), output, executor=executor))
    assert not output.exists()
    assert executor.calls == [] and executor.owned == []


@pytest.mark.parametrize(
    "scenario,fail_at",
    [
        ("HEALTHY", 2),
        ("HEALTHY", 3),
        ("STALE_LOAD", 2),
        ("STALE_LOAD", 3),
        ("STALE_LOAD", 4),
    ],
)
def test_partial_client_construction_closes_every_acquired_client(
    scenario: str, fail_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Acquired:
        close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    async def run() -> None:
        created: list[Acquired] = []
        attempts = 0

        def construct(*args: object, **kwargs: object) -> Acquired:
            nonlocal attempts
            attempts += 1
            if attempts == fail_at:
                raise OSError("private construction failure")
            client = Acquired()
            created.append(client)
            return client

        monkeypatch.setattr(study_module, "AiohttpTransport", construct)
        monkeypatch.setattr(study_module, "AiohttpProbeTransport", construct)
        trial = compile_study(load(study_payload(scenario))).trials[0]
        with pytest.raises((OSError, EvaluationError)):
            await execute_trial(trial, stop=asyncio.Event())
        assert attempts == fail_at
        assert len(created) == fail_at - 1
        assert all(client.close_calls == 1 for client in created)
        assert_no_tasks()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "exception", "timeout"])
def test_partial_acquisition_cleanup_joins_all_closes_and_blocks_failed_claim(
    failure: str | None,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = [Probe(clock, (1, 2)), Probe(clock, (1, 2))]
        if failure == "exception":
            owned[0].close_error = True
        elif failure == "timeout":
            owned[0].close_release = asyncio.Event()
        if failure is None:
            await close_routing_clients(owned, 20 * MS)
        else:
            with pytest.raises(
                EvaluationError, match=r"^routing client cleanup failed$"
            ):
                await close_routing_clients(owned, 20 * MS)
        assert all(
            client.close_finished and client.close_calls == 1 for client in owned
        )
        assert owned[1].closed
        assert_no_tasks()

    asyncio.run(scenario())


def test_partial_acquisition_cleanup_preserves_repeated_cancellation_after_join() -> (
    None
):
    async def scenario() -> None:
        owned = [Probe(ManualClock(), (1, 2)), Probe(ManualClock(), (1, 2))]
        release = asyncio.Event()
        for client in owned:
            client.close_release = release
        task = asyncio.create_task(close_routing_clients(owned, 20 * MS))
        await wait_until(lambda: all(client.close_started.is_set() for client in owned))
        for _ in range(3):
            task.cancel()
            await settle()
            assert not task.done()
            assert all(not client.close_finished for client in owned)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(client.closed and client.close_calls == 1 for client in owned)
        assert_no_tasks()

    asyncio.run(scenario())


def test_existing_output_is_not_overwritten_and_creates_no_clients(
    tmp_path: Path,
) -> None:
    output = tmp_path / "study"
    output.mkdir(mode=0o700)
    sentinel = output / "plan.json"
    sentinel.write_bytes(b"prior committed output")
    executor = LocalExecutor()
    with pytest.raises((EvaluationError, OSError)):
        asyncio.run(run_study(config(), output, executor=executor))
    assert sentinel.read_bytes() == b"prior committed output"
    assert executor.calls == [] and executor.owned == []


def test_output_failure_retains_prior_committed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = StudyDirectory.write

    def write(self: StudyDirectory, name: str, content: bytes, *, limit: int) -> None:
        if name == "trial-0001.json":
            raise OSError("private disk failure")
        original(self, name, content, limit=limit)

    monkeypatch.setattr(StudyDirectory, "write", write)
    executor = LocalExecutor()
    output = tmp_path / "study"
    result = asyncio.run(
        run_study(config(), output, executor=executor, clock=executor.clock)
    )
    assert result.status == "ABORTED" and result.reason == "OUTPUT_FAILED"
    assert executor.calls == ["trial-0000", "trial-0001"]
    assert (output / "trial-0000.json").exists()
    assert not (output / "trial-0001.json").exists()
    assert result.trials[1].state == "ABORTED"
    assert all(entry.state == "NOT_RUN" for entry in result.trials[2:])


@pytest.fixture
def completed(tmp_path: Path) -> tuple[StudyConfig, Path, StudyManifest]:
    parsed = config()
    output = tmp_path / "study"
    executor = LocalExecutor()
    result = asyncio.run(
        run_study(parsed, output, executor=executor, clock=executor.clock)
    )
    assert result.status == "COMPLETED"
    return parsed, output, result


@pytest.mark.parametrize(
    "field", ["plan_sha256", "trial_id", "block_id", "workload_sha256", "config_sha256"]
)
def test_trial_envelope_rejects_reassignment(
    completed: tuple[StudyConfig, Path, StudyManifest], field: str
) -> None:
    parsed, output, _ = completed
    plan = compile_study(parsed)
    raw = json.loads((output / "trial-0000.json").read_bytes())
    raw[field] = (
        "trial-0001"
        if field == "trial_id"
        else "other-block"
        if field == "block_id"
        else "sha256:" + "f" * 64
    )
    content = canonical_json_bytes(raw) + b"\n"
    with pytest.raises(EvaluationError):
        load_study_trial_bytes(content, plan, plan.trials[0])


@pytest.mark.parametrize("change", ["unknown", "duplicate", "raw", "swap"])
def test_trial_envelope_requires_closed_canonical_content(
    completed: tuple[StudyConfig, Path, StudyManifest], change: str
) -> None:
    parsed, output, _ = completed
    plan = compile_study(parsed)
    content = (output / "trial-0000.json").read_bytes()
    raw = json.loads(content)
    if change == "unknown":
        raw["private"] = "hidden value"
        content = canonical_json_bytes(raw) + b"\n"
    elif change == "duplicate":
        content = content[:-2] + b',"trial_id":"trial-0000"}\n'
    elif change == "raw":
        content = canonical_json_bytes(raw["result"]) + b"\n"
    else:
        content = (output / "trial-0001.json").read_bytes()
    with pytest.raises(EvaluationError):
        load_study_trial_bytes(content, plan, plan.trials[0])


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'{"result":' + b"[" * 33 + b"0" + b"]" * 33 + b"}", id="depth"),
        pytest.param(b'{"result":10000000000000000}', id="integer-digits"),
    ],
)
def test_trial_lexical_preflight_precedes_model_construction(
    content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def unexpected(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("model construction preceded lexical bounds")

    plan = compile_study(config())
    monkeypatch.setattr(
        study_module.StudyTrialArtifact, "model_validate_json", unexpected
    )
    with pytest.raises(
        EvaluationError, match="study trial artifact violates its expected binding"
    ):
        load_study_trial_bytes(content, plan, plan.trials[0])
    assert calls == 0


def test_manifest_lexical_preflight_precedes_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def unexpected(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("manifest construction preceded lexical bounds")

    plan = compile_study(config())
    output = tmp_path / "study"
    with StudyDirectory.create(output, budget=1024 * 1024) as directory:
        directory.write("plan.json", plan_bytes(plan), limit=1024 * 1024)
        directory.write(
            "manifest.json",
            b'{"trials":' + b"[" * 64 + b"0" + b"]" * 64 + b"}",
            limit=1024 * 1024,
        )
    monkeypatch.setattr(StudyManifest, "model_validate_json", unexpected)
    with (
        StudyDirectory.open(output) as directory,
        pytest.raises(
            EvaluationError, match="study manifest violates its canonical contract"
        ),
    ):
        read_manifest(directory, plan)
    assert calls == 0


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.update(plan_sha256="sha256:" + "0" * 64),
        lambda p: p["trials"][0].update(trial_id="trial-0001"),
        lambda p: p["trials"][0].update(result_filename="trial-0001.json"),
        lambda p: p["trials"][0].update(result_sha256=None),
        lambda p: p["trials"][0].update(state="NOT_RUN"),
        lambda p: p["trials"][0].update(cleanup="UNCONFIRMED"),
        lambda p: p.update(status="COMPLETED", reason="WARMUP_FAILED"),
        lambda p: p.update(status="ABORTED", reason="CANCELLED"),
        lambda p: p.update(status="ABORTED", reason="STUDY_DEADLINE"),
        lambda p: p.update(status="ABORTED", reason="WARMUP_FAILED"),
    ],
)
def test_manifest_rejects_inconsistent_trial_ledger(
    completed: tuple[StudyConfig, Path, StudyManifest], edit: Callable[[dict], None]
) -> None:
    parsed, _, manifest = completed
    raw = manifest.model_dump(mode="json")
    edit(raw)
    with pytest.raises(EvaluationError):
        validate_manifest(
            compile_study(parsed), StudyManifest.model_validate_json(json.dumps(raw))
        )


def test_report_validates_bindings_before_creating_output(
    completed: tuple[StudyConfig, Path, StudyManifest], tmp_path: Path
) -> None:
    parsed, output, _ = completed
    artifact = output / "trial-0000.json"
    artifact.chmod(0o600)
    artifact.write_bytes((output / "trial-0001.json").read_bytes())
    artifact.chmod(0o400)
    report = tmp_path / "report"
    with pytest.raises(EvaluationError):
        report_study(parsed, output, report)
    assert not report.exists()


def test_report_accepts_bound_results_and_never_overwrites_its_output(
    completed: tuple[StudyConfig, Path, StudyManifest], tmp_path: Path
) -> None:
    parsed, output, _ = completed
    destination = tmp_path / "report"
    report = report_study(parsed, output, destination)
    assert report["status"] == "COMPLETED"
    assert report["coverage"]["planned_trials"] == 4
    assert report["coverage"]["returned_trials"] == 4
    assert not report["evidence_eligible"]
    assert set(path.name for path in destination.iterdir()) == {
        "report.json",
        "report.md",
    }
    original = (destination / "report.json").read_bytes()
    with pytest.raises((EvaluationError, OSError)):
        report_study(parsed, output, destination)
    assert (destination / "report.json").read_bytes() == original


@pytest.mark.parametrize("change", ["elapsed", "status"])
def test_report_rejects_manifest_claims_inconsistent_with_measured_results(
    completed: tuple[StudyConfig, Path, StudyManifest], tmp_path: Path, change: str
) -> None:
    parsed, output, manifest = completed
    raw = manifest.model_dump(mode="json")
    if change == "elapsed":
        raw["elapsed_ns"] = 0
    else:
        raw["trials"][-1]["result_status"] = "WARMUP_FAILED"
        raw.update(status="ABORTED", reason="WARMUP_FAILED")
    path = output / "manifest.json"
    path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(raw) + b"\n")
    path.chmod(0o400)
    destination = tmp_path / "report"
    with pytest.raises(EvaluationError):
        report_study(parsed, output, destination)
    assert not destination.exists()
