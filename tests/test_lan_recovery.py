from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import lan_recovery
from lan_windows import FileIdentity, get_identity, protect_text


BINDING_HASH = "A" * 64
CONTRACT_HASH = "B" * 64
TRANSACTION_ID = "3" * 32
RUN_ID = "4" * 32
JOB_ID = "1" * 32
ATTEMPT_ID = "2" * 32
ORIGINAL_BYTES = b"original-source"
CANDIDATE_BYTES = b"validated-output"


@dataclass
class RecoveryCase:
    root: Path
    work: Path
    staging: Path
    journal: Path
    ledger: Path
    source: Path
    destination: Path
    candidate: Path
    backup: Path
    source_identity: FileIdentity
    candidate_identity: FileIdentity

    def recover(self) -> lan_recovery.RecoveryOutcome:
        return lan_recovery.recover_active_transaction(
            journal_path=str(self.journal),
            root=str(self.root),
            staging_root=str(self.staging),
            runner_binding_hash=BINDING_HASH,
            contract_hash=CONTRACT_HASH,
            ledger_path=str(self.ledger),
            now=lambda: 1234.5,
        )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _write_journal(
    case: RecoveryCase,
    *,
    same_path: bool,
    phase: str,
    candidate_hash: str | None = None,
    source_path: Path | None = None,
) -> None:
    value = {
        "SchemaVersion": 1,
        "TransactionId": TRANSACTION_ID,
        "RunId": RUN_ID,
        "RunnerBindingHash": BINDING_HASH,
        "ContractHash": CONTRACT_HASH,
        "JobId": JOB_ID,
        "AttemptId": ATTEMPT_ID,
        "FencingEpoch": 1,
        "Phase": phase,
        "SourceProtected": protect_text(str(source_path or case.source)),
        "DestinationProtected": protect_text(str(case.destination)),
        "CandidateProtected": protect_text(str(case.candidate)),
        "BackupProtected": protect_text(str(case.backup)),
        "SamePath": same_path,
        "SourceIdentity": case.source_identity.to_dict(),
        "CandidateIdentity": case.candidate_identity.to_dict(),
        "CandidateSha256": candidate_hash or _sha256(CANDIDATE_BYTES),
        "EncodedFrameCount": 100,
    }
    if phase in {
        "FinalValidated",
        "DeleteIntent",
        "SourceDeleted",
        "BackupDeleteIntent",
        "BackupDeleted",
    }:
        value["OutputIdentity"] = case.candidate_identity.to_dict()
    case.journal.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    marker = {
        "SchemaVersion": 1,
        "RunId": RUN_ID,
        "ContractHash": CONTRACT_HASH,
        "RunnerBindingHash": BINDING_HASH,
    }
    (case.staging / "coordinator-run.marker").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _make_case(
    tmp_path: Path,
    *,
    same_path: bool,
    phase: str,
    physical: str,
) -> RecoveryCase:
    root = tmp_path / "media"
    work = tmp_path / "work"
    staging = work / "staging"
    root.mkdir()
    staging.mkdir(parents=True)
    source = root / ("movie.mkv" if same_path else "movie.mp4")
    destination = root / "movie.mkv"
    candidate = (
        staging / f"candidate-{JOB_ID}-{ATTEMPT_ID}.ready.mkv"
    )
    backup = root / f".codex-original-backup-{TRANSACTION_ID}.bak"
    journal = work / "active-transaction.json"
    ledger = work / "completed-ledger.json"
    source.write_bytes(ORIGINAL_BYTES)
    candidate.write_bytes(CANDIDATE_BYTES)
    source_identity = get_identity(str(source))
    candidate_identity = get_identity(str(candidate))

    case = RecoveryCase(
        root=root,
        work=work,
        staging=staging,
        journal=journal,
        ledger=ledger,
        source=source,
        destination=destination,
        candidate=candidate,
        backup=backup,
        source_identity=source_identity,
        candidate_identity=candidate_identity,
    )

    if same_path and physical in {
        "original_renamed",
        "published",
        "backup_deleted",
    }:
        os.rename(source, backup)
    if physical in {"published", "source_deleted", "backup_deleted"}:
        os.rename(candidate, destination)
    if physical == "source_deleted":
        source.unlink()
    if physical == "backup_deleted":
        backup.unlink()
    _write_journal(case, same_path=same_path, phase=phase)
    return case


