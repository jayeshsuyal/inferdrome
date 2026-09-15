"""Public synthetic keys, fake HTTP, private temporary pins; no network or SSH."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import struct
import threading
import time
from pathlib import Path

import pytest

from inferdrome.deployment import vast_ssh_trust as trust
from inferdrome.deployment.vast_control import (
    ControlFailure,
    ControlIntent,
    ControlJournal,
    CreateResult,
    _bounded_call,
)
from inferdrome.deployment.vast_provider import (
    HttpReply,
    VastProvider,
    compiled_create_sha256,
)
from tests.unit.test_vast_bootstrap import launch_intent

NONCE = "a" * 32
URL = "https://s3.amazonaws.com/vast.ai/instance_logs/synthetic_123-Ab.log"


def key(character: bytes = b"k") -> str:
    wire = (
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + character * 32
    )
    return "ssh-ed25519 " + base64.b64encode(wire).decode("ascii")


def marker(*, instance_id: int = 123, nonce: str = NONCE, public: str = key()) -> bytes:
    return trust.format_announcement(instance_id, nonce, public)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def test_marker_round_trip_public_key_fingerprint_and_identical_repetition() -> None:
    line = marker()
    record = trust.parse_log_announcement(
        b"ordinary startup output\n" + line + line,
        instance_id=123,
        run_nonce=NONCE,
    )
    assert record == trust.HostKeyAnnouncement(123, NONCE, key())
    assert record.host_key_sha256 == (
        "sha256:" + hashlib.sha256(base64.b64decode(key().split()[1])).hexdigest()
    )
    assert line.endswith(b"\n")
    assert len(line) < trust.MAX_ANNOUNCEMENT_BYTES


@pytest.mark.parametrize(
    "instance_id", [True, False, 0, -1, "123", 1.2, 9_007_199_254_740_992]
)
def test_exact_positive_integer_id_required(instance_id: object) -> None:
    with pytest.raises(trust.SshTrustFailure, match="BINDING_INVALID"):
        trust.format_announcement(instance_id, NONCE, key())  # type: ignore[arg-type]


@pytest.mark.parametrize("nonce", ["", "a" * 31, "a" * 33, "A" * 32, "../bad", 123])
def test_fresh_nonce_has_closed_shape(nonce: object) -> None:
    with pytest.raises(trust.SshTrustFailure, match="BINDING_INVALID"):
        trust.format_announcement(123, nonce, key())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "public",
    [
        key() + " comment", key() + "\n", key().replace(" ", "  "),
        "ssh-rsa " + key().split()[1], "ssh-ed25519 !!!", "-----BEGIN PRIVATE KEY-----",
        "ssh-ed25519 " + base64.b64encode(b"x" * 51).decode(),
        "ssh-ed25519 " + base64.b64encode(b"x" * 52).decode(),
        None,
    ],
)
def test_only_canonical_ed25519_wire_key_is_accepted(public: object) -> None:
    with pytest.raises(trust.SshTrustFailure, match="KEY_INVALID"):
        trust.format_announcement(123, NONCE, public)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"no marker\n", "MARKER_MISSING"),
        (marker()[:-1], "MARKER_INVALID"),
        (b"timestamp " + marker(), "MARKER_INVALID"),
        (marker().replace(b"\n", b"\r\n"), "RECORD_INVALID"),
        (trust.ANNOUNCEMENT_PREFIX + b"x" * 1024 + b"\n", "MARKER_INVALID"),
        (b"x" * (trust.MAX_LOG_BYTES + 1), "LOG_LIMIT"),
        (marker(instance_id=124), "STALE_OR_REPLAYED"),
        (marker(nonce="b" * 32), "STALE_OR_REPLAYED"),
        (marker() + marker(nonce="b" * 32), "STALE_OR_REPLAYED"),
        (marker() + marker(public=key(b"z")), "CONFLICTING_KEYS"),
    ],
)
def test_absent_partial_stale_replayed_conflicting_logs_fail_closed(
    content: bytes, code: str
) -> None:
    with pytest.raises(trust.SshTrustFailure, match=code):
        trust.parse_log_announcement(content, instance_id=123, run_nonce=NONCE)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.replace(b'"instance_id":123', b'"instance_id":true'),
        lambda raw: raw.replace(b'"instance_id":123', b'"instance_id":"123"'),
        lambda raw: raw.replace(b'"instance_id":123', b'"instance_id":123.0'),
        lambda raw: raw.replace(
            b'"instance_id":123', b'"instance_id":123,"instance_id":123'
        ),
        lambda raw: raw.replace(b'"instance_id":123', b'"extra":1,"instance_id":123'),
        lambda raw: raw.replace(b'"instance_id":123', b'"instance_id": 123'),
        lambda raw: raw.replace(b"inferdrome.vast-ssh-host-key.v1", b"wrong"),
    ],
)
def test_noncanonical_or_ambiguous_json_never_enrolls(mutate: object) -> None:
    assert callable(mutate)
    with pytest.raises(trust.SshTrustFailure, match="RECORD_INVALID"):
        trust.parse_log_announcement(mutate(marker()), instance_id=123, run_nonce=NONCE)


@pytest.mark.parametrize(
    "url",
    [
        "http://s3.amazonaws.com/vast.ai/instance_logs/a.log",
        "https://s3.amazonaws.com:443/vast.ai/instance_logs/a.log",
        "https://user@s3.amazonaws.com/vast.ai/instance_logs/a.log",
        "https://s3.amazonaws.com.evil.invalid/vast.ai/instance_logs/a.log",
        "https://s3.amazonaws.com/vast.ai/instance_logs/../a.log",
        "https://s3.amazonaws.com/vast.ai/instance_logs/%61.log",
        "https://s3.amazonaws.com/vast.ai/instance_logs/a/b.log",
        "https://s3.amazonaws.com/other/instance_logs/a.log",
        URL + "?token=secret", URL + "#fragment", URL + "\n",
        URL.replace("s3.amazonaws.com", "127.0.0.1"),
        "https://s3.amazonaws.com/vast.ai/instance_logs/" + "x" * 129 + ".log",
        None,
    ],
)
def test_log_url_rejects_all_alternate_authorities_and_paths(url: object) -> None:
    with pytest.raises(trust.SshTrustFailure, match="URL_FORBIDDEN"):
        trust.validate_result_url(url)  # type: ignore[arg-type]


class Response:
    def __init__(
        self, content: bytes = marker(), *, status: int = 200,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.content = content
        self.status = status
        self.headers = headers or []
        self.closed = False
        self.read_sizes: list[int] = []

    def getheaders(self) -> list[tuple[str, str]]:
        return self.headers

    def read(self, amt: int) -> bytes:
        self.read_sizes.append(amt)
        result, self.content = self.content[:amt], self.content[amt:]
        return result

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self, response: Response | None = None) -> None:
        self.response = response or Response()
        self.closed = False
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, url, headers))

    def getresponse(self) -> Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_public_get_has_no_credentials_proxy_redirect_or_ambient_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NETRC", "VAST_API_KEY"):
        monkeypatch.setenv(name, "must-never-be-forwarded")
    connection = Connection()
    observed: list[tuple[str, float]] = []

    def factory(host: str, seconds: float) -> Connection:
        observed.append((host, seconds))
        return connection

    assert trust.fetch_log_bytes(URL, seconds=1, connection_factory=factory) == marker()
    assert observed == [("s3.amazonaws.com", 1)]
    assert connection.requests == [
        ("GET", "/vast.ai/instance_logs/synthetic_123-Ab.log",
         {"Accept": "text/plain", "Accept-Encoding": "identity"})
    ]
    assert connection.closed and connection.response.closed


@pytest.mark.parametrize(
    ("status", "headers", "body", "failure"),
    [
        (302, [("Location", "https://evil.invalid")], b"", "HTTP_STATUS"),
        (403, [], b"secret error body", "HTTP_STATUS"),
        (200, [("Content-Encoding", "gzip")], b"", "ENCODING_FORBIDDEN"),
        (200, [("Content-Length", "131073")], b"", "LOG_LIMIT"),
        (200, [("Content-Length", "1"), ("Content-Length", "1")], b"x", "LOG_LIMIT"),
        (200, [("Content-Length", "nan")], b"", "LOG_LIMIT"),
        (200, [("Content-Length", "5")], b"abc", "TRUNCATED"),
        (200, [], b"x" * (trust.MAX_LOG_BYTES + 1), "LOG_LIMIT"),
    ],
)
def test_public_fetch_rejects_redirects_encodings_oversize_and_truncation(
    status: int, headers: list[tuple[str, str]], body: bytes, failure: str
) -> None:
    connection = Connection(Response(body, status=status, headers=headers))
    with pytest.raises(trust.SshTrustFailure, match=failure):
        trust.fetch_log_bytes(URL, seconds=1, connection_factory=lambda *_: connection)
    assert len(connection.requests) == 1
    assert connection.closed and connection.response.closed


@pytest.mark.parametrize("stage", ["connect", "request", "headers", "body"])
def test_whole_call_alarm_bounds_every_fake_blocking_fetch_stage(stage: str) -> None:
    connection = Connection()

    def block(*_args: object, **_kwargs: object) -> None:
        time.sleep(10)

    def factory(_host: str, _seconds: float) -> Connection:
        if stage == "connect":
            block()
        return connection

    if stage == "request":
        connection.request = block  # type: ignore[method-assign]
    elif stage == "headers":
        connection.getresponse = block  # type: ignore[method-assign,assignment]
    elif stage == "body":
        connection.response.read = block  # type: ignore[method-assign,assignment]
    started = time.monotonic()
    with pytest.raises(trust.SshTrustFailure, match="DEADLINE_OR_PLATFORM"):
        trust.fetch_log_bytes(URL, seconds=0.03, connection_factory=factory)
    assert time.monotonic() - started < 0.5
    if stage != "connect":
        assert connection.closed
    if stage == "body":
        assert connection.response.closed


def test_inner_fetch_does_not_renew_enclosing_alarm() -> None:
    def factory(_host: str, _seconds: float) -> Connection:
        time.sleep(10)
        return Connection()

    started = time.monotonic()
    try:
        with pytest.raises((trust.SshTrustFailure, ControlFailure)):
            _bounded_call(
                lambda: trust.fetch_log_bytes(
                    URL, seconds=1, connection_factory=factory
                ),
                0.03,
            )
        assert time.monotonic() - started < 0.5
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_non_main_thread_fails_before_fake_http() -> None:
    failures: list[str] = []
    connections: list[str] = []

    def factory(host: str, _seconds: float) -> Connection:
        connections.append(host)
        return Connection()

    def worker() -> None:
        try:
            trust.fetch_log_bytes(
                URL, seconds=1,
                connection_factory=factory,
            )
        except trust.SshTrustFailure as error:
            failures.append(str(error))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1)
    assert failures == ["VAST_SSH_LOG_DEADLINE_OR_PLATFORM"]
    assert connections == []


@pytest.mark.parametrize("seconds", [True, 0, -1, float("nan"), float("inf"), 601, "1"])
def test_invalid_budgets_do_not_fetch(seconds: object) -> None:
    with pytest.raises(trust.SshTrustFailure, match="BUDGET_INVALID"):
        trust.fetch_log_bytes(URL, seconds=seconds)  # type: ignore[arg-type]


def test_immutable_pin_persists_first_evidence_and_rejects_key_replacement(
    tmp_path: Path,
) -> None:
    with trust.SshPinJournal(tmp_path) as journal:
        assert journal.read(instance_id=123, run_nonce=NONCE) is None
        pin = journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
        path = tmp_path / "instance-123.json"
        original = path.read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
        assert journal.enroll(
            b"new ordinary log\n" + marker(), instance_id=123,
            run_nonce=NONCE, result_url=URL,
        ) == pin
        assert path.read_bytes() == original
        with pytest.raises(trust.SshTrustFailure, match="KEY_REPLACEMENT_FORBIDDEN"):
            journal.enroll(
                marker(public=key(b"z")), instance_id=123,
                run_nonce=NONCE, result_url=URL,
            )
        with pytest.raises(trust.SshTrustFailure, match="PIN_BINDING_MISMATCH"):
            journal.enroll(
                marker(nonce="b" * 32), instance_id=123,
                run_nonce="b" * 32, result_url=URL,
            )
        assert path.read_bytes() == original
    with trust.SshPinJournal(tmp_path) as journal:
        assert journal.read(instance_id=123, run_nonce=NONCE) == pin
    assert set(p.name for p in tmp_path.iterdir()) == {"instance-123.json"}


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public", "corrupt"])
def test_unsafe_or_corrupt_pin_file_cannot_be_adopted(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "instance-123.json"
    with trust.SshPinJournal(tmp_path) as journal:
        journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
    if kind == "symlink":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "public":
        path.chmod(0o644)
    else:
        path.write_bytes(b"{}")
    with (
        trust.SshPinJournal(tmp_path) as journal,
        pytest.raises((OSError, trust.SshTrustFailure)),
    ):
        journal.read(instance_id=123, run_nonce=NONCE)


def test_observed_pin_deletion_or_replacement_fails_without_reenrollment(
    tmp_path: Path,
) -> None:
    with trust.SshPinJournal(tmp_path) as journal:
        journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
        path = tmp_path / "instance-123.json"
        original = path.read_bytes()
        path.unlink()
        with pytest.raises(trust.SshTrustFailure, match="PIN_REPLACED"):
            journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
        value = json.loads(original)
        value["log_sha256"] = "sha256:" + "b" * 64
        path.write_bytes(canonical(value))
        path.chmod(0o600)
        with pytest.raises(trust.SshTrustFailure, match="PIN_REPLACED"):
            journal.read(instance_id=123, run_nonce=NONCE)


def test_nonprivate_or_symlink_journal_root_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o755)
    with pytest.raises(trust.SshTrustFailure, match="JOURNAL_NOT_PRIVATE"):
        trust.SshPinJournal(directory)
    directory.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        trust.SshPinJournal(alias)


def test_provider_exact_id_and_public_fetch_have_separate_arguments(
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Provider:
        def request_logs(self, instance_id: int, *, seconds: float) -> str:
            calls.append(("provider", instance_id, seconds))
            return URL

    def fetcher(result_url: str, *, seconds: float) -> bytes:
        calls.append(("public", result_url, seconds))
        return marker()

    with trust.SshPinJournal(tmp_path) as journal:
        pin = trust.enroll_from_provider(
            Provider(), journal, instance_id=123, run_nonce=NONCE,
            seconds=1, fetcher=fetcher,
        )
    assert pin.announcement.host_public_key == key()
    assert calls[0][:2] == ("provider", 123)
    assert calls[1][:2] == ("public", URL)
    assert 0 < calls[1][2] <= calls[0][2] <= 1  # type: ignore[operator]


def test_provider_cannot_redirect_public_fetch(tmp_path: Path) -> None:
    fetched: list[str] = []

    class Provider:
        def request_logs(self, instance_id: int, *, seconds: float) -> str:
            return "https://evil.invalid/"

    def fetcher(result_url: str, *, seconds: float) -> bytes:
        fetched.append(result_url)
        return marker()

    with trust.SshPinJournal(tmp_path) as journal:
        with pytest.raises(trust.SshTrustFailure, match="URL_FORBIDDEN"):
            trust.enroll_from_provider(
                Provider(), journal, instance_id=123, run_nonce=NONCE,
                seconds=1, fetcher=fetcher,
            )
        assert journal.read(instance_id=123, run_nonce=NONCE) is None
    assert fetched == []


def test_shared_budget_cannot_publish_a_late_fake_fetch(tmp_path: Path) -> None:
    class Provider:
        def request_logs(self, instance_id: int, *, seconds: float) -> str:
            time.sleep(0.015)
            return URL

    def fetcher(result_url: str, *, seconds: float) -> bytes:
        time.sleep(10)
        return marker()

    started = time.monotonic()
    with trust.SshPinJournal(tmp_path) as journal:
        with pytest.raises(trust.SshTrustFailure, match="DEADLINE_OR_PLATFORM"):
            trust.enroll_from_provider(
                Provider(), journal, instance_id=123, run_nonce=NONCE,
                seconds=0.03, fetcher=fetcher,
            )
        assert journal.read(instance_id=123, run_nonce=NONCE) is None
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("different", [False, True])
def test_concurrent_enrollment_cannot_overwrite_first_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, different: bool
) -> None:
    original_link = os.link
    entered = False

    def competing_link(*args: object, **kwargs: object) -> None:
        nonlocal entered
        if not entered:
            entered = True
            with trust.SshPinJournal(tmp_path) as concurrent:
                concurrent.enroll(
                    marker(public=key(b"z") if different else key()),
                    instance_id=123, run_nonce=NONCE, result_url=URL,
                )
        original_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", competing_link)
    with trust.SshPinJournal(tmp_path) as journal:
        if different:
            with pytest.raises(trust.SshTrustFailure, match="REPLACEMENT_FORBIDDEN"):
                journal.enroll(
                    marker(), instance_id=123, run_nonce=NONCE, result_url=URL
                )
        else:
            journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
        retained = journal.read(instance_id=123, run_nonce=NONCE)
        assert retained is not None
        assert retained.announcement.host_public_key == (
            key(b"z") if different else key()
        )
    assert {p.name for p in tmp_path.iterdir()} == {"instance-123.json"}


def test_response_close_failure_still_closes_connection() -> None:
    connection = Connection()

    def failing_close() -> None:
        raise OSError("untrusted failure text")

    connection.response.close = failing_close  # type: ignore[method-assign]
    with pytest.raises(trust.SshTrustFailure, match="FETCH_FAILED") as caught:
        trust.fetch_log_bytes(URL, seconds=1, connection_factory=lambda *_: connection)
    assert str(caught.value) == "VAST_SSH_LOG_FETCH_FAILED"
    assert connection.closed


def test_directory_fsync_failure_rolls_back_pin_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    with trust.SshPinJournal(tmp_path) as journal:
        def fail_directory(descriptor: int) -> None:
            if descriptor == journal._root.fd:
                raise OSError("synthetic durability failure")
            original(descriptor)

        monkeypatch.setattr(os, "fsync", fail_directory)
        with pytest.raises(OSError):
            journal.enroll(marker(), instance_id=123, run_nonce=NONCE, result_url=URL)
        assert journal.read(instance_id=123, run_nonce=NONCE) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ["provider", "fetcher"])
def test_custom_adapter_errors_are_sanitized_without_pin(
    tmp_path: Path, stage: str
) -> None:
    class Provider:
        def request_logs(self, instance_id: int, *, seconds: float) -> str:
            if stage == "provider":
                raise RuntimeError("private operator credential")
            return URL

    def fetcher(result_url: str, *, seconds: float) -> bytes:
        raise RuntimeError("private operator credential")

    with trust.SshPinJournal(tmp_path) as journal:
        with pytest.raises(trust.SshTrustFailure) as caught:
            trust.enroll_from_provider(
                Provider(), journal, instance_id=123, run_nonce=NONCE,
                seconds=1, fetcher=fetcher,
            )
        assert str(caught.value) == "VAST_SSH_ENROLLMENT_FAILED"
        assert journal.read(instance_id=123, run_nonce=NONCE) is None


def test_concrete_provider_retained_id_through_fake_http_to_private_pin(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    class FakeHttp:
        def request(
            self, method: str, path: str, body: bytes | None, *, seconds: float
        ) -> HttpReply:
            assert 0 < seconds <= 1
            calls.append((method, path, body))
            return HttpReply(200, canonical({"success": True, "result_url": URL}))

    launch = launch_intent()
    control = ControlIntent(
        launch_request_sha256=compiled_create_sha256(launch),
        execution_deadline_utc=launch.execution_deadline_utc,
        cleanup_deadline_utc=launch.cleanup_deadline_utc,
    )
    control_dir, pins_dir = tmp_path / "control", tmp_path / "pins"
    control_dir.mkdir(mode=0o700)
    pins_dir.mkdir(mode=0o700)
    control_journal = ControlJournal(control_dir)
    try:
        control_journal.initialize(control)
        control_journal.start_create(control)
        control_journal.retain_created(CreateResult(new_contract=123))
        provider = VastProvider(launch, journal=control_journal, transport=FakeHttp())
        connection = Connection()

        def fetcher(result_url: str, *, seconds: float) -> bytes:
            return trust.fetch_log_bytes(
                result_url, seconds=seconds, connection_factory=lambda *_: connection
            )

        with trust.SshPinJournal(pins_dir) as journal:
            with pytest.raises(trust.SshTrustFailure, match="ENROLLMENT_FAILED"):
                trust.enroll_from_provider(
                    provider, journal, instance_id=124, run_nonce=NONCE,
                    seconds=1, fetcher=fetcher,
                )
            assert calls == [] and connection.requests == []
            pin = trust.enroll_from_provider(
                provider, journal, instance_id=123, run_nonce=NONCE,
                seconds=1, fetcher=fetcher,
            )
            assert pin.announcement.instance_id == 123
            assert pin.announcement.host_public_key == key()
        assert calls == [("PUT", "/api/v0/instances/request_logs/123/", b"{}")]
        assert connection.requests == [
            ("GET", "/vast.ai/instance_logs/synthetic_123-Ab.log",
             {"Accept": "text/plain", "Accept-Encoding": "identity"})
        ]
        stored = (pins_dir / "instance-123.json").read_text()
        assert URL not in stored
        assert "VAST_CONTROL_PLANE_LOG_BINDING_NOT_HARDWARE_ATTESTATION" in stored
    finally:
        control_journal.close()
