from __future__ import annotations

import builtins
import secrets
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import lan_coordinator
import lan_helper_control
import lan_media
import lan_service
import lan_worker
from lan_protocol import WorkerRole
from lan_transport import LoopbackJsonClient, LoopbackJsonServer


FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"
HELPER_CONTROL_ID = "c" * 32


def _helper_control_store(tmp_path, *, pc_in_use=False):
    control_file = (tmp_path / "helper-control.json").resolve()
    status_file = (tmp_path / "helper-control-status.json").resolve()
    command = lan_helper_control.set_desired_state(
        control_file,
        status_file,
        HELPER_CONTROL_ID,
        pc_in_use,
    )
    return (
        lan_helper_control.HelperControlStore(
            control_file,
            status_file,
            HELPER_CONTROL_ID,
        ),
        command,
    )


def test_helper_run_marker_access_failure_is_auth_blocked(
    tmp_path, monkeypatch
):
    class NeverClient:
        def helper_seen(self):
            raise AssertionError("blocked staging must stop before coordination")

    def access_denied(_path):
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(lan_worker, "open_read_pin", access_denied)
    worker = lan_worker.ComputeWorker(
        client=NeverClient(),
        worker_id="helper-nvenc",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(tmp_path / "staging"),
        staging_root=str(tmp_path / "staging"),
        local_cache_root=str(tmp_path / "cache"),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
    )

    result = worker.run_one()

    assert result == lan_worker.WorkerResult(
        "Blocked",
        "AuthBlocked",
        phase="RunMarkerOpen",
    )


def test_helper_run_marker_network_failure_is_reconnectable(
    tmp_path, monkeypatch
):
    class NeverClient:
        def helper_seen(self):
            raise AssertionError("unavailable staging must stop before coordination")

    def network_unavailable(_path):
        error = OSError("synthetic network loss")
        error.winerror = 53
        raise error

    monkeypatch.setattr(lan_worker, "open_read_pin", network_unavailable)
    worker = lan_worker.ComputeWorker(
        client=NeverClient(),
        worker_id="helper-nvenc",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(tmp_path / "staging"),
        staging_root=str(tmp_path / "staging"),
        local_cache_root=str(tmp_path / "cache"),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
    )

    result = worker.run_one()

    assert result == lan_worker.WorkerResult(
        "Disconnected",
        "CoordinatorDisconnected",
        phase="RunMarkerOpen",
        winerror=53,
    )


def test_helper_run_marker_safety_failure_is_terminal(
    tmp_path, monkeypatch
):
    def unsafe_marker(_path):
        raise lan_worker.FileSafetyError("ReparsePointRejected")

    monkeypatch.setattr(lan_worker, "open_read_pin", unsafe_marker)
    worker = lan_worker.ComputeWorker(
        client=object(),
        worker_id="helper-nvenc",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(tmp_path / "staging"),
        staging_root=str(tmp_path / "staging"),
        local_cache_root=str(tmp_path / "cache"),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
    )

    assert worker.run_one() == lan_worker.WorkerResult(
        "Blocked",
        "StagingAccessBlocked",
        phase="RunMarkerOpen",
    )


def _helper_access_case(
    tmp_path, *, validation_policy="redundant-full"
):
    staging = tmp_path / "staging"
    cache = tmp_path / "cache"
    staging.mkdir()
    marker = staging / lan_worker.RUN_MARKER_NAME
    marker.write_bytes(b"run marker")
    source = staging / "source-alias.bin"
    source.write_bytes(b"source")
    job_id = "1" * 32
    attempt_id = "2" * 32
    claim = lan_coordinator.ClaimPayload(
        schema_version=1,
        run_id="synthetic-run",
        contract_hash=lan_worker.DEFAULT_CONTRACT.digest(),
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_epoch=1,
        worker_id="helper-nvenc",
        worker_role=WorkerRole.HELPER.value,
        lease_deadline=time.monotonic() + 60,
        source_alias=source.name,
        candidate_name=f"candidate-{job_id}-{attempt_id}.ready.mkv",
        source_identity=lan_worker.get_identity(str(source)).to_dict(),
        run_marker_identity=lan_worker.get_identity(str(marker)).to_dict(),
        source_size_bytes=source.stat().st_size,
        maximum_output_bytes=1024 * 1024,
        validation_policy=validation_policy,
    )

    class TrackingClient:
        def __init__(self):
            self.abandoned = []
            self.failures = []
            self.submissions = []
            self.allow_submit = False

        def helper_seen(self):
            return None

        def claim(self, _worker_id, _worker_role):
            return claim

        def heartbeat(self, **_kwargs):
            return claim

        def abandon(self, **kwargs):
            self.abandoned.append(kwargs)
            return True

        def report_failure(self, **kwargs):
            self.failures.append(kwargs)
            return True

        def submit(self, payload):
            self.submissions.append(payload)
            if not self.allow_submit:
                raise AssertionError("blocked work must not be submitted")
            return lan_coordinator.CommitOutcome("Success", "Committed")

    client = TrackingClient()
    worker = lan_worker.ComputeWorker(
        client=client,
        worker_id="helper-nvenc",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(staging),
        staging_root=str(staging),
        local_cache_root=str(cache),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
    )
    return worker, client, marker, source


