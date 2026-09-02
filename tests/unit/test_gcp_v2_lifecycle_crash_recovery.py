"""Fresh-process crash/recovery coverage for additive v2 lifecycle journals.

These tests deliberately re-enter this module through a new pytest interpreter
instead of using ``fork()``.  The child process owns the injected crash point;
the parent only reads durable journals after that interpreter has disappeared.
No test contacts a provider: every transport is the deterministic local fake.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest

from inferdrome.deployment import (
    FakeGcpComputeTransport,
    GcpClock,
    GcpGuardedLifecycleController,
    GcpLeaseJournal,
    InMemoryExecutionArmStore,
)
from inferdrome.deployment.gcp_compute_transport import GcpV2LocalTransportFactory
from inferdrome.deployment.gcp_supervisor import (
    FakeGcpWatchdog,
    FileGcpWatchdog,
    GcpV2LocalWatchdogExecutorFactory,
    _stop_file_watchdog_runners_for_tests,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    FileBackedFakeDiskProvider,
    GcpV2LocalDiskCleanupFactory,
)

ROOT = Path(__file__).resolve().parents[2]
_MODE_ENV = "INFERDROME_V2_LIFECYCLE_CRASH_TEST_MODE"
_ROOT_ENV = "INFERDROME_V2_LIFECYCLE_CRASH_TEST_ROOT"
_STATE_ENV = "INFERDROME_V2_LIFECYCLE_CRASH_TEST_STATE"
_WORKER_CWD_ENV = "INFERDROME_V2_LIFECYCLE_CRASH_TEST_WORKER_CWD"
_CONTROLLER_ID = "ctl-12345678"


@pytest.fixture(autouse=True)
def _stop_local_file_watchdog_runners_after_test() -> Any:
    """Explicit test cleanup; production runner survival is unchanged."""

    # Fresh-child modes deliberately model a controller interpreter exiting.
    # Their parent test owns the exact PID and performs the explicit cleanup;
    # killing it here would recreate the production atexit bug under test.
    if os.environ.get(_MODE_ENV) is not None:
        yield
        return
    try:
        yield
    finally:
        _stop_file_watchdog_runners_for_tests()


@lru_cache(maxsize=1)
def _existing_lifecycle_helpers() -> ModuleType:
    """Load shared local-only v2 fixtures without importing a test package.

    The established lifecycle module owns the sizeable, frozen v1-compatible
    plan/quote/environment fixture.  Loading it here keeps this test focused
    on process boundaries rather than maintaining a second copy of those
    security-sensitive fixture facts.
    """

    path = ROOT / "tests/unit/test_gcp_guarded_lifecycle.py"
    spec = importlib.util.spec_from_file_location(
        "_inferdrome_v2_lifecycle_shared_fixtures", path
    )
    if spec is None or spec.loader is None:
        raise AssertionError("shared lifecycle fixture module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_disk_factory(root: Path, helpers: ModuleType, record: Any) -> Any:
    """Construct a local-only exact-disk factory; it never reaches a cloud."""

    disk_binding = helpers._v2_disk_binding_for_record(record)
    disk_journal_root = root / "disk-journal"
    disk_journal_root.mkdir(mode=0o700, exist_ok=True)
    provider_root = root / "disk-provider"
    provider = (
        FileBackedFakeDiskProvider(provider_root)
        if (provider_root / "state.json").is_file()
        else FileBackedFakeDiskProvider.initialize(
            provider_root, observation=disk_binding.disk
        )
    )
    return (
        GcpV2LocalDiskCleanupFactory(
            provider_supplier=lambda: provider,
            journal_root=disk_journal_root,
            now_fn=lambda: helpers.NOW,
            startup_projection=helpers._v2_startup_projection(helpers._v2_inputs()[-1]),
        ),
        disk_binding,
    )


def _make_crash_topology(root: Path) -> tuple[Any, ...]:
    """Build the v2 local-fake route up to its post-insert disk binding."""

    helpers = _existing_lifecycle_helpers()
    inputs = helpers._v2_inputs()
    startup_projection = helpers._v2_startup_projection(inputs[-1])
    seed_root = root / "seed-core"
    seed_root.mkdir(mode=0o700)
    _seed_outcome, seeded = helpers._run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    seed_record = seeded.journal.load(_CONTROLLER_ID)
    disk_factory, disk_binding = _make_disk_factory(root, helpers, seed_record)
    core_root = root / "core"
    core_root.mkdir(mode=0o700)
    watchdog_root = root / "watchdog"
    watchdog_root.mkdir(mode=0o700)
    raw_transport = FakeGcpComputeTransport(
        exact_boot_disk_observation=disk_binding.disk,
        exact_boot_disk_inventory=disk_binding.owned_inventory,
        exact_owned_instance_observation=disk_binding.attached_instance,
    )
    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=lambda: raw_transport,
        now_fn=lambda: helpers.NOW,
        startup_projection=startup_projection,
    )
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=transport_factory,
            disk_cleanup_factory=disk_factory,
            core_journal=GcpLeaseJournal(core_root),
            now_fn=lambda: helpers.NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: helpers.NOW,
    )
    supervisor, approval = helpers._v2_supervisor(
        root / "safety", inputs[-1], watchdog=watchdog
    )
    controller = GcpGuardedLifecycleController(
        activation_transport_factory=transport_factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: helpers.NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
        exact_disk_cleanup_factory=disk_factory,
    )
    return (
        helpers,
        inputs,
        approval,
        controller,
        core_root,
        watchdog_root,
        disk_factory,
        transport_factory,
    )


def _crash_after_successful_insert(root: Path) -> NoReturn:
    """Die at the binding factory after the local fake insert has completed."""

    (
        helpers,
        inputs,
        _approval,
        controller,
        _core_root,
        _watchdog_root,
        disk_factory,
        _transport_factory,
    ) = _make_crash_topology(root)
    marker = root / "post-insert-before-disk-binding.marker"

    def die_after_exact_observation(**_: Any) -> NoReturn:
        # ``bind_inventory`` is reachable only after create's operation is
        # terminal, instance ownership is checked, and the exact boot-disk
        # read/inventory has completed.  Do not serialize that observation:
        # this is precisely the unbound crash window under test.
        marker.write_text("reached", encoding="ascii")
        os._exit(77)

    disk_factory.bind_inventory = die_after_exact_observation
    helpers._execute_v2(controller, inputs)
    raise AssertionError("crash injection returned")


def _restart_after_unbound_crash(root: Path) -> None:
    """Prove a new interpreter records only the permitted prebind orphan."""

    (
        helpers,
        _inputs,
        _approval,
        controller,
        core_root,
        watchdog_root,
        disk_factory,
        transport_factory,
    ) = _make_crash_topology_for_restart(root)
    record = controller.journal.load(_CONTROLLER_ID)
    # The controller died after the terminal create operation had been
    # journaled, but before any disk binding or disk intent could be durable.
    assert record.state == "CREATE_SUBMITTED"
    assert record.provider_mutation_attempted is True
    assert record.provider_operation_terminal is True
    assert record.cleanup_confirmed is False

    restarted = FileGcpWatchdog(
        watchdog_root,
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=transport_factory,
            disk_cleanup_factory=disk_factory,
            core_journal=GcpLeaseJournal(core_root),
            now_fn=lambda: helpers.NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: helpers.NOW,
    )
    restarted.configure_lease(record)
    runner_pid: int | None = None
    try:
        with restarted._exclusive() as descriptor_root:
            latest = restarted._read_locked(descriptor_root, _CONTROLLER_ID)[-1]
            runner_pid = latest.runner_process_id
            due = helpers.datetime.fromisoformat(
                str(latest.provider_runtime_deadline_at).replace("Z", "+00:00")
            )
        # The crash child's runner is an independent process; kill only this
        # local test runner before the restart process drives its durable
        # state. Its PID is never used as a provider ownership fact.
        if runner_pid is not None:
            restarted._kill_runner(runner_pid)
            runner_pid = None
        terminal = restarted.run_due(controller_id=_CONTROLLER_ID, now=due)
        assert terminal.state == "ORPHANED"
        assert terminal.error_code == "WATCHDOG_DISK_BINDING_UNRESOLVED"
        assert terminal.disk_cleanup_binding is None
        assert terminal.cleanup_result is None
        assert terminal.prebind_cleanup_result is not None
        assert terminal.prebind_cleanup_result.instance_observation.state == "NOT_FOUND"
        assert terminal.prebind_cleanup_result.owned_inventory.instances == ()
    finally:
        if runner_pid is not None:
            restarted._kill_runner(runner_pid)

    # No unbound disk name may be guessed into either cleanup journal or fake
    # provider state. The known test fake is deliberately left untouched.
    assert not (
        root / "disk-journal" / f"{_CONTROLLER_ID}.disk-cleanup-v2.events.jsonl"
    ).exists()
    provider_state = (root / "disk-provider" / "state.json").read_text(
        encoding="utf-8"
    )
    assert '"delete_calls":0' in provider_state
    (root / "restart-result.json").write_text(
        json.dumps(
            {
                "core_state": record.state,
                "event_state": terminal.state,
                "error_code": terminal.error_code,
                "disk_binding": terminal.disk_cleanup_binding is not None,
                "prebind_absent": (
                    terminal.prebind_cleanup_result is not None
                    and terminal.prebind_cleanup_result.instance_observation.state
                    == "NOT_FOUND"
                ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _make_crash_topology_for_restart(root: Path) -> tuple[Any, ...]:
    """Reopen the durable post-crash topology without using child memory."""

    helpers = _existing_lifecycle_helpers()
    inputs = helpers._v2_inputs()
    startup_projection = helpers._v2_startup_projection(inputs[-1])
    core_root = root / "core"
    record = GcpLeaseJournal(core_root).load(_CONTROLLER_ID)
    disk_factory, _disk_binding = _make_disk_factory(root, helpers, record)
    raw_transport = FakeGcpComputeTransport()
    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=lambda: raw_transport,
        now_fn=lambda: helpers.NOW,
        startup_projection=startup_projection,
    )
    watchdog_root = root / "watchdog"
    # Constructing this supervisor is intentionally local; the actual
    # restart test drives only its independently durable watchdog journal.
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=transport_factory,
            disk_cleanup_factory=disk_factory,
            core_journal=GcpLeaseJournal(core_root),
            now_fn=lambda: helpers.NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: helpers.NOW,
    )
    supervisor, approval = helpers._v2_supervisor(
        root / "restart-safety", inputs[-1], watchdog=watchdog
    )
    controller = GcpGuardedLifecycleController(
        activation_transport_factory=transport_factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: helpers.NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
        exact_disk_cleanup_factory=disk_factory,
    )
    return (
        helpers,
        inputs,
        approval,
        controller,
        core_root,
        watchdog_root,
        disk_factory,
        transport_factory,
    )


def _activate_detached_file_watchdog(root: Path) -> int:
    """Activate one local fake watchdog, then intentionally return normally.

    The caller owns no process-global cleanup hook.  The durable event records
    the new runner PID, which lets the parent test prove that a normal
    controller interpreter exit does not revoke independently durable cleanup.
    """

    helpers = _existing_lifecycle_helpers()
    inputs = helpers._v2_inputs()
    record = helpers._prepared_record(root / "prepared")
    core_root = root / "core"
    core_root.mkdir(mode=0o700)
    core_journal = GcpLeaseJournal(core_root)
    core_journal.reserve(record)
    startup_projection = helpers._v2_startup_projection(inputs[-1])
    disk_factory, disk_binding = _make_disk_factory(root, helpers, record)
    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=FakeGcpComputeTransport,
        now_fn=lambda: helpers.NOW,
        startup_projection=startup_projection,
    )
    watchdog_root = root / "watchdog"
    watchdog_root.mkdir(mode=0o700)
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=transport_factory,
            disk_cleanup_factory=disk_factory,
            core_journal=core_journal,
            now_fn=lambda: helpers.NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: helpers.NOW,
    )
    supervisor, approval = helpers._v2_supervisor(
        root / "safety", inputs[-1], watchdog=watchdog
    )
    binding = helpers.gcp_supervisor_binding(approval, record)
    capability = helpers.issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=helpers.gcp_execution_approval_digest(approval),
        now=helpers.NOW,
    )
    watchdog.configure_lease(record)
    watchdog.arm(binding, approval=approval, now=helpers.NOW)
    # Test-only hostile directory: the subprocess test itself still starts in
    # the repository so its imports remain deterministic.  Only the watchdog
    # parent changes cwd immediately before spawning its fresh worker.
    hostile_cwd = os.environ.get(_WORKER_CWD_ENV)
    if hostile_cwd is not None:
        os.chdir(hostile_cwd)
    watchdog.activate(
        binding, approval=approval, capability=capability, now=helpers.NOW
    )
    watchdog.bind_disk_cleanup(
        binding, disk_cleanup_binding=disk_binding, now=helpers.NOW
    )
    with watchdog._exclusive() as descriptor_root:
        latest = watchdog._read_locked(descriptor_root, _CONTROLLER_ID)[-1]
    assert latest.state == "DISK_BOUND"
    assert latest.runner_process_id is not None
    (root / "normal-exit-activation.json").write_text(
        json.dumps({"runner_pid": latest.runner_process_id}, sort_keys=True),
        encoding="utf-8",
    )
    return latest.runner_process_id


def _restart_detached_file_watchdog(root: Path) -> None:
    """Reopen a normal-exit sidecar and settle its exact local fake cleanup."""

    helpers = _existing_lifecycle_helpers()
    inputs = helpers._v2_inputs()
    core_root = root / "core"
    record = GcpLeaseJournal(core_root).load(_CONTROLLER_ID)
    startup_projection = helpers._v2_startup_projection(inputs[-1])
    disk_factory, _disk_binding = _make_disk_factory(root, helpers, record)
    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=FakeGcpComputeTransport,
        now_fn=lambda: helpers.NOW,
        startup_projection=startup_projection,
    )
    watchdog = FileGcpWatchdog(
        root / "watchdog",
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=transport_factory,
            disk_cleanup_factory=disk_factory,
            core_journal=GcpLeaseJournal(core_root),
            now_fn=lambda: helpers.NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: helpers.NOW,
    )
    watchdog.configure_lease(record)
    with watchdog._exclusive() as descriptor_root:
        latest = watchdog._read_locked(descriptor_root, _CONTROLLER_ID)[-1]
    assert latest.runner_process_id is not None
    # This is a local test runner whose exact PID is bound in the durable test
    # journal.  Production restart leaves a healthy independent runner alone;
    # the test stops it so this process can deterministically own the due run.
    watchdog._kill_runner(latest.runner_process_id)
    assert latest.mutation_capability is not None
    due = helpers.datetime.fromisoformat(
        str(latest.mutation_capability.provider_runtime_deadline_at).replace(
            "Z", "+00:00"
        )
    )
    hostile_cwd = os.environ.get(_WORKER_CWD_ENV)
    if hostile_cwd is not None:
        os.chdir(hostile_cwd)
    terminal = watchdog.run_due(controller_id=_CONTROLLER_ID, now=due)
    assert terminal.state == "CLEANUP_CONFIRMED"
    assert terminal.cleanup_result is not None
    assert terminal.cleanup_result.instance_observation.state == "NOT_FOUND"
    assert terminal.cleanup_result.owned_inventory.instances == ()
    assert terminal.cleanup_result.disk_cleanup_outcome.state == "ABSENCE_CONFIRMED"
    (root / "normal-exit-restart.json").write_text(
        json.dumps(
            {
                "disk_absence": terminal.cleanup_result.disk_cleanup_outcome.state,
                "instance_absence": terminal.cleanup_result.instance_observation.state,
                "state": terminal.state,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _stop_local_test_runner(runner_pid: int) -> None:
    """Best-effort explicit cleanup for one detached local test process."""

    try:
        os.killpg(runner_pid, signal.SIGKILL)
    except (AttributeError, OSError):
        with suppress(OSError):
            os.kill(runner_pid, signal.SIGKILL)


def _seed_journal_crash(root: Path, state: str) -> NoReturn:
    """Fsync one core state, then die before its sidecar can reconcile."""

    helpers = _existing_lifecycle_helpers()
    record = helpers._prepared_record(root / "prepared")
    core_root = root / "core"
    core_root.mkdir(mode=0o700)
    journal = GcpLeaseJournal(core_root)
    journal.reserve(record)
    if state == "ARM_CONSUMED":
        record = record.model_copy(
            update={"state": "ARM_CONSUMED", "arm_consumed": True}
        )
        journal.update(record)
    elif state == "CLEANUP_CONFIRMED":
        record = record.model_copy(
            update={
                "state": "CLEANUP_CONFIRMED",
                "cleanup_confirmed": True,
                "orphaned": False,
                "last_error_code": "NO_PROVIDER_MUTATION",
            }
        )
        journal.update(record)
    elif state != "PREPARED":
        raise AssertionError(f"unexpected local state {state!r}")
    (root / "journal-state.marker").write_text(state, encoding="ascii")
    os._exit(78)


def _restart_journal_reconciliation(root: Path, state: str) -> None:
    """Use a clean interpreter to reconcile both journals without transport."""

    helpers = _existing_lifecycle_helpers()
    inputs = helpers._v2_inputs()
    core_root = root / "core"
    core_journal = GcpLeaseJournal(core_root)
    transport = FakeGcpComputeTransport()
    watchdog = FakeGcpWatchdog()
    supervisor, _approval = helpers._v2_supervisor(
        root / "safety", inputs[-1], watchdog=watchdog
    )
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=core_journal,
        clock=GcpClock(now_fn=lambda: helpers.NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )
    recovered = controller.recover_cleanup(controller_id=_CONTROLLER_ID)
    core = core_journal.load(_CONTROLLER_ID)
    sidecar = supervisor.journal.load(_CONTROLLER_ID)
    assert core.state == "CLEANUP_CONFIRMED"
    assert sidecar.state == "CLEANUP_CONFIRMED"
    assert transport.insert_calls == 0
    assert transport.delete_calls == 0
    assert watchdog.activation_calls == 0
    expected = (
        "CLEANUP_ALREADY_CONFIRMED"
        if state == "CLEANUP_CONFIRMED"
        else "NO_PROVIDER_MUTATION_RECOVERED"
    )
    assert recovered.primary_error_code == expected
    (root / "journal-restart-result.json").write_text(
        json.dumps(
            {
                "state": state,
                "core": core.state,
                "sidecar": sidecar.state,
                "result": recovered.primary_error_code,
                "insert_calls": transport.insert_calls,
                "delete_calls": transport.delete_calls,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _run_child(
    mode: str,
    root: Path,
    *,
    state: str | None = None,
    worker_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Start the exact test node in a fresh interpreter with no cloud inputs."""

    environment = dict(os.environ)
    source = os.fspath(ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not existing else f"{source}{os.pathsep}{existing}"
    )
    environment[_MODE_ENV] = mode
    environment[_ROOT_ENV] = os.fspath(root)
    environment["PYDANTIC_DISABLE_PLUGINS"] = "__all__"
    if state is not None:
        environment[_STATE_ENV] = state
    else:
        environment.pop(_STATE_ENV, None)
    if worker_cwd is not None:
        environment[_WORKER_CWD_ENV] = os.fspath(worker_cwd)
    else:
        environment.pop(_WORKER_CWD_ENV, None)
    return subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-m",
            "pytest",
            "-q",
            f"{Path(__file__)}::test_v2_fresh_process_crash_and_journal_recovery",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )


