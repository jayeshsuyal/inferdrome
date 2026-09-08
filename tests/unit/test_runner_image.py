"""Runner image policy and local endpoint-client coverage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.error import URLError

import pytest

from inferdrome import __version__
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.runner import RunnerError, build_parser, run_endpoint_request

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
APACHE_2_LICENSE_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)
PROJECT_LICENSE_FILE_PATTERNS = {
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES/*.txt",
}
PACKAGED_LICENSE_FILES = {
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES/ISC-Lucide.txt",
    "LICENSES/MIT-React.txt",
    "LICENSES/MIT-Vite.txt",
    "LICENSES/OFL-1.1-IBM-Plex-Mono.txt",
    "LICENSES/OFL-1.1-Instrument-Sans.txt",
}
IDENTITY_GUARD_DOCKERFILES = (
    "Dockerfile",
    "Dockerfile.vllm-benchmark-runner",
)


def _checked_in_identity_guard(filename: str) -> str:
    dockerfile = (REPOSITORY_ROOT / filename).read_text(encoding="utf-8")
    start = dockerfile.index('RUN case "${BUILD_FLAVOR}" in')
    end = dockerfile.index("\n    esac", start) + len("\n    esac")
    return dockerfile[start + len("RUN ") : end]


def _run_checked_in_identity_guard(
    filename: str,
    *,
    build_flavor: str,
    version: str,
    source_commit: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", _checked_in_identity_guard(filename)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "BUILD_FLAVOR": build_flavor,
            "INFERDROME_VERSION": version,
            "SOURCE_REPOSITORY_COMMIT": source_commit,
        },
        text=True,
    )


@pytest.mark.parametrize("filename", IDENTITY_GUARD_DOCKERFILES)
@pytest.mark.parametrize("build_flavor", ("proof", "release"))
@pytest.mark.parametrize("version", (__version__, "0.3.0", "0.3.0+build.1"))
def test_checked_in_identity_guards_accept_supported_proof_versions(
    filename: str,
    build_flavor: str,
    version: str,
) -> None:
    completed = _run_checked_in_identity_guard(
        filename,
        build_flavor=build_flavor,
        version=version,
        source_commit="a" * 40,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("filename", IDENTITY_GUARD_DOCKERFILES)
@pytest.mark.parametrize("version", ("0.3", "0.3.0/invalid", "0.3.0 dev0"))
def test_checked_in_identity_guards_reject_malformed_single_line_versions(
    filename: str,
    version: str,
) -> None:
    completed = _run_checked_in_identity_guard(
        filename,
        build_flavor="proof",
        version=version,
        source_commit="a" * 40,
    )

    assert completed.returncode != 0


@pytest.mark.parametrize("filename", IDENTITY_GUARD_DOCKERFILES)
def test_checked_in_identity_guards_reject_invalid_source_commits(
    filename: str,
) -> None:
    completed = _run_checked_in_identity_guard(
        filename,
        build_flavor="release",
        version=__version__,
        source_commit="g" * 40,
    )

    assert completed.returncode != 0


@pytest.mark.parametrize("filename", IDENTITY_GUARD_DOCKERFILES)
def test_checked_in_identity_guards_reject_unknown_flavors(filename: str) -> None:
    completed = _run_checked_in_identity_guard(
        filename,
        build_flavor="candidate",
        version=__version__,
        source_commit="a" * 40,
    )

    assert completed.returncode != 0


@pytest.mark.parametrize("filename", IDENTITY_GUARD_DOCKERFILES)
def test_checked_in_identity_guards_keep_development_bypass(filename: str) -> None:
    completed = _run_checked_in_identity_guard(
        filename,
        build_flavor="development",
        version="not-a-proof-version",
        source_commit="not-a-source-commit",
    )

    assert completed.returncode == 0, completed.stderr


def test_runner_dockerfile_has_pinned_base_non_root_and_locked_install() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)

    assert "# syntax=" not in dockerfile
    assert len(from_images) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in from_images)
    assert "python:3.12.12-slim-bookworm@sha256:" in from_images[0]
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "COPY LICENSE THIRD_PARTY_NOTICES.md ./" in dockerfile
    assert "COPY LICENSES ./LICENSES" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "ADD --checksum=sha256:" in dockerfile
    assert "UV_VERSION" not in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "/usr/sbin/useradd" in dockerfile
    assert not re.search(r"^RUN\s+useradd\b", dockerfile, re.MULTILINE)
    assert "USER 10001:10001" in dockerfile
    assert not re.search(r"^USER\s+root\s*$", dockerfile, re.MULTILINE)
    assert 'VOLUME ["/evidence"]' in dockerfile
    assert 'TMPDIR="/tmp/inferdrome"' in dockerfile
    assert 'ENTRYPOINT ["/opt/inferdrome-runtime/bin/inferdrome"]' in dockerfile
    assert "inferdrome.runner" not in dockerfile.split("ENTRYPOINT", 1)[-1]
    assert "UV_PROJECT_ENVIRONMENT=/opt/inferdrome-runtime" in dockerfile
    assert "/build/.venv" not in dockerfile
    assert (
        "COPY --from=builder --chown=10001:10001 "
        "/opt/inferdrome-runtime /opt/inferdrome-runtime"
    ) in dockerfile
    assert "/usr/share/licenses/inferdrome/" in dockerfile
    assert "/opt/inferdrome-runtime/bin/inferdrome --version" in dockerfile


def test_packaged_runtime_dockerfile_defaults_match_the_package_version() -> None:
    for filename in ("Dockerfile", "Dockerfile.vllm-benchmark-runner"):
        dockerfile = (REPOSITORY_ROOT / filename).read_text(encoding="utf-8")
        assert f"ARG INFERDROME_VERSION={__version__}" in dockerfile


def test_package_license_metadata_and_notices_are_pinned() -> None:
    metadata = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert metadata["license"] == "Apache-2.0"
    assert set(metadata["license-files"]) == PROJECT_LICENSE_FILE_PATTERNS
    assert metadata["urls"] == {
        "Repository": "https://github.com/jayeshsuyal/inferdrome"
    }
    assert hashlib.sha256((REPOSITORY_ROOT / "LICENSE").read_bytes()).hexdigest() == (
        APACHE_2_LICENSE_SHA256
    )

    for relative_path in PACKAGED_LICENSE_FILES:
        assert (REPOSITORY_ROOT / relative_path).is_file()

    lock = json.loads(
        (REPOSITORY_ROOT / "frontend/package-lock.json").read_text(encoding="utf-8")
    )
    runtime_licenses = {
        "@fontsource-variable/instrument-sans": ("5.3.0", "OFL-1.1"),
        "@fontsource/ibm-plex-mono": ("5.3.0", "OFL-1.1"),
        "lucide-react": ("0.468.0", "ISC"),
        "react": ("19.2.8", "MIT"),
        "react-dom": ("19.2.8", "MIT"),
        "scheduler": ("0.27.0", "MIT"),
        "vite": ("7.3.6", "MIT"),
    }
    notices = (REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for package_name, (version, license_name) in runtime_licenses.items():
        locked = lock["packages"][f"node_modules/{package_name}"]
        assert (locked["version"], locked["license"]) == (version, license_name)
        assert package_name in notices
        assert version in notices

    dashboard_bundles = list(
        (REPOSITORY_ROOT / "src/inferdrome/dashboard/static/assets").glob("index-*.js")
    )
    assert len(dashboard_bundles) == 1
    assert '"modulepreload"' in dashboard_bundles[0].read_text(encoding="utf-8")


def test_runner_dockerfile_has_proof_identity_labels_and_no_serving_engine() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    required_labels = (
        "org.opencontainers.image.source=",
        "org.opencontainers.image.revision=",
        "org.opencontainers.image.version=",
        "org.opencontainers.image.title=",
        "org.opencontainers.image.description=",
        'com.inferdrome.image-purpose="runner-only"',
    )

    for label in required_labels:
        assert label in dockerfile
    assert "BUILD_FLAVOR=development" in dockerfile
    assert "proof|release" in dockerfile
    assert not re.search(r"(?i)\b(vllm|sglang)\b", dockerfile)
    assert not re.search(r"(?i)(?:CMD|ENTRYPOINT).*\bserve\b", dockerfile)
    assert "packaged Inferdrome version does not match build identity" in dockerfile
    assert "linux/amd64" in (
        REPOSITORY_ROOT / "scripts/build_runner_image.py"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "filename",
    (
        "Dockerfile",
        "Dockerfile.compose-mock",
        "Dockerfile.vllm-benchmark-runner",
    ),
)
def test_inferdrome_images_retain_project_and_dashboard_licenses(
    filename: str,
) -> None:
    dockerfile = (REPOSITORY_ROOT / filename).read_text(encoding="utf-8")
    assert "LICENSE" in dockerfile
    assert "THIRD_PARTY_NOTICES.md" in dockerfile
    assert "LICENSES" in dockerfile
    assert "/usr/share/licenses/inferdrome" in dockerfile


def test_runner_dockerfile_does_not_declare_secret_shaped_build_inputs() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    declarations = re.findall(r"^(?:ARG|ENV)\s+([^\s=\\]+)", dockerfile, re.MULTILINE)
    forbidden = re.compile(
        r"(?i)(?:secret|password|token|api[_-]?key|credential|private[_-]?key)"
    )

    assert all(forbidden.search(name) is None for name in declarations)
    assert "rm /tmp/uv.tar.gz" in dockerfile
    assert 'HOME="/home/inferdrome"' in dockerfile


def test_runner_build_context_is_deny_by_default_and_excludes_host_state() -> None:
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")

    assert dockerignore.splitlines()[1] == "**"
    for excluded in (
        ".git/",
        ".venv/",
        "tests/",
        "evidence/",
        "gpu-proof-retrieved/",
        "runs/",
        "**/.env",
        "**/*.pem",
        "**/*.key",
        "**/*credentials*",
        "**/*secret*",
        ".DS_Store",
        "**/.DS_Store",
        ".pytest_cache/",
        "**/.pytest_cache/",
        ".mypy_cache/",
        "**/.mypy_cache/",
        ".ruff_cache/",
        "**/.ruff_cache/",
        "**/*.egg-info/",
        "**/__pycache__/",
        "**/*.pyc",
    ):
        assert excluded in dockerignore
    for allowed in ("!Dockerfile", "!pyproject.toml", "!uv.lock", "!src/"):
        assert allowed in dockerignore
    for allowed in (
        "!LICENSE",
        "!THIRD_PARTY_NOTICES.md",
        "!LICENSES/",
        "!LICENSES/**",
    ):
        assert allowed in dockerignore


def test_runner_cli_check_is_explicitly_non_gpu() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/runner_smoke.py", "--check"],
        cwd=REPOSITORY_ROOT,
        env={"PYTHONPATH": str(REPOSITORY_ROOT / "src")},
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "runner host smoke path: available" in completed.stdout
    assert "Docker execution gate:" in completed.stdout


def test_runner_probe_is_named_separately_from_canonical_cli() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'inferdrome = "inferdrome.cli:entrypoint"' in pyproject
    assert 'inferdrome-runner-probe = "inferdrome.runner:main"' in pyproject
    assert 'inferdrome-runner = "inferdrome.runner:main"' not in pyproject


class _FakeResponse:
    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def read(self, _limit: int) -> bytes:
        return b'{"generated":"must-not-be-serialized"}'


class _FailingResponse(_FakeResponse):
    def read(self, _limit: int) -> bytes:
        raise OSError("synthetic response read failure")


class _FakeOpener:
    def __init__(
        self,
        response: _FakeResponse | BaseException | None = None,
    ):
        self.calls = 0
        self.response = response or _FakeResponse()

    def open(self, _request: object, timeout: float) -> _FakeResponse:
        self.calls += 1
        assert timeout == 30.0
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_runner_writes_canonical_bounded_metadata_without_prompt_or_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inferdrome.runner._OPENER", _FakeOpener())
    output = run_endpoint_request(
        endpoint="http://127.0.0.1:8000/v1/chat/completions",
        model="inferdrome/mock-model",
        prompt="prompt must not appear in output",
        evidence_dir=tmp_path,
    )
    output_bytes = (tmp_path / "runner-output.json").read_bytes()

    assert output_bytes == canonical_json_bytes(output)
    assert output["synthetic_only"] is True
    assert output["evidence_eligible"] is False
    assert b"prompt must not appear" not in output_bytes
    assert b"generated" not in output_bytes
    assert b"response_sha256" in output_bytes
    assert "prompt" not in output
    assert not list(tmp_path.glob(".runner-output.json*"))


def test_runner_rejects_unsafe_endpoint_and_output_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = _FakeOpener()
    monkeypatch.setattr("inferdrome.runner._OPENER", opener)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    unsafe_endpoints = (
        "file:///tmp/inferdrome",
        "http://user:secret@127.0.0.1:8000/v1",
        "http://127.0.0.1:8000/v1?token=secret",
        "http://0.0.0.0:8000/v1",
    )

    for endpoint in unsafe_endpoints:
        with pytest.raises(RunnerError):
            run_endpoint_request(
                endpoint=endpoint,
                model="inferdrome/mock-model",
                prompt="safe prompt",
                evidence_dir=evidence_dir,
            )
    assert opener.calls == 0

    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=evidence_dir,
            output_name="../escape.json",
        )
    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path / "missing",
        )
    assert opener.calls == 0


def test_runner_output_is_no_replace_and_symlink_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inferdrome.runner._OPENER", _FakeOpener())
    output_path = tmp_path / "runner-output.json"
    output_path.write_bytes(b"original")
    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path,
        )
    assert output_path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".runner-output.json*"))

    output_path.unlink()
    target = tmp_path / "target.json"
    target.write_bytes(b"target")
    output_path.symlink_to(target)
    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path,
        )
    assert target.read_bytes() == b"target"
    assert not list(tmp_path.glob(".runner-output.json*"))


def test_runner_parser_keeps_narrow_required_surface() -> None:
    arguments = build_parser().parse_args(
        [
            "--endpoint",
            "http://127.0.0.1:8000/v1/chat/completions",
            "--model",
            "inferdrome/mock-model",
            "--prompt",
            "smoke",
            "--evidence-dir",
            "/evidence",
        ]
    )

    assert arguments.endpoint.endswith("/v1/chat/completions")
    assert arguments.evidence_dir == Path("/evidence")


@pytest.mark.parametrize(
    "response",
    [
        URLError("synthetic endpoint failure"),
        _FailingResponse(),
    ],
)
def test_runner_failure_removes_reservation_and_clean_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: BaseException,
) -> None:
    failing = _FakeOpener(response)
    monkeypatch.setattr("inferdrome.runner._OPENER", failing)
    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path,
        )
    assert failing.calls == 1
    assert not (tmp_path / "runner-output.json").exists()
    assert not list(tmp_path.glob(".runner-output.json*"))

    retry = _FakeOpener()
    monkeypatch.setattr("inferdrome.runner._OPENER", retry)
    output = run_endpoint_request(
        endpoint="http://127.0.0.1:8000/v1/chat/completions",
        model="inferdrome/mock-model",
        prompt="safe prompt",
        evidence_dir=tmp_path,
    )
    assert output["evidence_eligible"] is False
    assert retry.calls == 1
    assert not list(tmp_path.glob(".runner-output.json*"))


def test_runner_write_failure_removes_final_and_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inferdrome.runner as runner

    monkeypatch.setattr("inferdrome.runner._OPENER", _FakeOpener())

    def fail_write(*_args: object) -> int:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(runner.os, "write", fail_write)
    with pytest.raises(RunnerError):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path,
        )
    assert not (tmp_path / "runner-output.json").exists()
    assert not list(tmp_path.glob(".runner-output.json*"))


def test_runner_keyboard_interrupt_cleans_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = _FakeOpener(KeyboardInterrupt())
    monkeypatch.setattr("inferdrome.runner._OPENER", opener)
    with pytest.raises(KeyboardInterrupt):
        run_endpoint_request(
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model="inferdrome/mock-model",
            prompt="safe prompt",
            evidence_dir=tmp_path,
        )
    assert opener.calls == 1
    assert not (tmp_path / "runner-output.json").exists()
    assert not list(tmp_path.glob(".runner-output.json*"))
