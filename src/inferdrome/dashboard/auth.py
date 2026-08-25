"""Strict, local-only bearer keyring for the optional dashboard auth mode."""

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import DashboardAuthError

KEYRING_SCHEMA_VERSION: Literal["inferdrome.dashboard-keyring.v1"] = (
    "inferdrome.dashboard-keyring.v1"
)
TOKEN_VERSION = "inferdrome-dashboard-v1"
TOKEN_SCOPE: Literal["dashboard:read"] = "dashboard:read"
MAX_KEYRING_BYTES = 64 * 1024
MAX_KEY_RECORDS = 32
MAX_TOKEN_BYTES = 512
MAX_LABEL_LENGTH = 64
_KEY_ID_RE = re.compile(r"^dk-[0-9a-f]{16}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(
    rf"^{re.escape(TOKEN_VERSION)}\.(dk-[0-9a-f]{{16}})\.([A-Za-z0-9_-]{{43}})$"
)
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TOKEN_DOMAIN = b"inferdrome.dashboard.bearer-token.v1\0"
_DUMMY_DIGEST = hashlib.sha256(_TOKEN_DOMAIN + b"dummy").hexdigest()


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _canonical_timestamp(value: str) -> str:
    if not _TIMESTAMP_RE.fullmatch(value):
        raise ValueError("timestamp is not canonical")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("timestamp is invalid") from None
    if parsed.tzinfo != UTC or parsed.microsecond != 0:
        raise ValueError("timestamp is invalid")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("timestamp is not canonical")
    return value


def _looks_like_secret(value: str) -> bool:
    return bool(
        _TOKEN_RE.fullmatch(value)
        or re.search(r"(?:sk|ghp|github_pat|AKIA|-----BEGIN)[-_A-Za-z0-9]{20,}", value)
        or re.fullmatch(r"[A-Za-z0-9_-]{40,}", value)
    )


class DashboardKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["inferdrome.dashboard-key.v1"]
    key_id: Annotated[str, Field(min_length=19, max_length=19)]
    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LENGTH)]
    scope: Literal["dashboard:read"]
    token_digest: Annotated[str, Field(min_length=71, max_length=71)]
    created_at: str
    revoked_at: str | None = None
    status: Literal["active", "revoked"]

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not _KEY_ID_RE.fullmatch(value):
            raise ValueError("key id is invalid")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not _LABEL_RE.fullmatch(value) or _looks_like_secret(value):
            raise ValueError("label is invalid")
        return value

    @field_validator("token_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("token digest is invalid")
        return value

    @field_validator("created_at", "revoked_at")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_timestamp(value)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "active" and self.revoked_at is not None:
            raise ValueError("active key cannot have a revocation timestamp")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked key requires a revocation timestamp")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revocation precedes creation")
        return self


