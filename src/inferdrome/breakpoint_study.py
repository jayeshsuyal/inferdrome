"""Prepare a bounded prospective study locally; never collect serving evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inferdrome import vllm_bounded_search as bounded
from inferdrome import vllm_router_study as study
from inferdrome.errors import AdapterError
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.qwen3_tokenizer import QWEN3_TOKENIZERS_VERSION
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing, validate_timing
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA as PAIRED_INPUT_SCHEMA
from inferdrome.vllm_paired_protocol import validate_protocol
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_router_capacity import _tokenizer_lengths
from inferdrome.vllm_search_plan import candidate_protocol, make_search_plan

CONFIG_SCHEMA = "inferdrome.breakpoint-study-config.v1"
SCHEMA = "inferdrome.breakpoint-study-plan.v1"
INVENTORY_SCHEMA = "inferdrome.breakpoint-study-inventory.v1"
SOURCE_REVISION = "14df96b93ff1c476483de51d4c4e1fa9d6f10d34"
IMAGE = (
    "vastai/vllm@sha256:"
    "39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77"
)
_PINS = {
    "source_revision": SOURCE_REVISION,
    "model": QWEN3_8B_MODEL_ID,
    "model_revision": QWEN3_8B_REVISION,
    "vllm_version": "0.26.0",
    "image_reference": IMAGE,
    "tokenizers_version": QWEN3_TOKENIZERS_VERSION,
}
_SPENDING = {"decision": "UNDECIDED", "cap_usd": None}
_CONFIG_FIELDS = {
    "schema",
    "execution_authorized",
    "spending",
    "pins",
    "workload",
    "discovery_seeds",
    "heldout_seeds",
    "comparison",
    "search",
    "reduction",
    "confirmation",
    "runtime",
}
_WORKLOAD_BOUNDS = {
    "duration_s": (3, 300),
    "offers_per_trial": (1, 20_000),
    "rate_rps": (1, 64),
    "document_count": (8, 96),
    "target_prefix_tokens": (256, 6144),
    "output_tokens": (1, 256),
    "context_length": (512, 16384),
    "max_client_concurrency": (1, 1024),
    "first_content_slo_ns": (1, 60_000_000_000),
    "completion_slo_ns": (1, 60_000_000_000),
}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _hash(value: object) -> str:
    return _digest(canonical_json_bytes(value))


def _bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _types(value: object) -> None:
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int and abs(value) <= 9_007_199_254_740_991:
        return
    if isinstance(value, list):
        for item in value:
            _types(item)
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for item in value.values():
            _types(item)
        return
    raise ValueError("study artifacts require bounded integers, never floats")


def _fields(value: object, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    return value


def _equal(actual: object, expected: object, label: str) -> None:
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ValueError(f"{label} differs from the supported study contract")


def _axis(value: object, low: int, high: int, label: str) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError(f"{label} must contain one to sixteen integers")
    result = [_integer(item, low, high, label) for item in value]
    if result != sorted(set(result)):
        raise ValueError(f"{label} must be sorted and unique")
    return result


def _seeds(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 8:
        raise ValueError(f"{label} must contain exactly eight seeds")
    seeds = [_integer(item, 0, 4_294_967_295, label) for item in value]
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"{label} seeds must be distinct")
    return seeds


def _config(config: dict[str, Any]) -> None:
    _types(config)
    _fields(config, _CONFIG_FIELDS, "study config")
    _equal(config["schema"], CONFIG_SCHEMA, "config schema")
    _equal(config["execution_authorized"], False, "execution authorization")
    _equal(config["spending"], _SPENDING, "undecided spending")
    _equal(config["pins"], _PINS, "public model, runtime and source pins")
    workload = _fields(config["workload"], set(_WORKLOAD_BOUNDS), "workload")
    for name, (low, high) in _WORKLOAD_BOUNDS.items():
        _integer(workload[name], low, high, name)
    if (
        workload["offers_per_trial"] != workload["rate_rps"] * workload["duration_s"]
        or workload["target_prefix_tokens"] + workload["output_tokens"]
        > workload["context_length"]
        or workload["first_content_slo_ns"] > workload["completion_slo_ns"]
    ):
        raise ValueError("workload rate, context or SLO bounds are inconsistent")
    discovery = _seeds(config["discovery_seeds"], "discovery")
    heldout = _seeds(config["heldout_seeds"], "heldout")
    if set(discovery) & set(heldout):
        raise ValueError("discovery and heldout seeds must be disjoint")
    comparison = _fields(
        config["comparison"],
        {
            "policy_a",
            "policy_b",
            "order_seed",
            "confirmation_order_seed",
            "minimum_effect_microrps",
            "max_scheduling_lag_p95_ns",
            "max_client_queue_p95_ns",
        },
        "comparison",
    )
    if (
        comparison["policy_a"] not in study.POLICIES
        or comparison["policy_b"] not in study.POLICIES
        or comparison["policy_a"] == comparison["policy_b"]
    ):
        raise ValueError("comparison needs two distinct supported policies")
    for name in ("order_seed", "confirmation_order_seed"):
        _integer(comparison[name], 0, 4_294_967_295, name)
    _integer(
        comparison["minimum_effect_microrps"],
        1,
        workload["rate_rps"] * 1_000_000 - 1,
        "minimum effect",
    )
    for name in ("max_scheduling_lag_p95_ns", "max_client_queue_p95_ns"):
        _integer(comparison[name], 0, 60_000_000_000, name)
    search = _fields(
        config["search"],
        {
            "group_sizes",
            "retained_spacing_bps",
            "max_advances_ns",
            "max_candidates",
            "max_trial_slots",
        },
        "search",
    )
    groups = _axis(search["group_sizes"], 1, 1024, "group sizes")
    spacings = _axis(search["retained_spacing_bps"], 0, 10_000, "spacing")
    advances = _axis(
        search["max_advances_ns"], 0, workload["duration_s"] * 1_000_000_000, "advance"
    )
    if (
        not any(group > 1 for group in groups)
        or not any(spacing < 10_000 for spacing in spacings)
        or not any(advance > 0 for advance in advances)
    ):
        raise ValueError("search must include a nonidentity timing recipe")
    recipes = len(groups) * len(spacings) * len(advances)
    if recipes > 128 or recipes * 8 * workload["offers_per_trial"] > 2_000_000:
        raise ValueError("search exceeds the bounded planning work limit")
    count = _integer(search["max_candidates"], 1, recipes, "max candidates")
    _equal(search["max_trial_slots"], 32 * count, "search trial reservation")
    reduction = _fields(
        config["reduction"],
        {
            "max_proposals",
            "max_trial_slots",
        },
        "reduction",
    )
    count = _integer(reduction["max_proposals"], 1, 128, "max proposals")
    _equal(reduction["max_trial_slots"], 32 * count, "reduction trial reservation")
    _equal(
        config["confirmation"],
        {"max_attempts": 1, "max_trial_slots": 32},
        "single heldout reservation",
    )
    runtime = _fields(
        config["runtime"],
        {
            "drain_allowance_s_per_trial",
            "warm_reset_setup_allowance_s_per_trial",
            "session_overhead_s",
            "teardown_reserve_s",
            "max_session_s",
        },
        "runtime",
    )
    _equal(runtime["drain_allowance_s_per_trial"], 60, "existing client drain")
    _integer(
        runtime["warm_reset_setup_allowance_s_per_trial"],
        1,
        3600,
        "warm/reset/setup allowance",
    )
    _integer(runtime["session_overhead_s"], 1, 43_200, "session overhead")
    _integer(
        runtime["teardown_reserve_s"],
        1,
        runtime["session_overhead_s"],
        "teardown reserve included in overhead",
    )
    _integer(runtime["max_session_s"], 1, 43_200, "session limit")


def _environment(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": "INTENDED_CONFIGURATION_NOT_OBSERVED",
        "authentication": "OPERATOR_SUPPLIED_NOT_AUTHENTICATED",
        "pins": dict(config["pins"]),
        "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
        "hardware_requirement": "TWO_INDEPENDENT_A100_40GB_GPUS",
        "observed_hardware": None,
        "observed_driver": None,
        "replicas": 2,
        "dtype": "bfloat16",
        "tensor_parallel_size_per_replica": 1,
        "gpu_memory_utilization_decimal": "0.90",
        "context_length": config["workload"]["context_length"],
        "prefix_caching": True,
        "engine_host": "127.0.0.1",
        "engine_ports": [8001, 8002],
        "cuda_visible_devices_per_replica": ["0", "1"],
        "engine_environment": {"HF_HUB_OFFLINE": "1", "VLLM_SERVER_DEV_MODE": "1"},
        "request_logging": False,
        "router": {
            "host": "127.0.0.1",
            "port": 8090,
            "max_active": 128,
            "max_queue": 256,
            "queue_timeout_s": 5,
            "request_timeout_s": 120,
            "max_body_bytes": 262_144,
            "max_stream_bytes": 16_777_216,
        },
        "request": {
            "temperature": 0,
            "n": 1,
            "stream": True,
            "include_usage": True,
            "ignore_eos": True,
            "enable_thinking": False,
        },
    }


def _reset() -> dict[str, Any]:
    return {
        "scope": "INTENDED_PROCEDURE_NOT_RESET_RECEIPTS",
        "authentication": "OPERATOR_SUPPLIED_NOT_AUTHENTICATED",
        "before_every_trial": [
            "Finish the preceding full offered window and drain; stop its router.",
            "Send the disjoint startup health-check prompt directly to each engine.",
            "Check both engines have zero running and waiting requests.",
            "POST /reset_prefix_cache on each engine; require HTTP 200 "
            'and exactly {"success":true}.',
            "Check quiescence again; retain both reset responses "
            "and before/after metrics privately.",
            "Start a fresh router with the planned policy and a new empty ledger.",
            "Record reset_completed true only after observing "
            "successful checks on both engines.",
        ],
        "warmup_prompt": "Disjoint startup health check: count to two.",
        "warmup_output_tokens": 8,
        "observed_reset_receipts": None,
    }


def make_plan(config: dict[str, Any]) -> dict[str, Any]:
    """Validate prospective declarations and compute conservative slot reservations."""
    _config(config)
    config = json.loads(canonical_json_bytes(config))
    workload, runtime = config["workload"], config["runtime"]
    search_slots = config["search"]["max_trial_slots"]
    reduction_slots = config["reduction"]["max_trial_slots"]
    confirmation_slots = config["confirmation"]["max_trial_slots"]
    total = search_slots + reduction_slots + confirmation_slots
    traffic = total * workload["duration_s"]
    drain = total * runtime["drain_allowance_s_per_trial"]
    setup = total * runtime["warm_reset_setup_allowance_s_per_trial"]
    maximum = traffic + drain + setup + runtime["session_overhead_s"]
    if maximum > runtime["max_session_s"]:
        raise ValueError("trial reservations exceed the declared session limit")
    environment, reset = _environment(config), _reset()
    comparison = dict(config["comparison"])
    comparison.pop("confirmation_order_seed")
    comparison.update(
        {
            "model": config["pins"]["model"],
            "source_revision": config["pins"]["source_revision"],
            "environment_sha256": _hash(environment),
            "reset_procedure_sha256": _hash(reset),
        }
    )
    plan = {
        "schema": SCHEMA,
        "status": "PLANNED_NOT_EXECUTED",
        "execution_authorized": False,
        "spending": dict(_SPENDING),
        "config": config,
        "environment_declaration": environment,
        "reset_procedure_declaration": reset,
        "comparison_options": comparison,
        "reservations": {
            "scope": "PROSPECTIVE_RESERVATIONS_NOT_EXECUTION_OR_COST",
            "trial_slots_per_comparison": 32,
            "search_trial_slots": search_slots,
            "reduction_trial_slots": reduction_slots,
            "confirmation_trial_slots": confirmation_slots,
            "total_trial_slots": total,
            "max_offered_requests": total * workload["offers_per_trial"],
            "scheduled_traffic_s": traffic,
            "drain_allowance_s": drain,
            "warm_reset_setup_allowance_s": setup,
            "session_overhead_s": runtime["session_overhead_s"],
            "teardown_reserve_included_s": runtime["teardown_reserve_s"],
            "max_planned_session_s": maximum,
            "declared_session_limit_s": runtime["max_session_s"],
            "remaining_session_margin_s": runtime["max_session_s"] - maximum,
        },
        "required_before_execution": [
            "Explicit spending and GPU-execution authorization with a whole-run cap.",
            "Current total instance quote, storage/transfer terms and billing start.",
            "Observed GPU topology, runtime/driver, model snapshot and cache capacity.",
            "Verify the intended environment matches the host "
            "before collecting outcomes.",
            "Independent private reset/metrics receipts "
            "and fresh router for each trial.",
        ],
        "stop_rules": [
            "No execution is authorized by this plan or its preparation bundle.",
            "Collect only the next protocol returned "
            "by the verified offline controller.",
            "Stop search at its first screened-in candidate, "
            "grid exhaustion or budget exhaustion.",
            "Without a search candidate, do not reduce or confirm.",
            "Finalize each comparison once; invalid or incomplete submissions "
            "consume every reserved slot.",
            "Use at most the frozen reduction proposals and one heldout batch; "
            "do not retry confirmation.",
            "Stop collection on failed reset, provenance, token, accounting "
            "or runtime checks; retain failures.",
            "An operator must enforce future time and approved spending limits, "
            "retaining teardown reserve.",
        ],
        "limitations": [
            "This is a design, not GPU evidence, authenticated preregistration "
            "or a cost quote.",
            "Future observed durations may exceed allowances; "
            "there is no live scheduler or automatic cutoff.",
            "The chosen offered rate is a design choice, "
            "not fresh calibration or an assured reversal.",
            "Timing reduction preserves offers and denominators, "
            "not identical runtime/cache state.",
            "Search multiplicity is uncontrolled; heldout assumptions "
            "and operator declarations remain unauthenticated.",
        ],
    }
    plan["plan_sha256"] = _hash(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    _types(plan)
    if not isinstance(plan, dict) or "config" not in plan:
        raise ValueError("study plan must retain its config")
    _equal(plan, make_plan(plan["config"]), "regenerated study plan")


def _build(plan: dict[str, Any], tokenizer_root: Path) -> dict[str, bytes]:
    validate_plan(plan)
    config, settings = plan["config"], plan["config"]["workload"]
    workload = study.capacity_workload(
        tokenizer_root,
        **{
            name: settings[name]
            for name in (
                "document_count",
                "target_prefix_tokens",
                "context_length",
                "output_tokens",
            )
        },
    )
    objects: dict[str, object] = {
        "plan.json": plan,
        "environment.json": plan["environment_declaration"],
        "reset-procedure.json": plan["reset_procedure_declaration"],
    }
    discoveries: list[dict[str, Any]] = []
    discovery_paths: list[str] = []
    for population in ("discovery", "heldout"):
        for block, seed in enumerate(config[f"{population}_seeds"], 1):
            offers = study.capacity_trace(
                seed,
                settings["offers_per_trial"],
                settings["duration_s"] * 1_000_000_000,
                workload,
            )
            base = study.make_capacity_plan(
                phase="evaluation",
                seed=seed,
                count=settings["offers_per_trial"],
                duration_ns=settings["duration_s"] * 1_000_000_000,
                workload=workload,
                prompt_tokens_by_index=_tokenizer_lengths(offers, tokenizer_root),
                max_client_concurrency=settings["max_client_concurrency"],
                first_content_slo_ns=settings["first_content_slo_ns"],
                completion_slo_ns=settings["completion_slo_ns"],
            )
            certificate = study.certify_capacity_plan(base, tokenizer_root)
            study.validate_capacity_certificate(base, certificate)
            name = f"plans/{population}-b{block:02d}"
            objects[f"{name}-plan.json"] = base
            objects[f"{name}-certificate.json"] = certificate
            if population == "discovery":
                discoveries.append(base)
                discovery_paths.append(f"{name}-plan.json")
    search = make_search_plan(
        discoveries, comparison_options=plan["comparison_options"], **config["search"]
    )
    initial = bounded.evaluate(search, discoveries, [])
    if initial["status"] != "AWAITING_EVIDENCE":
        raise ValueError("prepared search must await its first eligible comparison")
    protocol = candidate_protocol(
        search, discoveries, search["candidates"][0]["candidate_id"]
    )
    validate_protocol(protocol, discoveries)
    objects.update(
        {
            "search-plan.json": search,
            "search-report.initial.json": initial,
            "search-inputs.empty.json": {
                "schema": bounded.INPUT_SCHEMA,
                "search_plan_sha256": search["search_plan_sha256"],
                "plans": discovery_paths,
                "candidates": [],
            },
            "initial/protocol.json": protocol,
            "initial/inputs.empty.json": {
                "schema": PAIRED_INPUT_SCHEMA,
                "protocol_sha256": protocol["protocol_sha256"],
                "plans": [f"../{path}" for path in discovery_paths],
                "trials": [],
            },
        }
    )
    for block, base in enumerate(discoveries, 1):
        for condition, parameters in (
            (
                "baseline",
                {"group_size": 1, "retained_spacing_bps": 10_000, "max_advance_ns": 0},
            ),
            ("candidate", protocol["candidate_parameters"]),
        ):
            timing = make_timing(base, **parameters)
            validate_timing(base, timing)
            objects[f"initial/timings/b{block:02d}-{condition}.json"] = timing
    template, commands = [], []
    for trial in protocol["trials"]:
        block, trial_id = trial["block"], trial["trial_id"]
        base_path = discovery_paths[block - 1]
        cert_path = base_path.replace("-plan.json", "-certificate.json")
        timing_path = f"initial/timings/b{block:02d}-{trial['condition']}.json"
        materialized = objects[timing_path]
        if (
            not isinstance(materialized, dict)
            or materialized["timing_sha256"] != trial["timing_sha256"]
        ):
            raise ValueError("materialized timing differs from initial protocol")
        result_path = f"../collection/c001/{trial_id}-result.json"
        ledger_path = f"../collection/c001/{trial_id}-ledger.jsonl"
        template.append(
            {
                "trial_id": trial_id,
                "timing": timing_path.removeprefix("initial/"),
                "result": f"../{result_path}",
                "ledger": f"../{ledger_path}",
                "token_certificate": f"../{cert_path}",
                "execution": {
                    **{
                        field: protocol[field]
                        for field in (
                            "source_revision",
                            "environment_sha256",
                            "reset_procedure_sha256",
                        )
                    },
                    "reset_completed": False,
                },
            }
        )
        commands.append(
            {
                "sequence": trial["sequence"],
                "trial_id": trial_id,
                "router_argv": [
                    "python",
                    "-m",
                    "inferdrome.vllm_router",
                    "--replica-a",
                    "http://127.0.0.1:8001",
                    "--replica-b",
                    "http://127.0.0.1:8002",
                    "--ledger",
                    ledger_path,
                    "--policy",
                    trial["policy"],
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8090",
                    "--max-active",
                    "128",
                    "--max-queue",
                    "256",
                ],
                "client_argv": [
                    "python",
                    "-m",
                    "inferdrome.vllm_router_study",
                    "run",
                    "--plan",
                    base_path,
                    "--token-certificate",
                    cert_path,
                    "--timing",
                    timing_path,
                    "--correlate-requests",
                    "--router",
                    "http://127.0.0.1:8090",
                    "--model",
                    protocol["model"],
                    "--policy",
                    trial["policy"],
                    "--output",
                    result_path,
                ],
            }
        )
    objects["initial/inputs.template.json"] = {
        "schema": PAIRED_INPUT_SCHEMA,
        "protocol_sha256": protocol["protocol_sha256"],
        "plans": [f"../{path}" for path in discovery_paths],
        "trials": template,
    }
    objects["initial/trial-commands.json"] = {
        "status": "NOT_EXECUTED",
        "execution_authorized": False,
        "working_directory": "PREPARED_BUNDLE_ROOT",
        "raw_output_directory": "../collection/c001",
        "instruction": (
            "Future authorized operator only: reset both engines, then start "
            "the fresh router and client in protocol order. "
            "These argv arrays are not executed by preparation."
        ),
        "trials": commands,
    }
    files = {name: _bytes(value) for name, value in objects.items()}
    files["README.md"] = _guide(plan).encode()
    return files


def _guide(plan: dict[str, Any]) -> str:
    reservations = plan["reservations"]
    return f"""# Prepared Breakpoint study — not executed

