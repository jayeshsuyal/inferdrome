"""Real HTTP ownership and matched wire bytes, never a vLLM cache test."""

from __future__ import annotations

import asyncio
from pathlib import Path

from inferdrome.evaluation.cache import load_cache_cell, run_cache_cell
from inferdrome.evaluation.cache_report import report_cache_experiment
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.integration.test_evaluation_loopback import (
    _SUCCESS,
    _headers,
    _Request,
    _servers,
)
from tests.unit.test_evaluation_cache import cache_payload, make_plan, preparation


def _payload(origins: tuple[str, str]) -> dict[str, object]:
    payload = cache_payload()
    for endpoint, origin in zip(payload["endpoints"], origins, strict=True):
        endpoint["origin"] = origin
    payload["bounds"].update(
        duration_ns=500_000_000,
        request_timeout_ns=1_500_000_000,
        drain_ns=700_000_000,
        cleanup_timeout_ns=100_000_000,
    )
    payload["profile"].update(
        window_end_ns=500_000_000,
        first_content_slo_ns=500_000_000,
        completion_slo_ns=600_000_000,
    )
    for index, case in enumerate(payload["blocks"][0]["cases"]):
        case["scheduled_ns"] = 50_000_000 + index * 50_000_000
    return payload


def test_four_cache_cells_preserve_pair_bytes_and_fixed_assignments(
    tmp_path: Path,
) -> None:
    async def handler(
        _request: _Request,
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _headers(writer)
        for offset in range(0, len(_SUCCESS), 17):
            writer.write(_SUCCESS[offset : offset + 17])
            await writer.drain()

    async def scenario() -> None:
        async with _servers(handler) as pair:
            plan = make_plan(_payload(pair.origins))
            wire: dict[str, list[tuple[int, bytes]]] = {}
            inputs: dict[str, Path] = {}
            for index, cell in enumerate(plan.cells):
                before = len(pair.requests)
                transport = AiohttpTransport(cell.config)
                destination = tmp_path / cell.cell_id
                manifest = await run_cache_cell(
                    plan,
                    cell.cell_id,
                    preparation(plan, index),
                    destination,
                    transport=transport,
                )
                assert manifest.status == "COMPLETED" and transport.closed
                imported = load_cache_cell(plan, cell, destination)
                assert imported.result is not None
                assert all(row.outcome == "SUCCESS" for row in imported.result.records)
                wire[cell.condition] = sorted(
                    (request.endpoint, canonical_json_bytes(request.body))
                    for request in pair.requests[before:]
                )
                assert len(wire[cell.condition]) == 4
                assert [row.endpoint_id for row in imported.result.records] == [
                    "endpoint-a",
                    "endpoint-b",
                    "endpoint-a",
                    "endpoint-b",
                ]
                inputs[cell.cell_id] = destination
            assert wire["S0"] == wire["S1"]
            assert wire["U0"] == wire["U1"]
            assert wire["S0"] != wire["U0"]
            report = report_cache_experiment(plan, inputs, tmp_path / "report")
            assert report["evidence_class"] == "SYNTHETIC_ONLY"
            assert report["evidence_eligible"] is False
            assert len(pair.requests) == 16
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_cache_loopback_failures_keep_the_offered_population(tmp_path: Path) -> None:
    async def handler(
        _request: _Request,
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _headers(writer, status=503)

    async def scenario() -> None:
        async with _servers(handler) as pair:
            plan = make_plan(_payload(pair.origins))
            cell = plan.cells[0]
            transport = AiohttpTransport(cell.config)
            manifest = await run_cache_cell(
                plan,
                cell.cell_id,
                preparation(plan),
                tmp_path / "cell",
                transport=transport,
            )
            assert manifest.status == "COMPLETED" and transport.closed
            imported = load_cache_cell(plan, cell, tmp_path / "cell")
            assert imported.result is not None and len(imported.result.records) == 4
            assert all(row.outcome == "HTTP_ERROR" for row in imported.result.records)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_cache_loopback_stop_closes_inflight_client_and_keeps_unoffered_records(
    tmp_path: Path,
) -> None:
    async def handler(
        _request: _Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _headers(writer)
        await reader.read()

    async def scenario() -> None:
        async with _servers(handler) as pair:
            plan = make_plan(_payload(pair.origins))
            cell = plan.cells[0]
            transport = AiohttpTransport(cell.config)
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_cache_cell(
                    plan,
                    cell.cell_id,
                    preparation(plan),
                    tmp_path / "cell",
                    transport=transport,
                    stop=stop,
                )
            )
            async with asyncio.timeout(2):
                while not pair.requests:
                    await asyncio.sleep(0.001)
            stop.set()
            manifest = await asyncio.wait_for(task, 2)
            assert manifest.status == "CANCELLED" and transport.closed
            imported = load_cache_cell(plan, cell, tmp_path / "cell")
            assert imported.result is not None
            assert len(imported.result.records) == 4
            assert all(row.outcome == "CANCELLED" for row in imported.result.records)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())
