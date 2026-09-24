"""Offline rental gate: archive bytes, exact model inventory, time and disk."""

import io
import json
import tarfile

import pytest

from inferdrome.evaluation import engine_comparison_readiness as gate
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_comparison import encoded
from inferdrome.routing_execution.canonical import sha256_digest
from tests.unit.test_engine_comparison_local import inputs


def archive(tmp_path, role, files):
    path = tmp_path / f"{role}.tar"
    inventory = []
    with tarfile.open(path, "w") as tar:
        for name, content in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
            inventory.append([name, len(content), sha256_digest(content)])
    return gate.StageArchive(
        role=role,
        path=str(path),
        sha256=sha256_digest(path.read_bytes()),
        unpacked_bytes=sum(len(b) for b in files.values()),
        inventory_sha256=sha256_digest(encoded(inventory)),
    )


def fixture(tmp_path, monkeypatch):
    value = inputs()
    runtimes = {
        engine: getattr(value, engine).model_copy(
            update={"python_sha256": sha256_digest(b"fake executable")}
        )
        for engine in ("vllm", "sglang")
    }
    value = value.model_copy(update=runtimes)
    # Tiny SYNTHETIC_ONLY substitution of model bytes; actual production gate
    # compares the complete 15-file repository Qwen manifest.
    monkeypatch.setattr(
        gate,
        "qwen3_model_manifest",
        lambda: {
            "files": [
                {"path": "config.json", "size_bytes": 2, "sha256": sha256_digest(b"{}")}
            ]
        },
    )
    archives = (
        archive(tmp_path, "source", {"source.py": b"synthetic only"}),
        archive(
            tmp_path, "vllm", {runtimes["vllm"].python.lstrip("/"): b"fake executable"}
        ),
        archive(
            tmp_path,
            "sglang",
            {runtimes["sglang"].python.lstrip("/"): b"fake executable"},
        ),
        archive(tmp_path, "model", {"config.json": b"{}"}),
    )
    recipe = tmp_path / "operator.md"
    recipe.write_text(
        "SYNTHETIC_ONLY one create; retrieve; exact-ID destroy; two absence reads"
    )
    preparation = gate.SessionPreparation(
        inputs_sha256=sha256_digest(encoded(value.model_dump(mode="json"))),
        source_commit=value.plan.workload.source_commit,
        archives=archives,
        result_destination=str(tmp_path / "results"),
        available_disk_bytes=100 * 1024**3,
        max_session_seconds=7200,
        staging_seconds=600,
        retrieval_seconds=300,
        destruction_reserve_seconds=300,
        operator_recipe=str(recipe),
        operator_recipe_sha256=sha256_digest(recipe.read_bytes()),
    )
    return value, preparation


def test_one_session_offline_admission_is_deterministic_and_inert(
    tmp_path, monkeypatch
):
    value, preparation = fixture(tmp_path, monkeypatch)
    first = gate.offline_readiness(value, preparation)
    assert first == gate.offline_readiness(value, preparation)
    assert first["max_creates"] == 1
    assert first["provider_calls"] == 0
    assert first["worst_case_seconds"] <= 7200
    assert first["engine_order"] == ["vllm", "sglang", "sglang", "vllm"]
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    "change",
    [
        "missing-runtime",
        "hash",
        "model",
        "disk",
        "time",
        "recipe",
        "inputs",
        "existing-result",
    ],
)
def test_unresolved_prerequisite_fails_before_any_session(
    tmp_path, monkeypatch, change
):
    value, preparation = fixture(tmp_path, monkeypatch)
    if change == "missing-runtime":
        preparation = preparation.model_copy(
            update={"archives": preparation.archives[:3]}
        )
    elif change == "hash":
        from pathlib import Path

        Path(preparation.archives[1].path).write_bytes(b"changed")
    elif change == "model":
        wrong = archive(tmp_path, "model", {"config.json": b"wrong"})
        preparation = preparation.model_copy(
            update={"archives": (*preparation.archives[:3], wrong)}
        )
    elif change == "disk":
        preparation = preparation.model_copy(update={"available_disk_bytes": 1})
    elif change == "time":
        preparation = preparation.model_copy(update={"max_session_seconds": 3600})
    elif change == "recipe":
        preparation = preparation.model_copy(
            update={"operator_recipe_sha256": "sha256:" + "0" * 64}
        )
    elif change == "inputs":
        value = value.model_copy(
            update={"plan": value.plan.model_copy(update={"seed": 22})}
        )
    else:
        (tmp_path / "results").mkdir()
    with pytest.raises(ValueError):
        gate.offline_readiness(value, preparation)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "opt/../escape"])
def test_archive_traversal_rejected(tmp_path, name):
    item = archive(tmp_path, "source", {name: b"data"})
    with pytest.raises(EvaluationError):
        gate.verify_archive(item)


def test_cli_preview_no_local_runtime_access_and_execute_missing_gate(
    tmp_path, monkeypatch, capsys
):
    from scripts.run_matched_engine_comparison import main

    source = tmp_path / "inputs.json"
    source.write_bytes(encoded(inputs().model_dump(mode="json")))
    source.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["compare", "--inputs", str(source)])
    assert main() == 0
    assert (
        json.loads(capsys.readouterr().out)["status"] == "PREVIEW_ONLY_NOT_RENTAL_READY"
    )
    monkeypatch.setattr(
        "sys.argv", ["compare", "--inputs", str(source), "--execute-local"]
    )
    assert main() == 2
