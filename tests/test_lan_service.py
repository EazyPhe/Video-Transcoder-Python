from __future__ import annotations

import threading
import secrets
import time
from pathlib import Path

import pytest

import lan_service
import lan_coordinator
import lan_helper_control
import lan_media
from lan_protocol import WorkerRole
from lan_transport import (
    DispatchRejected,
    PublicError,
    TransportError,
    TunnelStatus,
    LoopbackJsonClient,
    LoopbackJsonServer,
)
from lan_worker import WorkerResult


FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"


class _FakeCoordinator:
    systemic_failure = ""

    def snapshot(self):
        return {
            "pending": 0,
            "leased": 0,
            "suspect": 0,
            "committing": 0,
        }


class _IdleWorker:
    def run_one(self):
        return WorkerResult("Idle", "NoPendingJob")


def test_remote_loop_stops_reaper_when_all_work_is_terminal():
    service = object.__new__(lan_service.CoordinatorService)
    service.coordinator = _FakeCoordinator()
    service.remote_worker = _IdleWorker()
    service.stop_event = threading.Event()
    service.last_remote_result = WorkerResult("Idle", "NotStarted")

    service._remote_loop()

    assert service.stop_event.is_set()
    assert service.last_remote_result.kind == "Idle"


def test_remote_loop_stays_live_when_completion_keep_alive_is_enabled():
    class OneWaitStopEvent:
        def __init__(self):
            self.stopped = False
            self.waits = []

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, timeout):
            self.waits.append(timeout)
            self.stopped = True
            return True

    service = object.__new__(lan_service.CoordinatorService)
    service.coordinator = _FakeCoordinator()
    service.remote_worker = _IdleWorker()
    service.stop_event = OneWaitStopEvent()
    service.last_remote_result = WorkerResult("Idle", "NotStarted")
    service.keep_alive_when_complete = True

    service._remote_loop()

    assert service.stop_event.waits == [1.0]
    assert service.last_remote_result.kind == "Idle"


class _ExplodingReaperCoordinator:
    def reap(self):
        raise RuntimeError("synthetic failure")


def test_unexpected_reaper_failure_stops_service():
    service = object.__new__(lan_service.CoordinatorService)
    service.coordinator = _ExplodingReaperCoordinator()
    service.stop_event = threading.Event()
    service.reaper_interval_seconds = 0.001

    service._reaper_loop()

    assert service.stop_event.is_set()


class _Tunnel:
    def __init__(self, *, online: bool):
        self.online = online
        self.starts = 0
        self.stops = 0

    def status(self):
        return TunnelStatus(self.starts > 0, self.online, None)

    def start(self):
        self.starts += 1
        if not self.online:
            raise TransportError(PublicError.TUNNEL_START_FAILED)
        return self

    def ensure_running(self):
        if not self.online:
            raise TransportError(PublicError.TUNNEL_STOPPED)

    def stop(self):
        self.stops += 1


class _Client:
    def __init__(self):
        self.seen = 0

    def helper_seen(self):
        self.seen += 1


class _OneShotWorker:
    def __init__(self, supervisor_box, result):
        self.supervisor_box = supervisor_box
        self.result = result
        self.calls = 0

    def run_one(self):
        self.calls += 1
        self.supervisor_box[0].stop_event.set()
        return self.result


class _ResultWorker:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run_one(self):
        self.calls += 1
        return self.result


def test_offline_supervisor_finishes_one_fallback_transaction():
    supervisor_box = [None]
    tunnel = _Tunnel(online=False)
    helper = _OneShotWorker(
        supervisor_box, WorkerResult("Idle", "ShouldNotRun")
    )
    fallback = _OneShotWorker(
        supervisor_box,
        WorkerResult("Success", "LocalCommittedOriginalRetained"),
    )
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=_Client(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_seconds=0.001,
        status_callback=observed.append,
    )
    supervisor_box[0] = supervisor

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 1
    assert tunnel.stops == 1
    assert [item.category for item in observed] == [
        "CoordinatorDisconnected",
        "LocalCommittedOriginalRetained",
    ]
    assert observed[0].transport_category == "tunnel_start_failed"
    assert supervisor.terminal_result is None


