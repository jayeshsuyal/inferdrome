"""Small pure projection of the frozen R1 policy semantics onto live receipts."""

from __future__ import annotations

from dataclasses import dataclass

from inferdrome.routing_execution.contracts import (
    CandidateState,
    EndpointId,
    FallbackReason,
    PolicyId,
)


@dataclass(frozen=True)
class PolicyDecision:
    """Selection and claim handling prior to one possible transport dispatch."""

    selected_endpoint_id: EndpointId | None
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
    fallback_reason: FallbackReason


def decide(
    policy_id: PolicyId, candidates: tuple[CandidateState, CandidateState]
) -> PolicyDecision:
    """Apply only admissible state, matching R1's first fault vectors.

    This is intentionally not a generalized router.  It has exactly the three
    R1 policies and two ordered candidates, so every selected endpoint and
    fallback can be inspected from the receipt alone.
    """

    if any(candidate.health.admissibility != "ADMISSIBLE" for candidate in candidates):
        return PolicyDecision(
            selected_endpoint_id=None,
            claims_used=(),
            claims_permitted_stale=(),
            claims_discarded=("health:unavailable", "load", "kv:unavailable"),
            fallback_reason="HEALTH_NOT_ADMISSIBLE",
        )
    # A missing/invalid required metric is not a stale observation.  The sole
    # controlled fail-open policy may use an explicitly observed stale value,
    # but it must never turn telemetry absence into permission to dispatch.
    if any(candidate.load.state == "UNAVAILABLE" for candidate in candidates):
        return PolicyDecision(
            selected_endpoint_id=None,
            claims_used=("health",),
            claims_permitted_stale=(),
            claims_discarded=("load:unavailable", "kv:unavailable"),
            fallback_reason="REQUIRED_LOAD_UNAVAILABLE",
        )
    stale_load = any(candidate.load.state == "STALE" for candidate in candidates)
    if stale_load and policy_id == "fail_closed_required_load_v1":
        return PolicyDecision(
            selected_endpoint_id=None,
            claims_used=("health",),
            claims_permitted_stale=(),
            claims_discarded=("load:stale", "kv:unavailable"),
            fallback_reason="REQUIRED_LOAD_STALE",
        )
    if stale_load and policy_id == "explicit_fail_open_stale_load_v1":
        return PolicyDecision(
            selected_endpoint_id="endpoint-b",
            claims_used=("health", "load"),
            claims_permitted_stale=("load",),
            claims_discarded=("kv:unavailable",),
            fallback_reason="STALE_LOAD_FAIL_OPEN",
        )
    if stale_load:
        return PolicyDecision(
            selected_endpoint_id="endpoint-a",
            claims_used=("health",),
            claims_permitted_stale=(),
            claims_discarded=("load:stale", "kv:unavailable"),
            fallback_reason="HEALTH_ONLY_TIE_BREAK",
        )
    loads: dict[EndpointId, int] = {}
    for candidate in candidates:
        value = candidate.load.value
        if not isinstance(value, int):
            raise AssertionError("admissible load must be a concrete integer")
        loads[candidate.endpoint_id] = value
    # R1's deterministic vector chooses endpoint-b for its lower load; the
    # explicit ordered tie break keeps the real projection inspectable too.
    options: tuple[EndpointId, EndpointId] = ("endpoint-b", "endpoint-a")
    selected: EndpointId = min(options, key=lambda item: loads[item])
    return PolicyDecision(
        selected_endpoint_id=selected,
        claims_used=("health", "load"),
        claims_permitted_stale=(),
        claims_discarded=("kv:unavailable",),
        fallback_reason="NONE",
    )
