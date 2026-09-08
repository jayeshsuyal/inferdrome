"""CPU-only packaging checks; no Docker, package downloads or serving imports."""

from __future__ import annotations

import importlib.metadata
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from inferdrome import __version__

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "/opt/inferdrome-runtime"


def _instructions() -> list[str]:
    return (ROOT / "Dockerfile.vllm-benchmark-runner").read_text().replace(
        "\\\n", ""
    ).splitlines()


def test_copied_venv_retains_the_same_pinned_base_and_explicit_locked_interpreter():
    instructions = _instructions()
    bases = [shlex.split(line)[1] for line in instructions if line.startswith("FROM ")]
    assert len(bases) == 2
    # Cross-base venv copies leave interpreter/stdlib holes.
    assert bases[0] == bases[1]
    assert bases[0].startswith("vllm/vllm-openai@sha256:")
    sync = next(line for line in instructions if "uv sync" in line)
    words = shlex.split(sync)
    for flag in ("--frozen", "--no-dev", "--no-editable", "--no-python-downloads"):
        assert flag in words
    assert words[words.index("--python") + 1] == RUNTIME + "/bin/python"


def _populate_local_application(runtime: Path) -> None:
    """Copy the already-installed core lock graph for shell smokes, not an install.

    This fixture exercises interpreter isolation and source entrypoints. Only a
    future image build can validate uv's locked installation on Linux/amd64.
    """
    site = next(runtime.glob("lib/python*/site-packages"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    pending = [item["name"] for item in packages["inferdrome"]["dependencies"]]
    copied = set()
    while pending:
        name = pending.pop()
        if name in copied:
            continue
        copied.add(name)
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files or ():
            if ".." in file.parts or file.suffix == ".pyc":
                continue
            source = distribution.locate_file(file)
            if source.is_file():
                target = site / file
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        pending.extend(item["name"] for item in packages[name].get("dependencies", []))
    shutil.copytree(ROOT / "src/inferdrome", site / "inferdrome")
    # Use the checked-in console entrypoint declaration; no copied CLI behavior.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    module, function = project["scripts"]["inferdrome"].split(":")
    cli = runtime / "bin/inferdrome"
    cli.write_text(
        f"#!{runtime}/bin/python\nfrom {module} import {function}\n{function}()\n"
    )
    cli.chmod(0o755)


def test_actual_environment_creation_and_final_application_cpu_checks(tmp_path: Path):
    instructions = _instructions()
    create = next(line for line in instructions if "-m venv " in line)
    words = shlex.split(create.removeprefix("RUN "))
    assert words[0] == "/usr/bin/python3.12"
    assert "--without-pip" in words
    assert "--system-site-packages" not in words
    runtime = tmp_path / "application"
    # Only map the image's interpreter and absolute prefix to local fixture paths.
    create = create.removeprefix("RUN ").replace(words[0], shlex.quote(sys.executable))
    create = create.replace(RUNTIME, shlex.quote(str(runtime)))
    environment = {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}
    subprocess.run(["/bin/sh", "-c", create], env=environment, check=True)
    config = (runtime / "pyvenv.cfg").read_text()
    assert "include-system-site-packages = false" in config
    _populate_local_application(runtime)

    smoke_index = next(
        index for index, line in enumerate(instructions)
        if line.startswith('RUN test "$(/opt/inferdrome-runtime/bin/python')
    )
    assert "USER 2000:0" in instructions[:smoke_index]
    smoke = instructions[smoke_index].removeprefix("RUN ")
    checks = smoke.split(" && ")
    # Serving distribution lookup is exercised with a separate real interpreter
    # in test_gcp_private_engine_adapter, including the actual serve() boundary.
    assert "observed_vllm_version()" in checks.pop()
    assert any("inferdrome.routing_execution --help" in check for check in checks)
    assert sum(" --help" in check for check in checks) == 3
    environment.update({
        "PATH": f"{runtime}/bin:{os.defpath}",
        "INFERDROME_VERSION": __version__,
        "VIRTUAL_ENV": "",
        "PYTHONPATH": "",
        "PYTHONHOME": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for check in checks:
        completed = subprocess.run(
            ["/bin/sh", "-c", check.replace(RUNTIME, str(runtime))],
            env=environment,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 0, (check, completed.stderr)
