"""Prepare one fixed exploratory comparison without authorizing GPU execution."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inferdrome import breakpoint_study as preparation
from inferdrome import vllm_router_study as study
from inferdrome.errors import AdapterError
from inferdrome.qwen3_tokenizer import QWEN3_TOKENIZERS_VERSION
from inferdrome.vllm_arrival_timing import make_timing, validate_timing
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA
from inferdrome.vllm_paired_protocol import make_protocol, validate_protocol
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_router_capacity import _tokenizer_lengths

CONFIG_SCHEMA = "inferdrome.breakpoint-pilot-config.v1"
SCHEMA = "inferdrome.breakpoint-pilot-plan.v1"
INVENTORY_SCHEMA = "inferdrome.breakpoint-pilot-inventory.v1"
SCOPE = "EXPLORATORY_SINGLE_RECIPE_ONLY"
_PINS = {
    key: value for key, value in preparation._PINS.items() if key != "source_revision"
}
_SPENDING = {"decision": "UNDECIDED", "cap_usd": None}
_MAX_FILE_BYTES = 64 * 1024 * 1024


def _config(config: dict[str, Any]) -> None:
    preparation._types(config)
    preparation._fields(
        config,
        {
            "schema",
            "execution_authorized",
            "spending",
            "pins",
            "workload",
            "seeds",
            "comparison",
            "candidate_parameters",
            "runtime",
        },
        "pilot config",
    )
    preparation._equal(config["schema"], CONFIG_SCHEMA, "pilot config schema")
    preparation._equal(config["execution_authorized"], False, "execution authority")
    preparation._equal(config["spending"], _SPENDING, "undecided spending")
    preparation._equal(config["pins"], _PINS, "public runtime and model pins")
    workload = preparation._fields(
        config["workload"], set(preparation._WORKLOAD_BOUNDS), "workload"
    )
    for name, (low, high) in preparation._WORKLOAD_BOUNDS.items():
        preparation._integer(workload[name], low, high, name)
    if (
        workload["offers_per_trial"] != workload["rate_rps"] * workload["duration_s"]
        or workload["target_prefix_tokens"] + workload["output_tokens"]
        > workload["context_length"]
        or workload["first_content_slo_ns"] > workload["completion_slo_ns"]
    ):
        raise ValueError("pilot workload rate, context or SLO bounds are inconsistent")
    preparation._seeds(config["seeds"], "pilot")
    comparison = preparation._fields(
        config["comparison"],
        {
            "policy_a",
            "policy_b",
            "order_seed",
            "minimum_effect_microrps",
            "max_scheduling_lag_p95_ns",
            "max_client_queue_p95_ns",
        },
        "pilot comparison",
    )
    if (
        comparison["policy_a"] not in study.POLICIES
        or comparison["policy_b"] not in study.POLICIES
        or comparison["policy_a"] == comparison["policy_b"]
    ):
        raise ValueError("pilot needs two distinct supported policies")
    preparation._integer(comparison["order_seed"], 0, 2**32 - 1, "order seed")
    preparation._integer(
        comparison["minimum_effect_microrps"],
        1,
        workload["rate_rps"] * 1_000_000 - 1,
        "minimum effect",
    )
    for name in ("max_scheduling_lag_p95_ns", "max_client_queue_p95_ns"):
        preparation._integer(comparison[name], 0, 60_000_000_000, name)
    recipe = preparation._fields(
        config["candidate_parameters"],
        {"group_size", "retained_spacing_bps", "max_advance_ns"},
        "timing recipe",
    )
    preparation._integer(recipe["group_size"], 2, 1024, "group size")
    preparation._integer(recipe["retained_spacing_bps"], 0, 9999, "spacing")
    preparation._integer(
        recipe["max_advance_ns"],
        1,
        workload["duration_s"] * 1_000_000_000,
        "maximum advance",
    )
    runtime = preparation._fields(
        config["runtime"],
        {
            "drain_allowance_s_per_trial",
            "warm_reset_setup_allowance_s_per_trial",
            "session_overhead_s",
            "teardown_reserve_s",
            "max_session_s",
        },
        "pilot runtime",
    )
    preparation._equal(runtime["drain_allowance_s_per_trial"], 60, "client drain")
    preparation._integer(
        runtime["warm_reset_setup_allowance_s_per_trial"], 1, 3600, "trial setup"
    )
    preparation._integer(runtime["session_overhead_s"], 1, 12600, "session overhead")
    preparation._integer(
        runtime["teardown_reserve_s"],
        1,
        runtime["session_overhead_s"],
        "teardown included in overhead",
    )
    preparation._integer(runtime["max_session_s"], 1, 12600, "pilot session limit")


def make_plan(config: dict[str, Any], source_revision: str) -> dict[str, Any]:
    """Bind an explicit collection source revision to one prospective comparison."""
    _config(config)
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
        or source_revision == "0" * 40
    ):
        raise ValueError(
            "source revision must be an explicit nonzero lowercase Git SHA"
        )
    config = json.loads(preparation._bytes(config))
    workload, runtime = config["workload"], config["runtime"]
    slots = 4 * len(config["seeds"])
    traffic = slots * workload["duration_s"]
    drain = slots * runtime["drain_allowance_s_per_trial"]
    setup = slots * runtime["warm_reset_setup_allowance_s_per_trial"]
    maximum = traffic + drain + setup + runtime["session_overhead_s"]
    if maximum > runtime["max_session_s"]:
        raise ValueError("pilot reservations exceed the declared session limit")
    declarations = {
        **config,
        "pins": {**config["pins"], "source_revision": source_revision},
    }
    environment = preparation._environment(declarations)
    reset = preparation._reset()
    plan = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "status": "PLANNED_NOT_EXECUTED",
        "execution_authorized": False,
        "spending": dict(_SPENDING),
        "source_revision": source_revision,
        "config": config,
        "environment_declaration": environment,
        "reset_procedure_declaration": reset,
        "comparison_options": {
            **config["comparison"],
            "candidate_parameters": config["candidate_parameters"],
            "model": config["pins"]["model"],
            "source_revision": source_revision,
            "environment_sha256": preparation._hash(environment),
            "reset_procedure_sha256": preparation._hash(reset),
        },
        "reservations": {
            "scope": "PROSPECTIVE_RESERVATIONS_NOT_EXECUTION_OR_COST",
            "total_trial_slots": slots,
            "max_offered_requests": slots * workload["offers_per_trial"],
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
            "Separate explicit GPU-execution approval and whole-session spending cap.",
            "Current quote, exact instance identity, billing origin "
            "and outside-host teardown.",
            "Clean checkout at the declared revision; "
            "verified model, runtime and two GPUs.",
            "Retokenizing bundle verification before any serving or process startup.",
            "Observed reset receipts and a fresh router for every frozen trial.",
        ],
        "limitations": [
            "One recipe selected before outcomes; "
            "no search, reduction or held-out confirmation.",
            "This plan is not execution evidence, a price quote "
            "or authenticated preregistration.",
            "Evaluation is the existing workload schema phase; "
            "this pilot remains exploratory.",
            "No extra calibration or replacement trials are reserved; "
            "retain failures and stop.",
            "Time allowances are unmeasured; "
            "stopping traffic does not terminate cloud billing.",
        ],
    }
    plan["plan_sha256"] = preparation._hash(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    preparation._types(plan)
    if not isinstance(plan, dict) or not {"config", "source_revision"} <= plan.keys():
        raise ValueError("pilot plan must retain config and explicit source revision")
    preparation._equal(
        plan,
        make_plan(plan["config"], plan["source_revision"]),
        "regenerated pilot plan",
    )


def _guide(plan: dict[str, Any]) -> str:
    return f"""# Prepared exploratory pilot — not executed