class DashboardKeyring(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["inferdrome.dashboard-keyring.v1"]
    keys: tuple[DashboardKeyRecord, ...]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        if len(self.keys) > MAX_KEY_RECORDS:
            raise ValueError("keyring contains too many records")
        identifiers = [record.key_id for record in self.keys]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("keyring contains duplicate key ids")
        return self


class _KeyringOperation(Protocol):
    label: str

    def apply(
        self,
        keyring: DashboardKeyring,
        record: DashboardKeyRecord,
        now: str,
    ) -> DashboardKeyring: ...


def _keyring_bytes(keyring: DashboardKeyring) -> bytes:
    return canonical_json_bytes(keyring.model_dump(mode="json", exclude_none=False))


def _parse_keyring(raw: bytes) -> DashboardKeyring:
    if len(raw) > MAX_KEYRING_BYTES:
        raise DashboardAuthError("dashboard keyring is unavailable")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise ValueError("keyring root is invalid")
        # The duplicate-key preflight above establishes one unambiguous JSON
        # object; the JSON validation path retains array-to-tuple semantics.
        keyring = DashboardKeyring.model_validate_json(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DashboardAuthError("dashboard keyring is unavailable") from None
    if _keyring_bytes(keyring) != raw:
        raise DashboardAuthError("dashboard keyring is unavailable")
    return keyring


def _token_digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(_TOKEN_DOMAIN + token.encode("ascii")).hexdigest()


def _new_token() -> tuple[str, str]:
    key_id = "dk-" + secrets.token_hex(8)
    secret = (
        base64.urlsafe_b64encode(secrets.token_bytes(32))
        .decode("ascii")
        .rstrip("=")
    )
    token = f"{TOKEN_VERSION}.{key_id}.{secret}"
    if len(token.encode("ascii")) > MAX_TOKEN_BYTES:
        raise DashboardAuthError("dashboard token generation failed")
    return key_id, token


def _timestamp_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_path(path: Path) -> Path:
    if not path.is_absolute():
        path = path.absolute()
    # Do not silently normalize traversal aliases.  The parent check below
    # also compares the lexical and strict-resolved paths, but rejecting an
    # explicit traversal component keeps the path boundary unambiguous when
    # a component is created concurrently.
    if any(part in {".", ".."} for part in path.parts):
        raise DashboardAuthError("dashboard keyring path is invalid")
    if path.name in {"", ".", ".."} or "/" in path.name or "\\" in path.name:
        raise DashboardAuthError("dashboard keyring path is invalid")
    return path


class DashboardKeyringStore:
    """A small locked, no-follow, canonical JSON keyring store."""

    def __init__(self, path: Path) -> None:
        self.path = _safe_path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def _parent(self) -> Path:
        parent = self.path.parent
        try:
            metadata = parent.lstat()
            resolved_parent = parent.resolve(strict=True)
        except OSError:
            raise DashboardAuthError(
                "dashboard keyring storage is unavailable"
            ) from None
        except RuntimeError:
            raise DashboardAuthError(
                "dashboard keyring storage is unavailable"
            ) from None
        if (
            resolved_parent != parent
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise DashboardAuthError("dashboard keyring storage is unavailable")
        return parent

    @contextmanager
    def _locked(self) -> Iterator[Path]:
        parent = self._parent()
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            raise DashboardAuthError("dashboard keyring lock is unavailable") from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(
                metadata.st_mode
            ) != 0o600:
                raise DashboardAuthError("dashboard keyring lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield parent
        except DashboardAuthError:
            raise
        except OSError:
            raise DashboardAuthError("dashboard keyring lock is unavailable") from None
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_unlocked(self, *, allow_missing: bool = False) -> DashboardKeyring:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            if allow_missing:
                return DashboardKeyring(
                    schema_version=KEYRING_SCHEMA_VERSION,
                    keys=(),
                )
            raise DashboardAuthError("dashboard keyring is unavailable") from None
        except OSError:
            raise DashboardAuthError("dashboard keyring is unavailable") from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_KEYRING_BYTES
            ):
                raise DashboardAuthError("dashboard keyring is unavailable")
            raw = os.read(descriptor, MAX_KEYRING_BYTES + 1)
            final = os.fstat(descriptor)
            if final.st_ino != metadata.st_ino or final.st_size != len(raw):
                raise DashboardAuthError("dashboard keyring is unavailable")
        except DashboardAuthError:
            raise
        except OSError:
            raise DashboardAuthError("dashboard keyring is unavailable") from None
        finally:
            os.close(descriptor)
        return _parse_keyring(raw)

    def load(self) -> DashboardKeyring:
        with self._locked():
            return self._read_unlocked()

    def _publish_unlocked(self, keyring: DashboardKeyring, parent: Path) -> None:
        raw = _keyring_bytes(keyring)
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError:
            raise DashboardAuthError("dashboard keyring is unavailable") from None
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DashboardAuthError("dashboard keyring is unavailable")
        stage = parent / f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.stage"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                stage,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            written = 0
            while written < len(raw):
                count = os.write(descriptor, raw[written:])
                if count <= 0:
                    raise OSError("short keyring write")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(stage, self.path)
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if self._read_unlocked() != keyring:
                raise DashboardAuthError("dashboard keyring publication failed")
        except DashboardAuthError:
            raise
        except OSError:
            raise DashboardAuthError("dashboard keyring publication failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                stage.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _mutate(self, operation: _KeyringOperation) -> tuple[str, DashboardKeyRecord]:
        with self._locked() as parent:
            keyring = self._read_unlocked(allow_missing=True)
            token_id, token = _new_token()
            now = _timestamp_now()
            try:
                record = DashboardKeyRecord(
                    schema_version="inferdrome.dashboard-key.v1",
                    key_id=token_id,
                    label=operation.label,
                    scope=TOKEN_SCOPE,
                    token_digest=_token_digest(token),
                    created_at=now,
                    status="active",
                )
                updated = operation.apply(keyring, record, now)
            except DashboardAuthError:
                raise
            except (TypeError, ValueError):
                raise DashboardAuthError(
                    "dashboard keyring mutation was rejected"
                ) from None
            self._publish_unlocked(updated, parent)
            return token, record

    def create(self, label: str) -> tuple[str, DashboardKeyRecord]:
        class Create:
            def __init__(self, value: str) -> None:
                self.label = value

            @staticmethod
            def apply(
                keyring: DashboardKeyring,
                record: DashboardKeyRecord,
                _: str,
            ) -> DashboardKeyring:
                return DashboardKeyring(
                    schema_version=KEYRING_SCHEMA_VERSION,
                    keys=(*keyring.keys, record),
                )

        return self._mutate(Create(label))

    def rotate(self, revoke_key_id: str, label: str) -> tuple[str, DashboardKeyRecord]:
        class Rotate:
            def __init__(self, value: str, old_id: str) -> None:
                self.label = value
                self.old_id = old_id

            def apply(
                self,
                keyring: DashboardKeyring,
                record: DashboardKeyRecord,
                now: str,
            ) -> DashboardKeyring:
                found = False
                keys: list[DashboardKeyRecord] = []
                for current in keyring.keys:
                    if current.key_id == self.old_id:
                        found = True
                        if current.status != "active":
                            raise DashboardAuthError("dashboard key cannot be rotated")
                        keys.append(
                            current.model_copy(
                                update={"status": "revoked", "revoked_at": now}
                            )
                        )
                    else:
                        keys.append(current)
                if not found:
                    raise DashboardAuthError("dashboard key cannot be rotated")
                keys.append(record)
                return DashboardKeyring(
                    schema_version=KEYRING_SCHEMA_VERSION,
                    keys=tuple(keys),
                )

        return self._mutate(Rotate(label, revoke_key_id))

    def revoke(self, key_id: str) -> DashboardKeyRecord:
        with self._locked() as parent:
            keyring = self._read_unlocked()
            found: DashboardKeyRecord | None = None
            keys: list[DashboardKeyRecord] = []
            for current in keyring.keys:
                if current.key_id != key_id:
                    keys.append(current)
                    continue
                found = current
                if current.status == "active":
                    try:
                        now = _timestamp_now()
                        if now < current.created_at:
                            raise ValueError("clock precedes key creation")
                        current = DashboardKeyRecord(
                            schema_version=current.schema_version,
                            key_id=current.key_id,
                            label=current.label,
                            scope=current.scope,
                            token_digest=current.token_digest,
                            created_at=current.created_at,
                            revoked_at=now,
                            status="revoked",
                        )
                    except DashboardAuthError:
                        raise
                    except (TypeError, ValueError):
                        raise DashboardAuthError(
                            "dashboard key revocation was rejected"
                        ) from None
                keys.append(current)
            if found is None:
                raise DashboardAuthError("dashboard key cannot be revoked")
            try:
                updated = DashboardKeyring(
                    schema_version=KEYRING_SCHEMA_VERSION,
                    keys=tuple(keys),
                )
            except (TypeError, ValueError):
                raise DashboardAuthError(
                    "dashboard key revocation was rejected"
                ) from None
            if updated != keyring:
                self._publish_unlocked(updated, parent)
                return next(
                    record for record in updated.keys if record.key_id == key_id
                )
            return found

    def list_public(self) -> tuple[dict[str, str | None], ...]:
        keyring = self.load()
        return tuple(
            {
                "key_id": record.key_id,
                "label": record.label,
                "scope": record.scope,
                "created_at": record.created_at,
                "revoked_at": record.revoked_at,
                "status": record.status,
            }
            for record in keyring.keys
        )

    def assert_usable(self) -> None:
        keyring = self.load()
        if not any(record.status == "active" for record in keyring.keys):
            raise DashboardAuthError("dashboard keyring has no active read key")

    def verify(self, token: str) -> bool:
        if not isinstance(token, str) or len(
            token.encode("utf-8", "ignore")
        ) > MAX_TOKEN_BYTES:
            candidate_digest = _DUMMY_DIGEST
            key_id = None
        else:
            match = _TOKEN_RE.fullmatch(token)
            if match is None:
                candidate_digest = _DUMMY_DIGEST
                key_id = None
            else:
                key_id = match.group(1)
                candidate_digest = _token_digest(token).removeprefix("sha256:")
        keyring = self.load()
        expected = _DUMMY_DIGEST
        selected: DashboardKeyRecord | None = None
        for record in keyring.keys:
            if key_id == record.key_id:
                selected = record
                expected = record.token_digest.removeprefix("sha256:")
        equal = hmac.compare_digest(candidate_digest, expected)
        return bool(selected is not None and selected.status == "active" and equal)


def validate_token_shape(token: str) -> bool:
    if not isinstance(token, str) or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in token
    ):
        return False
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        return False
    try:
        secret = base64.urlsafe_b64decode(match.group(2) + "===")
    except (binascii.Error, ValueError):
        return False
    return len(secret) == 32
