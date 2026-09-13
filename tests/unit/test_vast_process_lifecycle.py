"""Kernel-level local process checks; no endpoint, model, GPU or provider."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from inferdrome.deployment import vast_process_runtime as runtime


def test_readiness_probe_wall_limit_stops_a_blocked_local_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[subprocess.Popen[bytes]] = []

    def blocked_probe(
        argv: Sequence[str], environment: Mapping[str, str]
    ) -> runtime.Child:
        del argv, environment
        child = subprocess.Popen(
            (sys.executable, "-I", "-c", "import time; time.sleep(60)"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        children.append(child)
        return child

    monkeypatch.setattr(runtime, "spawn", blocked_probe)
    started = time.monotonic()
    assert runtime._ready({}, 0.05) is False
    assert time.monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None


def test_stop_escalates_when_a_local_owned_process_ignores_sigterm(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "armed"
    child = runtime.spawn(
        (
            sys.executable,
            "-I",
            "-c",
            "import pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)",
            str(marker),
        ),
        {"PATH": os.defpath},
    )
    try:
        cutoff = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < cutoff:
            time.sleep(0.01)
        assert marker.exists()
        runtime.stop_child(child)
        assert child.poll() == -9
    finally:
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait(timeout=3)


def test_interrupting_preflight_reaps_probe_in_its_owned_group(tmp_path: Path) -> None:
    marker = tmp_path / "probe.json"
    source = Path(runtime.__file__).resolve().parents[2]
    # Replace only the command at the Popen seam in this isolated test process.
    # The real _gpu_probe selector, signal handler and teardown still run.
    code = """
import sys, signal
from types import SimpleNamespace
from inferdrome.deployment import vast_process_runtime as runtime
real_popen = runtime.subprocess.Popen
def fake_gpu(argv, **kwargs):
    assert kwargs['start_new_session'] is False
    command = (
        'import json,os,pathlib,sys,time; '
        'pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),os.getpgrp()])); '
        'time.sleep(60)'
    )
    return real_popen((sys.executable, '-I', '-c', command, sys.argv[1]), **kwargs)
runtime.subprocess.Popen = fake_gpu
runtime.remaining_seconds = lambda spec: 60
signal.signal(signal.SIGTERM, runtime._interrupt)
runtime._gpu_probe(SimpleNamespace(preparation_path=sys.argv[2]), {})
"""
    preflight = subprocess.Popen(
        (sys.executable, "-c", code, str(marker), str(tmp_path)),
        env={"PYTHONPATH": str(source), "PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        cutoff = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < cutoff:
            time.sleep(0.01)
        assert marker.exists()
        probe_pid, process_group = json.loads(marker.read_bytes())
        assert process_group == preflight.pid
        preflight.send_signal(signal.SIGTERM)
        assert preflight.wait(timeout=5) != 0
        with pytest.raises(ProcessLookupError):
            os.kill(probe_pid, 0)
    finally:
        if preflight.poll() is None:
            os.killpg(preflight.pid, signal.SIGKILL)
            preflight.wait(timeout=3)
