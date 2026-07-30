"""Windows file-identity and handle-safe mutation primitives.

The storage-host coordinator uses these functions to pin sources, reject
reparse points, flush candidates, and rename/delete only an already-verified
NTFS file identity. Compute workers must never call the mutation functions.
"""

from __future__ import annotations

import contextlib
import base64
import ctypes
import os
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


class FileSafetyError(RuntimeError):
    def __init__(self, category: str, winerror: int = 0):
        super().__init__(category)
        self.category = category
        self.winerror = winerror


@dataclass(frozen=True)
class FileIdentity:
    volume_serial_hex: str
    file_id_hex: str
    length: int
    creation_file_time: int
    last_write_file_time: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "FileIdentity":
        if not isinstance(value, dict):
            raise FileSafetyError("IdentityInvalid")
        try:
            identity = cls(
                volume_serial_hex=str(value["volume_serial_hex"]),
                file_id_hex=str(value["file_id_hex"]),
                length=int(value["length"]),
                creation_file_time=int(value["creation_file_time"]),
                last_write_file_time=int(value["last_write_file_time"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise FileSafetyError("IdentityInvalid") from exc
        if (
            len(identity.volume_serial_hex) != 16
            or len(identity.file_id_hex) != 32
            or identity.length < 0
            or any(
                character not in "0123456789ABCDEF"
                for character in (
                    identity.volume_serial_hex + identity.file_id_hex
                )
            )
        ):
            raise FileSafetyError("IdentityInvalid")
        return identity


if os.name == "nt":
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_READ_ATTRIBUTES = 0x00000080
    DELETE = 0x00010000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    FILE_RENAME_INFO_CLASS = 3
    FILE_DISPOSITION_INFO_CLASS = 4
    FILE_ID_INFO_CLASS = 18
    ERROR_FILE_EXISTS = 80
    ERROR_ALREADY_EXISTS = 183
    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class FILE_ID_128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", FILE_ID_128),
        ]

    class FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cb_data", wintypes.DWORD),
            ("pb_data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL


def _raise_last(category: str) -> None:
    error = ctypes.get_last_error() if os.name == "nt" else 0
    raise FileSafetyError(category, error)


def _filetime(value: object) -> int:
    return (
        int(getattr(value, "dwHighDateTime")) << 32
    ) | int(getattr(value, "dwLowDateTime"))


def _open_windows(
    path: str,
    access: int,
    share: int,
    flags: int = 0,
) -> int:
    handle = kernel32.CreateFileW(
        os.path.abspath(path),
        access,
        share,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        _raise_last("OpenHandleFailed")
    return int(handle)


def _identity_windows(handle: int, *, directory: bool = False) -> FileIdentity:
    information = BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(
        handle, ctypes.byref(information)
    ):
        _raise_last("IdentityUnavailable")
    attributes = int(information.dwFileAttributes)
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise FileSafetyError("ReparsePointRejected")
    is_directory = bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
    if is_directory != directory:
        raise FileSafetyError(
            "DirectoryRequired" if directory else "RegularFileRequired"
        )
    file_id = FILE_ID_INFO()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        FILE_ID_INFO_CLASS,
        ctypes.byref(file_id),
        ctypes.sizeof(file_id),
    ):
        _raise_last("FileIdUnavailable")
    # Match the hardened PowerShell runner's FILE_ID_INFO representation so
    # an existing validated ledger can be imported without weakening identity.
    identifier = bytes(file_id.file_id.identifier)
    low = int.from_bytes(identifier[:8], byteorder="little", signed=False)
    high = int.from_bytes(identifier[8:], byteorder="little", signed=False)
    file_id_hex = f"{high:016X}{low:016X}"
    length = (
        int(information.nFileSizeHigh) << 32
    ) | int(information.nFileSizeLow)
    return FileIdentity(
        volume_serial_hex=f"{int(file_id.volume_serial_number):016X}",
        file_id_hex=file_id_hex,
        length=length,
        creation_file_time=_filetime(information.ftCreationTime),
        last_write_file_time=_filetime(information.ftLastWriteTime),
    )


class PinnedFile:
    """A live handle that denies writes, renames, and deletion."""

    def __init__(self, path: str, handle: int, identity: FileIdentity):
        self.path = os.path.abspath(path)
        self.handle = handle
        self.identity = identity
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if os.name == "nt":
            kernel32.CloseHandle(self.handle)
        else:
            os.close(self.handle)

    def __enter__(self) -> "PinnedFile":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_read_pin(path: str) -> PinnedFile:
    absolute = os.path.abspath(path)
    if os.name == "nt":
        handle = _open_windows(
            absolute,
            GENERIC_READ,
            FILE_SHARE_READ,
            FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            identity = _identity_windows(handle)
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        return PinnedFile(absolute, handle, identity)

    descriptor = os.open(absolute, os.O_RDONLY)
    try:
        identity = _identity_portable(absolute)
    except Exception:
        os.close(descriptor)
        raise
    return PinnedFile(absolute, descriptor, identity)


def open_exclusive_lock(path: str) -> PinnedFile:
    """Hold a crash-released, cross-process lock on one regular file."""
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    if os.name == "nt":
        handle = kernel32.CreateFileW(
            absolute,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_ALWAYS,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            _raise_last("ExclusiveLockUnavailable")
        try:
            identity = _identity_windows(int(handle))
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        return PinnedFile(absolute, int(handle), identity)

    import fcntl

    descriptor = os.open(absolute, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = _identity_portable(absolute)
    except Exception as exc:
        os.close(descriptor)
        raise FileSafetyError("ExclusiveLockUnavailable") from exc
    return PinnedFile(absolute, descriptor, identity)


@contextlib.contextmanager
def open_directory_pin(path: str) -> Iterator[object]:
    absolute = os.path.abspath(path)
    if os.name == "nt":
        handle = _open_windows(
            absolute,
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            _identity_windows(handle, directory=True)
            yield handle
        finally:
            kernel32.CloseHandle(handle)
    else:
        descriptor = os.open(absolute, os.O_RDONLY)
        try:
            if not os.path.isdir(absolute) or os.path.islink(absolute):
                raise FileSafetyError("DirectoryRequired")
            yield descriptor
        finally:
            os.close(descriptor)


def _identity_portable(path: str) -> FileIdentity:
    if os.path.islink(path) or not os.path.isfile(path):
        raise FileSafetyError("RegularFileRequired")
    stat = os.stat(path, follow_symlinks=False)
    return FileIdentity(
        volume_serial_hex=f"{int(stat.st_dev) & ((1 << 64) - 1):016X}",
        file_id_hex=f"{int(stat.st_ino) & ((1 << 128) - 1):032X}",
        length=int(stat.st_size),
        creation_file_time=int(stat.st_ctime_ns // 100),
        last_write_file_time=int(stat.st_mtime_ns // 100),
    )


def get_identity(path: str) -> FileIdentity:
    absolute = os.path.abspath(path)
    if os.name != "nt":
        return _identity_portable(absolute)
    handle = _open_windows(
        absolute,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        return _identity_windows(handle)
    finally:
        kernel32.CloseHandle(handle)


def get_pinned_identity(pin: PinnedFile) -> FileIdentity:
    if pin.closed:
        raise FileSafetyError("PinClosed")
    if os.name == "nt":
        return _identity_windows(pin.handle)
    return _identity_portable(pin.path)


def flush_verified(path: str, expected: FileIdentity) -> FileIdentity:
    absolute = os.path.abspath(path)
    if os.name != "nt":
        current = get_identity(absolute)
        if current != expected:
            raise FileSafetyError("IdentityChangedBeforeFlush")
        with open(absolute, "r+b", buffering=0) as handle:
            os.fsync(handle.fileno())
        return get_identity(absolute)

    handle = _open_windows(
        absolute,
        GENERIC_READ | GENERIC_WRITE,
        0,
        FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        if _identity_windows(handle) != expected:
            raise FileSafetyError("IdentityChangedBeforeFlush")
        if not kernel32.FlushFileBuffers(handle):
            _raise_last("FlushFailed")
        return _identity_windows(handle)
    finally:
        kernel32.CloseHandle(handle)


def rename_verified(
    path: str,
    destination: str,
    expected: FileIdentity,
) -> bool:
    source = os.path.abspath(path)
    target = os.path.abspath(destination)
    if os.path.exists(target):
        return False
    if os.name != "nt":
        if get_identity(source) != expected:
            return False
        try:
            os.rename(source, target)
        except FileExistsError:
            return False
        return not os.path.exists(source) and get_identity(target) == expected

    handle = _open_windows(
        source,
        FILE_READ_ATTRIBUTES | DELETE,
        FILE_SHARE_READ,
        FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        if _identity_windows(handle) != expected:
            return False
        name_bytes = target.encode("utf-16-le")
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        header_size = 20 if pointer_size == 8 else 12
        buffer = ctypes.create_string_buffer(header_size + len(name_bytes) + 2)
        if pointer_size == 8:
            struct.pack_into("<?", buffer, 0, False)
            struct.pack_into("<Q", buffer, 8, 0)
            struct.pack_into("<I", buffer, 16, len(name_bytes))
        else:
            struct.pack_into("<?", buffer, 0, False)
            struct.pack_into("<I", buffer, 4, 0)
            struct.pack_into("<I", buffer, 8, len(name_bytes))
        ctypes.memmove(
            ctypes.addressof(buffer) + header_size,
            name_bytes,
            len(name_bytes),
        )
        if not kernel32.SetFileInformationByHandle(
            handle,
            FILE_RENAME_INFO_CLASS,
            ctypes.byref(buffer),
            len(buffer),
        ):
            error = ctypes.get_last_error()
            if error in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
                return False
            raise FileSafetyError("RenameFailed", error)
    finally:
        kernel32.CloseHandle(handle)
    return (
        not os.path.exists(source)
        and os.path.exists(target)
        and get_identity(target) == expected
    )


def delete_verified(path: str, expected: FileIdentity) -> bool:
    absolute = os.path.abspath(path)
    if os.name != "nt":
        if get_identity(absolute) != expected:
            return False
        os.remove(absolute)
        return not os.path.exists(absolute)

    handle = _open_windows(
        absolute,
        FILE_READ_ATTRIBUTES | DELETE,
        FILE_SHARE_READ,
        FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        if _identity_windows(handle) != expected:
            return False
        disposition = FILE_DISPOSITION_INFO(True)
        if not kernel32.SetFileInformationByHandle(
            handle,
            FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            _raise_last("DeleteFailed")
    finally:
        kernel32.CloseHandle(handle)
    return not os.path.exists(absolute)


def prove_exclusive_access(path: str) -> bool:
    """Return true only when no other process retains a handle to *path*."""
    absolute = os.path.abspath(path)
    if not os.path.exists(absolute):
        return True
    if os.name != "nt":
        return True
    try:
        handle = _open_windows(
            absolute,
            GENERIC_READ | GENERIC_WRITE | DELETE,
            0,
            FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except FileSafetyError:
        return False
    kernel32.CloseHandle(handle)
    return True


def assert_no_reparse_components(path: str) -> None:
    current = Path(os.path.abspath(path))
    seen: set[str] = set()
    while True:
        key = os.path.normcase(str(current))
        if key in seen:
            raise FileSafetyError("PathCycleRejected")
        seen.add(key)
        if os.path.lexists(current):
            if os.name == "nt":
                attributes = int(kernel32.GetFileAttributesW(str(current)))
                if attributes == 0xFFFFFFFF:
                    _raise_last("PathAttributesUnavailable")
                if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise FileSafetyError("ReparsePathRejected")
            elif current.is_symlink():
                raise FileSafetyError("ReparsePathRejected")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _blob_from_bytes(value: bytes):
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    blob = DATA_BLOB(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def protect_text(value: str, *, description: str = "VideoTranscoderLAN") -> str:
    """Protect UTF-8 text for the current Windows user with DPAPI."""
    if not isinstance(value, str):
        raise TypeError("value must be str")
    raw = value.encode("utf-8")
    if os.name != "nt":
        return "portable:" + base64.b64encode(raw).decode("ascii")
    input_blob, input_buffer = _blob_from_bytes(raw)
    output_blob = DATA_BLOB()
    # Keep the input buffer alive through CryptProtectData.
    _ = input_buffer
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        description,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        _raise_last("ProtectDataFailed")
    try:
        protected = ctypes.string_at(output_blob.pb_data, output_blob.cb_data)
    finally:
        kernel32.LocalFree(output_blob.pb_data)
    return base64.b64encode(protected).decode("ascii")


def unprotect_text(value: str) -> str:
    """Unprotect a value created by :func:`protect_text`."""
    if not isinstance(value, str) or not value:
        raise FileSafetyError("ProtectedDataInvalid")
    if os.name != "nt":
        if not value.startswith("portable:"):
            raise FileSafetyError("ProtectedDataInvalid")
        try:
            return base64.b64decode(
                value.removeprefix("portable:"), validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise FileSafetyError("ProtectedDataInvalid") from exc
    try:
        protected = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise FileSafetyError("ProtectedDataInvalid") from exc
    input_blob, input_buffer = _blob_from_bytes(protected)
    output_blob = DATA_BLOB()
    description_pointer = wintypes.LPWSTR()
    _ = input_buffer
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description_pointer),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        _raise_last("UnprotectDataFailed")
    try:
        raw = ctypes.string_at(output_blob.pb_data, output_blob.cb_data)
    finally:
        kernel32.LocalFree(output_blob.pb_data)
        if description_pointer:
            kernel32.LocalFree(description_pointer)
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise FileSafetyError("ProtectedDataInvalid") from exc
