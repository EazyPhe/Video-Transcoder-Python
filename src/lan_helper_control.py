"""Strict local command/status files for the LAN helper presence control.

The tray controller and helper exchange only small, versioned JSON documents.
This module deliberately treats missing, malformed, redirected, or linked
documents as failures so a damaged control channel cannot make the XPS helper
eligible for work by accident.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from lan_windows import FileSafetyError, assert_no_reparse_components


HELPER_CONTROL_SCHEMA_VERSION = 1
HELPER_CONTROL_MAX_BYTES = 4096
HELPER_CONTROL_MAX_REVISION = (1 << 63) - 1
HELPER_CONTROL_GATE_TIMEOUT_SECONDS = 10.0
HELPER_CONTROL_GATE_MAX_TIMEOUT_SECONDS = 60.0

_CONTROL_KEYS = frozenset(
    {"schema_version", "control_id", "revision", "pc_in_use"}
)
_STATUS_KEYS = frozenset(
    {
        "schema_version",
        "control_id",
        "revision",
        "desired_state",
        "effective_state",
        "category",
        "run_id",
    }
)
_DESIRED_STATES = frozenset({"pc_in_use", "available"})
_EFFECTIVE_STATES = frozenset(
    {"pending", "paused", "available", "blocked", "working", "draining"}
)
_CONTROL_ID = re.compile(r"[0-9a-f]{32,64}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_WINDOWS_REPARSE_ATTRIBUTE = 0x00000400
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_WINDOWS_GATE_PREFIX = "Local\\VideoTranscoderLanControlGate-"


class HelperControlError(RuntimeError):
    """A path-free, stable failure raised by the helper control store."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class HelperControlCommand:
    schema_version: int
    control_id: str
    revision: int
    pc_in_use: bool


@dataclass(frozen=True)
class HelperControlStatus:
    schema_version: int
    control_id: str
    revision: int
    desired_state: str
    effective_state: str
    category: str
    run_id: str


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _coerce_exact_path(value: str | os.PathLike[str]) -> str:
    try:
        path = os.fspath(value)
    except TypeError as exc:
        raise HelperControlError("PathInvalid") from exc
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or not os.path.isabs(path)
        or os.path.normpath(path) != path
    ):
        raise HelperControlError("PathInvalid")
    if os.name == "nt":
        drive, tail = os.path.splitdrive(path)
        if not drive or path.startswith(("\\\\", "//")) or ":" in tail:
            raise HelperControlError("PathInvalid")
    return path


def _is_local_path(path: str) -> bool:
    if os.name != "nt":
        return True
    drive, _tail = os.path.splitdrive(path)
    root = drive + "\\"
    # DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_CDROM, and DRIVE_RAMDISK are local.
    try:
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError, ValueError):
        return False
    return drive_type in {2, 3, 5, 6}


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_regular_single_link(
    info: os.stat_result,
    category: str,
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or _is_reparse(info)
        or info.st_nlink != 1
    ):
        raise HelperControlError(category)


def _check_path_components(path: str, category: str) -> None:
    if not _is_local_path(path):
        raise HelperControlError(category)
    try:
        assert_no_reparse_components(path)
        parent = os.lstat(os.path.dirname(path))
    except (FileSafetyError, OSError) as exc:
        raise HelperControlError(category) from exc
    if not stat.S_ISDIR(parent.st_mode) or _is_reparse(parent):
        raise HelperControlError(category)


