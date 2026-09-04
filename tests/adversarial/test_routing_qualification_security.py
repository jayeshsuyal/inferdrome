"""Descriptor-only adversarial coverage for the v0.3 qualification overlay."""

from __future__ import annotations

import ast
import os
import shutil
import stat
from pathlib import Path

import pytest

from inferdrome.routing_campaign import run_campaign
from inferdrome.routing_qualification.qualification import (
    MAX_QUALIFICATION_BYTES,
    QUALIFICATION_ARTIFACT_ID,
    StaleTelemetryQualificationError,
    _read_published_descriptor,
    capture_qualification,
    publish_qualification,
    verify_qualification,
)

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def _run_source(output: Path):
    return run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output,
    )


def _published(tmp_path: Path):
    source = _run_source(tmp_path / "campaign")
    captured = capture_qualification(source.path)
    output_root = tmp_path / "qualification"
    sealed = publish_qualification(
        captured,
        campaign_package=source.path,
        output_root=output_root,
    )
    return source, captured, output_root, sealed


def _make_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def test_descriptor_reader_rejects_symlink_hardlink_fifo_and_oversize(
    tmp_path: Path,
) -> None:
    source, captured, output_root, sealed = _published(tmp_path)
    try:
        _make_writable(sealed.path)
        link = output_root / "replacement-link"
        link.symlink_to(sealed.path, target_is_directory=True)
        shutil.rmtree(sealed.path)
        (output_root / QUALIFICATION_ARTIFACT_ID).symlink_to(
            link,
            target_is_directory=True,
        )
        with pytest.raises(StaleTelemetryQualificationError, match="not immutable"):
            verify_qualification(
                output_root,
                campaign_package=source.path,
                expected_descriptor_digest=captured.retained_digest,
            )
    finally:
        if (output_root / QUALIFICATION_ARTIFACT_ID).is_symlink():
            (output_root / QUALIFICATION_ARTIFACT_ID).unlink()

    _, captured, output_root, sealed = _published(tmp_path / "hardlink")
    try:
        _make_writable(sealed.path)
        os.link(sealed.descriptor_path, output_root / "descriptor-copy")
        sealed.descriptor_path.chmod(0o400)
        sealed.path.chmod(0o500)
        with pytest.raises(StaleTelemetryQualificationError, match="unsafe"):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)

    _, _, output_root, sealed = _published(tmp_path / "extra-entry")
    try:
        _make_writable(sealed.path)
        sealed.path.joinpath("unexpected.json").write_text("{}", encoding="utf-8")
        sealed.path.chmod(0o500)
        with pytest.raises(StaleTelemetryQualificationError, match="not closed"):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)

    _, captured, output_root, sealed = _published(tmp_path / "fifo")
    try:
        _make_writable(sealed.path)
        sealed.descriptor_path.unlink()
        os.mkfifo(sealed.descriptor_path)
        sealed.path.chmod(0o500)
        with pytest.raises(StaleTelemetryQualificationError, match="unsafe"):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)

    _, captured, output_root, sealed = _published(tmp_path / "oversize")
    try:
        _make_writable(sealed.path)
        sealed.descriptor_path.write_bytes(b"x" * (MAX_QUALIFICATION_BYTES + 1))
        sealed.descriptor_path.chmod(0o400)
        sealed.path.chmod(0o500)
        with pytest.raises(StaleTelemetryQualificationError, match="unsafe"):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)


@pytest.mark.parametrize("flag", ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
@pytest.mark.parametrize("state", ("missing", "zero"))
def test_descriptor_reader_fails_before_open_without_required_safe_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    state: str,
) -> None:
    _, _, output_root, _ = _published(tmp_path)
    import inferdrome.routing_qualification.qualification as qualification

    opened = 0

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        nonlocal opened
        opened += 1
        raise AssertionError("unsafe descriptor reader reached os.open")

    with monkeypatch.context() as context:
        if state == "missing":
            context.delattr(qualification.os, flag, raising=False)
        else:
            context.setattr(qualification.os, flag, 0, raising=False)
        context.setattr(qualification.os, "open", forbidden_open)
        with pytest.raises(StaleTelemetryQualificationError, match=flag):
            _read_published_descriptor(output_root)
    assert opened == 0


def test_descriptor_reader_rejects_same_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, output_root, sealed = _published(tmp_path)
    import inferdrome.routing_qualification.qualification as qualification

    original_read = qualification.os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced:
            replaced = True
            _make_writable(sealed.path)
            replacement = sealed.path / "replacement"
            replacement.write_bytes(sealed.descriptor_path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, sealed.descriptor_path)
            sealed.descriptor_path.chmod(0o400)
            sealed.path.chmod(0o500)
        return chunk

    monkeypatch.setattr(qualification.os, "read", replace_after_read)
    try:
        with pytest.raises(StaleTelemetryQualificationError, match="changed during"):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)