Spending remains UNDECIDED (`cap_usd=null`); execution_authorized=false.
No GPU, endpoint, cloud resource or model download was contacted.

Reserved upper bounds: {reservations["total_trial_slots"]} trials,
{reservations["max_offered_requests"]} offered requests,
{reservations["scheduled_traffic_s"]} seconds of offered windows.
The prospective total including allowances is {reservations["max_planned_session_s"]}
seconds. Teardown is included in session overhead. An operator must enforce
future approved spending/time limits; this bundle contains no scheduler.

## Verify before collection

From a checkout providing `inferdrome breakpoint study`, run:

```bash
inferdrome breakpoint study verify --prepared-dir BUNDLE \\
  --tokenizer-root PINNED_LOCAL_TOKENIZER
```

Verification retokenizes and reconstructs the exact immutable bundle inventory.
Keep this directory unchanged. Future raw results belong in the sibling
`collection/c001` directory and private archive, not this bundle.

## Future authorized collection

First satisfy every prerequisite in plan.json. The intended source, environment
and reset descriptions are declarations, not observations or reset receipts.
Review docs/BREAKPOINT_STUDY.md for startup prerequisites and adaptive steps.

The only materialized comparison is initial/protocol.json. Its 32 trials are
ordered in initial/trial-commands.json. Run those argv arrays from the bundle
root only after separate authorization. Independently reset both engines and
start a fresh router/new ledger before each trial. Keep every offered window
and drain clear before the next trial. Preserve all failures privately.

