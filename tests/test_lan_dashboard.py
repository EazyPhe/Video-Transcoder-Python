"""Synthetic tests for the coordinator-PC-only detailed dashboard."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import lan_cli
import lan_dashboard


def _snapshot() -> dict:
    return {
        "schema_version": 1,
        "generated_utc": 1_800_000_000.0,
        "run": {
            "run_id": "opaque-run",
            "status": "running",
            "failure_category": "",
            "contract_hash": "A" * 64,
            "started_utc": 1_799_996_400.0,
            "elapsed_seconds": 3600.0,
            "eta_seconds": 1800.0,
            "scheduling_mode": "helper_preferred",
            "scheduling_state": "helper_active",
            "validation_policy": "producer_full",
            "preferred_worker_role": "helper",
            "helper_eligible": True,
            "remote_eligible": False,
            "fallback_countdown_seconds": 90.0,
            "helper_control_known": True,
            "helper_control_revision": 7,
            "helper_paused": False,
            "private_source_path": "D:/private/never-return",
            "scheduling_secret": "must-not-return",
            "helper_control_secret": "must-not-return",
        },
        "totals": {
            "total_jobs": 12,
            "pending": 4,
            "active": 2,
            "completed": 6,
            "failed": 0,
            "skipped": 1,
            "source_bytes_total": 120_000_000_000,
            "source_bytes_completed": 70_000_000_000,
        },
        "workers": [
            {
                "worker_id": "remote-qsv",
                "role": "remote",
                "label": "This PC · Intel QSV",
                "online": True,
                "phase": "encoding",
                "current_filename": "Synthetic Small.mkv",
                "source_size_bytes": 2_000_000_000,
                "media_seconds": 900.0,
                "duration_seconds": 1800.0,
                "encode_elapsed_seconds": 400.0,
                "encode_speed_ratio": 2.25,
                "encode_eta_seconds": 400.0,
                "updated_utc": 1_800_000_000.0,
                "command_line": "must-not-return",
            },
            {
                "worker_id": "helper-nvenc",
                "role": "helper",
                "label": "Helper PC · NVIDIA NVENC",
                "online": True,
                "phase": "transferring",
                "current_filename": "Synthetic Large.mkv",
                "source_size_bytes": 8_000_000_000,
                "media_seconds": 3600.0,
                "duration_seconds": 3600.0,
                "encode_elapsed_seconds": 1200.0,
                "encode_speed_ratio": 3.0,
                "encode_eta_seconds": 0.0,
                "transfer_bytes": 3_000_000_000,
                "transfer_total_bytes": 4_000_000_000,
                "transfer_elapsed_seconds": 30.0,
                "transfer_bytes_per_second": 100_000_000.0,
                "transfer_eta_seconds": 10.0,
                "updated_utc": 1_800_000_000.0,
            },
        ],
        "queue": [
            {
                "filename": "Synthetic Large.mkv",
                "size_bytes": 8_000_000_000,
                "state": "transferring",
                "worker_role": "helper",
                "progress_percent": 75.0,
                "source_path": "D:/private/never-return",
            }
        ],
        "recent": [
            {
                "timestamp_utc": 1_800_000_000.0,
                "worker_role": "helper",
                "event": "Started secure transfer",
                "filename": "Synthetic Large.mkv",
                "token": "must-not-return",
            }
        ],
        "bearer_token": "must-not-return",
        "staging_root": "D:/private/never-return",
    }


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    def dashboard_snapshot(self):
        self.calls += 1
        return _snapshot()


@pytest.fixture
def dashboard():
    provider = Provider()
    server = lan_dashboard.DashboardServer(provider=provider, port=0)
    server.start()
    try:
        yield server, provider
    finally:
        server.close()


def _get(url: str, path: str):
    request = Request(
        url.rstrip("/") + path,
        headers={"Accept": "application/json"},
    )
    return urlopen(request, timeout=3)


def test_snapshot_normalization_allows_details_but_drops_private_fields():
    normalized = lan_dashboard.normalize_dashboard_snapshot(_snapshot())
    serialized = json.dumps(normalized)

    assert normalized["workers"][0]["current_filename"] == (
        "Synthetic Small.mkv"
    )
    assert normalized["workers"][1]["transfer_bytes"] == 3_000_000_000
    assert normalized["workers"][1]["transfer_bytes_per_second"] == (
        100_000_000.0
    )
    assert normalized["workers"][1]["transfer_eta_seconds"] == 10.0
    assert normalized["queue"][0]["filename"] == "Synthetic Large.mkv"
    assert "D:/private/never-return" not in serialized
    assert "must-not-return" not in serialized
    assert "command_line" not in serialized
    assert "bearer_token" not in serialized


def test_policy_fields_are_allowlisted_and_worker_eligibility_is_derived():
    normalized = lan_dashboard.normalize_dashboard_snapshot(_snapshot())
    run = normalized["run"]

    assert run["scheduling_mode"] == "helper_preferred"
    assert run["scheduling_state"] == "helper_active"
    assert run["validation_policy"] == "producer_full"
    assert run["preferred_worker_role"] == "helper"
    assert run["helper_eligible"] is True
    assert run["remote_eligible"] is False
    assert run["fallback_countdown_seconds"] == 90.0
    assert run["helper_control_known"] is True
    assert run["helper_control_revision"] == 7
    assert run["helper_paused"] is False
    assert normalized["workers"][0]["eligible_for_new_work"] is False
    assert normalized["workers"][0]["standby_reason"] == (
        "HOT-BOX preferred for new transcodes"
    )
    assert normalized["workers"][1]["eligible_for_new_work"] is True
    assert normalized["workers"][1]["pc_in_use_paused"] is False
    assert "scheduling_secret" not in json.dumps(normalized)
    assert "helper_control_secret" not in json.dumps(normalized)


def test_pc_in_use_marks_only_idle_xps_paused_and_inspiron_eligible():
    value = _snapshot()
    value["run"].update(
        {
            "scheduling_state": "helper-paused",
            "helper_eligible": False,
            "remote_eligible": True,
            "fallback_countdown_seconds": 0.0,
            "helper_control_revision": 8,
            "helper_paused": True,
        }
    )
    value["workers"][1]["phase"] = "PcInUsePaused"
    value["workers"][1]["current_filename"] = ""

    normalized = lan_dashboard.normalize_dashboard_snapshot(value)

    remote, helper = normalized["workers"]
    assert remote["eligible_for_new_work"] is True
    assert remote["pc_in_use_paused"] is False
    assert helper["eligible_for_new_work"] is False
    assert helper["standby_reason"] == "PC in use"
    assert helper["pc_in_use_paused"] is True
    assert normalized["run"]["helper_control_revision"] == 8


def test_pc_in_use_does_not_mark_active_xps_as_idle_paused():
    value = _snapshot()
    value["run"].update(
        {
            "scheduling_state": "helper-active",
            "helper_eligible": False,
            "remote_eligible": False,
            "helper_paused": True,
        }
    )

    normalized = lan_dashboard.normalize_dashboard_snapshot(value)

    assert normalized["workers"][1]["current_filename"] == (
        "Synthetic Large.mkv"
    )
    assert normalized["workers"][1]["pc_in_use_paused"] is False


def test_legacy_snapshot_defaults_both_workers_to_eligible():
    value = _snapshot()
    for key in (
        "scheduling_mode",
        "scheduling_state",
        "validation_policy",
        "preferred_worker_role",
        "helper_eligible",
        "remote_eligible",
        "fallback_countdown_seconds",
        "helper_control_known",
        "helper_control_revision",
        "helper_paused",
    ):
        value["run"].pop(key)

    normalized = lan_dashboard.normalize_dashboard_snapshot(value)

    assert normalized["run"]["scheduling_mode"] == ""
    assert normalized["run"]["validation_policy"] == ""
    assert normalized["run"]["helper_eligible"] is True
    assert normalized["run"]["remote_eligible"] is True
    assert normalized["run"]["fallback_countdown_seconds"] == 0.0
    assert normalized["run"]["helper_control_known"] is False
    assert normalized["run"]["helper_control_revision"] == 0
    assert normalized["run"]["helper_paused"] is False
    assert all(
        worker["eligible_for_new_work"]
        for worker in normalized["workers"]
    )
    assert not any(
        worker["pc_in_use_paused"]
        for worker in normalized["workers"]
    )


def test_normalization_bounds_lists_text_numbers_and_percentages():
    value = _snapshot()
    value["workers"] = value["workers"] * 5
    value["queue"] = [
        {
            "filename": "x" * 2000,
            "progress_percent": 900,
            "size_bytes": -10,
        }
    ] * 600
    value["recent"] = value["recent"] * 200
    normalized = lan_dashboard.normalize_dashboard_snapshot(value)

    assert len(normalized["workers"]) == lan_dashboard.MAX_WORKERS
    assert len(normalized["queue"]) == lan_dashboard.MAX_QUEUE_ITEMS
    assert len(normalized["recent"]) == lan_dashboard.MAX_RECENT_ITEMS
    assert len(normalized["queue"][0]["filename"]) == (
        lan_dashboard.MAX_TEXT_LENGTH
    )
    assert normalized["queue"][0]["progress_percent"] == 100.0
    assert normalized["queue"][0]["size_bytes"] == 0


def test_server_binds_ipv4_loopback_only_and_root_is_static(dashboard):
    server, provider = dashboard

    with _get(server.url, "/") as response:
        body = response.read().decode("utf-8")

    assert server._server.server_address[0] == "127.0.0.1"
    assert server.url == f"http://127.0.0.1:{server.port}/"
    assert response.status == 200
    assert "LAN Transcode Monitor" in body
    assert "Local to this PC" in body
    assert "Network transfer" in body
    assert "Transfer speed" in body
    assert "Conversion ETA" in body
    assert "Pause auto-refresh" in body
    assert "INSPIRON Standby" in body
    assert "INSPIRON Offline" in body
    assert "ProducerFullValidation" in body
    assert "CoordinatorIntegrity" in body
    assert "PostPublishIntegrity" in body
    assert "scheduling-mode" in body
    assert "helper-control-banner" in body
    assert "HOT-BOX PC in use" in body
    assert "New HOT-BOX transcodes paused" in body
    assert "INSPIRON eligible for new work" in body
    assert "PC in use · Paused" in body
    assert "Paused while HOT-BOX PC is in use" in body
    assert "remote-eligibility" in body
    assert "INSPIRON eligible" in body
    assert "Read-only HOT-BOX helper status" in body
    assert "innerHTML" not in body
    assert "Synthetic Small.mkv" not in body
    assert provider.calls == 0


def test_snapshot_endpoint_returns_detailed_allow_list_and_security_headers(
    dashboard,
):
    server, provider = dashboard

    with _get(server.url, "/api/snapshot") as response:
        raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        headers = response.headers

    assert response.status == 200
    assert payload["workers"][0]["current_filename"] == (
        "Synthetic Small.mkv"
    )
    assert payload["workers"][1]["phase"] == "transferring"
    assert payload["workers"][1]["transfer_total_bytes"] == 4_000_000_000
    assert payload["run"]["validation_policy"] == "producer_full"
    assert payload["run"]["fallback_countdown_seconds"] == 90.0
    assert payload["workers"][0]["eligible_for_new_work"] is False
    assert payload["run"]["helper_control_known"] is True
    assert payload["run"]["helper_control_revision"] == 7
    assert payload["run"]["helper_paused"] is False
    assert payload["workers"][1]["pc_in_use_paused"] is False
    assert "must-not-return" not in raw
    assert "D:/private/never-return" not in raw
    assert headers["Cache-Control"] == "no-store, max-age=0"
    assert headers["X-Frame-Options"] == "DENY"
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers.get("Access-Control-Allow-Origin") is None
    assert provider.calls == 1


def test_non_loopback_host_and_origin_are_rejected(dashboard):
    server, _provider = dashboard
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=3,
    )
    connection.putrequest("GET", "/api/snapshot", skip_host=True)
    connection.putheader("Host", "attacker.example")
    connection.endheaders()
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()

    assert response.status == 403
    assert body == {"error": "DashboardAccessDenied"}

    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=3,
    )
    connection.request(
        "GET",
        "/api/snapshot",
        headers={"Origin": "http://attacker.example"},
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()

    assert response.status == 403
    assert body == {"error": "DashboardAccessDenied"}


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "8.8.8.8", "::1"])
def test_dashboard_rejects_wildcard_names_public_and_ipv6_hosts(host):
    with pytest.raises(lan_dashboard.DashboardError) as raised:
        lan_dashboard.DashboardServer(provider=Provider(), port=0, host=host)
    assert raised.value.category == "DashboardHostInvalid"


def test_missing_or_failed_provider_is_a_category_only_503():
    missing = lan_dashboard.DashboardServer(
        provider=SimpleNamespace(),
        port=0,
    )
    missing.start()
    try:
        with pytest.raises(HTTPError) as raised:
            _get(missing.url, "/api/snapshot")
        response = raised.value
        assert response.code == 503
        assert json.loads(response.read()) == {
            "error": "SnapshotUnavailable"
        }
    finally:
        missing.close()

    class FailedProvider:
        def dashboard_snapshot(self):
            raise RuntimeError("D:/private/do-not-return")

    failed = lan_dashboard.DashboardServer(
        provider=FailedProvider(),
        port=0,
    )
    failed.start()
    try:
        with pytest.raises(HTTPError) as raised:
            _get(failed.url, "/api/snapshot")
        response = raised.value
        body = response.read().decode("utf-8")
        assert response.code == 503
        assert body == '{"error":"SnapshotUnavailable"}'
        assert "private" not in body
    finally:
        failed.close()


def test_dashboard_is_read_only(dashboard):
    server, provider = dashboard
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=3,
    )
    connection.request(
        "POST",
        "/api/snapshot",
        body=b"{}",
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()

    assert response.status == 405
    assert body == {"error": "MethodNotAllowed"}
    assert provider.calls == 0


def _write_config(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "VideoTranscoderLanAssist.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_coordinator_config_has_separate_dashboard_port(tmp_path):
    config = {
        "schema_version": 1,
        "mode": "coordinator",
        "root": "media-root",
        "work_root": "work-root",
        "token_file": "private/token.txt",
        "api_port": 41800,
        "dashboard_host": "10.0.0.121",
        "dashboard_port": 41802,
        "helper_worker_id": "helper-nvenc",
        "helper_control_id": "a" * 32,
        "legacy_ledger_paths": [
            "legacy/completed-ledger.json",
        ],
        "consecutive_failure_limit": 3,
    }
    args = lan_cli._load_config(str(_write_config(tmp_path, config)))

    assert args.api_port == 41800
    assert args.dashboard_host == "10.0.0.121"
    assert args.dashboard_port == 41802
    assert args.legacy_ledger_paths == [
        str(
            (
                tmp_path
                / "legacy"
                / "completed-ledger.json"
            ).resolve()
        )
    ]
    assert args.consecutive_failure_limit == 3

    config["dashboard_port"] = 41800
    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, config)))
    assert raised.value.category == "ConfigInvalid"


def test_legacy_ledger_path_list_is_bounded_and_unique(tmp_path):
    base = {
        "schema_version": 1,
        "mode": "coordinator",
        "root": "media-root",
        "work_root": "work-root",
        "token_file": "private/token.txt",
        "api_port": 41800,
        "helper_worker_id": "helper-nvenc",
        "helper_control_id": "a" * 32,
    }
    too_many = {
        **base,
        "legacy_ledger_paths": [
            f"legacy/ledger-{index}.json"
            for index in range(lan_cli.MAX_LEGACY_LEDGER_PATHS + 1)
        ],
    }
    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, too_many)))
    assert raised.value.category == "ConfigInvalid"

    duplicates = {
        **base,
        "legacy_ledger_paths": [
            "legacy/completed-ledger.json",
            "legacy/../legacy/completed-ledger.json",
        ],
    }
    with pytest.raises(lan_cli.CliError) as raised:
        lan_cli._load_config(str(_write_config(tmp_path, duplicates)))
    assert raised.value.category == "ConfigInvalid"


def test_coordinator_wiring_starts_and_closes_personal_dashboard(monkeypatch):
    observed = {}

    class StopEvent:
        def is_set(self):
            return False

        def wait(self, _timeout):
            return False

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
                "completed": 1,
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

    class FakeDashboard:
        def __init__(self, **kwargs):
            observed["dashboard_kwargs"] = kwargs

        def start(self):
            observed["dashboard_started"] = True

        def close(self):
            observed["dashboard_closed"] = True

    monkeypatch.setattr(
        lan_cli,
        "_resolve_media_tools",
        lambda *_args: ("trusted-ffmpeg", "trusted-ffprobe"),
    )
    monkeypatch.setattr(lan_cli, "DistributedCoordinator", FakeCoordinator)
    monkeypatch.setattr(lan_cli, "CoordinatorService", FakeService)
    monkeypatch.setattr(lan_cli, "DashboardServer", FakeDashboard)
    args = SimpleNamespace(
        root="media-root",
        work_root="work-root",
        token_file="token-file",
        api_port=41800,
        dashboard_port=41802,
        dashboard_host="10.0.0.121",
        ffmpeg=None,
        ffprobe=None,
        remote_worker_id="remote-qsv",
        helper_worker_id="helper-nvenc",
        helper_control_id="a" * 32,
        keep_alive_when_complete=False,
        reserve_gib=10,
        lease_seconds=45.0,
        helper_presence_seconds=30.0,
        status_interval=2.0,
        status_file=None,
        accepted_legacy_settings_hash=[],
        legacy_ledger_paths=["legacy-ledger"],
        consecutive_failure_limit=3,
    )

    assert lan_cli._run_coordinator(args) == 0
    assert observed["dashboard_kwargs"]["port"] == 41802
    assert observed["dashboard_kwargs"]["host"] == "10.0.0.121"
    assert isinstance(
        observed["dashboard_kwargs"]["provider"],
        FakeCoordinator,
    )
    assert observed["coordinator_kwargs"]["legacy_ledger_paths"] == (
        "legacy-ledger",
    )
    assert observed["coordinator_kwargs"]["consecutive_failure_limit"] == 3
    assert observed["dashboard_started"]
    assert observed["dashboard_closed"]
    assert observed["service_started"]
    assert observed["service_closed"]
    assert observed["coordinator_closed"]
