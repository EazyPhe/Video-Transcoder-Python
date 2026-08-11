"""Interactive, non-elevated XPS helper control tray.

The tray owns only the user's persisted availability command.  It never reads
the LAN bearer token, never stops the helper, and never cancels FFmpeg.  The
hidden helper acknowledges the command through a separate status file and
drains any active fenced attempt before entering the paused state.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from lan_helper_control import (
    HelperControlCommand,
    HelperControlError,
    HelperControlStatus,
    HelperControlStore,
    set_desired_state,
)
from lan_windows import FileSafetyError, assert_no_reparse_components


try:  # Optional during source-only tests; mandatory in the tray build.
    import pystray
    from PIL import Image, ImageDraw

    _TRAY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by packaged smoke testing
    pystray = None
    Image = None
    ImageDraw = None
    _TRAY_AVAILABLE = False


CONFIG_SCHEMA_VERSION = 1
MAXIMUM_CONFIG_BYTES = 64 * 1024
CONTROL_FILENAME = "helper-control.json"
CONTROL_STATUS_FILENAME = "helper-control-status.json"
HELPER_TASK_NAME = r"\VideoTranscoder LAN Helper"
TRAY_MUTEX_NAME = r"Local\VideoTranscoderLanTray-v1"
POLL_SECONDS = 2.0
STATUS_FRESHNESS_SECONDS = 15.0
STATUS_FUTURE_TOLERANCE_SECONDS = 2.0
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CONTROL_ID = re.compile(r"[0-9a-f]{32,64}\Z")

GREEN = (45, 138, 78)
AMBER = (210, 145, 20)
BLUE = (42, 106, 180)
RED = (180, 55, 55)
GRAY = (95, 100, 108)


class TrayError(RuntimeError):
    """One path-free, user-displayable tray failure category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class TrayConfig:
    config_path: str
    control_file: str
    control_status_file: str
    control_id: str


