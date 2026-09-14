"""Synthetic package bytes and fake Docker/APT only; no package/network operation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import vast_cpu_os as host
from scripts import vast_cpu_os_guest as guest
from scripts.vast_cpu_common import Failure, Result

CID = "a" * 64
BASELINE = [
    {"name": "apt", "version": "2.4.13", "architecture": "amd64"},
    {
        "name": "openssh-client",
        "version": "1:8.9p1-3ubuntu0.12",
        "architecture": "amd64",
    },
]
DEB = b"fake archive, deliberately not executable"
SHA = guest.digest(DEB)
VERSION = "1:8.9p1-3ubuntu0.13"
CHANGE = {
    "name": "openssh-client",
    "version": VERSION,
    "architecture": "amd64",
    "previous_version": BASELINE[1]["version"],
    "sha256": SHA,
    "size": len(DEB),
    "filename": "pool/main/o/openssh/openssh-client.deb",
    "suite": "jammy",
    "depends": "libc6 (>= 2.34)",
    "pre_depends": "",
    "file": SHA + ".deb",
}
SIMULATED = (
    f"Inst openssh-client [{BASELINE[1]['version']}] "
    f"({VERSION} Ubuntu:22.04/jammy [amd64])\n"
).encode()


def make_inputs(root: Path) -> dict:
    root.mkdir(exist_ok=True)
    for name in ("debs", "indexes"):
        (root / name).mkdir()
    if not (root / "install.py").exists():
        (root / "install.py").write_bytes(b"offline helper fixture\n")
    (root / "sources.list").write_bytes(b"snapshot fixture\n")
    (root / "apt.conf").write_bytes(b"isolated config fixture\n")
    (root / "debs" / CHANGE["file"]).write_bytes(DEB)
    (root / "indexes/jammy.InRelease").write_bytes(b"fake signed metadata")
    lock = {
        "schema": guest.SCHEMA,
        "base_image": guest.BASE,
        "snapshot": guest.SNAPSHOT,
        "suites": list(guest.SUITES),
        "packages": list(guest.PACKAGES),
        "architecture": "amd64",
        "baseline": BASELINE,
        "expected": guest.expected_inventory(BASELINE, [CHANGE]),
        "holds": [],
        "transaction": [CHANGE],
        "indexes": [
            {
                "file": "jammy.InRelease",
                "sha256": guest.digest(b"fake signed metadata"),
                "size": len(b"fake signed metadata"),
            }
        ],
        "apt_version": "2.4.13",
        "keyring_sha256": guest.digest(b"keyring"),
        "status_sha256": guest.digest(b"status"),
        "sources_sha256": guest.digest((root / "sources.list").read_bytes()),
        "apt_config_sha256": guest.digest((root / "apt.conf").read_bytes()),
        "helper_sha256": guest.digest((root / "install.py").read_bytes()),
    }
    (root / "lock.json").write_bytes(guest.canonical(lock))
    return lock


def lock_digest(root: Path) -> str:
    return guest.digest((root / "lock.json").read_bytes())


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.fail = False

    def run(self, argv, *, env=None, check=True):
        self.calls.append((argv, env))
        if self.fail:
            raise guest.Refusal("fake failure")
        return 0, b""


def candidate(**changes):
    return {
        "Package": CHANGE["name"],
        "Version": VERSION,
        "Architecture": "amd64",
        "SHA256": SHA,
        "Size": str(len(DEB)),
        "Filename": CHANGE["filename"],
        "Depends": CHANGE["depends"],
        "suite": "jammy",
        **changes,
    }


def test_transaction_preserves_epoch_and_authenticated_hash():
    commands = FakeCommands()
    assert guest.transaction(SIMULATED, BASELINE, [], [candidate()], commands) == [
        CHANGE
    ]
    assert commands.calls[0][0] == [
        "/usr/bin/dpkg",
        "--compare-versions",
        VERSION,
        "ge",
        BASELINE[1]["version"],
    ]


@pytest.mark.parametrize(
    "data",
    [
        b"Remv openssh-client [1]\n",
        b"Inst invalid\n",
        SIMULATED + SIMULATED,
        SIMULATED.replace(b"[amd64]", b"[arm64]"),
    ],
)
def test_invalid_or_removing_transaction_is_refused(data):
    with pytest.raises(guest.Refusal):
        guest.transaction(data, BASELINE, [], [candidate()], FakeCommands())


def test_hold_downgrade_missing_index_and_conflicting_index_refused():
    with pytest.raises(guest.Refusal, match="held"):
        guest.transaction(
            SIMULATED, BASELINE, ["openssh-client"], [candidate()], FakeCommands()
        )
    commands = FakeCommands()
    commands.fail = True
    with pytest.raises(guest.Refusal):
        guest.transaction(SIMULATED, BASELINE, [], [candidate()], commands)
    for candidates in ([], [candidate(), candidate(SHA256="b" * 64)]):
        with pytest.raises(guest.Refusal):
            guest.transaction(SIMULATED, BASELINE, [], candidates, FakeCommands())


def test_core_upgrade_requires_separate_review():
    data = b"Inst apt [2.4.13] (2.4.14 Ubuntu:22.04/jammy [amd64])\n"
    with pytest.raises(guest.Refusal, match="core"):
        guest.transaction(data, BASELINE, [], [], FakeCommands())


@pytest.mark.parametrize(
    "data",
    [
        b"bad\n",
        b"p\t1\tarm64\tii \n",
        b"apt\t2.4.13\tamd64\tiU \n",
        b"apt\t2.4.13\tamd64\tii \n" * 2,
    ],
)
def test_incomplete_foreign_duplicate_inventory_refused(data):
    with pytest.raises(guest.Refusal):
        guest.inventory(data)


def test_held_installed_inventory_is_retained():
    assert guest.inventory(b"apt:amd64\t2.4.13\tamd64\thi \n") == BASELINE[:1]


def test_isolated_config_disables_inherited_sources_hooks_pins_and_credentials(
    tmp_path,
):
    env, data = guest.apt_config(tmp_path / "apt", "fixed source\n")
    text = data.decode()
    for key in (
        "main",
        "parts",
        "sourceparts",
        "preferences",
        "preferencesparts",
        "netrc",
        "netrcparts",
        "trusted",
        "trustedparts",
    ):
        assert f'Dir::Etc::{key} "-";' in text
    assert 'Acquire::Check-Valid-Until "true";' in text
    assert 'Acquire::Retries "0";' in text
    assert 'Acquire::https::Proxy "DIRECT";' in text
    assert env == {**guest.ENV, "APT_CONFIG": str(tmp_path / "apt/apt.conf")}
    assert "TOKEN" not in " ".join(env) and "SSH_AUTH_SOCK" not in env


def test_signed_release_binds_package_index_before_selection(tmp_path):
    work, output = tmp_path / "apt", tmp_path / "indexes"
    (work / "lists").mkdir(parents=True)
    package = b"Package: openssh-client\nVersion: 1\nArchitecture: amd64\n\n"

    class Signed(FakeCommands):
        def run(self, argv, **kwargs):
            self.calls.append((argv, kwargs))
            suite = Path(argv[-1]).name.split("_dists_")[1].removesuffix("_InRelease")
            return 0, (
                f"Origin: Ubuntu\nLabel: Ubuntu\nSuite: {suite}\nCodename: jammy\n"
                f"SHA256:\n {guest.digest(package)} {len(package)} "
                "main/binary-amd64/Packages\n"
            ).encode()

    for suite in guest.SUITES:
        stem = f"snapshot.ubuntu.com_ubuntu_{guest.SNAPSHOT}_dists_{suite}"
        (work / "lists" / (stem + "_InRelease")).write_bytes(b"fake signature")
        (work / "lists" / (stem + "_main_binary-amd64_Packages")).write_bytes(package)
    commands = Signed()
    files, records = guest.authenticated_indexes(work, output, commands)
    assert len(files) == 6 and len(records) == 3
    assert all(
        call[0][:3] == ["/usr/bin/gpgv", "--keyring", str(guest.KEYRING)]
        for call in commands.calls
    )
    for path in output.iterdir():
        path.unlink()
    output.rmdir()
    next((work / "lists").glob("*_Packages")).write_bytes(b"tampered")
    with pytest.raises(guest.Refusal, match="signed Release"):
        guest.authenticated_indexes(work, output, commands)


def test_valid_lock_inputs(tmp_path):
    lock = make_inputs(tmp_path)
    assert guest.validate_inputs(tmp_path, lock_digest(tmp_path)) == lock


@pytest.mark.parametrize(
    "filename",
    [
        "install.py",
        "sources.list",
        "apt.conf",
        "indexes/jammy.InRelease",
        "debs/" + SHA + ".deb",
    ],
)
def test_each_bound_input_detects_tampering(tmp_path, filename):
    make_inputs(tmp_path)
    expected = lock_digest(tmp_path)
    (tmp_path / filename).write_bytes(b"tampered")
    with pytest.raises(guest.Refusal):
        guest.validate_inputs(tmp_path, expected)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_archive_links_or_directory_refused(tmp_path, kind):
    make_inputs(tmp_path)
    expected = lock_digest(tmp_path)
    path = tmp_path / "debs" / CHANGE["file"]
    path.unlink()
    other = tmp_path / "outside"
    other.write_bytes(DEB)
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    else:
        path.mkdir()
    with pytest.raises(guest.Refusal):
        guest.validate_inputs(tmp_path, expected)


def test_extra_archive_unknown_fields_and_digest_refused(tmp_path):
    lock = make_inputs(tmp_path)
    with pytest.raises(guest.Refusal, match="digest"):
        guest.validate_inputs(tmp_path, "0" * 64)
    lock["unreviewed"] = True
    (tmp_path / "lock.json").write_bytes(guest.canonical(lock))
    with pytest.raises(guest.Refusal, match="unknown"):
        guest.validate_inputs(tmp_path, lock_digest(tmp_path))
    del lock["unreviewed"]
    (tmp_path / "lock.json").write_bytes(guest.canonical(lock))
    (tmp_path / "debs/extra.deb").write_bytes(b"extra")
    with pytest.raises(guest.Refusal, match="unexpected deb"):
        guest.validate_inputs(tmp_path, lock_digest(tmp_path))


def test_install_local_only_and_exact_final_inventory(tmp_path, monkeypatch):
    lock = make_inputs(tmp_path)
    monkeypatch.setattr(guest, "platform", lambda _: (BASELINE, []))
    original_regular = guest.regular
    monkeypatch.setattr(
        guest,
        "regular",
        lambda path, *args: (
            b"keyring"
            if path == guest.KEYRING
            else b"status"
            if str(path) == "/var/lib/dpkg/status"
            else original_regular(path, *args)
        ),
    )
    monkeypatch.setattr(guest, "current_inventory", lambda _: lock["expected"])
    published = []
    original_publish = guest.publish

    def publish(path, data):
        if str(path) == "/opt/inferdrome-vast-os-lock.json":
            published.append((path, data))
        else:
            original_publish(path, data)

    monkeypatch.setattr(guest, "publish", publish)

    class Installer(FakeCommands):
        def run(self, argv, **kwargs):
            if argv[0] == "/usr/bin/apt-get":
                config = Path(kwargs["env"]["APT_CONFIG"])
                assert (config.parent / "etc/sources.list").read_bytes() == b""
                assert "--no-download" in argv and "--no-remove" in argv
                assert "update" not in argv
            return super().run(argv, **kwargs)

    commands = Installer()
    guest.install(tmp_path, lock_digest(tmp_path), commands)
    assert json.loads(published[0][1]) == lock
    assert len([call for call in commands.calls if call[0][0].endswith("apt-get")]) == 1
    monkeypatch.setattr(guest, "current_inventory", lambda _: BASELINE)
    with pytest.raises(guest.Refusal, match="final"):
        guest.install(tmp_path, lock_digest(tmp_path), Installer())
    assert len(published) == 1


class Budget:
    def __init__(self, seconds=500):
        self.seconds, self.children = seconds, []

    def child(self, seconds):
        self.children.append(seconds)
        return Budget(min(self.seconds, seconds))

    def remaining(self):
        if self.seconds <= 0:
            raise Failure("expired")
        return self.seconds


class Docker:
    def __init__(self, root, *, fail=None):
        self.root, self.fail, self.calls, self.owned = root, fail, [], []

    def record_container(self, cid):
        self.owned.append(cid)

    def forget_container(self, cid):
        self.owned.remove(cid)

    def run(self, argv, *, deadline, **kwargs):
        self.calls.append((argv, deadline, kwargs))
        if argv[1] == "create":
            assert "--pull=never" in argv and "--runtime=runc" in argv
            assert "--gpus" not in argv and "--privileged" not in argv
            if self.fail != "ambiguous":
                (self.root / "os-resolver.cid").write_text(CID + "\n")
            if self.fail in {"ambiguous", "create-timeout"}:
                raise Failure("timeout")
            return Result(0, (CID + "\n").encode(), b"")
        if argv[1] == "start":
            if self.fail == "start":
                raise Failure("failed")
            make_inputs(self.root / "os-input")
            if self.fail == "tamper":
                (self.root / "os-input/debs" / CHANGE["file"]).write_bytes(b"tampered")
        if argv[1] == "rm" and self.fail == "cleanup":
            raise Failure("cleanup deadline")
        if argv[1] == "ps" and self.fail == "present":
            return Result(0, CID.encode(), b"")
        return Result(0, b"", b"")


def test_host_resolver_exact_container_inputs_budget_and_absence(tmp_path):
    runner, execution, cleanup = Docker(tmp_path), Budget(80), Budget(120)
    result = host.resolve_os(runner, tmp_path, execution, cleanup)
    assert result["base_image"] == guest.BASE and result["snapshot"] == guest.SNAPSHOT
    assert result["lock_sha256"] == lock_digest(tmp_path / "os-input")
    assert execution.children == [900] and cleanup.children == [30]
    assert not runner.owned
    assert [call[0][1] for call in runner.calls] == ["create", "start", "rm", "ps"]
    assert runner.calls[0][1].remaining() == 80
    assert runner.calls[-1][0][5] == "id=" + CID


@pytest.mark.parametrize("failure", ["start", "tamper", "cleanup", "present"])
def test_host_failures_cleanup_or_remain_unconfirmed(tmp_path, failure):
    runner = Docker(tmp_path, fail=failure)
    with pytest.raises(Failure):
        host.resolve_os(runner, tmp_path, Budget(), Budget())
    assert any(call[0][1] == "rm" and call[0][-1] == CID for call in runner.calls)
    if failure in {"cleanup", "present"}:
        assert runner.owned == [CID]
    else:
        assert not runner.owned


def test_create_ambiguity_is_not_retried_or_inferred(tmp_path):
    runner = Docker(tmp_path, fail="ambiguous")
    with pytest.raises(Failure, match="UNRESOLVED"):
        host.resolve_os(runner, tmp_path, Budget(), Budget())
    assert [call[0][1] for call in runner.calls] == ["create"]


def test_create_timeout_with_retained_id_is_cleaned(tmp_path):
    runner = Docker(tmp_path, fail="create-timeout")
    with pytest.raises(Failure, match="timeout"):
        host.resolve_os(runner, tmp_path, Budget(), Budget())
    assert [call[0][1] for call in runner.calls] == ["create", "rm", "ps"]
    assert not runner.owned


def test_expired_budget_prevents_create(tmp_path):
    runner = Docker(tmp_path)
    with pytest.raises(Failure):
        host.resolve_os(runner, tmp_path, Budget(0), Budget())
    assert not runner.calls


def test_no_replace_publication_and_open_time_replacement(tmp_path, monkeypatch):
    path = tmp_path / "data"
    guest.publish(path, b"first")
    with pytest.raises(FileExistsError):
        guest.publish(path, b"second")
    assert path.read_bytes() == b"first"
    original_open = os.open

    def replaced_open(name, flags, *args, **kwargs):
        if name == path:
            path.unlink()
            path.write_bytes(b"other")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced_open)
    with pytest.raises(guest.Refusal, match="replaced"):
        guest.regular(path)


def test_full_synthetic_resolution_is_download_only_and_complete(tmp_path, monkeypatch):
    (tmp_path / "install.py").write_bytes(b"synthetic immutable helper")
    monkeypatch.setattr(guest, "platform", lambda _: (BASELINE, []))
    monkeypatch.setattr(guest, "current_inventory", lambda _: BASELINE)
    original_regular = guest.regular
    monkeypatch.setattr(
        guest,
        "regular",
        lambda path, *args: (
            b"keyring"
            if path == guest.KEYRING
            else b"status"
            if str(path) == "/var/lib/dpkg/status"
            else original_regular(path, *args)
        ),
    )
    package = (
        f"Package: openssh-client\nVersion: {VERSION}\nArchitecture: amd64\n"
        f"Filename: {CHANGE['filename']}\nSize: {len(DEB)}\nSHA256: {SHA}\n"
        f"Depends: {CHANGE['depends']}\n\n"
    ).encode()

    class Resolver(FakeCommands):
        def run(self, argv, *, env=None, check=True):
            self.calls.append((argv, env))
            if "update" in argv:
                assert "--error-on=any" in argv
                work = Path(env["APT_CONFIG"]).parent
                assert (work / "status").read_bytes() == b"status"
                assert (
                    f'Dir::State::status "{work}/status";'
                    in (work / "apt.conf").read_text()
                )
                sources = (work / "etc/sources.list").read_text()
                assert sources.count(guest.ROOT_URL) == 3
                assert "archive.ubuntu.com" not in sources
                for suite in guest.SUITES:
                    stem = f"snapshot.ubuntu.com_ubuntu_{guest.SNAPSHOT}_dists_{suite}"
                    (work / "lists" / (stem + "_InRelease")).write_bytes(
                        b"signed fixture"
                    )
                    (
                        work / "lists" / (stem + "_main_binary-amd64_Packages")
                    ).write_bytes(package)
            elif argv[0] == "/usr/bin/gpgv":
                suite = (
                    Path(argv[-1]).name.split("_dists_")[1].removesuffix("_InRelease")
                )
                return 0, (
                    f"Origin: Ubuntu\nLabel: Ubuntu\nSuite: {suite}\nCodename: jammy\n"
                    f"SHA256:\n {guest.digest(package)} {len(package)} "
                    "main/binary-amd64/Packages\n"
                ).encode()
            elif "--simulate" in argv:
                return 0, SIMULATED
            elif "--download-only" in argv:
                archive = Path(env["APT_CONFIG"]).parent / "archives/fixture.deb"
                archive.write_bytes(DEB)
            elif argv[0] == "/usr/bin/dpkg-deb":
                return 0, (
                    f"Package: openssh-client\nVersion: {VERSION}\n"
                    "Architecture: amd64\n"
                ).encode()
            return 0, b""

    commands = Resolver()
    lock = guest.resolve(tmp_path, commands)
    assert guest.validate_inputs(tmp_path, lock_digest(tmp_path)) == lock
    assert lock["baseline"] == BASELINE
    assert lock["transaction"] == [CHANGE]
    assert len(lock["indexes"]) == 6
    assert not (tmp_path / ".apt").exists()
    assert all(
        "--simulate" in argv or "--download-only" in argv
        for argv, _ in commands.calls
        if "install" in argv
    )


@pytest.mark.parametrize(
    "architecture,foreign,apt",
    [
        (b"arm64\n", b"", "2.4.13"),
        (b"amd64\n", b"i386\n", "2.4.13"),
        (b"amd64\n", b"", "2.4.10"),
    ],
)
def test_platform_checks_actual_architectures_and_apt_floor(
    monkeypatch, architecture, foreign, apt
):
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            'ID=ubuntu\nVERSION_ID="22.04"\nVERSION_CODENAME=jammy\n'
            if str(path) == "/etc/os-release"
            else original_read(path, *args, **kwargs)
        ),
    )

    class Platform(FakeCommands):
        def run(self, argv, **kwargs):
            if "--print-architecture" in argv:
                return 0, architecture
            if "--print-foreign-architectures" in argv:
                return 0, foreign
            if argv[0] == "/usr/bin/dpkg-query":
                return 0, f"apt\t{apt}\tamd64\tii \n".encode()
            if "--compare-versions" in argv and apt == "2.4.10":
                raise guest.Refusal("old apt")
            return 0, b""

    with pytest.raises(guest.Refusal):
        guest.platform(Platform())


def test_missing_signature_never_reaches_package_selection(tmp_path):
    (tmp_path / "lists").mkdir()
    commands = FakeCommands()
    with pytest.raises(FileNotFoundError):
        guest.authenticated_indexes(tmp_path, tmp_path / "output", commands)
    assert not commands.calls


@pytest.mark.parametrize("seconds", [0, -1, 901, float("nan"), float("inf")])
def test_guest_budget_rejects_invalid_or_unbounded_values(seconds):
    with pytest.raises(guest.Refusal):
        guest.Commands(seconds)
