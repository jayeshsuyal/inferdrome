"""Closed cache plans, fixed assignments, condition bindings and proof gates."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path

import pytest

from inferdrome.evaluation.cache_config import (
    CACHE_CONDITIONS,
    CACHE_PLAN_RESERVE_BYTES,
    CACHE_REPORT_RESERVE_BYTES,
    CELL_METADATA_RESERVE_BYTES,
    WILLIAMS_ORDERS,
    CacheExperimentConfig,
    cache_plan_bytes,
    compile_cache_experiment,
    load_cache_config_bytes,
)
from inferdrome.evaluation.cache_workload import (
    CacheWorkloadError,
    CacheWorkloadVerification,
    WorkloadPair,
    _verify_cache_workloads,
    render_prompt,
)
from inferdrome.evaluation.contracts import MAX_INPUT_BYTES, EvaluationError
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

MS = 1_000_000


@dataclass(frozen=True)
class Encoding:
    ids: list[int]


class CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> Encoding:
        assert add_special_tokens is False
        return Encoding([ord(character) for character in text])


def synthetic_verifier(
    pairs: tuple[WorkloadPair, ...], *, block_size: int, max_tokens: int
) -> CacheWorkloadVerification:
    return _verify_cache_workloads(
        pairs,
        tokenizer=CharacterTokenizer(),
        block_size=block_size,
        max_tokens=max_tokens,
    )


def cache_payload(block_count: int = 1, case_count: int = 4) -> dict:
    return {
        "schema_version": "inferdrome.evaluation-cache-config.v1",
        "experiment_id": "public-synthetic",
        "source_commit": "a" * 40,
        "model": "Qwen/Qwen3-8B",
        "endpoints": [
            {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8000"},
            {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"},
        ],
        "bounds": {
            "max_requests": max(4, case_count),
            "concurrency": 2,
            "max_queue": 4,
            "duration_ns": 100 * MS,
            "request_timeout_ns": 50 * MS,
            "drain_ns": 20 * MS,
            "cleanup_timeout_ns": 20 * MS,
            "max_content_events": 64,
        },
        "max_tokens": 16,
        "profile": {
            "profile_id": "rehearsal",
            "load_level": "REHEARSAL",
            "window_start_ns": 10 * MS,
            "window_end_ns": 100 * MS,
            "first_content_slo_ns": 5 * MS,
            "completion_slo_ns": 10 * MS,
        },
        "blocks": [
            {
                "block_id": f"block-{index}",
                "workload_seed": 17 + index,
                "shared_document": f"{index:08d}" + "S" * 40,
                "cases": [
                    {
                        "unique_document": f"{case:08d}" + "u" * 40,
                        "suffix": f"case-{case:04d}",
                        "scheduled_ns": 10 * MS + case,
                    }
                    for case in range(case_count)
                ],
                "cell_order": list(WILLIAMS_ORDERS[index % 4]),
                "attempt_ids": [f"attempt-{index}-{cell}" for cell in range(4)],
            }
            for index in range(block_count)
        ],
        "preparation_protocol_sha256": "sha256:" + "b" * 64,
        "cache_block_size": 16,
        "max_model_len": 2048,
        "limits": {},
        "reporting": {"bootstrap_seed": 37},
    }


def load(value: dict) -> CacheExperimentConfig:
    return load_cache_config_bytes(json.dumps(value).encode("utf-8"))


@pytest.mark.parametrize("block_count", [1, 4, 8])
def test_finite_four_cell_blocks_with_fixed_assignments_and_matched_bytes(
    block_count: int,
) -> None:
    config = load(cache_payload(block_count))
    plan = compile_cache_experiment(config, verifier=synthetic_verifier)
    assert len(plan.cells) == 4 * block_count
    assert plan.planned_request_count == 16 * block_count
    assert plan.verification_status == "SYNTHETIC_TOKENIZER"
    assert plan.synthetic and not plan.executable
    assert plan.evidence_class == "SYNTHETIC_ONLY"
    assert plan.verification is not None
    assert plan.verification.tokenizer_identity is None
    for index, block in enumerate(config.blocks):
        cells = plan.cells[4 * index : 4 * index + 4]
        by_condition = {cell.condition: cell for cell in cells}
        assert tuple(cell.condition for cell in cells) == block.cell_order
        assert tuple(cell.attempt_id for cell in cells) == block.attempt_ids
        assert len({cell.cell_sha256 for cell in cells}) == 4
        for family in ("S", "U"):
            off, on = by_condition[family + "0"], by_condition[family + "1"]
            assert off.config == on.config
            assert off.config_sha256 == on.config_sha256
            assert off.workload_sha256 == on.workload_sha256
            assert off.cell_sha256 != on.cell_sha256
            assert off.mode == "DECLARED_DISABLED" and on.mode == "DECLARED_ENABLED"
            assert off.prefix_enabled is False and on.prefix_enabled is True
        for cell in cells:
            assert cell.cell_id == f"cell-{cell.index:04d}"
            assert [offer.endpoint_id for offer in cell.config.offers] == [
                "endpoint-a",
                "endpoint-b",
                "endpoint-a",
                "endpoint-b",
            ]
            assert cell.config.temperature == 0 and cell.config.enable_thinking is False
            assert cell.config_sha256 == sha256_digest(
                canonical_json_bytes(cell.config.model_dump(mode="json"))
            )
            for case_index, case in enumerate(block.cases):
                document = (
                    block.shared_document
                    if cell.workload_family == "SHARED"
                    else case.unique_document
                )
                assert cell.config.offers[case_index].prompt == render_prompt(
                    document, case.suffix
                )
                assert cell.config.offers[case_index].scheduled_ns == case.scheduled_ns
    assert plan.cells[-1].cell_id == f"cell-{4 * block_count - 1:04d}"


def test_missing_tokenizer_has_no_token_facts_and_cannot_execute(
    tmp_path: Path,
) -> None:
    config = load(cache_payload())
    no_path = compile_cache_experiment(config)
    missing = compile_cache_experiment(config, tokenizer_root=tmp_path / "missing")
    assert no_path == missing
    assert no_path.verification_status == "UNAVAILABLE"
    assert no_path.verification is None and not no_path.executable
    assert not no_path.synthetic
    assert no_path.to_dict()["verification"] is None
    assert no_path.to_dict()["cache_hits"] == "UNAVAILABLE"


def test_injected_verifier_cannot_claim_pinned_real_evidence() -> None:
    def misleading(
        pairs: tuple[WorkloadPair, ...], *, block_size: int, max_tokens: int
    ) -> CacheWorkloadVerification:
        return replace(
            synthetic_verifier(pairs, block_size=block_size, max_tokens=max_tokens),
            tokenization_status="VERIFIED_PINNED_QWEN3",
            evidence_class="LOCAL_MEASUREMENT_ONLY",
        )

    plan = compile_cache_experiment(load(cache_payload()), verifier=misleading)
    assert plan.synthetic and not plan.executable
    assert plan.verification is not None
    assert plan.verification.tokenization_status == "SYNTHETIC_TOKENIZER"
    assert plan.verification.evidence_class == "SYNTHETIC_ONLY"
    assert plan.verification.tokenizer_identity is None


def test_invalid_token_workload_is_not_downgraded_to_unavailable() -> None:
    value = cache_payload()
    value["blocks"][0]["cases"][0]["unique_document"] += "x"
    with pytest.raises(CacheWorkloadError, match="different token lengths"):
        compile_cache_experiment(load(value), verifier=synthetic_verifier)


@pytest.mark.parametrize("field", ["family_count", "input_sha256", "prompt_sha256"])
def test_verification_bound_to_every_family_and_prompt(field: str) -> None:
    def mismatched(
        pairs: tuple[WorkloadPair, ...], *, block_size: int, max_tokens: int
    ) -> CacheWorkloadVerification:
        proof = synthetic_verifier(pairs, block_size=block_size, max_tokens=max_tokens)
        if field == "family_count":
            return replace(proof, family_count=2)
        family = proof.families[0]
        if field == "input_sha256":
            family = replace(family, input_sha256="sha256:" + "0" * 64)
        else:
            case = replace(family.cases[0], shared_prompt_sha256="sha256:" + "0" * 64)
            family = replace(family, cases=(case, *family.cases[1:]))
        return replace(proof, families=(family,))

    with pytest.raises(EvaluationError, match=r"binding|planned workload"):
        compile_cache_experiment(load(cache_payload()), verifier=mismatched)


def test_tokenizer_sources_cannot_be_combined(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="one tokenizer source"):
        compile_cache_experiment(
            load(cache_payload()), tokenizer_root=tmp_path, verifier=synthetic_verifier
        )


def test_plan_is_deterministic_private_and_binds_external_condition() -> None:
    value = cache_payload()
    first = compile_cache_experiment(load(value), verifier=synthetic_verifier)
    second = compile_cache_experiment(
        load_cache_config_bytes(json.dumps(value, indent=2, sort_keys=True).encode()),
        verifier=synthetic_verifier,
    )
    assert cache_plan_bytes(first) == cache_plan_bytes(second)
    output = cache_plan_bytes(first)
    for raw in ("S" * 40, "u" * 40, "case-0000", "127.0.0.1", "Qwen/Qwen3-8B"):
        assert raw.encode() not in output
        assert raw not in repr(first)
    assert output.endswith(b"\n")
    assert first.to_dict()["evidence_eligible"] is False
    assert first.to_dict()["external_preparation_duration_ns"] == "UNAVAILABLE"
    changed = deepcopy(value)
    changed["preparation_protocol_sha256"] = "sha256:" + "c" * 64
    third = compile_cache_experiment(load(changed), verifier=synthetic_verifier)
    assert first.cells[0].config_sha256 == third.cells[0].config_sha256
    assert first.cells[0].workload_sha256 == third.cells[0].workload_sha256
    assert first.cells[0].cell_sha256 != third.cells[0].cell_sha256
    changed["endpoints"][0]["origin"] = "http://10.0.0.1:8000"
    fourth = compile_cache_experiment(load(changed), verifier=synthetic_verifier)
    assert third.cells[0].config_sha256 != fourth.cells[0].config_sha256
    assert third.cells[0].workload_sha256 == fourth.cells[0].workload_sha256


def test_williams_orders_balance_each_position_and_ordered_predecessor_pair() -> None:
    for position in range(4):
        assert Counter(order[position] for order in WILLIAMS_ORDERS) == Counter(
            CACHE_CONDITIONS
        )
    predecessors = Counter(
        pair for order in WILLIAMS_ORDERS for pair in pairwise(order)
    )
    assert len(predecessors) == 12
    assert set(predecessors.values()) == {1}
    value = cache_payload(4)
    value["blocks"].reverse()
    compile_cache_experiment(load(value))


@pytest.mark.parametrize("count", [2, 3, 5, 6, 7, 9])
def test_only_one_four_or_eight_blocks_admitted(count: int) -> None:
    with pytest.raises(EvaluationError):
        load(cache_payload(count))


@pytest.mark.parametrize("count", [4, 8])
def test_repetitions_must_use_the_balanced_williams_multiset(count: int) -> None:
    value = cache_payload(count)
    value["blocks"][1]["cell_order"] = list(value["blocks"][0]["cell_order"])
    with pytest.raises(EvaluationError):
        load(value)


def test_single_pilot_accepts_an_explicit_other_permutation() -> None:
    value = cache_payload()
    value["blocks"][0]["cell_order"] = list(reversed(CACHE_CONDITIONS))
    assert [
        cell.condition for cell in compile_cache_experiment(load(value)).cells
    ] == list(reversed(CACHE_CONDITIONS))


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("unknown",), "private-sentinel"),
        (("schema_version",), "inferdrome.evaluation-study-config.v1"),
        (("model",), "Qwen/Qwen3-0.6B"),
        (("source_commit",), "main"),
        (("experiment_id",), "../private"),
        (("cache_block_size",), 0),
        (("cache_block_size",), 257),
        (("cache_block_size",), True),
        (("max_model_len",), 2048.0),
        (("max_model_len",), 4096),
        (("max_tokens",), 2048),
        (("profile", "load_level"), "MODERATE"),
        (("profile", "window_start_ns"), 11 * MS),
        (("profile", "window_end_ns"), 99 * MS),
        (("profile", "completion_slo_ns"), 21 * MS),
        (("profile", "first_content_slo_ns"), 11 * MS),
        (("preparation_protocol_sha256",), "/private/sentinel"),
        (("bounds", "max_requests"), 4001),
        (("bounds", "concurrency"), 65),
        (("bounds", "max_queue"), 1025),
        (("blocks", 0, "workload_seed"), True),
        (("blocks", 0, "cases", 0, "scheduled_ns"), 100 * MS),
        (("blocks", 0, "cases", 0, "suffix"), ""),
        (("blocks", 0, "cases", 0, "suffix"), "\ud800"),
        (("blocks", 0, "shared_document"), "x" * 32768),
        (("blocks", 0, "cell_order"), ["S0"] * 4),
        (("blocks", 0, "attempt_ids"), ["same"] * 4),
        (("limits", "max_planned_requests"), 100001),
        (("limits", "max_duration_ns"), 86_400_000_000_001),
        (("limits", "per_cell_result_bytes"), 64 * 1024 * 1024 + 1),
        (("limits", "total_output_bytes"), 1024 * 1024 * 1024 + 1),
        (("reporting", "p99_min_successes"), 999),
        (("reporting", "minimum_complete_blocks"), 4),
    ],
)
def test_closed_scope_and_private_invalid_inputs(
    path: tuple, replacement: object
) -> None:
    value = cache_payload()
    target = value
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement
    with pytest.raises(EvaluationError) as error:
        load(value)
    assert str(error.value) == "cache configuration violates its contract"


@pytest.mark.parametrize(
    "which",
    [
        "block_id",
        "attempt_ids",
        "workload_seed",
        "shared_document",
        "unique_document",
        "suffix",
    ],
)
def test_duplicate_attribution_or_workload_rejected_without_tokenizer(
    which: str,
) -> None:
    value = cache_payload(4)
    if which in ("unique_document", "suffix"):
        value["blocks"][0]["cases"][1][which] = value["blocks"][0]["cases"][0][which]
    else:
        value["blocks"][1][which] = value["blocks"][0][which]
    with pytest.raises(EvaluationError):
        load(value)


def test_expanded_byte_and_timing_storage_bounds_apply_without_tokenizer() -> None:
    value = cache_payload(case_count=4000)
    value["blocks"][0]["shared_document"] = "s" * 1100
    for case in value["blocks"][0]["cases"]:
        case["unique_document"] += "u" * 1100
    with pytest.raises(EvaluationError):
        load(value)
    value = cache_payload()
    value["bounds"].update(max_requests=1000, max_content_events=501)
    with pytest.raises(EvaluationError):
        load(value)


def test_exact_replay_duration_request_and_complete_artifact_budget() -> None:
    value = cache_payload()
    duration = 4 * (100 + 20 + 7 * 20) * MS
    total = (
        4 * (12345 + CELL_METADATA_RESERVE_BYTES)
        + CACHE_PLAN_RESERVE_BYTES
        + CACHE_REPORT_RESERVE_BYTES
    )
    value["limits"] = {
        "max_planned_requests": 16,
        "max_duration_ns": duration,
        "per_cell_result_bytes": 12345,
        "total_output_bytes": total,
    }
    plan = compile_cache_experiment(load(value))
    assert plan.worst_case_duration_ns == duration
    assert plan.reserved_output_bytes == total
    assert plan.planned_request_count == 16
    for field in ("max_planned_requests", "max_duration_ns", "total_output_bytes"):
        smaller = deepcopy(value)
        smaller["limits"][field] -= 1
        with pytest.raises(EvaluationError):
            load(smaller)


def test_overall_request_and_storage_limits_count_all_four_cells() -> None:
    value = cache_payload(8, case_count=3125)
    assert compile_cache_experiment(load(value)).planned_request_count == 100_000
    value = cache_payload(8, case_count=3126)
    with pytest.raises(EvaluationError):
        load(value)
    value = cache_payload(4)
    value["limits"]["per_cell_result_bytes"] = 64 * 1024 * 1024
    with pytest.raises(EvaluationError):
        load(value)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b'{"blocks":[],"blocks":[]}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[" * 1000 + b"]" * 1000,
        b" " * (MAX_INPUT_BYTES + 1),
    ],
)
def test_ambiguous_or_excessive_json_fails_closed(raw: bytes) -> None:
    with pytest.raises(
        EvaluationError, match="cache configuration violates its contract"
    ):
        load_cache_config_bytes(raw)
