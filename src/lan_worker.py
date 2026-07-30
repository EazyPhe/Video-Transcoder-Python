"""Compute-only workers for the LAN transcoder.

The helper worker never publishes a remote final filename and never deletes a
remote source. It writes one attempt-specific staging candidate and asks the
storage-host coordinator to validate and commit it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from lan_coordinator import (
    RUN_MARKER_NAME,
    ClaimPayload,
    CommitOutcome,
    DistributedCoordinator,
    SubmitPayload,
)
from lan_media import (
    DEFAULT_CONTRACT,
    SUPPORTED_EXTENSIONS,
    CandidateEvidence,
    DecodeStats,
    MediaContract,
    MediaContractError,
    encode_candidate,
    sha256_file,
    validate_candidate,
    validate_top_level_relative_name,
)
from lan_protocol import WorkerRole
from lan_windows import (
    FileIdentity,
    FileSafetyError,
    delete_verified,
    flush_verified,
    get_identity,
    open_read_pin,
    rename_verified,
)


_CANDIDATE_NAME = re.compile(
    r"^candidate-[0-9a-f]{32}-[0-9a-f]{32}\.ready\.mkv$"
)
_LOCAL_LEDGER_NAME = ".video-transcoder-local-ledger.json"


class WorkerError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CoordinatorClient(Protocol):
    def helper_seen(self) -> None: ...

    def claim(
        self, worker_id: str, worker_role: WorkerRole | str
    ) -> ClaimPayload | None: ...

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
    ) -> ClaimPayload: ...

    def abandon(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
    ) -> bool: ...

    def report_failure(
        self,
        *,
        worker_id: str,
        attempt_id: str,
        fencing_epoch: int,
        failure_category: str,
    ) -> bool: ...

    def submit(self, payload: SubmitPayload) -> CommitOutcome: ...


@dataclass(frozen=True)
class WorkerResult:
    kind: str
    category: str
    source_size_bytes: int = 0
    encode_seconds: float = 0.0


@dataclass
class _Progress:
    seconds: float = 0.0
    frames: int = 0
    phase: str = "Claimed"
    media_duration_seconds: float = 0.0
    encode_elapsed_seconds: float = 0.0
    transfer_bytes: int = 0
    transfer_total_bytes: int = 0
    transfer_elapsed_seconds: float = 0.0


class _CombinedCancel:
    def __init__(self, *events: threading.Event | None):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(
                0.05
                if deadline is None
                else min(0.05, max(0.0, deadline - time.monotonic()))
            )
        return True


class DirectCoordinatorClient:
    """Adapter used by the storage host's local QSV worker."""

    def __init__(self, coordinator: DistributedCoordinator):
        self.coordinator = coordinator

    def helper_seen(self) -> None:
        self.coordinator.helper_seen()

    def claim(self, worker_id, worker_role):
        return self.coordinator.claim(worker_id, worker_role)

    def heartbeat(self, **kwargs):
        return self.coordinator.heartbeat(**kwargs)

    def abandon(self, **kwargs):
        return self.coordinator.abandon(**kwargs)

    def report_failure(self, **kwargs):
        return self.coordinator.report_failure(**kwargs)

    def submit(self, payload):
        return self.coordinator.submit(payload)