def _read_bounded_file(
    path: str,
    *,
    missing_category: str,
    unsafe_category: str,
    read_category: str,
) -> bytes:
    _check_path_components(path, unsafe_category)
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise HelperControlError(missing_category) from exc
    except OSError as exc:
        raise HelperControlError(read_category) from exc
    _require_regular_single_link(before, unsafe_category)
    if before.st_size > HELPER_CONTROL_MAX_BYTES:
        raise HelperControlError(read_category)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened, unsafe_category)
        if not _same_file(before, opened):
            raise HelperControlError(unsafe_category)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(HELPER_CONTROL_MAX_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        if len(raw) > HELPER_CONTROL_MAX_BYTES:
            raise HelperControlError(read_category)
        if (
            not _same_file(opened, after_read)
            or opened.st_size != after_read.st_size
            or opened.st_mtime_ns != after_read.st_mtime_ns
        ):
            raise HelperControlError(unsafe_category)
        after_path = os.lstat(path)
        _require_regular_single_link(after_path, unsafe_category)
        if not _same_file(opened, after_path):
            raise HelperControlError(unsafe_category)
        return raw
    except HelperControlError:
        raise
    except FileNotFoundError as exc:
        raise HelperControlError(missing_category) from exc
    except OSError as exc:
        raise HelperControlError(read_category) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_object(raw: bytes, invalid_category: str) -> dict[str, Any]:
    if not raw:
        raise HelperControlError(invalid_category)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
    ) as exc:
        raise HelperControlError(invalid_category) from exc
    if not isinstance(value, dict):
        raise HelperControlError(invalid_category)
    return value


def _valid_revision(value: object) -> bool:
    return (
        type(value) is int
        and 1 <= value <= HELPER_CONTROL_MAX_REVISION
    )


def _validate_control_id(value: object) -> bool:
    return isinstance(value, str) and _CONTROL_ID.fullmatch(value) is not None


def _command_from_object(
    value: Mapping[str, Any],
    expected_control_id: str,
) -> HelperControlCommand:
    if (
        set(value) != _CONTROL_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != HELPER_CONTROL_SCHEMA_VERSION
        or value.get("control_id") != expected_control_id
        or not _validate_control_id(value.get("control_id"))
        or not _valid_revision(value.get("revision"))
        or type(value.get("pc_in_use")) is not bool
    ):
        raise HelperControlError("ControlInvalid")
    return HelperControlCommand(
        schema_version=HELPER_CONTROL_SCHEMA_VERSION,
        control_id=expected_control_id,
        revision=value["revision"],
        pc_in_use=value["pc_in_use"],
    )


def _valid_category(value: object) -> bool:
    return isinstance(value, str) and (
        value == "" or _CATEGORY.fullmatch(value) is not None
    )


def _valid_run_id(value: object) -> bool:
    return isinstance(value, str) and (
        value == "" or _RUN_ID.fullmatch(value) is not None
    )


def _status_from_object(
    value: Mapping[str, Any],
    expected_control_id: str,
) -> HelperControlStatus:
    if (
        set(value) != _STATUS_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != HELPER_CONTROL_SCHEMA_VERSION
        or value.get("control_id") != expected_control_id
        or not _validate_control_id(value.get("control_id"))
        or not _valid_revision(value.get("revision"))
        or not isinstance(value.get("desired_state"), str)
        or value.get("desired_state") not in _DESIRED_STATES
        or not isinstance(value.get("effective_state"), str)
        or value.get("effective_state") not in _EFFECTIVE_STATES
        or not _valid_category(value.get("category"))
        or not _valid_run_id(value.get("run_id"))
    ):
        raise HelperControlError("StatusInvalid")
    return HelperControlStatus(
        schema_version=HELPER_CONTROL_SCHEMA_VERSION,
        control_id=expected_control_id,
        revision=value["revision"],
        desired_state=value["desired_state"],
        effective_state=value["effective_state"],
        category=value["category"],
        run_id=value["run_id"],
    )


def _existing_destination_is_safe(
    path: str,
    unsafe_category: str,
) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HelperControlError(unsafe_category) from exc
    _require_regular_single_link(info, unsafe_category)


def _gate_digest(control_file: str, control_id: str) -> str:
    identity = (
        os.path.normcase(control_file) + "\0" + control_id
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(identity).hexdigest()


def _validate_gate_timeout(timeout_seconds: object) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
        or float(timeout_seconds) > HELPER_CONTROL_GATE_MAX_TIMEOUT_SECONDS
    ):
        raise HelperControlError("ControlGateInvalid")
    return float(timeout_seconds)


