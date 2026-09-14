"""Policy checks for manual Vast CPU execution; never dispatch or build images."""

from __future__ import annotations

import os
import re
import subprocess
import time
import types
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/vast-cpu-qualification.yml"
MODES = {
    "qualify-only": "QUALIFY_ONLY",
    "publish-qualified-vast": "PUBLISH_QUALIFIED_VAST",
}
PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
}


def workflow() -> dict[str, Any]:
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 boolean coercion.
    return cast(dict[str, Any], yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader))


def test_workflow_mapping_keys_and_step_ids_are_unique() -> None:
    def visit(node: yaml.Node) -> None:
        if isinstance(node, yaml.MappingNode):
            keys = [key.value for key, _value in node.value]
            assert len(keys) == len(set(keys))
            for _key, value in node.value:
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                visit(value)

    tree = yaml.compose(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert tree is not None
    visit(tree)
    for job in workflow()["jobs"].values():
        identifiers = [step["id"] for step in job["steps"] if "id" in step]
        assert len(identifiers) == len(set(identifiers))


def test_only_manual_matching_modes_and_no_ambient_write_permission() -> None:
    document = workflow()
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["permissions"] == {}
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert all(value["required"] == "true" for value in inputs.values())
    for name in ("execution_mode", "confirmation"):
        assert inputs[name]["type"] == "choice"
        assert inputs[name]["options"] == list(MODES.values())
    assert document["concurrency"]["cancel-in-progress"] == "false"
    assert set(document["jobs"]) == set(MODES)
    for job_name, mode in MODES.items():
        job = document["jobs"][job_name]
        assert f"inputs.execution_mode == '{mode}'" in job["if"]
        assert "github.event_name == 'workflow_dispatch'" in job["if"]
        assert "github.repository == 'jayeshsuyal/inferdrome'" in job["if"]
        assert job["runs-on"] == ["self-hosted", "Linux", "X64", "inferdrome-vast-cpu"]
        assert job["timeout-minutes"] == "110"


@pytest.mark.parametrize("job_name,mode", MODES.items())
@pytest.mark.parametrize(
    "changed,value",
    [
        (None, None),
        ("GITHUB_REPOSITORY", "other/inferdrome"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("SOURCE_COMMIT", "A" * 40),
        ("GITHUB_SHA", "b" * 40),
        ("EXPECTED_REF", "refs/heads/other"),
        ("GITHUB_REF", "refs/pull/81/merge"),
        ("EXECUTION_MODE", "OTHER"),
        ("MODE_CONFIRMATION", "OTHER"),
        ("EXPECTED_RUNNER_NAME", "unsafe/name"),
        ("RUNNER_NAME", "another-worker"),
        ("RUNNER_OS", "macOS"),
        ("RUNNER_ARCH", "ARM64"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
    ],
)
def test_dispatch_guard_rejects_mismatch_before_checkout(
    job_name: str,
    mode: str,
    changed: str | None,
    value: str | None,
) -> None:
    guard = workflow()["jobs"][job_name]["steps"][0]["run"]
    # This test executes shell builtins only, never actions or the build helper.
    assert all(line.startswith(("set ", "[[ ")) for line in guard.splitlines())
    env = {
        "PATH": os.defpath,
        "GITHUB_REPOSITORY": "jayeshsuyal/inferdrome",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "SOURCE_COMMIT": "a" * 40,
        "GITHUB_SHA": "a" * 40,
        "EXPECTED_REF": "refs/heads/codex/add-vast-process-adapter",
        "GITHUB_REF": "refs/heads/codex/add-vast-process-adapter",
        "EXECUTION_MODE": mode,
        "MODE_CONFIRMATION": mode,
        "EXPECTED_RUNNER_NAME": "vast-cpu-test-01",
        "RUNNER_NAME": "vast-cpu-test-01",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "RUNNER_ENVIRONMENT": "self-hosted",
    }
    if changed is not None:
        assert value is not None
        env[changed] = value
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", guard],
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert (result.returncode == 0) == (changed is None)


def test_pinned_checkout_and_commands_keep_inputs_out_of_shell_source() -> None:
    document = workflow()
    legacy = (ROOT / ".github/workflows/role-image-publish.yml").read_text()
    for job in document["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                action, pin = step["uses"].split("@")
                assert pin == PINS[action]
                assert step["uses"] in legacy
                if action == "actions/checkout":
                    assert step["with"]["ref"] == "${{ github.sha }}"
                    assert step["with"]["persist-credentials"] == "false"
            if "run" in step:
                assert "${{" not in step["run"]
                syntax = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-n"],
                    input=step["run"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                assert syntax.returncode == 0, syntax.stderr


def test_original_attempt_identity_and_deadlines_reach_every_helper_call() -> None:
    document = workflow()
    bindings = {
        "mode": "EXECUTION_MODE",
        "source-commit": "SOURCE_COMMIT",
        "expected-ref": "EXPECTED_REF",
        "expected-runner-name": "EXPECTED_RUNNER_NAME",
        "attempt-start": "ATTEMPT_START",
        "execution-deadline": "EXECUTION_DEADLINE",
        "cleanup-deadline": "CLEANUP_DEADLINE",
        "work-root": "WORK_ROOT",
        "attempt-id": "GITHUB_RUN_ID",
        "max-egress-bytes": "MAX_EGRESS_BYTES",
    }
    for job_name, job in document["jobs"].items():
        assert job["env"]["WORK_ROOT"] == (
            "${{ runner.temp }}/inferdrome-vast-${{ github.run_id }}"
        )
        commands = [
            step["run"]
            for step in job["steps"]
            if "run" in step and "-m scripts.vast_cpu_build " in step["run"]
        ]
        expected = ["validate", "qualify", "cleanup"]
        if job_name == "publish-qualified-vast":
            expected.insert(2, "publish")
        assert [
            match[1] if (match := re.search(r"vast_cpu_build (\w+)", text)) else None
            for text in commands
        ] == expected
        for text in commands:
            for flag, variable in bindings.items():
                assert f'--{flag} "${variable}"' in text
        validate_index = next(
            i for i, step in enumerate(job["steps"]) if step.get("id") == "validate"
        )
        bootstrap_index = next(
            i
            for i, step in enumerate(job["steps"])
            if "bootstrap_ci_uv.sh" in step.get("run", "")
        )
        assert validate_index < bootstrap_index
        bootstrap = job["steps"][bootstrap_index]["run"]
        assert 'os.environ["ATTEMPT_START"]' in bootstrap
        assert 'os.environ["EXECUTION_DEADLINE"]' not in bootstrap
        assert "timeout --signal=TERM --kill-after=5s" in bootstrap
        assert "scripts/bootstrap_ci_uv.sh --extra dev" in bootstrap


def clock_program(step: dict[str, Any]) -> str:
    match = re.search(r"python3 - <<'PY'\n(.*?)\nPY(?:\n|$)", step["run"], re.DOTALL)
    assert match is not None
    return match[1]


def evaluate_clock(program: str, elapsed: float, start: str | None = None) -> str:
    """Execute only the extracted stdlib clock arithmetic with fake time and IO."""
    origin = datetime(2030, 1, 1, tzinfo=UTC)
    output = StringIO()
    environment = {
        "ATTEMPT_START": start or origin.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "GITHUB_OUTPUT": "/fake/github-output",
    }

    class Clock:
        @staticmethod
        def strptime(value: str, fmt: str) -> datetime:
            return datetime.strptime(value, fmt)

        @staticmethod
        def now(tz: Any = None) -> datetime:
            assert tz == UTC
            return origin + timedelta(seconds=elapsed)

    class Output:
        def __enter__(self) -> StringIO:
            return output

        def __exit__(self, *_args: Any) -> None:
            pass

    def opened(path: str, mode: str, *, encoding: str) -> Output:
        assert (path, mode, encoding) == ("/fake/github-output", "a", "utf-8")
        return Output()

    def imported(name: str, *_args: Any) -> Any:
        if name == "os":
            return types.SimpleNamespace(environ=environment)
        if name == "datetime":
            return types.SimpleNamespace(UTC=UTC, datetime=Clock, timedelta=timedelta)
        if name == "math":
            import math

            return math
        if name == "time":
            return time
        raise AssertionError("clock snippets may import only the fixed stdlib modules")

    def printed(value: Any, *, file: Any = None) -> None:
        assert file is None or file is output
        output.write(str(value) + "\n")

    scope: dict[str, Any] = {
        "__builtins__": {
            "__import__": imported,
            "open": opened,
            "print": printed,
            "min": min,
            "int": int,
            "str": str,
            "SystemExit": SystemExit,
        }
    }
    exec(compile(program, "<workflow-clock-only>", "exec"), scope)
    return output.getvalue()


@pytest.mark.parametrize("job_name", MODES)
@pytest.mark.parametrize(
    "elapsed,minutes", [(0, 5), (590, 5), (600, 4), (774, 2), (835, 1)]
)
def test_setup_and_checkout_timeout_minutes_consume_original_setup_window(
    job_name: str, elapsed: int, minutes: int
) -> None:
    steps = workflow()["jobs"][job_name]["steps"]
    for action, timer_id, key in (
        ("actions/checkout", "precheckout", "checkout_minutes"),
        ("actions/setup-python", "validate", "setup_minutes"),
    ):
        timer_index = next(
            i for i, step in enumerate(steps) if step.get("id") == timer_id
        )
        action_index = next(
            i
            for i, step in enumerate(steps)
            if step.get("uses", "").startswith(action + "@")
        )
        assert timer_index < action_index
        assert steps[action_index]["timeout-minutes"] == (
            "${{ fromJSON(steps." + timer_id + ".outputs." + key + ") }}"
        )
        assert (
            evaluate_clock(clock_program(steps[timer_index]), elapsed)
            == f"{key}={minutes}\n"
        )


@pytest.mark.parametrize("job_name", MODES)
@pytest.mark.parametrize("elapsed", [-1, 836, 895, 900, 1000])
def test_setup_actions_fail_when_no_whole_minute_and_kill_reserve_remain(
    job_name: str, elapsed: int
) -> None:
    for step in workflow()["jobs"][job_name]["steps"]:
        if step.get("id") in {"precheckout", "validate"}:
            with pytest.raises(SystemExit):
                evaluate_clock(clock_program(step), elapsed)


@pytest.mark.parametrize(
    "elapsed,expected",
    [(0, 600), (300, 595), (894, 1), (894.5, None), (895, None), (1000, None)],
)
def test_dependency_bootstrap_uses_setup_deadline_with_five_second_kill_reserve(
    elapsed: float, expected: int | None
) -> None:
    for job in workflow()["jobs"].values():
        step = next(
            step for step in job["steps"] if "bootstrap_ci_uv.sh" in step.get("run", "")
        )
        program = clock_program(step)
        if expected is None:
            with pytest.raises(SystemExit):
                evaluate_clock(program, elapsed)
        else:
            assert evaluate_clock(program, elapsed) == str(expected) + "\n"


def test_only_publication_step_explicitly_receives_token_after_qualification() -> None:
    document = workflow()
    assert "GHCR_TOKEN" not in document.get("env", {})
    for job_name, job in document["jobs"].items():
        expected = {"contents": "read"}
        if job_name == "publish-qualified-vast":
            expected["packages"] = "write"
        assert job["permissions"] == expected
        assert "GHCR_TOKEN" not in job.get("env", {})
        token_steps = [
            i
            for i, step in enumerate(job["steps"])
            if "GHCR_TOKEN" in step.get("env", {})
        ]
        if job_name == "qualify-only":
            assert token_steps == []
            continue
        assert len(token_steps) == 1
        index = token_steps[0]
        publish = job["steps"][index]
        assert publish["env"]["GHCR_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        assert "vast_cpu_build publish" in publish["run"]
        assert "vast_cpu_build qualify" in job["steps"][index - 1]["run"]
        assert "if" not in publish and "continue-on-error" not in publish
    text = WORKFLOW.read_text()
    assert text.count("secrets.GITHUB_TOKEN") == 1
    for forbidden in (
        "docker login",
        "docker push",
        "gcloud",
        "gh api",
        "--gpus",
        "role_image",
        "rm -rf",
        "prune",
    ):
        assert forbidden not in text


def test_failure_cleanup_and_artifacts_stay_with_the_validated_attempt() -> None:
    for job in workflow()["jobs"].values():
        cleanup, artifact = job["steps"][-2:]
        condition = "${{ always() && steps.validate.outcome == 'success' }}"
        assert cleanup["if"] == condition and artifact["if"] == condition
        assert "python3 -m scripts.vast_cpu_build cleanup" in cleanup["run"]
        assert artifact["with"]["path"] == (
            "${{ runner.temp }}/inferdrome-vast-${{ github.run_id }}/evidence/*.json"
        )
        assert artifact["with"]["retention-days"] == "14"
        assert artifact["with"].get("include-hidden-files", "false") == "false"
        assert artifact["with"]["if-no-files-found"] == "warn"