class LeaseHeartbeat:
    def __init__(
        self,
        client: CoordinatorClient,
        claim: ClaimPayload,
        progress: _Progress,
        *,
        interval_seconds: float = 10.0,
        failure_limit: int = 3,
    ) -> None:
        self.client = client
        self.claim = claim
        self.progress = progress
        self.interval_seconds = float(interval_seconds)
        self.failure_limit = int(failure_limit)
        self.stop_event = threading.Event()
        self.failed_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._started = time.monotonic()

    def start(self) -> None:
        if self._thread is not None:
            raise WorkerError("HeartbeatAlreadyStarted")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.pulse()
                self._failures = 0
            except Exception:
                self._failures += 1
                if self._failures >= self.failure_limit:
                    self.failed_event.set()
                    return
            if self.stop_event.wait(self.interval_seconds):
                return

    def pulse(self) -> None:
        with self._lock:
            values = {
                "progress_seconds": self.progress.seconds,
                "frame_count": self.progress.frames,
                "phase": self.progress.phase,
                "media_duration_seconds": (
                    self.progress.media_duration_seconds
                ),
                "encode_elapsed_seconds": (
                    self.progress.encode_elapsed_seconds
                ),
                "transfer_bytes": self.progress.transfer_bytes,
                "transfer_total_bytes": (
                    self.progress.transfer_total_bytes
                ),
                "transfer_elapsed_seconds": (
                    self.progress.transfer_elapsed_seconds
                ),
            }
        self.client.heartbeat(
            worker_id=self.claim.worker_id,
            attempt_id=self.claim.attempt_id,
            fencing_epoch=self.claim.fencing_epoch,
            **values,
        )

    def update(self, stats: DecodeStats) -> None:
        with self._lock:
            self.progress.seconds = max(
                self.progress.seconds, stats.out_time_seconds
            )
            self.progress.frames = max(
                self.progress.frames, stats.frame_count
            )
            self.progress.encode_elapsed_seconds = max(
                self.progress.encode_elapsed_seconds,
                time.monotonic() - self._started,
            )

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.progress.phase = str(phase)

    def set_media_info(self, info) -> None:
        with self._lock:
            self.progress.media_duration_seconds = max(
                self.progress.media_duration_seconds,
                float(info.duration),
            )

    def update_transfer(
        self,
        transferred: int,
        total: int,
        elapsed: float,
        phase: str,
    ) -> None:
        with self._lock:
            self.progress.phase = str(phase)
            self.progress.transfer_bytes = max(
                self.progress.transfer_bytes, int(transferred)
            )
            self.progress.transfer_total_bytes = max(
                self.progress.transfer_total_bytes, int(total)
            )
            self.progress.transfer_elapsed_seconds = max(
                self.progress.transfer_elapsed_seconds, float(elapsed)
            )

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_seconds * 2))


def _safe_child(root: str, name: str) -> str:
    valid_name = validate_top_level_relative_name(name)
    root_path = os.path.abspath(root)
    child = os.path.abspath(os.path.join(root_path, valid_name))
    if os.path.normcase(os.path.dirname(child)) != os.path.normcase(root_path):
        raise WorkerError("PathEscapeRejected")
    return child


def _validate_claim(claim: ClaimPayload) -> None:
    if (
        claim.schema_version != 1
        or not claim.run_id
        or not claim.contract_hash
        or not claim.job_id
        or not claim.attempt_id
        or claim.fencing_epoch <= 0
        or claim.source_size_bytes <= 0
        or claim.maximum_output_bytes <= 0
        or not _CANDIDATE_NAME.fullmatch(claim.candidate_name)
        or claim.job_id not in claim.candidate_name
        or claim.attempt_id not in claim.candidate_name
    ):
        raise WorkerError("ClaimInvalid")
    validate_top_level_relative_name(claim.source_alias)


def _remove_exact(path: str) -> None:
    try:
        if not os.path.exists(path):
            return
        identity = get_identity(path)
        delete_verified(path, identity)
    except (OSError, FileSafetyError):
        pass


