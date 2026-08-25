"""Adversarial contracts for the opt-in local dashboard authentication boundary."""

import hmac
import json
import multiprocessing as mp
import os
import subprocess
import sys
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
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import DashboardAuthError


def _store(tmp_path: Path) -> tuple[DashboardKeyringStore, str, str]:
    path = tmp_path / "dashboard-keyring.json"
    store = DashboardKeyringStore(path)
    token, record = store.create("test-key")
    return store, token, record.key_id


def _create_in_process(path: str, output: Any) -> None:
    try:
        token, record = DashboardKeyringStore(Path(path)).create("process-worker")
        output.put(("ok", token, record.key_id))
    except Exception:
        output.put(("error",))


def _revoke_or_rotate_in_process(
    path: str,
    key_id: str,
    operation: str,
    start: Any,
    output: Any,
) -> None:
    start.wait()
    try:
        store = DashboardKeyringStore(Path(path))
        if operation == "revoke":
            store.revoke(key_id)
        else:
            store.rotate(key_id, "process-rotation")
        output.put((operation, "ok"))
    except DashboardAuthError:
        output.put((operation, "rejected"))
    except Exception:
        output.put((operation, "error"))


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


def test_keyring_rejects_symlinked_ancestors_and_nonregular_boundaries(
    tmp_path: Path,
) -> None:
    normal_parent = tmp_path / "normal"
    normal_parent.mkdir()
    normal_store = DashboardKeyringStore(
        Path(os.path.abspath(normal_parent / "keyring.json"))
    )
    normal_store.create("normal")

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    immediate_target = real_parent / "immediate-target"
    immediate_target.mkdir()
    immediate_link = tmp_path / "immediate-link"
    immediate_link.symlink_to(immediate_target, target_is_directory=True)
    with pytest.raises(DashboardAuthError):
        DashboardKeyringStore(immediate_link / "keyring.json").create("rejected")

    deep_target = real_parent / "deep-target"
    (deep_target / "nested").mkdir(parents=True)
    deep_link = tmp_path / "deep-link"
    deep_link.symlink_to(deep_target, target_is_directory=True)
    with pytest.raises(DashboardAuthError):
        DashboardKeyringStore(deep_link / "nested" / "keyring.json").create(
            "rejected"
        )

    leaf = tmp_path / "leaf.json"
    leaf_target = tmp_path / "leaf-target.json"
    leaf_target.write_bytes(b"not-a-keyring")
    os.chmod(leaf_target, 0o600)
    leaf.symlink_to(leaf_target)
    with pytest.raises(DashboardAuthError):
        DashboardKeyringStore(leaf).load()

    lock_store_path = tmp_path / "lock-keyring.json"
    lock_target = tmp_path / "lock-target"
    lock_target.write_bytes(b"")
    os.chmod(lock_target, 0o600)
    lock_store = DashboardKeyringStore(lock_store_path)
    lock_store.lock_path.symlink_to(lock_target)
    with pytest.raises(DashboardAuthError):
        lock_store.create("rejected")

    nonregular_leaf = tmp_path / "nonregular-leaf"
    nonregular_leaf.mkdir()
    with pytest.raises(DashboardAuthError):
        DashboardKeyringStore(nonregular_leaf).load()

    nonregular_lock_store = DashboardKeyringStore(tmp_path / "nonregular-lock.json")
    nonregular_lock_store.lock_path.mkdir()
    with pytest.raises(DashboardAuthError):
        nonregular_lock_store.create("rejected")


