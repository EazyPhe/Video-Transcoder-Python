# Video Transcoder — Python Edition v2.1

A feature-rich video transcoding tool built on FFmpeg with two interfaces: a **GUI** (CustomTkinter) for point-and-click use and a **CLI** (Rich) for terminal power users. Supports NVIDIA NVENC GPU acceleration, multiple codecs, presets, batch processing, a full encoding queue, 10-bit/HDR encoding, 2-pass mode, concurrent encoding, watch folders, and custom preset management.

---

## Quick Start

### GUI (Graphical Interface)

1. Double-click **`run_gui.bat`** — it auto-installs dependencies on first run.
2. Click **Browse Files** or **Browse Folder** to add videos to the queue.
3. Pick a preset or adjust settings with the dropdowns.
4. Click **Start Encoding**. Progress, logs, and per-file status update in real time.
5. Compressed files appear in the `compressed/` subfolder (or your chosen output folder).

**Drag & Drop:** Drag video files onto `run_gui.bat` to open the GUI with those files pre-loaded. If you install the optional `tkinterdnd2` package, you can also drop files directly onto the GUI window.

### CLI (Terminal Interface)

1. Copy the folder into a directory with video files (or navigate there via the command line).
2. Double-click **`run.bat`** — it auto-installs dependencies on first run.
3. Follow the interactive menus to choose a preset or configure custom settings.
4. Compressed files appear in the `compressed/` subfolder.

**Drag & Drop:** Drag any video file onto `run.bat` to encode it directly in single-file mode.

---

## Requirements

- **Python 3.10+**
- **FFmpeg** with ffprobe
- **NVIDIA GPU** (optional) — for NVENC hardware acceleration

### Core Dependencies

Dependencies are installed automatically by the `.bat` launchers, or manually:
```
pip install -r requirements.txt
```
This installs:
- `rich>=13.0` — CLI terminal UI
- `customtkinter>=5.2` — GUI framework

### Optional Dependencies (GUI Extras)

These are optional — the GUI works without them, but specific features will be disabled:

| Package | Feature | Install |
|---|---|---|
| `tkinterdnd2` | Native drag-and-drop onto the GUI window | `pip install tkinterdnd2` |
| `Pillow` | Video thumbnail previews + system tray icon | `pip install Pillow` |
| `pystray` | Minimize to system tray | `pip install pystray` |

Install all optional extras at once:
```
pip install tkinterdnd2 Pillow pystray
```

---

## Project Structure

```
Video-Transcoder-Python/
├── src/
│   ├── transcode.py        # Core encoding engine + CLI application
│   └── gui.py              # GUI application (CustomTkinter)
├── docs/
│   └── screenshots/        # Screenshots for documentation
├── run.bat                 # Double-click launcher for the CLI
├── run_gui.bat             # Double-click launcher for the GUI
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT License
└── README.md               # This file
```

Generated at runtime:

| File / Folder | Purpose |
|---|---|
| `compressed/` | Default output folder for encoded videos |
| `transcode_log.txt` | Detailed log of all sessions (shared by GUI and CLI) |
| `transcode_config.json` | Last-used settings (auto-saved, auto-loaded) |
| `custom_presets.json` | User-saved custom encoding presets (GUI only) |
| `.thumbs/` | Cached video thumbnails (GUI only, if Pillow installed) |

---

## Features

### Both Interfaces

