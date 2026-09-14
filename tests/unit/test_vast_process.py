"""Local-only Vast adapter tests: fake children, no Docker, GPU or provider."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment import vast_process as prep
from inferdrome.deployment import vast_process_observer as observer
from inferdrome.deployment import vast_process_runtime as runtime
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.executor import declared_input_transfer_digest
from inferdrome.routing_execution.package import (
    VerificationError,
    verify_execution_package,
)
from tests.unit.test_vast_execution import seal_vast_fixture


def process_input_value(root: Path) -> dict[str, Any]:
    """Synthetic IDs and paths; these are not provider or GPU observations."""
    value = prep.template()
    image = {"reference": "example.invalid/synthetic-vast@" + sha256_digest(b"image")}
    value.update(
        source_commit="a" * 40,
        instance_id=101,
        offer_id=202,
        container_image=image,
        gpu_uuids=[
            "GPU-00000000-0000-0000-0000-000000000001",
            "GPU-00000000-0000-0000-0000-000000000002",
        ],
        module_sha256=prep.module_digests(),
        request_timeout_ms=1000,
        readiness_timeout_seconds=10,
        campaign_timeout_seconds=20,
    )
    for key, name in (
        ("model_path", "model"),
        ("preparation_path", "preparation"),
        ("evidence_path", "evidence"),
        ("cache_path", "cache"),
    ):
        value[key] = str(root / name)
    value["launch_readback"].update(instance_id=101, requested_image=image)
    value["cleanup"].update(
        instance_id=101,
        accountable_operator="synthetic-operator",
        terminate_by_utc="2099-01-01T00:00:00Z",
    )
    return value


def process_spec(root: Path) -> prep.VastProcessInput:
    return prep.VastProcessInput.model_validate_json(
        canonical_json_bytes(process_input_value(root))
    )


def test_preparation_is_canonical_private_bound_and_create_only(tmp_path: Path) -> None:
    spec = process_spec(tmp_path)
    output = Path(spec.preparation_path)
    digest = prep.prepare(spec, output)
    assert digest == sha256_digest(canonical_json_bytes(spec.model_dump(mode="json")))
    assert prep.load_plan(output, digest) == spec
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "plan.json").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        prep.prepare(spec, output)
    with pytest.raises(ValueError, match="digest differs"):
        prep.load_plan(output, "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="path must match"):
        prep.prepare(spec, tmp_path / "wrong-path")


def test_template_never_supplies_usable_authority() -> None:
    with pytest.raises(ValidationError):
        prep.VastProcessInput.model_validate_json(canonical_json_bytes(prep.template()))


@pytest.mark.parametrize(
    ("section", "key", "replacement"),
    [
        (None, "instance_id", True),
        (None, "offer_id", 0),
        (None, "uid", 0),
        (None, "gid", 2000),
        (None, "provider", "LAMBDA"),
        (None, "model_path", "/workspace/../model"),
        (None, "cache_path", "/workspace//cache"),
        (None, "module_sha256", {}),
        (None, "provider_payload", {"secret": "not-accepted"}),
        ("cleanup", "instance_id", 102),
        ("cleanup", "terminate_by_utc", "2099-1-1T00:00:00Z"),
        ("cleanup", "state", "VERIFIED_DESTROYED"),
        ("launch_readback", "instance_id", 102),
        ("launch_readback", "launch_mode", "ssh"),
        ("launch_readback", "public_port_mappings", [8000]),
        ("launch_readback", "persistent_volume_ids", [123]),
        ("launch_readback", "resolved_image_digest", "sha256:" + "1" * 64),
    ],
)
def test_invalid_plan_facts_fail_closed(
    tmp_path: Path, section: str | None, key: str, replacement: Any
) -> None:
    value = process_input_value(tmp_path)
    target = value if section is None else value[section]
    target[key] = replacement
    with pytest.raises(ValidationError):
        prep.VastProcessInput.model_validate_json(canonical_json_bytes(value))


@pytest.mark.parametrize("mutation", ["same-gpu", "path-overlap", "wrong-image"])
def test_plan_cross_field_bindings_reject_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    value = process_input_value(tmp_path)
    if mutation == "same-gpu":
        value["gpu_uuids"][1] = value["gpu_uuids"][0]
    elif mutation == "path-overlap":
        value["cache_path"] = value["model_path"] + "/cache"
    else:
        value["launch_readback"]["requested_image"] = {
            "reference": "example.invalid/wrong@" + sha256_digest(b"wrong")
        }
    with pytest.raises(ValidationError):
        prep.VastProcessInput.model_validate_json(canonical_json_bytes(value))


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_private_input_rejects_link_aliases(tmp_path: Path, link_kind: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"private")
    alias = tmp_path / "alias"
    if link_kind == "symlink":
        alias.symlink_to(source)
    else:
        os.link(source, alias)
    with pytest.raises((OSError, ValueError)):
        prep.read_private(alias)


def test_runtime_config_binds_observation_without_claiming_oci_attestation(
    tmp_path: Path,
) -> None:
    spec = process_spec(tmp_path)
    observation = {"synthetic": True, "engine_process_ids": [31, 32]}
    config = prep.routing_config(spec, observation)
    assert config.source_commit == spec.source_commit
    assert config.container_image == spec.container_image
    assert config.artifact_provenance.runtime_observation_sha256 == sha256_digest(
        canonical_json_bytes(observation)
    )
    assert config.artifact_provenance.container_image_assertion == (
        "OPERATOR_DECLARED_NOT_OBSERVED"
    )
    assert config.evidence_destination.declared_input_transfer_sha256 == (
        declared_input_transfer_digest(config)
    )
    assert [endpoint.origin for endpoint in config.endpoints] == [
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001",
    ]


def test_closed_child_environment_and_two_engine_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = process_spec(tmp_path)
    for key in ("HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "HTTPS_PROXY", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "never-inherit")
    observer = runtime.child_environment(spec, gpu_uuid=None)
    assert observer["CUDA_VISIBLE_DEVICES"] == ""
    assert observer["NVIDIA_VISIBLE_DEVICES"] == "void"
    assert observer["HF_HUB_OFFLINE"] == observer["TRANSFORMERS_OFFLINE"] == "1"
    assert "never-inherit" not in observer.values()
    assert (
        not {"HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "HTTPS_PROXY", "SSH_AUTH_SOCK"}
        & observer.keys()
    )
    for index, gpu in enumerate(spec.gpu_uuids):
        env = runtime.child_environment(spec, gpu_uuid=gpu)
        argv = runtime.engine_argv(spec, index, "/serving/bin/python")
        assert env["CUDA_VISIBLE_DEVICES"] == gpu
        assert argv[:4] == ("/serving/bin/python", "-I", "/usr/local/bin/vllm", "serve")
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == str(8000 + index)
        assert argv[argv.index("--tensor-parallel-size") + 1] == "1"
        assert argv[argv.index("--max-model-len") + 1] == "2048"
        assert argv[argv.index("--dtype") + 1] == "bfloat16"
        assert "--no-enable-log-requests" in argv
    with pytest.raises(runtime.RuntimeFailure):
        runtime.engine_argv(spec, 2, "/serving/bin/python")


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeChild:
    pid: int
    clock: FakeClock
    finish_at: float | None = None
    status: int = 0
    poll_advance: float = 0.0

    def poll(self) -> int | None:
        self.clock.now += self.poll_advance
        self.poll_advance = 0.0
        return (
            self.status
            if self.finish_at is not None and self.clock.now >= self.finish_at
            else None
        )

    def wait(self, timeout: float | None = None) -> int:
        result = self.poll()
        if result is None:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        return result


class SupervisorHarness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.children: list[FakeChild] = []
        self.stopped: list[int] = []
        self.observer_status = 0
        self.observer_finishes = True
        self.observer_poll_advance = 0.0
        self.engine_death: float | None = None
        self.fail_spawn: int | None = None
        self.first_spawn_delay = 0.0
        self.cleanup_error: type[Exception] | None = None
        self.observer_called = 0

    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        del env
        if len(self.children) == self.fail_spawn:
            raise OSError("synthetic spawn failure")
        if not self.children:
            self.clock.now += self.first_spawn_delay
        is_observer = argv[0] == "observer"
        child = FakeChild(100 + len(self.children), self.clock)
        if is_observer:
            child.finish_at = self.clock.now + 0.2 if self.observer_finishes else None
            child.status = self.observer_status
            child.poll_advance = self.observer_poll_advance
        elif not self.children:
            child.finish_at = self.engine_death
            child.status = 9
        self.children.append(child)
        return child

    def stop(self, child: runtime.Child) -> None:
        self.stopped.append(child.pid)
        if self.cleanup_error is not None and child.pid == 101:
            raise self.cleanup_error("synthetic cleanup failure")

    def observer(
        self, children: Sequence[runtime.Child]
    ) -> tuple[Sequence[str], Mapping[str, str]]:
        assert len(children) == 2
        self.observer_called += 1
        return ("observer",), {}

    def run(
        self, ready: Callable[[float], bool] = lambda seconds: True, **limits: float
    ) -> None:
        runtime.supervise(
            [(("engine-a",), {}), (("engine-b",), {})],
            self.observer,
            ready,
            readiness_seconds=limits.get("readiness", 1.0),
            campaign_seconds=limits.get("campaign", 1.0),
            total_seconds=limits.get("total", 5.0),
            spawn_child=self.spawn,
            stop=self.stop,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )


def test_supervisor_happy_path_stops_every_owned_group_once() -> None:
    harness = SupervisorHarness()
    harness.run()
    assert len(harness.children) == 3 and harness.observer_called == 1
    assert harness.stopped == [102, 101, 100]


@pytest.mark.parametrize("total", [0.3, 5.0])
def test_supervisor_readiness_deadline_never_launches_observer(total: float) -> None:
    harness = SupervisorHarness()
    with pytest.raises(runtime.RuntimeFailure, match="READINESS_TIMEOUT"):
        harness.run(lambda seconds: False, total=total)
    assert harness.observer_called == 0 and harness.stopped == [101, 100]
    assert harness.clock.now <= min(total, 1.0)


def test_supervisor_rejects_readiness_returning_after_its_cutoff() -> None:
    harness = SupervisorHarness()

    def late_ready(seconds: float) -> bool:
        assert seconds <= 1.0
        harness.clock.now += 1.1
        return True

    with pytest.raises(runtime.RuntimeFailure, match="READINESS_TIMEOUT"):
        harness.run(late_ready)
    assert harness.observer_called == 0 and harness.stopped == [101, 100]


@pytest.mark.parametrize("phase", ["readiness", "capture"])
def test_supervisor_engine_death_cleans_up_without_restart(phase: str) -> None:
    harness = SupervisorHarness()
    harness.engine_death = 0.0 if phase == "readiness" else 0.1
    with pytest.raises(runtime.RuntimeFailure, match="ENGINE_DIED"):
        harness.run()
    assert len(harness.children) == (2 if phase == "readiness" else 3)
    assert harness.stopped == [child.pid for child in reversed(harness.children)]


def test_supervisor_observer_failure_does_not_retry() -> None:
    harness = SupervisorHarness()
    harness.observer_status = 7
    with pytest.raises(runtime.RuntimeFailure, match="OBSERVER_FAILED"):
        harness.run()
    assert harness.observer_called == 1 and len(harness.children) == 3
    assert harness.stopped == [102, 101, 100]


@pytest.mark.parametrize("late_completion", [False, True])
def test_supervisor_capture_deadline_rejects_running_or_late_success(
    late_completion: bool,
) -> None:
    harness = SupervisorHarness()
    harness.observer_finishes = late_completion
    harness.observer_poll_advance = 1.1 if late_completion else 0.0
    with pytest.raises(runtime.RuntimeFailure, match="CAMPAIGN_TIMEOUT"):
        harness.run()
    assert harness.stopped == [102, 101, 100]


def test_supervisor_spawn_failure_stops_only_previously_owned_child() -> None:
    harness = SupervisorHarness()
    harness.fail_spawn = 1
    with pytest.raises(OSError):
        harness.run()
    assert len(harness.children) == 1 and harness.stopped == [100]


def test_supervisor_delayed_first_spawn_does_not_start_second_engine() -> None:
    harness = SupervisorHarness()
    harness.first_spawn_delay = 6.0
    with pytest.raises(runtime.RuntimeFailure, match="VAST_RUN_DEADLINE"):
        harness.run(total=5.0)
    assert len(harness.children) == 1 and harness.stopped == [100]
    assert harness.observer_called == 0


@pytest.mark.parametrize("cleanup_error", [OSError, runtime.RuntimeFailure])
def test_supervisor_cleanup_failure_attempts_remaining_groups(
    cleanup_error: type[Exception],
) -> None:
    harness = SupervisorHarness()
    harness.cleanup_error = cleanup_error
    with pytest.raises(runtime.RuntimeFailure, match="PROCESS_CLEANUP_UNCONFIRMED"):
        harness.run()
    assert harness.stopped == [102, 101, 100]


def test_stop_child_kills_remaining_group_after_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(runtime.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    child = FakeChild(999, FakeClock(), finish_at=0)
    runtime.stop_child(child)
    assert calls == [(999, signal.SIGTERM), (999, signal.SIGKILL)]


def test_execute_rejects_authority_then_consumes_attempt_before_failed_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = process_spec(tmp_path)
    directory = Path(spec.preparation_path)
    digest = prep.prepare(spec, directory)
    calls = 0

    def failed_stage(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise runtime.RuntimeFailure("SYNTHETIC_PREFLIGHT_FAILED")

    monkeypatch.setattr(runtime, "_stage", failed_stage)
    for operator, accepted in (
        ("wrong", True),
        (spec.cleanup.accountable_operator, False),
    ):
        with pytest.raises(
            runtime.RuntimeFailure, match="EXACT_PLAN_APPROVAL_REQUIRED"
        ):
            runtime.execute(directory, digest, operator, accepted)
    assert calls == 0 and not (directory / "execution-attempt.json").exists()
    with pytest.raises(runtime.RuntimeFailure, match="SYNTHETIC_PREFLIGHT_FAILED"):
        runtime.execute(directory, digest, spec.cleanup.accountable_operator, True)
    with pytest.raises(FileExistsError):
        runtime.execute(directory, digest, spec.cleanup.accountable_operator, True)
    assert calls == 1


def gpu_output(spec: prep.VastProcessInput) -> str:
    return "\n".join(
        f"{gpu}, NVIDIA H100 80GB HBM3, 81559, Disabled, 580.65.06"
        for gpu in spec.gpu_uuids
    )


def test_gpu_observation_is_exact_and_sanitized(tmp_path: Path) -> None:
    spec = process_spec(tmp_path)
    rows = runtime.check_gpu_observation(spec, gpu_output(spec))
    assert [row["gpu_uuid_sha256"] for row in rows] == [
        sha256_digest(gpu.encode()) for gpu in spec.gpu_uuids
    ]
    assert "GPU-" not in json.dumps(rows)


@pytest.mark.parametrize(
    "mutation", ["name", "memory", "mig", "duplicate", "missing", "driver"]
)
def test_gpu_mismatch_fails_admission(tmp_path: Path, mutation: str) -> None:
    spec = process_spec(tmp_path)
    content = gpu_output(spec)
    if mutation == "name":
        content = content.replace("NVIDIA H100 80GB HBM3", "NVIDIA A100-PCIE-40GB")
    elif mutation == "memory":
        content = content.replace("81559", "80000")
    elif mutation == "mig":
        content = content.replace("Disabled", "Enabled")
    elif mutation == "duplicate":
        content = content.replace(spec.gpu_uuids[1], spec.gpu_uuids[0])
    elif mutation == "missing":
        content = content.splitlines()[0]
    else:
        content = content.replace("580.65.06", "private-driver-data", 1)
    with pytest.raises(runtime.RuntimeFailure):
        runtime.check_gpu_observation(spec, content)


def test_build_record_cannot_substitute_source_or_module_bytes(tmp_path: Path) -> None:
    spec = process_spec(tmp_path)
    marker = {"source_commit": spec.source_commit, "module_sha256": spec.module_sha256}
    runtime.verify_build(spec, marker)
    marker["source_commit"] = "b" * 40
    with pytest.raises(runtime.RuntimeFailure, match="INSTALLED_ARTIFACT_MISMATCH"):
        runtime.verify_build(spec, marker)


def test_export_copies_only_four_verified_records_and_preserves_digest(
    tmp_path: Path,
) -> None:
    package = seal_vast_fixture(tmp_path / "package")
    (tmp_path / "private-plan.json").write_text("private material never exported")
    digest = verify_execution_package(package).report.retained_digest
    output = tmp_path / "export"
    assert prep.export_package(package, output, digest) == digest
    assert (
        verify_execution_package(output, expected_digest=digest).report.retained_digest
        == digest
    )
    assert {path.name for path in output.iterdir()} == {
        path.name for path in package.iterdir()
    }
    assert len(list(output.iterdir())) == 4
    assert all(
        (output / path.name).read_bytes() == path.read_bytes()
        for path in package.iterdir()
    )
    with pytest.raises((OSError, ValueError)):
        prep.export_package(package, output, digest)


@pytest.mark.parametrize("tamper", [False, True])
def test_export_rejects_wrong_digest_or_changed_package(
    tmp_path: Path, tamper: bool
) -> None:
    package = seal_vast_fixture(tmp_path / "package")
    digest = verify_execution_package(package).report.retained_digest
    if tamper:
        target = package / "executed-manifest.json"
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b" ")
        target.chmod(0o400)
    else:
        digest = "sha256:" + "0" * 64
    output = tmp_path / "export"
    with pytest.raises(VerificationError):
        prep.export_package(package, output, digest)
    assert not output.exists()


def cleanup_value() -> dict[str, Any]:
    return {
        "provider": "VAST_AI",
        "instance_id": 101,
        "destroy_acknowledged": True,
        "destroy_requested_at_utc": "2026-09-13T10:00:00Z",
        "readback_at_utc": "2026-09-13T10:00:01Z",
        "query_instance_id": 101,
        "readback_succeeded": True,
        "matching_instance_ids": [],
        "pagination_exhausted": True,
        "persistent_volume_ids": [],
    }


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("instance_id", 102),
        ("query_instance_id", 102),
        ("destroy_acknowledged", False),
        ("readback_succeeded", False),
        ("matching_instance_ids", [101]),
        ("pagination_exhausted", False),
        ("persistent_volume_ids", [4]),
        ("readback_at_utc", "2026-09-13T09:59:59Z"),
        ("readback_at_utc", "not-a-time"),
    ],
)
def test_failed_or_ambiguous_cleanup_never_confirms_absence(
    key: str, replacement: Any
) -> None:
    value = cleanup_value()
    value[key] = replacement
    receipt = prep.DestroyReadback.model_validate_json(canonical_json_bytes(value))
    assert prep.cleanup_state(101, receipt) == "CLEANUP_UNCONFIRMED"


def test_cleanup_success_remains_operator_reported_not_provider_attested() -> None:
    assert prep.cleanup_state(101, None) == "CLEANUP_UNCONFIRMED"
    receipt = prep.DestroyReadback.model_validate_json(
        canonical_json_bytes(cleanup_value())
    )
    assert prep.cleanup_state(101, receipt) == (
        "OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED"
    )


def simulate_path_owner(
    spec: prep.VastProcessInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate target ownership on temporary local dirs without chown."""
    paths = {
        Path(value)
        for value in (
            spec.model_path,
            spec.preparation_path,
            spec.evidence_path,
            spec.cache_path,
        )
    }
    original_stat = Path.stat

    def owned_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        metadata = original_stat(path, *args, **kwargs)
        if path in paths:
            fields = list(metadata)
            fields[4] = spec.uid
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "stat", owned_stat)


