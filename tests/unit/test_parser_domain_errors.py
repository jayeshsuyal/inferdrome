"""Malformed structured inputs stay inside stable bounded domain errors."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import scripts.prospective_handoff as prospective_handoff
import scripts.prospective_real_gpu_capture as prospective_capture
import scripts.real_gpu_capture as real_capture
import scripts.review_gpu_evidence_publication as publication_review
from inferdrome.bundle.reader import strict_json_value
from inferdrome.errors import SourceInputError, VerificationError
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.resolution.workload import parse_custom_workload
from inferdrome.resolution.yaml_loader import load_strict_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _deep_json() -> bytes:
    return ("[" * 65 + "0" + "]" * 65).encode()


def _large_integer_json() -> bytes:
    return ('{"value":' + "9" * 257 + "}").encode()


@pytest.mark.parametrize("payload", [_deep_json(), _large_integer_json()])
@pytest.mark.parametrize(
    "parse,error_type",
    [
        (
            lambda content: strict_json_value(content, label="probe"),
            VerificationError,
        ),
        (
            lambda content: parse_custom_workload(content + b"\n"),
            SourceInputError,
        ),
        (
            lambda content: real_capture._strict_json_bytes(
                content,
                label="probe",
            ),
            real_capture.CaptureError,
        ),
        (
            lambda content: prospective_handoff._strict_json(
                content,
                label="probe",
            ),
            prospective_handoff.ProspectiveHandoffError,
        ),
        (
            lambda content: prospective_capture._strict_json_bytes(
                content,
                label="probe",
            ),
            prospective_capture.ProspectiveCaptureError,
        ),
        (
            lambda content: publication_review._strict_json_value(
                content,
                label="probe",
            ),
            publication_review.PublicationReviewError,
        ),
    ],
)
def test_json_boundaries_raise_only_stable_domain_errors(
    parse: Callable[[bytes], Any],
    error_type: type[Exception],
    payload: bytes,
) -> None:
    with pytest.raises(error_type) as raised:
        parse(payload)

    assert raised.value.__suppress_context__ is True
    assert len(str(raised.value)) <= 160
    assert "9" * 40 not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        ("[" * 65 + "0" + "]" * 65).encode(),
        ("value: 0x" + "f" * 257 + "\n").encode(),
        ("value: 1" + ":59" * 128 + "\n").encode(),
    ],
)
@pytest.mark.parametrize(
    "parse,error_type",
    [
        (load_strict_yaml, SourceInputError),
        (
            lambda content: prospective_handoff._strict_source_yaml(
                content,
                label="probe",
            ),
            prospective_handoff.ProspectiveHandoffError,
        ),
    ],
)
def test_yaml_boundaries_raise_only_stable_domain_errors(
    parse: Callable[[bytes], Any],
    error_type: type[Exception],
    payload: bytes,
) -> None:
    with pytest.raises(error_type) as raised:
        parse(payload)

    assert raised.value.__suppress_context__ is True
    assert len(str(raised.value)) <= 160


def test_parser_failure_cannot_reserve_a_partial_run_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "experiment.yaml"
    source.write_text(
        (REPOSITORY_ROOT / "examples" / "fake-smoke.yaml").read_text()
        + "\nparser_bomb: "
        + "[" * 65
        + "0"
        + "]" * 65
        + "\n",
        encoding="utf-8",
    )
    runs_root = tmp_path / "runs"

    with pytest.raises(SourceInputError):
        run_experiment(source, runs_root=runs_root)

    assert not runs_root.exists()
