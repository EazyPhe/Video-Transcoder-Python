from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

import lan_coordinator
import lan_media
from lan_protocol import WorkerRole


FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"
HELPER_CONTROL_ID = "a" * 32


@pytest.fixture
def toolchain():
    if not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file():
        pytest.skip("Local full FFmpeg toolchain is unavailable")
    return FFMPEG, FFPROBE


def make_coordinator(tmp_path, toolchain, files, **coordinator_kwargs):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    for name, size in files:
        (root / name).write_bytes(b"x" * size)
    ffmpeg, ffprobe = toolchain
    coordinator_kwargs.setdefault("helper_worker_id", "rtx")
    coordinator_kwargs.setdefault("helper_control_id", HELPER_CONTROL_ID)
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        **coordinator_kwargs,
    )
    return coordinator, root, work


def set_helper_available(coordinator, *, revision=1):
    return coordinator.helper_state(
        worker_id=coordinator.helper_worker_id,
        control_id=coordinator.helper_control_id,
        revision=revision,
        pc_in_use=False,
    )


@pytest.mark.parametrize(
    ("worker_id", "control_id"),
    [
        ("", HELPER_CONTROL_ID),
        ("x" * 129, HELPER_CONTROL_ID),
        ("rtx", "A" * 32),
        ("rtx", "a" * 31),
        ("rtx", "g" * 32),
    ],
)
def test_helper_control_configuration_is_strict(
    tmp_path, toolchain, worker_id, control_id
):
    with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
        make_coordinator(
            tmp_path,
            toolchain,
            [("private.mp4", 20)],
            helper_worker_id=worker_id,
            helper_control_id=control_id,
        )
    assert rejected.value.category == "HelperControlConfigInvalid"


def test_pc_in_use_pause_makes_remote_immediately_eligible(
    tmp_path, toolchain
):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private-one.mp4", 20), ("private-two.mp4", 30)],
        scheduling_mode="prefer-helper",
    )
    try:
        acknowledged = coordinator.helper_state(
            worker_id="rtx",
            control_id=HELPER_CONTROL_ID,
            revision=1,
            pc_in_use=True,
        )
        assert acknowledged == {
            "run_id": coordinator.run_id,
            "revision": 1,
            "pc_in_use": True,
        }
        paused = coordinator.snapshot()
        assert paused["HelperPaused"] is True
        assert paused["HelperControlKnown"] is True
        assert paused["HelperControlRevision"] == 1
        assert paused["SchedulingState"] == "helper-paused"
        assert paused["HelperEligible"] is False
        assert paused["RemoteEligible"] is True
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is not None
    finally:
        coordinator.close()


def test_pc_in_use_pause_drains_active_helper_before_remote_claim(
    tmp_path, toolchain
):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private-one.mp4", 20), ("private-two.mp4", 30)],
        scheduling_mode="prefer-helper",
    )
    try:
        set_helper_available(coordinator)
        active = coordinator.claim("rtx", WorkerRole.HELPER)
        assert active is not None
        coordinator.helper_state(
            worker_id="rtx",
            control_id=HELPER_CONTROL_ID,
            revision=2,
            pc_in_use=True,
        )
        draining = coordinator.snapshot()
        assert draining["HelperPaused"] is True
        assert draining["SchedulingState"] == "helper-active"
        assert draining["RemoteEligible"] is False
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is None
        renewed = coordinator.heartbeat(
            worker_id=active.worker_id,
            attempt_id=active.attempt_id,
            fencing_epoch=active.fencing_epoch,
            phase="ProducerFullValidation",
        )
        assert renewed.attempt_id == active.attempt_id
        assert coordinator.abandon(
            worker_id=active.worker_id,
            attempt_id=active.attempt_id,
            fencing_epoch=active.fencing_epoch,
        )
        assert coordinator.snapshot()["SchedulingState"] == "helper-paused"
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is not None
    finally:
        coordinator.close()


def test_helper_claim_is_fail_closed_until_exact_control_sync(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        scheduling_mode="prefer-helper",
        helper_startup_grace_seconds=10.0,
        clock=lambda: now[0],
    )
    try:
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        assert coordinator.snapshot()["HelperControlKnown"] is False
        assert coordinator.snapshot()["helper_online"] is False

        now[0] = 109.0
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        now[0] = 110.0
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is not None
    finally:
        coordinator.close()