def test_auth_blocked_worker_stops_without_fallback_or_retry():
    tunnel = _Tunnel(online=True)
    helper = _ResultWorker(WorkerResult("Blocked", "AuthBlocked"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=_Client(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_seconds=0.001,
        status_callback=observed.append,
    )

    supervisor.run()

    assert supervisor.stop_event.is_set()
    assert helper.calls == 1
    assert fallback.calls == 0
    assert tunnel.stops == 1
    assert observed == [WorkerResult("Blocked", "AuthBlocked")]
    assert supervisor.terminal_result == WorkerResult(
        "Blocked", "AuthBlocked"
    )


def test_network_failures_report_status_and_back_off_exponentially():
    class RecordingStopEvent:
        def __init__(self):
            self.stopped = False
            self.waits = []

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, timeout):
            self.waits.append(timeout)
            if len(self.waits) == 3:
                self.stopped = True
            return self.stopped

    tunnel = _Tunnel(online=False)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=_Client(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_initial_seconds=1.0,
        reconnect_max_seconds=3.0,
        reconnect_multiplier=2.0,
        status_callback=observed.append,
    )
    stop_event = RecordingStopEvent()
    supervisor.stop_event = stop_event

    supervisor.run()

    assert stop_event.waits == [1.0, 2.0, 3.0]
    assert helper.calls == 0
    assert fallback.calls == 3
    assert [item.kind for item in observed] == [
        "Disconnected",
        "Idle",
        "Waiting",
    ] * 3
    assert [item.category for item in observed[::3]] == [
        "CoordinatorDisconnected"
    ] * 3
    assert [item.category for item in observed[2::3]] == [
        "ReconnectBackoff"
    ] * 3
    assert [item.transport_category for item in observed[::3]] == [
        "tunnel_start_failed"
    ] * 3


def test_coordinator_request_conflict_preserves_safe_transport_category():
    class ConflictClient(_Client):
        def helper_seen(self):
            raise TransportError(PublicError.REQUEST_CONFLICT, status_code=409)

    supervisor_box = [None]
    tunnel = _Tunnel(online=True)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _OneShotWorker(
        supervisor_box,
        WorkerResult("Idle", "NoLocalWork"),
    )
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=ConflictClient(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_seconds=0.001,
        status_callback=observed.append,
    )
    supervisor_box[0] = supervisor

    supervisor.run()

    assert helper.calls == 0
    assert observed[0] == WorkerResult(
        "Disconnected",
        "CoordinatorDisconnected",
        transport_category="request_conflict",
    )
    assert observed[1] == WorkerResult("Idle", "NoLocalWork")


def test_transport_authentication_failure_is_terminal():
    class AuthFailureTunnel(_Tunnel):
        def start(self):
            self.starts += 1
            raise TransportError(PublicError.AUTHENTICATION_FAILED)

    tunnel = AuthFailureTunnel(online=False)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=_Client(),
        helper_worker=helper,
        fallback_worker=fallback,
        status_callback=observed.append,
    )

    supervisor.run()

    assert supervisor.stop_event.is_set()
    assert helper.calls == 0
    assert fallback.calls == 0
    assert observed == [
        WorkerResult(
            "Blocked",
            "AuthBlocked",
            transport_category="authentication_failed",
        )
    ]
    assert supervisor.terminal_result == WorkerResult(
        "Blocked",
        "AuthBlocked",
        transport_category="authentication_failed",
    )


def test_transport_authentication_block_publishes_fresh_local_status(
    tmp_path,
):
    class AuthFailureTunnel(_Tunnel):
        def start(self):
            self.starts += 1
            raise TransportError(PublicError.AUTHENTICATION_FAILED)

    store, command = _make_control_store(tmp_path, pc_in_use=False)
    client = _ControlClient()
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=AuthFailureTunnel(online=False),
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
    )

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 0
    assert client.control_calls == []
    status = store.read_status()
    assert status.revision == command.revision
    assert status.effective_state == "blocked"
    assert status.category == "AuthBlocked"
    assert status.run_id == ""


class _ControlClient(_Client):
    def __init__(self, *, failure: PublicError | None = None):
        super().__init__()
        self.failure = failure
        self.control_calls = []

    def helper_state(self, **kwargs):
        self.control_calls.append(kwargs)
        if self.failure is not None:
            raise TransportError(self.failure)
        return {
            "run_id": "f" * 32,
            "revision": kwargs["revision"],
            "pc_in_use": kwargs["pc_in_use"],
        }


class _StopAfterWaits:
    def __init__(self, count):
        self.count = count
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.count:
            self.stopped = True
        return self.stopped


