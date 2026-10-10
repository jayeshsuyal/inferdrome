"""The public Breakpoint entry point preserves argument and exit semantics."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import Any

import pytest

from inferdrome.cli import main


def _invoke(arguments: list[str]) -> int:
    try:
        return main(arguments)
    except SystemExit as error:
        assert isinstance(error.code, int)
        return error.code


@pytest.mark.parametrize(
    ("stage", "module"),
    [
        ("search", "inferdrome.vllm_bounded_search"),
        ("reduce", "inferdrome.vllm_witness_reducer"),
        ("confirm", "inferdrome.vllm_heldout_confirmation"),
    ],
)
def test_public_dispatch_forwards_explicit_arguments_without_mutating_sys_argv(
    monkeypatch: pytest.MonkeyPatch, stage: str, module: str
) -> None:
    original = ["unrelated-program", "--must-not-be-parsed"]
    monkeypatch.setattr(sys, "argv", original)
    seen: list[list[str]] = []

    def delegated(argv: Any = None) -> None:
        seen.append(list(argv))
        assert sys.argv is original

    monkeypatch.setattr(import_module(module), "main", delegated)
    arguments = ["breakpoint", stage, "verify", "--report", "private report.json"]
    before = list(arguments)
    assert _invoke(arguments) == 0
    assert seen == [["verify", "--report", "private report.json"]]
    assert arguments == before
    assert sys.argv is original
    assert original == ["unrelated-program", "--must-not-be-parsed"]


@pytest.mark.parametrize("status", [0, 2])
def test_delegated_exit_status_is_preserved(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def delegated(_argv: Any = None) -> None:
        raise SystemExit(status)

    monkeypatch.setattr(
        import_module("inferdrome.vllm_heldout_confirmation"), "main", delegated
    )
    assert _invoke(["breakpoint", "confirm", "verify"]) == status


@pytest.mark.parametrize(
    "error", [ValueError("invalid evidence"), OSError("missing artifact")]
)
def test_artifact_errors_return_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    def delegated(_argv: Any = None) -> None:
        raise error

    monkeypatch.setattr(
        import_module("inferdrome.vllm_witness_reducer"), "main", delegated
    )
    assert _invoke(["breakpoint", "reduce", "verify"]) == 1
    output = capsys.readouterr()
    assert str(error) in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "stage", [None, "search", "reduce", "confirm", "summarize", "demo"]
)
def test_public_help_is_accessible_without_execution(
    capsys: pytest.CaptureFixture[str], stage: str | None
) -> None:
    arguments = ["breakpoint"] + ([] if stage is None else [stage]) + ["--help"]
    assert _invoke(arguments) == 0
    output = capsys.readouterr()
    assert "usage:" in output.out
    assert "Traceback" not in output.err


def test_top_level_help_lists_breakpoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _invoke(["--help"]) == 0
    assert "breakpoint" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        "plan",
        "prepare",
        "verify",
        "rehearse",
        "run",
        "guard",
        "export",
        "verify-export",
    ],
)
def test_pilot_help_never_runs_paid_actions(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _invoke(["breakpoint", "pilot", command, "--help"]) == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments", [["breakpoint", "unknown"], ["breakpoint", "confirm", "unknown"]]
)
def test_unknown_commands_fail_with_parser_status(arguments: list[str]) -> None:
    assert _invoke(arguments) == 2


def test_demo_dispatch_preserves_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def delegated(argv: Any = None) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(import_module("inferdrome.breakpoint_demo"), "main", delegated)
    assert _invoke(["breakpoint", "demo", "--output-dir", "private output"]) == 0
    assert seen == [["--output-dir", "private output"]]