def test_helper_claim_rejects_wrong_identity_then_accepts_synced_helper(
    tmp_path, toolchain
):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
    )
    try:
        with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
            coordinator.claim("forged-helper", WorkerRole.HELPER)
        assert rejected.value.category == "HelperIdentityMismatch"
        assert coordinator.snapshot()["helper_online"] is False

        set_helper_available(coordinator)
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None
        assert claim.worker_id == "rtx"
    finally:
        coordinator.close()


def test_resume_changes_only_new_claims_and_never_preempts_remote(
    tmp_path, toolchain
):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private-one.mp4", 20), ("private-two.mp4", 30)],
        scheduling_mode="prefer-helper",
        helper_recovery_stable_seconds=0.0,
    )
    try:
        coordinator.helper_state(
            worker_id="rtx",
            control_id=HELPER_CONTROL_ID,
            revision=1,
            pc_in_use=True,
        )
        remote = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert remote is not None
        coordinator.helper_state(
            worker_id="rtx",
            control_id=HELPER_CONTROL_ID,
            revision=2,
            pc_in_use=False,
        )
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        assert coordinator.abandon(
            worker_id=remote.worker_id,
            attempt_id=remote.attempt_id,
            fencing_epoch=remote.fencing_epoch,
        )
        assert coordinator.claim("rtx", WorkerRole.HELPER) is not None
    finally:
        coordinator.close()


def test_helper_control_revision_cas_rejects_stale_and_conflicting_updates(
    tmp_path, toolchain
):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
    )
    try:
        kwargs = {
            "worker_id": "rtx",
            "control_id": HELPER_CONTROL_ID,
            "revision": 2,
            "pc_in_use": True,
        }
        first = coordinator.helper_state(**kwargs)
        assert coordinator.helper_state(**kwargs) == first
        with pytest.raises(lan_coordinator.CoordinatorError) as stale:
            coordinator.helper_state(**{**kwargs, "revision": 1})
        assert stale.value.category == "StaleHelperControl"
        with pytest.raises(lan_coordinator.CoordinatorError) as conflict:
            coordinator.helper_state(**{**kwargs, "pc_in_use": False})
        assert conflict.value.category == "HelperControlConflict"
        with pytest.raises(lan_coordinator.CoordinatorError) as forged:
            coordinator.helper_state(**{**kwargs, "control_id": "b" * 32})
        assert forged.value.category == "HelperControlInvalid"
        assert coordinator.snapshot()["HelperPaused"] is True
        assert coordinator.snapshot()["HelperControlRevision"] == 2
    finally:
        coordinator.close()


def test_prefer_helper_startup_grace_has_exact_fallback_boundary(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        scheduling_mode="prefer-helper",
        helper_startup_grace_seconds=60.0,
        clock=lambda: now[0],
    )
    try:
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is None
        snapshot = coordinator.snapshot()
        assert snapshot["SchedulingState"] == "helper-startup-wait"
        assert snapshot["FallbackCountdownSeconds"] == pytest.approx(60.0)

        now[0] = 159.999
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is None
        now[0] = 160.0
        claim = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert claim is not None
        assert coordinator.snapshot()["SchedulingState"] == "remote-active"
    finally:
        coordinator.close()


def test_prefer_helper_waits_from_last_authenticated_presence(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        scheduling_mode="prefer-helper",
        helper_fallback_after_seconds=90.0,
        helper_presence_seconds=30.0,
        clock=lambda: now[0],
    )
    try:
        set_helper_available(coordinator)
        now[0] = 189.999
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is None
        assert coordinator.snapshot()["SchedulingState"] == (
            "helper-absence-wait"
        )
        now[0] = 190.0
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is not None
    finally:
        coordinator.close()


def test_prefer_helper_recovery_is_stable_and_never_preempts_fallback(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private-one.mp4", 20), ("private-two.mp4", 30)],
        scheduling_mode="prefer-helper",
        helper_startup_grace_seconds=60.0,
        helper_recovery_stable_seconds=15.0,
        clock=lambda: now[0],
    )
    try:
        now[0] = 160.0
        remote = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert remote is not None

        now[0] = 161.0
        set_helper_available(coordinator)
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        assert coordinator.abandon(
            worker_id=remote.worker_id,
            attempt_id=remote.attempt_id,
            fencing_epoch=remote.fencing_epoch,
        )

        now[0] = 175.999
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        snapshot = coordinator.snapshot()
        assert snapshot["SchedulingState"] == "helper-recovery-wait"
        assert snapshot["RemoteEligible"] is False

        now[0] = 176.0
        helper = coordinator.claim("rtx", WorkerRole.HELPER)
        assert helper is not None
        assert coordinator.snapshot()["SchedulingState"] == "helper-active"
    finally:
        coordinator.close()


