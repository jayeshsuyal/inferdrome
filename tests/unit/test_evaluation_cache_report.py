"""Offline cell bundles, all-offered cache contrasts and whole-block uncertainty."""

from __future__ import annotations

import asyncio
import json
import random
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inferdrome.evaluation import cache
from inferdrome.evaluation.cache import (
    cache_plan_bytes,
    load_cache_cell,
    load_cache_preparation_bytes,
    run_cache_cell,
)
from inferdrome.evaluation.cache_config import (
    CompiledCachePlan,
    compile_cache_experiment,
)
from inferdrome.evaluation.cache_report import (
    _output_length_differences,
    _paired_contrasts,
    report_cache_experiment,
)
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.metrics.quantiles import nearest_rank
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_cache_config import (
    MS,
    cache_payload,
    load,
    synthetic_verifier,
)
from tests.unit.test_evaluation_runner import (
    DONE,
    USAGE,
    FakeTransport,
    ManualClock,
    Script,
    advance,
    content,
    settle,
)


def _digest(label: str) -> str:
    return sha256_digest(label.encode())


def _preparation(plan: CompiledCachePlan, index: int, mutation: str | None = None):
    cell = plan.cells[index]
    value = {
        "schema_version": "inferdrome.evaluation-cache-preparation.v1",
        "plan_sha256": sha256_digest(cache_plan_bytes(plan)),
        "cell_id": cell.cell_id,
        "cell_sha256": cell.cell_sha256,
        "attempt_id": cell.attempt_id,
        "preparation_id": f"preparation-{index}",
        "preparation_protocol_sha256": plan.config.preparation_protocol_sha256,
        "config_sha256": cell.config_sha256,
        "order_position": index,
        "previous_attempt_id": plan.cells[index - 1].attempt_id if index else None,
        "runtime_recipe_sha256": _digest("common runtime"),
        "cache_block_size": plan.config.cache_block_size,
        "endpoints": [
            {
                "endpoint_id": endpoint.endpoint_id,
                "process_generation_sha256": _digest(
                    f"process {index} {endpoint.endpoint_id}"
                ),
                "prefix_caching": cell.mode,
            }
            for endpoint in cell.config.endpoints
        ],
        "initial_prefix_state": "DECLARED_ABSENT",
        "method": "DECLARED_FRESH_PROCESSES",
        "method_reference": _digest("method"),
        "model_warmup_reference": _digest("warmup"),
        "model_warmup_overlap": "DECLARED_DISJOINT",
        "chronology_reference": _digest("common campaign chronology"),
        "exclusive_traffic": "DECLARED_NO_OTHER_TRAFFIC",
    }
    if mutation == "reviewed-clear":
        value["method"] = "DECLARED_REVIEWED_CLEAR"
        for endpoint in value["endpoints"]:
            endpoint["process_generation_sha256"] = _digest(endpoint["endpoint_id"])
    elif index == 1:
        if mutation == "unknown-cache":
            value["endpoints"][0]["prefix_caching"] = "UNKNOWN"
        elif mutation == "runtime-recipe":
            value["runtime_recipe_sha256"] = _digest("different runtime")
        elif mutation == "duplicate-preparation":
            value["preparation_id"] = "preparation-0"
        elif mutation == "reused-fresh-generation":
            value["endpoints"][0]["process_generation_sha256"] = _digest(
                "process 0 endpoint-a"
            )
        elif mutation == "swapped-fresh-generations":
            value["endpoints"][0]["process_generation_sha256"] = _digest(
                "process 0 endpoint-b"
            )
            value["endpoints"][1]["process_generation_sha256"] = _digest(
                "process 0 endpoint-a"
            )
        elif mutation == "different-method":
            value["method"] = "DECLARED_REVIEWED_CLEAR"
        elif mutation == "unknown-chronology":
            value["chronology_reference"] = None
    return load_cache_preparation_bytes(json.dumps(value).encode())