@dataclass(frozen=True)
class TrayView:
    label: str
    color: tuple[int, int, int]
    pc_in_use: bool
    can_retry: bool


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _exact_sibling(config_root: Path, value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TrayError("TrayConfigInvalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = config_root / candidate
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    if (
        absolute.name.casefold() != name.casefold()
        or os.path.normcase(os.fspath(absolute.parent))
        != os.path.normcase(os.fspath(config_root))
    ):
        raise TrayError("TrayConfigInvalid")
    return os.fspath(absolute)


def load_tray_config(path: str) -> TrayConfig:
    """Read only the non-secret helper-control configuration fields."""

    try:
        config_path = Path(os.path.abspath(path))
        file_stat = os.stat(config_path, follow_symlinks=False)
        if (
            config_path.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
        ):
            raise OSError("unsafe configuration identity")
        assert_no_reparse_components(os.fspath(config_path))
        raw = config_path.read_bytes()
        if not raw or len(raw) > MAXIMUM_CONFIG_BYTES:
            raise ValueError("configuration size")
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("constant")
            ),
        )
    except (
        FileSafetyError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise TrayError("TrayConfigInvalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != CONFIG_SCHEMA_VERSION
        or value.get("mode") != "helper"
    ):
        raise TrayError("TrayConfigInvalid")
    control_id = value.get("control_id")
    if (
        not isinstance(control_id, str)
        or _CONTROL_ID.fullmatch(control_id) is None
    ):
        raise TrayError("TrayConfigInvalid")
    root = config_path.parent
    control_file = _exact_sibling(
        root, value.get("control_file"), CONTROL_FILENAME
    )
    status_file = _exact_sibling(
        root, value.get("control_status_file"), CONTROL_STATUS_FILENAME
    )
    if os.path.normcase(control_file) == os.path.normcase(status_file):
        raise TrayError("TrayConfigInvalid")
    return TrayConfig(
        config_path=os.fspath(config_path),
        control_file=control_file,
        control_status_file=status_file,
        control_id=control_id,
    )


def _helper_task_command() -> list[str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return [
        os.path.join(system_root, "System32", "schtasks.exe"),
        "/Run",
        "/TN",
        HELPER_TASK_NAME,
    ]


def start_helper_task(
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Start the one fixed limited-user helper task without elevation."""

    try:
        result = runner(
            _helper_task_command(),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrayError("HelperTaskStartFailed") from exc
    if int(result.returncode) != 0:
        raise TrayError("HelperTaskStartFailed")


def _status_matches(
    command: HelperControlCommand,
    status: HelperControlStatus,
) -> bool:
    return (
        status.control_id == command.control_id
        and status.revision == command.revision
        and status.desired_state
        == ("pc_in_use" if command.pc_in_use else "available")
    )


def _safe_status_stat(path: str) -> os.stat_result:
    try:
        assert_no_reparse_components(path)
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise HelperControlError("StatusMissing") from exc
    except (FileSafetyError, OSError) as exc:
        raise HelperControlError("StatusUnsafe") from exc
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise HelperControlError("StatusUnsafe")
    return value


def _same_status_snapshot(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_nlink == after.st_nlink == 1
    )


def _read_status_freshness(
    store: HelperControlStore,
    path: str,
    *,
    now_ns: int,
) -> tuple[HelperControlStatus, bool]:
    """Read one status and bind its age to the exact validated file snapshot."""

    before = _safe_status_stat(path)
    status = store.read_status()
    after = _safe_status_stat(path)
    if not _same_status_snapshot(before, after):
        raise HelperControlError("StatusUnsafe")
    age_ns = int(now_ns) - int(after.st_mtime_ns)
    maximum_age_ns = int(STATUS_FRESHNESS_SECONDS * 1_000_000_000)
    future_tolerance_ns = int(
        STATUS_FUTURE_TOLERANCE_SECONDS * 1_000_000_000
    )
    return status, -future_tolerance_ns <= age_ns <= maximum_age_ns


def _view_for(
    command: HelperControlCommand,
    status: HelperControlStatus | None,
    action_error: str = "",
    *,
    status_stale: bool = False,
) -> TrayView:
    if action_error:
        return TrayView(
            f"Blocked: {action_error}", RED, command.pc_in_use, True
        )
    if status_stale:
        return TrayView(
            (
                "XPS disconnected - pause pending"
                if command.pc_in_use
                else "XPS disconnected - retry helper"
            ),
            AMBER if command.pc_in_use else GRAY,
            command.pc_in_use,
            True,
        )
    if status is None or not _status_matches(command, status):
        return TrayView(
            (
                "Pause requested - waiting for helper"
                if command.pc_in_use
                else "Resume requested - starting helper"
            ),
            AMBER,
            command.pc_in_use,
            True,
        )
    effective = status.effective_state
    if effective == "paused":
        return TrayView(
            "XPS paused - INSPIRON fallback", BLUE, True, True
        )
    if effective in {"working", "draining"}:
        return TrayView(
            (
                "Pausing XPS after current file"
                if command.pc_in_use
                else "XPS transcoding"
            ),
            AMBER if command.pc_in_use else GREEN,
            command.pc_in_use,
            not command.pc_in_use,
        )
    if effective == "available":
        return TrayView("XPS available", GREEN, False, True)
    if effective == "pending":
        return TrayView(
            (
                "Pause requested - waiting for safe boundary"
                if command.pc_in_use
                else "Resume requested - connecting"
            ),
            AMBER,
            command.pc_in_use,
            True,
        )
    return TrayView(
        f"Blocked: {status.category or 'HelperControlBlocked'}",
        RED,
        command.pc_in_use,
        True,
    )


class TrayController:
    """Thread-safe command writer and acknowledgment view model."""

    def __init__(
        self,
        config: TrayConfig,
        *,
        store: HelperControlStore | None = None,
        setter: Callable[..., HelperControlCommand] = set_desired_state,
        task_starter: Callable[[], None] = start_helper_task,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.config = config
        self.store = store or HelperControlStore(
            config.control_file,
            config.control_status_file,
            config.control_id,
        )
        self.setter = setter
        self.task_starter = task_starter
        self.clock_ns = clock_ns
        self._lock = threading.RLock()
        self._command: HelperControlCommand | None = None
        self._status: HelperControlStatus | None = None
        self._action_error = ""
        self._view = TrayView(
            "Helper control unavailable", GRAY, True, False
        )

    @property
    def view(self) -> TrayView:
        with self._lock:
            return self._view

    def refresh(self) -> TrayView:
        with self._lock:
            try:
                command = self.store.read_command()
            except HelperControlError as exc:
                self._command = None
                self._status = None
                self._action_error = exc.category
                self._view = TrayView(
                    f"Blocked: {exc.category}", RED, True, False
                )
                return self._view
            try:
                status, status_fresh = _read_status_freshness(
                    self.store,
                    self.config.control_status_file,
                    now_ns=self.clock_ns(),
                )
            except HelperControlError as exc:
                if exc.category != "StatusMissing":
                    self._command = command
                    self._status = None
                    self._view = TrayView(
                        f"Blocked: {exc.category}",
                        RED,
                        command.pc_in_use,
                        True,
                    )
                    return self._view
                status = None
                status_fresh = False
            status_stale = status is not None and not status_fresh
            trusted_status = status if status_fresh else None
            if trusted_status is not None and _status_matches(
                command, trusted_status
            ):
                self._action_error = ""
            self._command = command
            self._status = trusted_status
            self._view = _view_for(
                command,
                trusted_status,
                self._action_error,
                status_stale=status_stale,
            )
            return self._view

    def _set(self, pc_in_use: bool) -> TrayView:
        with self._lock:
            try:
                command = self.setter(
                    self.config.control_file,
                    self.config.control_status_file,
                    self.config.control_id,
                    pc_in_use,
                )
                self._command = command
                self._status = None
                self._action_error = ""
                self._view = _view_for(command, None)
            except HelperControlError as exc:
                self._action_error = exc.category
                self._view = TrayView(
                    f"Blocked: {exc.category}", RED, pc_in_use, True
                )
            return self._view

    def pause_after_current_file(self) -> TrayView:
        """Persist pause intent, then ensure the helper can announce/drain."""

        view = self._set(True)
        if view.color == RED:
            return view
        return self._start_task_for_command(True)

    def _start_task_for_command(self, pc_in_use: bool) -> TrayView:
        try:
            self.task_starter()
        except TrayError as exc:
            with self._lock:
                self._action_error = exc.category
                self._view = TrayView(
                    f"Blocked: {exc.category}", RED, pc_in_use, True
                )
                return self._view
        return self.view

    def resume(self) -> TrayView:
        """Persist availability, then start the fixed helper task once."""

        view = self._set(False)
        if view.color == RED:
            return view
        return self._start_task_for_command(False)

    def retry_helper(self) -> TrayView:
        with self._lock:
            command = self._command
        if command is None:
            self.refresh()
            with self._lock:
                command = self._command
        if command is None:
            return self.view
        view = self._set(command.pc_in_use)
        if view.color == RED:
            return view
        return self._start_task_for_command(command.pc_in_use)

    def start_if_available(self) -> TrayView:
        view = self.refresh()
        with self._lock:
            command = self._command
        if command is not None:
            return self.retry_helper()
        return view


class SingleInstance:
    """One tray per interactive Windows session, with a portable test fallback."""

    def __init__(self, name: str = TRAY_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None
        self._fallback_path: str | None = None

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_wchar_p,
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, 0, self.name)
            if not handle:
                raise TrayError("TraySingleInstanceFailed")
            self._handle = int(handle)
            if ctypes.get_last_error() == 183:
                self.release()
                return False
            return True
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", self.name)
        path = os.path.join(tempfile.gettempdir(), safe_name + ".lock")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.close(descriptor)
        self._fallback_path = path
        return True

    def release(self) -> None:
        if self._handle is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._fallback_path:
            try:
                os.remove(self._fallback_path)
            except FileNotFoundError:
                pass
            self._fallback_path = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise TrayError("TrayAlreadyRunning")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _tray_image(color: tuple[int, int, int]):
    if not _TRAY_AVAILABLE:
        raise TrayError("TrayDependenciesUnavailable")
    image = Image.new("RGB", (64, 64), (28, 30, 34))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=color)
    draw.rectangle((19, 18, 45, 46), fill=(245, 245, 245))
    draw.text((23, 23), "XPS", fill=color)
    return image


class TrayApplication:
    def __init__(self, controller: TrayController) -> None:
        if not _TRAY_AVAILABLE:
            raise TrayError("TrayDependenciesUnavailable")
        self.controller = controller
        self.stop_event = threading.Event()
        self.icon = pystray.Icon(
            "VideoTranscoderLanTray",
            _tray_image(GRAY),
            "Dual-PC transcoder helper",
            self._menu(),
        )

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: self.controller.view.label,
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Pause XPS after current file",
                self._pause,
                visible=lambda _item: not self.controller.view.pc_in_use,
            ),
            pystray.MenuItem(
                "Resume XPS transcoding",
                self._resume,
                visible=lambda _item: self.controller.view.pc_in_use,
            ),
            pystray.MenuItem(
                "Retry helper now",
                self._retry,
                enabled=lambda _item: self.controller.view.can_retry,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit tray", self._exit),
        )

    def _apply_view(self, view: TrayView) -> None:
        self.icon.title = view.label
        self.icon.icon = _tray_image(view.color)
        self.icon.update_menu()

    def _pause(self, _icon=None, _item=None) -> None:
        self._apply_view(self.controller.pause_after_current_file())

    def _resume(self, _icon=None, _item=None) -> None:
        self._apply_view(self.controller.resume())

    def _retry(self, _icon=None, _item=None) -> None:
        self._apply_view(self.controller.retry_helper())

    def _exit(self, _icon=None, _item=None) -> None:
        # Exiting is intentionally UI-only. Persisted intent and helper state
        # are not changed, and the hidden helper is never stopped.
        self.stop_event.set()
        self.icon.stop()

    def _poll(self) -> None:
        while not self.stop_event.wait(POLL_SECONDS):
            self._apply_view(self.controller.refresh())

    def run(self) -> None:
        self._apply_view(self.controller.start_if_available())
        threading.Thread(target=self._poll, daemon=True).start()
        self.icon.run()


def _write_self_test_report(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp"
    )
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XPS LAN helper tray")
    parser.add_argument("--config", default="")
    parser.add_argument("--self-test-report", default="")
    return parser


def _default_config_path() -> str:
    executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return os.fspath(executable.resolve().parent / "VideoTranscoderLanAssist.json")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    try:
        config = load_tray_config(args.config or _default_config_path())
        store = HelperControlStore(
            config.control_file,
            config.control_status_file,
            config.control_id,
        )
        if args.self_test_report:
            command = store.read_command()
            try:
                status = store.read_status()
                status_readable = status.control_id == command.control_id
                status_category = "" if status_readable else "StatusInvalid"
            except HelperControlError as exc:
                status_readable = False
                status_category = exc.category
            _write_self_test_report(
                args.self_test_report,
                {
                    "event": "LanTraySelfTest",
                    "status": "Ready",
                    "control_id": command.control_id,
                    "revision": command.revision,
                    "pc_in_use": command.pc_in_use,
                    "status_readable": status_readable,
                    "status_category": status_category,
                    "tray_dependencies": _TRAY_AVAILABLE,
                },
            )
            return 0 if _TRAY_AVAILABLE else 2
        controller = TrayController(config, store=store)
        with SingleInstance():
            TrayApplication(controller).run()
        return 0
    except (HelperControlError, TrayError, OSError, ValueError) as exc:
        category = getattr(exc, "category", "TrayFailed")
        if args.self_test_report:
            try:
                _write_self_test_report(
                    args.self_test_report,
                    {
                        "event": "LanTraySelfTest",
                        "status": "Failed",
                        "category": str(category),
                    },
                )
            except OSError:
                pass
        elif os.name == "nt":  # pragma: no cover - user-facing Windows path
            ctypes.WinDLL("user32", use_last_error=True).MessageBoxW(
                None,
                f"Dual-PC transcoder tray could not start ({category}).",
                "Dual-PC Transcoder",
                0x10,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