def test_prefer_helper_allows_only_one_noncommitting_encoder(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private-one.mp4", 20), ("private-two.mp4", 30)],
        scheduling_mode="prefer-helper",
        clock=lambda: now[0],
    )
    try:
        set_helper_available(coordinator)
        helper = coordinator.claim("rtx", WorkerRole.HELPER)
        assert helper is not None
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        now[0] = 1000.0
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is None
        assert coordinator.snapshot()["suspect"] == 1
    finally:
        coordinator.close()


def test_local_only_never_grants_helper_claim(tmp_path, toolchain):
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        scheduling_mode="local-only",
    )
    try:
        assert coordinator.claim("rtx", WorkerRole.HELPER) is None
        assert coordinator.claim("qsv", WorkerRole.REMOTE) is not None
        snapshot = coordinator.snapshot()
        assert snapshot["SchedulingMode"] == "local-only"
        assert snapshot["ValidationPolicy"] == "redundant-full"
    finally:
        coordinator.close()


def test_presence_timeout_does_not_fence_valid_helper_lease(
    tmp_path, toolchain
):
    now = [100.0]
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        lease_seconds=45.0,
        helper_presence_seconds=30.0,
        clock=lambda: now[0],
    )
    try:
        set_helper_available(coordinator)
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None

        now[0] += 31.0
        snapshot = coordinator.snapshot()
        assert snapshot["helper_online"] is False
        assert snapshot["helper_leased"] == 1
        assert snapshot["suspect"] == 0

        with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
            coordinator.heartbeat(
                worker_id="wrong-worker",
                attempt_id=claim.attempt_id,
                fencing_epoch=claim.fencing_epoch,
            )
        assert rejected.value.category == "StaleAttempt"
        assert coordinator.snapshot()["helper_online"] is False

        renewed = coordinator.heartbeat(
            worker_id=claim.worker_id,
            attempt_id=claim.attempt_id,
            fencing_epoch=claim.fencing_epoch,
            phase="UploadVerification",
        )
        assert renewed.lease_deadline == pytest.approx(now[0] + 45.0)
        snapshot = coordinator.snapshot()
        assert snapshot["helper_online"] is True
        assert snapshot["helper_leased"] == 1
        assert snapshot["suspect"] == 0

        now[0] += 45.0
        with pytest.raises(lan_coordinator.CoordinatorError) as expired:
            coordinator.heartbeat(
                worker_id=claim.worker_id,
                attempt_id=claim.attempt_id,
                fencing_epoch=claim.fencing_epoch,
            )
        assert expired.value.category == "StaleAttempt"
        assert coordinator.snapshot()["suspect"] == 1
    finally:
        coordinator.close()


def test_helper_heartbeat_renews_before_slow_observational_state_write(
    tmp_path, toolchain, monkeypatch
):
    now = [100.0]
    delay_writes = [False]
    real_atomic_write = lan_coordinator._atomic_write_json

    def delayed_atomic_write(path, value):
        if delay_writes[0]:
            now[0] += 20.0
        return real_atomic_write(path, value)

    monkeypatch.setattr(
        lan_coordinator, "_atomic_write_json", delayed_atomic_write
    )
    coordinator, _root, _work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 20)],
        lease_seconds=45.0,
        helper_presence_seconds=30.0,
        clock=lambda: now[0],
    )
    try:
        set_helper_available(coordinator)
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None

        now[0] += 31.0
        delay_writes[0] = True
        renewed = coordinator.heartbeat(
            worker_id=claim.worker_id,
            attempt_id=claim.attempt_id,
            fencing_epoch=claim.fencing_epoch,
            phase="UploadVerification",
        )

        assert renewed.lease_deadline == pytest.approx(176.0)
        assert coordinator.snapshot()["helper_leased"] == 1
    finally:
        coordinator.close()


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
        set_helper_available(coordinator)
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