def _write_cells(
    root: Path,
    plan: CompiledCachePlan,
    *,
    mutation: str | None = None,
) -> dict[str, Path]:
    async def exercise() -> dict[str, Path]:
        paths = {}
        for cell in plan.cells:
            clock, stop = ManualClock(), asyncio.Event()
            good = {"S0": 1, "S1": 4, "U0": 1, "U1": 2}[cell.condition]
            if mutation == "reject-all":
                good = 0
            scripts = {
                index: Script(
                    status=200 if index < good else 429,
                    chunks=((0, content(finish="stop") + USAGE + DONE),)
                    if index < good
                    else (),
                )
                for index in range(4)
            }
            if mutation == "drain":
                scripts[3] = Script(
                    chunks=(
                        (4 * MS, content()),
                        (6 * MS, content("", finish="stop") + USAGE + DONE),
                    )
                )
            if mutation == "cancelled" and cell.index == 1:
                stop.set()

            class FailingClose(FakeTransport):
                async def close(self) -> None:
                    await super().close()
                    raise EvaluationError("private cleanup error")

            transport = (
                FailingClose(clock, scripts)
                if mutation == "aborted" and cell.index == 1
                else FakeTransport(clock, scripts)
            )
            path = root / cell.cell_id
            task = asyncio.create_task(
                run_cache_cell(
                    plan,
                    cell.cell_id,
                    _preparation(plan, cell.index, mutation),
                    path,
                    transport=transport,
                    clock=clock,
                    stop=stop,
                )
            )
            await settle()
            times = sorted(
                {
                    offer.scheduled_ns + offset
                    for offer in cell.config.offers
                    for offset in (0, 4 * MS, 6 * MS)
                }
                | {cell.config.bounds.duration_ns, 200 * MS}
            )
            for at in times:
                if task.done():
                    break
                await advance(clock, at)
            await asyncio.wait_for(task, 1)
            assert transport.closed and transport.active == 0
            paths[cell.cell_id] = path
        return paths

    return asyncio.run(exercise())


@pytest.fixture(scope="module")
def complete_cells(tmp_path_factory: pytest.TempPathFactory):
    plan = compile_cache_experiment(load(cache_payload()), verifier=synthetic_verifier)
    return plan, _write_cells(tmp_path_factory.mktemp("cache-cells"), plan)


def test_real_synthetic_bundles_report_all_four_cells_and_exact_signed_contrasts(
    complete_cells, tmp_path: Path
) -> None:
    plan, inputs = complete_cells
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["status"] == "COMPLETED"
    assert report["comparison_status"] == "AVAILABLE"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert report["runtime_verification"] == "UNVERIFIED"
    assert report["cache_treatment_attribution"] == "UNVERIFIED"
    assert report["low_replication"] is True
    assert (
        report["coverage"]["planned_offers"]
        == report["coverage"]["returned_records"]
        == 16
    )
    assert report["preparation_consistency_issues"] == []
    family = plan.verification.families[0]
    proof = report["prefix_potential"][0]
    assert proof["shared_document_lcp_tokens"] == family.shared_document_lcp_tokens
    assert (
        proof["shared_document_complete_prefix_blocks"]
        == family.shared_document_complete_prefix_blocks
    )
    assert proof["shared_lcp_token_ids_sha256"] == family.shared_lcp_token_ids_sha256
    assert (
        proof["shared_document_lcp_token_ids_sha256"]
        == family.shared_document_lcp_token_ids_sha256
    )
    assert [
        item["mean_goodput_difference_rps"]["decimal"] for item in report["contrasts"]
    ] == ["33.333333", "11.111111", "22.222222"]
    assert all(item["interval_rps"] is None for item in report["contrasts"])
    for row in report["cells"]:
        population = row["population"]
        assert population["offered_count"] == 4
        assert population["slo_goodput_rps"]["denominator"] == 90 * MS
        assert (
            population["successful_count"]
            + population["outcomes"]["HTTP_ERROR"]["count"]
            == 4
        )
        assert (
            population["successful_latency_ns"]["scheduled_to_terminal"]["p99"] is None
        )
        assert row["endpoint_assignment"] == [
            {"endpoint_id": "endpoint-a", "planned_offers": 2, "actual_dispatches": 2},
            {"endpoint_id": "endpoint-b", "planned_offers": 2, "actual_dispatches": 2},
        ]
        assert row["reported_output_lengths"]["success"]["mean_decimal"] == "9.000000"
        assert row["reported_output_lengths"]["other_outcomes"]["mean_decimal"] is None
    encoded = (tmp_path / "report" / "report.json").read_bytes()
    assert encoded == canonical_json_bytes(report) + b"\n"
    markdown = (tmp_path / "report" / "report.md").read_text()
    assert "SYNTHETIC_ONLY" in markdown and "UNVERIFIED" in markdown
    assert "33.333333" in markdown and "22.222222" in markdown
    for secret in (
        "Qwen/Qwen3",
        "127.0.0.1",
        "S" * 48,
        "case-0000",
        "private cleanup error",
    ):
        assert secret not in encoded.decode() and secret not in markdown