initial/inputs.template.json references those future outputs. Its reset flags
are false. Make a separate working copy, retaining its path base or updating
all relative paths; record true only after observing both successful resets.
Do not use the empty manifest as completed evidence.

Use existing `breakpoint search report` and `search verify` on collected raw
artifacts, then follow only the returned next protocol. No-hit search ends the
study. Reduction needs a reproducible selected search candidate and fresh
comparisons. Only a terminal reducer permits preparing one confirmation with
the eight retained heldout plans and the frozen confirmation order seed.
No reducer/confirmation protocol or result is invented by this preparation.

The initial AWAITING_EVIDENCE report has zero observations. Controller statuses
and exit codes do not establish a measured reversal. Public summaries require
separate review; keep raw client files, ledgers, host/reset logs and archives private.
"""


def _inventory(plan: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "status": "PREPARED_NOT_EXECUTED",
        "execution_authorized": False,
        "spending": dict(_SPENDING),
        "study_plan_sha256": plan["plan_sha256"],
        "verification_scope": "RETOKENIZED_AND_REGENERATED_ARTIFACT_CONSISTENCY",
        "files": {
            name: {"sha256": _digest(data), "bytes": len(data)}
            for name, data in sorted(files.items())
        },
    }
    inventory["inventory_sha256"] = _hash(inventory)
    return inventory


def _check_files(root: Path, expected: dict[str, bytes]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("prepared directory must be a real directory")
    expected_directories = {
        str(parent)
        for name in expected
        for parent in Path(name).parents
        if str(parent) != "."
    }
    observed_files, observed_directories = set(), set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("prepared bundle cannot contain symlinks")
        if path.is_dir():
            observed_directories.add(relative)
        elif path.is_file():
            observed_files.add(relative)
        else:
            raise ValueError("prepared bundle contains a nonregular artifact")
    if set(expected) != observed_files or expected_directories != observed_directories:
        raise ValueError("prepared bundle inventory has missing or extra entries")
    for name, data in expected.items():
        if (root / name).read_bytes() != data:
            raise ValueError(f"prepared artifact differs from regeneration: {name}")


def prepare(
    plan: dict[str, Any], tokenizer_root: Path, output_dir: Path
) -> dict[str, Any]:
    """Build and check all payloads before publishing the final success inventory."""
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("study preparation requires a new output directory")
    files = _build(plan, tokenizer_root)
    inventory = _inventory(plan, files)
    guide = files.pop("README.md")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        for name, data in files.items():
            path = output_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
        _check_files(output_dir, files)
        with (output_dir / "README.md").open("xb") as stream:
            stream.write(guide)
        files["README.md"] = guide
        _check_files(output_dir, files)
        with (output_dir / "inventory.json").open("xb") as stream:
            stream.write(_bytes(inventory))
    except BaseException:
        # Payloads may remain for inspection, but never leave a success marker.
        (output_dir / "inventory.json").unlink(missing_ok=True)
        (output_dir / "README.md").unlink(missing_ok=True)
        raise
    return inventory


def verify(prepared_dir: Path, tokenizer_root: Path) -> dict[str, Any]:
    """Retokenize retained design inputs and compare the complete byte inventory."""
    if prepared_dir.is_symlink() or not prepared_dir.is_dir():
        raise ValueError("prepared directory must be a real directory")
    plan_path = prepared_dir / "plan.json"
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ValueError("prepared design input must be a regular, nonsymlink file")
    plan = _read_json(plan_path.read_bytes())
    files = _build(plan, tokenizer_root)
    inventory = _inventory(plan, files)
    files["inventory.json"] = _bytes(inventory)
    _check_files(prepared_dir, files)
    return inventory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--config", type=Path, required=True)
    planning.add_argument("--output", type=Path, required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--plan", type=Path, required=True)
    preparation.add_argument("--tokenizer-root", type=Path, required=True)
    preparation.add_argument("--output-dir", type=Path, required=True)
    verification = commands.add_parser("verify")
    verification.add_argument("--prepared-dir", type=Path, required=True)
    verification.add_argument("--tokenizer-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = make_plan(_read_json(args.config.read_bytes()))
            with args.output.open("xb") as stream:
                stream.write(_bytes(plan))
        elif args.command == "prepare":
            prepare(
                _read_json(args.plan.read_bytes()), args.tokenizer_root, args.output_dir
            )
        else:
            verify(args.prepared_dir, args.tokenizer_root)
    except AdapterError as error:
        raise ValueError(str(error)) from error
    except ImportError as error:
        raise ValueError(
            f"local preparation requires tokenizers=={QWEN3_TOKENIZERS_VERSION}"
        ) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
