"""Run every matched phase over real loopback HTTP using the shared SSE client."""

import asyncio

from inferdrome.evaluation.contracts import load_config_bytes
from inferdrome.evaluation.engine_comparison import encoded, run_comparison
from tests.integration.test_evaluation_loopback import _SUCCESS, _headers, _servers
from tests.unit.test_engine_comparison import pairs, plan


def test_two_real_loopback_servers_identical_bodies_and_all_phases(tmp_path):
    async def exercise():
        async def handler(request, reader, writer):
            await _headers(writer)
            writer.write(_SUCCESS)
            await writer.drain()

        async with _servers(handler) as pair:
            recipe = plan()
            value = recipe.workload.model_dump(mode="json")
            value["bounds"].update(
                duration_ns=1_000_000_000,
                request_timeout_ns=1_000_000_000,
                drain_ns=100_000_000,
            )
            for i, endpoint in enumerate(value["endpoints"]):
                endpoint["origin"] = pair.origins[i]
            for i, offer in enumerate(value["offers"]):
                offer["scheduled_ns"] = i * 50_000_000
            recipe = recipe.model_copy(
                update={"workload": load_config_bytes(encoded(value))}
            )
            events = []
            report = await run_comparison(recipe, pairs(events), tmp_path / "out")
            assert report["status"] == "COMPLETED"
            assert len(pair.requests) == 16  # 2 warmup + 2 measured, four phases.
            for offset in (0, 4, 8, 12):
                assert [r.body for r in pair.requests[offset : offset + 4]] == [
                    r.body for r in pair.requests[:4]
                ]
            assert {r.endpoint for r in pair.requests} == {0, 1}
            assert all(s["population"]["success"] == 2 for s in report["summaries"])
            assert all(
                r.body["max_tokens"] == recipe.workload.max_tokens
                for r in pair.requests
            )
            assert all(r.body["temperature"] == 0 for r in pair.requests)
        assert not pair.handlers

    asyncio.run(exercise())