def _upload_candidate(
    local_candidate: str,
    staging_share_root: str,
    candidate_name: str,
    expected_sha256: str,
    *,
    cancel_event=None,
    progress_callback=None,
) -> str:
    ready_path = _safe_child(staging_share_root, candidate_name)
    upload_path = ready_path + ".upload"
    if os.path.exists(ready_path) or os.path.exists(upload_path):
        raise WorkerError("AttemptArtifactCollision")
    os.makedirs(staging_share_root, exist_ok=True)
    total_bytes = os.path.getsize(local_candidate)
    # Network work includes both the SMB write and the independent SMB read
    # used to verify what the storage host actually received.
    transfer_total_bytes = total_bytes * 2
    started = time.monotonic()
    source_digest = hashlib.sha256()
    try:
        if progress_callback is not None:
            progress_callback(0, transfer_total_bytes, 0.0, "Uploading")
        with open(local_candidate, "rb") as source:
            with open(upload_path, "xb", buffering=0) as destination:
                transferred = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise WorkerError("CoordinatorDisconnected")
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    destination.write(block)
                    source_digest.update(block)
                    transferred += len(block)
                    if progress_callback is not None:
                        progress_callback(
                            transferred,
                            transfer_total_bytes,
                            time.monotonic() - started,
                            "Uploading",
                        )
                destination.flush()
                os.fsync(destination.fileno())
        if source_digest.hexdigest().upper() != expected_sha256.upper():
            raise WorkerError("LocalCandidateHashMismatch")
        if os.path.getsize(upload_path) != total_bytes:
            raise WorkerError("UploadSizeMismatch")
        upload_identity = get_identity(upload_path)
        upload_identity = flush_verified(upload_path, upload_identity)
        uploaded_digest = hashlib.sha256()
        verified = 0
        if progress_callback is not None:
            progress_callback(
                total_bytes,
                transfer_total_bytes,
                time.monotonic() - started,
                "UploadVerification",
            )
        with open(upload_path, "rb") as uploaded:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise WorkerError("CoordinatorDisconnected")
                block = uploaded.read(1024 * 1024)
                if not block:
                    break
                uploaded_digest.update(block)
                verified += len(block)
                if (
                    progress_callback is not None
                    and verified < total_bytes
                ):
                    progress_callback(
                        total_bytes + verified,
                        transfer_total_bytes,
                        time.monotonic() - started,
                        "UploadVerification",
                    )
        if uploaded_digest.hexdigest().upper() != expected_sha256.upper():
            raise WorkerError("UploadHashMismatch")
        if progress_callback is not None:
            progress_callback(
                transfer_total_bytes,
                transfer_total_bytes,
                time.monotonic() - started,
                "UploadVerification",
            )
        if not rename_verified(upload_path, ready_path, upload_identity):
            raise WorkerError("UploadPublishFailed")
        return ready_path
    except Exception:
        _remove_exact(upload_path)
        raise


