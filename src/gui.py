#!/usr/bin/env python3
"""
Video Transcoder GUI v3.2 -- CustomTkinter frontend.
Reuses all encoding logic from transcode.py.

Features:
  - Drag-and-drop files onto window (tkinterdnd2 if available)
  - Per-file progress table with status icons
  - Queue management (add / remove / reorder / clear)
  - Load & save settings (remembers last session)
  - Output folder selector + Open output folder button
  - Delete originals option (Keep / Delete / Ask Each)
  - 60-second preview mode
  - Pause / Resume button
  - Hardware decode toggle (hwaccel cuda)
  - Audio codec selection (AAC / Opus / Copy)
  - Estimated output size before encoding
  - Dark / Light theme toggle
  - Recursive folder scanning
  - Video thumbnail preview (via ffmpeg)
  - Encoding history tab
  - System tray minimization (pystray + Pillow if available)
  - Multi-GPU selection
"""

import json
import os
import re
import signal
import sys
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
#  Import core logic from the CLI module
# ---------------------------------------------------------------------------
from transcode import (
    FFMPEG_PATH,
    FFPROBE_PATH,
    APP_PATHS,
    VIDEO_EXTENSIONS,
    OUTPUT_DIR,
    LOG_FILE,
    CONFIG_FILE,
    CodecOption,
    Settings,
    EncodeResult,
    CODECS_GPU,
    CODECS_CPU,
    CODECS_AMD,
    CODECS_INTEL,
    PRESETS,
    QUALITY_LABELS,
    RES_LABELS,
    AUDIO_LABELS,
    FILENAME_TEMPLATES,
    AUDIO_EXTRACT_FORMATS,
    _10BIT_PIX_FMT,
    detect_gpu,
    detect_amd_gpu,
    detect_intel_gpu,
    check_ffmpeg,
    get_duration,
    get_file_size_mb,
    format_duration,
    format_size,
    find_videos,
    get_all_codecs,
    find_codec_by_encoder,
    log_message,
    save_config,
    load_config,
    update_config_values,
    build_ffmpeg_command,
    notify_complete,
    probe_video,
    execute_post_action,
    save_custom_preset,
    load_custom_presets,
    delete_custom_preset,
    CUSTOM_PRESETS_FILE,
    detect_crop,
    validate_settings,
    save_queue,
    load_queue,
    QUEUE_FILE,
    get_system_stats,
    resolve_preset_codec,
    build_subtitle_extract_command,
    detect_scenes,
    run_vmaf_score,
    export_queue,
    import_queue,
    ADVANCED_OPTIONS,
    TranscodeEventBus,
    TranscodeEngine,
    TranscodeProcessControl,
    apply_settings_override,
    sanitize_portable_override,
    settings_to_dict,
    merge_portable_overrides,
    build_output_path as build_safe_output_path,
    default_output_directory,
    delete_source_if_unchanged,
    initialize_app_state,
    run_portable_self_test,
    SUBPROCESS_CREATION_FLAGS,
    validate_output_file,
)

# ---------------------------------------------------------------------------
#  GUI dependency check
# ---------------------------------------------------------------------------
try:
    import customtkinter as ctk
except ImportError:
    print("\n  Missing 'customtkinter' library. Install it with:")
    print("  pip install customtkinter\n")
    sys.exit(1)

import tkinter as tk
from tkinter import filedialog, messagebox

# Optional: tkinterdnd2 for native drag-and-drop
_HAS_DND = False
try:
    import tkinterdnd2
    _HAS_DND = True
except ImportError:
    pass

# Optional: pystray + Pillow for system tray
_HAS_TRAY = False
try:
    import pystray
    from PIL import Image as PilImage, ImageDraw
    _HAS_TRAY = True
except ImportError:
    pass

# Optional: Pillow for thumbnails (may be present even without pystray)
_HAS_PIL = False
try:
    from PIL import Image as _PilImg, ImageTk as _PilImgTk
    _HAS_PIL = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
#  Theme / appearance defaults
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

POLL_MS = 100  # progress poll interval


# ============================================================
#  GPU ENUMERATION (multi-GPU)
# ============================================================

