"""Configuration-driven entry point for the separate LAN-assist executable.

The ordinary ``VideoTranscoderPortable.exe`` continues to use ``gui.py``
directly.  This module belongs to the separate
``VideoTranscoderLanAssist.exe`` and takes all machine-specific values from an
external JSON file.  Coordinator and helper status is deliberately
aggregate-only: paths, source names, and bearer tokens are never written to
stdout or status files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import uuid
from typing import Any, Mapping, Sequence

from lan_coordinator import CoordinatorError, DistributedCoordinator
from lan_dashboard import DEFAULT_DASHBOARD_PORT, DashboardServer
from lan_protocol import WorkerRole
from lan_service import (
    CoordinatorService,
    HttpCoordinatorClient,
    LanAssistSupervisor,
)
from lan_transport import (
    LoopbackJsonClient,
    SshLocalForward,
    TransportError,
)
from lan_windows import (
    FileIdentity,
    FileSafetyError,
    delete_verified,
    get_identity,
)
from lan_worker import (
    ComputeWorker,
    LocalFallbackWorker,
    WorkerError,
    WorkerResult,
    default_fallback_root,
)


CONFIG_SCHEMA_VERSION = 1
DEFAULT_CONFIG_NAME = "VideoTranscoderLanAssist.json"
MAXIMUM_CONFIG_BYTES = 64 * 1024
MAX_LEGACY_LEDGER_PATHS = 16
_SNAPSHOT_KEYS = (
    "SchemaVersion",
    "RunId",
    "ContractHash",
    "Status",
    "FailureCategory",
    "InitialSkipped",
    "ReservedBytes",
    "ConsecutiveFailures",
    "total_jobs",
    "pending",
    "leased",
    "suspect",
    "committing",
    "completed",
    "failed",
    "remote_leased",
    "helper_leased",
    "helper_online",
)


class CliError(RuntimeError):
    """Fixed-category CLI failure safe for aggregate output."""

    def __init__(self, category: str, *, exit_code: int = 1) -> None:
        super().__init__(category)
        self.category = category
        self.exit_code = int(exit_code)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Suppress raw argument values from parser failures."""

    def error(self, _message: str) -> None:
        raise CliError("ArgumentsInvalid", exit_code=2)


def _port(value: str) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("invalid port") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("invalid port")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("invalid number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("invalid number")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("invalid integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("invalid integer")
    return parsed


def _settings_hash(value: str) -> str:
    normalized = value.upper()
    if (
        len(normalized) != 64
        or any(character not in "0123456789ABCDEF" for character in normalized)
    ):
        raise argparse.ArgumentTypeError("invalid settings hash")
    return normalized


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="VideoTranscoderLanAssist",
        description=(
            "Run a LAN coordinator or helper from an external JSON "
            "configuration."
        ),
    )
    parser.add_argument(
        "--config",
        help=(
            "configuration path; defaults to "
            "VideoTranscoderLanAssist.json beside the executable"
        ),
    )
    parser.add_argument(
        "--create-token",
        action="store_true",
        help="create the configured token file and exit",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="validate configuration without starting a service",
    )
    return parser


def default_config_path() -> str:
    """Return the visible executable folder's external configuration path."""

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path.cwd()
    return str(root / DEFAULT_CONFIG_NAME)


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite configuration number")


def _required_text(
    config: Mapping[str, Any],
    key: str,
    *,
    default: str | None = None,
) -> str | None:
    value = config.get(key, default)
    if value is None and default is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise CliError("ConfigInvalid", exit_code=2)
    return value


def _config_number(
    config: Mapping[str, Any],
    key: str,
    default: int | float,
    *,
    integer: bool,
) -> int | float:
    value = config.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or (integer and not isinstance(value, int))
    ):
        raise CliError("ConfigInvalid", exit_code=2)
    return int(value) if integer else float(value)


def _config_port(config: Mapping[str, Any], key: str) -> int:
    value = _config_number(config, key, 0, integer=True)
    if not 1 <= int(value) <= 65535:
        raise CliError("ConfigInvalid", exit_code=2)
    return int(value)