def test_vast_path_contract_accepts_gid_zero_and_rejects_nonempty_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = process_spec(tmp_path)
    for value in (
        spec.model_path,
        spec.preparation_path,
        spec.evidence_path,
        spec.cache_path,
    ):
        Path(value).mkdir(mode=0o700)
    simulate_path_owner(spec, monkeypatch)
    assert spec.uid == 2000 and spec.gid == 0
    runtime.require_process_paths(spec)
    (Path(spec.evidence_path) / "existing").write_bytes(b"must not overwrite")
    with pytest.raises(runtime.RuntimeFailure, match="EVIDENCE_NOT_EMPTY"):
        runtime.require_process_paths(spec)


def test_fake_linux_preflight_verifies_model_before_gpu_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = process_spec(tmp_path)
    for value in (
        spec.model_path,
        spec.preparation_path,
        spec.evidence_path,
        spec.cache_path,
    ):
        Path(value).mkdir(mode=0o700)
    simulate_path_owner(spec, monkeypatch)
    marker = tmp_path / "build.json"
    marker.write_bytes(
        canonical_json_bytes(
            {
                "source_commit": spec.source_commit,
                "module_sha256": spec.module_sha256,
            }
        )
    )
    monkeypatch.setattr(runtime, "_BUILD_MARKER", marker)
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runtime.sys, "version_info", (3, 12))
    monkeypatch.setattr(runtime.os, "getuid", lambda: 2000)
    monkeypatch.setattr(runtime.os, "getgid", lambda: 0)
    events: list[str] = []

    def verify_model(path: str) -> None:
        assert path == spec.model_path
        events.append("model")

    def probe(
        actual_spec: prep.VastProcessInput, environment: Mapping[str, str]
    ) -> bytes:
        assert events == ["model"]
        assert actual_spec == spec
        assert environment == runtime.child_environment(spec, gpu_uuid=None)
        assert environment["CUDA_VISIBLE_DEVICES"] == ""
        events.append("gpu")
        return gpu_output(spec).encode()

    bound: list[tuple[str, int]] = []

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            bound.append(address)

    monkeypatch.setattr(runtime, "_require_model_snapshot", verify_model)
    monkeypatch.setattr(runtime, "_gpu_probe", probe)
    monkeypatch.setattr(runtime, "_vllm_python", lambda: "/serving/bin/python")
    monkeypatch.setattr(runtime, "observed_vllm_version", lambda **kwargs: "0.26.0")
    monkeypatch.setattr(runtime.socket, "socket", lambda *args: FakeSocket())
    result = runtime.preflight(spec)
    assert events == ["model", "gpu"]
    assert bound == [("127.0.0.1", 8000), ("127.0.0.1", 8001)]
    assert result["container_image_assertion"] == "OPERATOR_DECLARED_NOT_OBSERVED"
    assert result["gid"] == 0 and result["runtime_version"] == "0.26.0"
    assert "GPU-" not in json.dumps(result)


