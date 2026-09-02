"""Deterministic, in-process routing-campaign-v1 simulator."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from inferdrome.routing_campaign.contracts import (
    CampaignSummary,
    CandidateState,
    EndpointId,
    FallbackReason,
    FaultSchedule,
    Observation,
    PolicyId,
    RequestTraceRecord,
    ResetReceipt,
    RouteDecisionReceipt,
    RoutingCampaignPlan,
    StateObservationRecord,
    TerminalOutcomeReceipt,
    TerminalStatus,
    TrialDefinition,
    TrialPlan,
    TrialSummary,
)


@dataclass(frozen=True)
class TrialExecution:
    """All separately sealed records for one cold policy trial."""

    reset: ResetReceipt
    observations: tuple[StateObservationRecord, ...]
    decisions: tuple[RouteDecisionReceipt, ...]
    terminals: tuple[TerminalOutcomeReceipt, ...]
    summary: TrialSummary


@dataclass(frozen=True)
class CampaignExecution:
    """Complete deterministic output before package serialization."""

    trials: tuple[TrialExecution, ...]
    summary: CampaignSummary


@dataclass(frozen=True)
class InProcessMockEndpoint:
    """One cold, deterministic endpoint behavior used only by R1's virtual clock."""

    endpoint_id: EndpointId
    saturated_at_ms: int | None

    def terminal_at(self, decision_time_ms: int) -> tuple[TerminalStatus, str, int]:
        """Return the bounded mock terminal result for one selected request."""

        if (
            self.saturated_at_ms is not None
            and decision_time_ms >= self.saturated_at_ms
        ):
            return "TIMED_OUT", "SIMULATED_ENDPOINT_B_SATURATED", 5
        return "SUCCEEDED", "NONE", 1


def validate_trace(
    plan: RoutingCampaignPlan,
    trace: tuple[RequestTraceRecord, ...],
) -> None:
    """Reject any trace that is not the declared fixed six-request vector."""

    expected_times = plan.virtual_timing.request_times_ms
    expected_ids = tuple(f"request-{index:03d}" for index in range(len(expected_times)))
    if len(trace) != len(expected_times):
        raise ValueError("routing campaign trace must contain exactly six requests")
    if tuple(record.request_id for record in trace) != expected_ids:
        raise ValueError("routing campaign trace request IDs are not the frozen order")
    if tuple(record.sequence_index for record in trace) != tuple(range(len(trace))):
        raise ValueError("routing campaign trace sequence indexes are not contiguous")
    if tuple(record.decision_time_ms for record in trace) != expected_times:
        raise ValueError("routing campaign trace times are not the frozen vector")


def _reset(plan: RoutingCampaignPlan, trial: TrialDefinition) -> ResetReceipt:
    return ResetReceipt(
        schema_version="inferdrome.routing-reset-receipt.v1",
        campaign_id=plan.campaign_id,
        trial_id=trial.trial_id,
        policy_id=trial.policy_id,
        virtual_time_ms=0,
        endpoint_instance_ids={
            "endpoint-a": f"{trial.trial_id}-endpoint-a-instance-v1",
            "endpoint-b": f"{trial.trial_id}-endpoint-b-instance-v1",
        },
        observer_epochs={
            plan.health_observer.observer_id: 1,
            plan.load_observer.observer_id: 1,
            plan.kv_observer.observer_id: 1,
        },
        queue_cleared=True,
        load_state_cleared=True,
        kv_state_cleared=True,
    )


def _observation(
    *,
    signal: str,
    observer_id: str,
    endpoint_id: str,
    epoch: int,
    observed_at_ms: int,
    decision_time_ms: int,
    freshness_bound_ms: int,
    value: str | int,
) -> Observation:
    age_ms = decision_time_ms - observed_at_ms
    return Observation(
        signal=signal,  # type: ignore[arg-type]
        observer_id=observer_id,
        endpoint_id=endpoint_id,  # type: ignore[arg-type]
        epoch=epoch,
        observed_at_ms=observed_at_ms,
        decision_time_ms=decision_time_ms,
        age_ms=age_ms,
        freshness_bound_ms=freshness_bound_ms,
        value=value,
        admissibility="ADMISSIBLE" if age_ms <= freshness_bound_ms else "INADMISSIBLE",
    )


