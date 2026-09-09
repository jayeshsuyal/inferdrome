"""Static safety checks for the manual fixed role-image workflow."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/role-image-publish.yml"
BUILD_AND_SMOKE = REPOSITORY_ROOT / "scripts/build_role_images_and_smoke.sh"
WORKER_MONITOR = REPOSITORY_ROOT / "scripts/role_image_worker_monitor.py"
PUBLICATION_RUNBOOK = REPOSITORY_ROOT / "docs/ROLE_IMAGE_PUBLICATION_V1.md"
PRE_GPU_CHECKLIST = REPOSITORY_ROOT / "docs/PRE_GPU_READY_CHECKLIST_V1.md"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, name: str, following_name: str | None = None) -> str:
    start = workflow.index(f"  {name}:\n")
    if following_name is None:
        return workflow[start:]
    end = workflow.index(f"  {following_name}:\n", start + 1)
    return workflow[start:end]


def test_role_image_workflow_is_manual_only_and_has_no_global_package_write() -> None:
    workflow = _workflow_text()

    assert "workflow_dispatch:" in workflow
    for automatic_trigger in ("pull_request:", "push:", "schedule:"):
        assert automatic_trigger not in workflow
    assert "permissions: {}" in workflow
    assert "environment:" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "BUILD_AND_SMOKE_ONLY" in workflow
    assert "PUBLISH_FIXED_ROLE_IMAGES" in workflow
    assert "expected_runner_name:" in workflow


def test_dispatch_guard_fails_closed_before_any_self_hosted_worker_job() -> None:
    workflow = _workflow_text()
    guard = _job(workflow, "validate-dispatch", "build-and-smoke-only")

    assert "runs-on: ubuntu-24.04" in guard
    assert "timeout-minutes: 5" in guard
    assert "packages: write" not in guard
    assert '[[ "$GITHUB_REPOSITORY" == "jayeshsuyal/inferdrome" ]]' in guard
    assert '[[ "$GITHUB_REF" == "refs/heads/main" ]]' in guard
    assert '[[ "$SOURCE_COMMIT" == "$GITHUB_SHA" ]]' in guard
    assert '[[ "$(git rev-parse HEAD)" == "$SOURCE_COMMIT" ]]' in guard
    assert '[[ -z "$(git status --porcelain)" ]]' in guard
    assert '[[ "$EXPECTED_RUNNER_NAME" =~ ^[a-z][a-z0-9-]{2,62}$ ]]' in guard
    assert "BUILD_AND_SMOKE_ONLY:BUILD_AND_SMOKE_ONLY" in guard
    assert "PUBLISH_FIXED_ROLE_IMAGES:PUBLISH_FIXED_ROLE_IMAGES" in guard


def test_build_test_mode_has_no_registry_authority() -> None:
    workflow = _workflow_text()
    build_test = _job(workflow, "build-and-smoke-only", "publish-fixed-role-images")

    assert "needs: validate-dispatch" in build_test
    assert "if: ${{ inputs.execution_mode == 'BUILD_AND_SMOKE_ONLY' }}" in build_test
    assert "runs-on: [self-hosted, Linux, X64, inferdrome-role-image-cpu]" in build_test
    assert "timeout-minutes: 45" in build_test
    assert "contents: read" in build_test
    assert "packages: write" not in build_test
    for forbidden in ("GHCR_TOKEN", "docker login", "docker push", "DOCKER_CONFIG"):
        assert forbidden not in build_test
    assert "scripts/role_image_worker_monitor.py" in build_test
    assert "scripts/build_role_images_and_smoke.sh" in build_test
    assert "--build-deadline-seconds 2550" in build_test
    assert "Retain bounded raw worker observations" in build_test
    assert "capacity-observations.json" in build_test
    assert "if: ${{ always() }}" in build_test
    assert "if-no-files-found: warn" in build_test
    assert "scripts/role_image_runner_disk.py" not in build_test


def test_publish_mode_keeps_fixed_repositories_after_smokes() -> None:
    workflow = _workflow_text()
    publish = _job(workflow, "publish-fixed-role-images")

    assert "needs: validate-dispatch" in publish
    assert "if: ${{ inputs.execution_mode == 'PUBLISH_FIXED_ROLE_IMAGES' }}" in publish
    assert "runs-on: [self-hosted, Linux, X64, inferdrome-role-image-cpu]" in publish
    assert "timeout-minutes: 45" in publish
    assert "contents: read" in publish
    assert "packages: write" in publish
    assert "GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in publish
    assert "--password-stdin" in publish
    build = BUILD_AND_SMOKE.read_text(encoding="utf-8")
    assert "ghcr.io/jayeshsuyal/inferdrome-private-engine" in build
    assert "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer" in build
    assert "validate-digest-pair" in publish
    assert "Retain the canonical paired-digest record" in publish
    assert "Retain bounded raw worker observations" in publish
    assert "capacity-observations.json" in publish
    assert publish.index("scripts/build_role_images_and_smoke.sh") < publish.index(
        'docker push "$tag"'
    )
    assert publish.index("docker login ghcr.io") < publish.index('docker push "$tag"')


def test_worker_jobs_use_pinned_actions_and_pass_required_observation_inputs() -> None:
    workflow = _workflow_text()

    for action in (
        "actions/checkout",
        "actions/setup-python",
        "actions/upload-artifact",
    ):
        assert re.search(rf"uses: {re.escape(action)}@[0-9a-f]{{40}}", workflow)
    assert workflow.count("persist-credentials: false") == 3
    assert workflow.count("RUNNER_ENVIRONMENT: ${{ runner.environment }}") == 2
    assert workflow.count("RUNNER_NAME: ${{ runner.name }}") == 2
    assert workflow.count("--expected-runner-name \"$EXPECTED_RUNNER_NAME\"") == 2
    assert workflow.count("--workspace \"$GITHUB_WORKSPACE\"") == 2
    assert "docker system prune" not in workflow
    assert "docker builder prune" not in workflow
    assert "rm -rf" not in workflow


def test_shared_build_helper_separates_smokes_and_publication() -> None:
    build = BUILD_AND_SMOKE.read_text(encoding="utf-8")
    monitor = WORKER_MONITOR.read_text(encoding="utf-8")

    assert "docker login" not in build
    assert "docker push" not in build
    assert "for role in private-engine cpu-runner-observer; do" in build
    assert "--flavor release" in build
    assert "--platform linux/amd64" in build
    assert "--network none" in build
    assert "--read-only" in build
    assert "--user 2000:0" in build
    assert "--cap-drop=ALL" in build
    assert "/opt/inferdrome-runtime/bin/inferdrome" in build
    assert "observed_vllm_version" in build
    assert "gcp_private_engine_adapter" in build
    assert "gcp_private_runner_adapter" in build
    assert "_MAX_DURING_SAMPLES = 180" in monitor
    assert "build_deadline_seconds: int = 2_550" in monitor
    assert "prunes Docker storage" in monitor
    assert "_terminate_and_wait" in monitor


def test_worker_runbook_states_limits_without_claiming_a_worker() -> None:
    publication = re.sub(
        r"\s+", " ", PUBLICATION_RUNBOOK.read_text(encoding="utf-8")
    )
    checklist = re.sub(r"\s+", " ", PRE_GPU_CHECKLIST.read_text(encoding="utf-8"))

    assert "no worker or publication asserted" in publication
    assert (
        "no current manual dispatch, worker registration, vm, running role image"
        in publication.lower()
    )
    assert "BUILD_AND_SMOKE_ONLY" in publication
    assert "PUBLISH_FIXED_ROLE_IMAGES" in publication
    assert "The same selected worker builds" in publication
    assert "does not invoke the GitHub-hosted Android SDK helper" in publication
    assert "provider-native maximum-runtime action" in publication
    assert (
        "Runner deregistration and workflow timeout are not VM deletion"
        in publication
    )
    assert "suggested starting configuration" in publication
    assert "do not establish a build-fit threshold" in checklist
    assert "does not remove an SDK, prune Docker" in checklist
