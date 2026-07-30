#!/usr/bin/env python3
"""
Video Transcoder v3.2 (Python)
Compress and convert video files using FFmpeg with real-time progress.
Supports NVIDIA NVENC GPU acceleration, multiple codecs, presets, and batch processing.
"""

import copy
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app_state import (
    atomic_update_mapping,
    atomic_write_json,
    migrate_legacy_state,
    read_json,
    resolve_app_paths,
)

try:
    import psutil
except ImportError:  # pragma: no cover - platform fallback remains available
    psutil = None

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.prompt import Prompt, IntPrompt
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("\n  Missing 'rich' library. Install it with:")
    print("  pip install rich\n")
    sys.exit(1)

# ============================================================
#  CONFIGURATION
# ============================================================

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
OUTPUT_DIR = "compressed"
APP_PATHS = resolve_app_paths()
LOG_FILE = str(APP_PATHS.log)
CONFIG_FILE = str(APP_PATHS.config)

console = Console()
SUBPROCESS_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt"
    else 0
)

# Common FFmpeg install locations on Windows (searched in order)
_FFMPEG_SEARCH_DIRS: list[str] = [
    r"C:\ffmpeg",
    r"C:\Program Files\ffmpeg",
    r"C:\Program Files (x86)\ffmpeg",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg"),
    os.path.join(os.environ.get("USERPROFILE", ""), "ffmpeg"),
]


def default_output_directory() -> str:
    """Return the initial output directory for the active runtime.

    Frozen apps use the containing folder of the user-visible executable.
    ``sys._MEIPASS`` is intentionally ignored because it is a temporary
    PyInstaller extraction directory. Source launches retain ``compressed``.
    """
    if not getattr(sys, "frozen", False):
        return OUTPUT_DIR
    try:
        return str(Path(sys.executable).resolve().parent)
    except OSError:
        return os.path.dirname(os.path.abspath(sys.executable))


def _find_executable(name: str) -> str:
    """
    Locate an FFmpeg executable by *name* (e.g. 'ffmpeg' or 'ffprobe').

    Search order:
      1. Bundled next to the application in a frozen portable build.
      2. Already resolved & cached in the config file.
      3. On the system PATH  (shutil.which).
      4. Common Windows install directories (recursive glob for <name>.exe).
    Returns the absolute path, or an empty string if not found.
    """
    # A frozen build must always use the matching bundled pair instead of a
    # stale saved path or another FFmpeg installation on the host.
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        exe_name = f"{name}.exe" if sys.platform == "win32" else name
        for candidate in (
            Path(bundle_root) / "ffmpeg" / exe_name,
            Path(bundle_root) / exe_name,
        ):
            if candidate.is_file():
                return str(candidate.resolve())

    # 2. Check config file for a previously saved path
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            saved = cfg.get(f"{name}_path", "")
            if saved and os.path.isfile(saved):
                return saved
    except (json.JSONDecodeError, OSError):
        pass

    # 3. System PATH
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    # 4. Common directories (look for <name>.exe recursively)
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    for base in _FFMPEG_SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for match in Path(base).rglob(exe_name):
            if match.is_file():
                return str(match.resolve())

    return ""


def _resolve_ffmpeg_paths() -> tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path), searching automatically."""
    ffmpeg = _find_executable("ffmpeg")
    ffprobe = _find_executable("ffprobe")
    return ffmpeg, ffprobe


# Resolve once at import time
FFMPEG_PATH, FFPROBE_PATH = _resolve_ffmpeg_paths()


def initialize_app_state() -> tuple[str, str]:
    """Run one-time state migration at application startup.

    Keeping migration out of module import makes library use and the test suite
    read-only.  Executable entry points call this before constructing the UI or
    starting the CLI.
    """
    migrate_legacy_state(APP_PATHS)
    global FFMPEG_PATH, FFPROBE_PATH
    FFMPEG_PATH, FFPROBE_PATH = _resolve_ffmpeg_paths()
    return FFMPEG_PATH, FFPROBE_PATH


# ============================================================
#  DATA CLASSES
# ============================================================


@dataclass
class CodecOption:
    name: str
    encoder: str
    args: list[str]
    crf_flag: str  # "-crf", "-cq", "-global_quality", "-qp_p"
    crf_values: dict[str, int]  # {"high": 20, "medium": 28, "low": 34}
    requires_gpu: bool = False
    gpu_vendor: str = ""  # "nvidia", "amd", "intel", or "" for CPU


@dataclass
class Settings:
    codec: Optional[CodecOption] = None
    quality: str = "medium"
    resolution: Optional[str] = None  # None = original, "1080", "720", "480"
    fps: Optional[int] = None  # None = original
    audio_bitrate: str = "128k"
    audio_codec: str = "aac"  # "aac", "opus", "copy"
    output_format: str = "mp4"
    subtitle_mode: str = "keep"  # "keep", "burn", "strip"
    delete_originals: str = "no"  # "no", "yes", "ask"
    skip_existing: bool = True
    hwaccel: bool = False  # use -hwaccel cuda for GPU decode
    mode: str = "batch"  # "batch", "single", "preview"
    target_file: Optional[str] = None  # for single/preview/dragdrop
    ten_bit: bool = False  # 10-bit pixel format
    two_pass: bool = False  # 2-pass encoding (CPU codecs only)
    filename_template: str = "{name}"  # output filename template
    trim_start: Optional[float] = None  # trim start in seconds
    trim_end: Optional[float] = None  # trim end in seconds
    post_action: str = "none"  # "none", "shutdown", "sleep", "command"
    post_command: str = ""  # custom command for post_action="command"
    concurrent: int = 1  # number of parallel encodes
    auto_crop: bool = False  # auto-detect and crop black bars
    audio_extract: bool = False  # extract audio only (no video)
    audio_extract_format: str = "mp3"  # mp3, aac, flac, opus
    notification_sound: bool = True  # play completion sound
    notification_toast: bool = True  # show toast notification
    # Phase 5: HDR support
    hdr_mode: str = "auto"  # "auto", "passthrough", "tonemap", "off"
    # Phase 7: Video filter chain
    video_filters: list[str] | None = None  # extra -vf filters
    # Phase 11: Bitrate modes
    bitrate_mode: str = "crf"  # "crf", "cbr", "vbr", "filesize"
    target_bitrate: str = ""  # e.g. "6000k" for CBR/VBR
    max_bitrate: str = ""  # e.g. "8000k" for VBR ceiling
    target_size_mb: float = 0  # target file size in MB (filesize mode)
    # Phase 12: Advanced codec options
    advanced_args: list[str] | None = None  # extra FFmpeg args
    # Phase 15: Network / cloud output
    post_upload: str = ""  # deprecated legacy field (never executed)
    post_copy_dir: str = ""  # safe atomic copy destination
    vmaf_enabled: bool = False
    vmaf_sample_seconds: int = 30


PORTABLE_OVERRIDE_FIELDS = {
    "codec_encoder",
    "quality",
    "resolution",
    "fps",
    "audio_bitrate",
    "audio_codec",
    "output_format",
    "subtitle_mode",
    "skip_existing",
    "hwaccel",
    "ten_bit",
    "two_pass",
    "concurrent",
    "auto_crop",
    "audio_extract",
    "audio_extract_format",
    "hdr_mode",
    "bitrate_mode",
    "target_bitrate",
    "max_bitrate",
    "target_size_mb",
    "trim_start",
    "trim_end",
    "filename_template",
    "vmaf_enabled",
    "vmaf_sample_seconds",
}


def settings_to_dict(settings: "Settings") -> dict:
    """Serialize every setting using an encoder name instead of an object."""
    result: dict = {}
    for definition in fields(Settings):
        if definition.name == "codec":
            continue
        result[definition.name] = copy.deepcopy(
            getattr(settings, definition.name))
    result["codec_encoder"] = (
        settings.codec.encoder if settings.codec else None)
    return result


def apply_settings_override(
    base: "Settings",
    override: dict,
    available_codecs: list["CodecOption"],
) -> "Settings":
    """Return an isolated Settings copy with a validated sparse override."""
    if not isinstance(override, dict):
        raise ValueError("Settings override must be an object.")
    valid_fields = {definition.name for definition in fields(Settings)}
    unknown = set(override) - valid_fields - {"codec_encoder"}
    if unknown:
        raise ValueError(
            f"Unknown override field(s): {', '.join(sorted(unknown))}")

    result = copy.deepcopy(base)
    if "codec_encoder" in override:
        encoder = override["codec_encoder"]
        codec_map = {codec.encoder: codec for codec in available_codecs}
        if not isinstance(encoder, str) or encoder not in codec_map:
            raise ValueError(f"Encoder is not available: {encoder!r}")
        result.codec = codec_map[encoder]

    boolean_fields = {
        "skip_existing", "hwaccel", "ten_bit", "two_pass", "auto_crop",
        "audio_extract", "notification_sound", "notification_toast",
        "vmaf_enabled",
    }
    nullable_integer_fields = {"fps"}
    required_integer_fields = {"concurrent", "vmaf_sample_seconds"}
    nullable_numeric_fields = {"trim_start", "trim_end"}
    required_numeric_fields = {"target_size_mb"}
    list_fields = {"video_filters", "advanced_args"}
    nullable_string_fields = {"resolution"}
    required_string_fields = {
        "quality", "audio_bitrate", "audio_codec",
        "output_format", "subtitle_mode", "delete_originals", "mode",
        "target_file", "filename_template", "post_action", "post_command",
        "audio_extract_format", "hdr_mode", "bitrate_mode",
        "target_bitrate", "max_bitrate", "post_upload", "post_copy_dir",
    }
    for name, value in override.items():
        if name in ("codec", "codec_encoder"):
            continue
        if name in boolean_fields and not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false.")
        if (
            name in nullable_integer_fields
            and value is not None
            and (not isinstance(value, int) or isinstance(value, bool))
        ):
            raise ValueError(f"{name} must be an integer or null.")
        if (
            name in required_integer_fields
            and (not isinstance(value, int) or isinstance(value, bool))
        ):
            raise ValueError(f"{name} must be an integer.")
        if (
            name in nullable_numeric_fields
            and value is not None
            and (
                not isinstance(value, (int, float))
                or isinstance(value, bool) or not math.isfinite(value)
            )
        ):
            raise ValueError(f"{name} must be finite numeric or null.")
        if (
            name in required_numeric_fields
            and (
                not isinstance(value, (int, float))
                or isinstance(value, bool) or not math.isfinite(value)
            )
        ):
            raise ValueError(f"{name} must be finite numeric.")
        if (
            name in nullable_string_fields
            and value is not None and not isinstance(value, str)
        ):
            raise ValueError(f"{name} must be text or null.")
        if name in required_string_fields and not isinstance(value, str):
            raise ValueError(f"{name} must be text.")
        if name in list_fields:
            if (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                raise ValueError(f"{name} must be a list of strings.")
        setattr(result, name, copy.deepcopy(value))

    enum_values = {
        "quality": {"high", "medium", "low"},
        "audio_codec": {"aac", "opus", "copy"},
        "output_format": {"mp4", "mkv", "mov"},
        "subtitle_mode": {"keep", "burn", "strip"},
        "delete_originals": {"no", "yes", "ask"},
        "audio_extract_format": {"mp3", "aac", "flac", "opus"},
        "hdr_mode": {"auto", "passthrough", "tonemap", "off"},
        "bitrate_mode": {"crf", "cbr", "vbr", "filesize"},
    }
    for name, allowed in enum_values.items():
        if getattr(result, name) not in allowed:
            raise ValueError(
                f"Invalid {name}: {getattr(result, name)!r}.")
    if result.resolution not in (None, "1080", "720", "480"):
        raise ValueError(f"Invalid resolution: {result.resolution!r}.")
    if result.fps is not None and result.fps <= 0:
        raise ValueError("Frame rate must be greater than zero.")
    if not 1 <= result.concurrent <= 4:
        raise ValueError("Concurrent encodes must be between 1 and 4.")
    if not 1 <= result.vmaf_sample_seconds <= 300:
        raise ValueError("VMAF sample length must be between 1 and 300 seconds.")
    if result.trim_start is not None and result.trim_start < 0:
        raise ValueError("Trim start cannot be negative.")
    if result.trim_end is not None and result.trim_end < 0:
        raise ValueError("Trim end cannot be negative.")
    if (
        "filename_template" in override
        and any(token in result.filename_template for token in ("/", "\\", ":"))
    ):
        raise ValueError(
            "Filename templates cannot contain path separators or drive names.")

    warnings = validate_settings(result)
    fatal_markers = (
        "No codec selected",
        "Trim start must",
        "requires a target size",
        "requires a target bitrate",
        "requires a valid positive target bitrate",
        "Maximum bitrate must",
    )
    fatal = [
        warning for warning in warnings
        if any(marker in warning for marker in fatal_markers)
    ]
    if fatal:
        raise ValueError(" ".join(fatal))
    return result


def settings_override_diff(
    base: "Settings",
    candidate: "Settings",
) -> dict:
    """Return only fields that differ from the inherited base settings."""
    base_data = settings_to_dict(base)
    candidate_data = settings_to_dict(candidate)
    return {
        key: copy.deepcopy(value)
        for key, value in candidate_data.items()
        if value != base_data.get(key)
    }


def sanitize_portable_override(override: dict) -> dict:
    """Drop dangerous fields from an imported/shareable queue override."""
    if not isinstance(override, dict):
        return {}
    result: dict = {}
    for key, value in override.items():
        if key not in PORTABLE_OVERRIDE_FIELDS:
            continue
        if key == "filename_template":
            if not isinstance(value, str):
                continue
            try:
                validate_output_filename(f"{value}.mp4")
            except ValueError:
                continue
        result[key] = copy.deepcopy(value)
    return result


def merge_portable_overrides(
    base: "Settings",
    global_override: dict,
    item_override: dict,
    available_codecs: list["CodecOption"],
) -> dict:
    """Materialize exported global and item settings against a local base.

    The returned sparse override reproduces the exported effective settings on
    the importing machine while retaining the portable-field allowlist.
    """
    if not isinstance(global_override, dict):
        raise ValueError("Exported global settings must be an object.")
    if not isinstance(item_override, dict):
        raise ValueError("Exported item settings must be an object.")
    combined = sanitize_portable_override(global_override)
    combined.update(sanitize_portable_override(item_override))
    effective = apply_settings_override(base, combined, available_codecs)
    return sanitize_portable_override(settings_override_diff(base, effective))


@dataclass
class EncodeResult:
    file: str
    success: bool
    input_size: int = 0
    output_size: int = 0
    input_duration: float = 0
    output_duration: float = 0
    encode_time: float = 0
    skipped: bool = False
    error: str = ""
    output_file: str = ""
    validated: bool = False
    validation_message: str = ""
    vmaf_score: Optional[float] = None
    vmaf_error: str = ""
    post_copy_path: str = ""
    post_copy_error: str = ""
    input_identity: Optional[tuple[int, int, int, int, int]] = None
    reference_start: float = 0.0
    vmaf_sample_seconds: int = 30


# ============================================================
#  CODEC DEFINITIONS
# ============================================================

CODECS_GPU = [
    CodecOption(
        name="H.265 GPU (NVENC)",
        encoder="hevc_nvenc",
        args=["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-rc-lookahead", "32",
              "-spatial-aq", "1", "-temporal-aq", "1"],
        crf_flag="-cq",
        crf_values={"high": 22, "medium": 28, "low": 34},
        requires_gpu=True,
        gpu_vendor="nvidia",
    ),
    CodecOption(
        name="H.264 GPU (NVENC)",
        encoder="h264_nvenc",
        args=["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-rc-lookahead", "32",
              "-spatial-aq", "1", "-temporal-aq", "1"],
        crf_flag="-cq",
        crf_values={"high": 20, "medium": 26, "low": 32},
        requires_gpu=True,
        gpu_vendor="nvidia",
    ),
]

CODECS_CPU = [
    CodecOption(
        name="H.265 CPU",
        encoder="libx265",
        args=["-preset", "slow"],
        crf_flag="-crf",
        crf_values={"high": 20, "medium": 28, "low": 34},
    ),
    CodecOption(
        name="H.264 CPU",
        encoder="libx264",
        args=["-preset", "medium"],
        crf_flag="-crf",
        crf_values={"high": 18, "medium": 23, "low": 28},
    ),
    CodecOption(
        name="AV1 CPU (libaom)",
        encoder="libaom-av1",
        args=["-b:v", "0", "-cpu-used", "4", "-row-mt", "1", "-tiles", "2x2"],
        crf_flag="-crf",
        crf_values={"high": 22, "medium": 30, "low": 38},
    ),
    CodecOption(
        name="SVT-AV1",
        encoder="libsvtav1",
        args=["-preset", "6"],
        crf_flag="-crf",
        crf_values={"high": 22, "medium": 30, "low": 38},
    ),
]

CODECS_AMD = [
    CodecOption(
        name="H.265 GPU (AMF)",
        encoder="hevc_amf",
        args=["-quality", "quality", "-rc", "cqp"],
        crf_flag="-qp_p",
        crf_values={"high": 22, "medium": 28, "low": 34},
        requires_gpu=True,
        gpu_vendor="amd",
    ),
    CodecOption(
        name="H.264 GPU (AMF)",
        encoder="h264_amf",
        args=["-quality", "quality", "-rc", "cqp"],
        crf_flag="-qp_p",
        crf_values={"high": 20, "medium": 26, "low": 32},
        requires_gpu=True,
        gpu_vendor="amd",
    ),
]

CODECS_INTEL = [
    CodecOption(
        name="H.265 GPU (QSV)",
        encoder="hevc_qsv",
        args=["-preset", "slower"],
        crf_flag="-global_quality",
        crf_values={"high": 22, "medium": 28, "low": 34},
        requires_gpu=True,
        gpu_vendor="intel",
    ),
    CodecOption(
        name="H.264 GPU (QSV)",
        encoder="h264_qsv",
        args=["-preset", "slower"],
        crf_flag="-global_quality",
        crf_values={"high": 20, "medium": 26, "low": 32},
        requires_gpu=True,
        gpu_vendor="intel",
    ),
]

PRESETS = {
    "1": {
        "name": "Fast & Small",
        "desc": "H.264, Medium quality, 720p, 30fps",
        "codec_gpu": "h264_nvenc",
        "codec_cpu": "libx264",
        "quality": "medium",
        "resolution": "720",
        "fps": 30,
        "audio": "128k",
    },
    "2": {
        "name": "Balanced",
        "desc": "H.265, High quality, Original resolution",
        "codec_gpu": "hevc_nvenc",
        "codec_cpu": "libx265",
        "quality": "high",
        "resolution": None,
        "fps": None,
        "audio": "192k",
    },
    "3": {
        "name": "Archive Quality",
        "desc": "H.265 CPU slow, High quality, Original",
        "codec_gpu": "libx265",
        "codec_cpu": "libx265",
        "quality": "high",
        "resolution": None,
        "fps": None,
        "audio": "192k",
    },
    "4": {
        "name": "Max Compression",
        "desc": "AV1, Medium quality, 720p, 30fps",
        "codec_gpu": "libaom-av1",
        "codec_cpu": "libaom-av1",
        "quality": "medium",
        "resolution": "720",
        "fps": 30,
        "audio": "96k",
    },
    "5": {
        "name": "Quick Share",
        "desc": "H.264, Low quality, 480p, 30fps",
        "codec_gpu": "h264_nvenc",
        "codec_cpu": "libx264",
        "quality": "low",
        "resolution": "480",
        "fps": 30,
        "audio": "64k",
    },
}

QUALITY_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
RES_LABELS = {None: "Original", "1080": "1080p", "720": "720p", "480": "480p"}
AUDIO_LABELS = {"192k": "192k", "128k": "128k", "96k": "96k", "64k": "64k"}

# 10-bit pixel format mapping per encoder
_10BIT_PIX_FMT = {
    "hevc_nvenc": "p010le",
    "h264_nvenc": "p010le",  # limited support
    "hevc_amf": "p010le",
    "h264_amf": "p010le",
    "hevc_qsv": "p010le",
    "h264_qsv": "p010le",
    "libx265": "yuv420p10le",
    "libx264": "yuv420p10le",
    "libaom-av1": "yuv420p10le",
    "libsvtav1": "yuv420p10le",
}

# Filename template tokens: {name}, {codec}, {quality}, {res}, {fps}, {date}
FILENAME_TEMPLATES = [
    "{name}",
    "{name}_{codec}_{quality}",
    "{name}_{res}_{quality}",
    "{name}_{codec}_{quality}_{date}",
    "{name}_{date}",
]

AUDIO_EXTRACT_FORMATS = {
    "mp3": {"codec": "libmp3lame", "ext": "mp3"},
    "aac": {"codec": "aac", "ext": "m4a"},
    "flac": {"codec": "flac", "ext": "flac"},
    "opus": {"codec": "libopus", "ext": "ogg"},
}


# ============================================================
#  UTILITY FUNCTIONS
# ============================================================


def detect_gpu() -> tuple[bool, str]:
    """Detect NVIDIA GPU via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip().split("\n")[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, "None detected"


def detect_amd_gpu() -> tuple[bool, str]:
    """Detect AMD GPU via PowerShell Get-CimInstance."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and ("AMD" in line.upper() or "RADEON" in line.upper()):
                    return True, line
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, ""


def detect_intel_gpu() -> tuple[bool, str]:
    """Detect Intel integrated/discrete GPU via PowerShell Get-CimInstance."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and "INTEL" in line.upper() and (
                    "ARC" in line.upper() or "UHD" in line.upper()
                    or "IRIS" in line.upper() or "HD GRAPHICS" in line.upper()
                ):
                    return True, line
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, ""


