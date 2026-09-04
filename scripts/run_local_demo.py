#!/usr/bin/env python3
"""Prepare and launch the reproducible local Inferdrome product demo."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
DEFAULT_WORKSPACE = Path.home() / ".inferdrome" / "local-demo"
STATE_SCHEMA_VERSION = "inferdrome.local-demo-state.v1"
SUMMARY_SCHEMA_VERSION = "inferdrome.local-demo-summary.v1"
CLAIM_BOUNDARY = "SYNTHETIC_ONLY"
PLAN_ID = "comparison-plan-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
RESULT_ID = "comparison-result-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
BASELINE_TRIAL_SET_ID = "trial-set-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CANDIDATE_TRIAL_SET_ID = "trial-set-cccccccccccccccccccccccccccccccc"
ROUTING_CAMPAIGN_ID = "routing-campaign-v1"
ROUTING_CAMPAIGN_ROOT_NAME = "routing-campaign-v1"
SCHEDULE_SEED = "0" * 64
RUN_ID_PATTERN = re.compile(r"^run-[0-9a-f]{32}$")
COMMAND_TIMEOUT_SECONDS = 300
MAX_STATE_BYTES = 1024 * 1024
EXPECTED_UNSATISFIED_CONTROLS = (
    "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT",
    "OUTCOME_COVERAGE_AND_SEMANTICS",
)


class LocalDemoError(RuntimeError):
    """Raised when the local demo cannot continue without weakening its claims."""


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "prepare a verified SYNTHETIC_ONLY comparison and launch the local "
            "Inferdrome dashboard"
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="persistent demo workspace (default: ~/.inferdrome/local-demo)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=8787,
        help="loopback dashboard port (default: 8787)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the dashboard in the default browser",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="prepare and verify the demo artifacts without starting a server",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit only a machine-readable summary (requires --prepare-only)",
    )
    return parser


def _python_executable() -> str:
    configured = os.environ.get("INFERDROME_PYTHON", "").strip()
    if configured:
        candidate = Path(configured)
        if len(candidate.parts) > 1 and not candidate.is_absolute():
            return str((REPOSITORY_ROOT / candidate).absolute())
        return configured
    project_python = REPOSITORY_ROOT / ".venv" / "bin" / "python"
    if project_python.is_file():
        return str(project_python)
    return sys.executable


def _inferdrome_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(SOURCE_ROOT), existing) if value
    )
    return environment


def _run_json_module(
    python: str,
    module: str,
    arguments: Sequence[str],
) -> dict[str, Any]:
    command_label = " ".join((module, *arguments[:2]))
    try:
        completed = subprocess.run(
            [python, "-m", module, *arguments],
            cwd=REPOSITORY_ROOT,
            env=_inferdrome_environment(),
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalDemoError(
            f"could not run Inferdrome command {command_label}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LocalDemoError(
            f"Inferdrome command {command_label} failed:\n{detail}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LocalDemoError(
            f"Inferdrome command {command_label} returned invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise LocalDemoError("Inferdrome command returned a non-object JSON value")
    return value


def _run_inferdrome_json(
    python: str,
    arguments: Sequence[str],
) -> dict[str, Any]:
    return _run_json_module(python, "inferdrome", arguments)


def _require_dashboard_runtime(python: str) -> None:
    try:
        completed = subprocess.run(
            [python, "-c", "import fastapi, inferdrome, uvicorn"],
            cwd=REPOSITORY_ROOT,
            env=_inferdrome_environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalDemoError(
            f"could not inspect the local Python runtime: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LocalDemoError(
            "the dashboard runtime is not installed; run "
            "`uv lock --check` and then "
            f"`uv sync --frozen --extra dev --extra dashboard` first ({detail})"
        )


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise LocalDemoError(f"demo metadata field {key!r} is not an object")
    return item


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise LocalDemoError(f"demo metadata field {key!r} is not a string")
    return item


def _required_bool(value: Mapping[str, Any], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise LocalDemoError(f"demo metadata field {key!r} is not a boolean")
    return item


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise LocalDemoError(f"demo metadata field {key!r} is not an integer")
    return item


def _required_strings(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or not all(
        isinstance(member, str) and member for member in item
    ):
        raise LocalDemoError(f"demo metadata field {key!r} is not a string list")
    return tuple(item)


def _validate_run_ids(run_ids: Sequence[str], *, label: str) -> tuple[str, ...]:
    values = tuple(run_ids)
    if len(values) != 2 or len(set(values)) != 2:
        raise LocalDemoError(f"{label} must contain exactly two unique run IDs")
    if not all(RUN_ID_PATTERN.fullmatch(run_id) for run_id in values):
        raise LocalDemoError(f"{label} contains an invalid run ID")
    return values


def _write_state(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".local-demo-state-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _load_state(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LocalDemoError("demo-state.json must be one regular, non-symlink file")
    if path.stat().st_size > MAX_STATE_BYTES:
        raise LocalDemoError("demo-state.json exceeds the local demo size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalDemoError(f"demo-state.json is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise LocalDemoError("demo-state.json must contain one JSON object")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise LocalDemoError("demo-state.json has an unsupported schema version")
    if value.get("claim_boundary") != CLAIM_BOUNDARY:
        raise LocalDemoError("demo-state.json does not preserve SYNTHETIC_ONLY")
    plan = _required_mapping(value, "plan")
    if _required_string(plan, "comparison_plan_id") != PLAN_ID:
        raise LocalDemoError("demo-state.json identifies a different comparison plan")
    if _required_string(plan, "baseline_trial_set_id") != BASELINE_TRIAL_SET_ID:
        raise LocalDemoError(
            "demo-state.json identifies a different baseline Trial Set"
        )
    if _required_string(plan, "candidate_trial_set_id") != CANDIDATE_TRIAL_SET_ID:
        raise LocalDemoError(
            "demo-state.json identifies a different candidate Trial Set"
        )
    baseline_run_ids = _validate_run_ids(
        _required_strings(plan, "baseline_run_ids"),
        label="baseline_run_ids",
    )
    candidate_run_ids = _validate_run_ids(
        _required_strings(plan, "candidate_run_ids"),
        label="candidate_run_ids",
    )
    if set(baseline_run_ids) & set(candidate_run_ids):
        raise LocalDemoError("demo-state.json reuses a run ID across comparison arms")
    routing_campaign = value.get("routing_campaign")
    if routing_campaign is not None:
        if not isinstance(routing_campaign, dict):
            raise LocalDemoError("demo-state.json routing_campaign is not an object")
        if _required_string(routing_campaign, "campaign_id") != ROUTING_CAMPAIGN_ID:
            raise LocalDemoError(
                "demo-state.json identifies a different routing campaign"
            )
        if (
            _required_string(routing_campaign, "package_root")
            != ROUTING_CAMPAIGN_ROOT_NAME
        ):
            raise LocalDemoError("demo-state.json identifies an unsafe routing package")
        _required_string(routing_campaign, "retained_digest")
    return value


def _new_plan_state(
    created: Mapping[str, Any],
    *,
    state_path: Path,
) -> dict[str, Any]:
    if _required_bool(created, "valid") is not True:
        raise LocalDemoError("new comparison plan was not reported valid")
    if _required_string(created, "comparison_plan_id") != PLAN_ID:
        raise LocalDemoError("new comparison plan has an unexpected identity")
    arms = _required_mapping(created, "arms")
    baseline = _required_mapping(arms, "baseline")
    candidate = _required_mapping(arms, "candidate")
    if _required_string(baseline, "planned_trial_set_id") != BASELINE_TRIAL_SET_ID:
        raise LocalDemoError("new plan has an unexpected baseline Trial Set")
    if _required_string(candidate, "planned_trial_set_id") != CANDIDATE_TRIAL_SET_ID:
        raise LocalDemoError("new plan has an unexpected candidate Trial Set")
    baseline_run_ids = _validate_run_ids(
        _required_strings(baseline, "run_ids"),
        label="baseline_run_ids",
    )
    candidate_run_ids = _validate_run_ids(
        _required_strings(candidate, "run_ids"),
        label="candidate_run_ids",
    )
    if set(baseline_run_ids) & set(candidate_run_ids):
        raise LocalDemoError("new comparison plan reused a run ID across arms")
    state: dict[str, Any] = {
        "claim_boundary": CLAIM_BOUNDARY,
        "plan": {
            "baseline_run_ids": list(baseline_run_ids),
            "baseline_trial_set_id": BASELINE_TRIAL_SET_ID,
            "candidate_run_ids": list(candidate_run_ids),
            "candidate_trial_set_id": CANDIDATE_TRIAL_SET_ID,
            "comparison_plan_digest": _required_string(
                created,
                "comparison_plan_digest",
            ),
            "comparison_plan_id": PLAN_ID,
        },
        "schema_version": STATE_SCHEMA_VERSION,
    }
    _write_state(state_path, state)
    return state


def _roots(workspace: Path) -> dict[str, Path]:
    return {
        "comparison_plans": workspace / "comparison-plans",
        "comparison_results": workspace / "comparison-results",
        "routing_campaign": workspace / ROUTING_CAMPAIGN_ROOT_NAME,
        "runs": workspace / "runs",
        "trial_sets": workspace / "trial-sets",
    }


def _routing_campaign_metadata(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = state.get("routing_campaign")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LocalDemoError("demo-state.json routing_campaign is not an object")
    if _required_string(value, "campaign_id") != ROUTING_CAMPAIGN_ID:
        raise LocalDemoError("demo-state.json identifies a different routing campaign")
    if _required_string(value, "package_root") != ROUTING_CAMPAIGN_ROOT_NAME:
        raise LocalDemoError("demo-state.json identifies an unsafe routing package")
    _required_string(value, "retained_digest")
    return value


def _prepare_routing_campaign(
    python: str,
    workspace: Path,
    roots: Mapping[str, Path],
    state: dict[str, Any],
) -> str:
    package_root = roots["routing_campaign"]
    retained = _routing_campaign_metadata(state)
    if retained is None:
        if package_root.exists() or package_root.is_symlink():
            raise LocalDemoError(
                "a routing campaign exists without its retained digest; choose a "
                "fresh --workspace because Inferdrome will not guess its digest"
            )
        campaign_source = REPOSITORY_ROOT / "campaigns" / ROUTING_CAMPAIGN_ID
        created = _run_json_module(
            python,
            "inferdrome.routing_campaign",
            [
                "run",
                "--campaign-plan",
                str(campaign_source / "stale-load-fresh-health.plan.json"),
                "--request-trace",
                str(campaign_source / "stale-load-fresh-health.trace.jsonl"),
                "--fault-schedule",
                str(campaign_source / "stale-load-fresh-health.fault-schedule.json"),
                "--trial-plan",
                str(campaign_source / "trial-plan.json"),
                "--output",
                str(package_root),
            ],
        )
        reported_root = Path(_required_string(created, "package_path")).absolute()
        if reported_root != package_root.absolute():
            raise LocalDemoError(
                "new routing campaign reported an unexpected package path"
            )
        retained_digest = _required_string(created, "retained_digest")
        state["routing_campaign"] = {
            "campaign_id": ROUTING_CAMPAIGN_ID,
            "package_root": ROUTING_CAMPAIGN_ROOT_NAME,
            "retained_digest": retained_digest,
        }
        _write_state(workspace / "demo-state.json", state)
    else:
        retained_digest = _required_string(retained, "retained_digest")

    verified = _run_json_module(
        python,
        "inferdrome.routing_campaign",
        [
            "verify",
            str(package_root),
            "--expected-digest",
            retained_digest,
        ],
    )
    if (
        _required_bool(verified, "valid") is not True
        or _required_string(verified, "retained_digest") != retained_digest
    ):
        raise LocalDemoError("retained routing campaign did not reverify exactly")
    return retained_digest


def _prepare_plan(
    python: str,
    workspace: Path,
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    state_path = workspace / "demo-state.json"
    plan_path = roots["comparison_plans"] / PLAN_ID
    if state_path.exists() or state_path.is_symlink():
        state = _load_state(state_path)
        if not plan_path.is_dir() or plan_path.is_symlink():
            raise LocalDemoError("retained demo plan is missing or is not a directory")
    else:
        if plan_path.exists() or plan_path.is_symlink():
            raise LocalDemoError(
                "a demo plan exists without retained demo-state.json; choose a fresh "
                "--workspace because Inferdrome will not guess its digest"
            )
        created = _run_inferdrome_json(
            python,
            [
                "comparison-plan",
                "create",
                "--baseline-source",
                str(REPOSITORY_ROOT / "examples" / "controlled-concurrency-2.yaml"),
                "--candidate-source",
                str(REPOSITORY_ROOT / "examples" / "controlled-concurrency-4.yaml"),
                "--title",
                "Inferdrome local product demo",
                "--hypothesis",
                "Concurrency may change attempted request throughput.",
                "--repetitions",
                "2",
                "--primary-outcome",
                "attempted_request_throughput_per_s:rate",
                "--comparison-plan-id",
                PLAN_ID,
                "--baseline-trial-set-id",
                BASELINE_TRIAL_SET_ID,
                "--candidate-trial-set-id",
                CANDIDATE_TRIAL_SET_ID,
                "--schedule-seed",
                SCHEDULE_SEED,
                "--runs-root",
                str(roots["runs"]),
                "--comparison-plans-root",
                str(roots["comparison_plans"]),
            ],
        )
        state = _new_plan_state(created, state_path=state_path)

    plan = _required_mapping(state, "plan")
    plan_digest = _required_string(plan, "comparison_plan_digest")
    verified = _run_inferdrome_json(
        python,
        [
            "comparison-plan",
            "verify",
            str(plan_path),
            "--expected-digest",
            plan_digest,
        ],
    )
    if (
        _required_bool(verified, "valid") is not True
        or _required_string(verified, "comparison_plan_id") != PLAN_ID
        or _required_string(verified, "comparison_plan_digest") != plan_digest
    ):
        raise LocalDemoError("retained comparison plan did not reverify exactly")
    return state


def _validate_execution(
    executed: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if _required_bool(executed, "valid") is not True:
        raise LocalDemoError("comparison execution did not return a valid result")
    if _required_integer(executed, "planned_run_count") != 4:
        raise LocalDemoError("local demo did not preserve its four-run schedule")
    if _required_string(executed, "comparison_plan_id") != PLAN_ID:
        raise LocalDemoError("comparison execution returned a different plan identity")
    if _required_string(executed, "comparison_result_id") != RESULT_ID:
        raise LocalDemoError(
            "comparison execution returned a different result identity"
        )
    if _required_string(executed, "status") != "INCOMPARABLE":
        raise LocalDemoError("local demo did not preserve its fail-closed result")
    unsatisfied_controls = _required_strings(executed, "unsatisfied_controls")
    if unsatisfied_controls != EXPECTED_UNSATISFIED_CONTROLS:
        raise LocalDemoError(
            "local demo returned unexpected unsatisfied comparison controls"
        )
    if _required_string(executed, "baseline_trial_set_id") != BASELINE_TRIAL_SET_ID:
        raise LocalDemoError(
            "comparison execution returned a different baseline Trial Set"
        )
    if _required_string(executed, "candidate_trial_set_id") != CANDIDATE_TRIAL_SET_ID:
        raise LocalDemoError(
            "comparison execution returned a different candidate Trial Set"
        )

    executed_run_ids = _required_strings(executed, "executed_run_ids")
    reused_run_ids = _required_strings(executed, "reused_run_ids")
    if set(executed_run_ids) & set(reused_run_ids):
        raise LocalDemoError("a local demo run was both executed and reused")
    plan = _required_mapping(state, "plan")
    planned_run_ids = {
        *_required_strings(plan, "baseline_run_ids"),
        *_required_strings(plan, "candidate_run_ids"),
    }
    if set(executed_run_ids) | set(reused_run_ids) != planned_run_ids:
        raise LocalDemoError(
            "comparison execution did not cover the exact frozen run IDs"
        )
    return executed_run_ids, reused_run_ids


def _prepare_demo(
    *,
    python: str,
    workspace: Path,
    port: int,
) -> dict[str, Any]:
    selected_workspace = workspace.expanduser()
    if selected_workspace.is_symlink():
        raise LocalDemoError("demo workspace must not be a symlink")
    workspace = selected_workspace.absolute()
    workspace.mkdir(parents=True, exist_ok=True)
    if not workspace.is_dir():
        raise LocalDemoError("demo workspace is not a directory")
    roots = _roots(workspace)
    state = _prepare_plan(python, workspace, roots)
    routing_campaign_digest = _prepare_routing_campaign(
        python,
        workspace,
        roots,
        state,
    )
    plan = _required_mapping(state, "plan")
    plan_digest = _required_string(plan, "comparison_plan_digest")
    plan_path = roots["comparison_plans"] / PLAN_ID

    executed = _run_inferdrome_json(
        python,
        [
            "comparison-plan",
            "execute",
            str(plan_path),
            "--expected-digest",
            plan_digest,
            "--baseline-source",
            str(REPOSITORY_ROOT / "examples" / "controlled-concurrency-2.yaml"),
            "--candidate-source",
            str(REPOSITORY_ROOT / "examples" / "controlled-concurrency-4.yaml"),
            "--runs-root",
            str(roots["runs"]),
            "--trial-sets-root",
            str(roots["trial_sets"]),
            "--comparison-results-root",
            str(roots["comparison_results"]),
        ],
    )
    executed_run_ids, reused_run_ids = _validate_execution(executed, state)
    result_digest = _required_string(executed, "comparison_result_digest")
    result_path = roots["comparison_results"] / RESULT_ID
    verified_result = _run_inferdrome_json(
        python,
        [
            "comparison-result",
            "verify",
            str(result_path),
            "--runs-root",
            str(roots["runs"]),
            "--trial-sets-root",
            str(roots["trial_sets"]),
            "--comparison-plans-root",
            str(roots["comparison_plans"]),
            "--expected-digest",
            result_digest,
        ],
    )
    if (
        _required_bool(verified_result, "valid") is not True
        or _required_string(verified_result, "comparison_result_id") != RESULT_ID
        or _required_string(verified_result, "comparison_result_digest")
        != result_digest
        or _required_string(verified_result, "status") != "INCOMPARABLE"
        or _required_strings(verified_result, "unsatisfied_controls")
        != EXPECTED_UNSATISFIED_CONTROLS
    ):
        raise LocalDemoError("comparison result did not independently reverify")

    baseline_run_ids = _required_strings(plan, "baseline_run_ids")
    candidate_run_ids = _required_strings(plan, "candidate_run_ids")
    featured_baseline = baseline_run_ids[0]
    featured_candidate = candidate_run_ids[0]
    state["last_verified_result"] = {
        "comparison_result_digest": result_digest,
        "comparison_result_id": RESULT_ID,
        "status": "INCOMPARABLE",
        "unsatisfied_controls": list(EXPECTED_UNSATISFIED_CONTROLS),
    }
    state["last_verified_routing_campaign"] = {
        "campaign_id": ROUTING_CAMPAIGN_ID,
        "retained_digest": routing_campaign_digest,
    }
    _write_state(workspace / "demo-state.json", state)
    return {
        "claim_boundary": CLAIM_BOUNDARY,
        "comparison": {
            "baseline_trial_set_id": BASELINE_TRIAL_SET_ID,
            "candidate_trial_set_id": CANDIDATE_TRIAL_SET_ID,
            "comparison_plan_digest": plan_digest,
            "comparison_plan_id": PLAN_ID,
            "comparison_result_digest": result_digest,
            "comparison_result_id": RESULT_ID,
            "executed_run_ids": list(executed_run_ids),
            "planned_run_count": 4,
            "reused_run_ids": list(reused_run_ids),
            "status": "INCOMPARABLE",
            "unsatisfied_controls": list(EXPECTED_UNSATISFIED_CONTROLS),
        },
        "dashboard_url": f"http://127.0.0.1:{port}",
        "recording_routes": {
            "compare": "/compare",
            "comparison": f"/comparisons/{PLAN_ID}",
            "comparisons": "/comparisons",
            "evidence": f"/evidence/{featured_candidate}",
            "run": f"/runs/{featured_candidate}",
            "runs": "/runs",
            "routing_campaign": f"/routing-campaigns/{ROUTING_CAMPAIGN_ID}",
            "routing_campaigns": "/routing-campaigns",
            "trial_set": f"/trial-sets/{CANDIDATE_TRIAL_SET_ID}",
            "trial_sets": "/trial-sets",
        },
        "routing_campaign": {
            "campaign_id": ROUTING_CAMPAIGN_ID,
            "execution_mode": "SYNTHETIC_CPU_ONLY",
            "retained_digest": routing_campaign_digest,
            "verified_by_replay": True,
        },
        "roots": {key: str(value) for key, value in roots.items()},
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "suggested_pair": {
            "baseline_run_id": featured_baseline,
            "candidate_run_id": featured_candidate,
        },
        "workspace": str(workspace),
    }


def _require_available_port(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as error:
        raise LocalDemoError(
            f"loopback port {port} is unavailable; choose another with --port"
        ) from error
    finally:
        probe.close()


def _print_handoff(summary: Mapping[str, Any]) -> None:
    comparison = _required_mapping(summary, "comparison")
    routing_campaign = _required_mapping(summary, "routing_campaign")
    executed_count = len(_required_strings(comparison, "executed_run_ids"))
    reused_count = len(_required_strings(comparison, "reused_run_ids"))
    print("\nInferdrome local product demo", flush=True)
    print("=" * 31, flush=True)
    print(
        "CLAIM BOUNDARY: SYNTHETIC_ONLY — product mechanics, not GPU proof.",
        flush=True,
    )
    print(
        f"Verified controlled result: {comparison['status']} "
        f"({executed_count} executed, {reused_count} reused)",
        flush=True,
    )
    print(
        "Expected withheld controls: "
        "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT, OUTCOME_COVERAGE_AND_SEMANTICS",
        flush=True,
    )
    print(
        "Verified routing evidence: two synthetic endpoints, one sealed replay "
        f"package ({routing_campaign['campaign_id']}).",
        flush=True,
    )
    print(f"Workspace: {summary['workspace']}", flush=True)
    print(f"Dashboard: {summary['dashboard_url']}", flush=True)


def _launch_dashboard(
    *,
    python: str,
    summary: Mapping[str, Any],
    port: int,
    open_browser: bool,
) -> NoReturn:
    roots = _required_mapping(summary, "roots")
    command = [
        python,
        "-m",
        "inferdrome",
        "dashboard",
        "--runs-root",
        _required_string(roots, "runs"),
        "--trial-sets-root",
        _required_string(roots, "trial_sets"),
        "--comparison-plans-root",
        _required_string(roots, "comparison_plans"),
        "--comparison-results-root",
        _required_string(roots, "comparison_results"),
        "--routing-campaigns-root",
        _required_string(roots, "routing_campaign"),
        "--port",
        str(port),
    ]
    if open_browser:
        command.append("--open")
    os.execvpe(python, command, _inferdrome_environment())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.json and not arguments.prepare_only:
        parser.error("--json requires --prepare-only")
    python = _python_executable()
    try:
        _require_dashboard_runtime(python)
        summary = _prepare_demo(
            python=python,
            workspace=arguments.workspace,
            port=arguments.port,
        )
        if arguments.json:
            print(json.dumps(summary, sort_keys=True))
            return 0
        _print_handoff(summary)
        if arguments.prepare_only:
            return 0
        _require_available_port(arguments.port)
        print("Press Ctrl-C to stop the loopback dashboard.\n", flush=True)
        _launch_dashboard(
            python=python,
            summary=summary,
            port=arguments.port,
            open_browser=not arguments.no_open,
        )
    except LocalDemoError as error:
        print(f"local-demo: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
