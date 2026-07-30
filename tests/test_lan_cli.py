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
    }


def _coordinator_config() -> dict:
    return {
        "schema_version": 1,
        "mode": "coordinator",
        "root": "media-root",
        "work_root": "work-root",
        "token_file": "private/token.txt",
        "api_port": 41800,
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
    assert observed["service_kwargs"]["api_port"] == 41800
    assert observed["service_started"]
    assert observed["service_closed"]
    assert observed["coordinator_closed"]


def test_helper_wiring_uses_nvenc_ssh_tunnel_and_frozen_default(
    monkeypatch,
    capsys,
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
        status_file=None,
    )

    result = lan_cli._run_helper(args)

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert observed["tunnel"] == {
        "destination": "remote-alias",
        "local_port": 41801,
        "remote_port": 41800,
        "ssh_executable": "ssh",
    }
    assert observed["helper"]["worker_role"] is WorkerRole.HELPER
    assert observed["helper"]["encoder"] == "hevc_nvenc"
    assert observed["helper"]["direct_staging"] is False
    assert observed["fallback"]["root"] == "visible-executable-folder"
    assert observed["fallback"]["encoder"] == "hevc_nvenc"
    assert observed["supervisor_stopped"]
    assert output == {
        "Category": "Committed",
        "EncodeSeconds": 67.5,
        "Event": "HelperStatus",
        "Kind": "Success",
        "SourceSizeBytes": 12345,
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
