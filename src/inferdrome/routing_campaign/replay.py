"""Independent semantic replay for sealed routing-campaign-v1 packages.

This module deliberately does not import the campaign engine or package writer.
It reconstructs the frozen virtual-clock vector from parsed contracts and checks
every receipt against that independent reconstruction.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal

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

_Signal = Literal["HEALTH", "LOAD", "KV"]


class ReplayVerificationError(ValueError):
    """A sealed receipt disagrees with the independently reconstructed vector."""


def _observation(
    *,
    signal: _Signal,
    observer_id: str,
    endpoint_id: EndpointId,
    epoch: int,
    observed_at_ms: int,
    decision_time_ms: int,
    freshness_bound_ms: int,
    value: str | int,
) -> Observation:
    age_ms = decision_time_ms - observed_at_ms
    return Observation(
        signal=signal,
        observer_id=observer_id,
        endpoint_id=endpoint_id,
        epoch=epoch,
        observed_at_ms=observed_at_ms,
        decision_time_ms=decision_time_ms,
        age_ms=age_ms,
        freshness_bound_ms=freshness_bound_ms,
        value=value,
        admissibility="ADMISSIBLE" if age_ms <= freshness_bound_ms else "INADMISSIBLE",
    )


def _candidate(
    plan: RoutingCampaignPlan,
    fault_schedule: FaultSchedule,
    *,
    endpoint_id: EndpointId,
    record: RequestTraceRecord,
    policy_id: PolicyId,
) -> CandidateState:
    if not fault_schedule.health_continues:
        raise ReplayVerificationError("independent health observer did not continue")
    health_observed_at_ms = record.decision_time_ms - (
        record.decision_time_ms % plan.health_observer.interval_ms
    )
    health = _observation(
        signal="HEALTH",
        observer_id=plan.health_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=(health_observed_at_ms // plan.health_observer.interval_ms) + 1,
        observed_at_ms=health_observed_at_ms,
        decision_time_ms=record.decision_time_ms,
        freshness_bound_ms=plan.health_observer.freshness_bound_ms,
        value="HEALTHY",
    )
    eligible_updates = tuple(
        update_time_ms
        for update_time_ms in plan.load_observer.update_times_ms
        if (
            update_time_ms <= record.decision_time_ms
            and update_time_ms < fault_schedule.load_observer_pause_at_ms
        )
    )
    if not eligible_updates:
        raise ReplayVerificationError("load observer has no sample before request")
    load = _observation(
        signal="LOAD",
        observer_id=plan.load_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=len(eligible_updates),
        observed_at_ms=eligible_updates[-1],
        decision_time_ms=record.decision_time_ms,
        freshness_bound_ms=plan.load_observer.freshness_bound_ms,
        value=plan.load_observer.initial_values[endpoint_id],
    )
    kv = _observation(
        signal="KV",
        observer_id=plan.kv_observer.observer_id,
        endpoint_id=endpoint_id,
        epoch=record.sequence_index + 1,
        observed_at_ms=record.decision_time_ms,
        decision_time_ms=record.decision_time_ms,
        freshness_bound_ms=plan.kv_observer.freshness_bound_ms,
        value="NO_USABLE_SESSION_CLAIM",
    )
    return CandidateState(
        endpoint_id=endpoint_id,
        health=health,
        load=load,
        kv=kv,
        eligible=(
            health.admissibility == "ADMISSIBLE"
            and (
                policy_id != "fail_closed_required_load_v1"
                or load.admissibility == "ADMISSIBLE"
            )
        ),
    )


def _expected_reset(plan: RoutingCampaignPlan, trial: TrialDefinition) -> ResetReceipt:
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


def _expected_request(
    plan: RoutingCampaignPlan,
    fault_schedule: FaultSchedule,
    trial: TrialDefinition,
    record: RequestTraceRecord,
) -> tuple[
    tuple[StateObservationRecord, ...], RouteDecisionReceipt, TerminalOutcomeReceipt
]:
    candidates = tuple(
        _candidate(
            plan,
            fault_schedule,
            endpoint_id=endpoint.endpoint_id,
            record=record,
            policy_id=trial.policy_id,
        )
        for endpoint in plan.endpoints
    )
    observations = tuple(
        StateObservationRecord(
            schema_version="inferdrome.routing-state-observation.v1",
            campaign_id=plan.campaign_id,
            trial_id=trial.trial_id,
            request_id=record.request_id,
            sequence_index=record.sequence_index,
            observation=observation,
        )
        for candidate in candidates
        for observation in (candidate.health, candidate.load, candidate.kv)
    )
    stale_load = candidates[0].load.admissibility == "INADMISSIBLE"
    selected_endpoint_id: EndpointId | None
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
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

    trial_token = trial.trial_id.removeprefix("trial-")
    decision_id = f"decision-{trial_token}-{record.sequence_index:03d}"
    terminal_id = f"terminal-{trial_token}-{record.sequence_index:03d}"
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
    if selected_endpoint_id is None:
        status: TerminalStatus = "NO_SAFE_ROUTE"
        reason = "REQUIRED_LOAD_STALE"
        duration_ms = 0
    elif (
        selected_endpoint_id == "endpoint-b"
        and record.decision_time_ms >= plan.endpoint_behavior.endpoint_b_saturated_at_ms
    ):
        status = "TIMED_OUT"
        reason = "SIMULATED_ENDPOINT_B_SATURATED"
        duration_ms = 5
    else:
        status = "SUCCEEDED"
        reason = "NONE"
        duration_ms = 1
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
        ended_at_ms=record.decision_time_ms + duration_ms,
        reason=reason,
    )
    return observations, decision, terminal


def _expected_summary(
    plan: RoutingCampaignPlan,
    trial: TrialDefinition,
    terminals: tuple[TerminalOutcomeReceipt, ...],
) -> TrialSummary:
    population_counts = Counter(terminal.status for terminal in terminals)
    population: dict[TerminalStatus, int] = {
        "SUCCEEDED": population_counts["SUCCEEDED"],
        "TIMED_OUT": population_counts["TIMED_OUT"],
        "FAILED": population_counts["FAILED"],
        "CANCELLED": population_counts["CANCELLED"],
        "NO_SAFE_ROUTE": population_counts["NO_SAFE_ROUTE"],
    }
    return TrialSummary(
        schema_version="inferdrome.routing-trial-summary.v1",
        campaign_id=plan.campaign_id,
        trial_id=trial.trial_id,
        policy_id=trial.policy_id,
        planned_request_count=6,
        terminal_population=population,
    )


def _require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ReplayVerificationError(f"routing replay disagrees with {label}")


def verify_replay(
    *,
    plan: RoutingCampaignPlan,
    trace: tuple[RequestTraceRecord, ...],
    fault_schedule: FaultSchedule,
    trial_plan: TrialPlan,
    resets: Mapping[str, ResetReceipt],
    observations: Mapping[str, tuple[StateObservationRecord, ...]],
    decisions: Mapping[str, tuple[RouteDecisionReceipt, ...]],
    terminals: Mapping[str, tuple[TerminalOutcomeReceipt, ...]],
    summaries: Mapping[str, TrialSummary],
    campaign_summary: CampaignSummary,
) -> None:
    """Check all records using an implementation independent of the producer."""

    expected_times = plan.virtual_timing.request_times_ms
    if (
        tuple(record.request_id for record in trace)
        != tuple(f"request-{index:03d}" for index in range(len(expected_times)))
        or tuple(record.sequence_index for record in trace)
        != tuple(range(len(expected_times)))
        or tuple(record.decision_time_ms for record in trace) != expected_times
    ):
        raise ReplayVerificationError(
            "request trace disagrees with frozen virtual vector"
        )
    expected_trial_ids = {trial.trial_id for trial in trial_plan.trials}
    for label, actual in (
        ("reset trial IDs", set(resets)),
        ("observation trial IDs", set(observations)),
        ("decision trial IDs", set(decisions)),
        ("terminal trial IDs", set(terminals)),
        ("summary trial IDs", set(summaries)),
    ):
        _require_equal(label, actual, expected_trial_ids)

    expected_summaries: list[TrialSummary] = []
    for trial in trial_plan.trials:
        _require_equal(
            f"reset receipt for {trial.trial_id}",
            resets[trial.trial_id],
            _expected_reset(plan, trial),
        )
        expected_observations: list[StateObservationRecord] = []
        expected_decisions: list[RouteDecisionReceipt] = []
        expected_terminals: list[TerminalOutcomeReceipt] = []
        for record in trace:
            expected_state, expected_decision, expected_terminal = _expected_request(
                plan,
                fault_schedule,
                trial,
                record,
            )
            expected_observations.extend(expected_state)
            expected_decisions.append(expected_decision)
            expected_terminals.append(expected_terminal)
        expected_observation_rows = tuple(expected_observations)
        expected_decision_rows = tuple(expected_decisions)
        expected_terminal_rows = tuple(expected_terminals)
        _require_equal(
            f"state observations for {trial.trial_id}",
            observations[trial.trial_id],
            expected_observation_rows,
        )
        _require_equal(
            f"route decisions for {trial.trial_id}",
            decisions[trial.trial_id],
            expected_decision_rows,
        )
        _require_equal(
            f"terminal outcomes for {trial.trial_id}",
            terminals[trial.trial_id],
            expected_terminal_rows,
        )
        expected_summary = _expected_summary(plan, trial, expected_terminal_rows)
        _require_equal(
            f"trial summary for {trial.trial_id}",
            summaries[trial.trial_id],
            expected_summary,
        )
        expected_summaries.append(expected_summary)
    _require_equal(
        "campaign summary",
        campaign_summary,
        CampaignSummary(
            schema_version="inferdrome.routing-campaign-summary.v1",
            campaign_id=plan.campaign_id,
            trial_summaries=tuple(expected_summaries),
        ),
    )
