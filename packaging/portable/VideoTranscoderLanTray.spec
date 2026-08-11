# -*- mode: python ; coding: utf-8 -*-
"""Windowed PyInstaller recipe for the limited-user XPS helper tray."""

from pathlib import Path


spec_dir = Path(SPECPATH).resolve()
project_root = spec_dir.parents[1]

a = Analysis(
    [str(project_root / "src" / "lan_tray.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32", "PIL.Image", "PIL.ImageDraw"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["build", "pytest", "ruff", "customtkinter"],
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
    name="VideoTranscoderLanTray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    version=str(spec_dir / "lan_assist_version_info.txt"),
)