def _get_available_encoders() -> set[str]:
    """Query FFmpeg once and return the set of all available encoder names."""
    encoders: set[str] = set()
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and len(parts[0]) >= 6 and parts[0][0] in "VA":
                encoders.add(parts[1])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return encoders


def check_ffmpeg() -> bool:
    """Verify FFmpeg and FFprobe are available."""
    global FFMPEG_PATH, FFPROBE_PATH
    # Re-resolve in case the user installed FFmpeg after the module was imported
    if not FFMPEG_PATH or not os.path.isfile(FFMPEG_PATH):
        FFMPEG_PATH, FFPROBE_PATH = _resolve_ffmpeg_paths()
    if not FFMPEG_PATH or not os.path.isfile(FFMPEG_PATH):
        console.print("\n  [red]ERROR:[/] FFmpeg not found.")
        console.print("  Install it from [cyan]https://www.gyan.dev/ffmpeg/builds/[/]")
        console.print("  and make sure [bold]ffmpeg.exe[/] is on your PATH")
        console.print("  (or place it in C:\\ffmpeg\\).\n")
        return False
    if not FFPROBE_PATH or not os.path.isfile(FFPROBE_PATH):
        console.print(f"\n  [red]ERROR:[/] FFprobe not found (expected next to ffmpeg at {Path(FFMPEG_PATH).parent})\n")
        return False
    return True


