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
    crf_flag: str  # "-crf" or "-cq"
    crf_values: dict[str, int]  # {"high": 20, "medium": 28, "low": 34}
    requires_gpu: bool = False


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
    ),
    CodecOption(
        name="H.264 GPU (NVENC)",
        encoder="h264_nvenc",
        args=["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-rc-lookahead", "32",
              "-spatial-aq", "1", "-temporal-aq", "1"],
        crf_flag="-cq",
        crf_values={"high": 20, "medium": 26, "low": 32},
        requires_gpu=True,
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
        name="AV1 CPU",
        encoder="libaom-av1",
        args=["-b:v", "0", "-cpu-used", "4", "-row-mt", "1", "-tiles", "2x2"],
        crf_flag="-crf",
        crf_values={"high": 22, "medium": 30, "low": 38},
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
    "libx265": "yuv420p10le",
    "libx264": "yuv420p10le",
    "libaom-av1": "yuv420p10le",
}

# Filename template tokens: {name}, {codec}, {quality}, {res}, {fps}, {date}
FILENAME_TEMPLATES = [
    "{name}",
    "{name}_{codec}_{quality}",
    "{name}_{res}_{quality}",
    "{name}_{codec}_{quality}_{date}",
    "{name}_{date}",
]


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
    }
    if not FFPROBE_PATH or not os.path.isfile(filepath):
        return info
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error",
             "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,width,height,bit_rate,r_frame_rate,pix_fmt,"
             "bits_per_raw_sample",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            s = streams[0]
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
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError,
            OSError):
        pass

    # Audio stream
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate,channels",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            s = streams[0]
            info["audio_codec"] = s.get("codec_name", "")
            abr = s.get("bit_rate", "")
            if abr and abr.isdigit():
                info["audio_bitrate"] = f"{int(abr) // 1000} kbps"
            ch = s.get("channels", "")
            if ch:
                info["audio_channels"] = str(ch)
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


def get_all_codecs(has_gpu: bool) -> list[CodecOption]:
    """Get available codecs based on GPU detection."""
    if has_gpu:
        return CODECS_GPU + CODECS_CPU
    return CODECS_CPU


def find_codec_by_encoder(encoder: str, has_gpu: bool) -> Optional[CodecOption]:
    """Find a codec option by encoder name."""
    for codec in get_all_codecs(has_gpu):
        if codec.encoder == encoder:
            return codec
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


def notify_complete(file_count: int):
    """Play notification sound and show toast."""
    # Beep
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
#  ENCODING
# ============================================================


def build_ffmpeg_command(
    input_file: str,
    output_file: str,
    settings: Settings,
    preview: bool = False,
    pass_number: int = 0,
) -> list[str]:
    """Build the full FFmpeg command from settings.

    *pass_number*: 0 = single-pass (default), 1 = first pass, 2 = second pass.
    """
    codec = settings.codec
    crf_val = codec.crf_values[settings.quality]

    cmd = [FFMPEG_PATH]

    # Hardware-accelerated decode (must come before -i)
    if settings.hwaccel and codec.requires_gpu:
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
    cmd += [codec.crf_flag, str(crf_val)]

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

    # 2-pass support (CPU codecs only)
    if pass_number in (1, 2) and not codec.requires_gpu:
        cmd += ["-pass", str(pass_number)]
        passlog = os.path.join(
            os.path.dirname(output_file) or ".",
            "ffmpeg2pass")
        cmd += ["-passlogfile", passlog]

    # Video filter (resolution + subtitle burn-in)
    vf_parts = []
    if settings.subtitle_mode == "burn":
        # Escape path for subtitles filter
        escaped = input_file.replace("\\", "/").replace(":", "\\:")
        vf_parts.append(f"subtitles='{escaped}'")
    if settings.resolution:
        vf_parts.append(f"scale=-2:{settings.resolution}")
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]

    # Frame rate
    if settings.fps:
        cmd += ["-r", str(settings.fps)]

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

    cmd = build_ffmpeg_command(input_file, output_file, settings, preview)

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


def menu_codec(has_gpu: bool) -> CodecOption:
    """Show codec selection menu."""
    codecs = get_all_codecs(has_gpu)

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
        "libx265": ("★★", "★★★★★", "HEVC req."),
        "libx264": ("★★★", "★★★", "Universal"),
        "libaom-av1": ("★", "★★★★★", "Modern"),
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
    console.print(f"\r  GPU: [{'green' if has_gpu else 'yellow'}]{gpu_name}[/]          ")

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
        encoder_name = preset["codec_gpu"] if has_gpu else preset["codec_cpu"]
        settings.codec = find_codec_by_encoder(encoder_name, has_gpu)
        settings.quality = preset["quality"]
        settings.resolution = preset["resolution"]
        settings.fps = preset["fps"]
        settings.audio_bitrate = preset["audio"]
        settings.output_format = "mp4"
        settings.subtitle_mode = "keep"
    else:
        # Custom mode
        settings.codec = menu_codec(has_gpu)
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


if __name__ == "__main__":
    main()
