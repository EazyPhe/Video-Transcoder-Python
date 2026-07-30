"""Fail-closed recovery for coordinator publish transactions.

The coordinator journal intentionally contains enough evidence to recover
without enumerating the media directory or trusting path names supplied by a
worker.  Recovery has two policies:

* transactions that did not record ``FinalValidated`` are rolled back;
* transactions at or beyond ``FinalValidated`` are rolled forward.

Every mutation is identity checked.  A missing, extra, moved, replaced, busy,
or hash-mismatched artifact is ambiguous and leaves the journal in place.
Callers must hold the coordinator's exclusive lock for the entire call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lan_windows import (
    FileIdentity,
    FileSafetyError,
    assert_no_reparse_components,
    delete_verified,
    get_identity,
    open_directory_pin,
    prove_exclusive_access,
    rename_verified,
    unprotect_text,
)


SCHEMA_VERSION = 1

_COMMON_PHASES = {
    "TempValidated",
    "PublishIntent",
    "Published",
    "FinalValidated",
}
_SAME_PATH_PHASES = {
    "OriginalRenameIntent",
    "OriginalRenamed",
    "BackupDeleteIntent",
    "BackupDeleted",
}
_DISTINCT_PHASES = {"DeleteIntent", "SourceDeleted"}
_FORWARD_PHASES = {
    "FinalValidated",
    "BackupDeleteIntent",
    "BackupDeleted",
    "DeleteIntent",
    "SourceDeleted",
}
_HEX = frozenset("0123456789ABCDEF")

_REQUIRED_FIELDS = {
    "SchemaVersion",
    "TransactionId",
    "RunId",
    "RunnerBindingHash",
    "ContractHash",
    "JobId",
    "AttemptId",
    "FencingEpoch",
    "Phase",
    "SourceProtected",
    "DestinationProtected",
    "CandidateProtected",
    "BackupProtected",
    "SamePath",
    "SourceIdentity",
    "CandidateIdentity",
    "CandidateSha256",
    "EncodedFrameCount",
}
_OPTIONAL_FIELDS = {"OutputIdentity"}


class RecoveryError(RuntimeError):
    """A path-redacted, fail-closed transaction recovery error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class RecoveryOutcome:
    kind: str
    phase: str
    ledger_updated: bool = False


@dataclass(frozen=True)
class _Transaction:
    raw: dict[str, Any]
    transaction_id: str
    run_id: str
    phase: str
    same_path: bool
    source: str
    destination: str
    candidate: str
    backup: str
    source_identity: FileIdentity
    candidate_identity: FileIdentity
    output_identity: FileIdentity | None
    candidate_sha256: str


def _normalized_path(path: str) -> str:
    return os.path.abspath(path).rstrip("\\/").upper()