- **5 Codecs:** H.264 GPU (NVENC), H.264 CPU, H.265 GPU (NVENC), H.265 CPU, AV1
- **GPU Auto-Detection:** Detects NVIDIA GPUs via `nvidia-smi`; shows/hides GPU codecs accordingly
- **5 Preset Profiles:** Fast & Small, Balanced, Archive Quality, Max Compression, Quick Share
- **Full Custom Settings:** Codec, quality, resolution, FPS, audio bitrate, audio codec, format, subtitles
- **Audio Codec Selection:** AAC, Opus, or Copy (passthrough)
- **Hardware Decode:** Optional `-hwaccel cuda` for faster GPU pipeline
- **10-bit / HDR Encoding:** Enable 10-bit pixel depth for H.264, H.265, and AV1 (GPU and CPU)
- **2-Pass Encoding:** Two-pass mode for CPU codecs (libx264, libx265, libaom-av1) for better quality-to-size ratio
- **Trim / Crop:** Specify start and end times (in seconds) to encode only a portion of a file
- **Batch Processing:** Encode all videos in a folder
- **Skip / Resume:** Skips files whose output already exists
- **Delete Originals:** Keep, delete automatically, or ask per file
- **Preview Mode:** Encode only the first 60 seconds as a test
- **Real-Time Progress:** Percentage, speed multiplier, FPS, ETA
- **Duration Validation:** Compares input/output duration, warns on mismatch > 2 seconds
- **Size Comparison:** Per-file and batch totals with percentage saved
- **Log File:** Timestamped session logs with `[OK]` / `[FAIL]` / `[SKIP]` entries
- **Config Persistence:** Saves and reloads last-used settings automatically
- **Notifications:** Beep + Windows toast on completion
- **FFmpeg Auto-Detection:** Finds FFmpeg automatically (PATH, common directories, saved config)
- **Drag & Drop:** Drop files onto `.bat` launchers

### GUI-Exclusive Features

- **File Queue with Status Table:** Add, remove, and clear files before encoding; see per-file status (Queued / Encoding / Done / Failed / Skipped / Cancelled) with size savings
- **Video Metadata Display:** Queue shows codec, resolution, and bitrate info; right-click any file for a detailed metadata popup (probed via ffprobe)
- **Estimated Output Size:** Approximate per-file and total estimates before encoding starts
- **Concurrent Encoding:** Encode 1-4 files simultaneously using a thread pool (configurable)
- **Watch Folder Mode:** Monitor a folder for new video files and auto-add them to the queue
- **Custom Preset Save/Load:** Save your current settings as a named preset, load or delete saved presets (`custom_presets.json`)
- **Output Filename Templates:** Choose from naming patterns like `{name}_{codec}_{quality}`, `{name}_{date}`, etc.
- **Post-Encode Actions:** Automatically shut down, sleep, or run a custom command after encoding completes
- **Keyboard Shortcuts:** Ctrl+O (add files), Enter (start), Escape (cancel), Ctrl+P (pause), Delete (remove), Ctrl+A (select all)
- **Pause / Resume:** Pause encoding mid-file and resume where you left off
- **Cancel:** Stop encoding gracefully at any point
- **Output Folder Selector:** Choose a custom output directory; open it with one click
- **Recursive Folder Scanning:** Optionally scan subfolders when browsing a directory
- **Dark / Light Theme Toggle:** Switch between dark and light mode
- **Video Thumbnail Preview:** Click any filename in the queue to generate a preview frame (requires Pillow)
- **Encoding History Tab:** Session-by-session history of all encodes
- **Native Drag-and-Drop:** Drop files directly onto the window (requires `tkinterdnd2`)
- **System Tray Minimization:** Minimize to the Windows system tray while encoding (requires `pystray` + `Pillow`)
- **Multi-GPU Support:** Select which NVIDIA GPU to use when multiple are detected
- **Settings Auto-Restore:** All settings are restored from the previous session on startup

---

## GUI Usage

### Launching

| Method | How |
|---|---|
| Double-click | `run_gui.bat` |
| Command line | `python gui.py` |
| With files | `python gui.py "video1.mp4" "video2.mkv"` |
| Drag & Drop | Drag video files onto `run_gui.bat` |

### Interface Overview

