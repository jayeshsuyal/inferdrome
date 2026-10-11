"""A retrieved private archive must match both its retained hash and inventory."""

from __future__ import annotations

import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from inferdrome import breakpoint_pilot_export as export


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    prepared, collection = tmp_path / "prepared", tmp_path / "collection"
    prepared.mkdir(mode=0o700)
    collection.mkdir(mode=0o700)
    (prepared / "inventory.json").write_text('{"status":"PREPARED_NOT_EXECUTED"}')
    (collection / "session-status.json").write_text('{"status":"FAILED"}')
    (collection / "partial-result.json").write_text('{"requests":1}')
    return prepared, collection


def test_roundtrip_failed_partial_run_and_retrieved_copy(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    archive = tmp_path / "private.zip"
    receipt = export.pack(*inputs, archive)
    retrieved = tmp_path / "retrieved.zip"
    retrieved.write_bytes(archive.read_bytes())
    assert export.verify_archive(retrieved, receipt["archive_sha256"]) == receipt
    assert receipt["payload_files"] == 3
    assert receipt["evidence_eligible"] is False
    assert archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        export.pack(*inputs, archive)


def test_external_hash_detects_corrupted_transfer(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    archive = tmp_path / "private.zip"
    receipt = export.pack(*inputs, archive)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="mismatch"):
        export.verify_archive(archive, receipt["archive_sha256"])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory_link"])
def test_rejects_non_owned_tree_members(
    inputs: tuple[Path, Path], tmp_path: Path, kind: str
) -> None:
    prepared, collection = inputs
    target = collection / "unexpected"
    if kind == "symlink":
        target.symlink_to(prepared / "inventory.json")
    elif kind == "hardlink":
        os.link(prepared / "inventory.json", target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(prepared, target_is_directory=True)
    with pytest.raises(ValueError):
        export.pack(*inputs, tmp_path / "archive.zip")


def test_requires_terminal_record_and_external_output(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="outside"):
        export.pack(*inputs, inputs[1] / "archive.zip")
    (inputs[1] / "session-status.json").unlink()
    with pytest.raises(ValueError, match="terminal"):
        export.pack(*inputs, tmp_path / "archive.zip")


def test_rejects_changed_payload_even_if_transport_hash_recomputed(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    original = tmp_path / "original.zip"
    export.pack(*inputs, original)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w") as sink:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "collection/partial-result.json":
                data = b'{"requests":2}'
            sink.writestr(item, data)
    with pytest.raises(ValueError, match="payload hash"):
        export.verify_archive(changed, export.archive_digest(changed))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a//b", "a/./b", "a\\b"])
def test_archive_paths_never_escape(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive, "w") as sink:
        for filename in (name, "export-inventory.json"):
            info = zipfile.ZipInfo(filename)
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            sink.writestr(info, b"{}")
    with pytest.raises(ValueError, match="relative POSIX"):
        export.verify_archive(archive, export.archive_digest(archive))


def test_detects_unlisted_extra_member(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    archive = tmp_path / "archive.zip"
    export.pack(*inputs, archive)
    with zipfile.ZipFile(archive, "a") as sink:
        info = zipfile.ZipInfo("collection/extra.json")
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        sink.writestr(info, b"{}")
    with pytest.raises(ValueError, match="additional"):
        export.verify_archive(archive, export.archive_digest(archive))


def test_inventory_must_account_for_exact_bytes(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    original = tmp_path / "original.zip"
    export.pack(*inputs, original)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w") as sink:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "export-inventory.json":
                inventory = json.loads(data)
                inventory["files"][0]["bytes"] = True
                data = json.dumps(inventory).encode()
            sink.writestr(item, data)
    with pytest.raises(ValueError, match="size mismatch"):
        export.verify_archive(changed, export.archive_digest(changed))


def test_size_limits_apply_before_reading(
    inputs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(ValueError, match="total byte"):
        export.pack(*inputs, tmp_path / "archive.zip")


@pytest.mark.parametrize(
    "status", ["COMPLETED", "FAILED", "INTERRUPTED", "CLEANUP_UNCONFIRMED"]
)
def test_all_terminal_states_can_export_partial_private_evidence(
    inputs: tuple[Path, Path], tmp_path: Path, status: str
) -> None:
    (inputs[1] / "session-status.json").write_text(json.dumps({"status": status}))
    assert export.pack(*inputs, tmp_path / "archive.zip")["evidence_eligible"] is False


@pytest.mark.parametrize(
    "record",
    [
        '{"status":"RUNNING"}',
        '{"status":"INCOMPLETE"}',
        '{"status":true}',
        "{}",
        "[]",
        '{"status":"RUNNING","status":"FAILED"}',
    ],
)
def test_refuses_unfinished_or_ambiguous_terminal_record_before_output(
    inputs: tuple[Path, Path], tmp_path: Path, record: str
) -> None:
    (inputs[1] / "session-status.json").write_text(record)
    archive = tmp_path / "archive.zip"
    with pytest.raises(ValueError, match="terminal"):
        export.pack(*inputs, archive)
    assert not archive.exists()


def test_inventory_rejects_duplicate_keys_even_with_matching_external_hash(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    original, changed = tmp_path / "original.zip", tmp_path / "changed.zip"
    export.pack(*inputs, original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w") as sink:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "export-inventory.json":
                data = b'{"scope":"AMBIGUOUS",' + data[1:]
            sink.writestr(item, data)
    with pytest.raises(ValueError, match="ambiguous"):
        export.verify_archive(changed, export.archive_digest(changed))


def test_stored_zip_must_have_matching_compressed_and_uncompressed_sizes(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    archive = tmp_path / "archive.zip"
    export.pack(*inputs, archive)
    data = bytearray(archive.read_bytes())
    central = data.index(b"PK\x01\x02")
    size = struct.unpack_from("<I", data, central + 20)[0]
    struct.pack_into("<I", data, central + 20, size + 1)
    archive.write_bytes(data)
    with pytest.raises(ValueError, match="unsupported"):
        export.verify_archive(archive, export.archive_digest(archive))


def test_dot_segments_cannot_put_archive_inside_input(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    alias = inputs[0] / ".." / "collection"
    archive = inputs[1] / "archive.zip"
    with pytest.raises(ValueError, match="outside"):
        export.pack(inputs[0], alias, archive)
    assert not archive.exists()


def test_changed_payload_during_pack_never_returns_success_receipt(
    inputs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = export._read
    changed = False

    def change_after_read(path: Path, limit: int = export.MAX_FILE_BYTES) -> bytes:
        nonlocal changed
        data = read(path, limit)
        if path.name == "partial-result.json" and not changed:
            path.write_bytes(b'{"requests":2}')
            changed = True
        return data

    monkeypatch.setattr(export, "_read", change_after_read)
    with pytest.raises(ValueError, match="changed during packaging"):
        export.pack(*inputs, tmp_path / "archive.zip")