def test_readiness_subprocess_timeout_is_bounded_and_group_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float | None] = []
    stopped: list[int] = []

    class ProbeChild(FakeChild):
        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired("synthetic-probe", timeout)

    child = ProbeChild(999, FakeClock())
    monkeypatch.setattr(runtime, "spawn", lambda argv, env: child)
    monkeypatch.setattr(
        runtime, "stop_child", lambda candidate: stopped.append(candidate.pid)
    )
    assert runtime._ready({}, 0.25) is False
    assert waits == [0.25] and stopped == [999]


def observer_inputs(
    tmp_path: Path,
    *,
    expired: bool = False,
    profile: Literal["v1", "v2"] = "v1",
    tampered_module: str | None = None,
) -> tuple[prep.ProcessInput, Path, str]:
    value = process_input_value(tmp_path)
    if profile == "v2":
        value["schema_version"] = "inferdrome.vast-process-input.v2"
        value["module_sha256"] = prep.module_digests(profile="v2")
        value["launch_readback"].update(
            user="0:0",
            transfer_uid=2001,
            transfer_gid=0,
            public_port_mappings=[{
                "purpose": "SSH_MANAGEMENT",
                "container_port": 2222,
                "protocol": "tcp",
                "public_host": "1.1.1.1",
                "public_port": 2222,
            }],
        )
    if tampered_module is not None:
        value["module_sha256"][tampered_module] = sha256_digest(b"tampered-module")
    if expired:
        value["cleanup"]["terminate_by_utc"] = "2000-01-01T00:00:00Z"
    spec = prep.parse_process_input(canonical_json_bytes(value))
    directory = Path(spec.preparation_path)
    digest = prep.prepare(spec, directory)
    observation = {"synthetic": True, "engine_process_ids": [31, 32]}
    config = prep.routing_config(spec, observation)
    for name, content in {
        "execution-attempt.json": canonical_json_bytes(
            {"plan_sha256": digest, "no_retry": True}
        ),
        "runtime-observation-ready.json": canonical_json_bytes(observation),
        "execution-config.json": canonical_json_bytes(config.model_dump(mode="json")),
        "selected-workload.jsonl": b"unused by blocked synthetic transport",
    }.items():
        prep.write_private(directory / name, content)
    return spec, directory, digest