def test_helper_gate_tray_writer_wins_before_fresh_claim(
    tmp_path, monkeypatch
):
    store, _initial = _helper_control_store(tmp_path, pc_in_use=False)
    worker, client, _marker, _source = _helper_access_case(tmp_path)
    worker.control_store = store
    writer_inside = threading.Event()
    release_writer = threading.Event()
    writer_done = threading.Event()
    real_atomic_write = lan_helper_control._atomic_write_object

    def blocked_pause_write(path, value, **kwargs):
        if path == store.control_file and value.get("pc_in_use") is True:
            writer_inside.set()
            assert release_writer.wait(3)
        return real_atomic_write(path, value, **kwargs)

    monkeypatch.setattr(
        lan_helper_control,
        "_atomic_write_object",
        blocked_pause_write,
    )
    claim_calls = []
    real_claim = client.claim
    client.claim = lambda *args, **kwargs: (
        claim_calls.append((args, kwargs)) or real_claim(*args, **kwargs)
    )
    writer_result = []
    worker_result = []

    def write_pause():
        try:
            writer_result.append(
                lan_helper_control.set_desired_state(
                    store.control_file,
                    store.status_file,
                    store.control_id,
                    True,
                )
            )
        finally:
            writer_done.set()

    writer_thread = threading.Thread(target=write_pause)
    worker_thread = threading.Thread(
        target=lambda: worker_result.append(worker.run_one())
    )
    try:
        writer_thread.start()
        assert writer_inside.wait(1)
        worker_thread.start()
        assert not writer_done.is_set()
        assert claim_calls == []
    finally:
        release_writer.set()
        writer_thread.join(timeout=3)
        worker_thread.join(timeout=3)

    assert not writer_thread.is_alive()
    assert not worker_thread.is_alive()
    assert writer_result[0].pc_in_use is True
    assert worker_result == [
        lan_worker.WorkerResult("Waiting", "PcInUsePaused")
    ]
    assert claim_calls == []
    assert not worker.work_started_event.is_set()


def test_helper_gate_claim_wins_then_pause_waits_and_work_drains(
    tmp_path, monkeypatch
):
    store, _initial = _helper_control_store(tmp_path, pc_in_use=False)
    worker, client, _marker, _source = _helper_access_case(tmp_path)
    worker.control_store = store
    claim_entered = threading.Event()
    release_claim = threading.Event()
    encode_entered = threading.Event()
    release_encode = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    real_claim = client.claim

    def blocking_claim(*args, **kwargs):
        claim_entered.set()
        assert release_claim.wait(3)
        return real_claim(*args, **kwargs)

    def blocking_encode(*_args, **_kwargs):
        encode_entered.set()
        assert release_encode.wait(3)
        raise lan_worker.MediaContractError("SyntheticFinished")

    client.claim = blocking_claim
    monkeypatch.setattr(lan_worker, "encode_candidate", blocking_encode)
    worker_result = []

    def write_pause():
        writer_started.set()
        lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
        writer_done.set()

    worker_thread = threading.Thread(
        target=lambda: worker_result.append(worker.run_one())
    )
    writer_thread = threading.Thread(target=write_pause)
    try:
        worker_thread.start()
        assert claim_entered.wait(1)
        writer_thread.start()
        assert writer_started.wait(1)
        assert not writer_done.wait(0.05)
        release_claim.set()
        assert encode_entered.wait(1)
        assert writer_done.wait(1)
        assert store.read_command().pc_in_use is True
        assert worker_thread.is_alive()
    finally:
        release_claim.set()
        release_encode.set()
        writer_thread.join(timeout=3)
        worker_thread.join(timeout=3)

    assert not writer_thread.is_alive()
    assert not worker_thread.is_alive()
    assert worker_result[0].kind == "Failure"
    assert worker_result[0].category == "SyntheticFinished"
    assert worker.work_started_event.is_set()


