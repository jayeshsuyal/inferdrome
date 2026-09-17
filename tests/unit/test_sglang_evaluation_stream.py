"""SYNTHETIC_ONLY SGLang 0.5.18 protocol rehearsal; runtime is unverified.

These hand-authored frames exercise Inferdrome's unchanged native measurement
path. They are not engine captures or evidence of GPU compatibility/performance.
The pinned source contract is documented in docs/SGLANG_SERVING_PREPARATION.md.
Server-generated IDs/model labels never establish client or model identity.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from inferdrome.evaluation.runner import run_evaluation
from inferdrome.evaluation.transport import AiohttpTransport
from tests.unit.test_evaluation_runner import (
    DONE,
    FakeTransport,
    ManualClock,
    Script,
    advance,
    config,
    result_of,
    settle,
)

_MODEL = "synthetic/sglang-model"
_OUTPUT = "SYNTHETIC_ONLY generated text \u00e9"
_ERROR = "SYNTHETIC_ONLY private server error"


def _frame(*, choices: list[dict], usage: dict | None = None) -> bytes:
    """Hand-authored OpenAI chat chunk envelope, not an observed SGLang frame."""
    payload = {
        "id": "synthetic-server-generated-id",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": _MODEL,
        "choices": choices,
        "usage": usage,
    }
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def _choice(
    content: str | None = None,
    *,
    role: str | None = None,
    finish: str | None = None,
) -> bytes:
    return _frame(
        choices=[
            {
                "index": 0,
                "delta": {"role": role, "content": content},
                "finish_reason": finish,
                "matched_stop": None,
                "logprobs": None,
            }
        ]
    )


_ROLE = _choice("", role="assistant")
_CONTENT = _choice(_OUTPUT)
_FINISH = _choice(finish="stop")
_USAGE = _frame(
    choices=[],
    usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
)
_SUCCESS = _ROLE + _CONTENT + _FINISH + _USAGE + DONE


def test_pinned_chat_shape_reuses_native_measurements_without_server_identity() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        split = _CONTENT.index("\u00e9".encode()) + 1
        transport = FakeTransport(
            clock,
            {
                0: Script(
                    headers_ns=1,
                    chunks=(
                        (2, _ROLE),
                        (3, _CONTENT[:split]),
                        (5, _CONTENT[split:]),
                        (6, _choice("another content event")),
                        (7, _FINISH + _USAGE + DONE),
                    ),
                    eof_ns=9,
                ),
                1: Script(chunks=((0, _SUCCESS),)),
            },
        )
        cfg = config([10, 30]).model_copy(update={"model": _MODEL})
        task = asyncio.create_task(run_evaluation(cfg, transport, clock=clock))
        await settle()
        # Coordinator lateness does not move the planned offer or its identity.
        for elapsed in (12, 13, 14, 15, 17, 18, 19, 21, 30):
            await advance(clock, elapsed)
        result = replace(
            await result_of(task, transport, clock), evidence_class="SYNTHETIC_ONLY"
        )
        row = result.records[0]
        assert [r.request_index for r in result.records] == [0, 1]
        assert [r.scheduled_ns for r in result.records] == [10, 30]
        assert [r.arrival_observed_ns for r in result.records] == [12, 30]
        assert [r.outcome for r in result.records] == ["SUCCESS", "SUCCESS"]
        assert row.dispatch_ns == 12
        assert row.dispatch_lag_ns == 2
        assert row.response_headers_ns == 13
        assert row.first_body_byte_ns == 14
        assert row.first_content_ns == 17
        assert row.content_event_times_ns == (17, 18)
        assert row.protocol_done_ns == 19
        assert row.terminal_ns == 21
        assert row.prompt_tokens == 11
        assert row.completion_tokens == 7
        assert row.usage_provenance == "SERVER_REPORTED_STREAM_USAGE"
        # Two content frames can carry seven tokens; event timing is not ITL.
        assert result.exact_token_timing == "UNAVAILABLE"
        assert result.wire_first_response_byte == "UNAVAILABLE"
        assert result.endpoint_identity == "UNVERIFIED"
        assert result.evidence_eligible is False
        request = json.loads(transport.bodies[0])
        assert request == {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "private-prompt-0"}],
            "max_tokens": 128,
            "temperature": 0,
            "n": 1,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        output = json.dumps(result.to_dict())
        assert "SYNTHETIC_ONLY" in output
        for raw_value in (_OUTPUT, _MODEL, "synthetic-server-generated-id"):
            assert raw_value not in output

    asyncio.run(scenario())


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_stream_without_usage_keeps_token_count_provenance_unavailable(
    finish_reason: str,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        payload = _ROLE + _CONTENT + _choice(finish=finish_reason) + DONE
        transport = FakeTransport(clock, {0: Script(chunks=((0, payload),))})
        task = asyncio.create_task(run_evaluation(config([0]), transport, clock=clock))
        row = (await result_of(task, transport, clock)).records[0]
        assert row.outcome == "SUCCESS"
        assert row.finish_reason == finish_reason
        assert row.prompt_tokens is row.completion_tokens is None
        assert row.usage_provenance == "UNAVAILABLE"
        assert row.content_event_times_ns == (0,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tail, outcome",
    [
        (
            b'data: {"error":{"object":"error","message":"'
            + _ERROR.encode()
            + b'","type":"Internal Server Error","param":null,"code":500}}\n\n'
            + DONE,
            "STREAM_ERROR",
        ),
        (b"event: error\ndata: " + _ERROR.encode() + b"\n\n", "STREAM_ERROR"),
        (b"data: malformed " + _ERROR.encode() + b"\n\n", "STREAM_ERROR"),
        (b"", "INCOMPLETE_STREAM"),
        (_FINISH, "INCOMPLETE_STREAM"),
        (_FINISH + b"data: [DONE]\n", "INCOMPLETE_STREAM"),
        (_choice(finish="abort") + DONE, "STREAM_ERROR"),
        (_choice(finish="tool_calls") + DONE, "STREAM_ERROR"),
    ],
)
def test_partial_and_error_streams_retain_offered_population_and_diagnostics(
    tail: bytes, outcome: str,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(
            clock, {0: Script(chunks=((1, _ROLE + _CONTENT), (2, tail)), eof_ns=2)}
        )
        task = asyncio.create_task(run_evaluation(config([10]), transport, clock=clock))
        await settle()
        for elapsed in (10, 11, 12):
            await advance(clock, elapsed)
        result = await result_of(task, transport, clock)
        row = result.records[0]
        assert row.request_index == 0
        assert row.scheduled_ns == row.arrival_observed_ns == row.dispatch_ns == 10
        assert row.outcome == outcome
        assert row.attempts == 1
        assert row.first_content_ns == 11
        assert row.content_event_times_ns == (11,)
        assert row.protocol_done_ns is None
        assert row.terminal_ns == 12
        assert row.completion_tokens is None
        assert row.usage_provenance == "UNAVAILABLE"
        assert result.to_dict()["offered_count"] == 1
        assert result.to_dict()["outcomes"] == {outcome: 1}
        assert result.evidence_eligible is False
        assert _ERROR not in json.dumps(result.to_dict())

    asyncio.run(scenario())


@pytest.mark.parametrize("direct_cancel", [False, True])
def test_cancellation_preserves_inflight_queued_future_ids_and_closes_client(
    direct_cancel: bool,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        stop = asyncio.Event()
        transport = FakeTransport(
            clock, {0: Script(chunks=((1, _ROLE + _CONTENT),), eof_ns=100)}
        )
        task = asyncio.create_task(
            run_evaluation(
                config([0, 0, 20], concurrency=1, max_queue=1),
                transport,
                clock=clock,
                stop=stop,
            )
        )
        await settle()
        await advance(clock, 1)
        await advance(clock, 2)
        if direct_cancel:
            task.cancel()
        else:
            stop.set()
        result = await result_of(task, transport, clock)
        assert result.cancelled
        assert [r.request_index for r in result.records] == [0, 1, 2]
        assert [r.scheduled_ns for r in result.records] == [0, 0, 20]
        assert [r.arrival_observed_ns for r in result.records] == [0, 0, None]
        assert [r.outcome for r in result.records] == ["CANCELLED"] * 3
        assert [r.attempts for r in result.records] == [1, 0, 0]
        assert [r.terminal_ns for r in result.records] == [2, 2, 2]
        assert result.records[0].content_event_times_ns == (1,)
        assert result.records[0].completion_tokens is None
        assert result.to_dict()["offered_count"] == 3
        assert result.to_dict()["arrivals_observed_count"] == 2
        assert transport.cancelled == [0]
        # Client closure does not establish server-side KV release or cache reset.
        assert result.cleanup == "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"

    asyncio.run(scenario())


def test_response_model_echo_is_not_model_identity_verification() -> None:
    """A future preflight must reject mismatch before this unchanged replay seam."""
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {0: Script(chunks=((0, _SUCCESS),))})
        cfg = config([0])
        assert cfg.model != _MODEL
        task = asyncio.create_task(run_evaluation(cfg, transport, clock=clock))
        result = await result_of(task, transport, clock)
        assert result.records[0].outcome == "SUCCESS"
        assert json.loads(transport.bodies[0])["model"] == cfg.model
        assert result.endpoint_identity == "UNVERIFIED"
        assert result.evidence_eligible is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status, content_type, expected",
    [
        (200, "text/event-stream", "SUCCESS"),
        (404, "application/json", "HTTP_ERROR"),
        (503, "application/json", "HTTP_ERROR"),
        (200, "application/json", "STREAM_ERROR"),
    ],
)
def test_native_transport_posts_chat_endpoint_and_never_reads_http_error_bodies(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
    expected: str,
) -> None:
    """SYNTHETIC_ONLY aiohttp context managers: no socket or SGLang process."""
    class Response:
        def __init__(self) -> None:
            self.status = status
            self.content_type = content_type
            self.headers = {"Content-Encoding": "identity"}
            self.content = self
            self.read = False
            self.closed = False

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.close()

        def close(self) -> None:
            self.closed = True

        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            assert size == 16_384
            assert status == 200 and content_type == "text/event-stream"
            self.read = True
            yield _SUCCESS

    class Session:
        def __init__(self, **kwargs: object) -> None:
            # The real transport constructs a connector; this fake closes it too.
            self.connector = kwargs["connector"]
            self.closed = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post(self, url: str, **kwargs: object) -> Response:
            self.calls.append((url, kwargs))
            return response

        async def close(self) -> None:
            await self.connector.close()
            self.closed = True

    async def scenario() -> None:
        monkeypatch.setattr(
            "inferdrome.evaluation.transport.aiohttp.ClientSession", Session
        )
        cfg = config([0]).model_copy(update={"model": _MODEL})
        transport = AiohttpTransport(cfg)
        result = await run_evaluation(cfg, transport, clock=ManualClock())
        assert result.records[0].outcome == expected
        assert result.records[0].http_status == status
        assert result.records[0].request_index == 0
        assert result.to_dict()["offered_count"] == 1
        assert transport.closed and response.closed
        assert response.read is (expected == "SUCCESS")
        [(url, arguments)] = transport._session.calls
        assert url == "http://127.0.0.1:8001/v1/chat/completions"
        assert arguments["allow_redirects"] is False
        assert arguments["headers"] == {"Content-Type": "application/json"}
        assert json.loads(arguments["data"])["model"] == _MODEL
        if expected == "HTTP_ERROR":
            assert result.records[0].first_body_byte_ns is None
            assert result.records[0].usage_provenance == "UNAVAILABLE"

    response = Response()
    asyncio.run(scenario())
