import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import lan_tray
from lan_helper_control import (
    HelperControlCommand,
    HelperControlError,
    HelperControlStatus,
)


CONTROL_ID = "019fc655d07b7f13adef0ce2beebc1b8"


def command(revision=1, pc_in_use=False):
    return HelperControlCommand(
        schema_version=1,
        control_id=CONTROL_ID,
        revision=revision,
        pc_in_use=pc_in_use,
    )


def status(
    revision=1,
    desired_state="available",
    effective_state="available",
    category="Ready",
):
    return HelperControlStatus(
        schema_version=1,
        control_id=CONTROL_ID,
        revision=revision,
        desired_state=desired_state,
        effective_state=effective_state,
        category=category,
        run_id="019fc655d07b7f13adef0ce2beebc1b8",
    )


class FakeStore:
    def __init__(self, current_command, current_status=None):
        self.current_command = current_command
        self.current_status = current_status

    def read_command(self):
        if isinstance(self.current_command, Exception):
            raise self.current_command
        return self.current_command

    def read_status(self):
        if isinstance(self.current_status, Exception):
            raise self.current_status
        if self.current_status is None:
            raise HelperControlError("StatusMissing")
        return self.current_status


def tray_config(tmp_path):
    status_path = tmp_path / "helper-control-status.json"
    if not status_path.exists():
        status_path.write_bytes(b"{}")
    return lan_tray.TrayConfig(
        config_path=str(tmp_path / "VideoTranscoderLanAssist.json"),
        control_file=str(tmp_path / "helper-control.json"),
        control_status_file=str(status_path),
        control_id=CONTROL_ID,
    )


def test_tray_config_reads_only_exact_local_control_paths(tmp_path):
    config_path = tmp_path / "VideoTranscoderLanAssist.json"
    # The token target intentionally does not exist. Loading tray configuration
    # must not open or validate the bearer token.
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "helper",
                "token_file": str(tmp_path / "must-not-be-opened.token"),
                "control_file": ".\\helper-control.json",
                "control_status_file": ".\\helper-control-status.json",
                "control_id": CONTROL_ID,
            }
        ),
        encoding="utf-8",
    )

    parsed = lan_tray.load_tray_config(str(config_path))

    assert parsed.control_id == CONTROL_ID
    assert Path(parsed.control_file) == tmp_path / "helper-control.json"
    assert Path(parsed.control_status_file) == (
        tmp_path / "helper-control-status.json"
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"control_file": "nested/helper-control.json"},
        {"control_file": "helper-control-status.json"},
        {"control_status_file": "other.json"},
        {"control_id": "bad control id"},
    ],
)
def test_tray_config_rejects_nonexact_control_identity(tmp_path, updates):
    value = {
        "schema_version": 1,
        "mode": "helper",
        "control_file": "helper-control.json",
        "control_status_file": "helper-control-status.json",
        "control_id": CONTROL_ID,
    }
    value.update(updates)
    config_path = tmp_path / "VideoTranscoderLanAssist.json"
    config_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(lan_tray.TrayError) as rejected:
        lan_tray.load_tray_config(str(config_path))

    assert rejected.value.category == "TrayConfigInvalid"


def test_pause_persists_command_then_starts_helper_to_announce_or_drain(tmp_path):
    store = FakeStore(command(), status())
    set_calls = []
    task_calls = []

    def setter(control_file, status_file, control_id, pc_in_use):
        set_calls.append((control_file, status_file, control_id, pc_in_use))
        store.current_command = command(2, pc_in_use=True)
        return store.current_command

    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=store,
        setter=setter,
        task_starter=lambda: task_calls.append("start"),
    )

    view = controller.pause_after_current_file()

    assert set_calls == [
        (
            str(tmp_path / "helper-control.json"),
            str(tmp_path / "helper-control-status.json"),
            CONTROL_ID,
            True,
        )
    ]
    assert task_calls == ["start"]
    assert view.pc_in_use is True
    assert "Pause requested" in view.label