@pytest.mark.parametrize("mutation", ["missing", "invalid", "cancelled", "aborted"])
def test_incomplete_cells_preserve_coverage_without_fabricated_measurements(
    mutation: str, complete_cells, tmp_path: Path
) -> None:
    plan, complete = complete_cells
    inputs = dict(complete)
    if mutation == "missing":
        inputs.pop(plan.cells[1].cell_id)
    elif mutation == "invalid":
        inputs[plan.cells[1].cell_id] = inputs[plan.cells[0].cell_id]
    else:
        inputs = _write_cells(tmp_path, plan, mutation=mutation)
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["comparison_status"] == "SUPPRESSED_INCOMPLETE"
    assert report["comparative_headline"] == "SUPPRESSED"
    assert all(
        item["mean_goodput_difference_rps"] is None and item["interval_rps"] is None
        for item in report["contrasts"]
    )
    assert report["coverage"]["planned_offers"] == 16
    failed = report["cells"][1]
    if mutation == "cancelled":
        assert report["coverage"]["returned_records"] == 16
        assert failed["population"]["slo_good_count"] == 0
        assert failed["population"]["outcomes"]["CANCELLED"]["count"] == 4
    else:
        assert report["coverage"]["returned_records"] == 12
        assert report["coverage"]["offers_without_measurements"] == 4
        assert failed["population"] is None
        assert failed["reported_output_lengths"] is None


@pytest.mark.parametrize(
    ("mutation", "issue"),
    [
        ("unknown-cache", "UNKNOWN_CACHE_MODE"),
        ("unknown-chronology", "UNKNOWN_CHRONOLOGY"),
        ("runtime-recipe", "RUNTIME_RECIPE_UNRESOLVED_OR_MISMATCHED"),
        ("duplicate-preparation", "REUSED_PREPARATION_ID"),
        ("reused-fresh-generation", "REUSED_FRESH_PROCESS_GENERATION"),
        ("swapped-fresh-generations", "REUSED_FRESH_PROCESS_GENERATION"),
        ("different-method", "PREPARATION_METHOD_MISMATCH"),
    ],
)
def test_preparation_uncertainty_and_cross_cell_mismatch_suppress_comparison(
    mutation: str, issue: str, complete_cells, tmp_path: Path
) -> None:
    plan, _ = complete_cells
    inputs = _write_cells(tmp_path, plan, mutation=mutation)
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["status"] == "COMPLETED"
    assert report["comparison_status"] == "SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH"
    assert report["coverage"]["returned_records"] == 16
    assert issue in (
        report["preparation_consistency_issues"]
        + report["cells"][1]["attribution_reasons"]
    )
    assert all(
        item["mean_goodput_difference_rps"] is None for item in report["contrasts"]
    )


def test_reviewed_clear_may_reuse_process_generations_and_common_chronology(
    complete_cells, tmp_path: Path
) -> None:
    plan, _ = complete_cells
    inputs = _write_cells(tmp_path, plan, mutation="reviewed-clear")
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["comparison_status"] == "AVAILABLE"


