"""Write a deterministic, fabricated Breakpoint walkthrough without serving calls."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from inferdrome import vllm_bounded_search as bounded
from inferdrome import vllm_heldout_confirmation as confirmation
from inferdrome import vllm_router_study as study
from inferdrome import vllm_witness_reducer as reducer
from inferdrome.breakpoint_report import main as summarize_report
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import (
    TIMED_RESULT_SCHEMA,
    make_timing,
    validate_timing,
)
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA as PAIRED_INPUT_SCHEMA
from inferdrome.vllm_reduced_protocol import reduced_timings
from inferdrome.vllm_request_identity import correlated_result
from inferdrome.vllm_search_plan import candidate_protocol, make_search_plan

_SOURCE_REVISION_PLACEHOLDER = "0" * 40
_BLOCKS = 8
_OFFERS = 12


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plans(first_seed: int) -> list[dict[str, Any]]:
    return [
        study.make_plan(
            phase="fixture",
            seed=first_seed + index,
            count=_OFFERS,
            expected_prompt_tokens=200,
            duration_ns=3_000_000_000,
            max_tokens=4,
        )
        for index in range(_BLOCKS)
    ]


def _fabricate_inputs(
    plans: list[dict[str, Any]],
    protocol: dict[str, Any],
    timings: list[dict[str, dict[str, Any]]],
    batch: int,
) -> list[dict[str, Any]]:
    """Fabricate a directional signal; no result is an observed measurement."""
    inputs = []
    for trial in protocol["trials"]:
        plan = plans[trial["block"] - 1]
        timing = timings[trial["block"] - 1][trial["condition"]]
        favored = (trial["condition"] == "baseline") == (
            trial["policy"] == protocol["policy_a"]
        )
        good_count = 12 if favored else 6
        rows = []
        links = []
        ledger = []
        for offer in validate_timing(plan, timing):
            scheduled = offer.scheduled_ns
            first = scheduled + (
                5_000 if offer.index < good_count else plan["first_content_slo_ns"] + 1
            )
            rows.append(
                study.RequestResult(
                    index=offer.index,
                    scheduled_ns=scheduled,
                    epoch=offer.epoch,
                    traffic_class=offer.traffic_class,
                    tenant=offer.tenant,
                    ready_ns=scheduled + 1_000,
                    dispatch_ns=scheduled + 2_000,
                    response_headers_ns=scheduled + 3_000,
                    first_body_byte_ns=scheduled + 4_000,
                    first_content_ns=first,
                    terminal_ns=first + 1_000,
                    max_content_gap_ns=0,
                    outcome="completed",
                    http_status=200,
                    prompt_tokens=200,
                    completion_tokens=4,
                    document_id=offer.document_id,
                )
            )
            identity = batch * 1_000_000 + trial["sequence"] * 100 + offer.index
            request_id = f"{identity:032x}"
            links.append(
                {
                    "index": offer.index,
                    "request_id": request_id,
                    "response_request_id": request_id,
                }
            )
            ledger.append(
                {
                    "request_id": request_id,
                    "policy": trial["policy"],
                    "outcome": "completed",
                    "replica": offer.index % 2,
                }
            )
        measurement = {
            "schema": TIMED_RESULT_SCHEMA,
            "plan_sha256": timing["timing_sha256"],
            "trace_sha256": timing["transformed_trace_sha256"],
            "token_certificate_sha256": None,
            "policy": trial["policy"],
            "model": protocol["model"],
            "router_origin": "http://127.0.0.1:8090",
            "started_unix_ns": str(
                1_700_000_000_000_000_000
                + batch * 1_000_000_000_000
                + trial["sequence"] * 13_000_000_000
            ),
            "evidence_class": "SYNTHETIC_ONLY",
            "router_accounting_valid": True,
            "status": "COMPLETED",
            "comparison_valid": True,
            "router_stats_after": {
                "policy": trial["policy"],
                "offered": len(rows),
                "terminal": len(rows),
                "ledger_rows": len(rows),
                "in_flight": 0,
                "pending": 0,
                "active": 0,
                "busy": [0, 0],
                "accounting_failed": False,
            },
            "rows": [asdict(row) for row in rows],
            "summary": study.summarize(plan, rows, include_client_timing=True),
            "base_plan_schema": plan["schema"],
            "base_plan_sha256": plan["plan_sha256"],
            "base_trace_sha256": plan["trace_sha256"],
            "token_certificate_scope": "BASE_WORKLOAD_UNCHANGED_TIMING_ONLY",
            "arrival_timing_scope": "PLANNED_OFFERS_NOT_OBSERVED_ARRIVALS",
        }
        measurement["result_sha256"] = _digest(measurement)
        inputs.append(
            {
                "trial_id": trial["trial_id"],
                "timing": timing,
                "result": correlated_result(measurement, links),
                "ledger_rows": ledger,
                "token_certificate": None,
                "execution": {
                    field: protocol[field]
                    for field in (
                        "source_revision",
                        "environment_sha256",
                        "reset_procedure_sha256",
                    )
                }
                | {"reset_completed": True},
            }
        )
    return inputs


def _add_json(artifacts: dict[Path, bytes], path: Path, value: object) -> None:
    encoded = canonical_json_bytes(value) + b"\n"
    if path in artifacts and artifacts[path] != encoded:
        raise ValueError("demo artifact path has inconsistent contents")
    artifacts[path] = encoded


def _paired_artifacts(
    artifacts: dict[Path, bytes],
    directory: Path,
    plan_kind: str,
    protocol: dict[str, Any],
    inputs: list[dict[str, Any]],
) -> None:
    prefix = "../" * len(directory.parts)
    manifest = {
        "schema": PAIRED_INPUT_SCHEMA,
        "protocol_sha256": protocol["protocol_sha256"],
        "plans": [
            f"{prefix}plans/{plan_kind}-b{block:02d}.json"
            for block in range(1, _BLOCKS + 1)
        ],
        "trials": [],
    }
    trials = {trial["trial_id"]: trial for trial in protocol["trials"]}
    entries = []
    for item in inputs:
        trial_id = item["trial_id"]
        trial = trials[trial_id]
        timing_name = f"b{trial['block']:02d}-{trial['condition']}-timing.json"
        result_name = f"{trial_id}-result.json"
        ledger_name = f"{trial_id}-ledger.jsonl"
        _add_json(artifacts, directory / timing_name, item["timing"])
        _add_json(artifacts, directory / result_name, item["result"])
        artifacts[directory / ledger_name] = b"".join(
            canonical_json_bytes(row) + b"\n" for row in item["ledger_rows"]
        )
        entries.append(
            {
                "trial_id": trial_id,
                "timing": timing_name,
                "result": result_name,
                "ledger": ledger_name,
                "token_certificate": None,
                "execution": item["execution"],
            }
        )
    manifest["trials"] = entries
    _add_json(artifacts, directory / "protocol.json", protocol)
    _add_json(artifacts, directory / "inputs.json", manifest)


def _build_artifacts() -> tuple[dict[Path, bytes], dict[str, Any]]:
    artifacts: dict[Path, bytes] = {}
    discovery, held_out = _plans(100), _plans(800)
    environment = {
        "kind": "FABRICATED_DEMO_DECLARATION",
        "serving_engine": None,
        "hardware": None,
    }
    reset = {"kind": "FABRICATED_DEMO_DECLARATION", "reset_was_executed": False}
    options = {
        "policy_a": "cache_only",
        "policy_b": "least_busy",
        "order_seed": 41,
        "minimum_effect_microrps": 100_000,
        "max_scheduling_lag_p95_ns": 1_000_000,
        "max_client_queue_p95_ns": 1_000_000,
        "model": "fabricated-breakpoint-demo",
        "source_revision": _SOURCE_REVISION_PLACEHOLDER,
        "environment_sha256": _digest(environment),
        "reset_procedure_sha256": _digest(reset),
    }
    for kind, plans in (("discovery", discovery), ("heldout", held_out)):
        for block, plan in enumerate(plans, 1):
            _add_json(artifacts, Path(f"plans/{kind}-b{block:02d}.json"), plan)
    search_plan = make_search_plan(
        discovery,
        comparison_options=options,
        group_sizes=[4],
        retained_spacing_bps=[0],
        max_advances_ns=[1_000_000],
        max_candidates=1,
        max_trial_slots=32,
    )
    candidate = search_plan["candidates"][0]
    protocol = candidate_protocol(search_plan, discovery, candidate["candidate_id"])
    timings = [
        {
            "baseline": make_timing(
                plan, group_size=1, retained_spacing_bps=10_000, max_advance_ns=0
            ),
            "candidate": make_timing(plan, **candidate["parameters"]),
        }
        for plan in discovery
    ]
    search_inputs = _fabricate_inputs(discovery, protocol, timings, batch=1)
    search_observations = [
        {"candidate_id": candidate["candidate_id"], "inputs": search_inputs}
    ]
    search_report = bounded.evaluate(search_plan, discovery, search_observations)
    search_source = {
        "search_plan": search_plan,
        "plans": discovery,
        "observations": search_observations,
        "report": search_report,
        "previous_report": None,
    }
    _paired_artifacts(
        artifacts, Path("search/c001"), "discovery", protocol, search_inputs
    )
    _add_json(artifacts, Path("search/plan.json"), search_plan)
    _add_json(artifacts, Path("search/report.json"), search_report)
    _add_json(
        artifacts,
        Path("search/inputs.json"),
        {
            "schema": bounded.INPUT_SCHEMA,
            "search_plan_sha256": search_plan["search_plan_sha256"],
            "plans": [
                f"../plans/discovery-b{block:02d}.json"
                for block in range(1, _BLOCKS + 1)
            ],
            "candidates": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "inputs": "c001/inputs.json",
                }
            ],
        },
    )
    reduction_plan = reducer.make_reduction_plan(
        search_source, max_comparisons=8, max_trial_slots=256
    )
    observations: list[dict[str, Any]] = []
    reduction_report = reducer.reduce(reduction_plan, search_source, observations)
    proposals = []
    batch = 1
    while reduction_report["next_action"] is not None:
        batch += 1
        action = reduction_report["next_action"]
        protocol = action["protocol"]
        inputs = _fabricate_inputs(
            discovery, protocol, reduced_timings(discovery, protocol), batch
        )
        observation = {"proposal_id": action["proposal_id"], "inputs": inputs}
        observations.append(observation)
        _paired_artifacts(
            artifacts,
            Path("reduction") / action["proposal_id"],
            "discovery",
            protocol,
            inputs,
        )
        proposals.append(
            {
                "proposal_id": action["proposal_id"],
                "inputs": f"{action['proposal_id']}/inputs.json",
            }
        )
        reduction_report = reducer.reduce(reduction_plan, search_source, observations)
    _add_json(artifacts, Path("reduction/plan.json"), reduction_plan)
    _add_json(artifacts, Path("reduction/report.json"), reduction_report)
    _add_json(
        artifacts,
        Path("reduction/inputs.json"),
        {
            "schema": reducer.INPUT_SCHEMA,
            "reduction_plan_sha256": reduction_plan["reduction_plan_sha256"],
            "proposals": proposals,
        },
    )
    _add_json(
        artifacts,
        Path("reduction/source.json"),
        {
            "schema": reducer.SOURCE_SCHEMA,
            "search_plan": "../search/plan.json",
            "inputs": "../search/inputs.json",
            "report": "../search/report.json",
            "previous_report": None,
        },
    )
    confirmation_source = {
        "search_source": search_source,
        "reduction_plan": reduction_plan,
        "observations": observations,
        "report": reduction_report,
        "previous_report": None,
    }
    confirmation_plan = confirmation.make_confirmation_plan(
        confirmation_source, held_out, order_seed=73
    )
    protocol = confirmation_plan["protocol"]
    inputs = _fabricate_inputs(
        held_out, protocol, reduced_timings(held_out, protocol), batch + 1
    )
    confirmation_report = confirmation.evaluate(
        confirmation_plan, confirmation_source, held_out, inputs
    )
    _paired_artifacts(artifacts, Path("confirmation"), "heldout", protocol, inputs)
    _add_json(artifacts, Path("confirmation/plan.json"), confirmation_plan)
    _add_json(artifacts, Path("confirmation/report.json"), confirmation_report)
    _add_json(
        artifacts,
        Path("confirmation/source.json"),
        {
            "schema": confirmation.SOURCE_SCHEMA,
            "search_source": "../reduction/source.json",
            "reduction_plan": "../reduction/plan.json",
            "inputs": "../reduction/inputs.json",
            "report": "../reduction/report.json",
            "previous_report": None,
        },
    )
    trial_count = (batch + 1) * _BLOCKS * 4
    inventory = {
        "schema": "inferdrome.breakpoint-synthetic-demo.v1",
        "evidence_class": "SYNTHETIC_ONLY",
        "evidence_eligible": False,
        "fabricated": True,
        "source_revision": _SOURCE_REVISION_PLACEHOLDER,
        "source_revision_scope": "PLACEHOLDER_NOT_AN_EXECUTED_GIT_REVISION",
        "environment": environment,
        "reset_procedure": reset,
        "discovery_blocks": _BLOCKS,
        "held_out_blocks": _BLOCKS,
        "offers_per_trial": _OFFERS,
        "fabricated_trials": trial_count,
        "fabricated_requests": trial_count * _OFFERS,
        "search_status": search_report["status"],
        "reduction_status": reduction_report["status"],
        "accepted_reductions": reduction_report["coverage"]["accepted_reductions"],
        "original_groups": reduction_report["coverage"]["source_groups"],
        "retained_groups": reduction_report["coverage"]["retained_groups"],
        "confirmation_status": confirmation_report["status"],
    }
    _add_json(artifacts, Path("demo.json"), inventory)
    return artifacts, inventory


def _readme(inventory: dict[str, Any]) -> str:
    return f"""# Breakpoint synthetic walkthrough

