"""Synthetic tests for the loopback-only coordinator transport."""

from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest

import lan_transport
from lan_transport import (
    DispatchRejected,
    LoopbackJsonClient,
    LoopbackJsonServer,
    PublicError,
    SshLocalForward,
    TransportError,
    build_ssh_local_forward_command,
    validate_loopback_host,
)


TOKEN = "test-only-token-" + ("a" * 32)


def _token_file(tmp_path: Path, value: str = TOKEN) -> Path:
    path = tmp_path / "bearer-token.txt"
    path.write_text(value + "\n", encoding="ascii")
    return path


def _client(
    server: LoopbackJsonServer,
    token_file: Path,
    **kwargs: object,
) -> LoopbackJsonClient:
    return LoopbackJsonClient(
        base_url=f"http://127.0.0.1:{server.port}",
        token_file=token_file,
        **kwargs,
    )


def _raw_post(
    server: LoopbackJsonServer,
    *,
    body: bytes,
    token: str = TOKEN,
    path: str = "/v1/claim",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=2,
    )
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    request_headers.update(headers or {})
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers=request_headers,
        )
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload)
    finally:
        connection.close()


def test_authenticated_dispatch_round_trip(tmp_path):
    token_file = _token_file(tmp_path)
    observed = []

    def dispatch(endpoint, request):
        observed.append((endpoint, request))
        return {"pending": 7, "helper_online": True}

    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=dispatch,
    ) as server:
        result = _client(server, token_file).call(
            "snapshot",
            {"worker_id": "opaque-worker"},
        )

    assert result == {"pending": 7, "helper_online": True}
    assert observed == [
        ("snapshot", {"worker_id": "opaque-worker"})
    ]


def test_wrong_token_is_category_only_and_never_dispatched(tmp_path):
    token_file = _token_file(tmp_path)
    wrong_file = tmp_path / "wrong-token.txt"
    wrong_secret = "wrong-secret-" + ("b" * 32)
    wrong_file.write_text(wrong_secret, encoding="ascii")
    calls = []

    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda endpoint, request: calls.append(
            (endpoint, request)
        ),
    ) as server:
        with pytest.raises(TransportError) as raised:
            _client(server, wrong_file).call("claim", {})

    assert raised.value.category is PublicError.AUTHENTICATION_FAILED
    assert str(raised.value) == "authentication_failed"
    assert wrong_secret not in repr(raised.value)
    assert calls == []


def test_oversized_request_is_rejected_before_body_read(tmp_path):
    token_file = _token_file(tmp_path)
    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: {},
        max_request_bytes=128,
    ) as server:
        status, response = _raw_post(
            server,
            body=b"{}",
            headers={"Content-Length": "129"},
        )

    assert status == 413
    assert response == {"ok": False, "error": "request_too_large"}


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        b'{"duplicate":1,"duplicate":2}',
        b'{"not_finite":NaN}',
        b"\xff",
    ],
)
def test_malformed_or_non_object_json_is_rejected(tmp_path, payload):
    token_file = _token_file(tmp_path)
    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: {},
    ) as server:
        status, response = _raw_post(server, body=payload)

    assert status == 400
    assert response == {"ok": False, "error": "malformed_request"}


def test_dispatch_exception_and_secret_detail_are_not_exposed(tmp_path):
    token_file = _token_file(tmp_path)
    sensitive_detail = "D:/private/media-name.mkv"

    def dispatch(_endpoint, _request):
        raise RuntimeError(sensitive_detail)

    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=dispatch,
    ) as server:
        status, response = _raw_post(server, body=b"{}")

    encoded = json.dumps(response)
    assert status == 500
    assert response == {"ok": False, "error": "dispatch_failed"}
    assert sensitive_detail not in encoded


def test_dispatcher_must_return_a_json_object(tmp_path):
    token_file = _token_file(tmp_path)
    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: ["not", "an", "object"],
    ) as server:
        status, response = _raw_post(server, body=b"{}")

    assert status == 500
    assert response == {"ok": False, "error": "dispatch_failed"}


