"""Deterministic tests for the pure LAN lease/scheduling state machine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import threading

import pytest

from lan_protocol import (
    InvalidTransitionError,
    Job,
    JobState,
    LeaseCoordinator,
    StaleAttemptError,
    WorkerRole,
)


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _complete(coordinator: LeaseCoordinator, lease) -> None:
    coordinator.begin_commit(
        worker_id=lease.worker_id,
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )
    coordinator.mark_completed(
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )


def test_online_helper_takes_largest_and_remote_takes_smallest():
    coordinator = LeaseCoordinator(
        [
            Job("ten", 10),
            Job("hundred", 100),
            Job("thirty", 30),
            Job("five", 5),
        ]
    )
    coordinator.set_helper_online(True)

    helper = coordinator.claim("rtx", WorkerRole.HELPER)
    remote = coordinator.claim("qsv", WorkerRole.REMOTE)

    assert helper is not None
    assert remote is not None
    assert (helper.job_id, helper.size_bytes) == ("hundred", 100)
    assert (remote.job_id, remote.size_bytes) == ("five", 5)


def test_remote_can_drain_every_remaining_job_while_helper_offline():
    coordinator = LeaseCoordinator(
        [
            Job("middle", 20),
            Job("largest", 30),
            Job("smallest", 10),
        ]
    )

    claimed_sizes = []
    for index in range(3):
        lease = coordinator.claim(f"remote-{index}", WorkerRole.REMOTE)
        assert lease is not None
        claimed_sizes.append(lease.size_bytes)

    assert claimed_sizes == [10, 20, 30]
    assert coordinator.claim("remote-extra", WorkerRole.REMOTE) is None


def test_helper_cannot_claim_while_offline():
    coordinator = LeaseCoordinator([Job("job", 10)])

    assert coordinator.claim("rtx", WorkerRole.HELPER) is None
    assert coordinator.snapshot().pending == 1


@pytest.mark.parametrize("lease_seconds", [0, -1, float("nan"), float("inf")])
def test_lease_duration_must_be_positive_and_finite(lease_seconds):
    with pytest.raises(ValueError):
        LeaseCoordinator([Job("job", 10)], lease_seconds=lease_seconds)


def test_heartbeat_uses_coordinator_time_and_extends_deadline():
    clock = FakeClock(100.0)
    coordinator = LeaseCoordinator(
        [Job("job", 10)],
        lease_seconds=20.0,
        clock=clock,
    )
    lease = coordinator.claim("remote", WorkerRole.REMOTE)
    assert lease is not None
    assert lease.lease_deadline == 120.0

    clock.advance(15.0)
    renewed = coordinator.heartbeat(
        worker_id="remote",
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )

    assert renewed.lease_deadline == 135.0
    clock.advance(19.0)
    assert coordinator.snapshot().leased == 1
    clock.advance(1.0)
    assert coordinator.snapshot().suspect == 1


def test_late_heartbeat_is_rejected_after_coordinator_expiry():
    clock = FakeClock()
    coordinator = LeaseCoordinator(
        [Job("job", 10)],
        lease_seconds=5.0,
        clock=clock,
    )
    lease = coordinator.claim("remote", WorkerRole.REMOTE)
    assert lease is not None

    clock.advance(5.0)
    with pytest.raises(InvalidTransitionError, match="suspect"):
        coordinator.heartbeat(
            worker_id="remote",
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )


def test_expired_attempt_requires_release_proof_before_requeue():
    clock = FakeClock()
    coordinator = LeaseCoordinator(
        [Job("job", 10)],
        lease_seconds=5.0,
        clock=clock,
    )
    first = coordinator.claim("helper", WorkerRole.REMOTE)
    assert first is not None
    clock.advance(6.0)
    assert coordinator.expire_leases() == 1

    with pytest.raises(
        InvalidTransitionError,
        match="resource release",
    ):
        coordinator.requeue(
            attempt_id=first.attempt_id,
            fencing_epoch=first.fencing_epoch,
        )

    coordinator.resource_release_proven(
        attempt_id=first.attempt_id,
        fencing_epoch=first.fencing_epoch,
    )
    coordinator.requeue(
        attempt_id=first.attempt_id,
        fencing_epoch=first.fencing_epoch,
    )

    second = coordinator.claim("remote", WorkerRole.REMOTE)
    assert second is not None
    assert second.attempt_id != first.attempt_id
    assert second.fencing_epoch > first.fencing_epoch


def test_helper_disconnect_suspects_its_job_and_remote_falls_back():
    coordinator = LeaseCoordinator(
        [
            Job("large", 100),
            Job("medium", 50),
            Job("small", 10),
        ]
    )
    coordinator.set_helper_online(True)
    helper = coordinator.claim("rtx", WorkerRole.HELPER)
    remote_small = coordinator.claim("qsv-1", WorkerRole.REMOTE)
    assert helper is not None and helper.job_id == "large"
    assert remote_small is not None and remote_small.job_id == "small"

    assert coordinator.set_helper_online(False) == 1
    snapshot = coordinator.snapshot()
    assert snapshot.suspect == 1
    assert snapshot.helper_online is False

    remote_medium = coordinator.claim("qsv-2", WorkerRole.REMOTE)
    assert remote_medium is not None
    assert remote_medium.job_id == "medium"

    coordinator.resource_release_proven(
        attempt_id=helper.attempt_id,
        fencing_epoch=helper.fencing_epoch,
    )
    coordinator.requeue(
        attempt_id=helper.attempt_id,
        fencing_epoch=helper.fencing_epoch,
    )
    remote_large = coordinator.claim("qsv-3", WorkerRole.REMOTE)
    assert remote_large is not None
    assert remote_large.job_id == "large"


def test_requeued_attempt_is_fenced_from_late_submission():
    clock = FakeClock()
    coordinator = LeaseCoordinator(
        [Job("job", 10)],
        lease_seconds=5.0,
        clock=clock,
    )
    old = coordinator.claim("old-worker", WorkerRole.REMOTE)
    assert old is not None
    clock.advance(5.0)
    coordinator.expire_leases()
    coordinator.resource_release_proven(
        attempt_id=old.attempt_id,
        fencing_epoch=old.fencing_epoch,
    )
    coordinator.requeue(
        attempt_id=old.attempt_id,
        fencing_epoch=old.fencing_epoch,
    )
    current = coordinator.claim("current-worker", WorkerRole.REMOTE)
    assert current is not None

    with pytest.raises(StaleAttemptError):
        coordinator.begin_commit(
            worker_id=old.worker_id,
            attempt_id=old.attempt_id,
            fencing_epoch=old.fencing_epoch,
        )
    with pytest.raises(StaleAttemptError):
        coordinator.mark_completed(
            attempt_id=old.attempt_id,
            fencing_epoch=old.fencing_epoch,
        )

    _complete(coordinator, current)
    assert coordinator.snapshot().completed == 1


def test_wrong_worker_and_wrong_epoch_are_rejected():
    coordinator = LeaseCoordinator([Job("job", 10)])
    lease = coordinator.claim("remote", WorkerRole.REMOTE)
    assert lease is not None

    with pytest.raises(StaleAttemptError):
        coordinator.heartbeat(
            worker_id="impostor",
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )
    with pytest.raises(StaleAttemptError):
        coordinator.begin_commit(
            worker_id=lease.worker_id,
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch + 1,
        )


def test_committing_state_does_not_expire():
    clock = FakeClock()
    coordinator = LeaseCoordinator(
        [Job("job", 10)],
        lease_seconds=5.0,
        clock=clock,
    )
    lease = coordinator.claim("remote", WorkerRole.REMOTE)
    assert lease is not None
    coordinator.begin_commit(
        worker_id=lease.worker_id,
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )

    clock.advance(10_000.0)
    assert coordinator.expire_leases() == 0
    snapshot = coordinator.snapshot()
    assert snapshot.committing == 1
    assert snapshot.suspect == 0

    coordinator.mark_completed(
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )
    assert coordinator.snapshot().completed == 1


def test_failed_is_terminal_and_suspect_failure_requires_release_proof():
    clock = FakeClock()
    coordinator = LeaseCoordinator(
        [Job("attempted", 10), Job("pending", 20)],
        lease_seconds=5.0,
        clock=clock,
    )
    lease = coordinator.claim("remote", WorkerRole.REMOTE)
    assert lease is not None
    clock.advance(5.0)

    with pytest.raises(
        InvalidTransitionError,
        match="resource release",
    ):
        coordinator.mark_failed(
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )
    coordinator.resource_release_proven(
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )
    coordinator.mark_failed(
        attempt_id=lease.attempt_id,
        fencing_epoch=lease.fencing_epoch,
    )
    coordinator.fail_pending("pending")

    snapshot = coordinator.snapshot()
    assert snapshot.failed == 2
    assert snapshot.pending == 0
    with pytest.raises(InvalidTransitionError):
        coordinator.requeue(
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )


def test_one_worker_cannot_hold_two_live_leases():
    coordinator = LeaseCoordinator([Job("one", 1), Job("two", 2)])

    first = coordinator.claim("remote", WorkerRole.REMOTE)
    second = coordinator.claim("remote", WorkerRole.REMOTE)

    assert first is not None
    assert second is None
    assert coordinator.snapshot().leased == 1


def test_simultaneous_claims_are_unique_and_serialized_by_lock():
    job_count = 64
    coordinator = LeaseCoordinator(
        Job(f"job-{index}", index + 1)
        for index in range(job_count)
    )
    coordinator.set_helper_online(True)
    barrier = threading.Barrier(job_count)

    def claim(index: int):
        barrier.wait(timeout=10)
        role = (
            WorkerRole.HELPER
            if index % 2
            else WorkerRole.REMOTE
        )
        return coordinator.claim(f"worker-{index}", role)

    with ThreadPoolExecutor(max_workers=job_count) as executor:
        leases = list(executor.map(claim, range(job_count)))

    assert all(lease is not None for lease in leases)
    assert len({lease.job_id for lease in leases}) == job_count
    assert len({lease.attempt_id for lease in leases}) == job_count
    epochs = sorted(lease.fencing_epoch for lease in leases)
    assert epochs == list(range(1, job_count + 1))
    snapshot = coordinator.snapshot()
    assert snapshot.pending == 0
    assert snapshot.leased == job_count


def test_simultaneous_claims_by_same_worker_yield_only_one_lease():
    thread_count = 16
    coordinator = LeaseCoordinator(
        Job(f"job-{index}", index)
        for index in range(thread_count)
    )
    barrier = threading.Barrier(thread_count)

    def claim(_index: int):
        barrier.wait(timeout=10)
        return coordinator.claim("one-worker", WorkerRole.REMOTE)

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        leases = list(executor.map(claim, range(thread_count)))

    assert sum(lease is not None for lease in leases) == 1
    assert coordinator.snapshot().leased == 1


def test_aggregate_snapshot_contains_no_paths_or_identifiers():
    coordinator = LeaseCoordinator([Job("opaque-token", 10)])
    coordinator.set_helper_online(True)
    lease = coordinator.claim("rtx", WorkerRole.HELPER)
    assert lease is not None

    snapshot = asdict(coordinator.snapshot())

    assert snapshot == {
        "total_jobs": 1,
        "pending": 0,
        "leased": 1,
        "suspect": 0,
        "committing": 0,
        "completed": 0,
        "failed": 0,
        "remote_leased": 0,
        "helper_leased": 1,
        "helper_online": True,
    }
    forbidden = (
        "path",
        "source",
        "destination",
        "attempt_id",
        "worker_id",
    )
    assert not any(
        marker in key
        for key in snapshot
        for marker in forbidden
    )


def test_job_state_values_cover_every_protocol_terminal_and_active_state():
    assert {state.value for state in JobState} == {
        "pending",
        "leased",
        "suspect",
        "committing",
        "completed",
        "failed",
    }