**SYNTHETIC_ONLY · evidence_eligible=false · all outcomes are fabricated.**

Start with [summary.md](summary.md). This deterministic example runs the artifact
verification workflow without a model server, network request or GPU experiment.
Its {inventory['fabricated_trials']} fabricated trials contain
{inventory['fabricated_requests']} fabricated request rows. Eight discovery seeds
and eight fresh held-out seeds each offer 12 requests per trial.

The fabricated signal gives the favored policy 12 SLO-good requests and the other
policy 6. The favored policy reverses between baseline and candidate timing.
{inventory['accepted_reductions']} reduction comparisons keep that fabricated signal
while shrinking the timing
mask from {inventory['original_groups']} groups to {inventory['retained_groups']}.
These numbers demonstrate the workflow; they are not policy performance results.

| Files | Inspect |
|---|---|
| [demo.json](demo.json) | Counts and explicitly fabricated execution declarations. |
| `plans/` | Eight discovery and eight held-out fixture workload plans. |
| [Search summary](search/summary.md) | One bounded candidate and verified report. |
| `search/c001/` | Protocol, timings, fabricated results, receipts and manifest. |
| [Reduction summary](reduction/summary.md) | Terminal report and every proposal. |
| `reduction/rNNN/` | Fresh fabricated raw artifacts for each reduction comparison. |
| `confirmation/` | Frozen plan, source, protocol, raw artifacts and verified report. |

