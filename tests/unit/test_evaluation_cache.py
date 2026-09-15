"""Single-cell ownership, immutable import, and external-declaration boundaries."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import inferdrome.evaluation.cache as cache_module
from inferdrome.evaluation.cache import (
    CachePreparation,
    cache_plan_bytes,
    load_cache_cell,
    load_cache_preparation_bytes,
    run_cache_cell,
    validate_cache_preparation,
)
from inferdrome.evaluation.cache_config import (
    CompiledCachePlan,
    compile_cache_experiment,
    load_cache_config_bytes,
)
from inferdrome.evaluation.cache_workload import (
    CacheWorkloadVerification,
    WorkloadPair,
    _verify_cache_workloads,
)
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.study_validation import load_fixed_population_bytes
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_runner import (
    FakeTransport,
    ManualClock,
    Script,
    advance,
    settle,
)

MS = 1_000_000


@dataclass
class _Encoding:
    ids: list[int]


class CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> _Encoding:
        assert add_special_tokens is False
        return _Encoding([ord(character) for character in text])


def synthetic_verifier(
    pairs: tuple[WorkloadPair, ...], *, block_size: int, max_tokens: int
) -> CacheWorkloadVerification:
    return _verify_cache_workloads(
        pairs,
        tokenizer=CharacterTokenizer(),
        block_size=block_size,
        max_tokens=max_tokens,
    )


def cache_payload() -> dict[str, Any]:
    return {
        "schema_version": "inferdrome.evaluation-cache-config.v1",
        "experiment_id": "cache-rehearsal",
        "source_commit": "0" * 40,
        "endpoints": [
            {"endpoint_id": f"endpoint-{letter}", "origin": f"http://127.0.0.1:{port}"}
            for letter, port in (("a", 19121), ("b", 19122))
        ],
        "bounds": {
            "max_requests": 4,
            "concurrency": 2,
            "max_queue": 2,
            "duration_ns": 100 * MS,
            "request_timeout_ns": 500 * MS,
            "drain_ns": 250 * MS,
            "cleanup_timeout_ns": 20 * MS,
            "max_content_events": 8,
        },
        "max_tokens": 16,
        "profile": {
            "profile_id": "rehearsal",
            "window_start_ns": 0,
            "window_end_ns": 100 * MS,
            "first_content_slo_ns": 100 * MS,
            "completion_slo_ns": 200 * MS,
        },
        "blocks": [
            {
                "block_id": "block-a",
                "workload_seed": 7,
                "shared_document": "S" * 96,
                "cases": [
                    {
                        "unique_document": chr(65 + index) * 96,
                        "suffix": f"query-{index}",
                        "scheduled_ns": index * 10 * MS,
                    }
                    for index in range(4)
                ],
                "cell_order": ["S0", "S1", "U0", "U1"],
                "attempt_ids": [f"attempt-{index}" for index in range(4)],
            }
        ],
        "preparation_protocol_sha256": "sha256:" + "a" * 64,
        "cache_block_size": 8,
        "limits": {},
        "reporting": {"bootstrap_seed": 11},
    }


def make_plan(payload: dict[str, Any] | None = None) -> CompiledCachePlan:
    config = load_cache_config_bytes(json.dumps(payload or cache_payload()).encode())
    return compile_cache_experiment(config, verifier=synthetic_verifier)


def preparation(plan: CompiledCachePlan, index: int = 0) -> CachePreparation:
    cell = plan.cells[index]
    value = {
        "schema_version": "inferdrome.evaluation-cache-preparation.v1",
        "plan_sha256": sha256_digest(cache_plan_bytes(plan)),
        "cell_id": cell.cell_id,
        "cell_sha256": cell.cell_sha256,
        "attempt_id": cell.attempt_id,
        "preparation_id": f"preparation-{index}",
        "preparation_protocol_sha256": plan.config.preparation_protocol_sha256,
        "config_sha256": cell.config_sha256,
        "order_position": index,
        "previous_attempt_id": plan.cells[index - 1].attempt_id if index else None,
        "runtime_recipe_sha256": "sha256:" + "b" * 64,
        "cache_block_size": plan.config.cache_block_size,
        "endpoints": [
            {
                "endpoint_id": endpoint.endpoint_id,
                "process_generation_sha256": sha256_digest(
                    f"{index}:{endpoint.endpoint_id}".encode()
                ),
                "prefix_caching": cell.mode,
            }
            for endpoint in cell.config.endpoints
        ],
        "initial_prefix_state": "DECLARED_ABSENT",
        "method": "DECLARED_FRESH_PROCESSES",
        "method_reference": "sha256:" + "c" * 64,
        "model_warmup_reference": "sha256:" + "d" * 64,
        "model_warmup_overlap": "DECLARED_DISJOINT",
        "chronology_reference": "sha256:" + "e" * 64,
        "exclusive_traffic": "DECLARED_NO_OTHER_TRAFFIC",
    }
    return load_cache_preparation_bytes(json.dumps(value).encode())


async def run_fixture_cell(
    plan: CompiledCachePlan,
    path: Path,
    index: int = 0,
    *,
    declaration: CachePreparation | None = None,
    scripts: dict[int, Script] | None = None,
    stop: asyncio.Event | None = None,
) -> tuple[cache_module.CacheCellManifest, FakeTransport]:
    clock = ManualClock()
    transport = FakeTransport(clock, scripts or {})
    task = asyncio.create_task(
        run_cache_cell(
            plan,
            plan.cells[index].cell_id,
            declaration or preparation(plan, index),
            path,
            transport=transport,
            clock=clock,
            stop=stop,
        )
    )
    await settle()
    for timestamp in range(1, 601):
        if task.done():
            break
        await advance(clock, timestamp * MS)
    assert task.done()
    result = await task
    assert not clock.waiters
    return result, transport


def rewrite_json(path: Path, value: dict[str, Any]) -> None:
    path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    path.chmod(0o400)


def test_one_invocation_replays_one_cell_and_binds_an_immutable_bundle(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    manifest, transport = asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    assert manifest.status == "COMPLETED"
    assert len(transport.calls) == 4 and transport.closed
    assert [call[2] for call in transport.calls] == [
        plan.cells[0].config.endpoints[index % 2].origin for index in range(4)
    ]
    imported = load_cache_cell(plan, plan.cells[0], tmp_path / "cell")
    assert imported.result is not None and imported.attribution_eligible
    assert imported.evidence_class == "SYNTHETIC_ONLY"
    assert imported.result.evidence_eligible is False
    assert imported.preparation.runtime_verification == "UNVERIFIED"
    assert len(imported.result.records) == 4
    assert set(path.name for path in (tmp_path / "cell").iterdir()) == {
        "plan.json",
        "manifest.json",
        "trial-0000.json",
    }
    artifacts = b"".join(path.read_bytes() for path in (tmp_path / "cell").iterdir())
    assert b"127.0.0.1" not in artifacts and b"Question:" not in artifacts


def test_capacity_rejection_does_not_shift_later_endpoint_assignment(
    tmp_path: Path,
) -> None:
    payload = cache_payload()
    payload["bounds"].update(concurrency=1, max_queue=0)
    for case, timestamp in zip(
        payload["blocks"][0]["cases"], (0, 0, 20, 40), strict=True
    ):
        case["scheduled_ns"] = timestamp * MS
    plan = make_plan(payload)
    _, transport = asyncio.run(
        run_fixture_cell(plan, tmp_path / "cell", scripts={0: Script(eof_ns=10 * MS)})
    )
    result = load_cache_cell(plan, plan.cells[0], tmp_path / "cell").result
    assert result is not None
    assert result.records[1].outcome == "REJECTED_CAPACITY"
    assert result.records[2].endpoint_id == "endpoint-a"
    assert [call[0] for call in transport.calls] == [0, 2, 3]
    assert len(result.records) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_recipe_sha256", None),
        ("initial_prefix_state", "UNKNOWN"),
        ("method", "UNKNOWN"),
        ("method_reference", None),
        ("model_warmup_reference", None),
        ("model_warmup_overlap", "UNKNOWN"),
        ("chronology_reference", None),
        ("exclusive_traffic", "UNKNOWN"),
    ],
)
def test_unknown_preparation_stays_descriptive(
    tmp_path: Path, field: str, value: object
) -> None:
    plan = make_plan()
    raw = preparation(plan).model_dump(mode="json")
    raw[field] = value
    declaration = load_cache_preparation_bytes(json.dumps(raw).encode())
    manifest, _ = asyncio.run(
        run_fixture_cell(plan, tmp_path / "cell", declaration=declaration)
    )
    assert manifest.status == "COMPLETED"
    imported = load_cache_cell(plan, plan.cells[0], tmp_path / "cell")
    assert not imported.attribution_eligible and imported.attribution_reasons
    assert imported.result is not None


@pytest.mark.parametrize("field", ["prefix_caching", "process_generation_sha256"])
def test_unknown_endpoint_preparation_is_not_an_attributed_treatment(
    field: str,
) -> None:
    plan = make_plan()
    raw = preparation(plan).model_dump(mode="json")
    raw["endpoints"][0][field] = "UNKNOWN" if field == "prefix_caching" else None
    value = load_cache_preparation_bytes(json.dumps(raw).encode())
    assert validate_cache_preparation(plan, plan.cells[0], value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_sha256", "sha256:" + "0" * 64),
        ("cell_id", "cell-0001"),
        ("cell_sha256", "sha256:" + "0" * 64),
        ("attempt_id", "later-successful-attempt"),
        ("config_sha256", "sha256:" + "0" * 64),
        ("preparation_protocol_sha256", "sha256:" + "0" * 64),
        ("order_position", 1),
        ("previous_attempt_id", "unrelated"),
        ("cache_block_size", 16),
    ],
)
def test_misbound_preparation_closes_owned_transport_without_replay(
    tmp_path: Path, field: str, value: object
) -> None:
    async def scenario() -> None:
        plan = make_plan()
        raw = preparation(plan).model_dump(mode="json")
        raw[field] = value
        declaration = load_cache_preparation_bytes(json.dumps(raw).encode())
        transport = FakeTransport(ManualClock(), {})
        with pytest.raises(EvaluationError):
            await run_cache_cell(
                plan, "cell-0000", declaration, tmp_path / "cell", transport=transport
            )
        assert not transport.calls and transport.closed
        assert not (tmp_path / "cell").exists()

    asyncio.run(scenario())


def test_wrong_known_cache_mode_is_rejected() -> None:
    plan = make_plan()
    raw = preparation(plan).model_dump(mode="json")
    raw["endpoints"][1]["prefix_caching"] = "DECLARED_ENABLED"
    value = load_cache_preparation_bytes(json.dumps(raw).encode())
    with pytest.raises(EvaluationError):
        validate_cache_preparation(plan, plan.cells[0], value)


def test_unavailable_or_fake_tokenizers_never_create_a_default_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_config: object) -> None:
        pytest.fail("default client constructed without real tokenization")

    monkeypatch.setattr(cache_module, "AiohttpTransport", forbidden)
    unavailable = compile_cache_experiment(
        load_cache_config_bytes(json.dumps(cache_payload()).encode())
    )
    for plan in (make_plan(), unavailable):
        with pytest.raises(EvaluationError):
            asyncio.run(
                run_cache_cell(plan, "cell-0000", preparation(plan), tmp_path / "cell")
            )
    assert not (tmp_path / "cell").exists()


def test_stopped_cell_keeps_every_offer_and_closes_without_requests(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        stop.set()
        plan = make_plan()
        manifest, transport = await run_fixture_cell(plan, tmp_path / "cell", stop=stop)
        assert manifest.status == "CANCELLED" and not transport.calls
        value = load_cache_cell(plan, plan.cells[0], tmp_path / "cell")
        assert value.result is not None and len(value.result.records) == 4
        assert all(row.outcome == "CANCELLED" for row in value.result.records)

    asyncio.run(scenario())


def test_result_budget_failure_has_no_completed_or_retry_result(tmp_path: Path) -> None:
    payload = cache_payload()
    payload["limits"]["per_cell_result_bytes"] = 200
    plan = make_plan(payload)
    manifest, transport = asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    assert manifest.status == "ABORTED" and manifest.reason == "RESULT_LIMIT"
    assert manifest.result_filename is None and transport.closed
    assert load_cache_cell(plan, plan.cells[0], tmp_path / "cell").result is None
    with pytest.raises(FileExistsError):
        asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))


@pytest.mark.parametrize("field", ["attempt_id", "cell_id", "cell_sha256"])
def test_import_rejects_another_attempt_or_cell(tmp_path: Path, field: str) -> None:
    plan = make_plan()
    asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    manifest_path = tmp_path / "cell" / "manifest.json"
    raw = json.loads(manifest_path.read_bytes())
    raw[field] = getattr(plan.cells[1], field)
    rewrite_json(manifest_path, raw)
    with pytest.raises(EvaluationError):
        load_cache_cell(plan, plan.cells[0], tmp_path / "cell")


def test_recomputed_envelope_cannot_hide_wrong_fixed_endpoint(tmp_path: Path) -> None:
    plan = make_plan()
    asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    result_path = tmp_path / "cell" / "trial-0000.json"
    raw = json.loads(result_path.read_bytes())
    raw["result"]["records"][0]["endpoint_id"] = "endpoint-b"
    rewrite_json(result_path, raw)
    manifest_path = tmp_path / "cell" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["result_sha256"] = sha256_digest(result_path.read_bytes())
    rewrite_json(manifest_path, manifest)
    with pytest.raises(EvaluationError):
        load_cache_cell(plan, plan.cells[0], tmp_path / "cell")


@pytest.mark.parametrize(
    "content",
    [
        b"{}",
        b"[]",
        b'{"schema_version":1,"schema_version":2}',
        b'{"x":1.2}',
        b'{"x":NaN}',
        b'{"x":10000000000000000}',
        b"[" * 34 + b"]" * 34,
    ],
)
def test_preparation_and_fixed_result_parsers_reject_ambiguous_json(
    content: bytes,
) -> None:
    with pytest.raises(EvaluationError):
        load_cache_preparation_bytes(content)
    with pytest.raises(EvaluationError):
        load_fixed_population_bytes(content, make_plan().cells[0].config)


def test_fixed_population_import_retains_pr1_bytes(tmp_path: Path) -> None:
    plan = make_plan()
    asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    raw = json.loads((tmp_path / "cell" / "trial-0000.json").read_bytes())["result"]
    content = canonical_json_bytes(raw) + b"\n"
    imported = load_fixed_population_bytes(content, plan.cells[0].config)
    assert canonical_json_bytes(imported.to_dict()) + b"\n" == content
    altered = deepcopy(raw)
    altered["request_settings"]["temperature"] = False
    with pytest.raises(EvaluationError):
        load_fixed_population_bytes(canonical_json_bytes(altered), plan.cells[0].config)
    with pytest.raises(EvaluationError):
        load_fixed_population_bytes(
            content, plan.cells[0].config.model_copy(update={"max_tokens": 17})
        )


@pytest.mark.parametrize(
    "reason,cleanup,elapsed",
    [
        ("RESULT_LIMIT", "NOT_STARTED", 0),
        ("RESULT_LIMIT", "UNCONFIRMED", 1),
        ("DURATION_LIMIT", "CONFIRMED_BY_LOCAL_RESULT", 0),
        ("CANCELLED", "CONFIRMED_BY_LOCAL_RESULT", 1),
        ("EXECUTION_FAILED", "UNCONFIRMED", 2**53 - 1),
    ],
)
def test_aborted_imports_preserve_reason_cleanup_and_duration_causality(
    tmp_path: Path, reason: str, cleanup: str, elapsed: int
) -> None:
    plan = make_plan()
    asyncio.run(run_fixture_cell(plan, tmp_path / "cell"))
    path = tmp_path / "cell" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(
        status="ABORTED",
        reason=reason,
        cleanup=cleanup,
        elapsed_ns=elapsed,
        result_filename=None,
        result_sha256=None,
    )
    rewrite_json(path, manifest)
    with pytest.raises(EvaluationError):
        load_cache_cell(plan, plan.cells[0], tmp_path / "cell")


def test_final_elapsed_sample_is_shared_by_completion_guard_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan()
    asyncio.run(run_fixture_cell(plan, tmp_path / "source"))
    result = load_cache_cell(plan, plan.cells[0], tmp_path / "source").result
    assert result is not None

    async def finished(_config: object, transport: FakeTransport, **_kwargs: object):
        await transport.close()
        return result

    class BoundaryClock:
        def __init__(self) -> None:
            self.calls = 0

        def now_ns(self) -> int:
            values = (
                0,
                plan.cells[0].worst_case_duration_ns - 1,
                plan.cells[0].worst_case_duration_ns + 1,
            )
            observed = values[min(self.calls, 2)]
            self.calls += 1
            return observed

        async def sleep_until(self, _absolute_ns: int) -> None:
            raise AssertionError("no clock wait in a completed replay")

    monkeypatch.setattr(cache_module, "run_evaluation", finished)
    clock = BoundaryClock()
    manifest = asyncio.run(
        run_cache_cell(
            plan,
            "cell-0000",
            preparation(plan),
            tmp_path / "boundary",
            transport=FakeTransport(ManualClock(), {}),
            clock=clock,
        )
    )
    assert manifest.status == "COMPLETED" and clock.calls == 2
    assert (
        load_cache_cell(plan, plan.cells[0], tmp_path / "boundary").result is not None
    )


def test_cleanup_failure_publishes_only_an_unconfirmed_abort(tmp_path: Path) -> None:
    class FailingClose(FakeTransport):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("PRIVATE_CLEANUP_FAILURE")

    async def scenario() -> None:
        plan = make_plan()
        clock = ManualClock()
        transport = FailingClose(clock, {})
        task = asyncio.create_task(
            run_cache_cell(
                plan,
                "cell-0000",
                preparation(plan),
                tmp_path / "cell",
                transport=transport,
                clock=clock,
            )
        )
        await settle()
        for timestamp in range(1, 200):
            if task.done():
                break
            await advance(clock, timestamp * MS)
        manifest = await task
        assert manifest.status == "ABORTED" and manifest.cleanup == "UNCONFIRMED"
        assert manifest.result_filename is None and transport.closed
        assert load_cache_cell(plan, plan.cells[0], tmp_path / "cell").result is None
        assert (
            b"PRIVATE_CLEANUP_FAILURE"
            not in (tmp_path / "cell" / "manifest.json").read_bytes()
        )
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_repeated_cancellation_keeps_cleanup_owned(tmp_path: Path) -> None:
    class HeldClose(FakeTransport):
        def __init__(self, clock: ManualClock) -> None:
            super().__init__(clock, {0: Script(eof_ns=100 * MS)})
            self.closing = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self) -> None:
            self.closing.set()
            await self.release.wait()
            await super().close()

    async def scenario() -> None:
        plan = make_plan()
        clock = ManualClock()
        transport = HeldClose(clock)
        task = asyncio.create_task(
            run_cache_cell(
                plan,
                "cell-0000",
                preparation(plan),
                tmp_path / "cell",
                transport=transport,
                clock=clock,
            )
        )
        await settle()
        await advance(clock, 5 * MS)
        task.cancel()
        await asyncio.wait_for(transport.closing.wait(), 1)
        task.cancel()
        await settle()
        assert not task.done()
        transport.release.set()
        manifest = await asyncio.wait_for(task, 1)
        assert manifest.status in ("CANCELLED", "ABORTED")
        assert transport.closed and not clock.waiters
        assert (
            load_cache_cell(plan, plan.cells[0], tmp_path / "cell").status
            == manifest.status
        )
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())
