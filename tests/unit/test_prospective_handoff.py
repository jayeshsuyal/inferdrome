"""Adversarial local validation for the immutable P1 handoff snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import rfc8785

import scripts.prospective_handoff as handoff


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _make_handoff(root: Path) -> tuple[str, str]:
    fixture = Path(__file__).parents[1] / "fixtures" / "prospective_p1_v1_bytes.json"
    files = {
        relative: content.encode("utf-8")
        for relative, content in json.loads(fixture.read_text())["files"].items()
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return _digest(files["handoff-manifest.json"]), _digest(
        files["sources/real-gpu/workload.jsonl"]
    )


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
    assert snapshot.expected_contract_digests == (
        "sha256:c73f3fe1127575443bc30baa1cac4a610dfebfcd721ac72a2c998a6bf1c21580",
        "sha256:6a499cfc2e15245e905ecec8282910536e1a594ca3a4d9117e50394ee4f0d855",
        "sha256:fe776bccedbd5a935480be5808bfaf73b60e17c3f275c2c9b1ba46c2ba9eb248",
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


def test_snapshot_rejects_unallowlisted_nested_directory(tmp_path: Path) -> None:
    root = tmp_path / "inferdrome-p1"
    _make_handoff(root)
    (root / "contracts" / "unexpected-directory").mkdir()

    with pytest.raises(handoff.ProspectiveHandoffError, match="extra path"):
        handoff.snapshot_handoff(root)


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "handoff-manifest.json").read_bytes())


def _write_manifest(root: Path, value: dict[str, object]) -> None:
    (root / "handoff-manifest.json").write_bytes(
        json.dumps(value, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_top_level",
        "alias_envelope",
        "wrong_canonicalization",
        "wrong_hash_algorithm",
        "wrong_link_policy",
        "producer_verdict",
        "case_swap",
        "unknown_case",
        "unknown_methodology",
        "metric_drift",
        "target_drift",
        "workload_drift",
        "traffic_drift",
        "sampling_drift",
    ],
)
def test_exact_p1_v1_rejects_protocol_and_methodology_drift(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / mutation
    _make_handoff(root)
    manifest = _manifest(root)
    if mutation == "unknown_top_level":
        manifest["unexpected"] = True
    elif mutation == "alias_envelope":
        manifest["artifacts"] = {}
    elif mutation == "wrong_canonicalization":
        manifest["canonicalization_scheme_id"] = "legacy"
    elif mutation == "wrong_hash_algorithm":
        manifest["hash_algorithm_id"] = "sha512_v1"
    elif mutation == "wrong_link_policy":
        manifest["link_derivation_policy_id"] = "legacy"
    elif mutation == "producer_verdict":
        manifest["acceptance_verdict"] = "PASS"
    elif mutation == "case_swap":
        cases = manifest["cases"]
        assert isinstance(cases, list)
        cases[0], cases[1] = cases[1], cases[0]
    else:
        cases = manifest["cases"]
        assert isinstance(cases, list)
        selected = cases[0]
        assert isinstance(selected, dict)
        if mutation == "unknown_case":
            selected["unexpected"] = True
        elif mutation == "unknown_methodology":
            selected["methodology"]["unexpected"] = True
        elif mutation == "metric_drift":
            selected["methodology"]["requested_criterion_metric_definition_id"] = (
                "first_nonempty_choices_delta_content_v1"
            )
        elif mutation == "target_drift":
            selected["methodology"]["target_model"] = "other-model"
        elif mutation == "workload_drift":
            selected["methodology"]["workload_digest"] = "sha256:" + "0" * 64
        elif mutation == "traffic_drift":
            selected["methodology"]["traffic"]["configured_concurrency"] = 8
        else:
            selected["methodology"]["sampling"]["seed"] = 43
    _write_manifest(root, manifest)

    with pytest.raises(handoff.ProspectiveHandoffError):
        handoff.snapshot_handoff(root)


def test_producer_link_cannot_self_authorize_a_changed_source_and_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer-link-mismatch"
    _make_handoff(root)
    manifest = _manifest(root)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    selected = cases[0]
    assert isinstance(selected, dict)
    replacement = "sha256:" + "a" * 64
    source_path = root / selected["source_yaml_artifact_path"]
    source = source_path.read_bytes().replace(
        selected["producer_contract_link"].encode(), replacement.encode()
    )
    source_path.write_bytes(source)
    selected["producer_contract_link"] = replacement
    selected["source_yaml_artifact_sha256"] = _digest(source)
    _write_manifest(root, manifest)

    with pytest.raises(handoff.ProspectiveHandoffError, match="producer contract"):
        handoff.snapshot_handoff(root)


def test_changed_contract_cannot_pass_with_recalculated_artifact_and_manifest_pins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "contract-look-alike"
    manifest_digest, workload_digest = _make_handoff(root)
    contract_path = root / "contracts/native-p95-under-20ms.frozen.json"
    contract = json.loads(contract_path.read_bytes())
    contract["target_system"]["model"] = "other-model"
    contract_bytes = json.dumps(contract, separators=(",", ":")).encode()
    contract_path.write_bytes(contract_bytes)
    manifest = _manifest(root)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    selected = cases[0]
    assert isinstance(selected, dict)
    selected["contract_artifact_sha256"] = _digest(contract_bytes)
    _write_manifest(root, manifest)

    with pytest.raises(handoff.ProspectiveHandoffError):
        handoff.snapshot_handoff(
            root,
            expected_manifest_sha256=manifest_digest,
            expected_workload_sha256=workload_digest,
        )


def test_recalculated_contract_fingerprint_cannot_self_authorize_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recalculated-fingerprint"
    _make_handoff(root)
    manifest = _manifest(root)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    selected = cases[0]
    assert isinstance(selected, dict)
    confirmation_path = root / selected["confirmation_artifact_path"]
    confirmation = json.loads(confirmation_path.read_bytes())
    confirmation["rationale"] = "recalculated look-alike confirmation"
    confirmation_bytes = rfc8785.dumps(confirmation)
    confirmation_path.write_bytes(confirmation_bytes)
    selected["confirmation_record_sha256"] = _digest(confirmation_bytes)
    selected["contract_confirmation_fingerprint"] = (
        "a" * 64
    )
    _write_manifest(root, manifest)

    with pytest.raises(handoff.ProspectiveHandoffError):
        handoff.snapshot_handoff(root)


def test_exact_fixture_matches_observed_operator_pins(tmp_path: Path) -> None:
    root = tmp_path / "exact-fixture"
    manifest_digest, workload_digest = _make_handoff(root)
    assert manifest_digest == (
        "sha256:2dfb5808c2b172f0fd17d034421aa8439f96c54f0a578b7c3f42bdcba2b8231c"
    )
    assert workload_digest == (
        "sha256:22bf3389cc29ee946ae567870d7f8d7b458594224542a796e8990c15b1cfcd63"
    )
    snapshot = handoff.snapshot_handoff(
        root,
        expected_manifest_sha256=manifest_digest,
        expected_workload_sha256=workload_digest,
    )
    assert tuple(case.case_id for case in snapshot.cases) == handoff.CASE_IDS