The source revision is forty zeroes: an explicit placeholder, not an executed
Git revision. The model, environment, reset declarations, client clocks, router
receipts and two replica assignments are fabricated. The loopback origin in the
fixture is never contacted. No GPU provenance, causality, independent execution,
preregistration, minimality or customer acceptance is established.

From this directory, regenerate and verify the full confirmation:

```bash
inferdrome breakpoint confirm verify --plan confirmation/plan.json \\
  --source confirmation/source.json --inputs confirmation/inputs.json \\
  --report confirmation/report.json

inferdrome breakpoint summarize confirm --plan confirmation/plan.json \\
  --source confirmation/source.json --inputs confirmation/inputs.json \\
  --report confirmation/report.json --output fresh-summary.md
```

Verification rereads every supplied source and raw artifact. Summaries refuse to
replace existing files. Rerun the demo with a new output directory to regenerate
the same bytes; existing output directories are never overwritten.
An interrupted or failed generation can leave a partial directory. This guide
is written only after all three summaries have reverified their raw artifacts.
"""


def create_demo(output_dir: Path) -> dict[str, Any]:
    """Write and recheck fixtures in a new directory; failures may leave it partial."""
    # Reserve before doing work so an existing destination fails immediately.
    output_dir.mkdir(mode=0o700)
    artifacts, inventory = _build_artifacts()
    for relative, encoded in sorted(artifacts.items()):
        destination = output_dir / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(encoded)
    specifications = (
        (
            "search",
            ["--search-plan", str(output_dir / "search/plan.json")],
            "search",
            output_dir / "search/summary.md",
        ),
        (
            "reduce",
            [
                "--plan",
                str(output_dir / "reduction/plan.json"),
                "--source",
                str(output_dir / "reduction/source.json"),
            ],
            "reduction",
            output_dir / "reduction/summary.md",
        ),
        (
            "confirm",
            [
                "--plan",
                str(output_dir / "confirmation/plan.json"),
                "--source",
                str(output_dir / "confirmation/source.json"),
            ],
            "confirmation",
            output_dir / "summary.md",
        ),
    )
    for stage, flags, directory, output in specifications:
        code = summarize_report(
            [
                stage,
                *flags,
                "--inputs",
                str(output_dir / directory / "inputs.json"),
                "--report",
                str(output_dir / directory / "report.json"),
                "--output",
                str(output),
            ]
        )
        if code != 0:
            raise ValueError(f"synthetic {stage} walkthrough verification failed")
    with (output_dir / "README.md").open("x", encoding="utf-8") as stream:
        stream.write(_readme(inventory))
    return inventory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a fully fabricated SYNTHETIC_ONLY Breakpoint walkthrough"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = create_demo(args.output_dir)
    print(
        f"SYNTHETIC_ONLY: wrote {inventory['fabricated_trials']} fabricated trials "
        f"to {args.output_dir}. Read {args.output_dir / 'summary.md'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
