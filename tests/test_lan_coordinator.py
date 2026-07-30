from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

import lan_coordinator
import lan_media
from lan_protocol import WorkerRole


FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"


@pytest.fixture
def toolchain():
    if not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file():
        pytest.skip("Local full FFmpeg toolchain is unavailable")
    return FFMPEG, FFPROBE


def make_coordinator(tmp_path, toolchain, files):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    for name, size in files:
        (root / name).write_bytes(b"x" * size)
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    return coordinator, root, work


def test_size_aware_claims_and_path_free_state(tmp_path, toolchain):
    coordinator, _root, work = make_coordinator(
        tmp_path,
        toolchain,
        [
            ("private-small.mp4", 10),
            ("private-large.mov", 100),
            ("private-medium.avi", 50),
        ],
    )
    try:
        coordinator.helper_seen()
        helper = coordinator.claim("rtx", WorkerRole.HELPER)
        remote = coordinator.claim("qsv", WorkerRole.REMOTE)

        assert helper is not None and helper.source_size_bytes == 100
        assert remote is not None and remote.source_size_bytes == 10
        state_text = (work / "state.json").read_text(encoding="utf-8")
        assert "private-small" not in state_text
        assert "private-large" not in state_text
        assert "private-medium" not in state_text
        snapshot = coordinator.snapshot()
        assert snapshot["total_jobs"] == 3
        assert snapshot["helper_leased"] == 1
        assert snapshot["remote_leased"] == 1
    finally:
        coordinator.close()


def test_claim_pin_blocks_source_mutation(tmp_path, toolchain):
    coordinator, root, _work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    try:
        claim = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert claim is not None
        if os.name == "nt":
            with pytest.raises(PermissionError):
                (root / "private.mp4").write_bytes(b"changed")
    finally:
        coordinator.close()


def test_abandon_requeues_with_new_fencing_epoch(tmp_path, toolchain):
    coordinator, _root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    try:
        first = coordinator.claim("qsv-one", WorkerRole.REMOTE)
        assert first is not None
        first_alias = work / "staging" / first.source_alias
        assert first_alias.is_file()
        assert coordinator.abandon(
            worker_id=first.worker_id,
            attempt_id=first.attempt_id,
            fencing_epoch=first.fencing_epoch,
        )
        assert not first_alias.exists()
        second = coordinator.claim("qsv-two", WorkerRole.REMOTE)
        assert second is not None
        assert second.attempt_id != first.attempt_id
        assert second.fencing_epoch > first.fencing_epoch
    finally:
        coordinator.close()