def test_keyring_create_is_process_safe_on_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "process-keyring.json"
    context = mp.get_context("spawn")
    output = context.Queue()
    workers = [
        context.Process(target=_create_in_process, args=(str(path), output))
        for _ in range(12)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        assert worker.exitcode == 0

    results = [output.get(timeout=5) for _ in workers]
    assert all(result[0] == "ok" for result in results)
    tokens = [result[1] for result in results]
    key_ids = [result[2] for result in results]
    assert len(set(tokens)) == len(tokens) == 12
    assert len(set(key_ids)) == len(key_ids) == 12
    store = DashboardKeyringStore(path)
    assert len(store.load().keys) == 12
    assert all(store.verify(token) for token in tokens)
    public = json.dumps(store.list_public())
    assert all(token not in public for token in tokens)
    assert "token_digest" not in public
    assert not list(tmp_path.glob(f".{path.name}.*.stage"))


def test_keyring_rotate_revoke_process_collision_has_valid_closed_result(
    tmp_path: Path,
) -> None:
    store, _, old_id = _store(tmp_path)
    context = mp.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    workers = [
        context.Process(
            target=_revoke_or_rotate_in_process,
            args=(str(store.path), old_id, operation, start, output),
        )
        for operation in ("revoke", "rotate")
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(20)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        assert worker.exitcode == 0

    results = [output.get(timeout=5) for _ in workers]
    assert {result[0] for result in results} == {"revoke", "rotate"}
    assert all(result[1] in {"ok", "rejected"} for result in results)
    final = store.load()
    old_record = next(record for record in final.keys if record.key_id == old_id)
    assert old_record.status == "revoked"
    assert len(final.keys) in {1, 2}
    assert not list(tmp_path.glob(f".{store.path.name}.*.stage"))


@pytest.mark.parametrize("clock_mode", ["future", "rollback"])
def test_cli_revoke_validation_failure_is_bounded_and_preserves_bytes(
    tmp_path: Path,
    clock_mode: str,
) -> None:
    store, _, key_id = _store(tmp_path)
    if clock_mode == "future":
        payload = json.loads(store.path.read_bytes())
        payload["keys"][0]["created_at"] = "2099-01-01T00:00:00Z"
        store.path.write_bytes(canonical_json_bytes(payload))
        os.chmod(store.path, 0o600)
    before = store.path.read_bytes()
    script = """
import sys
import inferdrome.dashboard.auth as auth
from inferdrome.cli import main
if sys.argv[1] == "rollback":
    auth._timestamp_now = lambda: "2000-01-01T00:00:00Z"
raise SystemExit(
    main(["dashboard-keyring", "revoke", "--keyring", sys.argv[2], sys.argv[3]])
)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script, clock_mode, str(store.path), key_id],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "inferdrome: error: dashboard key revocation was rejected\n"
    assert "traceback" not in result.stderr.lower()
    assert str(store.path) not in result.stderr
    assert key_id not in result.stderr
    assert "sha256:" not in result.stderr
    assert store.path.read_bytes() == before


def test_revoke_is_explicitly_idempotent_and_rotation_is_atomic(
    tmp_path: Path,
) -> None:
    revoke_root = tmp_path / "revoke"
    revoke_root.mkdir()
    revoke_store, token, revoke_id = _store(revoke_root)
    first = revoke_store.revoke(revoke_id)
    assert first.status == "revoked"
    assert first.revoked_at is not None
    assert not revoke_store.verify(token)
    assert revoke_store.revoke(revoke_id) == first

    rotation_root = tmp_path / "rotation"
    rotation_root.mkdir()
    store, token, old_id = _store(rotation_root)
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


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/runs",
        "/api/v1/runs/nonexistent",
        "/api/v1/compare",
        "/api/v1/trial-sets",
        "/api/v1/trial-sets/nonexistent",
        "/api/v1/controlled-comparisons",
        "/api/v1/controlled-comparisons/nonexistent",
    ],
)
def test_auth_precedes_route_validation_and_lookup(
    tmp_path: Path,
    path: str,
) -> None:
    _, _, _ = _store(tmp_path)
    keyring = tmp_path / "dashboard-keyring.json"
    app = create_app(
        DashboardIndex(tmp_path / "runs"),
        keyring_path=keyring,
    )
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "dashboard authentication failed"}
    assert response.headers["www-authenticate"] == "Bearer"


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