def _resolve_config_path(config_root: Path, value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = config_root / path
    return os.path.abspath(os.fspath(path))


def _load_config(path: str) -> argparse.Namespace:
    """Load one strict, bounded external coordinator/helper configuration."""

    try:
        config_path = Path(path)
        raw = config_path.read_bytes()
        if not raw or len(raw) > MAXIMUM_CONFIG_BYTES:
            raise ValueError("configuration size")
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CliError("ConfigInvalid", exit_code=2) from exc
    if not isinstance(value, dict):
        raise CliError("ConfigInvalid", exit_code=2)
    if value.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise CliError("ConfigInvalid", exit_code=2)
    mode = value.get("mode")
    if mode not in {"coordinator", "helper"}:
        raise CliError("ConfigInvalid", exit_code=2)

    common = {
        "schema_version",
        "mode",
        "token_file",
        "ffmpeg",
        "ffprobe",
        "status_file",
    }
    coordinator_keys = common | {
        "root",
        "work_root",
        "api_port",
        "dashboard_port",
        "remote_worker_id",
        "reserve_gib",
        "lease_seconds",
        "helper_presence_seconds",
        "status_interval",
        "accepted_legacy_settings_hashes",
        "legacy_ledger_paths",
        "consecutive_failure_limit",
    }
    helper_keys = common | {
        "ssh_destination",
        "local_port",
        "remote_port",
        "staging_root",
        "cache_root",
        "fallback_root",
        "ssh_executable",
        "worker_id",
        "reconnect_seconds",
    }
    allowed = coordinator_keys if mode == "coordinator" else helper_keys
    if set(value) - allowed:
        raise CliError("ConfigInvalid", exit_code=2)

    config_root = config_path.resolve().parent
    token_file = _required_text(value, "token_file")
    if token_file is None:
        raise CliError("ConfigInvalid", exit_code=2)
    shared: dict[str, Any] = {
        "mode": mode,
        "token_file": _resolve_config_path(config_root, token_file),
        "ffmpeg": _resolve_config_path(
            config_root,
            _required_text(value, "ffmpeg"),
        ),
        "ffprobe": _resolve_config_path(
            config_root,
            _required_text(value, "ffprobe"),
        ),
        "status_file": _resolve_config_path(
            config_root,
            _required_text(value, "status_file"),
        ),
    }
    if mode == "coordinator":
        accepted = value.get("accepted_legacy_settings_hashes", [])
        if not isinstance(accepted, list):
            raise CliError("ConfigInvalid", exit_code=2)
        try:
            accepted = [
                _settings_hash(item)
                for item in accepted
                if isinstance(item, str)
            ]
        except argparse.ArgumentTypeError as exc:
            raise CliError("ConfigInvalid", exit_code=2) from exc
        if len(accepted) != len(
            value.get("accepted_legacy_settings_hashes", [])
        ):
            raise CliError("ConfigInvalid", exit_code=2)
        legacy_paths_value = value.get("legacy_ledger_paths", [])
        if (
            not isinstance(legacy_paths_value, list)
            or len(legacy_paths_value) > MAX_LEGACY_LEDGER_PATHS
        ):
            raise CliError("ConfigInvalid", exit_code=2)
        legacy_ledger_paths: list[str] = []
        for item in legacy_paths_value:
            if (
                not isinstance(item, str)
                or not item
                or any(ord(character) < 0x20 for character in item)
            ):
                raise CliError("ConfigInvalid", exit_code=2)
            resolved = _resolve_config_path(config_root, item)
            if resolved is None:
                raise CliError("ConfigInvalid", exit_code=2)
            legacy_ledger_paths.append(resolved)
        if len(
            {os.path.normcase(path) for path in legacy_ledger_paths}
        ) != len(legacy_ledger_paths):
            raise CliError("ConfigInvalid", exit_code=2)
        root = _required_text(value, "root")
        work_root = _required_text(value, "work_root")
        remote_worker_id = _required_text(
            value,
            "remote_worker_id",
            default="remote-qsv",
        )
        if root is None or work_root is None or remote_worker_id is None:
            raise CliError("ConfigInvalid", exit_code=2)
        shared.update(
            {
                "root": _resolve_config_path(config_root, root),
                "work_root": _resolve_config_path(config_root, work_root),
                "api_port": _config_port(value, "api_port"),
                "dashboard_port": int(
                    _config_number(
                        value,
                        "dashboard_port",
                        DEFAULT_DASHBOARD_PORT,
                        integer=True,
                    )
                ),
                "remote_worker_id": remote_worker_id,
                "reserve_gib": _config_number(
                    value,
                    "reserve_gib",
                    10,
                    integer=True,
                ),
                "lease_seconds": _config_number(
                    value,
                    "lease_seconds",
                    45.0,
                    integer=False,
                ),
                "helper_presence_seconds": _config_number(
                    value,
                    "helper_presence_seconds",
                    30.0,
                    integer=False,
                ),
                "status_interval": _config_number(
                    value,
                    "status_interval",
                    2.0,
                    integer=False,
                ),
                "accepted_legacy_settings_hash": accepted,
                "legacy_ledger_paths": legacy_ledger_paths,
                "consecutive_failure_limit": _config_number(
                    value,
                    "consecutive_failure_limit",
                    3,
                    integer=True,
                ),
            }
        )
        if shared["dashboard_port"] > 65535:
            raise CliError("ConfigInvalid", exit_code=2)
        if shared["dashboard_port"] == shared["api_port"]:
            raise CliError("ConfigInvalid", exit_code=2)
    else:
        required_paths: dict[str, str] = {}
        for key in (
            "staging_root",
            "cache_root",
        ):
            raw_path = _required_text(value, key)
            if raw_path is None:
                raise CliError("ConfigInvalid", exit_code=2)
            required_paths[key] = str(
                _resolve_config_path(config_root, raw_path)
            )
        ssh_destination = _required_text(value, "ssh_destination")
        ssh_executable = _required_text(
            value,
            "ssh_executable",
            default="ssh",
        )
        worker_id = _required_text(
            value,
            "worker_id",
            default="helper-nvenc",
        )
        if (
            ssh_destination is None
            or ssh_executable is None
            or worker_id is None
        ):
            raise CliError("ConfigInvalid", exit_code=2)
        shared.update(
            {
                "ssh_destination": ssh_destination,
                "local_port": _config_port(value, "local_port"),
                "remote_port": _config_port(value, "remote_port"),
                **required_paths,
                "fallback_root": _resolve_config_path(
                    config_root,
                    _required_text(value, "fallback_root"),
                ),
                "ssh_executable": ssh_executable,
                "worker_id": worker_id,
                "reconnect_seconds": _config_number(
                    value,
                    "reconnect_seconds",
                    5.0,
                    integer=False,
                ),
            }
        )
    return argparse.Namespace(**shared)


def _candidate_tool_paths(
    name: str,
    *,
    explicit: str | None,
    sibling_of: str | None,
) -> list[Path]:
    executable_name = name + (".exe" if os.name == "nt" else "")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if sibling_of:
        candidates.append(Path(sibling_of).parent / executable_name)
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.append(Path(bundle_root) / "ffmpeg" / executable_name)
    discovered = shutil.which(executable_name) or shutil.which(name)
    if discovered:
        candidates.append(Path(discovered))
    return candidates


def _first_existing(candidates: Sequence[Path]) -> str | None:
    for candidate in candidates:
        try:
            if candidate.is_file():
                return os.path.abspath(os.fspath(candidate))
        except OSError:
            continue
    return None


def _resolve_media_tools(
    ffmpeg: str | None,
    ffprobe: str | None,
) -> tuple[str, str]:
    """Resolve explicit, sibling, bundled, or PATH media tools."""

    resolved_ffmpeg = _first_existing(
        _candidate_tool_paths(
            "ffmpeg",
            explicit=ffmpeg,
            sibling_of=ffprobe,
        )
    )
    resolved_ffprobe = _first_existing(
        _candidate_tool_paths(
            "ffprobe",
            explicit=ffprobe,
            sibling_of=resolved_ffmpeg or ffmpeg,
        )
    )
    if resolved_ffmpeg is None or resolved_ffprobe is None:
        raise CliError("ToolchainUnavailable")
    return resolved_ffmpeg, resolved_ffprobe


def _write_status_file(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(payload),
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise CliError("StatusWriteFailed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _emit(payload: Mapping[str, Any], status_file: str | None = None) -> None:
    safe_payload = dict(payload)
    if status_file:
        _write_status_file(status_file, safe_payload)
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        print(
            json.dumps(
                safe_payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=stream,
            flush=True,
        )


def _aggregate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"Event": "CoordinatorStatus"}
    for key in _SNAPSHOT_KEYS:
        value = snapshot.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
    return payload


def _worker_status(result: WorkerResult) -> dict[str, Any]:
    return {
        "Event": "HelperStatus",
        "Kind": result.kind,
        "Category": result.category,
        "SourceSizeBytes": int(result.source_size_bytes),
        "EncodeSeconds": float(result.encode_seconds),
    }


def _run_coordinator(args: argparse.Namespace) -> int:
    ffmpeg, ffprobe = _resolve_media_tools(args.ffmpeg, args.ffprobe)
    coordinator: DistributedCoordinator | None = None
    service: CoordinatorService | None = None
    dashboard: DashboardServer | None = None
    try:
        coordinator = DistributedCoordinator(
            root=args.root,
            work_root=args.work_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            reserve_bytes=int(args.reserve_gib) * 1024**3,
            lease_seconds=float(args.lease_seconds),
            helper_presence_seconds=float(args.helper_presence_seconds),
            consecutive_failure_limit=int(
                getattr(args, "consecutive_failure_limit", 3)
            ),
            accepted_legacy_settings_hashes=(
                args.accepted_legacy_settings_hash
            ),
            legacy_ledger_paths=tuple(
                getattr(args, "legacy_ledger_paths", ())
            ),
        )
        service = CoordinatorService(
            coordinator=coordinator,
            token_file=args.token_file,
            api_port=args.api_port,
            remote_worker_id=args.remote_worker_id,
        )
        dashboard_port = getattr(args, "dashboard_port", None)
        if dashboard_port is not None:
            dashboard = DashboardServer(
                provider=coordinator,
                port=int(dashboard_port),
            )
        service.start()
        if dashboard is not None:
            dashboard.start()
        while True:
            snapshot = coordinator.snapshot()
            _emit(_aggregate_snapshot(snapshot), args.status_file)
            if snapshot.get("FailureCategory"):
                return 1
            active = sum(
                int(snapshot.get(key, 0))
                for key in ("pending", "leased", "suspect", "committing")
            )
            if active == 0:
                return 3 if int(snapshot.get("failed", 0)) else 0
            if service.stop_event.is_set():
                raise CliError("CoordinatorServiceStopped")
            service.stop_event.wait(float(args.status_interval))
    finally:
        if dashboard is not None:
            dashboard.close()
        if service is not None:
            service.close()
        if coordinator is not None:
            coordinator.close()


def _run_helper(args: argparse.Namespace) -> int:
    ffmpeg, ffprobe = _resolve_media_tools(args.ffmpeg, args.ffprobe)
    transport = LoopbackJsonClient(
        base_url=f"http://127.0.0.1:{args.local_port}",
        token_file=args.token_file,
    )
    client = HttpCoordinatorClient(transport)
    tunnel = SshLocalForward(
        destination=args.ssh_destination,
        local_port=args.local_port,
        remote_port=args.remote_port,
        ssh_executable=args.ssh_executable,
    )
    helper = ComputeWorker(
        client=client,
        worker_id=args.worker_id,
        worker_role=WorkerRole.HELPER,
        encoder="hevc_nvenc",
        source_root=args.staging_root,
        staging_root=args.staging_root,
        local_cache_root=args.cache_root,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        direct_staging=False,
    )
    fallback = LocalFallbackWorker(
        root=args.fallback_root or default_fallback_root(),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        encoder="hevc_nvenc",
    )
    supervisor = LanAssistSupervisor(
        tunnel=tunnel,
        client=client,
        helper_worker=helper,
        fallback_worker=fallback,
        reconnect_seconds=float(args.reconnect_seconds),
        status_callback=lambda result: _emit(
            _worker_status(result),
            args.status_file,
        ),
    )
    try:
        supervisor.run()
        return 0
    finally:
        supervisor.stop()


def _windows_path_is_network(path: str) -> bool:
    """Return whether *path* resolves to UNC or remote Windows storage."""

    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    absolute = os.path.abspath(path)
    if absolute.replace("/", "\\").startswith("\\\\"):
        return True

    probe = Path(absolute).parent
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise CliError("TokenPathCheckFailed")
        probe = parent

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumePathNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    volume_path = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(
        str(probe),
        volume_path,
        len(volume_path),
    ):
        raise CliError("TokenPathCheckFailed")
    drive_type = int(kernel32.GetDriveTypeW(volume_path.value))
    if drive_type in {0, 1}:
        raise CliError("TokenPathCheckFailed")
    return drive_type == 4


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    error_insufficient_buffer = 122

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("sid", wintypes.LPVOID),
            ("attributes", wintypes.DWORD),
        ]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        token_query,
        ctypes.byref(token),
    ):
        raise CliError("TokenAclFailed")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            token_user_class,
            None,
            0,
            ctypes.byref(required),
        )
        if (
            required.value <= 0
            or ctypes.get_last_error() != error_insufficient_buffer
        ):
            raise CliError("TokenAclFailed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise CliError("TokenAclFailed")
        token_user = ctypes.cast(
            buffer,
            ctypes.POINTER(TokenUser),
        ).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            token_user.user.sid,
            ctypes.byref(sid_text),
        ):
            raise CliError("TokenAclFailed")
        try:
            value = sid_text.value
            if not value:
                raise CliError("TokenAclFailed")
            return value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _windows_token_acl_is_private(path: str, sid_text: str) -> bool:
    """Verify one protected full-control ACE for exactly *sid_text*."""

    import ctypes
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    se_dacl_protected = 0x1000
    acl_size_information_class = 2
    access_allowed_ace_type = 0
    file_all_access = 0x001F01FF

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", wintypes.BYTE),
            ("ace_flags", wintypes.BYTE),
            ("ace_size", wintypes.WORD),
        ]

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    advapi32.EqualSid.restype = wintypes.BOOL

    required = wintypes.DWORD()
    advapi32.GetFileSecurityW(
        path,
        dacl_security_information,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value <= 0:
        return False
    descriptor_buffer = ctypes.create_string_buffer(required.value)
    descriptor = ctypes.cast(descriptor_buffer, wintypes.LPVOID)
    if not advapi32.GetFileSecurityW(
        path,
        dacl_security_information,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        return False

    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        return False
    if not int(control.value) & se_dacl_protected:
        return False

    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    dacl = wintypes.LPVOID()
    if not advapi32.GetSecurityDescriptorDacl(
        descriptor,
        ctypes.byref(present),
        ctypes.byref(dacl),
        ctypes.byref(defaulted),
    ):
        return False
    if not present.value or not dacl.value:
        return False

    acl_information = AclSizeInformation()
    if not advapi32.GetAclInformation(
        dacl,
        ctypes.byref(acl_information),
        ctypes.sizeof(acl_information),
        acl_size_information_class,
    ):
        return False
    if acl_information.ace_count != 1:
        return False

    ace_pointer = wintypes.LPVOID()
    if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
        return False
    ace = ctypes.cast(
        ace_pointer,
        ctypes.POINTER(AccessAllowedAce),
    ).contents
    if (
        ace.header.ace_type != access_allowed_ace_type
        or ace.header.ace_flags != 0
        or ace.mask != file_all_access
    ):
        return False

    expected_sid = wintypes.LPVOID()
    if not advapi32.ConvertStringSidToSidW(
        sid_text,
        ctypes.byref(expected_sid),
    ):
        return False
    try:
        actual_sid = wintypes.LPVOID(
            int(ace_pointer.value)
            + AccessAllowedAce.sid_start.offset
        )
        return bool(advapi32.EqualSid(expected_sid, actual_sid))
    finally:
        kernel32.LocalFree(expected_sid)


def _secure_token_permissions(path: str) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            if stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode) != 0o600:
                raise OSError
        except OSError as exc:
            raise CliError("TokenAclFailed") from exc
        return

    import ctypes
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    sddl_revision_1 = 1

    sid_text = _windows_current_user_sid()
    security_descriptor = wintypes.LPVOID()
    security_descriptor_size = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL

    sddl = f"D:P(A;;FA;;;{sid_text})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        sddl_revision_1,
        ctypes.byref(security_descriptor),
        ctypes.byref(security_descriptor_size),
    ):
        raise CliError("TokenAclFailed")
    try:
        if not advapi32.SetFileSecurityW(
            path,
            (
                dacl_security_information
                | protected_dacl_security_information
            ),
            security_descriptor,
        ):
            raise CliError("TokenAclFailed")
    finally:
        kernel32.LocalFree(security_descriptor)

    if not _windows_token_acl_is_private(path, sid_text):
        raise CliError("TokenAclFailed")