@contextlib.contextmanager
def _windows_claim_gate(
    control_file: str,
    control_id: str,
    timeout_seconds: float,
) -> Iterator[None]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    name = _WINDOWS_GATE_PREFIX + _gate_digest(control_file, control_id)
    handle_value = kernel32.CreateMutexW(None, 0, name)
    if not handle_value:
        raise HelperControlError("ControlGateUnavailable")
    handle = ctypes.c_void_p(handle_value)
    acquired = False
    body_failed = False
    try:
        timeout_ms = max(1, math.ceil(timeout_seconds * 1000.0))
        result = int(kernel32.WaitForSingleObject(handle, timeout_ms))
        if result == _WAIT_TIMEOUT:
            raise HelperControlError("ControlGateTimeout")
        if result == _WAIT_FAILED:
            raise HelperControlError("ControlGateUnavailable")
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            raise HelperControlError("ControlGateUnavailable")
        acquired = True
        try:
            yield
        except BaseException:
            body_failed = True
            raise
    finally:
        release_failed = False
        if acquired and not kernel32.ReleaseMutex(handle):
            release_failed = True
        if not kernel32.CloseHandle(handle):
            release_failed = True
        if release_failed and not body_failed:
            raise HelperControlError("ControlGateReleaseFailed")


@contextlib.contextmanager
def _portable_claim_gate(
    control_file: str,
    control_id: str,
    timeout_seconds: float,
) -> Iterator[None]:
    import fcntl

    digest = _gate_digest(control_file, control_id)
    gate_file = os.fspath(
        Path(control_file).with_name(
            f".lan-helper-control-{digest}.claim.lock"
        )
    )
    _check_path_components(gate_file, "ControlGateUnsafe")
    _existing_destination_is_safe(gate_file, "ControlGateUnsafe")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    acquired = False
    body_failed = False
    try:
        descriptor = os.open(gate_file, flags, 0o600)
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened, "ControlGateUnsafe")
        after_path = os.lstat(gate_file)
        _require_regular_single_link(after_path, "ControlGateUnsafe")
        if not _same_file(opened, after_path):
            raise HelperControlError("ControlGateUnsafe")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise HelperControlError(
                        "ControlGateUnavailable"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HelperControlError("ControlGateTimeout") from exc
                time.sleep(min(0.01, remaining))
        try:
            yield
        except BaseException:
            body_failed = True
            raise
    except HelperControlError:
        raise
    except OSError as exc:
        raise HelperControlError("ControlGateUnavailable") from exc
    finally:
        release_failed = False
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                release_failed = True
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                release_failed = True
        if release_failed and not body_failed:
            raise HelperControlError("ControlGateReleaseFailed")


def _atomic_write_object(
    path: str,
    value: Mapping[str, Any],
    *,
    unsafe_category: str,
    write_category: str,
) -> None:
    try:
        raw = (
            json.dumps(
                dict(value),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HelperControlError(write_category) from exc
    if not raw or len(raw) > HELPER_CONTROL_MAX_BYTES:
        raise HelperControlError(write_category)

    _check_path_components(path, unsafe_category)
    _existing_destination_is_safe(path, unsafe_category)
    destination = Path(path)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temporary, "xb") as handle:
            created = os.fstat(handle.fileno())
            _require_regular_single_link(created, unsafe_category)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _existing_destination_is_safe(path, unsafe_category)
        os.replace(temporary, destination)
        _existing_destination_is_safe(path, unsafe_category)
    except HelperControlError:
        raise
    except OSError as exc:
        raise HelperControlError(write_category) from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


class HelperControlStore:
    """Read and atomically publish one helper control/status file pair."""

    def __init__(
        self,
        control_file: str | os.PathLike[str],
        status_file: str | os.PathLike[str],
        control_id: str,
    ):
        self.control_file = _coerce_exact_path(control_file)
        self.status_file = _coerce_exact_path(status_file)
        if not _validate_control_id(control_id):
            raise HelperControlError("ControlInvalid")
        if os.path.normcase(self.control_file) == os.path.normcase(
            self.status_file
        ):
            raise HelperControlError("PathInvalid")
        self.control_id = control_id

    @contextlib.contextmanager
    def claim_gate(
        self,
        timeout_seconds: float = HELPER_CONTROL_GATE_TIMEOUT_SECONDS,
    ) -> Iterator[None]:
        """Serialize one command write or final claim-selection check.

        Callers must release this gate immediately after claim/source selection;
        it must never be held while encoding, uploading, or waiting on a remote
        transaction.
        """

        timeout = _validate_gate_timeout(timeout_seconds)
        _check_path_components(self.control_file, "ControlGateUnsafe")
        _existing_destination_is_safe(
            self.control_file,
            "ControlGateUnsafe",
        )
        gate = (
            _windows_claim_gate
            if os.name == "nt"
            else _portable_claim_gate
        )
        with gate(self.control_file, self.control_id, timeout):
            # The path may have changed while this caller waited. Recheck under
            # the gate before allowing any final eligibility decision.
            _check_path_components(self.control_file, "ControlGateUnsafe")
            _existing_destination_is_safe(
                self.control_file,
                "ControlGateUnsafe",
            )
            yield

    def read_command(self) -> HelperControlCommand:
        raw = _read_bounded_file(
            self.control_file,
            missing_category="ControlMissing",
            unsafe_category="ControlUnsafe",
            read_category="ControlInvalid",
        )
        value = _decode_object(raw, "ControlInvalid")
        return _command_from_object(value, self.control_id)

    def read_status(self) -> HelperControlStatus:
        raw = _read_bounded_file(
            self.status_file,
            missing_category="StatusMissing",
            unsafe_category="StatusUnsafe",
            read_category="StatusInvalid",
        )
        value = _decode_object(raw, "StatusInvalid")
        return _status_from_object(value, self.control_id)

    def write_status(
        self,
        command: HelperControlCommand,
        *,
        effective_state: str,
        category: str,
        run_id: str = "",
    ) -> HelperControlStatus:
        if type(command) is not HelperControlCommand:
            raise HelperControlError("StatusInvalid")
        try:
            validated_command = _command_from_object(
                {
                    "schema_version": command.schema_version,
                    "control_id": command.control_id,
                    "revision": command.revision,
                    "pc_in_use": command.pc_in_use,
                },
                self.control_id,
            )
        except HelperControlError as exc:
            raise HelperControlError("StatusInvalid") from exc
        value = {
            "schema_version": HELPER_CONTROL_SCHEMA_VERSION,
            "control_id": self.control_id,
            "revision": validated_command.revision,
            "desired_state": (
                "pc_in_use" if validated_command.pc_in_use else "available"
            ),
            "effective_state": effective_state,
            "category": category,
            "run_id": run_id,
        }
        status = _status_from_object(value, self.control_id)
        _atomic_write_object(
            self.status_file,
            value,
            unsafe_category="StatusUnsafe",
            write_category="StatusWriteFailed",
        )
        return status


def set_desired_state(
    control_file: str | os.PathLike[str],
    status_file: str | os.PathLike[str],
    control_id: str,
    pc_in_use: bool,
) -> HelperControlCommand:
    """Publish one explicit desired state without touching helper-owned status."""

    if type(pc_in_use) is not bool:
        raise HelperControlError("ControlInvalid")
    store = HelperControlStore(control_file, status_file, control_id)
    with store.claim_gate():
        try:
            previous = store.read_command()
        except HelperControlError as exc:
            if exc.category != "ControlMissing":
                raise
            revision = 1
        else:
            if previous.revision >= HELPER_CONTROL_MAX_REVISION:
                raise HelperControlError("ControlRevisionExhausted")
            revision = previous.revision + 1

        command = HelperControlCommand(
            schema_version=HELPER_CONTROL_SCHEMA_VERSION,
            control_id=control_id,
            revision=revision,
            pc_in_use=pc_in_use,
        )
        value = {
            "schema_version": command.schema_version,
            "control_id": command.control_id,
            "revision": command.revision,
            "pc_in_use": command.pc_in_use,
        }
        _atomic_write_object(
            store.control_file,
            value,
            unsafe_category="ControlUnsafe",
            write_category="ControlWriteFailed",
        )
        return command
