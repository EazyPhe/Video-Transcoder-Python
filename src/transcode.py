#!/usr/bin/env python3
"""
Video Transcoder v1.0 (Python)
Compress and convert video files using FFmpeg with real-time progress.
Supports NVIDIA NVENC GPU acceleration, multiple codecs, presets, and batch processing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
LOG_FILE = "transcode_log.txt"
CONFIG_FILE = "transcode_config.json"

console = Console()

# Common FFmpeg install locations on Windows (searched in order)
_FFMPEG_SEARCH_DIRS: list[str] = [
    r"C:\ffmpeg",
    r"C:\Program Files\ffmpeg",
    r"C:\Program Files (x86)\ffmpeg",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg"),
    os.path.join(os.environ.get("USERPROFILE", ""), "ffmpeg"),
]


def _find_executable(name: str) -> str:
    """
    Locate an FFmpeg executable by *name* (e.g. 'ffmpeg' or 'ffprobe').

    Search order:
      1. Already resolved & cached in the config file.
      2. On the system PATH  (shutil.which).
      3. Common Windows install directories (recursive glob for <name>.exe).
    Returns the absolute path, or an empty string if not found.
    """
    # 1. Check config file for a previously saved path
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            saved = cfg.get(f"{name}_path", "")
            if saved and os.path.isfile(saved):
                return saved
    except (json.JSONDecodeError, OSError):
        pass

    # 2. System PATH
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    # 3. Common directories (look for <name>.exe recursively)
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
    post_upload: str = ""  # post-encode upload command / path


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
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def save_config(settings: Settings):
    """Save current settings as default config (including FFmpeg paths)."""
    config = {
        "ffmpeg_path": FFMPEG_PATH,
        "ffprobe_path": FFPROBE_PATH,
        "codec_encoder": settings.codec.encoder if settings.codec else None,
        "quality": settings.quality,
        "resolution": settings.resolution,
        "fps": settings.fps,
        "audio_bitrate": settings.audio_bitrate,
        "audio_codec": settings.audio_codec,
        "output_format": settings.output_format,
        "subtitle_mode": settings.subtitle_mode,
        "delete_originals": settings.delete_originals,
        "skip_existing": settings.skip_existing,
        "hwaccel": settings.hwaccel,
        "ten_bit": settings.ten_bit,
        "two_pass": settings.two_pass,
        "filename_template": settings.filename_template,
        "post_action": settings.post_action,
        "post_command": settings.post_command,
        "concurrent": settings.concurrent,
        "auto_crop": settings.auto_crop,
        "audio_extract": settings.audio_extract,
        "audio_extract_format": settings.audio_extract_format,
        "notification_sound": settings.notification_sound,
        "notification_toast": settings.notification_toast,
        "hdr_mode": settings.hdr_mode,
        "bitrate_mode": settings.bitrate_mode,
        "target_bitrate": settings.target_bitrate,
        "max_bitrate": settings.max_bitrate,
        "target_size_mb": settings.target_size_mb,
    }
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def load_config() -> dict:
    """Load saved config from JSON. Returns empty dict on failure."""
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


CUSTOM_PRESETS_FILE = "custom_presets.json"
QUEUE_FILE = "transcode_queue.json"


def save_custom_preset(name: str, settings: Settings):
    """Save a named custom preset to disk."""
    presets = load_custom_presets()
    presets[name] = {
        "codec_encoder": settings.codec.encoder if settings.codec else None,
        "quality": settings.quality,
        "resolution": settings.resolution,
        "fps": settings.fps,
        "audio_bitrate": settings.audio_bitrate,
        "audio_codec": settings.audio_codec,
        "output_format": settings.output_format,
        "subtitle_mode": settings.subtitle_mode,
        "delete_originals": settings.delete_originals,
        "skip_existing": settings.skip_existing,
        "hwaccel": settings.hwaccel,
        "ten_bit": settings.ten_bit,
        "two_pass": settings.two_pass,
        "filename_template": settings.filename_template,
        "post_action": settings.post_action,
        "post_command": settings.post_command,
        "concurrent": settings.concurrent,
        "auto_crop": settings.auto_crop,
        "audio_extract": settings.audio_extract,
        "audio_extract_format": settings.audio_extract_format,
        "notification_sound": settings.notification_sound,
        "notification_toast": settings.notification_toast,
    }
    try:
        with open(CUSTOM_PRESETS_FILE, "w") as f:
            json.dump(presets, f, indent=2)
    except OSError:
        pass


def load_custom_presets() -> dict:
    """Load custom presets from disk. Returns dict of name -> preset dict."""
    try:
        if os.path.isfile(CUSTOM_PRESETS_FILE):
            with open(CUSTOM_PRESETS_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def delete_custom_preset(name: str):
    """Delete a named custom preset from disk."""
    presets = load_custom_presets()
    if name in presets:
        del presets[name]
        try:
            with open(CUSTOM_PRESETS_FILE, "w") as f:
                json.dump(presets, f, indent=2)
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
    if settings.bitrate_mode != "crf" and settings.two_pass:
        warnings.append("2-pass is designed for CRF mode. Other bitrate modes may ignore it.")
    if settings.bitrate_mode == "filesize" and settings.target_size_mb <= 0:
        warnings.append("File-size bitrate mode requires a target size > 0 MB.")
    if settings.bitrate_mode in ("cbr", "vbr") and not settings.target_bitrate:
        warnings.append(f"{settings.bitrate_mode.upper()} mode requires a target bitrate.")
    return warnings


# ============================================================
#  AUDIO EXTRACTION
# ============================================================


def build_audio_extract_command(
    input_file: str,
    output_file: str,
    format_key: str = "mp3",
    bitrate: str = "192k",
) -> list[str]:
    """Build FFmpeg command to extract audio only."""
    fmt = AUDIO_EXTRACT_FORMATS.get(format_key, AUDIO_EXTRACT_FORMATS["mp3"])
    cmd = [FFMPEG_PATH, "-i", input_file, "-vn", "-sn"]
    if fmt["codec"] == "flac":
        cmd += ["-c:a", "flac"]
    else:
        cmd += ["-c:a", fmt["codec"], "-b:a", bitrate]
    cmd += ["-progress", "pipe:1", "-nostats", "-y", output_file]
    return cmd


# ============================================================
#  QUEUE PERSISTENCE
# ============================================================


def save_queue(items: list[dict]):
    """Save queue items to disk for persistence across restarts."""
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except OSError:
        pass


def load_queue() -> list[dict]:
    """Load saved queue items from disk."""
    try:
        if os.path.isfile(QUEUE_FILE):
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return []


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
) -> list[float]:
    """Detect scene changes using FFmpeg's scene filter.

    Returns a list of timestamps (seconds) where scene changes occur.
    """
    if not FFMPEG_PATH or not os.path.isfile(filepath):
        return []
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-i", filepath,
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        timestamps = []
        for line in result.stderr.splitlines():
            if "pts_time:" in line:
                for part in line.split():
                    if part.startswith("pts_time:"):
                        try:
                            timestamps.append(float(part.split(":")[1]))
                        except (ValueError, IndexError):
                            pass
        return timestamps
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


# ============================================================
#  VMAF QUALITY SCORING (Phase 10)
# ============================================================


def run_vmaf_score(
    reference: str,
    distorted: str,
) -> float | None:
    """Run FFmpeg libvmaf filter to compute VMAF score.

    Returns the harmonic mean score, or None on failure.
    Requires FFmpeg built with libvmaf support.
    """
    if not FFMPEG_PATH:
        return None
    try:
        result = subprocess.run(
            [FFMPEG_PATH,
             "-i", distorted, "-i", reference,
             "-lavfi", "libvmaf=log_fmt=json:log_path=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
        # libvmaf prints JSON to the designated log path; when log_path=-
        # some builds print to stderr.
        for line in result.stderr.splitlines():
            if '"mean"' in line.lower() or '"harmonic_mean"' in line.lower():
                import re as _re
                m = _re.search(r"[\d.]+", line)
                if m:
                    return float(m.group())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


# ============================================================
#  QUEUE IMPORT / EXPORT (Phase 9)
# ============================================================


def export_queue(items: list[dict], filepath: str) -> bool:
    """Export queue items to a shareable JSON file."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        return True
    except OSError:
        return False


