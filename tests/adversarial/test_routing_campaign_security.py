"""Adversarial integrity and isolation checks for routing-campaign-v1."""

import ast
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
import rfc8785

import inferdrome.routing_campaign.engine as routing_engine
import inferdrome.routing_campaign.package as routing_package
from inferdrome.errors import InferdromeError, VerificationError
from inferdrome.routing_campaign import run_campaign, verify_campaign
from inferdrome.routing_campaign.engine import execute_campaign
from inferdrome.routing_campaign.package import load_campaign_inputs

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ROOT = REPOSITORY_ROOT / "campaigns" / "routing-campaign-v1"
PLAN_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.plan.json"
TRACE_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.trace.jsonl"
FAULT_SCHEDULE_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.fault-schedule.json"
TRIAL_PLAN_PATH = CAMPAIGN_ROOT / "trial-plan.json"


def _run_campaign(tmp_path: Path) -> tuple[Path, str]:
    sealed = run_campaign(
        plan_path=PLAN_PATH,
        trace_path=TRACE_PATH,
        fault_schedule_path=FAULT_SCHEDULE_PATH,
        trial_plan_path=TRIAL_PLAN_PATH,
        output_root=tmp_path / "output",
    )
    assert sealed.path.is_dir()
    assert sealed.retained_digest.startswith("sha256:")
    verify_campaign(
        sealed.path,
        expected_digest=sealed.retained_digest,
        require_immutable=True,
    )
    return sealed.path, sealed.retained_digest


def _make_tree_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _mutable_copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    _make_tree_writable(destination)
    return destination


