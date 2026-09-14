"""CPU coordinator refusal/publication tests with fake commands and HTTP only."""

from __future__ import annotations

import argparse
import builtins
import copy
import hashlib
import io
import json
import os
import select
import signal
import tarfile
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import vast_cpu_build as build
from scripts.vast_cpu_common import Failure, Result, read_json, sha256_file, write_json

SOURCE = "a" * 40
IMAGE = "sha256:" + "b" * 64
LOCK = "c" * 64
TOKEN = "synthetic-public-token-for-testing"


class Budget:
    def __init__(self, timestamp: str) -> None:
        self.original_timestamp = timestamp
        self.calls = 0

    def child(self, seconds: float) -> Budget:
        return self

    def remaining(self) -> float:
        self.calls += 1
        if self.calls > 20:
            raise Failure("ORIGINAL_DEADLINE_EXPIRED")
        return 5


def image_metadata() -> dict[str, Any]:
    return {
        "Os": "linux",
        "Architecture": "amd64",
        "Id": IMAGE,
        "Size": 1000,
        "Config": {
            "User": "0:0",
            "Entrypoint": [
                build.PYTHON,
                "-I",
                "-m",
                "inferdrome.deployment.vast_guest_ssh",
            ],
            "Labels": {
                "org.opencontainers.image.revision": SOURCE,
                "org.opencontainers.image.source": "https://github.com/"
                + build.REPOSITORY,
                "com.inferdrome.os-package-lock-sha256": LOCK,
            },
        },
    }


