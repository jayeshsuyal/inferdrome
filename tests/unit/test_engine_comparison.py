"""CPU replay, matched inputs, complete populations and exact phase ownership."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from inferdrome.evaluation.contracts import EvaluationError, load_config_bytes
from inferdrome.evaluation.engine_comparison import (
    GpuObservations,
    GpuSample,
    MatchedPlan,
    PhaseReceipt,
    comparison_report,
    encoded,
    percentage_delta,
    run_comparison,
)
from inferdrome.evaluation.runner import run_evaluation
from inferdrome.qwen3_campaign import QWEN3_8B_MODEL_ID, qwen3_expected_snapshot_sha256
from tests.unit.test_evaluation_runner import (
    DONE,
    USAGE,
    FakeTransport,
    ManualClock,
    Script,
    advance,
    config,
    content,
    settle,
)

DIGEST = "sha256:" + "a" * 64


def plan():
    value = config([10, 30]).model_dump(mode="json")
    value["model"] = QWEN3_8B_MODEL_ID
    value["offers"][1]["endpoint_id"] = "endpoint-b"
    return MatchedPlan(
        workload=load_config_bytes(encoded(value)),
        snapshot_sha256=qwen3_expected_snapshot_sha256(),
        chat_template_sha256=DIGEST,
    )


async def synthetic(config, stop):
    clock = ManualClock()
    script = Script(
        chunks=((2, content()), (4, content(finish="stop") + USAGE + DONE)), eof_ns=4
    )
    transport = FakeTransport(clock, {0: script, 1: script})
    task = asyncio.create_task(
        run_evaluation(config, transport, clock=clock, stop=stop)
    )
    await settle()
    for t in range(1, config.bounds.duration_ns + config.bounds.drain_ns + 2):
        await advance(clock, t)
        if task.done():
            break
    result = await task
    assert transport.closed
    return replace(result, evidence_class="SYNTHETIC_ONLY")


class FakePair:
    def __init__(self, engine, events, *, fail=None):
        self.engine, self.events, self.fail = engine, events, fail

    async def prepare(self, plan, *, stop):
        assert not self.events or self.events[-1][1] == "cleanup"
        self.events.append((self.engine, "prepare"))
        if self.fail == "prepare":
            raise ValueError("PRIVATE_ERROR")
        return PhaseReceipt(
            engine=self.engine,
            version="0.26.0" if self.engine == "vllm" else "0.5.15",
            runtime_sha256=DIGEST,
            hardware_sha256=DIGEST,
            plan_sha256=plan.digest,
            launch_sha256=DIGEST,
            startup_to_ready_ns=100,
        )

    async def begin(self):
        self.events.append((self.engine, "measure"))

    async def finish(self):
        return GpuObservations(
            status="SAMPLED",
            samples=(
                GpuSample(
                    elapsed_ns=1,
                    memory_mib=(20000, 21000),
                    utilization_percent=(50, 60),
                ),
            ),
        )

    async def cleanup(self):
        self.events.append((self.engine, "cleanup"))
        if self.fail == "cleanup":
            raise ValueError("PRIVATE_CLEANUP")


def pairs(events, **kw):
    return {e: FakePair(e, events, **kw) for e in ("vllm", "sglang")}


def test_same_workload_counterbalance_deterministic_and_no_raw_content(tmp_path):
    events, configs = [], []
    recipe = plan()

    async def execute(config, stop):
        configs.append(encoded(config.model_dump(mode="json")))
        return await synthetic(config, stop)

    report = asyncio.run(
        run_comparison(recipe, pairs(events), tmp_path / "a", execute=execute)
    )
    again = asyncio.run(
        run_comparison(recipe, pairs([]), tmp_path / "b", execute=synthetic)
    )
    assert report == again
    assert report["engine_order"] == ["vllm", "sglang", "sglang", "vllm"]
    assert [e for e, action in events if action == "prepare"] == report["engine_order"]
    assert len(set(configs[1::2])) == 1  # All engine phases receive identical bytes.
    assert len(set(configs[0::2])) == 1  # Identical explicit warmup.
    assert all(
        s["population"]
        == {
            "offered": 2,
            "success": 2,
            "error": 0,
            "rejected": 0,
            "outcomes": {"SUCCESS": 2},
        }
        for s in report["summaries"]
    )
    assert len(report["pairs"]) == 2
    assert all(
        v == "0.000000"
        for k, v in report["pairs"][0]["sglang_vs_vllm_percent"].items()
        if k != "model_load_ns"
    )
    raw = (tmp_path / "a/report.json").read_bytes()
    assert b"prompt-0" not in raw and b"127.0.0.1" not in raw
    assert comparison_report(recipe, report["phases"], completed=True) == report
    with pytest.raises(FileExistsError):
        asyncio.run(
            run_comparison(recipe, pairs([]), tmp_path / "a", execute=synthetic)
        )


@pytest.mark.parametrize(
    "ref,candidate,expected",
    [
        (100, 75, "-25.000000"),
        (100, 150, "50.000000"),
        (0, 10, None),
        (None, 2, None),
        (3, 4, "33.333333"),
        (100, 0, "-100.000000"),
    ],
)
def test_percentage_math(ref, candidate, expected):
    assert percentage_delta(ref, candidate) == expected


@pytest.mark.parametrize(
    "fail,cleanup", [("prepare", "CONFIRMED"), ("cleanup", "UNCONFIRMED")]
)
def test_failure_always_cleans_and_never_starts_next_engine(tmp_path, fail, cleanup):
    events = []
    with pytest.raises(EvaluationError):
        asyncio.run(
            run_comparison(
                plan(), pairs(events, fail=fail), tmp_path / "out", execute=synthetic
            )
        )
    assert events[-1] == ("vllm", "cleanup")
    assert not any(engine == "sglang" for engine, _ in events)
    report = json.loads((tmp_path / "out/report.json").read_bytes())
    assert report["status"] == "ABORTED"
    assert report["failed_phase_cleanup"] == cleanup
    assert report["pairs"] == []


@pytest.mark.parametrize(
    "change",
    ["missing", "duplicate", "schedule", "hardware", "order", "runtime", "outcome"],
)
def test_offline_tamper_population_and_match_rejected(tmp_path, change):
    recipe = plan()
    report = asyncio.run(
        run_comparison(recipe, pairs([]), tmp_path / "out", execute=synthetic)
    )
    phases = json.loads(encoded(report["phases"]))
    if change == "missing":
        phases[0]["population"]["records"].pop()
    elif change == "duplicate":
        phases[0]["population"]["records"][1] = phases[0]["population"]["records"][0]
    elif change == "schedule":
        phases[0]["population"]["records"][0]["scheduled_ns"] += 1
    elif change == "hardware":
        phases[1]["receipt"]["hardware_sha256"] = "sha256:" + "b" * 64
    elif change == "runtime":
        phases[3]["receipt"]["version"] = "0.27.0"
    elif change == "outcome":
        phases[0]["population"]["outcomes"] = {"SUCCESS": 999}
    else:
        phases.reverse()
        phases[0]["receipt"]["engine"] = "sglang"
    with pytest.raises(ValueError):
        comparison_report(recipe, phases, completed=True)


def test_invalid_result_never_advances_and_incomplete_population_not_zero(tmp_path):
    events = []
    calls = 0

    async def execute(config, stop):
        nonlocal calls
        calls += 1
        result = await synthetic(config, stop)
        return replace(result, records=()) if calls == 2 else result

    with pytest.raises(EvaluationError):
        asyncio.run(
            run_comparison(plan(), pairs(events), tmp_path / "out", execute=execute)
        )
    assert events[-1] == ("vllm", "cleanup")
    assert calls == 2


def test_engine_order_and_source_revision_are_bound():
    recipe = plan()
    reverse = recipe.model_copy(update={"first_engine": "sglang"})
    assert reverse.order() == ("sglang", "vllm", "vllm", "sglang")
    assert reverse.digest != recipe.digest
    invalid = recipe.model_dump(mode="json")
    invalid["model_revision"] = "b" * 40
    with pytest.raises(ValueError):
        MatchedPlan.model_validate_json(encoded(invalid))
