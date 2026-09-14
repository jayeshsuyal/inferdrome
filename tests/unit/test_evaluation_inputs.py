"""Malformed input and private-file admission before any evaluation socket."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError, load_config_bytes
from inferdrome.evaluation.files import OutputFile, read_input


def config_bytes(**updates: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": "inferdrome.evaluation-config.v1",
        "source_commit": "a" * 40,
        "model": "model-private-sentinel",
        "endpoints": [
            {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8000"},
            {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"},
        ],
        "bounds": {},
        "offers": [
            {
                "scheduled_ns": 0,
                "endpoint_id": "endpoint-a",
                "prompt": "prompt-private-sentinel",
            }
        ],
    }
    value.update(updates)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com:8000",
        "http://8.8.8.8:8000",
        "http://169.254.169.254:80",
        "http://127.0.0.2:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000/",
        "http://secret@127.0.0.1:8000",
        "http://127.0.0.1:8000?secret",
        "http://127.0.0.1:8000#secret",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:08000",
        "http://[::1]:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8000\n",
        "http://2130706433:8000",
    ],
)
def test_secret_bearing_or_unsupported_endpoint_rejected(origin: str) -> None:
    data = config_bytes(
        endpoints=[
            {"endpoint_id": "endpoint-a", "origin": origin},
            {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"},
        ]
    )
    with pytest.raises(EvaluationError, match="violates its contract") as error:
        load_config_bytes(data)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "private-sentinel"},
        {"temperature": False},
        {"temperature": 0.0},
        {"enable_thinking": 0},
        {"enable_thinking": True},
        {"max_tokens": True},
        {"model": "bad\nmodel"},
        {"source_commit": "mutable-main"},
        {"bounds": {"concurrency": 0}},
        {"bounds": {"max_queue": -1}},
        {"bounds": {"request_timeout_ns": 0}},
        {"bounds": {"duration_ns": float("nan")}},
        {"bounds": {"max_requests": 10000, "max_content_events": 4096}},
        {"bounds": {"max_event_bytes": 20, "max_stream_bytes": 10}},
        {"offers": []},
        {
            "offers": [
                {"scheduled_ns": True, "endpoint_id": "endpoint-a", "prompt": "x"}
            ]
        },
        {
            "offers": [
                {
                    "scheduled_ns": 10_000_000_000,
                    "endpoint_id": "endpoint-a",
                    "prompt": "x",
                }
            ]
        },
        {"offers": [{"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": ""}]},
        {
            "offers": [
                {"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": "é" * 32768}
            ]
        },
        {
            "offers": [
                {"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": "\ud800"}
            ]
        },
    ],
)
def test_malformed_config_is_sanitized(change: dict[str, object]) -> None:
    with pytest.raises(EvaluationError) as error:
        load_config_bytes(config_bytes(**change))
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff",
        b'{"bounds":{},"bounds":{}}',
        b"[" * 65 + b"]" * 65,
        b'{"number":1e999}',
        b'{"number":' + b"9" * 257 + b"}",
    ],
)
def test_bounded_lexical_json(content: bytes) -> None:
    with pytest.raises(EvaluationError):
        load_config_bytes(content)


def test_more_than_six_offers_are_separately_validated() -> None:
    config = load_config_bytes(
        config_bytes(
            offers=[
                {"scheduled_ns": i, "endpoint_id": "endpoint-a", "prompt": "x"}
                for i in range(20)
            ]
        )
    )
    assert len(config.offers) == 20
    assert "private-sentinel" not in repr(config)


def test_input_rejects_symlinks_hardlinks_and_fifo(tmp_path: Path) -> None:
    target = tmp_path / "input.json"
    target.write_bytes(config_bytes())
    assert read_input(target) == config_bytes()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_input(link)
    link.unlink()
    os.link(target, link)
    with pytest.raises(OSError):
        read_input(target)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        read_input(fifo)


def test_output_reservation_no_replace_and_failed_run_removal(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    with OutputFile(destination) as output:
        assert destination.stat().st_mode & 0o777 == 0o600
        output.write(b'{"test":true}\n')
    assert destination.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        OutputFile(destination)
    assert destination.read_bytes() == b'{"test":true}\n'
    failed = tmp_path / "failed.json"
    with pytest.raises(RuntimeError), OutputFile(failed):
        raise RuntimeError("abort before output")
    assert not failed.exists()


def test_output_replacement_is_detected_without_deleting_substitute(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "report.json"
    with pytest.raises(OSError), OutputFile(destination) as output:
        destination.rename(tmp_path / "original")
        destination.write_bytes(b"unrelated owner file")
        output.write(b"{}")
    assert destination.read_bytes() == b"unrelated owner file"


def test_cli_rejects_config_or_existing_output_before_sockets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.json"
    config.write_bytes(b'{"secret":"private-sentinel"}')
    destination = tmp_path / "out.json"
    with patch("inferdrome.evaluation.cli.AiohttpTransport") as transport:
        assert main(["run", "--config", str(config), "--output", str(destination)]) == 2
        config.write_bytes(config_bytes())
        destination.write_bytes(b"existing")
        assert main(["run", "--config", str(config), "--output", str(destination)]) == 2
        transport.assert_not_called()
    assert destination.read_bytes() == b"existing"
    assert "private-sentinel" not in capsys.readouterr().err


def test_output_rejects_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        OutputFile(alias / "result.json")
    assert not (real / "result.json").exists()