def test_resume_persists_available_then_starts_fixed_task_once(tmp_path):
    store = FakeStore(
        command(pc_in_use=True),
        status(desired_state="pc_in_use", effective_state="paused"),
    )
    order = []

    def setter(_control_file, _status_file, _control_id, pc_in_use):
        order.append(("set", pc_in_use))
        store.current_command = command(2, pc_in_use=False)
        return store.current_command

    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=store,
        setter=setter,
        task_starter=lambda: order.append(("start", False)),
    )

    view = controller.resume()

    assert order == [("set", False), ("start", False)]
    assert view.pc_in_use is False


def test_pause_task_start_failure_keeps_new_pause_command(tmp_path):
    store = FakeStore(command(), status())

    def setter(_control_file, _status_file, _control_id, pc_in_use):
        store.current_command = command(2, pc_in_use=pc_in_use)
        return store.current_command

    def fail_start():
        raise lan_tray.TrayError("HelperTaskStartFailed")

    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=store,
        setter=setter,
        task_starter=fail_start,
    )

    view = controller.pause_after_current_file()

    assert store.current_command == command(2, pc_in_use=True)
    assert view.label == "Blocked: HelperTaskStartFailed"
    assert view.pc_in_use is True
    assert view.can_retry is True


def test_startup_starts_once_for_available_and_paused_persisted_state(tmp_path):
    starts = []
    available = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(command(), status()),
        task_starter=lambda: starts.append("available"),
    )
    paused = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(
            command(pc_in_use=True),
            status(desired_state="pc_in_use", effective_state="paused"),
        ),
        task_starter=lambda: starts.append("paused"),
    )

    available.start_if_available()
    paused.start_if_available()

    assert starts == ["available", "paused"]


@pytest.mark.parametrize("pc_in_use", [False, True])
def test_task_start_failure_preserves_command_and_allows_retry(
    tmp_path, pc_in_use
):
    store = FakeStore(
        command(pc_in_use=pc_in_use),
        status(
            desired_state="pc_in_use" if pc_in_use else "available",
            effective_state="paused" if pc_in_use else "available",
        ),
    )

    def fail_start():
        raise lan_tray.TrayError("HelperTaskStartFailed")

    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=store,
        task_starter=fail_start,
    )

    view = controller.start_if_available()

    assert store.current_command.pc_in_use is pc_in_use
    assert view.label == "Blocked: HelperTaskStartFailed"
    assert view.pc_in_use is pc_in_use
    assert view.can_retry is True


def test_paused_retry_republishes_pause_then_starts_helper(tmp_path):
    starts = []
    set_calls = []
    store = FakeStore(
        command(pc_in_use=True),
        status(desired_state="pc_in_use", effective_state="paused"),
    )

    def setter(_control_file, _status_file, _control_id, pc_in_use):
        set_calls.append(pc_in_use)
        store.current_command = command(2, pc_in_use=pc_in_use)
        store.current_status = status(
            revision=2,
            desired_state="pc_in_use",
            effective_state="pending",
            category="CoordinatorSyncPending",
        )
        return store.current_command

    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=store,
        setter=setter,
        task_starter=lambda: starts.append("start"),
    )
    controller.refresh()

    view = controller.retry_helper()

    assert starts == ["start"]
    assert set_calls == [True]
    assert store.current_command.pc_in_use is True
    assert store.current_command.revision == 2
    assert view.label == "Pause requested - waiting for helper"


@pytest.mark.parametrize(
    ("current_status", "expected_text", "expected_color"),
    [
        (status(effective_state="working"), "XPS transcoding", lan_tray.GREEN),
        (
            status(
                revision=2,
                desired_state="pc_in_use",
                effective_state="draining",
            ),
            "Pausing XPS after current file",
            lan_tray.AMBER,
        ),
        (
            status(
                revision=2,
                desired_state="pc_in_use",
                effective_state="paused",
            ),
            "XPS paused - INSPIRON fallback",
            lan_tray.BLUE,
        ),
        (
            status(effective_state="blocked", category="ControlInvalid"),
            "Blocked: ControlInvalid",
            lan_tray.RED,
        ),
    ],
)
def test_acknowledgment_states_are_user_visible(
    tmp_path, current_status, expected_text, expected_color
):
    current_command = command(
        revision=current_status.revision,
        pc_in_use=current_status.desired_state == "pc_in_use",
    )
    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(current_command, current_status),
    )

    view = controller.refresh()

    assert view.label == expected_text
    assert view.color == expected_color