def _make_control_store(tmp_path, *, pc_in_use):
    control_file = tmp_path / "helper-control.json"
    status_file = tmp_path / "helper-control-status.json"
    control_id = "a" * 32
    command = lan_helper_control.set_desired_state(
        control_file,
        status_file,
        control_id,
        pc_in_use,
    )
    return (
        lan_helper_control.HelperControlStore(
            control_file,
            status_file,
            control_id,
        ),
        command,
    )


def _signal_status_write(store, monkeypatch, *, effective_state):
    written = threading.Event()
    real_write = store.write_status

    def observed_write(*args, **kwargs):
        result = real_write(*args, **kwargs)
        if kwargs.get("effective_state") == effective_state:
            written.set()
        return result

    monkeypatch.setattr(store, "write_status", observed_write)
    return written


def test_paused_helper_stays_connected_without_claim_or_local_fallback(
    tmp_path, monkeypatch
):
    store, command = _make_control_store(tmp_path, pc_in_use=True)
    writes = []
    real_write = store.write_status

    def record_write(*args, **kwargs):
        writes.append((args, kwargs))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store, "write_status", record_write)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    client = _ControlClient()
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
        status_callback=observed.append,
    )
    supervisor.stop_event = _StopAfterWaits(3)

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 0
    assert len(client.control_calls) == 3
    assert len(writes) == 2
    assert store.read_status() == lan_helper_control.HelperControlStatus(
        schema_version=1,
        control_id=command.control_id,
        revision=command.revision,
        desired_state="pc_in_use",
        effective_state="paused",
        category="",
        run_id="f" * 32,
    )
    assert [item.category for item in observed] == ["PcInUsePaused"] * 3


def test_idle_available_helper_does_not_churn_control_status(
    tmp_path, monkeypatch
):
    store, _command = _make_control_store(tmp_path, pc_in_use=False)
    writes = []
    real_write = store.write_status

    def record_write(*args, **kwargs):
        writes.append((args, kwargs))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store, "write_status", record_write)
    helper = _ResultWorker(WorkerResult("Idle", "NoPendingJob"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    client = _ControlClient()
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
    )
    supervisor.stop_event = _StopAfterWaits(3)

    supervisor.run()

    assert helper.calls == 3
    assert fallback.calls == 0
    assert len(client.control_calls) == 3
    assert len(writes) == 2
    assert store.read_status().effective_state == "available"


def test_identical_control_status_refreshes_only_after_heartbeat(tmp_path):
    store, command = _make_control_store(tmp_path, pc_in_use=False)
    writes = []
    real_write = store.write_status
    now = [100.0]

    def record_write(*args, **kwargs):
        writes.append((now[0], args, kwargs))
        return real_write(*args, **kwargs)

    store.write_status = record_write
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=_ControlClient(),
        helper_worker=_ResultWorker(WorkerResult("Idle", "NoPendingJob")),
        fallback_worker=_ResultWorker(WorkerResult("Idle", "NoLocalWork")),
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_heartbeat_seconds=5.0,
        control_clock=lambda: now[0],
    )

    supervisor._announce_control(command)
    assert len(writes) == 2
    now[0] = 104.999
    supervisor._announce_control(command)
    assert len(writes) == 2
    now[0] = 105.0
    supervisor._announce_control(command)

    assert len(writes) == 3
    assert writes[-1][2]["effective_state"] == "available"
    assert writes[-1][0] == 105.0


def test_pause_after_ack_prevents_fresh_helper_claim(tmp_path):
    store, initial = _make_control_store(tmp_path, pc_in_use=False)
    supervisor_box = [None]

    class PauseAfterFirstAck(_ControlClient):
        def helper_state(self, **kwargs):
            result = super().helper_state(**kwargs)
            if len(self.control_calls) == 1:
                lan_helper_control.set_desired_state(
                    store.control_file,
                    store.status_file,
                    store.control_id,
                    True,
                )
            elif len(self.control_calls) == 2:
                supervisor_box[0].stop_event.set()
            return result

    client = PauseAfterFirstAck()
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
    )
    supervisor_box[0] = supervisor

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 0
    assert [call["revision"] for call in client.control_calls] == [
        initial.revision,
        initial.revision + 1,
    ]
    assert [call["pc_in_use"] for call in client.control_calls] == [
        False,
        True,
    ]
    assert store.read_status().effective_state == "paused"


