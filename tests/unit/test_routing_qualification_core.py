"""Deterministic qualification facts over the sealed R1 routing package."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.errors import VerificationError
from inferdrome.routing_campaign import run_campaign
from inferdrome.routing_campaign.canonical import canonical_json_bytes
from inferdrome.routing_qualification.contracts import TerminalPopulation
from inferdrome.routing_qualification.qualification import (
    QUALIFICATION_ARTIFACT_ID,
    QUALIFICATION_FILENAME,
    CapturedQualification,
    StaleTelemetryQualificationError,
    capture_qualification,
    publish_qualification,
    qualification_digest,
    run_qualification,
    verify_qualification,
    verify_qualification_descriptor,
)

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def _run_source(output: Path):
    return run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output,
    )


def _make_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def test_capture_is_byte_stable_and_exposes_the_fixed_falsifiable_vector(
    tmp_path: Path,
) -> None:
    first = _run_source(tmp_path / "source-a")
    second = _run_source(tmp_path / "source-b")

    captured_a = capture_qualification(
        first.path,
        expected_source_digest=first.retained_digest,
    )
    captured_b = capture_qualification(
        second.path,
        expected_source_digest=second.retained_digest,
    )

    assert captured_a.canonical_bytes == captured_b.canonical_bytes
    assert captured_a.retained_digest == captured_b.retained_digest
    assert captured_a.descriptor.source_package_retained_digest == first.retained_digest
    assert captured_a.descriptor.fault_timeline.model_dump(mode="json") == {
        "first_stale_load_fresh_health_decision_time_ms": 20,
        "freshness_bound_ms": 5,
        "health_age_ms": 0,
        "health_continues": True,
        "load_age_ms": 10,
        "load_observer_pause_at_ms": 15,
    }
    assert [trial.policy_id for trial in captured_a.descriptor.trials] == [
        "fail_closed_required_load_v1",
        "explicit_fail_open_stale_load_v1",
        "typed_admissible_state_only_v1",
    ]
    assert all(trial.repetition_index == 0 for trial in captured_a.descriptor.trials)
    assert all(trial.request_denominator == 6 for trial in captured_a.descriptor.trials)
    trials = captured_a.descriptor.trials
    assert all(trial.state_observation_count == 36 for trial in trials)
    assert all(trial.route_decision_count == 6 for trial in trials)
    assert all(trial.terminal_outcome_count == 6 for trial in trials)
    assert [trial.terminal_population.values for trial in trials] == [
        {
            "SUCCEEDED": 2,
            "TIMED_OUT": 0,
            "FAILED": 0,
            "CANCELLED": 0,
            "NO_SAFE_ROUTE": 4,
        },
        {
            "SUCCEEDED": 2,
            "TIMED_OUT": 4,
            "FAILED": 0,
            "CANCELLED": 0,
            "NO_SAFE_ROUTE": 0,
        },
        {
            "SUCCEEDED": 6,
            "TIMED_OUT": 0,
            "FAILED": 0,
            "CANCELLED": 0,
            "NO_SAFE_ROUTE": 0,
        },
    ]

    verified = verify_qualification_descriptor(
        captured_a.canonical_bytes,
        campaign_package=first.path,
        expected_descriptor_digest=captured_a.retained_digest,
    )
    assert verified == captured_a


def test_canonical_reparse_accepts_sorted_terminal_population_keys(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)

    reparsed = verify_qualification_descriptor(
        captured.canonical_bytes,
        campaign_package=source.path,
        expected_descriptor_digest=captured.retained_digest,
    )

    assert reparsed.descriptor == captured.descriptor


def test_terminal_population_requires_every_terminal_without_key_order_dependence(
) -> None:
    value = TerminalPopulation(
        values={
            "NO_SAFE_ROUTE": 4,
            "CANCELLED": 0,
            "FAILED": 0,
            "TIMED_OUT": 0,
            "SUCCEEDED": 2,
        }
    )
    assert sum(value.values.values()) == 6
    with pytest.raises(ValidationError, match="fixed inventory"):
        TerminalPopulation(
            values={
                "SUCCEEDED": 6,
                "TIMED_OUT": 0,
                "FAILED": 0,
                "CANCELLED": 0,
            }
        )


def test_publication_is_no_replace_immutable_and_read_back_verified(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "qualification"

    sealed = publish_qualification(
        captured,
        campaign_package=source.path,
        output_root=output_root,
    )

    assert sealed.path == output_root / QUALIFICATION_ARTIFACT_ID
    assert sealed.descriptor_path == sealed.path / QUALIFICATION_FILENAME
    assert stat.S_IMODE(os.lstat(sealed.path).st_mode) == 0o500
    assert stat.S_IMODE(os.lstat(sealed.descriptor_path).st_mode) == 0o400
    assert verify_qualification(
        output_root,
        campaign_package=source.path,
        expected_descriptor_digest=sealed.retained_digest,
    ) == captured
    with pytest.raises(FileExistsError):
        publish_qualification(
            captured,
            campaign_package=source.path,
            output_root=output_root,
        )


def test_publisher_rejects_a_forged_capture_before_consuming_its_output_id(
    tmp_path: Path,
) -> None:
    """A caller cannot use publication to seal arbitrary unverified bytes."""

    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "forged-qualification"
    forged = CapturedQualification(
        descriptor=captured.descriptor,
        canonical_bytes=b"{}",
        retained_digest=qualification_digest(b"{}"),
    )

    with pytest.raises(
        StaleTelemetryQualificationError,
        match="strict contract",
    ):
        publish_qualification(
            forged,
            campaign_package=source.path,
            output_root=output_root,
        )

    assert not output_root.exists()


def test_publisher_rejects_mismatched_capture_metadata_before_publication(
    tmp_path: Path,
) -> None:
    """The retained Python object cannot disagree with its canonical bytes."""

    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "mismatched-qualification"
    forged = CapturedQualification(
        descriptor=captured.descriptor.model_copy(
            update={"qualification_id": "forged-qualification"}
        ),
        canonical_bytes=captured.canonical_bytes,
        retained_digest=captured.retained_digest,
    )

    with pytest.raises(
        StaleTelemetryQualificationError,
        match="disagrees with its canonical descriptor",
    ):
        publish_qualification(
            forged,
            campaign_package=source.path,
            output_root=output_root,
        )

    assert not output_root.exists()
    assert publish_qualification(
        captured,
        campaign_package=source.path,
        output_root=output_root,
    ).retained_digest == captured.retained_digest


def test_publisher_rebinds_the_source_before_creating_its_output_root(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "unbound-qualification"

    with pytest.raises(
        StaleTelemetryQualificationError,
        match="not independently verified",
    ):
        publish_qualification(
            captured,
            campaign_package=tmp_path / "missing-source",
            output_root=output_root,
        )

    assert not output_root.exists()


def test_descriptor_tampering_is_detected_by_external_digest_and_source_binding(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "qualification"
    sealed = publish_qualification(
        captured,
        campaign_package=source.path,
        output_root=output_root,
    )
    try:
        _make_writable(sealed.path)
        payload = json.loads(sealed.descriptor_path.read_text(encoding="utf-8"))
        payload["source_inputs"]["campaign_plan_sha256"] = f"sha256:{'0' * 64}"
        tampered = canonical_json_bytes(payload)
        sealed.descriptor_path.write_bytes(tampered)
        sealed.descriptor_path.chmod(0o400)
        sealed.path.chmod(0o500)

        with pytest.raises(VerificationError, match="digest disagrees"):
            verify_qualification(
                output_root,
                campaign_package=source.path,
                expected_descriptor_digest=captured.retained_digest,
            )
        with pytest.raises(VerificationError, match="source campaign"):
            verify_qualification(
                output_root,
                campaign_package=source.path,
                expected_descriptor_digest=qualification_digest(tampered),
            )
    finally:
        _make_writable(output_root)


def test_descriptor_verifier_rejects_noncanonical_duplicate_and_cross_trial_data(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    payload = json.loads(captured.canonical_bytes)

    noncanonical = json.dumps(payload).encode("utf-8")
    with pytest.raises(StaleTelemetryQualificationError, match="not canonical"):
        verify_qualification_descriptor(
            noncanonical,
            campaign_package=source.path,
            expected_descriptor_digest=qualification_digest(noncanonical),
        )
    duplicate = (
        b'{"schema_version":"inferdrome.stale-telemetry-qualification.v1",'
        b'"schema_version":"inferdrome.stale-telemetry-qualification.v1"}'
    )
    with pytest.raises(StaleTelemetryQualificationError, match="duplicate JSON keys"):
        verify_qualification_descriptor(
            duplicate,
            campaign_package=source.path,
            expected_descriptor_digest=qualification_digest(duplicate),
        )

    payload["trials"][0]["terminal_population"] = payload["trials"][1][
        "terminal_population"
    ]
    crossed = canonical_json_bytes(payload)
    with pytest.raises(VerificationError, match="source campaign"):
        verify_qualification_descriptor(
            crossed,
            campaign_package=source.path,
            expected_descriptor_digest=qualification_digest(crossed),
        )


def test_capture_rejects_a_wrong_expected_source_digest(tmp_path: Path) -> None:
    source = _run_source(tmp_path / "source")
    with pytest.raises(
        StaleTelemetryQualificationError,
        match="not independently verified",
    ):
        capture_qualification(
            source.path,
            expected_source_digest=f"sha256:{'0' * 64}",
        )


def test_capture_fails_closed_when_the_bound_source_package_is_tampered(
    tmp_path: Path,
) -> None:
    source = _run_source(tmp_path / "source")
    plan = source.path / "campaign-plan.json"
    try:
        source.path.chmod(0o700)
        plan.chmod(0o600)
        plan.write_bytes(b"{}")
        plan.chmod(0o400)
        source.path.chmod(0o500)
        with pytest.raises(
            StaleTelemetryQualificationError,
            match="not independently verified",
        ):
            capture_qualification(source.path)
    finally:
        _make_writable(source.path)


def test_verification_does_not_invoke_the_producer_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _run_source(tmp_path / "source")
    captured = capture_qualification(source.path)
    sealed = publish_qualification(
        captured,
        campaign_package=source.path,
        output_root=tmp_path / "qualification",
    )

    import inferdrome.routing_campaign.package as routing_package

    def producer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("producer ran")

    monkeypatch.setattr(
        routing_package,
        "execute_campaign",
        producer_must_not_run,
    )
    assert verify_qualification(
        tmp_path / "qualification",
        campaign_package=source.path,
        expected_descriptor_digest=sealed.retained_digest,
    ) == captured


def test_one_command_run_keeps_the_qualification_outside_the_source_package(
    tmp_path: Path,
) -> None:
    sealed = run_qualification(
        campaign_plan=_INPUTS / "stale-load-fresh-health.plan.json",
        request_trace=_INPUTS / "stale-load-fresh-health.trace.jsonl",
        fault_schedule=_INPUTS / "stale-load-fresh-health.fault-schedule.json",
        trial_plan=_INPUTS / "trial-plan.json",
        campaign_output=tmp_path / "campaign",
        qualification_output_root=tmp_path / "qualification",
    )
    assert verify_qualification(
        tmp_path / "qualification",
        campaign_package=sealed.campaign.path,
        expected_descriptor_digest=sealed.qualification.retained_digest,
    ).retained_digest == sealed.qualification.retained_digest
    with pytest.raises(StaleTelemetryQualificationError, match="must not be inside"):
        run_qualification(
            campaign_plan=_INPUTS / "stale-load-fresh-health.plan.json",
            request_trace=_INPUTS / "stale-load-fresh-health.trace.jsonl",
            fault_schedule=_INPUTS / "stale-load-fresh-health.fault-schedule.json",
            trial_plan=_INPUTS / "trial-plan.json",
            campaign_output=tmp_path / "nested-campaign",
            qualification_output_root=tmp_path / "nested-campaign" / "qualification",
        )
