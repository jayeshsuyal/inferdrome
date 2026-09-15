"""Exercise image account setup with fake effects; never run account or SSH tools."""

from __future__ import annotations

import builtins
import json
import re
import shlex
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.vast-process"
EXECUTABLES = (
    "/usr/sbin/useradd",
    "/usr/sbin/usermod",
    "/usr/bin/passwd",
    "/usr/bin/openssl",
    "/usr/sbin/nologin",
    "/usr/sbin/sshd",
    "/usr/bin/ssh-keygen",
)


@dataclass(frozen=True)
class Account:
    pw_name: str
    pw_uid: int
    pw_gid: int = 0
    pw_dir: str = "/home/vllm"
    pw_shell: str = "/bin/bash"


BASE = [Account("root", 0, pw_dir="/root"), Account("vllm", 2000)]


class FakeEffects:
    """Only these fake imports reach the checked-in Dockerfile Python block."""

    def __init__(self, accounts: list[Account] | None = None) -> None:
        self.accounts = list(BASE if accounts is None else accounts)
        self.calls: list[tuple[Any, ...]] = []
        self.status = b"P"
        self.password_hash = "$6$fakeSalt$" + "a" * 86
        self.metadata = {
            name: SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=0)
            for name in EXECUTABLES
        }
        for name in ("passwd", "group", "shadow", "gshadow"):
            self.metadata["/etc/" + name] = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o664,
                st_uid=2000,
                st_gid=2001,
            )
        effects = self

        class FakePath:
            def __init__(self, name: str) -> None:
                self.name = name

            def __str__(self) -> str:
                return self.name

            def __truediv__(self, name: str) -> FakePath:
                return FakePath(self.name.rstrip("/") + "/" + name)

            def stat(self) -> SimpleNamespace:
                if self.name not in effects.metadata:
                    raise FileNotFoundError(self.name)
                return effects.metadata[self.name]

            lstat = stat

        self.modules = {
            "os": SimpleNamespace(
                X_OK=1,
                access=lambda path, mode: bool(path.stat().st_mode & 0o111),
                chown=self.chown,
                chmod=self.chmod,
            ),
            "pwd": SimpleNamespace(getpwall=lambda: list(self.accounts)),
            "pathlib": SimpleNamespace(Path=FakePath),
            "secrets": SimpleNamespace(token_urlsafe=self.token),
            "subprocess": SimpleNamespace(run=self.run, PIPE=-1, DEVNULL=-3),
            "re": re,
            "stat": stat,
        }

    def token(self, size: int) -> str:
        self.calls.append(("token", size))
        return "public-synthetic-test-value"

    def run(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("subprocess", argv, kwargs))
        assert kwargs.get("check") is True
        program = argv[0]

        def option(name: str) -> str:
            return argv[argv.index(name) + 1]

        if program == "/usr/sbin/usermod":
            account = next(item for item in self.accounts if item.pw_name == argv[-1])
            self.accounts[self.accounts.index(account)] = replace(
                account,
                pw_name=option("--login"),
                pw_shell=option("--shell"),
            )
        elif program == "/usr/bin/openssl":
            assert (
                "-stdin" in argv and kwargs["input"] == b"public-synthetic-test-value"
            )
            return SimpleNamespace(stdout=(self.password_hash + "\n").encode())
        elif program == "/usr/sbin/useradd":
            self.accounts.append(
                Account(
                    argv[-1],
                    int(option("--uid")),
                    int(option("--gid")),
                    option("--home-dir"),
                    option("--shell"),
                )
            )
        elif program == "/usr/bin/passwd":
            assert argv[1:] == ["-S", "inferdrome-transfer"]
            return SimpleNamespace(
                stdout=b"inferdrome-transfer " + self.status + b" fake"
            )
        else:
            raise AssertionError("unexpected fake command: " + program)
        return SimpleNamespace(stdout=b"")

    def chown(self, path: object, uid: int, gid: int) -> None:
        self.calls.append(("chown", str(path), uid, gid))
        metadata = self.metadata[str(path)]
        metadata.st_uid, metadata.st_gid = uid, gid

    def chmod(self, path: object, mode: int) -> None:
        self.calls.append(("chmod", str(path), mode))
        metadata = self.metadata[str(path)]
        metadata.st_mode = stat.S_IFMT(metadata.st_mode) | mode

    def execute(self) -> None:
        blocks = re.findall(
            r"^RUN /usr/bin/python3[.]12 -I - <<'PY'\n(.*?)\nPY$",
            DOCKERFILE.read_text(),
            re.MULTILINE | re.DOTALL,
        )
        assert len(blocks) == 1, "expected one checked-in account setup block"

        def fake_import(
            name: str,
            globals: Any = None,
            locals: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            assert not level and name in self.modules, "unexpected import: " + name
            return self.modules[name]

        allowed = {
            name: getattr(builtins, name) for name in ("RuntimeError", "len", "all")
        }
        allowed["__import__"] = fake_import
        # No real OS imports, subprocesses, filesystem paths, or open/eval builtins.
        exec(
            compile(blocks[0], str(DOCKERFILE) + ":account-setup", "exec"),
            {
                "__builtins__": allowed,
            },
        )


def test_account_setup_preserves_uid_and_home_and_restricts_account_files() -> None:
    effects = FakeEffects()
    effects.execute()
    assert effects.accounts == [
        BASE[0],
        Account("inferdrome-experiment", 2000, 0, "/home/vllm", "/usr/sbin/nologin"),
        Account("inferdrome-transfer", 2001, 0, "/uploads", "/usr/sbin/nologin"),
    ]
    commands = [call[1] for call in effects.calls if call[0] == "subprocess"]
    rename = next(argv for argv in commands if argv[0] == "/usr/sbin/usermod")
    assert "--lock" in rename
    creates = [argv for argv in commands if argv[0] == "/usr/sbin/useradd"]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--uid") + 1] == "2001"
    assert "--no-create-home" in creates[0]
    assert creates[0][creates[0].index("--password") + 1] == effects.password_hash
    for name, mode in (
        ("passwd", 0o644),
        ("group", 0o644),
        ("shadow", 0o600),
        ("gshadow", 0o600),
    ):
        metadata = effects.metadata["/etc/" + name]
        assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (
            0,
            0,
            mode,
        )


@pytest.mark.parametrize(
    "accounts",
    [
        pytest.param([BASE[0]], id="missing-experiment-uid"),
        pytest.param(
            [*BASE, Account("duplicate", 2000)], id="duplicate-experiment-uid"
        ),
        pytest.param(
            [BASE[0], replace(BASE[1], pw_name="other")], id="wrong-base-name"
        ),
        pytest.param([BASE[0], replace(BASE[1], pw_gid=2000)], id="wrong-base-group"),
        pytest.param(
            [BASE[0], replace(BASE[1], pw_dir="/wrong")], id="wrong-base-home"
        ),
        pytest.param(
            [BASE[0], replace(BASE[1], pw_shell="/bin/sh")], id="wrong-base-shell"
        ),
        pytest.param([*BASE, Account("other", 2001)], id="occupied-transfer-uid"),
        pytest.param(
            [*BASE, Account("inferdrome-experiment", 3000)], id="experiment-name"
        ),
        pytest.param([*BASE, Account("inferdrome-transfer", 3000)], id="transfer-name"),
    ],
)
def test_unexpected_base_accounts_fail_before_any_effect(
    accounts: list[Account],
) -> None:
    effects = FakeEffects(accounts)
    with pytest.raises(RuntimeError):
        effects.execute()
    assert effects.calls == []
    assert effects.accounts == accounts


@pytest.mark.parametrize("path", EXECUTABLES)
def test_missing_required_executable_fails_before_any_effect(path: str) -> None:
    effects = FakeEffects()
    del effects.metadata[path]
    with pytest.raises(FileNotFoundError):
        effects.execute()
    assert effects.calls == []


@pytest.mark.parametrize(
    "mode,uid",
    [
        (stat.S_IFREG | 0o644, 0),
        (stat.S_IFDIR | 0o755, 0),
        (stat.S_IFREG | 0o755, 2000),
        (stat.S_IFREG | 0o775, 0),
        (stat.S_IFREG | 0o757, 0),
    ],
)
def test_unsafe_executable_fails_before_any_effect(mode: int, uid: int) -> None:
    effects = FakeEffects()
    effects.metadata["/usr/sbin/nologin"].st_mode = mode
    effects.metadata["/usr/sbin/nologin"].st_uid = uid
    with pytest.raises(RuntimeError, match="required executable"):
        effects.execute()
    assert effects.calls == []


@pytest.mark.parametrize("status", [b"L", b"NP", b""])
def test_transfer_account_requires_a_nonlocked_password_status(status: bytes) -> None:
    effects = FakeEffects()
    effects.status = status
    with pytest.raises(RuntimeError, match="transfer account locked"):
        effects.execute()


@pytest.mark.parametrize("password_hash", ["", "!", "*", "$6$truncated"])
def test_invalid_hash_never_creates_transfer_account(password_hash: str) -> None:
    effects = FakeEffects()
    effects.password_hash = password_hash
    with pytest.raises(RuntimeError, match="password hash unavailable"):
        effects.execute()
    assert all(account.pw_uid != 2001 for account in effects.accounts)
    assert not any(call[:2] == ("chown", "/etc/passwd") for call in effects.calls)


def test_image_context_supplies_locked_downloader_and_owned_root_entrypoint() -> None:
    instructions = DOCKERFILE.read_text().replace("\\\n", "").splitlines()
    copied = [
        path
        for line in instructions
        if line.startswith("COPY ")
        for path in shlex.split(line)[1:-1]
    ]
    assert "requirements-vast-downloader.txt" in copied
    assert (
        "!requirements-vast-downloader.txt"
        in (ROOT / ".dockerignore").read_text().splitlines()
    )
    downloader = next(line for line in instructions if "uv pip install" in line)
    assert "--require-hashes" in shlex.split(downloader)
    final_stage = instructions[instructions.index("FROM scratch") + 1 :]
    assert [line for line in final_stage if line.startswith("USER ")] == ["USER 0:0"]
    entrypoints = [
        line.removeprefix("ENTRYPOINT ")
        for line in final_stage
        if line.startswith("ENTRYPOINT ")
    ]
    assert len(entrypoints) == 1
    assert json.loads(entrypoints[0]) == [
        "/opt/inferdrome-runtime/bin/python",
        "-I",
        "-m",
        "inferdrome.deployment.vast_guest_ssh",
    ]
