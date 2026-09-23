#!/usr/bin/env python3
"""Generate additive 0.5.15 contracts and explicitly synthetic snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inferdrome.evaluation.engine_binding import (
    SglangDirectEngineBinding,
    build_sglang_direct_binding,
    engine_binding_bytes,
)
from inferdrome.evaluation.sglang_direct_profile import build_sglang_direct_profile
from inferdrome.evaluation.sglang_direct_runtime import (
    SglangDirectRuntimeManifest,
    runtime_manifest_bytes,
    runtime_manifest_sha256,
)
from inferdrome.evaluation.sglang_report import (
    SglangDirectStudyReport,
    bind_sglang_report,
)
from inferdrome.evaluation.sglang_results import (
    _DirectTrialEnvelope,
    load_sglang_trial_bytes,
    sglang_trial_bytes,
    wrap_sglang_result,
)
from inferdrome.evaluation.study_config import compile_study
from inferdrome.evaluation.study_report import summarize_study, summarize_trial
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.sglang_direct_support import (
    direct_profiles,
    direct_study,
    synthetic_native,
    synthetic_runtime,
)
from tests.unit.test_evaluation_study_report import _manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/sglang/direct_0_5_15"
SCHEMAS = ROOT / "schemas/evaluation/sglang-direct-0.5.15"


def render_contracts() -> dict[Path, bytes]:
    manifest = synthetic_runtime()
    profiles = direct_profiles()
    config = direct_study()
    plan = compile_study(config)
    binding = build_sglang_direct_binding(
        plan, profiles, runtime_manifest_sha256=runtime_manifest_sha256(manifest)
    )
    result = {}
    for name, model in (
        ("engine-binding-v2", SglangDirectEngineBinding),
        ("trial-result-v3", _DirectTrialEnvelope),
        ("study-report-v3", SglangDirectStudyReport),
        ("runtime-manifest-v1", SglangDirectRuntimeManifest),
    ):
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        result[SCHEMAS / f"{name}.schema.json"] = (
            json.dumps(schema, indent=2, sort_keys=True) + "\n"
        ).encode()
    result[FIXTURES / "synthetic-runtime.json"] = runtime_manifest_bytes(manifest)
    result[FIXTURES / "engine-binding.json"] = engine_binding_bytes(binding)
    result[FIXTURES / "study-config.json"] = (
        canonical_json_bytes(config.model_dump(mode="json")) + b"\n"
    )
    result[FIXTURES / "launch.json"] = (
        canonical_json_bytes(
            {
                e: {
                    "argv": list(build_sglang_direct_profile(p).argv),
                    "config_sha256": build_sglang_direct_profile(p).config_sha256,
                    "producer_version": "0.5.15",
                    "evidence_eligible": False,
                }
                for e, p in profiles.items()
            }
        )
        + b"\n"
    )
    summaries = []
    for trial in plan.trials:
        wrapped = wrap_sglang_result(synthetic_native(trial), binding, plan, trial)
        content = sglang_trial_bytes(plan, trial, wrapped, binding)
        result[FIXTURES / f"{trial.trial_id}.json"] = content
        checked = load_sglang_trial_bytes(content, plan, trial, binding)
        summaries.append(summarize_trial(trial, checked._statistical_input))
    statistics = summarize_study(plan, summaries, _manifest(plan, summaries))
    result[FIXTURES / "study-report.json"] = (
        canonical_json_bytes(bind_sglang_report(statistics, binding, plan)) + b"\n"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_contracts()
    if args.check:
        stale = [
            str(p.relative_to(ROOT))
            for p, b in rendered.items()
            if not p.exists() or p.read_bytes() != b
        ]
        if stale:
            print("Stale direct SGLang contracts: " + ", ".join(stale))
            return 1
        print(f"Direct SGLang contracts current ({len(rendered)} files)")
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"Generated {len(rendered)} direct SGLang contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
