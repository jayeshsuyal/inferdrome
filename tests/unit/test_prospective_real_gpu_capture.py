"""Fail-closed preflight and metadata checks for prospective GPU capture."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import scripts.prospective_real_gpu_capture as prospective
import scripts.run_real_gpu_demo as demo
from inferdrome.domain.evidence import EvidenceEligibility
from inferdrome.domain.ids import sha256_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIGESTS = {
    case_id: f"sha256:{index:064x}"
    for index, case_id in enumerate(prospective.CASE_IDS, start=1)
}


def _linked_sources(root: Path) -> tuple[list[str], list[str]]:
    workload = root / "real-gpu" / "workload.jsonl"
    workload.parent.mkdir(parents=True)
    workload.write_bytes(
        (REPOSITORY_ROOT / "examples" / "real-gpu" / "workload.jsonl").read_bytes()
    )
    source_template = yaml.safe_load(
        (REPOSITORY_ROOT / "examples" / "real-gpu-smoke.yaml").read_text()
    )
    case_arguments: list[str] = []
    expected_arguments: list[str] = []
    for case_id in prospective.CASE_IDS:
        document = copy.deepcopy(source_template)
        document["experiment"]["id"] = f"prospective-{case_id}"
        document["links"] = {"exitspec_contract_digest": CONTRACT_DIGESTS[case_id]}
        source = root / f"{case_id}.yaml"
        source.write_text(yaml.safe_dump(document, sort_keys=False))
        case_arguments.append(f"{case_id}={source}")
        expected_arguments.append(f"{case_id}={CONTRACT_DIGESTS[case_id]}")
    return case_arguments, expected_arguments


def _host_pin() -> dict[str, str]:
    return demo._host_pin()


def test_three_case_preflight_requires_exact_linked_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_arguments, expected_arguments = _linked_sources(tmp_path)
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())

    cases = prospective.validate_prospective_cases(
        case_arguments,
        expected_arguments,
    )

    assert [case.case_id for case in cases] == list(prospective.CASE_IDS)
    assert [
        case.resolution.resolved_spec.links.exitspec_contract_digest for case in cases
    ] == [CONTRACT_DIGESTS[case_id] for case_id in prospective.CASE_IDS]


def test_preflight_rejects_missing_or_wrong_expected_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_arguments, expected_arguments = _linked_sources(tmp_path)
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())

    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match="exactly the three named cases",
    ):
        prospective.validate_prospective_cases(case_arguments, expected_arguments[:-1])

    wrong = [
        (
            argument
            if not argument.startswith("native-p95-under-10ms=")
            else argument.split("=", maxsplit=1)[0] + "=sha256:" + "f" * 64
        )
        for argument in expected_arguments
    ]
    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match="source and expected ExitSpec digest disagree",
    ):
        prospective.validate_prospective_cases(case_arguments, wrong)


def test_preflight_rejects_duplicate_contract_digests_before_host_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_arguments, expected_arguments = _linked_sources(tmp_path)
    duplicate = [
        expected_arguments[0],
        expected_arguments[0].replace(
            "native-p95-under-20ms=",
            "native-p95-under-10ms=",
        ),
        expected_arguments[2],
    ]
    monkeypatch.setattr(
        prospective,
        "_static_pin",
        lambda: pytest.fail("duplicate contracts must fail before host work"),
    )

    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match="pairwise distinct",
    ):
        prospective.validate_prospective_cases(case_arguments, duplicate)


def test_preflight_rejects_methodology_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_arguments, expected_arguments = _linked_sources(tmp_path)
    drifted = Path(case_arguments[0].split("=", maxsplit=1)[1])
    document = yaml.safe_load(drifted.read_text())
    document["traffic"]["concurrency"] = 2
    drifted.write_text(yaml.safe_dump(document, sort_keys=False))
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())

    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match=r"pinned Qwen2\.5 methodology",
    ):
        prospective.validate_prospective_cases(case_arguments, expected_arguments)


def test_check_without_contracts_is_inert_and_does_not_prepare_or_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())
    monkeypatch.setattr(
        prospective.demo,
        "_require_clean_prepared_host",
        lambda *_args, **_kwargs: pytest.fail("host preflight must not run"),
    )
    monkeypatch.setattr(
        prospective.demo,
        "_run_cli",
        lambda *_args, **_kwargs: pytest.fail("provider/GPU execution must not run"),
    )
    output_root = tmp_path / "must-not-be-created"

    assert prospective.main(["--check", "--output-root", str(output_root)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "provider_or_gpu_mutation": "NONE",
        "status": "INERT_NO_CONTRACTS",
        "valid": True,
    }
    assert not output_root.exists()


def test_read_regular_rejects_symlinks_hardlinks_and_unsafe_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(source)
    hard_link = tmp_path / "hard-link.json"
    hard_link.hardlink_to(source)
    directory = tmp_path / "directory.json"
    directory.mkdir()

    for path in (link, hard_link, directory):
        with pytest.raises(prospective.ProspectiveCaptureError):
            prospective._read_regular(path, label="test input")


@pytest.mark.parametrize("swapped", ["commit", "digest"])
def test_exported_source_marker_cannot_self_authorize_controller_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    swapped: str,
) -> None:
    controller_digest = "sha256:" + "a" * 64
    swapped_marker_digest = "sha256:" + "b" * 64
    marker_commit = "b" * 40 if swapped == "commit" else "c" * 40
    host_pin = _host_pin()
    monkeypatch.setattr(demo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(demo, "_git_checkout_present", lambda: False)
    monkeypatch.setattr(
        demo,
        "_source_export_identity",
        lambda: (
            marker_commit,
            swapped_marker_digest if swapped == "digest" else controller_digest,
        ),
    )
    monkeypatch.setattr(
        demo,
        "_read_json",
        lambda *_args, **_kwargs: pytest.fail(
            "host preparation must not be consulted after marker mismatch"
        ),
    )

    with pytest.raises(demo.DemoError, match="controller pin"):
        demo._require_clean_prepared_host(
            tmp_path / "state",
            host_pin,
            expected_source_archive_sha256=controller_digest,
            expected_repository_commit="c" * 40,
        )


def test_exported_source_marker_and_host_preparation_must_share_controller_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    model_path = state_root / "models" / "model"
    model_path.mkdir(parents=True)
    packages = state_root / "python-packages.txt"
    packages.write_bytes(b"alpha==1\n")
    controller_commit = "a" * 40
    controller_digest = "sha256:" + "a" * 64
    host_pin = _host_pin()
    monkeypatch.setattr(demo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(demo, "_git_checkout_present", lambda: False)
    monkeypatch.setattr(
        demo,
        "_source_export_identity",
        lambda: (controller_commit, controller_digest),
    )
    monkeypatch.setattr(
        demo,
        "_read_json",
        lambda *_args, **_kwargs: {
            "architecture": "arm64",
            "model_directory": str(model_path),
            "model_id": "model-id",
            "model_revision": "revision",
            "prepared_at": "2026-08-27T00:00:00Z",
            "python_packages_sha256": "sha256:" + "b" * 64,
            "repository_commit": "b" * 40,
            "schema_version": "inferdrome.real-gpu-host-preparation.v1",
            "vllm_wheel_filename": "vllm.whl",
            "vllm_wheel_sha256": "sha256:" + "c" * 64,
        },
    )
    monkeypatch.setattr(
        demo,
        "_producer_wheel_pin",
        lambda *_args: {"filename": "vllm.whl", "sha256": "c" * 64},
    )
    monkeypatch.setattr(
        demo, "_require_package_environment_unchanged", lambda _path: None
    )
    monkeypatch.setattr(
        demo.sys, "executable", str(state_root / "venv" / "bin" / "python")
    )

    with pytest.raises(demo.DemoError, match="does not match this checkout"):
        demo._require_clean_prepared_host(
            state_root,
            host_pin,
            expected_source_archive_sha256=controller_digest,
            expected_repository_commit=controller_commit,
        )


def _metadata_cases(
    tmp_path: Path,
) -> tuple[tuple[prospective.CompletedCase, ...], list[str]]:
    case_arguments, expected_arguments = _linked_sources(tmp_path / "inputs")
    cases = prospective.validate_prospective_cases(
        case_arguments,
        expected_arguments,
    )
    completed: list[prospective.CompletedCase] = []
    session_root = tmp_path / "session"
    for index, case in enumerate(cases, start=1):
        run_id = f"run-{index:032x}"
        bundle = session_root / "cases" / case.case_id / "runs" / run_id / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "experiment.original.yaml").write_bytes(case.resolution.source_bytes)
        completed.append(
            prospective.CompletedCase(
                case=case,
                bundle_path=bundle,
                bundle_digest=f"sha256:{index + 10:064x}",
                run_id=run_id,
                source_spec_digest=case.resolution.source_spec_digest,
                execution_fingerprint=case.resolution.execution_fingerprint,
                request_plan_digest=f"sha256:{index + 20:064x}",
                exitspec_contract_digest=case.expected_contract_digest,
            )
        )
    return tuple(completed), expected_arguments


def _install_fake_reports(
    completed: tuple[prospective.CompletedCase, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_reports = {
        item.bundle_digest: SimpleNamespace(
            bundle_digest=item.bundle_digest,
            descriptor=SimpleNamespace(
                evidence_eligibility=EvidenceEligibility.CUSTOMER_ELIGIBLE,
                digests=SimpleNamespace(
                    execution_fingerprint=item.execution_fingerprint,
                    exitspec_contract_digest=item.exitspec_contract_digest,
                    request_plan_digest=item.request_plan_digest,
                    source_spec_digest=item.source_spec_digest,
                ),
            ),
            run_id=item.run_id,
        )
        for item in completed
    }
    monkeypatch.setattr(
        prospective,
        "verify_bundle",
        lambda _path, expected_bundle_digest: fake_reports[expected_bundle_digest],
    )


def test_run_case_retains_actual_run_scoped_request_plan_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_arguments, expected_arguments = _linked_sources(tmp_path / "inputs")
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())
    case = prospective.validate_prospective_cases(
        case_arguments,
        expected_arguments,
    )[0]
    session_root = tmp_path / "session"
    actual_run_id = "run-" + "c" * 32
    actual_bundle_digest = "sha256:" + "d" * 64
    actual_request_plan_digest = "sha256:" + "e" * 64
    bundle = (
        session_root
        / "cases"
        / case.case_id
        / "runs"
        / actual_run_id
        / "bundle"
    )

    def fake_run_cli(*_args: object, **_kwargs: object) -> SimpleNamespace:
        bundle.mkdir(parents=True)
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "bundle_digest": actual_bundle_digest,
                    "bundle_path": str(bundle),
                    "evidence_eligibility": "CUSTOMER_ELIGIBLE",
                    "integrity_status": "VALID",
                    "run_id": actual_run_id,
                }
            ).encode()
        )

    monkeypatch.setattr(prospective.demo, "_run_cli", fake_run_cli)
    monkeypatch.setattr(
        prospective,
        "verify_bundle",
        lambda _path, expected_bundle_digest: SimpleNamespace(
            bundle_digest=expected_bundle_digest,
            descriptor=SimpleNamespace(
                evidence_eligibility=EvidenceEligibility.CUSTOMER_ELIGIBLE,
                digests=SimpleNamespace(
                    execution_fingerprint=case.resolution.execution_fingerprint,
                    exitspec_contract_digest=case.expected_contract_digest,
                    request_plan_digest=actual_request_plan_digest,
                    source_spec_digest=case.resolution.source_spec_digest,
                ),
            ),
            run_id=actual_run_id,
        ),
    )

    completed = prospective._run_case(
        case,
        session_root=session_root,
        model_path=tmp_path / "model",
        gpu_index=0,
        startup_timeout_seconds=1,
    )

    assert completed.request_plan_digest == actual_request_plan_digest
    assert completed.request_plan_digest != case.resolution.request_plan_digest
    assert completed.run_id == actual_run_id
    assert completed.bundle_digest == actual_bundle_digest


def test_metadata_binds_capture_handoff_publication_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())
    completed, expected_arguments = _metadata_cases(tmp_path)
    session_root = tmp_path / "session"
    _install_fake_reports(completed, monkeypatch)
    receipt_path = prospective._write_session_metadata(
        session_root,
        completed,
        repository_commit="a" * 40,
        host_preparation_sha256=sha256_digest(b"host-preparation"),
        reference=completed[0].case.resolution,
    )

    result = prospective.verify_session(session_root, expected_arguments)

    assert receipt_path.name == "prospective-capture-receipt.json"
    assert result["valid"] is True
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "CAPTURED_PENDING_EXTERNAL_EXITSPEC"
    assert receipt["contract_digests"] == [
        CONTRACT_DIGESTS[case_id] for case_id in prospective.CASE_IDS
    ]
    assert [item["request_plan_digest"] for item in receipt["cases"]] == [
        item.request_plan_digest for item in completed
    ]
    assert all(
        item["exitspec_contract_digest"] in receipt["contract_digests"]
        for item in receipt["cases"]
    )


@pytest.mark.parametrize("field", ["execution_fingerprint", "request_plan_digest"])
def test_metadata_rejects_tampered_run_identity_fields(
    tmp_path: Path,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())
    completed, expected_arguments = _metadata_cases(tmp_path)
    session_root = tmp_path / "session"
    _install_fake_reports(completed, monkeypatch)
    prospective._write_session_metadata(
        session_root,
        completed,
        repository_commit="a" * 40,
        host_preparation_sha256=sha256_digest(b"host-preparation"),
        reference=completed[0].case.resolution,
    )
    capture = json.loads((session_root / "capture-manifest.json").read_text())
    capture["cases"][0][field] = "sha256:" + "f" * 64
    capture_path = session_root / "capture-manifest.json"
    capture_path.chmod(0o600)
    capture_path.write_text(json.dumps(capture))
    capture_path.chmod(0o400)

    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match="bundle linkage disagrees",
    ):
        prospective.verify_session(session_root, expected_arguments)


def test_metadata_rejects_unknown_publication_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prospective, "_static_pin", lambda: _host_pin())
    completed, expected_arguments = _metadata_cases(tmp_path)
    session_root = tmp_path / "session"
    _install_fake_reports(completed, monkeypatch)
    prospective._write_session_metadata(
        session_root,
        completed,
        repository_commit="a" * 40,
        host_preparation_sha256=sha256_digest(b"host-preparation"),
        reference=completed[0].case.resolution,
    )
    publication = session_root / "publication-review.json"
    value = json.loads(publication.read_text())
    value["unexpected"] = "reject"
    publication.chmod(0o600)
    publication.write_text(json.dumps(value))
    publication.chmod(0o400)

    with pytest.raises(
        prospective.ProspectiveCaptureError,
        match="unexpected shape",
    ):
        prospective.verify_session(session_root, expected_arguments)