def get_duration(filepath: str) -> float:
    """Get video duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0


def probe_video(filepath: str) -> dict:
    """Probe a video file and return metadata dict.

    Returns keys: video_codec, video_res, video_bitrate, video_fps,
    audio_codec, audio_bitrate, audio_channels, pixel_format, bit_depth.
    """
    info: dict = {
        "video_codec": "", "video_res": "", "video_bitrate": "",
        "video_fps": "", "audio_codec": "", "audio_bitrate": "",
        "audio_channels": "", "pixel_format": "", "bit_depth": "",
        "hdr": False, "color_transfer": "", "color_primaries": "",
        "color_space": "", "subtitle_streams": [],
    }
    if not FFPROBE_PATH or not os.path.isfile(filepath):
        return info
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error",
             "-show_entries",
             "stream=codec_name,codec_type,width,height,bit_rate,"
             "r_frame_rate,pix_fmt,bits_per_raw_sample,channels,"
             "color_transfer,color_primaries,color_space"
             ":stream_tags=language",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])

        for s in streams:
            ctype = s.get("codec_type", "")
            if ctype == "video" and not info["video_codec"]:
                info["video_codec"] = s.get("codec_name", "")
                w, h = s.get("width", 0), s.get("height", 0)
                if w and h:
                    info["video_res"] = f"{w}x{h}"
                br = s.get("bit_rate", "")
                if br and br.isdigit():
                    info["video_bitrate"] = f"{int(br) // 1000} kbps"
                rfr = s.get("r_frame_rate", "")
                if rfr and "/" in rfr:
                    num, den = rfr.split("/")
                    try:
                        info["video_fps"] = f"{int(num) / int(den):.2f}"
                    except (ValueError, ZeroDivisionError):
                        info["video_fps"] = rfr
                info["pixel_format"] = s.get("pix_fmt", "")
                info["bit_depth"] = s.get("bits_per_raw_sample", "")
                # HDR detection
                ct = s.get("color_transfer", "")
                cp = s.get("color_primaries", "")
                cs = s.get("color_space", "")
                info["color_transfer"] = ct
                info["color_primaries"] = cp
                info["color_space"] = cs
                if ct in ("smpte2084", "arib-std-b67") or cp == "bt2020":
                    info["hdr"] = True

            elif ctype == "audio" and not info["audio_codec"]:
                info["audio_codec"] = s.get("codec_name", "")
                abr = s.get("bit_rate", "")
                if abr and abr.isdigit():
                    info["audio_bitrate"] = f"{int(abr) // 1000} kbps"
                ch = s.get("channels", "")
                if ch:
                    info["audio_channels"] = str(ch)

            elif ctype == "subtitle":
                lang = s.get("tags", {}).get("language", "und")
                info["subtitle_streams"].append({
                    "codec": s.get("codec_name", ""),
                    "language": lang,
                })

    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError,
            OSError):
        pass

    return info


def render_filename_template(
    template: str,
    input_path: str,
    settings: "Settings",
) -> str:
    """Expand a filename template with tokens.

    Tokens: {name}, {codec}, {quality}, {res}, {fps}, {date}
    """
    name = Path(input_path).stem
    codec_tag = settings.codec.encoder if settings.codec else "unknown"
    quality_tag = settings.quality
    res_tag = settings.resolution or "orig"
    fps_tag = str(settings.fps) if settings.fps else "orig"
    date_tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    result = template.replace("{name}", name)
    result = result.replace("{codec}", codec_tag)
    result = result.replace("{quality}", quality_tag)
    result = result.replace("{res}", res_tag)
    result = result.replace("{fps}", fps_tag)
    result = result.replace("{date}", date_tag)
    return result


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def validate_output_filename(filename: str) -> str:
    """Return a safe single filename component or raise ``ValueError``."""
    if (
        not isinstance(filename, str)
        or not filename
        or filename in (".", "..")
        or filename.endswith((" ", "."))
        or any(character in filename for character in ("/", "\\", ":", "\0"))
        or any(ord(character) < 32 for character in filename)
    ):
        raise ValueError(
            "Output filename must be one safe filename without path "
            "separators, a drive name, or control characters.")
    if filename.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Output filename uses a reserved Windows device name.")
    return filename


def build_output_filename(
    input_path: str,
    settings: "Settings",
    preview: bool = False,
) -> str:
    """Build and validate the output basename for one input."""
    stem = Path(input_path).stem
    if settings.audio_extract:
        fmt = AUDIO_EXTRACT_FORMATS.get(
            settings.audio_extract_format,
            AUDIO_EXTRACT_FORMATS["mp3"],
        )
        preview_suffix = "_preview" if preview else ""
        filename = f"{stem}{preview_suffix}.{fmt['ext']}"
    elif preview:
        filename = f"{stem}_preview.{settings.output_format}"
    elif settings.filename_template and settings.filename_template != "{name}":
        rendered = render_filename_template(
            settings.filename_template, input_path, settings)
        filename = f"{rendered}.{settings.output_format}"
    else:
        filename = f"{stem}.{settings.output_format}"
    return validate_output_filename(filename)


def build_output_path(
    input_path: str,
    settings: "Settings",
    output_dir: str = OUTPUT_DIR,
    preview: bool = False,
) -> str:
    """Build a safe output path rooted in the requested destination."""
    return os.path.join(
        output_dir, build_output_filename(input_path, settings, preview))


def get_file_size_mb(filepath: str) -> float:
    """Get file size in MB."""
    try:
        return os.path.getsize(filepath) / (1024 * 1024)
    except OSError:
        return 0.0


def format_duration(seconds: float) -> str:
    """Format seconds to HH:MM:SS or MM:SS."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h, r = divmod(seconds, 3600)
        m, s = divmod(r, 60)
        return f"{h}:{m:02d}:{s:02d}"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def format_size(mb: float) -> str:
    """Format size with appropriate units."""
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def find_videos(directory: str = ".") -> list[Path]:
    """Find all video files in directory."""
    videos = []
    for f in sorted(Path(directory).iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(f)
    return videos


# Cache of encoder names available in the user's FFmpeg build.
# Populated on first call to get_all_codecs().
_ffmpeg_encoders: set[str] | None = None


def get_all_codecs(has_gpu: bool, has_amd: bool = False,
                   has_intel: bool = False) -> list[CodecOption]:
    """Get available codecs based on GPU detection.

    Filters out any codec whose encoder is not present in the
    user's FFmpeg build (e.g. libsvtav1 in essentials builds).
    """
    global _ffmpeg_encoders
    if _ffmpeg_encoders is None:
        _ffmpeg_encoders = _get_available_encoders()

    codecs: list[CodecOption] = []
    if has_gpu:
        codecs += CODECS_GPU
    if has_amd:
        codecs += CODECS_AMD
    if has_intel:
        codecs += CODECS_INTEL
    codecs += CODECS_CPU

    # Filter to encoders actually available in this FFmpeg build
    if _ffmpeg_encoders:
        codecs = [c for c in codecs if c.encoder in _ffmpeg_encoders]
    return codecs


def find_codec_by_encoder(encoder: str, has_gpu: bool,
                          has_amd: bool = False,
                          has_intel: bool = False) -> Optional[CodecOption]:
    """Find a codec option by encoder name."""
    for codec in get_all_codecs(has_gpu, has_amd, has_intel):
        if codec.encoder == encoder:
            return codec
    return None


def resolve_preset_codec(
    preset: dict,
    has_gpu: bool,
    has_amd: bool = False,
    has_intel: bool = False,
) -> Optional[CodecOption]:
    """Resolve the best codec for a preset given the available hardware.

    Tries GPU codec first (NVENC → AMF → QSV), then falls back to CPU.
    """
    available = get_all_codecs(has_gpu, has_amd, has_intel)
    encoder_map = {c.encoder: c for c in available}

    gpu_codec_name = preset.get("codec_gpu", "")
    cpu_codec_name = preset.get("codec_cpu", "")

    # Try GPU codec first
    if gpu_codec_name and gpu_codec_name in encoder_map:
        return encoder_map[gpu_codec_name]

    # Try AMF/QSV equivalents for the GPU codec
    _nvenc_to_amf = {"hevc_nvenc": "hevc_amf", "h264_nvenc": "h264_amf"}
    _nvenc_to_qsv = {"hevc_nvenc": "hevc_qsv", "h264_nvenc": "h264_qsv"}
    if gpu_codec_name:
        amf_name = _nvenc_to_amf.get(gpu_codec_name, "")
        qsv_name = _nvenc_to_qsv.get(gpu_codec_name, "")
        if amf_name and amf_name in encoder_map:
            return encoder_map[amf_name]
        if qsv_name and qsv_name in encoder_map:
            return encoder_map[qsv_name]

    # Fall back to CPU codec
    if cpu_codec_name and cpu_codec_name in encoder_map:
        return encoder_map[cpu_codec_name]

    return None


def log_message(message: str):
    """Append a message to the log file."""
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def save_config(settings: Settings):
    """Save current settings as default config (including FFmpeg paths)."""
    config = settings_to_dict(settings)
    config.update({
        "ffmpeg_path": FFMPEG_PATH,
        "ffprobe_path": FFPROBE_PATH,
    })
    try:
        atomic_update_mapping(CONFIG_FILE, config)
    except OSError:
        pass


def load_config() -> dict:
    """Load saved config from JSON. Returns empty dict on failure."""
    return read_json(CONFIG_FILE, dict, {})


def update_config_values(updates: dict) -> dict:
    """Atomically merge UI/runtime values into the saved configuration."""
    try:
        return atomic_update_mapping(CONFIG_FILE, updates)
    except OSError:
        return load_config()


CUSTOM_PRESETS_FILE = str(APP_PATHS.presets)
QUEUE_FILE = str(APP_PATHS.queue)


def save_custom_preset(name: str, settings: Settings):
    """Save a named custom preset to disk."""
    presets = load_custom_presets()
    presets[name] = settings_to_dict(settings)
    try:
        atomic_write_json(CUSTOM_PRESETS_FILE, presets)
    except OSError:
        pass


def load_custom_presets() -> dict:
    """Load custom presets from disk. Returns dict of name -> preset dict."""
    return read_json(CUSTOM_PRESETS_FILE, dict, {})


def delete_custom_preset(name: str):
    """Delete a named custom preset from disk."""
    presets = load_custom_presets()
    if name in presets:
        del presets[name]
        try:
            atomic_write_json(CUSTOM_PRESETS_FILE, presets)
        except OSError:
            pass


def notify_complete(file_count: int, sound: bool = True, toast: bool = True):
    """Play notification sound and show toast."""
    # Beep
    if sound:
        try:
            subprocess.run(
                ["powershell", "-command",
                 "[console]::beep(800,200);[console]::beep(1000,200);[console]::beep(1200,400)"],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Toast notification
    if not toast:
        return
    try:
        msg = f"Transcoding complete - {file_count} file{'s' if file_count != 1 else ''} encoded"
        ps_cmd = (
            "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipTitle='Video Transcoder';"
            f"$n.BalloonTipText='{msg}';"
            "$n.Visible=$true;$n.ShowBalloonTip(5000);"
            "Start-Sleep 6;$n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


# ============================================================
#  CROP DETECTION
# ============================================================


def detect_crop(input_file: str, duration: float = 0) -> str:
    """Detect black bars using FFmpeg cropdetect filter.

    Returns a crop filter string like ``crop=1920:800:0:140`` or ``""``.
    """
    if not FFMPEG_PATH or not os.path.isfile(input_file):
        return ""
    seek = max(duration * 0.25, 30) if duration > 60 else 5
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-ss", str(seek), "-i", input_file,
             "-t", "5", "-vf", "cropdetect=24:16:0",
             "-f", "null", "NUL" if sys.platform == "win32" else "/dev/null"],
            capture_output=True, text=True, timeout=30,
        )
        crop_lines = [l for l in result.stderr.splitlines() if "crop=" in l]
        if crop_lines:
            match = re.search(r"crop=(\d+:\d+:\d+:\d+)", crop_lines[-1])
            if match:
                return f"crop={match.group(1)}"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


# ============================================================
#  INPUT VALIDATION
# ============================================================


def validate_settings(settings: "Settings") -> list[str]:
    """Validate settings and return a list of warning messages."""
    warnings: list[str] = []
    if settings.codec is None:
        warnings.append("No codec selected.")
        return warnings
    if settings.audio_codec == "opus" and settings.output_format == "mp4":
        warnings.append("Opus audio in MP4 has limited compatibility. Consider MKV.")
    if settings.trim_start is not None and settings.trim_end is not None:
        if settings.trim_start >= settings.trim_end:
            warnings.append("Trim start must be less than trim end.")
    if settings.two_pass and settings.codec.requires_gpu:
        warnings.append("2-pass encoding is only for CPU codecs. It will be ignored for GPU.")
    if settings.ten_bit and settings.codec.encoder not in _10BIT_PIX_FMT:
        warnings.append(f"10-bit not supported for {settings.codec.encoder}.")
    if settings.concurrent > 1 and settings.codec.requires_gpu:
        warnings.append("Concurrent GPU encoding may cause VRAM issues.")
    if settings.auto_crop and settings.audio_extract:
        warnings.append("Auto-crop has no effect in audio extraction mode.")
    if settings.two_pass and settings.bitrate_mode == "crf":
        warnings.append(
            "2-pass requires CBR, VBR, or target-size mode. "
            "It is ignored in constant-quality (CRF/CQ) mode.")
    if settings.bitrate_mode == "filesize" and settings.target_size_mb <= 0:
        warnings.append("File-size bitrate mode requires a target size > 0 MB.")
    if settings.bitrate_mode in ("cbr", "vbr"):
        if not settings.target_bitrate:
            warnings.append(
                f"{settings.bitrate_mode.upper()} mode requires a target bitrate.")
        elif normalize_bitrate(settings.target_bitrate) is None:
            warnings.append(
                f"{settings.bitrate_mode.upper()} mode requires a valid "
                "positive target bitrate.")
    if settings.max_bitrate and normalize_bitrate(settings.max_bitrate) is None:
        warnings.append("Maximum bitrate must be a valid positive bitrate.")
    return warnings


# ============================================================
#  AUDIO EXTRACTION
# ============================================================


def requested_duration_limit(
    settings: "Settings",
    preview: bool = False,
) -> Optional[float]:
    """Return the requested output-duration cap after seek, if any."""
    start = max(0.0, float(settings.trim_start or 0.0))
    limit: Optional[float] = None
    if settings.trim_end is not None and settings.trim_end > 0:
        limit = max(0.0, float(settings.trim_end) - start)
    if preview:
        limit = 60.0 if limit is None else min(limit, 60.0)
    return limit


def format_ffmpeg_seconds(value: float) -> str:
    """Format a duration without an unnecessary decimal suffix."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def build_audio_extract_command(
    input_file: str,
    output_file: str,
    format_key: str = "mp3",
    bitrate: str = "192k",
    *,
    settings: Optional["Settings"] = None,
    preview: bool = False,
) -> list[str]:
    """Build FFmpeg command to extract audio only."""
    fmt = AUDIO_EXTRACT_FORMATS.get(format_key, AUDIO_EXTRACT_FORMATS["mp3"])
    cmd = [FFMPEG_PATH]
    if settings and settings.trim_start and settings.trim_start > 0:
        cmd += ["-ss", str(settings.trim_start)]
    cmd += ["-i", input_file]
    duration_limit = (
        requested_duration_limit(settings, preview) if settings else
        (60.0 if preview else None)
    )
    if duration_limit is not None:
        cmd += ["-t", format_ffmpeg_seconds(duration_limit)]
    cmd += ["-vn", "-sn"]
    if fmt["codec"] == "flac":
        cmd += ["-c:a", "flac"]
    else:
        cmd += ["-c:a", fmt["codec"], "-b:a", bitrate]
    cmd += ["-progress", "pipe:1", "-nostats", "-y", output_file]
    return cmd


# ============================================================
#  QUEUE PERSISTENCE
# ============================================================


QUEUE_SCHEMA_VERSION = 2


def validate_queue_document(value) -> Optional[list[dict] | dict]:
    """Validate legacy-list or current versioned queue structure."""
    if isinstance(value, list):
        return value if all(isinstance(item, dict) for item in value) else None
    if not isinstance(value, dict):
        return None
    if value.get("version") != QUEUE_SCHEMA_VERSION:
        return None
    items = value.get("items")
    if (
        not isinstance(items, list)
        or not all(isinstance(item, dict) for item in items)
    ):
        return None
    global_settings = value.get("global_settings", {})
    if not isinstance(global_settings, dict):
        return None
    return value


def save_queue(document: list[dict] | dict):
    """Atomically save a legacy list or versioned queue document."""
    if validate_queue_document(document) is None:
        return
    try:
        atomic_write_json(QUEUE_FILE, document)
    except OSError:
        pass


def load_queue() -> list[dict] | dict:
    """Load a legacy list or versioned queue document."""
    document = read_json(QUEUE_FILE, (list, dict), [])
    return validate_queue_document(document) or []


# ============================================================
#  SYSTEM STATS
# ============================================================


def get_system_stats() -> dict:
    """Get CPU and GPU usage stats (Windows)."""
    stats: dict = {"cpu": "", "gpu_util": "", "gpu_temp": "", "ram": ""}
    # CPU load via PowerShell Get-CimInstance
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty LoadPercentage"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                stats["cpu"] = f"{line}%"
                break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # GPU usage via nvidia-smi
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split(",")
            if len(parts) >= 2:
                stats["gpu_util"] = f"{parts[0].strip()}%"
                stats["gpu_temp"] = f"{parts[1].strip()}C"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return stats


# ============================================================
#  SUBTITLE EXTRACTION (Phase 8)
# ============================================================


def build_subtitle_extract_command(
    input_file: str,
    output_file: str,
    stream_index: int = 0,
    fmt: str = "srt",
) -> list[str]:
    """Build FFmpeg command to extract a subtitle stream.

    *stream_index*: index of the subtitle stream (0-based).
    *fmt*: output format, e.g. 'srt', 'ass', 'vtt'.
    """
    cmd = [
        FFMPEG_PATH, "-i", input_file,
        "-map", f"0:s:{stream_index}",
        "-c:s", fmt if fmt != "srt" else "srt",
        "-y", output_file,
    ]
    return cmd


# ============================================================
#  SCENE DETECTION (Phase 14)
# ============================================================


def detect_scenes(
    filepath: str,
    threshold: float = 0.3,
    process_control: "TranscodeProcessControl | None" = None,
    on_progress=None,
    timeout: float | None = None,
) -> list[float]:
    """Return sorted scene-boundary timestamps from a lightweight analysis."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("Scene threshold must be between 0 and 1.")
    if not FFMPEG_PATH or not os.path.isfile(filepath):
        return []
    control = process_control or TranscodeProcessControl()
    duration = get_duration(filepath)
    effective_timeout = timeout or max(120.0, duration * 0.75)
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-i",
        filepath,
        "-vf",
        f"scale=640:-2,select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        control.register(process)
        try:
            _stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            control._terminate_process(process)
            process.communicate()
            return []
        finally:
            control.unregister(process)
        if control.cancelled or process.returncode != 0:
            return []

        timestamps: list[float] = []
        for line in stderr.splitlines():
            if "pts_time:" in line:
                for part in line.split():
                    if part.startswith("pts_time:"):
                        try:
                            timestamp = float(part.split(":", 1)[1])
                            if timestamp >= 0 and (
                                    duration <= 0 or timestamp <= duration):
                                timestamps.append(timestamp)
                                if on_progress and duration > 0:
                                    on_progress(
                                        min(100.0, timestamp / duration * 100))
                        except (ValueError, IndexError):
                            pass
        return sorted({round(timestamp, 3) for timestamp in timestamps})
    except (FileNotFoundError, OSError):
        return []


# ============================================================
#  VMAF QUALITY SCORING (Phase 10)
# ============================================================


_VMAF_SEMAPHORE = threading.Semaphore(1)


def run_vmaf_score(
    reference: str,
    distorted: str,
    sample_seconds: int = 30,
    trim_start: float = 0.0,
    process_control: "TranscodeProcessControl | None" = None,
) -> float | None:
    """Compute a normalized sample VMAF score from FFmpeg JSON output."""
    if (
        not FFMPEG_PATH
        or not os.path.isfile(reference)
        or not os.path.isfile(distorted)
    ):
        return None

    control = process_control or TranscodeProcessControl()
    descriptor, report_path = tempfile.mkstemp(suffix=".vmaf.json")
    os.close(descriptor)
    _remove_file_quietly(report_path)
    escaped_report = (
        report_path.replace("\\", "/").replace(":", r"\:")
    )
    seconds = max(1, min(int(sample_seconds), 300))
    seek = max(0.0, float(trim_start or 0.0))
    normalize = (
        "scale=1280:720:force_original_aspect_ratio=decrease:"
        "flags=bicubic,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
    )
    filter_graph = (
        f"[0:v]{normalize}[dist];"
        f"[1:v]{normalize}[ref];"
        f"[dist][ref]libvmaf=log_fmt=json:log_path='{escaped_report}'"
    )
    command = [FFMPEG_PATH, "-hide_banner", "-i", distorted]
    if seek > 0:
        command += ["-ss", str(seek)]
    command += [
        "-i",
        reference,
        "-t",
        str(seconds),
        "-lavfi",
        filter_graph,
        "-f",
        "null",
        "-",
    ]

    try:
        with _VMAF_SEMAPHORE:
            if control.cancelled:
                return None
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            control.register(process)
            try:
                process.communicate(timeout=max(120, seconds * 10))
            except subprocess.TimeoutExpired:
                control._terminate_process(process)
                process.communicate()
                return None
            finally:
                control.unregister(process)
            if control.cancelled or process.returncode != 0:
                return None

        report = read_json(report_path, dict, {})
        pooled = report.get("pooled_metrics", {}).get("vmaf", {})
        for key in ("harmonic_mean", "mean"):
            value = pooled.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    except (FileNotFoundError, OSError, ValueError):
        return None
    finally:
        _remove_file_quietly(report_path)
    return None


# ============================================================
#  QUEUE IMPORT / EXPORT (Phase 9)
# ============================================================


def export_queue(document: list[dict] | dict, filepath: str) -> bool:
    """Export queue items to a shareable JSON file."""
    if validate_queue_document(document) is None:
        return False
    try:
        atomic_write_json(filepath, document)
        return True
    except OSError:
        return False


def import_queue(filepath: str) -> list[dict] | dict:
    """Import queue items from a JSON file."""
    document = read_json(filepath, (list, dict), [])
    return validate_queue_document(document) or []


# ============================================================
#  ADVANCED CODEC OPTIONS (Phase 12)
# ============================================================


ADVANCED_OPTIONS: dict[str, list[dict[str, str]]] = {
    "hevc_nvenc": [
        {"flag": "-rc", "desc": "Rate control mode", "default": "vbr"},
        {"flag": "-spatial-aq", "desc": "Spatial AQ", "default": "1"},
        {"flag": "-temporal-aq", "desc": "Temporal AQ", "default": "1"},
        {"flag": "-b_ref_mode", "desc": "B-frame ref mode", "default": "middle"},
        {"flag": "-lookahead", "desc": "Lookahead frames", "default": "32"},
    ],
    "h264_nvenc": [
        {"flag": "-rc", "desc": "Rate control mode", "default": "vbr"},
        {"flag": "-spatial-aq", "desc": "Spatial AQ", "default": "1"},
        {"flag": "-temporal-aq", "desc": "Temporal AQ", "default": "1"},
        {"flag": "-lookahead", "desc": "Lookahead frames", "default": "32"},
    ],
    "libx265": [
        {"flag": "-x265-params", "desc": "x265 params string", "default": ""},
        {"flag": "-preset", "desc": "Encoding preset", "default": "medium"},
    ],
    "libx264": [
        {"flag": "-x264-params", "desc": "x264 params string", "default": ""},
        {"flag": "-preset", "desc": "Encoding preset", "default": "medium"},
    ],
    "libsvtav1": [
        {"flag": "-preset", "desc": "Encoding preset (0-13)", "default": "8"},
        {"flag": "-svtav1-params", "desc": "SVT-AV1 params string", "default": ""},
    ],
    "hevc_amf": [
        {"flag": "-quality", "desc": "Quality preset", "default": "quality"},
    ],
    "hevc_qsv": [
        {"flag": "-preset", "desc": "Encoding preset", "default": "veryslow"},
    ],
}


# ============================================================
#  HDR HELPERS (Phase 5)
# ============================================================


def _apply_hdr_flags(
    cmd: list[str],
    settings: "Settings",
    codec: "CodecOption",
    hdr_info: dict | None,
):
    """Append HDR-related flags to *cmd* in-place.

    *hdr_info*: dict from probe_video() – only the ``hdr``,
    ``color_transfer``, ``color_primaries``, ``color_space`` keys are used.
    """
    if not hdr_info or not hdr_info.get("hdr"):
        return
    mode = settings.hdr_mode
    if mode == "off":
        return
    if mode in ("auto", "passthrough"):
        # Passthrough HDR metadata — copy colour info to output
        ct = hdr_info.get("color_transfer", "")
        cp = hdr_info.get("color_primaries", "")
        cs = hdr_info.get("color_space", "")
        if ct:
            cmd += ["-color_trc", ct]
        if cp:
            cmd += ["-color_primaries", cp]
        if cs:
            cmd += ["-colorspace", cs]
    # "tonemap" is handled via vf_parts in build_ffmpeg_command


def _probe_duration(filepath: str) -> float:
    """Quick probe to get duration in seconds (for bitrate calc)."""
    if not FFPROBE_PATH or not os.path.isfile(filepath):
        return 0.0
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip())
    except (ValueError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 0.0


# ============================================================
#  ENCODING
# ============================================================


def paths_refer_to_same_file(first: str, second: str) -> bool:
    """Return True when two paths identify the same filesystem location."""
    try:
        if os.path.exists(first) and os.path.exists(second):
            return os.path.samefile(first, second)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second))