@pytest.mark.parametrize(
    ("phase", "physical"),
    [
        ("TempValidated", "candidate"),
        ("PublishIntent", "candidate"),
        ("PublishIntent", "published"),
        ("Published", "published"),
    ],
)
def test_distinct_early_phases_roll_back(
    tmp_path: Path, phase: str, physical: str
):
    case = _make_case(
        tmp_path, same_path=False, phase=phase, physical=physical
    )

    outcome = case.recover()

    assert outcome.kind == "RolledBack"
    assert case.source.read_bytes() == ORIGINAL_BYTES
    assert not case.destination.exists()
    assert not case.candidate.exists()
    assert not case.backup.exists()
    assert not case.journal.exists()
    assert not case.ledger.exists()


@pytest.mark.parametrize(
    ("phase", "physical"),
    [
        ("FinalValidated", "published"),
        ("DeleteIntent", "published"),
        ("DeleteIntent", "source_deleted"),
        ("SourceDeleted", "source_deleted"),
    ],
)
def test_distinct_validated_phases_roll_forward(
    tmp_path: Path, phase: str, physical: str
):
    case = _make_case(
        tmp_path, same_path=False, phase=phase, physical=physical
    )

    outcome = case.recover()

    assert outcome.kind == "RolledForward"
    assert not case.source.exists()
    assert case.destination.read_bytes() == CANDIDATE_BYTES
    assert not case.candidate.exists()
    assert not case.backup.exists()
    assert not case.journal.exists()
    ledger = json.loads(case.ledger.read_text(encoding="utf-8"))
    assert len(ledger) == 1
    assert ledger[0]["SettingsHash"] == CONTRACT_HASH
    assert ledger[0]["CompletedUtc"] == 1234.5


@pytest.mark.parametrize(
    ("phase", "physical"),
    [
        ("TempValidated", "candidate"),
        ("OriginalRenameIntent", "candidate"),
        ("OriginalRenameIntent", "original_renamed"),
        ("OriginalRenamed", "original_renamed"),
        ("PublishIntent", "original_renamed"),
        ("PublishIntent", "published"),
        ("Published", "published"),
    ],
)
def test_same_path_early_phases_roll_back(
    tmp_path: Path, phase: str, physical: str
):
    case = _make_case(
        tmp_path, same_path=True, phase=phase, physical=physical
    )

    outcome = case.recover()

    assert outcome.kind == "RolledBack"
    assert case.source.read_bytes() == ORIGINAL_BYTES
    assert not case.candidate.exists()
    assert not case.backup.exists()
    assert not case.journal.exists()
    assert not case.ledger.exists()


@pytest.mark.parametrize(
    ("phase", "physical"),
    [
        ("FinalValidated", "published"),
        ("BackupDeleteIntent", "published"),
        ("BackupDeleteIntent", "backup_deleted"),
        ("BackupDeleted", "backup_deleted"),
    ],
)
def test_same_path_validated_phases_roll_forward(
    tmp_path: Path, phase: str, physical: str
):
    case = _make_case(
        tmp_path, same_path=True, phase=phase, physical=physical
    )

    outcome = case.recover()

    assert outcome.kind == "RolledForward"
    assert case.source.read_bytes() == CANDIDATE_BYTES
    assert not case.candidate.exists()
    assert not case.backup.exists()
    assert not case.journal.exists()
    ledger = json.loads(case.ledger.read_text(encoding="utf-8"))
    assert len(ledger) == 1


def test_no_journal_is_a_noop(tmp_path: Path):
    root = tmp_path / "media"
    staging = tmp_path / "work" / "staging"
    root.mkdir()
    staging.mkdir(parents=True)

    outcome = lan_recovery.recover_active_transaction(
        journal_path=str(tmp_path / "work" / "active-transaction.json"),
        root=str(root),
        staging_root=str(staging),
        runner_binding_hash=BINDING_HASH,
        contract_hash=CONTRACT_HASH,
        ledger_path=str(tmp_path / "work" / "completed-ledger.json"),
    )

    assert outcome.kind == "NoJournal"


@pytest.mark.parametrize(
    "mutation",
    [
        "binding",
        "schema",
        "phase",
        "protected_path",
        "outside_root",
        "candidate_hash",
        "missing_original",
        "run_marker",
    ],
)
def test_ambiguous_or_tampered_journal_is_preserved(
    tmp_path: Path, mutation: str
):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="PublishIntent",
        physical="candidate",
    )
    raw = json.loads(case.journal.read_text(encoding="utf-8"))
    if mutation == "binding":
        raw["RunnerBindingHash"] = "C" * 64
    elif mutation == "schema":
        raw["Unexpected"] = True
    elif mutation == "phase":
        raw["Phase"] = "InventedPhase"
    elif mutation == "protected_path":
        raw["CandidateProtected"] = "not-dpapi"
    elif mutation == "outside_root":
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(ORIGINAL_BYTES)
        raw["SourceProtected"] = protect_text(str(outside))
    elif mutation == "candidate_hash":
        raw["CandidateSha256"] = "D" * 64
    elif mutation == "missing_original":
        case.source.unlink()
    elif mutation == "run_marker":
        marker = case.staging / "coordinator-run.marker"
        marker.write_text(
            json.dumps(
                {
                    "SchemaVersion": 1,
                    "RunId": "5" * 32,
                    "ContractHash": CONTRACT_HASH,
                    "RunnerBindingHash": BINDING_HASH,
                }
            ),
            encoding="utf-8",
        )
    case.journal.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()

    assert case.journal.exists()


