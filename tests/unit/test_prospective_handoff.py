"""Adversarial local validation for the immutable P1 handoff snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.prospective_handoff as handoff


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _make_handoff(root: Path) -> tuple[str, str]:
    root.joinpath("contracts").mkdir(parents=True)
    root.joinpath("confirmations").mkdir()
    root.joinpath("sources", "real-gpu").mkdir(parents=True)
    files: dict[str, bytes] = {
        ".complete": b"exitspec.inferdrome-prospective-handoff.complete.v1\n",
        "sources/real-gpu/workload.jsonl": b'{"prompt":"P1"}\n',
    }
    cases: list[dict[str, object]] = []
    for index, case_id in enumerate(handoff.CASE_IDS, start=1):
        canonical = f"{index:064x}"
        contract_path = f"contracts/{case_id}.frozen.json"
        confirmation_path = f"confirmations/{case_id}.confirmation.json"
        source_path = f"sources/{case_id}.yaml"
        contract = {
            "canonical_hash": canonical,
            "confirmation_id": f"cnf_{index}",
            "id": f"inferdrome-p1-{case_id}",
            "status": "FROZEN",
            "version": "1.0.0",
        }
        confirmation = {
            "agreement_acknowledged": True,
            "confirmation_id": f"cnf_{index}",
            "contract_fingerprint": f"{index + 10:064x}",
            "contract_id": contract["id"],
            "contract_version": contract["version"],
            "decision": "CONFIRM",
        }
        files[contract_path] = json.dumps(contract, separators=(",", ":")).encode()
        files[confirmation_path] = json.dumps(
            confirmation, separators=(",", ":")
        ).encode()
        files[source_path] = (
            "experiment:\n  id: prospective-" + case_id + "\n"
        ).encode()
        cases.append(
            {
                "case_id": case_id,
                "confirmation_artifact_path": confirmation_path,
                "confirmation_record_sha256": _digest(files[confirmation_path]),
                "contract_artifact_path": contract_path,
                "contract_artifact_sha256": _digest(files[contract_path]),
                "contract_canonical_hash": canonical,
                "contract_confirmation_fingerprint": confirmation[
                    "contract_fingerprint"
                ],
                "contract_id": contract["id"],
                "contract_version": contract["version"],
                "producer_contract_link": "sha256:" + canonical,
                "source_yaml_artifact_path": source_path,
                "source_yaml_artifact_sha256": _digest(files[source_path]),
            }
        )
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "acceptance_verdict": None,
        "authority_boundary": "EXIT_SPEC_CUSTOMER_CONFIRMED_HANDOFF_ONLY",
        "cases": cases,
        "completion_marker": ".complete",
        "schema_version": "exitspec.inferdrome-prospective-handoff.v1",
        "workload_artifact_path": "sources/real-gpu/workload.jsonl",
        "workload_artifact_sha256": _digest(files["sources/real-gpu/workload.jsonl"]),
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    (root / "handoff-manifest.json").write_bytes(manifest_bytes)
    return _digest(manifest_bytes), _digest(files["sources/real-gpu/workload.jsonl"])


def test_exact_p1_snapshot_and_archive_preserve_relative_workload_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "inferdrome-p1"
    manifest_digest, workload_digest = _make_handoff(root)

    snapshot = handoff.snapshot_handoff(
        root,
        expected_manifest_sha256=manifest_digest,
        expected_workload_sha256=workload_digest,
    )
    archived = handoff.create_handoff_archive(snapshot, tmp_path / "handoff.tar.gz")

    assert len(snapshot.files) == 12
    assert snapshot.expected_contract_digests == tuple(
        f"sha256:{index:064x}" for index in range(1, 4)
    )
    assert archived.archive_path == tmp_path / "handoff.tar.gz"
    assert archived.archive_size_bytes == (tmp_path / "handoff.tar.gz").stat().st_size


@pytest.mark.parametrize(
    "mutation",
    [
        "symlink",
        "hardlink",
        "extra",
        "wrong_hash",
        "malformed",
        "duplicate_case",
        "swapped_contracts",
        "mismatched_confirmation",
        "missing",
    ],
)
def test_snapshot_rejects_local_handoff_input_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "inferdrome-p1"
    _make_handoff(root)
    target = root / "sources" / "native-p95-under-20ms.yaml"
    if mutation == "symlink":
        target.unlink()
        target.symlink_to(root / "sources" / "native-p95-under-10ms.yaml")
    elif mutation == "hardlink":
        replacement = root / "sources" / "replacement.yaml"
        replacement.write_bytes(target.read_bytes())
        target.unlink()
        target.hardlink_to(replacement)
    elif mutation == "extra":
        (root / "sources" / "extra.yaml").write_bytes(b"extra\n")
    elif mutation == "wrong_hash":
        manifest = json.loads((root / "handoff-manifest.json").read_text())
        manifest["cases"][0]["source_yaml_artifact_sha256"] = _digest(b"wrong")
        (root / "handoff-manifest.json").write_text(json.dumps(manifest))
    elif mutation == "malformed":
        (root / "handoff-manifest.json").write_bytes(b"{")
    elif mutation == "duplicate_case":
        manifest = json.loads((root / "handoff-manifest.json").read_text())
        manifest["cases"].append(manifest["cases"][0])
        (root / "handoff-manifest.json").write_text(json.dumps(manifest))
    elif mutation == "swapped_contracts":
        first = root / "contracts" / "native-p95-under-20ms.frozen.json"
        second = root / "contracts" / "native-p95-under-10ms.frozen.json"
        first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
        first.write_bytes(second_bytes)
        second.write_bytes(first_bytes)
    elif mutation == "mismatched_confirmation":
        first = root / "confirmations" / "native-p95-under-20ms.confirmation.json"
        second = root / "confirmations" / "native-p95-under-10ms.confirmation.json"
        first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
        first.write_bytes(second_bytes)
        second.write_bytes(first_bytes)
    else:
        target.unlink()

    with pytest.raises(handoff.ProspectiveHandoffError):
        handoff.snapshot_handoff(root)


def test_snapshot_rejects_toctou_inventory_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "inferdrome-p1"
    _make_handoff(root)
    original = handoff._walk_inventory
    calls = 0

    def mutate_after_first(selected: Path) -> dict[str, tuple[int, ...]]:
        nonlocal calls
        calls += 1
        inventory = original(selected)
        if calls == 1:
            (selected / "sources" / "native-p95-under-20ms.yaml").write_bytes(
                b"changed\n"
            )
        return inventory

    monkeypatch.setattr(handoff, "_walk_inventory", mutate_after_first)
    with pytest.raises(handoff.ProspectiveHandoffError, match="changed"):
        handoff.snapshot_handoff(root)