def test_existing_destination_and_ambiguous_destinations_are_skipped(
    tmp_path, toolchain
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    (root / "collision.mp4").write_bytes(b"a")
    (root / "collision.avi").write_bytes(b"b")
    (root / "existing.mov").write_bytes(b"c")
    (root / "existing.mkv").write_bytes(b"d")
    ffmpeg, ffprobe = toolchain

    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        snapshot = coordinator.snapshot()
        assert snapshot["total_jobs"] == 0
        assert snapshot["InitialSkipped"] == 4
    finally:
        coordinator.close()


def test_candidate_name_is_attempt_scoped_and_not_source_derived(
    tmp_path, toolchain
):
    coordinator, root, work = make_coordinator(
        tmp_path, toolchain, [("very-private-title.mp4", 20)]
    )
    try:
        claim = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert claim is not None
        assert claim.job_id in claim.candidate_name
        assert claim.attempt_id in claim.candidate_name
        assert "very-private-title" not in claim.candidate_name
        assert "very-private-title" not in claim.source_alias
        alias = work / "staging" / claim.source_alias
        assert alias.is_file()
        assert lan_coordinator.get_identity(str(alias)) == (
            lan_coordinator.get_identity(
                str(root / "very-private-title.mp4")
            )
        )
    finally:
        coordinator.close()


def test_active_transaction_blocks_startup(tmp_path, toolchain):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    work.mkdir()
    (root / "private.mp4").write_bytes(b"x")
    (work / "active-transaction.json").write_text(
        json.dumps({"Phase": "Published"}), encoding="utf-8"
    )
    ffmpeg, ffprobe = toolchain

    with pytest.raises(lan_coordinator.CoordinatorError) as raised:
        lan_coordinator.DistributedCoordinator(
            root=str(root),
            work_root=str(work),
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
    assert raised.value.systemic
    assert raised.value.category in {
        "JournalSchemaInvalid",
        "RunMarkerMissing",
    }
    assert (work / "active-transaction.json").exists()


def test_second_coordinator_cannot_own_same_work_root(
    tmp_path, toolchain
):
    coordinator, root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    ffmpeg, ffprobe = toolchain
    try:
        with pytest.raises(lan_coordinator.CoordinatorError) as raised:
            lan_coordinator.DistributedCoordinator(
                root=str(root),
                work_root=str(work),
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        assert raised.value.category == "CoordinatorAlreadyRunning"
    finally:
        coordinator.close()


def test_restart_waits_for_stale_worker_run_marker_pin(
    tmp_path, toolchain
):
    coordinator, root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    marker = (
        work
        / "staging"
        / lan_coordinator.RUN_MARKER_NAME
    )
    worker_marker_pin = lan_coordinator.open_read_pin(str(marker))
    coordinator.close()
    ffmpeg, ffprobe = toolchain
    try:
        with pytest.raises(lan_coordinator.CoordinatorError) as raised:
            lan_coordinator.DistributedCoordinator(
                root=str(root),
                work_root=str(work),
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        assert raised.value.category == "ResumeWorkersActive"
    finally:
        worker_marker_pin.close()

    resumed = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    resumed.close()


def test_restart_removes_only_fenced_orphan_source_alias(
    tmp_path, toolchain
):
    coordinator, root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    claim = coordinator.claim("qsv", WorkerRole.REMOTE)
    assert claim is not None
    old_alias = work / "staging" / claim.source_alias
    assert old_alias.is_file()
    coordinator.close()

    ffmpeg, ffprobe = toolchain
    resumed = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        assert not old_alias.exists()
        assert (root / "private.mp4").is_file()
        assert resumed.snapshot()["pending"] == 1
    finally:
        resumed.close()


def test_legacy_ledger_is_imported_without_reconverting_completed_output(
    tmp_path, toolchain
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    legacy_work = tmp_path / "legacy-control"
    root.mkdir()
    legacy_work.mkdir()
    completed = root / "completed.mkv"
    completed.write_bytes(b"already-converted")
    identity = lan_coordinator.get_identity(str(completed))
    legacy_hash = "A" * 64
    legacy_ledger = legacy_work / "completed-ledger.json"
    legacy_ledger.write_text(
        json.dumps(
            [
                {
                    "PathHash": lan_coordinator._path_hash(str(completed)),
                    "SettingsHash": legacy_hash,
                    "VolumeSerialHex": identity.volume_serial_hex,
                    "FileIdHex": identity.file_id_hex,
                    "Length": identity.length,
                    "CreationFileTime": identity.creation_file_time,
                    "LastWriteFileTime": identity.last_write_file_time,
                }
            ]
        ),
        encoding="utf-8",
    )
    ffmpeg, ffprobe = toolchain

    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        accepted_legacy_settings_hashes=[legacy_hash],
        legacy_ledger_paths=[str(legacy_ledger)],
    )
    try:
        snapshot = coordinator.snapshot()
        assert snapshot["total_jobs"] == 0
        assert snapshot["InitialSkipped"] == 1
        assert completed.read_bytes() == b"already-converted"
    finally:
        coordinator.close()


def test_submit_parser_rejects_bad_hash():
    with pytest.raises(lan_coordinator.CoordinatorError):
        lan_coordinator.submit_payload_from_dict(
            {
                "run_id": "run",
                "contract_hash": "contract",
                "worker_id": "worker",
                "attempt_id": "attempt",
                "fencing_epoch": 1,
                "candidate_sha256": "not-a-hash",
                "encoded_frame_count": 1,
            }
        )


def test_verified_rename_retries_transient_share_violation(monkeypatch):
    attempts = []

    def transient_then_success(*_args):
        attempts.append(True)
        if len(attempts) == 1:
            raise lan_coordinator.FileSafetyError(
                "OpenHandleFailed", 32
            )
        return True

    monkeypatch.setattr(
        lan_coordinator,
        "rename_verified",
        transient_then_success,
    )
    identity = lan_coordinator.FileIdentity(
        "0" * 16,
        "0" * 32,
        1,
        1,
        1,
    )

    assert lan_coordinator._rename_verified_with_retry(
        "source",
        "destination",
        identity,
        timeout_seconds=1,
    )
    assert len(attempts) == 2


def test_verified_rename_does_not_retry_nontransient_failure(monkeypatch):
    attempts = []

    def permanent_failure(*_args):
        attempts.append(True)
        raise lan_coordinator.FileSafetyError(
            "IdentityUnavailable", 87
        )

    monkeypatch.setattr(
        lan_coordinator,
        "rename_verified",
        permanent_failure,
    )
    identity = lan_coordinator.FileIdentity(
        "0" * 16,
        "0" * 32,
        1,
        1,
        1,
    )

    with pytest.raises(lan_coordinator.FileSafetyError):
        lan_coordinator._rename_verified_with_retry(
            "source",
            "destination",
            identity,
            timeout_seconds=1,
        )
    assert len(attempts) == 1


def _make_synthetic_source(path: Path, toolchain, seconds: float = 2.0):
    ffmpeg, _ffprobe = toolchain
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=24",
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
    ]
    completed = subprocess.run(command, capture_output=True, check=False)
    assert completed.returncode == 0


def _encode_and_submit(coordinator, claim, root, work, toolchain):
    ffmpeg, ffprobe = toolchain
    del root
    source = work / "staging" / claim.source_alias
    candidate = work / "staging" / claim.candidate_name
    _info, _audio, evidence = lan_media.encode_candidate(
        ffmpeg,
        ffprobe,
        str(source),
        str(candidate),
        "hevc_nvenc",
        claim.maximum_output_bytes,
    )
    payload = lan_coordinator.SubmitPayload(
        run_id=claim.run_id,
        contract_hash=claim.contract_hash,
        worker_id=claim.worker_id,
        attempt_id=claim.attempt_id,
        fencing_epoch=claim.fencing_epoch,
        candidate_sha256=evidence.sha256,
        encoded_frame_count=evidence.encoded_frame_count,
        encode_seconds=evidence.encode_seconds,
    )
    return coordinator.submit(payload)


def test_real_distinct_commit_is_validated_before_source_delete(
    tmp_path, toolchain
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    source = root / "synthetic-input.mp4"
    _make_synthetic_source(source, toolchain)
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        coordinator.helper_seen()
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None
        outcome = _encode_and_submit(
            coordinator, claim, root, work, toolchain
        )

        assert outcome.category == "Committed"
        assert not source.exists()
        assert (root / "synthetic-input.mkv").is_file()
        assert not (work / "active-transaction.json").exists()
        assert coordinator.snapshot()["completed"] == 1
    finally:
        coordinator.close()

    resumed = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        assert resumed.snapshot()["total_jobs"] == 0
        assert resumed.snapshot()["InitialSkipped"] == 1
        assert (root / "synthetic-input.mkv").is_file()
    finally:
        resumed.close()


def test_real_same_path_commit_replaces_only_after_validation(
    tmp_path, toolchain
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    source = root / "synthetic-input.mkv"
    _make_synthetic_source(source, toolchain)
    original_identity = lan_coordinator.get_identity(str(source))
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        coordinator.helper_seen()
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None
        outcome = _encode_and_submit(
            coordinator, claim, root, work, toolchain
        )

        assert outcome.category == "Committed"
        assert source.is_file()
        assert lan_coordinator.get_identity(str(source)) != original_identity
        assert not list(root.glob(".codex-original-backup-*.bak"))
        assert not (work / "active-transaction.json").exists()
    finally:
        coordinator.close()
