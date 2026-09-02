"""Adversarial local-only tests for exact v0.2 boot-disk cleanup."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inferdrome.deployment import gcp_securefs
from inferdrome.deployment.gcp_lifecycle import GcpExecutionLabels
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    GCP_V2_OWNED_BOOT_DISK_SCHEMA_VERSION,
    FileBackedFakeDiskProvider,
    GcpV2DiskCleanupBinding,
    GcpV2DiskCleanupError,
    GcpV2DiskCleanupJournal,
    GcpV2ExactOwnedBootDisk,
    GcpV2ExactOwnedInstance,
    cleanup_exact_owned_boot_disk,
    gcp_v2_disk_delete_request_id,
    issue_gcp_v2_disk_cleanup_binding,
    issue_gcp_v2_owned_boot_disk_inventory,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REQUEST_DIGEST = "sha256:" + "a" * 64


def _labels(*, controller_id: str = "c-12345678") -> GcpExecutionLabels:
    return GcpExecutionLabels(
        inferdrome="inferdrome",
        controller_id=controller_id,
        plan_id="p-12345678",
        arm_id="a-12345678",
        managed_by="inferdrome_gcp_execution_v1",
        role="provider-envelope",
    )


def _disk(*, labels: GcpExecutionLabels | None = None) -> GcpV2ExactOwnedBootDisk:
    return GcpV2ExactOwnedBootDisk(
        schema_version=GCP_V2_OWNED_BOOT_DISK_SCHEMA_VERSION,
        request_digest=REQUEST_DIGEST,
        project_id="inferdrome-example",
        zone="us-central1-a",
        instance_name="inferdrome-ctl-12345678",
        disk_name="inferdrome-ctl-12345678",
        provider_disk_id=987654321,
        self_link=(
            "projects/inferdrome-example/zones/us-central1-a/disks/"
            "inferdrome-ctl-12345678"
        ),
        attached_instance_provider_id=123456790,
        attached_instance_self_link=(
            "projects/inferdrome-example/zones/us-central1-a/instances/"
            "inferdrome-ctl-12345678"
        ),
        boot_device_name="boot",
        boot_source_disk_self_link=(
            "projects/inferdrome-example/zones/us-central1-a/disks/"
            "inferdrome-ctl-12345678"
        ),
        boot_attachment=True,
        labels=labels or _labels(),
        source_image_name=(
            "projects/inferdrome-example/global/images/inferdrome-base-v1"
        ),
        source_image_provider_id=123456789,
        source_image_digest="sha256:" + "b" * 64,
        disk_type="pd-balanced",
        size_gib=100,
        state="PRESENT",
    )


def _binding() -> GcpV2DiskCleanupBinding:
    inventory = issue_gcp_v2_owned_boot_disk_inventory(disk=_disk(), observed_at=NOW)
    instance = GcpV2ExactOwnedInstance(
        schema_version="inferdrome.gcp-owned-instance.v2",
        request_digest=REQUEST_DIGEST,
        project_id="inferdrome-example",
        region="us-central1",
        zone="us-central1-a",
        instance_name="inferdrome-ctl-12345678",
        provider_instance_id=123456790,
        self_link=(
            "projects/inferdrome-example/zones/us-central1-a/instances/"
            "inferdrome-ctl-12345678"
        ),
        labels=_labels(),
        state="RUNNING",
    )
    return issue_gcp_v2_disk_cleanup_binding(
        inventory,
        controller_id="ctl-12345678",
        attached_instance=instance,
    )


def _state_bytes(provider_root: Path) -> bytes:
    return (provider_root / "state.json").read_bytes()


def _journal(tmp_path: Path) -> GcpV2DiskCleanupJournal:
    root = tmp_path / "journal"
    root.mkdir()
    return GcpV2DiskCleanupJournal(root)


def test_disk_cleanup_journal_rejects_symlinked_root_before_reservation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "journal"
    linked_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(GcpV2DiskCleanupError, match="DISK_JOURNAL_UNSAFE"):
        GcpV2DiskCleanupJournal(linked_root).prepare(_binding(), now=NOW)

    assert list(target.iterdir()) == []


def test_disk_cleanup_journal_keeps_operations_on_verified_directory_fd_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "journal"
    root.mkdir()
    moved = tmp_path / "journal-held-by-directory-fd"
    journal = GcpV2DiskCleanupJournal(root)
    binding = _binding()
    event_name = journal._path_for(binding.controller_id)
    original_open_child = gcp_securefs.SafeDirFD.open_child
    swapped = False

    def rename_root_before_event_create(
        directory: gcp_securefs.SafeDirFD,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        nonlocal swapped
        if name == event_name and not swapped:
            swapped = True
            root.rename(moved)
            root.mkdir()
            (root / "replacement-marker").write_text("replacement")
        return original_open_child(directory, name, flags, mode)

    monkeypatch.setattr(
        gcp_securefs.SafeDirFD,
        "open_child",
        rename_root_before_event_create,
    )

    event = journal.prepare(binding, now=NOW)

    assert event.state == "PREPARED"
    assert (moved / event_name).is_file()
    assert not (root / event_name).exists()
    assert (root / "replacement-marker").read_text() == "replacement"


def test_exact_disk_cleanup_handles_instance_gone_disk_remains(tmp_path: Path) -> None:
    binding = _binding()
    provider_root = tmp_path / "provider"
    provider = FileBackedFakeDiskProvider.initialize(
        provider_root, observation=binding.disk
    )
    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
    )

    assert outcome.state == "ABSENCE_CONFIRMED"
    assert outcome.absence is not None
    assert outcome.absence.disk_name == binding.disk.disk_name
    # The fake models the residual-disk case independently of any instance;
    # cleanup never tries to infer a target from an instance hostname.
    assert b'"present":false' in _state_bytes(provider_root)


def test_exact_detached_residual_boot_disk_is_deleted_by_bound_identity(
    tmp_path: Path,
) -> None:
    """Instance absence may detach the bound boot disk without broadening it."""

    binding = _binding()
    detached = GcpV2ExactOwnedBootDisk.model_validate(
        {
            **binding.disk.model_dump(mode="json"),
            "attachment_state": "DETACHED",
            "attached_instance_provider_id": None,
            "attached_instance_self_link": None,
        }
    )
    provider_root = tmp_path / "provider"
    provider = FileBackedFakeDiskProvider.initialize(
        provider_root, observation=detached
    )

    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
    )

    assert outcome.state == "ABSENCE_CONFIRMED"
    assert outcome.absence is not None
    assert outcome.absence.disk_name == binding.disk.disk_name
    assert b'"present":false' in _state_bytes(provider_root)


def test_detached_disk_with_wrong_provider_identity_fails_closed(
    tmp_path: Path,
) -> None:
    binding = _binding()
    wrong = GcpV2ExactOwnedBootDisk.model_validate(
        {
            **binding.disk.model_dump(mode="json"),
            "attachment_state": "DETACHED",
            "attached_instance_provider_id": None,
            "attached_instance_self_link": None,
            "provider_disk_id": 111222333,
        }
    )
    provider_root = tmp_path / "provider"
    provider = FileBackedFakeDiskProvider.initialize(provider_root, observation=wrong)

    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
    )

    assert outcome.state == "BLOCKED"
    assert outcome.error_code == "DISK_OWNERSHIP_MISMATCH"
    assert b'"delete_calls":0' in _state_bytes(provider_root)


def test_lost_delete_response_reuses_exact_request_identity(tmp_path: Path) -> None:
    binding = _binding()
    provider = FileBackedFakeDiskProvider.initialize(
        tmp_path / "provider",
        observation=binding.disk,
        lost_delete_response_once=True,
    )
    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
        max_delete_attempts=3,
        max_reconcile_attempts=3,
    )

    assert outcome.state == "ABSENCE_CONFIRMED"
    assert gcp_v2_disk_delete_request_id(binding) == gcp_v2_disk_delete_request_id(
        binding
    )
    assert outcome.delete_attempts == 1


def test_reconcile_timeout_is_bounded_and_then_confirms_exact_absence(
    tmp_path: Path,
) -> None:
    binding = _binding()
    provider = FileBackedFakeDiskProvider.initialize(
        tmp_path / "provider",
        observation=binding.disk,
        reconciliation_timeouts=1,
    )
    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
        max_reconcile_attempts=3,
    )

    assert outcome.state == "ABSENCE_CONFIRMED"
    assert outcome.delete_attempts == 1


def test_wrong_labels_fail_closed_before_named_disk_delete(tmp_path: Path) -> None:
    binding = _binding()
    wrong = _disk(labels=_labels(controller_id="c-87654321"))
    provider_root = tmp_path / "provider"
    provider = FileBackedFakeDiskProvider.initialize(provider_root, observation=wrong)

    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
    )

    assert outcome.state == "BLOCKED"
    assert outcome.error_code == "DISK_OWNERSHIP_MISMATCH"
    assert b'"delete_calls":0' in _state_bytes(provider_root)


def test_ambiguous_inventory_fails_closed_without_delete(tmp_path: Path) -> None:
    binding = _binding()
    provider_root = tmp_path / "provider"
    provider = FileBackedFakeDiskProvider.initialize(
        provider_root,
        observation=binding.disk,
        ambiguous_inventory=True,
    )

    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=_journal(tmp_path),
        provider=provider,
        now=NOW,
    )

    assert outcome.state == "BLOCKED"
    assert outcome.error_code == "DISK_INVENTORY_AMBIGUOUS"
    assert b'"delete_calls":0' in _state_bytes(provider_root)


def test_restarted_local_process_recovers_durable_exact_disk_cleanup(
    tmp_path: Path,
) -> None:
    """A fresh process can finish an exact cleanup after controller death."""

    binding = _binding()
    provider_root = tmp_path / "provider"
    journal_root = tmp_path / "journal"
    FileBackedFakeDiskProvider.initialize(provider_root, observation=binding.disk)
    journal_root.mkdir()
    binding_path = tmp_path / "binding.json"
    binding_path.write_bytes(binding.model_dump_json().encode("utf-8"))
    source_root = Path(__file__).resolve().parents[2] / "src"
    script = """
from datetime import UTC, datetime
from pathlib import Path
import sys
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    FileBackedFakeDiskProvider,
    GcpV2DiskCleanupBinding,
    GcpV2DiskCleanupJournal,
    cleanup_exact_owned_boot_disk,
)
binding = GcpV2DiskCleanupBinding.model_validate_json(Path(sys.argv[1]).read_bytes())
outcome = cleanup_exact_owned_boot_disk(
    binding,
    journal=GcpV2DiskCleanupJournal(Path(sys.argv[2])),
    provider=FileBackedFakeDiskProvider(Path(sys.argv[3])),
    now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
)
assert outcome.state == "ABSENCE_CONFIRMED"
"""
    environment = {**os.environ, "PYTHONPATH": str(source_root)}

    subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(binding_path),
            str(journal_root),
            str(provider_root),
        ],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )
    outcome = cleanup_exact_owned_boot_disk(
        binding,
        journal=GcpV2DiskCleanupJournal(journal_root),
        provider=FileBackedFakeDiskProvider(provider_root),
        now=NOW,
    )
    assert outcome.state == "ABSENCE_CONFIRMED"