def _windows_error(winerror, *, permission=False):
    error = (
        PermissionError("synthetic Windows failure")
        if permission
        else OSError("synthetic Windows failure")
    )
    error.winerror = winerror
    return error


@pytest.mark.parametrize(
    ("failure", "kind", "category"),
    [
        (PermissionError("denied"), "Blocked", "AuthBlocked"),
        (
            _windows_error(53),
            "Disconnected",
            "CoordinatorDisconnected",
        ),
        (
            _windows_error(32, permission=True),
            "Blocked",
            "StagingAccessBlocked",
        ),
    ],
)
def test_active_helper_source_access_failure_abandons_claim(
    tmp_path, monkeypatch, failure, kind, category
):
    worker, client, marker, _source = _helper_access_case(tmp_path)
    real_open_read_pin = lan_worker.open_read_pin

    def fail_source(path):
        if Path(path) == marker:
            return real_open_read_pin(path)
        raise failure

    monkeypatch.setattr(lan_worker, "open_read_pin", fail_source)

    result = worker.run_one()

    assert result.kind == kind
    assert result.category == category
    assert len(client.abandoned) == 1
    assert client.failures == []
    assert client.submissions == []


@pytest.mark.parametrize(
    ("failure", "kind", "category"),
    [
        (PermissionError("denied"), "Blocked", "AuthBlocked"),
        (
            _windows_error(64),
            "Disconnected",
            "CoordinatorDisconnected",
        ),
        (
            _windows_error(112),
            "Blocked",
            "StagingAccessBlocked",
        ),
    ],
)
def test_active_helper_upload_failure_abandons_claim(
    tmp_path, monkeypatch, failure, kind, category
):
    worker, client, _marker, _source = _helper_access_case(tmp_path)

    def fake_encode(_ffmpeg, _ffprobe, _source, candidate, *_args, **_kwargs):
        payload = b"candidate"
        Path(candidate).write_bytes(payload)
        return (
            None,
            None,
            lan_worker.CandidateEvidence(
                sha256=hashlib.sha256(payload).hexdigest(),
                encoded_frame_count=1,
                output_bytes=len(payload),
                encode_seconds=0.1,
            ),
        )

    def fail_upload(*_args, **_kwargs):
        raise lan_worker.StagingShareError(failure)

    monkeypatch.setattr(lan_worker, "encode_candidate", fake_encode)
    monkeypatch.setattr(lan_worker, "_upload_candidate", fail_upload)

    result = worker.run_one()

    assert result.kind == kind
    assert result.category == category
    assert len(client.abandoned) == 1
    assert client.failures == []
    assert client.submissions == []


def test_producer_full_worker_submits_fenced_validation_evidence(
    tmp_path, monkeypatch
):
    worker, client, _marker, _source = _helper_access_case(
        tmp_path, validation_policy="producer-full"
    )
    worker._producer_build_sha256 = "A" * 64
    worker._ffmpeg_sha256 = "B" * 64
    worker._ffprobe_sha256 = "C" * 64
    client.allow_submit = True

    def fake_encode(_ffmpeg, _ffprobe, _source, candidate, *_args, **kwargs):
        payload = b"producer-full-candidate"
        Path(candidate).write_bytes(payload)
        phase_callback = kwargs.get("phase_callback")
        if phase_callback is not None:
            phase_callback("LocalValidation")
        return (
            None,
            (),
            lan_media.CandidateEvidence(
                sha256=hashlib.sha256(payload).hexdigest().upper(),
                encoded_frame_count=1,
                output_bytes=len(payload),
                encode_seconds=0.1,
                full_decode=lan_media.FullDecodeEvidence(
                    video=lan_media.StreamDecodeEvidence(
                        stream_index=0,
                        stream_type="video",
                        success=True,
                        frame_count=1,
                        out_time_seconds=1.0,
                    ),
                    audio=(),
                ),
            ),
        )

    monkeypatch.setattr(lan_worker, "encode_candidate", fake_encode)

    result = worker.run_one()

    assert result.kind == "Success"
    assert len(client.submissions) == 1
    submitted = client.submissions[0]
    evidence = submitted.validation_evidence
    assert evidence is not None
    assert evidence.worker_role == "helper"
    assert evidence.producer_build_sha256 == "A" * 64
    assert evidence.ffmpeg_sha256 == "B" * 64
    assert evidence.ffprobe_sha256 == "C" * 64
    wire = evidence.to_dict()
    assert "evidence_digest" in wire
    assert not any("path" in key or "name" in key for key in wire)


