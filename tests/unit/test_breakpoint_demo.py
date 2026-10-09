"""The installed walkthrough produces replayable, explicitly fabricated evidence."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from inferdrome import breakpoint_demo as demo
from inferdrome import breakpoint_report
from inferdrome import vllm_heldout_confirmation as confirmation
from inferdrome.vllm_paired_comparison import _load_inputs


def _json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text())
    return value


def _no_execution(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("the synthetic demo attempted external execution")


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("breakpoint-demo") / "example"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "create_connection", _no_execution)
        patch.setattr(socket.socket, "connect", _no_execution)
        patch.setattr(demo.study, "run_trial", _no_execution)
        inventory = demo.create_demo(root)
    assert inventory["evidence_class"] == "SYNTHETIC_ONLY"
    return root


def test_complete_walkthrough_replays_from_its_disk_artifacts(generated: Path) -> None:
    plan = _json(generated / "confirmation/plan.json")
    source = confirmation._load_source(generated / "confirmation/source.json")
    plans, inputs = _load_inputs(
        generated / "confirmation/inputs.json", plan["protocol"]
    )
    report = confirmation.evaluate(plan, source, plans, inputs)
    assert report == _json(generated / "confirmation/report.json")
    assert report["status"] == "HELD_OUT_CRITERIA_MET"
    assert report["evidence_eligible"] is False
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert source["search_source"]["report"]["status"] == "CANDIDATE_FOUND"
    assert source["report"]["status"] == "REDUCTION_COMPLETE"
    assert source["report"]["coverage"]["accepted_reductions"] == 2
    assert len(source["report"]["incumbent"]["retained_groups"]) == 1
    assert len(plan["discovery_seeds"]) == len(plan["held_out_seeds"]) == 8
    assert set(plan["discovery_seeds"]).isdisjoint(plan["held_out_seeds"])
    assert plan["protocol"]["source_revision"] == "0" * 40
    assert len(plans) == 8
    assert all(base["phase"] == "fixture" for base in plans)


def test_fabricated_identity_population_and_private_destination(
    generated: Path,
) -> None:
    inventory = _json(generated / "demo.json")
    assert inventory["fabricated"] is True
    assert inventory["fabricated_trials"] == 128
    assert inventory["fabricated_requests"] == 1536
    assert (
        inventory["source_revision_scope"]
        == "PLACEHOLDER_NOT_AN_EXECUTED_GIT_REVISION"
    )
    assert inventory["reset_procedure"]["reset_was_executed"] is False
    requests: set[str] = set()
    count = 0
    for path in generated.rglob("*-result.json"):
        result = _json(path)
        measurement = result["measurement"]
        assert measurement["evidence_class"] == "SYNTHETIC_ONLY"
        assert measurement["model"] == "fabricated-breakpoint-demo"
        assert len(measurement["rows"]) == 12
        assert measurement["summary"]["all_offered"]["slo_good"] in {6, 12}
        identities = {link["request_id"] for link in result["request_links"]}
        assert requests.isdisjoint(identities)
        requests.update(identities)
        count += 1
    assert count == 128
    assert len(requests) == 1536
    assert generated.stat().st_mode & 0o777 == 0o700
    for relative in ("summary.md", "search/summary.md", "reduction/summary.md"):
        text = (generated / relative).read_text()
        assert "SYNTHETIC_ONLY" in text
        assert str(generated) not in text
    readme = (generated / "README.md").read_text()
    assert "all outcomes are fabricated" in readme
    assert "inferdrome breakpoint confirm verify" in readme
    assert "partial directory" in readme


def test_cli_regenerates_identical_files_in_a_fresh_directory(
    generated: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "same-demo"
    assert demo.main(["--output-dir", str(other)]) == 0
    assert "128 fabricated trials" in capsys.readouterr().out
    first = {
        path.relative_to(generated): path.read_bytes()
        for path in generated.rglob("*")
        if path.is_file()
    }
    second = {
        path.relative_to(other): path.read_bytes()
        for path in other.rglob("*")
        if path.is_file()
    }
    assert first == second


@pytest.mark.parametrize("kind", ["empty", "populated", "file", "symlink"])
def test_existing_destinations_are_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    destination = tmp_path / "existing"
    if kind == "file":
        destination.write_bytes(b"keep")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
        destination.symlink_to(target, target_is_directory=True)
    else:
        destination.mkdir()
        if kind == "populated":
            (destination / "keep").write_bytes(b"keep")
    monkeypatch.setattr(demo, "_build_artifacts", _no_execution)
    with pytest.raises(FileExistsError):
        demo.create_demo(destination)
    if kind == "file":
        assert destination.read_bytes() == b"keep"
    elif kind in {"populated", "symlink"}:
        assert (destination / "keep").read_bytes() == b"keep"
    else:
        assert list(destination.iterdir()) == []


def test_failed_verification_leaves_no_completion_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "failed"
    monkeypatch.setattr(demo, "summarize_report", lambda argv: 2)
    with pytest.raises(ValueError, match="walkthrough verification failed"):
        demo.create_demo(destination)
    assert (destination / "confirmation/report.json").is_file()
    assert not (destination / "README.md").exists()


def test_modified_raw_demo_receipt_cannot_produce_a_verified_summary(
    generated: Path, tmp_path: Path
) -> None:
    changed = tmp_path / "changed"
    shutil.copytree(generated, changed)
    receipt = next((changed / "confirmation").glob("*-ledger.jsonl"))
    rows = [json.loads(line) for line in receipt.read_text().splitlines()]
    rows[0]["request_id"] = "f" * 32
    receipt.write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = changed / "tampered-summary.md"
    with pytest.raises(ValueError):
        breakpoint_report.main(
            [
                "confirm",
                "--plan",
                str(changed / "confirmation/plan.json"),
                "--source",
                str(changed / "confirmation/source.json"),
                "--inputs",
                str(changed / "confirmation/inputs.json"),
                "--report",
                str(changed / "confirmation/report.json"),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_demo_requires_an_explicit_new_output_directory() -> None:
    with pytest.raises(SystemExit) as error:
        demo.main([])
    assert error.value.code == 2