def test_dispatcher_can_use_only_allowlisted_public_errors(tmp_path):
    token_file = _token_file(tmp_path)

    def dispatch(_endpoint, _request):
        raise DispatchRejected(PublicError.REQUEST_CONFLICT)

    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=dispatch,
    ) as server:
        with pytest.raises(TransportError) as raised:
            _client(server, token_file).call("commit", {})

    assert raised.value.category is PublicError.REQUEST_CONFLICT
    assert raised.value.status_code == 409
    with pytest.raises(ValueError):
        DispatchRejected(PublicError.AUTHENTICATION_FAILED)


def test_response_size_is_bounded(tmp_path):
    token_file = _token_file(tmp_path)
    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: {"value": "x" * 200},
        max_response_bytes=128,
    ) as server:
        with pytest.raises(TransportError) as raised:
            _client(server, token_file).call("snapshot", {})

    assert raised.value.category is PublicError.RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "192.168.1.10",
        "localhost",
        "127.0.0.2",
        "::",
    ],
)
def test_non_exact_loopback_bind_is_rejected(tmp_path, host):
    token_file = _token_file(tmp_path)
    with pytest.raises(ValueError):
        LoopbackJsonServer(
            token_file=token_file,
            dispatcher=lambda _endpoint, _request: {},
            host=host,
        )


def test_loopback_validation_accepts_only_exact_literals():
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_host("::1") == "::1"


def test_client_rejects_non_loopback_and_non_http_urls(tmp_path):
    token_file = _token_file(tmp_path)
    for url in (
        "http://192.168.1.10:8000",
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8000/v1",
    ):
        with pytest.raises(ValueError):
            LoopbackJsonClient(base_url=url, token_file=token_file)


def test_client_rejects_invalid_or_oversized_objects_locally(tmp_path):
    token_file = _token_file(tmp_path)
    client = LoopbackJsonClient(
        base_url="http://127.0.0.1:9",
        token_file=token_file,
        max_request_bytes=128,
    )
    with pytest.raises(TransportError) as non_object:
        client.call("claim", ["not", "an", "object"])
    assert non_object.value.category is PublicError.INVALID_REQUEST

    with pytest.raises(TransportError) as oversized:
        client.call("claim", {"value": "x" * 200})
    assert oversized.value.category is PublicError.REQUEST_TOO_LARGE


def test_server_shutdown_is_observable_by_client(tmp_path):
    token_file = _token_file(tmp_path)
    server = LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: {"ok": True},
    ).start()
    client = _client(server, token_file, timeout=0.5)
    assert client.call("snapshot", {}) == {"ok": True}

    server.close()
    with pytest.raises(TransportError) as raised:
        client.call("snapshot", {})
    # Windows can report a closed listener as either a refusal/reset or a
    # bounded connect timeout depending on the TCP teardown state.
    assert raised.value.category in {
        PublicError.CONNECTION_FAILED,
        PublicError.REQUEST_TIMEOUT,
    }


def test_server_close_stops_admission_and_drains_active_request(tmp_path):
    token_file = _token_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    client_outcome = []
    close_outcome = []

    def dispatcher(_endpoint, _request):
        entered.set()
        assert release.wait(2)
        return {"drained": True}

    server = LoopbackJsonServer(
        token_file=token_file,
        dispatcher=dispatcher,
    ).start()
    client = _client(server, token_file)
    client_thread = threading.Thread(
        target=lambda: client_outcome.append(client.call("status", {}))
    )
    close_thread = threading.Thread(
        target=lambda: close_outcome.append(server.close(timeout=2))
    )
    try:
        client_thread.start()
        assert entered.wait(1)
        close_thread.start()
        time.sleep(0.05)
        assert close_thread.is_alive()

        release.set()
        client_thread.join(timeout=2)
        close_thread.join(timeout=2)

        assert not client_thread.is_alive()
        assert not close_thread.is_alive()
        assert client_outcome == [{"drained": True}]
        assert close_outcome == [None]
    finally:
        release.set()
        client_thread.join(timeout=2)
        close_thread.join(timeout=2)


