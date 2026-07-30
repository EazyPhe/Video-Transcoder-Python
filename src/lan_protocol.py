"""Thread-safe lease scheduling for the LAN transcoder.

This module deliberately models coordination only.  Jobs are opaque identifiers
plus byte sizes; it performs no filesystem, network, process, or media I/O.

The coordinator is the sole source of time and fencing epochs.  An expired or
disconnected lease becomes suspect and cannot be requeued until an external
owner explicitly proves that the worker's resources have been released.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Callable, Iterable
import uuid


class ProtocolError(RuntimeError):
    """Base class for invalid protocol operations."""


class StaleAttemptError(ProtocolError):
    """Raised when a message does not match the job's current fenced attempt."""


class InvalidTransitionError(ProtocolError):
    """Raised when a valid attempt cannot make the requested state transition."""


class JobState(str, Enum):
    """Authoritative coordinator states for one job."""

    PENDING = "pending"
    LEASED = "leased"
    SUSPECT = "suspect"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerRole(str, Enum):
    """Scheduling roles supported by the two-lane batch."""

    REMOTE = "remote"
    HELPER = "helper"


@dataclass(frozen=True, slots=True)
class Job:
    """Opaque schedulable work.

    ``job_id`` is never interpreted by this module.  Callers should use an
    opaque token rather than a source path.
    """

    job_id: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id:
            raise ValueError("job_id must be a non-empty opaque string")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class Lease:
    """A fenced, time-limited assignment returned to a worker."""

    job_id: str
    size_bytes: int
    attempt_id: str
    fencing_epoch: int
    worker_id: str
    worker_role: WorkerRole
    lease_deadline: float


@dataclass(frozen=True, slots=True)
class AggregateSnapshot:
    """Path-free aggregate coordinator state suitable for monitoring."""

    total_jobs: int
    pending: int
    leased: int
    suspect: int
    committing: int
    completed: int
    failed: int
    remote_leased: int
    helper_leased: int
    helper_online: bool


@dataclass(slots=True)
class _JobRecord:
    job: Job
    sequence: int
    state: JobState = JobState.PENDING
    attempt_id: str | None = None
    fencing_epoch: int = 0
    worker_id: str | None = None
    worker_role: WorkerRole | None = None
    lease_deadline: float | None = None
    release_proven: bool = False


