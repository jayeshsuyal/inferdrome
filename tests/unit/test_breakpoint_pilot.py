"""Strict exploratory pilot preparation without serving or fabricated evidence."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inferdrome import breakpoint_pilot as pilot
from inferdrome import vllm_router_study as study

SOURCE = "a" * 40


def _config() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parents[2] / "examples/breakpoint-qwen3-pilot.json").read_text()
    )


def _small() -> dict[str, Any]:
    config = _config()
    config["workload"].update(
        duration_s=3,
        offers_per_trial=12,
        rate_rps=4,
        document_count=8,
        target_prefix_tokens=256,
        output_tokens=4,
        context_length=512,
    )
    return config


class WordTokenizer:
    """Test-only tokenization substitute; never an actual Qwen3 certificate."""

    def encode(self, text: str, *, add_special_tokens: bool) -> SimpleNamespace:
        assert add_special_tokens is False
        return SimpleNamespace(ids=text.split())


@pytest.fixture
def tokenizer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(study, "_capacity_tokenizer", lambda _root: WordTokenizer())
    return tmp_path / "test-only-tokenizer"


@pytest.fixture
def bundle(tmp_path: Path, tokenizer: Path) -> tuple[Path, Path]:
    root = tmp_path / "prepared"
    pilot.prepare(pilot.make_plan(_small(), SOURCE), tokenizer, root)
    return root, tokenizer


def test_exact_single_recipe_budget_and_revision_binding() -> None:
    config = _config()
    plan = pilot.make_plan(config, SOURCE)
    assert plan == pilot.make_plan(copy.deepcopy(config), SOURCE)
    pilot.validate_plan(plan)
    assert plan["reservations"] == {
        "scope": "PROSPECTIVE_RESERVATIONS_NOT_EXECUTION_OR_COST",
        "total_trial_slots": 32,
        "max_offered_requests": 23040,
        "scheduled_traffic_s": 3840,
        "drain_allowance_s": 1920,
        "warm_reset_setup_allowance_s": 1920,
        "session_overhead_s": 3600,
        "teardown_reserve_included_s": 600,
        "max_planned_session_s": 11280,
        "declared_session_limit_s": 12600,
        "remaining_session_margin_s": 1320,
    }
    assert plan["scope"] == "EXPLORATORY_SINGLE_RECIPE_ONLY"
    assert plan["execution_authorized"] is False
    assert plan["spending"] == {"decision": "UNDECIDED", "cap_usd": None}
    assert (
        plan["source_revision"]
        == plan["comparison_options"]["source_revision"]
        == SOURCE
    )
    assert plan["environment_declaration"]["pins"]["source_revision"] == SOURCE
    assert "source_revision" not in config["pins"]
    assert plan["config"]["candidate_parameters"] == {
        "group_size": 8,
        "retained_spacing_bps": 2500,
        "max_advance_ns": 1000000000,
    }
    assert not {"search", "reduction", "confirmation", "heldout_seeds"} & config.keys()


@pytest.mark.parametrize("source", ["main", "0" * 40, "A" * 40, "a" * 39, 1, None])
def test_source_must_be_explicit_full_nonzero_revision(source: Any) -> None:
    with pytest.raises(ValueError, match="source revision"):
        pilot.make_plan(_config(), source)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("execution_authorized",), True),
        (("execution_authorized",), 0),
        (("spending", "cap_usd"), 20),
        (("spending", "decision"), "APPROVED"),
        (("workload", "rate_rps"), True),
        (("workload", "rate_rps"), 6.0),
        (("workload", "rate_rps"), float("nan")),
        (("workload", "rate_rps"), float("inf")),
        (("workload", "offers_per_trial"), 721),
        (("pins", "model_revision"), "b" * 40),
        (("pins", "image_reference"), "latest"),
        (("pins", "tokenizers_version"), "0.21.0"),
        (("comparison", "policy_b"), "cache_only"),
        (("comparison", "minimum_effect_microrps"), 0),
        (("comparison", "order_seed"), True),
        (("candidate_parameters", "group_size"), 1),
        (("candidate_parameters", "retained_spacing_bps"), 10000),
        (("candidate_parameters", "max_advance_ns"), 0),
        (("runtime", "max_session_s"), 11279),
        (("runtime", "max_session_s"), 12601),
        (("runtime", "drain_allowance_s_per_trial"), 0),
        (("runtime", "teardown_reserve_s"), 3601),
    ],
)
def test_strict_config_rejects_ambiguous_or_unbounded_designs(
    path: tuple[str, ...], value: Any
) -> None:
    config = _config()
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        pilot.make_plan(config, SOURCE)


@pytest.mark.parametrize("change", ["unknown", "missing", "short", "duplicate", "bool"])
def test_exact_keys_and_eight_distinct_seeds(change: str) -> None:
    config = _config()
    if change == "unknown":
        config["search"] = {}
    elif change == "missing":
        del config["runtime"]
    elif change == "short":
        config["seeds"].pop()
    elif change == "duplicate":
        config["seeds"][1] = config["seeds"][0]
    else:
        config["seeds"][0] = True
    with pytest.raises(ValueError):
        pilot.make_plan(config, SOURCE)


def test_prepare_and_retokenizing_verify_are_offline(
    tmp_path: Path, tokenizer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("offline preparation contacted serving or process APIs")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(study, "run_trial", forbidden)
    root = tmp_path / "prepared"
    plan = pilot.make_plan(_small(), SOURCE)
    inventory = pilot.prepare(plan, tokenizer, root)
    assert pilot.verify(root, tokenizer) == inventory
    retained = pilot.load_prepared(root)
    assert retained["inventory"] == inventory
    assert (
        retained["load_verification_scope"] == "ARTIFACT_BINDINGS_ONLY_NOT_RETOKENIZED"
    )
    assert len(retained["plans"]) == len(retained["certificates"]) == 8
    assert len(retained["protocol"]["trials"]) == 32
    assert retained["protocol"]["source_revision"] == SOURCE
    assert retained["protocol"]["scope"] == "EXPLORATORY_SEARCH_ONLY"
    assert all(base["phase"] == "evaluation" for base in retained["plans"])
    assert all(
        not row["execution"]["reset_completed"]
        for row in retained["inputs_template"]["trials"]
    )
    assert not list(root.rglob("*-result.json"))
    assert root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="new output"):
        pilot.prepare(plan, tokenizer, root)


@pytest.mark.parametrize(
    "change", ["timing", "certificate", "plan", "extra", "missing", "symlink"]
)
def test_rehashed_tampering_missing_extra_and_symlink_rejected(
    bundle: tuple[Path, Path], change: str
) -> None:
    root, tokenizer = bundle
    if change == "extra":
        (root / "results.json").write_text("{}")
    elif change == "missing":
        (root / "inputs.empty.json").unlink()
    elif change == "symlink":
        path = root / "inputs.empty.json"
        content = path.read_bytes()
        path.unlink()
        (root.parent / "external.json").write_bytes(content)
        path.symlink_to(root.parent / "external.json")
    else:
        relative, field = {
            "timing": ("timings/b01-candidate.json", "timing_sha256"),
            "certificate": ("plans/b01-certificate.json", "certificate_sha256"),
            "plan": ("pilot-plan.json", "plan_sha256"),
        }[change]
        path = root / relative
        value = json.loads(path.read_text())
        if change == "timing":
            value["moved_count"] += 1
        elif change == "certificate":
            value["prompt_tokens_min"] += 1
        else:
            value["reservations"]["total_trial_slots"] = 1
        value[field] = pilot.preparation._hash(
            {k: v for k, v in value.items() if k != field}
        )
        path.write_bytes(pilot.preparation._bytes(value))
        inventory_path = root / "inventory.json"
        inventory = json.loads(inventory_path.read_text())
        data = path.read_bytes()
        inventory["files"][relative] = {
            "sha256": pilot.preparation._digest(data),
            "bytes": len(data),
        }
        inventory["inventory_sha256"] = pilot.preparation._hash(
            {k: v for k, v in inventory.items() if k != "inventory_sha256"}
        )
        inventory_path.write_bytes(pilot.preparation._bytes(inventory))
    with pytest.raises(ValueError):
        pilot.verify(root, tokenizer)
    with pytest.raises(ValueError):
        pilot.load_prepared(root)


def test_verify_retokenizes_instead_of_trusting_certificates(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tokenizer = bundle
    pilot.load_prepared(root)

    def unavailable(_root: Path) -> Any:
        raise ValueError("tokenizer unavailable")

    monkeypatch.setattr(study, "_capacity_tokenizer", unavailable)
    with pytest.raises(ValueError, match="tokenizer unavailable"):
        pilot.verify(root, tokenizer)


def test_failed_preparation_leaves_no_success_marker(
    tmp_path: Path, tokenizer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "failed"

    def fail(*_args: object) -> None:
        raise OSError("injected verification failure")

    monkeypatch.setattr(pilot.preparation, "_check_files", fail)
    with pytest.raises(OSError, match="injected"):
        pilot.prepare(pilot.make_plan(_small(), SOURCE), tokenizer, root)
    assert not (root / "inventory.json").exists()
    assert not (root / "README.md").exists()


def test_cli_requires_source_and_uses_exclusive_output(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps(_config()))
    output = tmp_path / "plan.json"
    args = [
        "plan",
        "--config",
        str(config),
        "--source-revision",
        SOURCE,
        "--output",
        str(output),
    ]
    assert pilot.main(args) == 0
    pilot.validate_plan(json.loads(output.read_text()))
    with pytest.raises(FileExistsError):
        pilot.main(args)
    duplicate = config.read_text().replace(
        '"execution_authorized": false',
        '"execution_authorized": false, "execution_authorized": false',
    )
    config.write_text(duplicate)
    with pytest.raises(ValueError):
        pilot.main([*args[:-1], str(tmp_path / "other.json")])