class ComputeWorker:
    """Produce one fenced candidate using QSV or NVENC."""

    def __init__(
        self,
        *,
        client: CoordinatorClient,
        worker_id: str,
        worker_role: WorkerRole,
        encoder: str,
        source_root: str,
        staging_root: str,
        local_cache_root: str,
        ffmpeg: str,
        ffprobe: str,
        contract: MediaContract = DEFAULT_CONTRACT,
        heartbeat_interval_seconds: float = 2.0,
        heartbeat_failure_limit: int = 3,
        direct_staging: bool = False,
        external_cancel_event: threading.Event | None = None,
    ) -> None:
        if encoder not in {"hevc_qsv", "hevc_nvenc"}:
            raise WorkerError("EncoderUnsupported")
        if worker_role is WorkerRole.HELPER and encoder != "hevc_nvenc":
            raise WorkerError("HelperEncoderInvalid")
        if worker_role is WorkerRole.REMOTE and encoder != "hevc_qsv":
            raise WorkerError("RemoteEncoderInvalid")
        self.client = client
        self.worker_id = worker_id
        self.worker_role = worker_role
        self.encoder = encoder
        self.source_root = os.path.abspath(source_root)
        self.staging_root = os.path.abspath(staging_root)
        self.local_cache_root = os.path.abspath(local_cache_root)
        self.ffmpeg = os.path.abspath(ffmpeg)
        self.ffprobe = os.path.abspath(ffprobe)
        self.contract = contract
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.heartbeat_failure_limit = heartbeat_failure_limit
        self.direct_staging = direct_staging
        self.external_cancel_event = external_cancel_event
        os.makedirs(self.local_cache_root, exist_ok=True)

    def run_one(self) -> WorkerResult:
        marker_path = _safe_child(self.staging_root, RUN_MARKER_NAME)
        try:
            marker_pin = open_read_pin(marker_path)
        except (OSError, FileSafetyError):
            return WorkerResult(
                "Disconnected"
                if self.worker_role is WorkerRole.HELPER
                else "Failure",
                "RunMarkerUnavailable",
            )
        try:
            if self.worker_role is WorkerRole.HELPER:
                self.client.helper_seen()
            claim = self.client.claim(self.worker_id, self.worker_role)
        except Exception:
            marker_pin.close()
            raise
        if claim is None:
            marker_pin.close()
            return WorkerResult("Idle", "NoPendingJob")
        try:
            _validate_claim(claim)
            claimed_marker = FileIdentity.from_dict(
                claim.run_marker_identity
            )
            if marker_pin.identity != claimed_marker:
                raise WorkerError("RunMarkerMismatch")
        except (WorkerError, FileSafetyError):
            marker_pin.close()
            self._abandon(claim)
            return WorkerResult(
                "Failure",
                "RunMarkerMismatch",
                source_size_bytes=claim.source_size_bytes,
            )
        if claim.contract_hash != self.contract.digest():
            marker_pin.close()
            self._report_failure(claim, "ContractMismatch")
            return WorkerResult(
                "Failure",
                "ContractMismatch",
                source_size_bytes=claim.source_size_bytes,
            )

        source_path = _safe_child(self.source_root, claim.source_alias)
        if self.direct_staging:
            local_candidate = _safe_child(
                self.staging_root, claim.candidate_name
            )
        else:
            local_name = (
                f"local-{claim.job_id}-{claim.attempt_id}.part.mkv"
            )
            local_candidate = _safe_child(
                self.local_cache_root, local_name
            )
        if os.path.exists(local_candidate):
            marker_pin.close()
            self._report_failure(claim, "LocalCandidateCollision")
            return WorkerResult(
                "Failure",
                "LocalCandidateCollision",
                source_size_bytes=claim.source_size_bytes,
            )

        progress = _Progress()
        heartbeat = LeaseHeartbeat(
            self.client,
            claim,
            progress,
            interval_seconds=self.heartbeat_interval_seconds,
            failure_limit=self.heartbeat_failure_limit,
        )
        cancel_event = _CombinedCancel(
            heartbeat.failed_event, self.external_cancel_event
        )
        source_pin = None
        ready_path = ""
        submission_started = False
        try:
            source_pin = open_read_pin(source_path)
            claimed_identity = FileIdentity.from_dict(
                claim.source_identity
            )
            if (
                source_pin.identity != claimed_identity
                or source_pin.identity.length != claim.source_size_bytes
            ):
                raise WorkerError("SourceIdentityMismatch")
            heartbeat.start()
            _info, _audio_durations, evidence = encode_candidate(
                self.ffmpeg,
                self.ffprobe,
                source_path,
                local_candidate,
                self.encoder,
                claim.maximum_output_bytes,
                cancel_event=cancel_event,
                progress_callback=heartbeat.update,
                metadata_callback=heartbeat.set_media_info,
                phase_callback=heartbeat.set_phase,
                contract=self.contract,
            )
            if cancel_event.is_set():
                raise WorkerError("CoordinatorDisconnected")
            if not self.direct_staging:
                ready_path = _upload_candidate(
                    local_candidate,
                    self.staging_root,
                    claim.candidate_name,
                    evidence.sha256,
                    cancel_event=cancel_event,
                    progress_callback=heartbeat.update_transfer,
                )
            else:
                ready_path = local_candidate
            if cancel_event.is_set():
                raise WorkerError("CoordinatorDisconnected")
            heartbeat.set_phase("RemoteValidation")
            heartbeat.pulse()
            heartbeat.stop()
            source_pin.close()
            source_pin = None
            submission_started = True
            outcome = self.client.submit(
                SubmitPayload(
                    run_id=claim.run_id,
                    contract_hash=claim.contract_hash,
                    worker_id=claim.worker_id,
                    attempt_id=claim.attempt_id,
                    fencing_epoch=claim.fencing_epoch,
                    candidate_sha256=evidence.sha256,
                    encoded_frame_count=evidence.encoded_frame_count,
                    encode_seconds=evidence.encode_seconds,
                )
            )
            if outcome.kind != "Success":
                raise WorkerError(outcome.category)
            if not self.direct_staging:
                _remove_exact(local_candidate)
            return WorkerResult(
                "Success",
                outcome.category,
                source_size_bytes=claim.source_size_bytes,
                encode_seconds=evidence.encode_seconds,
            )
        except (MediaContractError, FileSafetyError) as exc:
            category = getattr(exc, "category", "WorkerFailed")
            heartbeat.stop()
            if source_pin is not None:
                source_pin.close()
                source_pin = None
            if ready_path and not submission_started:
                _remove_exact(ready_path)
                ready_path = ""
            if not submission_started:
                _remove_exact(local_candidate)
            external_stop = (
                self.external_cancel_event is not None
                and self.external_cancel_event.is_set()
            )
            disconnected = heartbeat.failed_event.is_set()
            if disconnected or external_stop:
                self._abandon(claim)
            else:
                self._report_failure(claim, str(category))
            return WorkerResult(
                (
                    "Disconnected"
                    if disconnected
                    else "Stopped"
                    if external_stop
                    else "Failure"
                ),
                (
                    "CoordinatorDisconnected"
                    if disconnected
                    else "ExternalStop"
                    if external_stop
                    else str(category)
                ),
                source_size_bytes=claim.source_size_bytes,
            )
        except WorkerError as exc:
            heartbeat.stop()
            if source_pin is not None:
                source_pin.close()
                source_pin = None
            if ready_path and not submission_started:
                _remove_exact(ready_path)
                ready_path = ""
            if not submission_started:
                _remove_exact(local_candidate)
            external_stop = (
                self.external_cancel_event is not None
                and self.external_cancel_event.is_set()
            )
            if exc.category == "CoordinatorDisconnected" or external_stop:
                self._abandon(claim)
            else:
                self._report_failure(claim, exc.category)
            return WorkerResult(
                (
                    "Stopped"
                    if external_stop
                    else "Disconnected"
                    if exc.category == "CoordinatorDisconnected"
                    else "Failure"
                ),
                "ExternalStop" if external_stop else exc.category,
                source_size_bytes=claim.source_size_bytes,
            )
        finally:
            heartbeat.stop()
            marker_pin.close()
            if source_pin is not None:
                source_pin.close()
            if (
                ready_path
                and not self.direct_staging
                and not submission_started
            ):
                # A submitted candidate is removed by coordinator rename. A
                # stale rejected candidate is attempt-specific and safe to
                # remove only by exact identity.
                _remove_exact(ready_path)
            if not submission_started or not self.direct_staging:
                _remove_exact(local_candidate)

    def _abandon(self, claim: ClaimPayload) -> None:
        try:
            self.client.abandon(
                worker_id=claim.worker_id,
                attempt_id=claim.attempt_id,
                fencing_epoch=claim.fencing_epoch,
            )
        except Exception:
            pass

    def _report_failure(
        self, claim: ClaimPayload, failure_category: str
    ) -> None:
        try:
            self.client.report_failure(
                worker_id=claim.worker_id,
                attempt_id=claim.attempt_id,
                fencing_epoch=claim.fencing_epoch,
                failure_category=failure_category,
            )
        except Exception:
            pass


