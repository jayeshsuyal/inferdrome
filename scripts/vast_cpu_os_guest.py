"""Standalone, explicitly invoked Vast OS resolver/local installer.

Resolution downloads signed Ubuntu inputs without installing them. Installation
requires the generated lock's exact digest and is invoked with Docker networking
disabled. Importing this module performs no subprocess or network operation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

BASE = (
    "vllm/vllm-openai@sha256:"
    "ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52"
)
SNAPSHOT = "20260913T000000Z"
SUITES = ("jammy", "jammy-updates", "jammy-security")
PACKAGES = ("openssh-server", "openssh-client", "passwd", "openssl", "login")
KEYRING = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")
ROOT_URL = f"https://snapshot.ubuntu.com/ubuntu/{SNAPSHOT}/"
SCHEMA = "vast-cpu-os-lock-v1"
NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]*")
HASH = re.compile(r"[0-9a-f]{64}")
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "HOME": "/nonexistent",
    "DEBIAN_FRONTEND": "noninteractive",
}
CORE = {"apt", "dpkg", "libc6", "libc-bin", "ubuntu-keyring", "base-files"}


class Refusal(RuntimeError):
    """A package input or operation did not meet the fixed contract."""


def require(value: object, message: str) -> None:
    if not value:
        raise Refusal(message)


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular(path: Path, limit: int = 256 * 1024 * 1024) -> bytes:
    before = path.lstat()
    require(
        stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
        "input must be a single-link regular file",
    )
    require(before.st_size <= limit, "input exceeds size limit")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        require(
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            and opened.st_nlink == 1,
            "input replaced during open",
        )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        current = path.lstat()
        require(
            (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "input changed during read",
        )
        require(
            (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
            and after.st_nlink == current.st_nlink == 1,
            "input replaced during read",
        )
        require(len(data) == before.st_size, "input size changed")
        return data
    finally:
        os.close(fd)


def publish(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Commands:
    def __init__(self, seconds: float):
        require(0 < seconds <= 900, "invalid guest operation budget")
        self.end = time.monotonic() + seconds

    def run(
        self, argv: list[str], *, env: dict[str, str] | None = None, check: bool = True
    ) -> tuple[int, bytes]:
        require(time.monotonic() < self.end, "guest deadline expired")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or ENV,
            start_new_session=True,
        )
        output = bytearray()
        total = 0
        try:
            with selectors.DefaultSelector() as selector:
                for pipe in (process.stdout, process.stderr):
                    assert pipe is not None
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = self.end - time.monotonic()
                    require(remaining > 0, "guest deadline expired")
                    for event, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(event.fd, 65536)
                        if not chunk:
                            selector.unregister(event.fileobj)
                        else:
                            total += len(chunk)
                            require(
                                total <= 4 * 1024 * 1024, "command output too large"
                            )
                            if event.fileobj is process.stdout:
                                output.extend(chunk)
                remaining = self.end - time.monotonic()
                require(remaining > 0, "guest deadline expired")
                code = process.wait(timeout=remaining)
            require(not check or code == 0, "OS command failed")
            return code, bytes(output)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


def inventory(data: bytes) -> list[dict[str, str]]:
    result = []
    seen = set()
    for line in data.decode("ascii").splitlines():
        fields = line.split("\t")
        require(len(fields) == 4, "malformed dpkg inventory")
        name, version, architecture, status = fields
        name = name.removesuffix(":" + architecture)
        require(
            NAME.fullmatch(name) and VERSION.fullmatch(version),
            "malformed package identity",
        )
        require(
            architecture in {"amd64", "all"} and status in {"ii ", "hi "},
            "base contains foreign or incompletely installed packages",
        )
        require((name, architecture) not in seen, "duplicate package identity")
        seen.add((name, architecture))
        result.append({"name": name, "version": version, "architecture": architecture})
    require(result, "empty dpkg inventory")
    return sorted(result, key=lambda row: (row["name"], row["architecture"]))


def current_inventory(commands: Commands) -> list[dict[str, str]]:
    return inventory(
        commands.run(
            [
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n",
            ]
        )[1]
    )


def platform(commands: Commands) -> tuple[list[dict[str, str]], list[str]]:
    release = Path("/etc/os-release").read_text()
    require(
        re.search(r'^ID=["\']?ubuntu["\']?$', release, re.M)
        and re.search(r'^VERSION_ID=["\']?22\.04["\']?$', release, re.M)
        and re.search(r"^VERSION_CODENAME=jammy$", release, re.M),
        "pinned base is not expected Ubuntu Jammy",
    )
    require(
        commands.run(["/usr/bin/dpkg", "--print-architecture"])[1] == b"amd64\n",
        "pinned base architecture mismatch",
    )
    require(
        not commands.run(["/usr/bin/dpkg", "--print-foreign-architectures"])[1],
        "foreign architectures are not permitted",
    )
    baseline = current_inventory(commands)
    apt = next((row["version"] for row in baseline if row["name"] == "apt"), None)
    require(apt, "APT is absent")
    commands.run(["/usr/bin/dpkg", "--compare-versions", str(apt), "ge", "2.4.11"])
    selections = commands.run(["/usr/bin/dpkg", "--get-selections"])[1]
    holds = sorted(
        line.split()[0]
        for line in selections.decode("ascii").splitlines()
        if line.split()[-1] == "hold"
    )
    return baseline, holds


def apt_config(
    work: Path, sources: str, *, status: Path | None = None
) -> tuple[dict[str, str], bytes]:
    work.mkdir(mode=0o700)
    for directory in ("lists", "archives", "etc", "log"):
        (work / directory).mkdir()
    (work / "lists/partial").mkdir()
    (work / "archives/partial").mkdir()
    publish(work / "etc/sources.list", sources.encode("ascii"))
    config = f'''Dir::Etc "{work}/etc";
Dir::Etc::main "-";
Dir::Etc::parts "-";
Dir::Etc::sourcelist "sources.list";
Dir::Etc::sourceparts "-";
Dir::Etc::preferences "-";
Dir::Etc::preferencesparts "-";
Dir::Etc::netrc "-";
Dir::Etc::netrcparts "-";
Dir::Etc::trusted "-";
Dir::Etc::trustedparts "-";
Dir::State::status "{status or "/var/lib/dpkg/status"}";
Dir::State::extended_states "{work}/extended_states";
Dir::State::lists "{work}/lists";
Dir::Cache::archives "{work}/archives";
Dir::Cache::pkgcache "{work}/pkgcache.bin";
Dir::Cache::srcpkgcache "{work}/srcpkgcache.bin";
Dir::Log "{work}/log";
APT::Architecture "amd64";
APT::Architectures {{ "amd64"; }};
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Retries "0";
Acquire::Languages "none";
Acquire::GzipIndexes "false";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
Acquire::http::Proxy "DIRECT";
Acquire::https::Proxy "DIRECT";
Acquire::AllowInsecureRepositories "false";
Acquire::AllowDowngradeToInsecureRepositories "false";
Acquire::Check-Valid-Until "true";
APT::Get::AllowUnauthenticated "false";
DPkg::Use-Pty "false";
DPkg::Options {{ "--force-confold"; }};
'''.encode("ascii")
    publish(work / "apt.conf", config)
    return {**ENV, "APT_CONFIG": str(work / "apt.conf")}, config


def paragraphs(data: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for paragraph in data.decode("utf-8").strip().split("\n\n"):
        row: dict[str, str] = {}
        key = ""
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                require(key, "invalid index continuation")
                row[key] += "\n" + line[1:]
            else:
                key, separator, value = line.partition(":")
                require(separator and key not in row, "invalid index field")
                row[key] = value.strip()
        records.append(row)
    return records


def authenticated_indexes(
    work: Path, output: Path, commands: Commands
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    output.mkdir()
    files = []
    candidates = []
    prefix = f"snapshot.ubuntu.com_ubuntu_{SNAPSHOT}_dists_"
    for suite in SUITES:
        release_path = work / "lists" / f"{prefix}{suite}_InRelease"
        package_path = work / "lists" / f"{prefix}{suite}_main_binary-amd64_Packages"
        release_bytes = regular(release_path)
        clear = commands.run(
            [
                "/usr/bin/gpgv",
                "--keyring",
                str(KEYRING),
                "--output",
                "-",
                str(release_path),
            ]
        )[1]
        release = paragraphs(clear)[0]
        require(
            release.get("Origin") == "Ubuntu"
            and release.get("Label") == "Ubuntu"
            and release.get("Suite") == suite
            and release.get("Codename") == "jammy",
            "signed index origin mismatch",
        )
        package_bytes = regular(package_path)
        lines = [line.split() for line in release.get("SHA256", "").splitlines()]
        match = [
            line
            for line in lines
            if len(line) == 3 and line[2] == "main/binary-amd64/Packages"
        ]
        require(
            len(match) == 1
            and match[0][0] == digest(package_bytes)
            and match[0][1] == str(len(package_bytes)),
            "Packages index is not bound by signed Release",
        )
        for suffix, data in (("InRelease", release_bytes), ("Packages", package_bytes)):
            name = f"{suite}.{suffix}"
            publish(output / name, data)
            files.append({"file": name, "sha256": digest(data), "size": len(data)})
        for row in paragraphs(package_bytes):
            if row.get("Architecture") in {"amd64", "all"}:
                candidates.append({**row, "suite": suite})
    return files, candidates


def transaction(
    data: bytes,
    baseline: list[dict[str, str]],
    holds: list[str],
    candidates: list[dict[str, str]],
    commands: Commands,
) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for line in data.decode("utf-8").splitlines():
        require(not line.startswith("Remv "), "package removal refused")
        if not line.startswith("Inst "):
            continue
        match = re.fullmatch(
            r"Inst ([a-z0-9][a-z0-9+.:~-]*)(?: \[([^]]+)\])? "
            r"\((\S+) [^\n]*\[([a-z0-9]+)\]\)",
            line,
        )
        require(match, "unrecognized APT transaction")
        assert match is not None
        name, old, version, architecture = match.groups()
        name = name.removesuffix(":" + architecture)
        require(
            NAME.fullmatch(name)
            and VERSION.fullmatch(version)
            and architecture in {"amd64", "all"}
            and name not in seen,
            "invalid APT transaction identity",
        )
        require(
            name not in holds and name + ":" + architecture not in holds,
            "held package change refused",
        )
        before = next((row for row in baseline if row["name"] == name), None)
        require(
            (before is None and old is None)
            or (before is not None and old == before["version"]),
            "simulation baseline mismatch",
        )
        if before:
            commands.run(["/usr/bin/dpkg", "--compare-versions", version, "ge", old])
            require(name not in CORE, "core package change requires separate review")
        options = [
            row
            for row in candidates
            if row.get("Package") == name
            and row.get("Version") == version
            and row.get("Architecture") == architecture
        ]
        require(options, "transaction package absent from authenticated indexes")
        fingerprints = {
            (row.get("SHA256"), row.get("Size"), row.get("Filename")) for row in options
        }
        require(len(fingerprints) == 1, "conflicting package index records")
        row = options[0]
        filename = row.get("Filename", "")
        require(
            re.fullmatch(r"pool/main/[A-Za-z0-9+./_~%-]+\.deb", filename)
            and ".." not in filename
            and "%" not in filename,
            "invalid authenticated package path",
        )
        sha = row.get("SHA256", "")
        size = row.get("Size", "")
        require(
            HASH.fullmatch(sha)
            and size.isdigit()
            and 0 < int(size) <= 256 * 1024 * 1024,
            "invalid authenticated package hash/size",
        )
        result.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "previous_version": old,
                "sha256": sha,
                "size": int(size),
                "filename": filename,
                "suite": row["suite"],
                "depends": row.get("Depends", ""),
                "pre_depends": row.get("Pre-Depends", ""),
                "file": sha + ".deb",
            }
        )
        seen.add(name)
    return sorted(result, key=lambda row: row["name"])


def expected_inventory(
    baseline: list[dict[str, str]], changes: list[dict[str, Any]]
) -> list[dict[str, str]]:
    result = {row["name"]: row for row in baseline}
    for row in changes:
        result[row["name"]] = {
            key: row[key] for key in ("name", "version", "architecture")
        }
    return sorted(result.values(), key=lambda row: (row["name"], row["architecture"]))


def resolve(root: Path, commands: Commands) -> dict[str, Any]:
    require(not (root / "lock.json").exists(), "resolution output already exists")
    baseline, holds = platform(commands)
    sources = "".join(
        f"deb [arch=amd64 signed-by={KEYRING}] {ROOT_URL} {suite} main\n"
        for suite in SUITES
    )
    work = root / ".apt"
    env, config = apt_config(work, sources, status=work / "status")
    # APT's locks and solver state belong to this private directory. The
    # operator-UID resolver never locks or writes the read-only base database.
    publish(work / "status", regular(Path("/var/lib/dpkg/status")))
    commands.run(["/usr/bin/apt-get", "update", "--error-on=any"], env=env)
    indexes, candidates = authenticated_indexes(work, root / "indexes", commands)
    options = ["--no-install-recommends", "--no-remove"]
    simulated = commands.run(
        ["/usr/bin/apt-get", "--simulate", *options, "install", *PACKAGES], env=env
    )[1]
    changes = transaction(simulated, baseline, holds, candidates, commands)
    commands.run(
        [
            "/usr/bin/apt-get",
            "--download-only",
            "--yes",
            *options,
            "install",
            *PACKAGES,
        ],
        env=env,
    )
    require(
        current_inventory(commands) == baseline, "resolver modified package inventory"
    )
    debs = root / "debs"
    debs.mkdir()
    fetched = list((work / "archives").glob("*.deb"))
    by_hash = {digest(regular(path)): path for path in fetched}
    require(
        len(by_hash) == len(fetched)
        and set(by_hash) == {row["sha256"] for row in changes},
        "downloaded deb set differs from simulation",
    )
    for row in changes:
        data = regular(by_hash[row["sha256"]])
        require(len(data) == row["size"], "downloaded deb size differs")
        fields = paragraphs(
            commands.run(
                [
                    "/usr/bin/dpkg-deb",
                    "--field",
                    str(by_hash[row["sha256"]]),
                    "Package",
                    "Version",
                    "Architecture",
                ]
            )[1]
        )[0]
        require(
            fields
            == {
                "Package": row["name"],
                "Version": row["version"],
                "Architecture": row["architecture"],
            },
            "authenticated archive identity mismatch",
        )
        publish(debs / row["file"], data)
    lock = {
        "schema": SCHEMA,
        "base_image": BASE,
        "snapshot": SNAPSHOT,
        "suites": list(SUITES),
        "packages": list(PACKAGES),
        "architecture": "amd64",
        "baseline": baseline,
        "expected": expected_inventory(baseline, changes),
        "holds": holds,
        "transaction": changes,
        "indexes": indexes,
        "apt_version": next(row["version"] for row in baseline if row["name"] == "apt"),
        "keyring_sha256": digest(regular(KEYRING)),
        "status_sha256": digest(regular(Path("/var/lib/dpkg/status"))),
        "sources_sha256": digest(sources.encode("ascii")),
        "apt_config_sha256": digest(config),
        "helper_sha256": digest(regular(root / "install.py")),
    }
    publish(root / "sources.list", sources.encode("ascii"))
    publish(root / "apt.conf", config)
    publish(root / "lock.json", canonical(lock))
    shutil.rmtree(work)
    return lock


def validate_inputs(root: Path, expected_digest: str) -> dict[str, Any]:
    for directory in (root, root / "debs", root / "indexes"):
        require(
            stat.S_ISDIR(directory.lstat().st_mode), "OS input directory is not real"
        )
    require(
        {path.name for path in root.iterdir()}
        == {"lock.json", "install.py", "sources.list", "apt.conf", "debs", "indexes"},
        "unexpected OS input entry",
    )
    require(
        HASH.fullmatch(expected_digest), "required lock digest missing or malformed"
    )
    data = regular(root / "lock.json", 8 * 1024 * 1024)
    require(digest(data) == expected_digest, "OS lock digest mismatch")
    lock = json.loads(data)
    if not isinstance(lock, dict):
        raise Refusal("OS lock must be an object")
    require(canonical(lock) == data, "OS lock must be canonical")
    require(
        set(lock)
        == {
            "schema",
            "base_image",
            "snapshot",
            "suites",
            "packages",
            "architecture",
            "baseline",
            "expected",
            "holds",
            "transaction",
            "indexes",
            "apt_version",
            "keyring_sha256",
            "status_sha256",
            "sources_sha256",
            "apt_config_sha256",
            "helper_sha256",
        },
        "unknown OS lock fields",
    )
    require(
        lock["schema"] == SCHEMA
        and lock["base_image"] == BASE
        and lock["snapshot"] == SNAPSHOT
        and lock["suites"] == list(SUITES)
        and lock["packages"] == list(PACKAGES)
        and lock["architecture"] == "amd64",
        "OS lock profile mismatch",
    )
    for filename, field in (
        ("install.py", "helper_sha256"),
        ("sources.list", "sources_sha256"),
        ("apt.conf", "apt_config_sha256"),
    ):
        require(
            digest(regular(root / filename)) == lock[field], "OS lock input tampered"
        )
    require(
        {path.name for path in (root / "debs").iterdir()}
        == {row["file"] for row in lock["transaction"]},
        "unexpected deb input",
    )
    require(
        {path.name for path in (root / "indexes").iterdir()}
        == {row["file"] for row in lock["indexes"]},
        "unexpected index input",
    )
    for directory_name, rows in (
        ("debs", lock["transaction"]),
        ("indexes", lock["indexes"]),
    ):
        for row in rows:
            require(
                Path(row["file"]).name == row["file"]
                and row["file"] not in {".", ".."},
                "unsafe lock path",
            )
            content = regular(root / directory_name / row["file"])
            require(
                digest(content) == row["sha256"] and len(content) == row["size"],
                "OS archive input tampered",
            )
    require(
        expected_inventory(lock["baseline"], lock["transaction"]) == lock["expected"],
        "inconsistent final inventory",
    )
    return lock


def install(root: Path, expected_digest: str, commands: Commands) -> None:
    lock = validate_inputs(root, expected_digest)
    baseline, holds = platform(commands)
    require(
        baseline == lock["baseline"] and holds == lock["holds"],
        "build baseline mismatch",
    )
    require(
        digest(regular(KEYRING)) == lock["keyring_sha256"]
        and digest(regular(Path("/var/lib/dpkg/status"))) == lock["status_sha256"],
        "build base package state differs",
    )
    with tempfile.TemporaryDirectory(prefix="inferdrome-os-") as temporary:
        env, _ = apt_config(Path(temporary) / "apt", "")
        debs = [str(root / "debs" / row["file"]) for row in lock["transaction"]]
        if debs:
            commands.run(
                [
                    "/usr/bin/apt-get",
                    "--yes",
                    "--no-download",
                    "--no-remove",
                    "--no-install-recommends",
                    "install",
                    *debs,
                ],
                env=env,
            )
    require(
        current_inventory(commands) == lock["expected"],
        "final package inventory mismatch",
    )
    require(
        not commands.run(["/usr/bin/dpkg", "--audit"])[1], "dpkg audit is not empty"
    )
    publish(Path("/opt/inferdrome-vast-os-lock.json"), canonical(lock))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("resolve", "install"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--lock-sha256")
    args = parser.parse_args()
    try:
        commands = Commands(args.seconds)
        if args.mode == "resolve":
            resolve(args.root, commands)
        else:
            install(args.root, args.lock_sha256 or "", commands)
        return 0
    except (
        Refusal,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        print("Vast OS package operation refused", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
