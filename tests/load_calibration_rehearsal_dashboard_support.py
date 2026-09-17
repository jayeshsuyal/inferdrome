"""Create one real CPU-loopback rehearsal report for the dashboard E2E suite.

This is test support only.  It starts ephemeral local fakes, runs the native
study bridge, closes every socket, and leaves only a digest-pinned report
catalog for the browser to read.  It has no GPU, provider, or credential path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from inferdrome.evaluation.faults import RoutingFaultResult
from inferdrome.evaluation.load_calibration import load_calibration_protocol_bytes
from inferdrome.evaluation.load_calibration_rehearsal import (
    CandidateStudyRecipe,
    LoopbackTwoEndpointReadinessLifecycle,
    compile_rehearsal,
    recover_pinned_confirmation_catalog,
    run_rehearsal,
    write_pinned_confirmation_catalog,
)
from inferdrome.evaluation.study import TrialResult, execute_trial
from inferdrome.evaluation.study_config import CompiledTrial
from tests.integration import test_evaluation_routing_loopback as routing_loopback
from tests.integration import test_sglang_study_loopback as sglang_loopback
from tests.integration.test_evaluation_routing_loopback import _replicas
from tests.integration.test_load_calibration_rehearsal import _protocol, _recipe_configs
from tests.integration.test_sglang_rehearsal import _profiles, _rehearsal
from tests.sglang_dashboard_support import synthetic_sglang_sources


async def _prepare_sglang(root: Path) -> dict[str, object]:
    output = root / "rehearsal"
    output.mkdir(mode=0o700)
    (root / "runs").mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as patch, synthetic_sglang_sources():
        patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
        patch.setattr(sglang_loopback, "_MODEL", "Qwen/Qwen3-8B")
        async with sglang_loopback._replicas() as (origins, replicas):
            result = await run_rehearsal(
                _rehearsal(origins),
                output,
                lifecycle=LoopbackTwoEndpointReadinessLifecycle(origins),
                sglang_profiles=_profiles(origins),
            )
            report_sha256 = write_pinned_confirmation_catalog(
                result, catalog_path=root / "original-catalog.json"
            )
            catalog = root / "dashboard-catalog.json"
            assert (
                recover_pinned_confirmation_catalog(output, catalog_path=catalog)
                == report_sha256
            )
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
    return {
        "fixture_provenance": "CPU_LOOPBACK_REHEARSAL_ONLY",
        "engine": "sglang",
        "root": str(root),
        "catalog": str(catalog),
        "report_sha256": report_sha256,
    }


async def _prepare(root: Path, *, engine: str = "vllm") -> dict[str, object]:
    if engine == "sglang":
        return await _prepare_sglang(root)
    output = root / "rehearsal"
    output.mkdir(mode=0o700)
    (root / "runs").mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routing_loopback, "_MODEL", "Qwen/Qwen3-8B")
        async with _replicas() as (origins, replicas):
            low_calibration, low_confirmation = _recipe_configs(
                origins, level_id="load-low", offered_count=7, seed=11
            )
            high_calibration, high_confirmation = _recipe_configs(
                origins, level_id="load-high", offered_count=14, seed=29
            )
            protocol = load_calibration_protocol_bytes(
                _protocol(
                    (
                        ("load-low", low_calibration, low_confirmation),
                        ("load-high", high_calibration, high_confirmation),
                    )
                )
            )
            rehearsal = compile_rehearsal(
                protocol,
                (
                    CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
                    CandidateStudyRecipe(
                        "load-high", high_calibration, high_confirmation
                    ),
                ),
            )

            async def execute_synthetic_loopback(
                trial: CompiledTrial, *, stop: asyncio.Event
            ) -> TrialResult:
                result = await execute_trial(trial, stop=stop)
                if isinstance(result, RoutingFaultResult):
                    return replace(
                        result,
                        evidence_class="SYNTHETIC_ONLY",
                        foreground=replace(
                            result.foreground, evidence_class="SYNTHETIC_ONLY"
                        ),
                        background=replace(
                            result.background, evidence_class="SYNTHETIC_ONLY"
                        ),
                    )
                return replace(
                    result,
                    evidence_class="SYNTHETIC_ONLY",
                    foreground=replace(
                        result.foreground, evidence_class="SYNTHETIC_ONLY"
                    ),
                )

            result = await run_rehearsal(
                rehearsal,
                output,
                lifecycle=LoopbackTwoEndpointReadinessLifecycle(origins),
                executor=execute_synthetic_loopback,
            )
            catalog = root / "dashboard-catalog.json"
            report_sha256 = write_pinned_confirmation_catalog(
                result, catalog_path=catalog
            )
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
    return {
        "fixture_provenance": "CPU_LOOPBACK_REHEARSAL_ONLY",
        "engine": "vllm",
        "root": str(root),
        "catalog": str(catalog),
        "report_sha256": report_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", choices=("vllm", "sglang"), default="vllm")
    args = parser.parse_args()
    root = args.root.absolute()
    if not root.is_dir() or root.is_symlink():
        raise SystemExit("dashboard fixture root must be an existing regular directory")
    print(
        json.dumps(
            asyncio.run(_prepare(root, engine=args.engine)),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