class LocalFallbackWorker:
    """Safely process one local file while the LAN coordinator is unavailable.

    Local fallback never deletes the original. This makes disconnect behavior
    useful without silently applying the remote batch's destructive policy to
    unrelated files on the helper PC.
    """

    def __init__(
        self,
        *,
        root: str,
        ffmpeg: str,
        ffprobe: str,
        encoder: str = "hevc_nvenc",
        contract: MediaContract = DEFAULT_CONTRACT,
    ) -> None:
        self.root = os.path.abspath(root)
        self.ffmpeg = os.path.abspath(ffmpeg)
        self.ffprobe = os.path.abspath(ffprobe)
        self.encoder = encoder
        self.contract = contract

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.root, _LOCAL_LEDGER_NAME)

    @staticmethod
    def _path_hash(path: str) -> str:
        normalized = os.path.normcase(os.path.abspath(path)).upper()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()

    def _load_ledger(self) -> list[dict]:
        if not os.path.exists(self.ledger_path):
            return []
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, UnicodeError) as exc:
            raise WorkerError("LocalLedgerInvalid") from exc
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise WorkerError("LocalLedgerInvalid")
        return value

    def _ledger_match(self, path: str, identity: FileIdentity, ledger) -> bool:
        path_hash = self._path_hash(path)
        return any(
            str(item.get("PathHash", "")).upper() == path_hash
            and item.get("Identity") == identity.to_dict()
            for item in ledger
        )

    def _save_ledger(self, ledger: list[dict]) -> None:
        destination = Path(self.ledger_path)
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    ledger,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            raise WorkerError("LocalLedgerWriteFailed") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def run_one(self) -> WorkerResult:
        os.makedirs(self.root, exist_ok=True)
        try:
            ledger = self._load_ledger()
        except WorkerError as exc:
            return WorkerResult("Failure", exc.category)
        try:
            candidates = sorted(
                (
                    entry
                    for entry in os.scandir(self.root)
                    if entry.is_file(follow_symlinks=False)
                    and not entry.name.startswith(".")
                    and Path(entry.name).suffix.lower()
                    in SUPPORTED_EXTENSIONS
                    and not self._ledger_match(
                        entry.path,
                        get_identity(entry.path),
                        ledger,
                    )
                ),
                key=lambda entry: entry.stat(
                    follow_symlinks=False
                ).st_size,
                reverse=True,
            )
        except (OSError, FileSafetyError):
            return WorkerResult("Failure", "LocalScanFailed")
        for entry in candidates:
            source = os.path.abspath(entry.path)
            if Path(source).suffix.lower() == ".mkv":
                destination = str(
                    Path(source).with_name(Path(source).stem + ".local.mkv")
                )
            else:
                destination = str(Path(source).with_suffix(".mkv"))
            if os.path.exists(destination):
                continue
            temporary = str(
                Path(destination).with_name(
                    f".{Path(destination).stem}."
                    f"{uuid.uuid4().hex}.part.mkv"
                )
            )
            source_pin = None
            published_identity = None
            try:
                source_pin = open_read_pin(source)
                free = shutil.disk_usage(self.root).free
                maximum = max(1, free - 2 * 1024**3)
                info, audio, evidence = encode_candidate(
                    self.ffmpeg,
                    self.ffprobe,
                    source,
                    temporary,
                    self.encoder,
                    maximum,
                    contract=self.contract,
                )
                identity = get_identity(temporary)
                identity = flush_verified(temporary, identity)
                if not rename_verified(temporary, destination, identity):
                    raise WorkerError("LocalPublishFailed")
                published_identity = identity
                if not validate_candidate(
                    self.ffmpeg,
                    self.ffprobe,
                    destination,
                    info,
                    audio,
                    evidence.encoded_frame_count,
                    contract=self.contract,
                ):
                    raise WorkerError("LocalPublishedValidationFailed")
                ledger = [
                    item
                    for item in ledger
                    if str(item.get("PathHash", "")).upper()
                    != self._path_hash(destination)
                ]
                ledger.append(
                    {
                        "PathHash": self._path_hash(destination),
                        "Identity": get_identity(destination).to_dict(),
                    }
                )
                self._save_ledger(ledger)
                return WorkerResult(
                    "Success",
                    "LocalCommittedOriginalRetained",
                    source_size_bytes=entry.stat(
                        follow_symlinks=False
                    ).st_size,
                    encode_seconds=evidence.encode_seconds,
                )
            except (OSError, MediaContractError, FileSafetyError, WorkerError) as exc:
                _remove_exact(temporary)
                if published_identity is not None:
                    try:
                        delete_verified(destination, published_identity)
                    except (OSError, FileSafetyError):
                        pass
                return WorkerResult(
                    "Failure",
                    getattr(exc, "category", "LocalFallbackFailed"),
                )
            finally:
                if source_pin is not None:
                    source_pin.close()
        return WorkerResult("Idle", "NoLocalWork")


def default_fallback_root() -> str:
    """Use the user-visible executable folder in frozen portable builds."""
    import sys

    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path.cwd())


__all__ = [
    "ComputeWorker",
    "CoordinatorClient",
    "DirectCoordinatorClient",
    "LeaseHeartbeat",
    "LocalFallbackWorker",
    "WorkerError",
    "WorkerResult",
    "default_fallback_root",
]