def test_v2_fresh_process_crash_and_journal_recovery(tmp_path: Path) -> None:
    """Exercise create/binding and cross-journal crashes through fresh execs."""

    mode = os.environ.get(_MODE_ENV)
    root_value = os.environ.get(_ROOT_ENV)
    if mode is not None:
        if root_value is None:
            raise AssertionError("crash child has no durable root")
        root = Path(root_value)
        state = os.environ.get(_STATE_ENV)
        if mode == "post-insert-crash":
            _crash_after_successful_insert(root)
        if mode == "post-insert-restart":
            _restart_after_unbound_crash(root)
            return
        if mode == "journal-seed":
            if state is None:
                raise AssertionError("journal seed state is missing")
            _seed_journal_crash(root, state)
        if mode == "journal-restart":
            if state is None:
                raise AssertionError("journal restart state is missing")
            _restart_journal_reconciliation(root, state)
            return
        if mode == "normal-exit-activate":
            _activate_detached_file_watchdog(root)
            return
        if mode == "normal-exit-restart":
            _restart_detached_file_watchdog(root)
            return
        raise AssertionError(f"unknown child mode {mode!r}")

    crash_root = tmp_path / "post-insert"
    crash_root.mkdir(mode=0o700)
    crashed = _run_child("post-insert-crash", crash_root)
    assert crashed.returncode == 77, crashed.stderr
    assert (crash_root / "post-insert-before-disk-binding.marker").read_text(
        encoding="ascii"
    ) == "reached"
    restarted = _run_child("post-insert-restart", crash_root)
    assert restarted.returncode == 0, restarted.stderr
    restart_summary = json.loads(
        (crash_root / "restart-result.json").read_text(encoding="utf-8")
    )
    assert restart_summary == {
        "core_state": "CREATE_SUBMITTED",
        "disk_binding": False,
        "error_code": "WATCHDOG_DISK_BINDING_UNRESOLVED",
        "event_state": "ORPHANED",
        "prebind_absent": True,
    }

    for state in ("PREPARED", "ARM_CONSUMED", "CLEANUP_CONFIRMED"):
        journal_root = tmp_path / f"journal-{state.lower()}"
        journal_root.mkdir(mode=0o700)
        crashed = _run_child("journal-seed", journal_root, state=state)
        assert crashed.returncode == 78, crashed.stderr
        assert (journal_root / "journal-state.marker").read_text(
            encoding="ascii"
        ) == state
        restarted = _run_child("journal-restart", journal_root, state=state)
        assert restarted.returncode == 0, restarted.stderr
        summary = json.loads(
            (journal_root / "journal-restart-result.json").read_text(
                encoding="utf-8"
            )
        )
        assert summary["core"] == "CLEANUP_CONFIRMED"
        assert summary["sidecar"] == "CLEANUP_CONFIRMED"
        assert summary["insert_calls"] == 0
        assert summary["delete_calls"] == 0