def test_all_server_rejections_keep_zero_goodput_and_undefined_usage_means(
    complete_cells, tmp_path: Path
) -> None:
    plan, _ = complete_cells
    inputs = _write_cells(tmp_path, plan, mutation="reject-all")
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["comparison_status"] == "AVAILABLE"
    for cell in report["cells"]:
        assert cell["population"]["slo_good_count"] == 0
        assert cell["population"]["slo_success_fraction"]["decimal"] == "0.000000"
        assert cell["population"]["outcomes"]["HTTP_ERROR"]["count"] == 4
        usage = cell["reported_output_lengths"]["other_outcomes"]
        assert usage["missing_count"] == 4 and usage["mean_decimal"] is None
    assert all(
        item["mean_goodput_difference_rps"]["decimal"] == "0.000000"
        for item in report["contrasts"]
    )
    assert report["output_length_differences"][0]["status"] == "UNAVAILABLE_USAGE"


def test_successful_completion_during_drain_uses_the_original_offered_window(
    tmp_path: Path,
) -> None:
    payload = cache_payload()
    payload["blocks"][0]["cases"][3]["scheduled_ns"] = 99 * MS
    plan = compile_cache_experiment(load(payload), verifier=synthetic_verifier)
    inputs = _write_cells(tmp_path, plan, mutation="drain")
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    for cell in report["cells"]:
        assert cell["population"]["successful_completions_during_drain"] == 1
        assert cell["population"]["slo_goodput_rps"]["denominator"] == 90 * MS
        assert (
            cell["population"]["slo_good_count"]
            == cell["population"]["successful_count"]
        )


def test_eight_complete_synthetic_blocks_keep_all_four_cells_in_the_interval(
    tmp_path: Path,
) -> None:
    plan = compile_cache_experiment(load(cache_payload(8)), verifier=synthetic_verifier)
    inputs = _write_cells(tmp_path, plan)
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["coverage"]["complete_blocks"] == 8
    assert report["low_replication"] is False
    interaction = report["contrasts"][2]
    assert interaction["interval_status"] == "DESCRIPTIVE_ONLY"
    assert interaction["interval_rps"]["lower"]["decimal"] == "22.222222"
    assert interaction["interval_rps"]["upper"]["decimal"] == "22.222222"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"


def test_output_length_differences_use_means_with_complete_usage_coverage() -> None:
    block = {
        condition: {
            "reported_output_lengths": {
                "success": {
                    "reported_count": count,
                    "total_decimal": str(total),
                    "missing_count": 0,
                }
            }
        }
        for condition, count, total in (
            ("S0", 1, 9),
            ("S1", 4, 48),
            ("U0", 2, 20),
            ("U1", 3, 24),
        )
    }
    result = _output_length_differences(block, available=True)
    assert result["shared_mean_tokens_difference_decimal"] == "3.000000"
    assert result["unique_mean_tokens_difference_decimal"] == "-2.000000"
    block["S1"]["reported_output_lengths"]["success"]["missing_count"] = 1
    result = _output_length_differences(block, available=True)
    assert result["status"] == "UNAVAILABLE_USAGE"
    assert result["shared_mean_tokens_difference_decimal"] is None