def _remove_created_token(
    destination: Path,
    identity: FileIdentity | None,
) -> None:
    try:
        if not destination.exists():
            return
        current_identity = get_identity(str(destination))
        expected = identity or current_identity
        if current_identity != expected or not delete_verified(
            str(destination),
            expected,
        ):
            raise OSError
    except (OSError, FileSafetyError) as exc:
        raise CliError("TokenCleanupFailed") from exc


def _create_token_file(path: str) -> None:
    destination = Path(path)
    created = False
    identity: FileIdentity | None = None
    try:
        if _windows_path_is_network(str(destination)):
            raise CliError("TokenPathNetworkRejected")
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(destination, flags, 0o600)
        created = True
        try:
            token = secrets.token_urlsafe(48).encode("ascii") + b"\n"
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            identity = get_identity(str(destination))
            _secure_token_permissions(str(destination))
            if get_identity(str(destination)) != identity:
                raise CliError("TokenAclFailed")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except FileExistsError as exc:
        raise CliError("TokenFileExists") from exc
    except CliError:
        if created:
            _remove_created_token(destination, identity)
        raise
    except (OSError, FileSafetyError) as exc:
        if created:
            _remove_created_token(destination, identity)
        raise CliError("TokenCreateFailed") from exc


def _failure_category(exc: BaseException) -> str:
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category:
        return category
    enum_value = getattr(category, "value", None)
    if isinstance(enum_value, str) and enum_value:
        return enum_value
    return "ServiceFailed"


