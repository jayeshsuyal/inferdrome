"""Subprocess supervision is bounded and preserves exact producer diagnostics."""

import sys
from pathlib import Path

import pytest

from inferdrome.errors import AdapterError, CancellationRequested
from inferdrome.execution import ProcessTermination, run_captured_process
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
    TerminationPolicy,
)


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_normal_exit_preserves_exact_separate_streams(tmp_path: Path) -> None:
    capture = run_captured_process(
        _python(
            "import os; os.write(1, b'producer-out\\x00'); "
            "os.write(2, b'producer-err\\n')"
        ),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
    )

    assert capture.exit_status == 0
    assert capture.termination is ProcessTermination.EXITED
    assert capture.stdout == b"producer-out\x00"
    assert capture.stderr == b"producer-err\n"
    assert capture.argv[0] == sys.executable
    assert capture.ended_at >= capture.started_at


def test_nonzero_exit_is_captured_without_rewriting_diagnostics(
    tmp_path: Path,
) -> None:
    capture = run_captured_process(
        _python("import os; os.write(2, b'failed'); raise SystemExit(7)"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
    )

    assert capture.exit_status == 7
    assert capture.termination is ProcessTermination.EXITED
    assert capture.stdout == b""
    assert capture.stderr == b"failed"


def test_merged_capture_keeps_version_probe_output_in_one_stream(
    tmp_path: Path,
) -> None:
    capture = run_captured_process(
        _python("import os; os.write(1, b'log\\n'); os.write(2, b'0.26.0\\n')"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
        merge_stderr=True,
    )

    assert capture.termination is ProcessTermination.EXITED
    assert capture.stdout == b"log\n0.26.0\n"
    assert capture.stderr == b""


def test_deadline_terminates_process_with_bounded_wait(tmp_path: Path) -> None:
    capture = run_captured_process(
        _python("import time; time.sleep(5)"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=0.05,
        termination_policy=TerminationPolicy(
            graceful_timeout_seconds=0.2,
            forced_timeout_seconds=0.2,
        ),
    )

    assert capture.termination is ProcessTermination.DEADLINE
    assert capture.exit_status != 0


def test_output_overflow_terminates_or_invalidates_capture(tmp_path: Path) -> None:
    capture = run_captured_process(
        _python("import os; os.write(1, b'x' * 1000000)"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
        output_limit_bytes=1_024,
        termination_policy=TerminationPolicy(
            graceful_timeout_seconds=0.2,
            forced_timeout_seconds=0.2,
        ),
    )

    assert capture.termination is ProcessTermination.OUTPUT_LIMIT
    assert capture.stdout == b"x" * 1_024
    assert len(capture.stderr) == 0


def test_parent_cannot_leave_pipe_holding_descendants_running(
    tmp_path: Path,
) -> None:
    capture = run_captured_process(
        _python(
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "print('spawned', flush=True)"
        ),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
        termination_policy=TerminationPolicy(
            graceful_timeout_seconds=0.2,
            forced_timeout_seconds=0.5,
        ),
    )

    assert capture.exit_status == 0
    assert capture.termination is ProcessTermination.ORPHANED_DESCENDANTS
    assert capture.stdout == b"spawned\n"


def test_pre_requested_cancellation_prevents_process_launch(tmp_path: Path) -> None:
    cancellation = CancellationToken()
    cancellation.request(CancellationReason.USER)

    with pytest.raises(CancellationRequested, match="USER"):
        run_captured_process(
            _python("raise SystemExit(0)"),
            cwd=tmp_path.resolve(),
            max_runtime_seconds=2,
            cancellation=cancellation,
        )


@pytest.mark.parametrize(
    ("runtime", "output_limit"),
    [
        (0, 1),
        (float("nan"), 1),
        (1, 0),
        (1, True),
    ],
)
def test_invalid_resource_bounds_fail_before_launch(
    tmp_path: Path,
    runtime: float,
    output_limit: int,
) -> None:
    with pytest.raises(AdapterError, match="limit"):
        run_captured_process(
            _python("raise SystemExit(0)"),
            cwd=tmp_path.resolve(),
            max_runtime_seconds=runtime,
            output_limit_bytes=output_limit,
        )
