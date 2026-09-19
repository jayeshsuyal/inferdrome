"""Offline checks for the versioned ordinary-container operator packet."""

from __future__ import annotations

from pathlib import Path

import pytest

from inferdrome.evaluation import vast_container_operator as operator
from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE
from tests.unit.test_load_calibration_operator import _prepared


def _inputs(tmp_path: Path) -> tuple[Path, tuple[Path, Path]]:
    _prepared(tmp_path)
    root = tmp_path / "inputs"
    return root / "protocol.json", (root / "load-low.json", root / "load-high.json")


def test_preflight_observes_executable_but_never_constructs_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, recipes = _inputs(tmp_path)

    def unexpected_engine(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("offline preflight constructed an engine")

    monkeypatch.setattr(
        operator, "TwoEngineVllmDirectProcessLifecycle", unexpected_engine
    )
    first = operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VLLM_RUNTIME_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )
    second = operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VLLM_RUNTIME_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )
    assert operator.vast_container_preflight_bytes(
        first.preflight
    ) == operator.vast_container_preflight_bytes(second.preflight)
    assert (
        first.preflight.runtime_identity.outer_image_state
        == "DECLARED_BY_OPERATOR_UNVERIFIED"
    )
    assert (
        first.preflight.runtime_identity.executable_observation
        == "LOCAL_METADATA_OBSERVED"
    )
    assert first.preflight.provider_action_performed is False
    assert first.preflight.evidence_eligible is False


def test_preflight_rejects_an_unpinned_outer_image_before_any_runtime(
    tmp_path: Path,
) -> None:
    protocol, recipes = _inputs(tmp_path)
    with pytest.raises(EvaluationError, match="outer image"):
        operator.prepare_vast_container_rehearsal(
            protocol_path=protocol,
            recipe_paths=recipes,
            runtime="vllm",
            outer_image_reference="vllm/vllm-openai:latest",
            runtime_executable="/bin/sh",
        )


def test_cli_preflight_writes_only_a_canonical_offline_packet(tmp_path: Path) -> None:
    protocol, recipes = _inputs(tmp_path)
    output_root = tmp_path / "preflight-output"
    output_root.mkdir()
    output = output_root / "preflight.json"
    assert (
        main(
            [
                "load-calibration-vast-container-preflight",
                "--protocol",
                str(protocol),
                "--recipe",
                str(recipes[0]),
                "--recipe",
                str(recipes[1]),
                "--runtime",
                "vllm",
                "--outer-image-reference",
                VLLM_RUNTIME_IMAGE_REFERENCE,
                "--runtime-executable",
                "/bin/sh",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_bytes().endswith(b"\n")
    assert output.read_bytes() == operator.vast_container_preflight_bytes(
        operator.prepare_vast_container_rehearsal(
            protocol_path=protocol,
            recipe_paths=recipes,
            runtime="vllm",
            outer_image_reference=VLLM_RUNTIME_IMAGE_REFERENCE,
            runtime_executable="/bin/sh",
        ).preflight
    )


def test_run_requires_the_new_exact_direct_process_confirmation(tmp_path: Path) -> None:
    protocol, recipes = _inputs(tmp_path)
    assert (
        main(
            [
                "load-calibration-vast-container-run",
                "--protocol",
                str(protocol),
                "--recipe",
                str(recipes[0]),
                "--recipe",
                str(recipes[1]),
                "--runtime",
                "vllm",
                "--outer-image-reference",
                VLLM_RUNTIME_IMAGE_REFERENCE,
                "--runtime-executable",
                "/bin/sh",
                "--authorization",
                str(tmp_path / "not-read.json"),
                "--model-snapshot",
                str(tmp_path / "not-read-model"),
                "--output-root",
                str(tmp_path / "not-read-output"),
                "--execute-approval",
                "wrong",
            ]
        )
        == 2
    )
