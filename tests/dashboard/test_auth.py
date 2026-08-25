"""Adversarial contracts for the opt-in local dashboard authentication boundary."""

import hmac
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.cli import main
from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.auth import (
    KEYRING_SCHEMA_VERSION,
    MAX_KEYRING_BYTES,
    DashboardKeyringStore,
    validate_token_shape,
)
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.errors import DashboardAuthError


def _store(tmp_path: Path) -> tuple[DashboardKeyringStore, str, str]:
    path = tmp_path / "dashboard-keyring.json"
    store = DashboardKeyringStore(path)
    token, record = store.create("test-key")
    return store, token, record.key_id


def test_create_persists_only_digest_with_private_canonical_keyring(
    tmp_path: Path,
) -> None:
    store, token, key_id = _store(tmp_path)
    raw = store.path.read_bytes()

    assert validate_token_shape(token)
    assert len(token.encode("ascii")) >= 256 // 6
    assert token.encode("ascii") not in raw
    assert b"sha256:" in raw
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert json.loads(raw)["schema_version"] == KEYRING_SCHEMA_VERSION
    assert store.list_public()[0]["key_id"] == key_id
    assert "token_digest" not in json.dumps(store.list_public())


def test_unknown_id_and_malformed_tokens_fail_without_disclosure(
    tmp_path: Path,
) -> None:
    store, token, _ = _store(tmp_path)
    version, _, secret = token.split(".")
    unknown_id = f"{version}.dk-ffffffffffffffff.{secret}"
    candidates = (
        unknown_id,
        token + "x",
        "Basic " + token,
        "sk-" + "A" * 60,
        "",
    )

    assert store.verify(token)
    assert all(not store.verify(candidate) for candidate in candidates)
    assert not validate_token_shape(token + "\n")


def test_unknown_key_id_still_runs_dummy_constant_time_comparison(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store, token, _ = _store(tmp_path)
    version, _, secret = token.split(".")
    unknown = f"{version}.dk-ffffffffffffffff.{secret}"
    original_compare = hmac.compare_digest
    comparisons: list[tuple[str, str]] = []

    def observe(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", observe)
    assert not store.verify(unknown)
    assert len(comparisons) == 1
    assert len(comparisons[0][0]) == len(comparisons[0][1]) == 64


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.replace(
            '"schema_version":"inferdrome.dashboard-keyring.v1"',
            '"schema_version":"inferdrome.dashboard-keyring.v1",'
            '"schema_version":"inferdrome.dashboard-keyring.v1"',
        ),
        lambda value: value.replace('"keys":[', '"keys":[],"keys":['),
        lambda value: value.replace(
            '"status":"active"', '"status":"active","unexpected":true'
        ),
    ],
)
def test_duplicate_and_unknown_keyring_fields_reject(
    tmp_path: Path,
    mutator: Any,
) -> None:
    store, _, _ = _store(tmp_path)
    raw = store.path.read_text()
    store.path.write_text(mutator(raw))
    os.chmod(store.path, 0o600)

    with pytest.raises(DashboardAuthError, match="unavailable"):
        store.load()


def test_keyring_rejects_noncanonical_oversized_and_unsafe_files(
    tmp_path: Path,
) -> None:
    store, _, _ = _store(tmp_path)
    store.path.write_bytes(b"{" + b" " * MAX_KEYRING_BYTES)
    os.chmod(store.path, 0o600)
    with pytest.raises(DashboardAuthError):
        store.load()

    os.unlink(store.path)
    store.path.symlink_to(tmp_path / "other")
    with pytest.raises(DashboardAuthError):
        store.load()

    store.path.unlink()
    store.path.write_text("{}")
    os.chmod(store.path, 0o644)
    with pytest.raises(DashboardAuthError):
        store.load()


def test_revoke_is_explicitly_idempotent_and_rotation_is_atomic(
    tmp_path: Path,
) -> None:
    store, token, old_id = _store(tmp_path)
    new_token, new_record = store.rotate(old_id, "replacement")
    assert new_record.status == "active"
    assert not store.verify(token)
    assert store.verify(new_token)
    old_record = store.revoke(old_id)
    assert old_record.status == "revoked"
    revoked_again = store.revoke(old_id)
    assert revoked_again.status == "revoked"


