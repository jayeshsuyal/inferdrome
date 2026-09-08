"""Static safety checks for the opt-in fixed role-image publication workflow."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/role-image-publish.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_role_image_workflow_is_manual_only_and_has_no_global_package_write() -> None:
    workflow = _workflow_text()

    assert "workflow_dispatch:" in workflow
    for automatic_trigger in ("pull_request:", "push:", "schedule:"):
        assert automatic_trigger not in workflow
    assert "permissions: {}" in workflow
    assert "packages: write" in workflow
    assert "environment:" not in workflow
    assert "timeout-minutes: 45" in workflow
    assert "cancel-in-progress: false" in workflow


def test_role_image_workflow_pins_actions_and_exact_source_identity() -> None:
    workflow = _workflow_text()

    for action in (
        "actions/checkout",
        "actions/setup-python",
        "actions/upload-artifact",
    ):
        assert re.search(rf"uses: {re.escape(action)}@[0-9a-f]{{40}}", workflow)
    assert "persist-credentials: false" in workflow
    assert '[[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert '[[ "$GITHUB_REPOSITORY" == "jayeshsuyal/inferdrome" ]]' in workflow
    assert '[[ "$GITHUB_REF" == "refs/heads/main" ]]' in workflow
    assert '[[ "$SOURCE_COMMIT" == "$GITHUB_SHA" ]]' in workflow
    assert '[[ "$(git rev-parse HEAD)" == "$SOURCE_COMMIT" ]]' in workflow
    assert '[[ -z "$(git status --porcelain)" ]]' in workflow


def test_role_image_workflow_observes_capacity_before_vllm_build_downloads() -> None:
    workflow = _workflow_text()

    assert "runs-on: ubuntu-24.04" in workflow
    assert "timeout-minutes: 45" in workflow
    assert "RUNNER_OS: ${{ runner.os }}" in workflow
    assert "RUNNER_ENVIRONMENT: ${{ runner.environment }}" in workflow
    assert "RUNNER_TEMP: ${{ runner.temp }}" in workflow
    assert "scripts/role_image_runner_disk.py" in workflow
    assert "--runner-os \"$RUNNER_OS\"" in workflow
    assert "--runner-environment \"$RUNNER_ENVIRONMENT\"" in workflow
    assert "--runner-temp \"$RUNNER_TEMP\"" in workflow
    assert '| tee -a "$GITHUB_STEP_SUMMARY"' in workflow
    assert "docker system prune" not in workflow
    assert "docker builder prune" not in workflow
    source_check = "Validate manual confirmation and exact source identity"
    capacity_check = (
        "Observe runner capacity and reclaim disposable Android SDK tooling"
    )
    build = "Build and CPU-smoke both fixed roles before any push"
    assert (
        workflow.index(source_check)
        < workflow.index(capacity_check)
        < workflow.index(build)
    )


def test_role_image_workflow_diagnoses_a_failed_build_without_retrying_cleanup(
) -> None:
    workflow = _workflow_text()

    build = "Build and CPU-smoke both fixed roles before any push"
    diagnosis = "Diagnose capacity after a failed role-image build"
    publish = "Push both role images and emit immutable digest outputs"
    after_diagnosis = workflow.split(diagnosis, maxsplit=1)[1]
    diagnostic_step = after_diagnosis.split(publish, maxsplit=1)[0]

    assert "if: ${{ failure() && steps.build.outcome == 'failure' }}" in diagnostic_step
    assert "continue-on-error: true" in diagnostic_step
    assert "--diagnose-only" in diagnostic_step
    assert "--runner-temp \"$RUNNER_TEMP\"" in diagnostic_step
    assert "rm -rf" not in diagnostic_step
    assert workflow.index(build) < workflow.index(diagnosis) < workflow.index(publish)


def test_role_image_workflow_is_fixed_role_cpu_smoked_and_digest_bound() -> None:
    workflow = _workflow_text()

    assert "ghcr.io/jayeshsuyal/inferdrome-private-engine" in workflow
    assert "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer" in workflow
    assert "for role in private-engine cpu-runner-observer; do" in workflow
    assert "--flavor release" in workflow
    assert "--platform linux/amd64" in workflow
    assert "--network none" in workflow
    assert "--user 2000:0" in workflow
    assert "--cap-drop=ALL" in workflow
    assert "inferdrome.deployment.gcp_private_engine_adapter" in workflow
    assert "inferdrome.deployment.gcp_private_runner_adapter" in workflow
    assert 'run_cpu_smoke "$tag" "$version" "$adapter_module"' in workflow
    assert "--password-stdin" in workflow
    assert "validate-digest-pair" in workflow
    assert "private_engine_image" in workflow
    assert "cpu_runner_observer_image" in workflow
    assert "publication_pair_sha256" in workflow
    assert "Retain the canonical paired-digest record" in workflow
    assert "digest-pair.json" in workflow
    assert "full-labels.json" not in workflow.split(
        "Retain the canonical paired-digest record", maxsplit=1
    )[1]
    assert "docker-config" not in workflow.split(
        "Retain the canonical paired-digest record", maxsplit=1
    )[1]
    assert workflow.index("run_cpu_smoke") < workflow.index('docker push "$tag"')