```
+-------------------------------------------------------------------+
|  Video Transcoder v2.1         [Light]  GPU: RTX 3050             |
+-------------------------------------------------------------------+
|  Files: 3 files (1.2 GB)  [Browse Files] [Browse Folder] [Watch] [x] Recursive
|  Output: compressed/                     [Change] [Open Folder]
+-------------------------------------------------------------------+
|  Preset: [Fast & Small] [Balanced] [Archive] [MaxComp] [Quick]
+-------------------------------------------------------------------+
|  Codec: H.265 GPU     Quality: High     Resolution: Original
|  Audio Codec: AAC      Frame Rate: Original
|  Audio Bitrate: 128k   Format: MP4      Subtitles: Keep
|  Filename: {name}      Post-Action: None  Concurrent: 1
|  Trim Start: ___       Trim End: ___
|  Originals: Keep       GPU: Auto
|  Custom Presets: [Save] [Load] [Delete]
|  [x] Skip existing  [x] GPU decode  [ ] Preview  [ ] 10-bit  [ ] 2-Pass
+-------------------------------------------------------------------+
|  Estimated output: ~350 MB  (~71% reduction from 1.2 GB)
+-------------------------------------------------------------------+
|  [ Queue ]  [ Log ]  [ History ]                                  |
|  +---+----------------+-------------+------+------+------+------+ |
|  |   | File           | Info        | Size | Dur. | Est. | Status |
|  | x | video1.mp4     | 1080p|h264  | 500M | 2:34 | 150M | Done   |
|  | x | video2.mkv     | 4K|hevc     | 400M | 1:50 | 120M | Encode |
|  | x | video3.mov     | 720p|h264   | 300M | 1:15 |  80M | Queued |
|  +---+----------------+-------------+------+------+------+------+ |
|            [Remove Selected] [Clear All]                          |
+-------------------------------------------------------------------+
|  [1/3] video2.mkv   ============-------   67%                    |
|  Speed: 2.3x | 142 fps | ETA: 0:22                              |
|         [Start Encoding] [Pause] [Cancel] [To Tray]              |
+-------------------------------------------------------------------+
```

### GUI Workflow

1. **Add files** — Click Browse Files, Browse Folder, drag onto the window, or pass files via command line.
2. **Review queue** — The Queue tab shows all files with size, duration, and estimated output. Remove unwanted files with the checkbox + Remove Selected.
3. **Configure** — Click a preset for one-click setup, or manually adjust each dropdown. Toggle Preview mode to test with only the first 60 seconds.
4. **Estimate** — The estimate bar updates automatically as you change settings, showing approximate output size and savings.
5. **Encode** — Click **Start Encoding**. Per-file progress appears in the progress bar, and the Queue tab updates status in real time.
6. **Pause / Resume** — Click **Pause** to freeze encoding mid-file. Click **Resume** to continue.
7. **Cancel** — Click **Cancel** to stop. Remaining queued files are marked as cancelled.
8. **Review** — Switch to the Log tab for detailed output or the History tab for session summaries. Click **Open Folder** to view results.

### Tabs

| Tab | Contents |
|---|---|
| **Queue** | File list with checkboxes, sizes, durations, metadata info, estimates, and live status. Click a filename for a thumbnail preview; right-click for detailed metadata. |
| **Log** | Full encoding log with FFmpeg command, per-file results, and batch summary. |
| **History** | Persistent session history showing codec settings and per-file outcomes across multiple runs. |

### Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open file browser to add videos |
| `Enter` | Start encoding |
| `Escape` | Cancel encoding |
| `Ctrl+P` | Pause / Resume encoding |
| `Delete` | Remove selected items from queue |
| `Ctrl+A` | Select all items in queue |

---

## CLI Usage

### Launching

| Method | How |
|---|---|
| Double-click | `run.bat` (from a folder containing videos) |
| Command line | `python transcode.py` |
| Drag & Drop | Drag a video file onto `run.bat` |

### Interactive Menus

The CLI walks you through settings step by step:

1. **Setup mode** — Quick Preset or Custom Settings
2. **Preset / Custom** — Choose a profile or configure each option individually
3. **Shared options** — Delete originals, skip existing
4. **Mode** — Batch (all videos), Single (pick one), Preview (first 60 seconds)
5. **Confirm** — Review the settings table, then start encoding

### CLI Progress Bar

```
@ [1/5] video_name.mp4 ================--------  67%  * 142 fps * 2.3x  * 0:45 -> 0:22
```