@pytest.fixture
def toolchain():
    if not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file():
        pytest.skip("Local full FFmpeg toolchain is unavailable")
    return FFMPEG, FFPROBE


def make_source(path: Path, toolchain, *, size: str, seconds: float):
    ffmpeg, _ffprobe = toolchain
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0


def wait_for(predicate, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def test_simultaneous_size_aware_nvenc_and_qsv_workers(
    tmp_path, toolchain
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    cache = tmp_path / "helper-cache"
    root.mkdir()
    make_source(
        root / "synthetic-large.mp4",
        toolchain,
        size="1280x720",
        seconds=8,
    )
    make_source(
        root / "synthetic-small.mp4",
        toolchain,
        size="320x180",
        seconds=3,
    )
    token_file = tmp_path / "token"
    token_file.write_text(secrets.token_urlsafe(48), encoding="ascii")
    ffmpeg, ffprobe = toolchain

    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        lease_seconds=30,
        helper_worker_id="synthetic-rtx",
        helper_control_id=HELPER_CONTROL_ID,
    )
    server = LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lan_service.CoordinatorDispatcher(coordinator),
        port=0,
    ).start()
    transport = LoopbackJsonClient(
        base_url=f"http://127.0.0.1:{server.port}",
        token_file=token_file,
    )
    helper_client = lan_service.HttpCoordinatorClient(transport)
    helper_client.helper_state(
        worker_id="synthetic-rtx",
        control_id=HELPER_CONTROL_ID,
        revision=1,
        pc_in_use=False,
    )
    helper = lan_worker.ComputeWorker(
        client=helper_client,
        worker_id="synthetic-rtx",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(work / "staging"),
        staging_root=str(work / "staging"),
        local_cache_root=str(cache),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        heartbeat_interval_seconds=0.25,
        direct_staging=False,
    )
    remote = lan_worker.ComputeWorker(
        client=lan_worker.DirectCoordinatorClient(coordinator),
        worker_id="synthetic-qsv",
        worker_role=WorkerRole.REMOTE,
        encoder="hevc_qsv",
        source_root=str(work / "staging"),
        staging_root=str(work / "staging"),
        local_cache_root=str(work / "staging"),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        heartbeat_interval_seconds=0.25,
        direct_staging=True,
    )
    helper_result = []
    remote_result = []
    helper_thread = threading.Thread(
        target=lambda: helper_result.append(helper.run_one())
    )
    try:
        helper_thread.start()
        wait_for(
            lambda: coordinator.snapshot()["helper_leased"] == 1,
            timeout=10,
        )
        remote_thread = threading.Thread(
            target=lambda: remote_result.append(remote.run_one())
        )
        remote_thread.start()
        helper_thread.join(timeout=180)
        remote_thread.join(timeout=180)

        assert not helper_thread.is_alive()
        assert not remote_thread.is_alive()
        assert helper_result[0].kind == "Success"
        assert remote_result[0].kind == "Success"
        assert (
            helper_result[0].source_size_bytes
            > remote_result[0].source_size_bytes
        )
        snapshot = coordinator.snapshot()
        assert snapshot["completed"] == 2
        assert snapshot["failed"] == 0
        assert not list(root.glob("*.mp4"))
        assert len(list(root.glob("*.mkv"))) == 2
        assert not (work / "active-transaction.json").exists()
    finally:
        server.close()
        coordinator.close()


class DisconnectingClient(lan_worker.DirectCoordinatorClient):
    def heartbeat(self, **kwargs):
        raise OSError("synthetic disconnect")


def test_disconnect_cancels_helper_and_remote_reclaims_after_proof(
    tmp_path, toolchain, monkeypatch
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    cache = tmp_path / "cache"
    root.mkdir()
    (root / "synthetic-large.mp4").write_bytes(b"x" * 1024)
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        lease_seconds=0.5,
        helper_presence_seconds=0.2,
        helper_worker_id="synthetic-rtx",
        helper_control_id=HELPER_CONTROL_ID,
    )
    coordinator.helper_state(
        worker_id="synthetic-rtx",
        control_id=HELPER_CONTROL_ID,
        revision=1,
        pc_in_use=False,
    )

    def cancelled_encode(*_args, cancel_event=None, **_kwargs):
        assert cancel_event is not None
        assert cancel_event.wait(5)
        raise lan_worker.MediaContractError("EncodeFailed")

    monkeypatch.setattr(lan_worker, "encode_candidate", cancelled_encode)
    helper = lan_worker.ComputeWorker(
        client=DisconnectingClient(coordinator),
        worker_id="synthetic-rtx",
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=str(work / "staging"),
        staging_root=str(work / "staging"),
        local_cache_root=str(cache),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        heartbeat_interval_seconds=0.05,
        heartbeat_failure_limit=1,
    )
    try:
        result = helper.run_one()
        assert result.kind in {"Failure", "Disconnected"}
        assert (root / "synthetic-large.mp4").exists()
        time.sleep(0.6)
        coordinator.reap()
        assert coordinator.snapshot()["pending"] == 1
        reclaimed = coordinator.claim("synthetic-qsv", WorkerRole.REMOTE)
        assert reclaimed is not None
        assert reclaimed.source_size_bytes == 1024
    finally:
        coordinator.close()