def test_default_http_request_logging_is_disabled(tmp_path, capsys):
    token_file = _token_file(tmp_path)
    with LoopbackJsonServer(
        token_file=token_file,
        dispatcher=lambda _endpoint, _request: {},
    ) as server:
        _client(server, token_file).call("snapshot", {})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_token_is_validated_without_being_echoed(tmp_path):
    short_secret = "do-not-echo"
    token_file = tmp_path / "short-token.txt"
    token_file.write_text(short_secret, encoding="ascii")
    with pytest.raises(TransportError) as raised:
        LoopbackJsonServer(
            token_file=token_file,
            dispatcher=lambda _endpoint, _request: {},
        )
    assert raised.value.category is PublicError.SERVER_UNAVAILABLE
    assert short_secret not in repr(raised.value)


def test_tunnel_command_has_required_liveness_and_no_secret():
    secret = "must-never-enter-argv"
    command = build_ssh_local_forward_command(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        server_alive_interval=17,
        server_alive_count_max=4,
        connect_timeout=8,
    )
    rendered = " ".join(command)

    assert command[0] == "ssh"
    assert "-n" in command
    assert "-N" in command
    assert "-T" in command
    assert "BatchMode=yes" in command
    assert "PreferredAuthentications=publickey" in command
    assert "PasswordAuthentication=no" in command
    assert "KbdInteractiveAuthentication=no" in command
    assert "NumberOfPasswordPrompts=0" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "ConnectionAttempts=1" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "ServerAliveInterval=17" in command
    assert "ServerAliveCountMax=4" in command
    assert "ConnectTimeout=8" in command
    assert "127.0.0.1:38123:127.0.0.1:39123" in command
    assert command[-1] == "storage-host.test"
    assert secret not in rendered


def test_tunnel_command_rejects_unsafe_destination_and_wide_forward():
    with pytest.raises(ValueError):
        build_ssh_local_forward_command(
            destination="-oProxyCommand=bad",
            local_port=38123,
            remote_port=39123,
        )
    with pytest.raises(ValueError):
        build_ssh_local_forward_command(
            destination="storage-host.test",
            local_port=38123,
            remote_port=39123,
            local_host="0.0.0.0",
        )


class _FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.return_code = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = 0

    def kill(self):
        self.killed = True
        self.return_code = -9

    def wait(self, timeout):
        del timeout
        self.wait_calls += 1
        if self.return_code is None:
            raise subprocess.TimeoutExpired("ssh", 1)
        return self.return_code


def test_tunnel_start_monitor_and_stop_are_quiet(monkeypatch):
    fake = _FakeProcess()
    launched = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(lan_transport.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
    )

    assert tunnel.status().started is False
    tunnel.start(verify_ready=False)
    assert tunnel.status().running is True
    tunnel.ensure_running()
    assert launched["kwargs"]["stdin"] is subprocess.DEVNULL
    assert launched["kwargs"]["stdout"] is subprocess.DEVNULL
    assert launched["kwargs"]["stderr"] is subprocess.DEVNULL
    assert launched["kwargs"]["shell"] is False

    fake.return_code = 255
    with pytest.raises(TransportError) as raised:
        tunnel.ensure_running()
    assert raised.value.category is PublicError.TUNNEL_STOPPED

    fake.return_code = None
    tunnel.stop()
    assert fake.terminated is True


def test_stopped_tunnel_can_be_started_again(monkeypatch):
    processes = [_FakeProcess(), _FakeProcess()]

    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: processes.pop(0),
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
    )

    tunnel.start(verify_ready=False)
    first = tunnel._process
    first.return_code = 255
    tunnel.start(verify_ready=False)

    assert tunnel.status().running is True
    assert tunnel._process is not first


def test_tunnel_owner_record_is_atomic_bound_and_removed_on_stop(
    tmp_path,
    monkeypatch,
):
    fake = _FakeProcess(pid=4321)
    record_path = tmp_path / "lan-tunnel-owner.json"
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport,
        "_process_creation_filetime",
        lambda process: 133_700_000_000_000_123
        if process is fake
        else 0,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        ownership_record_path=record_path,
    )

    tunnel.start(verify_ready=False)

    expected_digest = hashlib.sha256(
        "\0".join(tunnel.command).encode("utf-8")
    ).hexdigest().upper()
    assert json.loads(record_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "event": "LanTunnelOwner",
        "ssh_pid": 4321,
        "helper_pid": lan_transport.os.getpid(),
        "ssh_creation_filetime": 133_700_000_000_000_123,
        "local_address": "127.0.0.1",
        "local_port": 38123,
        "remote_address": "127.0.0.1",
        "remote_port": 39123,
        "command_sha256": expected_digest,
    }
    assert not list(tmp_path.glob(".lan-tunnel-owner.json.*.tmp"))

    tunnel.stop()

    assert fake.terminated is True
    assert fake.wait_calls == 1
    assert not record_path.exists()