def main(argv: Sequence[str] | None = None) -> int:
    """Load external configuration and run the selected LAN service role."""

    try:
        cli_args = _build_parser().parse_args(
            list(sys.argv[1:] if argv is None else argv)
        )
        args = _load_config(cli_args.config or default_config_path())
        if cli_args.create_token:
            _create_token_file(args.token_file)
            _emit({"Event": "TokenCreated", "Status": "Ready"})
            return 0
        if cli_args.validate_config:
            _resolve_media_tools(args.ffmpeg, args.ffprobe)
            _emit(
                {
                    "Event": "ConfigValidated",
                    "Status": "Ready",
                    "Mode": args.mode,
                }
            )
            return 0
        if args.mode == "coordinator":
            return _run_coordinator(args)
        if args.mode == "helper":
            return _run_helper(args)
        raise CliError("ArgumentsInvalid", exit_code=2)
    except KeyboardInterrupt:
        _emit({"Event": "ServiceStopped", "Status": "Interrupted"})
        return 130
    except SystemExit as exc:
        return int(exc.code or 0)
    except (
        CliError,
        CoordinatorError,
        TransportError,
        WorkerError,
        OSError,
        ValueError,
    ) as exc:
        category = _failure_category(exc)
        _emit(
            {
                "Event": "ServiceStopped",
                "Status": "Failed",
                "FailureCategory": category,
            }
        )
        return int(getattr(exc, "exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
