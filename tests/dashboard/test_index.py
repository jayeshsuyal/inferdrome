"""Behavioral contract for safe dashboard run discovery."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import inferdrome.dashboard.index as index_module
import inferdrome.dashboard.projection as projection_module
from inferdrome.bundle import recalculate_bundle as authoritative_recalculate
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.errors import DashboardRunNotFound


def _status_value(entry: Any) -> str:
    status = entry.status
    return status.value if hasattr(status, "value") else str(status)


def test_discovers_complete_workspace_and_direct_sealed_bundle(
    sealed_fake_bundle: Any,
    tmp_path: Path,
    copy_sealed_bundle: Callable[[Path, Path], Path],
) -> None:
    workspace_index = DashboardIndex(sealed_fake_bundle.workspace.path.parent)
    workspace_snapshot = workspace_index.refresh()
    workspace_detail = workspace_index.get_run(sealed_fake_bundle.workspace.run_id)

    assert [item.run_id for item in workspace_snapshot.runs] == [
        sealed_fake_bundle.workspace.run_id
    ]
    assert workspace_snapshot.rejected == ()
    assert workspace_detail.summary.bundle_digest == (
        sealed_fake_bundle.sealed.bundle_digest
    )

    imports_root = tmp_path / "direct-bundles"
    imports_root.mkdir()
    direct_bundle = copy_sealed_bundle(
        sealed_fake_bundle.sealed.path,
        imports_root / "evidence-import",
    )
    direct_index = DashboardIndex(imports_root)
    direct_snapshot = direct_index.refresh()
    direct_detail = direct_index.get_run(sealed_fake_bundle.workspace.run_id)

    assert [item.run_id for item in direct_snapshot.runs] == [
        sealed_fake_bundle.workspace.run_id
    ]
    assert direct_snapshot.rejected == ()
    assert direct_detail.summary.bundle_digest == (
        sealed_fake_bundle.sealed.bundle_digest
    )
    assert direct_bundle.is_dir()


def test_discovery_does_not_recurse_below_direct_children(
    sealed_fake_bundle: Any,
    tmp_path: Path,
    copy_sealed_bundle: Callable[[Path, Path], Path],
) -> None:
    runs_root = tmp_path / "shallow-runs"
    nested_group = runs_root / "group"
    nested_group.mkdir(parents=True)
    copy_sealed_bundle(
        sealed_fake_bundle.sealed.path,
        nested_group / "nested-evidence",
    )
    (runs_root / "not-a-run.txt").write_text("ignored", encoding="utf-8")

    index = DashboardIndex(runs_root)
    snapshot = index.refresh()

    assert snapshot.runs == ()
    assert snapshot.rejected == ()
    with pytest.raises(DashboardRunNotFound):
        index.get_run(sealed_fake_bundle.workspace.run_id)


def test_every_valid_candidate_is_recalculated_before_indexing(
    sealed_fake_bundle: Any,
    sealed_vllm_bundle: Any,
    monkeypatch: Any,
) -> None:
    observed: list[Path] = []

    def recalculate_spy(bundle_path: Path, **kwargs: object) -> object:
        observed.append(Path(bundle_path))
        return authoritative_recalculate(bundle_path, **kwargs)

    monkeypatch.setattr(projection_module, "recalculate_bundle", recalculate_spy)

    runs_root = sealed_fake_bundle.workspace.path.parent
    snapshot = DashboardIndex(runs_root).refresh()

    assert {entry.run_id for entry in snapshot.runs} == {
        sealed_fake_bundle.workspace.run_id,
        sealed_vllm_bundle.workspace.run_id,
    }
    assert set(observed) == {
        sealed_fake_bundle.sealed.path,
        sealed_vllm_bundle.sealed.path,
    }


def test_limit_one_cursor_pages_reuse_one_verified_run_snapshot(
    sealed_fake_bundle: Any,
    sealed_vllm_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_calls = 0
    authoritative_verify = index_module.verify_bundle

    def verify_spy(*args: object, **kwargs: object) -> object:
        nonlocal verification_calls
        verification_calls += 1
        return authoritative_verify(*args, **kwargs)

    monkeypatch.setattr(index_module, "verify_bundle", verify_spy)
    index = DashboardIndex(sealed_fake_bundle.workspace.path.parent)

    first = index.refresh(limit=1)
    assert first.page.next_cursor is not None
    assert verification_calls == 2
    second = index.refresh(cursor=first.page.next_cursor, limit=1)

    assert verification_calls == 2
    assert second.generated_at == first.generated_at
    assert second.page.total == first.page.total == 2
    assert {
        item.run_id for page in (first, second) for item in page.runs
    } == {
        sealed_fake_bundle.workspace.run_id,
        sealed_vllm_bundle.workspace.run_id,
    }


def test_tampered_entry_has_only_a_bounded_generic_public_error(
    sealed_fake_bundle: Any,
    tmp_path: Path,
    tampered_bundle: Callable[[Path, Path], Path],
    as_public_json: Callable[[object], Any],
) -> None:
    runs_root = tmp_path / "tampered-runs"
    runs_root.mkdir()
    tampered_path = tampered_bundle(
        sealed_fake_bundle.sealed.path,
        runs_root / "tampered-evidence",
    )

    snapshot = DashboardIndex(runs_root).refresh()

    assert snapshot.runs == ()
    assert len(snapshot.rejected) == 1
    rejected = snapshot.rejected[0]
    assert _status_value(rejected) == "REJECTED"
    assert rejected.code == "VERIFICATION_FAILED"
    assert 0 < len(rejected.message) <= 160
    assert str(tampered_path) not in rejected.message
    assert "artifact hash does not match manifest" not in rejected.message.lower()

    public_text = json.dumps(as_public_json(rejected), sort_keys=True).lower()
    assert "measurements" not in public_text
    assert "distributions" not in public_text


def test_lookup_resolves_only_run_ids_present_in_the_index(
    sealed_fake_bundle: Any,
) -> None:
    index = DashboardIndex(sealed_fake_bundle.workspace.path.parent)
    index.refresh()

    assert index.get_run(sealed_fake_bundle.workspace.run_id).summary.run_id == (
        sealed_fake_bundle.workspace.run_id
    )
    for unindexed_id in (
        "run-ffffffffffffffffffffffffffffffff",
        "../../outside",
        str(sealed_fake_bundle.sealed.path),
    ):
        with pytest.raises(DashboardRunNotFound):
            index.get_run(unindexed_id)