def test_local_fallback_finishes_one_file_and_retains_original(
    tmp_path, toolchain
):
    root = tmp_path / "local-work"
    root.mkdir()
    source = root / "synthetic-local.mp4"
    make_source(source, toolchain, size="640x360", seconds=2)
    ffmpeg, ffprobe = toolchain
    fallback = lan_worker.LocalFallbackWorker(
        root=str(root),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )

    result = fallback.run_one()

    assert result.kind == "Success"
    assert result.category == "LocalCommittedOriginalRetained"
    assert source.is_file()
    assert (root / "synthetic-local.mkv").is_file()
    assert fallback.run_one().kind == "Idle"
    assert not (root / "synthetic-local.local.mkv").exists()


def test_fallback_gate_tray_writer_wins_before_source_selection(
    tmp_path, monkeypatch
):
    store, _initial = _helper_control_store(tmp_path, pc_in_use=False)
    root = tmp_path / "local-work"
    root.mkdir()
    (root / "private.mp4").write_bytes(b"source")
    fallback = lan_worker.LocalFallbackWorker(
        root=str(root),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
        control_store=store,
    )
    writer_inside = threading.Event()
    release_writer = threading.Event()
    real_atomic_write = lan_helper_control._atomic_write_object

    def blocked_pause_write(path, value, **kwargs):
        if path == store.control_file and value.get("pc_in_use") is True:
            writer_inside.set()
            assert release_writer.wait(3)
        return real_atomic_write(path, value, **kwargs)

    monkeypatch.setattr(
        lan_helper_control,
        "_atomic_write_object",
        blocked_pause_write,
    )
    monkeypatch.setattr(
        lan_worker,
        "encode_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paused fallback encoded a new source")
        ),
    )
    writer_thread = threading.Thread(
        target=lambda: lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
    )
    results = []
    worker_thread = threading.Thread(
        target=lambda: results.append(fallback.run_one())
    )
    try:
        writer_thread.start()
        assert writer_inside.wait(1)
        worker_thread.start()
    finally:
        release_writer.set()
        writer_thread.join(timeout=3)
        worker_thread.join(timeout=3)

    assert not writer_thread.is_alive()
    assert not worker_thread.is_alive()
    assert results == [
        lan_worker.WorkerResult("Waiting", "PcInUsePaused")
    ]
    assert not fallback.work_started_event.is_set()


def test_fallback_gate_source_start_wins_then_pause_waits_and_drains(
    tmp_path, monkeypatch
):
    store, _initial = _helper_control_store(tmp_path, pc_in_use=False)
    root = tmp_path / "local-work"
    root.mkdir()
    source = root / "private.mp4"
    source.write_bytes(b"source")
    fallback = lan_worker.LocalFallbackWorker(
        root=str(root),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
        control_store=store,
    )
    source_pinned = threading.Event()
    release_pin = threading.Event()
    encode_entered = threading.Event()
    release_encode = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    real_open_read_pin = lan_worker.open_read_pin

    def blocking_source_pin(path):
        pin = real_open_read_pin(path)
        source_pinned.set()
        assert release_pin.wait(3)
        return pin

    def blocking_encode(*_args, **_kwargs):
        encode_entered.set()
        assert release_encode.wait(3)
        raise lan_worker.MediaContractError("SyntheticFinished")

    monkeypatch.setattr(lan_worker, "open_read_pin", blocking_source_pin)
    monkeypatch.setattr(lan_worker, "encode_candidate", blocking_encode)
    results = []

    def write_pause():
        writer_started.set()
        lan_helper_control.set_desired_state(
            store.control_file,
            store.status_file,
            store.control_id,
            True,
        )
        writer_done.set()

    worker_thread = threading.Thread(
        target=lambda: results.append(fallback.run_one())
    )
    writer_thread = threading.Thread(target=write_pause)
    try:
        worker_thread.start()
        assert source_pinned.wait(1)
        writer_thread.start()
        assert writer_started.wait(1)
        assert not writer_done.wait(0.05)
        release_pin.set()
        assert encode_entered.wait(1)
        assert writer_done.wait(1)
        assert store.read_command().pc_in_use is True
        assert worker_thread.is_alive()
    finally:
        release_pin.set()
        release_encode.set()
        writer_thread.join(timeout=3)
        worker_thread.join(timeout=3)

    assert not writer_thread.is_alive()
    assert not worker_thread.is_alive()
    assert results[0].kind == "Failure"
    assert results[0].category == "SyntheticFinished"
    assert source.is_file()
    assert fallback.work_started_event.is_set()