def detect_all_gpus() -> list[dict]:
    """Return list of dicts with 'index' and 'name' for each NVIDIA GPU."""
    gpus: list[dict] = []
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split(",", 1)
                if len(parts) == 2:
                    gpus.append({
                        "index": parts[0].strip(),
                        "name": parts[1].strip(),
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return gpus


# ============================================================
#  THUMBNAIL GENERATOR
# ============================================================

def generate_thumbnail(
    video_path: str, output_path: str, size: str = "192x108",
) -> bool:
    """Extract a single frame from *video_path* as a PNG thumbnail."""
    try:
        subprocess.run(
            [FFMPEG_PATH, "-y", "-ss", "5", "-i", video_path,
             "-frames:v", "1", "-s", size, "-f", "image2", output_path],
            capture_output=True, timeout=10,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
        return os.path.isfile(output_path)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ============================================================
#  ESTIMATED OUTPUT SIZE (rough)
# ============================================================

_BITRATE_EST = {
    "hevc_nvenc":  {"high": 5000, "medium": 3000, "low": 1500},
    "h264_nvenc":  {"high": 8000, "medium": 5000, "low": 2500},
    "hevc_amf":    {"high": 5000, "medium": 3000, "low": 1500},
    "h264_amf":    {"high": 8000, "medium": 5000, "low": 2500},
    "hevc_qsv":    {"high": 5000, "medium": 3000, "low": 1500},
    "h264_qsv":    {"high": 8000, "medium": 5000, "low": 2500},
    "libx265":     {"high": 4000, "medium": 2500, "low": 1200},
    "libx264":     {"high": 7000, "medium": 4500, "low": 2000},
    "libaom-av1":  {"high": 3500, "medium": 2000, "low": 1000},
    "libsvtav1":   {"high": 3500, "medium": 2000, "low": 1000},
}
_RES_SCALE = {None: 1.0, "1080": 1.0, "720": 0.56, "480": 0.25}


def estimate_output_mb(
    duration_s: float, encoder: str, quality: str,
    resolution: Optional[str], audio_br: str,
) -> float:
    """Very rough output-size estimate in MB."""
    vbr = _BITRATE_EST.get(encoder, {}).get(quality, 3000)
    vbr *= _RES_SCALE.get(resolution, 1.0)
    abr_num = int(audio_br.replace("k", "")) if audio_br.replace("k", "").isdigit() else 128
    total_kbps = vbr + abr_num
    return max((total_kbps / 8) * duration_s / 1024, 0.1)


# ============================================================
#  ENCODE HELPER (with pause / cancel support)
# ============================================================


# ---- Tooltip helper --------------------------------------------------
class _ToolTip:
    """Simple hover tooltip for CustomTkinter widgets."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw, text=self.text, justify="left",
            background="#333333", foreground="white",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 9), padx=6, pady=3)
        label.pack()

    def _hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def encode_file_gui(
    input_file: str,
    output_file: str,
    settings: Settings,
    *,
    preview: bool = False,
    on_progress: Optional[callable] = None,
    on_log: Optional[callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    gpu_index: Optional[str] = None,
    pass_number: int = 0,
    crop_filter: str = "",
    process_control: Optional[TranscodeProcessControl] = None,
) -> EncodeResult:
    """Encode one file, calling back for progress / log updates.

    *pause_event*: when **cleared**, the read-loop blocks until set again.
    *gpu_index*: selects a specific NVIDIA GPU via ``CUDA_VISIBLE_DEVICES``.
    ``pass_number`` and ``crop_filter`` remain only for compatibility and are
    rejected when supplied; the engine owns two-pass and crop decisions.
    """
    if pass_number != 0 or crop_filter:
        raise ValueError(
            "pass_number and crop_filter are engine-managed; configure "
            "Settings.two_pass or Settings.auto_crop instead.")
    bus = TranscodeEventBus()
    if on_progress:
        bus.on(
            "progress",
            lambda _file, pct, speed, fps, eta:
            on_progress(pct, speed, fps, eta),
        )
    if on_log:
        bus.on("log", on_log)
    control = process_control or TranscodeProcessControl(
        cancel_event, pause_event)
    engine = TranscodeEngine(bus, control)
    return engine.encode_to(
        input_file,
        output_file,
        settings,
        preview=preview,
        gpu_index=gpu_index,
    )


# ============================================================
#  QUEUE ITEM
# ============================================================

@dataclass
class QueueItem:
    path: str
    size_mb: float = 0.0
    duration: float = 0.0
    status: str = "queued"       # queued / encoding / done / failed / skipped / cancelled
    result: Optional[EncodeResult] = None
    est_mb: float = 0.0
    metadata: Optional[dict] = None  # probe_video() output
    settings_override: dict = field(default_factory=dict)
    output_dir_override: Optional[str] = None


# ============================================================
#  PRESET PICKER DIALOG
# ============================================================

class _PresetPicker(ctk.CTkToplevel):
    """Simple dialog to pick a preset name from a list."""

    def __init__(self, parent, title: str, names: list[str]):
        super().__init__(parent)
        self.title(title)
        self.geometry("320x350")
        self.resizable(False, False)
        self.result: Optional[str] = None
        self.grab_set()

        ctk.CTkLabel(self, text="Select a preset:",
                      font=ctk.CTkFont(weight="bold")).pack(
                          padx=12, pady=(12, 4))

        self._listbox = tk.Listbox(
            self, selectmode="single", font=("Consolas", 11),
            bg="#2b2b2b", fg="white", selectbackground="#3b8ed0",
            highlightthickness=0)
        self._listbox.pack(fill="both", expand=True, padx=12, pady=4)
        for n in names:
            self._listbox.insert("end", n)

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=12, pady=(4, 12))
        ctk.CTkButton(bf, text="OK", width=80,
                       command=self._ok).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Cancel", width=80,
                       fg_color="#555",
                       command=self.destroy).pack(side="left", padx=4)

    def _ok(self):
        sel = self._listbox.curselection()
        if sel:
            self.result = self._listbox.get(sel[0])
        self.destroy()


# ============================================================
#  APPLICATION
# ============================================================

class TranscoderApp(ctk.CTk):
    """Main Video Transcoder GUI."""

    W, H = 1000, 860

    def __init__(self):
        super().__init__()

        self.title("Video Transcoder")
        self.geometry(f"{self.W}x{self.H}")
        self.minsize(820, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- State ---------------------------------------------------
        self.has_gpu, self.gpu_name = detect_gpu()
        self.has_amd, self.amd_name = detect_amd_gpu()
        self.has_intel, self.intel_name = detect_intel_gpu()
        self.all_gpus = detect_all_gpus()
        self.available_codecs = get_all_codecs(
            self.has_gpu, self.has_amd, self.has_intel)

        self.queue: list[QueueItem] = []
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()               # not paused
        self.process_control = TranscodeProcessControl(
            self.cancel_event, self.pause_event)
        self.encoding_thread: Optional[threading.Thread] = None
        self._analysis_thread: Optional[threading.Thread] = None
        self._closing = False
        self._planned_output_paths: dict[int, str] = {}
        self._progress_data: dict = {}
        self._item_progress: dict[int, float] = {}  # per-item progress %
        self._is_encoding = False
        self.output_dir: str = default_output_directory()
        self._tray_icon = None
        self._watch_active = False
        self._watch_thread: Optional[threading.Thread] = None
        self._watch_dir: Optional[str] = None
        self._watch_seen: set[str] = set()
        self._watch_sizes: dict[str, int] = {}

        # Custom filter / advanced args state
        self._custom_filters: list[str] = []
        self._advanced_args: list[str] = []
        self._status_polling = False

        # ---- Build UI ------------------------------------------------
        self._build_ui()
        self._load_saved_settings()
        self._restore_geometry()
        self._show_startup_info()
        self._setup_dnd()
        self._bind_shortcuts()
        self._load_queue_from_disk()

    # ==================================================================
    #  UI BUILD
    # ==================================================================

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)   # tabview row stretches

        # -- Row 0: Header ---------------------------------------------
        hdr = ctk.CTkFrame(self, corner_radius=8)
        hdr.grid(row=0, column=0, padx=10, pady=(10, 3), sticky="ew")

        ctk.CTkLabel(
            hdr, text="Video Transcoder",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(side="left", padx=14, pady=8)

        self.theme_var = ctk.StringVar(value="dark")
        ctk.CTkSwitch(
            hdr, text="Light", variable=self.theme_var,
            onvalue="light", offvalue="dark",
            command=self._toggle_theme, width=40,
        ).pack(side="right", padx=14, pady=8)

        gpu_any = self.has_gpu or self.has_amd or self.has_intel
        gpu_clr = "#4ec959" if gpu_any else "#e8a838"
        gpu_parts = []
        if self.has_gpu:
            gpu_parts.append(f"NVIDIA: {self.gpu_name}")
        if self.has_amd:
            gpu_parts.append(f"AMD: {self.amd_name}")
        if self.has_intel:
            gpu_parts.append(f"Intel: {self.intel_name}")
        gpu_text = ", ".join(gpu_parts) if gpu_parts else "CPU only"
        ctk.CTkLabel(
            hdr, text=f"GPU: {gpu_text}",
            font=ctk.CTkFont(size=13), text_color=gpu_clr,
        ).pack(side="right", padx=14, pady=8)

        # -- Row 1: File / output paths --------------------------------
        fbox = ctk.CTkFrame(self, corner_radius=8)
        fbox.grid(row=1, column=0, padx=10, pady=3, sticky="ew")
        fbox.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(fbox, text="Files:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(12, 6), pady=6, sticky="w")
        self.file_label = ctk.CTkLabel(
            fbox, text="Drag files here or click Browse",
            anchor="w", text_color="gray")
        self.file_label.grid(row=0, column=1, padx=4, pady=6, sticky="ew")

        bbx = ctk.CTkFrame(fbox, fg_color="transparent")
        bbx.grid(row=0, column=2, padx=(4, 10), pady=6)
        ctk.CTkButton(bbx, text="Browse Files", width=100,
                       command=self._browse_files).pack(side="left", padx=2)
        ctk.CTkButton(bbx, text="Browse Folder", width=110,
                       command=self._browse_folder).pack(side="left", padx=2)
        self.recursive_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bbx, text="Recursive", variable=self.recursive_var,
                         width=20).pack(side="left", padx=(8, 2))
        self.watch_btn = ctk.CTkButton(
            bbx, text="Watch Folder", width=110,
            fg_color="#555", hover_color="#444",
            command=self._toggle_watch_folder)
        self.watch_btn.pack(side="left", padx=2)

        ctk.CTkLabel(fbox, text="Output:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=(12, 6), pady=(0, 6), sticky="w")
        self.output_label = ctk.CTkLabel(
            fbox, text=self.output_dir, anchor="w", text_color="#88aacc")
        self.output_label.grid(row=1, column=1, padx=4, pady=(0, 6), sticky="ew")

        obx = ctk.CTkFrame(fbox, fg_color="transparent")
        obx.grid(row=1, column=2, padx=(4, 10), pady=(0, 6))
        ctk.CTkButton(obx, text="Change", width=80,
                       command=self._change_output_dir).pack(side="left", padx=2)
        ctk.CTkButton(obx, text="Open Folder", width=90,
                       command=self._open_output_folder).pack(side="left", padx=2)

        # -- Row 2: Presets --------------------------------------------
        pf = ctk.CTkFrame(self, corner_radius=8)
        pf.grid(row=2, column=0, padx=10, pady=3, sticky="ew")
        ctk.CTkLabel(pf, text="Preset:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(12, 6), pady=6)
        self.preset_buttons: list[ctk.CTkButton] = []
        for key, preset in PRESETS.items():
            b = ctk.CTkButton(
                pf, text=preset["name"], width=110, height=28,
                fg_color="transparent", border_width=1, border_color="#555",
                hover_color="#333",
                command=lambda k=key: self._apply_preset(k))
            b.pack(side="left", padx=2, pady=6)
            self.preset_buttons.append(b)

        # -- Row 3: Settings grid --------------------------------------
        sf = ctk.CTkFrame(self, corner_radius=8)
        sf.grid(row=3, column=0, padx=10, pady=3, sticky="ew")
        for c in range(5):
            sf.grid_columnconfigure(c, weight=1)

        # ---- Settings row 0: Codec / Quality / Res / FPS / Audio Codec
        ctk.CTkLabel(sf, text="Codec").grid(
            row=0, column=0, padx=8, pady=(8, 1), sticky="w")
        codec_names = [c.name for c in self.available_codecs]
        self.codec_var = ctk.StringVar(
            value=codec_names[0] if codec_names else "")
        self.codec_menu = ctk.CTkOptionMenu(
            sf, variable=self.codec_var, values=codec_names, width=180,
            command=lambda _: self._update_estimate())
        self.codec_menu.grid(row=1, column=0, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Quality").grid(
            row=0, column=1, padx=8, pady=(8, 1), sticky="w")
        self.quality_var = ctk.StringVar(value="Medium")
        ctk.CTkOptionMenu(
            sf, variable=self.quality_var,
            values=["High", "Medium", "Low"], width=110,
            command=lambda _: self._update_estimate(),
        ).grid(row=1, column=1, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Resolution").grid(
            row=0, column=2, padx=8, pady=(8, 1), sticky="w")
        self.resolution_var = ctk.StringVar(value="Original")
        ctk.CTkOptionMenu(
            sf, variable=self.resolution_var,
            values=["Original", "1080p", "720p", "480p"], width=110,
            command=lambda _: self._update_estimate(),
        ).grid(row=1, column=2, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Frame Rate").grid(
            row=0, column=3, padx=8, pady=(8, 1), sticky="w")
        self.fps_var = ctk.StringVar(value="Original")
        ctk.CTkOptionMenu(
            sf, variable=self.fps_var,
            values=["Original", "60 fps", "30 fps", "24 fps"], width=110,
        ).grid(row=1, column=3, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Audio Codec").grid(
            row=0, column=4, padx=8, pady=(8, 1), sticky="w")
        self.audio_codec_var = ctk.StringVar(value="AAC")
        ctk.CTkOptionMenu(
            sf, variable=self.audio_codec_var,
            values=["AAC", "Opus", "Copy"], width=100,
        ).grid(row=1, column=4, padx=8, pady=(0, 6), sticky="ew")

        # ---- Settings row 2: Bitrate / Format / Subs / Originals / GPU
        ctk.CTkLabel(sf, text="Audio Bitrate").grid(
            row=2, column=0, padx=8, pady=(4, 1), sticky="w")
        self.audio_var = ctk.StringVar(value="128k")
        ctk.CTkOptionMenu(
            sf, variable=self.audio_var,
            values=["192k", "128k", "96k", "64k"], width=110,
            command=lambda _: self._update_estimate(),
        ).grid(row=3, column=0, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Format").grid(
            row=2, column=1, padx=8, pady=(4, 1), sticky="w")
        self.format_var = ctk.StringVar(value="MP4")
        ctk.CTkOptionMenu(
            sf, variable=self.format_var,
            values=["MP4", "MKV", "MOV"], width=100,
        ).grid(row=3, column=1, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Subtitles").grid(
            row=2, column=2, padx=8, pady=(4, 1), sticky="w")
        self.subtitle_var = ctk.StringVar(value="Keep")
        ctk.CTkOptionMenu(
            sf, variable=self.subtitle_var,
            values=["Keep", "Burn In", "Strip"], width=100,
        ).grid(row=3, column=2, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Originals").grid(
            row=2, column=3, padx=8, pady=(4, 1), sticky="w")
        self.delete_var = ctk.StringVar(value="Keep")
        ctk.CTkOptionMenu(
            sf, variable=self.delete_var,
            values=["Keep", "Delete", "Ask Each"], width=110,
        ).grid(row=3, column=3, padx=8, pady=(0, 6), sticky="ew")

        # GPU dropdown (visible only with 2+ GPUs; otherwise a hidden var)
        self.gpu_var = ctk.StringVar(value="Auto")
        if len(self.all_gpus) > 1:
            ctk.CTkLabel(sf, text="GPU").grid(
                row=2, column=4, padx=8, pady=(4, 1), sticky="w")
            gpu_choices = (
                ["Auto"] +
                [f"GPU {g['index']}: {g['name']}" for g in self.all_gpus]
            )
            ctk.CTkOptionMenu(
                sf, variable=self.gpu_var,
                values=gpu_choices, width=150,
            ).grid(row=3, column=4, padx=8, pady=(0, 6), sticky="ew")
        else:
            # Even if single GPU, add post-action in column 4
            ctk.CTkLabel(sf, text="Post-Action").grid(
                row=2, column=4, padx=8, pady=(4, 1), sticky="w")
            self._post_action_menu_single = True

        # ---- Settings row 4: Filename / Post-action / Concurrent
        ctk.CTkLabel(sf, text="Filename").grid(
            row=4, column=0, padx=8, pady=(4, 1), sticky="w")
        self.template_var = ctk.StringVar(value="{name}")
        ctk.CTkOptionMenu(
            sf, variable=self.template_var,
            values=FILENAME_TEMPLATES, width=200,
        ).grid(row=5, column=0, columnspan=2, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Post-Action").grid(
            row=4, column=2, padx=8, pady=(4, 1), sticky="w")
        self.post_action_var = ctk.StringVar(value="None")
        ctk.CTkOptionMenu(
            sf, variable=self.post_action_var,
            values=["None", "Shutdown", "Sleep", "Command"], width=110,
            command=self._on_post_action_change,
        ).grid(row=5, column=2, padx=8, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(sf, text="Concurrent").grid(
            row=4, column=3, padx=8, pady=(4, 1), sticky="w")
        self.concurrent_var = ctk.StringVar(value="1")
        ctk.CTkOptionMenu(
            sf, variable=self.concurrent_var,
            values=["1", "2", "3", "4"], width=70,
        ).grid(row=5, column=3, padx=8, pady=(0, 6), sticky="ew")

        # Post-action custom command entry (hidden by default)
        self.post_cmd_var = ctk.StringVar(value="")
        self.post_cmd_entry = ctk.CTkEntry(
            sf, textvariable=self.post_cmd_var, width=180,
            placeholder_text="Custom command...")
        # Initially hidden; shown when post_action == "Command"

        # ---- Settings row 6: Trim controls
        ctk.CTkLabel(sf, text="Trim Start (s)").grid(
            row=6, column=0, padx=8, pady=(4, 1), sticky="w")
        self.trim_start_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            sf, textvariable=self.trim_start_var, width=80,
            placeholder_text="0.0",
        ).grid(row=7, column=0, padx=8, pady=(0, 6), sticky="w")

        ctk.CTkLabel(sf, text="Trim End (s)").grid(
            row=6, column=1, padx=8, pady=(4, 1), sticky="w")
        self.trim_end_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            sf, textvariable=self.trim_end_var, width=80,
            placeholder_text="end",
        ).grid(row=7, column=1, padx=8, pady=(0, 6), sticky="w")

        # ---- Settings row 6 cont: Custom preset buttons
        cpf = ctk.CTkFrame(sf, fg_color="transparent")
        cpf.grid(row=7, column=2, columnspan=3, padx=8, pady=(0, 6), sticky="e")
        ctk.CTkButton(cpf, text="Save Preset", width=100, height=26,
                       command=self._save_custom_preset).pack(
                           side="left", padx=2)
        ctk.CTkButton(cpf, text="Load Preset", width=100, height=26,
                       command=self._load_custom_preset).pack(
                           side="left", padx=2)
        ctk.CTkButton(cpf, text="Delete Preset", width=100, height=26,
                       fg_color="#b03030", hover_color="#8a2020",
                       command=self._delete_custom_preset).pack(
                           side="left", padx=2)

        # ---- Settings row 8: checkboxes
        cbox = ctk.CTkFrame(sf, fg_color="transparent")
        cbox.grid(row=8, column=0, columnspan=5, padx=8, pady=(0, 6), sticky="w")

        self.skip_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(cbox, text="Skip existing",
                         variable=self.skip_var).pack(side="left", padx=(0, 16))

        self.hwaccel_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox, text="GPU decode (hwaccel cuda)",
                         variable=self.hwaccel_var).pack(side="left", padx=(0, 16))

        self.preview_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox, text="Preview (first 60 s)",
                         variable=self.preview_var,
                         command=self._update_estimate).pack(side="left", padx=(0, 16))

        self.ten_bit_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox, text="10-bit",
                         variable=self.ten_bit_var).pack(side="left", padx=(0, 16))

        self.two_pass_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox, text="2-pass (CPU)",
                         variable=self.two_pass_var).pack(side="left", padx=(0, 16))

        self.auto_crop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox, text="Auto-crop",
                         variable=self.auto_crop_var).pack(side="left", padx=(0, 16))

        # ---- Settings row 9: checkboxes row 2 (audio extract + notifications)
        cbox2 = ctk.CTkFrame(sf, fg_color="transparent")
        cbox2.grid(row=9, column=0, columnspan=5, padx=8, pady=(0, 6), sticky="w")

        self.audio_extract_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(cbox2, text="Audio extract only",
                         variable=self.audio_extract_var,
                         command=self._on_audio_extract_toggle).pack(
                             side="left", padx=(0, 8))

        self.audio_fmt_var = ctk.StringVar(value="MP3")
        self.audio_fmt_menu = ctk.CTkOptionMenu(
            cbox2, variable=self.audio_fmt_var,
            values=["MP3", "AAC", "FLAC", "Opus"], width=80)
        self.audio_fmt_menu.pack(side="left", padx=(0, 16))

        self.notif_sound_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(cbox2, text="Sound notify",
                         variable=self.notif_sound_var).pack(side="left", padx=(0, 16))

        self.notif_toast_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(cbox2, text="Toast notify",
                         variable=self.notif_toast_var).pack(side="left", padx=(0, 16))

        # ---- Row 3c: HDR, Bitrate mode, Filters, Advanced -----------
        cbox3 = ctk.CTkFrame(sf, fg_color="transparent")
        cbox3.grid(row=10, column=0, columnspan=5, padx=8, pady=(0, 6), sticky="w")

        ctk.CTkLabel(cbox3, text="HDR:").pack(side="left", padx=(0, 4))
        self.hdr_var = ctk.StringVar(value="Auto")
        ctk.CTkOptionMenu(cbox3, variable=self.hdr_var, width=110,
                          values=["Auto", "Passthrough", "Tonemap", "Off"]
                          ).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(cbox3, text="Bitrate:").pack(side="left", padx=(0, 4))
        self.bitrate_mode_var = ctk.StringVar(value="CRF")
        self.bitrate_mode_menu = ctk.CTkOptionMenu(
            cbox3, variable=self.bitrate_mode_var, width=100,
            values=["CRF", "CBR", "VBR", "File Size"],
            command=self._on_bitrate_mode_change)
        self.bitrate_mode_menu.pack(side="left", padx=(0, 8))

        self.target_bitrate_var = ctk.StringVar(value="")
        self.target_bitrate_entry = ctk.CTkEntry(
            cbox3, textvariable=self.target_bitrate_var,
            width=80, placeholder_text="6000k")
        self.target_bitrate_entry.pack(side="left", padx=(0, 4))
        self.target_bitrate_entry.configure(state="disabled")

        self.max_bitrate_var = ctk.StringVar(value="")
        self.max_bitrate_entry = ctk.CTkEntry(
            cbox3, textvariable=self.max_bitrate_var,
            width=80, placeholder_text="max")
        self.max_bitrate_entry.pack(side="left", padx=(0, 4))
        self.max_bitrate_entry.configure(state="disabled")

        self.target_size_var = ctk.StringVar(value="")
        self.target_size_entry = ctk.CTkEntry(
            cbox3, textvariable=self.target_size_var,
            width=80, placeholder_text="MB")
        self.target_size_entry.pack(side="left", padx=(0, 4))
        self.target_size_entry.configure(state="disabled")

        ctk.CTkButton(
            cbox3, text="Filters", width=60,
            command=self._open_filter_dialog).pack(side="left", padx=(8, 4))

        ctk.CTkButton(
            cbox3, text="Advanced", width=70,
            command=self._open_advanced_dialog).pack(side="left", padx=(0, 4))

        self.vmaf_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            cbox3,
            text="VMAF",
            variable=self.vmaf_var,
            width=64,
        ).pack(side="left", padx=(6, 0))

        cbox4 = ctk.CTkFrame(sf, fg_color="transparent")
        cbox4.grid(
            row=11, column=0, columnspan=5,
            padx=8, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            cbox4, text="Copy validated output to:"
        ).pack(side="left", padx=(0, 4))
        self.post_copy_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            cbox4,
            textvariable=self.post_copy_var,
            width=360,
            placeholder_text="Optional local, UNC, or network folder",
        ).pack(side="left", padx=(0, 4), fill="x", expand=True)
        ctk.CTkButton(
            cbox4,
            text="Browse",
            width=70,
            command=self._browse_post_copy,
        ).pack(side="left")

        # ---- Apply tooltips to all settings widgets
        self._apply_tooltips(sf, cbox, cbox2)

        # -- Row 4: Estimate label -------------------------------------
        self.est_frame = ctk.CTkFrame(self, corner_radius=8, height=28)
        self.est_frame.grid(row=4, column=0, padx=10, pady=3, sticky="ew")
        self.est_label = ctk.CTkLabel(
            self.est_frame, text="", font=ctk.CTkFont(size=12),
            text_color="#88aacc")
        self.est_label.pack(padx=12, pady=4, anchor="w")

        # -- Row 5: Tabview (Queue / Log / History) --------------------
        self.tabview = ctk.CTkTabview(self, corner_radius=8)
        self.tabview.grid(row=5, column=0, padx=10, pady=3, sticky="nsew")

        # ---- QUEUE tab ----
        tq = self.tabview.add("Queue")
        tq.grid_rowconfigure(1, weight=1)
        tq.grid_columnconfigure(0, weight=1)

        q_hdr = ctk.CTkFrame(tq, fg_color="transparent")
        q_hdr.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        q_hdr.grid_columnconfigure(0, weight=1)

        self.queue_summary = ctk.CTkLabel(
            q_hdr, text="0 files in queue", anchor="w",
            font=ctk.CTkFont(size=12))
        self.queue_summary.grid(row=0, column=0, sticky="w", padx=4)

        qb = ctk.CTkFrame(q_hdr, fg_color="transparent")
        qb.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(qb, text="Move Up", width=80, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._move_queue_up).pack(side="left", padx=2)
        ctk.CTkButton(qb, text="Move Down", width=80, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._move_queue_down).pack(side="left", padx=2)
        ctk.CTkButton(qb, text="Compare", width=80, height=26,
                       fg_color="#3b6ea5", hover_color="#2d5680",
                       command=self._compare_profiles).pack(side="left", padx=2)
        ctk.CTkButton(qb, text="Override", width=75, height=26,
                       fg_color="#6a5a32", hover_color="#554725",
                       command=self._edit_selected_override).pack(
                           side="left", padx=2)
        ctk.CTkButton(qb, text="Clear Override", width=105, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._clear_selected_overrides).pack(
                           side="left", padx=2)
        ctk.CTkButton(qb, text="Remove Selected", width=120, height=26,
                       command=self._remove_selected).pack(side="left", padx=2)
        ctk.CTkButton(qb, text="Clear All", width=80, height=26,
                       fg_color="#b03030", hover_color="#8a2020",
                       command=self._clear_queue).pack(side="left", padx=2)

        # Second row of queue buttons
        qb2 = ctk.CTkFrame(q_hdr, fg_color="transparent")
        qb2.grid(row=1, column=0, columnspan=2, sticky="e", pady=(2, 0))
        ctk.CTkButton(qb2, text="Export Queue", width=100, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._export_queue_dialog).pack(side="left", padx=2)
        ctk.CTkButton(qb2, text="Import Queue", width=100, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._import_queue_dialog).pack(side="left", padx=2)
        ctk.CTkButton(qb2, text="Extract Subs", width=100, height=26,
                       fg_color="#5a4080", hover_color="#4a3070",
                       command=self._extract_subtitles).pack(side="left", padx=2)
        ctk.CTkButton(qb2, text="Analyze Scenes", width=110, height=26,
                       fg_color="#356b63", hover_color="#28524c",
                       command=self._analyze_scenes).pack(side="left", padx=2)
        ctk.CTkButton(qb2, text="Score VMAF", width=95, height=26,
                       fg_color="#3b6ea5", hover_color="#2d5680",
                       command=self._score_selected_vmaf).pack(side="left", padx=2)

        # Scrollable queue list
        self.queue_scroll = ctk.CTkScrollableFrame(tq, corner_radius=4)
        self.queue_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.queue_scroll.grid_columnconfigure(1, weight=1)

        # Column headers
        col_hdr = ctk.CTkFrame(self.queue_scroll, fg_color="transparent")
        col_hdr.pack(fill="x", padx=2, pady=(0, 2))
        col_hdr.grid_columnconfigure(1, weight=1)
        for ci, (txt, w) in enumerate(
                [("", 28), ("File", 0), ("Info", 160), ("Size", 70),
                 ("Duration", 75), ("Est.", 70), ("Status", 110)]):
            lbl = ctk.CTkLabel(col_hdr, text=txt,
                                font=ctk.CTkFont(size=11, weight="bold"),
                                width=w if w else 0, anchor="w")
            lbl.grid(row=0, column=ci, sticky="ew" if ci == 1 else "w", padx=2)

        self._q_widgets: list[dict] = []
        self._q_vars: list[ctk.BooleanVar] = []

        # Thumbnail preview area (right side, if PIL available)
        self._thumb_label: Optional[ctk.CTkLabel] = None

        # ---- LOG tab ----
        tl = self.tabview.add("Log")
        tl.grid_rowconfigure(0, weight=1)
        tl.grid_columnconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            tl, wrap="word",
            font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=0, column=0, padx=4, pady=4, sticky="nsew")
        self.log_box.configure(state="disabled")

        log_btn_frame = ctk.CTkFrame(tl, fg_color="transparent")
        log_btn_frame.grid(row=1, column=0, sticky="e", padx=4, pady=(0, 4))
        ctk.CTkButton(log_btn_frame, text="Export Log", width=90, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._export_log).pack(side="left", padx=2)
        ctk.CTkButton(log_btn_frame, text="Clear Log", width=90, height=26,
                       fg_color="#555", hover_color="#444",
                       command=self._clear_log).pack(side="left", padx=2)

        # ---- HISTORY tab ----
        th = self.tabview.add("History")
        th.grid_rowconfigure(0, weight=1)
        th.grid_columnconfigure(0, weight=1)
        self.hist_box = ctk.CTkTextbox(
            th, wrap="word",
            font=ctk.CTkFont(family="Consolas", size=12))
        self.hist_box.grid(row=0, column=0, padx=4, pady=4, sticky="nsew")
        self.hist_box.configure(state="disabled")

        # -- Row 6: Progress + controls --------------------------------
        bot = ctk.CTkFrame(self, corner_radius=8)
        bot.grid(row=6, column=0, padx=10, pady=(3, 10), sticky="ew")
        bot.grid_columnconfigure(1, weight=1)

        self.prog_label = ctk.CTkLabel(
            bot, text="Ready", font=ctk.CTkFont(size=12))
        self.prog_label.grid(row=0, column=0, padx=(12, 6), pady=(8, 2), sticky="w")

        self.prog_bar = ctk.CTkProgressBar(bot, height=16)
        self.prog_bar.grid(row=0, column=1, padx=4, pady=(8, 2), sticky="ew")
        self.prog_bar.set(0)

        self.pct_label = ctk.CTkLabel(
            bot, text="0 %", width=50, font=ctk.CTkFont(size=12))
        self.pct_label.grid(row=0, column=2, padx=(4, 10), pady=(8, 2))

        self.stats_label = ctk.CTkLabel(
            bot, text="", font=ctk.CTkFont(size=11), text_color="gray")
        self.stats_label.grid(
            row=1, column=0, columnspan=2, padx=12, pady=(0, 6), sticky="w")

        bf = ctk.CTkFrame(bot, fg_color="transparent")
        bf.grid(row=1, column=2, padx=10, pady=(0, 6), sticky="e")

        self.start_btn = ctk.CTkButton(
            bf, text="Start Encoding", width=130, height=34,
            fg_color="#2d8a4e", hover_color="#236b3c",
            command=self._start_encoding)
        self.start_btn.pack(side="left", padx=2)

        self.pause_btn = ctk.CTkButton(
            bf, text="Pause", width=70, height=34,
            fg_color="#b08620", hover_color="#8a6a18",
            state="disabled", command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=2)

        self.cancel_btn = ctk.CTkButton(
            bf, text="Cancel", width=70, height=34,
            fg_color="#b03030", hover_color="#8a2020",
            state="disabled", command=self._cancel_encoding)
        self.cancel_btn.pack(side="left", padx=2)

        if _HAS_TRAY:
            ctk.CTkButton(
                bf, text="To Tray", width=70, height=34,
                fg_color="#555", hover_color="#444",
                command=self._minimize_to_tray,
            ).pack(side="left", padx=2)

        # -- Row 7: Status bar -----------------------------------------
        self.status_frame = ctk.CTkFrame(self, corner_radius=4, height=24)
        self.status_frame.grid(row=7, column=0, padx=10, pady=(0, 5), sticky="ew")
        self.status_label = ctk.CTkLabel(
            self.status_frame, text="",
            font=ctk.CTkFont(size=10), text_color="gray")
        self.status_label.pack(padx=8, pady=2, side="left")
        self.time_est_label = ctk.CTkLabel(
            self.status_frame, text="",
            font=ctk.CTkFont(size=10), text_color="#88aacc")
        self.time_est_label.pack(padx=8, pady=2, side="right")

    # ==================================================================
    #  DND
    # ==================================================================

    def _setup_dnd(self):
        if not _HAS_DND:
            return
        try:
            tkinterdnd2.TkinterDnD.require(self)
            self.drop_target_register(tkinterdnd2.DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        raw = event.data
        files: list[str] = []
        if "{" in raw:
            files = re.findall(r"\{([^}]+)\}", raw)
            rest = re.sub(r"\{[^}]+\}", "", raw).strip()
            if rest:
                files += rest.split()
        else:
            files = raw.split()
        vids = [f for f in files
                if Path(f).suffix.lower() in VIDEO_EXTENSIONS and os.path.isfile(f)]
        if vids:
            self._add_to_queue(vids)

    # ==================================================================
    #  THEME
    # ==================================================================

    def _toggle_theme(self):
        ctk.set_appearance_mode(self.theme_var.get())

    # ==================================================================
    #  FILE / FOLDER BROWSE
    # ==================================================================

    def _browse_files(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        files = filedialog.askopenfilenames(
            title="Select Video Files",
            filetypes=[("Video Files", exts), ("All Files", "*.*")])
        if files:
            self._add_to_queue(list(files))

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Select Folder with Videos")
        if not folder:
            return
        root = Path(folder)
        it = root.rglob("*") if self.recursive_var.get() else root.iterdir()
        vids = sorted(
            str(f) for f in it
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
        if vids:
            self._add_to_queue(vids)
        else:
            messagebox.showinfo("No Videos",
                                "No video files found in the selected folder.")

    def _change_output_dir(self):
        d = filedialog.askdirectory(
            title="Select Output Folder",
            initialdir=os.path.abspath(self.output_dir),
        )
        if d:
            self.output_dir = d
            self.output_label.configure(text=d)

    def _browse_post_copy(self):
        directory = filedialog.askdirectory(
            title="Copy Validated Outputs To")
        if directory:
            self.post_copy_var.set(directory)

    def _open_output_folder(self):
        p = os.path.abspath(self.output_dir)
        os.makedirs(p, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", p])

    # ==================================================================
    #  QUEUE
    # ==================================================================

    def _add_to_queue(self, files: list[str]):
        existing = {q.path for q in self.queue}
        n = 0
        for f in files:
            if f in existing:
                continue
            qi = QueueItem(path=f)
            qi.size_mb = get_file_size_mb(f)
            qi.duration = get_duration(f)
            self.queue.append(qi)
            existing.add(f)
            n += 1
        self._refresh_queue()
        # Probe metadata in background
        threading.Thread(target=self._probe_queue_metadata,
                         daemon=True).start()
        self._update_estimate()
        if n:
            self._log(f"Added {n} file(s) to queue.")
            self._save_queue_to_disk()

    def _remove_selected(self):
        if self._is_encoding:
            messagebox.showwarning("Busy",
                                   "Cannot modify queue while encoding.")
            return
        rm = {i for i, v in enumerate(self._q_vars) if v.get()}
        if not rm:
            return
        self.queue = [q for i, q in enumerate(self.queue) if i not in rm]
        self._refresh_queue()
        self._update_estimate()
        self._save_queue_to_disk()

    def _clear_queue(self):
        if self._is_encoding:
            messagebox.showwarning("Busy",
                                   "Cannot modify queue while encoding.")
            return
        self.queue.clear()
        self._refresh_queue()
        self._update_estimate()
        self._save_queue_to_disk()

    def _effective_settings(
        self,
        item: QueueItem,
        base_settings: Settings,
    ) -> Settings:
        return apply_settings_override(
            base_settings,
            item.settings_override,
            self.available_codecs,
        )

    def _edit_selected_override(self):
        """Edit sparse settings inherited by one selected queue item."""
        if self._is_encoding:
            messagebox.showwarning(
                "Busy", "Overrides cannot be edited while encoding.")
            return
        selected = [
            index for index, value in enumerate(self._q_vars)
            if value.get()
        ]
        if len(selected) != 1:
            messagebox.showinfo(
                "Override Settings", "Select exactly one queue item.")
            return
        base = self._build_settings()
        if base is None:
            return
        item = self.queue[selected[0]]
        current = item.settings_override

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Override — {Path(item.path).name}")
        dialog.geometry("620x650")
        dialog.transient(self)
        dialog.grab_set()

        frame = ctk.CTkScrollableFrame(dialog)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(
            frame,
            text="Only non-inherited values are stored for this file.",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 8))

        variables: dict[str, ctk.StringVar] = {}

        def _option(
            key: str,
            label: str,
            choices: list[tuple[str, object]],
        ):
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=label, width=170, anchor="w"
            ).pack(side="left")
            display_for_value = {
                json.dumps(value, sort_keys=True): display
                for display, value in choices
            }
            if key in current:
                current_key = json.dumps(current[key], sort_keys=True)
                initial = display_for_value.get(current_key, "Inherit")
            else:
                initial = "Inherit"
            variable = ctk.StringVar(value=initial)
            variables[key] = variable
            menu = ctk.CTkOptionMenu(
                row,
                variable=variable,
                values=["Inherit"] + [display for display, _ in choices],
                width=330,
            )
            menu.pack(side="left", fill="x", expand=True)
            menu._override_choices = dict(choices)

        _option(
            "codec_encoder",
            "Codec",
            [(codec.name, codec.encoder) for codec in self.available_codecs],
        )
        _option(
            "quality", "Quality",
            [("High", "high"), ("Medium", "medium"), ("Low", "low")])
        _option(
            "resolution", "Resolution",
            [("Original", None), ("1080p", "1080"),
             ("720p", "720"), ("480p", "480")])
        _option(
            "fps", "Frame rate",
            [("Original", None), ("60 fps", 60),
             ("30 fps", 30), ("24 fps", 24)])
        _option(
            "output_format", "Container",
            [("MP4", "mp4"), ("MKV", "mkv"), ("MOV", "mov")])
        _option(
            "audio_codec", "Audio",
            [("AAC", "aac"), ("Opus", "opus"), ("Copy", "copy")])
        _option(
            "subtitle_mode", "Subtitles",
            [("Keep", "keep"), ("Burn In", "burn"), ("Strip", "strip")])
        _option(
            "bitrate_mode", "Bitrate mode",
            [("CRF/CQ", "crf"), ("CBR", "cbr"),
             ("VBR", "vbr"), ("Target size", "filesize")])
        _option(
            "hdr_mode", "HDR",
            [("Auto", "auto"), ("Passthrough", "passthrough"),
             ("Tonemap", "tonemap"), ("Off", "off")])
        _option(
            "delete_originals", "After success",
            [("Keep source", "no"), ("Delete source", "yes"),
             ("Ask", "ask")])

        entry_variables: dict[str, ctk.StringVar] = {}
        for key, label, placeholder in (
            ("trim_start", "Trim start (seconds)", "inherit"),
            ("trim_end", "Trim end (seconds)", "inherit"),
            ("target_bitrate", "Target bitrate", "e.g. 6000k"),
            ("max_bitrate", "Maximum bitrate", "e.g. 8000k"),
            ("target_size_mb", "Target size (MB)", "inherit"),
            ("filename_template", "Filename template", "inherit"),
        ):
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=label, width=170, anchor="w"
            ).pack(side="left")
            value = current.get(key, "")
            variable = ctk.StringVar(
                value="" if value is None else str(value))
            entry_variables[key] = variable
            ctk.CTkEntry(
                row,
                textvariable=variable,
                placeholder_text=placeholder,
                width=330,
            ).pack(side="left", fill="x", expand=True)

        destination_row = ctk.CTkFrame(frame, fg_color="transparent")
        destination_row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            destination_row, text="Output directory",
            width=170, anchor="w"
        ).pack(side="left")
        destination_var = ctk.StringVar(
            value=item.output_dir_override or "")
        ctk.CTkEntry(
            destination_row,
            textvariable=destination_var,
            placeholder_text="inherit global output directory",
            width=255,
        ).pack(side="left", fill="x", expand=True)

        def _browse_destination():
            selected_dir = filedialog.askdirectory(
                title="Per-file Output Directory")
            if selected_dir:
                destination_var.set(selected_dir)

        ctk.CTkButton(
            destination_row,
            text="Browse",
            width=70,
            command=_browse_destination,
        ).pack(side="left", padx=(4, 0))

        def _save():
            override: dict = {}
            choice_maps = {
                "codec_encoder": {
                    codec.name: codec.encoder
                    for codec in self.available_codecs
                },
                "quality": {"High": "high", "Medium": "medium", "Low": "low"},
                "resolution": {
                    "Original": None, "1080p": "1080",
                    "720p": "720", "480p": "480"},
                "fps": {
                    "Original": None, "60 fps": 60,
                    "30 fps": 30, "24 fps": 24},
                "output_format": {"MP4": "mp4", "MKV": "mkv", "MOV": "mov"},
                "audio_codec": {
                    "AAC": "aac", "Opus": "opus", "Copy": "copy"},
                "subtitle_mode": {
                    "Keep": "keep", "Burn In": "burn", "Strip": "strip"},
                "bitrate_mode": {
                    "CRF/CQ": "crf", "CBR": "cbr",
                    "VBR": "vbr", "Target size": "filesize"},
                "hdr_mode": {
                    "Auto": "auto", "Passthrough": "passthrough",
                    "Tonemap": "tonemap", "Off": "off"},
                "delete_originals": {
                    "Keep source": "no", "Delete source": "yes", "Ask": "ask"},
            }
            for key, variable in variables.items():
                display = variable.get()
                if display != "Inherit":
                    override[key] = choice_maps[key][display]

            try:
                for key, variable in entry_variables.items():
                    raw = variable.get().strip()
                    if not raw:
                        continue
                    if key in ("trim_start", "trim_end", "target_size_mb"):
                        override[key] = float(raw)
                    else:
                        override[key] = raw
                apply_settings_override(
                    base, override, self.available_codecs)
            except (ValueError, KeyError) as exc:
                messagebox.showerror(
                    "Override Settings", str(exc), parent=dialog)
                return

            item.settings_override = override
            item.output_dir_override = (
                destination_var.get().strip() or None)
            self._refresh_queue()
            self._update_estimate()
            self._save_queue_to_disk()
            dialog.destroy()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            buttons, text="Save Override", command=_save
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="Cancel", fg_color="#555",
            command=dialog.destroy
        ).pack(side="right", padx=8)

    def _clear_selected_overrides(self):
        if self._is_encoding:
            return
        selected = [
            index for index, value in enumerate(self._q_vars)
            if value.get()
        ]
        for index in selected:
            self.queue[index].settings_override.clear()
            self.queue[index].output_dir_override = None
        if selected:
            self._refresh_queue()
            self._update_estimate()
            self._save_queue_to_disk()

    # ---- queue display ----

    def _refresh_queue(self):
        """Rebuild queue list widgets from ``self.queue``."""
        for w in self._q_widgets:
            w["frame"].destroy()
        self._q_widgets.clear()
        self._q_vars.clear()

        for i, item in enumerate(self.queue):
            row = ctk.CTkFrame(self.queue_scroll, fg_color="transparent")
            row.pack(fill="x", padx=2, pady=1)
            row.grid_columnconfigure(1, weight=1)

            var = ctk.BooleanVar(value=False)
            self._q_vars.append(var)
            chk = ctk.CTkCheckBox(row, text="", variable=var,
                                   width=24, checkbox_width=18,
                                   checkbox_height=18)
            chk.grid(row=0, column=0, padx=2, sticky="w")

            clr = {
                "queued": "gray", "encoding": "#3b8ed0",
                "done": "#4ec959", "failed": "#e05050",
                "skipped": "#e8a838", "cancelled": "#888",
            }.get(item.status, "gray")

            display_name = Path(item.path).name
            if item.settings_override or item.output_dir_override:
                display_name += "  [Custom]"
            nm = ctk.CTkLabel(row, text=display_name, anchor="w",
                               font=ctk.CTkFont(size=12))
            nm.grid(row=0, column=1, padx=2, sticky="ew")

            # Clicking the name shows a thumbnail
            nm.bind("<Button-1>",
                    lambda e, p=item.path: self._show_thumbnail(p))

            # Right-click shows metadata info
            nm.bind("<Button-3>",
                    lambda e, idx=i: self._show_metadata_popup(e, idx))

            # Metadata summary (compact)
            meta_txt = ""
            if item.metadata:
                m = item.metadata
                parts = []
                if m.get("video_res"):
                    parts.append(m["video_res"])
                if m.get("video_codec"):
                    parts.append(m["video_codec"])
                if m.get("video_bitrate"):
                    parts.append(m["video_bitrate"])
                meta_txt = " | ".join(parts) if parts else ""

            mt = ctk.CTkLabel(row, text=meta_txt, width=160,
                               font=ctk.CTkFont(size=10),
                               text_color="#999")
            mt.grid(row=0, column=2, padx=2)

            sz = ctk.CTkLabel(row, text=format_size(item.size_mb), width=70,
                               font=ctk.CTkFont(size=11), text_color="gray")
            sz.grid(row=0, column=3, padx=2)

            dr = ctk.CTkLabel(row, text=format_duration(item.duration),
                               width=75, font=ctk.CTkFont(size=11),
                               text_color="gray")
            dr.grid(row=0, column=4, padx=2)

            est_txt = format_size(item.est_mb) if item.est_mb > 0 else "--"
            es = ctk.CTkLabel(row, text=est_txt, width=70,
                               font=ctk.CTkFont(size=11),
                               text_color="#88aacc")
            es.grid(row=0, column=5, padx=2)

            if item.status == "done" and item.result and item.result.success:
                out_mb = item.result.output_size / (1024 * 1024)
                pct_saved = (
                    (item.result.input_size - item.result.output_size)
                    / item.result.input_size * 100
                ) if item.result.input_size > 0 else 0
                stxt = f"Done ({pct_saved:.0f}% saved)"
            else:
                stxt = item.status.capitalize()

            st = ctk.CTkLabel(row, text=stxt, width=110,
                               font=ctk.CTkFont(size=11), text_color=clr)
            st.grid(row=0, column=6, padx=2)

            # Per-file progress bar (shown only for encoding items)
            prog = ctk.CTkProgressBar(row, width=0, height=6,
                                        corner_radius=2)
            if item.status == "encoding":
                pct = self._item_progress.get(i, 0)
                prog.set(pct / 100)
                prog.grid(row=1, column=1, columnspan=5,
                           padx=2, pady=(0, 2), sticky="ew")

            self._q_widgets.append({
                "frame": row, "name": nm, "meta": mt, "size": sz,
                "dur": dr, "est": es, "status": st, "progress": prog,
            })

        cnt = len(self.queue)
        tot = sum(q.size_mb for q in self.queue)
        if cnt:
            self.file_label.configure(
                text=f"{cnt} file{'s' if cnt != 1 else ''} "
                     f"({format_size(tot)})",
                text_color="white")
        else:
            self.file_label.configure(
                text="Drag files here or click Browse",
                text_color="gray")
        self.queue_summary.configure(
            text=f"{cnt} file{'s' if cnt != 1 else ''} in queue")

    def _update_queue_item(self, idx: int, status: str,
                           result: Optional[EncodeResult] = None):
        """Thread-safe single-item status update."""
        if 0 <= idx < len(self.queue):
            self.queue[idx].status = status
            if result:
                self.queue[idx].result = result
        self.after(0, self._refresh_queue)

    # ==================================================================
    #  ESTIMATE
    # ==================================================================

    def _update_estimate(self):
        if not self.queue:
            self.est_label.configure(text="")
            return

        codec_name = self.codec_var.get()
        encoder = ""
        for c in self.available_codecs:
            if c.name == codec_name:
                encoder = c.encoder
                break

        quality = self.quality_var.get().lower()
        res_map = {"Original": None, "1080p": "1080",
                   "720p": "720", "480p": "480"}
        res = res_map.get(self.resolution_var.get())
        abr = self.audio_var.get()
        base = Settings()
        base.codec = next(
            (codec for codec in self.available_codecs
             if codec.encoder == encoder),
            None,
        )
        base.quality = quality
        base.resolution = res
        base.audio_bitrate = abr
        base.output_format = self.format_var.get().lower()
        base.audio_codec = {
            "AAC": "aac", "Opus": "opus", "Copy": "copy"
        }.get(self.audio_codec_var.get(), "aac")

        total_in = 0.0
        total_est = 0.0
        for item in self.queue:
            try:
                effective = self._effective_settings(item, base)
            except ValueError:
                effective = base
            dur = min(item.duration, 60) if self.preview_var.get() else item.duration
            effective_encoder = (
                effective.codec.encoder if effective.codec else encoder)
            e = estimate_output_mb(
                dur,
                effective_encoder,
                effective.quality,
                effective.resolution,
                effective.audio_bitrate,
            )
            item.est_mb = e
            total_est += e
            total_in += item.size_mb

        red = ((total_in - total_est) / total_in * 100) if total_in > 0 else 0
        self.est_label.configure(
            text=f"Estimated output: ~{format_size(total_est)}  "
                 f"(~{red:.0f}% reduction from {format_size(total_in)})  "
                 f"|  Estimates are approximate")
        self._refresh_queue()

    # ==================================================================
    #  PRESETS
    # ==================================================================

    def _apply_preset(self, key: str):
        p = PRESETS[key]
        if self.has_gpu:
            encoder = p["codec_gpu"]
        elif self.has_amd:
            _nvenc_amf = {"hevc_nvenc": "hevc_amf", "h264_nvenc": "h264_amf"}
            encoder = _nvenc_amf.get(p["codec_gpu"], p["codec_cpu"])
        elif self.has_intel:
            _nvenc_qsv = {"hevc_nvenc": "hevc_qsv", "h264_nvenc": "h264_qsv"}
            encoder = _nvenc_qsv.get(p["codec_gpu"], p["codec_cpu"])
        else:
            encoder = p["codec_cpu"]
        codec = find_codec_by_encoder(encoder, self.has_gpu,
                                       self.has_amd, self.has_intel)
        if codec:
            self.codec_var.set(codec.name)
        self.quality_var.set(p["quality"].capitalize())
        r = p["resolution"]
        self.resolution_var.set("Original" if r is None else f"{r}p")
        f = p["fps"]
        self.fps_var.set("Original" if f is None else f"{f} fps")
        self.audio_var.set(p["audio"])
        self.format_var.set("MP4")
        self.subtitle_var.set("Keep")
        self.audio_codec_var.set("AAC")

        self._log(f"Preset: {p['name']} -- {p['desc']}")
        for btn in self.preset_buttons:
            btn.configure(fg_color="transparent", border_color="#555")
        idx = list(PRESETS.keys()).index(key)
        self.preset_buttons[idx].configure(
            fg_color="#1f538d", border_color="#3b8ed0")
        self._update_estimate()

    # ==================================================================
    #  SETTINGS <-> DATACLASS
    # ==================================================================

    def _build_settings(self) -> Optional[Settings]:
        s = Settings()
        name = self.codec_var.get()
        codec = None
        for c in self.available_codecs:
            if c.name == name:
                codec = c
                break
        if codec is None:
            messagebox.showerror("Error", f"Unknown codec: {name}")
            return None

        s.codec = codec
        s.quality = self.quality_var.get().lower()
        s.resolution = {"Original": None, "1080p": "1080",
                        "720p": "720", "480p": "480"}.get(
                            self.resolution_var.get())
        s.fps = {"Original": None, "60 fps": 60,
                 "30 fps": 30, "24 fps": 24}.get(self.fps_var.get())
        s.audio_bitrate = self.audio_var.get()
        s.audio_codec = {"AAC": "aac", "Opus": "opus",
                         "Copy": "copy"}.get(
                             self.audio_codec_var.get(), "aac")
        s.output_format = self.format_var.get().lower()
        s.subtitle_mode = {"Keep": "keep", "Burn In": "burn",
                           "Strip": "strip"}.get(
                               self.subtitle_var.get(), "keep")
        s.skip_existing = self.skip_var.get()
        s.hwaccel = self.hwaccel_var.get()
        s.delete_originals = {"Keep": "no", "Delete": "yes",
                              "Ask Each": "ask"}.get(
                                  self.delete_var.get(), "no")
        s.ten_bit = self.ten_bit_var.get()
        s.two_pass = self.two_pass_var.get()
        s.filename_template = self.template_var.get()
        s.concurrent = int(self.concurrent_var.get())

        # Trim
        try:
            ts = self.trim_start_var.get().strip()
            s.trim_start = float(ts) if ts else None
        except ValueError:
            messagebox.showerror("Error", f"Invalid trim start value: {self.trim_start_var.get()}")
            return None
        try:
            te = self.trim_end_var.get().strip()
            s.trim_end = float(te) if te else None
        except ValueError:
            messagebox.showerror("Error", f"Invalid trim end value: {self.trim_end_var.get()}")
            return None

        # Post-action
        pa = self.post_action_var.get()
        s.post_action = {"None": "none", "Shutdown": "shutdown",
                         "Sleep": "sleep", "Command": "command"}.get(pa, "none")
        s.post_command = self.post_cmd_var.get()

        s.auto_crop = self.auto_crop_var.get()
        s.audio_extract = self.audio_extract_var.get()
        s.audio_extract_format = self.audio_fmt_var.get().lower()
        s.notification_sound = self.notif_sound_var.get()
        s.notification_toast = self.notif_toast_var.get()

        # New feature fields (Phases 5, 7, 11, 12, 15)
        s.hdr_mode = {"Auto": "auto", "Passthrough": "passthrough",
                      "Tonemap": "tonemap", "Off": "off"}.get(
                          self.hdr_var.get(), "auto")

        bm = {"CRF": "crf", "CBR": "cbr", "VBR": "vbr",
              "File Size": "filesize"}.get(self.bitrate_mode_var.get(), "crf")
        s.bitrate_mode = bm
        s.target_bitrate = self.target_bitrate_var.get().strip()
        s.max_bitrate = self.max_bitrate_var.get().strip()
        try:
            sz = self.target_size_var.get().strip()
            s.target_size_mb = float(sz) if sz else 0.0
        except ValueError:
            s.target_size_mb = 0.0

        if hasattr(self, '_custom_filters') and self._custom_filters:
            s.video_filters = [f for f in self._custom_filters if f.strip()]
        if hasattr(self, '_advanced_args') and self._advanced_args:
            s.advanced_args = self._advanced_args[:]
        s.post_copy_dir = self.post_copy_var.get().strip()
        s.vmaf_enabled = self.vmaf_var.get()

        return s

    # ==================================================================
    #  LOAD SAVED SETTINGS
    # ==================================================================

    def _load_saved_settings(self):
        cfg = load_config()
        if not cfg:
            return

        encoder = cfg.get("codec_encoder")
        if encoder:
            codec = find_codec_by_encoder(
                encoder, self.has_gpu,
                has_amd=self.has_amd, has_intel=self.has_intel)
            if codec:
                self.codec_var.set(codec.name)

        q = cfg.get("quality", "").capitalize()
        if q in ("High", "Medium", "Low"):
            self.quality_var.set(q)

        res = cfg.get("resolution")
        self.resolution_var.set(f"{res}p" if res else "Original")

        fps = cfg.get("fps")
        self.fps_var.set(f"{fps} fps" if fps else "Original")

        abr = cfg.get("audio_bitrate")
        if abr in ("192k", "128k", "96k", "64k"):
            self.audio_var.set(abr)

        ac = cfg.get("audio_codec", "aac")
        self.audio_codec_var.set(
            {"aac": "AAC", "opus": "Opus", "copy": "Copy"}.get(ac, "AAC"))

        fmt = cfg.get("output_format", "mp4").upper()
        if fmt in ("MP4", "MKV", "MOV"):
            self.format_var.set(fmt)

        sub = cfg.get("subtitle_mode", "keep")
        self.subtitle_var.set(
            {"keep": "Keep", "burn": "Burn In",
             "strip": "Strip"}.get(sub, "Keep"))

        dm = cfg.get("delete_originals", "no")
        self.delete_var.set(
            {"no": "Keep", "yes": "Delete",
             "ask": "Ask Each"}.get(dm, "Keep"))

        self.skip_var.set(cfg.get("skip_existing", True))
        self.hwaccel_var.set(cfg.get("hwaccel", False))

        self.ten_bit_var.set(cfg.get("ten_bit", False))
        self.two_pass_var.set(cfg.get("two_pass", False))

        tmpl = cfg.get("filename_template", "{name}")
        if tmpl:
            self.template_var.set(tmpl)

        pa = cfg.get("post_action", "none")
        self.post_action_var.set(
            {"none": "None", "shutdown": "Shutdown",
             "sleep": "Sleep", "command": "Command"}.get(pa, "None"))
        self.post_cmd_var.set(cfg.get("post_command", ""))

        conc = cfg.get("concurrent", 1)
        self.concurrent_var.set(str(conc))

        self.auto_crop_var.set(cfg.get("auto_crop", False))
        self.audio_extract_var.set(cfg.get("audio_extract", False))
        afmt = cfg.get("audio_extract_format", "mp3").upper()
        if afmt in ("MP3", "AAC", "FLAC", "OPUS"):
            self.audio_fmt_var.set(afmt)
        self.notif_sound_var.set(cfg.get("notification_sound", True))
        self.notif_toast_var.set(cfg.get("notification_toast", True))

        # Restore new feature settings
        hdr = cfg.get("hdr_mode", "auto")
        self.hdr_var.set({"auto": "Auto", "passthrough": "Passthrough",
                          "tonemap": "Tonemap", "off": "Off"}.get(hdr, "Auto"))
        bm = cfg.get("bitrate_mode", "crf")
        self.bitrate_mode_var.set({"crf": "CRF", "cbr": "CBR", "vbr": "VBR",
                                    "filesize": "File Size"}.get(bm, "CRF"))
        self.target_bitrate_var.set(cfg.get("target_bitrate", ""))
        self.max_bitrate_var.set(cfg.get("max_bitrate", ""))
        tsz = cfg.get("target_size_mb", 0)
        self.target_size_var.set(str(tsz) if tsz else "")
        self.vmaf_var.set(cfg.get("vmaf_enabled", False))
        self.post_copy_var.set(cfg.get("post_copy_dir", ""))
        self._on_bitrate_mode_change(self.bitrate_mode_var.get())

        self._log("Loaded settings from previous session.")

    # ==================================================================
    #  LOGGING
    # ==================================================================

    def _log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_ts(self, text: str):
        """Thread-safe log."""
        self.after(0, self._log, text)

    def _hist(self, text: str):
        def _do():
            self.hist_box.configure(state="normal")
            self.hist_box.insert("end", text + "\n")
            self.hist_box.see("end")
            self.hist_box.configure(state="disabled")
        self.after(0, _do)

    # ==================================================================
    #  ENCODING
    # ==================================================================

    def _start_encoding(self):
        if self._analysis_thread and self._analysis_thread.is_alive():
            messagebox.showwarning(
                "Busy", "Wait for the active analysis to finish.")
            return
        queued = [q for q in self.queue if q.status == "queued"]
        if not queued:
            messagebox.showwarning("No Files",
                                   "Add files to the queue first.")
            return

        settings = self._build_settings()
        if settings is None:
            return

        if not check_ffmpeg():
            messagebox.showerror(
                "FFmpeg Missing",
                "FFmpeg was not found.\n\n"
                "Download: https://www.gyan.dev/ffmpeg/builds/\n"
                "Add the bin/ folder to your PATH.")
            return

        if settings.audio_codec == "opus" and settings.output_format == "mp4":
            messagebox.showwarning(
                "Compatibility",
                "Opus audio is not natively supported in MP4.\n"
                "Switch to MKV or use AAC.")
            return

        # Input validation warnings
        warnings = validate_settings(settings)
        if warnings:
            msg = "Settings warnings:\n\n" + "\n".join(
                f"\u2022 {w}" for w in warnings)
            if not messagebox.askyesno("Warnings",
                                        msg + "\n\nContinue anyway?"):
                return

        preview = self.preview_var.get()
        output_plan: list[tuple[QueueItem, Settings, str]] = []
        override_errors: list[str] = []
        for item in queued:
            try:
                effective = self._effective_settings(item, settings)
                output_path = self._build_output_path(
                    item.path,
                    effective,
                    preview,
                    item.output_dir_override,
                )
            except ValueError as exc:
                override_errors.append(
                    f"{Path(item.path).name}: {exc}")
                continue
            output_plan.append((item, effective, output_path))

        if override_errors:
            messagebox.showerror(
                "Invalid Overrides",
                "Fix these per-file overrides before encoding:\n\n"
                + "\n".join(override_errors),
            )
            return

        source_paths = {
            os.path.normcase(os.path.abspath(item.path)) for item in queued
        }
        planned_destinations: dict[str, list[str]] = {}
        source_conflicts: list[str] = []
        for item, effective, output_path in output_plan:
            primary_key = os.path.normcase(os.path.abspath(output_path))
            planned_destinations.setdefault(primary_key, []).append(
                f"{Path(item.path).name} (primary)")
            if primary_key in source_paths:
                source_conflicts.append(
                    f"{Path(item.path).name}: {output_path}")
            if effective.post_copy_dir:
                copy_path = os.path.join(
                    effective.post_copy_dir, Path(output_path).name)
                copy_key = os.path.normcase(os.path.abspath(copy_path))
                if copy_key != primary_key:
                    planned_destinations.setdefault(copy_key, []).append(
                        f"{Path(item.path).name} (secondary)")
                if copy_key in source_paths:
                    source_conflicts.append(
                        f"{Path(item.path).name}: {copy_path}")
        if source_conflicts:
            messagebox.showerror(
                "Output Conflicts With Input",
                "A primary or secondary destination would overwrite a queued "
                "source file:\n\n" + "\n".join(source_conflicts),
            )
            return

        collisions = [
            names for names in planned_destinations.values() if len(names) > 1
        ]
        if collisions:
            messagebox.showerror(
                "Output Collision",
                "Multiple queue items resolve to the same output path:\n\n"
                + "\n".join(" - " + ", ".join(names) for names in collisions)
                + "\n\nChange a filename template, format, or per-file "
                "destination.",
            )
            return

        would_skip = 0
        for item, effective, output_path in output_plan:
            if (
                effective.skip_existing
                and not preview
                and os.path.isfile(output_path)
            ):
                media_kind = "audio" if effective.audio_extract else "video"
                valid, _message, _duration = validate_output_file(
                    item.path,
                    output_path,
                    effective,
                    preview,
                    media_kind,
                )
                if valid:
                    would_skip += 1

        if would_skip:
            if would_skip == len(queued):
                if not messagebox.askyesno(
                        "All Files Exist",
                        f"All {would_skip} output file(s) already exist "
                        "and passed validation.\n\nThey will all be skipped.\n"
                        "Uncheck 'Skip existing' to re-encode.\n\n"
                        "Continue anyway?"):
                    return
            else:
                if not messagebox.askyesno(
                        "Some Files Exist",
                        f"{would_skip} of {len(queued)} output file(s) "
                        "already exist, passed validation, and will be "
                        "skipped.\n\n"
                        "Continue anyway?"):
                    return

        # UI state
        self._planned_output_paths = {
            id(item): output_path
            for item, _effective, output_path in output_plan
        }
        self._is_encoding = True
        self.process_control.reset()
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause")
        self.cancel_btn.configure(state="normal")
        self.prog_bar.set(0)
        self.pct_label.configure(text="0 %")
        self.prog_label.configure(text="Starting...")
        self.stats_label.configure(text="")

        os.makedirs(self.output_dir, exist_ok=True)

        # GPU index
        gpu_idx = None
        gv = self.gpu_var.get()
        if gv != "Auto" and gv:
            try:
                gpu_idx = gv.split(":")[0].replace("GPU ", "").strip()
            except Exception:
                pass

        preview = self.preview_var.get()

        self.encoding_thread = threading.Thread(
            target=self._worker,
            args=(settings, preview, gpu_idx), daemon=True)
        self.encoding_thread.start()
        self._start_status_polling()
        # Show time estimate
        est = self._estimate_batch_time()
        if est:
            self.time_est_label.configure(text=f"Est. time: ~{est}")
        self._poll()

    def _cancel_encoding(self):
        self.process_control.cancel()
        self.cancel_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self._log("Cancellation requested...")

    def _toggle_pause(self):
        if self.pause_event.is_set():
            self.process_control.pause()
            self.pause_btn.configure(
                text="Resume", fg_color="#2d8a4e",
                hover_color="#236b3c")
            self._log("Encoding paused.")
            cur = self.prog_label.cget("text")
            if "(paused)" not in cur:
                self.prog_label.configure(text=cur + " (paused)")
        else:
            self.process_control.resume()
            self.pause_btn.configure(
                text="Pause", fg_color="#b08620",
                hover_color="#8a6a18")
            self._log("Encoding resumed.")
            self.prog_label.configure(
                text=self.prog_label.cget("text").replace(" (paused)", ""))

    def _poll(self):
        d = self._progress_data.copy()
        if d:
            pct = d.get("pct", 0)
            self.prog_bar.set(pct / 100)
            self.pct_label.configure(text=f"{pct:.0f} %")
            parts = []
            sp = d.get("speed", "")
            if sp and sp != "...":
                parts.append(f"Speed: {sp}")
            fp = d.get("fps", "")
            if fp:
                parts.append(fp)
            eta = d.get("eta", "")
            if eta and eta != "--:--":
                parts.append(f"ETA: {eta}")
            self.stats_label.configure(text="  |  ".join(parts))

            # Update per-item progress bar for encoding items
            for i, item in enumerate(self.queue):
                if item.status == "encoding" and i < len(self._q_widgets):
                    self._item_progress[i] = pct
                    w = self._q_widgets[i]
                    if "progress" in w:
                        try:
                            w["progress"].set(pct / 100)
                            if not w["progress"].winfo_ismapped():
                                w["progress"].grid(
                                    row=1, column=1, columnspan=5,
                                    padx=2, pady=(0, 2), sticky="ew")
                        except Exception:
                            pass
                    break

        if self.encoding_thread and self.encoding_thread.is_alive():
            self.after(POLL_MS, self._poll)

    # ---- worker (runs in thread) ----

    def _worker(self, settings: Settings, preview: bool,
                gpu_idx: Optional[str]):
        results: list[EncodeResult] = []
        t0 = time.time()

        log_message("")
        log_message("=" * 50)
        log_message(f"  Session (GUI): "
                     f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_message(f"  GPU: {self.gpu_name}")
        log_message(f"  {settings.codec.name} / {settings.quality} / "
                     f"{settings.resolution or 'Original'} / "
                     f"{settings.fps or 'Original'}fps / "
                     f"{settings.audio_bitrate} / {settings.audio_codec} / "
                     f"{settings.output_format} / {settings.subtitle_mode}")
        if preview:
            log_message("  Mode: Preview (60s)")
        if settings.hwaccel:
            log_message("  Hardware decode: enabled")
        log_message("=" * 50)

        self._hist(f"\n--- Session "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        self._hist(f"    {settings.codec.name} / {settings.quality} / "
                    f"{settings.output_format.upper()}")

        save_config(settings)

        qi_list = [i for i, q in enumerate(self.queue)
                   if q.status == "queued"]
        total = len(qi_list)
        concurrency_values = [
            self._effective_settings(
                self.queue[index], settings).concurrent
            for index in qi_list
        ]
        concurrent_n = (
            min(concurrency_values)
            if concurrency_values
            else (getattr(settings, "concurrent", 1) or 1)
        )
        if len(set(concurrency_values)) > 1:
            self._log_ts(
                "Mixed per-file concurrency values; using the safest "
                f"shared value ({concurrent_n}).")

        if concurrent_n > 1 and total > 1:
            self._worker_concurrent(
                settings,
                preview,
                gpu_idx,
                qi_list,
                total,
                results,
                concurrent_n,
            )
        else:
            self._worker_sequential(
                settings, preview, gpu_idx, qi_list, total, results)

        # ---- summary ----
        elapsed = time.time() - t0
        done = [r for r in results if r.success]
        skipped = [r for r in results if r.skipped]
        failed = [r for r in results
                  if not r.success and not r.skipped]

        tin = sum(r.input_size for r in done) / (1024 * 1024)
        tout = sum(r.output_size for r in done) / (1024 * 1024)
        sp = ((tin - tout) / tin * 100) if tin > 0 else 0

        lines = [
            "",
            "=" * 50,
            f"  COMPLETE  --  {len(done)} encoded, "
            f"{len(skipped)} skipped, {len(failed)} failed",
            f"  Time: {format_duration(elapsed)}",
        ]
        if done:
            lines.append(
                f"  {format_size(tin)} -> {format_size(tout)}  "
                f"({sp:.0f}% saved, "
                f"{format_size(tin - tout)} freed)")
        lines += [f"  Output: {self.output_dir}/", "=" * 50]
        self._log_ts("\n".join(lines))

        self._hist(
            f"  Total: {len(done)} OK, {len(skipped)} skip, "
            f"{len(failed)} fail | {format_duration(elapsed)}")
        log_message(
            f"  Summary: {len(done)} ok, {len(skipped)} skipped, "
            f"{len(failed)} failed | {format_duration(elapsed)}")
        log_message("")

        self.after(0, self._encoding_done, len(done))

    # ---- sequential & concurrent worker helpers ----

    def _build_output_path(
        self,
        item_path: str,
        settings: Settings,
        preview: bool,
        output_dir: Optional[str] = None,
    ) -> str:
        """Build the output file path using filename template."""
        destination = output_dir or self.output_dir
        return build_safe_output_path(
            item_path, settings, destination, preview)

    def _encode_single_item(
        self, qi: int, seq: int, total: int,
        settings: Settings, preview: bool,
        gpu_idx: Optional[str],
    ) -> EncodeResult:
        """Encode one queue item. Returns EncodeResult.

        Handles skip-existing, 2-pass, progress callbacks, and
        queue-item status updates.
        """
        return self._encode_single_item_engine(
            qi, seq, total, settings, preview, gpu_idx)

    def _encode_single_item_engine(
        self,
        qi: int,
        seq: int,
        total: int,
        settings: Settings,
        preview: bool,
        gpu_idx: Optional[str],
    ) -> EncodeResult:
        """Encode and report one item through the shared safe engine."""
        item = self.queue[qi]
        try:
            effective_settings = self._effective_settings(item, settings)
            out_path = self._planned_output_paths.get(id(item))
            if not out_path:
                out_path = self._build_output_path(
                    item.path,
                    effective_settings,
                    preview,
                    item.output_dir_override,
                )
        except ValueError as exc:
            result = EncodeResult(
                file=item.path,
                success=False,
                error=f"Invalid per-file override: {exc}",
            )
            self._update_queue_item(qi, "failed", result)
            return result
        self._update_queue_item(qi, "encoding")
        self.after(
            0,
            self.prog_label.configure,
            {"text": f"[{seq}/{total}] {Path(item.path).name}"},
        )
        self.after(0, self.prog_bar.set, 0)
        self._progress_data = {
            "pct": 0, "speed": "", "fps": "", "eta": ""}

        input_mb = get_file_size_mb(item.path)
        input_duration = get_duration(item.path)
        mode_label = " (preview 60s)" if preview else ""
        self._log_ts(
            f"[{seq}/{total}] {Path(item.path).name}  --  "
            f"{format_size(input_mb)}  |  "
            f"{format_duration(input_duration)}{mode_label}"
        )

        bus = TranscodeEventBus()

        def _on_progress(
            _filepath: str,
            percent: float,
            speed: str,
            fps: str,
            eta: str,
        ):
            self._progress_data = {
                "pct": percent,
                "speed": speed,
                "fps": fps,
                "eta": eta,
            }

        bus.on("progress", _on_progress)
        bus.on("log", self._log_ts)
        engine = TranscodeEngine(bus, self.process_control)
        result = engine.encode_to(
            item.path,
            out_path,
            effective_settings,
            preview=preview,
            gpu_index=gpu_idx,
        )

        if result.success and result.validated:
            output_mb = result.output_size / (1024 * 1024)
            saved = (
                (result.input_size - result.output_size)
                / result.input_size
                * 100
                if result.input_size > 0
                else 0
            )
            self._log_ts(
                f"  Done in {format_duration(result.encode_time)}  |  "
                f"{format_size(input_mb)} -> {format_size(output_mb)}  "
                f"({saved:.0f}% saved)  |  VALIDATED"
            )
            log_message(
                f"  [OK] {Path(item.path).name} | "
                f"{input_mb:.0f}MB->{output_mb:.0f}MB ({saved:.0f}%) | "
                f"{format_duration(result.encode_time)} | validated"
            )
            self._hist(
                f"  [OK] {Path(item.path).name} | "
                f"{format_size(input_mb)} -> {format_size(output_mb)} "
                f"({saved:.0f}%)"
            )
            self._update_queue_item(qi, "done", result)
            if result.vmaf_score is not None:
                self._log_ts(f"  VMAF: {result.vmaf_score:.2f}")
            elif result.vmaf_error:
                self._log_ts(f"  VMAF warning: {result.vmaf_error}")
            if result.post_copy_path:
                self._log_ts(
                    f"  Copied to: {result.post_copy_path}")
            elif result.post_copy_error:
                self._log_ts(
                    f"  Copy failed; original retained: "
                    f"{result.post_copy_error}")
            if (
                not preview
                and not result.post_copy_error
                and not self.process_control.cancelled
            ):
                self._handle_delete(
                    item.path,
                    effective_settings.delete_originals,
                    result.input_identity,
                )
        elif result.skipped:
            self._log_ts(
                f"  SKIPPED (validated output exists): "
                f"{Path(item.path).name}"
            )
            self._update_queue_item(qi, "skipped", result)
        elif result.error == "Cancelled by user":
            self._update_queue_item(qi, "cancelled", result)
            self._hist(f"  [CANCEL] {Path(item.path).name}")
        else:
            self._log_ts(f"  FAILED: {result.error[:200]}")
            log_message(
                f"  [FAIL] {Path(item.path).name} | "
                f"{result.error[:200]}"
            )
            self._hist(f"  [FAIL] {Path(item.path).name}")
            self._update_queue_item(qi, "failed", result)

        return result

    def _worker_sequential(
        self, settings: Settings, preview: bool,
        gpu_idx: Optional[str],
        qi_list: list[int], total: int,
        results: list[EncodeResult],
    ):
        """Encode queue items one at a time."""
        for seq, qi in enumerate(qi_list, 1):
            if self.cancel_event.is_set():
                for ri in qi_list[seq - 1:]:
                    self._update_queue_item(ri, "cancelled")
                self._log_ts("Encoding cancelled.")
                break
            r = self._encode_single_item(
                qi, seq, total, settings, preview, gpu_idx)
            results.append(r)

    def _worker_concurrent(
        self, settings: Settings, preview: bool,
        gpu_idx: Optional[str],
        qi_list: list[int], total: int,
        results: list[EncodeResult],
        concurrent_n: int,
    ):
        """Encode queue items concurrently using a thread pool."""
        self._log_ts(f"Concurrent encoding: {concurrent_n} workers")

        completed_count = 0
        lock = threading.Lock()

        def _run(qi: int, seq: int) -> EncodeResult:
            nonlocal completed_count
            r = self._encode_single_item(
                qi, seq, total, settings, preview, gpu_idx)
            with lock:
                completed_count += 1
                overall = completed_count / total * 100
                self.after(0, self.prog_label.configure,
                           {"text": f"Batch: {completed_count}/{total} complete"})
                self.after(0, self.prog_bar.set, overall / 100)
                self.after(0, self.pct_label.configure,
                           {"text": f"{overall:.0f} %"})
            return r

        with ThreadPoolExecutor(max_workers=concurrent_n) as pool:
            futures = {}
            for seq, qi in enumerate(qi_list, 1):
                if self.cancel_event.is_set():
                    break
                f = pool.submit(_run, qi, seq)
                futures[f] = qi

            for future in as_completed(futures):
                try:
                    r = future.result()
                    results.append(r)
                except Exception as exc:
                    qi = futures[future]
                    item = self.queue[qi]
                    r = EncodeResult(file=item.path, success=False,
                                     error=str(exc))
                    results.append(r)
                    self._update_queue_item(qi, "failed", r)

    # ---- post-encode helpers ----

    def _handle_delete(
        self,
        filepath: str,
        mode: str,
        expected_identity: Optional[tuple[int, int, int, int, int]],
    ):
        def _delete():
            deleted, error = delete_source_if_unchanged(
                filepath, expected_identity)
            if deleted:
                self._log_ts(
                    f"  Deleted original: {Path(filepath).name}")
            else:
                self._log_ts(f"  Original retained: {error}")

        if mode == "yes":
            _delete()
        elif mode == "ask":
            answer: list[Optional[bool]] = [None]
            ev = threading.Event()

            def _ask():
                answer[0] = messagebox.askyesno(
                    "Delete Original?",
                    f"Delete original file?\n{Path(filepath).name}")
                ev.set()

            self.after(0, _ask)
            ev.wait(timeout=120)
            if answer[0]:
                if self.process_control.cancelled:
                    self._log_ts(
                        "  Original retained: cancellation was requested.")
                else:
                    _delete()

    def _encoding_done(self, done_count: int):
        self._is_encoding = False
        self._planned_output_paths.clear()
        self._stop_status_polling()
        self.time_est_label.configure(text="")
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(
            state="disabled", text="Pause",
            fg_color="#b08620", hover_color="#8a6a18")
        self.cancel_btn.configure(state="disabled")
        self.prog_bar.set(1.0 if done_count else 0)
        self.pct_label.configure(
            text="100 %" if done_count else "0 %")
        self.prog_label.configure(
            text="Done!" if done_count else "Ready")
        self.stats_label.configure(text="")

        try:
            self.tabview.set("Log")
        except Exception:
            pass
        try:
            notify_complete(
                done_count,
                sound=self.notif_sound_var.get(),
                toast=self.notif_toast_var.get())
        except Exception:
            pass

        # Post-encode action
        if done_count > 0:
            settings = self._build_settings()
            if settings and settings.post_action != "none":
                self._log(f"Post-action: {settings.post_action}")
                execute_post_action(settings.post_action,
                                    settings.post_command)

    # ==================================================================
    #  POST-ACTION UI CALLBACK
    # ==================================================================

    def _on_post_action_change(self, value: str):
        """Show/hide the custom command entry when Post-Action changes."""
        if value == "Command":
            self.post_cmd_entry.grid(
                row=5, column=3, columnspan=2,
                padx=8, pady=(0, 6), sticky="ew")
        else:
            self.post_cmd_entry.grid_forget()

    # ==================================================================
    #  METADATA PROBING
    # ==================================================================

    def _probe_queue_metadata(self):
        """Background probe of metadata for queue items without it."""
        for item in self.queue:
            if item.metadata is None:
                item.metadata = probe_video(item.path)
        self.after(0, self._refresh_queue)

    def _show_metadata_popup(self, event, idx: int):
        """Show a popup with full metadata for a queue item."""
        if idx < 0 or idx >= len(self.queue):
            return
        item = self.queue[idx]
        meta = item.metadata or {}
        lines = [
            f"File: {Path(item.path).name}",
            f"Size: {format_size(item.size_mb)}",
            f"Duration: {format_duration(item.duration)}",
            "",
            f"Video Codec: {meta.get('video_codec', 'N/A')}",
            f"Resolution: {meta.get('video_res', 'N/A')}",
            f"Bitrate: {meta.get('video_bitrate', 'N/A')}",
            f"FPS: {meta.get('video_fps', 'N/A')}",
            f"Pixel Fmt: {meta.get('pixel_format', 'N/A')}",
            f"Bit Depth: {meta.get('bit_depth', 'N/A') or 'N/A'}",
            "",
            f"Audio Codec: {meta.get('audio_codec', 'N/A')}",
            f"Audio Bitrate: {meta.get('audio_bitrate', 'N/A')}",
            f"Channels: {meta.get('audio_channels', 'N/A')}",
        ]
        messagebox.showinfo("Video Info", "\n".join(lines))

    # ==================================================================
    #  QUEUE REORDER
    # ==================================================================

    def _move_queue_up(self):
        """Move selected queue items up by one position."""
        if self._is_encoding:
            return
        sel = [i for i, v in enumerate(self._q_vars) if v.get()]
        if not sel or sel[0] == 0:
            return
        for i in sel:
            if i > 0:
                self.queue[i], self.queue[i - 1] = (
                    self.queue[i - 1], self.queue[i])
        self._refresh_queue()
        # Re-check the moved items
        for i in sel:
            if i - 1 >= 0 and i - 1 < len(self._q_vars):
                self._q_vars[i - 1].set(True)
        self._save_queue_to_disk()

    def _move_queue_down(self):
        """Move selected queue items down by one position."""
        if self._is_encoding:
            return
        sel = [i for i, v in enumerate(self._q_vars) if v.get()]
        if not sel or sel[-1] >= len(self.queue) - 1:
            return
        for i in reversed(sel):
            if i < len(self.queue) - 1:
                self.queue[i], self.queue[i + 1] = (
                    self.queue[i + 1], self.queue[i])
        self._refresh_queue()
        for i in sel:
            if i + 1 < len(self._q_vars):
                self._q_vars[i + 1].set(True)
        self._save_queue_to_disk()

    # ==================================================================
    #  LOG EXPORT / CLEAR
    # ==================================================================

    def _export_log(self):
        """Export the log contents to a text file."""
        path = filedialog.asksaveasfilename(
            title="Export Log",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
        if not path:
            return
        try:
            content = self.log_box.get("1.0", "end").strip()
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"Log exported to: {path}")
        except OSError as e:
            messagebox.showerror("Export Failed", str(e))

    def _clear_log(self):
        """Clear the log textbox."""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ==================================================================
    #  PROFILE COMPARISON
    # ==================================================================

    def _compare_profiles(self):
        """Encode a short preview clip with each codec and compare."""
        if not self.queue:
            messagebox.showwarning("No Files",
                                   "Add at least one file first.")
            return
        if (
            self._is_encoding
            or (self._analysis_thread and self._analysis_thread.is_alive())
        ):
            messagebox.showwarning("Busy",
                                   "Cannot compare while another job is active.")
            return
        test_file = self.queue[0].path
        quality = self.quality_var.get().lower()
        self._log("Starting profile comparison (preview clips)...")
        self.process_control.reset()
        self._analysis_thread = threading.Thread(
            target=self._run_comparison,
            args=(test_file, quality), daemon=True)
        self._analysis_thread.start()

    def _run_comparison(self, test_file: str, quality: str):
        """Background worker for profile comparison."""
        results = []
        for codec in self.available_codecs:
            if self.process_control.cancelled:
                break
            s = Settings()
            s.codec = codec
            s.quality = quality
            ext = "mkv" if codec.encoder in (
                "libsvtav1", "libaom-av1") else "mp4"
            out_file = os.path.join(
                self.output_dir,
                f"._video_transcoder_compare_{codec.encoder}_"
                f"{time.time_ns()}.{ext}",
            )

            self._log_ts(f"  Testing {codec.name}...")
            t0 = time.time()
            try:
                r = encode_file_gui(
                    test_file,
                    out_file,
                    s,
                    preview=True,
                    process_control=self.process_control,
                )
                elapsed = time.time() - t0
                out_mb = (r.output_size / (1024 * 1024)) if r.success else 0
                results.append({
                    "codec": codec.name,
                    "time": format_duration(elapsed),
                    "size": format_size(out_mb) if r.success else "FAILED",
                    "speed": (f"{r.input_duration / elapsed:.1f}x"
                              if elapsed > 0 and r.success else "-"),
                })
            except Exception as exc:
                results.append({
                    "codec": codec.name, "time": "-",
                    "size": "ERROR", "speed": "-",
                })
            # Clean up
            if os.path.isfile(out_file):
                try:
                    os.remove(out_file)
                except OSError:
                    pass

        lines = ["Profile Comparison (60s preview):\n"]
        lines.append(f"{'Codec':<25} {'Time':>8} {'Size':>10} {'Speed':>8}")
        lines.append("-" * 55)
        for r in results:
            lines.append(
                f"{r['codec']:<25} {r['time']:>8} "
                f"{r['size']:>10} {r['speed']:>8}")
        msg = "\n".join(lines)
        if not self._closing:
            try:
                self.after(0, lambda: messagebox.showinfo(
                    "Profile Comparison", msg))
                self._log_ts("Profile comparison complete.")
            except (RuntimeError, tk.TclError):
                pass

    # ==================================================================
    #  BITRATE MODE (Phase 11)
    # ==================================================================

    def _on_bitrate_mode_change(self, mode: str):
        """Enable/disable bitrate fields based on selected mode."""
        crf = mode == "CRF"
        cbr = mode == "CBR"
        vbr = mode == "VBR"
        fs = mode == "File Size"

        self.target_bitrate_entry.configure(
            state="normal" if (cbr or vbr) else "disabled")
        self.max_bitrate_entry.configure(
            state="normal" if vbr else "disabled")
        self.target_size_entry.configure(
            state="normal" if fs else "disabled")

    # ==================================================================
    #  VIDEO FILTER DIALOG (Phase 7)
    # ==================================================================

    def _open_filter_dialog(self):
        """Open a dialog to add/edit custom video filters."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Video Filters")
        dialog.geometry("420x300")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="Video Filters (one per line):",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
                         padx=12, pady=(12, 4), anchor="w")
        ctk.CTkLabel(dialog, text="Examples: eq=brightness=0.06, unsharp=5:5:0.8,"
                     " nlmeans=6:3:2, hflip",
                     font=ctk.CTkFont(size=11)).pack(padx=12, anchor="w")

        text_box = ctk.CTkTextbox(dialog, height=160, width=380)
        text_box.pack(padx=12, pady=8)
        if hasattr(self, '_custom_filters') and self._custom_filters:
            text_box.insert("1.0", "\n".join(self._custom_filters))

        def _save():
            raw = text_box.get("1.0", "end").strip()
            self._custom_filters = [
                line.strip() for line in raw.splitlines() if line.strip()]
            count = len(self._custom_filters)
            self._log(f"Set {count} custom video filter(s).")
            dialog.destroy()

        ctk.CTkButton(dialog, text="Save", command=_save, width=100).pack(
            padx=12, pady=6)

    # ==================================================================
    #  ADVANCED CODEC OPTIONS DIALOG (Phase 12)
    # ==================================================================

    def _open_advanced_dialog(self):
        """Open a dialog for advanced encoder-specific options."""
        codec_name = self.codec_var.get()
        encoder = None
        for c in self.available_codecs:
            if c.name == codec_name:
                encoder = c.encoder
                break
        if not encoder:
            from tkinter import messagebox
            messagebox.showwarning("Advanced", "Select a codec first.")
            return

        opts = ADVANCED_OPTIONS.get(encoder, [])
        if not opts:
            from tkinter import messagebox
            messagebox.showinfo("Advanced",
                                f"No advanced options for {codec_name}.")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Advanced — {codec_name}")
        dialog.geometry("450x" + str(80 + len(opts) * 40))
        dialog.transient(self)
        dialog.grab_set()

        entries: list[tuple[str, ctk.CTkEntry]] = []
        for i, opt in enumerate(opts):
            row = ctk.CTkFrame(dialog, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            ctk.CTkLabel(row, text=f"{opt['flag']}:", width=140,
                         anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, width=200,
                             placeholder_text=opt.get("default", ""))
            e.pack(side="left", padx=4)
            ctk.CTkLabel(row, text=opt.get("desc", ""),
                         font=ctk.CTkFont(size=11)).pack(side="left", padx=4)
            entries.append((opt["flag"], e))

        def _save():
            args = []
            for flag, entry in entries:
                val = entry.get().strip()
                if val:
                    args.append(flag)
                    args.append(val)
            self._advanced_args = args
            self._log(f"Set {len(args)//2} advanced arg(s) for {codec_name}.")
            dialog.destroy()

        ctk.CTkButton(dialog, text="Save", command=_save, width=100).pack(
            padx=12, pady=8)

    # ==================================================================
    #  QUEUE IMPORT / EXPORT (Phase 9)
    # ==================================================================

    def _export_queue_dialog(self):
        """Export the current queue to a JSON file."""
        from tkinter import filedialog as fd
        path = fd.asksaveasfilename(
            title="Export Queue",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")])
        if not path:
            return
        base = self._build_settings()
        document = {
            "version": 2,
            "created": datetime.now().isoformat(timespec="seconds"),
            "global_settings": (
                sanitize_portable_override(settings_to_dict(base))
                if base else {}
            ),
            "items": [
                {
                    "path": item.path,
                    "status": "queued",
                    "settings_override": sanitize_portable_override(
                        item.settings_override),
                }
                for item in self.queue
            ],
        }
        if export_queue(document, path):
            self._log(
                f"Exported {len(self.queue)} queue items to "
                f"{Path(path).name}")
        else:
            self._log("Failed to export queue.")

    def _import_queue_dialog(self):
        """Import queue items from a JSON file."""
        from tkinter import filedialog as fd
        path = fd.askopenfilename(
            title="Import Queue",
            filetypes=[("JSON files", "*.json")])
        if not path:
            return
        document = import_queue(path)
        if not document:
            self._log("Failed to import queue (invalid format).")
            return
        items = (
            document.get("items", [])
            if isinstance(document, dict)
            else document
        )
        if not isinstance(items, list):
            self._log("Failed to import queue (invalid item list).")
            return
        global_override = (
            document.get("global_settings", {})
            if isinstance(document, dict)
            else {}
        )
        existing = {item.path for item in self.queue}
        imported = 0
        missing = 0
        rejected = 0
        base = self._build_settings()
        if base is None:
            return
        for saved in items:
            if not isinstance(saved, dict):
                rejected += 1
                continue
            filepath = saved.get("path", "")
            if not isinstance(filepath, str) or not os.path.isfile(filepath):
                missing += 1
                continue
            if filepath in existing:
                continue
            try:
                override = merge_portable_overrides(
                    base,
                    global_override,
                    saved.get("settings_override", {}),
                    self.available_codecs,
                )
            except (TypeError, ValueError):
                rejected += 1
                continue
            item = QueueItem(
                path=filepath,
                settings_override=override,
                output_dir_override=None,
            )
            item.size_mb = get_file_size_mb(filepath)
            item.duration = get_duration(filepath)
            self.queue.append(item)
            existing.add(filepath)
            imported += 1

        if imported:
            self._refresh_queue()
            self._update_estimate()
            self._save_queue_to_disk()
            threading.Thread(
                target=self._probe_queue_metadata, daemon=True).start()
        self._log(
            f"Imported {imported} file(s) from {Path(path).name}; "
            f"{missing} missing, {rejected} unsafe/invalid.")

    # ==================================================================
    #  QUALITY / SCENE ANALYSIS
    # ==================================================================

    def _selected_queue_indices(self) -> list[int]:
        return [
            index for index, selected in enumerate(self._q_vars)
            if selected.get()
        ]

    def _analyze_scenes(self):
        """Analyze scene boundaries for one selected source video."""
        if (
            self._is_encoding
            or (self._analysis_thread and self._analysis_thread.is_alive())
        ):
            messagebox.showwarning(
                "Busy", "Wait for the active encode or analysis to finish.")
            return
        selected = self._selected_queue_indices()
        if len(selected) != 1:
            messagebox.showinfo(
                "Analyze Scenes", "Select exactly one queue item.")
            return
        dialog = ctk.CTkInputDialog(
            text="Scene threshold from 0.0 to 1.0:",
            title="Analyze Scenes",
        )
        raw_threshold = dialog.get_input()
        if raw_threshold is None:
            return
        try:
            threshold = float(raw_threshold)
            if not 0.0 <= threshold <= 1.0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Analyze Scenes", "Threshold must be between 0 and 1.")
            return

        item = self.queue[selected[0]]
        self._log(
            f"Analyzing scenes in {Path(item.path).name} "
            f"(threshold {threshold:.2f})...")
        self.process_control.reset()

        def _run():
            timestamps = detect_scenes(
                item.path,
                threshold,
                process_control=self.process_control,
            )
            if not self._closing:
                try:
                    self.after(
                        0, self._show_scene_results,
                        item.path, threshold, timestamps)
                except (RuntimeError, tk.TclError):
                    pass

        self._analysis_thread = threading.Thread(
            target=_run, daemon=True)
        self._analysis_thread.start()

    def _show_scene_results(
        self,
        filepath: str,
        threshold: float,
        timestamps: list[float],
    ):
        if timestamps:
            preview = ", ".join(
                format_duration(timestamp)
                for timestamp in timestamps[:12])
            if len(timestamps) > 12:
                preview += ", ..."
            text = (
                f"Found {len(timestamps)} scene boundaries.\n\n"
                f"{preview}\n\nExport the complete list as CSV?"
            )
        else:
            text = (
                "No scene boundaries were found, or analysis could not "
                "complete.\n\nExport an empty CSV report?"
            )
        if not messagebox.askyesno("Scene Analysis", text):
            return
        output = filedialog.asksaveasfilename(
            title="Export Scene Analysis",
            defaultextension=".csv",
            initialfile=f"{Path(filepath).stem}_scenes.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not output:
            return
        try:
            with open(output, "w", encoding="utf-8", newline="") as handle:
                handle.write("scene,timestamp_seconds,display,threshold\n")
                for index, timestamp in enumerate(timestamps, 1):
                    handle.write(
                        f"{index},{timestamp:.3f},"
                        f"{format_duration(timestamp)},{threshold:.3f}\n")
            self._log(f"Scene report exported: {output}")
        except OSError as exc:
            messagebox.showerror(
                "Scene Analysis", f"Could not export report:\n{exc}")

    def _score_selected_vmaf(self):
        """Score the completed output for one selected queue item."""
        if (
            self._is_encoding
            or (self._analysis_thread and self._analysis_thread.is_alive())
        ):
            messagebox.showwarning(
                "Busy", "Wait for the active encode or analysis to finish.")
            return
        selected = self._selected_queue_indices()
        if len(selected) != 1:
            messagebox.showinfo(
                "Score VMAF", "Select exactly one completed queue item.")
            return
        item = self.queue[selected[0]]
        output = item.result.output_file if item.result else ""
        if not output or not os.path.isfile(output):
            messagebox.showinfo(
                "Score VMAF",
                "The selected item does not have a completed output file.",
            )
            return
        self._log(f"Calculating VMAF for {Path(output).name}...")
        self.process_control.reset()

        def _run():
            score = run_vmaf_score(
                item.path,
                output,
                sample_seconds=(
                    item.result.vmaf_sample_seconds if item.result else 30),
                trim_start=(
                    item.result.reference_start if item.result else 0.0),
                process_control=self.process_control,
            )
            if item.result:
                item.result.vmaf_score = score

            def _show():
                if score is None:
                    messagebox.showwarning(
                        "VMAF",
                        "VMAF analysis failed or is unavailable for this "
                        "media pair.",
                    )
                else:
                    messagebox.showinfo(
                        "VMAF",
                        f"{Path(output).name}\n\nVMAF score: {score:.2f}",
                    )
                    self._log(f"VMAF {score:.2f}: {Path(output).name}")

            if not self._closing:
                try:
                    self.after(0, _show)
                except (RuntimeError, tk.TclError):
                    pass

        self._analysis_thread = threading.Thread(
            target=_run, daemon=True)
        self._analysis_thread.start()

    # ==================================================================
    #  SUBTITLE EXTRACTION (Phase 8)
    # ==================================================================

    def _extract_subtitles(self):
        """Extract subtitles from selected queue items."""
        from tkinter import messagebox
        selected = [i for i, v in enumerate(self._q_vars) if v.get()]
        if not selected:
            messagebox.showinfo("Subtitles", "Select queue items first.")
            return
        count = 0
        for idx in selected:
            item = self.queue[idx]
            info = probe_video(item.path)
            subs = info.get("subtitle_streams", [])
            if not subs:
                self._log(f"No subtitles in {Path(item.path).name}")
                continue
            for si, sub in enumerate(subs):
                lang = sub.get("language", "und")
                out = os.path.join(
                    self.output_dir,
                    f"{Path(item.path).stem}_{lang}_{si}.srt")
                cmd = build_subtitle_extract_command(
                    item.path, out, stream_index=si)
                try:
                    subprocess.run(
                        cmd,
                        capture_output=True,
                        timeout=60,
                        creationflags=SUBPROCESS_CREATION_FLAGS,
                    )
                    count += 1
                except (subprocess.TimeoutExpired, OSError):
                    self._log(f"Failed to extract subtitle {si} from {Path(item.path).name}")
        self._log(f"Extracted {count} subtitle track(s).")

    # ==================================================================
    #  AUDIO EXTRACTION
    # ==================================================================

    def _on_audio_extract_toggle(self):
        """Toggle audio extraction mode UI state."""
        if self.audio_extract_var.get():
            self.audio_fmt_menu.configure(state="normal")
        else:
            self.audio_fmt_menu.configure(state="disabled")

    # ==================================================================
    #  QUEUE PERSISTENCE
    # ==================================================================

    def _save_queue_to_disk(self):
        """Save current queue to disk for persistence."""
        items: list[dict] = []
        for q in self.queue:
            if q.status in ("queued", "failed", "encoding", "cancelled"):
                items.append({
                    "path": q.path,
                    "status": (
                        "failed" if q.status == "failed" else "queued"),
                    "settings_override": q.settings_override,
                    "output_dir_override": q.output_dir_override,
                })
        save_queue({
            "version": 2,
            "created": datetime.now().isoformat(timespec="seconds"),
            "items": items,
        })

    def _load_queue_from_disk(self):
        """Restore queue items from previous session."""
        document = load_queue()
        items = (
            document.get("items", [])
            if isinstance(document, dict)
            else document
        )
        if not isinstance(items, list) or not items:
            return
        restored = 0
        missing = 0
        existing = {q.path for q in self.queue}
        for saved in items:
            if not isinstance(saved, dict):
                continue
            path = saved.get("path", "")
            if not os.path.isfile(path):
                missing += 1
                continue
            if path in existing:
                continue
            item = QueueItem(
                path=path,
                status="queued",
                settings_override=(
                    saved.get("settings_override", {})
                    if isinstance(saved.get("settings_override", {}), dict)
                    else {}
                ),
                output_dir_override=(
                    saved.get("output_dir_override")
                    if isinstance(saved.get("output_dir_override"), str)
                    else None
                ),
            )
            item.size_mb = get_file_size_mb(path)
            item.duration = get_duration(path)
            self.queue.append(item)
            existing.add(path)
            restored += 1
        if restored:
            self._refresh_queue()
            self._update_estimate()
            threading.Thread(
                target=self._probe_queue_metadata, daemon=True).start()
            self._log(
                f"Restored {restored} file(s) from previous queue.")
        if missing:
            self._log(
                f"Queue restore: {missing} missing file(s) were retained "
                "in neither the active queue nor output plan.")

    # ==================================================================
    #  GEOMETRY & THEME PERSISTENCE
    # ==================================================================

    def _save_geometry(self):
        """Save window position, size, and theme to config."""
        update_config_values({
            "window_geometry": self.geometry(),
            "theme": self.theme_var.get(),
        })

    def _restore_geometry(self):
        """Restore window position, size, and theme from config."""
        try:
            cfg = load_config() or {}
            geo = cfg.get("window_geometry")
            if geo:
                self.geometry(geo)
            theme = cfg.get("theme", "dark")
            self.theme_var.set(theme)
            ctk.set_appearance_mode(theme)
        except Exception:
            pass

    # ==================================================================
    #  STATUS BAR (SYSTEM STATS)
    # ==================================================================

    def _start_status_polling(self):
        """Begin periodic status bar updates."""
        self._status_polling = True
        self._update_status_bar()

    def _stop_status_polling(self):
        """Stop periodic status bar updates."""
        self._status_polling = False

    def _update_status_bar(self):
        """Update the status bar with system stats."""
        if not self._status_polling:
            return

        def _fetch():
            stats = get_system_stats()
            parts = []
            if stats.get("cpu"):
                parts.append(f"CPU: {stats['cpu']}")
            if stats.get("gpu_util"):
                parts.append(f"GPU: {stats['gpu_util']}")
            if stats.get("gpu_temp"):
                parts.append(f"Temp: {stats['gpu_temp']}")
            text = "  |  ".join(parts) if parts else ""
            self.after(0, self.status_label.configure, {"text": text})

        threading.Thread(target=_fetch, daemon=True).start()
        if self._status_polling:
            self.after(3000, self._update_status_bar)

    # ==================================================================
    #  ENCODING TIME ESTIMATE
    # ==================================================================

    def _estimate_batch_time(self) -> str:
        """Estimate total encoding time for queued files."""
        queued = [q for q in self.queue if q.status == "queued"]
        if not queued:
            return ""
        total_dur = sum(q.duration for q in queued)
        codec_name = self.codec_var.get()
        speed_map = {
            "H.265 GPU (NVENC)": 3.0, "H.264 GPU (NVENC)": 4.0,
            "H.265 GPU (AMF)": 2.5, "H.264 GPU (AMF)": 3.5,
            "H.265 GPU (QSV)": 2.5, "H.264 GPU (QSV)": 3.5,
            "H.265 CPU": 0.3, "H.264 CPU": 0.8,
            "AV1 CPU": 0.1, "SVT-AV1": 0.5,
        }
        mult = speed_map.get(codec_name, 1.0)
        est_sec = total_dur / mult if mult > 0 else total_dur
        return format_duration(est_sec)

    # ==================================================================
    #  TOOLTIPS
    # ==================================================================

    def _apply_tooltips(self, sf, cbox, cbox2):
        """Apply hover tooltips to settings widgets."""
        # Walk through known widgets and add tooltips
        tip = _ToolTip
        for child in sf.winfo_children():
            try:
                txt = child.cget("text") if hasattr(child, "cget") else ""
            except (ValueError, Exception):
                continue
            if not isinstance(txt, str):
                continue
            if "Codec" in txt:
                tip(child, "Video encoder. GPU codecs are fastest.")
            elif "Quality" in txt:
                tip(child, "Quality level. High = larger file, better quality.")
            elif "Resolution" in txt:
                tip(child, "Output resolution. Original keeps source size.")
            elif "FPS" in txt:
                tip(child, "Frame rate. Original keeps source FPS.")
            elif "Audio" in txt and "Codec" not in txt:
                tip(child, "Audio bitrate for the output file.")
            elif "Format" in txt:
                tip(child, "Container format: MP4 (most compatible), MKV (flexible).")
        # Checkboxes
        for child in cbox.winfo_children():
            try:
                txt = child.cget("text") if hasattr(child, "cget") else ""
            except (ValueError, Exception):
                continue
            if not isinstance(txt, str):
                continue
            if "Skip" in txt:
                tip(child, "Skip files that already exist in output folder.")
            elif "HW Accel" in txt:
                tip(child, "Use GPU for decoding (faster, uses more VRAM).")
            elif "Preview" in txt:
                tip(child, "Encode only first 60 seconds for testing.")
            elif "10-bit" in txt:
                tip(child, "Use 10-bit color depth (better gradients).")
            elif "2-pass" in txt:
                tip(child, "Two-pass encoding for CPU codecs (better quality).")
            elif "Auto-crop" in txt:
                tip(child, "Detect and remove black bars automatically.")
        for child in cbox2.winfo_children():
            try:
                txt = child.cget("text") if hasattr(child, "cget") else ""
            except (ValueError, Exception):
                continue
            if not isinstance(txt, str):
                continue
            if "Audio extract" in txt:
                tip(child, "Extract audio only, no video transcoding.")
            elif "Sound" in txt:
                tip(child, "Play a beep when encoding finishes.")
            elif "Toast" in txt:
                tip(child, "Show a Windows notification when done.")

    # ==================================================================
    #  WATCH FOLDER
    # ==================================================================

    def _toggle_watch_folder(self):
        if self._watch_active:
            self._watch_active = False
            self.watch_btn.configure(
                text="Watch Folder", fg_color="#555",
                hover_color="#444")
            self._log("Stopped watching folder.")
            return

        folder = filedialog.askdirectory(
            title="Select Folder to Watch for New Videos")
        if not folder:
            return

        self._watch_active = True
        self._watch_dir = folder
        # Seed with already-known files
        self._watch_seen = {
            str(f) for f in Path(folder).iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS}
        self.watch_btn.configure(
            text="Stop Watch", fg_color="#b08620",
            hover_color="#8a6a18")
        self._log(f"Watching folder: {folder}")
        self._poll_watch_folder()

    def _poll_watch_folder(self):
        """Check watched folder for new files every 3 seconds.

        Uses file-size stability check: a file is only considered ready
        when its size remains unchanged between two consecutive polls.
        """
        if not self._watch_active or not self._watch_dir:
            return
        try:
            current = {
                str(f) for f in Path(self._watch_dir).iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS}
            new_candidates = sorted(current - self._watch_seen)
            if new_candidates:
                ready: list[str] = []
                for fp in new_candidates:
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        continue
                    prev_size = self._watch_sizes.get(fp)
                    self._watch_sizes[fp] = size
                    if prev_size is not None and prev_size == size and size > 0:
                        ready.append(fp)
                if ready:
                    self._watch_seen.update(ready)
                    for fp in ready:
                        self._watch_sizes.pop(fp, None)
                    self._add_to_queue(ready)
                    self._log(f"Watch: added {len(ready)} new file(s).")
        except OSError:
            pass
        if self._watch_active:
            self.after(3000, self._poll_watch_folder)

    # ==================================================================
    #  KEYBOARD SHORTCUTS
    # ==================================================================

    def _bind_shortcuts(self):
        self.bind("<Control-o>", lambda e: self._browse_files())
        self.bind("<Control-O>", lambda e: self._browse_files())
        self.bind("<Return>", lambda e: self._start_encoding_shortcut())
        self.bind("<Escape>", lambda e: self._cancel_shortcut())
        self.bind("<Control-p>", lambda e: self._toggle_pause_shortcut())
        self.bind("<Control-P>", lambda e: self._toggle_pause_shortcut())
        self.bind("<Delete>", lambda e: self._remove_selected())
        self.bind("<Control-a>", lambda e: self._select_all_queue())
        self.bind("<Control-A>", lambda e: self._select_all_queue())

    def _start_encoding_shortcut(self):
        if not self._is_encoding:
            self._start_encoding()

    def _cancel_shortcut(self):
        if self._is_encoding:
            self._cancel_encoding()

    def _toggle_pause_shortcut(self):
        if self._is_encoding:
            self._toggle_pause()

    def _select_all_queue(self):
        for v in self._q_vars:
            v.set(True)

    # ==================================================================
    #  CUSTOM PRESETS
    # ==================================================================

    def _save_custom_preset(self):
        settings = self._build_settings()
        if settings is None:
            return
        dialog = ctk.CTkInputDialog(
            text="Enter a name for this preset:",
            title="Save Custom Preset")
        name = dialog.get_input()
        if not name or not name.strip():
            return
        name = name.strip()
        save_custom_preset(name, settings)
        self._log(f"Custom preset saved: {name}")

    def _load_custom_preset(self):
        presets = load_custom_presets()
        if not presets:
            messagebox.showinfo("No Presets",
                                "No custom presets found.")
            return
        names = list(presets.keys())
        dialog = _PresetPicker(self, "Load Custom Preset", names)
        self.wait_window(dialog)
        chosen = dialog.result
        if not chosen:
            return
        p = presets[chosen]
        self._apply_custom_preset_dict(p)
        self._log(f"Loaded custom preset: {chosen}")

    def _delete_custom_preset(self):
        presets = load_custom_presets()
        if not presets:
            messagebox.showinfo("No Presets",
                                "No custom presets found.")
            return
        names = list(presets.keys())
        dialog = _PresetPicker(self, "Delete Custom Preset", names)
        self.wait_window(dialog)
        chosen = dialog.result
        if not chosen:
            return
        if messagebox.askyesno("Confirm",
                               f"Delete preset '{chosen}'?"):
            delete_custom_preset(chosen)
            self._log(f"Deleted custom preset: {chosen}")

    def _apply_custom_preset_dict(self, p: dict):
        """Apply a custom preset dict to the UI variables."""
        encoder = p.get("codec_encoder")
        if encoder:
            codec = find_codec_by_encoder(encoder, self.has_gpu,
                                           self.has_amd, self.has_intel)
            if codec:
                self.codec_var.set(codec.name)
        q = p.get("quality", "medium").capitalize()
        if q in ("High", "Medium", "Low"):
            self.quality_var.set(q)
        res = p.get("resolution")
        self.resolution_var.set(f"{res}p" if res else "Original")
        fps = p.get("fps")
        self.fps_var.set(f"{fps} fps" if fps else "Original")
        abr = p.get("audio_bitrate")
        if abr:
            self.audio_var.set(abr)
        ac = p.get("audio_codec", "aac")
        self.audio_codec_var.set(
            {"aac": "AAC", "opus": "Opus", "copy": "Copy"}.get(ac, "AAC"))
        fmt = p.get("output_format", "mp4").upper()
        if fmt in ("MP4", "MKV", "MOV"):
            self.format_var.set(fmt)
        sub = p.get("subtitle_mode", "keep")
        self.subtitle_var.set(
            {"keep": "Keep", "burn": "Burn In",
             "strip": "Strip"}.get(sub, "Keep"))
        self.skip_var.set(p.get("skip_existing", True))
        self.hwaccel_var.set(p.get("hwaccel", False))
        self.ten_bit_var.set(p.get("ten_bit", False))
        self.two_pass_var.set(p.get("two_pass", False))
        tmpl = p.get("filename_template", "{name}")
        if tmpl:
            self.template_var.set(tmpl)
        self.hdr_var.set({
            "auto": "Auto", "passthrough": "Passthrough",
            "tonemap": "Tonemap", "off": "Off",
        }.get(p.get("hdr_mode", "auto"), "Auto"))
        self.bitrate_mode_var.set({
            "crf": "CRF", "cbr": "CBR", "vbr": "VBR",
            "filesize": "File Size",
        }.get(p.get("bitrate_mode", "crf"), "CRF"))
        self.target_bitrate_var.set(p.get("target_bitrate", ""))
        self.max_bitrate_var.set(p.get("max_bitrate", ""))
        target_size = p.get("target_size_mb", 0)
        self.target_size_var.set(str(target_size) if target_size else "")
        self.vmaf_var.set(p.get("vmaf_enabled", False))
        self.post_copy_var.set(p.get("post_copy_dir", ""))
        self._custom_filters = list(p.get("video_filters") or [])
        self._advanced_args = list(p.get("advanced_args") or [])
        self._on_bitrate_mode_change(self.bitrate_mode_var.get())
        self._update_estimate()

    # ==================================================================
    #  THUMBNAIL PREVIEW
    # ==================================================================

    def _show_thumbnail(self, video_path: str):
        if not FFMPEG_PATH or not _HAS_PIL:
            return
        thumb_dir = str(APP_PATHS.thumbnail_cache)
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_file = os.path.join(
            thumb_dir, Path(video_path).stem + ".png")

        def _gen():
            if not os.path.isfile(thumb_file):
                generate_thumbnail(video_path, thumb_file)
            if os.path.isfile(thumb_file):
                self.after(0, self._display_thumb, thumb_file)

        threading.Thread(target=_gen, daemon=True).start()

    def _display_thumb(self, path: str):
        try:
            img = _PilImg.open(path).resize((192, 108))
            photo = _PilImgTk.PhotoImage(img)
            if self._thumb_label is None:
                self._thumb_label = ctk.CTkLabel(
                    self.est_frame, text="", image=photo)
                self._thumb_label.pack(side="right", padx=12, pady=4)
            else:
                self._thumb_label.configure(image=photo)
            self._thumb_label._photo = photo  # prevent GC
        except Exception:
            pass

    # ==================================================================
    #  SYSTEM TRAY
    # ==================================================================

    def _minimize_to_tray(self):
        if not _HAS_TRAY:
            return
        self.withdraw()
        self._create_tray()

    def _create_tray(self):
        img = PilImage.new("RGB", (64, 64), color=(45, 138, 78))
        d = ImageDraw.Draw(img)
        d.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
        d.text((22, 22), "VT", fill=(45, 138, 78))
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "VideoTranscoder", img, "Video Transcoder", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None):
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.after(0, self.deiconify)

    def _tray_quit(self, icon=None, item=None):
        if self._tray_icon:
            self._tray_icon.stop()
        self.after(0, self.destroy)

    # ==================================================================
    #  STARTUP INFO
    # ==================================================================

    def _show_startup_info(self):
        self._log(f"GPU: {self.gpu_name}")
        if self.has_amd:
            self._log(f"AMD GPU: {self.amd_name}")
        if self.has_intel:
            self._log(f"Intel GPU: {self.intel_name}")
        if len(self.all_gpus) > 1:
            names = ", ".join(g["name"] for g in self.all_gpus)
            self._log(f"Multi-GPU detected: {names}")
        if check_ffmpeg():
            self._log(f"FFmpeg: {FFMPEG_PATH}")
        else:
            self._log("WARNING: FFmpeg not found!")
            self._log("  Download: https://www.gyan.dev/ffmpeg/builds/")
        if _HAS_DND:
            self._log("Drag-and-drop: enabled")
        else:
            self._log("Drag-and-drop: install 'tkinterdnd2' for support")
        if _HAS_TRAY:
            self._log("System tray: available")
        if _HAS_PIL:
            self._log("Thumbnails: available (click a filename to preview)")
        self._log("Ready.\n")

    # ==================================================================
    #  CLOSE
    # ==================================================================

    def _on_close(self):
        analysis_active = bool(
            self._analysis_thread and self._analysis_thread.is_alive())
        if self._is_encoding or analysis_active:
            if not messagebox.askyesno(
                    "Background Work Active",
                    "Encoding or analysis is in progress. Quit and cancel it?"):
                return
        self._closing = True
        if self._is_encoding or analysis_active:
            self.process_control.cancel()
            for worker in (self.encoding_thread, self._analysis_thread):
                if worker and worker.is_alive():
                    worker.join(timeout=3.0)
        # Save queue and geometry on exit
        self._save_queue_to_disk()
        self._save_geometry()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.destroy()


# ============================================================
#  ENTRY POINT
# ============================================================

def _portable_self_test_argument(args: list[str]) -> str | None:
    """Return the report path requested by a pre-GUI self-test flag."""
    for flag in ("--portable-self-test", "--self-test-report"):
        if flag not in args:
            continue
        index = args.index(flag)
        if index + 1 < len(args) and not args[index + 1].startswith("--"):
            return args[index + 1]
        return str(
            Path(os.environ.get("TEMP", os.getcwd()))
            / "VideoTranscoderPortableSelfTest"
            / "report.json"
        )
    return None


def main() -> int:
    global FFMPEG_PATH, FFPROBE_PATH
    FFMPEG_PATH, FFPROBE_PATH = initialize_app_state()
    args = sys.argv[1:]
    self_test_report = _portable_self_test_argument(args)
    if self_test_report:
        return run_portable_self_test(self_test_report)

    app = TranscoderApp()

    # Command-line args (e.g. from drag-drop onto .bat)
    if args:
        files = [f for f in args if os.path.isfile(f)]
        if files:
            app._add_to_queue(files)
            app._log(f"Loaded {len(files)} file(s) from command line.")

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