def _candidate_state(
    plan: RoutingCampaignPlan,
    fault_schedule: FaultSchedule,
    *,
    endpoint_id: EndpointId,
    decision_time_ms: int,
    sequence_index: int,
    policy_id: PolicyId,
) -> CandidateState:
    if not fault_schedule.health_continues:
        raise ValueError("routing campaign requires the independent health observer")
    health_observed_at_ms = decision_time_ms - (
        decision_time_ms % plan.health_observer.interval_ms
    )
    health = _observation(
        signal="HEALTH",
        observer_id=plan.health_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=(health_observed_at_ms // plan.health_observer.interval_ms) + 1,
        observed_at_ms=health_observed_at_ms,
        decision_time_ms=decision_time_ms,
        freshness_bound_ms=plan.health_observer.freshness_bound_ms,
        value="HEALTHY",
    )
    load_updates = tuple(
        update_time_ms
        for update_time_ms in plan.load_observer.update_times_ms
        if (
            update_time_ms <= decision_time_ms
            and update_time_ms < fault_schedule.load_observer_pause_at_ms
        )
    )
    if not load_updates:
        raise ValueError("routing load observer has no sample before the request")
    last_load_at_ms = load_updates[-1]
    load = _observation(
        signal="LOAD",
        observer_id=plan.load_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=len(load_updates),
        observed_at_ms=last_load_at_ms,
        decision_time_ms=decision_time_ms,
        freshness_bound_ms=plan.load_observer.freshness_bound_ms,
        value=plan.load_observer.initial_values[endpoint_id],
    )
    kv = _observation(
        signal="KV",
        observer_id=plan.kv_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=sequence_index + 1,
        observed_at_ms=decision_time_ms,
        decision_time_ms=decision_time_ms,
        freshness_bound_ms=plan.kv_observer.freshness_bound_ms,
        value="NO_USABLE_SESSION_CLAIM",
    )
    if policy_id == "fail_closed_required_load_v1":
        eligible = (
            health.admissibility == "ADMISSIBLE" and load.admissibility == "ADMISSIBLE"
        )
    else:
        eligible = health.admissibility == "ADMISSIBLE"
    return CandidateState(
        endpoint_id=endpoint_id,
        health=health,
        load=load,
        kv=kv,
        eligible=eligible,
    )


def _decision_and_terminal(
    plan: RoutingCampaignPlan,
    fault_schedule: FaultSchedule,
    trial: TrialDefinition,
    record: RequestTraceRecord,
    endpoints: tuple[InProcessMockEndpoint, ...],
) -> tuple[RouteDecisionReceipt, TerminalOutcomeReceipt]:
    candidates = tuple(
        _candidate_state(
            plan,
            fault_schedule,
            endpoint_id=endpoint_id,
            decision_time_ms=record.decision_time_ms,
            sequence_index=record.sequence_index,
            policy_id=trial.policy_id,
        )
        for endpoint_id in (endpoint.endpoint_id for endpoint in endpoints)
    )
    stale_load = candidates[0].load.admissibility == "INADMISSIBLE"
    trial_token = trial.trial_id.removeprefix("trial-")
    decision_id = f"decision-{trial_token}-{record.sequence_index:03d}"
    terminal_id = f"terminal-{trial_token}-{record.sequence_index:03d}"
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
    selected_endpoint_id: EndpointId | None
    fallback_reason: FallbackReason

    if stale_load and trial.policy_id == "fail_closed_required_load_v1":
        selected_endpoint_id = None
        claims_used = ("health",)
        claims_permitted_stale = ()
        claims_discarded = ("load:stale", "kv:no_session_claim")
        fallback_reason = "REQUIRED_LOAD_STALE"
    elif stale_load and trial.policy_id == "explicit_fail_open_stale_load_v1":
        selected_endpoint_id = "endpoint-b"
        claims_used = ("health", "load")
        claims_permitted_stale = ("load",)
        claims_discarded = ("kv:no_session_claim",)
        fallback_reason = "STALE_LOAD_FAIL_OPEN"
    elif stale_load:
        selected_endpoint_id = "endpoint-a"
        claims_used = ("health",)
        claims_permitted_stale = ()
        claims_discarded = ("load:stale", "kv:no_session_claim")
        fallback_reason = "HEALTH_ONLY_TIE_BREAK"
    else:
        selected_endpoint_id = "endpoint-b"
        claims_used = ("health", "load")
        claims_permitted_stale = ()
        claims_discarded = ("kv:no_session_claim",)
        fallback_reason = "NONE"
    if selected_endpoint_id is None:
        status: TerminalStatus = "NO_SAFE_ROUTE"
        terminal_reason = "REQUIRED_LOAD_STALE"
        duration_ms = 0
    else:
        selected_endpoint = next(
            endpoint
            for endpoint in endpoints
            if endpoint.endpoint_id == selected_endpoint_id
        )
        status, terminal_reason, duration_ms = selected_endpoint.terminal_at(
            record.decision_time_ms
        )
    end_ms = record.decision_time_ms + duration_ms

    decision = RouteDecisionReceipt(
        schema_version="inferdrome.route-decision-receipt.v1",
        campaign_id=plan.campaign_id,
        trial_id=trial.trial_id,
        policy_id=trial.policy_id,
        request_id=record.request_id,
        sequence_index=record.sequence_index,
        decision_id=decision_id,
        decision_time_ms=record.decision_time_ms,
        candidates=candidates,
        selected_endpoint_id=selected_endpoint_id,
        claims_used=claims_used,
        claims_permitted_stale=claims_permitted_stale,
        claims_discarded=claims_discarded,
        fallback_reason=fallback_reason,
        terminal_outcome_id=terminal_id,
    )
    terminal = TerminalOutcomeReceipt(
        schema_version="inferdrome.terminal-outcome-receipt.v1",
        campaign_id=plan.campaign_id,
        trial_id=trial.trial_id,
        request_id=record.request_id,
        sequence_index=record.sequence_index,
        terminal_outcome_id=terminal_id,
        decision_id=decision_id,
        selected_endpoint_id=selected_endpoint_id,
        status=status,
        started_at_ms=record.decision_time_ms,
        ended_at_ms=end_ms,
        reason=terminal_reason,
    )
    return decision, terminal


def _state_records(
    plan: RoutingCampaignPlan,
    trial: TrialDefinition,
    decision: RouteDecisionReceipt,
) -> tuple[StateObservationRecord, ...]:
    rows: list[StateObservationRecord] = []
    for candidate in decision.candidates:
        for observation in (candidate.health, candidate.load, candidate.kv):
            rows.append(
                StateObservationRecord(
                    schema_version="inferdrome.routing-state-observation.v1",
                    campaign_id=plan.campaign_id,
                    trial_id=trial.trial_id,
                    request_id=decision.request_id,
                    sequence_index=decision.sequence_index,
                    observation=observation,
                )
            )
    return tuple(rows)


def _summary(
    plan: RoutingCampaignPlan,
    trial: TrialDefinition,
    terminals: tuple[TerminalOutcomeReceipt, ...],
) -> TrialSummary:
    counts = Counter(terminal.status for terminal in terminals)
    population: dict[TerminalStatus, int] = {
        "SUCCEEDED": counts["SUCCEEDED"],
        "TIMED_OUT": counts["TIMED_OUT"],
        "FAILED": counts["FAILED"],
        "CANCELLED": counts["CANCELLED"],
        "NO_SAFE_ROUTE": counts["NO_SAFE_ROUTE"],
    }
    if sum(population.values()) != 6:
        raise AssertionError("routing simulation did not close its terminal population")
    return TrialSummary(
        schema_version="inferdrome.routing-trial-summary.v1",
        campaign_id=plan.campaign_id,
        trial_id=trial.trial_id,
        policy_id=trial.policy_id,
        planned_request_count=6,
        terminal_population=population,
    )


def execute_campaign(
    plan: RoutingCampaignPlan,
    trace: tuple[RequestTraceRecord, ...],
    fault_schedule: FaultSchedule,
    trial_plan: TrialPlan,
) -> CampaignExecution:
    """Run three isolated cold trials without I/O, network, or wall time."""

    validate_trace(plan, trace)
    if (
        fault_schedule.campaign_id != plan.campaign_id
        or trial_plan.campaign_id != plan.campaign_id
    ):
        raise ValueError("routing inputs disagree on campaign identity")
    executions: list[TrialExecution] = []
    for trial in trial_plan.trials:
        reset = _reset(plan, trial)
        endpoints = tuple(
            InProcessMockEndpoint(
                endpoint_id=endpoint.endpoint_id,
                saturated_at_ms=(
                    plan.endpoint_behavior.endpoint_b_saturated_at_ms
                    if endpoint.endpoint_id == "endpoint-b"
                    else None
                ),
            )
            for endpoint in plan.endpoints
        )
        decisions: list[RouteDecisionReceipt] = []
        terminals: list[TerminalOutcomeReceipt] = []
        observations: list[StateObservationRecord] = []
        for record in trace:
            decision, terminal = _decision_and_terminal(
                plan,
                fault_schedule,
                trial,
                record,
                endpoints,
            )
            decisions.append(decision)
            terminals.append(terminal)
            observations.extend(_state_records(plan, trial, decision))
        terminal_rows = tuple(terminals)
        executions.append(
            TrialExecution(
                reset=reset,
                observations=tuple(observations),
                decisions=tuple(decisions),
                terminals=terminal_rows,
                summary=_summary(plan, trial, terminal_rows),
            )
        )
    summaries = tuple(execution.summary for execution in executions)
    return CampaignExecution(
        trials=tuple(executions),
        summary=CampaignSummary(
            schema_version="inferdrome.routing-campaign-summary.v1",
            campaign_id=plan.campaign_id,
            trial_summaries=summaries,
        ),
    )
