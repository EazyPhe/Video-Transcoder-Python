from __future__ import annotations

import threading
import secrets
import time
from pathlib import Path

import pytest

import lan_service
import lan_coordinator
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
        "LocalCommittedOriginalRetained"
    ]


class _DispatcherCoordinator:
    def snapshot(self):
        return {"Status": "Running"}


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
    )
    coordinator.helper_seen()
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