def test_busy_upload_cleanup_resumes_after_alias_release_proof(
    tmp_path, toolchain, monkeypatch
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    (root / "private.mp4").write_bytes(b"source")
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        lease_seconds=0.02,
    )
    busy = True
    real_prove_exclusive = lan_coordinator.prove_exclusive_access
    try:
        first = coordinator.claim("qsv-one", WorkerRole.REMOTE)
        assert first is not None
        alias = work / "staging" / first.source_alias
        upload = work / "staging" / (first.candidate_name + ".upload")
        upload.write_bytes(b"partial upload")

        def selectively_busy(path):
            if Path(path) == upload and busy:
                return False
            return real_prove_exclusive(path)

        monkeypatch.setattr(
            lan_coordinator,
            "prove_exclusive_access",
            selectively_busy,
        )

        assert not coordinator.abandon(
            worker_id=first.worker_id,
            attempt_id=first.attempt_id,
            fencing_epoch=first.fencing_epoch,
        )
        assert not alias.exists()
        assert upload.exists()
        assert coordinator.snapshot()["suspect"] == 1
        assert coordinator._active[
            first.attempt_id
        ].source_alias_release_proven

        busy = False
        time.sleep(0.03)
        assert coordinator.reap() == 1
        assert not upload.exists()
        assert coordinator.snapshot()["pending"] == 1
        second = coordinator.claim("qsv-two", WorkerRole.REMOTE)
        assert second is not None
        assert second.attempt_id != first.attempt_id
        assert second.fencing_epoch > first.fencing_epoch
    finally:
        coordinator.close()


def test_missing_alias_without_scoped_release_proof_remains_suspect(
    tmp_path, toolchain
):
    coordinator, _root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    try:
        claim = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert claim is not None
        active = coordinator._active[claim.attempt_id]
        alias = work / "staging" / claim.source_alias
        coordinator._close_attempt_source_pins(active)
        alias.unlink()

        assert not coordinator.abandon(
            worker_id=claim.worker_id,
            attempt_id=claim.attempt_id,
            fencing_epoch=claim.fencing_epoch,
        )
        assert not active.source_alias_release_proven
        assert coordinator.snapshot()["suspect"] == 1
    finally:
        coordinator.close()


def test_final_source_mutation_never_accepts_proven_missing_alias(
    tmp_path, toolchain
):
    coordinator, _root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    try:
        claim = coordinator.claim("qsv", WorkerRole.REMOTE)
        assert claim is not None
        active = coordinator._active[claim.attempt_id]
        alias = work / "staging" / claim.source_alias
        coordinator._close_attempt_source_pins(active)
        alias.unlink()
        active.source_alias_release_proven = True

        with coordinator._lock:
            assert not coordinator._delete_source_alias_locked(
                active,
                require_source_exclusive=True,
            )
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


def _submit_admission_fixture(tmp_path):
    candidate_name = f"candidate-{'1' * 32}-{'2' * 32}.ready.mkv"
    candidate_path = tmp_path / candidate_name
    candidate_path.write_bytes(b"candidate")
    active = SimpleNamespace(
        lease=SimpleNamespace(
            worker_id="worker",
            fencing_epoch=1,
            worker_role=WorkerRole.REMOTE,
        ),
        candidate_name=candidate_name,
        committing=False,
        phase="Claimed",
    )

    class Protocol:
        def __init__(self):
            self.begin_calls = []

        def begin_commit(self, **kwargs):
            self.begin_calls.append(kwargs)

    class Coordinator:
        run_id = "run"
        contract_hash = "A" * 64
        validation_policy = "redundant-full"
        staging_root = str(tmp_path)

        def __init__(self):
            self._active = {"attempt": active}
            self._protocol = Protocol()
            self.saved_states = []

        def _require_running(self):
            return None

        def _refresh_presence_locked(self):
            return None

        def _mark_helper_seen_locked(self):
            raise AssertionError("remote attempt marked helper present")

        def _save_state(self, state):
            self.saved_states.append(state)

    coordinator = Coordinator()
    payload = lan_coordinator.SubmitPayload(
        run_id=coordinator.run_id,
        contract_hash=coordinator.contract_hash,
        worker_id="worker",
        attempt_id="attempt",
        fencing_epoch=1,
        candidate_sha256="B" * 64,
        encoded_frame_count=1,
        encode_seconds=1.0,
    )
    return coordinator, active, payload, candidate_path


