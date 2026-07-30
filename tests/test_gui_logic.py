"""Headless regression tests for GUI orchestration helpers."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gui import (  # noqa: E402
    QueueItem,
    TranscoderApp,
    _portable_self_test_argument,
    default_output_directory,
    encode_file_gui,
)
from transcode import (  # noqa: E402
    CODECS_CPU,
    Settings,
    capture_file_identity,
)


def _settings() -> Settings:
    settings = Settings()
    settings.codec = next(
        codec for codec in CODECS_CPU if codec.encoder == "libx264")
    return settings


def test_queue_save_preserves_active_items_as_queued(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "gui.save_queue",
        lambda document: captured.setdefault("document", document),
    )
    fake = SimpleNamespace(queue=[
        QueueItem("queued.mp4", status="queued"),
        QueueItem("active.mp4", status="encoding"),
        QueueItem("cancelled.mp4", status="cancelled"),
        QueueItem("failed.mp4", status="failed"),
        QueueItem("done.mp4", status="done"),
    ])
    TranscoderApp._save_queue_to_disk(fake)
    items = captured["document"]["items"]
    assert [item["path"] for item in items] == [
        "queued.mp4", "active.mp4", "cancelled.mp4", "failed.mp4"]
    assert [item["status"] for item in items] == [
        "queued", "queued", "queued", "failed"]


def test_close_cancels_and_joins_active_workers(tmp_path):
    encoding = MagicMock()
    encoding.is_alive.return_value = True
    analysis = MagicMock()
    analysis.is_alive.return_value = True
    fake = SimpleNamespace(
        _analysis_thread=analysis,
        encoding_thread=encoding,
        _is_encoding=True,
        _closing=False,
        process_control=MagicMock(),
        _save_queue_to_disk=MagicMock(),
        _save_geometry=MagicMock(),
        _tray_icon=None,
        destroy=MagicMock(),
    )
    with patch("gui.messagebox.askyesno", return_value=True):
        TranscoderApp._on_close(fake)
    assert fake._closing
    fake.process_control.cancel.assert_called_once()
    encoding.join.assert_called_once_with(timeout=3.0)
    analysis.join.assert_called_once_with(timeout=3.0)
    fake._save_queue_to_disk.assert_called_once()
    fake.destroy.assert_called_once()

    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    delete_host = SimpleNamespace(
        process_control=SimpleNamespace(cancelled=True),
        after=lambda _delay, callback: callback(),
        _log_ts=MagicMock(),
    )
    with patch("gui.messagebox.askyesno", return_value=True):
        TranscoderApp._handle_delete(
            delete_host,
            str(source),
            "ask",
            capture_file_identity(str(source)),
        )
    assert source.read_bytes() == b"original"
    assert "cancellation" in delete_host._log_ts.call_args.args[0]


def test_legacy_gui_wrapper_rejects_ignored_pass_controls():
    with pytest.raises(ValueError, match="engine-managed"):
        encode_file_gui(
            "source.mp4",
            "output.mp4",
            _settings(),
            pass_number=1,
        )


def test_portable_self_test_argument_supports_explicit_and_default_paths(
    tmp_path,
    monkeypatch,
):
    explicit = tmp_path / "remote report.json"
    assert _portable_self_test_argument(
        ["--portable-self-test", str(explicit)]
    ) == str(explicit)

    monkeypatch.setenv("TEMP", str(tmp_path))
    assert _portable_self_test_argument(["--self-test-report"]) == str(
        tmp_path / "VideoTranscoderPortableSelfTest" / "report.json"
    )
    assert _portable_self_test_argument(["video.mp4"]) is None


def test_setup_dnd_loads_tkdnd_before_registering(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        drop_target_register=lambda value: calls.append(("register", value)),
        dnd_bind=lambda event, callback: calls.append(("bind", event)),
        _on_drop=MagicMock(),
    )
    monkeypatch.setattr("gui._HAS_DND", True)
    require = MagicMock(side_effect=lambda root: calls.append(("require", root)))
    monkeypatch.setattr("gui.tkinterdnd2.TkinterDnD.require", require)

    TranscoderApp._setup_dnd(fake)

    assert calls[0] == ("require", fake)
    assert calls[1][0] == "register"


def test_portable_output_defaults_to_executable_directory(
    tmp_path,
    monkeypatch,
):
    executable = tmp_path / "portable folder" / "VideoTranscoderPortable.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test")
    monkeypatch.setattr("gui.sys.frozen", True, raising=False)
    monkeypatch.setattr("gui.sys.executable", str(executable))
    monkeypatch.setattr(
        "gui.sys._MEIPASS",
        str(tmp_path / "temporary extraction"),
        raising=False,
    )

    assert default_output_directory() == str(executable.parent.resolve())


def test_source_output_keeps_compressed_default(monkeypatch):
    monkeypatch.delattr("gui.sys.frozen", raising=False)

    assert default_output_directory() == "compressed"


def test_output_picker_starts_at_current_folder_and_updates_selection(
    tmp_path,
    monkeypatch,
):
    current = tmp_path / "current"
    selected = tmp_path / "selected"
    current.mkdir()
    selected.mkdir()
    chooser = MagicMock(return_value=str(selected))
    monkeypatch.setattr("gui.filedialog.askdirectory", chooser)
    fake = SimpleNamespace(
        output_dir=str(current),
        output_label=MagicMock(),
    )

    TranscoderApp._change_output_dir(fake)

    chooser.assert_called_once_with(
        title="Select Output Folder",
        initialdir=str(current.resolve()),
    )
    assert fake.output_dir == str(selected)
    fake.output_label.configure.assert_called_once_with(text=str(selected))

    chooser.reset_mock()
    fake.output_label.configure.reset_mock()
    chooser.return_value = ""
    TranscoderApp._change_output_dir(fake)

    assert fake.output_dir == str(selected)
    fake.output_label.configure.assert_not_called()