One fixed recipe, eight blocks, 32 trials. No held-out confirmation is claimed.
Spending remains UNDECIDED and execution_authorized=false.
Source revision: {plan["source_revision"]}.

Run `inferdrome breakpoint pilot verify --prepared-dir BUNDLE --tokenizer-dir TOKENIZER`
before collection. Verification retokenizes and regenerates all immutable artifacts.
Keep this bundle unchanged. Future collected outputs belong in a separate private
directory; never replace preparation artifacts with results. inputs.template.json
uses paths relative to this bundle and points at ../collection; its reset flags
are false until successful reset checks are actually observed.

The proposed allowance is {plan["reservations"]["max_planned_session_s"]} seconds
inside a {plan["reservations"]["declared_session_limit_s"]}-second rental ceiling.
These are planning limits, not observed timings or cloud spending enforcement.
Do not rent, launch, or collect from this packet without separate authorization.
See docs/BREAKPOINT_PILOT.md for the collection and cleanup procedure.
"""


def _assemble(
    plan: dict[str, Any],
    plans: list[dict[str, Any]],
    certificates: list[dict[str, Any]],
) -> dict[str, bytes]:
    validate_plan(plan)
    settings, config = plan["config"]["workload"], plan["config"]
    if len(plans) != 8 or len(certificates) != 8:
        raise ValueError("pilot bundle requires eight certified workload plans")
    workload = plans[0].get("workload")
    if not isinstance(workload, dict):
        raise ValueError("pilot workload must be an object")
    for field in (
        "document_count",
        "target_prefix_tokens",
        "output_tokens",
        "context_length",
    ):
        preparation._equal(workload.get(field), settings[field], f"workload {field}")
    objects: dict[str, object] = {
        "pilot-plan.json": plan,
        "environment.json": plan["environment_declaration"],
        "reset-procedure.json": plan["reset_procedure_declaration"],
    }
    for block, (base, certificate, seed) in enumerate(
        zip(plans, certificates, config["seeds"], strict=True), 1
    ):
        study.validate_capacity_plan(base)
        study.validate_capacity_certificate(base, certificate)
        expected = study.make_capacity_plan(
            phase="evaluation",
            seed=seed,
            count=settings["offers_per_trial"],
            duration_ns=settings["duration_s"] * 1_000_000_000,
            workload=workload,
            prompt_tokens_by_index=base["prompt_tokens_by_index"],
            max_client_concurrency=settings["max_client_concurrency"],
            first_content_slo_ns=settings["first_content_slo_ns"],
            completion_slo_ns=settings["completion_slo_ns"],
        )
        preparation._equal(base, expected, "retained pilot workload plan")
        objects[f"plans/b{block:02d}-plan.json"] = base
        objects[f"plans/b{block:02d}-certificate.json"] = certificate
    protocol = make_protocol(plans, **plan["comparison_options"])
    validate_protocol(protocol, plans)
    objects["protocol.json"] = protocol
    for block, base in enumerate(plans, 1):
        for condition, parameters in (
            (
                "baseline",
                {"group_size": 1, "retained_spacing_bps": 10000, "max_advance_ns": 0},
            ),
            ("candidate", config["candidate_parameters"]),
        ):
            timing = make_timing(base, **parameters)
            validate_timing(base, timing)
            objects[f"timings/b{block:02d}-{condition}.json"] = timing
    inputs = {
        "schema": INPUT_SCHEMA,
        "protocol_sha256": protocol["protocol_sha256"],
        "plans": [f"plans/b{block:02d}-plan.json" for block in range(1, 9)],
        "trials": [],
    }
    objects["inputs.empty.json"] = inputs
    objects["inputs.template.json"] = {
        **inputs,
        "trials": [
            {
                "trial_id": trial["trial_id"],
                "timing": f"timings/b{trial['block']:02d}-{trial['condition']}.json",
                "result": f"../collection/{trial['trial_id']}-result.json",
                "ledger": f"../collection/{trial['trial_id']}-ledger.jsonl",
                "token_certificate": f"plans/b{trial['block']:02d}-certificate.json",
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
            for trial in protocol["trials"]
        ],
    }
    files = {name: preparation._bytes(value) for name, value in objects.items()}
    files["README.md"] = _guide(plan).encode()
    return files


def _build(plan: dict[str, Any], tokenizer_root: Path) -> dict[str, bytes]:
    validate_plan(plan)
    settings = plan["config"]["workload"]
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
    plans, certificates = [], []
    for seed in plan["config"]["seeds"]:
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
        plans.append(base)
        certificates.append(study.certify_capacity_plan(base, tokenizer_root))
    return _assemble(plan, plans, certificates)


def _inventory(plan: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "scope": SCOPE,
        "status": "PREPARED_NOT_EXECUTED",
        "execution_authorized": False,
        "spending": dict(_SPENDING),
        "pilot_plan_sha256": plan["plan_sha256"],
        "verification_scope": "RETOKENIZED_AND_REGENERATED_ARTIFACT_CONSISTENCY",
        "files": {
            name: {"sha256": preparation._digest(data), "bytes": len(data)}
            for name, data in sorted(files.items())
        },
    }
    inventory["inventory_sha256"] = preparation._hash(inventory)
    return inventory


def prepare(
    plan: dict[str, Any], tokenizer_root: Path, output_dir: Path
) -> dict[str, Any]:
    """Write a private exclusive bundle, publishing its inventory only after checks."""
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("pilot preparation requires a new output directory")
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
        preparation._check_files(output_dir, files)
        with (output_dir / "README.md").open("xb") as stream:
            stream.write(guide)
        files["README.md"] = guide
        preparation._check_files(output_dir, files)
        with (output_dir / "inventory.json").open("xb") as stream:
            stream.write(preparation._bytes(inventory))
    except BaseException:
        (output_dir / "README.md").unlink(missing_ok=True)
        (output_dir / "inventory.json").unlink(missing_ok=True)
        raise
    return inventory


def _read(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if any((root / parent).is_symlink() for parent in Path(name).parents):
        raise ValueError("pilot artifacts cannot have symlink directory parents")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("pilot artifacts must be bounded regular nonsymlink files")
    try:
        value = _read_json(path.read_bytes())
    except RecursionError as error:
        raise ValueError("pilot artifact nesting exceeds supported bounds") from error
    if not isinstance(value, dict):
        raise ValueError("pilot artifact must be an object")
    return value


def _bounded_tree(prepared_dir: Path) -> None:
    if prepared_dir.is_symlink() or not prepared_dir.is_dir():
        raise ValueError("prepared pilot must be a real directory")
    total = 0
    for count, path in enumerate(prepared_dir.rglob("*"), 1):
        if count > 64 or path.is_symlink():
            raise ValueError("pilot bundle exceeds entry bounds or contains symlinks")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("pilot bundle contains an oversized or nonregular file")
        total += path.stat().st_size
        if total > 256 * 1024 * 1024:
            raise ValueError("pilot bundle exceeds total size bound")


def load_prepared(prepared_dir: Path) -> dict[str, Any]:
    """Check retained bindings and byte inventory; this does not retokenize inputs."""
    try:
        return _load_prepared(prepared_dir)
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed pilot preparation bundle") from error


def _load_prepared(prepared_dir: Path) -> dict[str, Any]:
    _bounded_tree(prepared_dir)
    plan = _read(prepared_dir, "pilot-plan.json")
    plans = [
        _read(prepared_dir, f"plans/b{block:02d}-plan.json") for block in range(1, 9)
    ]
    certificates = [
        _read(prepared_dir, f"plans/b{block:02d}-certificate.json")
        for block in range(1, 9)
    ]
    files = _assemble(plan, plans, certificates)
    inventory = _inventory(plan, files)
    files["inventory.json"] = preparation._bytes(inventory)
    preparation._check_files(prepared_dir, files)
    return {
        "plan": plan,
        "plans": plans,
        "certificates": certificates,
        "protocol": _read(prepared_dir, "protocol.json"),
        "timings": [
            {
                condition: _read(prepared_dir, f"timings/b{block:02d}-{condition}.json")
                for condition in ("baseline", "candidate")
            }
            for block in range(1, 9)
        ],
        "inputs_template": _read(prepared_dir, "inputs.template.json"),
        "inventory": inventory,
        "load_verification_scope": "ARTIFACT_BINDINGS_ONLY_NOT_RETOKENIZED",
    }


def verify(prepared_dir: Path, tokenizer_root: Path) -> dict[str, Any]:
    """Retokenize all inputs and regenerate the complete preparation inventory."""
    _bounded_tree(prepared_dir)
    plan = _read(prepared_dir, "pilot-plan.json")
    files = _build(plan, tokenizer_root)
    inventory = _inventory(plan, files)
    files["inventory.json"] = preparation._bytes(inventory)
    preparation._check_files(prepared_dir, files)
    return inventory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Other pilot commands: rehearse, run, guard, export, verify-export. "
            "Use inferdrome breakpoint pilot COMMAND --help for each command."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--config", type=Path, required=True)
    planning.add_argument("--source-revision", required=True)
    planning.add_argument("--output", type=Path, required=True)
    preparation_command = commands.add_parser("prepare")
    preparation_command.add_argument("--plan", type=Path, required=True)
    preparation_command.add_argument("--tokenizer-dir", type=Path, required=True)
    preparation_command.add_argument("--output-dir", type=Path, required=True)
    verification = commands.add_parser("verify")
    verification.add_argument("--prepared-dir", type=Path, required=True)
    verification.add_argument("--tokenizer-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = make_plan(
                _read(args.config.parent, args.config.name), args.source_revision
            )
            with args.output.open("xb") as stream:
                stream.write(preparation._bytes(plan))
        elif args.command == "prepare":
            prepare(
                _read(args.plan.parent, args.plan.name),
                args.tokenizer_dir,
                args.output_dir,
            )
        else:
            verify(args.prepared_dir, args.tokenizer_dir)
    except AdapterError as error:
        raise ValueError(str(error)) from error
    except ImportError as error:
        raise ValueError(
            f"local preparation requires tokenizers=={QWEN3_TOKENIZERS_VERSION}"
        ) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