class Scenario:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.args = argparse.Namespace(
            mode="PUBLISH_QUALIFIED_VAST",
            source_commit=SOURCE,
            expected_ref="refs/heads/codex/add-vast-process-adapter",
            expected_runner_name="vast-cpu-test-01",
            attempt_id="12345",
            attempt_start="2030-01-01T00:00:00Z",
            execution_deadline="2030-01-01T01:45:00Z",
            cleanup_deadline="2030-01-01T02:00:00Z",
            max_egress_bytes=10 * 1024**3,
            work_root=root / "inferdrome-vast-12345",
        )
        self.now = datetime(2030, 1, 1, 0, 1, tzinfo=UTC)
        self.head, self.dirty = SOURCE, ""
        self.gpus: list[Path] = []
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.http: list[tuple[str, bool]] = []
        self.metadata = image_metadata()
        self.remote_image_id = IMAGE
        self.raw = json.dumps(
            {"schemaVersion": 2, "config": {"digest": IMAGE}}
        ).encode()
        self.digest = "sha256:" + hashlib.sha256(self.raw).hexdigest()
        self.immutable = build.TARGET + "@" + self.digest
        self.tag = f"{build.TARGET}:candidate-{SOURCE[:12]}-12345"
        self.package: Any = {
            "name": "inferdrome-vast-process",
            "package_type": "container",
            "visibility": "private",
        }
        self.fail_push = False
        self.pushed = False
        self.manifest_members: list[tarfile.TarInfo] = []
        self.environment = {"PATH": "/usr/bin:/bin"}
        self.home = self.args.work_root / "command-home"
        for name, value in {
            "EXECUTION_MODE": self.args.mode,
            "MODE_CONFIRMATION": self.args.mode,
            "GITHUB_REPOSITORY": build.REPOSITORY,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": self.args.expected_ref,
            "GITHUB_SHA": SOURCE,
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
            "RUNNER_NAME": self.args.expected_runner_name,
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "RUNNER_ENVIRONMENT": "self-hosted",
            "RUNNER_TEMP": str(root),
            "GHCR_TOKEN": TOKEN,
            "GITHUB_ACTOR": "jayeshsuyal",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(build, "git", self.git)
        monkeypatch.setattr(
            build, "datetime", SimpleNamespace(now=lambda zone: self.now)
        )
        monkeypatch.setattr(
            build,
            "platform",
            SimpleNamespace(
                system=lambda: "Linux",
                machine=lambda: "x86_64",
            ),
        )
        original_glob = Path.glob
        monkeypatch.setattr(
            Path,
            "glob",
            lambda path, pattern: (
                iter(self.gpus)
                if path == Path("/dev")
                else original_glob(path, pattern)
            ),
        )
        monkeypatch.setattr(build, "Deadline", Budget)
        monkeypatch.setattr(build, "Runner", lambda root, cleanup: self)
        monkeypatch.setattr(build, "network_bytes", lambda: {"eth0": 100})
        monkeypatch.setattr(build, "package_metadata", self.package_metadata)
        monkeypatch.setattr(build.time, "sleep", lambda seconds: None)

    def git(self, *arguments: str) -> str:
        return {
            ("rev-parse", "HEAD"): self.head,
            ("status", "--porcelain"): self.dirty,
            ("rev-parse", "HEAD^{tree}"): "d" * 40,
        }[arguments]

    def prepare_receipt(self) -> None:
        build.initialize(self.args)
        self.home.mkdir(mode=0o700)
        binding = build.identity(self.args)
        write_json(
            self.args.work_root / "evidence/qualified.json",
            {
                **binding,
                "schema_version": "vast-cpu-qualified-image-v1",
                "status": "QUALIFIED",
                "tag": self.tag,
                "image_id": IMAGE,
                "os_lock_sha256": LOCK,
            },
        )
        self.seal_receipt()
        write_json(self.args.work_root / "owned-containers.json", [])

    def seal_receipt(self) -> None:
        (self.args.work_root / "qualification-digest.json").write_text(
            json.dumps(
                {
                    "sha256": sha256_file(
                        self.args.work_root / "evidence/qualified.json"
                    ),
                }
            )
        )

    def edit_receipt(self, field: str, value: Any, *, reseal: bool = True) -> None:
        path = self.args.work_root / "evidence/qualified.json"
        content = read_json(path)
        content[field] = value
        path.write_text(json.dumps(content))
        if reseal:
            self.seal_receipt()

    def package_metadata(
        self, token: str, budget: Budget, *, versions: bool = False
    ) -> Any:
        self.http.append((token, versions))
        if versions:
            return [
                {
                    "id": 987,
                    "name": self.digest,
                    "metadata": {
                        "container": {"tags": [self.tag.rsplit(":", 1)[1]]},
                    },
                }
            ]
        return self.package

    def check_egress(self) -> dict[str, object]:
        return {"observed": True, "conservative_tx_bytes": 0}

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        self.calls.append((argv, kwargs))
        if argv[:3] == ["docker", "image", "inspect"]:
            value = copy.deepcopy(self.metadata)
            if argv[-1] == self.immutable:
                value["Id"] = self.remote_image_id
            if self.pushed:
                value["RepoDigests"] = [self.immutable]
            return Result(0, json.dumps([value]).encode(), b"")
        if argv[:2] == ["docker", "login"]:
            assert "GHCR_TOKEN" not in os.environ
            assert kwargs["input"] == TOKEN.encode()
            assert argv[-1] == "--password-stdin" and TOKEN not in argv
        elif argv[:2] == ["docker", "push"]:
            assert argv[2:] == [self.tag]
            if self.fail_push:
                raise Failure("SYNTHETIC_PUSH_FAILURE")
            self.pushed = True
        elif argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            assert argv[4:] == [self.immutable, "--raw"]
            return Result(0, self.raw, b"")
        elif argv[:2] == ["docker", "pull"]:
            assert argv[2:] == ["--platform=linux/amd64", self.immutable]
        elif argv == ["docker", "logout", "ghcr.io"]:
            assert kwargs["deadline"].original_timestamp == self.args.cleanup_deadline
        elif argv[:4] == ["git", "-C", str(build.SOURCE), "archive"]:
            archive = Path(
                next(
                    value.removeprefix("--output=")
                    for value in argv
                    if value.startswith("--output=")
                )
            )
            with tarfile.open(archive, "w") as stream:
                for member in self.manifest_members:
                    stream.addfile(member, io.BytesIO(b"x" * member.size))
        else:
            raise AssertionError("unexpected fake command")
        return Result(0, b"", b"")


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    return Scenario(tmp_path, monkeypatch)


def test_identity_and_initialized_attempt_bind_exact_source_and_original_clock(
    scenario: Scenario,
) -> None:
    binding = build.identity(scenario.args)
    assert binding["source_commit"] == SOURCE and binding["source_tree"] == "d" * 40
    assert binding["target"] == build.TARGET and binding["platform"] == "linux/amd64"
    build.initialize(scenario.args)
    assert build.load_attempt(scenario.args) == binding
    assert read_json(scenario.args.work_root / "network-start.json") == {
        "interfaces": {"eth0": 100},
        "max_egress_bytes": scenario.args.max_egress_bytes,
    }
    assert scenario.calls == [] and scenario.http == []
    with pytest.raises(FileExistsError):
        build.initialize(scenario.args)


@pytest.mark.parametrize(
    "name,value",
    [
        ("EXECUTION_MODE", "QUALIFY_ONLY"),
        ("MODE_CONFIRMATION", "OTHER"),
        ("GITHUB_REPOSITORY", "other/inferdrome"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/other"),
        ("GITHUB_SHA", "e" * 40),
        ("GITHUB_RUN_ID", "67890"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("RUNNER_NAME", "other-worker"),
        ("RUNNER_OS", "macOS"),
        ("RUNNER_ARCH", "ARM64"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
        ("RUNNER_TEMP", "relative/path"),
    ],
)
def test_identity_environment_mismatch_precedes_attempt_or_external_actions(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(Failure):
        build.initialize(scenario.args)
    assert not scenario.args.work_root.exists()
    assert scenario.calls == [] and scenario.http == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_commit", "A" * 40),
        ("expected_ref", "refs/pull/81/head"),
        ("expected_runner_name", "bad/name"),
        ("attempt_id", "012345"),
        ("execution_deadline", "2030-01-01T01:46:00Z"),
        ("cleanup_deadline", "2030-01-01T02:01:00Z"),
        ("max_egress_bytes", 0),
        ("max_egress_bytes", 40 * 1024**3 + 1),
        ("work_root", Path("/synthetic/wrong-root")),
    ],
)
def test_identity_argument_mismatch_is_refused_before_commands(
    scenario: Scenario,
    field: str,
    value: Any,
) -> None:
    setattr(scenario.args, field, value)
    with pytest.raises(Failure):
        build.identity(scenario.args)
    assert scenario.calls == [] and scenario.http == []


@pytest.mark.parametrize("state", ["dirty", "wrong-head", "gpu", "future", "expired"])
def test_identity_rejects_unreviewed_or_expired_host_state(
    scenario: Scenario,
    state: str,
) -> None:
    if state == "dirty":
        scenario.dirty = " M source.py"
    elif state == "wrong-head":
        scenario.head = "e" * 40
    elif state == "gpu":
        scenario.gpus = [Path("/dev/nvidia0")]
    elif state == "future":
        scenario.now -= timedelta(minutes=2)
    else:
        scenario.now += timedelta(minutes=105)
    with pytest.raises(Failure):
        build.identity(scenario.args)
    assert scenario.calls == []


def test_cleanup_identity_keeps_the_original_window_after_execution_expires(
    scenario: Scenario,
) -> None:
    scenario.now = datetime(2030, 1, 1, 1, 50, tzinfo=UTC)
    with pytest.raises(Failure, match="ATTEMPT_EXPIRED_OR_FUTURE"):
        build.identity(scenario.args)
    assert build.identity(scenario.args, cleaning=True)["cleanup_deadline"] == (
        "2030-01-01T02:00:00Z"
    )
    scenario.now = datetime(2030, 1, 1, 2, tzinfo=UTC)
    with pytest.raises(Failure, match="ATTEMPT_EXPIRED_OR_FUTURE"):
        build.identity(scenario.args, cleaning=True)


def test_dirty_source_blocks_work_but_preserves_bound_owned_cleanup(
    scenario: Scenario,
) -> None:
    build.initialize(scenario.args)
    scenario.dirty = " M source.py"
    with pytest.raises(Failure, match="SOURCE_MISMATCH_OR_DIRTY"):
        build.load_attempt(scenario.args)
    assert build.load_attempt(scenario.args, cleaning=True)["source_commit"] == SOURCE
    scenario.head = "e" * 40
    with pytest.raises(Failure, match="SOURCE_MISMATCH_OR_DIRTY"):
        build.load_attempt(scenario.args, cleaning=True)


def test_expired_setup_refuses_initialization_before_creating_state(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(args: argparse.Namespace, minutes: int) -> Any:
        assert minutes == 15
        raise Failure("ORIGINAL_DEADLINE_EXPIRED")

    monkeypatch.setattr(build, "phase", expired)
    with pytest.raises(Failure, match="ORIGINAL_DEADLINE_EXPIRED"):
        build.initialize(scenario.args)
    assert not scenario.args.work_root.exists()
    assert scenario.calls == []


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_source_archive_rejects_links_and_special_files_before_extraction(
    scenario: Scenario,
    tmp_path: Path,
    kind: bytes,
) -> None:
    member = tarfile.TarInfo("src/unsafe")
    member.type, member.linkname = kind, "outside"
    scenario.manifest_members = [member]
    with pytest.raises(Failure, match="SOURCE_ARCHIVE_LINK_OR_SPECIAL_FILE"):
        build.archive_source(scenario, tmp_path, SOURCE, Budget("synthetic"))
    assert not list((tmp_path / "context").iterdir())


def test_source_archive_refuses_a_path_outside_its_context(
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    member = tarfile.TarInfo("../outside")
    member.size = 1
    scenario.manifest_members = [member]
    with pytest.raises(tarfile.OutsideDestinationError):
        build.archive_source(scenario, tmp_path, SOURCE, Budget("synthetic"))
    assert not (tmp_path / "outside").exists()


def test_source_archive_refuses_duplicate_paths_before_extraction(
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    member = tarfile.TarInfo("src/duplicated.py")
    member.size = 1
    scenario.manifest_members = [member, member]
    with pytest.raises(Failure, match="SOURCE_ARCHIVE_DUPLICATE_PATH"):
        build.archive_source(scenario, tmp_path, SOURCE, Budget("synthetic"))
    assert not list((tmp_path / "context").iterdir())


@pytest.mark.parametrize(
    "section,field,value",
    [
        (None, "Os", "windows"),
        (None, "Architecture", "arm64"),
        (None, "Id", "latest"),
        (None, "Size", -1),
        (None, "Size", True),
        (None, "Size", 0),
        ("Config", "User", "2000:0"),
        ("Config", "Entrypoint", ["/bin/sh"]),
        ("Config", "Cmd", ["serve"]),
        ("Config", "ExposedPorts", {"8000/tcp": {}}),
        ("Config", "Volumes", {"/model": {}}),
        ("Config", "Healthcheck", {"Test": ["CMD", "true"]}),
        ("Labels", "org.opencontainers.image.revision", "e" * 40),
        ("Labels", "org.opencontainers.image.source", "https://github.com/other/repo"),
        ("Labels", "com.inferdrome.os-package-lock-sha256", "e" * 64),
    ],
)
def test_image_metadata_refuses_other_source_platform_or_runtime_configuration(
    section: str | None,
    field: str,
    value: Any,
) -> None:
    metadata = image_metadata()
    target = (
        metadata
        if section is None
        else (
            metadata["Config"]["Labels"] if section == "Labels" else metadata[section]
        )
    )
    target[field] = value
    with pytest.raises(Failure):
        build.validate_image(metadata, SOURCE, LOCK)


@pytest.mark.parametrize(
    "field,value,reseal",
    [
        ("status", "FAILED", True),
        ("source_commit", "e" * 40, True),
        ("attempt_id", "67890", True),
        ("target", "ghcr.io/other/image", True),
        ("tag", "ghcr.io/other/image:tag", True),
        ("image_id", "sha256:" + "e" * 64, True),
        ("status", "QUALIFIED", False),
    ],
)
def test_publication_requires_unchanged_bound_receipt_before_credentials(
    scenario: Scenario,
    field: str,
    value: Any,
    reseal: bool,
) -> None:
    scenario.prepare_receipt()
    scenario.edit_receipt(field, value, reseal=reseal)
    with pytest.raises(Failure):
        build.publish(scenario.args)
    assert scenario.http == []
    assert not any(
        argv[:2] in (["docker", "login"], ["docker", "push"])
        for argv, _ in scenario.calls
    )
    assert os.environ["GHCR_TOKEN"] == TOKEN


@pytest.mark.parametrize(
    "state", ["remaining-container", "changed-image", "over-egress"]
)
def test_image_identity_cleanup_and_transfer_size_gate_publication_credentials(
    scenario: Scenario,
    state: str,
) -> None:
    scenario.prepare_receipt()
    if state == "remaining-container":
        (scenario.args.work_root / "owned-containers.json").write_text(
            json.dumps(["e" * 64])
        )
    elif state == "changed-image":
        scenario.metadata["Id"] = "sha256:" + "e" * 64
    else:
        scenario.metadata["Size"] = scenario.args.max_egress_bytes
    with pytest.raises(Failure):
        build.publish(scenario.args)
    assert scenario.http == [] and os.environ["GHCR_TOKEN"] == TOKEN


@pytest.mark.parametrize(
    "field,value",
    [
        ("visibility", "public"),
        ("name", "different-image"),
        ("package_type", "npm"),
    ],
)
def test_wrong_existing_package_is_refused_before_login_or_push_and_logs_out(
    scenario: Scenario,
    field: str,
    value: str,
) -> None:
    scenario.prepare_receipt()
    scenario.package[field] = value
    with pytest.raises(Failure, match="TARGET_NOT_PRIVATE"):
        build.publish(scenario.args)
    assert not any(
        argv[:2] in (["docker", "login"], ["docker", "push"])
        for argv, _ in scenario.calls
    )
    assert scenario.calls[-1][0] == ["docker", "logout", "ghcr.io"]
    assert not (scenario.home / "docker-config").exists()


def test_publication_pins_remote_manifest_image_private_tag_and_cleans_credentials(
    scenario: Scenario,
) -> None:
    scenario.prepare_receipt()
    build.publish(scenario.args)
    receipt = read_json(scenario.args.work_root / "evidence/publication.json")
    assert receipt["immutable_image"] == scenario.immutable
    assert receipt["image_id"] == IMAGE and receipt["visibility"] == "private"
    assert receipt["package_version_id"] == 987
    assert receipt["qualification_sha256"] == sha256_file(
        scenario.args.work_root / "evidence/qualified.json"
    )
    assert receipt["provider_cleanup"] == "OPERATOR_MUST_VERIFY"
    assert [
        argv[1] for argv, _ in scenario.calls if argv[1] in {"login", "push", "logout"}
    ] == [
        "login",
        "push",
        "logout",
    ]
    assert not (scenario.home / "docker-config").exists()
    assert "GHCR_TOKEN" not in os.environ
    assert all(TOKEN not in " ".join(argv) for argv, _ in scenario.calls)
    assert all(
        "env" not in kwargs or "GHCR_TOKEN" not in kwargs["env"]
        for _, kwargs in scenario.calls
    )


def test_qualification_only_mode_cannot_reach_publication_commands(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario.args.mode = "QUALIFY_ONLY"
    monkeypatch.setenv("EXECUTION_MODE", "QUALIFY_ONLY")
    monkeypatch.setenv("MODE_CONFIRMATION", "QUALIFY_ONLY")
    scenario.prepare_receipt()
    with pytest.raises(Failure, match="PUBLICATION_MODE_REQUIRED"):
        build.publish(scenario.args)
    assert scenario.calls == [] and scenario.http == []
    assert os.environ["GHCR_TOKEN"] == TOKEN


def test_visibility_change_after_push_is_not_reported_as_private_publication(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario.prepare_receipt()

    def metadata(token: str, budget: Budget, *, versions: bool = False) -> Any:
        if not versions and scenario.pushed:
            return {**scenario.package, "visibility": "public"}
        return scenario.package_metadata(token, budget, versions=versions)

    monkeypatch.setattr(build, "package_metadata", metadata)
    with pytest.raises(Failure, match="PUBLISHED_TARGET_NOT_PRIVATE"):
        build.publish(scenario.args)
    assert scenario.pushed
    assert not (scenario.args.work_root / "evidence/publication.json").exists()
    assert scenario.calls[-1][0] == ["docker", "logout", "ghcr.io"]


@pytest.mark.parametrize(
    "failure", ["push", "manifest-hash", "manifest-config", "pulled-image"]
)
def test_failed_push_or_immutable_readback_never_emits_success_and_logs_out(
    scenario: Scenario,
    failure: str,
) -> None:
    scenario.prepare_receipt()
    if failure == "push":
        scenario.fail_push = True
    elif failure == "manifest-hash":
        scenario.raw += b" "
    elif failure == "manifest-config":
        scenario.raw = json.dumps({"config": {"digest": "sha256:" + "e" * 64}}).encode()
        scenario.digest = "sha256:" + hashlib.sha256(scenario.raw).hexdigest()
        scenario.immutable = build.TARGET + "@" + scenario.digest
    else:
        scenario.remote_image_id = "sha256:" + "e" * 64
    with pytest.raises(Failure):
        build.publish(scenario.args)
    assert not (scenario.args.work_root / "evidence/publication.json").exists()
    assert scenario.calls[-1][0] == ["docker", "logout", "ghcr.io"]
    assert not (scenario.home / "docker-config").exists()


def test_validate_module_import_needs_no_application_or_third_party_packages() -> None:
    real_import = builtins.__import__

    def restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name.split(".")[0] not in {"inferdrome", "yaml", "pydantic", "pytest"}
        return real_import(name, *args, **kwargs)

    namespace = {
        "__name__": "synthetic_vast_cpu_build_import",
        "__file__": build.__file__,
        "__builtins__": {**vars(builtins), "__import__": restricted_import},
    }
    exec(compile(Path(build.__file__).read_text(), build.__file__, "exec"), namespace)
    assert callable(namespace["initialize"]) and callable(namespace["cleanup_owned"])


@pytest.mark.parametrize("body", [b"not-json", b"x" * (2 * 1024 * 1024 + 1)])
def test_registry_http_response_is_bounded_and_invalid_data_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    def open_request(request: Any, *, timeout: float) -> io.BytesIO:
        assert request.full_url == build.PACKAGE_API
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        assert 0 < timeout <= 10
        return io.BytesIO(body)

    monkeypatch.setattr(
        build.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=open_request),
    )
    with pytest.raises(Failure):
        build.package_metadata(TOKEN, Budget("synthetic"))


def test_registry_credentials_are_not_forwarded_to_redirects() -> None:
    with pytest.raises(Failure, match="REGISTRY_METADATA_REDIRECT_REFUSED"):
        build.NoRedirect().redirect_request(
            None, None, None, None, None, "https://other.invalid"
        )


@pytest.mark.parametrize("status", [403, 404, 500])
def test_registry_absence_and_access_errors_are_not_conflated(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def open_request(request: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError(request.full_url, status, "synthetic", None, None)

    monkeypatch.setattr(
        build.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=open_request),
    )
    if status == 404:
        assert build.package_metadata(TOKEN, Budget("synthetic")) is None
    else:
        with pytest.raises(Failure, match="PACKAGE_ACCESS_UNCONFIRMED"):
            build.package_metadata(TOKEN, Budget("synthetic"))


def test_registry_entire_body_read_has_an_absolute_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowBody(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            # A local wait stands in for a TLS peer dripping bytes; no network.
            select.select([], [], [], 1)
            pytest.fail("the bounded read should have been interrupted")

    monkeypatch.setattr(
        build.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: SlowBody()),
    )
    before = signal.getsignal(signal.SIGALRM)
    with pytest.raises(Failure, match="REGISTRY_METADATA_DEADLINE"):
        build.package_metadata(TOKEN, SimpleNamespace(remaining=lambda: 0.03))
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize("command", ["qualify", "publish"])
def test_failed_phase_retains_sanitized_evidence_and_cleans_its_bound_attempt(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: Any, command: str
) -> None:
    build.initialize(scenario.args)
    scenario.args.command = command
    cleaned: list[argparse.Namespace] = []

    def fail(args: argparse.Namespace) -> None:
        scenario.dirty = " M generated-unexpectedly"
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(
        build, "parser", lambda: SimpleNamespace(parse_args=lambda argv: scenario.args)
    )
    monkeypatch.setattr(build, "build_and_qualify", fail)
    monkeypatch.setattr(build, "publish", fail)
    monkeypatch.setattr(build, "_cleanup_validated", cleaned.append)
    assert build.main([]) == 1
    assert cleaned == [scenario.args]
    evidence = scenario.args.work_root / "evidence" / f"failure-{command}.json"
    assert read_json(evidence)["status"] == "FAILED"
    assert TOKEN not in evidence.read_text() + capsys.readouterr().out


def test_unbound_attempt_failure_never_runs_cleanup(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario.args.command = "qualify"
    cleaned: list[argparse.Namespace] = []
    monkeypatch.setattr(
        build, "parser", lambda: SimpleNamespace(parse_args=lambda argv: scenario.args)
    )
    monkeypatch.setattr(build, "_cleanup_validated", cleaned.append)
    assert build.main([]) == 1
    assert not cleaned
    assert not scenario.args.work_root.exists()


def test_interrupted_static_create_still_removes_recovered_id_if_journal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = "f" * 64
    removed: list[str] = []

    def interrupted(argv: list[str], **kwargs: Any) -> Any:
        assert argv[:2] == ["docker", "create"]
        (tmp_path / "static.cid").write_text(cid + "\n")
        raise Failure("SYNTHETIC_CREATE_INTERRUPTION")

    def broken_journal(value: str) -> None:
        assert value == cid
        raise OSError("synthetic journal failure")

    runner = SimpleNamespace(run=interrupted, record_container=broken_journal)
    monkeypatch.setattr(
        build, "remove_container", lambda runner, value, cleanup: removed.append(value)
    )
    with pytest.raises(OSError, match="synthetic journal failure"):
        build.static_probe(runner, IMAGE, tmp_path, Budget("work"), Budget("cleanup"))
    assert removed == [cid]