def test_control_change_during_active_work_is_local_draining_until_return(
    tmp_path, monkeypatch
):
    store, _command = _make_control_store(tmp_path, pc_in_use=False)
    draining_written = _signal_status_write(
        store,
        monkeypatch,
        effective_state="draining",
    )
    entered = threading.Event()
    release = threading.Event()
    supervisor_box = [None]

    class BlockingWorker:
        calls = 0

        def run_one(self):
            self.calls += 1
            entered.set()
            release.wait()
            return WorkerResult("Success", "Committed")

    class StopAfterSecondControl(_ControlClient):
        def helper_state(self, **kwargs):
            result = super().helper_state(**kwargs)
            if len(self.control_calls) == 2:
                supervisor_box[0].stop_event.set()
            return result

    client = StopAfterSecondControl()
    helper = BlockingWorker()
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_poll_seconds=0.01,
    )
    supervisor_box[0] = supervisor
    thread = threading.Thread(target=supervisor.run)
    try:
        thread.start()
        assert entered.wait(1)
        pause = lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
        assert draining_written.wait(5), "draining status was not observed"
        status = store.read_status()
        assert status.revision == pause.revision
        assert status.effective_state == "draining"
        assert len(client.control_calls) == 1
        assert not supervisor.stop_event.is_set()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(client.control_calls) == 2
    assert helper.calls == 1
    assert fallback.calls == 0
    assert store.read_status().effective_state == "paused"


def test_pause_during_blocked_worker_is_synced_before_terminal_stop(
    tmp_path, monkeypatch
):
    store, initial = _make_control_store(tmp_path, pc_in_use=False)
    draining_written = _signal_status_write(
        store,
        monkeypatch,
        effective_state="draining",
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingBlockedWorker:
        calls = 0

        def run_one(self):
            self.calls += 1
            entered.set()
            release.wait()
            return WorkerResult("Blocked", "StagingAccessBlocked")

    worker = BlockingBlockedWorker()
    client = _ControlClient()
    observed = []
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=client,
        helper_worker=worker,
        fallback_worker=_ResultWorker(WorkerResult("Idle", "NoLocalWork")),
        status_callback=observed.append,
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_poll_seconds=0.01,
    )
    thread = threading.Thread(target=supervisor.run)
    try:
        thread.start()
        assert entered.wait(1)
        paused = lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
        assert draining_written.wait(5), "blocked work never published draining"
        status = store.read_status()
        assert status.revision == paused.revision
        assert status.effective_state == "draining"
    finally:
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert [call["revision"] for call in client.control_calls] == [
        initial.revision,
        paused.revision,
    ]
    assert client.control_calls[-1]["pc_in_use"] is True
    assert supervisor.terminal_result == WorkerResult(
        "Blocked", "StagingAccessBlocked"
    )
    status = store.read_status()
    assert status.revision == paused.revision
    assert status.effective_state == "blocked"
    assert status.category == "StagingAccessBlocked"
    assert status.run_id == "f" * 32
    assert observed[-1] == supervisor.terminal_result


def test_pause_during_offline_fallback_drains_then_stays_sync_pending(
    tmp_path, monkeypatch
):
    store, _command = _make_control_store(tmp_path, pc_in_use=False)
    draining_written = _signal_status_write(
        store,
        monkeypatch,
        effective_state="draining",
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingFallback:
        calls = 0

        def run_one(self):
            self.calls += 1
            entered.set()
            release.wait()
            return WorkerResult("Success", "LocalCommittedOriginalRetained")

    fallback = BlockingFallback()
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=False),
        client=_ControlClient(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.01,
        reconnect_multiplier=2.0,
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_poll_seconds=0.01,
    )
    thread = threading.Thread(target=supervisor.run)
    try:
        thread.start()
        assert entered.wait(1)
        pause = lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
        assert draining_written.wait(5), (
            "fallback draining status was not observed"
        )
        status = store.read_status()
        assert status.revision == pause.revision
        assert status.effective_state == "draining"
        assert status.run_id == ""
        supervisor.stop_event.set()
    finally:
        supervisor.stop_event.set()
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert helper.calls == 0
    assert fallback.calls == 1
    status = store.read_status()
    assert status.revision == pause.revision
    assert status.effective_state == "pending"
    assert status.category == "CoordinatorSyncPending"
    assert status.run_id == ""


