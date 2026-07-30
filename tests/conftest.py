"""Shared pytest fixtures for host-independent media-tool tests."""

import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import transcode  # noqa: E402


@pytest.fixture(autouse=True)
def _media_tool_command_placeholders(monkeypatch):
    """Keep mocked tests independent of FFmpeg installed on the test host."""
    monkeypatch.setattr(transcode, "FFMPEG_PATH", "ffmpeg")
    monkeypatch.setattr(transcode, "FFPROBE_PATH", "ffprobe")

    gui = sys.modules.get("gui")
    if gui is not None:
        monkeypatch.setattr(gui, "FFMPEG_PATH", "ffmpeg")
        monkeypatch.setattr(gui, "FFPROBE_PATH", "ffprobe")