@pytest.mark.parametrize(
    "mutation", ["expired", "attempt", "environment", "uid", "config", "observation"]
)
def test_observer_guard_rejects_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _, directory, digest = observer_inputs(tmp_path, expired=mutation == "expired")
    monkeypatch.setattr(observer.os, "getuid", lambda: 2000)
    monkeypatch.setattr(observer.os, "getgid", lambda: 0)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    calls = 0

    def forbidden(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("guarded observer reached transport")

    monkeypatch.setattr(observer, "run_execution_from_bytes", forbidden)
    if mutation == "attempt":
        (directory / "execution-attempt.json").write_bytes(b"{}")
    elif mutation == "environment":
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    elif mutation == "uid":
        monkeypatch.setattr(observer.os, "getuid", lambda: 0)
    elif mutation == "config":
        path = directory / "execution-config.json"
        value = json.loads(path.read_bytes())
        value["request_timeout_ms"] = 999
        path.write_bytes(canonical_json_bytes(value))
    elif mutation == "observation":
        (directory / "runtime-observation-ready.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        observer.run(directory, digest)
    assert calls == 0 and not (directory / "observer-attempt.json").exists()


def test_observer_failed_attempt_cannot_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, directory, digest = observer_inputs(tmp_path)
    monkeypatch.setattr(observer.os, "getuid", lambda: 2000)
    monkeypatch.setattr(observer.os, "getgid", lambda: 0)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("synthetic capture failure")

    monkeypatch.setattr(observer, "run_execution_from_bytes", fail_once)
    with pytest.raises(ValueError, match="synthetic capture failure"):
        observer.run(directory, digest)
    with pytest.raises(FileExistsError):
        observer.run(directory, digest)
    assert calls == 1


@pytest.mark.parametrize("profile", ["v1", "v2"])
def test_observer_run_accepts_real_inventory_for_the_validated_plan_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Literal["v1", "v2"],
) -> None:
    spec, directory, digest = observer_inputs(tmp_path, profile=profile)
    assert spec.module_sha256 == prep.module_digests(profile=profile)
    assert set(spec.module_sha256) == set(
        prep.SFTP_MODULES if profile == "v2" else prep.MODULES
    )
    monkeypatch.setattr(observer.os, "getuid", lambda: 2000)
    monkeypatch.setattr(observer.os, "getgid", lambda: 0)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    output = Path(spec.evidence_path) / "routing-execution-package"
    retained_digest = sha256_digest(b"synthetic-sealed-package")
    captured: list[Path] = []
    verified: list[tuple[Path, str]] = []

    def capture(config: bytes, workload: bytes, target: Path) -> SimpleNamespace:
        assert config == prep.read_private(directory / "execution-config.json")
        assert workload == prep.read_private(directory / "selected-workload.jsonl")
        captured.append(target)
        return SimpleNamespace(path=target, retained_digest=retained_digest)

    def verify(path: Path, *, expected_digest: str) -> None:
        verified.append((path, expected_digest))

    monkeypatch.setattr(observer, "run_execution_from_bytes", capture)
    monkeypatch.setattr(observer, "verify_execution_package", verify)
    observer.run(directory, digest)
    assert captured == [output] and verified == [(output, retained_digest)]
    assert prep.read_private(directory / "observer-attempt.json") == (
        canonical_json_bytes({"plan_sha256": digest, "no_retry": True})
    )


@pytest.mark.parametrize(
    ("profile", "module"),
    [
        ("v1", "vast_process_observer"),
        ("v2", "vast_process_observer"),
        ("v2", "vast_guest_ssh"),
    ],
)
def test_observer_run_rejects_tampered_shared_and_v2_module_digests_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Literal["v1", "v2"],
    module: str,
) -> None:
    _, directory, digest = observer_inputs(
        tmp_path, profile=profile, tampered_module=module
    )
    monkeypatch.setattr(observer.os, "getuid", lambda: 2000)
    monkeypatch.setattr(observer.os, "getgid", lambda: 0)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    captured = False

    def forbidden(*args: Any, **kwargs: Any) -> None:
        nonlocal captured
        captured = True
        raise AssertionError("tampered observer reached capture")

    monkeypatch.setattr(observer, "run_execution_from_bytes", forbidden)
    with pytest.raises(ValueError, match="observer process boundary differs"):
        observer.run(directory, digest)
    assert captured is False and not (directory / "observer-attempt.json").exists()