def test_control_sync_conflict_is_terminal_without_fallback(tmp_path):
    store, command = _make_control_store(tmp_path, pc_in_use=False)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=_ControlClient(failure=PublicError.REQUEST_CONFLICT),
        helper_worker=helper,
        fallback_worker=fallback,
        control_store=store,
        helper_worker_id="helper-nvenc",
    )

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 0
    assert supervisor.terminal_result == WorkerResult(
        "Blocked",
        "HelperControlBlocked",
        phase="ControlSyncRejected",
    )
    status = store.read_status()
    assert status.revision == command.revision
    assert status.effective_state == "blocked"
    assert status.category == "ControlSyncRejected"


def test_paused_control_network_loss_never_runs_local_fallback(tmp_path):
    store, _command = _make_control_store(tmp_path, pc_in_use=True)
    helper = _ResultWorker(WorkerResult("Idle", "ShouldNotRun"))
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=_Tunnel(online=True),
        client=_ControlClient(failure=PublicError.CONNECTION_FAILED),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.01,
        reconnect_multiplier=2.0,
        control_store=store,
        helper_worker_id="helper-nvenc",
    )
    supervisor.stop_event = _StopAfterWaits(1)

    supervisor.run()

    assert helper.calls == 0
    assert fallback.calls == 0


def test_control_revision_retry_wakes_long_reconnect_backoff(tmp_path):
    store, initial = _make_control_store(tmp_path, pc_in_use=False)
    tunnel = _Tunnel(online=False)
    backoff_started = threading.Event()
    supervisor_box = [None]

    class StopAfterRecoveryWorker:
        calls = 0

        def run_one(self):
            self.calls += 1
            supervisor_box[0].stop_event.set()
            return WorkerResult("Idle", "NoPendingJob")

    def observe(result):
        if result == WorkerResult("Waiting", "ReconnectBackoff"):
            backoff_started.set()

    helper = StopAfterRecoveryWorker()
    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = lan_service.LanAssistSupervisor(
        tunnel=tunnel,
        client=_ControlClient(),
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_initial_seconds=30.0,
        reconnect_max_seconds=30.0,
        reconnect_multiplier=2.0,
        status_callback=observe,
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_poll_seconds=0.01,
    )
    supervisor_box[0] = supervisor
    thread = threading.Thread(target=supervisor.run)
    try:
        thread.start()
        assert backoff_started.wait(1)
        tunnel.online = True
        started = time.monotonic()
        retried = lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            False,
        )
        thread.join(timeout=2)
        elapsed = time.monotonic() - started
    finally:
        supervisor.stop_event.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert retried.revision == initial.revision + 1
    assert elapsed < 1.0
    assert fallback.calls == 1
    assert helper.calls == 1


