"""Bounded fixed-arrival replay with a complete terminal population.

One coordinator owns admission and terminalization. There is no task/semaphore
per future offer, no retry and no hidden unbounded waiting queue.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from inferdrome.evaluation.contracts import (
    EndpointId,
    EvaluationConfig,
    EvaluationError,
    Outcome,
)
from inferdrome.evaluation.stream import (
    IncompleteStream,
    StreamError,
    StreamLimitError,
    StreamParser,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest


class Clock(Protocol):
    def now_ns(self) -> int: ...

    async def sleep_until(self, absolute_ns: int) -> None: ...


class SystemClock:
    def now_ns(self) -> int:
        return time.monotonic_ns()

    async def sleep_until(self, absolute_ns: int) -> None:
        await asyncio.sleep(max(0, absolute_ns - self.now_ns()) / 1_000_000_000)


class TransportError(OSError):
    """Sanitized transport failure; no raw exception text is retained."""


class Transport(Protocol):
    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass
class Measurement:
    """In-memory progress survives deadline/cancellation for partial diagnostics."""

    parser: StreamParser
    arrival_observed_ns: int | None = None
    endpoint_id: EndpointId | None = None
    dispatch_ns: int | None = None
    response_headers_ns: int | None = None
    http_status: int | None = None
    protocol_done_ns: int | None = None
    terminal_ns: int | None = None
    outcome: Outcome | None = None


@dataclass(frozen=True)
class RequestRecord:
    request_index: int
    scheduled_ns: int
    arrival_observed_ns: int | None
    endpoint_id: EndpointId | None
    dispatch_ns: int | None
    dispatch_lag_ns: int | None
    response_headers_ns: int | None
    first_body_byte_ns: int | None
    first_content_ns: int | None
    content_event_times_ns: tuple[int, ...]
    protocol_done_ns: int | None
    terminal_ns: int
    outcome: Outcome
    attempts: int
    http_status: int | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    usage_provenance: str


@dataclass(frozen=True)
class EvaluationResult:
    config_sha256: str
    declared_source_commit: str
    model_sha256: str
    bounds: dict[str, object]
    request_settings: dict[str, object]
    records: tuple[RequestRecord, ...]
    elapsed_ns: int
    peak_active: int
    peak_queue: int
    cancelled: bool
    routing: str
    schema_version: str = "inferdrome.evaluation-result.v1"
    evidence_class: str = "LOCAL_MEASUREMENT_ONLY"
    evidence_eligible: bool = False
    endpoint_identity: str = "UNVERIFIED"
    exact_token_timing: str = "UNAVAILABLE"
    wire_first_response_byte: str = "UNAVAILABLE"
    cleanup: str = "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["outcomes"] = dict(Counter(row.outcome for row in self.records))
        result["offered_count"] = len(self.records)
        result["arrivals_observed_count"] = sum(
            row.arrival_observed_ns is not None for row in self.records
        )
        result["dispatched_count"] = sum(row.attempts for row in self.records)
        return result


RouteSelector = Callable[[int, int], EndpointId | None]


@dataclass
class _Replay:
    config: EvaluationConfig
    transport: Transport
    clock: Clock
    stop: asyncio.Event
    selector: RouteSelector | None
    start_ns: int = 0
    stop_at_ns: int | None = None
    measurements: list[Measurement] = field(default_factory=list)
    active: dict[asyncio.Task[None], int] = field(default_factory=dict)
    pending: deque[int] = field(default_factory=deque)
    cursor: int = 0
    peak_active: int = 0
    peak_queue: int = 0
    cancelled: bool = False

    def now(self) -> int:
        return self.clock.now_ns() - self.start_ns

    def terminal(
        self,
        index: int,
        outcome: Outcome,
        *,
        observed_ns: int | None = None,
    ) -> None:
        measurement = self.measurements[index]
        if measurement.terminal_ns is not None:
            raise EvaluationError("duplicate evaluation terminal")
        observed = self.now() if observed_ns is None else observed_ns
        cutoff = self.cutoff_outcome(index, observed)
        if cutoff == "CANCELLED":
            self.cancelled = True
        measurement.outcome = cutoff or outcome
        measurement.terminal_ns = observed

    def cutoff_outcome(self, index: int, observed: int) -> Outcome | None:
        if (
            self.cancelled
            or self.stop.is_set()
            or (self.stop_at_ns is not None and observed >= self.stop_at_ns)
        ):
            return "CANCELLED"
        end = self.config.bounds.duration_ns + self.config.bounds.drain_ns
        deadline = self.deadline(index)
        # Earliest cutoff wins; a request deadline wins an exact tie. Sample
        # only once so SUCCESS cannot carry a terminal time past its deadline.
        if observed >= deadline and deadline <= end:
            return "TIMEOUT"
        if observed >= end:
            return "DRAIN_TIMEOUT"
        return None

    def deadline(self, index: int) -> int:
        return (
            self.config.offers[index].scheduled_ns
            + self.config.bounds.request_timeout_ns
        )

    async def request(self, index: int) -> None:
        measurement = self.measurements[index]
        offer = self.config.offers[index]
        if self.now() >= self.deadline(index):
            self.terminal(index, "TIMEOUT")
            return
        selected = (
            offer.endpoint_id
            if self.selector is None
            else self.selector(index, self.now())
        )
        if selected is None:
            self.terminal(index, "REJECTED_ROUTE")
            return
        if selected not in {"endpoint-a", "endpoint-b"}:
            self.terminal(index, "INTERNAL_ERROR")
            return
        measurement.endpoint_id = selected
        origin = self.config.endpoints[0 if selected == "endpoint-a" else 1].origin
        body = canonical_json_bytes(
            {
                "model": self.config.model,
                "messages": [{"role": "user", "content": offer.prompt}],
                "max_tokens": self.config.max_tokens,
                "temperature": 0,
                "n": 1,
                "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {
                    "enable_thinking": self.config.enable_thinking
                },
            }
        )

        def headers(status: int) -> None:
            measurement.http_status = status
            measurement.response_headers_ns = self.now()

        def body_bytes(data: bytes) -> None:
            observed = self.now()
            measurement.parser.feed(data, observed)
            if measurement.parser.done and measurement.protocol_done_ns is None:
                measurement.protocol_done_ns = observed

        # Timestamp immediately before invoking the HTTP client, including any
        # connection establishment/writing work; not a kernel wire-send time.
        dispatch = self.now()
        cutoff = self.cutoff_outcome(index, dispatch)
        if cutoff is not None:
            self.terminal(index, cutoff, observed_ns=dispatch)
            return
        measurement.dispatch_ns = dispatch
        try:
            await self.transport.stream(origin, body, headers, body_bytes)
            if measurement.http_status != 200:
                self.terminal(index, "HTTP_ERROR")
            else:
                measurement.parser.finish()
                self.terminal(index, "SUCCESS")
        except StreamLimitError:
            self.terminal(index, "STREAM_LIMIT")
        except IncompleteStream:
            self.terminal(index, "INCOMPLETE_STREAM")
        except StreamError:
            self.terminal(index, "STREAM_ERROR")
        except TransportError:
            self.terminal(index, "TRANSPORT_ERROR")

    async def cancel_tasks(self, tasks: list[asyncio.Task[None]]) -> None:
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        _, unfinished = await asyncio.wait(
            tasks,
            timeout=self.config.bounds.cleanup_timeout_ns / 1_000_000_000,
        )
        if unfinished:
            raise EvaluationError("evaluation client cleanup exceeded its bound")
        for task in tasks:
            if not task.cancelled():
                task.exception()  # Consume sanitized internal failures.

    def harvest(self) -> None:
        for task, index in list(self.active.items()):
            if not task.done():
                continue
            del self.active[task]
            if (task.cancelled() or task.exception() is not None) and (
                self.measurements[index].terminal_ns is None
            ):
                self.terminal(index, "INTERNAL_ERROR")

    def launch(self, index: int) -> None:
        task = asyncio.create_task(self.request(index))
        self.active[task] = index
        self.peak_active = max(self.peak_active, len(self.active))

    async def execute(self) -> None:
        bounds = self.config.bounds
        end_ns = bounds.duration_ns + bounds.drain_ns
        if self.stop_at_ns is not None:
            end_ns = min(end_ns, self.stop_at_ns)
        stop_waiter = asyncio.create_task(self.stop.wait())
        try:
            while self.cursor < len(self.measurements) or self.active or self.pending:
                self.harvest()
                if self.stop.is_set() or self.now() >= end_ns:
                    self.cancelled = self.stop.is_set() or (
                        self.stop_at_ns is not None and self.now() >= self.stop_at_ns
                    )
                    break

                expired = [
                    task
                    for task, i in self.active.items()
                    if self.now() >= self.deadline(i)
                ]
                await self.cancel_tasks(expired)
                for task in expired:
                    index = self.active.pop(task)
                    if self.measurements[index].terminal_ns is None:
                        self.terminal(index, "TIMEOUT")

                while self.pending:
                    index = self.pending[0]
                    if self.now() >= self.deadline(index):
                        self.pending.popleft()
                        self.terminal(index, "TIMEOUT")
                    elif len(self.active) < bounds.concurrency:
                        self.launch(self.pending.popleft())
                    else:
                        break

                while (
                    self.cursor < len(self.measurements)
                    and self.config.offers[self.cursor].scheduled_ns <= self.now()
                ):
                    index = self.cursor
                    self.cursor += 1
                    self.measurements[index].arrival_observed_ns = self.now()
                    if self.now() >= self.deadline(index):
                        self.terminal(index, "TIMEOUT")
                    elif len(self.active) < bounds.concurrency and not self.pending:
                        self.launch(index)
                    elif len(self.pending) < bounds.max_queue:
                        self.pending.append(index)
                        self.peak_queue = max(self.peak_queue, len(self.pending))
                    else:
                        self.terminal(index, "REJECTED_CAPACITY")

                if self.cursor == len(self.measurements) and not (
                    self.active or self.pending
                ):
                    break
                wake = [end_ns]
                if self.cursor < len(self.measurements):
                    wake.append(self.config.offers[self.cursor].scheduled_ns)
                wake.extend(self.deadline(i) for i in self.active.values())
                if self.pending:
                    wake.append(self.deadline(self.pending[0]))
                timer = asyncio.create_task(
                    self.clock.sleep_until(self.start_ns + min(wake))
                )
                try:
                    await asyncio.wait(
                        [*self.active, timer, stop_waiter],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
        except asyncio.CancelledError:
            # Deliberate API contract: first cancellation requests a complete
            # cancelled population. A second interrupt may abort publication.
            self.cancelled = True
        finally:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
            await self.cancel_tasks(list(self.active))
            self.active.clear()
            for index, measurement in enumerate(self.measurements):
                if measurement.terminal_ns is None:
                    self.terminal(
                        index, "CANCELLED" if self.cancelled else "DRAIN_TIMEOUT"
                    )

    def records(self) -> tuple[RequestRecord, ...]:
        result: list[RequestRecord] = []
        for index, measurement in enumerate(self.measurements):
            parser = measurement.parser
            assert measurement.terminal_ns is not None
            assert measurement.outcome is not None
            scheduled = self.config.offers[index].scheduled_ns
            result.append(
                RequestRecord(
                    request_index=index,
                    scheduled_ns=scheduled,
                    arrival_observed_ns=measurement.arrival_observed_ns,
                    endpoint_id=measurement.endpoint_id,
                    dispatch_ns=measurement.dispatch_ns,
                    dispatch_lag_ns=(
                        None
                        if measurement.dispatch_ns is None
                        else measurement.dispatch_ns - scheduled
                    ),
                    response_headers_ns=measurement.response_headers_ns,
                    first_body_byte_ns=parser.first_body_byte_ns,
                    first_content_ns=parser.first_content_ns,
                    content_event_times_ns=tuple(parser.content_event_times_ns),
                    protocol_done_ns=measurement.protocol_done_ns,
                    terminal_ns=measurement.terminal_ns,
                    outcome=measurement.outcome,
                    attempts=int(measurement.dispatch_ns is not None),
                    http_status=measurement.http_status,
                    finish_reason=parser.finish_reason,
                    prompt_tokens=parser.prompt_tokens,
                    completion_tokens=parser.completion_tokens,
                    usage_provenance=(
                        "SERVER_REPORTED_STREAM_USAGE"
                        if parser.completion_tokens is not None
                        else "UNAVAILABLE"
                    ),
                )
            )
        return tuple(result)


async def run_evaluation(
    config: EvaluationConfig,
    transport: Transport,
    *,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
    selector: RouteSelector | None = None,
    start_ns: int | None = None,
    stop_at_ns: int | None = None,
) -> EvaluationResult:
    """Take ownership of a cancellable transport and close it before returning.

    The injected clock must be monotonic and its sleep must be cancellable.
    An optional shared start_ns preserves the recipe epoch across replays started
    after warm-up; it must be a nonnegative integer no later than the current clock.
    An optional trial-relative stop_at_ns cancels this population at that boundary,
    including a final pre-dispatch check, independently of controller task ordering.
    Injectable transports/selectors are trusted code, never configuration input.
    Cleanup uses a real event-loop wall-time guard even with a simulated clock.
    """
    replay = _Replay(
        config, transport, clock or SystemClock(), stop or asyncio.Event(), selector
    )
    replay.measurements = [
        Measurement(
            StreamParser(
                max_stream_bytes=config.bounds.max_stream_bytes,
                max_event_bytes=config.bounds.max_event_bytes,
                max_content_events=config.bounds.max_content_events,
            )
        )
        for _ in config.offers
    ]
    try:
        observed_start_ns = replay.clock.now_ns()
        if start_ns is None:
            replay.start_ns = observed_start_ns
        else:
            if (
                isinstance(start_ns, bool)
                or not isinstance(start_ns, int)
                or not 0 <= start_ns <= observed_start_ns
            ):
                raise EvaluationError("evaluation start epoch violates its contract")
            replay.start_ns = start_ns
        if stop_at_ns is not None:
            if (
                type(stop_at_ns) is not int
                or not 0
                <= stop_at_ns
                <= config.bounds.duration_ns + config.bounds.drain_ns
            ):
                raise EvaluationError("evaluation stop boundary violates its contract")
            replay.stop_at_ns = stop_at_ns
        await replay.execute()
    finally:
        close = asyncio.create_task(transport.close())
        cleanup_seconds = config.bounds.cleanup_timeout_ns / 1_000_000_000
        loop = asyncio.get_running_loop()
        close_deadline = loop.time() + cleanup_seconds
        while True:
            try:
                _, unfinished = await asyncio.wait(
                    [close],
                    timeout=max(0, close_deadline - loop.time()),
                )
                break
            except asyncio.CancelledError:
                # Keep ownership of close through a caller interrupt. Repeated
                # signals cannot extend the original real-time cleanup bound.
                replay.cancelled = True
        if unfinished:
            close.cancel()
            cancel_deadline = loop.time() + cleanup_seconds
            while True:
                try:
                    await asyncio.wait(
                        [close],
                        timeout=max(0, cancel_deadline - loop.time()),
                    )
                    break
                except asyncio.CancelledError:
                    replay.cancelled = True
            if close.done() and not close.cancelled():
                close.exception()
            raise EvaluationError("evaluation transport cleanup exceeded its bound")
        if close.cancelled() or close.exception() is not None:
            raise EvaluationError("evaluation transport cleanup failed")
    return EvaluationResult(
        config_sha256=sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
        declared_source_commit=config.source_commit,
        model_sha256=sha256_digest(config.model.encode("utf-8")),
        bounds=config.bounds.model_dump(),
        request_settings={
            "max_tokens": config.max_tokens,
            "temperature": 0,
            "enable_thinking": config.enable_thinking,
            "n": 1,
            "stream": True,
            "include_usage": True,
        },
        records=replay.records(),
        elapsed_ns=replay.now(),
        peak_active=replay.peak_active,
        peak_queue=replay.peak_queue,
        cancelled=replay.cancelled or replay.stop.is_set(),
        routing="FIXED_ASSIGNMENT" if selector is None else "INJECTED",
    )