def _path_hash(path: str) -> str:
    return hashlib.sha256(
        _normalized_path(path).encode("utf-8")
    ).hexdigest().upper()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _valid_hex(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    return all(character in _HEX for character in value.upper())


def _read_json(
    path: str,
    expected_type: type,
    *,
    category: str = "JournalInvalid",
) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(category) from exc
    if not isinstance(value, expected_type):
        raise RecoveryError(category)
    return value


def _atomic_write_json(path: str, value: object) -> None:
    destination = Path(path)
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


def _direct_child(path: str, parent: str) -> bool:
    return _normalized_path(os.path.dirname(path)) == _normalized_path(parent)


def _decrypt_path(value: object) -> str:
    if not isinstance(value, str):
        raise RecoveryError("JournalInvalid")
    try:
        path = unprotect_text(value)
    except FileSafetyError as exc:
        raise RecoveryError("JournalProtectedDataInvalid") from exc
    if (
        not isinstance(path, str)
        or not path
        or not os.path.isabs(path)
        or os.path.normpath(path) != path
    ):
        raise RecoveryError("JournalPathInvalid")
    return os.path.abspath(path)


def _parse_transaction(
    raw: dict[str, Any],
    *,
    root: str,
    staging_root: str,
    runner_binding_hash: str,
    contract_hash: str,
) -> _Transaction:
    if set(raw) - (_REQUIRED_FIELDS | _OPTIONAL_FIELDS):
        raise RecoveryError("JournalSchemaInvalid")
    if not _REQUIRED_FIELDS.issubset(raw):
        raise RecoveryError("JournalSchemaInvalid")
    if raw.get("SchemaVersion") != SCHEMA_VERSION:
        raise RecoveryError("JournalSchemaInvalid")
    if not isinstance(raw.get("SamePath"), bool):
        raise RecoveryError("JournalSchemaInvalid")
    if (
        not _valid_hex(raw.get("TransactionId"), 32)
        or not _valid_hex(raw.get("RunId"), 32)
        or not _valid_hex(raw.get("JobId"), 32)
        or not _valid_hex(raw.get("AttemptId"), 32)
        or not _valid_hex(raw.get("RunnerBindingHash"), 64)
        or not _valid_hex(raw.get("ContractHash"), 64)
        or not _valid_hex(raw.get("CandidateSha256"), 64)
    ):
        raise RecoveryError("JournalSchemaInvalid")
    if (
        not hmac.compare_digest(
            str(raw["RunnerBindingHash"]).upper(),
            runner_binding_hash.upper(),
        )
        or not hmac.compare_digest(
            str(raw["ContractHash"]).upper(), contract_hash.upper()
        )
    ):
        raise RecoveryError("JournalBindingMismatch")
    try:
        fencing_epoch = int(raw["FencingEpoch"])
        encoded_frames = int(raw["EncodedFrameCount"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecoveryError("JournalSchemaInvalid") from exc
    if fencing_epoch <= 0 or encoded_frames <= 0:
        raise RecoveryError("JournalSchemaInvalid")

    phase = raw.get("Phase")
    same_path = bool(raw["SamePath"])
    valid_phases = _COMMON_PHASES | (
        _SAME_PATH_PHASES if same_path else _DISTINCT_PHASES
    )
    if not isinstance(phase, str) or phase not in valid_phases:
        raise RecoveryError("JournalPhaseInvalid")

    try:
        source_identity = FileIdentity.from_dict(raw["SourceIdentity"])
        candidate_identity = FileIdentity.from_dict(raw["CandidateIdentity"])
        output_identity = (
            FileIdentity.from_dict(raw["OutputIdentity"])
            if "OutputIdentity" in raw
            else None
        )
    except FileSafetyError as exc:
        raise RecoveryError("JournalIdentityInvalid") from exc
    if candidate_identity.length <= 0:
        raise RecoveryError("JournalIdentityInvalid")
    if (
        candidate_identity.volume_serial_hex
        != source_identity.volume_serial_hex
    ):
        raise RecoveryError("JournalIdentityInvalid")
    if output_identity is not None and output_identity != candidate_identity:
        raise RecoveryError("JournalIdentityInvalid")
    if phase in _FORWARD_PHASES and output_identity is None:
        raise RecoveryError("JournalIdentityInvalid")
    if phase not in _FORWARD_PHASES and output_identity is not None:
        raise RecoveryError("JournalSchemaInvalid")

    source = _decrypt_path(raw["SourceProtected"])
    destination = _decrypt_path(raw["DestinationProtected"])
    candidate = _decrypt_path(raw["CandidateProtected"])
    backup = _decrypt_path(raw["BackupProtected"])
    root = os.path.abspath(root)
    staging_root = os.path.abspath(staging_root)

    if (
        not _direct_child(source, root)
        or not _direct_child(destination, root)
        or not _direct_child(backup, root)
        or not _direct_child(candidate, staging_root)
        or _normalized_path(destination)
        != _normalized_path(str(Path(source).with_suffix(".mkv")))
        or same_path
        != (_normalized_path(source) == _normalized_path(destination))
    ):
        raise RecoveryError("JournalPathInvalid")
    expected_candidate = (
        f"candidate-{str(raw['JobId']).lower()}-"
        f"{str(raw['AttemptId']).lower()}.ready.mkv"
    )
    expected_backup = (
        f".codex-original-backup-"
        f"{str(raw['TransactionId']).lower()}.bak"
    )
    if (
        os.path.basename(candidate).lower() != expected_candidate
        or os.path.basename(backup).lower() != expected_backup
    ):
        raise RecoveryError("JournalPathInvalid")
    if len(
        {
            _normalized_path(source),
            _normalized_path(candidate),
            _normalized_path(backup),
        }
    ) != 3:
        raise RecoveryError("JournalPathInvalid")
    if (
        not same_path
        and len(
            {
                _normalized_path(source),
                _normalized_path(destination),
                _normalized_path(candidate),
                _normalized_path(backup),
            }
        )
        != 4
    ):
        raise RecoveryError("JournalPathInvalid")

    try:
        for path in (
            root,
            staging_root,
            source,
            destination,
            candidate,
            backup,
        ):
            assert_no_reparse_components(path)
    except FileSafetyError as exc:
        raise RecoveryError("JournalPathUnsafe") from exc

    return _Transaction(
        raw=dict(raw),
        transaction_id=str(raw["TransactionId"]),
        run_id=str(raw["RunId"]),
        phase=phase,
        same_path=same_path,
        source=source,
        destination=destination,
        candidate=candidate,
        backup=backup,
        source_identity=source_identity,
        candidate_identity=candidate_identity,
        output_identity=output_identity,
        candidate_sha256=str(raw["CandidateSha256"]).upper(),
    )


def _identity_or_none(path: str) -> FileIdentity | None:
    if not os.path.exists(path):
        return None
    try:
        identity = get_identity(path)
    except (OSError, FileSafetyError) as exc:
        raise RecoveryError("RecoveryArtifactInvalid") from exc
    if not prove_exclusive_access(path):
        raise RecoveryError("RecoveryArtifactBusy")
    return identity


def _validate_run_marker(
    staging_root: str,
    transaction: _Transaction,
    runner_binding_hash: str,
    contract_hash: str,
) -> None:
    marker_path = os.path.join(staging_root, "coordinator-run.marker")
    try:
        assert_no_reparse_components(marker_path)
    except FileSafetyError as exc:
        raise RecoveryError("RunMarkerUnsafe") from exc
    identity_before = _identity_or_none(marker_path)
    if identity_before is None:
        raise RecoveryError("RunMarkerMissing")
    marker = _read_json(
        marker_path, dict, category="RunMarkerInvalid"
    )
    identity_after = _identity_or_none(marker_path)
    if identity_before != identity_after:
        raise RecoveryError("RunMarkerChanged")
    if set(marker) != {
        "SchemaVersion",
        "RunId",
        "ContractHash",
        "RunnerBindingHash",
    }:
        raise RecoveryError("RunMarkerInvalid")
    if (
        marker.get("SchemaVersion") != SCHEMA_VERSION
        or marker.get("RunId") != transaction.run_id
        or not isinstance(marker.get("ContractHash"), str)
        or not isinstance(marker.get("RunnerBindingHash"), str)
        or not hmac.compare_digest(
            str(marker["ContractHash"]).upper(), contract_hash.upper()
        )
        or not hmac.compare_digest(
            str(marker["RunnerBindingHash"]).upper(),
            runner_binding_hash.upper(),
        )
    ):
        raise RecoveryError("RunMarkerBindingMismatch")


def _require_identity(
    actual: FileIdentity | None,
    expected: FileIdentity,
    category: str,
) -> None:
    if actual != expected:
        raise RecoveryError(category)


def _require_absent(actual: FileIdentity | None, category: str) -> None:
    if actual is not None:
        raise RecoveryError(category)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RecoveryError("RecoveryArtifactUnreadable") from exc
    return digest.hexdigest().upper()


def _require_candidate_hash(path: str, transaction: _Transaction) -> None:
    if not hmac.compare_digest(
        _sha256(path), transaction.candidate_sha256
    ):
        raise RecoveryError("RecoveryCandidateHashMismatch")


def _rename(path: str, destination: str, identity: FileIdentity) -> None:
    try:
        moved = rename_verified(path, destination, identity)
    except (OSError, FileSafetyError) as exc:
        raise RecoveryError("RecoveryRenameFailed") from exc
    if not moved:
        raise RecoveryError("RecoveryRenameFailed")


def _delete(path: str, identity: FileIdentity) -> None:
    try:
        deleted = delete_verified(path, identity)
    except (OSError, FileSafetyError) as exc:
        raise RecoveryError("RecoveryDeleteFailed") from exc
    if not deleted:
        raise RecoveryError("RecoveryDeleteFailed")


def _remove_journal(journal_path: str) -> None:
    identity = _identity_or_none(journal_path)
    if identity is None:
        raise RecoveryError("JournalDisappeared")
    _delete(journal_path, identity)


def _set_phase(
    journal_path: str,
    transaction: _Transaction,
    next_phase: str,
) -> _Transaction:
    current = _read_json(journal_path, dict)
    if (
        current.get("TransactionId") != transaction.transaction_id
        or current.get("RunnerBindingHash")
        != transaction.raw["RunnerBindingHash"]
        or current.get("Phase") != transaction.phase
    ):
        raise RecoveryError("JournalChanged")
    updated = dict(transaction.raw)
    updated["Phase"] = next_phase
    _atomic_write_json(journal_path, updated)
    return _Transaction(
        raw=updated,
        transaction_id=transaction.transaction_id,
        run_id=transaction.run_id,
        phase=next_phase,
        same_path=transaction.same_path,
        source=transaction.source,
        destination=transaction.destination,
        candidate=transaction.candidate,
        backup=transaction.backup,
        source_identity=transaction.source_identity,
        candidate_identity=transaction.candidate_identity,
        output_identity=transaction.output_identity,
        candidate_sha256=transaction.candidate_sha256,
    )


def _inspect(
    transaction: _Transaction,
) -> tuple[
    FileIdentity | None,
    FileIdentity | None,
    FileIdentity | None,
    FileIdentity | None,
]:
    source = _identity_or_none(transaction.source)
    destination = (
        source
        if transaction.same_path
        else _identity_or_none(transaction.destination)
    )
    candidate = _identity_or_none(transaction.candidate)
    backup = _identity_or_none(transaction.backup)
    allowed = {transaction.source_identity, transaction.candidate_identity}
    for identity in {source, destination, candidate, backup} - {None}:
        if identity not in allowed:
            raise RecoveryError("RecoveryArtifactIdentityMismatch")
    locations = (
        [source, candidate, backup]
        if transaction.same_path
        else [source, destination, candidate, backup]
    )
    if sum(value == transaction.source_identity for value in locations) > 1:
        raise RecoveryError("RecoveryDuplicateIdentity")
    if sum(value == transaction.candidate_identity for value in locations) > 1:
        raise RecoveryError("RecoveryDuplicateIdentity")
    return source, destination, candidate, backup


def _rollback_distinct(
    journal_path: str, transaction: _Transaction
) -> RecoveryOutcome:
    source, destination, candidate, backup = _inspect(transaction)
    _require_identity(
        source, transaction.source_identity, "RecoveryOriginalMissing"
    )
    _require_absent(backup, "RecoveryUnexpectedBackup")

    if transaction.phase == "TempValidated":
        _require_absent(destination, "RecoveryUnexpectedPublishedOutput")
    if destination is not None:
        _require_identity(
            destination,
            transaction.candidate_identity,
            "RecoveryPublishedIdentityMismatch",
        )
        _require_absent(candidate, "RecoveryDuplicateCandidate")
        _require_candidate_hash(transaction.destination, transaction)
        _rename(
            transaction.destination,
            transaction.candidate,
            transaction.candidate_identity,
        )
        candidate = transaction.candidate_identity
    if candidate is not None:
        _require_identity(
            candidate,
            transaction.candidate_identity,
            "RecoveryCandidateIdentityMismatch",
        )
        _require_candidate_hash(transaction.candidate, transaction)
        _delete(transaction.candidate, transaction.candidate_identity)

    source, destination, candidate, backup = _inspect(transaction)
    _require_identity(
        source, transaction.source_identity, "RecoveryOriginalMissing"
    )
    _require_absent(destination, "RecoveryRollbackIncomplete")
    _require_absent(candidate, "RecoveryRollbackIncomplete")
    _require_absent(backup, "RecoveryRollbackIncomplete")
    _remove_journal(journal_path)
    return RecoveryOutcome("RolledBack", transaction.phase)


def _rollback_same_path(
    journal_path: str, transaction: _Transaction
) -> RecoveryOutcome:
    source, _destination, candidate, backup = _inspect(transaction)
    phase = transaction.phase
    original_at_source = source == transaction.source_identity
    original_at_backup = backup == transaction.source_identity
    output_at_source = source == transaction.candidate_identity

    if phase == "TempValidated":
        if not original_at_source or backup is not None or output_at_source:
            raise RecoveryError("RecoveryStateImpossible")
    elif phase in {"OriginalRenameIntent", "OriginalRenamed"}:
        if output_at_source:
            raise RecoveryError("RecoveryStateImpossible")
        if not (original_at_source or original_at_backup):
            raise RecoveryError("RecoveryOriginalMissing")
    else:
        if not (original_at_source or original_at_backup):
            raise RecoveryError("RecoveryOriginalMissing")

    if output_at_source:
        _require_absent(candidate, "RecoveryDuplicateCandidate")
        _require_candidate_hash(transaction.source, transaction)
        _delete(transaction.source, transaction.candidate_identity)
        source = None
    if original_at_backup:
        if source is not None:
            raise RecoveryError("RecoveryRestoreCollision")
        _rename(
            transaction.backup,
            transaction.source,
            transaction.source_identity,
        )
        source = transaction.source_identity
        backup = None
    if candidate is not None:
        _require_identity(
            candidate,
            transaction.candidate_identity,
            "RecoveryCandidateIdentityMismatch",
        )
        _require_candidate_hash(transaction.candidate, transaction)
        _delete(transaction.candidate, transaction.candidate_identity)

    source, _destination, candidate, backup = _inspect(transaction)
    _require_identity(
        source, transaction.source_identity, "RecoveryOriginalMissing"
    )
    _require_absent(candidate, "RecoveryRollbackIncomplete")
    _require_absent(backup, "RecoveryRollbackIncomplete")
    _remove_journal(journal_path)
    return RecoveryOutcome("RolledBack", transaction.phase)


def _load_ledger(ledger_path: str) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_path):
        return []
    value = _read_json(ledger_path, list, category="LedgerInvalid")
    if not all(isinstance(item, dict) for item in value):
        raise RecoveryError("LedgerInvalid")
    for item in value:
        required = {
            "PathHash",
            "SettingsHash",
            "VolumeSerialHex",
            "FileIdHex",
            "Length",
            "CreationFileTime",
            "LastWriteFileTime",
            "CompletedUtc",
        }
        if not required.issubset(item):
            raise RecoveryError("LedgerInvalid")
    return [dict(item) for item in value]


def _record_ledger(
    ledger_path: str,
    records: list[dict[str, Any]],
    *,
    published_path: str,
    output_identity: FileIdentity,
    contract_hash: str,
    now: Callable[[], float],
) -> bool:
    path_hash = _path_hash(published_path)
    expected = {
        "PathHash": path_hash,
        "SettingsHash": contract_hash.upper(),
        "VolumeSerialHex": output_identity.volume_serial_hex,
        "FileIdHex": output_identity.file_id_hex,
        "Length": output_identity.length,
        "CreationFileTime": output_identity.creation_file_time,
        "LastWriteFileTime": output_identity.last_write_file_time,
    }
    for record in records:
        if all(record.get(key) == value for key, value in expected.items()):
            return False
    remaining = [
        record
        for record in records
        if str(record.get("PathHash", "")).upper() != path_hash
    ]
    remaining.append({**expected, "CompletedUtc": float(now())})
    _atomic_write_json(ledger_path, remaining)
    return True


def _roll_forward_distinct(
    journal_path: str,
    ledger_path: str,
    transaction: _Transaction,
    contract_hash: str,
    now: Callable[[], float],
) -> RecoveryOutcome:
    records = _load_ledger(ledger_path)
    source, destination, candidate, backup = _inspect(transaction)
    _require_absent(candidate, "RecoveryUnexpectedCandidate")
    _require_absent(backup, "RecoveryUnexpectedBackup")
    _require_identity(
        destination,
        transaction.output_identity,
        "RecoveryPublishedIdentityMismatch",
    )
    _require_candidate_hash(transaction.destination, transaction)

    if transaction.phase == "FinalValidated":
        _require_identity(
            source, transaction.source_identity, "RecoveryOriginalMissing"
        )
        transaction = _set_phase(
            journal_path, transaction, "DeleteIntent"
        )
    elif transaction.phase == "DeleteIntent":
        if source not in {transaction.source_identity, None}:
            raise RecoveryError("RecoveryOriginalIdentityMismatch")
    elif transaction.phase == "SourceDeleted":
        _require_absent(source, "RecoveryOriginalStillPresent")
    else:
        raise RecoveryError("JournalPhaseInvalid")

    if source is not None:
        _require_identity(
            source,
            transaction.source_identity,
            "RecoveryOriginalIdentityMismatch",
        )
        _delete(transaction.source, transaction.source_identity)
    if transaction.phase != "SourceDeleted":
        transaction = _set_phase(
            journal_path, transaction, "SourceDeleted"
        )
    source, destination, candidate, backup = _inspect(transaction)
    _require_absent(source, "RecoveryDeleteIncomplete")
    _require_identity(
        destination,
        transaction.output_identity,
        "RecoveryPublishedIdentityMismatch",
    )
    _require_absent(candidate, "RecoveryUnexpectedCandidate")
    _require_absent(backup, "RecoveryUnexpectedBackup")
    _require_candidate_hash(transaction.destination, transaction)
    ledger_updated = _record_ledger(
        ledger_path,
        records,
        published_path=transaction.destination,
        output_identity=transaction.output_identity,
        contract_hash=contract_hash,
        now=now,
    )
    _remove_journal(journal_path)
    return RecoveryOutcome(
        "RolledForward", transaction.phase, ledger_updated
    )


def _roll_forward_same_path(
    journal_path: str,
    ledger_path: str,
    transaction: _Transaction,
    contract_hash: str,
    now: Callable[[], float],
) -> RecoveryOutcome:
    records = _load_ledger(ledger_path)
    source, _destination, candidate, backup = _inspect(transaction)
    _require_absent(candidate, "RecoveryUnexpectedCandidate")
    _require_identity(
        source,
        transaction.output_identity,
        "RecoveryPublishedIdentityMismatch",
    )
    _require_candidate_hash(transaction.source, transaction)

    if transaction.phase == "FinalValidated":
        _require_identity(
            backup, transaction.source_identity, "RecoveryOriginalMissing"
        )
        transaction = _set_phase(
            journal_path, transaction, "BackupDeleteIntent"
        )
    elif transaction.phase == "BackupDeleteIntent":
        if backup not in {transaction.source_identity, None}:
            raise RecoveryError("RecoveryOriginalIdentityMismatch")
    elif transaction.phase == "BackupDeleted":
        _require_absent(backup, "RecoveryBackupStillPresent")
    else:
        raise RecoveryError("JournalPhaseInvalid")

    if backup is not None:
        _require_identity(
            backup,
            transaction.source_identity,
            "RecoveryOriginalIdentityMismatch",
        )
        _delete(transaction.backup, transaction.source_identity)
    if transaction.phase != "BackupDeleted":
        transaction = _set_phase(
            journal_path, transaction, "BackupDeleted"
        )
    source, _destination, candidate, backup = _inspect(transaction)
    _require_identity(
        source,
        transaction.output_identity,
        "RecoveryPublishedIdentityMismatch",
    )
    _require_absent(candidate, "RecoveryUnexpectedCandidate")
    _require_absent(backup, "RecoveryBackupDeleteIncomplete")
    _require_candidate_hash(transaction.source, transaction)
    ledger_updated = _record_ledger(
        ledger_path,
        records,
        published_path=transaction.source,
        output_identity=transaction.output_identity,
        contract_hash=contract_hash,
        now=now,
    )
    _remove_journal(journal_path)
    return RecoveryOutcome(
        "RolledForward", transaction.phase, ledger_updated
    )


def recover_active_transaction(
    *,
    journal_path: str,
    root: str,
    staging_root: str,
    runner_binding_hash: str,
    contract_hash: str,
    ledger_path: str,
    now: Callable[[], float] = time.time,
) -> RecoveryOutcome:
    """Recover one coordinator transaction while preserving ambiguity.

    The caller must already own the coordinator's exclusive lock.  No directory
    enumeration is performed.  ``NoJournal`` is returned when there is no work.
    All raised errors are path-redacted and leave the journal in place unless a
    previous invocation had already completed the recovery.
    """

    journal_path = os.path.abspath(journal_path)
    root = os.path.abspath(root)
    staging_root = os.path.abspath(staging_root)
    ledger_path = os.path.abspath(ledger_path)
    if not os.path.exists(journal_path):
        return RecoveryOutcome("NoJournal", "")
    if (
        not _valid_hex(runner_binding_hash, 64)
        or not _valid_hex(contract_hash, 64)
        or _normalized_path(os.path.dirname(journal_path))
        != _normalized_path(os.path.dirname(ledger_path))
        or _normalized_path(os.path.dirname(staging_root))
        != _normalized_path(os.path.dirname(journal_path))
        or os.path.basename(staging_root).lower() != "staging"
        or os.path.basename(journal_path).lower()
        != "active-transaction.json"
        or os.path.basename(ledger_path).lower()
        != "completed-ledger.json"
    ):
        raise RecoveryError("RecoveryBindingInvalid")
    try:
        for path in (
            journal_path,
            ledger_path,
            root,
            staging_root,
        ):
            assert_no_reparse_components(path)
        with ExitStack() as stack:
            for directory in dict.fromkeys(
                (root, os.path.dirname(journal_path), staging_root)
            ):
                stack.enter_context(open_directory_pin(directory))
            raw = _read_json(journal_path, dict)
            transaction = _parse_transaction(
                raw,
                root=root,
                staging_root=staging_root,
                runner_binding_hash=runner_binding_hash,
                contract_hash=contract_hash,
            )
            _validate_run_marker(
                staging_root,
                transaction,
                runner_binding_hash,
                contract_hash,
            )
            if transaction.phase in _FORWARD_PHASES:
                if transaction.same_path:
                    return _roll_forward_same_path(
                        journal_path,
                        ledger_path,
                        transaction,
                        contract_hash,
                        now,
                    )
                return _roll_forward_distinct(
                    journal_path,
                    ledger_path,
                    transaction,
                    contract_hash,
                    now,
                )
            if transaction.same_path:
                return _rollback_same_path(journal_path, transaction)
            return _rollback_distinct(journal_path, transaction)
    except RecoveryError:
        raise
    except (OSError, FileSafetyError) as exc:
        raise RecoveryError("RecoveryUnsafe") from exc
    except Exception as exc:
        raise RecoveryError("RecoveryInterrupted") from exc


__all__ = [
    "RecoveryError",
    "RecoveryOutcome",
    "recover_active_transaction",
]
