# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the self-contained Windows x64 application."""

import os
from pathlib import Path


spec_dir = Path(SPECPATH).resolve()
project_root = spec_dir.parents[1]
ffmpeg_path = Path(os.environ["VIDEO_TRANSCODER_FFMPEG"]).resolve()
ffprobe_path = Path(os.environ["VIDEO_TRANSCODER_FFPROBE"]).resolve()

for tool_path in (ffmpeg_path, ffprobe_path):
    if not tool_path.is_file():
        raise SystemExit(f"Required portable-build tool was not found: {tool_path}")

a = Analysis(
    [str(project_root / "src" / "gui.py")],
    pathex=[str(project_root / "src")],
    binaries=[
        (str(ffmpeg_path), "ffmpeg"),
        (str(ffprobe_path), "ffmpeg"),
    ],
    datas=[
        (
            str(spec_dir / "THIRD_PARTY_NOTICES.txt"),
            "licenses",
        ),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["build", "pytest", "ruff"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoTranscoderPortable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    version=str(spec_dir / "version_info.txt"),
)
