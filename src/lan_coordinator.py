"""Authoritative storage-host coordinator for two-PC LAN transcoding.

The coordinator is the only component permitted to publish a final filename or
delete an original. Workers receive fenced, attempt-specific jobs and may only
produce candidates in the coordinator staging area.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from lan_media import (
    DEFAULT_CONTRACT,
    SUPPORTED_EXTENSIONS,
    CandidateEvidence,
    MediaContract,
    MediaContractError,
    MediaInfo,
    ProducerValidationEvidence,
    decode_audio_durations,
    maximum_beneficial_output_bytes,
    probe_media,
    sha256_file,
    validate_candidate,
    validate_candidate_metadata,
    validate_top_level_relative_name,
)
from lan_protocol import (
    AggregateSnapshot,
    InvalidTransitionError,
    Job,
    Lease,
    LeaseCoordinator,
    ProtocolError,
    StaleAttemptError,
    WorkerRole,
)
from lan_windows import (
    FileIdentity,
    FileSafetyError,
    PinnedFile,
    assert_no_reparse_components,
    delete_verified,
    flush_verified,
    get_identity,
    get_pinned_identity,
    open_directory_pin,
    open_exclusive_lock,
    open_read_pin,
    protect_text,
    prove_exclusive_access,
    rename_verified,
)
import lan_media
import lan_protocol
import lan_recovery
import lan_windows
from lan_recovery import RecoveryError, recover_active_transaction


SCHEMA_VERSION = 1
DEFAULT_RESERVE_BYTES = 10 * 1024**3
MINIMUM_JOB_RESERVATION = 2 * 1024**3
RUN_MARKER_NAME = "coordinator-run.marker"
WORK_PHASES = frozenset(
    {
        "Claimed",
        "SourceRead",
        "Converting",
        "LocalValidation",
        "Uploading",
        "UploadVerification",
        "RemoteValidation",
        "ProducerFullValidation",
        "CoordinatorIntegrity",
        "PostPublishIntegrity",
        "Publishing",
    }
)
SCHEDULING_MODES = frozenset({"balanced", "prefer-helper", "local-only"})
VALIDATION_POLICIES = frozenset({"redundant-full", "producer-full"})
HELPER_CONTROL_ID_PATTERN = re.compile(r"[0-9a-f]{32,64}\Z")
MAXIMUM_HELPER_CONTROL_REVISION = (1 << 63) - 1


class CoordinatorError(RuntimeError):
    """A stable path-redacted coordinator failure."""

    def __init__(self, category: str, *, systemic: bool = False):
        super().__init__(category)
        self.category = category
        self.systemic = systemic


@dataclass(frozen=True)
class PlannedJob:
    job_id: str
    relative_name: str
    source_path: str
    destination_path: str
    same_path: bool
    identity: FileIdentity
    size_bytes: int


@dataclass
class ActiveAttempt:
    lease: Lease
    job: PlannedJob
    source_pin: PinnedFile
    source_alias_pin: PinnedFile
    reservation_bytes: int
    candidate_name: str
    source_alias_name: str
    progress_seconds: float = 0.0
    progress_frame_count: int = 0
    progress_updated: float = 0.0
    committing: bool = False
    terminal_failure_pending: bool = False
    phase: str = "Claimed"
    media_duration_seconds: float = 0.0
    encode_elapsed_seconds: float = 0.0
    transfer_bytes: int = 0
    transfer_total_bytes: int = 0
    transfer_elapsed_seconds: float = 0.0
    claimed_monotonic: float = 0.0
    claimed_utc: float = 0.0
    source_alias_release_proven: bool = False


@dataclass(frozen=True)
class ClaimPayload:
    schema_version: int
    run_id: str
    contract_hash: str
    job_id: str
    attempt_id: str
    fencing_epoch: int
    worker_id: str
    worker_role: str
    lease_deadline: float
    source_alias: str
    candidate_name: str
    source_identity: dict[str, str | int]
    run_marker_identity: dict[str, str | int]
    source_size_bytes: int
    maximum_output_bytes: int
    validation_policy: str = "redundant-full"


@dataclass(frozen=True)
class SubmitPayload:
    run_id: str
    contract_hash: str
    worker_id: str
    attempt_id: str
    fencing_epoch: int
    candidate_sha256: str
    encoded_frame_count: int
    encode_seconds: float
    validation_evidence: ProducerValidationEvidence | None = None


@dataclass(frozen=True)
class CommitOutcome:
    kind: str
    category: str


@dataclass
class AsyncSubmission:
    payload: SubmitPayload
    outcome: CommitOutcome | None = None


def _normalized_path(path: str) -> str:
    return os.path.abspath(path).rstrip("\\/").upper()


SOURCE_ALIAS_PREFIX = ".lan-source-"
ORPHAN_CANDIDATE_NAME = re.compile(
    r"^candidate-[0-9a-f]{32}-[0-9a-f]{32}\.ready\.mkv(?:\.upload)?$"
)
TRANSIENT_WINDOWS_FILE_ERRORS = frozenset({5, 32, 33})
SUBMIT_CANDIDATE_READY_TIMEOUT_SECONDS = 3.0
SUBMIT_CANDIDATE_READY_INTERVAL_SECONDS = 0.1


def _rename_verified_with_retry(
    source: str,
    destination: str,
    identity: FileIdentity,
    *,
    timeout_seconds: float = 10.0,
) -> bool:
    """Retry only transient Windows access/share failures, preserving no-replace."""

    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        try:
            return rename_verified(source, destination, identity)
        except FileSafetyError as exc:
            if (
                exc.winerror not in TRANSIENT_WINDOWS_FILE_ERRORS
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.1)


def _wait_for_exclusive_access(
    path: str,
    *,
    timeout_seconds: float = 10.0,
) -> bool:
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        if prove_exclusive_access(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _wait_for_submit_candidate_ready(
    path: str,
    *,
    timeout_seconds: float = SUBMIT_CANDIDATE_READY_TIMEOUT_SECONDS,
) -> bool:
    """Wait briefly for a just-published candidate to become visible and free.

    The helper closes an SMB handle immediately before submitting. Windows AV,
    indexing, or an SMB lease break can retain that exact candidate briefly.
    Keep this admission wait well below the 10-second transport timeout so the
    caller still receives a typed response. Missing or persistently busy files
    continue to fail closed as ``CandidateNotReady``.
    """

    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        if os.path.isfile(path) and prove_exclusive_access(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SUBMIT_CANDIDATE_READY_INTERVAL_SECONDS)


def _path_hash(path: str) -> str:
    return hashlib.sha256(_normalized_path(path).encode("utf-8")).hexdigest().upper()


def _file_sha256(path: str) -> str:
    return sha256_file(path)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _atomic_write_json(path: str, value: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_json(path: str, value: object) -> None:
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: str, expected: type) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise CoordinatorError("ControlStateInvalid", systemic=True) from exc
    if not isinstance(value, expected):
        raise CoordinatorError("ControlStateInvalid", systemic=True)
    return value


class DistributedCoordinator:
    """Single-authority scheduler, validator, and transactional committer."""

    def __init__(
        self,
        *,
        root: str,
        work_root: str,
        ffmpeg: str,
        ffprobe: str,
        reserve_bytes: int = DEFAULT_RESERVE_BYTES,
        lease_seconds: float = 45.0,
        helper_presence_seconds: float = 30.0,
        scheduling_mode: str = "balanced",
        helper_startup_grace_seconds: float = 60.0,
        helper_fallback_after_seconds: float = 90.0,
        helper_recovery_stable_seconds: float = 15.0,
        validation_policy: str = "redundant-full",
        helper_worker_id: str = "helper-nvenc",
        helper_control_id: str = "0" * 32,
        consecutive_failure_limit: int = 3,
        contract: MediaContract = DEFAULT_CONTRACT,
        accepted_legacy_settings_hashes: Iterable[str] = (),
        legacy_ledger_paths: Iterable[str] = (),
        run_secret: bytes | None = None,
        clock=time.monotonic,
    ) -> None:
        self.root = os.path.abspath(root)
        self.work_root = os.path.abspath(work_root)
        self.ffmpeg = os.path.abspath(ffmpeg)
        self.ffprobe = os.path.abspath(ffprobe)
        self.reserve_bytes = int(reserve_bytes)
        self.helper_presence_seconds = float(helper_presence_seconds)
        self.scheduling_mode = str(scheduling_mode).lower()
        self.helper_startup_grace_seconds = float(
            helper_startup_grace_seconds
        )
        self.helper_fallback_after_seconds = float(
            helper_fallback_after_seconds
        )
        self.helper_recovery_stable_seconds = float(
            helper_recovery_stable_seconds
        )
        self.validation_policy = str(validation_policy).lower()
        self.helper_worker_id = str(helper_worker_id)
        self.helper_control_id = str(helper_control_id)
        self.consecutive_failure_limit = int(consecutive_failure_limit)
        self.contract = contract
        self.contract_hash = contract.digest()
        self.accepted_legacy_settings_hashes = frozenset(
            str(value).upper()
            for value in accepted_legacy_settings_hashes
            if value
        )
        self.legacy_ledger_paths = tuple(
            os.path.abspath(str(value))
            for value in legacy_ledger_paths
            if value
        )
        self.run_secret = run_secret or secrets.token_bytes(32)
        self.run_id = uuid.uuid4().hex
        self.clock = clock
        self._lock = threading.RLock()
        self._commit_lock = threading.Lock()
        self._async_submissions: dict[str, AsyncSubmission] = {}
        self._async_threads: set[threading.Thread] = set()
        self._helper_last_seen: float | None = None
        self._helper_stable_since: float | None = None
        self._helper_ever_seen = False
        self._fallback_active = False
        self._helper_control_known = False
        self._helper_control_revision = 0
        self._helper_paused = False
        self._active: dict[str, ActiveAttempt] = {}
        self._reserved_bytes = 0
        self._systemic_failure = ""
        self._consecutive_failures = 0
        self._started_monotonic = float(clock())
        self._started_utc = time.time()
        self._terminal_states: dict[str, str] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=100)
        self._last_state_payload: bytes | None = None
        self._closed = False
        self._root_pin_context = None
        self._root_pin = None
        self._coordinator_lock: PinnedFile | None = None
        self._run_marker_pin: PinnedFile | None = None
        self._immutable_pins: list[PinnedFile] = []

        if self.reserve_bytes < DEFAULT_RESERVE_BYTES:
            raise CoordinatorError("ReserveBelowMinimum", systemic=True)
        if self.helper_presence_seconds <= 0:
            raise CoordinatorError("HelperPresenceInvalid", systemic=True)
        if self.scheduling_mode not in SCHEDULING_MODES:
            raise CoordinatorError("SchedulingModeInvalid", systemic=True)
        for value in (
            self.helper_startup_grace_seconds,
            self.helper_fallback_after_seconds,
            self.helper_recovery_stable_seconds,
        ):
            if not math.isfinite(value) or value < 0:
                raise CoordinatorError(
                    "SchedulingWindowInvalid", systemic=True
                )
        if self.validation_policy not in VALIDATION_POLICIES:
            raise CoordinatorError("ValidationPolicyInvalid", systemic=True)
        if (
            not self.helper_worker_id
            or len(self.helper_worker_id) > 128
            or HELPER_CONTROL_ID_PATTERN.fullmatch(self.helper_control_id)
            is None
        ):
            raise CoordinatorError("HelperControlConfigInvalid", systemic=True)
        if self.consecutive_failure_limit <= 0:
            raise CoordinatorError("FailureLimitInvalid", systemic=True)
        for required in (
            self.root,
            self.work_root,
            self.ffmpeg,
            self.ffprobe,
            *self.legacy_ledger_paths,
        ):
            assert_no_reparse_components(required)
        if not os.path.isdir(self.root):
            raise CoordinatorError("RootUnavailable", systemic=True)
        if _normalized_path(self.work_root).startswith(
            _normalized_path(self.root) + "\\"
        ):
            raise CoordinatorError("WorkRootInsideMediaRoot", systemic=True)
        if not os.path.isfile(self.ffmpeg) or not os.path.isfile(self.ffprobe):
            raise CoordinatorError("ToolchainUnavailable", systemic=True)

        os.makedirs(self.work_root, exist_ok=True)
        assert_no_reparse_components(self.work_root)
        self.lock_path = os.path.join(self.work_root, "coordinator.lock")
        assert_no_reparse_components(self.lock_path)
        self.staging_root = os.path.join(self.work_root, "staging")
        os.makedirs(self.staging_root, exist_ok=True)
        assert_no_reparse_components(self.staging_root)
        if (
            os.stat(self.root, follow_symlinks=False).st_dev
            != os.stat(self.staging_root, follow_symlinks=False).st_dev
        ):
            raise CoordinatorError(
                "WorkRootVolumeMismatch", systemic=True
            )
        self.state_path = os.path.join(self.work_root, "state.json")
        self.ledger_path = os.path.join(self.work_root, "completed-ledger.json")
        self.journal_path = os.path.join(
            self.work_root, "active-transaction.json"
        )
        try:
            self._coordinator_lock = open_exclusive_lock(self.lock_path)
        except FileSafetyError as exc:
            raise CoordinatorError(
                "CoordinatorAlreadyRunning", systemic=True
            ) from exc

        self._root_pin_context = open_directory_pin(self.root)
        try:
            self._root_pin = self._root_pin_context.__enter__()
        except Exception:
            self._coordinator_lock.close()
            self._coordinator_lock = None
            self._root_pin_context = None
            raise
        immutable_paths = [self.ffmpeg, self.ffprobe]
        if getattr(sys, "frozen", False):
            immutable_paths.append(os.path.abspath(sys.executable))
        else:
            immutable_paths.extend(
                os.path.abspath(module.__file__)
                for module in (
                    sys.modules[__name__],
                    lan_media,
                    lan_protocol,
                    lan_recovery,
                    lan_windows,
                )
                if getattr(module, "__file__", None)
            )
        immutable_hashes: dict[str, str] = {}
        try:
            for immutable_path in dict.fromkeys(immutable_paths):
                pin = open_read_pin(immutable_path)
                self._immutable_pins.append(pin)
                immutable_hashes[
                    os.path.basename(immutable_path).lower()
                ] = _file_sha256(immutable_path)
        except Exception:
            for pin in self._immutable_pins:
                pin.close()
            self._immutable_pins.clear()
            self._root_pin_context.__exit__(None, None, None)
            self._root_pin_context = None
            self._coordinator_lock.close()
            self._coordinator_lock = None
            raise
        try:
            runtime_path = (
                os.path.abspath(sys.executable)
                if getattr(sys, "frozen", False)
                else os.path.abspath(__file__)
            )
            self.runtime_build_hash = immutable_hashes[
                os.path.basename(runtime_path).lower()
            ]
            self.ffmpeg_hash = _file_sha256(self.ffmpeg)
            self.ffprobe_hash = _file_sha256(self.ffprobe)
            root_stat = os.stat(self.root, follow_symlinks=False)
            root_identity = {
                "device": int(root_stat.st_dev),
                "inode": int(root_stat.st_ino),
                "creation_ns": int(root_stat.st_ctime_ns),
            }
            self.runner_binding_hash = hashlib.sha256(
                _canonical_json(
                    {
                        "schema": SCHEMA_VERSION,
                        "contract_hash": self.contract_hash,
                        "ffmpeg_hash": self.ffmpeg_hash,
                        "ffprobe_hash": self.ffprobe_hash,
                        "root": _normalized_path(self.root),
                        "root_identity": root_identity,
                        "immutable_hashes": immutable_hashes,
                    }
                )
            ).hexdigest().upper()
            try:
                recover_active_transaction(
                    journal_path=self.journal_path,
                    root=self.root,
                    staging_root=self.staging_root,
                    runner_binding_hash=self.runner_binding_hash,
                    contract_hash=self.contract_hash,
                    ledger_path=self.ledger_path,
                )
            except RecoveryError as exc:
                raise CoordinatorError(
                    exc.category, systemic=True
                ) from exc
            self._rotate_run_marker()
            self._cleanup_fenced_orphan_candidates()
            self._cleanup_orphan_source_aliases()
            self._ledger = self._load_ledger()
            self._jobs, self._initial_skipped = self._plan_jobs()
            if any(
                not prove_exclusive_access(job.source_path)
                for job in self._jobs.values()
            ):
                raise CoordinatorError(
                    "ResumeSourceBusy", systemic=True
                )
            self._protocol = LeaseCoordinator(
                (
                    Job(job_id=job.job_id, size_bytes=job.size_bytes)
                    for job in self._jobs.values()
                ),
                lease_seconds=lease_seconds,
                clock=clock,
            )
            self._save_state("Ready")
        except Exception:
            self.close()
            raise

    @property
    def systemic_failure(self) -> str:
        with self._lock:
            return self._systemic_failure

    def _require_running(self) -> None:
        if self._closed:
            raise CoordinatorError("CoordinatorClosed", systemic=True)
        if self._systemic_failure:
            raise CoordinatorError(self._systemic_failure, systemic=True)

    def _load_ledger(self) -> list[dict[str, Any]]:
        paths = [self.ledger_path, *self.legacy_ledger_paths]
        records: list[dict[str, Any]] = []
        seen: set[bytes] = set()
        for path in paths:
            if not os.path.exists(path):
                if path != self.ledger_path:
                    raise CoordinatorError(
                        "LegacyLedgerUnavailable", systemic=True
                    )
                continue
            value = _read_json(path, list)
            if not all(isinstance(item, dict) for item in value):
                raise CoordinatorError("LedgerInvalid", systemic=True)
            for item in value:
                fingerprint = _canonical_json(item)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    records.append(dict(item))
        return records

    def _rotate_run_marker(self) -> None:
        marker_path = os.path.join(self.staging_root, RUN_MARKER_NAME)
        assert_no_reparse_components(marker_path)
        if os.path.exists(marker_path):
            if not prove_exclusive_access(marker_path):
                raise CoordinatorError(
                    "ResumeWorkersActive", systemic=True
                )
            marker_identity = get_identity(marker_path)
            if not delete_verified(marker_path, marker_identity):
                raise CoordinatorError(
                    "RunMarkerCleanupFailed", systemic=True
                )
        _create_json(
            marker_path,
            {
                "SchemaVersion": SCHEMA_VERSION,
                "RunId": self.run_id,
                "ContractHash": self.contract_hash,
                "RunnerBindingHash": self.runner_binding_hash,
            },
        )
        marker_identity = flush_verified(
            marker_path, get_identity(marker_path)
        )
        marker_pin = open_read_pin(marker_path)
        if marker_pin.identity != marker_identity:
            marker_pin.close()
            raise CoordinatorError(
                "RunMarkerIdentityChanged", systemic=True
            )
        self._run_marker_pin = marker_pin

    def _cleanup_orphan_source_aliases(self) -> None:
        """Remove only coordinator-owned opaque aliases after worker fencing."""

        with os.scandir(self.staging_root) as entries:
            aliases = [
                os.path.abspath(entry.path)
                for entry in entries
                if entry.name.startswith(SOURCE_ALIAS_PREFIX)
                and entry.is_file(follow_symlinks=False)
            ]
        for alias_path in aliases:
            if not prove_exclusive_access(alias_path):
                raise CoordinatorError(
                    "SourceAliasBusy", systemic=True
                )
            identity = get_identity(alias_path)
            if not delete_verified(alias_path, identity):
                raise CoordinatorError(
                    "SourceAliasCleanupFailed", systemic=True
                )

    def _cleanup_fenced_orphan_candidates(self) -> None:
        """Delete only exact coordinator-owned candidates after run fencing."""

        with os.scandir(self.staging_root) as entries:
            candidates = [
                os.path.abspath(entry.path)
                for entry in entries
                if ORPHAN_CANDIDATE_NAME.fullmatch(entry.name)
                and entry.is_file(follow_symlinks=False)
            ]
        for candidate_path in candidates:
            if not prove_exclusive_access(candidate_path):
                raise CoordinatorError(
                    "OrphanCandidateBusy", systemic=True
                )
            identity = get_identity(candidate_path)
            if not delete_verified(candidate_path, identity):
                raise CoordinatorError(
                    "OrphanCandidateCleanupFailed", systemic=True
                )

    def _ledger_match(self, path: str, identity: FileIdentity) -> bool:
        path_hash = _path_hash(path)
        accepted = {self.contract_hash, *self.accepted_legacy_settings_hashes}
        for record in self._ledger:
            settings_hash = str(record.get("SettingsHash", "")).upper()
            if (
                str(record.get("PathHash", "")).upper() == path_hash
                and settings_hash in accepted
                and str(record.get("VolumeSerialHex", ""))
                == identity.volume_serial_hex
                and str(record.get("FileIdHex", "")) == identity.file_id_hex
                and int(record.get("Length", -1)) == identity.length
                and int(record.get("LastWriteFileTime", -1))
                == identity.last_write_file_time
            ):
                return True
        return False

    def _plan_jobs(self) -> tuple[dict[str, PlannedJob], int]:
        raw: list[PlannedJob] = []
        skipped = 0
        root_key = _normalized_path(self.root)
        with os.scandir(self.root) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name.startswith(".codex-"):
                        continue
                    if Path(entry.name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    source = os.path.abspath(entry.path)
                    if _normalized_path(os.path.dirname(source)) != root_key:
                        skipped += 1
                        continue
                    identity = get_identity(source)
                    if identity.length <= 0:
                        skipped += 1
                        continue
                    destination = str(Path(source).with_suffix(".mkv"))
                    material = (
                        _normalized_path(source)
                        + "|"
                        + identity.volume_serial_hex
                        + "|"
                        + identity.file_id_hex
                    ).encode("utf-8")
                    job_id = hmac.new(
                        self.run_secret, material, hashlib.sha256
                    ).hexdigest()[:32]
                    raw.append(
                        PlannedJob(
                            job_id=job_id,
                            relative_name=validate_top_level_relative_name(
                                entry.name
                            ),
                            source_path=source,
                            destination_path=destination,
                            same_path=(
                                _normalized_path(source)
                                == _normalized_path(destination)
                            ),
                            identity=identity,
                            size_bytes=identity.length,
                        )
                    )
                except (OSError, FileSafetyError, MediaContractError):
                    skipped += 1

        destination_groups: dict[str, list[PlannedJob]] = {}
        for job in raw:
            destination_groups.setdefault(
                _normalized_path(job.destination_path), []
            ).append(job)

        planned: dict[str, PlannedJob] = {}
        for job in raw:
            if len(destination_groups[_normalized_path(job.destination_path)]) > 1:
                skipped += 1
                continue
            if (
                not job.same_path
                and os.path.exists(job.destination_path)
            ):
                skipped += 1
                continue
            if self._ledger_match(job.source_path, job.identity):
                skipped += 1
                continue
            planned[job.job_id] = job
        return planned, skipped

    def helper_seen(self) -> None:
        """Retain the legacy endpoint without granting helper presence.

        Manual helper control is now the authenticated presence contract.  A
        legacy client that does not know the control identity must not hold
        back remote fallback merely by polling this endpoint.
        """

        with self._lock:
            self._require_running()
            self._save_state("Running")

    def helper_state(
        self,
        *,
        worker_id: str,
        control_id: str,
        revision: int,
        pc_in_use: bool,
    ) -> dict[str, str | int | bool]:
        """Accept one authenticated, monotonic helper availability update.

        The state changes only future claim eligibility. Existing helper or
        remote attempts continue heartbeating, validating, and committing.
        """

        with self._lock:
            self._require_running()
            if (
                worker_id != self.helper_worker_id
                or control_id != self.helper_control_id
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or not 1 <= revision <= MAXIMUM_HELPER_CONTROL_REVISION
                or type(pc_in_use) is not bool
            ):
                raise CoordinatorError("HelperControlInvalid")
            if revision < self._helper_control_revision:
                raise CoordinatorError("StaleHelperControl")
            if revision == self._helper_control_revision:
                if (
                    not self._helper_control_known
                    or pc_in_use is not self._helper_paused
                ):
                    raise CoordinatorError("HelperControlConflict")
            else:
                was_paused = self._helper_paused
                self._helper_control_known = True
                self._helper_control_revision = revision
                self._helper_paused = pc_in_use
                if pc_in_use:
                    self._fallback_active = True
                elif was_paused:
                    self._helper_stable_since = float(self.clock())
            self._mark_helper_seen_locked()
            self._save_state("Running")
            return {
                "run_id": self.run_id,
                "revision": self._helper_control_revision,
                "pc_in_use": self._helper_paused,
            }

    def _mark_helper_seen_locked(self) -> None:
        now = float(self.clock())
        if not math.isfinite(now):
            raise CoordinatorError("CoordinatorClockInvalid", systemic=True)
        if not self._helper_online_locked():
            self._helper_stable_since = now
        self._helper_last_seen = now
        self._helper_ever_seen = True
        self._protocol.set_helper_presence(True)

    def _helper_online_locked(self) -> bool:
        if self._helper_last_seen is None:
            return False
        return (
            float(self.clock()) - self._helper_last_seen
            <= self.helper_presence_seconds
        )

    def _role_scheduling_status_locked(self) -> dict[str, Any]:
        """Return worker eligibility without preempting active attempts."""

        now = float(self.clock())
        helper_online = self._helper_online_locked()
        helper_control_ready = (
            self._helper_control_known and not self._helper_paused
        )
        preferred_role = ""
        helper_eligible = helper_online and helper_control_ready
        remote_eligible = True
        countdown: float | None = None

        if self.scheduling_mode == "local-only":
            return {
                "scheduling_mode": self.scheduling_mode,
                "scheduling_state": "local-only",
                "validation_policy": self.validation_policy,
                "preferred_worker_role": WorkerRole.REMOTE.value,
                "helper_eligible": False,
                "remote_eligible": True,
                "fallback_countdown_seconds": None,
            }
        if self.scheduling_mode == "balanced":
            return {
                "scheduling_mode": self.scheduling_mode,
                "scheduling_state": (
                    "helper-paused"
                    if self._helper_control_known and self._helper_paused
                    else "balanced"
                ),
                "validation_policy": self.validation_policy,
                "preferred_worker_role": preferred_role,
                "helper_eligible": (
                    helper_eligible
                ),
                "remote_eligible": remote_eligible,
                "fallback_countdown_seconds": None,
            }

        preferred_role = WorkerRole.HELPER.value
        unresolved = [
            active for active in self._active.values() if not active.committing
        ]
        if unresolved:
            role = unresolved[0].lease.worker_role.value
            return {
                "scheduling_mode": self.scheduling_mode,
                "scheduling_state": f"{role}-active",
                "validation_policy": self.validation_policy,
                "preferred_worker_role": preferred_role,
                "helper_eligible": False,
                "remote_eligible": False,
                "fallback_countdown_seconds": None,
            }

        if self._helper_control_known and self._helper_paused:
            return {
                "scheduling_mode": self.scheduling_mode,
                "scheduling_state": "helper-paused",
                "validation_policy": self.validation_policy,
                "preferred_worker_role": preferred_role,
                "helper_eligible": False,
                "remote_eligible": True,
                "fallback_countdown_seconds": 0.0,
            }

        if helper_online and helper_control_ready:
            helper_eligible = True
            state = "helper-preferred"
            if self._fallback_active:
                stable_since = self._helper_stable_since
                stable_for = (
                    0.0
                    if stable_since is None
                    else max(0.0, now - stable_since)
                )
                countdown = max(
                    0.0,
                    self.helper_recovery_stable_seconds - stable_for,
                )
                helper_eligible = countdown <= 0.0
                state = (
                    "helper-recovered"
                    if helper_eligible
                    else "helper-recovery-wait"
                )
            return {
                "scheduling_mode": self.scheduling_mode,
                "scheduling_state": state,
                "validation_policy": self.validation_policy,
                "preferred_worker_role": preferred_role,
                "helper_eligible": helper_eligible,
                "remote_eligible": False,
                "fallback_countdown_seconds": countdown,
            }

        helper_eligible = False
        if self._helper_ever_seen and self._helper_last_seen is not None:
            eligible_at = (
                self._helper_last_seen
                + self.helper_fallback_after_seconds
            )
            waiting_state = "helper-absence-wait"
        else:
            eligible_at = (
                self._started_monotonic
                + self.helper_startup_grace_seconds
            )
            waiting_state = "helper-startup-wait"
        countdown = max(0.0, eligible_at - now)
        remote_eligible = countdown <= 0.0
        return {
            "scheduling_mode": self.scheduling_mode,
            "scheduling_state": (
                "fallback-ready" if remote_eligible else waiting_state
            ),
            "validation_policy": self.validation_policy,
            "preferred_worker_role": preferred_role,
            "helper_eligible": helper_eligible,
            "remote_eligible": remote_eligible,
            "fallback_countdown_seconds": countdown,
        }

    def _scheduling_status_locked(self) -> dict[str, Any]:
        scheduling = self._role_scheduling_status_locked()
        scheduling.update(
            {
                "helper_control_known": self._helper_control_known,
                "helper_control_revision": self._helper_control_revision,
                "helper_paused": (
                    self._helper_control_known and self._helper_paused
                ),
            }
        )
        return scheduling

    def claim(
        self,
        worker_id: str,
        worker_role: WorkerRole | str,
    ) -> ClaimPayload | None:
        role = WorkerRole(worker_role)
        with self._lock:
            self._require_running()
            self._refresh_presence_locked()
            if role is WorkerRole.HELPER:
                if worker_id != self.helper_worker_id:
                    raise CoordinatorError("HelperIdentityMismatch")
                if (
                    not self._helper_control_known
                    or self._helper_paused
                ):
                    self._save_state("Running")
                    return None
                self._mark_helper_seen_locked()
            scheduling = self._scheduling_status_locked()
            eligible_key = (
                "helper_eligible"
                if role is WorkerRole.HELPER
                else "remote_eligible"
            )
            if not scheduling[eligible_key]:
                self._save_state("Running")
                return None
            lease = self._protocol.claim(worker_id, role)
            if lease is None:
                self._save_state("Running")
                return None
            if self.scheduling_mode == "prefer-helper":
                if role is WorkerRole.REMOTE:
                    self._fallback_active = True
                else:
                    self._fallback_active = False
            job = self._jobs[lease.job_id]
            reservation = max(
                MINIMUM_JOB_RESERVATION, job.size_bytes * 2
            )
            available = shutil.disk_usage(self.work_root).free
            if (
                available
                < self.reserve_bytes + self._reserved_bytes + reservation
            ):
                self._fail_leased_attempt_locked(lease)
                self._systemic_failure = "LowDiskSpace"
                self._save_state("Stopped")
                raise CoordinatorError("LowDiskSpace", systemic=True)
            try:
                source_pin = open_read_pin(job.source_path)
                if source_pin.identity != job.identity:
                    raise CoordinatorError("SourceChangedSincePlan")
            except Exception as exc:
                self._fail_leased_attempt_locked(lease)
                self._record_terminal_failure_locked()
                self._save_state(
                    "Stopped" if self._systemic_failure else "Running"
                )
                if isinstance(exc, CoordinatorError):
                    raise
                raise CoordinatorError("SourcePinFailed") from exc
            candidate_name = (
                f"candidate-{job.job_id}-{lease.attempt_id}.ready.mkv"
            )
            source_alias_name = validate_top_level_relative_name(
                f"{SOURCE_ALIAS_PREFIX}{self.run_id}-"
                f"{lease.attempt_id}.media"
            )
            source_alias_path = os.path.join(
                self.staging_root, source_alias_name
            )
            source_alias_pin: PinnedFile | None = None
            try:
                if os.path.exists(source_alias_path):
                    raise CoordinatorError(
                        "SourceAliasCollision", systemic=True
                    )
                os.link(job.source_path, source_alias_path)
                source_alias_pin = open_read_pin(source_alias_path)
                if source_alias_pin.identity != job.identity:
                    raise CoordinatorError(
                        "SourceAliasIdentityMismatch", systemic=True
                    )
            except Exception as exc:
                if source_alias_pin is not None:
                    source_alias_pin.close()
                source_pin.close()
                if os.path.exists(source_alias_path):
                    try:
                        alias_identity = get_identity(source_alias_path)
                        delete_verified(source_alias_path, alias_identity)
                    except (OSError, FileSafetyError):
                        pass
                self._fail_leased_attempt_locked(lease)
                self._systemic_failure = "SourceAliasCreateFailed"
                self._save_state("Stopped")
                if isinstance(exc, CoordinatorError):
                    raise
                raise CoordinatorError(
                    "SourceAliasCreateFailed", systemic=True
                ) from exc
            active = ActiveAttempt(
                lease=lease,
                job=job,
                source_pin=source_pin,
                source_alias_pin=source_alias_pin,
                reservation_bytes=reservation,
                candidate_name=candidate_name,
                source_alias_name=source_alias_name,
                progress_updated=float(self.clock()),
                claimed_monotonic=float(self.clock()),
                claimed_utc=time.time(),
            )
            self._active[lease.attempt_id] = active
            self._reserved_bytes += reservation
            self._recent_events.append(
                {
                    "timestamp_utc": time.time(),
                    "worker_role": role.value,
                    "event": "Claimed",
                    "filename": job.relative_name,
                }
            )
            self._save_state("Running")
            return ClaimPayload(
                schema_version=SCHEMA_VERSION,
                run_id=self.run_id,
                contract_hash=self.contract_hash,
                job_id=job.job_id,
                attempt_id=lease.attempt_id,
                fencing_epoch=lease.fencing_epoch,
                worker_id=worker_id,
                worker_role=role.value,
                lease_deadline=lease.lease_deadline,
                source_alias=source_alias_name,
                candidate_name=candidate_name,
                source_identity=job.identity.to_dict(),
                run_marker_identity=self._run_marker_pin.identity.to_dict(),
                source_size_bytes=job.size_bytes,
                maximum_output_bytes=reservation,
                validation_policy=self.validation_policy,
            )

    def heartbeat(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
        progress_seconds: float = 0.0,
        frame_count: int = 0,
        phase: str = "Claimed",
        media_duration_seconds: float = 0.0,
        encode_elapsed_seconds: float = 0.0,
        transfer_bytes: int = 0,
        transfer_total_bytes: int = 0,
        transfer_elapsed_seconds: float = 0.0,
    ) -> ClaimPayload:
        with self._lock:
            self._require_running()
            active = self._active.get(attempt_id)
            if active is None:
                raise CoordinatorError("StaleAttempt")
            progress_value = float(progress_seconds)
            frame_value = int(frame_count)
            media_duration_value = float(media_duration_seconds)
            encode_elapsed_value = float(encode_elapsed_seconds)
            transfer_value = int(transfer_bytes)
            transfer_total_value = int(transfer_total_bytes)
            transfer_elapsed_value = float(transfer_elapsed_seconds)
            if (
                not math.isfinite(progress_value)
                or progress_value < 0
                or frame_value < 0
                or phase not in WORK_PHASES
                or not math.isfinite(media_duration_value)
                or media_duration_value < 0
                or not math.isfinite(encode_elapsed_value)
                or encode_elapsed_value < 0
                or transfer_value < 0
                or transfer_total_value < 0
                or transfer_value > transfer_total_value
                or not math.isfinite(transfer_elapsed_value)
                or transfer_elapsed_value < 0
            ):
                raise CoordinatorError("ProgressInvalid")
            try:
                lease = self._protocol.heartbeat(
                    worker_id=worker_id,
                    attempt_id=attempt_id,
                    fencing_epoch=fencing_epoch,
                )
            except (ProtocolError, ValueError) as exc:
                raise CoordinatorError("StaleAttempt") from exc
            active.lease = lease
            if active.lease.worker_role is WorkerRole.HELPER:
                self._mark_helper_seen_locked()
            active.progress_seconds = max(
                active.progress_seconds, progress_value
            )
            active.progress_frame_count = max(
                active.progress_frame_count, frame_value
            )
            active.progress_updated = float(self.clock())
            active.phase = phase
            active.media_duration_seconds = max(
                active.media_duration_seconds, media_duration_value
            )
            active.encode_elapsed_seconds = max(
                active.encode_elapsed_seconds, encode_elapsed_value
            )
            active.transfer_bytes = max(
                active.transfer_bytes, transfer_value
            )
            active.transfer_total_bytes = max(
                active.transfer_total_bytes, transfer_total_value
            )
            active.transfer_elapsed_seconds = max(
                active.transfer_elapsed_seconds, transfer_elapsed_value
            )
            self._save_state("Running")
            return self._claim_payload_locked(active)

    def _claim_payload_locked(self, active: ActiveAttempt) -> ClaimPayload:
        lease = active.lease
        job = active.job
        return ClaimPayload(
            schema_version=SCHEMA_VERSION,
            run_id=self.run_id,
            contract_hash=self.contract_hash,
            job_id=job.job_id,
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
            worker_id=lease.worker_id,
            worker_role=lease.worker_role.value,
            lease_deadline=lease.lease_deadline,
            source_alias=active.source_alias_name,
            candidate_name=active.candidate_name,
            source_identity=job.identity.to_dict(),
            run_marker_identity=self._run_marker_pin.identity.to_dict(),
            source_size_bytes=job.size_bytes,
            maximum_output_bytes=active.reservation_bytes,
            validation_policy=self.validation_policy,
        )

    def abandon(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
    ) -> bool:
        with self._lock:
            self._require_running()
            active = self._active.get(attempt_id)
            if (
                active is None
                or active.lease.worker_id != worker_id
                or active.lease.fencing_epoch != fencing_epoch
            ):
                raise CoordinatorError("StaleAttempt")
            self._protocol.disconnect_worker(worker_id)
            released = self._release_suspect_locked(
                active, terminal=False
            )
            self._save_state("Running")
            return released

    def report_failure(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
        failure_category: str,
    ) -> bool:
        with self._lock:
            self._require_running()
            if (
                not isinstance(failure_category, str)
                or not failure_category
                or len(failure_category) > 128
                or any(ord(character) < 0x20 for character in failure_category)
            ):
                raise CoordinatorError("FailureCategoryInvalid")
            active = self._active.get(attempt_id)
            if (
                active is None
                or active.lease.worker_id != worker_id
                or active.lease.fencing_epoch != fencing_epoch
            ):
                raise CoordinatorError("StaleAttempt")
            active.terminal_failure_pending = True
            self._protocol.disconnect_worker(worker_id)
            released = self._release_suspect_locked(
                active, terminal=True
            )
            self._save_state(
                "Stopped" if self._systemic_failure else "Running"
            )
            return released

    def submit(self, payload: SubmitPayload) -> CommitOutcome:
        with self._lock:
            active, candidate_path = self._prepare_submit_locked(payload)
        return self._finish_prepared_submit(
            active, payload, candidate_path
        )

    def _prepare_submit_locked(
        self, payload: SubmitPayload
    ) -> tuple[ActiveAttempt, str]:
        self._require_running()
        self._refresh_presence_locked()
        if (
            payload.run_id != self.run_id
            or payload.contract_hash != self.contract_hash
        ):
            raise CoordinatorError("BindingMismatch")
        if (
            self.validation_policy == "producer-full"
            and payload.validation_evidence is None
        ):
            raise CoordinatorError("ValidationEvidenceMissing")
        active = self._active.get(payload.attempt_id)
        if (
            active is None
            or active.lease.worker_id != payload.worker_id
            or active.lease.fencing_epoch != payload.fencing_epoch
        ):
            raise CoordinatorError("StaleAttempt")
        candidate_path = os.path.join(
            self.staging_root, active.candidate_name
        )
        if not _wait_for_submit_candidate_ready(candidate_path):
            raise CoordinatorError("CandidateNotReady")
        try:
            self._protocol.begin_commit(
                worker_id=payload.worker_id,
                attempt_id=payload.attempt_id,
                fencing_epoch=payload.fencing_epoch,
            )
        except (ProtocolError, ValueError) as exc:
            raise CoordinatorError("StaleAttempt") from exc
        active.committing = True
        active.phase = (
            "CoordinatorIntegrity"
            if self.validation_policy == "producer-full"
            else "RemoteValidation"
        )
        if active.lease.worker_role is WorkerRole.HELPER:
            self._mark_helper_seen_locked()
        self._save_state("Committing")
        return active, candidate_path

    def _finish_prepared_submit(
        self,
        active: ActiveAttempt,
        payload: SubmitPayload,
        candidate_path: str,
    ) -> CommitOutcome:
        try:
            with self._commit_lock:
                outcome = self._commit_candidate(
                    active, payload, candidate_path
                )
        except CoordinatorError as exc:
            with self._lock:
                if exc.systemic:
                    self._systemic_failure = exc.category
                    self._save_state("Stopped")
                else:
                    self._protocol.mark_failed(
                        attempt_id=payload.attempt_id,
                        fencing_epoch=payload.fencing_epoch,
                    )
                    self._terminal_states[
                        active.job.job_id
                    ] = "failed"
                    self._recent_events.append(
                        {
                            "timestamp_utc": time.time(),
                            "worker_role": active.lease.worker_role.value,
                            "event": "Failed",
                            "filename": active.job.relative_name,
                        }
                    )
                    self._release_active_locked(active)
                    self._record_terminal_failure_locked()
                    self._save_state(
                        "Stopped"
                        if self._systemic_failure
                        else "Running"
                    )
            raise

        with self._lock:
            self._protocol.mark_completed(
                attempt_id=payload.attempt_id,
                fencing_epoch=payload.fencing_epoch,
            )
            self._terminal_states[active.job.job_id] = "completed"
            self._recent_events.append(
                {
                    "timestamp_utc": time.time(),
                    "worker_role": active.lease.worker_role.value,
                    "event": "Completed",
                    "filename": active.job.relative_name,
                }
            )
            self._release_active_locked(active)
            if not self._systemic_failure:
                self._consecutive_failures = 0
            self._save_state(
                "Stopped" if self._systemic_failure else "Running"
            )
        return outcome

    def submit_async(self, payload: SubmitPayload) -> CommitOutcome:
        with self._lock:
            existing = self._async_submissions.get(payload.attempt_id)
            if existing is not None:
                if existing.payload != payload:
                    raise CoordinatorError("BindingMismatch")
                return CommitOutcome("Accepted", "CommitAccepted")
            active, candidate_path = self._prepare_submit_locked(payload)
            record = AsyncSubmission(payload=payload)
            self._async_submissions[payload.attempt_id] = record
            thread = threading.Thread(
                target=self._run_async_commit,
                args=(record, active, candidate_path),
                name="lan-coordinator-commit",
                daemon=True,
            )
            self._async_threads.add(thread)
            thread.start()
        return CommitOutcome("Accepted", "CommitAccepted")

    def _run_async_commit(
        self,
        record: AsyncSubmission,
        active: ActiveAttempt,
        candidate_path: str,
    ) -> None:
        try:
            outcome = self._finish_prepared_submit(
                active, record.payload, candidate_path
            )
        except CoordinatorError as exc:
            outcome = CommitOutcome("Failure", exc.category)
        except Exception:
            outcome = CommitOutcome("Failure", "CommitFailed")
        with self._lock:
            record.outcome = outcome
            self._async_threads.discard(threading.current_thread())

    def poll_submission(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
    ) -> CommitOutcome:
        with self._lock:
            record = self._async_submissions.get(attempt_id)
            if (
                record is None
                or record.payload.worker_id != worker_id
                or record.payload.fencing_epoch != fencing_epoch
            ):
                raise CoordinatorError("StaleAttempt")
            return record.outcome or CommitOutcome(
                "Pending", "CommitInProgress"
            )

    def wait_for_async_commits(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._async_threads)
            if not threads:
                return True
            for thread in threads:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                with self._lock:
                    return not self._async_threads

    def _verify_producer_validation_evidence(
        self,
        active: ActiveAttempt,
        payload: SubmitPayload,
        candidate_identity: FileIdentity,
        candidate_hash: str,
        output_info: MediaInfo,
    ) -> None:
        evidence = payload.validation_evidence
        if evidence is None:
            raise CoordinatorError("ValidationEvidenceMissing")
        expected_bindings = (
            (evidence.run_id, self.run_id),
            (evidence.job_id, active.job.job_id),
            (evidence.worker_id, active.lease.worker_id),
            (evidence.worker_role, active.lease.worker_role.value),
            (evidence.attempt_id, active.lease.attempt_id),
            (evidence.contract_hash, self.contract_hash),
            (evidence.producer_build_sha256, self.runtime_build_hash),
            (evidence.ffmpeg_sha256, self.ffmpeg_hash),
            (evidence.ffprobe_sha256, self.ffprobe_hash),
            (evidence.candidate_sha256, candidate_hash),
            (evidence.candidate_sha256, payload.candidate_sha256),
        )
        if any(
            not hmac.compare_digest(str(actual), str(expected))
            for actual, expected in expected_bindings
        ):
            raise CoordinatorError("ValidationEvidenceMismatch")
        if (
            evidence.fencing_epoch != active.lease.fencing_epoch
            or evidence.fencing_epoch != payload.fencing_epoch
            or evidence.candidate_bytes != candidate_identity.length
            or evidence.encoded_frame_count
            != payload.encoded_frame_count
            or evidence.full_decode.video.stream_index
            != output_info.video_index
            or tuple(
                item.stream_index for item in evidence.full_decode.audio
            )
            != tuple(item.index for item in output_info.audio_streams)
        ):
            raise CoordinatorError("ValidationEvidenceMismatch")
        duration_tolerance = max(
            0.5, min(2.0, output_info.duration * 0.005)
        )
        if (
            abs(
                evidence.full_decode.video.out_time_seconds
                - output_info.duration
            )
            > duration_tolerance
        ):
            raise CoordinatorError("ValidationEvidenceMismatch")

    def _commit_candidate(
        self,
        active: ActiveAttempt,
        payload: SubmitPayload,
        candidate_path: str,
    ) -> CommitOutcome:
        job = active.job
        journal_started = False
        candidate_pin: PinnedFile | None = None
        output_pin: PinnedFile | None = None
        transaction_id = uuid.uuid4().hex
        backup_path = os.path.join(
            self.root, f".codex-original-backup-{transaction_id}.bak"
        )
        published_path = ""
        try:
            if get_pinned_identity(active.source_pin) != job.identity:
                raise CoordinatorError("SourceChanged", systemic=True)
            source_info = probe_media(self.ffprobe, job.source_path)
            source_audio_durations = (
                tuple(
                    item.duration
                    if item.duration > 0
                    else source_info.duration
                    for item in source_info.audio_streams
                )
                if self.validation_policy == "producer-full"
                else decode_audio_durations(
                    self.ffmpeg, job.source_path, source_info
                )
            )

            candidate_identity = get_identity(candidate_path)
            if (
                candidate_identity.length <= 0
                or candidate_identity.length > active.reservation_bytes
            ):
                raise CoordinatorError("CandidateSizeInvalid")
            if (
                active.lease.worker_role is WorkerRole.HELPER
                and candidate_identity.length
                > maximum_beneficial_output_bytes(
                    job.size_bytes,
                    active.reservation_bytes,
                )
            ):
                raise CoordinatorError("CandidateSizeNotBeneficial")
            candidate_identity = flush_verified(
                candidate_path, candidate_identity
            )
            if (
                candidate_identity.volume_serial_hex
                != job.identity.volume_serial_hex
            ):
                raise CoordinatorError("CandidateVolumeMismatch")
            candidate_pin = open_read_pin(candidate_path)
            if candidate_pin.identity != candidate_identity:
                raise CoordinatorError(
                    "CandidateIdentityChanged", systemic=True
                )
            candidate_hash = sha256_file(candidate_path)
            if not hmac.compare_digest(
                candidate_hash.upper(), payload.candidate_sha256.upper()
            ):
                raise CoordinatorError("CandidateHashMismatch")
            if payload.encoded_frame_count <= 0:
                raise CoordinatorError("EncodedFrameCountUnavailable")
            output_info: MediaInfo | None = None
            if (
                self.validation_policy == "producer-full"
                or payload.validation_evidence is not None
            ):
                output_info = validate_candidate_metadata(
                    self.ffprobe,
                    candidate_path,
                    source_info,
                    source_audio_durations,
                    contract=self.contract,
                )
                if output_info is None:
                    raise CoordinatorError("CandidateValidationFailed")
            if self.validation_policy == "producer-full":
                assert output_info is not None
                self._verify_producer_validation_evidence(
                    active,
                    payload,
                    candidate_identity,
                    candidate_hash,
                    output_info,
                )
            elif not validate_candidate(
                self.ffmpeg,
                self.ffprobe,
                candidate_path,
                source_info,
                source_audio_durations,
                payload.encoded_frame_count,
                decode_to_end=True,
                contract=self.contract,
            ):
                raise CoordinatorError("CandidateValidationFailed")
            elif payload.validation_evidence is not None:
                assert output_info is not None
                self._verify_producer_validation_evidence(
                    active,
                    payload,
                    candidate_identity,
                    candidate_hash,
                    output_info,
                )
            if (
                not job.same_path
                and os.path.exists(job.destination_path)
            ):
                raise CoordinatorError("DestinationAppeared")
            if job.same_path and os.path.exists(backup_path):
                raise CoordinatorError("BackupCollision")
            candidate_pin.close()
            candidate_pin = None
            if not _wait_for_exclusive_access(candidate_path):
                raise CoordinatorError("CandidateBusy")

            transaction = {
                "SchemaVersion": SCHEMA_VERSION,
                "TransactionId": transaction_id,
                "RunId": self.run_id,
                "RunnerBindingHash": self.runner_binding_hash,
                "ContractHash": self.contract_hash,
                "JobId": job.job_id,
                "AttemptId": payload.attempt_id,
                "FencingEpoch": payload.fencing_epoch,
                "Phase": "TempValidated",
                "SourceProtected": protect_text(job.source_path),
                "DestinationProtected": protect_text(job.destination_path),
                "CandidateProtected": protect_text(candidate_path),
                "BackupProtected": protect_text(backup_path),
                "SamePath": job.same_path,
                "SourceIdentity": job.identity.to_dict(),
                "CandidateIdentity": candidate_identity.to_dict(),
                "CandidateSha256": candidate_hash,
                "EncodedFrameCount": payload.encoded_frame_count,
            }
            _create_json(self.journal_path, transaction)
            journal_started = True
            with self._lock:
                active.phase = "Publishing"
                active.progress_updated = float(self.clock())

            if job.same_path:
                self._journal_phase(transaction, "OriginalRenameIntent")
                self._prepare_source_mutation(active)
                if not _rename_verified_with_retry(
                    job.source_path, backup_path, job.identity
                ):
                    raise CoordinatorError(
                        "OriginalRenameFailed", systemic=True
                    )
                self._journal_phase(transaction, "OriginalRenamed")
                self._journal_phase(transaction, "PublishIntent")
                if not _rename_verified_with_retry(
                    candidate_path,
                    job.source_path,
                    candidate_identity,
                ):
                    raise CoordinatorError("PublishFailed", systemic=True)
                published_path = job.source_path
            else:
                self._journal_phase(transaction, "PublishIntent")
                if not _rename_verified_with_retry(
                    candidate_path,
                    job.destination_path,
                    candidate_identity,
                ):
                    raise CoordinatorError("PublishFailed", systemic=True)
                published_path = job.destination_path
            self._journal_phase(transaction, "Published")

            with self._lock:
                active.phase = "PostPublishIntegrity"
                active.progress_updated = float(self.clock())

            output_identity = get_identity(published_path)
            output_identity = flush_verified(
                published_path, output_identity
            )
            output_pin = open_read_pin(published_path)
            if output_pin.identity != output_identity:
                raise CoordinatorError(
                    "PublishedIdentityChanged", systemic=True
                )
            if sha256_file(published_path) != candidate_hash:
                raise CoordinatorError(
                    "PublishedHashMismatch", systemic=True
                )
            published_valid = (
                validate_candidate_metadata(
                    self.ffprobe,
                    published_path,
                    source_info,
                    source_audio_durations,
                    contract=self.contract,
                )
                is not None
                if self.validation_policy == "producer-full"
                else validate_candidate(
                    self.ffmpeg,
                    self.ffprobe,
                    published_path,
                    source_info,
                    source_audio_durations,
                    payload.encoded_frame_count,
                    decode_to_end=True,
                    contract=self.contract,
                )
            )
            if not published_valid:
                raise CoordinatorError(
                    "PublishedValidationFailed", systemic=True
                )
            transaction["OutputIdentity"] = output_identity.to_dict()
            self._journal_phase(transaction, "FinalValidated")

            if job.same_path:
                self._journal_phase(transaction, "BackupDeleteIntent")
                if not delete_verified(backup_path, job.identity):
                    raise CoordinatorError(
                        "BackupCleanupFailed", systemic=True
                    )
                self._journal_phase(transaction, "BackupDeleted")
            else:
                self._journal_phase(transaction, "DeleteIntent")
                self._prepare_source_mutation(active)
                if not delete_verified(job.source_path, job.identity):
                    raise CoordinatorError(
                        "SourceDeletionFailed", systemic=True
                    )
                self._journal_phase(transaction, "SourceDeleted")

            self._add_ledger_record(published_path, output_identity)
            os.remove(self.journal_path)
            if os.path.exists(self.journal_path):
                raise CoordinatorError(
                    "JournalCleanupFailed", systemic=True
                )
            journal_started = False
            return CommitOutcome(kind="Success", category="Committed")
        except CoordinatorError:
            if journal_started or os.path.exists(self.journal_path):
                raise CoordinatorError(
                    "TransactionRecoveryRequired", systemic=True
                )
            self._remove_candidate_if_exact(candidate_path)
            raise
        except Exception as exc:
            if journal_started or os.path.exists(self.journal_path):
                raise CoordinatorError(
                    "TransactionRecoveryRequired", systemic=True
                ) from exc
            self._remove_candidate_if_exact(candidate_path)
            raise CoordinatorError("CommitFailed") from exc
        finally:
            if candidate_pin is not None:
                candidate_pin.close()
            if output_pin is not None:
                output_pin.close()

    def _journal_phase(
        self, transaction: dict[str, Any], next_phase: str
    ) -> None:
        current = _read_json(self.journal_path, dict)
        if (
            current.get("TransactionId") != transaction["TransactionId"]
            or current.get("RunnerBindingHash")
            != transaction["RunnerBindingHash"]
            or current.get("Phase") != transaction["Phase"]
        ):
            raise CoordinatorError("JournalChanged", systemic=True)
        transaction["Phase"] = next_phase
        _atomic_write_json(self.journal_path, transaction)

    def _add_ledger_record(
        self, published_path: str, identity: FileIdentity
    ) -> None:
        path_hash = _path_hash(published_path)
        remaining = [
            record
            for record in self._ledger
            if str(record.get("PathHash", "")).upper() != path_hash
        ]
        remaining.append(
            {
                "PathHash": path_hash,
                "SettingsHash": self.contract_hash,
                "VolumeSerialHex": identity.volume_serial_hex,
                "FileIdHex": identity.file_id_hex,
                "Length": identity.length,
                "CreationFileTime": identity.creation_file_time,
                "LastWriteFileTime": identity.last_write_file_time,
                "CompletedUtc": time.time(),
            }
        )
        _atomic_write_json(self.ledger_path, remaining)
        self._ledger = remaining

    def _remove_candidate_if_exact(self, candidate_path: str) -> None:
        try:
            if not os.path.exists(candidate_path):
                return
            identity = get_identity(candidate_path)
            if prove_exclusive_access(candidate_path):
                delete_verified(candidate_path, identity)
        except (OSError, FileSafetyError):
            pass

    def _fail_leased_attempt_locked(self, lease: Lease) -> None:
        self._protocol.disconnect_worker(lease.worker_id)
        self._protocol.resource_release_proven(
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )
        self._protocol.mark_failed(
            attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )
        self._terminal_states[lease.job_id] = "failed"

    def _close_attempt_source_pins(self, active: ActiveAttempt) -> None:
        active.source_alias_pin.close()
        active.source_pin.close()

    def _delete_source_alias_locked(
        self,
        active: ActiveAttempt,
        *,
        require_source_exclusive: bool,
        allow_proven_missing: bool = False,
    ) -> bool:
        self._close_attempt_source_pins(active)
        if allow_proven_missing and active.source_alias_release_proven:
            return True
        alias_path = os.path.join(
            self.staging_root, active.source_alias_name
        )
        if require_source_exclusive and not prove_exclusive_access(
            active.job.source_path
        ):
            return False
        if not os.path.exists(alias_path):
            return False
        try:
            if get_identity(alias_path) != active.job.identity:
                return False
            if not prove_exclusive_access(alias_path):
                return False
            if not delete_verified(alias_path, active.job.identity):
                return False
        except (OSError, FileSafetyError):
            return False
        removed = not os.path.exists(alias_path)
        if removed and allow_proven_missing:
            active.source_alias_release_proven = True
        return removed

    def _prepare_source_mutation(self, active: ActiveAttempt) -> None:
        if (
            get_pinned_identity(active.source_pin) != active.job.identity
            or get_pinned_identity(active.source_alias_pin)
            != active.job.identity
        ):
            raise CoordinatorError(
                "SourceChangedBeforeDeletion", systemic=True
            )
        if not self._delete_source_alias_locked(
            active,
            require_source_exclusive=True,
        ):
            raise CoordinatorError(
                "SourceAliasCleanupFailed", systemic=True
            )

    def _release_active_locked(self, active: ActiveAttempt) -> None:
        self._close_attempt_source_pins(active)
        self._reserved_bytes = max(
            0, self._reserved_bytes - active.reservation_bytes
        )
        self._active.pop(active.lease.attempt_id, None)

    def _record_terminal_failure_locked(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.consecutive_failure_limit:
            self._systemic_failure = "ConsecutiveFailureLimit"

    def _release_suspect_locked(
        self, active: ActiveAttempt, *, terminal: bool
    ) -> bool:
        if not self._delete_source_alias_locked(
            active,
            require_source_exclusive=True,
            allow_proven_missing=True,
        ):
            return False
        candidate_path = os.path.join(
            self.staging_root, active.candidate_name
        )
        upload_path = candidate_path + ".upload"
        for artifact in (candidate_path, upload_path):
            if os.path.exists(artifact):
                if not prove_exclusive_access(artifact):
                    return False
                self._remove_candidate_if_exact(artifact)
                if os.path.exists(artifact):
                    return False
        self._protocol.resource_release_proven(
            attempt_id=active.lease.attempt_id,
            fencing_epoch=active.lease.fencing_epoch,
        )
        if terminal:
            self._protocol.mark_failed(
                attempt_id=active.lease.attempt_id,
                fencing_epoch=active.lease.fencing_epoch,
            )
            self._record_terminal_failure_locked()
            self._terminal_states[active.job.job_id] = "failed"
            self._recent_events.append(
                {
                    "timestamp_utc": time.time(),
                    "worker_role": active.lease.worker_role.value,
                    "event": "Failed",
                    "filename": active.job.relative_name,
                }
            )
        else:
            self._protocol.requeue(
                attempt_id=active.lease.attempt_id,
                fencing_epoch=active.lease.fencing_epoch,
            )
        self._release_active_locked(active)
        return True

    def reap(self) -> int:
        with self._lock:
            self._require_running()
            self._refresh_presence_locked()
            self._protocol.expire_leases()
            released = 0
            for active in list(self._active.values()):
                if active.committing:
                    continue
                if float(self.clock()) < active.lease.lease_deadline:
                    continue
                try:
                    if self._release_suspect_locked(
                        active,
                        terminal=active.terminal_failure_pending,
                    ):
                        released += 1
                except (ProtocolError, FileSafetyError, OSError):
                    continue
            self._save_state(
                "Stopped" if self._systemic_failure else "Running"
            )
            return released

    def _refresh_presence_locked(self) -> None:
        if (
            self._protocol.helper_online
            and not self._helper_online_locked()
        ):
            self._protocol.set_helper_presence(False)
            self._helper_stable_since = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_presence_locked()
            aggregate: AggregateSnapshot = self._protocol.snapshot()
            scheduling = self._scheduling_status_locked()
            return {
                "SchemaVersion": SCHEMA_VERSION,
                "RunId": self.run_id,
                "ContractHash": self.contract_hash,
                "Status": (
                    "Stopped" if self._systemic_failure else "Running"
                ),
                "FailureCategory": self._systemic_failure,
                "InitialSkipped": self._initial_skipped,
                "ReservedBytes": self._reserved_bytes,
                "ConsecutiveFailures": self._consecutive_failures,
                "SchedulingMode": scheduling["scheduling_mode"],
                "SchedulingState": scheduling["scheduling_state"],
                "ValidationPolicy": scheduling["validation_policy"],
                "PreferredWorkerRole": scheduling[
                    "preferred_worker_role"
                ],
                "HelperEligible": scheduling["helper_eligible"],
                "RemoteEligible": scheduling["remote_eligible"],
                "FallbackCountdownSeconds": scheduling[
                    "fallback_countdown_seconds"
                ],
                "HelperControlKnown": scheduling["helper_control_known"],
                "HelperControlRevision": scheduling[
                    "helper_control_revision"
                ],
                "HelperPaused": scheduling["helper_paused"],
                **{
                    key: value.value if hasattr(value, "value") else value
                    for key, value in asdict(aggregate).items()
                },
            }

    def dashboard_snapshot(self) -> dict[str, Any]:
        """Return detailed local-only monitoring data.

        This method is intentionally not routed by ``CoordinatorDispatcher``.
        It may contain media basenames and is only for the storage host's
        loopback dashboard.
        """
        with self._lock:
            self._refresh_presence_locked()
            now_mono = float(self.clock())
            now_utc = time.time()
            aggregate = self._protocol.snapshot()
            scheduling = self._scheduling_status_locked()
            active_by_job = {
                active.job.job_id: active
                for active in self._active.values()
            }
            total_bytes = sum(job.size_bytes for job in self._jobs.values())
            completed_bytes = sum(
                self._jobs[job_id].size_bytes
                for job_id, state in self._terminal_states.items()
                if state == "completed" and job_id in self._jobs
            )
            remaining_bytes = sum(
                job.size_bytes
                for job_id, job in self._jobs.items()
                if self._terminal_states.get(job_id) is None
            )
            elapsed = max(0.0, now_mono - self._started_monotonic)
            eta = None
            if completed_bytes > 0 and elapsed > 0:
                eta = remaining_bytes / (completed_bytes / elapsed)

            workers: list[dict[str, Any]] = []
            for role in (WorkerRole.REMOTE, WorkerRole.HELPER):
                matching = [
                    active
                    for active in self._active.values()
                    if active.lease.worker_role is role
                ]
                if not matching:
                    workers.append(
                        {
                            "worker_id": (
                                "remote-qsv"
                                if role is WorkerRole.REMOTE
                                else "helper-nvenc"
                            ),
                            "role": role.value,
                            "label": (
                                "This PC · Intel QSV"
                                if role is WorkerRole.REMOTE
                                else "Helper PC · NVIDIA NVENC"
                            ),
                            "online": (
                                not bool(self._systemic_failure)
                                if role is WorkerRole.REMOTE
                                else aggregate.helper_online
                            ),
                            "phase": (
                                "PcInUsePaused"
                                if role is WorkerRole.HELPER
                                and scheduling["helper_paused"]
                                else "Idle"
                            ),
                            "current_filename": "",
                            "source_size_bytes": 0,
                            "media_seconds": 0.0,
                            "duration_seconds": 0.0,
                            "encode_elapsed_seconds": 0.0,
                            "encode_speed_ratio": 0.0,
                            "encode_eta_seconds": None,
                            "transfer_bytes": 0,
                            "transfer_total_bytes": 0,
                            "transfer_elapsed_seconds": 0.0,
                            "transfer_bytes_per_second": 0.0,
                            "transfer_eta_seconds": None,
                            "updated_utc": now_utc,
                        }
                    )
                    continue
                for active in matching:
                    speed = (
                        active.progress_seconds
                        / active.encode_elapsed_seconds
                        if active.encode_elapsed_seconds > 0
                        else 0.0
                    )
                    encode_eta = None
                    if (
                        speed > 0
                        and active.media_duration_seconds
                        > active.progress_seconds
                    ):
                        encode_eta = (
                            active.media_duration_seconds
                            - active.progress_seconds
                        ) / speed
                    transfer_speed = (
                        active.transfer_bytes
                        / active.transfer_elapsed_seconds
                        if active.transfer_elapsed_seconds > 0
                        else 0.0
                    )
                    transfer_eta = None
                    if (
                        transfer_speed > 0
                        and active.transfer_total_bytes
                        > active.transfer_bytes
                    ):
                        transfer_eta = (
                            active.transfer_total_bytes
                            - active.transfer_bytes
                        ) / transfer_speed
                    workers.append(
                        {
                            "worker_id": active.lease.worker_id,
                            "role": role.value,
                            "label": (
                                "This PC · Intel QSV"
                                if role is WorkerRole.REMOTE
                                else "Helper PC · NVIDIA NVENC"
                            ),
                            "online": True,
                            "phase": active.phase,
                            "current_filename": active.job.relative_name,
                            "source_size_bytes": active.job.size_bytes,
                            "media_seconds": active.progress_seconds,
                            "duration_seconds": (
                                active.media_duration_seconds
                            ),
                            "encode_elapsed_seconds": (
                                active.encode_elapsed_seconds
                            ),
                            "encode_speed_ratio": speed,
                            "encode_eta_seconds": encode_eta,
                            "transfer_bytes": active.transfer_bytes,
                            "transfer_total_bytes": (
                                active.transfer_total_bytes
                            ),
                            "transfer_elapsed_seconds": (
                                active.transfer_elapsed_seconds
                            ),
                            "transfer_bytes_per_second": transfer_speed,
                            "transfer_eta_seconds": transfer_eta,
                            "updated_utc": now_utc
                            - max(
                                0.0,
                                now_mono - active.progress_updated,
                            ),
                        }
                    )

            queue: list[dict[str, Any]] = []
            for job_id, job in self._jobs.items():
                active = active_by_job.get(job_id)
                state = self._terminal_states.get(job_id, "pending")
                worker_role = ""
                progress_percent = 0.0
                if active is not None:
                    state = {
                        "Claimed": "claimed",
                        "SourceRead": "reading",
                        "Converting": "encoding",
                        "LocalValidation": "validating",
                        "Uploading": "transferring",
                        "UploadVerification": "verifying-upload",
                        "RemoteValidation": "validating",
                        "ProducerFullValidation": "validating",
                        "CoordinatorIntegrity": "verifying-integrity",
                        "PostPublishIntegrity": "verifying-integrity",
                        "Publishing": "committing",
                    }.get(active.phase, "active")
                    worker_role = active.lease.worker_role.value
                    if active.media_duration_seconds > 0:
                        progress_percent = min(
                            100.0,
                            100.0
                            * active.progress_seconds
                            / active.media_duration_seconds,
                        )
                elif state == "completed":
                    progress_percent = 100.0
                queue.append(
                    {
                        "filename": job.relative_name,
                        "size_bytes": job.size_bytes,
                        "state": state,
                        "worker_role": worker_role,
                        "progress_percent": progress_percent,
                    }
                )

            active_count = (
                aggregate.leased
                + aggregate.suspect
                + aggregate.committing
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_utc": now_utc,
                "run": {
                    "run_id": self.run_id,
                    "status": (
                        "Stopped"
                        if self._systemic_failure
                        else (
                            "Complete"
                            if aggregate.pending == 0
                            and active_count == 0
                            else "Running"
                        )
                    ),
                    "failure_category": self._systemic_failure,
                    "contract_hash": self.contract_hash,
                    **scheduling,
                    "started_utc": self._started_utc,
                    "elapsed_seconds": elapsed,
                    "eta_seconds": eta,
                },
                "totals": {
                    "total_jobs": aggregate.total_jobs,
                    "pending": aggregate.pending,
                    "active": active_count,
                    "completed": aggregate.completed,
                    "failed": aggregate.failed,
                    "skipped": self._initial_skipped,
                    "source_bytes_total": total_bytes,
                    "source_bytes_completed": completed_bytes,
                },
                "workers": workers,
                "queue": queue,
                "recent": list(self._recent_events),
            }

    def _save_state(self, status: str) -> None:
        aggregate = self._protocol.snapshot() if hasattr(
            self, "_protocol"
        ) else None
        value: dict[str, Any] = {
            "SchemaVersion": SCHEMA_VERSION,
            "RunId": self.run_id,
            "ContractHash": self.contract_hash,
            "RunnerBindingHash": self.runner_binding_hash,
            "Status": status,
            "FailureCategory": self._systemic_failure,
            "InitialSkipped": getattr(self, "_initial_skipped", 0),
            "ReservedBytes": self._reserved_bytes,
            "ConsecutiveFailures": self._consecutive_failures,
        }
        if aggregate is not None:
            value.update(asdict(aggregate))
            scheduling = self._scheduling_status_locked()
            value.update(
                {
                    "SchedulingMode": scheduling["scheduling_mode"],
                    "SchedulingState": scheduling[
                        "scheduling_state"
                    ],
                    "ValidationPolicy": scheduling[
                        "validation_policy"
                    ],
                    "PreferredWorkerRole": scheduling[
                        "preferred_worker_role"
                    ],
                    "HelperEligible": scheduling["helper_eligible"],
                    "RemoteEligible": scheduling["remote_eligible"],
                    "FallbackCountdownSeconds": scheduling[
                        "fallback_countdown_seconds"
                    ],
                    "HelperControlKnown": scheduling[
                        "helper_control_known"
                    ],
                    "HelperControlRevision": scheduling[
                        "helper_control_revision"
                    ],
                    "HelperPaused": scheduling["helper_paused"],
                }
            )
        stable_payload = _canonical_json(value)
        if (
            stable_payload == self._last_state_payload
            and os.path.isfile(self.state_path)
        ):
            return
        value["UpdatedUtc"] = time.time()
        _atomic_write_json(self.state_path, value)
        self._last_state_payload = stable_payload

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._async_threads or any(
                active.committing for active in self._active.values()
            ):
                raise CoordinatorError(
                    "ShutdownCommitActive", systemic=True
                )
            for active in list(self._active.values()):
                self._close_attempt_source_pins(active)
            self._active.clear()
            self._reserved_bytes = 0
            if self._root_pin_context is not None:
                self._root_pin_context.__exit__(None, None, None)
            for pin in self._immutable_pins:
                pin.close()
            self._immutable_pins.clear()
            if self._run_marker_pin is not None:
                self._run_marker_pin.close()
                self._run_marker_pin = None
            if self._coordinator_lock is not None:
                self._coordinator_lock.close()
                self._coordinator_lock = None
            self._closed = True

    def __enter__(self) -> "DistributedCoordinator":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def submit_payload_from_dict(value: object) -> SubmitPayload:
    if not isinstance(value, dict):
        raise CoordinatorError("RequestInvalid")
    try:
        raw_evidence = value.get("validation_evidence")
        validation_evidence = (
            None
            if raw_evidence is None
            else ProducerValidationEvidence.from_dict(raw_evidence)
        )
        payload = SubmitPayload(
            run_id=str(value["run_id"]),
            contract_hash=str(value["contract_hash"]),
            worker_id=str(value["worker_id"]),
            attempt_id=str(value["attempt_id"]),
            fencing_epoch=int(value["fencing_epoch"]),
            candidate_sha256=str(value["candidate_sha256"]).upper(),
            encoded_frame_count=int(value["encoded_frame_count"]),
            encode_seconds=float(value.get("encode_seconds", 0.0)),
            validation_evidence=validation_evidence,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        MediaContractError,
    ) as exc:
        raise CoordinatorError("RequestInvalid") from exc
    if (
        not payload.run_id
        or not payload.contract_hash
        or not payload.worker_id
        or not payload.attempt_id
        or len(payload.candidate_sha256) != 64
        or any(
            character not in "0123456789ABCDEF"
            for character in payload.candidate_sha256
        )
        or payload.fencing_epoch <= 0
        or payload.encoded_frame_count <= 0
        or payload.encode_seconds < 0
    ):
        raise CoordinatorError("RequestInvalid")
    return payload


__all__ = [
    "ClaimPayload",
    "CommitOutcome",
    "CoordinatorError",
    "DistributedCoordinator",
    "PlannedJob",
    "RUN_MARKER_NAME",
    "SCHEDULING_MODES",
    "SubmitPayload",
    "VALIDATION_POLICIES",
    "submit_payload_from_dict",
]