def test_mixed_evidence_never_produces_a_combined_contrast(
    complete_cells, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, inputs = complete_cells

    def mixed(plan, cell, path):
        result = load_cache_cell(plan, cell, path)
        return (
            replace(result, evidence_class="LOCAL_MEASUREMENT_ONLY")
            if cell.index == 1
            else result
        )

    monkeypatch.setattr(cache, "load_cache_cell", mixed)
    report = report_cache_experiment(plan, inputs, tmp_path / "report")
    assert report["evidence_class"] is None
    assert report["evidence_classes"] == ["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]
    assert report["comparative_headline"] == "SUPPRESSED"


def _arithmetic_blocks() -> list[dict[str, dict[str, Any]]]:
    return [
        {
            condition: {"population": {"slo_good_count": count}}
            for condition, count in {
                "S0": base,
                "S1": base + shared,
                "U0": base + 10,
                "U1": base + 10 + unique,
            }.items()
        }
        for base, shared, unique in zip(
            (100, 200, 900, 800, 400, 10, 1, 300),
            (-4, 0, 1, 2, 3, 4, 6, 9),
            (1, -2, 0, 5, 1, 3, -1, 2),
            strict=True,
        )
    ]


def test_bootstrap_resamples_whole_correlated_blocks_with_frozen_seed() -> None:
    blocks = _arithmetic_blocks()
    actual = _paired_contrasts(blocks, window_ns=1_000_000_000, seed=37, available=True)
    rng = random.Random(37)
    expected: list[int] = []
    differences = [-5, 2, 1, -3, 2, 1, 7, 7]
    for _ in range(2000):
        expected.append(sum(differences[rng.randrange(8)] for _ in range(8)))
    interaction = actual[2]
    assert interaction["mean_goodput_difference_rps"]["decimal"] == "1.500000"
    interval = interaction["interval_rps"]
    assert (
        interval["lower"]["magnitude_numerator"]
        == abs(nearest_rank(tuple(expected), 5)) * 1_000_000_000
    )
    assert interval["lower"]["denominator"] == 8_000_000_000
    assert (
        interval["upper"]["magnitude_numerator"]
        == abs(nearest_rank(tuple(expected), 95)) * 1_000_000_000
    )
    assert all(item["interval_status"] == "DESCRIPTIVE_ONLY" for item in actual)
    assert all(
        item["interval_rps"] is None
        for item in _paired_contrasts(
            blocks[:7], window_ns=1_000_000_000, seed=37, available=True
        )
    )
    assert all(
        item["mean_goodput_difference_rps"] is None
        for item in _paired_contrasts(
            blocks, window_ns=1_000_000_000, seed=37, available=False
        )
    )


def test_unknown_cell_ids_fail_before_reads_or_output(
    complete_cells, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = complete_cells

    def forbidden(*args):
        raise AssertionError("unexpected cell read")

    monkeypatch.setattr(cache, "load_cache_cell", forbidden)
    with pytest.raises(EvaluationError):
        report_cache_experiment(plan, {"arbitrary-cell": tmp_path}, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_missing_all_cells_retains_plan_evidence_and_zero_measured_coverage(
    complete_cells, tmp_path: Path
) -> None:
    plan, _ = complete_cells
    report = report_cache_experiment(plan, {}, tmp_path / "report")
    assert report["planned_evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_class"] is None and report["evidence_classes"] == []
    assert report["coverage"]["missing_cells"] == 4
    assert report["coverage"]["returned_records"] == 0
    assert report["coverage"]["offers_without_measurements"] == 16
    assert report["external_preparation_elapsed_ns"] is None
    assert "SYNTHETIC_ONLY" in (tmp_path / "report" / "report.md").read_text()


def test_each_raw_cell_is_released_before_the_next_is_loaded(
    complete_cells, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, inputs = complete_cells
    references = []

    def bounded_load(plan, cell, path):
        assert all(reference() is None for reference in references)
        result = load_cache_cell(plan, cell, path)
        references.append(weakref.ref(result))
        return result

    monkeypatch.setattr(cache, "load_cache_cell", bounded_load)
    report_cache_experiment(plan, inputs, tmp_path / "report")
    assert len(references) == 4
    assert all(reference() is None for reference in references)


def test_report_files_are_private_immutable_and_size_bounded(
    complete_cells, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, inputs = complete_cells
    output = tmp_path / "report"
    report_cache_experiment(plan, inputs, output)
    assert output.stat().st_mode & 0o077 == 0
    assert (output / "report.json").stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        report_cache_experiment(plan, inputs, output)
    monkeypatch.setattr("inferdrome.evaluation.cache_report.MAX_METADATA_BYTES", 10)
    with pytest.raises(EvaluationError):
        report_cache_experiment(plan, inputs, tmp_path / "oversized")
    assert not (tmp_path / "oversized").exists()