def test_observer_cli_arms_and_clears_wall_timer_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec, directory, digest = observer_inputs(tmp_path)
    timers: list[float] = []
    monkeypatch.setattr(observer.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        observer.signal, "setitimer", lambda kind, seconds: timers.append(seconds)
    )

    def failed_run(*args: Any) -> None:
        observer._expired(signal.SIGALRM, None)

    monkeypatch.setattr(observer, "run", failed_run)
    assert observer.main(["--directory", str(directory), "--execute-plan", digest]) == 2
    assert len(timers) == 2 and 0 < timers[0] <= spec.campaign_timeout_seconds
    assert timers[1] == 0
    assert json.loads(capsys.readouterr().out) == {"status": "OBSERVER_FAILED"}


def test_vast_preparation_schema_snapshots_are_current_and_valid() -> None:
    from inferdrome.deployment.vast_process_schema import artifacts, check

    check(Path(__file__).resolve().parents[2])
    for name, content in artifacts().items():
        if name.endswith(".schema.json"):
            schema = json.loads(content)
            Draft202012Validator.check_schema(schema)
            assert schema["additionalProperties"] is False


def test_export_cli_returns_sanitized_failure_for_wrong_retained_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = seal_vast_fixture(tmp_path / "package")
    result = prep.main(
        [
            "export",
            "--package",
            str(package),
            "--output",
            str(tmp_path / "export"),
            "--expected-digest",
            "sha256:" + "0" * 64,
        ]
    )
    assert result == 2
    output = capsys.readouterr()
    assert "rejected" in output.out and not output.err
    assert str(tmp_path) not in output.out


def test_total_deadline_after_observer_preparation_does_not_spawn_capture() -> None:
    harness = SupervisorHarness()

    def delayed_observer(
        children: Sequence[runtime.Child],
    ) -> tuple[Sequence[str], Mapping[str, str]]:
        assert len(children) == 2
        harness.clock.now = 6.0
        return ("observer",), {}

    harness.observer = delayed_observer  # type: ignore[method-assign]
    with pytest.raises(runtime.RuntimeFailure, match="RUN_DEADLINE"):
        harness.run()
    assert len(harness.children) == 2 and harness.stopped == [101, 100]
