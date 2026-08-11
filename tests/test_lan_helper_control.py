from __future__ import annotations

import json
import multiprocessing
import os
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import lan_helper_control
from lan_helper_control import (
    HELPER_CONTROL_MAX_REVISION,
    HELPER_CONTROL_SCHEMA_VERSION,
    HelperControlCommand,
    HelperControlError,
    HelperControlStatus,
    HelperControlStore,
    set_desired_state,
)


CONTROL_ID = "a" * 32
RUN_ID = "b" * 32


def _claim_gate_in_child(
    control_file: str,
    status_file: str,
    control_id: str,
    timeout_seconds: float,
    results,
) -> None:
    store = HelperControlStore(control_file, status_file, control_id)
    try:
        with store.claim_gate(timeout_seconds=timeout_seconds):
            results.put("acquired")
    except HelperControlError as exc:
        results.put(exc.category)


def _store(tmp_path: Path) -> HelperControlStore:
    return HelperControlStore(
        tmp_path / "helper-control.json",
        tmp_path / "helper-control-status.json",
        CONTROL_ID,
    )


def _command_payload(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": HELPER_CONTROL_SCHEMA_VERSION,
        "control_id": CONTROL_ID,
        "revision": 1,
        "pc_in_use": True,
    }
    value.update(updates)
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_missing_control_and_status_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(HelperControlError) as control_error:
        store.read_command()
    with pytest.raises(HelperControlError) as status_error:
        store.read_status()

    assert control_error.value.category == "ControlMissing"
    assert status_error.value.category == "StatusMissing"


def test_public_records_are_frozen() -> None:
    command = HelperControlCommand(1, CONTROL_ID, 1, True)
    status = HelperControlStatus(
        1,
        CONTROL_ID,
        1,
        "pc_in_use",
        "pending",
        "",
        "",
    )

    with pytest.raises(FrozenInstanceError):
        command.revision = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        status.effective_state = "paused"  # type: ignore[misc]


def test_windows_gate_namespace_is_exactly_session_local() -> None:
    assert (
        lan_helper_control._WINDOWS_GATE_PREFIX
        == "Local\\VideoTranscoderLanControlGate-"
    )
    assert "Global\\" not in lan_helper_control._WINDOWS_GATE_PREFIX


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff",
        b"[]",
        b"",
        b"{" + (b" " * 4096) + b"}",
    ],
)
def test_corrupt_or_oversized_control_is_invalid(
    tmp_path: Path,
    raw: bytes,
) -> None:
    store = _store(tmp_path)
    Path(store.control_file).write_bytes(raw)

    with pytest.raises(HelperControlError) as error:
        store.read_command()

    assert error.value.category == "ControlInvalid"


def test_unknown_and_duplicate_control_keys_are_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    path = Path(store.control_file)
    _write_json(path, {**_command_payload(), "unexpected": 1})
    with pytest.raises(HelperControlError) as unknown_error:
        store.read_command()

    path.write_text(
        '{"schema_version":1,"control_id":"'
        + CONTROL_ID
        + '","revision":1,"revision":2,"pc_in_use":true}',
        encoding="utf-8",
    )
    with pytest.raises(HelperControlError) as duplicate_error:
        store.read_command()

    assert unknown_error.value.category == "ControlInvalid"
    assert duplicate_error.value.category == "ControlInvalid"


@pytest.mark.parametrize(
    ("updates", "category"),
    [
        ({"schema_version": True}, "ControlInvalid"),
        ({"control_id": "A" * 32}, "ControlInvalid"),
        ({"revision": True}, "ControlInvalid"),
        ({"revision": 0}, "ControlInvalid"),
        ({"revision": HELPER_CONTROL_MAX_REVISION + 1}, "ControlInvalid"),
        ({"pc_in_use": 1}, "ControlInvalid"),
    ],
)
def test_command_fields_are_strict(
    tmp_path: Path,
    updates: dict[str, object],
    category: str,
) -> None:
    store = _store(tmp_path)
    _write_json(Path(store.control_file), _command_payload(**updates))

    with pytest.raises(HelperControlError) as error:
        store.read_command()

    assert error.value.category == category


