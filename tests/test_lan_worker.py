from __future__ import annotations

import secrets
import hashlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import lan_coordinator
import lan_service
import lan_worker
from lan_protocol import WorkerRole
from lan_transport import LoopbackJsonClient, LoopbackJsonServer


FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"


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
