"""Reduced schedules reach the existing client and two fake local replicas."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from inferdrome import vllm_router_study as study
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing, validate_timing
from inferdrome.vllm_reduced_timing import (
    make_reduced_timing,
    original_groups,
)
from inferdrome.vllm_request_identity import verify_links
from inferdrome.vllm_timed_result import verify_timed_result
from tests.integration import test_vllm_timed_study as loopback


def _parameters(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "group_size": 2,
        "retained_spacing_bps": 0,
        "max_advance_ns": plan["duration_ns"],
    }


def _reduced(plan: dict[str, Any]) -> dict[str, Any]:
    parameters = _parameters(plan)
    active = [
        group["group_id"]
        for group in original_groups(plan, parameters)
        if group["moved_count"]
    ]
    assert len(active) >= 2
    # Retain alternating original groups, including a group after a restored one.
    return make_reduced_timing(
        plan, source_parameters=parameters, retained_groups=active[1::2]
    )


@pytest.mark.parametrize("kind", ["baseline", "capacity"])
def test_reduced_schedule_runs_through_existing_client_and_two_replicas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(loopback, "_timing", _reduced)
    plan, descriptor, result, ledger, payloads = asyncio.run(
        loopback._trial(tmp_path, kind)
    )
    source = make_timing(plan, **_parameters(plan))
    original = study.validate_plan(plan)
    reduced = validate_timing(plan, descriptor)
    assert descriptor["source_timing_sha256"] == source["timing_sha256"]
    assert 0 < descriptor["moved_count"] < source["moved_count"]
    assert descriptor["population_sha256"] == source["population_sha256"]
    assert any(
        offer.scheduled_ns == base.scheduled_ns != root["scheduled_ns"]
        for offer, base, root in zip(reduced, original, source["arrivals"], strict=True)
    )
    assert sorted(payload["messages"][0]["content"] for payload in payloads) == sorted(
        offer.prompt for offer in original
    )
    assert all(payload["max_tokens"] == plan["max_tokens"] for payload in payloads)
    measurement = result["measurement"]
    assert measurement["plan_sha256"] == descriptor["timing_sha256"]
    assert measurement["trace_sha256"] == descriptor["transformed_trace_sha256"]
    assert [row["scheduled_ns"] for row in measurement["rows"]] == [
        offer.scheduled_ns for offer in reduced
    ]
    assert [row["epoch"] for row in measurement["rows"]] == [
        offer.epoch for offer in original
    ]
    assert measurement["summary"]["all_offered"]["offered"] == len(original)
    assert (
        measurement["summary"]["all_offered"]["goodput_denominator_ns"]
        == plan["duration_ns"]
    )
    assert (
        verify_timed_result(plan, descriptor, result)["measurement_comparison_valid"]
        is True
    )
    links = verify_links(result, list(reversed(ledger)))
    assert links["correlation_valid"] is True
    assert links["matched"] == len(original)
    assert {row["replica"] for row in links["links"]} == {0, 1}


@pytest.mark.parametrize("mutation", ["unknown_group", "duplicate_group", "arrival"])
def test_rehashed_invalid_reduction_fails_before_opening_a_client(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan = loopback._plan()
    descriptor = copy.deepcopy(_reduced(plan))
    if mutation == "unknown_group":
        descriptor["retained_groups"] = ["e9-g0000"]
    elif mutation == "duplicate_group":
        descriptor["retained_groups"] *= 2
    else:
        descriptor["arrivals"][0]["scheduled_ns"] += 1
    descriptor["timing_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in descriptor.items()
                    if key != "timing_sha256"
                }
            )
        ).hexdigest()
    )

    def unexpected_session(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid reduction reached HTTP session construction")

    monkeypatch.setattr(study.aiohttp, "ClientSession", unexpected_session)
    with pytest.raises(ValueError):
        asyncio.run(
            study.run_trial(
                plan,
                router_origin="http://127.0.0.1:8090",
                model="fake",
                expected_policy="round_robin",
                correlate_requests=True,
                timing=descriptor,
            )
        )
