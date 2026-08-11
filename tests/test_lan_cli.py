"""Tests for the external-config LAN-assist entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import lan_cli
from lan_protocol import WorkerRole
from lan_worker import WorkerResult


def _write_config(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "VideoTranscoderLanAssist.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _helper_config() -> dict:
    return {
        "schema_version": 1,
        "mode": "helper",
        "ssh_destination": "remote-alias",
        "local_port": 41801,
        "remote_port": 41800,
        "token_file": "private/token.txt",
        "staging_root": "staging-share",
        "cache_root": "local-cache",
        "control_file": "helper-control.json",
        "control_status_file": "helper-control-status.json",
        "control_id": "a" * 32,
    }


def _coordinator_config() -> dict:
    return {
        "schema_version": 1,
        "mode": "coordinator",
        "root": "media-root",
        "work_root": "work-root",
        "token_file": "private/token.txt",
        "api_port": 41800,
        "helper_worker_id": "helper-nvenc",
        "helper_control_id": "a" * 32,
    }


def test_no_arguments_uses_external_config_beside_executable(
    tmp_path,
    monkeypatch,
):
    config_path = _write_config(tmp_path, _helper_config())
    observed = {}
    monkeypatch.setattr(
        lan_cli,
        "default_config_path",
        lambda: str(config_path),
    )
    monkeypatch.setattr(
        lan_cli,
        "_run_helper",
        lambda args: observed.update(vars(args)) or 17,
    )

    assert lan_cli.main([]) == 17
    assert observed["mode"] == "helper"
    assert observed["ssh_destination"] == "remote-alias"
    assert observed["local_port"] == 41801


def test_explicit_config_routes_coordinator(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, _coordinator_config())
    observed = {}
    monkeypatch.setattr(
        lan_cli,
        "_run_coordinator",
        lambda args: observed.update(vars(args)) or 23,
    )

    result = lan_cli.main(["--config", str(config_path)])

    assert result == 23
    assert observed["mode"] == "coordinator"
    assert observed["api_port"] == 41800
    assert observed["root"] == str((tmp_path / "media-root").resolve())
    assert observed["work_root"] == str((tmp_path / "work-root").resolve())


def test_coordinator_config_parses_prefer_helper_producer_full_policy(
    tmp_path,
):
    config = _coordinator_config()
    config.update(
        {
            "scheduling_mode": "prefer-helper",
            "helper_startup_grace_seconds": 60,
            "helper_fallback_after_seconds": 90,
            "helper_recovery_stable_seconds": 15,
            "validation_policy": "producer-full",
        }
    )

    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.scheduling_mode == "prefer-helper"
    assert args.helper_startup_grace_seconds == 60.0
    assert args.helper_fallback_after_seconds == 90.0
    assert args.helper_recovery_stable_seconds == 15.0
    assert args.validation_policy == "producer-full"


def test_coordinator_config_parses_helper_control_and_keep_alive(tmp_path):
    config = _coordinator_config()
    config["keep_alive_when_complete"] = True

    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.helper_worker_id == "helper-nvenc"
    assert args.helper_control_id == "a" * 32
    assert args.keep_alive_when_complete is True


def test_coordinator_config_keep_alive_default_is_disabled(
    tmp_path,
):
    args = lan_cli._load_config(
        str(_write_config(tmp_path, _coordinator_config()))
    )

    assert args.keep_alive_when_complete is False


@pytest.mark.parametrize(
    "updates",
    [
        {"keep_alive_when_complete": 1},
        {"keep_alive_when_complete": "true"},
        {"keep_alive_when_complete": None},
        {"helper_worker_id": ""},
        {"helper_worker_id": "x" * 129},
        {"helper_control_id": "A" * 32},
        {"helper_control_id": "a" * 31},
    ],
)
def test_coordinator_config_rejects_invalid_control_or_keep_alive(
    tmp_path, updates
):
    config = _coordinator_config()
    config.update(updates)

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"


@pytest.mark.parametrize(
    "key,value",
    [
        ("scheduling_mode", "fastest-wins"),
        ("validation_policy", "trust-me"),
        ("helper_startup_grace_seconds", 0),
        ("helper_fallback_after_seconds", -1),
        ("helper_recovery_stable_seconds", float("inf")),
    ],
)
def test_coordinator_config_rejects_invalid_policy(key, value, tmp_path):
    config = _coordinator_config()
    config[key] = value

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"


def test_config_failure_is_category_only(tmp_path, capsys):
    sensitive_path = (
        tmp_path / "private" / "do-not-display" / "media-name.json"
    )

    result = lan_cli.main(["--config", str(sensitive_path)])

    output = capsys.readouterr()
    assert result == 2
    assert output.err == ""
    assert str(sensitive_path) not in output.out
    assert json.loads(output.out) == {
        "Event": "ServiceStopped",
        "FailureCategory": "ConfigInvalid",
        "Status": "Failed",
    }


def test_resume_source_busy_has_exact_launcher_contract(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = _write_config(tmp_path, _coordinator_config())
    monkeypatch.setattr(
        lan_cli,
        "_run_coordinator",
        lambda _args: (_ for _ in ()).throw(
            lan_cli.CoordinatorError("ResumeSourceBusy", systemic=True)
        ),
    )

    result = lan_cli.main(["--config", str(config_path)])

    output = capsys.readouterr()
    assert result == 1
    assert output.err == ""
    assert str(tmp_path) not in output.out
    assert json.loads(output.out) == {
        "Event": "ServiceStopped",
        "FailureCategory": "ResumeSourceBusy",
        "Status": "Failed",
    }


def test_config_rejects_unknown_secret_field_and_duplicate_keys(tmp_path):
    config = _helper_config()
    config["bearer_token"] = "must-not-be-embedded"
    unknown = _write_config(tmp_path, config)

    try:
        lan_cli._load_config(str(unknown))
    except lan_cli.CliError as exc:
        assert exc.category == "ConfigInvalid"
    else:
        raise AssertionError("unknown configuration key was accepted")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"mode":"helper","mode":"coordinator"}',
        encoding="utf-8",
    )
    try:
        lan_cli._load_config(str(duplicate))
    except lan_cli.CliError as exc:
        assert exc.category == "ConfigInvalid"
    else:
        raise AssertionError("duplicate configuration key was accepted")


def test_helper_config_parses_exponential_reconnect_backoff(tmp_path):
    config = _helper_config()
    config.update(
        {
            "reconnect_initial_seconds": 3,
            "reconnect_max_seconds": 45,
            "reconnect_multiplier": 2.5,
        }
    )

    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.reconnect_seconds == 3.0
    assert args.reconnect_initial_seconds == 3.0
    assert args.reconnect_max_seconds == 45.0
    assert args.reconnect_multiplier == 2.5


def test_helper_config_uses_travel_safe_reconnect_defaults(tmp_path):
    args = lan_cli._load_config(
        str(_write_config(tmp_path, _helper_config()))
    )

    assert args.reconnect_initial_seconds == 30.0
    assert args.reconnect_max_seconds == 600.0
    assert args.reconnect_multiplier == 4.0
    assert args.event_log_file is None


def test_helper_config_parses_local_event_log_with_bounded_defaults(tmp_path):
    config = _helper_config()
    config["status_file"] = "helper-status.json"
    config["event_log_file"] = "helper-events.jsonl"

    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.event_log_file == str(
        (tmp_path / "helper-events.jsonl").resolve()
    )
    assert (
        args.event_log_max_bytes
        == lan_cli.DEFAULT_HELPER_EVENT_LOG_MAX_BYTES
    )
    assert (
        args.event_log_backup_count
        == lan_cli.DEFAULT_HELPER_EVENT_LOG_BACKUP_COUNT
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"event_log_max_bytes": 4096},
        {"event_log_backup_count": 2},
        {"event_log_file": "events.jsonl", "event_log_max_bytes": 4095},
        {
            "event_log_file": "events.jsonl",
            "event_log_max_bytes": 64 * 1024 * 1024 + 1,
        },
        {"event_log_file": "events.jsonl", "event_log_backup_count": 21},
        {"event_log_file": "private/token.txt"},
        {"event_log_file": "VideoTranscoderLanAssist.json"},
        {"event_log_file": "another-local-file.jsonl"},
    ],
)
def test_helper_config_rejects_unsafe_event_log_settings(tmp_path, updates):
    config = _helper_config()
    config.update(updates)

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"


def test_helper_config_rejects_network_event_log(tmp_path, monkeypatch):
    config = _helper_config()
    config["event_log_file"] = "helper-events.jsonl"
    monkeypatch.setattr(lan_cli, "_windows_path_is_network", lambda _path: True)

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"


def test_helper_config_rejects_existing_event_log_hardlink(tmp_path):
    sensitive = tmp_path / "sensitive-local-file.txt"
    event_log = tmp_path / "helper-events.jsonl"
    sensitive.write_text("must remain unchanged", encoding="utf-8")
    try:
        os.link(sensitive, event_log)
    except OSError:
        pytest.skip("Hard-link creation is unavailable")
    config = _helper_config()
    config["event_log_file"] = event_log.name

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"
    assert sensitive.read_text(encoding="utf-8") == "must remain unchanged"


def test_legacy_reconnect_seconds_remains_supported(tmp_path):
    config = _helper_config()
    config["reconnect_seconds"] = 7

    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.reconnect_initial_seconds == 7.0
    assert args.reconnect_max_seconds == 600.0
    assert args.reconnect_multiplier == 4.0


@pytest.mark.parametrize(
    "updates",
    [
        {
            "reconnect_seconds": 5,
            "reconnect_initial_seconds": 5,
        },
        {
            "reconnect_initial_seconds": 10,
            "reconnect_max_seconds": 5,
        },
        {"reconnect_multiplier": 1},
    ],
)
def test_helper_config_rejects_invalid_reconnect_backoff(
    tmp_path, updates
):
    config = _helper_config()
    config.update(updates)

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert raised.value.category == "ConfigInvalid"


def test_token_creation_uses_config_is_exclusive_and_hides_secret(
    tmp_path,
    capsys,
):
    config_path = _write_config(tmp_path, _helper_config())
    token_file = tmp_path / "private" / "token.txt"

    assert (
        lan_cli.main(
            [
                "--config",
                str(config_path),
                "--create-token",
            ]
        )
        == 0
    )
    first_output = capsys.readouterr().out
    raw = token_file.read_bytes()
    token = raw.rstrip(b"\n")

    assert raw.endswith(b"\n")
    assert 32 <= len(token) <= 512
    assert all(0x21 <= byte <= 0x7E for byte in token)
    assert token.decode("ascii") not in first_output
    assert str(token_file) not in first_output
    assert json.loads(first_output) == {
        "Event": "TokenCreated",
        "Status": "Ready",
    }
    if os.name == "nt":
        assert lan_cli._windows_token_acl_is_private(
            str(token_file),
            lan_cli._windows_current_user_sid(),
        )

    assert (
        lan_cli.main(
            [
                "--config",
                str(config_path),
                "--create-token",
            ]
        )
        == 1
    )
    second_output = capsys.readouterr().out
    assert str(token_file) not in second_output
    assert json.loads(second_output)["FailureCategory"] == "TokenFileExists"


def test_token_acl_failure_removes_newly_created_secret(
    tmp_path,
    monkeypatch,
):
    token_file = tmp_path / "private" / "token.txt"

    def fail_acl(_path):
        raise lan_cli.CliError("TokenAclFailed")

    monkeypatch.setattr(lan_cli, "_secure_token_permissions", fail_acl)

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._create_token_file(str(token_file))

    assert raised.value.category == "TokenAclFailed"
    assert not token_file.exists()


def test_token_creation_rejects_network_storage_before_creating_file(
    tmp_path,
    monkeypatch,
):
    token_file = tmp_path / "token.txt"
    monkeypatch.setattr(
        lan_cli,
        "_windows_path_is_network",
        lambda _path: True,
    )

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._create_token_file(str(token_file))

    assert raised.value.category == "TokenPathNetworkRejected"
    assert not token_file.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC semantics")
def test_unc_token_path_is_rejected_without_network_access():
    assert lan_cli._windows_path_is_network(
        r"\\synthetic.invalid\share\lan-token.txt"
    )


def test_media_tools_resolve_explicit_pair(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    assert lan_cli._resolve_media_tools(
        str(ffmpeg),
        str(ffprobe),
    ) == (str(ffmpeg.resolve()), str(ffprobe.resolve()))


def test_validate_config_is_nondestructive_and_aggregate_only(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = _write_config(tmp_path, _helper_config())
    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(
        lan_cli,
        "_run_helper",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("service was started")
        ),
    )

    result = lan_cli.main(
        ["--config", str(config_path), "--validate-config"]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "Event": "ConfigValidated",
        "Mode": "helper",
        "Status": "Ready",
    }


def test_coordinator_wiring_emits_only_aggregate_state(monkeypatch, capsys):
    observed = {}

    class StopEvent:
        def is_set(self):
            return False

        def wait(self, _timeout):
            return False

    class FakeCoordinator:
        def __init__(self, **kwargs):
            observed["coordinator_kwargs"] = kwargs
            self._snapshots = [
                {
                    "Status": "Running",
                    "FailureCategory": "",
                    "HelperControlKnown": True,
                    "HelperControlRevision": 7,
                    "HelperPaused": False,
                    "pending": 1,
                    "leased": 0,
                    "suspect": 0,
                    "committing": 0,
                    "completed": 0,
                    "failed": 0,
                    "private_path": "D:/private/never-emit.mkv",
                },
                {
                    "Status": "Running",
                    "FailureCategory": "",
                    "HelperControlKnown": True,
                    "HelperControlRevision": 7,
                    "HelperPaused": False,
                    "pending": 0,
                    "leased": 0,
                    "suspect": 0,
                    "committing": 0,
                    "completed": 1,
                    "failed": 0,
                    "private_path": "D:/private/never-emit.mkv",
                },
            ]

        def snapshot(self):
            return self._snapshots.pop(0)

        def close(self):
            observed["coordinator_closed"] = True

    class FakeService:
        def __init__(self, **kwargs):
            observed["service_kwargs"] = kwargs
            self.stop_event = StopEvent()

        def start(self):
            observed["service_started"] = True

        def close(self):
            observed["service_closed"] = True

    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(lan_cli, "DistributedCoordinator", FakeCoordinator)
    monkeypatch.setattr(lan_cli, "CoordinatorService", FakeService)
    args = SimpleNamespace(
        root="media-root",
        work_root="work-root",
        token_file="token-file",
        api_port=41800,
        ffmpeg=None,
        ffprobe=None,
        remote_worker_id="remote-qsv",
        reserve_gib=10,
        lease_seconds=45.0,
        helper_presence_seconds=30.0,
        helper_worker_id="helper-nvenc",
        helper_control_id="a" * 32,
        keep_alive_when_complete=False,
        status_interval=2.0,
        status_file=None,
        accepted_legacy_settings_hash=["A" * 64],
    )

    result = lan_cli._run_coordinator(args)

    output = capsys.readouterr().out
    assert result == 0
    assert "D:/private/never-emit.mkv" not in output
    lines = [json.loads(line) for line in output.splitlines()]
    assert [line["pending"] for line in lines] == [1, 0]
    assert lines[-1]["completed"] == 1
    assert observed["coordinator_kwargs"]["reserve_bytes"] == 10 * 1024**3
    assert observed["coordinator_kwargs"]["scheduling_mode"] == "balanced"
    assert observed["coordinator_kwargs"]["validation_policy"] == (
        "redundant-full"
    )
    assert observed["coordinator_kwargs"]["helper_worker_id"] == (
        "helper-nvenc"
    )
    assert observed["coordinator_kwargs"]["helper_control_id"] == "a" * 32
    assert lines[0]["HelperControlKnown"] is True
    assert lines[0]["HelperControlRevision"] == 7
    assert lines[0]["HelperPaused"] is False
    assert observed["service_kwargs"]["api_port"] == 41800
    assert observed["service_kwargs"]["keep_alive_when_complete"] is False
    assert observed["service_started"]
    assert observed["service_closed"]
    assert observed["coordinator_closed"]


def test_keep_alive_when_complete_keeps_service_loop_running(
    monkeypatch, capsys
):
    observed = {}

    class StopEvent:
        def is_set(self):
            return True

        def wait(self, _timeout):
            raise AssertionError("already-stopped service must not wait")

    class FakeCoordinator:
        def __init__(self, **kwargs):
            observed["coordinator_kwargs"] = kwargs

        def snapshot(self):
            return {
                "Status": "Running",
                "FailureCategory": "",
                "pending": 0,
                "leased": 0,
                "suspect": 0,
                "committing": 0,
                "completed": 4,
                "failed": 0,
            }

        def close(self):
            observed["coordinator_closed"] = True

    class FakeService:
        def __init__(self, **kwargs):
            observed["service_kwargs"] = kwargs
            self.stop_event = StopEvent()

        def start(self):
            observed["service_started"] = True

        def close(self):
            observed["service_closed"] = True

    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(lan_cli, "DistributedCoordinator", FakeCoordinator)
    monkeypatch.setattr(lan_cli, "CoordinatorService", FakeService)
    args = SimpleNamespace(
        root="media-root",
        work_root="work-root",
        token_file="token-file",
        api_port=41800,
        ffmpeg=None,
        ffprobe=None,
        remote_worker_id="remote-qsv",
        reserve_gib=10,
        lease_seconds=45.0,
        helper_presence_seconds=30.0,
        helper_worker_id="helper-nvenc",
        helper_control_id="a" * 32,
        keep_alive_when_complete=True,
        status_interval=2.0,
        status_file=None,
        accepted_legacy_settings_hash=[],
    )

    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._run_coordinator(args)
    capsys.readouterr()

    assert raised.value.category == "CoordinatorServiceStopped"
    assert observed["service_started"]
    assert observed["service_kwargs"]["keep_alive_when_complete"] is True
    assert observed["service_closed"]
    assert observed["coordinator_closed"]


def test_aggregate_snapshot_allows_only_public_helper_control_fields():
    payload = lan_cli._aggregate_snapshot(
        {
            "HelperControlKnown": True,
            "HelperControlRevision": 9,
            "HelperPaused": True,
            "private_path": r"D:\private\movie.mkv",
        }
    )

    assert payload["Event"] == "CoordinatorStatus"
    assert payload["HelperControlKnown"] is True
    assert payload["HelperControlRevision"] == 9
    assert payload["HelperPaused"] is True
    assert "private_path" not in payload


@pytest.mark.parametrize("status_name", [None, "status/helper-status.json"])
def test_helper_wiring_uses_nvenc_ssh_tunnel_and_frozen_default(
    monkeypatch,
    capsys,
    tmp_path,
    status_name,
):
    observed = {}

    def factory(name):
        def create(**kwargs):
            observed[name] = kwargs
            return SimpleNamespace(kind=name)

        return create

    class FakeSupervisor:
        def __init__(self, **kwargs):
            observed["supervisor"] = kwargs

        def run(self):
            observed["supervisor"]["status_callback"](
                WorkerResult(
                    "Success",
                    "Committed",
                    source_size_bytes=12345,
                    encode_seconds=67.5,
                )
            )

        def stop(self):
            observed["supervisor_stopped"] = True

    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(
        lan_cli,
        "LoopbackJsonClient",
        factory("transport"),
    )
    monkeypatch.setattr(
        lan_cli,
        "HttpCoordinatorClient",
        lambda transport: SimpleNamespace(transport=transport),
    )
    monkeypatch.setattr(lan_cli, "SshLocalForward", factory("tunnel"))
    monkeypatch.setattr(lan_cli, "ComputeWorker", factory("helper"))
    monkeypatch.setattr(lan_cli, "LocalFallbackWorker", factory("fallback"))
    monkeypatch.setattr(lan_cli, "LanAssistSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        lan_cli,
        "default_fallback_root",
        lambda: "visible-executable-folder",
    )
    status_file = None if status_name is None else str(tmp_path / status_name)
    args = SimpleNamespace(
        ffmpeg=None,
        ffprobe=None,
        token_file="token-file",
        local_port=41801,
        remote_port=41800,
        ssh_destination="remote-alias",
        ssh_executable="ssh",
        worker_id="helper-nvenc",
        staging_root="staging-share",
        cache_root="local-cache",
        fallback_root=None,
        reconnect_seconds=5.0,
        status_file=status_file,
        event_log_file=str(tmp_path / "helper-events.jsonl"),
        event_log_max_bytes=1024 * 1024,
        event_log_backup_count=2,
        control_file=str(tmp_path / "helper-control.json"),
        control_status_file=str(tmp_path / "helper-control-status.json"),
        control_id="a" * 32,
    )

    result = lan_cli._run_helper(args)

    output = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
    ]
    assert result == 0
    assert observed["tunnel"] == {
        "destination": "remote-alias",
        "local_port": 41801,
        "remote_port": 41800,
        "ssh_executable": "ssh",
        "ownership_record_path": (
            None
            if status_file is None
            else str(Path(status_file).with_name("lan-tunnel-owner.json"))
        ),
    }
    assert observed["helper"]["worker_role"] is WorkerRole.HELPER
    assert observed["helper"]["encoder"] == "hevc_nvenc"
    assert observed["helper"]["direct_staging"] is False
    assert observed["fallback"]["root"] == "visible-executable-folder"
    assert observed["fallback"]["encoder"] == "hevc_nvenc"
    assert observed["supervisor"]["reconnect_initial_seconds"] == 5.0
    assert observed["supervisor"]["reconnect_max_seconds"] == 600.0
    assert observed["supervisor"]["reconnect_multiplier"] == 4.0
    assert observed["supervisor"]["helper_worker_id"] == "helper-nvenc"
    control_store = observed["supervisor"]["control_store"]
    assert control_store.control_file == str(
        tmp_path / "helper-control.json"
    )
    assert control_store.status_file == str(
        tmp_path / "helper-control-status.json"
    )
    assert control_store.control_id == "a" * 32
    assert observed["helper"]["control_store"] is control_store
    assert observed["fallback"]["control_store"] is control_store
    assert observed["supervisor_stopped"]
    event_log_text = (tmp_path / "helper-events.jsonl").read_text(
        encoding="utf-8"
    )
    event_records = [
        json.loads(line) for line in event_log_text.splitlines()
    ]
    assert event_records[0]["Kind"] == "Starting"
    assert event_records[0]["Category"] == "HelperProcessStarted"
    assert output[0] == {
        "Category": "HelperProcessStarted",
        "EncodeSeconds": 0.0,
        "Event": "HelperStatus",
        "Kind": "Starting",
        "Phase": "",
        "RetryCount": 0,
        "SourceSizeBytes": 0,
        "TransportCategory": "",
        "WinError": 0,
    }
    assert output[1] == {
        "Category": "Committed",
        "EncodeSeconds": 67.5,
        "Event": "HelperStatus",
        "Kind": "Success",
        "Phase": "",
        "RetryCount": 0,
        "SourceSizeBytes": 12345,
        "TransportCategory": "",
        "WinError": 0,
    }


def test_status_file_contains_aggregate_only(tmp_path):
    status_file = tmp_path / "status" / "helper.json"
    lan_cli._emit(
        {
            "Event": "HelperStatus",
            "Kind": "Idle",
            "Category": "NoPendingJob",
        },
        str(status_file),
    )

    assert json.loads(status_file.read_text(encoding="utf-8")) == {
        "Category": "NoPendingJob",
        "Event": "HelperStatus",
        "Kind": "Idle",
    }


def test_helper_event_log_is_path_free_deduplicated_and_rotated(
    tmp_path, capsys
):
    event_log = tmp_path / "logs" / "helper-events.jsonl"
    sink = lan_cli._HelperStatusSink(
        status_file=None,
        event_log_file=str(event_log),
        event_log_max_bytes=420,
        event_log_backup_count=2,
    )
    idle = WorkerResult("Idle", "NoPendingJob")
    recovered = WorkerResult(
        "Success",
        "Committed",
        source_size_bytes=1234,
        encode_seconds=5.5,
        phase="UploadExclusiveFlush",
        winerror=32,
        retry_count=2,
        transport_category="request_conflict",
    )

    sink.record(idle)
    sink.record(idle)
    for _ in range(5):
        sink.record(recovered)
    capsys.readouterr()

    retained = sorted(event_log.parent.glob("helper-events.jsonl*"))
    assert {path.name for path in retained} <= {
        "helper-events.jsonl",
        "helper-events.jsonl.1",
        "helper-events.jsonl.2",
    }
    assert event_log.is_file()
    records = []
    for path in retained:
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    assert records
    assert all(set(record) == {
        "Category",
        "EncodeSeconds",
        "Event",
        "Kind",
        "LoggedUtc",
        "Phase",
        "RetryCount",
        "SourceSizeBytes",
        "TransportCategory",
        "WinError",
    } for record in records)
    assert any(record["WinError"] == 32 for record in records)
    assert any(
        record["TransportCategory"] == "request_conflict"
        for record in records
    )
    assert "playa" not in "\n".join(
        path.read_text(encoding="utf-8") for path in retained
    )


def test_helper_event_log_suppresses_repeated_idle_only(tmp_path, capsys):
    event_log = tmp_path / "helper-events.jsonl"
    sink = lan_cli._HelperStatusSink(
        status_file=None,
        event_log_file=str(event_log),
        event_log_max_bytes=1024 * 1024,
        event_log_backup_count=2,
    )

    sink.record(WorkerResult("Idle", "NoPendingJob"))
    sink.record(WorkerResult("Idle", "NoPendingJob"))
    sink.record(WorkerResult("Success", "Committed"))
    sink.record(WorkerResult("Success", "Committed"))
    capsys.readouterr()

    records = [
        json.loads(line)
        for line in event_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["Kind"] for record in records] == [
        "Idle",
        "Success",
        "Success",
    ]


def test_helper_event_log_failure_updates_latest_status(
    tmp_path, monkeypatch, capsys
):
    status_file = tmp_path / "helper-status.json"
    sink = lan_cli._HelperStatusSink(
        status_file=str(status_file),
        event_log_file=str(tmp_path / "helper-events.jsonl"),
        event_log_max_bytes=4096,
        event_log_backup_count=2,
    )
    monkeypatch.setattr(
        lan_cli,
        "_append_helper_event_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            lan_cli.CliError("HelperEventLogWriteFailed")
        ),
    )

    with pytest.raises(lan_cli.CliError) as raised:
        sink.record(WorkerResult("Success", "Committed"))
    capsys.readouterr()

    assert raised.value.category == "HelperEventLogWriteFailed"
    assert json.loads(status_file.read_text(encoding="utf-8"))[
        "Category"
    ] == "HelperEventLogWriteFailed"


def test_helper_status_rejects_path_like_diagnostic_values():
    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._worker_status(
            WorkerResult("Blocked", r"C:\private\media-title.mkv")
        )

    assert raised.value.category == "HelperStatusInvalid"


def test_helper_status_rejects_non_allowlisted_transport_category():
    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._worker_status(
            WorkerResult(
                "Disconnected",
                "CoordinatorDisconnected",
                transport_category=r"C:\private\transport-error.txt",
            )
        )

    assert raised.value.category == "HelperStatusInvalid"


@pytest.mark.parametrize("category", ["AuthBlocked", "StagingAccessBlocked"])
def test_helper_terminal_block_returns_nonzero(
    monkeypatch, capsys, tmp_path, category
):
    class FakeSupervisor:
        def __init__(self, **kwargs):
            self.status_callback = kwargs["status_callback"]
            self.terminal_result = None

        def run(self):
            self.terminal_result = WorkerResult(
                "Blocked",
                category,
                phase="UploadExclusiveFlush",
                winerror=32,
                retry_count=3,
            )
            self.status_callback(self.terminal_result)

        def stop(self):
            return None

    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(lan_cli, "LoopbackJsonClient", lambda **_kwargs: object())
    monkeypatch.setattr(lan_cli, "HttpCoordinatorClient", lambda _value: object())
    monkeypatch.setattr(lan_cli, "SshLocalForward", lambda **_kwargs: object())
    monkeypatch.setattr(lan_cli, "ComputeWorker", lambda **_kwargs: object())
    monkeypatch.setattr(lan_cli, "LocalFallbackWorker", lambda **_kwargs: object())
    monkeypatch.setattr(lan_cli, "LanAssistSupervisor", FakeSupervisor)
    args = SimpleNamespace(
        ffmpeg=None,
        ffprobe=None,
        token_file="token-file",
        local_port=41801,
        remote_port=41800,
        ssh_destination="remote-alias",
        ssh_executable="ssh",
        worker_id="helper-nvenc",
        staging_root="staging-share",
        cache_root="local-cache",
        fallback_root="fallback",
        reconnect_seconds=5.0,
        status_file=None,
        event_log_file=None,
        control_file=str((tmp_path / "helper-control.json").resolve()),
        control_status_file=str(
            (tmp_path / "helper-control-status.json").resolve()
        ),
        control_id="a" * 32,
    )

    assert lan_cli._run_helper(args) == 1
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1]["Category"] == category
    assert output[-1]["WinError"] == 32
    assert output[-1]["RetryCount"] == 3


def test_main_portable_spec_remains_gui_and_lan_has_separate_name():
    project_root = Path(lan_cli.__file__).resolve().parents[1]
    main_spec = (
        project_root
        / "packaging"
        / "portable"
        / "VideoTranscoderPortable.spec"
    ).read_text(encoding="utf-8")
    lan_spec = (
        project_root
        / "packaging"
        / "portable"
        / "VideoTranscoderLanAssist.spec"
    ).read_text(encoding="utf-8")

    assert 'src" / "gui.py"' in main_spec
    assert 'name="VideoTranscoderPortable"' in main_spec
    assert "lan_cli.py" not in main_spec
    assert 'src" / "lan_cli.py"' in lan_spec
    assert 'name="VideoTranscoderLanAssist"' in lan_spec