---

## Preset Profiles

| # | Name | Codec | Quality | Resolution | FPS | Audio |
|---|---|---|---|---|---|---|
| 1 | Fast & Small | H.264 GPU/CPU | Medium | 720p | 30 | 128k |
| 2 | Balanced | H.265 GPU/CPU | High | Original | Original | 192k |
| 3 | Archive Quality | H.265 CPU (slow) | High | Original | Original | 192k |
| 4 | Max Compression | AV1 | Medium | 720p | 30 | 96k |
| 5 | Quick Share | H.264 GPU/CPU | Low | 480p | 30 | 64k |

GPU presets automatically fall back to CPU equivalents when no NVIDIA GPU is detected.

---

## Custom Settings

| Setting | Options | Notes |
|---|---|---|
| Codec | H.265 GPU, H.264 GPU, H.265 CPU, H.264 CPU, AV1 | GPU codecs require NVIDIA GPU |
| Quality | High / Medium / Low | Maps to CRF/CQ values per codec |
| Resolution | Original / 1080p / 720p / 480p | Downscale only |
| Frame Rate | Original / 60 / 30 / 24 fps | |
| Audio Codec | AAC / Opus / Copy | Opus requires MKV (not MP4) |
| Audio Bitrate | 192k / 128k / 96k / 64k | Ignored when Audio Codec is Copy |
| Format | MP4 / MKV / MOV | |
| Subtitles | Keep / Burn In / Strip | |
| Originals | Keep / Delete / Ask Each | What to do with source files after encoding |
| GPU Decode | On / Off | Hardware decode with `-hwaccel cuda` |
| Skip Existing | On / Off | Resume interrupted batches |
| Preview | On / Off | Encode only first 60 seconds |
| 10-bit | On / Off | Enables 10-bit pixel depth (p010le for GPU, yuv420p10le for CPU) |
| 2-Pass | On / Off | Two-pass encoding for CPU codecs; ignored for GPU codecs |
| Trim Start | Seconds | Start encoding from this timestamp (leave blank for beginning) |
| Trim End | Seconds | Stop encoding at this timestamp (leave blank for end) |
| Filename Template | Pattern | `{name}`, `{name}_{codec}_{quality}`, `{name}_{date}`, etc. |
| Post-Action | None / Shutdown / Sleep / Command | Action to run after all files finish (GUI only) |
| Concurrent | 1 / 2 / 3 / 4 | Number of files to encode simultaneously (GUI only) |
| GPU Selection | Auto / GPU 0 / GPU 1 / ... | Shown when multiple NVIDIA GPUs detected |

---

## Output

```
My Videos/
|-- video1.mp4              (original)
|-- video2.mkv              (original)
|-- run.bat
|-- transcode.py
|-- gui.py
|-- transcode_log.txt       (session logs)
|-- transcode_config.json   (saved settings)
+-- compressed/
    |-- video1.mp4          (compressed)
    +-- video2.mp4          (compressed)
```

### Results (CLI)

```
+------------ Encoding Results ------------+
| File          | Original | Compressed | Saved | Time  | Valid |
| video1.mp4    |  500 MB  |    150 MB  |  70%  | 2:34  |  OK   |
| video2.mkv    |  300 MB  |     90 MB  |  70%  | 1:45  |  OK   |
+------------------------------------------+

+------------ ALL DONE! ----------------+
| Encoded:   2 files                    |
| Time:      4:19                       |
| Original:  800 MB                     |
| Output:    240 MB                     |
| Saved:     70% (560 MB freed)         |
+---------------------------------------+
```

### Results (GUI)

The Queue tab shows per-file status with savings percentage. The Log tab contains a full session summary, and the History tab keeps a record across sessions.

---

## Log File

`transcode_log.txt` records every session from both interfaces:

