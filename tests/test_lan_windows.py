from __future__ import annotations

import os
from pathlib import Path

import pytest

import lan_windows


def test_identity_round_trip(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"identity-test")
    identity = lan_windows.get_identity(str(source))

    assert lan_windows.FileIdentity.from_dict(identity.to_dict()) == identity


def test_read_pin_allows_read_and_blocks_mutation_on_windows(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"pin-test")
    with lan_windows.open_read_pin(str(source)) as pin:
        assert lan_windows.get_pinned_identity(pin) == pin.identity
        assert source.read_bytes() == b"pin-test"
        if os.name == "nt":
            with pytest.raises(PermissionError):
                source.write_bytes(b"changed")
            with pytest.raises(PermissionError):
                source.unlink()


def test_flush_rename_and_delete_are_identity_bound(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"transaction-test")
    identity = lan_windows.get_identity(str(source))

    flushed = lan_windows.flush_verified(str(source), identity)
    assert flushed == identity
    assert lan_windows.rename_verified(
        str(source), str(destination), identity
    )
    assert not source.exists()
    assert lan_windows.get_identity(str(destination)) == identity
    assert lan_windows.delete_verified(str(destination), identity)
    assert not destination.exists()


def test_rename_never_replaces_existing_destination(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    identity = lan_windows.get_identity(str(source))

    assert not lan_windows.rename_verified(
        str(source), str(destination), identity
    )
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


def test_wrong_identity_cannot_rename_or_delete(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    identity = lan_windows.get_identity(str(source))
    wrong = lan_windows.FileIdentity(
        volume_serial_hex=identity.volume_serial_hex,
        file_id_hex=identity.file_id_hex,
        length=identity.length + 1,
        creation_file_time=identity.creation_file_time,
        last_write_file_time=identity.last_write_file_time,
    )

    assert not lan_windows.rename_verified(
        str(source), str(destination), wrong
    )
    assert not lan_windows.delete_verified(str(source), wrong)
    assert source.exists()


def test_prove_exclusive_access_observes_pin_on_windows(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    with lan_windows.open_read_pin(str(source)):
        if os.name == "nt":
            assert not lan_windows.prove_exclusive_access(str(source))
    assert lan_windows.prove_exclusive_access(str(source))


def test_exclusive_lock_rejects_second_owner_and_is_crash_releasable(
    tmp_path,
):
    lock_path = tmp_path / "coordinator.lock"
    first = lan_windows.open_exclusive_lock(str(lock_path))
    try:
        with pytest.raises(lan_windows.FileSafetyError):
            lan_windows.open_exclusive_lock(str(lock_path))
    finally:
        first.close()

    replacement = lan_windows.open_exclusive_lock(str(lock_path))
    replacement.close()


def test_reparse_component_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not available")

    with pytest.raises(lan_windows.FileSafetyError):
        lan_windows.assert_no_reparse_components(str(link / "child"))


def test_reparse_check_walks_lexical_path_without_resolving(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("path resolution would hide reparse components")
        ),
    )

    lan_windows.assert_no_reparse_components(str(tmp_path / "missing"))


def test_dpapi_text_round_trip():
    protected = lan_windows.protect_text("private-path-value")
    assert protected != "private-path-value"
    assert lan_windows.unprotect_text(protected) == "private-path-value"