def test_hard_linked_control_is_unsafe_when_supported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    control = Path(store.control_file)
    _write_json(control, _command_payload())
    alias = tmp_path / "control-alias.json"
    try:
        os.link(control, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(HelperControlError) as error:
        store.read_command()

    assert error.value.category == "ControlUnsafe"


def test_non_regular_control_is_unsafe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    Path(store.control_file).mkdir()

    with pytest.raises(HelperControlError) as error:
        store.read_command()

    assert error.value.category == "ControlUnsafe"


def test_symlinked_control_is_unsafe_when_supported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "target.json"
    _write_json(target, _command_payload())
    control = Path(store.control_file)
    try:
        control.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(HelperControlError) as error:
        store.read_command()

    assert error.value.category == "ControlUnsafe"


def test_setter_atomically_creates_and_increments_command_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    first = set_desired_state(
        store.control_file,
        store.status_file,
        CONTROL_ID,
        True,
    )
    second = set_desired_state(
        store.control_file,
        store.status_file,
        CONTROL_ID,
        True,
    )
    third = set_desired_state(
        store.control_file,
        store.status_file,
        CONTROL_ID,
        False,
    )

    assert first == HelperControlCommand(1, CONTROL_ID, 1, True)
    assert second == HelperControlCommand(1, CONTROL_ID, 2, True)
    assert third == HelperControlCommand(1, CONTROL_ID, 3, False)
    assert store.read_command() == third
    with pytest.raises(HelperControlError) as status_error:
        store.read_status()
    assert status_error.value.category == "StatusMissing"
    assert set(json.loads(Path(store.control_file).read_text("utf-8"))) == {
        "schema_version",
        "control_id",
        "revision",
        "pc_in_use",
    }
    assert not list(tmp_path.glob(".*.tmp"))


def test_setter_never_overwrites_helper_owned_draining_status(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    acknowledged = HelperControlCommand(1, CONTROL_ID, 8, True)
    store.write_status(
        acknowledged,
        effective_state="draining",
        category="",
        run_id=RUN_ID,
    )
    before = Path(store.status_file).read_bytes()

    command = set_desired_state(
        store.control_file,
        store.status_file,
        CONTROL_ID,
        False,
    )

    assert command == HelperControlCommand(1, CONTROL_ID, 1, False)
    assert Path(store.status_file).read_bytes() == before
    assert store.read_status() == HelperControlStatus(
        1,
        CONTROL_ID,
        8,
        "pc_in_use",
        "draining",
        "",
        RUN_ID,
    )


@pytest.mark.parametrize(
    "effective_state",
    ["pending", "paused", "available", "blocked", "working", "draining"],
)
def test_status_round_trip_supports_all_effective_states(
    tmp_path: Path,
    effective_state: str,
) -> None:
    store = _store(tmp_path)

    written = store.write_status(
        HelperControlCommand(1, CONTROL_ID, 7, True),
        effective_state=effective_state,
        category="CoordinatorDisconnected",
        run_id=RUN_ID,
    )

    assert store.read_status() == written


@pytest.mark.parametrize(
    "updates",
    [
        {"revision": True},
        {"desired_state": "unknown"},
        {"desired_state": []},
        {"effective_state": "unknown"},
        {"effective_state": {}},
        {"category": "contains/path"},
        {"category": "x" * 65},
        {"run_id": "B" * 32},
        {"run_id": "a" * 31},
    ],
)
def test_status_fields_are_strict(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    store = _store(tmp_path)
    status: dict[str, object] = {
        "schema_version": 1,
        "control_id": CONTROL_ID,
        "revision": 1,
        "desired_state": "available",
        "effective_state": "available",
        "category": "",
        "run_id": "",
    }
    status.update(updates)
    _write_json(Path(store.status_file), status)

    with pytest.raises(HelperControlError) as error:
        store.read_status()

    assert error.value.category == "StatusInvalid"


def test_unknown_and_duplicate_status_keys_are_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    status = {
        "schema_version": 1,
        "control_id": CONTROL_ID,
        "revision": 1,
        "desired_state": "available",
        "effective_state": "available",
        "category": "",
        "run_id": "",
        "unexpected": True,
    }
    _write_json(Path(store.status_file), status)
    with pytest.raises(HelperControlError) as unknown_error:
        store.read_status()

    Path(store.status_file).write_text(
        '{"schema_version":1,"control_id":"'
        + CONTROL_ID
        + '","revision":1,"desired_state":"available",'
        '"effective_state":"available","category":"",'
        '"category":"Again","run_id":""}',
        encoding="utf-8",
    )
    with pytest.raises(HelperControlError) as duplicate_error:
        store.read_status()

    assert unknown_error.value.category == "StatusInvalid"
    assert duplicate_error.value.category == "StatusInvalid"


def test_setter_refuses_to_replace_corrupt_or_hard_linked_control(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    control = Path(store.control_file)
    control.write_text("not-json", encoding="utf-8")
    with pytest.raises(HelperControlError) as corrupt_error:
        set_desired_state(
            store.control_file,
            store.status_file,
            CONTROL_ID,
            True,
        )
    assert corrupt_error.value.category == "ControlInvalid"
    assert control.read_text("utf-8") == "not-json"

    _write_json(control, _command_payload())
    alias = tmp_path / "alias.json"
    try:
        os.link(control, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(HelperControlError) as link_error:
        set_desired_state(
            store.control_file,
            store.status_file,
            CONTROL_ID,
            False,
        )
    assert link_error.value.category == "ControlGateUnsafe"


def test_status_writer_refuses_hard_link_destination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status = Path(store.status_file)
    status.write_text("existing", encoding="utf-8")
    alias = tmp_path / "status-alias.json"
    try:
        os.link(status, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(HelperControlError) as error:
        store.write_status(
            HelperControlCommand(1, CONTROL_ID, 1, False),
            effective_state="available",
            category="",
        )

    assert error.value.category == "StatusUnsafe"
    assert status.read_text("utf-8") == "existing"


def test_status_reader_refuses_hard_link_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status = Path(store.status_file)
    store.write_status(
        HelperControlCommand(1, CONTROL_ID, 1, False),
        effective_state="available",
        category="",
    )
    alias = tmp_path / "status-read-alias.json"
    try:
        os.link(status, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(HelperControlError) as error:
        store.read_status()

    assert error.value.category == "StatusUnsafe"


def test_status_writer_rejects_invalid_or_mismatched_command(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(HelperControlError) as invalid_error:
        store.write_status(
            HelperControlCommand(1, CONTROL_ID, 0, False),
            effective_state="available",
            category="",
        )
    with pytest.raises(HelperControlError) as mismatch_error:
        store.write_status(
            HelperControlCommand(1, "b" * 32, 1, False),
            effective_state="available",
            category="",
        )

    assert invalid_error.value.category == "StatusInvalid"
    assert mismatch_error.value.category == "StatusInvalid"
    assert not Path(store.status_file).exists()


def test_paths_and_control_id_must_be_exact_and_safe(tmp_path: Path) -> None:
    with pytest.raises(HelperControlError) as relative_error:
        HelperControlStore(
            "relative-control.json",
            tmp_path / "status.json",
            CONTROL_ID,
        )
    with pytest.raises(HelperControlError) as same_error:
        HelperControlStore(
            tmp_path / "same.json",
            tmp_path / "same.json",
            CONTROL_ID,
        )
    with pytest.raises(HelperControlError) as id_error:
        HelperControlStore(
            tmp_path / "control.json",
            tmp_path / "status.json",
            "A" * 32,
        )

    assert relative_error.value.category == "PathInvalid"
    assert same_error.value.category == "PathInvalid"
    assert id_error.value.category == "ControlInvalid"


def test_claim_gate_serializes_worker_selection_before_tray_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    initial = set_desired_state(
        store.control_file,
        store.status_file,
        CONTROL_ID,
        False,
    )
    setter_started = threading.Event()
    setter_finished = threading.Event()
    setter_result: list[HelperControlCommand] = []

    def pause_from_tray() -> None:
        setter_started.set()
        setter_result.append(
            set_desired_state(
                store.control_file,
                store.status_file,
                CONTROL_ID,
                True,
            )
        )
        setter_finished.set()

    with store.claim_gate():
        thread = threading.Thread(target=pause_from_tray)
        thread.start()
        assert setter_started.wait(1)
        assert not setter_finished.wait(0.15)
        # This is the worker's final eligibility read and source-selection
        # point. The already-waiting tray setter cannot pass it.
        selected = store.read_command()
        assert selected == initial
        assert selected.pc_in_use is False

    assert setter_finished.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert setter_result == [
        HelperControlCommand(1, CONTROL_ID, initial.revision + 1, True)
    ]
    assert store.read_command() == setter_result[0]


def test_claim_gate_is_cross_process_and_timeout_is_stable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    context = multiprocessing.get_context("spawn")
    blocked_results = context.Queue()

    with store.claim_gate():
        blocked = context.Process(
            target=_claim_gate_in_child,
            args=(
                store.control_file,
                store.status_file,
                CONTROL_ID,
                0.2,
                blocked_results,
            ),
        )
        blocked.start()
        assert blocked_results.get(timeout=5) == "ControlGateTimeout"
        blocked.join(timeout=5)
        if blocked.is_alive():
            blocked.terminate()
            blocked.join(timeout=2)
        assert blocked.exitcode == 0
    blocked_results.close()
    blocked_results.join_thread()

    acquired_results = context.Queue()
    acquired = context.Process(
        target=_claim_gate_in_child,
        args=(
            store.control_file,
            store.status_file,
            CONTROL_ID,
            2.0,
            acquired_results,
        ),
    )
    acquired.start()
    assert acquired_results.get(timeout=5) == "acquired"
    acquired.join(timeout=5)
    if acquired.is_alive():
        acquired.terminate()
        acquired.join(timeout=2)
    assert acquired.exitcode == 0
    acquired_results.close()
    acquired_results.join_thread()


def test_claim_gate_releases_after_body_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic"):
        with store.claim_gate():
            raise RuntimeError("synthetic")

    with store.claim_gate(timeout_seconds=0.25):
        assert True


@pytest.mark.parametrize(
    "timeout_seconds",
    [True, 0, -1, float("inf"), float("nan"), 61],
)
def test_claim_gate_timeout_is_strict(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(HelperControlError) as error:
        with store.claim_gate(timeout_seconds=timeout_seconds):  # type: ignore[arg-type]
            raise AssertionError("invalid timeout entered the gate")

    assert error.value.category == "ControlGateInvalid"


def test_claim_gate_refuses_hard_linked_control(tmp_path: Path) -> None:
    store = _store(tmp_path)
    control = Path(store.control_file)
    _write_json(control, _command_payload())
    alias = tmp_path / "gate-control-alias.json"
    try:
        os.link(control, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(HelperControlError) as error:
        with store.claim_gate():
            raise AssertionError("unsafe control entered the gate")

    assert error.value.category == "ControlGateUnsafe"