def capture_file_identity(
    filepath: str,
) -> Optional[tuple[int, int, int, int, int]]:
    """Capture enough stat data to detect replacement of a source pathname."""
    try:
        stat = os.stat(filepath, follow_symlinks=True)
    except OSError:
        return None
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def delete_source_if_unchanged(
    filepath: str,
    expected_identity: Optional[tuple[int, int, int, int, int]],
) -> tuple[bool, str]:
    """Delete only when the source is still the exact file that was encoded."""
    if expected_identity is None:
        return False, "Source identity was unavailable; original retained."
    current_identity = capture_file_identity(filepath)
    if current_identity is None:
        return False, "Source no longer exists or cannot be inspected."
    if current_identity != expected_identity:
        return False, "Source changed after encoding; replacement retained."
    try:
        os.remove(filepath)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def make_temporary_output_path(output_file: str) -> str:
    """Create a unique same-directory media path suitable for atomic replace.

    The real media suffix remains last so FFmpeg can infer the container.
    """
    output = Path(output_file)
    token = uuid.uuid4().hex[:12]
    suffix = output.suffix
    if suffix:
        name = f".{output.stem}.{token}.part{suffix}"
    else:
        name = f".{output.name}.{token}.part"
    return str(output.with_name(name))


def expected_output_duration(
    input_duration: float,
    settings: "Settings",
    preview: bool = False,
) -> float:
    """Calculate the expected encoded duration after trim/preview settings."""
    if input_duration <= 0:
        return 0.0
    start = max(0.0, float(settings.trim_start or 0.0))
    end = input_duration
    if settings.trim_end is not None and settings.trim_end > 0:
        end = min(input_duration, float(settings.trim_end))
    duration = max(0.0, end - start)
    return min(duration, 60.0) if preview else duration


def validate_output_file(
    input_file: str,
    output_file: str,
    settings: "Settings",
    preview: bool = False,
    media_kind: str = "video",
) -> tuple[bool, str, float]:
    """Validate a completed output before it is published or trusted.

    Validation requires a non-empty file, the expected stream type, a readable
    duration, and (when the input duration is known) a close duration match.
    """
    if not os.path.isfile(output_file):
        return False, "FFmpeg did not create an output file.", 0.0
    try:
        if os.path.getsize(output_file) <= 0:
            return False, "The encoded output is empty.", 0.0
    except OSError as exc:
        return False, f"Unable to inspect the encoded output: {exc}", 0.0

    metadata = probe_video(output_file)
    if media_kind == "audio":
        if not metadata.get("audio_codec"):
            return False, "The output does not contain a readable audio stream.", 0.0
    elif not metadata.get("video_codec"):
        return False, "The output does not contain a readable video stream.", 0.0

    output_duration = get_duration(output_file)
    if output_duration <= 0:
        return False, "FFprobe could not read the output duration.", 0.0

    input_duration = get_duration(input_file)
    expected = expected_output_duration(input_duration, settings, preview)
    if expected > 0:
        tolerance = max(2.0, expected * 0.02)
        difference = abs(output_duration - expected)
        if difference > tolerance:
            return (
                False,
                f"Duration mismatch: expected about {expected:.2f}s, "
                f"got {output_duration:.2f}s.",
                output_duration,
            )

    return True, "Output passed stream, size, and duration validation.", output_duration


def _remove_file_quietly(filepath: str):
    try:
        if filepath and os.path.isfile(filepath):
            os.remove(filepath)
    except OSError:
        pass


def _cleanup_passlog(output_file: str):
    stem = Path(output_file).stem
    base = os.path.join(
        os.path.dirname(output_file) or ".",
        f"ffmpeg2pass_{stem}",
    )
    for suffix in (".log", "-0.log", "-0.log.mbtree", ".log.mbtree"):
        _remove_file_quietly(base + suffix)


def copy_validated_output(
    source_file: str,
    destination_dir: str,
) -> tuple[bool, str, str]:
    """Atomically copy a validated output to a secondary destination."""
    if not destination_dir:
        return True, "", ""
    destination = os.path.join(destination_dir, Path(source_file).name)
    if paths_refer_to_same_file(source_file, destination):
        return True, destination, ""

    temporary = make_temporary_output_path(destination)
    try:
        os.makedirs(destination_dir, exist_ok=True)
        shutil.copy2(source_file, temporary)
        if os.path.getsize(source_file) != os.path.getsize(temporary):
            return False, "", "Copied file size does not match the source."
        os.replace(temporary, destination)
        return True, destination, ""
    except OSError as exc:
        return False, "", str(exc)
    finally:
        _remove_file_quietly(temporary)