def test_auth_is_opt_in_and_protects_evidence_routes_only(tmp_path: Path) -> None:
    store, token, _ = _store(tmp_path)
    static_root = tmp_path / "static"
    (static_root / "assets").mkdir(parents=True)
    (static_root / "index.html").write_text("<html>unlock</html>")
    (static_root / "assets" / "app.js").write_text("export {};")
    app = create_app(
        DashboardIndex(tmp_path / "runs"),
        static_dir=static_root,
        keyring_path=store.path,
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        shell = client.get("/")
        asset = client.get("/assets/app.js")
        missing = client.get("/api/v1/runs")
        basic = client.get("/api/v1/runs", headers={"Authorization": "Basic abc"})
        query = client.get("/api/v1/runs", params={"token": token})
        cookie = client.get("/api/v1/runs", cookies={"dashboard_token": token})
        whitespace = client.get(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer  {token}"},
        )
        valid = client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"})

    assert health.status_code == 200
    assert shell.status_code == asset.status_code == 200
    assert missing.status_code == basic.status_code == query.status_code == 401
    assert cookie.status_code == whitespace.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json() == {"detail": "dashboard authentication failed"}
    assert valid.status_code == 200
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["x-content-type-options"] == "nosniff"


def test_auth_rejects_duplicate_authorization_and_oversized_tokens(
    tmp_path: Path,
) -> None:
    store, token, _ = _store(tmp_path)
    app = create_app(DashboardIndex(tmp_path / "runs"), keyring_path=store.path)
    with TestClient(app) as client:
        duplicate = client.request(
            "GET",
            "/api/v1/runs",
            headers=[
                ("authorization", f"Bearer {token}"),
                ("authorization", f"Bearer {token}"),
            ],
        )
        oversized = client.get(
            "/api/v1/runs",
            headers={"Authorization": "Bearer " + "a" * 513},
        )

    assert duplicate.status_code == 401
    assert oversized.status_code == 401
    assert duplicate.headers["www-authenticate"] == "Bearer"


def test_keyring_live_reload_revocation_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    store, token, key_id = _store(tmp_path)
    app = create_app(DashboardIndex(tmp_path / "runs"), keyring_path=store.path)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/runs", headers=headers).status_code == 200
        store.revoke(key_id)
        assert client.get("/api/v1/runs", headers=headers).status_code == 401
        store.path.write_text("not-json")
        os.chmod(store.path, 0o600)
        unavailable = client.get(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "dashboard authentication data unavailable"}
    assert token not in unavailable.text


def test_startup_requires_valid_keyring_with_active_read_key(tmp_path: Path) -> None:
    path = tmp_path / "missing-keyring.json"
    with pytest.raises(DashboardAuthError):
        create_app(DashboardIndex(tmp_path / "runs"), keyring_path=path)


def test_openapi_advertises_bearer_only_when_auth_is_enabled(tmp_path: Path) -> None:
    unauthenticated = create_app(DashboardIndex(tmp_path / "runs")).openapi()
    store, _, _ = _store(tmp_path)
    authenticated = create_app(
        DashboardIndex(tmp_path / "auth-runs"),
        keyring_path=store.path,
    ).openapi()

    assert "securitySchemes" not in unauthenticated["components"]
    schemes = authenticated["components"]["securitySchemes"]
    assert schemes["DashboardBearer"] == {"type": "http", "scheme": "bearer"}
    assert authenticated["paths"]["/api/v1/runs"]["get"]["security"] == [
        {"DashboardBearer": []}
    ]
    assert "security" not in authenticated["paths"]["/api/v1/health"]["get"]


def test_cli_keyring_lifecycle_prints_new_secrets_only_once(
    tmp_path: Path,
    capsys: Any,
) -> None:
    path = tmp_path / "cli-keyring.json"
    create_args = [
        "dashboard-keyring",
        "create",
        "--keyring",
        str(path),
        "--label",
        "cli-test",
    ]
    assert main(create_args) == 0
    created_output = capsys.readouterr().out
    created = json.loads(created_output)
    token = created["token"]
    old_id = created["key_id"]
    assert created_output.count(token) == 1

    assert main(["dashboard-keyring", "list", "--keyring", str(path)]) == 0
    listed_output = capsys.readouterr().out
    assert token not in listed_output
    assert "token_digest" not in listed_output
    assert old_id in listed_output

    assert main(
        [
            "dashboard-keyring",
            "rotate",
            "--keyring",
            str(path),
            "--revoke-key-id",
            old_id,
        ]
    ) == 0
    rotated_output = capsys.readouterr().out
    rotated = json.loads(rotated_output)
    assert rotated_output.count(rotated["token"]) == 1
    assert token not in rotated_output

    assert main(
        ["dashboard-keyring", "revoke", "--keyring", str(path), rotated["key_id"]]
    ) == 0
    revoked_output = capsys.readouterr().out
    assert "token" not in revoked_output
    assert "sha256:" not in revoked_output


def test_invalid_secret_shaped_cli_label_is_bounded_and_non_disclosing(
    tmp_path: Path,
    capsys: Any,
) -> None:
    secret_shape = "sk-" + "A" * 64
    result = main(
        [
            "dashboard-keyring",
            "create",
            "--keyring",
            str(tmp_path / "rejected-keyring.json"),
            "--label",
            secret_shape,
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert secret_shape not in captured.out
    assert secret_shape not in captured.err
    assert "traceback" not in captured.err.lower()
