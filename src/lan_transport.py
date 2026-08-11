"""Authenticated loopback HTTP transport for LAN transcoder coordination.

The transport is intentionally small and dependency-free.  Both peers bind or
connect only to a literal loopback address; OpenSSH local forwarding supplies
the encrypted and authenticated LAN hop.  Media paths and bearer tokens never
belong in transport errors or process arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


API_VERSION = "v1"
DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_CLIENT_TIMEOUT_SECONDS = 10.0
_TOKEN_MIN_BYTES = 32
_TOKEN_MAX_BYTES = 512
_ENDPOINT_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
TUNNEL_OWNER_SCHEMA_VERSION = 1
TUNNEL_OWNER_EVENT = "LanTunnelOwner"
_MAXIMUM_TUNNEL_OWNER_RECORD_BYTES = 4 * 1024


class PublicError(str, Enum):
    """Error categories safe to expose to an aggregate-only caller."""

    AUTHENTICATION_FAILED = "authentication_failed"
    CONNECTION_FAILED = "connection_failed"
    DISPATCH_FAILED = "dispatch_failed"
    ENDPOINT_NOT_FOUND = "endpoint_not_found"
    INVALID_REQUEST = "invalid_request"
    MALFORMED_REQUEST = "malformed_request"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    REQUEST_CONFLICT = "request_conflict"
    REQUEST_TIMEOUT = "request_timeout"
    REQUEST_TOO_LARGE = "request_too_large"
    RESPONSE_TOO_LARGE = "response_too_large"
    SERVER_UNAVAILABLE = "server_unavailable"
    TRANSPORT_PROTOCOL_ERROR = "transport_protocol_error"
    TUNNEL_ALREADY_RUNNING = "tunnel_already_running"
    TUNNEL_LOCAL_PORT_IN_USE = "tunnel_local_port_in_use"
    TUNNEL_START_FAILED = "tunnel_start_failed"
    TUNNEL_START_TIMEOUT = "tunnel_start_timeout"
    TUNNEL_STOPPED = "tunnel_stopped"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    VERSION_NOT_SUPPORTED = "version_not_supported"


_DISPATCH_STATUS = {
    PublicError.INVALID_REQUEST: 400,
    PublicError.ENDPOINT_NOT_FOUND: 404,
    PublicError.REQUEST_CONFLICT: 409,
    PublicError.SERVER_UNAVAILABLE: 503,
}


class TransportError(RuntimeError):
    """A category-only transport failure safe for aggregate monitoring."""

    def __init__(
        self,
        category: PublicError,
        *,
        status_code: int | None = None,
    ) -> None:
        if not isinstance(category, PublicError):
            raise TypeError("category must be PublicError")
        super().__init__(category.value)
        self.category = category
        self.status_code = status_code


class DispatchRejected(RuntimeError):
    """Allow a dispatcher to reject a request using a fixed safe category."""

    def __init__(self, category: PublicError) -> None:
        if category not in _DISPATCH_STATUS:
            raise ValueError("category is not safe for dispatcher use")
        super().__init__(category.value)
        self.category = category


JsonObject = dict[str, Any]
JsonDispatcher = Callable[[str, JsonObject], Mapping[str, Any]]


def _validate_positive_number(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not (0 < float(value) < float("inf"))
    ):
        raise ValueError(f"{name} must be positive")
    return float(value)


def _validate_size_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 128:
        raise ValueError(f"{name} must be an integer of at least 128")
    return value


def _validate_port(value: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 65535
    ):
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def validate_loopback_host(host: str) -> str:
    """Return a canonical permitted loopback literal.

    Hostnames and the rest of 127/8 are rejected so configuration cannot
    silently widen or redirect the listener.
    """

    if not isinstance(host, str):
        raise TypeError("host must be str")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("host must be a permitted loopback literal") from exc
    canonical = parsed.compressed
    if canonical not in {"127.0.0.1", "::1"}:
        raise ValueError("host must be 127.0.0.1 or ::1")
    return canonical


def _load_bearer_token(path: str | os.PathLike[str]) -> bytes:
    """Load one printable bearer token without including it in failures."""

    try:
        token_path = Path(path)
        size = token_path.stat().st_size
        if size > _TOKEN_MAX_BYTES + 2:
            raise OSError
        raw = token_path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise TransportError(PublicError.SERVER_UNAVAILABLE) from exc

    # A single editor-created line ending is tolerated.  Embedded whitespace is
    # not: it is ambiguous in the Authorization header.
    token = raw.rstrip(b"\r\n")
    if (
        not _TOKEN_MIN_BYTES <= len(token) <= _TOKEN_MAX_BYTES
        or len(raw) - len(token) > 2
        or any(byte < 0x21 or byte > 0x7E for byte in token)
    ):
        raise TransportError(PublicError.SERVER_UNAVAILABLE)
    return token


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_json_object(payload: bytes) -> JsonObject:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise TransportError(PublicError.MALFORMED_REQUEST) from exc
    if not isinstance(value, dict):
        raise TransportError(PublicError.MALFORMED_REQUEST)
    return value


def _encode_json_object(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping):
        raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
    normalized = dict(value)
    if any(not isinstance(key, str) for key in normalized):
        raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR) from exc


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not _ENDPOINT_PATTERN.fullmatch(endpoint):
        raise TransportError(PublicError.ENDPOINT_NOT_FOUND)
    return endpoint


class _CoordinatorHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        dispatcher: JsonDispatcher,
        token_digest: bytes,
        max_request_bytes: int,
        max_response_bytes: int,
        request_timeout: float,
    ) -> None:
        self.dispatcher = dispatcher
        self.token_digest = token_digest
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.request_timeout = request_timeout
        self._request_condition = threading.Condition()
        self._active_request_threads = 0
        super().__init__(server_address, handler_class)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        with self._request_condition:
            self._active_request_threads += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._request_condition:
                self._active_request_threads -= 1
                self._request_condition.notify_all()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._request_condition:
                self._active_request_threads -= 1
                self._request_condition.notify_all()

    def wait_for_request_drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._request_condition:
            while self._active_request_threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._request_condition.wait(remaining)
            return True

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def handle_error(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        # socketserver's default prints a traceback (and possibly request
        # details) to stderr.  The peer receives a bounded transport category
        # whenever request parsing got far enough to send one.
        del request, client_address


class _CoordinatorHttpServerV6(_CoordinatorHttpServer):
    address_family = socket.AF_INET6


class _CoordinatorRequestHandler(BaseHTTPRequestHandler):
    """Strict, quiet request handler with bounded category-only responses."""

    protocol_version = "HTTP/1.1"
    server_version = "LANCoordinator"
    sys_version = ""

    @property
    def coordinator_server(self) -> _CoordinatorHttpServer:
        server = self.server
        if not isinstance(server, _CoordinatorHttpServer):
            raise RuntimeError("invalid server")
        return server

    def log_message(self, _format: str, *args: object) -> None:
        # BaseHTTPRequestHandler otherwise writes request paths to stderr.
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del code, message, explain
        self._write_error(400, PublicError.MALFORMED_REQUEST)

    def do_POST(self) -> None:
        if not self._authenticated():
            self._write_error(401, PublicError.AUTHENTICATION_FAILED)
            return

        endpoint_or_error = self._parse_endpoint()
        if isinstance(endpoint_or_error, PublicError):
            status = (
                404
                if endpoint_or_error is PublicError.ENDPOINT_NOT_FOUND
                else 400
            )
            self._write_error(status, endpoint_or_error)
            return

        if not self._valid_json_content_type():
            self._write_error(415, PublicError.UNSUPPORTED_MEDIA_TYPE)
            return
        length = self._content_length()
        if isinstance(length, PublicError):
            status = (
                413
                if length is PublicError.REQUEST_TOO_LARGE
                else 400
            )
            self._write_error(status, length)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._write_error(400, PublicError.MALFORMED_REQUEST)
            return

        try:
            body = self.rfile.read(length)
        except (OSError, socket.timeout):
            self._write_error(408, PublicError.REQUEST_TIMEOUT)
            return
        if len(body) != length:
            self._write_error(400, PublicError.MALFORMED_REQUEST)
            return
        try:
            request_object = _decode_json_object(body)
        except TransportError as exc:
            self._write_error(400, exc.category)
            return

        try:
            response = self.coordinator_server.dispatcher(
                endpoint_or_error,
                request_object,
            )
        except DispatchRejected as exc:
            self._write_error(_DISPATCH_STATUS[exc.category], exc.category)
            return
        except Exception:
            self._write_error(500, PublicError.DISPATCH_FAILED)
            return

        if not isinstance(response, Mapping):
            self._write_error(500, PublicError.DISPATCH_FAILED)
            return
        try:
            payload = _encode_json_object(
                {"ok": True, "result": dict(response)}
            )
        except Exception:
            self._write_error(500, PublicError.DISPATCH_FAILED)
            return
        if len(payload) > self.coordinator_server.max_response_bytes:
            self._write_error(500, PublicError.RESPONSE_TOO_LARGE)
            return
        self._write_payload(200, payload)

    def do_GET(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def do_TRACE(self) -> None:
        self._reject_method()

    def do_CONNECT(self) -> None:
        self._reject_method()

    def _reject_method(self) -> None:
        if not self._authenticated():
            self._write_error(401, PublicError.AUTHENTICATION_FAILED)
            return
        self._write_error(405, PublicError.METHOD_NOT_ALLOWED)

    def _authenticated(self) -> bool:
        values = self.headers.get_all("Authorization") or []
        candidate = b""
        if len(values) == 1:
            raw = values[0]
            if len(raw) <= _TOKEN_MAX_BYTES + 16:
                scheme, separator, supplied = raw.partition(" ")
                if separator and scheme.lower() == "bearer":
                    try:
                        candidate = supplied.encode("ascii")
                    except UnicodeEncodeError:
                        candidate = b""
        candidate_digest = hashlib.sha256(candidate).digest()
        return hmac.compare_digest(
            candidate_digest,
            self.coordinator_server.token_digest,
        )

    def _parse_endpoint(self) -> str | PublicError:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return PublicError.ENDPOINT_NOT_FOUND
        if parsed.query or parsed.fragment:
            return PublicError.ENDPOINT_NOT_FOUND
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0]:
            return PublicError.ENDPOINT_NOT_FOUND
        if parts[1] != API_VERSION:
            return PublicError.VERSION_NOT_SUPPORTED
        try:
            return _validate_endpoint(parts[2])
        except TransportError:
            return PublicError.ENDPOINT_NOT_FOUND

    def _valid_json_content_type(self) -> bool:
        raw = self.headers.get("Content-Type")
        if raw is None:
            return False
        parts = [part.strip().lower() for part in raw.split(";")]
        if not parts or parts[0] != "application/json":
            return False
        return all(part == "charset=utf-8" for part in parts[1:])

    def _content_length(self) -> int | PublicError:
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1:
            return PublicError.MALFORMED_REQUEST
        raw = values[0]
        if not raw or not raw.isascii() or not raw.isdecimal():
            return PublicError.MALFORMED_REQUEST
        try:
            length = int(raw, 10)
        except ValueError:
            return PublicError.MALFORMED_REQUEST
        if length > self.coordinator_server.max_request_bytes:
            return PublicError.REQUEST_TOO_LARGE
        if length <= 0:
            return PublicError.MALFORMED_REQUEST
        return length

    def _write_error(self, status: int, category: PublicError) -> None:
        payload = _encode_json_object(
            {"ok": False, "error": category.value}
        )
        self._write_payload(status, payload)

    def _write_payload(self, status: int, payload: bytes) -> None:
        self.close_connection = True
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
            return


class LoopbackJsonServer:
    """Background loopback-only JSON server for a coordinator dispatcher."""

    def __init__(
        self,
        *,
        token_file: str | os.PathLike[str],
        dispatcher: JsonDispatcher,
        host: str = "127.0.0.1",
        port: int = 0,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        host = validate_loopback_host(host)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError("port must be between 0 and 65535")
        if not callable(dispatcher):
            raise TypeError("dispatcher must be callable")
        max_request_bytes = _validate_size_limit(
            max_request_bytes,
            "max_request_bytes",
        )
        max_response_bytes = _validate_size_limit(
            max_response_bytes,
            "max_response_bytes",
        )
        request_timeout = _validate_positive_number(
            request_timeout,
            "request_timeout",
        )

        token = _load_bearer_token(token_file)
        token_digest = hashlib.sha256(token).digest()
        del token
        server_type = (
            _CoordinatorHttpServerV6
            if host == "::1"
            else _CoordinatorHttpServer
        )
        try:
            self._server = server_type(
                (host, port),
                _CoordinatorRequestHandler,
                dispatcher=dispatcher,
                token_digest=token_digest,
                max_request_bytes=max_request_bytes,
                max_response_bytes=max_response_bytes,
                request_timeout=request_timeout,
            )
        except OSError as exc:
            raise TransportError(PublicError.SERVER_UNAVAILABLE) from exc
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> "LoopbackJsonServer":
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return self
                raise TransportError(PublicError.SERVER_UNAVAILABLE)
            thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="lan-coordinator-http",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self

    def close(self, *, timeout: float = 5.0) -> None:
        timeout = _validate_positive_number(timeout, "timeout")
        deadline = time.monotonic() + timeout
        with self._lock:
            thread = self._thread
        if thread is not None:
            self._server.shutdown()
            thread.join(max(0.0, deadline - time.monotonic()))
        thread_stopped = thread is None or not thread.is_alive()
        requests_drained = self._server.wait_for_request_drain(
            max(0.0, deadline - time.monotonic())
        )
        self._server.server_close()
        if not thread_stopped or not requests_drained:
            raise TransportError(PublicError.SERVER_UNAVAILABLE)

    def __enter__(self) -> "LoopbackJsonServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LoopbackJsonClient:
    """Strict JSON client for a local coordinator or SSH-forward endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: str | os.PathLike[str],
        timeout: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            raise ValueError("base_url must be loopback HTTP with no path")
        host = validate_loopback_host(parsed.hostname)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        if port is None:
            port = 80
        self._host = host
        self._port = _validate_port(port, "port")
        self._timeout = _validate_positive_number(timeout, "timeout")
        self._max_request_bytes = _validate_size_limit(
            max_request_bytes,
            "max_request_bytes",
        )
        self._max_response_bytes = _validate_size_limit(
            max_response_bytes,
            "max_response_bytes",
        )
        self._token = _load_bearer_token(token_file).decode("ascii")

    def call(self, endpoint: str, request: Mapping[str, Any]) -> JsonObject:
        endpoint = _validate_endpoint(endpoint)
        try:
            payload = _encode_json_object(request)
        except TransportError as exc:
            raise TransportError(PublicError.INVALID_REQUEST) from exc
        if len(payload) > self._max_request_bytes:
            raise TransportError(PublicError.REQUEST_TOO_LARGE)

        connection = http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=self._timeout,
        )
        try:
            connection.request(
                "POST",
                f"/{API_VERSION}/{endpoint}",
                body=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(payload)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            response_payload = self._read_response(response)
        except socket.timeout as exc:
            raise TransportError(PublicError.REQUEST_TIMEOUT) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(PublicError.CONNECTION_FAILED) from exc
        finally:
            connection.close()

        try:
            envelope = _decode_json_object(response_payload)
        except TransportError as exc:
            raise TransportError(
                PublicError.TRANSPORT_PROTOCOL_ERROR,
                status_code=response.status,
            ) from exc
        if response.status == 200:
            if envelope.get("ok") is not True:
                raise TransportError(
                    PublicError.TRANSPORT_PROTOCOL_ERROR,
                    status_code=response.status,
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise TransportError(
                    PublicError.TRANSPORT_PROTOCOL_ERROR,
                    status_code=response.status,
                )
            return result

        category = _safe_remote_error(envelope)
        raise TransportError(category, status_code=response.status)

    def _read_response(self, response: http.client.HTTPResponse) -> bytes:
        content_type = response.getheader("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        transfer_encoding = response.getheader("Transfer-Encoding")
        lengths = response.headers.get_all("Content-Length") or []
        if transfer_encoding is not None or len(lengths) != 1:
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        length = int(raw_length, 10)
        if length > self._max_response_bytes:
            raise TransportError(PublicError.RESPONSE_TOO_LARGE)
        payload = response.read(length + 1)
        if len(payload) != length:
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        return payload


def _safe_remote_error(envelope: JsonObject) -> PublicError:
    if envelope.get("ok") is not False:
        return PublicError.TRANSPORT_PROTOCOL_ERROR
    raw = envelope.get("error")
    try:
        return PublicError(raw)
    except (TypeError, ValueError):
        return PublicError.TRANSPORT_PROTOCOL_ERROR


def _validate_ssh_destination(destination: str) -> str:
    if (
        not isinstance(destination, str)
        or not destination
        or destination.startswith("-")
        or any(character.isspace() or ord(character) < 0x20 for character in destination)
    ):
        raise ValueError("destination must be a safe OpenSSH host or alias")
    return destination


def _ssh_host_field(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def build_ssh_local_forward_command(
    *,
    destination: str,
    local_port: int,
    remote_port: int,
    ssh_executable: str = "ssh",
    local_host: str = "127.0.0.1",
    remote_host: str = "127.0.0.1",
    server_alive_interval: int = 15,
    server_alive_count_max: int = 3,
    connect_timeout: int = 10,
) -> list[str]:
    """Build a passwordless OpenSSH forward command.

    The signature deliberately has no password, bearer-token, or secret
    argument.  Authentication is delegated to the user's OpenSSH configuration
    or agent; the application bearer token remains in its file.
    """

    destination = _validate_ssh_destination(destination)
    local_host = validate_loopback_host(local_host)
    remote_host = validate_loopback_host(remote_host)
    local_port = _validate_port(local_port, "local_port")
    remote_port = _validate_port(remote_port, "remote_port")
    if not isinstance(ssh_executable, str) or not ssh_executable:
        raise ValueError("ssh_executable must be non-empty")
    server_alive_interval = _validate_port(
        server_alive_interval,
        "server_alive_interval",
    )
    server_alive_count_max = _validate_port(
        server_alive_count_max,
        "server_alive_count_max",
    )
    connect_timeout = _validate_port(connect_timeout, "connect_timeout")
    forward = (
        f"{_ssh_host_field(local_host)}:{local_port}:"
        f"{_ssh_host_field(remote_host)}:{remote_port}"
    )
    return [
        ssh_executable,
        "-n",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ServerAliveInterval={server_alive_interval}",
        "-o",
        f"ServerAliveCountMax={server_alive_count_max}",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-L",
        forward,
        destination,
    ]


def _tunnel_command_sha256(command: list[str]) -> str:
    """Bind an owner record to the exact, secret-free SSH argv vector."""

    encoded = "\0".join(command).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _process_creation_filetime(process: subprocess.Popen[bytes]) -> int:
    """Return the Windows creation FILETIME for an already-created process.

    The helper is deliberately module-level so non-Windows tests can replace
    it with a deterministic token provider.  Ownership records are only used
    by the Windows helper deployment.
    """

    if os.name != "nt":
        raise OSError("process creation FILETIME is available only on Windows")

    import ctypes
    from ctypes import wintypes

    process_handle = getattr(process, "_handle", None)
    if process_handle is None:
        raise OSError("process handle is unavailable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(int(process_handle)),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    value = (int(creation.dwHighDateTime) << 32) | int(
        creation.dwLowDateTime
    )
    if value <= 0:
        raise OSError("process creation FILETIME is invalid")
    return value


def _write_tunnel_ownership_record(
    path: str,
    record: Mapping[str, object],
) -> None:
    """Atomically publish one local owner record without replacing another."""

    destination = Path(path)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}."
        f"{time.time_ns()}.tmp"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(record),
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A hard-link publication is atomic and fails if a prior owner's
        # record still exists.  The temporary link is removed immediately,
        # leaving the published record with a single link in normal operation.
        os.link(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_matching_tunnel_ownership_record(
    path: str,
    expected: Mapping[str, object],
) -> bool:
    """Remove *path* only when it still describes this exact tunnel instance."""

    destination = Path(path)
    try:
        before = destination.stat(follow_symlinks=False)
        if before.st_size <= 0 or before.st_size > _MAXIMUM_TUNNEL_OWNER_RECORD_BYTES:
            return False
        with open(destination, "r", encoding="utf-8") as handle:
            current = json.load(handle)
        after = destination.stat(follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or current != dict(expected):
        return False
    try:
        confirmed = destination.stat(follow_symlinks=False)
        identity_confirmed = (
            confirmed.st_dev,
            confirmed.st_ino,
            confirmed.st_size,
            confirmed.st_mtime_ns,
        )
        if identity_confirmed != identity_after:
            return False
        destination.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    started: bool
    running: bool
    return_code: int | None


class SshLocalForward:
    """Own and monitor one quiet OpenSSH local-forward subprocess."""

    def __init__(
        self,
        *,
        destination: str,
        local_port: int,
        remote_port: int,
        ssh_executable: str = "ssh",
        local_host: str = "127.0.0.1",
        remote_host: str = "127.0.0.1",
        server_alive_interval: int = 15,
        server_alive_count_max: int = 3,
        connect_timeout: int = 10,
        startup_timeout: float = 10.0,
        stop_timeout: float = 5.0,
        ownership_record_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._command = build_ssh_local_forward_command(
            destination=destination,
            local_port=local_port,
            remote_port=remote_port,
            ssh_executable=ssh_executable,
            local_host=local_host,
            remote_host=remote_host,
            server_alive_interval=server_alive_interval,
            server_alive_count_max=server_alive_count_max,
            connect_timeout=connect_timeout,
        )
        self._local_host = validate_loopback_host(local_host)
        self._local_port = _validate_port(local_port, "local_port")
        self._remote_host = validate_loopback_host(remote_host)
        self._remote_port = _validate_port(remote_port, "remote_port")
        self._startup_timeout = _validate_positive_number(
            startup_timeout,
            "startup_timeout",
        )
        self._stop_timeout = _validate_positive_number(
            stop_timeout,
            "stop_timeout",
        )
        if ownership_record_path is None:
            self._ownership_record_path = None
        else:
            raw_record_path = os.fspath(ownership_record_path)
            if not isinstance(raw_record_path, str) or not raw_record_path.strip():
                raise ValueError("ownership_record_path must be a non-empty path")
            self._ownership_record_path = os.path.abspath(raw_record_path)
        self._process: subprocess.Popen[bytes] | None = None
        self._ownership_record: dict[str, object] | None = None
        self._lock = threading.Lock()

    @property
    def command(self) -> tuple[str, ...]:
        """Return the secret-free OpenSSH argument vector."""

        return tuple(self._command)

    def _build_ownership_record(
        self,
        process: subprocess.Popen[bytes],
    ) -> dict[str, object]:
        ssh_pid = int(process.pid)
        helper_pid = int(os.getpid())
        creation_filetime = int(_process_creation_filetime(process))
        if ssh_pid <= 0 or helper_pid <= 0 or creation_filetime <= 0:
            raise OSError("process ownership token is invalid")
        return {
            "schema_version": TUNNEL_OWNER_SCHEMA_VERSION,
            "event": TUNNEL_OWNER_EVENT,
            "ssh_pid": ssh_pid,
            "helper_pid": helper_pid,
            "ssh_creation_filetime": creation_filetime,
            "local_address": self._local_host,
            "local_port": self._local_port,
            "remote_address": self._remote_host,
            "remote_port": self._remote_port,
            "command_sha256": _tunnel_command_sha256(self._command),
        }

    def _clear_matching_ownership_record(
        self,
        record: Mapping[str, object] | None = None,
    ) -> bool:
        if self._ownership_record_path is None:
            return True
        expected = record if record is not None else self._ownership_record
        if expected is None:
            return True
        return _remove_matching_tunnel_ownership_record(
            self._ownership_record_path,
            expected,
        )

    def _stop_process(self, process: subprocess.Popen[bytes]) -> bool:
        try:
            running = process.poll() is None
        except OSError:
            running = True
        if running:
            try:
                process.terminate()
            except OSError:
                try:
                    process.kill()
                except OSError:
                    return False
        try:
            process.wait(timeout=self._stop_timeout)
            return True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=self._stop_timeout)
                return True
            except (OSError, subprocess.TimeoutExpired):
                return False
        except OSError:
            return False

    def _cleanup_failed_start(
        self,
        process: subprocess.Popen[bytes],
        record: Mapping[str, object] | None,
    ) -> bool:
        stopped = self._stop_process(process)
        if stopped:
            cleared = self._clear_matching_ownership_record(record)
            with self._lock:
                if cleared and self._process is process:
                    self._process = None
                    self._ownership_record = None
            return cleared
        return False

    def start(self, *, verify_ready: bool = True) -> "SshLocalForward":
        with self._lock:
            if self._process is not None:
                if self._process.poll() is None:
                    raise TransportError(PublicError.TUNNEL_ALREADY_RUNNING)
                # A dead tunnel can be replaced after the caller has observed
                # its stopped status and allowed coordinator leases to fence.
                if not self._clear_matching_ownership_record():
                    raise TransportError(PublicError.TUNNEL_START_FAILED)
                self._process = None
                self._ownership_record = None
            if (
                self._ownership_record_path is not None
                and os.path.lexists(self._ownership_record_path)
            ):
                raise TransportError(PublicError.TUNNEL_START_FAILED)
            if _local_listener_is_open(
                self._local_host,
                self._local_port,
                timeout=min(0.2, self._startup_timeout),
            ):
                raise TransportError(PublicError.TUNNEL_LOCAL_PORT_IN_USE)
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            )
            try:
                process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=creation_flags,
                )
            except OSError as exc:
                raise TransportError(PublicError.TUNNEL_START_FAILED) from exc
            self._process = process
            ownership_record = None
            if self._ownership_record_path is not None:
                try:
                    ownership_record = self._build_ownership_record(process)
                    self._ownership_record = ownership_record
                    _write_tunnel_ownership_record(
                        self._ownership_record_path,
                        ownership_record,
                    )
                except Exception as exc:
                    # The child is not allowed to outlive a failure to bind its
                    # durable recovery identity.
                    stopped = self._stop_process(process)
                    if stopped:
                        cleared = self._clear_matching_ownership_record(
                            ownership_record
                        )
                        if cleared:
                            self._process = None
                            self._ownership_record = None
                    raise TransportError(
                        PublicError.TUNNEL_START_FAILED
                    ) from exc

        if not verify_ready:
            if process.poll() is not None:
                self._cleanup_failed_start(process, ownership_record)
                raise TransportError(PublicError.TUNNEL_START_FAILED)
            return self

        try:
            deadline = time.monotonic() + self._startup_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise TransportError(PublicError.TUNNEL_START_FAILED)
                if _local_listener_is_open(
                    self._local_host,
                    self._local_port,
                    timeout=min(0.2, max(0.01, deadline - time.monotonic())),
                ):
                    return self
                time.sleep(0.05)
            raise TransportError(PublicError.TUNNEL_START_TIMEOUT)
        except TransportError:
            self._cleanup_failed_start(process, ownership_record)
            raise
        except Exception as exc:
            self._cleanup_failed_start(process, ownership_record)
            raise TransportError(PublicError.TUNNEL_START_FAILED) from exc

    def status(self) -> TunnelStatus:
        process = self._process
        if process is None:
            return TunnelStatus(False, False, None)
        return_code = process.poll()
        return TunnelStatus(True, return_code is None, return_code)

    def ensure_running(self) -> None:
        if not self.status().running:
            raise TransportError(PublicError.TUNNEL_STOPPED)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            record = self._ownership_record
            if process is None:
                return
            if not self._stop_process(process):
                raise TransportError(PublicError.TUNNEL_STOPPED)
            if not self._clear_matching_ownership_record(record):
                raise TransportError(PublicError.TUNNEL_STOPPED)
            self._ownership_record = None

    def __enter__(self) -> "SshLocalForward":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _local_listener_is_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
