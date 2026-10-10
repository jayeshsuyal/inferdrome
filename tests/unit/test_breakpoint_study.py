"""Offline study preparation binds strict budgets, pins and certified seed plans.

WordTokenizer is a test-only substitute at the existing tokenizer-loading boundary;
these artifacts are never presented as production Qwen3 tokenization evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inferdrome import breakpoint_study as preparation
from inferdrome import vllm_router_study as capacity
from inferdrome.cli import main
from inferdrome.routing_execution.canonical import canonical_json_bytes


def _config() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "examples/breakpoint-qwen3-study.json"
    return json.loads(path.read_text())


def _small_config() -> dict[str, Any]:
    value = _config()
    value["workload"].update(
        duration_s=3,
        offers_per_trial=12,
        rate_rps=4,
        document_count=8,
        target_prefix_tokens=256,
        output_tokens=4,
        context_length=512,
        max_client_concurrency=64,
    )
    value["search"]["group_sizes"] = [2]
    value["search"]["max_advances_ns"] = [1_000_000, 100_000_000]
    return value


def _resign(value: dict[str, Any], field: str) -> None:
    value[field] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != field}
            )
        ).hexdigest()
    )


class WordTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> SimpleNamespace:
        assert add_special_tokens is False
        return SimpleNamespace(ids=text.split())


@pytest.fixture
def test_tokenizer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(capacity, "_capacity_tokenizer", lambda _root: WordTokenizer())
    return tmp_path / "test-only-tokenizer"


@pytest.fixture
def prepared(tmp_path: Path, test_tokenizer: Path) -> tuple[Path, Path]:
    root = tmp_path / "prepared"
    preparation.prepare(preparation.make_plan(_small_config()), test_tokenizer, root)
    return root, test_tokenizer


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("execution_authorized",), True),
        (("execution_authorized",), 0),
        (("spending", "decision"), "APPROVED"),
        (("spending", "cap_usd"), 1),
        (("workload", "offers_per_trial"), True),
        (("workload", "offers_per_trial"), 720.0),
        (("workload", "rate_rps"), float("nan")),
        (("workload", "rate_rps"), float("inf")),
        (("comparison", "order_seed"), True),
        (("runtime", "max_session_s"), 43_200.0),
        (("runtime", "drain_allowance_s_per_trial"), 0),
        (("confirmation", "max_attempts"), 2),
        (("pins", "model"), "untrusted/model"),
        (("pins", "model_revision"), "a" * 40),
        (("pins", "source_revision"), "main"),
        (("pins", "source_revision"), "A" * 40),
        (("pins", "tokenizers_version"), "0.22.0"),
        (("pins", "image_reference"), "vllm/vllm-openai:latest"),
    ],
)
def test_configuration_rejects_ambiguous_scalars_unpinned_tools_and_approval_claims(
    path: tuple[str, ...],
    value: Any,
) -> None:
    config = _config()
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        preparation.make_plan(config)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "overlap",
        "duplicate",
        "too_few",
        "inconsistent_rate",
        "infeasible_session",
    ],
)
def test_config_contract_rejects_reused_seeds_and_infeasible_budgets(
    mutation: str,
) -> None:
    config = _config()
    if mutation == "unknown":
        config["endpoint"] = "http://127.0.0.1:8000"
    elif mutation == "overlap":
        config["heldout_seeds"][0] = config["discovery_seeds"][0]
    elif mutation == "duplicate":
        config["discovery_seeds"][1] = config["discovery_seeds"][0]
    elif mutation == "too_few":
        config["heldout_seeds"] = config["heldout_seeds"][:4]
    elif mutation == "inconsistent_rate":
        config["workload"]["offers_per_trial"] -= 1
    else:
        config["runtime"]["max_session_s"] = 41_999
    with pytest.raises(ValueError):
        preparation.make_plan(config)


def test_plan_is_deterministic_and_validates_its_entire_recipe() -> None:
    config = _config()
    plan = preparation.make_plan(config)
    assert plan == preparation.make_plan(copy.deepcopy(config))
    preparation.validate_plan(plan)
    plan["invented_approval"] = True
    with pytest.raises(ValueError):
        preparation.validate_plan(plan)


@pytest.mark.parametrize(
    "raw", ['{"schema":1,"schema":2}', '{"value":NaN}', '{"value":Infinity}', "null"]
)
def test_cli_strict_json_rejection_never_publishes_a_plan(
    tmp_path: Path, raw: str
) -> None:
    source = tmp_path / "config.json"
    source.write_text(raw)
    output = tmp_path / "must-not-exist.json"
    assert (
        main(
            [
                "breakpoint",
                "study",
                "plan",
                "--config",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_cli_plan_is_offline_and_does_not_replace_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("offline planning attempted a network connection or process launch")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    source = tmp_path / "config.json"
    source.write_text(json.dumps(_config()))
    output = tmp_path / "plan.json"
    args = [
        "breakpoint",
        "study",
        "plan",
        "--config",
        str(source),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert json.loads(output.read_text()) == preparation.make_plan(_config())
    before = output.read_bytes()
    assert main(args) == 1
    assert output.read_bytes() == before


def test_real_unpinned_tokenizer_is_rejected_before_publication(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(preparation.make_plan(_small_config())))
    tokenizer = tmp_path / "wrong-tokenizer"
    tokenizer.mkdir()
    (tokenizer / "tokenizer.json").write_text("{}")
    (tokenizer / "tokenizer_config.json").write_text("{}")
    output = tmp_path / "must-not-be-prepared"
    assert (
        main(
            [
                "breakpoint",
                "study",
                "prepare",
                "--plan",
                str(plan),
                "--tokenizer-root",
                str(tokenizer),
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert not (output / "inventory.json").exists()
    assert not (output / "README.md").exists()


def test_prepare_certifies_all_sixteen_capacity_evaluation_seed_plans(
    prepared: tuple[Path, Path],
) -> None:
    root, tokenizer = prepared
    assert root.stat().st_mode & 0o777 == 0o700
    plans = sorted((root / "plans").glob("*-plan.json"))
    assert len(plans) == 16
    seeds = []
    for path in plans:
        plan = json.loads(path.read_text())
        assert plan["schema"] == capacity.CAPACITY_SCHEMA
        assert plan["phase"] == "evaluation"
        assert len(capacity.validate_capacity_plan(plan)) == 12
        certificate = json.loads(
            path.with_name(
                path.name.replace("-plan.json", "-certificate.json")
            ).read_text()
        )
        capacity.validate_capacity_certificate(plan, certificate)
        assert certificate == capacity.certify_capacity_plan(plan, tokenizer)
        seeds.append(plan["seed"])
    assert set(seeds) == set(
        _small_config()["discovery_seeds"] + _small_config()["heldout_seeds"]
    )
    preparation.verify(root, tokenizer)


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "plan", "certificate", "inventory", "guide"]
)
def test_prepared_verification_rejects_missing_tampered_and_rehashed_files(
    prepared: tuple[Path, Path], mutation: str
) -> None:
    root, tokenizer = prepared
    if mutation == "missing":
        (root / "initial" / "protocol.json").unlink()
    elif mutation == "extra":
        (root / "unlisted.json").write_text("{}")
    elif mutation == "guide":
        (root / "README.md").write_text("GPU execution is approved")
    else:
        path = {
            "plan": root / "plans" / "heldout-b01-plan.json",
            "certificate": root / "plans" / "heldout-b01-certificate.json",
            "inventory": root / "inventory.json",
        }[mutation]
        value = json.loads(path.read_text())
        if mutation == "plan":
            value["prompt_tokens_by_index"][0] += 1
            _resign(value, "plan_sha256")
        elif mutation == "certificate":
            value["prompt_tokens_max"] += 1
            _resign(value, "certificate_sha256")
        else:
            value["execution_authorized"] = True
            _resign(value, "inventory_sha256")
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        preparation.verify(root, tokenizer)


def test_verify_retokenizes_instead_of_trusting_self_hashed_certificates(
    prepared: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tokenizer = prepared

    class ChangedTokenizer(WordTokenizer):
        def encode(self, text: str, *, add_special_tokens: bool) -> SimpleNamespace:
            value = super().encode(text, add_special_tokens=add_special_tokens)
            return SimpleNamespace(ids=[*value.ids, "changed"])

    monkeypatch.setattr(
        capacity, "_capacity_tokenizer", lambda _root: ChangedTokenizer()
    )
    with pytest.raises(ValueError):
        preparation.verify(root, tokenizer)


def test_prepare_preserves_existing_output(prepared: tuple[Path, Path]) -> None:
    root, tokenizer = prepared
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    with pytest.raises((ValueError, FileExistsError)):
        preparation.prepare(preparation.make_plan(_small_config()), tokenizer, root)
    assert before == {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_failed_preparation_never_publishes_success_marker_or_guide(
    tmp_path: Path, test_tokenizer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "partial"
    original = Path.open

    def interrupted(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path.name == "search-plan.json" and ("w" in mode or "x" in mode):
            raise OSError("injected publication failure")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupted)
    with pytest.raises(OSError, match="injected publication failure"):
        preparation.prepare(
            preparation.make_plan(_small_config()), test_tokenizer, output
        )
    assert not (output / "inventory.json").exists()
    assert not (output / "README.md").exists()


@pytest.mark.parametrize("action", ["plan", "prepare", "verify"])
def test_study_actions_are_discoverable_through_public_help(
    action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["breakpoint", "study", action, "--help"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_example_reserves_the_entire_possible_study_within_twelve_hours() -> None:
    plan = preparation.make_plan(_config())
    slots = plan["reservations"]
    assert slots["trial_slots_per_comparison"] == 32
    assert slots["search_trial_slots"] == slots["reduction_trial_slots"] == 64
    assert slots["confirmation_trial_slots"] == 32
    assert slots["total_trial_slots"] == 160
    assert slots["max_offered_requests"] == 115_200
    assert slots["scheduled_traffic_s"] == 19_200
    assert slots["drain_allowance_s"] == slots["warm_reset_setup_allowance_s"] == 9_600
    assert slots["session_overhead_s"] == 3_600
    assert slots["teardown_reserve_included_s"] == 600
    assert slots["max_planned_session_s"] == 42_000
    assert slots["declared_session_limit_s"] == 43_200
    assert slots["remaining_session_margin_s"] == 1_200
    assert plan["status"] == "PLANNED_NOT_EXECUTED"
    assert plan["execution_authorized"] is False
    assert plan["spending"] == {"decision": "UNDECIDED", "cap_usd": None}
    assert plan["environment_declaration"]["replicas"] == 2
    assert plan["environment_declaration"]["tensor_parallel_size_per_replica"] == 1
    assert plan["environment_declaration"]["observed_hardware"] is None
    assert plan["reset_procedure_declaration"]["observed_reset_receipts"] is None


def test_predeclared_seed_order_is_bound_to_the_plan_digest() -> None:
    config = _config()
    initial = preparation.make_plan(config)
    config["heldout_seeds"].reverse()
    reversed_plan = preparation.make_plan(config)
    assert reversed_plan["config"]["heldout_seeds"] == config["heldout_seeds"]
    assert reversed_plan["plan_sha256"] != initial["plan_sha256"]
    initial["config"]["heldout_seeds"].reverse()
    with pytest.raises(ValueError):
        preparation.validate_plan(initial)


@pytest.mark.parametrize(
    "mutation", ["reservation", "source", "environment", "spending"]
)
def test_rehashed_plan_metadata_cannot_override_its_declared_recipe(
    mutation: str,
) -> None:
    plan = preparation.make_plan(_config())
    if mutation == "reservation":
        plan["reservations"]["max_planned_session_s"] = 0
    elif mutation == "source":
        plan["comparison_options"]["source_revision"] = "a" * 40
    elif mutation == "environment":
        plan["environment_declaration"]["tensor_parallel_size_per_replica"] = 2
    else:
        plan["spending"]["cap_usd"] = 100
    _resign(plan, "plan_sha256")
    with pytest.raises(ValueError):
        preparation.validate_plan(plan)


def test_prepare_and_verify_do_not_contact_endpoints_or_launch_processes(
    tmp_path: Path, test_tokenizer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiohttp

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("offline study preparation attempted a network or experiment call")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(capacity, "run_trial", forbidden)
    root = tmp_path / "offline"
    inventory = preparation.prepare(
        preparation.make_plan(_small_config()), test_tokenizer, root
    )
    assert preparation.verify(root, test_tokenizer) == inventory
    assert inventory["execution_authorized"] is False
    assert inventory["status"] == "PREPARED_NOT_EXECUTED"


def test_initial_trial_commands_and_input_paths_match_frozen_protocol(
    prepared: tuple[Path, Path],
) -> None:
    root, _ = prepared
    protocol = json.loads((root / "initial/protocol.json").read_text())
    commands = json.loads((root / "initial/trial-commands.json").read_text())
    inputs = json.loads((root / "initial/inputs.template.json").read_text())
    assert commands["execution_authorized"] is False
    assert commands["status"] == "NOT_EXECUTED"
    assert commands["working_directory"] == "PREPARED_BUNDLE_ROOT"
    assert (
        len(commands["trials"])
        == len(protocol["trials"])
        == len(inputs["trials"])
        == 32
    )
    for trial, command, item in zip(
        protocol["trials"], commands["trials"], inputs["trials"], strict=True
    ):
        assert command["trial_id"] == item["trial_id"] == trial["trial_id"]
        assert command["sequence"] == trial["sequence"]
        router, client = command["router_argv"], command["client_argv"]
        assert router[router.index("--policy") + 1] == trial["policy"]
        assert client[client.index("--policy") + 1] == trial["policy"]
        assert client[client.index("--model") + 1] == protocol["model"]
        assert "--correlate-requests" in client
        raw_result = (root / client[client.index("--output") + 1]).resolve()
        raw_ledger = (root / router[router.index("--ledger") + 1]).resolve()
        assert not raw_result.is_relative_to(root.resolve())
        assert not raw_ledger.is_relative_to(root.resolve())
        assert (
            raw_result.parent
            == raw_ledger.parent
            == (root.parent / "collection/c001").resolve()
        )
        assert (root / "initial" / item["result"]).resolve() == raw_result
        assert (root / "initial" / item["ledger"]).resolve() == raw_ledger
        assert item["execution"]["reset_completed"] is False
        for field in (
            "source_revision",
            "environment_sha256",
            "reset_procedure_sha256",
        ):
            assert item["execution"][field] == protocol[field]
        for flag, field in (
            ("--timing", "timing"),
            ("--token-certificate", "token_certificate"),
        ):
            path = (root / client[client.index(flag) + 1]).resolve()
            assert path.is_file()
            assert path == (root / "initial" / item[field]).resolve()
        base = json.loads((root / client[client.index("--plan") + 1]).read_text())
        timing = json.loads((root / client[client.index("--timing") + 1]).read_text())
        assert base["seed"] == _small_config()["discovery_seeds"][trial["block"] - 1]
        assert timing["timing_sha256"] == trial["timing_sha256"]


def test_missing_local_tokenizer_runtime_is_a_clean_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.metadata

    def missing_runtime(_root: Path) -> Any:
        raise importlib.metadata.PackageNotFoundError("tokenizers")

    monkeypatch.setattr(capacity, "_capacity_tokenizer", missing_runtime)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(preparation.make_plan(_small_config())))
    output = tmp_path / "must-not-exist"
    assert (
        main(
            [
                "breakpoint",
                "study",
                "prepare",
                "--plan",
                str(plan),
                "--tokenizer-root",
                str(tmp_path),
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert not (output / "inventory.json").exists()
    assert not (output / "README.md").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require POSIX")
def test_verify_rejects_a_fifo_plan_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fifo-bundle"
    root.mkdir()
    plan_path = root / "plan.json"
    os.mkfifo(plan_path)
    original_read = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if path == plan_path:
            pytest.fail("verification attempted to read a FIFO study plan")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(ValueError):
        preparation.verify(root, tmp_path / "unread-tokenizer")


@pytest.mark.parametrize(
    ("axis", "identity_values"),
    [
        ("group_sizes", [1]),
        ("retained_spacing_bps", [10_000]),
        ("max_advances_ns", [0]),
    ],
)
def test_identity_only_grid_is_rejected_before_the_cli_publishes_a_plan(
    tmp_path: Path, axis: str, identity_values: list[int]
) -> None:
    config = _small_config()
    config["search"][axis] = identity_values
    # Keep the declared slot budget feasible even when the axis has one value;
    # the rejection must concern the absence of a timing intervention.
    config["search"].update(max_candidates=1, max_trial_slots=32)
    with pytest.raises(ValueError):
        preparation.make_plan(config)
    source = tmp_path / "identity-config.json"
    source.write_text(json.dumps(config))
    output = tmp_path / "must-not-exist.json"
    assert (
        main(
            [
                "breakpoint",
                "study",
                "plan",
                "--config",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_mixed_identity_and_nonidentity_grid_still_prepares_a_usable_search(
    tmp_path: Path, test_tokenizer: Path
) -> None:
    config = _small_config()
    config["search"].update(
        group_sizes=[1, 2],
        retained_spacing_bps=[2500, 10_000],
        max_advances_ns=[0, 1_000_000],
    )
    plan = preparation.make_plan(config)
    root = tmp_path / "mixed-grid"
    inventory = preparation.prepare(plan, test_tokenizer, root)
    assert inventory["status"] == "PREPARED_NOT_EXECUTED"
    initial = json.loads((root / "search-report.initial.json").read_text())
    assert initial["status"] == "AWAITING_EVIDENCE"
    assert initial["next_action"] is not None
    assert preparation.verify(root, test_tokenizer) == inventory