class TranscodeProcessControl:
    """Own and directly control all FFmpeg processes in an encode session."""

    def __init__(
        self,
        cancel_event: threading.Event | None = None,
        run_event: threading.Event | None = None,
    ):
        self.cancel_event = cancel_event or threading.Event()
        self.run_event = run_event or threading.Event()
        if run_event is None:
            self.run_event.set()
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen] = set()
        self._suspended: set[int] = set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def paused(self) -> bool:
        return not self.run_event.is_set()

    def reset(self):
        """Prepare the controller for a new batch."""
        with self._lock:
            self.cancel_event.clear()
            self.run_event.set()
            for process in list(self._processes):
                self._resume_process(process)

    def register(self, process: subprocess.Popen):
        with self._lock:
            self._processes.add(process)
            if self.cancelled:
                self._terminate_process(process)
            elif self.paused:
                self._suspend_process(process)

    def unregister(self, process: subprocess.Popen):
        with self._lock:
            self._processes.discard(process)
            self._suspended.discard(process.pid)

    def cancel(self):
        with self._lock:
            self.cancel_event.set()
            self.run_event.set()
            for process in list(self._processes):
                self._terminate_process(process)

    def pause(self):
        with self._lock:
            self.run_event.clear()
            for process in list(self._processes):
                self._suspend_process(process)

    def resume(self):
        with self._lock:
            self.run_event.set()
            for process in list(self._processes):
                self._resume_process(process)

    def publish_if_active(self, temporary: str, destination: str) -> bool:
        """Atomically publish while linearizing against cancellation."""
        with self._lock:
            if self.cancelled:
                return False
            os.replace(temporary, destination)
            return True

    def _suspend_process(self, process: subprocess.Popen):
        if process.poll() is not None or process.pid in self._suspended:
            return
        try:
            if psutil is not None:
                psutil.Process(process.pid).suspend()
            elif sys.platform != "win32":
                os.kill(process.pid, signal.SIGSTOP)
            else:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(
                    0x0800, False, process.pid)
                if not handle:
                    return
                try:
                    ctypes.windll.ntdll.NtSuspendProcess(handle)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
            self._suspended.add(process.pid)
        except (OSError, ProcessLookupError):
            pass
        except Exception:
            pass

    def _resume_process(self, process: subprocess.Popen):
        if process.pid not in self._suspended:
            return
        try:
            if psutil is not None:
                psutil.Process(process.pid).resume()
            elif sys.platform != "win32":
                os.kill(process.pid, signal.SIGCONT)
            else:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(
                    0x0800, False, process.pid)
                if not handle:
                    return
                try:
                    ctypes.windll.ntdll.NtResumeProcess(handle)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except (OSError, ProcessLookupError):
            pass
        except Exception:
            pass
        finally:
            self._suspended.discard(process.pid)

    def _terminate_process(self, process: subprocess.Popen):
        if process.poll() is not None:
            return
        self._resume_process(process)
        try:
            if psutil is not None:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    child.kill()
                parent.kill()
            else:
                process.kill()
        except Exception:
            try:
                process.kill()
            except OSError:
                pass


_BITRATE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$")


def normalize_bitrate(value: str) -> Optional[str]:
    """Validate an FFmpeg bitrate token and return a normalized value."""
    if not isinstance(value, str):
        return None
    match = _BITRATE_PATTERN.fullmatch(value)
    if not match or float(match.group(1)) <= 0:
        return None
    return f"{match.group(1)}{match.group(2).lower()}"


def bitrate_to_kbps(value: str, default: float = 0.0) -> float:
    """Convert a validated bitrate token to kilobits per second."""
    normalized = normalize_bitrate(value)
    if normalized is None:
        return default
    match = _BITRATE_PATTERN.fullmatch(normalized)
    assert match is not None
    amount = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "m":
        return amount * 1000.0
    if suffix == "":
        return amount / 1000.0
    return amount


def doubled_bitrate(value: str) -> str:
    """Return a bitrate token with its numeric component doubled."""
    normalized = normalize_bitrate(value)
    if normalized is None:
        return value
    match = _BITRATE_PATTERN.fullmatch(normalized)
    assert match is not None
    doubled = float(match.group(1)) * 2
    number = str(int(doubled)) if doubled.is_integer() else f"{doubled:g}"
    return f"{number}{match.group(2).lower()}"


def resolve_target_video_bitrate(
    settings: "Settings",
    input_file: str,
    preview: bool = False,
) -> Optional[str]:
    """Resolve the usable target bitrate for bitrate-driven modes."""
    if settings.bitrate_mode in ("cbr", "vbr"):
        return normalize_bitrate(settings.target_bitrate)
    if settings.bitrate_mode != "filesize" or settings.target_size_mb <= 0:
        return None
    input_duration = _probe_duration(input_file)
    duration = expected_output_duration(input_duration, settings, preview)
    if duration <= 0:
        return None
    audio_kbps = bitrate_to_kbps(settings.audio_bitrate, default=128.0)
    target_kbps = int((settings.target_size_mb * 8192) / duration - audio_kbps)
    return f"{target_kbps}k" if target_kbps > 0 else None


def build_ffmpeg_command(
    input_file: str,
    output_file: str,
    settings: Settings,
    preview: bool = False,
    pass_number: int = 0,
    crop_filter: str = "",
    hdr_info: dict | None = None,
) -> list[str]:
    """Build the full FFmpeg command from settings.

    *pass_number*: 0 = single-pass (default), 1 = first pass, 2 = second pass.
    *crop_filter*: optional crop filter string from detect_crop().
    *hdr_info*: optional dict from probe_video() with HDR metadata.
    """
    codec = settings.codec
    crf_val = codec.crf_values[settings.quality]

    cmd = [FFMPEG_PATH]

    # Hardware-accelerated decode (must come before -i)
    if settings.hwaccel and codec.requires_gpu:
        if codec.gpu_vendor == "intel":
            cmd += ["-hwaccel", "qsv"]
        elif codec.gpu_vendor == "amd":
            cmd += ["-hwaccel", "d3d11va"]
        else:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

    # Trim: seek start (before -i for fast seek)
    if settings.trim_start and settings.trim_start > 0:
        cmd += ["-ss", str(settings.trim_start)]

    cmd += ["-i", input_file]

    # One duration cap covers trim and preview.  Emitting a single -t avoids
    # FFmpeg's "last option wins" behavior when both features are enabled.
    duration_limit = requested_duration_limit(settings, preview)
    if duration_limit is not None:
        cmd += ["-t", format_ffmpeg_seconds(duration_limit)]

    # Video codec
    cmd += ["-c:v", codec.encoder]
    cmd += codec.args

    # Bitrate mode handling (Phase 11)
    bm = settings.bitrate_mode
    target_bitrate = resolve_target_video_bitrate(
        settings, input_file, preview=preview)
    if bm == "cbr" and target_bitrate:
        cmd += [
            "-b:v", target_bitrate,
            "-minrate", target_bitrate,
            "-maxrate", target_bitrate,
            "-bufsize", doubled_bitrate(target_bitrate),
        ]
        if codec.gpu_vendor == "amd":
            cmd += ["-rc", "cbr"]
        elif codec.encoder.endswith("_nvenc"):
            cmd += ["-rc", "cbr"]
    elif bm == "vbr" and target_bitrate:
        cmd += ["-b:v", target_bitrate]
        if codec.gpu_vendor == "amd":
            cmd += ["-rc", "vbr_peak"]
        max_bitrate = normalize_bitrate(settings.max_bitrate)
        if max_bitrate:
            cmd += ["-maxrate", max_bitrate,
                    "-bufsize", max_bitrate]
    elif bm == "filesize" and target_bitrate:
        cmd += ["-b:v", target_bitrate]
        if codec.gpu_vendor == "amd":
            cmd += ["-rc", "vbr_peak"]
    else:
        # Default CRF / CQ / QP mode
        cmd += [codec.crf_flag, str(crf_val)]

    # AMF encoders need both qp_i and qp_p for constant quality
    if (getattr(codec, 'gpu_vendor', '') == "amd"
            and codec.crf_flag == "-qp_p" and bm == "crf"):
        cmd += ["-qp_i", str(crf_val)]

    # 10-bit pixel format
    if settings.ten_bit:
        pix_fmt = _10BIT_PIX_FMT.get(codec.encoder, "")
        if pix_fmt:
            # GPU codecs: if hwaccel outputs cuda surfaces, upload before pix_fmt
            if codec.requires_gpu and settings.hwaccel:
                pass  # let the hardware pipeline handle format
            else:
                cmd += ["-pix_fmt", pix_fmt]
            # libx265 needs profile flag for 10-bit
            if codec.encoder == "libx265":
                cmd += ["-profile:v", "main10"]
            elif codec.encoder == "hevc_nvenc":
                cmd += ["-profile:v", "main10"]

    # HDR handling (Phase 5)
    _apply_hdr_flags(cmd, settings, codec, hdr_info)

    # 2-pass support (CPU codecs only)
    use_two_pass = (
        pass_number in (1, 2)
        and not codec.requires_gpu
        and settings.bitrate_mode in ("cbr", "vbr", "filesize")
        and target_bitrate is not None
    )
    if use_two_pass:
        cmd += ["-pass", str(pass_number)]
        # Use a unique passlog name per file so concurrent encodes don't collide
        stem = Path(output_file).stem
        passlog = os.path.join(
            os.path.dirname(output_file) or ".",
            f"ffmpeg2pass_{stem}")
        cmd += ["-passlogfile", passlog]

    # Video filter (resolution + subtitle burn-in + crop + custom filters)
    vf_parts = []
    if crop_filter:
        vf_parts.append(crop_filter)
    if settings.subtitle_mode == "burn":
        # Escape path for subtitles filter
        escaped = input_file.replace("\\", "/").replace(":", "\\:")
        vf_parts.append(f"subtitles='{escaped}'")
    if settings.resolution:
        vf_parts.append(f"scale=-2:{settings.resolution}")
    # HDR tone-mapping filter
    if hdr_info and hdr_info.get("hdr") and settings.hdr_mode == "tonemap":
        vf_parts.append("zscale=t=linear:npl=100,"
                        "format=gbrpf32le,zscale=p=bt709:t=bt709:m=bt709,"
                        "tonemap=hable:desat=0,"
                        "zscale=t=bt709:m=bt709:r=tv,"
                        "format=yuv420p")
    # Custom video filters (Phase 7)
    if settings.video_filters:
        vf_parts.extend(settings.video_filters)
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]

    # Frame rate
    if settings.fps:
        cmd += ["-r", str(settings.fps)]

    # Advanced codec options (Phase 12)
    if settings.advanced_args:
        cmd += settings.advanced_args

    # Pass 1: discard output (only write log)
    if pass_number == 1 and use_two_pass:
        cmd += ["-an", "-sn"]
        cmd += ["-f", "null"]
        cmd += ["-progress", "pipe:1", "-nostats"]
        cmd += ["-y"]
        # On Windows, null device is NUL
        cmd += ["NUL" if sys.platform == "win32" else "/dev/null"]
        return cmd

    # Audio
    if settings.audio_codec == "copy":
        cmd += ["-c:a", "copy"]
    elif settings.audio_codec == "opus":
        cmd += ["-c:a", "libopus", "-b:a", settings.audio_bitrate]
    else:  # aac (default)
        cmd += ["-c:a", "aac", "-b:a", settings.audio_bitrate]

    # Subtitles
    if settings.subtitle_mode == "keep":
        cmd += ["-c:s", "copy"]
    elif settings.subtitle_mode in ("burn", "strip"):
        cmd += ["-sn"]

    # Progress output for parsing
    cmd += ["-progress", "pipe:1", "-nostats"]

    # Overwrite
    cmd += ["-y", output_file]

    return cmd