def test_control_revision_wake_resets_exponential_delay(tmp_path):
    store, _initial = _make_control_store(tmp_path, pc_in_use=False)

    class RecordingSupervisor(lan_service.LanAssistSupervisor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.delays = []

        def _wait_for_reconnect_or_control(self, command, delay_seconds):
            self.delays.append(delay_seconds)
            if len(self.delays) == 1:
                lan_helper_control.set_desired_state(
                    store.control_file,
                    store.status_file,
                    store.control_id,
                    False,
                )
                return super()._wait_for_reconnect_or_control(
                    command,
                    delay_seconds,
                )
            self.stop_event.set()
            return False

    fallback = _ResultWorker(WorkerResult("Idle", "NoLocalWork"))
    supervisor = RecordingSupervisor(
        tunnel=_Tunnel(online=False),
        client=_ControlClient(),
        helper_worker=_ResultWorker(WorkerResult("Idle", "ShouldNotRun")),
        fallback_worker=fallback,
        reconnect_initial_seconds=1.0,
        reconnect_max_seconds=10.0,
        reconnect_multiplier=4.0,
        control_store=store,
        helper_worker_id="helper-nvenc",
        control_poll_seconds=0.01,
    )

    supervisor.run()

    assert supervisor.delays == [1.0, 1.0]
    assert fallback.calls == 2


class _DispatcherCoordinator:
    def snapshot(self):
        return {"Status": "Running"}


def test_helper_state_dispatch_is_strict_and_preserves_revision_cas():
    class ControlCoordinator(_DispatcherCoordinator):
        def __init__(self):
            self.calls = []

        def helper_state(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "run_id": "f" * 32,
                "revision": kwargs["revision"],
                "pc_in_use": kwargs["pc_in_use"],
            }

    coordinator = ControlCoordinator()
    dispatcher = lan_service.CoordinatorDispatcher(coordinator)
    request = {
        "worker_id": "helper-nvenc",
        "control_id": "a" * 32,
        "revision": 7,
        "pc_in_use": True,
    }

    assert dispatcher("helper_state", request) == {
        "run_id": "f" * 32,
        "revision": 7,
        "pc_in_use": True,
    }
    assert coordinator.calls == [request]
    for invalid in (
        {**request, "extra": True},
        {**request, "revision": True},
        {**request, "pc_in_use": 1},
    ):
        with pytest.raises(DispatchRejected) as rejected:
            dispatcher("helper_state", invalid)
        assert rejected.value.category is PublicError.INVALID_REQUEST


@pytest.mark.parametrize(
    "category",
    ["StaleHelperControl", "HelperControlConflict"],
)
def test_helper_state_cas_conflicts_map_to_category_only_409(category):
    class RejectingCoordinator(_DispatcherCoordinator):
        def helper_state(self, **_kwargs):
            raise lan_coordinator.CoordinatorError(category)

    dispatcher = lan_service.CoordinatorDispatcher(RejectingCoordinator())
    with pytest.raises(DispatchRejected) as rejected:
        dispatcher(
            "helper_state",
            {
                "worker_id": "helper-nvenc",
                "control_id": "a" * 32,
                "revision": 1,
                "pc_in_use": False,
            },
        )
    assert rejected.value.category is PublicError.REQUEST_CONFLICT


def test_dispatcher_helper_claim_requires_exact_synced_control(tmp_path):
    if not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file():
        pytest.skip("Local full FFmpeg toolchain is unavailable")
    root = tmp_path / "media"
    work = tmp_path / "work"
    root.mkdir()
    (root / "private.mp4").write_bytes(b"x" * 20)
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=FFMPEG,
        ffprobe=FFPROBE,
        helper_worker_id="helper-nvenc",
        helper_control_id="a" * 32,
    )
    dispatcher = lan_service.CoordinatorDispatcher(coordinator)
    try:
        assert dispatcher(
            "claim",
            {
                "worker_id": "helper-nvenc",
                "worker_role": "helper",
            },
        ) == {"idle": True}
        assert coordinator.snapshot()["helper_online"] is False

        with pytest.raises(DispatchRejected) as rejected:
            dispatcher(
                "claim",
                {
                    "worker_id": "forged-helper",
                    "worker_role": "helper",
                },
            )
        assert rejected.value.category is PublicError.INVALID_REQUEST

        dispatcher(
            "helper_state",
            {
                "worker_id": "helper-nvenc",
                "control_id": "a" * 32,
                "revision": 1,
                "pc_in_use": False,
            },
        )
        claim = dispatcher(
            "claim",
            {
                "worker_id": "helper-nvenc",
                "worker_role": "helper",
            },
        )
        assert claim["worker_id"] == "helper-nvenc"
        assert claim["worker_role"] == "helper"
    finally:
        coordinator.close()


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("claim", {"worker_id": "x", "worker_role": "remote"}),
        ("status", {"unexpected": True}),
        ("missing", {}),
    ],
)
def test_http_dispatcher_rejects_remote_or_expanded_surface(
    endpoint, payload
):
    dispatcher = lan_service.CoordinatorDispatcher(
        _DispatcherCoordinator()
    )

    with pytest.raises(DispatchRejected):
        dispatcher(endpoint, payload)


