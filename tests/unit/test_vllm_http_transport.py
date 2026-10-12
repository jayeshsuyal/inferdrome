"""Transport bounds and owned serving environment are explicit before any GPU."""

from pathlib import Path
from typing import Any

import pytest

from inferdrome import breakpoint_pilot_run as pilot
from inferdrome import vllm_router_gpu as gpu
from inferdrome.vllm_router import Router


@pytest.mark.parametrize(
    "value", [0, -1, float("nan"), float("inf"), True, "1", None, 10**1000]
)
def test_invalid_idle_limits_fail_before_network(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ValueError, match="upstream connection settings"):
        Router(
            ("http://127.0.0.1:8001", "http://127.0.0.1:8002"),
            tmp_path / "ledger.jsonl",
            upstream_keepalive_s=value,
        )
    assert not (tmp_path / "ledger.jsonl").exists()


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_connection_reuse_is_an_explicit_boolean(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ValueError, match="upstream connection settings"):
        Router(
            ("http://127.0.0.1:8001", "http://127.0.0.1:8002"),
            tmp_path / "ledger.jsonl",
            reuse_upstream_connections=value,
        )


@pytest.mark.parametrize("pilot_launcher", [False, True], ids=["gpu-study", "pilot"])
def test_both_owned_replicas_override_inherited_idle_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot_launcher: bool
) -> None:
    monkeypatch.setenv("VLLM_HTTP_TIMEOUT_KEEP_ALIVE", "1")
    started: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> object:
        started.append({"argv": argv, **kwargs})
        return object()

    monkeypatch.setattr(gpu.subprocess, "Popen", popen)
    if pilot_launcher:
        processes, logs = pilot._start_engines(
            Path("/engine/vllm"), tmp_path, tmp_path, 8192
        )
    else:
        processes, logs = gpu._start_engines(Path("/engine/vllm"), tmp_path, tmp_path)
    try:
        assert len(processes) == len(started) == 2
        assert [item["env"]["CUDA_VISIBLE_DEVICES"] for item in started] == ["0", "1"]
        assert all(
            item["env"]["VLLM_HTTP_TIMEOUT_KEEP_ALIVE"] == "30" for item in started
        )
        settings = gpu._http_transport_settings()
        assert (
            0 < settings["upstream_keepalive_s"] < settings["engine_http_keepalive_s"]
        )
        assert settings["post_retries"] is False
        assert settings["provenance"] == "CONFIGURED_NOT_RUNTIME_ATTESTED"
    finally:
        for log in logs:
            log.close()