def test_submit_admission_retries_a_transient_candidate_lock(
    tmp_path, monkeypatch
):
    coordinator, active, payload, candidate_path = _submit_admission_fixture(
        tmp_path
    )
    attempts = []

    def transient_unlock(path):
        assert path == str(candidate_path)
        attempts.append(path)
        return len(attempts) >= 3

    monkeypatch.setattr(
        lan_coordinator, "prove_exclusive_access", transient_unlock
    )
    monkeypatch.setattr(lan_coordinator.time, "sleep", lambda _delay: None)

    prepared, prepared_path = (
        lan_coordinator.DistributedCoordinator._prepare_submit_locked(
            coordinator, payload
        )
    )

    assert prepared is active
    assert prepared_path == str(candidate_path)
    assert len(attempts) == 3
    assert active.committing is True
    assert coordinator.saved_states == ["Committing"]
    assert len(coordinator._protocol.begin_calls) == 1


def test_submit_admission_keeps_a_permanent_candidate_lock_fail_closed(
    tmp_path, monkeypatch
):
    coordinator, active, payload, _candidate = _submit_admission_fixture(
        tmp_path
    )
    wait_for_ready = lan_coordinator._wait_for_submit_candidate_ready
    attempts = []

    def permanently_locked(path):
        attempts.append(path)
        return False

    monkeypatch.setattr(
        lan_coordinator, "prove_exclusive_access", permanently_locked
    )
    monkeypatch.setattr(
        lan_coordinator,
        "_wait_for_submit_candidate_ready",
        lambda path: wait_for_ready(path, timeout_seconds=0),
    )

    with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
        lan_coordinator.DistributedCoordinator._prepare_submit_locked(
            coordinator, payload
        )

    assert rejected.value.category == "CandidateNotReady"
    assert active.committing is False
    assert len(attempts) == 1
    assert coordinator.saved_states == []
    assert coordinator._protocol.begin_calls == []


@pytest.mark.parametrize(
    "payload_change,expected_category",
    [
        ({"run_id": "other-run"}, "BindingMismatch"),
        ({"attempt_id": "stale-attempt"}, "StaleAttempt"),
        ({"fencing_epoch": 2}, "StaleAttempt"),
    ],
)
def test_submit_admission_does_not_retry_binding_or_stale_attempts(
    tmp_path, monkeypatch, payload_change, expected_category
):
    coordinator, active, payload, _candidate = _submit_admission_fixture(
        tmp_path
    )
    waits = []

    def unexpected_wait(path):
        waits.append(path)
        return True

    monkeypatch.setattr(
        lan_coordinator,
        "_wait_for_submit_candidate_ready",
        unexpected_wait,
    )
    payload = dataclasses.replace(payload, **payload_change)

    with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
        lan_coordinator.DistributedCoordinator._prepare_submit_locked(
            coordinator, payload
        )

    assert rejected.value.category == expected_category
    assert waits == []
    assert active.committing is False
    assert coordinator._protocol.begin_calls == []


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


def test_helper_candidate_above_benefit_cap_preserves_exact_source(
    tmp_path,
    toolchain,
    monkeypatch,
):
    coordinator, root, work = make_coordinator(
        tmp_path,
        toolchain,
        [("private.mp4", 1_000_000)],
    )
    source = root / "private.mp4"
    original = source.read_bytes()
    source_info = lan_media.MediaInfo(
        video_index=0,
        video_codec="h264",
        width=640,
        height=360,
        duration=10.0,
        audio_streams=(),
        average_frame_rate="24/1",
        transfer="bt709",
        primaries="bt709",
        matrix="bt709",
        color_range="tv",
        is_hdr=False,
    )
    monkeypatch.setattr(
        lan_coordinator,
        "probe_media",
        lambda *_args, **_kwargs: source_info,
    )
    monkeypatch.setattr(
        lan_coordinator,
        "decode_audio_durations",
        lambda *_args, **_kwargs: (),
    )
    try:
        set_helper_available(coordinator)
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None
        candidate = work / "staging" / claim.candidate_name
        candidate.write_bytes(b"y" * 950_001)
        payload = lan_coordinator.SubmitPayload(
            run_id=claim.run_id,
            contract_hash=claim.contract_hash,
            worker_id=claim.worker_id,
            attempt_id=claim.attempt_id,
            fencing_epoch=claim.fencing_epoch,
            candidate_sha256="A" * 64,
            encoded_frame_count=1,
            encode_seconds=1.0,
        )

        with pytest.raises(lan_coordinator.CoordinatorError) as rejected:
            coordinator.submit(payload)

        assert rejected.value.category == "CandidateSizeNotBeneficial"
        assert source.read_bytes() == original
        assert not (root / "private.mkv").exists()
        assert not candidate.exists()
        assert not (work / "active-transaction.json").exists()
        snapshot = coordinator.snapshot()
        assert snapshot["completed"] == 0
        assert snapshot["failed"] == 1
    finally:
        coordinator.close()


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