def test_http_heartbeat_accepts_every_canonical_work_phase(tmp_path):
    class WorkPhaseCoordinator:
        def __init__(self):
            self.observed = []

        def heartbeat(self, **kwargs):
            self.observed.append(kwargs["phase"])
            return lan_coordinator.ClaimPayload(
                schema_version=1,
                run_id="run",
                contract_hash="A" * 64,
                job_id="job",
                attempt_id="attempt",
                fencing_epoch=1,
                worker_id="helper",
                worker_role="helper",
                lease_deadline=45.0,
                source_alias="source.media",
                candidate_name="candidate.ready.mkv",
                source_identity={},
                run_marker_identity={},
                source_size_bytes=1,
                maximum_output_bytes=2,
            )

    token = tmp_path / "token"
    token.write_text(secrets.token_urlsafe(48), encoding="ascii")
    coordinator = WorkPhaseCoordinator()
    with LoopbackJsonServer(
        token_file=token,
        dispatcher=lan_service.CoordinatorDispatcher(coordinator),
        port=0,
    ).start() as server:
        client = lan_service.HttpCoordinatorClient(
            LoopbackJsonClient(
                base_url=f"http://127.0.0.1:{server.port}",
                token_file=token,
            )
        )
        request = {
            "worker_id": "helper",
            "attempt_id": "attempt",
            "fencing_epoch": 1,
            "progress_seconds": 0.0,
            "frame_count": 0,
            "media_duration_seconds": 0.0,
            "encode_elapsed_seconds": 0.0,
            "transfer_bytes": 0,
            "transfer_total_bytes": 0,
            "transfer_elapsed_seconds": 0.0,
        }

        assert "UploadVerification" in lan_coordinator.WORK_PHASES
        for phase in sorted(lan_coordinator.WORK_PHASES):
            assert client.heartbeat(phase=phase, **request).attempt_id == "attempt"

        with pytest.raises(TransportError) as rejected:
            client.heartbeat(phase="NotAWorkPhase", **request)
        assert rejected.value.category is PublicError.INVALID_REQUEST
        assert rejected.value.status_code == 400

    assert coordinator.observed == sorted(lan_coordinator.WORK_PHASES)


def test_http_submit_preserves_producer_full_evidence_digest(tmp_path):
    class SubmissionCoordinator:
        def __init__(self):
            self.submitted = None

        def submit_async(self, payload):
            self.submitted = payload
            return lan_coordinator.CommitOutcome("Accepted", "CommitAccepted")

        def poll_submission(self, **_kwargs):
            return lan_coordinator.CommitOutcome("Success", "Committed")

    token = tmp_path / "token"
    token.write_text(secrets.token_urlsafe(48), encoding="ascii")
    coordinator = SubmissionCoordinator()
    evidence = lan_media.ProducerValidationEvidence(
        schema_version=1,
        mode="producer-full",
        run_id="a" * 32,
        job_id="job",
        worker_id="helper",
        worker_role="helper",
        attempt_id="attempt",
        fencing_epoch=1,
        contract_hash="B" * 64,
        producer_build_sha256="C" * 64,
        ffmpeg_sha256="D" * 64,
        ffprobe_sha256="E" * 64,
        candidate_sha256="F" * 64,
        candidate_bytes=1024,
        encoded_frame_count=12,
        full_decode=lan_media.FullDecodeEvidence(
            video=lan_media.StreamDecodeEvidence(
                stream_index=0,
                stream_type="video",
                success=True,
                frame_count=12,
                out_time_seconds=1.0,
            ),
            audio=(),
        ),
    )
    payload = lan_coordinator.SubmitPayload(
        run_id=evidence.run_id,
        contract_hash=evidence.contract_hash,
        worker_id=evidence.worker_id,
        attempt_id=evidence.attempt_id,
        fencing_epoch=evidence.fencing_epoch,
        candidate_sha256=evidence.candidate_sha256,
        encoded_frame_count=evidence.encoded_frame_count,
        encode_seconds=1.0,
        validation_evidence=evidence,
    )

    with LoopbackJsonServer(
        token_file=token,
        dispatcher=lan_service.CoordinatorDispatcher(coordinator),
        port=0,
    ).start() as server:
        client = lan_service.HttpCoordinatorClient(
            LoopbackJsonClient(
                base_url=f"http://127.0.0.1:{server.port}",
                token_file=token,
            ),
            submit_poll_seconds=0.001,
        )
        outcome = client.submit(payload)

    assert outcome == lan_coordinator.CommitOutcome("Success", "Committed")
    assert coordinator.submitted == payload


def test_dispatcher_close_gate_rejects_new_work_and_drains_admitted_call():
    entered = threading.Event()
    release = threading.Event()
    results = []

    class BlockingCoordinator:
        def snapshot(self):
            entered.set()
            assert release.wait(2)
            return {"Status": "Running"}

    dispatcher = lan_service.CoordinatorDispatcher(BlockingCoordinator())
    active = threading.Thread(
        target=lambda: results.append(dispatcher("status", {}))
    )
    try:
        active.start()
        assert entered.wait(1)
        dispatcher.close_admission()

        with pytest.raises(DispatchRejected) as rejected:
            dispatcher("status", {})
        assert rejected.value.category is PublicError.SERVER_UNAVAILABLE
        assert not dispatcher.wait_for_idle(0.01)

        release.set()
        active.join(timeout=2)
        assert not active.is_alive()
        assert results == [{"Status": "Running"}]
        assert dispatcher.wait_for_idle(1)
    finally:
        release.set()
        active.join(timeout=2)