```
==================================================
  Session (GUI): 2026-02-27 20:15:00
  GPU: NVIDIA GeForce RTX 3050 Laptop GPU
  H.265 GPU (NVENC) / high / Original / Originalfps / 192k / aac / mp4 / keep
  Hardware decode: enabled
==================================================
  [OK] video1.mp4 | 500MB->150MB (70%) | 2:34 | OK
  [SKIP] video2.mp4 - output exists
  [FAIL] corrupt.mp4 | 0:02
  Summary: 1 ok, 1 skipped, 1 failed | 4:19
```

---

## Configuration

### FFmpeg (Auto-Detected)

FFmpeg is **automatically detected** at startup. The app searches in this order:

1. **Saved path** in `transcode_config.json` (from a previous session)
2. **System PATH** — if you installed FFmpeg and added it to PATH, it just works
3. **Common directories** — `C:\ffmpeg\`, `C:\Program Files\ffmpeg\`, `%LOCALAPPDATA%\ffmpeg\`, `%USERPROFILE%\ffmpeg\` (recursively)

**To install FFmpeg:**
1. Download from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) (get the "essentials" build)
2. Extract to `C:\ffmpeg\` (or anywhere)
3. **Either** add the `bin/` folder to your system PATH, **or** just leave it in `C:\ffmpeg\` — the app will find it

The detected paths are cached in `transcode_config.json` so lookup only happens once.

### Settings Persistence

Both the GUI and CLI save settings to `transcode_config.json` after each session. The GUI automatically restores all settings on startup, so you don't have to reconfigure every time.

Saved fields include: codec, quality, resolution, FPS, audio bitrate, audio codec, format, subtitle mode, delete originals, skip existing, hardware decode, 10-bit, 2-pass, filename template, post-action, post-command, concurrent workers, and FFmpeg paths.

### Video Extensions

Supported formats (configurable in `transcode.py`):

```python
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
```

### Output Directory

Default output folder (configurable in `transcode.py`):

```python
OUTPUT_DIR = "compressed"
```

In the GUI, you can also change the output folder per-session using the **Change** button.

---

## Troubleshooting

| Issue | Solution |
|---|---|
| "Python is not installed" | Install from [python.org/downloads](https://www.python.org/downloads/) and check "Add to PATH" |
| "Missing 'rich' library" | Run `pip install -r requirements.txt` |
| "Missing 'customtkinter' library" | Run `pip install -r requirements.txt` |
| "FFmpeg not found" | Download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), extract to `C:\ffmpeg\` or add to PATH |
| GPU options not showing | NVIDIA drivers must be installed; `nvidia-smi` must work in a terminal |
| Opus + MP4 warning | Opus audio is not compatible with MP4 containers; switch to MKV or use AAC |
| Encoding fails | Check `transcode_log.txt` for FFmpeg error details; try a different codec |
| Duration mismatch warning | Usually harmless for short mismatches; verify the output file plays correctly |
| Progress stuck at 0% | Very short files may not report progress; encoding still works |
| Drag-and-drop not working in GUI | Install `tkinterdnd2` (`pip install tkinterdnd2`); as a fallback use Browse buttons |
| No thumbnail preview | Install `Pillow` (`pip install Pillow`) |
| No system tray option | Install `pystray` and `Pillow` (`pip install pystray Pillow`) |
| Multiple GPUs not listed | Only NVIDIA GPUs are detected; ensure `nvidia-smi` lists all GPUs |

---

## GUI vs CLI — Which to Use?

| Situation | Recommended |
|---|---|
| First-time user, prefer visual interface | **GUI** (`run_gui.bat`) |
| Batch encode with queue management | **GUI** — add files, review queue, start |
| Want to pause/resume mid-encode | **GUI** — Pause button |
| Need output size estimates before encoding | **GUI** — estimate updates live |
| Quick one-off encode via drag & drop | **CLI** (`run.bat`) — drag file, pick preset, done |
| Remote / headless server | **CLI** — no display required |
| Want per-file progress with thumbnails | **GUI** — click filenames for previews |
| Scripting / automation | **CLI** — extend `transcode.py` with command-line args |

Both interfaces share the same encoding engine, codecs, presets, log file, config file, and output folder.

---

## License

This project is licensed under the [MIT License](LICENSE).
