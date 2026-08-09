"""Crash-safe publication of immutable descriptor directories."""

import errno
import os
import stat
from pathlib import Path

import pytest

import inferdrome.immutable as immutable


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def test_publication_freezes_one_complete_descriptor_directory(
    tmp_path: Path,
) -> None:
    try:
        root = tmp_path / "artifacts"
        published = immutable.publish_immutable_directory(
            root=root,
            artifact_id="artifact-11111111111111111111111111111111",
            filename="descriptor.json",
            content=b'{"complete":true}\n',
        )

        descriptor = published / "descriptor.json"
        assert descriptor.read_bytes() == b'{"complete":true}\n'
        assert stat.S_IMODE(os.lstat(published).st_mode) == 0o500
        assert stat.S_IMODE(os.lstat(descriptor).st_mode) == 0o400
        assert sorted(path.name for path in published.iterdir()) == [
            "descriptor.json"
        ]
    finally:
        _make_tree_writable(tmp_path)


def test_existing_artifact_is_never_replaced(
    tmp_path: Path,
) -> None:
    try:
        root = tmp_path / "artifacts"
        artifact_id = "artifact-22222222222222222222222222222222"
        published = immutable.publish_immutable_directory(
            root=root,
            artifact_id=artifact_id,
            filename="descriptor.json",
            content=b'{"version":1}\n',
        )

        with pytest.raises(FileExistsError):
            immutable.publish_immutable_directory(
                root=root,
                artifact_id=artifact_id,
                filename="descriptor.json",
                content=b'{"version":2}\n',
            )

        assert published.joinpath("descriptor.json").read_bytes() == (
            b'{"version":1}\n'
        )
    finally:
        _make_tree_writable(tmp_path)


def test_failed_private_stage_cannot_poison_public_identity_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        root = tmp_path / "artifacts"
        artifact_id = "artifact-33333333333333333333333333333333"
        destination = root / artifact_id

        with monkeypatch.context() as context:
            context.setattr(
                immutable,
                "_rename_no_replace",
                lambda _source, _destination: (_ for _ in ()).throw(
                    OSError(errno.EIO, "injected publication failure")
                ),
            )
            with pytest.raises(OSError, match="injected publication failure"):
                immutable.publish_immutable_directory(
                    root=root,
                    artifact_id=artifact_id,
                    filename="descriptor.json",
                    content=b'{"complete":true}\n',
                )

        assert not destination.exists()
        orphan_stages = tuple(
            path
            for path in root.iterdir()
            if path.name.startswith(immutable.STAGING_PREFIX)
        )
        assert len(orphan_stages) == 1
        assert immutable.is_internal_staging_entry(orphan_stages[0].name)

        published = immutable.publish_immutable_directory(
            root=root,
            artifact_id=artifact_id,
            filename="descriptor.json",
            content=b'{"complete":true}\n',
        )

        assert published == destination
        assert published.joinpath("descriptor.json").read_bytes() == (
            b'{"complete":true}\n'
        )
    finally:
        _make_tree_writable(tmp_path)


@pytest.mark.parametrize(
    ("artifact_id", "filename"),
    [
        ("../escape", "descriptor.json"),
        ("artifact-44444444444444444444444444444444", "../descriptor.json"),
        ("", "descriptor.json"),
    ],
)
def test_publication_rejects_unsafe_path_components(
    tmp_path: Path,
    artifact_id: str,
    filename: str,
) -> None:
    with pytest.raises(ValueError, match="input is invalid"):
        immutable.publish_immutable_directory(
            root=tmp_path / "artifacts",
            artifact_id=artifact_id,
            filename=filename,
            content=b"{}\n",
        )
