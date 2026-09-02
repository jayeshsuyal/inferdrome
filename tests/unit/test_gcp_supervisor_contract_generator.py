"""Tests for the additive v0.2 GCP schema generator integrity controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import generate_gcp_supervisor_contracts as generator


def _valid_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }


def test_generated_schema_directory_is_exact_current_and_valid() -> None:
    rendered = generator._render()

    assert rendered
    assert generator._schema_directory_issues(rendered, require_current=True) == ()


def test_generated_supervisor_outputs_include_exact_quote_and_cleanup_result() -> None:
    """Keep two easy-to-omit v2 outputs in the generated contract set."""

    schemas = generator._collect_schemas()
    approval = schemas["gcp-execution-approval.schema.json"]
    approval_properties = approval["properties"]

    assert "read_only_quote_digest" in approval_properties
    assert "rate_basis_digest" in approval_properties
    assert "quote_digest" not in approval_properties
    assert "read_only_quote_digest" in approval["required"]
    assert "quote_digest" not in approval["required"]
    cleanup_result = schemas["gcp-watchdog-cleanup-result.schema.json"]
    assert cleanup_result["$id"] == "urn:inferdrome:gcp-watchdog-cleanup-result:v2"
    assert cleanup_result["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    worker_spec = schemas["gcp-file-watchdog-worker-spec.schema.json"]
    fake_state = schemas["gcp-file-watchdog-fake-cleanup-state.schema.json"]
    assert worker_spec["$id"] == "urn:inferdrome:gcp-file-watchdog-worker-spec:v2"
    assert fake_state["$id"] == "urn:inferdrome:gcp-file-watchdog-fake-cleanup-state:v2"


def test_generator_rejects_duplicate_output_names(monkeypatch) -> None:
    def first() -> dict[str, dict[str, object]]:
        return {"gcp-duplicate.schema.json": _valid_schema()}

    def second() -> dict[str, dict[str, object]]:
        return {"gcp-duplicate.schema.json": _valid_schema()}

    monkeypatch.setattr(generator, "_SCHEMA_PRODUCERS", (first, second))

    with pytest.raises(ValueError, match="duplicate generated schema output"):
        generator._collect_schemas()


def test_generator_rejects_invalid_schema_from_any_producer(monkeypatch) -> None:
    def invalid() -> dict[str, dict[str, object]]:
        return {
            "gcp-invalid.schema.json": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": 7,
            }
        }

    monkeypatch.setattr(generator, "_SCHEMA_PRODUCERS", (invalid,))

    with pytest.raises(ValueError, match="invalid Draft 2020-12 schema"):
        generator._collect_schemas()


def test_generator_rejects_unmanaged_stale_or_unsafe_schema_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schema_root = tmp_path / "schemas/deployment/v2"
    monkeypatch.setattr(generator, "SCHEMA_ROOT", schema_root)
    rendered = generator._render()
    schema_root.mkdir(parents=True)
    for path, content in rendered.items():
        path.write_bytes(content)
    stale = schema_root / "gcp-obsolete.schema.json"
    stale.write_text("{}\n", encoding="utf-8")

    stale_issues = generator._schema_directory_issues(rendered, require_current=True)

    assert any("unmanaged generated schema" in issue for issue in stale_issues)
    stale.unlink()
    unsafe = next(iter(rendered))
    outside_target = tmp_path / "outside-generated-schema"
    outside_target.write_bytes(unsafe.read_bytes())
    unsafe.unlink()
    unsafe.symlink_to(outside_target)
    unsafe_issues = generator._schema_directory_issues(rendered, require_current=True)

    assert any("unsafe generated schema path" in issue for issue in unsafe_issues)


def test_generator_rejects_every_unexpected_direct_or_nested_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_root = tmp_path / "schemas/deployment/v2"
    schema_root.mkdir(parents=True)
    monkeypatch.setattr(generator, "SCHEMA_ROOT", schema_root)
    rendered = generator._render()
    for path, content in rendered.items():
        path.write_bytes(content)
    (schema_root / "not-a-generated-schema.txt").write_text("unexpected\n")
    nested = schema_root / "nested-output"
    nested.mkdir()
    (nested / "gcp-hidden.schema.json").write_text("{}\n")

    issues = generator._schema_directory_issues(rendered, require_current=False)

    assert any("not-a-generated-schema.txt" in issue for issue in issues)
    assert any("nested-output" in issue for issue in issues)


def test_generator_writes_through_no_follow_atomic_staging_and_rejects_linked_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_root = tmp_path / "schemas/deployment/v2"
    schema_root.mkdir(parents=True)
    monkeypatch.setattr(generator, "SCHEMA_ROOT", schema_root)
    rendered = generator._render()

    generator._write_schemas(rendered)

    assert generator._schema_directory_issues(rendered, require_current=True) == ()
    target = next(iter(rendered))
    outside = tmp_path / "outside-generated-schema"
    outside.write_text("outside\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe generated schema path"):
        generator._write_schemas(rendered)

    assert outside.read_text(encoding="utf-8") == "outside\n"