def _rehash_manifest_entry(package: Path, relative_path: str) -> None:
    """Preserve manifest internal consistency to isolate verifier checks."""

    manifest_path = package / "integrity" / "artifact-hashes.json"
    manifest = json.loads(manifest_path.read_bytes())
    content = (package / relative_path).read_bytes()
    for entry in manifest["entries"]:
        if entry["path"] == relative_path:
            entry["size_bytes"] = len(content)
            entry["sha256"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
            break
    else:
        raise AssertionError(f"manifest does not declare {relative_path}")
    manifest_path.write_bytes(rfc8785.dumps(manifest))


def _assert_rejected(
    package: Path,
    *,
    expected_digest: str | None = None,
    require_immutable: bool = False,
) -> None:
    with pytest.raises((InferdromeError, ValueError)):
        verify_campaign(
            package,
            expected_digest=expected_digest,
            require_immutable=require_immutable,
        )


def test_verifier_rejects_missing_and_undeclared_package_paths(
    tmp_path: Path,
) -> None:
    package, digest = _run_campaign(tmp_path)

    missing = _mutable_copy(package, tmp_path / "missing")
    (missing / "campaign-plan.json").unlink()
    _assert_rejected(missing, expected_digest=digest)

    injected = _mutable_copy(package, tmp_path / "injected")
    (injected / "extra.json").write_text("{}", encoding="utf-8")
    _assert_rejected(injected, expected_digest=digest)


def test_verifier_rejects_symlink_and_hardlink_before_artifact_use(
    tmp_path: Path,
) -> None:
    package, digest = _run_campaign(tmp_path)

    symlinked = _mutable_copy(package, tmp_path / "symlinked")
    plan = symlinked / "campaign-plan.json"
    plan.unlink()
    plan.symlink_to("fault-schedule.json")
    _assert_rejected(symlinked, expected_digest=digest)

    hardlinked = _mutable_copy(package, tmp_path / "hardlinked")
    plan = hardlinked / "campaign-plan.json"
    plan.unlink()
    os.link(hardlinked / "fault-schedule.json", plan)
    _assert_rejected(hardlinked, expected_digest=digest)


def test_verifier_rejects_writable_package_tree(tmp_path: Path) -> None:
    package, digest = _run_campaign(tmp_path)
    writable = _mutable_copy(package, tmp_path / "writable")

    assert stat.S_IMODE(os.lstat(writable).st_mode) & 0o222
    _assert_rejected(
        writable,
        expected_digest=digest,
        require_immutable=True,
    )


def test_verifier_rejects_noncanonical_json_after_manifest_rehash(
    tmp_path: Path,
) -> None:
    package, _ = _run_campaign(tmp_path)
    mutated = _mutable_copy(package, tmp_path / "noncanonical")
    plan = mutated / "campaign-plan.json"
    original = plan.read_bytes()
    assert original.startswith(b"{")
    plan.write_bytes(b"{\n" + original[1:])
    _rehash_manifest_entry(mutated, "campaign-plan.json")

    _assert_rejected(mutated)


def test_verifier_rejects_duplicate_json_key_after_manifest_rehash(
    tmp_path: Path,
) -> None:
    package, _ = _run_campaign(tmp_path)
    mutated = _mutable_copy(package, tmp_path / "duplicate-key")
    plan = mutated / "campaign-plan.json"
    original = plan.read_bytes()
    parsed = json.loads(original)
    first_key = next(iter(parsed))
    duplicate = b'{"' + first_key.encode("utf-8") + b'":null,' + original[1:]
    plan.write_bytes(duplicate)
    _rehash_manifest_entry(mutated, "campaign-plan.json")

    _assert_rejected(mutated)


def test_retained_digest_anchor_rejects_valid_package_with_tampered_digest(
    tmp_path: Path,
) -> None:
    package, digest = _run_campaign(tmp_path)
    assert digest != f"sha256:{'0' * 64}"

    _assert_rejected(
        package,
        expected_digest=f"sha256:{'0' * 64}",
        require_immutable=True,
    )


def test_verifier_replays_without_the_producer_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification must not call the writer's engine implementation."""

    package, digest = _run_campaign(tmp_path)

    def producer_must_not_run(*_: object, **__: object) -> object:
        raise AssertionError("the producer path must not run during verification")

    monkeypatch.setattr(routing_engine, "execute_campaign", producer_must_not_run)
    monkeypatch.setattr(routing_package, "execute_campaign", producer_must_not_run)

    report = verify_campaign(
        package,
        expected_digest=digest,
        require_immutable=True,
    )
    assert report.retained_digest == digest
    assert report.planned_request_count == 18


@pytest.mark.parametrize(
    ("relative_path", "field", "replacement"),
    [
        (
            "trials/trial-typed-v1/route-decisions.jsonl",
            "claims_discarded",
            [],
        ),
        (
            "trials/trial-typed-v1/terminal-outcomes.jsonl",
            "reason",
            "ALTERED_SEMANTIC_RECEIPT",
        ),
    ],
)
def test_independent_verifier_rejects_rehashed_semantic_receipt_tampering(
    tmp_path: Path,
    relative_path: str,
    field: str,
    replacement: object,
) -> None:
    """Schema-valid receipt changes must fail independent replay, not just hashes."""

    package, _ = _run_campaign(tmp_path)
    mutated = _mutable_copy(package, tmp_path / "semantic-tampering")
    artifact_path = mutated / relative_path
    rows = artifact_path.read_bytes().splitlines()
    first_row = json.loads(rows[0])
    first_row[field] = replacement
    rows[0] = rfc8785.dumps(first_row)
    artifact_path.write_bytes(b"\n".join(rows) + b"\n")
    _rehash_manifest_entry(mutated, relative_path)

    with pytest.raises(VerificationError, match="replay"):
        verify_campaign(mutated, require_immutable=False)


def test_verifier_rejects_same_path_replacement_after_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verifier binds the first scan to the same filesystem objects at exit."""

    package, _ = _run_campaign(tmp_path)
    mutable = _mutable_copy(package, tmp_path / "same-path-replacement")
    original_read = routing_package._read_scanned_file
    replacement_made = False

    def replace_after_read(scanned: routing_package._ScannedFile) -> bytes:
        nonlocal replacement_made
        content = original_read(scanned)
        if not replacement_made and scanned.relative_path == "campaign-plan.json":
            target = scanned.path
            replacement = target.with_name(".campaign-plan-replacement")
            replacement.write_bytes(content)
            replacement.replace(target)
            replacement_made = True
        return content

    monkeypatch.setattr(routing_package, "_read_scanned_file", replace_after_read)

    with pytest.raises(VerificationError, match="changed during verification"):
        verify_campaign(mutable, require_immutable=False)
    assert replacement_made


def _frozen_inputs():
    return load_campaign_inputs(
        PLAN_PATH,
        TRACE_PATH,
        FAULT_SCHEDULE_PATH,
        TRIAL_PLAN_PATH,
    )


def test_engine_consumes_declared_load_updates_and_fault_pause() -> None:
    """An in-memory perturbation must move the stale-load boundary.

    The campaign's persisted contracts remain closed and fixed.  ``model_copy``
    deliberately supplies an impossible-on-disk variation so this test detects
    an engine that merely hard-codes the fixture's 10-ms last update or 15-ms
    fault pause instead of consuming its typed inputs.
    """

    plan, trace, fault_schedule, trial_plan = _frozen_inputs()
    extended_load_updates = plan.load_observer.model_copy(
        update={"update_times_ms": (0, 10, 20, 30, 40, 50)}
    )
    delayed_fault = fault_schedule.model_copy(update={"load_observer_pause_at_ms": 35})
    altered_plan = plan.model_copy(update={"load_observer": extended_load_updates})

    execution = execute_campaign(altered_plan, trace, delayed_fault, trial_plan)
    fail_closed = next(
        trial
        for trial in execution.trials
        if trial.summary.policy_id == "fail_closed_required_load_v1"
    )

    assert [
        decision.candidates[0].load.observed_at_ms for decision in fail_closed.decisions
    ] == [0, 10, 20, 30, 30, 30]
    assert [
        decision.candidates[0].load.admissibility for decision in fail_closed.decisions
    ] == [
        "ADMISSIBLE",
        "ADMISSIBLE",
        "ADMISSIBLE",
        "ADMISSIBLE",
        "INADMISSIBLE",
        "INADMISSIBLE",
    ]
    assert [decision.selected_endpoint_id for decision in fail_closed.decisions] == [
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
        None,
        None,
    ]
    assert all(
        decision.candidates[0].health.admissibility == "ADMISSIBLE"
        for decision in fail_closed.decisions
    )
    assert [
        decision.candidates[0].health.epoch for decision in fail_closed.decisions
    ] == [1, 3, 5, 7, 9, 11]


def test_engine_rejects_fault_schedule_without_independent_health() -> None:
    """The frozen ``health_continues`` declaration is an execution input."""

    plan, trace, fault_schedule, trial_plan = _frozen_inputs()
    missing_health = fault_schedule.model_copy(update={"health_continues": False})

    with pytest.raises(ValueError, match="independent health observer"):
        execute_campaign(plan, trace, missing_health, trial_plan)


def test_engine_consumes_declared_endpoint_b_saturation_time() -> None:
    """A delayed mock saturation must delay fail-open terminal timeouts."""

    plan, trace, fault_schedule, trial_plan = _frozen_inputs()
    delayed_behavior = plan.endpoint_behavior.model_copy(
        update={"endpoint_b_saturated_at_ms": 40}
    )
    altered_plan = plan.model_copy(update={"endpoint_behavior": delayed_behavior})

    execution = execute_campaign(altered_plan, trace, fault_schedule, trial_plan)
    fail_open = next(
        trial
        for trial in execution.trials
        if trial.summary.policy_id == "explicit_fail_open_stale_load_v1"
    )

    assert [decision.selected_endpoint_id for decision in fail_open.decisions] == [
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
        "endpoint-b",
    ]
    assert [terminal.status for terminal in fail_open.terminals] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "TIMED_OUT",
        "TIMED_OUT",
    ]
    assert [terminal.reason for terminal in fail_open.terminals] == [
        "NONE",
        "NONE",
        "NONE",
        "NONE",
        "SIMULATED_ENDPOINT_B_SATURATED",
        "SIMULATED_ENDPOINT_B_SATURATED",
    ]


def test_routing_campaign_source_has_no_prohibited_runtime_imports() -> None:
    source_root = REPOSITORY_ROOT / "src" / "inferdrome" / "routing_campaign"
    source_paths = sorted(source_root.rglob("*.py"))
    assert source_paths, "routing-campaign source package is required"
    prohibited_roots = {
        "aiohttp",
        "auth",
        "azure",
        "boto",
        "credential",
        "credentials",
        "cuda",
        "dcgm",
        "deployment",
        "docker",
        "fabric",
        "gcp",
        "google",
        "gpu",
        "grpc",
        "http",
        "httpx",
        "kubernetes",
        "lambda",
        "network",
        "nvml",
        "paramiko",
        "requests",
        "socket",
        "ssh",
        "subprocess",
        "urllib",
    }
    violations: list[str] = []

    for source_path in source_paths:
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        for node in ast.walk(tree):
            names: tuple[str, ...]
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = (module, *(alias.name for alias in node.names))
            else:
                continue
            for name in names:
                components = name.replace("-", "_").split(".")
                if any(component in prohibited_roots for component in components):
                    violations.append(
                        f"{source_path.relative_to(REPOSITORY_ROOT)}: {name}"
                    )

    assert not violations, (
        "routing-campaign imports prohibited runtime surface:\n" + "\n".join(violations)
    )