def execute_post_action(action: str, command: str = ""):
    """Run the post-encode action (shutdown / sleep / custom command)."""
    if action == "none":
        return
    try:
        if action == "shutdown":
            if sys.platform == "win32":
                subprocess.Popen(["shutdown", "/s", "/t", "60"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["shutdown", "-h", "+1"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        elif action == "sleep":
            if sys.platform == "win32":
                subprocess.Popen(
                    ["powershell", "-command",
                     "Add-Type -Assembly System.Windows.Forms;"
                     "[System.Windows.Forms.Application]::SetSuspendState("
                     "'Suspend', $false, $false)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
        elif action == "command" and command.strip():
            subprocess.Popen(command, shell=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        pass


def encode_file(
    input_file: str,
    output_file: str,
    settings: Settings,
    preview: bool = False,
    file_label: str = "",
) -> EncodeResult:
    """Encode a single file with real-time progress display."""
    bus = TranscodeEventBus()
    engine = TranscodeEngine(bus)
    engine.reset()
    label = file_label or Path(input_file).name

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[label]}[/]"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("•"),
        TextColumn("{task.fields[speed]}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("→"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            "Encoding", total=100.0, label=label, speed="...")

        def _on_progress(
            _file: str,
            percent: float,
            speed: str,
            fps: str,
            _eta: str,
        ):
            speed_label = " • ".join(
                part for part in (fps, speed) if part) or "..."
            progress.update(
                task, completed=min(100.0, percent), speed=speed_label)

        bus.on("progress", _on_progress)
        return engine.encode_to(
            input_file,
            output_file,
            settings,
            preview=preview,
        )



# ============================================================
#  MENUS
# ============================================================


def show_header(gpu_name: str, drag_file: Optional[str] = None):
    """Show the application header."""
    title = Text("Video Transcoder", style="bold white")
    subtitle = Text("Python Edition v1.0", style="dim")

    header_text = Text()
    header_text.append("GPU: ", style="dim")
    header_text.append(gpu_name, style="bold green" if gpu_name != "None detected" else "yellow")
    if drag_file:
        header_text.append("\nFile: ", style="dim")
        header_text.append(Path(drag_file).name, style="bold cyan")
        header_text.append(" (drag & drop)", style="dim")

    console.print()
    console.print(Panel(
        header_text,
        title=title,
        subtitle=subtitle,
        border_style="bright_blue",
        width=60,
    ))


def menu_setup_mode() -> str:
    """Ask preset or custom."""
    console.print()
    console.print("  [bold]How would you like to configure?[/]")
    console.print("  [cyan][1][/] Quick Preset Profile")
    console.print("  [cyan][2][/] Custom Settings")
    console.print()
    return Prompt.ask("  Choice", choices=["1", "2"], default="1")


def menu_preset(has_gpu: bool) -> dict:
    """Show preset selection menu."""
    console.print()
    table = Table(
        title="Preset Profiles",
        box=box.ROUNDED,
        title_style="bold",
        header_style="bold cyan",
        width=60,
    )
    table.add_column("#", style="bold", width=3)
    table.add_column("Name", width=18)
    table.add_column("Description", width=35)

    for key, preset in PRESETS.items():
        gpu_tag = ""
        if has_gpu and preset["codec_gpu"] != preset["codec_cpu"]:
            gpu_tag = " [green](GPU)[/]"
        table.add_row(key, preset["name"] + gpu_tag, preset["desc"])

    console.print(table)
    console.print()
    choice = Prompt.ask("  Choice", choices=list(PRESETS.keys()), default="2")
    return PRESETS[choice]


def menu_codec(has_gpu: bool, has_amd: bool = False,
               has_intel: bool = False) -> CodecOption:
    """Show codec selection menu."""
    codecs = get_all_codecs(has_gpu, has_amd, has_intel)

    console.print()
    table = Table(
        title="Codec Selection",
        box=box.ROUNDED,
        title_style="bold",
        header_style="bold cyan",
        width=60,
    )
    table.add_column("#", style="bold", width=3)
    table.add_column("Codec", width=22)
    table.add_column("Speed", width=10)
    table.add_column("Compression", width=12)
    table.add_column("Compatibility", width=12)

    speed_map = {
        "hevc_nvenc": ("★★★★★", "★★★★", "HEVC req."),
        "h264_nvenc": ("★★★★★", "★★★", "Universal"),
        "hevc_amf": ("★★★★★", "★★★★", "HEVC req."),
        "h264_amf": ("★★★★★", "★★★", "Universal"),
        "hevc_qsv": ("★★★★", "★★★★", "HEVC req."),
        "h264_qsv": ("★★★★", "★★★", "Universal"),
        "libx265": ("★★", "★★★★★", "HEVC req."),
        "libx264": ("★★★", "★★★", "Universal"),
        "libaom-av1": ("★", "★★★★★", "Modern"),
        "libsvtav1": ("★★★", "★★★★★", "Modern"),
    }

    for i, codec in enumerate(codecs, 1):
        speed, comp, compat = speed_map.get(codec.encoder, ("?", "?", "?"))
        gpu_tag = " [green](GPU)[/]" if codec.requires_gpu else ""
        table.add_row(str(i), codec.name + gpu_tag, speed, comp, compat)

    console.print(table)
    console.print()
    choice = IntPrompt.ask("  Choice", default=1)
    idx = max(0, min(choice - 1, len(codecs) - 1))
    return codecs[idx]


def menu_quality() -> str:
    """Show quality selection menu."""
    console.print()
    console.print("  [bold]Quality Level[/]")
    console.print("  [cyan][1][/] High   — larger files, best quality")
    console.print("  [cyan][2][/] Medium — balanced")
    console.print("  [cyan][3][/] Low    — smallest files")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="2")
    return {"1": "high", "2": "medium", "3": "low"}[choice]


def menu_resolution() -> Optional[str]:
    """Show resolution selection menu."""
    console.print()
    console.print("  [bold]Resolution[/]")
    console.print("  [cyan][1][/] Original")
    console.print("  [cyan][2][/] 1080p")
    console.print("  [cyan][3][/] 720p")
    console.print("  [cyan][4][/] 480p")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3", "4"], default="1")
    return {"1": None, "2": "1080", "3": "720", "4": "480"}[choice]


def menu_fps() -> Optional[int]:
    """Show frame rate selection menu."""
    console.print()
    console.print("  [bold]Frame Rate[/]")
    console.print("  [cyan][1][/] Original")
    console.print("  [cyan][2][/] 60 fps")
    console.print("  [cyan][3][/] 30 fps")
    console.print("  [cyan][4][/] 24 fps")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3", "4"], default="1")
    return {"1": None, "2": 60, "3": 30, "4": 24}[choice]


def menu_audio() -> str:
    """Show audio bitrate selection menu."""
    console.print()
    console.print("  [bold]Audio Bitrate[/]")
    console.print("  [cyan][1][/] 192k — high quality")
    console.print("  [cyan][2][/] 128k — standard")
    console.print("  [cyan][3][/] 96k  — compact")
    console.print("  [cyan][4][/] 64k  — voice/minimal")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3", "4"], default="2")
    return {"1": "192k", "2": "128k", "3": "96k", "4": "64k"}[choice]


def menu_format() -> str:
    """Show output format selection menu."""
    console.print()
    console.print("  [bold]Output Format[/]")
    console.print("  [cyan][1][/] MP4 — most compatible")
    console.print("  [cyan][2][/] MKV — best container features")
    console.print("  [cyan][3][/] MOV — Apple compatible")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="1")
    return {"1": "mp4", "2": "mkv", "3": "mov"}[choice]


def menu_subtitles() -> str:
    """Show subtitle mode selection menu."""
    console.print()
    console.print("  [bold]Subtitles[/]")
    console.print("  [cyan][1][/] Keep    — copy subtitle tracks")
    console.print("  [cyan][2][/] Burn in — hardcode into video")
    console.print("  [cyan][3][/] Strip   — remove all subtitles")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="1")
    return {"1": "keep", "2": "burn", "3": "strip"}[choice]


def menu_delete_originals() -> str:
    """Show delete originals selection menu."""
    console.print()
    console.print("  [bold]Delete Originals[/]")
    console.print("  [cyan][1][/] No  — keep original files")
    console.print("  [cyan][2][/] Yes — auto-delete after encoding")
    console.print("  [cyan][3][/] Ask — prompt for each file")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="1")
    return {"1": "no", "2": "yes", "3": "ask"}[choice]


def menu_skip_existing() -> bool:
    """Show skip existing selection menu."""
    console.print()
    console.print("  [bold]Skip Already-Processed[/]")
    console.print("  [cyan][1][/] Yes — skip if output exists")
    console.print("  [cyan][2][/] No  — re-encode everything")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2"], default="1")
    return choice == "1"


def menu_mode() -> str:
    """Show mode selection menu."""
    console.print()
    console.print("  [bold]Mode[/]")
    console.print("  [cyan][1][/] Batch   — all videos in folder")
    console.print("  [cyan][2][/] Single  — pick one file")
    console.print("  [cyan][3][/] Preview — first 60 sec of one file")
    console.print()
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="1")
    return {"1": "batch", "2": "single", "3": "preview"}[choice]


def menu_select_file(videos: list[Path]) -> Optional[Path]:
    """Show file selection menu."""
    console.print()
    table = Table(
        title="Select a File",
        box=box.ROUNDED,
        title_style="bold",
        header_style="bold cyan",
        width=70,
    )
    table.add_column("#", style="bold", width=4)
    table.add_column("File", width=38)
    table.add_column("Size", width=10, justify="right")
    table.add_column("Duration", width=10, justify="right")

    for i, v in enumerate(videos, 1):
        size = get_file_size_mb(str(v))
        dur = get_duration(str(v))
        table.add_row(str(i), v.name, format_size(size), format_duration(dur))

    console.print(table)
    console.print()
    choice = IntPrompt.ask("  File number", default=1)
    idx = max(0, min(choice - 1, len(videos) - 1))
    return videos[idx]


def show_settings(settings: Settings, file_count: int = 0, total_size: float = 0):
    """Display current settings summary."""
    table = Table(
        title="Settings Summary",
        box=box.ROUNDED,
        title_style="bold",
        show_header=False,
        width=60,
    )
    table.add_column("Setting", style="dim", width=16)
    table.add_column("Value", style="bold")

    table.add_row("Codec", settings.codec.name if settings.codec else "?")
    table.add_row("Quality", QUALITY_LABELS.get(settings.quality, settings.quality))
    table.add_row("Resolution", RES_LABELS.get(settings.resolution, settings.resolution or "Original"))
    table.add_row("Frame Rate", f"{settings.fps} fps" if settings.fps else "Original")
    table.add_row("Audio", settings.audio_bitrate)
    table.add_row("Format", settings.output_format.upper())
    table.add_row("Subtitles", settings.subtitle_mode.capitalize())
    table.add_row("Originals", settings.delete_originals.capitalize())
    table.add_row("Skip Existing", "Yes" if settings.skip_existing else "No")
    table.add_row("Mode", settings.mode.capitalize())

    if settings.target_file:
        table.add_row("File", Path(settings.target_file).name)
    elif file_count > 0:
        table.add_row("Files", f"{file_count} videos ({format_size(total_size)})")

    table.add_row("Output", f"{OUTPUT_DIR}/")

    console.print()
    console.print(table)


def show_results(results: list[EncodeResult], total_time: float):
    """Display final results summary."""
    done = [r for r in results if r.success]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if not r.success and not r.skipped]

    total_input = sum(r.input_size for r in done) / (1024 * 1024)
    total_output = sum(r.output_size for r in done) / (1024 * 1024)
    total_saved_pct = ((total_input - total_output) / total_input * 100) if total_input > 0 else 0
    freed = total_input - total_output

    # Results table
    if done:
        table = Table(
            title="Encoding Results",
            box=box.ROUNDED,
            title_style="bold green",
            header_style="bold cyan",
            width=78,
        )
        table.add_column("File", width=28)
        table.add_column("Original", width=10, justify="right")
        table.add_column("Compressed", width=10, justify="right")
        table.add_column("Saved", width=8, justify="right")
        table.add_column("Time", width=8, justify="right")
        table.add_column("Valid", width=6, justify="center")

        for r in done:
            in_mb = r.input_size / (1024 * 1024)
            out_mb = r.output_size / (1024 * 1024)
            saved = ((r.input_size - r.output_size) / r.input_size * 100) if r.input_size > 0 else 0
            dur_diff = abs(r.input_duration - r.output_duration)
            valid = "[green]OK[/]" if dur_diff <= 2 else f"[red]WARN[/]"
            table.add_row(
                Path(r.file).name[:28],
                format_size(in_mb),
                format_size(out_mb),
                f"{saved:.0f}%",
                format_duration(r.encode_time),
                valid,
            )

        console.print()
        console.print(table)

    # Summary panel
    summary_lines = []
    summary_lines.append(f"[green]Encoded:[/]  {len(done)} file{'s' if len(done) != 1 else ''}")
    if skipped:
        summary_lines.append(f"[yellow]Skipped:[/]  {len(skipped)}")
    if failed:
        summary_lines.append(f"[red]Failed:[/]   {len(failed)}")
    summary_lines.append(f"[dim]Time:[/]     {format_duration(total_time)}")
    if done:
        summary_lines.append(f"[dim]Original:[/]  {format_size(total_input)}")
        summary_lines.append(f"[dim]Output:[/]    {format_size(total_output)}")
        summary_lines.append(f"[bold green]Saved:[/]     {total_saved_pct:.0f}% ({format_size(freed)} freed)")
    summary_lines.append(f"[dim]Log:[/]      {LOG_FILE}")
    summary_lines.append(f"[dim]Output:[/]   {OUTPUT_DIR}/")

    console.print()
    console.print(Panel(
        "\n".join(summary_lines),
        title="[bold green]ALL DONE![/]",
        border_style="green",
        width=60,
    ))


# ============================================================
#  MAIN LOGIC
# ============================================================


def handle_delete(
    filepath: str,
    mode: str,
    expected_identity: Optional[tuple[int, int, int, int, int]] = None,
):
    """Delete an original only if it is still the file that was encoded."""
    should_delete = mode == "yes"
    if mode == "ask":
        choice = Prompt.ask(
            f"    Delete {Path(filepath).name}?",
            choices=["y", "n"],
            default="n",
        )
        should_delete = choice == "y"
    if not should_delete:
        return
    deleted, error = delete_source_if_unchanged(
        filepath, expected_identity)
    if deleted:
        console.print(f"    [red]Deleted:[/] {Path(filepath).name}")
    else:
        console.print(f"    [yellow]Original retained:[/] {error}")