def _encode_and_submit(
    coordinator,
    claim,
    root,
    work,
    toolchain,
    before_submit=None,
):
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
    validation_evidence = None
    if claim.validation_policy == "producer-full":
        validation_evidence = lan_media.build_producer_validation_evidence(
            evidence,
            run_id=claim.run_id,
            job_id=claim.job_id,
            worker_id=claim.worker_id,
            worker_role=claim.worker_role,
            attempt_id=claim.attempt_id,
            fencing_epoch=claim.fencing_epoch,
            contract_hash=claim.contract_hash,
            producer_build_sha256=coordinator.runtime_build_hash,
            ffmpeg_sha256=coordinator.ffmpeg_hash,
            ffprobe_sha256=coordinator.ffprobe_hash,
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
        validation_evidence=validation_evidence,
    )
    if before_submit is not None:
        before_submit()
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
        helper_worker_id="rtx",
        helper_control_id=HELPER_CONTROL_ID,
    )
    try:
        set_helper_available(coordinator)
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


def test_restart_removes_only_strict_fenced_orphan_candidates(
    tmp_path, toolchain
):
    coordinator, root, work = make_coordinator(
        tmp_path, toolchain, [("private.mp4", 20)]
    )
    claim = coordinator.claim("qsv", WorkerRole.REMOTE)
    assert claim is not None
    ready = work / "staging" / claim.candidate_name
    upload = work / "staging" / (claim.candidate_name + ".upload")
    decoy = work / "staging" / "candidate-user-file.ready.mkv"
    ready.write_bytes(b"ready")
    upload.write_bytes(b"upload")
    decoy.write_bytes(b"leave me")
    coordinator.close()

    ffmpeg, ffprobe = toolchain
    resumed = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    try:
        assert not ready.exists()
        assert not upload.exists()
        assert decoy.read_bytes() == b"leave me"
        assert (root / "private.mp4").is_file()
    finally:
        resumed.close()


def test_producer_full_commit_uses_evidence_without_coordinator_decode(
    tmp_path, toolchain, monkeypatch
):
    root = tmp_path / "media"
    work = tmp_path / "control"
    root.mkdir()
    source = root / "synthetic-input.mp4"
    _make_synthetic_source(source, toolchain, seconds=1.0)
    ffmpeg, ffprobe = toolchain
    coordinator = lan_coordinator.DistributedCoordinator(
        root=str(root),
        work_root=str(work),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        validation_policy="producer-full",
        helper_worker_id="rtx",
        helper_control_id=HELPER_CONTROL_ID,
    )

    def forbid_coordinator_full_decode():
        def forbidden(*_args, **_kwargs):
            raise AssertionError("coordinator performed a full decode")

        monkeypatch.setattr(
            lan_coordinator, "decode_audio_durations", forbidden
        )
        monkeypatch.setattr(lan_coordinator, "validate_candidate", forbidden)

    try:
        set_helper_available(coordinator)
        claim = coordinator.claim("rtx", WorkerRole.HELPER)
        assert claim is not None
        assert claim.validation_policy == "producer-full"
        outcome = _encode_and_submit(
            coordinator,
            claim,
            root,
            work,
            toolchain,
            before_submit=forbid_coordinator_full_decode,
        )

        assert outcome.category == "Committed"
        assert not source.exists()
        assert (root / "synthetic-input.mkv").is_file()
        assert coordinator.snapshot()["ValidationPolicy"] == "producer-full"
    finally:
        coordinator.close()


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
        helper_worker_id="rtx",
        helper_control_id=HELPER_CONTROL_ID,
    )
    try:
        set_helper_available(coordinator)
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