def test_v2_normal_controller_exit_preserves_exec_isolated_watchdog_and_safe_import(
    tmp_path: Path,
) -> None:
    """A normal controller exit cannot kill or shadow the durable worker.

    The hostile cwd supplies a syntactically valid shadow module.  The outer
    pytest interpreter remains in the repository; each watchdog child must
    independently make the safe import choice.  A legacy inherited-cwd worker
    would write the marker and fail readiness.
    """

    root = tmp_path / "normal-exit"
    root.mkdir(mode=0o700)
    hostile_cwd = tmp_path / "hostile-cwd"
    shadow_worker = hostile_cwd / "inferdrome" / "deployment"
    shadow_worker.mkdir(parents=True, mode=0o700)
    (hostile_cwd / "inferdrome" / "__init__.py").write_text(
        "", encoding="ascii"
    )
    (shadow_worker / "__init__.py").write_text("", encoding="ascii")
    shadow_marker = tmp_path / "hostile-worker-imported"
    (shadow_worker / "gcp_watchdog_worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({os.fspath(shadow_marker)!r}).write_text(\n"
        "    'shadowed', encoding='ascii'\n"
        ")\n",
        encoding="utf-8",
    )

    activated = _run_child(
        "normal-exit-activate", root, worker_cwd=hostile_cwd
    )
    assert activated.returncode == 0, activated.stderr
    summary = json.loads(
        (root / "normal-exit-activation.json").read_text(encoding="utf-8")
    )
    runner_pid = int(summary["runner_pid"])
    try:
        # The controller child returned normally, so this is stronger than the
        # SIGKILL-only crash coverage: its independent process group remains
        # alive until its own durable state reaches a terminal condition.
        os.kill(runner_pid, 0)
        assert not shadow_marker.exists()

        restarted = _run_child(
            "normal-exit-restart", root, worker_cwd=hostile_cwd
        )
        assert restarted.returncode == 0, restarted.stderr
        assert not shadow_marker.exists()
        assert json.loads(
            (root / "normal-exit-restart.json").read_text(encoding="utf-8")
        ) == {
            "disk_absence": "ABSENCE_CONFIRMED",
            "instance_absence": "NOT_FOUND",
            "state": "CLEANUP_CONFIRMED",
        }
    finally:
        # Keep teardown explicit: no module-level atexit hook is permitted to
        # terminate a production watchdog merely because a controller exits.
        _stop_local_test_runner(runner_pid)