class LeaseCoordinator:
    """Coordinate remote and helper workers with leases and fencing.

    All public operations are serialized by one re-entrant lock.  The helper
    takes the largest pending job, while the remote worker takes the smallest
    pending job whenever the helper is online.  When the helper is offline,
    every pending job remains available to remote workers.
    """

    def __init__(
        self,
        jobs: Iterable[Job],
        *,
        lease_seconds: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
        attempt_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")

        records: dict[str, _JobRecord] = {}
        for sequence, job in enumerate(jobs):
            if not isinstance(job, Job):
                raise TypeError("jobs must contain Job instances")
            if job.job_id in records:
                raise ValueError(f"duplicate job_id: {job.job_id!r}")
            records[job.job_id] = _JobRecord(job=job, sequence=sequence)

        self._records = records
        self._lease_seconds = float(lease_seconds)
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory or (
            lambda: uuid.uuid4().hex
        )
        self._lock = threading.RLock()
        self._helper_online = False
        self._fencing_epoch = 0
        self._issued_attempt_ids: set[str] = set()
        self._attempt_history: dict[str, tuple[str, int]] = {}
        self._active_by_worker: dict[str, str] = {}

    @property
    def helper_online(self) -> bool:
        with self._lock:
            return self._helper_online

    @property
    def current_fencing_epoch(self) -> int:
        with self._lock:
            return self._fencing_epoch

    def set_helper_online(self, online: bool) -> int:
        """Set helper presence and suspect its active leases on disconnect.

        Returns the number of helper leases newly marked suspect.  Committing
        jobs are coordinator-owned and intentionally do not expire or become
        suspect when the worker disconnects.
        """

        if not isinstance(online, bool):
            raise TypeError("online must be bool")
        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            self._helper_online = online
            if online:
                return 0

            changed = 0
            for record in self._records.values():
                if (
                    record.state is JobState.LEASED
                    and record.worker_role is WorkerRole.HELPER
                ):
                    self._mark_suspect_locked(record)
                    changed += 1
            return changed

    def disconnect_worker(self, worker_id: str) -> int:
        """Mark every non-committing lease for ``worker_id`` suspect."""

        self._validate_worker_id(worker_id)
        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            changed = 0
            for record in self._records.values():
                if (
                    record.state is JobState.LEASED
                    and record.worker_id == worker_id
                ):
                    self._mark_suspect_locked(record)
                    changed += 1
            return changed

    def claim(
        self,
        worker_id: str,
        worker_role: WorkerRole | str,
    ) -> Lease | None:
        """Claim one pending job according to the worker's scheduling role."""

        self._validate_worker_id(worker_id)
        role = WorkerRole(worker_role)
        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)

            if role is WorkerRole.HELPER and not self._helper_online:
                return None
            if worker_id in self._active_by_worker:
                return None

            pending = [
                record
                for record in self._records.values()
                if record.state is JobState.PENDING
            ]
            if not pending:
                return None

            if role is WorkerRole.HELPER:
                record = min(
                    pending,
                    key=lambda item: (-item.job.size_bytes, item.sequence),
                )
            else:
                # Remote workers prefer short work while the helper drains the
                # large end.  With no helper, repeated claims drain all work.
                record = min(
                    pending,
                    key=lambda item: (item.job.size_bytes, item.sequence),
                )

            attempt_id = self._new_attempt_id_locked()
            self._fencing_epoch += 1
            record.state = JobState.LEASED
            record.attempt_id = attempt_id
            record.fencing_epoch = self._fencing_epoch
            record.worker_id = worker_id
            record.worker_role = role
            record.lease_deadline = now + self._lease_seconds
            record.release_proven = False
            self._attempt_history[attempt_id] = (
                record.job.job_id,
                record.fencing_epoch,
            )
            self._active_by_worker[worker_id] = record.job.job_id
            return self._lease_from_record_locked(record)

    def heartbeat(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
    ) -> Lease:
        """Extend a live lease using only the coordinator's current clock."""

        self._validate_worker_id(worker_id)
        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            record = self._resolve_attempt_locked(
                attempt_id,
                fencing_epoch,
                worker_id=worker_id,
            )
            self._require_state_locked(record, JobState.LEASED, "heartbeat")
            record.lease_deadline = now + self._lease_seconds
            return self._lease_from_record_locked(record)

    def expire_leases(self) -> int:
        """Mark every coordinator-expired lease suspect."""

        with self._lock:
            return self._expire_locked(self._coordinator_now_locked())

    def begin_commit(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
    ) -> None:
        """Fence a worker result and transfer its job to coordinator commit."""

        self._validate_worker_id(worker_id)
        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            record = self._resolve_attempt_locked(
                attempt_id,
                fencing_epoch,
                worker_id=worker_id,
            )
            self._require_state_locked(
                record,
                JobState.LEASED,
                "begin commit",
            )
            record.state = JobState.COMMITTING
            record.lease_deadline = None
            record.release_proven = False
            self._release_worker_slot_locked(record)

    def resource_release_proven(
        self,
        *,
        attempt_id: str,
        fencing_epoch: int,
    ) -> None:
        """Record external proof that a suspect attempt owns no resources."""

        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            record = self._resolve_attempt_locked(attempt_id, fencing_epoch)
            self._require_state_locked(
                record,
                JobState.SUSPECT,
                "prove resource release",
            )
            record.release_proven = True

    def requeue(
        self,
        *,
        attempt_id: str,
        fencing_epoch: int,
    ) -> None:
        """Requeue a suspect attempt after explicit resource-release proof."""

        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            record = self._resolve_attempt_locked(attempt_id, fencing_epoch)
            self._require_state_locked(record, JobState.SUSPECT, "requeue")
            if not record.release_proven:
                raise InvalidTransitionError(
                    "resource release must be proven before requeue"
                )
            self._release_worker_slot_locked(record)
            record.state = JobState.PENDING
            record.attempt_id = None
            record.worker_id = None
            record.worker_role = None
            record.lease_deadline = None
            record.release_proven = False

    def mark_completed(
        self,
        *,
        attempt_id: str,
        fencing_epoch: int,
    ) -> None:
        """Finish a coordinator-owned commit successfully."""

        with self._lock:
            record = self._resolve_attempt_locked(attempt_id, fencing_epoch)
            self._require_state_locked(
                record,
                JobState.COMMITTING,
                "complete",
            )
            record.state = JobState.COMPLETED
            record.release_proven = True

    def mark_failed(
        self,
        *,
        attempt_id: str,
        fencing_epoch: int,
    ) -> None:
        """Finish a committing or safely released suspect attempt as failed."""

        with self._lock:
            now = self._coordinator_now_locked()
            self._expire_locked(now)
            record = self._resolve_attempt_locked(attempt_id, fencing_epoch)
            if record.state is JobState.SUSPECT:
                if not record.release_proven:
                    raise InvalidTransitionError(
                        "resource release must be proven before failure"
                    )
                self._release_worker_slot_locked(record)
            elif record.state is not JobState.COMMITTING:
                raise InvalidTransitionError(
                    f"cannot fail job from {record.state.value}"
                )
            record.state = JobState.FAILED
            record.lease_deadline = None

    def fail_pending(self, job_id: str) -> None:
        """Mark an unclaimed job terminally failed."""

        with self._lock:
            try:
                record = self._records[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id!r}") from exc
            self._require_state_locked(record, JobState.PENDING, "fail")
            record.state = JobState.FAILED

    def snapshot(self) -> AggregateSnapshot:
        """Return a path-free aggregate view, applying lease expiration first."""

        with self._lock:
            self._expire_locked(self._coordinator_now_locked())
            counts = Counter(record.state for record in self._records.values())
            remote_leased = sum(
                1
                for record in self._records.values()
                if (
                    record.state is JobState.LEASED
                    and record.worker_role is WorkerRole.REMOTE
                )
            )
            helper_leased = sum(
                1
                for record in self._records.values()
                if (
                    record.state is JobState.LEASED
                    and record.worker_role is WorkerRole.HELPER
                )
            )
            return AggregateSnapshot(
                total_jobs=len(self._records),
                pending=counts[JobState.PENDING],
                leased=counts[JobState.LEASED],
                suspect=counts[JobState.SUSPECT],
                committing=counts[JobState.COMMITTING],
                completed=counts[JobState.COMPLETED],
                failed=counts[JobState.FAILED],
                remote_leased=remote_leased,
                helper_leased=helper_leased,
                helper_online=self._helper_online,
            )

    def _coordinator_now_locked(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ProtocolError("coordinator clock must return a finite value")
        return now

    def _new_attempt_id_locked(self) -> str:
        for _ in range(100):
            candidate = self._attempt_id_factory()
            if (
                isinstance(candidate, str)
                and candidate
                and candidate not in self._issued_attempt_ids
            ):
                self._issued_attempt_ids.add(candidate)
                return candidate
        raise ProtocolError("attempt_id_factory did not produce a unique ID")

    def _expire_locked(self, now: float) -> int:
        changed = 0
        for record in self._records.values():
            if (
                record.state is JobState.LEASED
                and record.lease_deadline is not None
                and now >= record.lease_deadline
            ):
                self._mark_suspect_locked(record)
                changed += 1
        return changed

    def _mark_suspect_locked(self, record: _JobRecord) -> None:
        if record.state is not JobState.LEASED:
            raise InvalidTransitionError(
                f"cannot suspect job from {record.state.value}"
            )
        record.state = JobState.SUSPECT
        record.lease_deadline = None
        record.release_proven = False

    def _resolve_attempt_locked(
        self,
        attempt_id: str,
        fencing_epoch: int,
        *,
        worker_id: str | None = None,
    ) -> _JobRecord:
        history = self._attempt_history.get(attempt_id)
        if history is None or history[1] != fencing_epoch:
            raise StaleAttemptError("unknown or stale attempt")
        record = self._records[history[0]]
        if (
            record.attempt_id != attempt_id
            or record.fencing_epoch != fencing_epoch
            or (
                worker_id is not None
                and record.worker_id != worker_id
            )
        ):
            raise StaleAttemptError("attempt is no longer authoritative")
        return record

    @staticmethod
    def _require_state_locked(
        record: _JobRecord,
        required: JobState,
        operation: str,
    ) -> None:
        if record.state is not required:
            raise InvalidTransitionError(
                f"cannot {operation} job from {record.state.value}"
            )

    def _release_worker_slot_locked(self, record: _JobRecord) -> None:
        if (
            record.worker_id is not None
            and self._active_by_worker.get(record.worker_id)
            == record.job.job_id
        ):
            del self._active_by_worker[record.worker_id]

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id must be a non-empty string")

    @staticmethod
    def _lease_from_record_locked(record: _JobRecord) -> Lease:
        if (
            record.attempt_id is None
            or record.worker_id is None
            or record.worker_role is None
            or record.lease_deadline is None
        ):
            raise ProtocolError("leased record is incomplete")
        return Lease(
            job_id=record.job.job_id,
            size_bytes=record.job.size_bytes,
            attempt_id=record.attempt_id,
            fencing_epoch=record.fencing_epoch,
            worker_id=record.worker_id,
            worker_role=record.worker_role,
            lease_deadline=record.lease_deadline,
        )


__all__ = [
    "AggregateSnapshot",
    "InvalidTransitionError",
    "Job",
    "JobState",
    "Lease",
    "LeaseCoordinator",
    "ProtocolError",
    "StaleAttemptError",
    "WorkerRole",
]