def test_replaced_published_output_preserves_journal(tmp_path: Path):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="FinalValidated",
        physical="published",
    )
    case.destination.write_bytes(b"externally-replaced")

    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()

    assert case.journal.exists()
    assert case.source.read_bytes() == ORIGINAL_BYTES


def test_invalid_ledger_blocks_destructive_roll_forward(tmp_path: Path):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="FinalValidated",
        physical="published",
    )
    case.ledger.write_text('{"not":"a-list"}', encoding="utf-8")

    with pytest.raises(lan_recovery.RecoveryError) as caught:
        case.recover()

    assert caught.value.category == "LedgerInvalid"
    assert case.journal.exists()
    assert case.source.read_bytes() == ORIGINAL_BYTES


def test_interrupted_distinct_rollback_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="PublishIntent",
        physical="published",
    )
    real_delete = lan_recovery._delete

    def interrupt_delete(path: str, identity: FileIdentity) -> None:
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(lan_recovery, "_delete", interrupt_delete)
    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()
    assert case.journal.exists()
    assert case.candidate.read_bytes() == CANDIDATE_BYTES
    monkeypatch.setattr(lan_recovery, "_delete", real_delete)

    outcome = case.recover()

    assert outcome.kind == "RolledBack"
    assert case.source.read_bytes() == ORIGINAL_BYTES
    assert not case.candidate.exists()
    assert not case.destination.exists()


def test_interrupted_same_path_rollback_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = _make_case(
        tmp_path,
        same_path=True,
        phase="PublishIntent",
        physical="published",
    )
    real_delete = lan_recovery._delete

    def delete_then_interrupt(path: str, identity: FileIdentity) -> None:
        real_delete(path, identity)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(lan_recovery, "_delete", delete_then_interrupt)
    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()
    assert case.journal.exists()
    assert not case.source.exists()
    assert case.backup.read_bytes() == ORIGINAL_BYTES
    monkeypatch.setattr(lan_recovery, "_delete", real_delete)

    outcome = case.recover()

    assert outcome.kind == "RolledBack"
    assert case.source.read_bytes() == ORIGINAL_BYTES
    assert not case.backup.exists()


def test_interrupted_distinct_roll_forward_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="DeleteIntent",
        physical="published",
    )
    real_delete = lan_recovery._delete

    def delete_then_interrupt(path: str, identity: FileIdentity) -> None:
        real_delete(path, identity)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(lan_recovery, "_delete", delete_then_interrupt)
    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()
    assert case.journal.exists()
    assert not case.source.exists()
    monkeypatch.setattr(lan_recovery, "_delete", real_delete)

    outcome = case.recover()

    assert outcome.kind == "RolledForward"
    assert case.destination.read_bytes() == CANDIDATE_BYTES


def test_interrupted_same_path_roll_forward_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = _make_case(
        tmp_path,
        same_path=True,
        phase="BackupDeleteIntent",
        physical="published",
    )
    real_delete = lan_recovery._delete

    def delete_then_interrupt(path: str, identity: FileIdentity) -> None:
        real_delete(path, identity)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(lan_recovery, "_delete", delete_then_interrupt)
    with pytest.raises(lan_recovery.RecoveryError):
        case.recover()
    assert case.journal.exists()
    assert not case.backup.exists()
    monkeypatch.setattr(lan_recovery, "_delete", real_delete)

    outcome = case.recover()

    assert outcome.kind == "RolledForward"
    assert case.source.read_bytes() == CANDIDATE_BYTES


def test_existing_matching_ledger_record_is_idempotent(tmp_path: Path):
    case = _make_case(
        tmp_path,
        same_path=False,
        phase="SourceDeleted",
        physical="source_deleted",
    )
    first = case.recover()
    record = json.loads(case.ledger.read_text(encoding="utf-8"))[0]

    # Recreate only the final journal, as if the process crashed after the
    # durable ledger write but before journal deletion.
    _write_journal(
        case,
        same_path=False,
        phase="SourceDeleted",
    )
    second = case.recover()

    assert first.ledger_updated
    assert not second.ledger_updated
    assert json.loads(case.ledger.read_text(encoding="utf-8")) == [record]
