"""Pure, offline semantic replay for routing-execution-v1 evidence.

This module intentionally depends on contracts and the small policy projection,
not on the executor, transport, filesystem, or any deployment surface.  It
turns a coherently re-hashed but semantically false receipt into a verification
failure before a consumer can render or rely on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn, cast

from inferdrome.routing_execution.contracts import (
    CandidateState,
    EndpointId,
    ExecutedManifest,
    ProducerReceipt,
    RouteDecisionReceipt,
    Signal,
    TelemetryObservation,
    TerminalOutcomeReceipt,
    fixed_policy_ids,
    fixed_request_ids,
)
from inferdrome.routing_execution.policy import decide


class ReplayVerificationError(ValueError):
    """A sealed receipt does not reproduce the bounded declared experiment."""


_SIGNALS: tuple[Signal, Signal, Signal, Signal] = (
    "HEALTH",
    "LOAD",
    "GPU_DCGM",
    "KV_CACHE",
)
type TelemetryKey = tuple[str, str, int, EndpointId, Signal]


def _trial_id(policy_id: str) -> str:
    return {
        "fail_closed_required_load_v1": "trial-fail-closed-v1",
        "explicit_fail_open_stale_load_v1": "trial-fail-open-v1",
        "typed_admissible_state_only_v1": "trial-typed-v1",
    }[policy_id]


def _trial_token(trial_id: str) -> str:
    return trial_id.removeprefix("trial-").removesuffix("-v1")


def _fail(message: str) -> NoReturn:
    raise ReplayVerificationError(message)


def _telemetry_index(
    receipt: ProducerReceipt,
) -> dict[TelemetryKey, TelemetryObservation]:
    indexed = {
        (
            row.trial_id,
            row.request_id,
            row.sequence_index,
            row.endpoint_id,
            row.signal,
        ): row
        for row in receipt.telemetry_observations
    }
    if len(indexed) != len(receipt.telemetry_observations):
        _fail("telemetry identities are not unique")
    return indexed


def _candidate_eligible(policy_id: str, candidate: CandidateState) -> bool:
    return (
        candidate.health.admissibility == "ADMISSIBLE"
        and candidate.load.state != "UNAVAILABLE"
        and (
            policy_id != "fail_closed_required_load_v1"
            or candidate.load.admissibility == "ADMISSIBLE"
        )
    )


def _verify_terminal(
    decision: RouteDecisionReceipt, terminal: TerminalOutcomeReceipt
) -> None:
    if (
        terminal.decision_id != decision.decision_id
        or terminal.terminal_outcome_id != decision.terminal_outcome_id
        or terminal.selected_endpoint_id != decision.selected_endpoint_id
    ):
        _fail("decision and terminal bindings disagree")
    if decision.selected_endpoint_id is None:
        if (
            terminal.status != "NO_SAFE_ROUTE"
            or terminal.attempt_count != 0
            or terminal.request_sha256 is not None
            or terminal.response_sha256 is not None
            or terminal.http_status is not None
            or terminal.reason != decision.fallback_reason
        ):
            _fail("no-safe-route terminal is inconsistent")
        return
    if terminal.status == "NO_SAFE_ROUTE":
        _fail("selected decision cannot have no-safe-route terminal")
    if terminal.attempt_count == 0:
        if (
            terminal.status != "CANCELLED"
            or terminal.reason != "CANCELLATION_REQUESTED"
            or terminal.request_sha256 is not None
            or terminal.response_sha256 is not None
            or terminal.http_status is not None
        ):
            _fail("pre-dispatch cancellation is inconsistent")
        return
    if terminal.request_sha256 is None:
        _fail("one-attempt terminal lacks request identity")
    if terminal.status == "SUCCEEDED":
        if (
            terminal.reason != "HTTP_2XX"
            or terminal.response_sha256 is None
            or terminal.http_status is None
            or not 200 <= terminal.http_status <= 299
        ):
            _fail("successful terminal is inconsistent")
    elif terminal.status == "TIMED_OUT":
        if (
            terminal.reason != "TRANSPORT_TIMEOUT"
            or terminal.response_sha256 is not None
            or terminal.http_status is not None
        ):
            _fail("timeout terminal is inconsistent")
    elif terminal.status == "CANCELLED":
        if (
            terminal.reason != "TRANSPORT_CANCELLED"
            or terminal.response_sha256 is not None
            or terminal.http_status is not None
        ):
            _fail("transport cancellation terminal is inconsistent")
    elif terminal.status == "FAILED":
        if terminal.reason == "HTTP_NON_2XX":
            if (
                terminal.response_sha256 is None
                or terminal.http_status is None
                or 200 <= terminal.http_status <= 299
            ):
                _fail("HTTP failure terminal is inconsistent")
        elif terminal.reason == "MALFORMED_RESPONSE":
            if (
                terminal.response_sha256 is None
                or terminal.http_status is None
                or not 200 <= terminal.http_status <= 299
            ):
                _fail("malformed-response terminal is inconsistent")
        elif terminal.reason == "TRANSPORT_FAILED":
            if terminal.response_sha256 is not None or terminal.http_status is not None:
                _fail("transport failure terminal is inconsistent")
        else:
            _fail("failed terminal reason is not declared")
    else:
        _fail("terminal status is not declared")


def _verify_candidate_observations(
    decision: RouteDecisionReceipt,
    telemetry: Mapping[TelemetryKey, TelemetryObservation],
    *,
    manifest: ExecutedManifest,
    prior_load: Mapping[str, TelemetryObservation] | None,
) -> None:
    expected_bounds = {
        "HEALTH": manifest.telemetry.health_freshness_ms * 1_000_000,
        "LOAD": manifest.telemetry.load_freshness_ms * 1_000_000,
        "GPU_DCGM": manifest.telemetry.gpu_freshness_ms * 1_000_000,
        "KV_CACHE": 0,
    }
    for candidate in decision.candidates:
        observations = (
            candidate.health,
            candidate.load,
            candidate.gpu_dcgm,
            candidate.kv_cache,
        )
        for row, signal in zip(observations, _SIGNALS, strict=True):
            key = (
                decision.trial_id,
                decision.request_id,
                decision.sequence_index,
                candidate.endpoint_id,
                signal,
            )
            if telemetry.get(key) != row:
                _fail("decision candidate is not bound to telemetry population")
            if row.decision_at_monotonic_ns != decision.decision_at_monotonic_ns:
                _fail("candidate observation decision time disagrees")
            if row.freshness_bound_ns != expected_bounds[signal]:
                _fail("telemetry freshness bound disagrees with manifest")
            if row.state == "AVAILABLE" and row.age_ns > row.freshness_bound_ns:
                _fail("available telemetry age is inconsistent")
            if row.state == "STALE" and row.age_ns <= row.freshness_bound_ns:
                _fail("stale telemetry age is inconsistent")
        if (
            candidate.health.observer_id != "health-observer-v1"
            or candidate.health.source != "HTTP_HEALTH"
            or candidate.health.epoch != decision.sequence_index + 1
        ):
            _fail("health observer epoch identity disagrees")
        if (
            candidate.load.observer_id != "load-observer-v1"
            or candidate.load.source != "VLLM_METRICS"
            or candidate.load.epoch
            != (decision.sequence_index + 1 if decision.sequence_index < 2 else 2)
        ):
            _fail("load observer epoch identity disagrees")
        if decision.sequence_index >= 2:
            if prior_load is None or candidate.endpoint_id not in prior_load:
                _fail("faulted load has no pre-fault observation")
            previous = prior_load[candidate.endpoint_id]
            if (
                candidate.load.sampled_at_monotonic_ns
                != previous.sampled_at_monotonic_ns
            ):
                _fail("faulted load was sampled after collection pause")
            if previous.state == "AVAILABLE" and candidate.load.state != "STALE":
                _fail("faulted available load did not become stale")
            if (
                previous.state == "UNAVAILABLE"
                and candidate.load.state != "UNAVAILABLE"
            ):
                _fail("unavailable load was fabricated after pause")
        if (
            candidate.gpu_dcgm.observer_id != "gpu-dcgm-observer-v1"
            or candidate.gpu_dcgm.epoch != 0
            or candidate.gpu_dcgm.state != "UNAVAILABLE"
            or candidate.gpu_dcgm.source != "UNAVAILABLE_CAPABILITY"
            or candidate.gpu_dcgm.value != "UNAVAILABLE"
            or candidate.kv_cache.observer_id != "kv-cache-observer-v1"
            or candidate.kv_cache.epoch != 0
            or candidate.kv_cache.state != "UNAVAILABLE"
            or candidate.kv_cache.source != "UNAVAILABLE_CAPABILITY"
            or candidate.kv_cache.value != "UNAVAILABLE"
        ):
            _fail("unavailable capability telemetry was fabricated")
        if candidate.eligible != _candidate_eligible(decision.policy_id, candidate):
            _fail("candidate eligibility disagrees with policy contract")


def verify_replay(manifest: ExecutedManifest, receipt: ProducerReceipt) -> None:
    """Replay all bounded receipt decisions using only sealed package records."""

    policies = fixed_policy_ids()
    if tuple(summary.policy_id for summary in receipt.trial_summaries) != policies:
        _fail("trial summary policy ordering is invalid")
    telemetry = _telemetry_index(receipt)
    decisions = {
        (row.trial_id, row.request_id, row.sequence_index): row
        for row in receipt.route_decisions
    }
    terminals = {
        (row.trial_id, row.request_id, row.sequence_index): row
        for row in receipt.terminal_outcomes
    }
    if len(decisions) != 18 or len(terminals) != 18 or set(decisions) != set(terminals):
        _fail("decision and terminal denominator is not closed")
    resets = {row.trial_id: row for row in receipt.reset_receipts}
    faults = {row.trial_id: row for row in receipt.fault_receipts}
    if len(resets) != 3 or len(faults) != 3:
        _fail("reset or fault receipt inventory is invalid")
    for policy_id in policies:
        trial_id = _trial_id(policy_id)
        summary = next(
            (row for row in receipt.trial_summaries if row.trial_id == trial_id), None
        )
        reset = resets.get(trial_id)
        fault = faults.get(trial_id)
        if (
            summary is None
            or summary.policy_id != policy_id
            or reset is None
            or fault is None
        ):
            _fail("trial identity is invalid")
        if (
            reset.policy_id != policy_id
            or reset.observer_epochs
            != {"HEALTH": 0, "LOAD": 0, "GPU_DCGM": 0, "KV_CACHE": 0}
            or not reset.runner_connection_state_cleared
            or not reset.runner_telemetry_state_cleared
            or reset.endpoint_runtime_identities != manifest.endpoints
            or reset.endpoint_engine_reset_assertion
            != "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
        ):
            _fail("cold-reset receipt is inconsistent")
        if (
            fault.fault_id != "stale-load-fresh-health-v1"
            or fault.activated_at_sequence_index != 2
            or not fault.load_collection_paused
            or not fault.health_collection_continues
        ):
            _fail("fault receipt is inconsistent")
        prior_load: dict[str, TelemetryObservation] | None = None
        token = _trial_token(trial_id)
        previous_decision_at: int | None = None
        for sequence_index, request_id in enumerate(fixed_request_ids()):
            key = (trial_id, request_id, sequence_index)
            decision = decisions.get(key)
            terminal = terminals.get(key)
            if decision is None or terminal is None:
                _fail("fixed request trace is incomplete")
            if (
                decision.policy_id != policy_id
                or decision.decision_id != f"decision-{token}-{sequence_index:03d}"
                or decision.terminal_outcome_id
                != f"terminal-{token}-{sequence_index:03d}"
            ):
                _fail("route decision identity is invalid")
            if (
                previous_decision_at is not None
                and decision.decision_at_monotonic_ns < previous_decision_at
            ):
                _fail("request decision time is not monotonic")
            if sequence_index == 2:
                prior = decisions[(trial_id, fixed_request_ids()[1], 1)]
                if not (
                    prior.decision_at_monotonic_ns
                    < fault.activated_at_monotonic_ns
                    <= decision.decision_at_monotonic_ns
                ):
                    _fail("fault is outside its declared request interval")
            _verify_candidate_observations(
                decision,
                telemetry,
                manifest=manifest,
                prior_load=prior_load,
            )
            candidates = cast(
                tuple[CandidateState, CandidateState], decision.candidates
            )
            expected = decide(policy_id, candidates)
            if (
                decision.selected_endpoint_id != expected.selected_endpoint_id
                or decision.claims_used != expected.claims_used
                or decision.claims_permitted_stale != expected.claims_permitted_stale
                or decision.claims_discarded != expected.claims_discarded
                or decision.fallback_reason != expected.fallback_reason
            ):
                _fail("route decision does not replay from admissible state")
            _verify_terminal(decision, terminal)
            if terminal.started_at_monotonic_ns < decision.decision_at_monotonic_ns:
                _fail("terminal begins before its route decision")
            previous_decision_at = decision.decision_at_monotonic_ns
            if sequence_index == 1:
                prior_load = {
                    candidate.endpoint_id: candidate.load
                    for candidate in decision.candidates
                }
        first_decision = decisions[(trial_id, fixed_request_ids()[0], 0)]
        if reset.reset_at_monotonic_ns > first_decision.decision_at_monotonic_ns:
            _fail("reset occurs after a trial decision")
        if fault.activated_at_monotonic_ns < reset.reset_at_monotonic_ns:
            _fail("fault occurs before its cold reset")