def test_default_fallback_root_uses_visible_frozen_executable(
    monkeypatch, tmp_path
):
    executable = tmp_path / "VideoTranscoderPortable.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert lan_worker.default_fallback_root() == str(tmp_path)


def test_upload_cancellation_removes_partial_attempt(tmp_path):
    local = tmp_path / "local.mkv"
    staging = tmp_path / "staging"
    staging.mkdir()
    local.write_bytes(b"x" * (2 * 1024 * 1024))
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(lan_worker.WorkerError) as raised:
        lan_worker._upload_candidate(
            str(local),
            str(staging),
            "candidate-"
            + "1" * 32
            + "-"
            + "2" * 32
            + ".ready.mkv",
            hashlib.sha256(local.read_bytes()).hexdigest(),
            cancel_event=cancel,
        )

    assert raised.value.category == "CoordinatorDisconnected"
    assert raised.value.phase == "UploadWrite"
    assert not list(staging.iterdir())


def test_upload_progress_includes_smb_verification_read(tmp_path):
    local = tmp_path / "local.mkv"
    staging = tmp_path / "staging"
    staging.mkdir()
    local.write_bytes(b"x" * (2 * 1024 * 1024))
    progress = []

    ready = lan_worker._upload_candidate(
        str(local),
        str(staging),
        "candidate-"
        + "1" * 32
        + "-"
        + "2" * 32
        + ".ready.mkv",
        hashlib.sha256(local.read_bytes()).hexdigest(),
        progress_callback=lambda transferred, total, elapsed, phase: (
            progress.append((transferred, total, elapsed, phase))
        ),
    )

    source_bytes = local.stat().st_size
    assert Path(ready).is_file()
    assert {entry[1] for entry in progress} == {source_bytes * 2}
    assert progress[0][0] == 0
    assert progress[0][3] == "Uploading"
    verification = [
        entry for entry in progress if entry[3] == "UploadVerification"
    ]
    assert verification
    assert verification[0][0] == source_bytes
    assert verification[-1][0] == source_bytes * 2
    assert progress[-1][0] == progress[-1][1]
    assert all(
        current[0] >= previous[0]
        and current[2] >= previous[2]
        for previous, current in zip(progress, progress[1:])
    )


def _upload_retry_case(tmp_path):
    local = tmp_path / "local.mkv"
    staging = tmp_path / "staging"
    staging.mkdir()
    local.write_bytes(b"candidate-payload" * 4096)
    candidate_name = (
        "candidate-" + "1" * 32 + "-" + "2" * 32 + ".ready.mkv"
    )
    return (
        local,
        staging,
        candidate_name,
        hashlib.sha256(local.read_bytes()).hexdigest(),
    )