def test_coordinator_service_close_orders_admission_drain_workers_and_commits():
    events = []
    stop_event = threading.Event()

    class Dispatcher:
        def close_admission(self):
            assert not stop_event.is_set()
            events.append("admission-closed")

        def wait_for_idle(self, timeout):
            assert timeout == 3.0
            assert not stop_event.is_set()
            events.append("requests-drained")
            return True

    class Server:
        def close(self, *, timeout):
            assert timeout == 3.0
            assert not stop_event.is_set()
            events.append("listener-closed")

    class WorkerThread:
        def join(self):
            assert stop_event.is_set()
            events.append("worker-joined")

    class Coordinator:
        def wait_for_async_commits(self):
            assert stop_event.is_set()
            events.append("commits-drained")
            return True

    service = object.__new__(lan_service.CoordinatorService)
    service.dispatcher = Dispatcher()
    service.server = Server()
    service.stop_event = stop_event
    service.shutdown_timeout_seconds = 3.0
    service._threads = [WorkerThread()]
    service.coordinator = Coordinator()
    service._close_lock = threading.Lock()
    service._closed = False

    service.close()
    service.close()

    assert events == [
        "admission-closed",
        "listener-closed",
        "requests-drained",
        "worker-joined",
        "commits-drained",
    ]


def test_http_submit_is_accepted_before_slow_commit_finishes(
    tmp_path, monkeypatch
):
    if not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file():
        pytest.skip("Local full FFmpeg toolchain is unavailable")
    root = tmp_path / "media"
    work = tmp_path / "work"
    root.mkdir()
    (root / "synthetic.mp4").write_bytes(b"x" * 1024)
    token = tmp_path / "token"
    token.write_text(secrets.token_urlsafe(48), encoding="ascii")
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=FFMPEG,
        ffprobe=FFPROBE,
        helper_worker_id="helper",
        helper_control_id="a" * 32,
    )
    coordinator.helper_state(
        worker_id="helper",
        control_id="a" * 32,
        revision=1,
        pc_in_use=False,
    )
    claim = coordinator.claim("helper", WorkerRole.HELPER)
    assert claim is not None
    (work / "staging" / claim.candidate_name).write_bytes(b"candidate")
    entered = threading.Event()
    release = threading.Event()

    def slow_commit(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return lan_coordinator.CommitOutcome("Success", "Committed")

    monkeypatch.setattr(coordinator, "_commit_candidate", slow_commit)
    server = LoopbackJsonServer(
        token_file=token,
        dispatcher=lan_service.CoordinatorDispatcher(coordinator),
        port=0,
    ).start()
    transport = LoopbackJsonClient(
        base_url=f"http://127.0.0.1:{server.port}",
        token_file=token,
        timeout=0.5,
    )
    payload = {
        "run_id": claim.run_id,
        "contract_hash": claim.contract_hash,
        "worker_id": claim.worker_id,
        "attempt_id": claim.attempt_id,
        "fencing_epoch": claim.fencing_epoch,
        "candidate_sha256": "A" * 64,
        "encoded_frame_count": 1,
        "encode_seconds": 1.0,
    }
    try:
        started = time.monotonic()
        accepted = transport.call("submit", payload)
        elapsed = time.monotonic() - started

        assert accepted["kind"] == "Accepted"
        assert elapsed < 0.5
        assert entered.wait(1)
        pending = transport.call(
            "poll_submission",
            {
                "worker_id": claim.worker_id,
                "attempt_id": claim.attempt_id,
                "fencing_epoch": claim.fencing_epoch,
            },
        )
        assert pending["kind"] == "Pending"
        release.set()
        assert coordinator.wait_for_async_commits(5)
        completed = transport.call(
            "poll_submission",
            {
                "worker_id": claim.worker_id,
                "attempt_id": claim.attempt_id,
                "fencing_epoch": claim.fencing_epoch,
            },
        )
        assert completed["kind"] == "Success"
    finally:
        release.set()
        coordinator.wait_for_async_commits(5)
        server.close()
        coordinator.close()