def test_tunnel_owner_write_failure_stops_and_waits_for_child(
    tmp_path,
    monkeypatch,
):
    fake = _FakeProcess(pid=4322)
    record_path = tmp_path / "lan-tunnel-owner.json"
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport,
        "_process_creation_filetime",
        lambda _process: 133_700_000_000_000_124,
    )

    def fail_write(_path, _record):
        raise OSError("synthetic owner-record write failure")

    monkeypatch.setattr(
        lan_transport,
        "_write_tunnel_ownership_record",
        fail_write,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        ownership_record_path=record_path,
    )

    with pytest.raises(TransportError) as raised:
        tunnel.start(verify_ready=False)

    assert raised.value.category is PublicError.TUNNEL_START_FAILED
    assert fake.terminated is True
    assert fake.wait_calls == 1
    assert tunnel._process is None
    assert not record_path.exists()


def test_existing_owner_record_is_not_replaced_or_launched_over(
    tmp_path,
    monkeypatch,
):
    record_path = tmp_path / "lan-tunnel-owner.json"
    prior_record = {"schema_version": 1, "event": "LanTunnelOwner"}
    record_path.write_text(json.dumps(prior_record), encoding="utf-8")
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a prior owner record must block launch")
        ),
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        ownership_record_path=record_path,
    )

    with pytest.raises(TransportError) as raised:
        tunnel.start(verify_ready=False)

    assert raised.value.category is PublicError.TUNNEL_START_FAILED
    assert json.loads(record_path.read_text(encoding="utf-8")) == prior_record


def test_tunnel_stop_preserves_a_nonmatching_owner_record(
    tmp_path,
    monkeypatch,
):
    fake = _FakeProcess(pid=4323)
    record_path = tmp_path / "lan-tunnel-owner.json"
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport,
        "_process_creation_filetime",
        lambda _process: 133_700_000_000_000_125,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        ownership_record_path=record_path,
    )
    tunnel.start(verify_ready=False)
    replacement = json.loads(record_path.read_text(encoding="utf-8"))
    replacement["ssh_pid"] = 9999
    record_path.write_text(
        json.dumps(replacement, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TransportError) as raised:
        tunnel.stop()

    assert raised.value.category is PublicError.TUNNEL_STOPPED
    assert json.loads(record_path.read_text(encoding="utf-8")) == replacement
    assert tunnel._ownership_record is not None


def test_immediate_tunnel_exit_clears_matching_owner_record(
    tmp_path,
    monkeypatch,
):
    fake = _FakeProcess(pid=4324)
    fake.return_code = 255
    record_path = tmp_path / "lan-tunnel-owner.json"
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport,
        "_process_creation_filetime",
        lambda _process: 133_700_000_000_000_126,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        ownership_record_path=record_path,
    )

    with pytest.raises(TransportError) as raised:
        tunnel.start(verify_ready=False)

    assert raised.value.category is PublicError.TUNNEL_START_FAILED
    assert fake.wait_calls == 1
    assert not record_path.exists()


def test_tunnel_readiness_timeout_clears_matching_owner_record(
    tmp_path,
    monkeypatch,
):
    fake = _FakeProcess(pid=4325)
    record_path = tmp_path / "lan-tunnel-owner.json"
    monkeypatch.setattr(
        lan_transport.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        lan_transport,
        "_local_listener_is_open",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        lan_transport,
        "_process_creation_filetime",
        lambda _process: 133_700_000_000_000_127,
    )
    tunnel = SshLocalForward(
        destination="storage-host.test",
        local_port=38123,
        remote_port=39123,
        startup_timeout=0.01,
        ownership_record_path=record_path,
    )

    with pytest.raises(TransportError) as raised:
        tunnel.start()

    assert raised.value.category is PublicError.TUNNEL_START_TIMEOUT
    assert fake.terminated is True
    assert fake.wait_calls == 1
    assert not record_path.exists()
