"""Bounded real-endpoint execution bridge for routing-execution-v1."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.qwen3_campaign import qwen3_workload_prompts
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    input_transfer_digest,
    sha256_digest,
)
from inferdrome.routing_execution.contracts import (
    CandidateState,
    EndpointId,
    ExecutedManifest,
    FaultReceipt,
    InputTransferReceipt,
    ProducerReceipt,
    ResetReceipt,
    RouteDecisionReceipt,
    RoutingExecutionConfig,
    TelemetryObservation,
    TerminalOutcomeReceipt,
    TerminalStatus,
    TrialSummary,
    fixed_policy_ids,
    fixed_request_ids,
)
from inferdrome.routing_execution.package import (
    EvidenceReservation,
    ExecutionPackageError,
    SealedPackage,
    make_integrity_manifest,
)
from inferdrome.routing_execution.policy import decide
from inferdrome.routing_execution.telemetry import (
    TelemetrySample,
    observation,
    sample_health,
    sample_metrics,
    unavailable_sample,
)
from inferdrome.routing_execution.topology import (
    AdmittedEndpoint,
    AdmittedTopology,
    TopologyAdmissionError,
    admit_topology,
)
from inferdrome.routing_execution.transport import (
    EndpointTransport,
    TransportCancelled,
    TransportError,
    TransportTimedOut,
    UrllibEndpointTransport,
)

_MAX_CONFIG_BYTES = 1_048_576
_MAX_WORKLOAD_BYTES = 1_048_576
_MAX_PROMPT_BYTES = 32_768


class ExecutionError(ValueError):
    """A bounded routing execution could not safely produce evidence."""


class MonotonicClock(Protocol):
    """Injectable clock with an explicit pacing seam for deterministic tests."""

    def now_ns(self) -> int: ...

    def sleep_ms(self, milliseconds: int) -> None: ...


class SystemMonotonicClock:
    """Runner-local monotonic clock used by the command-line execution path."""

    def now_ns(self) -> int:
        return time.monotonic_ns()

    def sleep_ms(self, milliseconds: int) -> None:
        time.sleep(milliseconds / 1000)


@dataclass
class ManualMonotonicClock:
    """A deterministic, monotonic clock used only by local harnesses/tests."""

    value_ns: int = 0

    def now_ns(self) -> int:
        return self.value_ns

    def sleep_ms(self, milliseconds: int) -> None:
        self.value_ns += milliseconds * 1_000_000


@dataclass(frozen=True)
class WorkloadRequest:
    """Raw prompt held in memory only until one OpenAI request is sent."""

    request_id: str
    sequence_index: int
    prompt: str


@dataclass(frozen=True)
class ExecutionRecords:
    reset_receipts: tuple[ResetReceipt, ...]
    fault_receipts: tuple[FaultReceipt, ...]
    telemetry_observations: tuple[TelemetryObservation, ...]
    route_decisions: tuple[RouteDecisionReceipt, ...]
    terminal_outcomes: tuple[TerminalOutcomeReceipt, ...]
    trial_summaries: tuple[TrialSummary, ...]


@dataclass(frozen=True)
class SealedRoutingExecution:
    """The only successful run result: a sealed package identity."""

    path: Path
    retained_digest: str


def _read_source(path: Path, *, label: str, maximum: int) -> bytes:
    """Read one bounded source through a held no-follow parent descriptor."""

    selected = path.absolute()
    parent: SafeDirFD | None = None
    descriptor: int | None = None
    try:
        parent = SafeDirFD.open(selected.parent)
        descriptor = parent.open_child(selected.name, os.O_RDONLY)
        named_before = parent.validated_regular_child(
            selected.name, descriptor=descriptor
        )
        if named_before.st_size < 1 or named_before.st_size > maximum:
            raise ExecutionError(f"{label} is unsafe")
        chunks: list[bytes] = []
        remaining = named_before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise ExecutionError(f"{label} was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ExecutionError(f"{label} grew during read")
        parent.validated_regular_child(selected.name, descriptor=descriptor)
        return b"".join(chunks)
    except ExecutionError:
        raise
    except (OSError, SafeDirFSError):
        raise ExecutionError(f"{label} is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            parent.close()


def _strict_json(content: bytes, *, label: str) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ExecutionError(f"{label} is not UTF-8") from None

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ExecutionError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise ExecutionError(f"{label} has a non-finite number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except ExecutionError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise ExecutionError(f"{label} is not valid JSON") from None


def load_config(path: Path) -> tuple[RoutingExecutionConfig, bytes]:
    """Load canonical, source-only execution configuration without a transport."""

    content = _read_source(
        path, label="execution configuration", maximum=_MAX_CONFIG_BYTES
    )
    _strict_json(content, label="execution configuration")
    try:
        config = RoutingExecutionConfig.model_validate_json(content)
    except ValidationError:
        raise ExecutionError("execution configuration violates its contract") from None
    if canonical_json_bytes(config.model_dump(mode="json")) != content:
        raise ExecutionError("execution configuration is not canonical JSON")
    return config, content


def load_workload(
    path: Path, *, expected_sha256: str
) -> tuple[tuple[WorkloadRequest, ...], bytes]:
    """Read six canonical prompt rows, retain them in memory, and bind their hash."""

    content = _read_source(
        path, label="execution workload", maximum=_MAX_WORKLOAD_BYTES
    )
    if sha256_digest(content) != expected_sha256:
        raise ExecutionError("execution workload digest disagrees")
    if not content.endswith(b"\n"):
        raise ExecutionError("execution workload must end with one newline")
    rows = content[:-1].split(b"\n")
    if len(rows) != 6 or any(not row for row in rows):
        raise ExecutionError("execution workload must contain six rows")
    requests: list[WorkloadRequest] = []
    for sequence_index, row in enumerate(rows):
        value = _strict_json(row, label="execution workload row")
        if (
            not isinstance(value, dict)
            or set(value) != {"prompt"}
            or not isinstance(value["prompt"], str)
            or not 1 <= len(value["prompt"].encode("utf-8")) <= _MAX_PROMPT_BYTES
            or canonical_json_bytes(value) != row
            or value["prompt"] != qwen3_workload_prompts()[sequence_index]
        ):
            raise ExecutionError("execution workload row is invalid")
        requests.append(
            WorkloadRequest(
                request_id=fixed_request_ids()[sequence_index],
                sequence_index=sequence_index,
                prompt=value["prompt"],
            )
        )
    return tuple(requests), content


def declared_input_transfer_digest(config: RoutingExecutionConfig) -> str:
    """Calculate the non-self-referential configuration transfer declaration."""

    projection = config.model_dump(mode="json")
    destination = dict(projection["evidence_destination"])
    destination["declared_input_transfer_sha256"] = "sha256:" + ("0" * 64)
    projection["evidence_destination"] = destination
    return input_transfer_digest(projection, config.workload.selected_workload_sha256)


def _trial_id(policy_id: str) -> str:
    return {
        "fail_closed_required_load_v1": "trial-fail-closed-v1",
        "explicit_fail_open_stale_load_v1": "trial-fail-open-v1",
        "typed_admissible_state_only_v1": "trial-typed-v1",
    }[policy_id]


def _model_ready(
    transport: EndpointTransport, endpoint: AdmittedEndpoint, *, timeout_ms: int
) -> bool:
    """Confirm OpenAI compatibility without retaining its response body."""

    try:
        response = transport.get(
            endpoint.canonical_origin, "/v1/models", timeout_ms=timeout_ms
        )
        value = _strict_json(response.body, label="endpoint model response")
        if response.status != 200 or not isinstance(value, dict):
            return False
        data = value.get("data")
        return isinstance(data, list) and any(
            isinstance(row, dict) and row.get("id") == "Qwen/Qwen3-8B" for row in data
        )
    except (ExecutionError, TransportError):
        return False


def _request_body(config: RoutingExecutionConfig, request: WorkloadRequest) -> bytes:
    """Construct the only raw prompt-containing artifact; never serialize it later."""

    return canonical_json_bytes(
        {
            "max_tokens": 128,
            "messages": [{"content": request.prompt, "role": "user"}],
            "model": config.model.model_id,
            "stream": False,
        }
    )


def _valid_completion_response(content: bytes) -> bool:
    """Accept only the tiny OpenAI-compatible completion shape this bridge needs."""

    try:
        value = _strict_json(content, label="endpoint completion response")
    except ExecutionError:
        return False
    return isinstance(value, dict) and isinstance(value.get("choices"), list)


def _terminal(
    *,
    transport: EndpointTransport,
    endpoint: AdmittedEndpoint | None,
    request: WorkloadRequest,
    decision: RouteDecisionReceipt,
    config: RoutingExecutionConfig,
    clock: MonotonicClock,
    cancel_requested: Callable[[], bool],
) -> TerminalOutcomeReceipt:
    started = clock.now_ns()
    if endpoint is None:
        return TerminalOutcomeReceipt(
            schema_version="inferdrome.routing-execution-terminal-receipt.v1",
            trial_id=decision.trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            terminal_outcome_id=decision.terminal_outcome_id,
            decision_id=decision.decision_id,
            selected_endpoint_id=None,
            status="NO_SAFE_ROUTE",
            started_at_monotonic_ns=started,
            ended_at_monotonic_ns=clock.now_ns(),
            reason=decision.fallback_reason,
            request_sha256=None,
            response_sha256=None,
            http_status=None,
            attempt_count=0,
        )
    if cancel_requested():
        return TerminalOutcomeReceipt(
            schema_version="inferdrome.routing-execution-terminal-receipt.v1",
            trial_id=decision.trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            terminal_outcome_id=decision.terminal_outcome_id,
            decision_id=decision.decision_id,
            selected_endpoint_id=endpoint.endpoint_id,
            status="CANCELLED",
            started_at_monotonic_ns=started,
            ended_at_monotonic_ns=clock.now_ns(),
            reason="CANCELLATION_REQUESTED",
            request_sha256=None,
            response_sha256=None,
            http_status=None,
            attempt_count=0,
        )
    body = _request_body(config, request)
    request_hash = sha256_digest(body)
    try:
        response = transport.post_json(
            endpoint.canonical_origin,
            "/v1/chat/completions",
            body,
            timeout_ms=config.request_timeout_ms,
        )
        if 200 <= response.status <= 299 and _valid_completion_response(response.body):
            status: TerminalStatus = "SUCCEEDED"
            reason = "HTTP_2XX"
        elif 200 <= response.status <= 299:
            status = "FAILED"
            reason = "MALFORMED_RESPONSE"
        else:
            status = "FAILED"
            reason = "HTTP_NON_2XX"
        response_hash: str | None = sha256_digest(response.body)
        http_status: int | None = response.status
    except TransportTimedOut:
        status, reason, response_hash, http_status = (
            "TIMED_OUT",
            "TRANSPORT_TIMEOUT",
            None,
            None,
        )
    except TransportCancelled:
        status, reason, response_hash, http_status = (
            "CANCELLED",
            "TRANSPORT_CANCELLED",
            None,
            None,
        )
    except TransportError:
        status, reason, response_hash, http_status = (
            "FAILED",
            "TRANSPORT_FAILED",
            None,
            None,
        )
    return TerminalOutcomeReceipt(
        schema_version="inferdrome.routing-execution-terminal-receipt.v1",
        trial_id=decision.trial_id,
        request_id=request.request_id,
        sequence_index=request.sequence_index,
        terminal_outcome_id=decision.terminal_outcome_id,
        decision_id=decision.decision_id,
        selected_endpoint_id=endpoint.endpoint_id,
        status=status,
        started_at_monotonic_ns=started,
        ended_at_monotonic_ns=clock.now_ns(),
        reason=reason,
        request_sha256=request_hash,
        response_sha256=response_hash,
        http_status=http_status,
        attempt_count=1,
    )


def _candidate_states(
    *,
    topology: AdmittedTopology,
    trial_id: str,
    request: WorkloadRequest,
    transport: EndpointTransport,
    model_ready: Mapping[EndpointId, bool],
    health_epochs: dict[str, int],
    load_epochs: dict[str, int],
    latest_load: dict[str, TelemetrySample],
    clock: MonotonicClock,
    policy_id: str,
) -> tuple[tuple[CandidateState, CandidateState], tuple[TelemetryObservation, ...]]:
    """Independently sample health and load, then materialize one receipt state."""

    config = topology.config
    now = clock.now_ns()
    fresh_load = (
        request.sequence_index
        <= config.fault.load_collection_pause_after_sequence_index
    )
    raw_health: dict[str, TelemetrySample] = {}
    raw_load: dict[str, TelemetrySample] = {}
    raw_gpu: dict[str, TelemetrySample] = {}
    for declaration, admitted_endpoint in zip(
        config.endpoints, topology.endpoints, strict=True
    ):
        health_epochs[declaration.endpoint_id] += 1
        sampled_health = sample_health(
            transport,
            admitted_endpoint.canonical_origin,
            now_ns=clock.now_ns(),
            epoch=health_epochs[declaration.endpoint_id],
            timeout_ms=config.request_timeout_ms,
        )
        if not model_ready[declaration.endpoint_id]:
            sampled_health = TelemetrySample(
                sampled_at_monotonic_ns=sampled_health.sampled_at_monotonic_ns,
                epoch=sampled_health.epoch,
                state="UNAVAILABLE",
                value="UNAVAILABLE",
                source="HTTP_HEALTH",
                payload_sha256=sampled_health.payload_sha256,
            )
        raw_health[declaration.endpoint_id] = sampled_health
        if fresh_load:
            load_epochs[declaration.endpoint_id] += 1
            sampled_load, _ = sample_metrics(
                transport,
                admitted_endpoint.canonical_origin,
                now_ns=clock.now_ns(),
                epoch=load_epochs[declaration.endpoint_id],
                timeout_ms=config.request_timeout_ms,
                metric_name=config.telemetry.load_metric_name,
            )
            latest_load[declaration.endpoint_id] = sampled_load
        raw_load[declaration.endpoint_id] = latest_load.get(
            declaration.endpoint_id, unavailable_sample(now_ns=now)
        )
        # PR B does not claim a separately bound DCGM source. Record the
        # typed unavailable state rather than relabeling vLLM metrics as GPU.
        raw_gpu[declaration.endpoint_id] = unavailable_sample(now_ns=clock.now_ns())
    decision_ns = clock.now_ns()
    candidates: list[CandidateState] = []
    records: list[TelemetryObservation] = []
    for admitted_endpoint in topology.endpoints:
        endpoint_id = admitted_endpoint.endpoint_id
        health = observation(
            trial_id=trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            endpoint_id=endpoint_id,
            signal="HEALTH",
            observer_id="health-observer-v1",
            sample=raw_health[endpoint_id],
            decision_ns=decision_ns,
            freshness_bound_ns=config.telemetry.health_freshness_ms * 1_000_000,
        )
        load = observation(
            trial_id=trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            endpoint_id=endpoint_id,
            signal="LOAD",
            observer_id="load-observer-v1",
            sample=raw_load[endpoint_id],
            decision_ns=decision_ns,
            freshness_bound_ns=config.telemetry.load_freshness_ms * 1_000_000,
        )
        gpu = observation(
            trial_id=trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            endpoint_id=endpoint_id,
            signal="GPU_DCGM",
            observer_id="gpu-dcgm-observer-v1",
            sample=raw_gpu[endpoint_id],
            decision_ns=decision_ns,
            freshness_bound_ns=config.telemetry.gpu_freshness_ms * 1_000_000,
        )
        kv = observation(
            trial_id=trial_id,
            request_id=request.request_id,
            sequence_index=request.sequence_index,
            endpoint_id=endpoint_id,
            signal="KV_CACHE",
            observer_id="kv-cache-observer-v1",
            sample=unavailable_sample(now_ns=decision_ns),
            decision_ns=decision_ns,
            freshness_bound_ns=0,
        )
        candidates.append(
            CandidateState(
                endpoint_id=endpoint_id,
                health=health,
                load=load,
                gpu_dcgm=gpu,
                kv_cache=kv,
                eligible=(
                    health.admissibility == "ADMISSIBLE"
                    and load.state != "UNAVAILABLE"
                    and (
                        policy_id != "fail_closed_required_load_v1"
                        or load.admissibility == "ADMISSIBLE"
                    )
                ),
            )
        )
        records.extend((health, load, gpu, kv))
    first, second = candidates
    return (first, second), tuple(records)


def execute(
    topology: AdmittedTopology,
    workload: Sequence[WorkloadRequest],
    *,
    transport_factory: Callable[[], EndpointTransport] = UrllibEndpointTransport,
    clock: MonotonicClock | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> ExecutionRecords:
    """Run three fresh client/telemetry trials after topology admission only."""

    if (
        len(workload) != 6
        or tuple(row.request_id for row in workload) != fixed_request_ids()
    ):
        raise ExecutionError("execution workload is not the fixed request trace")
    selected_clock = clock or SystemMonotonicClock()
    cancelled = cancel_requested or (lambda: False)
    resets: list[ResetReceipt] = []
    faults: list[FaultReceipt] = []
    telemetry: list[TelemetryObservation] = []
    decisions: list[RouteDecisionReceipt] = []
    terminals: list[TerminalOutcomeReceipt] = []
    summaries: list[TrialSummary] = []
    config = topology.config
    endpoint_by_id = {endpoint.endpoint_id: endpoint for endpoint in topology.endpoints}
    for policy_id in fixed_policy_ids():
        trial_id = _trial_id(policy_id)
        health_epochs = {"endpoint-a": 0, "endpoint-b": 0}
        load_epochs = {"endpoint-a": 0, "endpoint-b": 0}
        latest_load: dict[str, TelemetrySample] = {}
        resets.append(
            ResetReceipt(
                schema_version="inferdrome.routing-execution-reset-receipt.v1",
                trial_id=trial_id,
                policy_id=policy_id,
                reset_at_monotonic_ns=selected_clock.now_ns(),
                observer_epochs={"HEALTH": 0, "LOAD": 0, "GPU_DCGM": 0, "KV_CACHE": 0},
                runner_connection_state_cleared=True,
                runner_telemetry_state_cleared=True,
                endpoint_runtime_identities=tuple(
                    endpoint.published_identity for endpoint in topology.endpoints
                ),
                endpoint_engine_reset_assertion="NOT_ASSERTED_SEPARATE_SERVING_ENGINE",
            )
        )
        transport = transport_factory()
        try:
            ready = {
                endpoint.endpoint_id: _model_ready(
                    transport, endpoint, timeout_ms=config.request_timeout_ms
                )
                for endpoint in topology.endpoints
            }
            for request in workload:
                if request.sequence_index:
                    selected_clock.sleep_ms(config.fault.inter_request_interval_ms)
                if request.sequence_index == 2:
                    faults.append(
                        FaultReceipt(
                            schema_version="inferdrome.routing-execution-fault-receipt.v1",
                            trial_id=trial_id,
                            fault_id="stale-load-fresh-health-v1",
                            activated_at_sequence_index=2,
                            activated_at_monotonic_ns=selected_clock.now_ns(),
                            load_collection_paused=True,
                            health_collection_continues=True,
                        )
                    )
                candidates, rows = _candidate_states(
                    topology=topology,
                    trial_id=trial_id,
                    request=request,
                    transport=transport,
                    model_ready=ready,
                    health_epochs=health_epochs,
                    load_epochs=load_epochs,
                    latest_load=latest_load,
                    clock=selected_clock,
                    policy_id=policy_id,
                )
                policy = decide(policy_id, candidates)
                trial_token = trial_id.removeprefix("trial-").removesuffix("-v1")
                decision = RouteDecisionReceipt(
                    schema_version="inferdrome.routing-execution-decision-receipt.v1",
                    trial_id=trial_id,
                    policy_id=policy_id,
                    request_id=request.request_id,
                    sequence_index=request.sequence_index,
                    decision_id=f"decision-{trial_token}-{request.sequence_index:03d}",
                    decision_at_monotonic_ns=candidates[
                        0
                    ].health.decision_at_monotonic_ns,
                    candidates=candidates,
                    selected_endpoint_id=policy.selected_endpoint_id,
                    claims_used=policy.claims_used,
                    claims_permitted_stale=policy.claims_permitted_stale,
                    claims_discarded=policy.claims_discarded,
                    fallback_reason=policy.fallback_reason,
                    terminal_outcome_id=(
                        f"terminal-{trial_token}-{request.sequence_index:03d}"
                    ),
                )
                terminal = _terminal(
                    transport=transport,
                    endpoint=(
                        endpoint_by_id[policy.selected_endpoint_id]
                        if policy.selected_endpoint_id is not None
                        else None
                    ),
                    request=request,
                    decision=decision,
                    config=config,
                    clock=selected_clock,
                    cancel_requested=cancelled,
                )
                telemetry.extend(rows)
                decisions.append(decision)
                terminals.append(terminal)
        finally:
            transport.close()
        population = Counter(
            terminal.status for terminal in terminals if terminal.trial_id == trial_id
        )
        summaries.append(
            TrialSummary(
                schema_version="inferdrome.routing-execution-trial-summary.v1",
                trial_id=trial_id,
                policy_id=policy_id,
                request_denominator=6,
                terminal_population={
                    "SUCCEEDED": population["SUCCEEDED"],
                    "TIMED_OUT": population["TIMED_OUT"],
                    "FAILED": population["FAILED"],
                    "CANCELLED": population["CANCELLED"],
                    "NO_SAFE_ROUTE": population["NO_SAFE_ROUTE"],
                },
            )
        )
    return ExecutionRecords(
        reset_receipts=tuple(resets),
        fault_receipts=tuple(faults),
        telemetry_observations=tuple(telemetry),
        route_decisions=tuple(decisions),
        terminal_outcomes=tuple(terminals),
        trial_summaries=tuple(summaries),
    )


def _build_manifest(
    topology: AdmittedTopology,
    *,
    config_bytes: bytes,
    input_transfer: InputTransferReceipt,
) -> ExecutedManifest:
    config = topology.config
    transfer_bytes = canonical_json_bytes(input_transfer.model_dump(mode="json"))
    return ExecutedManifest(
        schema_version="inferdrome.routing-executed-manifest.v1",
        execution_id=config.execution_id,
        mode=config.mode,
        source_commit=config.source_commit,
        config_sha256=sha256_digest(config_bytes),
        runner_image=config.runner_image,
        serving_image=config.serving_image,
        model=config.model,
        runtime=config.runtime,
        routing_inputs=config.routing_inputs,
        workload=config.workload,
        endpoints=tuple(endpoint.published_identity for endpoint in topology.endpoints),
        telemetry=config.telemetry,
        fault=config.fault,
        topology=config.topology,
        evidence_destination_sha256=config.evidence_destination.destination_sha256,
        input_transfer_receipt_sha256=sha256_digest(transfer_bytes),
        planned_requests_per_trial=6,
        planned_terminal_denominator=18,
        no_retry=True,
    )


def run_execution(
    config_path: Path,
    workload_path: Path,
    output_path: Path,
    *,
    transport_factory: Callable[[], EndpointTransport] = UrllibEndpointTransport,
    clock: MonotonicClock | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> SealedRoutingExecution:
    """Execute and seal one admitted two-endpoint campaign with no retry path."""

    config, config_bytes = load_config(config_path)
    try:
        topology = admit_topology(config)
    except TopologyAdmissionError as error:
        raise ExecutionError("execution topology was rejected") from error
    workload, workload_bytes = load_workload(
        workload_path, expected_sha256=config.workload.selected_workload_sha256
    )
    if (
        config.evidence_destination.declared_input_transfer_sha256
        != declared_input_transfer_digest(config)
    ):
        raise ExecutionError("execution input transfer declaration disagrees")
    input_transfer = InputTransferReceipt(
        schema_version="inferdrome.routing-input-transfer-receipt.v1",
        config_sha256=sha256_digest(config_bytes),
        selected_workload_sha256=sha256_digest(workload_bytes),
        workload_size_bytes=len(workload_bytes),
        declared_input_transfer_sha256=(
            config.evidence_destination.declared_input_transfer_sha256
        ),
        verified_before_transport=True,
    )
    # Reserve the create/no-replace output before a transport factory can run.
    try:
        reservation = EvidenceReservation.reserve(output_path)
    except ExecutionPackageError as error:
        raise ExecutionError("evidence destination was rejected") from error
    try:
        manifest = _build_manifest(
            topology, config_bytes=config_bytes, input_transfer=input_transfer
        )
        manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        transfer_bytes = canonical_json_bytes(input_transfer.model_dump(mode="json"))
        records = execute(
            topology,
            workload,
            transport_factory=transport_factory,
            clock=clock,
            cancel_requested=cancel_requested,
        )
        receipt = ProducerReceipt(
            schema_version="inferdrome.routing-producer-receipt.v1",
            executed_manifest_sha256=sha256_digest(manifest_bytes),
            input_transfer_receipt_sha256=sha256_digest(transfer_bytes),
            reset_receipts=records.reset_receipts,
            fault_receipts=records.fault_receipts,
            telemetry_observations=records.telemetry_observations,
            route_decisions=records.route_decisions,
            terminal_outcomes=records.terminal_outcomes,
            trial_summaries=records.trial_summaries,
        )
        payloads = {
            "input-transfer-receipt.json": transfer_bytes,
            "executed-manifest.json": manifest_bytes,
            "producer-receipt.json": canonical_json_bytes(
                receipt.model_dump(mode="json")
            ),
        }
        integrity = make_integrity_manifest(config.execution_id, payloads)
        sealed: SealedPackage = reservation.publish(payloads, integrity)
        return SealedRoutingExecution(
            path=sealed.path,
            retained_digest=sealed.retained_digest,
        )
    except (ExecutionPackageError, ValidationError) as error:
        reservation.close()
        raise ExecutionError("routing execution evidence sealing failed") from error
    except Exception:
        reservation.close()
        raise
