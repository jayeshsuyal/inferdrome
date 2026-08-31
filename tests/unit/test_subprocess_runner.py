"""Subprocess supervision is bounded and preserves exact producer diagnostics."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

from inferdrome.errors import AdapterError, CancellationRequested
from inferdrome.execution import ProcessTermination, run_captured_process
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
    TerminationPolicy,
)
from inferdrome.execution.subprocess_runner import (
    diagnostics_contain_credentials,
    redact_subprocess_diagnostics,
    resolve_executable_identity,
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


def test_default_environment_does_not_inherit_ambient_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUDIT_UNRELATED_MARKER", "synthetic-placeholder")
    monkeypatch.setenv("LAMBDA_CLOUD_API_KEY", "synthetic-placeholder")

    capture = run_captured_process(
        _python(
            "import os; raise SystemExit("
            "0 if 'AUDIT_UNRELATED_MARKER' not in os.environ "
            "and 'LAMBDA_CLOUD_API_KEY' not in os.environ else 9)"
        ),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
    )

    assert capture.exit_status == 0


def test_credential_shaped_producer_diagnostics_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="credential-shaped output"):
        run_captured_process(
            _python(
                "import os; "
                "os.write(1, b'LAMBDA_CLOUD_API_KEY=synthetic-placeholder\\n'); "
                "os.write(2, b'Authorization: Bearer synthetic-placeholder\\n')"
            ),
            cwd=tmp_path.resolve(),
            max_runtime_seconds=2,
        )


@pytest.mark.parametrize(
    "content",
    [
        b"API_KEY synthetic-placeholder\n",
        b"API key: synthetic-placeholder\n",
        b"Authorization: Bearer synthetic-placeholder\n",
        b"Bearer synthetic-placeholder\n",
        b"HF_TOKEN=synthetic-placeholder\n",
        b"GITHUB_TOKEN=synthetic-placeholder\n",
        b"TOKEN=synthetic-placeholder\n",
        b"GOOGLE_APPLICATION_CREDENTIALS=/synthetic/placeholder.json\n",
        b"https://operator:synthetic-placeholder@example.test/\n",
        b"https://example.test/?X-Amz-Signature=synthetic-placeholder\n",
        b"https://example.test/?sig=synthetic-placeholder\n",
        (
            b"-----BEGIN PRIVATE KEY-----\nsynthetic-placeholder\n"
            b"-----END PRIVATE KEY-----\n"
        ),
        b"prefix\n-----BEGIN RSA PRIVATE KEY-----\nsynthetic-placeholder",
    ],
)
def test_diagnostic_redactor_covers_common_credential_shapes(content: bytes) -> None:
    redacted = redact_subprocess_diagnostics(content)

    assert b"synthetic-placeholder" not in redacted
    assert b"[REDACTED" in redacted


@pytest.mark.parametrize(
    "content",
    [
        b"AWS_ACCESS_KEY_ID=SYNTHETICACCESSKEY1234\n",
        (
            b'DOCKER_AUTH_CONFIG={"auths":{"registry.example.test":'
            b'{"auth":"synthetic-placeholder"}}}\n'
        ),
        b"CI_JOB_JWT=synthetic-header.synthetic-payload.synthetic-signature\n",
        (
            b"AZURE_STORAGE_CONNECTION_STRING=AccountName=synthetic;"
            b"AccountKey=synthetic-placeholder\n"
        ),
    ],
    ids=("aws-access-id", "docker-auth", "ci-jwt", "azure-connection"),
)
def test_diagnostic_classifier_covers_cloud_credential_assignments(
    content: bytes,
) -> None:
    assert diagnostics_contain_credentials(content)
    assert content != redact_subprocess_diagnostics(content)


def test_benign_token_label_is_preserved_exactly(tmp_path: Path) -> None:
    capture = run_captured_process(
        _python("print('token: 42')"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
    )

    assert capture.stdout == b"token: 42\n"


def test_display_argv_can_bind_a_validated_absolute_executable(
    tmp_path: Path,
) -> None:
    identity = resolve_executable_identity(sys.executable)

    capture = run_captured_process(
        ("vllm", "-c", "print('bound')"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
        executable_identity=identity,
    )

    assert capture.argv[0] == "vllm"
    assert capture.stdout == b"bound\n"


def test_changed_executable_identity_fails_before_launch(tmp_path: Path) -> None:
    executable = tmp_path / "synthetic-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    identity = resolve_executable_identity(str(executable))
    executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="identity changed"):
        run_captured_process(
            (str(executable),),
            cwd=tmp_path.resolve(),
            max_runtime_seconds=2,
            executable_identity=identity,
        )


def test_start_observer_receives_isolated_process_identity(tmp_path: Path) -> None:
    observed: list[tuple[int, datetime]] = []
    capture = run_captured_process(
        _python("raise SystemExit(0)"),
        cwd=tmp_path.resolve(),
        max_runtime_seconds=2,
        on_start=lambda pid, started_at: observed.append((pid, started_at)),
    )

    assert len(observed) == 1
    assert observed[0][0] >= 2
    assert observed[0][1] == capture.started_at


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
