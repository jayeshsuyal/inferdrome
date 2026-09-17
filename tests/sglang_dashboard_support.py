"""CPU fake SGLang sessions and pinned mixed-engine dashboard fixtures.

The native SGLang metrics parser, session, typed trial writer and report reducer
run before the dashboard starts. No serving runtime, GPU or reset is exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import inferdrome.evaluation.sglang_execution as execution_module
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    build_sglang_engine_binding,
    engine_binding_bytes,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import RoutingFaultResult
from inferdrome.evaluation.healthy import HealthyRoutingResult
from inferdrome.evaluation.sglang_results import SGLangTrialResult
from inferdrome.evaluation.study import report_study, run_study
from inferdrome.evaluation.study_config import (
    CompiledStudy,
    CompiledTrial,
    compile_study,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.evaluation_dashboard_support import write_study_report
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_faults import Stream, finish
from tests.unit.test_evaluation_runner import ManualClock
from tests.unit.test_sglang_execution import SGLangProbe


@contextmanager
def synthetic_sglang_sources() -> Iterator[None]:
    """Classify fake native populations before the production v2 validator runs."""
    original = execution_module.wrap_sglang_result

    def classified(
        native: HealthyRoutingResult | RoutingFaultResult,
        binding: EvaluationEngineBinding,
        plan: CompiledStudy,
        trial: CompiledTrial,
    ) -> SGLangTrialResult:
        changes: dict[str, Any] = {
            "evidence_class": "SYNTHETIC_ONLY",
            "foreground": replace(native.foreground, evidence_class="SYNTHETIC_ONLY"),
        }
        if isinstance(native, RoutingFaultResult):
            changes["background"] = replace(
                native.background, evidence_class="SYNTHETIC_ONLY"
            )
        return original(replace(native, **changes), binding, plan, trial)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(execution_module, "wrap_sglang_result", classified)
        yield


def write_sglang_study_report(
    root: Path, *, scenario: str = "STALE_LOAD"
) -> tuple[Path, dict[str, Any]]:
    """Execute native sessions on manual clocks, then read their bound trial files."""
    root.mkdir(mode=0o700, parents=True)
    plan = compile_study(study(scenario))
    binding = build_sglang_engine_binding(plan, profiles())
    output = root / "study"

    async def run() -> None:
        study_clock = ManualClock()

        async def execute(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> SGLangTrialResult:
            assert (
                output / "engine-binding.json"
            ).read_bytes() == engine_binding_bytes(binding)
            clock = ManualClock()
            foreground = Stream(clock)
            router, observer = SGLangProbe(clock, (1, 3)), SGLangProbe(clock, (90, 0))
            background = (
                Stream(clock, block=True)
                if isinstance(trial.config, RoutingFaultConfig)
                else None
            )
            task = asyncio.create_task(
                execution_module.run_sglang_trial(
                    plan,
                    trial,
                    binding,
                    foreground,
                    router,
                    observer,
                    background_transport=background,
                    clock=clock,
                    stop=stop,
                )
            )
            await finish(clock)
            result = await task
            assert foreground.closed and router.closed and observer.closed
            assert background is None or background.closed
            assert not clock.waiters
            study_clock.advance_to(
                study_clock.elapsed_ns + result.to_dict()["elapsed_ns"]
            )
            return result

        manifest = await run_study(
            plan.config,
            output,
            executor=execute,
            clock=study_clock,
            engine_binding=binding,
        )
        assert manifest.status == "COMPLETED"
        assert not study_clock.waiters
        assert asyncio.all_tasks() == {asyncio.current_task()}

    with synthetic_sglang_sources():
        asyncio.run(run())
    report = report_study(plan.config, output, root / "reduced", engine_binding=binding)
    return root / "reduced" / "report.json", report


def write_engine_catalog(root: Path) -> dict[str, Any]:
    """Keep historical v1 and explicit SGLang v2 pins together without relabeling."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy_path, legacy = write_study_report(root / "legacy-study")
    sglang_path, sglang = write_sglang_study_report(root / "sglang-study")
    reports = {}
    entries = []
    for label, kind, path, report in (
        ("legacy-study", "STUDY", legacy_path, legacy),
        ("sglang-study", "SGLANG_STUDY", sglang_path, sglang),
    ):
        entry = {
            "kind": kind,
            "report_path": str(path),
            "expected_sha256": sha256_digest(path.read_bytes()),
        }
        entries.append(entry)
        reports[label] = {**entry, "evidence_class": report["evidence_class"]}
    catalog = root / "evaluation-reports-catalog.json"
    catalog.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
                "entries": entries,
            }
        )
        + b"\n"
    )
    catalog.chmod(0o600)
    return {
        "catalog": str(catalog),
        "reports": reports,
        "fixture_provenance": "SYNTHETIC_ONLY",
        "source_replay_by_viewer": "NOT_PERFORMED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(write_engine_catalog(arguments.root.resolve())))


if __name__ == "__main__":
    main()
