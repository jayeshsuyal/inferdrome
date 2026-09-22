"""Pure local checks for the fixed manual role-image publication contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.role_image_publish as publication

_COMMIT = "a" * 40
_VERSION = "0.3.0.dev0"
_RUN_ID = "12345"


def _plan(role: str) -> dict[str, object]:
    return publication.plan_role_image(
        role=role,
        source_commit=_COMMIT,
        version=_VERSION,
        workflow_run_id=_RUN_ID,
    )


def _image(plan: dict[str, object], digest_character: str) -> str:
    return f"{plan['repository']}@sha256:{digest_character * 64}"


def test_fixed_role_plans_are_distinct_and_reuse_the_verified_build_wrapper() -> None:
    engine = _plan("private-engine")
    runner = _plan("cpu-runner-observer")

    assert engine["repository"] == "ghcr.io/jayeshsuyal/inferdrome-private-engine"
    assert runner["repository"] == "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer"
    assert engine["transient_tag"] != runner["transient_tag"]
    assert engine["build"] == {
        "script": "scripts/build_runner_image.py",
        "flavor": "release",
        "image_kind": "vllm-benchmark-runner",
        "runtime_role": "private-engine",
        "platform": "linux/amd64",
        "pull": False,
    }
    assert runner["build"] == {**engine["build"], "runtime_role": "cpu-runner-observer"}
    assert publication.validate_plan(engine) == engine
    assert publication.validate_plan(runner) == runner
    assert engine["required_labels"]["com.inferdrome.vast-startup-profile"] == (
        "vast-ssh-public-v1"
    )
    assert engine["required_labels"]["com.inferdrome.vast-ssh-readiness-seconds"] == (
        "180"
    )
    assert runner["required_labels"]["com.inferdrome.vast-startup-profile"] == (
        "not-applicable"
    )
    assert runner["required_labels"]["com.inferdrome.public-pull-contract"] == (
        "private-visibility-unchanged"
    )


def test_public_vast_plan_can_publish_only_the_engine_role() -> None:
    plan = publication.plan_public_vast_engine(
        source_commit=_COMMIT,
        version=_VERSION,
        workflow_run_id=_RUN_ID,
    )
    assert plan["publication_scope"] == "PRIVATE_ENGINE_ONLY"
    assert plan["visibility"] == "PUBLIC_ANONYMOUS_PULL_REQUIRED"
    assert plan["engine_plan"] == _plan("private-engine")
    assert plan["post_build_smoke"] == "scripts/verify_vast_startup_image.py"
    assert plan["forbidden_visibility_change"] == (
        "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("role", "other-role"),
        ("source_commit", "A" * 40),
        ("source_commit", "a" * 39),
        ("version", "version-with-secret-shaped-value"),
        ("workflow_run_id", "0"),
        ("workflow_run_id", "123/unsafe"),
    ),
)
def test_planner_rejects_unbounded_dispatch_identity(field: str, value: str) -> None:
    submitted = {
        "role": "private-engine",
        "source_commit": _COMMIT,
        "version": _VERSION,
        "workflow_run_id": _RUN_ID,
    }
    submitted[field] = value

    with pytest.raises(publication.RoleImagePublicationError) as error:
        publication.plan_role_image(**submitted)

    assert value not in str(error.value)


def test_plan_tamper_and_local_inspection_mismatch_fail_closed() -> None:
    plan = _plan("private-engine")
    altered = {**plan, "repository": "ghcr.io/unapproved/other"}
    with pytest.raises(publication.RoleImagePublicationError):
        publication.validate_plan(altered)

    labels = dict(plan["required_labels"])
    assert (
        publication.validate_local_inspection(
            plan, labels=labels, platform="linux/amd64", user="2000:0"
        )["verified"]
        is True
    )
    for malformed_labels, platform, user in (
        (
            {**labels, "org.opencontainers.image.version": "0.0.0"},
            "linux/amd64",
            "2000:0",
        ),
        ({**labels, "unexpected": "value"}, "linux/amd64", "2000:0"),
        (labels, "linux/arm64", "2000:0"),
        (labels, "linux/amd64", "root"),
    ):
        with pytest.raises(publication.RoleImagePublicationError):
            publication.validate_local_inspection(
                plan,
                labels=malformed_labels,
                platform=platform,
                user=user,
            )


def test_registry_digest_binding_requires_the_fixed_repository_and_digest() -> None:
    plan = _plan("private-engine")
    receipt = publication.record_registry_digest(
        plan, immutable_image=_image(plan, "b")
    )
    assert receipt["validation_scope"] == "LOCAL_FORMAT_AND_ROLE_BINDING_ONLY"
    assert receipt["immutable_image"] == _image(plan, "b")
    for malformed in (
        "sha256:" + "a" * 64,
        "ghcr.io/jayeshsuyal/inferdrome-private-engine:mutable-tag",
        "ghcr.io/jayeshsuyal/other@sha256:" + "c" * 64,
        _image(plan, "B"),
        _image(plan, "c") + "-suffix",
    ):
        with pytest.raises(publication.RoleImagePublicationError) as error:
            publication.record_registry_digest(plan, immutable_image=malformed)
        assert malformed not in str(error.value)


def test_digest_pair_requires_two_distinct_role_content_digests() -> None:
    engine_plan = _plan("private-engine")
    runner_plan = _plan("cpu-runner-observer")
    engine = publication.record_registry_digest(
        engine_plan, immutable_image=_image(engine_plan, "d")
    )
    runner = publication.record_registry_digest(
        runner_plan, immutable_image=_image(runner_plan, "e")
    )
    pair = publication.validate_publication_pair(engine, runner)
    assert pair["private_engine_image"] == _image(engine_plan, "d")
    assert pair["cpu_runner_observer_image"] == _image(runner_plan, "e")

    collapsed = publication.record_registry_digest(
        runner_plan, immutable_image=_image(runner_plan, "d")
    )
    with pytest.raises(publication.RoleImagePublicationError, match="content digests"):
        publication.validate_publication_pair(engine, collapsed)


def test_digest_pair_rejects_a_bare_digest_with_a_controlled_error() -> None:
    engine_plan = _plan("private-engine")
    runner_plan = _plan("cpu-runner-observer")
    engine = publication.record_registry_digest(
        engine_plan, immutable_image=_image(engine_plan, "d")
    )
    runner = publication.record_registry_digest(
        runner_plan, immutable_image=_image(runner_plan, "e")
    )
    malformed_engine = {**engine, "immutable_image": "sha256:" + "d" * 64}

    with pytest.raises(publication.RoleImagePublicationError):
        publication.validate_publication_pair(malformed_engine, runner)


def test_cli_is_canonical_create_no_replace_and_never_needs_docker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "plan.json"
    arguments = [
        "plan",
        "--role",
        "private-engine",
        "--source-commit",
        _COMMIT,
        "--version",
        _VERSION,
        "--workflow-run-id",
        _RUN_ID,
        "--output",
        str(output),
    ]
    assert publication.main(arguments) == 0
    assert json.loads(output.read_bytes()) == _plan("private-engine")
    assert capsys.readouterr().out == ""
    assert publication.main(arguments) == 2
    assert "cannot be created" in capsys.readouterr().err