def process_file(
    filepath: str,
    settings: Settings,
    preview: bool = False,
    label: str = "",
    output_path: Optional[str] = None,
) -> EncodeResult:
    """Process a single video file: skip check, encode, validate, log."""
    if output_path is None:
        try:
            output_path = build_output_path(
                filepath, settings, OUTPUT_DIR, preview)
        except ValueError as exc:
            return EncodeResult(
                file=filepath,
                success=False,
                error=f"Unsafe output filename: {exc}",
            )

    # Show file info
    input_size = get_file_size_mb(filepath)
    input_dur = get_duration(filepath)
    console.print()
    icon = ">>" if not preview else ">>"
    console.print(f"  [bold]{icon} {Path(filepath).name}[/]")
    console.print(f"    Size: {format_size(input_size)}  •  Duration: {format_duration(input_dur)}")
    if preview:
        console.print("    [dim](Preview: first 60 seconds)[/]")
    console.print()

    # Encode
    result = encode_file(filepath, output_path, settings, preview, label)

    # Show result
    if result.success:
        out_mb = result.output_size / (1024 * 1024)
        saved = ((result.input_size - result.output_size) / result.input_size * 100) if result.input_size > 0 else 0

        valid_str = (
            "[green]VALIDATED[/]"
            if result.validated
            else "[red]NOT VALIDATED[/]"
        )

        console.print(f"    [green]✓ Done[/] in {format_duration(result.encode_time)}")
        console.print(f"    {format_size(input_size)} → {format_size(out_mb)}  ({saved:.0f}% saved)  •  Validation: {valid_str}")

        # Log
        log_message(f"  [OK] {Path(filepath).name} | {input_size:.0f}MB->{out_mb:.0f}MB ({saved:.0f}%) | {format_duration(result.encode_time)} | {valid_str}")

        # Delete original
        if result.vmaf_score is not None:
            console.print(f"    VMAF: [cyan]{result.vmaf_score:.2f}[/]")
        elif result.vmaf_error:
            console.print(f"    [yellow]{result.vmaf_error}[/]")
        if result.post_copy_error:
            console.print(
                f"    [yellow]Secondary copy failed; source retained: "
                f"{result.post_copy_error}[/]")
        if not preview and result.validated and not result.post_copy_error:
            handle_delete(
                filepath,
                settings.delete_originals,
                result.input_identity,
            )
    elif result.skipped:
        console.print(
            f"  [yellow]SKIPPED[/] (validated output exists): "
            f"{Path(filepath).name}")
        log_message(
            f"  [SKIP] {Path(filepath).name} - validated output exists")
    else:
        console.print(f"    [red]✗ FAILED[/] after {format_duration(result.encode_time)}")
        if result.error:
            console.print(f"    [dim]{result.error[:200]}[/]")
        log_message(f"  [FAIL] {Path(filepath).name} | {format_duration(result.encode_time)}")

    return result


def run_batch(settings: Settings, videos: list[Path]):
    """Process all videos in batch mode."""
    try:
        planned = [
            (video, build_output_path(str(video), settings, OUTPUT_DIR))
            for video in videos
        ]
    except ValueError as exc:
        console.print(f"  [red]Unsafe output filename:[/] {exc}")
        return

    source_paths = {
        os.path.normcase(os.path.abspath(str(video))) for video in videos
    }
    destinations: dict[str, list[str]] = {}
    source_collisions: list[str] = []
    for video, output_path in planned:
        key = os.path.normcase(os.path.abspath(output_path))
        destinations.setdefault(key, []).append(video.name)
        if key in source_paths:
            source_collisions.append(f"{video.name} -> {output_path}")
    collisions = [
        names for names in destinations.values() if len(names) > 1
    ]
    if source_collisions or collisions:
        console.print(
            "  [red]Batch rejected: output paths collide with an input or "
            "another planned output.[/]")
        for item in source_collisions:
            console.print(f"    {item}")
        for names in collisions:
            console.print(f"    {', '.join(names)}")
        return
    planned_by_input = {
        os.path.normcase(os.path.abspath(str(video))): output_path
        for video, output_path in planned
    }

    total_size = sum(get_file_size_mb(str(v)) for v in videos)
    show_settings(settings, file_count=len(videos), total_size=total_size)
    console.print()

    # Confirm
    proceed = Prompt.ask("  Start encoding?", choices=["y", "n"], default="y")
    if proceed != "y":
        console.print("  [yellow]Cancelled.[/]")
        return

    results: list[EncodeResult] = []
    batch_start = time.time()

    for i, video in enumerate(videos, 1):
        label = f"[{i}/{len(videos)}] {video.name}"
        console.print()
        console.print(f"  [bold cyan]{'═' * 56}[/]")
        console.print(f"  [bold][{i}/{len(videos)}][/] {video.name}")

        # ETA
        done_results = [r for r in results if r.success]
        if done_results:
            avg_time = sum(r.encode_time for r in done_results) / len(done_results)
            remaining = len(videos) - i + 1
            eta = avg_time * remaining
            console.print(f"    ETA for remaining: ~{format_duration(eta)}")

        result = process_file(
            str(video),
            settings,
            label=label,
            output_path=planned_by_input[
                os.path.normcase(os.path.abspath(str(video)))],
        )
        results.append(result)

    total_time = time.time() - batch_start
    show_results(results, total_time)

    # Log summary
    done_count = sum(1 for r in results if r.success)
    skip_count = sum(1 for r in results if r.skipped)
    fail_count = sum(1 for r in results if not r.success and not r.skipped)
    log_message(f"  Summary: {done_count} ok, {skip_count} skipped, {fail_count} failed | {format_duration(total_time)}")
    log_message("")

    notify_complete(done_count)


def run_single(settings: Settings, videos: list[Path], preview: bool = False):
    """Process a single selected file."""
    if settings.target_file:
        target = settings.target_file
    else:
        if not videos:
            console.print("  [red]No video files found.[/]")
            return
        selected = menu_select_file(videos)
        target = str(selected)

    show_settings(settings)
    console.print()

    start_time = time.time()
    result = process_file(target, settings, preview=preview)
    total_time = time.time() - start_time

    show_results([result], total_time)
    log_message("")

    done_count = 1 if result.success else 0
    notify_complete(done_count)


# ============================================================
#  MAIN
# ============================================================


def main():
    initialize_app_state()
    os.system("title Video Transcoder - Python Edition")

    # Check FFmpeg
    if not check_ffmpeg():
        input("\nPress Enter to exit...")
        return

    # GPU detection
    console.print("  [dim]Detecting GPU...[/]", end="")
    has_gpu, gpu_name = detect_gpu()
    has_amd, amd_name = detect_amd_gpu()
    has_intel, intel_name = detect_intel_gpu()
    gpu_display = gpu_name
    if has_amd and not has_gpu:
        gpu_display = amd_name
    elif has_intel and not has_gpu and not has_amd:
        gpu_display = intel_name
    console.print(f"\r  GPU: [{'green' if (has_gpu or has_amd or has_intel) else 'yellow'}]{gpu_display}[/]          ")

    # Drag & drop detection
    drag_file = None
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        drag_file = sys.argv[1]

    # Header
    show_header(gpu_name, drag_file)

    # Check for existing outputs (resume hint)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    existing = [f for f in Path(OUTPUT_DIR).iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS]
    if existing and not drag_file:
        console.print(f"  [yellow]Found {len(existing)} file(s) in {OUTPUT_DIR}/ — can skip these to resume.[/]")

    # Init log
    log_message("")
    log_message("=" * 50)
    log_message(f"  Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_message(f"  GPU: {gpu_name}")
    log_message("=" * 50)

    # Settings
    settings = Settings()

    # Setup mode
    setup = menu_setup_mode()

    if setup == "1":
        # Preset mode
        preset = menu_preset(has_gpu)
        if has_gpu:
            encoder_name = preset["codec_gpu"]
        elif has_amd:
            _nvenc_to_amf = {"hevc_nvenc": "hevc_amf", "h264_nvenc": "h264_amf"}
            encoder_name = _nvenc_to_amf.get(preset["codec_gpu"], preset["codec_cpu"])
        elif has_intel:
            _nvenc_to_qsv = {"hevc_nvenc": "hevc_qsv", "h264_nvenc": "h264_qsv"}
            encoder_name = _nvenc_to_qsv.get(preset["codec_gpu"], preset["codec_cpu"])
        else:
            encoder_name = preset["codec_cpu"]
        settings.codec = find_codec_by_encoder(encoder_name, has_gpu, has_amd, has_intel)
        settings.quality = preset["quality"]
        settings.resolution = preset["resolution"]
        settings.fps = preset["fps"]
        settings.audio_bitrate = preset["audio"]
        settings.output_format = "mp4"
        settings.subtitle_mode = "keep"
    else:
        # Custom mode
        settings.codec = menu_codec(has_gpu, has_amd, has_intel)
        settings.quality = menu_quality()
        settings.resolution = menu_resolution()
        settings.fps = menu_fps()
        settings.audio_bitrate = menu_audio()
        settings.output_format = menu_format()
        settings.subtitle_mode = menu_subtitles()

    # Shared menus
    settings.delete_originals = menu_delete_originals()
    settings.skip_existing = menu_skip_existing()

    # Mode
    if drag_file:
        settings.mode = "single"
        settings.target_file = drag_file
    else:
        settings.mode = menu_mode()

    # Log settings
    log_message(f"  Settings: {settings.codec.name} / {settings.quality} / "
                f"{settings.resolution or 'Original'} / {settings.fps or 'Original'}fps / "
                f"{settings.audio_bitrate} / {settings.output_format} / {settings.subtitle_mode}")

    # Save config
    save_config(settings)

    # Find videos
    videos = find_videos()

    # Execute
    if settings.mode == "batch":
        if not videos:
            console.print("\n  [red]No video files found in this folder.[/]")
            input("\nPress Enter to exit...")
            return
        run_batch(settings, videos)
    elif settings.mode == "single":
        run_single(settings, videos, preview=False)
    elif settings.mode == "preview":
        run_single(settings, videos, preview=True)

    console.print()
    input("Press Enter to exit...")


# ============================================================
#  TRANSCODE ENGINE & EVENT BUS (Phase 16)
# ============================================================


class TranscodeEventBus:
    """Simple callback-based event bus for decoupling encode events
    from UI updates.

    Events emitted:
        progress  — (file, percent, speed, fps, eta)
        log       — (message,)
        started   — (file,)
        finished  — (file, EncodeResult)
        error     — (file, error_str)
        batch_done — (results_list,)
    """

    def __init__(self):
        self._listeners: dict[str, list] = {}

    def on(self, event: str, callback):
        """Register a callback for *event*."""
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback=None):
        """Unregister callback(s) for *event*."""
        if callback is None:
            self._listeners.pop(event, None)
        else:
            cbs = self._listeners.get(event, [])
            self._listeners[event] = [c for c in cbs if c is not callback]

    def emit(self, event: str, *args, **kwargs):
        """Emit *event* — calls all registered callbacks."""
        for cb in self._listeners.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception:
                pass  # never crash the emit loop