def test_descriptor_reader_and_publisher_reject_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    source, captured, output_root, _ = _published(tmp_path)
    symlinked_ancestor = tmp_path / "symlinked-ancestor"
    symlinked_ancestor.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(StaleTelemetryQualificationError, match="unavailable"):
        verify_qualification(
            symlinked_ancestor / output_root.name,
            campaign_package=source.path,
            expected_descriptor_digest=captured.retained_digest,
        )
    with pytest.raises(StaleTelemetryQualificationError, match="unavailable"):
        publish_qualification(
            captured,
            campaign_package=source.path,
            output_root=symlinked_ancestor / "new-qualification",
        )
    assert not (tmp_path / "new-qualification").exists()


def test_descriptor_reader_rejects_artifact_directory_swap_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, captured, output_root, sealed = _published(tmp_path)
    import inferdrome.routing_qualification.qualification as qualification

    original_read = qualification.os.read
    swapped = False

    def swap_artifact_after_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, size)
        if not swapped:
            swapped = True
            replacement = tmp_path / "replacement-artifact"
            replacement.mkdir(mode=0o700)
            replacement_descriptor = replacement / sealed.descriptor_path.name
            replacement_descriptor.write_bytes(captured.canonical_bytes)
            replacement_descriptor.chmod(0o400)
            original = output_root / "original-artifact"
            # Darwin requires the source directory itself to be writable for
            # this adversarial rename. The descriptor remains immutable.
            sealed.path.chmod(0o700)
            os.replace(sealed.path, original)
            os.replace(replacement, sealed.path)
            sealed.path.chmod(0o500)
        return chunk

    monkeypatch.setattr(qualification.os, "read", swap_artifact_after_read)
    try:
        with pytest.raises(
            StaleTelemetryQualificationError,
            match="changed during reading",
        ):
            _read_published_descriptor(output_root)
    finally:
        _make_writable(output_root)


def test_publisher_holds_its_output_root_across_an_ancestor_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication cannot be redirected after source verification succeeds."""

    source = _run_source(tmp_path / "campaign")
    captured = capture_qualification(source.path)
    parent = tmp_path / "safe-parent"
    parent.mkdir(mode=0o700)
    output_root = parent / "qualification"
    displaced_parent = tmp_path / "displaced-parent"
    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.mkdir(mode=0o700)
    import inferdrome.routing_qualification.qualification as qualification

    original_rename = qualification._rename_no_replace_at
    swapped = False

    def swap_ancestor_before_publish(
        held_root: object,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        assert not swapped
        swapped = True
        parent.rename(displaced_parent)
        parent.symlink_to(redirected_parent, target_is_directory=True)
        original_rename(held_root, source_name, destination_name)

    monkeypatch.setattr(
        qualification,
        "_rename_no_replace_at",
        swap_ancestor_before_publish,
    )
    try:
        with pytest.raises(
            StaleTelemetryQualificationError,
            match="output root is unavailable",
        ):
            publish_qualification(
                captured,
                campaign_package=source.path,
                output_root=output_root,
            )
        assert swapped
        assert not (
            redirected_parent / "qualification" / QUALIFICATION_ARTIFACT_ID
        ).exists()
        assert (
            displaced_parent / "qualification" / QUALIFICATION_ARTIFACT_ID
        ).is_dir()
    finally:
        if parent.is_symlink():
            parent.unlink()
        if displaced_parent.exists():
            _make_writable(displaced_parent)


def test_publisher_bootstraps_a_private_root_below_a_public_temp_parent(
    tmp_path: Path,
) -> None:
    """A public ancestor is never trusted, but can contain a new private root."""

    source = _run_source(tmp_path / "campaign")
    captured = capture_qualification(source.path)
    public_parent = tmp_path / "public-temp-parent"
    public_parent.mkdir(mode=0o700)
    public_parent.chmod(0o1777)
    output_root = public_parent / "qualification"
    try:
        sealed = publish_qualification(
            captured,
            campaign_package=source.path,
            output_root=output_root,
        )
        assert stat.S_IMODE(output_root.stat().st_mode) == 0o700
        assert verify_qualification(
            output_root,
            campaign_package=source.path,
            expected_descriptor_digest=sealed.retained_digest,
        ) == captured
    finally:
        _make_writable(public_parent)


def test_qualification_namespace_imports_no_network_model_or_provider_surface() -> None:
    package = _ROOT / "src" / "inferdrome" / "routing_qualification"
    prohibited = {
        "aiohttp",
        "boto3",
        "docker",
        "google",
        "http",
        "requests",
        "socket",
        "subprocess",
        "transformers",
        "vllm",
    }
    imports: set[str] = set()
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
    assert not imports & prohibited
