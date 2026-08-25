"""Runner image policy and local endpoint-client coverage."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.runner import RunnerError, build_parser, run_endpoint_request

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"


def test_runner_dockerfile_has_pinned_base_non_root_and_locked_install() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)

    assert "# syntax=" not in dockerfile
    assert len(from_images) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in from_images)
    assert "python:3.12.12-slim-bookworm@sha256:" in from_images[0]
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "ADD --checksum=sha256:" in dockerfile
    assert "UV_VERSION" not in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert not re.search(r"^USER\s+root\s*$", dockerfile, re.MULTILINE)
    assert 'VOLUME ["/evidence"]' in dockerfile
    assert 'TMPDIR="/tmp/inferdrome"' in dockerfile
    assert 'ENTRYPOINT ["/opt/inferdrome-runtime/bin/inferdrome"]' in dockerfile
    assert "inferdrome.runner" not in dockerfile.split("ENTRYPOINT", 1)[-1]


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
    ):
        assert excluded in dockerignore
    for allowed in ("!Dockerfile", "!pyproject.toml", "!uv.lock", "!src/"):
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