def import_queue(filepath: str) -> list[dict]:
    """Import queue items from a JSON file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


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

    # Trim: end time (after -i)
    if settings.trim_end and settings.trim_end > 0:
        if settings.trim_start and settings.trim_start > 0:
            duration = settings.trim_end - settings.trim_start
            if duration > 0:
                cmd += ["-t", str(duration)]
        else:
            cmd += ["-to", str(settings.trim_end)]

    # Video codec
    cmd += ["-c:v", codec.encoder]
    cmd += codec.args

    # Bitrate mode handling (Phase 11)
    bm = settings.bitrate_mode
    if bm == "cbr" and settings.target_bitrate:
        cmd += ["-b:v", settings.target_bitrate]
    elif bm == "vbr" and settings.target_bitrate:
        cmd += ["-b:v", settings.target_bitrate]
        if settings.max_bitrate:
            cmd += ["-maxrate", settings.max_bitrate,
                    "-bufsize", settings.max_bitrate]
    elif bm == "filesize" and settings.target_size_mb > 0:
        # Rough bitrate calc from target size.
        # Assumes 128k audio; result in kbps
        dur = _probe_duration(input_file)
        if dur > 0:
            target_kbps = int((settings.target_size_mb * 8192) / dur) - 128
            if target_kbps > 0:
                cmd += ["-b:v", f"{target_kbps}k"]
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
    if pass_number in (1, 2) and not codec.requires_gpu:
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
    if pass_number == 1:
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

    # Preview: first 60 seconds
    if preview:
        cmd += ["-t", "60"]

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
    result = EncodeResult(file=input_file)
    result.input_size = os.path.getsize(input_file)
    result.input_duration = get_duration(input_file)

    total_duration = min(result.input_duration, 60) if preview else result.input_duration
    if total_duration <= 0:
        total_duration = 1  # Avoid division by zero

    crop = ""
    if settings.auto_crop:
        crop = detect_crop(input_file, result.input_duration)
    cmd = build_ffmpeg_command(input_file, output_file, settings, preview,
                               crop_filter=crop)

    start_time = time.time()

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Drain stderr in a background thread to prevent pipe deadlock
        stderr_lines = []
        def _drain_stderr():
            for line in process.stderr:
                stderr_lines.append(line)
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Real-time progress bar
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
                "Encoding",
                total=total_duration,
                label=label,
                speed="...",
            )

            current_time = 0
            speed_str = "..."

            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=")[1])
                        current_time = us / 1_000_000
                        progress.update(task, completed=min(current_time, total_duration))
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("speed="):
                    speed_val = line.split("=")[1].strip()
                    if speed_val and speed_val != "N/A":
                        speed_str = speed_val
                        progress.update(task, speed=speed_str)
                elif line.startswith("fps="):
                    try:
                        fps_val = line.split("=")[1].strip()
                        if fps_val and float(fps_val) > 0:
                            progress.update(task, speed=f"{float(fps_val):.0f} fps • {speed_str}")
                    except (ValueError, IndexError):
                        pass

        # Wait for process to finish
        process.wait()
        stderr_thread.join(timeout=10)
        stderr = "".join(stderr_lines)

        result.encode_time = time.time() - start_time

        if process.returncode == 0:
            result.success = True
            if os.path.isfile(output_file):
                result.output_size = os.path.getsize(output_file)
                result.output_duration = get_duration(output_file)
        else:
            result.success = False
            result.error = stderr[-500:] if stderr else "Unknown error"

    except subprocess.TimeoutExpired:
        process.kill()
        result.success = False
        result.error = "Encoding timed out"
    except Exception as e:
        result.success = False
        result.error = str(e)

    return result


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


def handle_delete(filepath: str, mode: str):
    """Handle original file deletion based on settings."""
    if mode == "yes":
        try:
            os.remove(filepath)
            console.print(f"    [red]Deleted:[/] {Path(filepath).name}")
        except OSError as e:
            console.print(f"    [red]Delete failed:[/] {e}")
    elif mode == "ask":
        choice = Prompt.ask(f"    Delete {Path(filepath).name}?", choices=["y", "n"], default="n")
        if choice == "y":
            try:
                os.remove(filepath)
                console.print(f"    [red]Deleted.[/]")
            except OSError as e:
                console.print(f"    [red]Delete failed:[/] {e}")


def process_file(
    filepath: str,
    settings: Settings,
    preview: bool = False,
    label: str = "",
) -> EncodeResult:
    """Process a single video file: skip check, encode, validate, log."""
    name = Path(filepath).stem
    ext = settings.output_format

    if preview:
        out_name = f"{name}_preview.{ext}"
    elif (settings.filename_template
          and settings.filename_template != "{name}"):
        out_name = (
            f"{render_filename_template(settings.filename_template, filepath, settings)}.{ext}"
        )
    else:
        out_name = f"{name}.{ext}"

    output_path = os.path.join(OUTPUT_DIR, out_name)

    # Skip check
    if settings.skip_existing and os.path.isfile(output_path) and not preview:
        console.print(f"  [yellow]SKIPPED[/] (output exists): {Path(filepath).name}")
        log_message(f"  [SKIP] {Path(filepath).name} - output exists")
        result = EncodeResult(file=filepath, success=False, skipped=True)
        return result

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

        # Validation
        if not preview:
            dur_diff = abs(result.input_duration - result.output_duration)
            valid_str = "[green]OK[/]" if dur_diff <= 2 else f"[red]WARN: {dur_diff:.0f}s mismatch[/]"
        else:
            valid_str = "[dim]preview[/]"

        console.print(f"    [green]✓ Done[/] in {format_duration(result.encode_time)}")
        console.print(f"    {format_size(input_size)} → {format_size(out_mb)}  ({saved:.0f}% saved)  •  Validation: {valid_str}")

        # Log
        log_message(f"  [OK] {Path(filepath).name} | {input_size:.0f}MB->{out_mb:.0f}MB ({saved:.0f}%) | {format_duration(result.encode_time)} | {valid_str}")

        # Delete original
        if not preview:
            handle_delete(filepath, settings.delete_originals)
    else:
        console.print(f"    [red]✗ FAILED[/] after {format_duration(result.encode_time)}")
        if result.error:
            console.print(f"    [dim]{result.error[:200]}[/]")
        log_message(f"  [FAIL] {Path(filepath).name} | {format_duration(result.encode_time)}")

    return result


def run_batch(settings: Settings, videos: list[Path]):
    """Process all videos in batch mode."""
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

        result = process_file(str(video), settings, label=label)
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
    """High-level encoding coordinator.

    Wraps the procedural encoding helpers into a class with a clean API.
    Emits events via an attached TranscodeEventBus so UIs can subscribe
    without coupling to implementation details.

    Usage::

        bus = TranscodeEventBus()
        engine = TranscodeEngine(bus)
        bus.on("progress", lambda f, pct, *_: print(f"{f}: {pct}%"))
        engine.encode(filepath, settings)
    """

    def __init__(self, event_bus: TranscodeEventBus | None = None):
        self.bus = event_bus or TranscodeEventBus()
        self._cancel = threading.Event()
        self._pause = threading.Event()

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def encode(
        self,
        filepath: str,
        settings: Settings,
        output_dir: str = OUTPUT_DIR,
        preview: bool = False,
    ) -> EncodeResult:
        """Encode a single file and return an EncodeResult.

        Emits *started*, *progress*, *finished*, and *error* events.
        """
        self._cancel.clear()
        self._pause.clear()

        stem = Path(filepath).stem
        ext = settings.output_format
        if preview:
            out_name = f"{stem}_preview.{ext}"
        elif settings.filename_template and settings.filename_template != "{name}":
            out_name = f"{render_filename_template(settings.filename_template, filepath, settings)}.{ext}"
        else:
            out_name = f"{stem}.{ext}"

        output_path = os.path.join(output_dir, out_name)

        self.bus.emit("started", filepath)
        self.bus.emit("log", f"Encoding: {Path(filepath).name} -> {out_name}")

        try:
            # Probe for HDR
            hdr_info = probe_video(filepath) if settings.hdr_mode != "off" else None

            # Auto-crop
            crop = ""
            if settings.auto_crop and not settings.audio_extract:
                crop = detect_crop(filepath)
                if crop:
                    self.bus.emit("log", f"  Auto-crop: {crop}")

            cmd = build_ffmpeg_command(
                filepath, output_path, settings,
                preview=preview, crop_filter=crop, hdr_info=hdr_info)

            start_time = time.time()
            duration = get_duration(filepath) or 0.0

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace",
            )

            # Drain stderr in background
            stderr_lines: list[str] = []

            def _drain():
                for line in proc.stderr:
                    stderr_lines.append(line)

            t = threading.Thread(target=_drain, daemon=True)
            t.start()

            # Parse progress
            for line in proc.stdout:
                if self._cancel.is_set():
                    proc.kill()
                    self.bus.emit("error", filepath, "Cancelled")
                    return EncodeResult(file=filepath, success=False,
                                        error="Cancelled", output_file=output_path)
                while self._pause.is_set():
                    time.sleep(0.2)

                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=")[1])
                        pct = min(100, int(us / (duration * 1_000_000) * 100)) if duration > 0 else 0
                        self.bus.emit("progress", filepath, pct, "", "", "")
                    except (ValueError, ZeroDivisionError):
                        pass

            proc.wait()
            t.join(timeout=5)
            elapsed = time.time() - start_time

            if proc.returncode != 0:
                err = "".join(stderr_lines[-5:]) if stderr_lines else "Unknown error"
                self.bus.emit("error", filepath, err)
                return EncodeResult(file=filepath, success=False,
                                    error=err, encode_time=elapsed,
                                    output_file=output_path)

            in_size = get_file_size_mb(filepath)
            out_size = get_file_size_mb(output_path)

            result = EncodeResult(
                file=filepath, success=True,
                input_size=int(in_size * 1024 * 1024),
                output_size=int(out_size * 1024 * 1024),
                input_duration=duration,
                output_duration=get_duration(output_path) or 0,
                encode_time=elapsed,
                output_file=output_path,
            )
            self.bus.emit("finished", filepath, result)
            return result

        except Exception as exc:
            self.bus.emit("error", filepath, str(exc))
            return EncodeResult(file=filepath, success=False,
                                error=str(exc), output_file=output_path)

    def cancel(self):
        """Signal cancellation to the running encode."""
        self._cancel.set()

    def pause(self):
        """Toggle pause state."""
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def encode_batch(
        self,
        files: list[str],
        settings: Settings,
        output_dir: str = OUTPUT_DIR,
    ) -> list[EncodeResult]:
        """Encode a list of files sequentially.

        Emits *batch_done* when all files are processed.
        """
        results: list[EncodeResult] = []
        for fp in files:
            if self._cancel.is_set():
                break
            r = self.encode(fp, settings, output_dir=output_dir)
            results.append(r)
        self.bus.emit("batch_done", results)
        return results


if __name__ == "__main__":
    main()