@pytest.mark.parametrize(
    ("pc_in_use", "effective_state", "expected_label"),
    [
        (True, "paused", "XPS disconnected - pause pending"),
        (False, "available", "XPS disconnected - retry helper"),
        (False, "working", "XPS disconnected - retry helper"),
    ],
)
def test_dead_matching_ack_ages_out_and_enables_retry(
    tmp_path, pc_in_use, effective_state, expected_label
):
    now_ns = 2_000_000_000_000_000_000
    config = tray_config(tmp_path)
    stale_ns = now_ns - int(
        (lan_tray.STATUS_FRESHNESS_SECONDS + 1.0) * 1_000_000_000
    )
    os.utime(config.control_status_file, ns=(stale_ns, stale_ns))
    desired = "pc_in_use" if pc_in_use else "available"
    controller = lan_tray.TrayController(
        config,
        store=FakeStore(
            command(pc_in_use=pc_in_use),
            status(
                desired_state=desired,
                effective_state=effective_state,
            ),
        ),
        clock_ns=lambda: now_ns,
    )

    view = controller.refresh()

    assert view.label == expected_label
    assert view.color not in {lan_tray.BLUE, lan_tray.GREEN}
    assert view.can_retry is True


def test_materially_future_status_mtime_fails_closed(tmp_path):
    now_ns = 2_000_000_000_000_000_000
    config = tray_config(tmp_path)
    future_ns = now_ns + int(
        (lan_tray.STATUS_FUTURE_TOLERANCE_SECONDS + 1.0) * 1_000_000_000
    )
    os.utime(config.control_status_file, ns=(future_ns, future_ns))
    controller = lan_tray.TrayController(
        config,
        store=FakeStore(
            command(pc_in_use=True),
            status(desired_state="pc_in_use", effective_state="paused"),
        ),
        clock_ns=lambda: now_ns,
    )

    view = controller.refresh()

    assert view.label == "XPS disconnected - pause pending"
    assert view.color != lan_tray.BLUE
    assert view.can_retry is True


@pytest.mark.parametrize("effective_state", ["pending", "paused", "blocked"])
def test_pause_pending_paused_and_blocked_views_allow_retry(
    tmp_path, effective_state
):
    current_status = status(
        desired_state="pc_in_use",
        effective_state=effective_state,
        category=("CoordinatorSyncPending" if effective_state == "pending" else "Ready"),
    )
    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(command(pc_in_use=True), current_status),
    )

    assert controller.refresh().can_retry is True


def test_working_status_becomes_live_again_after_periodic_mtime_refresh(tmp_path):
    now_ns = 2_000_000_000_000_000_000
    config = tray_config(tmp_path)
    old_ns = now_ns - int(
        (lan_tray.STATUS_FRESHNESS_SECONDS + 1.0) * 1_000_000_000
    )
    os.utime(config.control_status_file, ns=(old_ns, old_ns))
    controller = lan_tray.TrayController(
        config,
        store=FakeStore(command(), status(effective_state="working")),
        clock_ns=lambda: now_ns,
    )

    assert controller.refresh().color == lan_tray.GRAY

    fresh_ns = now_ns - 5_000_000_000
    os.utime(config.control_status_file, ns=(fresh_ns, fresh_ns))
    refreshed = controller.refresh()

    assert refreshed.label == "XPS transcoding"
    assert refreshed.color == lan_tray.GREEN


def test_status_file_changed_during_read_is_not_trusted(tmp_path):
    config = tray_config(tmp_path)

    class MutatingStore(FakeStore):
        def read_status(self):
            Path(config.control_status_file).write_bytes(b"changed")
            return super().read_status()

    controller = lan_tray.TrayController(
        config,
        store=MutatingStore(
            command(pc_in_use=True),
            status(desired_state="pc_in_use", effective_state="paused"),
        ),
    )

    view = controller.refresh()

    assert view.label == "Blocked: StatusUnsafe"
    assert view.color == lan_tray.RED
    assert view.can_retry is True