class TranscodeEngine:
    """Single safe encoding implementation shared by every frontend.

    Outputs are written to a same-directory temporary media file, validated,
    and atomically published. The supplied process controller owns the FFmpeg
    process, allowing real pause/resume and cancellation across concurrent jobs.
    """

    def __init__(
        self,
        event_bus: TranscodeEventBus | None = None,
        process_control: TranscodeProcessControl | None = None,
    ):
        self.bus = event_bus or TranscodeEventBus()
        self.control = process_control or TranscodeProcessControl()
        # Backward-compatible attributes used by existing integrations/tests.
        self._cancel = self.control.cancel_event
        self._pause = threading.Event()

    def reset(self):
        self._pause.clear()
        self.control.reset()

    def cancel(self):
        """Terminate every FFmpeg process owned by this controller."""
        self.control.cancel()

    def pause(self):
        """Toggle actual FFmpeg process suspension."""
        if self._pause.is_set():
            self._pause.clear()
            self.control.resume()
        else:
            self._pause.set()
            self.control.pause()

    def resume(self):
        self._pause.clear()
        self.control.resume()

    def encode(
        self,
        filepath: str,
        settings: Settings,
        output_dir: str = OUTPUT_DIR,
        preview: bool = False,
        gpu_index: str | None = None,
    ) -> EncodeResult:
        """Build an output name and safely encode one file."""
        try:
            out_name = build_output_filename(filepath, settings, preview)
        except ValueError as exc:
            return self._failed(
                EncodeResult(file=filepath, success=False),
                f"Unsafe output filename: {exc}",
            )
        return self.encode_to(
            filepath,
            os.path.join(output_dir, out_name),
            settings,
            preview=preview,
            gpu_index=gpu_index,
        )

    def encode_to(
        self,
        filepath: str,
        output_path: str,
        settings: Settings,
        *,
        preview: bool = False,
        gpu_index: str | None = None,
    ) -> EncodeResult:
        """Safely encode *filepath* to an explicit final output path."""
        result = EncodeResult(file=filepath, success=False,
                              output_file=output_path)
        started = time.time()
        result.input_identity = capture_file_identity(filepath)
        result.reference_start = max(0.0, float(settings.trim_start or 0.0))
        result.vmaf_sample_seconds = settings.vmaf_sample_seconds
        try:
            result.input_size = os.path.getsize(filepath)
        except OSError as exc:
            return self._failed(result, f"Unable to read input file: {exc}")
        result.input_duration = get_duration(filepath)

        if paths_refer_to_same_file(filepath, output_path):
            return self._failed(
                result,
                "Output path is the same as the input. Choose another "
                "output directory or filename.",
            )
        if not settings.audio_extract and settings.codec is None:
            return self._failed(result, "No video codec is selected.")
        if not settings.audio_extract and settings.bitrate_mode != "crf":
            target_bitrate = resolve_target_video_bitrate(
                settings, filepath, preview=preview)
            if target_bitrate is None:
                return self._failed(
                    result,
                    f"{settings.bitrate_mode.upper()} mode does not have a "
                    "valid positive video bitrate for this input.",
                )
            if (
                settings.max_bitrate
                and normalize_bitrate(settings.max_bitrate) is None
            ):
                return self._failed(
                    result, "Maximum bitrate is not a valid positive bitrate.")

        output_dir = os.path.dirname(os.path.abspath(output_path))
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            return self._failed(
                result, f"Unable to create output directory: {exc}")

        media_kind = "audio" if settings.audio_extract else "video"
        if settings.skip_existing and not preview and os.path.isfile(output_path):
            valid, message, duration = validate_output_file(
                filepath, output_path, settings, preview, media_kind)
            if self.control.cancelled:
                return self._failed(result, "Cancelled by user")
            if valid:
                result.skipped = True
                result.validated = True
                result.validation_message = message
                result.output_duration = duration
                result.output_size = os.path.getsize(output_path)
                self.bus.emit(
                    "log",
                    f"Skipping validated existing output: "
                    f"{Path(output_path).name}",
                )
                self.bus.emit("finished", filepath, result)
                return result
            self.bus.emit(
                "log",
                f"Existing output failed validation and will be replaced: "
                f"{message}",
            )

        temporary_output = make_temporary_output_path(output_path)
        self.bus.emit("started", filepath)
        self.bus.emit(
            "log",
            f"Encoding: {Path(filepath).name} -> {Path(output_path).name}",
        )

        try:
            if self.control.cancelled:
                return self._failed(result, "Cancelled by user")

            if settings.audio_extract:
                command = build_audio_extract_command(
                    filepath,
                    temporary_output,
                    settings.audio_extract_format,
                    settings.audio_bitrate,
                    settings=settings,
                    preview=preview,
                )
                ok, error, elapsed = self._run_command(
                    command,
                    filepath,
                    expected_output_duration(
                        result.input_duration, settings, preview),
                    gpu_index=gpu_index,
                )
                result.encode_time += elapsed
                if not ok:
                    return self._failed(result, error)
            else:
                hdr_info = (
                    probe_video(filepath)
                    if settings.hdr_mode != "off"
                    else None
                )
                crop = ""
                if settings.auto_crop:
                    crop = detect_crop(filepath, result.input_duration)
                    if crop:
                        self.bus.emit("log", f"Auto-crop: {crop}")

                two_pass = (
                    settings.two_pass
                    and not settings.codec.requires_gpu
                    and settings.bitrate_mode in ("cbr", "vbr", "filesize")
                    and resolve_target_video_bitrate(
                        settings, filepath, preview=False) is not None
                    and not preview
                )
                duration = expected_output_duration(
                    result.input_duration, settings, preview)

                if two_pass:
                    self.bus.emit("log", "Pass 1/2: analysis")
                    first_command = build_ffmpeg_command(
                        filepath,
                        temporary_output,
                        settings,
                        preview=False,
                        pass_number=1,
                        crop_filter=crop,
                        hdr_info=hdr_info,
                    )
                    ok, error, elapsed = self._run_command(
                        first_command,
                        filepath,
                        duration,
                        gpu_index=gpu_index,
                        progress_start=0.0,
                        progress_span=50.0,
                    )
                    result.encode_time += elapsed
                    if not ok:
                        return self._failed(result, error)
                    self.bus.emit("log", "Pass 2/2: encoding")
                    pass_number = 2
                    progress_start, progress_span = 50.0, 50.0
                else:
                    pass_number = 0
                    progress_start, progress_span = 0.0, 100.0

                command = build_ffmpeg_command(
                    filepath,
                    temporary_output,
                    settings,
                    preview=preview,
                    pass_number=pass_number,
                    crop_filter=crop,
                    hdr_info=hdr_info,
                )
                ok, error, elapsed = self._run_command(
                    command,
                    filepath,
                    duration,
                    gpu_index=gpu_index,
                    progress_start=progress_start,
                    progress_span=progress_span,
                )
                result.encode_time += elapsed
                if not ok:
                    return self._failed(result, error)

            valid, message, output_duration = validate_output_file(
                filepath,
                temporary_output,
                settings,
                preview,
                media_kind,
            )
            result.validation_message = message
            if not valid:
                return self._failed(
                    result, f"Output validation failed: {message}")

            if not self.control.publish_if_active(
                    temporary_output, output_path):
                return self._failed(result, "Cancelled by user")
            result.success = True
            result.validated = True
            result.output_duration = output_duration
            result.output_size = os.path.getsize(output_path)
            result.encode_time = max(result.encode_time, time.time() - started)

            if settings.vmaf_enabled and media_kind == "video":
                if (
                    settings.hdr_mode in ("auto", "passthrough")
                    and (probe_video(filepath).get("hdr") or False)
                ):
                    result.vmaf_error = (
                        "VMAF skipped for HDR passthrough output.")
                else:
                    self.bus.emit("log", "Calculating VMAF quality score...")
                    result.vmaf_score = run_vmaf_score(
                        filepath,
                        output_path,
                        sample_seconds=settings.vmaf_sample_seconds,
                        trim_start=float(settings.trim_start or 0.0),
                        process_control=self.control,
                    )
                    if result.vmaf_score is None:
                        result.vmaf_error = "VMAF analysis was unavailable or failed."

            if settings.post_copy_dir:
                if self.control.cancelled:
                    result.post_copy_error = (
                        "Secondary copy skipped because cancellation was "
                        "requested.")
                else:
                    self.bus.emit(
                        "log",
                        f"Copying validated output to: "
                        f"{settings.post_copy_dir}",
                    )
                    copy_target = os.path.join(
                        settings.post_copy_dir, Path(output_path).name)
                    if paths_refer_to_same_file(filepath, copy_target):
                        result.post_copy_error = (
                            "Secondary copy would overwrite the input file.")
                    else:
                        copied, copied_path, copy_error = copy_validated_output(
                            output_path, settings.post_copy_dir)
                        if copied:
                            result.post_copy_path = copied_path
                        else:
                            result.post_copy_error = copy_error

            self.bus.emit("progress", filepath, 100.0, "", "", "0:00")
            self.bus.emit("finished", filepath, result)
            return result
        except Exception as exc:
            return self._failed(result, str(exc))
        finally:
            _remove_file_quietly(temporary_output)
            _cleanup_passlog(temporary_output)

    def _run_command(
        self,
        command: list[str],
        filepath: str,
        duration: float,
        *,
        gpu_index: str | None = None,
        progress_start: float = 0.0,
        progress_span: float = 100.0,
    ) -> tuple[bool, str, float]:
        """Run one FFmpeg pass and emit normalized progress events."""
        env = os.environ.copy()
        if gpu_index is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)

        started = time.time()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
        self.control.register(process)
        stderr_lines: list[str] = []

        def _drain_stderr():
            if process.stderr is None:
                return
            for stderr_line in process.stderr:
                stderr_lines.append(stderr_line)
                clean = stderr_line.rstrip()
                if clean:
                    self.bus.emit("log", clean)

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True)
        stderr_thread.start()

        current_time = 0.0
        speed_text = ""
        fps_text = ""
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    if self.control.cancelled:
                        self.control._terminate_process(process)
                        break
                    line = raw_line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            current_time = int(line.split("=", 1)[1]) / 1_000_000
                        except (ValueError, IndexError):
                            continue
                    elif line.startswith("speed="):
                        speed_text = line.split("=", 1)[1].strip()
                    elif line.startswith("fps="):
                        try:
                            fps = float(line.split("=", 1)[1].strip())
                            fps_text = f"{fps:.0f} fps" if fps > 0 else ""
                        except (ValueError, IndexError):
                            fps_text = ""
                    else:
                        continue

                    ratio = min(1.0, current_time / duration) if duration > 0 else 0.0
                    percent = progress_start + ratio * progress_span
                    eta = ""
                    speed_match = re.match(r"([0-9.]+)x", speed_text)
                    if speed_match and duration > current_time:
                        speed_value = float(speed_match.group(1))
                        if speed_value > 0:
                            eta = format_duration(
                                (duration - current_time) / speed_value)
                    self.bus.emit(
                        "progress",
                        filepath,
                        percent,
                        speed_text,
                        fps_text,
                        eta,
                    )

            process.wait()
            stderr_thread.join(timeout=10)
        finally:
            self.control.unregister(process)

        elapsed = time.time() - started
        if self.control.cancelled:
            return False, "Cancelled by user", elapsed
        if process.returncode != 0:
            error = "".join(stderr_lines)[-2000:].strip()
            return False, error or "FFmpeg exited with an unknown error.", elapsed
        return True, "", elapsed

    def _failed(self, result: EncodeResult, error: str) -> EncodeResult:
        result.success = False
        result.error = error
        self.bus.emit("error", result.file, error)
        return result

    def encode_batch(
        self,
        files: list[str],
        settings: Settings,
        output_dir: str = OUTPUT_DIR,
    ) -> list[EncodeResult]:
        """Encode a list of files sequentially with one shared controller."""
        self.reset()
        results: list[EncodeResult] = []
        for filepath in files:
            if self.control.cancelled:
                break
            results.append(
                self.encode(filepath, settings, output_dir=output_dir))
        self.bus.emit("batch_done", results)
        return results


def _tool_version(executable: str) -> dict:
    """Return a compact executable-version check for portable diagnostics."""
    if not executable or not os.path.isfile(executable):
        return {
            "ok": False,
            "path": executable,
            "version": "",
            "error": "Executable was not found.",
        }
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        return {
            "ok": completed.returncode == 0,
            "path": str(Path(executable).resolve()),
            "version": output.splitlines()[0] if output else "",
            "error": "" if completed.returncode == 0 else output[-1000:],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "path": executable,
            "version": "",
            "error": str(exc),
        }


def _path_is_within(path: str, root: str) -> bool:
    """Return whether *path* is contained by *root*, case-insensitively."""
    if not path or not root:
        return False
    try:
        path_value = os.path.normcase(os.path.abspath(path))
        root_value = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((path_value, root_value)) == root_value
    except (OSError, ValueError):
        return False


def run_portable_self_test(report_path: str) -> int:
    """Exercise the frozen runtime, bundled tools, and safe encode engine.

    The report and generated media provide machine-readable evidence when the
    windowed executable is launched non-interactively (for example, over SSH).
    """
    global FFMPEG_PATH, FFPROBE_PATH
    FFMPEG_PATH, FFPROBE_PATH = _resolve_ffmpeg_paths()

    report_file = Path(report_path).expanduser().resolve()
    work_dir = report_file.parent
    source_file = work_dir / "portable-self-test-source.mp4"
    output_file = work_dir / "portable-self-test-encoded.mp4"
    bundle_root = str(getattr(sys, "_MEIPASS", ""))
    frozen = bool(getattr(sys, "frozen", False))
    started = datetime.now(timezone.utc)
    report: dict = {
        "schema_version": 1,
        "started_utc": started.isoformat(),
        "finished_utc": "",
        "success": False,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "runtime": {
            "frozen": frozen,
            "executable": sys.executable,
            "bundle_root": bundle_root,
            "default_output_directory": default_output_directory(),
        },
        "tools": {},
        "artifacts": {
            "source": str(source_file),
            "encoded": str(output_file),
            "report": str(report_file),
        },
        "generation": {},
        "transcode": {},
        "errors": [],
    }

    exit_code = 1
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_check = _tool_version(FFMPEG_PATH)
        ffprobe_check = _tool_version(FFPROBE_PATH)
        ffmpeg_check["bundled"] = _path_is_within(
            FFMPEG_PATH, bundle_root)
        ffprobe_check["bundled"] = _path_is_within(
            FFPROBE_PATH, bundle_root)
        report["tools"] = {
            "ffmpeg": ffmpeg_check,
            "ffprobe": ffprobe_check,
        }

        portable_runtime_ok = (
            frozen
            and ffmpeg_check["ok"]
            and ffprobe_check["ok"]
            and ffmpeg_check["bundled"]
            and ffprobe_check["bundled"]
        )
        if not portable_runtime_ok:
            report["errors"].append(
                "The executable is not using its frozen, bundled FFmpeg "
                "and FFprobe runtime.")
        else:
            generation_command = [
                FFMPEG_PATH,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(source_file),
            ]
            generated = subprocess.run(
                generation_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=SUBPROCESS_CREATION_FLAGS,
            )
            source_size = (
                source_file.stat().st_size
                if source_file.is_file()
                else 0
            )
            report["generation"] = {
                "ok": generated.returncode == 0 and source_size > 0,
                "return_code": generated.returncode,
                "source_size": source_size,
                "error": (generated.stderr or "")[-2000:].strip(),
            }
            if not report["generation"]["ok"]:
                report["errors"].append(
                    "Bundled FFmpeg could not generate the test video.")
            else:
                codec = next(
                    item for item in CODECS_CPU
                    if item.encoder == "libx264"
                )
                settings = Settings(
                    codec=codec,
                    quality="medium",
                    audio_bitrate="96k",
                    audio_codec="aac",
                    output_format="mp4",
                    subtitle_mode="strip",
                    skip_existing=False,
                    hwaccel=False,
                    hdr_mode="off",
                    notification_sound=False,
                    notification_toast=False,
                )
                result = TranscodeEngine().encode_to(
                    str(source_file),
                    str(output_file),
                    settings,
                )
                report["transcode"] = {
                    "ok": result.success and result.validated,
                    "encoder": codec.encoder,
                    "used_hardware_acceleration": settings.hwaccel,
                    "validated": result.validated,
                    "validation_message": result.validation_message,
                    "error": result.error,
                    "input_size": result.input_size,
                    "output_size": result.output_size,
                    "input_duration": result.input_duration,
                    "output_duration": result.output_duration,
                    "encode_seconds": result.encode_time,
                }
                if not report["transcode"]["ok"]:
                    report["errors"].append(
                        "The CPU libx264 engine transcode failed validation.")

        report["success"] = (
            portable_runtime_ok
            and bool(report["generation"].get("ok"))
            and bool(report["transcode"].get("ok"))
        )
        exit_code = 0 if report["success"] else 1
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["elapsed_seconds"] = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        try:
            atomic_write_json(report_file, report)
        except OSError:
            return 2
    return exit_code


if __name__ == "__main__":
    main()