def test_upload_flush_retries_winerror_32_then_succeeds(
    tmp_path, monkeypatch
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    real_flush = lan_worker.flush_verified
    calls = []

    def flaky_flush(path, identity):
        calls.append((path, identity))
        if len(calls) == 1:
            raise lan_worker.FileSafetyError("FlushFailed", 32)
        return real_flush(path, identity)

    monkeypatch.setattr(lan_worker, "flush_verified", flaky_flush)
    diagnostics = lan_worker._UploadDiagnostics()

    ready = lan_worker._upload_candidate(
        str(local),
        str(staging),
        candidate_name,
        digest,
        diagnostics=diagnostics,
    )

    assert len(calls) == 2
    assert Path(ready).read_bytes() == local.read_bytes()
    assert not Path(str(ready) + ".upload").exists()
    assert diagnostics.phase == "UploadExclusiveFlush"
    assert diagnostics.winerror == 32
    assert diagnostics.retry_count == 1


def test_upload_verification_open_retries_winerror_33_then_succeeds(
    tmp_path, monkeypatch
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    upload_path = staging / (candidate_name + ".upload")
    verification_open_calls = 0

    def flaky_open(path, mode="r", *args, **kwargs):
        nonlocal verification_open_calls
        if (
            os.path.abspath(os.fspath(path)) == os.path.abspath(upload_path)
            and mode == "rb"
        ):
            verification_open_calls += 1
            if verification_open_calls == 1:
                raise _windows_error(33)
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(lan_worker, "open", flaky_open, raising=False)
    diagnostics = lan_worker._UploadDiagnostics()

    ready = lan_worker._upload_candidate(
        str(local),
        str(staging),
        candidate_name,
        digest,
        diagnostics=diagnostics,
    )

    assert Path(ready).is_file()
    assert verification_open_calls == 2
    assert diagnostics.phase == "UploadReadOpen"
    assert diagnostics.winerror == 33
    assert diagnostics.retry_count == 1


def test_upload_rename_retries_winerror_32_with_same_identity(
    tmp_path, monkeypatch
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    real_rename = lan_worker.rename_verified
    calls = []

    def flaky_rename(path, destination, identity):
        calls.append((path, destination, identity))
        if len(calls) == 1:
            raise lan_worker.FileSafetyError("RenameFailed", 32)
        return real_rename(path, destination, identity)

    monkeypatch.setattr(lan_worker, "rename_verified", flaky_rename)
    diagnostics = lan_worker._UploadDiagnostics()

    ready = lan_worker._upload_candidate(
        str(local),
        str(staging),
        candidate_name,
        digest,
        diagnostics=diagnostics,
    )

    assert Path(ready).is_file()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert diagnostics.phase == "UploadPublish"
    assert diagnostics.winerror == 32
    assert diagnostics.retry_count == 1


@pytest.mark.parametrize("winerror", [0, 5, 53, 64, 112])
def test_post_upload_retry_rejects_every_non_lock_error(
    tmp_path, monkeypatch, winerror
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    calls = 0

    def fail_flush(_path, _identity):
        nonlocal calls
        calls += 1
        raise lan_worker.FileSafetyError("SyntheticFailure", winerror)

    monkeypatch.setattr(lan_worker, "flush_verified", fail_flush)

    with pytest.raises(lan_worker.StagingShareError) as raised:
        lan_worker._upload_candidate(
            str(local),
            str(staging),
            candidate_name,
            digest,
        )

    assert raised.value.cause.winerror == winerror
    assert raised.value.phase == "UploadExclusiveFlush"
    assert calls == 1
    assert not list(staging.iterdir())


def test_post_upload_retry_exhaustion_is_bounded_and_cleans_attempt(
    tmp_path, monkeypatch
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    calls = 0

    def locked_flush(_path, _identity):
        nonlocal calls
        calls += 1
        raise lan_worker.FileSafetyError("FlushFailed", 32)

    monkeypatch.setattr(lan_worker, "flush_verified", locked_flush)
    monkeypatch.setattr(
        lan_worker, "_POST_UPLOAD_SHARE_RETRY_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        lan_worker, "_POST_UPLOAD_SHARE_RETRY_INTERVAL_SECONDS", 0.002
    )
    diagnostics = lan_worker._UploadDiagnostics()
    started = time.monotonic()

    with pytest.raises(lan_worker.StagingShareError) as raised:
        lan_worker._upload_candidate(
            str(local),
            str(staging),
            candidate_name,
            digest,
            diagnostics=diagnostics,
        )

    assert time.monotonic() - started < 0.5
    assert raised.value.phase == "UploadExclusiveFlush"
    assert raised.value.cause.winerror == 32
    assert calls == diagnostics.retry_count + 1
    assert calls > 1
    assert not list(staging.iterdir())


def test_post_upload_retry_cancellation_stops_before_another_call(
    tmp_path, monkeypatch
):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    calls = 0

    def locked_flush(_path, _identity):
        nonlocal calls
        calls += 1
        raise lan_worker.FileSafetyError("FlushFailed", 32)

    class CancelDuringWait:
        def is_set(self):
            return False

        def wait(self, _timeout):
            return True

    monkeypatch.setattr(lan_worker, "flush_verified", locked_flush)

    with pytest.raises(lan_worker.WorkerError) as raised:
        lan_worker._upload_candidate(
            str(local),
            str(staging),
            candidate_name,
            digest,
            cancel_event=CancelDuringWait(),
        )

    assert raised.value.category == "CoordinatorDisconnected"
    assert raised.value.phase == "UploadExclusiveFlush"
    assert raised.value.winerror == 32
    assert calls == 1
    assert not list(staging.iterdir())


def test_false_rename_result_is_not_retried(tmp_path, monkeypatch):
    local, staging, candidate_name, digest = _upload_retry_case(tmp_path)
    calls = 0

    def reject_rename(_path, _destination, _identity):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(lan_worker, "rename_verified", reject_rename)

    with pytest.raises(lan_worker.WorkerError) as raised:
        lan_worker._upload_candidate(
            str(local),
            str(staging),
            candidate_name,
            digest,
        )

    assert raised.value.category == "UploadPublishFailed"
    assert calls == 1
    assert not list(staging.iterdir())


def test_external_stop_abandons_attempt_without_terminal_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "media"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    source = root / "synthetic.mp4"
    source.write_bytes(b"x" * 1024)
    marker = staging / lan_worker.RUN_MARKER_NAME
    marker.write_bytes(b"run marker")
    job_id = "1" * 32
    attempt_id = "2" * 32
    claim = lan_coordinator.ClaimPayload(
        schema_version=1,
        run_id="synthetic-run",
        contract_hash=lan_worker.DEFAULT_CONTRACT.digest(),
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_epoch=1,
        worker_id="synthetic-qsv",
        worker_role=WorkerRole.REMOTE.value,
        lease_deadline=time.monotonic() + 60,
        source_alias=source.name,
        candidate_name=(
            f"candidate-{job_id}-{attempt_id}.ready.mkv"
        ),
        source_identity=lan_worker.get_identity(
            str(source)
        ).to_dict(),
        run_marker_identity=lan_worker.get_identity(
            str(marker)
        ).to_dict(),
        source_size_bytes=source.stat().st_size,
        maximum_output_bytes=1024 * 1024,
    )

    class TrackingClient:
        def __init__(self):
            self.abandoned = []
            self.failures = []

        def claim(self, _worker_id, _worker_role):
            return claim

        def heartbeat(self, **_kwargs):
            return claim

        def abandon(self, **kwargs):
            self.abandoned.append(kwargs)
            return True

        def report_failure(self, **kwargs):
            self.failures.append(kwargs)
            return True

        def submit(self, _payload):
            raise AssertionError("cancelled work must not be submitted")

    client = TrackingClient()
    external_stop = threading.Event()

    def cancelled_encode(*_args, cancel_event=None, **_kwargs):
        external_stop.set()
        assert cancel_event is not None and cancel_event.is_set()
        raise lan_worker.MediaContractError("EncodeCancelled")

    monkeypatch.setattr(lan_worker, "encode_candidate", cancelled_encode)
    worker = lan_worker.ComputeWorker(
        client=client,
        worker_id="synthetic-qsv",
        worker_role=WorkerRole.REMOTE,
        encoder="hevc_qsv",
        source_root=str(root),
        staging_root=str(staging),
        local_cache_root=str(staging),
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        ffprobe=str(tmp_path / "ffprobe.exe"),
        direct_staging=True,
        external_cancel_event=external_stop,
    )

    result = worker.run_one()

    assert result.kind == "Stopped"
    assert result.category == "ExternalStop"
    assert len(client.abandoned) == 1
    assert client.failures == []
    assert source.is_file()
    assert not list(staging.glob("candidate-*.mkv"))


def test_permanent_failures_are_terminal_and_third_stops_batch(
    tmp_path, toolchain, monkeypatch
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    for index in range(4):
        (root / f"synthetic-{index}.mp4").write_bytes(
            b"x" * (1024 + index)
        )
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )

    def permanent_failure(*_args, **_kwargs):
        raise lan_worker.MediaContractError("UnsupportedSynthetic")

    monkeypatch.setattr(lan_worker, "encode_candidate", permanent_failure)
    worker = lan_worker.ComputeWorker(
        client=lan_worker.DirectCoordinatorClient(coordinator),
        worker_id="synthetic-qsv",
        worker_role=WorkerRole.REMOTE,
        encoder="hevc_qsv",
        source_root=str(work / "staging"),
        staging_root=str(work / "staging"),
        local_cache_root=str(work / "staging"),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        direct_staging=True,
    )
    try:
        results = [worker.run_one() for _ in range(3)]

        assert all(result.kind == "Failure" for result in results)
        snapshot = coordinator.snapshot()
        assert snapshot["failed"] == 3
        assert snapshot["pending"] == 1
        assert snapshot["ConsecutiveFailures"] == 3
        assert snapshot["Status"] == "Stopped"
        assert snapshot["FailureCategory"] == "ConsecutiveFailureLimit"
        assert len(list(root.glob("*.mp4"))) == 4
    finally:
        coordinator.close()