@pytest.mark.parametrize("category", ["StatusInvalid", "StatusUnsafe"])
def test_corrupt_or_unsafe_status_is_red_blocked_with_retry(tmp_path, category):
    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(command(), HelperControlError(category)),
    )

    view = controller.refresh()

    assert view.label == f"Blocked: {category}"
    assert view.color == lan_tray.RED
    assert view.can_retry is True


def test_stale_ack_is_never_presented_as_paused(tmp_path):
    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(
            command(revision=2, pc_in_use=True),
            status(
                revision=1,
                desired_state="pc_in_use",
                effective_state="paused",
            ),
        ),
    )

    view = controller.refresh()

    assert view.color == lan_tray.AMBER
    assert view.label == "Pause requested - waiting for helper"


def test_corrupt_or_missing_command_fails_closed(tmp_path):
    controller = lan_tray.TrayController(
        tray_config(tmp_path),
        store=FakeStore(HelperControlError("ControlInvalid")),
    )

    view = controller.refresh()

    assert view.color == lan_tray.RED
    assert view.pc_in_use is True
    assert view.can_retry is False


def test_helper_task_start_is_fixed_non_elevated_and_shell_free(monkeypatch):
    observed = {}

    def runner(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    lan_tray.start_helper_task(runner)

    assert observed["arguments"] == [
        os.path.join(r"C:\Windows", "System32", "schtasks.exe"),
        "/Run",
        "/TN",
        r"\VideoTranscoder LAN Helper",
    ]
    assert observed["kwargs"]["shell"] is False
    flattened = " ".join(observed["arguments"]).lower()
    assert "runas" not in flattened
    assert "/delete" not in flattened
    assert "/end" not in flattened


def test_exit_stops_only_the_tray_ui():
    calls = []
    application = object.__new__(lan_tray.TrayApplication)
    application.stop_event = threading.Event()
    application.icon = SimpleNamespace(stop=lambda: calls.append("icon-stop"))

    application._exit()

    assert application.stop_event.is_set()
    assert calls == ["icon-stop"]


def test_self_test_is_read_only_and_writes_bounded_report(tmp_path, monkeypatch):
    config = tray_config(tmp_path)
    fake_store = FakeStore(command(), status())
    setter_calls = []
    report = tmp_path / "tray-self-test.json"

    monkeypatch.setattr(lan_tray, "load_tray_config", lambda _path: config)
    monkeypatch.setattr(
        lan_tray,
        "HelperControlStore",
        lambda *_args: fake_store,
    )
    monkeypatch.setattr(
        lan_tray,
        "set_desired_state",
        lambda *_args: setter_calls.append(_args),
    )
    monkeypatch.setattr(lan_tray, "_TRAY_AVAILABLE", True)

    result = lan_tray.main(
        ["--config", config.config_path, "--self-test-report", str(report)]
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert result == 0
    assert payload["event"] == "LanTraySelfTest"
    assert payload["status"] == "Ready"
    assert payload["status_readable"] is True
    assert payload["status_category"] == ""
    assert setter_calls == []


@pytest.mark.parametrize(
    "category",
    ["StatusMissing", "StatusUnsafe", "StatusInvalid"],
)
def test_self_test_preserves_exact_status_failure_category(
    tmp_path, monkeypatch, category
):
    config = tray_config(tmp_path)
    fake_store = FakeStore(command(), HelperControlError(category))
    report = tmp_path / f"tray-self-test-{category}.json"

    monkeypatch.setattr(lan_tray, "load_tray_config", lambda _path: config)
    monkeypatch.setattr(
        lan_tray,
        "HelperControlStore",
        lambda *_args: fake_store,
    )
    monkeypatch.setattr(lan_tray, "_TRAY_AVAILABLE", True)

    result = lan_tray.main(
        ["--config", config.config_path, "--self-test-report", str(report)]
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert result == 0
    assert payload["status"] == "Ready"
    assert payload["status_readable"] is False
    assert payload["status_category"] == category


def test_source_has_no_stop_elevation_or_token_file_access():
    source = Path(lan_tray.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "Stop-ScheduledTask",
        "Stop-Process",
        "taskkill",
        "runas",
        "ShellExecute",
        "lan-token.txt",
    ):
        assert forbidden not in source
